from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from lae_luma_adapter import (
    AdapterErrorCode,
    BuilderLimits,
    BuildPlanRequest,
    BuilderTask,
    BuilderTaskEvent,
    LumaAdapterError,
    LumaBuilderAdapter,
    LumaCallContext,
    LumaRuntimeAdapter,
    RuntimeCallContext,
    RuntimeDeployment,
    RuntimeImageBinding,
    RuntimeManifest,
    RuntimeRouteSpec,
    RuntimeSecretRef,
    RuntimeServiceHealthcheck,
    RuntimeServiceResources,
    RuntimeServiceSpec,
    RuntimeVolumeBinding,
    RuntimeVolumeMount,
    RuntimeVolumeSpec,
)
from lae_store import (
    EventInput,
    LeaseLost,
    OperationRecord,
    OperationStatus,
    OperationStore,
    TenantScope,
)


_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$")
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOSTNAME = re.compile(r"^[0-9a-f]{32}\.itool\.tech$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_BUILD_STATUSES = {
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "timed_out",
    "canceled",
}


class DeploymentOrchestrationError(RuntimeError):
    code = "LAE_DEPLOYMENT_ORCHESTRATION_FAILED"
    public_message = "The deployment could not be orchestrated safely."
    retryable = False


class DeploymentContextInvalid(DeploymentOrchestrationError):
    code = "LAE_DEPLOYMENT_CONTEXT_INVALID"
    public_message = "The deployment is not bound to immutable application facts."


class DeploymentCheckpointConflict(DeploymentOrchestrationError):
    code = "LAE_DEPLOYMENT_CHECKPOINT_CONFLICT"
    public_message = "The deployment checkpoint changed concurrently."


class DeploymentBuildFailed(DeploymentOrchestrationError):
    code = "LAE_BUILD_FAILED"
    public_message = "The application build did not complete successfully."


class DeploymentBuildOutputInvalid(DeploymentOrchestrationError):
    code = "LAE_BUILD_OUTPUT_INVALID"
    public_message = "The application build returned invalid immutable outputs."


class DeploymentRuntimeFailed(DeploymentOrchestrationError):
    code = "LAE_RUNTIME_DEPLOY_FAILED"
    public_message = "Luma could not deploy the application revision."


class DeploymentHealthFailed(DeploymentOrchestrationError):
    code = "LAE_DEPLOYMENT_HEALTH_FAILED"
    public_message = "The new application revision did not become healthy."


class DeploymentRouteFailed(DeploymentOrchestrationError):
    code = "LAE_ROUTE_HEALTH_FAILED"
    public_message = "A required public HTTP route did not become healthy."


class DeploymentTimedOut(DeploymentOrchestrationError):
    code = "LAE_DEPLOYMENT_TIMED_OUT"
    public_message = "The deployment timed out before the new revision became healthy."


class RuntimeSecretsUnavailable(DeploymentOrchestrationError):
    code = "LAE_RUNTIME_SECRETS_UNAVAILABLE"
    public_message = "Runtime environment secrets could not be injected safely."


@dataclass(frozen=True, slots=True)
class DeploymentService:
    key: str
    role: str
    build_key: str
    command: str | None
    dependencies: tuple[str, ...]
    cpu: str
    memory_mib: int
    environment_names: tuple[str, ...]
    port: int | None = None
    health_path: str | None = None
    health_interval_seconds: int | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str)
            or _KEY.fullmatch(self.key) is None
            or not isinstance(self.build_key, str)
            or _KEY.fullmatch(self.build_key) is None
            or self.role not in {"http", "internal", "worker", "datastore"}
            or (self.command is not None and (
                not isinstance(self.command, str)
                or len(self.command) > 4096
                or "\x00" in self.command
            ))
            or not isinstance(self.dependencies, tuple)
            or len(self.dependencies) != len(set(self.dependencies))
            or self.key in self.dependencies
            or any(_KEY.fullmatch(value) is None for value in self.dependencies)
            or not isinstance(self.cpu, str)
            or re.fullmatch(r"^[0-9]+(?:\.[0-9]+)?$", self.cpu) is None
            or not 0 < float(self.cpu) <= 32
            or isinstance(self.memory_mib, bool)
            or not isinstance(self.memory_mib, int)
            or not 64 <= self.memory_mib <= 1_048_576
            or not isinstance(self.environment_names, tuple)
            or len(self.environment_names) != len(set(self.environment_names))
            or any(_ENV.fullmatch(value) is None for value in self.environment_names)
            or not isinstance(self.required, bool)
        ):
            raise DeploymentContextInvalid()
        if self.port is not None and (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise DeploymentContextInvalid()
        health_values = (self.health_path, self.health_interval_seconds)
        if any(value is not None for value in health_values):
            if (
                self.role != "http"
                or self.port is None
                or not isinstance(self.health_path, str)
                or not self.health_path.startswith("/")
                or "?" in self.health_path
                or "#" in self.health_path
                or len(self.health_path) > 256
                or any(character in self.health_path for character in "\x00\r\n")
                or isinstance(self.health_interval_seconds, bool)
                or not isinstance(self.health_interval_seconds, int)
                or not 1 <= self.health_interval_seconds <= 300
            ):
                raise DeploymentContextInvalid()


@dataclass(frozen=True, slots=True)
class DeploymentRoute:
    service_key: str
    hostname: str
    container_port: int
    health_path: str = "/"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.service_key, str)
            or _KEY.fullmatch(self.service_key) is None
            or not isinstance(self.hostname, str)
            or _HOSTNAME.fullmatch(self.hostname) is None
            or isinstance(self.container_port, bool)
            or not isinstance(self.container_port, int)
            or not 1 <= self.container_port <= 65535
            or not isinstance(self.health_path, str)
            or not self.health_path.startswith("/")
            or "?" in self.health_path
            or "#" in self.health_path
            or any(character in self.health_path for character in "\x00\r\n")
        ):
            raise DeploymentContextInvalid()


@dataclass(frozen=True, slots=True)
class DeploymentVolume:
    key: str
    requested_bytes: int
    service_keys: tuple[str, ...]
    mount_path: str
    access_mode: str
    existing_ref: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str)
            or _KEY.fullmatch(self.key) is None
            or isinstance(self.requested_bytes, bool)
            or not isinstance(self.requested_bytes, int)
            or self.requested_bytes <= 0
            or not isinstance(self.service_keys, tuple)
            or not self.service_keys
            or len(self.service_keys) != len(set(self.service_keys))
            or any(_KEY.fullmatch(value) is None for value in self.service_keys)
            or not isinstance(self.mount_path, str)
            or not self.mount_path.startswith("/")
            or self.mount_path == "/"
            or len(self.mount_path) > 1024
            or "//" in self.mount_path
            or any(part in {".", ".."} for part in self.mount_path.split("/"))
            or any(character in self.mount_path for character in "\x00\r\n")
            or self.access_mode not in {"ReadWriteOnce", "ReadWriteMany"}
        ):
            raise DeploymentContextInvalid()
        if self.existing_ref is not None and _REFERENCE.fullmatch(
            self.existing_ref
        ) is None:
            raise DeploymentContextInvalid()


@dataclass(frozen=True, slots=True)
class DeploymentEnvironmentRequirement:
    service_key: str
    name: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.service_key, str)
            or _KEY.fullmatch(self.service_key) is None
            or not isinstance(self.name, str)
            or _ENV.fullmatch(self.name) is None
        ):
            raise DeploymentContextInvalid()


@dataclass(frozen=True, slots=True)
class DeploymentContext:
    tenant_ref: str
    application_ref: str
    operation_ref: str
    deployment_ref: str
    revision_ref: str
    source_revision_ref: str
    analysis_ref: str
    luma_name: str
    kind: str
    region: str
    environment_version: int
    source_snapshot_id: str
    source_snapshot_digest: str
    build_plan_digest: str
    signed_build_plan: Mapping[str, Any] = field(repr=False)
    build_credential_lease_id: str = field(repr=False)
    services: tuple[DeploymentService, ...] = ()
    routes: tuple[DeploymentRoute, ...] = ()
    volumes: tuple[DeploymentVolume, ...] = ()
    environment: tuple[DeploymentEnvironmentRequirement, ...] = ()
    normalized_compose_digest: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.tenant_ref,
            self.application_ref,
            self.operation_ref,
            self.deployment_ref,
            self.revision_ref,
            self.source_revision_ref,
            self.analysis_ref,
            self.luma_name,
            self.source_snapshot_id,
            self.build_credential_lease_id,
        ):
            if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
                raise DeploymentContextInvalid()
        if self.kind not in {"service", "compose"} or self.region not in {"cn", "global"}:
            raise DeploymentContextInvalid()
        if (
            isinstance(self.environment_version, bool)
            or not isinstance(self.environment_version, int)
            or self.environment_version < 0
            or _DIGEST.fullmatch(self.source_snapshot_digest) is None
            or _DIGEST.fullmatch(self.build_plan_digest) is None
            or not isinstance(self.signed_build_plan, Mapping)
        ):
            raise DeploymentContextInvalid()
        if not self.services or (self.kind == "service" and len(self.services) != 1):
            raise DeploymentContextInvalid()
        if self.kind == "service" and self.normalized_compose_digest is not None:
            raise DeploymentContextInvalid()
        if self.kind == "compose" and (
            self.normalized_compose_digest is None
            or _DIGEST.fullmatch(self.normalized_compose_digest) is None
        ):
            raise DeploymentContextInvalid()
        service_keys = [service.key for service in self.services]
        build_keys = [service.build_key for service in self.services]
        if len(service_keys) != len(set(service_keys)) or len(build_keys) != len(set(build_keys)):
            raise DeploymentContextInvalid()
        known_services = set(service_keys)
        if any(
            dependency not in known_services
            for service in self.services
            for dependency in service.dependencies
        ):
            raise DeploymentContextInvalid()
        _require_acyclic_dependencies(self.services)
        roles = {service.key: service.role for service in self.services}
        if len({route.hostname for route in self.routes}) != len(self.routes) or any(
            roles.get(route.service_key) != "http" for route in self.routes
        ):
            raise DeploymentContextInvalid()
        if len({volume.key for volume in self.volumes}) != len(self.volumes):
            raise DeploymentContextInvalid()
        if any(
            service_key not in known_services
            for volume in self.volumes
            for service_key in volume.service_keys
        ):
            raise DeploymentContextInvalid()
        if len({(item.service_key, item.name) for item in self.environment}) != len(
            self.environment
        ) or any(item.service_key not in roles for item in self.environment):
            raise DeploymentContextInvalid()

    @property
    def build_request_digest(self) -> bytes:
        payload = {
            "sourceSnapshotId": self.source_snapshot_id,
            "sourceSnapshotDigest": self.source_snapshot_digest,
            "buildPlanDigest": self.build_plan_digest,
            "signedBuildPlan": self.signed_build_plan,
        }
        try:
            raw = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise DeploymentContextInvalid() from exc
        return hashlib.sha256(raw).digest()


class DeploymentContextLoader(Protocol):
    async def load(self, operation: OperationRecord) -> DeploymentContext: ...


@dataclass(frozen=True, slots=True)
class VerifiedBuildOutput:
    build_key: str
    service_key: str
    image_digest: str
    sbom_digest: str
    provenance_digest: str
    scan_digest: str

    def __post_init__(self) -> None:
        if (
            _KEY.fullmatch(self.build_key) is None
            or _KEY.fullmatch(self.service_key) is None
            or any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    self.image_digest,
                    self.sbom_digest,
                    self.provenance_digest,
                    self.scan_digest,
                )
            )
        ):
            raise DeploymentBuildOutputInvalid()

    @classmethod
    def from_task(
        cls, task: BuilderTask, context: DeploymentContext
    ) -> tuple[VerifiedBuildOutput, ...]:
        result = task.result
        if task.status != "succeeded" or not isinstance(result, dict):
            raise DeploymentBuildOutputInvalid()
        if result.get("sourceSnapshotDigest") != context.source_snapshot_digest:
            raise DeploymentBuildOutputInvalid()
        maps: list[dict[str, str]] = []
        for key in (
            "imageDigests",
            "sbomDigests",
            "provenanceDigests",
            "scanDigests",
        ):
            value = result.get(key)
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in value.items()
            ):
                raise DeploymentBuildOutputInvalid()
            maps.append(value)
        expected = {service.build_key for service in context.services}
        if any(set(value) != expected for value in maps):
            raise DeploymentBuildOutputInvalid()
        by_build = {service.build_key: service.key for service in context.services}
        return tuple(
            cls(
                build_key=build_key,
                service_key=by_build[build_key],
                image_digest=maps[0][build_key],
                sbom_digest=maps[1][build_key],
                provenance_digest=maps[2][build_key],
                scan_digest=maps[3][build_key],
            )
            for build_key in sorted(expected)
        )


@dataclass(frozen=True, slots=True)
class DeploymentOrchestrationState:
    operation_id: str
    version: int = 0
    tenant_ref: str | None = None
    application_ref: str | None = None
    deployment_ref: str | None = None
    revision_ref: str | None = None
    phase: str = "prepare"
    builder_task_id: str | None = None
    builder_cursor: int = 0
    builder_status: str | None = None
    build_cancel_forwarded: bool = False
    build_outputs: tuple[VerifiedBuildOutput, ...] = ()
    manifest_digest: str | None = None
    normalized_compose_digest: str | None = None
    volume_bindings: tuple[RuntimeVolumeBinding, ...] = ()
    luma_deployment_ref: str | None = None
    runtime_status: str | None = None
    runtime_cancel_forwarded: bool = False
    terminal_error_code: str | None = None
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or _REFERENCE.fullmatch(self.operation_id) is None
            or self.version < 0
            or self.builder_cursor < 0
            or self.phase
            not in {
                "prepare",
                "building",
                "rendering",
                "volumes",
                "deploying",
                "verifying",
                "activating",
                "complete",
                "failed",
                "canceled",
            }
        ):
            raise DeploymentCheckpointConflict()
        for value in (
            self.tenant_ref,
            self.application_ref,
            self.deployment_ref,
            self.revision_ref,
            self.builder_task_id,
            self.luma_deployment_ref,
        ):
            if value is not None and _REFERENCE.fullmatch(value) is None:
                raise DeploymentCheckpointConflict()
        if self.builder_status is not None and self.builder_status not in _BUILD_STATUSES:
            raise DeploymentCheckpointConflict()
        if self.manifest_digest is not None and _DIGEST.fullmatch(
            self.manifest_digest
        ) is None:
            raise DeploymentCheckpointConflict()
        if self.normalized_compose_digest is not None and _DIGEST.fullmatch(
            self.normalized_compose_digest
        ) is None:
            raise DeploymentCheckpointConflict()
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            raise DeploymentCheckpointConflict()

    def bind(self, context: DeploymentContext) -> DeploymentOrchestrationState:
        existing = (
            self.tenant_ref,
            self.application_ref,
            self.deployment_ref,
            self.revision_ref,
        )
        expected = (
            context.tenant_ref,
            context.application_ref,
            context.deployment_ref,
            context.revision_ref,
        )
        if any(value is not None for value in existing) and existing != expected:
            raise DeploymentContextInvalid()
        if self.normalized_compose_digest != context.normalized_compose_digest:
            raise DeploymentContextInvalid()
        return replace(
            self,
            tenant_ref=expected[0],
            application_ref=expected[1],
            deployment_ref=expected[2],
            revision_ref=expected[3],
        )


class DeploymentStateStore(Protocol):
    async def initialize(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        *,
        timeout: timedelta,
    ) -> DeploymentOrchestrationState: ...

    async def load(
        self, operation_id: str
    ) -> DeploymentOrchestrationState | None: ...

    async def save(
        self,
        state: DeploymentOrchestrationState,
        *,
        expected_version: int,
    ) -> DeploymentOrchestrationState: ...

    async def persist_build_outputs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        outputs: tuple[VerifiedBuildOutput, ...],
    ) -> None: ...

    async def bind_volumes(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        bindings: tuple[RuntimeVolumeBinding, ...],
    ) -> None: ...

    async def activate(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        runtime: RuntimeDeployment,
        *,
        worker_id: str,
    ) -> OperationRecord: ...

    async def finalize_terminal(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        *,
        status: str,
        error_code: str | None,
    ) -> None: ...


class InMemoryDeploymentStateStore:
    """Crash/retry deterministic state store used only by worker tests."""

    def __init__(self, operations: OperationStore) -> None:
        self._operations = operations
        self._states: dict[str, DeploymentOrchestrationState] = {}
        self._lock = asyncio.Lock()
        self.build_outputs: dict[str, tuple[VerifiedBuildOutput, ...]] = {}
        self.volume_bindings: dict[str, tuple[RuntimeVolumeBinding, ...]] = {}
        self.current: dict[str, tuple[str, str]] = {}
        self.deployment_statuses: dict[str, str] = {}
        self.reservations: dict[str, str] = {}

    async def initialize(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        *,
        timeout: timedelta,
    ) -> DeploymentOrchestrationState:
        async with self._lock:
            existing = self._states.get(operation.id)
            if existing is not None:
                if existing.bind(context) != existing:
                    raise DeploymentContextInvalid()
                return existing
            state = DeploymentOrchestrationState(
                operation_id=operation.id,
                tenant_ref=context.tenant_ref,
                application_ref=context.application_ref,
                deployment_ref=context.deployment_ref,
                revision_ref=context.revision_ref,
                normalized_compose_digest=context.normalized_compose_digest,
                deadline_at=datetime.now(timezone.utc) + timeout,
            )
            self._states[operation.id] = state
            self.deployment_statuses[context.deployment_ref] = "building"
            self.reservations[operation.id] = "held"
            return state

    async def load(self, operation_id: str) -> DeploymentOrchestrationState | None:
        async with self._lock:
            return self._states.get(operation_id)

    async def save(
        self,
        state: DeploymentOrchestrationState,
        *,
        expected_version: int,
    ) -> DeploymentOrchestrationState:
        async with self._lock:
            current = self._states.get(state.operation_id)
            if current is None or current.version != expected_version:
                raise DeploymentCheckpointConflict()
            if state.builder_cursor < current.builder_cursor:
                raise DeploymentCheckpointConflict()
            saved = replace(state, version=expected_version + 1)
            self._states[state.operation_id] = saved
            if saved.deployment_ref:
                self.deployment_statuses[saved.deployment_ref] = _deployment_status(
                    saved.phase
                )
            return saved

    async def persist_build_outputs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        outputs: tuple[VerifiedBuildOutput, ...],
    ) -> None:
        del context
        async with self._lock:
            existing = self.build_outputs.get(operation.id)
            if existing is not None and existing != outputs:
                raise DeploymentCheckpointConflict()
            self.build_outputs[operation.id] = outputs

    async def bind_volumes(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        bindings: tuple[RuntimeVolumeBinding, ...],
    ) -> None:
        del context
        async with self._lock:
            existing = self.volume_bindings.get(operation.id)
            if existing is not None and existing != bindings:
                raise DeploymentCheckpointConflict()
            self.volume_bindings[operation.id] = bindings

    async def activate(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        runtime: RuntimeDeployment,
        *,
        worker_id: str,
    ) -> OperationRecord:
        current = await self._operations.heartbeat(
            TenantScope(context.tenant_ref),
            operation.id,
            worker_id=worker_id,
            lease_seconds=60,
        )
        if current.cancel_requested:
            raise DeploymentCancellationRequested()
        async with self._lock:
            latest = self._states.get(operation.id)
            if latest is None or latest.version != state.version:
                raise DeploymentCheckpointConflict()
            self.current[context.application_ref] = (
                context.revision_ref,
                context.deployment_ref,
            )
            self.deployment_statuses[context.deployment_ref] = "succeeded"
            self.reservations[operation.id] = "consumed"
            self._states[operation.id] = replace(
                state, phase="complete", runtime_status=runtime.status, version=state.version + 1
            )
        return await self._operations.complete(
            TenantScope(context.tenant_ref),
            operation.id,
            worker_id=worker_id,
            status=OperationStatus.SUCCEEDED,
            result={
                "applicationId": context.application_ref,
                "deploymentId": context.deployment_ref,
                "revisionId": context.revision_ref,
                "status": "succeeded",
            },
        )

    async def finalize_terminal(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        *,
        status: str,
        error_code: str | None,
    ) -> None:
        async with self._lock:
            latest = self._states.get(operation.id)
            if latest is None:
                raise DeploymentCheckpointConflict()
            if latest.phase not in {"failed", "canceled"}:
                self._states[operation.id] = replace(
                    state,
                    phase=status,
                    terminal_error_code=error_code,
                    version=max(latest.version, state.version) + 1,
                )
            self.deployment_statuses[context.deployment_ref] = status
            self.reservations[operation.id] = "released"


class DeploymentCancellationRequested(DeploymentOrchestrationError):
    code = "LAE_DEPLOYMENT_CANCELED"
    public_message = "The deployment was canceled."


class RuntimeSecretProvider(Protocol):
    async def issue_refs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
    ) -> tuple[RuntimeSecretRef, ...]: ...


class UnconfiguredRuntimeSecretProvider:
    async def issue_refs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
    ) -> tuple[RuntimeSecretRef, ...]:
        del operation
        if context.environment:
            raise RuntimeSecretsUnavailable()
        return ()


class FakeRuntimeSecretProvider:
    """Test provider that emits opaque refs and never retains plaintext."""

    async def issue_refs(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
    ) -> tuple[RuntimeSecretRef, ...]:
        return tuple(
            RuntimeSecretRef(
                service_key=item.service_key,
                name=item.name,
                secret_ref=(
                    "lsec_"
                    + hashlib.sha256(
                        f"{operation.id}:{item.service_key}:{item.name}".encode()
                    ).hexdigest()[:24]
                ),
                environment_version=context.environment_version,
            )
            for item in context.environment
        )


class RuntimeManifestRenderer:
    """Render the strict LAE-owned Luma manifest from catalog facts only."""

    def render(
        self,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        secrets: tuple[RuntimeSecretRef, ...],
    ) -> RuntimeManifest:
        outputs = {output.service_key: output for output in state.build_outputs}
        if set(outputs) != {service.key for service in context.services}:
            raise DeploymentBuildOutputInvalid()
        volume_refs = {binding.key: binding.volume_ref for binding in state.volume_bindings}
        if set(volume_refs) != {volume.key for volume in context.volumes}:
            raise DeploymentContextInvalid()
        services = tuple(
            RuntimeServiceSpec(
                key=service.key,
                role=service.role,
                required=service.required,
                image=RuntimeImageBinding(
                    builder_task_ref=state.builder_task_id or "",
                    build_key=service.build_key,
                    image_digest=outputs[service.key].image_digest,
                ),
                command=service.command,
                dependencies=service.dependencies,
                resources=RuntimeServiceResources(
                    cpu=service.cpu,
                    memory_mib=service.memory_mib,
                ),
                environment_names=service.environment_names,
                port=service.port,
                healthcheck=(
                    RuntimeServiceHealthcheck(
                        path=service.health_path,
                        interval_seconds=service.health_interval_seconds,
                    )
                    if service.health_path is not None
                    and service.health_interval_seconds is not None
                    else None
                ),
            )
            for service in context.services
        )
        exposure = "cn-edge" if context.region == "cn" else "external-edge"
        routes = tuple(
            RuntimeRouteSpec(
                service_key=route.service_key,
                hostname=route.hostname,
                container_port=route.container_port,
                exposure=exposure,
                health_path=route.health_path,
            )
            for route in context.routes
        )
        volumes = tuple(
            RuntimeVolumeSpec(
                key=volume.key,
                requested_bytes=volume.requested_bytes,
                existing_ref=volume_refs[volume.key],
                access_mode=volume.access_mode,
                mounts=tuple(
                    RuntimeVolumeMount(
                        service_key=service_key,
                        mount_path=volume.mount_path,
                        read_only=False,
                    )
                    for service_key in volume.service_keys
                ),
            )
            for volume in context.volumes
        )
        digest = _runtime_manifest_digest(
            context=context,
            services=services,
            routes=routes,
            volumes=volumes,
            secrets=secrets,
        )
        return RuntimeManifest(
            name=context.luma_name,
            kind=context.kind,
            region=context.region,
            services=services,
            routes=routes,
            volumes=volumes,
            secrets=secrets,
            manifest_digest=digest,
            normalized_compose_digest=context.normalized_compose_digest,
        )


@dataclass(frozen=True, slots=True)
class DeploymentWorkerConfig:
    build_limits: BuilderLimits
    lease_seconds: int = 60
    timeout_seconds: int = 1800
    event_page_limit: int = 100
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            not 5 <= self.lease_seconds <= 3600
            or not 30 <= self.timeout_seconds <= 7200
            or not 1 <= self.event_page_limit <= 500
            or not 0 <= self.poll_interval_seconds <= 60
        ):
            raise ValueError("deployment worker configuration is invalid")


class DeploymentStepStatus(StrEnum):
    WAITING = "waiting"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class DeploymentStepResult:
    status: DeploymentStepStatus
    operation: OperationRecord


class DeploymentStepRunner:
    def __init__(
        self,
        *,
        operations: OperationStore,
        contexts: DeploymentContextLoader,
        states: DeploymentStateStore,
        builder: LumaBuilderAdapter,
        runtime: LumaRuntimeAdapter,
        secrets: RuntimeSecretProvider,
        renderer: RuntimeManifestRenderer,
        config: DeploymentWorkerConfig,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._operations = operations
        self._contexts = contexts
        self._states = states
        self._builder = builder
        self._runtime = runtime
        self._secrets = secrets
        self._renderer = renderer
        self._config = config
        self._worker_id = worker_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def step(self, operation: OperationRecord) -> DeploymentStepResult:
        _validate_operation(operation)
        context = await self._contexts.load(operation)
        _validate_binding(operation, context)
        scope = TenantScope(operation.tenant_id)
        state = await self._states.initialize(
            operation,
            context,
            timeout=timedelta(seconds=self._config.timeout_seconds),
        )
        state = state.bind(context)
        current = await self._operations.heartbeat(
            scope,
            operation.id,
            worker_id=self._worker_id,
            lease_seconds=self._config.lease_seconds,
        )
        try:
            if current.cancel_requested:
                return await self._cancel(current, context, state)
            if state.deadline_at is not None and self._clock() >= state.deadline_at:
                return await self._fail(
                    current, context, state, DeploymentTimedOut()
                )
            if state.phase == "prepare":
                return await self._start_build(current, context, state)
            if state.phase == "building":
                return await self._watch_build(current, context, state)
            if state.phase == "rendering":
                return await self._prepare_render(current, context, state)
            if state.phase == "volumes":
                return await self._prepare_volumes(current, context, state)
            if state.phase == "deploying":
                return await self._deploy(current, context, state)
            if state.phase == "verifying":
                return await self._verify(current, context, state)
            if state.phase == "activating":
                return await self._activate(current, context, state)
            if state.phase == "complete":
                return DeploymentStepResult(DeploymentStepStatus.TERMINAL, current)
            if state.phase in {"failed", "canceled"}:
                status = (
                    OperationStatus.CANCELED
                    if state.phase == "canceled"
                    else OperationStatus.FAILED
                )
                completed = await self._operations.complete(
                    scope,
                    current.id,
                    worker_id=self._worker_id,
                    status=status,
                    **(
                        {}
                        if status is OperationStatus.CANCELED
                        else {
                            "error_code": state.terminal_error_code
                            or "LAE_DEPLOYMENT_ORCHESTRATION_FAILED",
                            "error_message": DeploymentOrchestrationError.public_message,
                        }
                    ),
                )
                return DeploymentStepResult(DeploymentStepStatus.TERMINAL, completed)
            raise DeploymentCheckpointConflict()
        except LeaseLost:
            raise
        except DeploymentCancellationRequested:
            return await self._cancel(current, context, state)
        except LumaAdapterError as exc:
            if exc.retryable:
                return DeploymentStepResult(DeploymentStepStatus.WAITING, current)
            return await self._fail(current, context, state, _map_luma_error(exc))
        except DeploymentOrchestrationError as exc:
            return await self._fail(current, context, state, exc)

    async def _start_build(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        mutation = await _call_sync(
            self._builder.create_build_task,
            _builder_context(context),
            BuildPlanRequest(
                source_snapshot_id=context.source_snapshot_id,
                source_snapshot_digest=context.source_snapshot_digest,
                signed_build_plan=context.signed_build_plan,
                credential_lease_id=context.build_credential_lease_id,
                limits=self._config.build_limits,
            ),
            idempotency_key=f"lae:{operation.id}:deployment-build:v1",
        )
        _validate_build_task(mutation.task, context)
        state = await self._states.save(
            replace(
                state,
                phase="building",
                builder_task_id=mutation.task.task_id,
                builder_status=mutation.task.status,
            ),
            expected_version=state.version,
        )
        await self._event(
            operation,
            "deployment.build.started",
            "deploy.build",
            "Application build started",
            {"replayed": mutation.replayed},
        )
        return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)

    async def _watch_build(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        if state.builder_task_id is None:
            raise DeploymentCheckpointConflict()
        page = await _call_sync(
            self._builder.get_builder_task_events,
            _builder_context(context),
            state.builder_task_id,
            after=state.builder_cursor,
            limit=self._config.event_page_limit,
        )
        cursor = state.builder_cursor
        for event in page.events:
            if event.cursor <= cursor or event.cursor != event.sequence:
                raise DeploymentCheckpointConflict()
            await self._operations.append_event(
                TenantScope(context.tenant_ref),
                operation.id,
                _safe_build_event(event),
                worker_id=self._worker_id,
            )
            state = await self._states.save(
                replace(
                    state,
                    builder_cursor=event.cursor,
                    builder_status=event.status or page.status,
                ),
                expected_version=state.version,
            )
            cursor = event.cursor
        if page.has_more:
            return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)
        task = await _call_sync(
            self._builder.get_builder_task,
            _builder_context(context),
            state.builder_task_id,
        )
        _validate_build_task(task, context)
        if not task.terminal:
            if task.status != state.builder_status:
                await self._states.save(
                    replace(state, builder_status=task.status),
                    expected_version=state.version,
                )
            return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)
        if task.status == "canceled":
            return await self._cancel(operation, context, state)
        if task.status != "succeeded":
            raise DeploymentBuildFailed()
        outputs = VerifiedBuildOutput.from_task(task, context)
        await self._states.persist_build_outputs(operation, context, outputs)
        await self._states.save(
            replace(
                state,
                phase="rendering",
                builder_status="succeeded",
                build_outputs=outputs,
            ),
            expected_version=state.version,
        )
        await self._event(
            operation,
            "deployment.build.succeeded",
            "deploy.build",
            "Application build completed",
            {"serviceCount": len(outputs)},
        )
        return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)

    async def _prepare_render(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        # Secret refs are intentionally minted exactly once, immediately
        # before the runtime deploy call. A preflight issue here can expire
        # while managed storage is prepared; replaying its idempotency key then
        # returns the expired consume-once ref and prevents deployment.
        await self._states.save(
            replace(state, phase="volumes"), expected_version=state.version
        )
        await self._event(
            operation,
            "deployment.manifest.validated",
            "deploy.render",
            "Deployment topology validated",
            {
                "serviceCount": len(context.services),
                "publicHttpRouteCount": len(context.routes),
                "volumeCount": len(context.volumes),
            },
        )
        return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)

    async def _prepare_volumes(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        specs = tuple(
            RuntimeVolumeSpec(
                key=item.key,
                requested_bytes=item.requested_bytes,
                existing_ref=item.existing_ref,
                access_mode=item.access_mode,
                mounts=tuple(
                    RuntimeVolumeMount(
                        service_key=service_key,
                        mount_path=item.mount_path,
                        read_only=False,
                    )
                    for service_key in item.service_keys
                ),
            )
            for item in context.volumes
        )
        bindings = await _call_sync(
            self._runtime.prepare_volumes,
            _runtime_context(context),
            specs,
            idempotency_key=f"lae:{operation.id}:runtime-volumes:v1",
        )
        if {item.key for item in bindings} != {item.key for item in context.volumes}:
            raise DeploymentContextInvalid()
        await self._states.bind_volumes(operation, context, bindings)
        await self._states.save(
            replace(state, phase="deploying", volume_bindings=bindings),
            expected_version=state.version,
        )
        await self._event(
            operation,
            "deployment.volumes.prepared",
            "deploy.volumes",
            "Managed volumes prepared",
            {"volumeCount": len(bindings)},
        )
        return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)

    async def _deploy(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        secrets = await self._secrets.issue_refs(operation, context)
        manifest = self._renderer.render(context, state, secrets)
        mutation = await _call_sync(
            self._runtime.deploy_revision,
            _runtime_context(context),
            manifest,
            idempotency_key=f"lae:{operation.id}:runtime-deploy:v1",
        )
        if mutation.deployment.manifest_digest != manifest.manifest_digest:
            raise DeploymentContextInvalid()
        await self._states.save(
            replace(
                state,
                phase="verifying",
                manifest_digest=manifest.manifest_digest,
                luma_deployment_ref=mutation.deployment.deployment_ref,
                runtime_status=mutation.deployment.status,
            ),
            expected_version=state.version,
        )
        await self._event(
            operation,
            "deployment.runtime.started",
            "deploy.runtime",
            "Luma deployment started",
            {"replayed": mutation.replayed},
        )
        return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)

    async def _verify(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        if state.luma_deployment_ref is None or state.manifest_digest is None:
            raise DeploymentCheckpointConflict()
        runtime = await _call_sync(
            self._runtime.get_runtime_deployment,
            _runtime_context(context),
            state.luma_deployment_ref,
        )
        if runtime.manifest_digest != state.manifest_digest:
            raise DeploymentContextInvalid()
        if runtime.status in {"failed", "degraded"}:
            raise DeploymentRuntimeFailed()
        if runtime.status == "canceled":
            return await self._cancel(operation, context, state)
        failed_services = {
            "failed",
            "unhealthy",
            "stopped",
            "missing",
        }
        waiting_services = {"pending", "starting", "unknown"}
        for service in context.services:
            if not service.required:
                continue
            status = runtime.service_statuses.get(service.key, "missing")
            if status in failed_services:
                raise DeploymentHealthFailed()
            if status in waiting_services or status != "healthy":
                await self._states.save(
                    replace(state, runtime_status=runtime.status),
                    expected_version=state.version,
                )
                return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)
        for route in context.routes:
            status = runtime.route_statuses.get(route.hostname, "missing")
            if status in {"failed", "unhealthy", "missing"}:
                raise DeploymentRouteFailed()
            if status != "ready":
                await self._states.save(
                    replace(state, runtime_status=runtime.status),
                    expected_version=state.version,
                )
                return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)
        if runtime.status != "running":
            return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)
        await self._states.save(
            replace(state, phase="activating", runtime_status=runtime.status),
            expected_version=state.version,
        )
        await self._event(
            operation,
            "deployment.health.ready",
            "deploy.verify",
            "All required services and public HTTP routes are healthy",
            {
                "requiredServiceCount": sum(item.required for item in context.services),
                "publicHttpRouteCount": len(context.routes),
            },
        )
        return DeploymentStepResult(DeploymentStepStatus.WAITING, operation)

    async def _activate(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        if state.luma_deployment_ref is None:
            raise DeploymentCheckpointConflict()
        runtime = await _call_sync(
            self._runtime.get_runtime_deployment,
            _runtime_context(context),
            state.luma_deployment_ref,
        )
        if runtime.status != "running":
            raise DeploymentHealthFailed()
        completed = await self._states.activate(
            operation,
            context,
            state,
            runtime,
            worker_id=self._worker_id,
        )
        return DeploymentStepResult(DeploymentStepStatus.TERMINAL, completed)

    async def _cancel(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
    ) -> DeploymentStepResult:
        if state.builder_task_id is not None and not state.build_cancel_forwarded:
            await _call_sync(
                self._builder.cancel_builder_task,
                _builder_context(context),
                state.builder_task_id,
            )
            state = await self._states.save(
                replace(state, build_cancel_forwarded=True),
                expected_version=state.version,
            )
        if state.luma_deployment_ref is not None and not state.runtime_cancel_forwarded:
            await _call_sync(
                self._runtime.cancel_runtime_deployment,
                _runtime_context(context),
                state.luma_deployment_ref,
            )
            state = await self._states.save(
                replace(state, runtime_cancel_forwarded=True),
                expected_version=state.version,
            )
        await self._states.finalize_terminal(
            operation,
            context,
            state,
            status="canceled",
            error_code=None,
        )
        completed = await self._operations.complete(
            TenantScope(context.tenant_ref),
            operation.id,
            worker_id=self._worker_id,
            status=OperationStatus.CANCELED,
        )
        return DeploymentStepResult(DeploymentStepStatus.TERMINAL, completed)

    async def _fail(
        self,
        operation: OperationRecord,
        context: DeploymentContext,
        state: DeploymentOrchestrationState,
        error: DeploymentOrchestrationError,
    ) -> DeploymentStepResult:
        if state.luma_deployment_ref is not None and not state.runtime_cancel_forwarded:
            try:
                await _call_sync(
                    self._runtime.cancel_runtime_deployment,
                    _runtime_context(context),
                    state.luma_deployment_ref,
                )
            except LumaAdapterError:
                pass
        await self._states.finalize_terminal(
            operation,
            context,
            state,
            status="failed",
            error_code=error.code,
        )
        completed = await self._operations.complete(
            TenantScope(context.tenant_ref),
            operation.id,
            worker_id=self._worker_id,
            status=OperationStatus.FAILED,
            error_code=error.code,
            error_message=error.public_message,
        )
        return DeploymentStepResult(DeploymentStepStatus.TERMINAL, completed)

    async def _event(
        self,
        operation: OperationRecord,
        event_type: str,
        phase: str,
        message: str,
        data: dict[str, object],
    ) -> None:
        await self._operations.append_event(
            TenantScope(operation.tenant_id),
            operation.id,
            EventInput(
                type=event_type,
                phase=phase,
                status="running",
                message=message,
                data=data,
            ),
            worker_id=self._worker_id,
        )


class DeploymentWorker:
    def __init__(
        self,
        operations: OperationStore,
        runner: DeploymentStepRunner,
        *,
        worker_id: str,
        config: DeploymentWorkerConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._operations = operations
        self._runner = runner
        self._worker_id = worker_id
        self._config = config
        self._sleep = sleep

    async def run_once(self) -> DeploymentStepResult | None:
        operation = await self._operations.claim_next(
            worker_id=self._worker_id,
            kinds=["deployment.create"],
            lease_seconds=self._config.lease_seconds,
        )
        if operation is None:
            return None
        while True:
            result = await self._runner.step(operation)
            operation = result.operation
            if result.status is DeploymentStepStatus.TERMINAL:
                return result
            await self._sleep(self._config.poll_interval_seconds)


def _runtime_manifest_digest(
    *,
    context: DeploymentContext,
    services: tuple[RuntimeServiceSpec, ...],
    routes: tuple[RuntimeRouteSpec, ...],
    volumes: tuple[RuntimeVolumeSpec, ...],
    secrets: tuple[RuntimeSecretRef, ...],
) -> str:
    # Ephemeral secret refs are excluded; only bound schema names/version are
    # included. Retries therefore render the same immutable manifest digest.
    value = {
        "schemaVersion": "lae.runtime-manifest/v1",
        "applicationId": context.application_ref,
        "revisionId": context.revision_ref,
        "name": context.luma_name,
        "kind": context.kind,
        "region": context.region,
        "services": [item.to_wire() for item in services],
        "routes": [item.to_wire() for item in routes],
        "volumes": [item.to_wire() for item in volumes],
        "environment": sorted(
            [
                {
                    "serviceKey": item.service_key,
                    "name": item.name,
                    "environmentVersion": item.environment_version,
                }
                for item in secrets
            ],
            key=lambda item: (item["serviceKey"], item["name"]),
        ),
    }
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _require_acyclic_dependencies(services: tuple[DeploymentService, ...]) -> None:
    by_key = {service.key: service for service in services}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise DeploymentContextInvalid()
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key].dependencies:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key)


def _validate_operation(operation: OperationRecord) -> None:
    if (
        operation.kind != "deployment.create"
        or operation.target_type != "application"
        or not operation.target_id.startswith("app_")
        or operation.status != "running"
    ):
        raise DeploymentContextInvalid()


def _validate_binding(
    operation: OperationRecord, context: DeploymentContext
) -> None:
    if (
        context.tenant_ref != operation.tenant_id
        or context.application_ref != operation.target_id
        or context.operation_ref != operation.id
    ):
        raise DeploymentContextInvalid()


def _builder_context(context: DeploymentContext) -> LumaCallContext:
    return LumaCallContext(
        tenant_ref=context.tenant_ref,
        application_ref=context.application_ref,
        external_operation_id=context.operation_ref,
    )


def _runtime_context(context: DeploymentContext) -> RuntimeCallContext:
    return RuntimeCallContext(
        tenant_ref=context.tenant_ref,
        application_ref=context.application_ref,
        operation_ref=context.operation_ref,
        revision_ref=context.revision_ref,
        deployment_ref=context.deployment_ref,
    )


def _validate_build_task(task: BuilderTask, context: DeploymentContext) -> None:
    if (
        task.kind != "build-plan"
        or task.external_operation_id != context.operation_ref
        or task.tenant_ref != context.tenant_ref
        or task.application_ref != context.application_ref
    ):
        raise DeploymentContextInvalid()


def _safe_build_event(event: BuilderTaskEvent) -> EventInput:
    event_types = {
        "status": "deployment.build.progress",
        "resolve": "deployment.build.resolve",
        "build": "deployment.build.progress",
        "push": "deployment.build.progress",
        "complete": "deployment.build.progress",
    }
    return EventInput(
        type=event_types.get(event.event_type, "deployment.build.progress"),
        phase="deploy.build",
        status="running",
        message="Application build progress updated",
        data={
            "builderCursor": event.cursor,
            "builderEvent": event.event_type
            if event.event_type in event_types
            else "update",
            "builderStatus": (event.status or "running").replace("_", "-"),
        },
    )


def _deployment_status(phase: str) -> str:
    return {
        "prepare": "queued",
        "building": "building",
        "rendering": "building",
        "volumes": "deploying",
        "deploying": "deploying",
        "verifying": "verifying",
        "activating": "verifying",
        "complete": "succeeded",
        "failed": "failed",
        "canceled": "canceled",
    }[phase]


def _map_luma_error(error: LumaAdapterError) -> DeploymentOrchestrationError:
    if error.code is AdapterErrorCode.RUNTIME_DEPLOY_FAILED:
        return DeploymentRuntimeFailed()
    if error.code in {
        AdapterErrorCode.INVALID_REQUEST,
        AdapterErrorCode.PROTOCOL_ERROR,
        AdapterErrorCode.IDEMPOTENCY_CONFLICT,
    }:
        return DeploymentContextInvalid()
    return DeploymentRuntimeFailed()


async def _call_sync(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(function, *args, **kwargs)


__all__ = [
    "DeploymentBuildFailed",
    "DeploymentBuildOutputInvalid",
    "DeploymentCheckpointConflict",
    "DeploymentContext",
    "DeploymentContextInvalid",
    "DeploymentContextLoader",
    "DeploymentEnvironmentRequirement",
    "DeploymentHealthFailed",
    "DeploymentOrchestrationError",
    "DeploymentOrchestrationState",
    "DeploymentRoute",
    "DeploymentRouteFailed",
    "DeploymentService",
    "DeploymentStateStore",
    "DeploymentStepResult",
    "DeploymentStepRunner",
    "DeploymentStepStatus",
    "DeploymentTimedOut",
    "DeploymentVolume",
    "DeploymentWorker",
    "DeploymentWorkerConfig",
    "FakeRuntimeSecretProvider",
    "InMemoryDeploymentStateStore",
    "RuntimeManifestRenderer",
    "RuntimeSecretProvider",
    "RuntimeSecretsUnavailable",
    "UnconfiguredRuntimeSecretProvider",
    "VerifiedBuildOutput",
]
