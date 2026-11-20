"""Modular FastAPI APIRouter registry for Luma Control."""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from . import addons, builder, settings, system
from .deps import register_exception_handlers

ROUTERS: Tuple[APIRouter, ...] = (
    system.router,
    addons.router,
    builder.router,
    settings.router,
)

_FASTAPI_SUPPORTS_STRICT_CONTENT_TYPE = (
    "strict_content_type" in inspect.signature(FastAPI.__init__).parameters
)

_DIRECT_ROUTER_APP: Optional[FastAPI] = None


def include_control_routers(app: FastAPI) -> None:
    """Mount all modular FastAPI routers and Luma exception handlers onto ``app``."""
    register_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)


def _get_direct_router_app() -> FastAPI:
    global _DIRECT_ROUTER_APP
    if _DIRECT_ROUTER_APP is None:
        kwargs: Dict[str, Any] = {
            "docs_url": None,
            "redoc_url": None,
            "openapi_url": None,
        }
        if _FASTAPI_SUPPORTS_STRICT_CONTENT_TYPE:
            kwargs["strict_content_type"] = False
        app = FastAPI(**kwargs)
        include_control_routers(app)
        _DIRECT_ROUTER_APP = app
    return _DIRECT_ROUTER_APP


async def dispatch_direct_request(request: Request) -> Optional[Response]:
    """Dispatch a bare Starlette Request (unit tests bypassing ``create_app``)
    through the migrated FastAPI routers if a migrated route matches."""
    app = _get_direct_router_app()
    matched = any(
        route.matches(request.scope)[0] == Match.FULL
        for route in app.router.routes
    )
    if not matched:
        return None

    scope = dict(request.scope)
    scope.setdefault("root_path", "")
    scope.setdefault("scheme", "http")
    scope["app"] = app

    status_code = 500
    raw_headers: List[Tuple[bytes, bytes]] = []
    body_chunks: List[bytes] = []

    async def send(message: Dict[str, Any]) -> None:
        nonlocal status_code, raw_headers
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            raw_headers = list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            chunk = message.get("body") or b""
            if chunk:
                body_chunks.append(chunk)

    await app(scope, request.receive, send)
    response = Response(b"".join(body_chunks), status_code=status_code)
    response.raw_headers = raw_headers
    return response
