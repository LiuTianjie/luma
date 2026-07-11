from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

from .errors import AdapterErrorCode, LumaAdapterError, protocol_error
from .models import (
    BuilderTask,
    BuilderTaskEvent,
    BuilderTaskEventPage,
    BuilderTaskMutation,
    LumaCallContext,
)

SCHEMA_VERSION = "luma.builder-task/v1"
TASK_KINDS = frozenset({"analyze-source", "build-plan"})
TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "cancel_requested",
        "canceled",
        "succeeded",
        "failed",
        "timed_out",
    }
)
TERMINAL_STATUSES = frozenset({"canceled", "succeeded", "failed", "timed_out"})

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/@ -]{0,255}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_BUILD_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

_TASK_MESSAGES = {
    "queued": "Builder task queued.",
    "running": "Builder task running.",
    "cancel_requested": "Builder task cancellation requested.",
    "canceled": "Builder task canceled.",
    "succeeded": "Builder task succeeded.",
    "failed": "Builder task failed.",
    "timed_out": "Builder task timed out.",
}

_EVENT_TYPES = {
    "status": ("status", "Builder task status updated."),
    "output": ("output", "Builder output received."),
    "source.fetch": ("source.fetch", "Source fetch updated."),
    "source.snapshot": ("source.snapshot", "Source snapshot updated."),
    "analysis": ("analysis", "Source analysis updated."),
    "resolve": ("resolve", "External image resolution updated."),
    "build": ("build", "Image build updated."),
    "push": ("push", "Image push updated."),
    "complete": ("complete", "Builder task completion updated."),
}

_SAFE_RESULTS = {
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
            "artifacts",
        }
    ),
    "build-plan": frozenset(
        {
            "sourceSnapshotDigest",
            "imageDigests",
            "sbomDigests",
            "provenanceDigests",
            "scanDigests",
            "artifacts",
        }
    ),
}
_RAW_RESULTS = {
    "analyze-source": _SAFE_RESULTS["analyze-source"] | {"agentImageDigest"},
    "build-plan": _SAFE_RESULTS["build-plan"] | {"images"},
}
_ANALYZE_ARTIFACTS = {
    "evidence": ("evidenceDigest", "application/vnd.lae.evidence+json"),
    "deploymentPlan": (
        "deploymentPlanDigest",
        "application/vnd.lae.deployment-plan+json",
    ),
    "buildPlan": (
        "buildPlanDigest",
        "application/vnd.lae.build-plan-candidate+json",
    ),
}
_BUILD_ARTIFACTS = {
    "sbom": (
        "sbomDigests",
        frozenset(
            {
                "application/vnd.cyclonedx+json",
                "application/spdx+json",
            }
        ),
    ),
    "provenance": (
        "provenanceDigests",
        frozenset(
            {
                "application/vnd.in-toto+json",
                "application/vnd.lae.provenance+json",
                "application/vnd.lae.external-resolution+json",
            }
        ),
    ),
    "scan": (
        "scanDigests",
        frozenset({"application/vnd.lae.scan-report+json"}),
    ),
}


def validate_context(context: LumaCallContext) -> None:
    for field_name, value in (
        ("tenant_ref", context.tenant_ref),
        ("application_ref", context.application_ref),
        ("external_operation_id", context.external_operation_id),
    ):
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    if context.request_id is not None and not _REQUEST_ID_RE.fullmatch(
        context.request_id
    ):
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)


def validate_principal(principal_id: str, token: str) -> None:
    if not isinstance(principal_id, str) or not _IDENTIFIER_RE.fullmatch(principal_id):
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    if (
        not isinstance(token, str)
        or not token
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    key = value.strip()
    if (
        not key
        or len(key) > 200
        or any(ord(character) < 33 or ord(character) > 126 for character in key)
    ):
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    return key


def validate_task_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    return value


def validate_event_query(after: int, limit: int) -> None:
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)


def task_request_body(
    context: LumaCallContext, *, kind: str, payload: Mapping[str, object]
) -> dict[str, object]:
    validate_context(context)
    if kind not in TASK_KINDS:
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": kind,
        "externalOperationId": context.external_operation_id,
        "tenantRef": context.tenant_ref,
        "applicationRef": context.application_ref,
        "payload": json_copy(dict(payload)),
    }


def context_headers(context: LumaCallContext) -> dict[str, str]:
    validate_context(context)
    headers = {
        "X-LAE-Operation-Id": context.external_operation_id,
        "X-LAE-Tenant-Id": context.tenant_ref,
        "X-LAE-Application-Id": context.application_ref,
    }
    if context.request_id:
        headers["X-Request-Id"] = context.request_id
    return headers


def parse_task_mutation(
    value: Mapping[str, Any], context: LumaCallContext
) -> BuilderTaskMutation:
    task_value = value.get("task")
    replayed = value.get("replayed")
    if not isinstance(task_value, dict) or not isinstance(replayed, bool):
        raise protocol_error()
    return BuilderTaskMutation(task=parse_task(task_value, context), replayed=replayed)


def parse_task_envelope(
    value: Mapping[str, Any], context: LumaCallContext
) -> BuilderTask:
    task_value = value.get("task")
    if not isinstance(task_value, dict):
        raise protocol_error()
    return parse_task(task_value, context)


def parse_task(value: Mapping[str, Any], context: LumaCallContext) -> BuilderTask:
    validate_context(context)
    if required_string(value, "schemaVersion") != SCHEMA_VERSION:
        raise protocol_error()
    kind = required_string(value, "kind")
    status = required_string(value, "status")
    if kind not in TASK_KINDS or status not in TASK_STATUSES:
        raise protocol_error()
    task_context = (
        required_string(value, "tenantRef"),
        required_string(value, "applicationRef"),
        required_string(value, "externalOperationId"),
    )
    expected_context = (
        context.tenant_ref,
        context.application_ref,
        context.external_operation_id,
    )
    if task_context != expected_context:
        # A same-principal, wrong-tenant response is a protocol/security error,
        # never a task object that can accidentally cross a tenant boundary.
        raise protocol_error()
    raw_result = value.get("result")
    if raw_result is not None and not isinstance(raw_result, dict):
        raise protocol_error()
    if status == "succeeded" and not isinstance(raw_result, dict):
        raise protocol_error()
    result = (
        safe_result(kind, raw_result, require_complete=status == "succeeded")
        if isinstance(raw_result, dict)
        else None
    )
    return BuilderTask(
        task_id=required_task_id(value, "id"),
        kind=kind,
        external_operation_id=task_context[2],
        tenant_ref=task_context[0],
        application_ref=task_context[1],
        status=status,
        message=_TASK_MESSAGES[status],
        created_at=required_non_negative_int(value, "createdAt"),
        updated_at=required_non_negative_int(value, "updatedAt"),
        started_at=required_non_negative_int(value, "startedAt"),
        completed_at=required_non_negative_int(value, "completedAt"),
        last_cursor=required_non_negative_int(value, "lastCursor"),
        result=result,
    )


def parse_event_page(
    value: Mapping[str, Any],
    *,
    task_id: str,
    after: int,
) -> BuilderTaskEventPage:
    response_task_id = required_string(value, "taskId")
    status = required_string(value, "status")
    if response_task_id != task_id or status not in TASK_STATUSES:
        raise protocol_error()
    raw_events = value.get("events")
    if not isinstance(raw_events, list):
        raise protocol_error()
    events: list[BuilderTaskEvent] = []
    prior_cursor = after
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise protocol_error()
        event = parse_event(raw_event)
        if event.cursor <= prior_cursor:
            raise protocol_error()
        prior_cursor = event.cursor
        events.append(event)
    next_cursor = required_non_negative_int(value, "nextCursor")
    expected_next = events[-1].cursor if events else after
    if next_cursor != expected_next:
        raise protocol_error()
    oldest_cursor = required_non_negative_int(value, "oldestCursor")
    if oldest_cursor < 1:
        raise protocol_error()
    has_more = value.get("hasMore")
    terminal = value.get("terminal")
    if not isinstance(has_more, bool) or not isinstance(terminal, bool):
        raise protocol_error()
    if terminal != (status in TERMINAL_STATUSES):
        raise protocol_error()
    return BuilderTaskEventPage(
        task_id=response_task_id,
        status=status,
        events=tuple(events),
        next_cursor=next_cursor,
        oldest_cursor=oldest_cursor,
        has_more=has_more,
        terminal=terminal,
    )


def parse_event(value: Mapping[str, Any]) -> BuilderTaskEvent:
    cursor = required_positive_int(value, "cursor")
    sequence = required_positive_int(value, "seq")
    if cursor != sequence:
        raise protocol_error()
    raw_type = required_string(value, "type")
    event_type, default_message = _EVENT_TYPES.get(
        raw_type, ("update", "Builder task updated.")
    )
    status_value = value.get("status")
    if status_value is not None and (
        not isinstance(status_value, str) or status_value not in TASK_STATUSES
    ):
        raise protocol_error()
    message = _TASK_MESSAGES.get(status_value, default_message)
    raw_message = value.get("message")
    if raw_message == "[redacted builder output]":
        message = "Builder output redacted."
    return BuilderTaskEvent(
        cursor=cursor,
        sequence=sequence,
        event_type=event_type,
        status=status_value,
        message=message,
        timestamp=required_non_negative_int(value, "ts"),
    )


def safe_result(
    kind: str,
    result: Mapping[str, Any],
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    raw_allowed = _RAW_RESULTS[kind]
    if not set(result).issubset(raw_allowed):
        raise protocol_error()
    if require_complete and set(result) != raw_allowed:
        raise protocol_error()
    if kind == "build-plan":
        return _safe_build_result(result, require_complete=require_complete)
    return _safe_analyze_result(result, require_complete=require_complete)


def _safe_analyze_result(
    result: Mapping[str, Any],
    *,
    require_complete: bool,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    digest_fields = {
        "sourceTreeDigest",
        "sourceSnapshotDigest",
        "deploymentPlanDigest",
        "buildPlanDigest",
        "evidenceDigest",
    }
    for key, value in result.items():
        if key in digest_fields:
            if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
                raise protocol_error()
            safe[key] = value
        elif key == "resolvedCommit":
            if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
                raise protocol_error()
            safe[key] = value
        elif key == "sourceSnapshotId":
            if (
                not isinstance(value, str)
                or not _IDENTIFIER_RE.fullmatch(value)
                or not value.startswith("snapshot-")
                or "://" in value
            ):
                raise protocol_error()
            safe[key] = value
        elif key == "policyVersion":
            if (
                not isinstance(value, str)
                or not _IDENTIFIER_RE.fullmatch(value)
                or "://" in value
            ):
                raise protocol_error()
            safe[key] = value
        elif key == "agentImageDigest":
            if not isinstance(value, str) or not _IMAGE_DIGEST_RE.fullmatch(value):
                raise protocol_error()
        elif key == "artifacts":
            safe[key] = _safe_analyze_artifact_map(value, result=result)
    if require_complete and set(safe) != _SAFE_RESULTS["analyze-source"]:
        raise protocol_error()
    return safe


def _safe_build_result(
    result: Mapping[str, Any],
    *,
    require_complete: bool,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    source_digest = result.get("sourceSnapshotDigest")
    if source_digest is not None:
        if not isinstance(source_digest, str) or not _DIGEST_RE.fullmatch(
            source_digest
        ):
            raise protocol_error()
        safe["sourceSnapshotDigest"] = source_digest
    raw_images = result.get("images")
    images = safe_image_map(raw_images) if raw_images is not None else None
    for key in (
        "imageDigests",
        "sbomDigests",
        "provenanceDigests",
        "scanDigests",
    ):
        value = result.get(key)
        if value is not None:
            safe[key] = safe_digest_map(value)
    image_digests = safe.get("imageDigests")
    if images is not None and image_digests is not None:
        if set(images) != set(image_digests):
            raise protocol_error()
        for key, image in images.items():
            if not image.endswith("@" + image_digests[key]):
                raise protocol_error()
    if images is not None:
        for key in ("sbomDigests", "provenanceDigests", "scanDigests"):
            if key in safe and not set(safe[key]).issubset(images):
                raise protocol_error()
    raw_artifacts = result.get("artifacts")
    if raw_artifacts is not None:
        safe["artifacts"] = _safe_build_artifact_map(
            raw_artifacts,
            result=safe,
            require_complete=require_complete,
        )
    if require_complete and set(safe) != _SAFE_RESULTS["build-plan"]:
        raise protocol_error()
    return safe


def safe_digest_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        raise protocol_error()
    result: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not _BUILD_KEY_RE.fullmatch(key):
            raise protocol_error()
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise protocol_error()
        result[key] = digest
    return result


def safe_image_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        raise protocol_error()
    result: dict[str, str] = {}
    for key, image in value.items():
        if not isinstance(key, str) or not _BUILD_KEY_RE.fullmatch(key):
            raise protocol_error()
        if not isinstance(image, str) or not _IMAGE_DIGEST_RE.fullmatch(image):
            raise protocol_error()
        result[key] = image
    return result


def _safe_analyze_artifact_map(
    value: Any,
    *,
    result: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != set(_ANALYZE_ARTIFACTS):
        raise protocol_error()
    artifacts: dict[str, dict[str, Any]] = {}
    for key, (digest_field, media_type) in _ANALYZE_ARTIFACTS.items():
        descriptor = _safe_artifact_descriptor(
            value.get(key),
            allowed_media_types=frozenset({media_type}),
        )
        if descriptor["digest"] != result.get(digest_field):
            raise protocol_error()
        artifacts[key] = descriptor
    return artifacts


def _safe_build_artifact_map(
    value: Any,
    *,
    result: Mapping[str, Any],
    require_complete: bool,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or len(value) > 256:
        raise protocol_error()
    image_digests = result.get("imageDigests")
    if not isinstance(image_digests, dict):
        raise protocol_error()
    expected_keys = {
        f"{build_key}-{artifact_kind}"
        for build_key in image_digests
        for artifact_kind in _BUILD_ARTIFACTS
    }
    if require_complete and set(value) != expected_keys:
        raise protocol_error()
    if not set(value).issubset(expected_keys):
        raise protocol_error()
    artifacts: dict[str, dict[str, Any]] = {}
    for key, descriptor in value.items():
        if not isinstance(key, str) or "-" not in key:
            raise protocol_error()
        build_key, artifact_kind = key.rsplit("-", 1)
        if (
            not _BUILD_KEY_RE.fullmatch(build_key)
            or artifact_kind not in _BUILD_ARTIFACTS
        ):
            raise protocol_error()
        digest_field, media_types = _BUILD_ARTIFACTS[artifact_kind]
        digest_map = result.get(digest_field)
        if not isinstance(digest_map, dict) or build_key not in digest_map:
            raise protocol_error()
        safe_descriptor = _safe_artifact_descriptor(
            descriptor,
            allowed_media_types=media_types,
        )
        if safe_descriptor["digest"] != digest_map[build_key]:
            raise protocol_error()
        artifacts[key] = safe_descriptor
    return artifacts


def _safe_artifact_descriptor(
    value: Any,
    *,
    allowed_media_types: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "digest",
        "mediaType",
        "sizeBytes",
    }:
        raise protocol_error()
    digest = value.get("digest")
    media_type = value.get("mediaType")
    size_bytes = value.get("sizeBytes")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise protocol_error()
    if not isinstance(media_type, str) or media_type not in allowed_media_types:
        raise protocol_error()
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= 1024**3
    ):
        raise protocol_error()
    return {"digest": digest, "mediaType": media_type, "sizeBytes": size_bytes}


def json_copy(value: Any) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST) from exc


def canonical_hash_input(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 4096:
        raise protocol_error()
    return item


def required_task_id(value: Mapping[str, Any], key: str) -> str:
    item = required_string(value, key)
    if not _TASK_ID_RE.fullmatch(item):
        raise protocol_error()
    return item


def required_non_negative_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise protocol_error()
    return item


def required_positive_int(value: Mapping[str, Any], key: str) -> int:
    item = required_non_negative_int(value, key)
    if item < 1:
        raise protocol_error()
    return item


def validate_limits(
    cpu: float, memory_mib: int, disk_mib: int, timeout_seconds: int
) -> None:
    if (
        isinstance(cpu, bool)
        or not isinstance(cpu, (int, float))
        or not math.isfinite(cpu)
        or not 0.1 <= cpu <= 32
    ):
        raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
    for value, minimum, maximum in (
        (memory_mib, 128, 131072),
        (disk_mib, 256, 1048576),
        (timeout_seconds, 10, 14400),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)


def safe_request_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) else None
