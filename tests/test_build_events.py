import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from luma.control import build_events, database, history, server, state


class BuildEventTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        state.init_state(domain="build-events.test", cluster_id="build-events-test")

    def seed(self, count=0, status="running", identifier="build"):
        def create(current):
            current.setdefault("buildRuns", {})[identifier] = {
                "id": identifier, "status": status, "createdAt": 100, "updatedAt": 100,
                "request": {"name": "app"}, "message": "original",
                "events": [{"message": f"old-{i}"} for i in range(count)],
                "steps": [{"message": "unrelated stream"}],
            }
        state.mutate_state(create)

    def test_append_work_depends_on_new_batch_not_historical_log_size(self):
        self.seed(10_000)
        encoded_events = []
        encode = database._json

        def counted(value):
            if isinstance(value, dict) and "message" in value and "id" not in value:
                encoded_events.append(value["message"])
            return encode(value)

        with patch.object(database, "_json", side_effect=counted), patch.object(
            database, "read_entity", side_effect=AssertionError("must not materialize event history")
        ):
            build_events.append_build_run_events("build", [{"message": f"new-{i}"} for i in range(20)])

        self.assertEqual(encoded_events, [f"new-{i}" for i in range(20)])
        with database.transaction(immediate=False) as conn:
            count = conn.execute("SELECT COUNT(*) FROM control_events WHERE entity_id='build' AND stream='events'").fetchone()[0]
            self.assertEqual(count, 10_020)
            self.assertEqual(database.read_entity(conn, "buildRuns", "build")["steps"], [{"message": "unrelated stream"}])

    def test_ten_producers_append_without_lost_events_or_duplicate_positions(self):
        self.seed(3)

        def produce(worker):
            for batch in range(3):
                build_events.append_build_run_events("build", [
                    {"message": f"{worker}:{batch}:{index}"} for index in range(10)
                ])

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(produce, range(10)))
        with database.transaction(immediate=False) as conn:
            rows = conn.execute("SELECT position,payload FROM control_events WHERE entity_id='build' AND stream='events' ORDER BY position").fetchall()
        self.assertEqual([row[0] for row in rows], list(range(303)))
        actual = [json.loads(row[1])["message"] for row in rows[3:]]
        expected = {f"{worker}:{batch}:{index}" for worker in range(10) for batch in range(3) for index in range(10)}
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(expected))
        for worker in range(10):
            self.assertEqual([message for message in actual if message.startswith(f"{worker}:")],
                             [f"{worker}:{batch}:{index}" for batch in range(3) for index in range(10)])

    def test_new_events_and_failure_summary_roll_back_together(self):
        self.seed(2)
        with database.transaction() as conn:
            conn.execute("""CREATE TRIGGER reject_event BEFORE INSERT ON control_events
                WHEN json_extract(NEW.payload, '$.message') = 'reject'
                BEGIN SELECT RAISE(ABORT, 'injected event write failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            build_events.append_build_run_events("build", [
                {"status": "fail", "message": "first failure"}, {"message": "reject"},
            ])
        run = state.load_entity("buildRuns", "build")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["message"], "original")
        self.assertEqual(run["updatedAt"], 100)
        self.assertEqual(run["events"], [{"message": "old-0"}, {"message": "old-1"}])

    def test_cancellation_status_survives_late_failure_events(self):
        for status in ("running", "canceling", "canceled"):
            with self.subTest(status=status):
                self.seed(status=status, identifier=status)
                with patch.object(build_events.time, "time", return_value=1234):
                    build_events.append_build_run_events(status, [{"status": "fail", "message": "failed"}])
                run = state.load_entity("buildRuns", status)
                self.assertEqual(run["status"], "failed" if status == "running" else status)
                self.assertEqual(run["message"], "failed" if status == "running" else "original")
                self.assertEqual(run["updatedAt"], 1234)
                self.assertEqual(run["createdAt"], 100)

    def test_log_payload_bounds_and_summary_indexes_remain_compatible(self):
        self.seed()
        with patch.object(build_events.time, "time", return_value=1234):
            build_events.append_build_run_events("build", [
                {"status": "fail", "message": "x" * 100, "ignored": None, "number": 42, "ts": "untrusted"},
            ], message_limit=10)
        with database.transaction(immediate=False) as conn:
            run = database.read_entity(conn, "buildRuns", "build")
            row = conn.execute("SELECT status,app,created_at,updated_at FROM control_entities WHERE kind='buildRuns' AND id='build'").fetchone()
        self.assertEqual(tuple(row), ("failed", "app", 100, 1234))
        self.assertEqual(run["events"], [{"status": "fail", "message": "x" * 10, "number": "42", "ts": 1234}])

    def test_existing_history_cursor_and_live_resume_include_appended_batches(self):
        self.seed(3)
        first = history.get_build("build", {"limit": 2})
        build_events.append_build_run_events("build", [{"message": "new-0"}, {"message": "new-1"}])
        frozen = history.get_build("build", {"limit": 2, "cursor": first["eventsPage"]["nextCursor"]})
        self.assertEqual([event["message"] for event in frozen["run"]["events"]], ["old-2"])
        resumed = history.get_build("build", {"after": frozen["eventsPage"]["resumeAfter"]})
        self.assertEqual([event["message"] for event in resumed["run"]["events"]], ["new-0", "new-1"])
        self.assertEqual(resumed["eventsPage"]["resumeAfter"], 4)

    def test_missing_run_and_empty_batch_do_not_create_records(self):
        self.seed()
        build_events.append_build_run_events("missing", [{"message": "late"}])
        build_events.append_build_run_events("build", [])
        self.assertIsNone(state.load_entity("buildRuns", "missing"))
        self.assertEqual(state.load_entity("buildRuns", "build")["updatedAt"], 100)

    def test_batch_transport_preserves_cursor_and_does_not_also_emit_individual_events(self):
        first = [{"line": f"line-{index}"} for index in range(50)]
        snapshots = [
            {"id": "task", "status": "running", "progress": first},
            {"id": "task", "status": "succeeded", "progressOffset": 1,
             "progress": first[1:] + [{"line": "line-50"}], "result": {"image": "registry/app:attempt"}},
        ]
        batches = []
        with patch("luma.control.state.load_entity", side_effect=snapshots), patch.object(server.time, "sleep"), patch.object(
            server, "_emit_progress", side_effect=AssertionError("batched progress must not be written twice")
        ):
            result = server._wait_node_agent_task("task", "builder", "build-image", timeout=1, progress_batch=batches.append)
        self.assertEqual([len(batch) for batch in batches], [50, 1])
        self.assertEqual([event["message"] for batch in batches for event in batch], [f"line-{i}" for i in range(51)])
        self.assertEqual(result["image"], "registry/app:attempt")

    def test_event_batch_is_not_committed_if_summary_update_fails(self):
        self.seed(1)
        with patch.object(build_events, "update_entity_metadata", side_effect=RuntimeError("summary failed")):
            with self.assertRaisesRegex(RuntimeError, "summary failed"):
                build_events.append_build_run_events("build", [{"message": "must roll back"}])
        self.assertEqual(state.load_entity("buildRuns", "build")["events"], [{"message": "old-0"}])
