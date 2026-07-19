"""Email authentication, keyed sessions and Lite entitlement (expand).

Revision ID: 20260711_0002
Revises: 20260711_0001
Create Date: 2026-07-11

The nullable csrf_hash is intentional for expand/contract compatibility with
sessions created by the foundation release. New LAE API sessions always set it;
legacy sessions cannot authorize cookie mutations.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260711_0002"
down_revision: str | None = "20260711_0001"
branch_labels: str | Sequence[str] | None = None
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
    op.add_column(
        "auth_sessions",
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "auth_sessions", sa.Column("csrf_hash", sa.LargeBinary(length=32), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_key_version_positive"),
        "auth_sessions",
        "key_version > 0",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_csrf_hash_length"),
        "auth_sessions",
        "csrf_hash IS NULL OR octet_length(csrf_hash) = 32",
    )

    op.create_table(
        "email_challenges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("magic_token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_ip_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("device_hash", sa.LargeBinary(length=32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "purpose IN ('register','login')",
            name=op.f("ck_email_challenges_purpose"),
        ),
        sa.CheckConstraint(
            "octet_length(code_hash) = 32",
            name=op.f("ck_email_challenges_code_hash_length"),
        ),
        sa.CheckConstraint(
            "octet_length(magic_token_hash) = 32",
            name=op.f("ck_email_challenges_magic_token_hash_length"),
        ),
        sa.CheckConstraint(
            "key_version > 0",
            name=op.f("ck_email_challenges_key_version_positive"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_email_challenges_attempts_nonnegative"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_email_challenges_max_attempts_positive"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_email_challenges_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "request_ip_hash IS NULL OR octet_length(request_ip_hash) = 32",
            name=op.f("ck_email_challenges_request_ip_hash_length"),
        ),
        sa.CheckConstraint(
            "device_hash IS NULL OR octet_length(device_hash) = 32",
            name=op.f("ck_email_challenges_device_hash_length"),
        ),
        sa.CheckConstraint(
            "NOT (used_at IS NOT NULL AND canceled_at IS NOT NULL)",
            name=op.f("ck_email_challenges_terminal_once"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_challenges"),
        sa.UniqueConstraint(
            "magic_token_hash", name="uq_email_challenges_magic_token_hash"
        ),
    )
    op.create_index(
        "ix_email_challenges_email_purpose_created",
        "email_challenges",
        ["email", "purpose", "created_at"],
    )
    op.create_index(
        "ix_email_challenges_ip_created",
        "email_challenges",
        ["request_ip_hash", "created_at"],
    )
    op.create_index(
        "ix_email_challenges_device_created",
        "email_challenges",
        ["device_hash", "created_at"],
    )
    op.create_index(
        "ix_email_challenges_expires_at", "email_challenges", ["expires_at"]
    )

    op.create_table(
        "plan_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "limits_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "features_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "code IN ('lite','pro','ultra')", name=op.f("ck_plan_versions_code")
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_plan_versions_version_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(limits_json) = 'object'",
            name=op.f("ck_plan_versions_limits_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(features_json) = 'object'",
            name=op.f("ck_plan_versions_features_object"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plan_versions"),
        sa.UniqueConstraint("code", "version", name="uq_plan_versions_code_version"),
    )

    plans = sa.table(
        "plan_versions",
        sa.column("id", sa.String()),
        sa.column("code", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("limits_json", postgresql.JSONB()),
        sa.column("features_json", postgresql.JSONB()),
    )
    op.bulk_insert(
        plans,
        [
            {
                "id": "pln_00000000000000000000000001",
                "code": "lite",
                "version": 1,
                "limits_json": {
                    "applications": 3,
                    "servicesPerApp": 5,
                    "publicHttpRoutesPerApp": 2,
                    "persistentVolumeBytes": 2 * 1024 * 1024 * 1024,
                    "concurrentAnalyses": 1,
                    "concurrentBuilds": 1,
                    "concurrentDeployments": 1,
                },
                "features_json": {
                    "privateGit": True,
                    "manualUpdateChecks": True,
                },
            }
        ],
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("plan_version_id", sa.String(length=64), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "interval IN ('none','monthly','yearly')",
            name=op.f("ck_subscriptions_interval"),
        ),
        sa.CheckConstraint(
            "status IN ('active','trialing','past_due','canceled','expired')",
            name=op.f("ck_subscriptions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_subscriptions_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            ondelete="RESTRICT",
            name="fk_subscriptions_plan_version_id_plan_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    op.create_index(
        "uq_subscriptions_active_tenant",
        "subscriptions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active','trialing','past_due')"),
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_table("plan_versions")
    op.drop_table("email_challenges")
    op.drop_constraint(
        op.f("ck_auth_sessions_csrf_hash_length"),
        "auth_sessions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_key_version_positive"),
        "auth_sessions",
        type_="check",
    )
    op.drop_column("auth_sessions", "csrf_hash")
    op.drop_column("auth_sessions", "key_version")
