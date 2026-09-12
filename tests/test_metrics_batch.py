import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from luma.control import metrics, metrics_api
from luma.control.state import init_state
from luma.errors import LumaError


class MetricsBatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": tmp.name, "LUMA_METRICS_HISTORY_POINTS": "60"})
        env.start()
        self.addCleanup(env.stop)
        self.token = init_state(domain="luma.example.com")["deployToken"]

    def test_batch_reads_one_snapshot_and_compacts_each_distinct_target_once(self):
        metrics.record_samples("edge", {"cpuPercent": 12}, [{"service": "app_web", "cpuPercent": 4}], now=3000)
        targets = [{"kind": "node", "name": "edge"}, {"kind": "service", "name": "app_web"}, {"kind": "node", "name": "edge"}]
        with patch.object(metrics_api.time, "time", return_value=3030), \
                patch.object(metrics, "_load_raw", wraps=metrics._load_raw) as read, \
                patch.object(metrics_api, "load_history", wraps=metrics_api.load_history) as load, \
                patch.object(metrics_api, "load_auth_state", wraps=metrics_api.load_auth_state) as auth:
            result = metrics_api.handle_metrics_history_batch(self.token, {"targets": targets, "window": 86400})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(auth.call_count, 1)
        self.assertEqual(load.call_count, 2)
        self.assertIs(load.call_args_list[0].kwargs["snapshot"], load.call_args_list[1].kwargs["snapshot"])
        self.assertEqual(result["updatedAt"], 3030)
        self.assertEqual([item["name"] for item in result["results"]], ["edge", "app_web", "edge"])
        self.assertEqual(result["results"][0], result["results"][2])
        for item, value in zip(result["results"], [12, 4, 12]):
            payload = item["payload"]
            self.assertEqual(payload["series"]["cpuPercent"], [[3000, value]])
            self.assertEqual(payload["requestedWindow"], 86400)
            self.assertEqual(payload["window"], 1800)
            self.assertEqual(payload["updatedAt"], 3030)

    def test_invalid_targets_and_target_read_failure_preserve_other_results(self):
        targets = [
            {"kind": "bogus", "name": "edge"},
            {"kind": "node", "name": ""},
            None,
            {"kind": "service", "name": "broken"},
            {"kind": "service", "name": "missing"},
        ]
        def load(kind, name, **kwargs):
            if name == "broken":
                raise OSError("target unavailable")
            return {}
        with patch.object(metrics_api, "load_history", side_effect=load):
            result = metrics_api.handle_metrics_history_batch(self.token, {"targets": targets})
        self.assertEqual(len(result["results"]), 5)
        self.assertTrue(all("error" in item for item in result["results"][:4]))
        self.assertEqual(result["results"][3]["error"], "target unavailable")
        self.assertEqual(result["results"][4]["payload"]["series"], {})
        self.assertIsNone(result["results"][4]["payload"]["latestSampleAt"])

    def test_auth_and_invalid_batch_fail_before_history_is_read(self):
        target = {"kind": "node", "name": "edge"}
        with patch.object(metrics_api, "load_history_snapshot") as read:
            with self.assertRaisesRegex(LumaError, "unauthorized"):
                metrics_api.handle_metrics_history_batch("wrong-token", {"targets": [target]})
            for body in [None, {}, {"targets": "all"}, {"targets": [target] * 33}]:
                with self.subTest(body=body), self.assertRaises(LumaError):
                    metrics_api.handle_metrics_history_batch(self.token, body)
            for window in [True, None, {}, [], 1.5, "not-a-number"]:
                with self.subTest(window=window), self.assertRaisesRegex(LumaError, "window must be an integer"):
                    metrics_api.handle_metrics_history_batch(self.token, {"targets": [target], "window": window})
        read.assert_not_called()

    def test_empty_and_all_invalid_batches_skip_snapshot_and_limit_is_inclusive(self):
        with patch.object(metrics_api, "load_history_snapshot", return_value={}) as read:
            empty = metrics_api.handle_metrics_history_batch(self.token, {"targets": []})
            self.assertEqual(empty["results"], [])
            metrics_api.handle_metrics_history_batch(self.token, {"targets": [{"kind": "node", "name": "x" * 513}]})
            read.assert_not_called()
            result = metrics_api.handle_metrics_history_batch(self.token, {
                "targets": [{"kind": "node", "name": f"n{i}"} for i in range(32)], "window": -30,
            })
            self.assertEqual(len(result["results"]), 32)
            self.assertTrue(all(item["payload"]["window"] == 60 for item in result["results"]))
            read.assert_called_once()

    def test_batch_route_has_asgi_and_legacy_parity_including_auth_and_limits(self):
        from luma.control import server

        path = "/v1/dashboard/metrics/history/batch"
        for token, body, status in [
            (self.token, {"targets": [{"kind": "node", "name": "missing"}]}, 200),
            ("invalid", {"targets": []}, 401),
            (self.token, {"targets": [{}] * 33}, 400),
        ]:
            with self.subTest(status=status):
                handler = server.ControlHandler.__new__(server.ControlHandler)
                handler.path = path
                handler.headers = {"Authorization": f"Bearer {token}"}
                handler._read_json = Mock(return_value=body)
                handler._json = Mock()
                handler._error = Mock()
                handler.do_POST()
                received = handler._json if status == 200 else handler._error
                self.assertEqual(received.call_args.args[0], status)
                request = server.Request({"type": "http", "method": "POST", "path": path,
                    "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode())]})
                request._body = json.dumps(body).encode()
                response = asyncio.run(server._asgi_authenticated_post(request))
                self.assertEqual(response.status_code, status)
                if status == 200:
                    self.assertEqual(json.loads(response.body)["results"], handler._json.call_args.args[1]["results"])


if __name__ == "__main__":
    unittest.main()
