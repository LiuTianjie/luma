from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_store.application_catalog import (  # noqa: E402
    CreateApplication,
    CreateApplicationDraft,
    EncryptedEnvironmentValue,
    EnvironmentVariableMetadata,
    HttpRouteSpec,
    MaterializeApplicationTopology,
    ServiceSpec,
    VolumeSpec,
    new_managed_hostname,
    tenant_application_quota_lock_statement,
)
from lae_store.errors import CustomDomainUnsupported  # noqa: E402
from lae_store.ids import new_id  # noqa: E402
from lae_store.models import Application, ApplicationRoute  # noqa: E402
from lae_store.repositories import TenantScope  # noqa: E402


class ApplicationCatalogDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = TenantScope(new_id("ten"))

    def test_managed_hostname_is_lowercase_128_bit_label(self) -> None:
        first = new_managed_hostname()
        second = new_managed_hostname()
        self.assertRegex(first, r"^[0-9a-f]{32}\.itool\.tech$")
        self.assertNotEqual(first, second)

    def test_custom_hostname_is_rejected_at_command_boundary(self) -> None:
        with self.assertRaises(CustomDomainUnsupported):
            HttpRouteSpec(
                service_key="web",
                container_port=8080,
                requested_hostname="customer.example.com",
            )

    def test_compose_accepts_multiple_public_http_services(self) -> None:
        command = CreateApplication(
            scope=self.scope,
            name="Compose App",
            slug="compose-app",
            kind="compose",
            services=(
                ServiceSpec("web", "http"),
                ServiceSpec("admin", "http"),
                ServiceSpec("db", "datastore"),
            ),
            routes=(
                HttpRouteSpec("web", 8080, is_primary=True),
                HttpRouteSpec("admin", 9090),
            ),
            volumes=(VolumeSpec("database", 1024),),
        )
        self.assertEqual(len(command.routes), 2)
        self.assertEqual(
            {item.service_key for item in command.routes}, {"web", "admin"}
        )

    def test_draft_has_no_fake_topology_and_materialization_is_explicit(self) -> None:
        draft = CreateApplicationDraft(
            scope=self.scope,
            name="Unanalyzed App",
            slug="unanalyzed-app",
        )
        self.assertEqual(
            {field.name for field in dataclasses.fields(draft)},
            {"scope", "name", "slug"},
        )
        topology = MaterializeApplicationTopology(
            scope=self.scope,
            application_id=new_id("app"),
            analysis_id=new_id("ana"),
            kind="compose",
            services=(ServiceSpec("web", "http"), ServiceSpec("db", "datastore")),
            routes=(HttpRouteSpec("web", 8080, is_primary=True),),
        )
        self.assertEqual(topology.kind, "compose")

    def test_application_kind_allows_pending_shell(self) -> None:
        constraint_sql = " ".join(
            str(constraint.sqltext)
            for constraint in Application.__table__.constraints
            if getattr(constraint, "name", None) == "ck_applications_kind"
        )
        self.assertIn("pending", constraint_sql)

    def test_non_http_public_transport_has_no_domain_field(self) -> None:
        route_fields = {field.name for field in dataclasses.fields(HttpRouteSpec)}
        table_columns = set(ApplicationRoute.__table__.columns.keys())
        for unsupported in {"protocol", "host_port", "tcp_relay", "udp_port"}:
            self.assertNotIn(unsupported, route_fields)
            self.assertNotIn(unsupported, table_columns)
        self.assertEqual(ApplicationRoute.__table__.c.kind.server_default.arg, "http")

    def test_environment_public_record_cannot_carry_secret_bytes(self) -> None:
        public_fields = {
            field.name for field in dataclasses.fields(EnvironmentVariableMetadata)
        }
        self.assertNotIn("envelope_ciphertext", public_fields)
        self.assertNotIn("checksum", public_fields)
        self.assertNotIn("value", public_fields)

        with self.assertRaisesRegex(ValueError, "ciphertext"):
            EncryptedEnvironmentValue(
                service_scope="*",
                name="DATABASE_URL",
                envelope_ciphertext="secret",  # type: ignore[arg-type]
                checksum=b"x" * 32,
                key_version=1,
            )

    def test_desired_and_observed_state_are_distinct_columns(self) -> None:
        columns = set(Application.__table__.columns.keys())
        self.assertIn("desired_state", columns)
        self.assertIn("observed_state", columns)
        self.assertNotEqual(
            Application.__table__.c.desired_state,
            Application.__table__.c.observed_state,
        )

    def test_quota_lock_is_postgresql_transaction_advisory_lock(self) -> None:
        sql = str(
            tenant_application_quota_lock_statement().compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("hashtextextended", sql)


if __name__ == "__main__":
    unittest.main()
