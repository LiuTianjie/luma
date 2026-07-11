from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .config import DeployCredential
from .errors import CliError


_ERROR_CODE = re.compile(r"^LAE_[A-Z0-9_]{2,96}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the deploy token to a redirected origin."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_SAFE_OPENER = urllib.request.build_opener(_NoRedirect())


def _safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Any:
    return _SAFE_OPENER.open(request, timeout=timeout)


@dataclass(slots=True)
class ApiClient:
    base_url: str
    credential: DeployCredential
    timeout_seconds: float = 20.0
    opener: Callable[..., Any] = field(default=_safe_urlopen, repr=False)

    def __repr__(self) -> str:
        return (
            f"ApiClient(base_url={self.base_url!r}, credential=<redacted>, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    def get(
        self, path: str, *, query: Mapping[str, str | int] | None = None
    ) -> dict[str, Any]:
        return self.request("GET", path, query=query)

    def post(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST", path, body=body or {}, idempotency_key=idempotency_key
        )

    def patch(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "PATCH", path, body=body, idempotency_key=idempotency_key
        )

    def delete(
        self,
        path: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request("DELETE", path, idempotency_key=idempotency_key)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str | int] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise ValueError("unsupported HTTP method")
        url = _request_url(self.base_url, path, query)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.credential.value}",
            "User-Agent": "lae-cli/0.1",
        }
        data: bytes | None = None
        if body is not None:
            try:
                data = json.dumps(
                    body,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise CliError(
                    "LAE_CLI_REQUEST_INVALID",
                    "The request cannot be encoded safely.",
                    2,
                ) from exc
            if len(data) > 2 * 1024 * 1024:
                raise CliError(
                    "LAE_CLI_REQUEST_TOO_LARGE", "The request is too large.", 2
                )
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            if not _REQUEST_ID.fullmatch(idempotency_key):
                raise CliError(
                    "LAE_CLI_IDEMPOTENCY_KEY_INVALID",
                    "The idempotency key has an invalid format.",
                    2,
                )
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            with response:
                status = int(getattr(response, "status", 200))
                payload = _read_json_object(response)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise CliError(
                "LAE_API_UNAVAILABLE",
                "LAE is temporarily unavailable.",
                9,
                retryable=True,
            ) from exc
        if not 200 <= status < 300:
            raise CliError(
                "LAE_API_PROTOCOL_ERROR",
                "LAE returned an unexpected response status.",
                9,
                retryable=status >= 500,
            )
        return payload


def _request_url(
    base_url: str,
    path: str,
    query: Mapping[str, str | int] | None,
) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\\" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or "?" in path
        or "#" in path
    ):
        raise CliError("LAE_CLI_PATH_INVALID", "The API path is invalid.", 2)
    encoded_query = urllib.parse.urlencode(query or {}, doseq=False, safe="")
    return base_url.rstrip("/") + path + ("?" + encoded_query if encoded_query else "")


def _read_json_object(stream: Any) -> dict[str, Any]:
    raw = stream.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an oversized response.", 9
        )
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned invalid JSON.", 9
        ) from exc
    if not isinstance(value, dict):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid response shape.", 9
        )
    return value


def _http_error(error: urllib.error.HTTPError) -> CliError:
    try:
        payload = _read_json_object(error)
    except CliError:
        payload = {}
    envelope = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    raw_code = envelope.get("code")
    code = (
        raw_code
        if isinstance(raw_code, str) and _ERROR_CODE.fullmatch(raw_code)
        else "LAE_API_REQUEST_FAILED"
    )
    status = int(error.code)
    exit_code = _exit_code_for_status(status, code)
    safe_messages = {
        3: "Authentication or authorization is required.",
        4: "The operation requires additional user configuration.",
        5: "The request is not supported by LAE policy.",
        6: "The request exceeds the current plan or quota.",
        7: "Source validation failed.",
        9: "LAE is temporarily unavailable.",
    }
    return CliError(
        code,
        safe_messages.get(exit_code, "LAE rejected the request."),
        exit_code,
        retryable=status >= 500 or bool(envelope.get("retryable") is True),
    )


def _exit_code_for_status(status: int, code: str) -> int:
    if status in {401, 403}:
        return 3
    if code in {"LAE_UPLOAD_VERIFICATION_FAILED", "LAE_UPLOAD_SCAN_FAILED"}:
        return 7
    if status == 409 and any(part in code for part in ("CONFIG", "ENV", "CONFIRM")):
        return 4
    if status in {402, 429} or any(part in code for part in ("QUOTA", "PLAN", "LIMIT")):
        return 6
    if status in {409, 422}:
        return 5
    if status >= 500:
        return 9
    return 2
