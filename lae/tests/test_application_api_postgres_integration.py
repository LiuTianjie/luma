from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_api.application_api import (  # noqa: E402
    ApplicationApiService,
    ApplicationCreateRequest,
    EnvironmentPatchRequest,
)
from lae_store import (  # noqa: E402
    ApplicationCatalogStore,
    ApplicationQuotaExceeded,
    EnvironmentKeyRing,
    EnvironmentVersionConflict,
    IdempotencyKeyReused,
    ResourceNotFound,
    TenantScope,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_store.models import (  # noqa: E402
    Application,
    ApplicationEnvironmentVariable,
    IdempotencyRecord,
    Operation,
    PlanVersion,
    Subscription,
    Tenant,
    TenantMember,
    User,
)

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"


def _alembic_config() -> Config:
    assert Config is not None
    config = Config(str(LAE_ROOT / "migrations" / "alembic.ini"))
    config.set_main_option("script_location", str(LAE_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", DSN)
    return config


def _upgrade() -> None:
    assert command is not None
    command.upgrade(_alembic_config(), "head")


def _downgrade() -> None:
    assert command is not None
    command.downgrade(_alembic_config(), "base")


@unittest.skipUnless(
    DSN and DDL_ALLOWED and command is not None,
    "set LAE_TEST_POSTGRES_DSN and LAE_TEST_POSTGRES_ALLOW_DDL=1 for real PostgreSQL",
)
class ApplicationApiPostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.catalog = ApplicationCatalogStore(self.sessions)
        self.crypto = EnvironmentKeyRing(
            current_version=1,
            keys={1: b"e" * 32},
            checksum_key=b"c" * 32,
        )
        self.service = ApplicationApiService(
            self.catalog,
            self.crypto,
            idempotency_hash_key=b"i" * 32,
        )
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.other_tenant_id = new_id("ten")
        self.scope = TenantScope(self.tenant_id)
        self.other_scope = TenantScope(self.other_tenant_id)
        self.principal = SimpleNamespace(
            credential_type="deploy_token", credential_id=new_id("dtk")
        )
        self.other_principal = SimpleNamespace(
            credential_type="deploy_token", credential_id=new_id("dtk")
        )
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    User(
                        id=self.user_id,
                        email=f"{self.user_id.lower()}@example.test",
                        status="active",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        Tenant(
                            id=self.tenant_id,
                            type="personal",
                            name="Application API Tenant",
                            slug=self.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_tenant_id,
                            type="organization",
                            name="Other Tenant",
                            slug=self.other_tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        TenantMember(
                            tenant_id=self.tenant_id,
                            user_id=self.user_id,
                            role="owner",
                        ),
                        TenantMember(
                            tenant_id=self.other_tenant_id,
                            user_id=self.user_id,
                            role="owner",
                        ),
                    ]
                )
                plan_id = await session.scalar(
                    select(PlanVersion.id).where(
                        PlanVersion.code == "lite", PlanVersion.version == 1
                    )
                )
                assert plan_id is not None
                session.add(
                    Subscription(
                        id=new_id("sub"),
                        tenant_id=self.tenant_id,
                        plan_version_id=plan_id,
                        interval="none",
                        status="active",
                        provider="internal",
                    )
                )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    async def test_atomic_create_replay_conflict_tenant_fence_and_quota(self) -> None:
        payload = ApplicationCreateRequest(name="First", slug="first")
        first, second = await asyncio.gather(
            self.service.create(self.scope, self.principal, payload, "create-first"),
            self.service.create(self.scope, self.principal, payload, "create-first"),
        )
        self.assertEqual(first.response_body, second.response_body)
        self.assertEqual({first.replayed, second.replayed}, {False, True})
        application_id = first.response_body["application"]["id"]
        self.assertEqual(first.response_body["application"]["kind"], "pending")
        serialized = json.dumps(first.response_body).lower()
        self.assertNotIn("tenant", serialized)
        self.assertNotIn("luma", serialized)

        async with self.sessions() as session:
            application_count = await session.scalar(
                select(func.count()).select_from(Application)
            )
            idempotency = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.key == "create-first"
                )
            )
            operation = await session.scalar(
                select(Operation).where(Operation.id == idempotency.operation_id)
            )
        self.assertEqual(application_count, 1)
        self.assertIsNotNone(idempotency)
        self.assertEqual(idempotency.response_status, 201)
        self.assertEqual(len(idempotency.request_hash), 32)
        self.assertEqual(operation.kind, "application.create")
        self.assertEqual(operation.status, "succeeded")

        with self.assertRaises(IdempotencyKeyReused):
            await self.service.create(
                self.scope,
                self.principal,
                ApplicationCreateRequest(name="Different", slug="different"),
                "create-first",
            )
        with self.assertRaises(ResourceNotFound):
            await self.catalog.get_application(self.other_scope, application_id)

        await self.service.create(
            self.scope,
            self.principal,
            ApplicationCreateRequest(name="Second", slug="second"),
            "create-second",
        )
        await self.service.create(
            self.scope,
            self.principal,
            ApplicationCreateRequest(name="Third", slug="third"),
            "create-third",
        )
        with self.assertRaises(ApplicationQuotaExceeded):
            await self.service.create(
                self.scope,
                self.principal,
                ApplicationCreateRequest(name="Fourth", slug="fourth"),
                "create-fourth",
            )
        async with self.sessions() as session:
            count_after_rejection = await session.scalar(
                select(func.count()).select_from(Application)
            )
            rejected_idempotency = await session.scalar(
                select(IdempotencyRecord.id).where(
                    IdempotencyRecord.key == "create-fourth"
                )
            )
        self.assertEqual(count_after_rejection, 3)
        self.assertIsNone(rejected_idempotency)

    async def test_environment_cas_encryption_replay_and_durable_redaction(self) -> None:
        created = await self.service.create(
            self.scope,
            self.principal,
            ApplicationCreateRequest(name="Environment", slug="environment"),
            "create-environment",
        )
        application_id = created.response_body["application"]["id"]
        secret = "postgres://user:password@example.test/app"
        payload = EnvironmentPatchRequest.model_validate(
            {
                "expectedVersion": 0,
                "set": {
                    "*:DATABASE_URL": {"value": secret, "sensitive": True},
                    "*:NODE_ENV": {"value": "production", "sensitive": False},
                },
                "unset": [],
            }
        )
        changed = await self.service.patch_environment(
            self.scope,
            self.principal,
            application_id,
            payload,
            "environment-first",
        )
        replay = await self.service.patch_environment(
            self.scope,
            self.principal,
            application_id,
            payload,
            "environment-first",
        )
        self.assertFalse(changed.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(changed.response_body, replay.response_body)
        self.assertEqual(changed.response_body["environment"]["version"], 1)
        public_json = json.dumps(changed.response_body)
        for forbidden in (secret, "production", "ciphertext", "checksum", "keyVersion"):
            self.assertNotIn(forbidden, public_json)

        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ApplicationEnvironmentVariable).where(
                        ApplicationEnvironmentVariable.application_id == application_id
                    )
                )
            )
            application = await session.scalar(
                select(Application).where(Application.id == application_id)
            )
            idempotency = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.key == "environment-first"
                )
            )
        self.assertEqual(application.environment_version, 1)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.key_version, 1)
            self.assertEqual(len(row.value_checksum), 32)
            self.assertNotIn(secret.encode(), row.value_ciphertext)
            self.assertNotIn(b"production", row.value_ciphertext)
        durable_json = json.dumps(idempotency.response_body)
        self.assertNotIn(secret, durable_json)
        self.assertNotIn("production", durable_json)
        self.assertNotIn("ciphertext", durable_json.lower())
        self.assertNotIn("checksum", durable_json.lower())

        with self.assertRaises(IdempotencyKeyReused):
            await self.service.patch_environment(
                self.scope,
                self.principal,
                application_id,
                EnvironmentPatchRequest.model_validate(
                    {
                        "expectedVersion": 0,
                        "set": {"*:DATABASE_URL": {"value": "changed-secret"}},
                        "unset": [],
                    }
                ),
                "environment-first",
            )
        with self.assertRaises(EnvironmentVersionConflict):
            await self.service.patch_environment(
                self.scope,
                self.principal,
                application_id,
                payload,
                "environment-stale",
            )
        with self.assertRaises(ResourceNotFound):
            await self.service.patch_environment(
                self.other_scope,
                self.other_principal,
                application_id,
                payload,
                "environment-foreign",
            )


if __name__ == "__main__":
    unittest.main()
