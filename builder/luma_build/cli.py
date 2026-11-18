"""CLI for running luma-build server, stateless runners, and submitting builds."""
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
from typing import Any

from . import __version__
from .deployer import _NoRedirectHandler
from .errors import BuildError


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise BuildError(f".env file not found: {path}")
    secrets_map: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        if key:
            secrets_map[key] = val
    return secrets_map


def _luma_config_home() -> Path:
    return Path(os.environ.get("LUMA_CONFIG_HOME") or Path.home() / ".config" / "luma")


def _load_saved_build_url() -> str:
    cfg = _luma_config_home() / "build.json"
    if not cfg.is_file():
        return ""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return str(data.get("url") or "").strip() if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def _save_build_url(url: str) -> Path:
    clean = str(url or "").strip().rstrip("/")
    if not clean.startswith(("https://", "http://")):
        raise BuildError("luma-build URL must start with https:// or http://")
    home = _luma_config_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "build.json"
    target.write_text(json.dumps({"url": clean}, indent=2) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def _load_luma_context_token(context_name: str = "") -> tuple[str, bool]:
    """Reuse the operator's existing `luma login` context token automatically."""
    home = _luma_config_home()
    selected = str(context_name or os.environ.get("LUMA_CONTROL_CONTEXT") or "").strip()
    if not selected:
        cur = home / "current-context"
        if cur.is_file():
            selected = cur.read_text(encoding="utf-8").strip()
    if not selected:
        return "", False
    safe = "".join(ch for ch in selected if ch.isalnum() or ch in {"-", "_", "."})
    ctx_file = home / "contexts" / f"{safe}.json"
    if not ctx_file.is_file():
        return "", False
    try:
        data = json.loads(ctx_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", False
    if not isinstance(data, dict):
        return "", False
    return str(data.get("token") or "").strip(), bool(data.get("insecure", False))


class BuildApiClient:
    def __init__(self, url: str, token: str, *, insecure: bool = False, control_context: str = "") -> None:
        clean = str(url or os.environ.get("LUMA_BUILD_URL") or _load_saved_build_url() or "").strip().rstrip("/")
        tok = str(
            token
            or os.environ.get("LUMA_BUILD_ADMIN_TOKEN")
            or os.environ.get("LUMA_BUILD_TOKEN")
            or os.environ.get("LUMA_DEPLOY_TOKEN")
            or ""
        ).strip()
        ctx_insecure = False
        if not tok:
            tok, ctx_insecure = _load_luma_context_token(control_context)
        if not clean:
            raise BuildError("pass --url, set LUMA_BUILD_URL, or save once with 'luma-build use https://build.example.com'")
        if not tok:
            raise BuildError("not authenticated: run 'luma login' first, or pass --token / LUMA_DEPLOY_TOKEN")
        self.url = clean
        self.token = tok
        self.insecure = bool(insecure or ctx_insecure)

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [_NoRedirectHandler()]
        if self.insecure:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        return urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: int = 30) -> dict[str, Any]:
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
            raise BuildError(f"HTTP {exc.code}: {msg}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BuildError(f"cannot reach luma-build at {self.url}: {exc}") from exc

    def stream_post(self, path: str, body: dict[str, Any], *, timeout: int = 7200) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
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
                        print(f"[{item.get('stage')}] {item.get('message')}", flush=True)
                    elif item.get("type") == "done":
                        return item.get("build") or {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BuildError(f"HTTP {exc.code}: {detail}") from exc
        raise BuildError("build stream ended unexpectedly")


def _add_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default="", help="luma-build URL (or saved via 'luma-build use')")
    parser.add_argument("--token", default="", help="Token override (defaults to current 'luma login' context)")
    parser.add_argument("--control-context", default="", help="Named Luma login context to borrow token from")
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Print JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma-build", description="Decoupled Luma Build service & stateless BuildKit runner CLI")
    parser.add_argument("--version", action="version", version=f"luma-build {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve_p = sub.add_parser("serve", help="Run the luma-build HTTP queue & scheduling service")
    serve_p.add_argument("--host", default=os.environ.get("LUMA_BUILD_HOST", "0.0.0.0"))
    serve_p.add_argument("--port", type=int, default=int(os.environ.get("LUMA_BUILD_PORT", "8090")))
    serve_p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LUMA_BUILD_DATA_DIR", "/var/lib/luma-build")))

    use_p = sub.add_parser("use", help="Save default luma-build endpoint URL under ~/.config/luma/build.json")
    use_p.add_argument("endpoint", help="e.g. https://build.example.com")

    run_p = sub.add_parser("runner", help="Start a stateless BuildKit runner on Linux or macOS (OrbStack)")
    run_p.add_argument("--url", default=os.environ.get("LUMA_BUILD_URL", ""), help="luma-build server URL")
    run_p.add_argument("--token", default=os.environ.get("LUMA_RUNNER_TOKEN", ""), help="Runner token (lbr_...)")
    run_p.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("LUMA_RUNNER_STATE_DIR", "~/.luma-runner")),
        help="Directory for persistent local BuildKit builder metadata",
    )
    run_p.add_argument("--proxy", default=os.environ.get("LUMA_RUNNER_PROXY", ""), help="Optional local HTTP(S) proxy for git/base images")
    run_p.add_argument(
        "--cache-mode",
        choices=("", "read-write", "read-only", "local-only"),
        default=os.environ.get("LUMA_RUNNER_CACHE_MODE", ""),
        help="Override remote registry cache policy (use read-only on low-upload home runners)",
    )
    run_p.add_argument(
        "--cache-keep-gb",
        type=int,
        default=int(os.environ.get("LUMA_RUNNER_CACHE_KEEP_GB", "20")),
        help="Local BuildKit disk cache LRU cap in GiB (default: 20)",
    )
    run_p.add_argument(
        "--max-parallelism",
        type=int,
        default=int(os.environ.get("LUMA_RUNNER_MAX_PARALLELISM", "0")),
        help="Max parallel BuildKit OCI steps (0 = engine default)",
    )
    run_p.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("LUMA_RUNNER_WORKERS", "1")),
        help="Concurrent build worker slots on this runner host (default: 1)",
    )
    run_p.add_argument("--insecure", action="store_true", help="Skip TLS verification for server URL")
    run_p.add_argument("--once", action="store_true", help="Poll once, execute at most one task, and exit")

    status_p = sub.add_parser("status", help="Show luma-build service status, registry target, and runners")
    _add_client_args(status_p)

    rt_p = sub.add_parser("runner-token", help="Manage runner registration tokens")
    rt_sub = rt_p.add_subparsers(dest="rt_cmd", required=True)
    rt_create = rt_sub.add_parser("create", help="Issue a token for a new runner")
    rt_create.add_argument("name", help="Runner name, e.g. m4, air3, cloud-builder-1")
    rt_create.add_argument("--weight", type=int, default=100, help="Scheduling priority weight (higher = preferred when idle)")
    rt_create.add_argument("--concurrency", type=int, default=1, help="Max concurrent builds on this runner")
    rt_create.add_argument("--cache-mode", choices=("read-write", "read-only", "local-only"), default="read-write")
    rt_create.add_argument("--rotate", action="store_true", help="Rotate token if runner name already exists")
    _add_client_args(rt_create)

    rt_list = rt_sub.add_parser("list", help="List registered runners")
    _add_client_args(rt_list)

    rt_rev = rt_sub.add_parser("revoke", help="Revoke a runner token immediately")
    rt_rev.add_argument("name", help="Runner name or ID")
    _add_client_args(rt_rev)

    gc_p = sub.add_parser("git-cred", help="Manage server-stored Git pull credentials")
    gc_sub = gc_p.add_subparsers(dest="gc_cmd", required=True)
    gc_set = gc_sub.add_parser("set", help="Save a Git credential on luma-build")
    gc_set.add_argument("id", help="Credential ID, e.g. github:main or gitea:gcode")
    gc_set.add_argument("--host", required=True, help="Git hostname, e.g. github.com or git.example.com")
    gc_set.add_argument("--username", default="x-access-token")
    gc_set.add_argument("--path-prefix", default="", help="Optional org/owner prefix")
    gc_set.add_argument("--token-stdin", action="store_true", help="Read Git token from stdin")
    _add_client_args(gc_set)

    gc_list = gc_sub.add_parser("list", help="List saved Git credentials")
    _add_client_args(gc_list)

    gc_rm = gc_sub.add_parser("remove", help="Delete a saved Git credential")
    gc_rm.add_argument("id", help="Credential ID")
    _add_client_args(gc_rm)

    rc_p = sub.add_parser("registry-cred", help="Manage server-stored OCI Registry push credentials")
    rc_sub = rc_p.add_subparsers(dest="rc_cmd", required=True)
    rc_set = rc_sub.add_parser("set", help="Save a Registry credential (e.g. Aliyun ACR)")
    rc_set.add_argument("host", help="Registry host, e.g. crpi-xxx.cn-beijing.personal.cr.aliyuncs.com")
    rc_set.add_argument("--username", required=True)
    rc_set.add_argument("--password-stdin", action="store_true")
    rc_set.add_argument("--http", dest="insecure", action="store_true", default=None, help="Force plain HTTP")
    _add_client_args(rc_set)

    rc_list = rc_sub.add_parser("list", help="List saved Registry credentials")
    _add_client_args(rc_list)

    rc_rm = rc_sub.add_parser("remove", help="Delete a Registry credential")
    rc_rm.add_argument("host")
    _add_client_args(rc_rm)

    sub_p = sub.add_parser("submit", help="Submit a repository build (and optional Luma Control deployment)")
    sub_p.add_argument("source", help="Repository URL or owner/repo shorthand")
    sub_p.add_argument("--ref", default="main")
    sub_p.add_argument("--provider-id", default="", help="Saved Git credential ID")
    sub_p.add_argument("--platform", default="", help="Target platform, e.g. linux/amd64 or linux/arm64")
    sub_p.add_argument("--runner", default="", help="Pin a specific runner name")
    sub_p.add_argument("--compose-sidecar", default="", help="Path to luma.compose.yml in repo")
    sub_p.add_argument("--context", default="", help="Build context subpath")
    sub_p.add_argument("--dockerfile", default="", help="Dockerfile path")
    sub_p.add_argument("--target", default="", help="Dockerfile target stage")
    sub_p.add_argument("--region", default="", help="Override Luma region")
    sub_p.add_argument("--node", default="", help="Override Luma target deploy node")
    sub_p.add_argument("--exposure", default="", help="Override Luma exposure")
    sub_p.add_argument("--domain", default="", help="Override public domain")
    sub_p.add_argument("--port", type=int, default=None, help="Override container port")
    sub_p.add_argument("--env", dest="env_file", type=Path, default=None, help="Runtime .env forwarded only to Luma Control")
    sub_p.add_argument("--no-deploy", action="store_true", help="Build and push image only; do not call Luma Control")
    sub_p.add_argument("--no-cache", action="store_true", help="Disable BuildKit cache for this build")
    sub_p.add_argument("--detach", action="store_true", help="Return immediately after queueing")
    _add_client_args(sub_p)

    ls_p = sub.add_parser("list", help="List recent builds")
    ls_p.add_argument("--status", default="")
    ls_p.add_argument("--repo", default="")
    ls_p.add_argument("--limit", type=int, default=20)
    _add_client_args(ls_p)

    logs_p = sub.add_parser("logs", help="Show build details and logs")
    logs_p.add_argument("id", help="Build ID")
    _add_client_args(logs_p)

    can_p = sub.add_parser("cancel", help="Cancel a queued or running build")
    can_p.add_argument("id", help="Build ID")
    _add_client_args(can_p)

    ret_p = sub.add_parser("retry", help="Retry a previous build")
    ret_p.add_argument("id", help="Build ID")
    ret_p.add_argument("--detach", action="store_true")
    _add_client_args(ret_p)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except BuildError as exc:
        print(f"luma-build: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        app = create_app(data_dir=args.data_dir)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "runner":
        from .runner import RunnerDaemon

        if not args.url or not args.token:
            raise BuildError("runner requires --url (LUMA_BUILD_URL) and --token (LUMA_RUNNER_TOKEN)")
        daemon = RunnerDaemon(
            server_url=args.url,
            runner_token=args.token,
            state_dir=args.state_dir,
            local_proxy=args.proxy,
            cache_mode=args.cache_mode,
            cache_keep_gb=args.cache_keep_gb,
            max_parallelism=args.max_parallelism,
            workers=args.workers,
            insecure_tls=args.insecure,
        )
        return daemon.run_once_or_forever(once=args.once)

    if args.cmd == "use":
        saved_path = _save_build_url(args.endpoint)
        print(f"Saved default luma-build endpoint in {saved_path}: {args.endpoint.rstrip('/')}")
        return 0

    client = BuildApiClient(
        args.url,
        args.token,
        insecure=args.insecure,
        control_context=getattr(args, "control_context", ""),
    )

    if args.cmd == "status":
        res = client.request("GET", "/v1/status")
        if args.as_json:
            print(json.dumps(res, indent=2))
            return 0
        reg = res.get("registry") or {}
        ctl = res.get("controlTarget") or {}
        print(f"luma-build v{res.get('version')}")
        print(f"  Registry pull host : {reg.get('registryHost') or '-'}")
        print(f"  Registry push host : {reg.get('pushHost') or '-'}")
        print(f"  Registry namespace : {reg.get('namespace') or '-'} (flatten={reg.get('flatten')})")
        print(f"  Luma Control target: {ctl.get('endpoint') if ctl.get('configured') else 'disabled (build-only)'}")
        print(f"  Runners ({len(res.get('runners') or [])}):")
        for r in res.get("runners") or []:
            state = "REVOKED" if r.get("revoked") else ("online" if r.get("online") else "offline")
            print(
                f"    - {r['name']:<16} {state:<8} platform={r.get('nativePlatform') or '-':<12} "
                f"slots={r.get('activeBuilds', 0)}/{r.get('maxConcurrency', 1)} "
                f"weight={r.get('weight', 100)} cache={r.get('cacheMode')}"
            )
        return 0

    if args.cmd == "runner-token":
        if args.rt_cmd == "create":
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
            if args.as_json:
                print(json.dumps(res, indent=2))
            else:
                r = res["runner"]
                print(f"Runner registered: {r['name']} (weight={r['weight']}, cache={r['cacheMode']})")
                print(f"LUMA_RUNNER_TOKEN={r['runnerToken']}")
            return 0
        if args.rt_cmd == "list":
            res = client.request("GET", "/v1/runners")
            print(json.dumps(res["runners"], indent=2))
            return 0
        if args.rt_cmd == "revoke":
            res = client.request("POST", f"/v1/runners/{urllib.parse.quote(args.name, safe='')}/revoke", {})
            print(f"Revoked runner: {res['runner']['name']}")
            return 0

    if args.cmd == "git-cred":
        if args.gc_cmd == "set":
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
            print(json.dumps(res["credential"], indent=2) if args.as_json else f"Saved Git credential {args.id}")
            return 0
        if args.gc_cmd == "list":
            print(json.dumps(client.request("GET", "/v1/credentials/git")["credentials"], indent=2))
            return 0
        if args.gc_cmd == "remove":
            client.request("DELETE", f"/v1/credentials/git/{urllib.parse.quote(args.id, safe='')}")
            print(f"Removed Git credential {args.id}")
            return 0

    if args.cmd == "registry-cred":
        if args.rc_cmd == "set":
            pw = sys.stdin.read().strip() if args.password_stdin else getpass.getpass("Registry password: ").strip()
            res = client.request(
                "POST",
                "/v1/credentials/registry",
                {"host": args.host, "username": args.username, "password": pw, "insecure": args.insecure},
            )
            print(json.dumps(res["credential"], indent=2) if args.as_json else f"Saved Registry credential for {res['credential']['host']}")
            return 0
        if args.rc_cmd == "list":
            print(json.dumps(client.request("GET", "/v1/credentials/registry")["credentials"], indent=2))
            return 0
        if args.rc_cmd == "remove":
            client.request("DELETE", f"/v1/credentials/registry/{urllib.parse.quote(args.host, safe='')}")
            print(f"Removed Registry credential for {args.host}")
            return 0

    if args.cmd == "submit":
        body: dict[str, Any] = {
            "repoUrl": args.source if "://" in args.source or args.source.startswith("git@") else "",
            "repository": "" if ("://" in args.source or args.source.startswith("git@")) else args.source,
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
        if args.env_file:
            body["envSecrets"] = _parse_env_file(args.env_file)
        if args.detach:
            res = client.request("POST", "/v1/builds", body)
            print(json.dumps(res, indent=2) if args.as_json else f"Queued build: {res['build']['id']}")
            return 0
        final_build = client.stream_post("/v1/builds?stream=1", body)
        if args.as_json:
            print(json.dumps(final_build, indent=2))
        else:
            print(f"[{final_build.get('status')}] {final_build.get('id')}: {final_build.get('message')}")
            if (final_build.get("result") or {}).get("image"):
                print(f"Image: {final_build['result']['image']}")
        return 0 if final_build.get("status") == "succeeded" else 1

    if args.cmd == "list":
        qs = urllib.parse.urlencode({k: v for k, v in {"status": args.status, "repo": args.repo, "limit": args.limit}.items() if v})
        res = client.request("GET", f"/v1/builds?{qs}" if qs else "/v1/builds")
        if args.as_json:
            print(json.dumps(res, indent=2))
            return 0
        for b in res.get("builds") or []:
            print(f"{b['id']}  {b['status']:<13}  runner={b.get('runner') or '-':<12}  {b['repoSlug']}@{b['ref']}  {b.get('message')}")
        return 0

    if args.cmd == "logs":
        res = client.request("GET", f"/v1/builds/{urllib.parse.quote(args.id, safe='')}")
        b = res["build"]
        if args.as_json:
            print(json.dumps(b, indent=2))
            return 0
        print(f"Build {b['id']} ({b['status']}) on {b.get('runner') or '-'}: {b['repoSlug']}@{b['ref']}")
        for ev in b.get("events") or []:
            print(f"  [{ev['stage']}] {ev['message']}")
        return 0

    if args.cmd == "cancel":
        res = client.request("POST", f"/v1/builds/{urllib.parse.quote(args.id, safe='')}/cancel", {})
        print(f"Build {res['build']['id']}: {res['build']['status']}")
        return 0

    if args.cmd == "retry":
        path = f"/v1/builds/{urllib.parse.quote(args.id, safe='')}/retry"
        if args.detach:
            res = client.request("POST", path, {})
            print(f"Queued retry: {res['build']['id']}")
            return 0
        final_build = client.stream_post(f"{path}?stream=1", {})
        print(f"[{final_build.get('status')}] {final_build.get('id')}: {final_build.get('message')}")
        return 0 if final_build.get("status") == "succeeded" else 1

    return 0
