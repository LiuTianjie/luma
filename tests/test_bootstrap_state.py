from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from luma.bootstrap import _install_control_state_file, configure_tailscale_watchdog, install_control_state
from luma.control import database
from luma.control.state import load_state, mutate_state, save_state, state_path


class LocalInstallExecutor:
    """Run only generated state-install commands against an isolated directory."""
    def write_secret(self, content, path, mode="600"):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(int(mode, 8))
        return "written"

    def sudo(self, command, check=True):
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout


def watchdog_reader():
    remote = Mock()
    remote.run_result.return_value = Mock(code=0, output="Linux\n")
    configure_tailscale_watchdog(remote)
    command = remote.sudo.call_args.args[0]
    return command.split('peers=$(python3 - "$control_state" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]


class BootstrapStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": str(self.directory)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.initial = {"clusterId": "cluster", "domain": "old.example", "deployToken": "private-test-token",
                        "nodes": {"manager": {"nomadRole": "server", "agent": {"lastSeen": 1}}},
                        "buildRuns": {}, "secrets": {"APP_SECRET": "old", "CLOUDFLARE_API_TOKEN": "old-dns"}}

    def test_installer_seeds_sqlite_without_printing_credentials(self):
        self.assertEqual(install_control_state(LocalInstallExecutor(), self.initial), "Control SQLite state installed")
        self.assertEqual(load_state(), self.initial)
        self.assertTrue(database.database_path().exists())
        self.assertEqual(list(self.directory.glob(".control-install-*")), [])
        self.assertFalse(state_path().exists())
        self.assertFalse((self.directory / "control.json.pre-sqlite.bak").exists())
        self.assertFalse((self.directory / "control-sqlite-migration.json").exists())
        self.assertTrue(database.check_database()["ok"])
        with database.transaction(immediate=False) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertIsNone(conn.execute("SELECT value FROM database_meta WHERE key='legacy_import'").fetchone())

    def test_fresh_bootstrap_then_update_retains_identity_without_legacy_state(self):
        install_control_state(LocalInstallExecutor(), self.initial)
        identity = (self.directory / "control-sqlite-authority.json").read_bytes()
        incoming = {**self.initial, "domain": "new.example", "deployToken": "must-not-replace"}
        install_control_state(LocalInstallExecutor(), incoming)
        current = load_state()
        self.assertEqual(current["domain"], "new.example")
        self.assertEqual(current["deployToken"], self.initial["deployToken"])
        self.assertEqual((self.directory / "control-sqlite-authority.json").read_bytes(), identity)
        self.assertFalse(state_path().exists())

    def test_fresh_installer_refuses_to_reset_lost_database(self):
        install_control_state(LocalInstallExecutor(), self.initial)
        database.database_path().unlink()
        with self.assertRaisesRegex(RuntimeError, "restore a verified backup"):
            install_control_state(LocalInstallExecutor(), self.initial)

    def test_refresh_keeps_concurrent_runtime_writes_and_updates_manager_config(self):
        save_state(self.initial)
        incoming = load_state()
        incoming["domain"] = "new.example"
        incoming["secrets"]["CLOUDFLARE_API_TOKEN"] = "new-dns"
        incoming["nodes"]["manager"]["tailscaleIP"] = "100.64.0.10"
        def update(state):
            state["buildRuns"]["recent"] = {"id": "recent", "status": "running"}
            state["nodes"]["manager"]["agent"] = {"lastSeen": 999}
            state["secrets"]["APP_SECRET"] = "new-app"
        mutate_state(update)
        install_control_state(LocalInstallExecutor(), incoming)
        current = load_state()
        self.assertEqual(current["domain"], "new.example")
        self.assertIn("recent", current["buildRuns"])
        self.assertEqual(current["nodes"]["manager"]["agent"]["lastSeen"], 999)
        self.assertEqual(current["nodes"]["manager"]["tailscaleIP"], "100.64.0.10")
        self.assertEqual(current["secrets"], {"APP_SECRET": "new-app", "CLOUDFLARE_API_TOKEN": "new-dns"})
        self.assertFalse(state_path().exists())

    def test_different_cluster_requires_explicit_overwrite(self):
        save_state(self.initial)
        replacement = {"clusterId": "replacement", "domain": "new.example", "deployToken": "different"}
        with self.assertRaises(RuntimeError):
            install_control_state(LocalInstallExecutor(), replacement)
        self.assertEqual(load_state(), self.initial)
        install_control_state(LocalInstallExecutor(), replacement, overwrite=True)
        self.assertEqual(load_state(), replacement)

    def test_installer_imports_legacy_without_mutating_the_source(self):
        state_path().write_text(json.dumps(self.initial))
        legacy = state_path().read_bytes()
        incoming = {**self.initial, "domain": "updated.example"}
        request = self.directory / ".install.tmp"
        request.write_text(json.dumps({"state": incoming, "secretNames": []}))
        with patch("luma.bootstrap._stop_legacy_control_writer", return_value=None):
            _install_control_state_file(request, overwrite=False)
        self.assertEqual(load_state()["domain"], "updated.example")
        self.assertEqual(state_path().read_bytes(), legacy)
        self.assertEqual((self.directory / "control.json.pre-sqlite.bak").read_bytes(), legacy)

    def _run_reader(self):
        return subprocess.run([sys.executable, "-I", "-c", watchdog_reader(), str(state_path())],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_watchdog_reads_current_sqlite_nodes_without_importing_luma(self):
        now = int(time.time())
        initial = {**self.initial, "nodes": {"old": {"tailscaleIP": "100.64.0.1", "agent": {"status": "online", "lastSeen": now}}}}
        save_state(initial)
        current = {**initial, "nodes": {"current": {"tailscaleIP": "100.64.0.2", "agent": {"status": "online", "lastSeen": now}}}}
        save_state(current)
        before = database.database_path().read_bytes()
        self.assertEqual(self._run_reader(), "100.64.0.2")
        self.assertEqual(database.database_path().read_bytes(), before)

    def test_watchdog_never_falls_back_after_sqlite_cutover(self):
        now = int(time.time())
        self.initial["nodes"] = {"old": {"tailscaleIP": "100.64.0.1", "agent": {"status": "online", "lastSeen": now}}}
        save_state(self.initial)
        database.database_path().unlink()
        self.assertEqual(self._run_reader(), "")
        database.database_path().write_bytes(b"corrupt sqlite")
        self.assertEqual(self._run_reader(), "")

    def test_watchdog_legacy_fallback_before_migration(self):
        self.initial["nodes"] = {"legacy": {"tailscaleIP": "100.64.0.1", "agent": {"status": "online", "lastSeen": int(time.time())}}}
        state_path().write_text(json.dumps(self.initial))
        self.assertEqual(self._run_reader(), "100.64.0.1")

    def test_watchdog_rejects_database_identity_mismatch(self):
        self.initial["nodes"] = {"node": {"tailscaleIP": "100.64.0.1", "agent": {"status": "online", "lastSeen": int(time.time())}}}
        save_state(self.initial)
        (self.directory / "control-sqlite-authority.json").write_text('{"databaseId":"different"}')
        self.assertEqual(self._run_reader(), "")


if __name__ == "__main__":
    unittest.main()
