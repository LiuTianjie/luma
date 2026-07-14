from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from lae_store import (
    OperationStore,
    PrivateObjectIntegrityError,
    PrivateObjectStoreUnavailable,
    S3PrivateObjectConfig,
    S3PrivateObjectStore,
)

from .artifact_ingest import (
    ArtifactDownload,
    ArtifactDownloadLease,
    ArtifactIngestingAnalysisRecorder,
    ArtifactIntegrityError,
    ArtifactStorageUnavailable,
    ArtifactTransferBinding,
    ArtifactTransferUnavailable,
    StoredObject,
)
from .artifact_postgres import (
    PostgresAnalysisArtifactCatalog,
    PostgresArtifactIngestGuard,
)


_LEASE_ID = re.compile(r"^artdl_[A-Za-z0-9][A-Za-z0-9._-]{7,122}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_SCHEMA_VERSION = "luma.artifact-download-lease/v1"
_DEPLOYMENT_PLAN_MEDIA_TYPE = "application/vnd.lae.deployment-plan+json"
_MAX_DEPLOYMENT_PLAN_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LumaArtifactBrokerConfig:
    endpoint: str
    principal_id: str
    token: str = field(repr=False)
    production: bool = True
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.endpoint)
        schemes = {"https"} if self.production else {"http", "https"}
        if (
            parsed.scheme not in schemes
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not _REFERENCE.fullmatch(self.principal_id)
            or not isinstance(self.token, str)
            or not 16 <= len(self.token) <= 512
            or any(not 33 <= ord(character) <= 126 for character in self.token)
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1 <= float(self.timeout_seconds) <= 120
        ):
            raise ValueError("Luma artifact broker configuration is invalid")


@dataclass(slots=True)
class _SecretLease:
    binding: ArtifactTransferBinding
    expires_at: datetime
    token: str = field(repr=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class HttpArtifactTransferBroker:
    """HTTPS client for Luma's task-bound, one-shot artifact stream."""

    def __init__(
        self,
        config: LumaArtifactBrokerConfig,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._config = config
        self._endpoint = config.endpoint.rstrip("/")
        context = ssl_context or ssl.create_default_context()
        handlers: list[Any] = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if urllib.parse.urlsplit(config.endpoint).scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)
        self._leases: dict[str, _SecretLease] = {}
        self._lock = threading.Lock()

    async def issue_download_lease(
        self, binding: ArtifactTransferBinding, *, ttl_seconds: int
    ) -> ArtifactDownloadLease:
        if not 5 <= ttl_seconds <= 300:
            raise ArtifactTransferUnavailable()
        body = {
            "schemaVersion": _SCHEMA_VERSION,
            "tenantRef": binding.tenant_ref,
            "applicationRef": binding.application_ref,
            "externalOperationId": binding.operation_id,
            "builderTaskId": binding.builder_task_id,
            "artifact": _descriptor_body(binding),
            "ttlSeconds": ttl_seconds,
        }
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            (
                "/v1/builder/tasks/"
                f"{urllib.parse.quote(binding.builder_task_id, safe='')}"
                "/artifact-download-leases"
            ),
            body,
            self._config.token,
        )
        lease = _parse_lease(value, binding)
        token = value.get("downloadToken")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ArtifactTransferUnavailable()
        with self._lock:
            if lease.lease_id in self._leases:
                raise ArtifactTransferUnavailable()
            self._leases[lease.lease_id] = _SecretLease(
                binding=binding,
                expires_at=lease.expires_at,
                token=token,
            )
        return lease

    async def open_download(
        self,
        lease: ArtifactDownloadLease,
        binding: ArtifactTransferBinding,
    ) -> ArtifactDownload:
        with self._lock:
            secret = self._leases.pop(lease.lease_id, None)
        now = datetime.now(timezone.utc)
        if (
            secret is None
            or secret.binding != binding
            or lease.binding != binding
            or secret.expires_at != lease.expires_at
            or secret.expires_at <= now
        ):
            raise ArtifactTransferUnavailable()
        response = await asyncio.to_thread(
            self._open_download_sync, lease.lease_id, secret.token
        )
        try:
            media_type = str(response.headers.get("Content-Type") or "")
            digest = str(response.headers.get("X-Luma-Artifact-Digest") or "")
            response_lease = str(
                response.headers.get("X-Luma-Artifact-Lease-Id") or ""
            )
            content_length = int(response.headers.get("Content-Length") or "")
            if (
                media_type != binding.descriptor.media_type
                or digest != binding.descriptor.digest
                or response_lease != lease.lease_id
                or content_length != binding.descriptor.size_bytes
            ):
                raise ArtifactIntegrityError()
        except (TypeError, ValueError, ArtifactIntegrityError):
            response.close()
            raise ArtifactIntegrityError() from None

        async def chunks() -> AsyncIterator[bytes]:
            total = 0
            try:
                while True:
                    chunk = await asyncio.to_thread(response.read, 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > binding.descriptor.size_bytes:
                        raise ArtifactIntegrityError()
                    yield bytes(chunk)
                if total != binding.descriptor.size_bytes:
                    raise ArtifactIntegrityError()
            except asyncio.CancelledError:
                raise
            except ArtifactIntegrityError:
                raise
            except (OSError, TimeoutError, socket.timeout) as exc:
                raise ArtifactTransferUnavailable() from exc
            finally:
                response.close()

        return ArtifactDownload(
            media_type=media_type,
            content_length=content_length,
            chunks=chunks(),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: Mapping[str, object],
        token: str,
    ) -> dict[str, object]:
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        request = urllib.request.Request(
            self._endpoint + path,
            data=encoded,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-LAE-Service-Principal": self._config.principal_id,
            },
        )
        try:
            with self._opener.open(
                request, timeout=float(self._config.timeout_seconds)
            ) as response:
                raw = response.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ArtifactTransferUnavailable()
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            TimeoutError,
            socket.timeout,
        ):
            raise ArtifactTransferUnavailable() from None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ArtifactTransferUnavailable() from None
        if not isinstance(value, dict):
            raise ArtifactTransferUnavailable()
        return value

    def _open_download_sync(self, lease_id: str, token: str) -> Any:
        request = urllib.request.Request(
            (
                f"{self._endpoint}/v1/builder/artifact-downloads/"
                f"{urllib.parse.quote(lease_id, safe='')}"
            ),
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/octet-stream",
            },
        )
        try:
            return self._opener.open(
                request, timeout=float(self._config.timeout_seconds)
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            TimeoutError,
            socket.timeout,
        ):
            raise ArtifactTransferUnavailable() from None


class S3AnalysisArtifactObjectStore:
    def __init__(self, store: S3PrivateObjectStore) -> None:
        self._store = store

    async def head(self, key: str) -> StoredObject | None:
        try:
            value = await self._store.head(key)
        except PrivateObjectIntegrityError:
            raise ArtifactIntegrityError() from None
        except PrivateObjectStoreUnavailable:
            raise ArtifactStorageUnavailable() from None
        if value is None:
            return None
        try:
            return StoredObject(
                key=value.key,
                media_type=value.media_type,
                size_bytes=value.size_bytes,
                digest=value.digest,
            )
        except ArtifactIntegrityError:
            raise

    async def put_verified(
        self,
        *,
        key: str,
        media_type: str,
        size_bytes: int,
        digest: str,
        chunks: AsyncIterator[bytes],
    ) -> StoredObject:
        try:
            value = await self._store.put_verified(
                key=key,
                media_type=media_type,
                size_bytes=size_bytes,
                digest=digest,
                chunks=chunks,
            )
        except PrivateObjectIntegrityError:
            raise ArtifactIntegrityError() from None
        except PrivateObjectStoreUnavailable:
            raise ArtifactStorageUnavailable() from None
        return StoredObject(
            key=value.key,
            media_type=value.media_type,
            size_bytes=value.size_bytes,
            digest=value.digest,
        )


class S3UpdateCheckPlanLoader:
    """Read only verified LAE deployment plans for a closed update diff."""

    def __init__(self, store: S3PrivateObjectStore) -> None:
        self._store = store

    async def load(
        self, storage_key: str, *, expected_digest: str
    ) -> Mapping[str, object]:
        try:
            download = await self._store.get_stream(
                storage_key, max_bytes=_MAX_DEPLOYMENT_PLAN_BYTES
            )
            metadata = download.metadata
            if (
                metadata.media_type != _DEPLOYMENT_PLAN_MEDIA_TYPE
                or metadata.digest != expected_digest
            ):
                raise ValueError("deployment plan descriptor mismatch")
            hasher = hashlib.sha256()
            body = bytearray()
            async for chunk in download.chunks:
                hasher.update(chunk)
                body.extend(chunk)
            if f"sha256:{hasher.hexdigest()}" != expected_digest:
                raise ValueError("deployment plan digest mismatch")
            decoded = json.loads(body)
            if not isinstance(decoded, dict):
                raise ValueError("deployment plan body is invalid")
            return decoded
        except (
            PrivateObjectIntegrityError,
            PrivateObjectStoreUnavailable,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("verified deployment plan is unavailable") from exc


def _private_object_store_from_env(
    values: Mapping[str, str],
) -> S3PrivateObjectStore:
    environment = values.get("LAE_ENVIRONMENT", "development").strip().lower()
    production = environment in {"prod", "production"}
    return S3PrivateObjectStore(
        S3PrivateObjectConfig(
            endpoint=_required(values, "LAE_ARTIFACT_S3_ENDPOINT"),
            bucket=_required(values, "LAE_ARTIFACT_S3_BUCKET"),
            region=_required(values, "LAE_ARTIFACT_S3_REGION"),
            access_key=_required(values, "LAE_ARTIFACT_S3_ACCESS_KEY"),
            secret_key=_required(values, "LAE_ARTIFACT_S3_SECRET_KEY"),
            allowed_hosts=tuple(
                item.strip()
                for item in _required(
                    values, "LAE_ARTIFACT_S3_ALLOWED_HOSTS"
                ).split(",")
                if item.strip()
            ),
            path_style=_bool(values, "LAE_ARTIFACT_S3_PATH_STYLE", True),
            production=production,
            timeout_seconds=_float(
                values, "LAE_ARTIFACT_S3_TIMEOUT_SECONDS", 20.0
            ),
        )
    )


def update_check_plan_loader_from_env(
    environ: Mapping[str, str] | None = None,
) -> S3UpdateCheckPlanLoader:
    values = os.environ if environ is None else environ
    return S3UpdateCheckPlanLoader(_private_object_store_from_env(values))


def artifact_recorder_from_env(
    *,
    sessions: Any,
    operations: OperationStore,
    agent_image_digest: str,
    worker_id: str,
    lease_seconds: int,
    environ: Mapping[str, str] | None = None,
) -> ArtifactIngestingAnalysisRecorder:
    values = os.environ if environ is None else environ
    environment = values.get("LAE_ENVIRONMENT", "development").strip().lower()
    production = environment in {"prod", "production"}
    broker = HttpArtifactTransferBroker(
        LumaArtifactBrokerConfig(
            endpoint=_required(values, "LAE_LUMA_CONTROL_URL"),
            principal_id=_required(values, "LAE_LUMA_SERVICE_PRINCIPAL_ID"),
            token=_required(values, "LAE_LUMA_SERVICE_TOKEN"),
            production=production,
            timeout_seconds=_float(
                values, "LAE_LUMA_ARTIFACT_TIMEOUT_SECONDS", 20.0
            ),
        )
    )
    catalog = PostgresAnalysisArtifactCatalog(
        sessions,
        agent_image_digest=agent_image_digest,
        worker_id=worker_id,
    )
    guard = PostgresArtifactIngestGuard(
        operations,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    return ArtifactIngestingAnalysisRecorder(
        catalog=catalog,
        broker=broker,
        object_store=S3AnalysisArtifactObjectStore(
            _private_object_store_from_env(values)
        ),
        guard=guard,
        max_attempts=_int(values, "LAE_ARTIFACT_MAX_ATTEMPTS", 3),
        lease_ttl_seconds=_int(values, "LAE_ARTIFACT_LEASE_TTL_SECONDS", 60),
        attempt_timeout_seconds=_float(
            values, "LAE_ARTIFACT_ATTEMPT_TIMEOUT_SECONDS", 30.0
        ),
    )


def _descriptor_body(binding: ArtifactTransferBinding) -> dict[str, object]:
    descriptor = binding.descriptor
    return {
        "name": descriptor.name,
        "digest": descriptor.digest,
        "mediaType": descriptor.media_type,
        "sizeBytes": descriptor.size_bytes,
    }


def _parse_lease(
    value: Mapping[str, object], binding: ArtifactTransferBinding
) -> ArtifactDownloadLease:
    if set(value) != {
        "schemaVersion",
        "leaseId",
        "expiresAt",
        "downloadToken",
        "binding",
    } or value.get("schemaVersion") != _SCHEMA_VERSION:
        raise ArtifactTransferUnavailable()
    lease_id = value.get("leaseId")
    expires_raw = value.get("expiresAt")
    returned_binding = value.get("binding")
    if (
        not isinstance(lease_id, str)
        or not _LEASE_ID.fullmatch(lease_id)
        or not isinstance(expires_raw, str)
        or not isinstance(returned_binding, dict)
        or returned_binding
        != {
            "tenantRef": binding.tenant_ref,
            "applicationRef": binding.application_ref,
            "externalOperationId": binding.operation_id,
            "builderTaskId": binding.builder_task_id,
            "artifact": _descriptor_body(binding),
        }
    ):
        raise ArtifactTransferUnavailable()
    try:
        expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactTransferUnavailable() from None
    if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        raise ArtifactTransferUnavailable()
    return ArtifactDownloadLease(
        lease_id=lease_id,
        binding=binding,
        expires_at=expires_at,
    )


def _required(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    value = values.get(key)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _int(values: Mapping[str, str], key: str, default: int) -> int:
    value = values.get(key)
    return default if value is None else int(value)


def _float(values: Mapping[str, str], key: str, default: float) -> float:
    value = values.get(key)
    return default if value is None else float(value)


__all__ = [
    "HttpArtifactTransferBroker",
    "LumaArtifactBrokerConfig",
    "S3AnalysisArtifactObjectStore",
    "artifact_recorder_from_env",
]
