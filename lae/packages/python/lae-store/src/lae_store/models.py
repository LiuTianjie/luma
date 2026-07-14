from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    UniqueConstraint,
    and_,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .ids import new_id

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

APPLICATION_MUTATION_KINDS = (
    "deployment.create",
    "application.check-update",
    "application.resume",
    "application.suspend",
    "application.restart",
    "application.rollback",
    "application.delete",
)
ACTIVE_OPERATION_STATUS_VALUES = ("queued", "running")
TERMINAL_OPERATION_STATUS_VALUES = ("succeeded", "failed", "canceled")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TemplateHealth(TimestampMixin, Base):
    __tablename__ = "template_health"
    __table_args__ = (
        CheckConstraint(
            "last_status IN ('unverified','succeeded','failed')",
            name="last_status",
        ),
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
    )

    template_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="unverified"
    )
    last_run_id: Mapped[str | None] = mapped_column(String(80))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_unpublished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('pending','active','suspended')", name="status"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("usr")
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="pending"
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locale: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="zh-CN"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# SQLAlchemy declarative attributes must exist before a functional index can
# reference them.
Index("uq_users_email_lower", func.lower(User.__table__.c.email), unique=True)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_tenants_slug"),
        UniqueConstraint("id", "owner_user_id", name="uq_tenants_id_owner_user_id"),
        CheckConstraint("type IN ('personal','organization')", name="type"),
        CheckConstraint("status IN ('active','suspended','deleted')", name="status"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("ten")
    )
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="active"
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantMember(TimestampMixin, Base):
    __tablename__ = "tenant_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','developer','viewer')", name="role"),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)


class EmailChallenge(TimestampMixin, Base):
    __tablename__ = "email_challenges"
    __table_args__ = (
        CheckConstraint("purpose IN ('register','login')", name="purpose"),
        CheckConstraint("octet_length(code_hash) = 32", name="code_hash_length"),
        CheckConstraint(
            "octet_length(magic_token_hash) = 32", name="magic_token_hash_length"
        ),
        CheckConstraint("key_version > 0", name="key_version_positive"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint(
            "request_ip_hash IS NULL OR octet_length(request_ip_hash) = 32",
            name="request_ip_hash_length",
        ),
        CheckConstraint(
            "device_hash IS NULL OR octet_length(device_hash) = 32",
            name="device_hash_length",
        ),
        CheckConstraint(
            "NOT (used_at IS NOT NULL AND canceled_at IS NOT NULL)",
            name="terminal_once",
        ),
        Index(
            "ix_email_challenges_email_purpose_created",
            "email",
            "purpose",
            "created_at",
        ),
        Index("ix_email_challenges_ip_created", "request_ip_hash", "created_at"),
        Index("ix_email_challenges_device_created", "device_hash", "created_at"),
        Index("ix_email_challenges_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("emc")
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    magic_token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32), nullable=False, unique=True
    )
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    device_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))


class AuthSession(TimestampMixin, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("session_hash", name="uq_auth_sessions_session_hash"),
        CheckConstraint("octet_length(session_hash) = 32", name="session_hash_length"),
        CheckConstraint("key_version > 0", name="key_version_positive"),
        CheckConstraint(
            "csrf_hash IS NULL OR octet_length(csrf_hash) = 32",
            name="csrf_hash_length",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("ses")
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    csrf_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))


class PlanVersion(TimestampMixin, Base):
    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_plan_versions_code_version"),
        CheckConstraint("code IN ('lite','pro','ultra')", name="code"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("jsonb_typeof(limits_json) = 'object'", name="limits_object"),
        CheckConstraint(
            "jsonb_typeof(features_json) = 'object'", name="features_object"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("pln")
    )
    code: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    limits_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("interval IN ('none','monthly','yearly')", name="interval"),
        CheckConstraint(
            "status IN ('active','trialing','past_due','canceled','expired')",
            name="status",
        ),
        Index(
            "uq_subscriptions_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status IN ('active','trialing','past_due')"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("sub")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    plan_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=False
    )
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class DeployToken(TimestampMixin, Base):
    __tablename__ = "deploy_tokens"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_members.tenant_id", "tenant_members.user_id"],
            ondelete="CASCADE",
            name="fk_deploy_tokens_tenant_member",
        ),
        UniqueConstraint("prefix", name="uq_deploy_tokens_prefix"),
        UniqueConstraint("token_hash", name="uq_deploy_tokens_token_hash"),
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
        CheckConstraint("key_version > 0", name="key_version_positive"),
        CheckConstraint("jsonb_typeof(scopes) = 'array'", name="scopes_array"),
        Index(
            "uq_deploy_tokens_active_default",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=and_(
                text("is_default IS TRUE"), text("revoked_at IS NULL")
            ),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("dtk")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(10), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="deploy"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(INET)


class Application(TimestampMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_applications_tenant_slug"),
        UniqueConstraint("luma_name", name="uq_applications_luma_name"),
        UniqueConstraint("tenant_id", "id", name="uq_applications_tenant_id_id"),
        CheckConstraint("kind IN ('pending','service','compose')", name="kind"),
        CheckConstraint(
            "desired_state IN ('running','suspended','deleted')", name="desired_state"
        ),
        CheckConstraint(
            "observed_state IN ('provisioning','running','degraded','failed','suspending','suspended','unknown')",
            name="observed_state",
        ),
        CheckConstraint("environment_version >= 0", name="environment_version"),
        ForeignKeyConstraint(
            ["tenant_id", "current_revision_id"],
            ["app_revisions.tenant_id", "app_revisions.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_applications_tenant_current_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "current_deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_applications_tenant_current_deployment",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("app")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    luma_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="running"
    )
    observed_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="unknown"
    )
    current_revision_id: Mapped[str | None] = mapped_column(String(64))
    current_deployment_id: Mapped[str | None] = mapped_column(String(64))
    environment_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationService(TimestampMixin, Base):
    __tablename__ = "application_services"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "application_id",
            "service_key",
            name="uq_application_services_app_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_application_services_app_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_application_services_tenant_application",
        ),
        CheckConstraint(
            "role IN ('http','internal','worker','datastore')", name="role"
        ),
        CheckConstraint(
            "desired_state IN ('running','suspended','deleted')",
            name="desired_state",
        ),
        CheckConstraint(
            "observed_state IN ('provisioning','running','degraded','failed','suspending','suspended','unknown')",
            name="observed_state",
        ),
        CheckConstraint(
            "current_image_digest IS NULL OR "
            "current_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="current_image_digest",
        ),
        Index("ix_application_services_tenant_app", "tenant_id", "application_id"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("svc")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service_key: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    desired_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="running"
    )
    observed_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="unknown"
    )
    current_image_digest: Mapped[str | None] = mapped_column(String(71))


class ApplicationRoute(TimestampMixin, Base):
    __tablename__ = "application_routes"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_application_routes_hostname"),
        UniqueConstraint(
            "tenant_id", "application_id", "id", name="uq_application_routes_app_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_application_routes_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "service_id"],
            [
                "application_services.tenant_id",
                "application_services.application_id",
                "application_services.id",
            ],
            ondelete="CASCADE",
            name="fk_application_routes_tenant_app_service",
        ),
        CheckConstraint("kind = 'http'", name="kind_http_only"),
        CheckConstraint(
            "hostname ~ '^[0-9a-f]{32}\\.itool\\.tech$'", name="managed_hostname"
        ),
        CheckConstraint("container_port BETWEEN 1 AND 65535", name="container_port"),
        CheckConstraint(
            "status IN ('pending','provisioning','ready','failed','disabled')",
            name="status",
        ),
        Index(
            "uq_application_routes_primary",
            "tenant_id",
            "application_id",
            unique=True,
            postgresql_where=text("is_primary IS TRUE"),
        ),
        Index("ix_application_routes_tenant_app", "tenant_id", "application_id"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("rte")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="http")
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    container_port: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="pending"
    )


class ApplicationVolume(TimestampMixin, Base):
    __tablename__ = "application_volumes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "application_id",
            "volume_key",
            name="uq_application_volumes_app_key",
        ),
        UniqueConstraint(
            "tenant_id", "application_id", "id", name="uq_application_volumes_app_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_application_volumes_tenant_application",
        ),
        CheckConstraint("requested_bytes > 0", name="requested_bytes_positive"),
        CheckConstraint("storage_policy = 'managed'", name="managed_storage_only"),
        CheckConstraint(
            "backup_policy IN ('none','manual','scheduled')", name="backup_policy"
        ),
        CheckConstraint("delete_policy IN ('retain','delete')", name="delete_policy"),
        CheckConstraint(
            "status IN ('pending','provisioning','ready','failed','retained','deleted')",
            name="status",
        ),
        CheckConstraint(
            "luma_volume_ref IS NULL OR "
            "luma_volume_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$'",
            name="luma_volume_ref",
        ),
        CheckConstraint(
            "((luma_volume_ref IS NULL) = (provisioned_at IS NULL)) AND "
            "(status <> 'ready' OR luma_volume_ref IS NOT NULL)",
            name="provisioning_binding",
        ),
        Index("ix_application_volumes_tenant_app", "tenant_id", "application_id"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("vol")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    volume_key: Mapped[str] = mapped_column(String(80), nullable=False)
    requested_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="managed"
    )
    backup_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="none"
    )
    delete_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="retain"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="pending"
    )
    luma_volume_ref: Mapped[str | None] = mapped_column(String(256))
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationEnvironmentVariable(TimestampMixin, Base):
    __tablename__ = "app_environment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_app_environment_tenant_application",
        ),
        CheckConstraint(
            "octet_length(value_ciphertext) BETWEEN 1 AND 1048576",
            name="ciphertext_size",
        ),
        CheckConstraint("octet_length(value_checksum) = 32", name="checksum_length"),
        CheckConstraint("key_version > 0", name="key_version_positive"),
        CheckConstraint(
            "source IN ('user','analysis','template','system')", name="source"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service_scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    value_checksum: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="user"
    )


class Upload(TimestampMixin, Base):
    __tablename__ = "uploads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_uploads_tenant_id_id"),
        UniqueConstraint("object_key", name="uq_uploads_object_key"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_uploads_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_uploads_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_uploads_tenant_source_revision",
        ),
        CheckConstraint("kind IN ('html','zip')", name="kind"),
        CheckConstraint(
            "status IN ('quarantine','verifying','scanning','ready','failed',"
            "'expired','deleting','deleted')",
            name="status",
        ),
        CheckConstraint(
            "cleanup_status IN ('none','pending','deleting','deleted','failed')",
            name="cleanup_status",
        ),
        CheckConstraint(
            "expected_bytes > 0 AND expected_bytes <= 536870912",
            name="expected_bytes_range",
        ),
        CheckConstraint(
            "actual_bytes IS NULL OR (actual_bytes > 0 AND actual_bytes <= 536870912)",
            name="actual_bytes_range",
        ),
        CheckConstraint(
            "expected_sha256 ~ '^sha256:[0-9a-f]{64}$'", name="expected_sha256"
        ),
        CheckConstraint(
            "actual_sha256 IS NULL OR actual_sha256 ~ '^sha256:[0-9a-f]{64}$'",
            name="actual_sha256",
        ),
        CheckConstraint(
            "complete_idempotency_key_hash IS NULL OR "
            "octet_length(complete_idempotency_key_hash) = 32",
            name="complete_idempotency_key_hash_length",
        ),
        CheckConstraint(
            "(scan_lease_owner IS NULL AND scan_lease_expires_at IS NULL) OR "
            "(scan_lease_owner IS NOT NULL AND scan_lease_expires_at IS NOT NULL)",
            name="scan_lease_fields_together",
        ),
        CheckConstraint("cleanup_attempts >= 0", name="cleanup_attempts_nonnegative"),
        CheckConstraint(
            "(status = 'ready') = (source_revision_id IS NOT NULL)",
            name="ready_source_revision",
        ),
        Index("ix_uploads_tenant_app_created", "tenant_id", "application_id", "created_at"),
        Index("ix_uploads_scan_claim", "status", "scan_lease_expires_at", "created_at"),
        Index("ix_uploads_expiry", "status", "expires_at"),
        Index(
            "uq_uploads_source_revision",
            "tenant_id",
            "source_revision_id",
            unique=True,
            postgresql_where=text("source_revision_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("upl")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    expected_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_bytes: Mapped[int | None] = mapped_column(BigInteger)
    expected_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    actual_sha256: Mapped[str | None] = mapped_column(String(71))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="quarantine"
    )
    cleanup_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="none"
    )
    cleanup_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    failure_code: Mapped[str | None] = mapped_column(String(96))
    complete_idempotency_key_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_lease_owner: Mapped[str | None] = mapped_column(String(128))
    scan_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceRevision(TimestampMixin, Base):
    __tablename__ = "source_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_source_revisions_tenant_id_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_source_revisions_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["uploads.tenant_id", "uploads.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_source_revisions_tenant_upload",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            ondelete="RESTRICT",
            name="fk_source_revisions_tenant_connection",
        ),
        CheckConstraint("kind IN ('upload','git','template')", name="kind"),
        CheckConstraint(
            "resolved_commit_full IS NULL OR resolved_commit_full ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="resolved_commit_full",
        ),
        CheckConstraint(
            "snapshot_digest IS NULL OR snapshot_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="snapshot_digest",
        ),
        Index(
            "ix_source_revisions_tenant_snapshot",
            "tenant_id",
            "snapshot_digest",
            postgresql_where=text("snapshot_digest IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("src")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    connection_id: Mapped[str | None] = mapped_column(String(64))
    repository: Mapped[str | None] = mapped_column(String(512))
    ref: Mapped[str | None] = mapped_column(String(512))
    resolved_commit_full: Mapped[str | None] = mapped_column(String(64))
    source_tree_digest: Mapped[str | None] = mapped_column(String(71))
    upload_id: Mapped[str | None] = mapped_column(String(64))
    template_version_id: Mapped[str | None] = mapped_column(String(64))
    subdirectory: Mapped[str] = mapped_column(
        String(512), nullable=False, server_default=""
    )
    snapshot_id: Mapped[str | None] = mapped_column(String(128))
    snapshot_digest: Mapped[str | None] = mapped_column(String(71))
    snapshot_artifact_id: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceConnection(TimestampMixin, Base):
    """Tenant-owned encrypted HTTPS Git credential metadata.

    The authentication secret is always an AES-GCM ciphertext.  Username,
    provider and the exact allowed host are non-secret routing metadata; none
    of them may be supplied by a lease consumer to redirect a credential.
    """

    __tablename__ = "source_connections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_source_connections_tenant_id_id"
        ),
        CheckConstraint("provider IN ('github','gitea','generic')", name="provider"),
        CheckConstraint(
            "octet_length(secret_ciphertext) BETWEEN 16 AND 8192",
            name="secret_ciphertext_size",
        ),
        CheckConstraint("octet_length(secret_nonce) = 12", name="secret_nonce_length"),
        CheckConstraint("octet_length(secret_checksum) = 32", name="secret_checksum_length"),
        CheckConstraint("key_version > 0", name="key_version_positive"),
        CheckConstraint("credential_version > 0", name="credential_version_positive"),
        Index(
            "ix_source_connections_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("conn")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    allowed_host: Mapped[str] = mapped_column(String(260), nullable=False)
    username: Mapped[str | None] = mapped_column(String(256))
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    secret_checksum: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Operation(TimestampMixin, Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_operations_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_operations_tenant_parent",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','canceled')",
            name="status",
        ),
        CheckConstraint("lease_attempt > 0", name="lease_attempt_positive"),
        CheckConstraint("last_event_seq >= 0", name="last_event_seq_nonnegative"),
        CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'", name="result_object"
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL AND lease_heartbeat_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_heartbeat_at IS NOT NULL)",
            name="lease_fields_together",
        ),
        CheckConstraint(
            "status = 'running' OR lease_owner IS NULL",
            name="lease_only_while_running",
        ),
        Index(
            "ix_operations_tenant_status_created", "tenant_id", "status", "created_at"
        ),
        Index(
            "ix_operations_claim",
            "kind",
            "status",
            "lease_expires_at",
            "created_at",
            "id",
        ),
        Index(
            "uq_operations_active_application_mutation",
            "tenant_id",
            "target_id",
            unique=True,
            postgresql_where=and_(
                text("target_type = 'application'"),
                text(
                    "kind IN ('deployment.create','application.check-update','application.resume','application.suspend',"
                    "'application.restart','application.rollback','application.delete')"
                ),
                text("status IN ('queued','running')"),
            ),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("op")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(48), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="queued"
    )
    phase: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(96))
    error_message: Mapped[str | None] = mapped_column(String(512))
    parent_operation_id: Mapped[str | None] = mapped_column(String(64))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )


class OperationEvent(Base):
    __tablename__ = "operation_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_operation_events_tenant_operation",
        ),
        UniqueConstraint("event_id", name="uq_operation_events_event_id"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint("level IN ('debug','info','warning','error')", name="level"),
        CheckConstraint("jsonb_typeof(data) = 'object'", name="data_object"),
        Index("ix_operation_events_tenant_created", "tenant_id", "created_at"),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: new_id("evt")
    )
    type: Mapped[str] = mapped_column(String(96), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="info"
    )
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_idempotency_records_tenant_operation",
        ),
        UniqueConstraint(
            "tenant_id",
            "principal_type",
            "principal_id",
            "method",
            "route_template",
            "key",
            name="uq_idempotency_records_scope",
        ),
        CheckConstraint("octet_length(request_hash) = 32", name="request_hash_length"),
        CheckConstraint(
            "jsonb_typeof(response_body) = 'object'", name="response_body_object"
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("idem")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    route_template: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "dedupe_key", name="uq_outbox_events_tenant_dedupe_key"
        ),
        CheckConstraint(
            "status IN ('pending','publishing','published','dead')", name="status"
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_fields_together",
        ),
        Index("ix_outbox_events_claim", "status", "available_at", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("out")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    aggregate_type: Mapped[str] = mapped_column(String(48), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(512))


class BuilderTask(TimestampMixin, Base):
    __tablename__ = "builder_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_builder_tasks_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            "action",
            name="uq_builder_tasks_tenant_operation_action",
        ),
        UniqueConstraint(
            "luma_cluster_id",
            "luma_principal_id",
            "tenant_id",
            "application_id",
            "idempotency_key_hash",
            name="uq_builder_tasks_principal_idempotency_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_builder_tasks_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_builder_tasks_tenant_source_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_builder_tasks_tenant_operation",
        ),
        CheckConstraint("action IN ('source.analyze')", name="action"),
        CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name="idempotency_key_hash_length",
        ),
        CheckConstraint(
            "octet_length(request_digest) = 32", name="request_digest_length"
        ),
        CheckConstraint(
            "credential_lease_id ~ "
            "'^(lease_[0-9A-HJKMNP-TV-Z]{26}|cl_[A-Za-z0-9][A-Za-z0-9._-]{7,124})$'",
            name="credential_lease_id",
        ),
        CheckConstraint("hash_key_version > 0", name="hash_key_version_positive"),
        CheckConstraint("event_cursor >= 0", name="event_cursor_nonnegative"),
        CheckConstraint(
            "checkpoint_version >= 0", name="checkpoint_version_nonnegative"
        ),
        CheckConstraint(
            "upstream_status IS NULL OR upstream_status IN "
            "('queued','running','cancel_requested','succeeded','failed','timed_out','canceled')",
            name="upstream_status",
        ),
        CheckConstraint(
            "result_descriptor_json IS NULL OR "
            "jsonb_typeof(result_descriptor_json) = 'object'",
            name="result_descriptor_object",
        ),
        Index(
            "uq_builder_tasks_luma_principal_task",
            "luma_cluster_id",
            "luma_principal_id",
            "luma_task_id",
            unique=True,
            postgresql_where=text("luma_task_id IS NOT NULL"),
        ),
        Index(
            "ix_builder_tasks_operation_checkpoint",
            "tenant_id",
            "operation_id",
            "checkpoint_version",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("btask")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    luma_cluster_id: Mapped[str] = mapped_column(String(128), nullable=False)
    luma_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    luma_task_id: Mapped[str | None] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    credential_lease_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32), nullable=False
    )
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    hash_key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_cursor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    upstream_status: Mapped[str | None] = mapped_column(String(24))
    cancel_forwarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    checkpoint_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    result_descriptor_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SourceCredentialLease(TimestampMixin, Base):
    __tablename__ = "source_credential_leases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_source_credential_leases_tenant_id_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "builder_task_id",
            "allowed_action",
            "allowed_host",
            name="uq_source_credential_leases_task_action_host",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_source_credential_leases_tenant_source_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_source_credential_leases_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "builder_task_id"],
            ["builder_tasks.tenant_id", "builder_tasks.id"],
            ondelete="CASCADE",
            name="fk_source_credential_leases_tenant_builder_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            ondelete="RESTRICT",
            name="fk_source_credential_leases_tenant_connection",
        ),
        CheckConstraint("allowed_action IN ('source.fetch')", name="allowed_action"),
        CheckConstraint(
            "id ~ "
            "'^(lease_[0-9A-HJKMNP-TV-Z]{26}|cl_[A-Za-z0-9][A-Za-z0-9._-]{7,124})$'",
            name="opaque_id",
        ),
        CheckConstraint(
            "status IN ('issued','claimed','consumed','revoked','expired')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL)",
            name="consumed_status",
        ),
        CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
            name="revoked_status",
        ),
        CheckConstraint(
            "NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="terminal_once",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        CheckConstraint(
            "consumer_binding_hash IS NULL OR octet_length(consumer_binding_hash) = 32",
            name="consumer_binding_hash_length",
        ),
        CheckConstraint(
            "(consumer_binding_hash IS NULL) = (binding_key_version IS NULL)",
            name="consumer_binding_fields_together",
        ),
        CheckConstraint(
            "source_connection_id IS NULL OR consumer_binding_hash IS NOT NULL",
            name="private_connection_has_consumer_binding",
        ),
        CheckConstraint(
            "binding_key_version IS NULL OR binding_key_version > 0",
            name="binding_key_version_positive",
        ),
        Index("ix_source_credential_leases_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    source_connection_id: Mapped[str | None] = mapped_column(String(64))
    source_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    builder_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_action: Mapped[str] = mapped_column(String(40), nullable=False)
    allowed_host: Mapped[str] = mapped_column(String(260), nullable=False)
    consumer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    consumer_binding_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    binding_key_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="issued"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Analysis(TimestampMixin, Base):
    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_analyses_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "operation_id", name="uq_analyses_tenant_operation"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_source_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_operation",
        ),
        CheckConstraint(
            "status IN ('queued','analyzing','analyzed','deployable',"
            "'needs_configuration','not_deployable','diagnostic_failed','failed','expired')",
            name="status",
        ),
        CheckConstraint(
            "((status IN ('queued','analyzing','failed','expired')) AND "
            "policy_version IS NULL AND agent_image_digest IS NULL AND "
            "resolved_commit_full IS NULL AND source_tree_digest IS NULL AND "
            "source_snapshot_id IS NULL AND source_snapshot_digest IS NULL AND "
            "deployment_plan_digest IS NULL AND build_plan_digest IS NULL AND "
            "evidence_digest IS NULL AND artifact_state = 'descriptor-only' AND "
            "plan_stored IS FALSE) OR "
            "((status IN ('analyzed','deployable','needs_configuration',"
            "'not_deployable','diagnostic_failed')) AND policy_version IS NOT NULL AND "
            "agent_image_digest IS NOT NULL AND resolved_commit_full IS NOT NULL AND "
            "source_tree_digest IS NOT NULL AND source_snapshot_id IS NOT NULL AND "
            "source_snapshot_digest IS NOT NULL AND "
            "deployment_plan_digest IS NOT NULL AND build_plan_digest IS NOT NULL AND "
            "evidence_digest IS NOT NULL)",
            name="result_shape",
        ),
        CheckConstraint(
            "resolved_commit_full ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="resolved_commit_full",
        ),
        CheckConstraint(
            "source_tree_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="source_tree_digest",
        ),
        CheckConstraint(
            "source_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="source_snapshot_digest",
        ),
        CheckConstraint(
            "deployment_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="deployment_plan_digest",
        ),
        CheckConstraint(
            "build_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="build_plan_digest",
        ),
        CheckConstraint(
            "evidence_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="evidence_digest",
        ),
        CheckConstraint(
            "agent_image_digest ~ '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'",
            name="agent_image_digest",
        ),
        CheckConstraint(
            "artifact_state IN ('descriptor-only','stored')", name="artifact_state"
        ),
        CheckConstraint(
            "verdict IS NULL OR verdict IN "
            "('deployable','needs_input','unsupported','diagnostic_failed')",
            name="verdict",
        ),
        CheckConstraint(
            "diagnostic_status IS NULL OR diagnostic_status IN "
            "('succeeded','diagnostic_failed')",
            name="diagnostic_status",
        ),
        CheckConstraint(
            "diagnostic_mode IS NULL OR diagnostic_mode IN "
            "('ai','deterministic_fallback')",
            name="diagnostic_mode",
        ),
        CheckConstraint(
            "plan_stored = (artifact_state = 'stored')", name="plan_storage_state"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("ana")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str | None] = mapped_column(String(128))
    agent_image_digest: Mapped[str | None] = mapped_column(String(512))
    resolved_commit_full: Mapped[str | None] = mapped_column(String(64))
    source_tree_digest: Mapped[str | None] = mapped_column(String(71))
    source_snapshot_id: Mapped[str | None] = mapped_column(String(128))
    source_snapshot_digest: Mapped[str | None] = mapped_column(String(71))
    deployment_plan_digest: Mapped[str | None] = mapped_column(String(71))
    build_plan_digest: Mapped[str | None] = mapped_column(String(71))
    evidence_digest: Mapped[str | None] = mapped_column(String(71))
    verdict: Mapped[str | None] = mapped_column(String(32))
    diagnostic_status: Mapped[str | None] = mapped_column(String(32))
    diagnostic_mode: Mapped[str | None] = mapped_column(String(32))
    diagnostic_code: Mapped[str | None] = mapped_column(String(96))
    knowledge_version: Mapped[str | None] = mapped_column(String(96))
    blockers: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    artifact_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="descriptor-only"
    )
    plan_stored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class Artifact(TimestampMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_artifacts_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "kind", "digest", name="uq_artifacts_tenant_kind_digest"
        ),
        CheckConstraint(
            "kind IN ('evidence','deployment-plan','build-plan-candidate')",
            name="kind",
        ),
        CheckConstraint("digest ~ '^sha256:[0-9a-f]{64}$'", name="digest"),
        CheckConstraint(
            "(kind = 'evidence' AND media_type = 'application/vnd.lae.evidence+json') OR "
            "(kind = 'deployment-plan' AND media_type = 'application/vnd.lae.deployment-plan+json') OR "
            "(kind = 'build-plan-candidate' AND media_type = 'application/vnd.lae.build-plan-candidate+json')",
            name="kind_media_type",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 1073741824", name="size_bytes_range"
        ),
        CheckConstraint(
            "upload_status IN ('pending','uploading','verified','failed')",
            name="upload_status",
        ),
        CheckConstraint(
            "(upload_status = 'verified' AND storage_key IS NOT NULL AND verified_at IS NOT NULL) OR "
            "(upload_status <> 'verified' AND verified_at IS NULL)",
            name="verification_state",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("art")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    upload_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisArtifact(Base):
    __tablename__ = "analysis_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "analysis_id"],
            ["analyses.tenant_id", "analyses.id"],
            ondelete="CASCADE",
            name="fk_analysis_artifacts_tenant_analysis",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            ondelete="RESTRICT",
            name="fk_analysis_artifacts_tenant_artifact",
        ),
        CheckConstraint(
            "name IN ('evidence','deploymentPlan','buildPlan')", name="name"
        ),
        UniqueConstraint(
            "tenant_id", "analysis_id", "artifact_id", name="uq_analysis_artifacts_link"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    analysis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AppRevision(TimestampMixin, Base):
    __tablename__ = "app_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_app_revisions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_app_revisions_app_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "application_id",
            "revision_no",
            name="uq_app_revisions_app_number",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "analysis_id"],
            ["analyses.tenant_id", "analyses.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_analysis",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_source_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_plan_artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_plan_artifact",
        ),
        CheckConstraint("revision_no > 0", name="revision_no_positive"),
        CheckConstraint("kind IN ('service','compose')", name="kind"),
        CheckConstraint(
            "status IN ('candidate','active','superseded','failed')", name="status"
        ),
        CheckConstraint("environment_version >= 0", name="environment_version"),
        CheckConstraint(
            "deployment_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="deployment_plan_digest",
        ),
        CheckConstraint(
            "normalized_compose_digest IS NULL OR "
            "normalized_compose_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="normalized_compose_digest",
        ),
        CheckConstraint(
            "luma_manifest_digest IS NULL OR "
            "luma_manifest_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="luma_manifest_digest",
        ),
        CheckConstraint(
            "status NOT IN ('active','superseded') OR "
            "luma_manifest_digest IS NOT NULL",
            name="active_manifest_digest",
        ),
        CheckConstraint(
            "environment_schema_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="environment_schema_digest",
        ),
        Index(
            "ix_app_revisions_tenant_app_created",
            "tenant_id",
            "application_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("rev")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analysis_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    deployment_plan_artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_plan_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    normalized_compose_digest: Mapped[str | None] = mapped_column(String(71))
    luma_manifest_digest: Mapped[str | None] = mapped_column(String(71))
    environment_schema_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    environment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="candidate"
    )
    created_by_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(64), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deployment(TimestampMixin, Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_deployments_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_deployments_app_id",
        ),
        UniqueConstraint(
            "tenant_id", "operation_id", name="uq_deployments_tenant_operation"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "revision_id"],
            [
                "app_revisions.tenant_id",
                "app_revisions.application_id",
                "app_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_app_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "previous_deployment_id"],
            ["deployments.tenant_id", "deployments.application_id", "deployments.id"],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_app_previous",
        ),
        CheckConstraint(
            "status IN ('queued','building','deploying','verifying','succeeded','failed','canceled')",
            name="status",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','canceled')) = (finished_at IS NOT NULL)",
            name="terminal_finished",
        ),
        CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="failure_error_code",
        ),
        Index(
            "ix_deployments_tenant_app_created",
            "tenant_id",
            "application_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("dep")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="queued"
    )
    luma_cluster_id: Mapped[str | None] = mapped_column(String(128))
    luma_external_ref: Mapped[str | None] = mapped_column(String(512))
    previous_deployment_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(96))
    error_message: Mapped[str | None] = mapped_column(String(512))


class ApplicationLifecycleRequest(TimestampMixin, Base):
    """Immutable inputs for one application lifecycle reconciliation.

    Runtime workers consume this row instead of re-reading a moving branch,
    current deployment pointer, or caller-supplied topology.  Secrets and
    repository credentials are referenced only through existing tenant-owned
    source/connection records.
    """

    __tablename__ = "application_lifecycle_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            name="uq_application_lifecycle_requests_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_application_lifecycle_requests_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "base_source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_base_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "source_deployment_id"],
            ["deployments.tenant_id", "deployments.application_id", "deployments.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_source_deployment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "rollback_deployment_id"],
            ["deployments.tenant_id", "deployments.application_id", "deployments.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_rollback_deployment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "analysis_id"],
            ["analyses.tenant_id", "analyses.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_analysis",
        ),
        CheckConstraint(
            "action IN ('check-update','suspend','resume','restart','rollback','delete')",
            name="action",
        ),
        CheckConstraint(
            "previous_desired_state IN ('running','suspended')",
            name="previous_desired_state",
        ),
        CheckConstraint(
            "requested_desired_state IS NULL OR "
            "requested_desired_state IN ('running','suspended','deleted')",
            name="requested_desired_state",
        ),
        CheckConstraint(
            "(action = 'check-update') = (analysis_id IS NOT NULL)",
            name="analysis_only_for_check_update",
        ),
        CheckConstraint(
            "(action = 'check-update') = "
            "(base_source_revision_id IS NOT NULL AND source_revision_id IS NOT NULL)",
            name="check_update_source_binding",
        ),
        CheckConstraint(
            "(action = 'rollback') = (rollback_deployment_id IS NOT NULL)",
            name="rollback_target_binding",
        ),
        CheckConstraint(
            "(action IN ('check-update','restart')) = "
            "(requested_desired_state IS NULL)",
            name="requested_state_shape",
        ),
        CheckConstraint(
            "action <> 'suspend' OR requested_desired_state = 'suspended'",
            name="suspend_desired_state",
        ),
        CheckConstraint(
            "action NOT IN ('resume','rollback') OR requested_desired_state = 'running'",
            name="running_desired_state",
        ),
        CheckConstraint(
            "action <> 'delete' OR requested_desired_state = 'deleted'",
            name="delete_desired_state",
        ),
        Index(
            "ix_application_lifecycle_requests_tenant_app_created",
            "tenant_id",
            "application_id",
            "created_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    previous_desired_state: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_desired_state: Mapped[str | None] = mapped_column(String(24))
    base_source_revision_id: Mapped[str | None] = mapped_column(String(64))
    source_revision_id: Mapped[str | None] = mapped_column(String(64))
    source_deployment_id: Mapped[str | None] = mapped_column(String(64))
    rollback_deployment_id: Mapped[str | None] = mapped_column(String(64))
    analysis_id: Mapped[str | None] = mapped_column(String(64))


class DeploymentQuotaReservation(TimestampMixin, Base):
    __tablename__ = "deployment_quota_reservations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            name="uq_deployment_quota_reservations_tenant_operation",
        ),
        UniqueConstraint(
            "tenant_id",
            "deployment_id",
            name="uq_deployment_quota_reservations_tenant_deployment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployment_quota_reservations_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_quota_reservations_tenant_deployment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_deployment_quota_reservations_tenant_operation",
        ),
        CheckConstraint("deployment_slots = 1", name="single_slot"),
        CheckConstraint("volume_bytes >= 0", name="volume_bytes"),
        CheckConstraint("status IN ('held','released','consumed')", name="status"),
        CheckConstraint(
            "(status = 'held') = (released_at IS NULL)", name="terminal_time"
        ),
        Index(
            "ix_deployment_quota_reservations_held_expiry",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("qrs")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_slots: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    volume_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="held"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeploymentCheckpoint(TimestampMixin, Base):
    __tablename__ = "deployment_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "deployment_id", name="uq_deployment_checkpoints_deployment"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployment_checkpoints_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_checkpoints_tenant_deployment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "revision_id"],
            [
                "app_revisions.tenant_id",
                "app_revisions.application_id",
                "app_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_deployment_checkpoints_tenant_app_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_deployment_checkpoints_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["quota_reservation_id"],
            ["deployment_quota_reservations.id"],
            ondelete="RESTRICT",
            name="fk_deployment_checkpoints_quota_reservation",
        ),
        CheckConstraint("checkpoint_version >= 0", name="version"),
        CheckConstraint("builder_cursor >= 0", name="builder_cursor"),
        CheckConstraint(
            "phase IN ('prepare','building','rendering','volumes','deploying',"
            "'verifying','activating','complete','failed','canceled')",
            name="phase",
        ),
        CheckConstraint(
            "builder_status IS NULL OR builder_status IN "
            "('queued','running','cancel_requested','succeeded','failed','timed_out','canceled')",
            name="builder_status",
        ),
        CheckConstraint(
            "runtime_status IS NULL OR runtime_status IN "
            "('preparing','deploying','running','degraded','failed','canceling','canceled')",
            name="runtime_status",
        ),
        CheckConstraint(
            "octet_length(build_request_digest) = 32", name="build_request_digest"
        ),
        CheckConstraint(
            "manifest_digest IS NULL OR manifest_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="manifest_digest",
        ),
        CheckConstraint(
            "normalized_compose_digest IS NULL OR "
            "normalized_compose_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="compose_digest",
        ),
        CheckConstraint(
            "luma_deployment_ref IS NULL OR "
            "luma_deployment_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$'",
            name="luma_ref",
        ),
        CheckConstraint(
            "result_descriptor_json IS NULL OR "
            "jsonb_typeof(result_descriptor_json) = 'object'",
            name="result_object",
        ),
        Index(
            "ix_deployment_checkpoints_tenant_phase",
            "tenant_id",
            "phase",
            "updated_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quota_reservation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    phase: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="prepare"
    )
    builder_task_id: Mapped[str | None] = mapped_column(String(256))
    builder_cursor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    builder_status: Mapped[str | None] = mapped_column(String(24))
    build_cancel_forwarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    build_request_digest: Mapped[bytes] = mapped_column(
        LargeBinary(32), nullable=False
    )
    manifest_digest: Mapped[str | None] = mapped_column(String(71))
    normalized_compose_digest: Mapped[str | None] = mapped_column(String(71))
    luma_deployment_ref: Mapped[str | None] = mapped_column(String(256))
    runtime_status: Mapped[str | None] = mapped_column(String(24))
    runtime_cancel_forwarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    result_descriptor_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeploymentBuildOutput(TimestampMixin, Base):
    __tablename__ = "deployment_build_outputs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            "build_key",
            name="uq_deployment_build_outputs_operation_build",
        ),
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            "service_key",
            name="uq_deployment_build_outputs_operation_service",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployment_build_outputs_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_build_outputs_tenant_deployment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_deployment_build_outputs_tenant_operation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "revision_id"],
            [
                "app_revisions.tenant_id",
                "app_revisions.application_id",
                "app_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_deployment_build_outputs_tenant_app_revision",
        ),
        CheckConstraint("build_key ~ '^[a-z][a-z0-9-]{0,62}$'", name="build_key"),
        CheckConstraint(
            "service_key ~ '^[a-z0-9][a-z0-9._-]{0,79}$'", name="service_key"
        ),
        CheckConstraint(
            "image_digest ~ '^sha256:[0-9a-f]{64}$'", name="image_digest"
        ),
        CheckConstraint(
            "sbom_digest ~ '^sha256:[0-9a-f]{64}$'", name="sbom_digest"
        ),
        CheckConstraint(
            "provenance_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="provenance_digest",
        ),
        CheckConstraint(
            "scan_digest ~ '^sha256:[0-9a-f]{64}$'", name="scan_digest"
        ),
        Index(
            "ix_deployment_build_outputs_tenant_deployment",
            "tenant_id",
            "deployment_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("bout")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    build_key: Mapped[str] = mapped_column(String(63), nullable=False)
    service_key: Mapped[str] = mapped_column(String(80), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    sbom_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    provenance_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    scan_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingOrder(TimestampMixin, Base):
    __tablename__ = "billing_orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_billing_orders_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "principal_type",
            "principal_id",
            "idempotency_key_hash",
            name="uq_billing_orders_idempotency_scope",
        ),
        CheckConstraint(
            "principal_type IN ('session','deploy-token')", name="principal_type"
        ),
        CheckConstraint(
            "provider IN ('mock','wechat_pay','alipay')", name="provider"
        ),
        CheckConstraint("plan_code IN ('pro','ultra')", name="plan_code"),
        CheckConstraint("interval IN ('monthly','yearly')", name="interval"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "jsonb_typeof(pricing_snapshot) = 'object'",
            name="pricing_snapshot_object",
        ),
        CheckConstraint(
            "status IN ('pending','paid','failed','expired','canceled')",
            name="status",
        ),
        CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name="idempotency_key_hash_length",
        ),
        CheckConstraint(
            "octet_length(request_hash) = 32", name="request_hash_length"
        ),
        CheckConstraint(
            "(status = 'paid') = (paid_subscription_id IS NOT NULL)",
            name="paid_subscription",
        ),
        Index("ix_billing_orders_tenant_created", "tenant_id", "created_at"),
        Index(
            "ix_billing_orders_pending_expiry",
            "checkout_expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("ord")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_merchant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=False
    )
    plan_code: Mapped[str] = mapped_column(String(24), nullable=False)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    checkout_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    paid_subscription_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("subscriptions.id", ondelete="RESTRICT")
    )


class BillingPaymentEvent(Base):
    __tablename__ = "billing_payment_events"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_billing_payment_events_provider_event",
        ),
        UniqueConstraint(
            "provider",
            "provider_principal_id",
            "idempotency_key_hash",
            name="uq_billing_payment_events_idempotency_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["billing_orders.tenant_id", "billing_orders.id"],
            ondelete="RESTRICT",
            name="fk_billing_payment_events_tenant_order",
        ),
        CheckConstraint(
            "provider IN ('mock','wechat_pay','alipay')", name="provider"
        ),
        CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name="idempotency_key_hash_length",
        ),
        CheckConstraint(
            "octet_length(payload_hash) = 32", name="payload_hash_length"
        ),
        CheckConstraint(
            "reported_plan_code IN ('pro','ultra')", name="reported_plan_code"
        ),
        CheckConstraint(
            "reported_interval IN ('monthly','yearly')", name="reported_interval"
        ),
        CheckConstraint(
            "reported_currency ~ '^[A-Z]{3}$'", name="reported_currency"
        ),
        CheckConstraint(
            "reported_amount_minor > 0", name="reported_amount_positive"
        ),
        CheckConstraint(
            "reported_outcome IN ('paid','failed','expired','canceled')",
            name="reported_outcome",
        ),
        CheckConstraint(
            "processing_status IN ('accepted','rejected','ignored')",
            name="processing_status",
        ),
        CheckConstraint(
            "order_status_after IN ('pending','paid','failed','expired','canceled')",
            name="order_status_after",
        ),
        Index(
            "ix_billing_payment_events_tenant_order_created",
            "tenant_id",
            "order_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("pevt")
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    reported_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reported_merchant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reported_plan_code: Mapped[str] = mapped_column(String(24), nullable=False)
    reported_interval: Mapped[str] = mapped_column(String(16), nullable=False)
    reported_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reported_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reported_outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processing_status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    order_status_after: Mapped[str] = mapped_column(String(24), nullable=False)
    subscription_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("subscriptions.id", ondelete="RESTRICT")
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
