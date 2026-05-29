from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from .bootstrap import bootstrap_node, deploy_direct, setup_egress, setup_portainer, setup_tailscale
from .cloudflare import find_zone, sync_dns
from .config import LumaConfig, load_config, save_config
from .errors import LumaError
from .gitops import commit, push
from .io import dump_yaml, write_yaml
from .portainer import configured_webhook, trigger_webhook
from .profiles import PROFILES
from .remote import RemoteExecutor
from .render import render_stack, render_tailscale_route, route_path, stack_path
from .service import VALID_EXPOSURES, VALID_REGIONS, load_service, slugify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma", description="Self-hosted deployment control plane.")
    parser.add_argument("--config", type=Path, default=None, help="Path to luma.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--deep", action="store_true", help="Run slower live checks such as docker pull through egress")

    node = sub.add_parser("node")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    node_sub.add_parser("list")
    node_bootstrap = node_sub.add_parser("bootstrap")
    node_bootstrap.add_argument("node")
    node_bootstrap.add_argument("--profile", choices=sorted(PROFILES), required=True)

    cf = sub.add_parser("cloudflare")
    cf_sub = cf.add_subparsers(dest="cloudflare_command", required=True)
    cf_connect = cf_sub.add_parser("connect")
    cf_connect.add_argument("--zone", required=True)

    egress = sub.add_parser("egress")
    egress_sub = egress.add_subparsers(dest="egress_command", required=True)
    for name in ("setup", "refresh"):
        cmd = egress_sub.add_parser(name)
        cmd.add_argument("node")

    portainer = sub.add_parser("portainer")
    portainer_sub = portainer.add_subparsers(dest="portainer_command", required=True)
    portainer_setup = portainer_sub.add_parser("setup")
    portainer_setup.add_argument("node")

    tailscale = sub.add_parser("tailscale")
    tailscale_sub = tailscale.add_subparsers(dest="tailscale_command", required=True)
    tailscale_connect = tailscale_sub.add_parser("connect")
    tailscale_connect.add_argument("node")

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
    deploy.add_argument("--commit", action="store_true", help="Commit generated stack changes")
    deploy.add_argument("--push", action="store_true", help="Push Git changes before triggering Portainer")
    deploy.add_argument("--direct", action="store_true", help="Deploy directly with docker stack deploy over SSH")
    deploy.add_argument("--node", help="Manager node for --direct deploy")

    return parser


def validate_stack_file(path: Path) -> str:
    result = subprocess.run(
        ["docker", "compose", "-f", str(path), "config"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise LumaError(f"docker compose config failed for {path}:\n{result.stdout}")
    return f"Stack valid: {path}"


def write_rendered_service(config: LumaConfig, service_path: Path) -> tuple[Any, Path, Path | None]:
    service = load_service(service_path)
    target = stack_path(config, service)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_stack(config, service), encoding="utf-8")
    route_target = None
    if service.exposure == "tailscale-relay":
        route_target = route_path(config, service)
        route_target.parent.mkdir(parents=True, exist_ok=True)
        route_target.write_text(render_tailscale_route(config, service), encoding="utf-8")
    return service, target, route_target


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
        for line in bootstrap_node(config, node, profile):
            print(line)
        return 0
    raise LumaError(f"unknown node command: {args.node_command}")


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
    node = config.get_node(args.node)
    subscription_url = os.environ.get("EGRESS_SUBSCRIPTION_URL")
    if not subscription_url:
        raise LumaError("missing EGRESS_SUBSCRIPTION_URL")
    for line in setup_egress(config, node, subscription_url):
        print(line)
    if args.egress_command == "refresh":
        print("Egress refreshed")
    else:
        print("Egress setup complete")
    return 0


def cmd_portainer(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.portainer_command == "setup":
        node = config.get_node(args.node)
        for line in setup_portainer(node):
            print(line)
        return 0
    raise LumaError(f"unknown portainer command: {args.portainer_command}")


def cmd_tailscale(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.tailscale_command == "connect":
        node = config.get_node(args.node)
        for line in setup_tailscale(node):
            print(line)
        return 0
    raise LumaError(f"unknown tailscale command: {args.tailscale_command}")


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

    service, target, route_target = write_rendered_service(config, args.service)
    print(f"Stack written: {target}")
    written_paths = [target]
    if route_target:
        written_paths.append(route_target)
        print(f"Traefik relay route written: {route_target}")
    try:
        print(validate_stack_file(target))
    except FileNotFoundError:
        print("Stack validation skipped: docker is not installed")

    if not args.skip_dns:
        print(sync_dns(config, service))

    if args.direct:
        node = config.get_node(args.node) if args.node else config.default_manager()
        if not node:
            raise LumaError("no manager node configured for --direct")
        for line in deploy_direct(config, node, target, service.slug, route_target):
            print(line)
        return 0

    do_commit = args.commit or bool(config.git.get("autoCommit", False))
    do_push = args.push or bool(config.git.get("autoPush", False))
    if do_commit:
        message_template = config.git.get("commitMessage", "deploy {name}")
        message = str(message_template).format(name=service.name, region=service.region)
        print(commit(written_paths, message))
    if do_push:
        print(push())
    if not args.skip_webhook:
        if not configured_webhook(config):
            raise LumaError("Portainer webhook is required for default deploy. Set PORTAINER_WEBHOOK_URL or use --direct.")
        print(trigger_webhook(config, service))
    print(f"Deploy prepared for Portainer: {service.name} -> {target}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Config", bool(config.path and config.path.exists()), "Run: luma init"))
    checks.append(("Nodes", bool(config.nodes), "Add nodes to luma.yaml"))
    checks.append(("Cloudflare token", bool(os.environ.get(config.dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))), "Export CLOUDFLARE_API_TOKEN"))
    checks.append(("Portainer webhook", bool(configured_webhook(config)), "Set PORTAINER_WEBHOOK_URL"))
    checks.append(("Egress subscription", bool(os.environ.get("EGRESS_SUBSCRIPTION_URL")), "Export EGRESS_SUBSCRIPTION_URL"))
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
        print("Unknown command: depoly. Did you mean: deploy?", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args)
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
