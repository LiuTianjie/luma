from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field

_TOKEN_PREFIX = re.compile(r"^[0-9A-HJKMNP-TV-Z]{10}$")
_TOKEN = re.compile(
    r"^lae_dt_(?P<prefix>[0-9A-HJKMNP-TV-Z]{10})_(?P<secret>[A-Za-z0-9_-]{43})$"
)
_SESSION_TOKEN = re.compile(
    r"^lae_ss_v(?P<version>[1-9][0-9]{0,8})_(?P<secret>[A-Za-z0-9_-]{43})$"
)
_CSRF_TOKEN = re.compile(r"^lae_cs_(?P<secret>[A-Za-z0-9_-]{43})$")
_MAGIC_TOKEN = re.compile(
    r"^lae_em_(?P<challenge>emc_[0-9A-HJKMNP-TV-Z]{26})_(?P<secret>[A-Za-z0-9_-]{43})$"
)
_EMAIL_CODE = re.compile(r"^[0-9]{6}$")


@dataclass(frozen=True, slots=True)
class IssuedDeployToken:
    plaintext: str = field(repr=False)
    prefix: str
    digest: bytes
    key_version: int


@dataclass(frozen=True, slots=True)
class IssuedEmailChallenge:
    code: str = field(repr=False)
    magic_token: str = field(repr=False)
    code_digest: bytes
    magic_token_digest: bytes
    key_version: int


@dataclass(frozen=True, slots=True)
class IssuedSessionCredentials:
    session_token: str = field(repr=False)
    session_digest: bytes
    csrf_token: str = field(repr=False)
    csrf_digest: bytes
    key_version: int


def _require_hmac_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("HMAC key must contain at least 256 bits")
    return key


def keyed_secret_hash(secret: str | bytes, key: bytes, *, domain: str) -> bytes:
    """Hash an opaque secret with domain-separated keyed HMAC-SHA256."""

    _require_hmac_key(key)
    if not domain or len(domain) > 64 or not domain.isascii():
        raise ValueError("hash domain must be 1-64 ASCII characters")
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not raw:
        raise ValueError("secret must not be empty")
    return hmac.new(key, domain.encode("ascii") + b"\0" + raw, hashlib.sha256).digest()


def keyed_request_hash(payload: object, key: bytes) -> bytes:
    """Return a keyed digest of canonical JSON without retaining the request."""

    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("idempotency payload must be canonical JSON") from exc
    if len(canonical) > 2 * 1024 * 1024:
        raise ValueError("idempotency payload exceeds 2 MiB")
    return keyed_secret_hash(canonical, key, domain="lae.idempotency-request.v1")


def issue_deploy_token(key: bytes, *, key_version: int) -> IssuedDeployToken:
    _require_hmac_key(key)
    if key_version < 1:
        raise ValueError("key_version must be positive")
    prefix = "".join(
        secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(10)
    )
    secret = _opaque_secret()
    plaintext = f"lae_dt_{prefix}_{secret}"
    return IssuedDeployToken(
        plaintext=plaintext,
        prefix=prefix,
        digest=keyed_secret_hash(plaintext, key, domain="lae.deploy-token.v1"),
        key_version=key_version,
    )


def _opaque_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode(
        "ascii"
    )


def issue_email_challenge(
    key: bytes, *, key_version: int, challenge_id: str
) -> IssuedEmailChallenge:
    """Issue a six-digit code and magic token for one purpose-bound flow.

    The challenge id is used only for hash domain separation. It is safe public
    metadata, while the code and magic token must remain ephemeral plaintext.
    """

    _require_hmac_key(key)
    if key_version < 1:
        raise ValueError("key_version must be positive")
    if not isinstance(challenge_id, str) or not challenge_id.startswith("emc_"):
        raise ValueError("challenge_id must be an email challenge identifier")
    code = f"{secrets.randbelow(1_000_000):06d}"
    magic_token = f"lae_em_{challenge_id}_{_opaque_secret()}"
    return IssuedEmailChallenge(
        code=code,
        magic_token=magic_token,
        code_digest=keyed_secret_hash(
            code, key, domain=f"lae.email-code.v1:{challenge_id}"
        ),
        magic_token_digest=keyed_secret_hash(
            magic_token, key, domain=f"lae.email-magic.v1:{challenge_id}"
        ),
        key_version=key_version,
    )


def verify_email_challenge(
    value: str,
    *,
    method: str,
    challenge_id: str,
    expected_digest: bytes,
    key: bytes,
) -> bool:
    if method not in {"code", "magic"}:
        raise ValueError("email challenge method must be code or magic")
    valid_format = bool(
        _EMAIL_CODE.fullmatch(value)
        if method == "code" and isinstance(value, str)
        else _MAGIC_TOKEN.fullmatch(value)
        if method == "magic" and isinstance(value, str)
        else False
    )
    domain = (
        f"lae.email-code.v1:{challenge_id}"
        if method == "code"
        else f"lae.email-magic.v1:{challenge_id}"
    )
    candidate = value if valid_format else "invalid"
    actual = keyed_secret_hash(candidate, key, domain=domain)
    return valid_format and hmac.compare_digest(actual, expected_digest)


def parse_magic_token(token: str) -> str:
    match = _MAGIC_TOKEN.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        raise ValueError("invalid email magic token format")
    return match.group("challenge")


def issue_session_credentials(
    key: bytes, *, key_version: int
) -> IssuedSessionCredentials:
    _require_hmac_key(key)
    if key_version < 1:
        raise ValueError("key_version must be positive")
    session_token = f"lae_ss_v{key_version}_{_opaque_secret()}"
    csrf_token = f"lae_cs_{_opaque_secret()}"
    return IssuedSessionCredentials(
        session_token=session_token,
        session_digest=keyed_secret_hash(
            session_token, key, domain="lae.session.v1"
        ),
        csrf_token=csrf_token,
        csrf_digest=keyed_secret_hash(csrf_token, key, domain="lae.csrf.v1"),
        key_version=key_version,
    )


def parse_session_token(token: str) -> int:
    match = _SESSION_TOKEN.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        raise ValueError("invalid session token format")
    return int(match.group("version"))


def session_token_digest(token: str, key: bytes) -> tuple[int, bytes]:
    version = parse_session_token(token)
    return version, keyed_secret_hash(token, key, domain="lae.session.v1")


def verify_csrf_token(token: str, *, expected_digest: bytes, key: bytes) -> bool:
    valid_format = isinstance(token, str) and _CSRF_TOKEN.fullmatch(token) is not None
    actual = keyed_secret_hash(
        token if valid_format else "invalid", key, domain="lae.csrf.v1"
    )
    return valid_format and hmac.compare_digest(actual, expected_digest)


def parse_deploy_token(token: str) -> tuple[str, str]:
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        raise ValueError("invalid deploy token format")
    return match.group("prefix"), match.group("secret")


def verify_deploy_token(token: str, *, expected_digest: bytes, key: bytes) -> bool:
    try:
        parse_deploy_token(token)
        actual = keyed_secret_hash(token, key, domain="lae.deploy-token.v1")
    except (TypeError, ValueError):
        # Hash a fixed value to keep malformed-token handling close to the valid
        # path without ever comparing user-controlled plaintext directly.
        actual = keyed_secret_hash("invalid", key, domain="lae.deploy-token.v1")
    return hmac.compare_digest(actual, expected_digest)


def require_token_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not _TOKEN_PREFIX.fullmatch(prefix):
        raise ValueError("invalid deploy token public prefix")
    return prefix
