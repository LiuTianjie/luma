from __future__ import annotations

import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lae_store import (
    CredentialLeaseRejected,
    CredentialRedemptionRequest,
    PostgresCredentialRedemptionBroker,
)


CREDENTIAL_REDEMPTION_PATH = "/v1/internal/credential-leases/redeem"
CREDENTIAL_REDEMPTION_MAX_BODY_BYTES = 16 * 1024
_BEARER = re.compile(r"^Bearer (?P<token>[^\s,]{1,8192})$", re.IGNORECASE)
_GENERIC_MESSAGE = "Credential lease redemption failed"


@dataclass(frozen=True, slots=True)
class InternalBrokerToken:
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        encoded = self.value.encode("utf-8") if isinstance(self.value, str) else b""
        if (
            not 32 <= len(encoded) <= 8192
            or any(byte <= 0x20 or byte == 0x7F for byte in encoded)
            or len(set(encoded)) < 8
        ):
            raise ValueError("credential broker service token is not strong enough")

    def matches(self, candidate: str) -> bool:
        if not isinstance(candidate, str):
            return False
        return hmac.compare_digest(
            self.value.encode("utf-8"), candidate.encode("utf-8")
        )


@dataclass(frozen=True, slots=True)
class CredentialBrokerRuntime:
    broker: Any
    token: InternalBrokerToken


def credential_broker_runtime_from_env(
    sessions: Any,
    *,
    connection_key_ring: Any | None,
    environment: str,
    environ: Mapping[str, str] | None = None,
) -> CredentialBrokerRuntime:
    values = os.environ if environ is None else environ
    raw_token = str(values.get("LAE_CREDENTIAL_BROKER_TOKEN") or "")
    token_file = str(values.get("LAE_CREDENTIAL_BROKER_TOKEN_FILE") or "").strip()
    if raw_token and token_file:
        raise ValueError("credential broker token source is ambiguous")
    if token_file:
        raw_token = _read_token_file(Path(token_file))
    if not raw_token:
        if environment in {"prod", "production"}:
            raise ValueError("production credential broker token is required")
        raise ValueError("credential broker token is not configured")
    return CredentialBrokerRuntime(
        broker=PostgresCredentialRedemptionBroker(sessions, connection_key_ring),
        token=InternalBrokerToken(raw_token),
    )


def _read_token_file(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("credential broker token file is unavailable") from None
    if (
        not str(path)
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or not 1 <= metadata.st_size <= 16 * 1024
    ):
        raise ValueError("credential broker token file is unsafe")
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError("credential broker token file is unavailable") from None


def _response(status: int, code: str, message: str) -> JSONResponse:
    response = JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _authorized(request: Request, token: InternalBrokerToken) -> bool:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return False
    match = _BEARER.fullmatch(values[0])
    return match is not None and token.matches(match.group("token"))


async def _bounded_json_body(request: Request) -> dict[str, Any]:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        raise TypeError("content type is invalid")
    media_type = content_types[0].split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise TypeError("content type is invalid")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("body is invalid") from None
        if length < 0 or length > CREDENTIAL_REDEMPTION_MAX_BODY_BYTES:
            raise OverflowError("body is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > CREDENTIAL_REDEMPTION_MAX_BODY_BYTES:
            raise OverflowError("body is too large")
    if not body:
        raise ValueError("body is invalid")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("body is invalid") from None
    if not isinstance(decoded, dict):
        raise ValueError("body is invalid")
    return decoded


def register_credential_broker_route(
    app: FastAPI,
    runtime_getter: Any,
) -> None:
    """Register the service-principal-only broker outside user auth/CSRF."""

    @app.post(CREDENTIAL_REDEMPTION_PATH)
    async def redeem_credential(request: Request) -> JSONResponse:
        runtime = runtime_getter()
        if runtime is None:
            return _response(
                503,
                "LAE_CREDENTIAL_BROKER_UNAVAILABLE",
                _GENERIC_MESSAGE,
            )
        if not _authorized(request, runtime.token):
            return _response(
                401,
                "LAE_CREDENTIAL_BROKER_UNAUTHENTICATED",
                _GENERIC_MESSAGE,
            )
        try:
            body = await _bounded_json_body(request)
        except TypeError:
            return _response(
                415,
                "LAE_CREDENTIAL_BROKER_MEDIA_TYPE_INVALID",
                _GENERIC_MESSAGE,
            )
        except OverflowError:
            return _response(
                413,
                "LAE_CREDENTIAL_BROKER_REQUEST_TOO_LARGE",
                _GENERIC_MESSAGE,
            )
        except ValueError:
            return _response(
                400,
                "LAE_CREDENTIAL_BROKER_REQUEST_INVALID",
                _GENERIC_MESSAGE,
            )

        try:
            binding = CredentialRedemptionRequest.from_body(body)
            result = await runtime.broker.redeem(binding)
        except CredentialLeaseRejected:
            return _response(
                409,
                "LAE_CREDENTIAL_LEASE_UNAVAILABLE",
                _GENERIC_MESSAGE,
            )
        except Exception:
            # Database/crypto/config failures are deliberately collapsed. Do
            # not log exception text: drivers may include SQL parameters or a
            # broker-controlled repository URL.
            return _response(
                503,
                "LAE_CREDENTIAL_BROKER_UNAVAILABLE",
                _GENERIC_MESSAGE,
            )
        response = JSONResponse(result.public_body())
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response


__all__ = [
    "CREDENTIAL_REDEMPTION_MAX_BODY_BYTES",
    "CREDENTIAL_REDEMPTION_PATH",
    "CredentialBrokerRuntime",
    "InternalBrokerToken",
    "credential_broker_runtime_from_env",
    "register_credential_broker_route",
]
