import argparse
import http.client
import io
import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch

from luma.cli import _wait_for_queued_build
from luma.control.client import ControlClient
from luma.errors import ControlRequestError, LumaError


def response(payload=b'{"ok":true}'):
    value = io.BytesIO(payload)
    value.headers = {}
    return value


class ControlRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = ControlClient('https://control.example.com', 'secret')

    def test_read_and_workflow_check_retry_remote_disconnect(self):
        for method, path in [('GET', '/v1/health'), ('POST', '/v1/workflows/check')]:
            with self.subTest(path=path), patch('urllib.request.urlopen', side_effect=[http.client.RemoteDisconnected(), response()]) as opened, patch('luma.control.client.time.sleep'):
                self.assertEqual(self.client.request(method, path), {'ok': True})
                self.assertEqual(opened.call_count, 2)

    def test_build_submission_is_never_replayed(self):
        with patch('urllib.request.urlopen', side_effect=http.client.RemoteDisconnected()) as opened:
            with self.assertRaisesRegex(ControlRequestError, 'POST /v1/builds; attempts=1'):
                self.client.request('POST', '/v1/builds', {})
            self.assertEqual(opened.call_count, 1)

    def test_read_failure_retries_but_invalid_json_does_not(self):
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.read1.side_effect = http.client.IncompleteRead(b'partial')
        with patch('urllib.request.urlopen', side_effect=[broken, response()]), patch('luma.control.client.time.sleep'):
            self.assertEqual(self.client.health()['ok'], True)
        with patch('urllib.request.urlopen', return_value=response(b'no json')) as opened:
            with self.assertRaisesRegex(LumaError, 'non-JSON'):
                self.client.health()
            self.assertEqual(opened.call_count, 1)

    def test_permanent_http_and_tls_errors_are_not_retried(self):
        for error in [urllib.error.HTTPError('https://control.example.com', 401, 'Unauthorized', {}, io.BytesIO()), urllib.error.URLError(ssl.SSLCertVerificationError('bad certificate'))]:
            with self.subTest(error=error), patch('urllib.request.urlopen', side_effect=error) as opened:
                with self.assertRaises(LumaError):
                    self.client.health()
                self.assertEqual(opened.call_count, 1)

    def test_transient_http_has_bounded_attempts(self):
        def unavailable(*args, **kwargs):
            raise urllib.error.HTTPError('https://control.example.com', 503, 'Unavailable', {}, io.BytesIO())
        with patch('urllib.request.urlopen', side_effect=unavailable) as opened, patch('luma.control.client.time.sleep'):
            with self.assertRaisesRegex(ControlRequestError, 'attempts=3'):
                self.client.health()
            self.assertEqual(opened.call_count, 3)

    def test_tls_eof_retries_but_protocol_error_does_not(self):
        for error in [ssl.SSLEOFError("unexpected EOF"), urllib.error.URLError(ssl.SSLEOFError("unexpected EOF"))]:
            with self.subTest(error=error), patch('urllib.request.urlopen', side_effect=[error, response()]) as opened, patch('luma.control.client.time.sleep'):
                self.assertTrue(self.client.health()['ok'])
                self.assertEqual(opened.call_count, 2)
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError(ssl.SSLError("wrong version number"))) as opened:
            with self.assertRaises(LumaError) as caught:
                self.client.health()
            self.assertNotIsInstance(caught.exception, ControlRequestError)
            self.assertEqual(opened.call_count, 1)

    def test_invalid_gzip_is_not_a_transport_failure(self):
        for data in [b'not gzip', gzip.compress(b'{}')[:-4], b'\x1f\x8b\x08\x00' + b'\x00' * 6 + b'\x07']:
            value = response(data)
            value.headers['Content-Encoding'] = 'gzip'
            with self.subTest(data=data), patch('urllib.request.urlopen', return_value=value) as opened:
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
        with patch('urllib.request.urlopen', side_effect=[broken, response()]) as opened, patch('luma.control.client.time.sleep'):
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
        with patch('luma.cli.time.sleep'), patch('luma.cli._print_json') as output:
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
        with patch('luma.cli.time.sleep'), patch('luma.cli._print_json') as output:
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
        with patch('luma.cli.time.monotonic', side_effect=[0, 1, 121]):
            with self.assertRaisesRegex(LumaError, 'server task continues'):
                _wait_for_queued_build(self.args(), client, {'queued': True, 'buildRunId': 'b1'})
        client.cancel_build.assert_not_called()
