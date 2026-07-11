from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lae_store import (
    APPLICATION_ENVIRONMENT_PATCH_ROUTE,
    PLAN_ENVIRONMENT_PATCH_ROUTE,
    ApplicationCatalogStore,
    ApplicationConflict,
    ApplicationQuotaExceeded,
    ApplicationRecord,
    ApplicationSummary,
    EnvironmentKey,
    EnvironmentKeyRing,
    EnvironmentMetadata,
    EnvironmentPlaintext,
    EnvironmentVersionConflict,
    IdempotencyInput,
    IdempotencyKeyReused,
    IdempotentCatalogResult,
    PatchEnvironment,
    PreparedEnvironmentVariable,
    Principal,
    ResourceNotFound,
    SubscriptionUnavailable,
    TenantScope,
    CreateApplicationDraft,
    DeploymentEnvironmentScopeInvalid,
    keyed_request_hash,
)

_ENV_REFERENCE = re.compile(
    r"^(?P<scope>\*|[a-z0-9][a-z0-9._-]{0,79}):"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]{0,127})$"
)
_MAX_ENV_VALUE_BYTES = 64 * 1024
_MAX_ENV_PATCH_BYTES = 512 * 1024
_MAX_ENV_CHANGES = 128


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=80)


class EnvironmentSetRequest(StrictModel):
    value: str = Field(max_length=_MAX_ENV_VALUE_BYTES, repr=False)
    sensitive: bool = True
    required: bool = False

    @model_validator(mode="after")
    def validate_encoded_size(self) -> EnvironmentSetRequest:
        if len(self.value.encode("utf-8")) > _MAX_ENV_VALUE_BYTES:
            raise ValueError("environment value exceeds 64 KiB")
        return self


class EnvironmentPatchRequest(StrictModel):
    expectedVersion: int = Field(ge=0)
    set: dict[str, EnvironmentSetRequest] = Field(
        default_factory=dict, max_length=_MAX_ENV_CHANGES
    )
    unset: list[str] = Field(default_factory=list, max_length=_MAX_ENV_CHANGES)

    @model_validator(mode="after")
    def validate_total_size(self) -> EnvironmentPatchRequest:
        if not self.set and not self.unset:
            raise ValueError("environment patch is empty")
        serialized_bytes = len(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if serialized_bytes > _MAX_ENV_PATCH_BYTES:
            raise ValueError("environment patch exceeds 512 KiB")
        return self


def _timestamp(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _application_body(summary: ApplicationSummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "name": summary.name,
        "slug": summary.slug,
        "kind": summary.kind,
        "desiredState": summary.desired_state,
        "observedState": summary.observed_state,
        "currentRevisionId": summary.current_revision_id,
        "currentDeploymentId": summary.current_deployment_id,
        "environmentVersion": summary.environment_version,
        "createdAt": _timestamp(summary.created_at),
        "updatedAt": _timestamp(summary.updated_at),
    }


def _environment_body(metadata: EnvironmentMetadata) -> dict[str, Any]:
    return {
        "version": metadata.version,
        "variables": [
            {
                "serviceScope": item.service_scope,
                "name": item.name,
                "configured": item.configured,
                "sensitive": item.is_sensitive,
                "required": item.required,
                "source": item.source,
                "updatedAt": _timestamp(item.updated_at),
            }
            for item in metadata.variables
        ],
    }


def _record_body(record: ApplicationRecord) -> dict[str, Any]:
    service_keys = {item.id: item.service_key for item in record.services}
    return {
        "application": _application_body(record.application),
        "services": [
            {
                "key": item.service_key,
                "role": item.role,
                "required": item.required,
                "desiredState": item.desired_state,
                "observedState": item.observed_state,
                "currentImageDigest": item.current_image_digest,
            }
            for item in record.services
        ],
        "routes": [
            {
                "serviceKey": service_keys[item.service_id],
                "hostname": item.hostname,
                "primary": item.is_primary,
                "containerPort": item.container_port,
                "status": item.status,
            }
            for item in record.routes
        ],
        "volumes": [
            {
                "key": item.volume_key,
                "requestedBytes": item.requested_bytes,
                "storagePolicy": item.storage_policy,
                "backupPolicy": item.backup_policy,
                "deletePolicy": item.delete_policy,
                "status": item.status,
            }
            for item in record.volumes
        ],
        "environment": _environment_body(record.environment),
    }


def _parse_environment_reference(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("environment reference is invalid")
    match = _ENV_REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError("environment reference is invalid")
    return match.group("scope"), match.group("name")


class ApplicationApiService:
    """Public application orchestration with encrypted environment persistence."""

    def __init__(
        self,
        catalog: ApplicationCatalogStore,
        crypto: EnvironmentKeyRing,
        *,
        idempotency_hash_key: bytes,
    ) -> None:
        if not isinstance(idempotency_hash_key, bytes) or len(idempotency_hash_key) < 32:
            raise ValueError("application idempotency HMAC key must be at least 256 bits")
        self._catalog = catalog
        self._crypto = crypto
        self._idempotency_hash_key = idempotency_hash_key

    async def create(
        self,
        scope: TenantScope,
        principal: Any,
        payload: ApplicationCreateRequest,
        idempotency_key: str,
    ) -> IdempotentCatalogResult:
        request_hash = keyed_request_hash(
            {"name": payload.name, "slug": payload.slug},
            self._idempotency_hash_key,
        )
        return await self._catalog.create_application_draft_idempotent(
            CreateApplicationDraft(scope=scope, name=payload.name, slug=payload.slug),
            principal=_store_principal(principal),
            idempotency=IdempotencyInput(
                key=idempotency_key,
                method="POST",
                route_template="/v1/applications",
                request_hash=request_hash,
            ),
        )

    async def list(self, scope: TenantScope) -> dict[str, Any]:
        applications = await self._catalog.list_applications(scope)
        return {"applications": [_application_body(item) for item in applications]}

    async def get(self, scope: TenantScope, application_id: str) -> dict[str, Any]:
        return _record_body(await self._catalog.get_application(scope, application_id))

    async def services(
        self, scope: TenantScope, application_id: str
    ) -> dict[str, Any]:
        body = await self.get(scope, application_id)
        return {"services": body["services"]}

    async def routes(self, scope: TenantScope, application_id: str) -> dict[str, Any]:
        body = await self.get(scope, application_id)
        return {"routes": body["routes"]}

    async def volumes(self, scope: TenantScope, application_id: str) -> dict[str, Any]:
        body = await self.get(scope, application_id)
        return {"volumes": body["volumes"]}

    async def environment(
        self, scope: TenantScope, application_id: str
    ) -> dict[str, Any]:
        metadata = await self._catalog.get_environment(scope, application_id)
        return {"environment": _environment_body(metadata)}

    async def patch_environment(
        self,
        scope: TenantScope,
        principal: Any,
        application_id: str,
        payload: EnvironmentPatchRequest,
        idempotency_key: str,
    ) -> IdempotentCatalogResult:
        set_values: list[EnvironmentPlaintext] = []
        for reference, item in payload.set.items():
            service_scope, name = _parse_environment_reference(reference)
            set_values.append(
                EnvironmentPlaintext(
                    service_scope=service_scope,
                    name=name,
                    value=item.value,
                    is_sensitive=item.sensitive,
                    required=item.required,
                )
            )
        unset = [
            EnvironmentKey(*_parse_environment_reference(reference))
            for reference in payload.unset
        ]
        hash_payload = {
            "applicationId": application_id,
            "expectedVersion": payload.expectedVersion,
            "set": {
                f"{item.service_scope}:{item.name}": {
                    "value": item.value,
                    "sensitive": item.is_sensitive,
                    "required": item.required,
                }
                for item in set_values
            },
            "unset": sorted(payload.unset),
        }
        request_hash = keyed_request_hash(hash_payload, self._idempotency_hash_key)
        encrypted = tuple(
            self._crypto.encrypt_value(
                item,
                tenant_id=scope.tenant_id,
                application_id=application_id,
            )
            for item in set_values
        )
        return await self._catalog.patch_environment_idempotent(
            PatchEnvironment(
                scope=scope,
                application_id=application_id,
                expected_version=payload.expectedVersion,
                set_values=encrypted,
                unset=tuple(unset),
            ),
            principal=_store_principal(principal),
            idempotency=IdempotencyInput(
                key=idempotency_key,
                method="PATCH",
                route_template=APPLICATION_ENVIRONMENT_PATCH_ROUTE,
                request_hash=request_hash,
            ),
        )

    async def patch_plan_environment(
        self,
        scope: TenantScope,
        principal: Any,
        application_id: str,
        *,
        analysis_id: str,
        expected_version: int,
        environment_schema_digest: str,
        plan_service_keys: tuple[str, ...],
        schema_environment: tuple[PreparedEnvironmentVariable, ...],
        values: Mapping[str, str],
        unset: tuple[str, ...],
        idempotency_key: str,
    ) -> IdempotentCatalogResult:
        """Encrypt a verified-plan-bound, explicitly service-scoped patch."""

        service_keys = set(plan_service_keys)
        if not service_keys or len(service_keys) != len(plan_service_keys):
            raise DeploymentEnvironmentScopeInvalid(
                "deployment environment service namespace is invalid"
            )
        bindings: dict[tuple[str, str], PreparedEnvironmentVariable] = {}
        targets_by_name: dict[str, set[str]] = {}
        for variable in schema_environment:
            targets = targets_by_name.setdefault(variable.name, set())
            for service_key in variable.service_keys:
                bindings[(service_key, variable.name)] = variable
                targets.add(service_key)

        plaintext: list[EnvironmentPlaintext] = []
        explicitly_set_names: set[str] = set()
        for reference, value in values.items():
            service_scope, name = _parse_environment_reference(reference)
            if service_scope == "*" or service_scope not in service_keys:
                raise DeploymentEnvironmentScopeInvalid(
                    "deployment environment requires an explicit plan service"
                )
            known_targets = targets_by_name.get(name)
            if known_targets is not None and service_scope not in known_targets:
                raise DeploymentEnvironmentScopeInvalid(
                    "deployment environment variable targets the wrong service"
                )
            variable = bindings.get((service_scope, name))
            plaintext.append(
                EnvironmentPlaintext(
                    service_scope=service_scope,
                    name=name,
                    value=value,
                    is_sensitive=variable.sensitive if variable else True,
                    required=variable.required if variable else False,
                )
            )
            explicitly_set_names.add(name)

        effective_unset = set(unset)
        for reference in unset:
            service_scope, name = _parse_environment_reference(reference)
            if service_scope != "*" and service_scope not in service_keys:
                raise DeploymentEnvironmentScopeInvalid(
                    "deployment environment unset targets an unknown service"
                )
            known_targets = targets_by_name.get(name)
            if (
                service_scope != "*"
                and known_targets is not None
                and service_scope not in known_targets
            ):
                raise DeploymentEnvironmentScopeInvalid(
                    "deployment environment variable targets the wrong service"
                )
        # Atomically remove broad legacy values once an explicit service value
        # for the same name is supplied. Wildcard deletion is migration-only.
        effective_unset.update(f"*:{name}" for name in explicitly_set_names)
        unset_keys = tuple(
            EnvironmentKey(*_parse_environment_reference(reference))
            for reference in sorted(effective_unset)
        )

        hash_payload = {
            "applicationId": application_id,
            "analysisId": analysis_id,
            "environmentSchemaDigest": environment_schema_digest,
            "expectedVersion": expected_version,
            "set": {
                f"{item.service_scope}:{item.name}": {
                    "value": item.value,
                    "sensitive": item.is_sensitive,
                    "required": item.required,
                }
                for item in plaintext
            },
            "unset": sorted(effective_unset),
        }
        request_hash = keyed_request_hash(hash_payload, self._idempotency_hash_key)
        encrypted = tuple(
            self._crypto.encrypt_value(
                item,
                tenant_id=scope.tenant_id,
                application_id=application_id,
            )
            for item in plaintext
        )
        return await self._catalog.patch_environment_idempotent(
            PatchEnvironment(
                scope=scope,
                application_id=application_id,
                expected_version=expected_version,
                set_values=encrypted,
                unset=unset_keys,
                plan_analysis_id=analysis_id,
                plan_environment_schema_digest=environment_schema_digest,
                plan_service_keys=plan_service_keys,
            ),
            principal=_store_principal(principal),
            idempotency=IdempotencyInput(
                key=idempotency_key,
                method="PATCH",
                route_template=PLAN_ENVIRONMENT_PATCH_ROUTE,
                request_hash=request_hash,
            ),
        )


def _store_principal(principal: Any) -> Principal:
    return Principal(
        "deploy-token" if principal.credential_type == "deploy_token" else "session",
        principal.credential_id,
    )


def _decode_key(value: str, *, exact_32: bool, label: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} is not valid base64") from exc
    if (exact_32 and len(key) != 32) or (not exact_32 and len(key) < 32):
        requirement = "exactly" if exact_32 else "at least"
        raise ValueError(f"{label} must contain {requirement} 256 bits")
    return key


def application_service_from_env(sessions: Any) -> ApplicationApiService:
    """Build the production adapter; every missing secret fails readiness closed."""

    raw_version = os.environ.get("LAE_ENVIRONMENT_AEAD_KEY_VERSION", "")
    raw_keys = os.environ.get("LAE_ENVIRONMENT_AEAD_KEYS", "")
    raw_checksum = os.environ.get("LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY", "")
    raw_idempotency = os.environ.get("LAE_APPLICATION_IDEMPOTENCY_HMAC_KEY", "")
    if not all((raw_version, raw_keys, raw_checksum, raw_idempotency)):
        raise ValueError("application environment key configuration is incomplete")
    try:
        current_version = int(raw_version)
        decoded = json.loads(raw_keys)
        if not isinstance(decoded, Mapping):
            raise ValueError("environment AEAD key ring must be an object")
        keys = {
            int(version): _decode_key(
                encoded, exact_32=True, label="environment AEAD key"
            )
            for version, encoded in decoded.items()
            if isinstance(encoded, str)
        }
        if len(keys) != len(decoded):
            raise ValueError("environment AEAD key ring is invalid")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("environment AEAD key ring is invalid") from exc
    crypto = EnvironmentKeyRing(
        current_version=current_version,
        keys=keys,
        checksum_key=_decode_key(
            raw_checksum, exact_32=False, label="environment checksum key"
        ),
    )
    return ApplicationApiService(
        ApplicationCatalogStore(sessions),
        crypto,
        idempotency_hash_key=_decode_key(
            raw_idempotency,
            exact_32=False,
            label="application idempotency key",
        ),
    )


def register_application_routes(
    app: FastAPI,
    service_getter: Callable[[], Any],
    api_error: type[Exception],
) -> None:
    def error(status: int, code: str, message: str, **kwargs: Any) -> Exception:
        return api_error(status, code, message, **kwargs)

    def no_store(response: JSONResponse) -> JSONResponse:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    async def read_scope(request: Request) -> TenantScope:
        principal = await app.state.require_scoped_principal(
            request, "apps:read", mutation=False
        )
        return TenantScope(principal.tenant_id)

    async def write_principal(
        request: Request, csrf: str | None
    ) -> tuple[Any, TenantScope]:
        principal = await app.state.require_scoped_principal(
            request,
            "apps:write",
            csrf_header=csrf,
            mutation=True,
        )
        return principal, TenantScope(principal.tenant_id)

    @app.post("/v1/applications", status_code=201)
    async def create_application(
        request: Request,
        payload: ApplicationCreateRequest,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        principal, scope = await write_principal(request, x_csrf_token)
        if idempotency_key is None:
            raise error(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        try:
            result = await service_getter().create(
                scope, principal, payload, idempotency_key
            )
        except IdempotencyKeyReused as exc:
            raise error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            ) from exc
        except ApplicationQuotaExceeded as exc:
            raise error(
                409,
                "LAE_APPLICATION_QUOTA_EXCEEDED",
                "Application quota has been reached",
            ) from exc
        except SubscriptionUnavailable as exc:
            raise error(
                409,
                "LAE_SUBSCRIPTION_UNAVAILABLE",
                "An active subscription is required",
            ) from exc
        except ApplicationConflict as exc:
            raise error(
                409,
                "LAE_APPLICATION_CONFLICT",
                "Application name or slug conflicts with existing state",
            ) from exc
        except ValueError as exc:
            raise error(400, "LAE_INVALID_ARGUMENT", "Application request is invalid") from exc
        response = JSONResponse(result.response_body, status_code=201)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)

    @app.get("/v1/applications")
    async def list_applications(request: Request) -> JSONResponse:
        body = await service_getter().list(await read_scope(request))
        return no_store(JSONResponse(body))

    @app.get("/v1/applications/{application_id}")
    async def get_application(application_id: str, request: Request) -> JSONResponse:
        try:
            body = await service_getter().get(await read_scope(request), application_id)
        except (ResourceNotFound, ValueError) as exc:
            raise error(404, "LAE_NOT_FOUND", "Application not found") from exc
        return no_store(JSONResponse(body))

    async def subresource(
        request: Request, application_id: str, method: str
    ) -> JSONResponse:
        try:
            body = await getattr(service_getter(), method)(
                await read_scope(request), application_id
            )
        except (ResourceNotFound, ValueError) as exc:
            raise error(404, "LAE_NOT_FOUND", "Application not found") from exc
        return no_store(JSONResponse(body))

    @app.get("/v1/applications/{application_id}/services")
    async def get_application_services(
        application_id: str, request: Request
    ) -> JSONResponse:
        return await subresource(request, application_id, "services")

    @app.get("/v1/applications/{application_id}/routes")
    async def get_application_routes(
        application_id: str, request: Request
    ) -> JSONResponse:
        return await subresource(request, application_id, "routes")

    @app.get("/v1/applications/{application_id}/volumes")
    async def get_application_volumes(
        application_id: str, request: Request
    ) -> JSONResponse:
        return await subresource(request, application_id, "volumes")

    @app.get("/v1/applications/{application_id}/environment")
    async def get_application_environment(
        application_id: str, request: Request
    ) -> JSONResponse:
        return await subresource(request, application_id, "environment")

    @app.patch("/v1/applications/{application_id}/environment")
    async def patch_application_environment(
        application_id: str,
        request: Request,
        payload: EnvironmentPatchRequest,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        if len(await request.body()) > _MAX_ENV_PATCH_BYTES:
            raise error(
                413,
                "LAE_REQUEST_TOO_LARGE",
                "Environment request body is too large",
            )
        principal, scope = await write_principal(request, x_csrf_token)
        if idempotency_key is None:
            raise error(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        try:
            result = await service_getter().patch_environment(
                scope,
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
                "Environment version does not match current state",
                details={"expectedVersion": exc.expected, "currentVersion": exc.actual},
            ) from exc
        except DeploymentEnvironmentScopeInvalid as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_SCOPE_INVALID",
                "Environment variables must target an allowed application service",
            ) from exc
        except ResourceNotFound as exc:
            raise error(404, "LAE_NOT_FOUND", "Application not found") from exc
        except ApplicationConflict as exc:
            raise error(
                409,
                "LAE_ENVIRONMENT_CONFLICT",
                "Environment update conflicts with current state",
            ) from exc
        except ValueError as exc:
            raise error(400, "LAE_INVALID_ARGUMENT", "Environment request is invalid") from exc
        response = JSONResponse(result.response_body)
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return no_store(response)
