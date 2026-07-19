from __future__ import annotations

import hashlib
import io
import base64
import binascii
import os
import re
import stat
import struct
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol
from collections.abc import Mapping
from urllib.parse import urlsplit

from lae_store import (
    S3CompatibleUploadStore,
    S3SigV4UploadStore,
    S3UploadConfig,
    PostgresUploadStore,
    UploadScanClaim,
    UploadVerificationFailed,
    sha256_digest,
    create_postgres_engine,
    create_session_factory,
)


_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_HTML_START = re.compile(r"^(?:<!--.*?-->\s*)*(?:<!doctype\s+html(?:\s[^>]*)?>|<html(?:\s|>))", re.I | re.S)
_NESTED_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".jar",
    ".war",
    ".ear",
)
_EXECUTABLE_SUFFIXES = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".com",
    ".scr",
    ".bat",
    ".cmd",
    ".ps1",
    ".msi",
    ".apk",
    ".ipa",
    ".appimage",
    ".sh",
)
_ARCHIVE_MAGICS = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"\x1f\x8b",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"BZh",
    b"\xfd7zXZ\x00",
)
_EXECUTABLE_MAGICS = (
    b"MZ",
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
)


class StaticArtifactRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StaticValidationPolicy:
    max_archive_bytes: int
    max_unpacked_bytes: int
    max_files: int = 2_000
    max_path_bytes: int = 240
    max_compression_ratio: int = 100

    def __post_init__(self) -> None:
        if not 1 <= self.max_archive_bytes <= 536_870_912:
            raise ValueError("archive byte policy is invalid")
        if not self.max_archive_bytes <= self.max_unpacked_bytes <= 2_147_483_648:
            raise ValueError("unpacked byte policy is invalid")
        if not 1 <= self.max_files <= 10_000:
            raise ValueError("file count policy is invalid")
        if not 64 <= self.max_path_bytes <= 1_024:
            raise ValueError("path byte policy is invalid")
        if not 1 <= self.max_compression_ratio <= 1_000:
            raise ValueError("compression ratio policy is invalid")


@dataclass(frozen=True, slots=True)
class StaticArtifactFacts:
    source_tree_digest: str
    file_count: int
    unpacked_bytes: int


class UploadScannerStore(Protocol):
    async def claim_scan(self, *, worker_id: str) -> UploadScanClaim | None: ...

    async def scan_cancel_requested(self, claim: UploadScanClaim) -> bool: ...

    async def finish_scan(
        self,
        claim: UploadScanClaim,
        *,
        worker_id: str,
        source_tree_digest: str,
    ) -> object: ...

    async def fail_scan(
        self,
        claim: UploadScanClaim,
        *,
        worker_id: str,
        failure_code: str,
    ) -> object: ...

    async def mark_scan_canceled(
        self, claim: UploadScanClaim, *, worker_id: str
    ) -> object: ...

    async def claim_cleanup(self) -> UploadScanClaim | None: ...

    async def finish_delete(self, scope: object, upload_id: str) -> object: ...

    async def mark_cleanup_failed(self, claim: UploadScanClaim) -> object: ...


class StaticUploadScanner:
    """Worker-side, fail-closed scanner for immutable static source uploads."""

    def __init__(
        self,
        store: UploadScannerStore,
        objects: S3CompatibleUploadStore,
        *,
        worker_id: str,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("scanner worker id is invalid")
        self._store = store
        self._objects = objects
        self._worker_id = worker_id

    async def run_once(self) -> bool:
        claim = await self._store.claim_scan(worker_id=self._worker_id)
        if claim is None:
            return False
        if await self._store.scan_cancel_requested(claim):
            await self._store.mark_scan_canceled(claim, worker_id=self._worker_id)
            return True
        try:
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as stream:
                metadata = await self._objects.copy_to(
                    claim.upload.object_key,
                    stream,
                    max_bytes=claim.upload.expected_bytes,
                )
                digest, size = sha256_digest(
                    stream, max_bytes=claim.upload.expected_bytes
                )
                if (
                    metadata.size_bytes != claim.upload.actual_bytes
                    or size != claim.upload.actual_bytes
                    or metadata.media_type != claim.upload.media_type
                    or digest != claim.upload.actual_sha256
                ):
                    raise StaticArtifactRejected("LAE_UPLOAD_OBJECT_CHANGED")
                facts = validate_static_artifact(
                    stream,
                    filename=claim.upload.filename,
                    media_type=claim.upload.media_type,
                    policy=StaticValidationPolicy(
                        max_archive_bytes=claim.upload.expected_bytes,
                        max_unpacked_bytes=claim.max_unpacked_bytes,
                    ),
                )
            if await self._store.scan_cancel_requested(claim):
                await self._store.mark_scan_canceled(
                    claim, worker_id=self._worker_id
                )
                return True
            await self._store.finish_scan(
                claim,
                worker_id=self._worker_id,
                source_tree_digest=facts.source_tree_digest,
            )
        except StaticArtifactRejected as exc:
            if await self._store.scan_cancel_requested(claim):
                await self._store.mark_scan_canceled(
                    claim, worker_id=self._worker_id
                )
            else:
                await self._store.fail_scan(
                    claim,
                    worker_id=self._worker_id,
                    failure_code=exc.code,
                )
        except (UploadVerificationFailed, OSError, zipfile.BadZipFile):
            if await self._store.scan_cancel_requested(claim):
                await self._store.mark_scan_canceled(
                    claim, worker_id=self._worker_id
                )
            else:
                await self._store.fail_scan(
                    claim,
                    worker_id=self._worker_id,
                    failure_code="LAE_UPLOAD_OBJECT_INVALID",
                )
        return True

    async def cleanup_once(self) -> bool:
        claim = await self._store.claim_cleanup()
        if claim is None:
            return False
        try:
            await self._objects.delete(claim.upload.object_key)
            from lae_store import TenantScope

            await self._store.finish_delete(
                TenantScope(claim.tenant_id), claim.upload.id
            )
        except UploadVerificationFailed:
            await self._store.mark_cleanup_failed(claim)
        return True


@dataclass(slots=True)
class StaticUploadScannerRuntime:
    scanner: StaticUploadScanner
    engine: object

    async def close(self) -> None:
        dispose = getattr(self.engine, "dispose", None)
        if dispose is not None:
            await dispose()


def build_static_upload_scanner_from_env(
    environ: Mapping[str, str] | None = None,
) -> StaticUploadScannerRuntime:
    values = os.environ if environ is None else environ
    environment = values.get("LAE_ENVIRONMENT", "development").strip().lower()
    if values.get("LAE_UPLOAD_SCANNER_ENABLED") != "1":
        raise ValueError("static upload scanner is not enabled")
    if values.get("LAE_UPLOAD_DRIVER", "disabled").strip().lower() != "s3":
        raise ValueError("static upload scanner requires the S3 upload driver")
    try:
        hash_key = base64.b64decode(
            _required(values, "LAE_UPLOAD_HMAC_KEY"), validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload scanner HMAC key is invalid") from exc
    if len(hash_key) < 32:
        raise ValueError("upload scanner HMAC key must contain at least 256 bits")
    endpoint = _required(values, "LAE_UPLOAD_S3_ENDPOINT")
    parsed = urlsplit(endpoint)
    if parsed.hostname is None:
        raise ValueError("upload scanner endpoint is invalid")
    objects = S3SigV4UploadStore(
        S3UploadConfig(
            endpoint=endpoint,
            bucket=_required(values, "LAE_UPLOAD_S3_BUCKET"),
            region=values.get("LAE_UPLOAD_S3_REGION", "us-east-1"),
            access_key=_required(values, "LAE_UPLOAD_S3_ACCESS_KEY"),
            secret_key=_required(values, "LAE_UPLOAD_S3_SECRET_KEY"),
            production=environment in {"prod", "production"},
        )
    )
    engine = create_postgres_engine(_required(values, "LAE_DATABASE_URL"))
    sessions = create_session_factory(engine)
    scanner = StaticUploadScanner(
        PostgresUploadStore(
            sessions,
            hash_key=hash_key,
            reservation_ttl=timedelta(
                seconds=int(values.get("LAE_UPLOAD_RESERVATION_TTL_SECONDS", "3600"))
            ),
        ),
        objects,
        worker_id=values.get("LAE_UPLOAD_SCANNER_ID", "lae-upload-scanner-1"),
    )
    return StaticUploadScannerRuntime(scanner=scanner, engine=engine)


def _required(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise ValueError(f"{key} is required")
    return value


def validate_static_artifact(
    stream: BinaryIO,
    *,
    filename: str,
    media_type: str,
    policy: StaticValidationPolicy,
) -> StaticArtifactFacts:
    stream.seek(0, io.SEEK_END)
    archive_size = stream.tell()
    stream.seek(0)
    if not 0 < archive_size <= policy.max_archive_bytes:
        raise StaticArtifactRejected("LAE_UPLOAD_SIZE_INVALID")
    lowered = filename.lower()
    if lowered.endswith(".html") and media_type == "text/html":
        content = _bounded_read(stream, policy.max_archive_bytes)
        _validate_html(content)
        return StaticArtifactFacts(
            _tree_digest((("index.html", hashlib.sha256(content).hexdigest(), len(content)),)),
            1,
            len(content),
        )
    if not lowered.endswith(".zip") or media_type != "application/zip":
        raise StaticArtifactRejected("LAE_UPLOAD_TYPE_INVALID")
    return _validate_zip(stream, policy)


def _validate_zip(stream: BinaryIO, policy: StaticValidationPolicy) -> StaticArtifactFacts:
    _validate_eocd_and_raw_names(stream, policy)
    stream.seek(0)
    try:
        archive = zipfile.ZipFile(stream, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID") from exc
    seen: set[str] = set()
    files: list[tuple[str, str, int]] = []
    total_declared = 0
    total_compressed = 0
    root_html: bytes | None = None
    try:
        infos = archive.infolist()
        if len(infos) > policy.max_files * 2:
            raise StaticArtifactRejected("LAE_UPLOAD_TOO_MANY_FILES")
        for info in infos:
            path, folded = _canonical_zip_path(info.filename, policy.max_path_bytes)
            if folded in seen:
                raise StaticArtifactRejected("LAE_UPLOAD_DUPLICATE_PATH")
            seen.add(folded)
            _validate_zip_metadata(info, path)
            if info.is_dir():
                continue
            if len(files) >= policy.max_files:
                raise StaticArtifactRejected("LAE_UPLOAD_TOO_MANY_FILES")
            total_declared += info.file_size
            total_compressed += info.compress_size
            if total_declared > policy.max_unpacked_bytes:
                raise StaticArtifactRejected("LAE_UPLOAD_UNPACKED_TOO_LARGE")
            if info.file_size > max(1, info.compress_size) * policy.max_compression_ratio:
                raise StaticArtifactRejected("LAE_UPLOAD_COMPRESSION_RATIO")
            digest = hashlib.sha256()
            actual = 0
            prefix = bytearray()
            try:
                with archive.open(info, "r") as member:
                    while True:
                        chunk = member.read(1024 * 1024)
                        if not chunk:
                            break
                        actual += len(chunk)
                        if actual > info.file_size or sum(item[2] for item in files) + actual > policy.max_unpacked_bytes:
                            raise StaticArtifactRejected("LAE_UPLOAD_UNPACKED_TOO_LARGE")
                        if len(prefix) < 1024:
                            prefix.extend(chunk[: 1024 - len(prefix)])
                        digest.update(chunk)
            except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
                raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID") from exc
            if actual != info.file_size:
                raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")
            _reject_nested_or_executable(path, bytes(prefix))
            files.append((path, digest.hexdigest(), actual))
            if path == "index.html":
                with archive.open(info, "r") as member:
                    root_html = _bounded_read(member, min(info.file_size, 2 * 1024 * 1024))
        if total_declared > max(1, total_compressed) * policy.max_compression_ratio:
            raise StaticArtifactRejected("LAE_UPLOAD_COMPRESSION_RATIO")
        if root_html is None:
            raise StaticArtifactRejected("LAE_UPLOAD_INDEX_REQUIRED")
        _validate_html(root_html)
    finally:
        archive.close()
    return StaticArtifactFacts(
        _tree_digest(tuple(sorted(files))),
        len(files),
        sum(item[2] for item in files),
    )


def _validate_eocd_and_raw_names(stream: BinaryIO, policy: StaticValidationPolicy) -> None:
    stream.seek(0, io.SEEK_END)
    size = stream.tell()
    tail_size = min(size, 65_557)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")
    try:
        signature, disk, central_disk, disk_entries, total_entries, central_size, central_offset, comment_size = struct.unpack(
            "<4s4H2LH", tail[offset : offset + 22]
        )
    except struct.error as exc:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID") from exc
    if (
        signature != b"PK\x05\x06"
        or disk != 0
        or central_disk != 0
        or disk_entries != total_entries
        or total_entries in {0, 0xFFFF}
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or comment_size != len(tail) - offset - 22
        or total_entries > policy.max_files * 2
        or b"PK\x06\x07" in tail
    ):
        raise StaticArtifactRejected("LAE_UPLOAD_MULTIDISK_OR_ZIP64")
    if central_offset + central_size > size - 22:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")
    stream.seek(central_offset)
    central = stream.read(central_size)
    cursor = 0
    counted = 0
    while cursor < len(central):
        if len(central) - cursor < 46 or central[cursor : cursor + 4] != b"PK\x01\x02":
            raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")
        name_len = int.from_bytes(central[cursor + 28 : cursor + 30], "little")
        extra_len = int.from_bytes(central[cursor + 30 : cursor + 32], "little")
        comment_len = int.from_bytes(central[cursor + 32 : cursor + 34], "little")
        disk_start = int.from_bytes(central[cursor + 34 : cursor + 36], "little")
        end = cursor + 46 + name_len + extra_len + comment_len
        raw_name = central[cursor + 46 : cursor + 46 + name_len]
        if end > len(central) or not raw_name or b"\x00" in raw_name or disk_start != 0:
            raise StaticArtifactRejected("LAE_UPLOAD_ZIP_PATH_INVALID")
        cursor = end
        counted += 1
    if cursor != len(central) or counted != total_entries:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")


def _canonical_zip_path(value: str, max_bytes: int) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized.startswith(("/", "\\"))
        or _DRIVE_PATH.match(normalized)
        or "\\" in normalized
        or "\x00" in normalized
        or len(normalized.encode("utf-8")) > max_bytes
    ):
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_PATH_INVALID")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_PATH_INVALID")
    canonical = "/".join(path.parts)
    if normalized.endswith("/"):
        canonical += "/"
    return canonical, canonical.casefold()


def _validate_zip_metadata(info: zipfile.ZipInfo, path: str) -> None:
    if info.flag_bits & 0x1:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_ENCRYPTED")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise StaticArtifactRejected("LAE_UPLOAD_ZIP_COMPRESSION_UNSUPPORTED")
    mode = (info.external_attr >> 16) & 0xFFFF
    if info.create_system == 3 and mode:
        file_type = stat.S_IFMT(mode)
        allowed = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
        if file_type not in {0, allowed}:
            raise StaticArtifactRejected("LAE_UPLOAD_ZIP_SPECIAL_FILE")
        if not info.is_dir() and mode & 0o111:
            raise StaticArtifactRejected("LAE_UPLOAD_EXECUTABLE_FORBIDDEN")
    for field_id, _value in _extra_fields(info.extra):
        if field_id in {0x000D, 0x756E}:
            raise StaticArtifactRejected("LAE_UPLOAD_ZIP_LINK_METADATA")
    if not info.is_dir():
        _reject_suffix(path)


def _extra_fields(extra: bytes) -> list[tuple[int, bytes]]:
    fields: list[tuple[int, bytes]] = []
    cursor = 0
    while cursor < len(extra):
        if len(extra) - cursor < 4:
            raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")
        field_id = int.from_bytes(extra[cursor : cursor + 2], "little")
        size = int.from_bytes(extra[cursor + 2 : cursor + 4], "little")
        end = cursor + 4 + size
        if end > len(extra):
            raise StaticArtifactRejected("LAE_UPLOAD_ZIP_INVALID")
        fields.append((field_id, extra[cursor + 4 : end]))
        cursor = end
    return fields


def _reject_suffix(path: str) -> None:
    lowered = path.casefold()
    if lowered.endswith(_NESTED_ARCHIVE_SUFFIXES):
        raise StaticArtifactRejected("LAE_UPLOAD_NESTED_ARCHIVE")
    if lowered.endswith(_EXECUTABLE_SUFFIXES):
        raise StaticArtifactRejected("LAE_UPLOAD_EXECUTABLE_FORBIDDEN")


def _reject_nested_or_executable(path: str, prefix: bytes) -> None:
    _reject_suffix(path)
    if prefix.startswith(_ARCHIVE_MAGICS) or (
        len(prefix) > 262 and prefix[257:262] == b"ustar"
    ):
        raise StaticArtifactRejected("LAE_UPLOAD_NESTED_ARCHIVE")
    if prefix.startswith(_EXECUTABLE_MAGICS):
        raise StaticArtifactRejected("LAE_UPLOAD_EXECUTABLE_FORBIDDEN")


def _validate_html(content: bytes) -> None:
    if not content or b"\x00" in content:
        raise StaticArtifactRejected("LAE_UPLOAD_HTML_INVALID")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StaticArtifactRejected("LAE_UPLOAD_HTML_ENCODING") from exc
    if not _HTML_START.match(text.lstrip()[:65_536]):
        raise StaticArtifactRejected("LAE_UPLOAD_HTML_INVALID")


def _tree_digest(files: tuple[tuple[str, str, int], ...]) -> str:
    digest = hashlib.sha256(b"lae-static-tree-v1\0")
    for path, file_digest, size in sorted(files):
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(file_digest))
    return f"sha256:{digest.hexdigest()}"


def _bounded_read(stream: BinaryIO, limit: int) -> bytes:
    content = stream.read(limit + 1)
    if len(content) > limit:
        raise StaticArtifactRejected("LAE_UPLOAD_UNPACKED_TOO_LARGE")
    return content
