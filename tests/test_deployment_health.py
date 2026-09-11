import unittest
from types import SimpleNamespace

from luma.control.server import _deployment_health_result


class DeploymentHealthTests(unittest.TestCase):
    def test_public_route_health_distinguishes_ready_and_inconclusive(self):
        service = SimpleNamespace(name="web", exposure="cn-edge", domain="web.example.com", healthcheck={})

        ready = _deployment_health_result(service, "https://web.example.com/ -> HTTP 200")
        inconclusive = _deployment_health_result(service, "Public route probe inconclusive: https://web.example.com/")

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["kind"], "public-route")
        self.assertEqual(inconclusive["status"], "inconclusive")

    def test_internal_service_reports_nomad_check_without_claiming_runtime_ready(self):
        service = SimpleNamespace(
            name="worker",
            exposure="none",
            domain=None,
            healthcheck={"http": "/healthz"},
        )

        result = _deployment_health_result(service)

        self.assertEqual(result["status"], "configured")
        self.assertEqual(result["kind"], "nomad-check")
        self.assertIn("Nomad", result["message"])

    def test_non_http_service_without_check_is_explicitly_unverified(self):
        service = SimpleNamespace(name="worker", exposure="none", domain=None, healthcheck={})
        self.assertEqual(_deployment_health_result(service)["status"], "not-configured")


if __name__ == "__main__":
    unittest.main()
