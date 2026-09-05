"""Legacy cutover keeps reads inert and fences the actual old writer."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luma import bootstrap, cli
from luma.control import database, state
from luma.errors import LumaError


class ControlCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "control"
        self.root.mkdir()
        env = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.legacy = {"clusterId": "test", "domain": "old", "nodes": {}, "deployToken": "private", "buildRuns": {}}
        self.payload = self.root / ".install.tmp"
        self.payload.write_text(json.dumps({"state": {**self.legacy, "domain": "new"}, "secretNames": []}))

    def seed(self):
        state.state_path().write_text(json.dumps(self.legacy))

    def nomad(self, *, status="complete", malformed=False, on_stop=None):
        client = Mock()
        stopped = False
        def request(method, path, *args, **kwargs):
            nonlocal stopped
            if path == "/v1/jobs?prefix=luma-control":
                return [{"ID": "luma-control"}, {"ID": "luma-control-user"}]
            if path == "/v1/job/luma-control":
                return {"ID": "luma-control", "Stop": stopped}
            if method == "DELETE":
                self.assertEqual(path, "/v1/job/luma-control?purge=false")
                stopped = True
                if on_stop:
                    on_stop()
                return {"EvalID": "stop-eval"}
            if path == "/v1/allocations":
                if malformed:
                    return {"unavailable": True}
                return [{"ID": "old", "JobID": "luma-control", "ClientStatus": status,
                    "TaskStates": {"luma-control": {"State": "dead" if status == "complete" else "running"}}},
                    {"ID": "user", "JobID": "user-app", "ClientStatus": "running"}]
            raise AssertionError((method, path))
        client.request.side_effect = request
        return client

    def test_role_detection_reads_current_legacy_without_cutover(self):
        self.seed()
        with patch.object(cli, "LocalExecutor") as executor:
            self.assertEqual(cli._existing_control_state(), self.legacy)
            changed = {**self.legacy, "domain": "late-write"}
            state.state_path().write_text(json.dumps(changed))
            self.assertEqual(cli._existing_control_state(), changed)
            executor.assert_not_called()
        self.assertFalse(database.database_path().exists())
        self.assertFalse((self.root / "control-sqlite-authority.json").exists())

    def test_role_detection_does_not_import_an_uninitialized_sqlite_schema(self):
        self.seed()
        database.connect().close()
        self.assertEqual(cli._existing_control_state(), self.legacy)
        from contextlib import closing
        with closing(database.connect()) as conn:
            self.assertFalse(database.initialized(conn))
        self.assertFalse((self.root / "control-sqlite-authority.json").exists())

    def test_stop_then_backup_and_import_includes_last_old_writer_commit(self):
        self.seed()
        latest = {**self.legacy, "buildRuns": {"last": {"status": "succeeded"}}}
        client = self.nomad(on_stop=lambda: state.state_path().write_text(json.dumps(latest)))
        with patch("luma.nomad_api.NomadApi", return_value=client):
            self.assertTrue(bootstrap._install_control_state_file(self.payload, overwrite=False))
        current = state.load_state()
        self.assertEqual(current["buildRuns"], latest["buildRuns"])
        self.assertEqual(current["domain"], "new")
        pending = json.loads((self.root / "control-sqlite-cutover-pending.json").read_text())
        checkpoint = Path(pending["checkpoint"])
        self.assertEqual(checkpoint.parent, self.root.parent)
        self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((checkpoint / "control/control.json").read_text()), latest)
        self.assertEqual(json.loads((checkpoint / "luma-control.nomad.json").read_text())["Job"]["ID"], "luma-control")
        self.assertEqual([c.args[:2] for c in client.request.call_args_list if c.args[0] != "GET"],
                         [("DELETE", "/v1/job/luma-control?purge=false")])
        # Retry after import must preserve its rollback fence without stopping
        # an already migrated Control.
        with patch("luma.nomad_api.NomadApi") as again:
            self.assertTrue(bootstrap._install_control_state_file(self.payload, overwrite=False))
            again.assert_not_called()

    def test_unconfirmed_or_unreachable_writer_never_creates_database(self):
        for mode in ("lost", "malformed", "unreachable"):
            with self.subTest(mode=mode):
                self.seed()
                client = self.nomad(status="lost", malformed=mode == "malformed")
                if mode == "unreachable":
                    client.request.side_effect = LumaError("Nomad unavailable")
                with patch("luma.nomad_api.NomadApi", return_value=client), patch.object(bootstrap.time, "monotonic", side_effect=[0, 121]), self.assertRaises(LumaError):
                    bootstrap._install_control_state_file(self.payload, overwrite=False)
                self.assertFalse(database.database_path().exists())
                self.assertFalse((self.root / "control-sqlite-authority.json").exists())

    def test_fresh_and_already_sqlite_installations_do_not_stop_control(self):
        with patch("luma.nomad_api.NomadApi") as client:
            self.assertFalse(bootstrap._install_control_state_file(self.payload, overwrite=False))
            self.assertFalse(bootstrap._install_control_state_file(self.payload, overwrite=False))
            client.assert_not_called()
        self.assertFalse(state.state_path().exists())

    def test_prefetch_precedes_install_and_cutover_disables_auto_revert(self):
        order = []
        config = SimpleNamespace(defaults={"engine": "nomad"}, dns={})
        node = SimpleNamespace(roles=[])
        patches = [
            patch.object(bootstrap, "LocalExecutor", return_value=Mock()),
            patch.object(bootstrap, "_remember_local_manager_node", return_value="manager"),
            patch.object(bootstrap, "_prefetch_control_image_for_manager_refresh", side_effect=lambda *a, **k: order.append("prefetch") or "ready"),
            patch.object(bootstrap, "configure_tailscale_watchdog", return_value="watchdog"),
            patch.object(bootstrap, "sync_nomad_tailscale_service_metadata", return_value="metadata"),
            patch.object(bootstrap, "_traefik_ports", return_value=(80, 443)),
            patch.object(bootstrap, "configure_firewall", return_value="firewall"),
            patch.object(bootstrap, "install_control_config", return_value="config"),
            patch.object(bootstrap, "install_control_state", side_effect=lambda *a, **k: order.append("cutover") or "Control SQLite state installed (legacy cutover pending)"),
            patch.object(bootstrap, "deploy_control_stack", return_value=["healthy"]),
        ]
        mocks = [p.start() for p in patches]
        try:
            bootstrap.refresh_manager_control_local(config, node, "example.com", self.legacy)
            self.assertEqual(order, ["prefetch", "cutover"])
            self.assertFalse(mocks[-1].call_args.kwargs["allow_auto_revert"])
        finally:
            for p in reversed(patches):
                p.stop()


if __name__ == "__main__":
    unittest.main()
