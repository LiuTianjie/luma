from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lae_worker import (
    ArtifactDescriptor,
    ArtifactTransferBinding,
    ArtifactTransferUnavailable,
    HttpArtifactTransferBroker,
    LumaArtifactBrokerConfig,
    PostgresArtifactIngestGuard,
    S3AnalysisArtifactObjectStore,
    build_worker_from_env,
)


class _BrokerHandler(BaseHTTPRequestHandler):
    body = b'{"schemaVersion":"lae.deployment-plan/v1"}'
    binding: dict[str, object] = {}
    issue_token = "download-token-abcdefghijklmnopqrstuvwxyz012345"
    download_calls = 0
    redirect_issue = False

    def log_message(self, _format: str, *_args: object) -> None:
        return None

    def do_POST(self) -> None:
        if self.redirect_issue:
            self.send_response(307)
            self.send_header("Location", "http://redirect.invalid/lease")
            self.end_headers()
            return
        size = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(size))
        self.__class__.binding = {
            key: request[key]
            for key in (
                "tenantRef",
                "applicationRef",
                "externalOperationId",
                "builderTaskId",
                "artifact",
            )
        }
        response = {
            "schemaVersion": "luma.artifact-download-lease/v1",
            "leaseId": "artdl_" + "a" * 40,
            "expiresAt": (
                datetime.now(timezone.utc) + timedelta(minutes=1)
            ).isoformat().replace("+00:00", "Z"),
            "downloadToken": self.issue_token,
            "binding": self.binding,
        }
        encoded = json.dumps(response, separators=(",", ":")).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        self.__class__.download_calls += 1
        if self.headers.get("Authorization") != f"Bearer {self.issue_token}":
            self.send_response(401)
            self.end_headers()
            return
        descriptor = self.binding["artifact"]
        self.send_response(200)
        self.send_header("Content-Type", descriptor["mediaType"])
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("X-Luma-Artifact-Digest", descriptor["digest"])
        self.send_header("X-Luma-Artifact-Lease-Id", "artdl_" + "a" * 40)
        self.end_headers()
        self.wfile.write(self.body)


class ArtifactRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _BrokerHandler.binding = {}
        _BrokerHandler.download_calls = 0
        _BrokerHandler.redirect_issue = False
        digest = "sha256:" + hashlib.sha256(_BrokerHandler.body).hexdigest()
        self.binding = ArtifactTransferBinding(
            tenant_ref="tenant-runtime-test",
            application_ref="application-runtime-test",
            operation_id="operation-runtime-test",
            builder_task_id="builder-runtime-test",
            descriptor=ArtifactDescriptor(
                name="deploymentPlan",
                digest=digest,
                media_type="application/vnd.lae.deployment-plan+json",
                size_bytes=len(_BrokerHandler.body),
            ),
        )
        self.secret = "service-secret-canary-value"
        self.config = LumaArtifactBrokerConfig(
            endpoint=f"http://127.0.0.1:{self.server.server_port}",
            principal_id="lae-worker",
            token=self.secret,
            production=False,
            timeout_seconds=2,
        )
        self.broker = HttpArtifactTransferBroker(self.config)

    async def test_http_broker_binds_and_consumes_download_once(self) -> None:
        lease = await self.broker.issue_download_lease(
            self.binding, ttl_seconds=60
        )
        self.assertEqual(lease.binding, self.binding)
        download = await self.broker.open_download(lease, self.binding)
        body = b"".join([chunk async for chunk in download.chunks])
        self.assertEqual(body, _BrokerHandler.body)
        with self.assertRaises(ArtifactTransferUnavailable):
            await self.broker.open_download(lease, self.binding)
        self.assertEqual(_BrokerHandler.download_calls, 1)

    async def test_redirect_and_cross_binding_fail_without_secret_disclosure(self) -> None:
        _BrokerHandler.redirect_issue = True
        with self.assertRaises(ArtifactTransferUnavailable) as caught:
            await self.broker.issue_download_lease(self.binding, ttl_seconds=60)
        rendered = repr(self.config) + repr(self.broker) + str(caught.exception)
        self.assertNotIn(self.secret, rendered)

        _BrokerHandler.redirect_issue = False
        lease = await self.broker.issue_download_lease(
            self.binding, ttl_seconds=60
        )
        foreign = ArtifactTransferBinding(
            tenant_ref="tenant-other",
            application_ref=self.binding.application_ref,
            operation_id=self.binding.operation_id,
            builder_task_id=self.binding.builder_task_id,
            descriptor=self.binding.descriptor,
        )
        with self.assertRaises(ArtifactTransferUnavailable):
            await self.broker.open_download(lease, foreign)
        self.assertEqual(_BrokerHandler.download_calls, 0)

    async def test_production_worker_factory_wires_real_verified_recorder(self) -> None:
        runtime = build_worker_from_env(
            environ={
                "LAE_ENVIRONMENT": "production",
                "LAE_ARTIFACT_DRIVER": "s3",
                "LAE_ARTIFACT_S3_ENDPOINT": "https://artifact-store.internal:9000",
                "LAE_ARTIFACT_S3_ALLOWED_HOSTS": "artifact-store.internal",
                "LAE_ARTIFACT_S3_BUCKET": "lae-artifacts",
                "LAE_ARTIFACT_S3_REGION": "us-east-1",
                "LAE_ARTIFACT_S3_ACCESS_KEY": "artifact-access",
                "LAE_ARTIFACT_S3_SECRET_KEY": "artifact-secret-at-least-16",
                "LAE_ARTIFACT_S3_PATH_STYLE": "1",
                "LAE_DATABASE_URL": "postgresql+asyncpg://lae:password@127.0.0.1:5432/unused",
                "LAE_LUMA_CONTROL_URL": "https://luma.example.test",
                "LAE_LUMA_CLUSTER_ID": "luma-primary",
                "LAE_LUMA_SERVICE_PRINCIPAL_ID": "lae-worker",
                "LAE_LUMA_SERVICE_TOKEN": "service-principal-secret",
                "LAE_WORKER_STATE_HMAC_KEY": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
                "LAE_ANALYZER_IMAGE_DIGEST": "registry.internal/lae-agent@sha256:"
                + "a" * 64,
            }
        )
        try:
            recorder = runtime.worker._runner._recorder
            self.assertTrue(recorder.stores_verified_artifacts)
            self.assertIsInstance(recorder._guard, PostgresArtifactIngestGuard)
            self.assertIsInstance(
                recorder._object_store, S3AnalysisArtifactObjectStore
            )
            self.assertIsInstance(recorder._broker, HttpArtifactTransferBroker)
        finally:
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
