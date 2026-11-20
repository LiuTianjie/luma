import argparse
import http.client
import io
import gzip
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch

from luma.cli.builds import _wait_for_queued_build
from luma.control import server, state as state_mod
from luma.control.client import ControlClient
from luma.control.state import init_state
from luma.errors import ControlRequestError, LumaError
from tests.support import _restore_env, _set_env


def response(payload=b'{"ok":true}'):
    value = io.BytesIO(payload)
    value.headers = {}
    return value


class ControlRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = ControlClient('https://control.example.com', 'secret')

    def test_read_and_workflow_check_retry_remote_disconnect(self):
        for method, path in [('GET', '/v1/health'), ('POST', '/v1/workflows/check')]:
            with self.subTest(path=path), patch('urllib.request.OpenerDirector.open', side_effect=[http.client.RemoteDisconnected(), response()]) as opened, patch('luma.control.client.time.sleep'):
                self.assertEqual(self.client.request(method, path), {'ok': True})
                self.assertEqual(opened.call_count, 2)

    def test_build_submission_is_never_replayed(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=http.client.RemoteDisconnected()) as opened:
            with self.assertRaisesRegex(ControlRequestError, 'POST /v1/builds; attempts=1'):
                self.client.request('POST', '/v1/builds', {})
            self.assertEqual(opened.call_count, 1)

    def test_read_failure_retries_but_invalid_json_does_not(self):
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.read1.side_effect = http.client.IncompleteRead(b'partial')
        with patch('urllib.request.OpenerDirector.open', side_effect=[broken, response()]), patch('luma.control.client.time.sleep'):
            self.assertEqual(self.client.health()['ok'], True)
        with patch('urllib.request.OpenerDirector.open', return_value=response(b'no json')) as opened:
            with self.assertRaisesRegex(LumaError, 'non-JSON'):
                self.client.health()
            self.assertEqual(opened.call_count, 1)

    def test_permanent_http_and_tls_errors_are_not_retried(self):
        for error in [urllib.error.HTTPError('https://control.example.com', 401, 'Unauthorized', {}, io.BytesIO()), urllib.error.URLError(ssl.SSLCertVerificationError('bad certificate'))]:
            with self.subTest(error=error), patch('urllib.request.OpenerDirector.open', side_effect=error) as opened:
                with self.assertRaises(LumaError):
                    self.client.health()
                self.assertEqual(opened.call_count, 1)

    def test_transient_http_has_bounded_attempts(self):
        def unavailable(*args, **kwargs):
            raise urllib.error.HTTPError('https://control.example.com', 503, 'Unavailable', {}, io.BytesIO())
        with patch('urllib.request.OpenerDirector.open', side_effect=unavailable) as opened, patch('luma.control.client.time.sleep'):
            with self.assertRaisesRegex(ControlRequestError, 'attempts=3'):
                self.client.health()
            self.assertEqual(opened.call_count, 3)

    def test_tls_eof_retries_but_protocol_error_does_not(self):
        for error in [ssl.SSLEOFError("unexpected EOF"), urllib.error.URLError(ssl.SSLEOFError("unexpected EOF"))]:
            with self.subTest(error=error), patch('urllib.request.OpenerDirector.open', side_effect=[error, response()]) as opened, patch('luma.control.client.time.sleep'):
                self.assertTrue(self.client.health()['ok'])
                self.assertEqual(opened.call_count, 2)
        with patch('urllib.request.OpenerDirector.open', side_effect=urllib.error.URLError(ssl.SSLError("wrong version number"))) as opened:
            with self.assertRaises(LumaError) as caught:
                self.client.health()
            self.assertNotIsInstance(caught.exception, ControlRequestError)
            self.assertEqual(opened.call_count, 1)

    def test_invalid_gzip_is_not_a_transport_failure(self):
        for data in [b'not gzip', gzip.compress(b'{}')[:-4], b'\x1f\x8b\x08\x00' + b'\x00' * 6 + b'\x07']:
            value = response(data)
            value.headers['Content-Encoding'] = 'gzip'
            with self.subTest(data=data), patch('urllib.request.OpenerDirector.open', return_value=value) as opened:
                with self.assertRaisesRegex(LumaError, 'invalid gzip') as caught:
                    self.client.health()
                self.assertNotIsInstance(caught.exception, ControlRequestError)
                self.assertEqual(opened.call_count, 1)

    def test_real_slow_headers_and_body_obey_total_deadline(self):
        finished = threading.Event()

        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    if self.path == '/headers':
                        for byte in b'HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n':
                            self.connection.sendall(bytes([byte]))
                            time.sleep(.02)
                    else:
                        self.send_response(200)
                        self.send_header('Content-Length', '100')
                        self.end_headers()
                    for _ in range(100):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    finished.set()

        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_open = __import__('urllib.request', fromlist=['urlopen']).urlopen
        try:
            for path in ['/body', '/headers']:
                finished.clear()
                def local_open(*args, **kwargs):
                    return real_open(f'http://127.0.0.1:{server.server_port}{path}', timeout=kwargs['timeout'])
                with self.subTest(path=path), patch.object(self.client, '_open', side_effect=local_open) as opened:
                    started = time.monotonic()
                    with self.assertRaises(ControlRequestError):
                        self.client.request('GET', path, timeout=.15)
                    self.assertLess(time.monotonic() - started, .5)
                    self.assertEqual(opened.call_count, 1)
                    self.assertTrue(finished.wait(2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_expired_requests_cannot_accumulate_unbounded_workers(self):
        release = threading.Event()
        def blocked(*args, **kwargs):
            release.wait(2)
            raise TimeoutError()
        try:
            with patch.object(self.client, '_request_once', side_effect=blocked) as opened:
                for _ in range(6):
                    with self.assertRaises(ControlRequestError):
                        self.client.request('POST', '/v1/builds', {}, timeout=.02)
                self.assertEqual(opened.call_count, 4)
        finally:
            release.set()
        # All abandoned workers release their slots once the transport exits.
        for _ in range(4):
            self.assertTrue(self.client._request_slots.acquire(timeout=2))
        for _ in range(4):
            self.client._request_slots.release()

    def test_premature_content_length_eof_is_retried(self):
        broken = response(b'{"ok":true}')
        broken.length = 10
        with patch('urllib.request.OpenerDirector.open', side_effect=[broken, response()]) as opened, patch('luma.control.client.time.sleep'):
            self.assertTrue(self.client.health()['ok'])
            self.assertEqual(opened.call_count, 2)

    def test_retry_budget_is_shared(self):
        with patch.object(self.client, '_request_before_deadline', side_effect=TimeoutError()) as opened, patch('luma.control.client.time.monotonic', side_effect=[0, 29.8, 30]), patch('luma.control.client.time.sleep') as sleep:
            with self.assertRaises(ControlRequestError):
                self.client.health()
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()


class BuildResumeTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(timeout=120, format='ndjson', quiet=False)

    def page(self, messages, after, *, cursor=None, status='running'):
        return {'run': {'status': status, 'events': [{'message': m} for m in messages], 'result': {'service': 'app'}},
                'eventsPage': {'resumeAfter': after, 'nextCursor': cursor}}

    def test_resume_failed_page_without_duplicate_output_or_resubmission(self):
        client = Mock()
        client.get_build.side_effect = [
            self.page(['one'], 0, cursor='page-two'),
            ControlRequestError('connection lost'),
            self.page(['two'], 1),
            self.page(['final'], 2, status='succeeded'),
            self.page([], 2, status='succeeded'),
        ]
        with patch('luma.cli.common.time.sleep'), patch('luma.cli.builds._print_json') as output:
            result = _wait_for_queued_build(self.args(), client, {'queued': True, 'buildRunId': 'b1'})
        self.assertEqual(result['buildRunId'], 'b1')
        messages = [c.args[0].get('message') for c in output.call_args_list]
        self.assertEqual([m for m in messages if m in ['one', 'two', 'final']], ['one', 'two', 'final'])
        self.assertEqual([c.kwargs['query'] for c in client.get_build.call_args_list], [
            {'limit': 100}, {'limit': 100, 'cursor': 'page-two'}, {'limit': 100, 'cursor': 'page-two'},
            {'limit': 100, 'after': 1}, {'limit': 100, 'after': 2}])
        self.assertTrue(all(c.args == ('b1',) for c in client.get_build.call_args_list))
        client.build_deploy.assert_not_called()
        client.cancel_build.assert_not_called()

    def test_legacy_control_restarts_incomplete_page_without_duplicate_logs(self):
        client = Mock()
        first = {'run': {'status': 'running', 'events': [{'message': 'one'}]},
                 'eventsPage': {'nextCursor': 'two'}}
        client.get_build.side_effect = [first, ControlRequestError('offline'), first,
            {'run': {'events': [{'message': 'two'}]}, 'eventsPage': {}},
            {'run': {'status': 'succeeded', 'events': [{'message': 'one'}, {'message': 'two'}],
                     'result': {'service': 'app'}}}]
        with patch('luma.cli.common.time.sleep'), patch('luma.cli.builds._print_json') as output:
            _wait_for_queued_build(self.args(), client, {'queued': True, 'buildRunId': 'b1'})
        messages = [c.args[0].get('message') for c in output.call_args_list]
        self.assertEqual([m for m in messages if m in ['one', 'two']], ['one', 'two'])
        self.assertTrue(all('after' not in c.kwargs['query'] for c in client.get_build.call_args_list))

    def test_authentication_error_fails_immediately(self):
        client = Mock()
        client.get_build.side_effect = LumaError('control API error 401')
        with self.assertRaisesRegex(LumaError, '401'):
            _wait_for_queued_build(self.args(), client, {'queued': True, 'buildRunId': 'b1'})
        self.assertEqual(client.get_build.call_count, 1)

    def test_outage_deadline_preserves_server_task(self):
        client = Mock()
        client.get_build.side_effect = ControlRequestError('offline')
        with patch('luma.cli.common.time.monotonic', side_effect=[0, 1, 121]):
            with self.assertRaisesRegex(LumaError, 'server task continues'):
                _wait_for_queued_build(self.args(), client, {'queued': True, 'buildRunId': 'b1'})
        client.cancel_build.assert_not_called()


class ControlPlaneHardeningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(self._tmp.name) / "state"))
        self.state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
        self.token = self.state["deployToken"]

    def tearDown(self):
        _restore_env("LUMA_CONTROL_STATE_DIR", self._old_state)
        self._tmp.cleanup()

    def _register_node_with_agent(self, name: str = "worker-1") -> str:
        from luma.nomad_migration_node import CAPABILITY

        now = int(time.time())

        def mutate(current):
            current.setdefault("nodes", {})[name] = {
                "name": name,
                "host": "10.0.0.10",
                "nodeId": f"id-{name}",
                "status": "ready",
                "agent": {
                    "tokenHash": server._hash_agent_token("agent-secret"),
                    "status": "ready",
                    "lastSeen": now,
                    "capabilities": [CAPABILITY, "managed-volume-path"],
                },
            }
        state_mod.mutate_state(mutate)
        return "agent-secret"

    def test_async_node_agent_lease_wakes_promptly_when_task_is_queued(self):
        import asyncio

        agent_token = self._register_node_with_agent("worker-wake")

        async def run():
            started = time.monotonic()
            lease_task = asyncio.create_task(
                server._asgi_node_agent_lease(
                    agent_token,
                    {"nodeName": "worker-wake", "waitSeconds": 25},
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(lease_task.done())
            task_id = server._queue_node_agent_task(
                state_mod.load_state(),
                "worker-wake",
                "noop",
                {"hello": "world"},
                required_capability=None,
            )
            result = await asyncio.wait_for(lease_task, timeout=3.0)
            elapsed = time.monotonic() - started
            return task_id, result, elapsed

        task_id, result, elapsed = asyncio.run(run())
        self.assertLess(elapsed, 3.0)
        self.assertIsNotNone(result.get("task"))
        self.assertEqual(result["task"]["id"], task_id)

    def test_authenticated_post_validates_auth_before_parsing_or_sizing_body(self):
        from starlette.testclient import TestClient

        with TestClient(server.create_app(), base_url="https://luma.example.com") as client:
            # Unauthenticated request with invalid JSON gets 401, not 400.
            no_auth = client.post(
                "/v1/secrets",
                content=b"{malformed-json",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(no_auth.status_code, 401)

            # Wrong bearer token with oversized Content-Length gets 401, not 413.
            wrong_auth = client.post(
                "/v1/secrets",
                content=b"{}",
                headers={
                    "Authorization": "Bearer wrong-token",
                    "Content-Length": str(server.MAX_AUTHENTICATED_BODY_BYTES + 1024),
                },
            )
            self.assertEqual(wrong_auth.status_code, 401)

            # Valid bearer token with oversized body gets 413.
            with patch.object(server, "MAX_AUTHENTICATED_BODY_BYTES", 64):
                too_big = client.post(
                    "/v1/secrets",
                    content=b'{"name":"A","value":"' + (b"x" * 100) + b'"}',
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
            self.assertEqual(too_big.status_code, 413)
            self.assertEqual(too_big.json()["errorInfo"]["code"], "body_too_large")

    def test_terminal_broker_isolates_browser_and_agent_pending_auth_semaphores(self):
        import asyncio
        import threading
        from starlette.websockets import WebSocketDisconnect

        class FakeSocket:
            def __init__(self, query, token):
                self.query_params = query
                self.token = token
                self.sent = []
                self.closed = None
                self.headers = {}
                self.cookies = {}

            async def accept(self):
                pass

            async def receive_json(self):
                if self.token is not None:
                    tok, self.token = self.token, None
                    return {"type": "auth", "token": tok}
                raise WebSocketDisconnect(1000)

            async def send_json(self, message):
                self.sent.append(message)

            async def close(self, code=1000):
                self.closed = code

        self._register_node_with_agent("worker-term")
        browser_blocked = threading.Event()
        release_browser = threading.Event()

        def slow_browser_target(*_args, **_kwargs):
            browser_blocked.set()
            release_browser.wait(3.0)
            return ("worker-term", None)

        async def run():
            broker = server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
            broker._pending_auth = asyncio.Semaphore(1)
            browser_sock = FakeSocket({"node": "worker-term"}, self.token)
            agent_sock = FakeSocket({"node": "worker-term"}, "agent-secret")
            with patch.object(server, "_terminal_browser_target", side_effect=slow_browser_target):
                b_task = asyncio.create_task(broker.connect_browser(browser_sock))
                try:
                    self.assertTrue(await asyncio.wait_for(asyncio.to_thread(browser_blocked.wait, 2.0), 3.0))
                    # Browser pending-auth slot is full, yet node agent must still authenticate.
                    await asyncio.wait_for(broker.connect_agent(agent_sock), timeout=2.0)
                    self.assertIn({"type": "ready", "node": "worker-term"}, agent_sock.sent)
                finally:
                    release_browser.set()
                    await asyncio.wait_for(b_task, timeout=3.0)

        asyncio.run(run())

    def test_registry_scan_cache_and_epochs_are_bounded(self):
        with server._REGISTRY_SCAN_LOCK:
            server._REGISTRY_SCAN_CACHE.clear()
            server._REGISTRY_SCAN_EPOCHS.clear()
            for idx in range(server._REGISTRY_SCAN_CACHE_MAX_ENTRIES + 40):
                key = f"reg-{idx}"
                server._bump_registry_scan_epoch(key)
                server._set_registry_scan_cache(key, {"ok": True, "idx": idx})
            self.assertLessEqual(len(server._REGISTRY_SCAN_CACHE), server._REGISTRY_SCAN_CACHE_MAX_ENTRIES)
            self.assertLessEqual(len(server._REGISTRY_SCAN_EPOCHS), server._REGISTRY_SCAN_CACHE_MAX_ENTRIES)
            self.assertNotIn("reg-0", server._REGISTRY_SCAN_CACHE)
            self.assertIn(f"reg-{server._REGISTRY_SCAN_CACHE_MAX_ENTRIES + 39}", server._REGISTRY_SCAN_CACHE)
            server._REGISTRY_SCAN_CACHE.clear()
            server._REGISTRY_SCAN_EPOCHS.clear()

    def test_stream_enqueue_bounds_queue_preserves_terminal_and_ignores_closed_loop(self):
        import asyncio

        async def run():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue(maxsize=2)
            server._stream_enqueue(loop, queue, {"status": "progress", "n": 1})
            server._stream_enqueue(loop, queue, {"status": "progress", "n": 2})
            server._stream_enqueue(loop, queue, {"status": "progress", "n": 3})  # dropped
            server._stream_enqueue(loop, queue, {"status": "done", "result": {"ok": True}})
            server._stream_enqueue(loop, queue, None)
            await asyncio.sleep(0)
            items = []
            while not queue.empty():
                items.append(queue.get_nowait())
            return items

        items = asyncio.run(run())
        self.assertEqual(items[-1], None)
        self.assertEqual(items[-2], {"status": "done", "result": {"ok": True}})

        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        # Must not raise RuntimeError on a closed event loop.
        server._stream_enqueue(closed_loop, asyncio.Queue(maxsize=1), None)

    def test_node_agent_complete_does_not_overwrite_timeout_status(self):
        agent_token = self._register_node_with_agent("worker-timeout")
        task_id = server._queue_node_agent_task(
            state_mod.load_state(),
            "worker-timeout",
            "noop",
            {},
            required_capability=None,
        )

        def mark_timeout(current):
            current["agentTasks"][task_id]["status"] = "timeout"
            current["agentTasks"][task_id]["message"] = "timed out"
        state_mod.mutate_state(mark_timeout)

        res = server.handle_node_agent_complete(
            agent_token,
            {"nodeName": "worker-timeout", "taskId": task_id, "status": "succeeded", "result": {"late": True}},
        )
        self.assertEqual(res["status"], "timeout")
        self.assertEqual(state_mod.load_state()["agentTasks"][task_id]["status"], "timeout")

    def test_nomad_security_task_requeues_interrupted_failed_task(self):
        self._register_node_with_agent("worker-sec")
        body = {
            "nodeName": "worker-sec",
            "operationKey": "op-interrupted-1",
            "payload": {"schema": 1, "op": "inspect"},
        }
        first = server.handle_nomad_security_task(self.token, body)
        task_id = first["id"]

        def mark_running(current):
            current["agentTasks"][task_id]["status"] = "running"
        state_mod.mutate_state(mark_running)

        state_mod.mutate_state(lambda current: server._reconcile_interrupted_agent_tasks(current, "worker-sec", ""))
        after_reconcile = state_mod.load_state()["agentTasks"][task_id]
        self.assertEqual(after_reconcile["status"], "failed")
        self.assertTrue(after_reconcile.get("interrupted"))

        second = server.handle_nomad_security_task(self.token, body)
        self.assertEqual(second["status"], "queued")
        self.assertFalse(state_mod.load_state()["agentTasks"][task_id].get("interrupted"))

    def test_dashboard_setup_configure_persists_cloudflare_check_invalidation(self):
        config_path = Path(self._tmp.name) / "luma.yaml"
        config_path.write_text("defaults:\n  engine: nomad\n", encoding="utf-8")
        old_cfg = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
        try:
            def seed_check(current):
                current["setupChecks"] = {"checks": {"cloudflare": {"status": "ready", "detail": "ok"}}}
            state_mod.mutate_state(seed_check)

            server.handle_dashboard_setup_configure(
                self.token,
                {"cloudflareZone": "example.com", "cloudflareZoneId": "zone-123"},
            )
            persisted = state_mod.load_state()
            cf_check = ((persisted.get("setupChecks") or {}).get("checks") or {}).get("cloudflare")
            self.assertNotEqual((cf_check or {}).get("status"), "ready")
        finally:
            _restore_env("LUMA_CONTROL_CONFIG", old_cfg)

    def test_operations_lifespan_raises_threadpool_limiter_and_stops_registry_automation(self):
        import anyio.to_thread
        from starlette.testclient import TestClient

        observed_tokens = None
        app = server.create_app()

        with TestClient(app, base_url="https://luma.example.com") as client:
            thread = app.state.registry_automation_thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())

            async def read_tokens():
                return anyio.to_thread.current_default_thread_limiter().total_tokens

            observed_tokens = client.portal.call(read_tokens)
            self.assertGreaterEqual(observed_tokens, server.DEFAULT_THREADPOOL_TOKENS)

        self.assertTrue(app.state.registry_automation_stop.is_set())
        self.assertFalse(app.state.registry_automation_thread.is_alive())
