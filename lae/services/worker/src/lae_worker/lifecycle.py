from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from lae_luma_adapter import (
    AdapterErrorCode,
    LumaAdapterError,
    LumaRuntimeAdapter,
    RuntimeCallContext,
    RuntimeDeployment,
)
from lae_store import (
    EventInput,
    LeaseLost,
    OperationRecord,
    OperationStore,
    TenantScope,
)
from lae_store.models import (
    AppRevision,
    Application,
    ApplicationLifecycleRequest,
    ApplicationRoute,
    ApplicationService,
    ApplicationVolume,
    Deployment,
    DeploymentBuildOutput,
    Operation,
)
from lae_store.repositories import _append_event, _operation_record


_ACTIONS = frozenset({"suspend", "resume", "restart", "rollback", "delete"})
_KINDS = tuple(f"application.{action}" for action in sorted(_ACTIONS))
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$")


class LifecycleExecutionError(RuntimeError):
    code = "LAE_APPLICATION_LIFECYCLE_FAILED"
    public_message = "The application lifecycle action did not complete safely."
    retryable = False


class LifecycleContextInvalid(LifecycleExecutionError):
    code = "LAE_APPLICATION_LIFECYCLE_CONTEXT_INVALID"


class LifecycleRuntimeFailed(LifecycleExecutionError):
    code = "LAE_APPLICATION_RUNTIME_ACTION_FAILED"


class LifecycleTimedOut(LifecycleExecutionError):
    code = "LAE_APPLICATION_LIFECYCLE_TIMED_OUT"


@dataclass(frozen=True, slots=True)
class LifecycleDeploymentBinding:
    deployment_id: str
    revision_id: str
    deployment_operation_id: str
    runtime_deployment_ref: str
    manifest_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.deployment_id,
            self.revision_id,
            self.deployment_operation_id,
            self.runtime_deployment_ref,
        ):
            if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
                raise LifecycleContextInvalid()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.manifest_digest):
            raise LifecycleContextInvalid()


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    tenant_id: str
    application_id: str
    action: str
    previous_desired_state: str
    requested_desired_state: str | None
    source: LifecycleDeploymentBinding | None
    target: LifecycleDeploymentBinding | None
    request_created_at: datetime

    def __post_init__(self) -> None:
        if (
            self.action not in _ACTIONS
            or self.previous_desired_state not in {"running", "suspended"}
            or self.requested_desired_state
            not in {None, "running", "suspended", "deleted"}
            or self.request_created_at.tzinfo is None
        ):
            raise LifecycleContextInvalid()
        if self.action in {"suspend", "resume", "restart", "rollback"} and self.source is None:
            raise LifecycleContextInvalid()
        if (self.action == "rollback") != (self.target is not None):
            raise LifecycleContextInvalid()

    def runtime_context(
        self, binding: LifecycleDeploymentBinding
    ) -> RuntimeCallContext:
        return RuntimeCallContext(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            operation_ref=binding.deployment_operation_id,
            revision_ref=binding.revision_id,
            deployment_ref=binding.deployment_id,
        )


class LifecycleContextLoader(Protocol):
    async def load(self, operation: OperationRecord) -> LifecycleContext: ...


class PostgresLifecycleContextLoader:
    def __init__(self, sessions: Any, *, luma_cluster_id: str) -> None:
        if not luma_cluster_id:
            raise ValueError("Luma cluster id is required")
        self._sessions = sessions
        self._cluster = luma_cluster_id

    async def load(self, operation: OperationRecord) -> LifecycleContext:
        _validate_operation(operation)
        try:
            async with self._sessions() as session:
                request = await session.scalar(
                    select(ApplicationLifecycleRequest).where(
                        ApplicationLifecycleRequest.tenant_id == operation.tenant_id,
                        ApplicationLifecycleRequest.operation_id == operation.id,
                        ApplicationLifecycleRequest.application_id == operation.target_id,
                        ApplicationLifecycleRequest.action
                        == operation.kind.removeprefix("application."),
                    )
                )
                application = await session.scalar(
                    select(Application).where(
                        Application.tenant_id == operation.tenant_id,
                        Application.id == operation.target_id,
                        Application.deleted_at.is_(None),
                    )
                )
                if request is None or application is None:
                    raise LifecycleContextInvalid()
                source = await self._deployment_binding(
                    session,
                    operation.tenant_id,
                    application.id,
                    request.source_deployment_id,
                )
                target = await self._deployment_binding(
                    session,
                    operation.tenant_id,
                    application.id,
                    request.rollback_deployment_id,
                )
                if source is not None and (
                    application.current_deployment_id != source.deployment_id
                    or application.current_revision_id != source.revision_id
                ):
                    raise LifecycleContextInvalid()
                if source is None and (
                    application.current_deployment_id is not None
                    or application.current_revision_id is not None
                ):
                    raise LifecycleContextInvalid()
                return LifecycleContext(
                    tenant_id=operation.tenant_id,
                    application_id=application.id,
                    action=request.action,
                    previous_desired_state=request.previous_desired_state,
                    requested_desired_state=request.requested_desired_state,
                    source=source,
                    target=target,
                    request_created_at=request.created_at,
                )
        except DBAPIError:
            raise

    async def _deployment_binding(
        self,
        session: Any,
        tenant_id: str,
        application_id: str,
        deployment_id: str | None,
    ) -> LifecycleDeploymentBinding | None:
        if deployment_id is None:
            return None
        row = (
            await session.execute(
                select(Deployment, AppRevision)
                .join(
                    AppRevision,
                    (AppRevision.tenant_id == Deployment.tenant_id)
                    & (AppRevision.application_id == Deployment.application_id)
                    & (AppRevision.id == Deployment.revision_id),
                )
                .where(
                    Deployment.tenant_id == tenant_id,
                    Deployment.application_id == application_id,
                    Deployment.id == deployment_id,
                    Deployment.status == "succeeded",
                )
            )
        ).one_or_none()
        if row is None:
            raise LifecycleContextInvalid()
        deployment, revision = row
        if (
            deployment.luma_cluster_id != self._cluster
            or deployment.luma_external_ref is None
            or revision.luma_manifest_digest is None
            or revision.status not in {"active", "superseded"}
        ):
            raise LifecycleContextInvalid()
        return LifecycleDeploymentBinding(
            deployment_id=deployment.id,
            revision_id=revision.id,
            deployment_operation_id=deployment.operation_id,
            runtime_deployment_ref=deployment.luma_external_ref,
            manifest_digest=revision.luma_manifest_digest,
        )


class LifecycleStateStore(Protocol):
    async def mark_runtime_started(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        *,
        worker_id: str,
    ) -> OperationRecord: ...

    async def cancel_before_runtime(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        *,
        worker_id: str,
    ) -> OperationRecord: ...

    async def succeed(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        runtime: RuntimeDeployment | None,
        *,
        worker_id: str,
    ) -> OperationRecord: ...

    async def fail_unloaded(
        self,
        operation: OperationRecord,
        error: LifecycleExecutionError,
        *,
        worker_id: str,
    ) -> OperationRecord: ...

    async def fail(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        error: LifecycleExecutionError,
        *,
        worker_id: str,
    ) -> OperationRecord: ...


class PostgresLifecycleStateStore:
    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def mark_runtime_started(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        *,
        worker_id: str,
    ) -> OperationRecord:
        async with self._sessions() as session:
            async with session.begin():
                row, request, application, now = await self._locked(
                    session, operation, context, worker_id=worker_id
                )
                if row.phase == "application.lifecycle.runtime":
                    return _operation_record(row)
                if row.phase != "application.lifecycle":
                    raise LifecycleContextInvalid()
                if row.cancel_requested_at is not None:
                    raise LifecycleCancellationBeforeRuntime()
                row.phase = "application.lifecycle.runtime"
                row.updated_at = now
                await _append_event(
                    session,
                    row,
                    EventInput(
                        type="application.lifecycle.runtime-submitted",
                        phase=row.phase,
                        status="running",
                        message="Application lifecycle action submitted to Luma",
                        data={"action": context.action},
                    ),
                )
                await session.flush()
                del request, application
                return _operation_record(row)

    async def cancel_before_runtime(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        *,
        worker_id: str,
    ) -> OperationRecord:
        async with self._sessions() as session:
            async with session.begin():
                row, request, application, now = await self._locked(
                    session, operation, context, worker_id=worker_id
                )
                if row.phase != "application.lifecycle":
                    raise LifecycleContextInvalid()
                await self._restore_desired(session, request, application, now)
                return await self._finish_operation(
                    session,
                    row,
                    status="canceled",
                    now=now,
                    result=None,
                    error=None,
                )

    async def succeed(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        runtime: RuntimeDeployment | None,
        *,
        worker_id: str,
    ) -> OperationRecord:
        async with self._sessions() as session:
            async with session.begin():
                row, request, application, now = await self._locked(
                    session, operation, context, worker_id=worker_id
                )
                if row.phase != "application.lifecycle.runtime":
                    raise LifecycleContextInvalid()
                if runtime is not None:
                    expected = context.target if context.action == "rollback" else context.source
                    if (
                        expected is None
                        or runtime.deployment_ref != expected.runtime_deployment_ref
                        or runtime.manifest_digest != expected.manifest_digest
                    ):
                        raise LifecycleContextInvalid()
                await self._apply_success(
                    session, request, application, context, now
                )
                result = {
                    "applicationId": context.application_id,
                    "action": context.action,
                    "desiredState": application.desired_state,
                    "observedState": application.observed_state,
                    **(
                        {}
                        if context.action == "delete"
                        else {
                            "deploymentId": application.current_deployment_id,
                            "revisionId": application.current_revision_id,
                        }
                    ),
                }
                if row.cancel_requested_at is not None:
                    await _append_event(
                        session,
                        row,
                        EventInput(
                            type="application.lifecycle.cancel-too-late",
                            phase=row.phase,
                            status="running",
                            level="warning",
                            message="Cancellation arrived after the runtime action started",
                            data={"action": context.action},
                        ),
                    )
                return await self._finish_operation(
                    session,
                    row,
                    status="succeeded",
                    now=now,
                    result=result,
                    error=None,
                    ignore_cancel=True,
                )

    async def fail_unloaded(
        self,
        operation: OperationRecord,
        error: LifecycleExecutionError,
        *,
        worker_id: str,
    ) -> OperationRecord:
        """Fail invalid immutable context and undo its admission transition."""

        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == operation.tenant_id,
                        Operation.id == operation.id,
                        Operation.target_type == "application",
                        Operation.target_id == operation.target_id,
                        Operation.status == "running",
                        Operation.lease_owner == worker_id,
                        Operation.lease_attempt == operation.lease_attempt,
                        Operation.lease_expires_at > func.now(),
                    )
                    .with_for_update()
                )
                request = await session.scalar(
                    select(ApplicationLifecycleRequest)
                    .where(
                        ApplicationLifecycleRequest.tenant_id
                        == operation.tenant_id,
                        ApplicationLifecycleRequest.operation_id == operation.id,
                        ApplicationLifecycleRequest.application_id
                        == operation.target_id,
                        ApplicationLifecycleRequest.action
                        == operation.kind.removeprefix("application."),
                    )
                    .with_for_update()
                )
                application = await session.scalar(
                    select(Application)
                    .where(
                        Application.tenant_id == operation.tenant_id,
                        Application.id == operation.target_id,
                    )
                    .with_for_update()
                )
                now = await session.scalar(select(func.now()))
                if row is None:
                    raise LeaseLost("lifecycle operation lease is no longer owned")
                if request is None or application is None or now is None:
                    raise LifecycleContextInvalid()
                await self._restore_desired(session, request, application, now)
                application.observed_state = "unknown"
                application.updated_at = now
                return await self._finish_operation(
                    session,
                    row,
                    status="failed",
                    now=now,
                    result=None,
                    error=error,
                    ignore_cancel=True,
                )

    async def fail(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        error: LifecycleExecutionError,
        *,
        worker_id: str,
    ) -> OperationRecord:
        async with self._sessions() as session:
            async with session.begin():
                row, request, application, now = await self._locked(
                    session, operation, context, worker_id=worker_id
                )
                await self._restore_desired(session, request, application, now)
                application.observed_state = "unknown"
                application.updated_at = now
                return await self._finish_operation(
                    session,
                    row,
                    status="failed",
                    now=now,
                    result=None,
                    error=error,
                    ignore_cancel=True,
                )

    async def _locked(
        self,
        session: Any,
        operation: OperationRecord,
        context: LifecycleContext,
        *,
        worker_id: str,
    ) -> tuple[Operation, ApplicationLifecycleRequest, Application, datetime]:
        row = await session.scalar(
            select(Operation)
            .where(
                Operation.tenant_id == context.tenant_id,
                Operation.id == operation.id,
                Operation.status == "running",
                Operation.lease_owner == worker_id,
                Operation.lease_attempt == operation.lease_attempt,
                Operation.lease_expires_at > func.now(),
            )
            .with_for_update()
        )
        request = await session.scalar(
            select(ApplicationLifecycleRequest)
            .where(
                ApplicationLifecycleRequest.tenant_id == context.tenant_id,
                ApplicationLifecycleRequest.operation_id == operation.id,
                ApplicationLifecycleRequest.application_id == context.application_id,
                ApplicationLifecycleRequest.action == context.action,
            )
            .with_for_update()
        )
        application = await session.scalar(
            select(Application)
            .where(
                Application.tenant_id == context.tenant_id,
                Application.id == context.application_id,
            )
            .with_for_update()
        )
        now = await session.scalar(select(func.now()))
        if row is None or request is None or application is None or now is None:
            raise LeaseLost("lifecycle operation lease is no longer owned")
        if (
            request.previous_desired_state != context.previous_desired_state
            or request.requested_desired_state != context.requested_desired_state
            or request.source_deployment_id
            != (None if context.source is None else context.source.deployment_id)
            or request.rollback_deployment_id
            != (None if context.target is None else context.target.deployment_id)
        ):
            raise LifecycleContextInvalid()
        return row, request, application, now

    async def _apply_success(
        self,
        session: Any,
        request: ApplicationLifecycleRequest,
        application: Application,
        context: LifecycleContext,
        now: datetime,
    ) -> None:
        services = list(
            await session.scalars(
                select(ApplicationService)
                .where(
                    ApplicationService.tenant_id == context.tenant_id,
                    ApplicationService.application_id == context.application_id,
                )
                .with_for_update()
            )
        )
        if context.action == "suspend":
            application.observed_state = "suspended"
            for service in services:
                service.observed_state = "suspended"
                service.updated_at = now
            await self._set_routes(session, context, "disabled", now)
        elif context.action in {"resume", "restart"}:
            application.observed_state = "running"
            for service in services:
                service.observed_state = "running"
                service.updated_at = now
            await self._set_routes(session, context, "ready", now)
        elif context.action == "rollback":
            assert context.source is not None and context.target is not None
            source_revision = await session.scalar(
                select(AppRevision)
                .where(
                    AppRevision.tenant_id == context.tenant_id,
                    AppRevision.application_id == context.application_id,
                    AppRevision.id == context.source.revision_id,
                )
                .with_for_update()
            )
            target_revision = await session.scalar(
                select(AppRevision)
                .where(
                    AppRevision.tenant_id == context.tenant_id,
                    AppRevision.application_id == context.application_id,
                    AppRevision.id == context.target.revision_id,
                )
                .with_for_update()
            )
            outputs = list(
                await session.scalars(
                    select(DeploymentBuildOutput).where(
                        DeploymentBuildOutput.tenant_id == context.tenant_id,
                        DeploymentBuildOutput.deployment_id
                        == context.target.deployment_id,
                    )
                )
            )
            output_by_service = {item.service_key: item for item in outputs}
            if (
                source_revision is None
                or target_revision is None
                or source_revision.status != "active"
                or target_revision.status != "superseded"
                or set(output_by_service) != {service.service_key for service in services}
                or application.current_deployment_id != context.source.deployment_id
                or application.current_revision_id != context.source.revision_id
            ):
                raise LifecycleContextInvalid()
            source_revision.status = "superseded"
            source_revision.updated_at = now
            target_revision.status = "active"
            target_revision.activated_at = now
            target_revision.updated_at = now
            application.current_deployment_id = context.target.deployment_id
            application.current_revision_id = context.target.revision_id
            application.observed_state = "running"
            for service in services:
                service.current_image_digest = output_by_service[
                    service.service_key
                ].image_digest
                service.observed_state = "running"
                service.updated_at = now
            await self._set_routes(session, context, "ready", now)
        elif context.action == "delete":
            application.deleted_at = now
            application.current_deployment_id = None
            application.current_revision_id = None
            application.observed_state = "suspended"
            for service in services:
                service.desired_state = "deleted"
                service.observed_state = "suspended"
                service.updated_at = now
            await self._set_routes(session, context, "disabled", now)
            await session.execute(
                update(ApplicationVolume)
                .where(
                    ApplicationVolume.tenant_id == context.tenant_id,
                    ApplicationVolume.application_id == context.application_id,
                    ApplicationVolume.status.notin_(("deleted", "retained")),
                )
                .values(status="retained", updated_at=now)
            )
        else:
            raise LifecycleContextInvalid()
        application.updated_at = now
        del request
        await session.flush()

    async def _restore_desired(
        self,
        session: Any,
        request: ApplicationLifecycleRequest,
        application: Application,
        now: datetime,
    ) -> None:
        application.desired_state = request.previous_desired_state
        application.updated_at = now
        await session.execute(
            update(ApplicationService)
            .where(
                ApplicationService.tenant_id == request.tenant_id,
                ApplicationService.application_id == request.application_id,
            )
            .values(desired_state=request.previous_desired_state, updated_at=now)
        )

    @staticmethod
    async def _set_routes(
        session: Any,
        context: LifecycleContext,
        status: str,
        now: datetime,
    ) -> None:
        await session.execute(
            update(ApplicationRoute)
            .where(
                ApplicationRoute.tenant_id == context.tenant_id,
                ApplicationRoute.application_id == context.application_id,
            )
            .values(status=status, updated_at=now)
        )

    @staticmethod
    async def _finish_operation(
        session: Any,
        operation: Operation,
        *,
        status: str,
        now: datetime,
        result: dict[str, Any] | None,
        error: LifecycleExecutionError | None,
        ignore_cancel: bool = False,
    ) -> OperationRecord:
        if status not in {"succeeded", "failed", "canceled"}:
            raise ValueError("lifecycle terminal status is invalid")
        if operation.cancel_requested_at is not None and not ignore_cancel:
            status = "canceled"
        operation.status = status
        operation.result = result if status == "succeeded" else None
        operation.error_code = error.code if status == "failed" and error else None
        operation.error_message = (
            error.public_message if status == "failed" and error else None
        )
        operation.finished_at = now
        operation.lease_owner = None
        operation.lease_expires_at = None
        operation.lease_heartbeat_at = None
        operation.updated_at = now
        await _append_event(
            session,
            operation,
            EventInput(
                type=f"operation.{status}",
                phase=operation.phase,
                status=status,
                level="error" if status == "failed" else "info",
                message=f"Operation {status}",
                data={},
            ),
        )
        await session.flush()
        return _operation_record(operation)


class LifecycleCancellationBeforeRuntime(LifecycleExecutionError):
    code = "LAE_APPLICATION_LIFECYCLE_CANCELED"


@dataclass(frozen=True, slots=True)
class LifecycleWorkerConfig:
    lease_seconds: int = 60
    timeout_seconds: int = 1800
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            not 5 <= self.lease_seconds <= 3600
            or not 30 <= self.timeout_seconds <= 7200
            or not 0 <= self.poll_interval_seconds <= 60
        ):
            raise ValueError("lifecycle worker configuration is invalid")


class LifecycleStepStatus(StrEnum):
    WAITING = "waiting"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class LifecycleStepResult:
    status: LifecycleStepStatus
    operation: OperationRecord


class LifecycleStepRunner:
    def __init__(
        self,
        *,
        operations: OperationStore,
        contexts: LifecycleContextLoader,
        states: LifecycleStateStore,
        runtime: LumaRuntimeAdapter,
        config: LifecycleWorkerConfig,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._operations = operations
        self._contexts = contexts
        self._states = states
        self._runtime = runtime
        self._config = config
        self._worker_id = worker_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def step(self, operation: OperationRecord) -> LifecycleStepResult:
        _validate_operation(operation)
        try:
            context = await self._contexts.load(operation)
        except LifecycleExecutionError as exc:
            completed = await self._states.fail_unloaded(
                operation, exc, worker_id=self._worker_id
            )
            return LifecycleStepResult(LifecycleStepStatus.TERMINAL, completed)
        current = await self._operations.heartbeat(
            TenantScope(context.tenant_id),
            operation.id,
            worker_id=self._worker_id,
            lease_seconds=self._config.lease_seconds,
        )
        if (
            current.cancel_requested
            and current.phase == "application.lifecycle"
        ):
            canceled = await self._states.cancel_before_runtime(
                current, context, worker_id=self._worker_id
            )
            return LifecycleStepResult(LifecycleStepStatus.TERMINAL, canceled)
        try:
            current = await self._states.mark_runtime_started(
                current, context, worker_id=self._worker_id
            )
        except LifecycleCancellationBeforeRuntime:
            canceled = await self._states.cancel_before_runtime(
                current, context, worker_id=self._worker_id
            )
            return LifecycleStepResult(LifecycleStepStatus.TERMINAL, canceled)

        deadline = context.request_created_at + timedelta(
            seconds=self._config.timeout_seconds
        )
        if self._clock() >= deadline:
            return await self._fail(current, context, LifecycleTimedOut())
        if context.action == "delete" and context.source is None:
            completed = await self._states.succeed(
                current, context, None, worker_id=self._worker_id
            )
            return LifecycleStepResult(LifecycleStepStatus.TERMINAL, completed)
        assert context.source is not None
        try:
            runtime = await self._mutate(current, context)
            if not _runtime_ready_for_action(context.action, runtime):
                runtime = await _call_sync(
                    self._runtime.get_runtime_deployment,
                    context.runtime_context(
                        context.target
                        if context.action == "rollback" and context.target is not None
                        else context.source
                    ),
                    runtime.deployment_ref,
                )
            if runtime.status in {"failed", "canceled", "deleted"} and context.action != "delete":
                raise LifecycleRuntimeFailed()
            if not _runtime_ready_for_action(context.action, runtime):
                return LifecycleStepResult(LifecycleStepStatus.WAITING, current)
            completed = await self._states.succeed(
                current, context, runtime, worker_id=self._worker_id
            )
            return LifecycleStepResult(LifecycleStepStatus.TERMINAL, completed)
        except LeaseLost:
            raise
        except LumaAdapterError as exc:
            if context.action == "delete" and exc.code == AdapterErrorCode.NOT_FOUND:
                completed = await self._states.succeed(
                    current, context, None, worker_id=self._worker_id
                )
                return LifecycleStepResult(LifecycleStepStatus.TERMINAL, completed)
            if exc.retryable:
                return LifecycleStepResult(LifecycleStepStatus.WAITING, current)
            return await self._fail(current, context, LifecycleRuntimeFailed())
        except LifecycleExecutionError as exc:
            return await self._fail(current, context, exc)

    async def _mutate(
        self, operation: OperationRecord, context: LifecycleContext
    ) -> RuntimeDeployment:
        assert context.source is not None
        source_context = context.runtime_context(context.source)
        key = f"lae:{operation.id}:application-{context.action}:v1"
        if context.action == "suspend":
            mutation = await _call_sync(
                self._runtime.suspend_runtime_deployment,
                source_context,
                context.source.runtime_deployment_ref,
                idempotency_key=key,
            )
        elif context.action == "resume":
            mutation = await _call_sync(
                self._runtime.resume_runtime_deployment,
                source_context,
                context.source.runtime_deployment_ref,
                idempotency_key=key,
            )
        elif context.action == "restart":
            mutation = await _call_sync(
                self._runtime.restart_runtime_deployment,
                source_context,
                context.source.runtime_deployment_ref,
                idempotency_key=key,
            )
        elif context.action == "delete":
            mutation = await _call_sync(
                self._runtime.delete_runtime_deployment,
                source_context,
                context.source.runtime_deployment_ref,
                volume_policy="retain",
                idempotency_key=key,
            )
        elif context.action == "rollback":
            assert context.target is not None
            source_runtime, target_runtime = await asyncio.gather(
                _call_sync(
                    self._runtime.get_runtime_deployment,
                    source_context,
                    context.source.runtime_deployment_ref,
                ),
                _call_sync(
                    self._runtime.get_runtime_deployment,
                    context.runtime_context(context.target),
                    context.target.runtime_deployment_ref,
                ),
            )
            if (
                set(source_runtime.service_statuses)
                != set(target_runtime.service_statuses)
                or set(source_runtime.route_statuses)
                != set(target_runtime.route_statuses)
                or {
                    (item.key, item.volume_ref)
                    for item in source_runtime.volume_bindings
                }
                != {
                    (item.key, item.volume_ref)
                    for item in target_runtime.volume_bindings
                }
            ):
                # The v1 catalog is application-scoped rather than revisioned.
                # Never roll the runtime to a topology the database cannot
                # represent atomically.
                raise LifecycleContextInvalid()
            mutation = await _call_sync(
                self._runtime.rollback_runtime_deployment,
                source_context,
                context.source.runtime_deployment_ref,
                target_context=context.runtime_context(context.target),
                target_deployment_ref=context.target.runtime_deployment_ref,
                idempotency_key=key,
            )
        else:
            raise LifecycleContextInvalid()
        expected = context.target if context.action == "rollback" else context.source
        if (
            mutation.deployment.deployment_ref != expected.runtime_deployment_ref
            or mutation.deployment.manifest_digest != expected.manifest_digest
        ):
            raise LifecycleContextInvalid()
        return mutation.deployment

    async def _fail(
        self,
        operation: OperationRecord,
        context: LifecycleContext,
        error: LifecycleExecutionError,
    ) -> LifecycleStepResult:
        completed = await self._states.fail(
            operation, context, error, worker_id=self._worker_id
        )
        return LifecycleStepResult(LifecycleStepStatus.TERMINAL, completed)


class LifecycleWorker:
    def __init__(
        self,
        operations: OperationStore,
        runner: LifecycleStepRunner,
        *,
        worker_id: str,
        config: LifecycleWorkerConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._operations = operations
        self._runner = runner
        self._worker_id = worker_id
        self._config = config
        self._sleep = sleep

    async def run_once(self) -> LifecycleStepResult | None:
        operation = await self._operations.claim_next(
            worker_id=self._worker_id,
            kinds=_KINDS,
            lease_seconds=self._config.lease_seconds,
        )
        if operation is None:
            return None
        while True:
            result = await self._runner.step(operation)
            operation = result.operation
            if result.status is LifecycleStepStatus.TERMINAL:
                return result
            await self._sleep(self._config.poll_interval_seconds)


def _runtime_ready_for_action(action: str, runtime: RuntimeDeployment) -> bool:
    if action == "suspend":
        return runtime.status == "suspended"
    if action == "delete":
        return runtime.status == "deleted"
    if runtime.status != "running":
        return False
    return all(value == "healthy" for value in runtime.service_statuses.values()) and all(
        value == "ready" for value in runtime.route_statuses.values()
    )


def _validate_operation(operation: OperationRecord) -> None:
    if (
        operation.kind not in _KINDS
        or operation.target_type != "application"
        or not operation.target_id.startswith("app_")
        or operation.status != "running"
        or operation.lease_owner is None
        or operation.phase not in {
            "application.lifecycle",
            "application.lifecycle.runtime",
        }
    ):
        raise LifecycleContextInvalid()


async def _call_sync(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(function, *args, **kwargs)


__all__ = [
    "LifecycleContext",
    "LifecycleContextInvalid",
    "LifecycleDeploymentBinding",
    "LifecycleExecutionError",
    "LifecycleRuntimeFailed",
    "LifecycleStepResult",
    "LifecycleStepRunner",
    "LifecycleStepStatus",
    "LifecycleTimedOut",
    "LifecycleWorker",
    "LifecycleWorkerConfig",
    "PostgresLifecycleContextLoader",
    "PostgresLifecycleStateStore",
]
