"""Tenant-scoped support tickets for platform failure reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .ids import new_id, require_opaque_id
from .models import SupportTicket
from .repositories import TenantScope


@dataclass(frozen=True, slots=True)
class SupportTicketRecord:
    id: str
    subject: str
    body: str
    error_code: str | None
    operation_id: str | None
    application_id: str | None
    status: str
    created_at: datetime


class PostgresSupportTicketStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(
        self,
        scope: TenantScope,
        *,
        user_id: str,
        subject: str,
        body: str,
        error_code: str | None = None,
        operation_id: str | None = None,
        application_id: str | None = None,
    ) -> SupportTicketRecord:
        subject = subject.strip()
        body = body.strip()
        if not subject or len(subject) > 200:
            raise ValueError("subject is invalid")
        if not body or len(body) > 8000:
            raise ValueError("body is invalid")
        if error_code is not None and (
            not error_code.startswith("LAE_") or len(error_code) > 96
        ):
            raise ValueError("error code is invalid")
        if operation_id is not None:
            require_opaque_id(operation_id, prefix="op")
        if application_id is not None:
            require_opaque_id(application_id, prefix="app")
        ticket_id = new_id("tkt")
        async with self._sessions() as session:
            async with session.begin():
                row = SupportTicket(
                    id=ticket_id,
                    tenant_id=scope.tenant_id,
                    user_id=user_id,
                    subject=subject,
                    body=body,
                    error_code=error_code,
                    operation_id=operation_id,
                    application_id=application_id,
                    status="open",
                )
                session.add(row)
                await session.flush()
                return SupportTicketRecord(
                    id=row.id,
                    subject=row.subject,
                    body=row.body,
                    error_code=row.error_code,
                    operation_id=row.operation_id,
                    application_id=row.application_id,
                    status=row.status,
                    created_at=row.created_at,
                )

    async def list_for_tenant(
        self, scope: TenantScope, *, limit: int = 20
    ) -> list[SupportTicketRecord]:
        limit = max(1, min(50, int(limit)))
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(SupportTicket)
                    .where(SupportTicket.tenant_id == scope.tenant_id)
                    .order_by(SupportTicket.created_at.desc())
                    .limit(limit)
                )
            )
        return [
            SupportTicketRecord(
                id=row.id,
                subject=row.subject,
                body=row.body,
                error_code=row.error_code,
                operation_id=row.operation_id,
                application_id=row.application_id,
                status=row.status,
                created_at=row.created_at,
            )
            for row in rows
        ]
