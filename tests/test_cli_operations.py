from __future__ import annotations

import io
import http.client
import json
import os
import tempfile
import urllib.error
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from luma.cli import build_parser, main
from luma.control.client import ControlClient
from luma.control.context import current_context_name, load_current_context, save_context
from luma.errors import LumaError


class CliOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LUMA_CONFIG_HOME": self.temp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = Mock()
        self.factory = patch("luma.cli.ControlClient", return_value=self.client)
        self.client_cls = self.factory.start()
        self.addCleanup(self.factory.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--no-env", *args])
        return code, out.getvalue(), err.getvalue()

    def credentials(self):
        os.environ.update(LUMA_CONTROL_URL="https://control.example.com", LUMA_DEPLOY_TOKEN="secret-token")

    def test_stateless_doctor_json_reports_failed_checks_without_progress(self):
        self.credentials()
        self.client.verify_login.return_value = {"clusterId": "test"}
        self.client.status.return_value = {"dns": {"ready": False}, "nomad": {"available": False}}
        code, out, err = self.run_cli("doctor", "--format", "json")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        result = json.loads(out)["result"]
        self.assertFalse(result["healthy"])
        self.assertTrue(any(not check["ok"] for check in result["checks"]))
        self.assertNotIn("secret-token", out)
        self.client.status.assert_called_once()

    def test_doctor_missing_auth_is_structured(self):
        code, out, err = self.run_cli("doctor", "--format", "ndjson")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["type"], "result")
        self.assertFalse(json.loads(out)["result"]["healthy"])
        self.assertEqual(err, "")
        self.client_cls.assert_not_called()

    def test_restart_and_remove_stateless_json_are_single_results(self):
        self.credentials()
        for operation in ("restart", "remove"):
            with self.subTest(operation=operation):
                self.client.restart_application.return_value = {"mode": "recreate", "restarted": []}
                self.client.remove_service.return_value = {"service": "api", "steps": [{"name": "remove", "status": "ok"}]}
                code, out, err = self.run_cli("service", operation, "api", "--format", "json")
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                self.assertEqual(json.loads(out)["command"], "service " + operation)
                self.assertEqual(len(out.splitlines()), 1)

    def test_remove_quiet_suppresses_steps_and_heartbeat(self):
        self.credentials()
        self.client.remove_service.return_value = {"service": "api", "steps": [{"name": "private-progress", "status": "ok"}]}
        code, out, err = self.run_cli("service", "remove", "api", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("Remove finished: api", out)
        self.assertNotIn("private-progress", out)
        self.assertNotIn("[start]", out)

    def test_errors_go_to_stderr_as_json(self):
        self.credentials()
        self.client.restart_application.side_effect = LumaError("application not found")
        code, out, err = self.run_cli("service", "restart", "missing", "--format", "json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["message"], "application not found")

    def test_named_context_does_not_switch_current(self):
        save_context(endpoint="https://first.example.com", cluster_id="first", token="first-token")
        save_context(endpoint="https://second.example.com", cluster_id="second", token="second-token")
        self.client.dashboard.return_value = {"services": []}
        code, out, err = self.run_cli("service", "list", "--control-context", "first", "--format", "json")
        self.assertEqual(code, 0)
        self.client_cls.assert_called_once_with("https://first.example.com", "first-token", insecure=False, resolve_ip=None)
        self.assertEqual(current_context_name(), "second")
        self.assertNotIn("first-token", out)

    def test_url_override_does_not_reuse_other_cluster_credentials(self):
        save_context(endpoint="https://first.example.com", cluster_id="first", token="first-secret", insecure=True, resolve_ip="127.0.0.1")
        code, out, err = self.run_cli("service", "list", "--control-url", "https://second.example.com", "--format", "json")
        self.assertEqual(code, 1)
        self.client_cls.assert_not_called()
        self.assertNotIn("first-secret", out + err)
        self.client.dashboard.return_value = {"services": []}
        code, out, err = self.run_cli("service", "list", "--control-url", "https://second.example.com", "--token", "second-token", "--format", "json")
        self.assertEqual(code, 0)
        self.client_cls.assert_called_once_with("https://second.example.com", "second-token", insecure=False, resolve_ip=None)

    def test_named_context_environment_and_cli_override(self):
        save_context(endpoint="https://first.example.com", cluster_id="first", token="first-token")
        save_context(endpoint="https://second.example.com", cluster_id="second", token="second-token")
        os.environ["LUMA_CONTROL_CONTEXT"] = "first"
        self.client.dashboard.return_value = {"services": []}
        self.run_cli("service", "list", "--format", "json")
        self.assertEqual(self.client_cls.call_args.args[0], "https://first.example.com")
        self.run_cli("service", "list", "--control-context", "second", "--format", "json")
        self.assertEqual(self.client_cls.call_args.args[0], "https://second.example.com")

    def test_context_json_never_contains_token(self):
        save_context(endpoint="https://first.example.com", cluster_id="first", token="must-not-appear")
        for args in (("context", "list"), ("context", "use", "first")):
            code, out, err = self.run_cli(*args, "--format", "json")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["ok"])
            self.assertNotIn("must-not-appear", out + err)

    def test_login_stdin_keeps_token_out_of_output_and_secure_on_disk(self):
        self.client.verify_login.return_value = {"clusterId": "test"}
        with patch("sys.stdin", io.StringIO("stdin-secret\n")):
            code, out, err = self.run_cli("login", "https://control.example.com", "--token-stdin", "--format", "json")
        self.assertEqual(code, 0)
        self.assertNotIn("stdin-secret", out + err)
        self.assertEqual(load_current_context()["token"], "stdin-secret")
        self.assertEqual((Path(self.temp.name) / "contexts/test.json").stat().st_mode & 0o777, 0o600)

    def test_login_hidden_prompt_and_env_sources(self):
        self.client.verify_login.return_value = {"clusterId": "test"}
        with patch("sys.stdin.isatty", return_value=True), patch("luma.cli.getpass.getpass", return_value="prompt-token") as prompt:
            code, out, err = self.run_cli("login", "https://control.example.com")
        self.assertEqual(code, 0)
        prompt.assert_called_once()
        self.assertEqual(load_current_context()["token"], "prompt-token")
        os.environ["LUMA_DEPLOY_TOKEN"] = "env-token"
        with patch("luma.cli.getpass.getpass") as prompt:
            code, out, err = self.run_cli("login", "https://control.example.com")
        prompt.assert_not_called()
        self.assertEqual(load_current_context()["token"], "env-token")

    def test_empty_stdin_token_fails_without_falling_back_to_env(self):
        self.credentials()
        with patch("sys.stdin", io.StringIO("\n")):
            code, out, err = self.run_cli("login", "https://control.example.com", "--token-stdin", "--format", "json")
        self.assertEqual(code, 1)
        self.client_cls.assert_not_called()
        self.assertEqual(out, "")

    def test_list_filters_and_inspect_keeps_all_application_services(self):
        self.credentials()
        services = [
            {"name": "api", "fullName": "shop_api", "stack": "shop", "region": "cn"},
            {"name": "db", "fullName": "shop_db", "stack": "shop", "region": "cn"},
            {"name": "worker", "fullName": "worker", "stack": "worker", "region": "global"},
        ]
        self.client.dashboard.return_value = {"services": services}
        code, out, _ = self.run_cli("service", "list", "--region", "global", "--format", "json")
        self.assertEqual(json.loads(out)["result"]["services"], services[2:])
        code, out, _ = self.run_cli("service", "inspect", "shop", "--format", "json")
        self.assertEqual(json.loads(out)["result"]["services"], services[:2])
        code, out, err = self.run_cli("service", "inspect", "missing", "--format", "json")
        self.assertEqual(code, 1)
        self.assertIn("not found", json.loads(err)["error"]["message"])

    def test_events_uses_existing_runtime_api_result(self):
        self.credentials()
        self.client.service_events.return_value = {"events": [{"type": "Started", "message": "Task started"}]}
        code, out, _ = self.run_cli("service", "events", "shop_api", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"]["events"][0]["type"], "Started")
        self.client.service_events.assert_called_once_with("shop_api")

    def test_logs_snapshot_and_follow(self):
        self.credentials()
        self.client.service_logs.return_value = {"logs": ["hello"], "warnings": ["Logs are bounded"]}
        code, out, err = self.run_cli("service", "logs", "shop_api", "--allocation", "a", "--previous")
        self.assertEqual(out, "[source unavailable] hello\n")
        self.assertIn("Logs are bounded", err)
        self.client.service_logs.assert_called_once_with("shop_api", tail=120, allocation="a", previous=True)
        events = [{"status": "start"}, {"line": "hello", "cursor": "cursor-1"}]
        self.client.service_log_events.return_value = iter(events)
        code, out, err = self.run_cli("service", "logs", "shop_api", "-f", "--format", "ndjson")
        self.assertEqual([json.loads(line) for line in out.splitlines()], events)

    def test_text_logs_label_interleaved_sources_and_fragments(self):
        self.credentials()
        entries = [
            {"allocationId": "alloc-a", "task": "web", "stream": "stdout", "line": "first", "partial": True},
            {"allocationId": "alloc-b", "task": "worker", "stream": "stderr", "line": "other source"},
            {"allocationId": "alloc-a", "task": "web", "stream": "stdout", "line": "second", "continued": True, "partial": True},
            {"allocationId": "alloc-a", "task": "web", "stream": "stdout", "line": "last", "continued": True},
        ]
        expected = (
            "[alloc-a/web/stdout] [partial] first\n"
            "[alloc-b/worker/stderr] other source\n"
            "[alloc-a/web/stdout] [continued] [partial] second\n"
            "[alloc-a/web/stdout] [continued] last\n"
        )
        self.client.service_logs.return_value = {"entries": entries, "logs": ["legacy duplicate"]}
        code, out, err = self.run_cli("service", "logs", "api")
        self.assertEqual(code, 0)
        self.assertEqual(out, expected)
        self.assertEqual(err, "")
        self.client.service_log_events.return_value = iter(entries)
        code, out, err = self.run_cli("service", "logs", "api", "--follow")
        self.assertEqual(code, 0)
        self.assertEqual(out, expected)
        self.assertEqual(err, "")
        self.client.service_log_events.return_value = iter(entries)
        code, out, err = self.run_cli("service", "logs", "api", "--follow", "--format", "ndjson")
        self.assertEqual([json.loads(line) for line in out.splitlines()], entries)

    def test_logs_follow_error_returns_failure(self):
        self.credentials()
        self.client.service_log_events.return_value = iter([{"type": "status", "status": "error", "message": "Node unavailable"}])
        code, out, err = self.run_cli("service", "logs", "api", "-f")
        self.assertEqual(code, 1)
        self.assertIn("Node unavailable", err)

    def test_logs_invalid_options_fail_before_network(self):
        for options in (("--tail", "0"), ("--tail", "501"), ("--follow", "--format", "json")):
            with self.subTest(options=options):
                code, _, _ = self.run_cli("service", "logs", "api", *options)
                self.assertEqual(code, 1)
        self.client_cls.assert_not_called()

    def test_control_context_does_not_conflict_with_build_context(self):
        args = build_parser().parse_args(["build", "local", ".", "--context", "subdir", "--control-context", "cluster"])
        self.assertEqual(args.build_context, "subdir")
        self.assertEqual(args.control_context, "cluster")


class ExistingControlStateTests(unittest.TestCase):
    def test_database_only_manager_state_is_found_without_sudo(self):
        from luma.cli import _existing_control_state
        from luma.control.state import new_state, save_state, state_path
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LUMA_CONTROL_STATE_DIR": temp}):
            state = new_state(domain="old.example.com")
            save_state(state)
            state["domain"] = "current.example.com"
            save_state(state)
            # Fresh installations have no legacy JSON. Even if an obsolete
            # file is copied in later, current SQLite state remains authoritative.
            with patch("luma.cli.LocalExecutor") as executor:
                self.assertFalse(state_path().exists())
                self.assertEqual(_existing_control_state()["domain"], "current.example.com")
                state_path().write_text('{"domain":"stale.example.com"}')
                self.assertEqual(_existing_control_state()["domain"], "current.example.com")
                executor.assert_not_called()

    def test_privileged_fallback_uses_installed_python_and_captured_state(self):
        import shlex
        import sys
        from luma.cli import _existing_control_state
        from luma.local import LocalResult
        marker = "test-secret-not-logged"
        directory = "/tmp/manager state ' quoted"
        with patch("luma.cli.control_state_is_initialized", side_effect=PermissionError), patch("luma.cli.state_path", return_value=Path(directory) / "control.json"), patch("luma.cli.LocalExecutor") as executor:
            executor.return_value.sudo_result.return_value = LocalResult(0, json.dumps({"domain": "current.example.com", "deployToken": marker}))
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                result = _existing_control_state()
            command = executor.return_value.sudo_result.call_args.args[0]
            argv = shlex.split(command)
            self.assertEqual(argv[0:2], [sys.executable, "-c"])
            self.assertIn("is_initialized,load_state", argv[2])
            self.assertEqual(argv[3], directory)
            self.assertNotIn("cat ", command)
            self.assertNotIn(marker, command + output.getvalue())
            self.assertEqual(result["deployToken"], marker)


class ControlReadClientTests(unittest.TestCase):
    def test_query_values_are_encoded_and_not_interpreted_as_parameters(self):
        client = ControlClient("https://control.example.com", "token")
        with patch.object(client, "request", return_value={}) as request:
            client.service_logs("app/api&previous=true", allocation="alloc&x=1", previous=True, tail=5)
            path = request.call_args.args[1]
            self.assertEqual(parse_qs(urlparse(path).query), {
                "service": ["app/api&previous=true"], "allocation": ["alloc&x=1"], "previous": ["true"], "tail": ["5"]
            })
        with patch.object(client, "stream", return_value=iter([{"line": "hi", "cursor": "position-1"}])) as stream:
            iterator = client.service_log_events("app_api")
            self.assertEqual(next(iterator)["line"], "hi")
            iterator.close()
            self.assertTrue(stream.call_args.args[1].startswith("/v1/dashboard/logs/stream?"))


class LogReconnectTests(unittest.TestCase):
    def setUp(self):
        self.client = ControlClient("https://control.example.com", "token")
        self.sleep = patch("luma.control.client.time.sleep")
        self.sleeper = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_clean_eof_resumes_from_heartbeat_without_replaying_lines(self):
        streams = [
            iter([{"line": "same", "cursor": "line-1"}, {"status": "heartbeat", "cursor": "after-heartbeat"}]),
            iter([{"line": "same", "cursor": "line-2"}, {"status": "error", "message": "stop"}]),
        ]
        with patch.object(self.client, "stream", side_effect=streams) as stream:
            events = list(self.client.service_log_events("app_api"))
        # Identical text is retained: they represent two different byte offsets.
        self.assertEqual([event["line"] for event in events if "line" in event], ["same", "same"])
        query = parse_qs(urlparse(stream.call_args_list[1].args[1]).query)
        self.assertEqual(query["cursor"], ["after-heartbeat"])
        self.assertEqual(query["service"], ["app_api"])
        self.sleeper.assert_called_once_with(0.5)

    def test_transport_drop_reconnects_from_last_delivered_line(self):
        def broken():
            yield {"line": "before", "cursor": "resume-here"}
            raise LumaError("interrupted") from ConnectionResetError("reset")
        with patch.object(self.client, "stream", side_effect=[broken(), iter([{"line": "after", "cursor": "next"}, {"status": "error"}])]) as stream:
            events = list(self.client.service_log_events("app_api"))
        self.assertEqual([event["line"] for event in events if "line" in event], ["before", "after"])
        self.assertEqual(parse_qs(urlparse(stream.call_args_list[1].args[1]).query)["cursor"], ["resume-here"])

    def test_broken_chunked_response_is_classified_as_transport_failure(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def chunks():
            yield b'{"line":"before","cursor":"resume"}\n'
            raise http.client.IncompleteRead(b"", 10)
        response.__iter__ = Mock(side_effect=chunks)
        with patch.object(self.client, "_open", return_value=response):
            stream = self.client.stream("GET", "/v1/dashboard/logs/stream")
            self.assertEqual(next(stream)["line"], "before")
            with self.assertRaises(LumaError) as caught:
                next(stream)
        self.assertTrue(self.client._retryable_log_stream_error(caught.exception))

    def test_http_auth_invalid_cursor_and_protocol_errors_do_not_retry(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                error = LumaError("HTTP failure")
                error.__cause__ = urllib.error.HTTPError("https://control.example.com", status, "error", {}, None)
                with patch.object(self.client, "stream", side_effect=error) as stream:
                    with self.assertRaises(LumaError):
                        list(self.client.service_log_events("app_api"))
                    stream.assert_called_once()
        with patch.object(self.client, "stream", side_effect=LumaError("invalid stream JSON")) as stream:
            with self.assertRaises(LumaError):
                list(self.client.service_log_events("app_api"))
            stream.assert_called_once()
        self.sleeper.assert_not_called()

    def test_retries_are_bounded_and_backoff_is_capped(self):
        with patch.object(self.client, "stream", side_effect=lambda *args, **kwargs: iter([])) as stream:
            with self.assertRaisesRegex(LumaError, "after 8 attempts"):
                list(self.client.service_log_events("app_api"))
        self.assertEqual(stream.call_count, 9)
        self.assertEqual([call.args[0] for call in self.sleeper.call_args_list], [0.5, 1, 2, 4, 8, 15, 15, 15])

    def test_ctrl_c_interrupts_backoff(self):
        self.sleeper.side_effect = KeyboardInterrupt
        with patch.object(self.client, "stream", return_value=iter([])) as stream:
            with self.assertRaises(KeyboardInterrupt):
                list(self.client.service_log_events("app_api"))
        stream.assert_called_once()

    def test_old_control_without_cursor_cannot_duplicate_initial_snapshot(self):
        with patch.object(self.client, "stream", return_value=iter([{"line": "old server"}])) as stream:
            with self.assertRaisesRegex(LumaError, "resume cursors"):
                list(self.client.service_log_events("app_api"))
        stream.assert_called_once()
        self.sleeper.assert_not_called()
