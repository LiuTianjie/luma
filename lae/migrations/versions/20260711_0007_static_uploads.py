"""Tenant-fenced static source uploads and scanner state.

Revision ID: 20260711_0007
Revises: 20260711_0006
Create Date: 2026-07-11
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260711_0007"
down_revision: str | None = "20260711_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Upload/storage limits are data and remain versioned with each plan.  The
    # maximum single archive is additionally capped by a database constraint.
    op.execute(
        sa.text(
            "UPDATE plan_versions SET limits_json = limits_json || "
            "jsonb_build_object("
            "'uploadBytes', CASE code WHEN 'lite' THEN 268435456 "
            "WHEN 'pro' THEN 5368709120 ELSE 21474836480 END, "
            "'artifactStorageBytes', CASE code WHEN 'lite' THEN 536870912 "
            "WHEN 'pro' THEN 10737418240 ELSE 53687091200 END, "
            "'maxUploadBytes', CASE code WHEN 'lite' THEN 33554432 "
            "WHEN 'pro' THEN 268435456 ELSE 536870912 END, "
            "'maxUnpackedBytes', CASE code WHEN 'lite' THEN 134217728 "
            "WHEN 'pro' THEN 1073741824 ELSE 2147483648 END) "
            "WHERE code IN ('lite','pro','ultra')"
        )
    )

    op.create_table(
        "uploads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("source_revision_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), nullable=True),
        sa.Column("expected_sha256", sa.String(length=71), nullable=False),
        sa.Column("actual_sha256", sa.String(length=71), nullable=True),
        sa.Column(
            "status", sa.String(length=24), server_default="quarantine", nullable=False
        ),
        sa.Column(
            "cleanup_status", sa.String(length=16), server_default="none", nullable=False
        ),
        sa.Column(
            "cleanup_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column(
            "complete_idempotency_key_hash", sa.LargeBinary(length=32), nullable=True
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_lease_owner", sa.String(length=128), nullable=True),
        sa.Column("scan_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('html','zip')", name=op.f("ck_uploads_kind")),
        sa.CheckConstraint(
            "status IN ('quarantine','verifying','scanning','ready','failed',"
            "'expired','deleting','deleted')",
            name=op.f("ck_uploads_status"),
        ),
        sa.CheckConstraint(
            "cleanup_status IN ('none','pending','deleting','deleted','failed')",
            name=op.f("ck_uploads_cleanup_status"),
        ),
        sa.CheckConstraint(
            "expected_bytes > 0 AND expected_bytes <= 536870912",
            name=op.f("ck_uploads_expected_bytes_range"),
        ),
        sa.CheckConstraint(
            "actual_bytes IS NULL OR (actual_bytes > 0 AND actual_bytes <= 536870912)",
            name=op.f("ck_uploads_actual_bytes_range"),
        ),
        sa.CheckConstraint(
            "expected_sha256 ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_uploads_expected_sha256"),
        ),
        sa.CheckConstraint(
            "actual_sha256 IS NULL OR actual_sha256 ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_uploads_actual_sha256"),
        ),
        sa.CheckConstraint(
            "complete_idempotency_key_hash IS NULL OR "
            "octet_length(complete_idempotency_key_hash) = 32",
            name=op.f("ck_uploads_complete_idempotency_key_hash_length"),
        ),
        sa.CheckConstraint(
            "(scan_lease_owner IS NULL AND scan_lease_expires_at IS NULL) OR "
            "(scan_lease_owner IS NOT NULL AND scan_lease_expires_at IS NOT NULL)",
            name=op.f("ck_uploads_scan_lease_fields_together"),
        ),
        sa.CheckConstraint(
            "cleanup_attempts >= 0", name=op.f("ck_uploads_cleanup_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "(status = 'ready') = (source_revision_id IS NOT NULL)",
            name=op.f("ck_uploads_ready_source_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_uploads_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_uploads_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_uploads_tenant_operation",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_uploads_tenant_id_id"),
        sa.UniqueConstraint("object_key", name="uq_uploads_object_key"),
    )
    op.create_index(
        "ix_uploads_tenant_app_created",
        "uploads",
        ["tenant_id", "application_id", "created_at"],
    )
    op.create_index(
        "ix_uploads_scan_claim",
        "uploads",
        ["status", "scan_lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_uploads_expiry", "uploads", ["status", "expires_at"]
    )
    op.create_index(
        "uq_uploads_source_revision",
        "uploads",
        ["tenant_id", "source_revision_id"],
        unique=True,
        postgresql_where=sa.text("source_revision_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_source_revisions_tenant_upload",
        "source_revisions",
        "uploads",
        ["tenant_id", "upload_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_uploads_tenant_source_revision",
        "uploads",
        "source_revisions",
        ["tenant_id", "source_revision_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_uploads_tenant_source_revision", "uploads", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_source_revisions_tenant_upload", "source_revisions", type_="foreignkey"
    )
    op.drop_index("uq_uploads_source_revision", table_name="uploads")
    op.drop_index("ix_uploads_expiry", table_name="uploads")
    op.drop_index("ix_uploads_scan_claim", table_name="uploads")
    op.drop_index("ix_uploads_tenant_app_created", table_name="uploads")
    op.drop_table("uploads")
    op.execute(
        sa.text(
            "UPDATE plan_versions SET limits_json = limits_json "
            "- 'uploadBytes' - 'artifactStorageBytes' - 'maxUploadBytes' "
            "- 'maxUnpackedBytes' WHERE code IN ('lite','pro','ultra')"
        )
    )
