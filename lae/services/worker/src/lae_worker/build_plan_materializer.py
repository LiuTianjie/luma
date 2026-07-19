from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from lae_contracts import validate_instance
from lae_store import (
    PrivateObjectIntegrityError,
    PrivateObjectStoreUnavailable,
    S3PrivateObjectStore,
    StoredDeploymentPlanArtifact,
)

from .deployment import DeploymentContextInvalid
from .deployment_postgres import (
    StoredBuildPlanArtifact,
    TrustedBuildPlan,
    TrustedBuildPlanMaterializer,
    TrustedBuildPlanUnavailable,
    TrustedRuntimeRoute,
    TrustedRuntimeService,
    TrustedRuntimeVolume,
)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^lae-plan-[A-Za-z0-9_-]+$")
_IMAGE_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MAX_BUILD_PLAN_BYTES = 16 * 1024 * 1024
_BUILD_PLAN_MEDIA_TYPE = "application/vnd.lae.build-plan-candidate+json"


class BuildPlanIntegrityError(DeploymentContextInvalid):
    code = "LAE_BUILD_PLAN_INTEGRITY_FAILED"
    public_message = "The stored build plan failed integrity validation."


@dataclass(frozen=True, slots=True)
class BuildCredentialLeaseBinding:
    """Immutable, secret-free binding for the opaque build capability id."""

    tenant_ref: str
    application_ref: str
    operation_ref: str
    revision_ref: str
    source_snapshot_id: str
    source_snapshot_digest: str
    build_plan_digest: str


class BuildCredentialLeaseIssuer(Protocol):
    async def issue(self, binding: BuildCredentialLeaseBinding) -> str: ...


@dataclass(frozen=True, slots=True)
class HmacBuildCredentialLeaseIssuer:
    """Issue a deterministic, task-request-bound opaque lease identifier.

    BuildPlan v1 does not carry source credentials: Luma builds the immutable
    snapshot it already owns and derives an anonymous tenant registry lease.
    The field is nevertheless a required capability binding in Builder Task
    v1. This issuer makes it unguessable and binds it to all immutable LAE
    facts. It is not a reusable Git, registry, or runtime secret.
    """

    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, bytes) or len(self.key) < 32:
            raise ValueError("build credential lease HMAC key must be at least 256 bits")

    async def issue(self, binding: BuildCredentialLeaseBinding) -> str:
        payload = _canonical_bytes(
            {
                "schemaVersion": "lae.build-credential-lease-binding/v1",
                "tenantRef": binding.tenant_ref,
                "applicationRef": binding.application_ref,
                "operationRef": binding.operation_ref,
                "revisionRef": binding.revision_ref,
                "sourceSnapshotId": binding.source_snapshot_id,
                "sourceSnapshotDigest": binding.source_snapshot_digest,
                "buildPlanDigest": binding.build_plan_digest,
            }
        )
        digest = hmac.new(self.key, payload, hashlib.sha256).digest()
        return "cl_" + _base64url(digest)


class S3TrustedBuildPlanMaterializer(TrustedBuildPlanMaterializer):
    """Verify, transform, sign and bind one stored BuildPlan candidate."""

    def __init__(
        self,
        object_store: S3PrivateObjectStore,
        *,
        signing_key_id: str,
        signing_key: bytes,
        lease_issuer: BuildCredentialLeaseIssuer,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(signing_key_id, str) or _KEY_ID.fullmatch(signing_key_id) is None:
            raise ValueError("build plan signing key id must start with lae-plan-")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("build plan signing HMAC key must be at least 256 bits")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= 120
        ):
            raise ValueError("build plan materialization timeout is invalid")
        self._object_store = object_store
        self._signing_key_id = signing_key_id
        self._signing_key = signing_key
        self._lease_issuer = lease_issuer
        self._timeout_seconds = float(timeout_seconds)

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
    ) -> TrustedBuildPlan:
        _validate_descriptor(
            artifact,
            tenant_ref=tenant_ref,
            source_snapshot_digest=source_snapshot_digest,
        )
        _validate_deployment_descriptor(
            deployment_artifact,
            tenant_ref=tenant_ref,
            source_snapshot_digest=source_snapshot_digest,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                candidate = await self._read_verified_candidate(artifact)
                _validate_candidate_binding(
                    candidate,
                    source_snapshot_digest=source_snapshot_digest,
                    resolved_commit=resolved_commit,
                    policy_version=policy_version,
                )
                deployment_plan = await self._read_verified_deployment_plan(
                    deployment_artifact
                )
                kind, services, routes, volumes, service_build_keys = (
                    _trusted_runtime_topology(
                        deployment_plan,
                        candidate=candidate,
                        source_snapshot_digest=source_snapshot_digest,
                        policy_version=policy_version,
                    )
                )
                signed = _signed_plan(
                    candidate,
                    tenant_ref=tenant_ref,
                    application_ref=application_ref,
                    source_snapshot_id=source_snapshot_id,
                    key_id=self._signing_key_id,
                    signing_key=self._signing_key,
                )
                lease_id = await self._lease_issuer.issue(
                    BuildCredentialLeaseBinding(
                        tenant_ref=tenant_ref,
                        application_ref=application_ref,
                        operation_ref=operation_ref,
                        revision_ref=revision_ref,
                        source_snapshot_id=source_snapshot_id,
                        source_snapshot_digest=source_snapshot_digest,
                        build_plan_digest=artifact.digest,
                    )
                )
                if (
                    not isinstance(lease_id, str)
                    or not lease_id.startswith("cl_")
                    or not 11 <= len(lease_id) <= 128
                    or any(character in lease_id for character in "\x00\r\n")
                ):
                    raise BuildPlanIntegrityError()
                return TrustedBuildPlan(
                    signed_build_plan=signed,
                    credential_lease_id=lease_id,
                    service_build_keys=service_build_keys,
                    kind=kind,
                    services=services,
                    routes=routes,
                    volumes=volumes,
                )
        except BuildPlanIntegrityError:
            raise
        except PrivateObjectIntegrityError as exc:
            raise BuildPlanIntegrityError() from exc
        except (
            PrivateObjectStoreUnavailable,
            TimeoutError,
            OSError,
        ) as exc:
            raise TrustedBuildPlanUnavailable() from exc

    async def _read_verified_candidate(
        self, artifact: StoredBuildPlanArtifact
    ) -> dict[str, object]:
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
            raise BuildPlanIntegrityError()
        raw = bytearray()
        hasher = hashlib.sha256()
        async for chunk in download.chunks:
            if not isinstance(chunk, bytes):
                raise BuildPlanIntegrityError()
            raw.extend(chunk)
            if len(raw) > artifact.size_bytes:
                raise BuildPlanIntegrityError()
            hasher.update(chunk)
        if len(raw) != artifact.size_bytes or not hmac.compare_digest(
            "sha256:" + hasher.hexdigest(), artifact.digest
        ):
            raise BuildPlanIntegrityError()
        return _decode_canonical_candidate(bytes(raw))

    async def _read_verified_deployment_plan(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> dict[str, object]:
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
            raise BuildPlanIntegrityError()
        raw = bytearray()
        hasher = hashlib.sha256()
        async for chunk in download.chunks:
            if not isinstance(chunk, bytes):
                raise BuildPlanIntegrityError()
            raw.extend(chunk)
            if len(raw) > artifact.size_bytes:
                raise BuildPlanIntegrityError()
            hasher.update(chunk)
        if len(raw) != artifact.size_bytes or not hmac.compare_digest(
            "sha256:" + hasher.hexdigest(), artifact.digest
        ):
            raise BuildPlanIntegrityError()
        return _decode_canonical_artifact(
            bytes(raw), schema="deployment-plan.v1.schema.json"
        )


def _validate_descriptor(
    artifact: StoredBuildPlanArtifact,
    *,
    tenant_ref: str,
    source_snapshot_digest: str,
) -> None:
    if (
        not isinstance(artifact.artifact_id, str)
        or not artifact.artifact_id.startswith("art_")
        or not _DIGEST.fullmatch(artifact.digest)
        or artifact.media_type != _BUILD_PLAN_MEDIA_TYPE
        or isinstance(artifact.size_bytes, bool)
        or not isinstance(artifact.size_bytes, int)
        or not 1 <= artifact.size_bytes <= _MAX_BUILD_PLAN_BYTES
        or not _DIGEST.fullmatch(source_snapshot_digest)
    ):
        raise BuildPlanIntegrityError()
    expected_key = (
        f"tenants/{tenant_ref}/analysis-artifacts/build-plan-candidate/"
        f"sha256/{artifact.digest.removeprefix('sha256:')}.json"
    )
    if not isinstance(artifact.storage_key, str) or not hmac.compare_digest(
        artifact.storage_key, expected_key
    ):
        raise BuildPlanIntegrityError()


def _validate_deployment_descriptor(
    artifact: StoredDeploymentPlanArtifact,
    *,
    tenant_ref: str,
    source_snapshot_digest: str,
) -> None:
    if (
        not _DIGEST.fullmatch(artifact.digest)
        or artifact.media_type != "application/vnd.lae.deployment-plan+json"
        or isinstance(artifact.size_bytes, bool)
        or not isinstance(artifact.size_bytes, int)
        or not 1 <= artifact.size_bytes <= _MAX_BUILD_PLAN_BYTES
        or artifact.source_snapshot_digest != source_snapshot_digest
    ):
        raise BuildPlanIntegrityError()
    expected_key = (
        f"tenants/{tenant_ref}/analysis-artifacts/deployment-plan/"
        f"sha256/{artifact.digest.removeprefix('sha256:')}.json"
    )
    if not isinstance(artifact.storage_key, str) or not hmac.compare_digest(
        artifact.storage_key, expected_key
    ):
        raise BuildPlanIntegrityError()


def _decode_canonical_candidate(raw: bytes) -> dict[str, object]:
    return _decode_canonical_artifact(
        raw, schema="build-plan-candidate.v1.schema.json"
    )


def _decode_canonical_artifact(
    raw: bytes, *, schema: str
) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_closed_object,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise BuildPlanIntegrityError() from exc
    if not isinstance(value, dict) or raw != _canonical_bytes(value):
        raise BuildPlanIntegrityError()
    if validate_instance(schema, value):
        raise BuildPlanIntegrityError()
    return value


def _validate_candidate_binding(
    candidate: Mapping[str, object],
    *,
    source_snapshot_digest: str,
    resolved_commit: str,
    policy_version: str,
) -> None:
    if (
        candidate.get("sourceSnapshotDigest") != source_snapshot_digest
        or candidate.get("resolvedCommit") != resolved_commit
        or candidate.get("policyVersion") != policy_version
    ):
        raise BuildPlanIntegrityError()
    image_keys = _candidate_image_keys(candidate)
    if not image_keys:
        raise BuildPlanIntegrityError()


def _candidate_image_keys(candidate: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for field_name in ("builds", "externalImages"):
        items = candidate.get(field_name)
        if not isinstance(items, list):
            raise BuildPlanIntegrityError()
        for item in items:
            if not isinstance(item, Mapping):
                raise BuildPlanIntegrityError()
            key = item.get("key")
            if not isinstance(key, str) or _IMAGE_KEY.fullmatch(key) is None:
                raise BuildPlanIntegrityError()
            values.append(key)
    if len(values) != len(set(values)) or len(values) > 64:
        raise BuildPlanIntegrityError()
    return tuple(sorted(values))


def _trusted_runtime_topology(
    plan: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    source_snapshot_digest: str,
    policy_version: str,
) -> tuple[
    str,
    tuple[TrustedRuntimeService, ...],
    tuple[TrustedRuntimeRoute, ...],
    tuple[TrustedRuntimeVolume, ...],
    dict[str, str],
]:
    if plan.get("sourceDigest") != source_snapshot_digest:
        raise BuildPlanIntegrityError()
    policy = _mapping(plan, "policy")
    blockers = _string_list(plan, "blockers")
    if (
        policy.get("version") != policy_version
        or policy.get("decision") not in {"allow", "needs_configuration"}
        or blockers
    ):
        raise BuildPlanIntegrityError()
    kind = _string(plan, "kind")
    raw_services = _mapping_list(plan, "services")
    raw_routes = _mapping_list(plan, "routes")
    raw_volumes = _mapping_list(plan, "volumes")
    raw_environment = _mapping_list(plan, "environment")
    if (
        kind not in {"service", "compose"}
        or not 1 <= len(raw_services) <= 64
        or (kind == "service" and len(raw_services) != 1)
        or len(raw_routes) > 64
        or len(raw_volumes) > 64
        or len(raw_environment) > 512
    ):
        raise BuildPlanIntegrityError()

    candidate_builds = {
        _string(item, "key"): item for item in _mapping_list(candidate, "builds")
    }
    candidate_external = _mapping_list(candidate, "externalImages")
    candidate_keys = set(_candidate_image_keys(candidate))
    service_keys = [_string(item, "key") for item in raw_services]
    if len(service_keys) != len(set(service_keys)):
        raise BuildPlanIntegrityError()
    known_services = set(service_keys)

    runtime_environment: dict[str, set[str]] = {
        key: set() for key in service_keys
    }
    build_environment: dict[str, set[str]] = {key: set() for key in service_keys}
    for variable in raw_environment:
        name = _string(variable, "name")
        scope = _string(variable, "scope")
        targets = _string_list(variable, "services")
        if (
            scope not in {"runtime", "build"}
            or any(target not in known_services for target in targets)
        ):
            raise BuildPlanIntegrityError()
        destination = runtime_environment if scope == "runtime" else build_environment
        for target in targets:
            if name in destination[target]:
                raise BuildPlanIntegrityError()
            destination[target].add(name)

    service_build_keys: dict[str, str] = {}
    trusted_services: list[TrustedRuntimeService] = []
    raw_service_by_key = {_string(item, "key"): item for item in raw_services}
    for service_key in service_keys:
        service = raw_service_by_key[service_key]
        role = _string(service, "role")
        if role not in {"http", "worker", "internal", "datastore"}:
            raise BuildPlanIntegrityError()
        dependencies = tuple(_string_list(service, "dependencies"))
        if service_key in dependencies or any(
            dependency not in known_services for dependency in dependencies
        ):
            raise BuildPlanIntegrityError()
        environment_names = tuple(_string_list(service, "environmentNames"))
        if set(environment_names) != (
            runtime_environment[service_key] | build_environment[service_key]
        ):
            raise BuildPlanIntegrityError()
        image = _mapping(service, "image")
        source = _string(image, "source")
        if source == "build" and set(image) == {"source", "buildKey"}:
            build_key = _string(image, "buildKey")
            if build_key not in candidate_builds:
                raise BuildPlanIntegrityError()
            build = candidate_builds[build_key]
            required_build_values = set(_string_list(build, "buildArgNames")) | set(
                _string_list(build, "secretMountNames")
            )
            if required_build_values != build_environment[service_key]:
                raise BuildPlanIntegrityError()
        elif source == "external" and set(image) == {"source", "ref"}:
            reference = _string(image, "ref")
            matches = [
                item
                for item in candidate_external
                if item.get("ref") == reference
            ]
            if len(matches) != 1:
                raise BuildPlanIntegrityError()
            build_key = _string(matches[0], "key")
            if build_environment[service_key]:
                raise BuildPlanIntegrityError()
        else:
            raise BuildPlanIntegrityError()
        if build_key in service_build_keys.values():
            # v1 persistence stores one attestation row per service/build key.
            raise BuildPlanIntegrityError()
        service_build_keys[service_key] = build_key

        resources = _mapping(service, "resources")
        cpu = _string(resources, "cpu")
        memory_mib = _integer(resources, "memoryMiB")
        command = service.get("command")
        if command is not None and not isinstance(command, str):
            raise BuildPlanIntegrityError()
        port_value = service.get("port")
        port = None if port_value is None else _integer(service, "port")
        health_value = service.get("healthcheck")
        health_path: str | None = None
        health_interval: int | None = None
        if health_value is not None:
            health = _mapping_value(health_value)
            if health.get("type") != "http" or role != "http" or port is None:
                raise BuildPlanIntegrityError()
            health_path = _string(health, "path")
            health_interval = _integer(health, "intervalSeconds")
        trusted_services.append(
            TrustedRuntimeService(
                service_key=service_key,
                role=role,
                build_key=build_key,
                command=command,
                dependencies=dependencies,
                cpu=cpu,
                memory_mib=memory_mib,
                environment_names=tuple(sorted(runtime_environment[service_key])),
                port=port,
                health_path=health_path,
                health_interval_seconds=health_interval,
            )
        )
    if set(service_build_keys.values()) != candidate_keys:
        raise BuildPlanIntegrityError()
    _require_acyclic_service_plan(trusted_services)

    trusted_routes: list[TrustedRuntimeRoute] = []
    seen_routes: set[tuple[str, int]] = set()
    if raw_routes and sum(item.get("primary") is True for item in raw_routes) != 1:
        raise BuildPlanIntegrityError()
    for route in raw_routes:
        service_key = _string(route, "serviceKey")
        container_port = _integer(route, "containerPort")
        service = raw_service_by_key.get(service_key)
        pair = (service_key, container_port)
        if (
            service is None
            or service.get("role") != "http"
            or service.get("port") != container_port
            or pair in seen_routes
        ):
            raise BuildPlanIntegrityError()
        health_path = _string(route, "healthPath")
        health = service.get("healthcheck")
        if health is not None and _mapping_value(health).get("path") != health_path:
            raise BuildPlanIntegrityError()
        seen_routes.add(pair)
        trusted_routes.append(
            TrustedRuntimeRoute(service_key, container_port, health_path)
        )

    trusted_volumes: list[TrustedRuntimeVolume] = []
    volume_keys: set[str] = set()
    mounts: set[tuple[str, str]] = set()
    for volume in raw_volumes:
        key = _string(volume, "key")
        service_targets = tuple(_string_list(volume, "serviceKeys"))
        mount_path = _string(volume, "mountPath")
        access_mode = _string(volume, "accessMode")
        if (
            key in volume_keys
            or any(target not in known_services for target in service_targets)
            or any((target, mount_path) in mounts for target in service_targets)
        ):
            raise BuildPlanIntegrityError()
        volume_keys.add(key)
        mounts.update((target, mount_path) for target in service_targets)
        trusted_volumes.append(
            TrustedRuntimeVolume(
                volume_key=key,
                requested_bytes=_integer(volume, "requestedBytes"),
                service_keys=service_targets,
                mount_path=mount_path,
                access_mode=access_mode,
            )
        )
    return (
        kind,
        tuple(trusted_services),
        tuple(trusted_routes),
        tuple(trusted_volumes),
        service_build_keys,
    )


def _require_acyclic_service_plan(services: list[TrustedRuntimeService]) -> None:
    by_key = {item.service_key: item for item in services}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise BuildPlanIntegrityError()
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key].dependencies:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key)


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _mapping_value(value.get(key))


def _mapping_value(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BuildPlanIntegrityError()
    return value


def _mapping_list(
    value: Mapping[str, object], key: str
) -> list[Mapping[str, object]]:
    items = value.get(key)
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise BuildPlanIntegrityError()
    return list(items)  # type: ignore[arg-type]


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise BuildPlanIntegrityError()
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise BuildPlanIntegrityError()
    return item


def _string_list(value: Mapping[str, object], key: str) -> list[str]:
    items = value.get(key)
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise BuildPlanIntegrityError()
    return list(items)


def _signed_plan(
    candidate: Mapping[str, object],
    *,
    tenant_ref: str,
    application_ref: str,
    source_snapshot_id: str,
    key_id: str,
    signing_key: bytes,
) -> dict[str, object]:
    unsigned = dict(candidate)
    unsigned["schemaVersion"] = "lae.build-plan/v1"
    signature_payload = _canonical_bytes(
        {
            "schemaVersion": "luma.builder-plan-signature/v1",
            "tenantRef": tenant_ref,
            "applicationRef": application_ref,
            "sourceSnapshotId": source_snapshot_id,
            "plan": unsigned,
        }
    )
    signed = dict(unsigned)
    signed["signature"] = {
        "keyId": key_id,
        "value": _base64url(
            hmac.new(signing_key, signature_payload, hashlib.sha256).digest()
        ),
    }
    if validate_instance("build-plan.v1.schema.json", signed):
        raise BuildPlanIntegrityError()
    return signed


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BuildPlanIntegrityError() from exc


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


__all__ = [
    "BuildCredentialLeaseBinding",
    "BuildCredentialLeaseIssuer",
    "BuildPlanIntegrityError",
    "HmacBuildCredentialLeaseIssuer",
    "S3TrustedBuildPlanMaterializer",
]
