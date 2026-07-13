from __future__ import annotations

import copy
import io
import json
import logging
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-luma-adapter" / "src"))

from lae_luma_adapter import (  # noqa: E402
    AdapterErrorCode,
    AnalyzeSourceRequest,
    BuilderLimits,
    BuildPlanRequest,
    HttpLumaBuilderAdapter,
    LumaAdapterError,
    LumaCallContext,
    ObjectSourceReference,
    ServicePrincipal,
    SourceReference,
)


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class HttpLumaAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "principal-token-must-never-be-logged"
        self.principal = ServicePrincipal("lae-worker", self.token)
        self.adapter = HttpLumaBuilderAdapter(
            "https://luma.internal.example", self.principal
        )
        self.context = LumaCallContext(
            tenant_ref="tenant-a",
            application_ref="application-a",
            external_operation_id="operation-a",
            request_id="request-a",
        )
        self.request = AnalyzeSourceRequest(
            source=SourceReference("https://github.com/acme/example.git", ref="main"),
            credential_lease_id="credential-lease-must-never-be-logged",
            agent_image_digest="registry.internal/lae-agent@sha256:" + ("a" * 64),
            policy_version="2026-07-11",
            limits=BuilderLimits(
                cpu=2, memory_mib=2048, disk_mib=4096, timeout_seconds=300
            ),
        )

    def test_object_source_wire_contains_only_immutable_public_descriptor(self) -> None:
        digest = "sha256:" + "b" * 64
        request = AnalyzeSourceRequest(
            source=ObjectSourceReference(
                digest=digest,
                media_type="application/zip",
                size_bytes=2048,
            ),
            credential_lease_id="object-lease-never-persisted-in-source-ref",
            agent_image_digest="registry.internal/lae-agent@sha256:" + ("a" * 64),
            policy_version="2026-07-11",
            limits=BuilderLimits(
                cpu=2, memory_mib=2048, disk_mib=4096, timeout_seconds=300
            ),
        )
        self.assertEqual(
            request.to_wire()["sourceRef"],
            {
                "kind": "object",
                "digest": digest,
                "mediaType": "application/zip",
                "sizeBytes": 2048,
            },
        )
        self.assertNotIn("url", request.to_wire()["sourceRef"])
        self.assertNotIn("objectKey", request.to_wire()["sourceRef"])

    @staticmethod
    def build_request() -> BuildPlanRequest:
        digest = "sha256:" + ("c" * 64)
        return BuildPlanRequest(
            source_snapshot_id="snapshot-a",
            source_snapshot_digest=digest,
            signed_build_plan={
                "schemaVersion": "lae.build-plan/v1",
                "sourceSnapshotDigest": digest,
                "resolvedCommit": "1" * 40,
                "policyVersion": "2026-07-11",
                "builds": [],
                "externalImages": [],
                "signature": {
                    "keyId": "lae-plan-test",
                    "value": "ZmFrZS1zaWduYXR1cmU",
                },
            },
            credential_lease_id="credential-lease-build-must-never-be-logged",
            limits=BuilderLimits(
                cpu=4, memory_mib=4096, disk_mib=8192, timeout_seconds=900
            ),
        )

    @staticmethod
    def analyze_result() -> dict[str, object]:
        evidence_digest = "sha256:" + ("e" * 64)
        deployment_digest = "sha256:" + ("d" * 64)
        build_digest = "sha256:" + ("f" * 64)
        return {
            "resolvedCommit": "1" * 40,
            "sourceTreeDigest": "sha256:" + ("b" * 64),
            "sourceSnapshotId": "snapshot-a",
            "sourceSnapshotDigest": "sha256:" + ("c" * 64),
            "deploymentPlanDigest": deployment_digest,
            "buildPlanDigest": build_digest,
            "evidenceDigest": evidence_digest,
            "policyVersion": "2026-07-11",
            "agentImageDigest": "registry.internal/lae-agent@sha256:" + ("a" * 64),
            "verdict": "deployable",
            "diagnosticStatus": "succeeded",
            "diagnosticMode": "ai",
            "diagnosticCode": "AI_ANALYSIS_SUCCEEDED",
            "knowledgeVersion": "2026-07-14.1",
            "blockers": [],
            "artifacts": {
                "evidence": {
                    "digest": evidence_digest,
                    "mediaType": "application/vnd.lae.evidence+json",
                    "sizeBytes": 100,
                },
                "deploymentPlan": {
                    "digest": deployment_digest,
                    "mediaType": "application/vnd.lae.deployment-plan+json",
                    "sizeBytes": 200,
                },
                "buildPlan": {
                    "digest": build_digest,
                    "mediaType": "application/vnd.lae.build-plan-candidate+json",
                    "sizeBytes": 300,
                },
            },
        }

    @staticmethod
    def build_result() -> dict[str, object]:
        image_digest = "sha256:" + ("d" * 64)
        sbom_digest = "sha256:" + ("e" * 64)
        provenance_digest = "sha256:" + ("f" * 64)
        scan_digest = "sha256:" + ("a" * 64)
        return {
            "sourceSnapshotDigest": "sha256:" + ("c" * 64),
            "images": {"web": "registry.internal/web@" + image_digest},
            "imageDigests": {"web": image_digest},
            "sbomDigests": {"web": sbom_digest},
            "provenanceDigests": {"web": provenance_digest},
            "scanDigests": {"web": scan_digest},
            "artifacts": {
                "web-sbom": {
                    "digest": sbom_digest,
                    "mediaType": "application/vnd.cyclonedx+json",
                    "sizeBytes": 400,
                },
                "web-provenance": {
                    "digest": provenance_digest,
                    "mediaType": "application/vnd.in-toto+json",
                    "sizeBytes": 500,
                },
                "web-scan": {
                    "digest": scan_digest,
                    "mediaType": "application/vnd.lae.scan-report+json",
                    "sizeBytes": 600,
                },
            },
        }

    def task_wire(
        self, *, status: str = "queued", **overrides: object
    ) -> dict[str, object]:
        task: dict[str, object] = {
            "id": "builder-http-1",
            "schemaVersion": "luma.builder-task/v1",
            "kind": "analyze-source",
            "externalOperationId": self.context.external_operation_id,
            "tenantRef": self.context.tenant_ref,
            "applicationRef": self.context.application_ref,
            "status": status,
            "builderNode": "builder-private-node",
            "credentialLeaseId": "credential-lease-must-never-be-logged",
            "internalAddress": "http://10.0.0.7:4646",
            "message": "connect to http://10.0.0.7:4646",
            "createdAt": 100,
            "updatedAt": 100,
            "startedAt": 0,
            "completedAt": 0,
            "lastCursor": 1,
        }
        task.update(overrides)
        return task

    @staticmethod
    def response(value: object, *, status: int = 200) -> _Response:
        return _Response(
            json.dumps(value, separators=(",", ":")).encode("utf-8"), status=status
        )

    def test_create_uses_principal_and_context_headers_and_filters_internal_fields(
        self,
    ) -> None:
        captured: list[urllib.request.Request] = []

        def open_request(
            request: urllib.request.Request, **_kwargs: object
        ) -> _Response:
            captured.append(request)
            return self.response(
                {
                    "task": self.task_wire(
                        status="succeeded",
                        startedAt=101,
                        completedAt=102,
                        result=self.analyze_result(),
                    ),
                    "replayed": True,
                },
                status=202,
            )

        with patch("urllib.request.urlopen", side_effect=open_request):
            created = self.adapter.create_analyze_task(
                self.context,
                self.request,
                idempotency_key="operation-a:analyze",
            )

        sent = captured[0]
        sent_headers = {name.lower(): value for name, value in sent.header_items()}
        sent_body = json.loads(sent.data.decode("utf-8"))
        self.assertEqual(sent_headers["authorization"], f"Bearer {self.token}")
        self.assertEqual(sent_headers["idempotency-key"], "operation-a:analyze")
        self.assertEqual(sent_headers["x-lae-tenant-id"], self.context.tenant_ref)
        self.assertEqual(
            sent_headers["x-lae-application-id"], self.context.application_ref
        )
        self.assertEqual(
            sent_headers["x-lae-operation-id"], self.context.external_operation_id
        )
        self.assertEqual(sent_headers["x-request-id"], self.context.request_id)
        self.assertEqual(sent_body["tenantRef"], self.context.tenant_ref)
        self.assertEqual(sent_body["applicationRef"], self.context.application_ref)
        self.assertEqual(
            sent_body["payload"]["credentialLeaseId"], self.request.credential_lease_id
        )

        public_text = json.dumps(created.task.to_tenant_dict(), sort_keys=True)
        for forbidden in (
            "builder-private-node",
            "credential-lease-must-never-be-logged",
            "10.0.0.7",
            "registry.internal",
            "builderNode",
        ):
            self.assertNotIn(forbidden, public_text)
        self.assertEqual(created.task.result["sourceSnapshotId"], "snapshot-a")
        self.assertEqual(created.task.result["verdict"], "deployable")
        self.assertEqual(created.task.result["diagnosticMode"], "ai")

    def test_events_preflight_context_and_resume_from_cursor_without_upstream_messages(
        self,
    ) -> None:
        calls: list[str] = []
        event_payload = {
            "taskId": "builder-http-1",
            "status": "running",
            "events": [
                {
                    "cursor": 8,
                    "seq": 8,
                    "ts": 102,
                    "type": "source.fetch",
                    "message": "fetch from http://10.0.0.8:9418 with token secret",
                }
            ],
            "nextCursor": 8,
            "oldestCursor": 1,
            "hasMore": False,
            "terminal": False,
        }

        def open_request(
            request: urllib.request.Request, **_kwargs: object
        ) -> _Response:
            calls.append(request.full_url)
            if request.full_url.endswith("/builder-http-1"):
                return self.response(
                    {
                        "task": self.task_wire(
                            status="running", startedAt=101, lastCursor=8
                        )
                    }
                )
            return self.response(event_payload)

        with patch("urllib.request.urlopen", side_effect=open_request):
            page = self.adapter.get_builder_task_events(
                self.context,
                "builder-http-1",
                after=7,
                limit=25,
            )

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith("/builder-http-1/events?after=7&limit=25"))
        self.assertEqual(page.next_cursor, 8)
        self.assertEqual(page.events[0].message, "Source fetch updated.")
        self.assertNotIn("10.0.0.8", json.dumps(page.events[0].to_tenant_dict()))

    def test_build_plan_uses_the_same_typed_builder_endpoint(self) -> None:
        captured: list[urllib.request.Request] = []

        def open_request(
            request: urllib.request.Request, **_kwargs: object
        ) -> _Response:
            captured.append(request)
            return self.response(
                {
                    "task": self.task_wire(
                        kind="build-plan",
                        status="succeeded",
                        startedAt=101,
                        completedAt=102,
                        result=self.build_result(),
                    ),
                    "replayed": True,
                },
                status=202,
            )

        request = self.build_request()
        with patch("urllib.request.urlopen", side_effect=open_request):
            created = self.adapter.create_build_task(
                self.context,
                request,
                idempotency_key="operation-a:build",
            )

        sent_body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual(sent_body["kind"], "build-plan")
        self.assertEqual(sent_body["payload"]["sourceSnapshotId"], "snapshot-a")
        self.assertEqual(
            sent_body["payload"]["credentialLeaseId"],
            request.credential_lease_id,
        )
        public_text = json.dumps(created.task.to_tenant_dict(), sort_keys=True)
        self.assertNotIn("registry.internal", public_text)
        self.assertEqual(
            created.task.result["imageDigests"]["web"],
            "sha256:" + ("d" * 64),
        )

    def test_cancel_preflights_scope_and_encodes_task_path(self) -> None:
        calls: list[tuple[str, str]] = []

        def open_request(
            request: urllib.request.Request, **_kwargs: object
        ) -> _Response:
            calls.append((request.method, request.full_url))
            if request.method == "GET":
                return self.response({"task": self.task_wire(id="builder/http id")})
            return self.response(
                {
                    "task": self.task_wire(
                        id="builder/http id",
                        status="canceled",
                        completedAt=102,
                        lastCursor=2,
                    ),
                    "replayed": False,
                }
            )

        with patch("urllib.request.urlopen", side_effect=open_request):
            canceled = self.adapter.cancel_builder_task(self.context, "builder/http id")

        self.assertEqual(calls[0][0], "GET")
        self.assertIn("builder%2Fhttp%20id", calls[0][1])
        self.assertEqual(calls[1][0], "POST")
        self.assertTrue(calls[1][1].endswith("/builder%2Fhttp%20id/cancel"))
        self.assertEqual(canceled.task.status, "canceled")

    def test_http_errors_map_to_stable_codes_without_echoing_response_body(
        self,
    ) -> None:
        cases = {
            400: AdapterErrorCode.INVALID_REQUEST,
            401: AdapterErrorCode.UNAUTHORIZED,
            404: AdapterErrorCode.NOT_FOUND,
            409: AdapterErrorCode.IDEMPOTENCY_CONFLICT,
            410: AdapterErrorCode.CURSOR_EXPIRED,
            503: AdapterErrorCode.CAPACITY_UNAVAILABLE,
            500: AdapterErrorCode.UPSTREAM_UNAVAILABLE,
        }
        sentinel = "github_pat_secret_that_must_not_escape"
        for status, expected_code in cases.items():
            error_body = json.dumps(
                {
                    "requestId": "luma-request-a",
                    "errorInfo": {
                        "code": "service_unavailable",
                        "message": f"upstream said {sentinel} at http://10.0.0.9:4646",
                        "requestId": "luma-request-a",
                    },
                }
            ).encode("utf-8")
            error = urllib.error.HTTPError(
                "https://luma.internal.example/v1/builder/tasks",
                status,
                "failure",
                {},
                io.BytesIO(error_body),
            )
            with (
                self.subTest(status=status),
                patch("urllib.request.urlopen", side_effect=error),
            ):
                with self.assertRaises(LumaAdapterError) as caught:
                    self.adapter.create_analyze_task(
                        self.context,
                        self.request,
                        idempotency_key=f"operation-a:error-{status}",
                    )
            self.assertEqual(caught.exception.code, expected_code)
            self.assertEqual(caught.exception.request_id, "luma-request-a")
            self.assertNotIn(sentinel, str(caught.exception))
            self.assertNotIn("10.0.0.9", str(caught.exception))

    def test_malformed_or_cross_context_success_response_is_protocol_error(
        self,
    ) -> None:
        malformed_responses = (
            b"not-json",
            b"[]",
            json.dumps({"task": {"status": "queued"}, "replayed": False}).encode(
                "utf-8"
            ),
            json.dumps(
                {
                    "task": self.task_wire(tenantRef="tenant-other"),
                    "replayed": False,
                }
            ).encode("utf-8"),
            json.dumps(
                {
                    "task": self.task_wire(kind="build-plan"),
                    "replayed": False,
                }
            ).encode("utf-8"),
            json.dumps(
                {
                    "task": self.task_wire(
                        result={
                            "artifacts": {
                                "evidence": {
                                    "digest": "sha256:" + ("e" * 64),
                                    "mediaType": "application/json",
                                    "sizeBytes": 10,
                                    "url": "http://10.0.0.10/private",
                                }
                            }
                        }
                    ),
                    "replayed": False,
                }
            ).encode("utf-8"),
        )
        for raw in malformed_responses:
            with (
                self.subTest(raw=raw[:40]),
                patch(
                    "urllib.request.urlopen", return_value=_Response(raw, status=202)
                ),
            ):
                with self.assertRaises(LumaAdapterError) as caught:
                    self.adapter.create_analyze_task(
                        self.context,
                        self.request,
                        idempotency_key="operation-a:malformed",
                    )
            self.assertEqual(caught.exception.code, AdapterErrorCode.PROTOCOL_ERROR)

    def test_succeeded_results_must_match_strict_control_contract(self) -> None:
        incomplete_analyze = self.analyze_result()
        incomplete_analyze.pop("sourceTreeDigest")
        wrong_analyze_media = self.analyze_result()
        wrong_analyze_media["artifacts"]["buildPlan"]["mediaType"] = (
            "application/vnd.lae.build-plan+json"
        )
        mismatched_analyze_digest = self.analyze_result()
        mismatched_analyze_digest["artifacts"]["evidence"]["digest"] = "sha256:" + (
            "9" * 64
        )
        missing_analyze_verdict = self.analyze_result()
        missing_analyze_verdict.pop("verdict")
        inconsistent_analyze_blockers = self.analyze_result()
        inconsistent_analyze_blockers["blockers"] = [
            {
                "code": "HTTP_ENTRYPOINT_MISSING",
                "path": "Dockerfile",
                "field": "command",
                "remediation": "Expose one HTTP entrypoint.",
            }
        ]

        incomplete_build = self.build_result()
        incomplete_build.pop("provenanceDigests")
        wrong_build_media = self.build_result()
        wrong_build_media["artifacts"]["web-scan"]["mediaType"] = "application/json"
        mismatched_build_artifact = self.build_result()
        mismatched_build_artifact["artifacts"]["web-sbom"]["digest"] = (
            "sha256:" + ("9" * 64)
        )
        mismatched_build_image = self.build_result()
        mismatched_build_image["images"]["web"] = "registry.internal/web@sha256:" + (
            "9" * 64
        )

        variants = (
            ("analyze-incomplete", "analyze-source", incomplete_analyze),
            ("analyze-wrong-media", "analyze-source", wrong_analyze_media),
            (
                "analyze-descriptor-digest-mismatch",
                "analyze-source",
                mismatched_analyze_digest,
            ),
            ("analyze-missing-verdict", "analyze-source", missing_analyze_verdict),
            (
                "analyze-inconsistent-blockers",
                "analyze-source",
                inconsistent_analyze_blockers,
            ),
            ("build-incomplete", "build-plan", incomplete_build),
            ("build-wrong-media", "build-plan", wrong_build_media),
            (
                "build-artifact-digest-mismatch",
                "build-plan",
                mismatched_build_artifact,
            ),
            ("build-image-digest-mismatch", "build-plan", mismatched_build_image),
        )
        for label, kind, result in variants:
            response = self.response(
                {
                    "task": self.task_wire(
                        kind=kind,
                        status="succeeded",
                        startedAt=101,
                        completedAt=102,
                        result=copy.deepcopy(result),
                    ),
                    "replayed": True,
                },
                status=202,
            )
            with (
                self.subTest(case=label),
                patch("urllib.request.urlopen", return_value=response),
                self.assertRaises(LumaAdapterError) as caught,
            ):
                if kind == "analyze-source":
                    self.adapter.create_analyze_task(
                        self.context,
                        self.request,
                        idempotency_key=f"operation-a:{label}",
                    )
                else:
                    self.adapter.create_build_task(
                        self.context,
                        self.build_request(),
                        idempotency_key=f"operation-a:{label}",
                    )
            self.assertEqual(caught.exception.code, AdapterErrorCode.PROTOCOL_ERROR)

    def test_secrets_and_internal_endpoint_never_enter_adapter_logs(self) -> None:
        response = self.response(
            {
                "task": self.task_wire(
                    status="succeeded",
                    startedAt=101,
                    completedAt=102,
                    result=self.analyze_result(),
                ),
                "replayed": True,
            },
            status=202,
        )
        logger = logging.getLogger("lae_luma_adapter.http")
        old_propagate = logger.propagate
        logger.propagate = True
        try:
            with (
                patch("urllib.request.urlopen", return_value=response),
                self.assertLogs("lae_luma_adapter.http", level="DEBUG") as captured,
            ):
                self.adapter.create_analyze_task(
                    self.context,
                    self.request,
                    idempotency_key="operation-a:log-safety",
                )
        finally:
            logger.propagate = old_propagate
        log_text = "\n".join(captured.output)
        for forbidden in (
            self.token,
            self.request.credential_lease_id,
            self.request.source.repository,
            "luma.internal.example",
            "registry.internal",
        ):
            self.assertNotIn(forbidden, log_text)
        self.assertIn("method=POST path=/v1/builder/tasks status=202", log_text)

    def test_error_response_secrets_never_enter_adapter_logs(self) -> None:
        sentinel = "github_pat_error_body_secret"
        error = urllib.error.HTTPError(
            "https://luma.internal.example/v1/builder/tasks",
            500,
            "failure",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "errorInfo": {
                            "code": "luma_error",
                            "message": f"{sentinel} at http://10.0.0.11:4646",
                        }
                    }
                ).encode("utf-8")
            ),
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertLogs("lae_luma_adapter.http", level="INFO") as captured,
            self.assertRaises(LumaAdapterError),
        ):
            self.adapter.create_analyze_task(
                self.context,
                self.request,
                idempotency_key="operation-a:error-log-safety",
            )
        log_text = "\n".join(captured.output)
        for forbidden in (
            sentinel,
            self.token,
            self.request.credential_lease_id,
            "10.0.0.11",
            "luma.internal.example",
        ):
            self.assertNotIn(forbidden, log_text)


if __name__ == "__main__":
    unittest.main()
