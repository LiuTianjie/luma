from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-core/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

try:
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover
    command = None
    Config = None

from lae_api.upload_api import UploadApiService, UploadCreateRequest  # noqa: E402
from lae_store import (  # noqa: E402
    CreateUploadAnalysis,
    CredentialLeaseRejected,
    FakeUploadObjectStore,
    IdempotencyKeyReused,
    ObjectSourceDescriptor,
    ObjectSourceRedemptionRequest,
    PostgresObjectSourceRedemptionBroker,
    PostgresPublicResourceStore,
    PostgresUploadAnalysisStore,
    PostgresUploadStore,
    Principal,
    ResourceNotFound,
    TenantScope,
    UploadQuotaExceeded,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_store.models import (  # noqa: E402
    Application,
    BuilderTask,
    PlanVersion,
    SourceCredentialLease,
    SourceRevision,
    Subscription,
    Tenant,
    TenantMember,
    Upload,
    User,
)
from lae_worker.static_upload import StaticUploadScanner  # noqa: E402

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


@dataclass(frozen=True)
class ApiPrincipal:
    credential_type: str
    credential_id: str


@unittest.skipUnless(
    DSN and DDL_ALLOWED and command is not None,
    "set LAE_TEST_POSTGRES_DSN and LAE_TEST_POSTGRES_ALLOW_DDL=1 for real PostgreSQL",
)
class StaticUploadPostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.store = PostgresUploadStore(self.sessions, hash_key=b"u" * 32)
        self.objects = FakeUploadObjectStore()
        self.api = UploadApiService(self.store, self.objects)
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.other_tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.other_application_id = new_id("app")
        self.scope = TenantScope(self.tenant_id)
        self.other_scope = TenantScope(self.other_tenant_id)
        self.principal = ApiPrincipal("deploy_token", new_id("dtk"))
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
                            name="Upload Tenant",
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
                session.add_all(
                    [
                        Subscription(
                            id=new_id("sub"),
                            tenant_id=self.tenant_id,
                            plan_version_id=plan_id,
                            interval="none",
                            status="active",
                            provider="internal",
                        ),
                        Subscription(
                            id=new_id("sub"),
                            tenant_id=self.other_tenant_id,
                            plan_version_id=plan_id,
                            interval="none",
                            status="active",
                            provider="internal",
                        ),
                        Application(
                            id=self.application_id,
                            tenant_id=self.tenant_id,
                            name="Static App",
                            slug="static-app",
                            luma_name=f"lae-{self.application_id.lower()}",
                            kind="pending",
                            desired_state="running",
                            observed_state="unknown",
                        ),
                        Application(
                            id=self.other_application_id,
                            tenant_id=self.other_tenant_id,
                            name="Other App",
                            slug="other-app",
                            luma_name=f"lae-{self.other_application_id.lower()}",
                            kind="pending",
                            desired_state="running",
                            observed_state="unknown",
                        ),
                    ]
                )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    def payload(self, content: bytes, *, filename: str = "index.html") -> UploadCreateRequest:
        return UploadCreateRequest(
            applicationId=self.application_id,
            filename=filename,
            mediaType="text/html" if filename.endswith(".html") else "application/zip",
            sizeBytes=len(content),
            sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
        )

    async def reserve_and_put(self, content: bytes, *, key: str):
        body, replayed = await self.api.create(
            self.scope, self.principal, self.payload(content), key
        )
        self.assertFalse(replayed)
        transfer = body["transfer"]
        self.objects.put_from_grant(
            transfer["url"], content, headers=transfer["headers"]
        )
        return body

    async def test_full_lifecycle_tenant_fence_idempotency_analysis_and_delete(self) -> None:
        content = b"<!doctype html><html><body>Static</body></html>"
        first, second = await asyncio.gather(
            self.api.create(
                self.scope, self.principal, self.payload(content), "upload-create-1"
            ),
            self.api.create(
                self.scope, self.principal, self.payload(content), "upload-create-1"
            ),
        )
        issued = [item for item in (first, second) if not item[1]]
        replayed = [item for item in (first, second) if item[1]]
        self.assertEqual(len(issued), 1)
        self.assertEqual(len(replayed), 1)
        self.assertIn("transfer", issued[0][0])
        self.assertNotIn("transfer", replayed[0][0])
        upload_id = issued[0][0]["upload"]["id"]
        transfer = issued[0][0]["transfer"]
        self.objects.put_from_grant(
            transfer["url"], content, headers=transfer["headers"]
        )
        completed, complete_replayed = await self.api.complete(
            self.scope, self.principal, upload_id, "upload-complete-1"
        )
        self.assertFalse(complete_replayed)
        self.assertEqual(completed["upload"]["status"], "scanning")
        again, complete_replayed = await self.api.complete(
            self.scope, self.principal, upload_id, "upload-complete-1"
        )
        self.assertTrue(complete_replayed)
        self.assertEqual(again["upload"]["status"], "scanning")
        with self.assertRaises(IdempotencyKeyReused):
            await self.api.complete(
                self.scope, self.principal, upload_id, "different-complete-key"
            )
        with self.assertRaises(ResourceNotFound):
            await self.api.get(self.other_scope, upload_id)

        scanner = StaticUploadScanner(
            self.store, self.objects, worker_id="scanner.integration"
        )
        self.assertTrue(await scanner.run_once())
        ready = await self.api.get(self.scope, upload_id)
        self.assertEqual(ready["upload"]["status"], "ready")
        source_id = ready["upload"]["sourceRevisionId"]
        self.assertIsNotNone(source_id)
        async with self.sessions() as session:
            source = await session.get(SourceRevision, source_id)
            assert source is not None
            self.assertEqual(source.kind, "upload")
            self.assertEqual(source.upload_id, upload_id)
            self.assertIsNone(source.repository)
            self.assertIsNone(source.ref)

        analyses = PostgresUploadAnalysisStore(
            self.sessions,
            luma_cluster_id="luma.test",
            luma_principal_id="lae.test",
            object_store_host="objects.internal",
            hash_key=b"a" * 32,
        )
        analysis = await analyses.create(
            CreateUploadAnalysis(
                scope=self.scope,
                principal=Principal("deploy-token", self.principal.credential_id),
                application_id=self.application_id,
                upload_id=upload_id,
                region="cn",
                public_protocols=("http",),
                idempotency_key="upload-analysis-1",
            )
        )
        replay = await analyses.create(
            CreateUploadAnalysis(
                scope=self.scope,
                principal=Principal("deploy-token", self.principal.credential_id),
                application_id=self.application_id,
                upload_id=upload_id,
                region="cn",
                public_protocols=("http",),
                idempotency_key="upload-analysis-1",
            )
        )
        self.assertFalse(analysis.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(analysis.analysis_id, replay.analysis_id)
        serialized = str(analysis.public_body()).lower()
        for forbidden in ("bucket", "object", "internal", "url", "repository"):
            self.assertNotIn(forbidden, serialized)

        async with self.sessions() as session:
            task = await session.scalar(
                select(BuilderTask).where(
                    BuilderTask.operation_id == analysis.operation_id
                )
            )
            lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == analysis.operation_id
                )
            )
        assert task is not None and lease is not None
        luma_task_id = "builder-" + "9" * 24
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(BuilderTask)
                    .where(BuilderTask.id == task.id)
                    .values(luma_task_id=luma_task_id, upstream_status="queued")
                )
        request = ObjectSourceRedemptionRequest(
            lease_id=lease.id,
            builder_task_id=luma_task_id,
            external_operation_id=analysis.operation_id,
            principal_ref="lae.test",
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            object=ObjectSourceDescriptor(
                digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
                media_type="text/html",
                size_bytes=len(content),
            ),
        )
        broker = PostgresObjectSourceRedemptionBroker(self.sessions)
        with self.assertRaises(CredentialLeaseRejected):
            await broker.redeem(
                replace(
                    request,
                    object=replace(request.object, size_bytes=len(content) + 1),
                )
            )
        claims = await asyncio.gather(
            broker.redeem(request),
            broker.redeem(request),
            return_exceptions=True,
        )
        succeeded = [claim for claim in claims if not isinstance(claim, Exception)]
        rejected = [
            claim for claim in claims if isinstance(claim, CredentialLeaseRejected)
        ]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(rejected), 1)
        claim = succeeded[0]
        self.assertEqual(claim.allowed_host, "objects.internal")
        self.assertLessEqual(claim.ttl_seconds, 300)
        self.assertNotIn(claim.object_key, repr(claim))
        async with self.sessions() as session:
            consumed = await session.get(SourceCredentialLease, lease.id)
        assert consumed is not None
        self.assertEqual(consumed.status, "consumed")

        deleted = await self.api.delete(self.scope, upload_id)
        self.assertEqual(deleted["upload"]["status"], "deleted")
        async with self.sessions() as session:
            source = await session.get(SourceRevision, source_id)
            assert source is not None
            self.assertIsNotNone(source.deleted_at)

    async def test_quota_expiry_verification_failure_and_cancel_release_storage(self) -> None:
        content = b"<!doctype html><html></html>"
        async with self.sessions() as session:
            async with session.begin():
                plan = await session.scalar(
                    select(PlanVersion).where(
                        PlanVersion.code == "lite", PlanVersion.version == 1
                    ).with_for_update()
                )
                assert plan is not None
                limits = dict(plan.limits_json)
                limits["uploadBytes"] = len(content)
                limits["maxUploadBytes"] = len(content)
                plan.limits_json = limits
        reserved = await self.reserve_and_put(content, key="quota-create-1")
        with self.assertRaises(UploadQuotaExceeded):
            await self.api.create(
                self.scope, self.principal, self.payload(content), "quota-create-2"
            )
        upload_id = reserved["upload"]["id"]
        await self.api.complete(
            self.scope, self.principal, upload_id, "quota-complete-1"
        )
        public = PostgresPublicResourceStore(self.sessions)
        await public.request_cancel(self.scope, reserved["operation"]["id"])
        scanner = StaticUploadScanner(self.store, self.objects, worker_id="scanner.cancel")
        self.assertTrue(await scanner.run_once())
        canceled = await self.api.get(self.scope, upload_id)
        self.assertEqual(canceled["upload"]["failureCode"], "LAE_UPLOAD_CANCELED")

        # Failed/canceled uploads no longer consume the hard upload quota.
        next_body, _ = await self.api.create(
            self.scope, self.principal, self.payload(content), "quota-create-3"
        )
        next_id = next_body["upload"]["id"]
        async with self.sessions() as session:
            async with session.begin():
                row = await session.get(Upload, next_id, with_for_update=True)
                assert row is not None
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        expired = await self.store.expire_stale()
        self.assertEqual([item.upload.id for item in expired], [next_id])
        self.assertTrue(await scanner.cleanup_once())
        self.assertTrue(await scanner.cleanup_once())
        self.assertEqual((await self.api.get(self.scope, next_id))["upload"]["status"], "deleted")

        # A forged object with the right length but wrong hash is terminal and
        # never reaches scanning/SourceRevision creation.
        bad_body, _ = await self.api.create(
            self.scope, self.principal, self.payload(content), "bad-create-1"
        )
        transfer = bad_body["transfer"]
        forged = b"X" * len(content)
        self.objects.put_from_grant(transfer["url"], forged, headers=transfer["headers"])
        with self.assertRaises(Exception) as caught:
            await self.api.complete(
                self.scope,
                self.principal,
                bad_body["upload"]["id"],
                "bad-complete-1",
            )
        self.assertIn("UploadVerificationFailed", type(caught.exception).__name__)
        failed = await self.api.get(self.scope, bad_body["upload"]["id"])
        self.assertEqual(failed["upload"]["status"], "failed")
        self.assertIsNone(failed["upload"]["sourceRevisionId"])


if __name__ == "__main__":
    unittest.main()
