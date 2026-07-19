from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol

from sqlalchemy import func, null, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from lae_luma_adapter import RuntimeDeployment, RuntimeVolumeBinding
from lae_store import (
    EventInput,
    LeaseLost,
    OperationRecord,
    StoredDeploymentPlanArtifact,
    TenantScope,
    new_id,
)
from lae_store.ids import require_opaque_id
from lae_store.models import (
    Analysis,
    AnalysisArtifact,
    AppRevision,
    Application,
    ApplicationEnvironmentVariable,
    ApplicationRoute,
    ApplicationService,
    ApplicationVolume,
    Artifact,
    Deployment,
    DeploymentBuildOutput,
    DeploymentCheckpoint,
    DeploymentQuotaReservation,
    Operation,
)
from lae_store.repositories import _append_event, _operation_record
from lae_store.security import ensure_persistable_payload

from .deployment import (
    DeploymentCheckpointConflict,
    DeploymentContext,
    DeploymentContextInvalid,
    DeploymentEnvironmentRequirement,
    DeploymentOrchestrationState,
    DeploymentRoute,
    DeploymentService,
    DeploymentVolume,
    VerifiedBuildOutput,
    _deployment_status,
)


_SCHEMA_SQLSTATES = {"42P01", "42703"}


class DeploymentSchemaUnavailable(DeploymentContextInvalid):
    code = "LAE_DEPLOYMENT_SCHEMA_UNAVAILABLE"
    public_message = "Durable deployment checkpoint storage is not available."


class TrustedBuildPlanUnavailable(DeploymentContextInvalid):
    code = "LAE_BUILD_PLAN_UNAVAILABLE"
    public_message = "The trusted build plan is not available."


@dataclass(frozen=True, slots=True)
class StoredBuildPlanArtifact:
    artifact_id: str
    digest: str
    media_type: str
    size_bytes: int
    storage_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrustedRuntimeService:
    service_key: str
    role: str
    build_key: str
    command: str | None
    dependencies: tuple[str, ...]
    cpu: str
    memory_mib: int
    environment_names: tuple[str, ...]
    port: int | None
    health_path: str | None
    health_interval_seconds: int | None


@dataclass(frozen=True, slots=True)
class TrustedRuntimeRoute:
    service_key: str
    container_port: int
    health_path: str


@dataclass(frozen=True, slots=True)
class TrustedRuntimeVolume:
    volume_key: str
    requested_bytes: int
    service_keys: tuple[str, ...]
    mount_path: str
    access_mode: str


@dataclass(frozen=True, slots=True)
class TrustedBuildPlan:
    signed_build_plan: Mapping[str, Any] = field(repr=False)
    credential_lease_id: str = field(repr=False)
    service_build_keys: Mapping[str, str]
    kind: str
    services: tuple[TrustedRuntimeService, ...]
    routes: tuple[TrustedRuntimeRoute, ...]
    volumes: tuple[TrustedRuntimeVolume, ...]


class TrustedBuildPlanMaterializer(Protocol):
    """Private S3/signing/broker boundary for one immutable build plan.

    Implementations must stream and verify ``artifact`` by exact size/media/
    digest, validate the candidate schema, resolve external tags, sign the
    resulting ``lae.build-plan/v1`` document, and issue a task-bound credential
    lease. No raw registry coordinate or credential may be returned separately.
    """

    async def materialize(
        self,
        artifact: StoredBuildPlanArtifact,
        deployment_artifact: StoredDeploymentPlanArtifact,
        *,
        tenant_ref: str,
        application_ref: str,
        operation_ref: str,
        revision_ref: str,
        source_snapshot_id: str,
        source_snapshot_digest: str,
        resolved_commit: str,
        policy_version: str,
    ) -> TrustedBuildPlan: ...


class UnconfiguredTrustedBuildPlanMaterializer:
    async def materialize(
        self,
        artifact: StoredBuildPlanArtifact,
        deployment_artifact: StoredDeploymentPlanArtifact,
        **kwargs: Any,
    ) -> TrustedBuildPlan:
        del artifact, deployment_artifact, kwargs
        raise TrustedBuildPlanUnavailable()


class PostgresDeploymentContextLoader:
    """Load immutable deployment/revision/topology facts through tenant joins."""

    def __init__(
        self,
        sessions: Any,
        materializer: TrustedBuildPlanMaterializer,
        *,
        region: str,
    ) -> None:
        if region not in {"cn", "global"}:
            raise ValueError("LAE deployment region must be cn or global")
        self._sessions = sessions
        self._materializer = materializer
        self._region = region

    async def load(self, operation: OperationRecord) -> DeploymentContext:
        _require_operation_record(operation)
        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(
                            Deployment,
                            AppRevision,
                            Application,
                            Analysis,
                            Artifact,
                        )
                        .join(
                            AppRevision,
                            (AppRevision.tenant_id == Deployment.tenant_id)
                            & (AppRevision.application_id == Deployment.application_id)
                            & (AppRevision.id == Deployment.revision_id),
                        )
                        .join(
                            Application,
                            (Application.tenant_id == Deployment.tenant_id)
                            & (Application.id == Deployment.application_id),
                        )
                        .join(
                            Analysis,
                            (Analysis.tenant_id == AppRevision.tenant_id)
                            & (Analysis.id == AppRevision.analysis_id),
                        )
                        .join(
                            AnalysisArtifact,
                            (AnalysisArtifact.tenant_id == Analysis.tenant_id)
                            & (AnalysisArtifact.analysis_id == Analysis.id)
                            & (AnalysisArtifact.name == "buildPlan"),
                        )
                        .join(
                            Artifact,
                            (Artifact.tenant_id == AnalysisArtifact.tenant_id)
                            & (Artifact.id == AnalysisArtifact.artifact_id),
                        )
                        .where(
                            Deployment.tenant_id == operation.tenant_id,
                            Deployment.operation_id == operation.id,
                            Deployment.application_id == operation.target_id,
                            Application.deleted_at.is_(None),
                            AppRevision.status == "candidate",
                            # Admission accepts a stored ``needs_configuration``
                            # analysis after its required environment has been
                            # supplied. The worker must load that same immutable
                            # plan instead of forcing a redundant re-analysis.
                            Analysis.status.in_(
                                ("deployable", "needs_configuration")
                            ),
                            Analysis.artifact_state == "stored",
                            Analysis.plan_stored.is_(True),
                            Artifact.kind == "build-plan-candidate",
                            Artifact.upload_status == "verified",
                            Artifact.storage_key.is_not(None),
                            Artifact.digest == Analysis.build_plan_digest,
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise DeploymentContextInvalid()
                deployment, revision, application, analysis, artifact = row
                if (
                    deployment.revision_id != revision.id
                    or revision.application_id != application.id
                    or revision.analysis_id != analysis.id
                    or revision.source_revision_id != analysis.source_revision_id
                    or revision.environment_version != application.environment_version
                    or analysis.source_snapshot_id is None
                    or analysis.source_snapshot_digest is None
                    or analysis.build_plan_digest is None
                    or analysis.resolved_commit_full is None
                    or analysis.policy_version is None
                    or artifact.storage_key is None
                ):
                    raise DeploymentContextInvalid()

                services = list(
                    await session.scalars(
                        select(ApplicationService)
                        .where(
                            ApplicationService.tenant_id == operation.tenant_id,
                            ApplicationService.application_id == application.id,
                        )
                        .order_by(ApplicationService.service_key)
                    )
                )
                route_rows = list(
                    (
                        await session.execute(
                            select(ApplicationRoute, ApplicationService.service_key)
                            .join(
                                ApplicationService,
                                (
                                    ApplicationService.tenant_id
                                    == ApplicationRoute.tenant_id
                                )
                                & (
                                    ApplicationService.application_id
                                    == ApplicationRoute.application_id
                                )
                                & (ApplicationService.id == ApplicationRoute.service_id),
                            )
                            .where(
                                ApplicationRoute.tenant_id == operation.tenant_id,
                                ApplicationRoute.application_id == application.id,
                                ApplicationRoute.kind == "http",
                            )
                            .order_by(ApplicationRoute.hostname)
                        )
                    ).all()
                )
                volumes = list(
                    await session.scalars(
                        select(ApplicationVolume)
                        .where(
                            ApplicationVolume.tenant_id == operation.tenant_id,
                            ApplicationVolume.application_id == application.id,
                            ApplicationVolume.status.notin_(["deleted", "retained"]),
                        )
                        .order_by(ApplicationVolume.volume_key)
                    )
                )
                environment_rows = list(
                    await session.scalars(
                        select(ApplicationEnvironmentVariable).where(
                            ApplicationEnvironmentVariable.tenant_id
                            == operation.tenant_id,
                            ApplicationEnvironmentVariable.application_id
                            == application.id,
                        )
                    )
                )
                deployment_artifact_row = (
                    await session.execute(
                        select(Artifact)
                        .join(
                            AnalysisArtifact,
                            (AnalysisArtifact.tenant_id == Artifact.tenant_id)
                            & (AnalysisArtifact.artifact_id == Artifact.id),
                        )
                        .where(
                            AnalysisArtifact.tenant_id == operation.tenant_id,
                            AnalysisArtifact.analysis_id == analysis.id,
                            AnalysisArtifact.name == "deploymentPlan",
                            Artifact.kind == "deployment-plan",
                            Artifact.upload_status == "verified",
                            Artifact.storage_key.is_not(None),
                            Artifact.digest == analysis.deployment_plan_digest,
                        )
                    )
                ).scalar_one_or_none()
                if (
                    deployment_artifact_row is None
                    or deployment_artifact_row.storage_key is None
                    or analysis.deployment_plan_digest is None
                ):
                    raise DeploymentContextInvalid()

            stored = StoredBuildPlanArtifact(
                artifact_id=artifact.id,
                digest=artifact.digest,
                media_type=artifact.media_type,
                size_bytes=artifact.size_bytes,
                storage_key=artifact.storage_key,
            )
            trusted = await self._materializer.materialize(
                stored,
                StoredDeploymentPlanArtifact(
                    artifact_id=deployment_artifact_row.id,
                    analysis_id=analysis.id,
                    source_revision_id=analysis.source_revision_id,
                    digest=deployment_artifact_row.digest,
                    media_type=deployment_artifact_row.media_type,
                    size_bytes=deployment_artifact_row.size_bytes,
                    storage_key=deployment_artifact_row.storage_key,
                    source_snapshot_digest=analysis.source_snapshot_digest,
                ),
                tenant_ref=operation.tenant_id,
                application_ref=application.id,
                operation_ref=operation.id,
                revision_ref=revision.id,
                source_snapshot_id=analysis.source_snapshot_id,
                source_snapshot_digest=analysis.source_snapshot_digest,
                resolved_commit=analysis.resolved_commit_full,
                policy_version=analysis.policy_version,
            )
            service_keys = {service.service_key for service in services}
            trusted_services = {service.service_key: service for service in trusted.services}
            if (
                set(trusted.service_build_keys) != service_keys
                or set(trusted_services) != service_keys
                or trusted.kind != revision.kind
            ):
                raise DeploymentContextInvalid()
            requirements = _environment_requirements(
                environment_rows,
                tuple(trusted_services.values()),
            )
            environment_by_service: dict[str, set[str]] = {
                key: set() for key in service_keys
            }
            for requirement in requirements:
                environment_by_service[requirement.service_key].add(requirement.name)
            catalog_by_key = {service.service_key: service for service in services}
            if any(
                catalog_by_key[key].role != trusted_services[key].role
                for key in service_keys
            ):
                raise DeploymentContextInvalid()
            trusted_routes = {
                (route.service_key, route.container_port): route
                for route in trusted.routes
            }
            if len(trusted_routes) != len(trusted.routes) or set(trusted_routes) != {
                (service_key, route.container_port)
                for route, service_key in route_rows
            }:
                raise DeploymentContextInvalid()
            trusted_volumes = {volume.volume_key: volume for volume in trusted.volumes}
            if len(trusted_volumes) != len(trusted.volumes) or set(trusted_volumes) != {
                volume.volume_key for volume in volumes
            } or any(
                trusted_volumes[volume.volume_key].requested_bytes
                != volume.requested_bytes
                for volume in volumes
            ):
                raise DeploymentContextInvalid()
            return DeploymentContext(
                tenant_ref=operation.tenant_id,
                application_ref=application.id,
                operation_ref=operation.id,
                deployment_ref=deployment.id,
                revision_ref=revision.id,
                source_revision_ref=revision.source_revision_id,
                analysis_ref=analysis.id,
                luma_name=application.luma_name,
                kind=revision.kind,
                region=self._region,
                environment_version=revision.environment_version,
                source_snapshot_id=analysis.source_snapshot_id,
                source_snapshot_digest=analysis.source_snapshot_digest,
                build_plan_digest=analysis.build_plan_digest,
                signed_build_plan=trusted.signed_build_plan,
                build_credential_lease_id=trusted.credential_lease_id,
                services=tuple(
                    DeploymentService(
                        key=service.service_key,
                        role=service.role,
                        build_key=trusted.service_build_keys[service.service_key],
                        command=trusted_services[service.service_key].command,
                        dependencies=trusted_services[service.service_key].dependencies,
                        cpu=trusted_services[service.service_key].cpu,
                        memory_mib=trusted_services[service.service_key].memory_mib,
                        environment_names=tuple(
                            sorted(environment_by_service[service.service_key])
                        ),
                        port=trusted_services[service.service_key].port,
                        health_path=trusted_services[service.service_key].health_path,
                        health_interval_seconds=trusted_services[
                            service.service_key
                        ].health_interval_seconds,
                        required=service.required,
                    )
                    for service in services
                ),
                routes=tuple(
                    DeploymentRoute(
                        service_key=service_key,
                        hostname=route.hostname,
                        container_port=route.container_port,
                        health_path=trusted_routes[
                            (service_key, route.container_port)
                        ].health_path,
                    )
                    for route, service_key in route_rows
                ),
                volumes=tuple(
                    DeploymentVolume(
                        key=volume.volume_key,
                        requested_bytes=volume.requested_bytes,
                        service_keys=trusted_volumes[
                            volume.volume_key
                        ].service_keys,
                        mount_path=trusted_volumes[volume.volume_key].mount_path,
                        access_mode=trusted_volumes[
                            volume.volume_key
                        ].access_mode,
                        existing_ref=volume.luma_volume_ref,
                    )
                    for volume in volumes
                ),
                environment=requirements,
                normalized_compose_digest=revision.normalized_compose_digest,
            )
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")


class PostgresDeploymentStateStore:
    """CAS deployment checkpoint and atomic activation transaction."""

    def __init__(self, sessions: Any, *, luma_cluster_id: str) -> None:
        if not isinstance(luma_cluster_id, str) or not luma_cluster_id:
            raise ValueError("luma_cluster_id is required")
        self._sessions = sessions
        self._cluster = luma_cluster_id

    async def initialize(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        *,
        timeout: timedelta,
    ) -> DeploymentOrchestrationState:
        if not timedelta(seconds=30) <= timeout <= timedelta(hours=2):
            raise ValueError("deployment timeout must be 30 seconds to 2 hours")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    row = await self._require_owned_operation(
                        session, operation, for_update=True
                    )
                    deployment = await session.scalar(
                        select(Deployment)
                        .where(
                            Deployment.tenant_id == context.tenant_ref,
                            Deployment.id == context.deployment_ref,
                            Deployment.operation_id == operation.id,
                            Deployment.application_id == context.application_ref,
                            Deployment.revision_id == context.revision_ref,
                        )
                        .with_for_update()
                    )
                    if deployment is None:
                        raise DeploymentContextInvalid()
                    existing = await session.get(
                        DeploymentCheckpoint, operation.id, with_for_update=True
                    )
                    if existing is not None:
                        self._validate_checkpoint(existing, context)
                        if not hmac.compare_digest(
                            existing.build_request_digest,
                            context.build_request_digest,
                        ):
                            raise DeploymentCheckpointConflict()
                        return _state_from_checkpoint(existing)
                    now = await session.scalar(select(func.now()))
                    if now is None:
                        raise DeploymentCheckpointConflict()
                    reservation = DeploymentQuotaReservation(
                        id=new_id("qrs"),
                        tenant_id=context.tenant_ref,
                        application_id=context.application_ref,
                        deployment_id=context.deployment_ref,
                        operation_id=operation.id,
                        deployment_slots=1,
                        volume_bytes=sum(item.requested_bytes for item in context.volumes),
                        status="held",
                        expires_at=now + timeout,
                    )
                    session.add(reservation)
                    # There is no ORM relationship between the reservation and
                    # checkpoint mappers. Flush the parent row explicitly so
                    # PostgreSQL never observes the checkpoint FK first.
                    # The flush remains inside this transaction, so retries are
                    # still atomic and the uniqueness constraints remain the
                    # concurrency authority.
                    await session.flush()
                    checkpoint = DeploymentCheckpoint(
                        operation_id=operation.id,
                        tenant_id=context.tenant_ref,
                        application_id=context.application_ref,
                        deployment_id=context.deployment_ref,
                        revision_id=context.revision_ref,
                        quota_reservation_id=reservation.id,
                        checkpoint_version=0,
                        phase="prepare",
                        builder_cursor=0,
                        build_request_digest=context.build_request_digest,
                        normalized_compose_digest=context.normalized_compose_digest,
                        deadline_at=now + timeout,
                    )
                    session.add(checkpoint)
                    if deployment.status == "queued":
                        deployment.status = "building"
                        deployment.started_at = now
                    elif deployment.status != "building":
                        raise DeploymentCheckpointConflict()
                    deployment.updated_at = now
                    row.phase = "deploy.prepare"
                    await session.flush()
                    return _state_from_checkpoint(checkpoint)
        except IntegrityError:
            loaded = await self.load(operation.id)
            if loaded is None or loaded.bind(context) != loaded:
                raise DeploymentCheckpointConflict() from None
            return loaded
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def load(
        self, operation_id: str
    ) -> DeploymentOrchestrationState | None:
        require_opaque_id(operation_id, prefix="op")
        try:
            async with self._sessions() as session:
                row = await session.get(DeploymentCheckpoint, operation_id)
                return None if row is None else _state_from_checkpoint(row)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def save(
        self,
        state: DeploymentOrchestrationState,
        *,
        expected_version: int,
    ) -> DeploymentOrchestrationState:
        descriptor = _serialize_state_descriptor(state)
        try:
            async with self._sessions() as session:
                async with session.begin():
                    values: dict[str, object] = {
                        "checkpoint_version": expected_version + 1,
                        "phase": state.phase,
                        "builder_task_id": state.builder_task_id,
                        "builder_cursor": state.builder_cursor,
                        "builder_status": state.builder_status,
                        "build_cancel_forwarded": state.build_cancel_forwarded,
                        "manifest_digest": state.manifest_digest,
                        "normalized_compose_digest": state.normalized_compose_digest,
                        "luma_deployment_ref": state.luma_deployment_ref,
                        "runtime_status": state.runtime_status,
                        "runtime_cancel_forwarded": state.runtime_cancel_forwarded,
                        # JSONB maps Python None to the JSON literal `null` by
                        # default. The checkpoint contract permits SQL NULL or
                        # an object, so emit an explicit SQL NULL while the
                        # descriptor is empty.
                        "result_descriptor_json": (
                            null() if descriptor is None else descriptor
                        ),
                        "updated_at": func.now(),
                    }
                    updated = await session.scalar(
                        update(DeploymentCheckpoint)
                        .where(
                            DeploymentCheckpoint.operation_id == state.operation_id,
                            DeploymentCheckpoint.checkpoint_version
                            == expected_version,
                            DeploymentCheckpoint.builder_cursor
                            <= state.builder_cursor,
                        )
                        .values(**values)
                        .returning(DeploymentCheckpoint)
                    )
                    if updated is None:
                        raise DeploymentCheckpointConflict()
                    deployment_status = _deployment_status(state.phase)
                    if deployment_status not in {"succeeded", "failed", "canceled"}:
                        await session.execute(
                            update(Deployment)
                            .where(
                                Deployment.tenant_id == updated.tenant_id,
                                Deployment.id == updated.deployment_id,
                                Deployment.status.notin_(["succeeded", "failed", "canceled"]),
                            )
                            .values(status=deployment_status, updated_at=func.now())
                        )
                    return _state_from_checkpoint(updated)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def persist_build_outputs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        outputs: tuple[VerifiedBuildOutput, ...],
    ) -> None:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_owned_operation(
                        session, operation, for_update=False
                    )
                    existing = list(
                        await session.scalars(
                            select(DeploymentBuildOutput).where(
                                DeploymentBuildOutput.tenant_id == context.tenant_ref,
                                DeploymentBuildOutput.operation_id == operation.id,
                            )
                        )
                    )
                    if existing:
                        if _outputs_from_rows(existing) != outputs:
                            raise DeploymentCheckpointConflict()
                        return
                    session.add_all(
                        [
                            DeploymentBuildOutput(
                                id=new_id("bout"),
                                tenant_id=context.tenant_ref,
                                application_id=context.application_ref,
                                deployment_id=context.deployment_ref,
                                operation_id=operation.id,
                                revision_id=context.revision_ref,
                                build_key=item.build_key,
                                service_key=item.service_key,
                                image_digest=item.image_digest,
                                sbom_digest=item.sbom_digest,
                                provenance_digest=item.provenance_digest,
                                scan_digest=item.scan_digest,
                            )
                            for item in outputs
                        ]
                    )
                    await session.flush()
        except IntegrityError as exc:
            raise DeploymentCheckpointConflict() from exc
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def bind_volumes(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        bindings: tuple[RuntimeVolumeBinding, ...],
    ) -> None:
        expected = {item.key for item in context.volumes}
        by_key = {item.key: item.volume_ref for item in bindings}
        if set(by_key) != expected:
            raise DeploymentContextInvalid()
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_owned_operation(
                        session, operation, for_update=False
                    )
                    rows = list(
                        await session.scalars(
                            select(ApplicationVolume)
                            .where(
                                ApplicationVolume.tenant_id == context.tenant_ref,
                                ApplicationVolume.application_id
                                == context.application_ref,
                            )
                            .with_for_update()
                        )
                    )
                    if {row.volume_key for row in rows} != expected:
                        raise DeploymentContextInvalid()
                    now = await session.scalar(select(func.now()))
                    for row in rows:
                        bound = by_key[row.volume_key]
                        if row.luma_volume_ref is not None and row.luma_volume_ref != bound:
                            raise DeploymentCheckpointConflict()
                        row.luma_volume_ref = bound
                        row.status = "ready"
                        row.provisioned_at = row.provisioned_at or now
                        row.updated_at = now
                    await session.flush()
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def activate(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        runtime: RuntimeDeployment,
        *,
        worker_id: str,
    ) -> OperationRecord:
        if runtime.status != "running" or runtime.manifest_digest != state.manifest_digest:
            raise DeploymentContextInvalid()
        try:
            async with self._sessions() as session:
                async with session.begin():
                    op_row = await session.scalar(
                        select(Operation)
                        .where(
                            Operation.tenant_id == context.tenant_ref,
                            Operation.id == operation.id,
                        )
                        .with_for_update()
                    )
                    now = await session.scalar(select(func.now()))
                    if (
                        op_row is None
                        or now is None
                        or op_row.status != "running"
                        or op_row.lease_owner != worker_id
                        or op_row.lease_expires_at is None
                        or op_row.lease_expires_at <= now
                    ):
                        raise LeaseLost("deployment operation lease is no longer owned")
                    if op_row.cancel_requested_at is not None:
                        from .deployment import DeploymentCancellationRequested

                        raise DeploymentCancellationRequested()
                    checkpoint = await session.get(
                        DeploymentCheckpoint, operation.id, with_for_update=True
                    )
                    if (
                        checkpoint is None
                        or checkpoint.checkpoint_version != state.version
                        or checkpoint.phase != "activating"
                        or checkpoint.luma_deployment_ref != runtime.deployment_ref
                        or checkpoint.manifest_digest != runtime.manifest_digest
                    ):
                        raise DeploymentCheckpointConflict()
                    application = await session.scalar(
                        select(Application)
                        .where(
                            Application.tenant_id == context.tenant_ref,
                            Application.id == context.application_ref,
                        )
                        .with_for_update()
                    )
                    deployment = await session.scalar(
                        select(Deployment)
                        .where(
                            Deployment.tenant_id == context.tenant_ref,
                            Deployment.id == context.deployment_ref,
                        )
                        .with_for_update()
                    )
                    revision = await session.scalar(
                        select(AppRevision)
                        .where(
                            AppRevision.tenant_id == context.tenant_ref,
                            AppRevision.id == context.revision_ref,
                        )
                        .with_for_update()
                    )
                    reservation = await session.get(
                        DeploymentQuotaReservation,
                        checkpoint.quota_reservation_id,
                        with_for_update=True,
                    )
                    if (
                        application is None
                        or deployment is None
                        or revision is None
                        or reservation is None
                        or deployment.operation_id != operation.id
                        or deployment.revision_id != revision.id
                        or deployment.previous_deployment_id
                        != application.current_deployment_id
                        or revision.status != "candidate"
                        or revision.luma_manifest_digest
                        not in {None, runtime.manifest_digest}
                        or reservation.status != "held"
                    ):
                        raise DeploymentCheckpointConflict()
                    required = {item.key for item in context.services if item.required}
                    if any(runtime.service_statuses.get(key) != "healthy" for key in required):
                        raise DeploymentCheckpointConflict()
                    if any(
                        runtime.route_statuses.get(route.hostname) != "ready"
                        for route in context.routes
                    ):
                        raise DeploymentCheckpointConflict()
                    outputs = list(
                        await session.scalars(
                            select(DeploymentBuildOutput).where(
                                DeploymentBuildOutput.tenant_id == context.tenant_ref,
                                DeploymentBuildOutput.operation_id == operation.id,
                            )
                        )
                    )
                    if {item.service_key for item in outputs} != {
                        item.key for item in context.services
                    }:
                        raise DeploymentCheckpointConflict()

                    if application.current_revision_id is not None:
                        previous = await session.scalar(
                            select(AppRevision)
                            .where(
                                AppRevision.tenant_id == context.tenant_ref,
                                AppRevision.id == application.current_revision_id,
                            )
                            .with_for_update()
                        )
                        if previous is None or previous.status != "active":
                            raise DeploymentCheckpointConflict()
                        previous.status = "superseded"
                        previous.updated_at = now
                    revision.luma_manifest_digest = runtime.manifest_digest
                    revision.status = "active"
                    revision.activated_at = now
                    revision.updated_at = now
                    deployment.status = "succeeded"
                    deployment.luma_cluster_id = self._cluster
                    deployment.luma_external_ref = runtime.deployment_ref
                    deployment.finished_at = now
                    deployment.error_code = None
                    deployment.error_message = None
                    deployment.updated_at = now
                    application.current_revision_id = revision.id
                    application.current_deployment_id = deployment.id
                    application.observed_state = "running"
                    application.updated_at = now
                    output_by_service = {item.service_key: item for item in outputs}
                    service_rows = list(
                        await session.scalars(
                            select(ApplicationService)
                            .where(
                                ApplicationService.tenant_id == context.tenant_ref,
                                ApplicationService.application_id
                                == context.application_ref,
                            )
                            .with_for_update()
                        )
                    )
                    for service in service_rows:
                        service.current_image_digest = output_by_service[
                            service.service_key
                        ].image_digest
                        service.observed_state = "running"
                        service.updated_at = now
                    await session.execute(
                        update(ApplicationRoute)
                        .where(
                            ApplicationRoute.tenant_id == context.tenant_ref,
                            ApplicationRoute.application_id == context.application_ref,
                        )
                        .values(status="ready", updated_at=now)
                    )
                    checkpoint.phase = "complete"
                    checkpoint.runtime_status = "running"
                    checkpoint.checkpoint_version += 1
                    checkpoint.updated_at = now
                    reservation.status = "consumed"
                    reservation.released_at = now
                    reservation.updated_at = now
                    result = {
                        "applicationId": context.application_ref,
                        "deploymentId": context.deployment_ref,
                        "revisionId": context.revision_ref,
                        "status": "succeeded",
                    }
                    ensure_persistable_payload(result)
                    op_row.status = "succeeded"
                    op_row.result = result
                    op_row.error_code = None
                    op_row.error_message = None
                    op_row.finished_at = now
                    op_row.lease_owner = None
                    op_row.lease_expires_at = None
                    op_row.lease_heartbeat_at = None
                    op_row.phase = "deploy.activate"
                    op_row.updated_at = now
                    await _append_event(
                        session,
                        op_row,
                        EventInput(
                            type="operation.succeeded",
                            phase="deploy.activate",
                            status="succeeded",
                            message="Operation succeeded",
                            data={},
                        ),
                    )
                    await session.flush()
                    return _operation_record(op_row)
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    async def finalize_terminal(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        *,
        status: str,
        error_code: str | None,
    ) -> None:
        if status not in {"failed", "canceled"}:
            raise ValueError("deployment terminal status is invalid")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._require_owned_operation(
                        session, operation, for_update=True
                    )
                    checkpoint = await session.get(
                        DeploymentCheckpoint, operation.id, with_for_update=True
                    )
                    if checkpoint is None:
                        raise DeploymentCheckpointConflict()
                    deployment = await session.scalar(
                        select(Deployment)
                        .where(
                            Deployment.tenant_id == context.tenant_ref,
                            Deployment.id == context.deployment_ref,
                        )
                        .with_for_update()
                    )
                    revision = await session.scalar(
                        select(AppRevision)
                        .where(
                            AppRevision.tenant_id == context.tenant_ref,
                            AppRevision.id == context.revision_ref,
                        )
                        .with_for_update()
                    )
                    reservation = await session.get(
                        DeploymentQuotaReservation,
                        checkpoint.quota_reservation_id,
                        with_for_update=True,
                    )
                    if deployment is None or revision is None or reservation is None:
                        raise DeploymentCheckpointConflict()
                    if deployment.status in {"succeeded", "failed", "canceled"}:
                        if deployment.status != status:
                            raise DeploymentCheckpointConflict()
                        return
                    now = await session.scalar(select(func.now()))
                    deployment.status = status
                    deployment.finished_at = now
                    deployment.error_code = error_code if status == "failed" else None
                    deployment.error_message = (
                        "The deployment did not complete successfully."
                        if status == "failed"
                        else None
                    )
                    deployment.updated_at = now
                    if revision.status == "candidate":
                        revision.status = "failed"
                        revision.updated_at = now
                    checkpoint.phase = status
                    checkpoint.result_descriptor_json = {
                        "terminalErrorCode": error_code
                    } if error_code else None
                    checkpoint.checkpoint_version = max(
                        checkpoint.checkpoint_version, state.version
                    ) + 1
                    checkpoint.updated_at = now
                    reservation.status = "released"
                    reservation.released_at = now
                    reservation.updated_at = now
                    await session.flush()
        except DBAPIError as exc:
            _raise_database_error(exc)
            raise AssertionError("unreachable")

    @staticmethod
    def _validate_checkpoint(
        checkpoint: DeploymentCheckpoint, context: DeploymentContext
    ) -> None:
        if (
            checkpoint.tenant_id != context.tenant_ref
            or checkpoint.application_id != context.application_ref
            or checkpoint.deployment_id != context.deployment_ref
            or checkpoint.revision_id != context.revision_ref
            or checkpoint.normalized_compose_digest
            != context.normalized_compose_digest
        ):
            raise DeploymentCheckpointConflict()

    @staticmethod
    async def _require_owned_operation(
        session: Any,
        operation: OperationRecord,
        *,
        for_update: bool,
    ) -> Operation:
        statement = select(Operation).where(
            Operation.tenant_id == operation.tenant_id,
            Operation.id == operation.id,
            Operation.status == "running",
            Operation.lease_owner == operation.lease_owner,
            Operation.lease_attempt == operation.lease_attempt,
            Operation.lease_expires_at > func.now(),
        )
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise LeaseLost("deployment operation lease is no longer owned")
        return row


def _environment_requirements(
    rows: list[ApplicationEnvironmentVariable],
    services: tuple[TrustedRuntimeService, ...],
) -> tuple[DeploymentEnvironmentRequirement, ...]:
    service_keys = {service.service_key for service in services}
    declared_targets_by_name: dict[str, set[str]] = {}
    for service in services:
        for name in service.environment_names:
            declared_targets_by_name.setdefault(name, set()).add(service.service_key)

    bindings: set[tuple[str, str]] = set()
    for row in rows:
        if row.service_scope == "*":
            if len(service_keys) == 1:
                bindings.add((next(iter(service_keys)), row.name))
                continue
            declared_targets = declared_targets_by_name.get(row.name)
            if declared_targets != service_keys:
                raise DeploymentContextInvalid()
            bindings.update((service_key, row.name) for service_key in service_keys)
            continue
        if row.service_scope not in service_keys:
            raise DeploymentContextInvalid()
        declared_targets = declared_targets_by_name.get(row.name)
        if declared_targets is not None and row.service_scope not in declared_targets:
            raise DeploymentContextInvalid()
        bindings.add((row.service_scope, row.name))
    return tuple(
        DeploymentEnvironmentRequirement(service_key, name)
        for service_key, name in sorted(bindings)
    )


def _serialize_state_descriptor(
    state: DeploymentOrchestrationState,
) -> dict[str, object] | None:
    value: dict[str, object] = {}
    if state.build_outputs:
        value["buildOutputs"] = [
            {
                "buildKey": item.build_key,
                "serviceKey": item.service_key,
                "imageDigest": item.image_digest,
                "sbomDigest": item.sbom_digest,
                "provenanceDigest": item.provenance_digest,
                "scanDigest": item.scan_digest,
            }
            for item in state.build_outputs
        ]
    if state.volume_bindings:
        value["volumeBindings"] = [
            {"key": item.key, "volumeRef": item.volume_ref}
            for item in state.volume_bindings
        ]
    if state.terminal_error_code:
        value["terminalErrorCode"] = state.terminal_error_code
    if not value:
        return None
    ensure_persistable_payload(value)
    return value


def _state_from_checkpoint(row: DeploymentCheckpoint) -> DeploymentOrchestrationState:
    descriptor = row.result_descriptor_json or {}
    if not isinstance(descriptor, dict) or not set(descriptor).issubset(
        {"buildOutputs", "volumeBindings", "terminalErrorCode"}
    ):
        raise DeploymentCheckpointConflict()
    raw_outputs = descriptor.get("buildOutputs", [])
    raw_volumes = descriptor.get("volumeBindings", [])
    if not isinstance(raw_outputs, list) or not isinstance(raw_volumes, list):
        raise DeploymentCheckpointConflict()
    try:
        outputs = tuple(
            VerifiedBuildOutput(
                build_key=item["buildKey"],
                service_key=item["serviceKey"],
                image_digest=item["imageDigest"],
                sbom_digest=item["sbomDigest"],
                provenance_digest=item["provenanceDigest"],
                scan_digest=item["scanDigest"],
            )
            for item in raw_outputs
            if isinstance(item, dict)
            and set(item)
            == {
                "buildKey",
                "serviceKey",
                "imageDigest",
                "sbomDigest",
                "provenanceDigest",
                "scanDigest",
            }
        )
        volumes = tuple(
            RuntimeVolumeBinding(item["key"], item["volumeRef"])
            for item in raw_volumes
            if isinstance(item, dict) and set(item) == {"key", "volumeRef"}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentCheckpointConflict() from exc
    if len(outputs) != len(raw_outputs) or len(volumes) != len(raw_volumes):
        raise DeploymentCheckpointConflict()
    terminal_error = descriptor.get("terminalErrorCode")
    if terminal_error is not None and not isinstance(terminal_error, str):
        raise DeploymentCheckpointConflict()
    return DeploymentOrchestrationState(
        operation_id=row.operation_id,
        version=row.checkpoint_version,
        tenant_ref=row.tenant_id,
        application_ref=row.application_id,
        deployment_ref=row.deployment_id,
        revision_ref=row.revision_id,
        phase=row.phase,
        builder_task_id=row.builder_task_id,
        builder_cursor=row.builder_cursor,
        builder_status=row.builder_status,
        build_cancel_forwarded=row.build_cancel_forwarded,
        build_outputs=outputs,
        manifest_digest=row.manifest_digest,
        normalized_compose_digest=row.normalized_compose_digest,
        volume_bindings=volumes,
        luma_deployment_ref=row.luma_deployment_ref,
        runtime_status=row.runtime_status,
        runtime_cancel_forwarded=row.runtime_cancel_forwarded,
        terminal_error_code=terminal_error,
        deadline_at=row.deadline_at,
    )


def _outputs_from_rows(
    rows: list[DeploymentBuildOutput],
) -> tuple[VerifiedBuildOutput, ...]:
    return tuple(
        VerifiedBuildOutput(
            build_key=row.build_key,
            service_key=row.service_key,
            image_digest=row.image_digest,
            sbom_digest=row.sbom_digest,
            provenance_digest=row.provenance_digest,
            scan_digest=row.scan_digest,
        )
        for row in sorted(rows, key=lambda item: item.build_key)
    )


def _require_operation_record(operation: OperationRecord) -> None:
    if (
        operation.kind != "deployment.create"
        or operation.target_type != "application"
        or operation.status != "running"
        or operation.lease_owner is None
    ):
        raise DeploymentContextInvalid()


def _schema_unavailable(exc: DBAPIError) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if getattr(current, "sqlstate", None) in _SCHEMA_SQLSTATES:
            return True
        current = current.__cause__
    return False


def _raise_database_error(exc: DBAPIError) -> None:
    if _schema_unavailable(exc):
        raise DeploymentSchemaUnavailable() from None
    raise exc


__all__ = [
    "DeploymentSchemaUnavailable",
    "PostgresDeploymentContextLoader",
    "PostgresDeploymentStateStore",
    "StoredBuildPlanArtifact",
    "TrustedBuildPlan",
    "TrustedBuildPlanMaterializer",
    "TrustedBuildPlanUnavailable",
    "UnconfiguredTrustedBuildPlanMaterializer",
]
