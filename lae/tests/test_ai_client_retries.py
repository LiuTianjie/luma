from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "packages/python/lae-agent-core/src"):
    sys.path.insert(0, str(ROOT / relative))

from lae_agent_core.ai import (  # noqa: E402
    AIControllerClientConfig,
    request_ai_analysis,
)


class _Response:
    def __init__(self, value: dict[str, object]):
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.body


class AIClientRetryTests(unittest.TestCase):
    def test_retryable_controller_failure_is_retried_without_changing_request(self):
        error = urllib.error.HTTPError(
            "https://controller.example/v1/analyze",
            502,
            "bad gateway",
            {},
            io.BytesIO(b"{}"),
        )
        response = _Response(
            {
                "schemaVersion": "lae.ai-analysis-response/v1",
                "status": "succeeded",
                "knowledgeVersion": "2026-07-14.1",
                "proposal": {},
            }
        )
        with patch(
            "lae_agent_core.ai._NO_REDIRECT_OPENER.open",
            side_effect=[error, response],
        ) as opened, patch("lae_agent_core.ai.time.sleep") as slept:
            result = request_ai_analysis(
                AIControllerClientConfig(
                    "https://controller.example", "service-token", 5
                ),
                {"schemaVersion": "lae.ai-analysis-request/v1"},
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
