from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lae_store.models import TemplateHealth
from lae_store.template_health import _apply_result


class TemplateHealthTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 14, tzinfo=timezone.utc)
        self.row = TemplateHealth(
            template_id="fastapi-minimal",
            template_version="2026.07.14-1",
            published=True,
            consecutive_failures=0,
            last_status="unverified",
        )

    def apply(self, run: int, *, succeeded: bool) -> None:
        _apply_result(
            self.row,
            run_id=f"tsm-{run}",
            succeeded=succeeded,
            error_code=None if succeeded else "LAE_TEMPLATE_ACCEPTANCE_FAILED",
            now=self.now + timedelta(days=run),
            failure_threshold=3,
        )

    def test_three_consecutive_failures_auto_unpublish(self) -> None:
        self.apply(1, succeeded=False)
        self.apply(2, succeeded=False)
        self.assertTrue(self.row.published)
        self.apply(3, succeeded=False)
        self.assertFalse(self.row.published)
        self.assertEqual(self.row.consecutive_failures, 3)
        self.assertIsNotNone(self.row.auto_unpublished_at)

    def test_success_resets_failures_and_republishes(self) -> None:
        for run in range(1, 4):
            self.apply(run, succeeded=False)
        self.apply(4, succeeded=True)
        self.assertTrue(self.row.published)
        self.assertEqual(self.row.consecutive_failures, 0)
        self.assertEqual(self.row.last_status, "succeeded")
        self.assertIsNone(self.row.last_error_code)
        self.assertIsNone(self.row.auto_unpublished_at)


if __name__ == "__main__":
    unittest.main()
