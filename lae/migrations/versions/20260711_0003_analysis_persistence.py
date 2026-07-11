"""Durable source analysis checkpoints and artifact descriptors (expand).

Revision ID: 20260711_0003
Revises: 20260711_0002
Create Date: 2026-07-11

This migration stores only opaque credential lease identifiers. Exchanged
repository credentials remain ephemeral and never enter PostgreSQL.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260711_0003"
down_revision: str | None = "20260711_0002"
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
    op.create_unique_constraint(
        "uq_source_revisions_tenant_id_id",
        "source_revisions",
        ["tenant_id", "id"],
    )

    op.create_table(
        "builder_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("source_revision_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("luma_cluster_id", sa.String(length=128), nullable=False),
        sa.Column("luma_principal_id", sa.String(length=128), nullable=False),
        sa.Column("luma_task_id", sa.String(length=256), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("credential_lease_id", sa.String(length=128), nullable=False),
        sa.Column(
            "idempotency_key_hash", sa.LargeBinary(length=32), nullable=False
        ),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("hash_key_version", sa.Integer(), nullable=False),
        sa.Column(
            "event_cursor", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("upstream_status", sa.String(length=24), nullable=True),
        sa.Column("cancel_forwarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "checkpoint_version",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "result_descriptor_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "action IN ('source.analyze')", name=op.f("ck_builder_tasks_action")
        ),
        sa.CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name=op.f("ck_builder_tasks_idempotency_key_hash_length"),
        ),
        sa.CheckConstraint(
            "octet_length(request_digest) = 32",
            name=op.f("ck_builder_tasks_request_digest_length"),
        ),
        sa.CheckConstraint(
            "credential_lease_id ~ "
            "'^(lease_[0-9A-HJKMNP-TV-Z]{26}|cl_[A-Za-z0-9][A-Za-z0-9._-]{7,124})$'",
            name=op.f("ck_builder_tasks_credential_lease_id"),
        ),
        sa.CheckConstraint(
            "hash_key_version > 0",
            name=op.f("ck_builder_tasks_hash_key_version_positive"),
        ),
        sa.CheckConstraint(
            "event_cursor >= 0",
            name=op.f("ck_builder_tasks_event_cursor_nonnegative"),
        ),
        sa.CheckConstraint(
            "checkpoint_version >= 0",
            name=op.f("ck_builder_tasks_checkpoint_version_nonnegative"),
        ),
        sa.CheckConstraint(
            "upstream_status IS NULL OR upstream_status IN "
            "('queued','running','cancel_requested','succeeded','failed','timed_out','canceled')",
            name=op.f("ck_builder_tasks_upstream_status"),
        ),
        sa.CheckConstraint(
            "result_descriptor_json IS NULL OR "
            "jsonb_typeof(result_descriptor_json) = 'object'",
            name=op.f("ck_builder_tasks_result_descriptor_object"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_builder_tasks_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_builder_tasks_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_builder_tasks_tenant_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_builder_tasks_tenant_operation",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_builder_tasks")),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_builder_tasks_tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "operation_id",
            "action",
            name="uq_builder_tasks_tenant_operation_action",
        ),
        sa.UniqueConstraint(
            "luma_cluster_id",
            "luma_principal_id",
            "tenant_id",
            "application_id",
            "idempotency_key_hash",
            name="uq_builder_tasks_principal_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_builder_tasks_operation_checkpoint",
        "builder_tasks",
        ["tenant_id", "operation_id", "checkpoint_version"],
        unique=False,
    )
    op.create_index(
        "uq_builder_tasks_luma_principal_task",
        "builder_tasks",
        ["luma_cluster_id", "luma_principal_id", "luma_task_id"],
        unique=True,
        postgresql_where=sa.text("luma_task_id IS NOT NULL"),
    )

    op.create_table(
        "source_credential_leases",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_connection_id", sa.String(length=64), nullable=True),
        sa.Column("source_revision_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("builder_task_id", sa.String(length=64), nullable=False),
        sa.Column("allowed_action", sa.String(length=40), nullable=False),
        sa.Column("allowed_host", sa.String(length=260), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="issued", nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "allowed_action IN ('source.fetch')",
            name=op.f("ck_source_credential_leases_allowed_action"),
        ),
        sa.CheckConstraint(
            "id ~ "
            "'^(lease_[0-9A-HJKMNP-TV-Z]{26}|cl_[A-Za-z0-9][A-Za-z0-9._-]{7,124})$'",
            name=op.f("ck_source_credential_leases_opaque_id"),
        ),
        sa.CheckConstraint(
            "status IN ('issued','claimed','consumed','revoked','expired')",
            name=op.f("ck_source_credential_leases_status"),
        ),
        sa.CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL)",
            name=op.f("ck_source_credential_leases_consumed_status"),
        ),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
            name=op.f("ck_source_credential_leases_revoked_status"),
        ),
        sa.CheckConstraint(
            "NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name=op.f("ck_source_credential_leases_terminal_once"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_source_credential_leases_expires_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_source_credential_leases_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_source_credential_leases_tenant_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_source_credential_leases_tenant_operation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "builder_task_id"],
            ["builder_tasks.tenant_id", "builder_tasks.id"],
            ondelete="CASCADE",
            name="fk_source_credential_leases_tenant_builder_task",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_credential_leases")),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_source_credential_leases_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "builder_task_id",
            "allowed_action",
            "allowed_host",
            name="uq_source_credential_leases_task_action_host",
        ),
    )
    op.create_index(
        "ix_source_credential_leases_expiry",
        "source_credential_leases",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "analyses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("source_revision_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=True),
        sa.Column("agent_image_digest", sa.String(length=512), nullable=True),
        sa.Column("resolved_commit_full", sa.String(length=64), nullable=True),
        sa.Column("source_tree_digest", sa.String(length=71), nullable=True),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("source_snapshot_digest", sa.String(length=71), nullable=True),
        sa.Column("deployment_plan_digest", sa.String(length=71), nullable=True),
        sa.Column("build_plan_digest", sa.String(length=71), nullable=True),
        sa.Column("evidence_digest", sa.String(length=71), nullable=True),
        sa.Column(
            "artifact_state",
            sa.String(length=24),
            server_default="descriptor-only",
            nullable=False,
        ),
        sa.Column(
            "plan_stored", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued','analyzing','analyzed','deployable',"
            "'needs_configuration','not_deployable','failed','expired')",
            name=op.f("ck_analyses_status"),
        ),
        sa.CheckConstraint(
            "((status IN ('queued','analyzing','failed','expired')) AND "
            "policy_version IS NULL AND agent_image_digest IS NULL AND "
            "resolved_commit_full IS NULL AND source_tree_digest IS NULL AND "
            "source_snapshot_id IS NULL AND source_snapshot_digest IS NULL AND "
            "deployment_plan_digest IS NULL AND build_plan_digest IS NULL AND "
            "evidence_digest IS NULL AND artifact_state = 'descriptor-only' AND "
            "plan_stored IS FALSE) OR "
            "((status IN ('analyzed','deployable','needs_configuration',"
            "'not_deployable')) AND policy_version IS NOT NULL AND "
            "agent_image_digest IS NOT NULL AND resolved_commit_full IS NOT NULL AND "
            "source_tree_digest IS NOT NULL AND source_snapshot_id IS NOT NULL AND "
            "source_snapshot_digest IS NOT NULL AND "
            "deployment_plan_digest IS NOT NULL AND build_plan_digest IS NOT NULL AND "
            "evidence_digest IS NOT NULL)",
            name=op.f("ck_analyses_result_shape"),
        ),
        sa.CheckConstraint(
            "resolved_commit_full ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name=op.f("ck_analyses_resolved_commit_full"),
        ),
        sa.CheckConstraint(
            "source_tree_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_analyses_source_tree_digest"),
        ),
        sa.CheckConstraint(
            "source_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_analyses_source_snapshot_digest"),
        ),
        sa.CheckConstraint(
            "deployment_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_analyses_deployment_plan_digest"),
        ),
        sa.CheckConstraint(
            "build_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_analyses_build_plan_digest"),
        ),
        sa.CheckConstraint(
            "evidence_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_analyses_evidence_digest"),
        ),
        sa.CheckConstraint(
            "agent_image_digest ~ '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'",
            name=op.f("ck_analyses_agent_image_digest"),
        ),
        sa.CheckConstraint(
            "artifact_state IN ('descriptor-only','stored')",
            name=op.f("ck_analyses_artifact_state"),
        ),
        sa.CheckConstraint(
            "plan_stored = (artifact_state = 'stored')",
            name=op.f("ck_analyses_plan_storage_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_analyses_tenant_operation",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyses")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_analyses_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", name="uq_analyses_tenant_operation"
        ),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column(
            "upload_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('evidence','deployment-plan','build-plan-candidate')",
            name=op.f("ck_artifacts_kind"),
        ),
        sa.CheckConstraint(
            "digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_artifacts_digest"),
        ),
        sa.CheckConstraint(
            "(kind = 'evidence' AND media_type = 'application/vnd.lae.evidence+json') OR "
            "(kind = 'deployment-plan' AND media_type = 'application/vnd.lae.deployment-plan+json') OR "
            "(kind = 'build-plan-candidate' AND media_type = 'application/vnd.lae.build-plan-candidate+json')",
            name=op.f("ck_artifacts_kind_media_type"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 1073741824",
            name=op.f("ck_artifacts_size_bytes_range"),
        ),
        sa.CheckConstraint(
            "upload_status IN ('pending','uploading','verified','failed')",
            name=op.f("ck_artifacts_upload_status"),
        ),
        sa.CheckConstraint(
            "(upload_status = 'verified' AND storage_key IS NOT NULL AND verified_at IS NOT NULL) OR "
            "(upload_status <> 'verified' AND verified_at IS NULL)",
            name=op.f("ck_artifacts_verification_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_artifacts_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_artifacts_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "kind", "digest", name="uq_artifacts_tenant_kind_digest"
        ),
    )

    op.create_table(
        "analysis_artifacts",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("analysis_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "name IN ('evidence','deploymentPlan','buildPlan')",
            name=op.f("ck_analysis_artifacts_name"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_analysis_artifacts_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "analysis_id"],
            ["analyses.tenant_id", "analyses.id"],
            ondelete="CASCADE",
            name="fk_analysis_artifacts_tenant_analysis",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            ondelete="RESTRICT",
            name="fk_analysis_artifacts_tenant_artifact",
        ),
        sa.PrimaryKeyConstraint(
            "analysis_id", "name", name=op.f("pk_analysis_artifacts")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "analysis_id",
            "artifact_id",
            name="uq_analysis_artifacts_link",
        ),
    )


def downgrade() -> None:
    op.drop_table("analysis_artifacts")
    op.drop_table("artifacts")
    op.drop_table("analyses")
    op.drop_index(
        "ix_source_credential_leases_expiry",
        table_name="source_credential_leases",
    )
    op.drop_table("source_credential_leases")
    op.drop_index(
        "uq_builder_tasks_luma_principal_task", table_name="builder_tasks"
    )
    op.drop_index("ix_builder_tasks_operation_checkpoint", table_name="builder_tasks")
    op.drop_table("builder_tasks")
    op.drop_constraint(
        "uq_source_revisions_tenant_id_id", "source_revisions", type_="unique"
    )
