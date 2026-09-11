import unittest
from types import SimpleNamespace
from unittest.mock import patch

from luma.config import LumaConfig
from luma.control import server


class DashboardSetupCheckTests(unittest.TestCase):
    def test_legacy_state_gets_a_real_join_token(self):
        state = {}
        persisted = {}

        def mutate(mutator):
            mutator(persisted)

        with patch.object(server, "mutate_state", side_effect=mutate):
            token = server._ensure_dashboard_join_token(state)

        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(persisted["joinToken"], token)
        self.assertEqual(state["joinToken"], token)

    def test_checks_are_secret_free_and_persist_latest_result(self):
        state = {
            "deployToken": "deploy-token",
            "secrets": {
                "CLOUDFLARE_API_TOKEN": "cf-token",
                "CLOUDFLARE_ZONE_ID": "zone-id",
                "TRAEFIK_ACME_EMAIL": "ops@example.com",
            },
        }
        config = SimpleNamespace(dns={
            "provider": "cloudflare",
            "apiTokenEnv": "CLOUDFLARE_API_TOKEN",
            "zoneIdEnv": "CLOUDFLARE_ZONE_ID",
            "edgeTarget": "203.0.113.10",
        })

        def mutate(mutator):
            mutator(state)

        class FakeCloudflare:
            def __init__(self, token):
                self.token = token

            def request(self, method, path):
                self.last = (method, path)
                return {"success": True}

        with (
            patch.object(server, "load_state", return_value=state),
            patch.object(server, "load_config", return_value=config),
            patch.object(server, "_apply_state_secrets"),
            patch.object(server, "CloudflareClient", FakeCloudflare),
            patch.object(server, "_managed_registry_spec", side_effect=server.LumaError("registry not installed")),
            patch.object(server, "_mutate_control_state", side_effect=mutate),
            patch.object(server.shutil, "which", return_value=None),
        ):
            result = server.handle_dashboard_setup_check("deploy-token")

        self.assertEqual(result["checks"]["cloudflare"]["status"], "ready")
        self.assertEqual(result["checks"]["acme"]["status"], "ready")
        self.assertEqual(result["checks"]["tailscale"]["status"], "skipped")
        self.assertNotIn("cf-token", str(result))
        self.assertEqual(state["setupChecks"], result)

    def test_configure_persists_dns_provider_without_persisting_secret_values(self):
        state = {"deployToken": "deploy-token"}
        config = LumaConfig({"providers": {"dns": {"type": "cloudflare"}}}, None)
        with (
            patch.object(server, "load_state", return_value=state),
            patch.object(server, "load_config", return_value=config),
            patch.object(server, "save_config") as save_config,
        ):
            result = server.handle_dashboard_setup_configure(
                "deploy-token",
                {
                    "cloudflareZone": "example.com.",
                    "cloudflareZoneId": "zone-id",
                    "edgeTarget": "203.0.113.10",
                },
            )

        saved = save_config.call_args.args[0]
        dns = saved.raw["providers"]["dns"]
        self.assertEqual(dns["provider"], "cloudflare")
        self.assertEqual(dns["zone"], "example.com")
        self.assertEqual(dns["zoneId"], "zone-id")
        self.assertEqual(dns["edgeTarget"], "203.0.113.10")
        self.assertNotIn("token", str(saved.raw))
        self.assertTrue(result["saved"])

    def test_configure_rejects_unknown_fields(self):
        state = {"deployToken": "deploy-token"}
        with patch.object(server, "load_state", return_value=state):
            with self.assertRaisesRegex(server.LumaError, "unsupported setup configuration"):
                server.handle_dashboard_setup_configure("deploy-token", {"secret": "nope"})

    def test_changing_provider_secret_marks_previous_check_stale(self):
        state = {
            "deployToken": "deploy-token",
            "secrets": {},
            "setupChecks": {"checks": {"cloudflare": {"status": "ready"}}},
        }

        def mutate(mutator):
            mutator(state)

        with patch.object(server, "mutate_state", side_effect=mutate):
            server.handle_secret_set("deploy-token", {"name": "CLOUDFLARE_API_TOKEN", "value": "new-token"})

        self.assertEqual(state["setupChecks"]["checks"]["cloudflare"]["status"], "stale")


if __name__ == "__main__":
    unittest.main()
