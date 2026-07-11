from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from sqlalchemy import select

from lae_luma_adapter import (
    AdapterErrorCode,
    AnalyzeSourceRequest,
    BuilderLimits,
    BuilderTask,
    BuilderTaskEvent,
    LumaAdapterError,
    LumaBuilderAdapter,
    LumaCallContext,
    ObjectSourceReference,
    SourceReference,
)
from lae_store import (
    EventInput,
    LeaseLost,
    OperationRecord,
    OperationStatus,
    OperationStore,
    TenantRepository,
    TenantScope,
    UpdateCheckResult,
)
from lae_store.models import BuilderTask, Upload


_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SECRET_QUERY_KEY = re.compile(
    r"(?:authorization|password|passwd|secret|token|credential|private.?key)",
    re.IGNORECASE,
)


class AnalyzeOrchestrationError(RuntimeError):
    code = "LAE_ANALYZE_ORCHESTRATION_INVALID"
    public_message = "The analysis operation could not be orchestrated safely."


class AnalyzeCheckpointMissing(AnalyzeOrchestrationError):
    code = "LAE_ANALYZE_CHECKPOINT_MISSING"
    public_message = "The analysis operation is missing its durable task checkpoint."


class AnalyzeStateConflict(AnalyzeOrchestrationError):
    code = "LAE_ANALYZE_CHECKPOINT_CONFLICT"
    public_message = "The analysis task checkpoint changed concurrently."


class AnalyzeContextInvalid(AnalyzeOrchestrationError):
    code = "LAE_ANALYZE_CONTEXT_INVALID"
    public_message = "The analysis source is not bound to this tenant and application."


class OrchestrationSchemaUnavailable(AnalyzeOrchestrationError):
    code = "LAE_ANALYZE_SCHEMA_UNAVAILABLE"
    public_message = "Durable analysis task checkpoint storage is not available."


class ArtifactIngestCanceled(AnalyzeOrchestrationError):
    code = "LAE_ARTIFACT_INGEST_CANCELED"
    public_message = "Analysis artifact ingestion was canceled."


class UpdateCheckResultInvalid(AnalyzeOrchestrationError):
    code = "LAE_UPDATE_CHECK_RESULT_INVALID"
    public_message = "The update comparison could not be produced safely."


@dataclass(frozen=True, slots=True)
class AnalyzeSourceContext:
    tenant_ref: str
    application_ref: str
    source_revision_ref: str
    repository: str | None
    ref: str | None = None
    subdirectory: str | None = None
    object_digest: str | None = None
    object_media_type: str | None = None
    object_size_bytes: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.tenant_ref,
            self.application_ref,
            self.source_revision_ref,
        ):
            if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                raise AnalyzeContextInvalid()
        object_fields = (
            self.object_digest,
            self.object_media_type,
            self.object_size_bytes,
        )
        if self.repository is not None:
            if any(value is not None for value in object_fields):
                raise AnalyzeContextInvalid()
            _validate_repository(self.repository)
            if self.ref is not None and (
                not isinstance(self.ref, str)
                or not self.ref
                or len(self.ref) > 512
                or any(character in self.ref for character in "\x00\r\n")
            ):
                raise AnalyzeContextInvalid()
            if self.subdirectory is not None:
                normalized = self.subdirectory.replace("\\", "/")
                if (
                    normalized.startswith("/")
                    or any(part == ".." for part in normalized.split("/"))
                    or "\x00" in normalized
                    or len(normalized) > 1024
                ):
                    raise AnalyzeContextInvalid()
            return
        if self.ref is not None or self.subdirectory is not None:
            raise AnalyzeContextInvalid()
        if (
            not isinstance(self.object_digest, str)
            or not _DIGEST.fullmatch(self.object_digest)
            or self.object_media_type not in {"text/html", "application/zip"}
            or isinstance(self.object_size_bytes, bool)
            or not isinstance(self.object_size_bytes, int)
            or not 1 <= self.object_size_bytes <= 536_870_912
        ):
            raise AnalyzeContextInvalid()

    @property
    def object_source(self) -> bool:
        return self.repository is None


class AnalyzeContextLoader(Protocol):
    async def load(self, operation: OperationRecord) -> AnalyzeSourceContext: ...


class UpdateCheckResolver(Protocol):
    async def resolve(
        self,
        operation: OperationRecord,
        context: AnalyzeSourceContext,
    ) -> UpdateCheckResult: ...


class PostgresAnalyzeContextLoader:
    """Resolve tenant/application/source facts through scoped repositories.

    The orchestration checkpoint is deliberately not accepted as a source of
    these facts.  A compromised or stale checkpoint therefore cannot redirect
    a task to another tenant, application, source revision, or repository.
    """

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def load(self, operation: OperationRecord) -> AnalyzeSourceContext:
        _validate_claimed_operation(operation)
        scope = TenantScope(operation.tenant_id)
        async with self._sessions() as session:
            repository = TenantRepository(session, scope)
            if operation.kind == "source.analyze":
                source_id = operation.target_id
            else:
                task = await session.scalar(
                    select(BuilderTask).where(
                        BuilderTask.tenant_id == operation.tenant_id,
                        BuilderTask.application_id == operation.target_id,
                        BuilderTask.operation_id == operation.id,
                        BuilderTask.action == "source.analyze",
                    )
                )
                if task is None:
                    raise AnalyzeContextInvalid()
                source_id = task.source_revision_id
            source = await repository.get_source_revision(source_id)
            if source.application_id is None:
                raise AnalyzeContextInvalid()
            application = await repository.get_application(source.application_id)
            if (
                operation.kind == "application.check-update"
                and application.id != operation.target_id
            ):
                raise AnalyzeContextInvalid()
            if source.kind == "git" and source.repository:
                return AnalyzeSourceContext(
                    tenant_ref=operation.tenant_id,
                    application_ref=application.id,
                    source_revision_ref=source.id,
                    repository=source.repository,
                    ref=source.ref,
                    subdirectory=source.subdirectory or None,
                )
            if source.kind != "upload" or not source.upload_id:
                raise AnalyzeContextInvalid()
            upload = await session.scalar(
                select(Upload).where(
                    Upload.tenant_id == operation.tenant_id,
                    Upload.application_id == application.id,
                    Upload.id == source.upload_id,
                    Upload.source_revision_id == source.id,
                    Upload.status == "ready",
                    Upload.deleted_at.is_(None),
                )
            )
            if (
                upload is None
                or upload.actual_sha256 is None
                or upload.actual_bytes is None
                or upload.kind not in {"html", "zip"}
                or upload.media_type
                != ("text/html" if upload.kind == "html" else "application/zip")
            ):
                raise AnalyzeContextInvalid()
            return AnalyzeSourceContext(
                tenant_ref=operation.tenant_id,
                application_ref=application.id,
                source_revision_ref=source.id,
                repository=None,
                object_digest=upload.actual_sha256,
                object_media_type=upload.media_type,
                object_size_bytes=upload.actual_bytes,
            )


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    name: str
    digest: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        media_types = {
            "evidence": "application/vnd.lae.evidence+json",
            "deploymentPlan": "application/vnd.lae.deployment-plan+json",
            "buildPlan": "application/vnd.lae.build-plan-candidate+json",
        }
        if self.name not in media_types:
            raise AnalyzeOrchestrationError()
        if not _DIGEST.fullmatch(self.digest):
            raise AnalyzeOrchestrationError()
        if self.media_type != media_types[self.name]:
            raise AnalyzeOrchestrationError()
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= 1024**3
        ):
            raise AnalyzeOrchestrationError()

    def to_result(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "mediaType": self.media_type,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class AnalysisDigestReferences:
    resolved_commit: str
    source_tree_digest: str
    source_snapshot_id: str
    source_snapshot_digest: str
    deployment_plan_digest: str
    build_plan_digest: str
    evidence_digest: str
    policy_version: str
    artifacts: tuple[ArtifactDescriptor, ...]

    def __post_init__(self) -> None:
        if not _COMMIT.fullmatch(self.resolved_commit):
            raise AnalyzeOrchestrationError()
        for digest in (
            self.source_tree_digest,
            self.source_snapshot_digest,
            self.deployment_plan_digest,
            self.build_plan_digest,
            self.evidence_digest,
        ):
            if not _DIGEST.fullmatch(digest):
                raise AnalyzeOrchestrationError()
        if (
            not _REFERENCE.fullmatch(self.source_snapshot_id)
            or not self.source_snapshot_id.startswith("snapshot-")
            or not _REFERENCE.fullmatch(self.policy_version)
        ):
            raise AnalyzeOrchestrationError()
        if {descriptor.name for descriptor in self.artifacts} != {
            "evidence",
            "deploymentPlan",
            "buildPlan",
        }:
            raise AnalyzeOrchestrationError()
        expected = {
            "evidence": self.evidence_digest,
            "deploymentPlan": self.deployment_plan_digest,
            "buildPlan": self.build_plan_digest,
        }
        if any(
            descriptor.digest != expected[descriptor.name]
            for descriptor in self.artifacts
        ):
            raise AnalyzeOrchestrationError()

    @classmethod
    def from_task(cls, task: BuilderTask) -> "AnalysisDigestReferences":
        result = task.result
        if task.status != "succeeded" or not isinstance(result, dict):
            raise AnalyzeOrchestrationError()
        artifacts_value = result.get("artifacts")
        if not isinstance(artifacts_value, dict):
            raise AnalyzeOrchestrationError()
        artifacts: list[ArtifactDescriptor] = []
        for name in ("evidence", "deploymentPlan", "buildPlan"):
            value = artifacts_value.get(name)
            if not isinstance(value, dict):
                raise AnalyzeOrchestrationError()
            artifacts.append(
                ArtifactDescriptor(
                    name=name,
                    digest=_required_string(value, "digest"),
                    media_type=_required_string(value, "mediaType"),
                    size_bytes=_required_int(value, "sizeBytes"),
                )
            )
        return cls(
            resolved_commit=_required_string(result, "resolvedCommit"),
            source_tree_digest=_required_string(result, "sourceTreeDigest"),
            source_snapshot_id=_required_string(result, "sourceSnapshotId"),
            source_snapshot_digest=_required_string(result, "sourceSnapshotDigest"),
            deployment_plan_digest=_required_string(result, "deploymentPlanDigest"),
            build_plan_digest=_required_string(result, "buildPlanDigest"),
            evidence_digest=_required_string(result, "evidenceDigest"),
            policy_version=_required_string(result, "policyVersion"),
            artifacts=tuple(artifacts),
        )

    def to_result(self) -> dict[str, object]:
        return {
            "resolvedCommit": self.resolved_commit,
            "sourceTreeDigest": self.source_tree_digest,
            "sourceSnapshotId": self.source_snapshot_id,
            "sourceSnapshotDigest": self.source_snapshot_digest,
            "deploymentPlanDigest": self.deployment_plan_digest,
            "buildPlanDigest": self.build_plan_digest,
            "evidenceDigest": self.evidence_digest,
            "policyVersion": self.policy_version,
            "artifactDescriptors": {
                descriptor.name: descriptor.to_result()
                for descriptor in self.artifacts
            },
        }


@dataclass(frozen=True, slots=True)
class AnalysisRecording:
    analysis_status: str = "analyzed"
    artifact_state: str = "descriptor-only"
    plan_stored: bool = False

    def __post_init__(self) -> None:
        if self.analysis_status not in {
            "analyzed",
            "deployable",
            "needs_configuration",
            "not_deployable",
        }:
            raise AnalyzeOrchestrationError()
        if self.artifact_state not in {"descriptor-only", "stored"}:
            raise AnalyzeOrchestrationError()
        if self.plan_stored != (self.artifact_state == "stored"):
            raise AnalyzeOrchestrationError()


class AnalysisResultRecorder(Protocol):
    async def record(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str | None = None,
    ) -> AnalysisRecording: ...


class DescriptorOnlyAnalysisRecorder:
    """Honest foundation behavior until artifact download/storage exists."""

    async def record(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str | None = None,
    ) -> AnalysisRecording:
        del operation_id, context, references, builder_task_id
        return AnalysisRecording()


@dataclass(frozen=True, slots=True)
class AnalyzeOrchestrationState:
    operation_id: str
    credential_lease_id: str = field(repr=False)
    version: int = 0
    tenant_ref: str | None = None
    application_ref: str | None = None
    source_revision_ref: str | None = None
    luma_task_id: str | None = None
    luma_cursor: int = 0
    luma_status: str | None = None
    cancel_forwarded: bool = False
    digest_references: AnalysisDigestReferences | None = None
    recording: AnalysisRecording | None = None

    def __post_init__(self) -> None:
        if not _REFERENCE.fullmatch(self.operation_id):
            raise AnalyzeOrchestrationError()
        if not _REFERENCE.fullmatch(self.credential_lease_id):
            raise AnalyzeOrchestrationError()
        if self.version < 0 or self.luma_cursor < 0:
            raise AnalyzeOrchestrationError()
        for value in (
            self.tenant_ref,
            self.application_ref,
            self.source_revision_ref,
        ):
            if value is not None and not _REFERENCE.fullmatch(value):
                raise AnalyzeOrchestrationError()
        if self.luma_task_id is not None and (
            not isinstance(self.luma_task_id, str)
            or not self.luma_task_id
            or len(self.luma_task_id) > 256
            or any(character in self.luma_task_id for character in "\x00\r\n")
        ):
            raise AnalyzeOrchestrationError()

    def bind(self, context: AnalyzeSourceContext) -> "AnalyzeOrchestrationState":
        current = (
            self.tenant_ref,
            self.application_ref,
            self.source_revision_ref,
        )
        expected = (
            context.tenant_ref,
            context.application_ref,
            context.source_revision_ref,
        )
        if any(value is not None for value in current) and current != expected:
            raise AnalyzeContextInvalid()
        return replace(
            self,
            tenant_ref=expected[0],
            application_ref=expected[1],
            source_revision_ref=expected[2],
        )


class AnalyzeStateStore(Protocol):
    async def initialize(
        self, operation_id: str, *, credential_lease_id: str
    ) -> AnalyzeOrchestrationState: ...

    async def load(self, operation_id: str) -> AnalyzeOrchestrationState | None: ...

    async def save(
        self,
        state: AnalyzeOrchestrationState,
        *,
        expected_version: int,
    ) -> AnalyzeOrchestrationState: ...


class InMemoryAnalyzeStateStore:
    """Deterministic fake for orchestration tests, never a production fallback."""

    def __init__(self) -> None:
        self._states: dict[str, AnalyzeOrchestrationState] = {}
        self._lock = asyncio.Lock()

    async def initialize(
        self, operation_id: str, *, credential_lease_id: str
    ) -> AnalyzeOrchestrationState:
        candidate = AnalyzeOrchestrationState(
            operation_id=operation_id,
            credential_lease_id=credential_lease_id,
        )
        async with self._lock:
            existing = self._states.get(operation_id)
            if existing is not None:
                if existing.credential_lease_id != credential_lease_id:
                    raise AnalyzeStateConflict()
                return existing
            self._states[operation_id] = candidate
            return candidate

    async def load(self, operation_id: str) -> AnalyzeOrchestrationState | None:
        async with self._lock:
            return self._states.get(operation_id)

    async def save(
        self,
        state: AnalyzeOrchestrationState,
        *,
        expected_version: int,
    ) -> AnalyzeOrchestrationState:
        async with self._lock:
            current = self._states.get(state.operation_id)
            if current is None or current.version != expected_version:
                raise AnalyzeStateConflict()
            stored = replace(state, version=expected_version + 1)
            self._states[state.operation_id] = stored
            return stored


@dataclass(frozen=True, slots=True)
class AnalyzeWorkerConfig:
    agent_image_digest: str
    policy_version: str
    limits: BuilderLimits
    lease_seconds: int = 60
    event_page_limit: int = 100
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not _IMAGE_DIGEST.fullmatch(self.agent_image_digest):
            raise ValueError("agent_image_digest must be immutable")
        if not _REFERENCE.fullmatch(self.policy_version):
            raise ValueError("policy_version has invalid format")
        if not 5 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        if not 1 <= self.event_page_limit <= 500:
            raise ValueError("event_page_limit must be between 1 and 500")
        if not 0 <= self.poll_interval_seconds <= self.lease_seconds / 3:
            raise ValueError("poll_interval_seconds must fit within the lease")


class StepStatus(StrEnum):
    WAITING = "waiting"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class StepResult:
    status: StepStatus
    operation: OperationRecord


class AnalyzeStepRunner:
    """One deterministic, crash-resumable source analysis state transition."""

    def __init__(
        self,
        *,
        operations: OperationStore,
        contexts: AnalyzeContextLoader,
        states: AnalyzeStateStore,
        luma: LumaBuilderAdapter,
        config: AnalyzeWorkerConfig,
        worker_id: str,
        recorder: AnalysisResultRecorder | None = None,
        update_checks: UpdateCheckResolver | None = None,
    ) -> None:
        self._operations = operations
        self._contexts = contexts
        self._states = states
        self._luma = luma
        self._config = config
        self._worker_id = worker_id
        self._recorder = recorder or DescriptorOnlyAnalysisRecorder()
        self._update_checks = update_checks

    async def step(self, operation: OperationRecord) -> StepResult:
        scope = TenantScope(operation.tenant_id)
        # Heartbeat is the first side effect. A stale worker must not create,
        # inspect, cancel, or mirror a Luma task after losing its database lease.
        current = await self._operations.heartbeat(
            scope,
            operation.id,
            worker_id=self._worker_id,
            lease_seconds=self._config.lease_seconds,
        )
        if current.cancel_requested and (await self._states.load(current.id)) is None:
            completed = await self._operations.complete(
                scope,
                current.id,
                worker_id=self._worker_id,
                status=OperationStatus.CANCELED,
            )
            return StepResult(StepStatus.TERMINAL, completed)

        try:
            _validate_claimed_operation(current)
            state = await self._states.load(current.id)
            if state is None:
                raise AnalyzeCheckpointMissing()
            context = await self._contexts.load(current)
            _validate_context_binding(current, context)
            bound = state.bind(context)
            if bound != state:
                state = await self._states.save(
                    bound, expected_version=state.version
                )

            call_context = LumaCallContext(
                tenant_ref=context.tenant_ref,
                application_ref=context.application_ref,
                external_operation_id=current.id,
            )
            if current.cancel_requested and state.luma_task_id is None:
                completed = await self._operations.complete(
                    scope,
                    current.id,
                    worker_id=self._worker_id,
                    status=OperationStatus.CANCELED,
                )
                return StepResult(StepStatus.TERMINAL, completed)

            if state.luma_task_id is None:
                mutation = await _call_sync(
                    self._luma.create_analyze_task,
                    call_context,
                    AnalyzeSourceRequest(
                        source=(
                            ObjectSourceReference(
                                digest=context.object_digest or "",
                                media_type=context.object_media_type or "",
                                size_bytes=context.object_size_bytes or 0,
                            )
                            if context.object_source
                            else SourceReference(
                                context.repository or "",
                                ref=context.ref,
                                subdirectory=context.subdirectory,
                            )
                        ),
                        credential_lease_id=state.credential_lease_id,
                        agent_image_digest=self._config.agent_image_digest,
                        policy_version=self._config.policy_version,
                        limits=self._config.limits,
                    ),
                    idempotency_key=_analyze_idempotency_key(current.id),
                )
                state = await self._states.save(
                    replace(
                        state,
                        luma_task_id=mutation.task.task_id,
                        luma_status=mutation.task.status,
                    ),
                    expected_version=state.version,
                )
                await self._operations.append_event(
                    scope,
                    current.id,
                    EventInput(
                        type="builder.analyze.progress",
                        phase="source.analyze",
                        status="running",
                        message="Source analysis submitted to Luma",
                        data={"replayed": mutation.replayed},
                    ),
                    worker_id=self._worker_id,
                )

            assert state.luma_task_id is not None
            if current.cancel_requested and not state.cancel_forwarded:
                canceled = await _call_sync(
                    self._luma.cancel_builder_task,
                    call_context,
                    state.luma_task_id,
                )
                state = await self._states.save(
                    replace(
                        state,
                        cancel_forwarded=True,
                        luma_status=canceled.task.status,
                    ),
                    expected_version=state.version,
                )

            try:
                page = await _call_sync(
                    self._luma.get_builder_task_events,
                    call_context,
                    state.luma_task_id,
                    after=state.luma_cursor,
                    limit=self._config.event_page_limit,
                )
            except LumaAdapterError as exc:
                if exc.code is not AdapterErrorCode.CURSOR_EXPIRED:
                    raise
                # Event retention may be shorter than a prolonged worker
                # outage. Resynchronize only to Luma's authenticated task view,
                # record the gap, then continue from its last durable cursor.
                task = await _call_sync(
                    self._luma.get_builder_task,
                    call_context,
                    state.luma_task_id,
                )
                _validate_task_binding(
                    task,
                    task_id=state.luma_task_id,
                    operation=current,
                    context=context,
                )
                if task.last_cursor < state.luma_cursor:
                    raise AnalyzeOrchestrationError() from None
                await self._operations.append_event(
                    scope,
                    current.id,
                    EventInput(
                        type="builder.analyze.progress",
                        phase="source.analyze",
                        status="running",
                        message="Builder event history expired; analysis state resynchronized",
                        data={"skippedThroughCursor": task.last_cursor},
                        level="warning",
                    ),
                    worker_id=self._worker_id,
                )
                await self._states.save(
                    replace(
                        state,
                        luma_cursor=task.last_cursor,
                        luma_status=task.status,
                    ),
                    expected_version=state.version,
                )
                return StepResult(StepStatus.WAITING, current)
            prior_cursor = state.luma_cursor
            if page.task_id != state.luma_task_id:
                raise AnalyzeOrchestrationError()
            for event in page.events:
                if event.cursor <= prior_cursor or event.cursor != event.sequence:
                    raise AnalyzeOrchestrationError()
                prior_cursor = event.cursor
            if page.next_cursor != prior_cursor:
                raise AnalyzeOrchestrationError()
            for index, event in enumerate(page.events, start=1):
                await self._operations.append_event(
                    scope,
                    current.id,
                    _safe_mirrored_event(event),
                    worker_id=self._worker_id,
                )
                # At-least-once mirroring: advance only after the durable LAE
                # event append. A crash in this narrow cross-table window can
                # repeat one safe event, identified by builderCursor, but never
                # loses progress or replays raw builder text.
                state = await self._states.save(
                    replace(
                        state,
                        luma_cursor=event.cursor,
                        luma_status=event.status or page.status,
                    ),
                    expected_version=state.version,
                )
                if index % 20 == 0:
                    current = await self._operations.heartbeat(
                        scope,
                        current.id,
                        worker_id=self._worker_id,
                        lease_seconds=self._config.lease_seconds,
                    )
            if page.has_more:
                return StepResult(StepStatus.WAITING, current)

            task = await _call_sync(
                self._luma.get_builder_task,
                call_context,
                state.luma_task_id,
            )
            _validate_task_binding(
                task,
                task_id=state.luma_task_id,
                operation=current,
                context=context,
            )
            if task.status not in {"canceled", "succeeded", "failed", "timed_out"}:
                if state.luma_status != task.status:
                    await self._states.save(
                        replace(state, luma_status=task.status),
                        expected_version=state.version,
                    )
                return StepResult(StepStatus.WAITING, current)

            # Re-check the lease and cancellation flag immediately before the
            # irreversible LAE terminal transition. Late user cancellation wins
            # even if Luma has concurrently reported success.
            current = await self._operations.heartbeat(
                scope,
                current.id,
                worker_id=self._worker_id,
                lease_seconds=self._config.lease_seconds,
            )
            if current.cancel_requested:
                if not state.cancel_forwarded:
                    await _call_sync(
                        self._luma.cancel_builder_task,
                        call_context,
                        state.luma_task_id,
                    )
                    await self._states.save(
                        replace(state, cancel_forwarded=True),
                        expected_version=state.version,
                    )
                # Cancellation is authoritative even if the upstream success
                # raced with this final heartbeat. Do not record descriptor
                # refs or activate any success result after cancellation.
                completed = await self._operations.complete(
                    scope,
                    current.id,
                    worker_id=self._worker_id,
                    status=OperationStatus.CANCELED,
                )
                return StepResult(StepStatus.TERMINAL, completed)
            if task.status == "succeeded":
                references = AnalysisDigestReferences.from_task(task)
                if references.policy_version != self._config.policy_version:
                    raise AnalyzeOrchestrationError()
                state = await self._states.save(
                    replace(
                        state,
                        luma_status=task.status,
                        digest_references=references,
                    ),
                    expected_version=state.version,
                )
                recording = await self._recorder.record(
                    current.id,
                    context,
                    references,
                    builder_task_id=state.luma_task_id,
                )
                state = await self._states.save(
                    replace(state, recording=recording),
                    expected_version=state.version,
                )
                result = {
                    "sourceRevisionId": context.source_revision_ref,
                    "analysisStatus": recording.analysis_status,
                    "artifactState": recording.artifact_state,
                    "planStored": recording.plan_stored,
                    **references.to_result(),
                }
                if current.kind == "application.check-update":
                    if self._update_checks is None:
                        raise UpdateCheckResultInvalid()
                    update_check = await self._update_checks.resolve(
                        current, context
                    )
                    if not isinstance(update_check, UpdateCheckResult):
                        raise UpdateCheckResultInvalid()
                    result["updateCheck"] = update_check.to_body()
                completed = await self._operations.complete(
                    scope,
                    current.id,
                    worker_id=self._worker_id,
                    status=OperationStatus.SUCCEEDED,
                    result=result,
                )
            elif task.status == "canceled":
                completed = await self._operations.complete(
                    scope,
                    current.id,
                    worker_id=self._worker_id,
                    status=OperationStatus.CANCELED,
                )
            else:
                error_code = (
                    "LAE_ANALYSIS_TIMED_OUT"
                    if task.status == "timed_out"
                    else "LAE_ANALYSIS_FAILED"
                )
                completed = await self._operations.complete(
                    scope,
                    current.id,
                    worker_id=self._worker_id,
                    status=OperationStatus.FAILED,
                    error_code=error_code,
                    error_message="Luma source analysis did not complete successfully.",
                )
            return StepResult(StepStatus.TERMINAL, completed)
        except LeaseLost:
            raise
        except ArtifactIngestCanceled:
            completed = await self._operations.complete(
                scope,
                current.id,
                worker_id=self._worker_id,
                status=OperationStatus.CANCELED,
            )
            return StepResult(StepStatus.TERMINAL, completed)
        except LumaAdapterError as exc:
            if exc.retryable:
                return StepResult(StepStatus.WAITING, current)
            completed = await self._operations.complete(
                scope,
                current.id,
                worker_id=self._worker_id,
                status=OperationStatus.FAILED,
                error_code=exc.code.value,
                error_message=str(exc),
            )
            return StepResult(StepStatus.TERMINAL, completed)
        except AnalyzeOrchestrationError as exc:
            completed = await self._operations.complete(
                scope,
                current.id,
                worker_id=self._worker_id,
                status=OperationStatus.FAILED,
                error_code=exc.code,
                error_message=exc.public_message,
            )
            return StepResult(StepStatus.TERMINAL, completed)


class AnalyzeWorker:
    def __init__(
        self,
        operations: OperationStore,
        runner: AnalyzeStepRunner,
        *,
        worker_id: str,
        config: AnalyzeWorkerConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._operations = operations
        self._runner = runner
        self._worker_id = worker_id
        self._config = config
        self._sleep = sleep

    async def run_once(self) -> StepResult | None:
        operation = await self._operations.claim_next(
            worker_id=self._worker_id,
            kinds=["source.analyze", "application.check-update"],
            lease_seconds=self._config.lease_seconds,
        )
        if operation is None:
            return None
        while True:
            result = await self._runner.step(operation)
            operation = result.operation
            if result.status is StepStatus.TERMINAL:
                return result
            await self._sleep(self._config.poll_interval_seconds)


def _validate_claimed_operation(operation: OperationRecord) -> None:
    source_analysis = (
        operation.kind == "source.analyze"
        and operation.target_type == "source-revision"
        and operation.target_id.startswith("src_")
    )
    update_check = (
        operation.kind == "application.check-update"
        and operation.target_type == "application"
        and operation.target_id.startswith("app_")
    )
    if (
        not (source_analysis or update_check)
        or operation.status != OperationStatus.RUNNING.value
    ):
        raise AnalyzeContextInvalid()


def _validate_context_binding(
    operation: OperationRecord, context: AnalyzeSourceContext
) -> None:
    if (
        context.tenant_ref != operation.tenant_id
        or (
            operation.kind == "source.analyze"
            and context.source_revision_ref != operation.target_id
        )
        or (
            operation.kind == "application.check-update"
            and context.application_ref != operation.target_id
        )
    ):
        raise AnalyzeContextInvalid()


def _validate_task_binding(
    task: BuilderTask,
    *,
    task_id: str,
    operation: OperationRecord,
    context: AnalyzeSourceContext,
) -> None:
    if (
        task.task_id != task_id
        or task.kind != "analyze-source"
        or task.external_operation_id != operation.id
        or task.tenant_ref != context.tenant_ref
        or task.application_ref != context.application_ref
    ):
        raise AnalyzeContextInvalid()


def _validate_repository(repository: str) -> None:
    if (
        not isinstance(repository, str)
        or not repository
        or len(repository) > 2048
        or any(character in repository for character in "\x00\r\n")
    ):
        raise AnalyzeContextInvalid()
    parsed = urllib.parse.urlparse(repository)
    # The current Luma analyze executor clones HTTPS only. Keep SSH and
    # shorthand repositories fail-closed until the credential broker/executor
    # implements and tests them end to end.
    if parsed.scheme != "https" or not parsed.hostname:
        raise AnalyzeContextInvalid()
    if parsed.username or parsed.password:
        raise AnalyzeContextInvalid()
    if any(
        _SECRET_QUERY_KEY.search(key)
        for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise AnalyzeContextInvalid()


def _analyze_idempotency_key(operation_id: str) -> str:
    return f"lae:{operation_id}:source-analyze:v1"


_MIRRORED_EVENTS: dict[str, tuple[str, str, str]] = {
    "status": (
        "builder.analyze.progress",
        "source.analyze",
        "Source analysis state updated",
    ),
    "source.fetch": (
        "builder.fetch.started",
        "source.fetch",
        "Source fetch updated",
    ),
    "source.snapshot": (
        "builder.analyze.progress",
        "source.analyze",
        "Source snapshot updated",
    ),
    "analysis": (
        "builder.analyze.progress",
        "source.analyze",
        "Source analysis updated",
    ),
    "complete": (
        "builder.analyze.progress",
        "source.analyze",
        "Source analysis completion updated",
    ),
}


def _safe_mirrored_event(event: BuilderTaskEvent) -> EventInput:
    event_type, phase, message = _MIRRORED_EVENTS.get(
        event.event_type,
        (
            "builder.analyze.progress",
            "source.analyze",
            "Source analysis progress updated",
        ),
    )
    # Never copy event.message. Even though the adapter already normalizes it,
    # this worker boundary remains safe if a future/mock adapter is defective.
    status = (event.status or "running").replace("_", "-")
    return EventInput(
        type=event_type,
        phase=phase,
        status="running",
        message=message,
        data={
            "builderCursor": event.cursor,
            "builderEvent": event.event_type
            if event.event_type in _MIRRORED_EVENTS
            else "update",
            "builderStatus": status,
        },
    )


async def _call_sync(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(function, *args, **kwargs)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise AnalyzeOrchestrationError()
    return item


def _required_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise AnalyzeOrchestrationError()
    return item
