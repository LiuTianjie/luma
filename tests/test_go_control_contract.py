"""Go Control reads actual Python-created SQLite, never a translated fixture DB."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from luma.control import history
from luma.control.state import init_state, load_state, save_state

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fixture-management-token"
APP = "中文 <app> 🦆"


def comparable(value):
    if isinstance(value, dict):
        return {k: json.loads(base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)))
                if k == "nextCursor" and v else comparable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [comparable(v) for v in value]
    return value


@unittest.skipUnless(shutil.which("go"), "Go required for Control migration contracts")
class GoControlContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build_dir.cleanup)
        cls.binary = Path(cls.build_dir.name) / "luma-control"
        subprocess.run(["go", "build", "-o", str(cls.binary), "./cmd/luma-control"],
                       cwd=ROOT, check=True, capture_output=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "restored state #1"
        self.environment = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(self.directory))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        state = init_state(domain="fixture.test", cluster_id="fixture")
        state.update(deployToken=TOKEN, joinToken="fixture-join-token")
        state["buildQueue"] = {"private": {"body": {"envSecrets": {"PASSWORD": "private-build-secret"}}}}
        state["buildRuns"] = {}
        state["deploymentEvents"] = []
        for index in range(7):
            # Same-second ordering must use id/kind or legacy insertion order.
            stamp = 1700000000 + index // 3
            build_id = f"构建-{index}"
            state["buildRuns"][build_id] = {
                "id": build_id, "status": "succeeded" if index % 2 else "failed",
                "name": APP if index % 2 else "worker", "createdAt": stamp, "updatedAt": stamp + 1,
                "request": {"repository": "repo/app", "ref": "main", "buildNode": "worker"},
                "result": {"image": "app:1"}, "message": "中文" * 510,
                "events": [{"type": "step", "message": f"build event {i}"} for i in range(7)],
                "progress": [{"private": "unrelated-progress"}],
                "__luma_event_streams__": {"must": "disappear"},
            }
            state["deploymentEvents"].append({
                "id": f"deploy-{6-index}", "name": APP if index % 2 else "worker",
                "createdAt": stamp, "updatedAt": stamp + 2, "status": "succeeded",
                "origin": "dashboard" if index % 2 else "cli",
                "gitSource": {"repository": "repo/deploy", "ref": "dev"},
                "steps": [{"type": "step", "message": f"deploy event {i}"} for i in range(5)],
            })
        save_state(state)
        self.process = None
        self.addCleanup(self.stop)

    def start(self):
        self.process = subprocess.Popen([str(self.binary), "--state-dir", str(self.directory),
                                         "--read-only", "--listen", "127.0.0.1:0"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.process.stdout], [], [], 10)
        if not ready:
            self.fail("Go Control did not report readiness")
        line = self.process.stdout.readline().strip()
        if "listening on " not in line:
            _, stderr = self.process.communicate(timeout=5)
            self.fail(f"Go Control failed to start: {stderr}")
        self.endpoint = "http://" + line.split("listening on ", 1)[1]

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

    def request(self, path, query=None, token=TOKEN, method="GET"):
        request = Request(self.endpoint + path + ("?" + urlencode(query) if query else ""), method=method)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            return response.status, json.loads(response.read())

    def assert_response(self, path, query, expected):
        status, actual = self.request(path, query)
        self.assertEqual(status, 200, actual)
        self.assertEqual(comparable(actual), comparable(expected))
        return actual

    def test_list_parity_and_bidirectional_cursors(self):
        self.start()
        for path, reference in (("/v1/history", history.list_history),
                                ("/v1/builds", history.list_builds),
                                ("/v1/deployments/history", history.list_deployments)):
            for query in ({"limit": "2"}, {"limit": "1", "app": APP},
                          {"status": "succeeded", "source": "dashboard"},
                          {"since": "2023-11-14T22:13:21Z", "until": "1700000002"}):
                with self.subTest(path=path, query=query):
                    expected = reference(query)
                    actual = self.assert_response(path, query, expected)
                    while expected["page"]["hasMore"]:
                        # Both runtimes consume the other's cursor.
                        py_next = reference({**query, "cursor": actual["page"]["nextCursor"]})
                        actual = self.assert_response(path, {**query, "cursor": expected["page"]["nextCursor"]}, py_next)
                        expected = py_next

    def test_detail_parity_event_watermark_and_resume(self):
        self.start()
        for kind, record_id in (("build", "构建-3"), ("deployment", "deploy-3")):
            path = f"/v1/history/{kind}/{quote(record_id)}"
            expected = history.get_history(kind, record_id, {"limit": "2"})
            actual = self.assert_response(path, {"limit": "2"}, expected)
            while expected["page"]["hasMore"]:
                py_next = history.get_history(kind, record_id, {"limit": "2", "cursor": actual["page"]["nextCursor"]})
                actual = self.assert_response(path, {"limit": "2", "cursor": expected["page"]["nextCursor"]}, py_next)
                expected = py_next
            query = {"after": "2", "limit": "2"}
            self.assert_response(path, query, history.get_history(kind, record_id, query))
        self.assert_response("/v1/deployments/history/deploy-3", {"limit": "2"}, history.get_deployment("deploy-3", {"limit": "2"}))
        self.assert_response("/v1/builds/" + quote("构建-3"), {"limit": "2"}, history.get_build("构建-3", {"limit": "2"}))

    def test_build_detail_queue_position_matches_existing_control(self):
        from luma.control import build_queue

        state = load_state()
        state["buildRuns"].update({
            "target": {"id": "target", "status": "queued", "projectKey": "project", "queueOrder": 4},
            "older": {"id": "older", "status": "queued", "projectKey": "project", "queueOrder": 2},
            "later": {"id": "later", "status": "queued", "projectKey": "project", "queueOrder": 5},
            "owner": {"id": "owner", "status": "running", "projectKey": "project", "queueOrder": 1},
            "foreign": {"id": "foreign", "status": "running", "projectKey": "other"},
            "upload": {"id": "upload", "status": "running", "mode": "local", "request": {"queue": True}, "projectKey": "project"},
        })
        save_state(state)
        self.start()
        expected = history.get_build("target")
        expected["run"].update(build_queue.position("target"))
        self.assertEqual(expected["run"]["queuePosition"], 3)
        self.assert_response("/v1/builds/target", {}, expected)
        # The generic history detail is a record projection, not a live queue view.
        self.assert_response("/v1/history/build/target", {}, history.get_history("build", "target"))

    def test_concurrent_insert_does_not_enter_existing_page_window(self):
        self.start()
        _, first = self.request("/v1/history", {"limit": "2"})
        _, details = self.request("/v1/history/build/" + quote("构建-3"), {"limit": "2"})
        state = load_state()
        state["deploymentEvents"].append({"id": "new-backdated", "name": APP, "createdAt": 1, "status": "succeeded"})
        state["buildRuns"]["构建-3"]["events"].append({"type": "new-after-first-page"})
        save_state(state)
        query = {"cursor": first["page"]["nextCursor"], "limit": "100"}
        actual = self.assert_response("/v1/history", query, history.list_history(query))
        self.assertNotIn("new-backdated", [item["id"] for item in actual["items"]])
        query = {"cursor": details["page"]["nextCursor"], "limit": "100"}
        actual = self.assert_response("/v1/history/build/" + quote("构建-3"), query, history.get_history("build", "构建-3", query))
        self.assertNotIn("new-after-first-page", [event["type"] for event in actual["events"]])

    def test_authentication_read_only_and_no_secret_or_database_mutation(self):
        before = hashlib.sha256((self.directory / "control.sqlite3").read_bytes()).hexdigest()
        self.start()
        self.assertEqual(self.request("/v1/health", token=None)[1]["capabilities"],
                         ["history-read-v1", "build-config-read-v1", "deployment-workflow-read-v1", "secret-names-v1", "registry-credentials-read-v1"])
        for token in (None, "", "bad", "fixture-join-token"):
            self.assertEqual(self.request("/v1/history", token=token)[0], 401)
        self.assertEqual(self.request("/v1/auth/login/verify", method="POST"),
                         (200, {"clusterId": "fixture", "endpoint": "fixture.test"}))
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request("/v1/deployments", method=method)[0], 501)
        for path in ("/v1/status", "/v1/storage", "/v1/builds/unsupported"):
            self.assertEqual(self.request(path)[0], 404)
        _, data = self.request("/v1/history", {"limit": "100"})
        for private in (TOKEN, "fixture-join-token", "private-build-secret", "unrelated-progress"):
            self.assertNotIn(private, json.dumps(data))
        self.stop()
        self.assertEqual(before, hashlib.sha256((self.directory / "control.sqlite3").read_bytes()).hexdigest())

    def test_invalid_query_and_cross_scope_cursor_fail(self):
        self.start()
        _, page = self.request("/v1/history", {"limit": "1"})
        cursor = page["page"]["nextCursor"]
        for query in ({"limit": "0"}, {"limit": "101"}, {"limit": "1.5"}, {"limit": "01"},
                      {"kind": "node"}, {"source": "agent"}, {"since": "2026-01-01"},
                      {"since": "20", "until": "10"}, {"cursor": "!"},
                      {"cursor": cursor, "app": "different"}):
            with self.subTest(query=query):
                self.assertEqual(self.request("/v1/history", query)[0], 400)
        self.assertEqual(self.request("/v1/history/build/" + quote("构建-3"), {"cursor": cursor})[0], 400)
        self.assertEqual(self.request("/v1/history/build/absent")[0], 404)

    def test_refuses_missing_corrupt_newer_and_wrong_authority_without_changes(self):
        self.directory.mkdir(exist_ok=True)
        original = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}

        def rejected():
            completed = subprocess.run([str(self.binary), "--state-dir", str(self.directory), "--read-only"],
                                       capture_output=True, text=True, timeout=10)
            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn(TOKEN, completed.stderr)
            self.assertNotIn("listening on", completed.stdout)

        for scenario in ("missing-db", "corrupt", "new-schema", "wrong-id", "uninitialized", "missing-authority", "public-db", "symlink-db"):
            with self.subTest(scenario=scenario):
                for p in self.directory.iterdir():
                    p.unlink()
                for name, data in original.items():
                    p = self.directory / name
                    p.write_bytes(data)
                    p.chmod(0o600)
                db = self.directory / "control.sqlite3"
                if scenario == "missing-db":
                    db.unlink()
                elif scenario == "corrupt":
                    db.write_bytes(b"not sqlite")
                elif scenario in ("new-schema", "wrong-id", "uninitialized"):
                    key, value = {"new-schema": ("schema_version", "999"), "wrong-id": ("database_id", "foreign"),
                                  "uninitialized": ("state_initialized", "0")}[scenario]
                    with sqlite3.connect(db) as conn:
                        conn.execute("UPDATE database_meta SET value=? WHERE key=?", (value, key))
                    conn.close()
                elif scenario == "missing-authority":
                    (self.directory / "control-sqlite-authority.json").unlink()
                elif scenario == "public-db":
                    db.chmod(0o644)
                elif scenario == "symlink-db":
                    db.rename(self.directory / "real.sqlite3")
                    db.symlink_to("real.sqlite3")
                # A stale legacy file must never become a fallback authority.
                (self.directory / "control.json").write_text(json.dumps({"deployToken": "stale"}))
                # SQLite mode=ro can create empty WAL/shared-memory coordination
                # files. It must never change durable DB, authority or legacy data.
                sidecars = {"control.sqlite3-wal", "control.sqlite3-shm"}
                before = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file() and p.name not in sidecars}
                rejected()
                after = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file() and p.name not in sidecars}
                self.assertEqual(before, after)
                wal = self.directory / "control.sqlite3-wal"
                if wal.exists():
                    self.assertEqual(wal.stat().st_size, 0)
                for name in sidecars:
                    p = self.directory / name
                    if p.exists():
                        self.assertEqual(p.stat().st_mode & 0o077, 0)

    def test_authority_change_after_start_fails_closed(self):
        self.start()
        marker = self.directory / "control-sqlite-authority.json"
        data = json.loads(marker.read_text())
        data["databaseId"] = "other-manager"
        marker.write_text(json.dumps(data))
        status, response = self.request("/v1/history")
        self.assertEqual(status, 500)
        self.assertEqual(response["error"], "Control state could not be read")
        self.assertNotIn("other-manager", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
