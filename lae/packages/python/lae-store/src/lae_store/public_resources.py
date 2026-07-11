from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import ResourceNotFound
from .ids import require_opaque_id
from .models import Analysis, Operation, OperationEvent
from .repositories import OperationRecord, OperationStore, TenantScope
from .state import TERMINAL_OPERATION_STATUSES, OperationStatus
from .update_checks import UpdateCheckResult, public_update_check_from_operation


_PUBLIC_OPERATION_SCOPES = {
    "source.analyze": "analyses:write",
    "source.upload.scan": "sources:write",
}
_PUBLIC_OPERATION_KIND_PREFIX_SCOPES = {
    "deployment.": "deployments:write",
    "application.": "apps:write",
}

_PUBLIC_PHASES = frozenset(
    {
        "source.fetch",
        "source.upload",
        "source.upload.scan",
        "source.analyze",
        "analysis.topology",
        "analysis.policy",
        "build",
        "build.prepare",
        "build.execute",
        "deploy",
        "deploy.prepare",
        "deploy.apply",
        "verify",
        "application.lifecycle",
    }
)
_PUBLIC_STATUSES = frozenset(status.value for status in OperationStatus)
_PUBLIC_LEVELS = frozenset({"debug", "info", "warning", "error"})
_MAX_EVENT_CURSOR = (1 << 63) - 1
_PUBLIC_ERROR_CODE = re.compile(r"^LAE_[A-Z0-9_]{1,92}$")
_PUBLIC_EVENT_MESSAGES = {
    "operation.queued": "Operation queued",
    "operation.started": "Operation started",
    "operation.reclaimed": "Operation resumed after worker recovery",
    "operation.cancel-requested": "Operation cancellation requested",
    "operation.canceled": "Operation canceled",
    "operation.succeeded": "Operation succeeded",
    "operation.failed": "Operation failed",
    "operation.progress": "Operation progress updated",
    "builder.analyze.progress": "Source analysis updated",
    "compose.detected": "Application topology detected",
    "build.service.completed": "Service image build completed",
    "deployment.ready": "Deployment verification succeeded",
}


def required_scope_for_operation(kind: str) -> str:
    """Return the least-privilege public scope for a supported operation kind.

    Unknown kinds fail closed so adding a new worker operation never makes it
    readable or cancelable before its public authorization policy is chosen.
    """

    direct = _PUBLIC_OPERATION_SCOPES.get(kind)
    if direct is not None:
        return direct
    for prefix, scope in _PUBLIC_OPERATION_KIND_PREFIX_SCOPES.items():
        if kind.startswith(prefix):
            return scope
    raise ResourceNotFound("operation not found")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_event_data(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Copy only explicitly public, type-checked event metadata.

    In particular, image references, builder/Luma identifiers and cursors,
    credential leases, URLs and arbitrary stdout/stderr never cross this
    boundary even if a future internal producer persists them by mistake.
    """

    public: dict[str, Any] = {}
    if event_type == "builder.analyze.progress":
        replayed = data.get("replayed")
        if isinstance(replayed, bool):
            public["replayed"] = replayed
    elif event_type == "operation.progress":
        step = data.get("step")
        if isinstance(step, int) and not isinstance(step, bool) and step >= 0:
            public["step"] = step
    elif event_type == "compose.detected":
        for key in ("services", "routes", "volumes"):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                public[key] = value
    elif event_type == "build.service.completed":
        service = data.get("service")
        if (
            isinstance(service, str)
            and 1 <= len(service) <= 80
            and service.replace("-", "").replace("_", "").isalnum()
        ):
            public["service"] = service
    return public


@dataclass(frozen=True, slots=True)
class PublicOperationRecord:
    id: str
    kind: str
    status: str
    phase: str | None
    error_code: str | None
    cancel_requested: bool
    last_event_seq: int
    update_check: UpdateCheckResult | None = None

    @property
    def terminal(self) -> bool:
        return OperationStatus(self.status) in TERMINAL_OPERATION_STATUSES

    def public_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase if self.phase in _PUBLIC_PHASES else None,
            "cancelRequested": self.cancel_requested,
            "cursor": self.last_event_seq,
            "terminal": self.terminal,
            "links": {"events": f"/v1/operations/{self.id}/events"},
        }
        if (
            self.status == OperationStatus.FAILED.value
            and self.error_code
            and _PUBLIC_ERROR_CODE.fullmatch(self.error_code)
        ):
            # The worker's free-form error message is deliberately not copied.
            body["error"] = {
                "code": self.error_code,
                "message": "Operation failed",
            }
        if (
            self.kind == "application.check-update"
            and self.status == OperationStatus.SUCCEEDED.value
            and self.update_check is not None
        ):
            body["updateCheck"] = self.update_check.to_body()
        return body


@dataclass(frozen=True, slots=True)
class PublicAnalysisRecord:
    id: str
    operation_id: str
    status: str
    source_tree_digest: str | None
    source_snapshot_digest: str | None
    deployment_plan_digest: str | None
    build_plan_digest: str | None
    evidence_digest: str | None
    plan_stored: bool

    def public_body(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "digests": {
                "sourceTree": self.source_tree_digest,
                "sourceSnapshot": self.source_snapshot_digest,
                "deploymentPlan": self.deployment_plan_digest,
                "buildPlan": self.build_plan_digest,
                "evidence": self.evidence_digest,
            },
            "planStored": self.plan_stored,
            "links": {
                "operation": f"/v1/operations/{self.operation_id}",
                "events": f"/v1/operations/{self.operation_id}/events",
            },
        }


@dataclass(frozen=True, slots=True)
class PublicOperationEventRecord:
    event_id: str
    operation_id: str
    cursor: int
    type: str
    phase: str | None
    status: str
    level: str
    data: dict[str, Any]
    created_at: datetime

    def public_body(self) -> dict[str, Any]:
        event_type = (
            self.type if self.type in _PUBLIC_EVENT_MESSAGES else "operation.progress"
        )
        return {
            "eventId": self.event_id,
            "operationId": self.operation_id,
            "cursor": self.cursor,
            "type": event_type,
            "phase": self.phase if self.phase in _PUBLIC_PHASES else None,
            "status": self.status if self.status in _PUBLIC_STATUSES else "running",
            "level": self.level if self.level in _PUBLIC_LEVELS else "info",
            "message": _PUBLIC_EVENT_MESSAGES[event_type],
            "data": _public_event_data(event_type, self.data),
            "createdAt": _timestamp(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class PublicOperationEventPage:
    operation: PublicOperationRecord
    events: tuple[PublicOperationEventRecord, ...]
    cursor: int
    has_more: bool

    def public_body(self) -> dict[str, Any]:
        return {
            "operationId": self.operation.id,
            "events": [event.public_body() for event in self.events],
            "cursor": self.cursor,
            "status": self.operation.status,
            # A terminal operation can still have more retained events than
            # this page. Clients stop only after the terminal event is replayed.
            "terminal": self.operation.terminal and not self.has_more,
            "hasMore": self.has_more,
        }


def _public_operation(operation: Operation | OperationRecord) -> PublicOperationRecord:
    return PublicOperationRecord(
        id=operation.id,
        kind=operation.kind,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
        cancel_requested=operation.cancel_requested_at is not None,
        last_event_seq=operation.last_event_seq,
        update_check=public_update_check_from_operation(
            kind=operation.kind,
            status=operation.status,
            result=operation.result,
        ),
    )


class PostgresPublicResourceStore:
    """Tenant-fenced read/cancel view for the public LAE API."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._operations = OperationStore(sessions)

    async def get_operation(
        self, scope: TenantScope, operation_id: str
    ) -> PublicOperationRecord:
        require_opaque_id(operation_id, prefix="op")
        async with self._sessions() as session:
            operation = await session.scalar(
                select(Operation).where(
                    Operation.tenant_id == scope.tenant_id,
                    Operation.id == operation_id,
                )
            )
        if operation is None:
            raise ResourceNotFound("operation not found")
        return _public_operation(operation)

    async def get_analysis(
        self, scope: TenantScope, analysis_id: str
    ) -> PublicAnalysisRecord:
        require_opaque_id(analysis_id, prefix="ana")
        async with self._sessions() as session:
            analysis = await session.scalar(
                select(Analysis).where(
                    Analysis.tenant_id == scope.tenant_id,
                    Analysis.id == analysis_id,
                )
            )
        if analysis is None:
            raise ResourceNotFound("analysis not found")
        return PublicAnalysisRecord(
            id=analysis.id,
            operation_id=analysis.operation_id,
            status=analysis.status,
            source_tree_digest=analysis.source_tree_digest,
            source_snapshot_digest=analysis.source_snapshot_digest,
            deployment_plan_digest=analysis.deployment_plan_digest,
            build_plan_digest=analysis.build_plan_digest,
            evidence_digest=analysis.evidence_digest,
            plan_stored=analysis.plan_stored,
        )

    async def list_operation_events(
        self,
        scope: TenantScope,
        operation_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> PublicOperationEventPage:
        require_opaque_id(operation_id, prefix="op")
        if isinstance(after, bool) or not 0 <= after <= _MAX_EVENT_CURSOR:
            raise ValueError("event cursor is invalid")
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("event page limit is invalid")
        async with self._sessions() as session:
            async with session.begin():
                # FOR SHARE makes the operation row and its committed event
                # sequence one coherent snapshot without allowing a worker to
                # append/complete between these two reads.
                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == scope.tenant_id,
                        Operation.id == operation_id,
                    )
                    .with_for_update(read=True)
                )
                if operation is None:
                    raise ResourceNotFound("operation not found")
                rows = await session.scalars(
                    select(OperationEvent)
                    .where(
                        OperationEvent.tenant_id == scope.tenant_id,
                        OperationEvent.operation_id == operation_id,
                        OperationEvent.seq > after,
                    )
                    .order_by(OperationEvent.seq)
                    .limit(limit)
                )
                events = tuple(
                    PublicOperationEventRecord(
                        event_id=row.event_id,
                        operation_id=row.operation_id,
                        cursor=row.seq,
                        type=row.type,
                        phase=row.phase,
                        status=row.status,
                        level=row.level,
                        data=row.data,
                        created_at=row.created_at,
                    )
                    for row in rows
                )
                cursor = events[-1].cursor if events else after
                public_operation = _public_operation(operation)
                return PublicOperationEventPage(
                    operation=public_operation,
                    events=events,
                    cursor=cursor,
                    has_more=cursor < public_operation.last_event_seq,
                )

    async def request_cancel(
        self, scope: TenantScope, operation_id: str
    ) -> PublicOperationRecord:
        # OperationStore serializes this state transition under a row lock.
        # Repeated cancellation of queued, running or terminal operations is
        # therefore state-idempotent and does not require an idempotency row.
        operation = await self._operations.request_cancel(scope, operation_id)
        return _public_operation(operation)
