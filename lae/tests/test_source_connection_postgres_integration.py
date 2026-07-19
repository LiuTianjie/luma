from __future__ import annotations

import asyncio
import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_store import (  # noqa: E402
    CreateAnalysisRequest,
    CreateSourceConnection,
    CredentialLeaseRejected,
    IdempotencyKeyReused,
    PostgresAnalysisRequestStore,
    PostgresCredentialRedemptionBroker,
    PostgresSourceConnectionStore,
    Principal,
    ResourceNotFound,
    RevokeSourceConnection,
    RotateSourceConnection,
    CredentialRedemptionRequest,
    SourceConnectionHostMismatch,
    SourceConnectionKeyRing,
    TenantScope,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_store.models import (  # noqa: E402
    Analysis,
    Application,
    BuilderTask,
    IdempotencyRecord,
    Operation,
    SourceConnection,
    SourceCredentialLease,
    SourceRevision,
    Tenant,
    TenantMember,
    User,
)
from lae_worker import PostgresConnectionCredentialBroker  # noqa: E402

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
class PostgreSQLSourceConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.scope = TenantScope(new_id("ten"))
        self.other_scope = TenantScope(new_id("ten"))
        self.user_id = new_id("usr")
        self.other_user_id = new_id("usr")
        self.application_id = new_id("app")
        self.principal = Principal("deploy-token", new_id("dtk"))
        self.key_ring = SourceConnectionKeyRing(
            current_version=1,
            encryption_keys={1: b"a" * 32},
            hmac_keys={1: b"h" * 32},
        )
        self.connections = PostgresSourceConnectionStore(
            self.sessions,
            self.key_ring,
            idempotency_hash_key=b"i" * 32,
        )
        async with self.sessions() as session:
            async with session.begin():
                session.add_all(
                    [
                        User(
                            id=self.user_id,
                            email=f"{self.user_id.lower()}@example.test",
                            status="active",
                        ),
                        User(
                            id=self.other_user_id,
                            email=f"{self.other_user_id.lower()}@example.test",
                            status="active",
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        Tenant(
                            id=self.scope.tenant_id,
                            type="personal",
                            name="Private Git tenant",
                            slug=self.scope.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_scope.tenant_id,
                            type="personal",
                            name="Other tenant",
                            slug=self.other_scope.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.other_user_id,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        TenantMember(
                            tenant_id=self.scope.tenant_id,
                            user_id=self.user_id,
                            role="owner",
                        ),
                        TenantMember(
                            tenant_id=self.other_scope.tenant_id,
                            user_id=self.other_user_id,
                            role="owner",
                        ),
                        Application(
                            id=self.application_id,
                            tenant_id=self.scope.tenant_id,
                            name="Private source target",
                            slug="private-source-target",
                            luma_name=f"lae-{self.application_id.lower()}",
                            kind="pending",
                        ),
                    ]
                )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    async def _bind_luma_task(self, task_id: str, luma_task_id: str) -> None:
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(BuilderTask)
                    .where(BuilderTask.id == task_id)
                    .values(luma_task_id=luma_task_id, upstream_status="queued")
                )

    def create_command(
        self,
        *,
        key: str = "source-connection-create-1",
        secret: str = "private-pat-canary-v1",
    ) -> CreateSourceConnection:
        return CreateSourceConnection(
            scope=self.scope,
            principal=self.principal,
            provider="gitea",
            display_name="Production Gitea",
            base_url="https://git.example.com/gitea/",
            username="deploy",
            secret=secret,
            idempotency_key=key,
        )

    def analysis_command(
        self,
        connection_id: str,
        *,
        key: str,
        repository: str = "https://git.example.com/acme/private.git",
    ) -> CreateAnalysisRequest:
        return CreateAnalysisRequest(
            scope=self.scope,
            principal=self.principal,
            application_id=self.application_id,
            repository=repository,
            ref="main",
            subdirectory="",
            region="cn",
            public_protocols=("http",),
            connection_id=connection_id,
            idempotency_key=key,
        )

    async def test_encrypted_catalog_analysis_binding_and_single_use_broker(
        self,
    ) -> None:
        command_input = self.create_command()
        self.assertNotIn("private-pat-canary-v1", repr(command_input))
        first, replay = await asyncio.gather(
            self.connections.create(command_input),
            self.connections.create(command_input),
        )
        self.assertEqual(first.response_body, replay.response_body)
        self.assertEqual({first.replayed, replay.replayed}, {False, True})
        connection_id = first.response_body["connection"]["id"]
        public = str(first.response_body).lower()
        for forbidden in (
            "private-pat-canary-v1",
            "ciphertext",
            "nonce",
            "checksum",
            "keyversion",
        ):
            self.assertNotIn(forbidden, public)
        self.assertEqual(
            first.response_body["connection"]["baseUrl"],
            "https://git.example.com/gitea",
        )
        self.assertEqual(
            first.response_body["connection"]["allowedHost"], "git.example.com"
        )

        with self.assertRaises(IdempotencyKeyReused):
            await self.connections.create(
                self.create_command(secret="different-secret")
            )
        self.assertEqual(len(await self.connections.list(self.scope)), 1)
        self.assertEqual(await self.connections.list(self.other_scope), ())
        with self.assertRaises(ResourceNotFound):
            await self.connections.rotate(
                RotateSourceConnection(
                    scope=self.other_scope,
                    principal=Principal("deploy-token", new_id("dtk")),
                    connection_id=connection_id,
                    secret="cross-tenant-secret",
                    idempotency_key="foreign-rotate",
                )
            )

        analyses = PostgresAnalysisRequestStore(
            self.sessions,
            luma_cluster_id="luma-integration",
            luma_principal_id="lae-builder",
            hash_key=b"w" * 32,
            hash_key_version=1,
            connection_key_ring=self.key_ring,
        )
        created = await analyses.create(
            self.analysis_command(connection_id, key="private-analysis-1")
        )
        async with self.sessions() as session:
            source = await session.get(SourceRevision, created.source_revision_id)
            task = await session.scalar(
                select(BuilderTask).where(
                    BuilderTask.operation_id == created.operation_id
                )
            )
            lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == created.operation_id
                )
            )
            row = await session.get(SourceConnection, connection_id)
        assert (
            source is not None
            and task is not None
            and lease is not None
            and row is not None
        )
        self.assertEqual(source.connection_id, connection_id)
        self.assertEqual(task.credential_lease_id, lease.id)
        self.assertEqual(lease.source_connection_id, connection_id)
        self.assertEqual(lease.consumer_id, "lae-builder")
        self.assertEqual(len(lease.consumer_binding_hash or b""), 32)
        self.assertEqual(lease.allowed_host, "git.example.com")
        self.assertNotIn(b"private-pat-canary-v1", row.secret_ciphertext)
        self.assertNotEqual(row.secret_checksum, b"private-pat-canary-v1")

        with self.assertRaises(SourceConnectionHostMismatch):
            await analyses.create(
                self.analysis_command(
                    connection_id,
                    key="private-analysis-host-mismatch",
                    repository="https://github.com/acme/private.git",
                )
            )

        broker = PostgresConnectionCredentialBroker(self.sessions, self.key_ring)
        with self.assertRaises(CredentialLeaseRejected):
            await broker.claim(
                lease.id,
                consumer_id="other-builder",
                repository="https://git.example.com/acme/private.git",
            )
        with self.assertRaises(CredentialLeaseRejected):
            await broker.claim(
                lease.id,
                consumer_id="lae-builder",
                repository="https://other.example.com/acme/private.git",
            )
        credential = await broker.claim(
            lease.id,
            consumer_id="lae-builder",
            repository="https://git.example.com/acme/private.git",
        )
        self.assertEqual(credential.secret, "private-pat-canary-v1")
        self.assertEqual(credential.username, "deploy")
        self.assertEqual(credential.allowed_host, "git.example.com")
        self.assertNotIn("private-pat-canary-v1", repr(credential))
        with self.assertRaises(CredentialLeaseRejected):
            await broker.claim(
                lease.id,
                consumer_id="lae-builder",
                repository="https://git.example.com/acme/private.git",
            )

        expired_analysis = await analyses.create(
            self.analysis_command(connection_id, key="private-analysis-expired-lease")
        )
        async with self.sessions() as session:
            async with session.begin():
                expired_lease = await session.scalar(
                    select(SourceCredentialLease)
                    .where(
                        SourceCredentialLease.operation_id
                        == expired_analysis.operation_id
                    )
                    .with_for_update()
                )
                assert expired_lease is not None
                expired_lease.created_at = datetime.now(timezone.utc) - timedelta(
                    hours=2
                )
                expired_lease.expires_at = datetime.now(timezone.utc) - timedelta(
                    hours=1
                )
        with self.assertRaises(CredentialLeaseRejected):
            await broker.claim(
                expired_lease.id,
                consumer_id="lae-builder",
                repository="https://git.example.com/acme/private.git",
            )

        open_analysis = await analyses.create(
            self.analysis_command(connection_id, key="private-analysis-before-rotate")
        )
        async with self.sessions() as session:
            open_lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == open_analysis.operation_id
                )
            )
        assert open_lease is not None
        rotated = await self.connections.rotate(
            RotateSourceConnection(
                scope=self.scope,
                principal=self.principal,
                connection_id=connection_id,
                secret="private-pat-canary-v2",
                idempotency_key="source-connection-rotate-1",
            )
        )
        self.assertEqual(rotated.response_body["connection"]["credentialVersion"], 2)
        self.assertEqual(rotated.response_body["connection"]["username"], "deploy")
        with self.assertRaises(CredentialLeaseRejected):
            await broker.claim(
                open_lease.id,
                consumer_id="lae-builder",
                repository="https://git.example.com/acme/private.git",
            )

        latest = await analyses.create(
            self.analysis_command(connection_id, key="private-analysis-after-rotate")
        )
        async with self.sessions() as session:
            latest_lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == latest.operation_id
                )
            )
        assert latest_lease is not None
        concurrent_claims = await asyncio.gather(
            *(
                broker.claim(
                    latest_lease.id,
                    consumer_id="lae-builder",
                    repository="https://git.example.com/acme/private.git",
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        claimed = [
            item for item in concurrent_claims if not isinstance(item, Exception)
        ]
        rejected = [
            item
            for item in concurrent_claims
            if isinstance(item, CredentialLeaseRejected)
        ]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(rejected), 1)
        latest_credential = claimed[0]
        self.assertEqual(latest_credential.secret, "private-pat-canary-v2")

        pending = await analyses.create(
            self.analysis_command(connection_id, key="private-analysis-before-revoke")
        )
        revoked = await self.connections.revoke(
            RevokeSourceConnection(
                scope=self.scope,
                principal=self.principal,
                connection_id=connection_id,
                idempotency_key="source-connection-revoke-1",
            )
        )
        self.assertIsNotNone(revoked.response_body["connection"]["revokedAt"])
        with self.assertRaises(ResourceNotFound):
            await analyses.create(
                self.analysis_command(connection_id, key="analysis-after-revoke")
            )
        async with self.sessions() as session:
            pending_lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == pending.operation_id
                )
            )
            row = await session.get(SourceConnection, connection_id)
            idempotency_rows = tuple(
                await session.scalars(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.tenant_id == self.scope.tenant_id
                    )
                )
            )
            operation_results = tuple(
                await session.scalars(
                    select(Operation.result).where(
                        Operation.tenant_id == self.scope.tenant_id
                    )
                )
            )
            analysis_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Analysis)
                    .where(Analysis.tenant_id == self.scope.tenant_id)
                )
                or 0
            )
        assert pending_lease is not None and row is not None
        self.assertEqual(pending_lease.status, "revoked")
        self.assertIsNotNone(row.last_used_at)
        self.assertGreaterEqual(analysis_count, 4)
        durable_text = repr(
            [item.response_body for item in idempotency_rows] + list(operation_results)
        )
        self.assertNotIn("private-pat-canary-v1", durable_text)
        self.assertNotIn("private-pat-canary-v2", durable_text)

    async def test_exact_luma_redemption_binds_full_graph_and_anonymous_lease(
        self,
    ) -> None:
        connection = await self.connections.create(
            self.create_command(
                key="exact-broker-connection",
                secret="exact-private-pat-canary",
            )
        )
        connection_id = connection.response_body["connection"]["id"]
        analyses = PostgresAnalysisRequestStore(
            self.sessions,
            luma_cluster_id="luma-integration",
            luma_principal_id="lae-builder",
            hash_key=b"w" * 32,
            hash_key_version=1,
            connection_key_ring=self.key_ring,
        )
        created = await analyses.create(
            self.analysis_command(connection_id, key="exact-private-analysis")
        )
        async with self.sessions() as session:
            task = await session.scalar(
                select(BuilderTask).where(
                    BuilderTask.operation_id == created.operation_id
                )
            )
            lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == created.operation_id
                )
            )
        assert task is not None and lease is not None
        luma_task_id = "builder-" + "1" * 24
        await self._bind_luma_task(task.id, luma_task_id)
        request = CredentialRedemptionRequest(
            lease_id=lease.id,
            builder_task_id=luma_task_id,
            external_operation_id=created.operation_id,
            principal_ref="lae-builder",
            tenant_ref=self.scope.tenant_id,
            application_ref=self.application_id,
            repository="https://git.example.com/acme/private.git",
        )
        broker = PostgresCredentialRedemptionBroker(self.sessions, self.key_ring)
        invalid_bindings = (
            replace(request, builder_task_id=new_id("btask")),
            replace(request, external_operation_id=new_id("op")),
            replace(request, principal_ref="other-builder"),
            replace(request, tenant_ref=self.other_scope.tenant_id),
            replace(request, application_ref=new_id("app")),
            replace(
                request,
                repository="https://other.example.com/acme/private.git",
            ),
        )
        for invalid in invalid_bindings:
            with self.subTest(binding=invalid), self.assertRaises(
                CredentialLeaseRejected
            ) as caught:
                await broker.redeem(invalid)
            self.assertEqual(str(caught.exception), "credential lease is unavailable")
            self.assertNotIn(invalid.repository, str(caught.exception))

        claims = await asyncio.gather(
            broker.redeem(request),
            broker.redeem(request),
            return_exceptions=True,
        )
        successes = [item for item in claims if not isinstance(item, Exception)]
        rejected = [
            item for item in claims if isinstance(item, CredentialLeaseRejected)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(rejected), 1)
        private = successes[0]
        self.assertEqual(private.kind, "git-https")
        self.assertEqual(private.username, "deploy")
        self.assertEqual(private.password, "exact-private-pat-canary")
        self.assertNotIn("exact-private-pat-canary", repr(private))
        self.assertLessEqual(
            private.expires_at, int(datetime.now(timezone.utc).timestamp()) + 300
        )
        self.assertLessEqual(private.expires_at, int(lease.expires_at.timestamp()))

        race_created = await analyses.create(
            self.analysis_command(connection_id, key="exact-rotate-race-analysis")
        )
        async with self.sessions() as session:
            race_task = await session.scalar(
                select(BuilderTask).where(
                    BuilderTask.operation_id == race_created.operation_id
                )
            )
            race_lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == race_created.operation_id
                )
            )
        assert race_task is not None and race_lease is not None
        race_luma_task_id = "builder-" + "2" * 24
        await self._bind_luma_task(race_task.id, race_luma_task_id)
        race_request = replace(
            request,
            lease_id=race_lease.id,
            builder_task_id=race_luma_task_id,
            external_operation_id=race_created.operation_id,
        )
        race_claim, rotation = await asyncio.gather(
            broker.redeem(race_request),
            self.connections.rotate(
                RotateSourceConnection(
                    scope=self.scope,
                    principal=self.principal,
                    connection_id=connection_id,
                    secret="exact-private-pat-after-rotate",
                    idempotency_key="exact-rotate-race",
                )
            ),
            return_exceptions=True,
        )
        self.assertFalse(isinstance(rotation, Exception))
        self.assertTrue(
            isinstance(race_claim, CredentialLeaseRejected)
            or race_claim.password == "exact-private-pat-canary"
        )
        with self.assertRaises(CredentialLeaseRejected):
            await broker.redeem(race_request)

        revoke_created = await analyses.create(
            self.analysis_command(connection_id, key="exact-revoke-analysis")
        )
        async with self.sessions() as session:
            revoke_task = await session.scalar(
                select(BuilderTask).where(
                    BuilderTask.operation_id == revoke_created.operation_id
                )
            )
            revoke_lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == revoke_created.operation_id
                )
            )
        assert revoke_task is not None and revoke_lease is not None
        revoke_luma_task_id = "builder-" + "3" * 24
        await self._bind_luma_task(revoke_task.id, revoke_luma_task_id)
        revoke_request = replace(
            request,
            lease_id=revoke_lease.id,
            builder_task_id=revoke_luma_task_id,
            external_operation_id=revoke_created.operation_id,
        )
        await self.connections.revoke(
            RevokeSourceConnection(
                scope=self.scope,
                principal=self.principal,
                connection_id=connection_id,
                idempotency_key="exact-revoke-before-redemption",
            )
        )
        with self.assertRaises(CredentialLeaseRejected):
            await broker.redeem(revoke_request)

        public_created = await analyses.create(
            CreateAnalysisRequest(
                scope=self.scope,
                principal=self.principal,
                application_id=self.application_id,
                repository="https://github.com/acme/public.git",
                ref="main",
                subdirectory="",
                region="cn",
                public_protocols=("http",),
                idempotency_key="exact-public-analysis",
            )
        )
        async with self.sessions() as session:
            public_task = await session.scalar(
                select(BuilderTask).where(
                    BuilderTask.operation_id == public_created.operation_id
                )
            )
            public_lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id
                    == public_created.operation_id
                )
            )
            durable = repr(
                list(await session.scalars(select(Operation.result)))
                + list(await session.scalars(select(IdempotencyRecord.response_body)))
            )
        assert public_task is not None and public_lease is not None
        public_luma_task_id = "builder-" + "4" * 24
        self.assertIsNone(public_task.luma_task_id)
        public_request = CredentialRedemptionRequest(
            lease_id=public_lease.id,
            builder_task_id=public_luma_task_id,
            external_operation_id=public_created.operation_id,
            principal_ref="lae-builder",
            tenant_ref=self.scope.tenant_id,
            application_ref=self.application_id,
            repository="https://github.com/acme/public.git",
        )
        anonymous = await broker.redeem(public_request)
        self.assertEqual(anonymous.kind, "none")
        self.assertFalse(anonymous.username)
        self.assertFalse(anonymous.password)
        async with self.sessions() as session:
            late_bound_task = await session.get(BuilderTask, public_task.id)
        assert late_bound_task is not None
        self.assertEqual(late_bound_task.luma_task_id, public_luma_task_id)
        self.assertEqual(late_bound_task.checkpoint_version, 0)
        with self.assertRaises(CredentialLeaseRejected):
            await broker.redeem(public_request)
        self.assertNotIn("exact-private-pat-canary", durable)

        expired_created = await analyses.create(
            CreateAnalysisRequest(
                scope=self.scope,
                principal=self.principal,
                application_id=self.application_id,
                repository="https://github.com/acme/expired-public.git",
                ref="main",
                subdirectory="",
                region="cn",
                public_protocols=("http",),
                idempotency_key="exact-expired-analysis",
            )
        )
        async with self.sessions() as session:
            async with session.begin():
                expired_task = await session.scalar(
                    select(BuilderTask).where(
                        BuilderTask.operation_id == expired_created.operation_id
                    )
                )
                expired_lease = await session.scalar(
                    select(SourceCredentialLease)
                    .where(
                        SourceCredentialLease.operation_id
                        == expired_created.operation_id
                    )
                    .with_for_update()
                )
                assert expired_task is not None and expired_lease is not None
                expired_task.luma_task_id = "builder-" + "5" * 24
                expired_task.upstream_status = "queued"
                expired_lease.created_at = datetime.now(timezone.utc) - timedelta(
                    minutes=10
                )
                expired_lease.expires_at = datetime.now(timezone.utc) - timedelta(
                    minutes=5
                )
        with self.assertRaises(CredentialLeaseRejected):
            await broker.redeem(
                CredentialRedemptionRequest(
                    lease_id=expired_lease.id,
                    builder_task_id="builder-" + "5" * 24,
                    external_operation_id=expired_created.operation_id,
                    principal_ref="lae-builder",
                    tenant_ref=self.scope.tenant_id,
                    application_ref=self.application_id,
                    repository="https://github.com/acme/expired-public.git",
                )
            )


if __name__ == "__main__":
    unittest.main()
