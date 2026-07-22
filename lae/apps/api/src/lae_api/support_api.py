"""User support ticket API."""

from __future__ import annotations

from datetime import timezone
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lae_store import TenantScope
from lae_store.engine import create_session_factory
from lae_store.support_tickets import PostgresSupportTicketStore


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTicketRequest(StrictModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    errorCode: str | None = Field(default=None, max_length=96)
    operationId: str | None = Field(default=None, max_length=64)
    applicationId: str | None = Field(default=None, max_length=64)


def _timestamp(value: Any) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def create_support_router(
    get_store: Callable[[], PostgresSupportTicketStore],
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/support/tickets", status_code=201)
    async def create_ticket(
        request: Request,
        payload: CreateTicketRequest,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        principal = await request.app.state.require_scoped_principal(
            request,
            "apps:read",
            csrf_header=x_csrf_token,
            mutation=True,
        )
        user_id = getattr(principal, "user_id", None) or principal.credential_id
        try:
            ticket = await get_store().create(
                TenantScope(principal.tenant_id),
                user_id=str(user_id),
                subject=payload.subject,
                body=payload.body,
                error_code=payload.errorCode,
                operation_id=payload.operationId,
                application_id=payload.applicationId,
            )
        except ValueError as exc:
            return JSONResponse(
                {
                    "error": {
                        "code": "LAE_SUPPORT_TICKET_INVALID",
                        "message": str(exc) or "Invalid support ticket",
                        "retryable": False,
                        "details": {},
                    }
                },
                status_code=400,
            )
        response = JSONResponse(
            {
                "ticket": {
                    "id": ticket.id,
                    "subject": ticket.subject,
                    "status": ticket.status,
                    "errorCode": ticket.error_code,
                    "operationId": ticket.operation_id,
                    "applicationId": ticket.application_id,
                    "createdAt": _timestamp(ticket.created_at),
                }
            },
            status_code=201,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/v1/support/tickets")
    async def list_tickets(request: Request) -> JSONResponse:
        principal = await request.app.state.require_scoped_principal(
            request,
            "apps:read",
            mutation=False,
        )
        tickets = await get_store().list_for_tenant(TenantScope(principal.tenant_id))
        response = JSONResponse(
            {
                "tickets": [
                    {
                        "id": item.id,
                        "subject": item.subject,
                        "status": item.status,
                        "errorCode": item.error_code,
                        "operationId": item.operation_id,
                        "applicationId": item.application_id,
                        "createdAt": _timestamp(item.created_at),
                    }
                    for item in tickets
                ]
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def support_store_from_engine(engine: Any) -> PostgresSupportTicketStore:
    return PostgresSupportTicketStore(create_session_factory(engine))
