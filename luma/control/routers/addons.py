"""Addon catalog, lifecycle, and Luma Eye integration FastAPI routes."""

from __future__ import annotations

import asyncio
import functools
import json
import re
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from ...errors import LumaError
from ..schemas import (
    AddonApplyRequest,
    AddonConfigureRequest,
    AddonRenderRequest,
    AddonRollbackRequest,
    AddonUninstallRequest,
)
from ..dashboard_bundle import MAX_COMPRESSED_BYTES
from .deps import AuthenticatedAPIRoute, not_found_response, raw_binary_body, require_authenticated_token

ADDON_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

router = APIRouter(route_class=AuthenticatedAPIRoute, tags=["addons"])


def _valid_addon_id(addon_id: str) -> bool:
    return bool(ADDON_ID_RE.fullmatch(addon_id))


@router.get("/v1/addons", summary="List official addons and cluster configuration state")
async def list_addons(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import addons as control_addons, server

    return server._json_response(
        200, await run_in_threadpool(control_addons.handle_addons_list, token)
    )


@router.get("/v1/addons/summary", summary="Summarize active addons and capabilities")
async def get_addons_summary(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import addons as control_addons, server

    return server._json_response(
        200, await run_in_threadpool(control_addons.handle_addons_summary, token)
    )


@router.get("/v1/addons/{addon_id}", summary="Get details for a single addon")
async def get_addon(
    addon_id: str,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    return server._json_response(
        200, await run_in_threadpool(control_addons.handle_addon_get, token, addon_id)
    )


@router.post("/v1/addons/{addon_id}/configure", summary="Configure an addon and its scoped secrets")
async def configure_addon(
    addon_id: str,
    payload: AddonConfigureRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    return server._json_response(
        200,
        await run_in_threadpool(
            control_addons.handle_addon_configure, token, addon_id, payload.to_payload()
        ),
    )


@router.post("/v1/addons/{addon_id}/render", summary="Render Compose and sidecar manifests for an addon")
async def render_addon(
    addon_id: str,
    payload: AddonRenderRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    return server._json_response(
        200,
        await run_in_threadpool(
            control_addons.handle_addon_render, token, addon_id, payload.to_payload()
        ),
    )


@router.post("/v1/addons/{addon_id}/apply", summary="Deploy or update an addon stack")
async def apply_addon(
    addon_id: str,
    payload: AddonApplyRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    body = payload.to_payload()
    if await run_in_threadpool(control_addons.is_static_bundle_addon, addon_id):
        result = await run_in_threadpool(
            control_addons.handle_addon_bundle_apply, token, addon_id, body
        )
        return server._json_response(200, result)

    compose_body = await run_in_threadpool(
        control_addons.prepare_addon_apply_body, token, addon_id, body
    )
    return server._json_response(
        200, await run_in_threadpool(server.handle_compose_deployment, token, compose_body)
    )


@router.post("/v1/addons/{addon_id}/apply/stream", summary="Deploy or update an addon stack with NDJSON progress")
async def apply_addon_stream(
    addon_id: str,
    payload: AddonApplyRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    body = payload.to_payload()
    if await run_in_threadpool(control_addons.is_static_bundle_addon, addon_id):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        sentinel = object()

        def emit(status: str, name: str, message: str = "") -> None:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"status": status, "name": name, "message": message},
            )

        def worker() -> None:
            try:
                result = control_addons.handle_addon_bundle_apply(token, addon_id, body, emit=emit)
                loop.call_soon_threadsafe(queue.put_nowait, {"status": "done", "result": result})
            except LumaError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, {"status": "fail", "message": str(exc)})
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, {"status": "fail", "message": str(exc)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        async def generate() -> AsyncIterator[bytes]:
            task = asyncio.create_task(run_in_threadpool(worker))
            try:
                while True:
                    item = await queue.get()
                    if item is sentinel:
                        break
                    yield (json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            finally:
                await task

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    compose_body = await run_in_threadpool(
        control_addons.prepare_addon_apply_body, token, addon_id, body
    )
    return server._asgi_stream_deployment(token, compose_body, compose=True)


@router.post("/v1/addons/{addon_id}/upload", summary="Upload and hot-swap a static-bundle addon archive")
@raw_binary_body(MAX_COMPRESSED_BYTES)
async def upload_addon_bundle(
    addon_id: str,
    request: Request,
    version: str = Query(default="", description="Optional release version label"),
    source: str = Query(default="upload", description="Upload source label (e.g. cli, browser)"),
    sha256: str = Query(default="", description="Optional expected SHA-256 digest"),
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    raw_bytes = await request.body()
    result = await run_in_threadpool(
        functools.partial(
            control_addons.handle_addon_bundle_upload,
            token,
            addon_id,
            raw_bytes,
            version=str(version or ""),
            source=str(source or "upload"),
            expected_sha256=str(sha256 or ""),
        )
    )
    return server._json_response(200, result)


@router.post("/v1/addons/{addon_id}/rollback", summary="Roll back a static-bundle addon to a previous release")
async def rollback_addon_bundle(
    addon_id: str,
    payload: AddonRollbackRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    result = await run_in_threadpool(
        control_addons.handle_addon_bundle_rollback,
        token,
        addon_id,
        payload.to_payload(),
    )
    return server._json_response(200, result)


@router.get("/v1/addons/{addon_id}/check-update", summary="Check remote manifest for a newer static-bundle release")
async def check_addon_bundle_update(
    addon_id: str,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    result = await run_in_threadpool(
        control_addons.handle_addon_bundle_check_update,
        token,
        addon_id,
    )
    return server._json_response(200, result)


@router.post("/v1/addons/{addon_id}/uninstall", summary="Uninstall an addon and revoke its SSO sessions")
async def uninstall_addon(
    addon_id: str,
    payload: AddonUninstallRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not _valid_addon_id(addon_id):
        return not_found_response()
    from .. import addons as control_addons, server

    return server._json_response(
        200,
        await run_in_threadpool(
            control_addons.handle_addon_uninstall, token, addon_id, payload.to_payload()
        ),
    )


@router.get("/v1/dashboard/addons/eye/portfolio", summary="Fetch Luma Eye portfolio telemetry summary")
async def get_eye_portfolio(
    since: str = Query(default="24h", description="Lookback window (e.g. 24h, 7d)"),
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import addons as control_addons, server

    effective_since = str(since or "24h")
    return server._json_response(
        200,
        await run_in_threadpool(
            functools.partial(control_addons.handle_addon_eye_portfolio, token, since=effective_since)
        ),
    )


@router.get("/v1/dashboard/addons/eye/app", summary="Fetch Luma Eye telemetry for a single application")
async def get_eye_app(
    app: str = Query(default="", description="Luma application slug"),
    since: str = Query(default="24h", description="Lookback window (e.g. 24h, 7d)"),
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import addons as control_addons, server

    effective_since = str(since or "24h")
    return server._json_response(
        200,
        await run_in_threadpool(
            functools.partial(
                control_addons.handle_addon_eye_app, token, str(app or ""), since=effective_since
            )
        ),
    )
