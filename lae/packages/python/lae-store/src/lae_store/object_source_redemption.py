from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import CredentialLeaseRejected
from .ids import require_opaque_id
from .models import (
    Application,
    BuilderTask,
    Operation,
    SourceCredentialLease,
    SourceRevision,
    Upload,
)


OBJECT_SOURCE_REDEMPTION_REQUEST_SCHEMA = "luma.object-source-redemption/v1"
OBJECT_SOURCE_REDEMPTION_RESULT_SCHEMA = (
    "luma.object-source-redemption-result/v1"
)
OBJECT_SOURCE_REDEMPTION_MAX_TTL_SECONDS = 300
OBJECT_SOURCE_REDEMPTION_FIELDS = frozenset(
    {
        "schemaVersion",
        "leaseId",
        "builderTaskId",
        "externalOperationId",
        "principalRef",
        "tenantRef",
        "applicationRef",
        "object",
    }
)
OBJECT_SOURCE_DESCRIPTOR_FIELDS = frozenset(
    {"kind", "digest", "mediaType", "sizeBytes"}
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UPSTREAM_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_OBJECT_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_GENERIC_REJECTION = "object source lease is unavailable"


@dataclass(frozen=True, slots=True)
class ObjectSourceDescriptor:
    digest: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.digest, str)
            or _SHA256.fullmatch(self.digest) is None
            or self.media_type not in {"text/html", "application/zip"}
            or isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 1 <= self.size_bytes <= 536_870_912
        ):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)

    @classmethod
    def from_body(cls, body: object) -> ObjectSourceDescriptor:
        if (
            not isinstance(body, Mapping)
            or set(body) != OBJECT_SOURCE_DESCRIPTOR_FIELDS
            or body.get("kind") != "object"
        ):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)
        return cls(
            digest=body.get("digest"),  # type: ignore[arg-type]
            media_type=body.get("mediaType"),  # type: ignore[arg-type]
            size_bytes=body.get("sizeBytes"),  # type: ignore[arg-type]
        )

    def public_body(self) -> dict[str, Any]:
        return {
            "kind": "object",
            "digest": self.digest,
            "mediaType": self.media_type,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ObjectSourceRedemptionRequest:
    lease_id: str
    builder_task_id: str
    external_operation_id: str
    principal_ref: str
    tenant_ref: str
    application_ref: str
    object: ObjectSourceDescriptor

    def __post_init__(self) -> None:
        try:
            require_opaque_id(self.lease_id, prefix="lease")
            require_opaque_id(self.external_operation_id, prefix="op")
            require_opaque_id(self.tenant_ref, prefix="ten")
            require_opaque_id(self.application_ref, prefix="app")
            if (
                not isinstance(self.builder_task_id, str)
                or _UPSTREAM_TASK.fullmatch(self.builder_task_id) is None
                or not isinstance(self.principal_ref, str)
                or _PRINCIPAL.fullmatch(self.principal_ref) is None
                or not isinstance(self.object, ObjectSourceDescriptor)
            ):
                raise ValueError("invalid object-source binding")
        except (TypeError, ValueError) as exc:
            raise CredentialLeaseRejected(_GENERIC_REJECTION) from exc

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> ObjectSourceRedemptionRequest:
        if (
            not isinstance(body, Mapping)
            or set(body) != OBJECT_SOURCE_REDEMPTION_FIELDS
            or body.get("schemaVersion")
            != OBJECT_SOURCE_REDEMPTION_REQUEST_SCHEMA
        ):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)
        string_fields = {
            "lease_id": body.get("leaseId"),
            "builder_task_id": body.get("builderTaskId"),
            "external_operation_id": body.get("externalOperationId"),
            "principal_ref": body.get("principalRef"),
            "tenant_ref": body.get("tenantRef"),
            "application_ref": body.get("applicationRef"),
        }
        if any(not isinstance(value, str) for value in string_fields.values()):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)
        return cls(
            **string_fields,  # type: ignore[arg-type]
            object=ObjectSourceDescriptor.from_body(body.get("object")),
        )

    def binding_body(self) -> dict[str, Any]:
        return {
            "leaseId": self.lease_id,
            "builderTaskId": self.builder_task_id,
            "externalOperationId": self.external_operation_id,
            "principalRef": self.principal_ref,
            "tenantRef": self.tenant_ref,
            "applicationRef": self.application_ref,
            "object": self.object.public_body(),
        }


@dataclass(frozen=True, slots=True)
class ObjectSourceRedemptionClaim:
    request: ObjectSourceRedemptionRequest
    allowed_host: str
    ttl_seconds: int
    object_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_host, str)
            or _OBJECT_HOST.fullmatch(self.allowed_host) is None
            or self.allowed_host != self.allowed_host.lower()
            or isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or not 1
            <= self.ttl_seconds
            <= OBJECT_SOURCE_REDEMPTION_MAX_TTL_SECONDS
            or not isinstance(self.object_key, str)
            or not self.object_key
        ):
            raise CredentialLeaseRejected(_GENERIC_REJECTION)


@dataclass(frozen=True, slots=True)
class ObjectSourceRedemptionResult:
    request: ObjectSourceRedemptionRequest
    expires_at: int
    allowed_host: str
    object_url: str = field(repr=False)

    def public_body(self) -> dict[str, Any]:
        return {
            "schemaVersion": OBJECT_SOURCE_REDEMPTION_RESULT_SCHEMA,
            **self.request.binding_body(),
            "method": "GET",
            "expiresAt": self.expires_at,
            "objectUrl": self.object_url,
            "allowedHost": self.allowed_host,
        }


class PostgresObjectSourceRedemptionBroker:
    """Consume an exact upload-source lease and reveal only its hidden key.

    The returned key stays inside the API process and is immediately exchanged
    for a descriptor-bound, short-lived signed GET by the HTTP service layer.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def redeem(
        self, request: ObjectSourceRedemptionRequest
    ) -> ObjectSourceRedemptionClaim:
        async with self._sessions() as session:
            async with session.begin():
                reference = (
                    await session.execute(
                        select(
                            SourceCredentialLease.tenant_id,
                            SourceCredentialLease.source_connection_id,
                        ).where(SourceCredentialLease.id == request.lease_id)
                    )
                ).one_or_none()
                if reference is None or reference.source_connection_id is not None:
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)

                lease = await session.scalar(
                    select(SourceCredentialLease)
                    .where(
                        SourceCredentialLease.id == request.lease_id,
                        SourceCredentialLease.tenant_id == reference.tenant_id,
                        SourceCredentialLease.source_connection_id.is_(None),
                    )
                    .with_for_update()
                )
                now = await session.scalar(select(func.clock_timestamp()))
                if now is None or not self._valid_open_lease(
                    lease, request=request, now=now
                ):
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)
                assert lease is not None

                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.tenant_id == request.tenant_ref,
                        Operation.id == request.external_operation_id,
                    )
                    .with_for_update()
                )
                task = await session.scalar(
                    select(BuilderTask)
                    .where(
                        BuilderTask.tenant_id == request.tenant_ref,
                        BuilderTask.id == lease.builder_task_id,
                    )
                    .with_for_update()
                )
                source = await session.scalar(
                    select(SourceRevision).where(
                        SourceRevision.tenant_id == request.tenant_ref,
                        SourceRevision.id == lease.source_revision_id,
                        SourceRevision.application_id == request.application_ref,
                    )
                )
                upload = None
                if source is not None and source.upload_id is not None:
                    # Upload deletion takes this same row lock first. Waiting
                    # here ensures we observe its terminal state rather than
                    # signing a URL concurrently with cleanup.
                    upload = await session.scalar(
                        select(Upload)
                        .where(
                            Upload.tenant_id == request.tenant_ref,
                            Upload.id == source.upload_id,
                            Upload.application_id == request.application_ref,
                        )
                        .with_for_update()
                    )
                    source = await session.scalar(
                        select(SourceRevision).where(
                            SourceRevision.tenant_id == request.tenant_ref,
                            SourceRevision.id == lease.source_revision_id,
                            SourceRevision.application_id
                            == request.application_ref,
                        )
                    )
                application = await session.scalar(
                    select(Application).where(
                        Application.tenant_id == request.tenant_ref,
                        Application.id == request.application_ref,
                    )
                )
                if not self._valid_graph(
                    request,
                    lease=lease,
                    task=task,
                    operation=operation,
                    source=source,
                    upload=upload,
                    application=application,
                ):
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)
                assert upload is not None

                ttl_seconds = min(
                    OBJECT_SOURCE_REDEMPTION_MAX_TTL_SECONDS,
                    int((lease.expires_at - now).total_seconds()),
                )
                if ttl_seconds < 1:
                    raise CredentialLeaseRejected(_GENERIC_REJECTION)

                lease.status = "consumed"
                lease.consumed_at = now
                lease.updated_at = now
                await session.flush()
                return ObjectSourceRedemptionClaim(
                    request=request,
                    allowed_host=lease.allowed_host,
                    ttl_seconds=ttl_seconds,
                    object_key=upload.object_key,
                )

    @staticmethod
    def _valid_open_lease(
        lease: SourceCredentialLease | None,
        *,
        request: ObjectSourceRedemptionRequest,
        now: datetime,
    ) -> bool:
        return bool(
            lease is not None
            and lease.status == "issued"
            and lease.consumed_at is None
            and lease.revoked_at is None
            and lease.expires_at > now
            and lease.allowed_action == "source.fetch"
            and lease.source_connection_id is None
            and hmac.compare_digest(lease.tenant_id, request.tenant_ref)
            and hmac.compare_digest(
                lease.operation_id, request.external_operation_id
            )
            and hmac.compare_digest(lease.consumer_id, request.principal_ref)
        )

    @staticmethod
    def _valid_graph(
        request: ObjectSourceRedemptionRequest,
        *,
        lease: SourceCredentialLease,
        task: BuilderTask | None,
        operation: Operation | None,
        source: SourceRevision | None,
        upload: Upload | None,
        application: Application | None,
    ) -> bool:
        descriptor = request.object
        return bool(
            task is not None
            and task.action == "source.analyze"
            and task.cancel_forwarded_at is None
            and task.upstream_status not in {"failed", "timed_out", "canceled"}
            and hmac.compare_digest(task.tenant_id, request.tenant_ref)
            and hmac.compare_digest(task.application_id, request.application_ref)
            and hmac.compare_digest(task.source_revision_id, lease.source_revision_id)
            and hmac.compare_digest(task.operation_id, request.external_operation_id)
            and hmac.compare_digest(task.id, lease.builder_task_id)
            and hmac.compare_digest(task.credential_lease_id, request.lease_id)
            and task.luma_task_id is not None
            and hmac.compare_digest(task.luma_task_id, request.builder_task_id)
            and hmac.compare_digest(task.luma_principal_id, request.principal_ref)
            and operation is not None
            and operation.kind == "source.analyze"
            and operation.target_type == "source-revision"
            and operation.status in {"queued", "running"}
            and operation.cancel_requested_at is None
            and hmac.compare_digest(operation.tenant_id, request.tenant_ref)
            and hmac.compare_digest(operation.id, request.external_operation_id)
            and application is not None
            and application.deleted_at is None
            and hmac.compare_digest(application.tenant_id, request.tenant_ref)
            and hmac.compare_digest(application.id, request.application_ref)
            and source is not None
            and source.kind == "upload"
            and source.deleted_at is None
            and source.connection_id is None
            and source.repository is None
            and source.ref is None
            and source.upload_id is not None
            and hmac.compare_digest(source.id, lease.source_revision_id)
            and hmac.compare_digest(source.application_id or "", request.application_ref)
            and hmac.compare_digest(operation.target_id, source.id)
            and upload is not None
            and upload.status == "ready"
            and upload.deleted_at is None
            and upload.source_revision_id is not None
            and hmac.compare_digest(upload.tenant_id, request.tenant_ref)
            and hmac.compare_digest(upload.application_id, request.application_ref)
            and hmac.compare_digest(upload.id, source.upload_id)
            and hmac.compare_digest(upload.source_revision_id, source.id)
            and upload.actual_sha256 is not None
            and hmac.compare_digest(upload.actual_sha256, descriptor.digest)
            and hmac.compare_digest(upload.media_type, descriptor.media_type)
            and upload.actual_bytes == descriptor.size_bytes
            and lease.consumer_binding_hash is None
            and lease.binding_key_version is None
            and _OBJECT_HOST.fullmatch(lease.allowed_host) is not None
            and lease.allowed_host == lease.allowed_host.lower()
        )


__all__ = [
    "OBJECT_SOURCE_DESCRIPTOR_FIELDS",
    "OBJECT_SOURCE_REDEMPTION_FIELDS",
    "OBJECT_SOURCE_REDEMPTION_MAX_TTL_SECONDS",
    "OBJECT_SOURCE_REDEMPTION_REQUEST_SCHEMA",
    "OBJECT_SOURCE_REDEMPTION_RESULT_SCHEMA",
    "ObjectSourceDescriptor",
    "ObjectSourceRedemptionClaim",
    "ObjectSourceRedemptionRequest",
    "ObjectSourceRedemptionResult",
    "PostgresObjectSourceRedemptionBroker",
]
