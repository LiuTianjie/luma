from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/python/lae-agent-core/src",
    "packages/python/lae-core/src",
    "services/agent-controller/src",
):
    sys.path.insert(0, str(ROOT / relative))

from lae_agent_controller.__main__ import _valid_request  # noqa: E402
from lae_agent_core import analyze_source, build_ai_request  # noqa: E402


class AgentControllerContractTests(unittest.TestCase):
    def test_request_is_closed_and_never_accepts_source_content_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "package.json").write_text(
                '{"scripts":{"start":"node server.js"}}', encoding="utf-8"
            )
            analyze_source(
                source,
                {
                    "externalOperationId": "op-controller-test",
                    "tenantRef": "tenant-controller-test",
                    "applicationRef": "application-controller-test",
                    "resolvedCommit": "a" * 40,
                    "sourceSnapshotId": "snapshot-controller-test",
                    "sourceSnapshotDigest": "sha256:" + "b" * 64,
                    "policyVersion": "2026-07-11",
                },
                output,
            )
            request = build_ai_request(
                source,
                json.loads((output / "deployment-plan.json").read_text()),
                json.loads((output / "build-plan-proposal.json").read_text()),
                json.loads((output / "evidence.json").read_text()),
            )
        self.assertTrue(_valid_request(request, knowledge_version="2026-07-14.1"))

        with_content = json.loads(json.dumps(request))
        with_content["source"]["files"][0]["content"] = "credential=canary"
        self.assertFalse(_valid_request(with_content, knowledge_version="2026-07-14.1"))
        with_extra = json.loads(json.dumps(request))
        with_extra["extra"] = {"token": "canary"}
        self.assertFalse(_valid_request(with_extra, knowledge_version="2026-07-14.1"))
        nested = json.loads(json.dumps(request))
        nested["deterministic"]["findings"].append(
            {"path": "app.py", "rule": "x", "credential": "canary"}
        )
        self.assertFalse(_valid_request(nested, knowledge_version="2026-07-14.1"))
        self.assertFalse(_valid_request(request, knowledge_version="skewed"))


if __name__ == "__main__":
    unittest.main()
