"""Defer the final Luma manifest digest until a candidate activates.

Revision ID: 20260711_0009
Revises: 20260711_0008
Create Date: 2026-07-11

Images are produced after deployment admission.  A candidate revision therefore
cannot truthfully carry its final runtime manifest digest at creation time.  The
deployment worker fills the digest in the same transaction that activates the
revision; active and superseded revisions remain permanently digest-bound.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260711_0009"
down_revision: str | None = "20260711_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_app_revisions_luma_manifest_digest"),
        "app_revisions",
        type_="check",
    )
    op.alter_column("app_revisions", "luma_manifest_digest", nullable=True)
    op.create_check_constraint(
        op.f("ck_app_revisions_luma_manifest_digest"),
        "app_revisions",
        "luma_manifest_digest IS NULL OR "
        "luma_manifest_digest ~ '^sha256:[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_app_revisions_active_manifest_digest"),
        "app_revisions",
        "status NOT IN ('active','superseded') OR "
        "luma_manifest_digest IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_app_revisions_active_manifest_digest"),
        "app_revisions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_app_revisions_luma_manifest_digest"),
        "app_revisions",
        type_="check",
    )
    # Pre-0009 cannot represent a pre-render candidate.  Preserve a valid,
    # deterministic digest rather than deleting revision lineage on downgrade.
    op.execute(
        "UPDATE app_revisions SET luma_manifest_digest = deployment_plan_digest "
        "WHERE luma_manifest_digest IS NULL"
    )
    op.alter_column("app_revisions", "luma_manifest_digest", nullable=False)
    op.create_check_constraint(
        op.f("ck_app_revisions_luma_manifest_digest"),
        "app_revisions",
        "luma_manifest_digest ~ '^sha256:[0-9a-f]{64}$'",
    )
