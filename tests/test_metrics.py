import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import metrics
from luma.control.state import init_state
from luma.errors import LumaError


class MetricsHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        env = patch.dict(
            os.environ,
            {"LUMA_CONTROL_STATE_DIR": str(self.state_dir), "LUMA_METRICS_HISTORY_POINTS": "60"},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def test_node_series_append_and_ring_buffer_trim(self):
        for i in range(65):
            metrics.record_samples(
                "cn-edge",
                {"cpuPercent": float(i), "memoryUsedPercent": 50.0},
                [],
                now=1000 + i * 30,
            )
        history = metrics.load_history("node", "cn-edge")
        cpu = history["cpuPercent"]
        # 65 points written, limit 60 -> oldest 5 dropped.
        self.assertEqual(len(cpu), 60)
        self.assertEqual(cpu[0], [1000 + 5 * 30, 5.0])
        self.assertEqual(cpu[-1], [1000 + 64 * 30, 64.0])
        self.assertEqual(len(history["memoryUsedPercent"]), 60)

    def test_darwin_loadpercent_falls_back_to_cpu_series(self):
        metrics.record_samples("mac", {"loadPercent": 42.0}, [], now=2000)
        history = metrics.load_history("node", "mac")
        self.assertEqual(history["cpuPercent"], [[2000, 42.0]])

    def test_service_metrics_sum_across_nodes_at_same_timestamp(self):
        metrics.record_samples(
            "node-a",
            {"cpuPercent": 5},
            [{"service": "web_web", "containerId": "aaa", "cpuPercent": 10, "memoryUsageBytes": 1000}],
            now=2000,
        )
        metrics.record_samples(
            "node-b",
            {"cpuPercent": 5},
            [{"service": "web_web", "containerId": "bbb", "cpuPercent": 20, "memoryUsageBytes": 3000}],
            now=2000,
        )
        svc = metrics.load_history("service", "web_web")
        # node-a heartbeat sees only itself (10); node-b heartbeat sums both (30).
        self.assertEqual(svc["cpuPercent"], [[2000, 10.0], [2000, 30.0]])
        self.assertEqual(svc["memoryUsageBytes"], [[2000, 1000], [2000, 4000]])

    def test_stale_node_contribution_drops_from_service_sum(self):
        metrics.record_samples(
            "node-a",
            {"cpuPercent": 5},
            [{"service": "web_web", "containerId": "aaa", "cpuPercent": 10, "memoryUsageBytes": 1000}],
            now=2000,
        )
        metrics.record_samples(
            "node-b",
            {"cpuPercent": 5},
            [{"service": "web_web", "containerId": "bbb", "cpuPercent": 20, "memoryUsageBytes": 3000}],
            now=2000,
        )
        later = 2000 + metrics.SERVICE_SCRATCH_TTL_SECONDS + 30
        metrics.record_samples(
            "node-b",
            {"cpuPercent": 5},
            [{"service": "web_web", "containerId": "bbb", "cpuPercent": 20, "memoryUsageBytes": 3000}],
            now=later,
        )
        svc = metrics.load_history("service", "web_web")
        # node-a's contribution aged out, so the latest sum is node-b alone.
        self.assertEqual(svc["cpuPercent"][-1], [later, 20.0])
        self.assertEqual(svc["memoryUsageBytes"][-1], [later, 3000])

    def test_window_filters_old_points(self):
        for i in range(5):
            metrics.record_samples("cn-edge", {"cpuPercent": float(i)}, [], now=1000 + i * 30)
        windowed = metrics.load_history("node", "cn-edge", window=10, now=10_000)
        self.assertEqual(windowed.get("cpuPercent"), [])

    def test_corrupt_history_file_recovers_gracefully(self):
        metrics.record_samples("cn-edge", {"cpuPercent": 1.0}, [], now=1000)
        metrics.history_path().write_text("{bad json", encoding="utf-8")
        # Reading must not raise; a corrupt file resets to empty.
        self.assertEqual(metrics.load_history("node", "cn-edge"), {})
        # And a subsequent write rebuilds cleanly.
        metrics.record_samples("cn-edge", {"cpuPercent": 2.0}, [], now=1030)
        self.assertEqual(metrics.load_history("node", "cn-edge")["cpuPercent"], [[1030, 2.0]])

    def test_unknown_kind_or_missing_name_returns_empty(self):
        metrics.record_samples("cn-edge", {"cpuPercent": 1.0}, [], now=1000)
        self.assertEqual(metrics.load_history("bogus", "cn-edge"), {})
        self.assertEqual(metrics.load_history("node", ""), {})
        self.assertEqual(metrics.load_history("node", "does-not-exist"), {})

    def test_max_points_env_is_clamped(self):
        with patch.dict(os.environ, {"LUMA_METRICS_HISTORY_POINTS": "3"}, clear=False):
            self.assertEqual(metrics.max_points(), metrics.MIN_MAX_POINTS)
        with patch.dict(os.environ, {"LUMA_METRICS_HISTORY_POINTS": "99999999"}, clear=False):
            self.assertEqual(metrics.max_points(), metrics.MAX_MAX_POINTS)
        with patch.dict(os.environ, {"LUMA_METRICS_HISTORY_POINTS": "not-a-number"}, clear=False):
            self.assertEqual(metrics.max_points(), metrics.DEFAULT_MAX_POINTS)


class SustainedBreachTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": self._tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _fill(self, values, *, base=10_000, step=30):
        for i, value in enumerate(values):
            metrics.record_samples("cn-edge", {"memoryUsedPercent": float(value)}, [], now=base + i * step)
        return base + (len(values) - 1) * step

    def test_returns_peak_when_breach_is_sustained(self):
        last = self._fill([88, 90, 92, 89, 91, 90, 93, 90, 88, 90, 91])  # 11 pts over 300s, all >=85
        peak = metrics.sustained_breach("node", "cn-edge", "memoryUsedPercent", threshold=85, duration_seconds=300, now=last)
        self.assertEqual(peak, 93.0)

    def test_single_spike_does_not_alert(self):
        # Mostly calm with a couple of high readings; current value high but
        # majority below threshold -> not sustained.
        last = self._fill([50, 50, 50, 50, 50, 50, 50, 50, 50, 90, 90])
        self.assertIsNone(metrics.sustained_breach("node", "cn-edge", "memoryUsedPercent", threshold=85, duration_seconds=300, now=last))

    def test_already_recovered_does_not_alert(self):
        # High for most of the window but the latest sample dropped back.
        last = self._fill([90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 50])
        self.assertIsNone(metrics.sustained_breach("node", "cn-edge", "memoryUsedPercent", threshold=85, duration_seconds=300, now=last))

    def test_too_few_points_does_not_alert(self):
        last = self._fill([90, 92])
        self.assertIsNone(metrics.sustained_breach("node", "cn-edge", "memoryUsedPercent", threshold=85, duration_seconds=300, now=last))

    def test_insufficient_time_span_does_not_alert(self):
        # Three high points but only spanning 60s of a 300s window (e.g. a node
        # that just came up hot) -> not yet "sustained".
        last = self._fill([90, 92, 91], step=30)  # spans 60s < 300*0.6
        self.assertIsNone(metrics.sustained_breach("node", "cn-edge", "memoryUsedPercent", threshold=85, duration_seconds=300, now=last))


class MetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.state_dir)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.state = init_state(domain="luma.example.com")
        self.token = self.state["deployToken"]

    def test_handle_metrics_history_returns_series_structure(self):
        from luma.control.server import handle_metrics_history

        # Record at real time so the endpoint's real-clock window keeps it.
        metrics.record_samples("cn-edge", {"cpuPercent": 12.0, "memoryUsedPercent": 40.0}, [])
        result = handle_metrics_history(self.token, "node", "cn-edge", window=3600)
        self.assertEqual(result["kind"], "node")
        self.assertEqual(result["name"], "cn-edge")
        self.assertIn("cpuPercent", result["series"])
        self.assertEqual(result["series"]["cpuPercent"][-1][1], 12.0)
        self.assertIn("updatedAt", result)

    def test_handle_metrics_history_rejects_bad_kind_and_token(self):
        from luma.control.server import handle_metrics_history

        with self.assertRaises(LumaError):
            handle_metrics_history(self.token, "bogus", "cn-edge")
        with self.assertRaises(LumaError):
            handle_metrics_history(self.token, "node", "")
        with self.assertRaises(LumaError):
            handle_metrics_history("wrong-token", "node", "cn-edge")


if __name__ == "__main__":
    unittest.main()
