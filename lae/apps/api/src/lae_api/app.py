from __future__ import annotations

import base64
import binascii
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Sequence
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from lae_core import VERSION, component_payload
from lae_store import (
    CreateAnalysisRequest,
    IdempotencyKeyReused,
    OperationConflict,
    PostgresAnalysisRequestStore,
    PostgresPublicResourceStore,
    Principal,
    PublicAnalysisRecord,
    PublicOperationEventPage,
    PublicOperationPage,
    PublicOperationRecord,
    ResourceNotFound,
    SourceConnectionHostMismatch,
    SourceConnectionUnavailable,
    TenantScope,
    create_postgres_engine,
    create_session_factory,
    new_id,
    required_scope_for_operation,
)
from lae_store.auth import (
    AuthenticatedPrincipal,
    AuthCompletion,
    AuthConfigurationError,
    AuthKeyRing,
    AuthRejected,
    DeployTokenConflict,
    DeployTokenNotFound,
    DeployTokenPrincipal,
    DeployTokenRecord,
    PostgresAuthStore,
    SessionPrincipal,
)

from .auth_service import (
    AuthService,
    CsrfRejected,
    PreviewAuthDisabled,
    PreviewAuthUnavailable,
    preview_email_from_env,
)
from .admin_api import (
    PostgresAdminReadStore,
    admin_authenticator_from_env,
    register_admin_routes,
)
from .application_api import application_service_from_env, register_application_routes
from .application_lifecycle_api import (
    application_lifecycle_service_from_env,
    register_application_lifecycle_routes,
)
from .billing import (
    BillingHttpError,
    BillingRuntime,
    billing_runtime_from_env,
    create_billing_router,
)
from .deployment_api import deployment_service_from_env, register_deployment_routes
from .credential_broker_api import (
    credential_broker_runtime_from_env,
    register_credential_broker_route,
)
from .object_source_broker_api import (
    object_source_broker_runtime_from_env,
    register_object_source_broker_route,
)
from .observability_api import (
    observability_service_from_env,
    register_observability_routes,
)
from .plan_resolver import deployment_plan_resolver_from_env
from .email import (
    ConsoleEmailSender,
    EmailConfigurationError,
    SmtpEmailSender,
    smtp_config_from_env,
)
from .source_connection_api import (
    register_source_connection_routes,
    source_connection_service_from_env,
)
from .template_api import TemplateApiService, register_template_routes
from .upload_api import (
    UploadAnalysisSourceRequest,
    create_upload_analysis,
    register_upload_routes,
    upload_analysis_store_from_env,
    upload_service_from_env,
)

SESSION_COOKIE = "__Host-lae_session"
CSRF_COOKIE = "__Host-lae_csrf"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_BEARER = re.compile(r"^Bearer (?P<token>[^\s,]{1,128})$", re.IGNORECASE)
_CREDENTIAL_LIKE = re.compile(
    r"(?:lae_(?:dt|ss_v[0-9]+|cs|em)_[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~-]{8,})",
    re.IGNORECASE,
)
_PUBLIC_OPERATION_LIST_SCOPES = frozenset(
    {"analyses:write", "sources:write", "deployments:write", "apps:write"}
)
_CORS_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_CORS_HEADERS = [
    "Accept",
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-CSRF-Token",
    "X-Request-Id",
]


def _cors_allowed_origins(
    configured: Sequence[str] | str | None = None,
) -> list[str]:
    raw_origins: Sequence[str]
    if configured is None:
        raw_origins = os.environ.get("LAE_CORS_ALLOWED_ORIGINS", "").split(",")
    elif isinstance(configured, str):
        raw_origins = configured.split(",")
    else:
        raw_origins = configured

    origins: list[str] = []
    for raw_origin in raw_origins:
        origin = raw_origin.strip()
        if not origin:
            continue
        try:
            parsed = urlsplit(origin)
            parsed_port = parsed.port
        except ValueError as exc:
            raise AuthConfigurationError("CORS allowed origin is invalid") from exc
        if (
            origin == "*"
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise AuthConfigurationError("CORS allowed origin is invalid")
        normalized = f"{parsed.scheme.lower()}://{parsed.hostname.lower()}"
        if parsed_port is not None:
            normalized += f":{parsed_port}"
        if origin.lower() != normalized:
            raise AuthConfigurationError("CORS allowed origin is invalid")
        if normalized not in origins:
            origins.append(normalized)
    return origins


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRequest(StrictModel):
    # Missing/malformed mailbox identities intentionally still take the public
    # 202 path, including structural validation failures on start endpoints.
    email: str = Field(default="", max_length=320)


class ResendRequest(StartRequest):
    purpose: Literal["register", "login"]


class VerifyRequest(StartRequest):
    code: str | None = Field(default=None, max_length=32)
    magicToken: str | None = Field(default=None, max_length=128)


class DeployTokenCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=16)
    expiresAt: datetime | None = None


class GitAnalysisSourceRequest(StrictModel):
    type: Literal["git"]
    repository: str = Field(min_length=1, max_length=2048)
    ref: str = Field(default="main", min_length=1, max_length=255)
    subdirectory: str = Field(default="", max_length=512)
    connectionId: str | None = Field(default=None, min_length=1, max_length=64)


class AnalysisIntentRequest(StrictModel):
    # Public placement is intentionally coarse. ``home`` is an internal Luma
    # topology label and is never a tenant-selectable LAE deployment region.
    region: Literal["cn", "global"]
    publicProtocols: list[Literal["http"]] = Field(min_length=1, max_length=1)


class AnalysisCreateRequest(StrictModel):
    applicationId: str = Field(min_length=1, max_length=64)
    source: Annotated[
        GitAnalysisSourceRequest | UploadAnalysisSourceRequest,
        Field(discriminator="type"),
    ]
    intent: AnalysisIntentRequest


@dataclass(frozen=True, slots=True)
class AuthResolution:
    selected: AuthenticatedPrincipal
    session: SessionPrincipal | None
    deploy_token: DeployTokenPrincipal | None


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", new_id("req"))


def _error_body(request: Request, error: ApiError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "requestId": _request_id(request),
            "retryable": error.retryable,
            "details": error.details,
        }
    }


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


def _client_ip(request: Request) -> str | None:
    # Forwarded headers are intentionally ignored unless a trusted proxy layer
    # canonicalizes them in a future deployment adapter.
    return request.client.host if request.client is not None else None


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _deploy_token_body(record: DeployTokenRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "prefix": record.prefix,
        "scopes": list(record.scopes),
        "purpose": record.purpose,
        "isDefault": record.is_default,
        "expiresAt": _timestamp(record.expires_at),
        "revokedAt": _timestamp(record.revoked_at),
        "lastUsedAt": _timestamp(record.last_used_at),
        "lastUsedIp": record.last_used_ip,
        "createdAt": _timestamp(record.created_at),
    }


def _principal_body(principal: AuthenticatedPrincipal) -> dict[str, Any]:
    return {
        "user": {
            "id": principal.user_id,
            "email": principal.email,
            "status": "active",
        },
        "tenant": {"id": principal.tenant_id, "type": "personal"},
        "entitlement": {"plan": principal.entitlement_code},
        "credential": {
            "type": principal.credential_type,
            "scopes": sorted(principal.scopes),
        },
    }


def require_scope(principal: AuthenticatedPrincipal, scope: str) -> None:
    """Apply an explicit tenant-operation scope at every protected route."""

    if scope not in principal.scopes:
        raise ApiError(403, "LAE_FORBIDDEN", "The credential lacks the required scope")


def _session_response(completion: AuthCompletion) -> JSONResponse:
    body: dict[str, Any] = {
        "user": {
            "id": completion.user_id,
            "email": completion.email,
            "status": "active",
        },
        "tenant": {"id": completion.tenant_id, "type": "personal"},
        "entitlement": {"plan": completion.entitlement_code},
    }
    if completion.default_deploy_token is not None:
        body["defaultDeployToken"] = completion.default_deploy_token
    response = JSONResponse(body, status_code=200)
    max_age = max(
        0,
        int(
            (
                completion.session_expires_at
                - datetime.now(completion.session_expires_at.tzinfo)
            ).total_seconds()
        ),
    )
    response.set_cookie(
        SESSION_COOKIE,
        completion.session_token,
        max_age=max_age,
        expires=completion.session_expires_at,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        completion.csrf_token,
        max_age=max_age,
        expires=completion.session_expires_at,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )
    _no_store(response)
    return response


def _decode_hmac_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AuthConfigurationError("auth HMAC key is not valid base64") from exc
    if len(key) < 32:
        raise AuthConfigurationError("auth HMAC key must contain at least 256 bits")
    return key


def _key_ring_from_env() -> AuthKeyRing:
    current = int(os.environ.get("LAE_AUTH_HMAC_KEY_VERSION", "1"))
    encoded_ring = os.environ.get("LAE_AUTH_HMAC_KEYS")
    if encoded_ring:
        try:
            raw = json.loads(encoded_ring)
            keys = {int(version): _decode_hmac_key(value) for version, value in raw.items()}
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthConfigurationError("auth HMAC key ring is invalid") from exc
    else:
        encoded_key = os.environ.get("LAE_AUTH_HMAC_KEY")
        if not encoded_key:
            raise AuthConfigurationError("auth HMAC key is not configured")
        keys = {current: _decode_hmac_key(encoded_key)}
    return AuthKeyRing(current_version=current, keys=keys)


def _runtime_auth_service() -> tuple[AuthService, Any]:
    dsn = os.environ.get("LAE_DATABASE_URL", "")
    if not dsn:
        raise AuthConfigurationError("LAE_DATABASE_URL is not configured")
    environment = os.environ.get("LAE_ENVIRONMENT", "development").strip().lower()
    driver = os.environ.get("LAE_EMAIL_DRIVER", "console").strip().lower()
    external_mailbox_value = os.environ.get("LAE_AUTH_EXTERNAL_MAILBOX")
    if external_mailbox_value is None:
        external_mailbox_enabled = environment in {"production", "prod"}
    else:
        normalized_external_mailbox = external_mailbox_value.strip().lower()
        if normalized_external_mailbox in {"1", "true"}:
            external_mailbox_enabled = True
        elif normalized_external_mailbox in {"0", "false"}:
            external_mailbox_enabled = False
        else:
            raise AuthConfigurationError(
                "LAE_AUTH_EXTERNAL_MAILBOX must be 0/1/false/true"
            )
    if driver == "console":
        if environment in {"production", "prod"}:
            raise AuthConfigurationError(
                "the console email adapter is forbidden in production"
            )
        email_sender = ConsoleEmailSender()
    elif driver == "smtp":
        try:
            email_sender = SmtpEmailSender(
                smtp_config_from_env(os.environ, environment=environment)
            )
        except EmailConfigurationError as exc:
            raise AuthConfigurationError("SMTP email configuration is invalid") from exc
    else:
        raise AuthConfigurationError("email driver is not supported")
    if external_mailbox_enabled and driver != "smtp":
        raise AuthConfigurationError(
            "external mailbox delivery requires the SMTP adapter"
        )
    key_ring = _key_ring_from_env()
    engine = create_postgres_engine(dsn)
    backend = PostgresAuthStore(create_session_factory(engine), key_ring)
    preview_email = preview_email_from_env(os.environ, environment=environment)
    return (
        AuthService(
            backend,
            email_sender,
            preview_email=preview_email,
            external_mailbox_enabled=external_mailbox_enabled,
        ),
        engine,
    )


def _runtime_analysis_request_store(
    engine: Any,
    *,
    connection_key_ring: Any | None = None,
) -> PostgresAnalysisRequestStore:
    encoded_key = os.environ.get("LAE_WORKER_STATE_HMAC_KEY", "")
    if not encoded_key:
        raise AuthConfigurationError("analysis request HMAC key is not configured")
    cluster_id = os.environ.get("LAE_LUMA_CLUSTER_ID", "")
    principal_id = os.environ.get("LAE_LUMA_SERVICE_PRINCIPAL_ID", "")
    if not cluster_id or not principal_id:
        raise AuthConfigurationError("analysis builder binding is not configured")
    try:
        lease_seconds = int(os.environ.get("LAE_SOURCE_LEASE_TTL_SECONDS", "900"))
        key_version = int(os.environ.get("LAE_WORKER_STATE_HMAC_KEY_VERSION", "1"))
    except ValueError as exc:
        raise AuthConfigurationError("analysis request configuration is invalid") from exc
    return PostgresAnalysisRequestStore(
        create_session_factory(engine),
        luma_cluster_id=cluster_id,
        luma_principal_id=principal_id,
        hash_key=_decode_hmac_key(encoded_key),
        hash_key_version=key_version,
        credential_lease_ttl=timedelta(seconds=lease_seconds),
        connection_key_ring=connection_key_ring,
    )


def create_app(
    auth_service: AuthService | None = None,
    analysis_requests: Any | None = None,
    public_resources: Any | None = None,
    billing_runtime: BillingRuntime | None = None,
    billing_environment: str | None = None,
    applications: Any | None = None,
    application_lifecycle: Any | None = None,
    deployments: Any | None = None,
    source_connections: Any | None = None,
    uploads: Any | None = None,
    upload_analyses: Any | None = None,
    credential_broker: Any | None = None,
    object_source_broker: Any | None = None,
    observability: Any | None = None,
    admin_authenticator: Any | None = None,
    admin_store: Any | None = None,
    billing_driver: str | None = None,
    cors_allowed_origins: Sequence[str] | str | None = None,
) -> FastAPI:
    environment = (
        billing_environment or os.environ.get("LAE_ENVIRONMENT", "development")
    ).strip().lower()
    selected_billing_driver = (
        billing_driver
        or os.environ.get("LAE_BILLING_DRIVER")
        or (
            "mock"
            if billing_runtime is not None and environment not in {"prod", "production"}
            else "disabled"
        )
    ).strip().lower()
    runtime: dict[str, Any] = {
        "auth": auth_service,
        "analysis_requests": analysis_requests,
        "public_resources": public_resources,
        "applications": applications,
        "application_lifecycle": application_lifecycle,
        "application_lifecycle_error": None,
        "deployments": deployments,
        "source_connections": source_connections,
        "source_connections_error": None,
        "uploads": uploads,
        "upload_analyses": upload_analyses,
        "uploads_error": None,
        "credential_broker": credential_broker,
        "credential_broker_error": None,
        "object_source_broker": object_source_broker,
        "object_source_broker_error": None,
        "observability": observability,
        "observability_error": None,
        "admin_authenticator": admin_authenticator,
        "admin_store": admin_store,
        "admin_error": None,
        "billing": billing_runtime,
        "billing_driver": selected_billing_driver,
        "billing_error": None,
        "engine": None,
        "error": None,
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if runtime["auth"] is None:
            try:
                runtime["auth"], runtime["engine"] = _runtime_auth_service()
            except Exception:
                # Readiness reports the unavailable state. Configuration errors
                # are deliberately not expanded into logs or public responses.
                runtime["error"] = "identity unavailable"
        if runtime["engine"] is not None and runtime["source_connections"] is None:
            try:
                runtime["source_connections"] = source_connection_service_from_env(
                    create_session_factory(runtime["engine"])
                )
            except Exception:
                # Private Git is optional. Anonymous HTTPS Git remains ready
                # while connection CRUD and connection-backed analysis 503.
                runtime["source_connections_error"] = "source connections unavailable"
        if runtime["engine"] is not None and runtime["analysis_requests"] is None:
            try:
                runtime["analysis_requests"] = _runtime_analysis_request_store(
                    runtime["engine"],
                    connection_key_ring=getattr(
                        runtime["source_connections"], "key_ring", None
                    ),
                )
            except Exception:
                runtime["error"] = "analysis unavailable"
        if runtime["engine"] is not None and runtime["public_resources"] is None:
            runtime["public_resources"] = PostgresPublicResourceStore(
                create_session_factory(runtime["engine"])
            )
        if runtime["engine"] is not None and runtime["applications"] is None:
            try:
                runtime["applications"] = application_service_from_env(
                    create_session_factory(runtime["engine"])
                )
            except Exception:
                runtime["error"] = "application catalog unavailable"
        if (
            runtime["engine"] is not None
            and runtime["application_lifecycle"] is None
        ):
            try:
                runtime["application_lifecycle"] = (
                    application_lifecycle_service_from_env(
                        create_session_factory(runtime["engine"]),
                        connection_key_ring=getattr(
                            runtime["source_connections"], "key_ring", None
                        ),
                    )
                )
            except Exception:
                # Lifecycle admission is capability-scoped. Its failure must
                # not take identity, app catalog, or analysis offline.
                runtime["application_lifecycle_error"] = (
                    "application lifecycle unavailable"
                )
        if runtime["engine"] is not None and runtime["deployments"] is None:
            try:
                runtime["deployments"] = deployment_service_from_env(
                    create_session_factory(runtime["engine"]),
                    resolver=deployment_plan_resolver_from_env(),
                    environment_writer=runtime["applications"],
                )
            except Exception:
                # Deployment admission is capability-scoped until its verified
                # object-store PlanResolver is configured. Other product paths
                # remain ready; deployment endpoints fail closed with 503.
                runtime["error"] = "deployment admission unavailable"
        if runtime["engine"] is not None and runtime["uploads"] is None:
            try:
                runtime["uploads"] = upload_service_from_env(
                    create_session_factory(runtime["engine"]),
                    environment=environment,
                )
            except Exception:
                runtime["uploads_error"] = "upload capability unavailable"
        if runtime["engine"] is not None and runtime["upload_analyses"] is None:
            try:
                runtime["upload_analyses"] = upload_analysis_store_from_env(
                    create_session_factory(runtime["engine"])
                )
            except Exception:
                # Upload analysis requires a real task-bound Luma redemption
                # broker. Upload reservation/status remain separately usable.
                runtime["uploads_error"] = "upload analysis unavailable"
        if runtime["engine"] is not None and runtime["credential_broker"] is None:
            try:
                runtime["credential_broker"] = credential_broker_runtime_from_env(
                    create_session_factory(runtime["engine"]),
                    connection_key_ring=getattr(
                        runtime["source_connections"], "key_ring", None
                    ),
                    environment=environment,
                )
            except Exception:
                # This endpoint is service-principal-only and independently
                # fail-closed. Public product routes remain available.
                runtime["credential_broker_error"] = (
                    "credential broker unavailable"
                )
        if (
            runtime["engine"] is not None
            and runtime["object_source_broker"] is None
        ):
            try:
                runtime["object_source_broker"] = (
                    object_source_broker_runtime_from_env(
                        create_session_factory(runtime["engine"]),
                        environment=environment,
                    )
                )
            except Exception:
                # Upload reservation/scanning remain independent. Only the
                # internal Builder redemption endpoint fails closed.
                runtime["object_source_broker_error"] = (
                    "object source broker unavailable"
                )
        if runtime["engine"] is not None and runtime["observability"] is None:
            try:
                runtime["observability"] = observability_service_from_env(
                    create_session_factory(runtime["engine"])
                )
            except Exception:
                runtime["observability_error"] = "observability unavailable"
        if runtime["engine"] is not None and runtime["admin_store"] is None:
            runtime["admin_store"] = PostgresAdminReadStore(
                create_session_factory(runtime["engine"])
            )
        if runtime["admin_authenticator"] is None:
            try:
                runtime["admin_authenticator"] = admin_authenticator_from_env()
            except Exception:
                # Admin is an independent internal capability and never gates
                # identity, tenant routes, or ordinary readiness.
                runtime["admin_error"] = "admin API unavailable"
        if (
            runtime["engine"] is not None
            and runtime["billing"] is None
            and runtime["billing_driver"] == "mock"
        ):
            try:
                runtime["billing"] = billing_runtime_from_env(
                    runtime["engine"], environment=environment
                )
            except Exception:
                # Billing is a capability-scoped dependency. Identity, Lite,
                # apps and analysis remain ready while every billing endpoint
                # fails closed with 503.
                runtime["billing_error"] = "billing unavailable"
        elif runtime["billing_driver"] not in {"disabled", "mock"}:
            runtime["billing_error"] = "billing driver unsupported"
        yield
        if runtime["engine"] is not None:
            await runtime["engine"].dispose()

    app = FastAPI(
        title="Luma Application Engine API",
        version=VERSION,
        lifespan=lifespan,
    )
    allowed_origins = _cors_allowed_origins(cors_allowed_origins)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=_CORS_METHODS,
            allow_headers=_CORS_HEADERS,
            expose_headers=["X-Request-Id"],
            max_age=600,
        )

    def auth() -> AuthService:
        service = runtime["auth"]
        if service is None:
            raise ApiError(
                503,
                "LAE_IDENTITY_UNAVAILABLE",
                "Identity service is temporarily unavailable",
                retryable=True,
            )
        return service

    def analysis_request_store() -> Any:
        store = runtime["analysis_requests"]
        if store is None:
            raise ApiError(
                503,
                "LAE_ANALYSIS_UNAVAILABLE",
                "Analysis service is temporarily unavailable",
                retryable=True,
            )
        return store

    def public_resource_store() -> Any:
        store = runtime["public_resources"]
        if store is None:
            raise ApiError(
                503,
                "LAE_RESOURCE_VIEW_UNAVAILABLE",
                "Resource view is temporarily unavailable",
                retryable=True,
            )
        return store

    def application_service() -> Any:
        service = runtime["applications"]
        if service is None:
            raise ApiError(
                503,
                "LAE_APPLICATION_CATALOG_UNAVAILABLE",
                "Application catalog is temporarily unavailable",
                retryable=True,
            )
        return service

    def deployment_service() -> Any:
        service = runtime["deployments"]
        if service is None:
            raise ApiError(
                503,
                "LAE_DEPLOYMENT_UNAVAILABLE",
                "Deployment service is temporarily unavailable",
                retryable=True,
            )
        return service

    def application_lifecycle_service() -> Any:
        service = runtime["application_lifecycle"]
        if service is None:
            raise ApiError(
                503,
                "LAE_APPLICATION_LIFECYCLE_UNAVAILABLE",
                "Application lifecycle service is temporarily unavailable",
                retryable=True,
            )
        return service

    def credential_broker_runtime() -> Any | None:
        return runtime["credential_broker"]

    def object_source_broker_runtime() -> Any | None:
        return runtime["object_source_broker"]

    def observability_service() -> Any:
        service = runtime["observability"]
        if service is None:
            raise ApiError(
                503,
                "LAE_OBSERVABILITY_UNAVAILABLE",
                "Application observability is temporarily unavailable",
                retryable=True,
            )
        return service

    def admin_authenticator_service() -> Any:
        service = runtime["admin_authenticator"]
        if service is None:
            raise ApiError(
                503,
                "LAE_ADMIN_UNAVAILABLE",
                "Admin service is temporarily unavailable",
                retryable=True,
            )
        return service

    def admin_read_store() -> Any:
        store = runtime["admin_store"]
        if store is None:
            raise ApiError(
                503,
                "LAE_ADMIN_UNAVAILABLE",
                "Admin service is temporarily unavailable",
                retryable=True,
            )
        return store

    def source_connection_service() -> Any:
        service = runtime["source_connections"]
        if service is None:
            raise ApiError(
                503,
                "LAE_SOURCE_CONNECTIONS_UNAVAILABLE",
                "Source connection service is temporarily unavailable",
                retryable=True,
            )
        return service

    def upload_service() -> Any:
        service = runtime["uploads"]
        if service is None:
            raise ApiError(
                503,
                "LAE_UPLOAD_UNAVAILABLE",
                "Static upload service is temporarily unavailable",
                retryable=True,
            )
        return service

    def upload_analysis_store() -> Any:
        store = runtime["upload_analyses"]
        if store is None:
            raise ApiError(
                503,
                "LAE_UPLOAD_ANALYSIS_UNAVAILABLE",
                "Static upload analysis is temporarily unavailable",
                retryable=True,
            )
        return store

    def billing() -> BillingRuntime:
        service = runtime["billing"]
        if service is None:
            raise BillingHttpError(
                503,
                "LAE_BILLING_UNAVAILABLE",
                "Billing service is temporarily unavailable",
                retryable=True,
            )
        return service

    def bearer_credential(request: Request) -> str | None:
        values = request.headers.getlist("authorization")
        if not values:
            return None
        if len(values) != 1:
            raise AuthRejected("authentication failed")
        match = _BEARER.fullmatch(values[0])
        if match is None:
            raise AuthRejected("authentication failed")
        return match.group("token")

    async def resolve_principal(request: Request) -> AuthResolution:
        cookie_present = SESSION_COOKIE in request.cookies
        try:
            bearer_token = bearer_credential(request)
            bearer_present = bearer_token is not None
            if not cookie_present and not bearer_present:
                raise AuthRejected("authentication failed")
            session_principal = (
                await auth().authenticate(request.cookies.get(SESSION_COOKIE))
                if cookie_present
                else None
            )
            token_principal = (
                await auth().authenticate_deploy_token(
                    bearer_token,
                    request_ip=_client_ip(request),
                )
                if bearer_present
                else None
            )
            if (
                session_principal is not None
                and token_principal is not None
                and (
                    session_principal.user_id != token_principal.user_id
                    or session_principal.tenant_id != token_principal.tenant_id
                )
            ):
                raise AuthRejected("authentication failed")
        except AuthRejected as exc:
            raise ApiError(
                401,
                "LAE_UNAUTHENTICATED",
                "Authentication is required",
            ) from exc
        # Cookie authority wins only after both credentials independently
        # authenticate to the same user and tenant. Therefore a browser cannot
        # add a Bearer header to bypass CSRF on a cookie-authenticated mutation.
        selected = session_principal or token_principal
        assert selected is not None
        return AuthResolution(selected, session_principal, token_principal)

    async def require_session(
        request: Request,
        *,
        csrf_header: str | None = None,
        mutation: bool,
    ) -> SessionPrincipal:
        resolution = await resolve_principal(request)
        principal = resolution.session
        if principal is None:
            raise ApiError(
                401,
                "LAE_UNAUTHENTICATED",
                "Authentication is required",
            )
        if mutation and not auth().csrf_valid(
            principal,
            csrf_cookie=request.cookies.get(CSRF_COOKIE),
            csrf_header=csrf_header,
        ):
            raise ApiError(403, "LAE_CSRF_FAILED", "CSRF validation failed")
        return principal

    async def require_scoped_principal(
        request: Request,
        scope: str,
        *,
        csrf_header: str | None = None,
        mutation: bool,
    ) -> AuthenticatedPrincipal:
        """Shared guard for analysis, deployment, app and log routes.

        Bearer mutations are authorized exclusively by their stored scope and
        never require browser CSRF state. A selected cookie session must pass
        double-submit CSRF for mutations, including when the same user's Bearer
        credential is also present.
        """

        resolution = await resolve_principal(request)
        principal = resolution.selected
        require_scope(principal, scope)
        if (
            mutation
            and isinstance(principal, SessionPrincipal)
            and not auth().csrf_valid(
                principal,
                csrf_cookie=request.cookies.get(CSRF_COOKIE),
                csrf_header=csrf_header,
            )
        ):
            raise ApiError(403, "LAE_CSRF_FAILED", "CSRF validation failed")
        return principal

    async def require_operation_principal(
        request: Request,
        operation_id: str,
        *,
        csrf_header: str | None = None,
        mutation: bool,
    ) -> tuple[AuthenticatedPrincipal, PublicOperationRecord]:
        """Resolve an operation inside the principal tenant, then guard its kind.

        The tenant predicate runs before the kind-to-scope decision, making a
        foreign operation indistinguishable from a nonexistent one. Unknown
        operation kinds fail closed until an explicit public scope is assigned.
        """

        resolution = await resolve_principal(request)
        principal = resolution.selected
        try:
            operation: PublicOperationRecord = (
                await public_resource_store().get_operation(
                    TenantScope(principal.tenant_id), operation_id
                )
            )
            required_scope = required_scope_for_operation(operation.kind)
        except (ResourceNotFound, ValueError) as exc:
            raise ApiError(404, "LAE_NOT_FOUND", "Operation not found") from exc
        require_scope(principal, required_scope)
        if (
            mutation
            and isinstance(principal, SessionPrincipal)
            and not auth().csrf_valid(
                principal,
                csrf_cookie=request.cookies.get(CSRF_COOKIE),
                csrf_header=csrf_header,
            )
        ):
            raise ApiError(403, "LAE_CSRF_FAILED", "CSRF validation failed")
        return principal, operation

    # Future route modules receive the same guard rather than reimplementing
    # cookie/Bearer precedence or CSRF distinctions.
    app.state.require_scoped_principal = require_scoped_principal
    app.state.require_operation_principal = require_operation_principal
    app.include_router(
        create_billing_router(
            billing,
            environment=environment,
            mock_enabled=selected_billing_driver == "mock",
        )
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        supplied = request.headers.get("X-Request-Id", "")
        request.state.request_id = (
            supplied
            if _REQUEST_ID.fullmatch(supplied)
            and _CREDENTIAL_LIKE.search(supplied) is None
            else new_id("req")
        )
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        response = JSONResponse(_error_body(request, error), status_code=error.status)
        _no_store(response)
        return response

    @app.exception_handler(BillingHttpError)
    async def handle_billing_error(
        request: Request, error: BillingHttpError
    ) -> JSONResponse:
        api_error = ApiError(
            error.status,
            error.code,
            error.message,
            retryable=error.retryable,
        )
        response = JSONResponse(
            _error_body(request, api_error), status_code=error.status
        )
        _no_store(response)
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, validation_error: RequestValidationError
    ) -> JSONResponse:
        if request.url.path in {
            "/v1/auth/register",
            "/v1/auth/login/request",
            "/v1/auth/email/resend",
        }:
            response = JSONResponse({"accepted": True}, status_code=202)
            _no_store(response)
            return response
        if request.url.path == "/v1/analyses" and any(
            error.get("type") == "missing"
            and tuple(error.get("loc", ())) == ("body", "applicationId")
            for error in validation_error.errors()
        ):
            error = ApiError(
                422,
                "LAE_APPLICATION_REQUIRED",
                "An existing applicationId is required for analysis",
            )
            response = JSONResponse(_error_body(request, error), status_code=422)
            _no_store(response)
            return response
        error = ApiError(
            400,
            "LAE_INVALID_ARGUMENT",
            "Request body is invalid",
            details={},
        )
        response = JSONResponse(_error_body(request, error), status_code=400)
        _no_store(response)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        code = "LAE_NOT_FOUND" if error.status_code == 404 else "LAE_METHOD_NOT_ALLOWED"
        message = "Route not found" if error.status_code == 404 else "Method not allowed"
        api_error = ApiError(error.status_code, code, message)
        response = JSONResponse(
            _error_body(request, api_error), status_code=error.status_code
        )
        _no_store(response)
        return response

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, _error: Exception) -> JSONResponse:
        error = ApiError(
            500,
            "LAE_INTERNAL",
            "An internal error occurred",
            retryable=True,
        )
        response = JSONResponse(_error_body(request, error), status_code=500)
        _no_store(response)
        return response

    @app.get("/health/live")
    async def live() -> dict[str, Any]:
        return component_payload("lae-api")

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        ready_now = (
            runtime["auth"] is not None
            and runtime["analysis_requests"] is not None
            and runtime["public_resources"] is not None
            and runtime["applications"] is not None
        )
        return JSONResponse(
            component_payload("lae-api", status="ok" if ready_now else "unavailable"),
            status_code=200 if ready_now else 503,
        )

    @app.get("/version")
    async def version() -> dict[str, Any]:
        return component_payload("lae-api", status="version")

    @app.get("/v1/auth/config")
    async def auth_config() -> JSONResponse:
        service = runtime["auth"]
        preview_access = service is not None and bool(
            getattr(service, "preview_enabled", False)
        )
        external_mailbox = service is not None and bool(
            getattr(service, "external_mailbox_enabled", True)
        )
        mode = (
            "email"
            if external_mailbox
            else "preview"
            if preview_access
            else "unavailable"
        )
        response = JSONResponse(
            {
                "emailDelivery": {
                    "mode": mode,
                    "externalMailbox": external_mailbox,
                    "previewAccess": preview_access,
                }
            }
        )
        _no_store(response)
        return response

    async def start_flow(
        request: Request, payload: StartRequest, purpose: str
    ) -> JSONResponse:
        service = runtime["auth"]
        if service is not None:
            await service.start(
                email=payload.email,
                purpose=purpose,
                request_ip=_client_ip(request),
                device_id=request.headers.get("X-LAE-Device-Id"),
            )
        response = JSONResponse({"accepted": True}, status_code=202)
        _no_store(response)
        return response

    @app.post("/v1/auth/register", status_code=202)
    async def register(request: Request, payload: StartRequest) -> JSONResponse:
        return await start_flow(request, payload, "register")

    @app.post("/v1/auth/login/request", status_code=202)
    async def login_request(request: Request, payload: StartRequest) -> JSONResponse:
        return await start_flow(request, payload, "login")

    @app.post("/v1/auth/email/resend", status_code=202)
    async def resend(request: Request, payload: ResendRequest) -> JSONResponse:
        return await start_flow(request, payload, payload.purpose)

    @app.post("/v1/auth/preview", status_code=201)
    async def preview_auth(request: Request) -> JSONResponse:
        try:
            delivery = await auth().request_preview_challenge(
                request_ip=_client_ip(request),
                device_id=request.headers.get("X-LAE-Device-Id"),
            )
        except PreviewAuthDisabled as exc:
            raise ApiError(
                404,
                "LAE_AUTH_PREVIEW_DISABLED",
                "Preview authentication is not enabled",
            ) from exc
        except PreviewAuthUnavailable as exc:
            raise ApiError(
                429,
                "LAE_AUTH_PREVIEW_COOLDOWN",
                "Preview access was just issued; try again shortly",
                retryable=True,
            ) from exc
        response = JSONResponse(
            {
                "email": delivery.email,
                "purpose": delivery.purpose,
                "code": delivery.code,
                "magicToken": delivery.magic_token,
                "expiresAt": _timestamp(delivery.expires_at),
            },
            status_code=201,
        )
        _no_store(response)
        return response

    async def verify_flow(
        request: Request, payload: VerifyRequest, purpose: str
    ) -> JSONResponse:
        try:
            completion = await auth().verify(
                email=payload.email,
                purpose=purpose,
                code=payload.code,
                magic_token=payload.magicToken,
                request_ip=_client_ip(request),
                user_agent=request.headers.get("User-Agent"),
            )
        except AuthRejected as exc:
            raise ApiError(
                401,
                "LAE_AUTH_CHALLENGE_INVALID",
                "The authentication challenge is invalid or expired",
            ) from exc
        except AuthConfigurationError as exc:
            raise ApiError(
                503,
                "LAE_IDENTITY_UNAVAILABLE",
                "Identity service is temporarily unavailable",
                retryable=True,
            ) from exc
        return _session_response(completion)

    @app.post("/v1/auth/email/verify")
    async def verify_registration(request: Request, payload: VerifyRequest) -> JSONResponse:
        return await verify_flow(request, payload, "register")

    @app.post("/v1/auth/login/verify")
    async def verify_login(request: Request, payload: VerifyRequest) -> JSONResponse:
        return await verify_flow(request, payload, "login")

    @app.get("/v1/me")
    async def me(request: Request) -> JSONResponse:
        resolution = await resolve_principal(request)
        response = JSONResponse(_principal_body(resolution.selected))
        _no_store(response)
        return response

    @app.post("/v1/auth/token/verify")
    async def verify_deploy_token(request: Request) -> JSONResponse:
        resolution = await resolve_principal(request)
        principal = resolution.deploy_token
        if principal is None:
            raise ApiError(
                401,
                "LAE_UNAUTHENTICATED",
                "Authentication is required",
            )
        body = _principal_body(principal)
        body["token"] = {
            "id": principal.token_id,
            "prefix": principal.token_prefix,
            "scopes": sorted(principal.scopes),
        }
        response = JSONResponse(body)
        _no_store(response)
        return response

    @app.get("/v1/deploy-tokens")
    async def list_deploy_tokens(request: Request) -> JSONResponse:
        principal = await require_session(request, mutation=False)
        records = await auth().list_deploy_tokens(principal)
        response = JSONResponse(
            {"tokens": [_deploy_token_body(record) for record in records]}
        )
        _no_store(response)
        return response

    @app.post("/v1/analyses", status_code=202)
    async def create_analysis(
        request: Request,
        payload: AnalysisCreateRequest,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        principal = await app.state.require_scoped_principal(
            request,
            "analyses:write",
            csrf_header=x_csrf_token,
            mutation=True,
        )
        if idempotency_key is None:
            raise ApiError(
                400,
                "LAE_IDEMPOTENCY_REQUIRED",
                "Idempotency-Key is required",
            )
        try:
            if isinstance(payload.source, UploadAnalysisSourceRequest):
                created = await create_upload_analysis(
                    upload_analysis_store(),
                    principal=principal,
                    application_id=payload.applicationId,
                    source=payload.source,
                    region=payload.intent.region,
                    public_protocols=tuple(payload.intent.publicProtocols),
                    idempotency_key=idempotency_key,
                )
            else:
                command = CreateAnalysisRequest(
                    scope=TenantScope(principal.tenant_id),
                    principal=Principal(
                        "deploy-token"
                        if principal.credential_type == "deploy_token"
                        else "session",
                        principal.credential_id,
                    ),
                    application_id=payload.applicationId,
                    repository=payload.source.repository,
                    ref=payload.source.ref,
                    subdirectory=payload.source.subdirectory,
                    connection_id=payload.source.connectionId,
                    region=payload.intent.region,
                    public_protocols=tuple(payload.intent.publicProtocols),
                    idempotency_key=idempotency_key,
                )
                created = await analysis_request_store().create(command)
        except IdempotencyKeyReused as exc:
            raise ApiError(
                409,
                "LAE_IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
            ) from exc
        except ResourceNotFound as exc:
            raise ApiError(
                404,
                "LAE_NOT_FOUND",
                "Application or source not found",
            ) from exc
        except SourceConnectionUnavailable as exc:
            raise ApiError(
                503,
                "LAE_SOURCE_CONNECTIONS_UNAVAILABLE",
                "Source connection service is temporarily unavailable",
                retryable=True,
            ) from exc
        except SourceConnectionHostMismatch as exc:
            raise ApiError(
                400,
                "LAE_SOURCE_CONNECTION_HOST_MISMATCH",
                "Repository host does not match the selected source connection",
            ) from exc
        except OperationConflict as exc:
            raise ApiError(
                409,
                "LAE_ANALYSIS_CONFLICT",
                "Analysis request conflicts with current state",
            ) from exc
        except ValueError as exc:
            raise ApiError(
                400,
                "LAE_UNSUPPORTED_SOURCE",
                "Analysis source request is not supported",
            ) from exc
        response = JSONResponse(created.public_body(), status_code=202)
        response.headers["Idempotency-Replayed"] = (
            "true" if created.replayed else "false"
        )
        _no_store(response)
        return response

    @app.get("/v1/analyses/{analysis_id}")
    async def get_analysis(analysis_id: str, request: Request) -> JSONResponse:
        # Scope debt: analyses:read does not exist in the v1 deploy-token
        # vocabulary yet. Reuse analyses:write until a backward-compatible
        # read-only scope and token migration policy are introduced.
        principal = await app.state.require_scoped_principal(
            request,
            "analyses:write",
            mutation=False,
        )
        try:
            analysis: PublicAnalysisRecord = await public_resource_store().get_analysis(
                TenantScope(principal.tenant_id), analysis_id
            )
        except (ResourceNotFound, ValueError) as exc:
            raise ApiError(404, "LAE_NOT_FOUND", "Analysis not found") from exc
        response = JSONResponse(analysis.public_body())
        _no_store(response)
        return response

    @app.get("/v1/operations")
    async def list_operations(
        request: Request,
        application_id: Annotated[
            str | None,
            Query(alias="applicationId", min_length=1, max_length=64),
        ] = None,
        kind: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        before: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        resolution = await resolve_principal(request)
        principal = resolution.selected
        allowed_scopes = frozenset(principal.scopes) & _PUBLIC_OPERATION_LIST_SCOPES
        if kind is not None:
            try:
                required_scope = required_scope_for_operation(kind)
            except ResourceNotFound as exc:
                raise ApiError(
                    400,
                    "LAE_INVALID_ARGUMENT",
                    "Operation kind is invalid",
                ) from exc
            require_scope(principal, required_scope)
            allowed_scopes = frozenset({required_scope})
        elif not allowed_scopes:
            raise ApiError(
                403,
                "LAE_FORBIDDEN",
                "The credential lacks an operation history scope",
            )
        try:
            page: PublicOperationPage = (
                await public_resource_store().list_operations(
                    TenantScope(principal.tenant_id),
                    allowed_scopes=allowed_scopes,
                    application_id=application_id,
                    kind=kind,
                    before=before,
                    limit=limit,
                )
            )
        except (ResourceNotFound, ValueError) as exc:
            raise ApiError(
                400,
                "LAE_INVALID_ARGUMENT",
                "Operation history query is invalid",
            ) from exc
        response = JSONResponse(page.public_body())
        _no_store(response)
        return response

    @app.get("/v1/operations/{operation_id}")
    async def get_operation(operation_id: str, request: Request) -> JSONResponse:
        _principal, operation = await app.state.require_operation_principal(
            request,
            operation_id,
            mutation=False,
        )
        response = JSONResponse(operation.public_body())
        _no_store(response)
        return response

    @app.get("/v1/operations/{operation_id}/events")
    async def list_operation_events(
        operation_id: str,
        request: Request,
        after: int = 0,
        limit: int = 100,
    ) -> JSONResponse:
        principal, _operation = await app.state.require_operation_principal(
            request,
            operation_id,
            mutation=False,
        )
        try:
            page: PublicOperationEventPage = (
                await public_resource_store().list_operation_events(
                    TenantScope(principal.tenant_id),
                    operation_id,
                    after=after,
                    limit=limit,
                )
            )
        except ResourceNotFound as exc:
            raise ApiError(404, "LAE_NOT_FOUND", "Operation not found") from exc
        except ValueError as exc:
            raise ApiError(
                400,
                "LAE_INVALID_ARGUMENT",
                "Operation event cursor or limit is invalid",
            ) from exc
        response = JSONResponse(page.public_body())
        _no_store(response)
        return response

    @app.post("/v1/operations/{operation_id}/cancel", status_code=202)
    async def cancel_operation(
        operation_id: str,
        request: Request,
        x_csrf_token: Annotated[
            str | None, Header(alias="X-CSRF-Token")
        ] = None,
    ) -> JSONResponse:
        principal, _operation = await app.state.require_operation_principal(
            request,
            operation_id,
            csrf_header=x_csrf_token,
            mutation=True,
        )
        try:
            canceled: PublicOperationRecord = (
                await public_resource_store().request_cancel(
                    TenantScope(principal.tenant_id), operation_id
                )
            )
        except (ResourceNotFound, ValueError) as exc:
            raise ApiError(404, "LAE_NOT_FOUND", "Operation not found") from exc
        response = JSONResponse(canceled.public_body(), status_code=202)
        # Cancellation is a row-locked state transition: replaying the same
        # request returns the same current state and appends no duplicate event.
        response.headers["Idempotency-Policy"] = "state-transition"
        _no_store(response)
        return response

    @app.post("/v1/deploy-tokens", status_code=201)
    async def create_deploy_token(
        request: Request,
        payload: DeployTokenCreateRequest,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        principal = await require_session(
            request,
            csrf_header=x_csrf_token,
            mutation=True,
        )
        try:
            issued = await auth().create_deploy_token(
                principal,
                name=payload.name,
                scopes=tuple(payload.scopes),
                expires_at=payload.expiresAt,
            )
        except ValueError as exc:
            raise ApiError(
                400,
                "LAE_INVALID_ARGUMENT",
                "Deploy token request is invalid",
            ) from exc
        except DeployTokenConflict as exc:
            raise ApiError(
                409,
                "LAE_DEPLOY_TOKEN_LIMIT",
                "The active deploy token limit has been reached",
            ) from exc
        response = JSONResponse(
            {
                "token": _deploy_token_body(issued.record),
                "plaintext": issued.plaintext,
            },
            status_code=201,
        )
        _no_store(response)
        return response

    @app.post("/v1/deploy-tokens/{token_id}/rotate")
    async def rotate_deploy_token(
        token_id: str,
        request: Request,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> JSONResponse:
        principal = await require_session(
            request,
            csrf_header=x_csrf_token,
            mutation=True,
        )
        try:
            issued = await auth().rotate_deploy_token(principal, token_id)
        except DeployTokenNotFound as exc:
            raise ApiError(
                404,
                "LAE_DEPLOY_TOKEN_NOT_FOUND",
                "Deploy token not found",
            ) from exc
        except DeployTokenConflict as exc:
            raise ApiError(
                409,
                "LAE_DEPLOY_TOKEN_INACTIVE",
                "Only an active deploy token can be rotated",
            ) from exc
        response = JSONResponse(
            {
                "token": _deploy_token_body(issued.record),
                "plaintext": issued.plaintext,
            }
        )
        _no_store(response)
        return response

    @app.delete("/v1/deploy-tokens/{token_id}", status_code=204)
    async def revoke_deploy_token(
        token_id: str,
        request: Request,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> Response:
        principal = await require_session(
            request,
            csrf_header=x_csrf_token,
            mutation=True,
        )
        try:
            await auth().revoke_deploy_token(principal, token_id)
        except DeployTokenNotFound as exc:
            raise ApiError(
                404,
                "LAE_DEPLOY_TOKEN_NOT_FOUND",
                "Deploy token not found",
            ) from exc
        except DeployTokenConflict as exc:
            raise ApiError(
                409,
                "LAE_DEFAULT_DEPLOY_TOKEN_PROTECTED",
                "Rotate the default deploy token instead of revoking it",
            ) from exc
        response = Response(status_code=204)
        _no_store(response)
        return response

    @app.post("/v1/auth/logout", status_code=204)
    async def logout(
        request: Request,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> Response:
        await require_session(
            request,
            csrf_header=x_csrf_token,
            mutation=True,
        )
        try:
            await auth().logout(
                session_token=request.cookies.get(SESSION_COOKIE),
                csrf_cookie=request.cookies.get(CSRF_COOKIE),
                csrf_header=x_csrf_token,
            )
        except CsrfRejected as exc:
            raise ApiError(403, "LAE_CSRF_FAILED", "CSRF validation failed") from exc
        except AuthRejected as exc:
            raise ApiError(401, "LAE_UNAUTHENTICATED", "Authentication is required") from exc
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        response.delete_cookie(
            CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="lax"
        )
        _no_store(response)
        return response

    register_application_routes(app, application_service, ApiError)
    register_application_lifecycle_routes(
        app, application_lifecycle_service, ApiError
    )
    register_deployment_routes(app, deployment_service, ApiError)
    register_source_connection_routes(app, source_connection_service, ApiError)
    register_upload_routes(app, upload_service, ApiError)
    register_credential_broker_route(app, credential_broker_runtime)
    register_object_source_broker_route(app, object_source_broker_runtime)
    register_observability_routes(app, observability_service, ApiError)
    register_admin_routes(
        app,
        admin_authenticator_service,
        admin_read_store,
        ApiError,
    )
    register_template_routes(
        app,
        TemplateApiService(application_service, analysis_request_store),
        ApiError,
    )

    return app


app = create_app()
