"""Local runner enrollment and persistent service commands."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import urllib.error

from ..builder_runner import RunnerDaemon
from ..errors import LumaError
from .. import runner_service
from ..runner_logging import rotating_output
from ..build_prerequisites import prepare_build_dependencies, prepare_runner_buildkit, verify_build_dependencies
from . import common


def add_runner_arguments(parser: argparse.ArgumentParser, *, foreground: bool = False) -> None:
    parser.add_argument("--url", dest="runner_url", default=None, help="luma-build URL (or LUMA_BUILD_URL / saved builder URL)")
    parser.add_argument("--token", dest="runner_token", default=None, help="Runner token starting with lbr_ (or LUMA_RUNNER_TOKEN)")
    parser.add_argument("--token-stdin", action="store_true", help="Read the runner token from stdin instead of putting it in shell history")
    parser.add_argument("--state-dir", type=Path, default=None, help="Persistent BuildKit metadata directory (default: ~/.luma-runner)")
    parser.add_argument("--proxy", default=None, help="Local HTTP(S) proxy for Git and base image pulls (or LUMA_RUNNER_PROXY)")
    parser.add_argument("--no-proxy", default=None, help="Hosts/CIDRs bypassing --proxy (or LUMA_RUNNER_NO_PROXY)")
    parser.add_argument("--cache-mode", choices=("read-write", "read-only", "local-only"), default=None, help="Remote registry cache policy; omitted keeps the server policy")
    parser.add_argument("--cache-keep-gb", type=int, default=None, help="Local BuildKit disk cache cap in GiB (default: 20)")
    parser.add_argument("--max-parallelism", type=int, default=None, help="Max parallel BuildKit steps; 0 uses the engine default")
    parser.add_argument("--workers", type=int, default=None, help="Concurrent worker slots, 1-16 (default: 1)")
    parser.add_argument("--insecure", action="store_true", default=None, help="Skip TLS verification for the luma-build endpoint")
    if foreground:
        parser.add_argument("--once", action="store_true", help="Poll once, run at most one build and exit")


def add_parser(sub, command_fn, commands_fn, add_output_arguments) -> None:
    parser = command_fn(sub, "runner", "Join and manage this machine's persistent build runner",
                        epilog="First: luma runner join --url https://build.example.com --token lbr_xxx\n"
                        "Then: luma runner stop / start / restart / status / logs --follow")
    commands = commands_fn(parser, "runner_command")
    join = command_fn(commands, "join", "Install build tools, prepare BuildKit and join the background runner")
    add_runner_arguments(join)
    add_output_arguments(join)
    for name, help_text in (
        ("start", "Start the configured runner and enable automatic startup"),
        ("stop", "Stop the local runner and disable automatic startup; keep its configuration"),
        ("restart", "Restart the configured runner service"),
        ("status", "Show the local runner service state (remote presence: luma builder token list)"),
    ):
        child = command_fn(commands, name, help_text)
        add_output_arguments(child)
    logs = command_fn(commands, "logs", "Read the local runner service logs")
    logs.add_argument("--follow", "-f", action="store_true", help="Follow new log output")
    logs.add_argument("--lines", "-n", type=int, default=100, help="Number of recent lines, 1-10000 (default: 100)")
    run = command_fn(commands, "run", "Run in the foreground for debugging or inside a container")
    add_runner_arguments(run, foreground=True)
    run.add_argument("--service-config", type=Path, default=None, help=argparse.SUPPRESS)


def resolve_options(args: argparse.Namespace, previous: dict | None = None) -> dict:
    from .builder import _load_saved_builder_url
    previous = previous or {}
    mappings = (
        ("server_url", "runner_url", "LUMA_BUILD_URL", _load_saved_builder_url()),
        ("runner_token", "runner_token", "LUMA_RUNNER_TOKEN", ""),
        ("state_dir", "state_dir", "LUMA_RUNNER_STATE_DIR", str(Path.home() / ".luma-runner")),
        ("local_proxy", "proxy", "LUMA_RUNNER_PROXY", ""),
        ("local_no_proxy", "no_proxy", "LUMA_RUNNER_NO_PROXY", ""),
        ("cache_mode", "cache_mode", "LUMA_RUNNER_CACHE_MODE", ""),
        ("cache_keep_gb", "cache_keep_gb", "LUMA_RUNNER_CACHE_KEEP_GB", 20),
        ("max_parallelism", "max_parallelism", "LUMA_RUNNER_MAX_PARALLELISM", 0),
        ("workers", "workers", "LUMA_RUNNER_WORKERS", 1),
    )
    options = {}
    for key, attribute, variable, default in mappings:
        value = getattr(args, attribute, None)
        if value is None:
            value = os.environ.get(variable) or previous.get(key, default)
        options[key] = str(value) if key == "state_dir" else value
    insecure = getattr(args, "insecure", None)
    options["insecure_tls"] = bool(previous.get("insecure_tls", False) if insecure is None else insecure)
    if getattr(args, "token_stdin", False):
        if getattr(args, "runner_token", None):
            raise LumaError("Use only one of --token and --token-stdin")
        options["runner_token"] = sys.stdin.readline().strip()
    if options["cache_mode"] not in {"", "read-write", "read-only", "local-only"}:
        raise LumaError("Invalid runner cache mode")
    return runner_service.validate_options(options)


def run_foreground(args: argparse.Namespace) -> int:
    config_path = getattr(args, "service_config", None)
    if config_path:
        # Service argv only contains this path; token and Docker/proxy settings
        # stay inside the mode-0600 configuration.
        try:
            config = json.loads(config_path.read_text())
            options = runner_service.validate_options(config["options"])
            os.environ.update(config["environment"])
        except (OSError, ValueError, KeyError, TypeError):
            raise LumaError(f"Invalid runner service configuration: {config_path}") from None
    else:
        options = resolve_options(args)
    output = rotating_output(runner_service.LOG) if config_path and sys.platform == "darwin" else contextlib.nullcontext()
    with output:
        verify_build_dependencies()
        daemon = RunnerDaemon(**options)
        prepare_runner_buildkit(daemon)
        return daemon.run_once_or_forever(once=bool(getattr(args, "once", False)))


def cmd_runner(args: argparse.Namespace) -> int:
    action = args.runner_command
    if action == "run":
        return run_foreground(args)
    if action == "logs":
        return runner_service.logs(follow=args.follow, lines=args.lines)
    if action == "join":
        runner_service.platform_backend()
        previous = runner_service.load_config()
        options = resolve_options(args, previous.get("options"))
        # Install and verify build tools before replacing service configuration
        # or registering presence. Enrollment never leases a build.
        prepare_build_dependencies()
        daemon = RunnerDaemon(**options)
        try:
            response = daemon._post("/v1/runner/join", {})
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise LumaError("Runner token is invalid or deleted; create a new token before joining") from None
            if exc.code == 404:
                raise LumaError("The luma-build server does not support runner join; upgrade the build service first") from None
            raise LumaError(f"Runner join failed (HTTP {exc.code})") from None
        except (OSError, ValueError):
            raise LumaError("Cannot reach luma-build to verify the runner token") from None
        runner = response.get("runner") or {}
        if not runner.get("id") or not runner.get("name"):
            raise LumaError("luma-build returned an invalid runner identity")
        if runner.get("activeBuilds", 0):
            raise LumaError("Runner has active builds; wait for them to finish before joining again")
        prepare_runner_buildkit(daemon)
        result = runner_service.start(options, configure=True)
        result["runner"] = runner["name"]
    elif action in {"start", "restart"}:
        result = runner_service.start(runner_service.load_config(required=True)["options"], restart=action == "restart")
    elif action == "stop":
        result = runner_service.stop()
    else:
        result = runner_service.status()
    if common._output_format(args) != "text":
        common._print_success(args, result)
    elif not getattr(args, "quiet", False):
        if action == "join":
            print(f"Runner joined: {result['runner']}")
        print(f"Runner service: {result['state']} ({result['backend']})")
        if result.get("running"):
            print("Runs in the background and starts automatically. Logs: luma runner logs --follow")
        elif action == "stop":
            print("Automatic startup disabled. Resume with: luma runner start")
    return 0
