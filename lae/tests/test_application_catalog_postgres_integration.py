from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_store.application_catalog import (  # noqa: E402
    ApplicationCatalogStore,
    ApplicationRecord,
    CreateApplication,
    CreateApplicationDraft,
    CreateDeployment,
    CreateRevision,
    EncryptedEnvironmentValue,
    EnvironmentKey,
    HttpRouteSpec,
    MaterializeApplicationTopology,
    PatchEnvironment,
    ServiceSpec,
    VolumeSpec,
)
from lae_store.engine import create_postgres_engine, create_session_factory  # noqa: E402
from lae_store.errors import (  # noqa: E402
    ApplicationAlreadyMaterialized,
    ApplicationQuotaExceeded,
    EnvironmentVersionConflict,
    ResourceNotFound,
)
from lae_store.ids import new_id  # noqa: E402
from lae_store.models import (  # noqa: E402
    Analysis,
    ApplicationEnvironmentVariable,
    Artifact,
    Operation,
    PlanVersion,
    SourceRevision,
    Subscription,
    Tenant,
    TenantMember,
    User,
)
from lae_store.repositories import Principal, TenantScope  # noqa: E402

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"
SHA = "sha256:" + "a" * 64


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
class ApplicationCatalogPostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.store = ApplicationCatalogStore(self.sessions)
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.other_tenant_id = new_id("ten")
        self.scope = TenantScope(self.tenant_id)
        self.other_scope = TenantScope(self.other_tenant_id)
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
                            name="Catalog Tenant",
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

    def _compose_topology(
        self, application_id: str, analysis_id: str
    ) -> MaterializeApplicationTopology:
        return MaterializeApplicationTopology(
            scope=self.scope,
            application_id=application_id,
            analysis_id=analysis_id,
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
            environment=(
                EncryptedEnvironmentValue(
                    service_scope="web",
                    name="DATABASE_URL",
                    envelope_ciphertext=b"envelope:v1:ciphertext",
                    checksum=b"c" * 32,
                    key_version=1,
                ),
            ),
        )

    async def _seed_deployable_analysis(
        self, application_id: str
    ) -> tuple[str, str, str]:
        source_id = new_id("src")
        analysis_operation_id = new_id("op")
        analysis_id = new_id("ana")
        artifact_id = new_id("art")
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    SourceRevision(
                        id=source_id,
                        tenant_id=self.tenant_id,
                        application_id=application_id,
                        kind="git",
                        repository="acme/example",
                        ref="main",
                        resolved_commit_full="b" * 40,
                        source_tree_digest=SHA,
                        snapshot_id="snapshot-catalog",
                        snapshot_digest=SHA,
                    )
                )
                session.add(
                    Operation(
                        id=analysis_operation_id,
                        tenant_id=self.tenant_id,
                        principal_type="user",
                        principal_id=self.user_id,
                        kind="analysis.create",
                        target_type="application",
                        target_id=application_id,
                        status="succeeded",
                        result={},
                    )
                )
                session.add(
                    Artifact(
                        id=artifact_id,
                        tenant_id=self.tenant_id,
                        kind="deployment-plan",
                        digest=SHA,
                        media_type="application/vnd.lae.deployment-plan+json",
                        size_bytes=128,
                        storage_key=f"tenant/{self.tenant_id}/plan.json",
                        upload_status="verified",
                        verified_at=now,
                    )
                )
                await session.flush()
                session.add(
                    Analysis(
                        id=analysis_id,
                        tenant_id=self.tenant_id,
                        application_id=application_id,
                        source_revision_id=source_id,
                        operation_id=analysis_operation_id,
                        status="deployable",
                        policy_version="policy-v1",
                        agent_image_digest=f"registry.internal/lae-agent@{SHA}",
                        resolved_commit_full="b" * 40,
                        source_tree_digest=SHA,
                        source_snapshot_id="snapshot-catalog",
                        source_snapshot_digest=SHA,
                        deployment_plan_digest=SHA,
                        build_plan_digest=SHA,
                        evidence_digest=SHA,
                        artifact_state="stored",
                        plan_stored=True,
                    )
                )
        return source_id, analysis_id, artifact_id

    async def test_catalog_tenant_cas_quota_and_lifecycle_facts(self) -> None:
        draft = await self.store.create_application_draft(
            CreateApplicationDraft(
                scope=self.scope,
                name="Integration Compose",
                slug="integration-compose",
            )
        )
        application_id = draft.application.id
        self.assertEqual(draft.application.kind, "pending")
        self.assertEqual(draft.services, ())
        self.assertEqual(draft.routes, ())
        self.assertEqual(draft.volumes, ())

        with self.assertRaises(ResourceNotFound):
            await self.store.materialize_topology(
                self._compose_topology(application_id, new_id("ana"))
            )
        source_id, analysis_id, artifact_id = await self._seed_deployable_analysis(
            application_id
        )
        created = await self.store.materialize_topology(
            self._compose_topology(application_id, analysis_id)
        )
        with self.assertRaises(ApplicationAlreadyMaterialized):
            await self.store.materialize_topology(
                self._compose_topology(application_id, analysis_id)
            )
        self.assertEqual(created.application.kind, "compose")
        self.assertEqual(len(created.services), 3)
        self.assertEqual(len(created.routes), 2)
        self.assertTrue(
            all(route.hostname.endswith(".itool.tech") for route in created.routes)
        )
        self.assertEqual(len({route.hostname for route in created.routes}), 2)
        self.assertEqual(created.environment.version, 1)
        self.assertEqual(len(created.environment.variables), 1)
        self.assertFalse(
            hasattr(created.environment.variables[0], "envelope_ciphertext")
        )

        with self.assertRaises(ResourceNotFound):
            await self.store.get_application(self.other_scope, application_id)
        self.assertEqual(await self.store.list_applications(self.other_scope), ())

        patches = await asyncio.gather(
            self.store.patch_environment(
                PatchEnvironment(
                    scope=self.scope,
                    application_id=application_id,
                    expected_version=1,
                    set_values=(
                        EncryptedEnvironmentValue(
                            service_scope="web",
                            name="API_KEY",
                            envelope_ciphertext=b"envelope:v2:first",
                            checksum=b"1" * 32,
                            key_version=2,
                        ),
                    ),
                )
            ),
            self.store.patch_environment(
                PatchEnvironment(
                    scope=self.scope,
                    application_id=application_id,
                    expected_version=1,
                    unset=(EnvironmentKey("web", "DATABASE_URL"),),
                )
            ),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(not isinstance(item, BaseException) for item in patches), 1
        )
        self.assertEqual(
            sum(isinstance(item, EnvironmentVersionConflict) for item in patches), 1
        )
        environment = await self.store.get_environment(self.scope, application_id)
        self.assertEqual(environment.version, 2)

        suspended = await self.store.set_desired_state(
            self.scope, application_id, "suspended"
        )
        self.assertEqual(suspended.desired_state, "suspended")
        self.assertEqual(suspended.observed_state, "unknown")
        running_observed = await self.store.set_observed_state(
            self.scope, application_id, "running"
        )
        self.assertEqual(running_observed.desired_state, "suspended")
        self.assertEqual(running_observed.observed_state, "running")

        quota_results = await asyncio.gather(
            *[
                self.store.create_application(
                    CreateApplication(
                        scope=self.scope,
                        name=f"Quota App {index}",
                        slug=f"quota-app-{index}",
                        kind="service",
                        services=(ServiceSpec("web", "http"),),
                        routes=(HttpRouteSpec("web", 8000, is_primary=True),),
                    )
                )
                for index in range(3)
            ],
            return_exceptions=True,
        )
        self.assertEqual(
            sum(isinstance(item, ApplicationRecord) for item in quota_results), 2
        )
        self.assertEqual(
            sum(isinstance(item, ApplicationQuotaExceeded) for item in quota_results),
            1,
        )

        revision = await self.store.create_revision(
            CreateRevision(
                scope=self.scope,
                principal=Principal("user", self.user_id),
                application_id=application_id,
                analysis_id=analysis_id,
                source_revision_id=source_id,
                deployment_plan_artifact_id=artifact_id,
                deployment_plan_digest=SHA,
                normalized_compose_digest=SHA,
                luma_manifest_digest=SHA,
                environment_schema_digest=SHA,
                environment_version=environment.version,
            )
        )
        self.assertEqual(revision.revision_no, 1)
        self.assertEqual(revision.status, "candidate")

        deployment_operation_id = new_id("op")
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    Operation(
                        id=deployment_operation_id,
                        tenant_id=self.tenant_id,
                        principal_type="user",
                        principal_id=self.user_id,
                        kind="deployment.create",
                        target_type="application",
                        target_id=application_id,
                        status="queued",
                    )
                )
        deployment = await self.store.create_deployment(
            CreateDeployment(
                scope=self.scope,
                application_id=application_id,
                revision_id=revision.id,
                operation_id=deployment_operation_id,
            )
        )
        self.assertEqual(deployment.status, "queued")
        self.assertEqual(
            (
                await self.store.get_deployment(
                    self.scope, application_id, deployment.id
                )
            ).id,
            deployment.id,
        )
        with self.assertRaises(ResourceNotFound):
            await self.store.get_deployment(
                self.other_scope, application_id, deployment.id
            )

        async with self.sessions() as session:
            persisted = await session.scalar(
                select(ApplicationEnvironmentVariable).where(
                    ApplicationEnvironmentVariable.tenant_id == self.tenant_id,
                    ApplicationEnvironmentVariable.application_id == application_id,
                )
            )
        if persisted is not None:
            self.assertIsInstance(persisted.value_ciphertext, bytes)
            self.assertEqual(len(persisted.value_checksum), 32)


if __name__ == "__main__":
    unittest.main()
