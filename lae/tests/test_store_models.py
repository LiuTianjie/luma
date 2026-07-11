from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_store.engine import create_postgres_engine  # noqa: E402
from lae_store.models import (  # noqa: E402
    AppRevision,
    ApplicationEnvironmentVariable,
    ApplicationLifecycleRequest,
    ApplicationRoute,
    ApplicationService,
    ApplicationVolume,
    Base,
    BillingOrder,
    BillingPaymentEvent,
    DeployToken,
    Operation,
    SourceConnection,
)


class StoreModelTests(unittest.TestCase):
    def test_foundation_tables_exist(self) -> None:
        self.assertEqual(
            set(Base.metadata.tables),
            {
                "users",
                "tenants",
                "tenant_members",
                "email_challenges",
                "auth_sessions",
                "plan_versions",
                "subscriptions",
                "deploy_tokens",
                "applications",
                "application_services",
                "application_routes",
                "application_volumes",
                "app_environment",
                "source_revisions",
                "source_connections",
                "operations",
                "operation_events",
                "idempotency_records",
                "outbox_events",
                "builder_tasks",
                "source_credential_leases",
                "analyses",
                "artifacts",
                "analysis_artifacts",
                "app_revisions",
                "deployments",
                "application_lifecycle_requests",
                "deployment_quota_reservations",
                "deployment_checkpoints",
                "deployment_build_outputs",
                "billing_orders",
                "billing_payment_events",
                "uploads",
            },
        )

    def test_postgresql_specific_types_and_timestamptz_compile(self) -> None:
        dialect = postgresql.dialect()
        operation_ddl = str(CreateTable(Operation.__table__).compile(dialect=dialect))
        token_ddl = str(CreateTable(DeployToken.__table__).compile(dialect=dialect))
        self.assertIn("TIMESTAMP WITH TIME ZONE", operation_ddl)
        self.assertIn("JSONB", operation_ddl)
        self.assertIn("BYTEA", token_ddl)
        self.assertIn("INET", token_ddl)

    def test_active_application_mutation_is_a_postgres_partial_unique_index(
        self,
    ) -> None:
        index = next(
            item
            for item in Operation.__table__.indexes
            if item.name == "uq_operations_active_application_mutation"
        )
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        self.assertIn("CREATE UNIQUE INDEX", ddl)
        self.assertIn("target_type = 'application'", ddl)
        self.assertIn("deployment.create", ddl)
        self.assertIn("application.check-update", ddl)
        self.assertIn("status IN ('queued','running')", ddl)
        self.assertNotIn("build.service", ddl)

    def test_lifecycle_request_schema_contains_bindings_not_source_credentials(
        self,
    ) -> None:
        columns = set(ApplicationLifecycleRequest.__table__.columns.keys())
        self.assertTrue(
            {
                "operation_id",
                "application_id",
                "base_source_revision_id",
                "source_revision_id",
                "source_deployment_id",
                "rollback_deployment_id",
                "analysis_id",
            }.issubset(columns)
        )
        for forbidden in ("repository", "ref", "token", "secret", "credential"):
            self.assertNotIn(forbidden, columns)

    def test_default_deploy_token_is_unique_only_while_active(self) -> None:
        index = next(
            item
            for item in DeployToken.__table__.indexes
            if item.name == "uq_deploy_tokens_active_default"
        )
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        self.assertIn("CREATE UNIQUE INDEX", ddl)
        self.assertIn("is_default IS TRUE", ddl)
        self.assertIn("revoked_at IS NULL", ddl)

    def test_no_sqlite_fallback_is_accepted(self) -> None:
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            create_postgres_engine("sqlite+aiosqlite:///:memory:")

    def test_application_catalog_indexes_match_migration_contract(self) -> None:
        expected = {
            (ApplicationService, "ix_application_services_tenant_app"),
            (ApplicationRoute, "ix_application_routes_tenant_app"),
            (ApplicationVolume, "ix_application_volumes_tenant_app"),
            (AppRevision, "ix_app_revisions_tenant_app_created"),
        }
        for model, name in expected:
            self.assertIn(name, {index.name for index in model.__table__.indexes})

    def test_environment_schema_has_no_plaintext_column(self) -> None:
        columns = set(ApplicationEnvironmentVariable.__table__.columns.keys())
        self.assertIn("value_ciphertext", columns)
        self.assertIn("value_checksum", columns)
        self.assertIn("key_version", columns)
        self.assertNotIn("value", columns)
        self.assertNotIn("plaintext", columns)

    def test_source_connection_schema_has_only_encrypted_secret_material(self) -> None:
        columns = set(SourceConnection.__table__.columns.keys())
        self.assertIn("secret_ciphertext", columns)
        self.assertIn("secret_nonce", columns)
        self.assertIn("secret_checksum", columns)
        self.assertIn("key_version", columns)
        for forbidden in (
            "secret",
            "token",
            "password",
            "plaintext",
            "private_token",
        ):
            self.assertNotIn(forbidden, columns)

    def test_billing_schema_persists_hashes_and_server_price_snapshot_only(self) -> None:
        order_columns = set(BillingOrder.__table__.columns.keys())
        event_columns = set(BillingPaymentEvent.__table__.columns.keys())
        self.assertIn("idempotency_key_hash", order_columns)
        self.assertIn("request_hash", order_columns)
        self.assertIn("pricing_snapshot", order_columns)
        self.assertIn("idempotency_key_hash", event_columns)
        self.assertIn("payload_hash", event_columns)
        for forbidden in ("token", "secret", "plaintext", "callback_signature"):
            self.assertNotIn(forbidden, order_columns)
            self.assertNotIn(forbidden, event_columns)


if __name__ == "__main__":
    unittest.main()
