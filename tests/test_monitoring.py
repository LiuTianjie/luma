import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.testclient import TestClient

from luma import agent
from luma.control import database, monitoring, server
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


class MonitoringTests(unittest.TestCase):
    def test_freshness_does_not_turn_old_metrics_into_fresh_samples(self):
        state = {"nodes": {"one": {"region": "cn", "agent": {
            "lastSeen": 990, "metricsCollectedAt": 800, "metrics": {"cpuPercent": 88},
            "containerStatsCollectedAt": 800,
            "containerStats": [{"service": "web", "cpuPercent": 100}],
        }}}}
        text = monitoring.render_metrics(state, now=1000)
        self.assertIn('luma_node_agent_up{cluster="",node="one",region="cn"} 1', text)
        self.assertNotIn("luma_node_cpu_used_ratio", text)
        self.assertNotIn("luma_service_cpu_cores", text)
        stale = monitoring.render_metrics(state, now=1200)
        self.assertIn('luma_node_agent_up{cluster="",node="one",region="cn"} 0', stale)

    def test_units_and_no_darwin_cpu_guess(self):
        state = {"nodes": {"node": {"agent": {
            "lastSeen": 995, "os": "darwin", "metrics": {"cpuPercent": 50, "load1": 2, "diskUsedPercent": 92, "metricsPath": "/data"},
            "containerStats": [{"service": "web", "cpuPercent": 200, "memoryUsageBytes": 100}, {"service": "web", "cpuPercent": 50, "memoryUsageBytes": 50}],
        }}}}
        text = monitoring.render_metrics(state, now=1000)
        self.assertNotIn("luma_node_cpu_used_ratio", text)
        self.assertIn('luma_node_filesystem_used_ratio{cluster="",node="node",path="/data",region=""} 0.92', text)
        self.assertIn('luma_service_cpu_cores{cluster="",node="node",region="",service="web",task=""} 2.5', text)
        self.assertIn('luma_service_memory_bytes{cluster="",node="node",region="",service="web",task=""} 150', text)

    def test_compose_task_identity_and_missing_job_are_explicit(self):
        state = {"nodes": {"node": {"agent": {"lastSeen": 999, "containerStats": [
            {"service": "shop", "nomadTask": "web", "cpuPercent": 50},
            {"service": "shop", "nomadTask": "db", "cpuPercent": 100},
            {"service": "nomad:allocation", "nomadTask": "web", "cpuPercent": 50},
        ]}}}}
        text = monitoring.render_metrics(state, now=1000, compose_jobs={"shop"})
        self.assertIn('service="shop_web",task="web"} 0.5', text)
        self.assertIn('service="shop_db",task="db"} 1', text)
        self.assertNotIn('service="shop"', text)
        self.assertNotIn("nomad:allocation", text)
        self.assertIn('luma_node_unresolved_containers{cluster="",node="node",region=""} 1', text)

    def test_label_escaping_and_no_secrets_or_nonfinite_values(self):
        text = monitoring.render_metrics({"deployToken": "never-show", "nodes": {'a"\nb': {"agent": {"lastSeen": 999, "metrics": {"cpuPercent": float("nan"), "memoryTotalBytes": float("inf")}}}}}, now=1000)
        self.assertIn('node="a\\"\\nb"', text)
        self.assertNotIn("never-show", text)
        self.assertNotIn("luma_node_cpu_used_ratio", text)
        self.assertNotIn("luma_node_memory_total_bytes", text)

    def test_queue_metrics_are_current_gauges(self):
        text = monitoring.render_metrics({"agentTasks": {"x": {"status": "queued", "createdAt": 900}, "y": {"status": "succeeded", "createdAt": 500}}}, now=1000)
        self.assertIn('luma_tasks{cluster="",kind="agent",status="queued"} 1', text)
        self.assertIn('luma_task_queue_oldest_age_seconds{cluster="",kind="agent",status="queued"} 100', text)

    def test_disk_collection_uses_selected_filesystem_and_reserved_blocks(self):
        usage = SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=100, f_bfree=20, f_bavail=10, f_files=1000, f_ffree=100)
        with patch.dict(os.environ, {"LUMA_METRICS_DISK_PATH": "/data"}), patch.object(os, "statvfs", return_value=usage) as call:
            metrics = agent._filesystem_metrics()
        call.assert_called_once_with(Path("/data"))
        self.assertEqual(metrics["diskUsedPercent"], 88.9)
        self.assertEqual(metrics["diskAvailableBytes"], 40960)
        self.assertEqual(metrics["inodesUsedPercent"], 90)
        self.assertEqual(metrics["metricsPath"], "/data")

    def test_missing_disk_or_inode_support_is_not_reported_as_zero_usage(self):
        with patch.object(os, "statvfs", side_effect=OSError()):
            self.assertEqual(agent._filesystem_metrics(), {})
        usage = SimpleNamespace(f_frsize=1, f_bsize=1, f_blocks=100, f_bfree=80, f_bavail=80, f_files=0, f_ffree=0)
        with patch.object(os, "statvfs", return_value=usage):
            self.assertNotIn("inodesUsedPercent", agent._filesystem_metrics())

    def test_server_omits_nonfinite_agent_samples(self):
        values = server._agent_metrics({"cpuPercent": "nan", "diskUsedPercent": "inf", "inodesUsedPercent": -1, "diskAvailableBytes": 0, "metricsPath": "/data"})
        self.assertEqual(values, {"diskAvailableBytes": 0, "metricsPath": "/data"})


class MonitoringEndpointTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.token = "m" * 48
        self.token_file = self.root / "metrics-token"
        self.token_file.write_text(self.token + "\n")
        self.token_file.chmod(0o600)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root), "LUMA_METRICS_TOKEN_FILE": str(self.token_file)})
        env.start()
        self.addCleanup(env.stop)
        self.state = init_state(domain="example.com")

    def test_dedicated_token_asgi_cannot_access_management_and_scrape_is_read_only(self):
        with patch.object(server, "_reconcile_orphaned_build_runs_after_control_restart"), patch.object(server.operations_api.OperationsWorker, "start"), TestClient(server.create_app()) as client:
            before = database.database_path().read_bytes()
            response = client.get("/v1/metrics", headers={"Authorization": "Bearer " + self.token})
            self.assertEqual(response.status_code, 200)
            self.assertIn("version=0.0.4", response.headers["content-type"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("luma_control_info", response.text)
            self.assertEqual(database.database_path().read_bytes(), before)
            self.assertEqual(client.get("/v1/dashboard", headers={"Authorization": "Bearer " + self.token}).status_code, 401)
            self.assertEqual(client.get("/v1/metrics", headers={"Authorization": "Bearer invalid"}).status_code, 401)
            self.assertEqual(client.get("/v1/metrics", headers={"Authorization": "Bearer " + self.state["deployToken"]}).status_code, 200)

    def test_legacy_http_endpoint_has_same_auth_and_content(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{httpd.server_port}/v1/metrics"
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"luma_control_info", response.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)

    def test_invalid_token_file_fails_closed(self):
        self.token_file.chmod(0o644)
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            monitoring.require_metrics_token(self.state, self.token)
        self.token_file.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.token_file)
        with patch.dict(os.environ, {"LUMA_METRICS_TOKEN_FILE": str(link)}), self.assertRaisesRegex(LumaError, "unauthorized"):
            monitoring.require_metrics_token(self.state, self.token)
        with self.assertRaisesRegex(LumaError, "unauthorized"):
            monitoring.require_metrics_token(self.state, "无效")


if __name__ == "__main__":
    unittest.main()
