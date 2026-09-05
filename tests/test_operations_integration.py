import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from luma.control import alerting, operations, server
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError


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

    def test_management_routes_and_channel_crud_have_same_http_stack_contract(self):
        legacy = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlHandler)
        thread = threading.Thread(target=legacy.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(legacy.server_close)
        self.addCleanup(legacy.shutdown)
        def request(method, path, body=None, stack="asgi", token=None):
            headers = {"Authorization": "Bearer " + (token or self.token), "Content-Type": "application/json"}
            if stack == "asgi":
                response = self.client.request(method, path, json=body, headers=headers)
                return response.status_code, response.json()
            req = urllib.request.Request(f"http://127.0.0.1:{legacy.server_port}" + path,
                data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
            try:
                response = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        for stack in ("asgi", "legacy"):
            with self.subTest(stack=stack):
                for path in ("/v1/alerting/overview", "/v1/alerting/presets", "/v1/governance/inventory", "/v1/governance/policy"):
                    self.assertEqual(request("GET", path, stack=stack)[0], 200)
                    self.assertEqual(request("GET", path, stack=stack, token="wrong")[0], 401)
                code, payload = request("POST", "/v1/alerting/channels", {
                    "name": "Isolated test", "appId": "cli_isolated1234", "chatId": "oc_isolatedgroup1234",
                    "enabled": True, "appSecret": "test-only-signing-secret",
                }, stack=stack)
                self.assertEqual(code, 200, payload)
                self.assertNotIn("test-only-signing-secret", json.dumps(payload))
                # No test-send request: this exercises storage only.
                channels = request("GET", "/v1/alerting/channels", stack=stack)[1]["items"]
                identifier = channels[-1]["id"]
                self.assertEqual(request("DELETE", f"/v1/alerting/channels/{identifier}", stack=stack)[0], 200)

    def test_worker_evaluates_without_dashboard_and_delivery_is_independent(self):
        worker = operations.OperationsWorker(interval=1)
        with patch.object(alerting, "load_evaluation_state", return_value={"nodes": {}}) as snapshot, patch.object(alerting, "tick") as evaluate, patch.object(alerting, "deliver_pending") as deliveries:
            worker.start()
            deadline = time.monotonic() + 3
            while not evaluate.called and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.close()
            snapshot.assert_called()
            evaluate.assert_called()
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(worker.delivery_thread.is_alive())

    def test_cleanup_request_cannot_supply_paths_or_reference_claims(self):
        for extra in ({"root": "/tmp"}, {"references": {"coverageComplete": True}}):
            response = self.client.post("/v1/governance/builder", json={"node": "builder", "operation": "preview", **extra}, headers={"Authorization": "Bearer " + self.token})
            self.assertEqual(response.status_code, 400)
        with self.assertRaises(LumaError):
            server.handle_builder_storage_request(self.token, {"node": "builder", "operation": "purge", "planId": "a" * 32})

    def test_cleanup_task_retention_survives_grace_period(self):
        now = int(time.time())
        state = load_state()
        state["agentTasks"] = {"cleanup": {"id": "cleanup", "action": "builder-storage", "status": "succeeded", "completedAt": now - 2 * 86400},
            "ordinary": {"id": "ordinary", "status": "succeeded", "completedAt": now - 2 * 86400}}
        server._prune_agent_tasks(state, now=now)
        self.assertIn("cleanup", state["agentTasks"])
        self.assertNotIn("ordinary", state["agentTasks"])
