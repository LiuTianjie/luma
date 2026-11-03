"""Guest-password enforcement, revocation and separation from operator access."""
from collections import deque
import time
from unittest.mock import patch

import yaml

from luma.control import observe_sharing
from luma.observe_grafana import grafana_public_route_yaml
from tests.test_dashboard_auth import COOKIE as OPERATOR_COOKIE, ORIGIN
from tests.test_observe_settings import ObserveSettingsBase, PASSWORD, SETTINGS

COOKIE = observe_sharing.SESSION_COOKIE
FORWARD = "/v1/auth/grafana-public/forward"
SIGN_IN = "/v1/auth/grafana-public/session"
SHARE_ID = "a" * 32
PAGE = f"/grafana/public-dashboards/{SHARE_ID}"
API = f"/grafana/api/public/dashboards/{SHARE_ID}"
QUERY = f"{API}/panels/1/query"


class ObserveSharingTests(ObserveSettingsBase):
    def setUp(self):
        super().setUp()
        self._attempts_patch = patch.object(observe_sharing, "_attempts", deque())
        self._attempts_patch.start()
        self.addCleanup(self._attempts_patch.stop)

    def sign_in(self, password=PASSWORD, *, origin=ORIGIN):
        headers = {"Origin": origin} if origin else {}
        return self.client.post(SIGN_IN, headers=headers, json={"password": password})

    def guest(self, password=PASSWORD):
        response = self.sign_in(password)
        self.assertEqual(response.status_code, 200, response.text)
        session = response.cookies[COOKIE]
        self.client.cookies.clear()
        return session

    def forward(self, path=PAGE, method="GET", session="", *, origin=ORIGIN, extra=None):
        headers = {"X-Forwarded-Uri": path, "X-Forwarded-Method": method, "Cookie": f"{COOKIE}={session}"}
        if origin:
            headers["Origin"] = origin
        headers.update(extra or {})
        return self.client.get(FORWARD, headers=headers, follow_redirects=False)

    def test_anonymous_page_redirects_to_password_and_public_data_is_denied(self):
        self.assertEqual(self.sign_in().status_code, 404)
        self.assertEqual(self.forward().status_code, 404)
        self.assertEqual(self.save(True).status_code, 200)
        response = self.forward(PAGE + "?from=now-6h&to=now")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], f"/dashboard/share/{SHARE_ID}?from=now-6h&to=now")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path, method in ((API, "GET"), (API + "/annotations", "GET"), (QUERY, "POST"), ("/grafana/public/build/app.js", "GET")):
            with self.subTest(path=path):
                response = self.forward(path, method)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_password_session_allows_shared_content_but_no_management_access(self):
        self.assertEqual(self.save(True).status_code, 200)
        self.assertEqual(self.sign_in("wrong-password").status_code, 401)
        self.assertNotIn(COOKIE, self.client.cookies)
        response = self.sign_in()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"authenticated": True})
        cookie = response.headers["Set-Cookie"].lower()
        for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("domain=", cookie)
        self.assertNotIn(OPERATOR_COOKIE, response.cookies)
        session = response.cookies[COOKIE]
        for path, method in ((PAGE, "GET"), (PAGE, "HEAD"), (API, "GET"), (API + "/annotations", "GET"),
                             (QUERY, "POST"), ("/grafana/public/build/app.js", "GET")):
            self.assertEqual(self.forward(path, method, session).status_code, 200, path)
        for path in ("/v1/secrets", SETTINGS, "/v1/auth/session", "/v1/auth/forward"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.post(SETTINGS, headers={"Origin": ORIGIN},
                                         json={"grafanaPublicSharingEnabled": False}).status_code, 401)
        self.assertEqual(self.client.get("/v1/secrets", headers={"Authorization": f"Bearer {session}"}).status_code, 401)

    def test_guest_cannot_expand_forwarded_paths_or_cross_origin_queries(self):
        self.assertEqual(self.save(True).status_code, 200)
        session = self.guest()
        for path, method in (("/grafana/api/search", "GET"), ("/grafana/api/ds/query", "POST"),
                             ("/grafana/d/private", "GET"), (API, "DELETE"), (API, "POST"),
                             ("/grafana/public/build/../../api/search", "GET"),
                             ("/grafana/public/build/%2e%2e/api/search", "GET"),
                             ("//evil.example/grafana/public-dashboards/" + SHARE_ID, "GET"), ("", "GET")):
            with self.subTest(path=path, method=method):
                self.assertEqual(self.forward(path, method, session).status_code, 403)
        for origin in (None, "https://other.example.com", "http://luma.example.com"):
            self.assertEqual(self.forward(QUERY, "POST", session, origin=origin).status_code, 403)
        self.assertEqual(self.forward(QUERY, "POST", session).status_code, 200)

    def test_expired_forged_rotated_and_disabled_sessions_are_rejected(self):
        self.assertEqual(self.save(True).status_code, 200)
        session = self.guest()
        forged = session[:-1] + ("0" if session[-1] != "0" else "1")
        for invalid in ("malformed", forged):
            self.assertEqual(self.forward(API, session=invalid).status_code, 401)
        expires = int(session.split(".", 1)[0])
        with patch.object(observe_sharing.time, "time", return_value=expires + 1):
            self.assertEqual(self.forward(API, session=session).status_code, 401)
        new_password = "replacement-sharing-password"
        self.assertEqual(self.save(True, new_password).status_code, 200)
        self.assertEqual(self.forward(API, session=session).status_code, 401)
        self.assertEqual(self.sign_in(PASSWORD).status_code, 401)
        replacement = self.guest(new_password)
        self.assertEqual(self.forward(API, session=replacement).status_code, 200)
        self.assertEqual(self.save(False).status_code, 200)
        self.assertEqual(self.forward(API, session=replacement).status_code, 404)
        self.assertEqual(self.sign_in(new_password).status_code, 404)
        # Even reusing the same password must not resurrect a revoked session.
        self.assertEqual(self.save(True, new_password).status_code, 200)
        self.assertEqual(self.forward(API, session=replacement).status_code, 401)
        self.assertEqual(self.forward(API, session=self.guest(new_password)).status_code, 200)

    def test_sign_in_enforces_origin_body_limits_and_guessing_limit(self):
        self.assertEqual(self.save(True).status_code, 200)
        for origin in (None, "https://other.example.com"):
            self.assertEqual(self.sign_in(origin=origin).status_code, 403)
        headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
        self.assertEqual(self.client.post(SIGN_IN, headers=headers, content="{" + " " * 4096).status_code, 413)
        self.assertEqual(self.client.post(SIGN_IN, headers=headers, content="{").status_code, 400)
        self.assertEqual(self.client.post(SIGN_IN, headers={"Origin": ORIGIN}, data={"password": PASSWORD}).status_code, 415)
        for _ in range(5):
            self.assertEqual(self.sign_in("wrong-password").status_code, 401)
        response = self.sign_in()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertNotIn(COOKIE, response.cookies)
        clock = time.monotonic
        with patch.object(observe_sharing.time, "monotonic", side_effect=lambda: clock() + 61):
            self.assertEqual(self.sign_in().status_code, 200)

    def test_operator_authentication_still_works_without_guest_password(self):
        self.assertEqual(self.save(True).status_code, 200)
        self.assertEqual(self.forward(extra=self.bearer()).status_code, 200)
        operator = self.token_session()
        self.assertEqual(self.forward(QUERY, "POST", extra={"Cookie": f"{OPERATOR_COOKIE}={operator}"}).status_code, 200)
        self.assertEqual(self.forward(QUERY, "POST", origin="https://other.example.com",
                                      extra={"Cookie": f"{OPERATOR_COOKIE}={operator}"}).status_code, 403)

    def test_every_public_router_verifies_auth_before_stripping_credentials(self):
        http = yaml.safe_load(grafana_public_route_yaml("luma.example.com"))["http"]
        auth_name = "luma-observe-grafana-public-auth"
        identity_name = "luma-observe-grafana-public-identity"
        for router in http["routers"].values():
            self.assertLess(router["middlewares"].index(auth_name), router["middlewares"].index(identity_name))
        auth = http["middlewares"][auth_name]["forwardAuth"]
        self.assertEqual(auth["address"], "http://127.0.0.1:8080" + FORWARD)
        self.assertIs(auth["trustForwardHeader"], False)
        self.assertIs(auth["preserveLocationHeader"], True)
        self.assertEqual(set(auth["authRequestHeaders"]), {"Cookie", "Origin", "Authorization"})
        stripped = http["middlewares"][identity_name]["headers"]["customRequestHeaders"]
        self.assertEqual(stripped["Cookie"], "")
        self.assertEqual(stripped["Authorization"], "")
