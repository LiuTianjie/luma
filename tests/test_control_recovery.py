import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from starlette.requests import Request

from luma.agent import _AgentTaskProgressReporter
from luma.control import build_queue, database, database_errors, server, state, storage_governance
from luma.control.client import ControlClient
from luma.errors import DatabaseBusyError, LumaError


class ControlRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": folder.name})
        env.start()
        self.addCleanup(env.stop)
        self.token = "node-token"
        self.original = {
            "domain": "recovery.test", "clusterId": "test", "deployToken": "management",
            "build": {"defaultNode": "builder", "registryHost": "builder:5000"},
            "nodes": {"builder": {"agent": {"status": "ready", "capabilities": ["docker-build"],
                                             "tokenHash": server._hash_agent_token(self.token)}}},
            "agentTasks": {"task": {"id": "task", "nodeName": "builder", "status": "running", "progress": []}},
            "buildRuns": {},
        }
        state.save_state(self.original)

    def remote(self, **body):
        return server._create_build_run(body, source="https://github.com/acme/app.git", build_node="builder",
                                        project_key="acme/app", queued_work=body)

    def test_deployed_success_receipt_retries_without_replaying_deployment(self):
        body = {"repoUrl": "https://github.com/acme/app.git"}
        run = self.remote(**body)
        build_queue.claim()
        update = database.update_entity_metadata
        attempts = []

        def locked_once(conn, kind, identifier, value):
            if kind == "buildRuns" and identifier == run and value.get("status") == "succeeded":
                attempts.append(value["result"])
                if len(attempts) == 1:
                    raise sqlite3.OperationalError("database is locked")
            return update(conn, kind, identifier, value)

        def retry_wait(_delay):
            self.assertEqual(database._writer_gate.owner, "")
            state.mutate_state(lambda current: current["nodes"]["builder"].update(heartbeatDuringRetry=True))

        with patch.object(server, "load_config", return_value=Mock(defaults={})), patch.object(
            server, "_require_build_node", return_value="builder"
        ), patch.object(server, "_build_proxy_for_request", return_value=""), patch.object(
            server, "_run_node_agent_task", return_value={"image": "registry/app:sha", "manifest": "name: app\nregion: cn\nexposure: none\n"}
        ), patch.object(server, "handle_deployment", return_value={"service": "app", "image": "registry/app:sha"}) as deploy, patch.object(
            database, "update_entity_metadata", side_effect=locked_once
        ), patch.object(database_errors.time, "sleep", side_effect=retry_wait):
            result = server.handle_build_deploy("management", body, _queued_run_id=run)
        self.assertEqual(result["image"], "registry/app:sha")
        deploy.assert_called_once()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        saved = state.load_entity("buildRuns", run)
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["result"]["image"], "registry/app:sha")

    def test_receipt_retries_only_transient_database_errors(self):
        for error in (ValueError("invalid"), sqlite3.IntegrityError("constraint"), LumaError("unauthorized")):
            with self.subTest(error=type(error).__name__), patch.object(database_errors.time, "sleep") as sleep:
                write = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    database_errors.persist_receipt(write, owner="test")
                write.assert_called_once()
                sleep.assert_not_called()

    def test_blocked_queue_poll_and_claim_never_load_or_rewrite_log_streams(self):
        first = self.remote(ref="main")
        build_queue.claim()
        second = self.remote(ref="dev")
        state.mutate_state(lambda current: current["buildRuns"][first].update(events=[{"message": "old"}] * 10000))
        read = database.read_entity

        def only_nodes(conn, kind, identifier):
            self.assertEqual(kind, "nodes")
            return read(conn, kind, identifier)

        with patch.object(database, "read_entity", side_effect=only_nodes):
            self.assertIsNone(build_queue.claim())
            self.assertEqual(build_queue.position(second)["waitReason"], "builder")
        self.assertEqual(len(state.load_entity("buildRuns", first)["events"]), 10000)

    def test_metadata_mutation_preserves_unloaded_event_streams(self):
        state.mutate_state(lambda current: current["buildRuns"].update(run={"id": "run", "status": "running", "events": [{"message": "preserve"}]}))
        state.mutate_state(lambda current: current["buildRuns"]["run"].update(status="succeeded"), metadata_kinds={"buildRuns"})
        self.assertEqual(state.load_entity("buildRuns", "run")["events"], [{"message": "preserve"}])
        with self.assertRaisesRegex(ValueError, "metadata must not contain event streams"):
            state.mutate_state(lambda current: current["buildRuns"]["run"].update(events=[]), metadata_kinds={"buildRuns"})

    def test_completion_does_not_read_unrelated_history(self):
        state.mutate_state(lambda current: current["buildRuns"].update(old={"id": "old", "status": "succeeded", "events": [{"message": "old"}] * 1000}))
        with patch.object(server, "load_state", side_effect=AssertionError("full snapshot")), patch.object(server, "load_config", return_value=None):
            result = server.handle_node_agent_complete(self.token, {"nodeName": "builder", "taskId": "task", "status": "succeeded", "result": {"image": "new"}})
        self.assertEqual(result["status"], "succeeded")

    def test_busy_http_response_is_retryable_and_validation_stays_400(self):
        async def invoke(error):
            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}
            request = Request({"type": "http", "method": "POST", "path": "/v1/secrets", "query_string": b"",
                               "headers": [(b"authorization", b"Bearer management")]}, receive)
            with patch.object(server, "handle_secret_set", side_effect=error):
                return await server._asgi_authenticated_post(request)
        for error in (DatabaseBusyError("writer busy"), sqlite3.OperationalError("database is locked")):
            response = asyncio.run(invoke(error))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(json.loads(response.body)["errorInfo"]["code"], "database_busy")
            self.assertEqual(response.headers["Retry-After"], "1")
        self.assertEqual(asyncio.run(invoke(LumaError("invalid name"))).status_code, 400)

    def test_progress_retries_and_late_batches_are_not_appended_twice(self):
        body = {"nodeName": "builder", "taskId": "task", "batchSequence": 1, "events": [{"line": "one"}]}
        self.assertEqual(server.handle_node_agent_progress(self.token, body)["events"], 1)
        self.assertEqual(server.handle_node_agent_progress(self.token, body)["events"], 0)
        server.handle_node_agent_progress(self.token, {**body, "batchSequence": 3, "events": [{"line": "three"}]})
        self.assertEqual(server.handle_node_agent_progress(self.token, {**body, "batchSequence": 2})["events"], 0)
        saved = state.load_entity("agentTasks", "task")
        self.assertEqual([e["line"] for e in saved["progress"]], ["one", "three"])
        self.assertEqual(saved["progressBatchSequence"], 3)

    def test_progress_sequence_and_events_commit_atomically(self):
        body = {"nodeName": "builder", "taskId": "task", "batchSequence": 1, "events": [{"line": "one"}]}
        with patch.object(database, "write_state", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(DatabaseBusyError):
                server.handle_node_agent_progress(self.token, body)
        self.assertNotIn("progressBatchSequence", state.load_entity("agentTasks", "task"))
        self.assertEqual(server.handle_node_agent_progress(self.token, body)["events"], 1)

    def test_progress_sender_reuses_sequence_and_client_serializes_it(self):
        reporter = object.__new__(_AgentTaskProgressReporter)
        reporter.client = Mock()
        reporter.node_name, reporter.node_id, reporter.task_id = "builder", "node", "task"
        reporter.batch_sequence = 0
        reporter.client.progress_agent_task.side_effect = [TimeoutError(), {}, {}]
        with patch("luma.agent.time.sleep"):
            self.assertTrue(reporter._send([{"line": "one"}]))
            self.assertTrue(reporter._send([{"line": "two"}]))
        self.assertEqual([c.kwargs["batch_sequence"] for c in reporter.client.progress_agent_task.call_args_list], [1, 1, 2])
        client = ControlClient("https://unused.test", "token")
        with patch.object(client, "request", return_value={}) as request:
            client.progress_agent_task(task_id="task", node_name="builder", batch_sequence=7, events=[])
        self.assertEqual(request.call_args.args[2]["batchSequence"], 7)

    def test_preview_hashes_without_writer_and_remains_compatible_with_old_plans(self):
        state.mutate_state(lambda current: current["buildRuns"].update(old={"id": "old", "status": "succeeded", "updatedAt": 1, "events": [{"message": "old"}] * 1000}))
        fingerprint = storage_governance._fingerprint
        calls = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            def check(conn, row):
                self.assertEqual(database._writer_gate.owner, "")
                # A writer can commit while this read snapshot is still alive.
                pool.submit(state.mutate_state, lambda current: current["nodes"]["builder"].update(heartbeat=True)).result(timeout=2)
                events = list(conn.execute("SELECT stream,position,payload FROM control_events WHERE kind=? AND entity_id=? ORDER BY stream,position", (row["kind"], row["id"])))
                expected = hashlib.sha256(json.dumps([dict(row), [tuple(event) for event in events]], sort_keys=True).encode()).hexdigest()
                result = fingerprint(conn, row)
                self.assertEqual(result[0], expected)
                calls.append(row["id"])
                return result
            with patch.object(storage_governance, "_fingerprint", side_effect=check):
                plan = storage_governance.preview_history(now=10000000)
        self.assertEqual(calls, ["old"])
        self.assertEqual(plan["candidateCount"], 1)

    def test_concurrent_progress_replays_have_one_committed_batch(self):
        barrier = threading.Barrier(4)
        def send(_):
            barrier.wait(timeout=2)
            return server.handle_node_agent_progress(self.token, {"nodeName": "builder", "taskId": "task", "batchSequence": 1, "events": [{"line": "one"}]})["events"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(send, range(4)))
        self.assertEqual(sum(results), 1)
