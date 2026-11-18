"""Runner enrollment, service ownership and terminal authentication failures."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from luma import runner_service as service
from luma.builder_runner import RunnerDaemon
from luma.cli import main
from luma.errors import LumaError
from luma.runner_logging import rotating_output


OPTIONS = dict(server_url="https://build.example.test", runner_token="lbr_private_token",
               state_dir="/home/build/.luma-runner", local_proxy="", local_no_proxy="", cache_mode="",
               cache_keep_gb=20, max_parallelism=0, workers=1, insecure_tls=False)


class RunnerServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="luma-runner-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for key, path in (("CONFIG", self.root / "config.json"), ("UNIT_PATH", self.root / "runner.service"), ("PLIST", self.root / "runner.plist")):
            manager = patch.object(service, key, path)
            manager.start()
            self.addCleanup(manager.stop)
        self.owner = {"name": "build", "uid": os.geteuid(), "gid": os.getgid(), "home": str(self.root)}
        self.env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin", "DOCKER_CONTEXT": "orbstack"}
        self.command = [str(self.root / "bin/luma")]
        self.config = dict(schemaVersion=1, owner=self.owner, options=OPTIONS, command=self.command, environment=self.env)

    def test_systemd_join_start_stop_and_restart_reuse_one_private_config(self):
        state = {"running": False, "enabled": False}
        calls = []

        def execute(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "show":
                text = (f"ActiveState={'active' if state['running'] else 'inactive'}\n"
                        f"SubState={'running' if state['running'] else 'dead'}\nMainPID={123 if state['running'] else 0}\n"
                        f"UnitFileState={'enabled' if state['enabled'] else 'disabled'}\n")
                return subprocess.CompletedProcess(argv, 0, text, "")
            if argv[1] == "enable":
                state["enabled"] = True
            if argv[1] in {"start", "restart"}:
                state["running"] = True
            if argv[1] == "disable":
                state.update(running=False, enabled=False)
            return subprocess.CompletedProcess(argv, 0, "", "")

        def install(path, content, *, mode, owner=None):
            path.write_bytes(content)
            path.chmod(int(mode, 8))

        with patch.object(service, "platform_backend", return_value="systemd"), \
             patch.object(service, "_identity", return_value=self.owner), \
             patch.object(service, "_executable", return_value=(self.command, {})), \
             patch.object(service, "_environment", return_value=self.env), \
             patch.object(service, "_install_file", side_effect=install) as writes, \
             patch.object(service, "_command", side_effect=execute):
            self.assertTrue(service.start(OPTIONS, configure=True)["running"])
            self.assertEqual(service.CONFIG.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(OPTIONS["runner_token"], service.UNIT_PATH.read_text())
            self.assertNotIn("DOCKER_CONTEXT", service.UNIT_PATH.read_text())
            self.assertEqual(service.load_config()["environment"]["DOCKER_CONTEXT"], "orbstack")
            self.assertEqual(writes.call_count, 2)
            self.assertTrue(service.start(OPTIONS, configure=True)["running"])
            self.assertEqual(writes.call_count, 2)
            fresh_env = {**self.env, "HTTPS_PROXY": "http://127.0.0.1:7890", "DOCKER_CONTEXT": "updated-context"}
            with patch.object(service, "_environment", return_value=fresh_env):
                service.start(OPTIONS, configure=True)
            self.assertEqual(service.load_config()["environment"], fresh_env)
            self.assertFalse(service.stop()["running"])
            self.assertFalse(state["enabled"])
            with patch.object(service, "_executable", side_effect=AssertionError("must retain the original install")), \
                 patch.object(service, "_environment", side_effect=AssertionError("start must retain the joined environment")):
                self.assertTrue(service.start(OPTIONS)["running"])
                self.assertTrue(service.start(OPTIONS, restart=True)["running"])
            self.assertIn(["systemctl", "restart", service.UNIT], calls)

    def test_launchd_service_keeps_user_context_and_does_not_restart_auth_rejection(self):
        content = service.service_definition(self.config, "launchd")
        plist = plistlib.loads(content)
        self.assertEqual(plist["UserName"], "build")
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["ProgramArguments"][0], self.command[0])
        self.assertNotIn(OPTIONS["runner_token"].encode(), content)
        unit = service.service_definition(self.config, "systemd").decode()
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("KillMode=control-group", unit)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("systemd-analyze"), "requires the real systemd unit parser")
    def test_generated_unit_passes_real_systemd_verification(self):
        spaced_home = self.root / "home with spaces%"
        spaced_home.mkdir()
        for home in ("/root", str(spaced_home)):
            with self.subTest(home=home):
                config = {**self.config, "command": [shutil.which("true")],
                          "owner": {**service._identity(), "home": home}}
                service.UNIT_PATH.write_bytes(service.service_definition(config, "systemd"))
                result = subprocess.run(["systemd-analyze", "verify", str(service.UNIT_PATH)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_service_command_reports_operation_and_redacts_credentials(self):
        argv = ["systemctl", "start", service.UNIT]
        error = ('Unit has a bad setting: WorkingDirectory="/root"\n'
                 'ExecStart=/bin/luma --token old-private-value\n'
                 'Rejected lbr_private-token and Bearer other-private-value')
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess(argv, 1, "", error)):
            with self.assertRaises(LumaError) as raised:
                service._command(argv)
        message = str(raised.exception)
        self.assertIn("systemctl start", message)
        self.assertIn('WorkingDirectory="/root"', message)
        for secret in ("old-private-value", "lbr_private-token", "other-private-value"):
            self.assertNotIn(secret, message)

    def test_missing_launchd_log_reports_installation_failure_without_running_tail(self):
        with patch.object(service, "platform_backend", return_value="launchd"), patch.object(
            service, "_command", return_value=subprocess.CompletedProcess([], 1, "", ""),
        ) as command:
            with self.assertRaisesRegex(LumaError, "log has not been created yet"):
                service.logs()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][0], "test")

    def test_managed_install_uses_registered_shim_instead_of_path(self):
        bindir = self.root / "managed/bin"
        bindir.mkdir(parents=True)
        shim = bindir / "luma"
        shim.write_text("#!/bin/sh\n")
        shim.chmod(0o755)
        with patch.object(service, "runtime_record", return_value={"binDir": str(bindir)}), \
             patch.dict(os.environ, {"PATH": "/root/stale/bin"}):
            self.assertEqual(service._executable(), ([str(shim)], {}))

    def test_join_verifies_token_before_installing_and_reuses_saved_options(self):
        service.CONFIG.write_text(json.dumps(self.config))
        daemon = Mock()
        daemon._post.return_value = {"runner": {"id": "rn-1", "name": "existing"}}
        installed = dict(backend="systemd", state="active", running=True)
        with patch("luma.cli.main.auto_sync"), patch("luma.cli.main._load_environment"), \
             patch("luma.cli.runner.RunnerDaemon", return_value=daemon) as create, \
             patch.object(service, "platform_backend", return_value="systemd"), \
             patch.object(service, "start", return_value=installed) as start, \
             patch("luma.cli.builder._load_saved_builder_url", return_value=""), \
             patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["runner", "join", "--format", "json"]), 0)
            daemon._post.assert_called_once_with("/v1/runner/join", {})
            create.assert_called_once_with(**OPTIONS)
            start.assert_called_once_with(OPTIONS, configure=True)
            self.assertEqual(json.loads(out.getvalue())["result"]["runner"], "existing")
            self.assertNotIn(OPTIONS["runner_token"], out.getvalue())

    def test_failed_join_leaves_existing_service_untouched(self):
        import urllib.error
        service.CONFIG.write_text(json.dumps(self.config))
        before = service.CONFIG.read_bytes()
        daemon = Mock()
        daemon._post.side_effect = urllib.error.HTTPError("https://build.test", 401, "Unauthorized", {}, None)
        with patch("luma.cli.main.auto_sync"), patch("luma.cli.main._load_environment"), \
             patch("luma.cli.runner.RunnerDaemon", return_value=daemon), \
             patch.object(service, "platform_backend", return_value="systemd"), \
             patch.object(service, "start") as start, \
             patch("luma.cli.builder._load_saved_builder_url", return_value=""), \
             patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["runner", "join", "--token", "lbr_invalid"]), 1)
            start.assert_not_called()
            self.assertEqual(service.CONFIG.read_bytes(), before)
            self.assertNotIn("lbr_invalid", err.getvalue())


class RunnerExitTests(unittest.TestCase):
    def test_stopping_idle_runner_interrupts_outstanding_http_long_poll(self):
        started, finish = threading.Event(), threading.Event()

        class Server(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                started.set()
                finish.wait(10)
                with contextlib.suppress(OSError):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"task": null}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(finish.set)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp, \
             patch("luma.builder_runner.find_docker_binary", return_value="docker"), \
             patch("luma.builder_runner.capture_docker_runtime", return_value={}), \
             patch("luma.builder_runner.detect_native_engine_platform", return_value="linux/amd64"):
            runner = RunnerDaemon(server_url=f"http://127.0.0.1:{server.server_port}", runner_token="lbr_test", state_dir=Path(tmp))
            thread = threading.Thread(target=runner._worker_loop)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                runner.stop_event.set()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive(), "stop must not wait for the server's long-poll response")
            finally:
                finish.set()
                thread.join(timeout=3)

    def test_macos_output_rotates_and_retains_latest_stdout_and_stderr(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "runner.log"
            with rotating_output(log, max_bytes=128, backups=2):
                for _ in range(40):
                    print("build output " * 3)
                print("latest stdout")
                print("latest stderr", file=sys.stderr)
            files = list(Path(tmp).glob("runner.log*"))
            self.assertEqual(len(files), 3)
            self.assertLessEqual(sum(p.stat().st_size for p in files), 3 * 128)
            self.assertIn("latest stdout", log.read_text())
            self.assertIn("latest stderr", log.read_text())

    def test_auth_rejection_during_build_cancels_work_and_exits(self):
        import urllib.error
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://build.test", 401, "Unauthorized", {}, None)

        def build(*args, cancel_event, **kwargs):
            self.assertTrue(cancel_event.wait(5), "rejected heartbeat must cancel active work")
            raise LumaError("canceled")

        with tempfile.TemporaryDirectory() as tmp, \
             patch("luma.builder_runner.find_docker_binary", return_value="docker"), \
             patch("luma.builder_runner.capture_docker_runtime", return_value={}), \
             patch("luma.builder_runner.detect_native_engine_platform", return_value="linux/amd64"), \
             patch("luma.builder_runner.urllib.request.build_opener", return_value=opener), \
             patch("luma.builder_runner.execute_build_task", side_effect=build), \
             contextlib.redirect_stderr(io.StringIO()):
            runner = RunnerDaemon(server_url="https://build.test", runner_token="lbr_test", state_dir=Path(tmp))
            runner._run_leased_task({"buildId": "bld-test", "leaseId": "lease-test"})
            self.assertTrue(runner.stop_event.is_set())
            self.assertTrue(runner.credential_rejected)

    def test_deleted_token_exits_but_transient_server_failure_retries(self):
        requests = []

        class Server(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(self.path)
                self.send_response(503 if len(requests) == 1 else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp, \
             patch("luma.builder_runner.find_docker_binary", return_value="docker"), \
             patch("luma.builder_runner.capture_docker_runtime", return_value={}), \
             patch("luma.builder_runner.detect_native_engine_platform", return_value="linux/amd64"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            runner = RunnerDaemon(server_url=f"http://127.0.0.1:{server.server_port}", runner_token="lbr_test",
                                  state_dir=Path(tmp), poll_interval=0.5)
            self.assertEqual(runner.run_once_or_forever(), 0)
            self.assertTrue(runner.stop_event.is_set())
            self.assertTrue(runner.credential_rejected)
            self.assertEqual(requests, ["/v1/runner/lease", "/v1/runner/lease"])
            self.assertNotIn("lbr_test", err.getvalue())

    def test_start_without_join_gives_actionable_error(self):
        with patch.object(service, "CONFIG", Path("/nonexistent/luma-runner.json")):
            with self.assertRaisesRegex(LumaError, "luma runner join"):
                service.load_config(required=True)
