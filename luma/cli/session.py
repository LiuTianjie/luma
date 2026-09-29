"""Luma CLI: session commands."""
from __future__ import annotations

import argparse
import getpass
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from ..control.client import ControlClient
from ..control.context import list_contexts, load_current_context, save_context, use_context
from ..errors import LumaError
from ..io import write_yaml
from ..installation import installation_diagnostics
from ..userconfig import configured_keys, interactive_configure, masked_config_lines, user_config_path
from .. import __version__
from . import common
from .common import _arg_text, _configured_label, _control_context, _env_text, _output_format, _print_key_values, _print_success, _print_table, _quiet, _status_value, _yes_no


def cmd_init(args: argparse.Namespace) -> int:
    path = args.config or Path("luma.yaml")
    if path.exists():
        print(f"Config already exists: {path}")
        return 0
    data: Dict[str, Any] = {
        "project": "luma",
        "providers": {
            "dns": {
                "type": "cloudflare",
                "zone": "example.com",
                "apiTokenEnv": "CLOUDFLARE_API_TOKEN",
            }
        },
        "nodes": {},
        "defaults": {
            "engine": "nomad",
            "exposure": "cn-edge",
            "stackRoot": "stacks",
            "routesRoot": "routes",
            "publicNetwork": "public",
            "egressNetwork": "egress",
            "entrypoint": "websecure",
            "certResolver": "letsencrypt",
        },
        "git": {"autoCommit": False, "autoPush": False, "commitMessage": "deploy {name} to {region}"},
    }
    write_yaml(path, data)
    print(f"Config created: {path}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    env_file = args.env_file
    checks = [
        ("Python", True, f"{sys.executable} ({sys.version_info.major}.{sys.version_info.minor})", "Install Python 3.9+"),
        ("pip", _module_available("pip"), "python -m pip", "Run: python3 -m ensurepip --upgrade"),
        ("venv", _module_available("venv"), "python -m venv", "Install python3-venv"),
        ("Git", bool(shutil.which("git")), shutil.which("git") or "-", "Optional: install Git for source-checkout development"),
        ("Env file", env_file.exists(), str(env_file), "Optional: cp .env.example .env"),
        ("Docker Compose", _docker_compose_available(), "docker compose", "Optional locally; install Docker to validate rendered stacks"),
    ]
    for name, ok, detail, fix in checks:
        print(f"{name}: {'ok' if ok else 'missing'} ({detail})")
        if not ok:
            print(f"  Fix: {fix}")
    required_ok = all(ok for name, ok, _, _ in checks if name not in {"Docker Compose", "Env file", "Git", "SSH"})
    return 0 if required_ok else 1


def cmd_configure(args: argparse.Namespace) -> int:
    path = user_config_path()
    if args.show:
        keys = configured_keys(path)
        print(f"Config: {path}")
        if not keys:
            print("No keys configured. Run: luma configure --role manager")
            return 0
        for line in masked_config_lines(keys):
            print(line)
        return 0
    if args.role == "client":
        print("Client machines usually do not need local secrets. Run luma login <control-url> --token <management-token>.")
    path = interactive_configure(args.role, path=path)
    print(f"Config saved: {path}")
    for line in masked_config_lines(configured_keys(path)):
        print(line)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"Luma CLI: {__version__}")
    if args.local:
        return 0
    health_context = _version_health_context(args)
    if health_context is None:
        print("Luma Control: not checked (run luma login or pass --control-url)")
        return 0
    endpoint, token, insecure, resolve_ip = health_context
    try:
        payload = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).health()
    except LumaError as exc:
        print(f"Luma Control: unavailable ({exc})")
        return 0
    print(f"Luma Control: {payload.get('version') or 'unknown'}")
    node_join_model = payload.get("nodeJoinModel")
    if node_join_model:
        print(f"Node join model: {node_join_model}")
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, list):
        print("Capabilities: " + ", ".join(str(item) for item in capabilities))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    payload = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).status()
    if _output_format(args) != "text":
        _print_success(args, payload)
        return 0
    if _quiet(args):
        print("ok")
        return 0
    print("Luma status")
    _print_key_values(
        "Control",
        [
            ("API", "ok"),
            ("Cluster", _status_value(payload.get("clusterId"))),
            ("Version", _status_value(payload.get("version"))),
            ("Config", _status_value(payload.get("configPath"))),
        ],
    )
    dns = payload.get("dns") if isinstance(payload.get("dns"), dict) else {}
    token_env = dns.get("tokenEnv") or "CLOUDFLARE_API_TOKEN"
    dns_rows = [
        ("Ready", _yes_no(bool(dns.get("ready")))),
        ("Provider", _status_value(dns.get("provider") or "not configured")),
        ("Zone", _status_value(dns.get("zone"))),
        ("Zone ID", _configured_label(bool(dns.get("zoneIdConfigured")))),
        ("Token", f"{_configured_label(bool(dns.get('tokenConfigured')))} ({token_env})"),
        ("Target", _status_value(dns.get("target"))),
    ]
    missing = dns.get("missing") if isinstance(dns.get("missing"), list) else []
    if missing and not dns.get("ready"):
        dns_rows.append(("Missing", ", ".join(str(item) for item in missing)))
    _print_key_values(
        "DNS",
        dns_rows,
    )
    nomad = payload.get("nomad") if isinstance(payload.get("nomad"), dict) else {}
    _print_key_values(
        "Orchestrator (Nomad)",
        [
            ("Ready", _yes_no(bool(nomad.get("available")))),
            ("Leader", _status_value(nomad.get("leader"))),
        ],
    )
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    storage_classes = storage.get("storageClasses") if isinstance(storage.get("storageClasses"), list) else []
    print()
    print("Storage")
    print(f"  Summary: storageClasses={len(storage_classes)}")
    if storage_classes:
        _print_table(["NAME", "MODE", "PROVIDER", "NODE", "PATH/ENDPOINT", "REGIONS"], _status_storage_rows(storage_classes))
    else:
        print("  No storage classes registered")
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    registered_items = nodes.get("items") if isinstance(nodes.get("items"), list) else []
    nomad = payload.get("nomad") if isinstance(payload.get("nomad"), dict) else {}
    nomad_nodes = nomad.get("nodes") if isinstance(nomad.get("nodes"), list) else []
    print()
    print("Nodes")
    if nomad and not nomad.get("available"):
        print(f"  Orchestrator unavailable ({nomad.get('error') or 'unknown error'})")
    else:
        print(f"  Summary: registered={nodes.get('registered', len(registered_items))}, nomad={len(nomad_nodes)}")
    rows = _status_node_rows(registered_items, nomad_nodes)
    if rows:
        _print_table(["NAME", "REGION", "REGISTERED", "NODE", "ROLE", "AVAIL", "LEADER", "DISPLAY", "AGENT"], rows)
    elif isinstance(nodes.get("names"), list) and nodes.get("names"):
        print("  Registered: " + ", ".join(str(name) for name in nodes["names"]))
    else:
        print("  No nodes reported")
    build = payload.get("build") if isinstance(payload.get("build"), dict) else {}
    build_nodes = [item for item in build.get("nodes") or [] if isinstance(item, dict) and item.get("ready")]
    services = payload.get("services") if isinstance(payload.get("services"), list) else []
    registry_services = [svc for svc in services if isinstance(svc, dict) and "registry" in str(svc.get("name") or "").lower()]
    print()
    print("Build (luma import)")
    default_build_node = str(build.get("defaultNode") or "").strip()
    print(f"  Summary: declared build nodes={len(build_nodes)}, in-cluster registries={len(registry_services)}")
    if default_build_node:
        print(f"  Default: {default_build_node}")
    if build_nodes:
        _print_table(
            ["NAME", "REGION", "OS", "AGENT"],
            [
                [
                    _status_value(item.get("name")),
                    _status_value(item.get("region")),
                    _status_value(item.get("agentOs")),
                    _status_value(item.get("agentStatus")),
                ]
                for item in build_nodes
            ],
        )
    else:
        print("  No declared ready builder nodes (declare a builder node and make sure it advertises docker-build)")
    if registry_services:
        for svc in registry_services:
            print(f"  Registry service: {svc.get('name')} (region={_status_value(svc.get('region'))})")
    else:
        print("  No in-cluster registry (run: luma registry serve --node <build-node>)")
    return 0


def _status_node_rows(registered_items: list[object], orchestrator_nodes: list[object]) -> list[list[str]]:
    merged: dict[str, dict[str, object]] = {}
    for item in registered_items:
        if not isinstance(item, dict):
            continue
        name = _status_value(item.get("name"))
        if name == "-":
            continue
        merged.setdefault(name, {})["registered"] = item
    for item in orchestrator_nodes:
        if not isinstance(item, dict):
            continue
        name = _status_value(item.get("lumaNode") or item.get("hostname") or item.get("id"))
        if name == "-":
            continue
        merged.setdefault(name, {})["orchestrator"] = item

    rows: list[list[str]] = []
    for name in sorted(merged):
        registered = merged[name].get("registered")
        orchestrator = merged[name].get("orchestrator")
        registered_dict = registered if isinstance(registered, dict) else {}
        orchestrator_dict = orchestrator if isinstance(orchestrator, dict) else {}
        display = _status_value(registered_dict.get("displayName"))
        if display == "-":
            display = _status_value(orchestrator_dict.get("hostname"))
        if display == name:
            display = "-"
        rows.append(
            [
                name,
                _status_value(registered_dict.get("region") or orchestrator_dict.get("region")),
                _status_value(registered_dict.get("status")),
                _status_value(orchestrator_dict.get("state")) if orchestrator_dict else "missing",
                _status_value(orchestrator_dict.get("role")),
                _status_value(orchestrator_dict.get("availability")),
                "yes" if orchestrator_dict.get("leader") else "-",
                display,
                _status_value(registered_dict.get("agentStatus")),
            ]
        )
    return rows


def _status_storage_rows(storage_classes: list[object]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in sorted((value for value in storage_classes if isinstance(value, dict)), key=lambda value: str(value.get("name") or "")):
        regions = item.get("regions") if isinstance(item.get("regions"), list) else []
        rows.append(
            [
                _status_value(item.get("name")),
                _status_value(item.get("mode")),
                _status_value(item.get("provider")),
                _status_value(item.get("node")),
                _status_value(item.get("path") or item.get("endpoint")),
                ", ".join(str(region) for region in regions) or "-",
            ]
        )
    return rows


def _version_health_context(args: argparse.Namespace) -> tuple[str, str, bool, str | None] | None:
    if args.control_url:
        return args.control_url, "health", bool(args.insecure), args.resolve_ip
    try:
        context = load_current_context()
    except LumaError:
        return None
    return (
        str(context["endpoint"]),
        str(context["token"]),
        bool(context.get("insecure", False)),
        str(context["resolveIp"]) if context.get("resolveIp") else None,
    )


def _module_available(name: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", f"import {name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _docker_compose_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    result = subprocess.run(
        [docker, "compose", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def cmd_login(args: argparse.Namespace) -> int:
    token = _arg_text(args, "token")
    if getattr(args, "token_stdin", False):
        token = sys.stdin.read().strip()
    elif not token:
        token = _env_text("LUMA_DEPLOY_TOKEN")
        if not token and sys.stdin.isatty():
            token = getpass.getpass("Management token: ").strip()
    if not token:
        raise LumaError("management token is required; use --token-stdin, LUMA_DEPLOY_TOKEN, or an interactive terminal")
    client = common.ControlClient(args.endpoint, token, insecure=args.insecure, resolve_ip=args.resolve_ip)
    result = client.verify_login()
    cluster_id = str(result.get("clusterId") or "")
    if not cluster_id:
        raise LumaError("control API did not return clusterId")
    save_context(
        endpoint=args.endpoint,
        cluster_id=cluster_id,
        token=token,
        insecure=args.insecure,
        resolve_ip=args.resolve_ip,
    )
    if _output_format(args) != "text":
        _print_success(args, {"clusterId": cluster_id, "endpoint": args.endpoint.rstrip("/")})
    else:
        print(f"Logged in to {cluster_id} at {args.endpoint.rstrip('/')}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    if args.context_command == "list":
        contexts = list_contexts()
        if _output_format(args) != "text":
            _print_success(args, {"contexts": contexts})
            return 0
        if not contexts:
            print("No contexts. Run: luma login <control-url> --token <management-token>")
            return 0
        for item in contexts:
            marker = "*" if item.get("current") else " "
            print(f"{marker} {item.get('clusterId')}\t{item.get('endpoint')}")
        return 0
    if args.context_command == "use":
        use_context(args.cluster)
        if _output_format(args) != "text":
            _print_success(args, {"clusterId": args.cluster})
        else:
            print(f"Current context: {args.cluster}")
        return 0
    raise LumaError(f"unknown context command: {args.context_command}")


def cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "local", False):
        result = installation_diagnostics()
        if _output_format(args) != "text":
            _print_success(args, result)
        else:
            print(f"Runtime: {result['runtime']}")
            installation = result.get("installation") or {}
            print(f"Installation: {installation.get('mode', 'development/unmanaged')}")
            print(f"Install root: {installation.get('installHome', '-')}")
            print(f"Command directory: {installation.get('binDir', '-')}")
            policy = result.get("dependencyPolicy") or {}
            print(f"Dependency source: {policy.get('wheelhouse') or policy.get('indexUrl') or 'invalid'}")
            print("Host pip configuration: ignored by managed installation")
            for check in result["checks"]:
                print(f"{check['name']}: {'ok' if check['ok'] else 'fail'}")
                if check.get("detail"):
                    print(f"  {check['detail']}")
            print("Local checks only; agent/Control health was not verified. Run luma doctor for remote checks.")
        return 0 if result["healthy"] else 1
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Control credentials", False, "Use --control-url and LUMA_DEPLOY_TOKEN, or run luma login"))
    try:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        checks[-1] = ("Control credentials", True, endpoint)
        client = common.ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
        verified = client.verify_login()
        control_ok = bool(verified.get("clusterId"))
        checks.append(("Control API", control_ok, "Check the control URL, token, DNS, and HTTPS route"))
        if control_ok:
            _append_control_status_checks(checks, client, deep=bool(getattr(args, "deep", False)))
    except LumaError as exc:
        checks.append(("Control API", False, str(exc)))

    healthy = all(ok for _, ok, _ in checks)
    if _output_format(args) != "text":
        _print_success(args, {"healthy": healthy, "checks": [
            {"name": name, "ok": ok, "fix": fix if not ok else ""} for name, ok, fix in checks
        ]})
        return 0 if healthy else 1
    for name, ok, fix in checks:
        print(f"{name}: {'ok' if ok else 'fail'}")
        if not ok:
            print(f"  Fix: {fix}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def _append_control_status_checks(checks: list[tuple[str, bool, str]], client: ControlClient, *, deep: bool = False) -> None:
    try:
        status = client.status()
    except LumaError as exc:
        checks.append(("Control status", False, str(exc)))
        return
    checks.append(("Control status", True, ""))
    dns = status.get("dns") if isinstance(status.get("dns"), dict) else {}
    dns_missing = dns.get("missing") if isinstance(dns.get("missing"), list) else []
    checks.append(
        (
            "DNS readiness",
            bool(dns.get("ready")),
            "Configure DNS provider, zone, token, and edge target"
            + (f"; missing: {', '.join(str(item) for item in dns_missing)}" if dns_missing else ""),
        )
    )
    nomad = status.get("nomad") if isinstance(status.get("nomad"), dict) else {}
    checks.append(
        (
            "Nomad readiness",
            bool(nomad.get("available")),
            str(nomad.get("error") or "Check the Nomad server and rerun manager bootstrap/update"),
        )
    )
    checks.append(("Scheduler availability", bool(nomad.get("available")), str(nomad.get("error") or "Check Nomad on the manager")))
    nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
    node_items = nodes.get("items") if isinstance(nodes.get("items"), list) else []
    checks.append(("Registered nodes", bool(node_items), "Run `luma node join` on at least one worker or rerun manager bootstrap"))
    pending_agents = [
        str(item.get("name") or "")
        for item in node_items
        if isinstance(item, dict) and str(item.get("agentStatus") or "") in {"provisioned", "offline"}
    ]
    checks.append(
        (
            "Node agent heartbeats",
            not pending_agents,
            "Restart or reinstall Luma node agent on: " + ", ".join(name for name in pending_agents if name),
        )
    )
    if deep:
        _append_deep_node_checks(checks, node_items)


def _append_deep_node_checks(checks: list[tuple[str, bool, str]], node_items: list[Any]) -> None:
    for item in node_items:
        if not isinstance(item, dict):
            continue
        node_name = str(item.get("name") or "node")
        diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
        docker = diagnostics.get("docker") if isinstance(diagnostics.get("docker"), dict) else {}
        nomad = diagnostics.get("nomad") if isinstance(diagnostics.get("nomad"), dict) else {}
        mirror_fix = _docker_mirror_fix(docker.get("mirrors"))
        checks.append((f"Node {node_name} docker mirrors", not mirror_fix, mirror_fix or "Docker registry mirrors look reachable or are not configured"))
        proxy = docker.get("proxy") if isinstance(docker.get("proxy"), dict) else {}
        no_proxy_fix = _docker_proxy_no_proxy_fix(proxy)
        checks.append((f"Node {node_name} Docker NO_PROXY", not no_proxy_fix, no_proxy_fix or "Docker daemon proxy bypass includes local registries"))
        driver = nomad.get("dockerDriver") if isinstance(nomad.get("dockerDriver"), dict) else {}
        timeout_value = str(driver.get("pullActivityTimeout") or "").strip()
        timeout_seconds = _duration_seconds(timeout_value)
        timeout_ok = timeout_seconds >= 30 * 60
        checks.append(
            (
                f"Node {node_name} Nomad Docker pull timeout",
                timeout_ok,
                f"Set Nomad Docker driver pull_activity_timeout = \"30m\" and restart nomad (current: {timeout_value or 'missing'})",
            )
        )
        cni = nomad.get("cniHostPorts") if isinstance(nomad.get("cniHostPorts"), dict) else {}
        cni_fixes = [
            fix
            for fix in (
                _cni_missing_network_fix(cni.get("missingNetworks")),
                _cni_hostport_fix(cni.get("conflicts")),
            )
            if fix
        ]
        checks.append(
            (
                f"Node {node_name} Nomad CNI hostports",
                not cni_fixes,
                " ".join(cni_fixes) or "Nomad allocation networks and CNI hostport rules look healthy",
            )
        )
        pull_errors = diagnostics.get("recentImagePullErrors") if isinstance(diagnostics.get("recentImagePullErrors"), list) else []
        checks.append(
            (
                f"Node {node_name} recent image pulls",
                not pull_errors,
                _image_pull_fix(pull_errors),
            )
        )


def _cni_hostport_fix(raw_conflicts: Any) -> str:
    if not isinstance(raw_conflicts, list) or not raw_conflicts:
        return ""
    details: list[str] = []
    for item in raw_conflicts:
        if not isinstance(item, dict):
            continue
        protocol = str(item.get("protocol") or "tcp").strip() or "tcp"
        port = str(item.get("port") or "").strip()
        alloc_ids = item.get("allocIds") if isinstance(item.get("allocIds"), list) else []
        alloc_text = " -> ".join(str(alloc_id) for alloc_id in alloc_ids if str(alloc_id or "").strip())
        if port:
            details.append(f"{protocol}/{port}" + (f" {alloc_text}" if alloc_text else ""))
    if not details:
        return ""
    return "Clear stale CNI-HOSTPORT-DNAT rules or recreate the affected allocation after cleaning CNI state; duplicated hostports: " + "; ".join(details[:6])


def _cni_missing_network_fix(raw_missing: Any) -> str:
    if not isinstance(raw_missing, list) or not raw_missing:
        return ""
    allocations = sorted(
        {
            str(item.get("allocId") or "").strip()
            for item in raw_missing
            if isinstance(item, dict) and str(item.get("allocId") or "").strip()
        }
    )
    if not allocations:
        return ""
    return (
        "Recreate the affected allocation(s): their Nomad CNI namespace has only loopback after a Docker restart; "
        "allocations: " + ", ".join(allocations[:8])
    )


def _docker_mirror_fix(raw_mirrors: Any) -> str:
    if not isinstance(raw_mirrors, list):
        return ""
    failures: list[str] = []
    for mirror in raw_mirrors:
        if isinstance(mirror, dict):
            ok = bool(mirror.get("ok", True))
            if not ok:
                url = str(mirror.get("url") or mirror.get("mirror") or "").strip()
                message = str(mirror.get("message") or mirror.get("error") or "unreachable").strip()
                failures.append(f"bad mirror {url}: {message}".strip())
    return "; ".join(failures)


def _duration_seconds(value: str) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return 0
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", text):
        matched = True
        number = float(amount)
        if unit == "h":
            total += number * 3600
        elif unit == "m":
            total += number * 60
        elif unit == "s":
            total += number
        elif unit == "ms":
            total += number / 1000
        elif unit in {"us", "µs"}:
            total += number / 1_000_000
        elif unit == "ns":
            total += number / 1_000_000_000
    if matched:
        return int(total)
    if text.isdigit():
        return int(text)
    return 0


def _docker_proxy_no_proxy_fix(proxy: Dict[str, Any]) -> str:
    http_proxy = str(proxy.get("http") or proxy.get("HTTPProxy") or "").strip()
    https_proxy = str(proxy.get("https") or proxy.get("HTTPSProxy") or "").strip()
    if not http_proxy and not https_proxy:
        return ""
    no_proxy = str(proxy.get("noProxy") or proxy.get("NoProxy") or "").lower()
    expected = ["localhost", "127.0.0.1", "gcode.gaojiua.com"]
    missing = [item for item in expected if item not in no_proxy]
    if not missing:
        return ""
    return "Docker daemon image-pull proxy is active; add to NO_PROXY: " + ", ".join(missing)


def _image_pull_fix(raw_errors: list[Any]) -> str:
    errors = [str(item).strip() for item in raw_errors if str(item).strip()]
    if not errors:
        return "No recent image pull errors"
    return (
        errors[0]
        + " — check Docker daemon registry mirrors/proxy/NO_PROXY. "
        "Runtime `proxy: true` does not affect Docker image pulls."
    )
