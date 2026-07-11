from __future__ import annotations

import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lae_store import (
    CredentialLeaseRejected,
    ObjectSourceRedemptionRequest,
    ObjectSourceRedemptionResult,
    PostgresObjectSourceRedemptionBroker,
    S3SigV4UploadStore,
    S3UploadConfig,
)

from .credential_broker_api import (
    InternalBrokerToken,
    _authorized,
    _bounded_json_body,
    _read_token_file,
    _response,
)


OBJECT_SOURCE_REDEMPTION_PATH = "/v1/internal/object-source-leases/redeem"
_GENERIC_MESSAGE = "Object source lease redemption failed"


class ObjectSourceBrokerService:
    """Turn a consumed DB lease into an in-memory, short-lived S3 GET URL."""

    def __init__(self, broker: Any, objects: Any, *, allowed_host: str) -> None:
        if not isinstance(allowed_host, str) or allowed_host != allowed_host.lower():
            raise ValueError("object source broker host is invalid")
        self._broker = broker
        self._objects = objects
        self._allowed_host = allowed_host

    async def redeem(
        self, request: ObjectSourceRedemptionRequest
    ) -> ObjectSourceRedemptionResult:
        claim = await self._broker.redeem(request)
        if not hmac.compare_digest(claim.allowed_host, self._allowed_host):
            raise CredentialLeaseRejected("object source lease is unavailable")
        try:
            grant = await self._objects.issue_bounded_get(
                object_key=claim.object_key,
                expires_in=timedelta(seconds=claim.ttl_seconds),
            )
            parsed = urllib.parse.urlsplit(grant.url)
            port = parsed.port
        except Exception:
            # Signing and parsing errors are intentionally secret-free.
            raise CredentialLeaseRejected(
                "object source lease is unavailable"
            ) from None
        if (
            grant.headers
            or parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port is not None and not 1 <= port <= 65535
            or not hmac.compare_digest(parsed.hostname.lower(), self._allowed_host)
        ):
            raise CredentialLeaseRejected("object source lease is unavailable")
        now = int(time.time())
        expires_at = int(grant.expires_at.timestamp())
        if not now < expires_at <= now + claim.ttl_seconds + 1:
            raise CredentialLeaseRejected("object source lease is unavailable")
        return ObjectSourceRedemptionResult(
            request=request,
            expires_at=expires_at,
            allowed_host=self._allowed_host,
            object_url=grant.url,
        )


@dataclass(frozen=True, slots=True)
class ObjectSourceBrokerRuntime:
    broker: Any
    token: InternalBrokerToken


def object_source_broker_runtime_from_env(
    sessions: Any,
    *,
    environment: str,
    environ: Mapping[str, str] | None = None,
) -> ObjectSourceBrokerRuntime:
    values = os.environ if environ is None else environ
    raw_token, token_file = _token_source(values)
    if token_file:
        raw_token = _read_token_file(Path(token_file))
    if not raw_token:
        raise ValueError("object source broker token is not configured")
    if str(values.get("LAE_UPLOAD_DRIVER") or "disabled").strip().lower() != "s3":
        raise ValueError("object source broker requires the S3 upload driver")

    endpoint = str(values.get("LAE_UPLOAD_S3_ENDPOINT") or "").strip()
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.hostname is None:
        raise ValueError("object source broker endpoint is invalid")
    allowed_host = parsed.hostname.lower()
    objects = S3SigV4UploadStore(
        S3UploadConfig(
            endpoint=endpoint,
            bucket=str(values.get("LAE_UPLOAD_S3_BUCKET") or ""),
            region=str(values.get("LAE_UPLOAD_S3_REGION") or "us-east-1"),
            access_key=str(values.get("LAE_UPLOAD_S3_ACCESS_KEY") or ""),
            secret_key=str(values.get("LAE_UPLOAD_S3_SECRET_KEY") or ""),
            production=environment in {"prod", "production"},
        )
    )
    return ObjectSourceBrokerRuntime(
        broker=ObjectSourceBrokerService(
            PostgresObjectSourceRedemptionBroker(sessions),
            objects,
            allowed_host=allowed_host,
        ),
        token=InternalBrokerToken(raw_token),
    )


def _token_source(values: Mapping[str, str]) -> tuple[str, str]:
    raw_token = str(values.get("LAE_OBJECT_SOURCE_BROKER_TOKEN") or "")
    token_file = str(
        values.get("LAE_OBJECT_SOURCE_BROKER_TOKEN_FILE") or ""
    ).strip()
    if raw_token and token_file:
        raise ValueError("object source broker token source is ambiguous")
    if raw_token or token_file:
        return raw_token, token_file

    # The deployment uses one service-principal secret for both internal
    # redemption endpoints unless an explicit object-only token is configured.
    raw_token = str(values.get("LAE_CREDENTIAL_BROKER_TOKEN") or "")
    token_file = str(values.get("LAE_CREDENTIAL_BROKER_TOKEN_FILE") or "").strip()
    if raw_token and token_file:
        raise ValueError("credential broker token source is ambiguous")
    return raw_token, token_file


def register_object_source_broker_route(
    app: FastAPI,
    runtime_getter: Any,
) -> None:
    """Register the Luma service-principal endpoint outside user auth/CSRF."""

    @app.post(OBJECT_SOURCE_REDEMPTION_PATH)
    async def redeem_object_source(request: Request) -> JSONResponse:
        runtime = runtime_getter()
        if runtime is None:
            return _response(
                503,
                "LAE_OBJECT_SOURCE_BROKER_UNAVAILABLE",
                _GENERIC_MESSAGE,
            )
        if not _authorized(request, runtime.token):
            return _response(
                401,
                "LAE_OBJECT_SOURCE_BROKER_UNAUTHENTICATED",
                _GENERIC_MESSAGE,
            )
        try:
            body = await _bounded_json_body(request)
        except TypeError:
            return _response(
                415,
                "LAE_OBJECT_SOURCE_BROKER_MEDIA_TYPE_INVALID",
                _GENERIC_MESSAGE,
            )
        except OverflowError:
            return _response(
                413,
                "LAE_OBJECT_SOURCE_BROKER_REQUEST_TOO_LARGE",
                _GENERIC_MESSAGE,
            )
        except ValueError:
            return _response(
                400,
                "LAE_OBJECT_SOURCE_BROKER_REQUEST_INVALID",
                _GENERIC_MESSAGE,
            )

        try:
            binding = ObjectSourceRedemptionRequest.from_body(body)
            result = await runtime.broker.redeem(binding)
        except CredentialLeaseRejected:
            return _response(
                409,
                "LAE_OBJECT_SOURCE_LEASE_UNAVAILABLE",
                _GENERIC_MESSAGE,
            )
        except Exception:
            # Never log exception text: it may contain the object key or URL.
            return _response(
                503,
                "LAE_OBJECT_SOURCE_BROKER_UNAVAILABLE",
                _GENERIC_MESSAGE,
            )
        response = JSONResponse(result.public_body())
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response


__all__ = [
    "OBJECT_SOURCE_REDEMPTION_PATH",
    "ObjectSourceBrokerRuntime",
    "ObjectSourceBrokerService",
    "object_source_broker_runtime_from_env",
    "register_object_source_broker_route",
]
