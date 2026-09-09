import json
import unittest
from unittest.mock import patch

from luma.control import observe
from luma.errors import LumaError


class ObserveQueryTests(unittest.TestCase):
    def test_observe_url_uses_manager_tailscale(self):
        url = observe.observe_base_url({
            "nodes": {
                "manager": {
                    "status": "manager",
                    "tailscaleIP": "100.106.154.3",
                    "labels": {"role.nomad-manager": "true"},
                }
            }
        })
        self.assertEqual(url, "http://100.106.154.3:8428")

    def test_observe_url_env_wins(self):
        with patch.dict("os.environ", {"LUMA_OBSERVE_URL": "http://example.internal:8428/"}):
            self.assertEqual(observe.observe_base_url({"nodes": {}}), "http://example.internal:8428")

    def test_handle_observe_apps_shapes_http_and_jobs(self):
        def fake_range(base, expr, *, window, now):
            if "traefik_router_requests_total[5m]" in expr and "5.." not in expr:
                return [{"labels": {"router": "word2pdf-web@nomad"}, "points": [[int(now) - 60, 2.0], [int(now), 4.0]]}]
            if "5.." in expr:
                return [{"labels": {"router": "word2pdf-web@nomad"}, "points": [[int(now) - 60, 0.1], [int(now), 0.2]]}]
            if expr == "luma_observe_job_failed":
                return [{"labels": {"job": "granary", "task_group": "app"}, "points": [[int(now), 21]]}]
            return [{"labels": {"job": "granary", "task_group": "app"}, "points": [[int(now), 0]]}]

        with patch.object(observe, "require_token"), patch.object(observe, "load_state", return_value={"nodes": {}}), patch.object(observe, "_query_range", side_effect=fake_range):
            result = observe.handle_observe_apps("token", window=3600)
        self.assertTrue(result["available"])
        self.assertEqual(result["http"][0]["id"], "word2pdf-web")
        self.assertEqual(result["http"][0]["requestRate"], 4.0)
        self.assertEqual(result["jobs"][0]["job"], "granary")
        self.assertEqual(result["jobs"][0]["failed"], 21)

    def test_unavailable_observe_is_explicit(self):
        with patch.object(observe, "require_token"), patch.object(observe, "load_state", return_value={"nodes": {}}), patch.object(observe, "_query_range", side_effect=LumaError("luma-observe metrics are unavailable")):
            result = observe.handle_observe_apps("token")
        self.assertFalse(result["available"])
        self.assertEqual(result["http"], [])
