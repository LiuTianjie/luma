import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from luma.control import build_events, database, monitoring, server, state
from luma.control.write_gate import WriterGate
from luma.errors import LumaError


class WriterGateTests(unittest.TestCase):
    def test_waiters_enter_fifo_and_newcomers_cannot_overtake(self):
        gate = WriterGate()
        entered = []

        def write(number):
            with gate.acquire(str(number)):
                entered.append(number)

        # Admit writers deterministically while the first owner holds the gate.
        with ThreadPoolExecutor(max_workers=3) as pool:
            with gate.acquire("first"):
                futures = []
                for number in range(3):
                    futures.append(pool.submit(write, number))
                    with gate.condition:
                        self.assertTrue(gate.condition.wait_for(lambda: len(gate.pending) == number + 1, timeout=2))
                self.assertEqual(entered, [])
            for future in futures:
                future.result(timeout=2)
        self.assertEqual(entered, [0, 1, 2])

    def test_timed_out_waiter_is_removed_and_does_not_block_successors(self):
        gate = WriterGate()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with gate.acquire("holder"):
                def wait():
                    with gate.acquire("waiter", timeout=0):
                        self.fail("must not acquire while holder is active")
                with self.assertRaisesRegex(LumaError, "database is locked:.*current writer: holder"):
                    pool.submit(wait).result(timeout=2)
                self.assertEqual(list(gate.pending), [])
            with gate.acquire("next"):
                self.assertEqual(gate.owner, "next")

    def test_nested_writer_fails_immediately_and_exception_releases_owner(self):
        gate = WriterGate()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with gate.acquire("outer"):
                with self.assertRaisesRegex(LumaError, "nested Control write transaction"):
                    with gate.acquire("nested"):
                        self.fail("nested write must not wait for itself")
                raise RuntimeError("abort")
        with gate.acquire("next"):
            self.assertEqual(gate.owner, "next")


class ControlContentionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": folder.name})
        env.start()
        self.addCleanup(env.stop)
        self.token = "test-node-token"
        state.save_state({
            "domain": "contention.test", "clusterId": "test", "deployToken": "management",
            "nodes": {"worker": {"agent": {"tokenHash": server._hash_agent_token(self.token)}}},
            "agentTasks": {}, "buildRuns": {}, "builderTasks": {},
        })

    def seed_history(self, count=30, events=100):
        def seed(current):
            for kind in ("buildRuns", "agentTasks", "builderTasks"):
                for index in range(count):
                    current[kind][f"old-{index:04d}"] = {
                        "id": f"old-{index:04d}", "status": "succeeded", "completedAt": 1,
                        "events": [{"message": f"history-{n}"} for n in range(events)],
                    }
        state.mutate_state(seed)

    def test_metrics_preserve_values_without_reading_terminal_records_or_logs(self):
        self.seed_history()
        def seed_active(current):
            for kind in ("agentTasks", "builderTasks", "buildRuns"):
                current[kind]["waiting"] = {"status": "queued", "createdAt": 100, "events": [{"message": "ignore"}]}
                current[kind]["active"] = {"status": "running", "createdAt": 110, "progress": [{"line": "ignore"}]}
        state.mutate_state(seed_active)
        expected = monitoring.render_metrics(state.load_state(), now=200)
        read = database.read_entity

        def only_nodes(conn, kind, identifier):
            self.assertEqual(kind, "nodes")
            return read(conn, kind, identifier)

        with patch.object(database, "read_entity", side_effect=only_nodes):
            snapshot = state.load_metrics_state()
            with patch.object(monitoring.time, "time", return_value=200):
                actual = server.handle_prometheus_metrics("management")
        self.assertEqual(actual, expected)
        self.assertNotIn("events", snapshot["buildRuns"]["active"])
        self.assertEqual(set(snapshot["buildRuns"]), {"waiting", "active"})

    def test_heartbeat_and_idle_lease_never_hydrate_unrelated_receipts(self):
        self.seed_history()
        read = database.read_entity

        def only_nodes(conn, kind, identifier):
            self.assertEqual(kind, "nodes")
            return read(conn, kind, identifier)

        with patch.object(database, "read_entity", side_effect=only_nodes), patch.object(
            server, "load_config", return_value=None
        ), patch.object(server, "_record_metrics_history"):
            body = {"nodeName": "worker", "os": "linux", "activeTaskId": ""}
            response = server.handle_node_agent_heartbeat(self.token, body)
            self.assertFalse(response["cancelRequested"])
            self.assertEqual(server.handle_node_agent_lease(self.token, body), {"task": None})
        self.assertEqual(len(state.load_state()["agentTasks"]), 30)

    def test_runtime_snapshot_keeps_terminal_active_child_cancellation(self):
        self.seed_history()
        state.mutate_state(lambda current: current["agentTasks"].update(child={
            "status": "timeout", "nodeName": "worker", "cancelRequestedAt": 12,
            "progress": [{"line": "large progress"}] * 300,
        }))
        snapshot = state.load_runtime_state(task_id="child")
        self.assertEqual(set(snapshot["agentTasks"]), {"child"})
        self.assertEqual(snapshot["agentTasks"]["child"]["cancelRequestedAt"], 12)
        self.assertNotIn("progress", snapshot["agentTasks"]["child"])
        self.assertNotIn("agentTasks", state.load_runtime_state())

    def test_maintenance_is_bounded_and_cursor_eventually_reaches_every_receipt(self):
        self.seed_history(count=205, events=0)
        for remaining in (105, 5, 0):
            state.mutate_state(lambda current: server._prune_agent_tasks(current, now=10000))
            self.assertEqual(len(state.load_state()["agentTasks"]), remaining)
        self.assertNotIn("agentTaskMaintenanceCursor", state.load_state())

    def test_nested_database_write_rolls_back_and_next_writer_can_commit(self):
        with self.assertRaisesRegex(LumaError, "nested Control write transaction"):
            with database.transaction(owner="outer") as conn:
                conn.execute("UPDATE control_config SET payload='\"changed\"' WHERE key='domain'")
                state.mutate_state(lambda current: current.update(domain="nested"))
        self.assertEqual(state.load_state()["domain"], "contention.test")
        state.mutate_state(lambda current: current.update(domain="recovered.test"))
        self.assertEqual(state.load_state()["domain"], "recovered.test")

    def test_wal_reader_is_not_queued_behind_writer(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with database.transaction(owner="writer") as conn:
                conn.execute("UPDATE control_config SET payload='\"uncommitted\"' WHERE key='domain'")
                read = pool.submit(state.load_metrics_state).result(timeout=2)
                self.assertEqual(read["domain"], "contention.test")
        self.assertEqual(state.load_state()["domain"], "uncommitted")

    def test_simultaneous_heartbeats_and_build_logs_preserve_every_write(self):
        self.seed_history()
        state.mutate_state(lambda current: current["buildRuns"].update(build={"id": "build", "status": "running", "events": []}))
        ready = threading.Barrier(6)

        def work(worker):
            ready.wait(timeout=3)
            for index in range(5):
                if worker % 2:
                    build_events.append_build_run_events("build", [{"message": f"{worker}:{index}:{n}"} for n in range(10)])
                else:
                    state.mutate_state(lambda current: current["nodes"]["worker"]["agent"].update({str(worker): index}))

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(work, range(6)))
        current = state.load_state()
        self.assertEqual(len(current["buildRuns"]["build"]["events"]), 150)
        self.assertEqual(len({entry["message"] for entry in current["buildRuns"]["build"]["events"]}), 150)
        for worker in (0, 2, 4):
            self.assertEqual(current["nodes"]["worker"]["agent"][str(worker)], 4)

    def test_slow_write_diagnostic_records_owner_without_sql_values(self):
        with patch.object(database, "SLOW_TRANSACTION_SECONDS", 0), self.assertLogs(database.LOG, level="WARNING") as logs:
            with database.transaction(owner="heartbeat") as conn:
                conn.execute("UPDATE control_config SET payload=? WHERE key='deployToken'", (json.dumps("private-value"),))
        self.assertIn("owner=heartbeat", logs.output[0])
        self.assertIn("wait=", logs.output[0])
        self.assertIn("held=", logs.output[0])
        self.assertNotIn("private-value", "\n".join(logs.output))
