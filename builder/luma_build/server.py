"""Starlette ASGI server for the decoupled luma-build service."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from .browser_auth import BrowserAuth
from .dashboard import FAVICON_SVG, render_dashboard_html
from .deployer import (
    ControlAuthVerifier,
    ControlDeployClient,
    extract_manifest_target_hints,
)
from .errors import BuildError
from .scheduler import DEFAULT_AFFINITY_WAIT_SECONDS
from .store import TERMINAL_STATUSES, BuildStore

LOG = logging.getLogger("luma_build.server")
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024


def _parse_int(value: Any, *, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise BuildError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BuildError(f"{field} must be an integer, got {value!r}") from exc


def _parse_float(value: Any, *, field: str, default: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise BuildError(f"{field} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise BuildError(f"{field} must be a number, got {value!r}") from exc


_BASE_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(), focus-without-user-activation=()",
}


def _no_store_json(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_BASE_SECURITY_HEADERS)


class DeployWorker:
    """Concurrent worker pool that hands pushed immutable images to Luma Control."""

    def __init__(self, store: BuildStore, deployer: ControlDeployClient, *, workers: int = 4) -> None:
        self.store = store
        self.deployer = deployer
        self.workers = max(int(workers), 1)
        self.cond = threading.Condition()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        if any(t.is_alive() for t in self.threads):
            return
        self.store.recover_interrupted_deploys()
        self.stop_event.clear()
        self.threads = [
            threading.Thread(target=self._loop, name=f"luma-build-deployer-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for t in self.threads:
            t.start()

    def notify(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def stop(self) -> None:
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        for t in self.threads:
            t.join(timeout=3)
        self.threads = []

    def run_pending_once(self) -> bool:
        self.store.reap_stale_builds()
        claimed = self.store.claim_next_deploy()
        if not claimed:
            return False
        build_record, env_secrets = claimed
        build_id = str(build_record["id"])
        try:
            deploy_res = self.deployer.deploy_built_artifact(
                build_record=build_record,
                env_secrets=env_secrets,
                on_step=lambda line: self.store.append_events(
                    build_id, "deploy", [{"kind": "status", "message": line}]
                ),
            )
            self.store.complete_deploy(
                build_id,
                succeeded=True,
                deploy_result=deploy_res,
                message=f"Deployed {deploy_res.get('service') or deploy_res.get('deployment') or build_record['repoSlug']}",
            )
        except Exception as exc:
            self.store.complete_deploy(build_id, succeeded=False, message=str(exc))
        return True

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                while not self.stop_event.is_set() and self.run_pending_once():
                    pass
            except Exception:
                LOG.exception("luma-build deploy worker iteration error")
            with self.cond:
                if not self.stop_event.is_set():
                    self.cond.wait(timeout=2.0)


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _prom_escape(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_prometheus_metrics(snapshot: dict[str, Any]) -> str:
    lines = [
        "# HELP luma_build_info luma-build service version.",
        "# TYPE luma_build_info gauge",
        f'luma_build_info{{version="{_prom_escape(__version__)}"}} 1',
        "# HELP luma_build_runner_online Whether a registered runner reported a fresh heartbeat.",
        "# TYPE luma_build_runner_online gauge",
        "# HELP luma_build_runner_active_builds Active builds currently leased to each runner.",
        "# TYPE luma_build_runner_active_builds gauge",
        "# HELP luma_build_runner_max_concurrency Configured build slot capacity per runner.",
        "# TYPE luma_build_runner_max_concurrency gauge",
    ]
    for r in snapshot.get("runners") or []:
        if r.get("revoked"):
            continue
        labels = (
            f'runner="{_prom_escape(r["name"])}",'
            f'platform="{_prom_escape(r.get("nativePlatform") or "unknown")}",'
            f'cache_mode="{_prom_escape(r.get("cacheMode") or "read-write")}"'
        )
        lines.append(f"luma_build_runner_online{{{labels}}} {1 if r.get('online') else 0}")
        lines.append(f"luma_build_runner_active_builds{{{labels}}} {int(r.get('activeBuilds') or 0)}")
        lines.append(f"luma_build_runner_max_concurrency{{{labels}}} {int(r.get('maxConcurrency') or 1)}")

    lines.extend(
        [
            "# HELP luma_build_runs_count Number of build runs in each state.",
            "# TYPE luma_build_runs_count gauge",
        ]
    )
    counts = snapshot.get("buildCounts") or {}
    for st in ("queued", "building", "queued_deploy", "deploying", "succeeded", "failed", "canceled"):
        lines.append(f'luma_build_runs_count{{status="{st}"}} {int(counts.get(st) or 0)}')

    lines.extend(
        [
            "# HELP luma_build_queue_oldest_seconds Age in seconds of the oldest queued build.",
            "# TYPE luma_build_queue_oldest_seconds gauge",
            f"luma_build_queue_oldest_seconds {int(snapshot.get('oldestQueuedSeconds') or 0)}",
        ]
    )
    return "\n".join(lines) + "\n"


def create_app(
    *,
    data_dir: Path | None = None,
    admin_token: str | None = None,
    metrics_token: str | None = None,
    control_url: str | None = None,
    control_deploy_token: str | None = None,
    control_insecure: bool | None = None,
    default_registry_host: str | None = None,
    default_push_host: str | None = None,
    default_registry_namespace: str | None = None,
    default_registry_flatten: bool | None = None,
    affinity_wait_seconds: int | None = None,
    start_worker: bool = True,
) -> Starlette:
    resolved_dir = Path(data_dir or os.environ.get("LUMA_BUILD_DATA_DIR") or "/var/lib/luma-build")
    resolved_admin = (admin_token if admin_token is not None else os.environ.get("LUMA_BUILD_ADMIN_TOKEN") or "").strip()
    if resolved_admin and len(resolved_admin) < 16:
        raise BuildError("LUMA_BUILD_ADMIN_TOKEN must be at least 16 characters when set")
    resolved_control_url = (control_url if control_url is not None else os.environ.get("LUMA_CONTROL_URL") or "").strip()
    if not resolved_admin and not resolved_control_url:
        raise BuildError(
            "configure LUMA_CONTROL_URL (to reuse Luma login authentication) or set LUMA_BUILD_ADMIN_TOKEN"
        )
    resolved_insecure = (
        control_insecure
        if control_insecure is not None
        else os.environ.get("LUMA_CONTROL_INSECURE", "").strip().lower() in {"1", "true", "yes"}
    )
    resolved_metrics = (metrics_token if metrics_token is not None else os.environ.get("LUMA_BUILD_METRICS_TOKEN") or "").strip()

    raw_flatten = os.environ.get("LUMA_BUILD_REGISTRY_FLATTEN", "").strip().lower()
    env_flatten = True if raw_flatten in {"1", "true", "yes"} else (False if raw_flatten in {"0", "false", "no"} else None)

    resolved_affinity = (
        affinity_wait_seconds
        if affinity_wait_seconds is not None
        else _parse_int(
            os.environ.get("LUMA_BUILD_AFFINITY_SECONDS"),
            field="LUMA_BUILD_AFFINITY_SECONDS",
            default=DEFAULT_AFFINITY_WAIT_SECONDS,
        )
    )
    resolved_auth_ttl = _parse_float(
        os.environ.get("LUMA_BUILD_AUTH_CACHE_SECONDS"),
        field="LUMA_BUILD_AUTH_CACHE_SECONDS",
        default=60.0,
    )

    store = BuildStore(
        resolved_dir,
        default_registry_host=default_registry_host if default_registry_host is not None else os.environ.get("LUMA_BUILD_REGISTRY_HOST", ""),
        default_push_host=default_push_host if default_push_host is not None else os.environ.get("LUMA_BUILD_PUSH_HOST", ""),
        default_registry_namespace=default_registry_namespace if default_registry_namespace is not None else os.environ.get("LUMA_BUILD_REGISTRY_NAMESPACE", ""),
        default_registry_flatten=default_registry_flatten if default_registry_flatten is not None else env_flatten,
        affinity_wait_seconds=resolved_affinity,
    )
    store.recover_interrupted_deploys()

    auth_verifier = ControlAuthVerifier(
        resolved_control_url,
        insecure=resolved_insecure,
        ttl_seconds=resolved_auth_ttl,
    )
    browser_auth = BrowserAuth(auth_verifier)
    deployer = ControlDeployClient(
        resolved_control_url,
        control_deploy_token if control_deploy_token is not None else os.environ.get("LUMA_DEPLOY_TOKEN", ""),
        insecure=resolved_insecure,
    )
    worker = DeployWorker(store, deployer)
    lease_wakeup = asyncio.Event()

    def notify_lease_waiters() -> None:
        nonlocal lease_wakeup
        previous = lease_wakeup
        lease_wakeup = asyncio.Event()
        previous.set()

    def _is_valid_admin_token_sync(tok: str) -> bool:
        if not tok or tok.startswith("lbr_"):
            return False
        if resolved_admin and secrets.compare_digest(tok, resolved_admin):
            return True
        if auth_verifier.enabled and auth_verifier.verify(tok):
            return True
        return False

    async def require_admin(request: Request) -> None:
        if not request.headers.get("authorization"):
            await browser_auth.require_session(request)
            return
        tok = _bearer_token(request)
        ok = await run_in_threadpool(_is_valid_admin_token_sync, tok)
        if not ok:
            raise BuildError("unauthorized operator token", status_code=401)

    async def require_metrics(request: Request) -> None:
        tok = _bearer_token(request)
        if resolved_metrics and tok and secrets.compare_digest(tok, resolved_metrics):
            return
        ok = await run_in_threadpool(_is_valid_admin_token_sync, tok)
        if not ok:
            raise BuildError("unauthorized metrics token", status_code=401)

    async def json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BODY_BYTES:
            raise BuildError("request body exceeds 2 MiB limit", status_code=413)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise BuildError("request body must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise BuildError("request JSON body must be an object")
        return parsed

    def error_response(exc: BuildError) -> JSONResponse:
        return _no_store_json({"ok": False, "error": str(exc)}, status_code=exc.status_code)

    async def healthz(_request: Request) -> JSONResponse:
        return _no_store_json({"ok": True, "service": "luma-build", "version": __version__})

    async def dashboard_route(_request: Request) -> HTMLResponse:
        html_body, nonce = render_dashboard_html(version=__version__, control_sso=auth_verifier.enabled)
        csp = "; ".join(
            [
                "default-src 'self'",
                f"script-src 'nonce-{nonce}' 'strict-dynamic' 'report-sample'",
                f"style-src 'self' 'nonce-{nonce}'",
                "img-src 'self' data:",
                "connect-src 'self'",
                "object-src 'none'",
                "base-uri 'none'",
                "frame-ancestors 'none'",
                "form-action 'self'",
            ]
        )
        return HTMLResponse(
            html_body,
            headers={**_BASE_SECURITY_HEADERS, "Content-Security-Policy": csp},
        )

    async def favicon_route(_request: Request) -> Response:
        return Response(
            FAVICON_SVG,
            media_type="image/svg+xml",
            headers={**_BASE_SECURITY_HEADERS, "Cache-Control": "public, max-age=86400"},
        )

    async def metrics_endpoint(request: Request) -> Response:
        try:
            await require_metrics(request)
            snap = await run_in_threadpool(store.metrics_snapshot)
            return PlainTextResponse(
                _render_prometheus_metrics(snap),
                media_type="text/plain; version=0.0.4",
                headers={"Cache-Control": "no-store"},
            )
        except BuildError as exc:
            return error_response(exc)

    async def status_endpoint(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            snap = await run_in_threadpool(store.metrics_snapshot)
            return _no_store_json(
                {
                    "ok": True,
                    "version": __version__,
                    "registry": {
                        "registryHost": store.default_registry_host,
                        "pushHost": store.default_push_host,
                        "namespace": store.default_registry_namespace,
                        "flatten": bool(store.default_registry_namespace) if store.default_registry_flatten is None else store.default_registry_flatten,
                    },
                    "controlTarget": {
                        "configured": deployer.configured,
                        "endpoint": deployer.endpoint,
                        "ssoEnabled": auth_verifier.enabled,
                    },
                    "authMode": (
                        "control-sso+admin-token"
                        if (auth_verifier.enabled and resolved_admin)
                        else ("control-sso" if auth_verifier.enabled else "admin-token")
                    ),
                    "affinityWaitSeconds": store.affinity_wait_seconds,
                    "runners": snap["runners"],
                    "buildCounts": snap["buildCounts"],
                    "oldestQueuedSeconds": snap["oldestQueuedSeconds"],
                }
            )
        except BuildError as exc:
            return error_response(exc)

    async def runners_list(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            runners = await run_in_threadpool(store.list_runners)
            return _no_store_json({"ok": True, "runners": runners})
        except BuildError as exc:
            return error_response(exc)

    async def runners_create_token(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            body = await json_body(request)
            weight = _parse_int(body.get("weight"), field="weight", default=100)
            concurrency = _parse_int(body.get("maxConcurrency"), field="maxConcurrency", default=1)
            created = await run_in_threadpool(
                lambda: store.create_runner_token(
                    name=str(body.get("name") or ""),
                    weight=weight,
                    max_concurrency=concurrency,
                    cache_mode=str(body.get("cacheMode") or "read-write"),
                    labels=body.get("labels") if isinstance(body.get("labels"), dict) else None,
                    rotate=bool(body.get("rotate", False)),
                )
            )
            return _no_store_json({"ok": True, "runner": created})
        except BuildError as exc:
            return error_response(exc)

    async def runners_delete(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            name = str(request.path_params.get("name") or "")
            deleted = await run_in_threadpool(store.delete_runner, name)
            notify_lease_waiters()
            return _no_store_json({"ok": True, "runner": deleted})
        except BuildError as exc:
            return error_response(exc)

    async def git_creds_route(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            if request.method == "GET":
                creds = await run_in_threadpool(store.list_git_credentials)
                return _no_store_json({"ok": True, "credentials": creds})
            body = await json_body(request)
            saved = await run_in_threadpool(
                lambda: store.set_git_credential(
                    cred_id=str(body.get("id") or ""),
                    host=str(body.get("host") or ""),
                    token=str(body.get("token") or ""),
                    username=str(body.get("username") or "x-access-token"),
                    path_prefix=str(body.get("pathPrefix") or ""),
                )
            )
            return _no_store_json({"ok": True, "credential": saved})
        except BuildError as exc:
            return error_response(exc)

    async def git_creds_delete(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            cid = str(request.path_params.get("id") or "")
            deleted = await run_in_threadpool(store.delete_git_credential, cid)
            return _no_store_json({"ok": True, "deleted": deleted})
        except BuildError as exc:
            return error_response(exc)

    async def reg_creds_route(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            if request.method == "GET":
                creds = await run_in_threadpool(store.list_registry_credentials)
                return _no_store_json({"ok": True, "credentials": creds})
            body = await json_body(request)
            insecure = body["insecure"] if "insecure" in body and body["insecure"] is not None else None
            saved = await run_in_threadpool(
                lambda: store.set_registry_credential(
                    host=str(body.get("host") or ""),
                    username=str(body.get("username") or ""),
                    password=str(body.get("password") or ""),
                    insecure=insecure,
                )
            )
            return _no_store_json({"ok": True, "credential": saved})
        except BuildError as exc:
            return error_response(exc)

    async def reg_creds_delete(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            host = str(request.path_params.get("host") or "")
            deleted = await run_in_threadpool(store.delete_registry_credential, host)
            return _no_store_json({"ok": True, "deleted": deleted})
        except BuildError as exc:
            return error_response(exc)

    async def builds_route(request: Request) -> Response:
        try:
            await require_admin(request)
            if request.method == "GET":
                q = request.query_params
                limit = _parse_int(q.get("limit"), field="limit", default=30)
                runs = await run_in_threadpool(
                    lambda: store.list_builds(
                        limit=limit,
                        status=str(q.get("status") or ""),
                        repo=str(q.get("repo") or ""),
                    )
                )
                return _no_store_json({"ok": True, "builds": runs})

            body = await json_body(request)
            if "buildTimeout" in body and body["buildTimeout"] is not None:
                body["buildTimeout"] = _parse_int(body["buildTimeout"], field="buildTimeout", default=7200)
            if "port" in body and body["port"] is not None:
                body["port"] = _parse_int(body["port"], field="port", default=0)

            if not body.get("platform") and not body.get("platforms"):
                hints = extract_manifest_target_hints(str(body.get("manifest") or ""))
                if hints["platforms"]:
                    body["platforms"] = hints["platforms"]
                    body["_platformExplicit"] = True
                elif deployer.configured:
                    placement = await run_in_threadpool(deployer.cluster_placement_platforms)
                    body["_nodePlatforms"] = placement.get("nodes") or {}
                    body["_regionPlatforms"] = placement.get("regions") or {}
                    req_node = str(body.get("node") or "").strip()
                    req_region = str(body.get("region") or "").strip()
                    target_nodes = [req_node] if req_node else hints["nodes"]
                    target_regions = [] if target_nodes else ([req_region] if req_region else hints["regions"])
                    if target_nodes or target_regions:
                        inferred = deployer.infer_platforms_for_placement(
                            nodes=target_nodes,
                            regions=target_regions,
                            placement=placement,
                        )
                        if inferred:
                            body["platforms"] = inferred
                            body["_platformExplicit"] = True
            created = await run_in_threadpool(store.enqueue_build, body)
            notify_lease_waiters()
            if request.query_params.get("stream") in {"1", "true"}:
                return StreamingResponse(
                    _stream_build_ndjson(store, created["id"]),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-store"},
                )
            return _no_store_json({"ok": True, "build": created})
        except BuildError as exc:
            return error_response(exc)

    async def build_detail(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            bid = str(request.path_params.get("id") or "")
            after_seq = _parse_int(request.query_params.get("afterSeq"), field="afterSeq", default=0)
            rec = await run_in_threadpool(lambda: store.get_build(bid, after_seq=after_seq))
            return _no_store_json({"ok": True, "build": rec})
        except BuildError as exc:
            return error_response(exc)

    async def build_stream(request: Request) -> Response:
        try:
            await require_admin(request)
            bid = str(request.path_params.get("id") or "")
            await run_in_threadpool(lambda: store.get_build(bid, limit_events=1))
            return StreamingResponse(
                _stream_build_ndjson(store, bid),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store"},
            )
        except BuildError as exc:
            return error_response(exc)

    async def build_cancel(request: Request) -> JSONResponse:
        try:
            await require_admin(request)
            bid = str(request.path_params.get("id") or "")
            rec = await run_in_threadpool(store.cancel_build, bid)
            return _no_store_json({"ok": True, "build": rec})
        except BuildError as exc:
            return error_response(exc)

    async def build_retry(request: Request) -> Response:
        try:
            await require_admin(request)
            bid = str(request.path_params.get("id") or "")
            body = await json_body(request)
            env_secrets = body.get("envSecrets") if isinstance(body.get("envSecrets"), dict) else None
            retried = await run_in_threadpool(lambda: store.retry_build(bid, env_secrets=env_secrets))
            notify_lease_waiters()
            if request.query_params.get("stream") in {"1", "true"}:
                return StreamingResponse(
                    _stream_build_ndjson(store, retried["id"]),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-store"},
                )
            return _no_store_json({"ok": True, "build": retried})
        except BuildError as exc:
            return error_response(exc)

    # ------------------------------------------------------------------
    # Runner-only endpoints (authenticated strictly by lbr_... token)
    # ------------------------------------------------------------------

    async def runner_join(request: Request) -> JSONResponse:
        try:
            runner = await run_in_threadpool(store.join_runner, _bearer_token(request))
            return _no_store_json({"ok": True, "runner": runner})
        except BuildError as exc:
            return error_response(exc)

    async def runner_lease(request: Request) -> JSONResponse:
        try:
            tok = _bearer_token(request)
            body = await json_body(request)
            wait_secs = min(max(_parse_int(body.get("waitSeconds"), field="waitSeconds", default=0), 0), 25)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_secs
            while True:
                if await request.is_disconnected():
                    return JSONResponse({"ok": False, "error": "runner disconnected"}, status_code=499)
                wake_event = lease_wakeup
                leased = await run_in_threadpool(store.poll_and_lease, tok, body)
                if leased.get("task") is not None or loop.time() >= deadline:
                    return _no_store_json({"ok": True, **leased})
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake_event.wait(), timeout=min(1.5, deadline - loop.time()))
        except BuildError as exc:
            return error_response(exc)

    async def runner_heartbeat(request: Request) -> JSONResponse:
        try:
            tok = _bearer_token(request)
            body = await json_body(request)
            res = await run_in_threadpool(
                lambda: store.runner_heartbeat(
                    tok,
                    build_id=str(body.get("buildId") or ""),
                    lease_id=str(body.get("leaseId") or ""),
                )
            )
            return _no_store_json({"ok": True, **res})
        except BuildError as exc:
            return error_response(exc)

    async def runner_progress(request: Request) -> JSONResponse:
        try:
            tok = _bearer_token(request)
            body = await json_body(request)
            events = body.get("events") if isinstance(body.get("events"), list) else []
            res = await run_in_threadpool(
                lambda: store.runner_progress(
                    tok,
                    build_id=str(body.get("buildId") or ""),
                    events=events,
                    lease_id=str(body.get("leaseId") or ""),
                )
            )
            return _no_store_json({"ok": True, **res})
        except BuildError as exc:
            return error_response(exc)

    async def runner_complete(request: Request) -> JSONResponse:
        try:
            tok = _bearer_token(request)
            body = await json_body(request)
            res = await run_in_threadpool(
                lambda: store.runner_complete(
                    tok,
                    build_id=str(body.get("buildId") or ""),
                    status=str(body.get("status") or ""),
                    result=body.get("result") if isinstance(body.get("result"), dict) else None,
                    message=str(body.get("message") or ""),
                    lease_id=str(body.get("leaseId") or ""),
                )
            )
            if not res.get("replayed"):
                next_status = (res.get("build") or {}).get("status")
                if next_status == "queued_deploy":
                    worker.notify()
                elif next_status == "queued":
                    notify_lease_waiters()
            return _no_store_json({"ok": True, **res})
        except BuildError as exc:
            return error_response(exc)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            *browser_auth.routes(),
            Route("/", dashboard_route, methods=["GET"]),
            Route("/dashboard", dashboard_route, methods=["GET"]),
            Route("/favicon.svg", favicon_route, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/metrics", metrics_endpoint, methods=["GET"]),
            Route("/v1/status", status_endpoint, methods=["GET"]),
            Route("/v1/runners", runners_list, methods=["GET"]),
            Route("/v1/runners/tokens", runners_create_token, methods=["POST"]),
            Route("/v1/runners/{name}", runners_delete, methods=["DELETE"]),
            Route("/v1/runners/{name}/revoke", runners_delete, methods=["POST"]),
            Route("/v1/credentials/git", git_creds_route, methods=["GET", "POST"]),
            Route("/v1/credentials/git/{id}", git_creds_delete, methods=["DELETE"]),
            Route("/v1/credentials/registry", reg_creds_route, methods=["GET", "POST"]),
            Route("/v1/credentials/registry/{host:path}", reg_creds_delete, methods=["DELETE"]),
            Route("/v1/builds", builds_route, methods=["GET", "POST"]),
            Route("/v1/builds/{id}", build_detail, methods=["GET"]),
            Route("/v1/builds/{id}/stream", build_stream, methods=["GET"]),
            Route("/v1/builds/{id}/cancel", build_cancel, methods=["POST"]),
            Route("/v1/builds/{id}/retry", build_retry, methods=["POST"]),
            Route("/v1/runner/join", runner_join, methods=["POST"]),
            Route("/v1/runner/lease", runner_lease, methods=["POST"]),
            Route("/v1/runner/heartbeat", runner_heartbeat, methods=["POST"]),
            Route("/v1/runner/progress", runner_progress, methods=["POST"]),
            Route("/v1/runner/complete", runner_complete, methods=["POST"]),
        ],
    )
    app.state.store = store
    app.state.deployer = deployer
    app.state.deploy_worker = worker
    return app


async def _stream_build_ndjson(store: BuildStore, build_id: str) -> AsyncIterator[bytes]:
    after_seq = 0
    while True:
        record = await run_in_threadpool(lambda: store.get_build(build_id, after_seq=after_seq))
        for ev in record.get("events") or []:
            after_seq = max(after_seq, int(ev["seq"]))
            yield (json.dumps({"type": "event", **ev}, ensure_ascii=False) + "\n").encode("utf-8")
        if record["status"] in TERMINAL_STATUSES:
            yield (json.dumps({"type": "done", "build": record}, ensure_ascii=False) + "\n").encode("utf-8")
            return
        await asyncio.sleep(0.5)
