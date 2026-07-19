from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lae_store import (
    ApplicationConflict,
    ApplicationQuotaExceeded,
    CreateAnalysisRequest,
    IdempotencyKeyReused,
    OperationConflict,
    Principal,
    ResourceNotFound,
    SourceConnectionHostMismatch,
    SourceConnectionUnavailable,
    SubscriptionUnavailable,
    TenantScope,
)

from .application_api import ApplicationCreateRequest


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    id: str
    version: str
    name: str
    description: str
    stack: str
    repository: str
    commit: str
    subdirectory: str = ""
    kind: str = "service"
    icon: str = "sparkles"
    tone: str = "mist"
    estimated_memory_mib: int = 512

    def public_body(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "stack": self.stack,
            "kind": self.kind,
            "icon": self.icon,
            "tone": self.tone,
            "estimatedResources": {"memoryMiB": self.estimated_memory_mib},
            "verification": {
                "status": "agent-pass",
                "policyVersion": "2026-07-11",
                "sourceCommit": self.commit,
            },
        }


# Each source revision below was analyzed with the checked-in 2026-07-11
# policy and produced an allow decision, one HTTP route, and no required env.
# Launch still creates a fresh Builder analysis; this catalog is never an
# allowlist that bypasses current policy or image scanning.
TEMPLATES: tuple[TemplateDefinition, ...] = (
    TemplateDefinition(
        id="nextjs-docker",
        version="2026.07.14-1",
        name="Next.js",
        description="Pinned App Router starter with reproducible standalone output.",
        stack="Next.js · Node.js",
        repository="https://github.com/LiuTianjie/luma.git",
        commit="a759c8606fdb21f793b0e8071c99491ca7ba52c8",
        subdirectory="lae/e2e/fixtures/nextjs-standalone",
        icon="orbit",
        tone="mist",
        estimated_memory_mib=768,
    ),
    TemplateDefinition(
        id="fastapi-minimal",
        version="2026.07.11-1",
        name="FastAPI",
        description="Small Python API with an automatically generated OpenAPI UI.",
        stack="FastAPI · Python",
        repository="https://github.com/render-examples/fastapi.git",
        commit="f829276c68503f7afae195c3e3f778f085242cb0",
        icon="server-cog",
        tone="moss",
        estimated_memory_mib=512,
    ),
    TemplateDefinition(
        id="flask-hello",
        version="2026.07.11-1",
        name="Flask",
        description="A restrained Python web service ready for customization.",
        stack="Flask · Python",
        repository="https://github.com/render-examples/flask-hello-world.git",
        commit="facfc73c44b35d00bb1f72e8986f0ea47cab0755",
        icon="layers",
        tone="pearl",
        estimated_memory_mib=384,
    ),
    TemplateDefinition(
        id="express-hello",
        version="2026.07.11-1",
        name="Express",
        description="Minimal Node HTTP service with a conventional start command.",
        stack="Express · Node.js",
        repository="https://github.com/render-examples/express-hello-world.git",
        commit="039c34770852fb07cef7f9f0f8534c5de408b207",
        icon="square-terminal",
        tone="amber",
        estimated_memory_mib=384,
    ),
)


class TemplateLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    region: str = Field(default="cn", pattern=r"^(cn|global)$")


class TemplateSmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(alias="runId", min_length=1, max_length=80)
    template_id: str = Field(
        alias="templateId",
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    version: str = Field(min_length=1, max_length=64)
    status: Literal["succeeded", "failed"]
    error_code: str | None = Field(
        default=None,
        alias="errorCode",
        max_length=80,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )


class TemplateSmokeAuthenticator:
    def __init__(self, token: str) -> None:
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 512
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ValueError("template smoke token is invalid")
        self._token = token

    def require(self, authorization: str | None) -> None:
        prefix = "Bearer "
        if (
            authorization is None
            or not authorization.startswith(prefix)
            or not hmac.compare_digest(authorization[len(prefix) :], self._token)
        ):
            raise PermissionError("template smoke authentication failed")

    def require_token(self, token: str | None) -> None:
        if token is None or not hmac.compare_digest(token, self._token):
            raise PermissionError("template smoke authentication failed")


def template_smoke_authenticator_from_env() -> TemplateSmokeAuthenticator:
    return TemplateSmokeAuthenticator(
        os.environ.get("LAE_TEMPLATE_SMOKE_REPORT_TOKEN", "").strip()
    )


@dataclass(frozen=True, slots=True)
class _DefaultPublication:
    template_id: str
    template_version: str
    published: bool = True
    consecutive_failures: int = 0
    last_status: str = "unverified"
    last_error_code: str | None = None


class _AlwaysPublished:
    async def publication(
        self, template_id: str, template_version: str
    ) -> _DefaultPublication:
        return _DefaultPublication(template_id, template_version)


class TemplateApiService:
    def __init__(
        self,
        application_service_getter: Callable[[], Any],
        analysis_store_getter: Callable[[], Any],
        publication_store_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._application_service_getter = application_service_getter
        self._analysis_store_getter = analysis_store_getter
        self._publication_store_getter = publication_store_getter
        self._by_id = {item.id: item for item in TEMPLATES}

    def _publication_store(self) -> Any:
        if self._publication_store_getter is None:
            return _AlwaysPublished()
        return self._publication_store_getter() or _AlwaysPublished()

    async def list(self, *, include_unpublished: bool = False) -> dict[str, object]:
        templates: list[dict[str, object]] = []
        store = self._publication_store()
        for item in TEMPLATES:
            publication = await store.publication(item.id, item.version)
            if not publication.published and not include_unpublished:
                continue
            body = item.public_body()
            verification = cast(dict[str, object], body["verification"])
            body["verification"] = {
                **verification,
                "healthStatus": publication.last_status,
            }
            if include_unpublished:
                body["publication"] = {
                    "published": publication.published,
                    "consecutiveFailures": publication.consecutive_failures,
                    "errorCode": publication.last_error_code,
                }
            templates.append(body)
        return {"templates": templates}

    async def record_smoke(self, payload: TemplateSmokeResult) -> dict[str, object]:
        definition = self._by_id.get(payload.template_id)
        if definition is None or definition.version != payload.version:
            raise ResourceNotFound("template version not found")
        if self._publication_store_getter is None:
            raise RuntimeError("template health store is unavailable")
        store = self._publication_store_getter()
        if store is None:
            raise RuntimeError("template health store is unavailable")
        publication = await store.record(
            template_id=payload.template_id,
            template_version=payload.version,
            run_id=payload.run_id,
            succeeded=payload.status == "succeeded",
            error_code=payload.error_code,
        )
        return {
            "templateId": publication.template_id,
            "version": publication.template_version,
            "published": publication.published,
            "consecutiveFailures": publication.consecutive_failures,
            "status": publication.last_status,
            "errorCode": publication.last_error_code,
        }

    async def launch(
        self,
        *,
        principal: Any,
        template_id: str,
        payload: TemplateLaunchRequest,
        idempotency_key: str,
        allow_unpublished: bool = False,
    ) -> tuple[dict[str, object], bool]:
        definition = self._by_id.get(template_id)
        if definition is None:
            raise ResourceNotFound("template not found")
        publication = await self._publication_store().publication(
            definition.id, definition.version
        )
        if not publication.published and not allow_unpublished:
            raise ResourceNotFound("template not found")
        suffix = hashlib.sha256(
            f"{template_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
        application_result = await self._application_service_getter().create(
            TenantScope(principal.tenant_id),
            principal,
            ApplicationCreateRequest(name=payload.name, slug=payload.slug),
            f"template-app:{suffix}",
        )
        application = application_result.response_body["application"]
        analysis_result = await self._analysis_store_getter().create(
            CreateAnalysisRequest(
                scope=TenantScope(principal.tenant_id),
                principal=Principal(
                    "deploy-token"
                    if principal.credential_type == "deploy_token"
                    else "session",
                    principal.credential_id,
                ),
                application_id=application["id"],
                repository=definition.repository,
                ref=definition.commit,
                subdirectory=definition.subdirectory,
                connection_id=None,
                region=payload.region,
                public_protocols=("http",),
                idempotency_key=f"template-analysis:{suffix}",
            )
        )
        return (
            {
                "template": definition.public_body(),
                "application": application,
                **analysis_result.public_body(),
            },
            application_result.replayed and analysis_result.replayed,
        )


def register_template_routes(
    app: FastAPI,
    service: TemplateApiService,
    api_error: type[Exception],
    smoke_authenticator_getter: Callable[[], TemplateSmokeAuthenticator] | None = None,
) -> None:
    def error(
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> Exception:
        return api_error(status, code, message, retryable=retryable)

    def smoke_authenticator() -> TemplateSmokeAuthenticator:
        if smoke_authenticator_getter is None:
            raise error(
                503,
                "LAE_TEMPLATE_SMOKE_UNAVAILABLE",
                "Template smoke reporting is unavailable",
                retryable=True,
            )
        authenticator = smoke_authenticator_getter()
        if not isinstance(authenticator, TemplateSmokeAuthenticator):
            raise error(
                503,
                "LAE_TEMPLATE_SMOKE_UNAVAILABLE",
                "Template smoke reporting is unavailable",
                retryable=True,
            )
        return authenticator

    def require_smoke_token(token: str | None) -> None:
        try:
            smoke_authenticator().require_token(token)
        except PermissionError as exc:
            raise error(
                401,
                "LAE_TEMPLATE_SMOKE_UNAUTHENTICATED",
                "Template smoke authentication is required",
            ) from exc

    @app.get("/v1/templates")
    async def list_templates(
        smoke_token: Annotated[
            str | None, Header(alias="X-LAE-Template-Smoke-Token")
        ] = None,
    ) -> JSONResponse:
        include_unpublished = smoke_token is not None
        if include_unpublished:
            require_smoke_token(smoke_token)
        response = JSONResponse(
            await service.list(include_unpublished=include_unpublished)
        )
        response.headers["Cache-Control"] = (
            "no-store, max-age=0"
            if include_unpublished
            else "public, max-age=300, stale-while-revalidate=3600"
        )
        return response

    @app.post("/v1/templates/{template_id}/launch", status_code=202)
    async def launch_template(
        template_id: str,
        payload: TemplateLaunchRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
        smoke_token: Annotated[
            str | None, Header(alias="X-LAE-Template-Smoke-Token")
        ] = None,
    ) -> JSONResponse:
        principal = await app.state.require_scoped_principal(
            request,
            "apps:write",
            csrf_header=x_csrf_token,
            mutation=True,
        )
        if "analyses:write" not in principal.scopes:
            raise error(
                403,
                "LAE_FORBIDDEN",
                "The credential lacks the required scope",
            )
        if idempotency_key is None:
            raise error(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        allow_unpublished = smoke_token is not None
        if allow_unpublished:
            require_smoke_token(smoke_token)
        try:
            body, replayed = await service.launch(
                principal=principal,
                template_id=template_id,
                payload=payload,
                idempotency_key=idempotency_key,
                allow_unpublished=allow_unpublished,
            )
        except ResourceNotFound as exc:
            raise error(404, "LAE_NOT_FOUND", "Template was not found") from exc
        except IdempotencyKeyReused as exc:
            raise error(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for another request",
            ) from exc
        except ApplicationQuotaExceeded as exc:
            raise error(409, "LAE_APPLICATION_QUOTA_EXCEEDED", "Application quota exceeded") from exc
        except SubscriptionUnavailable as exc:
            raise error(503, "LAE_SUBSCRIPTION_UNAVAILABLE", "Subscription is unavailable", retryable=True) from exc
        except (ApplicationConflict, OperationConflict) as exc:
            raise error(409, "LAE_TEMPLATE_LAUNCH_CONFLICT", "Template launch conflicts with current state") from exc
        except SourceConnectionUnavailable as exc:
            raise error(503, "LAE_SOURCE_CONNECTIONS_UNAVAILABLE", "Source analysis is unavailable", retryable=True) from exc
        except SourceConnectionHostMismatch as exc:
            raise error(400, "LAE_TEMPLATE_SOURCE_INVALID", "Template source is invalid") from exc
        except ValueError as exc:
            raise error(400, "LAE_TEMPLATE_LAUNCH_INVALID", "Template launch request is invalid") from exc

        response = JSONResponse(body, status_code=202)
        response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.post("/internal/v1/template-smoke/results")
    async def record_template_smoke(
        payload: TemplateSmokeResult,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        try:
            smoke_authenticator().require(authorization)
        except (PermissionError, ValueError) as exc:
            raise error(
                401,
                "LAE_TEMPLATE_SMOKE_UNAUTHENTICATED",
                "Template smoke authentication is required",
            ) from exc
        try:
            body = await service.record_smoke(payload)
        except ResourceNotFound as exc:
            raise error(404, "LAE_NOT_FOUND", "Template version was not found") from exc
        except ValueError as exc:
            raise error(
                400,
                "LAE_TEMPLATE_SMOKE_INVALID",
                "Template smoke result is invalid",
            ) from exc
        except RuntimeError as exc:
            raise error(
                503,
                "LAE_TEMPLATE_HEALTH_UNAVAILABLE",
                "Template health is temporarily unavailable",
                retryable=True,
            ) from exc
        response = JSONResponse(body)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


__all__ = [
    "TEMPLATES",
    "TemplateApiService",
    "TemplateDefinition",
    "TemplateLaunchRequest",
    "TemplateSmokeAuthenticator",
    "TemplateSmokeResult",
    "template_smoke_authenticator_from_env",
    "register_template_routes",
]
