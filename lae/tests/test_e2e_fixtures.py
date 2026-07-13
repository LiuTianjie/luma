from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


LAE_ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/python/lae-agent-core/src",
):
    sys.path.insert(0, str(LAE_ROOT / relative))

from lae_agent_core import analyze_source  # noqa: E402
from lae_contracts import validate_instance  # noqa: E402


def metadata() -> dict[str, str]:
    return {
        "externalOperationId": "op-e2e-fixture",
        "tenantRef": "tenant-e2e",
        "applicationRef": "application-e2e",
        "resolvedCommit": "0123456789abcdef0123456789abcdef01234567",
        "sourceSnapshotId": "snapshot-e2e",
        "sourceSnapshotDigest": "sha256:" + "a" * 64,
        "policyVersion": "2026-07-11",
    }


class E2EFixtureTests(unittest.TestCase):
    def _analyze(self, name: str) -> tuple[dict[str, object], dict[str, object]]:
        source = LAE_ROOT / "e2e" / "fixtures" / name
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = analyze_source(source, metadata(), output)
            plan = json.loads((output / "deployment-plan.json").read_text())
        self.assertFalse(validate_instance("deployment-plan.v1.schema.json", plan))
        return result, plan

    def test_compose_golden_fixture_requires_only_database_secret(self) -> None:
        result, plan = self._analyze("compose-two-http-volume")

        self.assertEqual(result["decision"], "needs_configuration")
        self.assertEqual(plan["kind"], "compose")
        self.assertEqual(
            {route["serviceKey"] for route in plan["routes"]},
            {"admin", "web"},
        )
        self.assertEqual(
            {service["key"]: service["role"] for service in plan["services"]},
            {
                "admin": "http",
                "postgres": "datastore",
                "web": "http",
                "worker": "worker",
            },
        )
        self.assertEqual(
            {volume["key"] for volume in plan["volumes"]},
            {"app-data", "pg-data"},
        )
        self.assertEqual(
            [item["name"] for item in plan["environment"]],
            ["POSTGRES_PASSWORD"],
        )
        self.assertEqual(plan["blockers"], [])

    def test_unsupported_fixture_returns_actionable_blockers(self) -> None:
        result, plan = self._analyze("compose-unsupported-host-access")

        self.assertEqual(result["decision"], "deny")
        blockers = "\n".join(plan["blockers"])
        self.assertIn("COMPOSE_PRIVILEGED", blockers)
        self.assertIn("COMPOSE_NETWORK_MODE_HOST", blockers)
        self.assertIn("COMPOSE_HOST_PORT", blockers)
        self.assertIn("COMPOSE_DOCKER_SOCKET", blockers)


if __name__ == "__main__":
    unittest.main()
