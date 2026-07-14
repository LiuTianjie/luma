from __future__ import annotations

import collections
import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "staging_recovery_drill.py"
SPEC = importlib.util.spec_from_file_location("staging_recovery_drill", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RecoveryPolicyTest(unittest.TestCase):
    def test_worker_rejects_any_public_failure(self) -> None:
        with self.assertRaises(MODULE.DrillFailure):
            MODULE._validate_counts(
                "worker",
                {"https://example.test": collections.Counter({200: 3, 502: 1})},
                [],
                max_api_outage_seconds=15,
            )

    def test_api_allows_bounded_ready_outage(self) -> None:
        MODULE._validate_counts(
            "api",
            {
                MODULE.API_READY_URL: collections.Counter({502: 2, 200: 8}),
                "https://lae-staging.itool.tech/": collections.Counter({200: 10}),
            },
            [
                {"second": 0.5, "url": MODULE.API_READY_URL, "status": 502},
                {"second": 3.5, "url": MODULE.API_READY_URL, "status": 200},
            ],
            max_api_outage_seconds=15,
        )

    def test_api_rejects_unbounded_ready_outage(self) -> None:
        with self.assertRaises(MODULE.DrillFailure):
            MODULE._validate_counts(
                "api",
                {MODULE.API_READY_URL: collections.Counter({502: 4, 200: 1})},
                [
                    {"second": 1.0, "url": MODULE.API_READY_URL, "status": 502},
                    {"second": 20.0, "url": MODULE.API_READY_URL, "status": 200},
                ],
                max_api_outage_seconds=15,
            )


if __name__ == "__main__":
    unittest.main()
