"""Durable template smoke health and publication state.

Revision ID: 20260714_0013
Revises: 20260711_0012
Create Date: 2026-07-14
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260714_0013"
down_revision: str | None = "20260711_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "template_health",
        sa.Column("template_id", sa.String(length=100), nullable=False),
        sa.Column("template_version", sa.String(length=64), nullable=False),
        sa.Column(
            "published",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "last_status",
            sa.String(length=16),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("last_run_id", sa.String(length=80), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_unpublished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "last_status IN ('unverified','succeeded','failed')",
            name=op.f("ck_template_health_last_status"),
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_template_health_failures_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("template_id", name=op.f("pk_template_health")),
    )


def downgrade() -> None:
    op.drop_table("template_health")
