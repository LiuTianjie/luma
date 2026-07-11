"""Persist public analysis verdict and safe diagnostic details.

Revision ID: 20260711_0011
Revises: 20260711_0010
Create Date: 2026-07-11
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0011"
down_revision: str | None = "20260711_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_analyses_result_shape"), "analyses", type_="check")
    op.drop_constraint(op.f("ck_analyses_status"), "analyses", type_="check")
    op.create_check_constraint(
        op.f("ck_analyses_status"),
        "analyses",
        "status IN ('queued','analyzing','analyzed','deployable',"
        "'needs_configuration','not_deployable','diagnostic_failed','failed','expired')",
    )
    op.create_check_constraint(
        op.f("ck_analyses_result_shape"),
        "analyses",
        "((status IN ('queued','analyzing','failed','expired')) AND "
        "policy_version IS NULL AND agent_image_digest IS NULL AND "
        "resolved_commit_full IS NULL AND source_tree_digest IS NULL AND "
        "source_snapshot_id IS NULL AND source_snapshot_digest IS NULL AND "
        "deployment_plan_digest IS NULL AND build_plan_digest IS NULL AND "
        "evidence_digest IS NULL AND artifact_state = 'descriptor-only' AND "
        "plan_stored IS FALSE) OR ((status IN ('analyzed','deployable',"
        "'needs_configuration','not_deployable','diagnostic_failed')) AND "
        "policy_version IS NOT NULL AND agent_image_digest IS NOT NULL AND "
        "resolved_commit_full IS NOT NULL AND source_tree_digest IS NOT NULL AND "
        "source_snapshot_id IS NOT NULL AND source_snapshot_digest IS NOT NULL AND "
        "deployment_plan_digest IS NOT NULL AND build_plan_digest IS NOT NULL AND "
        "evidence_digest IS NOT NULL)",
    )
    op.add_column("analyses", sa.Column("verdict", sa.String(32)))
    op.add_column("analyses", sa.Column("diagnostic_status", sa.String(32)))
    op.add_column("analyses", sa.Column("diagnostic_mode", sa.String(32)))
    op.add_column("analyses", sa.Column("diagnostic_code", sa.String(96)))
    op.add_column("analyses", sa.Column("knowledge_version", sa.String(96)))
    op.add_column(
        "analyses",
        sa.Column(
            "blockers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        op.f("ck_analyses_verdict"),
        "analyses",
        "verdict IS NULL OR verdict IN "
        "('deployable','needs_input','unsupported','diagnostic_failed')",
    )
    op.create_check_constraint(
        op.f("ck_analyses_diagnostic_status"),
        "analyses",
        "diagnostic_status IS NULL OR diagnostic_status IN "
        "('succeeded','diagnostic_failed')",
    )
    op.create_check_constraint(
        op.f("ck_analyses_diagnostic_mode"),
        "analyses",
        "diagnostic_mode IS NULL OR diagnostic_mode IN "
        "('ai','deterministic_fallback')",
    )


def downgrade() -> None:
    # Older schemas cannot represent a diagnostic result with stored artifacts.
    # Convert it to the legacy failed shape explicitly instead of letting the
    # old status/result constraints fail halfway through downgrade.
    op.execute(
        sa.text(
            "DELETE FROM analysis_artifacts WHERE analysis_id IN "
            "(SELECT id FROM analyses WHERE status = 'diagnostic_failed')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE analyses SET status = 'failed', policy_version = NULL, "
            "agent_image_digest = NULL, resolved_commit_full = NULL, "
            "source_tree_digest = NULL, source_snapshot_id = NULL, "
            "source_snapshot_digest = NULL, deployment_plan_digest = NULL, "
            "build_plan_digest = NULL, evidence_digest = NULL, "
            "artifact_state = 'descriptor-only', plan_stored = FALSE "
            "WHERE status = 'diagnostic_failed'"
        )
    )
    op.drop_constraint(op.f("ck_analyses_diagnostic_mode"), "analyses", type_="check")
    op.drop_constraint(op.f("ck_analyses_diagnostic_status"), "analyses", type_="check")
    op.drop_constraint(op.f("ck_analyses_verdict"), "analyses", type_="check")
    for column in (
        "blockers",
        "knowledge_version",
        "diagnostic_code",
        "diagnostic_mode",
        "diagnostic_status",
        "verdict",
    ):
        op.drop_column("analyses", column)
    op.drop_constraint(op.f("ck_analyses_result_shape"), "analyses", type_="check")
    op.drop_constraint(op.f("ck_analyses_status"), "analyses", type_="check")
    op.create_check_constraint(
        op.f("ck_analyses_status"),
        "analyses",
        "status IN ('queued','analyzing','analyzed','deployable',"
        "'needs_configuration','not_deployable','failed','expired')",
    )
    op.create_check_constraint(
        op.f("ck_analyses_result_shape"),
        "analyses",
        "((status IN ('queued','analyzing','failed','expired')) AND "
        "policy_version IS NULL AND agent_image_digest IS NULL AND "
        "resolved_commit_full IS NULL AND source_tree_digest IS NULL AND "
        "source_snapshot_id IS NULL AND source_snapshot_digest IS NULL AND "
        "deployment_plan_digest IS NULL AND build_plan_digest IS NULL AND "
        "evidence_digest IS NULL AND artifact_state = 'descriptor-only' AND "
        "plan_stored IS FALSE) OR ((status IN ('analyzed','deployable',"
        "'needs_configuration','not_deployable')) AND policy_version IS NOT NULL AND "
        "agent_image_digest IS NOT NULL AND resolved_commit_full IS NOT NULL AND "
        "source_tree_digest IS NOT NULL AND source_snapshot_id IS NOT NULL AND "
        "source_snapshot_digest IS NOT NULL AND deployment_plan_digest IS NOT NULL AND "
        "build_plan_digest IS NOT NULL AND evidence_digest IS NOT NULL)",
    )
