from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
from typing import Any, Dict

from .errors import LumaError


BUILDER_TASK_SCHEMA_VERSION = "luma.builder-task/v1"
BUILDER_TASK_KINDS = frozenset({"analyze-source", "build-plan"})

_COMMON_FIELDS = frozenset({"schemaVersion", "kind", "externalOperationId", "tenantRef", "applicationRef", "payload"})
_ANALYZE_FIELDS = frozenset({"sourceRef", "credentialLeaseId", "agentImageDigest", "policyVersion", "limits"})
_BUILD_FIELDS = frozenset({"sourceSnapshotId", "sourceSnapshotDigest", "signedBuildPlan", "credentialLeaseId", "limits"})
_GIT_SOURCE_FIELDS = frozenset({"repository", "ref", "subdirectory"})
_OBJECT_SOURCE_FIELDS = frozenset({"kind", "digest", "mediaType", "sizeBytes"})
_LIMIT_FIELDS = frozenset({"cpu", "memoryMiB", "diskMiB", "timeoutSeconds"})
_PLAN_FIELDS = frozenset(
    {
        "schemaVersion",
        "sourceSnapshotDigest",
        "resolvedCommit",
        "policyVersion",
        "builds",
        "externalImages",
        "signature",
    }
)
_PLAN_BUILD_FIELDS = frozenset(
    {
        "key",
        "context",
        "dockerfile",
        "target",
        "platform",
        "buildArgNames",
        "secretMountNames",
        "dependsOnBuilds",
    }
)
_PLAN_EXTERNAL_IMAGE_FIELDS = frozenset({"key", "ref", "resolvedDigest", "platform"})
_SIGNATURE_FIELDS = frozenset({"keyId", "value"})
_ARTIFACT_DESCRIPTOR_FIELDS = frozenset({"digest", "mediaType", "sizeBytes"})
_ANALYZE_ARTIFACTS = {
    "evidence": ("evidenceDigest", "application/vnd.lae.evidence+json"),
    "deploymentPlan": ("deploymentPlanDigest", "application/vnd.lae.deployment-plan+json"),
    "buildPlan": ("buildPlanDigest", "application/vnd.lae.build-plan-candidate+json"),
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_BUILD_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_BUILD_ARTIFACT_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PLAN_KEY_ID_RE = re.compile(r"^lae-plan-[A-Za-z0-9_-]+$")
_PLAN_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_IMAGE_REPOSITORY_COMPONENT_RE = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$"
)
_PUBLIC_REGISTRY_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_PRIVATE_REGISTRY_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".private",
)
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:password|passwd|token|secret|private[_-]?key|api[_-]?key|access[_-]?key|"
    r"authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|session|credentials?)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"https://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
)
_SECRET_REFERENCE_KEYS = frozenset({"credentialLeaseId", "secretMountNames"})
_RESULT_FIELDS = {
    "analyze-source": frozenset(
        {
            "resolvedCommit",
            "sourceTreeDigest",
            "sourceSnapshotId",
            "sourceSnapshotDigest",
            "deploymentPlanDigest",
            "buildPlanDigest",
            "evidenceDigest",
            "policyVersion",
            "agentImageDigest",
            "verdict",
            "diagnosticStatus",
            "diagnosticMode",
            "diagnosticCode",
            "knowledgeVersion",
            "blockers",
            "artifacts",
        }
    ),
    "build-plan": frozenset(
        {
            "sourceSnapshotDigest",
            "images",
            "imageDigests",
            "sbomDigests",
            "provenanceDigests",
            "scanDigests",
            "artifacts",
        }
    ),
}


def validate_builder_task_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the public Builder Task v1 request.

    This is intentionally a closed schema. Builder node and node-agent action are
    not request fields: Control derives both after validation.
    """

    if not isinstance(body, dict):
        raise LumaError("builder task request must be a JSON object")
    _reject_unknown_fields(body, _COMMON_FIELDS, "builder task request")
    schema_version = _required_string(body, "schemaVersion", "builder task request", max_length=80)
    if schema_version != BUILDER_TASK_SCHEMA_VERSION:
        raise LumaError(f"unsupported builder task schemaVersion: {schema_version}")
    kind = _required_string(body, "kind", "builder task request", max_length=40)
    if kind not in BUILDER_TASK_KINDS:
        raise LumaError(f"builder task kind must be one of {sorted(BUILDER_TASK_KINDS)}")
    external_operation_id = _required_string(body, "externalOperationId", "builder task request", max_length=256)
    if not _IDENTIFIER_RE.fullmatch(external_operation_id):
        raise LumaError("externalOperationId contains unsupported characters")
    tenant_ref = _required_reference(body, "tenantRef", "builder task request")
    application_ref = _required_reference(body, "applicationRef", "builder task request")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise LumaError("builder task payload must be a JSON object")
    _reject_recursive_inline_secrets(payload)
    if kind == "analyze-source":
        normalized_payload = _validate_analyze_payload(payload)
    else:
        normalized_payload = _validate_build_payload(payload)
    return {
        "schemaVersion": BUILDER_TASK_SCHEMA_VERSION,
        "kind": kind,
        "externalOperationId": external_operation_id,
        "tenantRef": tenant_ref,
        "applicationRef": application_ref,
        "payload": normalized_payload,
    }


def builder_action_for_kind(kind: str) -> str:
    value = str(kind or "").strip()
    if value not in BUILDER_TASK_KINDS:
        raise LumaError(f"unsupported builder task kind: {value}")
    return value


def canonical_builder_task_request(request: Dict[str, Any]) -> str:
    normalized = validate_builder_task_request(request)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def builder_task_request_hash(request: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_builder_task_request(request).encode("utf-8")).hexdigest()


def builder_registry_repository(
    principal_ref: str,
    tenant_ref: str,
    application_ref: str,
    build_key: str,
) -> str:
    """Derive the platform-owned registry repository for one signed build.

    The derivation is deliberately shared by Control and the node executor so a
    leased task cannot replace the tenant/application namespace with an
    attacker-selected repository.  Opaque hashes also keep internal principal
    identifiers out of public image names.
    """

    principal = _registry_scope_value(principal_ref, "principalRef")
    tenant = _registry_scope_value(tenant_ref, "tenantRef")
    application = _registry_scope_value(application_ref, "applicationRef")
    key = str(build_key or "").strip().lower()
    if not _BUILD_KEY_RE.fullmatch(key):
        raise LumaError("build key is invalid for registry repository derivation")

    principal_id = hashlib.sha256(f"principal\0{principal}".encode("utf-8")).hexdigest()[:16]
    tenant_id = hashlib.sha256(f"{principal}\0tenant\0{tenant}".encode("utf-8")).hexdigest()[:16]
    application_id = hashlib.sha256(
        f"{principal}\0{tenant}\0application\0{application}".encode("utf-8")
    ).hexdigest()[:20]
    return f"lae/p-{principal_id}/t-{tenant_id}/a-{application_id}/{key}"


def parse_external_image_reference(value: Any) -> Dict[str, str]:
    """Validate a public, anonymous Docker image reference.

    External images are deliberately narrower than the full Docker reference
    grammar.  They may use Docker Hub shorthand (``postgres:17``) or an
    explicit public DNS registry, but never a URL, credential-bearing value,
    localhost/IP target, mutable ``latest`` tag, or implicit tag.  Digest-only
    inputs are accepted and still pass through the resolver so platform and
    artifact processing follow the same lane.

    The original spelling is retained for signature and provenance binding;
    ``canonicalName`` is the normalized repository name used in the immutable
    execution result.
    """

    if not isinstance(value, str):
        raise LumaError("external image ref must be a string")
    reference = value.strip()
    if (
        not reference
        or len(reference) > 512
        or reference != value
        or any(character.isspace() for character in reference)
        or any(character in reference for character in ("\0", "\n", "\r", "?", "#", "\\", "%"))
        or "://" in reference
    ):
        raise LumaError("external image ref is invalid")

    if reference.count("@") > 1:
        raise LumaError("external image ref contains an invalid digest")
    name_and_tag, separator, digest = reference.partition("@")
    if separator and not _SHA256_RE.fullmatch(digest):
        raise LumaError("external image ref digest must be sha256:<64 lowercase hex characters>")

    last_slash = name_and_tag.rfind("/")
    last_colon = name_and_tag.rfind(":")
    tag = ""
    name = name_and_tag
    if last_colon > last_slash:
        tag = name_and_tag[last_colon + 1 :]
        name = name_and_tag[:last_colon]
    if separator and tag:
        raise LumaError("external image ref must not combine a tag and digest")
    if not separator:
        if not _IMAGE_TAG_RE.fullmatch(tag):
            raise LumaError("external image ref must include an explicit valid tag")
        if tag.lower() == "latest":
            raise LumaError("external image ref must not use the mutable latest tag")

    components = name.split("/")
    if not components or any(not component for component in components):
        raise LumaError("external image repository is invalid")
    explicit_registry = len(components) > 1 and (
        "." in components[0] or ":" in components[0] or components[0].lower() == "localhost"
    )
    if explicit_registry:
        registry_host = components[0]
        repository_components = components[1:]
        if (
            registry_host != registry_host.lower()
            or not _PUBLIC_REGISTRY_HOST_RE.fullmatch(registry_host)
            or registry_host.endswith(_PRIVATE_REGISTRY_SUFFIXES)
        ):
            raise LumaError("external image registry must be a public lowercase DNS hostname without a port")
    else:
        registry_host = "docker.io"
        repository_components = components
    if not repository_components or any(
        not _IMAGE_REPOSITORY_COMPONENT_RE.fullmatch(component)
        for component in repository_components
    ):
        raise LumaError("external image repository must use lowercase Docker repository components")
    if registry_host == "docker.io" and len(repository_components) == 1:
        repository_components = ["library", repository_components[0]]
    canonical_name = registry_host + "/" + "/".join(repository_components)
    return {
        "reference": reference,
        "registryHost": registry_host,
        "canonicalName": canonical_name,
        "digest": digest if separator else "",
        "tag": tag,
    }


def _registry_scope_value(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or any(char in normalized for char in ("\0", "\n", "\r")):
        raise LumaError(f"{label} is invalid for registry repository derivation")
    return normalized


def builder_plan_content_digest(request: Dict[str, Any]) -> str:
    """Digest the exact unsigned candidate represented by a signed BuildPlan.

    The analyzer persists ``lae.build-plan-candidate/v1`` bytes.  The trusted
    controller changes only ``schemaVersion`` and adds ``signature`` when it
    creates ``lae.build-plan/v1``.  Reversing that transform here binds build
    execution to the analyzed candidate's actual content digest.
    """

    normalized = validate_builder_task_request(request)
    if normalized.get("kind") != "build-plan":
        raise LumaError("builder plan digest requires a build-plan task request")
    plan = dict(normalized["payload"]["signedBuildPlan"])
    plan.pop("signature", None)
    plan["schemaVersion"] = "lae.build-plan-candidate/v1"
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def builder_plan_signature_payload(request: Dict[str, Any]) -> bytes:
    """Return the canonical tenant/application/snapshot-bound signature payload."""

    normalized = validate_builder_task_request(request)
    if normalized.get("kind") != "build-plan":
        raise LumaError("builder plan signature requires a build-plan task request")
    plan = dict(normalized["payload"]["signedBuildPlan"])
    plan.pop("signature", None)
    envelope = {
        "schemaVersion": "luma.builder-plan-signature/v1",
        "tenantRef": normalized["tenantRef"],
        "applicationRef": normalized["applicationRef"],
        "sourceSnapshotId": normalized["payload"]["sourceSnapshotId"],
        "plan": plan,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sanitize_builder_task_result(
    kind: str,
    result: Dict[str, Any],
    *,
    request: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a bounded JSON copy of a trusted-shape agent result.

    Node agents are authenticated, but their output is still treated as
    untrusted data. In particular, an accidental credential echo must never be
    copied into the durable Builder Task record.
    """

    allowed = _RESULT_FIELDS.get(str(kind or ""))
    if allowed is None:
        raise LumaError(f"unsupported builder task kind: {kind}")
    if not isinstance(result, dict):
        raise LumaError("builder task result must be a JSON object")
    _reject_unknown_fields(result, allowed, f"{kind} result")
    _reject_recursive_inline_secrets(result, path="result")
    _validate_json_result_value(result, path="result", depth=0)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise LumaError("builder task result exceeds 1 MiB")
    decoded = json.loads(encoded)
    normalized = decoded if isinstance(decoded, dict) else {}
    if str(kind) == "analyze-source":
        return _validate_analyze_result(normalized, request=request)
    return _validate_build_result(normalized, request=request)


def _validate_analyze_result(
    result: Dict[str, Any],
    *,
    request: Dict[str, Any] | None,
) -> Dict[str, Any]:
    required = _RESULT_FIELDS["analyze-source"]
    missing = sorted(required - set(result))
    if missing:
        raise LumaError(f"analyze-source result is missing required field(s): {', '.join(missing)}")
    resolved_commit = _required_string(result, "resolvedCommit", "analyze-source result", max_length=64).lower()
    if not _COMMIT_RE.fullmatch(resolved_commit):
        raise LumaError("analyze-source result.resolvedCommit must be a full Git object id")
    normalized: Dict[str, Any] = {
        "resolvedCommit": resolved_commit,
        "sourceTreeDigest": _required_sha256(result, "sourceTreeDigest", "analyze-source result"),
        "sourceSnapshotId": _required_reference(result, "sourceSnapshotId", "analyze-source result"),
        "sourceSnapshotDigest": _required_sha256(result, "sourceSnapshotDigest", "analyze-source result"),
        "deploymentPlanDigest": _required_sha256(result, "deploymentPlanDigest", "analyze-source result"),
        "buildPlanDigest": _required_sha256(result, "buildPlanDigest", "analyze-source result"),
        "evidenceDigest": _required_sha256(result, "evidenceDigest", "analyze-source result"),
        "policyVersion": _required_reference(result, "policyVersion", "analyze-source result"),
        "agentImageDigest": _required_string(result, "agentImageDigest", "analyze-source result", max_length=1024),
        "verdict": _required_string(result, "verdict", "analyze-source result", max_length=32),
        "diagnosticStatus": _required_string(result, "diagnosticStatus", "analyze-source result", max_length=32),
        "diagnosticMode": _required_string(result, "diagnosticMode", "analyze-source result", max_length=32),
        "diagnosticCode": _required_string(result, "diagnosticCode", "analyze-source result", max_length=96),
        "knowledgeVersion": _required_string(result, "knowledgeVersion", "analyze-source result", max_length=96),
    }
    if normalized["verdict"] not in {"deployable", "needs_input", "unsupported", "diagnostic_failed"}:
        raise LumaError("analyze-source result.verdict is invalid")
    if normalized["diagnosticStatus"] not in {"succeeded", "diagnostic_failed"}:
        raise LumaError("analyze-source result.diagnosticStatus is invalid")
    if normalized["diagnosticMode"] not in {"ai", "deterministic_fallback"}:
        raise LumaError("analyze-source result.diagnosticMode is invalid")
    for field in ("diagnosticCode", "knowledgeVersion"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,95}", normalized[field]):
            raise LumaError(f"analyze-source result.{field} is invalid")
    raw_blockers = result.get("blockers")
    if not isinstance(raw_blockers, list) or len(raw_blockers) > 128:
        raise LumaError("analyze-source result.blockers is invalid")
    blockers: list[Dict[str, str]] = []
    for item in raw_blockers:
        if not isinstance(item, dict) or set(item) != {"code", "path", "field", "remediation"}:
            raise LumaError("analyze-source result.blockers is invalid")
        blocker = {
            key: _required_string(item, key, "analyze-source result blocker", max_length=1024)
            for key in ("code", "path", "field", "remediation")
        }
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", blocker["code"]):
            raise LumaError("analyze-source result blocker code is invalid")
        blockers.append(blocker)
    if normalized["verdict"] == "unsupported" and not blockers:
        raise LumaError("unsupported analyze-source result must contain blockers")
    if normalized["verdict"] != "unsupported" and blockers:
        raise LumaError("only unsupported analyze-source result may contain blockers")
    normalized["blockers"] = blockers
    if not _IMAGE_DIGEST_RE.fullmatch(normalized["agentImageDigest"]):
        raise LumaError("analyze-source result.agentImageDigest must be an immutable image reference")
    payload = request.get("payload") if isinstance(request, dict) and isinstance(request.get("payload"), dict) else {}
    for field in ("policyVersion", "agentImageDigest"):
        expected = str(payload.get(field) or "")
        if expected and normalized[field] != expected:
            raise LumaError(f"analyze-source result.{field} does not match the task request")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_ANALYZE_ARTIFACTS):
        raise LumaError("analyze-source result.artifacts must contain evidence, deploymentPlan, and buildPlan")
    normalized_artifacts: Dict[str, Any] = {}
    for artifact_name, (digest_field, media_type) in _ANALYZE_ARTIFACTS.items():
        descriptor = _validate_artifact_descriptor(
            artifacts.get(artifact_name),
            label=f"analyze-source result.artifacts.{artifact_name}",
            allowed_media_types=frozenset({media_type}),
        )
        if descriptor["digest"] != normalized[digest_field]:
            raise LumaError(f"analyze-source result.artifacts.{artifact_name}.digest does not match {digest_field}")
        normalized_artifacts[artifact_name] = descriptor
    normalized["artifacts"] = normalized_artifacts
    return normalized


def _validate_build_result(
    result: Dict[str, Any],
    *,
    request: Dict[str, Any] | None,
) -> Dict[str, Any]:
    required = _RESULT_FIELDS["build-plan"]
    missing = sorted(required - set(result))
    if missing:
        raise LumaError(f"build-plan result is missing required field(s): {', '.join(missing)}")
    source_digest = _required_sha256(result, "sourceSnapshotDigest", "build-plan result")
    payload = request.get("payload") if isinstance(request, dict) and isinstance(request.get("payload"), dict) else {}
    expected_digest = str(payload.get("sourceSnapshotDigest") or "")
    if expected_digest and source_digest != expected_digest:
        raise LumaError("build-plan result.sourceSnapshotDigest does not match the task request")
    images = _validate_result_map(result.get("images"), "build-plan result.images", image_references=True)
    image_digests = _validate_result_map(result.get("imageDigests"), "build-plan result.imageDigests", digests=True)
    if set(images) != set(image_digests):
        raise LumaError("build-plan result images and imageDigests must use the same build keys")
    plan = payload.get("signedBuildPlan") if isinstance(payload.get("signedBuildPlan"), dict) else {}
    requested_builds = plan.get("builds") if isinstance(plan.get("builds"), list) else []
    requested_external_images = plan.get("externalImages") if isinstance(plan.get("externalImages"), list) else []
    requested_build_keys = {
        str(item.get("key") or "") for item in requested_builds if isinstance(item, dict)
    }
    requested_external_keys = {
        str(item.get("key") or "") for item in requested_external_images if isinstance(item, dict)
    }
    if request is not None and set(images) != requested_build_keys | requested_external_keys:
        raise LumaError("build-plan result image keys do not match the signed build plan")
    for key, image in images.items():
        if not str(image).endswith("@" + image_digests[key]):
            raise LumaError(f"build-plan result image digest does not match image reference for {key}")
    sbom_digests = _validate_result_map(result.get("sbomDigests"), "build-plan result.sbomDigests", digests=True)
    provenance_digests = _validate_result_map(
        result.get("provenanceDigests"),
        "build-plan result.provenanceDigests",
        digests=True,
    )
    scan_digests = _validate_result_map(
        result.get("scanDigests"),
        "build-plan result.scanDigests",
        digests=True,
    )
    if set(sbom_digests) != set(images):
        raise LumaError("build-plan result must include one SBOM digest for every image")
    if set(provenance_digests) != set(images):
        raise LumaError("build-plan result must include one provenance digest for every image")
    if set(scan_digests) != set(images):
        raise LumaError("build-plan result must include one scan digest for every image")
    raw_artifacts = result.get("artifacts")
    if not isinstance(raw_artifacts, dict) or len(raw_artifacts) > 256:
        raise LumaError("build-plan result.artifacts must be a bounded JSON object")
    expected_artifacts = {
        f"{build_key}-{kind}"
        for build_key in images
        for kind in ("sbom", "provenance", "scan")
    }
    if set(raw_artifacts) != expected_artifacts:
        raise LumaError("build-plan result.artifacts must describe SBOM, provenance, and scan output for every image")
    artifacts: Dict[str, Any] = {}
    for key, value in raw_artifacts.items():
        if not isinstance(key, str) or not _BUILD_ARTIFACT_KEY_RE.fullmatch(key):
            raise LumaError("build-plan result.artifacts contains an invalid artifact key")
        kind = key.rsplit("-", 1)[-1]
        allowed_media_types = {
            "sbom": frozenset({"application/vnd.cyclonedx+json", "application/spdx+json"}),
            "provenance": frozenset(
                {
                    "application/vnd.in-toto+json",
                    "application/vnd.lae.provenance+json",
                    "application/vnd.lae.external-resolution+json",
                }
            ),
            "scan": frozenset({"application/vnd.lae.scan-report+json"}),
        }.get(kind)
        if allowed_media_types is None:
            raise LumaError("build-plan result.artifacts contains an unsupported artifact kind")
        descriptor = _validate_artifact_descriptor(
            value,
            label=f"build-plan result.artifacts.{key}",
            allowed_media_types=allowed_media_types,
        )
        build_key = key[: -(len(kind) + 1)]
        if kind == "provenance" and request is not None:
            expected_provenance_media = (
                "application/vnd.lae.external-resolution+json"
                if build_key in requested_external_keys
                else None
            )
            if expected_provenance_media is not None and descriptor["mediaType"] != expected_provenance_media:
                raise LumaError(
                    f"build-plan result.artifacts.{key}.mediaType must describe external image resolution"
                )
            if build_key in requested_build_keys and descriptor["mediaType"] == "application/vnd.lae.external-resolution+json":
                raise LumaError(
                    f"build-plan result.artifacts.{key}.mediaType cannot describe an external image"
                )
        expected_digest = {
            "sbom": sbom_digests,
            "provenance": provenance_digests,
            "scan": scan_digests,
        }[kind][build_key]
        if descriptor["digest"] != expected_digest:
            raise LumaError(f"build-plan result.artifacts.{key}.digest does not match {kind}Digests")
        artifacts[key] = descriptor
    return {
        "sourceSnapshotDigest": source_digest,
        "images": images,
        "imageDigests": image_digests,
        "sbomDigests": sbom_digests,
        "provenanceDigests": provenance_digests,
        "scanDigests": scan_digests,
        "artifacts": artifacts,
    }


def _validate_result_map(
    value: Any,
    label: str,
    *,
    image_references: bool = False,
    digests: bool = False,
) -> Dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        raise LumaError(f"{label} must be a bounded JSON object")
    result: Dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not _BUILD_KEY_RE.fullmatch(raw_key):
            raise LumaError(f"{label} contains an invalid build key")
        if not isinstance(raw_value, str):
            raise LumaError(f"{label}.{raw_key} must be a string")
        if image_references and not _IMAGE_DIGEST_RE.fullmatch(raw_value):
            raise LumaError(f"{label}.{raw_key} must be an immutable image reference")
        if digests and not _SHA256_RE.fullmatch(raw_value):
            raise LumaError(f"{label}.{raw_key} must be a sha256 digest")
        result[raw_key] = raw_value
    return result


def _validate_artifact_descriptor(
    value: Any,
    *,
    label: str,
    allowed_media_types: frozenset[str],
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LumaError(f"{label} must be a JSON object")
    _reject_unknown_fields(value, _ARTIFACT_DESCRIPTOR_FIELDS, label)
    if set(value) != set(_ARTIFACT_DESCRIPTOR_FIELDS):
        raise LumaError(f"{label} must contain digest, mediaType, and sizeBytes")
    digest = _required_sha256(value, "digest", label)
    media_type = _required_string(value, "mediaType", label, max_length=128)
    if media_type not in allowed_media_types:
        raise LumaError(f"{label}.mediaType is not allowed")
    size_bytes = value.get("sizeBytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0 or size_bytes > 1024 * 1024 * 1024:
        raise LumaError(f"{label}.sizeBytes must be an integer between 0 and 1073741824")
    return {"digest": digest, "mediaType": media_type, "sizeBytes": size_bytes}


_BUILDER_PROGRESS_MESSAGES = {
    "output": "builder output received",
    "status": "builder status updated",
    "source.fetch": "source fetch updated",
    "source.snapshot": "source snapshot updated",
    "analysis": "source analysis updated",
    "resolve": "external image resolution updated",
    "build": "image build updated",
    "push": "image push updated",
    "complete": "builder task completion updated",
}


def sanitize_builder_task_progress_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an agent progress event to a secret-safe durable event.

    Builder stdout is untrusted and can contain credentials whose format is not
    knowable to Control.  Consequently no free-form line from a public-tenant
    builder task is persisted.  A small allowlist preserves the structured
    phase while the durable message is generated by Control.
    """

    raw_type = str(event.get("type") or "output").strip()
    event_type = raw_type if raw_type in _BUILDER_PROGRESS_MESSAGES else "output"
    raw_line = str(event.get("line") or event.get("message") or "")
    redacted = any(pattern.search(raw_line) for pattern in _SECRET_VALUE_PATTERNS)
    return {
        "type": event_type,
        "line": "[redacted builder output]" if redacted else _BUILDER_PROGRESS_MESSAGES[event_type],
        "ts": int(event.get("ts") or 0),
    }


def _validate_analyze_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    _reject_unknown_fields(payload, _ANALYZE_FIELDS, "analyze-source payload")
    source = payload.get("sourceRef")
    if not isinstance(source, dict):
        raise LumaError("analyze-source payload.sourceRef must be a JSON object")
    if source.get("kind") == "object":
        _reject_unknown_fields(source, _OBJECT_SOURCE_FIELDS, "analyze-source sourceRef")
        digest = _required_sha256(source, "digest", "analyze-source sourceRef")
        media_type = _required_string(
            source, "mediaType", "analyze-source sourceRef", max_length=128
        )
        if media_type not in {"text/html", "application/zip"}:
            raise LumaError("analyze-source object mediaType is unsupported")
        size_bytes = source.get("sizeBytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= 536_870_912
        ):
            raise LumaError("analyze-source object sizeBytes is invalid")
        normalized_source: Dict[str, Any] = {
            "kind": "object",
            "digest": digest,
            "mediaType": media_type,
            "sizeBytes": size_bytes,
        }
    else:
        _reject_unknown_fields(source, _GIT_SOURCE_FIELDS, "analyze-source sourceRef")
        repository = _required_string(source, "repository", "analyze-source sourceRef", max_length=2048)
        _validate_repository_reference(repository)
        ref = _optional_string(source, "ref", "analyze-source sourceRef", max_length=512)
        subdirectory = _optional_repo_path(source, "subdirectory", default="")
        normalized_source = {
            "repository": repository,
            **({"ref": ref} if ref else {}),
            **({"subdirectory": subdirectory} if subdirectory else {}),
        }
    credential_lease_id = _required_reference(payload, "credentialLeaseId", "analyze-source payload")
    agent_image = _required_string(payload, "agentImageDigest", "analyze-source payload", max_length=1024)
    if not _IMAGE_DIGEST_RE.fullmatch(agent_image):
        raise LumaError("agentImageDigest must be an immutable sha256 image reference")
    policy_version = _required_reference(payload, "policyVersion", "analyze-source payload")
    return {
        "sourceRef": normalized_source,
        "credentialLeaseId": credential_lease_id,
        "agentImageDigest": agent_image,
        "policyVersion": policy_version,
        "limits": _validate_limits(payload.get("limits")),
    }


def _validate_build_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    _reject_unknown_fields(payload, _BUILD_FIELDS, "build-plan payload")
    source_snapshot_id = _required_reference(payload, "sourceSnapshotId", "build-plan payload")
    source_digest = _required_string(payload, "sourceSnapshotDigest", "build-plan payload", max_length=80).lower()
    if not _SHA256_RE.fullmatch(source_digest):
        raise LumaError("sourceSnapshotDigest must be sha256:<64 lowercase hex characters>")
    plan = payload.get("signedBuildPlan")
    if not isinstance(plan, dict):
        raise LumaError("build-plan payload.signedBuildPlan must be a JSON object")
    normalized_plan = _validate_signed_build_plan(plan, expected_source_digest=source_digest)
    return {
        "sourceSnapshotId": source_snapshot_id,
        "sourceSnapshotDigest": source_digest,
        "signedBuildPlan": normalized_plan,
        "credentialLeaseId": _required_reference(payload, "credentialLeaseId", "build-plan payload"),
        "limits": _validate_limits(payload.get("limits")),
    }


def _validate_signed_build_plan(plan: Dict[str, Any], *, expected_source_digest: str) -> Dict[str, Any]:
    _reject_unknown_fields(plan, _PLAN_FIELDS, "signedBuildPlan")
    schema_version = _required_string(plan, "schemaVersion", "signedBuildPlan", max_length=80)
    if schema_version != "lae.build-plan/v1":
        raise LumaError(f"unsupported signedBuildPlan schemaVersion: {schema_version}")
    source_digest = _required_string(plan, "sourceSnapshotDigest", "signedBuildPlan", max_length=80).lower()
    if source_digest != expected_source_digest:
        raise LumaError("signedBuildPlan sourceSnapshotDigest does not match task sourceSnapshotDigest")
    resolved_commit = _required_string(plan, "resolvedCommit", "signedBuildPlan", max_length=64).lower()
    if not _COMMIT_RE.fullmatch(resolved_commit):
        raise LumaError("signedBuildPlan resolvedCommit must be a full Git object id")
    policy_version = _required_reference(plan, "policyVersion", "signedBuildPlan")
    raw_builds = plan.get("builds")
    if not isinstance(raw_builds, list):
        raise LumaError("signedBuildPlan builds must be an array")
    if len(raw_builds) > 32:
        raise LumaError("signedBuildPlan supports at most 32 builds")
    builds = [_validate_plan_build(item, index=index) for index, item in enumerate(raw_builds)]
    build_keys = [item["key"] for item in builds]
    if len(build_keys) != len(set(build_keys)):
        raise LumaError("signedBuildPlan build keys must be unique")
    raw_external_images = plan.get("externalImages")
    if not isinstance(raw_external_images, list):
        raise LumaError("signedBuildPlan externalImages must be an array")
    if len(raw_external_images) > 64:
        raise LumaError("signedBuildPlan supports at most 64 external images")
    external_images = [
        _validate_plan_external_image(item, index=index)
        for index, item in enumerate(raw_external_images)
    ]
    external_keys = [item["key"] for item in external_images]
    if len(external_keys) != len(set(external_keys)):
        raise LumaError("signedBuildPlan external image keys must be unique")
    if set(build_keys) & set(external_keys):
        raise LumaError("signedBuildPlan build and external image keys must be globally unique")
    if len(build_keys) + len(external_keys) > 64:
        raise LumaError("signedBuildPlan supports at most 64 total images")
    known_keys = set(build_keys)
    for item in builds:
        unknown_dependencies = sorted(set(item.get("dependsOnBuilds") or []) - known_keys)
        if unknown_dependencies:
            raise LumaError(
                f"signedBuildPlan build {item['key']} depends on unknown build(s): {', '.join(unknown_dependencies)}"
            )
    signature = plan.get("signature")
    if not isinstance(signature, dict):
        raise LumaError("signedBuildPlan signature must be a JSON object")
    _reject_unknown_fields(signature, _SIGNATURE_FIELDS, "signedBuildPlan signature")
    normalized_signature = {
        "keyId": _required_reference(signature, "keyId", "signedBuildPlan signature"),
        "value": _required_string(signature, "value", "signedBuildPlan signature", max_length=8192),
    }
    if not _PLAN_KEY_ID_RE.fullmatch(normalized_signature["keyId"]):
        raise LumaError("signedBuildPlan signature.keyId must start with lae-plan-")
    if not _PLAN_SIGNATURE_RE.fullmatch(normalized_signature["value"]):
        raise LumaError("signedBuildPlan signature.value must be base64url text of at least 16 characters")
    return {
        "schemaVersion": "lae.build-plan/v1",
        "sourceSnapshotDigest": source_digest,
        "resolvedCommit": resolved_commit,
        "policyVersion": policy_version,
        "builds": builds,
        "externalImages": external_images,
        "signature": normalized_signature,
    }


def _validate_plan_build(value: Any, *, index: int) -> Dict[str, Any]:
    label = f"signedBuildPlan builds[{index}]"
    if not isinstance(value, dict):
        raise LumaError(f"{label} must be a JSON object")
    _reject_unknown_fields(value, _PLAN_BUILD_FIELDS, label)
    key = _required_string(value, "key", label, max_length=63)
    if not _BUILD_KEY_RE.fullmatch(key):
        raise LumaError(f"{label}.key must be a lowercase service identifier")
    result: Dict[str, Any] = {
        "key": key,
        "context": _required_repo_path(value, "context", label=label),
        "dockerfile": _required_repo_path(value, "dockerfile", label=label),
        "target": _required_nullable_string(value, "target", label, max_length=128),
        "platform": _required_string(value, "platform", label, max_length=80),
        "buildArgNames": _validate_name_list(value.get("buildArgNames"), f"{label}.buildArgNames", env_names=True),
        "secretMountNames": _validate_name_list(value.get("secretMountNames"), f"{label}.secretMountNames", env_names=True),
        "dependsOnBuilds": _validate_name_list(value.get("dependsOnBuilds"), f"{label}.dependsOnBuilds", build_keys=True),
    }
    if result["platform"] != "linux/amd64":
        raise LumaError(f"{label}.platform must be linux/amd64")
    return result


def _validate_plan_external_image(value: Any, *, index: int) -> Dict[str, str]:
    label = f"signedBuildPlan externalImages[{index}]"
    if not isinstance(value, dict):
        raise LumaError(f"{label} must be a JSON object")
    _reject_unknown_fields(value, _PLAN_EXTERNAL_IMAGE_FIELDS, label)
    if set(value) != set(_PLAN_EXTERNAL_IMAGE_FIELDS):
        raise LumaError(f"{label} must contain key, ref, resolvedDigest, and platform")
    key = _required_string(value, "key", label, max_length=63)
    if not _BUILD_KEY_RE.fullmatch(key):
        raise LumaError(f"{label}.key must be a lowercase service identifier")
    raw_reference = value.get("ref")
    if not isinstance(raw_reference, str) or len(raw_reference) > 512:
        raise LumaError(f"{label}.ref is required")
    parsed = parse_external_image_reference(raw_reference)
    resolved_digest = _required_sha256(value, "resolvedDigest", label)
    if parsed["digest"] and resolved_digest != parsed["digest"]:
        raise LumaError(f"{label}.resolvedDigest must match the digest in ref")
    platform = value.get("platform")
    if not isinstance(platform, str):
        raise LumaError(f"{label}.platform is required")
    if platform != "linux/amd64":
        raise LumaError(f"{label}.platform must be linux/amd64")
    return {
        "key": key,
        "ref": parsed["reference"],
        "resolvedDigest": resolved_digest,
        "platform": platform,
    }


def _validate_limits(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LumaError("builder task limits must be a JSON object")
    _reject_unknown_fields(value, _LIMIT_FIELDS, "builder task limits")
    cpu = _bounded_number(value, "cpu", minimum=0.1, maximum=32.0)
    memory = _bounded_integer(value, "memoryMiB", minimum=128, maximum=131072)
    disk = _bounded_integer(value, "diskMiB", minimum=256, maximum=1048576)
    timeout = _bounded_integer(value, "timeoutSeconds", minimum=10, maximum=14400)
    return {"cpu": cpu, "memoryMiB": memory, "diskMiB": disk, "timeoutSeconds": timeout}


def _validate_name_list(value: Any, label: str, *, env_names: bool = False, build_keys: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LumaError(f"{label} must be an array")
    if len(value) > 128:
        raise LumaError(f"{label} supports at most 128 entries")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LumaError(f"{label} entries must be non-empty strings")
        item = item.strip()
        if env_names and not _ENV_NAME_RE.fullmatch(item):
            raise LumaError(f"{label} contains an invalid environment name: {item}")
        if build_keys and not _BUILD_KEY_RE.fullmatch(item):
            raise LumaError(f"{label} contains an invalid build key: {item}")
        result.append(item)
    if len(result) != len(set(result)):
        raise LumaError(f"{label} entries must be unique")
    return result


def _optional_repo_path(body: Dict[str, Any], key: str, *, default: str) -> str:
    value = _optional_string(body, key, key, max_length=1024) or default
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or any(part == ".." for part in normalized.split("/")):
        raise LumaError(f"{key} must stay within the source snapshot")
    if "\x00" in normalized:
        raise LumaError(f"{key} contains an invalid character")
    return normalized


def _required_repo_path(body: Dict[str, Any], key: str, *, label: str) -> str:
    value = _required_string(body, key, label, max_length=1024)
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or any(part == ".." for part in normalized.split("/")):
        raise LumaError(f"{label}.{key} must stay within the source snapshot")
    if "\x00" in normalized:
        raise LumaError(f"{label}.{key} contains an invalid character")
    return normalized


def _validate_repository_reference(value: str) -> None:
    if any(char in value for char in ("\x00", "\n", "\r")):
        raise LumaError("sourceRef.repository contains an invalid character")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        if parsed.scheme not in {"https", "ssh"}:
            raise LumaError("sourceRef.repository must use https or ssh")
        if parsed.username or parsed.password:
            raise LumaError("sourceRef.repository must not contain inline credentials")
        for key, _item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if _SECRET_KEY_RE.search(key):
                raise LumaError("sourceRef.repository must not contain credential query parameters")


def _reject_recursive_inline_secrets(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            next_path = f"{path}.{key_text}"
            if key_text not in _SECRET_REFERENCE_KEYS and _SECRET_KEY_RE.search(key_text):
                raise LumaError(f"inline secret field is not allowed: {next_path}")
            _reject_recursive_inline_secrets(item, path=next_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_recursive_inline_secrets(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and any(pattern.search(value.strip()) for pattern in _SECRET_VALUE_PATTERNS):
        raise LumaError(f"inline secret value is not allowed: {path}")


def _validate_json_result_value(value: Any, *, path: str, depth: int) -> None:
    if depth > 12:
        raise LumaError(f"builder task result nesting is too deep at {path}")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 65536:
            raise LumaError(f"builder task result string is too large at {path}")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LumaError(f"builder task result contains a non-finite number at {path}")
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise LumaError(f"builder task result array is too large at {path}")
        for index, item in enumerate(value):
            _validate_json_result_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 4096:
            raise LumaError(f"builder task result object is too large at {path}")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise LumaError(f"builder task result contains an invalid key at {path}")
            _validate_json_result_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise LumaError(f"builder task result contains a non-JSON value at {path}")


def _reject_unknown_fields(body: Dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise LumaError(f"{label} contains unknown field(s): {', '.join(unknown)}")


def _required_reference(body: Dict[str, Any], key: str, label: str) -> str:
    value = _required_string(body, key, label, max_length=256)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise LumaError(f"{label}.{key} contains unsupported characters")
    return value


def _required_sha256(body: Dict[str, Any], key: str, label: str) -> str:
    value = _required_string(body, key, label, max_length=71).lower()
    if not _SHA256_RE.fullmatch(value):
        raise LumaError(f"{label}.{key} must be a sha256 digest")
    return value


def _required_string(body: Dict[str, Any], key: str, label: str, *, max_length: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LumaError(f"{label}.{key} is required")
    value = value.strip()
    if len(value) > max_length:
        raise LumaError(f"{label}.{key} exceeds {max_length} characters")
    return value


def _optional_string(body: Dict[str, Any], key: str, label: str, *, max_length: int) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LumaError(f"{label}.{key} must be a string")
    value = value.strip()
    if len(value) > max_length:
        raise LumaError(f"{label}.{key} exceeds {max_length} characters")
    return value


def _required_nullable_string(
    body: Dict[str, Any],
    key: str,
    label: str,
    *,
    max_length: int,
) -> str | None:
    if key not in body:
        raise LumaError(f"{label}.{key} is required")
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise LumaError(f"{label}.{key} must be a string or null")
    value = value.strip()
    if len(value) > max_length:
        raise LumaError(f"{label}.{key} exceeds {max_length} characters")
    return value


def _bounded_integer(body: Dict[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LumaError(f"builder task limits.{key} must be an integer")
    if value < minimum or value > maximum:
        raise LumaError(f"builder task limits.{key} must be between {minimum} and {maximum}")
    return value


def _bounded_number(body: Dict[str, Any], key: str, *, minimum: float, maximum: float) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LumaError(f"builder task limits.{key} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise LumaError(f"builder task limits.{key} must be between {minimum} and {maximum}")
    return value
