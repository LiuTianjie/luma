from __future__ import annotations

import unittest

from lae_store import UpdateCheckResult, public_update_check_from_operation


SOURCE_BASE = "sha256:" + "a" * 64
PLAN_BASE = "sha256:" + "b" * 64
SOURCE_NEW = "sha256:" + "c" * 64
PLAN_NEW = "sha256:" + "d" * 64


class UpdateCheckResultTests(unittest.TestCase):
    def test_available_baseline_round_trips_only_closed_digest_shape(self) -> None:
        result = UpdateCheckResult(
            baseline_available=True,
            source_changed=True,
            deployment_plan_changed=False,
            changed=True,
            baseline_source_tree_digest=SOURCE_BASE,
            baseline_deployment_plan_digest=PLAN_BASE,
            candidate_source_tree_digest=SOURCE_NEW,
            candidate_deployment_plan_digest=PLAN_BASE,
        )
        body = result.to_body()
        self.assertEqual(UpdateCheckResult.from_body(body), result)
        self.assertEqual(
            set(body),
            {
                "baselineAvailable",
                "sourceChanged",
                "deploymentPlanChanged",
                "changed",
                "digests",
            },
        )
        self.assertEqual(
            public_update_check_from_operation(
                kind="application.check-update",
                status="succeeded",
                result={
                    "sourceRevisionId": "src_internal",
                    "credential": "must-not-escape",
                    "updateCheck": body,
                },
            ),
            result,
        )

    def test_missing_baseline_is_explicit_and_conservatively_changed(self) -> None:
        result = UpdateCheckResult(
            baseline_available=False,
            source_changed=True,
            deployment_plan_changed=True,
            changed=True,
            candidate_source_tree_digest=SOURCE_NEW,
            candidate_deployment_plan_digest=PLAN_NEW,
        )
        body = result.to_body()
        self.assertIsNone(body["digests"]["baseline"])
        self.assertEqual(UpdateCheckResult.from_body(body), result)
        with self.assertRaises(ValueError):
            UpdateCheckResult(
                baseline_available=False,
                source_changed=False,
                deployment_plan_changed=True,
                changed=True,
                candidate_source_tree_digest=SOURCE_NEW,
                candidate_deployment_plan_digest=PLAN_NEW,
            )

    def test_extra_fields_malformed_digests_and_inconsistent_flags_fail_closed(
        self,
    ) -> None:
        valid = UpdateCheckResult(
            baseline_available=True,
            source_changed=False,
            deployment_plan_changed=False,
            changed=False,
            baseline_source_tree_digest=SOURCE_BASE,
            baseline_deployment_plan_digest=PLAN_BASE,
            candidate_source_tree_digest=SOURCE_BASE,
            candidate_deployment_plan_digest=PLAN_BASE,
        ).to_body()
        for mutated in (
            {**valid, "topology": {"node": "secret"}},
            {
                **valid,
                "digests": {
                    **valid["digests"],
                    "candidate": {
                        "sourceTree": "sha256:not-a-digest",
                        "deploymentPlan": PLAN_BASE,
                    },
                },
            },
            {**valid, "changed": True},
        ):
            with self.subTest(mutated=mutated):
                with self.assertRaises(ValueError):
                    UpdateCheckResult.from_body(mutated)
                self.assertIsNone(
                    public_update_check_from_operation(
                        kind="application.check-update",
                        status="succeeded",
                        result={"updateCheck": mutated},
                    )
                )

        self.assertIsNone(
            public_update_check_from_operation(
                kind="source.analyze",
                status="succeeded",
                result={"updateCheck": valid},
            )
        )


if __name__ == "__main__":
    unittest.main()
