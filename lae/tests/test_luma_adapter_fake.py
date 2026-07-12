from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-luma-adapter" / "src"))

from lae_luma_adapter import (  # noqa: E402
    AdapterErrorCode,
    AnalyzeSourceRequest,
    BuilderLimits,
    BuildPlanRequest,
    FakeLuma,
    LumaAdapterError,
    LumaBuilderAdapter,
    LumaCallContext,
    ServicePrincipal,
    SourceReference,
)


class FakeLumaAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeLuma(clock=lambda: 1_720_000_000)
        self.principal = ServicePrincipal(
            "lae-worker-a", "principal-token-super-secret"
        )
        self.adapter = self.fake.bind(self.principal)
        self.context = LumaCallContext(
            tenant_ref="tenant-a",
            application_ref="application-a",
            external_operation_id="operation-a",
            request_id="request-a",
        )

    @staticmethod
    def analyze_request(*, ref: str = "main") -> AnalyzeSourceRequest:
        return AnalyzeSourceRequest(
            source=SourceReference("https://github.com/acme/example.git", ref=ref),
            credential_lease_id="credential-lease-super-secret",
            agent_image_digest="registry.internal/lae-agent@sha256:" + ("a" * 64),
            policy_version="2026-07-11",
            limits=BuilderLimits(
                cpu=2, memory_mib=2048, disk_mib=4096, timeout_seconds=300
            ),
        )

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
                "signature": {"keyId": "lae-plan-test", "value": "ZmFrZS1zaWduYXR1cmU"},
            },
            credential_lease_id="credential-lease-build-super-secret",
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
            "knowledgeVersion": "2026-07-11.1",
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
            "images": {
                "web": "registry.internal/tenant/app/web@" + image_digest,
            },
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

    def test_fake_implements_protocol_and_secret_fields_are_repr_safe(self) -> None:
        self.assertIsInstance(self.adapter, LumaBuilderAdapter)
        self.assertNotIn(self.principal.token, repr(self.principal))
        self.assertNotIn("credential-lease-super-secret", repr(self.analyze_request()))

    def test_context_is_authoritative_and_task_view_is_tenant_safe(self) -> None:
        created = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:analyze",
        )
        task = self.fake.complete_task(
            created.task.task_id,
            status="succeeded",
            result=self.analyze_result(),
        )
        public_text = json.dumps(task.to_tenant_dict(), sort_keys=True)
        self.assertNotIn("registry.internal", public_text)
        self.assertNotIn("credentialLease", public_text)
        self.assertEqual(task.result["sourceSnapshotId"], "snapshot-a")

    def test_idempotency_replays_same_request_and_rejects_changed_request(self) -> None:
        first = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:analyze",
        )
        replay = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:analyze",
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.task.task_id, replay.task.task_id)

        with self.assertRaises(LumaAdapterError) as caught:
            self.adapter.create_analyze_task(
                self.context,
                self.analyze_request(ref="release"),
                idempotency_key="operation-a:analyze",
            )
        self.assertEqual(caught.exception.code, AdapterErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(caught.exception.http_status, 409)

    def test_same_principal_can_resume_but_other_principal_and_context_cannot(
        self,
    ) -> None:
        created = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:ownership",
        )
        resumed_adapter = self.fake.bind(
            ServicePrincipal("lae-worker-a", self.principal.token)
        )
        self.assertEqual(
            resumed_adapter.get_builder_task(
                self.context, created.task.task_id
            ).task_id,
            created.task.task_id,
        )

        other_adapter = self.fake.bind(
            ServicePrincipal("lae-worker-b", "other-principal-token")
        )
        for adapter, context in (
            (other_adapter, self.context),
            (
                resumed_adapter,
                LumaCallContext("tenant-b", "application-a", "operation-a"),
            ),
        ):
            with (
                self.subTest(adapter=adapter, context=context),
                self.assertRaises(LumaAdapterError) as caught,
            ):
                adapter.get_builder_task(context, created.task.task_id)
            self.assertEqual(caught.exception.code, AdapterErrorCode.NOT_FOUND)

    def test_queued_cancel_is_idempotent_and_late_completion_cannot_revive(
        self,
    ) -> None:
        created = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:queued-cancel",
        )
        first = self.adapter.cancel_builder_task(self.context, created.task.task_id)
        second = self.adapter.cancel_builder_task(self.context, created.task.task_id)
        late = self.fake.complete_task(
            created.task.task_id, status="succeeded", result={"unexpected": True}
        )
        self.assertEqual(first.task.status, "canceled")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(late.status, "canceled")
        self.assertIsNone(late.result)

    def test_running_cancel_fences_late_success(self) -> None:
        created = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:running-cancel",
        )
        self.fake.start_task(created.task.task_id)
        canceled = self.adapter.cancel_builder_task(self.context, created.task.task_id)
        self.assertEqual(canceled.task.status, "cancel_requested")
        late = self.fake.complete_task(
            created.task.task_id, status="succeeded", result={"unexpected": True}
        )
        self.assertEqual(late.status, "canceled")
        self.assertIsNone(late.result)

    def test_event_cursor_replay_and_expiry(self) -> None:
        created = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:events",
        )
        self.fake.emit_event(created.task.task_id, "source.fetch")
        self.fake.emit_event(created.task.task_id, "analysis")

        first_page = self.adapter.get_builder_task_events(
            self.context, created.task.task_id, after=0, limit=1
        )
        second_page = self.adapter.get_builder_task_events(
            self.context,
            created.task.task_id,
            after=first_page.next_cursor,
            limit=10,
        )
        self.assertTrue(first_page.has_more)
        self.assertEqual([event.cursor for event in first_page.events], [1])
        self.assertEqual([event.cursor for event in second_page.events], [2, 3])

        self.fake.trim_events_before(created.task.task_id, 3)
        with self.assertRaises(LumaAdapterError) as caught:
            self.adapter.get_builder_task_events(
                self.context, created.task.task_id, after=1
            )
        self.assertEqual(caught.exception.code, AdapterErrorCode.CURSOR_EXPIRED)

    def test_build_plan_is_a_distinct_typed_task(self) -> None:
        created = self.adapter.create_build_task(
            self.context,
            self.build_request(),
            idempotency_key="operation-a:build",
        )
        self.assertEqual(created.task.kind, "build-plan")
        completed = self.fake.complete_task(
            created.task.task_id,
            status="succeeded",
            result=self.build_result(),
        )
        public_text = json.dumps(completed.to_tenant_dict(), sort_keys=True)
        self.assertNotIn("registry.internal", public_text)
        self.assertEqual(
            completed.result["imageDigests"]["web"], "sha256:" + ("d" * 64)
        )

    def test_external_image_resolution_artifacts_are_strict_and_tenant_safe(self) -> None:
        result = self.build_result()
        result["images"] = {
            "database": "docker.io/library/postgres@sha256:" + ("d" * 64)
        }
        for field in (
            "imageDigests",
            "sbomDigests",
            "provenanceDigests",
            "scanDigests",
        ):
            result[field] = {"database": next(iter(result[field].values()))}
        result["artifacts"] = {
            key.replace("web-", "database-"): value
            for key, value in result["artifacts"].items()
        }
        result["artifacts"]["database-provenance"]["mediaType"] = (
            "application/vnd.lae.external-resolution+json"
        )

        created = self.adapter.create_build_task(
            self.context,
            self.build_request(),
            idempotency_key="operation-a:external-build",
        )
        completed = self.fake.complete_task(
            created.task.task_id,
            status="succeeded",
            result=result,
        )
        public_text = json.dumps(completed.to_tenant_dict(), sort_keys=True)
        self.assertNotIn("docker.io", public_text)
        self.assertEqual(
            completed.result["artifacts"]["database-provenance"]["mediaType"],
            "application/vnd.lae.external-resolution+json",
        )

        wrong = copy.deepcopy(result)
        wrong["artifacts"]["database-provenance"]["mediaType"] = "application/json"
        second = self.adapter.create_build_task(
            self.context,
            self.build_request(),
            idempotency_key="operation-a:external-build-invalid",
        )
        with self.assertRaises(LumaAdapterError) as caught:
            self.fake.complete_task(second.task.task_id, status="succeeded", result=wrong)
        self.assertEqual(caught.exception.code, AdapterErrorCode.PROTOCOL_ERROR)

    def test_fake_rejects_partial_succeeded_results_without_mutating_task(self) -> None:
        created = self.adapter.create_analyze_task(
            self.context,
            self.analyze_request(),
            idempotency_key="operation-a:partial-success",
        )
        with self.assertRaises(LumaAdapterError) as caught:
            self.fake.complete_task(
                created.task.task_id,
                status="succeeded",
                result={"sourceSnapshotDigest": "sha256:" + ("c" * 64)},
            )
        self.assertEqual(caught.exception.code, AdapterErrorCode.PROTOCOL_ERROR)
        self.assertEqual(
            self.adapter.get_builder_task(self.context, created.task.task_id).status,
            "queued",
        )

    def test_fake_rejects_wrong_artifact_media_and_digest_binding(self) -> None:
        wrong_media = self.analyze_result()
        wrong_media["artifacts"]["buildPlan"]["mediaType"] = "application/json"
        wrong_digest = self.analyze_result()
        wrong_digest["artifacts"]["evidence"]["digest"] = "sha256:" + ("9" * 64)
        for index, result in enumerate((wrong_media, wrong_digest)):
            created = self.adapter.create_analyze_task(
                self.context,
                self.analyze_request(),
                idempotency_key=f"operation-a:invalid-result-{index}",
            )
            with (
                self.subTest(index=index),
                self.assertRaises(LumaAdapterError) as caught,
            ):
                self.fake.complete_task(
                    created.task.task_id,
                    status="succeeded",
                    result=copy.deepcopy(result),
                )
            self.assertEqual(caught.exception.code, AdapterErrorCode.PROTOCOL_ERROR)
            self.assertEqual(
                self.adapter.get_builder_task(
                    self.context, created.task.task_id
                ).status,
                "queued",
            )


if __name__ == "__main__":
    unittest.main()
