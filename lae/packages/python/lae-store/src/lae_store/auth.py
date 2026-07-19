from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, TypeAlias

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .ids import new_id
from .models import (
    AuthSession,
    DeployToken,
    EmailChallenge,
    PlanVersion,
    Subscription,
    Tenant,
    TenantMember,
    User,
)
from .tokens import (
    issue_deploy_token,
    issue_email_challenge,
    issue_session_credentials,
    keyed_secret_hash,
    parse_deploy_token,
    parse_magic_token,
    parse_session_token,
    session_token_digest,
    verify_csrf_token,
    verify_deploy_token,
    verify_email_challenge,
)

_EMAIL_LOCAL = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
_EMAIL_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+$"
)
_CREDENTIAL_LIKE = re.compile(
    r"(?:lae_(?:dt|ss_v[0-9]+|cs|em)_[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~-]{8,})",
    re.IGNORECASE,
)

SUPPORTED_DEPLOY_TOKEN_SCOPES = frozenset(
    {
        "analyses:write",
        "apps:read",
        "apps:write",
        "billing:checkout",
        "deployments:write",
        "logs:read",
        "sources:write",
    }
)

DEFAULT_DEPLOY_TOKEN_SCOPES = (
    "apps:read",
    "apps:write",
    "sources:write",
    "analyses:write",
    "deployments:write",
    "logs:read",
)

MAX_ACTIVE_DEPLOY_TOKENS = 50


class AuthRejected(Exception):
    """Publicly indistinguishable authentication failure."""


class AuthConfigurationError(RuntimeError):
    """Server-side identity configuration or invariant is missing."""


class DeployTokenNotFound(Exception):
    """A tenant-scoped token management target does not exist."""


class DeployTokenConflict(Exception):
    """A token management mutation conflicts with current token state."""

    def __init__(self, reason: Literal["default_protected", "inactive", "limit"]):
        super().__init__(reason)
        self.reason = reason


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str) -> str:
    """Return the canonical V1 ASCII mailbox identity.

    V1 deliberately rejects internationalized local parts. Supporting those
    safely requires an explicit normalization and delivery policy rather than
    silently treating visually different addresses as one identity.
    """

    if not isinstance(value, str):
        raise ValueError("email must be a string")
    normalized = value.strip().lower()
    if len(normalized) > 320 or not normalized.isascii() or normalized.count("@") != 1:
        raise ValueError("email has invalid format")
    local, domain = normalized.rsplit("@", 1)
    if (
        not _EMAIL_LOCAL.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _EMAIL_DOMAIN.fullmatch(domain)
    ):
        raise ValueError("email has invalid format")
    return normalized


def safe_user_agent(value: str | None) -> str | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(
            (ord(char) < 0x20 and char not in "\t") or ord(char) == 0x7F
            for char in value
        )
        or _CREDENTIAL_LIKE.search(value) is not None
    ):
        return None
    return value


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    challenge_ttl: timedelta = timedelta(minutes=10)
    challenge_attempts: int = 5
    email_starts_per_window: int = 5
    ip_starts_per_window: int = 30
    device_starts_per_window: int = 10
    start_window: timedelta = timedelta(hours=1)
    resend_interval: timedelta = timedelta(seconds=60)
    session_ttl: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        if not timedelta(minutes=2) <= self.challenge_ttl <= timedelta(minutes=30):
            raise ValueError("challenge_ttl must be between 2 and 30 minutes")
        if not 3 <= self.challenge_attempts <= 10:
            raise ValueError("challenge_attempts must be between 3 and 10")
        if not 1 <= self.email_starts_per_window <= 100:
            raise ValueError("email rate limit is invalid")
        if not 1 <= self.ip_starts_per_window <= 10_000:
            raise ValueError("IP rate limit is invalid")
        if not 1 <= self.device_starts_per_window <= 1_000:
            raise ValueError("device rate limit is invalid")
        if not timedelta(minutes=1) <= self.start_window <= timedelta(days=1):
            raise ValueError("start_window is invalid")
        if not timedelta(seconds=10) <= self.resend_interval <= self.challenge_ttl:
            raise ValueError("resend_interval is invalid")
        if not timedelta(hours=1) <= self.session_ttl <= timedelta(days=90):
            raise ValueError("session_ttl is invalid")


@dataclass(frozen=True, slots=True)
class AuthKeyRing:
    current_version: int
    keys: Mapping[int, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        copied = dict(self.keys)
        if self.current_version < 1 or self.current_version not in copied:
            raise ValueError("current HMAC key version is missing")
        if not 1 <= len(copied) <= 8:
            raise ValueError("HMAC key ring must contain 1-8 versions")
        for version, key in copied.items():
            if not isinstance(version, int) or version < 1:
                raise ValueError("HMAC key versions must be positive integers")
            if not isinstance(key, bytes) or len(key) < 32:
                raise ValueError("each HMAC key must contain at least 256 bits")
        object.__setattr__(self, "keys", MappingProxyType(copied))

    @property
    def current_key(self) -> bytes:
        return self.keys[self.current_version]

    def key(self, version: int) -> bytes:
        try:
            return self.keys[version]
        except KeyError as exc:
            raise AuthRejected("authentication failed") from exc


@dataclass(frozen=True, slots=True)
class PendingEmailChallenge:
    id: str
    email: str
    purpose: str
    code: str = field(repr=False)
    magic_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthCompletion:
    user_id: str
    email: str
    tenant_id: str
    session_id: str
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    session_expires_at: datetime
    entitlement_code: str
    default_deploy_token: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    session_id: str
    user_id: str
    email: str
    tenant_id: str
    entitlement_code: str
    key_version: int
    csrf_digest: bytes | None = field(repr=False)

    @property
    def credential_type(self) -> Literal["session"]:
        return "session"

    @property
    def credential_id(self) -> str:
        return self.session_id

    @property
    def scopes(self) -> frozenset[str]:
        # Browser sessions represent the user's interactive authority. Token
        # management and administrative billing changes remain session-only;
        # deploy tokens may only start checkout when explicitly scoped.
        return SUPPORTED_DEPLOY_TOKEN_SCOPES


@dataclass(frozen=True, slots=True)
class DeployTokenPrincipal:
    token_id: str
    token_prefix: str
    user_id: str
    email: str
    tenant_id: str
    entitlement_code: str
    member_role: str
    scopes: frozenset[str]

    @property
    def credential_type(self) -> Literal["deploy_token"]:
        return "deploy_token"

    @property
    def credential_id(self) -> str:
        return self.token_id


AuthenticatedPrincipal: TypeAlias = SessionPrincipal | DeployTokenPrincipal


@dataclass(frozen=True, slots=True)
class DeployTokenRecord:
    id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    purpose: str
    is_default: bool
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedManagedDeployToken:
    record: DeployTokenRecord
    plaintext: str = field(repr=False)


class AuthBackend(Protocol):
    async def begin_challenge(
        self,
        *,
        email: str,
        purpose: str,
        request_ip: str | None,
        device_id: str | None,
    ) -> PendingEmailChallenge | None: ...

    async def activate_challenge(self, challenge_id: str) -> bool: ...

    async def cancel_challenge(self, challenge_id: str) -> None: ...

    async def complete_challenge(
        self,
        *,
        email: str,
        purpose: str,
        method: str,
        credential: str,
        request_ip: str | None,
        user_agent: str | None,
    ) -> AuthCompletion: ...

    async def authenticate(self, session_token: str) -> SessionPrincipal: ...

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal: ...

    async def list_deploy_tokens(
        self, principal: SessionPrincipal
    ) -> tuple[DeployTokenRecord, ...]: ...

    async def create_deploy_token(
        self,
        principal: SessionPrincipal,
        *,
        name: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
    ) -> IssuedManagedDeployToken: ...

    async def rotate_deploy_token(
        self, principal: SessionPrincipal, token_id: str
    ) -> IssuedManagedDeployToken: ...

    async def revoke_deploy_token(
        self, principal: SessionPrincipal, token_id: str
    ) -> None: ...

    async def revoke_session(self, session_id: str) -> None: ...

    def csrf_matches(self, principal: SessionPrincipal, csrf_token: str) -> bool: ...


class PostgresAuthStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        key_ring: AuthKeyRing,
        *,
        policy: AuthPolicy | None = None,
    ) -> None:
        self._sessions = session_factory
        self._keys = key_ring
        self._policy = policy or AuthPolicy()

    @staticmethod
    def _purpose(value: str) -> str:
        if value not in {"register", "login", "auto"}:
            raise ValueError(
                "authentication purpose must be register, login, or auto"
            )
        return value

    def _request_ip(self, value: str | None) -> tuple[str | None, bytes | None]:
        if value is None:
            return None, None
        try:
            canonical = str(ipaddress.ip_address(value))
        except ValueError:
            return None, None
        digest = keyed_secret_hash(
            canonical, self._keys.current_key, domain="lae.auth-rate-ip.v1"
        )
        return canonical, digest

    def _device_hash(self, value: str | None) -> bytes | None:
        if value is None or not isinstance(value, str) or not 1 <= len(value) <= 128:
            return None
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            return None
        return keyed_secret_hash(
            value, self._keys.current_key, domain="lae.auth-rate-device.v1"
        )

    @staticmethod
    def _canonical_ip(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(ipaddress.ip_address(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _deploy_token_name(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("deploy token name must be a string")
        normalized = value.strip()
        if (
            not 1 <= len(normalized) <= 120
            or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in normalized
            )
            or _CREDENTIAL_LIKE.search(normalized) is not None
        ):
            raise ValueError("deploy token name is invalid")
        return normalized

    @staticmethod
    def _deploy_token_scopes(values: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(values, tuple) or not values:
            raise ValueError("at least one deploy token scope is required")
        if any(
            not isinstance(value, str) or value not in SUPPORTED_DEPLOY_TOKEN_SCOPES
            for value in values
        ):
            raise ValueError("deploy token scope is invalid")
        if len(values) != len(set(values)):
            raise ValueError("deploy token scopes must be unique")
        return tuple(sorted(values))

    @staticmethod
    def _deploy_token_expiry(
        value: datetime | None, *, now: datetime
    ) -> datetime | None:
        if value is None:
            return None
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError("deploy token expiry must include a timezone")
        normalized = value.astimezone(timezone.utc)
        if not now + timedelta(minutes=1) <= normalized <= now + timedelta(days=366):
            raise ValueError("deploy token expiry is outside the allowed range")
        return normalized

    @staticmethod
    def _deploy_token_record(token: DeployToken) -> DeployTokenRecord:
        return DeployTokenRecord(
            id=token.id,
            name=token.name,
            prefix=token.prefix,
            scopes=tuple(sorted(str(scope) for scope in token.scopes)),
            purpose=token.purpose,
            is_default=token.is_default,
            expires_at=token.expires_at,
            revoked_at=token.revoked_at,
            last_used_at=token.last_used_at,
            last_used_ip=str(token.last_used_ip) if token.last_used_ip is not None else None,
            created_at=token.created_at,
        )

    async def begin_challenge(
        self,
        *,
        email: str,
        purpose: str,
        request_ip: str | None,
        device_id: str | None,
    ) -> PendingEmailChallenge | None:
        email = normalize_email(email)
        purpose = self._purpose(purpose)
        now = utcnow()
        _canonical_ip, request_ip_hash = self._request_ip(request_ip)
        device_hash = self._device_hash(device_id)
        cutoff = now - self._policy.start_window

        async with self._sessions() as session:
            async with session.begin():
                rate_identities = [f"email:{email}"]
                if request_ip_hash is not None:
                    rate_identities.append(f"ip:{request_ip_hash.hex()}")
                if device_hash is not None:
                    rate_identities.append(f"device:{device_hash.hex()}")
                for identity in sorted(rate_identities):
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"
                        ),
                        {"identity": identity},
                    )
                existing_user = await session.scalar(
                    select(User).where(func.lower(User.email) == email)
                )
                if purpose == "auto":
                    purpose = "login" if existing_user is not None else "register"
                eligible = (
                    existing_user is None
                    if purpose == "register"
                    else existing_user is not None
                    and existing_user.status == "active"
                    and existing_user.deleted_at is None
                )

                email_count = int(
                    await session.scalar(
                        select(func.count(EmailChallenge.id)).where(
                            EmailChallenge.email == email,
                            EmailChallenge.created_at >= cutoff,
                        )
                    )
                    or 0
                )
                last_created = await session.scalar(
                    select(EmailChallenge.created_at)
                    .where(EmailChallenge.email == email)
                    .order_by(EmailChallenge.created_at.desc())
                    .limit(1)
                )
                ip_count = 0
                if request_ip_hash is not None:
                    ip_count = int(
                        await session.scalar(
                            select(func.count(EmailChallenge.id)).where(
                                EmailChallenge.request_ip_hash == request_ip_hash,
                                EmailChallenge.created_at >= cutoff,
                            )
                        )
                        or 0
                    )

                device_count = 0
                if device_hash is not None:
                    device_count = int(
                        await session.scalar(
                            select(func.count(EmailChallenge.id)).where(
                                EmailChallenge.device_hash == device_hash,
                                EmailChallenge.created_at >= cutoff,
                            )
                        )
                        or 0
                    )

                limited = (
                    email_count >= self._policy.email_starts_per_window
                    or ip_count >= self._policy.ip_starts_per_window
                    or device_count >= self._policy.device_starts_per_window
                    or last_created is not None
                    and last_created > now - self._policy.resend_interval
                )
                if not eligible or limited:
                    # Keep the ineligible path on a keyed-hash code path without
                    # creating a deliverable challenge or exposing the reason.
                    keyed_secret_hash(
                        email,
                        self._keys.current_key,
                        domain="lae.auth-ineligible.v1",
                    )
                    return None

                await session.execute(
                    update(EmailChallenge)
                    .where(
                        EmailChallenge.email == email,
                        EmailChallenge.purpose == purpose,
                        EmailChallenge.used_at.is_(None),
                        EmailChallenge.canceled_at.is_(None),
                    )
                    .values(canceled_at=now, updated_at=now)
                )

                challenge_id = new_id("emc")
                issued = issue_email_challenge(
                    self._keys.current_key,
                    key_version=self._keys.current_version,
                    challenge_id=challenge_id,
                )
                expires_at = now + self._policy.challenge_ttl
                session.add(
                    EmailChallenge(
                        id=challenge_id,
                        email=email,
                        purpose=purpose,
                        code_hash=issued.code_digest,
                        magic_token_hash=issued.magic_token_digest,
                        key_version=issued.key_version,
                        attempts=0,
                        max_attempts=self._policy.challenge_attempts,
                        expires_at=expires_at,
                        request_ip_hash=request_ip_hash,
                        device_hash=device_hash,
                    )
                )

        return PendingEmailChallenge(
            id=challenge_id,
            email=email,
            purpose=purpose,
            code=issued.code,
            magic_token=issued.magic_token,
            expires_at=expires_at,
        )

    async def activate_challenge(self, challenge_id: str) -> bool:
        now = utcnow()
        async with self._sessions() as session:
            async with session.begin():
                result = await session.execute(
                    update(EmailChallenge)
                    .where(
                        EmailChallenge.id == challenge_id,
                        EmailChallenge.activated_at.is_(None),
                        EmailChallenge.used_at.is_(None),
                        EmailChallenge.canceled_at.is_(None),
                        EmailChallenge.expires_at > now,
                    )
                    .values(activated_at=now, updated_at=now)
                )
                return result.rowcount == 1

    async def cancel_challenge(self, challenge_id: str) -> None:
        now = utcnow()
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    update(EmailChallenge)
                    .where(
                        EmailChallenge.id == challenge_id,
                        EmailChallenge.used_at.is_(None),
                        EmailChallenge.canceled_at.is_(None),
                    )
                    .values(canceled_at=now, updated_at=now)
                )

    async def complete_challenge(
        self,
        *,
        email: str,
        purpose: str,
        method: str,
        credential: str,
        request_ip: str | None,
        user_agent: str | None,
    ) -> AuthCompletion:
        email = normalize_email(email)
        purpose = self._purpose(purpose)
        if method not in {"code", "magic"}:
            raise AuthRejected("authentication failed")
        now = utcnow()
        canonical_ip, _request_ip_hash = self._request_ip(request_ip)
        sanitized_user_agent = safe_user_agent(user_agent)
        rejected = False
        completion: AuthCompletion | None = None

        async with self._sessions() as session:
            async with session.begin():
                filters = [
                    EmailChallenge.email == email,
                    EmailChallenge.activated_at.is_not(None),
                    EmailChallenge.used_at.is_(None),
                    EmailChallenge.canceled_at.is_(None),
                ]
                if purpose != "auto":
                    filters.append(EmailChallenge.purpose == purpose)
                if method == "magic":
                    try:
                        filters.append(EmailChallenge.id == parse_magic_token(credential))
                    except (TypeError, ValueError):
                        filters.append(EmailChallenge.id == "emc_invalid")
                challenge_query = select(EmailChallenge).where(*filters)
                if method == "code":
                    challenge_query = challenge_query.order_by(
                        EmailChallenge.created_at.desc()
                    ).limit(1)
                challenge = await session.scalar(challenge_query.with_for_update())
                if (
                    challenge is None
                    or challenge.expires_at <= now
                    or challenge.attempts >= challenge.max_attempts
                ):
                    rejected = True
                else:
                    key = self._keys.key(challenge.key_version)
                    expected = (
                        challenge.code_hash
                        if method == "code"
                        else challenge.magic_token_hash
                    )
                    valid = verify_email_challenge(
                        credential,
                        method=method,
                        challenge_id=challenge.id,
                        expected_digest=expected,
                        key=key,
                    )
                    if not valid:
                        challenge.attempts += 1
                        challenge.updated_at = now
                        if challenge.attempts >= challenge.max_attempts:
                            challenge.canceled_at = now
                        rejected = True
                    else:
                        challenge.used_at = now
                        challenge.updated_at = now
                        completion = await self._complete_identity(
                            session,
                            email=email,
                            purpose=challenge.purpose,
                            now=now,
                            request_ip=canonical_ip,
                            user_agent=sanitized_user_agent,
                        )

        if rejected or completion is None:
            raise AuthRejected("authentication failed")
        return completion

    async def _complete_identity(
        self,
        session: AsyncSession,
        *,
        email: str,
        purpose: str,
        now: datetime,
        request_ip: str | None,
        user_agent: str | None,
    ) -> AuthCompletion:
        # Serialize registration for one normalized mailbox, including the
        # no-row-yet case that SELECT FOR UPDATE cannot otherwise lock.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:email, 0))"),
            {"email": email},
        )
        user = await session.scalar(
            select(User).where(func.lower(User.email) == email).with_for_update()
        )
        if user is None:
            if purpose != "register":
                raise AuthRejected("authentication failed")
            user = User(
                id=new_id("usr"),
                email=email,
                status="active",
                email_verified_at=now,
                last_login_at=now,
            )
            session.add(user)
            await session.flush()
        elif user.status != "active" or user.deleted_at is not None:
            raise AuthRejected("authentication failed")
        else:
            user.email_verified_at = user.email_verified_at or now
            user.last_login_at = now
            user.updated_at = now

        tenant = await session.scalar(
            select(Tenant)
            .where(
                Tenant.owner_user_id == user.id,
                Tenant.type == "personal",
                Tenant.status == "active",
                Tenant.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if tenant is None:
            if purpose != "register":
                raise AuthConfigurationError("active user has no personal tenant")
            tenant_id = new_id("ten")
            tenant = Tenant(
                id=tenant_id,
                type="personal",
                name="Personal workspace",
                slug=f"personal-{tenant_id.removeprefix('ten_').lower()}",
                status="active",
                owner_user_id=user.id,
            )
            session.add(tenant)
            await session.flush()
            session.add(
                TenantMember(tenant_id=tenant.id, user_id=user.id, role="owner")
            )
            await session.flush()

        subscription = await session.scalar(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant.id,
                Subscription.status.in_(("active", "trialing", "past_due")),
            )
            .with_for_update()
        )
        if subscription is None:
            if purpose != "register":
                raise AuthConfigurationError("active tenant has no entitlement")
            lite_plan = await session.scalar(
                select(PlanVersion)
                .where(PlanVersion.code == "lite", PlanVersion.effective_at <= now)
                .order_by(PlanVersion.version.desc())
                .limit(1)
            )
            if lite_plan is None:
                raise AuthConfigurationError("Lite plan version is not configured")
            subscription = Subscription(
                id=new_id("sub"),
                tenant_id=tenant.id,
                plan_version_id=lite_plan.id,
                interval="none",
                status="active",
                provider="system",
            )
            session.add(subscription)
            plan_code = lite_plan.code
        else:
            plan_code = await session.scalar(
                select(PlanVersion.code).where(
                    PlanVersion.id == subscription.plan_version_id
                )
            )
            if plan_code is None:
                raise AuthConfigurationError("entitlement references an unknown plan")

        default_plaintext: str | None = None
        active_default = await session.scalar(
            select(DeployToken.id).where(
                DeployToken.tenant_id == tenant.id,
                DeployToken.user_id == user.id,
                DeployToken.is_default.is_(True),
                DeployToken.revoked_at.is_(None),
            )
        )
        if active_default is None and purpose == "register":
            issued_deploy = issue_deploy_token(
                self._keys.current_key, key_version=self._keys.current_version
            )
            session.add(
                DeployToken(
                    id=new_id("dtk"),
                    tenant_id=tenant.id,
                    user_id=user.id,
                    name="Default deploy token",
                    prefix=issued_deploy.prefix,
                    token_hash=issued_deploy.digest,
                    key_version=issued_deploy.key_version,
                    scopes=list(DEFAULT_DEPLOY_TOKEN_SCOPES),
                    purpose="deploy",
                    is_default=True,
                )
            )
            default_plaintext = issued_deploy.plaintext

        issued_session = issue_session_credentials(
            self._keys.current_key, key_version=self._keys.current_version
        )
        session_id = new_id("ses")
        expires_at = now + self._policy.session_ttl
        session.add(
            AuthSession(
                id=session_id,
                user_id=user.id,
                session_hash=issued_session.session_digest,
                key_version=issued_session.key_version,
                csrf_hash=issued_session.csrf_digest,
                expires_at=expires_at,
                last_seen_at=now,
                ip=request_ip,
                user_agent=user_agent,
            )
        )

        return AuthCompletion(
            user_id=user.id,
            email=user.email,
            tenant_id=tenant.id,
            session_id=session_id,
            session_token=issued_session.session_token,
            csrf_token=issued_session.csrf_token,
            session_expires_at=expires_at,
            entitlement_code=str(plan_code),
            default_deploy_token=default_plaintext,
        )

    async def authenticate(self, session_token: str) -> SessionPrincipal:
        try:
            version = parse_session_token(session_token)
            key = self._keys.key(version)
            _parsed_version, digest = session_token_digest(session_token, key)
        except (TypeError, ValueError, AuthRejected) as exc:
            raise AuthRejected("authentication failed") from exc
        now = utcnow()
        async with self._sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AuthSession, User)
                        .join(User, User.id == AuthSession.user_id)
                        .where(
                            AuthSession.session_hash == digest,
                            AuthSession.key_version == version,
                            AuthSession.revoked_at.is_(None),
                            AuthSession.expires_at > now,
                            User.status == "active",
                            User.deleted_at.is_(None),
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise AuthRejected("authentication failed")
                auth_session, user = row
                tenant = await session.scalar(
                    select(Tenant).where(
                        Tenant.owner_user_id == user.id,
                        Tenant.type == "personal",
                        Tenant.status == "active",
                        Tenant.deleted_at.is_(None),
                    )
                )
                if tenant is None:
                    raise AuthRejected("authentication failed")
                plan_code = await session.scalar(
                    select(PlanVersion.code)
                    .join(Subscription, Subscription.plan_version_id == PlanVersion.id)
                    .where(
                        Subscription.tenant_id == tenant.id,
                        Subscription.status.in_(("active", "trialing", "past_due")),
                    )
                )
                if plan_code is None:
                    raise AuthRejected("authentication failed")
                auth_session.last_seen_at = now
                auth_session.updated_at = now
                return SessionPrincipal(
                    session_id=auth_session.id,
                    user_id=user.id,
                    email=user.email,
                    tenant_id=tenant.id,
                    entitlement_code=str(plan_code),
                    key_version=auth_session.key_version,
                    csrf_digest=auth_session.csrf_hash,
                )

    async def authenticate_deploy_token(
        self, token: str, *, request_ip: str | None
    ) -> DeployTokenPrincipal:
        try:
            prefix, _secret = parse_deploy_token(token)
        except (TypeError, ValueError) as exc:
            # Keep malformed input on a keyed-HMAC/constant-time compare path
            # without retaining or reporting any caller-provided credential.
            verify_deploy_token(
                "invalid",
                expected_digest=b"\0" * 32,
                key=self._keys.current_key,
            )
            raise AuthRejected("authentication failed") from exc

        now = utcnow()
        canonical_ip = self._canonical_ip(request_ip)
        async with self._sessions() as session:
            async with session.begin():
                candidate = await session.scalar(
                    select(DeployToken).where(DeployToken.prefix == prefix)
                )
                if candidate is None:
                    verify_deploy_token(
                        token,
                        expected_digest=b"\0" * 32,
                        key=self._keys.current_key,
                    )
                    raise AuthRejected("authentication failed")
                try:
                    key = self._keys.key(candidate.key_version)
                except AuthRejected as exc:
                    verify_deploy_token(
                        token,
                        expected_digest=b"\0" * 32,
                        key=self._keys.current_key,
                    )
                    raise AuthRejected("authentication failed") from exc
                verified = verify_deploy_token(
                    token,
                    expected_digest=candidate.token_hash,
                    key=key,
                )
                if (
                    not verified
                    or candidate.revoked_at is not None
                    or candidate.expires_at is not None
                    and candidate.expires_at <= now
                ):
                    raise AuthRejected("authentication failed")

                identity = (
                    await session.execute(
                        select(User, Tenant, TenantMember, PlanVersion.code)
                        .join(TenantMember, TenantMember.user_id == User.id)
                        .join(
                            Tenant,
                            and_(
                                Tenant.id == TenantMember.tenant_id,
                                Tenant.id == candidate.tenant_id,
                            ),
                        )
                        .join(
                            Subscription,
                            and_(
                                Subscription.tenant_id == Tenant.id,
                                Subscription.status.in_(
                                    ("active", "trialing", "past_due")
                                ),
                            ),
                        )
                        .join(
                            PlanVersion,
                            PlanVersion.id == Subscription.plan_version_id,
                        )
                        .where(
                            User.id == candidate.user_id,
                            User.status == "active",
                            User.deleted_at.is_(None),
                            Tenant.status == "active",
                            Tenant.deleted_at.is_(None),
                        )
                    )
                ).one_or_none()
                if identity is None:
                    raise AuthRejected("authentication failed")
                user, tenant, member, entitlement_code = identity
                if member.tenant_id != candidate.tenant_id:
                    raise AuthRejected("authentication failed")
                raw_scopes = candidate.scopes
                if (
                    not isinstance(raw_scopes, list)
                    or not raw_scopes
                    or any(
                        not isinstance(scope, str)
                        or scope not in SUPPORTED_DEPLOY_TOKEN_SCOPES
                        for scope in raw_scopes
                    )
                    or len(raw_scopes) != len(set(raw_scopes))
                ):
                    raise AuthRejected("authentication failed")

                candidate.last_used_at = now
                if canonical_ip is not None:
                    candidate.last_used_ip = canonical_ip
                candidate.updated_at = now
                return DeployTokenPrincipal(
                    token_id=candidate.id,
                    token_prefix=candidate.prefix,
                    user_id=user.id,
                    email=user.email,
                    tenant_id=tenant.id,
                    entitlement_code=str(entitlement_code),
                    member_role=member.role,
                    scopes=frozenset(raw_scopes),
                )

    async def list_deploy_tokens(
        self, principal: SessionPrincipal
    ) -> tuple[DeployTokenRecord, ...]:
        async with self._sessions() as session:
            tokens = (
                await session.scalars(
                    select(DeployToken)
                    .where(
                        DeployToken.tenant_id == principal.tenant_id,
                        DeployToken.user_id == principal.user_id,
                    )
                    .order_by(DeployToken.created_at.desc(), DeployToken.id.desc())
                    .limit(200)
                )
            ).all()
            return tuple(self._deploy_token_record(token) for token in tokens)

    async def create_deploy_token(
        self,
        principal: SessionPrincipal,
        *,
        name: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
    ) -> IssuedManagedDeployToken:
        normalized_name = self._deploy_token_name(name)
        normalized_scopes = self._deploy_token_scopes(scopes)
        now = utcnow()
        normalized_expiry = self._deploy_token_expiry(expires_at, now=now)
        issued = issue_deploy_token(
            self._keys.current_key,
            key_version=self._keys.current_version,
        )
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"
                    ),
                    {"identity": f"deploy-token:{principal.tenant_id}:{principal.user_id}"},
                )
                active_count = int(
                    await session.scalar(
                        select(func.count(DeployToken.id)).where(
                            DeployToken.tenant_id == principal.tenant_id,
                            DeployToken.user_id == principal.user_id,
                            DeployToken.revoked_at.is_(None),
                        )
                    )
                    or 0
                )
                if active_count >= MAX_ACTIVE_DEPLOY_TOKENS:
                    raise DeployTokenConflict("limit")
                token = DeployToken(
                    id=new_id("dtk"),
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    name=normalized_name,
                    prefix=issued.prefix,
                    token_hash=issued.digest,
                    key_version=issued.key_version,
                    scopes=list(normalized_scopes),
                    purpose="deploy",
                    is_default=False,
                    expires_at=normalized_expiry,
                )
                session.add(token)
                await session.flush()
                record = self._deploy_token_record(token)
        return IssuedManagedDeployToken(record=record, plaintext=issued.plaintext)

    async def rotate_deploy_token(
        self, principal: SessionPrincipal, token_id: str
    ) -> IssuedManagedDeployToken:
        now = utcnow()
        issued = issue_deploy_token(
            self._keys.current_key,
            key_version=self._keys.current_version,
        )
        async with self._sessions() as session:
            async with session.begin():
                old = await session.scalar(
                    select(DeployToken)
                    .where(
                        DeployToken.id == token_id,
                        DeployToken.tenant_id == principal.tenant_id,
                        DeployToken.user_id == principal.user_id,
                    )
                    .with_for_update()
                )
                if old is None:
                    raise DeployTokenNotFound()
                if (
                    old.revoked_at is not None
                    or old.expires_at is not None
                    and old.expires_at <= now
                ):
                    raise DeployTokenConflict("inactive")
                normalized_scopes = self._deploy_token_scopes(tuple(old.scopes))
                old.revoked_at = now
                old.updated_at = now
                # Flush the old default revocation before inserting its
                # replacement so the partial unique constraint cannot observe
                # two active defaults in one transaction.
                await session.flush()
                replacement = DeployToken(
                    id=new_id("dtk"),
                    tenant_id=old.tenant_id,
                    user_id=old.user_id,
                    name=old.name,
                    prefix=issued.prefix,
                    token_hash=issued.digest,
                    key_version=issued.key_version,
                    scopes=list(normalized_scopes),
                    purpose=old.purpose,
                    is_default=old.is_default,
                    expires_at=old.expires_at,
                )
                session.add(replacement)
                await session.flush()
                record = self._deploy_token_record(replacement)
        return IssuedManagedDeployToken(record=record, plaintext=issued.plaintext)

    async def revoke_deploy_token(
        self, principal: SessionPrincipal, token_id: str
    ) -> None:
        now = utcnow()
        async with self._sessions() as session:
            async with session.begin():
                token = await session.scalar(
                    select(DeployToken)
                    .where(
                        DeployToken.id == token_id,
                        DeployToken.tenant_id == principal.tenant_id,
                        DeployToken.user_id == principal.user_id,
                    )
                    .with_for_update()
                )
                if token is None:
                    raise DeployTokenNotFound()
                if token.revoked_at is not None:
                    return
                if token.is_default:
                    raise DeployTokenConflict("default_protected")
                token.revoked_at = now
                token.updated_at = now

    def csrf_matches(self, principal: SessionPrincipal, csrf_token: str) -> bool:
        if principal.csrf_digest is None:
            return False
        try:
            key = self._keys.key(principal.key_version)
            return verify_csrf_token(
                csrf_token, expected_digest=principal.csrf_digest, key=key
            )
        except (TypeError, ValueError, AuthRejected):
            return False

    async def revoke_session(self, session_id: str) -> None:
        now = utcnow()
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    update(AuthSession)
                    .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
                    .values(revoked_at=now, updated_at=now)
                )
