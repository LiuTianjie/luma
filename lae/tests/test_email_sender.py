from __future__ import annotations

import base64
import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "apps" / "api" / "src"))

from lae_api.email import (  # noqa: E402
    EmailChallengeDelivery,
    EmailConfigurationError,
    EmailDeliveryError,
    SmtpConfig,
    SmtpEmailSender,
    smtp_config_from_env,
)
from lae_api.app import AuthConfigurationError, _runtime_auth_service  # noqa: E402


class FakeSmtp:
    def __init__(self, *_args, **_kwargs) -> None:
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.login_args = None
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self, *, context) -> None:
        self.starttls_calls += 1
        self.tls_context = context

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message, *, from_addr: str, to_addrs: list[str]) -> None:
        self.sent = (message, from_addr, to_addrs)


class EmailSenderTests(unittest.IsolatedAsyncioTestCase):
    def config(self, *, security: str = "starttls") -> SmtpConfig:
        return SmtpConfig(
            host="smtp.example.test",
            port=587,
            security=security,
            sender="LAE <no-reply@example.test>",
            public_login_url="https://lae.itool.tech/login",
            username="smtp-user",
            password="smtp-password-canary",
            timeout_seconds=5,
        )

    def delivery(self) -> EmailChallengeDelivery:
        return EmailChallengeDelivery(
            challenge_id="emc_test",
            email="person@example.test",
            purpose="register",
            code="123456",
            magic_token="lae_em_magic-token-test",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    async def test_starttls_delivery_builds_fragment_link_and_redacts_config(self) -> None:
        fake = FakeSmtp()
        config = self.config()
        sender = SmtpEmailSender(config)
        with patch("lae_api.email.smtplib.SMTP", return_value=fake):
            await sender.send_auth_challenge(self.delivery())

        self.assertEqual(fake.starttls_calls, 1)
        self.assertEqual(fake.ehlo_calls, 2)
        self.assertEqual(fake.login_args, ("smtp-user", "smtp-password-canary"))
        message, from_addr, recipients = fake.sent
        self.assertEqual(from_addr, config.sender)
        self.assertEqual(recipients, ["person@example.test"])
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("123456", plain)
        self.assertIn("magicToken=lae_em_magic-token-test", plain)
        self.assertIn("purpose=register", plain)
        self.assertNotIn("smtp-password-canary", repr(config))
        self.assertNotIn("smtp-password-canary", repr(sender))

    async def test_provider_exception_is_replaced_with_stable_error(self) -> None:
        class FailingSmtp(FakeSmtp):
            def send_message(self, *_args, **_kwargs) -> None:
                raise RuntimeError("smtp-password-canary upstream detail")

        with patch("lae_api.email.smtplib.SMTP", return_value=FailingSmtp()):
            with self.assertRaises(EmailDeliveryError) as caught:
                await SmtpEmailSender(self.config()).send_auth_challenge(
                    self.delivery()
                )
        self.assertEqual(str(caught.exception), "email delivery failed")
        self.assertNotIn("smtp-password-canary", str(caught.exception))

        injected = replace(
            self.delivery(), email="person@example.test\r\nBcc: attacker@example.test"
        )
        with self.assertRaises(EmailDeliveryError) as invalid:
            await SmtpEmailSender(self.config()).send_auth_challenge(injected)
        self.assertEqual(str(invalid.exception), "email delivery failed")
        self.assertNotIn("attacker", str(invalid.exception))

    def test_environment_configuration_forbids_plaintext_in_production(self) -> None:
        values = {
            "LAE_SMTP_HOST": "mailpit",
            "LAE_SMTP_PORT": "1025",
            "LAE_SMTP_SECURITY": "plain",
            "LAE_EMAIL_FROM": "no-reply@example.test",
            "LAE_PUBLIC_LOGIN_URL": "https://lae.itool.tech/login",
        }
        with self.assertRaises(EmailConfigurationError):
            smtp_config_from_env(values, environment="production")
        config = smtp_config_from_env(values, environment="development")
        self.assertEqual((config.host, config.port, config.security), ("mailpit", 1025, "plain"))

    def test_production_requires_public_authenticated_smtp_and_real_sender(self) -> None:
        valid = {
            "LAE_SMTP_HOST": "smtp.mailgun.org",
            "LAE_SMTP_PORT": "465",
            "LAE_SMTP_SECURITY": "tls",
            "LAE_SMTP_USERNAME": "lae",
            "LAE_SMTP_PASSWORD": "provider-password-canary",
            "LAE_EMAIL_FROM": "LAE <no-reply@itool.tech>",
            "LAE_PUBLIC_LOGIN_URL": "https://lae.itool.tech/login",
        }
        config = smtp_config_from_env(valid, environment="production")
        self.assertEqual(config.host, "smtp.mailgun.org")

        invalid_variants = (
            {**valid, "LAE_SMTP_HOST": "mailpit"},
            {
                **valid,
                "LAE_SMTP_USERNAME": "",
                "LAE_SMTP_PASSWORD": "",
            },
            {**valid, "LAE_EMAIL_FROM": "no-reply@example.org"},
            {**valid, "LAE_SMTP_HOST": "smtp.example.test"},
            {**valid, "LAE_EMAIL_FROM": "<@itool.tech>"},
            {
                **valid,
                "LAE_PUBLIC_LOGIN_URL": "https://lae.example.test/login",
            },
        )
        for index, values in enumerate(invalid_variants):
            with self.subTest(case=index), self.assertRaises(
                EmailConfigurationError
            ):
                smtp_config_from_env(values, environment="production")

    def test_login_url_cannot_contain_credentials_or_query(self) -> None:
        for url in (
            "https://user:pass@lae.itool.tech/login",
            "https://lae.itool.tech/login?token=unsafe",
            "http://lae.itool.tech/login",
        ):
            with self.subTest(url=url), self.assertRaises(EmailConfigurationError):
                SmtpConfig(
                    host="smtp.example.test",
                    port=465,
                    security="tls",
                    sender="no-reply@example.test",
                    public_login_url=url,
                )

    async def test_runtime_wires_smtp_and_production_console_fails_closed(self) -> None:
        common = {
            "LAE_DATABASE_URL": "postgresql+asyncpg://lae:unused@127.0.0.1/lae",
            "LAE_AUTH_HMAC_KEY": base64.b64encode(b"k" * 32).decode(),
            "LAE_ENVIRONMENT": "production",
        }
        with patch.dict(
            os.environ,
            {**common, "LAE_EMAIL_DRIVER": "console"},
            clear=True,
        ):
            with self.assertRaises(AuthConfigurationError) as caught:
                _runtime_auth_service()
        self.assertIn("console email adapter", str(caught.exception))

        smtp = {
            **common,
            "LAE_EMAIL_DRIVER": "smtp",
            "LAE_SMTP_HOST": "smtp.mailgun.org",
            "LAE_SMTP_PORT": "465",
            "LAE_SMTP_SECURITY": "tls",
            "LAE_SMTP_USERNAME": "lae",
            "LAE_SMTP_PASSWORD": "runtime-password-canary",
            "LAE_EMAIL_FROM": "no-reply@itool.tech",
            "LAE_PUBLIC_LOGIN_URL": "https://lae.itool.tech/login",
        }
        with patch.dict(os.environ, smtp, clear=True):
            service, engine = _runtime_auth_service()
        self.assertIsInstance(service._email, SmtpEmailSender)
        self.assertTrue(service.external_mailbox_enabled)
        self.assertNotIn("runtime-password-canary", repr(service._email))
        await engine.dispose()

        development_console = {
            "LAE_DATABASE_URL": common["LAE_DATABASE_URL"],
            "LAE_AUTH_HMAC_KEY": common["LAE_AUTH_HMAC_KEY"],
            "LAE_ENVIRONMENT": "development",
            "LAE_EMAIL_DRIVER": "console",
        }
        with patch.dict(os.environ, development_console, clear=True):
            preview_only, engine = _runtime_auth_service()
        self.assertFalse(preview_only.external_mailbox_enabled)
        await engine.dispose()

        with patch.dict(
            os.environ,
            {**development_console, "LAE_AUTH_EXTERNAL_MAILBOX": "true"},
            clear=True,
        ):
            with self.assertRaises(AuthConfigurationError) as caught:
                _runtime_auth_service()
        self.assertIn("requires the SMTP adapter", str(caught.exception))

        with patch.dict(
            os.environ,
            {**development_console, "LAE_AUTH_EXTERNAL_MAILBOX": "sometimes"},
            clear=True,
        ):
            with self.assertRaises(AuthConfigurationError) as caught:
                _runtime_auth_service()
        self.assertIn("must be 0/1/false/true", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
