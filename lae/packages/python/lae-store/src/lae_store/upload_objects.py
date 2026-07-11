from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Mapping, Protocol

from .errors import UploadUnavailable, UploadVerificationFailed


@dataclass(frozen=True, slots=True)
class UploadPutGrant:
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class UploadGetGrant:
    """Short-lived object read grant returned only by an internal broker."""

    url: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class UploadObjectMetadata:
    size_bytes: int
    media_type: str


class S3CompatibleUploadStore(Protocol):
    """Private object-store port used by API completion and the scanner.

    Implementations must never expose bucket/object identifiers through their
    public record types. ``copy_to`` is streaming and bounded so a forged HEAD
    response cannot turn verification into an unbounded memory allocation.
    """

    def ensure_available(self) -> None: ...

    async def issue_single_use_put(
        self,
        *,
        object_key: str,
        size_bytes: int,
        media_type: str,
        expires_in: timedelta,
    ) -> UploadPutGrant: ...

    async def issue_bounded_get(
        self,
        *,
        object_key: str,
        expires_in: timedelta,
    ) -> UploadGetGrant: ...

    async def head(self, object_key: str) -> UploadObjectMetadata: ...

    async def copy_to(
        self, object_key: str, sink: BinaryIO, *, max_bytes: int
    ) -> UploadObjectMetadata: ...

    async def delete(self, object_key: str) -> None: ...


class UnconfiguredUploadStore:
    """Production-safe fail-closed adapter.

    Git analysis remains independent because only upload routes resolve this
    capability.
    """

    def ensure_available(self) -> None:
        raise UploadUnavailable("upload object store is not configured")

    async def issue_single_use_put(self, **_kwargs: object) -> UploadPutGrant:
        self.ensure_available()
        raise AssertionError("unreachable")

    async def issue_bounded_get(self, **_kwargs: object) -> UploadGetGrant:
        self.ensure_available()
        raise AssertionError("unreachable")

    async def head(self, _object_key: str) -> UploadObjectMetadata:
        self.ensure_available()
        raise AssertionError("unreachable")

    async def copy_to(
        self, _object_key: str, _sink: BinaryIO, *, max_bytes: int
    ) -> UploadObjectMetadata:
        del max_bytes
        self.ensure_available()
        raise AssertionError("unreachable")

    async def delete(self, _object_key: str) -> None:
        self.ensure_available()


@dataclass(frozen=True, slots=True)
class S3UploadConfig:
    endpoint: str
    bucket: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    production: bool = True

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.endpoint)
        if (
            parsed.scheme not in ({"https"} if self.production else {"http", "https"})
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("S3 endpoint is invalid")
        if not self.bucket or not _safe_component(self.bucket, 63):
            raise ValueError("S3 bucket is invalid")
        if not self.region or not _safe_component(self.region, 64):
            raise ValueError("S3 region is invalid")
        if not self.access_key or len(self.access_key) > 128:
            raise ValueError("S3 access key is invalid")
        if len(self.secret_key) < 16 or len(self.secret_key) > 256:
            raise ValueError("S3 secret key is invalid")


class S3SigV4UploadStore:
    """Small dependency-free SigV4 adapter for S3-compatible private storage.

    The signed PUT requires ``If-None-Match: *``. S3 evaluates that condition
    atomically, so the random quarantine key can be written exactly once even
    while the short-lived URL is still valid.
    """

    def __init__(self, config: S3UploadConfig) -> None:
        self._config = config
        self._endpoint = config.endpoint.rstrip("/")
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=context),
        )

    def ensure_available(self) -> None:
        return None

    async def issue_single_use_put(
        self,
        *,
        object_key: str,
        size_bytes: int,
        media_type: str,
        expires_in: timedelta,
    ) -> UploadPutGrant:
        self.ensure_available()
        _require_object_key(object_key)
        seconds = int(expires_in.total_seconds())
        if not 30 <= seconds <= 900:
            raise ValueError("upload grant TTL must be between 30 and 900 seconds")
        if not 0 < size_bytes <= 536_870_912:
            raise ValueError("upload size is invalid")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=seconds)
        host = urllib.parse.urlsplit(self._endpoint).netloc
        path = self._object_path(object_key)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        credential_scope = f"{day}/{self._config.region}/s3/aws4_request"
        signed_headers = "content-length;content-type;host;if-none-match"
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self._config.access_key}/{credential_scope}",
            "X-Amz-Date": timestamp,
            "X-Amz-Expires": str(seconds),
            "X-Amz-SignedHeaders": signed_headers,
        }
        canonical_query = _canonical_query(query)
        canonical_headers = (
            f"content-length:{size_bytes}\n"
            f"content-type:{media_type}\n"
            f"host:{host}\n"
            "if-none-match:*\n"
        )
        canonical_request = (
            f"PUT\n{path}\n{canonical_query}\n{canonical_headers}\n"
            f"{signed_headers}\nUNSIGNED-PAYLOAD"
        )
        string_to_sign = _string_to_sign(
            timestamp, credential_scope, canonical_request
        )
        query["X-Amz-Signature"] = hmac.new(
            self._signing_key(day), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{self._endpoint}{path}?{_canonical_query(query)}"
        return UploadPutGrant(
            url=url,
            headers={
                "Content-Length": str(size_bytes),
                "Content-Type": media_type,
                "If-None-Match": "*",
            },
            expires_at=expires_at,
        )

    async def issue_bounded_get(
        self,
        *,
        object_key: str,
        expires_in: timedelta,
    ) -> UploadGetGrant:
        """Issue a short-lived GET after a one-use DB lease is redeemed.

        S3 URLs are time bounded rather than intrinsically single use.  The
        surrounding credential lease is consumed atomically before this URL is
        minted, and the Builder validates the exact digest and byte count.
        """

        self.ensure_available()
        _require_object_key(object_key)
        seconds = int(expires_in.total_seconds())
        if not 1 <= seconds <= 300:
            raise ValueError("upload read grant TTL must be between 1 and 300 seconds")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=seconds)
        host = urllib.parse.urlsplit(self._endpoint).netloc
        path = self._object_path(object_key)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        credential_scope = f"{day}/{self._config.region}/s3/aws4_request"
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self._config.access_key}/{credential_scope}",
            "X-Amz-Date": timestamp,
            "X-Amz-Expires": str(seconds),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = _canonical_query(query)
        canonical_request = (
            f"GET\n{path}\n{canonical_query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
        )
        string_to_sign = _string_to_sign(
            timestamp, credential_scope, canonical_request
        )
        query["X-Amz-Signature"] = hmac.new(
            self._signing_key(day), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return UploadGetGrant(
            url=f"{self._endpoint}{path}?{_canonical_query(query)}",
            headers={},
            expires_at=expires_at,
        )

    async def head(self, object_key: str) -> UploadObjectMetadata:
        return await asyncio.to_thread(self._head_sync, object_key)

    async def copy_to(
        self, object_key: str, sink: BinaryIO, *, max_bytes: int
    ) -> UploadObjectMetadata:
        if not 0 < max_bytes <= 536_870_912:
            raise ValueError("object read limit is invalid")
        return await asyncio.to_thread(
            self._copy_to_sync, object_key, sink, max_bytes
        )

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(self._delete_sync, object_key)

    def _head_sync(self, object_key: str) -> UploadObjectMetadata:
        with self._request_sync("HEAD", object_key, None) as response:
            return _metadata_from_headers(response.headers)

    def _copy_to_sync(
        self, object_key: str, sink: BinaryIO, max_bytes: int
    ) -> UploadObjectMetadata:
        with self._request_sync("GET", object_key, None) as response:
            metadata = _metadata_from_headers(response.headers)
            if metadata.size_bytes > max_bytes:
                raise UploadVerificationFailed("object exceeds verification limit")
            copied = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise UploadVerificationFailed("object exceeds verification limit")
                sink.write(chunk)
            if copied != metadata.size_bytes:
                raise UploadVerificationFailed("object size changed during verification")
            return metadata

    def _delete_sync(self, object_key: str) -> None:
        with self._request_sync("DELETE", object_key, None):
            return None

    def _request_sync(
        self, method: str, object_key: str, body: bytes | None
    ) -> object:
        self.ensure_available()
        _require_object_key(object_key)
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        host = urllib.parse.urlsplit(self._endpoint).netloc
        path = self._object_path(object_key)
        payload_hash = hashlib.sha256(body or b"").hexdigest()
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{timestamp}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = (
            f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        scope = f"{day}/{self._config.region}/s3/aws4_request"
        signature = hmac.new(
            self._signing_key(day),
            _string_to_sign(timestamp, scope, canonical_request).encode(),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._config.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(
            f"{self._endpoint}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": authorization,
                "Host": host,
                "X-Amz-Content-Sha256": payload_hash,
                "X-Amz-Date": timestamp,
            },
        )
        try:
            return self._opener.open(request, timeout=20)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise UploadVerificationFailed("object store request failed") from exc

    def _object_path(self, object_key: str) -> str:
        return "/" + "/".join(
            urllib.parse.quote(part, safe="-_.~")
            for part in (self._config.bucket, *object_key.split("/"))
        )

    def _signing_key(self, day: str) -> bytes:
        date_key = hmac.new(
            ("AWS4" + self._config.secret_key).encode(), day.encode(), hashlib.sha256
        ).digest()
        region_key = hmac.new(
            date_key, self._config.region.encode(), hashlib.sha256
        ).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


class FakeUploadObjectStore:
    """Strict fake: grants are one-shot and every read remains streaming."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._grants: dict[str, tuple[str, int, str, bool]] = {}
        self._get_grants: dict[str, tuple[str, bool]] = {}

    def ensure_available(self) -> None:
        return None

    async def issue_single_use_put(
        self,
        *,
        object_key: str,
        size_bytes: int,
        media_type: str,
        expires_in: timedelta,
    ) -> UploadPutGrant:
        del expires_in
        _require_object_key(object_key)
        token = secrets.token_urlsafe(32)
        self._grants[token] = (object_key, size_bytes, media_type, False)
        return UploadPutGrant(
            url=f"https://upload.invalid/once/{token}",
            headers={
                "Content-Length": str(size_bytes),
                "Content-Type": media_type,
                "If-None-Match": "*",
            },
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    async def issue_bounded_get(
        self,
        *,
        object_key: str,
        expires_in: timedelta,
    ) -> UploadGetGrant:
        seconds = int(expires_in.total_seconds())
        if not 1 <= seconds <= 300:
            raise ValueError("upload read grant TTL must be between 1 and 300 seconds")
        await self.head(object_key)
        token = secrets.token_urlsafe(32)
        self._get_grants[token] = (object_key, False)
        return UploadGetGrant(
            url=f"https://upload.invalid/read-once/{token}",
            headers={},
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        )

    def get_from_grant(self, url: str) -> bytes:
        token = url.rsplit("/", 1)[-1]
        grant = self._get_grants.get(token)
        if grant is None or grant[1]:
            raise UploadVerificationFailed("upload read grant is invalid")
        object_key, _consumed = grant
        try:
            content = self._objects[object_key][0]
        except KeyError as exc:
            raise UploadVerificationFailed("object is unavailable") from exc
        self._get_grants[token] = (object_key, True)
        return bytes(content)

    def put_from_grant(
        self, url: str, content: bytes, *, headers: Mapping[str, str]
    ) -> None:
        token = url.rsplit("/", 1)[-1]
        grant = self._grants.get(token)
        if grant is None:
            raise UploadVerificationFailed("upload grant is invalid")
        key, size, media_type, consumed = grant
        lowered = {name.lower(): value for name, value in headers.items()}
        if consumed or key in self._objects:
            raise UploadVerificationFailed("upload grant was already consumed")
        if (
            len(content) != size
            or lowered.get("content-length") != str(size)
            or lowered.get("content-type") != media_type
            or lowered.get("if-none-match") != "*"
        ):
            raise UploadVerificationFailed("upload does not match its grant")
        self._objects[key] = (bytes(content), media_type)
        self._grants[token] = (key, size, media_type, True)

    def seed(self, object_key: str, content: bytes, media_type: str) -> None:
        _require_object_key(object_key)
        self._objects[object_key] = (bytes(content), media_type)

    async def head(self, object_key: str) -> UploadObjectMetadata:
        try:
            content, media_type = self._objects[object_key]
        except KeyError as exc:
            raise UploadVerificationFailed("object is unavailable") from exc
        return UploadObjectMetadata(len(content), media_type)

    async def copy_to(
        self, object_key: str, sink: BinaryIO, *, max_bytes: int
    ) -> UploadObjectMetadata:
        metadata = await self.head(object_key)
        if metadata.size_bytes > max_bytes:
            raise UploadVerificationFailed("object exceeds verification limit")
        content = self._objects[object_key][0]
        for offset in range(0, len(content), 64 * 1024):
            sink.write(content[offset : offset + 64 * 1024])
        return metadata

    async def delete(self, object_key: str) -> None:
        self._objects.pop(object_key, None)


def sha256_digest(stream: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise UploadVerificationFailed("object exceeds verification limit")
        digest.update(chunk)
    stream.seek(0)
    return f"sha256:{digest.hexdigest()}", size


def checksum_header_value(digest: str) -> str:
    if not digest.startswith("sha256:"):
        raise ValueError("digest is invalid")
    try:
        raw = bytes.fromhex(digest.removeprefix("sha256:"))
    except ValueError as exc:
        raise ValueError("digest is invalid") from exc
    if len(raw) != 32:
        raise ValueError("digest is invalid")
    return base64.b64encode(raw).decode("ascii")


def _metadata_from_headers(headers: object) -> UploadObjectMetadata:
    try:
        raw_size = headers.get("Content-Length")  # type: ignore[attr-defined]
        raw_type = headers.get_content_type()  # type: ignore[attr-defined]
        size = int(raw_size)
    except (AttributeError, TypeError, ValueError) as exc:
        raise UploadVerificationFailed("object metadata is invalid") from exc
    if not 0 < size <= 536_870_912 or not isinstance(raw_type, str):
        raise UploadVerificationFailed("object metadata is invalid")
    return UploadObjectMetadata(size, raw_type.lower())


def _canonical_query(values: Mapping[str, str]) -> str:
    return "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}="
        f"{urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted(values.items())
    )


def _string_to_sign(timestamp: str, scope: str, canonical_request: str) -> str:
    return (
        "AWS4-HMAC-SHA256\n"
        f"{timestamp}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )


def _safe_component(value: str, maximum: int) -> bool:
    return 0 < len(value) <= maximum and all(
        character.isalnum() or character in "-_." for character in value
    )


def _require_object_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or value.startswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("object key is invalid")
    return value


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None
