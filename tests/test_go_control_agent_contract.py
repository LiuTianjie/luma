"""Real Go Control + Go Agent over TLS, with temporary SQLite and host paths."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import patch

from luma.control import database
from luma.control.state import init_state, load_state, save_state
from luma.control.workflows import handle_workflow_check, handle_workflow_get, handle_workflow_record
from luma.deploy_workflow import validate_recipe
from luma.errors import LumaError

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fixture-agent-token"
MANAGEMENT = "fixture-management-token"
ACTION = "prepare-managed-volume-path"


@unittest.skipUnless(shutil.which("go") and shutil.which("openssl"), "Go and openssl required")
class GoControlAgentContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.built.cleanup)
        root = Path(cls.built.name)
        cls.control, cls.agent, cls.cli = root / "luma-control", root / "luma-agent", root / "luma"
        for name, binary in (("luma-control", cls.control), ("luma-agent", cls.agent), ("luma", cls.cli)):
            subprocess.run(["go", "build", "-o", str(binary), "./cmd/" + name], cwd=ROOT, capture_output=True, check=True)
        cls.cert, cls.key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", str(cls.key), "-out", str(cls.cert)],
                       capture_output=True, check=True)
        cls.client_tls = ssl._create_unverified_context()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "state"
        self.env = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(self.directory))
        self.env.start()
        self.addCleanup(self.env.stop)
        state = init_state(domain="fixture.test", cluster_id="fixture")
        state.update(deployToken=MANAGEMENT, joinToken="fixture-join")
        state["nodes"] = {
            "worker": {"name": "worker", "displayName": "Node One", "nodeId": "node-1", "aliases": ["old-worker"],
                       "labels": {"custom": "keep"}, "customMetadata": {"keep": True},
                       "agent": {"tokenHash": hashlib.sha256(TOKEN.encode()).hexdigest(), "knownNodeIds": ["node-1"], "customAgentField": "keep"}},
            "other": {"name": "other", "nodeId": "node-2", "agent": {"tokenHash": hashlib.sha256(b"other-agent").hexdigest()}},
        }
        state["agentTasks"] = {"task-1": self.task("task-1")}
        state["registries"] = {"private.test": {"username": "fixture-user", "password": "fixture-registry-secret"}}
        save_state(state)
        self.process = None
        self.addCleanup(self.stop)
        self.presence = {"nodeName": "worker", "nodeId": "node-1", "os": "linux", "arch": "arm64",
                         "version": "fixture-version", "capabilities": ["managed-volume-path"],
                         "supportedActions": [ACTION], "activeTaskId": "", "waitSeconds": 0}

    def task(self, task_id, **overrides):
        return {"id": task_id, "nodeName": "worker", "action": ACTION, "status": "queued",
                "requiredCapability": "managed-volume-path", "createdAt": 1, "updatedAt": 1,
                "payload": {"root": str(self.root / "volumes"), "relative": "app/data"},
                "progress": [], "progressOffset": 0, "customTaskField": "keep", **overrides}

    def argv(self, mode="--agent-tasks"):
        return [str(self.control), "--state-dir", str(self.directory), mode, "--listen", "127.0.0.1:0",
                "--tls-cert", str(self.cert), "--tls-key", str(self.key)]

    def start(self, mode="--agent-tasks"):
        self.process = subprocess.Popen(self.argv(mode), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.process.stdout], [], [], 10)
        self.assertTrue(ready, "Control did not report readiness")
        line = self.process.stdout.readline().strip()
        if "listening on " not in line:
            _, stderr = self.process.communicate(timeout=5)
            self.fail("Control startup failed: " + stderr)
        self.endpoint = "https://" + line.split("listening on ", 1)[1]

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=5)
                raise
            self.process = None

    def request(self, path, body, token=TOKEN):
        request = Request(self.endpoint + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            response = urlopen(request, context=self.client_tls, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def stored(self, kind, record_id):
        # Read-only inspection is permitted while the Go writer holds its lock.
        conn = sqlite3.connect((self.directory / "control.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            return database.read_entity(conn, kind, record_id)
        finally:
            conn.close()

    def lease(self, **overrides):
        return self.request("/v1/node-agent/lease", {**self.presence, **overrides})

    def complete(self, **overrides):
        return self.request("/v1/node-agent/tasks/complete", {
            "nodeName": "worker", "nodeId": "node-1", "taskId": "task-1", "status": "succeeded",
            "message": "done", "result": {"proof": "first"}, **overrides})

    def test_serial_claim_auth_and_capability_gates(self):
        state = load_state()
        state["agentTasks"].update({
            "a-nfs": self.task("a-nfs", action="prepare-nfs-export"),
            "b-linked": self.task("b-linked", buildRunId="future-parent"),
            "c-capability": self.task("c-capability", requiredCapability="not-present"),
            "task-2": self.task("task-2"),
        })
        save_state(state)
        self.start()
        for token in ("bad", MANAGEMENT, "fixture-join", "other-agent"):
            self.assertEqual(self.request("/v1/node-agent/lease", self.presence, token)[0], 401)
        self.assertEqual(self.lease(nodeId="node-2")[0], 401)
        self.assertEqual(self.lease(supportedActions=[]), (200, {"task": None}))
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.lease(), range(8)))
        self.assertTrue(all(status == 200 for status, _ in responses), responses)
        leased = [body["task"] for _, body in responses if body["task"] is not None]
        self.assertEqual([task["id"] for task in leased], ["task-1"])
        self.assertEqual(self.stored("agentTasks", "a-nfs")["status"], "queued")
        self.assertEqual(self.stored("agentTasks", "b-linked")["status"], "queued")
        self.assertEqual(self.complete(), (200, {"taskId": "task-1", "status": "succeeded"}))
        self.assertEqual(self.lease()[1]["task"]["id"], "task-2")

    def test_progress_terminal_idempotency_and_python_readback(self):
        self.start()
        # Completion must not turn a never-leased task into an apparent success.
        self.assertEqual(self.complete(version="must-rollback")[0], 409)
        self.assertNotEqual(self.stored("nodes", "worker")["agent"].get("version"), "must-rollback")
        self.assertEqual(self.lease()[0], 200)
        for batch in range(7):
            body = {"nodeName": "old-worker", "nodeId": "node-1", "taskId": "task-1",
                    "events": [{"type": "output", "line": f"event-{batch * 50 + i}", "ts": 1} for i in range(50)]}
            self.assertEqual(self.request("/v1/node-agent/tasks/progress", body), (200, {"taskId": "task-1", "events": 50}))
        task = self.stored("agentTasks", "task-1")
        self.assertEqual(len(task["progress"]), 300)
        self.assertEqual(task["progressOffset"], 50)
        self.assertEqual(task["progress"][0]["line"], "event-50")
        self.assertEqual(self.complete()[0], 200)
        original = self.stored("agentTasks", "task-1")
        self.assertEqual(self.complete(status="failed", result={"proof": "later"}), (200, {"taskId": "task-1", "status": "succeeded"}))
        self.assertEqual(self.stored("agentTasks", "task-1"), original)
        self.assertEqual(self.request("/v1/node-agent/tasks/progress", body)[1]["events"], 0)
        self.assertEqual(self.stored("nodes", "worker")["agent"]["supportedActions"], [ACTION])
        self.stop()
        state = load_state()
        self.assertEqual(state["agentTasks"]["task-1"], original)
        self.assertEqual(state["nodes"]["worker"]["customMetadata"], {"keep": True})
        self.assertEqual(state["nodes"]["worker"]["agent"]["customAgentField"], "keep")
        self.assertEqual(original["customTaskField"], "keep")

    def test_restart_reconciliation_cancellation_and_foreign_tasks(self):
        state = load_state()
        old = int(time.time()) - 90
        state["agentTasks"] = {
            "task-1": self.task("task-1", status="running", leasedAt=old),
            "canceled": self.task("canceled", status="running", leasedAt=old, cancelRequestedAt=old),
            "foreign": self.task("foreign", nodeName="other", status="running", leasedAt=old),
            "active": self.task("active", status="running", leasedAt=old, cancelRequestedAt=old),
        }
        save_state(state)
        self.start()
        status, body = self.request("/v1/node-agent/heartbeat", {**self.presence, "activeTaskId": "active"})
        self.assertEqual((status, body["cancelRequested"]), (200, True))
        self.assertEqual(self.stored("agentTasks", "task-1")["status"], "failed")
        self.assertEqual(self.stored("agentTasks", "canceled")["status"], "canceled")
        self.assertEqual(self.stored("agentTasks", "foreign")["status"], "running")
        self.assertEqual(self.complete()[1]["status"], "failed", "late completion rewrote reconciled decision")
        self.assertEqual(self.complete(taskId="foreign")[0], 404)
        self.assertEqual(self.request("/v1/node-agent/tasks/progress", {"nodeName": "worker", "nodeId": "node-1", "taskId": "foreign", "events": []})[0], 404)
        self.assertEqual(self.request("/v1/node-agent/tasks/progress", {"nodeName": "worker", "nodeId": "node-1", "taskId": "active", "events": []}, "bad")[0], 401)

    def test_registry_credentials_are_only_in_lease_response(self):
        state = load_state()
        state["agentTasks"]["task-1"].update(action="resolve-docker-image", requiredCapability="",
                                             payload={"image": "private.test/app:1"})
        save_state(state)
        self.start()
        status, body = self.lease(supportedActions=["resolve-docker-image"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["task"]["payload"]["registryAuth"]["password"], "fixture-registry-secret")
        task = self.stored("agentTasks", "task-1")
        self.assertEqual(task["payload"], {"image": "private.test/app:1"})
        self.assertNotIn("fixture-registry-secret", json.dumps(task))

    def test_writer_lock_excludes_python_and_other_go_writer(self):
        connection = database.connect()
        try:
            rejected = subprocess.run(self.argv(), capture_output=True, text=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("another Python or Go writer", rejected.stderr)
        finally:
            connection.close()
        self.start()
        with self.assertRaisesRegex(LumaError, "Go writer"):
            load_state()
        rejected = subprocess.run(self.argv(), capture_output=True, text=True, timeout=10)
        self.assertNotEqual(rejected.returncode, 0)
        self.stop()
        self.assertEqual(load_state()["clusterId"], "fixture")

    def test_real_agent_recovers_lost_ack_after_both_processes_restart(self):
        self.start()
        target = self.root / "volumes/app/data"
        terminal = threading.Event()
        failure = {"active": True}
        paths = []
        owner = self

        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                paths.append(self.path)
                request = Request(owner.endpoint + self.path, data=payload,
                                  headers={"Authorization": self.headers["Authorization"], "Content-Type": "application/json"})
                try:
                    try:
                        response = urlopen(request, context=owner.client_tls, timeout=5)
                    except HTTPError as exc:
                        response = exc
                    with response:
                        status, raw = response.status, response.read()
                except (URLError, ConnectionResetError, ssl.SSLError):
                    if not terminal.is_set():
                        raise
                    # An in-flight heartbeat can race the intentional Control
                    # restart after its terminal result has already committed.
                    status, raw = 503, b'{"error":"injected Control restart"}'
                if self.path.endswith("/complete") and status == 200 and failure["active"]:
                    target.chmod(0o700)
                    terminal.set()
                    status, raw = 503, b'{"error":"lost acknowledgement"}'
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    # The test intentionally SIGKILLs a client after Control has
                    # committed but before its acknowledgement is delivered.
                    pass

        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert, self.key)
        proxy.socket = tls.wrap_socket(proxy.socket, server_side=True)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        agent = None
        try:
            config = self.root / "agent.json"
            config.write_text(json.dumps({"endpoint": f"https://127.0.0.1:{proxy.server_port}", "token": TOKEN,
                                          "nodeName": "worker", "nodeId": "node-1", "insecure": True,
                                          "busyHeartbeatIntervalSeconds": 0.05}))
            config.chmod(0o600)
            argv = [str(self.agent), "--config", str(config), "--once"]
            agent = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertTrue(terminal.wait(15), "Agent did not reach durable terminal acknowledgement")
            receipt = Path(str(config) + ".go-receipt.json")
            self.assertTrue(receipt.exists())
            agent.kill()
            agent.communicate(timeout=5)
            agent = None
            original = self.stored("agentTasks", "task-1")
            self.assertEqual(original["status"], "succeeded")
            self.stop()
            self.start()
            failure["active"] = False
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse(receipt.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o700, "host operation was replayed")
            self.assertEqual(self.stored("agentTasks", "task-1"), original)
            self.assertEqual(sum(path.endswith("/lease") for path in paths), 1)
            self.assertIn("activeTaskObservedAt", self.stored("nodes", "worker")["agent"])
        finally:
            if agent is not None:
                agent.kill()
                agent.communicate(timeout=5)
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)
    def test_missing_active_observation_does_not_fail_or_reassign_task(self):
        state = load_state()
        state["agentTasks"]["task-1"].update(status="running", leasedAt=int(time.time()) - 90)
        state["nodes"]["worker"]["agent"].update(activeTaskId="task-1", activeTaskObservedAt=10)
        save_state(state)
        self.start()
        body = {key: value for key, value in self.presence.items() if key != "activeTaskId"}
        for active_value in ("omitted", None):
            with self.subTest(active_value=active_value):
                if active_value is None:
                    body["activeTaskId"] = None
                status, reply = self.request("/v1/node-agent/heartbeat", body)
                self.assertEqual((status, reply["activeTaskId"]), (200, "task-1"))
                self.assertEqual(self.request("/v1/node-agent/lease", body), (200, {"task": None}))
                self.assertEqual(self.stored("agentTasks", "task-1")["status"], "running")
                self.assertEqual(self.stored("nodes", "worker")["agent"]["activeTaskObservedAt"], 10)

    @staticmethod
    def recipe(method, *argv, **extra):
        return {"schemaVersion": 1, "method": method, "argv": list(argv), **extra}

    def test_workflow_recipe_and_check_match_python(self):
        cases = [
            self.recipe("image-deploy", "--no-env", "deploy", "./app.yml", "--skip-dns", "--timeout", "400"),
            self.recipe("image-deploy", "--env-file", ".env", "deploy", "./configs//app.yml", "--env", "./prod.env"),
            self.recipe("compose-deploy", "compose", "deploy", "compose.yml", "--engine", "nomad", "--secrets-env-file", "prod.env"),
            self.recipe("remote-build", "import", "https://user:password@git.test/acme/repo.git?token=hidden#frag", "--ref", "main"),
            self.recipe("remote-build", "import", "--provider-id", "github:personal", "--repository", "acme/repo"),
            self.recipe("remote-build", "import", "--provider-id", "gitea:work", "--repository", "acme/repo", "--compose-sidecar", "b.yml"),
            self.recipe("remote-build", "import", "acme/repo", "--context", ".", "--dockerfile", "Dockerfile", "--proxy-mode", "auto", "--port", "8000"),
            self.recipe("remote-build", "import", "git@git.test:acme/repo.git", "--manifest", "dir/../app.yml"),
            self.recipe("local-build", "build", "local"),
            self.recipe("local-build", "build", "local", "./checkout", "--repo-url", "https://git.test/acme/repo.git", "--proxy", "https://user:secret@proxy.test:443?q=hidden"),
            self.recipe("remote-build", "build", "retry", "run-1", "--env", "prod.env", retryParameters={"method": "remote-build", "repo": "acme/repo", "port": 8080, "manifest": "sha256:fixture"}),
        ]
        state = load_state()
        state["gitProviders"] = {"gitea:work": {"baseUrl": "https://git.test", "token": "never-return"}}
        save_state(state)
        expected = []
        for index, recipe in enumerate(cases):
            app = f"app-{index}"
            handle_workflow_record(MANAGEMENT, {"name": app, "recipe": recipe})
            request = {"recipe": recipe, "selector": {"name": app}}
            expected.append((request, handle_workflow_check(MANAGEMENT, request)))
        self.start()
        for request, wanted in expected:
            with self.subTest(name=request["selector"]["name"]):
                status, result = self.request("/v1/workflows/check", request, MANAGEMENT)
                self.assertEqual(status, 200, result)
                self.assertEqual(result, wanted)
                status, result = self.request("/v1/workflows", {"name": request["selector"]["name"], "recipe": request["recipe"]}, MANAGEMENT)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["workflow"]["recipe"], validate_recipe(request["recipe"]))
        self.stop()
        self.assertNotIn("never-return", json.dumps(load_state()["deploymentWorkflows"]))

    def test_workflow_monorepo_matching_and_repository_equivalence(self):
        for app in ("a", "b"):
            handle_workflow_record(MANAGEMENT, {"name": app, "recipe": self.recipe("remote-build", "import", "--provider-id", "github:me", "--repository", "acme/repo", "--compose-sidecar", app + ".yml")})
        requests = [
            {"recipe": self.recipe("remote-build", "import", "https://github.com/acme/repo.git", "--compose-sidecar", "b.yml")},
            {"recipe": self.recipe("remote-build", "import", "acme/repo", "--compose-sidecar", "b.yml", "--ref", "dev")},
            {"recipe": self.recipe("image-deploy", "deploy", "new.yml"), "selector": {"name": "new", "repoUrl": "acme/repo"}},
            {"recipe": self.recipe("compose-deploy", "compose", "deploy", "b.yml"), "selector": {"name": "b", "repoUrl": "acme/repo"}},
        ]
        expected = [handle_workflow_check(MANAGEMENT, r) for r in requests]
        self.start()
        for request, wanted in zip(requests, expected):
            self.assertEqual(self.request("/v1/workflows/check", request, MANAGEMENT), (200, wanted))
        self.assertEqual(self.request("/v1/workflows/check", {"recipe": self.recipe("remote-build", "import", "acme/repo")}, MANAGEMENT)[0], 400)

    def test_workflow_cas_success_evidence_unknown_fields_and_restart(self):
        recipe = self.recipe("image-deploy", "deploy", "app.yml")
        first = handle_workflow_record(MANAGEMENT, {"name": "app", "recipe": recipe, "source": "cli-success", "note": "retained"})["workflow"]
        state = load_state()
        state["deploymentWorkflows"]["app"]["futureField"] = {"keep": True}
        state["unrelatedConfig"] = {"keep": True}
        save_state(state)
        self.start()
        body = {"name": "app", "recipe": self.recipe("compose-deploy", "compose", "deploy", "compose.yml"), "expectedRevision": 1}
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.request("/v1/workflows", body, MANAGEMENT), range(8)))
        self.assertEqual(sorted(status for status, _ in responses), [200] + [409] * 7)
        updated = next(result["workflow"] for status, result in responses if status == 200)
        self.assertEqual(updated["note"], "retained")
        self.assertEqual(updated["lastSuccess"], first["lastSuccess"])
        self.assertNotEqual(updated["recipe"], updated["lastSuccess"]["recipe"])
        success = {**body, "source": "cli-success", "expectedRevision": 2, "note": "", "evidence": {"image": "https://user:secret@registry.test/app?q=hidden", "buildId": "run-2", "revision": "sha", "token": "never-store"}}
        status, result = self.request("/v1/workflows", success, MANAGEMENT)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["workflow"]["lastSuccess"]["image"], "https://registry.test/app")
        self.assertNotIn("token", result["workflow"]["lastSuccess"])
        self.assertEqual(result["workflow"]["revision"], 3)
        self.assertEqual(result["workflow"]["note"], "")
        self.stop()
        saved = handle_workflow_get(MANAGEMENT, "app")["workflow"]
        self.assertEqual(saved["futureField"], {"keep": True})
        self.assertEqual(load_state()["unrelatedConfig"], {"keep": True})
        self.start()
        self.assertEqual(self.request("/v1/workflows/app", None, MANAGEMENT)[1]["workflow"]["revision"], 3)

    def test_workflow_validation_auth_readonly_and_cli(self):
        self.start("--read-only")
        recipe = self.recipe("image-deploy", "deploy", "app.yml")
        self.assertEqual(self.request("/v1/workflows/check", {"recipe": recipe}, MANAGEMENT)[1]["status"], "unrecorded")
        self.assertEqual(self.request("/v1/workflows", {"name": "app", "recipe": recipe}, MANAGEMENT)[0], 501)
        self.stop()
        self.start()
        for token in (TOKEN, "fixture-join", "bad"):
            self.assertEqual(self.request("/v1/workflows", None, token)[0], 401)
            self.assertEqual(self.request("/v1/workflows", {}, token)[0], 401)
        for extra in (["--control-url", "https://other.test"], ["--token", "secret"], ["--insecure"], ["--resolve-ip", "127.0.0.1"], ["--workflow-note", "nested"], ["--dry-run"], ["--skip-orchestrator"], ["--commit"], ["--accept-workflow-change"], ["--workflow-app", "other"], ["--help"]):
            with self.subTest(extra=extra):
                status, _ = self.request("/v1/workflows", {"name": "app", "recipe": {**recipe, "argv": [*recipe["argv"], *extra]}}, MANAGEMENT)
                self.assertEqual(status, 400)
        self.assertEqual(self.request("/v1/workflows", None, MANAGEMENT), (200, {"workflows": []}))
        manifest = self.root / "app.yml"
        manifest.write_text("name: app\nregion: cn\nimage: nginx\n")
        def cli(*args):
            result = subprocess.run([str(self.cli), "--control-url", self.endpoint, "--token", MANAGEMENT, "--insecure", "--no-env", "--format", "json", *args], cwd=self.root, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["result"]
        saved = cli("workflow", "record", "app", "--note", "via Go", "--", "--no-env", "deploy", str(manifest))
        self.assertEqual(saved["workflow"]["note"], "via Go")
        self.assertEqual(cli("workflow", "show", "app")["workflow"], saved["workflow"])
        self.assertEqual(cli("workflow", "list")["workflows"], [saved["workflow"]])


if __name__ == "__main__":
    unittest.main()
