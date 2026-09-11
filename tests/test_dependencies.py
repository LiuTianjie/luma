import unittest
from types import SimpleNamespace

from luma.dependencies import initialize_service_requirements, service_requirements, validate_requirement_block


class ServiceRequirementsTests(unittest.TestCase):
    def test_private_proxy_home_service_reports_missing_prerequisites(self):
        service = SimpleNamespace(
            environment={"PASSWORD": "${APP_PASSWORD}"},
            region="home",
            exposure="tailscale-relay",
            proxy=True,
            image="private.example.com/team/app:latest",
        )
        state = {"secrets": {}, "registries": {}, "nodes": {"home-1": {"region": "home", "state": "ready"}}}
        config = SimpleNamespace(dns={"provider": "cloudflare", "apiTokenEnv": "CLOUDFLARE_API_TOKEN"})

        result = service_requirements(service, state, config)
        self.assertFalse(result["ready"])
        checks = {item["kind"]: item for item in result["checks"]}
        self.assertEqual(checks["secrets"]["missing"], ["APP_PASSWORD"])
        self.assertEqual(checks["tailscale"]["status"], "missing")
        self.assertEqual(checks["egress"]["status"], "missing")
        self.assertEqual(checks["registry"]["status"], "missing")

    def test_public_service_with_ready_region_and_cloudflare_is_ready(self):
        service = SimpleNamespace(
            environment={}, region="cn", exposure="cn-edge", proxy=False,
            image="nginx:alpine",
        )
        state = {"secrets": {"CLOUDFLARE_API_TOKEN": "configured"}, "registries": {}, "nodes": {"cn-1": {"region": "cn", "state": "ready"}}}
        config = SimpleNamespace(dns={"provider": "cloudflare", "apiTokenEnv": "CLOUDFLARE_API_TOKEN"})

        result = service_requirements(service, state, config)
        self.assertTrue(result["ready"])
        self.assertTrue(all(item["status"] in {"ready", "skipped"} for item in result["checks"]))

    def test_dashboard_cloudflare_deploy_requires_a_verified_setup_check(self):
        service = SimpleNamespace(
            environment={},
            requirements={"capabilities": ["cloudflare"]},
            region="cn",
            exposure="cn-edge",
            proxy=False,
            image="traefik/whoami:latest",
        )
        state = {
            "nodes": {"manager": {"region": "cn", "state": "ready"}},
            "secrets": {"CLOUDFLARE_API_TOKEN": "cf-token"},
        }
        config = SimpleNamespace(dns={"provider": "cloudflare", "apiTokenEnv": "CLOUDFLARE_API_TOKEN"})
        pending = service_requirements(service, state, config, require_verified=True)
        self.assertFalse(pending["ready"])
        cloudflare = next(item for item in pending["checks"] if item["kind"] == "cloudflare")
        self.assertIn("run Dashboard setup check for Cloudflare", cloudflare["missing"])
        state["setupChecks"] = {"checks": {"cloudflare": {"status": "ready"}}}
        verified = service_requirements(service, state, config, require_verified=True)
        self.assertTrue(verified["ready"])

    def test_init_plan_rejects_unknown_actions(self):
        with self.assertRaisesRegex(Exception, "unsupported requirements.init"):
            initialize_service_requirements({"checks": [], "init": ["run-shell"]}, component="demo")

    def test_init_plan_rejects_a_failed_explicit_capability(self):
        with self.assertRaisesRegex(Exception, "initialization tailscale-node"):
            initialize_service_requirements({"checks": [{"kind": "tailscale", "status": "missing", "detail": "node"}], "init": ["tailscale-node"]}, component="demo")

    def test_requirement_contract_rejects_unknown_capability_and_init_action(self):
        with self.assertRaisesRegex(Exception, "unsupported requirements.capabilities"):
            validate_requirement_block({"capabilities": ["cloudlfare"]})
        with self.assertRaisesRegex(Exception, "unsupported requirements.init"):
            validate_requirement_block({"init": ["run-shell"]})


if __name__ == "__main__":
    unittest.main()
