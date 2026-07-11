from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lae_store import (
    CreateSourceConnection,
    IdempotencyKeyReused,
    PostgresSourceConnectionStore,
    Principal,
    ResourceNotFound,
    RevokeSourceConnection,
    RotateSourceConnection,
    SourceConnectionConflict,
    SourceConnectionKeyRing,
    TenantScope,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceConnectionCreateRequest(StrictModel):
    provider: Literal["github", "gitea", "generic"]
    displayName: str = Field(min_length=1, max_length=120)
    baseUrl: str = Field(min_length=1, max_length=2048)
    username: str | None = Field(default=None, min_length=1, max_length=256)
    secret: str = Field(min_length=1, max_length=4096, repr=False)


class SourceConnectionRotateRequest(StrictModel):
    secret: str = Field(min_length=1, max_length=4096, repr=False)
    username: str | None = Field(default=None, min_length=1, max_length=256)


class SourceConnectionApiService:
    def __init__(self, store: PostgresSourceConnectionStore) -> None:
        self._store = store

    @property
    def key_ring(self) -> SourceConnectionKeyRing:
        return self._store.key_ring

    async def create(
        self,
        scope: TenantScope,
        principal: Any,
        payload: SourceConnectionCreateRequest,
        idempotency_key: str,
    ) -> Any:
        return await self._store.create(
            CreateSourceConnection(
                scope=scope,
                principal=_store_principal(principal),
                provider=payload.provider,
                display_name=payload.displayName,
                base_url=payload.baseUrl,
                username=payload.username,
                secret=payload.secret,
                idempotency_key=idempotency_key,
            )
        )

    async def list(self, scope: TenantScope) -> dict[str, Any]:
        records = await self._store.list(scope)
        return {"connections": [record.public_body() for record in records]}

    async def rotate(
        self,
        scope: TenantScope,
        principal: Any,
        connection_id: str,
        payload: SourceConnectionRotateRequest,
        idempotency_key: str,
    ) -> Any:
        return await self._store.rotate(
            RotateSourceConnection(
                scope=scope,
                principal=_store_principal(principal),
                connection_id=connection_id,
                secret=payload.secret,
                username=payload.username,
                username_provided="username" in payload.model_fields_set,
                idempotency_key=idempotency_key,
            )
        )

    async def revoke(
        self,
        scope: TenantScope,
        principal: Any,
        connection_id: str,
        idempotency_key: str,
    ) -> Any:
        return await self._store.revoke(
            RevokeSourceConnection(
                scope=scope,
                principal=_store_principal(principal),
                connection_id=connection_id,
                idempotency_key=idempotency_key,
            )
        )


def source_connection_service_from_env(sessions: Any) -> SourceConnectionApiService:
    raw_version = os.environ.get("LAE_SOURCE_CONNECTION_KEY_VERSION", "")
    raw_aead = os.environ.get("LAE_SOURCE_CONNECTION_AEAD_KEYS", "")
    raw_hmac = os.environ.get("LAE_SOURCE_CONNECTION_HMAC_KEYS", "")
    raw_idempotency = os.environ.get("LAE_SOURCE_CONNECTION_IDEMPOTENCY_HMAC_KEY", "")
    if not all((raw_version, raw_aead, raw_hmac, raw_idempotency)):
        raise ValueError("source connection key configuration is incomplete")
    try:
        current_version = int(raw_version)
        aead_keys = _decode_key_map(raw_aead, exact=True, label="AEAD")
        hmac_keys = _decode_key_map(raw_hmac, exact=False, label="HMAC")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("source connection key configuration is invalid") from exc
    key_ring = SourceConnectionKeyRing(
        current_version=current_version,
        encryption_keys=aead_keys,
        hmac_keys=hmac_keys,
    )
    store = PostgresSourceConnectionStore(
        sessions,
        key_ring,
        idempotency_hash_key=_decode_key(
            raw_idempotency, exact=False, label="idempotency HMAC"
        ),
    )
    return SourceConnectionApiService(store)


def register_source_connection_routes(
    app: FastAPI,
    service_getter: Callable[[], Any],
    api_error: type[Exception],
) -> None:
    def error(status: int, code: str, message: str, **kwargs: Any) -> Exception:
        return api_error(status, code, message, **kwargs)

    async def principal_scope(
        request: Request,
        csrf: str | None,
        *,
        mutation: bool,
    ) -> tuple[Any, TenantScope]:
        principal = await app.state.require_scoped_principal(
            request,
            "sources:write",
            csrf_header=csrf,
            mutation=mutation,
        )
        return principal, TenantScope(principal.tenant_id)

    def require_idempotency(value: str | None) -> str:
        if value is None:
            raise error(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        return value

    def no_store(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    def map_store_error(exc: Exception) -> Exception:
        if isinstance(exc, IdempotencyKeyReused):
            return error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            )
        if isinstance(exc, ResourceNotFound):
            return error(404, "LAE_NOT_FOUND", "Source connection not found")
        if isinstance(exc, SourceConnectionConflict):
            return error(
                409,
                "LAE_SOURCE_CONNECTION_CONFLICT",
                "Source connection request conflicts with current state",
            )
        return error(
            400,
            "LAE_INVALID_SOURCE_CONNECTION",
            "Source connection request is invalid",
        )

    @app.post("/v1/source-connections", status_code=201)
    async def create_source_connection(
        request: Request,
        payload: SourceConnectionCreateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        principal, scope = await principal_scope(request, x_csrf_token, mutation=True)
        try:
            result = await service_getter().create(
                scope,
                principal,
                payload,
                require_idempotency(idempotency_key),
            )
        except (
            IdempotencyKeyReused,
            ResourceNotFound,
            SourceConnectionConflict,
            ValueError,
        ) as exc:
            raise map_store_error(exc) from exc
        response = JSONResponse(result.response_body, status_code=201)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)  # type: ignore[return-value]

    @app.get("/v1/source-connections")
    async def list_source_connections(request: Request) -> JSONResponse:
        _, scope = await principal_scope(request, None, mutation=False)
        try:
            body = await service_getter().list(scope)
        except ValueError as exc:
            raise map_store_error(exc) from exc
        return no_store(JSONResponse(body))  # type: ignore[return-value]

    @app.post("/v1/source-connections/{connection_id}/rotate")
    async def rotate_source_connection(
        connection_id: str,
        request: Request,
        payload: SourceConnectionRotateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        principal, scope = await principal_scope(request, x_csrf_token, mutation=True)
        try:
            result = await service_getter().rotate(
                scope,
                principal,
                connection_id,
                payload,
                require_idempotency(idempotency_key),
            )
        except (
            IdempotencyKeyReused,
            ResourceNotFound,
            SourceConnectionConflict,
            ValueError,
        ) as exc:
            raise map_store_error(exc) from exc
        response = JSONResponse(result.response_body)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)  # type: ignore[return-value]

    @app.delete("/v1/source-connections/{connection_id}", status_code=204)
    async def revoke_source_connection(
        connection_id: str,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> Response:
        principal, scope = await principal_scope(request, x_csrf_token, mutation=True)
        try:
            result = await service_getter().revoke(
                scope,
                principal,
                connection_id,
                require_idempotency(idempotency_key),
            )
        except (
            IdempotencyKeyReused,
            ResourceNotFound,
            SourceConnectionConflict,
            ValueError,
        ) as exc:
            raise map_store_error(exc) from exc
        response = Response(status_code=204)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)


def _store_principal(principal: Any) -> Principal:
    return Principal(
        "deploy-token" if principal.credential_type == "deploy_token" else "session",
        principal.credential_id,
    )


def _decode_key_map(value: str, *, exact: bool, label: str) -> dict[int, bytes]:
    decoded = json.loads(value)
    if not isinstance(decoded, Mapping):
        raise ValueError(f"source connection {label} key ring must be an object")
    keys = {
        int(version): _decode_key(encoded, exact=exact, label=label)
        for version, encoded in decoded.items()
        if isinstance(encoded, str)
    }
    if len(keys) != len(decoded):
        raise ValueError(f"source connection {label} key ring is invalid")
    return keys


def _decode_key(value: str, *, exact: bool, label: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"source connection {label} key is not valid base64") from exc
    if (exact and len(key) != 32) or (not exact and len(key) < 32):
        qualifier = "exactly" if exact else "at least"
        raise ValueError(
            f"source connection {label} key must contain {qualifier} 256 bits"
        )
    return key
