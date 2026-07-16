"""Versioned billing orders and signed mock payment events.

Revision ID: 20260711_0005
Revises: 20260711_0004
Create Date: 2026-07-11

Prices are deliberately absent from this migration.  Until commercial pricing
is approved, development/test loads a versioned price snapshot from
``LAE_MOCK_PRICING_JSON`` and persists the selected snapshot on each order.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0005"
down_revision: str | None = "20260711_0004"
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
    plans = sa.table(
        "plan_versions",
        sa.column("id", sa.String()),
        sa.column("code", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("limits_json", postgresql.JSONB()),
        sa.column("features_json", postgresql.JSONB()),
    )
    # These are explicitly capacity-planning drafts, not commercial promises.
    # Immutable versions let production replace them without silently changing
    # an order or an existing subscription.
    op.bulk_insert(
        plans,
        [
            {
                "id": "pln_00000000000000000000000002",
                "code": "pro",
                "version": 1,
                "limits_json": {
                    "applications": 20,
                    "servicesPerApp": 20,
                    "publicHttpRoutesPerApp": 8,
                    "persistentVolumeBytes": 50 * 1024 * 1024 * 1024,
                    "concurrentAnalyses": 2,
                    "concurrentBuilds": 2,
                    "concurrentDeployments": 2,
                },
                "features_json": {
                    "privateGit": True,
                    "manualUpdateChecks": True,
                    "scheduledUpdateChecks": True,
                },
            },
            {
                "id": "pln_00000000000000000000000003",
                "code": "ultra",
                "version": 1,
                "limits_json": {
                    "applications": 100,
                    "servicesPerApp": 50,
                    "publicHttpRoutesPerApp": 20,
                    "persistentVolumeBytes": 200 * 1024 * 1024 * 1024,
                    "concurrentAnalyses": 5,
                    "concurrentBuilds": 5,
                    "concurrentDeployments": 5,
                },
                "features_json": {
                    "privateGit": True,
                    "manualUpdateChecks": True,
                    "scheduledUpdateChecks": True,
                    "policyUpdateChecks": True,
                    "audit": True,
                },
            },
        ],
    )

    op.create_table(
        "billing_orders",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_merchant_id", sa.String(length=128), nullable=False),
        sa.Column("plan_version_id", sa.String(length=64), nullable=False),
        sa.Column("plan_code", sa.String(length=24), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("pricing_version", sa.String(length=64), nullable=False),
        sa.Column(
            "pricing_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("idempotency_key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("checkout_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_subscription_id", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "principal_type IN ('session','deploy-token')",
            name=op.f("ck_billing_orders_principal_type"),
        ),
        sa.CheckConstraint(
            "provider IN ('mock','wechat_pay','alipay')",
            name=op.f("ck_billing_orders_provider"),
        ),
        sa.CheckConstraint(
            "plan_code IN ('pro','ultra')",
            name=op.f("ck_billing_orders_plan_code"),
        ),
        sa.CheckConstraint(
            "interval IN ('monthly','yearly')",
            name=op.f("ck_billing_orders_interval"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_billing_orders_currency")
        ),
        sa.CheckConstraint(
            "amount_minor > 0", name=op.f("ck_billing_orders_amount_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(pricing_snapshot) = 'object'",
            name=op.f("ck_billing_orders_pricing_snapshot_object"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','paid','failed','expired','canceled')",
            name=op.f("ck_billing_orders_status"),
        ),
        sa.CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name=op.f("ck_billing_orders_idempotency_key_hash_length"),
        ),
        sa.CheckConstraint(
            "octet_length(request_hash) = 32",
            name=op.f("ck_billing_orders_request_hash_length"),
        ),
        sa.CheckConstraint(
            "(status = 'paid') = (paid_subscription_id IS NOT NULL)",
            name=op.f("ck_billing_orders_paid_subscription"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_billing_orders_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            ondelete="RESTRICT",
            name=op.f("fk_billing_orders_plan_version_id_plan_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["paid_subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
            name=op.f("fk_billing_orders_paid_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_orders")),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_billing_orders_tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_type",
            "principal_id",
            "idempotency_key_hash",
            name="uq_billing_orders_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_billing_orders_tenant_created",
        "billing_orders",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_billing_orders_pending_expiry",
        "billing_orders",
        ["checkout_expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "billing_payment_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_principal_id", sa.String(length=128), nullable=False),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("reported_order_id", sa.String(length=64), nullable=False),
        sa.Column("reported_merchant_id", sa.String(length=128), nullable=False),
        sa.Column("reported_plan_code", sa.String(length=24), nullable=False),
        sa.Column("reported_interval", sa.String(length=16), nullable=False),
        sa.Column("reported_currency", sa.String(length=3), nullable=False),
        sa.Column("reported_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("reported_outcome", sa.String(length=24), nullable=False),
        sa.Column("provider_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_status", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("order_status_after", sa.String(length=24), nullable=False),
        sa.Column("subscription_id", sa.String(length=64), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "provider IN ('mock','wechat_pay','alipay')",
            name=op.f("ck_billing_payment_events_provider"),
        ),
        sa.CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name=op.f(
                "ck_billing_payment_events_idempotency_key_hash_length"
            ),
        ),
        sa.CheckConstraint(
            "octet_length(payload_hash) = 32",
            name=op.f("ck_billing_payment_events_payload_hash_length"),
        ),
        sa.CheckConstraint(
            "reported_plan_code IN ('pro','ultra')",
            name=op.f("ck_billing_payment_events_reported_plan_code"),
        ),
        sa.CheckConstraint(
            "reported_interval IN ('monthly','yearly')",
            name=op.f("ck_billing_payment_events_reported_interval"),
        ),
        sa.CheckConstraint(
            "reported_currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_billing_payment_events_reported_currency"),
        ),
        sa.CheckConstraint(
            "reported_amount_minor > 0",
            name=op.f("ck_billing_payment_events_reported_amount_positive"),
        ),
        sa.CheckConstraint(
            "reported_outcome IN ('paid','failed','expired','canceled')",
            name=op.f("ck_billing_payment_events_reported_outcome"),
        ),
        sa.CheckConstraint(
            "processing_status IN ('accepted','rejected','ignored')",
            name=op.f("ck_billing_payment_events_processing_status"),
        ),
        sa.CheckConstraint(
            "order_status_after IN ('pending','paid','failed','expired','canceled')",
            name=op.f("ck_billing_payment_events_order_status_after"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name=op.f("fk_billing_payment_events_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["billing_orders.tenant_id", "billing_orders.id"],
            ondelete="RESTRICT",
            name="fk_billing_payment_events_tenant_order",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
            name=op.f(
                "fk_billing_payment_events_subscription_id_subscriptions"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_payment_events")),
        sa.UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_billing_payment_events_provider_event",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_principal_id",
            "idempotency_key_hash",
            name="uq_billing_payment_events_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_billing_payment_events_tenant_order_created",
        "billing_payment_events",
        ["tenant_id", "order_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_payment_events_tenant_order_created",
        table_name="billing_payment_events",
    )
    op.drop_table("billing_payment_events")
    op.drop_index("ix_billing_orders_pending_expiry", table_name="billing_orders")
    op.drop_index("ix_billing_orders_tenant_created", table_name="billing_orders")
    op.drop_table("billing_orders")
    # Preserve an immutable plan version when a retained subscription already
    # references it.  A clean downgrade removes the seed rows; downgrade-to-
    # base later drops the whole plan table either way.
    op.execute(
        sa.text(
            "DELETE FROM plan_versions p "
            "WHERE p.id IN ('pln_00000000000000000000000002', "
            "'pln_00000000000000000000000003') "
            "AND NOT EXISTS ("
            "SELECT 1 FROM subscriptions s WHERE s.plan_version_id = p.id"
            ")"
        )
    )
