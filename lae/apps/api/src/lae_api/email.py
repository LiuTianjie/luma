from __future__ import annotations

import asyncio
import html
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Mapping, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class EmailChallengeDelivery:
    challenge_id: str
    email: str
    purpose: str
    code: str
    magic_token: str
    expires_at: datetime


class EmailSender(Protocol):
    async def send_auth_challenge(self, delivery: EmailChallengeDelivery) -> None: ...


class EmailConfigurationError(RuntimeError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    security: str
    sender: str
    public_login_url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        if (
            not self.host
            or any(char.isspace() or ord(char) < 0x20 for char in self.host)
            or not 1 <= self.port <= 65535
            or self.security not in {"tls", "starttls", "plain"}
            or not 1 <= self.timeout_seconds <= 60
            or (self.username is None) != (self.password is None)
            or "\r" in self.sender
            or "\n" in self.sender
            or "@" not in self.sender
        ):
            raise EmailConfigurationError("SMTP configuration is invalid")
        parsed = urlsplit(self.public_login_url)
        local = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in ({"https", "http"} if local else {"https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise EmailConfigurationError("public login URL is invalid")

    def __repr__(self) -> str:
        return (
            "SmtpConfig("
            f"host={self.host!r}, port={self.port!r}, security={self.security!r}, "
            f"sender={self.sender!r}, public_login_url={self.public_login_url!r}, "
            f"username={self.username!r}, password=<redacted>, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


class SmtpEmailSender:
    """SMTP adapter with TLS-by-default and credential-safe failures."""

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def __repr__(self) -> str:
        return f"SmtpEmailSender(config={self._config!r})"

    async def send_auth_challenge(self, delivery: EmailChallengeDelivery) -> None:
        try:
            message = _auth_message(self._config, delivery)
            await asyncio.wait_for(
                asyncio.to_thread(self._send_sync, message),
                timeout=self._config.timeout_seconds + 2,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # SMTP libraries and provider gateways often include commands or
            # endpoints in exceptions. Keep the public/loggable error stable.
            raise EmailDeliveryError("email delivery failed") from None

    def _send_sync(self, message: EmailMessage) -> None:
        config = self._config
        context = ssl.create_default_context()
        smtp_type = smtplib.SMTP_SSL if config.security == "tls" else smtplib.SMTP
        with smtp_type(config.host, config.port, timeout=config.timeout_seconds) as smtp:
            if config.security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if config.username is not None and config.password is not None:
                smtp.login(config.username, config.password)
            smtp.send_message(
                message,
                from_addr=config.sender,
                to_addrs=[str(message["To"])],
            )


def smtp_config_from_env(
    values: Mapping[str, str], *, environment: str
) -> SmtpConfig:
    security = values.get("LAE_SMTP_SECURITY", "starttls").strip().lower()
    if security == "plain" and environment.lower() in {"production", "prod"}:
        raise EmailConfigurationError("plaintext SMTP is forbidden in production")
    try:
        port = int(values.get("LAE_SMTP_PORT", "465" if security == "tls" else "587"))
        timeout = float(values.get("LAE_SMTP_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise EmailConfigurationError("SMTP configuration is invalid") from exc
    return SmtpConfig(
        host=values.get("LAE_SMTP_HOST", "").strip(),
        port=port,
        security=security,
        sender=values.get("LAE_EMAIL_FROM", "").strip(),
        public_login_url=values.get("LAE_PUBLIC_LOGIN_URL", "").strip(),
        username=values.get("LAE_SMTP_USERNAME") or None,
        password=values.get("LAE_SMTP_PASSWORD") or None,
        timeout_seconds=timeout,
    )


def _auth_message(config: SmtpConfig, delivery: EmailChallengeDelivery) -> EmailMessage:
    if (
        delivery.purpose not in {"register", "login"}
        or "\r" in delivery.email
        or "\n" in delivery.email
        or "@" not in delivery.email
        or not re.fullmatch(r"[0-9]{6}", delivery.code)
        or not delivery.magic_token.startswith("lae_em_")
        or len(delivery.magic_token) > 128
        or delivery.expires_at.tzinfo is None
    ):
        raise EmailConfigurationError("email delivery payload is invalid")
    purpose_label = "注册" if delivery.purpose == "register" else "登录"
    fragment = urlencode(
        {
            "email": delivery.email,
            "magicToken": delivery.magic_token,
            "purpose": delivery.purpose,
        }
    )
    parsed = urlsplit(config.public_login_url)
    magic_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/login", parsed.query, fragment)
    )
    expires = delivery.expires_at.astimezone()
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = delivery.email
    message["Subject"] = f"LAE {purpose_label}验证码"
    message["Date"] = format_datetime(datetime.now().astimezone())
    message["X-LAE-Challenge-ID"] = delivery.challenge_id
    message.set_content(
        "\n".join(
            (
                f"你的 LAE {purpose_label}验证码是：{delivery.code}",
                f"验证码将在 {expires:%Y-%m-%d %H:%M:%S %Z} 失效。",
                "也可以使用下面的一次性链接继续：",
                magic_url,
                "如果这不是你的操作，可以忽略本邮件。",
            )
        )
    )
    message.add_alternative(
        """<!doctype html><html><body style="background:#07110f;color:#dce6d8;font-family:Arial,sans-serif;padding:32px">
<div style="max-width:560px;margin:auto;border:1px solid #24352d;border-radius:18px;padding:32px;background:#0c1814">
<p style="font-size:11px;letter-spacing:.18em;color:#7d9185">LUMA APPLICATION ENGINE</p>
<h1 style="font-size:28px;font-weight:500">{purpose} LAE</h1>
<p style="color:#8fa097">你的六位验证码</p>
<p style="font-size:38px;letter-spacing:.22em;margin:20px 0;color:#edf4e9">{code}</p>
<p style="color:#788b7f">验证码将在 {expires} 失效。</p>
<p><a href="{url}" style="display:inline-block;margin-top:16px;padding:14px 20px;border-radius:12px;background:#dbe7d4;color:#102018;text-decoration:none">继续{purpose}</a></p>
<p style="margin-top:28px;font-size:12px;color:#607168">如果这不是你的操作，可以忽略本邮件。</p>
</div></body></html>""".format(
            purpose=html.escape(purpose_label),
            code=html.escape(delivery.code),
            expires=html.escape(f"{expires:%Y-%m-%d %H:%M:%S %Z}"),
            url=html.escape(magic_url, quote=True),
        ),
        subtype="html",
    )
    return message


@dataclass(slots=True)
class RecordingEmailSender:
    """Deterministic fake whose in-memory mailbox is explicit test state."""

    deliveries: list[EmailChallengeDelivery] = field(default_factory=list)
    fail_next: bool = False

    async def send_auth_challenge(self, delivery: EmailChallengeDelivery) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("recording email sender failure")
        self.deliveries.append(delivery)


class ConsoleEmailSender:
    """Safe development placeholder.

    It intentionally does not print the code or magic token. Local interactive
    development should inject RecordingEmailSender or a real provider sandbox;
    credentials must never become process logs.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("lae.email")

    async def send_auth_challenge(self, delivery: EmailChallengeDelivery) -> None:
        self._logger.info(
            "development auth email accepted",
            extra={
                "challenge_id": delivery.challenge_id,
                "purpose": delivery.purpose,
                "expires_at": delivery.expires_at.isoformat(),
            },
        )
