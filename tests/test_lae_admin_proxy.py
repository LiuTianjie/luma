from __future__ import annotations

import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager, redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from luma.control import server as control_server
from luma.control.server import ControlHandler, create_app
from luma.control.state import init_state, load_state, save_state
from luma.lae_admin_proxy import (
    LaeAdminProxyConfig,
    LaeAdminProxyError,
    fetch_lae_admin_resource,
    load_lae_admin_proxy_config,
)


class _Response:
    status = 200

    def __init__(self, body):
        self._stream = io.BytesIO(json.dumps(body).encode())
        self.headers = {"Content-Type": "application/json"}

    def read(self, amount=-1):
        return self._stream.read(amount)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _Opener:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append((request, kwargs))
        return _Response(self.body)


class _DynamicAdminOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append((request, kwargs))
        parsed = urllib.parse.urlsplit(request.full_url)
        resource = parsed.path.rsplit("/", 1)[-1]
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        limit = int(query["limit"][0])
        offset = int(query["offset"][0])
        return _Response(
            {
                resource: [],
                "page": {"limit": limit, "offset": offset, "total": offset},
            }
        )


class LaeAdminProxyTests(unittest.TestCase):
    def setUp(self):
        self.token = "lae-admin-proxy-token-" + "a" * 32
        self.config = LaeAdminProxyConfig(
            "https://lae-api.internal.example", self.token
        )

    def test_proxy_uses_internal_bearer_and_returns_validated_page(self):
        body = {
            "applications": [
                {"id": "app_test", "tenantId": "ten_test", "name": "Notes"}
            ],
            "page": {"limit": 100, "offset": 0, "total": 1},
        }
        opener = _Opener(body)
        result = fetch_lae_admin_resource(
            "applications", config=self.config, opener=opener
        )
        request, options = opener.requests[0]
        self.assertEqual(result, body)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
        self.assertEqual(options["timeout"], 8.0)
        self.assertIn("/internal/v1/admin/applications?", request.full_url)
        self.assertNotIn(self.token, repr(self.config))

    def test_proxy_fails_closed_on_secret_shape_binding_or_resource(self):
        bad_bodies = (
            {"applications": [], "page": {"limit": 1, "offset": 0, "total": 0}},
            {
                "applications": [{"id": "app", "deployToken": "secret"}],
                "page": {"limit": 100, "offset": 0, "total": 1},
            },
        )
        for body in bad_bodies:
            with self.subTest(body=body), self.assertRaises(LaeAdminProxyError):
                fetch_lae_admin_resource(
                    "applications", config=self.config, opener=_Opener(body)
                )
        with self.assertRaises(LaeAdminProxyError):
            fetch_lae_admin_resource("secrets", config=self.config)

    def test_config_requires_private_regular_token_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admin-token"
            path.write_text(self.token + "\n", encoding="utf-8")
            path.chmod(0o600)
            config = load_lae_admin_proxy_config(
                {
                    "LUMA_LAE_ADMIN_API_URL": "https://lae-api.internal.example",
                    "LUMA_LAE_ADMIN_TOKEN_FILE": str(path),
                }
            )
            self.assertNotIn(self.token, repr(config))
            path.chmod(0o644)
            with self.assertRaises(LaeAdminProxyError):
                load_lae_admin_proxy_config(
                    {
                        "LUMA_LAE_ADMIN_API_URL": "https://lae-api.internal.example",
                        "LUMA_LAE_ADMIN_TOKEN_FILE": str(path),
                    }
                )


class LaeAdminControlProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "luma.yaml"
        self.config_path.write_text("providers: {}\n", encoding="utf-8")
        self.builder_token = "lae-builder-token-" + "b" * 32
        self.runtime_token = "lae-runtime-token-" + "r" * 32
        self.admin_token = "lae-admin-internal-token-" + "a" * 32
        self.environment = patch.dict(
            os.environ,
            {
                "LUMA_CONTROL_STATE_DIR": str(self.state_dir),
                "LUMA_CONTROL_CONFIG": str(self.config_path),
                "LUMA_LAE_SERVICE_TOKEN": self.builder_token,
                "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_JSON": json.dumps(
                    {
                        "lae-runtime": {
                            "token": self.runtime_token,
                            "tenantRefs": ["*"],
                            "applicationRefs": ["*"],
                            "builderPrincipalRefs": ["lae-service"],
                            "scopes": ["runtime:deployments:read"],
                        }
                    }
                ),
            },
            clear=True,
        )
        self.environment.start()
        state = init_state(
            domain="luma.example.com",
            cluster_id="luma-lae-admin-control-test",
            overwrite=True,
        )
        self.management_token = state["deployToken"]

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    @contextmanager
    def configured_admin_proxy(self):
        token_file = self.root / "lae-admin.token"
        token_file.write_text(self.admin_token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        with patch.dict(
            os.environ,
            {
                "LUMA_LAE_ADMIN_API_URL": "https://lae-api.internal.example",
                "LUMA_LAE_ADMIN_TOKEN_FILE": str(token_file),
            },
            clear=False,
        ):
            yield

    @staticmethod
    def _headers(token):
        return {"Authorization": "Bearer " + token}

    def test_asgi_management_token_reads_all_resources_without_exposing_admin_token(self):
        opener = _DynamicAdminOpener()
        resources = ("users", "tenants", "applications", "operations", "usage")
        with self.configured_admin_proxy(), patch.object(
            control_server.urllib.request,
            "build_opener",
            return_value=opener,
        ), TestClient(create_app()) as client:
            for resource in resources:
                response = client.get(
                    f"/v1/dashboard/lae/{resource}?limit=25&offset=50",
                    headers=self._headers(self.management_token),
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(
                    response.json(),
                    {
                        resource: [],
                        "page": {"limit": 25, "offset": 50, "total": 50},
                    },
                )

            for token in (self.builder_token, self.runtime_token):
                rejected = client.get(
                    "/v1/dashboard/lae/users?limit=25&offset=50",
                    headers=self._headers(token),
                )
                self.assertEqual(rejected.status_code, 401, rejected.text)
                self.assertEqual(rejected.headers["cache-control"], "no-store")
                self.assertEqual(rejected.json()["error"], "unauthorized")

        self.assertEqual(len(opener.requests), len(resources))
        for request, options in opener.requests:
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer " + self.admin_token,
            )
            self.assertEqual(options["timeout"], 8.0)
        state_text = self.state_dir.joinpath("control.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(self.admin_token, state_text)

    def test_sync_http_route_accepts_only_management_token_and_never_caches(self):
        opener = _DynamicAdminOpener()

        class QuietControlHandler(ControlHandler):
            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, token):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            try:
                connection.request("GET", path, headers=self._headers(token))
                response = connection.getresponse()
                return response.status, dict(response.headers), response.read()
            finally:
                connection.close()

        try:
            with self.configured_admin_proxy(), patch.object(
                control_server.urllib.request,
                "build_opener",
                return_value=opener,
            ):
                status, headers, body = request(
                    "/v1/dashboard/lae/applications?limit=200&offset=1000000",
                    self.management_token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(
                    json.loads(body),
                    {
                        "applications": [],
                        "page": {
                            "limit": 200,
                            "offset": 1_000_000,
                            "total": 1_000_000,
                        },
                    },
                )
                for token in (self.builder_token, self.runtime_token):
                    status, headers, _body = request(
                        "/v1/dashboard/lae/applications", token
                    )
                    self.assertEqual(status, 401)
                    self.assertEqual(headers["Cache-Control"], "no-store")
            sync_logs = io.StringIO()
            with redirect_stderr(sync_logs):
                status, headers, body = request(
                    "/v1/dashboard/lae/applications",
                    self.management_token,
                )
            self.assertEqual(status, 503)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(
                json.loads(body)["error"],
                "LAE admin API is unavailable",
            )
            self.assertNotIn(self.admin_token, sync_logs.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(len(opener.requests), 1)

    def test_resource_and_query_parsing_are_closed_and_canonical(self):
        fetch = patch.object(
            control_server,
            "fetch_lae_admin_resource",
            return_value={
                "users": [],
                "page": {"limit": 100, "offset": 0, "total": 0},
            },
        )
        invalid_paths = (
            "/v1/dashboard/lae/secrets",
            "/v1/dashboard/lae/users?limit=0",
            "/v1/dashboard/lae/users?limit=201",
            "/v1/dashboard/lae/users?limit=01",
            "/v1/dashboard/lae/users?limit=%2B1",
            "/v1/dashboard/lae/users?offset=-1",
            "/v1/dashboard/lae/users?offset=1000001",
            "/v1/dashboard/lae/users?offset=00",
            "/v1/dashboard/lae/users?limit=10&limit=20",
            "/v1/dashboard/lae/users?unknown=1",
            "/v1/dashboard/lae/users?limit=",
            "/v1/dashboard/lae/users?limit=10&",
        )
        with fetch as mocked, TestClient(create_app()) as client:
            for path in invalid_paths:
                with self.subTest(path=path):
                    response = client.get(
                        path,
                        headers=self._headers(self.management_token),
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(response.json()["error"], "LAE admin query is invalid")
            extra_path = client.get(
                "/v1/dashboard/lae/users/extra",
                headers=self._headers(self.management_token),
            )
            self.assertEqual(extra_path.status_code, 404)
            self.assertEqual(extra_path.headers["cache-control"], "no-store")
            valid = client.get(
                "/v1/dashboard/lae/users",
                headers=self._headers(self.management_token),
            )
            self.assertEqual(valid.status_code, 200, valid.text)
            mocked.assert_called_once_with("users", limit=100, offset=0)

    def test_management_only_placement_view_observes_actual_nomad_node_locally(self):
        state = load_state()
        state["nomadToken"] = "nomad-admin-token"
        state["laeRuntime"] = {
            "deployments": {
                "lae-run-placement": {
                    "runtimeDeploymentRef": "lae-run-placement",
                    "tenantRef": "tenant-placement",
                    "applicationRef": "application-placement",
                    "deploymentRef": "deployment-placement",
                    "jobSlug": "lae-placement-job",
                    "status": "running",
                    "manifest": {"region": "cn"},
                    "placement": {
                        "candidateNodeIds": ["node-b", "node-a"],
                        "preferredNodeId": "node-a",
                        "summary": {
                            "region": "cn",
                            "stateful": True,
                            "continuity": "preferred",
                            "decisionDigest": "sha256:" + "a" * 64,
                        },
                    },
                    "updatedAt": 123456,
                }
            }
        }
        save_state(state)
        allocations = [
            {
                "ID": "allocation-current",
                "NodeID": "node-a",
                "NodeName": "tecent.internal",
                "DesiredStatus": "run",
                "ClientStatus": "running",
            },
            {
                "ID": "allocation-old",
                "NodeID": "node-b",
                "NodeName": "aly.internal",
                "DesiredStatus": "stop",
                "ClientStatus": "complete",
            },
        ]
        with patch.object(
            control_server.NomadApi,
            "request",
            return_value=allocations,
        ) as nomad, patch.object(
            control_server,
            "fetch_lae_admin_resource",
        ) as upstream, TestClient(create_app()) as client:
            response = client.get(
                "/v1/dashboard/lae/placements?limit=10&offset=0",
                headers=self._headers(self.management_token),
            )
            rejected = client.get(
                "/v1/dashboard/lae/placements",
                headers=self._headers(self.runtime_token),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(rejected.status_code, 401)
        body = response.json()
        self.assertEqual(body["page"], {"limit": 10, "offset": 0, "total": 1})
        placement = body["placements"][0]
        self.assertEqual(placement["candidateNodeIds"], ["node-a", "node-b"])
        self.assertEqual(
            placement["activeAllocations"],
            [
                {
                    "allocationId": "allocation-current",
                    "nodeId": "node-a",
                    "nodeName": "tecent.internal",
                    "status": "running",
                }
            ],
        )
        self.assertEqual(placement["observationStatus"], "observed")
        nomad.assert_called_once_with(
            "GET", "/v1/job/lae-placement-job/allocations"
        )
        upstream.assert_not_called()

    def test_unconfigured_and_unexpected_upstream_fail_as_generic_503(self):
        log_output = io.StringIO()
        with redirect_stderr(log_output), TestClient(create_app()) as client:
            unavailable = client.get(
                "/v1/dashboard/lae/users",
                headers=self._headers(self.management_token),
            )
            self.assertEqual(unavailable.status_code, 503, unavailable.text)
            self.assertEqual(unavailable.headers["cache-control"], "no-store")
            self.assertEqual(
                unavailable.json()["error"],
                "LAE admin API is unavailable",
            )
            self.assertEqual(
                unavailable.json()["errorInfo"]["code"],
                "lae_admin_unavailable",
            )

            canary = "https://upstream.invalid/?token=admin-log-canary"
            with patch.object(
                control_server,
                "fetch_lae_admin_resource",
                side_effect=RuntimeError(canary),
            ):
                collapsed = client.get(
                    "/v1/dashboard/lae/users",
                    headers=self._headers(self.management_token),
                )
            self.assertEqual(collapsed.status_code, 503, collapsed.text)
            self.assertNotIn("admin-log-canary", collapsed.text)
            self.assertEqual(
                collapsed.json()["error"],
                "LAE admin API is unavailable",
            )

            dashboard = client.get(
                "/v1/dashboard",
                headers=self._headers(self.management_token),
            )
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
            self.assertEqual(
                dashboard.json()["readiness"]["laeAdmin"],
                {"available": False},
            )
            self.assertNotIn("LUMA_LAE_ADMIN", dashboard.text)
            self.assertNotIn("admin-log-canary", dashboard.text)
        self.assertNotIn("admin-log-canary", log_output.getvalue())
        self.assertNotIn(self.admin_token, log_output.getvalue())


if __name__ == "__main__":
    unittest.main()
