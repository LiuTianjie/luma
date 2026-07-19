"""Persist immutable application lifecycle requests and update checks.

Revision ID: 20260711_0010
Revises: 20260711_0009
Create Date: 2026-07-11

The request row freezes the active deployment/source/rollback target consumed
by a reconciler.  Update checks point at a newly cloned SourceRevision and an
Analysis created in the same transaction; repository coordinates never come
from the lifecycle HTTP request.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260711_0010"
down_revision: str | None = "20260711_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ACTIVE_MUTATIONS_V2 = (
    "target_type = 'application' "
    "AND kind IN ('deployment.create','application.check-update',"
    "'application.resume','application.suspend','application.restart',"
    "'application.rollback','application.delete') "
    "AND status IN ('queued','running')"
)

_ACTIVE_MUTATIONS_V1 = (
    "target_type = 'application' "
    "AND kind IN ('deployment.create','application.resume','application.suspend',"
    "'application.restart','application.rollback','application.delete') "
    "AND status IN ('queued','running')"
)


def upgrade() -> None:
    # Update checks are application mutations too: they must not race a deploy,
    # restart, rollback, suspend, resume, or delete for the same application.
    op.drop_index(
        "uq_operations_active_application_mutation", table_name="operations"
    )
    op.create_index(
        "uq_operations_active_application_mutation",
        "operations",
        ["tenant_id", "target_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_MUTATIONS_V2),
    )

    op.create_table(
        "application_lifecycle_requests",
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("previous_desired_state", sa.String(length=24), nullable=False),
        sa.Column("requested_desired_state", sa.String(length=24), nullable=True),
        sa.Column("base_source_revision_id", sa.String(length=64), nullable=True),
        sa.Column("source_revision_id", sa.String(length=64), nullable=True),
        sa.Column("source_deployment_id", sa.String(length=64), nullable=True),
        sa.Column("rollback_deployment_id", sa.String(length=64), nullable=True),
        sa.Column("analysis_id", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "action IN ('check-update','suspend','resume','restart','rollback','delete')",
            name=op.f("ck_application_lifecycle_requests_action"),
        ),
        sa.CheckConstraint(
            "previous_desired_state IN ('running','suspended')",
            name=op.f("ck_application_lifecycle_requests_previous_desired_state"),
        ),
        sa.CheckConstraint(
            "requested_desired_state IS NULL OR "
            "requested_desired_state IN ('running','suspended','deleted')",
            name=op.f("ck_application_lifecycle_requests_requested_desired_state"),
        ),
        sa.CheckConstraint(
            "(action = 'check-update') = (analysis_id IS NOT NULL)",
            name=op.f("ck_application_lifecycle_requests_analysis_only_for_check_update"),
        ),
        sa.CheckConstraint(
            "(action = 'check-update') = "
            "(base_source_revision_id IS NOT NULL AND source_revision_id IS NOT NULL)",
            name=op.f("ck_application_lifecycle_requests_check_update_source_binding"),
        ),
        sa.CheckConstraint(
            "(action = 'rollback') = (rollback_deployment_id IS NOT NULL)",
            name=op.f("ck_application_lifecycle_requests_rollback_target_binding"),
        ),
        sa.CheckConstraint(
            "(action IN ('check-update','restart')) = "
            "(requested_desired_state IS NULL)",
            name=op.f("ck_application_lifecycle_requests_requested_state_shape"),
        ),
        sa.CheckConstraint(
            "action <> 'suspend' OR requested_desired_state = 'suspended'",
            name=op.f("ck_application_lifecycle_requests_suspend_desired_state"),
        ),
        sa.CheckConstraint(
            "action NOT IN ('resume','rollback') OR requested_desired_state = 'running'",
            name=op.f("ck_application_lifecycle_requests_running_desired_state"),
        ),
        sa.CheckConstraint(
            "action <> 'delete' OR requested_desired_state = 'deleted'",
            name=op.f("ck_application_lifecycle_requests_delete_desired_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_application_lifecycle_requests_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="CASCADE",
            name="fk_application_lifecycle_requests_tenant_operation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "base_source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_base_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "source_deployment_id"],
            ["deployments.tenant_id", "deployments.application_id", "deployments.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_source_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "rollback_deployment_id"],
            ["deployments.tenant_id", "deployments.application_id", "deployments.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_rollback_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "analysis_id"],
            ["analyses.tenant_id", "analyses.id"],
            ondelete="RESTRICT",
            name="fk_application_lifecycle_requests_tenant_analysis",
        ),
        sa.PrimaryKeyConstraint(
            "operation_id", name=op.f("pk_application_lifecycle_requests")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "operation_id",
            name="uq_application_lifecycle_requests_tenant_operation",
        ),
    )
    op.create_index(
        "ix_application_lifecycle_requests_tenant_app_created",
        "application_lifecycle_requests",
        ["tenant_id", "application_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_application_lifecycle_requests_tenant_app_created",
        table_name="application_lifecycle_requests",
    )
    op.drop_table("application_lifecycle_requests")

    op.drop_index(
        "uq_operations_active_application_mutation", table_name="operations"
    )
    op.create_index(
        "uq_operations_active_application_mutation",
        "operations",
        ["tenant_id", "target_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_MUTATIONS_V1),
    )
