from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Select, Update, and_, bindparam, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import (
    IdempotencyKeyReused,
    InvalidOperationTransition,
    LeaseLost,
    OperationConflict,
    ResourceNotFound,
)
from .ids import new_id, require_opaque_id
from .models import (
    APPLICATION_MUTATION_KINDS,
    Application,
    IdempotencyRecord,
    Operation,
    OperationEvent,
    OutboxEvent,
    SourceRevision,
)
from .security import ensure_persistable_payload, ensure_safe_message
from .state import (
    OperationStatus,
    OutboxStatus,
    cancellation_result,
    require_transition,
)

_KIND = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METHOD = re.compile(r"^[A-Z]{3,16}$")


@dataclass(frozen=True, slots=True)
class TenantScope:
    tenant_id: str

    def __post_init__(self) -> None:
        require_opaque_id(self.tenant_id, prefix="ten")


@dataclass(frozen=True, slots=True)
class Principal:
    type: str
    id: str

    def __post_init__(self) -> None:
        _require_kind(self.type, field="principal type", max_length=32)
        require_opaque_id(self.id)


@dataclass(frozen=True, slots=True)
class IdempotencyInput:
    key: str
    method: str
    route_template: str
    request_hash: bytes
    retention: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not 1 <= len(self.key) <= 255:
            raise ValueError("idempotency key must contain 1-255 characters")
        if any(char.isspace() for char in self.key):
            raise ValueError("idempotency key must not contain whitespace")
        if not isinstance(self.method, str) or not _METHOD.fullmatch(self.method):
            raise ValueError("idempotency method must be uppercase ASCII letters")
        if (
            not self.route_template.startswith("/")
            or "?" in self.route_template
            or "#" in self.route_template
            or len(self.route_template) > 255
            or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in self.route_template
            )
        ):
            raise ValueError(
                "route_template must be a normalized path without query/fragment"
            )
        if not isinstance(self.request_hash, bytes) or len(self.request_hash) != 32:
            raise ValueError("request_hash must be a 32-byte keyed digest")
        if self.retention < timedelta(hours=24):
            raise ValueError("idempotency retention must be at least 24 hours")


@dataclass(frozen=True, slots=True)
class CreateOperation:
    scope: TenantScope
    principal: Principal
    kind: str
    target_type: str
    target_id: str
    phase: str | None = None
    parent_operation_id: str | None = None
    idempotency: IdempotencyInput | None = None

    def __post_init__(self) -> None:
        _require_kind(self.kind, field="operation kind", max_length=80)
        _require_kind(self.target_type, field="target type", max_length=48)
        require_opaque_id(self.target_id)
        if self.parent_operation_id is not None:
            require_opaque_id(self.parent_operation_id, prefix="op")
        if self.phase is not None:
            _require_kind(self.phase, field="phase", max_length=80)
        if self.kind in APPLICATION_MUTATION_KINDS:
            if self.target_type != "application":
                raise ValueError("application mutation must target an application")
            require_opaque_id(self.target_id, prefix="app")


@dataclass(frozen=True, slots=True)
class EventInput:
    type: str
    phase: str | None
    status: str
    message: str
    data: dict[str, Any]
    level: str = "info"

    def __post_init__(self) -> None:
        _require_kind(self.type, field="event type", max_length=96)
        if self.phase is not None:
            _require_kind(self.phase, field="event phase", max_length=80)
        if self.level not in {"debug", "info", "warning", "error"}:
            raise ValueError("invalid event level")
        _require_kind(self.status, field="event status", max_length=24)
        ensure_safe_message(self.message)
        ensure_persistable_payload(self.data)


@dataclass(frozen=True, slots=True)
class OperationRecord:
    id: str
    tenant_id: str
    kind: str
    target_type: str
    target_id: str
    status: str
    phase: str | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    cancel_requested_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_attempt: int
    last_event_seq: int

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: str
    tenant_id: str
    event_type: str
    dedupe_key: str
    payload: dict[str, Any]
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None


def _require_kind(value: str, *, field: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or not _KIND.fullmatch(value)
    ):
        raise ValueError(f"{field} has invalid format")
    return value


def _require_worker_id(worker_id: str) -> str:
    if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
        raise ValueError("worker_id has invalid format")
    return worker_id


def _operation_record(operation: Operation) -> OperationRecord:
    return OperationRecord(
        id=operation.id,
        tenant_id=operation.tenant_id,
        kind=operation.kind,
        target_type=operation.target_type,
        target_id=operation.target_id,
        status=operation.status,
        phase=operation.phase,
        result=operation.result,
        error_code=operation.error_code,
        error_message=operation.error_message,
        cancel_requested_at=operation.cancel_requested_at,
        lease_owner=operation.lease_owner,
        lease_expires_at=operation.lease_expires_at,
        lease_attempt=operation.lease_attempt,
        last_event_seq=operation.last_event_seq,
    )


def tenant_application_statement(
    scope: TenantScope, application_id: str
) -> Select[tuple[Application]]:
    require_opaque_id(application_id, prefix="app")
    return select(Application).where(
        Application.tenant_id == scope.tenant_id,
        Application.id == application_id,
        Application.deleted_at.is_(None),
    )


def operation_claim_statement(kinds: Iterable[str]) -> Select[tuple[Operation]]:
    normalized = tuple(kinds)
    if not 1 <= len(normalized) <= 64:
        raise ValueError("operation kind claim set must contain 1-64 items")
    for kind in normalized:
        _require_kind(kind, field="operation kind", max_length=80)
    reclaimable = or_(
        Operation.status == OperationStatus.QUEUED.value,
        and_(
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_expires_at < func.now(),
        ),
    )
    return (
        select(Operation)
        .where(Operation.kind.in_(normalized), reclaimable)
        .order_by(Operation.created_at, Operation.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def outbox_claim_statement() -> Select[tuple[OutboxEvent]]:
    claimable = or_(
        and_(
            OutboxEvent.status == OutboxStatus.PENDING.value,
            OutboxEvent.available_at <= func.now(),
        ),
        and_(
            OutboxEvent.status == OutboxStatus.PUBLISHING.value,
            OutboxEvent.lease_expires_at < func.now(),
        ),
    )
    return (
        select(OutboxEvent)
        .where(claimable)
        .order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def application_mutation_lock_statement():
    """Return the static, parameterized PostgreSQL advisory-lock statement."""

    return select(
        func.pg_advisory_xact_lock(
            func.hashtextextended(bindparam("application_lock_key"), 0)
        )
    )


def event_sequence_statement(scope: TenantScope, operation_id: str) -> Update:
    """Atomically reserve the next per-operation event sequence."""

    require_opaque_id(operation_id, prefix="op")
    return (
        update(Operation)
        .where(
            Operation.tenant_id == scope.tenant_id,
            Operation.id == operation_id,
        )
        .values(last_event_seq=Operation.last_event_seq + 1, updated_at=func.now())
        .returning(Operation.last_event_seq)
    )


class TenantRepository:
    """Public tenant repository; every query is scope-bound at construction."""

    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    async def get_application(self, application_id: str) -> Application:
        application = await self._session.scalar(
            tenant_application_statement(self._scope, application_id)
        )
        if application is None:
            raise ResourceNotFound("application not found")
        return application

    async def get_source_revision(self, revision_id: str) -> SourceRevision:
        require_opaque_id(revision_id, prefix="src")
        revision = await self._session.scalar(
            select(SourceRevision).where(
                SourceRevision.tenant_id == self._scope.tenant_id,
                SourceRevision.id == revision_id,
                SourceRevision.deleted_at.is_(None),
            )
        )
        if revision is None:
            raise ResourceNotFound("source revision not found")
        return revision

    async def get_operation(self, operation_id: str) -> OperationRecord:
        require_opaque_id(operation_id, prefix="op")
        operation = await self._session.scalar(
            select(Operation).where(
                Operation.tenant_id == self._scope.tenant_id,
                Operation.id == operation_id,
            )
        )
        if operation is None:
            raise ResourceNotFound("operation not found")
        return _operation_record(operation)

    async def list_operation_events(
        self, operation_id: str, *, after: int = 0, limit: int = 200
    ) -> list[OperationEvent]:
        require_opaque_id(operation_id, prefix="op")
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("event cursor/limit is invalid")
        rows = await self._session.scalars(
            select(OperationEvent)
            .where(
                OperationEvent.tenant_id == self._scope.tenant_id,
                OperationEvent.operation_id == operation_id,
                OperationEvent.seq > after,
            )
            .order_by(OperationEvent.seq)
            .limit(limit)
        )
        return list(rows)


class OperationStore:
    """Transactional API for operation creation and worker coordination."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_operation(self, command: CreateOperation) -> OperationRecord:
        operation_id = new_id("op")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    now = await _db_now(session)
                    existing = await self._find_idempotency(
                        session, command, for_update=True
                    )
                    if existing is not None:
                        if existing.expires_at <= now:
                            await session.delete(existing)
                            await session.flush()
                        else:
                            return await self._replay_idempotency(
                                session, command, existing
                            )

                    if _is_application_mutation(command):
                        await session.execute(
                            application_mutation_lock_statement(),
                            {
                                "application_lock_key": (
                                    f"{command.scope.tenant_id}:{command.target_id}"
                                )
                            },
                        )

                    operation = Operation(
                        id=operation_id,
                        tenant_id=command.scope.tenant_id,
                        principal_type=command.principal.type,
                        principal_id=command.principal.id,
                        kind=command.kind,
                        target_type=command.target_type,
                        target_id=command.target_id,
                        status=OperationStatus.QUEUED.value,
                        phase=command.phase,
                        parent_operation_id=command.parent_operation_id,
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.queued",
                            phase=command.phase,
                            status=OperationStatus.QUEUED.value,
                            message="Operation queued",
                            data={},
                        ),
                    )
                    if command.idempotency is not None:
                        response_body = {
                            "operation": {
                                "id": operation.id,
                                "status": OperationStatus.QUEUED.value,
                            }
                        }
                        session.add(
                            IdempotencyRecord(
                                tenant_id=command.scope.tenant_id,
                                principal_type=command.principal.type,
                                principal_id=command.principal.id,
                                key=command.idempotency.key,
                                method=command.idempotency.method,
                                route_template=command.idempotency.route_template,
                                request_hash=command.idempotency.request_hash,
                                response_status=202,
                                response_body=response_body,
                                operation_id=operation.id,
                                expires_at=now + command.idempotency.retention,
                            )
                        )
                    await session.flush()
                    return _operation_record(operation)
        except IntegrityError as exc:
            # Concurrent first use of the same idempotency scope races on the
            # database unique constraint. Resolve only that exact record;
            # otherwise surface an application-mutation/constraint conflict.
            if command.idempotency is not None:
                async with self._sessions() as session:
                    existing = await self._find_idempotency(
                        session, command, for_update=False
                    )
                    if existing is not None:
                        return await self._replay_idempotency(
                            session, command, existing
                        )
            raise OperationConflict("an active operation already conflicts") from exc

    async def claim_next(
        self, *, worker_id: str, kinds: Iterable[str], lease_seconds: int = 60
    ) -> OperationRecord | None:
        _require_worker_id(worker_id)
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        statement = operation_claim_statement(kinds)
        async with self._sessions() as session:
            async with session.begin():
                # Drain a bounded number of expired, already-canceled leases so
                # a replacement worker never executes canceled user code.
                for _ in range(100):
                    operation = (await session.scalars(statement)).first()
                    if operation is None:
                        return None
                    now = await _db_now(session)
                    reclaimed = operation.status == OperationStatus.RUNNING.value
                    # A lifecycle action becomes authoritative once its
                    # external runtime mutation is durably marked. Reclaim it
                    # so the worker observes and commits that outcome; an
                    # automatic cancel here would strand Luma and the catalog
                    # in different states.
                    if (
                        reclaimed
                        and operation.cancel_requested_at is not None
                        and operation.phase != "application.lifecycle.runtime"
                    ):
                        require_transition(operation.status, OperationStatus.CANCELED)
                        operation.status = OperationStatus.CANCELED.value
                        operation.finished_at = now
                        operation.lease_owner = None
                        operation.lease_heartbeat_at = None
                        operation.lease_expires_at = None
                        operation.updated_at = now
                        await _append_event(
                            session,
                            operation,
                            EventInput(
                                type="operation.canceled",
                                phase=operation.phase,
                                status=OperationStatus.CANCELED.value,
                                message="Canceled operation recovered after worker loss",
                                data={},
                                level="warning",
                            ),
                        )
                        await session.flush()
                        continue
                    if reclaimed:
                        operation.lease_attempt += 1
                    else:
                        require_transition(operation.status, OperationStatus.RUNNING)
                        operation.status = OperationStatus.RUNNING.value
                        operation.started_at = now
                    operation.lease_owner = worker_id
                    operation.lease_heartbeat_at = now
                    operation.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    operation.updated_at = now
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.reclaimed"
                            if reclaimed
                            else "operation.started",
                            phase=operation.phase,
                            status=OperationStatus.RUNNING.value,
                            message="Operation lease reclaimed"
                            if reclaimed
                            else "Operation started",
                            data={"attempt": operation.lease_attempt},
                            level="warning" if reclaimed else "info",
                        ),
                    )
                    await session.flush()
                    return _operation_record(operation)
                return None

    async def heartbeat(
        self,
        scope: TenantScope,
        operation_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> OperationRecord:
        require_opaque_id(operation_id, prefix="op")
        _require_worker_id(worker_id)
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        async with self._sessions() as session:
            async with session.begin():
                now = await _db_now(session)
                operation = await session.scalar(
                    update(Operation)
                    .where(
                        Operation.tenant_id == scope.tenant_id,
                        Operation.id == operation_id,
                        Operation.status == OperationStatus.RUNNING.value,
                        Operation.lease_owner == worker_id,
                        Operation.lease_expires_at > now,
                    )
                    .values(
                        lease_heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                    .returning(Operation)
                )
                if operation is None:
                    raise LeaseLost("operation lease is no longer owned by worker")
                return _operation_record(operation)

    async def complete(
        self,
        scope: TenantScope,
        operation_id: str,
        *,
        worker_id: str,
        status: OperationStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> OperationRecord:
        require_opaque_id(operation_id, prefix="op")
        _require_worker_id(worker_id)
        if status not in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELED,
        }:
            raise InvalidOperationTransition(
                "worker completion status must be terminal"
            )
        if result is not None:
            ensure_persistable_payload(result)
        if error_code is not None:
            _require_kind(
                error_code.lower().replace("_", "."), field="error code", max_length=96
            )
        if error_message is not None:
            ensure_safe_message(error_message)
        if status is OperationStatus.FAILED and (
            error_code is None or error_message is None
        ):
            raise ValueError(
                "failed operations require a stable error code and safe message"
            )
        if status is not OperationStatus.FAILED and (
            error_code is not None or error_message is not None
        ):
            raise ValueError("only failed operations may persist error details")
        async with self._sessions() as session:
            async with session.begin():
                now = await _db_now(session)
                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == scope.tenant_id,
                        Operation.id == operation_id,
                    )
                    .with_for_update()
                )
                if operation is None:
                    raise ResourceNotFound("operation not found")
                if (
                    operation.status != OperationStatus.RUNNING.value
                    or operation.lease_owner != worker_id
                    or operation.lease_expires_at is None
                    or operation.lease_expires_at <= now
                ):
                    raise LeaseLost("operation lease is no longer owned by worker")
                effective = (
                    OperationStatus.CANCELED
                    if operation.cancel_requested_at is not None
                    else status
                )
                require_transition(operation.status, effective)
                operation.status = effective.value
                operation.result = (
                    result if effective is OperationStatus.SUCCEEDED else None
                )
                operation.error_code = (
                    error_code if effective is OperationStatus.FAILED else None
                )
                operation.error_message = (
                    error_message if effective is OperationStatus.FAILED else None
                )
                operation.finished_at = now
                operation.lease_owner = None
                operation.lease_heartbeat_at = None
                operation.lease_expires_at = None
                operation.updated_at = now
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type=f"operation.{effective.value}",
                        phase=operation.phase,
                        status=effective.value,
                        message=f"Operation {effective.value}",
                        data={},
                        level="error"
                        if effective is OperationStatus.FAILED
                        else "info",
                    ),
                )
                await session.flush()
                return _operation_record(operation)

    async def request_cancel(
        self, scope: TenantScope, operation_id: str
    ) -> OperationRecord:
        require_opaque_id(operation_id, prefix="op")
        async with self._sessions() as session:
            async with session.begin():
                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == scope.tenant_id,
                        Operation.id == operation_id,
                    )
                    .with_for_update()
                )
                if operation is None:
                    raise ResourceNotFound("operation not found")
                current = OperationStatus(operation.status)
                target, notify_worker = cancellation_result(current)
                if current is target and not notify_worker:
                    return _operation_record(operation)
                now = await _db_now(session)
                if notify_worker:
                    if operation.cancel_requested_at is not None:
                        return _operation_record(operation)
                    operation.cancel_requested_at = now
                    event_type = "operation.cancel-requested"
                    message = "Operation cancellation requested"
                else:
                    require_transition(current, target)
                    operation.status = target.value
                    operation.cancel_requested_at = now
                    operation.finished_at = now
                    event_type = "operation.canceled"
                    message = "Operation canceled before execution"
                operation.updated_at = now
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type=event_type,
                        phase=operation.phase,
                        status=operation.status,
                        message=message,
                        data={},
                    ),
                )
                await session.flush()
                return _operation_record(operation)

    async def append_event(
        self,
        scope: TenantScope,
        operation_id: str,
        event: EventInput,
        *,
        worker_id: str,
    ) -> int:
        require_opaque_id(operation_id, prefix="op")
        _require_worker_id(worker_id)
        async with self._sessions() as session:
            async with session.begin():
                now = await _db_now(session)
                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == scope.tenant_id,
                        Operation.id == operation_id,
                        Operation.status == OperationStatus.RUNNING.value,
                        Operation.lease_owner == worker_id,
                        Operation.lease_expires_at > now,
                    )
                    .with_for_update()
                )
                if operation is None:
                    raise LeaseLost("operation lease is no longer owned by worker")
                if event.phase is not None:
                    operation.phase = event.phase
                return await _append_event(session, operation, event)

    async def _find_idempotency(
        self, session: AsyncSession, command: CreateOperation, *, for_update: bool
    ) -> IdempotencyRecord | None:
        if command.idempotency is None:
            return None
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == command.scope.tenant_id,
            IdempotencyRecord.principal_type == command.principal.type,
            IdempotencyRecord.principal_id == command.principal.id,
            IdempotencyRecord.method == command.idempotency.method,
            IdempotencyRecord.route_template == command.idempotency.route_template,
            IdempotencyRecord.key == command.idempotency.key,
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def _replay_idempotency(
        self,
        session: AsyncSession,
        command: CreateOperation,
        existing: IdempotencyRecord,
    ) -> OperationRecord:
        assert command.idempotency is not None
        if existing.request_hash != command.idempotency.request_hash:
            raise IdempotencyKeyReused("idempotency key was used for another request")
        operation = await session.scalar(
            select(Operation).where(
                Operation.tenant_id == command.scope.tenant_id,
                Operation.id == existing.operation_id,
            )
        )
        if operation is None:
            raise ResourceNotFound("idempotent operation no longer exists")
        return _operation_record(operation)


class OutboxStore:
    """Dedicated internal publisher API; tenant APIs never receive this handle."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim_next(
        self, *, worker_id: str, lease_seconds: int = 30
    ) -> OutboxRecord | None:
        _require_worker_id(worker_id)
        if not 5 <= lease_seconds <= 600:
            raise ValueError("outbox lease_seconds must be between 5 and 600")
        async with self._sessions() as session:
            async with session.begin():
                event = (await session.scalars(outbox_claim_statement())).first()
                if event is None:
                    return None
                now = await _db_now(session)
                event.status = OutboxStatus.PUBLISHING.value
                event.lease_owner = worker_id
                event.lease_expires_at = now + timedelta(seconds=lease_seconds)
                event.attempts += 1
                event.updated_at = now
                await session.flush()
                return _outbox_record(event)

    async def mark_published(self, outbox_id: str, *, worker_id: str) -> None:
        require_opaque_id(outbox_id, prefix="out")
        _require_worker_id(worker_id)
        async with self._sessions() as session:
            async with session.begin():
                event, now = await self._locked_owned_event(
                    session, outbox_id, worker_id
                )
                event.status = OutboxStatus.PUBLISHED.value
                event.published_at = now
                event.lease_owner = None
                event.lease_expires_at = None
                event.updated_at = now

    async def mark_failed(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        error: str,
        retry_after: timedelta,
        max_attempts: int = 20,
    ) -> None:
        require_opaque_id(outbox_id, prefix="out")
        _require_worker_id(worker_id)
        ensure_safe_message(error)
        if retry_after < timedelta(seconds=1) or retry_after > timedelta(hours=24):
            raise ValueError("retry_after is outside the allowed range")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts is outside the allowed range")
        async with self._sessions() as session:
            async with session.begin():
                event, now = await self._locked_owned_event(
                    session, outbox_id, worker_id
                )
                event.status = (
                    OutboxStatus.DEAD.value
                    if event.attempts >= max_attempts
                    else OutboxStatus.PENDING.value
                )
                event.available_at = now + retry_after
                event.last_error = error
                event.lease_owner = None
                event.lease_expires_at = None
                event.updated_at = now

    async def _locked_owned_event(
        self, session: AsyncSession, outbox_id: str, worker_id: str
    ) -> tuple[OutboxEvent, datetime]:
        now = await _db_now(session)
        event = await session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == outbox_id,
                OutboxEvent.status == OutboxStatus.PUBLISHING.value,
                OutboxEvent.lease_owner == worker_id,
                OutboxEvent.lease_expires_at > now,
            )
            .with_for_update()
        )
        if event is None:
            raise LeaseLost("outbox lease is no longer owned by worker")
        return event, now


def _is_application_mutation(command: CreateOperation) -> bool:
    return (
        command.target_type == "application"
        and command.kind in APPLICATION_MUTATION_KINDS
    )


async def _db_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.now()))
    if value is None:
        # Only useful to lightweight fakes; PostgreSQL always returns a value.
        return datetime.now(timezone.utc)
    return value


async def _append_event(
    session: AsyncSession, operation: Operation, event: EventInput
) -> int:
    seq = await session.scalar(
        event_sequence_statement(TenantScope(operation.tenant_id), operation.id)
    )
    if seq is None:
        raise ResourceNotFound("operation not found while appending event")
    event_id = new_id("evt")
    session.add(
        OperationEvent(
            operation_id=operation.id,
            tenant_id=operation.tenant_id,
            seq=seq,
            event_id=event_id,
            type=event.type,
            phase=event.phase,
            status=event.status,
            level=event.level,
            message=event.message,
            data=event.data,
        )
    )
    outbox_payload = {
        "eventId": event_id,
        "operationId": operation.id,
        "seq": seq,
        "type": event.type,
        "phase": event.phase,
        "status": event.status,
    }
    ensure_persistable_payload(outbox_payload)
    session.add(
        OutboxEvent(
            tenant_id=operation.tenant_id,
            aggregate_type="operation",
            aggregate_id=operation.id,
            event_type=event.type,
            dedupe_key=f"operation:{operation.id}:{seq}",
            payload=outbox_payload,
        )
    )
    operation.last_event_seq = seq
    return seq


def _outbox_record(event: OutboxEvent) -> OutboxRecord:
    return OutboxRecord(
        id=event.id,
        tenant_id=event.tenant_id,
        event_type=event.event_type,
        dedupe_key=event.dedupe_key,
        payload=event.payload,
        attempts=event.attempts,
        lease_owner=event.lease_owner,
        lease_expires_at=event.lease_expires_at,
    )
