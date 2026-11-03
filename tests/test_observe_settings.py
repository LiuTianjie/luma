"""Operator authorization, persistent sharing policy and route-write failures."""
from pathlib import Path
from unittest.mock import patch

from luma.control import observe_settings
from luma.observe_grafana import GRAFANA_PUBLIC_ROUTE_NAME
from tests.test_dashboard_auth import DashboardAuthBase, ORIGIN

SETTINGS = "/v1/dashboard/observe/settings"


class ObserveSettingsTests(DashboardAuthBase):
    def setUp(self):
        super().setUp()
        self.routes = Path(self._tmp.name) / "routes"
        self.routes.mkdir()
        self.route = self.routes / GRAFANA_PUBLIC_ROUTE_NAME
        self._routes_patch = patch.object(observe_settings, "ROUTES_DIR", self.routes)
        self._routes_patch.start()

    def tearDown(self):
        self._routes_patch.stop()
        super().tearDown()

    def save(self, enabled):
        return self.client.post(SETTINGS, headers=self.bearer(), json={"grafanaPublicSharingEnabled": enabled})

    def setting(self):
        response = self.client.get(SETTINGS, headers=self.bearer())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["grafanaPublicSharingEnabled"]

    def test_default_is_private_and_both_methods_require_management_authority(self):
        self.assertFalse(self.setting())
        self.assertFalse(self.route.exists())
        for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": f"Bearer {self.state['joinToken']}"}):
            self.assertEqual(self.client.get(SETTINGS, headers=headers).status_code, 401)
            response = self.client.post(SETTINGS, headers=headers, json={"grafanaPublicSharingEnabled": True})
            self.assertEqual(response.status_code, 401)
        self.assertFalse(self.setting())
        self.assertFalse(self.route.exists())

    def test_enable_disable_and_restart_reconcile_preserve_protected_route(self):
        protected = self.routes / "luma-observe-grafana.yml"
        protected.write_text("existing authenticated route")
        self.assertEqual(self.save(True).status_code, 200)
        self.assertTrue(self.setting())
        original = self.route.read_text()
        self.assertIn("luma.example.com", original)
        self.route.unlink()
        observe_settings.reconcile_settings()
        self.assertEqual(self.route.read_text(), original)
        self.assertEqual(self.save(False).status_code, 200)
        self.assertFalse(self.setting())
        self.assertFalse(self.route.exists())
        # A stale public file must not reopen sharing after a disabled restart.
        self.route.write_text(original)
        observe_settings.reconcile_settings()
        self.assertFalse(self.route.exists())
        self.assertEqual(protected.read_text(), "existing authenticated route")

    def test_settings_reject_non_boolean_and_unrelated_fields(self):
        for value in ("true", "false", 1, 0, None, {}, []):
            with self.subTest(value=value):
                self.assertEqual(self.save(value).status_code, 400)
        for body in ({}, {"grafanaPublicSharingEnabled": True, "domain": "other.example.com"}):
            self.assertEqual(self.client.post(SETTINGS, headers=self.bearer(), json=body).status_code, 400)
        self.assertFalse(self.route.exists())
        self.assertFalse(self.setting())

    def test_failed_route_write_does_not_save_or_damage_previous_route(self):
        self.assertEqual(self.save(True).status_code, 200)
        original = self.route.read_bytes()
        with patch("luma.observe_grafana.os.replace", side_effect=OSError("read-only routes")):
            self.assertEqual(self.save(True).status_code, 500)
        self.assertTrue(self.setting())
        self.assertEqual(self.route.read_bytes(), original)
        self.assertEqual(list(self.routes.glob("*.tmp")), [])
        with patch.object(observe_settings, "write_grafana_public_route", side_effect=OSError("read-only routes")):
            self.assertEqual(self.save(False).status_code, 500)
        self.assertTrue(self.setting())
        self.assertEqual(self.route.read_bytes(), original)

    def test_failed_database_commit_restores_route_and_previous_setting(self):
        with patch("luma.control.database.write_state", side_effect=RuntimeError("failed commit")):
            self.assertEqual(self.save(True).status_code, 500)
        self.assertFalse(self.route.exists())
        self.assertFalse(self.setting())
        self.assertEqual(self.save(True).status_code, 200)
        original = self.route.read_bytes()
        with patch("luma.control.database.write_state", side_effect=RuntimeError("failed commit")):
            self.assertEqual(self.save(False).status_code, 500)
        self.assertEqual(self.route.read_bytes(), original)
        self.assertTrue(self.setting())

    def test_cookie_write_requires_same_origin(self):
        session = self.token_session()
        response = self.client.post(SETTINGS, headers=self.with_session(session, "https://other.example.com"),
                                    json={"grafanaPublicSharingEnabled": True})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.setting())
        response = self.client.post(SETTINGS, headers=self.with_session(session, ORIGIN),
                                    json={"grafanaPublicSharingEnabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.setting())
