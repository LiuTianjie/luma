from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "apps/api/src",
    "packages/python/lae-store/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_api.deployment_api import (  # noqa: E402
    DeploymentApiService,
    DeploymentCreateRequest,
    PlanEnvironmentPatchRequest,
)
from lae_api.plan_resolver import DeploymentConfigurationSchema  # noqa: E402
from lae_store import (  # noqa: E402
    DeploymentEnvironmentSchemaConflict,
    IdempotencyInput,
    IdempotentCatalogResult,
    Principal,
    TenantScope,
    new_id,
)
from lae_store.deployment_admission import (  # noqa: E402
    DeploymentAdmissionResult,
    PreparedDeploymentPlan,
    PreparedEnvironmentVariable,
    PreparedHttpRoute,
    PreparedService,
    PreparedVolume,
    PublicDeploymentRecord,
    StoredDeploymentPlanArtifact,
    UnconfiguredPlanResolver,
    _validate_environment_bindings,
)
from lae_store.errors import (  # noqa: E402
    DeploymentEnvironmentIncomplete,
    DeploymentEnvironmentScopeInvalid,
    DeploymentPlanInvalid,
    DeploymentPlanUnavailable,
)

SHA = "sha256:" + "a" * 64


def plan(source_revision_id: str) -> PreparedDeploymentPlan:
    return PreparedDeploymentPlan(
        source_revision_id=source_revision_id,
        kind="compose",
        services=(
            PreparedService("web", "http"),
            PreparedService("admin", "http"),
            PreparedService("database", "datastore"),
        ),
        routes=(
            PreparedHttpRoute("web", 8080, is_primary=True),
            PreparedHttpRoute("admin", 9090),
        ),
        volumes=(PreparedVolume("database", 64 * 1024 * 1024),),
        environment=(
            PreparedEnvironmentVariable(
                "DATABASE_URL", ("web", "admin"), required=True
            ),
        ),
        luma_manifest_digest=None,
        environment_schema_digest="sha256:" + "b" * 64,
        normalized_compose_digest="sha256:" + "c" * 64,
    )


class PreparedPlanTests(unittest.TestCase):
    def test_compose_plan_supports_multiple_public_http_routes_and_named_volume(
        self,
    ) -> None:
        prepared = plan(new_id("src"))
        self.assertEqual(len(prepared.routes), 2)
        self.assertEqual(
            {route.service_key for route in prepared.routes}, {"web", "admin"}
        )
        self.assertEqual(prepared.volumes[0].volume_key, "database")

    def test_plan_shape_has_no_tcp_udp_or_host_escape_surface(self) -> None:
        fields = {
            field.name
            for value in (
                PreparedService,
                PreparedHttpRoute,
                PreparedVolume,
                PreparedDeploymentPlan,
            )
            for field in dataclasses.fields(value)
        }
        for forbidden in (
            "protocol",
            "publish_port",
            "host_port",
            "host_path",
            "node",
            "network_mode",
            "privileged",
            "manifest",
        ):
            self.assertNotIn(forbidden, fields)
        with self.assertRaises(DeploymentPlanInvalid):
            PreparedService("relay", "tcp")
        with self.assertRaises(DeploymentPlanInvalid):
            PreparedService("cron", "cron")

    def test_artifact_storage_key_is_redacted_and_default_resolver_fails_closed(
        self,
    ) -> None:
        artifact = StoredDeploymentPlanArtifact(
            artifact_id=new_id("art"),
            analysis_id=new_id("ana"),
            source_revision_id=new_id("src"),
            digest=SHA,
            media_type="application/vnd.lae.deployment-plan+json",
            size_bytes=128,
            storage_key="tenant/private/deployment-plan.json",
        )
        self.assertNotIn("tenant/private", repr(artifact))
        with self.assertRaises(DeploymentPlanUnavailable):
            asyncio.run(UnconfiguredPlanResolver().resolve(artifact))

    def test_compose_wildcard_is_only_compatible_with_an_all_service_binding(
        self,
    ) -> None:
        prepared = plan(new_id("src"))
        with self.assertRaises(DeploymentEnvironmentScopeInvalid):
            _validate_environment_bindings({("*", "DATABASE_URL")}, prepared)
        with self.assertRaises(DeploymentEnvironmentScopeInvalid):
            _validate_environment_bindings({("database", "DATABASE_URL")}, prepared)
        _validate_environment_bindings(
            {("web", "DATABASE_URL"), ("admin", "DATABASE_URL")},
            prepared,
        )

        all_services = dataclasses.replace(
            prepared,
            environment=(
                PreparedEnvironmentVariable(
                    "SHARED_TOKEN",
                    ("web", "admin", "database"),
                    required=True,
                ),
            ),
        )
        _validate_environment_bindings({("*", "SHARED_TOKEN")}, all_services)

    def test_required_compose_binding_cannot_be_satisfied_by_an_unrelated_scope(
        self,
    ) -> None:
        prepared = plan(new_id("src"))
        with self.assertRaises(DeploymentEnvironmentIncomplete):
            _validate_environment_bindings(
                {("web", "DATABASE_URL")},
                prepared,
            )


class _Principal:
    credential_type = "deploy_token"

    def __init__(self) -> None:
        self.tenant_id = new_id("ten")
        self.credential_id = new_id("dtk")


class _Resolver:
    def __init__(self, prepared: PreparedDeploymentPlan) -> None:
        self.prepared = prepared
        self.calls = 0

    async def resolve(
        self, _artifact: StoredDeploymentPlanArtifact
    ) -> PreparedDeploymentPlan:
        self.calls += 1
        return self.prepared

    async def resolve_configuration(
        self, artifact: StoredDeploymentPlanArtifact
    ) -> DeploymentConfigurationSchema:
        return DeploymentConfigurationSchema(
            source_revision_id=artifact.source_revision_id,
            kind=self.prepared.kind,
            service_keys=tuple(
                service.service_key for service in self.prepared.services
            ),
            environment=self.prepared.environment,
            environment_schema_digest=self.prepared.environment_schema_digest,
        )


class _ServiceStore:
    def __init__(
        self,
        artifact: StoredDeploymentPlanArtifact,
        result: DeploymentAdmissionResult,
    ) -> None:
        self.artifact = artifact
        self.result = result
        self.replay: DeploymentAdmissionResult | None = None
        self.admit_calls = 0
        self.last_idempotency: IdempotencyInput | None = None

    async def lookup_replay(
        self,
        _scope: TenantScope,
        _principal: Principal,
        idempotency: IdempotencyInput,
    ) -> DeploymentAdmissionResult | None:
        self.last_idempotency = idempotency
        return self.replay

    async def get_plan_artifact(
        self, _scope: TenantScope, _application_id: str, _analysis_id: str
    ) -> StoredDeploymentPlanArtifact:
        return self.artifact

    async def admit(
        self, *_args: object, **_kwargs: object
    ) -> DeploymentAdmissionResult:
        self.admit_calls += 1
        return self.result


class _EnvironmentWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def patch_plan_environment(
        self,
        scope: TenantScope,
        principal: object,
        application_id: str,
        **kwargs: object,
    ) -> IdempotentCatalogResult:
        self.calls.append(
            {
                "scope": scope,
                "principal": principal,
                "applicationId": application_id,
                **kwargs,
            }
        )
        return IdempotentCatalogResult(
            {"environment": {"version": 1, "variables": []}}, replayed=False
        )


class DeploymentApiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_avoids_object_store_and_new_request_uses_trusted_resolver(
        self,
    ) -> None:
        principal = _Principal()
        application_id = new_id("app")
        analysis_id = new_id("ana")
        source_revision_id = new_id("src")
        artifact = StoredDeploymentPlanArtifact(
            artifact_id=new_id("art"),
            analysis_id=analysis_id,
            source_revision_id=source_revision_id,
            digest=SHA,
            media_type="application/vnd.lae.deployment-plan+json",
            size_bytes=128,
            storage_key="private/object-key",
        )
        deployment = PublicDeploymentRecord(
            id=new_id("dep"),
            application_id=application_id,
            revision_id=new_id("rev"),
            operation_id=new_id("op"),
            status="queued",
            previous_deployment_id=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            created_at=datetime.now(timezone.utc),
        )
        result = DeploymentAdmissionResult(
            deployment=deployment,
            operation_id=deployment.operation_id,
            operation_status="queued",
            operation_phase="deploy.prepare",
            operation_cursor=1,
            replayed=False,
        )
        store = _ServiceStore(artifact, result)
        resolver = _Resolver(plan(source_revision_id))
        service = DeploymentApiService(
            store,  # type: ignore[arg-type]
            resolver,
            idempotency_hash_key=b"d" * 32,
        )
        payload = DeploymentCreateRequest(analysisId=analysis_id, environmentVersion=0)
        created = await service.create(
            TenantScope(principal.tenant_id),
            principal,
            application_id,
            payload,
            "deploy-request-1",
        )
        self.assertIs(created, result)
        self.assertEqual(resolver.calls, 1)
        self.assertEqual(store.admit_calls, 1)

        configuration = await service.configuration(
            TenantScope(principal.tenant_id), application_id, analysis_id
        )
        self.assertEqual(
            configuration["configuration"]["serviceKeys"],
            ["web", "admin", "database"],
        )
        self.assertEqual(
            configuration["configuration"]["environment"][0],
            {
                "name": "DATABASE_URL",
                "serviceKeys": ["web", "admin"],
                "references": ["web:DATABASE_URL", "admin:DATABASE_URL"],
                "required": True,
                "sensitive": True,
            },
        )
        self.assertEqual(
            configuration["configuration"]["environmentScopeMode"], "service"
        )
        assert store.last_idempotency is not None
        self.assertEqual(
            store.last_idempotency.route_template,
            "/v1/applications/{application_id}/deployments",
        )

        replay = dataclasses.replace(result, replayed=True)
        store.replay = replay
        replayed = await service.create(
            TenantScope(principal.tenant_id),
            principal,
            application_id,
            payload,
            "deploy-request-1",
        )
        self.assertIs(replayed, replay)
        self.assertEqual(resolver.calls, 1)
        self.assertEqual(store.admit_calls, 1)

    async def test_plan_environment_patch_is_bound_to_verified_schema(self) -> None:
        principal = _Principal()
        application_id = new_id("app")
        analysis_id = new_id("ana")
        source_revision_id = new_id("src")
        artifact = StoredDeploymentPlanArtifact(
            artifact_id=new_id("art"),
            analysis_id=analysis_id,
            source_revision_id=source_revision_id,
            digest=SHA,
            media_type="application/vnd.lae.deployment-plan+json",
            size_bytes=128,
            storage_key="private/object-key",
        )
        deployment = PublicDeploymentRecord(
            id=new_id("dep"),
            application_id=application_id,
            revision_id=new_id("rev"),
            operation_id=new_id("op"),
            status="queued",
            previous_deployment_id=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            created_at=datetime.now(timezone.utc),
        )
        store = _ServiceStore(
            artifact,
            DeploymentAdmissionResult(
                deployment=deployment,
                operation_id=deployment.operation_id,
                operation_status="queued",
                operation_phase="deploy.prepare",
                operation_cursor=1,
                replayed=False,
            ),
        )
        prepared = plan(source_revision_id)
        writer = _EnvironmentWriter()
        service = DeploymentApiService(
            store,  # type: ignore[arg-type]
            _Resolver(prepared),
            idempotency_hash_key=b"d" * 32,
            environment_writer=writer,
        )
        payload = PlanEnvironmentPatchRequest(
            expectedVersion=0,
            environmentSchemaDigest=prepared.environment_schema_digest,
            set={
                "web:DATABASE_URL": {"value": "postgres://secret"},
                "admin:DATABASE_URL": {"value": "postgres://secret"},
            },
        )
        result = await service.patch_environment(
            TenantScope(principal.tenant_id),
            principal,
            application_id,
            analysis_id,
            payload,
            "environment-1",
        )
        self.assertEqual(result.response_body["environment"]["version"], 1)
        self.assertEqual(len(writer.calls), 1)
        call = writer.calls[0]
        self.assertEqual(call["analysis_id"], analysis_id)
        self.assertEqual(call["plan_service_keys"], ("web", "admin", "database"))
        self.assertEqual(
            call["values"],
            {
                "web:DATABASE_URL": "postgres://secret",
                "admin:DATABASE_URL": "postgres://secret",
            },
        )

        with self.assertRaises(DeploymentEnvironmentSchemaConflict):
            await service.patch_environment(
                TenantScope(principal.tenant_id),
                principal,
                application_id,
                analysis_id,
                PlanEnvironmentPatchRequest(
                    expectedVersion=0,
                    environmentSchemaDigest="sha256:" + "f" * 64,
                    set={"web:DATABASE_URL": {"value": "changed"}},
                ),
                "environment-2",
            )
        self.assertEqual(len(writer.calls), 1)


if __name__ == "__main__":
    unittest.main()
