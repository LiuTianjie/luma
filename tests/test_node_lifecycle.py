"""Independent node-agent cutover: supervisor start, verify, and shim rollback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from luma.errors import LumaError
from luma import installation as ins
from luma import node_lifecycle as life


class NodeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.install = self.home / "install"
        self.bin = self.home / "bin"
        self.runtime = self.install / "releases" / "candidate.test" / "venv"
        self.runtime.mkdir(parents=True)
        (self.runtime / "bin").mkdir()
        (self.runtime / "bin" / "python").write_text("#!/bin/sh\n")
        (self.runtime / "bin" / "python").chmod(0o755)
        self.bin.mkdir()
        (self.bin / "luma").write_text("new-shim\n")
        (self.bin / "luma").chmod(0o755)
        ins.backup_shim(self.bin, self.install, previous_runtime="/old/runtime")
        (self.bin / "luma").write_text("new-shim\n")

    def test_backup_and_restore_replace_the_command_shim(self):
        self.assertEqual((ins.lifecycle_dir(self.install) / ins.PREVIOUS_SHIM).read_text(), "new-shim\n")
        (self.bin / "luma").write_text("broken\n")
        ins.restore_shim(self.install, self.bin)
        self.assertEqual((self.bin / "luma").read_text(), "new-shim\n")

    def test_start_cutover_records_operation_and_calls_spawner(self):
        spawned = []
        record = life.start_cutover(
            install_home=self.install,
            bin_dir=self.bin,
            target_runtime=self.runtime,
            target_version="0.1.320",
            previous_runtime="/old/runtime",
            spawner=spawned.append,
        )
        self.assertEqual(record["phase"], "switching")
        self.assertEqual(record["targetVersion"], "0.1.320")
        self.assertEqual(spawned[0]["id"], record["id"])
        stored = ins.read_operation(self.install, record["id"])
        self.assertEqual(stored["nonce"], record["nonce"])

    def test_start_cutover_restores_shim_when_supervisor_cannot_start(self):
        (self.bin / "luma").write_text("candidate-shim\n")

        def fail(_record):
            raise LumaError("systemd-run missing")

        with self.assertRaisesRegex(LumaError, "systemd-run missing"):
            life.start_cutover(
                install_home=self.install,
                bin_dir=self.bin,
                target_runtime=self.runtime,
                target_version="0.1.320",
                spawner=fail,
            )
        self.assertEqual((self.bin / "luma").read_text(), "new-shim\n")

    def test_switch_succeeds_when_new_runtime_stays_up(self):
        record = life.start_cutover(
            install_home=self.install,
            bin_dir=self.bin,
            target_runtime=self.runtime,
            target_version="0.1.320",
            spawner=lambda _record: None,
        )
        clock = {"now": 0.0}

        def sleeper(_seconds):
            clock["now"] += 1

        result = life.switch_operation(
            record,
            restarter=lambda _record: None,
            pid_reader=lambda: 4242,
            runtime_reader=lambda pid: str(self.runtime),
            sleeper=sleeper,
            clock=lambda: clock["now"],
            timeout=20,
        )
        self.assertEqual(result["phase"], "succeeded")
        self.assertEqual((self.bin / "luma").read_text(), "new-shim\n")

    def test_switch_rolls_back_shim_when_new_runtime_never_appears(self):
        (self.bin / "luma").write_text("candidate-shim\n")
        record = life.start_cutover(
            install_home=self.install,
            bin_dir=self.bin,
            target_runtime=self.runtime,
            target_version="0.1.320",
            spawner=lambda _record: None,
        )
        clock = {"now": 0.0}

        def sleeper(_seconds):
            clock["now"] += 10

        with self.assertRaisesRegex(LumaError, "previous command shim restored"):
            life.switch_operation(
                record,
                restarter=lambda _record: None,
                pid_reader=lambda: 7,
                runtime_reader=lambda pid: "/old/runtime",
                sleeper=sleeper,
                clock=lambda: clock["now"],
                timeout=15,
            )
        stored = ins.read_operation(self.install, record["id"])
        self.assertEqual(stored["phase"], "rolled_back")
        self.assertEqual((self.bin / "luma").read_text(), "new-shim\n")

    def test_nonce_observation_counts_as_proof(self):
        record = life.start_cutover(
            install_home=self.install,
            bin_dir=self.bin,
            target_runtime=self.runtime,
            target_version="0.1.320",
            spawner=lambda _record: None,
        )
        observed = {
            "operationId": record["id"],
            "nonce": record["nonce"],
            "runtime": str(self.runtime),
        }
        clock = {"now": 0.0}

        def sleeper(_seconds):
            clock["now"] += 1

        life.wait_for_target(
            record,
            pid_reader=lambda: 99,
            runtime_reader=lambda pid: str(self.runtime),
            observed_reader=lambda _home: observed,
            sleeper=sleeper,
            clock=lambda: clock["now"],
            timeout=20,
        )

    def test_nonce_without_matching_runtime_is_not_proof(self):
        record = life.start_cutover(
            install_home=self.install,
            bin_dir=self.bin,
            target_runtime=self.runtime,
            target_version="0.1.320",
            spawner=lambda _record: None,
        )
        observed = {
            "operationId": record["id"],
            "nonce": record["nonce"],
            "runtime": "/old/runtime",
        }
        clock = {"now": 0.0}

        def sleeper(_seconds):
            clock["now"] += 10

        with self.assertRaisesRegex(LumaError, "did not prove"):
            life.wait_for_target(
                record,
                pid_reader=lambda: None,
                runtime_reader=lambda pid: None,
                observed_reader=lambda _home: observed,
                sleeper=sleeper,
                clock=lambda: clock["now"],
                timeout=15,
            )

    def test_update_luma_install_uses_supervisor_when_target_record_exists(self):
        from luma.agent import update_luma_install

        ins.write_target(
            self.install,
            runtime=self.runtime,
            version="0.1.320",
            source=self.runtime.parent / "src",
            bin_dir=self.bin,
        )
        completed = Mock(returncode=0, stdout="Luma version: 0.1.320\n")
        spawned = []
        with patch("luma.agent.subprocess.run", return_value=completed), patch(
            "luma.agent._current_install_layout",
            return_value=(self.home, self.install, self.bin),
        ), patch("luma.agent.LocalExecutor") as executor, patch(
            "luma.agent.node_agent_os", return_value="linux"
        ), patch(
            "luma.agent._installed_luma_executable", return_value=str(self.bin / "luma")
        ), patch(
            "luma.node_lifecycle.spawn_supervisor", side_effect=spawned.append
        ):
            result = update_luma_install(install_ref="v0.1.320", config_path=Path("/opt/luma/node-agent/agent.json"))
        self.assertTrue(result["restartAgent"])
        self.assertTrue(result["lifecycleOperationId"])
        self.assertIn("cutover scheduled", result["message"])
        self.assertGreaterEqual(executor.return_value.sudo.call_count, 1)
        self.assertEqual(len(spawned), 1)
