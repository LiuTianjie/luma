import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from luma.control import alerting, operations, server
from luma.control.state import init_state


class OperationsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = self.root / "luma.yaml"
        config.write_text("providers: {}\n")
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root / "state"), "LUMA_CONTROL_CONFIG": str(config)})
        env.start()
        self.addCleanup(env.stop)
        self.token = init_state(domain="isolated.example")["deployToken"]
        self.client = TestClient(server.create_app())

    def test_management_routes_and_channel_crud(self):
        def request(method, path, body=None, token=None):
            headers = {"Authorization": "Bearer " + (token or self.token), "Content-Type": "application/json"}
            response = self.client.request(method, path, json=body, headers=headers)
            return response.status_code, response.json()
        for path in ("/v1/alerting/overview", "/v1/alerting/presets", "/v1/governance/inventory", "/v1/governance/policy"):
            self.assertEqual(request("GET", path)[0], 200)
            self.assertEqual(request("GET", path, token="wrong")[0], 401)
        code, payload = request("POST", "/v1/alerting/channels", {
            "name": "Isolated test", "appId": "cli_isolated1234", "chatId": "oc_isolatedgroup1234",
            "enabled": True, "appSecret": "test-only-signing-secret",
        })
        self.assertEqual(code, 200, payload)
        self.assertNotIn("test-only-signing-secret", json.dumps(payload))
        # No test-send request: this exercises storage only.
        channels = request("GET", "/v1/alerting/channels")[1]["items"]
        identifier = channels[-1]["id"]
        self.assertEqual(request("DELETE", f"/v1/alerting/channels/{identifier}")[0], 200)

    def test_worker_evaluates_without_dashboard_and_delivery_is_independent(self):
        worker = operations.OperationsWorker(interval=1)
        with patch.object(alerting, "load_evaluation_state", return_value={"nodes": {}}) as snapshot, patch.object(alerting, "tick") as evaluate, patch.object(alerting, "deliver_pending"):
            worker.start()
            deadline = time.monotonic() + 3
            while not evaluate.called and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.close()
            snapshot.assert_called()
            evaluate.assert_called()
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(worker.delivery_thread.is_alive())
