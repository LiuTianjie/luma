from __future__ import annotations

import sys
import unittest
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-luma-adapter" / "src"))
sys.path.insert(0, str(LAE_ROOT.parent))

from lae_luma_adapter import (  # noqa: E402
    AnalyzeSourceRequest,
    BuilderLimits,
    BuildPlanRequest,
    LumaCallContext,
    SourceReference,
)
from lae_luma_adapter._codec import task_request_body  # noqa: E402
from luma.builder_tasks import validate_builder_task_request  # noqa: E402


class LumaAdapterContractTests(unittest.TestCase):
    def test_generated_analyze_and_build_requests_match_current_luma_v1(self) -> None:
        context = LumaCallContext("tenant-a", "application-a", "operation-a")
        limits = BuilderLimits(
            cpu=2,
            memory_mib=2048,
            disk_mib=4096,
            timeout_seconds=300,
        )
        analyze = AnalyzeSourceRequest(
            source=SourceReference("https://github.com/acme/example.git", ref="main"),
            credential_lease_id="credential-lease-a",
            agent_image_digest="registry.internal/lae-agent@sha256:" + ("a" * 64),
            policy_version="2026-07-11",
            limits=limits,
        )
        snapshot_digest = "sha256:" + ("c" * 64)
        build = BuildPlanRequest(
            source_snapshot_id="snapshot-a",
            source_snapshot_digest=snapshot_digest,
            signed_build_plan={
                "schemaVersion": "lae.build-plan/v1",
                "sourceSnapshotDigest": snapshot_digest,
                "resolvedCommit": "1" * 40,
                "policyVersion": "2026-07-11",
                "builds": [],
                "externalImages": [
                    {
                        "key": "database",
                        "ref": "postgres:17",
                        "resolvedDigest": "sha256:" + ("d" * 64),
                        "platform": "linux/amd64",
                    }
                ],
                "signature": {
                    "keyId": "lae-plan-test",
                    "value": "ZmFrZS1zaWduYXR1cmU",
                },
            },
            credential_lease_id="credential-lease-b",
            limits=limits,
        )

        for kind, request in (
            ("analyze-source", analyze),
            ("build-plan", build),
        ):
            with self.subTest(kind=kind):
                body = task_request_body(context, kind=kind, payload=request.to_wire())
                normalized = validate_builder_task_request(body)
                self.assertEqual(normalized["schemaVersion"], "luma.builder-task/v1")
                self.assertEqual(normalized["kind"], kind)
                self.assertEqual(normalized["tenantRef"], context.tenant_ref)
                self.assertEqual(normalized["applicationRef"], context.application_ref)


if __name__ == "__main__":
    unittest.main()
