from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from lae_store import Principal, TenantScope
from lae_store.application_lifecycle import (
    APPLICATION_ACTIONS,
    ApplicationActionResult,
    PostgresApplicationLifecycleStore,
    RequestApplicationAction,
    UpdateCheckBinding,
)
from lae_store.errors import (
    ApplicationLifecycleConflict,
    ApplicationLifecycleSourceUnavailable,
    ApplicationLifecycleStateConflict,
    ApplicationRollbackUnavailable,
    IdempotencyKeyReused,
    OperationConflict,
    ResourceNotFound,
    SourceConnectionUnavailable,
)


_MAX_ACTION_BODY_BYTES = 4096


def _store_principal(principal: Any) -> Principal:
    return Principal(
        "deploy-token" if principal.credential_type == "deploy_token" else "session",
        principal.credential_id,
    )


class ApplicationLifecycleApiService:
    def __init__(self, store: PostgresApplicationLifecycleStore) -> None:
        self._store = store

    async def request(
        self,
        scope: TenantScope,
        principal: Any,
        application_id: str,
        action: str,
        *,
        rollback_deployment_id: str | None,
        idempotency_key: str,
    ) -> ApplicationActionResult:
        command = RequestApplicationAction(
            scope=scope,
            application_id=application_id,
            action=action,
            rollback_deployment_id=rollback_deployment_id,
        )
        return await self._store.request(
            command,
            principal=_store_principal(principal),
            idempotency=self._store.idempotency(command, key=idempotency_key),
        )


def _decode_hmac_key(value: str, *, field: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{field} is not valid base64") from exc
    if len(key) < 32:
        raise ValueError(f"{field} must contain at least 256 bits")
    return key


def application_lifecycle_service_from_env(
    sessions: Any,
    *,
    connection_key_ring: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApplicationLifecycleApiService:
    values = os.environ if environ is None else environ
    idempotency_key = values.get("LAE_APPLICATION_IDEMPOTENCY_HMAC_KEY", "")
    if not idempotency_key:
        raise ValueError("application lifecycle idempotency key is not configured")

    update_check: UpdateCheckBinding | None = None
    builder_values = (
        values.get("LAE_LUMA_CLUSTER_ID", ""),
        values.get("LAE_LUMA_SERVICE_PRINCIPAL_ID", ""),
        values.get("LAE_WORKER_STATE_HMAC_KEY", ""),
    )
    if all(builder_values):
        try:
            key_version = int(values.get("LAE_WORKER_STATE_HMAC_KEY_VERSION", "1"))
            lease_seconds = int(values.get("LAE_SOURCE_LEASE_TTL_SECONDS", "900"))
        except ValueError as exc:
            raise ValueError("update-check builder binding is invalid") from exc
        update_check = UpdateCheckBinding(
            luma_cluster_id=builder_values[0],
            luma_principal_id=builder_values[1],
            hash_key=_decode_hmac_key(
                builder_values[2], field="update-check HMAC key"
            ),
            hash_key_version=key_version,
            credential_lease_ttl=timedelta(seconds=lease_seconds),
            connection_key_ring=connection_key_ring,
        )
    elif any(builder_values):
        # A partial binding is almost certainly an operator mistake. Fail the
        # lifecycle capability closed rather than silently disabling only one
        # action in a way that readiness cannot explain.
        raise ValueError("update-check builder binding is incomplete")

    return ApplicationLifecycleApiService(
        PostgresApplicationLifecycleStore(
            sessions,
            idempotency_hash_key=_decode_hmac_key(
                idempotency_key, field="application lifecycle idempotency key"
            ),
            update_check=update_check,
        )
    )


async def _strict_action_body(
    request: Request, *, action: str
) -> str | None:
    raw = await request.body()
    if len(raw) > _MAX_ACTION_BODY_BYTES:
        raise ValueError("application action body is too large")
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("application action body must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("application action body must be an object")
    allowed = {"deploymentId"} if action == "rollback" else set()
    if set(payload) - allowed:
        raise ValueError("application action body has unsupported fields")
    if action != "rollback":
        return None
    deployment_id = payload.get("deploymentId")
    if deployment_id is None:
        return None
    if not isinstance(deployment_id, str):
        raise ValueError("rollback deployment ID is invalid")
    return deployment_id


def register_application_lifecycle_routes(
    app: FastAPI,
    service_getter: Callable[[], Any],
    api_error: type[Exception],
) -> None:
    """Register lifecycle routes without taking ownership of app startup."""

    def error(
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> Exception:
        return api_error(
            status,
            code,
            message,
            retryable=retryable,
            details=details,
        )

    @app.post("/v1/applications/{application_id}/actions/{action}", status_code=202)
    async def application_action(
        application_id: str,
        action: str,
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        if action not in APPLICATION_ACTIONS:
            raise error(404, "LAE_NOT_FOUND", "Application action was not found")
        scope = "deployments:write" if action == "rollback" else "apps:write"
        principal = await app.state.require_scoped_principal(
            request,
            scope,
            csrf_header=x_csrf_token,
            mutation=True,
        )
        if idempotency_key is None:
            raise error(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        try:
            rollback_deployment_id = await _strict_action_body(
                request, action=action
            )
            result = await service_getter().request(
                TenantScope(principal.tenant_id),
                principal,
                application_id,
                action,
                rollback_deployment_id=rollback_deployment_id,
                idempotency_key=idempotency_key,
            )
        except IdempotencyKeyReused as exc:
            raise error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            ) from exc
        except ApplicationLifecycleStateConflict as exc:
            raise error(
                409,
                "LAE_APPLICATION_STATE_CONFLICT",
                "The application is not in a valid state for this action",
            ) from exc
        except ApplicationRollbackUnavailable as exc:
            raise error(
                409,
                "LAE_ROLLBACK_UNAVAILABLE",
                "A verified previous deployment is not available",
            ) from exc
        except ApplicationLifecycleSourceUnavailable as exc:
            raise error(
                409,
                "LAE_UPDATE_SOURCE_UNAVAILABLE",
                "The application does not have a reusable Git source",
            ) from exc
        except SourceConnectionUnavailable as exc:
            raise error(
                503,
                "LAE_SOURCE_CONNECTIONS_UNAVAILABLE",
                "Private source connections are temporarily unavailable",
                retryable=True,
            ) from exc
        except (ApplicationLifecycleConflict, OperationConflict) as exc:
            raise error(
                409,
                "LAE_APPLICATION_MUTATION_CONFLICT",
                "Another application mutation is already in progress",
            ) from exc
        except ResourceNotFound as exc:
            raise error(404, "LAE_NOT_FOUND", "Application was not found") from exc
        except ValueError as exc:
            raise error(
                400,
                "LAE_INVALID_ARGUMENT",
                "Application action request is invalid",
            ) from exc

        response = JSONResponse(result.body, status_code=202)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response


__all__ = [
    "ApplicationLifecycleApiService",
    "application_lifecycle_service_from_env",
    "register_application_lifecycle_routes",
]
