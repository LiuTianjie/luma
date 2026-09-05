from __future__ import annotations

import asyncio
import base64
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from luma.control.logs import LogReader, LogUnavailable, MAX_SOURCES, MAX_BYTES, encode_event
from luma.control import server
from luma.errors import LumaError


def frame(text: str | bytes, offset: int = 0, file: str = 'alloc/logs/web.stdout.0') -> str:
    raw = text.encode() if isinstance(text, str) else text
    return json.dumps({'File': file, 'Offset': offset, 'Data': base64.b64encode(raw).decode()})


class FakeNomad:
    api_url = 'http://nomad.test'
    token = 'nomad-secret'

    def __init__(self):
        self.allocations = [{'ID': 'a1', 'DesiredStatus': 'run', 'ClientStatus': 'running',
                             'TaskStates': {'web': {}}, 'CreateIndex': 1}]
        self.files = {'alloc/logs/web.stdout.0': b'ready\n'}
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append(path)
        if path == '/v1/job/app/allocations':
            return self.allocations
        if path.startswith('/v1/client/fs/ls/'):
            return [{'Name': name.removeprefix('alloc/logs/'), 'Size': len(data), 'IsDir': False}
                    for name, data in self.files.items()]
        raise AssertionError(path)

    def request_text(self, method, path, body=None):
        self.calls.append(path)
        query = parse_qs(urlparse(path).query)
        stream = query['type'][0]
        return ''.join(frame(data, file=name) for name, data in self.files.items()
                       if f'.{stream}.' in name)

    def read(self, source, file, offset, limit):
        return self.files[file][offset:offset + limit]


class LogReaderTests(unittest.TestCase):
    def reader(self, client=None, **kwargs):
        client = client or FakeNomad()
        reader = LogReader(client, 'app', 'web', 'app', **kwargs)
        reader._read_bytes = client.read
        return reader

    def test_appends_after_initial_snapshot_and_keeps_repeated_lines(self):
        client = FakeNomad()
        reader = self.reader(client)
        self.assertEqual([e['line'] for e in reader.poll() if 'line' in e], ['ready'])
        client.files['alloc/logs/web.stdout.0'] += b'ready\nready\n'
        self.assertEqual([e['line'] for e in reader.poll() if 'line' in e], ['ready', 'ready'])
        self.assertEqual([e for e in reader.poll() if 'line' in e], [])

    def test_resume_uses_delivered_line_cursor_without_skipping_later_lines(self):
        client = FakeNomad()
        client.files['alloc/logs/web.stdout.0'] = b'one\ntwo\n'
        events = [e for e in self.reader(client).poll() if 'line' in e]
        reader = self.reader(client, cursor=events[0]['cursor'])
        self.assertEqual([e['line'] for e in reader.poll() if 'line' in e], ['two'])

    def test_rotation_reads_old_file_remainder_then_new_file(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.files['alloc/logs/web.stdout.0'] += b'last\n'
        client.files['alloc/logs/web.stdout.1'] = b'new\n'
        events = reader.poll()
        self.assertEqual([e['line'] for e in events if 'line' in e], ['last', 'new'])
        self.assertFalse(any(e.get('status') == 'warning' for e in events))

    def test_retention_gap_is_explicit_and_skips_older_files(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.files = {'alloc/logs/web.stdout.2': b'retained\n'}
        events = reader.poll()
        self.assertEqual([e['line'] for e in events if 'line' in e], ['retained'])
        self.assertTrue(any('retention gap' in e.get('message', '') for e in events))

    def test_truncation_restarts_at_zero(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.files['alloc/logs/web.stdout.0'] = b'x\n'
        events = reader.poll()
        self.assertEqual([e['line'] for e in events if 'line' in e], ['x'])
        self.assertTrue(any('truncated' in e.get('message', '') for e in events))

    def test_three_allocations_and_allocation_selector(self):
        client = FakeNomad()
        client.allocations *= 3
        client.allocations = [{**a, 'ID': f'a{i}'} for i, a in enumerate(client.allocations)]
        reader = self.reader(client)
        events = reader.poll()
        self.assertEqual({e['allocationId'] for e in events if 'line' in e}, {'a0', 'a1', 'a2'})
        selected = self.reader(client, allocation='a1')
        self.assertEqual({e['allocationId'] for e in selected.poll() if 'line' in e}, {'a1'})
        with self.assertRaisesRegex(LumaError, 'does not belong'):
            self.reader(client, allocation='another-service-alloc').poll()

    def test_previous_only_uses_stopped_allocations(self):
        client = FakeNomad()
        client.allocations.append({**client.allocations[0], 'ID': 'old', 'DesiredStatus': 'stop', 'ClientStatus': 'complete'})
        self.assertEqual({e['allocationId'] for e in self.reader(client, previous=True).poll() if 'line' in e}, {'old'})

    def test_discovers_replacement_allocations(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.allocations = [{**client.allocations[0], 'ID': 'new'}]
        self.assertEqual({e['allocationId'] for e in reader.poll() if 'line' in e}, {'new'})
        self.assertFalse(any('a1' in key for key in reader.positions))

    def test_source_limit_is_reported(self):
        client = FakeNomad()
        client.allocations = [{**client.allocations[0], 'ID': str(i)} for i in range(20)]
        reader = self.reader(client)
        events = reader.poll()
        self.assertEqual(len(reader.sources), 40)
        self.assertEqual(len(reader._read_sources), MAX_SOURCES)
        self.assertTrue(any('Reading 32 of 40' in e.get('message', '') for e in events))

    def test_snapshot_uses_file_size_not_nomad_frame_offset(self):
        client = FakeNomad()
        client.files['alloc/logs/web.stdout.0'] = '你好\n'.encode()
        # Nomad 1.9.7 can emit Offset=length for a one-frame fs/logs
        # response, or different offsets after coalescing. Never consume it.
        client.request_text = Mock(side_effect=AssertionError('fs/logs is not a cursor source'))
        reader = self.reader(client)
        events = [e for e in reader.poll() if 'line' in e]
        self.assertEqual([e['line'] for e in events], ['你好'])
        self.assertEqual(events[0]['offset'], len('你好\n'.encode()))
        client.files['alloc/logs/web.stdout.0'] += b'later\n'
        self.assertEqual([e['line'] for e in reader.poll() if 'line' in e], ['later'])
        client.request_text.assert_not_called()

    def test_small_snapshot_tail_does_not_limit_follow_throughput(self):
        client = FakeNomad()
        reader = self.reader(client, tail=1)
        reader.poll()
        client.files['alloc/logs/web.stdout.0'] += b'a\nb\nc\n' * 10
        self.assertEqual(len([e for e in reader.poll() if 'line' in e]), 30)

    def test_incomplete_utf8_in_rotated_file_does_not_stall_new_file(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.files['alloc/logs/web.stdout.0'] += b'\xe4'
        client.files['alloc/logs/web.stdout.1'] = b'\xbd\xa0\nnext\n'
        events = reader.poll()
        self.assertIn('next', [e['line'] for e in events if 'line' in e])
        self.assertEqual(reader.positions[reader.key(reader.sources[0])][0], 'alloc/logs/web.stdout.1')

    def test_nonlatest_file_read_boundary_preserves_utf8_until_real_eof(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.files['alloc/logs/web.stdout.0'] += b'x' * (MAX_BYTES - 1) + '你好\n'.encode()
        client.files['alloc/logs/web.stdout.1'] = b'next\n'
        events = reader.poll() + reader.poll() + reader.poll()
        text = ''.join(e['line'] for e in events if 'line' in e)
        self.assertNotIn('\ufffd', text)
        self.assertTrue(text.endswith('你好next'))

    def test_byte_read_boundary_does_not_split_utf8(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        client.files['alloc/logs/web.stdout.0'] += '你好\n'.encode()
        reader._read_bytes = lambda source, file, offset, limit: client.read(source, file, offset, min(limit, 4))
        first = [e for e in reader.poll() if 'line' in e]
        second = [e for e in reader.poll() if 'line' in e]
        self.assertEqual([e['line'] for e in first + second], ['你', '好'])
        self.assertTrue(first[0]['partial'])
        self.assertTrue(second[0]['continued'])

    def test_cursor_cannot_change_scope_or_read_arbitrary_file(self):
        client = FakeNomad()
        reader = self.reader(client)
        reader.poll()
        with self.assertRaisesRegex(LumaError, 'invalid logs cursor'):
            self.reader(client, allocation='a1', cursor=reader.cursor())
        key = next(iter(reader.positions))
        reader.positions[key] = ['secrets/private.key', 0, False]
        resumed = self.reader(client, cursor=reader.cursor())
        resumed._read_bytes = Mock(side_effect=client.read)
        resumed.poll()
        self.assertTrue(all(call.args[1].startswith('alloc/logs/web.stdout.') for call in resumed._read_bytes.call_args_list))

    def test_invalid_cursor_is_bounded_and_rejected(self):
        for cursor in ['?' * 25000, 'broken', base64.urlsafe_b64encode(b'[]').decode()]:
            with self.subTest(cursor=cursor[:20]), self.assertRaises(LumaError):
                self.reader(cursor=cursor)

    def test_safe_ndjson_framing_and_no_fabricated_event_time(self):
        encoded = encode_event({'line': 'hello\n{"status":"error"}\r\u001b'})
        self.assertEqual(len(encoded.splitlines()), 1)
        self.assertEqual(json.loads(encoded)['line'], 'hello\n{"status":"error"}\r\u001b')
        event = next(e for e in self.reader().poll() if 'line' in e)
        self.assertNotIn('ts', event)
        self.assertIn('observedAt', event)

    def test_real_http_snapshot_then_append_resumes_at_exact_byte_offset(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from luma.nomad_api import NomadApi

        contents = [b'ready\n']
        offsets = []
        class NomadHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/v1/job/app/allocations':
                    payload = json.dumps(FakeNomad().allocations).encode()
                elif parsed.path.startswith('/v1/client/fs/ls/'):
                    payload = json.dumps([{'Name': 'web.stdout.0', 'Size': len(contents[0]), 'IsDir': False}]).encode()
                elif parsed.path.startswith('/v1/client/fs/readat/'):
                    query = parse_qs(parsed.query)
                    start, limit = int(query['offset'][0]), int(query['limit'][0])
                    offsets.append(start)
                    payload = contents[0][start:start + limit]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        http = ThreadingHTTPServer(('127.0.0.1', 0), NomadHandler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            reader = LogReader(NomadApi(f'http://127.0.0.1:{http.server_port}'), 'app', 'web', 'app', tail=1)
            self.assertEqual([e['line'] for e in reader.poll() if 'line' in e], ['ready'])
            contents[0] += b'later\n'
            self.assertEqual([e['line'] for e in reader.poll() if 'line' in e], ['later'])
            self.assertEqual(offsets, [0, 6])
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)

    def test_raw_reads_have_limit_timeout_and_close(self):
        reader = self.reader()
        del reader._read_bytes
        response = Mock()
        response.read.return_value = b'foo'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch('luma.control.logs.urllib.request.urlopen', return_value=context) as opened:
            reader._read_bytes({'allocationId': 'a1'}, 'alloc/logs/web.stdout.0', 7, 20)
        self.assertEqual(parse_qs(urlparse(opened.call_args.args[0].full_url).query), {'path': ['alloc/logs/web.stdout.0'], 'offset': ['7'], 'limit': ['20']})
        self.assertEqual(opened.call_args.kwargs['timeout'], 3)
        response.read.assert_called_once_with(20)
        context.__exit__.assert_called_once()



class DashboardLogEndpointTests(unittest.TestCase):
    def test_snapshot_query_selectors_have_asgi_legacy_parity(self):
        query = 'service=app&tail=99&allocation=a1&previous=true&cursor=resume'
        expected = {'tail': 99, 'since': '', 'allocation': 'a1', 'previous': True, 'cursor': 'resume'}
        with patch.object(server, 'handle_dashboard_logs', return_value={'logs': []}) as logs:
            handler = server.ControlHandler.__new__(server.ControlHandler)
            handler.path = '/v1/dashboard/logs?' + query
            handler.headers = {'Authorization': 'Bearer secret'}
            handler._json = Mock()
            handler.do_GET()
            self.assertEqual(logs.call_args.args, ('secret', 'app'))
            self.assertEqual(logs.call_args.kwargs, expected)
            request = server.Request({'type': 'http', 'method': 'GET', 'path': '/v1/dashboard/logs',
                                      'query_string': query.encode(), 'headers': [(b'authorization', b'Bearer secret')]})
            response = asyncio.run(server._asgi_authenticated_get(request))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(logs.call_args.args, ('secret', 'app'))
            self.assertEqual(logs.call_args.kwargs, expected)

    def test_temporary_discovery_outage_is_503_in_both_http_stacks(self):
        with patch.object(server, 'handle_dashboard_logs', side_effect=LogUnavailable('upstream unavailable')):
            handler = server.ControlHandler.__new__(server.ControlHandler)
            handler.path = '/v1/dashboard/logs?service=app'
            handler.headers = {'Authorization': 'Bearer secret'}
            handler._error = Mock()
            handler.do_GET()
            self.assertEqual(handler._error.call_args.args[0], 503)
            request = server.Request({'type': 'http', 'method': 'GET', 'path': '/v1/dashboard/logs',
                                      'query_string': b'service=app', 'headers': [(b'authorization', b'Bearer secret')]})
            self.assertEqual(asyncio.run(server._asgi_authenticated_get(request)).status_code, 503)

    def test_discovery_network_error_is_retryable_but_missing_service_is_invalid(self):
        client = FakeNomad()
        reader = LogReader(client, 'app', 'web', 'app')
        client.request = Mock(side_effect=LumaError('Nomad API unavailable'))
        with self.assertRaises(LogUnavailable):
            reader.discover()
        client.request.side_effect = LumaError('Nomad API error 404: job not found')
        with self.assertRaises(LumaError) as error:
            reader.discover()
        self.assertNotIsInstance(error.exception, LogUnavailable)

    def test_snapshot_legacy_fields_and_explicit_capabilities(self):
        client = FakeNomad()
        reader = LogReader(client, 'app', 'web', 'app')
        reader._read_bytes = client.read
        with patch.object(server, '_dashboard_log_reader', return_value=reader):
            result = server.handle_dashboard_logs('token', 'app')
        self.assertEqual(result['logs'], ['ready'])
        self.assertEqual(result['mode'], 'snapshot')
        self.assertFalse(result['capabilities']['since'])
        self.assertEqual(result['entries'][0]['allocationId'], 'a1')

    def test_authentication_precedes_selector_and_cursor_processing(self):
        with patch.object(server, 'load_state', return_value={}), patch.object(server, 'require_token', side_effect=LumaError('unauthorized')), patch.object(server, 'NomadApi') as client:
            with self.assertRaisesRegex(LumaError, 'unauthorized'):
                server._dashboard_log_reader('bad', '', since='1h', cursor='invalid')
            client.assert_not_called()

    def test_since_is_rejected_instead_of_silently_ignored(self):
        with patch.object(server, 'load_state', return_value={}), patch.object(server, 'require_token'), patch.object(server, 'NomadApi') as client:
            with self.assertRaisesRegex(LumaError, 'since filtering is unsupported'):
                server._dashboard_log_reader('ok', 'app', since='1h')
            client.assert_not_called()

    def test_asgi_continues_after_first_snapshot_and_stops_on_close(self):
        async def check():
            client = FakeNomad()
            reader = LogReader(client, 'app', 'web', 'app')
            reader._read_bytes = client.read
            with patch.object(server, '_dashboard_log_reader', return_value=reader), patch.object(server, 'LOG_POLL_INTERVAL', 0):
                response = await server._asgi_stream_service_logs('token', 'app', '', 120)
                iterator = response.body_iterator
                self.assertEqual(json.loads(await anext(iterator))['status'], 'start')
                self.assertEqual(json.loads(await anext(iterator))['line'], 'ready')
                self.assertEqual(json.loads(await anext(iterator))['status'], 'heartbeat')
                client.files['alloc/logs/web.stdout.0'] += b'later\n'
                self.assertEqual(json.loads(await anext(iterator))['line'], 'later')
                await iterator.aclose()
                with self.assertRaises(StopAsyncIteration):
                    await anext(iterator)
        asyncio.run(check())

    def test_legacy_handler_streams_later_bytes_and_stops_on_broken_pipe(self):
        client = FakeNomad()
        reader = LogReader(client, 'app', 'web', 'app')
        reader._read_bytes = client.read
        buffer = io.BytesIO()
        count = 0
        def write(data):
            nonlocal count
            event = json.loads(data)
            if event.get('status') == 'heartbeat':
                count += 1
                if count == 2:
                    raise BrokenPipeError()
                client.files['alloc/logs/web.stdout.0'] += b'later\n'
            buffer.write(data)
        handler = server.ControlHandler.__new__(server.ControlHandler)
        handler.wfile = Mock(write=write)
        handler.connection = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch.object(server, '_dashboard_log_reader', return_value=reader), patch.object(server.time, 'sleep'):
            handler._stream_service_logs('token', 'app', '', 120)
        events = [json.loads(line) for line in buffer.getvalue().splitlines()]
        self.assertEqual([e['line'] for e in events if 'line' in e], ['ready', 'later'])
        self.assertTrue(handler.close_connection)
        handler.connection.settimeout.assert_called_once_with(15)


if __name__ == '__main__':
    unittest.main()
