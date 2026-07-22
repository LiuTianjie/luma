"""Support tickets for user-reported platform failures.

Revision ID: 20260722_0014
Revises: 20260714_0013
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0014"
down_revision: str | None = "20260714_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("application_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open','triaged','resolved','closed')",
            name=op.f("ck_support_tickets_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_support_tickets_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_tickets")),
    )
    op.create_index(
        "ix_support_tickets_tenant_created",
        "support_tickets",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_support_tickets_tenant_created", table_name="support_tickets")
    op.drop_table("support_tickets")
