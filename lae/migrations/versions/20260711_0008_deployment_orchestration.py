"""Durable deployment orchestration checkpoints and quota reservations.

Revision ID: 20260711_0008
Revises: 20260711_0007
Create Date: 2026-07-11

The checkpoint schema stores only immutable digests, opaque upstream
references and allowlisted status data.  Runtime environment plaintext,
registry coordinates, internal URLs and Luma credentials have no columns.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0008"
down_revision: str | None = "20260711_0007"
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
        "application_volumes",
        sa.Column("luma_volume_ref", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "application_volumes",
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
    )
    # No pre-0008 row has a durable Luma binding.  A legacy optimistic
    # ``ready`` marker therefore becomes pending and must be prepared through
    # the scoped runtime adapter before a deployment may activate.
    op.execute("UPDATE application_volumes SET status = 'pending' WHERE status = 'ready'")
    op.create_check_constraint(
        op.f("ck_application_volumes_luma_volume_ref"),
        "application_volumes",
        "luma_volume_ref IS NULL OR luma_volume_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$'",
    )
    op.create_check_constraint(
        op.f("ck_application_volumes_provisioning_binding"),
        "application_volumes",
        "(status = 'ready') = (luma_volume_ref IS NOT NULL AND provisioned_at IS NOT NULL)",
    )

    op.create_table(
        "deployment_quota_reservations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column(
            "deployment_slots", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "volume_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="held"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "deployment_slots = 1",
            name=op.f("ck_deployment_quota_reservations_single_slot"),
        ),
        sa.CheckConstraint(
            "volume_bytes >= 0",
            name=op.f("ck_deployment_quota_reservations_volume_bytes"),
        ),
        sa.CheckConstraint(
            "status IN ('held','released','consumed')",
            name=op.f("ck_deployment_quota_reservations_status"),
        ),
        sa.CheckConstraint(
            "(status = 'held') = (released_at IS NULL)",
            name=op.f("ck_deployment_quota_reservations_terminal_time"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_deployment_quota_reservations_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployment_quota_reservations_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_quota_reservations_tenant_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_deployment_quota_reservations_tenant_operation",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_deployment_quota_reservations")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "operation_id",
            name="uq_deployment_quota_reservations_tenant_operation",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "deployment_id",
            name="uq_deployment_quota_reservations_tenant_deployment",
        ),
    )
    op.create_index(
        "ix_deployment_quota_reservations_held_expiry",
        "deployment_quota_reservations",
        ["status", "expires_at"],
    )

    op.create_table(
        "deployment_checkpoints",
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("quota_reservation_id", sa.String(length=64), nullable=False),
        sa.Column(
            "checkpoint_version", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "phase", sa.String(length=24), nullable=False, server_default="prepare"
        ),
        sa.Column("builder_task_id", sa.String(length=256), nullable=True),
        sa.Column(
            "builder_cursor", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("builder_status", sa.String(length=24), nullable=True),
        sa.Column(
            "build_cancel_forwarded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("build_request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("manifest_digest", sa.String(length=71), nullable=True),
        sa.Column("normalized_compose_digest", sa.String(length=71), nullable=True),
        sa.Column("luma_deployment_ref", sa.String(length=256), nullable=True),
        sa.Column("runtime_status", sa.String(length=24), nullable=True),
        sa.Column(
            "runtime_cancel_forwarded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "result_descriptor_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "checkpoint_version >= 0",
            name=op.f("ck_deployment_checkpoints_version"),
        ),
        sa.CheckConstraint(
            "builder_cursor >= 0",
            name=op.f("ck_deployment_checkpoints_builder_cursor"),
        ),
        sa.CheckConstraint(
            "phase IN ('prepare','building','rendering','volumes','deploying',"
            "'verifying','activating','complete','failed','canceled')",
            name=op.f("ck_deployment_checkpoints_phase"),
        ),
        sa.CheckConstraint(
            "builder_status IS NULL OR builder_status IN "
            "('queued','running','cancel_requested','succeeded','failed','timed_out','canceled')",
            name=op.f("ck_deployment_checkpoints_builder_status"),
        ),
        sa.CheckConstraint(
            "runtime_status IS NULL OR runtime_status IN "
            "('preparing','deploying','running','degraded','failed','canceling','canceled')",
            name=op.f("ck_deployment_checkpoints_runtime_status"),
        ),
        sa.CheckConstraint(
            "octet_length(build_request_digest) = 32",
            name=op.f("ck_deployment_checkpoints_build_request_digest"),
        ),
        sa.CheckConstraint(
            "manifest_digest IS NULL OR manifest_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployment_checkpoints_manifest_digest"),
        ),
        sa.CheckConstraint(
            "normalized_compose_digest IS NULL OR normalized_compose_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployment_checkpoints_compose_digest"),
        ),
        sa.CheckConstraint(
            "luma_deployment_ref IS NULL OR "
            "luma_deployment_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,255}$'",
            name=op.f("ck_deployment_checkpoints_luma_ref"),
        ),
        sa.CheckConstraint(
            "result_descriptor_json IS NULL OR jsonb_typeof(result_descriptor_json) = 'object'",
            name=op.f("ck_deployment_checkpoints_result_object"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_deployment_checkpoints_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployment_checkpoints_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_checkpoints_tenant_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "revision_id"],
            [
                "app_revisions.tenant_id",
                "app_revisions.application_id",
                "app_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_deployment_checkpoints_tenant_app_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_deployment_checkpoints_tenant_operation",
        ),
        sa.ForeignKeyConstraint(
            ["quota_reservation_id"],
            ["deployment_quota_reservations.id"],
            ondelete="RESTRICT",
            name="fk_deployment_checkpoints_quota_reservation",
        ),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_deployment_checkpoints")),
        sa.UniqueConstraint(
            "tenant_id", "deployment_id", name="uq_deployment_checkpoints_deployment"
        ),
    )
    op.create_index(
        "ix_deployment_checkpoints_tenant_phase",
        "deployment_checkpoints",
        ["tenant_id", "phase", "updated_at"],
    )

    op.create_table(
        "deployment_build_outputs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("build_key", sa.String(length=63), nullable=False),
        sa.Column("service_key", sa.String(length=80), nullable=False),
        sa.Column("image_digest", sa.String(length=71), nullable=False),
        sa.Column("sbom_digest", sa.String(length=71), nullable=False),
        sa.Column("provenance_digest", sa.String(length=71), nullable=False),
        sa.Column("scan_digest", sa.String(length=71), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "build_key ~ '^[a-z][a-z0-9-]{0,62}$'",
            name=op.f("ck_deployment_build_outputs_build_key"),
        ),
        sa.CheckConstraint(
            "service_key ~ '^[a-z0-9][a-z0-9._-]{0,79}$'",
            name=op.f("ck_deployment_build_outputs_service_key"),
        ),
        sa.CheckConstraint(
            "image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployment_build_outputs_image_digest"),
        ),
        sa.CheckConstraint(
            "sbom_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployment_build_outputs_sbom_digest"),
        ),
        sa.CheckConstraint(
            "provenance_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployment_build_outputs_provenance_digest"),
        ),
        sa.CheckConstraint(
            "scan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployment_build_outputs_scan_digest"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_deployment_build_outputs_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployment_build_outputs_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_build_outputs_tenant_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_deployment_build_outputs_tenant_operation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "revision_id"],
            [
                "app_revisions.tenant_id",
                "app_revisions.application_id",
                "app_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_deployment_build_outputs_tenant_app_revision",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_build_outputs")),
        sa.UniqueConstraint(
            "tenant_id",
            "operation_id",
            "build_key",
            name="uq_deployment_build_outputs_operation_build",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "operation_id",
            "service_key",
            name="uq_deployment_build_outputs_operation_service",
        ),
    )
    op.create_index(
        "ix_deployment_build_outputs_tenant_deployment",
        "deployment_build_outputs",
        ["tenant_id", "deployment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deployment_build_outputs_tenant_deployment",
        table_name="deployment_build_outputs",
    )
    op.drop_table("deployment_build_outputs")
    op.drop_index(
        "ix_deployment_checkpoints_tenant_phase",
        table_name="deployment_checkpoints",
    )
    op.drop_table("deployment_checkpoints")
    op.drop_index(
        "ix_deployment_quota_reservations_held_expiry",
        table_name="deployment_quota_reservations",
    )
    op.drop_table("deployment_quota_reservations")
    op.drop_constraint(
        op.f("ck_application_volumes_provisioning_binding"),
        "application_volumes",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_application_volumes_luma_volume_ref"),
        "application_volumes",
        type_="check",
    )
    op.drop_column("application_volumes", "provisioned_at")
    op.drop_column("application_volumes", "luma_volume_ref")
