"""Executable acceptance matrix for the supported first-install paths.

These tests intentionally exercise the same prerequisite contract used by
Dashboard preview and live deployment. They do not pretend to replace a
clean-host smoke test; the release script runs this matrix together with
manifest rendering and dashboard checks.
"""

import unittest
from types import SimpleNamespace

from luma.dependencies import service_requirements


CONFIG = SimpleNamespace(dns={"provider": "cloudflare", "apiTokenEnv": "CLOUDFLARE_API_TOKEN"})


def service(**overrides):
    values = {
        "environment": {},
        "requirements": {},
        "region": "cn",
        "exposure": "none",
        "proxy": False,
        "image": "traefik/whoami:latest",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FirstInstallScenarioTests(unittest.TestCase):
    def test_single_manager_internal_hello_world_is_dependency_free(self):
        result = service_requirements(
            service(),
            {"nodes": {"manager": {"region": "cn", "state": "ready"}}, "secrets": {}, "registries": {}},
            CONFIG,
        )
        self.assertTrue(result["ready"])
        self.assertTrue(all(check["status"] in {"ready", "skipped"} for check in result["checks"]))

    def test_public_first_install_requires_verified_cloudflare(self):
        result = service_requirements(
            service(exposure="cn-edge", domain="hello.example.com", port=80),
            {"nodes": {"manager": {"region": "cn", "state": "ready"}}, "secrets": {}, "registries": {}},
            CONFIG,
        )
        checks = {check["kind"]: check for check in result["checks"]}
        self.assertFalse(result["ready"])
        self.assertEqual(checks["cloudflare"]["status"], "missing")

    def test_home_path_requires_tailscale_reachability(self):
        result = service_requirements(
            service(region="home", exposure="tailscale-relay", domain="home.example.com", port=80),
            {"nodes": {"home": {"region": "home", "state": "ready"}}, "secrets": {}, "registries": {}},
            CONFIG,
        )
        checks = {check["kind"]: check for check in result["checks"]}
        self.assertFalse(result["ready"])
        self.assertEqual(checks["tailscale"]["status"], "missing")

    def test_proxy_path_reports_egress_as_the_blocker(self):
        result = service_requirements(
            service(proxy=True),
            {"nodes": {"manager": {"region": "cn", "state": "ready"}}, "secrets": {}, "registries": {}},
            CONFIG,
        )
        checks = {check["kind"]: check for check in result["checks"]}
        self.assertFalse(result["ready"])
        self.assertEqual(checks["egress"]["status"], "missing")

    def test_private_registry_path_requires_credential_only_for_private_host(self):
        result = service_requirements(
            service(image="registry.internal/team/hello:1"),
            {"nodes": {"manager": {"region": "cn", "state": "ready"}}, "secrets": {}, "registries": {}},
            CONFIG,
        )
        checks = {check["kind"]: check for check in result["checks"]}
        self.assertEqual(checks["registry"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
