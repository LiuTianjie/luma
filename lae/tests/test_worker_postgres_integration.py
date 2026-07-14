from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

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
    CreateOperation,
    OperationStatus,
    OperationStore,
    Principal,
    TenantScope,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_store.models import (  # noqa: E402
    Analysis,
    AnalysisArtifact,
    Application,
    Artifact,
    BuilderTask,
    Operation,
    SourceCredentialLease,
    SourceRevision,
    Tenant,
    TenantMember,
    User,
)
from lae_worker import (  # noqa: E402
    AnalysisDigestReferences,
    ArtifactIngestingAnalysisRecorder,
    ArtifactIntegrityError,
    ArtifactTransferBinding,
    AnalyzeContextInvalid,
    AnalyzeSourceContext,
    PostgresAnalysisRecorder,
    PostgresAnalysisArtifactCatalog,
    PostgresAnalyzeStateStore,
    InMemoryArtifactTransferBroker,
    InMemoryS3CompatibleObjectStore,
)
from lae_worker.analyze import (  # noqa: E402
    AnalyzeOrchestrationError,
    AnalyzeStateConflict,
    ArtifactDescriptor,
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
class PostgreSQLAnalyzePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.operations = OperationStore(self.sessions)
        self.hash_key = b"analysis-checkpoint-integration-key"[:32]
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.source_id = new_id("src")
        self.other_user_id = new_id("usr")
        self.other_tenant_id = new_id("ten")
        self.other_application_id = new_id("app")
        self.other_source_id = new_id("src")
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
                            name="Analyze Tenant",
                            slug=self.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_tenant_id,
                            type="personal",
                            name="Other Tenant",
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
                            name="Analyze App",
                            slug=self.application_id.lower(),
                            luma_name=f"lae-{self.application_id.lower()}",
                            kind="compose",
                        ),
                        Application(
                            id=self.other_application_id,
                            tenant_id=self.other_tenant_id,
                            name="Other App",
                            slug=self.other_application_id.lower(),
                            luma_name=f"lae-{self.other_application_id.lower()}",
                            kind="compose",
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        SourceRevision(
                            id=self.source_id,
                            tenant_id=self.tenant_id,
                            application_id=self.application_id,
                            kind="git",
                            repository=(
                                "https://github.com:8443/acme/application.git"
                            ),
                            ref="main",
                            subdirectory="services/web",
                        ),
                        SourceRevision(
                            id=self.other_source_id,
                            tenant_id=self.other_tenant_id,
                            application_id=self.other_application_id,
                            kind="git",
                            repository="https://github.com/acme/other.git",
                            ref="main",
                            subdirectory="",
                        ),
                    ]
                )

        self.state_store = self._new_state_store()
        self.recorder = PostgresAnalysisRecorder(
            self.sessions,
            agent_image_digest="registry.internal/lae-agent@sha256:" + "a" * 64,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    def _new_state_store(self) -> PostgresAnalyzeStateStore:
        return PostgresAnalyzeStateStore(
            self.sessions,
            luma_cluster_id="luma-integration",
            luma_principal_id="lae-worker",
            hash_key=self.hash_key,
            hash_key_version=7,
        )

    async def _create_operation(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        source_id: str | None = None,
    ):
        return await self.operations.create_operation(
            CreateOperation(
                scope=TenantScope(tenant_id or self.tenant_id),
                principal=Principal("user", user_id or self.user_id),
                kind="source.analyze",
                target_type="source-revision",
                target_id=source_id or self.source_id,
                phase="source.analyze",
            )
        )

    @staticmethod
    def _references() -> AnalysisDigestReferences:
        evidence = "sha256:" + "e" * 64
        deployment = "sha256:" + "d" * 64
        build = "sha256:" + "f" * 64
        return AnalysisDigestReferences(
            resolved_commit="1" * 40,
            source_tree_digest="sha256:" + "b" * 64,
            source_snapshot_id="snapshot-analysis-integration",
            source_snapshot_digest="sha256:" + "c" * 64,
            deployment_plan_digest=deployment,
            build_plan_digest=build,
            evidence_digest=evidence,
            policy_version="2026-07-11",
            verdict="deployable",
            diagnostic_status="succeeded",
            diagnostic_mode="ai",
            diagnostic_code="AI_ANALYSIS_SUCCEEDED",
            knowledge_version="2026-07-14.2",
            blockers=(),
            artifacts=(
                ArtifactDescriptor(
                    "evidence",
                    evidence,
                    "application/vnd.lae.evidence+json",
                    101,
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

    async def test_bound_builder_queue_renews_short_credential_lease(self) -> None:
        operation = await self._create_operation()
        claimed = await self.operations.claim_next(
            worker_id="analysis-worker-renewal",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert claimed is not None and claimed.id == operation.id
        lease_id = new_id("lease")
        state = await self.state_store.initialize(
            operation.id, credential_lease_id=lease_id
        )
        state = await self.state_store.save(
            replace(
                state,
                luma_task_id="builder-task-long-queue",
                luma_status="queued",
            ),
            expected_version=state.version,
        )
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(SourceCredentialLease)
                    .where(SourceCredentialLease.id == lease_id)
                    .values(
                        expires_at=func.clock_timestamp()
                        + timedelta(seconds=10)
                    )
                )

        await self.state_store.renew_credential_lease(state)
        async with self.sessions() as session:
            renewed = await session.get(SourceCredentialLease, lease_id)
        assert renewed is not None
        self.assertGreater(
            renewed.expires_at,
            datetime.now(timezone.utc) + timedelta(minutes=14),
        )

        # An atomically consumed capability no longer needs renewal and is a
        # safe no-op while the Worker observes the task's terminal events.
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(SourceCredentialLease)
                    .where(SourceCredentialLease.id == lease_id)
                    .values(status="consumed", consumed_at=func.clock_timestamp())
                )
        await self.state_store.renew_credential_lease(state)

        await self.operations.request_cancel(TenantScope(self.tenant_id), operation.id)
        with self.assertRaises(AnalyzeStateConflict):
            await self.state_store.renew_credential_lease(state)

    async def test_cas_resume_cancel_tenant_fence_and_descriptor_recording(
        self,
    ) -> None:
        operation = await self._create_operation()
        lease_id = new_id("lease")
        initialized = await asyncio.gather(
            self.state_store.initialize(
                operation.id, credential_lease_id=lease_id
            ),
            self.state_store.initialize(
                operation.id, credential_lease_id=lease_id
            ),
        )
        self.assertEqual(initialized[0], initialized[1])
        self.assertEqual(initialized[0].tenant_ref, self.tenant_id)
        self.assertEqual(initialized[0].application_ref, self.application_id)

        alternatives = (
            replace(
                initialized[0],
                luma_task_id="builder-task-cas-a",
                luma_cursor=1,
                luma_status="queued",
            ),
            replace(
                initialized[0],
                luma_task_id="builder-task-cas-b",
                luma_cursor=2,
                luma_status="running",
            ),
        )
        raced = await asyncio.gather(
            *(
                self.state_store.save(item, expected_version=0)
                for item in alternatives
            ),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(not isinstance(item, BaseException) for item in raced),
            1,
            repr(raced),
        )
        self.assertEqual(
            sum(isinstance(item, AnalyzeStateConflict) for item in raced), 1
        )
        current = await self.state_store.load(operation.id)
        assert current is not None
        with self.assertRaises(AnalyzeStateConflict):
            await self.state_store.save(
                replace(current, luma_cursor=max(0, current.luma_cursor - 1)),
                expected_version=current.version,
            )
        references = self._references()
        current = await self.state_store.save(
            replace(
                current,
                luma_status="succeeded",
                digest_references=references,
            ),
            expected_version=current.version,
        )
        reloaded_references = await self._new_state_store().load(operation.id)
        assert reloaded_references is not None
        self.assertEqual(reloaded_references.digest_references, references)

        claimed = await self.operations.claim_next(
            worker_id="analysis-worker-a",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert claimed is not None
        context = AnalyzeSourceContext(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            source_revision_ref=self.source_id,
            repository="https://github.com:8443/acme/application.git",
            ref="main",
            subdirectory="services/web",
        )
        first_recording = await self.recorder.record(
            operation.id, context, references
        )
        second_recording = await self.recorder.record(
            operation.id, context, references
        )
        self.assertEqual(first_recording, second_recording)
        self.assertEqual(first_recording.artifact_state, "descriptor-only")
        self.assertFalse(first_recording.plan_stored)
        current = await self.state_store.save(
            replace(current, recording=first_recording),
            expected_version=current.version,
        )
        recorded_state = await self._new_state_store().load(operation.id)
        assert recorded_state is not None
        self.assertEqual(recorded_state.recording, first_recording)
        with self.assertRaises(AnalyzeStateConflict):
            await self.state_store.save(
                replace(current, luma_status="running"),
                expected_version=current.version,
            )
        with self.assertRaises(AnalyzeOrchestrationError):
            await self.state_store.save(
                replace(current, luma_status="not-a-builder-status"),
                expected_version=current.version,
            )

        async with self.sessions() as session:
            task = await session.scalar(
                select(BuilderTask).where(BuilderTask.operation_id == operation.id)
            )
            lease = await session.get(SourceCredentialLease, lease_id)
            analysis = await session.scalar(
                select(Analysis).where(Analysis.operation_id == operation.id)
            )
            artifacts = list(
                await session.scalars(
                    select(Artifact).where(Artifact.tenant_id == self.tenant_id)
                )
            )
            link_count = await session.scalar(
                select(func.count())
                .select_from(AnalysisArtifact)
                .where(AnalysisArtifact.tenant_id == self.tenant_id)
            )
        assert task is not None and lease is not None and analysis is not None
        self.assertEqual(len(task.idempotency_key_hash), 32)
        self.assertEqual(len(task.request_digest), 32)
        self.assertNotEqual(
            task.idempotency_key_hash,
            f"lae:{operation.id}:source-analyze:v1".encode(),
        )
        self.assertEqual(task.hash_key_version, 7)
        self.assertEqual(lease.allowed_host, "github.com:8443")
        self.assertEqual(lease.status, "issued")
        self.assertNotIn("token", SourceCredentialLease.__table__.c)
        self.assertNotIn("password", SourceCredentialLease.__table__.c)
        self.assertEqual(analysis.artifact_state, "descriptor-only")
        self.assertFalse(analysis.plan_stored)
        self.assertEqual(len(artifacts), 3)
        self.assertEqual(link_count, 3)
        self.assertTrue(
            all(
                artifact.upload_status == "pending"
                and artifact.storage_key is None
                and artifact.verified_at is None
                for artifact in artifacts
            )
        )
        with self.assertRaises(AnalyzeContextInvalid):
            await self.recorder.record(
                operation.id,
                replace(context, tenant_ref=self.other_tenant_id),
                self._references(),
            )

        await self.operations.complete(
            TenantScope(self.tenant_id),
            operation.id,
            worker_id="analysis-worker-a",
            status=OperationStatus.SUCCEEDED,
            result={"analysisId": analysis.id},
        )

        resumable = await self._create_operation()
        resumable_state = await self.state_store.initialize(
            resumable.id, credential_lease_id=new_id("lease")
        )
        assert current.luma_task_id is not None
        with self.assertRaises(AnalyzeStateConflict):
            await self.state_store.save(
                replace(
                    resumable_state,
                    luma_task_id=current.luma_task_id,
                    luma_status="queued",
                ),
                expected_version=0,
            )
        resumable_state = await self.state_store.save(
            replace(
                resumable_state,
                luma_task_id="builder-task-resume",
                luma_cursor=4,
                luma_status="running",
            ),
            expected_version=0,
        )
        claimed = await self.operations.claim_next(
            worker_id="analysis-worker-a",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert claimed is not None and claimed.id == resumable.id
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Operation)
                    .where(Operation.id == resumable.id)
                    .values(lease_expires_at=func.now() - timedelta(seconds=1))
                )
        reclaimed = await self.operations.claim_next(
            worker_id="analysis-worker-b",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert reclaimed is not None and reclaimed.id == resumable.id
        resumed = await self._new_state_store().load(resumable.id)
        assert resumed is not None
        self.assertEqual(resumed.luma_task_id, "builder-task-resume")
        self.assertEqual(resumed.luma_cursor, 4)
        await self.operations.request_cancel(
            TenantScope(self.tenant_id), resumable.id
        )
        canceled_checkpoint = await self.state_store.save(
            replace(resumed, cancel_forwarded=True, luma_status="cancel_requested"),
            expected_version=resumed.version,
        )
        with self.assertRaises(AnalyzeStateConflict):
            await self.state_store.save(
                replace(canceled_checkpoint, cancel_forwarded=False),
                expected_version=canceled_checkpoint.version,
            )
        terminal = await self.operations.complete(
            TenantScope(self.tenant_id),
            resumable.id,
            worker_id="analysis-worker-b",
            status=OperationStatus.SUCCEEDED,
            result={"late": "success"},
        )
        self.assertEqual(terminal.status, "canceled")

        foreign_operation = await self._create_operation(
            tenant_id=self.other_tenant_id,
            user_id=self.other_user_id,
            source_id=self.other_source_id,
        )
        with self.assertRaises(ValueError):
            await self.state_store.initialize(
                foreign_operation.id,
                credential_lease_id="github_pat_" + "x" * 40,
            )
        async with self.sessions() as session:
            with self.assertRaises(IntegrityError):
                async with session.begin():
                    session.add(
                        BuilderTask(
                            id=new_id("btask"),
                            tenant_id=self.other_tenant_id,
                            application_id=self.application_id,
                            source_revision_id=self.other_source_id,
                            operation_id=foreign_operation.id,
                            luma_cluster_id="luma-integration",
                            luma_principal_id="lae-worker",
                            action="source.analyze",
                            credential_lease_id=new_id("lease"),
                            idempotency_key_hash=b"i" * 32,
                            request_digest=b"r" * 32,
                            hash_key_version=7,
                        )
                    )
                    await session.flush()

    async def test_verified_artifact_catalog_transitions_analysis_atomically(
        self,
    ) -> None:
        operation = await self._create_operation()
        state = await self.state_store.initialize(
            operation.id, credential_lease_id=new_id("lease")
        )
        bodies = {
            "evidence": b'{"schemaVersion":"lae.analysis-evidence/v1"}\n',
            "deploymentPlan": b'{"schemaVersion":"lae.deployment-plan/v1"}\n',
            "buildPlan": b'{"schemaVersion":"lae.build-plan-candidate/v1"}\n',
        }
        media_types = {
            "evidence": "application/vnd.lae.evidence+json",
            "deploymentPlan": "application/vnd.lae.deployment-plan+json",
            "buildPlan": "application/vnd.lae.build-plan-candidate+json",
        }
        descriptors = tuple(
            ArtifactDescriptor(
                name,
                f"sha256:{hashlib.sha256(bodies[name]).hexdigest()}",
                media_types[name],
                len(bodies[name]),
            )
            for name in ("evidence", "deploymentPlan", "buildPlan")
        )
        by_name = {descriptor.name: descriptor for descriptor in descriptors}
        references = AnalysisDigestReferences(
            resolved_commit="2" * 40,
            source_tree_digest="sha256:" + "3" * 64,
            source_snapshot_id="snapshot-artifact-integration",
            source_snapshot_digest="sha256:" + "4" * 64,
            deployment_plan_digest=by_name["deploymentPlan"].digest,
            build_plan_digest=by_name["buildPlan"].digest,
            evidence_digest=by_name["evidence"].digest,
            policy_version="2026-07-11",
            artifacts=descriptors,
        )
        task_id = "builder-task-artifact-integration"
        await self.state_store.save(
            replace(
                state,
                luma_task_id=task_id,
                luma_status="succeeded",
                digest_references=references,
            ),
            expected_version=state.version,
        )
        claimed = await self.operations.claim_next(
            worker_id="artifact-worker",
            kinds=["source.analyze"],
            lease_seconds=30,
        )
        assert claimed is not None and claimed.id == operation.id
        context = AnalyzeSourceContext(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            source_revision_ref=self.source_id,
            repository="https://github.com:8443/acme/application.git",
            ref="main",
            subdirectory="services/web",
        )
        catalog = PostgresAnalysisArtifactCatalog(
            self.sessions,
            agent_image_digest="registry.internal/lae-agent@sha256:" + "a" * 64,
            worker_id="artifact-worker",
        )
        with self.assertRaises(ArtifactIntegrityError):
            await catalog.prepare_analysis(
                operation.id,
                context,
                replace(references, policy_version="2026-07-12"),
                builder_task_id=task_id,
            )
        broker = InMemoryArtifactTransferBroker(chunk_bytes=5)
        object_store = InMemoryS3CompatibleObjectStore()
        for descriptor in descriptors:
            binding = ArtifactTransferBinding(
                tenant_ref=self.tenant_id,
                application_ref=self.application_id,
                operation_id=operation.id,
                builder_task_id=task_id,
                descriptor=descriptor,
            )
            broker.register(binding, bodies[descriptor.name])
        recorder = ArtifactIngestingAnalysisRecorder(
            catalog=catalog,
            broker=broker,
            object_store=object_store,
        )

        first = await recorder.record(
            operation.id,
            context,
            references,
            builder_task_id=task_id,
        )
        issue_calls = broker.issue_calls
        second = await recorder.record(
            operation.id,
            context,
            references,
            builder_task_id=task_id,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.artifact_state, "stored")
        self.assertTrue(first.plan_stored)
        self.assertEqual(broker.issue_calls, issue_calls)
        async with self.sessions() as session:
            analysis = await session.scalar(
                select(Analysis).where(Analysis.operation_id == operation.id)
            )
            artifacts = list(
                await session.scalars(
                    select(Artifact).where(Artifact.tenant_id == self.tenant_id)
                )
            )
        assert analysis is not None
        self.assertEqual(analysis.artifact_state, "stored")
        self.assertTrue(analysis.plan_stored)
        self.assertEqual(len(artifacts), 3)
        self.assertTrue(
            all(
                artifact.upload_status == "verified"
                and artifact.storage_key is not None
                and artifact.storage_key.startswith(
                    f"tenants/{self.tenant_id}/analysis-artifacts/"
                )
                and artifact.verified_at is not None
                for artifact in artifacts
            )
        )


if __name__ == "__main__":
    unittest.main()
