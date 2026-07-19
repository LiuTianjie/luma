from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import bindparam, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import (
    ApplicationAlreadyMaterialized,
    ApplicationConflict,
    ApplicationQuotaExceeded,
    CustomDomainUnsupported,
    DeploymentEnvironmentScopeInvalid,
    EnvironmentVersionConflict,
    IdempotencyKeyReused,
    InvalidPlanLimits,
    ResourceNotFound,
    SubscriptionUnavailable,
)
from .ids import new_id, require_opaque_id
from .models import (
    Analysis,
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
from .repositories import IdempotencyInput, Principal, TenantScope
from .security import ensure_persistable_payload

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
_CATALOG_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HOST_LABEL = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")
_APPLICATION_KINDS = frozenset({"service", "compose"})
_SERVICE_ROLES = frozenset({"http", "internal", "worker", "datastore"})
_DESIRED_STATES = frozenset({"running", "suspended", "deleted"})
_OBSERVED_STATES = frozenset(
    {
        "provisioning",
        "running",
        "degraded",
        "failed",
        "suspending",
        "suspended",
        "unknown",
    }
)
APPLICATION_ENVIRONMENT_PATCH_ROUTE = "/v1/applications/{application_id}/environment"
PLAN_ENVIRONMENT_PATCH_ROUTE = (
    "/v1/applications/{application_id}/analyses/{analysis_id}/environment"
)


def _require_display_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 160
    ):
        raise ValueError("application name must contain 1-160 trimmed characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("application name must not contain control characters")
    return value


def _require_slug(value: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError("application slug has invalid format")
    return value


def _require_catalog_key(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _CATALOG_KEY.fullmatch(value):
        raise ValueError(f"{field} has invalid format")
    return value


def _require_digest(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


def _managed_hostname(label: str) -> str:
    if not isinstance(label, str) or not _HOST_LABEL.fullmatch(label):
        raise RuntimeError("managed hostname generator returned an invalid label")
    return f"{label}.itool.tech"


def new_managed_hostname() -> str:
    """Return a server-generated 128-bit lowercase hostname."""

    return _managed_hostname(secrets.token_hex(16))


def tenant_application_quota_lock_statement():
    return select(
        func.pg_advisory_xact_lock(
            func.hashtextextended(bindparam("tenant_application_quota_key"), 0)
        )
    )


def application_catalog_lock_statement():
    return select(
        func.pg_advisory_xact_lock(
            func.hashtextextended(bindparam("application_catalog_lock_key"), 0)
        )
    )


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    service_key: str
    role: str
    required: bool = True

    def __post_init__(self) -> None:
        _require_catalog_key(self.service_key, field="service_key")
        if self.role not in _SERVICE_ROLES:
            raise ValueError("service role is unsupported")
        if not isinstance(self.required, bool):
            raise ValueError("service required must be boolean")


@dataclass(frozen=True, slots=True)
class HttpRouteSpec:
    service_key: str
    container_port: int
    is_primary: bool = False
    requested_hostname: str | None = None

    def __post_init__(self) -> None:
        _require_catalog_key(self.service_key, field="route service_key")
        if (
            isinstance(self.container_port, bool)
            or not 1 <= self.container_port <= 65535
        ):
            raise ValueError("HTTP route container_port must be 1-65535")
        if not isinstance(self.is_primary, bool):
            raise ValueError("HTTP route is_primary must be boolean")
        if self.requested_hostname is not None:
            raise CustomDomainUnsupported(
                "custom domains are not supported; hostname is generated by LAE"
            )


@dataclass(frozen=True, slots=True)
class VolumeSpec:
    volume_key: str
    requested_bytes: int
    backup_policy: str = "none"
    delete_policy: str = "retain"

    def __post_init__(self) -> None:
        _require_catalog_key(self.volume_key, field="volume_key")
        if isinstance(self.requested_bytes, bool) or self.requested_bytes <= 0:
            raise ValueError("volume requested_bytes must be positive")
        if self.backup_policy not in {"none", "manual", "scheduled"}:
            raise ValueError("volume backup_policy is unsupported")
        if self.delete_policy not in {"retain", "delete"}:
            raise ValueError("volume delete_policy is unsupported")


@dataclass(frozen=True, slots=True)
class EncryptedEnvironmentValue:
    service_scope: str
    name: str
    envelope_ciphertext: bytes
    checksum: bytes
    key_version: int
    is_sensitive: bool = True
    required: bool = False
    source: str = "user"

    def __post_init__(self) -> None:
        if self.service_scope != "*":
            _require_catalog_key(self.service_scope, field="environment service_scope")
        if not isinstance(self.name, str) or not _ENV_NAME.fullmatch(self.name):
            raise ValueError("environment name has invalid format")
        if (
            not isinstance(self.envelope_ciphertext, bytes)
            or not 1 <= len(self.envelope_ciphertext) <= 1_048_576
        ):
            raise ValueError("environment envelope_ciphertext has invalid size")
        if not isinstance(self.checksum, bytes) or len(self.checksum) != 32:
            raise ValueError("environment checksum must contain 32 bytes")
        if isinstance(self.key_version, bool) or self.key_version <= 0:
            raise ValueError("environment key_version must be positive")
        if not isinstance(self.is_sensitive, bool) or not isinstance(
            self.required, bool
        ):
            raise ValueError("environment flags must be boolean")
        if self.source not in {"user", "analysis", "template", "system"}:
            raise ValueError("environment source is unsupported")

    @property
    def key(self) -> tuple[str, str]:
        return (self.service_scope, self.name)


@dataclass(frozen=True, slots=True)
class EnvironmentKey:
    service_scope: str
    name: str

    def __post_init__(self) -> None:
        if self.service_scope != "*":
            _require_catalog_key(self.service_scope, field="environment service_scope")
        if not isinstance(self.name, str) or not _ENV_NAME.fullmatch(self.name):
            raise ValueError("environment name has invalid format")

    @property
    def key(self) -> tuple[str, str]:
        return (self.service_scope, self.name)


def _validate_topology(
    *,
    kind: str,
    services: tuple[ServiceSpec, ...],
    routes: tuple[HttpRouteSpec, ...],
    volumes: tuple[VolumeSpec, ...],
    environment: tuple[EncryptedEnvironmentValue, ...],
) -> None:
    if kind not in _APPLICATION_KINDS:
        raise ValueError("materialized application kind is unsupported")
    if not services:
        raise ValueError("application must contain at least one service")
    if kind == "service" and len(services) != 1:
        raise ValueError("service application must contain exactly one service")

    service_keys = [item.service_key for item in services]
    if len(service_keys) != len(set(service_keys)):
        raise ValueError("service_key values must be unique")
    services_by_key = {item.service_key: item for item in services}
    primary_count = sum(route.is_primary for route in routes)
    if routes and primary_count != 1:
        raise ValueError("applications with HTTP routes require one primary route")
    for route in routes:
        service = services_by_key.get(route.service_key)
        if service is None:
            raise ValueError("HTTP route references an unknown service")
        if service.role != "http":
            raise ValueError("HTTP route must reference an HTTP service")

    volume_keys = [item.volume_key for item in volumes]
    if len(volume_keys) != len(set(volume_keys)):
        raise ValueError("volume_key values must be unique")
    environment_keys = [item.key for item in environment]
    if len(environment_keys) != len(set(environment_keys)):
        raise ValueError("environment keys must be unique")
    for value in environment:
        if value.service_scope != "*" and value.service_scope not in services_by_key:
            raise ValueError("environment value references an unknown service")


@dataclass(frozen=True, slots=True)
class CreateApplicationDraft:
    scope: TenantScope
    name: str
    slug: str

    def __post_init__(self) -> None:
        _require_display_name(self.name)
        _require_slug(self.slug)


@dataclass(frozen=True, slots=True)
class CreateApplication:
    scope: TenantScope
    name: str
    slug: str
    kind: str
    services: tuple[ServiceSpec, ...]
    routes: tuple[HttpRouteSpec, ...] = ()
    volumes: tuple[VolumeSpec, ...] = ()
    environment: tuple[EncryptedEnvironmentValue, ...] = ()

    def __post_init__(self) -> None:
        _require_display_name(self.name)
        _require_slug(self.slug)
        _validate_topology(
            kind=self.kind,
            services=self.services,
            routes=self.routes,
            volumes=self.volumes,
            environment=self.environment,
        )


@dataclass(frozen=True, slots=True)
class MaterializeApplicationTopology:
    scope: TenantScope
    application_id: str
    analysis_id: str
    kind: str
    services: tuple[ServiceSpec, ...]
    routes: tuple[HttpRouteSpec, ...] = ()
    volumes: tuple[VolumeSpec, ...] = ()
    environment: tuple[EncryptedEnvironmentValue, ...] = ()

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        require_opaque_id(self.analysis_id, prefix="ana")
        _validate_topology(
            kind=self.kind,
            services=self.services,
            routes=self.routes,
            volumes=self.volumes,
            environment=self.environment,
        )


@dataclass(frozen=True, slots=True)
class PatchEnvironment:
    scope: TenantScope
    application_id: str
    expected_version: int
    set_values: tuple[EncryptedEnvironmentValue, ...] = ()
    unset: tuple[EnvironmentKey, ...] = ()
    plan_analysis_id: str | None = None
    plan_environment_schema_digest: str | None = None
    plan_service_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        if isinstance(self.expected_version, bool) or self.expected_version < 0:
            raise ValueError("expected environment version must be nonnegative")
        if not self.set_values and not self.unset:
            raise ValueError("environment patch must contain at least one change")
        set_keys = [item.key for item in self.set_values]
        unset_keys = [item.key for item in self.unset]
        if len(set_keys) != len(set(set_keys)) or len(unset_keys) != len(
            set(unset_keys)
        ):
            raise ValueError("environment patch contains duplicate keys")
        if set(set_keys).intersection(unset_keys):
            raise ValueError("environment patch cannot set and unset the same key")
        plan_bound = self.plan_analysis_id is not None
        if plan_bound != (
            self.plan_environment_schema_digest is not None
        ) or plan_bound != bool(self.plan_service_keys):
            raise ValueError("environment plan binding must be complete")
        if plan_bound:
            require_opaque_id(self.plan_analysis_id or "", prefix="ana")
            _require_digest(
                self.plan_environment_schema_digest or "",
                field="plan_environment_schema_digest",
            )
            if len(self.plan_service_keys) != len(set(self.plan_service_keys)):
                raise ValueError("environment plan service keys must be unique")
            for service_key in self.plan_service_keys:
                _require_catalog_key(service_key, field="environment plan service key")
            if any(value.service_scope == "*" for value in self.set_values):
                raise ValueError("plan-bound environment writes require service scope")


def _environment_patch_service_keys(
    *,
    application_kind: str,
    materialized_service_keys: set[str],
    command: PatchEnvironment,
) -> set[str]:
    """Return the trusted service namespace for one environment mutation.

    A pending application has no catalog topology yet. Only an API command
    bound to a verified analysis may introduce service-scoped values there.
    Materialized applications continue to use their catalog topology and a
    plan-bound update must describe that exact topology.
    """

    if application_kind == "pending":
        allowed = set(command.plan_service_keys)
    else:
        allowed = set(materialized_service_keys)
        if (
            command.plan_analysis_id is not None
            and set(command.plan_service_keys) != allowed
        ):
            raise DeploymentEnvironmentScopeInvalid(
                "environment plan topology does not match application"
            )

    for value in command.set_values:
        if value.service_scope == "*":
            if command.plan_analysis_id is not None:
                raise ValueError("plan-bound environment writes require service scope")
            if len(materialized_service_keys) > 1:
                raise DeploymentEnvironmentScopeInvalid(
                    "wildcard environment writes are unsupported for Compose applications"
                )
        elif value.service_scope not in allowed:
            raise DeploymentEnvironmentScopeInvalid(
                "environment value references an unknown service"
            )
    for key in command.unset:
        # Wildcard deletion remains available solely to migrate legacy rows.
        if key.service_scope != "*" and key.service_scope not in allowed:
            raise DeploymentEnvironmentScopeInvalid(
                "environment value references an unknown service"
            )
    return allowed


@dataclass(frozen=True, slots=True)
class CreateRevision:
    scope: TenantScope
    principal: Principal
    application_id: str
    analysis_id: str
    source_revision_id: str
    deployment_plan_artifact_id: str
    deployment_plan_digest: str
    luma_manifest_digest: str
    environment_schema_digest: str
    environment_version: int
    normalized_compose_digest: str | None = None

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        require_opaque_id(self.analysis_id, prefix="ana")
        require_opaque_id(self.source_revision_id, prefix="src")
        require_opaque_id(self.deployment_plan_artifact_id, prefix="art")
        _require_digest(self.deployment_plan_digest, field="deployment_plan_digest")
        _require_digest(self.luma_manifest_digest, field="luma_manifest_digest")
        _require_digest(
            self.environment_schema_digest, field="environment_schema_digest"
        )
        if self.normalized_compose_digest is not None:
            _require_digest(
                self.normalized_compose_digest, field="normalized_compose_digest"
            )
        if isinstance(self.environment_version, bool) or self.environment_version < 0:
            raise ValueError("environment_version must be nonnegative")


@dataclass(frozen=True, slots=True)
class CreateDeployment:
    scope: TenantScope
    application_id: str
    revision_id: str
    operation_id: str
    previous_deployment_id: str | None = None

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        require_opaque_id(self.revision_id, prefix="rev")
        require_opaque_id(self.operation_id, prefix="op")
        if self.previous_deployment_id is not None:
            require_opaque_id(self.previous_deployment_id, prefix="dep")


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    id: str
    service_key: str
    role: str
    required: bool
    desired_state: str
    observed_state: str
    current_image_digest: str | None


@dataclass(frozen=True, slots=True)
class HttpRouteRecord:
    id: str
    service_id: str
    hostname: str
    is_primary: bool
    container_port: int
    status: str


@dataclass(frozen=True, slots=True)
class VolumeRecord:
    id: str
    volume_key: str
    requested_bytes: int
    storage_policy: str
    backup_policy: str
    delete_policy: str
    status: str


@dataclass(frozen=True, slots=True)
class EnvironmentVariableMetadata:
    service_scope: str
    name: str
    configured: bool
    key_version: int
    is_sensitive: bool
    required: bool
    source: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EnvironmentMetadata:
    version: int
    variables: tuple[EnvironmentVariableMetadata, ...]


@dataclass(frozen=True, slots=True)
class ApplicationSummary:
    id: str
    tenant_id: str
    name: str
    slug: str
    luma_name: str
    kind: str
    desired_state: str
    observed_state: str
    current_revision_id: str | None
    current_deployment_id: str | None
    environment_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    application: ApplicationSummary
    services: tuple[ServiceRecord, ...]
    routes: tuple[HttpRouteRecord, ...]
    volumes: tuple[VolumeRecord, ...]
    environment: EnvironmentMetadata


@dataclass(frozen=True, slots=True)
class IdempotentCatalogResult:
    """Historical safe response for one synchronous catalog mutation."""

    response_body: dict[str, Any]
    replayed: bool


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    id: str
    application_id: str
    revision_no: int
    analysis_id: str
    source_revision_id: str
    kind: str
    deployment_plan_artifact_id: str
    deployment_plan_digest: str
    normalized_compose_digest: str | None
    luma_manifest_digest: str | None
    environment_schema_digest: str
    environment_version: int
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    id: str
    application_id: str
    revision_id: str
    operation_id: str
    status: str
    luma_cluster_id: str | None
    luma_external_ref: str | None
    previous_deployment_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime


def _application_summary(row: Application) -> ApplicationSummary:
    return ApplicationSummary(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        slug=row.slug,
        luma_name=row.luma_name,
        kind=row.kind,
        desired_state=row.desired_state,
        observed_state=row.observed_state,
        current_revision_id=row.current_revision_id,
        current_deployment_id=row.current_deployment_id,
        environment_version=row.environment_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _public_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_application_body(summary: ApplicationSummary) -> dict[str, Any]:
    """Return the stable public application projection used for replay storage."""

    return {
        "application": {
            "id": summary.id,
            "name": summary.name,
            "slug": summary.slug,
            "kind": summary.kind,
            "desiredState": summary.desired_state,
            "observedState": summary.observed_state,
            "currentRevisionId": summary.current_revision_id,
            "currentDeploymentId": summary.current_deployment_id,
            "environmentVersion": summary.environment_version,
            "createdAt": _public_timestamp(summary.created_at),
            "updatedAt": _public_timestamp(summary.updated_at),
        }
    }


def _public_environment_body(metadata: EnvironmentMetadata) -> dict[str, Any]:
    """Return key metadata only; encryption material and values never enter it."""

    return {
        "environment": {
            "version": metadata.version,
            "variables": [
                {
                    "serviceScope": item.service_scope,
                    "name": item.name,
                    "configured": item.configured,
                    "sensitive": item.is_sensitive,
                    "required": item.required,
                    "source": item.source,
                    "updatedAt": _public_timestamp(item.updated_at),
                }
                for item in metadata.variables
            ],
        }
    }


def _service_record(row: ApplicationService) -> ServiceRecord:
    return ServiceRecord(
        id=row.id,
        service_key=row.service_key,
        role=row.role,
        required=row.required,
        desired_state=row.desired_state,
        observed_state=row.observed_state,
        current_image_digest=row.current_image_digest,
    )


def _route_record(row: ApplicationRoute) -> HttpRouteRecord:
    return HttpRouteRecord(
        id=row.id,
        service_id=row.service_id,
        hostname=row.hostname,
        is_primary=row.is_primary,
        container_port=row.container_port,
        status=row.status,
    )


def _volume_record(row: ApplicationVolume) -> VolumeRecord:
    return VolumeRecord(
        id=row.id,
        volume_key=row.volume_key,
        requested_bytes=row.requested_bytes,
        storage_policy=row.storage_policy,
        backup_policy=row.backup_policy,
        delete_policy=row.delete_policy,
        status=row.status,
    )


def _environment_metadata(
    version: int, rows: list[ApplicationEnvironmentVariable]
) -> EnvironmentMetadata:
    return EnvironmentMetadata(
        version=version,
        variables=tuple(
            EnvironmentVariableMetadata(
                service_scope=row.service_scope,
                name=row.name,
                configured=True,
                key_version=row.key_version,
                is_sensitive=row.is_sensitive,
                required=row.required,
                source=row.source,
                updated_at=row.updated_at,
            )
            for row in rows
        ),
    )


def _revision_record(row: AppRevision) -> RevisionRecord:
    return RevisionRecord(
        id=row.id,
        application_id=row.application_id,
        revision_no=row.revision_no,
        analysis_id=row.analysis_id,
        source_revision_id=row.source_revision_id,
        kind=row.kind,
        deployment_plan_artifact_id=row.deployment_plan_artifact_id,
        deployment_plan_digest=row.deployment_plan_digest,
        normalized_compose_digest=row.normalized_compose_digest,
        luma_manifest_digest=row.luma_manifest_digest,
        environment_schema_digest=row.environment_schema_digest,
        environment_version=row.environment_version,
        status=row.status,
        created_at=row.created_at,
    )


def _deployment_record(row: Deployment) -> DeploymentRecord:
    return DeploymentRecord(
        id=row.id,
        application_id=row.application_id,
        revision_id=row.revision_id,
        operation_id=row.operation_id,
        status=row.status,
        luma_cluster_id=row.luma_cluster_id,
        luma_external_ref=row.luma_external_ref,
        previous_deployment_id=row.previous_deployment_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
    )


class ApplicationCatalogStore:
    """PostgreSQL-only tenant application catalog and lifecycle fact store."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        hostname_factory: Callable[[], str] = new_managed_hostname,
    ) -> None:
        self._sessions = sessions
        self._hostname_factory = hostname_factory

    async def create_application_draft_idempotent(
        self,
        command: CreateApplicationDraft,
        *,
        principal: Principal,
        idempotency: IdempotencyInput,
    ) -> IdempotentCatalogResult:
        """Create a quota-counted draft and its replay record in one transaction."""

        if idempotency.method != "POST" or idempotency.route_template != "/v1/applications":
            raise ValueError("application create idempotency scope is invalid")
        lock_scope = (
            f"lae:application-create:{command.scope.tenant_id}:{principal.type}:"
            f"{principal.id}:{idempotency.key}"
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        application_catalog_lock_statement(),
                        {"application_catalog_lock_key": lock_scope},
                    )
                    now = await session.scalar(select(func.now()))
                    if now is None:
                        raise ApplicationConflict("database clock is unavailable")
                    existing = await self._idempotency_record(
                        session,
                        command.scope,
                        principal,
                        idempotency,
                        for_update=True,
                    )
                    if existing is not None and existing.expires_at > now:
                        return self._replay_idempotency(existing, idempotency)
                    if existing is not None:
                        await session.delete(existing)
                        await session.flush()

                    await session.execute(
                        tenant_application_quota_lock_statement(),
                        {
                            "tenant_application_quota_key": (
                                f"lae:application-quota:{command.scope.tenant_id}"
                            )
                        },
                    )
                    limits = await self._active_plan_limits(session, command.scope)
                    active_count = await session.scalar(
                        select(func.count())
                        .select_from(Application)
                        .where(
                            Application.tenant_id == command.scope.tenant_id,
                            Application.deleted_at.is_(None),
                        )
                    )
                    if int(active_count or 0) >= self._limit(limits, "applications"):
                        raise ApplicationQuotaExceeded("application quota exceeded")

                    application_id = new_id("app")
                    application = Application(
                        id=application_id,
                        tenant_id=command.scope.tenant_id,
                        name=command.name,
                        slug=command.slug,
                        luma_name=(
                            f"lae-{application_id.removeprefix('app_').lower()}"
                        ),
                        kind="pending",
                        environment_version=0,
                    )
                    session.add(application)
                    await session.flush()
                    response_body = _public_application_body(
                        _application_summary(application)
                    )
                    ensure_persistable_payload(response_body)
                    operation = Operation(
                        id=new_id("op"),
                        tenant_id=command.scope.tenant_id,
                        principal_type=principal.type,
                        principal_id=principal.id,
                        kind="application.create",
                        target_type="application",
                        target_id=application.id,
                        status="succeeded",
                        phase="application.create",
                        result=response_body,
                        finished_at=now,
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()
                    session.add(
                        IdempotencyRecord(
                            tenant_id=command.scope.tenant_id,
                            principal_type=principal.type,
                            principal_id=principal.id,
                            key=idempotency.key,
                            method=idempotency.method,
                            route_template=idempotency.route_template,
                            request_hash=idempotency.request_hash,
                            response_status=201,
                            response_body=response_body,
                            operation_id=operation.id,
                            expires_at=now + idempotency.retention,
                        )
                    )
                    await session.flush()
                    return IdempotentCatalogResult(response_body, replayed=False)
        except (ApplicationQuotaExceeded, IdempotencyKeyReused, SubscriptionUnavailable):
            raise
        except IntegrityError as error:
            raise ApplicationConflict(
                "application catalog value already exists"
            ) from error

    async def create_application_draft(
        self, command: CreateApplicationDraft
    ) -> ApplicationRecord:
        """Create the quota-counted shell that a source analysis binds to."""

        application_id = new_id("app")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        tenant_application_quota_lock_statement(),
                        {
                            "tenant_application_quota_key": (
                                f"lae:application-quota:{command.scope.tenant_id}"
                            )
                        },
                    )
                    limits = await self._active_plan_limits(session, command.scope)
                    active_count = await session.scalar(
                        select(func.count())
                        .select_from(Application)
                        .where(
                            Application.tenant_id == command.scope.tenant_id,
                            Application.deleted_at.is_(None),
                        )
                    )
                    if int(active_count or 0) >= self._limit(limits, "applications"):
                        raise ApplicationQuotaExceeded("application quota exceeded")
                    application = Application(
                        id=application_id,
                        tenant_id=command.scope.tenant_id,
                        name=command.name,
                        slug=command.slug,
                        luma_name=f"lae-{application_id.removeprefix('app_').lower()}",
                        kind="pending",
                        environment_version=0,
                    )
                    session.add(application)
                    await session.flush()
                    return ApplicationRecord(
                        application=_application_summary(application),
                        services=(),
                        routes=(),
                        volumes=(),
                        environment=EnvironmentMetadata(version=0, variables=()),
                    )
        except IntegrityError as error:
            raise ApplicationConflict(
                "application catalog value already exists"
            ) from error

    async def materialize_topology(
        self, command: MaterializeApplicationTopology
    ) -> ApplicationRecord:
        """Atomically turn one pending shell into its analyzed topology."""

        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        tenant_application_quota_lock_statement(),
                        {
                            "tenant_application_quota_key": (
                                f"lae:application-quota:{command.scope.tenant_id}"
                            )
                        },
                    )
                    await self._application_lock(
                        session, command.scope, command.application_id
                    )
                    application = await self._application(
                        session,
                        command.scope,
                        command.application_id,
                        for_update=True,
                    )
                    if application.kind != "pending":
                        raise ApplicationAlreadyMaterialized(
                            "application topology is already materialized"
                        )
                    analysis = await session.scalar(
                        select(Analysis.id).where(
                            Analysis.tenant_id == command.scope.tenant_id,
                            Analysis.application_id == command.application_id,
                            Analysis.id == command.analysis_id,
                            Analysis.status == "deployable",
                            Analysis.artifact_state == "stored",
                            Analysis.plan_stored.is_(True),
                        )
                    )
                    if analysis is None:
                        raise ResourceNotFound(
                            "deployable stored analysis for application not found"
                        )
                    limits = await self._active_plan_limits(session, command.scope)
                    await self._enforce_topology_limits(session, command, limits)

                    service_rows: list[ApplicationService] = []
                    services_by_key: dict[str, ApplicationService] = {}
                    for spec in command.services:
                        row = ApplicationService(
                            id=new_id("svc"),
                            tenant_id=command.scope.tenant_id,
                            application_id=command.application_id,
                            service_key=spec.service_key,
                            role=spec.role,
                            required=spec.required,
                        )
                        service_rows.append(row)
                        services_by_key[spec.service_key] = row
                        session.add(row)
                    await session.flush()

                    route_rows: list[ApplicationRoute] = []
                    for spec in command.routes:
                        row = ApplicationRoute(
                            id=new_id("rte"),
                            tenant_id=command.scope.tenant_id,
                            application_id=command.application_id,
                            service_id=services_by_key[spec.service_key].id,
                            kind="http",
                            hostname=self._next_hostname(),
                            is_primary=spec.is_primary,
                            container_port=spec.container_port,
                        )
                        route_rows.append(row)
                        session.add(row)
                    volume_rows = [
                        ApplicationVolume(
                            id=new_id("vol"),
                            tenant_id=command.scope.tenant_id,
                            application_id=command.application_id,
                            volume_key=spec.volume_key,
                            requested_bytes=spec.requested_bytes,
                            storage_policy="managed",
                            backup_policy=spec.backup_policy,
                            delete_policy=spec.delete_policy,
                        )
                        for spec in command.volumes
                    ]
                    session.add_all(volume_rows)
                    session.add_all(
                        [
                            self._environment_row(
                                command.scope.tenant_id,
                                command.application_id,
                                value,
                            )
                            for value in command.environment
                        ]
                    )
                    application.kind = command.kind
                    application.environment_version = 1 if command.environment else 0
                    application.updated_at = await session.scalar(select(func.now()))
                    await session.flush()
                    environment_rows = await self._environment_rows(
                        session, command.scope, command.application_id
                    )
                    return ApplicationRecord(
                        application=_application_summary(application),
                        services=tuple(_service_record(row) for row in service_rows),
                        routes=tuple(_route_record(row) for row in route_rows),
                        volumes=tuple(_volume_record(row) for row in volume_rows),
                        environment=_environment_metadata(
                            application.environment_version, environment_rows
                        ),
                    )
        except IntegrityError as error:
            raise ApplicationConflict(
                "application topology value already exists"
            ) from error

    async def create_application(self, command: CreateApplication) -> ApplicationRecord:
        application_id = new_id("app")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        tenant_application_quota_lock_statement(),
                        {
                            "tenant_application_quota_key": (
                                f"lae:application-quota:{command.scope.tenant_id}"
                            )
                        },
                    )
                    limits = await self._active_plan_limits(session, command.scope)
                    active_count = await session.scalar(
                        select(func.count())
                        .select_from(Application)
                        .where(
                            Application.tenant_id == command.scope.tenant_id,
                            Application.deleted_at.is_(None),
                        )
                    )
                    if int(active_count or 0) >= self._limit(limits, "applications"):
                        raise ApplicationQuotaExceeded("application quota exceeded")
                    await self._enforce_topology_limits(session, command, limits)

                    application = Application(
                        id=application_id,
                        tenant_id=command.scope.tenant_id,
                        name=command.name,
                        slug=command.slug,
                        luma_name=f"lae-{application_id.removeprefix('app_').lower()}",
                        kind=command.kind,
                        environment_version=1 if command.environment else 0,
                    )
                    session.add(application)
                    service_rows: list[ApplicationService] = []
                    services_by_key: dict[str, ApplicationService] = {}
                    for spec in command.services:
                        row = ApplicationService(
                            id=new_id("svc"),
                            tenant_id=command.scope.tenant_id,
                            application_id=application_id,
                            service_key=spec.service_key,
                            role=spec.role,
                            required=spec.required,
                        )
                        service_rows.append(row)
                        services_by_key[spec.service_key] = row
                        session.add(row)
                    # There are intentionally no ORM relationships in this
                    # persistence boundary. Flush parent/service facts before
                    # route rows so the composite service FK is satisfied.
                    await session.flush()

                    route_rows: list[ApplicationRoute] = []
                    for spec in command.routes:
                        row = ApplicationRoute(
                            id=new_id("rte"),
                            tenant_id=command.scope.tenant_id,
                            application_id=application_id,
                            service_id=services_by_key[spec.service_key].id,
                            kind="http",
                            hostname=self._next_hostname(),
                            is_primary=spec.is_primary,
                            container_port=spec.container_port,
                        )
                        route_rows.append(row)
                        session.add(row)

                    volume_rows = [
                        ApplicationVolume(
                            id=new_id("vol"),
                            tenant_id=command.scope.tenant_id,
                            application_id=application_id,
                            volume_key=spec.volume_key,
                            requested_bytes=spec.requested_bytes,
                            storage_policy="managed",
                            backup_policy=spec.backup_policy,
                            delete_policy=spec.delete_policy,
                        )
                        for spec in command.volumes
                    ]
                    session.add_all(volume_rows)
                    session.add_all(
                        [
                            self._environment_row(
                                command.scope.tenant_id, application_id, value
                            )
                            for value in command.environment
                        ]
                    )
                    await session.flush()
                    environment_rows = await self._environment_rows(
                        session, command.scope, application_id
                    )
                    record = ApplicationRecord(
                        application=_application_summary(application),
                        services=tuple(_service_record(row) for row in service_rows),
                        routes=tuple(_route_record(row) for row in route_rows),
                        volumes=tuple(_volume_record(row) for row in volume_rows),
                        environment=_environment_metadata(
                            application.environment_version, environment_rows
                        ),
                    )
            return record
        except IntegrityError as error:
            raise ApplicationConflict(
                "application catalog value already exists"
            ) from error

    async def list_applications(
        self, scope: TenantScope, *, limit: int = 100
    ) -> tuple[ApplicationSummary, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("application list limit must be 1-100")
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Application)
                .where(
                    Application.tenant_id == scope.tenant_id,
                    Application.deleted_at.is_(None),
                )
                .order_by(Application.created_at.desc(), Application.id.desc())
                .limit(limit)
            )
            return tuple(_application_summary(row) for row in rows)

    async def get_application(
        self, scope: TenantScope, application_id: str
    ) -> ApplicationRecord:
        require_opaque_id(application_id, prefix="app")
        async with self._sessions() as session:
            return await self._load_application_record(session, scope, application_id)

    async def get_environment(
        self, scope: TenantScope, application_id: str
    ) -> EnvironmentMetadata:
        require_opaque_id(application_id, prefix="app")
        async with self._sessions() as session:
            application = await self._application(session, scope, application_id)
            rows = await self._environment_rows(session, scope, application_id)
            return _environment_metadata(application.environment_version, rows)

    async def patch_environment_idempotent(
        self,
        command: PatchEnvironment,
        *,
        principal: Principal,
        idempotency: IdempotencyInput,
    ) -> IdempotentCatalogResult:
        """CAS-patch encrypted values and persist only a safe historical response."""

        if idempotency.method != "PATCH" or idempotency.route_template not in {
            APPLICATION_ENVIRONMENT_PATCH_ROUTE,
            PLAN_ENVIRONMENT_PATCH_ROUTE,
        }:
            raise ValueError("environment patch idempotency scope is invalid")
        lock_scope = (
            f"lae:environment-patch:{command.scope.tenant_id}:{principal.type}:"
            f"{principal.id}:{idempotency.route_template}:{idempotency.key}"
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await session.execute(
                        application_catalog_lock_statement(),
                        {"application_catalog_lock_key": lock_scope},
                    )
                    now = await session.scalar(select(func.now()))
                    if now is None:
                        raise ApplicationConflict("database clock is unavailable")
                    existing = await self._idempotency_record(
                        session,
                        command.scope,
                        principal,
                        idempotency,
                        for_update=True,
                    )
                    if existing is not None and existing.expires_at > now:
                        return self._replay_idempotency(existing, idempotency)
                    if existing is not None:
                        await session.delete(existing)
                        await session.flush()

                    metadata = await self._patch_environment_in_session(
                        session, command
                    )
                    response_body = _public_environment_body(metadata)
                    ensure_persistable_payload(response_body)
                    operation = Operation(
                        id=new_id("op"),
                        tenant_id=command.scope.tenant_id,
                        principal_type=principal.type,
                        principal_id=principal.id,
                        kind="application.environment-update",
                        target_type="application",
                        target_id=command.application_id,
                        status="succeeded",
                        phase="application.environment-update",
                        result=response_body,
                        finished_at=now,
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()
                    session.add(
                        IdempotencyRecord(
                            tenant_id=command.scope.tenant_id,
                            principal_type=principal.type,
                            principal_id=principal.id,
                            key=idempotency.key,
                            method=idempotency.method,
                            route_template=idempotency.route_template,
                            request_hash=idempotency.request_hash,
                            response_status=200,
                            response_body=response_body,
                            operation_id=operation.id,
                            expires_at=now + idempotency.retention,
                        )
                    )
                    await session.flush()
                    return IdempotentCatalogResult(response_body, replayed=False)
        except (
            EnvironmentVersionConflict,
            IdempotencyKeyReused,
            ResourceNotFound,
        ):
            raise
        except IntegrityError as error:
            raise ApplicationConflict("environment patch conflicts") from error

    async def patch_environment(self, command: PatchEnvironment) -> EnvironmentMetadata:
        async with self._sessions() as session:
            async with session.begin():
                return await self._patch_environment_in_session(session, command)

    async def set_desired_state(
        self, scope: TenantScope, application_id: str, desired_state: str
    ) -> ApplicationSummary:
        require_opaque_id(application_id, prefix="app")
        if desired_state not in _DESIRED_STATES:
            raise ValueError("application desired state is unsupported")
        async with self._sessions() as session:
            async with session.begin():
                application = await self._application(
                    session, scope, application_id, for_update=True
                )
                application.desired_state = desired_state
                application.updated_at = await session.scalar(select(func.now()))
                await session.flush()
                return _application_summary(application)

    async def set_observed_state(
        self, scope: TenantScope, application_id: str, observed_state: str
    ) -> ApplicationSummary:
        require_opaque_id(application_id, prefix="app")
        if observed_state not in _OBSERVED_STATES:
            raise ValueError("application observed state is unsupported")
        async with self._sessions() as session:
            async with session.begin():
                application = await self._application(
                    session, scope, application_id, for_update=True
                )
                application.observed_state = observed_state
                application.updated_at = await session.scalar(select(func.now()))
                await session.flush()
                return _application_summary(application)

    async def create_revision(self, command: CreateRevision) -> RevisionRecord:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._application_lock(
                        session, command.scope, command.application_id
                    )
                    application = await self._application(
                        session,
                        command.scope,
                        command.application_id,
                        for_update=True,
                    )
                    if application.environment_version != command.environment_version:
                        raise EnvironmentVersionConflict(
                            expected=command.environment_version,
                            actual=application.environment_version,
                        )
                    analysis = await session.scalar(
                        select(Analysis).where(
                            Analysis.tenant_id == command.scope.tenant_id,
                            Analysis.id == command.analysis_id,
                            Analysis.application_id == command.application_id,
                            Analysis.source_revision_id == command.source_revision_id,
                            Analysis.status == "deployable",
                            Analysis.artifact_state == "stored",
                            Analysis.plan_stored.is_(True),
                        )
                    )
                    if analysis is None:
                        raise ResourceNotFound("deployable stored analysis not found")
                    source = await session.scalar(
                        select(SourceRevision.id).where(
                            SourceRevision.tenant_id == command.scope.tenant_id,
                            SourceRevision.id == command.source_revision_id,
                            SourceRevision.application_id == command.application_id,
                            SourceRevision.deleted_at.is_(None),
                        )
                    )
                    if source is None:
                        raise ResourceNotFound("source revision not found")
                    artifact = await session.scalar(
                        select(Artifact).where(
                            Artifact.tenant_id == command.scope.tenant_id,
                            Artifact.id == command.deployment_plan_artifact_id,
                            Artifact.kind == "deployment-plan",
                            Artifact.digest == command.deployment_plan_digest,
                            Artifact.upload_status == "verified",
                        )
                    )
                    if artifact is None:
                        raise ResourceNotFound(
                            "verified deployment plan artifact not found"
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
                        )
                        + 1
                    )
                    revision = AppRevision(
                        id=new_id("rev"),
                        tenant_id=command.scope.tenant_id,
                        application_id=command.application_id,
                        revision_no=revision_no,
                        analysis_id=command.analysis_id,
                        source_revision_id=command.source_revision_id,
                        kind=application.kind,
                        deployment_plan_artifact_id=command.deployment_plan_artifact_id,
                        deployment_plan_digest=command.deployment_plan_digest,
                        normalized_compose_digest=command.normalized_compose_digest,
                        luma_manifest_digest=command.luma_manifest_digest,
                        environment_schema_digest=command.environment_schema_digest,
                        environment_version=command.environment_version,
                        status="candidate",
                        created_by_type=command.principal.type,
                        created_by_id=command.principal.id,
                    )
                    session.add(revision)
                    await session.flush()
                    return _revision_record(revision)
        except IntegrityError as error:
            raise ApplicationConflict("application revision already exists") from error

    async def create_deployment(self, command: CreateDeployment) -> DeploymentRecord:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._application_lock(
                        session, command.scope, command.application_id
                    )
                    await self._application(
                        session, command.scope, command.application_id, for_update=True
                    )
                    revision = await session.scalar(
                        select(AppRevision).where(
                            AppRevision.tenant_id == command.scope.tenant_id,
                            AppRevision.application_id == command.application_id,
                            AppRevision.id == command.revision_id,
                            AppRevision.status.in_(("candidate", "active")),
                        )
                    )
                    if revision is None:
                        raise ResourceNotFound("application revision not found")
                    operation = await session.scalar(
                        select(Operation).where(
                            Operation.tenant_id == command.scope.tenant_id,
                            Operation.id == command.operation_id,
                            Operation.kind == "deployment.create",
                            Operation.target_type == "application",
                            Operation.target_id == command.application_id,
                            Operation.status.in_(("queued", "running")),
                        )
                    )
                    if operation is None:
                        raise ResourceNotFound("active deployment operation not found")
                    if command.previous_deployment_id is not None:
                        previous = await session.scalar(
                            select(Deployment.id).where(
                                Deployment.tenant_id == command.scope.tenant_id,
                                Deployment.application_id == command.application_id,
                                Deployment.id == command.previous_deployment_id,
                            )
                        )
                        if previous is None:
                            raise ResourceNotFound("previous deployment not found")
                    deployment = Deployment(
                        id=new_id("dep"),
                        tenant_id=command.scope.tenant_id,
                        application_id=command.application_id,
                        revision_id=command.revision_id,
                        operation_id=command.operation_id,
                        status="queued",
                        previous_deployment_id=command.previous_deployment_id,
                    )
                    session.add(deployment)
                    await session.flush()
                    return _deployment_record(deployment)
        except IntegrityError as error:
            raise ApplicationConflict("deployment fact already exists") from error

    async def get_revision(
        self, scope: TenantScope, application_id: str, revision_id: str
    ) -> RevisionRecord:
        require_opaque_id(application_id, prefix="app")
        require_opaque_id(revision_id, prefix="rev")
        async with self._sessions() as session:
            row = await session.scalar(
                select(AppRevision).where(
                    AppRevision.tenant_id == scope.tenant_id,
                    AppRevision.application_id == application_id,
                    AppRevision.id == revision_id,
                )
            )
            if row is None:
                raise ResourceNotFound("application revision not found")
            return _revision_record(row)

    async def get_deployment(
        self, scope: TenantScope, application_id: str, deployment_id: str
    ) -> DeploymentRecord:
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
            return _deployment_record(row)

    async def _patch_environment_in_session(
        self, session: AsyncSession, command: PatchEnvironment
    ) -> EnvironmentMetadata:
        application = await self._application(
            session,
            command.scope,
            command.application_id,
            for_update=True,
        )
        if application.environment_version != command.expected_version:
            raise EnvironmentVersionConflict(
                expected=command.expected_version,
                actual=application.environment_version,
            )
        service_keys = set(
            await session.scalars(
                select(ApplicationService.service_key).where(
                    ApplicationService.tenant_id == command.scope.tenant_id,
                    ApplicationService.application_id == command.application_id,
                )
            )
        )
        if command.plan_analysis_id is not None:
            analysis_id = await session.scalar(
                select(Analysis.id).where(
                    Analysis.tenant_id == command.scope.tenant_id,
                    Analysis.application_id == command.application_id,
                    Analysis.id == command.plan_analysis_id,
                    Analysis.status.in_(("deployable", "needs_configuration")),
                    Analysis.plan_stored.is_(True),
                )
            )
            if analysis_id is None:
                raise ResourceNotFound("deployment analysis not found")
        _environment_patch_service_keys(
            application_kind=application.kind,
            materialized_service_keys=service_keys,
            command=command,
        )

        rows = {
            (row.service_scope, row.name): row
            for row in await self._environment_rows(
                session, command.scope, command.application_id
            )
        }
        for value in command.set_values:
            row = rows.get(value.key)
            if row is None:
                row = self._environment_row(
                    command.scope.tenant_id, command.application_id, value
                )
                session.add(row)
                rows[value.key] = row
            else:
                row.value_ciphertext = value.envelope_ciphertext
                row.value_checksum = value.checksum
                row.key_version = value.key_version
                row.is_sensitive = value.is_sensitive
                row.required = value.required
                row.source = value.source
                row.updated_at = await session.scalar(select(func.now()))
        for key in command.unset:
            await session.execute(
                delete(ApplicationEnvironmentVariable).where(
                    ApplicationEnvironmentVariable.tenant_id
                    == command.scope.tenant_id,
                    ApplicationEnvironmentVariable.application_id
                    == command.application_id,
                    ApplicationEnvironmentVariable.service_scope
                    == key.service_scope,
                    ApplicationEnvironmentVariable.name == key.name,
                )
            )
        application.environment_version += 1
        application.updated_at = await session.scalar(select(func.now()))
        await session.flush()
        environment_rows = await self._environment_rows(
            session, command.scope, command.application_id
        )
        return _environment_metadata(
            application.environment_version, environment_rows
        )

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
    def _replay_idempotency(
        existing: IdempotencyRecord, idempotency: IdempotencyInput
    ) -> IdempotentCatalogResult:
        if not hmac.compare_digest(existing.request_hash, idempotency.request_hash):
            raise IdempotencyKeyReused(
                "idempotency key was used for another request"
            )
        body = existing.response_body
        if not isinstance(body, dict):
            raise ApplicationConflict("idempotency response is unavailable")
        ensure_persistable_payload(body)
        return IdempotentCatalogResult(dict(body), replayed=True)

    def _next_hostname(self) -> str:
        candidate = self._hostname_factory()
        if not isinstance(candidate, str):
            raise RuntimeError("managed hostname generator returned a non-string value")
        if candidate.endswith(".itool.tech"):
            label = candidate.removesuffix(".itool.tech")
            return _managed_hostname(label)
        return _managed_hostname(candidate)

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

    async def _enforce_topology_limits(
        self,
        session: AsyncSession,
        command: CreateApplication | MaterializeApplicationTopology,
        limits: dict[str, object],
    ) -> None:
        if len(command.services) > self._limit(limits, "servicesPerApp"):
            raise ApplicationQuotaExceeded("services-per-application quota exceeded")
        if len(command.routes) > self._limit(limits, "publicHttpRoutesPerApp"):
            raise ApplicationQuotaExceeded("public HTTP route quota exceeded")
        current_volume_bytes = await session.scalar(
            select(func.coalesce(func.sum(ApplicationVolume.requested_bytes), 0)).where(
                ApplicationVolume.tenant_id == command.scope.tenant_id
            )
        )
        requested_volume_bytes = sum(item.requested_bytes for item in command.volumes)
        if int(current_volume_bytes or 0) + requested_volume_bytes > self._limit(
            limits, "persistentVolumeBytes"
        ):
            raise ApplicationQuotaExceeded("persistent volume byte quota exceeded")

    @staticmethod
    def _environment_row(
        tenant_id: str,
        application_id: str,
        value: EncryptedEnvironmentValue,
    ) -> ApplicationEnvironmentVariable:
        return ApplicationEnvironmentVariable(
            tenant_id=tenant_id,
            application_id=application_id,
            service_scope=value.service_scope,
            name=value.name,
            value_ciphertext=value.envelope_ciphertext,
            value_checksum=value.checksum,
            key_version=value.key_version,
            is_sensitive=value.is_sensitive,
            required=value.required,
            source=value.source,
        )

    @staticmethod
    async def _application(
        session: AsyncSession,
        scope: TenantScope,
        application_id: str,
        *,
        for_update: bool = False,
    ) -> Application:
        statement = select(Application).where(
            Application.tenant_id == scope.tenant_id,
            Application.id == application_id,
            Application.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        application = await session.scalar(statement)
        if application is None:
            raise ResourceNotFound("application not found")
        return application

    @staticmethod
    async def _environment_rows(
        session: AsyncSession, scope: TenantScope, application_id: str
    ) -> list[ApplicationEnvironmentVariable]:
        rows = await session.scalars(
            select(ApplicationEnvironmentVariable)
            .where(
                ApplicationEnvironmentVariable.tenant_id == scope.tenant_id,
                ApplicationEnvironmentVariable.application_id == application_id,
            )
            .order_by(
                ApplicationEnvironmentVariable.service_scope,
                ApplicationEnvironmentVariable.name,
            )
        )
        return list(rows)

    @staticmethod
    async def _load_application_record(
        session: AsyncSession, scope: TenantScope, application_id: str
    ) -> ApplicationRecord:
        application = await ApplicationCatalogStore._application(
            session, scope, application_id
        )
        services = list(
            await session.scalars(
                select(ApplicationService)
                .where(
                    ApplicationService.tenant_id == scope.tenant_id,
                    ApplicationService.application_id == application_id,
                )
                .order_by(ApplicationService.service_key)
            )
        )
        routes = list(
            await session.scalars(
                select(ApplicationRoute)
                .where(
                    ApplicationRoute.tenant_id == scope.tenant_id,
                    ApplicationRoute.application_id == application_id,
                )
                .order_by(ApplicationRoute.is_primary.desc(), ApplicationRoute.hostname)
            )
        )
        volumes = list(
            await session.scalars(
                select(ApplicationVolume)
                .where(
                    ApplicationVolume.tenant_id == scope.tenant_id,
                    ApplicationVolume.application_id == application_id,
                )
                .order_by(ApplicationVolume.volume_key)
            )
        )
        environment_rows = await ApplicationCatalogStore._environment_rows(
            session, scope, application_id
        )
        return ApplicationRecord(
            application=_application_summary(application),
            services=tuple(_service_record(row) for row in services),
            routes=tuple(_route_record(row) for row in routes),
            volumes=tuple(_volume_record(row) for row in volumes),
            environment=_environment_metadata(
                application.environment_version, environment_rows
            ),
        )

    @staticmethod
    async def _application_lock(
        session: AsyncSession, scope: TenantScope, application_id: str
    ) -> None:
        await session.execute(
            application_catalog_lock_statement(),
            {
                "application_catalog_lock_key": (
                    f"lae:application-catalog:{scope.tenant_id}:{application_id}"
                )
            },
        )
