"""Dashboard builds use the independent queue and never pin legacy resources."""
import asyncio
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from luma.control import builder, server


class ControlBuilderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                calls.append((self.command, self.path, self.headers.get("Authorization"), body))
                payload = {"registry": {"registryHost": "acr.example.com"}, "runners": []} if self.path == "/v1/status" else {"build": {"id": "build-test", "status": "queued"}}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = handle_request

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        def close():
            self.http.shutdown()
            self.http.server_close()
            thread.join(2)
        self.addCleanup(close)
        state = {"deployToken": "operator-token", "gitProviders": {"github:account": {"type": "github"}}}
        self.enterContext(patch.object(builder, "load_auth_state", return_value=state))
        self.enterContext(patch.dict(os.environ, {"LUMA_BUILD_URL": f"http://127.0.0.1:{self.http.server_port}"}))

    def request(self, method, path, body=None, token="operator-token", query=""):
        raw = json.dumps(body or {}).encode()
        async def receive():
            return {"type": "http.request", "body": raw}
        request = server.Request({"type": "http", "method": method, "path": path, "query_string": query.encode(), "headers": [(b"authorization", f"Bearer {token}".encode())]}, receive)
        handler = server._asgi_authenticated_get if method == "GET" else server._asgi_authenticated_post
        return asyncio.run(handler(request))

    def test_dashboard_routes_submit_without_runner_or_registry_and_follow_the_same_build(self):
        response = self.request("GET", "/v1/builder/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["runners"], [])
        response = self.request("POST", "/v1/builder/builds", {
            "providerId": "github:account", "repository": "acme/app", "ref": "dev",
            "runner": "old-runner", "buildNode": "old-node", "registryHost": "localhost:5000",
            "pushHost": "localhost:5000", "url": "https://untrusted.example", "token": "not-forwarded",
            "envSecrets": {"DATABASE_URL": "runtime-secret"},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1][3], {"repoUrl": "https://github.com/acme/app.git", "ref": "dev", "deploy": True, "envSecrets": {"DATABASE_URL": "runtime-secret"}})
        response = self.request("GET", "/v1/builder/builds/build-test", query="afterSeq=40")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([call[:2] for call in self.calls], [("GET", "/v1/status"), ("POST", "/v1/builds"), ("GET", "/v1/builds/build-test?afterSeq=40")])
        self.assertTrue(all(call[2] == "Bearer operator-token" for call in self.calls))

    def test_unauthorized_requests_never_reach_builder(self):
        for method, path in [("GET", "/v1/builder/status"), ("POST", "/v1/builder/builds"), ("GET", "/v1/builder/builds/build-test")]:
            self.assertEqual(self.request(method, path, token="wrong").status_code, 401)
        self.assertEqual(self.calls, [])

    def test_discovery_accepts_public_service_or_compose_but_not_url_payloads(self):
        for kind, manifest in [("services", "name: luma-build\nexposure: cn-edge\ndomain: build.example.com"), ("compose", "name: luma-build\nservices:\n  server:\n    exposure: cn-edge\n    domain: build.example.com")]:
            with self.subTest(kind=kind):
                state = {"deployments": {kind: {"luma-build": {"manifest": manifest}}}}
                self.assertEqual(server._builder_service_url(state), "https://build.example.com")
                state["deployments"][kind]["luma-build"]["manifest"] = manifest.replace("build.example.com", "attacker@example.com/?token=secret")
                self.assertEqual(server._builder_service_url(state), "")
        self.assertEqual(server._builder_service_url({}), "")
