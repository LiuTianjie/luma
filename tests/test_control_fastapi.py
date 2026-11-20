"""Dedicated test suite for Luma Control's FastAPI + Pydantic v2 migration."""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from starlette.testclient import TestClient

from luma import __version__
from luma.control import addons as control_addons, server
from luma.control.state import init_state


class ControlFastApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        config_path = root / "luma.yaml"
        config_path.write_text("defaults:\n  engine: nomad\n", encoding="utf-8")
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "LUMA_CONTROL_STATE_DIR": str(root / "state"),
                    "LUMA_CONTROL_CONFIG": str(config_path),
                },
            )
        )
        state = init_state(domain="luma.example.com", cluster_id="fastapi-test", overwrite=True)
        self.token = state["deployToken"]
        self.join_token = state["joinToken"]
        self.app = server.create_app()
        self.client = self.enterContext(TestClient(self.app, base_url="https://luma.example.com"))

    def _headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token if token is None else token}"}

    def test_app_is_fastapi_with_public_docs_and_openapi_disabled(self) -> None:
        self.assertIsInstance(self.app, FastAPI)
        self.assertEqual(self.app.title, "Luma Control API")
        self.assertEqual(self.app.version, __version__)
        for public_path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=public_path):
                resp = self.client.get(public_path)
                self.assertEqual(resp.status_code, 404)

    def test_health_is_public_and_openapi_requires_deploy_token(self) -> None:
        health = self.client.get("/v1/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], __version__)
        self.assertIn("addons-v1", body["capabilities"])

        # Unauthenticated, wrong token, and join token must all be rejected.
        self.assertEqual(self.client.get("/v1/openapi.json").status_code, 401)
        self.assertEqual(
            self.client.get("/v1/openapi.json", headers=self._headers("wrong-token")).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/v1/openapi.json", headers=self._headers(self.join_token)).status_code,
            401,
        )

        openapi_resp = self.client.get("/v1/openapi.json", headers=self._headers())
        self.assertEqual(openapi_resp.status_code, 200, openapi_resp.text)
        self.assertEqual(openapi_resp.headers.get("cache-control"), "no-store")
        schema = openapi_resp.json()
        self.assertTrue(schema["openapi"].startswith("3.1"))
        self.assertEqual(schema["info"]["title"], "Luma Control API")
        self.assertEqual(schema["info"]["version"], __version__)

        paths = schema["paths"]
        # Catch-all legacy Starlette routes must NOT pollute the OpenAPI spec.
        self.assertNotIn("/v1/{path}", paths)
        self.assertNotIn("/dashboard", paths)

        expected_paths = {
            "/v1/health",
            "/v1/openapi.json",
            "/v1/status",
            "/v1/control-resources",
            "/v1/workflows",
            "/v1/workflows/check",
            "/v1/workflows/{workflow_name}",
            "/v1/addons",
            "/v1/addons/summary",
            "/v1/addons/{addon_id}",
            "/v1/addons/{addon_id}/configure",
            "/v1/addons/{addon_id}/render",
            "/v1/addons/{addon_id}/apply",
            "/v1/addons/{addon_id}/apply/stream",
            "/v1/addons/{addon_id}/uninstall",
            "/v1/dashboard/addons/eye/portfolio",
            "/v1/dashboard/addons/eye/app",
            "/v1/builder/status",
            "/v1/builder/builds",
            "/v1/builder/builds/{build_id}",
            "/v1/secrets",
            "/v1/secrets/remove",
            "/v1/regions",
            "/v1/regions/update",
            "/v1/regions/remove",
            "/v1/dns/zones",
            "/v1/dns/zones/check",
            "/v1/dns/zones/remove",
            "/v1/registries",
            "/v1/registries/remove",
            "/v1/git-providers",
            "/v1/git-providers/remove",
            "/v1/git-providers/{provider_id}/repositories",
            "/v1/git-providers/{provider_id}/repositories/{owner}/{repo}/refs",
            "/v1/cdn",
            "/v1/cdn/check",
            "/v1/cdn/purge",
            "/v1/cdn/remove",
        }
        self.assertTrue(expected_paths.issubset(set(paths.keys())))
        schemas = schema["components"]["schemas"]
        for model_name in (
            "SecretSetRequest",
            "RegionCreateRequest",
            "AddonConfigureRequest",
            "BuilderSubmitRequest",
            "WorkflowRecordRequest",
            "DnsZoneAddRequest",
            "RegistrySetRequest",
            "GitProviderSetRequest",
            "CdnPurgeRequest",
        ):
            self.assertIn(model_name, schemas)

    def test_auth_before_parse_and_body_size_limits_across_routers(self) -> None:
        for path in ("/v1/secrets", "/v1/regions", "/v1/workflows", "/v1/addons/eye/configure"):
            with self.subTest(path=path):
                # Missing auth + malformed JSON -> 401 (never 400).
                no_auth = self.client.post(
                    path,
                    content=b"{not-json",
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(no_auth.status_code, 401)

                # Invalid auth + huge Content-Length -> 401 (never 413).
                wrong_auth = self.client.post(
                    path,
                    content=b"{}",
                    headers={
                        "Authorization": "Bearer nope",
                        "Content-Length": str(server.MAX_AUTHENTICATED_BODY_BYTES + 4096),
                    },
                )
                self.assertEqual(wrong_auth.status_code, 401)

                # Valid auth + oversized body -> 413 body_too_large.
                with patch.object(server, "MAX_AUTHENTICATED_BODY_BYTES", 48):
                    oversized = self.client.post(
                        path,
                        content=b'{"name":"' + (b"a" * 80) + b'"}',
                        headers={**self._headers(), "Content-Type": "application/json"},
                    )
                self.assertEqual(oversized.status_code, 413)
                self.assertEqual(oversized.json()["errorInfo"]["code"], "body_too_large")

                # Valid auth + malformed JSON -> 400 luma_error.
                bad_json = self.client.post(
                    path,
                    content=b"{bad",
                    headers={**self._headers(), "Content-Type": "application/json"},
                )
                self.assertEqual(bad_json.status_code, 400)
                self.assertEqual(bad_json.json()["errorInfo"]["code"], "luma_error")

                # Valid auth + non-object JSON -> 400 luma_error.
                non_dict = self.client.post(
                    path,
                    content=b"[1, 2, 3]",
                    headers={**self._headers(), "Content-Type": "application/json"},
                )
                self.assertEqual(non_dict.status_code, 400)
                self.assertEqual(non_dict.json()["errorInfo"]["code"], "luma_error")

    def test_pydantic_validation_error_returns_legacy_400_luma_error(self) -> None:
        # Wrong field type for exposures (expects list[str]) triggers Pydantic validation.
        resp = self.client.post(
            "/v1/regions",
            headers=self._headers(),
            json={"name": "edge-lab", "exposures": "not-a-list"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        payload = resp.json()
        self.assertIn("error", payload)
        self.assertIn("requestId", payload)
        self.assertEqual(payload["errorInfo"]["code"], "luma_error")
        self.assertIn("exposures", payload["error"])

        # Wrong type for boolean flag triggers Pydantic validation -> 400 luma_error.
        resp2 = self.client.post(
            "/v1/addons/eye/uninstall",
            headers=self._headers(),
            json={"purgeSecrets": ["not-a-bool"]},
        )
        self.assertEqual(resp2.status_code, 400, resp2.text)
        self.assertEqual(resp2.json()["errorInfo"]["code"], "luma_error")

    def test_old_cli_extra_fields_and_missing_content_type_remain_compatible(self) -> None:
        # Extra legacy fields and omitted Content-Type header must still succeed.
        resp = self.client.post(
            "/v1/secrets",
            headers=self._headers(),
            content=json.dumps(
                {"name": "LEGACY_CLI_KEY", "value": "top-secret", "legacyClientField": "keep"}
            ).encode("utf-8"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["saved"])

        listed = self.client.get("/v1/secrets", headers=self._headers())
        self.assertEqual(listed.status_code, 200)
        self.assertIn("LEGACY_CLI_KEY", listed.json()["secrets"])

    def test_invalid_addon_and_builder_ids_return_404_after_auth(self) -> None:
        # Unauthenticated invalid addon_id still fails auth first with 401.
        self.assertEqual(self.client.get("/v1/addons/INVALID_ID").status_code, 401)
        # Authenticated invalid addon_id matches legacy regex failure -> 404.
        for method, path in (
            ("GET", "/v1/addons/INVALID_ID"),
            ("GET", "/v1/addons/9starts-with-digit"),
            ("POST", "/v1/addons/INVALID_ID/configure"),
            ("POST", "/v1/addons/INVALID_ID/render"),
            ("POST", "/v1/addons/INVALID_ID/apply"),
            ("POST", "/v1/addons/INVALID_ID/apply/stream"),
            ("POST", "/v1/addons/INVALID_ID/uninstall"),
            ("GET", "/v1/builder/builds/bad$build"),
        ):
            with self.subTest(method=method, path=path):
                if method == "GET":
                    resp = self.client.get(path, headers=self._headers())
                else:
                    resp = self.client.post(path, headers=self._headers(), json={})
                self.assertEqual(resp.status_code, 404, resp.text)
                self.assertEqual(resp.json()["errorInfo"]["code"], "not_found")

    def test_settings_and_system_routes_roundtrip_and_monkeypatch_lookup(self) -> None:
        # System routes
        self.assertEqual(self.client.get("/v1/control-resources", headers=self._headers()).status_code, 200)
        with patch.object(server, "handle_control_status", return_value={"clusterId": "patched"}) as mock_status:
            status_resp = self.client.get("/v1/status", headers=self._headers())
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json(), {"clusterId": "patched"})
        mock_status.assert_called_once_with(self.token)

        # Monkeypatching handlers on luma.control.server must be honored by routers.
        with patch.object(server, "handle_secret_list", return_value={"secrets": ["PATCHED_SECRET"]}) as mock_list:
            patched_secrets = self.client.get("/v1/secrets", headers=self._headers())
        self.assertEqual(patched_secrets.status_code, 200)
        self.assertEqual(patched_secrets.json(), {"secrets": ["PATCHED_SECRET"]})
        mock_list.assert_called_once_with(self.token)

        # Regions CRUD
        created = self.client.post(
            "/v1/regions",
            headers=self._headers(),
            json={"name": "gpu-lab", "egress": "direct", "exposures": ["none"]},
        )
        self.assertEqual(created.status_code, 200, created.text)
        updated = self.client.post(
            "/v1/regions/update",
            headers=self._headers(),
            json={"name": "gpu-lab", "exposures": ["none", "tailscale-relay"]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertIn("tailscale-relay", updated.json()["exposures"])
        listed_regions = self.client.get("/v1/regions", headers=self._headers())
        self.assertEqual(listed_regions.status_code, 200)
        self.assertIn("gpu-lab", [r["name"] for r in listed_regions.json()["regions"]])
        removed_region = self.client.post(
            "/v1/regions/remove",
            headers=self._headers(),
            json={"name": "gpu-lab"},
        )
        self.assertEqual(removed_region.status_code, 200)
        self.assertTrue(removed_region.json()["removed"])

        # Registries CRUD
        self.assertEqual(
            self.client.post(
                "/v1/registries",
                headers=self._headers(),
                json={"host": "ghcr.io", "username": "octo", "password": "secret"},
            ).status_code,
            200,
        )
        regs = self.client.get("/v1/registries", headers=self._headers()).json()["registries"]
        self.assertEqual([r["host"] for r in regs], ["ghcr.io"])
        self.assertTrue(
            self.client.post(
                "/v1/registries/remove",
                headers=self._headers(),
                json={"host": "ghcr.io"},
            ).json()["removed"]
        )

        # Git Providers CRUD + repository & ref routes
        gp_set = self.client.post(
            "/v1/git-providers",
            headers=self._headers(),
            json={"type": "github", "account": "acme", "token": "ghp_test"},
        )
        self.assertEqual(gp_set.status_code, 200, gp_set.text)
        self.assertEqual(gp_set.json()["id"], "github:acme")
        self.assertEqual(
            [p["id"] for p in self.client.get("/v1/git-providers", headers=self._headers()).json()["providers"]],
            ["github:acme"],
        )
        with patch.object(
            server,
            "handle_git_provider_repositories",
            return_value={"repositories": [{"fullName": "acme/web"}]},
        ) as mock_repos:
            repos_resp = self.client.get(
                "/v1/git-providers/github%3Aacme/repositories", headers=self._headers()
            )
        self.assertEqual(repos_resp.status_code, 200)
        self.assertEqual(repos_resp.json()["repositories"][0]["fullName"], "acme/web")
        mock_repos.assert_called_once_with(self.token, "github:acme")

        with patch.object(
            server,
            "handle_git_provider_refs",
            return_value={"refs": [{"name": "main", "type": "branch"}]},
        ) as mock_refs:
            refs_resp = self.client.get(
                "/v1/git-providers/github%3Aacme/repositories/acme/web/refs",
                headers=self._headers(),
            )
        self.assertEqual(refs_resp.status_code, 200)
        self.assertEqual(refs_resp.json()["refs"][0]["name"], "main")
        mock_refs.assert_called_once_with(self.token, "github:acme", "acme/web")

        self.assertTrue(
            self.client.post(
                "/v1/git-providers/remove",
                headers=self._headers(),
                json={"id": "github:acme"},
            ).json()["removed"]
        )

        # DNS zones + CDN list/check
        self.assertEqual(self.client.get("/v1/dns/zones", headers=self._headers()).status_code, 200)
        self.assertEqual(self.client.get("/v1/cdn", headers=self._headers()).status_code, 200)
        self.assertEqual(self.client.post("/v1/cdn/check", headers=self._headers(), json={}).status_code, 200)

    def test_addon_apply_stream_and_eye_dashboard_routes(self) -> None:
        compose_body = {"manifest": "name: luma-eye\n", "composeContent": "services: {}\n"}
        with patch.object(control_addons, "prepare_addon_apply_body", return_value=compose_body), patch.object(
            server, "handle_compose_deployment", return_value={"deployment": "luma-eye"}
        ) as mock_deploy:
            apply_resp = self.client.post(
                "/v1/addons/eye/apply", headers=self._headers(), json={"origin": "dashboard"}
            )
            self.assertEqual(apply_resp.status_code, 200, apply_resp.text)
            self.assertEqual(apply_resp.json(), {"deployment": "luma-eye"})
            mock_deploy.assert_called_once_with(self.token, compose_body)

            stream_resp = self.client.post(
                "/v1/addons/eye/apply/stream", headers=self._headers(), json={}
            )
            self.assertEqual(stream_resp.status_code, 200)
            self.assertIn("application/x-ndjson", stream_resp.headers.get("content-type", ""))
            lines = [json.loads(line) for line in stream_resp.text.splitlines() if line.strip()]
            self.assertEqual(lines[-1]["status"], "done")
            self.assertEqual(lines[-1]["result"], {"deployment": "luma-eye"})

        with patch.object(
            control_addons, "handle_addon_eye_portfolio", return_value={"installed": True, "since": "7d"}
        ) as mock_portfolio:
            port_resp = self.client.get(
                "/v1/dashboard/addons/eye/portfolio?since=7d", headers=self._headers()
            )
        self.assertEqual(port_resp.status_code, 200)
        self.assertEqual(port_resp.json()["since"], "7d")
        mock_portfolio.assert_called_once_with(self.token, since="7d")

        with patch.object(
            control_addons, "handle_addon_eye_app", return_value={"app": "web", "since": "24h"}
        ) as mock_app:
            app_resp = self.client.get(
                "/v1/dashboard/addons/eye/app?app=web&since=24h", headers=self._headers()
            )
        self.assertEqual(app_resp.status_code, 200)
        self.assertEqual(app_resp.json()["app"], "web")
        mock_app.assert_called_once_with(self.token, "web", since="24h")

    def test_python_39_ast_compatibility_for_all_control_modules(self) -> None:
        control_dir = Path(__file__).resolve().parents[1] / "luma" / "control"
        checked = 0
        for path in sorted(control_dir.rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))
            checked += 1
        self.assertGreaterEqual(checked, 6)


if __name__ == "__main__":
    unittest.main()
