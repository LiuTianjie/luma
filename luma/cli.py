from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from .bootstrap import _is_tailscale_manager_addr, bootstrap_manager_local, bootstrap_node, configure_dns, join_local_node, local_docker_node_id, local_docker_node_name, setup_egress, setup_portainer, setup_tailscale
from .cloudflare import find_zone, sync_dns
from .config import LumaConfig, load_config, save_config
from .control.client import ControlClient
from .control.context import list_contexts, load_current_context, save_context, use_context
from .control.state import load_state, new_state, state_path
from .envfile import load_env_file
from .errors import LumaError
from .io import dump_yaml, write_yaml
from .local import LocalExecutor
from .profiles import PROFILES
from .remote import RemoteExecutor
from .render import render_stack, render_tailscale_route, route_path, stack_path
from .service import VALID_EXPOSURES, VALID_REGIONS, load_service, slugify
from .userconfig import configured_keys, ensure_interactive_config, interactive_configure, load_user_config, masked_config_lines, user_config_path
from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma", description="Self-hosted deployment control plane.")
    parser.add_argument("--config", type=Path, default=None, help="Path to luma.yaml")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to local env file")
    parser.add_argument("--no-env", action="store_true", help="Do not load .env")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    version = sub.add_parser("version")
    version.add_argument("--control-url", help="Control API URL to check instead of the current login context")
    version.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API check")
    version.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")
    version.add_argument("--local", action="store_true", help="Only print the local CLI version")
    status = sub.add_parser("status")
    status.add_argument("--control-url", help="Control API URL to check instead of the current login context")
    status.add_argument("--token", help="Deploy token to use with --control-url")
    status.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API check")
    status.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")
    sub.add_parser("preflight")
    configure = sub.add_parser("configure")
    configure.add_argument("--role", choices=("manager", "worker", "client"), default="manager")
    configure.add_argument("--show", action="store_true", help="Show configured key names without printing secret values")
    login = sub.add_parser("login")
    login.add_argument("endpoint")
    login.add_argument("--token", required=True)
    login.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    login.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_sub.add_parser("list")
    context_use = context_sub.add_parser("use")
    context_use.add_argument("cluster")
    secret = sub.add_parser("secret")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)
    secret_sub.add_parser("list")
    secret_set = secret_sub.add_parser("set")
    secret_set.add_argument("name")
    secret_set.add_argument("--value")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap_sub = bootstrap.add_subparsers(dest="bootstrap_command", required=True)
    manager = bootstrap_sub.add_parser("manager")
    manager.add_argument("--domain", required=True)
    manager.add_argument("--node")
    manager.add_argument("--profile", choices=sorted(PROFILES), default="single-node")
    manager.add_argument("--http-port", type=int, help="Public Traefik HTTP port")
    manager.add_argument("--https-port", type=int, help="Public Traefik HTTPS port")
    manager.add_argument("--skip-egress", action="store_true")
    manager.add_argument("--overwrite-control-state", action="store_true")
    update = sub.add_parser(
        "update",
        description=(
            "Update the local CLI. With no target, Luma refreshes the manager only "
            "when local manager state exists and the control API version differs; "
            "clients and workers update CLI only."
        ),
        epilog="Examples: luma update | luma update --install-ref v0.1.10 | luma update manager --domain luma.example.com",
    )
    _add_update_manager_arguments(update)
    update_sub = update.add_subparsers(dest="update_command", required=False, metavar="[target]")
    update_manager = update_sub.add_parser("manager", help="force a manager bootstrap refresh")
    _add_update_manager_arguments(update_manager)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--deep", action="store_true", help="Run slower live checks such as docker pull through egress")
    doctor.add_argument("--legacy-ssh", action="store_true", help="Also run legacy SSH checks for nodes in luma.yaml")

    node = sub.add_parser("node")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    node_sub.add_parser("list")
    node_bootstrap = node_sub.add_parser("bootstrap")
    node_bootstrap.add_argument("node")
    node_bootstrap.add_argument("--profile", choices=sorted(PROFILES), required=True)
    node_bootstrap.add_argument("--skip-egress", action="store_true", help="Skip egress setup during bootstrap; run luma egress setup later")
    node_join = node_sub.add_parser("join")
    node_join.add_argument("endpoint")
    node_join.add_argument("--token", required=True)
    node_join.add_argument("--profile", nargs="?", const="__legacy_profile__", help=argparse.SUPPRESS)
    node_join.add_argument("--region", choices=sorted(VALID_REGIONS))
    node_join.add_argument("--name", default=os.uname().nodename)
    node_join.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_join.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_exit = node_sub.add_parser("exit")
    node_exit.add_argument("--endpoint", help="Control endpoint; when set, unregister this node from Luma Control")
    node_exit.add_argument("--token", help="Deploy or join token used with --endpoint")
    node_exit.add_argument("--name", help="Luma node name to unregister; defaults to this node's registered label or Docker name")
    node_exit.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_exit.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_exit.add_argument("--tailscale", action="store_true", help="Also log out Tailscale on this node")
    node_exit.add_argument("--prune-docker", action="store_true", help="Also prune unused Docker containers, networks, images, and volumes")
    node_remove = node_sub.add_parser("remove")
    node_remove.add_argument("name")

    cf = sub.add_parser("cloudflare")
    cf_sub = cf.add_subparsers(dest="cloudflare_command", required=True)
    cf_connect = cf_sub.add_parser("connect")
    cf_connect.add_argument("--zone", required=True)

    egress = sub.add_parser("egress")
    egress_sub = egress.add_subparsers(dest="egress_command", required=True)
    for name in ("setup", "refresh"):
        cmd = egress_sub.add_parser(name)
        cmd.add_argument("node", nargs="?", help="Legacy remote node name. Omit to repair the current server.")

    portainer = sub.add_parser("portainer")
    portainer_sub = portainer.add_subparsers(dest="portainer_command", required=True)
    portainer_setup = portainer_sub.add_parser("setup")
    portainer_setup.add_argument("node", nargs="?", help="Legacy remote node name. Omit to repair the current server.")

    tailscale = sub.add_parser("tailscale")
    tailscale_sub = tailscale.add_subparsers(dest="tailscale_command", required=True)
    tailscale_connect = tailscale_sub.add_parser("connect")
    tailscale_connect.add_argument("node", nargs="?", help="Legacy remote node name. Omit to repair the current server.")

    service = sub.add_parser("service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_new = service_sub.add_parser("new")
    service_new.add_argument("--output", type=Path)

    for name in ("validate", "render"):
        cmd = sub.add_parser(name)
        cmd.add_argument("service", type=Path)

    dns = sub.add_parser("dns-sync")
    dns.add_argument("service", type=Path)

    deploy = sub.add_parser("deploy")
    deploy.add_argument("service", type=Path)
    deploy.add_argument("--dry-run", action="store_true")
    deploy.add_argument("--skip-dns", action="store_true")
    deploy.add_argument("--skip-webhook", action="store_true")
    deploy.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the control-plane deploy response")
    deploy.add_argument("--commit", action="store_true", help="Deprecated for control-plane deploy")
    deploy.add_argument("--push", action="store_true", help="Deprecated for control-plane deploy")
    deploy.add_argument("--via", choices=("portainer",), default="portainer", help="Deployment runner")

    return parser


def _add_update_manager_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--domain", help="Control domain. Defaults to the domain stored in /opt/luma/control/control.json.")
    parser.add_argument("--node")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="single-node")
    parser.add_argument("--http-port", type=int, help="Public Traefik HTTP port")
    parser.add_argument("--https-port", type=int, help="Public Traefik HTTPS port")
    parser.add_argument("--skip-egress", action="store_true")
    parser.add_argument("--overwrite-control-state", action="store_true")
    parser.add_argument("--install-ref", help="Git ref passed to the install script as LUMA_INSTALL_REF")


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
            },
            "portainer": {
                "webhookUrlEnv": "PORTAINER_WEBHOOK_URL",
            },
        },
        "nodes": {},
        "defaults": {
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
        ("SSH", bool(shutil.which("ssh")), shutil.which("ssh") or "-", "Optional: install OpenSSH client for legacy remote commands"),
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
        print("Client machines usually do not need local secrets. Run luma login <control-url> --token <deploy-token>.")
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
        payload = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).health()
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
    payload = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).status()
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
    _print_key_values(
        "DNS",
        [
            ("Ready", _yes_no(bool(dns.get("ready")))),
            ("Provider", _status_value(dns.get("provider") or "not configured")),
            ("Zone", _status_value(dns.get("zone"))),
            ("Zone ID", _configured_label(bool(dns.get("zoneIdConfigured")))),
            ("Token", f"{_configured_label(bool(dns.get('tokenConfigured')))} ({token_env})"),
            ("Target", _status_value(dns.get("target"))),
        ],
    )
    portainer = payload.get("portainer") if isinstance(payload.get("portainer"), dict) else {}
    _print_key_values(
        "Portainer",
        [
            ("Ready", _yes_no(bool(portainer.get("ready")))),
            ("API", _status_value(portainer.get("apiUrl"))),
            ("Endpoint", _configured_label(bool(portainer.get("endpointIdConfigured")))),
            ("Swarm ID", _configured_label(bool(portainer.get("swarmIdConfigured")))),
        ],
    )
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    registered_items = nodes.get("items") if isinstance(nodes.get("items"), list) else []
    swarm = payload.get("swarm") if isinstance(payload.get("swarm"), dict) else {}
    swarm_nodes = swarm.get("nodes") if isinstance(swarm.get("nodes"), list) else []
    print()
    print("Nodes")
    if swarm and not swarm.get("available"):
        print(f"  Swarm: unavailable ({swarm.get('error') or 'unknown error'})")
    else:
        print(f"  Summary: registered={nodes.get('registered', len(registered_items))}, swarm={len(swarm_nodes)}")
    rows = _status_node_rows(registered_items, swarm_nodes)
    if rows:
        _print_table(["NAME", "REGION", "REGISTERED", "SWARM", "ROLE", "AVAIL", "LEADER", "DISPLAY"], rows)
    elif isinstance(nodes.get("names"), list) and nodes.get("names"):
        print("  Registered: " + ", ".join(str(name) for name in nodes["names"]))
    else:
        print("  No nodes reported")
    return 0


def _configured_label(value: bool) -> str:
    return "configured" if value else "missing"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _status_value(value: object) -> str:
    text = str(value or "").strip()
    return text or "-"


def _print_key_values(title: str, rows: list[tuple[str, str]]) -> None:
    print()
    print(title)
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label.ljust(width)}  {value}")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  " + "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        print("  " + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _status_node_rows(registered_items: list[object], swarm_nodes: list[object]) -> list[list[str]]:
    merged: dict[str, dict[str, object]] = {}
    for item in registered_items:
        if not isinstance(item, dict):
            continue
        name = _status_value(item.get("name"))
        if name == "-":
            continue
        merged.setdefault(name, {})["registered"] = item
    for item in swarm_nodes:
        if not isinstance(item, dict):
            continue
        name = _status_value(item.get("lumaNode") or item.get("hostname") or item.get("id"))
        if name == "-":
            continue
        merged.setdefault(name, {})["swarm"] = item

    rows: list[list[str]] = []
    for name in sorted(merged):
        registered = merged[name].get("registered")
        swarm = merged[name].get("swarm")
        registered_dict = registered if isinstance(registered, dict) else {}
        swarm_dict = swarm if isinstance(swarm, dict) else {}
        display = _status_value(registered_dict.get("displayName"))
        if display == "-":
            display = _status_value(swarm_dict.get("hostname"))
        if display == name:
            display = "-"
        rows.append(
            [
                name,
                _status_value(registered_dict.get("region") or swarm_dict.get("region")),
                _status_value(registered_dict.get("status")),
                _status_value(swarm_dict.get("state")) if swarm_dict else "missing",
                _status_value(swarm_dict.get("role")),
                _status_value(swarm_dict.get("availability")),
                "yes" if swarm_dict.get("leader") else "-",
                display,
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


def _control_context(args: argparse.Namespace, *, require_token: bool) -> tuple[str, str, bool, str | None]:
    if args.control_url:
        if require_token and not args.token:
            raise LumaError("--token is required with --control-url")
        return args.control_url, str(args.token or "health"), bool(args.insecure), args.resolve_ip
    context = load_current_context()
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


def log(message: str) -> None:
    print(message, flush=True)


def cmd_node(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.node_command == "list":
        if not config.nodes:
            print("No nodes configured")
            return 0
        for node in config.nodes.values():
            print(f"{node.name}\thost={node.host}\tregion={node.region}\troles={','.join(node.roles)}\tpublicIp={node.public_ip or '-'}")
        return 0
    if args.node_command == "bootstrap":
        node = config.get_node(args.node)
        profile = PROFILES[args.profile]
        bootstrap_node(config, node, profile, run_egress=not args.skip_egress, emit=log)
        print("Bootstrap complete")
        return 0
    if args.node_command == "join":
        if args.profile is not None:
            raise LumaError("node join now uses --region; use --region home/global/cn and --name ...")
        if not args.region:
            raise LumaError("node join requires --region (cn, global, or home)")
        required_worker_keys = ["TAILSCALE_AUTHKEY"] if args.region == "home" and not _local_tailscale_connected() else []
        ensure_interactive_config("worker", required_keys=required_worker_keys)
        log("[start] Configure system DNS")
        log(f"[ok] {configure_dns(LocalExecutor())}")
        client = ControlClient(args.endpoint, args.token, insecure=args.insecure, resolve_ip=args.resolve_ip)
        result = client.register_node(node_name=args.name, region=args.region)
        print(f"Node registered: {result['nodeName']} ({result['region']})")
        manager_addr = result.get("managerAddr")
        swarm_token = result.get("swarmJoinToken")
        if manager_addr and _is_tailscale_manager_addr(str(manager_addr)) and not _local_tailscale_connected():
            ensure_interactive_config("worker", keys=["TAILSCALE_AUTHKEY"], required_keys=["TAILSCALE_AUTHKEY"])
        node = _local_node_for_region(args.region, name=args.name)
        join_local_node(node, _join_profile_for_region(args.region), str(manager_addr or ""), str(swarm_token or ""), emit=log)
        actual_node_name = local_docker_node_name()
        actual_node_id = local_docker_node_id()
        label_result = client.label_node(
            node_name=actual_node_name,
            region=args.region,
            registered_name=args.name,
            node_id=actual_node_id,
        )
        print(label_result.get("message", f"Node labels applied: {actual_node_name}"))
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
        result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).unregister_node(node_name=args.name)
        print(result.get("message", f"Node removed: {args.name}"))
        return 0
    raise LumaError(f"unknown node command: {args.node_command}")


def cmd_login(args: argparse.Namespace) -> int:
    client = ControlClient(args.endpoint, args.token, insecure=args.insecure, resolve_ip=args.resolve_ip)
    result = client.verify_login()
    cluster_id = str(result.get("clusterId") or "")
    if not cluster_id:
        raise LumaError("control API did not return clusterId")
    save_context(
        endpoint=args.endpoint,
        cluster_id=cluster_id,
        token=args.token,
        insecure=args.insecure,
        resolve_ip=args.resolve_ip,
    )
    print(f"Logged in to {cluster_id} at {args.endpoint.rstrip('/')}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    if args.context_command == "list":
        contexts = list_contexts()
        if not contexts:
            print("No contexts. Run: luma login <control-url> --token <token>")
            return 0
        for item in contexts:
            marker = "*" if item.get("current") else " "
            print(f"{marker} {item.get('clusterId')}\t{item.get('endpoint')}")
        return 0
    if args.context_command == "use":
        use_context(args.cluster)
        print(f"Current context: {args.cluster}")
        return 0
    raise LumaError(f"unknown context command: {args.context_command}")


def cmd_secret(args: argparse.Namespace) -> int:
    context = load_current_context()
    client = ControlClient(
        str(context["endpoint"]),
        str(context["token"]),
        insecure=bool(context.get("insecure")),
        resolve_ip=str(context["resolveIp"]) if context.get("resolveIp") else None,
    )
    if args.secret_command == "list":
        result = client.list_secrets()
        keys = result.get("secrets") if isinstance(result.get("secrets"), list) else []
        if not keys:
            print("No deployment secrets configured")
            return 0
        for key in keys:
            print(str(key))
        return 0
    if args.secret_command == "set":
        value = args.value
        if value is None:
            value = getpass.getpass(f"{args.name}: ")
        result = client.set_secret(name=args.name, value=value)
        print(f"Secret saved: {result.get('name', args.name)}")
        return 0
    raise LumaError(f"unknown secret command: {args.secret_command}")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.bootstrap_command == "manager":
        _apply_bootstrap_port_overrides(config, http_port=args.http_port, https_port=args.https_port)
        node = config.get_node(args.node) if args.node else (config.default_manager() or _local_node(args.profile))
        if not node:
            raise LumaError("no manager node configured. Add a node or pass --node.")
        profile = PROFILES[args.profile]
        keys = ["CLOUDFLARE_API_TOKEN", "TRAEFIK_ACME_EMAIL", "TAILSCALE_AUTHKEY", "LUMA_SUDO_PASSWORD"]
        if not _dns_target_for_bootstrap(config, node) and sys.stdin.isatty():
            keys.append("LUMA_DNS_EDGE_TARGET")
        if not args.skip_egress and "egress" in profile.roles:
            keys.append("EGRESS_SUBSCRIPTION_URL")
        ensure_interactive_config("manager", keys=keys)
        _ensure_cloudflare_dns_from_local_config(config, args.domain, node)
        state = _control_state_for_bootstrap(args.domain, overwrite=args.overwrite_control_state)
        _attach_control_secrets(state, config)
        bootstrap_manager_local(config, node, profile, args.domain, state, run_egress=not args.skip_egress, emit=log)
        control_url = _control_url(args.domain, args.https_port or _config_https_port(config))
        print("Bootstrap complete")
        print(f"Control domain: {args.domain}")
        print(f"Control URL: {control_url}")
        portainer_url = _portainer_url_from_state(state)
        if portainer_url:
            print(f"Portainer URL: {portainer_url}")
            print(f"Portainer username: {state.get('portainerAdminUsername', 'admin')}")
            print("Portainer password: sudo jq -r '.portainerAdminPassword' /opt/luma/control/control.json")
        print(f"Cluster: {state['clusterId']}")
        print(f"Deploy token: {state['deployToken']}")
        print(f"Join token: {state['joinToken']}")
        print("Join additional nodes:")
        for label, command in _node_join_examples(control_url, str(state["joinToken"])):
            print(f"  {label}: {command}")
        return 0
    raise LumaError(f"unknown bootstrap command: {args.bootstrap_command}")


def cmd_update(args: argparse.Namespace) -> int:
    if args.update_command in {None, "manager"}:
        print("[start] Update Luma CLI")
        _run_luma_installer(install_ref=args.install_ref)
        print("[ok] Luma CLI updated")
        if args.update_command is None:
            should_refresh, reason = _manager_refresh_decision(args)
            if not should_refresh:
                print(f"[skip] Manager bootstrap refresh skipped: {reason}")
                return 0
            print(f"[info] Manager bootstrap refresh required: {reason}")
        else:
            print("[info] Manager bootstrap refresh forced")
        print("[start] Refresh manager bootstrap")
        command = _updated_manager_bootstrap_command(args)
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise LumaError(f"manager bootstrap refresh failed with exit code {completed.returncode}")
        print("[ok] Manager update complete")
        return 0
    raise LumaError(f"unknown update command: {args.update_command}")


def _manager_refresh_decision(args: argparse.Namespace) -> tuple[bool, str]:
    if _manager_update_options_provided(args):
        return True, "manager update options were provided"
    state = _existing_control_state()
    if not state:
        return False, "no local manager control state found"
    domain = str(state.get("domain") or "").strip()
    if not domain:
        return False, "local manager control state has no domain; run luma update manager --domain <control-domain>"
    installed_version = _installed_cli_version()
    if not installed_version:
        return True, "could not determine updated CLI version"
    control_version = _control_version_for_update(domain, args)
    if not control_version:
        return True, "could not check current control API version"
    if control_version == installed_version:
        return False, f"control API already matches CLI version {installed_version}"
    return True, f"control API {control_version} differs from CLI {installed_version}"


def _manager_update_options_provided(args: argparse.Namespace) -> bool:
    if args.domain or args.node or args.http_port is not None or args.https_port is not None:
        return True
    if args.skip_egress or args.overwrite_control_state:
        return True
    if getattr(args, "profile", "single-node") != "single-node":
        return True
    return False


def _installed_cli_version() -> str:
    try:
        completed = subprocess.run(
            [_luma_executable(), "version", "--local"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    match = re.search(r"^Luma CLI:\s*(\S+)", completed.stdout, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _control_version_for_update(domain: str, args: argparse.Namespace) -> str:
    config = load_config(args.config)
    control_url = _control_url(domain, _config_https_port(config))
    try:
        payload = ControlClient(control_url, "health").health()
    except LumaError:
        return ""
    return str(payload.get("version") or "")


def _run_luma_installer(*, install_ref: str | None = None) -> None:
    env = os.environ.copy()
    if install_ref:
        env["LUMA_INSTALL_REF"] = install_ref
    command = "curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh"
    subprocess.run(command, shell=True, check=True, env=env)


def _updated_manager_bootstrap_command(args: argparse.Namespace) -> list[str]:
    domain = _manager_update_domain(args.domain)
    command = [
        _luma_executable(),
        "bootstrap",
        "manager",
        "--domain",
        domain,
        "--profile",
        args.profile,
    ]
    if args.node:
        command.extend(["--node", args.node])
    if args.http_port is not None:
        command.extend(["--http-port", str(args.http_port)])
    if args.https_port is not None:
        command.extend(["--https-port", str(args.https_port)])
    if args.skip_egress:
        command.append("--skip-egress")
    if args.overwrite_control_state:
        command.append("--overwrite-control-state")
    return command


def _manager_update_domain(explicit_domain: str | None) -> str:
    if explicit_domain:
        return explicit_domain
    state = _existing_control_state()
    if state:
        domain = str(state.get("domain") or "").strip()
        if domain:
            return domain
    raise LumaError("update manager could not infer the control domain. Pass --domain <control-domain> once.")


def _existing_control_state() -> Dict[str, object] | None:
    path = state_path()
    try:
        if path.exists():
            return load_state(path)
    except PermissionError:
        pass
    result = LocalExecutor().sudo_result(f"test -f {shlex.quote(str(path))} && cat {shlex.quote(str(path))}")
    if result.code != 0 or not result.output.strip():
        return None
    raw = result.output.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    data = json.loads(raw[start : end + 1] if start >= 0 and end >= start else raw)
    return data if isinstance(data, dict) else None


def _luma_executable() -> str:
    return shutil.which("luma") or sys.argv[0]


def _apply_bootstrap_port_overrides(config: LumaConfig, *, http_port: int | None, https_port: int | None) -> None:
    if http_port is None and https_port is None:
        return
    defaults = config.raw.setdefault("defaults", {})
    ports = defaults.setdefault("ports", {})
    if http_port is not None:
        ports["traefikHttp"] = http_port
    if https_port is not None:
        ports["traefikHttps"] = https_port


def _config_https_port(config: LumaConfig) -> int:
    ports = config.defaults.get("ports") or {}
    if isinstance(ports, dict):
        return int(ports.get("traefikHttps") or ports.get("https") or 443)
    return 443


def _control_url(domain: str, https_port: int) -> str:
    port = "" if https_port == 443 else f":{https_port}"
    return f"https://{domain}{port}"


def _portainer_url_from_state(state: Dict[str, object]) -> str:
    api_url = str(state.get("portainerApiUrl") or "")
    if not api_url:
        return ""
    return api_url.removesuffix("/api")


def _node_join_examples(control_url: str, join_token: str) -> list[tuple[str, str]]:
    base = f"luma node join {control_url} --token {join_token}"
    return [
        ("cn worker", f"{base} --region cn --name cn-worker-1"),
        ("global worker", f"{base} --region global --name global-sg-1"),
        ("home node", f"{base} --region home --name home-mac-mini"),
    ]


def _control_state_for_bootstrap(domain: str, *, overwrite: bool) -> Dict[str, object]:
    if overwrite:
        return new_state(domain=domain)
    data = _existing_control_state()
    if data:
        data["domain"] = domain
        return data
    return new_state(domain=domain)


def _attach_control_secrets(state: Dict[str, object], config: LumaConfig) -> None:
    names: set[str] = set()
    dns = config.dns
    if dns:
        names.add(str(dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN")))
        names.add(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID")))
    portainer = config.portainer
    if portainer:
        names.add(str(portainer.get("webhookUrlEnv", "PORTAINER_WEBHOOK_URL")))
        webhooks = portainer.get("webhooks") or {}
        if isinstance(webhooks, dict):
            names.update(str(value) for value in webhooks.values() if value)
    names.update(key for key in os.environ if key == "PORTAINER_WEBHOOK_URL" or key.startswith("PORTAINER_WEBHOOK_"))
    names.update(key for key in os.environ if key in {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"})

    existing = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    secrets = dict(existing or {})
    for name in names:
        value = os.environ.get(name)
        if value:
            secrets[name] = value
    if secrets:
        state["secrets"] = secrets


def _ensure_cloudflare_dns_from_local_config(config: LumaConfig, domain: str, node=None) -> None:
    dns = config.dns
    if dns.get("provider"):
        _ensure_dns_edge_target(config, node)
        return
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        _ensure_dns_edge_target(config, node)
        return
    dns_config = _writable_dns_config(config)
    for zone_name in _zone_candidates(domain):
        try:
            zone = find_zone(LumaConfig({"providers": {"dns": {"type": "cloudflare"}}}, None), zone_name)
        except LumaError:
            continue
        dns_config["type"] = "cloudflare"
        dns_config["zone"] = zone_name
        dns_config["zoneId"] = zone["id"]
        dns_config.setdefault("apiTokenEnv", "CLOUDFLARE_API_TOKEN")
        _ensure_dns_edge_target(config, node, dns_config=dns_config)
        if config.path:
            save_config(config)
        return
    raise LumaError(
        "CLOUDFLARE_API_TOKEN is configured, but Cloudflare zone could not be inferred from "
        f"{domain!r}. Run: luma cloudflare connect --zone <zone>, then rerun bootstrap/update manager."
    )


def _writable_dns_config(config: LumaConfig) -> Dict[str, object]:
    providers = config.raw.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise LumaError("providers must be a mapping to configure Cloudflare DNS")
    current = providers.get("dns")
    if current is None and isinstance(config.raw.get("dns"), dict):
        current = dict(config.raw["dns"])
        providers["dns"] = current
    if current is None:
        current = {}
        providers["dns"] = current
    if not isinstance(current, dict):
        raise LumaError("providers.dns must be a mapping to configure Cloudflare DNS")
    return current


def _ensure_dns_edge_target(config: LumaConfig, node=None, *, dns_config: Dict[str, object] | None = None) -> None:
    target = _dns_target_for_bootstrap(config, node)
    if not target:
        return
    dns_config = dns_config or _writable_dns_config(config)
    if dns_config.get("edgeTarget"):
        return
    dns_config["edgeTarget"] = target
    if config.path:
        save_config(config)


def _dns_target_for_bootstrap(config: LumaConfig, node=None) -> str:
    env_target = os.environ.get("LUMA_DNS_EDGE_TARGET", "").strip()
    if env_target:
        return env_target
    config_target = config.default_dns_target()
    if config_target:
        return str(config_target)
    node_target = getattr(node, "public_ip", None) if node is not None else None
    if not node_target and node is not None:
        raw = getattr(node, "raw", {}) or {}
        node_target = raw.get("edgeTarget") or raw.get("publicIp") or raw.get("public_ip")
    if not node_target:
        return ""
    target = str(node_target).strip()
    if target in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return target


def _zone_candidates(domain: str) -> list[str]:
    labels = [part for part in domain.strip(".").split(".") if part]
    candidates: list[str] = []
    for index in range(0, max(0, len(labels) - 1)):
        candidate = ".".join(labels[index:])
        if "." in candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _local_node(profile_name: str, *, name: str | None = None, region: str | None = None):
    from .config import NodeConfig

    profile = PROFILES[profile_name]
    return NodeConfig(
        name=name or os.uname().nodename,
        host="localhost",
        region=region or profile.labels.get("region", "cn"),
        roles=list(profile.roles),
        raw={},
    )


def _local_node_for_region(region: str, *, name: str | None = None):
    from .config import NodeConfig

    roles = [region]
    return NodeConfig(
        name=name or os.uname().nodename,
        host="localhost",
        region=region,
        roles=roles,
        raw={},
    )


def _join_profile_for_region(region: str):
    from .profiles import Profile

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
        node_name = name or _local_luma_node_name(remote) or local_docker_node_name()
        result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).unregister_node(node_name=node_name)
        results.append(str(result.get("message") or f"Node unregistered: {node_name}"))
    results.append(_leave_local_swarm(remote))
    results.append(_remove_local_runtime_state(remote))
    if tailscale:
        results.append(_tailscale_logout(remote))
    if prune_docker:
        results.append(_prune_local_docker(remote))
    return results


def _local_luma_node_name(remote: LocalExecutor) -> str:
    result = remote.sudo_result(
        "if command -v docker >/dev/null 2>&1; then "
        "docker node inspect self --format '{{ index .Spec.Labels \"luma.node.name\" }}' 2>/dev/null || true; "
        "fi"
    )
    if result.code != 0:
        return ""
    return _last_nonempty_line(result.output)


def _leave_local_swarm(remote: LocalExecutor) -> str:
    result = remote.sudo_result(
        "set -euo pipefail; "
        "if command -v docker >/dev/null 2>&1; then "
        "state=$(docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || echo inactive); "
        "if [ \"$state\" = \"active\" ]; then docker swarm leave --force >/dev/null; echo left; else echo skipped; fi; "
        "else echo skipped; fi"
    )
    if result.code != 0:
        raise LumaError(f"failed to leave Docker Swarm:\n{result.output.strip()}")
    status = _last_nonempty_line(result.output)
    if status == "left":
        return "Swarm left"
    return "Swarm leave skipped"


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
        "if command -v docker >/dev/null 2>&1; then docker system prune -af --volumes >/dev/null; echo done; else echo skipped; fi"
    )
    if result.code != 0:
        raise LumaError(f"failed to prune Docker:\n{result.output.strip()}")
    if _last_nonempty_line(result.output) == "skipped":
        return "Docker prune skipped"
    return "Docker pruned"


def cmd_cloudflare(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.cloudflare_command == "connect":
        zone = find_zone(config, args.zone)
        providers = config.raw.setdefault("providers", {})
        dns = providers.setdefault("dns", {})
        dns["type"] = "cloudflare"
        dns["zone"] = args.zone
        dns["zoneId"] = zone["id"]
        dns.setdefault("apiTokenEnv", "CLOUDFLARE_API_TOKEN")
        save_config(config)
        print(f"Cloudflare connected: {args.zone} ({zone['id']})")
        return 0
    raise LumaError(f"unknown cloudflare command: {args.cloudflare_command}")


def cmd_egress(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ensure_interactive_config("manager", keys=["EGRESS_SUBSCRIPTION_URL"])
    node = _repair_node(config, args.node, "egress-gateway")
    executor = None if args.node else LocalExecutor()
    subscription_url = os.environ.get("EGRESS_SUBSCRIPTION_URL")
    if not subscription_url:
        raise LumaError("missing EGRESS_SUBSCRIPTION_URL")
    setup_egress(config, node, subscription_url, emit=log, executor=executor)
    if args.egress_command == "refresh":
        print("Egress refreshed")
    else:
        print("Egress setup complete")
    return 0


def cmd_portainer(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.portainer_command == "setup":
        node = _repair_node(config, args.node, "single-node")
        setup_portainer(node, emit=log, executor=None if args.node else LocalExecutor())
        return 0
    raise LumaError(f"unknown portainer command: {args.portainer_command}")


def cmd_tailscale(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.tailscale_command == "connect":
        ensure_interactive_config("worker", keys=["TAILSCALE_AUTHKEY"], required_keys=["TAILSCALE_AUTHKEY"])
        node = _repair_node(config, args.node, "single-node")
        log("[start] Install and connect Tailscale")
        for line in setup_tailscale(node, executor=None if args.node else LocalExecutor()):
            log(f"[ok] {line}")
        return 0
    raise LumaError(f"unknown tailscale command: {args.tailscale_command}")


def _repair_node(config: LumaConfig, node_name: str | None, profile_name: str):
    if node_name:
        return config.get_node(node_name)
    return config.default_manager() or _local_node(profile_name)


def _local_tailscale_connected() -> bool:
    result = LocalExecutor().run_result("command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1")
    if result.code != 0:
        return False
    return bool(_last_output_line(result.output))


def _last_output_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def prompt(default: str, label: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def cmd_service(args: argparse.Namespace) -> int:
    if args.service_command != "new":
        raise LumaError(f"unknown service command: {args.service_command}")
    config = load_config(args.config)
    name = prompt("app", "name")
    image_default = f"{config.defaults.get('registry', 'ghcr.io/your-org')}/{slugify(name)}:latest"
    image = prompt(image_default, "image")
    region = prompt("cn", f"region ({', '.join(sorted(VALID_REGIONS))})")
    exposure = prompt(str(config.defaults.get("exposure", "cn-edge")), f"exposure ({', '.join(sorted(VALID_EXPOSURES))})")
    domain = ""
    port = None
    if exposure != "none":
        domain = prompt(f"{slugify(name)}.{config.dns.get('zone', 'example.com')}", "domain")
        port = int(prompt("3000", "port"))
    replicas = int(prompt("1", "replicas"))
    data: Dict[str, Any] = {
        "name": name,
        "image": image,
        "region": region,
        "public": exposure != "none",
        "exposure": exposure,
        "replicas": replicas,
    }
    if domain:
        data["domain"] = domain
    if port is not None:
        data["port"] = port
    output = args.output or Path(f"{slugify(name)}.yaml")
    write_yaml(output, data)
    print(f"Service manifest created: {output}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    print(f"Service valid: {service.name} ({service.service_kind})")
    print(render_stack(config, service))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    print(render_stack(config, service))
    return 0


def cmd_dns_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    print(sync_dns(config, service))
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    rendered = render_stack(config, service)
    target = stack_path(config, service)
    rendered_route = render_tailscale_route(config, service) if service.exposure == "tailscale-relay" else None
    route_target = route_path(config, service) if rendered_route else None

    if args.dry_run:
        print(f"Dry run: would write {target}")
        print(rendered)
        if rendered_route and route_target:
            print(f"Dry run: would write {route_target}")
            print(rendered_route)
        return 0

    if args.commit or args.push:
        raise LumaError("--commit/--push are not supported for control-plane deploy; run deploy --dry-run for local rendering")
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")

    print(f"[start] Load deploy context: {args.service}", flush=True)
    context = load_current_context()
    print(f"[ok] Logged in: {context['clusterId']} ({context['endpoint']})", flush=True)
    client = ControlClient(
        str(context["endpoint"]),
        str(context["token"]),
        insecure=bool(context.get("insecure")),
        resolve_ip=str(context["resolveIp"]) if context.get("resolveIp") else None,
    )
    print(f"[start] Submit deploy: {service.name} -> {service.region}/{service.exposure}", flush=True)
    print(f"[start] Waiting for control plane response (timeout {args.timeout}s)", flush=True)
    result = client.deploy(
        manifest=args.service.read_text(encoding="utf-8"),
        source_name=str(args.service),
        skip_dns=args.skip_dns,
        skip_webhook=args.skip_webhook,
        timeout=args.timeout,
    )
    for step in result.get("steps") or []:
        if isinstance(step, dict):
            message = step.get("message")
            suffix = f": {message}" if message else ""
            print(f"[{step.get('status', 'ok')}] {step.get('name', 'step')}{suffix}")
    print(f"[ok] Deploy finished: {result.get('service', service.name)}")
    if result.get("image"):
        image = result["image"]
        if image.get("fallback"):
            print(f"Image fallback: {image.get('requested')} -> {image.get('selected')}")
        else:
            print(f"Image ready: {image.get('selected')}")
    if result.get("dns"):
        print(result["dns"])
    if result.get("webhook"):
        print(result["webhook"])
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Login context", False, "Run: luma login <control-url> --token <deploy-token>"))
    try:
        context = load_current_context()
        checks[-1] = ("Login context", True, str(context.get("endpoint") or "current context loaded"))
        client = ControlClient(
            str(context["endpoint"]),
            str(context["token"]),
            insecure=bool(context.get("insecure")),
            resolve_ip=str(context["resolveIp"]) if context.get("resolveIp") else None,
        )
        verified = client.verify_login()
        checks.append(("Control API", bool(verified.get("clusterId")), "Check the control URL, token, DNS, and HTTPS route"))
    except LumaError as exc:
        checks.append(("Control API", False, str(exc)))

    if not args.legacy_ssh:
        if args.deep:
            checks.append(("Deep node checks", False, "Run: luma doctor --legacy-ssh --deep from a machine that can SSH to the nodes"))
        for name, ok, fix in checks:
            print(f"{name}: {'ok' if ok else 'fail'}")
            if not ok:
                print(f"  Fix: {fix}")
        return 0 if all(ok for _, ok, _ in checks) else 1

    checks.append(("Config", bool(config.path and config.path.exists()), "Run: luma init or create luma.yaml on manager nodes"))
    checks.append(("Nodes", bool(config.nodes), "Add nodes to luma.yaml"))
    for node in config.nodes.values():
        remote = RemoteExecutor(node)
        ssh = remote.run_result("true")
        checks.append((f"{node.name}: SSH", ssh.code == 0, f"Check SSH host alias: {node.host}"))
        docker = remote.sudo_result("command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1")
        sudo_fix = "Export LUMA_SUDO_PASSWORD or configure passwordless sudo"
        docker_fix = sudo_fix if _looks_like_sudo_auth_failure(docker.output) else f"Run: luma node bootstrap {node.name} --profile single-node"
        checks.append((f"{node.name}: Docker", docker.code == 0, docker_fix))
        swarm = remote.sudo_result("docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null")
        swarm_fix = sudo_fix if _looks_like_sudo_auth_failure(swarm.output) else f"Run: luma node bootstrap {node.name} --profile cn-edge"
        swarm_state = _last_nonempty_line(swarm.output)
        checks.append((f"{node.name}: Swarm", swarm_state in {"active", "pending"}, swarm_fix))
        if docker.code == 0 and swarm_state == "active":
            services = remote.sudo_result("docker service ls --format '{{.Name}} {{.Replicas}}' 2>/dev/null")
            checks.append((f"{node.name}: Traefik", _service_ready(services.output, "traefik_traefik"), f"Run: luma node bootstrap {node.name} --profile single-node"))
            checks.append((f"{node.name}: Portainer", _service_ready(services.output, "portainer_portainer"), f"Run: luma portainer setup {node.name}"))
            if node.has_role("egress"):
                checks.append((f"{node.name}: Egress gateway", _service_ready(services.output, "egress_mihomo"), f"Run: luma egress setup {node.name}"))
                if args.deep:
                    pull = remote.sudo_result("timeout 60 docker pull hello-world:latest >/dev/null 2>&1")
                    checks.append((f"{node.name}: Docker pull through egress", pull.code == 0, f"Run: luma egress setup {node.name}"))
        tailscale = remote.sudo_result("command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1")
        checks.append((f"{node.name}: Tailscale", tailscale.code == 0, f"Export TAILSCALE_AUTHKEY and run: luma tailscale connect {node.name}"))
    for name, ok, fix in checks:
        print(f"{name}: {'ok' if ok else 'fail'}")
        if not ok:
            print(f"  Fix: {fix}")
    return 0 if all(ok for _, ok, _ in checks) else 1


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


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "depoly":
        argv[0] = "deploy"
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.no_env:
            load_env_file(args.env_file)
            load_user_config()
        if args.command == "init":
            return cmd_init(args)
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "configure":
            return cmd_configure(args)
        if args.command == "version":
            return cmd_version(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "login":
            return cmd_login(args)
        if args.command == "context":
            return cmd_context(args)
        if args.command == "secret":
            return cmd_secret(args)
        if args.command == "bootstrap":
            return cmd_bootstrap(args)
        if args.command == "update":
            return cmd_update(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "node":
            return cmd_node(args)
        if args.command == "cloudflare":
            return cmd_cloudflare(args)
        if args.command == "egress":
            return cmd_egress(args)
        if args.command == "portainer":
            return cmd_portainer(args)
        if args.command == "tailscale":
            return cmd_tailscale(args)
        if args.command == "service":
            return cmd_service(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "render":
            return cmd_render(args)
        if args.command == "dns-sync":
            return cmd_dns_sync(args)
        if args.command == "deploy":
            return cmd_deploy(args)
    except LumaError as exc:
        print(f"luma: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
