"""Historical log contracts: authentication, bounded filters, paging and failure semantics."""
import asyncio
import base64
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from luma.cli import main
from luma.control import log_query, server
from luma.control.client import ControlClient
from luma.errors import LumaError


class LogQueryTests(unittest.TestCase):
    def query(self, params, rows):
        with patch.object(log_query, "load_state", return_value={}), \
                patch.object(log_query, "require_token"), \
                patch.object(log_query.time, "time", return_value=1800000000), \
                patch.object(log_query, "_read_rows", return_value=rows):
            return log_query.handle_log_query("secret", params)

    def test_denied_token_never_reaches_log_store(self):
        with patch.object(log_query, "load_state", return_value={}), \
                patch.object(log_query, "require_token", side_effect=LumaError("unauthorized")), \
                patch.object(log_query, "_read_rows") as backend:
            with self.assertRaisesRegex(LumaError, "unauthorized"):
                log_query.handle_log_query("wrong", {"app": "prod"})
        backend.assert_not_called()

    def test_literals_cannot_escape_app_or_inject_a_pipeline(self):
        query, limit, offset = log_query.normalize({
            "app": 'prod"} | stats count() #', "task": "worker",
            "contains": 'timeout.*" | limit 1', "exclude": "Bearer",
        }, 1800000000)
        expression = log_query.compile_query(query, limit, offset)
        self.assertTrue(expression.startswith('{app="prod\\\"} | stats count() #",task="worker"}'))
        self.assertIn(json.dumps(__import__('re').escape(query['contains'])), expression)
        self.assertIn('NOT _msg:~"Bearer"', expression)

    def test_cursor_freezes_window_filters_and_preserves_repeated_lines(self):
        rows = [{"_time": "2027-01-15T08:00:00Z", "_msg": "ERROR repeated", "task": "worker"}] * 3
        first = self.query({"app": "prod", "since": "1h", "contains": "repeated", "limit": 2}, rows)
        self.assertEqual(len(first["entries"]), 2)
        self.assertEqual(first["entries"][0], first["entries"][1])
        cursor = first["page"]["nextCursor"]
        frozen, limit, offset = log_query.normalize({"app": "prod", "cursor": cursor, "limit": 1}, 1800000300)
        self.assertEqual(frozen, first["query"])
        self.assertEqual((limit, offset), (1, 2))
        for params in ({"app": "other", "cursor": cursor},
                       {"app": "prod", "cursor": cursor, "since": "1h"}):
            with self.assertRaises(LumaError):
                log_query.normalize(params, 1800000300)

    def test_invalid_and_forged_requests_are_bounded(self):
        bad = [
            {"app": "*"}, {"app": "prod", "since": "2d"},
            {"app": "prod", "since": "2026-11-01T10:00:00"},
            {"app": "prod", "since": "nan"}, {"app": "prod", "limit": "501"},
            {"app": "prod", "stream": "arbitrary"}, {"app": "prod", "cursor": "!invalid"},
            {"app": "prod", "query": "*"},
            {"app": "prod", "cursor": base64.urlsafe_b64encode(json.dumps({
                "v": 1, "query": {"app": "prod"}, "offset": 10001,
            }).encode()).decode()},
        ]
        for params in bad:
            with self.subTest(params=params), self.assertRaises(LumaError):
                log_query.normalize(params, 1800000000)

    def test_redaction_and_caps_do_not_claim_complete_results(self):
        result = self.query({"app": "prod", "errors": "true", "limit": 1}, [
            {"_time": "now", "_msg": "Authorization: Bearer secret-token\n" + "x" * 33000},
            {"_msg": "more"},
        ])
        self.assertNotIn("secret-token", result["entries"][0]["line"])
        self.assertTrue(result["entries"][0]["truncated"])
        self.assertTrue(result["page"]["hasMore"])
        self.assertTrue(any("heuristic" in warning for warning in result["warnings"]))

    def test_backend_time_filters_are_parameters_and_outages_are_errors(self):
        query, limit, offset = log_query.normalize({"app": "prod"}, 1800000000)
        response = io.BytesIO(b'{"_time":"now","_msg":"hello"}\n')
        with patch.object(log_query.urllib.request, "urlopen", return_value=response) as opened:
            rows = log_query._read_rows(query, limit, offset)
        params = parse_qs(opened.call_args.args[0].data.decode())
        self.assertEqual(float(params["start"][0]), query["since"])
        self.assertEqual(float(params["end"][0]), query["until"])
        self.assertEqual(rows[0]["_msg"], "hello")
        with patch.object(log_query.urllib.request, "urlopen", side_effect=TimeoutError):
            with self.assertRaisesRegex(LumaError, "unavailable or timed out"):
                log_query._read_rows(query, limit, offset)

    def test_endpoint_and_client_keep_authenticated_scope(self):
        request = server.Request({
            "type": "http", "method": "GET", "path": "/v1/logs/query",
            "query_string": b'app=prod&contains=timeout',
            "headers": [(b'authorization', b'Bearer secret')],
        })
        with patch.object(server, "handle_log_query", return_value={"entries": []}) as handler:
            response = asyncio.run(server._asgi_authenticated_get(request))
        self.assertEqual(response.status_code, 200)
        handler.assert_called_once_with("secret", {"app": "prod", "contains": "timeout"})
        client = ControlClient("https://control.test", "secret")
        with patch.object(client, "request", return_value={}) as send:
            client.query_logs("prod", query={"contains": "a&app=other"})
        self.assertEqual(parse_qs(urlparse(send.call_args.args[1]).query),
                         {"app": ["prod"], "contains": ["a&app=other"]})

    def test_cli_exposes_structured_history_and_existing_incidents(self):
        client = Mock()
        client.query_logs.return_value = {"entries": [], "page": {"hasMore": False}}
        client.app_alerts.return_value = {"items": [], "nextCursor": None}
        with patch("luma.cli.apps._control_context", return_value=("https://control.test", "secret", False, None)), \
                patch("luma.cli.apps.common.ControlClient", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--no-env", "app", "log-query", "prod", "--since", "1h", "--errors", "--format", "json"]), 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])
            self.assertEqual(main(["--no-env", "app", "alerts", "prod", "--format", "json"]), 0)
        client.query_logs.assert_called_once_with("prod", query={"limit": 100, "since": "1h", "errors": "true"})
        client.app_alerts.assert_called_once_with("prod", query={"limit": 50})
