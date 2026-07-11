from __future__ import annotations

import asyncio
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, select

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
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
    IdempotencyKeyReused,
    OperationStatus,
    OperationStore,
    PostgresAnalysisRequestStore,
    Principal,
    ResourceNotFound,
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
    OperationEvent,
    OutboxEvent,
    SourceCredentialLease,
    SourceRevision,
    Tenant,
    TenantMember,
    User,
)
from lae_worker import (  # noqa: E402
    AnalysisDigestReferences,
    AnalyzeSourceContext,
    ArtifactDescriptor,
    PostgresAnalysisRecorder,
    PostgresAnalyzeStateStore,
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
class PostgreSQLAnalysisRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.operations = OperationStore(self.sessions)
        self.hash_key = b"public-analysis-request-test-key!"[:32]
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.other_user_id = new_id("usr")
        self.other_tenant_id = new_id("ten")
        self.other_application_id = new_id("app")
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
                            id=self.tenant_id,
                            type="personal",
                            name="Analysis API tenant",
                            slug=self.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_tenant_id,
                            type="personal",
                            name="Other tenant",
                            slug=self.other_tenant_id.lower(),
                            status="active",
                            owner_user_id=self.other_user_id,
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
                            user_id=self.other_user_id,
                            role="owner",
                        ),
                    ]
                )
                session.add_all(
                    [
                        Application(
                            id=self.application_id,
                            tenant_id=self.tenant_id,
                            name="Analysis target",
                            slug=self.application_id.lower(),
                            luma_name=f"lae-{self.application_id.lower()}",
                            kind="compose",
                        ),
                        Application(
                            id=self.other_application_id,
                            tenant_id=self.other_tenant_id,
                            name="Other target",
                            slug=self.other_application_id.lower(),
                            luma_name=f"lae-{self.other_application_id.lower()}",
                            kind="service",
                        ),
                    ]
                )
        self.store = PostgresAnalysisRequestStore(
            self.sessions,
            luma_cluster_id="luma-integration",
            luma_principal_id="lae-worker",
            hash_key=self.hash_key,
            hash_key_version=9,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    def request(
        self,
        *,
        principal_id: str | None = None,
        application_id: str | None = None,
        ref: str = "main",
        key: str = "analysis-create-integration-1",
    ) -> CreateAnalysisRequest:
        return CreateAnalysisRequest(
            scope=TenantScope(self.tenant_id),
            principal=Principal("deploy-token", principal_id or new_id("dtk")),
            application_id=application_id or self.application_id,
            repository="https://github.com/acme/example.git",
            ref=ref,
            subdirectory="services/web",
            region="cn",
            public_protocols=("http",),
            idempotency_key=key,
        )

    @staticmethod
    def references() -> AnalysisDigestReferences:
        evidence = "sha256:" + "e" * 64
        deployment = "sha256:" + "d" * 64
        build = "sha256:" + "f" * 64
        return AnalysisDigestReferences(
            resolved_commit="1" * 40,
            source_tree_digest="sha256:" + "b" * 64,
            source_snapshot_id="snapshot-public-analysis-api",
            source_snapshot_digest="sha256:" + "c" * 64,
            deployment_plan_digest=deployment,
            build_plan_digest=build,
            evidence_digest=evidence,
            policy_version="2026-07-11",
            artifacts=(
                ArtifactDescriptor(
                    "evidence", evidence, "application/vnd.lae.evidence+json", 101
                ),
                ArtifactDescriptor(
                    "deploymentPlan",
                    deployment,
                    "application/vnd.lae.deployment-plan+json",
                    202,
                ),
                ArtifactDescriptor(
                    "buildPlan",
                    build,
                    "application/vnd.lae.build-plan-candidate+json",
                    303,
                ),
            ),
        )

    async def test_atomic_idempotent_enqueue_is_worker_resumable_and_tenant_fenced(
        self,
    ) -> None:
        principal_id = new_id("dtk")
        request = self.request(principal_id=principal_id)
        first, second = await asyncio.gather(
            self.store.create(request),
            self.store.create(request),
        )
        self.assertEqual(first.analysis_id, second.analysis_id)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual({first.replayed, second.replayed}, {False, True})
        self.assertEqual(first.public_body(), second.public_body())
        public = str(first.public_body()).lower()
        for forbidden in ("luma", "lease", "credential", "repository", "image"):
            self.assertNotIn(forbidden, public)

        async with self.sessions() as session:
            counts = {
                model.__tablename__: int(
                    await session.scalar(
                        select(func.count()).select_from(model).where(
                            model.tenant_id == self.tenant_id
                        )
                    )
                    or 0
                )
                for model in (
                    SourceRevision,
                    Operation,
                    OperationEvent,
                    OutboxEvent,
                    BuilderTask,
                    SourceCredentialLease,
                    Analysis,
                    IdempotencyRecord,
                )
            }
            analysis = await session.get(Analysis, first.analysis_id)
            task = await session.scalar(
                select(BuilderTask).where(BuilderTask.operation_id == first.operation_id)
            )
            lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == first.operation_id
                )
            )
        self.assertTrue(all(value == 1 for value in counts.values()), counts)
        assert analysis is not None and task is not None and lease is not None
        self.assertEqual(analysis.status, "queued")
        self.assertIsNone(analysis.resolved_commit_full)
        self.assertIsNone(analysis.agent_image_digest)
        self.assertEqual(task.hash_key_version, 9)
        self.assertEqual(len(task.idempotency_key_hash), 32)
        self.assertEqual(len(task.request_digest), 32)
        self.assertNotEqual(task.idempotency_key_hash, request.idempotency_key.encode())
        self.assertEqual(lease.allowed_host, "github.com")
        self.assertIsNone(lease.source_connection_id)
        self.assertNotIn("token", SourceCredentialLease.__table__.c)
        self.assertNotIn("password", SourceCredentialLease.__table__.c)

        state_store = PostgresAnalyzeStateStore(
            self.sessions,
            luma_cluster_id="luma-integration",
            luma_principal_id="lae-worker",
            hash_key=self.hash_key,
            hash_key_version=9,
        )
        state = await state_store.load(first.operation_id)
        assert state is not None
        self.assertEqual(state.operation_id, first.operation_id)
        self.assertEqual(state.source_revision_ref, first.source_revision_id)
        self.assertEqual(state.credential_lease_id, lease.id)

        claimed = await self.operations.claim_next(
            worker_id="public-analysis-worker",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert claimed is not None
        self.assertEqual(claimed.id, first.operation_id)
        recorder = PostgresAnalysisRecorder(
            self.sessions,
            agent_image_digest="registry.internal/lae-agent@sha256:" + "a" * 64,
        )
        recording = await recorder.record(
            first.operation_id,
            AnalyzeSourceContext(
                tenant_ref=self.tenant_id,
                application_ref=self.application_id,
                source_revision_ref=first.source_revision_id,
                repository="https://github.com/acme/example.git",
                ref="main",
                subdirectory="services/web",
            ),
            self.references(),
        )
        self.assertEqual(recording.analysis_status, "analyzed")
        async with self.sessions() as session:
            completed_analysis = await session.get(Analysis, first.analysis_id)
            source = await session.get(SourceRevision, first.source_revision_id)
        assert completed_analysis is not None and source is not None
        self.assertEqual(completed_analysis.id, first.analysis_id)
        self.assertEqual(completed_analysis.status, "analyzed")
        self.assertEqual(completed_analysis.resolved_commit_full, "1" * 40)
        self.assertEqual(source.resolved_commit_full, "1" * 40)
        self.assertEqual(source.snapshot_id, "snapshot-public-analysis-api")

        await self.operations.complete(
            TenantScope(self.tenant_id),
            first.operation_id,
            worker_id="public-analysis-worker",
            status=OperationStatus.SUCCEEDED,
            result={"analysisId": first.analysis_id},
        )
        replay_after_completion = await self.store.create(request)
        self.assertEqual(replay_after_completion.public_body(), first.public_body())

        with self.assertRaises(IdempotencyKeyReused):
            await self.store.create(replace(request, ref="release"))
        other_principal = await self.store.create(
            replace(request, principal=Principal("deploy-token", new_id("dtk")))
        )
        self.assertNotEqual(other_principal.operation_id, first.operation_id)
        self.assertNotEqual(other_principal.analysis_id, first.analysis_id)
        with self.assertRaises(ResourceNotFound):
            await self.store.create(
                self.request(
                    principal_id=principal_id,
                    application_id=self.other_application_id,
                    key="analysis-cross-tenant",
                )
            )
        async with self.sessions() as session:
            self.assertEqual(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Operation)
                        .where(Operation.tenant_id == self.other_tenant_id)
                    )
                    or 0
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
