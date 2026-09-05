import json
import unittest

from luma.nomad_render import render_control_job


class ControlJobAutoRevertTests(unittest.TestCase):
    def test_ordinary_rollout_keeps_auto_revert(self):
        job = render_control_job(
            image="ghcr.io/acme/luma-control:v1",
            node_name="manager-1",
            as_json=False,
        )["Job"]
        self.assertIs(job["Update"]["AutoRevert"], True)

    def test_migration_rollout_disables_auto_revert_in_both_formats(self):
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                rendered = render_control_job(
                    image="ghcr.io/acme/luma-control:v2",
                    node_name="manager-1",
                    allow_auto_revert=False,
                    as_json=as_json,
                )
                job = (json.loads(rendered) if as_json else rendered)["Job"]
                self.assertIs(job["Update"]["AutoRevert"], False)
                self.assertEqual(job["Update"]["HealthCheck"], "checks")
                self.assertEqual(job["Update"]["HealthyDeadline"], 120_000_000_000)


if __name__ == "__main__":
    unittest.main()
