from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from lae_luma_adapter import (
    HttpLumaRuntimeAdapter,
    LumaAdapterError,
    RuntimeCallContext,
    RuntimeServicePrincipal,
)
from lae_store import ResourceNotFound, TenantScope, require_opaque_id
from lae_store.models import (
    Application,
    ApplicationRoute,
    ApplicationService,
    Deployment,
)


_SERVICE_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class ObservabilityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeReadBinding:
    tenant_ref: str
    application_ref: str
    operation_ref: str
    revision_ref: str
    deployment_ref: str
    service_key: str

    def context(self, *, request_id: str | None = None) -> RuntimeCallContext:
        return RuntimeCallContext(
            tenant_ref=self.tenant_ref,
            application_ref=self.application_ref,
            operation_ref=self.operation_ref,
            revision_ref=self.revision_ref,
            deployment_ref=self.deployment_ref,
            request_id=request_id,
        )


class PostgresObservabilityBindingStore:
    """Resolve an app-scoped runtime binding without accepting runtime IDs."""

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def resolve(
        self,
        scope: TenantScope,
        application_id: str,
        service_key: str | None,
    ) -> RuntimeReadBinding:
        require_opaque_id(application_id, prefix="app")
        if service_key is not None and _SERVICE_KEY.fullmatch(service_key) is None:
            raise ValueError("service key is invalid")
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Application, Deployment)
                    .join(
                        Deployment,
                        (Deployment.tenant_id == Application.tenant_id)
                        & (Deployment.application_id == Application.id)
                        & (Deployment.id == Application.current_deployment_id),
                    )
                    .where(
                        Application.tenant_id == scope.tenant_id,
                        Application.id == application_id,
                        Application.deleted_at.is_(None),
                        Application.current_deployment_id.is_not(None),
                        Application.current_revision_id.is_not(None),
                        Deployment.status == "succeeded",
                    )
                )
            ).one_or_none()
            if row is None:
                raise ResourceNotFound("active deployment not found")
            application, deployment = row
            if (
                deployment.revision_id != application.current_revision_id
                or deployment.luma_external_ref != deployment.id
            ):
                raise ObservabilityUnavailable("runtime binding is incomplete")

            keys = tuple(
                await session.scalars(
                    select(ApplicationService.service_key)
                    .where(
                        ApplicationService.tenant_id == scope.tenant_id,
                        ApplicationService.application_id == application.id,
                        ApplicationService.desired_state != "deleted",
                    )
                    .order_by(ApplicationService.service_key)
                )
            )
            if not keys:
                raise ObservabilityUnavailable("runtime services are unavailable")
            selected = service_key
            if selected is None:
                selected = await session.scalar(
                    select(ApplicationService.service_key)
                    .join(
                        ApplicationRoute,
                        (ApplicationRoute.tenant_id == ApplicationService.tenant_id)
                        & (
                            ApplicationRoute.application_id
                            == ApplicationService.application_id
                        )
                        & (ApplicationRoute.service_id == ApplicationService.id),
                    )
                    .where(
                        ApplicationRoute.tenant_id == scope.tenant_id,
                        ApplicationRoute.application_id == application.id,
                        ApplicationRoute.is_primary.is_(True),
                    )
                )
                selected = selected or keys[0]
            if selected not in keys:
                raise ResourceNotFound("application service not found")
            return RuntimeReadBinding(
                tenant_ref=scope.tenant_id,
                application_ref=application.id,
                operation_ref=deployment.operation_id,
                revision_ref=deployment.revision_id,
                deployment_ref=deployment.id,
                service_key=selected,
            )


class ApplicationObservabilityService:
    def __init__(self, bindings: Any, runtime: Any) -> None:
        self._bindings = bindings
        self._runtime = runtime

    async def logs(
        self,
        scope: TenantScope,
        application_id: str,
        *,
        service_key: str | None,
        tail: int,
        request_id: str | None,
    ) -> dict[str, object]:
        binding = await self._bindings.resolve(scope, application_id, service_key)
        try:
            result = await asyncio.to_thread(
                self._runtime.tail_runtime_logs,
                binding.context(request_id=request_id),
                binding.deployment_ref,
                binding.service_key,
                tail=tail,
            )
        except LumaAdapterError:
            raise ObservabilityUnavailable("runtime logs are unavailable") from None
        return {
            "applicationId": binding.application_ref,
            "deploymentId": binding.deployment_ref,
            "serviceKey": result.service_key,
            "tail": result.tail,
            "logs": list(result.logs),
            "truncated": result.truncated,
            "updatedAt": result.updated_at,
        }

    async def metrics(
        self,
        scope: TenantScope,
        application_id: str,
        *,
        service_key: str | None,
        window_seconds: int,
        request_id: str | None,
    ) -> dict[str, object]:
        binding = await self._bindings.resolve(scope, application_id, service_key)
        try:
            result = await asyncio.to_thread(
                self._runtime.get_runtime_metrics_history,
                binding.context(request_id=request_id),
                binding.deployment_ref,
                binding.service_key,
                window_seconds=window_seconds,
            )
        except LumaAdapterError:
            raise ObservabilityUnavailable("runtime metrics are unavailable") from None
        return {
            "applicationId": binding.application_ref,
            "deploymentId": binding.deployment_ref,
            "serviceKey": result.service_key,
            "windowSeconds": result.window_seconds,
            "series": {
                name: [list(point) for point in points]
                for name, points in result.series.items()
            },
            "updatedAt": result.updated_at,
        }


def observability_service_from_env(
    sessions: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> ApplicationObservabilityService:
    values = os.environ if environ is None else environ
    endpoint = values.get("LAE_LUMA_RUNTIME_URL", "").strip()
    principal_id = values.get("LAE_LUMA_RUNTIME_PRINCIPAL_ID", "").strip()
    token = values.get("LAE_LUMA_RUNTIME_SERVICE_TOKEN", "")
    if not endpoint or not principal_id or not token:
        raise ValueError("runtime observability binding is incomplete")
    try:
        timeout = float(values.get("LAE_LUMA_RUNTIME_HTTP_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise ValueError("runtime observability timeout is invalid") from exc
    return ApplicationObservabilityService(
        PostgresObservabilityBindingStore(sessions),
        HttpLumaRuntimeAdapter(
            endpoint,
            RuntimeServicePrincipal(principal_id, token),
            timeout_seconds=timeout,
        ),
    )


def register_observability_routes(
    app: FastAPI,
    service_getter: Callable[[], Any],
    api_error: type[Exception],
) -> None:
    def error(status: int, code: str, message: str, **kwargs: Any) -> Exception:
        return api_error(status, code, message, **kwargs)

    async def principal(request: Request) -> tuple[Any, TenantScope]:
        selected = await app.state.require_scoped_principal(
            request, "logs:read", mutation=False
        )
        return selected, TenantScope(selected.tenant_id)

    def translate(exc: Exception) -> Exception:
        if isinstance(exc, ResourceNotFound):
            return error(404, "LAE_NOT_FOUND", "Application runtime was not found")
        if isinstance(exc, ObservabilityUnavailable):
            return error(
                503,
                "LAE_OBSERVABILITY_UNAVAILABLE",
                "Application observability is temporarily unavailable",
                retryable=True,
            )
        return error(400, "LAE_INVALID_ARGUMENT", "Observability query is invalid")

    @app.get("/v1/applications/{application_id}/logs")
    async def application_logs(
        application_id: str,
        request: Request,
        service: Annotated[str | None, Query(max_length=63)] = None,
        tail: Annotated[int, Query(ge=1, le=500)] = 120,
    ) -> JSONResponse:
        _selected, scope = await principal(request)
        try:
            body = await service_getter().logs(
                scope,
                application_id,
                service_key=service,
                tail=tail,
                request_id=getattr(request.state, "request_id", None),
            )
        except (ResourceNotFound, ObservabilityUnavailable, ValueError) as exc:
            raise translate(exc) from exc
        response = JSONResponse(body)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/v1/applications/{application_id}/metrics")
    async def application_metrics(
        application_id: str,
        request: Request,
        service: Annotated[str | None, Query(max_length=63)] = None,
        window: Annotated[int, Query(ge=60, le=604800)] = 3600,
    ) -> JSONResponse:
        _selected, scope = await principal(request)
        try:
            body = await service_getter().metrics(
                scope,
                application_id,
                service_key=service,
                window_seconds=window,
                request_id=getattr(request.state, "request_id", None),
            )
        except (ResourceNotFound, ObservabilityUnavailable, ValueError) as exc:
            raise translate(exc) from exc
        response = JSONResponse(body)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


__all__ = [
    "ApplicationObservabilityService",
    "ObservabilityUnavailable",
    "PostgresObservabilityBindingStore",
    "RuntimeReadBinding",
    "observability_service_from_env",
    "register_observability_routes",
]
