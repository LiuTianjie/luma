from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .plan_resolver import DeploymentConfigurationSchema

from lae_store import (
    ApplicationConflict,
    DeploymentEnvironmentSchemaConflict,
    DeploymentEnvironmentScopeInvalid,
    IdempotencyInput,
    IdempotentCatalogResult,
    Principal,
    TenantScope,
    keyed_request_hash,
)
from lae_store.deployment_admission import (
    DEPLOYMENT_CREATE_ROUTE,
    CreateDeploymentAdmission,
    DeploymentAdmissionResult,
    DeploymentAdmissionStore,
    PlanResolver,
    PreparedDeploymentPlan,
    UnconfiguredPlanResolver,
)
from lae_store.errors import (
    DeploymentConflict,
    DeploymentEnvironmentIncomplete,
    DeploymentPlanInvalid,
    DeploymentPlanUnavailable,
    DeploymentQuotaExceeded,
    DeploymentTopologyConflict,
    EnvironmentVersionConflict,
    IdempotencyKeyReused,
    InvalidPlanLimits,
    OperationConflict,
    ResourceNotFound,
    SubscriptionUnavailable,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeploymentCreateRequest(StrictModel):
    # Deployment topology, images, routes, manifests and volume policies are
    # intentionally absent. They can only come from the stored trusted plan.
    analysisId: str = Field(min_length=1, max_length=64)
    environmentVersion: int = Field(ge=0)


_MAX_ENV_VALUE_BYTES = 64 * 1024
_MAX_ENV_PATCH_BYTES = 512 * 1024
_MAX_ENV_CHANGES = 128


class PlanEnvironmentValueRequest(StrictModel):
    value: str = Field(max_length=_MAX_ENV_VALUE_BYTES, repr=False)

    @model_validator(mode="after")
    def validate_encoded_size(self) -> PlanEnvironmentValueRequest:
        if len(self.value.encode("utf-8")) > _MAX_ENV_VALUE_BYTES:
            raise ValueError("environment value exceeds 64 KiB")
        return self


class PlanEnvironmentPatchRequest(StrictModel):
    expectedVersion: int = Field(ge=0)
    environmentSchemaDigest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    set: dict[str, PlanEnvironmentValueRequest] = Field(
        default_factory=dict, max_length=_MAX_ENV_CHANGES
    )
    unset: list[str] = Field(default_factory=list, max_length=_MAX_ENV_CHANGES)

    @model_validator(mode="after")
    def validate_patch(self) -> PlanEnvironmentPatchRequest:
        if not self.set and not self.unset:
            raise ValueError("environment patch is empty")
        size = len(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if size > _MAX_ENV_PATCH_BYTES:
            raise ValueError("environment patch exceeds 512 KiB")
        return self


def _store_principal(principal: Any) -> Principal:
    return Principal(
        "deploy-token" if principal.credential_type == "deploy_token" else "session",
        principal.credential_id,
    )


class DeploymentApiService:
    def __init__(
        self,
        store: DeploymentAdmissionStore,
        resolver: PlanResolver,
        *,
        idempotency_hash_key: bytes,
        environment_writer: Any | None = None,
    ) -> None:
        if (
            not isinstance(idempotency_hash_key, bytes)
            or len(idempotency_hash_key) < 32
        ):
            raise ValueError(
                "deployment idempotency HMAC key must be at least 256 bits"
            )
        self._store = store
        self._resolver = resolver
        self._idempotency_hash_key = idempotency_hash_key
        self._environment_writer = environment_writer

    async def configuration(
        self,
        scope: TenantScope,
        application_id: str,
        analysis_id: str,
    ) -> dict[str, object]:
        artifact = await self._store.get_plan_artifact(
            scope, application_id, analysis_id
        )
        resolve = getattr(self._resolver, "resolve_configuration", None)
        if not callable(resolve):
            raise DeploymentPlanUnavailable(
                "deployment configuration resolver is not configured"
            )
        configuration = await resolve(artifact)
        if not isinstance(configuration, DeploymentConfigurationSchema):
            raise DeploymentPlanInvalid(
                "trusted configuration resolver returned invalid data"
            )
        return {"configuration": configuration.public_body()}

    async def patch_environment(
        self,
        scope: TenantScope,
        principal: Any,
        application_id: str,
        analysis_id: str,
        payload: PlanEnvironmentPatchRequest,
        idempotency_key: str,
    ) -> IdempotentCatalogResult:
        artifact = await self._store.get_plan_artifact(
            scope, application_id, analysis_id
        )
        resolve = getattr(self._resolver, "resolve_configuration", None)
        if not callable(resolve):
            raise DeploymentPlanUnavailable(
                "deployment configuration resolver is not configured"
            )
        configuration = await resolve(artifact)
        if not isinstance(configuration, DeploymentConfigurationSchema):
            raise DeploymentPlanInvalid(
                "trusted configuration resolver returned invalid data"
            )
        if payload.environmentSchemaDigest != configuration.environment_schema_digest:
            raise DeploymentEnvironmentSchemaConflict(
                "deployment environment schema changed"
            )
        write = getattr(self._environment_writer, "patch_plan_environment", None)
        if not callable(write):
            raise DeploymentPlanUnavailable(
                "plan-bound environment writer is not configured"
            )
        return await write(
            scope,
            principal,
            application_id,
            analysis_id=analysis_id,
            expected_version=payload.expectedVersion,
            environment_schema_digest=configuration.environment_schema_digest,
            plan_service_keys=configuration.service_keys,
            schema_environment=configuration.environment,
            values={reference: item.value for reference, item in payload.set.items()},
            unset=tuple(payload.unset),
            idempotency_key=idempotency_key,
        )

    async def create(
        self,
        scope: TenantScope,
        principal: Any,
        application_id: str,
        payload: DeploymentCreateRequest,
        idempotency_key: str,
    ) -> DeploymentAdmissionResult:
        store_principal = _store_principal(principal)
        command = CreateDeploymentAdmission(
            scope=scope,
            application_id=application_id,
            analysis_id=payload.analysisId,
            environment_version=payload.environmentVersion,
        )
        idempotency = IdempotencyInput(
            key=idempotency_key,
            method="POST",
            route_template=DEPLOYMENT_CREATE_ROUTE,
            request_hash=keyed_request_hash(
                {
                    "applicationId": application_id,
                    "analysisId": payload.analysisId,
                    "environmentVersion": payload.environmentVersion,
                },
                self._idempotency_hash_key,
            ),
        )
        replay = await self._store.lookup_replay(scope, store_principal, idempotency)
        if replay is not None:
            return replay
        artifact = await self._store.get_plan_artifact(
            scope, application_id, payload.analysisId
        )
        plan = await self._resolver.resolve(artifact)
        if not isinstance(plan, PreparedDeploymentPlan):
            raise DeploymentPlanInvalid("trusted plan resolver returned invalid data")
        return await self._store.admit(
            command,
            principal=store_principal,
            idempotency=idempotency,
            artifact=artifact,
            plan=plan,
        )

    async def list(
        self, scope: TenantScope, application_id: str, *, limit: int
    ) -> dict[str, object]:
        records = await self._store.list_deployments(scope, application_id, limit=limit)
        return {"deployments": [record.public_body() for record in records]}

    async def get(
        self, scope: TenantScope, application_id: str, deployment_id: str
    ) -> dict[str, object]:
        record = await self._store.get_deployment(scope, application_id, deployment_id)
        return {"deployment": record.public_body()}


def _decode_hmac_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("deployment idempotency HMAC key is not valid base64") from exc
    if len(key) < 32:
        raise ValueError("deployment idempotency HMAC key must contain 256 bits")
    return key


def deployment_service_from_env(
    sessions: Any,
    *,
    resolver: PlanResolver | None = None,
    environment_writer: Any | None = None,
) -> DeploymentApiService:
    """Build the runtime service with a fail-closed artifact adapter by default."""

    encoded_key = os.environ.get("LAE_DEPLOYMENT_IDEMPOTENCY_HMAC_KEY", "")
    if not encoded_key:
        raise ValueError("deployment idempotency HMAC key is not configured")
    return DeploymentApiService(
        DeploymentAdmissionStore(sessions),
        resolver or UnconfiguredPlanResolver(),
        idempotency_hash_key=_decode_hmac_key(encoded_key),
        environment_writer=environment_writer,
    )


def register_deployment_routes(
    app: FastAPI,
    service_getter: Callable[[], Any],
    api_error: type[Exception],
) -> None:
    """Register routes without owning application startup or auth wiring."""

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

    def no_store(response: JSONResponse) -> JSONResponse:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    async def read_scope(request: Request) -> TenantScope:
        principal = await app.state.require_scoped_principal(
            request, "deployments:write", mutation=False
        )
        return TenantScope(principal.tenant_id)

    @app.post("/v1/applications/{application_id}/deployments", status_code=202)
    async def create_deployment(
        application_id: str,
        request: Request,
        payload: DeploymentCreateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        principal = await app.state.require_scoped_principal(
            request,
            "deployments:write",
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
            result = await service_getter().create(
                TenantScope(principal.tenant_id),
                principal,
                application_id,
                payload,
                idempotency_key,
            )
        except IdempotencyKeyReused as exc:
            raise error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            ) from exc
        except EnvironmentVersionConflict as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_VERSION_CONFLICT",
                "Application environment changed before deployment",
                details={"expectedVersion": exc.expected, "actualVersion": exc.actual},
            ) from exc
        except DeploymentEnvironmentIncomplete as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_INCOMPLETE",
                "Required application environment is not configured",
            ) from exc
        except DeploymentEnvironmentScopeInvalid as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_SCOPE_INVALID",
                "Application environment scopes are incompatible with the deployment plan",
            ) from exc
        except DeploymentTopologyConflict as exc:
            raise error(
                409,
                "LAE_DEPLOYMENT_TOPOLOGY_CONFLICT",
                "The analyzed topology is incompatible with this application",
            ) from exc
        except DeploymentQuotaExceeded as exc:
            raise error(
                409,
                "LAE_DEPLOYMENT_QUOTA_EXCEEDED",
                "The deployment exceeds the active plan limits",
            ) from exc
        except SubscriptionUnavailable as exc:
            raise error(
                409,
                "LAE_SUBSCRIPTION_UNAVAILABLE",
                "An active subscription is required",
            ) from exc
        except InvalidPlanLimits as exc:
            raise error(
                503,
                "LAE_ENTITLEMENT_UNAVAILABLE",
                "Deployment entitlement is temporarily unavailable",
                retryable=True,
            ) from exc
        except DeploymentPlanUnavailable as exc:
            raise error(
                503,
                "LAE_DEPLOYMENT_PLAN_UNAVAILABLE",
                "The stored deployment plan is temporarily unavailable",
                retryable=True,
            ) from exc
        except DeploymentPlanInvalid as exc:
            raise error(
                409,
                "LAE_DEPLOYMENT_PLAN_INVALID",
                "The stored deployment plan cannot be admitted",
            ) from exc
        except (DeploymentConflict, OperationConflict) as exc:
            raise error(
                409,
                "LAE_DEPLOYMENT_CONFLICT",
                "A deployment mutation is already in progress",
            ) from exc
        except ResourceNotFound as exc:
            raise error(404, "LAE_NOT_FOUND", "Deployment input was not found") from exc
        except ValueError as exc:
            raise error(
                400, "LAE_INVALID_ARGUMENT", "Deployment request is invalid"
            ) from exc
        response = JSONResponse(result.public_body(), status_code=202)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)

    @app.get("/v1/applications/{application_id}/deployments")
    async def list_deployments(
        application_id: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        try:
            body = await service_getter().list(
                await read_scope(request), application_id, limit=limit
            )
        except (ResourceNotFound, ValueError) as exc:
            raise error(404, "LAE_NOT_FOUND", "Application not found") from exc
        return no_store(JSONResponse(body))

    @app.get(
        "/v1/applications/{application_id}/analyses/{analysis_id}/configuration"
    )
    async def get_deployment_configuration(
        application_id: str,
        analysis_id: str,
        request: Request,
    ) -> JSONResponse:
        try:
            body = await service_getter().configuration(
                await read_scope(request), application_id, analysis_id
            )
        except DeploymentPlanUnavailable as exc:
            raise error(
                503,
                "LAE_DEPLOYMENT_PLAN_UNAVAILABLE",
                "The stored deployment configuration is temporarily unavailable",
                retryable=True,
            ) from exc
        except DeploymentPlanInvalid as exc:
            raise error(
                409,
                "LAE_DEPLOYMENT_PLAN_INVALID",
                "The stored deployment configuration is invalid",
            ) from exc
        except (ResourceNotFound, ValueError) as exc:
            raise error(404, "LAE_NOT_FOUND", "Deployment input was not found") from exc
        return no_store(JSONResponse(body))

    @app.patch(
        "/v1/applications/{application_id}/analyses/{analysis_id}/environment"
    )
    async def patch_plan_environment(
        application_id: str,
        analysis_id: str,
        request: Request,
        payload: PlanEnvironmentPatchRequest,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        principal = await app.state.require_scoped_principal(
            request,
            "apps:write",
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
            result = await service_getter().patch_environment(
                TenantScope(principal.tenant_id),
                principal,
                application_id,
                analysis_id,
                payload,
                idempotency_key,
            )
        except IdempotencyKeyReused as exc:
            raise error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            ) from exc
        except EnvironmentVersionConflict as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_VERSION_CONFLICT",
                "Application environment changed before configuration was saved",
                details={"expectedVersion": exc.expected, "actualVersion": exc.actual},
            ) from exc
        except DeploymentEnvironmentSchemaConflict as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_SCHEMA_CONFLICT",
                "The analyzed environment schema changed; refresh the deployment plan",
            ) from exc
        except DeploymentEnvironmentScopeInvalid as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_SCOPE_INVALID",
                "An environment variable does not target a service allowed by the plan",
            ) from exc
        except DeploymentPlanUnavailable as exc:
            raise error(
                503,
                "LAE_DEPLOYMENT_PLAN_UNAVAILABLE",
                "The stored deployment configuration is temporarily unavailable",
                retryable=True,
            ) from exc
        except DeploymentPlanInvalid as exc:
            raise error(
                409,
                "LAE_DEPLOYMENT_PLAN_INVALID",
                "The stored deployment configuration is invalid",
            ) from exc
        except ResourceNotFound as exc:
            raise error(404, "LAE_NOT_FOUND", "Deployment input was not found") from exc
        except ApplicationConflict as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_CONFLICT",
                "Environment update conflicts with current state",
            ) from exc
        except ValueError as exc:
            raise error(
                400,
                "LAE_INVALID_ARGUMENT",
                "Environment request is invalid",
            ) from exc
        response = JSONResponse(result.response_body)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)

    @app.get("/v1/applications/{application_id}/deployments/{deployment_id}")
    async def get_deployment(
        application_id: str,
        deployment_id: str,
        request: Request,
    ) -> JSONResponse:
        try:
            body = await service_getter().get(
                await read_scope(request), application_id, deployment_id
            )
        except (ResourceNotFound, ValueError) as exc:
            raise error(404, "LAE_NOT_FOUND", "Deployment not found") from exc
        return no_store(JSONResponse(body))


__all__ = [
    "DeploymentApiService",
    "DeploymentCreateRequest",
    "PlanEnvironmentPatchRequest",
    "PlanEnvironmentValueRequest",
    "deployment_service_from_env",
    "register_deployment_routes",
]
