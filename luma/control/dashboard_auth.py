"""Dashboard browser sign-in: passkeys (WebAuthn) and server-side sessions.

The management token stays the API credential for the CLI, CI and recovery. A
browser trades a passkey assertion, or the token itself unless the operator
requires passkeys, for a random session ID held in an ``HttpOnly`` cookie; only
its SHA-256 is stored. A request carrying a valid session acts with the
management token's authority, so every handler keeps its single
``require_token`` check and the CLI never depends on this module.

WebAuthn verification uses the pinned ``webauthn`` package from the ``control``
extra (Python 3.10+). It is imported lazily: node agents on Python 3.9 never load
it, and a Control without it still serves token sessions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from ..errors import LumaError
from . import database
from .state import load_auth_state, require_token

SESSION_COOKIE = "__Host-luma-session"
SESSION_ABSOLUTE_SECONDS = 24 * 3600
SESSION_IDLE_SECONDS = 2 * 3600
# Refresh last-seen at most this often so polling does not write on every request.
SESSION_TOUCH_SECONDS = 60
CHALLENGE_SECONDS = 300
ENROLLMENT_SECONDS = 15 * 60
MAX_PENDING_CHALLENGES = 32
MAX_SESSIONS = 20
MAX_PASSKEYS = 20
NAME_LIMIT = 64
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthError(LumaError):
    """Sign-in failure carrying the HTTP status the route should return."""

    def __init__(self, message: str, *, status: int = 401, code: str = "unauthorized"):
        super().__init__(message)
        self.status = status
        self.code = code


def passkeys_available() -> bool:
    try:
        import webauthn  # noqa: F401
    except ImportError:
        return False
    return True


def _webauthn():
    try:
        import webauthn
        from webauthn.helpers import structs
    except ImportError as exc:
        raise AuthError(
            "passkey support is not installed on this Control (install luma-infra[control] on Python 3.10+)",
            status=501, code="passkeys_unavailable",
        ) from exc
    return webauthn, structs


def _schema(conn: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE IF NOT EXISTS dashboard_sessions(id_hash TEXT PRIMARY KEY,public_id TEXT NOT NULL UNIQUE,method TEXT NOT NULL,passkey_id TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,last_seen_at REAL NOT NULL,expires_at REAL NOT NULL,user_agent TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS dashboard_passkeys(id TEXT PRIMARY KEY,name TEXT NOT NULL,public_key TEXT NOT NULL,sign_count INTEGER NOT NULL,transports TEXT NOT NULL,aaguid TEXT NOT NULL DEFAULT '',backed_up INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,last_used_at REAL)",
        "CREATE TABLE IF NOT EXISTS dashboard_auth_challenges(id TEXT PRIMARY KEY,purpose TEXT NOT NULL,challenge TEXT NOT NULL,enrollment_hash TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,expires_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS dashboard_enrollments(id_hash TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,expires_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS dashboard_auth_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    )
    for statement in statements:
        conn.execute(statement)


@contextmanager
def _read() -> Iterator[sqlite3.Connection]:
    """WAL read snapshot. The tables are created under a writer transaction first:
    a deferred transaction that upgrades to write can fail with SQLITE_BUSY."""
    with database.transaction(immediate=False) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_auth_settings'").fetchone():
            yield conn
            return
    with database.transaction(owner="dashboard_auth.schema") as conn:
        _schema(conn)
    with database.transaction(immediate=False) as conn:
        yield conn


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _from_b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _clock() -> float:
    return time.time()


def _now(now: float | None) -> float:
    return _clock() if now is None else float(now)


def _name(value: Any, default: str) -> str:
    text = " ".join(str(value or "").split())[:NAME_LIMIT]
    return text or default


def _setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM dashboard_auth_settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO dashboard_auth_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM dashboard_auth_challenges WHERE expires_at<=?", (now,))
    conn.execute("DELETE FROM dashboard_enrollments WHERE expires_at<=?", (now,))
    conn.execute(
        "DELETE FROM dashboard_sessions WHERE expires_at<=? OR last_seen_at<=?",
        (now, now - SESSION_IDLE_SECONDS),
    )


def _passkey_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM dashboard_passkeys").fetchone()[0])


def _require_passkey(conn: sqlite3.Connection) -> bool:
    return _setting(conn, "require_passkey") == "1" and _passkey_count(conn) > 0


def _relying_party(state: Mapping[str, Any]) -> Tuple[str, str]:
    domain = str(state.get("domain") or "").strip().lower().rstrip(".")
    if not domain:
        raise AuthError("Control domain is not configured", status=400, code="luma_error")
    return domain, f"https://{domain}"


def require_same_origin(headers: Mapping[str, str], state: Mapping[str, Any]) -> None:
    """Cookie-authenticated writes and WebSockets must come from the Control page."""
    _domain, origin = _relying_party(state)
    if str(headers.get("origin") or "").rstrip("/").lower() != origin:
        raise AuthError("request origin does not match the Control domain", status=403, code="forbidden_origin")


# Sessions -----------------------------------------------------------------

def _create_session(conn: sqlite3.Connection, *, method: str, passkey_id: str, user_agent: str, now: float) -> str:
    _prune(conn, now)
    session_id = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO dashboard_sessions(id_hash,public_id,method,passkey_id,created_at,last_seen_at,expires_at,user_agent) VALUES(?,?,?,?,?,?,?,?)",
        (_hash(session_id), secrets.token_hex(8), method, passkey_id, now, now, now + SESSION_ABSOLUTE_SECONDS, user_agent[:200]),
    )
    overflow = conn.execute("SELECT COUNT(*) FROM dashboard_sessions").fetchone()[0] - MAX_SESSIONS
    if overflow > 0:
        conn.execute(
            "DELETE FROM dashboard_sessions WHERE id_hash IN (SELECT id_hash FROM dashboard_sessions ORDER BY last_seen_at LIMIT ?)",
            (overflow,),
        )
    return session_id


def _session_row(conn: sqlite3.Connection, session_id: str, now: float) -> Optional[sqlite3.Row]:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM dashboard_sessions WHERE id_hash=?", (_hash(session_id),)).fetchone()
    if row is None or row["expires_at"] <= now or row["last_seen_at"] <= now - SESSION_IDLE_SECONDS:
        return None
    return row


def _session_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    passkey_name = ""
    if row["passkey_id"]:
        found = conn.execute("SELECT name FROM dashboard_passkeys WHERE id=?", (row["passkey_id"],)).fetchone()
        passkey_name = str(found[0]) if found else ""
    return {
        "id": row["public_id"],
        "method": row["method"],
        "passkeyName": passkey_name,
        "createdAt": int(row["created_at"]),
        "lastSeenAt": int(row["last_seen_at"]),
        "expiresAt": int(min(row["expires_at"], row["last_seen_at"] + SESSION_IDLE_SECONDS)),
        "absoluteExpiresAt": int(row["expires_at"]),
        "userAgent": row["user_agent"],
    }


def session_credential(method: str, headers: Mapping[str, str], session_id: str, *, now: float | None = None) -> str:
    """Resolve a session cookie to the management token for the existing handlers.

    Writes and WebSocket upgrades also need an ``Origin`` equal to the Control
    domain; ``SameSite=Strict`` alone does not cover every browser path.
    """
    at = _now(now)
    state = load_auth_state()
    if method.upper() not in SAFE_METHODS:
        require_same_origin(headers, state)
    with _read() as conn:
        row = _session_row(conn, session_id, at)
        stale = row is not None and at - row["last_seen_at"] >= SESSION_TOUCH_SECONDS
    if row is None:
        raise AuthError("unauthorized")
    if stale:
        with database.transaction(owner="dashboard_auth.touch") as conn:
            conn.execute("UPDATE dashboard_sessions SET last_seen_at=? WHERE id_hash=?", (at, _hash(session_id)))
    token = str(state.get("deployToken") or "")
    if not token:
        raise AuthError("unauthorized")
    return token


def current_session(session_id: str, *, now: float | None = None) -> Dict[str, Any]:
    at = _now(now)
    with _read() as conn:
        row = _session_row(conn, session_id, at)
        if row is None:
            raise AuthError("unauthorized")
        return {"authenticated": True, "session": _session_payload(conn, row)}


def exchange_token(token: str, headers: Mapping[str, str], *, now: float | None = None) -> Tuple[Dict[str, Any], str]:
    """Trade the management token for a browser session (the recovery path)."""
    at = _now(now)
    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    require_same_origin(headers, state)
    with database.transaction(owner="dashboard_auth.exchange_token") as conn:
        _schema(conn)
        if _require_passkey(conn):
            raise AuthError(
                "this Control requires a passkey for dashboard sign-in; run `luma passkey require off` to allow the management token again",
                status=403, code="passkey_required",
            )
        session_id = _create_session(conn, method="token", passkey_id="", user_agent=str(headers.get("user-agent") or ""), now=at)
        row = _session_row(conn, session_id, at)
        return {"authenticated": True, "session": _session_payload(conn, row)}, session_id


def logout(session_id: str) -> Dict[str, Any]:
    if not session_id:
        return {"signedOut": False}
    with database.transaction(owner="dashboard_auth.logout") as conn:
        _schema(conn)
        deleted = conn.execute("DELETE FROM dashboard_sessions WHERE id_hash=?", (_hash(session_id),)).rowcount
    return {"signedOut": bool(deleted)}


def list_sessions(current_id: str = "", *, now: float | None = None) -> Dict[str, Any]:
    at = _now(now)
    current_hash = _hash(current_id) if current_id else ""
    with _read() as conn:
        items = []
        for row in conn.execute("SELECT * FROM dashboard_sessions ORDER BY last_seen_at DESC"):
            if row["expires_at"] <= at or row["last_seen_at"] <= at - SESSION_IDLE_SECONDS:
                continue
            item = _session_payload(conn, row)
            item["current"] = bool(current_hash) and row["id_hash"] == current_hash
            items.append(item)
        return {"sessions": items}


def revoke_sessions(body: Mapping[str, Any]) -> Dict[str, Any]:
    public_id = str(body.get("id") or "").strip()
    with database.transaction(owner="dashboard_auth.revoke_sessions") as conn:
        _schema(conn)
        if public_id:
            revoked = conn.execute("DELETE FROM dashboard_sessions WHERE public_id=?", (public_id,)).rowcount
            if not revoked:
                raise LumaError(f"browser session not found: {public_id}")
        elif body.get("all") is True:
            revoked = conn.execute("DELETE FROM dashboard_sessions").rowcount
        else:
            raise LumaError("specify a session id or all=true")
    return {"revoked": revoked}


# Settings and public configuration ---------------------------------------

def public_config() -> Dict[str, Any]:
    """What the sign-in screen may know before authentication."""
    state = load_auth_state()
    with _read() as conn:
        registered = _passkey_count(conn) > 0
        required = _require_passkey(conn)
    rp_id = str(state.get("domain") or "").strip().lower().rstrip(".")
    return {
        "passkeys": {"available": passkeys_available(), "registered": registered},
        "requirePasskey": required,
        "rpId": rp_id,
    }


def get_settings() -> Dict[str, Any]:
    with _read() as conn:
        return {
            "requirePasskey": _setting(conn, "require_passkey") == "1",
            "passkeyCount": _passkey_count(conn),
            "sessionAbsoluteSeconds": SESSION_ABSOLUTE_SECONDS,
            "sessionIdleSeconds": SESSION_IDLE_SECONDS,
        }


def update_settings(body: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(body.get("requirePasskey"), bool):
        raise LumaError("requirePasskey must be true or false")
    enabled = bool(body["requirePasskey"])
    with database.transaction(owner="dashboard_auth.update_settings") as conn:
        _schema(conn)
        if enabled and _passkey_count(conn) == 0:
            raise LumaError("add a passkey before requiring passkeys for dashboard sign-in")
        _set_setting(conn, "require_passkey", "1" if enabled else "0")
        signed_out = 0
        if enabled:
            # A token-created browser session must not outlive the new policy.
            signed_out = conn.execute("DELETE FROM dashboard_sessions WHERE method='token'").rowcount
    result = get_settings()
    result["signedOutSessions"] = signed_out
    return result


# Passkeys -------------------------------------------------------------------

def _passkey_payload(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "transports": json.loads(row["transports"] or "[]"),
        "backedUp": bool(row["backed_up"]),
        "createdAt": int(row["created_at"]),
        "lastUsedAt": int(row["last_used_at"]) if row["last_used_at"] else None,
    }


def list_passkeys() -> Dict[str, Any]:
    with _read() as conn:
        rows = conn.execute("SELECT * FROM dashboard_passkeys ORDER BY created_at").fetchall()
        return {"passkeys": [_passkey_payload(row) for row in rows]}


def remove_passkey(body: Mapping[str, Any]) -> Dict[str, Any]:
    identifier = str(body.get("id") or "").strip()
    if not identifier:
        raise LumaError("passkey id is required")
    with database.transaction(owner="dashboard_auth.remove_passkey") as conn:
        _schema(conn)
        if not conn.execute("DELETE FROM dashboard_passkeys WHERE id=?", (identifier,)).rowcount:
            raise LumaError(f"passkey not found: {identifier}")
        signed_out = conn.execute("DELETE FROM dashboard_sessions WHERE passkey_id=?", (identifier,)).rowcount
        if _passkey_count(conn) == 0:
            # Never leave an inert requirement that silently returns with the next passkey.
            _set_setting(conn, "require_passkey", "0")
    return {"removed": identifier, "signedOutSessions": signed_out}


def create_enrollment(body: Mapping[str, Any], *, now: float | None = None) -> Dict[str, Any]:
    """One-time link for adding a passkey from a browser that holds no session."""
    at = _now(now)
    state = load_auth_state()
    _domain, origin = _relying_party(state)
    code = secrets.token_urlsafe(24)
    with database.transaction(owner="dashboard_auth.create_enrollment") as conn:
        _schema(conn)
        _prune(conn, at)
        conn.execute(
            "INSERT INTO dashboard_enrollments(id_hash,name,created_at,expires_at) VALUES(?,?,?,?)",
            (_hash(code), _name(body.get("name"), ""), at, at + ENROLLMENT_SECONDS),
        )
    # A fragment keeps the code out of request lines, proxy logs and Referer headers.
    return {"url": f"{origin}/dashboard/enroll#code={code}", "expiresAt": int(at + ENROLLMENT_SECONDS)}


def _enrollment(conn: sqlite3.Connection, code: str, now: float) -> Optional[sqlite3.Row]:
    row = conn.execute("SELECT * FROM dashboard_enrollments WHERE id_hash=?", (_hash(code),)).fetchone()
    if row is None or row["expires_at"] <= now:
        return None
    return row


def _store_challenge(conn: sqlite3.Connection, purpose: str, challenge: bytes, enrollment_hash: str, now: float) -> str:
    _prune(conn, now)
    overflow = conn.execute("SELECT COUNT(*) FROM dashboard_auth_challenges WHERE purpose=?", (purpose,)).fetchone()[0] - MAX_PENDING_CHALLENGES + 1
    if overflow > 0:
        conn.execute(
            "DELETE FROM dashboard_auth_challenges WHERE id IN (SELECT id FROM dashboard_auth_challenges WHERE purpose=? ORDER BY created_at LIMIT ?)",
            (purpose, overflow),
        )
    challenge_id = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO dashboard_auth_challenges(id,purpose,challenge,enrollment_hash,created_at,expires_at) VALUES(?,?,?,?,?,?)",
        (challenge_id, purpose, _b64url(challenge), enrollment_hash, now, now + CHALLENGE_SECONDS),
    )
    return challenge_id


def _consume_challenge(challenge_id: str, purpose: str, now: float) -> sqlite3.Row:
    # Committed on its own so a failed verification still burns the challenge.
    with database.transaction(owner="dashboard_auth.consume_challenge") as conn:
        _schema(conn)
        row = conn.execute(
            "SELECT * FROM dashboard_auth_challenges WHERE id=? AND purpose=?", (challenge_id, purpose)
        ).fetchone()
        conn.execute("DELETE FROM dashboard_auth_challenges WHERE id=?", (challenge_id,))
    if row is None or row["expires_at"] <= now:
        raise AuthError("passkey challenge expired; start again")
    return row


def _descriptors(conn: sqlite3.Connection, structs: Any) -> list:
    descriptors = []
    for row in conn.execute("SELECT id,transports FROM dashboard_passkeys ORDER BY created_at"):
        transports = []
        for value in json.loads(row["transports"] or "[]"):
            try:
                transports.append(structs.AuthenticatorTransport(value))
            except ValueError:
                continue
        descriptors.append(structs.PublicKeyCredentialDescriptor(id=_from_b64url(row["id"]), transports=transports or None))
    return descriptors


def _user_handle(conn: sqlite3.Connection) -> bytes:
    # One operator, one stable WebAuthn user handle per Control.
    value = _setting(conn, "user_handle")
    if not value:
        value = _b64url(secrets.token_bytes(32))
        _set_setting(conn, "user_handle", value)
    return _from_b64url(value)


def registration_options(body: Mapping[str, Any], *, authenticated: bool, now: float | None = None) -> Dict[str, Any]:
    """Start adding a passkey. Callers hold the management authority or an enrollment code."""
    webauthn, structs = _webauthn()
    at = _now(now)
    state = load_auth_state()
    rp_id, _origin = _relying_party(state)
    code = str(body.get("enrollmentCode") or "").strip()
    with database.transaction(owner="dashboard_auth.registration_options") as conn:
        _schema(conn)
        enrollment_hash = ""
        if not authenticated:
            enrollment = _enrollment(conn, code, at) if code else None
            if enrollment is None:
                raise AuthError("enrollment link is invalid or expired; create a new one with `luma passkey enroll`")
            enrollment_hash = enrollment["id_hash"]
        if _passkey_count(conn) >= MAX_PASSKEYS:
            raise LumaError(f"this Control already has {MAX_PASSKEYS} passkeys; remove one first")
        cluster = str(state.get("clusterId") or "luma")
        options = webauthn.generate_registration_options(
            rp_id=rp_id,
            rp_name="Luma",
            user_name=f"luma@{rp_id}",
            user_id=_user_handle(conn),
            user_display_name=f"Luma {cluster}",
            authenticator_selection=structs.AuthenticatorSelectionCriteria(
                resident_key=structs.ResidentKeyRequirement.PREFERRED,
                user_verification=structs.UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=_descriptors(conn, structs),
        )
        challenge_id = _store_challenge(conn, "register", options.challenge, enrollment_hash, at)
        suggested = conn.execute("SELECT name FROM dashboard_enrollments WHERE id_hash=?", (enrollment_hash,)).fetchone() if enrollment_hash else None
    return {
        "challengeId": challenge_id,
        "publicKey": json.loads(webauthn.options_to_json(options)),
        "suggestedName": str(suggested[0]) if suggested and suggested[0] else "",
    }


def registration_verify(body: Mapping[str, Any], headers: Mapping[str, str], *, authenticated: bool,
                        now: float | None = None) -> Tuple[Dict[str, Any], str]:
    """Finish adding a passkey; an enrollment-link registration also signs the browser in."""
    webauthn, _structs = _webauthn()
    at = _now(now)
    state = load_auth_state()
    rp_id, origin = _relying_party(state)
    require_same_origin(headers, state)
    challenge = _consume_challenge(str(body.get("challengeId") or ""), "register", at)
    if not authenticated and not challenge["enrollment_hash"]:
        raise AuthError("unauthorized")
    credential = body.get("credential")
    if not isinstance(credential, dict):
        raise LumaError("credential must be the object returned by navigator.credentials.create()")
    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=_from_b64url(challenge["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:  # the library raises several unrelated exception types
        raise AuthError(f"passkey registration failed: {exc}", status=400, code="passkey_invalid") from exc
    response = credential.get("response") if isinstance(credential.get("response"), dict) else {}
    transports = [str(item) for item in response.get("transports") or [] if isinstance(item, str)][:8]
    credential_id = _b64url(verified.credential_id)
    session_id = ""
    with database.transaction(owner="dashboard_auth.registration_verify") as conn:
        _schema(conn)
        default_name = "Passkey"
        if challenge["enrollment_hash"]:
            enrollment = conn.execute("SELECT * FROM dashboard_enrollments WHERE id_hash=?", (challenge["enrollment_hash"],)).fetchone()
            if enrollment is None or enrollment["expires_at"] <= at:
                raise AuthError("enrollment link is invalid or expired; create a new one with `luma passkey enroll`")
            default_name = enrollment["name"] or default_name
            conn.execute("DELETE FROM dashboard_enrollments WHERE id_hash=?", (challenge["enrollment_hash"],))
        if _passkey_count(conn) >= MAX_PASSKEYS:
            raise LumaError(f"this Control already has {MAX_PASSKEYS} passkeys; remove one first")
        if conn.execute("SELECT 1 FROM dashboard_passkeys WHERE id=?", (credential_id,)).fetchone():
            raise LumaError("this passkey is already registered")
        conn.execute(
            "INSERT INTO dashboard_passkeys(id,name,public_key,sign_count,transports,aaguid,backed_up,created_at,last_used_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
            (credential_id, _name(body.get("name"), default_name), _b64url(verified.credential_public_key),
             int(verified.sign_count), json.dumps(transports), str(verified.aaguid or ""),
             1 if verified.credential_backed_up else 0, at),
        )
        row = conn.execute("SELECT * FROM dashboard_passkeys WHERE id=?", (credential_id,)).fetchone()
        result: Dict[str, Any] = {"passkey": _passkey_payload(row)}
        if not authenticated:
            session_id = _create_session(conn, method="passkey", passkey_id=credential_id,
                                         user_agent=str(headers.get("user-agent") or ""), now=at)
            result["session"] = _session_payload(conn, _session_row(conn, session_id, at))
    return result, session_id


def authentication_options(*, now: float | None = None) -> Dict[str, Any]:
    webauthn, structs = _webauthn()
    at = _now(now)
    rp_id, _origin = _relying_party(load_auth_state())
    with database.transaction(owner="dashboard_auth.authentication_options") as conn:
        _schema(conn)
        descriptors = _descriptors(conn, structs)
        if not descriptors:
            raise AuthError("no passkeys are registered on this Control yet", status=404, code="no_passkeys")
        options = webauthn.generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=descriptors,
            user_verification=structs.UserVerificationRequirement.REQUIRED,
        )
        challenge_id = _store_challenge(conn, "authenticate", options.challenge, "", at)
    return {"challengeId": challenge_id, "publicKey": json.loads(webauthn.options_to_json(options))}


def authentication_verify(body: Mapping[str, Any], headers: Mapping[str, str], *,
                          now: float | None = None) -> Tuple[Dict[str, Any], str]:
    webauthn, _structs = _webauthn()
    at = _now(now)
    state = load_auth_state()
    rp_id, origin = _relying_party(state)
    require_same_origin(headers, state)
    challenge = _consume_challenge(str(body.get("challengeId") or ""), "authenticate", at)
    credential = body.get("credential")
    if not isinstance(credential, dict):
        raise LumaError("credential must be the object returned by navigator.credentials.get()")
    credential_id = str(credential.get("rawId") or credential.get("id") or "")
    with _read() as conn:
        passkey = conn.execute("SELECT * FROM dashboard_passkeys WHERE id=?", (credential_id,)).fetchone()
    if passkey is None:
        raise AuthError("this passkey is not registered on this Control")
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=_from_b64url(challenge["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=_from_b64url(passkey["public_key"]),
            credential_current_sign_count=int(passkey["sign_count"]),
            require_user_verification=True,
        )
    except Exception as exc:  # signature, counter, origin or structure failure
        raise AuthError(f"passkey sign-in failed: {exc}") from exc
    with database.transaction(owner="dashboard_auth.authentication_verify") as conn:
        _schema(conn)
        updated = conn.execute(
            "UPDATE dashboard_passkeys SET sign_count=?,last_used_at=?,backed_up=? WHERE id=?",
            (int(verified.new_sign_count), at, 1 if verified.credential_backed_up else 0, credential_id),
        ).rowcount
        if not updated:
            raise AuthError("this passkey was removed")
        session_id = _create_session(conn, method="passkey", passkey_id=credential_id,
                                     user_agent=str(headers.get("user-agent") or ""), now=at)
        return {"authenticated": True, "session": _session_payload(conn, _session_row(conn, session_id, at))}, session_id
