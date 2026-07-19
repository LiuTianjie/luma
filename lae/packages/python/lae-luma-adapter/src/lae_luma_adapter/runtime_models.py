from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import Mapping

from .errors import AdapterErrorCode, LumaAdapterError


_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$")
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOSTNAME = re.compile(r"^[0-9a-f]{32}\.itool\.tech$")
_SECRET_REF = re.compile(r"^lsec_[A-Za-z0-9][A-Za-z0-9._-]{7,122}$")
_CPU = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _invalid() -> LumaAdapterError:
    return LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)


def _reference(value: str) -> str:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise _invalid()
    return value


@dataclass(frozen=True, slots=True)
class RuntimeServicePrincipal:
    """Dedicated LAE runtime principal, never a Luma management token."""

    principal_id: str
    token: str = field(repr=False)
    audience: str = "luma-lae-runtime"

    def __post_init__(self) -> None:
        _reference(self.principal_id)
        if (
            not isinstance(self.token, str)
            or not 16 <= len(self.token) <= 512
            or any(not 33 <= ord(character) <= 126 for character in self.token)
            or self.audience != "luma-lae-runtime"
        ):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class RuntimeCallContext:
    tenant_ref: str
    application_ref: str
    operation_ref: str
    revision_ref: str
    deployment_ref: str
    request_id: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.tenant_ref,
            self.application_ref,
            self.operation_ref,
            self.revision_ref,
            self.deployment_ref,
        ):
            _reference(value)
        if self.request_id is not None:
            _reference(self.request_id)

    def headers(self) -> dict[str, str]:
        return {
            "X-LAE-Tenant-Id": self.tenant_ref,
            "X-LAE-Application-Id": self.application_ref,
            "X-LAE-Operation-Id": self.operation_ref,
            "X-LAE-Revision-Id": self.revision_ref,
            "X-LAE-Deployment-Id": self.deployment_ref,
            **({"X-Request-Id": self.request_id} if self.request_id else {}),
        }


@dataclass(frozen=True, slots=True)
class RuntimeImageBinding:
    builder_task_ref: str
    build_key: str
    image_digest: str

    def __post_init__(self) -> None:
        _reference(self.builder_task_ref)
        if not isinstance(self.build_key, str) or _KEY.fullmatch(self.build_key) is None:
            raise _invalid()
        if (
            not isinstance(self.image_digest, str)
            or _DIGEST.fullmatch(self.image_digest) is None
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, str]:
        # Luma resolves its private registry coordinate from its own builder
        # task. LAE never receives or persists that internal coordinate.
        return {
            "builderTaskRef": self.builder_task_ref,
            "buildKey": self.build_key,
            "imageDigest": self.image_digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeServiceResources:
    cpu: str
    memory_mib: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cpu, str)
            or _CPU.fullmatch(self.cpu) is None
            or not 0 < float(self.cpu) <= 32
            or isinstance(self.memory_mib, bool)
            or not isinstance(self.memory_mib, int)
            or not 64 <= self.memory_mib <= 1_048_576
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {"cpu": self.cpu, "memoryMiB": self.memory_mib}


@dataclass(frozen=True, slots=True)
class RuntimeServiceHealthcheck:
    path: str
    interval_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or "?" in self.path
            or "#" in self.path
            or len(self.path) > 256
            or any(character in self.path for character in "\x00\r\n")
            or isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, int)
            or not 1 <= self.interval_seconds <= 300
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "type": "http",
            "path": self.path,
            "intervalSeconds": self.interval_seconds,
        }


@dataclass(frozen=True, slots=True)
class RuntimeServiceSpec:
    key: str
    role: str
    image: RuntimeImageBinding
    command: str | None
    dependencies: tuple[str, ...]
    resources: RuntimeServiceResources
    environment_names: tuple[str, ...]
    port: int | None = None
    healthcheck: RuntimeServiceHealthcheck | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or _KEY.fullmatch(self.key) is None:
            raise _invalid()
        if self.role not in {"http", "internal", "worker", "datastore"}:
            raise _invalid()
        if self.command is not None and (
            not isinstance(self.command, str)
            or len(self.command) > 4096
            or "\x00" in self.command
        ):
            raise _invalid()
        if (
            not isinstance(self.dependencies, tuple)
            or len(self.dependencies) != len(set(self.dependencies))
            or self.key in self.dependencies
            or any(
                not isinstance(value, str) or _KEY.fullmatch(value) is None
                for value in self.dependencies
            )
            or not isinstance(self.resources, RuntimeServiceResources)
            or not isinstance(self.environment_names, tuple)
            or len(self.environment_names) != len(set(self.environment_names))
            or any(
                not isinstance(value, str) or _ENV.fullmatch(value) is None
                for value in self.environment_names
            )
        ):
            raise _invalid()
        if self.port is not None and (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise _invalid()
        if self.healthcheck is not None and (
            self.role != "http"
            or self.port is None
            or not isinstance(self.healthcheck, RuntimeServiceHealthcheck)
        ):
            raise _invalid()
        if not isinstance(self.required, bool):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "key": self.key,
            "role": self.role,
            "required": self.required,
            "exposure": "none",
            "image": self.image.to_wire(),
            "command": self.command,
            "dependencies": list(self.dependencies),
            "resources": self.resources.to_wire(),
            "environmentNames": list(self.environment_names),
            **({"port": self.port} if self.port is not None else {}),
            **(
                {"healthcheck": self.healthcheck.to_wire()}
                if self.healthcheck is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class RuntimeRouteSpec:
    service_key: str
    hostname: str
    container_port: int
    exposure: str
    health_path: str = "/"

    def __post_init__(self) -> None:
        if not isinstance(self.service_key, str) or _KEY.fullmatch(self.service_key) is None:
            raise _invalid()
        if not isinstance(self.hostname, str) or _HOSTNAME.fullmatch(self.hostname) is None:
            # The catalog-allocated random hostname is the only accepted domain.
            raise _invalid()
        if (
            isinstance(self.container_port, bool)
            or not isinstance(self.container_port, int)
            or not 1 <= self.container_port <= 65535
        ):
            raise _invalid()
        if self.exposure not in {"cn-edge", "external-edge"}:
            raise _invalid()
        if (
            not isinstance(self.health_path, str)
            or not self.health_path.startswith("/")
            or "?" in self.health_path
            or "#" in self.health_path
            or len(self.health_path) > 256
            or any(character in self.health_path for character in "\x00\r\n")
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "serviceKey": self.service_key,
            "kind": "http",
            "hostname": self.hostname,
            "containerPort": self.container_port,
            "exposure": self.exposure,
            "healthPath": self.health_path,
        }


@dataclass(frozen=True, slots=True)
class RuntimeVolumeMount:
    service_key: str
    mount_path: str
    read_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.service_key, str) or _KEY.fullmatch(self.service_key) is None:
            raise _invalid()
        if (
            not isinstance(self.mount_path, str)
            or not self.mount_path.startswith("/")
            or self.mount_path == "/"
            or len(self.mount_path) > 1024
            or "//" in self.mount_path
            or any(part in {".", ".."} for part in self.mount_path.split("/"))
            or any(character in self.mount_path for character in "\x00\r\n")
            or not isinstance(self.read_only, bool)
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "serviceKey": self.service_key,
            "mountPath": self.mount_path,
            "readOnly": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class RuntimeVolumeSpec:
    key: str
    requested_bytes: int
    access_mode: str
    mounts: tuple[RuntimeVolumeMount, ...]
    existing_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or _KEY.fullmatch(self.key) is None:
            raise _invalid()
        if (
            isinstance(self.requested_bytes, bool)
            or not isinstance(self.requested_bytes, int)
            or self.requested_bytes <= 0
        ):
            raise _invalid()
        if self.existing_ref is not None:
            _reference(self.existing_ref)
        if (
            self.access_mode not in {"ReadWriteOnce", "ReadWriteMany"}
            or not isinstance(self.mounts, tuple)
            or not self.mounts
            or len({(item.service_key, item.mount_path) for item in self.mounts})
            != len(self.mounts)
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "key": self.key,
            "requestedBytes": self.requested_bytes,
            "storagePolicy": "managed",
            "accessMode": self.access_mode,
            "mounts": [mount.to_wire() for mount in self.mounts],
            **({"existingRef": self.existing_ref} if self.existing_ref else {}),
        }


@dataclass(frozen=True, slots=True)
class RuntimeSecretRef:
    service_key: str
    name: str
    secret_ref: str = field(repr=False)
    environment_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.service_key, str) or _KEY.fullmatch(self.service_key) is None:
            raise _invalid()
        if not isinstance(self.name, str) or _ENV.fullmatch(self.name) is None:
            raise _invalid()
        if not isinstance(self.secret_ref, str) or _SECRET_REF.fullmatch(self.secret_ref) is None:
            raise _invalid()
        if (
            isinstance(self.environment_version, bool)
            or not isinstance(self.environment_version, int)
            or self.environment_version < 0
        ):
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "serviceKey": self.service_key,
            "name": self.name,
            "secretRef": self.secret_ref,
            "environmentVersion": self.environment_version,
        }


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    name: str
    kind: str
    region: str
    services: tuple[RuntimeServiceSpec, ...]
    routes: tuple[RuntimeRouteSpec, ...]
    volumes: tuple[RuntimeVolumeSpec, ...]
    secrets: tuple[RuntimeSecretRef, ...]
    manifest_digest: str
    normalized_compose_digest: str | None = None

    def __post_init__(self) -> None:
        _reference(self.name)
        if self.kind not in {"service", "compose"} or self.region not in {"cn", "global"}:
            raise _invalid()
        if not self.services or (self.kind == "service" and len(self.services) != 1):
            raise _invalid()
        if self.kind == "service" and self.normalized_compose_digest is not None:
            raise _invalid()
        if self.kind == "compose" and self.normalized_compose_digest is None:
            raise _invalid()
        if _DIGEST.fullmatch(self.manifest_digest) is None:
            raise _invalid()
        if self.normalized_compose_digest is not None and _DIGEST.fullmatch(
            self.normalized_compose_digest
        ) is None:
            raise _invalid()
        service_keys = [service.key for service in self.services]
        if len(service_keys) != len(set(service_keys)):
            raise _invalid()
        by_key = {service.key: service for service in self.services}
        if any(
            dependency not in by_key
            for service in self.services
            for dependency in service.dependencies
        ):
            raise _invalid()
        _require_acyclic_services(by_key)
        if self.routes and (
            len({route.hostname for route in self.routes}) != len(self.routes)
            or len({route.service_key for route in self.routes}) != len(self.routes)
        ):
            raise _invalid()
        for route in self.routes:
            service = by_key.get(route.service_key)
            if (
                service is None
                or service.role != "http"
                or service.port != route.container_port
                or (
                    service.healthcheck is not None
                    and service.healthcheck.path != route.health_path
                )
            ):
                raise _invalid()
            expected_exposure = "cn-edge" if self.region == "cn" else "external-edge"
            if route.exposure != expected_exposure:
                raise _invalid()
        if len({volume.key for volume in self.volumes}) != len(self.volumes):
            raise _invalid()
        if any(
            mount.service_key not in by_key
            for volume in self.volumes
            for mount in volume.mounts
        ):
            raise _invalid()
        secret_keys = [(item.service_key, item.name) for item in self.secrets]
        if len(secret_keys) != len(set(secret_keys)) or any(
            item.service_key not in by_key for item in self.secrets
        ):
            raise _invalid()
        expected_secrets = {
            (service.key, name)
            for service in self.services
            for name in service.environment_names
        }
        if set(secret_keys) != expected_secrets:
            raise _invalid()

    def to_wire(self) -> dict[str, object]:
        return {
            "schemaVersion": "luma.lae-runtime/v1",
            "name": self.name,
            "kind": self.kind,
            "region": self.region,
            "services": [service.to_wire() for service in self.services],
            "routes": [route.to_wire() for route in self.routes],
            "volumes": [volume.to_wire() for volume in self.volumes],
            "secretRefs": [secret.to_wire() for secret in self.secrets],
            "manifestDigest": self.manifest_digest,
            **(
                {"normalizedComposeDigest": self.normalized_compose_digest}
                if self.normalized_compose_digest
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class RuntimeVolumeBinding:
    key: str
    volume_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or _KEY.fullmatch(self.key) is None:
            raise _invalid()
        _reference(self.volume_ref)


@dataclass(frozen=True, slots=True)
class RuntimeDeployment:
    deployment_ref: str
    status: str
    manifest_digest: str
    service_statuses: Mapping[str, str]
    route_statuses: Mapping[str, str]
    volume_bindings: tuple[RuntimeVolumeBinding, ...] = ()

    def __post_init__(self) -> None:
        _reference(self.deployment_ref)
        if self.status not in {
            "preparing",
            "deploying",
            "running",
            "degraded",
            "failed",
            "canceling",
            "canceled",
            "suspending",
            "suspended",
            "resuming",
            "restarting",
            "rolling_back",
            "deleting",
            "deleted",
            "superseded",
        }:
            raise _invalid()
        if _DIGEST.fullmatch(self.manifest_digest) is None:
            raise _invalid()
        for mapping in (self.service_statuses, self.route_statuses):
            if not isinstance(mapping, Mapping) or len(mapping) > 128:
                raise _invalid()
            for key, value in mapping.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise _invalid()

    @property
    def terminal(self) -> bool:
        return self.status in {
            "running",
            "degraded",
            "failed",
            "canceled",
            "suspended",
            "deleted",
            "superseded",
        }


@dataclass(frozen=True, slots=True)
class RuntimeMutation:
    deployment: RuntimeDeployment
    replayed: bool


@dataclass(frozen=True, slots=True)
class RuntimeLogTail:
    luma_name: str
    service_key: str
    tail: int
    logs: tuple[str, ...]
    truncated: bool
    updated_at: int

    def __post_init__(self) -> None:
        _reference(self.luma_name)
        if (
            _KEY.fullmatch(self.service_key) is None
            or isinstance(self.tail, bool)
            or not 1 <= self.tail <= 500
            or not isinstance(self.logs, tuple)
            or len(self.logs) > self.tail
            or any(
                not isinstance(line, str)
                or len(line.encode("utf-8")) > 2051
                for line in self.logs
            )
            or not isinstance(self.truncated, bool)
            or isinstance(self.updated_at, bool)
            or not isinstance(self.updated_at, int)
            or self.updated_at < 0
        ):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class RuntimeMetricsHistory:
    luma_name: str
    service_key: str
    window_seconds: int
    series: Mapping[str, tuple[tuple[int, float | int], ...]]
    updated_at: int

    def __post_init__(self) -> None:
        _reference(self.luma_name)
        if (
            _KEY.fullmatch(self.service_key) is None
            or isinstance(self.window_seconds, bool)
            or not 1 <= self.window_seconds <= 7 * 24 * 3600
            or not isinstance(self.series, Mapping)
            or set(self.series) != {"cpuPercent", "memoryUsageBytes"}
            or isinstance(self.updated_at, bool)
            or not isinstance(self.updated_at, int)
            or self.updated_at < 0
        ):
            raise _invalid()
        for points in self.series.values():
            if not isinstance(points, tuple) or len(points) > 10_000:
                raise _invalid()
            for point in points:
                if (
                    not isinstance(point, tuple)
                    or len(point) != 2
                    or isinstance(point[0], bool)
                    or not isinstance(point[0], int)
                    or isinstance(point[1], bool)
                    or not isinstance(point[1], (int, float))
                    or not math.isfinite(float(point[1]))
                ):
                    raise _invalid()


def _require_acyclic_services(by_key: Mapping[str, RuntimeServiceSpec]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise _invalid()
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key].dependencies:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key)


__all__ = [
    "RuntimeCallContext",
    "RuntimeDeployment",
    "RuntimeImageBinding",
    "RuntimeManifest",
    "RuntimeLogTail",
    "RuntimeMetricsHistory",
    "RuntimeMutation",
    "RuntimeRouteSpec",
    "RuntimeSecretRef",
    "RuntimeServicePrincipal",
    "RuntimeServiceHealthcheck",
    "RuntimeServiceResources",
    "RuntimeServiceSpec",
    "RuntimeVolumeBinding",
    "RuntimeVolumeMount",
    "RuntimeVolumeSpec",
]
