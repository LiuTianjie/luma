from __future__ import annotations

import re
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import func, null, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from lae_store.ids import new_id, require_opaque_id
from lae_store.models import (
    Analysis,
    AnalysisArtifact,
    AppRevision,
    Application,
    ApplicationLifecycleRequest,
    Artifact,
    BuilderTask,
    Deployment,
    Operation,
    SourceCredentialLease,
    SourceRevision,
)
from lae_store.security import ensure_persistable_payload
from lae_store.repositories import OperationRecord
from lae_store.tokens import keyed_request_hash, keyed_secret_hash
from lae_store.update_checks import UpdateCheckResult

from .analyze import (
    AnalysisDigestReferences,
    AnalysisRecording,
    AnalyzeContextInvalid,
    AnalyzeOrchestrationError,
    AnalyzeOrchestrationState,
    AnalyzeSourceContext,
    AnalyzeStateConflict,
    ArtifactDescriptor,
    OrchestrationSchemaUnavailable,
    UpdateCheckResultInvalid,
    _analyze_idempotency_key,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_CREDENTIAL_LEASE_ID = re.compile(
    r"^(?:lease_[0-9A-HJKMNP-TV-Z]{26}|cl_[A-Za-z0-9][A-Za-z0-9._-]{7,124})$"
)
_SCHEMA_SQLSTATES = {"42P01", "42703"}
_ARTIFACT_KIND = {
    "evidence": "evidence",
    "deploymentPlan": "deployment-plan",
    "buildPlan": "build-plan-candidate",
}
_UPSTREAM_PREDECESSORS: dict[str, frozenset[str | None]] = {
    "queued": frozenset({None, "queued"}),
    "running": frozenset({None, "queued", "running"}),
    "cancel_requested": frozenset(
        {None, "queued", "running", "cancel_requested"}
    ),
    "succeeded": frozenset({None, "queued", "running", "succeeded"}),
    "failed": frozenset(
        {None, "queued", "running", "cancel_requested", "failed"}
    ),
    "timed_out": frozenset(
        {None, "queued", "running", "cancel_requested", "timed_out"}
    ),
    "canceled": frozenset(
        {None, "queued", "running", "cancel_requested", "canceled"}
    ),
}


def _require_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} has invalid format")
    return value


def _operation_matches_context(
    operation: Operation, context: AnalyzeSourceContext
) -> bool:
    return (
        operation.kind == "source.analyze"
        and operation.target_type == "source-revision"
        and operation.target_id == context.source_revision_ref
    ) or (
        operation.kind == "application.check-update"
        and operation.target_type == "application"
        and operation.target_id == context.application_ref
    )


def _canonical_allowed_host(repository: str) -> str:
    parsed = urllib.parse.urlparse(repository)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AnalyzeContextInvalid() from exc
    if parsed.hostname is None:
        raise AnalyzeContextInvalid()
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    # An explicit :443 and the implicit HTTPS default are the same origin.
    return host if port in {None, 443} else f"{host}:{port}"


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


def _serialize_result(
    references: AnalysisDigestReferences | None,
    recording: AnalysisRecording | None,
) -> dict[str, object] | None:
    value: dict[str, object] = {}
    if references is not None:
        value["digestReferences"] = references.to_result()
    if recording is not None:
        value["recording"] = {
            "analysisStatus": recording.analysis_status,
            "artifactState": recording.artifact_state,
            "planStored": recording.plan_stored,
        }
    if not value:
        return None
    ensure_persistable_payload(value)
    return value


def _mapping(value: object, *, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AnalyzeOrchestrationError()
    return value


def _optional_result_string(
    value: Mapping[str, Any], key: str, default: str
) -> str:
    item = value.get(key, default)
    if not isinstance(item, str) or not item:
        raise AnalyzeOrchestrationError()
    return item


def _deserialize_result(
    value: object,
) -> tuple[AnalysisDigestReferences | None, AnalysisRecording | None]:
    if value is None:
        return None, None
    if not isinstance(value, Mapping) or not set(value).issubset(
        {"digestReferences", "recording"}
    ):
        raise AnalyzeOrchestrationError()

    references: AnalysisDigestReferences | None = None
    raw_references = value.get("digestReferences")
    if raw_references is not None:
        required_reference_fields = {
            "resolvedCommit",
            "sourceTreeDigest",
            "sourceSnapshotId",
            "sourceSnapshotDigest",
            "deploymentPlanDigest",
            "buildPlanDigest",
            "evidenceDigest",
            "policyVersion",
            "artifactDescriptors",
        }
        diagnostic_reference_fields = {
            "verdict",
            "diagnosticStatus",
            "diagnosticMode",
            "diagnosticCode",
            "knowledgeVersion",
            "blockers",
        }
        if (
            not isinstance(raw_references, Mapping)
            or not required_reference_fields.issubset(raw_references)
            or not set(raw_references).issubset(
                required_reference_fields | diagnostic_reference_fields
            )
        ):
            raise AnalyzeOrchestrationError()
        raw = raw_references
        raw_artifacts = raw["artifactDescriptors"]
        artifacts_map = _mapping(
            raw_artifacts, fields={"evidence", "deploymentPlan", "buildPlan"}
        )
        descriptors: list[ArtifactDescriptor] = []
        for name in ("evidence", "deploymentPlan", "buildPlan"):
            descriptor = _mapping(
                artifacts_map[name], fields={"digest", "mediaType", "sizeBytes"}
            )
            descriptors.append(
                ArtifactDescriptor(
                    name=name,
                    digest=descriptor["digest"],
                    media_type=descriptor["mediaType"],
                    size_bytes=descriptor["sizeBytes"],
                )
            )
        raw_blockers = raw.get("blockers", [])
        if not isinstance(raw_blockers, list):
            raise AnalyzeOrchestrationError()
        references = AnalysisDigestReferences(
            resolved_commit=raw["resolvedCommit"],
            source_tree_digest=raw["sourceTreeDigest"],
            source_snapshot_id=raw["sourceSnapshotId"],
            source_snapshot_digest=raw["sourceSnapshotDigest"],
            deployment_plan_digest=raw["deploymentPlanDigest"],
            build_plan_digest=raw["buildPlanDigest"],
            evidence_digest=raw["evidenceDigest"],
            policy_version=raw["policyVersion"],
            artifacts=tuple(descriptors),
            verdict=_optional_result_string(raw, "verdict", "diagnostic_failed"),
            diagnostic_status=_optional_result_string(
                raw, "diagnosticStatus", "diagnostic_failed"
            ),
            diagnostic_mode=_optional_result_string(
                raw, "diagnosticMode", "deterministic_fallback"
            ),
            diagnostic_code=_optional_result_string(
                raw, "diagnosticCode", "LEGACY_ANALYSIS_RESULT"
            ),
            knowledge_version=_optional_result_string(
                raw, "knowledgeVersion", "legacy"
            ),
            blockers=tuple(raw_blockers),
        )

    recording: AnalysisRecording | None = None
    raw_recording = value.get("recording")
    if raw_recording is not None:
        item = _mapping(
            raw_recording,
            fields={"analysisStatus", "artifactState", "planStored"},
        )
        recording = AnalysisRecording(
            analysis_status=item["analysisStatus"],
            artifact_state=item["artifactState"],
            plan_stored=item["planStored"],
        )
    if recording is not None and references is None:
        raise AnalyzeOrchestrationError()
    return references, recording


def _state_from_task(task: BuilderTask) -> AnalyzeOrchestrationState:
    references, recording = _deserialize_result(task.result_descriptor_json)
    return AnalyzeOrchestrationState(
        operation_id=task.operation_id,
        credential_lease_id=task.credential_lease_id,
        version=task.checkpoint_version,
        tenant_ref=task.tenant_id,
        application_ref=task.application_id,
        source_revision_ref=task.source_revision_id,
        luma_task_id=task.luma_task_id,
        luma_cursor=task.event_cursor,
        luma_status=task.upstream_status,
        cancel_forwarded=task.cancel_forwarded_at is not None,
        digest_references=references,
        recording=recording,
    )


class PostgresAnalyzeStateStore:
    """Tenant-fenced, CAS-updated source analysis checkpoint store."""

    def __init__(
        self,
        sessions: Any,
        *,
        luma_cluster_id: str,
        luma_principal_id: str,
        hash_key: bytes,
        hash_key_version: int = 1,
        credential_lease_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._sessions = sessions
        self._cluster = _require_identifier(
            luma_cluster_id, field="luma_cluster_id"
        )
        self._principal = _require_identifier(
            luma_principal_id, field="luma_principal_id"
        )
        if not isinstance(hash_key, bytes) or len(hash_key) < 32:
            raise ValueError("checkpoint HMAC key must contain at least 256 bits")
        if not isinstance(hash_key_version, int) or hash_key_version < 1:
            raise ValueError("checkpoint HMAC key version must be positive")
        if not timedelta(minutes=1) <= credential_lease_ttl <= timedelta(hours=1):
            raise ValueError("credential lease TTL must be between 1 and 60 minutes")
        self._hash_key = hash_key
        self._hash_key_version = hash_key_version
        self._credential_lease_ttl = credential_lease_ttl

    async def initialize(
        self, operation_id: str, *, credential_lease_id: str
    ) -> AnalyzeOrchestrationState:
        require_opaque_id(operation_id, prefix="op")
        # The identifier is public opaque metadata, but reject credential-like
        # values so a caller cannot accidentally persist a PAT in this column.
        ensure_persistable_payload({"credentialLeaseId": credential_lease_id})
        if (
            not isinstance(credential_lease_id, str)
            or not _CREDENTIAL_LEASE_ID.fullmatch(credential_lease_id)
            or len(credential_lease_id) > 128
        ):
            raise ValueError("credential_lease_id must be an opaque lease identifier")
        candidate = AnalyzeOrchestrationState(
            operation_id=operation_id,
            credential_lease_id=credential_lease_id,
        )
        idempotency_hash = keyed_secret_hash(
            _analyze_idempotency_key(operation_id),
            self._hash_key,
            domain="lae.builder-idempotency.v1",
        )
        bound_candidate: AnalyzeOrchestrationState | None = None
        request_digest: bytes | None = None
        try:
            async with self._sessions() as session:
                async with session.begin():
                    operation = await session.scalar(
                        select(Operation)
                        .where(Operation.id == operation_id)
                        .with_for_update()
                    )
                    if operation is None or operation.status not in {
                        "queued",
                        "running",
                    }:
                        raise AnalyzeContextInvalid()
                    existing = await session.scalar(
                        select(BuilderTask)
                        .where(
                            BuilderTask.tenant_id == operation.tenant_id,
                            BuilderTask.operation_id == operation.id,
                            BuilderTask.action == "source.analyze",
                        )
                        .with_for_update()
                    )
                    if (
                        operation.kind == "source.analyze"
                        and operation.target_type == "source-revision"
                    ):
                        source_id = operation.target_id
                    elif (
                        operation.kind == "application.check-update"
                        and operation.target_type == "application"
                        and existing is not None
                        and existing.application_id == operation.target_id
                    ):
                        source_id = existing.source_revision_id
                    else:
                        raise AnalyzeContextInvalid()
                    source = await session.scalar(
                        select(SourceRevision).where(
                            SourceRevision.tenant_id == operation.tenant_id,
                            SourceRevision.id == source_id,
                            SourceRevision.deleted_at.is_(None),
                        )
                    )
                    if (
                        source is None
                        or source.application_id is None
                        or source.kind != "git"
                        or not source.repository
                    ):
                        raise AnalyzeContextInvalid()
                    application = await session.scalar(
                        select(Application).where(
                            Application.tenant_id == operation.tenant_id,
                            Application.id == source.application_id,
                            Application.deleted_at.is_(None),
                        )
                    )
                    if application is None:
                        raise AnalyzeContextInvalid()
                    if (
                        operation.kind == "application.check-update"
                        and application.id != operation.target_id
                    ):
                        raise AnalyzeContextInvalid()
                    source_context = AnalyzeSourceContext(
                        tenant_ref=operation.tenant_id,
                        application_ref=application.id,
                        source_revision_ref=source.id,
                        repository=source.repository,
                        ref=source.ref,
                        subdirectory=source.subdirectory or None,
                    )
                    allowed_host = _canonical_allowed_host(
                        source_context.repository
                    )
                    bound = replace(
                        candidate,
                        tenant_ref=operation.tenant_id,
                        application_ref=application.id,
                        source_revision_ref=source.id,
                    )
                    bound_candidate = bound
                    request_digest = keyed_request_hash(
                        {
                            "action": "source.analyze",
                            "tenantRef": operation.tenant_id,
                            "applicationRef": application.id,
                            "sourceRevisionRef": source.id,
                            "repository": source.repository,
                            "ref": source.ref,
                            "subdirectory": source.subdirectory,
                            "credentialLeaseRef": credential_lease_id,
                        },
                        self._hash_key,
                    )
                    if existing is not None:
                        self._validate_existing(
                            existing,
                            bound,
                            idempotency_hash=idempotency_hash,
                            request_digest=request_digest,
                        )
                        return _state_from_task(existing)

                    if source.connection_id is not None:
                        # Private source leases must be issued atomically by
                        # the source-connection-aware API store so their HMAC
                        # consumer binding cannot be skipped by a legacy
                        # checkpoint initializer.
                        raise AnalyzeStateConflict()

                    task = BuilderTask(
                        id=new_id("btask"),
                        tenant_id=operation.tenant_id,
                        application_id=application.id,
                        source_revision_id=source.id,
                        operation_id=operation.id,
                        luma_cluster_id=self._cluster,
                        luma_principal_id=self._principal,
                        action="source.analyze",
                        credential_lease_id=credential_lease_id,
                        idempotency_key_hash=idempotency_hash,
                        request_digest=request_digest,
                        hash_key_version=self._hash_key_version,
                        event_cursor=0,
                        checkpoint_version=0,
                    )
                    session.add(task)
                    await session.flush()
                    now = datetime.now(timezone.utc)
                    session.add(
                        SourceCredentialLease(
                            id=credential_lease_id,
                            tenant_id=operation.tenant_id,
                            source_connection_id=source.connection_id,
                            source_revision_id=source.id,
                            operation_id=operation.id,
                            builder_task_id=task.id,
                            allowed_action="source.fetch",
                            allowed_host=allowed_host,
                            consumer_id=self._principal,
                            consumer_binding_hash=None,
                            binding_key_version=None,
                            status="issued",
                            expires_at=now + self._credential_lease_ttl,
                        )
                    )
                    await session.flush()
                    return _state_from_task(task)
        except IntegrityError:
            # A concurrent initializer can win either unique scope. Reload only
            # the operation-bound row and accept it after full binding checks.
            if bound_candidate is None or request_digest is None:
                raise AnalyzeStateConflict() from None
            try:
                async with self._sessions() as session:
                    existing = await session.scalar(
                        select(BuilderTask).where(
                            BuilderTask.operation_id == operation_id,
                            BuilderTask.action == "source.analyze",
                            BuilderTask.luma_cluster_id == self._cluster,
                            BuilderTask.luma_principal_id == self._principal,
                        )
                    )
                if existing is None:
                    raise AnalyzeStateConflict() from None
                self._validate_existing(
                    existing,
                    bound_candidate,
                    idempotency_hash=idempotency_hash,
                    request_digest=request_digest,
                )
                return _state_from_task(existing)
            except DBAPIError as exc:
                _raise_database_error(exc)
                raise AssertionError("unreachable")
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    def _validate_existing(
        self,
        task: BuilderTask,
        state: AnalyzeOrchestrationState,
        *,
        idempotency_hash: bytes,
        request_digest: bytes,
    ) -> None:
        if (
            task.tenant_id != state.tenant_ref
            or task.application_id != state.application_ref
            or task.source_revision_id != state.source_revision_ref
            or task.credential_lease_id != state.credential_lease_id
            or task.luma_cluster_id != self._cluster
            or task.luma_principal_id != self._principal
            or task.hash_key_version != self._hash_key_version
            or task.idempotency_key_hash != idempotency_hash
            or task.request_digest != request_digest
        ):
            raise AnalyzeStateConflict()

    async def load(self, operation_id: str) -> AnalyzeOrchestrationState | None:
        require_opaque_id(operation_id, prefix="op")
        try:
            async with self._sessions() as session:
                task = await session.scalar(
                    select(BuilderTask).where(
                        BuilderTask.operation_id == operation_id,
                        BuilderTask.action == "source.analyze",
                        BuilderTask.luma_cluster_id == self._cluster,
                        BuilderTask.luma_principal_id == self._principal,
                    )
                )
                return None if task is None else _state_from_task(task)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def save(
        self,
        state: AnalyzeOrchestrationState,
        *,
        expected_version: int,
    ) -> AnalyzeOrchestrationState:
        if state.version != expected_version or expected_version < 0:
            raise AnalyzeStateConflict()
        if (
            state.tenant_ref is None
            or state.application_ref is None
            or state.source_revision_ref is None
        ):
            raise AnalyzeContextInvalid()
        if state.recording is not None and state.digest_references is None:
            raise AnalyzeOrchestrationError()
        if state.luma_status is not None and state.luma_status not in (
            _UPSTREAM_PREDECESSORS
        ):
            raise AnalyzeOrchestrationError()
        descriptor = _serialize_result(
            state.digest_references,
            state.recording,
        )
        task_binding = (
            BuilderTask.luma_task_id.is_(None)
            if state.luma_task_id is None
            else or_(
                BuilderTask.luma_task_id.is_(None),
                BuilderTask.luma_task_id == state.luma_task_id,
            )
        )
        cancellation_binding = (
            BuilderTask.cancel_forwarded_at.is_(None)
            if not state.cancel_forwarded
            else True
        )
        if state.luma_status is None:
            status_binding = BuilderTask.upstream_status.is_(None)
        else:
            predecessors = _UPSTREAM_PREDECESSORS[state.luma_status]
            nonnull_predecessors = tuple(
                item for item in predecessors if item is not None
            )
            status_binding = or_(
                BuilderTask.upstream_status.is_(None),
                BuilderTask.upstream_status.in_(nonnull_predecessors),
            )
        if descriptor is None:
            descriptor_binding = BuilderTask.result_descriptor_json.is_(None)
        elif state.recording is None:
            descriptor_binding = or_(
                BuilderTask.result_descriptor_json.is_(None),
                BuilderTask.result_descriptor_json == descriptor,
            )
        else:
            references_only = _serialize_result(state.digest_references, None)
            descriptor_binding = or_(
                BuilderTask.result_descriptor_json.is_(None),
                BuilderTask.result_descriptor_json == references_only,
                BuilderTask.result_descriptor_json == descriptor,
            )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    task = await session.scalar(
                        update(BuilderTask)
                        .where(
                            BuilderTask.operation_id == state.operation_id,
                            BuilderTask.tenant_id == state.tenant_ref,
                            BuilderTask.application_id == state.application_ref,
                            BuilderTask.source_revision_id
                            == state.source_revision_ref,
                            BuilderTask.luma_cluster_id == self._cluster,
                            BuilderTask.luma_principal_id == self._principal,
                            BuilderTask.credential_lease_id
                            == state.credential_lease_id,
                            BuilderTask.action == "source.analyze",
                            BuilderTask.checkpoint_version == expected_version,
                            BuilderTask.event_cursor <= state.luma_cursor,
                            task_binding,
                            cancellation_binding,
                            status_binding,
                            descriptor_binding,
                        )
                        .values(
                            luma_task_id=state.luma_task_id,
                            event_cursor=state.luma_cursor,
                            upstream_status=state.luma_status,
                            cancel_forwarded_at=(
                                func.coalesce(
                                    BuilderTask.cancel_forwarded_at, func.now()
                                )
                                if state.cancel_forwarded
                                else None
                            ),
                            result_descriptor_json=(
                                descriptor if descriptor is not None else null()
                            ),
                            checkpoint_version=expected_version + 1,
                            updated_at=func.now(),
                        )
                        .returning(BuilderTask)
                    )
                    if task is None:
                        raise AnalyzeStateConflict(
                            "checkpoint compare-and-swap failed"
                        )
                    return _state_from_task(task)
        except IntegrityError:
            raise AnalyzeStateConflict("checkpoint uniqueness conflict") from None
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")


class PostgresUpdateCheckResolver:
    """Resolve an immutable deployed baseline against the recorded analysis.

    The lifecycle request is the authority for the baseline.  This resolver
    never follows the application's moving current pointers after admission.
    """

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def resolve(
        self,
        operation: OperationRecord,
        context: AnalyzeSourceContext,
    ) -> UpdateCheckResult:
        if (
            operation.kind != "application.check-update"
            or operation.status != "running"
            or operation.tenant_id != context.tenant_ref
            or operation.target_type != "application"
            or operation.target_id != context.application_ref
        ):
            raise UpdateCheckResultInvalid()
        try:
            async with self._sessions() as session:
                request = await session.scalar(
                    select(ApplicationLifecycleRequest).where(
                        ApplicationLifecycleRequest.tenant_id
                        == context.tenant_ref,
                        ApplicationLifecycleRequest.operation_id == operation.id,
                        ApplicationLifecycleRequest.application_id
                        == context.application_ref,
                        ApplicationLifecycleRequest.action == "check-update",
                    )
                )
                if (
                    request is None
                    or request.analysis_id is None
                    or request.base_source_revision_id is None
                    or request.source_revision_id != context.source_revision_ref
                ):
                    raise UpdateCheckResultInvalid()
                analysis = await session.scalar(
                    select(Analysis).where(
                        Analysis.tenant_id == context.tenant_ref,
                        Analysis.id == request.analysis_id,
                        Analysis.operation_id == operation.id,
                        Analysis.application_id == context.application_ref,
                        Analysis.source_revision_id == context.source_revision_ref,
                    )
                )
                base_source = await session.scalar(
                    select(SourceRevision).where(
                        SourceRevision.tenant_id == context.tenant_ref,
                        SourceRevision.id == request.base_source_revision_id,
                        SourceRevision.application_id == context.application_ref,
                    )
                )
                if (
                    analysis is None
                    or base_source is None
                    or analysis.status
                    not in {
                        "analyzed",
                        "deployable",
                        "needs_configuration",
                        "not_deployable",
                    }
                    or analysis.source_tree_digest is None
                    or analysis.deployment_plan_digest is None
                ):
                    raise UpdateCheckResultInvalid()

                candidate_source = analysis.source_tree_digest
                candidate_plan = analysis.deployment_plan_digest
                if request.source_deployment_id is None:
                    return self._without_baseline(candidate_source, candidate_plan)

                deployment = await session.scalar(
                    select(Deployment).where(
                        Deployment.tenant_id == context.tenant_ref,
                        Deployment.application_id == context.application_ref,
                        Deployment.id == request.source_deployment_id,
                        Deployment.status == "succeeded",
                    )
                )
                if deployment is None:
                    raise UpdateCheckResultInvalid()
                revision = await session.scalar(
                    select(AppRevision).where(
                        AppRevision.tenant_id == context.tenant_ref,
                        AppRevision.application_id == context.application_ref,
                        AppRevision.id == deployment.revision_id,
                    )
                )
                if (
                    revision is None
                    or revision.source_revision_id
                    != request.base_source_revision_id
                ):
                    raise UpdateCheckResultInvalid()
                if base_source.source_tree_digest is None:
                    return self._without_baseline(candidate_source, candidate_plan)

                source_changed = (
                    base_source.source_tree_digest != candidate_source
                )
                plan_changed = revision.deployment_plan_digest != candidate_plan
                return UpdateCheckResult(
                    baseline_available=True,
                    source_changed=source_changed,
                    deployment_plan_changed=plan_changed,
                    changed=source_changed or plan_changed,
                    baseline_source_tree_digest=base_source.source_tree_digest,
                    baseline_deployment_plan_digest=(
                        revision.deployment_plan_digest
                    ),
                    candidate_source_tree_digest=candidate_source,
                    candidate_deployment_plan_digest=candidate_plan,
                )
        except UpdateCheckResultInvalid:
            raise
        except ValueError as exc:
            raise UpdateCheckResultInvalid() from exc
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    @staticmethod
    def _without_baseline(
        candidate_source: str,
        candidate_plan: str,
    ) -> UpdateCheckResult:
        try:
            return UpdateCheckResult(
                baseline_available=False,
                source_changed=True,
                deployment_plan_changed=True,
                changed=True,
                candidate_source_tree_digest=candidate_source,
                candidate_deployment_plan_digest=candidate_plan,
            )
        except ValueError as exc:
            raise UpdateCheckResultInvalid() from exc


class PostgresAnalysisRecorder:
    """Persist immutable analysis facts and descriptor-only artifact links."""

    def __init__(self, sessions: Any, *, agent_image_digest: str) -> None:
        if not isinstance(agent_image_digest, str) or not _IMAGE_DIGEST.fullmatch(
            agent_image_digest
        ):
            raise ValueError("agent_image_digest must be immutable")
        self._sessions = sessions
        self._agent_image_digest = agent_image_digest

    async def record(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str | None = None,
    ) -> AnalysisRecording:
        # Artifact ingestion binds its later download lease to this upstream
        # identifier.  Descriptor persistence itself does not expose or store
        # the identifier in the public analysis row.
        if builder_task_id is not None and (
            not isinstance(builder_task_id, str)
            or not 1 <= len(builder_task_id) <= 256
            or any(character in builder_task_id for character in "\x00\r\n")
        ):
            raise ValueError("builder_task_id has invalid format")
        require_opaque_id(operation_id, prefix="op")
        recording = AnalysisRecording(
            analysis_status=_analysis_status_for_verdict(references.verdict),
            artifact_state="descriptor-only",
            plan_stored=False,
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    operation = await session.scalar(
                        select(Operation)
                        .where(
                            Operation.tenant_id == context.tenant_ref,
                            Operation.id == operation_id,
                            Operation.status == "running",
                            Operation.cancel_requested_at.is_(None),
                            Operation.lease_expires_at > func.now(),
                        )
                        .with_for_update()
                    )
                    if operation is None or not _operation_matches_context(
                        operation, context
                    ):
                        raise AnalyzeContextInvalid()
                    source = await session.scalar(
                        select(SourceRevision).where(
                            SourceRevision.tenant_id == context.tenant_ref,
                            SourceRevision.id == context.source_revision_ref,
                            SourceRevision.application_id == context.application_ref,
                            SourceRevision.deleted_at.is_(None),
                        )
                    )
                    if source is None:
                        raise AnalyzeContextInvalid()
                    self._validate_or_populate_source(source, references)
                    existing = await session.scalar(
                        select(Analysis)
                        .where(
                            Analysis.tenant_id == context.tenant_ref,
                            Analysis.operation_id == operation_id,
                        )
                        .with_for_update()
                    )
                    if existing is None:
                        analysis = Analysis(
                            id=new_id("ana"),
                            tenant_id=context.tenant_ref,
                            application_id=context.application_ref,
                            source_revision_id=context.source_revision_ref,
                            operation_id=operation_id,
                            status=recording.analysis_status,
                            policy_version=references.policy_version,
                            agent_image_digest=self._agent_image_digest,
                            resolved_commit_full=references.resolved_commit,
                            source_tree_digest=references.source_tree_digest,
                            source_snapshot_id=references.source_snapshot_id,
                            source_snapshot_digest=references.source_snapshot_digest,
                            deployment_plan_digest=references.deployment_plan_digest,
                            build_plan_digest=references.build_plan_digest,
                            evidence_digest=references.evidence_digest,
                            verdict=references.verdict,
                            diagnostic_status=references.diagnostic_status,
                            diagnostic_mode=references.diagnostic_mode,
                            diagnostic_code=references.diagnostic_code,
                            knowledge_version=references.knowledge_version,
                            blockers=list(references.blockers),
                            artifact_state=recording.artifact_state,
                            plan_stored=recording.plan_stored,
                        )
                        session.add(analysis)
                        await session.flush()
                    else:
                        analysis = existing
                        if analysis.status in {"queued", "analyzing"}:
                            self._populate_queued_analysis(
                                analysis,
                                context=context,
                                references=references,
                                recording=recording,
                            )
                        else:
                            self._validate_analysis(
                                analysis, context=context, references=references
                            )

                    for descriptor in references.artifacts:
                        kind = _ARTIFACT_KIND[descriptor.name]
                        artifact = await session.scalar(
                            select(Artifact)
                            .where(
                                Artifact.tenant_id == context.tenant_ref,
                                Artifact.kind == kind,
                                Artifact.digest == descriptor.digest,
                            )
                            .with_for_update()
                        )
                        if artifact is None:
                            artifact = Artifact(
                                id=new_id("art"),
                                tenant_id=context.tenant_ref,
                                kind=kind,
                                digest=descriptor.digest,
                                media_type=descriptor.media_type,
                                size_bytes=descriptor.size_bytes,
                                storage_key=None,
                                upload_status="pending",
                                verified_at=None,
                            )
                            session.add(artifact)
                            await session.flush()
                        elif (
                            artifact.media_type != descriptor.media_type
                            or artifact.size_bytes != descriptor.size_bytes
                        ):
                            raise AnalyzeOrchestrationError()
                        link = await session.scalar(
                            select(AnalysisArtifact).where(
                                AnalysisArtifact.analysis_id == analysis.id,
                                AnalysisArtifact.name == descriptor.name,
                            )
                        )
                        if link is None:
                            session.add(
                                AnalysisArtifact(
                                    tenant_id=context.tenant_ref,
                                    analysis_id=analysis.id,
                                    name=descriptor.name,
                                    artifact_id=artifact.id,
                                )
                            )
                        elif (
                            link.tenant_id != context.tenant_ref
                            or link.artifact_id != artifact.id
                        ):
                            raise AnalyzeOrchestrationError()
                    await session.flush()
                    return recording
        except IntegrityError as exc:
            raise AnalyzeStateConflict() from exc
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    def _populate_queued_analysis(
        self,
        analysis: Analysis,
        *,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        recording: AnalysisRecording,
    ) -> None:
        empty = (
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
            analysis.verdict,
            analysis.diagnostic_status,
            analysis.diagnostic_mode,
            analysis.diagnostic_code,
            analysis.knowledge_version,
            analysis.blockers,
            analysis.artifact_state,
            analysis.plan_stored,
        )
        expected_empty = (
            context.application_ref,
            context.source_revision_ref,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [],
            "descriptor-only",
            False,
        )
        if empty != expected_empty:
            raise AnalyzeOrchestrationError()
        analysis.status = recording.analysis_status
        analysis.policy_version = references.policy_version
        analysis.agent_image_digest = self._agent_image_digest
        analysis.resolved_commit_full = references.resolved_commit
        analysis.source_tree_digest = references.source_tree_digest
        analysis.source_snapshot_id = references.source_snapshot_id
        analysis.source_snapshot_digest = references.source_snapshot_digest
        analysis.deployment_plan_digest = references.deployment_plan_digest
        analysis.build_plan_digest = references.build_plan_digest
        analysis.evidence_digest = references.evidence_digest
        analysis.verdict = references.verdict
        analysis.diagnostic_status = references.diagnostic_status
        analysis.diagnostic_mode = references.diagnostic_mode
        analysis.diagnostic_code = references.diagnostic_code
        analysis.knowledge_version = references.knowledge_version
        analysis.blockers = list(references.blockers)
        analysis.artifact_state = recording.artifact_state
        analysis.plan_stored = recording.plan_stored

    @staticmethod
    def _validate_or_populate_source(
        source: SourceRevision, references: AnalysisDigestReferences
    ) -> None:
        actual = (
            source.resolved_commit_full,
            source.source_tree_digest,
            source.snapshot_id,
            source.snapshot_digest,
        )
        expected = (
            references.resolved_commit,
            references.source_tree_digest,
            references.source_snapshot_id,
            references.source_snapshot_digest,
        )
        if actual == (None, None, None, None):
            (
                source.resolved_commit_full,
                source.source_tree_digest,
                source.snapshot_id,
                source.snapshot_digest,
            ) = expected
        elif actual != expected:
            raise AnalyzeOrchestrationError()

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
            _analysis_status_for_verdict(references.verdict),
            references.policy_version,
            self._agent_image_digest,
            references.resolved_commit,
            references.source_tree_digest,
            references.source_snapshot_id,
            references.source_snapshot_digest,
            references.deployment_plan_digest,
            references.build_plan_digest,
            references.evidence_digest,
            references.verdict,
            references.diagnostic_status,
            references.diagnostic_mode,
            references.diagnostic_code,
            references.knowledge_version,
            list(references.blockers),
            "descriptor-only",
            False,
        )
        actual = (
            analysis.application_id,
            analysis.source_revision_id,
            analysis.status,
            analysis.policy_version,
            analysis.agent_image_digest,
            analysis.resolved_commit_full,
            analysis.source_tree_digest,
            analysis.source_snapshot_id,
            analysis.source_snapshot_digest,
            analysis.deployment_plan_digest,
            analysis.build_plan_digest,
            analysis.evidence_digest,
            analysis.verdict,
            analysis.diagnostic_status,
            analysis.diagnostic_mode,
            analysis.diagnostic_code,
            analysis.knowledge_version,
            analysis.blockers,
            analysis.artifact_state,
            analysis.plan_stored,
        )
        if actual != expected:
            raise AnalyzeOrchestrationError()


def _analysis_status_for_verdict(verdict: str) -> str:
    try:
        return {
            "deployable": "deployable",
            "needs_input": "needs_configuration",
            "unsupported": "not_deployable",
            "diagnostic_failed": "diagnostic_failed",
        }[verdict]
    except KeyError as exc:
        raise AnalyzeOrchestrationError() from exc
