from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

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
        version="2026.07.11-1",
        name="Next.js",
        description="Container-ready App Router starter with standalone output.",
        stack="Next.js · Node.js",
        repository="https://github.com/nextjs/deploy-fly.git",
        commit="eb2da4890980776de122157e590d30c9faa82e6e",
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


class TemplateApiService:
    def __init__(
        self,
        application_service_getter: Callable[[], Any],
        analysis_store_getter: Callable[[], Any],
    ) -> None:
        self._application_service_getter = application_service_getter
        self._analysis_store_getter = analysis_store_getter
        self._by_id = {item.id: item for item in TEMPLATES}

    def list(self) -> dict[str, object]:
        return {"templates": [item.public_body() for item in TEMPLATES]}

    async def launch(
        self,
        *,
        principal: Any,
        template_id: str,
        payload: TemplateLaunchRequest,
        idempotency_key: str,
    ) -> tuple[dict[str, object], bool]:
        definition = self._by_id.get(template_id)
        if definition is None:
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
) -> None:
    def error(
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> Exception:
        return api_error(status, code, message, retryable=retryable)

    @app.get("/v1/templates")
    async def list_templates() -> JSONResponse:
        response = JSONResponse(service.list())
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=3600"
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
        try:
            body, replayed = await service.launch(
                principal=principal,
                template_id=template_id,
                payload=payload,
                idempotency_key=idempotency_key,
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


__all__ = [
    "TEMPLATES",
    "TemplateApiService",
    "TemplateDefinition",
    "TemplateLaunchRequest",
    "register_template_routes",
]
