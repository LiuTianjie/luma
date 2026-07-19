from __future__ import annotations

import copy
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ._codec import (
    TASK_STATUSES,
    TERMINAL_STATUSES,
    canonical_hash_input,
    safe_result,
    task_request_body,
    validate_context,
    validate_event_query,
    validate_idempotency_key,
    validate_limits,
    validate_principal,
    validate_task_id,
)
from .errors import AdapterErrorCode, LumaAdapterError
from .models import (
    AnalyzeSourceRequest,
    BuilderTask,
    BuilderTaskEvent,
    BuilderTaskEventPage,
    BuilderTaskMutation,
    BuildPlanRequest,
    LumaCallContext,
    ServicePrincipal,
)

_STATUS_MESSAGES = {
    "queued": "Builder task queued.",
    "running": "Builder task running.",
    "cancel_requested": "Builder task cancellation requested.",
    "canceled": "Builder task canceled.",
    "succeeded": "Builder task succeeded.",
    "failed": "Builder task failed.",
    "timed_out": "Builder task timed out.",
}


@dataclass(slots=True)
class _TaskRecord:
    task_id: str
    owner_principal_id: str
    context: LumaCallContext
    kind: str
    request_hash: str
    status: str
    created_at: int
    updated_at: int
    started_at: int = 0
    completed_at: int = 0
    result: dict[str, Any] | None = None
    events: list[BuilderTaskEvent] = field(default_factory=list)
    next_cursor: int = 1


class FakeLuma:
    """Shared in-memory Luma backend for deterministic worker tests."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._principals: dict[str, str] = {}
        self._tasks: dict[str, _TaskRecord] = {}
        self._idempotency: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        self._counter = 0

    def bind(self, principal: ServicePrincipal) -> "FakeLumaBuilderAdapter":
        validate_principal(principal.principal_id, principal.token)
        with self._lock:
            existing = self._principals.get(principal.principal_id)
            if existing is not None and not secrets.compare_digest(
                existing, principal.token
            ):
                raise LumaAdapterError(AdapterErrorCode.UNAUTHORIZED)
            self._principals[principal.principal_id] = principal.token
        return FakeLumaBuilderAdapter(self, principal)

    def start_task(self, task_id: str) -> BuilderTask:
        with self._lock:
            record = self._record(task_id)
            if record.status == "queued":
                now = self._now()
                record.status = "running"
                record.started_at = now
                record.updated_at = now
                self._append_event(record, "status", status="running")
            return self._task_view(record)

    def complete_task(
        self,
        task_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
    ) -> BuilderTask:
        if status not in {"canceled", "succeeded", "failed", "timed_out"}:
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        with self._lock:
            record = self._record(task_id)
            if record.status in TERMINAL_STATUSES:
                return self._task_view(record)
            if record.status == "cancel_requested":
                status = "canceled"
                result = None
            if status == "succeeded" and result is None:
                raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
            stored_result = (
                safe_result(
                    record.kind,
                    dict(result or {}),
                    require_complete=status == "succeeded",
                )
                if result is not None
                else None
            )
            now = self._now()
            record.status = status
            record.updated_at = now
            record.completed_at = now
            record.result = stored_result
            self._append_event(record, "status", status=status)
            return self._task_view(record)

    def emit_event(
        self,
        task_id: str,
        event_type: str,
        *,
        status: str | None = None,
    ) -> BuilderTaskEvent:
        with self._lock:
            record = self._record(task_id)
            return self._append_event(record, event_type, status=status)

    def trim_events_before(self, task_id: str, cursor: int) -> None:
        with self._lock:
            record = self._record(task_id)
            record.events = [event for event in record.events if event.cursor >= cursor]

    def _authenticate(self, principal: ServicePrincipal) -> str:
        with self._lock:
            expected = self._principals.get(principal.principal_id)
            if expected is None or not secrets.compare_digest(
                expected, principal.token
            ):
                raise LumaAdapterError(AdapterErrorCode.UNAUTHORIZED)
        return principal.principal_id

    def _create(
        self,
        principal: ServicePrincipal,
        context: LumaCallContext,
        *,
        kind: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        principal_id = self._authenticate(principal)
        key = validate_idempotency_key(idempotency_key)
        body = task_request_body(context, kind=kind, payload=payload)
        request_hash = hashlib.sha256(
            canonical_hash_input(body).encode("utf-8")
        ).hexdigest()
        scope = (principal_id, context.tenant_ref, context.application_ref, key)
        with self._lock:
            existing = self._idempotency.get(scope)
            if existing:
                existing_hash, task_id = existing
                if existing_hash != request_hash:
                    raise LumaAdapterError(
                        AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                    )
                return BuilderTaskMutation(
                    self._task_view(self._tasks[task_id]), replayed=True
                )
            now = self._now()
            self._counter += 1
            task_id = f"builder-fake-{self._counter:08d}"
            record = _TaskRecord(
                task_id=task_id,
                owner_principal_id=principal_id,
                context=context,
                kind=kind,
                request_hash=request_hash,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            self._append_event(record, "status", status="queued")
            self._tasks[task_id] = record
            self._idempotency[scope] = (request_hash, task_id)
            return BuilderTaskMutation(self._task_view(record), replayed=False)

    def _get(
        self, principal: ServicePrincipal, context: LumaCallContext, task_id: str
    ) -> BuilderTask:
        principal_id = self._authenticate(principal)
        validate_context(context)
        with self._lock:
            record = self._owned_record(
                principal_id, context, validate_task_id(task_id)
            )
            return self._task_view(record)

    def _events(
        self,
        principal: ServicePrincipal,
        context: LumaCallContext,
        task_id: str,
        *,
        after: int,
        limit: int,
    ) -> BuilderTaskEventPage:
        principal_id = self._authenticate(principal)
        validate_context(context)
        validate_event_query(after, limit)
        with self._lock:
            record = self._owned_record(
                principal_id, context, validate_task_id(task_id)
            )
            oldest = record.events[0].cursor if record.events else record.next_cursor
            if after and record.events and after < oldest - 1:
                raise LumaAdapterError(AdapterErrorCode.CURSOR_EXPIRED, http_status=410)
            remaining = [event for event in record.events if event.cursor > after]
            page = tuple(copy.deepcopy(remaining[:limit]))
            next_cursor = page[-1].cursor if page else after
            return BuilderTaskEventPage(
                task_id=record.task_id,
                status=record.status,
                events=page,
                next_cursor=next_cursor,
                oldest_cursor=oldest,
                has_more=len(remaining) > len(page),
                terminal=record.status in TERMINAL_STATUSES,
            )

    def _cancel(
        self,
        principal: ServicePrincipal,
        context: LumaCallContext,
        task_id: str,
    ) -> BuilderTaskMutation:
        principal_id = self._authenticate(principal)
        validate_context(context)
        with self._lock:
            record = self._owned_record(
                principal_id, context, validate_task_id(task_id)
            )
            if (
                record.status in TERMINAL_STATUSES
                or record.status == "cancel_requested"
            ):
                return BuilderTaskMutation(self._task_view(record), replayed=True)
            now = self._now()
            if record.status == "queued":
                record.status = "canceled"
                record.completed_at = now
            else:
                record.status = "cancel_requested"
            record.updated_at = now
            self._append_event(record, "status", status=record.status)
            return BuilderTaskMutation(self._task_view(record), replayed=False)

    def _owned_record(
        self, principal_id: str, context: LumaCallContext, task_id: str
    ) -> _TaskRecord:
        record = self._tasks.get(task_id)
        if (
            record is None
            or record.owner_principal_id != principal_id
            or record.context.tenant_ref != context.tenant_ref
            or record.context.application_ref != context.application_ref
            or record.context.external_operation_id != context.external_operation_id
        ):
            # Cross-principal and cross-tenant lookups are indistinguishable
            # from a missing task, mirroring Luma's ownership fence.
            raise LumaAdapterError(AdapterErrorCode.NOT_FOUND, http_status=404)
        return record

    def _record(self, task_id: str) -> _TaskRecord:
        record = self._tasks.get(validate_task_id(task_id))
        if record is None:
            raise LumaAdapterError(AdapterErrorCode.NOT_FOUND, http_status=404)
        return record

    def _append_event(
        self,
        record: _TaskRecord,
        event_type: str,
        *,
        status: str | None,
    ) -> BuilderTaskEvent:
        if status is not None and status not in TASK_STATUSES:
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        safe_type_messages = {
            "status": "Builder task status updated.",
            "source.fetch": "Source fetch updated.",
            "source.snapshot": "Source snapshot updated.",
            "analysis": "Source analysis updated.",
            "resolve": "External image resolution updated.",
            "build": "Image build updated.",
            "push": "Image push updated.",
            "complete": "Builder task completion updated.",
        }
        safe_type = event_type if event_type in safe_type_messages else "update"
        message = _STATUS_MESSAGES.get(
            status, safe_type_messages.get(safe_type, "Builder task updated.")
        )
        event = BuilderTaskEvent(
            cursor=record.next_cursor,
            sequence=record.next_cursor,
            event_type=safe_type,
            status=status,
            message=message,
            timestamp=self._now(),
        )
        record.events.append(event)
        record.next_cursor += 1
        record.updated_at = event.timestamp
        return copy.deepcopy(event)

    @staticmethod
    def _task_view(record: _TaskRecord) -> BuilderTask:
        return BuilderTask(
            task_id=record.task_id,
            kind=record.kind,
            external_operation_id=record.context.external_operation_id,
            tenant_ref=record.context.tenant_ref,
            application_ref=record.context.application_ref,
            status=record.status,
            message=_STATUS_MESSAGES[record.status],
            created_at=record.created_at,
            updated_at=record.updated_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            last_cursor=max(record.next_cursor - 1, 0),
            result=copy.deepcopy(record.result),
        )

    def _now(self) -> int:
        return int(self._clock())


class FakeLumaBuilderAdapter:
    def __init__(self, backend: FakeLuma, principal: ServicePrincipal) -> None:
        self._backend = backend
        self._principal = principal

    def create_analyze_task(
        self,
        context: LumaCallContext,
        request: AnalyzeSourceRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        limits = request.limits
        validate_limits(
            limits.cpu, limits.memory_mib, limits.disk_mib, limits.timeout_seconds
        )
        return self._backend._create(
            self._principal,
            context,
            kind="analyze-source",
            payload=request.to_wire(),
            idempotency_key=idempotency_key,
        )

    def create_build_task(
        self,
        context: LumaCallContext,
        request: BuildPlanRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        limits = request.limits
        validate_limits(
            limits.cpu, limits.memory_mib, limits.disk_mib, limits.timeout_seconds
        )
        return self._backend._create(
            self._principal,
            context,
            kind="build-plan",
            payload=request.to_wire(),
            idempotency_key=idempotency_key,
        )

    def get_builder_task(self, context: LumaCallContext, task_id: str) -> BuilderTask:
        return self._backend._get(self._principal, context, task_id)

    def get_builder_task_events(
        self,
        context: LumaCallContext,
        task_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> BuilderTaskEventPage:
        return self._backend._events(
            self._principal, context, task_id, after=after, limit=limit
        )

    def cancel_builder_task(
        self, context: LumaCallContext, task_id: str
    ) -> BuilderTaskMutation:
        return self._backend._cancel(self._principal, context, task_id)

    def analyze_source(
        self,
        context: LumaCallContext,
        request: AnalyzeSourceRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        return self.create_analyze_task(
            context, request, idempotency_key=idempotency_key
        )

    def build_plan(
        self,
        context: LumaCallContext,
        request: BuildPlanRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        return self.create_build_task(context, request, idempotency_key=idempotency_key)

    def watch_builder_task(
        self,
        context: LumaCallContext,
        task_id: str,
        *,
        cursor: int = 0,
        limit: int = 200,
    ) -> BuilderTaskEventPage:
        return self.get_builder_task_events(context, task_id, after=cursor, limit=limit)
