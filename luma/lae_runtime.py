from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import LumaError


SCHEMA_VERSION = "luma.lae-runtime/v1"
RUNTIME_AUDIENCE = "luma-lae-runtime"
MAX_RUNTIME_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SECRET_VALUE_BYTES = 64 * 1024

SCOPE_VOLUMES_PREPARE = "runtime:volumes:prepare"
SCOPE_DEPLOYMENTS_WRITE = "runtime:deployments:write"
SCOPE_DEPLOYMENTS_READ = "runtime:deployments:read"
SCOPE_SECRETS_ISSUE = "runtime:secrets:issue"
SCOPE_LOGS_READ = "runtime:logs"
SCOPE_METRICS_READ = "runtime:metrics"

_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$")
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOSTNAME = re.compile(r"^[0-9a-f]{32}\.itool\.tech$")
_SECRET_REF = re.compile(r"^lsec_[A-Za-z0-9][A-Za-z0-9._-]{7,122}$")
_CPU = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class LumaRuntimeError(LumaError):
    """Stable public error for the dedicated LAE runtime boundary."""

    def __init__(self, message: str, *, status: int = 422, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)


def unauthorized() -> LumaRuntimeError:
    return LumaRuntimeError("unauthorized", status=401, code="unauthorized")


def forbidden() -> LumaRuntimeError:
    return LumaRuntimeError("forbidden", status=403, code="forbidden")


def not_found() -> LumaRuntimeError:
    # Do not disclose whether a deployment exists outside the caller binding.
    return LumaRuntimeError("runtime deployment not found", status=404, code="not_found")


def conflict(message: str = "Idempotency-Key is already bound to another request") -> LumaRuntimeError:
    return LumaRuntimeError(message, status=409, code="conflict")


def unavailable(message: str) -> LumaRuntimeError:
    return LumaRuntimeError(message, status=503, code="service_unavailable")


def invalid(message: str = "LAE runtime request is invalid") -> LumaRuntimeError:
    return LumaRuntimeError(message, status=422, code="invalid_request")


def _require_reference(value: Any, label: str) -> str:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise invalid(f"{label} is invalid")
    return value


def _require_key(value: Any, label: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise invalid(f"{label} is invalid")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise invalid(f"{label} is invalid")
    return value


def _require_exact_keys(
    value: Any,
    required: set[str],
    *,
    optional: set[str] | None = None,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid(f"{label} must be an object")
    keys = set(value)
    optional = optional or set()
    if not required.issubset(keys) or keys - required - optional:
        raise invalid(f"{label} has an invalid schema")
    return value


def _positive_int(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise invalid(f"{label} must be a positive integer")
    if maximum is not None and value > maximum:
        raise invalid(f"{label} exceeds the platform limit")
    return value


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise invalid(f"{label} must be a non-negative integer")
    return value


def normalize_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise invalid("Idempotency-Key header is required")
    key = value.strip()
    if not key:
        raise invalid("Idempotency-Key header is required")
    if len(key) > 200 or any(ord(character) < 33 or ord(character) > 126 for character in key):
        raise invalid("Idempotency-Key header is invalid")
    return key


def canonical_hash(value: Any) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise invalid() from exc
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    tenant_ref: str
    application_ref: str
    operation_ref: str
    revision_ref: str
    deployment_ref: str

    def __post_init__(self) -> None:
        _require_reference(self.tenant_ref, "tenant binding")
        _require_reference(self.application_ref, "application binding")
        _require_reference(self.operation_ref, "operation binding")
        _require_reference(self.revision_ref, "revision binding")
        _require_reference(self.deployment_ref, "deployment binding")

    def state_body(self) -> dict[str, str]:
        return {
            "tenantRef": self.tenant_ref,
            "applicationRef": self.application_ref,
            "operationRef": self.operation_ref,
            "revisionRef": self.revision_ref,
            "deploymentRef": self.deployment_ref,
        }

    @classmethod
    def from_headers(cls, headers: Mapping[str, Any]) -> "RuntimeBinding":
        def get(name: str) -> str:
            value = headers.get(name)
            if value is None:
                value = headers.get(name.lower())
            return str(value or "")

        return cls(
            tenant_ref=get("X-LAE-Tenant-Id"),
            application_ref=get("X-LAE-Application-Id"),
            operation_ref=get("X-LAE-Operation-Id"),
            revision_ref=get("X-LAE-Revision-Id"),
            deployment_ref=get("X-LAE-Deployment-Id"),
        )


def validate_volume_prepare_body(body: Any) -> tuple[dict[str, Any], ...]:
    value = _require_exact_keys(
        body,
        {"schemaVersion", "volumes"},
        label="volume prepare request",
    )
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise invalid("volume prepare schemaVersion is invalid")
    raw = value.get("volumes")
    if not isinstance(raw, list) or len(raw) > 64:
        raise invalid("volumes must be a bounded array")
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, item in enumerate(raw):
        volume = _require_exact_keys(
            item,
            {
                "key",
                "requestedBytes",
                "storagePolicy",
                "accessMode",
                "mounts",
            },
            optional={"existingRef"},
            label=f"volumes[{index}]",
        )
        key = _require_key(volume.get("key"), f"volumes[{index}].key")
        access_mode = volume.get("accessMode")
        mounts = volume.get("mounts")
        if (
            key in keys
            or volume.get("storagePolicy") != "managed"
            or access_mode not in {"ReadWriteOnce", "ReadWriteMany"}
            or not isinstance(mounts, list)
            or not mounts
            or len(mounts) > 64
        ):
            raise invalid("volume definitions are invalid")
        keys.add(key)
        normalized_mounts: list[dict[str, Any]] = []
        mount_keys: set[tuple[str, str]] = set()
        for mount_index, raw_mount in enumerate(mounts):
            mount = _require_exact_keys(
                raw_mount,
                {"serviceKey", "mountPath", "readOnly"},
                label=f"volumes[{index}].mounts[{mount_index}]",
            )
            service_key = _require_key(
                mount.get("serviceKey"),
                f"volumes[{index}].mounts[{mount_index}].serviceKey",
            )
            mount_path = mount.get("mountPath")
            if (
                not isinstance(mount_path, str)
                or not mount_path.startswith("/")
                or mount_path == "/"
                or len(mount_path) > 1024
                or "//" in mount_path
                or any(part in {".", ".."} for part in mount_path.split("/"))
                or any(character in mount_path for character in "\x00\r\n")
                or not isinstance(mount.get("readOnly"), bool)
                or (service_key, mount_path) in mount_keys
            ):
                raise invalid("volume mount binding is invalid")
            mount_keys.add((service_key, mount_path))
            normalized_mounts.append(
                {
                    "serviceKey": service_key,
                    "mountPath": mount_path,
                    "readOnly": bool(mount["readOnly"]),
                }
            )
        normalized: dict[str, Any] = {
            "key": key,
            "requestedBytes": _positive_int(
                volume.get("requestedBytes"),
                f"volumes[{index}].requestedBytes",
                maximum=16 * 1024 * 1024 * 1024 * 1024,
            ),
            "storagePolicy": "managed",
            "accessMode": access_mode,
            "mounts": normalized_mounts,
        }
        if "existingRef" in volume:
            normalized["existingRef"] = _require_reference(
                volume.get("existingRef"), f"volumes[{index}].existingRef"
            )
        result.append(normalized)
    return tuple(result)


def validate_deploy_body(body: Any, binding: RuntimeBinding) -> dict[str, Any]:
    value = _require_exact_keys(
        body,
        {"schemaVersion", "manifest"},
        label="deployment request",
    )
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise invalid("deployment schemaVersion is invalid")
    manifest = _require_exact_keys(
        value.get("manifest"),
        {
            "schemaVersion",
            "name",
            "kind",
            "region",
            "services",
            "routes",
            "volumes",
            "secretRefs",
            "manifestDigest",
        },
        optional={"normalizedComposeDigest"},
        label="manifest",
    )
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise invalid("manifest schemaVersion is invalid")
    name = _require_reference(manifest.get("name"), "manifest.name")
    kind = manifest.get("kind")
    region = manifest.get("region")
    if kind not in {"service", "compose"} or region not in {"cn", "global"}:
        raise invalid("manifest kind or region is invalid")
    if kind == "service" and "normalizedComposeDigest" in manifest:
        raise invalid("service manifest cannot contain normalizedComposeDigest")
    if kind == "compose" and "normalizedComposeDigest" not in manifest:
        raise invalid("compose manifest requires normalizedComposeDigest")
    normalized_compose_digest = None
    if "normalizedComposeDigest" in manifest:
        normalized_compose_digest = _require_digest(
            manifest.get("normalizedComposeDigest"), "normalizedComposeDigest"
        )

    raw_services = manifest.get("services")
    if (
        not isinstance(raw_services, list)
        or not raw_services
        or len(raw_services) > 64
        or (kind == "service" and len(raw_services) != 1)
    ):
        raise invalid("manifest services are invalid")
    services: list[dict[str, Any]] = []
    service_keys: set[str] = set()
    build_keys: set[str] = set()
    for index, raw_service in enumerate(raw_services):
        service = _require_exact_keys(
            raw_service,
            {
                "key",
                "role",
                "required",
                "exposure",
                "image",
                "command",
                "dependencies",
                "resources",
                "environmentNames",
            },
            optional={"port", "healthcheck"},
            label=f"services[{index}]",
        )
        key = _require_key(service.get("key"), f"services[{index}].key")
        role = service.get("role")
        if key in service_keys or role not in {"http", "internal", "worker", "datastore"}:
            raise invalid("manifest services are invalid")
        if not isinstance(service.get("required"), bool) or service.get("exposure") != "none":
            # Public access is route-owned and HTTP-only. A service may never
            # smuggle host/TCP exposure through this protocol.
            raise invalid("manifest service exposure is invalid")
        image = _require_exact_keys(
            service.get("image"),
            {"builderTaskRef", "buildKey", "imageDigest"},
            label=f"services[{index}].image",
        )
        build_key = _require_key(image.get("buildKey"), f"services[{index}].image.buildKey")
        if build_key in build_keys:
            raise invalid("manifest build bindings are duplicated")
        service_keys.add(key)
        build_keys.add(build_key)
        command = service.get("command")
        dependencies = service.get("dependencies")
        resources = _require_exact_keys(
            service.get("resources"),
            {"cpu", "memoryMiB"},
            label=f"services[{index}].resources",
        )
        cpu = resources.get("cpu")
        memory_mib = resources.get("memoryMiB")
        environment_names = service.get("environmentNames")
        if (
            command is not None
            and (
                not isinstance(command, str)
                or len(command) > 4096
                or "\x00" in command
            )
            or not isinstance(dependencies, list)
            or len(dependencies) > 63
            or not all(isinstance(item, str) and _KEY.fullmatch(item) for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or key in dependencies
            or not isinstance(cpu, str)
            or _CPU.fullmatch(cpu) is None
            or not (0 < float(cpu) <= 32)
            or isinstance(memory_mib, bool)
            or not isinstance(memory_mib, int)
            or not 64 <= memory_mib <= 1_048_576
            or not isinstance(environment_names, list)
            or len(environment_names) > 128
            or not all(
                isinstance(item, str) and _ENV.fullmatch(item)
                for item in environment_names
            )
            or len(set(environment_names)) != len(environment_names)
        ):
            raise invalid("manifest service runtime fields are invalid")
        port: int | None = None
        if "port" in service:
            port = _positive_int(
                service.get("port"), f"services[{index}].port", maximum=65535
            )
        healthcheck: dict[str, Any] | None = None
        if "healthcheck" in service:
            raw_health = _require_exact_keys(
                service.get("healthcheck"),
                {"type", "path", "intervalSeconds"},
                label=f"services[{index}].healthcheck",
            )
            path = raw_health.get("path")
            interval = raw_health.get("intervalSeconds")
            if (
                raw_health.get("type") != "http"
                or not isinstance(path, str)
                or not path.startswith("/")
                or "?" in path
                or "#" in path
                or len(path) > 256
                or any(character in path for character in "\x00\r\n")
                or isinstance(interval, bool)
                or not isinstance(interval, int)
                or not 1 <= interval <= 300
                or role != "http"
                or port is None
            ):
                raise invalid("manifest service healthcheck is invalid")
            healthcheck = {
                "type": "http",
                "path": path,
                "intervalSeconds": interval,
            }
        services.append(
            {
                "key": key,
                "role": role,
                "required": service["required"],
                "exposure": "none",
                "image": {
                    "builderTaskRef": _require_reference(
                        image.get("builderTaskRef"),
                        f"services[{index}].image.builderTaskRef",
                    ),
                    "buildKey": build_key,
                    "imageDigest": _require_digest(
                        image.get("imageDigest"),
                        f"services[{index}].image.imageDigest",
                    ),
                },
                "command": command,
                "dependencies": list(dependencies),
                "resources": {"cpu": cpu, "memoryMiB": memory_mib},
                "environmentNames": list(environment_names),
                **({"port": port} if port is not None else {}),
                **({"healthcheck": healthcheck} if healthcheck is not None else {}),
            }
        )

    roles = {item["key"]: item["role"] for item in services}
    services_by_key = {item["key"]: item for item in services}
    if any(
        dependency not in roles
        for service in services
        for dependency in service["dependencies"]
    ):
        raise invalid("manifest service dependencies are invalid")
    pending = {item["key"]: set(item["dependencies"]) for item in services}
    while pending:
        ready = {key for key, dependencies in pending.items() if not dependencies}
        if not ready:
            raise invalid("manifest service dependency graph contains a cycle")
        pending = {
            key: dependencies - ready
            for key, dependencies in pending.items()
            if key not in ready
        }
    raw_routes = manifest.get("routes")
    if not isinstance(raw_routes, list) or len(raw_routes) > 64:
        raise invalid("manifest routes are invalid")
    routes: list[dict[str, Any]] = []
    hostnames: set[str] = set()
    routed_services: set[str] = set()
    expected_exposure = "cn-edge" if region == "cn" else "external-edge"
    for index, raw_route in enumerate(raw_routes):
        route = _require_exact_keys(
            raw_route,
            {"serviceKey", "kind", "hostname", "containerPort", "exposure", "healthPath"},
            label=f"routes[{index}]",
        )
        service_key = _require_key(route.get("serviceKey"), f"routes[{index}].serviceKey")
        hostname = route.get("hostname")
        health_path = route.get("healthPath")
        if (
            roles.get(service_key) != "http"
            or route.get("kind") != "http"
            or not isinstance(hostname, str)
            or _HOSTNAME.fullmatch(hostname) is None
            or hostname in hostnames
            or service_key in routed_services
            or route.get("exposure") != expected_exposure
            or not isinstance(health_path, str)
            or not health_path.startswith("/")
            or len(health_path) > 256
            or "?" in health_path
            or "#" in health_path
            or any(character in health_path for character in "\x00\r\n")
        ):
            raise invalid("manifest HTTP routes are invalid")
        service = services_by_key[service_key]
        container_port = _positive_int(
            route.get("containerPort"),
            f"routes[{index}].containerPort",
            maximum=65535,
        )
        if (
            int(service.get("port") or 0) != container_port
            or "healthcheck" in service
            and str(service["healthcheck"].get("path") or "") != health_path
        ):
            raise invalid("manifest HTTP route does not match its service")
        hostnames.add(hostname)
        routed_services.add(service_key)
        routes.append(
            {
                "serviceKey": service_key,
                "kind": "http",
                "hostname": hostname,
                "containerPort": container_port,
                "exposure": expected_exposure,
                "healthPath": health_path,
            }
        )

    raw_volumes = manifest.get("volumes")
    volumes = list(validate_volume_prepare_body({"schemaVersion": SCHEMA_VERSION, "volumes": raw_volumes}))
    if any("existingRef" not in volume for volume in volumes):
        raise invalid("deployment volumes must be prepared before deploy")
    if any(
        mount["serviceKey"] not in roles
        for volume in volumes
        for mount in volume["mounts"]
    ):
        raise invalid("deployment volume mount references an unknown service")
    all_mounts = [
        (mount["serviceKey"], mount["mountPath"])
        for volume in volumes
        for mount in volume["mounts"]
    ]
    if len(all_mounts) != len(set(all_mounts)):
        raise invalid("deployment volume mounts are duplicated")

    raw_secret_refs = manifest.get("secretRefs")
    if not isinstance(raw_secret_refs, list) or len(raw_secret_refs) > 512:
        raise invalid("manifest secretRefs are invalid")
    secret_refs: list[dict[str, Any]] = []
    secret_keys: set[tuple[str, str]] = set()
    environment_versions: set[int] = set()
    for index, raw_secret in enumerate(raw_secret_refs):
        secret = _require_exact_keys(
            raw_secret,
            {"serviceKey", "name", "secretRef", "environmentVersion"},
            label=f"secretRefs[{index}]",
        )
        service_key = _require_key(secret.get("serviceKey"), f"secretRefs[{index}].serviceKey")
        name_value = secret.get("name")
        secret_ref = secret.get("secretRef")
        if (
            service_key not in roles
            or not isinstance(name_value, str)
            or _ENV.fullmatch(name_value) is None
            or not isinstance(secret_ref, str)
            or _SECRET_REF.fullmatch(secret_ref) is None
            or (service_key, name_value) in secret_keys
        ):
            raise invalid("manifest secretRefs are invalid")
        version = _non_negative_int(
            secret.get("environmentVersion"),
            f"secretRefs[{index}].environmentVersion",
        )
        environment_versions.add(version)
        secret_keys.add((service_key, name_value))
        secret_refs.append(
            {
                "serviceKey": service_key,
                "name": name_value,
                "secretRef": secret_ref,
                "environmentVersion": version,
            }
        )
    if len(environment_versions) > 1:
        raise invalid("manifest secretRefs must use one environment version")
    expected_secret_keys = {
        (service["key"], name_value)
        for service in services
        for name_value in service["environmentNames"]
    }
    if secret_keys != expected_secret_keys:
        raise invalid("manifest secretRefs do not match the service environment schema")

    supplied_digest = _require_digest(manifest.get("manifestDigest"), "manifestDigest")
    digest_value = {
        "schemaVersion": "lae.runtime-manifest/v1",
        "applicationId": binding.application_ref,
        "revisionId": binding.revision_ref,
        "name": name,
        "kind": kind,
        "region": region,
        "services": services,
        "routes": routes,
        "volumes": volumes,
        "environment": sorted(
            [
                {
                    "serviceKey": item["serviceKey"],
                    "name": item["name"],
                    "environmentVersion": item["environmentVersion"],
                }
                for item in secret_refs
            ],
            key=lambda item: (item["serviceKey"], item["name"]),
        ),
    }
    expected_digest = canonical_hash(digest_value)
    if not secrets.compare_digest(supplied_digest, expected_digest):
        raise invalid("manifestDigest does not match the bound manifest")

    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "name": name,
        "kind": kind,
        "region": region,
        "services": services,
        "routes": routes,
        "volumes": volumes,
        "secretRefs": secret_refs,
        "manifestDigest": supplied_digest,
    }
    if normalized_compose_digest is not None:
        result["normalizedComposeDigest"] = normalized_compose_digest
    return result


def validate_secret_issue_body(body: Any) -> dict[str, Any]:
    value = _require_exact_keys(
        body,
        {
            "schemaVersion",
            "serviceKey",
            "name",
            "plaintext",
            "environmentVersion",
            "ttlSeconds",
        },
        label="runtime secret request",
    )
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise invalid("runtime secret schemaVersion is invalid")
    name = value.get("name")
    plaintext = value.get("plaintext")
    if not isinstance(name, str) or _ENV.fullmatch(name) is None:
        raise invalid("runtime secret name is invalid")
    if (
        not isinstance(plaintext, str)
        or "\x00" in plaintext
        or len(plaintext.encode("utf-8")) > MAX_SECRET_VALUE_BYTES
    ):
        raise invalid("runtime secret value is invalid")
    ttl = _positive_int(value.get("ttlSeconds"), "ttlSeconds", maximum=300)
    if ttl < 5:
        raise invalid("ttlSeconds must be between 5 and 300")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "serviceKey": _require_key(value.get("serviceKey"), "serviceKey"),
        "name": name,
        "plaintext": plaintext,
        "environmentVersion": _non_negative_int(
            value.get("environmentVersion"), "environmentVersion"
        ),
        "ttlSeconds": ttl,
    }


def validate_lifecycle_body(action: str, body: Any) -> dict[str, Any]:
    """Validate the closed lifecycle mutation wire.

    Lifecycle calls deliberately accept no replacement manifest or arbitrary
    Nomad options. Resume and restart can therefore operate only on the
    deployment already bound in Luma's runtime state. Delete requires an
    explicit volume policy so a missing/default boolean can never erase data.
    """

    if action not in {"suspend", "resume", "restart", "rollback", "delete"}:
        raise invalid("runtime lifecycle action is invalid")
    if action == "delete" and (
        not isinstance(body, dict) or "volumePolicy" not in body
    ):
        raise invalid("runtime delete volumePolicy is required")
    required = (
        {"schemaVersion", "volumePolicy"}
        if action == "delete"
        else {"schemaVersion", "target"}
        if action == "rollback"
        else {"schemaVersion"}
    )
    value = _require_exact_keys(
        body,
        required,
        label=f"runtime {action} request",
    )
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise invalid(f"runtime {action} schemaVersion is invalid")
    result: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION}
    if action == "delete":
        policy = value.get("volumePolicy")
        if policy not in {"retain", "delete"}:
            raise invalid("runtime delete volumePolicy must be retain or delete")
        result["volumePolicy"] = policy
    elif action == "rollback":
        target = _require_exact_keys(
            value.get("target"),
            {
                "runtimeDeploymentRef",
                "operationRef",
                "revisionRef",
                "deploymentRef",
            },
            label="runtime rollback target",
        )
        result["target"] = {
            key: _require_reference(
                target.get(key), f"runtime rollback target {key}"
            )
            for key in (
                "runtimeDeploymentRef",
                "operationRef",
                "revisionRef",
                "deploymentRef",
            )
        }
    return result


@dataclass(slots=True)
class RuntimeSecretLease:
    secret_ref: str
    principal_ref: str
    binding: RuntimeBinding
    service_key: str
    name: str
    environment_version: int
    expires_at: float
    value: str = field(repr=False)

    def public_body(self) -> dict[str, Any]:
        return {
            "serviceKey": self.service_key,
            "name": self.name,
            "secretRef": self.secret_ref,
            "environmentVersion": self.environment_version,
            "expiresAt": datetime.fromtimestamp(self.expires_at, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


class RuntimeSecretLeaseManager:
    """In-memory, deployment-bound secret exchange.

    Plaintext and its idempotency fingerprint never enter control.json. The
    fingerprint is keyed with a process-local random key so low-entropy values
    cannot be recovered from an offline state or log dump.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[str, RuntimeSecretLease] = {}
        self._idempotency: dict[tuple[str, ...], tuple[str, str]] = {}
        self._hash_key = secrets.token_bytes(32)

    def clear(self) -> None:
        with self._lock:
            self._leases.clear()
            self._idempotency.clear()
            self._hash_key = secrets.token_bytes(32)

    def issue(
        self,
        *,
        principal_ref: str,
        binding: RuntimeBinding,
        request: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[RuntimeSecretLease, bool]:
        now = time.time()
        key = (
            principal_ref,
            binding.tenant_ref,
            binding.application_ref,
            binding.operation_ref,
            binding.revision_ref,
            binding.deployment_ref,
            normalize_idempotency_key(idempotency_key),
        )
        request_body = {
            "serviceKey": request["serviceKey"],
            "name": request["name"],
            "environmentVersion": request["environmentVersion"],
            "ttlSeconds": request["ttlSeconds"],
            "plaintextHmac": hmac.new(
                self._hash_key,
                str(request["plaintext"]).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        }
        request_hash = canonical_hash(request_body)
        with self._lock:
            self._prune(now)
            existing = self._idempotency.get(key)
            if existing is not None:
                if not secrets.compare_digest(existing[0], request_hash):
                    raise conflict()
                lease = self._leases.get(existing[1])
                if lease is not None and lease.expires_at > now:
                    return lease, True
                self._idempotency.pop(key, None)
            # ``token_urlsafe`` may begin with ``-`` or ``_`` while the public
            # runtime wire deliberately requires an alphanumeric first
            # character after ``lsec_``. Hex keeps issuance inside the closed
            # schema without a rare, data-dependent rejection at deploy time.
            secret_ref = "lsec_" + secrets.token_hex(24)
            lease = RuntimeSecretLease(
                secret_ref=secret_ref,
                principal_ref=principal_ref,
                binding=binding,
                service_key=str(request["serviceKey"]),
                name=str(request["name"]),
                environment_version=int(request["environmentVersion"]),
                expires_at=now + int(request["ttlSeconds"]),
                value=str(request["plaintext"]),
            )
            self._leases[secret_ref] = lease
            self._idempotency[key] = (request_hash, secret_ref)
            return lease, False

    def resolve_manifest(
        self,
        *,
        principal_ref: str,
        binding: RuntimeBinding,
        secret_refs: list[dict[str, Any]],
    ) -> dict[str, str]:
        now = time.time()
        resolved: dict[str, str] = {}
        with self._lock:
            self._prune(now)
            for item in secret_refs:
                ref = str(item.get("secretRef") or "")
                lease = self._leases.get(ref)
                if (
                    lease is None
                    or lease.expires_at <= now
                    or lease.principal_ref != principal_ref
                    or lease.binding != binding
                    or lease.service_key != item.get("serviceKey")
                    or lease.name != item.get("name")
                    or lease.environment_version != item.get("environmentVersion")
                ):
                    raise invalid("runtime secret reference is expired or not bound")
                resolved[ref] = lease.value
        return resolved

    def _prune(self, now: float) -> None:
        expired = {
            ref for ref, lease in self._leases.items() if lease.expires_at <= now
        }
        for ref in expired:
            self._leases.pop(ref, None)
        if expired:
            for key, value in list(self._idempotency.items()):
                if value[1] in expired:
                    self._idempotency.pop(key, None)


RUNTIME_SECRETS = RuntimeSecretLeaseManager()


__all__ = [
    "MAX_RUNTIME_REQUEST_BYTES",
    "RUNTIME_AUDIENCE",
    "RUNTIME_SECRETS",
    "SCHEMA_VERSION",
    "SCOPE_DEPLOYMENTS_READ",
    "SCOPE_DEPLOYMENTS_WRITE",
    "SCOPE_LOGS_READ",
    "SCOPE_METRICS_READ",
    "SCOPE_SECRETS_ISSUE",
    "SCOPE_VOLUMES_PREPARE",
    "LumaRuntimeError",
    "RuntimeBinding",
    "RuntimeSecretLeaseManager",
    "canonical_hash",
    "conflict",
    "forbidden",
    "invalid",
    "normalize_idempotency_key",
    "not_found",
    "unauthorized",
    "unavailable",
    "validate_deploy_body",
    "validate_secret_issue_body",
    "validate_volume_prepare_body",
]
