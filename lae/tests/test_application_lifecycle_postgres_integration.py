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
    "packages/contracts/src",
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
    ApplicationLifecycleConflict,
    IdempotencyKeyReused,
    OperationStore,
    OperationStatus,
    PostgresApplicationLifecycleStore,
    PostgresPublicResourceStore,
    Principal,
    ResourceNotFound,
    RequestApplicationAction,
    TenantScope,
    UpdateCheckBinding,
    create_postgres_engine,
    create_session_factory,
    new_id,
)
from lae_luma_adapter import RuntimeDeployment  # noqa: E402
from lae_worker import (  # noqa: E402
    AnalyzeSourceContext,
    LifecycleContextInvalid,
    LifecycleRuntimeFailed,
    PostgresLifecycleContextLoader,
    PostgresLifecycleStateStore,
    PostgresUpdateCheckResolver,
)
from lae_store.models import (  # noqa: E402
    Analysis,
    AnalysisArtifact,
    AppRevision,
    Application,
    ApplicationLifecycleRequest,
    ApplicationService,
    Artifact,
    BuilderTask,
    Deployment,
    DeploymentBuildOutput,
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


DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"
SHA = "sha256:" + "a" * 64
PREVIOUS_IMAGE_SHA = "sha256:" + "b" * 64


class _UpdatePlanLoader:
    def __init__(self, plans: dict[str, dict[str, object]]) -> None:
        self.plans = plans
        self.calls: list[tuple[str, str]] = []

    async def load(
        self, storage_key: str, *, expected_digest: str
    ) -> dict[str, object]:
        self.calls.append((storage_key, expected_digest))
        return self.plans[storage_key]


def _alembic_config() -> Config:
    assert Config is not None
    config = Config(str(LAE_ROOT / "migrations/alembic.ini"))
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
class ApplicationLifecyclePostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.scope = TenantScope(new_id("ten"))
        self.other_scope = TenantScope(new_id("ten"))
        self.user_id = new_id("usr")
        self.principal = Principal("deploy-token", new_id("dtk"))
        self.application_id = new_id("app")
        self.current_source_id = new_id("src")
        self.previous_deployment_id = new_id("dep")
        self.current_deployment_id = new_id("dep")
        self.store = PostgresApplicationLifecycleStore(
            self.sessions,
            idempotency_hash_key=b"lifecycle-idempotency-integration".ljust(32, b"!"),
            update_check=UpdateCheckBinding(
                luma_cluster_id="luma-primary",
                luma_principal_id="lae-worker",
                hash_key=b"worker-state-integration-key".ljust(32, b"!"),
            ),
        )
        await self._seed_application()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    async def _seed_application(self) -> None:
        now = datetime.now(timezone.utc)
        previous_source_id = new_id("src")
        previous_analysis_id = new_id("ana")
        current_analysis_id = new_id("ana")
        previous_analysis_operation_id = new_id("op")
        current_analysis_operation_id = new_id("op")
        self.previous_revision_id = new_id("rev")
        self.current_revision_id = new_id("rev")
        self.previous_deployment_operation_id = new_id("op")
        self.current_deployment_operation_id = new_id("op")
        artifact_id = new_id("art")
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
                            id=self.scope.tenant_id,
                            type="personal",
                            name="Lifecycle tenant",
                            slug=self.scope.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_scope.tenant_id,
                            type="organization",
                            name="Other lifecycle tenant",
                            slug=self.other_scope.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
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
                            user_id=self.user_id,
                            role="owner",
                        ),
                    ]
                )
                session.add(
                    Application(
                        id=self.application_id,
                        tenant_id=self.scope.tenant_id,
                        name="Lifecycle compose",
                        slug="lifecycle-compose",
                        luma_name=f"lae-{self.application_id.lower()}",
                        kind="compose",
                        desired_state="running",
                        observed_state="running",
                    )
                )
                await session.flush()
                session.add(
                    ApplicationService(
                        id=new_id("svc"),
                        tenant_id=self.scope.tenant_id,
                        application_id=self.application_id,
                        service_key="web",
                        role="http",
                        required=True,
                        desired_state="running",
                        observed_state="running",
                        current_image_digest=SHA,
                    )
                )
                session.add_all(
                    [
                        SourceRevision(
                            id=previous_source_id,
                            tenant_id=self.scope.tenant_id,
                            application_id=self.application_id,
                            kind="git",
                            repository="https://github.com/acme/lifecycle.git",
                            ref="release-1",
                            subdirectory="services/web",
                            resolved_commit_full="1" * 40,
                            source_tree_digest=SHA,
                            snapshot_id="snapshot-lifecycle-previous",
                            snapshot_digest=SHA,
                        ),
                        SourceRevision(
                            id=self.current_source_id,
                            tenant_id=self.scope.tenant_id,
                            application_id=self.application_id,
                            kind="git",
                            repository="https://github.com/acme/lifecycle.git",
                            ref="release-2",
                            subdirectory="services/web",
                            resolved_commit_full="2" * 40,
                            source_tree_digest=SHA,
                            snapshot_id="snapshot-lifecycle-current",
                            snapshot_digest=SHA,
                        ),
                    ]
                )
                session.add_all(
                    [
                        Operation(
                            id=previous_analysis_operation_id,
                            tenant_id=self.scope.tenant_id,
                            principal_type="session",
                            principal_id=self.user_id,
                            kind="source.analyze",
                            target_type="source-revision",
                            target_id=previous_source_id,
                            status="succeeded",
                        ),
                        Operation(
                            id=current_analysis_operation_id,
                            tenant_id=self.scope.tenant_id,
                            principal_type="session",
                            principal_id=self.user_id,
                            kind="source.analyze",
                            target_type="source-revision",
                            target_id=self.current_source_id,
                            status="succeeded",
                        ),
                        Operation(
                            id=self.previous_deployment_operation_id,
                            tenant_id=self.scope.tenant_id,
                            principal_type="session",
                            principal_id=self.user_id,
                            kind="deployment.create",
                            target_type="application",
                            target_id=self.application_id,
                            status="succeeded",
                        ),
                        Operation(
                            id=self.current_deployment_operation_id,
                            tenant_id=self.scope.tenant_id,
                            principal_type="session",
                            principal_id=self.user_id,
                            kind="deployment.create",
                            target_type="application",
                            target_id=self.application_id,
                            status="succeeded",
                        ),
                    ]
                )
                session.add(
                    Artifact(
                        id=artifact_id,
                        tenant_id=self.scope.tenant_id,
                        kind="deployment-plan",
                        digest=SHA,
                        media_type="application/vnd.lae.deployment-plan+json",
                        size_bytes=256,
                        storage_key="lifecycle/plan.json",
                        upload_status="verified",
                        verified_at=now,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        Analysis(
                            id=previous_analysis_id,
                            tenant_id=self.scope.tenant_id,
                            application_id=self.application_id,
                            source_revision_id=previous_source_id,
                            operation_id=previous_analysis_operation_id,
                            status="deployable",
                            policy_version="policy-v1",
                            agent_image_digest=f"registry.test/agent@{SHA}",
                            resolved_commit_full="1" * 40,
                            source_tree_digest=SHA,
                            source_snapshot_id="snapshot-lifecycle-previous",
                            source_snapshot_digest=SHA,
                            deployment_plan_digest=SHA,
                            build_plan_digest=SHA,
                            evidence_digest=SHA,
                            artifact_state="stored",
                            plan_stored=True,
                        ),
                        Analysis(
                            id=current_analysis_id,
                            tenant_id=self.scope.tenant_id,
                            application_id=self.application_id,
                            source_revision_id=self.current_source_id,
                            operation_id=current_analysis_operation_id,
                            status="deployable",
                            policy_version="policy-v1",
                            agent_image_digest=f"registry.test/agent@{SHA}",
                            resolved_commit_full="2" * 40,
                            source_tree_digest=SHA,
                            source_snapshot_id="snapshot-lifecycle-current",
                            source_snapshot_digest=SHA,
                            deployment_plan_digest=SHA,
                            build_plan_digest=SHA,
                            evidence_digest=SHA,
                            artifact_state="stored",
                            plan_stored=True,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        AppRevision(
                            id=self.previous_revision_id,
                            tenant_id=self.scope.tenant_id,
                            application_id=self.application_id,
                            revision_no=1,
                            analysis_id=previous_analysis_id,
                            source_revision_id=previous_source_id,
                            kind="compose",
                            deployment_plan_artifact_id=artifact_id,
                            deployment_plan_digest=SHA,
                            normalized_compose_digest=SHA,
                            luma_manifest_digest=PREVIOUS_IMAGE_SHA,
                            environment_schema_digest=SHA,
                            environment_version=0,
                            status="superseded",
                            created_by_type="session",
                            created_by_id=self.user_id,
                            activated_at=now,
                        ),
                        AppRevision(
                            id=self.current_revision_id,
                            tenant_id=self.scope.tenant_id,
                            application_id=self.application_id,
                            revision_no=2,
                            analysis_id=current_analysis_id,
                            source_revision_id=self.current_source_id,
                            kind="compose",
                            deployment_plan_artifact_id=artifact_id,
                            deployment_plan_digest=SHA,
                            normalized_compose_digest=SHA,
                            luma_manifest_digest=SHA,
                            environment_schema_digest=SHA,
                            environment_version=0,
                            status="active",
                            created_by_type="session",
                            created_by_id=self.user_id,
                            activated_at=now,
                        ),
                    ]
                )
                await session.flush()
                session.add(
                    Deployment(
                        id=self.previous_deployment_id,
                        tenant_id=self.scope.tenant_id,
                        application_id=self.application_id,
                        revision_id=self.previous_revision_id,
                        operation_id=self.previous_deployment_operation_id,
                        status="succeeded",
                        luma_cluster_id="luma-primary",
                        luma_external_ref="lae-run-previous",
                        started_at=now,
                        finished_at=now,
                    )
                )
                await session.flush()
                session.add(
                    Deployment(
                        id=self.current_deployment_id,
                        tenant_id=self.scope.tenant_id,
                        application_id=self.application_id,
                        revision_id=self.current_revision_id,
                        operation_id=self.current_deployment_operation_id,
                        status="succeeded",
                        luma_cluster_id="luma-primary",
                        luma_external_ref="lae-run-current",
                        previous_deployment_id=self.previous_deployment_id,
                        started_at=now,
                        finished_at=now,
                    )
                )
                await session.flush()
                session.add(
                    DeploymentBuildOutput(
                        tenant_id=self.scope.tenant_id,
                        application_id=self.application_id,
                        deployment_id=self.previous_deployment_id,
                        operation_id=self.previous_deployment_operation_id,
                        revision_id=self.previous_revision_id,
                        build_key="web",
                        service_key="web",
                        image_digest=PREVIOUS_IMAGE_SHA,
                        sbom_digest=SHA,
                        provenance_digest=SHA,
                        scan_digest=SHA,
                        verified_at=now,
                    )
                )
                await session.execute(
                    update(Application)
                    .where(Application.id == self.application_id)
                    .values(
                        current_revision_id=self.current_revision_id,
                        current_deployment_id=self.current_deployment_id,
                    )
                )

    def action(
        self, action: str, *, rollback_deployment_id: str | None = None
    ) -> RequestApplicationAction:
        return RequestApplicationAction(
            scope=self.scope,
            application_id=self.application_id,
            action=action,
            rollback_deployment_id=rollback_deployment_id,
        )

    async def request(self, action: str, key: str):
        command_value = self.action(action)
        return await self.store.request(
            command_value,
            principal=self.principal,
            idempotency=self.store.idempotency(command_value, key=key),
        )

    async def finish(self, operation_id: str) -> None:
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Operation)
                    .where(Operation.id == operation_id)
                    .values(status="succeeded", finished_at=func.now())
                )

    async def test_check_update_clones_saved_source_and_is_fully_atomic(self) -> None:
        first, replay = await asyncio.gather(
            self.request("check-update", "check-update-1"),
            self.request("check-update", "check-update-1"),
        )
        self.assertEqual(first.body, replay.body)
        self.assertEqual({first.replayed, replay.replayed}, {False, True})
        operation_id = first.body["operation"]["id"]
        analysis_id = first.body["analysis"]["id"]

        async with self.sessions() as session:
            lifecycle = await session.get(ApplicationLifecycleRequest, operation_id)
            operation = await session.get(Operation, operation_id)
            analysis = await session.get(Analysis, analysis_id)
            task = await session.scalar(
                select(BuilderTask).where(BuilderTask.operation_id == operation_id)
            )
            lease = await session.scalar(
                select(SourceCredentialLease).where(
                    SourceCredentialLease.operation_id == operation_id
                )
            )
            cloned = await session.get(SourceRevision, analysis.source_revision_id)
            app = await session.get(Application, self.application_id)
            counts = {
                model.__tablename__: int(
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.tenant_id == self.scope.tenant_id)
                    )
                    or 0
                )
                for model in (OperationEvent, OutboxEvent, IdempotencyRecord)
            }
        assert lifecycle is not None
        assert operation is not None
        assert analysis is not None
        assert task is not None
        assert lease is not None
        assert cloned is not None
        assert app is not None
        self.assertEqual(operation.kind, "application.check-update")
        self.assertEqual(operation.target_id, self.application_id)
        self.assertEqual(lifecycle.base_source_revision_id, self.current_source_id)
        self.assertEqual(lifecycle.source_revision_id, cloned.id)
        self.assertEqual(lifecycle.analysis_id, analysis.id)
        self.assertEqual(task.source_revision_id, cloned.id)
        self.assertEqual(lease.source_revision_id, cloned.id)
        self.assertEqual(cloned.repository, "https://github.com/acme/lifecycle.git")
        self.assertEqual(cloned.ref, "release-2")
        self.assertEqual(cloned.subdirectory, "services/web")
        self.assertIsNone(cloned.resolved_commit_full)
        self.assertEqual(app.desired_state, "running")
        self.assertEqual(app.observed_state, "running")
        # Seed operations are excluded; each request still creates exactly one
        # event, outbox publication and idempotency record.
        self.assertEqual(counts, {name: 1 for name in counts})

        operations = OperationStore(self.sessions)
        claimed = await operations.claim_next(
            worker_id="update-check-postgres-integration",
            kinds=("application.check-update",),
            lease_seconds=60,
        )
        assert claimed is not None and claimed.id == operation_id
        candidate_plan = "sha256:" + "c" * 64
        candidate_artifact_id = new_id("art")
        candidate_storage_key = "lifecycle/candidate-plan.json"
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(SourceRevision)
                    .where(SourceRevision.id == cloned.id)
                    .values(
                        resolved_commit_full="3" * 40,
                        source_tree_digest=SHA,
                        snapshot_id="snapshot-update-check-integration",
                        snapshot_digest="sha256:" + "d" * 64,
                    )
                )
                await session.execute(
                    update(Analysis)
                    .where(Analysis.id == analysis_id)
                    .values(
                        status="analyzed",
                        policy_version="2026-07-11",
                        agent_image_digest=(
                            "registry.internal/lae-agent@sha256:" + "e" * 64
                        ),
                        resolved_commit_full="3" * 40,
                        source_tree_digest=SHA,
                        source_snapshot_id="snapshot-update-check-integration",
                        source_snapshot_digest="sha256:" + "d" * 64,
                        deployment_plan_digest=candidate_plan,
                        build_plan_digest="sha256:" + "e" * 64,
                        evidence_digest="sha256:" + "f" * 64,
                        verdict="deployable",
                        diagnostic_status="succeeded",
                        artifact_state="stored",
                        plan_stored=True,
                    )
                )
                session.add(
                    Artifact(
                        id=candidate_artifact_id,
                        tenant_id=self.scope.tenant_id,
                        kind="deployment-plan",
                        digest=candidate_plan,
                        media_type="application/vnd.lae.deployment-plan+json",
                        size_bytes=256,
                        storage_key=candidate_storage_key,
                        upload_status="verified",
                        verified_at=datetime.now(timezone.utc),
                    )
                )
                await session.flush()
                session.add(
                    AnalysisArtifact(
                        tenant_id=self.scope.tenant_id,
                        analysis_id=analysis_id,
                        name="deploymentPlan",
                        artifact_id=candidate_artifact_id,
                    )
                )
        context = AnalyzeSourceContext(
            tenant_ref=self.scope.tenant_id,
            application_ref=self.application_id,
            source_revision_ref=cloned.id,
            repository=cloned.repository,
            ref=cloned.ref,
            subdirectory=cloned.subdirectory,
        )
        loader = _UpdatePlanLoader(
            {
                "lifecycle/plan.json": {
                    "schemaVersion": "lae.deployment-plan/v1",
                    "services": [{"key": "web", "role": "http"}],
                    "routes": [],
                    "volumes": [],
                    "environment": [],
                },
                candidate_storage_key: {
                    "schemaVersion": "lae.deployment-plan/v1",
                    "services": [
                        {"key": "web", "role": "http"},
                        {"key": "worker", "role": "worker"},
                    ],
                    "routes": [],
                    "volumes": [],
                    "environment": [],
                },
            }
        )
        resolver = PostgresUpdateCheckResolver(self.sessions, plan_loader=loader)
        comparison = await resolver.resolve(claimed, context)
        self.assertTrue(comparison.baseline_available)
        self.assertFalse(comparison.source_changed)
        self.assertTrue(comparison.deployment_plan_changed)
        self.assertTrue(comparison.changed)
        self.assertEqual(comparison.candidate_analysis_id, analysis_id)
        self.assertEqual(comparison.candidate_verdict, "deployable")
        assert comparison.plan_changes is not None
        self.assertEqual(comparison.plan_changes.services.added, ("worker",))
        self.assertFalse(comparison.plan_changes.destructive)
        self.assertEqual(
            loader.calls,
            [
                ("lifecycle/plan.json", SHA),
                (candidate_storage_key, candidate_plan),
            ],
        )

        # A source-only analyzer cannot see saved application environment and
        # reports needs_input on every pass.  When the immutable plan digest is
        # unchanged, the deployed baseline proves that configuration already
        # satisfies the plan, so update admission must remain usable.
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Analysis)
                    .where(Analysis.id == analysis_id)
                    .values(deployment_plan_digest=SHA, verdict="needs_input")
                )
        unchanged = await resolver.resolve(claimed, context)
        self.assertFalse(unchanged.deployment_plan_changed)
        self.assertEqual(unchanged.candidate_verdict, "deployable")
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Analysis)
                    .where(Analysis.id == analysis_id)
                    .values(
                        deployment_plan_digest=candidate_plan,
                        verdict="deployable",
                    )
                )

        # A valid lifecycle request can have no deployed revision baseline
        # (for example a legacy/pending app). That state is explicit and
        # conservative instead of claiming that either dimension is unchanged.
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(ApplicationLifecycleRequest)
                    .where(ApplicationLifecycleRequest.operation_id == operation_id)
                    .values(source_deployment_id=None)
                )
        no_baseline = await resolver.resolve(claimed, context)
        self.assertFalse(no_baseline.baseline_available)
        self.assertTrue(no_baseline.source_changed)
        self.assertTrue(no_baseline.deployment_plan_changed)
        self.assertTrue(no_baseline.changed)
        self.assertIsNone(no_baseline.to_body()["digests"]["baseline"])
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(ApplicationLifecycleRequest)
                    .where(ApplicationLifecycleRequest.operation_id == operation_id)
                    .values(source_deployment_id=self.current_deployment_id)
                )

        completed = await operations.complete(
            self.scope,
            operation_id,
            worker_id="update-check-postgres-integration",
            status=OperationStatus.SUCCEEDED,
            result={
                "sourceRevisionId": cloned.id,
                "artifactState": "stored",
                "credentialLeaseId": "internal-must-not-escape",
                "updateCheck": comparison.to_body(),
            },
        )
        assert completed.result is not None
        self.assertEqual(completed.result["sourceRevisionId"], cloned.id)
        public = await PostgresPublicResourceStore(self.sessions).get_operation(
            self.scope, operation_id
        )
        public_body = public.public_body()
        self.assertEqual(public_body["updateCheck"], comparison.to_body())
        self.assertNotIn("result", public_body)
        self.assertNotIn("credentialLeaseId", str(public_body))

        reused = self.store.idempotency(
            self.action("check-update"), key="check-update-1"
        )
        reused = type(reused)(
            key=reused.key,
            method=reused.method,
            route_template=reused.route_template,
            request_hash=b"x" * 32,
        )
        with self.assertRaises(IdempotencyKeyReused):
            await self.store.request(
                self.action("check-update"),
                principal=self.principal,
                idempotency=reused,
            )

    async def test_desired_state_bindings_conflicts_rollback_and_tenant_fence(
        self,
    ) -> None:
        suspended = await self.request("suspend", "suspend-1")
        suspend_operation_id = suspended.body["operation"]["id"]
        self.assertEqual(suspended.body["application"]["desiredState"], "suspended")
        async with self.sessions() as session:
            app = await session.get(Application, self.application_id)
            service = await session.scalar(
                select(ApplicationService).where(
                    ApplicationService.application_id == self.application_id
                )
            )
            lifecycle = await session.get(
                ApplicationLifecycleRequest, suspend_operation_id
            )
        assert app is not None and service is not None and lifecycle is not None
        self.assertEqual(app.desired_state, "suspended")
        self.assertEqual(service.desired_state, "suspended")
        # Observed state remains evidence from the runtime, not an optimistic
        # copy of the newly requested state.
        self.assertEqual(app.observed_state, "running")
        self.assertEqual(service.observed_state, "running")
        self.assertEqual(lifecycle.source_deployment_id, self.current_deployment_id)

        with self.assertRaises(ApplicationLifecycleConflict):
            await self.request("resume", "resume-conflict")
        await self.finish(suspend_operation_id)

        resumed = await self.request("resume", "resume-1")
        await self.finish(resumed.body["operation"]["id"])
        rolled_back = await self.request("rollback", "rollback-1")
        rollback_operation_id = rolled_back.body["operation"]["id"]
        async with self.sessions() as session:
            lifecycle = await session.get(
                ApplicationLifecycleRequest, rollback_operation_id
            )
            app = await session.get(Application, self.application_id)
        assert lifecycle is not None and app is not None
        self.assertEqual(
            lifecycle.rollback_deployment_id, self.previous_deployment_id
        )
        self.assertEqual(app.desired_state, "running")
        self.assertEqual(app.observed_state, "running")

        foreign_command = RequestApplicationAction(
            self.other_scope, self.application_id, "restart"
        )
        with self.assertRaises(ResourceNotFound):
            await self.store.request(
                foreign_command,
                principal=self.principal,
                idempotency=self.store.idempotency(
                    foreign_command, key="foreign-restart"
                ),
            )

    async def test_worker_rollback_late_cancel_reclaim_and_failure_restore_are_atomic(
        self,
    ) -> None:
        operations = OperationStore(self.sessions)
        states = PostgresLifecycleStateStore(self.sessions)
        loader = PostgresLifecycleContextLoader(
            self.sessions, luma_cluster_id="luma-primary"
        )
        worker_id = "lifecycle-postgres-integration"

        requested = await self.request("rollback", "worker-rollback")
        claimed = await operations.claim_next(
            worker_id=worker_id,
            kinds=("application.rollback",),
            lease_seconds=60,
        )
        assert claimed is not None
        context = await loader.load(claimed)
        assert context.target is not None
        self.assertEqual(context.target.deployment_id, self.previous_deployment_id)
        with self.assertRaises(LifecycleContextInvalid):
            await loader.load(replace(claimed, tenant_id=self.other_scope.tenant_id))

        running = await states.mark_runtime_started(
            claimed, context, worker_id=worker_id
        )
        completed = await states.succeed(
            running,
            context,
            RuntimeDeployment(
                deployment_ref=context.target.runtime_deployment_ref,
                status="running",
                manifest_digest=context.target.manifest_digest,
                service_statuses={"web": "healthy"},
                route_statuses={},
                volume_bindings=(),
            ),
            worker_id=worker_id,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.id, requested.body["operation"]["id"])

        async with self.sessions() as session:
            application = await session.get(Application, self.application_id)
            current_revision = await session.get(
                AppRevision, self.current_revision_id
            )
            previous_revision = await session.get(
                AppRevision, self.previous_revision_id
            )
            service = await session.scalar(
                select(ApplicationService).where(
                    ApplicationService.application_id == self.application_id
                )
            )
        assert application is not None
        assert current_revision is not None and previous_revision is not None
        assert service is not None
        self.assertEqual(application.current_deployment_id, self.previous_deployment_id)
        self.assertEqual(application.current_revision_id, self.previous_revision_id)
        self.assertEqual(current_revision.status, "superseded")
        self.assertEqual(previous_revision.status, "active")
        self.assertEqual(service.current_image_digest, PREVIOUS_IMAGE_SHA)

        # Once runtime submission is durable, a cancellation cannot win by
        # merely expiring the worker lease. The replacement worker must
        # observe and commit the external outcome.
        suspended = await self.request("suspend", "worker-suspend")
        suspend_operation_id = suspended.body["operation"]["id"]
        first_claim = await operations.claim_next(
            worker_id=worker_id,
            kinds=("application.suspend",),
            lease_seconds=60,
        )
        assert first_claim is not None
        suspend_context = await loader.load(first_claim)
        await states.mark_runtime_started(
            first_claim, suspend_context, worker_id=worker_id
        )
        await operations.request_cancel(self.scope, suspend_operation_id)
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Operation)
                    .where(Operation.id == suspend_operation_id)
                    .values(
                        lease_expires_at=datetime.now(timezone.utc)
                        - timedelta(seconds=5)
                    )
                )
        reclaimed = await operations.claim_next(
            worker_id=worker_id,
            kinds=("application.suspend",),
            lease_seconds=60,
        )
        assert reclaimed is not None
        self.assertEqual(reclaimed.status, "running")
        self.assertEqual(reclaimed.phase, "application.lifecycle.runtime")
        self.assertTrue(reclaimed.cancel_requested)
        suspend_context = await loader.load(reclaimed)
        assert suspend_context.source is not None
        suspended_operation = await states.succeed(
            reclaimed,
            suspend_context,
            RuntimeDeployment(
                deployment_ref=suspend_context.source.runtime_deployment_ref,
                status="suspended",
                manifest_digest=suspend_context.source.manifest_digest,
                service_statuses={"web": "suspended"},
                route_statuses={},
                volume_bindings=(),
            ),
            worker_id=worker_id,
        )
        self.assertEqual(suspended_operation.status, "succeeded")

        resumed = await self.request("resume", "worker-resume-failure")
        resume_operation_id = resumed.body["operation"]["id"]
        resume_claim = await operations.claim_next(
            worker_id=worker_id,
            kinds=("application.resume",),
            lease_seconds=60,
        )
        assert resume_claim is not None
        resume_context = await loader.load(resume_claim)
        resume_running = await states.mark_runtime_started(
            resume_claim, resume_context, worker_id=worker_id
        )
        failed = await states.fail(
            resume_running,
            resume_context,
            LifecycleRuntimeFailed(),
            worker_id=worker_id,
        )
        self.assertEqual(failed.status, "failed")

        async with self.sessions() as session:
            application = await session.get(Application, self.application_id)
            service = await session.scalar(
                select(ApplicationService).where(
                    ApplicationService.application_id == self.application_id
                )
            )
            operation = await session.get(Operation, resume_operation_id)
            late_cancel_events = int(
                await session.scalar(
                    select(func.count())
                    .select_from(OperationEvent)
                    .where(
                        OperationEvent.operation_id == suspend_operation_id,
                        OperationEvent.type
                        == "application.lifecycle.cancel-too-late",
                    )
                )
                or 0
            )
        assert application is not None and service is not None and operation is not None
        self.assertEqual(application.desired_state, "suspended")
        self.assertEqual(application.observed_state, "unknown")
        self.assertEqual(service.desired_state, "suspended")
        self.assertEqual(operation.error_code, LifecycleRuntimeFailed.code)
        self.assertEqual(late_cancel_events, 1)


if __name__ == "__main__":
    unittest.main()
