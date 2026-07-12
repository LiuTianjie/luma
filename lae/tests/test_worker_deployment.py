from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_luma_adapter import (  # noqa: E402
    BuilderLimits,
    FakeLuma,
    FakeLumaRuntime,
    RuntimeServicePrincipal,
    ServicePrincipal,
)
from lae_store import (  # noqa: E402
    EventInput,
    LeaseLost,
    OperationRecord,
    OperationStatus,
    new_id,
)
from lae_worker import (  # noqa: E402
    DeploymentContext,
    DeploymentEnvironmentRequirement,
    DeploymentRoute,
    DeploymentService,
    DeploymentStepRunner,
    DeploymentStepStatus,
    DeploymentVolume,
    DeploymentWorkerConfig,
    FakeRuntimeSecretProvider,
    InMemoryDeploymentStateStore,
    RuntimeManifestRenderer,
)
from lae_worker.deployment_postgres import (  # noqa: E402
    DeploymentContextInvalid,
    PostgresDeploymentStateStore,
    TrustedRuntimeService,
    _environment_requirements,
)


NOW = datetime.now(timezone.utc)


class FakeOperations:
    def __init__(self, operation: OperationRecord) -> None:
        self.operation = operation
        self.events: list[EventInput] = []
        self.heartbeat_count = 0

    async def heartbeat(
        self, scope, operation_id, *, worker_id, lease_seconds=60
    ) -> OperationRecord:
        self._owned(scope, operation_id, worker_id)
        self.heartbeat_count += 1
        self.operation = replace(
            self.operation,
            lease_expires_at=NOW + timedelta(seconds=lease_seconds + 300),
        )
        return self.operation

    async def append_event(self, scope, operation_id, event, *, worker_id) -> int:
        self._owned(scope, operation_id, worker_id)
        self.events.append(event)
        self.operation = replace(
            self.operation,
            phase=event.phase or self.operation.phase,
            last_event_seq=self.operation.last_event_seq + 1,
        )
        return self.operation.last_event_seq

    async def complete(
        self,
        scope,
        operation_id,
        *,
        worker_id,
        status,
        result=None,
        error_code=None,
        error_message=None,
    ) -> OperationRecord:
        self._owned(scope, operation_id, worker_id)
        effective = (
            OperationStatus.CANCELED if self.operation.cancel_requested else status
        )
        self.operation = replace(
            self.operation,
            status=effective.value,
            result=result if effective is OperationStatus.SUCCEEDED else None,
            error_code=error_code if effective is OperationStatus.FAILED else None,
            error_message=error_message
            if effective is OperationStatus.FAILED
            else None,
            lease_owner=None,
            lease_expires_at=None,
        )
        return self.operation

    def cancel(self) -> None:
        self.operation = replace(self.operation, cancel_requested_at=NOW)

    def _owned(self, scope, operation_id, worker_id) -> None:
        if (
            scope.tenant_id != self.operation.tenant_id
            or operation_id != self.operation.id
            or self.operation.lease_owner != worker_id
            or self.operation.status != "running"
        ):
            raise LeaseLost("fake deployment lease lost")


class StaticContextLoader:
    def __init__(self, context: DeploymentContext) -> None:
        self.context = context

    async def load(self, operation: OperationRecord) -> DeploymentContext:
        del operation
        return self.context


def build_result(keys: tuple[str, ...], snapshot_digest: str) -> dict[str, object]:
    images = {
        key: f"registry.internal/apps/{key}@sha256:" + str(index + 1) * 64
        for index, key in enumerate(keys)
    }
    image_digests = {key: image.rsplit("@", 1)[1] for key, image in images.items()}
    sbom = {key: "sha256:" + "a" * 64 for key in keys}
    provenance = {key: "sha256:" + "b" * 64 for key in keys}
    scan = {key: "sha256:" + "c" * 64 for key in keys}
    artifacts: dict[str, object] = {}
    media = {
        "sbom": "application/vnd.cyclonedx+json",
        "provenance": "application/vnd.in-toto+json",
        "scan": "application/vnd.lae.scan-report+json",
    }
    maps = {"sbom": sbom, "provenance": provenance, "scan": scan}
    for key in keys:
        for kind in ("sbom", "provenance", "scan"):
            artifacts[f"{key}-{kind}"] = {
                "digest": maps[kind][key],
                "mediaType": media[kind],
                "sizeBytes": 128,
            }
    return {
        "sourceSnapshotDigest": snapshot_digest,
        "images": images,
        "imageDigests": image_digests,
        "sbomDigests": sbom,
        "provenanceDigests": provenance,
        "scanDigests": scan,
        "artifacts": artifacts,
    }


class AutoBuilder:
    def __init__(self, delegate, backend: FakeLuma, result, *, failure=False) -> None:
        self.delegate = delegate
        self.backend = backend
        self.result = result
        self.failure = failure
        self.create_count = 0
        self.task_ids: list[str] = []

    def create_build_task(self, *args, **kwargs):
        self.create_count += 1
        mutation = self.delegate.create_build_task(*args, **kwargs)
        self.task_ids.append(mutation.task.task_id)
        self.backend.start_task(mutation.task.task_id)
        self.backend.complete_task(
            mutation.task.task_id,
            status="failed" if self.failure else "succeeded",
            result=None if self.failure else self.result,
        )
        return mutation

    def get_builder_task_events(self, *args, **kwargs):
        return self.delegate.get_builder_task_events(*args, **kwargs)

    def get_builder_task(self, *args, **kwargs):
        return self.delegate.get_builder_task(*args, **kwargs)

    def cancel_builder_task(self, *args, **kwargs):
        return self.delegate.cancel_builder_task(*args, **kwargs)


class AutoRuntime:
    def __init__(self, delegate, backend: FakeLumaRuntime, *, route_fail=False) -> None:
        self.delegate = delegate
        self.backend = backend
        self.route_fail = route_fail

    def prepare_volumes(self, *args, **kwargs):
        return self.delegate.prepare_volumes(*args, **kwargs)

    def deploy_revision(self, *args, **kwargs):
        mutation = self.delegate.deploy_revision(*args, **kwargs)
        manifest = args[1]
        self.backend.set_health(
            mutation.deployment.deployment_ref,
            services={service.key: "healthy" for service in manifest.services},
            routes={
                route.hostname: "failed" if self.route_fail else "ready"
                for route in manifest.routes
            },
        )
        return mutation

    def get_runtime_deployment(self, *args, **kwargs):
        return self.delegate.get_runtime_deployment(*args, **kwargs)

    def cancel_runtime_deployment(self, *args, **kwargs):
        return self.delegate.cancel_runtime_deployment(*args, **kwargs)


class FailBuildTaskCheckpointOnce:
    def __init__(self, delegate: InMemoryDeploymentStateStore) -> None:
        self.delegate = delegate
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def save(self, state, *, expected_version):
        if state.builder_task_id is not None and not self.failed:
            self.failed = True
            raise RuntimeError("simulated worker crash")
        return await self.delegate.save(state, expected_version=expected_version)


class StopAfterCheckpointFlush(RuntimeError):
    pass


class _AsyncContext:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


class RecordingInitializeSession:
    def __init__(self) -> None:
        self.deployment = SimpleNamespace(
            status="queued", started_at=None, updated_at=None
        )
        self.added: list[object] = []
        self.flush_batches: list[tuple[str, ...]] = []
        self.scalar_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def begin(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def scalar(self, statement):
        del statement
        self.scalar_count += 1
        return self.deployment if self.scalar_count == 1 else NOW

    async def get(self, model, key, **kwargs):
        del model, key, kwargs
        return None

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_batches.append(tuple(type(value).__name__ for value in self.added))
        self.added.clear()
        if len(self.flush_batches) == 2:
            raise StopAfterCheckpointFlush()


class RecordingDeploymentStateStore(PostgresDeploymentStateStore):
    @staticmethod
    async def _require_owned_operation(session, operation, *, for_update):
        del session, operation, for_update
        return SimpleNamespace(phase=None)


class DeploymentWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tenant_id = new_id("ten")
        self.application_id = new_id("app")
        self.operation_id = new_id("op")
        self.deployment_id = new_id("dep")
        self.revision_id = new_id("rev")
        self.worker_id = "deployment-worker-test"
        self.operation = OperationRecord(
            id=self.operation_id,
            tenant_id=self.tenant_id,
            kind="deployment.create",
            target_type="application",
            target_id=self.application_id,
            status="running",
            phase="deploy.prepare",
            result=None,
            error_code=None,
            error_message=None,
            cancel_requested_at=None,
            lease_owner=self.worker_id,
            lease_expires_at=NOW + timedelta(minutes=10),
            lease_attempt=1,
            last_event_seq=0,
        )
        self.operations = FakeOperations(self.operation)
        self.builder_backend = FakeLuma(clock=lambda: 1_720_000_000)
        self.builder_delegate = self.builder_backend.bind(
            ServicePrincipal("lae-builder", "builder-token")
        )
        self.runtime_backend = FakeLumaRuntime()
        self.runtime_delegate = self.runtime_backend.bind(
            RuntimeServicePrincipal("lae-runtime", "runtime-token-value")
        )
        self.config = DeploymentWorkerConfig(
            build_limits=BuilderLimits(2, 2048, 4096, 300),
            lease_seconds=60,
            timeout_seconds=300,
            poll_interval_seconds=0,
        )

    def context(self, *, compose: bool) -> DeploymentContext:
        services = (
            DeploymentService(
                "web",
                "http",
                "web",
                None,
                ("postgres",) if compose else (),
                "0.50",
                512,
                ("DATABASE_URL",) if compose else (),
                port=8080,
                health_path="/healthz",
                health_interval_seconds=10,
            ),
            *(
                (
                    DeploymentService(
                        "admin", "http", "admin", None, (), "0.25", 256, (), port=9090
                    ),
                    DeploymentService(
                        "postgres", "datastore", "postgres", None, (), "0.50", 1024, ()
                    ),
                )
                if compose
                else ()
            ),
        )
        routes = (
            DeploymentRoute("web", "a" * 32 + ".itool.tech", 8080, "/healthz"),
            *(
                (DeploymentRoute("admin", "b" * 32 + ".itool.tech", 9090),)
                if compose
                else ()
            ),
        )
        digest = "sha256:" + "d" * 64
        return DeploymentContext(
            tenant_ref=self.tenant_id,
            application_ref=self.application_id,
            operation_ref=self.operation_id,
            deployment_ref=self.deployment_id,
            revision_ref=self.revision_id,
            source_revision_ref=new_id("src"),
            analysis_ref=new_id("ana"),
            luma_name="lae-" + self.application_id.lower(),
            kind="compose" if compose else "service",
            region="cn",
            environment_version=2,
            source_snapshot_id="snapshot-deployment-test",
            source_snapshot_digest=digest,
            build_plan_digest="sha256:" + "e" * 64,
            signed_build_plan={"schemaVersion": "lae.build-plan/v1"},
            build_credential_lease_id="cl_buildlease123",
            services=services,
            routes=routes,
            volumes=(
                DeploymentVolume(
                    "pg-data",
                    1024,
                    ("postgres",),
                    "/var/lib/postgresql/data",
                    "ReadWriteOnce",
                ),
            )
            if compose
            else (),
            environment=(DeploymentEnvironmentRequirement("web", "DATABASE_URL"),)
            if compose
            else (),
            normalized_compose_digest="sha256:" + "f" * 64 if compose else None,
        )

    def runner(
        self,
        context,
        *,
        states=None,
        build_failure=False,
        route_failure=False,
        runtime_failure=False,
        clock=None,
    ):
        keys = tuple(service.build_key for service in context.services)
        builder = AutoBuilder(
            self.builder_delegate,
            self.builder_backend,
            build_result(keys, context.source_snapshot_digest),
            failure=build_failure,
        )
        if runtime_failure:
            self.runtime_backend.fail_next_deploy = True
        runtime = AutoRuntime(
            self.runtime_delegate, self.runtime_backend, route_fail=route_failure
        )
        store = states or InMemoryDeploymentStateStore(self.operations)  # type: ignore[arg-type]
        runner = DeploymentStepRunner(
            operations=self.operations,  # type: ignore[arg-type]
            contexts=StaticContextLoader(context),
            states=store,
            builder=builder,  # type: ignore[arg-type]
            runtime=runtime,  # type: ignore[arg-type]
            secrets=FakeRuntimeSecretProvider(),
            renderer=RuntimeManifestRenderer(),
            config=self.config,
            worker_id=self.worker_id,
            clock=clock,
        )
        return runner, store, builder

    async def execute(self, runner):
        operation = self.operations.operation
        for _ in range(20):
            result = await runner.step(operation)
            operation = result.operation
            if result.status is DeploymentStepStatus.TERMINAL:
                return result
        self.fail("deployment did not reach a terminal state")

    async def test_checkpoint_flushes_quota_reservation_before_foreign_key(self) -> None:
        session = RecordingInitializeSession()
        store = RecordingDeploymentStateStore(lambda: session, luma_cluster_id="luma-test")

        with self.assertRaises(StopAfterCheckpointFlush):
            await store.initialize(
                self.operation,
                self.context(compose=False),
                timeout=timedelta(minutes=5),
            )

        self.assertEqual(
            session.flush_batches,
            [
                ("DeploymentQuotaReservation",),
                ("DeploymentCheckpoint",),
            ],
        )

    async def test_single_service_activates_only_after_service_and_route_health(
        self,
    ) -> None:
        context = self.context(compose=False)
        runner, store, _builder = self.runner(context)
        result = await self.execute(runner)
        self.assertEqual(result.operation.status, "succeeded")
        self.assertEqual(
            store.current[self.application_id],
            (self.revision_id, self.deployment_id),
        )

    async def test_compose_verifies_two_public_http_routes_and_keeps_datastore_internal(
        self,
    ) -> None:
        context = self.context(compose=True)
        runner, store, _builder = self.runner(context)
        result = await self.execute(runner)
        self.assertEqual(result.operation.status, "succeeded")
        self.assertEqual(len(context.routes), 2)
        self.assertFalse(
            any(route.service_key == "postgres" for route in context.routes)
        )
        self.assertEqual(store.reservations[self.operation_id], "consumed")
        self.assertEqual(len(store.volume_bindings[self.operation_id]), 1)

    async def test_build_deploy_and_route_failures_preserve_old_current(self) -> None:
        for mode in ("build", "deploy", "route"):
            with self.subTest(mode=mode):
                self.setUp()
                context = self.context(compose=True)
                runner, store, _builder = self.runner(
                    context,
                    build_failure=mode == "build",
                    runtime_failure=mode == "deploy",
                    route_failure=mode == "route",
                )
                old = (new_id("rev"), new_id("dep"))
                store.current[self.application_id] = old
                result = await self.execute(runner)
                self.assertEqual(result.operation.status, "failed")
                self.assertEqual(store.current[self.application_id], old)
                self.assertEqual(store.reservations[self.operation_id], "released")

    async def test_cancel_forwards_and_never_activates(self) -> None:
        context = self.context(compose=True)
        runner, store, _builder = self.runner(context)
        first = await runner.step(self.operations.operation)
        self.assertEqual(first.status, DeploymentStepStatus.WAITING)
        self.operations.cancel()
        result = await runner.step(self.operations.operation)
        self.assertEqual(result.operation.status, "canceled")
        self.assertNotIn(self.application_id, store.current)
        self.assertEqual(store.reservations[self.operation_id], "released")

    async def test_crash_before_task_checkpoint_replays_same_builder_task(self) -> None:
        context = self.context(compose=False)
        durable = InMemoryDeploymentStateStore(self.operations)  # type: ignore[arg-type]
        crashing = FailBuildTaskCheckpointOnce(durable)
        runner, _store, builder = self.runner(context, states=crashing)
        with self.assertRaisesRegex(RuntimeError, "simulated worker crash"):
            await runner.step(self.operations.operation)
        resumed, _store, _ = self.runner(context, states=durable)
        result = await self.execute(resumed)
        self.assertEqual(result.operation.status, "succeeded")
        self.assertEqual(builder.create_count, 1)
        self.assertEqual(len(set(builder.task_ids)), 1)

    async def test_timeout_releases_quota_without_starting_build(self) -> None:
        context = self.context(compose=False)
        runner, store, builder = self.runner(
            context, clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1)
        )
        result = await runner.step(self.operations.operation)
        self.assertEqual(result.operation.status, "failed")
        self.assertEqual(result.operation.error_code, "LAE_DEPLOYMENT_TIMED_OUT")
        self.assertEqual(builder.create_count, 0)
        self.assertEqual(store.reservations[self.operation_id], "released")


class DeploymentEnvironmentScopeTests(unittest.TestCase):
    @staticmethod
    def service(
        service_key: str, environment_names: tuple[str, ...]
    ) -> TrustedRuntimeService:
        return TrustedRuntimeService(
            service_key=service_key,
            role="worker",
            build_key=f"build-{service_key}",
            command=None,
            dependencies=(),
            cpu="0.25",
            memory_mib=256,
            environment_names=environment_names,
            port=None,
            health_path=None,
            health_interval_seconds=None,
        )

    def test_specific_values_never_cross_service_boundaries(self) -> None:
        services = (
            self.service("web", ("DATABASE_URL", "SHARED_TOKEN")),
            self.service("worker", ("QUEUE_URL", "SHARED_TOKEN")),
        )
        requirements = _environment_requirements(
            [
                SimpleNamespace(service_scope="web", name="DATABASE_URL"),
                SimpleNamespace(service_scope="worker", name="QUEUE_URL"),
                SimpleNamespace(service_scope="worker", name="CUSTOM_FLAG"),
            ],
            services,
        )
        self.assertEqual(
            {(item.service_key, item.name) for item in requirements},
            {
                ("web", "DATABASE_URL"),
                ("worker", "QUEUE_URL"),
                ("worker", "CUSTOM_FLAG"),
            },
        )
        with self.assertRaises(DeploymentContextInvalid):
            _environment_requirements(
                [SimpleNamespace(service_scope="worker", name="DATABASE_URL")],
                services,
            )

    def test_legacy_wildcard_requires_single_or_all_service_plan_binding(self) -> None:
        services = (
            self.service("web", ("DATABASE_URL", "SHARED_TOKEN")),
            self.service("worker", ("SHARED_TOKEN",)),
        )
        shared = _environment_requirements(
            [SimpleNamespace(service_scope="*", name="SHARED_TOKEN")],
            services,
        )
        self.assertEqual(
            {(item.service_key, item.name) for item in shared},
            {("web", "SHARED_TOKEN"), ("worker", "SHARED_TOKEN")},
        )
        for name in ("DATABASE_URL", "CUSTOM_SECRET"):
            with self.subTest(name=name):
                with self.assertRaises(DeploymentContextInvalid):
                    _environment_requirements(
                        [SimpleNamespace(service_scope="*", name=name)],
                        services,
                    )

        single = _environment_requirements(
            [SimpleNamespace(service_scope="*", name="LEGACY_SECRET")],
            (self.service("web", ()),),
        )
        self.assertEqual(
            {(item.service_key, item.name) for item in single},
            {("web", "LEGACY_SECRET")},
        )


if __name__ == "__main__":
    unittest.main()
