"""Real Go Agent against Python Control, using only temporary host directories."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("go") and shutil.which("openssl"), "Go and openssl required")
class GoAgentContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.binary = Path(cls.temporary.name) / "luma-agent"
        subprocess.run(["go", "build", "-o", str(cls.binary), "./cmd/luma-agent"], cwd=ROOT, capture_output=True, check=True)

    def test_terminal_response_loss_does_not_replay_host_mutation(self):
        self.exercise_agent(crash=False)

    def test_crash_recovers_receipt_before_idle_lease(self):
        self.exercise_agent(crash=True)

    def exercise_agent(self, *, crash):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from luma.control import server as control
        from luma.control.state import init_state, load_state, save_state

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "luma.yaml"
            config.write_text("providers: {}\n")
            cert, key = root / "cert.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)], capture_output=True, check=True)
            with patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / "state"), LUMA_CONTROL_CONFIG=str(config)):
                state = init_state(domain="fixture.test", cluster_id="fixture", overwrite=True)
                state["nodes"] = {"worker": {"name": "worker", "displayName": "worker", "nodeId": "node-1", "region": "cn"}}
                token = control._issue_node_agent_token(state, "worker", node_id="node-1")
                state["agentTasks"] = {
                    "a-ungated": {"id": "a-ungated", "nodeName": "worker", "status": "queued", "action": "future-ungated-action",
                                  "payload": {"registry": "fixture.invalid:5000"}},
                    "a-unsupported": {"id": "a-unsupported", "nodeName": "worker", "status": "queued", "action": "future-unimplemented-action",
                                      "requiredCapability": "future-unimplemented-capability", "payload": {"name": "unused", "path": str(root / "not-created")}},
                    "b-volume": {"id": "b-volume", "nodeName": "worker", "status": "queued", "action": "prepare-managed-volume-path",
                                 "requiredCapability": "managed-volume-path", "payload": {"root": str(root / "volumes"), "relative": "app/data", "unusedCredential": "ephemeral-test-secret"}},
                }
                save_state(state)
                received = threading.Event()
                requests, reports = [], []
                failures = {"active": crash, "first": True}
                target = root / "volumes/app/data"

                async def endpoint(request):
                    body = await request.json()
                    if request.url.path.endswith("/lease"):
                        self.assertIn("activeTaskId", body, "idle lease must explicitly prove the serial agent is idle")
                        self.assertEqual(body["activeTaskId"], "")
                    requests.append((request.url.path, body))
                    if request.url.path.endswith("/complete"):
                        reports.append(body)
                        if failures["active"]:
                            received.set()
                            return JSONResponse({"error": "temporary outage"}, status_code=503)
                        response = await control._asgi_authenticated_post(request)
                        if failures["first"] and not crash:
                            failures["first"] = False
                            # Re-executing preparation would undo this permission
                            # change. A completion retry must only resend the receipt.
                            target.chmod(0o700)
                            return JSONResponse({"error": "lost terminal response"}, status_code=503)
                        return response
                    return await control._asgi_authenticated_post(request)

                app = Starlette(routes=[Route("/v1/{path:path}", endpoint, methods=["POST"])])
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", ssl_keyfile=str(key), ssl_certfile=str(cert)))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                thread.start()
                process = None
                try:
                    deadline = time.monotonic() + 5
                    while not server.started and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(server.started)
                    agent_config = root / "agent.json"
                    agent_config.write_text(json.dumps({"endpoint": f"https://127.0.0.1:{sock.getsockname()[1]}", "token": token,
                                                        "nodeName": "worker", "nodeId": "node-1", "insecure": True, "busyHeartbeatIntervalSeconds": 0.02}))
                    agent_config.chmod(0o600)
                    argv = [str(self.binary), "--config", str(agent_config), "--once"]
                    receipt = Path(str(agent_config) + ".go-receipt.json")
                    if crash:
                        process = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        if not received.wait(15):
                            # Preserve actionable evidence when a loaded test
                            # host delays the TLS/SQLite round trip or the child
                            # exits early. Never include credentials/payloads.
                            status = process.poll()
                            if status is None:
                                process.kill()
                            _, stderr = process.communicate(timeout=5)
                            process = None
                            observed = [path for path, _ in requests][-10:]
                            self.fail(f"agent did not reach terminal reporting: exit={status}, paths={observed}, stderr={stderr}")
                        self.assertTrue(receipt.exists())
                        self.assertNotIn("ephemeral-test-secret", receipt.read_text())
                        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                        process.kill()
                        process.communicate(timeout=5)
                        process = None
                        target.chmod(0o700)
                        failures["active"] = False
                    completed = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=15)
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertNotIn(token, completed.stdout + completed.stderr)
                    self.assertNotIn("ephemeral-test-secret", completed.stdout + completed.stderr)
                    self.assertFalse(receipt.exists(), "acknowledged receipt was not cleared")
                    self.assertEqual(target.stat().st_mode & 0o777, 0o700, "host mutation was replayed")
                    self.assertEqual(sum(path.endswith("/lease") for path, _ in requests), 1)
                    self.assertGreaterEqual(len(reports), 2)
                    self.assertTrue(all(report == reports[0] for report in reports))
                    saved = load_state()
                    self.assertEqual(saved["agentTasks"]["b-volume"]["status"], "succeeded")
                    self.assertEqual(saved["agentTasks"]["a-unsupported"]["status"], "queued")
                    self.assertEqual(saved["agentTasks"]["a-ungated"]["status"], "queued")
                    self.assertFalse((root / "not-created").exists())
                    agent = saved["nodes"]["worker"]["agent"]
                    self.assertTrue(agent["joinVerified"])
                    self.assertIn("managed-volume-path", agent["capabilities"])
                    self.assertNotIn("future-unimplemented-capability", agent["capabilities"])
                    self.assertIn("prepare-managed-volume-path", agent["supportedActions"])
                    self.assertNotIn("future-ungated-action", agent["supportedActions"])
                    self.assertTrue(all(body["activeTaskId"] == "b-volume" for path, body in requests if path.endswith("/heartbeat")))
                finally:
                    if process is not None:
                        process.kill()
                        process.communicate(timeout=5)
                    server.should_exit = True
                    thread.join(timeout=5)
                    sock.close()
                    self.assertFalse(thread.is_alive())
