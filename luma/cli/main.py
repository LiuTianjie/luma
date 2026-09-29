"""Entry point and command dispatch for the ``luma`` CLI."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Dict

from .. import __version__
from ..agent import run_node_agent, run_terminal_supervisor
from ..envfile import load_env_file, load_luma_settings
from ..errors import LumaError
from ..userconfig import load_user_config
from .apps import cmd_app
from .builds import cmd_build, cmd_import, cmd_workflow
from .common import _print_structured_error
from .deploy import cmd_compose, cmd_deploy, cmd_validate
from .manager import cmd_bootstrap, cmd_manager, cmd_update
from .nodes import cmd_node, cmd_region, cmd_storage
from .parser import build_parser
from .session import cmd_context, cmd_doctor, cmd_init, cmd_login, cmd_status, cmd_version
from .settings import cmd_git_provider, cmd_registry, cmd_secret

ISSUES_URL = "https://github.com/LiuTianjie/luma/issues"

COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init,
    "login": cmd_login,
    "context": cmd_context,
    "doctor": cmd_doctor,
    "version": cmd_version,
    "validate": cmd_validate,
    "deploy": cmd_deploy,
    "compose": cmd_compose,
    "import": cmd_import,
    "build": cmd_build,
    "workflow": cmd_workflow,
    "status": cmd_status,
    "app": cmd_app,
    "secret": cmd_secret,
    "bootstrap": cmd_bootstrap,
    "update": cmd_update,
    "node": cmd_node,
    "region": cmd_region,
    "storage": cmd_storage,
    "registry": cmd_registry,
    "git-provider": cmd_git_provider,
    "manager": cmd_manager,
}


def _run_node_agent(args: argparse.Namespace) -> int:
    if args.node_agent_command == "run":
        return run_node_agent(args.config, once=args.once, poll_interval=args.poll_interval)
    return run_terminal_supervisor(args.config)


def _load_environment(args: argparse.Namespace) -> None:
    """Load local settings without letting an application's .env leak in.

    An explicit --env-file is loaded completely and strictly. The implicit ./.env
    usually belongs to the application being deployed, so only Luma's own
    settings are read from it, never Control credentials, and a line Luma cannot
    parse is reported instead of failing the command.
    """
    if args.no_env:
        return
    if args.env_file is not None:
        if not args.env_file.is_file():
            raise LumaError(f"env file not found: {args.env_file}")
        load_env_file(args.env_file)
    else:
        for warning in load_luma_settings(Path(".env")):
            print(f"luma: warning: {warning}", file=sys.stderr)
    load_user_config()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    args._raw_argv = list(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        _load_environment(args)
        if args.command == "node-agent":
            return _run_node_agent(args)
        return COMMANDS[args.command](args)
    except LumaError as exc:
        if _print_structured_error(args, exc):
            return 1
        print(f"luma: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nluma: interrupted", file=sys.stderr)
        return 130
    except EOFError:
        print("\nluma: input ended before all values were entered; pass them as flags instead", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort report for unexpected bugs
        if os.environ.get("LUMA_DEBUG"):
            raise
        print(f"luma: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"luma: rerun with LUMA_DEBUG=1 for a traceback and report it at {ISSUES_URL} (luma {__version__})", file=sys.stderr)
        return 1
