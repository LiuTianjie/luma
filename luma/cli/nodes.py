"""Luma CLI: nodes commands."""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict
from ..bootstrap import _is_tailscale_manager_addr, bootstrap_node, configure_dns, install_docker, install_nomad_node, local_host_name, setup_tailscale
from ..compose import (
    load_compose_deployment,
    resolve_storage_mounts,
    storage_summary,
)
from ..config import load_config
from ..errors import LumaError
from ..io import dump_yaml
from ..node_readiness import wait_for_node_readiness
from ..local import LocalExecutor
from ..profiles import PROFILES
from ..regions import parse_region_name
from ..storage import storage_check_plan, storage_migration_plan
from ..userconfig import ensure_interactive_config
from . import common
from .common import _control_context, _format_epoch, _output_format, _print_deploy_step, _print_success, _print_table, _run_with_wait_heartbeat, log
from .deploy import _compose_request_text, _control_node_records_for_local, _control_storage_classes_for_local
from .manager import _host_from_hostport, _install_node_agent_from_token, _local_agent_config, _local_node, _safe_local_nomad_node_info, local_nomad_node_info


def cmd_node(args: argparse.Namespace) -> int:
    if args.node_command == "list":
        config = load_config(args.config)
        if not config.nodes:
            print("No nodes configured")
            return 0
        for node in config.nodes.values():
            print(f"{node.name}\thost={node.host}\tregion={node.region}\troles={','.join(node.roles)}\tpublicIp={node.public_ip or '-'}")
        return 0
    if args.node_command == "bootstrap":
        config = load_config(args.config)
        node = config.get_node(args.node)
        profile = PROFILES[args.profile]
        bootstrap_node(config, node, profile, run_egress=not args.skip_egress, emit=log)
        print("Bootstrap complete")
        return 0
    if args.node_command == "nomad-join":
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).join_nomad_node(
            node_name=args.name,
            region=args.region,
            server_addr=args.server_addr,
            timeout=int(args.timeout or 1200),
        )
        if _output_format(args) != "text":
            _print_success(args, result)
            return 0
        print(result.get("message") or f"Nomad node joined through node agent: {args.name}")
        print(f"Node: {result.get('nodeName') or args.name}")
        print(f"Nomad node ID: {result.get('nomadNodeId') or result.get('nodeId') or '-'}")
        if result.get("tailscaleIP"):
            print(f"Tailscale IP: {result['tailscaleIP']}")
        return 0
    if args.node_command == "join":
        if not args.region:
            raise LumaError("node join requires --region")
        args.region = parse_region_name(args.region)
        required_worker_keys = ["TAILSCALE_AUTHKEY"] if args.region == "home" and not _local_tailscale_connected() else []
        ensure_interactive_config("worker", required_keys=required_worker_keys)
        log("[start] Configure system DNS")
        log(f"[ok] {configure_dns(LocalExecutor())}")
        log("[start] Install Docker")
        log(f"[ok] {install_docker(LocalExecutor())}")
        client = common.ControlClient(args.endpoint, args.token, insecure=args.insecure, resolve_ip=args.resolve_ip)
        result = client.register_node(node_name=args.name, region=args.region)
        print(f"Node registered: {result['nodeName']} ({result['region']})")
        registered_node_name = str(result.get("nodeName") or args.name)
        node = _local_node_for_region(args.region, name=args.name)
        nomad_rpc_addr = str(result.get("nomadRpcAddr") or result.get("nomadServerAddr") or "").strip()
        if not nomad_rpc_addr:
            raise LumaError("control did not return a Nomad RPC address")
        server_host = _host_from_hostport(nomad_rpc_addr)
        if server_host and _is_tailscale_manager_addr(nomad_rpc_addr) and not _local_tailscale_connected():
            ensure_interactive_config("worker", keys=["TAILSCALE_AUTHKEY"], required_keys=["TAILSCALE_AUTHKEY"])
        try:
            # Regions with egress=proxy use the manager gateway for HashiCorp/GitHub
            # downloads; direct regions download without that proxy.
            egress_proxy = str(result.get("egressProxy") or "").strip() or None
            if egress_proxy is None and str(result.get("egress") or "") == "proxy" and server_host:
                egress_proxy = f"http://{server_host}:7890"
            install_nomad_node(
                node,
                role="client",
                region=args.region,
                node_name=registered_node_name,
                server_addrs=[nomad_rpc_addr],
                egress_proxy=egress_proxy,
                emit=log,
                install_docker_first=False,
                insecure_registries=result.get("insecureRegistries") or [],
            )
        except LumaError as exc:
            log(f"[start] Roll back node registration: {registered_node_name}")
            try:
                client.unregister_node(node_name=registered_node_name)
            except LumaError as cleanup_exc:
                raise LumaError(
                    f"{exc}. Node registration cleanup also failed; run `luma node remove "
                    f"{registered_node_name}` after fixing control API access."
                ) from exc
            log(f"[ok] Rolled back node registration: {registered_node_name}")
            raise
        actual_node_name, actual_node_id = local_nomad_node_info()
        label_result = client.label_node(
            node_name=actual_node_name,
            region=args.region,
            registered_name=args.name,
            node_id=actual_node_id,
            tailscale_ip=_local_tailscale_ip(),
        )
        print(label_result.get("message", f"Node labels applied: {actual_node_name}"))
        agent_token = str(label_result.get("agentToken") or "")
        if agent_token:
            print("[start] Install Luma node agent")
            _install_node_agent_from_token(
                endpoint=args.endpoint,
                agent_token=agent_token,
                node_name=str(label_result.get("nodeName") or args.name),
                node_id=actual_node_id,
                insecure=args.insecure,
                resolve_ip=args.resolve_ip,
            )
            print("[ok] Luma node agent installed")
        else:
            raise LumaError("Node join incomplete: Control did not issue agent credentials. Update the manager, then rerun node join; the registered node was not removed.")
        print("[start] Verify node agent heartbeat with Control")
        verification = wait_for_node_readiness(
            common.ControlClient(args.endpoint, agent_token, insecure=args.insecure, resolve_ip=args.resolve_ip),
            node_name=str(label_result.get("nodeName") or args.name), node_id=actual_node_id,
        )
        print(f"[ok] Node agent ready: {verification.get('agentVersion') or 'unknown version'}")
        print("Node join complete")
        return 0
    if args.node_command == "exit":
        for message in exit_local_node(
            endpoint=args.endpoint,
            token=args.token,
            name=args.name,
            insecure=args.insecure,
            resolve_ip=args.resolve_ip,
            tailscale=args.tailscale,
            prune_docker=args.prune_docker,
        ):
            print(message)
        print("Node exit complete")
        return 0
    if args.node_command == "remove":
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).unregister_node(node_name=args.name)
        print(result.get("message", f"Node removed: {args.name}"))
        skipped = str(result.get("nomadDrainSkipped") or "").strip()
        if skipped:
            print(f"Nomad drain skipped: {skipped}")
        return 0
    if args.node_command == "status":
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        payload = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).status()
        if args.name:
            payload = _filter_node_status_payload(payload, args.name)
        if _output_format(args) != "text":
            _print_success(args, payload)
            return 0
        registered = ((payload.get("nodes") or {}).get("items") if isinstance(payload.get("nodes"), dict) else [])
        if not isinstance(registered, list) or not registered:
            if args.name:
                raise LumaError(f"node not found: {args.name}")
            print("No nodes registered")
            return 0
        rows = []
        for item in registered:
            if not isinstance(item, dict):
                continue
            rows.append(
                [
                    str(item.get("name") or ""),
                    str(item.get("region") or "-"),
                    str(item.get("status") or "-"),
                    str(item.get("agentStatus") or "missing"),
                    str(item.get("agentOs") or "-"),
                    ",".join(str(value) for value in item.get("storageCapabilities") or []) or "-",
                    _format_epoch(int(item.get("agentLastSeen") or 0)),
                ]
            )
        _print_table(["NODE", "REGION", "NODE STATUS", "AGENT", "OS", "CAPABILITIES", "LAST SEEN"], rows)
        return 0
    raise LumaError(f"unknown node command: {args.node_command}")


def _filter_node_status_payload(payload: Dict[str, Any], name: str) -> Dict[str, Any]:
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    registered = nodes.get("items") if isinstance(nodes.get("items"), list) else []
    matched_registered = [item for item in registered if isinstance(item, dict) and _node_status_item_matches(item, name)]
    if not matched_registered:
        return {**payload, "nodes": {**nodes, "items": [], "registered": 0, "names": []}}

    matched_names = {_node_status_canonical_name(item) for item in matched_registered}
    matched_aliases = set[str]()
    for item in matched_registered:
        matched_aliases.update(_node_status_names(item))

    nomad = payload.get("nomad") if isinstance(payload.get("nomad"), dict) else {}
    nomad_nodes = nomad.get("nodes") if isinstance(nomad.get("nodes"), list) else []
    matched_nomad = [
        item
        for item in nomad_nodes
        if isinstance(item, dict)
        and (
            _node_status_item_matches(item, name)
            or _node_status_canonical_name(item) in matched_names
            or bool(_node_status_names(item) & matched_aliases)
        )
    ]
    next_nodes = {**nodes, "items": matched_registered, "registered": len(matched_registered), "names": sorted(matched_names)}
    return {**payload, "nodes": next_nodes, "nomad": {**nomad, "nodes": matched_nomad}}


def _node_status_item_matches(item: Dict[str, Any], name: str) -> bool:
    return name in _node_status_names(item)


def _node_status_canonical_name(item: Dict[str, Any]) -> str:
    return str(item.get("name") or item.get("lumaNode") or item.get("displayName") or "").strip()


def _node_status_names(item: Dict[str, Any]) -> set[str]:
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    values = {
        str(item.get("name") or "").strip(),
        str(item.get("displayName") or "").strip(),
        str(item.get("hostname") or "").strip(),
        str(item.get("lumaNode") or "").strip(),
        str(item.get("nodeId") or "").strip(),
        str(item.get("address") or "").strip(),
        str(labels.get("luma.node.name") or "").strip(),
        str(labels.get("luma_node_name") or "").strip(),
    }
    aliases = item.get("aliases")
    if isinstance(aliases, list):
        values.update(str(value).strip() for value in aliases)
    elif isinstance(aliases, str):
        values.add(aliases.strip())
    return {value for value in values if value}


def _local_node_for_region(region: str, *, name: str | None = None):
    from ..config import NodeConfig

    roles = [region]
    return NodeConfig(
        name=name or os.uname().nodename,
        host="localhost",
        region=region,
        roles=roles,
        raw={},
    )


def _join_profile_for_region(region: str):
    from ..profiles import Profile

    labels = {"region": region}
    roles = [region]
    return Profile(
        name=f"{region}-node",
        roles=roles,
        labels=labels,
        description=f"{region} region worker node",
    )


def exit_local_node(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    name: str | None = None,
    insecure: bool = False,
    resolve_ip: str | None = None,
    tailscale: bool = False,
    prune_docker: bool = False,
) -> list[str]:
    remote = LocalExecutor()
    results: list[str] = []
    if endpoint and token:
        node_name = name or _local_luma_node_name(remote) or local_host_name(remote)
        result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).unregister_node(node_name=node_name)
        results.append(str(result.get("message") or f"Node unregistered: {node_name}"))
    results.append(_stop_local_nomad(remote))
    results.append(_remove_local_runtime_state(remote))
    if tailscale:
        results.append(_tailscale_logout(remote))
    if prune_docker:
        results.append(_prune_local_docker(remote))
    return results


def _local_luma_node_name(remote: LocalExecutor) -> str:
    agent = _local_agent_config()
    if agent:
        name = str(agent.get("nodeName") or "").strip()
        if name:
            return name
    name, _node_id = _safe_local_nomad_node_info()
    return name


def _stop_local_nomad(remote: LocalExecutor) -> str:
    result = remote.sudo_result(
        "set -euo pipefail; "
        "if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files nomad.service >/dev/null 2>&1; then "
        "systemctl disable --now nomad >/dev/null 2>&1 || true; echo stopped; "
        "elif command -v launchctl >/dev/null 2>&1 && [ -f /Library/LaunchDaemons/io.luma.nomad.plist ]; then "
        "launchctl unload /Library/LaunchDaemons/io.luma.nomad.plist >/dev/null 2>&1 || true; echo stopped; "
        "else echo skipped; fi"
    )
    if result.code != 0:
        raise LumaError(f"failed to stop Nomad agent:\n{result.output.strip()}")
    status = _last_nonempty_line(result.output)
    if status == "left":
        return "Nomad agent stopped"
    if status == "stopped":
        return "Nomad agent stopped"
    return "Nomad agent stop skipped"


def _remove_local_runtime_state(remote: LocalExecutor) -> str:
    remote.sudo("rm -rf /opt/luma")
    return "Removed /opt/luma"


def _tailscale_logout(remote: LocalExecutor) -> str:
    result = remote.sudo_result(
        "if command -v tailscale >/dev/null 2>&1; then tailscale logout >/dev/null 2>&1 || true; echo done; else echo skipped; fi"
    )
    if result.code != 0:
        raise LumaError(f"failed to log out Tailscale:\n{result.output.strip()}")
    if _last_nonempty_line(result.output) == "skipped":
        return "Tailscale logout skipped"
    return "Tailscale logged out"


def _prune_local_docker(remote: LocalExecutor) -> str:
    result = remote.sudo_result(
        "if command -v docker >/dev/null 2>&1; then "
        "mkdir -p /opt/luma/events; "
        "ts=$(date -u +%Y-%m-%dT%H:%M:%SZ); "
        "printf '{\"ts\":\"%s\",\"event\":\"docker-prune\",\"source\":\"luma node exit --prune-docker\"}\\n' \"$ts\" "
        ">> /opt/luma/events/node-exit.jsonl; "
        "logger -t luma 'Docker prune requested by luma node exit --prune-docker'; "
        "docker system prune -af --volumes >/dev/null; "
        "echo done; "
        "else echo skipped; fi"
    )
    if result.code != 0:
        raise LumaError(f"failed to prune Docker:\n{result.output.strip()}")
    if _last_nonempty_line(result.output) == "skipped":
        return "Docker prune skipped"
    return "Docker pruned"


def cmd_tailscale(args: argparse.Namespace) -> int:
    if args.tailscale_command == "connect":
        ensure_interactive_config("worker", keys=["TAILSCALE_AUTHKEY"], required_keys=["TAILSCALE_AUTHKEY"])
        log("[start] Install and connect Tailscale")
        for line in setup_tailscale(_local_node("single-node"), executor=LocalExecutor()):
            log(f"[ok] {line}")
        return 0
    raise LumaError(f"unknown tailscale command: {args.tailscale_command}")


def _local_tailscale_connected() -> bool:
    return bool(_local_tailscale_ip())


def _local_tailscale_ip() -> str:
    result = LocalExecutor().run_result("command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1")
    if result.code != 0:
        return ""
    return _last_output_line(result.output)


def _last_output_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def cmd_region(args: argparse.Namespace) -> int:
    if args.region_command == "list":
        return cmd_region_list(args)
    if args.region_command == "create":
        return cmd_region_create(args)
    if args.region_command == "remove":
        return cmd_region_remove(args)
    raise LumaError(f"unknown region command: {args.region_command}")


def cmd_region_list(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).list_regions()
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    rows = result.get("regions") if isinstance(result.get("regions"), list) else []
    if not rows:
        print("No regions")
        return 0
    for item in rows:
        if not isinstance(item, dict):
            continue
        kind = "builtin" if item.get("builtin") else "custom"
        exposures = ",".join(str(value) for value in item.get("exposures") or [])
        print(f"{item.get('name')}	{kind}	egress={item.get('egress') or '-'}	exposures={exposures or '-'}")
    return 0


def cmd_region_create(args: argparse.Namespace) -> int:
    name = parse_region_name(args.name)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).create_region(
        name=name,
        egress=str(args.egress or "proxy"),
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(f"Region created: {result.get('name') or name} (egress={result.get('egress') or args.egress})")
    return 0


def cmd_region_remove(args: argparse.Namespace) -> int:
    name = parse_region_name(args.name)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).remove_region(name=name)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(f"Region removed: {result.get('name') or name}")
    return 0


def cmd_storage(args: argparse.Namespace) -> int:
    if args.storage_command == "list":
        return cmd_storage_list(args)
    if args.storage_command == "set":
        return cmd_storage_set(args)
    if args.storage_command == "remove":
        return cmd_storage_remove(args)
    if args.storage_command == "apply":
        return cmd_storage_apply(args)
    if args.storage_command == "check":
        return cmd_storage_check(args)
    if args.storage_command == "migrate":
        return cmd_storage_migrate(args)
    raise LumaError(f"unknown storage command: {args.storage_command}")


def cmd_storage_list(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).list_storage()
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    for item in result.get("storageClasses") or []:
        location = item.get("endpoint") or item.get("path") or item.get("node") or ""
        print(f"{item.get('name')}: {item.get('provider')} {item.get('mode')} {location}".rstrip())
    return 0


def cmd_storage_set(args: argparse.Namespace) -> int:
    if args.external:
        if not args.endpoint:
            raise LumaError("external storage requires --endpoint")
        if not args.regions:
            raise LumaError("external storage requires at least one --region")
        if args.node or args.path:
            raise LumaError("external storage cannot set --node or --path")
    else:
        if not args.node:
            raise LumaError("managed storage requires --node")
        if not args.path:
            raise LumaError("managed storage requires --path")
        if args.endpoint:
            raise LumaError("managed storage endpoint is resolved automatically; do not pass --endpoint")
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).set_storage(
        name=args.name,
        provider=args.provider,
        external=args.external,
        node=args.node,
        path=args.path,
        endpoint=args.endpoint,
        mount_options=args.mount_options,
        regions=args.regions,
        nodes=args.nodes,
        timeout=args.timeout,
    )
    print(f"Storage class saved: {result.get('name', args.name)}")
    _print_storage_host_result(result.get("storageHost"))
    return 0


def cmd_storage_remove(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).remove_storage(
        name=args.name,
        timeout=args.timeout,
    )
    status = "removed" if result.get("removed") else "not configured"
    print(f"Storage class {status}: {args.name}")
    _print_storage_host_result(result.get("storageHost"))
    return 0


def _print_storage_host_result(value: Any) -> None:
    if not isinstance(value, dict):
        return
    prepared = value.get("prepared")
    removed = value.get("removed")
    export = value.get("export")
    if prepared:
        print(f"Storage host: {prepared}")
    if removed:
        print(f"Storage cleanup: {removed}")
    if export:
        print(f"Storage export: {export}")


def cmd_storage_apply(args: argparse.Namespace) -> int:
    storage_classes = _control_storage_classes_for_local(args, required=True)
    node_records = _control_node_records_for_local(args, required=True)
    deployment = load_compose_deployment(
        args.sidecar,
        storage_classes=storage_classes,
        allow_build_services=True,
    )
    # Storage preflight must validate placement/endpoints without requiring
    # Builder-produced image references to exist yet.
    resolve_storage_mounts(deployment, node_records=node_records)
    if args.dry_run:
        print(dump_yaml(storage_summary(deployment, node_records=node_records)))
        return 0
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    manifest_text, compose_text = _compose_request_text(args.sidecar, deployment)
    result = _run_with_wait_heartbeat(
        lambda: client.apply_storage(
            manifest=manifest_text,
            compose_content=compose_text,
            source_name=str(args.sidecar),
            timeout=args.timeout,
        ),
        timeout=args.timeout,
    )
    for step in result.get("steps") or []:
        if isinstance(step, dict):
            _print_deploy_step(step)
    print(f"[ok] Storage apply finished: {result.get('deployment', deployment.name)}")
    return 0


def cmd_storage_check(args: argparse.Namespace) -> int:
    storage_classes = _control_storage_classes_for_local(args, required=True)
    node_records = _control_node_records_for_local(args, required=True)
    deployment = load_compose_deployment(
        args.sidecar,
        storage_classes=storage_classes,
        allow_build_services=True,
    )
    result = storage_check_plan(deployment, node_records=node_records)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    for item in result.get("mounts") or []:
        print(
            f"{item['service']}/{item['volume']}: {item['storageClass']} "
            f"{item.get('mode', '')} via {item['networkPath']} {item['endpoint']} path={item.get('path', '')}".rstrip()
        )
    for item in result["storageClasses"]:
        print(f"{item['name']}: {item['message']}")
    for warning in result["warnings"]:
        print(f"[warn] {warning}")
    return 0


def cmd_storage_migrate(args: argparse.Namespace) -> int:
    deployment = load_compose_deployment(
        args.sidecar,
        storage_classes=_control_storage_classes_for_local(args, required=True),
        allow_build_services=True,
    )
    result = storage_migration_plan(
        deployment,
        volume=args.volume,
        from_node=args.from_node,
        from_volume=args.from_volume,
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(result["message"])
    return 0


def _looks_like_sudo_auth_failure(output: str) -> bool:
    lower = output.lower()
    return "sudo" in lower and ("password" in lower or "a terminal is required" in lower or "no tty present" in lower)


def _last_nonempty_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    if line.startswith("[sudo]") and ":" in line:
        return line.rsplit(":", 1)[-1].strip()
    return line


def _service_ready(output: str, name: str) -> bool:
    for line in output.splitlines():
        if line.startswith("[sudo]") and ": " in line:
            line = line.rsplit(": ", 1)[-1]
        parts = line.split()
        if len(parts) < 2 or parts[0] != name:
            continue
        replicas = parts[1]
        if "/" not in replicas:
            return False
        running, desired = replicas.split("/", 1)
        return running == desired and desired != "0"
    return False
