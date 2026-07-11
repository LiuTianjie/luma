from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import socket
import ssl
import stat
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Mapping

from .errors import CliError


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_MAX_TRANSFER_RESPONSE_BYTES = 64 * 1024
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_FORBIDDEN_TRANSFER_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "cookie",
        "host",
        "proxy-authorization",
        "transfer-encoding",
    }
)


class _NoTransferRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_TRANSFER_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoTransferRedirect(),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def _transfer_urlopen(request: urllib.request.Request, *, timeout: float) -> Any:
    return _TRANSFER_OPENER.open(request, timeout=timeout)


@dataclass(slots=True)
class LocalUpload:
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    _stream: BinaryIO = field(repr=False)
    _signature: tuple[int, int, int, int, int] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "LocalUpload(filename=<redacted>, "
            f"media_type={self.media_type!r}, size_bytes={self.size_bytes!r}, "
            f"sha256={self.sha256!r})"
        )

    def __enter__(self) -> LocalUpload:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._stream.close()

    def request_body(self, application_id: str) -> dict[str, object]:
        return {
            "applicationId": application_id,
            "filename": self.filename,
            "mediaType": self.media_type,
            "sizeBytes": self.size_bytes,
            "sha256": self.sha256,
        }

    def rewind_verified(self) -> BinaryIO:
        try:
            current = _file_signature(os.fstat(self._stream.fileno()))
            if current != self._signature:
                raise CliError(
                    "LAE_CLI_UPLOAD_FILE_CHANGED",
                    "The upload file changed while it was being prepared.",
                    2,
                )
            self._stream.seek(0)
        except CliError:
            raise
        except (OSError, ValueError) as exc:
            raise _file_error() from exc
        return self._stream

    def verify_unchanged(self) -> None:
        try:
            current = _file_signature(os.fstat(self._stream.fileno()))
        except (OSError, ValueError) as exc:
            raise _file_error() from exc
        if current != self._signature:
            raise CliError(
                "LAE_CLI_UPLOAD_FILE_CHANGED",
                "The upload file changed while it was being transferred.",
                2,
            )


def open_local_upload(path: str) -> LocalUpload:
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise _file_error()
    filename = unicodedata.normalize("NFC", os.path.basename(path))
    lowered = filename.lower()
    if lowered.endswith(".html"):
        media_type = "text/html"
    elif lowered.endswith(".zip"):
        media_type = "application/zip"
    else:
        raise CliError(
            "LAE_CLI_UPLOAD_TYPE_UNSUPPORTED",
            "Only .html and .zip static artifacts can be uploaded.",
            5,
        )
    if (
        not filename
        or len(filename.encode("utf-8")) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in filename)
    ):
        raise _file_error()

    descriptor = -1
    stream: BinaryIO | None = None
    try:
        listed = os.lstat(path)
        if not stat.S_ISREG(listed.st_mode) or stat.S_ISLNK(listed.st_mode):
            raise _file_error()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or listed.st_dev != opened.st_dev
            or listed.st_ino != opened.st_ino
        ):
            raise _file_error()
        if opened.st_size <= 0:
            raise CliError(
                "LAE_CLI_UPLOAD_FILE_EMPTY", "The upload file is empty.", 2
            )
        if opened.st_size > MAX_UPLOAD_BYTES:
            raise CliError(
                "LAE_CLI_UPLOAD_FILE_TOO_LARGE",
                "The upload file exceeds the 512 MiB limit.",
                6,
            )
        signature = _file_signature(opened)
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            observed += len(chunk)
            if observed > MAX_UPLOAD_BYTES:
                raise CliError(
                    "LAE_CLI_UPLOAD_FILE_TOO_LARGE",
                    "The upload file exceeds the 512 MiB limit.",
                    6,
                )
            digest.update(chunk)
        if observed != opened.st_size or _file_signature(os.fstat(stream.fileno())) != signature:
            raise CliError(
                "LAE_CLI_UPLOAD_FILE_CHANGED",
                "The upload file changed while it was being hashed.",
                2,
            )
        stream.seek(0)
        return LocalUpload(
            filename=filename,
            media_type=media_type,
            size_bytes=observed,
            sha256="sha256:" + digest.hexdigest(),
            _stream=stream,
            _signature=signature,
        )
    except CliError:
        if stream is not None:
            stream.close()
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        if stream is not None:
            stream.close()
        if descriptor >= 0:
            os.close(descriptor)
        raise _file_error() from exc


def put_upload_transfer(
    transfer: object,
    source: LocalUpload,
    *,
    timeout_seconds: float = 600,
    opener: Callable[..., Any] = _transfer_urlopen,
) -> None:
    if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 3600:
        raise CliError(
            "LAE_CLI_ARGUMENT_INVALID", "Transfer timeout is invalid.", 2
        )
    url, headers = _validated_transfer(transfer, source)
    stream = source.rewind_verified()
    request = urllib.request.Request(
        url,
        data=stream,
        headers=headers,
        method="PUT",
    )
    try:
        response = opener(request, timeout=timeout_seconds)
        with response:
            status = int(getattr(response, "status", 200))
            response_body = response.read(_MAX_TRANSFER_RESPONSE_BYTES + 1)
            if len(response_body) > _MAX_TRANSFER_RESPONSE_BYTES:
                raise CliError(
                    "LAE_API_PROTOCOL_ERROR",
                    "The upload transfer returned an oversized response.",
                    9,
                )
    except urllib.error.HTTPError as exc:
        if 300 <= int(exc.code) < 400:
            raise CliError(
                "LAE_UPLOAD_TRANSFER_REDIRECTED",
                "The one-time upload transfer attempted an unsafe redirect.",
                5,
            ) from None
        raise CliError(
            "LAE_UPLOAD_TRANSFER_FAILED",
            "The one-time upload transfer failed.",
            9,
            retryable=int(exc.code) >= 500 or int(exc.code) in {408, 429},
        ) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise CliError(
            "LAE_UPLOAD_TRANSFER_FAILED",
            "The one-time upload transfer failed.",
            9,
            retryable=True,
        ) from exc
    if 300 <= status < 400:
        raise CliError(
            "LAE_UPLOAD_TRANSFER_REDIRECTED",
            "The one-time upload transfer attempted an unsafe redirect.",
            5,
        )
    if not 200 <= status < 300:
        raise CliError(
            "LAE_UPLOAD_TRANSFER_FAILED",
            "The one-time upload transfer failed.",
            9,
            retryable=status >= 500,
        )
    source.verify_unchanged()


def _validated_transfer(
    value: object, source: LocalUpload
) -> tuple[str, dict[str, str]]:
    if not isinstance(value, Mapping) or value.get("method") != "PUT":
        raise _protocol_error()
    raw_url = value.get("url")
    raw_headers = value.get("headers")
    if not isinstance(raw_url, str) or not isinstance(raw_headers, Mapping):
        raise _protocol_error()
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        _ = parsed.port
    except ValueError as exc:
        raise _protocol_error() from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        is_ip = False
    blocked_host = (
        is_ip
        or hostname == "localhost"
        or "." not in hostname
        or hostname.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home.arpa")
        )
    )
    if (
        parsed.scheme != "https"
        or not hostname
        or blocked_host
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
        or len(raw_url) > 8192
    ):
        raise _protocol_error()

    headers: dict[str, str] = {}
    lowered: set[str] = set()
    if not 1 <= len(raw_headers) <= 64:
        raise _protocol_error()
    for raw_name, raw_value in raw_headers.items():
        if (
            not isinstance(raw_name, str)
            or not _HEADER_NAME.fullmatch(raw_name)
            or not isinstance(raw_value, str)
            or not raw_value
            or len(raw_value) > 8192
            or "\r" in raw_value
            or "\n" in raw_value
            or "\0" in raw_value
        ):
            raise _protocol_error()
        name = raw_name.casefold()
        if name in lowered or name in _FORBIDDEN_TRANSFER_HEADERS:
            raise _protocol_error()
        lowered.add(name)
        headers[raw_name] = raw_value
    normalized = {name.casefold(): value for name, value in headers.items()}
    if (
        normalized.get("content-length") != str(source.size_bytes)
        or normalized.get("content-type") != source.media_type
    ):
        raise _protocol_error()
    return raw_url, headers


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _file_error() -> CliError:
    return CliError(
        "LAE_CLI_UPLOAD_FILE_INVALID",
        "The upload path must identify one regular, non-symlink file.",
        2,
    )


def _protocol_error() -> CliError:
    return CliError(
        "LAE_API_PROTOCOL_ERROR",
        "LAE returned an invalid one-time upload transfer.",
        9,
    )
