from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .errors import LumaError


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_CHUNK_BYTES = 1024 * 1024
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_LEASE_ID = re.compile(r"^artdl_[A-Za-z0-9][A-Za-z0-9_-]{31,95}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACTS = {
    "evidence": "application/vnd.lae.evidence+json",
    "deploymentPlan": "application/vnd.lae.deployment-plan+json",
    "buildPlan": "application/vnd.lae.build-plan-candidate+json",
}


@dataclass(frozen=True, slots=True)
class ArtifactLeaseBinding:
    principal_ref: str
    tenant_ref: str
    application_ref: str
    external_operation_id: str
    builder_task_id: str
    artifact_name: str
    digest: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        for value in (
            self.principal_ref,
            self.tenant_ref,
            self.application_ref,
            self.external_operation_id,
            self.builder_task_id,
        ):
            if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                raise LumaError("artifact download binding is invalid")
        if (
            self.artifact_name not in _ARTIFACTS
            or self.media_type != _ARTIFACTS.get(self.artifact_name)
            or not _DIGEST.fullmatch(self.digest)
            or isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 1 <= self.size_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise LumaError("artifact download descriptor is invalid")

    def public_body(self) -> dict[str, object]:
        return {
            "tenantRef": self.tenant_ref,
            "applicationRef": self.application_ref,
            "externalOperationId": self.external_operation_id,
            "builderTaskId": self.builder_task_id,
            "artifact": {
                "name": self.artifact_name,
                "digest": self.digest,
                "mediaType": self.media_type,
                "sizeBytes": self.size_bytes,
            },
        }


@dataclass(slots=True)
class ArtifactLeaseRecord:
    lease_id: str
    binding: ArtifactLeaseBinding
    node_name: str
    expires_at: float
    token_digest: bytes = field(repr=False)
    temporary_path: Path | None = field(default=None, repr=False)
    consumed: bool = False
    error: bool = False
    ready: threading.Event = field(default_factory=threading.Event, repr=False)


class ArtifactLeaseManager:
    """In-memory one-shot rendezvous; no token or payload enters control state."""

    def __init__(
        self,
        *,
        temporary_root: Path | None = None,
        max_active_leases: int = 1024,
        max_active_per_artifact: int = 4,
    ) -> None:
        if not 1 <= max_active_leases <= 10_000:
            raise ValueError("artifact lease capacity is invalid")
        if not 1 <= max_active_per_artifact <= 16:
            raise ValueError("artifact lease binding capacity is invalid")
        self._root = temporary_root
        self._max_active_leases = max_active_leases
        self._max_active_per_artifact = max_active_per_artifact
        self._records: dict[str, ArtifactLeaseRecord] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        binding: ArtifactLeaseBinding,
        *,
        node_name: str,
        ttl_seconds: int,
    ) -> tuple[ArtifactLeaseRecord, str]:
        if (
            not isinstance(node_name, str)
            or not _REFERENCE.fullmatch(node_name)
            or not 5 <= ttl_seconds <= 300
        ):
            raise LumaError("artifact download lease request is invalid")
        self.prune()
        lease_id = "artdl_" + secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(48)
        record = ArtifactLeaseRecord(
            lease_id=lease_id,
            binding=binding,
            node_name=node_name,
            expires_at=time.time() + ttl_seconds,
            token_digest=hashlib.sha256(token.encode()).digest(),
        )
        with self._lock:
            same_binding = sum(
                existing.binding == binding
                for existing in self._records.values()
            )
            if (
                len(self._records) >= self._max_active_leases
                or same_binding >= self._max_active_per_artifact
            ):
                raise LumaError("artifact download lease capacity is unavailable")
            self._records[lease_id] = record
        return record, token

    def revoke(self, lease_id: str) -> None:
        record = self._remove(lease_id)
        if record is not None:
            self._cleanup_record(record)

    def get_record(self, lease_id: str) -> ArtifactLeaseRecord:
        self.prune()
        with self._lock:
            record = self._records.get(lease_id)
            if record is None or record.expires_at <= time.time():
                raise LumaError("artifact download lease not found")
            return record

    def accept_upload(
        self,
        lease_id: str,
        *,
        node_name: str,
        media_type: str,
        digest: str,
        content_length: int,
        chunks: Iterable[bytes],
    ) -> None:
        record = self._require_upload(
            lease_id,
            node_name=node_name,
            media_type=media_type,
            digest=digest,
            content_length=content_length,
        )
        temporary = self._new_temporary_file()
        try:
            with os.fdopen(temporary[0], "wb") as output:
                self._copy_verified(record, chunks, output)
                output.flush()
                os.fsync(output.fileno())
            self._finish_upload(record, temporary[1])
        except BaseException:
            _unlink(temporary[1])
            self._fail_upload(record)
            raise

    async def accept_upload_async(
        self,
        lease_id: str,
        *,
        node_name: str,
        media_type: str,
        digest: str,
        content_length: int,
        chunks: AsyncIterator[bytes],
    ) -> None:
        record = self._require_upload(
            lease_id,
            node_name=node_name,
            media_type=media_type,
            digest=digest,
            content_length=content_length,
        )
        temporary = self._new_temporary_file()
        total = 0
        hasher = hashlib.sha256()
        try:
            with os.fdopen(temporary[0], "wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise LumaError("artifact upload stream is invalid")
                    for offset in range(0, len(chunk), MAX_ARTIFACT_CHUNK_BYTES):
                        bounded = chunk[
                            offset : offset + MAX_ARTIFACT_CHUNK_BYTES
                        ]
                        total += len(bounded)
                        if total > record.binding.size_bytes:
                            raise LumaError("artifact upload stream is invalid")
                        hasher.update(bounded)
                        output.write(bounded)
                if total != record.binding.size_bytes or not hmac.compare_digest(
                    "sha256:" + hasher.hexdigest(), record.binding.digest
                ):
                    raise LumaError("artifact upload integrity check failed")
                output.flush()
                os.fsync(output.fileno())
            self._finish_upload(record, temporary[1])
        except BaseException:
            _unlink(temporary[1])
            self._fail_upload(record)
            raise

    def redeem(self, lease_id: str, token: str) -> ArtifactLeaseRecord:
        self.prune()
        if not _LEASE_ID.fullmatch(str(lease_id or "")):
            raise LumaError("artifact download lease not found")
        with self._lock:
            record = self._records.get(lease_id)
            supplied_digest = hashlib.sha256(str(token or "").encode()).digest()
            if (
                record is None
                or record.consumed
                or record.expires_at <= time.time()
                or not hmac.compare_digest(record.token_digest, supplied_digest)
            ):
                raise LumaError("artifact download lease not found")
            record.consumed = True
        remaining = max(record.expires_at - time.time(), 0)
        if not record.ready.wait(remaining):
            self.revoke(lease_id)
            raise LumaError("artifact download lease expired")
        if record.error or record.temporary_path is None:
            self.revoke(lease_id)
            raise LumaError("artifact download is unavailable")
        return record

    async def redeem_async(
        self, lease_id: str, token: str
    ) -> ArtifactLeaseRecord:
        return await asyncio.to_thread(self.redeem, lease_id, token)

    def complete(self, lease_id: str) -> None:
        record = self._remove(lease_id)
        if record is not None:
            self._cleanup_record(record)

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                lease_id
                for lease_id, record in self._records.items()
                if record.expires_at <= now
            ]
            records = [self._records.pop(lease_id) for lease_id in expired]
        for record in records:
            self._cleanup_record(record)

    def _require_upload(
        self,
        lease_id: str,
        *,
        node_name: str,
        media_type: str,
        digest: str,
        content_length: int,
    ) -> ArtifactLeaseRecord:
        self.prune()
        with self._lock:
            record = self._records.get(lease_id)
            if (
                record is None
                or record.expires_at <= time.time()
                or record.ready.is_set()
                or record.node_name != node_name
                or record.binding.media_type != media_type
                or record.binding.digest != digest
                or record.binding.size_bytes != content_length
            ):
                raise LumaError("artifact upload binding is invalid")
            return record

    @staticmethod
    def _copy_verified(
        record: ArtifactLeaseRecord,
        chunks: Iterable[bytes],
        output: BinaryIO,
    ) -> None:
        total = 0
        hasher = hashlib.sha256()
        for chunk in chunks:
            if (
                not isinstance(chunk, bytes)
                or len(chunk) > MAX_ARTIFACT_CHUNK_BYTES
            ):
                raise LumaError("artifact upload stream is invalid")
            total += len(chunk)
            if total > record.binding.size_bytes:
                raise LumaError("artifact upload stream is invalid")
            hasher.update(chunk)
            output.write(chunk)
        if total != record.binding.size_bytes or not hmac.compare_digest(
            "sha256:" + hasher.hexdigest(), record.binding.digest
        ):
            raise LumaError("artifact upload integrity check failed")

    def _new_temporary_file(self) -> tuple[int, Path]:
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, path = tempfile.mkstemp(
            prefix="luma-artifact-rendezvous-",
            dir=str(self._root) if self._root is not None else None,
        )
        os.fchmod(fd, 0o600)
        return fd, Path(path)

    def _finish_upload(self, record: ArtifactLeaseRecord, path: Path) -> None:
        with self._lock:
            current = self._records.get(record.lease_id)
            if current is not record or record.expires_at <= time.time():
                _unlink(path)
                raise LumaError("artifact download lease expired")
            record.temporary_path = path
            record.ready.set()

    def _fail_upload(self, record: ArtifactLeaseRecord) -> None:
        with self._lock:
            if self._records.get(record.lease_id) is record:
                record.error = True
                record.ready.set()

    def _remove(self, lease_id: str) -> ArtifactLeaseRecord | None:
        with self._lock:
            return self._records.pop(lease_id, None)

    @staticmethod
    def _cleanup_record(record: ArtifactLeaseRecord) -> None:
        record.ready.set()
        if record.temporary_path is not None:
            _unlink(record.temporary_path)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "ArtifactLeaseBinding",
    "ArtifactLeaseManager",
    "ArtifactLeaseRecord",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACT_CHUNK_BYTES",
]
