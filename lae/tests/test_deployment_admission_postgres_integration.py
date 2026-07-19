from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
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
    DeploymentChangeConfirmationRequired,
    IdempotencyInput,
    OperationStore,
    Principal,
    TenantScope,
    UpdateChangeSet,
    UpdateCheckResult,
    UpdatePlanChanges,
    create_postgres_engine,
    create_session_factory,
    keyed_request_hash,
    new_id,
)
from lae_store.deployment_admission import (  # noqa: E402
    DEPLOYMENT_CREATE_ROUTE,
    CreateDeploymentAdmission,
    DeploymentAdmissionStore,
    PreparedDeploymentPlan,
    PreparedHttpRoute,
    PreparedService,
    PreparedVolume,
)
from lae_store.errors import (  # noqa: E402
    DeploymentConflict,
    DeploymentTopologyConflict,
    EnvironmentVersionConflict,
    IdempotencyKeyReused,
    ResourceNotFound,
)
from lae_store.models import (  # noqa: E402
    Analysis,
    AnalysisArtifact,
    AppRevision,
    Application,
    ApplicationRoute,
    ApplicationService,
    ApplicationVolume,
    Artifact,
    Deployment,
    IdempotencyRecord,
    Operation,
    OperationEvent,
    OutboxEvent,
    PlanVersion,
    SourceRevision,
    Subscription,
    Tenant,
    TenantMember,
    User,
)
from lae_worker.deployment_postgres import (  # noqa: E402
    PostgresDeploymentContextLoader,
    TrustedBuildPlan,
    TrustedRuntimeRoute,
    TrustedRuntimeService,
    TrustedRuntimeVolume,
)

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"
PLAN_DIGEST = "sha256:" + "a" * 64


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
class DeploymentAdmissionPostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.store = DeploymentAdmissionStore(self.sessions)
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.other_tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.other_application_id = new_id("app")
        self.scope = TenantScope(self.tenant_id)
        self.other_scope = TenantScope(self.other_tenant_id)
        self.principal = Principal("deploy-token", new_id("dtk"))
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
                            name="Deployment tenant",
                            slug=self.tenant_id.lower(),
                            status="active",
                            owner_user_id=self.user_id,
                        ),
                        Tenant(
                            id=self.other_tenant_id,
                            type="organization",
                            name="Other tenant",
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
                plan_version_id = await session.scalar(
                    select(PlanVersion.id).where(
                        PlanVersion.code == "lite", PlanVersion.version == 1
                    )
                )
                assert plan_version_id is not None
                session.add(
                    Subscription(
                        id=new_id("sub"),
                        tenant_id=self.tenant_id,
                        plan_version_id=plan_version_id,
                        interval="none",
                        status="active",
                        provider="internal",
                    )
                )
                session.add_all(
                    [
                        Application(
                            id=self.application_id,
                            tenant_id=self.tenant_id,
                            name="Pending compose",
                            slug="pending-compose",
                            luma_name=f"lae-{self.application_id.lower()}",
                            kind="pending",
                            environment_version=0,
                        ),
                        Application(
                            id=self.other_application_id,
                            tenant_id=self.other_tenant_id,
                            name="Foreign app",
                            slug="foreign-app",
                            luma_name=f"lae-{self.other_application_id.lower()}",
                            kind="pending",
                            environment_version=0,
                        ),
                    ]
                )
        (
            self.source_revision_id,
            self.analysis_id,
            self.artifact_id,
        ) = await self._seed_analysis()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    async def _seed_analysis(self) -> tuple[str, str, str]:
        source_revision_id = new_id("src")
        analysis_id = new_id("ana")
        artifact_id = new_id("art")
        build_artifact_id = new_id("art")
        operation_id = new_id("op")
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    SourceRevision(
                        id=source_revision_id,
                        tenant_id=self.tenant_id,
                        application_id=self.application_id,
                        kind="git",
                        repository="https://github.com/acme/example.git",
                        ref="main",
                        resolved_commit_full="1" * 40,
                        source_tree_digest="sha256:" + "2" * 64,
                        snapshot_id="snapshot-deployment-admission",
                        snapshot_digest="sha256:" + "3" * 64,
                    )
                )
                session.add(
                    Operation(
                        id=operation_id,
                        tenant_id=self.tenant_id,
                        principal_type="session",
                        principal_id=self.user_id,
                        kind="source.analyze",
                        target_type="source-revision",
                        target_id=source_revision_id,
                        status="succeeded",
                        result={"analysisId": analysis_id},
                    )
                )
                session.add(
                    Artifact(
                        id=artifact_id,
                        tenant_id=self.tenant_id,
                        kind="deployment-plan",
                        digest=PLAN_DIGEST,
                        media_type="application/vnd.lae.deployment-plan+json",
                        size_bytes=512,
                        storage_key=(
                            f"tenants/{self.tenant_id}/analysis-artifacts/"
                            "deployment-plan/plan.json"
                        ),
                        upload_status="verified",
                        verified_at=now,
                    )
                )
                session.add(
                    Artifact(
                        id=build_artifact_id,
                        tenant_id=self.tenant_id,
                        kind="build-plan-candidate",
                        digest="sha256:" + "5" * 64,
                        media_type=(
                            "application/vnd.lae.build-plan-candidate+json"
                        ),
                        size_bytes=256,
                        storage_key=(
                            f"tenants/{self.tenant_id}/analysis-artifacts/"
                            "build-plan-candidate/plan.json"
                        ),
                        upload_status="verified",
                        verified_at=now,
                    )
                )
                await session.flush()
                session.add(
                    Analysis(
                        id=analysis_id,
                        tenant_id=self.tenant_id,
                        application_id=self.application_id,
                        source_revision_id=source_revision_id,
                        operation_id=operation_id,
                        status="deployable",
                        policy_version="policy-v1",
                        agent_image_digest=(
                            "registry.internal/lae-agent@sha256:" + "4" * 64
                        ),
                        resolved_commit_full="1" * 40,
                        source_tree_digest="sha256:" + "2" * 64,
                        source_snapshot_id="snapshot-deployment-admission",
                        source_snapshot_digest="sha256:" + "3" * 64,
                        deployment_plan_digest=PLAN_DIGEST,
                        build_plan_digest="sha256:" + "5" * 64,
                        evidence_digest="sha256:" + "6" * 64,
                        artifact_state="stored",
                        plan_stored=True,
                    )
                )
                await session.flush()
                session.add(
                    AnalysisArtifact(
                        tenant_id=self.tenant_id,
                        analysis_id=analysis_id,
                        name="deploymentPlan",
                        artifact_id=artifact_id,
                    )
                )
                session.add(
                    AnalysisArtifact(
                        tenant_id=self.tenant_id,
                        analysis_id=analysis_id,
                        name="buildPlan",
                        artifact_id=build_artifact_id,
                    )
                )
        return source_revision_id, analysis_id, artifact_id

    def prepared_plan(self, *, admin_port: int = 9090) -> PreparedDeploymentPlan:
        return PreparedDeploymentPlan(
            source_revision_id=self.source_revision_id,
            kind="compose",
            services=(
                PreparedService("web", "http"),
                PreparedService("admin", "http"),
                PreparedService("database", "datastore"),
            ),
            routes=(
                PreparedHttpRoute("web", 8080, is_primary=True),
                PreparedHttpRoute("admin", admin_port),
            ),
            volumes=(PreparedVolume("database", 64 * 1024 * 1024),),
            environment=(),
            luma_manifest_digest=None,
            environment_schema_digest="sha256:" + "b" * 64,
            normalized_compose_digest="sha256:" + "c" * 64,
        )

    def request_hash(self, *, environment_version: int = 0) -> bytes:
        return keyed_request_hash(
            {
                "applicationId": self.application_id,
                "analysisId": self.analysis_id,
                "environmentVersion": environment_version,
            },
            b"deployment-integration-hmac-key".ljust(32, b"!"),
        )

    def idempotency(
        self,
        key: str,
        *,
        request_hash: bytes | None = None,
    ) -> IdempotencyInput:
        return IdempotencyInput(
            key=key,
            method="POST",
            route_template=DEPLOYMENT_CREATE_ROUTE,
            request_hash=request_hash or self.request_hash(),
        )

    async def test_atomic_idempotent_admission_materializes_and_tenant_fences(
        self,
    ) -> None:
        artifact = await self.store.get_plan_artifact(
            self.scope, self.application_id, self.analysis_id
        )
        self.assertNotIn(artifact.storage_key, repr(artifact))
        command = CreateDeploymentAdmission(
            scope=self.scope,
            application_id=self.application_id,
            analysis_id=self.analysis_id,
            environment_version=0,
        )
        admitted = await asyncio.gather(
            *[
                self.store.admit(
                    command,
                    principal=self.principal,
                    idempotency=self.idempotency("deployment-admit-1"),
                    artifact=artifact,
                    plan=self.prepared_plan(),
                )
                for _ in range(2)
            ]
        )
        self.assertEqual(admitted[0].public_body(), admitted[1].public_body())
        self.assertEqual({item.replayed for item in admitted}, {False, True})
        first = admitted[0]
        public = str(first.public_body()).lower()
        for forbidden in (
            artifact.storage_key.lower(),
            "manifest",
            "image",
            "luma",
            "environmentvalue",
        ):
            self.assertNotIn(forbidden, public)

        async with self.sessions() as session:
            counts = {
                model.__tablename__: int(
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.tenant_id == self.tenant_id)
                    )
                    or 0
                )
                for model in (
                    AppRevision,
                    Deployment,
                    OperationEvent,
                    OutboxEvent,
                    IdempotencyRecord,
                )
            }
            deployment_operations = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Operation)
                    .where(
                        Operation.tenant_id == self.tenant_id,
                        Operation.kind == "deployment.create",
                    )
                )
                or 0
            )
            application = await session.get(Application, self.application_id)
            services = list(
                await session.scalars(
                    select(ApplicationService).where(
                        ApplicationService.tenant_id == self.tenant_id,
                        ApplicationService.application_id == self.application_id,
                    )
                )
            )
            routes = list(
                await session.scalars(
                    select(ApplicationRoute).where(
                        ApplicationRoute.tenant_id == self.tenant_id,
                        ApplicationRoute.application_id == self.application_id,
                    )
                )
            )
            volumes = list(
                await session.scalars(
                    select(ApplicationVolume).where(
                        ApplicationVolume.tenant_id == self.tenant_id,
                        ApplicationVolume.application_id == self.application_id,
                    )
                )
            )
        self.assertEqual(counts, {name: 1 for name in counts})
        self.assertEqual(deployment_operations, 1)
        assert application is not None
        self.assertEqual(application.kind, "compose")
        self.assertEqual(len(services), 3)
        self.assertEqual(len(routes), 2)
        self.assertEqual(len({route.hostname for route in routes}), 2)
        self.assertTrue(all(route.hostname.endswith(".itool.tech") for route in routes))
        self.assertEqual(len(volumes), 1)

        with self.assertRaises(IdempotencyKeyReused):
            await self.store.admit(
                command,
                principal=self.principal,
                idempotency=self.idempotency(
                    "deployment-admit-1", request_hash=b"z" * 32
                ),
                artifact=artifact,
                plan=self.prepared_plan(),
            )
        with self.assertRaises(DeploymentConflict):
            await self.store.admit(
                command,
                principal=self.principal,
                idempotency=self.idempotency("deployment-admit-2"),
                artifact=artifact,
                plan=self.prepared_plan(),
            )
        with self.assertRaises(ResourceNotFound):
            await self.store.get_plan_artifact(
                self.other_scope, self.application_id, self.analysis_id
            )
        with self.assertRaises(ResourceNotFound):
            await self.store.get_deployment(
                self.other_scope,
                self.application_id,
                first.deployment.id,
            )
        with self.assertRaises(ResourceNotFound):
            await self.store.list_deployments(self.other_scope, self.application_id)

    async def test_update_candidate_requires_exact_operation_bound_confirmation(
        self,
    ) -> None:
        artifact = await self.store.get_plan_artifact(
            self.scope, self.application_id, self.analysis_id
        )
        changes = UpdatePlanChanges(
            services=UpdateChangeSet(removed=("legacy-worker",)),
            destructive=True,
            confirmations=("SERVICE_REMOVAL",),
        )
        comparison = UpdateCheckResult(
            baseline_available=True,
            source_changed=True,
            deployment_plan_changed=True,
            changed=True,
            candidate_source_tree_digest="sha256:" + "2" * 64,
            candidate_deployment_plan_digest=PLAN_DIGEST,
            baseline_source_tree_digest="sha256:" + "7" * 64,
            baseline_deployment_plan_digest="sha256:" + "8" * 64,
            plan_changes=changes,
            candidate_analysis_id=self.analysis_id,
            candidate_verdict="deployable",
        )
        async with self.sessions() as session:
            async with session.begin():
                analysis = await session.get(Analysis, self.analysis_id)
                assert analysis is not None
                analysis.verdict = "deployable"
                operation = await session.get(Operation, analysis.operation_id)
                assert operation is not None
                operation.kind = "application.check-update"
                operation.result = {"updateCheck": comparison.to_body()}

        with self.assertRaises(DeploymentChangeConfirmationRequired) as required:
            await self.store.admit(
                CreateDeploymentAdmission(
                    scope=self.scope,
                    application_id=self.application_id,
                    analysis_id=self.analysis_id,
                    environment_version=0,
                ),
                principal=self.principal,
                idempotency=self.idempotency("update-confirmation-missing"),
                artifact=artifact,
                plan=self.prepared_plan(),
            )
        self.assertEqual(required.exception.required, ("SERVICE_REMOVAL",))

        admitted = await self.store.admit(
            CreateDeploymentAdmission(
                scope=self.scope,
                application_id=self.application_id,
                analysis_id=self.analysis_id,
                environment_version=0,
                confirmed_changes=("SERVICE_REMOVAL",),
            ),
            principal=self.principal,
            idempotency=self.idempotency(
                "update-confirmation-exact",
                request_hash=keyed_request_hash(
                    {
                        "applicationId": self.application_id,
                        "analysisId": self.analysis_id,
                        "environmentVersion": 0,
                        "confirmedChanges": ["SERVICE_REMOVAL"],
                    },
                    b"deployment-integration-hmac-key".ljust(32, b"!"),
                ),
            ),
            artifact=artifact,
            plan=self.prepared_plan(),
        )
        self.assertEqual(admitted.deployment.application_id, self.application_id)

    async def test_version_and_materialized_topology_changes_are_rejected(self) -> None:
        artifact = await self.store.get_plan_artifact(
            self.scope, self.application_id, self.analysis_id
        )
        with self.assertRaises(EnvironmentVersionConflict):
            await self.store.admit(
                CreateDeploymentAdmission(
                    scope=self.scope,
                    application_id=self.application_id,
                    analysis_id=self.analysis_id,
                    environment_version=1,
                ),
                principal=self.principal,
                idempotency=self.idempotency(
                    "wrong-environment",
                    request_hash=self.request_hash(environment_version=1),
                ),
                artifact=artifact,
                plan=self.prepared_plan(),
            )
        command = CreateDeploymentAdmission(
            scope=self.scope,
            application_id=self.application_id,
            analysis_id=self.analysis_id,
            environment_version=0,
        )
        first = await self.store.admit(
            command,
            principal=self.principal,
            idempotency=self.idempotency("topology-initial"),
            artifact=artifact,
            plan=self.prepared_plan(),
        )
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            async with session.begin():
                operation = await session.get(Operation, first.operation_id)
                deployment = await session.get(Deployment, first.deployment.id)
                assert operation is not None and deployment is not None
                operation.status = "succeeded"
                operation.finished_at = now
                deployment.status = "succeeded"
                deployment.finished_at = now
        with self.assertRaises(DeploymentTopologyConflict):
            await self.store.admit(
                command,
                principal=self.principal,
                idempotency=self.idempotency("topology-changed"),
                artifact=artifact,
                plan=self.prepared_plan(admin_port=9191),
            )
        async with self.sessions() as session:
            self.assertEqual(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(AppRevision)
                        .where(AppRevision.tenant_id == self.tenant_id)
                    )
                    or 0
                ),
                1,
            )

    async def test_needs_configuration_plan_remains_resolvable_for_env_retry(
        self,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                analysis = await session.get(Analysis, self.analysis_id)
                assert analysis is not None
                analysis.status = "needs_configuration"
        artifact = await self.store.get_plan_artifact(
            self.scope, self.application_id, self.analysis_id
        )
        self.assertEqual(artifact.analysis_id, self.analysis_id)
        self.assertEqual(artifact.source_snapshot_digest, "sha256:" + "3" * 64)

        admitted = await self.store.admit(
            CreateDeploymentAdmission(
                scope=self.scope,
                application_id=self.application_id,
                analysis_id=self.analysis_id,
                environment_version=0,
            ),
            principal=self.principal,
            idempotency=self.idempotency("needs-configuration-deploy"),
            artifact=artifact,
            plan=self.prepared_plan(),
        )

        class Materializer:
            async def materialize(self, *_args, **_kwargs):
                return TrustedBuildPlan(
                    signed_build_plan={"schemaVersion": "lae.build-plan/v1"},
                    credential_lease_id="bcred_" + "a" * 32,
                    service_build_keys={
                        "admin": "admin",
                        "database": "database",
                        "web": "web",
                    },
                    kind="compose",
                    services=(
                        TrustedRuntimeService(
                            "admin", "http", "admin", None, (), "0.25", 256,
                            (), 9090, "/", 10,
                        ),
                        TrustedRuntimeService(
                            "database", "datastore", "database", None, (),
                            "0.25", 256, (), None, None, None,
                        ),
                        TrustedRuntimeService(
                            "web", "http", "web", None, (), "0.25", 256,
                            (), 8080, "/", 10,
                        ),
                    ),
                    routes=(
                        TrustedRuntimeRoute("admin", 9090, "/"),
                        TrustedRuntimeRoute("web", 8080, "/"),
                    ),
                    volumes=(
                        TrustedRuntimeVolume(
                            "database",
                            64 * 1024 * 1024,
                            ("database",),
                            "/var/lib/database",
                            "ReadWriteOnce",
                        ),
                    ),
                )

        operation = await OperationStore(self.sessions).claim_next(
            worker_id="needs-configuration-integration",
            kinds=("deployment.create",),
            lease_seconds=60,
        )
        assert operation is not None and operation.id == admitted.operation_id
        context = await PostgresDeploymentContextLoader(
            self.sessions, Materializer(), region="cn"
        ).load(operation)
        self.assertEqual(context.analysis_ref, self.analysis_id)
        self.assertEqual({service.key for service in context.services}, {
            "admin", "database", "web"
        })

        async with self.sessions() as session:
            async with session.begin():
                analysis = await session.get(Analysis, self.analysis_id)
                assert analysis is not None
                analysis.status = "not_deployable"
        with self.assertRaises(ResourceNotFound):
            await self.store.get_plan_artifact(
                self.scope, self.application_id, self.analysis_id
            )


if __name__ == "__main__":
    unittest.main()
