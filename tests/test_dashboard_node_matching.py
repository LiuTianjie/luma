import unittest

from luma.control import server


class OrchestratorNodeMatchingTests(unittest.TestCase):
    def test_nomad_id_wins_over_shared_macos_hostnames(self):
        registered = [
            {"name": "air2", "hostname": "Mac.lan", "nodeId": "id-air2"},
            {"name": "air3", "hostname": "Mac.lan", "nodeId": "id-air3"},
            {"name": "lab", "hostname": "lab"},
        ]
        raw = [
            {"id": "id-air3", "hostname": "Mac.lan"},
            {"id": "id-air2", "hostname": "Mac.lan"},
            {"id": "id-lab", "hostname": "lab"},
            {"id": "id-other", "hostname": "ubuntu"},
        ]
        pairs = [(node["id"], name) for node, name in server._match_orchestrator_nodes(registered, raw)]
        self.assertEqual(pairs, [("id-air3", "air3"), ("id-air2", "air2"), ("id-lab", "lab"), ("id-other", "")])


class DashboardIssueTests(unittest.TestCase):
    def service(self, **values):
        base = {"fullName": "web", "running": 1, "desired": 1, "failed": 0, "pending": 0,
                "deploymentStatus": "failed_partial", "lastError": "rollout failed"}
        return {**base, **values}

    def test_failed_rollout_with_healthy_previous_version_is_a_warning(self):
        issues = server._dashboard_issues([], [self.service()])
        self.assertEqual([(item["severity"], item["kind"]) for item in issues], [("warning", "deployment")])
        self.assertIn("previous version is still serving", issues[0]["message"])

    def test_failed_rollout_without_running_tasks_stays_critical(self):
        issues = server._dashboard_issues([], [self.service(running=0)])
        self.assertIn(("critical", "deployment"), [(item["severity"], item["kind"]) for item in issues])

    def test_successful_redeploy_clears_the_issue(self):
        self.assertEqual(server._dashboard_issues([], [self.service(deploymentStatus="active")]), [])


if __name__ == "__main__":
    unittest.main()
