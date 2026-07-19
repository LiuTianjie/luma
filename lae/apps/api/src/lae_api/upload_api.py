from __future__ import annotations

import base64
import binascii
import os
import tempfile
import urllib.parse
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lae_store import (
    CompleteStaticUpload,
    CreateStaticUpload,
    CreateUploadAnalysis,
    FakeUploadObjectStore,
    IdempotencyKeyReused,
    InvalidPlanLimits,
    PostgresUploadStore,
    PostgresUploadAnalysisStore,
    Principal,
    ResourceNotFound,
    S3CompatibleUploadStore,
    S3SigV4UploadStore,
    S3UploadConfig,
    SubscriptionUnavailable,
    TenantScope,
    UnconfiguredUploadStore,
    UploadConflict,
    UploadQuotaExceeded,
    UploadRecord,
    UploadUnavailable,
    UploadVerificationFailed,
    sha256_digest,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadCreateRequest(StrictModel):
    applicationId: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    mediaType: str = Field(min_length=1, max_length=128)
    sizeBytes: int = Field(gt=0, le=536_870_912)
    sha256: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")


class UploadCompleteRequest(StrictModel):
    pass


class UploadAnalysisSourceRequest(StrictModel):
    type: Literal["upload"]
    uploadId: str = Field(min_length=1, max_length=64)


def _store_principal(principal: Any) -> Principal:
    return Principal(
        "deploy-token" if principal.credential_type == "deploy_token" else "session",
        principal.credential_id,
    )


class UploadApiService:
    def __init__(
        self,
        store: PostgresUploadStore,
        objects: S3CompatibleUploadStore,
        *,
        put_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if not timedelta(seconds=30) <= put_ttl <= timedelta(minutes=15):
            raise ValueError("upload PUT TTL is invalid")
        self._store = store
        self._objects = objects
        self._put_ttl = put_ttl

    async def create(
        self,
        scope: TenantScope,
        principal: Any,
        payload: UploadCreateRequest,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        self._objects.ensure_available()
        result = await self._store.create(
            CreateStaticUpload(
                scope=scope,
                principal=_store_principal(principal),
                application_id=payload.applicationId,
                filename=payload.filename,
                media_type=payload.mediaType,
                expected_bytes=payload.sizeBytes,
                expected_sha256=payload.sha256,
                idempotency_key=idempotency_key,
            )
        )
        body: dict[str, Any] = {
            "upload": result.upload.public_body(),
            "operation": {
                "id": result.upload.operation_id,
                "status": result.upload.operation_status,
            },
            "uploadUrlIssued": not result.replayed,
        }
        if not result.replayed:
            grant = await self._objects.issue_single_use_put(
                object_key=result.upload.object_key,
                size_bytes=result.upload.expected_bytes,
                media_type=result.upload.media_type,
                expires_in=self._put_ttl,
            )
            body["transfer"] = {
                "method": "PUT",
                "url": grant.url,
                "headers": dict(grant.headers),
                "expiresAt": grant.expires_at.isoformat().replace("+00:00", "Z"),
            }
        return body, result.replayed

    async def get(self, scope: TenantScope, upload_id: str) -> dict[str, Any]:
        upload = await self._store.get(scope, upload_id)
        return _response_body(upload)

    async def complete(
        self,
        scope: TenantScope,
        principal: Any,
        upload_id: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        self._objects.ensure_available()
        claim = await self._store.claim_completion(
            CompleteStaticUpload(
                scope=scope,
                principal=_store_principal(principal),
                upload_id=upload_id,
                idempotency_key=idempotency_key,
            )
        )
        if not claim.owns_verification:
            return _response_body(claim.upload), claim.replayed
        try:
            head = await self._objects.head(claim.upload.object_key)
            if (
                head.size_bytes != claim.upload.expected_bytes
                or head.media_type != claim.upload.media_type
            ):
                raise UploadVerificationFailed("object metadata does not match reservation")
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as stream:
                downloaded = await self._objects.copy_to(
                    claim.upload.object_key,
                    stream,
                    max_bytes=claim.upload.expected_bytes,
                )
                digest, size = sha256_digest(
                    stream, max_bytes=claim.upload.expected_bytes
                )
            if (
                downloaded != head
                or size != claim.upload.expected_bytes
                or digest != claim.upload.expected_sha256
            ):
                raise UploadVerificationFailed("object content does not match reservation")
            upload = await self._store.mark_scanning(
                scope,
                upload_id,
                actual_bytes=size,
                actual_sha256=digest,
            )
            return _response_body(upload), claim.replayed
        except UploadVerificationFailed:
            await self._store.mark_failed(
                scope,
                upload_id,
                failure_code="LAE_UPLOAD_VERIFICATION_FAILED",
            )
            raise

    async def delete(self, scope: TenantScope, upload_id: str) -> dict[str, Any]:
        self._objects.ensure_available()
        upload = await self._store.delete(scope, upload_id)
        if upload.status != "deleted":
            await self._objects.delete(upload.object_key)
            upload = await self._store.finish_delete(scope, upload_id)
        return _response_body(upload)

    async def expire_and_cleanup(self, *, limit: int = 100) -> int:
        self._objects.ensure_available()
        expired = await self._store.expire_stale(limit=limit)
        cleaned = 0
        for claim in expired:
            try:
                await self._objects.delete(claim.upload.object_key)
                await self._store.finish_delete(
                    TenantScope(claim.tenant_id), claim.upload.id
                )
            except UploadVerificationFailed:
                continue
            cleaned += 1
        return cleaned


def _response_body(upload: UploadRecord) -> dict[str, Any]:
    return {
        "upload": upload.public_body(),
        "operation": {"id": upload.operation_id, "status": upload.operation_status},
    }


def upload_service_from_env(
    sessions: Any,
    *,
    environment: str,
) -> UploadApiService:
    raw_key = os.environ.get("LAE_UPLOAD_HMAC_KEY", "")
    if not raw_key:
        raise ValueError("upload HMAC key is not configured")
    try:
        hash_key = base64.b64decode(raw_key, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload HMAC key is invalid") from exc
    if len(hash_key) < 32:
        raise ValueError("upload HMAC key must contain at least 256 bits")
    driver = os.environ.get("LAE_UPLOAD_DRIVER", "disabled").strip().lower()
    objects: S3CompatibleUploadStore
    if driver == "s3":
        objects = S3SigV4UploadStore(
            S3UploadConfig(
                endpoint=os.environ.get("LAE_UPLOAD_S3_ENDPOINT", ""),
                bucket=os.environ.get("LAE_UPLOAD_S3_BUCKET", ""),
                region=os.environ.get("LAE_UPLOAD_S3_REGION", "us-east-1"),
                access_key=os.environ.get("LAE_UPLOAD_S3_ACCESS_KEY", ""),
                secret_key=os.environ.get("LAE_UPLOAD_S3_SECRET_KEY", ""),
                production=environment in {"prod", "production"},
            )
        )
    elif driver == "disabled":
        objects = UnconfiguredUploadStore()
    elif driver == "fake" and environment not in {"prod", "production"}:
        objects = FakeUploadObjectStore()
    else:
        raise ValueError("upload object-store driver is unsupported")
    ttl = int(os.environ.get("LAE_UPLOAD_PUT_TTL_SECONDS", "300"))
    reservation_ttl = int(os.environ.get("LAE_UPLOAD_RESERVATION_TTL_SECONDS", "3600"))
    return UploadApiService(
        PostgresUploadStore(
            sessions,
            hash_key=hash_key,
            reservation_ttl=timedelta(seconds=reservation_ttl),
        ),
        objects,
        put_ttl=timedelta(seconds=ttl),
    )


def upload_analysis_store_from_env(sessions: Any) -> PostgresUploadAnalysisStore:
    """Build upload analysis admission only when the redemption broker is live.

    Setting up S3 writes is not enough: Builder must also have a task-bound,
    single-use download lease path.  The explicit capability flag prevents a
    ready upload from being enqueued into a worker that can only fetch Git.
    """

    if os.environ.get("LAE_UPLOAD_ANALYSIS_BROKER_ENABLED") != "1":
        raise ValueError("upload analysis broker is not enabled")
    raw_key = os.environ.get("LAE_WORKER_STATE_HMAC_KEY", "")
    try:
        hash_key = base64.b64decode(raw_key, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload analysis HMAC key is invalid") from exc
    if len(hash_key) < 32:
        raise ValueError("upload analysis HMAC key must contain at least 256 bits")
    endpoint = urllib.parse.urlsplit(os.environ.get("LAE_UPLOAD_S3_ENDPOINT", ""))
    if endpoint.hostname is None or endpoint.username is not None or endpoint.password is not None:
        raise ValueError("upload analysis object-store endpoint is invalid")
    host = endpoint.hostname.lower()
    if endpoint.port is not None:
        host = f"{host}:{endpoint.port}"
    try:
        key_version = int(os.environ.get("LAE_WORKER_STATE_HMAC_KEY_VERSION", "1"))
    except ValueError as exc:
        raise ValueError("upload analysis key version is invalid") from exc
    return PostgresUploadAnalysisStore(
        sessions,
        luma_cluster_id=os.environ.get("LAE_LUMA_CLUSTER_ID", ""),
        luma_principal_id=os.environ.get("LAE_LUMA_SERVICE_PRINCIPAL_ID", ""),
        object_store_host=host,
        hash_key=hash_key,
        hash_key_version=key_version,
    )


async def create_upload_analysis(
    store: PostgresUploadAnalysisStore,
    *,
    principal: Any,
    application_id: str,
    source: UploadAnalysisSourceRequest,
    region: str,
    public_protocols: tuple[str, ...],
    idempotency_key: str,
) -> Any:
    return await store.create(
        CreateUploadAnalysis(
            scope=TenantScope(principal.tenant_id),
            principal=_store_principal(principal),
            application_id=application_id,
            upload_id=source.uploadId,
            region=region,
            public_protocols=public_protocols,
            idempotency_key=idempotency_key,
        )
    )


def register_upload_routes(
    app: FastAPI,
    service_getter: Callable[[], UploadApiService],
    api_error: type[Exception],
) -> None:
    def error(status: int, code: str, message: str, **kwargs: Any) -> Exception:
        return api_error(status, code, message, **kwargs)

    def no_store(response: JSONResponse) -> JSONResponse:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    async def principal(
        request: Request, csrf: str | None, *, mutation: bool
    ) -> tuple[Any, TenantScope]:
        selected = await app.state.require_scoped_principal(
            request,
            "sources:write",
            csrf_header=csrf,
            mutation=mutation,
        )
        return selected, TenantScope(selected.tenant_id)

    def translate(exc: Exception) -> Exception:
        if isinstance(exc, ResourceNotFound):
            return error(404, "LAE_NOT_FOUND", "Upload not found")
        if isinstance(exc, IdempotencyKeyReused):
            return error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            )
        if isinstance(exc, UploadQuotaExceeded):
            return error(409, "LAE_UPLOAD_QUOTA_EXCEEDED", "Upload quota has been reached")
        if isinstance(exc, SubscriptionUnavailable):
            return error(
                409,
                "LAE_SUBSCRIPTION_UNAVAILABLE",
                "An active subscription is required",
            )
        if isinstance(exc, (UploadUnavailable, InvalidPlanLimits)):
            return error(
                503,
                "LAE_UPLOAD_UNAVAILABLE",
                "Static upload service is temporarily unavailable",
                retryable=True,
            )
        if isinstance(exc, UploadVerificationFailed):
            return error(
                422,
                "LAE_UPLOAD_VERIFICATION_FAILED",
                "Uploaded content did not match its reservation",
            )
        if isinstance(exc, UploadConflict):
            return error(409, "LAE_UPLOAD_CONFLICT", "Upload conflicts with current state")
        return error(400, "LAE_INVALID_ARGUMENT", "Upload request is invalid")

    @app.post("/v1/uploads", status_code=201)
    async def create_upload(
        request: Request,
        payload: UploadCreateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        selected, scope = await principal(request, x_csrf_token, mutation=True)
        if idempotency_key is None:
            raise error(400, "LAE_IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        try:
            body, replayed = await service_getter().create(
                scope, selected, payload, idempotency_key
            )
        except (
            IdempotencyKeyReused,
            InvalidPlanLimits,
            ResourceNotFound,
            SubscriptionUnavailable,
            UploadConflict,
            UploadQuotaExceeded,
            UploadUnavailable,
            ValueError,
        ) as exc:
            raise translate(exc) from exc
        response = no_store(JSONResponse(body, status_code=201))
        response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
        return response

    @app.post("/v1/uploads/{upload_id}/complete", status_code=202)
    async def complete_upload(
        upload_id: str,
        request: Request,
        _payload: UploadCompleteRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        selected, scope = await principal(request, x_csrf_token, mutation=True)
        if idempotency_key is None:
            raise error(400, "LAE_IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        try:
            body, replayed = await service_getter().complete(
                scope, selected, upload_id, idempotency_key
            )
        except (
            IdempotencyKeyReused,
            ResourceNotFound,
            UploadConflict,
            UploadUnavailable,
            UploadVerificationFailed,
            ValueError,
        ) as exc:
            raise translate(exc) from exc
        response = no_store(JSONResponse(body, status_code=202))
        response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
        return response

    @app.get("/v1/uploads/{upload_id}")
    async def get_upload(upload_id: str, request: Request) -> JSONResponse:
        _selected, scope = await principal(request, None, mutation=False)
        try:
            body = await service_getter().get(scope, upload_id)
        except (ResourceNotFound, ValueError) as exc:
            raise translate(exc) from exc
        return no_store(JSONResponse(body))

    @app.delete("/v1/uploads/{upload_id}", status_code=202)
    async def delete_upload(
        upload_id: str,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        _selected, scope = await principal(request, x_csrf_token, mutation=True)
        if idempotency_key is None:
            raise error(400, "LAE_IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        try:
            body = await service_getter().delete(scope, upload_id)
        except (
            ResourceNotFound,
            UploadConflict,
            UploadUnavailable,
            UploadVerificationFailed,
            ValueError,
        ) as exc:
            raise translate(exc) from exc
        return no_store(JSONResponse(body, status_code=202))
