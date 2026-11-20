"""Tests for the unified Luma Addon catalog, lifecycle API, and generalized Control SSO."""
import hashlib
import os
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import yaml

from luma import addons as addon_specs
from luma.control import addons as control_addons, dashboard_auth, database, state as control_state
from tests.test_dashboard_auth import DashboardAuthBase, ORIGIN, b64url

EYE_DOMAIN = "eye.example.com"
EYE_ORIGIN = f"https://{EYE_DOMAIN}"
BUILDER_DOMAIN = "build.example.com"
BUILDER_ORIGIN = f"https://{BUILDER_DOMAIN}"


class AddonPlatformTests(DashboardAuthBase):
    def setUp(self):
        super().setUp()
        self.parent = self.token_session()
        self.with_session(self.parent)

    def _pkce(self):
        verifier = secrets.token_urlsafe(32)
        challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        return verifier, challenge

    def test_catalog_loads_all_official_addons(self):
        catalog = addon_specs.load_addon_catalog()
        self.assertEqual(list(catalog.keys())[:3], ["builder", "eye", "observe"])
        self.assertEqual(catalog["eye"]["auth"]["mode"], "control-sso")
        self.assertEqual(catalog["builder"]["auth"]["mode"], "control-sso")
        self.assertEqual(catalog["observe"]["auth"]["mode"], "forward-auth")

    def test_auto_discovery_of_existing_deployed_stacks(self):
        def seed(persisted):
            persisted.setdefault("nodes", {})["bot"] = {"name": "bot", "region": "global"}
            persisted.setdefault("deployments", {}).setdefault("compose", {})["luma-eye"] = {
                "name": "luma-eye",
                "status": "active",
                "manifest": (
                    "name: luma-eye\n"
                    "compose: docker-compose.yml\n"
                    "region: global\n"
                    "services:\n"
                    "  eye:\n"
                    "    node: bot\n"
                    "    exposure: node-edge\n"
                    f"    domain: {EYE_DOMAIN}\n"
                    "    port: 8090\n"
                    "  clickhouse:\n"
                    "    exposure: none\n"
                ),
                "composeContent": (
                    "services:\n"
                    "  eye:\n"
                    "    image: registry.example.com/luma-eye/eye:sha-abc\n"
                    "    environment:\n"
                    "      LUMA_EYE_RETENTION_DAYS: '14'\n"
                    "      LUMA_EYE_MAX_STORAGE_GB: '25'\n"
                    "  clickhouse:\n"
                    "    image: registry.example.com/luma-eye/ch:sha-def\n"
                ),
            }

        control_state.mutate_state(seed)
        resp = self.client.get("/v1/addons", headers=self.bearer())
        self.assertEqual(resp.status_code, 200, resp.text)
        by_id = {item["id"]: item for item in resp.json()["addons"]}
        eye = by_id["eye"]
        self.assertTrue(eye["installed"])
        self.assertTrue(eye["autoDiscovered"])
        self.assertEqual(eye["status"], "running")
        self.assertEqual(eye["preset"], "node-edge")
        self.assertEqual(eye["url"], EYE_ORIGIN)
        self.assertEqual(eye["consoleUrl"], f"{EYE_ORIGIN}/console/")
        self.assertTrue(eye["ssoReady"])
        self.assertTrue(eye["canApplyInPlace"])
        self.assertEqual(eye["config"]["node"], "bot")
        self.assertEqual(eye["config"]["retentionDays"], 14)
        self.assertEqual(eye["config"]["maxStorageGb"], 25)

    def test_configure_auto_generates_secrets_wires_control_url_and_renders_cleanly(self):
        control_state.mutate_state(lambda p: p.setdefault("nodes", {}).update({"bot": {"name": "bot", "region": "global"}}))

        resp = self.client.post(
            "/v1/addons/eye/configure",
            headers=self.bearer(),
            json={
                "preset": "node-edge",
                "config": {
                    "domain": EYE_DOMAIN,
                    "region": "global",
                    "node": "bot",
                    "exposure": "node-edge",
                    "retentionDays": 10,
                    "maxStorageGb": 20,
                },
                "secrets": {
                    "LUMA_EYE_FEISHU_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/secret",
                },
                "requireComplete": True,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(
            set(body["generatedSecrets"]),
            {"LUMA_EYE_ADMIN_TOKEN", "LUMA_EYE_AUTO_KEY", "LUMA_EYE_RELAY_SECRET"},
        )
        eye = body["addon"]
        self.assertEqual(eye["url"], EYE_ORIGIN)
        self.assertEqual(eye["config"]["autoOriginSuffixes"], ".example.com,localhost,127.0.0.1")
        self.assertTrue(eye["secretsStatus"]["LUMA_EYE_ADMIN_TOKEN"])
        self.assertTrue(eye["secretsStatus"]["LUMA_EYE_FEISHU_WEBHOOK"])
        self.assertTrue(eye["secretsStatus"]["LUMA_CONTROL_URL"])
        # Secret values must never leak in API responses.
        self.assertNotIn("open.feishu.cn/open-apis/bot/v2/hook/secret", resp.text)

        # Second configure preserves existing auto-generated secrets.
        before_secrets = dict(control_state.load_state()["scopedSecrets"]["luma-eye"])
        self.assertEqual(before_secrets["LUMA_CONTROL_URL"], ORIGIN)
        self.assertEqual(before_secrets["LUMA_EYE_PUBLIC_URL"], EYE_ORIGIN)
        resp2 = self.client.post(
            "/v1/addons/eye/configure",
            headers=self.bearer(),
            json={"config": {"retentionDays": 14}},
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        self.assertEqual(resp2.json()["generatedSecrets"], [])
        after_secrets = control_state.load_state()["scopedSecrets"]["luma-eye"]
        self.assertEqual(after_secrets["LUMA_EYE_ADMIN_TOKEN"], before_secrets["LUMA_EYE_ADMIN_TOKEN"])

        # Render for source build includes build contexts, unified Control URL, and preserved volume names.
        rendered = self.client.post(
            "/v1/addons/eye/render",
            headers=self.bearer(),
            json={"forSourceBuild": True},
        )
        self.assertEqual(rendered.status_code, 200, rendered.text)
        compose_yaml = rendered.json()["rendered"]["composeYaml"]
        sidecar_yaml = rendered.json()["rendered"]["sidecarYaml"]
        self.assertIn(f"LUMA_CONTROL_URL: {ORIGIN}", compose_yaml)
        self.assertIn("LUMA_EYE_RETENTION_DAYS: '14'", compose_yaml)
        self.assertIn("eye-data:/var/lib/luma-eye", compose_yaml)
        self.assertIn("clickhouse-data:/var/lib/clickhouse", compose_yaml)
        self.assertIn("dockerfile: Dockerfile.clickhouse", compose_yaml)
        self.assertIn(f"domain: {EYE_DOMAIN}", sidecar_yaml)
        self.assertIn("node: bot", sidecar_yaml)

    def test_unified_addon_sso_cross_addon_isolation_and_cascading_revocation(self):
        # Register both eye and builder on distinct HTTPS origins.
        self.assertEqual(
            self.client.post(
                "/v1/addons/eye/configure",
                headers=self.bearer(),
                json={"config": {"domain": EYE_DOMAIN, "region": "cn", "exposure": "cn-edge"}},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/v1/addons/builder/configure",
                headers=self.bearer(),
                json={"config": {"domain": BUILDER_DOMAIN, "region": "cn", "exposure": "cn-edge"}},
            ).status_code,
            200,
        )

        # Info endpoint exposes safe display name for /dashboard/addon-login
        info = self.client.get("/v1/auth/addon/eye/info")
        self.assertEqual(info.status_code, 200, info.text)
        self.assertEqual(info.json(), {"addonId": "eye", "name": "Luma Eye"})

        # Authorize + exchange for eye
        eye_verifier, eye_challenge = self._pkce()
        auth_eye = self.client.post(
            "/v1/auth/addon/eye/authorize",
            headers={"Origin": ORIGIN},
            json={"challenge": eye_challenge},
        )
        self.assertEqual(auth_eye.status_code, 200, auth_eye.text)
        eye_redirect = auth_eye.json()["redirectUrl"]
        self.assertEqual(urlsplit(eye_redirect).netloc, EYE_DOMAIN)
        eye_code = parse_qs(urlsplit(eye_redirect).query)["code"][0]

        # An eye ticket MUST NOT be exchangeable by builder
        cross_exchange = self.client.post(
            "/v1/auth/addon/builder/exchange",
            json={"code": eye_code, "verifier": eye_verifier},
        )
        self.assertEqual(cross_exchange.status_code, 401)

        ex_eye = self.client.post(
            "/v1/auth/addon/eye/exchange",
            json={"code": eye_code, "verifier": eye_verifier},
        )
        self.assertEqual(ex_eye.status_code, 200, ex_eye.text)
        eye_tok = ex_eye.json()["token"]
        self.assertTrue(eye_tok.startswith("las_"))

        # Authorize + exchange for builder
        bld_verifier, bld_challenge = self._pkce()
        auth_bld = self.client.post(
            "/v1/auth/addon/builder/authorize",
            headers={"Origin": ORIGIN},
            json={"challenge": bld_challenge},
        )
        self.assertEqual(auth_bld.status_code, 200, auth_bld.text)
        bld_code = parse_qs(urlsplit(auth_bld.json()["redirectUrl"]).query)["code"][0]
        ex_bld = self.client.post(
            "/v1/auth/addon/builder/exchange",
            json={"code": bld_code, "verifier": bld_verifier},
        )
        self.assertEqual(ex_bld.status_code, 200, ex_bld.text)
        bld_tok = ex_bld.json()["token"]

        # Both verify under their own addonId, and reject cross-addon token use
        self.assertEqual(self.client.post("/v1/auth/addon/eye/verify", headers={"Authorization": f"Bearer {eye_tok}"}).status_code, 200)
        self.assertEqual(self.client.post("/v1/auth/addon/builder/verify", headers={"Authorization": f"Bearer {bld_tok}"}).status_code, 200)
        self.assertEqual(self.client.post("/v1/auth/addon/builder/verify", headers={"Authorization": f"Bearer {eye_tok}"}).status_code, 401)
        self.assertEqual(self.client.post("/v1/auth/addon/eye/verify", headers={"Authorization": f"Bearer {bld_tok}"}).status_code, 401)

        # Single Control logout immediately invalidates BOTH Eye and Builder sessions!
        self.assertEqual(self.client.post("/v1/auth/logout", headers={"Origin": ORIGIN}).status_code, 200)
        self.assertEqual(self.client.post("/v1/auth/addon/eye/verify", headers={"Authorization": f"Bearer {eye_tok}"}).status_code, 401)
        self.assertEqual(self.client.post("/v1/auth/addon/builder/verify", headers={"Authorization": f"Bearer {bld_tok}"}).status_code, 401)

    def test_uninstall_revokes_addon_sso_sessions(self):
        self.assertEqual(
            self.client.post(
                "/v1/addons/eye/configure",
                headers=self.bearer(),
                json={"config": {"domain": EYE_DOMAIN, "region": "cn", "exposure": "cn-edge"}},
            ).status_code,
            200,
        )
        verifier, challenge = self._pkce()
        auth_resp = self.client.post("/v1/auth/addon/eye/authorize", headers={"Origin": ORIGIN}, json={"challenge": challenge})
        code = parse_qs(urlsplit(auth_resp.json()["redirectUrl"]).query)["code"][0]
        tok = self.client.post("/v1/auth/addon/eye/exchange", json={"code": code, "verifier": verifier}).json()["token"]
        self.assertEqual(self.client.post("/v1/auth/addon/eye/verify", headers={"Authorization": f"Bearer {tok}"}).status_code, 200)

        uninst = self.client.post("/v1/addons/eye/uninstall", headers=self.bearer(), json={"purgeSecrets": True})
        self.assertEqual(uninst.status_code, 200, uninst.text)
        self.assertTrue(uninst.json()["uninstalled"])
        with database.transaction(immediate=False) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM dashboard_addon_sessions WHERE addon_id='eye'").fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertNotIn("luma-eye", control_state.load_state().get("scopedSecrets") or {})

    def test_load_external_addon_spec(self):
        custom = Path(self._tmp.name) / "addon.yaml"
        custom.write_text(
            "apiVersion: luma.addon/v1\n"
            "id: custom-metrics\n"
            "name: Custom Metrics\n"
            "stack: custom-metrics\n"
            "auth:\n"
            "  mode: control-sso\n",
            encoding="utf-8",
        )
        spec = addon_specs.load_addon_spec(custom)
        self.assertEqual(spec["id"], "custom-metrics")
        self.assertEqual(spec["auth"]["mode"], "control-sso")
        self.assertIsNotNone(dashboard_auth)
        self.assertIsNotNone(os)
        self.assertIsNotNone(patch)

    def test_one_click_default_images_and_bundled_observe_parity(self):
        catalog = addon_specs.load_addon_catalog()
        for addon_id in ("builder", "eye", "observe"):
            spec = catalog[addon_id]
            self.assertTrue(spec["uiCapabilities"], f"{addon_id} should declare uiCapabilities")
            self.assertTrue(spec["defaultImages"], f"{addon_id} should declare defaultImages")

        # Fresh first-time browser install (no existing compose, no --source) works for all 3 addons
        builder_rendered = addon_specs.render_addon_manifests(
            catalog["builder"],
            {"domain": "build.example.com", "region": "cn", "exposure": "cn-edge", "node": "manager"},
            control_url="https://luma.example.com",
            scoped_secrets={"LUMA_DEPLOY_TOKEN": "tok"},
            for_source_build=False,
        )
        builder_compose = yaml.safe_load(builder_rendered["composeYaml"])
        self.assertEqual(builder_compose["services"]["server"]["image"], catalog["builder"]["defaultImages"]["server"])
        self.assertNotIn("build", builder_compose["services"]["server"])

        eye_rendered = addon_specs.render_addon_manifests(
            catalog["eye"],
            {"domain": "eye.example.com", "region": "global", "exposure": "node-edge", "node": "edge-1"},
            preset_id="node-edge",
            control_url="https://luma.example.com",
            scoped_secrets={"LUMA_EYE_ADMIN_TOKEN": "a", "LUMA_EYE_RELAY_SECRET": "b", "LUMA_EYE_AUTO_KEY": "c"},
            for_source_build=False,
        )
        eye_compose = yaml.safe_load(eye_rendered["composeYaml"])
        self.assertEqual(eye_compose["services"]["eye"]["image"], catalog["eye"]["defaultImages"]["eye"])
        self.assertEqual(eye_compose["services"]["clickhouse"]["image"], catalog["eye"]["defaultImages"]["clickhouse"])
        self.assertNotIn("build", eye_compose["services"]["eye"])

        obs_rendered = addon_specs.render_addon_manifests(
            catalog["observe"],
            {"region": "global"},
            control_url="https://luma.example.com",
            scoped_secrets={"LUMA_OBSERVE_READ_TOKEN": "r", "LUMA_OBSERVE_INGEST_TOKEN": "i"},
            for_source_build=False,
        )
        obs_sidecar = yaml.safe_load(obs_rendered["sidecarYaml"])
        obs_compose = yaml.safe_load(obs_rendered["composeYaml"])
        self.assertEqual(obs_sidecar["region"], "global")
        for svc_name, svc in obs_compose["services"].items():
            self.assertNotIn("build", svc, f"bundled observe service {svc_name} must not require local build")
            self.assertTrue(svc.get("image"), f"bundled observe service {svc_name} missing image")

        # Verify bundled observe compose stays in sync with observe/docker-compose.yml service set
        repo_root = Path(__file__).resolve().parents[1]
        source_observe = yaml.safe_load((repo_root / "observe" / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertEqual(set(obs_compose["services"].keys()), set(source_observe["services"].keys()))

    def test_builder_inherits_saved_control_credentials_without_leaking_secrets(self):
        control_state.mutate_state(lambda s: s.update({
            "registries": {
                "ghcr.io": {"serverAddress": "ghcr.io", "username": "octocat", "password": "ghp_secret_registry_value"},
            },
            "build": {"registryHost": "ghcr.io", "pushHost": "ghcr.io"},
            "gitProviders": {
                "github:octocat": {"type": "github", "account": "octocat", "token": "ghp_secret_git_value"},
                "gitea:dev": {"type": "gitea", "account": "dev", "username": "dev", "baseUrl": "https://git.example.com", "token": "gitea_secret_value"},
            },
        }))

        listed_resp = self.client.get("/v1/addons", headers=self.bearer())
        self.assertEqual(listed_resp.status_code, 200, listed_resp.text)
        creds = listed_resp.json()["availableCredentials"]
        self.assertEqual(creds["registries"][0]["host"], "ghcr.io")
        self.assertNotIn("password", creds["registries"][0])
        self.assertEqual(len(creds["gitProviders"]), 2)
        for gp in creds["gitProviders"]:
            self.assertNotIn("token", gp)

        cfg_resp = self.client.post(
            "/v1/addons/builder/configure",
            headers=self.bearer(),
            json={
                "config": {"domain": "build.example.com", "region": "cn", "exposure": "cn-edge", "node": "manager", "registryNamespace": "acme"},
                "inherit": {"registryHost": "ghcr.io", "gitProviderIds": ["github:octocat", "gitea:dev"]},
            },
        )
        self.assertEqual(cfg_resp.status_code, 200, cfg_resp.text)
        res = cfg_resp.json()
        self.assertEqual(
            res["inheritedCredentials"],
            ["registry:ghcr.io", "git:github:octocat", "git:gitea:dev"],
        )
        saved_state = control_state.load_state()
        scoped = saved_state["scopedSecrets"]["luma-build"]
        self.assertEqual(scoped["LUMA_BUILD_REGISTRY_HOST"], "ghcr.io")
        self.assertEqual(scoped["LUMA_BUILD_PUSH_HOST"], "ghcr.io")
        self.assertEqual(scoped["LUMA_BUILD_REGISTRY_USERNAME"], "octocat")
        self.assertEqual(scoped["LUMA_BUILD_REGISTRY_PASSWORD"], "ghp_secret_registry_value")
        self.assertEqual(scoped["GITHUB_TOKEN"], "ghp_secret_git_value")
        self.assertEqual(scoped["GITEA_HOST"], "git.example.com")
        self.assertEqual(scoped["GITEA_USERNAME"], "dev")
        self.assertEqual(scoped["GITEA_TOKEN"], "gitea_secret_value")

    def test_addon_capabilities_summary_and_eye_app_matching(self):
        # Zero addons installed -> empty active/capabilities via /v1/addons/summary
        empty_resp = self.client.get("/v1/addons/summary", headers=self.bearer())
        self.assertEqual(empty_resp.status_code, 200, empty_resp.text)
        empty_summary = empty_resp.json()
        self.assertTrue(empty_summary["known"])
        self.assertEqual(empty_summary["active"], [])
        self.assertEqual(empty_summary["capabilities"], [])

        # Activate observe & eye deployments -> summary reflects both accurately
        control_state.mutate_state(lambda s: s.setdefault("deployments", {}).setdefault("compose", {}).update({
            "luma-observe": {"status": "active", "manifest": "name: luma-observe\nregion: cn\n"},
            "luma-eye": {
                "status": "active",
                "manifest": "name: luma-eye\nregion: global\nservices:\n  eye:\n    exposure: node-edge\n    domain: eye.example.com\n",
            },
            "web-app": {
                "status": "active",
                "manifest": "name: web-app\nregion: cn\nservices:\n  web:\n    domain: app.example.com\n",
            },
        }))
        summary = self.client.get("/v1/addons/summary", headers=self.bearer()).json()
        self.assertEqual(summary["active"], ["eye", "observe"])
        self.assertIn("eye.rum", summary["capabilities"])
        self.assertIn("observe.dashboards", summary["capabilities"])
        self.assertNotIn("builder.git_import", summary["capabilities"])
        self.assertEqual(summary["urls"]["eye"], "https://eye.example.com/console/")
        self.assertEqual(summary["urls"]["observe"], f"{ORIGIN}/grafana")

        # Eye project matching by slug, normalized slug, and configured domain
        sample_projects = [
            {
                "slug": "acme-web",
                "name": "Acme Web",
                "publicKey": "pk_1",
                "allowedOrigins": ["https://www.acme.example.com"],
                "consoleUrl": "https://eye.example.com/console/?project=acme-web",
                "uniqueVisitors": 42,
                "pageviews": 99,
                "errorCount": 0,
                "crashFreeRate": 100.0,
            },
            {
                "slug": "marketing",
                "name": "Marketing",
                "publicKey": "pk_2",
                "allowedOrigins": ["https://app.example.com"],
                "consoleUrl": "https://eye.example.com/console/?project=marketing",
                "uniqueVisitors": 10,
                "pageviews": 20,
                "errorCount": 1,
                "crashFreeRate": 95.0,
            },
        ]
        self.assertEqual(control_addons._match_eye_project(sample_projects, "acme-web", set())["slug"], "acme-web")
        self.assertEqual(control_addons._match_eye_project(sample_projects, "acme_web", set())["slug"], "acme-web")
        self.assertEqual(control_addons._match_eye_project(sample_projects, "web-app", {"app.example.com"})["slug"], "marketing")
        self.assertIsNone(control_addons._match_eye_project(sample_projects, "unmatched-api", set()))

    def test_cli_deploy_from_source_builds_complete_parser_namespace(self):
        from luma.cli import addons as cli_addons
        from luma.cli import parser as cli_parser

        # Regression test: _deploy_from_source must pass a full parsed Namespace (including --config)
        # rather than a partial manual argparse.Namespace missing global parser options.
        captured = {}

        def fake_cmd_build(ns):
            captured["ns"] = ns
            return 0

        class FakeClient:
            def request(self, method, path, body=None):
                self.last = (method, path, body)
                return {
                    "rendered": {
                        "sidecarYaml": "name: luma-eye\ncompose: docker-compose.yml\nregion: global\n",
                        "composeYaml": "services:\n  eye:\n    image: luma-eye:local\n",
                    }
                }

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "eye-src"
            src.mkdir()
            parsed_args = cli_parser.build_parser().parse_args(["addon", "install", "eye", "--yes"])
            with patch("luma.cli.builds.cmd_build", side_effect=fake_cmd_build):
                result = cli_addons._deploy_from_source(
                    parsed_args,
                    FakeClient(),
                    {"id": "eye", "stack": "luma-eye", "config": {"region": "global"}},
                    src,
                    quiet=True,
                )
            self.assertEqual(result["stack"], "luma-eye")
            self.assertTrue(hasattr(captured["ns"], "config"))
            self.assertEqual(captured["ns"].workflow_app, "luma-eye")
            self.assertFalse((src / ".luma.addon.compose.yml").exists())
            self.assertFalse((src / ".luma.addon.docker-compose.yml").exists())
