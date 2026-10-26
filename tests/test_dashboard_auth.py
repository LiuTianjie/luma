"""Dashboard passkey sign-in and browser sessions, through the Control ASGI app.

A small software authenticator (EC P-256, ``none`` attestation) produces real
WebAuthn responses so the pinned ``webauthn`` library verifies them end to end.
"""
import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import _restore_env, _set_env

from luma.control import dashboard_auth
from luma.control.state import init_state

try:
    import cbor2
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    HAVE_WEBAUTHN = dashboard_auth.passkeys_available()
except ImportError:  # pragma: no cover - the test extra installs these on 3.10+
    HAVE_WEBAUTHN = False

DOMAIN = "luma.example.com"
ORIGIN = f"https://{DOMAIN}"
COOKIE = dashboard_auth.SESSION_COOKIE


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SoftAuthenticator:
    def __init__(self, rp_id: str = DOMAIN, origin: str = ORIGIN):
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict) -> dict:
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + self.sign_count.to_bytes(4, "big")
            + bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id), "rawId": b64url(self.credential_id), "type": "public-key",
            "response": {
                "clientDataJSON": b64url(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": b64url(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def get(self, options: dict, *, sign_count: int | None = None) -> dict:
        self.sign_count = self.sign_count + 1 if sign_count is None else sign_count
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + self.sign_count.to_bytes(4, "big")
        client_data = self._client_data("webauthn.get", options["challenge"])
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64url(self.credential_id), "rawId": b64url(self.credential_id), "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


class DashboardAuthBase(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient
        from luma.control.server import create_app

        self._tmp = tempfile.TemporaryDirectory()
        self._old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(self._tmp.name) / "state"))
        self.state = init_state(domain=DOMAIN, cluster_id="luma-test", overwrite=True)
        self.token = self.state["deployToken"]
        self.client = TestClient(create_app(), base_url=ORIGIN)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        _restore_env("LUMA_CONTROL_STATE_DIR", self._old_state)
        self._tmp.cleanup()

    def bearer(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token or self.token}", "Origin": ORIGIN}

    def token_session(self) -> str:
        response = self.client.post("/v1/auth/session", headers=self.bearer())
        self.assertEqual(response.status_code, 200, response.text)
        return response.cookies[COOKIE]

    def with_session(self, session_id: str, origin: str | None = ORIGIN) -> dict:
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE, session_id, domain=DOMAIN)
        return {"Origin": origin} if origin else {}

    def register(self, authenticator: SoftAuthenticator, name: str = "Laptop") -> dict:
        options = self.client.post("/v1/auth/passkey/register/options", headers=self.bearer(), json={})
        self.assertEqual(options.status_code, 200, options.text)
        public_key = options.json()["publicKey"]
        self.assertEqual(public_key["rp"]["id"], DOMAIN)
        self.assertEqual(public_key["authenticatorSelection"]["userVerification"], "required")
        verify = self.client.post("/v1/auth/passkey/register/verify", headers=self.bearer(), json={
            "challengeId": options.json()["challengeId"], "credential": authenticator.create(public_key), "name": name,
        })
        self.assertEqual(verify.status_code, 200, verify.text)
        self.assertNotIn(COOKIE, verify.cookies, "an operator who already holds the token gets no extra session")
        return verify.json()["passkey"]

    def sign_in(self, authenticator: SoftAuthenticator, **kwargs):
        self.client.cookies.clear()
        options = self.client.post("/v1/auth/passkey/authenticate/options", json={})
        self.assertEqual(options.status_code, 200, options.text)
        body = {"challengeId": options.json()["challengeId"], "credential": authenticator.get(options.json()["publicKey"], **kwargs)}
        return self.client.post("/v1/auth/passkey/authenticate/verify", headers={"Origin": ORIGIN}, json=body), body


class TokenSessionTests(DashboardAuthBase):
    def test_token_exchange_sets_hardened_cookie_and_authorizes_existing_routes(self):
        response = self.client.post("/v1/auth/session", headers=self.bearer())
        self.assertEqual(response.status_code, 200, response.text)
        cookie = response.headers["set-cookie"]
        for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", f"Max-Age={dashboard_auth.SESSION_ABSOLUTE_SECONDS}"):
            self.assertIn(attribute.lower(), cookie.lower())
        self.assertNotIn("Domain=", cookie)
        session_id = response.cookies[COOKIE]
        self.assertEqual(response.json()["session"]["method"], "token")

        self.with_session(session_id)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 200)
        self.assertEqual(self.client.get("/v1/auth/session").json()["session"]["method"], "token")
        verified = self.client.post("/v1/auth/login/verify", headers={"Origin": ORIGIN}, json={})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["clusterId"], "luma-test")

    def test_only_a_hash_of_the_session_is_stored(self):
        session_id = self.token_session()
        from luma.control import database
        with database.transaction(immediate=False) as conn:
            stored = [row[0] for row in conn.execute("SELECT id_hash FROM dashboard_sessions")]
        self.assertEqual(stored, [hashlib.sha256(session_id.encode()).hexdigest()])

    def test_cookie_writes_require_the_control_origin(self):
        session_id = self.token_session()
        for origin in (None, "https://evil.example", "http://luma.example.com"):
            headers = self.with_session(session_id, origin)
            response = self.client.post("/v1/auth/login/verify", headers=headers, json={})
            self.assertEqual(response.status_code, 403, (origin, response.text))
            self.assertEqual(response.json()["errorInfo"]["code"], "forbidden_origin")
        # Reads stay usable without Origin (SameSite=Strict keeps them same-site).
        self.with_session(session_id, None)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 200)

    def test_bearer_header_wins_over_cookie(self):
        session_id = self.token_session()
        self.with_session(session_id)
        response = self.client.get("/v1/secrets", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)

    def test_bad_token_or_foreign_origin_creates_no_session(self):
        wrong = self.client.post("/v1/auth/session", headers=self.bearer("nope"))
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn(COOKIE, wrong.cookies)
        foreign = self.client.post("/v1/auth/session", headers={"Authorization": f"Bearer {self.token}", "Origin": "https://evil.example"})
        self.assertEqual(foreign.status_code, 403)
        self.assertNotIn(COOKIE, foreign.cookies)

    def test_unknown_or_missing_session_is_unauthorized(self):
        self.with_session("forged")
        self.assertEqual(self.client.get("/v1/secrets").status_code, 401)
        self.assertEqual(self.client.get("/v1/auth/session").status_code, 401)
        self.client.cookies.clear()
        missing = self.client.get("/v1/secrets")
        self.assertEqual(missing.status_code, 401)
        self.assertIn("bearer token", missing.json()["error"])

    def test_logout_revokes_and_clears_cookie(self):
        session_id = self.token_session()
        headers = self.with_session(session_id)
        response = self.client.post("/v1/auth/logout", headers=headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["signedOut"])
        self.assertIn(f'{COOKIE}=""', response.headers["set-cookie"])
        self.with_session(session_id)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 401)

    def test_idle_and_absolute_expiry(self):
        start = 1_800_000_000.0
        with patch.object(dashboard_auth, "_clock", return_value=start):
            session_id = self.token_session()
        self.with_session(session_id)
        # Activity inside the idle window keeps the session alive...
        for offset in (3600, 7000, 10600):
            with patch.object(dashboard_auth, "_clock", return_value=start + offset):
                self.assertEqual(self.client.get("/v1/secrets").status_code, 200, offset)
        # ...but a gap longer than the idle timeout ends it.
        with patch.object(dashboard_auth, "_clock", return_value=start + 10600 + dashboard_auth.SESSION_IDLE_SECONDS + 1):
            self.assertEqual(self.client.get("/v1/secrets").status_code, 401)

        with patch.object(dashboard_auth, "_clock", return_value=start):
            busy = self.token_session()
        self.with_session(busy)
        at = start
        while at < start + dashboard_auth.SESSION_ABSOLUTE_SECONDS - 3600:
            at += 3600
            with patch.object(dashboard_auth, "_clock", return_value=at):
                self.assertEqual(self.client.get("/v1/secrets").status_code, 200)
        with patch.object(dashboard_auth, "_clock", return_value=start + dashboard_auth.SESSION_ABSOLUTE_SECONDS + 1):
            self.assertEqual(self.client.get("/v1/secrets").status_code, 401)

    def test_list_and_revoke_sessions(self):
        first = self.token_session()
        self.token_session()
        self.with_session(first)
        listed = self.client.get("/v1/auth/sessions").json()["sessions"]
        self.assertEqual(len(listed), 2)
        self.assertEqual(sum(item["current"] for item in listed), 1)
        other = next(item for item in listed if not item["current"])
        revoked = self.client.post("/v1/auth/sessions/revoke", headers={"Origin": ORIGIN}, json={"id": other["id"]})
        self.assertEqual(revoked.json(), {"revoked": 1})
        self.client.cookies.clear()
        everything = self.client.post("/v1/auth/sessions/revoke", headers=self.bearer(), json={"all": True})
        self.assertEqual(everything.json(), {"revoked": 1})
        self.with_session(first)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 401)

    def test_management_routes_need_the_management_authority(self):
        self.client.cookies.clear()
        for method, path in (("GET", "/v1/auth/passkeys"), ("GET", "/v1/auth/sessions"), ("POST", "/v1/auth/enrollments"),
                             ("POST", "/v1/auth/settings"), ("POST", "/v1/auth/passkey/register/options")):
            response = self.client.request(method, path, json={} if method == "POST" else None)
            self.assertEqual(response.status_code, 401, (path, response.text))

    def test_terminal_websocket_accepts_session_from_control_origin_only(self):
        from starlette.websockets import WebSocketDisconnect
        session_id = self.token_session()
        cookie = {"cookie": f"{COOKIE}={session_id}"}
        with self.client.websocket_connect("/v1/terminal/browser?node=missing", headers={**cookie, "Origin": ORIGIN}) as socket:
            socket.send_json({"type": "auth"})
            # Past authentication: the next failure is about the node, not the credential.
            self.assertEqual(socket.receive_json(), {"type": "error", "message": "node is not registered: missing"})
        with self.client.websocket_connect("/v1/terminal/browser?node=missing", headers={**cookie, "Origin": "https://evil.example"}) as socket:
            socket.send_json({"type": "auth"})
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
            self.assertEqual(closed.exception.code, 1008)

    def test_health_advertises_sessions_and_config_is_public(self):
        self.assertIn("dashboard-sessions-v1", self.client.get("/v1/health").json()["capabilities"])
        config = self.client.get("/v1/auth/config").json()
        self.assertEqual(config["rpId"], DOMAIN)
        self.assertFalse(config["passkeys"]["registered"])
        self.assertFalse(config["requirePasskey"])


@unittest.skipUnless(HAVE_WEBAUTHN, "webauthn extra is not installed (Python 3.10+)")
class PasskeyTests(DashboardAuthBase):
    def test_register_list_and_sign_in_with_passkey(self):
        authenticator = SoftAuthenticator()
        passkey = self.register(authenticator, name="MacBook Touch ID")
        self.assertEqual(passkey["name"], "MacBook Touch ID")
        self.assertEqual(passkey["transports"], ["internal", "hybrid"])
        listed = self.client.get("/v1/auth/passkeys", headers=self.bearer()).json()["passkeys"]
        self.assertEqual([item["id"] for item in listed], [passkey["id"]])
        self.assertTrue(self.client.get("/v1/auth/config").json()["passkeys"]["registered"])

        response, _body = self.sign_in(authenticator)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["session"]["method"], "passkey")
        self.assertEqual(response.json()["session"]["passkeyName"], "MacBook Touch ID")
        session_id = response.cookies[COOKIE]
        self.with_session(session_id)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 200)
        used = self.client.get("/v1/auth/passkeys").json()["passkeys"][0]
        self.assertIsNotNone(used["lastUsedAt"])

    def test_authentication_options_list_registered_credentials(self):
        authenticator = SoftAuthenticator()
        self.register(authenticator)
        options = self.client.post("/v1/auth/passkey/authenticate/options", json={}).json()["publicKey"]
        self.assertEqual(options["rpId"], DOMAIN)
        self.assertEqual(options["userVerification"], "required")
        self.assertEqual([item["id"] for item in options["allowCredentials"]], [b64url(authenticator.credential_id)])

    def test_challenge_is_single_use(self):
        authenticator = SoftAuthenticator()
        self.register(authenticator)
        first, body = self.sign_in(authenticator)
        self.assertEqual(first.status_code, 200)
        self.client.cookies.clear()
        replay = self.client.post("/v1/auth/passkey/authenticate/verify", headers={"Origin": ORIGIN}, json=body)
        self.assertEqual(replay.status_code, 401)
        self.assertNotIn(COOKIE, replay.cookies)

    def test_rejects_wrong_origin_key_and_cloned_counter(self):
        authenticator = SoftAuthenticator()
        self.register(authenticator)
        self.assertEqual(self.sign_in(authenticator)[0].status_code, 200)

        cloned, _ = self.sign_in(authenticator, sign_count=1)
        self.assertEqual(cloned.status_code, 401, "a counter that does not advance means a cloned authenticator")

        phishing = SoftAuthenticator(origin="https://luma.example.com.evil.example")
        phishing.key, phishing.credential_id, phishing.sign_count = authenticator.key, authenticator.credential_id, 50
        self.assertEqual(self.sign_in(phishing)[0].status_code, 401)

        impostor = SoftAuthenticator()
        impostor.credential_id = authenticator.credential_id
        impostor.sign_count = 60
        self.assertEqual(self.sign_in(impostor)[0].status_code, 401)

        stranger = SoftAuthenticator()
        response, _ = self.sign_in(stranger)
        self.assertEqual(response.status_code, 401)
        self.assertIn("not registered", response.json()["error"])

    def test_registration_rejects_foreign_origin_and_duplicates(self):
        authenticator = SoftAuthenticator()
        self.register(authenticator)
        options = self.client.post("/v1/auth/passkey/register/options", headers=self.bearer(), json={}).json()
        self.assertEqual(options["publicKey"]["excludeCredentials"][0]["id"], b64url(authenticator.credential_id))
        duplicate = self.client.post("/v1/auth/passkey/register/verify", headers=self.bearer(), json={
            "challengeId": options["challengeId"], "credential": authenticator.create(options["publicKey"]),
        })
        self.assertEqual(duplicate.status_code, 400)

        other = SoftAuthenticator(origin="https://evil.example")
        options = self.client.post("/v1/auth/passkey/register/options", headers=self.bearer(), json={}).json()
        foreign = self.client.post("/v1/auth/passkey/register/verify", headers=self.bearer(), json={
            "challengeId": options["challengeId"], "credential": other.create(options["publicKey"]),
        })
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(foreign.json()["errorInfo"]["code"], "passkey_invalid")

    def test_session_can_add_a_second_passkey(self):
        first = SoftAuthenticator()
        self.register(first)
        session_id = self.sign_in(first)[0].cookies[COOKIE]
        headers = self.with_session(session_id)
        second = SoftAuthenticator()
        options = self.client.post("/v1/auth/passkey/register/options", headers=headers, json={}).json()
        verify = self.client.post("/v1/auth/passkey/register/verify", headers=headers, json={
            "challengeId": options["challengeId"], "credential": second.create(options["publicKey"]), "name": "Phone",
        })
        self.assertEqual(verify.status_code, 200, verify.text)
        names = [item["name"] for item in self.client.get("/v1/auth/passkeys").json()["passkeys"]]
        self.assertEqual(names, ["Laptop", "Phone"])

    def test_enrollment_link_registers_once_and_signs_in(self):
        created = self.client.post("/v1/auth/enrollments", headers=self.bearer(), json={"name": "YubiKey"})
        self.assertEqual(created.status_code, 200, created.text)
        url = created.json()["url"]
        self.assertTrue(url.startswith(f"{ORIGIN}/dashboard/enroll#code="))
        code = url.split("#code=", 1)[1]

        self.client.cookies.clear()
        options = self.client.post("/v1/auth/passkey/register/options", json={"enrollmentCode": code})
        self.assertEqual(options.status_code, 200, options.text)
        self.assertEqual(options.json()["suggestedName"], "YubiKey")
        authenticator = SoftAuthenticator()
        verify = self.client.post("/v1/auth/passkey/register/verify", headers={"Origin": ORIGIN}, json={
            "challengeId": options.json()["challengeId"], "credential": authenticator.create(options.json()["publicKey"]),
        })
        self.assertEqual(verify.status_code, 200, verify.text)
        self.assertEqual(verify.json()["passkey"]["name"], "YubiKey")
        self.assertEqual(verify.json()["session"]["method"], "passkey")
        self.assertIn(COOKIE, verify.cookies)

        self.client.cookies.clear()
        again = self.client.post("/v1/auth/passkey/register/options", json={"enrollmentCode": code})
        self.assertEqual(again.status_code, 401)

    def test_enrollment_link_expires(self):
        start = 1_800_000_000.0
        with patch.object(dashboard_auth, "_clock", return_value=start):
            url = self.client.post("/v1/auth/enrollments", headers=self.bearer(), json={}).json()["url"]
        self.client.cookies.clear()
        with patch.object(dashboard_auth, "_clock", return_value=start + dashboard_auth.ENROLLMENT_SECONDS + 1):
            response = self.client.post("/v1/auth/passkey/register/options", json={"enrollmentCode": url.split("#code=")[1]})
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_verify_without_enrollment_is_rejected(self):
        options = self.client.post("/v1/auth/passkey/register/options", headers=self.bearer(), json={}).json()
        self.client.cookies.clear()
        response = self.client.post("/v1/auth/passkey/register/verify", headers={"Origin": ORIGIN}, json={
            "challengeId": options["challengeId"], "credential": SoftAuthenticator().create(options["publicKey"]),
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/v1/auth/passkeys", headers=self.bearer()).json()["passkeys"], [])

    def test_require_passkey_blocks_token_sign_in_but_never_the_cli(self):
        refused = self.client.post("/v1/auth/settings", headers=self.bearer(), json={"requirePasskey": True})
        self.assertEqual(refused.status_code, 400, "nothing to require before a passkey exists")

        token_session = self.token_session()
        authenticator = SoftAuthenticator()
        passkey = self.register(authenticator)
        enabled = self.client.post("/v1/auth/settings", headers=self.bearer(), json={"requirePasskey": True})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(enabled.json()["signedOutSessions"], 1)
        self.assertTrue(self.client.get("/v1/auth/config").json()["requirePasskey"])

        self.with_session(token_session)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 401, "token sessions end when passkeys become required")
        self.client.cookies.clear()
        blocked = self.client.post("/v1/auth/session", headers=self.bearer())
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["errorInfo"]["code"], "passkey_required")
        # The management token keeps working as an API credential.
        self.assertEqual(self.client.get("/v1/secrets", headers=self.bearer()).status_code, 200)
        self.assertEqual(self.sign_in(authenticator)[0].status_code, 200)

        # Removing the last passkey drops the requirement instead of locking browsers out.
        self.client.cookies.clear()
        removed = self.client.post("/v1/auth/passkeys/remove", headers=self.bearer(), json={"id": passkey["id"]})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["signedOutSessions"], 1)
        self.assertFalse(self.client.get("/v1/auth/settings", headers=self.bearer()).json()["requirePasskey"])
        self.assertEqual(self.client.post("/v1/auth/session", headers=self.bearer()).status_code, 200)

    def test_removing_a_passkey_ends_its_sessions(self):
        authenticator = SoftAuthenticator()
        passkey = self.register(authenticator)
        session_id = self.sign_in(authenticator)[0].cookies[COOKIE]
        self.client.cookies.clear()
        self.client.post("/v1/auth/passkeys/remove", headers=self.bearer(), json={"id": passkey["id"]})
        self.with_session(session_id)
        self.assertEqual(self.client.get("/v1/secrets").status_code, 401)
        self.client.cookies.clear()
        response = self.client.post("/v1/auth/passkey/authenticate/options", json={})
        self.assertEqual(response.status_code, 404)


class PasskeyUnavailableTests(DashboardAuthBase):
    def test_missing_library_reports_501_and_token_sessions_still_work(self):
        import builtins
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "webauthn" or name.startswith("webauthn."):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked):
            self.assertFalse(self.client.get("/v1/auth/config").json()["passkeys"]["available"])
            response = self.client.post("/v1/auth/passkey/register/options", headers=self.bearer(), json={})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["errorInfo"]["code"], "passkeys_unavailable")
        self.token_session()


if __name__ == "__main__":
    unittest.main()
