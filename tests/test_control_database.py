import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from luma.control import database, state
from luma.errors import LumaError


class ControlDatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def sample(self):
        return {"domain": "example.test", "deployToken": "private-token", "nodes": {"node-a": {"agent": {"lastSeen": 1}}},
            "agentTasks": {"task": {"id": "task", "status": "queued", "createdAt": 10, "events": [], "progress": [{"line": "hello"}]}},
            "builderTasks": {"build": {"id": "build", "status": "running", "events": [{"message": "started"}]}},
            "builderTaskIdempotency": {"key": {"taskId": "build"}}, "builderSourceSnapshots": {"source": {"files": ["app.py"]}},
            "buildRuns": {"run": {"id": "run", "status": "succeeded", "createdAt": 20, "events": [{"message": "done"}]}},
            "deploymentEvents": [{"id": "deploy", "slug": "app", "createdAt": 30, "steps": [{"title": "ready"}]}]}

    def test_migration_roundtrip_backup_manifest_and_db_authority(self):
        original = self.sample()
        raw = json.dumps(original).encode()
        state.state_path().write_bytes(raw)
        self.assertEqual(state.load_state(), original)
        self.assertEqual((self.directory / "control.json.pre-sqlite.bak").read_bytes(), raw)
        manifest = json.loads((self.directory / "control-sqlite-migration.json").read_text())
        self.assertEqual(manifest["counts"]["control_entities"], 7)
        self.assertTrue(database.check_database()["ok"])
        with database.transaction(immediate=False) as conn:
            row = conn.execute("SELECT payload FROM control_entities WHERE kind='agentTasks'").fetchone()
            self.assertNotIn("progress", json.loads(row[0]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM control_events").fetchone()[0], 4)
        state.state_path().write_text('{"domain":"stale-file"}')
        self.assertEqual(state.load_state(), original)
        self.assertEqual(json.loads((self.directory / "control-sqlite-migration.json").read_text()), manifest)

    def test_fresh_init_writes_sqlite_directly_and_reuses_credentials(self):
        with patch.object(state, "_save_json_state", side_effect=AssertionError("fresh Control must not write JSON state")):
            original = state.init_state(domain="new.example")
            state.mutate_state(lambda current: current.update(buildRuns={"first": {"status": "succeeded", "events": [{"message": "ready"}]}}))
            reopened = state.init_state(domain="ignored.example")
        self.assertEqual(reopened["deployToken"], original["deployToken"])
        self.assertEqual(reopened["clusterId"], original["clusterId"])
        self.assertEqual(reopened["domain"], "new.example")
        self.assertEqual(reopened["buildRuns"]["first"]["events"], [{"message": "ready"}])
        self.assertFalse(state.state_path().exists())
        self.assertFalse((self.directory / "control.json.pre-sqlite.bak").exists())
        self.assertFalse((self.directory / "control-sqlite-migration.json").exists())
        with database.transaction(immediate=False) as conn:
            self.assertIsNone(conn.execute("SELECT value FROM database_meta WHERE key='legacy_import'").fetchone())
        self.assertTrue(database.check_database()["ok"])

    def test_fresh_save_initializes_without_legacy_json(self):
        state.save_state(self.sample())
        self.assertFalse(state.state_path().exists())
        self.assertFalse((self.directory / "control-sqlite-migration.json").exists())
        self.assertEqual(state.load_state(), self.sample())

    def test_failed_fresh_initialization_rolls_back_and_can_retry(self):
        original_write = database.write_state
        def crash(conn, data):
            original_write(conn, data)
            raise RuntimeError("interrupted installation")
        with patch.object(database, "write_state", side_effect=crash), self.assertRaises(RuntimeError):
            state.init_state(domain="new.example")
        self.assertFalse(state.is_initialized())
        self.assertFalse(state.state_path().exists())
        self.assertFalse((self.directory / "control-sqlite-authority.json").exists())
        with database.transaction(immediate=False) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM control_config").fetchone()[0], 0)
        self.assertEqual(state.init_state(domain="retry.example")["domain"], "retry.example")

    def test_uninitialized_reads_point_to_bootstrap_without_creating_state(self):
        for read in (state.load_state, state.load_auth_state, state.load_runtime_state):
            with self.subTest(read=read.__name__), self.assertRaisesRegex(LumaError, r"control.sqlite3; run luma bootstrap"):
                read()
        self.assertFalse(database.database_path().exists())
        self.assertFalse(state.state_path().exists())

    def test_failed_import_rolls_back_and_is_retryable(self):
        state.state_path().write_text(json.dumps(self.sample()))
        with patch.object(database, "read_state", return_value={}), self.assertRaises(LumaError):
            state.load_state()
        with database.transaction() as conn:
            self.assertFalse(database.initialized(conn))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM control_entities").fetchone()[0], 0)
        self.assertEqual(state.load_state(), self.sample())

    def test_corrupt_json_and_database_fail_closed(self):
        state.state_path().write_text("{bad")
        with self.assertRaises(LumaError):
            state.load_state()
        database.database_path().unlink()
        database.database_path().write_bytes(b"not sqlite")
        state.state_path().write_text(json.dumps(self.sample()))
        with self.assertRaises(LumaError):
            state.load_state()

    def test_committed_cutover_never_resurrects_json_after_database_loss(self):
        state.save_state(self.sample())
        state.mutate_state(lambda current: current.update(deployToken="rotated-token"))
        self.assertTrue((self.directory / "control-sqlite-authority.json").exists())
        database.database_path().unlink()
        with self.assertRaisesRegex(LumaError, "refusing stale"):
            state.load_state()
        with self.assertRaisesRegex(LumaError, "refusing stale"):
            state.save_state({"domain": "accidental-reset"})

    def test_truncated_database_is_not_reimported(self):
        state.save_state(self.sample())
        database.database_path().write_bytes(b"")
        with self.assertRaisesRegex(LumaError, "refusing stale"):
            state.load_state()

    def test_crash_after_commit_before_authority_does_not_reimport_stale_json(self):
        state.state_path().write_text(json.dumps(self.sample()))
        with patch.object(database, "_record_authority", side_effect=OSError("crash after commit")):
            with self.assertRaises(OSError):
                state.save_state(self.sample())
        self.assertTrue((self.directory / "control-sqlite-migration.json").exists())
        database.database_path().unlink()
        with self.assertRaisesRegex(LumaError, "refusing stale"):
            state.load_state()

    def test_wrong_database_identity_is_rejected_before_save_commits(self):
        state.save_state(self.sample())
        with closing(sqlite3.connect(database.database_path())) as conn:
            conn.execute("UPDATE database_meta SET value='wrong-database' WHERE key='database_id'")
            conn.commit()
        with self.assertRaisesRegex(LumaError, "authority check failed"):
            state.save_state({"domain": "must-not-commit"})
        with closing(sqlite3.connect(database.database_path())) as conn:
            payload = conn.execute("SELECT payload FROM control_config WHERE key='domain'").fetchone()[0]
            self.assertEqual(json.loads(payload), "example.test")

    def test_mutations_are_atomic_and_idle_poll_writes_nothing(self):
        state.save_state(self.sample())
        def crash(current):
            current["nodes"]["node-a"]["agent"]["lastSeen"] = 9
            current["builderTaskIdempotency"]["new"] = {"taskId": "new"}
            raise RuntimeError("transaction aborted")
        with self.assertRaises(RuntimeError):
            state.mutate_state(crash)
        self.assertEqual(state.load_state(), self.sample())
        def idle(current):
            current["domain"] = "discarded"
            return "idle", False
        self.assertEqual(state.mutate_state_if_changed(idle), "idle")
        self.assertEqual(state.load_state(), self.sample())

    def test_concurrent_claim_has_exactly_one_winner(self):
        state.save_state(self.sample())
        def claim(worker):
            def mutate(current):
                task = current["agentTasks"]["task"]
                if task["status"] != "queued":
                    return None, False
                task.update(status="running", claimedBy=worker)
                current["builderTaskIdempotency"]["claim"] = worker
                return worker, True
            return state.mutate_state_if_changed(mutate)
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(claim, range(16)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        persisted = state.load_state()
        self.assertEqual(persisted["agentTasks"]["task"]["claimedBy"], winners[0])
        self.assertEqual(persisted["builderTaskIdempotency"]["claim"], winners[0])

    def test_heartbeat_changes_only_touched_entity_without_loading_history(self):
        state.save_state(self.sample())
        traces = []
        original_connect = database.connect
        def traced(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(traces.append)
            return conn
        with patch.object(database, "connect", side_effect=traced):
            state.mutate_state(lambda current: current["nodes"]["node-a"]["agent"].update(lastSeen=2))
        history_reads = [query for query in traces if "SELECT payload FROM control_entities" in query and any(kind in query for kind in ("buildRuns", "deploymentEvents"))]
        self.assertEqual(history_reads, [])
        persisted = state.load_state()
        self.assertEqual(persisted["nodes"]["node-a"]["agent"]["lastSeen"], 2)
        with database.transaction() as conn:
            before = conn.total_changes
            database.write_state(conn, database.read_state(conn, lazy=True))
            self.assertEqual(conn.total_changes, before)

    def test_real_heartbeat_and_idle_lease_do_not_read_historical_payloads(self):
        from luma.control import server

        original = self.sample()
        token = "local-node-token"
        original["nodes"]["node-a"]["agent"]["tokenHash"] = server._hash_agent_token(token)
        state.save_state(original)
        for handler in (server.handle_node_agent_heartbeat, server.handle_node_agent_lease):
            with self.subTest(handler=handler.__name__), patch.object(server, "load_config", return_value=None), patch.object(database, "read_entity", wraps=database.read_entity) as reader:
                handler(token, {"nodeName": "node-a", "metrics": {"cpuPercent": 5}, "waitSeconds": 0})
                historical = [call.args[1:] for call in reader.call_args_list if call.args[1] in {"buildRuns", "deploymentEvents"}]
                self.assertEqual(historical, [])
        self.assertEqual(state.load_state()["nodes"]["node-a"]["agent"]["metrics"]["cpuPercent"], 5)

    def test_append_deployment_does_not_hydrate_older_history(self):
        state.save_state(self.sample())
        with patch.object(database, "read_entity", wraps=database.read_entity) as reader:
            state.mutate_state(lambda current: current["deploymentEvents"].append({"id": "second", "steps": []}))
            self.assertFalse(any(call.args[1] == "deploymentEvents" for call in reader.call_args_list))
        self.assertEqual([event["id"] for event in state.load_state()["deploymentEvents"]], ["deploy", "second"])

    def test_lazy_dict_and_list_mutations_roundtrip(self):
        state.save_state(self.sample())
        def mutate(current):
            tasks = current["agentTasks"]
            self.assertIsInstance(tasks, dict)
            self.assertEqual(dict(tasks), self.sample()["agentTasks"])
            tasks["second"] = {"status": "running", "progress": []}
            tasks.pop("task")
            events = current["deploymentEvents"]
            self.assertIsInstance(events, list)
            events.insert(0, {"id": "first"})
            events[-1]["status"] = "failed"
            return {"tasks": tasks, "events": events}
        result = state.mutate_state(mutate)
        self.assertEqual(result["tasks"], state.load_state()["agentTasks"])
        self.assertEqual(result["events"], state.load_state()["deploymentEvents"])

    def test_active_values_uses_current_transaction_updates(self):
        value = self.sample()
        value["buildRuns"]["active"] = {"status": "running"}
        state.save_state(value)
        def mutate(current):
            runs = current["buildRuns"]
            self.assertEqual(len(list(runs.active_values({"running"}))), 1)
            runs["active"]["status"] = "succeeded"
            runs["new"] = {"status": "queued"}
            self.assertEqual(list(runs.active_values({"running", "queued"})), [{"status": "queued"}])
        state.mutate_state(mutate)

    def test_backup_is_standalone_and_roundtrips_on_other_manager(self):
        state.save_state(self.sample())
        backup = self.directory / "backup" / "control.sqlite3"
        report = database.backup_database(backup)
        self.assertTrue(report["ok"])
        self.assertTrue(database.check_database(backup)["ok"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(backup) + "-wal").exists())
        with patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(backup.parent)}):
            self.assertEqual(state.load_state(), self.sample())
            state.mutate_state(lambda current: current.update(domain="new-manager.test"))
            self.assertEqual(state.load_state()["domain"], "new-manager.test")
        self.assertEqual(state.load_state()["domain"], "example.test")

    def test_explicit_auxiliary_json_paths_stay_json(self):
        path = self.directory / "registry-inventory.json"
        state.save_state({"images": [1, 2]}, path)
        self.assertEqual(json.loads(path.read_text()), {"images": [1, 2]})
        self.assertEqual(state.load_state(path), {"images": [1, 2]})
        self.assertFalse(database.database_path().exists())

    def test_auth_state_excludes_entity_collections(self):
        state.save_state(self.sample())
        self.assertEqual(state.load_auth_state(), {"domain": "example.test", "deployToken": "private-token"})

    def test_backup_check_rejects_valid_but_uninitialized_or_invalid_payload_store(self):
        conn = database.connect()
        conn.close()
        self.assertFalse(database.check_database()["ok"])
        state.save_state(self.sample())
        with database.transaction() as conn:
            conn.execute("UPDATE control_config SET payload='{bad' WHERE key='domain'")
        report = database.check_database()
        self.assertFalse(report["ok"])
        self.assertEqual(report["invalidJsonRows"], 1)

    def test_schema_and_file_permissions_and_indexes(self):
        state.save_state(self.sample())
        self.assertEqual(database.database_path().stat().st_mode & 0o777, 0o600)
        with database.transaction(immediate=False) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(control_entities)")}
            self.assertIn("control_history_created", indexes)


if __name__ == "__main__":
    unittest.main()
