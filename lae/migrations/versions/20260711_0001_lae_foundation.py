"""LAE Phase 1 PostgreSQL foundation (expand).

Revision ID: 20260711_0001
Revises: None
Create Date: 2026-07-11
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260711_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("expand",)
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="pending", nullable=False
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "locale", sa.String(length=32), server_default="zh-CN", nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','active','suspended')", name=op.f("ck_users_status")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index(
        "uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True
    )

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="active", nullable=False
        ),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "type IN ('personal','organization')", name=op.f("ck_tenants_type")
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended','deleted')", name=op.f("ck_tenants_status")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_tenants_owner_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_tenants_id_owner_user_id"),
    )

    op.create_table(
        "tenant_members",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "role IN ('owner','admin','developer','viewer')",
            name=op.f("ck_tenant_members_role"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_tenant_members_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_tenant_members_user_id_users",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_tenant_members"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "octet_length(session_hash) = 32",
            name=op.f("ck_auth_sessions_session_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_auth_sessions_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint("session_hash", name="uq_auth_sessions_session_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "deploy_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("prefix", sa.String(length=10), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "purpose", sa.String(length=40), server_default="deploy", nullable=False
        ),
        sa.Column(
            "is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", postgresql.INET(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name=op.f("ck_deploy_tokens_token_hash_length"),
        ),
        sa.CheckConstraint(
            "key_version > 0", name=op.f("ck_deploy_tokens_key_version_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scopes) = 'array'",
            name=op.f("ck_deploy_tokens_scopes_array"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_members.tenant_id", "tenant_members.user_id"],
            ondelete="CASCADE",
            name="fk_deploy_tokens_tenant_member",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deploy_tokens"),
        sa.UniqueConstraint("prefix", name="uq_deploy_tokens_prefix"),
        sa.UniqueConstraint("token_hash", name="uq_deploy_tokens_token_hash"),
    )
    op.create_index("ix_deploy_tokens_tenant_id", "deploy_tokens", ["tenant_id"])
    op.create_index(
        "uq_deploy_tokens_active_default",
        "deploy_tokens",
        ["tenant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE AND revoked_at IS NULL"),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("luma_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "desired_state",
            sa.String(length=24),
            server_default="running",
            nullable=False,
        ),
        sa.Column(
            "observed_state",
            sa.String(length=24),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('service','compose')", name=op.f("ck_applications_kind")
        ),
        sa.CheckConstraint(
            "desired_state IN ('running','suspended','deleted')",
            name=op.f("ck_applications_desired_state"),
        ),
        sa.CheckConstraint(
            "observed_state IN ('provisioning','running','degraded','failed','suspending','suspended','unknown')",
            name=op.f("ck_applications_observed_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_applications_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_applications"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_applications_tenant_slug"),
        sa.UniqueConstraint("luma_name", name="uq_applications_luma_name"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_applications_tenant_id_id"),
    )
    op.create_index("ix_applications_tenant_id", "applications", ["tenant_id"])

    op.create_table(
        "source_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=True),
        sa.Column("repository", sa.String(length=512), nullable=True),
        sa.Column("ref", sa.String(length=512), nullable=True),
        sa.Column("resolved_commit_full", sa.String(length=64), nullable=True),
        sa.Column("source_tree_digest", sa.String(length=71), nullable=True),
        sa.Column("upload_id", sa.String(length=64), nullable=True),
        sa.Column("template_version_id", sa.String(length=64), nullable=True),
        sa.Column(
            "subdirectory", sa.String(length=512), server_default="", nullable=False
        ),
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("snapshot_digest", sa.String(length=71), nullable=True),
        sa.Column("snapshot_artifact_id", sa.String(length=64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('upload','git','template')", name=op.f("ck_source_revisions_kind")
        ),
        sa.CheckConstraint(
            "resolved_commit_full IS NULL OR resolved_commit_full ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name=op.f("ck_source_revisions_resolved_commit_full"),
        ),
        sa.CheckConstraint(
            "snapshot_digest IS NULL OR snapshot_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_source_revisions_snapshot_digest"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_source_revisions_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_source_revisions_tenant_application",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_revisions"),
    )
    op.create_index(
        "ix_source_revisions_tenant_snapshot",
        "source_revisions",
        ["tenant_id", "snapshot_digest"],
        postgresql_where=sa.text("snapshot_digest IS NOT NULL"),
    )

    op.create_table(
        "operations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=48), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="queued", nullable=False
        ),
        sa.Column("phase", sa.String(length=80), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("parent_operation_id", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_event_seq", sa.BigInteger(), server_default="0", nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','canceled')",
            name=op.f("ck_operations_status"),
        ),
        sa.CheckConstraint(
            "lease_attempt > 0", name=op.f("ck_operations_lease_attempt_positive")
        ),
        sa.CheckConstraint(
            "last_event_seq >= 0", name=op.f("ck_operations_last_event_seq_nonnegative")
        ),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name=op.f("ck_operations_result_object"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL AND lease_heartbeat_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_heartbeat_at IS NOT NULL)",
            name=op.f("ck_operations_lease_fields_together"),
        ),
        sa.CheckConstraint(
            "status = 'running' OR lease_owner IS NULL",
            name=op.f("ck_operations_lease_only_while_running"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_operations_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_operations_tenant_parent",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operations"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_operations_tenant_id_id"),
    )
    op.create_index(
        "ix_operations_tenant_status_created",
        "operations",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_operations_claim",
        "operations",
        ["kind", "status", "lease_expires_at", "created_at", "id"],
    )
    op.create_index(
        "uq_operations_active_application_mutation",
        "operations",
        ["tenant_id", "target_id"],
        unique=True,
        postgresql_where=sa.text(
            "target_type = 'application' "
            "AND kind IN ('deployment.create','application.resume','application.suspend',"
            "'application.restart','application.rollback','application.delete') "
            "AND status IN ('queued','running')"
        ),
    )

    op.create_table(
        "operation_events",
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=96), nullable=False),
        sa.Column("phase", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("level", sa.String(length=16), server_default="info", nullable=False),
        sa.Column("message", sa.String(length=512), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_operation_events_seq_positive")),
        sa.CheckConstraint(
            "level IN ('debug','info','warning','error')",
            name=op.f("ck_operation_events_level"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'",
            name=op.f("ck_operation_events_data_object"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_operation_events_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_operation_events_tenant_operation",
        ),
        sa.PrimaryKeyConstraint("operation_id", "seq", name="pk_operation_events"),
        sa.UniqueConstraint("event_id", name="uq_operation_events_event_id"),
    )
    op.create_index(
        "ix_operation_events_tenant_created",
        "operation_events",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("route_template", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(request_hash) = 32",
            name=op.f("ck_idempotency_records_request_hash_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response_body) = 'object'",
            name=op.f("ck_idempotency_records_response_body_object"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_idempotency_records_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_idempotency_records_tenant_operation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_type",
            "principal_id",
            "method",
            "route_template",
            "key",
            name="uq_idempotency_records_scope",
        ),
    )
    op.create_index(
        "ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"]
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=48), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','publishing','published','dead')",
            name=op.f("ck_outbox_events_status"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_outbox_events_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_outbox_events_payload_object"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_outbox_events_lease_fields_together"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_outbox_events_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint(
            "tenant_id",
            "dedupe_key",
            name="uq_outbox_events_tenant_dedupe_key",
        ),
    )
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"])
    op.create_index(
        "ix_outbox_events_claim",
        "outbox_events",
        ["status", "available_at", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("idempotency_records")
    op.drop_table("operation_events")
    op.drop_table("operations")
    op.drop_table("source_revisions")
    op.drop_table("applications")
    op.drop_table("deploy_tokens")
    op.drop_table("auth_sessions")
    op.drop_table("tenant_members")
    op.drop_table("tenants")
    op.drop_table("users")
