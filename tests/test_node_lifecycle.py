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


class AgentSecurityAndReliabilityTests(unittest.TestCase):
    def test_configure_docker_egress_proxy_rejects_systemd_injection(self):
        from luma.agent import configure_docker_egress_proxy
        from luma.errors import LumaError

        bad_proxies = [
            "http://127.0.0.1:7890\nExecStart=/bin/sh",
            "http://127.0.0.1:7890\rEvil=1",
            'http://127.0.0.1:7890" "INJECT=1',
            "http://127.0.0.1:7890\\evil",
            "http://127.0.0.1:7890\x00evil",
            "http://127.0.0.1:7890 extra",
        ]
        for proxy in bad_proxies:
            with self.subTest(proxy=proxy), self.assertRaises(LumaError):
                configure_docker_egress_proxy(proxy=proxy, no_proxy="localhost")

        bad_no_proxies = [
            "localhost\nExecStart=/bin/sh",
            'localhost"INJECT=1',
            "localhost\\bad",
            "localhost\x07",
        ]
        for no_proxy in bad_no_proxies:
            with self.subTest(no_proxy=no_proxy), self.assertRaises(LumaError):
                configure_docker_egress_proxy(proxy="http://127.0.0.1:7890", no_proxy=no_proxy)

    def test_managed_volume_path_rejects_dangerous_host_roots_and_shallow_paths(self):
        from luma.agent import _remove_volume_path_command, _volume_path_command, execute_agent_task
        from luma.errors import LumaError

        dangerous_roots = [
            "/",
            "/etc",
            "/etc/luma",
            "/usr",
            "/usr/local",
            "/bin/foo",
            "/sbin/foo",
            "/lib/foo",
            "/var",
            "/root",
            "/root/data",
            "/home",
            "/home/alice",
            "/boot/data",
            "/sys/data",
            "/proc/data",
            "/dev/data",
            "/opt/luma",
            "/opt/luma/control",
            "/opt/nomad",
            "/opt/nomad/data",
            "/srv",
        ]
        for root in dangerous_roots:
            with self.subTest(root=root):
                with self.assertRaises(LumaError):
                    _volume_path_command(root)
                with self.assertRaises(LumaError):
                    _remove_volume_path_command(root, "app")
                with self.assertRaises(LumaError):
                    execute_agent_task({"action": "prepare-managed-volume-path", "payload": {"root": root, "relative": "app"}})
                with self.assertRaises(LumaError):
                    execute_agent_task({"action": "remove-managed-volume-path", "payload": {"root": root, "relative": "app"}})

        with self.assertRaises(LumaError):
            _remove_volume_path_command("/srv/luma", ".")

        cmd = _remove_volume_path_command("/srv/luma", "app/data")
        self.assertIn("/srv/luma/app/data", cmd)

    def test_report_agent_task_result_stops_on_terminal_4xx_and_retries_transient(self):
        import io
        import urllib.error
        from luma.agent import _report_agent_task_result
        from luma.errors import LumaError

        terminal_errors = [
            LumaError("agent task not found: task-1"),
            LumaError("task already completed"),
            urllib.error.HTTPError("https://ctrl/v1", 404, "Not Found", {}, io.BytesIO(b"")),
            urllib.error.HTTPError("https://ctrl/v1", 409, "Conflict", {}, io.BytesIO(b"")),
            urllib.error.HTTPError("https://ctrl/v1", 410, "Gone", {}, io.BytesIO(b"")),
        ]
        for exc in terminal_errors:
            with self.subTest(exc=str(exc)):
                client = Mock()
                client.complete_agent_task.side_effect = exc
                with patch("luma.agent.time.sleep") as sleep, patch("sys.stderr"):
                    _report_agent_task_result(
                        client,
                        task_id="task-1",
                        node_name="n1",
                        node_id="id-1",
                        status="succeeded",
                        message="ok",
                        result={},
                    )
                self.assertEqual(client.complete_agent_task.call_count, 1)
                sleep.assert_not_called()

        # Transient 502 and 429 must still retry until acknowledged.
        client = Mock()
        client.complete_agent_task.side_effect = [
            urllib.error.HTTPError("https://ctrl/v1", 502, "Bad Gateway", {}, io.BytesIO(b"")),
            urllib.error.HTTPError("https://ctrl/v1", 429, "Too Many Requests", {}, io.BytesIO(b"")),
            {"ok": True},
        ]
        with patch("luma.agent.time.sleep") as sleep, patch("sys.stderr"):
            _report_agent_task_result(
                client,
                task_id="task-2",
                node_name="n1",
                node_id="id-1",
                status="succeeded",
                message="ok",
                result={},
            )
        self.assertEqual(client.complete_agent_task.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_start_agent_task_heartbeat_respects_configured_interval_without_2s_clamp(self):
        import json
        import tempfile
        import threading
        from luma.agent import _start_agent_task_heartbeat, _stop_agent_task_heartbeat

        client = Mock()
        client.heartbeat_agent.return_value = {}
        waited = []
        original_event = threading.Event

        class RecordingEvent(original_event):
            def wait(self, timeout=None):
                if timeout is not None:
                    waited.append(timeout)
                    self.set()
                    return True
                return super().wait(timeout)

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 30}), encoding="utf-8")
            with patch("luma.agent.threading.Event", RecordingEvent), \
                    patch("luma.agent.node_agent_metrics", return_value={}):
                stop, thread = _start_agent_task_heartbeat(
                    client,
                    node_name="n1",
                    node_id="id-1",
                    active_task_id="task-1",
                    cancel_event=original_event(),
                    config_path=config,
                )
                thread.join(timeout=2)
                _stop_agent_task_heartbeat(stop, thread)

        self.assertEqual(waited, [30.0])

    def test_streaming_buffers_output_and_caps_memory(self):
        import sys
        from luma.agent import _BoundedOutputBuffer, _run_command_streaming, _run_process_streaming

        buf = _BoundedOutputBuffer(max_chars=10)
        buf.append("abcdef")
        buf.append("12345678")
        self.assertEqual(buf.text(), "ef12345678")

        lines = []
        with patch("luma.agent._MAX_STREAM_OUTPUT_CHARS", 128):
            res = _run_process_streaming(
                [sys.executable, "-c", "for i in range(50): print(f'line-{i:03d}-' + 'x'*20)"],
                timeout=10,
                on_line=lines.append,
            )
        self.assertEqual(res.code, 0)
        self.assertEqual(len(lines), 50)
        self.assertLessEqual(len(res.output), 128)
        self.assertIn("line-049-", res.output)

        cmd_lines = []
        with patch("luma.agent._MAX_STREAM_OUTPUT_CHARS", 64):
            cmd_res = _run_command_streaming(
                "printf 'first\\nsecond\\nthird\\n' && printf 'z%.0s' {1..200}",
                timeout=10,
                on_line=cmd_lines.append,
            )
        self.assertEqual(cmd_res.code, 0)
        self.assertLessEqual(len(cmd_res.output), 64)
        self.assertIn("first", cmd_lines)

    def test_progress_reporter_and_terminal_outbound_queues_are_bounded(self):
        import asyncio
        from luma.agent import _AgentTaskProgressReporter, _BoundedOutboundQueue

        client = Mock()
        client.progress_agent_task.side_effect = RuntimeError("down")
        with patch("luma.agent.time.sleep"), patch("sys.stderr"):
            reporter = _AgentTaskProgressReporter(
                client, task_id="t1", node_name="n1", node_id="id1", max_events=8, flush_interval=0.02
            )
            self.assertEqual(reporter.events.maxsize, 8)
            for i in range(50):
                reporter.submit({"line": f"m-{i}"})
            reporter.close()
            # After close/stop, submit is a no-op and does not grow queue.
            reporter.submit({"line": "after-close"})
            self.assertEqual(reporter.events.qsize(), 0)

        async def check_outbound():
            q = _BoundedOutboundQueue(maxsize=4)
            for i in range(10):
                q.put_nowait({"seq": i})
            self.assertEqual(q.qsize(), 4)
            items = [q.get_nowait()["seq"] for _ in range(4)]
            self.assertEqual(items, [6, 7, 8, 9])

        asyncio.run(check_outbound())

    def test_pty_session_master_fd_closed_only_once(self):
        import threading
        from luma.agent import _PtySession

        session = _PtySession.__new__(_PtySession)
        session.closed = threading.Event()
        session._fd_lock = threading.Lock()
        session.master_fd = 42
        session.process = Mock()
        session.process.poll.return_value = 0

        with patch("luma.agent.os.close") as close_fn:
            session._close_master_fd()
            session._close_master_fd()
            session.close()
        close_fn.assert_called_once_with(42)
        self.assertIsNone(session.master_fd)

    def test_macos_reload_log_uses_private_config_dir_not_tmp(self):
        from luma.agent import update_luma_install

        executor = Mock()
        completed = Mock(returncode=0, stdout="Luma version: 0.1.320\n")
        with patch("luma.agent.subprocess.run", return_value=completed), \
                patch("luma.agent.LocalExecutor", return_value=executor), \
                patch("luma.agent.node_agent_os", return_value="darwin"), \
                patch("luma.agent._current_install_layout", return_value=None), \
                patch("luma.agent._installed_luma_executable", return_value="/opt/luma/bin/luma"):
            update_luma_install(install_ref="v0.1.320", config_path=Path("/opt/luma/node-agent/agent.json"))

        cmd = executor.sudo.call_args_list[0].args[0]
        self.assertNotIn("/tmp/luma-node-agent-reload.log", cmd)
        self.assertIn("/opt/luma/node-agent/node-agent-reload.log", cmd)
        self.assertIn("install -d -m 0700", cmd)
