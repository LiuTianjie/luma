from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/python/lae-core/src",
    "packages/python/lae-luma-adapter/src",
    "packages/python/lae-store/src",
    "services/worker/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_luma_adapter import HttpLumaRuntimeAdapter  # noqa: E402
from lae_worker import (  # noqa: E402
    HttpEphemeralRuntimeSecretIssuer,
    PostgresLifecycleContextLoader,
    PostgresLifecycleStateStore,
    PostgresUpdateCheckResolver,
    S3TrustedBuildPlanMaterializer,
    UnifiedWorker,
    WorkerLaneFailure,
    build_worker_from_env,
)


class Lane:
    def __init__(self, own: asyncio.Event, peer: asyncio.Event, operation: str) -> None:
        self.own = own
        self.peer = peer
        self.operation = operation
        self._runner = SimpleNamespace()

    async def run_once(self):
        self.own.set()
        await asyncio.wait_for(self.peer.wait(), timeout=0.2)
        return SimpleNamespace(
            operation=SimpleNamespace(id=self.operation, status="succeeded")
        )


class Scanner:
    async def run_once(self) -> bool:
        await asyncio.sleep(0)
        return True

    async def cleanup_once(self) -> bool:
        return True


class FailedLane:
    _runner = SimpleNamespace()

    async def run_once(self):
        raise RuntimeError("secret-bearing-upstream-error")


class WorkerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_unified_run_once_services_all_lanes_concurrently(self) -> None:
        analyze_started = asyncio.Event()
        deploy_started = asyncio.Event()
        analyze = Lane(analyze_started, deploy_started, "op_analyze")
        deploy = Lane(deploy_started, analyze_started, "op_deploy")
        worker = UnifiedWorker(
            analyze,  # type: ignore[arg-type]
            deployment_worker=deploy,  # type: ignore[arg-type]
            upload_scanner=Scanner(),  # type: ignore[arg-type]
        )
        result = await worker.run_once()
        self.assertEqual(
            [item.id for item in result.operation_results],
            ["op_analyze", "op_deploy"],
        )
        self.assertTrue(result.upload_scanned)
        self.assertTrue(result.upload_cleaned)

    async def test_lane_exception_is_generic_and_not_reported_as_idle(self) -> None:
        worker = UnifiedWorker(FailedLane())  # type: ignore[arg-type]
        with self.assertRaises(WorkerLaneFailure) as caught:
            await worker.run_once()
        self.assertNotIn("secret-bearing", str(caught.exception))

    async def test_enabled_deployment_factory_wires_real_secure_adapters(self) -> None:
        key = base64.b64encode(b"k" * 32).decode()
        runtime = build_worker_from_env(
            environ={
                "LAE_ENVIRONMENT": "production",
                "LAE_DATABASE_URL": "postgresql+asyncpg://lae:password@127.0.0.1:5432/unused",
                "LAE_LUMA_CONTROL_URL": "https://luma.example.test",
                "LAE_LUMA_CLUSTER_ID": "luma-primary",
                "LAE_LUMA_SERVICE_PRINCIPAL_ID": "lae-builder-worker",
                "LAE_LUMA_SERVICE_TOKEN": "builder-service-token",
                "LAE_WORKER_STATE_HMAC_KEY": key,
                "LAE_ANALYZER_IMAGE_DIGEST": "registry.internal/lae-agent@sha256:"
                + "a" * 64,
                "LAE_ARTIFACT_DRIVER": "s3",
                "LAE_ARTIFACT_S3_ENDPOINT": "https://objects.internal:9000",
                "LAE_ARTIFACT_S3_ALLOWED_HOSTS": "objects.internal",
                "LAE_ARTIFACT_S3_BUCKET": "lae-artifacts",
                "LAE_ARTIFACT_S3_REGION": "us-east-1",
                "LAE_ARTIFACT_S3_ACCESS_KEY": "artifact-access",
                "LAE_ARTIFACT_S3_SECRET_KEY": "artifact-secret-at-least-16",
                "LAE_DEPLOYMENT_WORKER_ENABLED": "1",
                "LAE_LUMA_RUNTIME_PRINCIPAL_ID": "lae-runtime-worker",
                "LAE_LUMA_RUNTIME_SERVICE_TOKEN": "runtime-service-token",
                "LAE_BUILD_PLAN_SIGNING_KEY_ID": "lae-plan-primary",
                "LAE_BUILD_PLAN_SIGNING_HMAC_KEY": key,
                "LAE_BUILD_CREDENTIAL_LEASE_HMAC_KEY": key,
                "LAE_ENVIRONMENT_AEAD_KEY_VERSION": "1",
                "LAE_ENVIRONMENT_AEAD_KEYS": json.dumps({"1": key}),
                "LAE_ENVIRONMENT_CHECKSUM_HMAC_KEY": key,
            }
        )
        try:
            self.assertIsInstance(
                runtime.worker._runner._update_checks,
                PostgresUpdateCheckResolver,
            )
            deploy = runtime.worker.deployment_worker
            self.assertIsNotNone(deploy)
            runner = deploy._runner
            self.assertIsInstance(runner._runtime, HttpLumaRuntimeAdapter)
            self.assertIsInstance(
                runner._secrets._issuer, HttpEphemeralRuntimeSecretIssuer
            )
            self.assertIsInstance(
                runner._contexts._materializer, S3TrustedBuildPlanMaterializer
            )
            lifecycle = runtime.worker.lifecycle_worker
            self.assertIsNotNone(lifecycle)
            lifecycle_runner = lifecycle._runner
            self.assertIs(lifecycle_runner._runtime, runner._runtime)
            self.assertIsInstance(
                lifecycle_runner._contexts, PostgresLifecycleContextLoader
            )
            self.assertIsInstance(
                lifecycle_runner._states, PostgresLifecycleStateStore
            )
        finally:
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
