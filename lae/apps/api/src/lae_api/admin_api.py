from __future__ import annotations

import hmac
import os
import stat
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from lae_store.models import (
    Application,
    ApplicationService,
    ApplicationVolume,
    Operation,
    PlanVersion,
    Subscription,
    Tenant,
    Upload,
    User,
)


class AdminAuthenticationError(RuntimeError):
    pass


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AdminAuthenticator:
    """Independent Luma Dashboard service credential verifier."""

    def __init__(self, token: str) -> None:
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 512
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ValueError("admin API token is invalid")
        self._token = token

    def require(self, authorization_values: list[str]) -> None:
        if len(authorization_values) != 1:
            raise AdminAuthenticationError()
        value = authorization_values[0]
        prefix = "Bearer "
        if not value.startswith(prefix) or not hmac.compare_digest(
            value[len(prefix) :], self._token
        ):
            raise AdminAuthenticationError()


def _read_token_file(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("admin API token file is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or not 1 <= metadata.st_size <= 4096
    ):
        raise ValueError("admin API token file is unsafe")
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError("admin API token file is unavailable") from None


def admin_authenticator_from_env(
    environ: Mapping[str, str] | None = None,
) -> AdminAuthenticator:
    values = os.environ if environ is None else environ
    token_file = values.get("LAE_ADMIN_API_TOKEN_FILE", "").strip()
    test_mode = values.get("LAE_ADMIN_API_TEST_MODE", "") == "1"
    direct = values.get("LAE_ADMIN_API_TOKEN", "") if test_mode else ""
    if bool(token_file) == bool(direct):
        raise ValueError("exactly one admin API credential source is required")
    return AdminAuthenticator(_read_token_file(Path(token_file)) if token_file else direct)


class PostgresAdminReadStore:
    """Cross-tenant read model used only behind the internal admin token."""

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def users(self, *, limit: int, offset: int) -> dict[str, object]:
        async with self._sessions() as session:
            total = int(await session.scalar(select(func.count()).select_from(User)) or 0)
            rows = tuple(
                await session.scalars(
                    select(User)
                    .order_by(User.created_at.desc(), User.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return {
                "users": [
                    {
                        "id": row.id,
                        "email": row.email,
                        "status": row.status,
                        "emailVerifiedAt": _timestamp(row.email_verified_at),
                        "lastLoginAt": _timestamp(row.last_login_at),
                        "createdAt": _timestamp(row.created_at),
                    }
                    for row in rows
                ],
                "page": {"limit": limit, "offset": offset, "total": total},
            }

    async def tenants(self, *, limit: int, offset: int) -> dict[str, object]:
        async with self._sessions() as session:
            total = int(await session.scalar(select(func.count()).select_from(Tenant)) or 0)
            rows = (
                await session.execute(
                    select(Tenant, User.email)
                    .join(User, User.id == Tenant.owner_user_id)
                    .order_by(Tenant.created_at.desc(), Tenant.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            items: list[dict[str, object]] = []
            for tenant, owner_email in rows:
                plan = await session.scalar(
                    select(PlanVersion.code)
                    .join(Subscription, Subscription.plan_version_id == PlanVersion.id)
                    .where(
                        Subscription.tenant_id == tenant.id,
                        Subscription.status.in_(("active", "trialing", "past_due")),
                    )
                    .order_by(Subscription.created_at.desc())
                    .limit(1)
                )
                items.append(
                    {
                        "id": tenant.id,
                        "type": tenant.type,
                        "name": tenant.name,
                        "slug": tenant.slug,
                        "status": tenant.status,
                        "ownerUserId": tenant.owner_user_id,
                        "ownerEmail": owner_email,
                        "plan": plan,
                        "createdAt": _timestamp(tenant.created_at),
                    }
                )
            return {
                "tenants": items,
                "page": {"limit": limit, "offset": offset, "total": total},
            }

    async def applications(self, *, limit: int, offset: int) -> dict[str, object]:
        async with self._sessions() as session:
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Application)
                    .where(Application.deleted_at.is_(None))
                )
                or 0
            )
            rows = tuple(
                await session.scalars(
                    select(Application)
                    .where(Application.deleted_at.is_(None))
                    .order_by(Application.created_at.desc(), Application.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            items: list[dict[str, object]] = []
            for application in rows:
                service_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ApplicationService)
                        .where(
                            ApplicationService.tenant_id == application.tenant_id,
                            ApplicationService.application_id == application.id,
                        )
                    )
                    or 0
                )
                volume_bytes = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(ApplicationVolume.requested_bytes), 0)).where(
                            ApplicationVolume.tenant_id == application.tenant_id,
                            ApplicationVolume.application_id == application.id,
                            ApplicationVolume.status.notin_(("deleted", "retained")),
                        )
                    )
                    or 0
                )
                items.append(
                    {
                        "id": application.id,
                        "tenantId": application.tenant_id,
                        "name": application.name,
                        "slug": application.slug,
                        "lumaName": application.luma_name,
                        "kind": application.kind,
                        "desiredState": application.desired_state,
                        "observedState": application.observed_state,
                        "currentRevisionId": application.current_revision_id,
                        "currentDeploymentId": application.current_deployment_id,
                        "serviceCount": service_count,
                        "requestedVolumeBytes": volume_bytes,
                        "createdAt": _timestamp(application.created_at),
                        "updatedAt": _timestamp(application.updated_at),
                    }
                )
            return {
                "applications": items,
                "page": {"limit": limit, "offset": offset, "total": total},
            }

    async def operations(self, *, limit: int, offset: int) -> dict[str, object]:
        async with self._sessions() as session:
            total = int(await session.scalar(select(func.count()).select_from(Operation)) or 0)
            rows = tuple(
                await session.scalars(
                    select(Operation)
                    .order_by(Operation.created_at.desc(), Operation.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return {
                "operations": [
                    {
                        "id": row.id,
                        "tenantId": row.tenant_id,
                        "kind": row.kind,
                        "targetType": row.target_type,
                        "targetId": row.target_id,
                        "status": row.status,
                        "phase": row.phase,
                        "errorCode": row.error_code,
                        "cancelRequested": row.cancel_requested_at is not None,
                        "createdAt": _timestamp(row.created_at),
                        "startedAt": _timestamp(row.started_at),
                        "finishedAt": _timestamp(row.finished_at),
                    }
                    for row in rows
                ],
                "page": {"limit": limit, "offset": offset, "total": total},
            }

    async def usage(self, *, limit: int, offset: int) -> dict[str, object]:
        async with self._sessions() as session:
            total = int(await session.scalar(select(func.count()).select_from(Tenant)) or 0)
            tenants = tuple(
                await session.scalars(
                    select(Tenant)
                    .order_by(Tenant.created_at.desc(), Tenant.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            items: list[dict[str, object]] = []
            for tenant in tenants:
                applications = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Application)
                        .where(
                            Application.tenant_id == tenant.id,
                            Application.deleted_at.is_(None),
                        )
                    )
                    or 0
                )
                volume_bytes = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(ApplicationVolume.requested_bytes), 0)).where(
                            ApplicationVolume.tenant_id == tenant.id,
                            ApplicationVolume.status.notin_(("deleted", "retained")),
                        )
                    )
                    or 0
                )
                upload_bytes = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(Upload.actual_bytes), 0)).where(
                            Upload.tenant_id == tenant.id,
                            Upload.status == "ready",
                            Upload.deleted_at.is_(None),
                        )
                    )
                    or 0
                )
                items.append(
                    {
                        "tenantId": tenant.id,
                        "applicationCount": applications,
                        "requestedVolumeBytes": volume_bytes,
                        "storedUploadBytes": upload_bytes,
                    }
                )
            return {
                "usage": items,
                "page": {"limit": limit, "offset": offset, "total": total},
            }


def register_admin_routes(
    app: FastAPI,
    authenticator_getter: Callable[[], AdminAuthenticator],
    store_getter: Callable[[], Any],
    api_error: type[Exception],
) -> None:
    async def require_admin(request: Request) -> None:
        try:
            authenticator_getter().require(request.headers.getlist("authorization"))
        except AdminAuthenticationError as exc:
            raise api_error(
                401, "LAE_ADMIN_UNAUTHENTICATED", "Admin authentication is required"
            ) from exc

    def response(body: dict[str, object]) -> JSONResponse:
        result = JSONResponse(body)
        result.headers["Cache-Control"] = "no-store, max-age=0"
        result.headers["Pragma"] = "no-cache"
        return result

    async def page(
        request: Request,
        method: str,
        limit: int,
        offset: int,
    ) -> JSONResponse:
        await require_admin(request)
        body = await getattr(store_getter(), method)(limit=limit, offset=offset)
        return response(body)

    @app.get("/internal/v1/admin/users")
    async def users(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> JSONResponse:
        return await page(request, "users", limit, offset)

    @app.get("/internal/v1/admin/tenants")
    async def tenants(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> JSONResponse:
        return await page(request, "tenants", limit, offset)

    @app.get("/internal/v1/admin/applications")
    async def applications(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> JSONResponse:
        return await page(request, "applications", limit, offset)

    @app.get("/internal/v1/admin/operations")
    async def operations(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> JSONResponse:
        return await page(request, "operations", limit, offset)

    @app.get("/internal/v1/admin/usage")
    async def usage(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> JSONResponse:
        return await page(request, "usage", limit, offset)


__all__ = [
    "AdminAuthenticationError",
    "AdminAuthenticator",
    "PostgresAdminReadStore",
    "admin_authenticator_from_env",
    "register_admin_routes",
]
