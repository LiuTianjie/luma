from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import re
import ssl
import tempfile
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO, Mapping


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUCKET = re.compile(
    r"^(?=.{3,63}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
)
_REGION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\."
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
MAX_PRIVATE_OBJECT_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_OBJECT_CHUNK_BYTES = 1024 * 1024


class PrivateObjectStoreError(RuntimeError):
    """Stable, secret-free base error for the private S3 boundary."""


class PrivateObjectStoreUnavailable(PrivateObjectStoreError):
    pass


class PrivateObjectIntegrityError(PrivateObjectStoreError):
    pass


@dataclass(frozen=True, slots=True)
class S3PrivateObjectConfig:
    endpoint: str
    bucket: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    allowed_hosts: tuple[str, ...]
    path_style: bool = True
    production: bool = True
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.endpoint)
        hostname = (parsed.hostname or "").lower()
        schemes = {"https"} if self.production else {"http", "https"}
        normalized_allowlist = tuple(
            item.strip().lower() for item in self.allowed_hosts if item.strip()
        )
        if (
            parsed.scheme not in schemes
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not _HOST.fullmatch(hostname)
            or hostname not in normalized_allowlist
            or len(normalized_allowlist) != len(set(normalized_allowlist))
        ):
            raise ValueError("private object-store endpoint is invalid")
        if not _BUCKET.fullmatch(self.bucket) or ".." in self.bucket:
            raise ValueError("private object-store bucket is invalid")
        if not _REGION.fullmatch(self.region):
            raise ValueError("private object-store region is invalid")
        if not self.access_key or len(self.access_key) > 128:
            raise ValueError("private object-store access key is invalid")
        if not 16 <= len(self.secret_key) <= 256:
            raise ValueError("private object-store secret key is invalid")
        if not isinstance(self.path_style, bool):
            raise ValueError("private object-store path style is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1 <= float(self.timeout_seconds) <= 120
        ):
            raise ValueError("private object-store timeout is invalid")


@dataclass(frozen=True, slots=True)
class PrivateObjectMetadata:
    key: str = field(repr=False)
    media_type: str
    size_bytes: int
    digest: str

    def __post_init__(self) -> None:
        _require_object_key(self.key)
        if not self.media_type or len(self.media_type) > 255:
            raise PrivateObjectIntegrityError("object metadata is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= MAX_PRIVATE_OBJECT_BYTES
            or not _DIGEST.fullmatch(self.digest)
        ):
            raise PrivateObjectIntegrityError("object metadata is invalid")


@dataclass(frozen=True, slots=True)
class PrivateObjectDownload:
    metadata: PrivateObjectMetadata
    chunks: AsyncIterator[bytes] = field(repr=False)


class S3PrivateObjectStore:
    """Bounded SigV4 adapter for LAE-owned S3-compatible objects.

    Object keys are always supplied by a trusted LAE caller. The endpoint and
    bucket are immutable configuration; redirects, proxy environment variables
    and caller-provided URLs never participate in a request. Writes first spool
    and verify the stream in a private local file, then publish the final key
    with a conditional S3 PUT. A failed/partial PUT cannot expose a partial S3
    object, and an exact existing object is an idempotent crash-retry hit.
    """

    def __init__(
        self,
        config: S3PrivateObjectConfig,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._config = config
        self._parsed = urllib.parse.urlsplit(config.endpoint)
        self._ssl_context = ssl_context or ssl.create_default_context()

    async def head(self, key: str) -> PrivateObjectMetadata | None:
        _require_object_key(key)
        return await asyncio.to_thread(self._head_sync, key)

    async def put_verified(
        self,
        *,
        key: str,
        media_type: str,
        size_bytes: int,
        digest: str,
        chunks: AsyncIterator[bytes],
    ) -> PrivateObjectMetadata:
        _require_descriptor(key, media_type, size_bytes, digest)
        with tempfile.TemporaryFile(mode="w+b") as staging:
            hasher = hashlib.sha256()
            total = 0
            async for chunk in chunks:
                if (
                    not isinstance(chunk, bytes)
                    or len(chunk) > MAX_PRIVATE_OBJECT_CHUNK_BYTES
                ):
                    raise PrivateObjectIntegrityError("object stream is invalid")
                total += len(chunk)
                if total > size_bytes:
                    raise PrivateObjectIntegrityError("object stream is invalid")
                hasher.update(chunk)
                staging.write(chunk)
            if total != size_bytes or not hmac.compare_digest(
                f"sha256:{hasher.hexdigest()}", digest
            ):
                raise PrivateObjectIntegrityError("object stream is invalid")
            staging.flush()
            staging.seek(0)
            return await asyncio.to_thread(
                self._publish_sync,
                key,
                media_type,
                size_bytes,
                digest,
                staging,
            )

    async def get_stream(
        self,
        key: str,
        *,
        max_bytes: int = MAX_PRIVATE_OBJECT_BYTES,
    ) -> PrivateObjectDownload:
        _require_object_key(key)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_PRIVATE_OBJECT_BYTES
        ):
            raise ValueError("private object read limit is invalid")
        connection, response = await asyncio.to_thread(
            self._open_download_sync, key
        )
        try:
            metadata = _metadata_from_response(key, response)
            if metadata.size_bytes > max_bytes:
                raise PrivateObjectIntegrityError("object exceeds read limit")
        except BaseException:
            response.close()
            connection.close()
            raise

        async def stream() -> AsyncIterator[bytes]:
            total = 0
            try:
                while True:
                    chunk = await asyncio.to_thread(
                        response.read, MAX_PRIVATE_OBJECT_CHUNK_BYTES
                    )
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes or total > metadata.size_bytes:
                        raise PrivateObjectIntegrityError(
                            "object exceeds read limit"
                        )
                    yield bytes(chunk)
                if total != metadata.size_bytes:
                    raise PrivateObjectIntegrityError("object size changed during read")
            except asyncio.CancelledError:
                raise
            except PrivateObjectStoreError:
                raise
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                raise PrivateObjectStoreUnavailable(
                    "private object-store read failed"
                ) from exc
            finally:
                response.close()
                connection.close()

        return PrivateObjectDownload(metadata=metadata, chunks=stream())

    def _head_sync(self, key: str) -> PrivateObjectMetadata | None:
        connection, response = self._request_sync("HEAD", key)
        try:
            if response.status == 404:
                return None
            _require_success(response.status)
            return _metadata_from_response(key, response)
        finally:
            response.close()
            connection.close()

    def _open_download_sync(
        self, key: str
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection, response = self._request_sync("GET", key)
        if response.status == 404:
            response.close()
            connection.close()
            raise PrivateObjectStoreUnavailable("private object is unavailable")
        try:
            _require_success(response.status)
        except BaseException:
            response.close()
            connection.close()
            raise
        return connection, response

    def _publish_sync(
        self,
        key: str,
        media_type: str,
        size_bytes: int,
        digest: str,
        staging: BinaryIO,
    ) -> PrivateObjectMetadata:
        expected = PrivateObjectMetadata(key, media_type, size_bytes, digest)
        existing = self._head_sync(key)
        if existing is not None:
            _require_exact(existing, expected)
            self._verify_body_sync(expected)
            return existing

        connection, response = self._request_sync(
            "PUT",
            key,
            headers={
                "Content-Length": str(size_bytes),
                "Content-Type": media_type,
                "If-None-Match": "*",
                "X-Amz-Meta-LAE-SHA256": digest,
            },
            body=staging,
            payload_hash=digest.removeprefix("sha256:"),
        )
        try:
            if response.status not in {200, 201, 204, 412}:
                _require_success(response.status)
            response.read(64 * 1024)
        finally:
            response.close()
            connection.close()
        published = self._head_sync(key)
        if published is None:
            raise PrivateObjectStoreUnavailable("private object publish failed")
        _require_exact(published, expected)
        self._verify_body_sync(expected)
        return published

    def _verify_body_sync(self, expected: PrivateObjectMetadata) -> None:
        connection, response = self._open_download_sync(expected.key)
        try:
            _require_exact(_metadata_from_response(expected.key, response), expected)
            total = 0
            hasher = hashlib.sha256()
            while True:
                chunk = response.read(MAX_PRIVATE_OBJECT_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected.size_bytes:
                    raise PrivateObjectIntegrityError(
                        "existing object content does not match"
                    )
                hasher.update(chunk)
            if total != expected.size_bytes or not hmac.compare_digest(
                "sha256:" + hasher.hexdigest(), expected.digest
            ):
                raise PrivateObjectIntegrityError(
                    "existing object content does not match"
                )
        except PrivateObjectStoreError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise PrivateObjectStoreUnavailable(
                "private object-store read failed"
            ) from exc
        finally:
            response.close()
            connection.close()

    def _request_sync(
        self,
        method: str,
        key: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: BinaryIO | None = None,
        payload_hash: str | None = None,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        _require_object_key(key)
        request_headers = dict(headers or {})
        payload_digest = payload_hash or hashlib.sha256(b"").hexdigest()
        path, host = self._object_target(key)
        now = datetime.now(timezone.utc)
        request_headers.update(
            {
                "Host": host,
                "X-Amz-Content-Sha256": payload_digest,
                "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
            }
        )
        request_headers["Authorization"] = self._authorization(
            method, path, request_headers, payload_digest, now
        )
        connection = self._connection()
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in request_headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            if body is not None:
                while True:
                    chunk = body.read(MAX_PRIVATE_OBJECT_CHUNK_BYTES)
                    if not chunk:
                        break
                    connection.send(chunk)
            response = connection.getresponse()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            connection.close()
            raise PrivateObjectStoreUnavailable(
                "private object-store request failed"
            ) from exc
        if 300 <= response.status < 400:
            response.close()
            connection.close()
            raise PrivateObjectStoreUnavailable(
                "private object-store redirect was rejected"
            )
        return connection, response

    def _connection(self) -> http.client.HTTPConnection:
        host = self._request_hostname()
        port = self._parsed.port
        timeout = float(self._config.timeout_seconds)
        if self._parsed.scheme == "https":
            return http.client.HTTPSConnection(
                host, port=port, timeout=timeout, context=self._ssl_context
            )
        return http.client.HTTPConnection(host, port=port, timeout=timeout)

    def _request_hostname(self) -> str:
        endpoint_host = self._parsed.hostname or ""
        return (
            endpoint_host
            if self._config.path_style
            else f"{self._config.bucket}.{endpoint_host}"
        )

    def _object_target(self, key: str) -> tuple[str, str]:
        components = key.split("/")
        if self._config.path_style:
            components.insert(0, self._config.bucket)
        path = "/" + "/".join(
            urllib.parse.quote(component, safe="-_.~") for component in components
        )
        hostname = self._request_hostname()
        port = self._parsed.port
        default_port = 443 if self._parsed.scheme == "https" else 80
        host = hostname if port in {None, default_port} else f"{hostname}:{port}"
        return path, host

    def _authorization(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        payload_hash: str,
        now: datetime,
    ) -> str:
        lowered = {
            name.lower(): " ".join(str(value).strip().split())
            for name, value in headers.items()
            if name.lower() != "authorization"
        }
        signed_names = sorted(lowered)
        canonical_headers = "".join(
            f"{name}:{lowered[name]}\n" for name in signed_names
        )
        signed_headers = ";".join(signed_names)
        canonical_request = (
            f"{method}\n{path}\n\n{canonical_headers}\n"
            f"{signed_headers}\n{payload_hash}"
        )
        day = now.strftime("%Y%m%d")
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        scope = f"{day}/{self._config.region}/s3/aws4_request"
        string_to_sign = (
            "AWS4-HMAC-SHA256\n"
            f"{timestamp}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        signature = hmac.new(
            self._signing_key(day), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._config.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    def _signing_key(self, day: str) -> bytes:
        date_key = hmac.new(
            ("AWS4" + self._config.secret_key).encode(),
            day.encode(),
            hashlib.sha256,
        ).digest()
        region_key = hmac.new(
            date_key, self._config.region.encode(), hashlib.sha256
        ).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _require_descriptor(
    key: str, media_type: str, size_bytes: int, digest: str
) -> None:
    _require_object_key(key)
    if (
        not isinstance(media_type, str)
        or not media_type
        or len(media_type) > 255
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= MAX_PRIVATE_OBJECT_BYTES
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise PrivateObjectIntegrityError("object descriptor is invalid")


def _require_object_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 1024
        or key.startswith("/")
        or key.endswith("/")
        or "\\" in key
        or any(
            not component or component in {".", ".."}
            for component in key.split("/")
        )
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in key)
    ):
        raise PrivateObjectIntegrityError("object key is invalid")


def _metadata_from_response(
    key: str, response: http.client.HTTPResponse
) -> PrivateObjectMetadata:
    try:
        size_bytes = int(response.getheader("Content-Length", ""))
        media_type = str(response.getheader("Content-Type", ""))
        digest = str(response.getheader("X-Amz-Meta-LAE-SHA256", ""))
    except (TypeError, ValueError) as exc:
        raise PrivateObjectIntegrityError("object metadata is invalid") from exc
    return PrivateObjectMetadata(key, media_type, size_bytes, digest)


def _require_success(status: int) -> None:
    if not 200 <= status < 300:
        if status in {408, 429, 500, 502, 503, 504}:
            raise PrivateObjectStoreUnavailable("private object store is unavailable")
        raise PrivateObjectIntegrityError("private object-store response is invalid")


def _require_exact(
    actual: PrivateObjectMetadata, expected: PrivateObjectMetadata
) -> None:
    if actual != expected:
        raise PrivateObjectIntegrityError("existing object metadata does not match")


__all__ = [
    "MAX_PRIVATE_OBJECT_BYTES",
    "MAX_PRIVATE_OBJECT_CHUNK_BYTES",
    "PrivateObjectDownload",
    "PrivateObjectIntegrityError",
    "PrivateObjectMetadata",
    "PrivateObjectStoreError",
    "PrivateObjectStoreUnavailable",
    "S3PrivateObjectConfig",
    "S3PrivateObjectStore",
]
