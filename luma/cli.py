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
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, TypeVar

from .bootstrap import _is_tailscale_manager_addr, bootstrap_manager_local, bootstrap_node, configure_dns, install_docker, install_nomad_node, local_host_name, refresh_manager_control_local, setup_egress, setup_tailscale
from .cloudflare import find_zone, sync_dns
from .compose import (
    DEFAULT_NFS_MOUNT_OPTIONS,
    compose_route_path,
    compose_stack_path,
    init_compose_sidecar,
    load_compose_deployment,
    render_compose_routes,
    storage_summary,
)
from .config import LumaConfig, load_config, save_config
from .control.client import ControlClient
from .control.context import list_contexts, load_current_context, save_context, use_context
from .control.state import load_state, new_state, state_path
from .agent import DEFAULT_AGENT_CONFIG, install_node_agent, run_node_agent, run_terminal_supervisor
from .envfile import load_env_file, parse_env_file
from .errors import LumaError
from .io import dump_yaml, write_yaml
from .local import LocalExecutor
from .profiles import PROFILES
from .render import render_tailscale_route, render_tcp_route, route_path, stack_path
from .service import VALID_EXPOSURES, VALID_REGIONS, load_service, slugify
from .storage import storage_check_plan, storage_migration_plan
from .userconfig import configured_keys, ensure_interactive_config, interactive_configure, load_user_config, masked_config_lines, user_config_path
from . import __version__


T = TypeVar("T")
OUTPUT_FORMATS = ("text", "json", "ndjson")
UPDATE_REEXEC_ENV = "LUMA_UPDATE_REEXECED"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma", description="Self-hosted deployment control plane.")
    parser.add_argument("--config", type=Path, default=None, help="Path to luma.yaml")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to local env file")
    parser.add_argument("--no-env", action="store_true", help="Do not load .env")
    visible_commands = (
        "init,version,status,preflight,configure,login,context,secret,registry,"
        "bootstrap,update,doctor,node,cloudflare,egress,tailscale,"
        "service,validate,render,dns-sync,deploy,rollback,history,compose,storage"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{" + visible_commands + "}")

    sub.add_parser("init")
    version = sub.add_parser("version")
    version.add_argument("--control-url", help="Control API URL to check instead of the current login context")
    version.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API check")
    version.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")
    version.add_argument("--local", action="store_true", help="Only print the local CLI version")
    status = sub.add_parser("status")
    status.add_argument("--control-url", help="Control API URL to check instead of the current login context")
    status.add_argument("--token", help="Management token to use with --control-url")
    status.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API check")
    status.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")
    _add_output_arguments(status)
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
    secret_list = secret_sub.add_parser("list")
    _add_control_arguments(secret_list)
    _add_output_arguments(secret_list)
    secret_set = secret_sub.add_parser("set")
    secret_set.add_argument("name")
    secret_set.add_argument("--scope", default="", help="Application/stack scope; omit only for legacy global secrets")
    secret_set.add_argument("--value")
    secret_set.add_argument("--value-stdin", action="store_true", help="Read the secret value from stdin")
    _add_control_arguments(secret_set)
    secret_import = secret_sub.add_parser("import", help="Import deployment secrets from a .env file into an application scope")
    secret_import.add_argument("env_file", type=Path)
    secret_import.add_argument("--scope", required=True, help="Application/stack scope used to isolate common names like DATABASE_URL")
    _add_control_arguments(secret_import)
    registry = sub.add_parser("registry")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_list = registry_sub.add_parser("list")
    _add_control_arguments(registry_list)
    _add_output_arguments(registry_list)
    registry_login = registry_sub.add_parser("login")
    registry_login.add_argument("host")
    registry_login.add_argument("--username", required=True)
    registry_login.add_argument("--password-stdin", action="store_true", help="Read the registry password/token from stdin")
    _add_control_arguments(registry_login)
    registry_remove = registry_sub.add_parser("remove")
    registry_remove.add_argument("host")
    _add_control_arguments(registry_remove)
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
            "Update the local CLI. With no target, Luma hot-refreshes manager control only "
            "when local manager state exists; "
            "clients and workers update CLI only."
        ),
        epilog="Examples: luma update | luma update --install-ref v0.1.119 | luma update manager --domain luma.example.com",
    )
    _add_update_manager_arguments(update)
    _add_control_arguments(update)
    update_sub = update.add_subparsers(dest="update_command", required=False, metavar="[target]")
    update_manager = update_sub.add_parser("manager", help="force a manager control-plane refresh")
    _add_update_manager_arguments(update_manager)
    _add_control_arguments(update_manager)
    update_fleet = update_sub.add_parser("fleet", help="update Luma on registered non-manager nodes with ready agents")
    update_fleet.add_argument("--install-ref", dest="fleet_install_ref", help="Git ref passed to the installer on every node")
    update_fleet.add_argument("--all", action="store_true", help="Include offline nodes in the report as skipped")
    update_fleet.add_argument("--include-manager", action="store_true", help="Also update manager nodes through fleet tasks")
    update_fleet.add_argument("--timeout", type=int, default=900, help="Per-node update timeout in seconds")
    _add_control_arguments(update_fleet)
    _add_output_arguments(update_fleet)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--deep", action="store_true", help="Run slower live checks")

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
    node_join.add_argument("--region", choices=sorted(VALID_REGIONS))
    node_join.add_argument("--name", default=os.uname().nodename)
    node_join.add_argument("--engine", choices=("nomad",), default="nomad", metavar="{nomad}", help="Orchestrator to join; Nomad is the only supported engine")
    node_join.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_join.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_exit = node_sub.add_parser("exit")
    node_exit.add_argument("--endpoint", help="Control endpoint; when set, unregister this node from Luma Control")
    node_exit.add_argument("--token", help="Management token or node join token used with --endpoint")
    node_exit.add_argument("--name", help="Luma node name to unregister; defaults to this node's registered label or Docker name")
    node_exit.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_exit.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_exit.add_argument("--tailscale", action="store_true", help="Also log out Tailscale on this node")
    node_exit.add_argument("--prune-docker", action="store_true", help="Also prune unused Docker containers, networks, images, and volumes")
    node_remove = node_sub.add_parser("remove")
    node_remove.add_argument("name")
    node_remove.add_argument("--control-url", help="Control API URL to use instead of the current login context")
    node_remove.add_argument("--token", help="Management token to use with --control-url")
    node_remove.add_argument("--insecure", action="store_true", help="Skip TLS verification for self-signed control endpoints")
    node_remove.add_argument("--resolve-ip", help="Connect to this IP while keeping the endpoint hostname as Host")
    node_status = node_sub.add_parser("status")
    node_status.add_argument("name", nargs="?", help="Optional Luma node name, display name, hostname, or alias to show")
    _add_control_arguments(node_status)
    _add_output_arguments(node_status)
    node_nomad_join = node_sub.add_parser("nomad-join", help="ask a ready node agent to install and join Nomad on that node")
    node_nomad_join.add_argument("name")
    node_nomad_join.add_argument("--region", choices=sorted(VALID_REGIONS), help="Override the node's registered region")
    node_nomad_join.add_argument("--server-addr", help="Nomad RPC address to join; defaults to the control-plane join address")
    node_nomad_join.add_argument("--timeout", type=int, default=1200, help="Join timeout in seconds")
    _add_control_arguments(node_nomad_join)
    _add_output_arguments(node_nomad_join)

    node_agent = sub.add_parser("node-agent", help=argparse.SUPPRESS)
    node_agent_sub = node_agent.add_subparsers(dest="node_agent_command", required=True)
    node_agent_run = node_agent_sub.add_parser("run", help=argparse.SUPPRESS)
    node_agent_run.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG)
    node_agent_run.add_argument("--once", action="store_true")
    node_agent_run.add_argument("--poll-interval", type=int)
    node_agent_terminal = node_agent_sub.add_parser("terminal-supervisor", help=argparse.SUPPRESS)
    node_agent_terminal.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG)
    sub._choices_actions = [action for action in sub._choices_actions if action.dest != "node-agent"]

    cf = sub.add_parser("cloudflare")
    cf_sub = cf.add_subparsers(dest="cloudflare_command", required=True)
    cf_connect = cf_sub.add_parser("connect")
    cf_connect.add_argument("--zone", required=True)

    egress = sub.add_parser("egress")
    egress_sub = egress.add_subparsers(dest="egress_command", required=True)
    for name in ("setup", "refresh"):
        egress_sub.add_parser(name)

    tailscale = sub.add_parser("tailscale")
    tailscale_sub = tailscale.add_subparsers(dest="tailscale_command", required=True)
    tailscale_sub.add_parser("connect")

    service = sub.add_parser("service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_new = service_sub.add_parser("new")
    service_new.add_argument("--output", type=Path)
    service_remove = service_sub.add_parser("remove")
    service_remove.add_argument("service", help="Deployed service or Compose application name")
    service_remove.add_argument("--skip-dns", action="store_true", help="Keep Cloudflare DNS records")
    service_remove.add_argument("--skip-orchestrator", action="store_true", help="Keep the Nomad job running")
    service_remove.add_argument("--delete-storage", action="store_true", help="Delete removable storage referenced by the recorded deployment")
    service_remove.add_argument("--dry-run", action="store_true", help="Show what would be removed without changing the manager")
    service_remove.add_argument("--timeout", type=int, default=300, help="Seconds to wait for the control-plane remove response")

    validate = sub.add_parser("validate")
    validate.add_argument("service", type=Path)
    validate.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to validate for; Nomad is the only supported engine")
    _add_output_arguments(validate)
    render = sub.add_parser("render")
    render.add_argument("service", type=Path)
    render.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to render for; Nomad is the only supported engine")

    dns = sub.add_parser("dns-sync")
    dns.add_argument("service", type=Path)

    deploy = sub.add_parser("deploy")
    deploy.add_argument("service", type=Path)
    _add_control_arguments(deploy)
    _add_output_arguments(deploy)
    deploy.add_argument("--dry-run", action="store_true")
    deploy.add_argument("--skip-dns", action="store_true")
    deploy.add_argument("--skip-orchestrator", action="store_true")
    deploy.add_argument("--env", dest="deploy_env_file", type=Path, help="Use this .env file as scoped deployment secrets for this service")
    deploy.add_argument("--secrets-env-file", dest="deploy_env_file", type=Path, help=argparse.SUPPRESS)
    deploy.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the control-plane deploy response")
    deploy.add_argument("--commit", action="store_true", help="Deprecated for control-plane deploy")
    deploy.add_argument("--push", action="store_true", help="Deprecated for control-plane deploy")

    rollback = sub.add_parser("rollback", help="Roll a Nomad-engine service back to a previous version")
    rollback.add_argument("name")
    rollback.add_argument("--to-version", type=int, default=None, help="Target version (default: previous)")
    _add_control_arguments(rollback)
    _add_output_arguments(rollback)

    history = sub.add_parser("history", help="Show a Nomad-engine service's deploy version history")
    history.add_argument("name")
    _add_control_arguments(history)
    _add_output_arguments(history)

    compose = sub.add_parser("compose")
    compose_sub = compose.add_subparsers(dest="compose_command", required=True)
    compose_init = compose_sub.add_parser("init")
    compose_init.add_argument("--compose", type=Path, default=Path("docker-compose.yml"))
    compose_init.add_argument("--output", type=Path, default=Path("luma.compose.yml"))
    compose_validate = compose_sub.add_parser("validate")
    compose_validate.add_argument("sidecar", type=Path)
    compose_validate.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to validate for; Nomad is the only supported engine")
    _add_control_arguments(compose_validate)
    _add_output_arguments(compose_validate)
    compose_render = compose_sub.add_parser("render")
    compose_render.add_argument("sidecar", type=Path)
    compose_render.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator to render for; Nomad is the only supported engine")
    _add_control_arguments(compose_render)
    compose_deploy = compose_sub.add_parser("deploy")
    compose_deploy.add_argument("sidecar", type=Path)
    compose_deploy.add_argument("--engine", choices=("nomad",), metavar="{nomad}", help="Orchestrator for local dry-run preview; live deploy follows the control-plane config")
    _add_control_arguments(compose_deploy)
    _add_output_arguments(compose_deploy)
    compose_deploy.add_argument("--dry-run", action="store_true")
    compose_deploy.add_argument("--skip-dns", action="store_true")
    compose_deploy.add_argument("--skip-orchestrator", action="store_true")
    compose_deploy.add_argument("--env", dest="deploy_env_file", type=Path, help="Use this .env file as scoped deployment secrets for this Compose application")
    compose_deploy.add_argument("--secrets-env-file", dest="deploy_env_file", type=Path, help=argparse.SUPPRESS)
    compose_deploy.add_argument("--timeout", type=int, default=1800)
    storage = sub.add_parser("storage")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_list = storage_sub.add_parser("list")
    _add_control_arguments(storage_list)
    _add_output_arguments(storage_list)
    storage_set = storage_sub.add_parser("set")
    storage_set.add_argument("name")
    storage_set.add_argument("--provider", choices=("nfs",), default="nfs")
    storage_set.add_argument("--external", action="store_true")
    storage_set.add_argument("--node", default="")
    storage_set.add_argument("--path", default="")
    storage_set.add_argument("--endpoint", default="")
    storage_set.add_argument(
        "--mount-options",
        default="",
        help=f"NFS mount options; defaults to {DEFAULT_NFS_MOUNT_OPTIONS}",
    )
    storage_set.add_argument("--region", action="append", dest="regions", default=[])
    storage_set.add_argument("--eligible-node", action="append", dest="nodes", default=[])
    _add_control_arguments(storage_set)
    storage_remove = storage_sub.add_parser("remove")
    storage_remove.add_argument("name")
    _add_control_arguments(storage_remove)
    storage_apply = storage_sub.add_parser("apply")
    storage_apply.add_argument("sidecar", type=Path)
    _add_control_arguments(storage_apply)
    storage_apply.add_argument("--dry-run", action="store_true")
    storage_apply.add_argument("--timeout", type=int, default=300)
    storage_check = storage_sub.add_parser("check")
    storage_check.add_argument("sidecar", type=Path)
    _add_control_arguments(storage_check)
    _add_output_arguments(storage_check)
    storage_migrate = storage_sub.add_parser("migrate")
    storage_migrate.add_argument("sidecar", type=Path)
    storage_migrate.add_argument("--volume", required=True)
    storage_migrate.add_argument("--from-node", required=True)
    storage_migrate.add_argument("--from-volume", required=True)
    _add_control_arguments(storage_migrate)
    _add_output_arguments(storage_migrate)

    return parser


def _add_control_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--control-url", help="Control API URL to use instead of the current login context")
    parser.add_argument("--token", help="Management token to use with --control-url")
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification for the control API")
    parser.add_argument("--resolve-ip", help="Connect to this IP while keeping the control hostname as Host")


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=OUTPUT_FORMATS, default="text", help="Output format")
    parser.add_argument("--quiet", action="store_true", help="Print only the final result or error")


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


def _output_format(args: argparse.Namespace) -> str:
    return str(getattr(args, "format", "text") or "text")


def _quiet(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "quiet", False))


def _context_warnings(args: argparse.Namespace) -> list[str]:
    warnings = getattr(args, "_luma_context_warnings", None)
    if not isinstance(warnings, list):
        warnings = []
        setattr(args, "_luma_context_warnings", warnings)
    return warnings


def _add_context_warning(args: argparse.Namespace, message: str) -> None:
    warnings = _context_warnings(args)
    if message not in warnings:
        warnings.append(message)


def _validation_context(args: argparse.Namespace) -> Dict[str, Any]:
    warnings = _context_warnings(args)
    context_used = bool(getattr(args, "_luma_context_used", False))
    return {
        "validationMode": "degraded" if warnings else ("cluster-aware" if context_used else "local"),
        "warnings": list(warnings),
    }


def _command_name(args: argparse.Namespace) -> str:
    command = str(getattr(args, "command", ""))
    if command == "secret":
        return f"secret {getattr(args, 'secret_command', '')}".strip()
    if command == "registry":
        return f"registry {getattr(args, 'registry_command', '')}".strip()
    return command


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


def _print_json(payload: Dict[str, Any], *, file: Any = None) -> None:
    print(_json_dumps(payload), file=file)


def _success_payload(args: argparse.Namespace, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "command": _command_name(args), "result": result}


def _error_payload(exc: LumaError, *, code: str = "luma_error") -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": str(exc)}}


def _print_success(args: argparse.Namespace, result: Dict[str, Any]) -> None:
    output_format = _output_format(args)
    if output_format == "json":
        _print_json(_success_payload(args, result))
    elif output_format == "ndjson":
        _print_json({"type": "result", "ok": True, "result": result})


def _print_structured_error(args: argparse.Namespace, exc: LumaError) -> bool:
    output_format = _output_format(args)
    if output_format == "json":
        _print_json(_error_payload(exc), file=sys.stderr)
        return True
    if output_format == "ndjson":
        _print_json({"type": "error", **_error_payload(exc)}, file=sys.stderr)
        return True
    return False


def _env_text(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _arg_text(args: argparse.Namespace, name: str) -> str | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise LumaError(f"{name} must be true or false")


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


def _control_context(args: argparse.Namespace, *, require_token: bool) -> tuple[str, str, bool, str | None]:
    control_url = _arg_text(args, "control_url") or _env_text("LUMA_CONTROL_URL")
    token = _arg_text(args, "token") or _env_text("LUMA_DEPLOY_TOKEN")
    resolve_ip = _arg_text(args, "resolve_ip") or _env_text("LUMA_RESOLVE_IP")
    env_insecure = _env_bool("LUMA_INSECURE")
    cli_insecure = bool(getattr(args, "insecure", False))
    insecure: bool | None = True if cli_insecure else env_insecure

    has_stateless_context = any(
        value is not None
        for value in (control_url, token, resolve_ip, env_insecure)
    ) or cli_insecure

    if not has_stateless_context:
        context = load_current_context()
        return (
            str(context["endpoint"]),
            str(context["token"]),
            bool(context.get("insecure", False)),
            str(context["resolveIp"]) if context.get("resolveIp") else None,
        )

    context: Dict[str, Any] = {}
    if not control_url or (require_token and not token) or insecure is None:
        try:
            context = load_current_context()
        except LumaError:
            context = {}

    if not control_url:
        control_url = str(context["endpoint"]) if context.get("endpoint") else None
    if not token:
        token = str(context["token"]) if context.get("token") else None
    if insecure is None:
        insecure = bool(context.get("insecure", False))
    if not resolve_ip and context.get("resolveIp"):
        resolve_ip = str(context["resolveIp"])

    if not control_url:
        raise LumaError("control URL is required; pass --control-url, set LUMA_CONTROL_URL, or run luma login")
    if require_token and not token:
        raise LumaError("management token is required; pass --token, set LUMA_DEPLOY_TOKEN, or run luma login")
    if not token:
        token = "health"
    return (
        control_url,
        token,
        bool(insecure),
        resolve_ip,
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


def _host_from_hostport(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    if text.count(":") > 1:
        return text
    return text


def _find_nomad_cli() -> str:
    candidates = [
        shutil.which("nomad"),
        "/usr/local/bin/nomad",
        "/opt/homebrew/bin/nomad",
        "/usr/bin/nomad",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def local_nomad_node_info() -> tuple[str, str]:
    nomad = _find_nomad_cli()
    if not nomad:
        raise LumaError("Nomad CLI not found after install")
    result = subprocess.run(
        [nomad, "node", "status", "-self", "-json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LumaError(f"Nomad local node is not ready: {detail or 'nomad node status failed'}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise LumaError("Nomad local node status returned invalid JSON") from exc
    node_id = str(data.get("ID") or "").strip()
    meta = data.get("Meta") if isinstance(data.get("Meta"), dict) else {}
    node_name = str(meta.get("luma_node_name") or data.get("Name") or "").strip()
    if not node_id:
        raise LumaError("Nomad local node status did not include a node ID")
    return node_name or os.uname().nodename, node_id


def log(message: str) -> None:
    print(message, flush=True)


def _run_with_wait_heartbeat(action: Callable[[], T], *, timeout: int, interval: int = 30, emit: bool = True) -> T:
    if not emit:
        return action()
    done = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not done.wait(interval):
            elapsed = int(time.monotonic() - started)
            print(f"[wait] Control plane still working ({elapsed}s elapsed, timeout {timeout}s)", flush=True)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        return action()
    finally:
        done.set()
        thread.join(timeout=0.2)


def _print_deploy_step(step: Dict[str, Any]) -> None:
    status = str(step.get("status") or "ok")
    name = str(step.get("name") or "step")
    message = step.get("message")
    suffix = f": {message}" if message else ""
    print(f"[{status}] {name}{suffix}", flush=True)


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
        result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).join_nomad_node(
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
            raise LumaError("node join requires --region (cn, global, or home)")
        required_worker_keys = ["TAILSCALE_AUTHKEY"] if args.region == "home" and not _local_tailscale_connected() else []
        ensure_interactive_config("worker", required_keys=required_worker_keys)
        log("[start] Configure system DNS")
        log(f"[ok] {configure_dns(LocalExecutor())}")
        log("[start] Install Docker")
        log(f"[ok] {install_docker(LocalExecutor())}")
        client = ControlClient(args.endpoint, args.token, insecure=args.insecure, resolve_ip=args.resolve_ip)
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
            # CN-side and home nodes may need the manager egress proxy for
            # HashiCorp/GitHub downloads; global nodes download directly.
            egress_proxy = f"http://{server_host}:7890" if args.region in {"cn", "home"} else None
            install_nomad_node(
                node,
                role="client",
                region=args.region,
                node_name=registered_node_name,
                server_addrs=[nomad_rpc_addr],
                egress_proxy=egress_proxy,
                emit=log,
                install_docker_first=False,
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
            print("[skip] Luma node agent not installed: manager control API did not return node agent credentials; update the manager first")
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
    if args.node_command == "status":
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        payload = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).status()
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


def _install_node_agent_from_token(
    *,
    endpoint: str,
    agent_token: str,
    node_name: str,
    node_id: str = "",
    insecure: bool = False,
    resolve_ip: str | None = None,
) -> None:
    install_node_agent(
        endpoint=endpoint,
        token=agent_token,
        node_name=node_name,
        node_id=node_id,
        insecure=insecure,
        resolve_ip=resolve_ip,
    )


def _format_epoch(value: int) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def cmd_context(args: argparse.Namespace) -> int:
    if args.context_command == "list":
        contexts = list_contexts()
        if not contexts:
            print("No contexts. Run: luma login <control-url> --token <management-token>")
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
    if args.secret_command in {"list", "set", "import"}:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if args.secret_command == "list":
        result = client.list_secrets()
        keys = result.get("secrets") if isinstance(result.get("secrets"), list) else []
        if _output_format(args) != "text":
            _print_success(args, {"secrets": keys})
            return 0
        if not keys:
            print("No deployment secrets configured")
            return 0
        for key in keys:
            print(str(key))
        return 0
    if args.secret_command == "set":
        if args.value is not None and args.value_stdin:
            raise LumaError("--value and --value-stdin cannot be used together")
        if args.value_stdin:
            value = sys.stdin.read()
            if value.endswith("\n"):
                value = value[:-1]
        else:
            value = args.value
        if value is None:
            value = getpass.getpass(f"{args.name}: ")
        if args.scope:
            result = client.set_secret(name=args.name, value=value, scope=str(args.scope))
        else:
            result = client.set_secret(name=args.name, value=value)
        scope = result.get("scope")
        label = f"{scope}/{result.get('name', args.name)}" if scope else result.get("name", args.name)
        print(f"Secret saved: {label}")
        return 0
    if args.secret_command == "import":
        values = parse_env_file(args.env_file)
        if not values:
            print(f"No secrets found in {args.env_file}")
            return 0
        for key, value in sorted(values.items()):
            client.set_secret(name=key, value=value, scope=str(args.scope))
        print(f"Secrets imported: {len(values)} into scope {args.scope}")
        return 0
    raise LumaError(f"unknown secret command: {args.secret_command}")


def cmd_registry(args: argparse.Namespace) -> int:
    if args.registry_command in {"list", "login", "remove"}:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if args.registry_command == "list":
        result = client.list_registries()
        items = result.get("registries") if isinstance(result.get("registries"), list) else []
        if _output_format(args) != "text":
            _print_success(args, {"registries": items})
            return 0
        if not items:
            print("No registry credentials configured")
            return 0
        for item in items:
            host = str(item.get("host") or item.get("serverAddress") or "")
            username = str(item.get("username") or "")
            print(f"{host}\t{username}")
        return 0
    if args.registry_command == "login":
        if args.password_stdin:
            password = sys.stdin.read().strip()
        else:
            password = getpass.getpass(f"{args.host} password/token: ")
        result = client.set_registry(host=args.host, username=args.username, password=password)
        print(f"Registry credential saved: {result.get('host', args.host)}")
        return 0
    if args.registry_command == "remove":
        result = client.remove_registry(host=args.host)
        status = "removed" if result.get("removed") else "not configured"
        print(f"Registry credential {status}: {result.get('host', args.host)}")
        return 0
    raise LumaError(f"unknown registry command: {args.registry_command}")


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
        print(f"Cluster: {state['clusterId']}")
        print(f"Management token: {state['deployToken']}")
        print(f"Node join token: {state['joinToken']}")
        print("Join additional nodes:")
        for label, command in _node_join_examples(control_url, str(state["joinToken"])):
            print(f"  {label}: {command}")
        return 0
    raise LumaError(f"unknown bootstrap command: {args.bootstrap_command}")


def cmd_update(args: argparse.Namespace) -> int:
    if args.update_command not in {None, "manager", "fleet"}:
        raise LumaError(f"unknown update command: {args.update_command}")
    if os.environ.get(UPDATE_REEXEC_ENV) == "1":
        print("[skip] Luma CLI already updated in this run")
    else:
        print("[start] Update Luma CLI")
        _run_luma_installer(install_ref=_effective_update_install_ref(args))
        print("[ok] Luma CLI updated")
        _reexec_after_luma_update()
    if args.update_command == "fleet":
        return _cmd_update_fleet(args)
    if args.update_command == "manager":
        print("[info] Role: manager")
        print("[info] Manager control-plane refresh forced")
        print("[start] Refresh manager control plane")
        _refresh_manager_control(args)
        print("[ok] Manager control plane refreshed")
        _try_refresh_manager_agent(args)
        print("[ok] Manager update complete")
        return 0

    should_refresh, reason = _manager_refresh_decision(args)
    if should_refresh:
        print("[info] Role: manager")
        print(f"[info] Manager control-plane refresh required: {reason}")
        print("[start] Refresh manager control plane")
        _refresh_manager_control(args)
        print("[ok] Manager control plane refreshed")
        _try_refresh_manager_agent(args)
        print("[ok] Manager update complete")
        return 0

    if _local_agent_config() or _safe_local_nomad_node_id():
        print("[info] Role: joined node")
        _refresh_joined_node_agent(args)
        print("[ok] Joined node update complete")
        return 0

    print("[info] Role: client")
    print(f"[skip] Manager control-plane refresh skipped: {reason}")
    print("[skip] Node agent refresh skipped: no local joined-node metadata found")
    return 0


def _effective_update_install_ref(args: argparse.Namespace) -> str | None:
    return getattr(args, "fleet_install_ref", None) or getattr(args, "install_ref", None)


def _cmd_update_fleet(args: argparse.Namespace) -> int:
    should_refresh, reason = _manager_refresh_decision(args)
    if should_refresh:
        print("[info] Role: manager")
        print(f"[info] Manager control-plane refresh required: {reason}")
        print("[start] Refresh manager control plane")
        _refresh_manager_control(args)
        print("[ok] Manager control plane refreshed")
        _try_refresh_manager_agent(args)
    else:
        print(f"[skip] Local manager control-plane refresh skipped: {reason}")
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    print("[start] Update Luma on registered non-manager nodes")
    result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).update_fleet(
        install_ref=str(_effective_update_install_ref(args) or ""),
        include_all=bool(getattr(args, "all", False)),
        include_manager=bool(getattr(args, "include_manager", False)),
        timeout=int(getattr(args, "timeout", 900) or 900),
    )
    if _output_format(args) != "text":
        _print_success(args, result)
        return 1 if int(result.get("failed") or 0) else 0
    rows = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                str(item.get("nodeName") or ""),
                str(item.get("region") or "-"),
                str(item.get("os") or "-"),
                str(item.get("status") or "-"),
                str(item.get("message") or "-"),
            ]
        )
    if rows:
        _print_table(["node", "region", "os", "status", "message"], rows)
    else:
        print("No ready node agents found")
    print(
        f"[ok] Fleet update finished: {int(result.get('succeeded') or 0)} succeeded, "
        f"{int(result.get('failed') or 0)} failed, {int(result.get('skipped') or 0)} skipped"
    )
    return 1 if int(result.get("failed") or 0) else 0


def _try_refresh_manager_agent(args: argparse.Namespace) -> None:
    state = _existing_control_state()
    if not state:
        print("[skip] Manager node agent skipped: local manager control state not found")
        return
    domain = str(state.get("domain") or "").strip()
    token = str(state.get("joinToken") or state.get("deployToken") or "").strip()
    if not domain or not token:
        print("[skip] Manager node agent skipped: control domain or token is missing")
        return
    try:
        endpoint = _control_url(domain, args.https_port or _config_https_port(load_config(args.config)))
        _refresh_local_node_agent(endpoint=endpoint, token=token, insecure=bool(getattr(args, "insecure", False)), resolve_ip=getattr(args, "resolve_ip", None), allow_skip=True)
    except LumaError as exc:
        print(f"[skip] Manager node agent skipped: {exc}")


def _refresh_joined_node_agent(args: argparse.Namespace) -> None:
    if getattr(args, "control_url", None) or getattr(args, "token", None):
        try:
            endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        except LumaError as exc:
            print(f"[skip] Luma node agent skipped: joined node control context is unavailable ({exc})")
            return
        try:
            _refresh_local_node_agent(endpoint=endpoint, token=token, insecure=insecure, resolve_ip=resolve_ip, allow_skip=False)
        except LumaError as exc:
            if _node_agent_credentials_unsupported(exc) or _node_agent_credentials_unregistered(exc):
                print(f"[skip] Luma node agent skipped: {exc}")
                return
            raise
        return

    config = _local_agent_config()
    if config:
        endpoint = str(config.get("endpoint") or "")
        token = str(config.get("token") or "")
        node_name = str(config.get("nodeName") or "")
        node_id = str(config.get("nodeId") or "")
        if endpoint and token and node_name:
            print("[start] Refresh Luma node agent from local metadata")
            _install_node_agent_from_token(
                endpoint=endpoint,
                agent_token=token,
                node_name=node_name,
                node_id=node_id,
                insecure=bool(config.get("insecure")),
                resolve_ip=str(config.get("resolveIp") or "") or None,
            )
            print("[ok] Luma node agent refreshed")
            return
    try:
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    except LumaError as exc:
        print(f"[skip] Luma node agent skipped: joined node control context is unavailable ({exc})")
        return
    try:
        _refresh_local_node_agent(endpoint=endpoint, token=token, insecure=insecure, resolve_ip=resolve_ip, allow_skip=False)
    except LumaError as exc:
        if _node_agent_credentials_unsupported(exc) or _node_agent_credentials_unregistered(exc):
            print(f"[skip] Luma node agent skipped: {exc}")
            return
        raise


def _refresh_local_node_agent(
    *,
    endpoint: str,
    token: str,
    insecure: bool,
    resolve_ip: str | None,
    allow_skip: bool,
) -> None:
    local_name, local_id = _safe_local_nomad_node_info()
    if not local_id:
        message = "local Nomad node id is unavailable"
        if allow_skip:
            raise LumaError(message)
        raise LumaError(message + "; run this command on a joined node")
    client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    print("[start] Request node agent credentials")
    issued = client.issue_agent_token(node_name=local_name, node_id=local_id)
    node_name = str(issued.get("nodeName") or "")
    if not node_name:
        raise LumaError(f"this Nomad node is not registered in Luma Control: hostname={local_name}, nodeId={local_id}")
    agent_token = str(issued.get("agentToken") or "")
    if not agent_token:
        raise LumaError("control API did not return node agent credentials")
    print("[start] Install Luma node agent")
    _install_node_agent_from_token(
        endpoint=endpoint,
        agent_token=agent_token,
        node_name=node_name,
        node_id=local_id,
        insecure=insecure,
        resolve_ip=resolve_ip,
    )
    print("[ok] Luma node agent installed")


def _node_agent_credentials_unsupported(exc: LumaError) -> bool:
    message = str(exc)
    return "does not support node-agent credentials" in message or (
        "control API error 404" in message and "not found" in message
    )


def _node_agent_credentials_unregistered(exc: LumaError) -> bool:
    return "nodeName or nodeId must match a registered node" in str(exc)


def _local_agent_config() -> Dict[str, Any] | None:
    path = DEFAULT_AGENT_CONFIG
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        pass
    result = LocalExecutor().sudo_result(f"test -f {shlex.quote(str(path))} && cat {shlex.quote(str(path))}")
    if result.code != 0 or not result.output.strip():
        return None
    try:
        raw = result.output.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        data = json.loads(raw[start : end + 1] if start >= 0 and end >= start else raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _safe_local_nomad_node_info() -> tuple[str, str]:
    try:
        return local_nomad_node_info()
    except LumaError:
        return "", ""


def _safe_local_nomad_node_id() -> str:
    return _safe_local_nomad_node_info()[1]


def _manager_refresh_decision(args: argparse.Namespace) -> tuple[bool, str]:
    if _manager_update_options_provided(args):
        return True, "manager update options were provided"
    state = _existing_control_state()
    if not state:
        return False, "no local manager control state found"
    domain = str(state.get("domain") or "").strip()
    if not domain:
        return False, "local manager control state has no domain; run luma update manager --domain <control-domain>"
    return True, "local manager control state found"


def _manager_update_options_provided(args: argparse.Namespace) -> bool:
    if args.domain or args.node or args.http_port is not None or args.https_port is not None:
        return True
    if args.skip_egress or args.overwrite_control_state:
        return True
    if getattr(args, "profile", "single-node") != "single-node":
        return True
    return False


def _run_luma_installer(*, install_ref: str | None = None) -> None:
    env = os.environ.copy()
    if install_ref:
        env["LUMA_INSTALL_REF"] = install_ref
    command = "curl -fsSL https://raw.githubusercontent.com/LiuTianjie/luma/main/scripts/install-luma.sh | sh"
    subprocess.run(command, shell=True, check=True, env=env)


def _reexec_after_luma_update() -> None:
    command = _current_luma_command()
    if not command:
        print("[warn] Unable to re-exec updated Luma CLI; continuing in current process")
        return
    env = os.environ.copy()
    env[UPDATE_REEXEC_ENV] = "1"
    os.execvpe(command[0], [*command, *sys.argv[1:]], env)


def _current_luma_command() -> list[str]:
    candidate = str(sys.argv[0] or "").strip()
    if candidate and (Path(candidate).is_absolute() or "/" in candidate):
        path = Path(candidate)
        if path.suffix == ".py" and not os.access(path, os.X_OK):
            return [sys.executable, "-m", "luma.cli"]
        return [candidate]
    found = shutil.which("luma") or candidate
    return [found] if found else []


def _refresh_manager_control(args: argparse.Namespace) -> None:
    _reject_bootstrap_only_update_options(args)
    domain = _manager_update_domain(args.domain)
    state = _existing_control_state()
    if not state:
        raise LumaError("manager control state not found. Run luma bootstrap manager --domain <control-domain> for first install or repair.")
    state["domain"] = domain
    config = load_config(args.config)
    node = config.get_node(args.node) if args.node else (config.default_manager() or _local_node(args.profile))
    if not node:
        raise LumaError("no manager node configured. Add a node or pass --node.")
    _ensure_cloudflare_dns_from_local_config(config, domain, node)
    _attach_control_secrets(state, config)
    refresh_manager_control_local(config, node, domain, state, emit=log)


def _reject_bootstrap_only_update_options(args: argparse.Namespace) -> None:
    if args.http_port is not None or args.https_port is not None:
        raise LumaError(
            "luma update manager refreshes existing ingress from control state; "
            "HTTP/HTTPS port overrides are bootstrap-only. Use luma bootstrap manager "
            "--domain <control-domain> for explicit ingress repair."
        )
    if args.skip_egress:
        raise LumaError("luma update no longer runs egress setup. Use luma bootstrap manager --skip-egress only during full bootstrap repair.")
    if args.overwrite_control_state:
        raise LumaError("luma update preserves control state. Use luma bootstrap manager --overwrite-control-state only for explicit repair.")


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
        node_name = name or _local_luma_node_name(remote) or local_host_name(remote)
        result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).unregister_node(node_name=node_name)
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
    node = _local_node("egress-gateway")
    subscription_url = os.environ.get("EGRESS_SUBSCRIPTION_URL")
    if not subscription_url:
        raise LumaError("missing EGRESS_SUBSCRIPTION_URL")
    setup_egress(config, node, subscription_url, emit=log, executor=LocalExecutor())
    if args.egress_command == "refresh":
        print("Egress refreshed")
    else:
        print("Egress setup complete")
    return 0


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


def prompt(default: str, label: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def cmd_service(args: argparse.Namespace) -> int:
    if args.service_command == "new":
        return cmd_service_new(args)
    if args.service_command == "remove":
        return cmd_service_remove(args)
    raise LumaError(f"unknown service command: {args.service_command}")


def cmd_service_new(args: argparse.Namespace) -> int:
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


def cmd_service_remove(args: argparse.Namespace) -> int:
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")
    service_name = str(args.service).strip()
    if not service_name:
        raise LumaError("service name is required")
    if service_name.endswith((".yaml", ".yml")) or any(part in service_name for part in ("/", "\\")):
        raise LumaError("service remove expects a deployed service name, not a manifest path")
    print(f"[start] Load remove context: {service_name}", flush=True)
    context = load_current_context()
    print(f"[ok] Logged in: {context['clusterId']} ({context['endpoint']})", flush=True)
    client = ControlClient(
        str(context["endpoint"]),
        str(context["token"]),
        insecure=bool(context.get("insecure")),
        resolve_ip=str(context["resolveIp"]) if context.get("resolveIp") else None,
    )
    print(f"[start] Submit remove: {service_name}", flush=True)
    result = _run_with_wait_heartbeat(
        lambda: client.remove_service(
            name=service_name,
            skip_dns=args.skip_dns,
            skip_orchestrator=args.skip_orchestrator,
            delete_storage=args.delete_storage,
            dry_run=args.dry_run,
            timeout=args.timeout,
        ),
        timeout=args.timeout,
    )
    for step in result.get("steps") or []:
        if isinstance(step, dict):
            _print_deploy_step(step)
    action = "Remove dry run finished" if result.get("dryRun") else "Remove finished"
    print(f"[ok] {action}: {result.get('service') or result.get('deployment') or service_name}")
    if result.get("dns"):
        print(result["dns"])
    orchestrator_message = result.get("orchestrator")
    if orchestrator_message:
        print(orchestrator_message)
    if result.get("generatedFiles"):
        print(result["generatedFiles"])
    if result.get("storageCleanup"):
        print(result["storageCleanup"])
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    _require_nomad_engine(_service_engine(config, service, args))
    from .nomad_render import render_nomad_job

    rendered = render_nomad_job(config, service, resolve_secrets=False)
    target = stack_path(config, service)
    rendered_route = None
    if service.exposure == "tailscale-relay":
        rendered_route = render_tailscale_route(config, service)
    elif service.exposure == "tcp-relay":
        rendered_route = render_tcp_route(config, service)
    route_target = route_path(config, service) if rendered_route else None
    if _output_format(args) != "text":
        _print_success(args, _render_result(service, target, rendered, route_target, rendered_route, artifact_kind="job"))
        return 0
    if not _quiet(args):
        print(f"Service valid: {service.name} ({service.service_kind})")
        print(rendered)
    else:
        print(f"Service valid: {service.name}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    _require_nomad_engine(_service_engine(config, service, args))
    from .nomad_render import render_nomad_job

    print(render_nomad_job(config, service, resolve_secrets=False))
    return 0


def cmd_dns_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    print(sync_dns(config, service))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    result = client.rollback_service(name=args.name, version=args.to_version)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(result.get("message") or f"Rolled back {args.name}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    result = client.service_history(name=args.name)
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    versions = result.get("versions") or []
    if not versions:
        print(f"No version history for {args.name}")
        return 0
    rows = [
        [
            str(v.get("version")),
            "stable" if v.get("stable") else "-",
            str(v.get("image") or "-"),
        ]
        for v in versions
    ]
    _print_table(["version", "stable", "image"], rows)
    return 0


def _service_summary(service: Any) -> Dict[str, Any]:
    return {
        "source": str(service.source),
        "name": service.name,
        "slug": service.slug,
        "image": service.image,
        "region": service.region,
        "node": service.node,
        "exposure": service.exposure,
        "serviceKind": service.service_kind,
        "public": service.public,
        "domain": service.domain,
        "port": service.port,
        "replicas": service.replicas,
    }


def _render_result(
    service: Any,
    target: Path,
    rendered: str,
    route_target: Path | None = None,
    rendered_route: str | None = None,
    *,
    artifact_kind: str = "stack",
) -> Dict[str, Any]:
    artifacts: list[Dict[str, Any]] = [{"kind": artifact_kind, "path": str(target), "content": rendered}]
    if route_target and rendered_route:
        artifacts.append({"kind": "route", "path": str(route_target), "content": rendered_route})
    return {"service": _service_summary(service), "artifacts": artifacts}


def _service_storage_context_for_local(args: argparse.Namespace, service: Any) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    if not getattr(service, "storage", None):
        return None, None
    storage_classes = _control_storage_classes_for_local(args, required=True)
    node_records = _control_node_records_for_local(args, required=True)
    return storage_classes, node_records


def _deploy_env_secrets(path: Path | None, texts: list[str]) -> Dict[str, str] | None:
    if not path:
        return None
    if not path.exists():
        raise LumaError(f"deployment env file not found: {path}")
    values = parse_env_file(path)
    referenced: set[str] = set()
    for text in texts:
        referenced.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text))
    return {key: value for key, value in values.items() if key in referenced}


def cmd_deploy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    # Inherit cluster engine when the manifest omits it, mirroring the control
    # plane, so `--dry-run` previews the SAME artifact that will be deployed.
    effective_engine = _require_nomad_engine(_service_engine(config, service, args))
    output_format = _output_format(args)
    quiet = _quiet(args) or output_format != "text"

    if args.dry_run:
        rendered_route = None
        from .nomad_render import render_nomad_job

        rendered = render_nomad_job(config, service, resolve_secrets=False)
        target = stack_path(config, service)
        if service.exposure == "tailscale-relay":
            rendered_route = render_tailscale_route(config, service)
        elif service.exposure == "tcp-relay":
            rendered_route = render_tcp_route(config, service)
        route_target = route_path(config, service) if rendered_route else None
        result = _render_result(service, target, rendered, route_target, rendered_route, artifact_kind="job")
        result["dryRun"] = True
        result.update(_validation_context(args))
        if output_format != "text":
            _print_success(args, result)
            return 0
        if quiet:
            print(f"Dry run: {service.name}")
            return 0
        print(f"Dry run: would write {target}")
        for warning in _context_warnings(args):
            print(f"[warn] {warning}")
        print(rendered)
        if rendered_route and route_target:
            print(f"Dry run: would write {route_target}")
            print(rendered_route)
        return 0

    if args.commit or args.push:
        raise LumaError("--commit/--push are not supported for control-plane deploy; run deploy --dry-run for local rendering")
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")

    if not quiet:
        print(f"[start] Load deploy context: {args.service}", flush=True)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    if not quiet:
        print(f"[ok] Control endpoint: {endpoint}", flush=True)
    client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    if not quiet:
        print(f"[start] Submit deploy: {service.name} -> {service.region}/{service.exposure}", flush=True)
    manifest_text = args.service.read_text(encoding="utf-8")
    env_secrets = _deploy_env_secrets(args.deploy_env_file, [manifest_text])
    streamed = False
    result: Dict[str, Any] | None = None
    try:
        for event in client.deploy_events(
            manifest=manifest_text,
            source_name=str(args.service),
            skip_dns=args.skip_dns,
            skip_orchestrator=args.skip_orchestrator,
            env_secrets=env_secrets,
            timeout=args.timeout,
        ):
            status = str(event.get("status") or "")
            if output_format == "ndjson":
                _print_json({"type": "event", **event})
            if status in {"start", "ok", "fail"}:
                if not quiet:
                    _print_deploy_step(event)
                if status == "fail":
                    raise LumaError(str(event.get("message") or "deploy failed"))
            elif status == "done":
                payload = event.get("result")
                if not isinstance(payload, dict):
                    raise LumaError("control API stream ended without a deploy result")
                result = payload
            streamed = True
    except LumaError as exc:
        if "control API error 404" not in str(exc):
            raise

    if result is None:
        if streamed:
            raise LumaError("control API stream ended without a deploy result")
        if not quiet:
            print(f"[start] Waiting for control plane response (timeout {args.timeout}s)", flush=True)
        result = _run_with_wait_heartbeat(
            lambda: client.deploy(
                manifest=manifest_text,
                source_name=str(args.service),
                skip_dns=args.skip_dns,
                skip_orchestrator=args.skip_orchestrator,
                env_secrets=env_secrets,
                timeout=args.timeout,
            ),
            timeout=args.timeout,
            emit=not quiet,
        )
        for step in result.get("steps") or []:
            if isinstance(step, dict):
                if output_format == "ndjson":
                    _print_json({"type": "event", **step})
                elif not quiet:
                    _print_deploy_step(step)
    if output_format != "text":
        _print_success(args, result)
        return 0
    print(f"[ok] Deploy finished: {result.get('service', service.name)}")
    if result.get("image"):
        image = result["image"]
        if image.get("fallback"):
            print(f"Image fallback: {image.get('requested')} -> {image.get('selected')}")
        else:
            print(f"Image ready: {image.get('selected')}")
    if result.get("dns"):
        print(result["dns"])
    orchestrator_message = result.get("orchestrator")
    if orchestrator_message:
        print(orchestrator_message)
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    if args.compose_command == "init":
        init_compose_sidecar(args.compose, args.output)
        print(f"Compose sidecar created: {args.output}")
        return 0
    if args.compose_command == "validate":
        return cmd_compose_validate(args)
    if args.compose_command == "render":
        return cmd_compose_render(args)
    if args.compose_command == "deploy":
        return cmd_compose_deploy(args)
    raise LumaError(f"unknown compose command: {args.compose_command}")


def cmd_compose_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    deployment = load_compose_deployment(args.sidecar, storage_classes=_control_storage_classes_for_local(args))
    node_records = _control_node_records_for_local(args)
    _require_nomad_engine(_compose_engine(config, args))
    from .nomad_render import render_compose_job

    stack = render_compose_job(config, deployment, resolve_secrets=False)
    routes = render_compose_routes(config, deployment)
    result = {
        "deployment": _compose_summary(config, deployment, stack, routes, artifact_kind="job"),
        "storage": storage_summary(deployment, node_records=node_records),
        **_validation_context(args),
    }
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    print(f"Compose deployment valid: {deployment.name}")
    for warning in _context_warnings(args):
        print(f"[warn] {warning}")
    for warning in deployment.warnings:
        print(f"[warn] {warning}")
    return 0


def cmd_compose_render(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    deployment = load_compose_deployment(args.sidecar, storage_classes=_control_storage_classes_for_local(args))
    _require_nomad_engine(_compose_engine(config, args))
    from .nomad_render import render_compose_job

    rendered = render_compose_job(config, deployment, resolve_secrets=False)
    for warning in _context_warnings(args):
        print(f"# warning: {warning}")
    print(rendered)
    for service_name, route_text in render_compose_routes(config, deployment).items():
        print(f"# route: {compose_route_path(config, deployment, service_name)}")
        print(route_text)
    return 0


def cmd_compose_deploy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage_classes = _control_storage_classes_for_local(args, required=True)
    node_records = _control_node_records_for_local(args, required=True)
    deployment = load_compose_deployment(args.sidecar, storage_classes=storage_classes)
    # Inherit cluster engine when unset, mirroring the control plane, so the
    # dry-run preview matches what will actually be deployed.
    effective_engine = _require_nomad_engine(_compose_engine(config, args))
    output_format = _output_format(args)
    quiet = _quiet(args) or output_format != "text"
    if args.dry_run:
        from .nomad_render import render_compose_job

        stack = render_compose_job(config, deployment, resolve_secrets=False)
        routes = render_compose_routes(config, deployment)
        result = {
            "deployment": _compose_summary(config, deployment, stack, routes, artifact_kind="job"),
            "storage": storage_summary(deployment, node_records=node_records),
            "dryRun": True,
        }
        if output_format != "text":
            _print_success(args, result)
            return 0
        print(f"Dry run: would write {compose_stack_path(config, deployment)}")
        print(stack)
        for service_name, route_text in routes.items():
            print(f"Dry run: would write {compose_route_path(config, deployment, service_name)}")
            print(route_text)
        for warning in deployment.warnings:
            print(f"[warn] {warning}")
        return 0
    if args.timeout < 1:
        raise LumaError("--timeout must be at least 1 second")
    if not quiet:
        print(f"[start] Load compose deploy context: {args.sidecar}", flush=True)
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    if not quiet:
        print(f"[ok] Control endpoint: {endpoint}", flush=True)
    client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
    manifest_text, compose_text = _compose_request_text(args.sidecar, deployment)
    env_secrets = _deploy_env_secrets(args.deploy_env_file, [manifest_text, compose_text])
    result: Dict[str, Any] | None = None
    try:
        for event in client.deploy_compose_events(
            manifest=manifest_text,
            compose_content=compose_text,
            source_name=str(args.sidecar),
            skip_dns=args.skip_dns,
            skip_orchestrator=args.skip_orchestrator,
            env_secrets=env_secrets,
            timeout=args.timeout,
        ):
            status = str(event.get("status") or "")
            if output_format == "ndjson":
                _print_json({"type": "event", **event})
            if status in {"start", "ok", "fail"}:
                if not quiet:
                    _print_deploy_step(event)
                if status == "fail":
                    raise LumaError(str(event.get("message") or "compose deploy failed"))
            elif status == "done":
                payload = event.get("result")
                if not isinstance(payload, dict):
                    raise LumaError("control API stream ended without a compose deploy result")
                result = payload
    except LumaError as exc:
        if "control API error 404" not in str(exc):
            raise
    if result is None:
        result = _run_with_wait_heartbeat(
            lambda: client.deploy_compose(
                manifest=manifest_text,
                compose_content=compose_text,
                source_name=str(args.sidecar),
                skip_dns=args.skip_dns,
                skip_orchestrator=args.skip_orchestrator,
                env_secrets=env_secrets,
                timeout=args.timeout,
            ),
            timeout=args.timeout,
            emit=not quiet,
        )
        for step in result.get("steps") or []:
            if isinstance(step, dict) and not quiet:
                _print_deploy_step(step)
    if output_format != "text":
        _print_success(args, result)
        return 0
    print(f"[ok] Compose deploy finished: {result.get('deployment', deployment.name)}")
    for warning in (result.get("storage") or {}).get("warnings") or []:
        print(f"[warn] {warning}")
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
    result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).list_storage()
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
    result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).set_storage(
        name=args.name,
        provider=args.provider,
        external=args.external,
        node=args.node,
        path=args.path,
        endpoint=args.endpoint,
        mount_options=args.mount_options,
        regions=args.regions,
        nodes=args.nodes,
    )
    print(f"Storage class saved: {result.get('name', args.name)}")
    _print_storage_host_result(result.get("storageHost"))
    return 0


def cmd_storage_remove(args: argparse.Namespace) -> int:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).remove_storage(name=args.name)
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
    deployment = load_compose_deployment(args.sidecar, storage_classes=storage_classes)
    from .nomad_render import render_compose_job

    render_compose_job(load_config(args.config), deployment, resolve_secrets=False)
    if args.dry_run:
        print(dump_yaml(storage_summary(deployment, node_records=node_records)))
        return 0
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    client = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip)
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
    deployment = load_compose_deployment(args.sidecar, storage_classes=storage_classes)
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
    deployment = load_compose_deployment(args.sidecar, storage_classes=_control_storage_classes_for_local(args, required=True))
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


def _compose_summary(config: LumaConfig, deployment: Any, stack: str, routes: Dict[str, str], *, artifact_kind: str = "job") -> Dict[str, Any]:
    artifacts = [{"kind": artifact_kind, "path": str(compose_stack_path(config, deployment)), "content": stack}]
    for service_name, route_text in routes.items():
        artifacts.append({"kind": "route", "path": str(compose_route_path(config, deployment, service_name)), "content": route_text})
    return {
        "source": str(deployment.source),
        "name": deployment.name,
        "slug": deployment.slug,
        "compose": str(deployment.compose_path),
        "services": sorted(str(name) for name in deployment.compose.get("services", {}).keys()),
        "artifacts": artifacts,
        "warnings": deployment.warnings,
    }


def _compose_engine(config: LumaConfig, args: argparse.Namespace) -> str:
    return str(getattr(args, "engine", None) or config.defaults.get("engine") or "nomad")


def _service_engine(config: LumaConfig, service: Any, args: argparse.Namespace) -> str:
    return str(getattr(args, "engine", None) or getattr(service, "engine", "") or config.defaults.get("engine") or "nomad")


def _require_nomad_engine(engine: str) -> str:
    value = str(engine or "nomad").strip() or "nomad"
    if value != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    return value


def _compose_request_text(sidecar: Path, deployment: Any) -> tuple[str, str]:
    return sidecar.read_text(encoding="utf-8"), deployment.compose_path.read_text(encoding="utf-8")


def _control_storage_classes_for_local(args: argparse.Namespace, *, required: bool = False) -> Dict[str, Any] | None:
    try:
        setattr(args, "_luma_context_used", True)
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).list_storage()
    except LumaError as exc:
        if required:
            raise
        _add_context_warning(args, f"Storage classes were not loaded from Luma Control; validation is using local sidecar data only ({exc})")
        return None
    if not isinstance(result, dict):
        _add_context_warning(args, "Storage classes were not loaded from Luma Control; control API returned an invalid response")
        return None
    storage: Dict[str, Any] = {}
    for item in result.get("storageClasses") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        storage[name] = {key: value for key, value in item.items() if key != "name" and value not in ("", [], None)}
    return storage


def _control_node_records_for_local(args: argparse.Namespace, *, required: bool = False) -> Dict[str, Any] | None:
    try:
        setattr(args, "_luma_context_used", True)
        endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
        result = ControlClient(endpoint, token, insecure=insecure, resolve_ip=resolve_ip).status()
    except LumaError as exc:
        if required:
            raise
        _add_context_warning(args, f"Node records were not loaded from Luma Control; placement/storage reachability checks are degraded ({exc})")
        return None
    node_items = ((result.get("nodes") or {}).get("items") if isinstance(result, dict) else []) or []
    records: Dict[str, Any] = {}
    for item in node_items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        records[str(item["name"])] = dict(item)
    return records


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Login context", False, "Run: luma login <control-url> --token <management-token>"))
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
        control_ok = bool(verified.get("clusterId"))
        checks.append(("Control API", control_ok, "Check the control URL, token, DNS, and HTTPS route"))
        if control_ok:
            _append_control_status_checks(checks, client)
    except LumaError as exc:
        checks.append(("Control API", False, str(exc)))

    for name, ok, fix in checks:
        print(f"{name}: {'ok' if ok else 'fail'}")
        if not ok:
            print(f"  Fix: {fix}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def _append_control_status_checks(checks: list[tuple[str, bool, str]], client: ControlClient) -> None:
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
        if args.command == "registry":
            return cmd_registry(args)
        if args.command == "bootstrap":
            return cmd_bootstrap(args)
        if args.command == "update":
            return cmd_update(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "node":
            return cmd_node(args)
        if args.command == "node-agent":
            if args.node_agent_command == "run":
                return run_node_agent(args.config, once=args.once, poll_interval=args.poll_interval)
            if args.node_agent_command == "terminal-supervisor":
                return run_terminal_supervisor(args.config)
            raise LumaError(f"unknown node-agent command: {args.node_agent_command}")
        if args.command == "cloudflare":
            return cmd_cloudflare(args)
        if args.command == "egress":
            return cmd_egress(args)
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
        if args.command == "rollback":
            return cmd_rollback(args)
        if args.command == "history":
            return cmd_history(args)
        if args.command == "compose":
            return cmd_compose(args)
        if args.command == "storage":
            return cmd_storage(args)
    except LumaError as exc:
        if _print_structured_error(args, exc):
            return 1
        print(f"luma: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
