from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    principal_id: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LumaCallContext:
    tenant_ref: str
    application_ref: str
    external_operation_id: str
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class BuilderLimits:
    cpu: float
    memory_mib: int
    disk_mib: int
    timeout_seconds: int

    def to_wire(self) -> dict[str, int | float]:
        return {
            "cpu": self.cpu,
            "memoryMiB": self.memory_mib,
            "diskMiB": self.disk_mib,
            "timeoutSeconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class SourceReference:
    repository: str
    ref: str | None = None
    subdirectory: str | None = None

    def to_wire(self) -> dict[str, str]:
        result = {"repository": self.repository}
        if self.ref:
            result["ref"] = self.ref
        if self.subdirectory:
            result["subdirectory"] = self.subdirectory
        return result


@dataclass(frozen=True, slots=True)
class ObjectSourceReference:
    """Immutable uploaded source descriptor.

    The object URL and storage key are deliberately absent.  Luma receives
    them only through the task-bound credential lease after the public task has
    been accepted, so neither value can enter durable task state or logs.
    """

    digest: str
    media_type: str
    size_bytes: int

    def to_wire(self) -> dict[str, str | int]:
        return {
            "kind": "object",
            "digest": self.digest,
            "mediaType": self.media_type,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class AnalyzeSourceRequest:
    source: SourceReference | ObjectSourceReference
    credential_lease_id: str = field(repr=False)
    agent_image_digest: str
    policy_version: str
    limits: BuilderLimits

    def to_wire(self) -> dict[str, object]:
        return {
            "sourceRef": self.source.to_wire(),
            "credentialLeaseId": self.credential_lease_id,
            "agentImageDigest": self.agent_image_digest,
            "policyVersion": self.policy_version,
            "limits": self.limits.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class BuildPlanRequest:
    source_snapshot_id: str
    source_snapshot_digest: str
    signed_build_plan: Mapping[str, Any]
    credential_lease_id: str = field(repr=False)
    limits: BuilderLimits

    def to_wire(self) -> dict[str, object]:
        return {
            "sourceSnapshotId": self.source_snapshot_id,
            "sourceSnapshotDigest": self.source_snapshot_digest,
            "signedBuildPlan": dict(self.signed_build_plan),
            "credentialLeaseId": self.credential_lease_id,
            "limits": self.limits.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class BuilderTask:
    """Tenant-safe Builder Task view.

    ``result`` is an allowlisted result view. In particular it never contains
    Luma's analyzer image reference or internal image registry references.
    """

    task_id: str
    kind: str
    external_operation_id: str
    tenant_ref: str
    application_ref: str
    status: str
    message: str
    created_at: int
    updated_at: int
    started_at: int
    completed_at: int
    last_cursor: int
    result: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"canceled", "succeeded", "failed", "timed_out"}

    def to_tenant_dict(self) -> dict[str, object]:
        return {
            "id": self.task_id,
            "kind": self.kind,
            "externalOperationId": self.external_operation_id,
            "status": self.status,
            "message": self.message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "lastCursor": self.last_cursor,
            **({"result": self.result} if self.result is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class BuilderTaskMutation:
    task: BuilderTask
    replayed: bool


@dataclass(frozen=True, slots=True)
class BuilderTaskEvent:
    cursor: int
    sequence: int
    event_type: str
    status: str | None
    message: str
    timestamp: int

    def to_tenant_dict(self) -> dict[str, object]:
        return {
            "cursor": self.cursor,
            "seq": self.sequence,
            "type": self.event_type,
            "message": self.message,
            "timestamp": self.timestamp,
            **({"status": self.status} if self.status else {}),
        }


@dataclass(frozen=True, slots=True)
class BuilderTaskEventPage:
    task_id: str
    status: str
    events: tuple[BuilderTaskEvent, ...]
    next_cursor: int
    oldest_cursor: int
    has_more: bool
    terminal: bool
