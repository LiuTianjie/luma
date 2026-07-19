from __future__ import annotations

import io
import logging
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

from lae_api.auth_service import AuthService, preview_email_from_env  # noqa: E402
from lae_api.app import _runtime_auth_service  # noqa: E402
from lae_api.email import (  # noqa: E402
    ConsoleEmailSender,
    EmailChallengeDelivery,
    RecordingEmailSender,
)
from lae_store.auth import (  # noqa: E402
    AuthCompletion,
    AuthConfigurationError,
    AuthKeyRing,
    AuthPolicy,
    AuthRejected,
    DEFAULT_DEPLOY_TOKEN_SCOPES,
    PendingEmailChallenge,
    SessionPrincipal,
    SUPPORTED_DEPLOY_TOKEN_SCOPES,
    normalize_email,
    safe_user_agent,
)
from lae_store.ids import new_id  # noqa: E402
from lae_store.models import AuthSession, EmailChallenge  # noqa: E402
from lae_store.security import ensure_persistable_payload  # noqa: E402
from lae_store.tokens import (  # noqa: E402
    issue_email_challenge,
    issue_session_credentials,
    parse_session_token,
    verify_csrf_token,
    verify_email_challenge,
)


class FakeAuthBackend:
    def __init__(self) -> None:
        challenge_id = new_id("emc")
        self.challenge = PendingEmailChallenge(
            id=challenge_id,
            email="person@example.test",
            purpose="register",
            code="123456",
            magic_token=f"lae_em_{challenge_id}_" + "A" * 43,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        self.return_challenge = True
        self.activated: list[str] = []
        self.canceled: list[str] = []
        self.revoked: list[str] = []
        self.completed = False

    async def begin_challenge(self, **_kwargs: object) -> PendingEmailChallenge | None:
        return self.challenge if self.return_challenge else None

    async def activate_challenge(self, challenge_id: str) -> bool:
        self.activated.append(challenge_id)
        return True

    async def cancel_challenge(self, challenge_id: str) -> None:
        self.canceled.append(challenge_id)

    async def complete_challenge(self, **kwargs: object) -> AuthCompletion:
        if self.completed or kwargs.get("credential") not in {
            self.challenge.code,
            self.challenge.magic_token,
        }:
            raise AuthRejected("authentication failed")
        self.completed = True
        return AuthCompletion(
            user_id=new_id("usr"),
            email=self.challenge.email,
            tenant_id=new_id("ten"),
            session_id=new_id("ses"),
            session_token="lae_ss_v1_" + "B" * 43,
            csrf_token="lae_cs_" + "C" * 43,
            session_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            entitlement_code="lite",
            default_deploy_token="lae_dt_0123456789_" + "D" * 43,
        )

    async def authenticate(self, _session_token: str) -> SessionPrincipal:
        return SessionPrincipal(
            session_id=new_id("ses"),
            user_id=new_id("usr"),
            email=self.challenge.email,
            tenant_id=new_id("ten"),
            entitlement_code="lite",
            key_version=1,
            csrf_digest=b"x" * 32,
        )

    async def revoke_session(self, session_id: str) -> None:
        self.revoked.append(session_id)

    def csrf_matches(self, _principal: SessionPrincipal, csrf_token: str) -> bool:
        return csrf_token == "lae_cs_" + "C" * 43


class AuthDomainTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_auth_records_redact_plaintext_credentials_from_repr(self) -> None:
        backend = FakeAuthBackend()
        self.assertNotIn(backend.challenge.code, repr(backend.challenge))
        self.assertNotIn(backend.challenge.magic_token, repr(backend.challenge))
        completion = await backend.complete_challenge(
            credential=backend.challenge.code
        )
        self.assertNotIn(completion.session_token, repr(completion))
        self.assertNotIn(completion.csrf_token, repr(completion))
        assert completion.default_deploy_token is not None
        self.assertNotIn(completion.default_deploy_token, repr(completion))

    def test_email_normalization_is_explicit_and_bounded(self) -> None:
        self.assertEqual(normalize_email(" Person@Example.TEST "), "person@example.test")
        for invalid in (
            "no-at-sign",
            "a@localhost",
            ".a@example.test",
            "a..b@example.test",
            "用戶@example.test",
        ):
            with self.assertRaises(ValueError):
                normalize_email(invalid)

    def test_user_agent_rejects_embedded_credentials(self) -> None:
        self.assertEqual(safe_user_agent("lae-cli/0.1"), "lae-cli/0.1")
        for secret in (
            "lae_dt_0123456789_" + "A" * 43,
            "lae_ss_v1_" + "S" * 43,
            "lae_cs_" + "C" * 43,
            "Bearer " + "x" * 32,
        ):
            self.assertIsNone(safe_user_agent(f"agent {secret}"))

    def test_policy_and_key_ring_reject_unsafe_configuration(self) -> None:
        AuthPolicy()
        with self.assertRaises(ValueError):
            AuthPolicy(challenge_ttl=timedelta(hours=2))
        with self.assertRaises(ValueError):
            AuthKeyRing(current_version=1, keys={1: b"short"})
        ring = AuthKeyRing(current_version=2, keys={1: b"a" * 32, 2: b"b" * 32})
        self.assertEqual(ring.current_key, b"b" * 32)
        self.assertNotIn((b"b" * 32).decode(), repr(ring))
        self.assertIn("billing:checkout", SUPPORTED_DEPLOY_TOKEN_SCOPES)
        self.assertNotIn("billing:checkout", DEFAULT_DEPLOY_TOKEN_SCOPES)

    def test_preview_auth_is_development_only_and_requires_a_reserved_mailbox(self) -> None:
        self.assertIsNone(preview_email_from_env({}, environment="production"))
        self.assertEqual(
            preview_email_from_env(
                {
                    "LAE_AUTH_PREVIEW_MODE": "public",
                    "LAE_AUTH_PREVIEW_EMAIL": "Preview@LAE.Invalid",
                },
                environment="development",
            ),
            "preview@lae.invalid",
        )
        for values, environment in (
            (
                {
                    "LAE_AUTH_PREVIEW_MODE": "public",
                    "LAE_AUTH_PREVIEW_EMAIL": "preview@lae.invalid",
                },
                "production",
            ),
            (
                {
                    "LAE_AUTH_PREVIEW_MODE": "public",
                    "LAE_AUTH_PREVIEW_EMAIL": "person@itool.tech",
                },
                "development",
            ),
            ({"LAE_AUTH_PREVIEW_MODE": "unexpected"}, "development"),
        ):
            with self.subTest(values=values, environment=environment):
                with self.assertRaises(AuthConfigurationError):
                    preview_email_from_env(values, environment=environment)

    def test_code_magic_session_and_csrf_are_keyed_and_domain_separated(self) -> None:
        key = b"k" * 32
        challenge_id = new_id("emc")
        issued = issue_email_challenge(key, key_version=3, challenge_id=challenge_id)
        self.assertRegex(issued.code, r"^[0-9]{6}$")
        self.assertTrue(
            verify_email_challenge(
                issued.code,
                method="code",
                challenge_id=challenge_id,
                expected_digest=issued.code_digest,
                key=key,
            )
        )
        self.assertTrue(
            verify_email_challenge(
                issued.magic_token,
                method="magic",
                challenge_id=challenge_id,
                expected_digest=issued.magic_token_digest,
                key=key,
            )
        )
        self.assertFalse(
            verify_email_challenge(
                "000000" if issued.code != "000000" else "999999",
                method="code",
                challenge_id=challenge_id,
                expected_digest=issued.code_digest,
                key=key,
            )
        )
        self.assertNotIn(issued.code.encode(), issued.code_digest)
        self.assertNotIn(issued.magic_token.encode(), issued.magic_token_digest)
        self.assertNotIn(issued.code, repr(issued))
        self.assertNotIn(issued.magic_token, repr(issued))

        session = issue_session_credentials(key, key_version=3)
        self.assertEqual(parse_session_token(session.session_token), 3)
        self.assertTrue(
            verify_csrf_token(
                session.csrf_token, expected_digest=session.csrf_digest, key=key
            )
        )
        self.assertNotEqual(session.session_digest, session.csrf_digest)
        self.assertNotIn(session.session_token, repr(session))
        self.assertNotIn(session.csrf_token, repr(session))
        for secret in (
            issued.magic_token,
            session.session_token,
            session.csrf_token,
        ):
            with self.assertRaises(ValueError):
                ensure_persistable_payload({"message": f"credential={secret}"})

    def test_models_have_only_hashes_and_key_versions(self) -> None:
        challenge_columns = set(EmailChallenge.__table__.columns.keys())
        self.assertTrue({"code_hash", "magic_token_hash", "key_version"} <= challenge_columns)
        self.assertFalse(
            challenge_columns & {"code", "magic_token", "token", "plaintext", "secret"}
        )
        session_columns = set(AuthSession.__table__.columns.keys())
        self.assertTrue({"session_hash", "csrf_hash", "key_version"} <= session_columns)
        self.assertFalse(session_columns & {"session_token", "csrf_token", "plaintext"})

    async def test_start_has_same_result_and_injectable_minimum_response_floor(self) -> None:
        backend = FakeAuthBackend()
        sender = RecordingEmailSender()
        slept: list[float] = []
        clock = iter((10.0, 10.03, 20.0, 20.03))

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        service = AuthService(
            backend,
            sender,
            minimum_start_duration=0.2,
            monotonic=lambda: next(clock),
            sleeper=fake_sleep,
        )
        accepted = await service.start(
            email="person@example.test",
            purpose="register",
            request_ip="203.0.113.10",
            device_id="device-a",
        )
        backend.return_challenge = False
        ineligible = await service.start(
            email="missing@example.test",
            purpose="login",
            request_ip="203.0.113.10",
            device_id="device-a",
        )
        self.assertEqual(accepted, ineligible)
        self.assertEqual(len(sender.deliveries), 1)
        self.assertEqual(backend.activated, [backend.challenge.id])
        self.assertEqual(len(slept), 2)
        self.assertAlmostEqual(slept[0], slept[1], places=6)

    async def test_preview_challenge_falls_back_to_registration_without_sending_mail(self) -> None:
        class FreshPreviewBackend(FakeAuthBackend):
            async def begin_challenge(
                self, **kwargs: object
            ) -> PendingEmailChallenge | None:
                if kwargs.get("purpose") == "login":
                    return None
                return replace(
                    self.challenge,
                    email=str(kwargs["email"]),
                    purpose=str(kwargs["purpose"]),
                )

        backend = FreshPreviewBackend()
        downstream = RecordingEmailSender()
        service = AuthService(
            backend,
            downstream,
            minimum_start_duration=0,
            preview_email="preview@lae.invalid",
        )
        delivery = await service.request_preview_challenge(
            request_ip="203.0.113.10",
            device_id="preview-device",
        )
        self.assertEqual(delivery.email, "preview@lae.invalid")
        self.assertEqual(delivery.purpose, "register")
        self.assertEqual(downstream.deliveries, [])
        self.assertEqual(backend.activated, [backend.challenge.id])

    async def test_email_failure_cancels_pending_flow_and_never_logs_credentials(self) -> None:
        backend = FakeAuthBackend()
        sender = RecordingEmailSender(fail_next=True)
        log_output = io.StringIO()
        logger = logging.getLogger(f"lae-auth-test-{id(self)}")
        logger.handlers = [logging.StreamHandler(log_output)]
        logger.propagate = False
        service = AuthService(
            backend, sender, logger=logger, minimum_start_duration=0
        )
        result = await service.start(
            email="person@example.test",
            purpose="register",
            request_ip=None,
            device_id=None,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(backend.activated, [])
        self.assertEqual(backend.canceled, [backend.challenge.id])
        logs = log_output.getvalue()
        self.assertNotIn(backend.challenge.code, logs)
        self.assertNotIn(backend.challenge.magic_token, logs)
        self.assertNotIn(backend.challenge.email, logs)

    async def test_console_fake_redacts_credentials_and_production_fails_closed(self) -> None:
        log_output = io.StringIO()
        logger = logging.getLogger(f"lae-console-test-{id(self)}")
        logger.handlers = [logging.StreamHandler(log_output)]
        logger.propagate = False
        delivery = EmailChallengeDelivery(
            challenge_id=new_id("emc"),
            email="person@example.test",
            purpose="register",
            code="654321",
            magic_token="lae_em_" + "A" * 74,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        await ConsoleEmailSender(logger).send_auth_challenge(delivery)
        rendered = log_output.getvalue()
        self.assertNotIn(delivery.email, rendered)
        self.assertNotIn(delivery.code, rendered)
        self.assertNotIn(delivery.magic_token, rendered)

        with patch.dict(
            "os.environ",
            {
                "LAE_DATABASE_URL": "postgresql+asyncpg://localhost/lae",
                "LAE_EMAIL_DRIVER": "console",
                "LAE_ENVIRONMENT": "production",
            },
            clear=True,
        ):
            with self.assertRaises(AuthConfigurationError):
                _runtime_auth_service()

    async def test_verification_is_one_shot_and_purpose_bound_by_backend_contract(self) -> None:
        backend = FakeAuthBackend()
        service = AuthService(
            backend, RecordingEmailSender(), minimum_start_duration=0
        )
        first = await service.verify(
            email="person@example.test",
            purpose="register",
            code="123456",
            magic_token=None,
            request_ip=None,
            user_agent=None,
        )
        self.assertEqual(first.entitlement_code, "lite")
        with self.assertRaises(AuthRejected):
            await service.verify(
                email="person@example.test",
                purpose="register",
                code="123456",
                magic_token=None,
                request_ip=None,
                user_agent=None,
            )
        with self.assertRaises(AuthRejected):
            await service.verify(
                email="person@example.test",
                purpose="login",
                code="123456",
                magic_token=backend.challenge.magic_token,
                request_ip=None,
                user_agent=None,
            )


if __name__ == "__main__":
    unittest.main()
