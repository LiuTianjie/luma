"""Control-to-Builder browser SSO across both real ASGI applications."""
import hashlib
import io
import os
from pathlib import Path
import secrets
import sys
import urllib.error
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from luma.control import dashboard_auth, database
from tests.test_dashboard_auth import DashboardAuthBase, ORIGIN, b64url

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "builder"))
from luma_build.browser_auth import LOGIN_COOKIE, SESSION_COOKIE  # noqa: E402
from luma_build.deployer import ControlAuthVerifier  # noqa: E402
from luma_build.server import create_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BUILDER = "https://build.example.com"


class BuilderBrowserAuthTests(DashboardAuthBase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"LUMA_BUILD_URL": BUILDER}))
        self.parent = self.token_session()
        self.with_session(self.parent)
        self.builder = self.enterContext(TestClient(create_app(
            data_dir=Path(self._tmp.name) / "builder", control_url=ORIGIN,
            admin_token="", default_registry_host="registry.example.com", start_worker=False,
        ), base_url=BUILDER))
        control = self.client
        self.control_requests = []
        requests = self.control_requests

        class Opener:
            def open(self, request, timeout):
                requests.append((request.full_url, dict(request.header_items()), request.data))
                response = control.post(urlsplit(request.full_url).path, content=request.data, headers={"Cookie": "", **dict(request.header_items())})
                if response.status_code != 200:
                    raise urllib.error.HTTPError(request.full_url, response.status_code, "Control rejected request", {}, None)
                body = io.BytesIO(response.content)
                body.status = response.status_code
                return body

        self.enterContext(patch.object(ControlAuthVerifier, "_opener", return_value=Opener()))

    def ticket(self, verifier=None, **kwargs):
        verifier = verifier or secrets.token_urlsafe(32)
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        response = self.client.post("/v1/auth/builder/authorize", json={"challenge": challenge, **kwargs}, headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)
        redirect = response.json()["redirectUrl"]
        self.assertEqual(urlsplit(redirect).netloc, "build.example.com")
        return parse_qs(urlsplit(redirect).query)["code"][0], verifier

    def sign_in_builder(self):
        start = self.builder.get("/auth/login", follow_redirects=False)
        self.assertEqual(start.status_code, 303)
        self.assertIn("Secure", start.headers["set-cookie"])
        self.assertIn("HttpOnly", start.headers["set-cookie"])
        self.assertIn("SameSite=lax", start.headers["set-cookie"])
        challenge = parse_qs(urlsplit(start.headers["location"]).query)["challenge"][0]
        self.assertEqual(urlsplit(start.headers["location"]).path, "/dashboard/addon-login")
        self.assertEqual(parse_qs(urlsplit(start.headers["location"]).query).get("addon"), ["builder"])
        authorization = self.client.post("/v1/auth/addon/builder/authorize", json={"challenge": challenge}, headers={"Origin": ORIGIN})
        self.assertEqual(authorization.status_code, 200, authorization.text)
        callback = self.builder.get(authorization.json()["redirectUrl"], follow_redirects=False)
        self.assertEqual(callback.headers["location"], "/")
        cookie = callback.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn(self.token, callback.text + cookie)
        self.assertNotIn(self.parent, callback.text + cookie)
        self.assertEqual(callback.headers["referrer-policy"], "no-referrer")
        self.assertNotIn(LOGIN_COOKIE, self.builder.cookies)
        return self.builder.cookies[SESSION_COOKIE]

    def test_browser_handoff_uses_parent_session_and_never_forwards_master_credentials(self):
        self.sign_in_builder()
        self.assertEqual(self.builder.get("/v1/auth/session").status_code, 200)
        self.assertEqual(self.builder.get("/v1/status").status_code, 200)
        raw_requests = repr(self.control_requests)
        self.assertNotIn(self.token, raw_requests)
        self.assertNotIn(self.parent, raw_requests)
        # Control sign-out revokes the Builder session on its very next read.
        self.client.post("/v1/auth/logout", headers={"Origin": ORIGIN})
        self.assertEqual(self.builder.get("/v1/status").status_code, 401)

    def test_one_use_proof_and_audience_and_opaque_session_scope(self):
        code, verifier = self.ticket(redirectUrl="https://evil.example/callback")
        bad = self.client.post("/v1/auth/builder/exchange", json={"code": code, "verifier": secrets.token_urlsafe(32)})
        self.assertEqual(bad.status_code, 401)
        with patch.dict(os.environ, {"LUMA_BUILD_URL": "https://other.example.com"}):
            self.assertEqual(self.client.post("/v1/auth/builder/exchange", json={"code": code, "verifier": verifier}).status_code, 401)
        result = self.client.post("/v1/auth/builder/exchange", json={"code": code, "verifier": verifier})
        self.assertEqual(result.status_code, 200, result.text)
        token = result.json()["token"]
        self.assertEqual(self.client.post("/v1/auth/builder/exchange", json={"code": code, "verifier": verifier}).status_code, 401)
        self.assertEqual(self.client.get("/v1/secrets", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        with database.transaction(immediate=False) as conn:
            stored = conn.execute("SELECT id_hash,session_hash FROM dashboard_addon_sessions WHERE addon_id='builder'").fetchone()
        self.assertEqual(tuple(stored), (dashboard_auth._hash(token), dashboard_auth._hash(self.parent)))

    def test_tickets_expire_and_revoked_or_idle_parent_cannot_be_reused(self):
        code, verifier = self.ticket()
        with patch.object(dashboard_auth, "_clock", return_value=dashboard_auth._clock() + 61):
            self.assertEqual(self.client.post("/v1/auth/builder/exchange", json={"code": code, "verifier": verifier}).status_code, 401)
        self.sign_in_builder()
        with patch.object(dashboard_auth, "_clock", return_value=dashboard_auth._clock() + dashboard_auth.SESSION_IDLE_SECONDS + 1):
            self.assertEqual(self.builder.get("/v1/status").status_code, 401)
        self.client.post("/v1/auth/sessions/revoke", headers={"Origin": ORIGIN}, json={"all": True})
        self.assertEqual(self.builder.get("/v1/status").status_code, 401)

    def test_csrf_and_explicit_wrong_bearer_cannot_use_the_cookie(self):
        self.sign_in_builder()
        body = {"repoUrl": "https://github.com/acme/app"}
        for origin in (None, "https://evil.example.com"):
            headers = {"Origin": origin} if origin else {}
            self.assertEqual(self.builder.post("/v1/builds", headers=headers, json=body).status_code, 403)
            self.assertEqual(self.builder.post("/v1/auth/logout", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/v1/auth/builder/authorize", headers=headers, json={"challenge": "a" * 43}).status_code, 403)
        self.assertEqual(self.builder.get("/v1/status", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        self.assertEqual(self.builder.post("/v1/builds", headers={"Origin": BUILDER}, json=body).status_code, 200)
        self.assertEqual(self.builder.post("/v1/auth/logout", headers={"Origin": BUILDER}).status_code, 200)
        self.assertEqual(self.builder.get("/v1/status").status_code, 401)
        self.assertEqual(self.client.get("/v1/auth/session").status_code, 200)

    def test_no_session_or_missing_callback_proof_cannot_sign_in(self):
        code, _ = self.ticket()
        response = self.builder.get(f"/auth/callback?code={code}", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/?sso=failed")
        self.assertNotIn(SESSION_COOKIE, self.builder.cookies)
        self.client.cookies.clear()
        response = self.client.post("/v1/auth/builder/authorize", headers=self.bearer(), json={"challenge": "a" * 43})
        self.assertEqual(response.status_code, 401)

    def test_refresh_with_valid_session_renders_shell_directly_and_revocation_reverts(self):
        anon = self.builder.get("/")
        self.assertEqual(anon.status_code, 200)
        self.assertIn('const INITIAL_SESSION_STATE = "check";', anon.text)
        self.assertIn('<main id="auth-view" class="auth-screen" hidden>', anon.text)
        self.assertIn('<div id="app-shell" class="shell" hidden>', anon.text)

        self.sign_in_builder()
        refreshed = self.builder.get("/")
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn('const INITIAL_SESSION_STATE = "authenticated";', refreshed.text)
        self.assertIn('<div id="app-shell" class="shell">', refreshed.text)
        self.assertIn('<main id="auth-view" class="auth-screen" hidden>', refreshed.text)

        # After Control logout, refreshing Builder immediately sees an expired session.
        self.client.post("/v1/auth/logout", headers={"Origin": ORIGIN})
        expired = self.builder.get("/")
        self.assertEqual(expired.status_code, 200)
        self.assertIn('const INITIAL_SESSION_STATE = "expired";', expired.text)
        self.assertIn('<div id="app-shell" class="shell" hidden>', expired.text)

    def test_login_start_forwards_validated_theme_param(self):
        light = self.builder.get("/auth/login?theme=light", follow_redirects=False)
        self.assertEqual(light.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(light.headers["location"]).query).get("theme"), ["light"])

        dark = self.builder.get("/auth/login?theme=dark", follow_redirects=False)
        self.assertEqual(dark.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(dark.headers["location"]).query).get("theme"), ["dark"])

        invalid = self.builder.get("/auth/login?theme=sepia", follow_redirects=False)
        self.assertEqual(invalid.status_code, 303)
        self.assertNotIn("theme", parse_qs(urlsplit(invalid.headers["location"]).query))
