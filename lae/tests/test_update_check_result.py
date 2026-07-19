from __future__ import annotations

import unittest

from lae_store import (
    UpdateCheckResult,
    diff_deployment_plans,
    public_update_check_from_operation,
)


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
                "changes",
                "candidateAnalysis",
                "digests",
            },
        )
        self.assertIsNone(body["changes"])
        self.assertIsNone(body["candidateAnalysis"])
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

    def test_plan_diff_is_closed_sorted_and_marks_destructive_changes(self) -> None:
        baseline = {
            "schemaVersion": "lae.deployment-plan/v1",
            "services": [
                {"key": "api", "role": "http", "port": 8080},
                {"key": "worker", "role": "worker", "port": None},
            ],
            "routes": [
                {"serviceKey": "api", "containerPort": 8080, "primary": True}
            ],
            "volumes": [
                {"key": "data", "requestedBytes": 1024, "deletePolicy": "retain"}
            ],
            "environment": [
                {
                    "scope": "runtime",
                    "name": "DATABASE_URL",
                    "services": ["api", "worker"],
                    "required": True,
                    "sensitive": True,
                }
            ],
        }
        candidate = {
            "schemaVersion": "lae.deployment-plan/v1",
            "services": [
                {"key": "api", "role": "http", "port": 3000},
                {"key": "scheduler", "role": "worker", "port": None},
            ],
            "routes": [
                {"serviceKey": "api", "containerPort": 3000, "primary": True}
            ],
            "volumes": [
                {"key": "data", "requestedBytes": 2048, "deletePolicy": "retain"}
            ],
            "environment": [
                {
                    "scope": "runtime",
                    "name": "DATABASE_URL",
                    "services": ["api"],
                    "required": True,
                    "sensitive": True,
                },
                {
                    "scope": "runtime",
                    "name": "SIGNING_KEY",
                    "services": ["api"],
                    "required": True,
                    "sensitive": True,
                },
            ],
        }
        changes = diff_deployment_plans(baseline, candidate)
        self.assertEqual(changes.services.added, ("scheduler",))
        self.assertEqual(changes.services.removed, ("worker",))
        self.assertEqual(changes.services.changed, ("api",))
        self.assertEqual(changes.routes.changed, ("api",))
        self.assertEqual(changes.volumes.changed, ("data",))
        self.assertEqual(
            changes.environment.added,
            ("runtime:SIGNING_KEY",),
        )
        self.assertEqual(
            changes.environment.changed,
            ("runtime:DATABASE_URL",),
        )
        self.assertTrue(changes.destructive)
        self.assertEqual(
            changes.confirmations,
            (
                "PERSISTENT_VOLUME_CHANGE",
                "PUBLIC_ROUTE_CHANGE",
                "REQUIRED_ENVIRONMENT_ADDED",
                "SERVICE_REMOVAL",
            ),
        )

        result = UpdateCheckResult(
            baseline_available=True,
            source_changed=True,
            deployment_plan_changed=True,
            changed=True,
            baseline_source_tree_digest=SOURCE_BASE,
            baseline_deployment_plan_digest=PLAN_BASE,
            candidate_source_tree_digest=SOURCE_NEW,
            candidate_deployment_plan_digest=PLAN_NEW,
            plan_changes=changes,
            candidate_analysis_id="ana_candidate_001",
            candidate_verdict="deployable",
        )
        self.assertEqual(UpdateCheckResult.from_body(result.to_body()), result)

    def test_plan_diff_does_not_mark_additive_route_or_optional_env_destructive(self) -> None:
        baseline = {
            "schemaVersion": "lae.deployment-plan/v1",
            "services": [{"key": "api", "role": "http"}],
            "routes": [],
            "volumes": [],
            "environment": [],
        }
        candidate = {
            "schemaVersion": "lae.deployment-plan/v1",
            "services": [
                {"key": "api", "role": "http"},
                {"key": "docs", "role": "http"},
            ],
            "routes": [{"serviceKey": "docs", "containerPort": 8080}],
            "volumes": [{"key": "cache", "requestedBytes": 1024}],
            "environment": [
                {
                    "scope": "runtime",
                    "name": "OPTIONAL_SETTING",
                    "services": ["api"],
                    "required": False,
                }
            ],
        }
        changes = diff_deployment_plans(baseline, candidate)
        self.assertFalse(changes.destructive)
        self.assertEqual(changes.confirmations, ())


if __name__ == "__main__":
    unittest.main()
