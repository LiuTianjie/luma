from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from lae_store.auth import (
    AuthBackend,
    AuthCompletion,
    AuthRejected,
    DeployTokenPrincipal,
    DeployTokenRecord,
    IssuedManagedDeployToken,
    SessionPrincipal,
    normalize_email,
)

from .email import EmailChallengeDelivery, EmailSender


class CsrfRejected(AuthRejected):
    pass


@dataclass(frozen=True, slots=True)
class StartAccepted:
    accepted: bool = True


class AuthService:
    def __init__(
        self,
        backend: AuthBackend,
        email_sender: EmailSender,
        *,
        logger: logging.Logger | None = None,
        minimum_start_duration: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 0 <= minimum_start_duration <= 5:
            raise ValueError("minimum_start_duration must be between 0 and 5 seconds")
        self._backend = backend
        self._email = email_sender
        self._logger = logger or logging.getLogger("lae.auth")
        self._minimum_start_duration = minimum_start_duration
        self._monotonic = monotonic
        self._sleeper = sleeper

    async def start(
        self,
        *,
        email: str,
        purpose: str,
        request_ip: str | None,
        device_id: str | None,
    ) -> StartAccepted:
        """Start an email flow with an intentionally invariant public result."""

        started_at = self._monotonic()
        try:
            return await self._start(
                email=email,
                purpose=purpose,
                request_ip=request_ip,
                device_id=device_id,
            )
        finally:
            remaining = self._minimum_start_duration - (
                self._monotonic() - started_at
            )
            if remaining > 0:
                await self._sleeper(remaining)

    async def _start(
        self,
        *,
        email: str,
        purpose: str,
        request_ip: str | None,
        device_id: str | None,
    ) -> StartAccepted:

        try:
            normalized = normalize_email(email)
        except (TypeError, ValueError):
            return StartAccepted()

        try:
            challenge = await self._backend.begin_challenge(
                email=normalized,
                purpose=purpose,
                request_ip=request_ip,
                device_id=device_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.error("auth challenge start unavailable")
            return StartAccepted()
        if challenge is None:
            return StartAccepted()

        delivery = EmailChallengeDelivery(
            challenge_id=challenge.id,
            email=challenge.email,
            purpose=challenge.purpose,
            code=challenge.code,
            magic_token=challenge.magic_token,
            expires_at=challenge.expires_at,
        )
        try:
            await self._email.send_auth_challenge(delivery)
        except asyncio.CancelledError:
            await self._best_effort_cancel(challenge.id)
            raise
        except Exception:
            await self._best_effort_cancel(challenge.id)
            self._logger.error(
                "auth email delivery failed",
                extra={"challenge_id": challenge.id, "purpose": challenge.purpose},
            )
            return StartAccepted()

        try:
            activated = await self._backend.activate_challenge(challenge.id)
        except asyncio.CancelledError:
            await self._best_effort_cancel(challenge.id)
            raise
        except Exception:
            activated = False
        if not activated:
            await self._best_effort_cancel(challenge.id)
        return StartAccepted()

    async def _best_effort_cancel(self, challenge_id: str) -> None:
        try:
            await self._backend.cancel_challenge(challenge_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A pending challenge has no activated_at and is unusable even if
            # this cleanup fails. Do not log any delivery credential.
            self._logger.error(
                "auth challenge cleanup failed", extra={"challenge_id": challenge_id}
            )

    async def verify(
        self,
        *,
        email: str,
        purpose: str,
        code: str | None,
        magic_token: str | None,
        request_ip: str | None,
        user_agent: str | None,
    ) -> AuthCompletion:
        try:
            normalized = normalize_email(email)
        except (TypeError, ValueError) as exc:
            raise AuthRejected("authentication failed") from exc
        if (code is None) == (magic_token is None):
            raise AuthRejected("authentication failed")
        return await self._backend.complete_challenge(
            email=normalized,
            purpose=purpose,
            method="code" if code is not None else "magic",
            credential=code if code is not None else str(magic_token),
            request_ip=request_ip,
            user_agent=user_agent,
        )

    async def authenticate(self, session_token: str | None) -> SessionPrincipal:
        if not session_token:
            raise AuthRejected("authentication failed")
        return await self._backend.authenticate(session_token)

    async def authenticate_deploy_token(
        self, token: str | None, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        if not token:
            raise AuthRejected("authentication failed")
        return await self._backend.authenticate_deploy_token(
            token, request_ip=request_ip
        )

    def csrf_valid(
        self,
        principal: SessionPrincipal,
        *,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> bool:
        return bool(
            csrf_cookie
            and csrf_header
            and hmac.compare_digest(csrf_cookie, csrf_header)
            and self._backend.csrf_matches(principal, csrf_header)
        )

    async def list_deploy_tokens(
        self, principal: SessionPrincipal
    ) -> tuple[DeployTokenRecord, ...]:
        return await self._backend.list_deploy_tokens(principal)

    async def create_deploy_token(
        self,
        principal: SessionPrincipal,
        *,
        name: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
    ) -> IssuedManagedDeployToken:
        return await self._backend.create_deploy_token(
            principal,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )

    async def rotate_deploy_token(
        self, principal: SessionPrincipal, token_id: str
    ) -> IssuedManagedDeployToken:
        return await self._backend.rotate_deploy_token(principal, token_id)

    async def revoke_deploy_token(
        self, principal: SessionPrincipal, token_id: str
    ) -> None:
        await self._backend.revoke_deploy_token(principal, token_id)

    async def logout(
        self,
        *,
        session_token: str | None,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> None:
        principal = await self.authenticate(session_token)
        if not self.csrf_valid(
            principal,
            csrf_cookie=csrf_cookie,
            csrf_header=csrf_header,
        ):
            raise CsrfRejected("csrf validation failed")
        await self._backend.revoke_session(principal.session_id)
