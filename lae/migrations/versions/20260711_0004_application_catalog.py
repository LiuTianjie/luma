"""Tenant-scoped application catalog and lifecycle facts (expand).

Revision ID: 20260711_0004
Revises: 20260711_0003
Create Date: 2026-07-11

Only managed public HTTP routes are represented. Public TCP/UDP, tcp-relay,
host ports, custom domains, bind mounts and plaintext environment values have
no columns in this schema.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260711_0004"
down_revision: str | None = "20260711_0003"
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
    op.drop_constraint(op.f("ck_applications_kind"), "applications", type_="check")
    op.create_check_constraint(
        op.f("ck_applications_kind"),
        "applications",
        "kind IN ('pending','service','compose')",
    )
    op.add_column(
        "applications",
        sa.Column("current_revision_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "applications",
        sa.Column("current_deployment_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "applications",
        sa.Column(
            "environment_version",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_applications_environment_version"),
        "applications",
        "environment_version >= 0",
    )

    op.create_table(
        "application_services",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("service_key", sa.String(length=80), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column(
            "required", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
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
        sa.Column("current_image_digest", sa.String(length=71), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "role IN ('http','internal','worker','datastore')",
            name=op.f("ck_application_services_role"),
        ),
        sa.CheckConstraint(
            "desired_state IN ('running','suspended','deleted')",
            name=op.f("ck_application_services_desired_state"),
        ),
        sa.CheckConstraint(
            "observed_state IN ('provisioning','running','degraded','failed','suspending','suspended','unknown')",
            name=op.f("ck_application_services_observed_state"),
        ),
        sa.CheckConstraint(
            "current_image_digest IS NULL OR "
            "current_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_application_services_current_image_digest"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_application_services_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_application_services_tenant_application",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_services")),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "service_key",
            name="uq_application_services_app_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_application_services_app_id",
        ),
    )
    op.create_index(
        "ix_application_services_tenant_app",
        "application_services",
        ["tenant_id", "application_id"],
    )

    op.create_table(
        "application_routes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="http", nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("container_port", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "kind = 'http'", name=op.f("ck_application_routes_kind_http_only")
        ),
        sa.CheckConstraint(
            "hostname ~ '^[0-9a-f]{32}\\.itool\\.tech$'",
            name=op.f("ck_application_routes_managed_hostname"),
        ),
        sa.CheckConstraint(
            "container_port BETWEEN 1 AND 65535",
            name=op.f("ck_application_routes_container_port"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','provisioning','ready','failed','disabled')",
            name=op.f("ck_application_routes_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_application_routes_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_application_routes_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "service_id"],
            [
                "application_services.tenant_id",
                "application_services.application_id",
                "application_services.id",
            ],
            ondelete="CASCADE",
            name="fk_application_routes_tenant_app_service",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_routes")),
        sa.UniqueConstraint("hostname", name="uq_application_routes_hostname"),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_application_routes_app_id",
        ),
    )
    op.create_index(
        "ix_application_routes_tenant_app",
        "application_routes",
        ["tenant_id", "application_id"],
    )
    op.create_index(
        "uq_application_routes_primary",
        "application_routes",
        ["tenant_id", "application_id"],
        unique=True,
        postgresql_where=sa.text("is_primary IS TRUE"),
    )

    op.create_table(
        "application_volumes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("volume_key", sa.String(length=80), nullable=False),
        sa.Column("requested_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "storage_policy",
            sa.String(length=24),
            server_default="managed",
            nullable=False,
        ),
        sa.Column(
            "backup_policy",
            sa.String(length=24),
            server_default="none",
            nullable=False,
        ),
        sa.Column(
            "delete_policy",
            sa.String(length=24),
            server_default="retain",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "requested_bytes > 0",
            name=op.f("ck_application_volumes_requested_bytes_positive"),
        ),
        sa.CheckConstraint(
            "storage_policy = 'managed'",
            name=op.f("ck_application_volumes_managed_storage_only"),
        ),
        sa.CheckConstraint(
            "backup_policy IN ('none','manual','scheduled')",
            name=op.f("ck_application_volumes_backup_policy"),
        ),
        sa.CheckConstraint(
            "delete_policy IN ('retain','delete')",
            name=op.f("ck_application_volumes_delete_policy"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','provisioning','ready','failed','retained','deleted')",
            name=op.f("ck_application_volumes_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_application_volumes_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_application_volumes_tenant_application",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_volumes")),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "volume_key",
            name="uq_application_volumes_app_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_application_volumes_app_id",
        ),
    )
    op.create_index(
        "ix_application_volumes_tenant_app",
        "application_volumes",
        ["tenant_id", "application_id"],
    )

    op.create_table(
        "app_environment",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("service_scope", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("value_checksum", sa.LargeBinary(length=32), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column(
            "is_sensitive",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "source", sa.String(length=24), server_default="user", nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "octet_length(value_ciphertext) BETWEEN 1 AND 1048576",
            name=op.f("ck_app_environment_ciphertext_size"),
        ),
        sa.CheckConstraint(
            "octet_length(value_checksum) = 32",
            name=op.f("ck_app_environment_checksum_length"),
        ),
        sa.CheckConstraint(
            "key_version > 0",
            name=op.f("ck_app_environment_key_version_positive"),
        ),
        sa.CheckConstraint(
            "source IN ('user','analysis','template','system')",
            name=op.f("ck_app_environment_source"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_app_environment_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
            name="fk_app_environment_tenant_application",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "application_id",
            "service_scope",
            "name",
            name=op.f("pk_app_environment"),
        ),
    )

    op.create_table(
        "app_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.BigInteger(), nullable=False),
        sa.Column("analysis_id", sa.String(length=64), nullable=False),
        sa.Column("source_revision_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("deployment_plan_artifact_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_plan_digest", sa.String(length=71), nullable=False),
        sa.Column("normalized_compose_digest", sa.String(length=71), nullable=True),
        sa.Column("luma_manifest_digest", sa.String(length=71), nullable=False),
        sa.Column("environment_schema_digest", sa.String(length=71), nullable=False),
        sa.Column("environment_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="candidate",
            nullable=False,
        ),
        sa.Column("created_by_type", sa.String(length=32), nullable=False),
        sa.Column("created_by_id", sa.String(length=64), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "revision_no > 0", name=op.f("ck_app_revisions_revision_no_positive")
        ),
        sa.CheckConstraint(
            "kind IN ('service','compose')", name=op.f("ck_app_revisions_kind")
        ),
        sa.CheckConstraint(
            "status IN ('candidate','active','superseded','failed')",
            name=op.f("ck_app_revisions_status"),
        ),
        sa.CheckConstraint(
            "environment_version >= 0",
            name=op.f("ck_app_revisions_environment_version"),
        ),
        sa.CheckConstraint(
            "deployment_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_app_revisions_deployment_plan_digest"),
        ),
        sa.CheckConstraint(
            "normalized_compose_digest IS NULL OR "
            "normalized_compose_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_app_revisions_normalized_compose_digest"),
        ),
        sa.CheckConstraint(
            "luma_manifest_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_app_revisions_luma_manifest_digest"),
        ),
        sa.CheckConstraint(
            "environment_schema_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_app_revisions_environment_schema_digest"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_app_revisions_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "analysis_id"],
            ["analyses.tenant_id", "analyses.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_analysis",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_plan_artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            ondelete="RESTRICT",
            name="fk_app_revisions_tenant_plan_artifact",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_revisions")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_app_revisions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "id",
            name="uq_app_revisions_app_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "application_id",
            "revision_no",
            name="uq_app_revisions_app_number",
        ),
    )
    op.create_index(
        "ix_app_revisions_tenant_app_created",
        "app_revisions",
        ["tenant_id", "application_id", "created_at"],
    )

    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("luma_cluster_id", sa.String(length=128), nullable=True),
        sa.Column("luma_external_ref", sa.String(length=512), nullable=True),
        sa.Column("previous_deployment_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued','building','deploying','verifying','succeeded','failed','canceled')",
            name=op.f("ck_deployments_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','canceled')) = (finished_at IS NOT NULL)",
            name=op.f("ck_deployments_terminal_finished"),
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name=op.f("ck_deployments_failure_error_code"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_deployments_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "revision_id"],
            [
                "app_revisions.tenant_id",
                "app_revisions.application_id",
                "app_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_app_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["operations.tenant_id", "operations.id"],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_operation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id", "previous_deployment_id"],
            ["deployments.tenant_id", "deployments.application_id", "deployments.id"],
            ondelete="RESTRICT",
            name="fk_deployments_tenant_app_previous",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_deployments_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "application_id", "id", name="uq_deployments_app_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", name="uq_deployments_tenant_operation"
        ),
    )
    op.create_index(
        "ix_deployments_tenant_app_created",
        "deployments",
        ["tenant_id", "application_id", "created_at"],
    )

    op.create_foreign_key(
        "fk_applications_tenant_current_revision",
        "applications",
        "app_revisions",
        ["tenant_id", "current_revision_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_applications_tenant_current_deployment",
        "applications",
        "deployments",
        ["tenant_id", "current_deployment_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # The 0003 schema cannot represent a pending application. A pending shell
    # has no materialized revision/deployment topology and owns no deployed
    # workload, so the explicit lossy downgrade boundary is to remove those
    # drafts plus their source-analysis checkpoints. Converting them to
    # ``service`` would manufacture a topology that was never verified.
    #
    # Keep the operation rows as immutable audit history, but remove their
    # idempotency responses so an older API cannot replay a successful create
    # for an application removed by this contraction.
    op.execute(
        """
        CREATE TEMPORARY TABLE lae_0004_pending_operations
        ON COMMIT DROP AS
        SELECT DISTINCT o.tenant_id, o.id
        FROM operations AS o
        WHERE EXISTS (
            SELECT 1
            FROM applications AS a
            WHERE a.kind = 'pending'
              AND a.tenant_id = o.tenant_id
              AND o.target_type = 'application'
              AND o.target_id = a.id
        ) OR EXISTS (
            SELECT 1
            FROM source_revisions AS s
            JOIN applications AS a
              ON a.tenant_id = s.tenant_id
             AND a.id = s.application_id
             AND a.kind = 'pending'
            WHERE s.tenant_id = o.tenant_id
              AND o.target_type = 'source-revision'
              AND o.target_id = s.id
        ) OR EXISTS (
            SELECT 1
            FROM analyses AS n
            JOIN applications AS a
              ON a.tenant_id = n.tenant_id
             AND a.id = n.application_id
             AND a.kind = 'pending'
            WHERE n.tenant_id = o.tenant_id
              AND n.operation_id = o.id
        )
        """
    )
    op.execute(
        """
        DELETE FROM idempotency_records AS i
        USING lae_0004_pending_operations AS p
        WHERE i.tenant_id = p.tenant_id
          AND i.operation_id = p.id
        """
    )
    op.execute(
        """
        DELETE FROM source_credential_leases AS l
        USING source_revisions AS s, applications AS a
        WHERE l.tenant_id = s.tenant_id
          AND l.source_revision_id = s.id
          AND s.tenant_id = a.tenant_id
          AND s.application_id = a.id
          AND a.kind = 'pending'
        """
    )
    op.execute(
        """
        DELETE FROM builder_tasks AS b
        USING applications AS a
        WHERE b.tenant_id = a.tenant_id
          AND b.application_id = a.id
          AND a.kind = 'pending'
        """
    )
    op.execute(
        """
        DELETE FROM analyses AS n
        USING applications AS a
        WHERE n.tenant_id = a.tenant_id
          AND n.application_id = a.id
          AND a.kind = 'pending'
        """
    )
    op.execute(
        """
        DELETE FROM source_revisions AS s
        USING applications AS a
        WHERE s.tenant_id = a.tenant_id
          AND s.application_id = a.id
          AND a.kind = 'pending'
        """
    )
    op.execute("DELETE FROM applications WHERE kind = 'pending'")

    op.drop_constraint(
        "fk_applications_tenant_current_deployment",
        "applications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_applications_tenant_current_revision",
        "applications",
        type_="foreignkey",
    )
    op.drop_column("applications", "current_deployment_id")
    op.drop_column("applications", "current_revision_id")

    op.drop_index("ix_deployments_tenant_app_created", table_name="deployments")
    op.drop_table("deployments")
    op.drop_index("ix_app_revisions_tenant_app_created", table_name="app_revisions")
    op.drop_table("app_revisions")
    op.drop_table("app_environment")
    op.drop_index("ix_application_volumes_tenant_app", table_name="application_volumes")
    op.drop_table("application_volumes")
    op.drop_index("uq_application_routes_primary", table_name="application_routes")
    op.drop_index("ix_application_routes_tenant_app", table_name="application_routes")
    op.drop_table("application_routes")
    op.drop_index(
        "ix_application_services_tenant_app", table_name="application_services"
    )
    op.drop_table("application_services")
    op.drop_constraint(
        op.f("ck_applications_environment_version"),
        "applications",
        type_="check",
    )
    op.drop_column("applications", "environment_version")
    op.drop_constraint(op.f("ck_applications_kind"), "applications", type_="check")
    op.create_check_constraint(
        op.f("ck_applications_kind"),
        "applications",
        "kind IN ('service','compose')",
    )
