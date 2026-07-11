from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .application_catalog import new_managed_hostname
from .errors import (
    DeploymentConflict,
    DeploymentEnvironmentIncomplete,
    DeploymentEnvironmentScopeInvalid,
    DeploymentPlanInvalid,
    DeploymentPlanUnavailable,
    DeploymentQuotaExceeded,
    DeploymentTopologyConflict,
    EnvironmentVersionConflict,
    IdempotencyKeyReused,
    InvalidPlanLimits,
    OperationConflict,
    ResourceNotFound,
    SubscriptionUnavailable,
)
from .ids import new_id, require_opaque_id
from .models import (
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
    IdempotencyRecord,
    Operation,
    PlanVersion,
    SourceRevision,
    Subscription,
)
from .repositories import (
    EventInput,
    IdempotencyInput,
    Principal,
    TenantScope,
    _append_event,
)
from .security import ensure_persistable_payload

DEPLOYMENT_CREATE_ROUTE = "/v1/applications/{application_id}/deployments"

_CATALOG_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOSTNAME = re.compile(r"^[0-9a-f]{32}\.itool\.tech$")
_PUBLIC_ERROR_CODE = re.compile(r"^LAE_[A-Z0-9_]{1,92}$")
_ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")
_ACTIVE_OPERATION_STATUSES = ("queued", "running")
_SERVICE_ROLES = frozenset({"http", "internal", "worker", "datastore"})


def _catalog_key(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _CATALOG_KEY.fullmatch(value) is None:
        raise DeploymentPlanInvalid(f"{field_name} has invalid format")
    return value


def _digest(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise DeploymentPlanInvalid(f"{field_name} is not an immutable digest")
    return value


@dataclass(frozen=True, slots=True)
class StoredDeploymentPlanArtifact:
    """Private descriptor passed only to the trusted artifact resolver.

    ``storage_key`` is deliberately excluded from repr and from every public
    record. The admission transaction rechecks every descriptor field after
    resolution, so a replaced or cross-tenant catalog row cannot be admitted.
    """

    artifact_id: str
    analysis_id: str
    source_revision_id: str
    digest: str
    media_type: str
    size_bytes: int
    storage_key: str = field(repr=False)
    source_snapshot_digest: str | None = None

    def __post_init__(self) -> None:
        require_opaque_id(self.artifact_id, prefix="art")
        require_opaque_id(self.analysis_id, prefix="ana")
        require_opaque_id(self.source_revision_id, prefix="src")
        _digest(self.digest, field_name="deployment plan digest")
        if self.source_snapshot_digest is not None:
            _digest(
                self.source_snapshot_digest,
                field_name="deployment plan source snapshot digest",
            )
        if self.media_type != "application/vnd.lae.deployment-plan+json":
            raise DeploymentPlanInvalid("deployment plan media type is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not 1 <= self.size_bytes <= 16 * 1024 * 1024
        ):
            raise DeploymentPlanInvalid("deployment plan artifact size is invalid")
        if (
            not isinstance(self.storage_key, str)
            or not 1 <= len(self.storage_key) <= 1024
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in self.storage_key)
        ):
            raise DeploymentPlanInvalid("deployment plan storage binding is invalid")


@dataclass(frozen=True, slots=True)
class PreparedService:
    service_key: str
    role: str
    required: bool = True

    def __post_init__(self) -> None:
        _catalog_key(self.service_key, field_name="service key")
        if self.role not in _SERVICE_ROLES:
            # cron, host-network helpers and transport relays are outside LAE v1.
            raise DeploymentPlanInvalid("service role is unsupported")
        if not isinstance(self.required, bool):
            raise DeploymentPlanInvalid("service required flag is invalid")


@dataclass(frozen=True, slots=True)
class PreparedHttpRoute:
    service_key: str
    container_port: int
    is_primary: bool = False

    def __post_init__(self) -> None:
        _catalog_key(self.service_key, field_name="route service key")
        if (
            isinstance(self.container_port, bool)
            or not isinstance(self.container_port, int)
            or not 1 <= self.container_port <= 65535
        ):
            raise DeploymentPlanInvalid("HTTP route container port is invalid")
        if not isinstance(self.is_primary, bool):
            raise DeploymentPlanInvalid("HTTP route primary flag is invalid")


@dataclass(frozen=True, slots=True)
class PreparedVolume:
    volume_key: str
    requested_bytes: int
    backup_policy: str = "none"
    delete_policy: str = "retain"

    def __post_init__(self) -> None:
        _catalog_key(self.volume_key, field_name="volume key")
        if (
            isinstance(self.requested_bytes, bool)
            or not isinstance(self.requested_bytes, int)
            or self.requested_bytes <= 0
        ):
            raise DeploymentPlanInvalid("volume requested bytes is invalid")
        if self.backup_policy not in {"none", "manual", "scheduled"}:
            raise DeploymentPlanInvalid("volume backup policy is unsupported")
        if self.delete_policy not in {"retain", "delete"}:
            raise DeploymentPlanInvalid("volume delete policy is unsupported")


@dataclass(frozen=True, slots=True)
class PreparedEnvironmentVariable:
    name: str
    service_keys: tuple[str, ...]
    required: bool
    sensitive: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _ENV_NAME.fullmatch(self.name) is None:
            raise DeploymentPlanInvalid("environment name has invalid format")
        if not self.service_keys or len(self.service_keys) != len(
            set(self.service_keys)
        ):
            raise DeploymentPlanInvalid(
                "environment services must be nonempty and unique"
            )
        for service_key in self.service_keys:
            _catalog_key(service_key, field_name="environment service key")
        if not isinstance(self.required, bool) or not isinstance(self.sensitive, bool):
            raise DeploymentPlanInvalid("environment flags are invalid")


@dataclass(frozen=True, slots=True)
class PreparedDeploymentPlan:
    """Trusted, secret-free deployment facts produced from one stored artifact.

    There is intentionally no public constructor from an API request. A
    ``PlanResolver`` must retrieve and parse the verified artifact, resolve all
    runtime images to immutable digests, and produce this bounded shape.
    """

    source_revision_id: str
    kind: str
    services: tuple[PreparedService, ...]
    routes: tuple[PreparedHttpRoute, ...]
    volumes: tuple[PreparedVolume, ...]
    environment: tuple[PreparedEnvironmentVariable, ...]
    # The final Luma manifest contains post-build image digests and therefore
    # does not exist at admission time. Candidate revisions keep this null;
    # the deployment worker writes the verified digest atomically on activate.
    luma_manifest_digest: str | None
    environment_schema_digest: str
    normalized_compose_digest: str | None = None

    def __post_init__(self) -> None:
        require_opaque_id(self.source_revision_id, prefix="src")
        if self.kind not in {"service", "compose"}:
            raise DeploymentPlanInvalid("deployment plan kind is unsupported")
        if not self.services:
            raise DeploymentPlanInvalid("deployment plan must contain a service")
        if self.kind == "service" and len(self.services) != 1:
            raise DeploymentPlanInvalid("service plan must contain exactly one service")
        if self.kind == "service" and self.normalized_compose_digest is not None:
            raise DeploymentPlanInvalid("service plan cannot contain a compose digest")
        if self.kind == "compose" and self.normalized_compose_digest is None:
            raise DeploymentPlanInvalid(
                "compose plan requires a normalized compose digest"
            )
        if self.luma_manifest_digest is not None:
            _digest(self.luma_manifest_digest, field_name="Luma manifest digest")
        _digest(self.environment_schema_digest, field_name="environment schema digest")
        if self.normalized_compose_digest is not None:
            _digest(
                self.normalized_compose_digest, field_name="normalized compose digest"
            )

        service_keys = [service.service_key for service in self.services]
        if len(service_keys) != len(set(service_keys)):
            raise DeploymentPlanInvalid("deployment service keys must be unique")
        by_key = {service.service_key: service for service in self.services}
        route_keys = [
            (route.service_key, route.container_port) for route in self.routes
        ]
        if len(route_keys) != len(set(route_keys)):
            raise DeploymentPlanInvalid("deployment HTTP routes must be unique")
        if self.routes and sum(route.is_primary for route in self.routes) != 1:
            raise DeploymentPlanInvalid("HTTP routes require exactly one primary route")
        for route in self.routes:
            service = by_key.get(route.service_key)
            if service is None or service.role != "http":
                raise DeploymentPlanInvalid("HTTP route must target an HTTP service")

        volume_keys = [volume.volume_key for volume in self.volumes]
        if len(volume_keys) != len(set(volume_keys)):
            raise DeploymentPlanInvalid("deployment volume keys must be unique")
        environment_keys: set[tuple[str, str]] = set()
        for variable in self.environment:
            for service_key in variable.service_keys:
                if service_key not in by_key:
                    raise DeploymentPlanInvalid(
                        "environment variable targets an unknown service"
                    )
                key = (service_key, variable.name)
                if key in environment_keys:
                    raise DeploymentPlanInvalid(
                        "environment schema contains duplicates"
                    )
                environment_keys.add(key)


class PlanResolver(Protocol):
    """Private object-store boundary for a prepared deployment plan.

    Implementations must stream at most ``artifact.size_bytes`` bytes, verify
    the exact size and SHA-256 digest before JSON parsing, validate the
    canonical deployment-plan schema, resolve every runtime image to an
    immutable digest, and derive the manifest/environment schema digests. They
    must not accept redirects, caller URLs, credentials, or manifest fragments.
    """

    async def resolve(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> PreparedDeploymentPlan: ...


class UnconfiguredPlanResolver:
    """Production-safe default until the private object-store adapter is wired."""

    async def resolve(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> PreparedDeploymentPlan:
        del artifact
        raise DeploymentPlanUnavailable(
            "deployment plan object-store resolver is not configured"
        )


@dataclass(frozen=True, slots=True)
class CreateDeploymentAdmission:
    scope: TenantScope
    application_id: str
    analysis_id: str
    environment_version: int

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        require_opaque_id(self.analysis_id, prefix="ana")
        if (
            isinstance(self.environment_version, bool)
            or not isinstance(self.environment_version, int)
            or self.environment_version < 0
        ):
            raise ValueError("environment version must be nonnegative")


@dataclass(frozen=True, slots=True)
class PublicDeploymentRecord:
    id: str
    application_id: str
    revision_id: str
    operation_id: str
    status: str
    previous_deployment_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    created_at: datetime

    def public_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "id": self.id,
            "applicationId": self.application_id,
            "revisionId": self.revision_id,
            "operationId": self.operation_id,
            "status": self.status,
            "previousDeploymentId": self.previous_deployment_id,
            "startedAt": _timestamp(self.started_at),
            "finishedAt": _timestamp(self.finished_at),
            "createdAt": _timestamp(self.created_at),
            "links": {
                "operation": f"/v1/operations/{self.operation_id}",
                "events": f"/v1/operations/{self.operation_id}/events",
            },
        }
        if (
            self.status == "failed"
            and self.error_code
            and _PUBLIC_ERROR_CODE.fullmatch(self.error_code)
        ):
            body["error"] = {"code": self.error_code, "message": "Deployment failed"}
        return body


@dataclass(frozen=True, slots=True)
class DeploymentAdmissionResult:
    deployment: PublicDeploymentRecord
    operation_id: str
    operation_status: str
    operation_phase: str
    operation_cursor: int
    replayed: bool

    def public_body(self) -> dict[str, object]:
        return {
            "deployment": self.deployment.public_body(),
            "operation": {
                "id": self.operation_id,
                "status": self.operation_status,
                "phase": self.operation_phase,
                "cursor": self.operation_cursor,
                "links": {"events": f"/v1/operations/{self.operation_id}/events"},
            },
        }


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_deployment(row: Deployment) -> PublicDeploymentRecord:
    return PublicDeploymentRecord(
        id=row.id,
        application_id=row.application_id,
        revision_id=row.revision_id,
        operation_id=row.operation_id,
        status=row.status,
        previous_deployment_id=row.previous_deployment_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
        created_at=row.created_at,
    )


class DeploymentAdmissionStore:
    """Tenant-fenced admission for immutable application deployments."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        hostname_factory: Callable[[], str] = new_managed_hostname,
    ) -> None:
        self._sessions = sessions
        self._hostname_factory = hostname_factory

    async def lookup_replay(
        self,
        scope: TenantScope,
        principal: Principal,
        idempotency: IdempotencyInput,
    ) -> DeploymentAdmissionResult | None:
        """Return a safe historical response before touching object storage."""

        self._validate_idempotency(idempotency)
        async with self._sessions() as session:
            now = await session.scalar(select(func.now()))
            existing = await self._idempotency_record(
                session, scope, principal, idempotency, for_update=False
            )
            if existing is None or now is None or existing.expires_at <= now:
                return None
            return await self._replay(session, scope, existing, idempotency)

    async def get_plan_artifact(
        self,
        scope: TenantScope,
        application_id: str,
        analysis_id: str,
    ) -> StoredDeploymentPlanArtifact:
        require_opaque_id(application_id, prefix="app")
        require_opaque_id(analysis_id, prefix="ana")
        async with self._sessions() as session:
            descriptor = await self._plan_artifact_in_session(
                session, scope, application_id, analysis_id, for_update=False
            )
        if descriptor is None:
            # Missing, foreign, denied, unverified and unlinked analyses are
            # deliberately indistinguishable at the public boundary.
            raise ResourceNotFound("deployable stored analysis not found")
        return descriptor

    async def admit(
        self,
        command: CreateDeploymentAdmission,
        *,
        principal: Principal,
        idempotency: IdempotencyInput,
        artifact: StoredDeploymentPlanArtifact,
        plan: PreparedDeploymentPlan,
    ) -> DeploymentAdmissionResult:
        self._validate_idempotency(idempotency)
        if artifact.analysis_id != command.analysis_id:
            raise DeploymentPlanInvalid("deployment artifact analysis binding changed")
        if plan.source_revision_id != artifact.source_revision_id:
            raise DeploymentPlanInvalid("deployment plan source binding changed")
        lock_scope = (
            f"lae:deployment-create:{command.scope.tenant_id}:{principal.type}:"
            f"{principal.id}:{idempotency.key}"
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:deployment_idempotency_scope, 0))"
                        ),
                        {"deployment_idempotency_scope": lock_scope},
                    )
                    now = await session.scalar(select(func.now()))
                    if now is None:
                        raise OperationConflict("database clock is unavailable")
                    existing = await self._idempotency_record(
                        session,
                        command.scope,
                        principal,
                        idempotency,
                        for_update=True,
                    )
                    if existing is not None and existing.expires_at > now:
                        return await self._replay(
                            session, command.scope, existing, idempotency
                        )
                    if existing is not None:
                        await session.delete(existing)
                        await session.flush()

                    # One tenant-wide lock makes the concurrent deployment
                    # entitlement a strict upper bound across applications.
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:deployment_quota_scope, 0))"
                        ),
                        {
                            "deployment_quota_scope": (
                                f"lae:deployment-quota:{command.scope.tenant_id}"
                            )
                        },
                    )
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended(:application_scope, 0))"
                        ),
                        {
                            "application_scope": (
                                f"lae:application-catalog:{command.scope.tenant_id}:"
                                f"{command.application_id}"
                            )
                        },
                    )
                    application = await session.scalar(
                        select(Application)
                        .where(
                            Application.tenant_id == command.scope.tenant_id,
                            Application.id == command.application_id,
                            Application.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if application is None:
                        raise ResourceNotFound("application not found")
                    if application.environment_version != command.environment_version:
                        raise EnvironmentVersionConflict(
                            expected=command.environment_version,
                            actual=application.environment_version,
                        )

                    locked_artifact = await self._plan_artifact_in_session(
                        session,
                        command.scope,
                        command.application_id,
                        command.analysis_id,
                        for_update=True,
                    )
                    if locked_artifact is None or locked_artifact != artifact:
                        raise DeploymentPlanInvalid(
                            "deployment plan artifact binding changed"
                        )

                    limits = await self._active_plan_limits(session, command.scope)
                    await self._enforce_deployment_quota(
                        session, command.scope, command.application_id, limits
                    )
                    if application.kind == "pending":
                        await self._enforce_topology_limits(
                            session, command.scope, plan, limits
                        )
                        await self._materialize_topology(
                            session, command.scope, application, plan
                        )
                    else:
                        await self._require_compatible_topology(
                            session, command.scope, application, plan
                        )
                    await self._require_environment(
                        session, command.scope, command.application_id, plan
                    )

                    revision_no = (
                        int(
                            await session.scalar(
                                select(
                                    func.coalesce(func.max(AppRevision.revision_no), 0)
                                ).where(
                                    AppRevision.tenant_id == command.scope.tenant_id,
                                    AppRevision.application_id
                                    == command.application_id,
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    revision = AppRevision(
                        id=new_id("rev"),
                        tenant_id=command.scope.tenant_id,
                        application_id=command.application_id,
                        revision_no=revision_no,
                        analysis_id=command.analysis_id,
                        source_revision_id=artifact.source_revision_id,
                        kind=plan.kind,
                        deployment_plan_artifact_id=artifact.artifact_id,
                        deployment_plan_digest=artifact.digest,
                        normalized_compose_digest=plan.normalized_compose_digest,
                        luma_manifest_digest=plan.luma_manifest_digest,
                        environment_schema_digest=plan.environment_schema_digest,
                        environment_version=command.environment_version,
                        status="candidate",
                        created_by_type=principal.type,
                        created_by_id=principal.id,
                    )
                    session.add(revision)
                    await session.flush()

                    operation = Operation(
                        id=new_id("op"),
                        tenant_id=command.scope.tenant_id,
                        principal_type=principal.type,
                        principal_id=principal.id,
                        kind="deployment.create",
                        target_type="application",
                        target_id=command.application_id,
                        status="queued",
                        phase="deploy.prepare",
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()
                    deployment = Deployment(
                        id=new_id("dep"),
                        tenant_id=command.scope.tenant_id,
                        application_id=command.application_id,
                        revision_id=revision.id,
                        operation_id=operation.id,
                        status="queued",
                        previous_deployment_id=application.current_deployment_id,
                    )
                    session.add(deployment)
                    await session.flush()
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.queued",
                            phase="deploy.prepare",
                            status="queued",
                            message="Operation queued",
                            data={},
                        ),
                    )
                    await session.flush()

                    result = DeploymentAdmissionResult(
                        deployment=_public_deployment(deployment),
                        operation_id=operation.id,
                        operation_status=operation.status,
                        operation_phase=operation.phase or "deploy.prepare",
                        operation_cursor=operation.last_event_seq,
                        replayed=False,
                    )
                    response_body = result.public_body()
                    ensure_persistable_payload(response_body)
                    session.add(
                        IdempotencyRecord(
                            tenant_id=command.scope.tenant_id,
                            principal_type=principal.type,
                            principal_id=principal.id,
                            key=idempotency.key,
                            method=idempotency.method,
                            route_template=idempotency.route_template,
                            request_hash=idempotency.request_hash,
                            response_status=202,
                            response_body=response_body,
                            operation_id=operation.id,
                            expires_at=now + idempotency.retention,
                        )
                    )
                    await session.flush()
                    return result
        except (
            DeploymentConflict,
            DeploymentEnvironmentIncomplete,
            DeploymentPlanInvalid,
            DeploymentQuotaExceeded,
            DeploymentTopologyConflict,
            EnvironmentVersionConflict,
            IdempotencyKeyReused,
            InvalidPlanLimits,
            ResourceNotFound,
            SubscriptionUnavailable,
        ):
            raise
        except IntegrityError as exc:
            raise DeploymentConflict(
                "deployment admission conflicts with durable state"
            ) from exc

    async def list_deployments(
        self,
        scope: TenantScope,
        application_id: str,
        *,
        limit: int = 100,
    ) -> tuple[PublicDeploymentRecord, ...]:
        require_opaque_id(application_id, prefix="app")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("deployment list limit must be 1-100")
        async with self._sessions() as session:
            application = await session.scalar(
                select(Application.id).where(
                    Application.tenant_id == scope.tenant_id,
                    Application.id == application_id,
                    Application.deleted_at.is_(None),
                )
            )
            if application is None:
                raise ResourceNotFound("application not found")
            rows = await session.scalars(
                select(Deployment)
                .where(
                    Deployment.tenant_id == scope.tenant_id,
                    Deployment.application_id == application_id,
                )
                .order_by(Deployment.created_at.desc(), Deployment.id.desc())
                .limit(limit)
            )
            return tuple(_public_deployment(row) for row in rows)

    async def get_deployment(
        self,
        scope: TenantScope,
        application_id: str,
        deployment_id: str,
    ) -> PublicDeploymentRecord:
        require_opaque_id(application_id, prefix="app")
        require_opaque_id(deployment_id, prefix="dep")
        async with self._sessions() as session:
            row = await session.scalar(
                select(Deployment).where(
                    Deployment.tenant_id == scope.tenant_id,
                    Deployment.application_id == application_id,
                    Deployment.id == deployment_id,
                )
            )
            if row is None:
                raise ResourceNotFound("deployment not found")
            return _public_deployment(row)

    @staticmethod
    def _validate_idempotency(idempotency: IdempotencyInput) -> None:
        if (
            idempotency.method != "POST"
            or idempotency.route_template != DEPLOYMENT_CREATE_ROUTE
        ):
            raise ValueError("deployment idempotency scope is invalid")

    @staticmethod
    async def _idempotency_record(
        session: AsyncSession,
        scope: TenantScope,
        principal: Principal,
        idempotency: IdempotencyInput,
        *,
        for_update: bool,
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == scope.tenant_id,
            IdempotencyRecord.principal_type == principal.type,
            IdempotencyRecord.principal_id == principal.id,
            IdempotencyRecord.method == idempotency.method,
            IdempotencyRecord.route_template == idempotency.route_template,
            IdempotencyRecord.key == idempotency.key,
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    @staticmethod
    async def _replay(
        session: AsyncSession,
        scope: TenantScope,
        existing: IdempotencyRecord,
        idempotency: IdempotencyInput,
    ) -> DeploymentAdmissionResult:
        if not hmac.compare_digest(existing.request_hash, idempotency.request_hash):
            raise IdempotencyKeyReused("idempotency key was used for another request")
        row = (
            await session.execute(
                select(Deployment, Operation)
                .join(
                    Operation,
                    (Operation.tenant_id == Deployment.tenant_id)
                    & (Operation.id == Deployment.operation_id),
                )
                .where(
                    Deployment.tenant_id == scope.tenant_id,
                    Deployment.operation_id == existing.operation_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise OperationConflict("idempotent deployment state is incomplete")
        deployment, operation = row
        body = existing.response_body
        try:
            deployment_body = body["deployment"]
            operation_body = body["operation"]
            if (
                set(body) != {"deployment", "operation"}
                or not isinstance(deployment_body, dict)
                or not isinstance(operation_body, dict)
                or deployment_body["id"] != deployment.id
                or deployment_body["applicationId"] != deployment.application_id
                or deployment_body["revisionId"] != deployment.revision_id
                or deployment_body["operationId"] != operation.id
                or deployment_body["status"] != "queued"
                or operation_body["id"] != operation.id
                or operation_body["status"] != "queued"
                or operation_body["phase"] != "deploy.prepare"
                or operation_body["cursor"] != 1
                or existing.response_status != 202
            ):
                raise KeyError("invalid historical response")
        except (KeyError, TypeError) as exc:
            raise OperationConflict(
                "idempotent deployment response is invalid"
            ) from exc
        ensure_persistable_payload(body)
        return DeploymentAdmissionResult(
            deployment=PublicDeploymentRecord(
                id=deployment.id,
                application_id=deployment.application_id,
                revision_id=deployment.revision_id,
                operation_id=deployment.operation_id,
                status="queued",
                previous_deployment_id=deployment_body.get("previousDeploymentId"),
                started_at=None,
                finished_at=None,
                error_code=None,
                created_at=deployment.created_at,
            ),
            operation_id=operation.id,
            operation_status="queued",
            operation_phase="deploy.prepare",
            operation_cursor=1,
            replayed=True,
        )

    @staticmethod
    async def _plan_artifact_in_session(
        session: AsyncSession,
        scope: TenantScope,
        application_id: str,
        analysis_id: str,
        *,
        for_update: bool,
    ) -> StoredDeploymentPlanArtifact | None:
        statement = (
            select(Analysis, AnalysisArtifact, Artifact, SourceRevision)
            .join(
                AnalysisArtifact,
                (AnalysisArtifact.tenant_id == Analysis.tenant_id)
                & (AnalysisArtifact.analysis_id == Analysis.id)
                & (AnalysisArtifact.name == "deploymentPlan"),
            )
            .join(
                Artifact,
                (Artifact.tenant_id == AnalysisArtifact.tenant_id)
                & (Artifact.id == AnalysisArtifact.artifact_id),
            )
            .join(
                SourceRevision,
                (SourceRevision.tenant_id == Analysis.tenant_id)
                & (SourceRevision.id == Analysis.source_revision_id),
            )
            .where(
                Analysis.tenant_id == scope.tenant_id,
                Analysis.application_id == application_id,
                Analysis.id == analysis_id,
                # ``needs_configuration`` is a deployable plan whose final
                # admission is gated by _require_environment below. Keeping it
                # resolvable is what lets users add the named values and retry
                # without rerunning source analysis.
                Analysis.status.in_(("deployable", "needs_configuration")),
                Analysis.artifact_state == "stored",
                Analysis.plan_stored.is_(True),
                Artifact.kind == "deployment-plan",
                Artifact.digest == Analysis.deployment_plan_digest,
                Artifact.media_type == "application/vnd.lae.deployment-plan+json",
                Artifact.upload_status == "verified",
                Artifact.storage_key.is_not(None),
                SourceRevision.application_id == application_id,
                SourceRevision.deleted_at.is_(None),
            )
        )
        if for_update:
            statement = statement.with_for_update(
                of=(Analysis, Artifact, SourceRevision)
            )
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        analysis, _link, artifact, source = row
        assert artifact.storage_key is not None
        return StoredDeploymentPlanArtifact(
            artifact_id=artifact.id,
            analysis_id=analysis.id,
            source_revision_id=source.id,
            digest=artifact.digest,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            storage_key=artifact.storage_key,
            source_snapshot_digest=analysis.source_snapshot_digest,
        )

    @staticmethod
    async def _active_plan_limits(
        session: AsyncSession, scope: TenantScope
    ) -> dict[str, object]:
        row = (
            await session.execute(
                select(PlanVersion.limits_json)
                .join(Subscription, Subscription.plan_version_id == PlanVersion.id)
                .where(
                    Subscription.tenant_id == scope.tenant_id,
                    Subscription.status.in_(_ACTIVE_SUBSCRIPTION_STATUSES),
                )
                .with_for_update(of=Subscription)
            )
        ).one_or_none()
        if row is None:
            raise SubscriptionUnavailable("active subscription not found")
        limits = row[0]
        if not isinstance(limits, dict):
            raise InvalidPlanLimits("active plan limits are invalid")
        return limits

    @staticmethod
    def _limit(limits: dict[str, object], key: str) -> int:
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidPlanLimits(f"active plan limit {key!r} is invalid")
        return value

    async def _enforce_deployment_quota(
        self,
        session: AsyncSession,
        scope: TenantScope,
        application_id: str,
        limits: dict[str, object],
    ) -> None:
        active_for_application = await session.scalar(
            select(Operation.id)
            .where(
                Operation.tenant_id == scope.tenant_id,
                Operation.kind == "deployment.create",
                Operation.target_type == "application",
                Operation.target_id == application_id,
                Operation.status.in_(_ACTIVE_OPERATION_STATUSES),
            )
            .limit(1)
        )
        if active_for_application is not None:
            raise DeploymentConflict("application already has an active deployment")
        active_count = await session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(
                Operation.tenant_id == scope.tenant_id,
                Operation.kind == "deployment.create",
                Operation.status.in_(_ACTIVE_OPERATION_STATUSES),
            )
        )
        if int(active_count or 0) >= self._limit(limits, "concurrentDeployments"):
            raise DeploymentQuotaExceeded("concurrent deployment quota exceeded")

    async def _enforce_topology_limits(
        self,
        session: AsyncSession,
        scope: TenantScope,
        plan: PreparedDeploymentPlan,
        limits: dict[str, object],
    ) -> None:
        if len(plan.services) > self._limit(limits, "servicesPerApp"):
            raise DeploymentQuotaExceeded("services-per-application quota exceeded")
        if len(plan.routes) > self._limit(limits, "publicHttpRoutesPerApp"):
            raise DeploymentQuotaExceeded("public HTTP route quota exceeded")
        current_volume_bytes = await session.scalar(
            select(func.coalesce(func.sum(ApplicationVolume.requested_bytes), 0)).where(
                ApplicationVolume.tenant_id == scope.tenant_id
            )
        )
        requested_volume_bytes = sum(volume.requested_bytes for volume in plan.volumes)
        if int(current_volume_bytes or 0) + requested_volume_bytes > self._limit(
            limits, "persistentVolumeBytes"
        ):
            raise DeploymentQuotaExceeded("persistent volume byte quota exceeded")

    async def _materialize_topology(
        self,
        session: AsyncSession,
        scope: TenantScope,
        application: Application,
        plan: PreparedDeploymentPlan,
    ) -> None:
        services: dict[str, ApplicationService] = {}
        for spec in plan.services:
            row = ApplicationService(
                id=new_id("svc"),
                tenant_id=scope.tenant_id,
                application_id=application.id,
                service_key=spec.service_key,
                role=spec.role,
                required=spec.required,
            )
            services[spec.service_key] = row
            session.add(row)
        await session.flush()
        for spec in plan.routes:
            session.add(
                ApplicationRoute(
                    id=new_id("rte"),
                    tenant_id=scope.tenant_id,
                    application_id=application.id,
                    service_id=services[spec.service_key].id,
                    kind="http",
                    hostname=self._next_hostname(),
                    is_primary=spec.is_primary,
                    container_port=spec.container_port,
                )
            )
        session.add_all(
            [
                ApplicationVolume(
                    id=new_id("vol"),
                    tenant_id=scope.tenant_id,
                    application_id=application.id,
                    volume_key=spec.volume_key,
                    requested_bytes=spec.requested_bytes,
                    storage_policy="managed",
                    backup_policy=spec.backup_policy,
                    delete_policy=spec.delete_policy,
                )
                for spec in plan.volumes
            ]
        )
        application.kind = plan.kind
        application.updated_at = await session.scalar(select(func.now()))
        await session.flush()

    @staticmethod
    async def _require_compatible_topology(
        session: AsyncSession,
        scope: TenantScope,
        application: Application,
        plan: PreparedDeploymentPlan,
    ) -> None:
        if application.kind != plan.kind:
            raise DeploymentTopologyConflict(
                "deployment kind changes existing topology"
            )
        service_rows = list(
            await session.scalars(
                select(ApplicationService).where(
                    ApplicationService.tenant_id == scope.tenant_id,
                    ApplicationService.application_id == application.id,
                )
            )
        )
        actual_services = {
            (row.service_key, row.role, row.required) for row in service_rows
        }
        expected_services = {
            (spec.service_key, spec.role, spec.required) for spec in plan.services
        }
        if actual_services != expected_services:
            raise DeploymentTopologyConflict(
                "deployment services change existing topology"
            )
        service_keys = {row.id: row.service_key for row in service_rows}
        route_rows = list(
            await session.scalars(
                select(ApplicationRoute).where(
                    ApplicationRoute.tenant_id == scope.tenant_id,
                    ApplicationRoute.application_id == application.id,
                )
            )
        )
        actual_routes = {
            (service_keys.get(row.service_id), row.container_port, row.is_primary)
            for row in route_rows
        }
        expected_routes = {
            (spec.service_key, spec.container_port, spec.is_primary)
            for spec in plan.routes
        }
        if actual_routes != expected_routes:
            raise DeploymentTopologyConflict(
                "deployment routes change existing topology"
            )
        volume_rows = list(
            await session.scalars(
                select(ApplicationVolume).where(
                    ApplicationVolume.tenant_id == scope.tenant_id,
                    ApplicationVolume.application_id == application.id,
                )
            )
        )
        actual_volumes = {
            (
                row.volume_key,
                row.requested_bytes,
                row.backup_policy,
                row.delete_policy,
            )
            for row in volume_rows
        }
        expected_volumes = {
            (
                spec.volume_key,
                spec.requested_bytes,
                spec.backup_policy,
                spec.delete_policy,
            )
            for spec in plan.volumes
        }
        if actual_volumes != expected_volumes:
            raise DeploymentTopologyConflict(
                "deployment volumes change existing topology"
            )

    @staticmethod
    async def _require_environment(
        session: AsyncSession,
        scope: TenantScope,
        application_id: str,
        plan: PreparedDeploymentPlan,
    ) -> None:
        configured = set(
            await session.execute(
                select(
                    ApplicationEnvironmentVariable.service_scope,
                    ApplicationEnvironmentVariable.name,
                ).where(
                    ApplicationEnvironmentVariable.tenant_id == scope.tenant_id,
                    ApplicationEnvironmentVariable.application_id == application_id,
                )
            )
        )
        _validate_environment_bindings(configured, plan)

    def _next_hostname(self) -> str:
        hostname = self._hostname_factory()
        if not isinstance(hostname, str) or _HOSTNAME.fullmatch(hostname) is None:
            raise DeploymentPlanInvalid(
                "managed hostname generator returned invalid data"
            )
        return hostname


def _validate_environment_bindings(
    configured: set[tuple[str, str]],
    plan: PreparedDeploymentPlan,
) -> None:
    """Fail closed when a stored value would escape its trusted plan scope."""

    service_keys = {service.service_key for service in plan.services}
    targets_by_name: dict[str, set[str]] = {}
    for variable in plan.environment:
        targets_by_name.setdefault(variable.name, set()).update(variable.service_keys)

    for service_scope, name in configured:
        if service_scope == "*":
            if len(service_keys) == 1:
                continue
            if targets_by_name.get(name) != service_keys:
                raise DeploymentEnvironmentScopeInvalid(
                    "wildcard environment scope is not declared for every service"
                )
            continue
        if service_scope not in service_keys:
            raise DeploymentEnvironmentScopeInvalid(
                "environment scope targets an unknown service"
            )
        declared_targets = targets_by_name.get(name)
        if declared_targets is not None and service_scope not in declared_targets:
            raise DeploymentEnvironmentScopeInvalid(
                "environment variable targets a service outside its plan binding"
            )

    missing: list[str] = []
    for variable in plan.environment:
        if not variable.required:
            continue
        wildcard_allowed = len(service_keys) == 1 or (
            targets_by_name.get(variable.name) == service_keys
        )
        for service_key in variable.service_keys:
            if (service_key, variable.name) not in configured and not (
                wildcard_allowed and ("*", variable.name) in configured
            ):
                missing.append(f"{service_key}:{variable.name}")
    if missing:
        # Variable names are schema metadata, never plaintext values.
        raise DeploymentEnvironmentIncomplete(
            "required deployment environment is not configured"
        )


__all__ = [
    "DEPLOYMENT_CREATE_ROUTE",
    "CreateDeploymentAdmission",
    "DeploymentAdmissionResult",
    "DeploymentAdmissionStore",
    "PlanResolver",
    "PreparedDeploymentPlan",
    "PreparedEnvironmentVariable",
    "PreparedHttpRoute",
    "PreparedService",
    "PreparedVolume",
    "PublicDeploymentRecord",
    "StoredDeploymentPlanArtifact",
    "UnconfiguredPlanResolver",
]
