from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-core" / "src"))
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_api.app import (  # noqa: E402
    CSRF_COOKIE,
    SESSION_COOKIE,
    ApiError,
    create_app,
    require_scope,
)
from lae_api.auth_service import AuthService  # noqa: E402
from lae_api.email import RecordingEmailSender  # noqa: E402
from lae_store.auth import (  # noqa: E402
    AuthCompletion,
    AuthRejected,
    DeployTokenConflict,
    DeployTokenPrincipal,
    DeployTokenRecord,
    IssuedManagedDeployToken,
    PendingEmailChallenge,
    SessionPrincipal,
)
from lae_store.ids import new_id  # noqa: E402


class ApiAuthBackend:
    def __init__(self) -> None:
        self.code = "123456"
        self.session_token = "lae_ss_v1_" + "S" * 43
        self.csrf_token = "lae_cs_" + "C" * 43
        self.deploy_token = "lae_dt_0123456789_" + "D" * 43
        self.challenge_id = new_id("emc")
        self.magic = f"lae_em_{self.challenge_id}_" + "M" * 43
        self.user_id = new_id("usr")
        self.tenant_id = new_id("ten")
        self.session_id = new_id("ses")
        self.register_consumed = False
        self.challenge_purposes: dict[str, str] = {}
        self.revoked: list[str] = []
        self.revoked_deploy: list[str] = []
        self.deploy_scopes = frozenset(
            {
                "analyses:write",
                "apps:read",
                "apps:write",
                "deployments:write",
                "logs:read",
            }
        )
        self.deploy_token_id = new_id("dtk")
        self.deploy_tenant_id = self.tenant_id

    async def begin_challenge(self, **kwargs: object) -> PendingEmailChallenge | None:
        email = str(kwargs["email"])
        purpose = str(kwargs["purpose"])
        if purpose == "auto":
            purpose = "login" if email.startswith("existing") else "register"
        if email.startswith("missing") or email.startswith("existing") and purpose == "register":
            return None
        self.challenge_purposes[email] = purpose
        return PendingEmailChallenge(
            id=self.challenge_id,
            email=email,
            purpose=purpose,
            code=self.code,
            magic_token=self.magic,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    async def activate_challenge(self, _challenge_id: str) -> bool:
        return True

    async def cancel_challenge(self, _challenge_id: str) -> None:
        return None

    async def complete_challenge(self, **kwargs: object) -> AuthCompletion:
        purpose = str(kwargs["purpose"])
        if purpose == "auto":
            purpose = self.challenge_purposes.get(str(kwargs["email"]), "register")
        method = str(kwargs["method"])
        credential = str(kwargs["credential"])
        if credential != (self.code if method == "code" else self.magic):
            raise AuthRejected("invalid")
        if purpose == "register" and self.register_consumed:
            raise AuthRejected("replayed")
        if purpose == "register":
            self.register_consumed = True
        return AuthCompletion(
            user_id=self.user_id,
            email=str(kwargs["email"]),
            tenant_id=self.tenant_id,
            session_id=self.session_id,
            session_token=self.session_token,
            csrf_token=self.csrf_token,
            session_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            entitlement_code="lite",
            default_deploy_token=self.deploy_token if purpose == "register" else None,
        )

    async def authenticate(self, session_token: str) -> SessionPrincipal:
        if session_token != self.session_token or self.revoked:
            raise AuthRejected("invalid")
        return SessionPrincipal(
            session_id=self.session_id,
            user_id=self.user_id,
            email="person@example.test",
            tenant_id=self.tenant_id,
            entitlement_code="lite",
            key_version=1,
            csrf_digest=b"c" * 32,
        )

    async def revoke_session(self, session_id: str) -> None:
        self.revoked.append(session_id)

    def csrf_matches(self, _principal: SessionPrincipal, token: str) -> bool:
        return token == self.csrf_token

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        if token != self.deploy_token:
            raise AuthRejected("invalid")
        return DeployTokenPrincipal(
            token_id=self.deploy_token_id,
            token_prefix="0123456789",
            user_id=self.user_id,
            email="person@example.test",
            tenant_id=self.deploy_tenant_id,
            entitlement_code="lite",
            member_role="owner",
            scopes=self.deploy_scopes,
        )

    def _record(
        self, *, token_id: str | None = None, is_default: bool = False
    ) -> DeployTokenRecord:
        now = datetime.now(timezone.utc)
        return DeployTokenRecord(
            id=token_id or self.deploy_token_id,
            name="Default deploy token" if is_default else "Automation",
            prefix="0123456789",
            scopes=tuple(sorted(self.deploy_scopes)),
            purpose="deploy",
            is_default=is_default,
            expires_at=None,
            revoked_at=None,
            last_used_at=None,
            last_used_ip=None,
            created_at=now,
        )

    async def list_deploy_tokens(
        self, _principal: SessionPrincipal
    ) -> tuple[DeployTokenRecord, ...]:
        return (self._record(is_default=True),)

    async def create_deploy_token(
        self,
        _principal: SessionPrincipal,
        **_kwargs: object,
    ) -> IssuedManagedDeployToken:
        return IssuedManagedDeployToken(
            record=self._record(token_id=new_id("dtk")),
            plaintext="lae_dt_ABCDEFGHIJ_" + "N" * 43,
        )

    async def rotate_deploy_token(
        self, _principal: SessionPrincipal, token_id: str
    ) -> IssuedManagedDeployToken:
        return IssuedManagedDeployToken(
            record=self._record(token_id=new_id("dtk"), is_default=True),
            plaintext="lae_dt_JKLMNPQRST_" + "R" * 43,
        )

    async def revoke_deploy_token(
        self, _principal: SessionPrincipal, token_id: str
    ) -> None:
        if token_id == self.deploy_token_id:
            raise DeployTokenConflict("default_protected")
        self.revoked_deploy.append(token_id)


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = ApiAuthBackend()
        self.mail = RecordingEmailSender()
        self.service = AuthService(
            self.backend, self.mail, minimum_start_duration=0
        )
        self.client_context = TestClient(
            create_app(self.service), base_url="https://lae.example.test"
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_start_endpoints_are_generic_202_and_do_not_return_credentials(self) -> None:
        expected = {"accepted": True}
        eligible = self.client.post(
            "/v1/auth/register", json={"email": "person@example.test"}
        )
        existing = self.client.post(
            "/v1/auth/register", json={"email": "existing@example.test"}
        )
        missing = self.client.post(
            "/v1/auth/login/request", json={"email": "missing@example.test"}
        )
        malformed = self.client.post(
            "/v1/auth/register",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
        for response in (eligible, existing, missing, malformed):
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), expected)
            self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
            serialized = response.text
            self.assertNotIn(self.backend.code, serialized)
            self.assertNotIn(self.backend.magic, serialized)
        self.assertEqual(len(self.mail.deliveries), 1)

    def test_unified_email_flow_registers_or_logs_in_without_exposing_branch(self) -> None:
        registered = self.client.post(
            "/v1/auth/email/request", json={"email": "person@example.test"}
        )
        existing = self.client.post(
            "/v1/auth/email/request", json={"email": "existing@example.test"}
        )
        self.assertEqual(registered.status_code, 202)
        self.assertEqual(existing.status_code, 202)
        self.assertEqual(registered.json(), existing.json())
        self.assertEqual(
            [delivery.purpose for delivery in self.mail.deliveries],
            ["register", "login"],
        )

        completed_registration = self.client.post(
            "/v1/auth/email/complete",
            json={"email": "person@example.test", "code": self.backend.code},
        )
        self.assertEqual(completed_registration.status_code, 200)
        self.assertEqual(
            completed_registration.json()["defaultDeployToken"],
            self.backend.deploy_token,
        )

        completed_login = self.client.post(
            "/v1/auth/email/complete",
            json={"email": "existing@example.test", "code": self.backend.code},
        )
        self.assertEqual(completed_login.status_code, 200)
        self.assertNotIn("defaultDeployToken", completed_login.json())

    def test_preview_auth_is_explicit_and_only_returns_reserved_mailbox_credentials(self) -> None:
        disabled_config = self.client.get("/v1/auth/config")
        self.assertEqual(
            disabled_config.json(),
            {
                "emailDelivery": {
                    "mode": "email",
                    "externalMailbox": True,
                    "previewAccess": False,
                }
            },
        )
        self.assertEqual(self.client.post("/v1/auth/preview").status_code, 404)

        class DistinctPreviewBackend(ApiAuthBackend):
            preview_code = "222222"
            ordinary_code = "111111"

            async def begin_challenge(
                self, **kwargs: object
            ) -> PendingEmailChallenge | None:
                email = str(kwargs["email"])
                purpose = str(kwargs["purpose"])
                challenge_id = new_id("emc")
                code = (
                    self.preview_code
                    if email == "preview@lae.invalid"
                    else self.ordinary_code
                )
                return PendingEmailChallenge(
                    id=challenge_id,
                    email=email,
                    purpose=purpose,
                    code=code,
                    magic_token=f"lae_em_{challenge_id}_" + "P" * 43,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )

        backend = DistinctPreviewBackend()
        downstream_mail = RecordingEmailSender()
        preview_service = AuthService(
            backend,
            downstream_mail,
            minimum_start_duration=0,
            preview_email="preview@lae.invalid",
            external_mailbox_enabled=False,
        )
        with TestClient(
            create_app(preview_service), base_url="https://lae.example.test"
        ) as preview_client:
            config = preview_client.get("/v1/auth/config")
            self.assertEqual(config.status_code, 200)
            self.assertEqual(config.json()["emailDelivery"]["mode"], "preview")
            self.assertFalse(config.json()["emailDelivery"]["externalMailbox"])

            ordinary = preview_client.post(
                "/v1/auth/register", json={"email": "person@example.test"}
            )
            self.assertEqual(ordinary.json(), {"accepted": True})
            self.assertEqual(
                [delivery.email for delivery in downstream_mail.deliveries],
                ["person@example.test"],
            )
            self.assertEqual(
                downstream_mail.deliveries[0].code, backend.ordinary_code
            )

            issued = preview_client.post("/v1/auth/preview")
            self.assertEqual(issued.status_code, 201)
            self.assertEqual(issued.headers["cache-control"], "no-store, max-age=0")
            self.assertEqual(issued.json()["email"], "preview@lae.invalid")
            self.assertEqual(issued.json()["purpose"], "login")
            self.assertEqual(issued.json()["code"], backend.preview_code)
            self.assertNotEqual(
                issued.json()["code"], downstream_mail.deliveries[0].code
            )
            self.assertTrue(issued.json()["magicToken"].startswith("lae_em_"))
            self.assertEqual(len(downstream_mail.deliveries), 1)

        hybrid_service = AuthService(
            DistinctPreviewBackend(),
            RecordingEmailSender(),
            minimum_start_duration=0,
            preview_email="preview@lae.invalid",
            external_mailbox_enabled=True,
        )
        with TestClient(
            create_app(hybrid_service), base_url="https://lae.example.test"
        ) as hybrid_client:
            config = hybrid_client.get("/v1/auth/config")
            self.assertEqual(
                config.json(),
                {
                    "emailDelivery": {
                        "mode": "email",
                        "externalMailbox": True,
                        "previewAccess": True,
                    }
                },
            )

    def test_verify_requires_exactly_one_method_and_uses_stable_error_envelope(self) -> None:
        for payload in (
            {"email": "person@example.test"},
            {
                "email": "person@example.test",
                "code": self.backend.code,
                "magicToken": self.backend.magic,
            },
        ):
            response = self.client.post("/v1/auth/email/verify", json=payload)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "LAE_AUTH_CHALLENGE_INVALID")
            self.assertRegex(response.json()["error"]["requestId"], r"^req_")

    def test_registration_sets_secure_cookies_and_returns_default_token_once(self) -> None:
        response = self.client.post(
            "/v1/auth/email/verify",
            json={"email": "person@example.test", "code": self.backend.code},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["defaultDeployToken"], self.backend.deploy_token)
        self.assertEqual(response.json()["entitlement"], {"plan": "lite"})
        self.assertNotIn("session", response.text.lower())
        self.assertNotIn(self.backend.session_token, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(item for item in cookies if item.startswith(SESSION_COOKIE))
        csrf_cookie = next(item for item in cookies if item.startswith(CSRF_COOKIE))
        for item in cookies:
            self.assertIn("Secure", item)
            self.assertIn("SameSite=lax", item)
            self.assertIn("Path=/", item)
        self.assertIn("HttpOnly", session_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)

        replay = self.client.post(
            "/v1/auth/email/verify",
            json={"email": "person@example.test", "code": self.backend.code},
        )
        self.assertEqual(replay.status_code, 401)
        self.assertNotIn("defaultDeployToken", replay.text)

        login = self.client.post(
            "/v1/auth/login/verify",
            json={"email": "person@example.test", "magicToken": self.backend.magic},
        )
        self.assertEqual(login.status_code, 200)
        self.assertNotIn("defaultDeployToken", login.json())

    def test_me_requires_authentication_and_logout_requires_double_submit_csrf(self) -> None:
        fresh = TestClient(
            create_app(self.service), base_url="https://lae.example.test"
        )
        with fresh:
            unauthenticated = fresh.get("/v1/me")
            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(
                unauthenticated.json()["error"]["code"], "LAE_UNAUTHENTICATED"
            )

        self.client.post(
            "/v1/auth/email/verify",
            json={"email": "person@example.test", "code": self.backend.code},
        )
        me = self.client.get("/v1/me")
        self.assertEqual(me.status_code, 200)
        without_header = self.client.post("/v1/auth/logout")
        self.assertEqual(without_header.status_code, 403)
        self.assertEqual(without_header.json()["error"]["code"], "LAE_CSRF_FAILED")
        csrf = self.client.cookies.get(CSRF_COOKIE)
        logout = self.client.post(
            "/v1/auth/logout", headers={"X-CSRF-Token": str(csrf)}
        )
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.backend.revoked, [self.backend.session_id])

    def test_bearer_me_and_verify_use_one_generic_unauthenticated_error(self) -> None:
        authorization = {"Authorization": f"Bearer {self.backend.deploy_token}"}
        me = self.client.get("/v1/me", headers=authorization)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["credential"]["type"], "deploy_token")
        verified = self.client.post("/v1/auth/token/verify", headers=authorization)
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["token"]["prefix"], "0123456789")
        self.assertNotIn(self.backend.deploy_token, verified.text)

        wrong = "lae_dt_0123456789_" + "W" * 43
        for value in (wrong, "malformed", "Basic abc"):
            header = value if value.startswith("Basic") else f"Bearer {value}"
            rejected = self.client.get(
                "/v1/me", headers={"Authorization": header}
            )
            self.assertEqual(rejected.status_code, 401)
            self.assertEqual(
                rejected.json()["error"]["code"], "LAE_UNAUTHENTICATED"
            )
            self.assertNotIn(value, rejected.text)

    def test_dual_credentials_must_match_and_cookie_authority_wins(self) -> None:
        self.client.post(
            "/v1/auth/email/verify",
            json={"email": "person@example.test", "code": self.backend.code},
        )
        authorization = {"Authorization": f"Bearer {self.backend.deploy_token}"}
        same = self.client.get("/v1/me", headers=authorization)
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.json()["credential"]["type"], "session")

        self.backend.deploy_tenant_id = new_id("ten")
        confused = self.client.get("/v1/me", headers=authorization)
        self.assertEqual(confused.status_code, 401)
        self.assertEqual(
            confused.json()["error"]["code"], "LAE_UNAUTHENTICATED"
        )

    def test_token_management_is_session_only_csrf_protected_and_once_display(self) -> None:
        bearer_only = self.client.post(
            "/v1/deploy-tokens",
            headers={"Authorization": f"Bearer {self.backend.deploy_token}"},
            json={"name": "CI", "scopes": ["deployments:write"]},
        )
        self.assertEqual(bearer_only.status_code, 401)

        self.client.post(
            "/v1/auth/email/verify",
            json={"email": "person@example.test", "code": self.backend.code},
        )
        without_csrf = self.client.post(
            "/v1/deploy-tokens",
            json={"name": "CI", "scopes": ["deployments:write"]},
        )
        self.assertEqual(without_csrf.status_code, 403)
        csrf = str(self.client.cookies.get(CSRF_COOKIE))
        created = self.client.post(
            "/v1/deploy-tokens",
            headers={"X-CSRF-Token": csrf},
            json={"name": "CI", "scopes": ["deployments:write"]},
        )
        self.assertEqual(created.status_code, 201)
        plaintext = created.json()["plaintext"]
        self.assertTrue(plaintext.startswith("lae_dt_"))
        listed = self.client.get("/v1/deploy-tokens")
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("plaintext", listed.text)
        self.assertNotIn(plaintext, listed.text)

        protected = self.client.delete(
            f"/v1/deploy-tokens/{self.backend.deploy_token_id}",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(protected.status_code, 409)
        self.assertEqual(
            protected.json()["error"]["code"],
            "LAE_DEFAULT_DEPLOY_TOKEN_PROTECTED",
        )
        rotated = self.client.post(
            f"/v1/deploy-tokens/{self.backend.deploy_token_id}/rotate",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(rotated.status_code, 200)
        self.assertTrue(rotated.json()["token"]["isDefault"])
        self.assertIn("plaintext", rotated.json())

    def test_scope_guard_covers_tenant_operation_scopes(self) -> None:
        principal = DeployTokenPrincipal(
            token_id=self.backend.deploy_token_id,
            token_prefix="0123456789",
            user_id=self.backend.user_id,
            email="person@example.test",
            tenant_id=self.backend.tenant_id,
            entitlement_code="lite",
            member_role="owner",
            scopes=self.backend.deploy_scopes,
        )
        for scope in (
            "analyses:write",
            "deployments:write",
            "apps:read",
            "apps:write",
            "logs:read",
        ):
            require_scope(principal, scope)
        with self.assertRaises(ApiError) as caught:
            require_scope(principal, "admin:write")
        self.assertEqual(caught.exception.status, 403)

    def test_request_id_is_bounded_and_unexpected_errors_do_not_echo_secrets(self) -> None:
        missing = self.client.get("/v1/not-a-route")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "LAE_NOT_FOUND")
        response = self.client.get(
            "/v1/me", headers={"X-Request-Id": "request-safe-1"}
        )
        self.assertEqual(response.headers["X-Request-Id"], "request-safe-1")

        sentinel = "lae_dt_0123456789_" + "Z" * 43
        secret_request_id = self.client.get(
            "/v1/not-a-route", headers={"X-Request-Id": sentinel}
        )
        self.assertNotEqual(secret_request_id.headers["X-Request-Id"], sentinel)
        self.assertNotIn(sentinel, secret_request_id.text)

        class ExplodingService:
            async def verify(self, **_kwargs: object) -> None:
                raise RuntimeError(sentinel)

        with TestClient(
            create_app(ExplodingService()),
            base_url="https://lae.example.test",
            raise_server_exceptions=False,
        ) as client:
            failed = client.post(
                "/v1/auth/email/verify",
                json={"email": "person@example.test", "code": "123456"},
            )
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.json()["error"]["code"], "LAE_INTERNAL")
        self.assertNotIn(sentinel, failed.text)

    def test_every_response_has_browser_security_headers(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(
            response.headers["cross-origin-resource-policy"], "same-site"
        )
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertIn("max-age=31536000", response.headers["strict-transport-security"])
        self.assertIn("payment=()", response.headers["permissions-policy"])


if __name__ == "__main__":
    unittest.main()
