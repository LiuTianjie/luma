"""Shared FastAPI route classes, auth dependencies, and Luma error mapping."""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Coroutine

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from ...errors import LumaError
from .. import dashboard_auth
from ..logs import LogUnavailable

_APIROUTE_SUPPORTS_STRICT_CONTENT_TYPE = (
    "strict_content_type" in inspect.signature(APIRoute.__init__).parameters
)


def format_validation_error(exc: RequestValidationError) -> str:
    """Turn Pydantic/FastAPI validation errors into a concise legacy-CLI message."""
    parts = []
    for err in exc.errors():
        loc = [str(item) for item in (err.get("loc") or ()) if item != "body"]
        msg = str(err.get("msg") or "invalid request value")
        if loc:
            parts.append(f"{'.'.join(loc)}: {msg}")
        else:
            parts.append(msg)
    return "; ".join(parts) if parts else "invalid request payload"


def error_response_for_exception(exc: Exception) -> Response:
    """Map control-plane exceptions to exact Luma HTTP status codes and payloads."""
    from .. import server

    if isinstance(exc, RequestValidationError):
        return server._asgi_error(400, LumaError(format_validation_error(exc)), code="luma_error")
    if isinstance(exc, dashboard_auth.AuthError):
        response = server._asgi_error(exc.status, exc, code=exc.code)
        if exc.status == 429:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Retry-After"] = "60"
        return response
    if isinstance(exc, LogUnavailable):
        return server._asgi_error(503, exc, code="logs_unavailable")
    if isinstance(exc, LumaError):
        message = str(exc)
        status = 401 if message == "unauthorized" or "bearer token" in message else 400
        return server._asgi_error(status, exc, code="luma_error")
    return server._asgi_error(500, exc, code="internal_error")


def not_found_response() -> Response:
    """Return the standard Luma 404 JSON error payload."""
    from .. import server

    return server._json_response(
        404,
        server._error_payload("not_found", "not found", request_id=server._request_id()),
    )


async def require_authenticated_token(request: Request) -> str:
    """Resolve the request's Bearer token or Dashboard session credential."""
    cached = getattr(request.state, "luma_token", None)
    if isinstance(cached, str) and cached:
        return cached
    from .. import server

    token = await server._request_credential(request)
    request.state.luma_token = token
    return token


class AuthenticatedAPIRoute(APIRoute):
    """FastAPI route preserving auth-before-parse, streaming body-size limits,
    old CLI Content-Type tolerance, and exact Luma error envelopes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if _APIROUTE_SUPPORTS_STRICT_CONTENT_TYPE:
            kwargs.setdefault("strict_content_type", False)
        super().__init__(*args, **kwargs)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            from .. import server

            try:
                token = await server._request_credential(request)
                request.state.luma_token = token
                if request.method in {"POST", "PUT", "PATCH"}:
                    await run_in_threadpool(server._prevalidate_post_token, request.url.path, token)
                    body = await server._read_json_body(
                        request,
                        max_bytes=server.MAX_AUTHENTICATED_BODY_BYTES,
                        allow_empty=False,
                    )
                    request.state.luma_body = body
                    request._body = json.dumps(
                        body, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                return await original_handler(request)
            except Exception as exc:
                return error_response_for_exception(exc)

        return custom_route_handler


def register_exception_handlers(app: FastAPI) -> None:
    """Register Luma exception handlers on the FastAPI application."""

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> Response:
        return error_response_for_exception(exc)

    @app.exception_handler(dashboard_auth.AuthError)
    async def _handle_auth_error(_request: Request, exc: dashboard_auth.AuthError) -> Response:
        return error_response_for_exception(exc)

    @app.exception_handler(LogUnavailable)
    async def _handle_log_unavailable(_request: Request, exc: LogUnavailable) -> Response:
        return error_response_for_exception(exc)

    @app.exception_handler(LumaError)
    async def _handle_luma_error(_request: Request, exc: LumaError) -> Response:
        return error_response_for_exception(exc)
