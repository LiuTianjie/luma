from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .cloudflare import sync_dns
from .config import load_config
from .errors import LumaError
from .gitops import commit, push
from .portainer import trigger_webhook
from .render import render_stack, render_tailscale_route, route_path, stack_path
from .service import load_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="luma",
        description="Deploy region-aware Docker services from declarative manifests.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to luma.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

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


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = load_service(args.service)
    rendered = render_stack(config, service)
    print(f"Service valid: {service.name} ({service.service_kind})")
    print(rendered)
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

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"Stack written: {target}")
    written_paths = [target]
    if rendered_route and route_target:
        route_target.parent.mkdir(parents=True, exist_ok=True)
        route_target.write_text(rendered_route, encoding="utf-8")
        written_paths.append(route_target)
        print(f"Traefik relay route written: {route_target}")

    try:
        print(validate_stack_file(target))
    except FileNotFoundError:
        print("Stack validation skipped: docker is not installed")

    if not args.skip_dns:
        print(sync_dns(config, service))
    do_commit = args.commit or bool(config.git.get("autoCommit", False))
    do_push = args.push or bool(config.git.get("autoPush", False))
    if do_commit:
        message_template = config.git.get("commitMessage", "deploy {name}")
        message = str(message_template).format(name=service.name, region=service.region)
        print(commit(written_paths, message))
    if do_push:
        print(push())
    if not args.skip_webhook:
        print(trigger_webhook(config, service))

    print(f"Deploy prepared: {service.name} -> {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "depoly":
        print("Unknown command: depoly. Did you mean: deploy?", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
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
