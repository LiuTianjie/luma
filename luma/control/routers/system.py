"""System, health, authenticated OpenAPI, and workflow FastAPI routes."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..schemas import WorkflowCheckRequest, WorkflowRecordRequest
from .deps import AuthenticatedAPIRoute, require_authenticated_token

router = APIRouter(tags=["system"])
_auth_router = APIRouter(route_class=AuthenticatedAPIRoute, tags=["system"])


@router.get("/v1/health", summary="Public Control plane health and capabilities")
async def get_health(request: Request) -> Response:
    from .. import server

    return await server._asgi_health(request)


@_auth_router.get("/v1/openapi.json", summary="Authenticated OpenAPI 3.1 schema")
async def get_openapi_schema(
    request: Request,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    await run_in_threadpool(
        lambda: server.require_token(server.load_auth_state(), token, token_type="deploy")
    )
    return JSONResponse(request.app.openapi(), headers={"Cache-Control": "no-store"})


@_auth_router.get("/v1/status", summary="Control plane cluster status")
async def get_status(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_control_status, token))


@_auth_router.get("/v1/control-resources", summary="List configured Control credentials and resources")
async def get_control_resources(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(
        200, await run_in_threadpool(server.handle_credentials_resources_list, token)
    )


@_auth_router.get("/v1/workflows", summary="List recorded deployment workflows")
async def list_workflows(token: str = Depends(require_authenticated_token)) -> Response:
    from .. import server

    return server._json_response(200, await run_in_threadpool(server.handle_workflow_list, token))


@_auth_router.post("/v1/workflows", summary="Record or update a deployment workflow")
async def record_workflow(
    payload: WorkflowRecordRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(server.handle_workflow_record, token, payload.to_payload()),
    )


@_auth_router.post("/v1/workflows/check", summary="Compare a requested deployment against recorded workflows")
async def check_workflow(
    payload: WorkflowCheckRequest,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(server.handle_workflow_check, token, payload.to_payload()),
    )


@_auth_router.get("/v1/workflows/{workflow_name}", summary="Fetch a recorded deployment workflow by name")
async def get_workflow(
    workflow_name: str,
    token: str = Depends(require_authenticated_token),
) -> Response:
    from .. import server

    return server._json_response(
        200,
        await run_in_threadpool(
            server.handle_workflow_get,
            token,
            urllib.parse.unquote(workflow_name),
        ),
    )


router.include_router(_auth_router)
