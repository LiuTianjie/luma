from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from lae_store import LeaseLost, OperationStore, TenantScope
from lae_store.ids import require_opaque_id
from lae_store.models import (
    Analysis,
    AnalysisArtifact,
    Artifact,
    BuilderTask,
    Operation,
)

from .analyze import (
    AnalysisDigestReferences,
    AnalysisRecording,
    AnalyzeContextInvalid,
    AnalyzeSourceContext,
    ArtifactDescriptor,
    AnalyzeStateConflict,
    OrchestrationSchemaUnavailable,
)
from .artifact_ingest import (
    ArtifactIngestCanceled,
    ArtifactIngestGuard,
    ArtifactIntegrityError,
    ArtifactTransferBinding,
    ArtifactPersistenceState,
    StoredObject,
    object_key_for,
)
from .postgres import PostgresAnalysisRecorder


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SCHEMA_SQLSTATES = {"42P01", "42703"}
_ARTIFACT_KIND = {
    "evidence": "evidence",
    "deploymentPlan": "deployment-plan",
    "buildPlan": "build-plan-candidate",
}


def _schema_unavailable(exc: DBAPIError) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if getattr(current, "sqlstate", None) in _SCHEMA_SQLSTATES:
            return True
        current = current.__cause__
    return False


def _raise_database_error(exc: DBAPIError) -> None:
    if _schema_unavailable(exc):
        raise OrchestrationSchemaUnavailable() from None
    raise exc


class PostgresAnalysisArtifactCatalog:
    """Tenant/task/worker-fenced durable artifact ingest state machine."""

    def __init__(
        self,
        sessions: Any,
        *,
        agent_image_digest: str,
        worker_id: str,
    ) -> None:
        if not isinstance(agent_image_digest, str) or not _IMAGE_DIGEST.fullmatch(
            agent_image_digest
        ):
            raise ValueError("agent_image_digest must be immutable")
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id has invalid format")
        self._sessions = sessions
        self._agent_image_digest = agent_image_digest
        self._worker_id = worker_id
        self._descriptor_recorder = PostgresAnalysisRecorder(
            sessions, agent_image_digest=agent_image_digest
        )

    async def prepare_analysis(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str,
    ) -> AnalysisRecording:
        require_opaque_id(operation_id, prefix="op")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=False
                    )
                    await self._require_builder_task(
                        session,
                        operation_id,
                        context,
                        builder_task_id=builder_task_id,
                        references=references,
                    )
                    analysis = await session.scalar(
                        select(Analysis)
                        .where(
                            Analysis.tenant_id == context.tenant_ref,
                            Analysis.operation_id == operation_id,
                        )
                        .with_for_update()
                    )
                    if analysis is not None and analysis.artifact_state == "stored":
                        self._validate_analysis(
                            analysis, context=context, references=references
                        )
                        return self._recording(analysis)

            # Reuse the descriptor recorder as the one authority for queued
            # analysis/source materialization. It does not transfer bytes.
            await self._descriptor_recorder.record(
                operation_id,
                context,
                references,
                builder_task_id=builder_task_id,
            )

            # Recheck worker ownership and immutable task binding before any
            # external byte transfer. A stale worker may at most have written
            # the same descriptor facts; it cannot redeem a lease or store.
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=False
                    )
                    await self._require_builder_task(
                        session,
                        operation_id,
                        context,
                        builder_task_id=builder_task_id,
                        references=references,
                    )
                    analysis = await session.scalar(
                        select(Analysis)
                        .where(
                            Analysis.tenant_id == context.tenant_ref,
                            Analysis.operation_id == operation_id,
                        )
                        .with_for_update()
                    )
                    if analysis is None:
                        raise AnalyzeContextInvalid()
                    self._validate_analysis(
                        analysis, context=context, references=references
                    )
                    return self._recording(analysis)
        except IntegrityError as exc:
            raise AnalyzeStateConflict() from exc
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def get_artifact_state(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=False
                    )
                    _analysis, artifact = await self._load_artifact(
                        session,
                        operation_id,
                        context,
                        descriptor,
                        for_update=False,
                    )
                    return self._artifact_state(artifact)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def mark_artifact_uploading(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=False
                    )
                    _analysis, artifact = await self._load_artifact(
                        session,
                        operation_id,
                        context,
                        descriptor,
                        for_update=True,
                    )
                    if artifact.upload_status != "verified":
                        artifact.upload_status = "uploading"
                        artifact.storage_key = None
                        artifact.verified_at = None
                    await session.flush()
                    return self._artifact_state(artifact)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def mark_artifact_verified(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
        stored: StoredObject,
    ) -> ArtifactPersistenceState:
        expected_key = object_key_for(context.tenant_ref, descriptor)
        if (
            stored.key != expected_key
            or stored.media_type != descriptor.media_type
            or stored.size_bytes != descriptor.size_bytes
            or stored.digest != descriptor.digest
        ):
            raise ArtifactIntegrityError()
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=False
                    )
                    _analysis, artifact = await self._load_artifact(
                        session,
                        operation_id,
                        context,
                        descriptor,
                        for_update=True,
                    )
                    if artifact.upload_status == "verified":
                        if (
                            artifact.storage_key != expected_key
                            or artifact.verified_at is None
                        ):
                            raise ArtifactIntegrityError()
                    else:
                        artifact.storage_key = expected_key
                        artifact.upload_status = "verified"
                        artifact.verified_at = datetime.now(timezone.utc)
                    await session.flush()
                    return self._artifact_state(artifact)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def mark_artifact_failed(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=True
                    )
                    _analysis, artifact = await self._load_artifact(
                        session,
                        operation_id,
                        context,
                        descriptor,
                        for_update=True,
                    )
                    if artifact.upload_status != "verified":
                        artifact.upload_status = "failed"
                        artifact.storage_key = None
                        artifact.verified_at = None
                    await session.flush()
                    return self._artifact_state(artifact)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def finalize_stored_analysis(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
    ) -> AnalysisRecording:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_active_operation(
                        session, operation_id, context, allow_cancel=False
                    )
                    analysis = await session.scalar(
                        select(Analysis)
                        .where(
                            Analysis.tenant_id == context.tenant_ref,
                            Analysis.application_id == context.application_ref,
                            Analysis.source_revision_id
                            == context.source_revision_ref,
                            Analysis.operation_id == operation_id,
                        )
                        .with_for_update()
                    )
                    if analysis is None:
                        raise AnalyzeContextInvalid()
                    self._validate_analysis(
                        analysis, context=context, references=references
                    )
                    for descriptor in references.artifacts:
                        _same_analysis, artifact = await self._load_artifact(
                            session,
                            operation_id,
                            context,
                            descriptor,
                            for_update=True,
                        )
                        expected_key = object_key_for(
                            context.tenant_ref, descriptor
                        )
                        if (
                            artifact.upload_status != "verified"
                            or artifact.storage_key != expected_key
                            or artifact.verified_at is None
                        ):
                            raise ArtifactIntegrityError()
                    analysis.artifact_state = "stored"
                    analysis.plan_stored = True
                    await session.flush()
                    return self._recording(analysis)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def _require_active_operation(
        self,
        session: Any,
        operation_id: str,
        context: AnalyzeSourceContext,
        *,
        allow_cancel: bool,
    ) -> Operation:
        predicates = [
            Operation.tenant_id == context.tenant_ref,
            Operation.id == operation_id,
            Operation.status == "running",
            Operation.lease_owner == self._worker_id,
            Operation.lease_expires_at > func.now(),
        ]
        if not allow_cancel:
            predicates.append(Operation.cancel_requested_at.is_(None))
        operation = await session.scalar(
            select(Operation).where(*predicates).with_for_update()
        )
        if operation is None or not (
            (
                operation.kind == "source.analyze"
                and operation.target_type == "source-revision"
                and operation.target_id == context.source_revision_ref
            )
            or (
                operation.kind == "application.check-update"
                and operation.target_type == "application"
                and operation.target_id == context.application_ref
            )
        ):
            raise AnalyzeContextInvalid()
        return operation

    async def _require_builder_task(
        self,
        session: Any,
        operation_id: str,
        context: AnalyzeSourceContext,
        *,
        builder_task_id: str,
        references: AnalysisDigestReferences,
    ) -> BuilderTask:
        task = await session.scalar(
            select(BuilderTask)
            .where(
                BuilderTask.tenant_id == context.tenant_ref,
                BuilderTask.application_id == context.application_ref,
                BuilderTask.source_revision_id == context.source_revision_ref,
                BuilderTask.operation_id == operation_id,
                BuilderTask.action == "source.analyze",
                BuilderTask.luma_task_id == builder_task_id,
                BuilderTask.upstream_status == "succeeded",
            )
            .with_for_update()
        )
        if task is None:
            raise AnalyzeContextInvalid()
        result = task.result_descriptor_json
        if (
            not isinstance(result, dict)
            or not set(result).issubset({"digestReferences", "recording"})
            or result.get("digestReferences") != references.to_result()
        ):
            raise ArtifactIntegrityError()
        return task

    async def _load_artifact(
        self,
        session: Any,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
        *,
        for_update: bool,
    ) -> tuple[Analysis, Artifact]:
        analysis_statement = select(Analysis).where(
            Analysis.tenant_id == context.tenant_ref,
            Analysis.application_id == context.application_ref,
            Analysis.source_revision_id == context.source_revision_ref,
            Analysis.operation_id == operation_id,
        )
        if for_update:
            analysis_statement = analysis_statement.with_for_update()
        analysis = await session.scalar(analysis_statement)
        if analysis is None:
            raise AnalyzeContextInvalid()
        link = await session.scalar(
            select(AnalysisArtifact).where(
                AnalysisArtifact.tenant_id == context.tenant_ref,
                AnalysisArtifact.analysis_id == analysis.id,
                AnalysisArtifact.name == descriptor.name,
            )
        )
        if link is None:
            raise ArtifactIntegrityError()
        artifact_statement = select(Artifact).where(
            Artifact.tenant_id == context.tenant_ref,
            Artifact.id == link.artifact_id,
            Artifact.kind == _ARTIFACT_KIND[descriptor.name],
            Artifact.digest == descriptor.digest,
            Artifact.media_type == descriptor.media_type,
            Artifact.size_bytes == descriptor.size_bytes,
        )
        if for_update:
            artifact_statement = artifact_statement.with_for_update()
        artifact = await session.scalar(artifact_statement)
        if artifact is None:
            raise ArtifactIntegrityError()
        return analysis, artifact

    def _validate_analysis(
        self,
        analysis: Analysis,
        *,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
    ) -> None:
        expected = (
            context.application_ref,
            context.source_revision_ref,
            references.policy_version,
            self._agent_image_digest,
            references.resolved_commit,
            references.source_tree_digest,
            references.source_snapshot_id,
            references.source_snapshot_digest,
            references.deployment_plan_digest,
            references.build_plan_digest,
            references.evidence_digest,
        )
        actual = (
            analysis.application_id,
            analysis.source_revision_id,
            analysis.policy_version,
            analysis.agent_image_digest,
            analysis.resolved_commit_full,
            analysis.source_tree_digest,
            analysis.source_snapshot_id,
            analysis.source_snapshot_digest,
            analysis.deployment_plan_digest,
            analysis.build_plan_digest,
            analysis.evidence_digest,
        )
        if (
            actual != expected
            or analysis.status
            not in {
                "analyzed",
                "deployable",
                "needs_configuration",
                "not_deployable",
            }
            or analysis.artifact_state not in {"descriptor-only", "stored"}
            or analysis.plan_stored != (analysis.artifact_state == "stored")
        ):
            raise ArtifactIntegrityError()

    @staticmethod
    def _artifact_state(artifact: Artifact) -> ArtifactPersistenceState:
        return ArtifactPersistenceState(
            artifact.upload_status,
            artifact.storage_key,
        )

    @staticmethod
    def _recording(analysis: Analysis) -> AnalysisRecording:
        return AnalysisRecording(
            analysis_status=analysis.status,
            artifact_state=analysis.artifact_state,
            plan_stored=analysis.plan_stored,
        )


class PostgresArtifactIngestGuard(ArtifactIngestGuard):
    """Heartbeat the durable operation lease at every transfer checkpoint."""

    def __init__(
        self,
        operations: OperationStore,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id has invalid format")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        self._operations = operations
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    async def checkpoint(self, binding: ArtifactTransferBinding) -> None:
        try:
            operation = await self._operations.heartbeat(
                TenantScope(binding.tenant_ref),
                binding.operation_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
        except LeaseLost:
            raise ArtifactIngestCanceled() from None
        if operation.cancel_requested:
            raise ArtifactIngestCanceled()


__all__ = [
    "PostgresAnalysisArtifactCatalog",
    "PostgresArtifactIngestGuard",
]
