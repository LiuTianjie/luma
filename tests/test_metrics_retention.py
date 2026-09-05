"""Behavioral coverage for time-bounded metrics and incomplete heartbeats."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from luma.control import metrics


class MetricsRetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "LUMA_CONTROL_STATE_DIR": tmp.name,
            "LUMA_METRICS_HISTORY_POINTS": "60",
        })
        env.start()
        self.addCleanup(env.stop)

    def record(self, node, cpu=10, *, ts=3000, stats=True):
        containers = [{"service": "app_web", "cpuPercent": cpu, "memoryUsageBytes": 100}] if stats else []
        metrics.record_samples(node, {"cpuPercent": cpu}, containers, now=ts)

    def test_many_nodes_keep_same_duration_and_sum(self):
        for tick in range(65):
            for node in range(8):
                self.record(f"n{node}", node + 1, ts=3000 + tick * 30 + node)
        history = metrics.load_history("service", "app_web")
        self.assertEqual(len(history["cpuPercent"]), 60)
        self.assertEqual(history["cpuPercent"][-1], [4927, 36])
        self.assertEqual(history["cpuPercent"][0][0], 3157)
        self.assertEqual(history["memoryUsageBytes"][-1][1], 800)

    def test_burst_replaces_node_and_service_current_bucket(self):
        self.record("a", 10, ts=3000)
        self.record("a", 20, ts=3001)
        for kind, name in [("node", "a"), ("service", "app_web")]:
            self.assertEqual(metrics.load_history(kind, name)["cpuPercent"], [[3001, 20]])

    def test_empty_snapshot_removes_node_immediately(self):
        self.record("a", 10)
        self.record("b", 20)
        self.record("a", ts=3030, stats=False)
        self.assertEqual(metrics.load_history("service", "app_web")["cpuPercent"][-1], [3030, 20])
        scratch = json.loads(metrics.history_path().read_text())["_serviceScratch"]
        self.assertNotIn("a", scratch["app_web"])

    def test_omitted_snapshot_retains_previous_contribution(self):
        self.record("a", 10)
        metrics.record_samples("a", {"cpuPercent": 5}, None, now=3001)
        self.record("b", 20, ts=3002)
        self.assertEqual(metrics.load_history("service", "app_web")["cpuPercent"][-1], [3002, 30])

    def test_retired_entities_are_pruned_by_age_on_next_write(self):
        self.record("retired")
        metrics.record_samples("new", {"cpuPercent": 1}, [], now=3000 + metrics.retention_seconds() + 1)
        raw = json.loads(metrics.history_path().read_text())
        self.assertNotIn("retired", raw["nodes"])
        self.assertEqual(raw["services"], {})
        self.assertEqual(raw["_serviceScratch"], {})

    def test_read_window_never_exceeds_retention(self):
        self.record("old")
        self.assertEqual(metrics.load_history("node", "old", window=100000, now=100000), {"cpuPercent": []})

    def test_legacy_duplicate_history_is_compacted_without_failure(self):
        metrics.history_path().write_text(json.dumps({"version": 1, "nodes": {}, "services": {
            "legacy": {"cpuPercent": [[3000, 5], [3000, 7], [3004, 10], [3030, 11], ["bad", 1], [3040, "NaN"]]},
        }}))
        self.assertEqual(metrics.load_history("service", "legacy")["cpuPercent"], [[3004, 10], [3030, 11]])
        self.record("new", ts=3050)
        self.assertEqual(json.loads(metrics.history_path().read_text())["version"], 2)

    def test_missing_metrics_do_not_become_zero(self):
        metrics.record_samples("a", {}, [{"service": "app_web", "cpuPercent": 12}], now=3000)
        self.assertEqual(metrics.load_history("service", "app_web"), {"cpuPercent": [[3000, 12]]})

    def test_non_finite_or_malformed_values_are_dropped(self):
        metrics.record_samples("a", {"cpuPercent": "Infinity", "memoryUsedPercent": "NaN"}, [
            {"service": "app_web", "cpuPercent": float("nan"), "memoryUsageBytes": True},
        ], now=3000)
        self.assertEqual(metrics.load_history("node", "a"), {})
        self.assertEqual(metrics.load_history("service", "app_web"), {})

    def test_metadata_reports_actual_range_and_window_cap(self):
        info = metrics.history_metadata({"cpuPercent": [[3000, 1], [3060, 2]]}, 86400, now=3090)
        self.assertEqual(info, {"requestedWindow": 86400, "window": 1800, "retentionSeconds": 1800,
            "sampleIntervalSeconds": 30, "availableFrom": 3000, "latestSampleAt": 3060, "updatedAt": 3090})
        self.assertIsNone(metrics.history_metadata({}, 900)["latestSampleAt"])

    def test_disk_and_inode_series_are_retained_when_available(self):
        metrics.record_samples("a", {"diskUsedPercent": 90, "inodesUsedPercent": 42}, [], now=3000)
        self.assertEqual(metrics.load_history("node", "a"), {"diskUsedPercent": [[3000, 90]], "inodesUsedPercent": [[3000, 42]]})

    def test_expired_samples_do_not_trigger_sustained_alert(self):
        for ts in range(3000, 3301, 30):
            metrics.record_samples("a", {"memoryUsedPercent": 95}, [], now=ts)
        self.assertIsNone(metrics.sustained_breach("node", "a", "memoryUsedPercent", threshold=85, duration_seconds=900, now=3600))


class HeartbeatMetricsIntegrationTests(unittest.TestCase):
    """Exercise the real heartbeat -> normalization -> history path.

    Config and control-state I/O are isolated. Authentication, heartbeat
    mutation, and the metrics file recorder use their real implementations.
    """

    def setUp(self):
        from luma.control import server

        self.server = server
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token = "metrics-heartbeat-test-token"
        self.state = {"nodes": {name: {"agent": {
            "tokenHash": server._hash_agent_token(self.token),
        }} for name in ("node-a", "node-b")}}
        for mocked in (
            patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": tmp.name}),
            patch.object(server, "load_config", return_value=None),
            patch.object(server, "load_state", return_value=self.state),
            patch.object(server, "load_runtime_state", return_value=self.state),
            patch.object(server, "_mutate_control_state", side_effect=lambda mutate: mutate(self.state)),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.clock = patch.object(server.time, "time", return_value=3000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        for name, cpu in (("node-a", 10), ("node-b", 20)):
            server.handle_node_agent_heartbeat(self.token, {
                "nodeName": name,
                "metrics": {"cpuPercent": 5},
                "containerStats": [{"service": "app_web", "containerId": name,
                    "cpuPercent": cpu, "memoryUsageBytes": 100}],
            })

    def scratch(self):
        return json.loads(metrics.history_path().read_text())["_serviceScratch"]

    def test_metrics_only_heartbeat_preserves_service_contribution_and_timestamp(self):
        self.now.return_value = 3030
        result = self.server.handle_node_agent_heartbeat(self.token, {
            "nodeName": "node-a", "metrics": {"cpuPercent": 7},
        })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.scratch()["app_web"]["node-a"], {
            "ts": 3000, "cpuPercent": 10, "memoryUsageBytes": 100,
        })
        self.assertEqual(metrics.load_history("node", "node-a")["cpuPercent"][-1], [3030, 7])
        self.assertEqual(metrics.load_history("service", "app_web")["cpuPercent"][-1], [3000, 30])
        # Omission also leaves the last complete container snapshot intact.
        self.assertEqual(len(self.state["nodes"]["node-a"]["agent"]["containerStats"]), 1)

    def test_explicit_empty_snapshot_without_node_metrics_removes_contribution(self):
        self.now.return_value = 3030
        self.server.handle_node_agent_heartbeat(self.token, {
            "nodeName": "node-a", "containerStats": [],
        })
        self.assertNotIn("node-a", self.scratch()["app_web"])
        self.assertEqual(self.state["nodes"]["node-a"]["agent"]["containerStats"], [])
        self.assertEqual(metrics.load_history("service", "app_web")["cpuPercent"][-1], [3030, 20])
        self.assertEqual(metrics.load_history("service", "app_web")["memoryUsageBytes"][-1], [3030, 100])


if __name__ == "__main__":
    unittest.main()
