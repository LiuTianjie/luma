from __future__ import annotations

import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import (
    IdempotencyKeyReused,
    InvalidPlanLimits,
    LeaseLost,
    OperationConflict,
    ResourceNotFound,
    SubscriptionUnavailable,
    UploadConflict,
    UploadQuotaExceeded,
)
from .ids import new_id, require_opaque_id
from .models import (
    Analysis,
    Application,
    BuilderTask,
    IdempotencyRecord,
    Operation,
    OutboxEvent,
    PlanVersion,
    SourceCredentialLease,
    SourceRevision,
    Subscription,
    Upload,
)
from .repositories import EventInput, IdempotencyInput, Principal, TenantScope, _append_event
from .security import ensure_persistable_payload
from .tokens import keyed_request_hash, keyed_secret_hash

UPLOAD_CREATE_ROUTE = "/v1/uploads"
UPLOAD_COMPLETE_ROUTE = "/v1/uploads/{upload_id}/complete"
UPLOAD_DELETE_ROUTE = "/v1/uploads/{upload_id}"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FAILURE_CODE = re.compile(r"^LAE_UPLOAD_[A-Z0-9_]{1,80}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBJECT_HOST = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[1-9][0-9]{0,4})?$"
)
_ACTIVE_QUOTA_STATUSES = (
    "quarantine",
    "verifying",
    "scanning",
    "ready",
    "deleting",
)


@dataclass(frozen=True, slots=True)
class CreateStaticUpload:
    scope: TenantScope
    principal: Principal
    application_id: str
    filename: str
    media_type: str
    expected_bytes: int
    expected_sha256: str
    idempotency_key: str
    kind: str = field(init=False)

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        canonical_name, kind, media_type = canonical_upload_identity(
            self.filename, self.media_type
        )
        object.__setattr__(self, "filename", canonical_name)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "expected_sha256", canonical_digest(self.expected_sha256))
        if (
            not isinstance(self.expected_bytes, int)
            or isinstance(self.expected_bytes, bool)
            or not 0 < self.expected_bytes <= 536_870_912
        ):
            raise ValueError("upload size is invalid")
        object.__setattr__(self, "kind", kind)
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template=UPLOAD_CREATE_ROUTE,
            request_hash=b"\0" * 32,
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "applicationId": self.application_id,
            "filename": self.filename,
            "mediaType": self.media_type,
            "sizeBytes": self.expected_bytes,
            "sha256": self.expected_sha256,
        }


@dataclass(frozen=True, slots=True)
class CompleteStaticUpload:
    scope: TenantScope
    principal: Principal
    upload_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        require_opaque_id(self.upload_id, prefix="upl")
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template=UPLOAD_COMPLETE_ROUTE,
            request_hash=b"\0" * 32,
        )


@dataclass(frozen=True, slots=True)
class UploadRecord:
    id: str
    application_id: str
    operation_id: str
    source_revision_id: str | None
    filename: str
    kind: str
    media_type: str
    expected_bytes: int
    actual_bytes: int | None
    expected_sha256: str
    actual_sha256: str | None
    status: str
    cleanup_status: str
    failure_code: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    operation_status: str
    object_key: str = field(repr=False)

    def public_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "applicationId": self.application_id,
            "filename": self.filename,
            "kind": self.kind,
            "mediaType": self.media_type,
            "expectedBytes": self.expected_bytes,
            "actualBytes": self.actual_bytes,
            "sha256": self.actual_sha256 or self.expected_sha256,
            "status": self.status,
            "cleanupStatus": self.cleanup_status,
            "sourceRevisionId": self.source_revision_id,
            "expiresAt": _timestamp(self.expires_at),
            "createdAt": _timestamp(self.created_at),
            "updatedAt": _timestamp(self.updated_at),
        }
        if self.failure_code is not None:
            body["failureCode"] = self.failure_code
        return body


@dataclass(frozen=True, slots=True)
class UploadReservationResult:
    upload: UploadRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class UploadCompletionClaim:
    upload: UploadRecord
    owns_verification: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class UploadScanClaim:
    upload: UploadRecord
    tenant_id: str
    max_unpacked_bytes: int = 134_217_728


@dataclass(frozen=True, slots=True)
class CreateUploadAnalysis:
    scope: TenantScope
    principal: Principal
    application_id: str
    upload_id: str
    region: str
    public_protocols: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        require_opaque_id(self.application_id, prefix="app")
        require_opaque_id(self.upload_id, prefix="upl")
        if self.region not in {"cn", "global"}:
            raise ValueError("analysis region is invalid")
        if self.public_protocols != ("http",):
            raise ValueError("only the public HTTP protocol is supported")
        IdempotencyInput(
            key=self.idempotency_key,
            method="POST",
            route_template="/v1/analyses",
            request_hash=b"\0" * 32,
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "applicationId": self.application_id,
            "source": {"type": "upload", "uploadId": self.upload_id},
            "intent": {
                "region": self.region,
                "publicProtocols": list(self.public_protocols),
            },
        }


@dataclass(frozen=True, slots=True)
class UploadAnalysisRecord:
    analysis_id: str
    operation_id: str
    source_revision_id: str
    application_id: str
    replayed: bool

    def public_body(self) -> dict[str, Any]:
        return {
            "analysis": {"id": self.analysis_id, "status": "queued"},
            "operation": {"id": self.operation_id, "status": "queued"},
            "links": {
                "analysis": f"/v1/analyses/{self.analysis_id}",
                "events": f"/v1/operations/{self.operation_id}/events",
            },
        }


class PostgresUploadStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        hash_key: bytes,
        reservation_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        if not isinstance(hash_key, bytes) or len(hash_key) < 32:
            raise ValueError("upload HMAC key must contain at least 256 bits")
        if not timedelta(minutes=5) <= reservation_ttl <= timedelta(hours=24):
            raise ValueError("upload reservation TTL is invalid")
        self._sessions = sessions
        self._hash_key = hash_key
        self._reservation_ttl = reservation_ttl

    async def create(self, command: CreateStaticUpload) -> UploadReservationResult:
        request_hash = keyed_request_hash(command.hash_payload(), self._hash_key)
        idempotency = IdempotencyInput(
            key=command.idempotency_key,
            method="POST",
            route_template=UPLOAD_CREATE_ROUTE,
            request_hash=request_hash,
        )
        lock_scope = (
            f"upload-create:{command.scope.tenant_id}:{command.principal.type}:"
            f"{command.principal.id}:{idempotency.key}"
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await _advisory_lock(session, lock_scope)
                    now = await _database_now(session)
                    existing = await _find_idempotency(
                        session, command.scope, command.principal, idempotency
                    )
                    if existing is not None and existing.expires_at > now:
                        if not hmac.compare_digest(
                            existing.request_hash, idempotency.request_hash
                        ):
                            raise IdempotencyKeyReused(
                                "idempotency key was used for another request"
                            )
                        upload = await self._get_for_operation(
                            session, command.scope, existing.operation_id
                        )
                        return UploadReservationResult(upload, replayed=True)
                    if existing is not None:
                        await session.delete(existing)
                        await session.flush()

                    application = await session.scalar(
                        select(Application)
                        .where(
                            Application.tenant_id == command.scope.tenant_id,
                            Application.id == command.application_id,
                            Application.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if application is None:
                        raise ResourceNotFound("application not found")
                    await _advisory_lock(
                        session, f"upload-quota:{command.scope.tenant_id}"
                    )
                    limits = await _active_plan_limits(session, command.scope)
                    max_upload = _positive_limit(limits, "maxUploadBytes")
                    total_upload = _positive_limit(limits, "uploadBytes")
                    if command.expected_bytes > max_upload:
                        raise UploadQuotaExceeded("single upload quota exceeded")
                    used = await session.scalar(
                        select(
                            func.coalesce(
                                func.sum(
                                    case(
                                        (
                                            Upload.status == "ready",
                                            Upload.actual_bytes,
                                        ),
                                        else_=Upload.expected_bytes,
                                    )
                                ),
                                0,
                            )
                        ).where(
                            Upload.tenant_id == command.scope.tenant_id,
                            Upload.status.in_(_ACTIVE_QUOTA_STATUSES),
                        )
                    )
                    if int(used or 0) + command.expected_bytes > total_upload:
                        raise UploadQuotaExceeded("tenant upload quota exceeded")

                    upload_id = new_id("upl")
                    operation = Operation(
                        id=new_id("op"),
                        tenant_id=command.scope.tenant_id,
                        principal_type=command.principal.type,
                        principal_id=command.principal.id,
                        kind="source.upload.scan",
                        target_type="upload",
                        target_id=upload_id,
                        status="queued",
                        phase="source.upload",
                        last_event_seq=0,
                    )
                    session.add(operation)
                    await session.flush()
                    kind = "html" if command.filename.lower().endswith(".html") else "zip"
                    object_key = _new_object_key(
                        command.scope.tenant_id,
                        application.id,
                        upload_id,
                        kind,
                    )
                    upload_row = Upload(
                        id=upload_id,
                        tenant_id=command.scope.tenant_id,
                        application_id=application.id,
                        operation_id=operation.id,
                        source_revision_id=None,
                        kind=kind,
                        filename=command.filename,
                        media_type=command.media_type,
                        object_key=object_key,
                        expected_bytes=command.expected_bytes,
                        actual_bytes=None,
                        expected_sha256=command.expected_sha256,
                        actual_sha256=None,
                        status="quarantine",
                        cleanup_status="none",
                        cleanup_attempts=0,
                        expires_at=now + self._reservation_ttl,
                    )
                    session.add(upload_row)
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.queued",
                            phase="source.upload",
                            status="queued",
                            message="Operation queued",
                            data={},
                        ),
                    )
                    await session.flush()
                    record = _record(upload_row, operation)
                    safe_body = {
                        "upload": record.public_body(),
                        "operation": {"id": operation.id, "status": operation.status},
                    }
                    ensure_persistable_payload(safe_body)
                    session.add(
                        IdempotencyRecord(
                            tenant_id=command.scope.tenant_id,
                            principal_type=command.principal.type,
                            principal_id=command.principal.id,
                            key=idempotency.key,
                            method=idempotency.method,
                            route_template=idempotency.route_template,
                            request_hash=idempotency.request_hash,
                            response_status=201,
                            response_body=safe_body,
                            operation_id=operation.id,
                            expires_at=now + idempotency.retention,
                        )
                    )
                    await session.flush()
                    return UploadReservationResult(record, replayed=False)
        except (ResourceNotFound, IdempotencyKeyReused, UploadQuotaExceeded):
            raise
        except IntegrityError as exc:
            raise UploadConflict("upload reservation conflicts with durable state") from exc
        except DBAPIError:
            raise

    async def get(self, scope: TenantScope, upload_id: str) -> UploadRecord:
        require_opaque_id(upload_id, prefix="upl")
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Upload, Operation)
                    .join(
                        Operation,
                        (Operation.tenant_id == Upload.tenant_id)
                        & (Operation.id == Upload.operation_id),
                    )
                    .where(Upload.tenant_id == scope.tenant_id, Upload.id == upload_id)
                )
            ).one_or_none()
            if row is None:
                raise ResourceNotFound("upload not found")
            return _record(*row)

    async def claim_completion(
        self, command: CompleteStaticUpload
    ) -> UploadCompletionClaim:
        key_hash = keyed_secret_hash(
            command.idempotency_key,
            self._hash_key,
            domain="lae.upload-complete-idempotency.v1",
        )
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(session, command.scope, command.upload_id)
                now = await _database_now(session)
                if operation.status == "canceled" or operation.cancel_requested_at is not None:
                    raise UploadConflict("upload operation was canceled")
                if upload.expires_at <= now and upload.status in {"quarantine", "verifying"}:
                    raise UploadConflict("upload reservation expired")
                if upload.complete_idempotency_key_hash is not None:
                    if not hmac.compare_digest(
                        upload.complete_idempotency_key_hash, key_hash
                    ):
                        raise IdempotencyKeyReused(
                            "upload completion used another idempotency key"
                        )
                    return UploadCompletionClaim(
                        _record(upload, operation),
                        # Verification is intentionally safe to repeat.  A
                        # retry must be able to recover after the API process
                        # committed ``verifying`` and crashed before the
                        # object read; concurrent same-key reads converge in
                        # ``mark_scanning`` on the exact digest and size.
                        owns_verification=upload.status == "verifying",
                        replayed=True,
                    )
                if upload.status != "quarantine":
                    raise UploadConflict("upload cannot be completed in its current state")
                upload.complete_idempotency_key_hash = key_hash
                upload.status = "verifying"
                return UploadCompletionClaim(
                    _record(upload, operation), owns_verification=True, replayed=False
                )

    async def mark_scanning(
        self,
        scope: TenantScope,
        upload_id: str,
        *,
        actual_bytes: int,
        actual_sha256: str,
    ) -> UploadRecord:
        canonical = canonical_digest(actual_sha256)
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(session, scope, upload_id)
                if upload.status in {"scanning", "ready"}:
                    if upload.actual_bytes != actual_bytes or upload.actual_sha256 != canonical:
                        raise UploadConflict("verified upload facts changed")
                    return _record(upload, operation)
                if upload.status != "verifying":
                    raise UploadConflict("upload verification is not active")
                if actual_bytes != upload.expected_bytes or canonical != upload.expected_sha256:
                    raise UploadConflict("verified upload does not match reservation")
                now = await _database_now(session)
                upload.actual_bytes = actual_bytes
                upload.actual_sha256 = canonical
                upload.status = "scanning"
                upload.completed_at = now
                upload.expires_at = now + timedelta(days=1)
                operation.status = "running"
                operation.phase = "source.upload.scan"
                operation.started_at = operation.started_at or now
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type="operation.started",
                        phase="source.upload.scan",
                        status="running",
                        message="Operation started",
                        data={},
                    ),
                )
                session.add(
                    OutboxEvent(
                        tenant_id=scope.tenant_id,
                        aggregate_type="upload",
                        aggregate_id=upload.id,
                        event_type="upload.scan.requested",
                        dedupe_key=f"upload-scan:{upload.id}:v1",
                        payload={
                            "uploadId": upload.id,
                            "applicationId": upload.application_id,
                            "operationId": operation.id,
                        },
                    )
                )
                await session.flush()
                return _record(upload, operation)

    async def mark_failed(
        self, scope: TenantScope, upload_id: str, *, failure_code: str
    ) -> UploadRecord:
        if not _FAILURE_CODE.fullmatch(failure_code):
            raise ValueError("upload failure code is invalid")
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(session, scope, upload_id)
                if upload.status == "failed":
                    return _record(upload, operation)
                if upload.status in {"ready", "deleted", "deleting"}:
                    raise UploadConflict("terminal upload cannot fail")
                now = await _database_now(session)
                upload.status = "failed"
                upload.failure_code = failure_code
                upload.cleanup_status = "pending"
                upload.scan_lease_owner = None
                upload.scan_lease_expires_at = None
                operation.status = "failed"
                operation.phase = "source.upload.scan"
                operation.finished_at = now
                operation.error_code = failure_code
                operation.error_message = "Static source validation failed"
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type="operation.failed",
                        phase="source.upload.scan",
                        status="failed",
                        level="error",
                        message="Operation failed",
                        data={"errorCode": failure_code},
                    ),
                )
                return _record(upload, operation)

    async def fail_scan(
        self,
        claim: UploadScanClaim,
        *,
        worker_id: str,
        failure_code: str,
    ) -> UploadRecord:
        if not _FAILURE_CODE.fullmatch(failure_code):
            raise ValueError("upload failure code is invalid")
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(
                    session, TenantScope(claim.tenant_id), claim.upload.id
                )
                if upload.status == "failed":
                    return _record(upload, operation)
                if upload.status != "scanning" or upload.scan_lease_owner != worker_id:
                    raise LeaseLost("upload scanner lease was lost")
                now = await _database_now(session)
                upload.status = "failed"
                upload.failure_code = failure_code
                upload.cleanup_status = "pending"
                upload.scan_lease_owner = None
                upload.scan_lease_expires_at = None
                operation.status = "failed"
                operation.phase = "source.upload.scan"
                operation.finished_at = now
                operation.error_code = failure_code
                operation.error_message = "Static source validation failed"
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type="operation.failed",
                        phase="source.upload.scan",
                        status="failed",
                        level="error",
                        message="Operation failed",
                        data={"errorCode": failure_code},
                    ),
                )
                return _record(upload, operation)

    async def claim_scan(
        self, *, worker_id: str, lease_ttl: timedelta = timedelta(minutes=5)
    ) -> UploadScanClaim | None:
        if not _IDENTIFIER.fullmatch(worker_id):
            raise ValueError("scanner worker id is invalid")
        if not timedelta(seconds=30) <= lease_ttl <= timedelta(minutes=15):
            raise ValueError("scanner lease TTL is invalid")
        async with self._sessions() as session:
            async with session.begin():
                now = await _database_now(session)
                upload = await session.scalar(
                    select(Upload)
                    .where(
                        Upload.status == "scanning",
                        or_(
                            Upload.scan_lease_owner.is_(None),
                            Upload.scan_lease_expires_at <= now,
                        ),
                    )
                    .order_by(Upload.created_at, Upload.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if upload is None:
                    return None
                operation = await session.scalar(
                    select(Operation).where(
                        Operation.tenant_id == upload.tenant_id,
                        Operation.id == upload.operation_id,
                    )
                )
                if operation is None:
                    raise OperationConflict("upload operation is missing")
                upload.scan_lease_owner = worker_id
                upload.scan_lease_expires_at = now + lease_ttl
                limits = await _active_plan_limits(
                    session, TenantScope(upload.tenant_id)
                )
                return UploadScanClaim(
                    _record(upload, operation),
                    upload.tenant_id,
                    _positive_limit(limits, "maxUnpackedBytes"),
                )

    async def finish_scan(
        self,
        claim: UploadScanClaim,
        *,
        worker_id: str,
        source_tree_digest: str,
    ) -> UploadRecord:
        tree_digest = canonical_digest(source_tree_digest)
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(
                    session, TenantScope(claim.tenant_id), claim.upload.id
                )
                if upload.status == "ready":
                    return _record(upload, operation)
                if upload.status != "scanning" or upload.scan_lease_owner != worker_id:
                    raise LeaseLost("upload scanner lease was lost")
                if upload.actual_sha256 is None or upload.actual_bytes is None:
                    raise UploadConflict("upload verification facts are incomplete")
                now = await _database_now(session)
                source = SourceRevision(
                    id=new_id("src"),
                    tenant_id=upload.tenant_id,
                    application_id=upload.application_id,
                    kind="upload",
                    connection_id=None,
                    repository=None,
                    ref=None,
                    resolved_commit_full=None,
                    source_tree_digest=tree_digest,
                    upload_id=upload.id,
                    template_version_id=None,
                    subdirectory="",
                    snapshot_id=f"upload:{upload.id}",
                    snapshot_digest=upload.actual_sha256,
                    snapshot_artifact_id=None,
                )
                session.add(source)
                await session.flush()
                upload.source_revision_id = source.id
                upload.status = "ready"
                upload.ready_at = now
                upload.expires_at = now + timedelta(days=30)
                upload.scan_lease_owner = None
                upload.scan_lease_expires_at = None
                operation.status = "succeeded"
                operation.phase = "source.upload.scan"
                operation.finished_at = now
                operation.result = {"sourceRevisionId": source.id}
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type="operation.succeeded",
                        phase="source.upload.scan",
                        status="succeeded",
                        message="Operation succeeded",
                        data={},
                    ),
                )
                await session.flush()
                return _record(upload, operation)

    async def scan_cancel_requested(self, claim: UploadScanClaim) -> bool:
        async with self._sessions() as session:
            operation = await session.scalar(
                select(Operation).where(
                    Operation.tenant_id == claim.tenant_id,
                    Operation.id == claim.upload.operation_id,
                )
            )
            if operation is None:
                raise ResourceNotFound("upload operation not found")
            return operation.cancel_requested_at is not None or operation.status == "canceled"

    async def mark_scan_canceled(
        self, claim: UploadScanClaim, *, worker_id: str
    ) -> UploadRecord:
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(
                    session, TenantScope(claim.tenant_id), claim.upload.id
                )
                if upload.status in {"failed", "deleted"}:
                    return _record(upload, operation)
                if upload.status != "scanning" or upload.scan_lease_owner != worker_id:
                    raise LeaseLost("upload scanner lease was lost")
                now = await _database_now(session)
                upload.status = "failed"
                upload.failure_code = "LAE_UPLOAD_CANCELED"
                upload.cleanup_status = "pending"
                upload.scan_lease_owner = None
                upload.scan_lease_expires_at = None
                operation.status = "canceled"
                operation.phase = "source.upload.scan"
                operation.finished_at = now
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type="operation.canceled",
                        phase="source.upload.scan",
                        status="canceled",
                        message="Operation canceled",
                        data={},
                    ),
                )
                return _record(upload, operation)

    async def delete(self, scope: TenantScope, upload_id: str) -> UploadRecord:
        require_opaque_id(upload_id, prefix="upl")
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(session, scope, upload_id)
                if upload.status == "deleted":
                    return _record(upload, operation)
                if upload.source_revision_id is not None:
                    source = await session.scalar(
                        select(SourceRevision).where(
                            SourceRevision.tenant_id == scope.tenant_id,
                            SourceRevision.id == upload.source_revision_id,
                        )
                    )
                    if source is not None:
                        source.deleted_at = await _database_now(session)
                    upload.source_revision_id = None
                upload.status = "deleting"
                upload.cleanup_status = "deleting"
                upload.scan_lease_owner = None
                upload.scan_lease_expires_at = None
                return _record(upload, operation)

    async def finish_delete(self, scope: TenantScope, upload_id: str) -> UploadRecord:
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(session, scope, upload_id)
                if upload.status == "deleted":
                    return _record(upload, operation)
                if upload.status not in {"deleting", "expired", "failed"}:
                    raise UploadConflict("upload is not pending cleanup")
                now = await _database_now(session)
                upload.status = "deleted"
                upload.cleanup_status = "deleted"
                upload.deleted_at = now
                upload.scan_lease_owner = None
                upload.scan_lease_expires_at = None
                if operation.status not in {"succeeded", "failed", "canceled"}:
                    operation.status = "canceled"
                    operation.finished_at = now
                return _record(upload, operation)

    async def claim_cleanup(self) -> UploadScanClaim | None:
        async with self._sessions() as session:
            async with session.begin():
                now = await _database_now(session)
                upload = await session.scalar(
                    select(Upload)
                    .where(
                        Upload.status.in_(("failed", "expired", "deleting")),
                        Upload.cleanup_attempts < 10,
                        or_(
                            Upload.cleanup_status.in_(("pending", "failed")),
                            (
                                (Upload.cleanup_status == "deleting")
                                & (Upload.updated_at < now - timedelta(minutes=5))
                            ),
                        ),
                    )
                    .order_by(Upload.updated_at, Upload.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if upload is None:
                    return None
                operation = await session.scalar(
                    select(Operation).where(
                        Operation.tenant_id == upload.tenant_id,
                        Operation.id == upload.operation_id,
                    )
                )
                if operation is None:
                    raise OperationConflict("upload operation is missing")
                upload.cleanup_status = "deleting"
                upload.cleanup_attempts += 1
                upload.updated_at = now
                return UploadScanClaim(_record(upload, operation), upload.tenant_id)

    async def mark_cleanup_failed(self, claim: UploadScanClaim) -> UploadRecord:
        async with self._sessions() as session:
            async with session.begin():
                upload, operation = await self._locked(
                    session, TenantScope(claim.tenant_id), claim.upload.id
                )
                if upload.status == "deleted":
                    return _record(upload, operation)
                if upload.cleanup_status != "deleting":
                    raise UploadConflict("upload cleanup is not active")
                upload.cleanup_status = "failed"
                return _record(upload, operation)

    async def expire_stale(self, *, limit: int = 100) -> list[UploadScanClaim]:
        if not 1 <= limit <= 1000:
            raise ValueError("expiry batch limit is invalid")
        async with self._sessions() as session:
            async with session.begin():
                now = await _database_now(session)
                rows = list(
                    (
                        await session.scalars(
                            select(Upload)
                            .where(
                                Upload.status.in_(("quarantine", "verifying")),
                                Upload.expires_at <= now,
                            )
                            .order_by(Upload.expires_at, Upload.id)
                            .with_for_update(skip_locked=True)
                            .limit(limit)
                        )
                    ).all()
                )
                expired: list[UploadScanClaim] = []
                for upload in rows:
                    operation = await session.scalar(
                        select(Operation).where(
                            Operation.tenant_id == upload.tenant_id,
                            Operation.id == upload.operation_id,
                        )
                    )
                    if operation is None:
                        continue
                    upload.status = "expired"
                    upload.cleanup_status = "pending"
                    upload.failure_code = "LAE_UPLOAD_EXPIRED"
                    operation.status = "canceled"
                    operation.phase = "source.upload"
                    operation.finished_at = now
                    await _append_event(
                        session,
                        operation,
                        EventInput(
                            type="operation.canceled",
                            phase="source.upload",
                            status="canceled",
                            message="Operation canceled",
                            data={},
                        ),
                    )
                    expired.append(
                        UploadScanClaim(_record(upload, operation), upload.tenant_id)
                    )
                return expired

    async def _locked(
        self, session: AsyncSession, scope: TenantScope, upload_id: str
    ) -> tuple[Upload, Operation]:
        require_opaque_id(upload_id, prefix="upl")
        row = (
            await session.execute(
                select(Upload, Operation)
                .join(
                    Operation,
                    (Operation.tenant_id == Upload.tenant_id)
                    & (Operation.id == Upload.operation_id),
                )
                .where(Upload.tenant_id == scope.tenant_id, Upload.id == upload_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ResourceNotFound("upload not found")
        return row

    async def _get_for_operation(
        self, session: AsyncSession, scope: TenantScope, operation_id: str
    ) -> UploadRecord:
        row = (
            await session.execute(
                select(Upload, Operation)
                .join(
                    Operation,
                    (Operation.tenant_id == Upload.tenant_id)
                    & (Operation.id == Upload.operation_id),
                )
                .where(
                    Upload.tenant_id == scope.tenant_id,
                    Upload.operation_id == operation_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise OperationConflict("idempotent upload state is incomplete")
        return _record(*row)


class PostgresUploadAnalysisStore:
    """Analysis admission for an already-scanned immutable upload source."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        luma_cluster_id: str,
        luma_principal_id: str,
        object_store_host: str,
        hash_key: bytes,
        hash_key_version: int = 1,
        lease_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        for value in (luma_cluster_id, luma_principal_id):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError("upload analysis binding is invalid")
        if not _OBJECT_HOST.fullmatch(object_store_host):
            raise ValueError("upload object-store host binding is invalid")
        if len(hash_key) < 32 or hash_key_version < 1:
            raise ValueError("upload analysis key is invalid")
        self._sessions = sessions
        self._cluster = luma_cluster_id
        self._principal = luma_principal_id
        self._object_host = object_store_host
        self._hash_key = hash_key
        self._hash_key_version = hash_key_version
        self._lease_ttl = lease_ttl

    async def create(self, command: CreateUploadAnalysis) -> UploadAnalysisRecord:
        request_hash = keyed_request_hash(command.hash_payload(), self._hash_key)
        idem = IdempotencyInput(
            key=command.idempotency_key,
            method="POST",
            route_template="/v1/analyses",
            request_hash=request_hash,
        )
        async with self._sessions() as session:
            async with session.begin():
                await _advisory_lock(
                    session,
                    f"upload-analysis:{command.scope.tenant_id}:"
                    f"{command.principal.type}:{command.principal.id}:{idem.key}",
                )
                now = await _database_now(session)
                existing = await _find_idempotency(
                    session, command.scope, command.principal, idem
                )
                if existing is not None and existing.expires_at > now:
                    if not hmac.compare_digest(existing.request_hash, idem.request_hash):
                        raise IdempotencyKeyReused("idempotency key was reused")
                    analysis = await session.scalar(
                        select(Analysis).where(
                            Analysis.tenant_id == command.scope.tenant_id,
                            Analysis.operation_id == existing.operation_id,
                        )
                    )
                    if analysis is None:
                        raise OperationConflict("idempotent upload analysis is incomplete")
                    return UploadAnalysisRecord(
                        analysis.id,
                        analysis.operation_id,
                        analysis.source_revision_id,
                        analysis.application_id,
                        True,
                    )
                row = (
                    await session.execute(
                        select(Upload, SourceRevision)
                        .join(
                            SourceRevision,
                            (SourceRevision.tenant_id == Upload.tenant_id)
                            & (SourceRevision.id == Upload.source_revision_id),
                        )
                        .where(
                            Upload.tenant_id == command.scope.tenant_id,
                            Upload.application_id == command.application_id,
                            Upload.id == command.upload_id,
                            Upload.status == "ready",
                            Upload.deleted_at.is_(None),
                            SourceRevision.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    raise ResourceNotFound("upload not found")
                upload, source = row
                operation = Operation(
                    id=new_id("op"),
                    tenant_id=command.scope.tenant_id,
                    principal_type=command.principal.type,
                    principal_id=command.principal.id,
                    kind="source.analyze",
                    target_type="source-revision",
                    target_id=source.id,
                    status="queued",
                    phase="source.analyze",
                    last_event_seq=0,
                )
                session.add(operation)
                await session.flush()
                lease_id = new_id("lease")
                task = BuilderTask(
                    id=new_id("btask"),
                    tenant_id=command.scope.tenant_id,
                    application_id=command.application_id,
                    source_revision_id=source.id,
                    operation_id=operation.id,
                    luma_cluster_id=self._cluster,
                    luma_principal_id=self._principal,
                    action="source.analyze",
                    credential_lease_id=lease_id,
                    idempotency_key_hash=keyed_secret_hash(
                        f"lae:{operation.id}:source-analyze:v1",
                        self._hash_key,
                        domain="lae.builder-idempotency.v1",
                    ),
                    request_digest=keyed_request_hash(
                        {
                            "action": "source.analyze",
                            "sourceType": "upload",
                            "tenantRef": command.scope.tenant_id,
                            "applicationRef": command.application_id,
                            "sourceRevisionRef": source.id,
                            "uploadRef": upload.id,
                            "digest": upload.actual_sha256,
                        },
                        self._hash_key,
                    ),
                    hash_key_version=self._hash_key_version,
                    event_cursor=0,
                    checkpoint_version=0,
                )
                session.add(task)
                await session.flush()
                session.add(
                    SourceCredentialLease(
                        id=lease_id,
                        tenant_id=command.scope.tenant_id,
                        source_connection_id=None,
                        source_revision_id=source.id,
                        operation_id=operation.id,
                        builder_task_id=task.id,
                        allowed_action="source.fetch",
                        allowed_host=self._object_host,
                        consumer_id=self._principal,
                        consumer_binding_hash=None,
                        binding_key_version=None,
                        status="issued",
                        expires_at=now + self._lease_ttl,
                    )
                )
                analysis = Analysis(
                    id=new_id("ana"),
                    tenant_id=command.scope.tenant_id,
                    application_id=command.application_id,
                    source_revision_id=source.id,
                    operation_id=operation.id,
                    status="queued",
                    artifact_state="descriptor-only",
                    plan_stored=False,
                )
                session.add(analysis)
                await _append_event(
                    session,
                    operation,
                    EventInput(
                        type="operation.queued",
                        phase="source.analyze",
                        status="queued",
                        message="Operation queued",
                        data={},
                    ),
                )
                record = UploadAnalysisRecord(
                    analysis.id, operation.id, source.id, command.application_id, False
                )
                safe_body = record.public_body()
                ensure_persistable_payload(safe_body)
                session.add(
                    IdempotencyRecord(
                        tenant_id=command.scope.tenant_id,
                        principal_type=command.principal.type,
                        principal_id=command.principal.id,
                        key=idem.key,
                        method=idem.method,
                        route_template=idem.route_template,
                        request_hash=idem.request_hash,
                        response_status=202,
                        response_body=safe_body,
                        operation_id=operation.id,
                        expires_at=now + idem.retention,
                    )
                )
                await session.flush()
                return record


def canonical_upload_identity(filename: str, media_type: str) -> tuple[str, str, str]:
    if not isinstance(filename, str):
        raise ValueError("upload filename is invalid")
    normalized = unicodedata.normalize("NFC", filename)
    if (
        not 1 <= len(normalized.encode("utf-8")) <= 255
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError("upload filename is invalid")
    lowered = normalized.lower()
    if lowered.endswith(".html") and media_type.lower() == "text/html":
        return normalized, "html", "text/html"
    if lowered.endswith(".zip") and media_type.lower() == "application/zip":
        return normalized, "zip", "application/zip"
    raise ValueError("only .html and .zip static artifacts are supported")


def canonical_digest(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("SHA-256 digest is invalid")
    canonical = value.lower()
    if not _DIGEST.fullmatch(canonical):
        raise ValueError("SHA-256 digest is invalid")
    return canonical


def _new_object_key(tenant_id: str, application_id: str, upload_id: str, kind: str) -> str:
    return (
        f"tenants/{tenant_id}/apps/{application_id}/quarantine/"
        f"{upload_id}/{secrets.token_hex(24)}.{kind}"
    )


def _record(upload: Upload, operation: Operation) -> UploadRecord:
    return UploadRecord(
        id=upload.id,
        application_id=upload.application_id,
        operation_id=upload.operation_id,
        source_revision_id=upload.source_revision_id,
        filename=upload.filename,
        kind=upload.kind,
        media_type=upload.media_type,
        expected_bytes=upload.expected_bytes,
        actual_bytes=upload.actual_bytes,
        expected_sha256=upload.expected_sha256,
        actual_sha256=upload.actual_sha256,
        status=upload.status,
        cleanup_status=upload.cleanup_status,
        failure_code=upload.failure_code,
        expires_at=upload.expires_at,
        created_at=upload.created_at,
        updated_at=upload.updated_at,
        operation_status=operation.status,
        object_key=upload.object_key,
    )


async def _database_now(session: AsyncSession) -> datetime:
    now = await session.scalar(select(func.now()))
    if now is None:
        raise OperationConflict("database clock is unavailable")
    return now


async def _advisory_lock(session: AsyncSession, scope: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": scope},
    )


async def _find_idempotency(
    session: AsyncSession,
    scope: TenantScope,
    principal: Principal,
    idempotency: IdempotencyInput,
) -> IdempotencyRecord | None:
    return await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.tenant_id == scope.tenant_id,
            IdempotencyRecord.principal_type == principal.type,
            IdempotencyRecord.principal_id == principal.id,
            IdempotencyRecord.method == idempotency.method,
            IdempotencyRecord.route_template == idempotency.route_template,
            IdempotencyRecord.key == idempotency.key,
        )
        .with_for_update()
    )


async def _active_plan_limits(
    session: AsyncSession, scope: TenantScope
) -> dict[str, Any]:
    limits = await session.scalar(
        select(PlanVersion.limits_json)
        .join(Subscription, Subscription.plan_version_id == PlanVersion.id)
        .where(
            Subscription.tenant_id == scope.tenant_id,
            Subscription.status.in_(("active", "trialing")),
        )
    )
    if limits is None:
        raise SubscriptionUnavailable("active subscription is unavailable")
    if not isinstance(limits, dict):
        raise InvalidPlanLimits("plan limits are invalid")
    return limits


def _positive_limit(limits: dict[str, Any], key: str) -> int:
    value = limits.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidPlanLimits(f"plan limit {key} is invalid")
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
