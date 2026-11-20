"""Independent Luma Build adapter FastAPI routes."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from ..schemas import BuilderSubmitRequest
from .deps import AuthenticatedAPIRoute, not_found_response, require_authenticated_token

BUILD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

router = APIRouter(route_class=AuthenticatedAPIRoute, tags=["builder"])


@router.get("/v1/builder/status", summary="Fetch independent Builder status and runners")
async def get_builder_status(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.independent_builder.status, token)
    )


@router.post("/v1/builder/builds", summary="Submit a repository build to the independent Builder")
async def submit_builder_build(
    payload: BuilderSubmitRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(server.independent_builder.submit, token, payload.to_payload()),
    )


@router.get("/v1/builder/builds/{build_id}", summary="Fetch independent Builder build status and log tail")
async def get_builder_build(
    build_id: str,
    afterSeq: str = Query(default="0", description="Return build events after this sequence number"),
    token: str = Depends(require_authenticated_token),
) -> Response:
    if not BUILD_ID_RE.fullmatch(build_id):
        return not_found_response()
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(server.independent_builder.detail, token, build_id, afterSeq),
    )
