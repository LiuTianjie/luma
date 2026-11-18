"""Client and stateless runner commands for the decoupled ``luma-build`` service.

Like ``luma observe``, the service itself runs independently (from ``builder/``)
and ``luma-control`` requires zero server-side changes: ``luma builder`` talks
directly to the ``luma-build`` endpoint while reusing the operator's saved
``luma login`` token automatically.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..builder_runner import NoRedirectHandler, RunnerDaemon
from ..control.context import config_home, current_context_name
from ..envfile import parse_env_file
from ..errors import LumaError
from ..io import atomic_write_text
from . import common


def _builder_config_file() -> Path:
    return config_home() / "build.json"


def _load_saved_builder_url(context_name: Optional[str] = None) -> str:
    cfg = _builder_config_file()
    if not cfg.is_file():
        return ""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    selected = context_name or current_context_name(required=False)
    by_ctx = data.get("byContext")
    if selected and isinstance(by_ctx, dict) and by_ctx.get(selected):
        return str(by_ctx[selected]).strip().rstrip("/")
    return str(data.get("url") or "").strip().rstrip("/")


def _save_builder_url(url: str, *, context_name: Optional[str] = None) -> Path:
    clean = str(url or "").strip().rstrip("/")
    if not clean.startswith(("https://", "http://")):
        raise LumaError("builder URL must start with https:// or http://")
    target = _builder_config_file()
    existing: Dict[str, Any] = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    existing["url"] = clean
    selected = context_name or current_context_name(required=False)
    if selected:
        by_ctx = existing.get("byContext") if isinstance(existing.get("byContext"), dict) else {}
        by_ctx[selected] = clean
        existing["byContext"] = by_ctx
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(existing, indent=2, sort_keys=True) + "\n", mode=0o600)
    return target


def _resolve_builder_connection(args: argparse.Namespace) -> Tuple[str, str, bool]:
    ctx_name = common._arg_text(args, "control_context") or common._env_text("LUMA_CONTROL_CONTEXT")
    url = (
        common._arg_text(args, "builder_url")
        or common._env_text("LUMA_BUILD_URL")
        or _load_saved_builder_url(ctx_name)
    )
    if not url:
        raise LumaError(
            "luma-build URL is not set; run 'luma builder use https://build.example.com' or pass --url"
        )

    explicit_token = (
        common._arg_text(args, "token")
        or common._env_text("LUMA_BUILD_ADMIN_TOKEN")
        or common._env_text("LUMA_BUILD_TOKEN")
        or common._env_text("LUMA_DEPLOY_TOKEN")
    )
    cli_insecure = bool(getattr(args, "insecure", False))
    env_insecure = common._env_bool("LUMA_INSECURE")

    if explicit_token:
        return url.rstrip("/"), explicit_token, bool(cli_insecure or env_insecure)

    _ctl_url, ctl_token, ctl_insecure, _resolve_ip = common._control_context(args, require_token=True)
    return url.rstrip("/"), ctl_token, bool(cli_insecure or env_insecure or ctl_insecure)


class BuilderClient:
    def __init__(self, url: str, token: str, *, insecure: bool = False) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.insecure = insecure

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: List[Any] = [NoRedirectHandler()]
        if self.insecure:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        return urllib.request.build_opener(*handlers)

    def request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None, *, timeout: int = 30
    ) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(detail).get("error") or detail
            except ValueError:
                msg = detail
            raise LumaError(f"luma-build HTTP {exc.code}: {msg}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LumaError(f"cannot reach luma-build at {self.url}: {exc}") from exc

    def stream_ndjson(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        emit_text: bool = True,
        emit_ndjson: bool = False,
        timeout: int = 7200,
    ) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/x-ndjson",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    if item.get("type") == "event":
                        if emit_ndjson:
                            common._print_json(item)
                        elif emit_text:
                            print(f"[{item.get('stage')}] {item.get('message')}", flush=True)
                    elif item.get("type") == "done":
                        return item.get("build") or {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(detail).get("error") or detail
            except ValueError:
                msg = detail
            raise LumaError(f"luma-build HTTP {exc.code}: {msg}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LumaError(f"luma-build stream interrupted: {exc}") from exc
        raise LumaError("luma-build stream ended without final build status")


def _add_builder_client_options(
    parser: argparse.ArgumentParser,
    add_control_arguments: Any,
    add_output_arguments: Any,
) -> None:
    parser.add_argument(
        "--url",
        dest="builder_url",
        default="",
        help="luma-build URL override (default: saved via 'luma builder use' or LUMA_BUILD_URL)",
    )
    add_control_arguments(parser)
    add_output_arguments(parser)


def add_parser(
    sub: Any,
    command_fn: Any,
    commands_fn: Any,
    add_control_arguments: Any,
    add_output_arguments: Any,
) -> None:
    builder = command_fn(
        sub,
        "builder",
        description=(
            "Submit remote builds and manage stateless BuildKit runners through the decoupled "
            "luma-build service. Operator authentication reuses your current 'luma login' session."
        ),
        epilog=(
            "Examples:\n"
            "  luma builder use https://build.example.com\n"
            "  luma builder submit acme/web --ref main\n"
            "  luma builder runner --url https://build.example.com --token lbr_xxx"
        ),
    )
    bsub = commands_fn(builder, "builder_command")

    use_p = command_fn(bsub, "use", "Save the default luma-build endpoint URL")
    use_p.add_argument("endpoint", help="luma-build base URL, for example https://build.example.com")
    use_p.add_argument("--control-context", default="", help="Associate this URL with a named Luma login context")
    add_output_arguments(use_p)

    st_p = command_fn(bsub, "status", "Show luma-build service health, registry target and online runners")
    _add_builder_client_options(st_p, add_control_arguments, add_output_arguments)

    sub_p = command_fn(bsub, "submit", "Submit a Git repository build and optional Luma deployment")
    sub_p.add_argument("source", help="Git repository URL or owner/name shorthand")
    sub_p.add_argument("--ref", default="main", help="Branch, tag or 40-char commit SHA (default: main)")
    sub_p.add_argument("--provider-id", default="", help="Saved Git credential ID, for example gitea:gcode")
    sub_p.add_argument("--platform", default="", help="Target container platform, for example linux/amd64 or linux/arm64")
    sub_p.add_argument("--runner", default="", help="Pin a specific registered runner name")
    sub_p.add_argument("--compose-sidecar", default="", help="Repository-relative luma.compose.yml path")
    sub_p.add_argument("--context", default="", help="Docker build context subdirectory inside the repository")
    sub_p.add_argument("--dockerfile", default="", help="Dockerfile path inside the repository")
    sub_p.add_argument("--target", default="", help="Dockerfile multi-stage target name")
    sub_p.add_argument("--region", default="", help="Override Luma deployment region")
    sub_p.add_argument("--node", default="", help="Override Luma deployment target node")
    sub_p.add_argument("--exposure", default="", help="Override Luma single-service exposure")
    sub_p.add_argument("--domain", default="", help="Override Luma single-service domain")
    sub_p.add_argument("--port", type=int, default=None, help="Override Luma single-service container port")
    sub_p.add_argument("--env", dest="deploy_env_file", type=Path, default=None, help="Runtime .env forwarded only to Luma Control on deploy")
    sub_p.add_argument("--no-deploy", action="store_true", help="Build and push image only without deploying to Luma Control")
    sub_p.add_argument("--no-cache", action="store_true", help="Disable BuildKit cache for this build")
    sub_p.add_argument("--detach", action="store_true", help="Return immediately after queueing the build")
    _add_builder_client_options(sub_p, add_control_arguments, add_output_arguments)

    ls_p = command_fn(bsub, "list", "List recent remote builds on luma-build")
    ls_p.add_argument("--status", default="", help="Filter by status (queued, building, deploying, succeeded, failed, canceled)")
    ls_p.add_argument("--repo", default="", help="Filter by repository substring")
    ls_p.add_argument("--limit", type=int, default=25, help="Maximum records to return, 1-200 (default: 25)")
    _add_builder_client_options(ls_p, add_control_arguments, add_output_arguments)

    logs_p = command_fn(bsub, "logs", "Show build details and BuildKit logs")
    logs_p.add_argument("id", help="Build ID from 'luma builder list'")
    logs_p.add_argument("--follow", "-f", action="store_true", help="Stream live logs until the build finishes")
    _add_builder_client_options(logs_p, add_control_arguments, add_output_arguments)

    can_p = command_fn(bsub, "cancel", "Cancel a queued or running build")
    can_p.add_argument("id", help="Build ID to cancel")
    _add_builder_client_options(can_p, add_control_arguments, add_output_arguments)

    ret_p = command_fn(bsub, "retry", "Retry a previous remote build")
    ret_p.add_argument("id", help="Build ID to retry")
    ret_p.add_argument("--env", dest="deploy_env_file", type=Path, default=None, help="Updated runtime .env for Luma Control deployment")
    ret_p.add_argument("--detach", action="store_true", help="Return immediately after queueing the retry")
    _add_builder_client_options(ret_p, add_control_arguments, add_output_arguments)

    tok_p = command_fn(bsub, "token", "Issue, list and revoke stateless runner registration tokens")
    tok_sub = commands_fn(tok_p, "builder_token_command")
    tok_create = command_fn(tok_sub, "create", "Issue a scoped lbr_... token for a runner")
    tok_create.add_argument("name", help="Runner name, for example x86-builder, m4 or air3")
    tok_create.add_argument("--weight", type=int, default=100, help="Scheduling weight when idle, 1-1000 (default: 100)")
    tok_create.add_argument("--concurrency", type=int, default=1, help="Max concurrent builds on this runner, 1-16 (default: 1)")
    tok_create.add_argument(
        "--cache-mode",
        choices=("read-write", "read-only", "local-only"),
        default="read-write",
        help="Remote OCI cache policy; use read-only on low-upload home runners (default: read-write)",
    )
    tok_create.add_argument("--rotate", action="store_true", help="Rotate token if the runner name already exists")
    _add_builder_client_options(tok_create, add_control_arguments, add_output_arguments)

    tok_list = command_fn(tok_sub, "list", "List registered runners and their heartbeat status")
    _add_builder_client_options(tok_list, add_control_arguments, add_output_arguments)

    tok_rev = command_fn(tok_sub, "revoke", "Revoke a runner token immediately")
    tok_rev.add_argument("name", help="Runner name or ID")
    _add_builder_client_options(tok_rev, add_control_arguments, add_output_arguments)

    run_p = command_fn(
        bsub,
        "runner",
        "Start a stateless BuildKit runner daemon on this machine (no Nomad required)",
    )
    run_p.add_argument("--url", dest="runner_url", default="", help="luma-build URL (or set LUMA_BUILD_URL)")
    run_p.add_argument("--token", dest="runner_token", default="", help="Runner token starting with lbr_ (or set LUMA_RUNNER_TOKEN)")
    run_p.add_argument(
        "--state-dir",
        type=Path,
        default=Path("~/.luma-runner"),
        help="Local directory for persistent BuildKit builder metadata (default: ~/.luma-runner)",
    )
    run_p.add_argument("--proxy", default="", help="Optional local HTTP(S) proxy for git clone and base image pulls")
    run_p.add_argument(
        "--cache-mode",
        choices=("", "read-write", "read-only", "local-only"),
        default="",
        help="Override remote registry cache policy on this host",
    )
    run_p.add_argument("--cache-keep-gb", type=int, default=20, help="Local BuildKit SSD cache LRU cap in GiB (default: 20)")
    run_p.add_argument("--max-parallelism", type=int, default=0, help="Max parallel BuildKit steps; 0 uses engine default")
    run_p.add_argument("--workers", type=int, default=1, help="Concurrent worker slots on this host, 1-16 (default: 1)")
    run_p.add_argument("--insecure", action="store_true", help="Skip TLS verification for the luma-build endpoint")
    run_p.add_argument("--once", action="store_true", help="Poll once, run at most one build and exit")

    gc_p = command_fn(bsub, "git-cred", "Manage dynamic Git pull credentials stored on luma-build")
    gc_sub = commands_fn(gc_p, "builder_git_cred_command")
    gc_set = command_fn(gc_sub, "set", "Save a Git credential on luma-build")
    gc_set.add_argument("id", help="Credential ID, for example gitea:gcode or github:main")
    gc_set.add_argument("--host", required=True, help="Git hostname, for example gcode.gaojiua.com or github.com")
    gc_set.add_argument("--username", default="x-access-token", help="Git HTTP username (default: x-access-token)")
    gc_set.add_argument("--path-prefix", default="", help="Optional organization/owner prefix filter")
    gc_set.add_argument("--token-stdin", action="store_true", help="Read the Git token from stdin")
    _add_builder_client_options(gc_set, add_control_arguments, add_output_arguments)

    gc_list = command_fn(gc_sub, "list", "List dynamic Git credentials on luma-build")
    _add_builder_client_options(gc_list, add_control_arguments, add_output_arguments)

    gc_rm = command_fn(gc_sub, "remove", "Remove a dynamic Git credential from luma-build")
    gc_rm.add_argument("id", help="Credential ID to remove")
    _add_builder_client_options(gc_rm, add_control_arguments, add_output_arguments)

    rc_p = command_fn(bsub, "registry-cred", "Manage dynamic OCI registry push credentials on luma-build")
    rc_sub = commands_fn(rc_p, "builder_reg_cred_command")
    rc_set = command_fn(rc_sub, "set", "Save an OCI registry push credential on luma-build")
    rc_set.add_argument("host", help="Registry host, for example crpi-xxx.cn-beijing.personal.cr.aliyuncs.com")
    rc_set.add_argument("--username", required=True, help="Registry push username")
    rc_set.add_argument("--password-stdin", action="store_true", help="Read registry password from stdin")
    rc_set.add_argument("--http", dest="force_http", action="store_true", default=None, help="Force plain HTTP for an internal registry")
    _add_builder_client_options(rc_set, add_control_arguments, add_output_arguments)

    rc_list = command_fn(rc_sub, "list", "List dynamic OCI registry credentials on luma-build")
    _add_builder_client_options(rc_list, add_control_arguments, add_output_arguments)

    rc_rm = command_fn(rc_sub, "remove", "Remove a dynamic OCI registry credential from luma-build")
    rc_rm.add_argument("host", help="Registry host to remove")
    _add_builder_client_options(rc_rm, add_control_arguments, add_output_arguments)


def cmd_builder(args: argparse.Namespace) -> int:
    subcmd = args.builder_command
    fmt = common._output_format(args)
    quiet = common._quiet(args)

    if subcmd == "use":
        ctx_name = common._arg_text(args, "control_context") or None
        saved_path = _save_builder_url(args.endpoint, context_name=ctx_name)
        payload = {"url": args.endpoint.strip().rstrip("/"), "configPath": str(saved_path)}
        if fmt != "text":
            common._print_success(args, payload)
        elif not quiet:
            print(f"Saved luma-build endpoint: {payload['url']} ({saved_path})")
        return 0

    if subcmd == "runner":
        url = (
            common._arg_text(args, "runner_url")
            or common._env_text("LUMA_BUILD_URL")
            or _load_saved_builder_url()
        )
        tok = common._arg_text(args, "runner_token") or common._env_text("LUMA_RUNNER_TOKEN")
        if not url or not tok:
            raise LumaError("runner requires --url (or LUMA_BUILD_URL) and --token (or LUMA_RUNNER_TOKEN)")
        proxy = common._arg_text(args, "proxy") or common._env_text("LUMA_RUNNER_PROXY") or ""
        cache_mode = common._arg_text(args, "cache_mode") or common._env_text("LUMA_RUNNER_CACHE_MODE") or ""
        state_dir = Path(os.environ.get("LUMA_RUNNER_STATE_DIR") or args.state_dir)
        daemon = RunnerDaemon(
            server_url=url,
            runner_token=tok,
            state_dir=state_dir,
            local_proxy=proxy,
            cache_mode=cache_mode,
            cache_keep_gb=int(os.environ.get("LUMA_RUNNER_CACHE_KEEP_GB") or args.cache_keep_gb),
            max_parallelism=int(os.environ.get("LUMA_RUNNER_MAX_PARALLELISM") or args.max_parallelism),
            workers=int(os.environ.get("LUMA_RUNNER_WORKERS") or args.workers),
            insecure_tls=bool(args.insecure),
        )
        return daemon.run_once_or_forever(once=bool(args.once))

    url, token, insecure = _resolve_builder_connection(args)
    client = BuilderClient(url, token, insecure=insecure)

    if subcmd == "status":
        res = client.request("GET", "/v1/status")
        if fmt != "text":
            common._print_success(args, res)
            return 0
        reg = res.get("registry") or {}
        ctl = res.get("controlTarget") or {}
        common._print_key_values(
            f"Luma Build ({url})",
            [
                ("Version", str(res.get("version") or "-")),
                ("Auth mode", str(res.get("authMode") or "-")),
                ("Registry pull", str(reg.get("registryHost") or "-")),
                ("Registry push", str(reg.get("pushHost") or "-")),
                ("Namespace", f"{reg.get('namespace') or '-'} (flatten={reg.get('flatten')})"),
                ("Control target", str(ctl.get("endpoint")) if ctl.get("configured") else "disabled (build-only)"),
            ],
        )
        runners = res.get("runners") or []
        print(f"\nRunners ({len(runners)})")
        if runners:
            common._print_table(
                ["NAME", "STATE", "PLATFORM", "SLOTS", "WEIGHT", "CACHE"],
                [
                    [
                        str(r.get("name") or "-"),
                        "revoked" if r.get("revoked") else ("online" if r.get("online") else "offline"),
                        str(r.get("nativePlatform") or "-"),
                        f"{r.get('activeBuilds', 0)}/{r.get('maxConcurrency', 1)}",
                        str(r.get("weight", 100)),
                        str(r.get("cacheMode") or "-"),
                    ]
                    for r in runners
                ],
            )
        else:
            print("  (no runners registered)")
        return 0

    if subcmd == "submit":
        src = str(args.source).strip()
        is_url = "://" in src or src.startswith("git@")
        body: Dict[str, Any] = {
            "repoUrl": src if is_url else "",
            "repository": "" if is_url else src,
            "ref": args.ref,
            "providerId": args.provider_id,
            "platform": args.platform,
            "runner": args.runner,
            "composeSidecar": args.compose_sidecar,
            "context": args.context,
            "dockerfile": args.dockerfile,
            "target": args.target,
            "region": args.region,
            "node": args.node,
            "exposure": args.exposure,
            "domain": args.domain,
            "port": args.port,
            "deploy": not args.no_deploy,
            "noCache": args.no_cache,
        }
        if args.deploy_env_file:
            if not args.deploy_env_file.is_file():
                raise LumaError(f"deployment env file not found: {args.deploy_env_file}")
            body["envSecrets"] = parse_env_file(args.deploy_env_file)

        if args.detach:
            res = client.request("POST", "/v1/builds", body)
            if fmt != "text":
                common._print_success(args, res)
            else:
                print(f"Queued build: {res['build']['id']}")
            return 0

        final_build = client.stream_ndjson(
            "POST",
            "/v1/builds?stream=1",
            body,
            emit_text=(fmt == "text" and not quiet),
            emit_ndjson=(fmt == "ndjson" and not quiet),
        )
        if final_build.get("status") != "succeeded":
            raise LumaError(str(final_build.get("message") or f"build {final_build.get('id')} {final_build.get('status')}"))
        if fmt != "text":
            common._print_success(args, {"build": final_build})
        else:
            print(f"[succeeded] {final_build.get('id')}: {final_build.get('message')}")
            image = (final_build.get("result") or {}).get("image")
            if image:
                print(f"  Image: {image}")
        return 0

    if subcmd == "list":
        qs = urllib.parse.urlencode(
            {k: v for k, v in {"status": args.status, "repo": args.repo, "limit": args.limit}.items() if v}
        )
        res = client.request("GET", f"/v1/builds?{qs}" if qs else "/v1/builds")
        if fmt != "text":
            common._print_success(args, res)
            return 0
        builds = res.get("builds") or []
        if not builds:
            print("No builds found.")
            return 0
        common._print_table(
            ["ID", "STATUS", "RUNNER", "REPO@REF", "CREATED"],
            [
                [
                    str(b.get("id") or "-"),
                    str(b.get("status") or "-"),
                    str(b.get("runner") or b.get("pinnedRunner") or "-"),
                    f"{b.get('repoSlug')}@{b.get('ref')}",
                    common._format_epoch(int(b.get("createdAt") or 0)),
                ]
                for b in builds
            ],
        )
        return 0

    if subcmd == "logs":
        bid = urllib.parse.quote(args.id, safe="")
        if args.follow:
            final_build = client.stream_ndjson(
                "GET",
                f"/v1/builds/{bid}/stream",
                emit_text=(fmt == "text" and not quiet),
                emit_ndjson=(fmt == "ndjson" and not quiet),
            )
            if fmt != "text":
                common._print_success(args, {"build": final_build})
            return 0 if final_build.get("status") == "succeeded" else 1

        res = client.request("GET", f"/v1/builds/{bid}")
        b = res["build"]
        if fmt != "text":
            common._print_success(args, res)
            return 0
        print(f"Build {b['id']} ({b['status']}) on {b.get('runner') or '-'}: {b['repoSlug']}@{b['ref']}")
        for ev in b.get("events") or []:
            print(f"  [{ev['stage']}] {ev['message']}")
        return 0

    if subcmd == "cancel":
        res = client.request("POST", f"/v1/builds/{urllib.parse.quote(args.id, safe='')}/cancel", {})
        if fmt != "text":
            common._print_success(args, res)
        elif not quiet:
            print(f"Build {res['build']['id']}: {res['build']['status']}")
        return 0

    if subcmd == "retry":
        path = f"/v1/builds/{urllib.parse.quote(args.id, safe='')}/retry"
        body_retry: Dict[str, Any] = {}
        if args.deploy_env_file:
            if not args.deploy_env_file.is_file():
                raise LumaError(f"deployment env file not found: {args.deploy_env_file}")
            body_retry["envSecrets"] = parse_env_file(args.deploy_env_file)
        if args.detach:
            res = client.request("POST", path, body_retry)
            if fmt != "text":
                common._print_success(args, res)
            else:
                print(f"Queued retry: {res['build']['id']}")
            return 0
        final_build = client.stream_ndjson(
            "POST",
            f"{path}?stream=1",
            body_retry,
            emit_text=(fmt == "text" and not quiet),
            emit_ndjson=(fmt == "ndjson" and not quiet),
        )
        if final_build.get("status") != "succeeded":
            raise LumaError(str(final_build.get("message") or "retry failed"))
        if fmt != "text":
            common._print_success(args, {"build": final_build})
        else:
            print(f"[succeeded] {final_build.get('id')}: {final_build.get('message')}")
        return 0

    if subcmd == "token":
        tcmd = args.builder_token_command
        if tcmd == "create":
            res = client.request(
                "POST",
                "/v1/runners/tokens",
                {
                    "name": args.name,
                    "weight": args.weight,
                    "maxConcurrency": args.concurrency,
                    "cacheMode": args.cache_mode,
                    "rotate": args.rotate,
                },
            )
            if fmt != "text":
                common._print_success(args, res)
            else:
                r = res["runner"]
                print(f"Runner registered: {r['name']} (weight={r['weight']}, cache={r['cacheMode']})")
                print(f"LUMA_RUNNER_TOKEN={r['runnerToken']}")
                print(f"\nStart on macOS/Linux:\n  luma builder runner --url {url} --token {r['runnerToken']} --cache-mode {r['cacheMode']}")
            return 0
        if tcmd == "list":
            res = client.request("GET", "/v1/runners")
            if fmt != "text":
                common._print_success(args, res)
            else:
                runners = res.get("runners") or []
                if not runners:
                    print("No runners registered.")
                else:
                    common._print_table(
                        ["NAME", "STATE", "PLATFORM", "SLOTS", "WEIGHT", "CACHE"],
                        [
                            [
                                str(r.get("name") or "-"),
                                "revoked" if r.get("revoked") else ("online" if r.get("online") else "offline"),
                                str(r.get("nativePlatform") or "-"),
                                f"{r.get('activeBuilds', 0)}/{r.get('maxConcurrency', 1)}",
                                str(r.get("weight", 100)),
                                str(r.get("cacheMode") or "-"),
                            ]
                            for r in runners
                        ],
                    )
            return 0
        if tcmd == "revoke":
            res = client.request("POST", f"/v1/runners/{urllib.parse.quote(args.name, safe='')}/revoke", {})
            if fmt != "text":
                common._print_success(args, res)
            elif not quiet:
                print(f"Revoked runner: {res['runner']['name']}")
            return 0

    if subcmd == "git-cred":
        gcmd = args.builder_git_cred_command
        if gcmd == "set":
            secret = sys.stdin.read().strip() if args.token_stdin else getpass.getpass("Git token: ").strip()
            res = client.request(
                "POST",
                "/v1/credentials/git",
                {
                    "id": args.id,
                    "host": args.host,
                    "username": args.username,
                    "pathPrefix": args.path_prefix,
                    "token": secret,
                },
            )
            if fmt != "text":
                common._print_success(args, res)
            elif not quiet:
                print(f"Saved Git credential: {args.id}")
            return 0
        if gcmd == "list":
            res = client.request("GET", "/v1/credentials/git")
            if fmt != "text":
                common._print_success(args, res)
            else:
                print(json.dumps(res.get("credentials") or [], indent=2))
            return 0
        if gcmd == "remove":
            res = client.request("DELETE", f"/v1/credentials/git/{urllib.parse.quote(args.id, safe='')}")
            if fmt != "text":
                common._print_success(args, res)
            elif not quiet:
                print(f"Removed Git credential: {args.id}")
            return 0

    if subcmd == "registry-cred":
        rcmd = args.builder_reg_cred_command
        if rcmd == "set":
            pw = sys.stdin.read().strip() if args.password_stdin else getpass.getpass("Registry password: ").strip()
            res = client.request(
                "POST",
                "/v1/credentials/registry",
                {
                    "host": args.host,
                    "username": args.username,
                    "password": pw,
                    "insecure": args.force_http,
                },
            )
            if fmt != "text":
                common._print_success(args, res)
            elif not quiet:
                print(f"Saved Registry credential: {res['credential']['host']}")
            return 0
        if rcmd == "list":
            res = client.request("GET", "/v1/credentials/registry")
            if fmt != "text":
                common._print_success(args, res)
            else:
                print(json.dumps(res.get("credentials") or [], indent=2))
            return 0
        if rcmd == "remove":
            res = client.request("DELETE", f"/v1/credentials/registry/{urllib.parse.quote(args.host, safe='')}")
            if fmt != "text":
                common._print_success(args, res)
            elif not quiet:
                print(f"Removed Registry credential: {args.host}")
            return 0

    return 0
