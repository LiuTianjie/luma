from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_luma_adapter import (  # noqa: E402
    FakeLumaRuntime,
    RuntimeCallContext,
    RuntimeImageBinding,
    RuntimeManifest,
    RuntimeServicePrincipal,
    RuntimeServiceResources,
    RuntimeServiceSpec,
)
from lae_store import OperationRecord, new_id  # noqa: E402
from lae_worker import (  # noqa: E402
    LifecycleContext,
    LifecycleContextInvalid,
    LifecycleDeploymentBinding,
    LifecycleStepRunner,
    LifecycleStepStatus,
    LifecycleWorkerConfig,
)


NOW = datetime.now(timezone.utc)
SHA = "sha256:" + "a" * 64


def runtime_manifest(name: str, digest: str) -> RuntimeManifest:
    return RuntimeManifest(
        name=name,
        kind="service",
        region="cn",
        services=(
            RuntimeServiceSpec(
                "web",
                "http",
                RuntimeImageBinding("builder-task", "web", SHA),
                None,
                (),
                RuntimeServiceResources("0.50", 512),
                (),
                port=8080,
            ),
        ),
        routes=(),
        volumes=(),
        secrets=(),
        manifest_digest=digest,
    )


class Operations:
    def __init__(self, operation: OperationRecord) -> None:
        self.operation = operation

    async def heartbeat(self, scope, operation_id, *, worker_id, lease_seconds):
        if (
            scope.tenant_id != self.operation.tenant_id
            or operation_id != self.operation.id
            or worker_id != self.operation.lease_owner
        ):
            raise AssertionError("operation binding changed")
        self.operation = replace(
            self.operation,
            lease_expires_at=NOW + timedelta(seconds=lease_seconds + 60),
        )
        return self.operation


class Contexts:
    def __init__(self, context: LifecycleContext) -> None:
        self.context = context

    async def load(self, operation):
        del operation
        return self.context


class BrokenContexts:
    async def load(self, operation):
        del operation
        raise LifecycleContextInvalid()


class States:
    def __init__(self, operations: Operations) -> None:
        self.operations = operations
        self.succeeded = False
        self.failed = False
        self.canceled = False
        self.restored = False

    async def mark_runtime_started(self, operation, context, *, worker_id):
        del context, worker_id
        self.operations.operation = replace(
            operation, phase="application.lifecycle.runtime"
        )
        return self.operations.operation

    async def cancel_before_runtime(self, operation, context, *, worker_id):
        del context, worker_id
        self.canceled = True
        self.restored = True
        self.operations.operation = replace(
            operation, status="canceled", lease_owner=None, lease_expires_at=None
        )
        return self.operations.operation

    async def succeed(self, operation, context, runtime, *, worker_id):
        del context, runtime, worker_id
        self.succeeded = True
        self.operations.operation = replace(
            operation, status="succeeded", lease_owner=None, lease_expires_at=None
        )
        return self.operations.operation

    async def fail(self, operation, context, error, *, worker_id):
        del context, error, worker_id
        self.failed = True
        self.restored = True
        self.operations.operation = replace(
            operation, status="failed", lease_owner=None, lease_expires_at=None
        )
        return self.operations.operation

    async def fail_unloaded(self, operation, error, *, worker_id):
        del error, worker_id
        self.failed = True
        self.restored = True
        self.operations.operation = replace(
            operation, status="failed", lease_owner=None, lease_expires_at=None
        )
        return self.operations.operation


class HealthyRuntime:
    def __init__(self, backend, adapter) -> None:
        self.backend = backend
        self.adapter = adapter
        self.delete_volume_policies = []

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def resume_runtime_deployment(self, *args, **kwargs):
        mutation = self.adapter.resume_runtime_deployment(*args, **kwargs)
        self.backend.set_health(mutation.deployment.deployment_ref)
        return mutation

    def restart_runtime_deployment(self, *args, **kwargs):
        mutation = self.adapter.restart_runtime_deployment(*args, **kwargs)
        self.backend.set_health(mutation.deployment.deployment_ref)
        return mutation

    def rollback_runtime_deployment(self, *args, **kwargs):
        mutation = self.adapter.rollback_runtime_deployment(*args, **kwargs)
        self.backend.set_health(mutation.deployment.deployment_ref)
        return mutation

    def delete_runtime_deployment(self, *args, **kwargs):
        self.delete_volume_policies.append(kwargs.get("volume_policy"))
        return self.adapter.delete_runtime_deployment(*args, **kwargs)


class LifecycleWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tenant = new_id("ten")
        self.application = new_id("app")
        self.worker_id = "lifecycle-worker-test"
        self.backend = FakeLumaRuntime()
        self.adapter = self.backend.bind(
            RuntimeServicePrincipal("lae-runtime", "runtime-test-token")
        )
        self.runtime = HealthyRuntime(self.backend, self.adapter)
        self.target = self._deploy(self.adapter, "target")
        self.source = self._deploy(self.adapter, "source")

    def _binding(self, label: str) -> tuple[LifecycleDeploymentBinding, RuntimeCallContext]:
        deployment = new_id("dep")
        revision = new_id("rev")
        operation = new_id("op")
        context = RuntimeCallContext(
            self.tenant, self.application, operation, revision, deployment
        )
        return (
            LifecycleDeploymentBinding(
                deployment,
                revision,
                operation,
                "placeholder",
                "sha256:" + ("b" if label == "target" else "c") * 64,
            ),
            context,
        )

    def _deploy(self, adapter, label: str) -> LifecycleDeploymentBinding:
        binding, context = self._binding(label)
        mutation = adapter.deploy_revision(
            context,
            runtime_manifest("lae-lifecycle", binding.manifest_digest),
            idempotency_key=f"deploy-{label}",
        )
        self.backend.set_health(mutation.deployment.deployment_ref)
        return replace(
            binding, runtime_deployment_ref=mutation.deployment.deployment_ref
        )

    def operation(self, action: str, *, cancel: bool = False) -> OperationRecord:
        return OperationRecord(
            id=new_id("op"),
            tenant_id=self.tenant,
            kind=f"application.{action}",
            target_type="application",
            target_id=self.application,
            status="running",
            phase="application.lifecycle",
            result=None,
            error_code=None,
            error_message=None,
            cancel_requested_at=NOW if cancel else None,
            lease_owner=self.worker_id,
            lease_expires_at=NOW + timedelta(minutes=5),
            lease_attempt=1,
            last_event_seq=0,
        )

    def context(self, action: str, *, old: bool = False) -> LifecycleContext:
        return LifecycleContext(
            tenant_id=self.tenant,
            application_id=self.application,
            action=action,
            previous_desired_state=("suspended" if action == "resume" else "running"),
            requested_desired_state={
                "suspend": "suspended",
                "resume": "running",
                "restart": None,
                "rollback": "running",
                "delete": "deleted",
            }[action],
            source=self.source,
            target=self.target if action == "rollback" else None,
            request_created_at=NOW - (timedelta(hours=1) if old else timedelta()),
        )

    async def run_action(self, action: str):
        operation = self.operation(action)
        operations = Operations(operation)
        states = States(operations)
        runner = LifecycleStepRunner(
            operations=operations,  # type: ignore[arg-type]
            contexts=Contexts(self.context(action)),
            states=states,
            runtime=self.runtime,  # type: ignore[arg-type]
            config=LifecycleWorkerConfig(poll_interval_seconds=0),
            worker_id=self.worker_id,
        )
        result = await runner.step(operation)
        return result, states

    async def test_all_runtime_actions_reach_success_and_rollback_changes_runtime(self) -> None:
        for action in ("suspend", "resume", "restart", "rollback", "delete"):
            with self.subTest(action=action):
                self.setUp()
                if action == "resume":
                    self.runtime.suspend_runtime_deployment(
                        self.source_context,
                        self.source.runtime_deployment_ref,
                        idempotency_key="prepare-suspend",
                    )
                result, states = await self.run_action(action)
                self.assertEqual(result.status, LifecycleStepStatus.TERMINAL)
                self.assertEqual(result.operation.status, "succeeded")
                self.assertTrue(states.succeeded)
                if action == "delete":
                    self.assertEqual(self.runtime.delete_volume_policies, ["retain"])

    @property
    def source_context(self) -> RuntimeCallContext:
        return RuntimeCallContext(
            self.tenant,
            self.application,
            self.source.deployment_operation_id,
            self.source.revision_id,
            self.source.deployment_id,
        )

    async def test_pre_runtime_cancel_restores_desired_without_calling_luma(self) -> None:
        operation = self.operation("suspend", cancel=True)
        operations = Operations(operation)
        states = States(operations)
        runner = LifecycleStepRunner(
            operations=operations,  # type: ignore[arg-type]
            contexts=Contexts(self.context("suspend")),
            states=states,
            runtime=self.runtime,  # type: ignore[arg-type]
            config=LifecycleWorkerConfig(),
            worker_id=self.worker_id,
        )
        result = await runner.step(operation)
        self.assertEqual(result.operation.status, "canceled")
        self.assertTrue(states.restored)

    async def test_timeout_fails_and_restores_desired_state(self) -> None:
        operation = self.operation("restart")
        operations = Operations(operation)
        states = States(operations)
        runner = LifecycleStepRunner(
            operations=operations,  # type: ignore[arg-type]
            contexts=Contexts(self.context("restart", old=True)),
            states=states,
            runtime=self.runtime,  # type: ignore[arg-type]
            config=LifecycleWorkerConfig(timeout_seconds=30),
            worker_id=self.worker_id,
            clock=lambda: NOW,
        )
        result = await runner.step(operation)
        self.assertEqual(result.operation.status, "failed")
        self.assertTrue(states.restored)

    async def test_waiting_poll_replays_the_same_runtime_mutation_then_observes_success(self) -> None:
        operation = self.operation("restart")
        operations = Operations(operation)
        states = States(operations)
        runner = LifecycleStepRunner(
            operations=operations,  # type: ignore[arg-type]
            contexts=Contexts(self.context("restart")),
            states=states,
            runtime=self.adapter,
            config=LifecycleWorkerConfig(poll_interval_seconds=0),
            worker_id=self.worker_id,
        )

        waiting = await runner.step(operation)
        self.assertEqual(waiting.status, LifecycleStepStatus.WAITING)
        self.assertFalse(states.succeeded)
        self.backend.set_health(self.source.runtime_deployment_ref)
        completed = await runner.step(waiting.operation)

        self.assertEqual(completed.status, LifecycleStepStatus.TERMINAL)
        self.assertEqual(completed.operation.status, "succeeded")

    async def test_invalid_context_fails_terminally_and_restores_admission_state(self) -> None:
        operation = self.operation("suspend")
        operations = Operations(operation)
        states = States(operations)
        runner = LifecycleStepRunner(
            operations=operations,  # type: ignore[arg-type]
            contexts=BrokenContexts(),
            states=states,
            runtime=self.runtime,  # type: ignore[arg-type]
            config=LifecycleWorkerConfig(),
            worker_id=self.worker_id,
        )

        result = await runner.step(operation)

        self.assertEqual(result.operation.status, "failed")
        self.assertTrue(states.restored)


if __name__ == "__main__":
    unittest.main()
