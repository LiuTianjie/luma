from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Protocol

from lae_contracts import is_safe_external_image_reference, validate_instance
from lae_store import (
    DeploymentPlanInvalid,
    DeploymentPlanUnavailable,
    PreparedDeploymentPlan,
    PreparedEnvironmentVariable,
    PreparedHttpRoute,
    PreparedService,
    PreparedVolume,
    PrivateObjectIntegrityError,
    PrivateObjectStoreUnavailable,
    S3PrivateObjectConfig,
    S3PrivateObjectStore,
    StoredDeploymentPlanArtifact,
)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVIRONMENT = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SERVICE_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_IMAGE_REF_MAX_BYTES = 1024
_PLAN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class RuntimeImageRequirement:
    service_key: str
    source: str
    build_key: str | None = None
    external_ref: str | None = None

    def __post_init__(self) -> None:
        if not _SERVICE_KEY.fullmatch(self.service_key):
            raise DeploymentPlanInvalid("runtime image service binding is invalid")
        if self.source == "build":
            if (
                not isinstance(self.build_key, str)
                or not _SERVICE_KEY.fullmatch(self.build_key)
                or self.external_ref is not None
            ):
                raise DeploymentPlanInvalid("runtime build image binding is invalid")
        elif self.source == "external":
            if (
                self.build_key is not None
                or not isinstance(self.external_ref, str)
                or not 1 <= len(self.external_ref.encode("utf-8")) <= _IMAGE_REF_MAX_BYTES
                or not is_safe_external_image_reference(self.external_ref)
            ):
                raise DeploymentPlanInvalid(
                    "runtime external image binding is invalid"
                )
        else:
            raise DeploymentPlanInvalid("runtime image source is unsupported")


class VerifiedBuildArtifactImageResolver(Protocol):
    """Resolve service images only from verified build/artifact catalog facts.

    An implementation must bind the analysis, source revision and source digest
    to the signed build plan plus terminal build result. It may not perform a
    fresh mutable-tag lookup or accept image coordinates from an API request.
    The result maps every service key to exactly one ``sha256:...`` digest.
    """

    async def resolve_runtime_images(
        self,
        artifact: StoredDeploymentPlanArtifact,
        *,
        source_digest: str,
        requirements: tuple[RuntimeImageRequirement, ...],
    ) -> Mapping[str, str]: ...


class UnconfiguredBuildArtifactImageResolver:
    async def resolve_runtime_images(
        self,
        artifact: StoredDeploymentPlanArtifact,
        *,
        source_digest: str,
        requirements: tuple[RuntimeImageRequirement, ...],
    ) -> Mapping[str, str]:
        del artifact, source_digest, requirements
        raise DeploymentPlanUnavailable(
            "verified runtime image resolver is not configured"
        )


@dataclass(frozen=True, slots=True)
class DeploymentConfigurationSchema:
    source_revision_id: str
    kind: str
    service_keys: tuple[str, ...]
    environment: tuple[PreparedEnvironmentVariable, ...]
    environment_schema_digest: str
    services: tuple[dict[str, object], ...] = ()
    routes: tuple[dict[str, object], ...] = ()
    volumes: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()

    def public_body(self) -> dict[str, object]:
        return {
            "sourceRevisionId": self.source_revision_id,
            "kind": self.kind,
            "serviceKeys": list(self.service_keys),
            "environmentSchemaDigest": self.environment_schema_digest,
            "environmentScopeMode": "service",
            "services": [dict(service) for service in self.services],
            "routes": [dict(route) for route in self.routes],
            "volumes": [dict(volume) for volume in self.volumes],
            "warnings": list(self.warnings),
            "environment": [
                {
                    "name": variable.name,
                    "serviceKeys": list(variable.service_keys),
                    "references": [
                        f"{service_key}:{variable.name}"
                        for service_key in variable.service_keys
                    ],
                    "required": variable.required,
                    "sensitive": variable.sensitive,
                }
                for variable in self.environment
            ],
        }


class S3DeploymentPlanResolver:
    """Stream and prepare one verified LAE deployment plan from private S3."""

    def __init__(
        self,
        object_store: S3PrivateObjectStore,
        image_resolver: VerifiedBuildArtifactImageResolver | None = None,
        *,
        timeout_seconds: float = _PLAN_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= 120
        ):
            raise ValueError("deployment plan timeout is invalid")
        self._object_store = object_store
        # Retained as a source-compatible argument for older embedders. Image
        # outputs do not exist until the deployment worker runs and must never
        # be fabricated during admission.
        self._legacy_image_resolver = image_resolver
        self._timeout_seconds = float(timeout_seconds)

    async def resolve(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> PreparedDeploymentPlan:
        plan = await self._load_verified_plan(artifact)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._prepare(artifact, plan)
        except DeploymentPlanInvalid:
            raise
        except DeploymentPlanUnavailable:
            raise
        except TimeoutError as exc:
            raise DeploymentPlanUnavailable(
                "runtime image resolution timed out"
            ) from exc

    async def resolve_configuration(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> DeploymentConfigurationSchema:
        """Return only secret-free config schema, without resolving images."""

        plan = await self._load_verified_plan(artifact)
        _require_plan_semantics(plan, artifact)
        services = _list_of_mappings(plan, "services")
        routes = _list_of_mappings(plan, "routes")
        volumes = _list_of_mappings(plan, "volumes")
        environment = _prepared_runtime_environment(
            _list_of_mappings(plan, "environment")
        )
        normalized = _normalized_environment(environment)
        return DeploymentConfigurationSchema(
            source_revision_id=artifact.source_revision_id,
            kind=_required_string(plan, "kind"),
            service_keys=tuple(
                _required_string(service, "key") for service in services
            ),
            environment=environment,
            environment_schema_digest=_canonical_digest(normalized),
            services=tuple(_public_service_summary(service) for service in services),
            routes=tuple(_public_route_summary(route) for route in routes),
            volumes=tuple(_public_volume_summary(volume) for volume in volumes),
            warnings=tuple(_string_list(plan, "warnings")),
        )

    async def _load_verified_plan(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> dict[str, object]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                download = await self._object_store.get_stream(
                    artifact.storage_key,
                    max_bytes=artifact.size_bytes,
                )
                metadata = download.metadata
                if (
                    metadata.key != artifact.storage_key
                    or metadata.media_type != artifact.media_type
                    or metadata.size_bytes != artifact.size_bytes
                    or not hmac.compare_digest(metadata.digest, artifact.digest)
                ):
                    raise DeploymentPlanInvalid(
                        "deployment plan object metadata changed"
                    )
                raw = bytearray()
                digest = hashlib.sha256()
                async for chunk in download.chunks:
                    if not isinstance(chunk, bytes):
                        raise DeploymentPlanInvalid(
                            "deployment plan object stream is invalid"
                        )
                    raw.extend(chunk)
                    if len(raw) > artifact.size_bytes:
                        raise DeploymentPlanInvalid(
                            "deployment plan exceeds its stored descriptor"
                        )
                    digest.update(chunk)
                if len(raw) != artifact.size_bytes or not hmac.compare_digest(
                    f"sha256:{digest.hexdigest()}", artifact.digest
                ):
                    raise DeploymentPlanInvalid(
                        "deployment plan object integrity check failed"
                    )
                return _decode_canonical_plan(bytes(raw))
        except DeploymentPlanInvalid:
            raise
        except PrivateObjectIntegrityError as exc:
            raise DeploymentPlanInvalid(
                "deployment plan object integrity check failed"
            ) from exc
        except (
            PrivateObjectStoreUnavailable,
            TimeoutError,
            OSError,
        ) as exc:
            raise DeploymentPlanUnavailable(
                "deployment plan object store is unavailable"
            ) from exc

    async def _prepare(
        self,
        artifact: StoredDeploymentPlanArtifact,
        plan: dict[str, object],
    ) -> PreparedDeploymentPlan:
        _require_plan_semantics(plan, artifact)
        services = _list_of_mappings(plan, "services")
        routes = _list_of_mappings(plan, "routes")
        volumes = _list_of_mappings(plan, "volumes")
        environment = _list_of_mappings(plan, "environment")
        requirements = tuple(_image_requirement(service) for service in services)
        image_requirements = {
            requirement.service_key: {
                "source": requirement.source,
                **(
                    {"buildKey": requirement.build_key}
                    if requirement.build_key is not None
                    else {"externalRef": requirement.external_ref}
                ),
            }
            for requirement in requirements
        }

        prepared_services = tuple(
            PreparedService(
                service_key=_required_string(service, "key"),
                role=_required_string(service, "role"),
            )
            for service in services
        )
        prepared_routes = tuple(
            PreparedHttpRoute(
                service_key=_required_string(route, "serviceKey"),
                container_port=_required_int(route, "containerPort"),
                is_primary=_required_bool(route, "primary"),
            )
            for route in routes
        )
        prepared_volumes = tuple(
            PreparedVolume(
                volume_key=_required_string(volume, "key"),
                requested_bytes=_required_int(volume, "requestedBytes"),
                backup_policy=_normalize_backup_policy(
                    _required_string(volume, "backupPolicy")
                ),
                delete_policy=_normalize_delete_policy(
                    _required_string(volume, "deletePolicy")
                ),
            )
            for volume in volumes
        )
        prepared_environment = _prepared_runtime_environment(environment)
        normalized_environment = _normalized_environment(prepared_environment)
        kind = _required_string(plan, "kind")
        normalized_compose = (
            _normalized_compose_manifest(
                services=services,
                routes=routes,
                volumes=volumes,
                image_requirements=image_requirements,
                environment=normalized_environment,
            )
            if kind == "compose"
            else None
        )
        return PreparedDeploymentPlan(
            source_revision_id=artifact.source_revision_id,
            kind=kind,
            services=prepared_services,
            routes=prepared_routes,
            volumes=prepared_volumes,
            environment=prepared_environment,
            luma_manifest_digest=None,
            environment_schema_digest=_canonical_digest(normalized_environment),
            normalized_compose_digest=(
                None
                if normalized_compose is None
                else _canonical_digest(normalized_compose)
            ),
        )


def deployment_plan_resolver_from_env(
    *,
    image_resolver: VerifiedBuildArtifactImageResolver | None = None,
    environ: Mapping[str, str] | None = None,
) -> S3DeploymentPlanResolver:
    values = os.environ if environ is None else environ
    environment = values.get("LAE_ENVIRONMENT", "development").strip().lower()
    production = environment in {"prod", "production"}
    endpoint = _required_env(values, "LAE_ARTIFACT_S3_ENDPOINT")
    allowed_hosts = tuple(
        item.strip()
        for item in _required_env(
            values, "LAE_ARTIFACT_S3_ALLOWED_HOSTS"
        ).split(",")
        if item.strip()
    )
    config = S3PrivateObjectConfig(
        endpoint=endpoint,
        bucket=_required_env(values, "LAE_ARTIFACT_S3_BUCKET"),
        region=_required_env(values, "LAE_ARTIFACT_S3_REGION"),
        access_key=_required_env(values, "LAE_ARTIFACT_S3_ACCESS_KEY"),
        secret_key=_required_env(values, "LAE_ARTIFACT_S3_SECRET_KEY"),
        allowed_hosts=allowed_hosts,
        path_style=_boolean(values, "LAE_ARTIFACT_S3_PATH_STYLE", True),
        production=production,
        timeout_seconds=_float(values, "LAE_ARTIFACT_S3_TIMEOUT_SECONDS", 20.0),
    )
    return S3DeploymentPlanResolver(
        S3PrivateObjectStore(config),
        image_resolver,
        timeout_seconds=_float(
            values, "LAE_DEPLOYMENT_PLAN_TIMEOUT_SECONDS", _PLAN_TIMEOUT_SECONDS
        ),
    )


def _decode_canonical_plan(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise DeploymentPlanInvalid("deployment plan JSON is invalid") from exc
    if not isinstance(value, dict):
        raise DeploymentPlanInvalid("deployment plan JSON must be an object")
    canonical = _canonical_bytes(value)
    if raw != canonical:
        raise DeploymentPlanInvalid("deployment plan JSON is not canonical")
    issues = validate_instance("deployment-plan.v1.schema.json", value)
    if issues:
        raise DeploymentPlanInvalid("deployment plan schema validation failed")
    return value


def _require_plan_semantics(
    plan: Mapping[str, object], artifact: StoredDeploymentPlanArtifact
) -> None:
    # Analyzer v1 currently emits a snapshot-derived sourceRevisionId rather
    # than LAE's durable database ID. Never trust or substitute that field. The
    # catalog join supplies the authoritative source revision, while this
    # independently binds the plan bytes to Analysis.source_snapshot_digest.
    if (
        artifact.source_snapshot_digest is None
        or _required_digest(plan, "sourceDigest")
        != artifact.source_snapshot_digest
    ):
        raise DeploymentPlanInvalid("deployment plan source snapshot is not bound")
    blockers = _string_list(plan, "blockers")
    policy = _mapping(plan, "policy")
    if blockers or _required_string(policy, "decision") == "deny":
        raise DeploymentPlanInvalid("deployment plan policy does not permit deploy")
    if _required_string(plan, "kind") not in {"service", "compose"}:
        raise DeploymentPlanInvalid("deployment plan kind is unsupported")

    services = _list_of_mappings(plan, "services")
    service_keys = [_required_string(service, "key") for service in services]
    if len(service_keys) != len(set(service_keys)):
        raise DeploymentPlanInvalid("deployment services are not unique")
    known_services = set(service_keys)
    environment = _list_of_mappings(plan, "environment")
    routes = _list_of_mappings(plan, "routes")
    volumes = _list_of_mappings(plan, "volumes")
    warnings = _string_list(plan, "warnings")
    blockers = _string_list(plan, "blockers")
    if (
        not 1 <= len(services) <= 64
        or len(routes) > 64
        or len(volumes) > 64
        or len(environment) > 512
        or len(warnings) > 256
        or len(blockers) > 256
        or any(len(value) > 512 for value in (*warnings, *blockers))
    ):
        raise DeploymentPlanInvalid("deployment plan exceeds platform bounds")
    if _required_string(plan, "kind") == "service" and len(services) != 1:
        raise DeploymentPlanInvalid("service plan must contain one service")
    environment_bindings = {
        (service_key, _required_string(variable, "name"))
        for variable in environment
        for service_key in _string_list(variable, "services")
    }
    for service in services:
        service_key = _required_string(service, "key")
        role = _required_string(service, "role")
        if role not in {"http", "worker", "internal", "datastore"}:
            raise DeploymentPlanInvalid("deployment service role is unsupported")
        dependencies = _string_list(service, "dependencies")
        if (
            service_key in dependencies
            or any(dependency not in known_services for dependency in dependencies)
        ):
            raise DeploymentPlanInvalid("deployment dependency graph is invalid")
        for name in _string_list(service, "environmentNames"):
            if (service_key, name) not in environment_bindings:
                raise DeploymentPlanInvalid(
                    "deployment environment schema is incomplete"
                )
        command = service.get("command")
        if command is not None:
            _bounded_text(command, "service command", max_length=4096)
        resources = _mapping(service, "resources")
        _normalized_cpu(_required_string(resources, "cpu"))
        memory = _required_int(resources, "memoryMiB")
        if not 64 <= memory <= 1_048_576:
            raise DeploymentPlanInvalid("service memory is outside platform bounds")
        port = service.get("port")
        if port is not None and not 1 <= _integer_value(port, "service port") <= 65535:
            raise DeploymentPlanInvalid("service port is invalid")
        healthcheck = service.get("healthcheck")
        if healthcheck is not None:
            health = _mapping_value(healthcheck, "healthcheck")
            _safe_http_path(_required_string(health, "path"))
    _require_acyclic_dependencies(services)

    if routes and sum(_required_bool(route, "primary") for route in routes) != 1:
        raise DeploymentPlanInvalid("HTTP routes require one primary route")
    service_by_key = {
        _required_string(service, "key"): service for service in services
    }
    route_keys: set[tuple[str, int]] = set()
    for route in routes:
        service_key = _required_string(route, "serviceKey")
        service = service_by_key.get(service_key)
        container_port = _required_int(route, "containerPort")
        if (
            service is None
            or _required_string(service, "role") != "http"
            or service.get("port") != container_port
            or (service_key, container_port) in route_keys
        ):
            raise DeploymentPlanInvalid("HTTP route binding is invalid")
        route_keys.add((service_key, container_port))
        health_path = _safe_http_path(_required_string(route, "healthPath"))
        service_health = service.get("healthcheck")
        if service_health is not None and _required_string(
            _mapping_value(service_health, "healthcheck"), "path"
        ) != health_path:
            raise DeploymentPlanInvalid("HTTP route health binding is invalid")

    volume_keys: set[str] = set()
    volume_mounts: set[tuple[str, str]] = set()
    for volume in volumes:
        key = _required_string(volume, "key")
        if key in volume_keys:
            raise DeploymentPlanInvalid("deployment volumes are not unique")
        volume_keys.add(key)
        if any(
            service_key not in known_services
            for service_key in _string_list(volume, "serviceKeys")
        ):
            raise DeploymentPlanInvalid("volume service binding is invalid")
        mount_path = _safe_absolute_path(_required_string(volume, "mountPath"))
        for service_key in _string_list(volume, "serviceKeys"):
            mount = (service_key, mount_path)
            if mount in volume_mounts:
                raise DeploymentPlanInvalid("volume mount binding is duplicated")
            volume_mounts.add(mount)
        _normalize_backup_policy(_required_string(volume, "backupPolicy"))
        _normalize_delete_policy(_required_string(volume, "deletePolicy"))

    seen_environment: set[tuple[str, str]] = set()
    for variable in environment:
        name = _required_string(variable, "name")
        if not _ENVIRONMENT.fullmatch(name):
            raise DeploymentPlanInvalid("environment variable name is invalid")
        sensitive = _required_bool(variable, "sensitive")
        public = _required_bool(variable, "public")
        if sensitive and public:
            raise DeploymentPlanInvalid(
                "sensitive environment variable cannot be public"
            )
        for service_key in _string_list(variable, "services"):
            binding = (service_key, name)
            if service_key not in known_services or binding in seen_environment:
                raise DeploymentPlanInvalid("environment service binding is invalid")
            seen_environment.add(binding)


def _public_service_summary(service: Mapping[str, object]) -> dict[str, object]:
    """Return the reviewed, secret-free service facts shown before deploy."""

    resources = _mapping(service, "resources")
    image = _mapping(service, "image")
    healthcheck = service.get("healthcheck")
    return {
        "key": _required_string(service, "key"),
        "role": _required_string(service, "role"),
        "dependencies": list(_string_list(service, "dependencies")),
        "resources": {
            "cpu": _required_string(resources, "cpu"),
            "memoryMiB": _required_int(resources, "memoryMiB"),
        },
        "port": service.get("port"),
        "imageSource": _required_string(image, "source"),
        "healthPath": (
            _required_string(_mapping_value(healthcheck, "healthcheck"), "path")
            if healthcheck is not None
            else None
        ),
    }


def _public_route_summary(route: Mapping[str, object]) -> dict[str, object]:
    return {
        "serviceKey": _required_string(route, "serviceKey"),
        "containerPort": _required_int(route, "containerPort"),
        "healthPath": _required_string(route, "healthPath"),
        "primary": _required_bool(route, "primary"),
    }


def _public_volume_summary(volume: Mapping[str, object]) -> dict[str, object]:
    return {
        "key": _required_string(volume, "key"),
        "serviceKeys": list(_string_list(volume, "serviceKeys")),
        "mountPath": _required_string(volume, "mountPath"),
        "backupPolicy": _required_string(volume, "backupPolicy"),
        "deletePolicy": _required_string(volume, "deletePolicy"),
    }


def _image_requirement(service: Mapping[str, object]) -> RuntimeImageRequirement:
    image = _mapping(service, "image")
    source = _required_string(image, "source")
    return RuntimeImageRequirement(
        service_key=_required_string(service, "key"),
        source=source,
        build_key=(
            _required_string(image, "buildKey") if source == "build" else None
        ),
        external_ref=(
            _required_string(image, "ref") if source == "external" else None
        ),
    )


def _require_acyclic_dependencies(
    services: list[Mapping[str, object]],
) -> None:
    graph = {
        _required_string(service, "key"): tuple(
            _string_list(service, "dependencies")
        )
        for service in services
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(service_key: str) -> None:
        if service_key in visiting:
            raise DeploymentPlanInvalid("deployment dependency graph contains a cycle")
        if service_key in visited:
            return
        visiting.add(service_key)
        for dependency in graph[service_key]:
            visit(dependency)
        visiting.remove(service_key)
        visited.add(service_key)

    for service_key in graph:
        visit(service_key)


def _validated_image_digests(
    resolved: Mapping[str, str],
    requirements: tuple[RuntimeImageRequirement, ...],
) -> dict[str, str]:
    if not isinstance(resolved, Mapping):
        raise DeploymentPlanInvalid("runtime image resolver returned invalid data")
    expected = {requirement.service_key for requirement in requirements}
    if set(resolved) != expected:
        raise DeploymentPlanInvalid("runtime image resolution is incomplete")
    result: dict[str, str] = {}
    for key, value in resolved.items():
        if not isinstance(key, str) or not isinstance(value, str) or not _DIGEST.fullmatch(
            value
        ):
            raise DeploymentPlanInvalid("runtime image digest is not immutable")
        result[key] = value
    return result


def _prepared_runtime_environment(
    environment: list[Mapping[str, object]],
) -> tuple[PreparedEnvironmentVariable, ...]:
    return tuple(
        PreparedEnvironmentVariable(
            name=_required_string(variable, "name"),
            service_keys=tuple(_string_list(variable, "services")),
            required=_required_bool(variable, "required"),
            sensitive=_required_bool(variable, "sensitive"),
        )
        for variable in environment
        if _required_string(variable, "scope") == "runtime"
    )


def _normalized_environment(
    environment: tuple[PreparedEnvironmentVariable, ...],
) -> list[dict[str, object]]:
    return [
        {
            "name": variable.name,
            "required": variable.required,
            "sensitive": variable.sensitive,
            "serviceKeys": list(variable.service_keys),
        }
        for variable in environment
    ]


def _normalized_luma_manifest(
    plan: Mapping[str, object],
    *,
    services: list[Mapping[str, object]],
    routes: list[Mapping[str, object]],
    volumes: list[Mapping[str, object]],
    image_requirements: Mapping[str, Mapping[str, object]],
    environment: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schemaVersion": "lae.normalized-luma-manifest/v1",
        "kind": _required_string(plan, "kind"),
        "services": [
            {
                "key": _required_string(service, "key"),
                "role": _required_string(service, "role"),
                "image": image_requirements[_required_string(service, "key")],
                "command": service.get("command"),
                "port": service.get("port"),
                "healthcheck": service.get("healthcheck"),
                "dependencies": _string_list(service, "dependencies"),
                "resources": service.get("resources"),
            }
            for service in services
        ],
        "routes": [
            {
                "serviceKey": _required_string(route, "serviceKey"),
                "containerPort": _required_int(route, "containerPort"),
                "healthPath": _required_string(route, "healthPath"),
                "primary": _required_bool(route, "primary"),
            }
            for route in routes
        ],
        "volumes": [
            {
                "key": _required_string(volume, "key"),
                "serviceKeys": _string_list(volume, "serviceKeys"),
                "mountPath": _required_string(volume, "mountPath"),
                "requestedBytes": _required_int(volume, "requestedBytes"),
                "accessMode": _required_string(volume, "accessMode"),
                "backupPolicy": _normalize_backup_policy(
                    _required_string(volume, "backupPolicy")
                ),
                "deletePolicy": _normalize_delete_policy(
                    _required_string(volume, "deletePolicy")
                ),
            }
            for volume in volumes
        ],
        "environment": environment,
    }


def _normalized_compose_manifest(
    *,
    services: list[Mapping[str, object]],
    routes: list[Mapping[str, object]],
    volumes: list[Mapping[str, object]],
    image_requirements: Mapping[str, Mapping[str, object]],
    environment: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schemaVersion": "lae.normalized-compose/v1",
        "services": [
            {
                "key": _required_string(service, "key"),
                "image": image_requirements[_required_string(service, "key")],
                "command": service.get("command"),
                "port": service.get("port"),
                "dependencies": _string_list(service, "dependencies"),
            }
            for service in services
        ],
        "routes": [dict(route) for route in routes],
        "volumes": [dict(volume) for volume in volumes],
        "environment": environment,
    }


def _normalize_backup_policy(value: str) -> str:
    # Analyzer v1 emits plan-default. LAE Lite currently has no automatic
    # backup entitlement, so the server-owned v1 policy maps that marker to
    # the durable catalog's explicit "none" value.
    normalized = {"plan-default": "none", "none": "none", "manual": "manual", "scheduled": "scheduled"}.get(
        value
    )
    if normalized is None:
        raise DeploymentPlanInvalid("volume backup policy is unsupported")
    return normalized


def _normalize_delete_policy(value: str) -> str:
    normalized = {"retain": "retain", "delete-after-grace": "delete"}.get(value)
    if normalized is None:
        raise DeploymentPlanInvalid("volume delete policy is unsupported")
    return normalized


def _canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeploymentPlanInvalid("deployment plan cannot be canonicalized") from exc


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _mapping_value(value.get(key), key)


def _mapping_value(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DeploymentPlanInvalid(f"{field} is invalid")
    return value


def _list_of_mappings(
    value: Mapping[str, object], key: str
) -> list[Mapping[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise DeploymentPlanInvalid(f"{key} is invalid")
    return list(items)


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise DeploymentPlanInvalid(f"{key} is invalid")
    return item


def _required_digest(value: Mapping[str, object], key: str) -> str:
    item = _required_string(value, key)
    if not _DIGEST.fullmatch(item):
        raise DeploymentPlanInvalid(f"{key} is invalid")
    return item


def _required_int(value: Mapping[str, object], key: str) -> int:
    return _integer_value(value.get(key), key)


def _integer_value(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeploymentPlanInvalid(f"{field} is invalid")
    return value


def _required_bool(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise DeploymentPlanInvalid(f"{key} is invalid")
    return item


def _string_list(value: Mapping[str, object], key: str) -> list[str]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise DeploymentPlanInvalid(f"{key} is invalid")
    return list(items)


def _bounded_text(value: object, field: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise DeploymentPlanInvalid(f"{field} is invalid")
    return value


def _safe_http_path(value: str) -> str:
    if (
        not value.startswith("/")
        or len(value) > 512
        or "?" in value
        or "#" in value
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise DeploymentPlanInvalid("HTTP path is invalid")
    return value


def _safe_absolute_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.startswith("/")
        or len(value) > 1024
        or ".." in path.parts
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise DeploymentPlanInvalid("volume mount path is invalid")
    return value


def _normalized_cpu(value: str) -> str:
    try:
        cpu = Decimal(value)
    except InvalidOperation as exc:
        raise DeploymentPlanInvalid("service CPU is invalid") from exc
    if not Decimal("0.01") <= cpu <= Decimal("64"):
        raise DeploymentPlanInvalid("service CPU is outside platform bounds")
    return format(cpu.normalize(), "f")


def _required_env(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _boolean(
    values: Mapping[str, str], key: str, default: bool
) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _float(values: Mapping[str, str], key: str, default: float) -> float:
    raw = values.get(key)
    return default if raw is None else float(raw)


__all__ = [
    "DeploymentConfigurationSchema",
    "RuntimeImageRequirement",
    "S3DeploymentPlanResolver",
    "UnconfiguredBuildArtifactImageResolver",
    "VerifiedBuildArtifactImageResolver",
    "deployment_plan_resolver_from_env",
]
