"""Encrypted private Git source connections and bound credential leases.

Revision ID: 20260711_0006
Revises: 20260711_0005
Create Date: 2026-07-11

Only AES-GCM ciphertext, nonce and keyed checksums are durable.  Plaintext
credentials remain ephemeral and source-analysis tasks continue to carry only
an opaque credential lease identifier.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260711_0006"
down_revision: str | None = "20260711_0005"
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
    op.create_table(
        "source_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("allowed_host", sa.String(length=260), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("secret_checksum", sa.LargeBinary(length=32), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column(
            "credential_version", sa.BigInteger(), server_default="1", nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "provider IN ('github','gitea','generic')",
            name=op.f("ck_source_connections_provider"),
        ),
        sa.CheckConstraint(
            "octet_length(secret_ciphertext) BETWEEN 16 AND 8192",
            name=op.f("ck_source_connections_secret_ciphertext_size"),
        ),
        sa.CheckConstraint(
            "octet_length(secret_nonce) = 12",
            name=op.f("ck_source_connections_secret_nonce_length"),
        ),
        sa.CheckConstraint(
            "octet_length(secret_checksum) = 32",
            name=op.f("ck_source_connections_secret_checksum_length"),
        ),
        sa.CheckConstraint(
            "key_version > 0",
            name=op.f("ck_source_connections_key_version_positive"),
        ),
        sa.CheckConstraint(
            "credential_version > 0",
            name=op.f("ck_source_connections_credential_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_source_connections_tenant_id_tenants"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_connections")),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_source_connections_tenant_id_id"
        ),
    )
    op.create_index(
        "ix_source_connections_tenant_created",
        "source_connections",
        ["tenant_id", "created_at"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_source_revisions_tenant_connection",
        "source_revisions",
        "source_connections",
        ["tenant_id", "connection_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "source_credential_leases",
        sa.Column("consumer_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "source_credential_leases",
        sa.Column("consumer_binding_hash", sa.LargeBinary(length=32), nullable=True),
    )
    op.add_column(
        "source_credential_leases",
        sa.Column("binding_key_version", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE source_credential_leases AS lease "
            "SET consumer_id = task.luma_principal_id "
            "FROM builder_tasks AS task "
            "WHERE task.tenant_id = lease.tenant_id "
            "AND task.id = lease.builder_task_id"
        )
    )
    op.alter_column(
        "source_credential_leases",
        "consumer_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_source_credential_leases_consumer_binding_hash_length"),
        "source_credential_leases",
        "consumer_binding_hash IS NULL OR octet_length(consumer_binding_hash) = 32",
    )
    op.create_check_constraint(
        op.f("ck_source_credential_leases_consumer_binding_fields_together"),
        "source_credential_leases",
        "(consumer_binding_hash IS NULL) = (binding_key_version IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_source_credential_leases_private_connection_has_consumer_binding"),
        "source_credential_leases",
        "source_connection_id IS NULL OR consumer_binding_hash IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_source_credential_leases_binding_key_version_positive"),
        "source_credential_leases",
        "binding_key_version IS NULL OR binding_key_version > 0",
    )
    op.create_foreign_key(
        "fk_source_credential_leases_tenant_connection",
        "source_credential_leases",
        "source_connections",
        ["tenant_id", "source_connection_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_source_credential_leases_tenant_connection",
        "source_credential_leases",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_source_credential_leases_binding_key_version_positive"),
        "source_credential_leases",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_source_credential_leases_private_connection_has_consumer_binding"),
        "source_credential_leases",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_source_credential_leases_consumer_binding_fields_together"),
        "source_credential_leases",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_source_credential_leases_consumer_binding_hash_length"),
        "source_credential_leases",
        type_="check",
    )
    op.drop_column("source_credential_leases", "binding_key_version")
    op.drop_column("source_credential_leases", "consumer_binding_hash")
    op.drop_column("source_credential_leases", "consumer_id")

    op.drop_constraint(
        "fk_source_revisions_tenant_connection",
        "source_revisions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_source_connections_tenant_created", table_name="source_connections"
    )
    op.drop_table("source_connections")
