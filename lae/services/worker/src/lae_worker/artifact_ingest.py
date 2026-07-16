from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .analyze import (
    AnalysisDigestReferences,
    AnalysisRecording,
    AnalyzeOrchestrationError,
    AnalyzeSourceContext,
    ArtifactDescriptor,
    ArtifactIngestCanceled,
)


MAX_ANALYSIS_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TRANSFER_CHUNK_BYTES = 1024 * 1024

_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_LEASE_ID = re.compile(r"^artdl_[A-Za-z0-9][A-Za-z0-9._-]{7,122}$")
_STORAGE_KEY = re.compile(
    r"^tenants/[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}/"
    r"analysis-artifacts/(?:evidence|deployment-plan|build-plan-candidate)/"
    r"sha256/[0-9a-f]{64}\.json$"
)
_ARTIFACT_KIND = {
    "evidence": "evidence",
    "deploymentPlan": "deployment-plan",
    "buildPlan": "build-plan-candidate",
}


class ArtifactIngestError(AnalyzeOrchestrationError):
    code = "LAE_ARTIFACT_INGEST_FAILED"
    public_message = "Analysis artifacts could not be stored safely."
    retryable = False


class ArtifactTransferUnavailable(ArtifactIngestError):
    code = "LAE_ARTIFACT_TRANSFER_UNAVAILABLE"
    public_message = "The secure analysis artifact transfer broker is unavailable."
    retryable = True


class ArtifactStorageUnavailable(ArtifactIngestError):
    code = "LAE_ARTIFACT_STORAGE_UNAVAILABLE"
    public_message = "The managed analysis artifact store is unavailable."
    retryable = True


class ArtifactIntegrityError(ArtifactIngestError):
    code = "LAE_ARTIFACT_INTEGRITY_FAILED"
    public_message = "An analysis artifact did not match its signed descriptor."


@dataclass(frozen=True, slots=True)
class ArtifactTransferBinding:
    tenant_ref: str
    application_ref: str
    operation_id: str
    builder_task_id: str
    descriptor: ArtifactDescriptor

    def __post_init__(self) -> None:
        for value in (
            self.tenant_ref,
            self.application_ref,
            self.operation_id,
            self.builder_task_id,
        ):
            if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                raise ArtifactIntegrityError()
        if self.descriptor.size_bytes > MAX_ANALYSIS_ARTIFACT_BYTES:
            raise ArtifactIntegrityError()


@dataclass(frozen=True, slots=True)
class ArtifactDownloadLease:
    """Opaque, non-secret handle for one broker-owned redemption.

    A concrete HTTP broker may retain a bearer secret internally, but neither
    the secret nor an internal object URL is part of this value. This keeps
    repr/asdict/operation-event paths safe by construction.
    """

    lease_id: str
    binding: ArtifactTransferBinding
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or not _LEASE_ID.fullmatch(
            self.lease_id
        ):
            raise ArtifactIntegrityError()
        if self.expires_at.tzinfo is None:
            raise ArtifactIntegrityError()


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    media_type: str
    content_length: int
    chunks: AsyncIterator[bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ArtifactIntegrityError()
        if (
            isinstance(self.content_length, bool)
            or not isinstance(self.content_length, int)
            or not 0 <= self.content_length <= MAX_ANALYSIS_ARTIFACT_BYTES
        ):
            raise ArtifactIntegrityError()


class ArtifactTransferBroker(Protocol):
    """Broker for bound, short-lived, single-use Luma artifact downloads."""

    async def issue_download_lease(
        self, binding: ArtifactTransferBinding, *, ttl_seconds: int
    ) -> ArtifactDownloadLease: ...

    async def open_download(
        self,
        lease: ArtifactDownloadLease,
        binding: ArtifactTransferBinding,
    ) -> ArtifactDownload: ...


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    media_type: str
    size_bytes: int
    digest: str

    def __post_init__(self) -> None:
        if not _STORAGE_KEY.fullmatch(self.key):
            raise ArtifactIntegrityError()
        descriptor = ArtifactDescriptor(
            name=_name_from_storage_key(self.key),
            digest=self.digest,
            media_type=self.media_type,
            size_bytes=self.size_bytes,
        )
        if object_key_for(self.key.split("/", 2)[1], descriptor) != self.key:
            raise ArtifactIntegrityError()


class S3CompatibleObjectStore(Protocol):
    """Managed object-store port with an atomic verified-write contract.

    Implementations must stream to a private temporary upload, validate the exact
    digest/size/media descriptor, then atomically publish at ``key``. A partial
    or invalid stream must never become visible at the final key. Credentials
    are implementation-private and must come from the runtime secret mount.
    """

    async def head(self, key: str) -> StoredObject | None: ...

    async def put_verified(
        self,
        *,
        key: str,
        media_type: str,
        size_bytes: int,
        digest: str,
        chunks: AsyncIterator[bytes],
    ) -> StoredObject: ...


@dataclass(frozen=True, slots=True)
class ArtifactPersistenceState:
    upload_status: str
    storage_key: str | None = None

    def __post_init__(self) -> None:
        if self.upload_status not in {
            "pending",
            "uploading",
            "verified",
            "failed",
        }:
            raise ArtifactIngestError()
        if self.upload_status == "verified":
            if self.storage_key is None or not _STORAGE_KEY.fullmatch(
                self.storage_key
            ):
                raise ArtifactIngestError()
        elif self.storage_key is not None:
            raise ArtifactIngestError()


class AnalysisArtifactCatalog(Protocol):
    async def prepare_analysis(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str,
    ) -> AnalysisRecording: ...

    async def get_artifact_state(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState: ...

    async def mark_artifact_uploading(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState: ...

    async def mark_artifact_verified(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
        stored: StoredObject,
    ) -> ArtifactPersistenceState: ...

    async def mark_artifact_failed(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState: ...

    async def finalize_stored_analysis(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
    ) -> AnalysisRecording: ...


class ArtifactIngestGuard(Protocol):
    async def checkpoint(self, binding: ArtifactTransferBinding) -> None: ...


class NoopArtifactIngestGuard:
    async def checkpoint(self, binding: ArtifactTransferBinding) -> None:
        del binding


class ArtifactIngestingAnalysisRecorder:
    """Descriptor-bound, crash-idempotent Builder -> managed-store ingest."""

    stores_verified_artifacts = True

    def __init__(
        self,
        *,
        catalog: AnalysisArtifactCatalog,
        broker: ArtifactTransferBroker,
        object_store: S3CompatibleObjectStore,
        guard: ArtifactIngestGuard | None = None,
        max_attempts: int = 3,
        lease_ttl_seconds: int = 60,
        attempt_timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if not 5 <= lease_ttl_seconds <= 300:
            raise ValueError("lease_ttl_seconds must be between 5 and 300")
        if not 1 <= attempt_timeout_seconds <= lease_ttl_seconds:
            raise ValueError("attempt timeout must fit within the download lease")
        self._catalog = catalog
        self._broker = broker
        self._object_store = object_store
        self._guard = guard or NoopArtifactIngestGuard()
        self._max_attempts = max_attempts
        self._lease_ttl_seconds = lease_ttl_seconds
        self._attempt_timeout_seconds = float(attempt_timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def record(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str | None = None,
    ) -> AnalysisRecording:
        if builder_task_id is None:
            raise ArtifactIntegrityError()
        bindings = tuple(
            ArtifactTransferBinding(
                tenant_ref=context.tenant_ref,
                application_ref=context.application_ref,
                operation_id=operation_id,
                builder_task_id=builder_task_id,
                descriptor=descriptor,
            )
            for descriptor in references.artifacts
        )
        await self._catalog.prepare_analysis(
            operation_id,
            context,
            references,
            builder_task_id=builder_task_id,
        )
        for binding in bindings:
            await self._ingest_one(context, binding)
        # Re-read managed metadata immediately before the database finalizer;
        # a verified row must not conceal an object that disappeared between
        # per-artifact ingest and the all-three-stored transition.
        for binding in bindings:
            key = object_key_for(binding.tenant_ref, binding.descriptor)
            stored = await self._head_with_retry(key, binding)
            if stored is None:
                raise ArtifactIntegrityError()
            _validate_stored_object(
                stored, key=key, descriptor=binding.descriptor
            )
        await self._guard.checkpoint(bindings[-1])
        return await self._catalog.finalize_stored_analysis(
            operation_id, context, references
        )

    async def _ingest_one(
        self,
        context: AnalyzeSourceContext,
        binding: ArtifactTransferBinding,
    ) -> None:
        descriptor = binding.descriptor
        key = object_key_for(binding.tenant_ref, descriptor)
        await self._guard.checkpoint(binding)
        state = await self._catalog.get_artifact_state(
            binding.operation_id, context, descriptor
        )
        existing = await self._head_with_retry(key, binding)
        if existing is not None:
            _validate_stored_object(existing, key=key, descriptor=descriptor)
            await self._catalog.mark_artifact_verified(
                binding.operation_id, context, descriptor, existing
            )
            return
        if state.upload_status == "verified":
            # The database must never silently bless a missing/corrupt object.
            raise ArtifactIntegrityError()

        await self._catalog.mark_artifact_uploading(
            binding.operation_id, context, descriptor
        )
        last_retryable: ArtifactIngestError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._guard.checkpoint(binding)
                async with asyncio.timeout(self._attempt_timeout_seconds):
                    lease = await self._broker.issue_download_lease(
                        binding, ttl_seconds=self._lease_ttl_seconds
                    )
                    self._validate_lease(lease, binding)
                    download = await self._broker.open_download(lease, binding)
                    if (
                        download.media_type != descriptor.media_type
                        or download.content_length != descriptor.size_bytes
                    ):
                        raise ArtifactIntegrityError()
                    stream = _validated_chunks(
                        download.chunks,
                        binding=binding,
                        guard=self._guard,
                    )
                    stored = await self._object_store.put_verified(
                        key=key,
                        media_type=descriptor.media_type,
                        size_bytes=descriptor.size_bytes,
                        digest=descriptor.digest,
                        chunks=stream,
                    )
                    _validate_stored_object(
                        stored, key=key, descriptor=descriptor
                    )
                await self._catalog.mark_artifact_verified(
                    binding.operation_id, context, descriptor, stored
                )
                return
            except ArtifactIngestCanceled:
                await self._catalog.mark_artifact_failed(
                    binding.operation_id, context, descriptor
                )
                raise
            except TimeoutError:
                last_retryable = ArtifactTransferUnavailable()
            except ArtifactIngestError as exc:
                if not exc.retryable:
                    await self._catalog.mark_artifact_failed(
                        binding.operation_id, context, descriptor
                    )
                    raise
                last_retryable = exc
            if attempt < self._max_attempts:
                await asyncio.sleep(0)

        await self._catalog.mark_artifact_failed(
            binding.operation_id, context, descriptor
        )
        raise last_retryable or ArtifactTransferUnavailable()

    async def _head_with_retry(
        self,
        key: str,
        binding: ArtifactTransferBinding,
    ) -> StoredObject | None:
        last_error: ArtifactIngestError | None = None
        for attempt in range(1, self._max_attempts + 1):
            await self._guard.checkpoint(binding)
            try:
                async with asyncio.timeout(self._attempt_timeout_seconds):
                    return await self._object_store.head(key)
            except ArtifactIngestCanceled:
                raise
            except TimeoutError:
                last_error = ArtifactStorageUnavailable()
            except ArtifactIngestError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            if attempt < self._max_attempts:
                await asyncio.sleep(0)
        raise last_error or ArtifactStorageUnavailable()

    def _validate_lease(
        self,
        lease: ArtifactDownloadLease,
        binding: ArtifactTransferBinding,
    ) -> None:
        now = self._clock()
        if now.tzinfo is None:
            raise ArtifactIntegrityError()
        remaining = lease.expires_at.astimezone(timezone.utc) - now.astimezone(
            timezone.utc
        )
        if (
            lease.binding != binding
            or remaining <= timedelta(0)
            or remaining > timedelta(seconds=self._lease_ttl_seconds + 5)
        ):
            raise ArtifactIntegrityError()


async def _validated_chunks(
    chunks: AsyncIterator[bytes],
    *,
    binding: ArtifactTransferBinding,
    guard: ArtifactIngestGuard,
) -> AsyncIterator[bytes]:
    digest = hashlib.sha256()
    total = 0
    async for chunk in chunks:
        await guard.checkpoint(binding)
        if not isinstance(chunk, bytes) or len(chunk) > MAX_TRANSFER_CHUNK_BYTES:
            raise ArtifactIntegrityError()
        total += len(chunk)
        if total > binding.descriptor.size_bytes:
            raise ArtifactIntegrityError()
        digest.update(chunk)
        if chunk:
            yield chunk
    if total != binding.descriptor.size_bytes:
        raise ArtifactIntegrityError()
    if f"sha256:{digest.hexdigest()}" != binding.descriptor.digest:
        raise ArtifactIntegrityError()


def object_key_for(tenant_ref: str, descriptor: ArtifactDescriptor) -> str:
    if not isinstance(tenant_ref, str) or not _REFERENCE.fullmatch(tenant_ref):
        raise ArtifactIntegrityError()
    kind = _ARTIFACT_KIND[descriptor.name]
    digest_hex = descriptor.digest.removeprefix("sha256:")
    key = (
        f"tenants/{tenant_ref}/analysis-artifacts/{kind}/"
        f"sha256/{digest_hex}.json"
    )
    if not _STORAGE_KEY.fullmatch(key):
        raise ArtifactIntegrityError()
    return key


def _name_from_storage_key(key: str) -> str:
    kind = key.split("/", 4)[3] if _STORAGE_KEY.fullmatch(key) else ""
    names = {value: name for name, value in _ARTIFACT_KIND.items()}
    try:
        return names[kind]
    except KeyError:
        raise ArtifactIntegrityError() from None


def _validate_stored_object(
    stored: StoredObject,
    *,
    key: str,
    descriptor: ArtifactDescriptor,
) -> None:
    if (
        stored.key != key
        or stored.media_type != descriptor.media_type
        or stored.size_bytes != descriptor.size_bytes
        or stored.digest != descriptor.digest
    ):
        raise ArtifactIntegrityError()


@dataclass(slots=True)
class _FakeBrokerObject:
    body: bytes = field(repr=False)
    media_type: str
    content_length: int
    failures_remaining: int = 0


@dataclass(slots=True)
class _FakeLeaseState:
    lease: ArtifactDownloadLease
    consumed: bool = False


class InMemoryArtifactTransferBroker:
    """Test-only broker that enforces exact binding, expiry, and one use."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        chunk_bytes: int = 64 * 1024,
    ) -> None:
        if not 1 <= chunk_bytes <= MAX_TRANSFER_CHUNK_BYTES:
            raise ValueError("chunk_bytes is outside the safe transfer range")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._chunk_bytes = chunk_bytes
        self._objects: dict[ArtifactTransferBinding, _FakeBrokerObject] = {}
        self._leases: dict[str, _FakeLeaseState] = {}
        self._counter = 0
        self.issue_calls = 0
        self.open_calls = 0

    def register(
        self,
        binding: ArtifactTransferBinding,
        body: bytes,
        *,
        media_type: str | None = None,
        content_length: int | None = None,
        failures_before_success: int = 0,
    ) -> None:
        if not isinstance(body, bytes) or failures_before_success < 0:
            raise ValueError("invalid fake artifact registration")
        self._objects[binding] = _FakeBrokerObject(
            body=body,
            media_type=media_type or binding.descriptor.media_type,
            content_length=(
                len(body) if content_length is None else content_length
            ),
            failures_remaining=failures_before_success,
        )

    async def issue_download_lease(
        self, binding: ArtifactTransferBinding, *, ttl_seconds: int
    ) -> ArtifactDownloadLease:
        self.issue_calls += 1
        if binding not in self._objects or not 5 <= ttl_seconds <= 300:
            raise ArtifactTransferUnavailable()
        self._counter += 1
        lease = ArtifactDownloadLease(
            lease_id=f"artdl_fake_{self._counter:08d}",
            binding=binding,
            expires_at=self._clock() + timedelta(seconds=ttl_seconds),
        )
        self._leases[lease.lease_id] = _FakeLeaseState(lease)
        return lease

    async def open_download(
        self,
        lease: ArtifactDownloadLease,
        binding: ArtifactTransferBinding,
    ) -> ArtifactDownload:
        self.open_calls += 1
        state = self._leases.get(lease.lease_id)
        if (
            state is None
            or state.lease != lease
            or state.lease.binding != binding
            or state.consumed
            or state.lease.expires_at <= self._clock()
        ):
            raise ArtifactTransferUnavailable()
        state.consumed = True
        source = self._objects.get(binding)
        if source is None:
            raise ArtifactTransferUnavailable()
        if source.failures_remaining:
            source.failures_remaining -= 1
            raise ArtifactTransferUnavailable()

        async def chunks() -> AsyncIterator[bytes]:
            for offset in range(0, len(source.body), self._chunk_bytes):
                await asyncio.sleep(0)
                yield source.body[offset : offset + self._chunk_bytes]

        return ArtifactDownload(
            media_type=source.media_type,
            content_length=source.content_length,
            chunks=chunks(),
        )


class InMemoryS3CompatibleObjectStore:
    """Test-only atomic object store; invalid streams never commit."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[StoredObject, bytes]] = {}
        self.put_calls = 0

    async def head(self, key: str) -> StoredObject | None:
        if not _STORAGE_KEY.fullmatch(key):
            raise ArtifactIntegrityError()
        value = self._objects.get(key)
        return None if value is None else value[0]

    async def put_verified(
        self,
        *,
        key: str,
        media_type: str,
        size_bytes: int,
        digest: str,
        chunks: AsyncIterator[bytes],
    ) -> StoredObject:
        self.put_calls += 1
        existing = self._objects.get(key)
        if existing is not None:
            _validate_stored_object(
                existing[0],
                key=key,
                descriptor=ArtifactDescriptor(
                    name=_name_from_storage_key(key),
                    digest=digest,
                    media_type=media_type,
                    size_bytes=size_bytes,
                ),
            )
            return existing[0]
        temporary = bytearray()
        hasher = hashlib.sha256()
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise ArtifactIntegrityError()
            temporary.extend(chunk)
            if len(temporary) > size_bytes:
                raise ArtifactIntegrityError()
            hasher.update(chunk)
        if len(temporary) != size_bytes:
            raise ArtifactIntegrityError()
        if f"sha256:{hasher.hexdigest()}" != digest:
            raise ArtifactIntegrityError()
        stored = StoredObject(
            key=key,
            media_type=media_type,
            size_bytes=size_bytes,
            digest=digest,
        )
        self._objects[key] = (stored, bytes(temporary))
        return stored

    def read_for_test(self, key: str) -> bytes:
        return self._objects[key][1]


@dataclass(slots=True)
class _FakeAnalysis:
    references: AnalysisDigestReferences
    recording: AnalysisRecording
    artifacts: dict[str, ArtifactPersistenceState]


class InMemoryAnalysisArtifactCatalog:
    """Test-only persistence state machine mirroring PostgreSQL transitions."""

    def __init__(self) -> None:
        self._analyses: dict[tuple[str, str], _FakeAnalysis] = {}

    async def prepare_analysis(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
        *,
        builder_task_id: str,
    ) -> AnalysisRecording:
        ArtifactTransferBinding(
            tenant_ref=context.tenant_ref,
            application_ref=context.application_ref,
            operation_id=operation_id,
            builder_task_id=builder_task_id,
            descriptor=references.artifacts[0],
        )
        key = (context.tenant_ref, operation_id)
        existing = self._analyses.get(key)
        if existing is not None:
            if existing.references != references:
                raise ArtifactIntegrityError()
            return existing.recording
        recording = AnalysisRecording()
        self._analyses[key] = _FakeAnalysis(
            references=references,
            recording=recording,
            artifacts={
                descriptor.name: ArtifactPersistenceState("pending")
                for descriptor in references.artifacts
            },
        )
        return recording

    async def get_artifact_state(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState:
        return self._artifact(operation_id, context, descriptor)[1]

    async def mark_artifact_uploading(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState:
        analysis, state = self._artifact(operation_id, context, descriptor)
        if state.upload_status != "verified":
            state = ArtifactPersistenceState("uploading")
            analysis.artifacts[descriptor.name] = state
        return state

    async def mark_artifact_verified(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
        stored: StoredObject,
    ) -> ArtifactPersistenceState:
        analysis, _state = self._artifact(operation_id, context, descriptor)
        _validate_stored_object(
            stored,
            key=object_key_for(context.tenant_ref, descriptor),
            descriptor=descriptor,
        )
        state = ArtifactPersistenceState("verified", stored.key)
        analysis.artifacts[descriptor.name] = state
        return state

    async def mark_artifact_failed(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> ArtifactPersistenceState:
        analysis, state = self._artifact(operation_id, context, descriptor)
        if state.upload_status != "verified":
            state = ArtifactPersistenceState("failed")
            analysis.artifacts[descriptor.name] = state
        return state

    async def finalize_stored_analysis(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        references: AnalysisDigestReferences,
    ) -> AnalysisRecording:
        analysis = self._analysis(operation_id, context)
        if analysis.references != references or any(
            state.upload_status != "verified"
            for state in analysis.artifacts.values()
        ):
            raise ArtifactIngestError()
        analysis.recording = AnalysisRecording(
            analysis_status=analysis.recording.analysis_status,
            artifact_state="stored",
            plan_stored=True,
        )
        return analysis.recording

    def state_for_test(
        self, operation_id: str, context: AnalyzeSourceContext
    ) -> tuple[AnalysisRecording, dict[str, ArtifactPersistenceState]]:
        analysis = self._analysis(operation_id, context)
        return analysis.recording, dict(analysis.artifacts)

    def _analysis(
        self, operation_id: str, context: AnalyzeSourceContext
    ) -> _FakeAnalysis:
        try:
            return self._analyses[(context.tenant_ref, operation_id)]
        except KeyError:
            raise ArtifactIngestError() from None

    def _artifact(
        self,
        operation_id: str,
        context: AnalyzeSourceContext,
        descriptor: ArtifactDescriptor,
    ) -> tuple[_FakeAnalysis, ArtifactPersistenceState]:
        analysis = self._analysis(operation_id, context)
        expected = next(
            (
                item
                for item in analysis.references.artifacts
                if item.name == descriptor.name
            ),
            None,
        )
        if expected != descriptor:
            raise ArtifactIntegrityError()
        return analysis, analysis.artifacts[descriptor.name]
