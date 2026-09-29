"""Luma CLI: main commands."""
from __future__ import annotations

import sys
from ..agent import run_node_agent, run_terminal_supervisor
from ..envfile import load_env_file
from ..errors import LumaError
from ..userconfig import load_user_config
from .apps import cmd_history, cmd_rollback, cmd_service
from .builds import cmd_build, cmd_import, cmd_workflow
from .common import _print_structured_error
from .deploy import cmd_compose, cmd_deploy, cmd_dns_sync, cmd_render, cmd_validate
from .manager import cmd_bootstrap, cmd_cloudflare, cmd_egress, cmd_manager, cmd_update
from .nodes import cmd_node, cmd_region, cmd_storage, cmd_tailscale
from .parser import build_parser
from .session import cmd_configure, cmd_context, cmd_doctor, cmd_init, cmd_login, cmd_preflight, cmd_status, cmd_version
from .settings import cmd_git_provider, cmd_registry, cmd_secret


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    args._raw_argv = list(argv)
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
        if args.command == "git-provider":
            return cmd_git_provider(args)
        if args.command == "bootstrap":
            return cmd_bootstrap(args)
        if args.command == "update":
            return cmd_update(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "manager":
            return cmd_manager(args)
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
        if args.command == "import":
            return cmd_import(args)
        if args.command == "build":
            return cmd_build(args)
        if args.command == "workflow":
            return cmd_workflow(args)
        if args.command == "rollback":
            return cmd_rollback(args)
        if args.command == "history":
            return cmd_history(args)
        if args.command == "compose":
            return cmd_compose(args)
        if args.command == "storage":
            return cmd_storage(args)
        if args.command == "region":
            return cmd_region(args)
    except LumaError as exc:
        if _print_structured_error(args, exc):
            return 1
        print(f"luma: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2
