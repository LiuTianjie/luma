"""Unified Control Browser SSO (PKCE tickets + audience-bound sessions) for all Luma addons.

Every external-origin Luma addon (``builder``, ``eye``, and future plugins) shares
this single IdP protocol:

1. Addon redirects browser to ``{control}/dashboard/addon-login?addon=<id>&challenge=<S256>``.
2. Signed-in Control browser session posts ``POST /v1/auth/addon/<id>/authorize`` (same-origin
   CSRF protected) and receives a 60-second one-use ticket redirect to the addon's trusted
   registered HTTPS origin (never a client-supplied redirect URL).
3. Addon exchanges ``{code, verifier}`` server-to-server at ``POST /v1/auth/addon/<id>/exchange``
   for an opaque, audience-bound session token (``las_...``).
4. Addon verifies every request uncached at ``POST /v1/auth/addon/<id>/verify`` against the
   live Control parent browser session, so Control logout or Passkey revocation immediately
   invalidates every addon session across the entire fleet.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from . import addons, dashboard_auth as auth, database
from .state import load_auth_state

PROOF = re.compile(r"[A-Za-z0-9_-]{43}")
SESSION_GENERIC = re.compile(r"las_[A-Za-z0-9_-]{43}")
SESSION_BUILDER_LEGACY = re.compile(r"lbs_[A-Za-z0-9_-]{43}")
TICKET_SECONDS = 60
MAX_SESSIONS_PER_PARENT = 8


def _valid_session_token(addon_id: str, token: str) -> bool:
    if SESSION_GENERIC.fullmatch(token):
        return True
    if addon_id == "builder" and SESSION_BUILDER_LEGACY.fullmatch(token):
        return True
    return False


def _target(addon_id: str) -> dict[str, str]:
    try:
        return addons.resolve_addon_sso_target(load_auth_state(), addon_id)
    except Exception as exc:
        raise auth.AuthError(str(exc), status=400, code="addon_sso_unavailable") from exc


def _schema(conn) -> None:
    auth._schema(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_addon_tickets("
        "id_hash TEXT PRIMARY KEY,"
        "addon_id TEXT NOT NULL,"
        "session_hash TEXT NOT NULL,"
        "challenge TEXT NOT NULL,"
        "audience TEXT NOT NULL,"
        "expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_addon_sessions("
        "id_hash TEXT PRIMARY KEY,"
        "addon_id TEXT NOT NULL,"
        "session_hash TEXT NOT NULL,"
        "audience TEXT NOT NULL,"
        "created_at REAL NOT NULL,"
        "expires_at REAL NOT NULL)"
    )


def _prune(conn, now: float) -> None:
    for table in ("dashboard_addon_tickets", "dashboard_addon_sessions"):
        conn.execute(
            f"DELETE FROM {table} WHERE expires_at<=? OR session_hash NOT IN "
            "(SELECT id_hash FROM dashboard_sessions WHERE expires_at>? AND last_seen_at>?)",
            (now, now, now - auth.SESSION_IDLE_SECONDS),
        )


def _parent(conn, session_hash: str, now: float):
    row = conn.execute(
        "SELECT * FROM dashboard_sessions WHERE id_hash=? AND expires_at>? AND last_seen_at>?",
        (session_hash, now, now - auth.SESSION_IDLE_SECONDS),
    ).fetchone()
    if row is None:
        raise auth.AuthError("Control browser session has expired")
    return row


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def authorize(addon_id: str, session_id: str, headers, body: dict[str, Any]) -> dict[str, Any]:
    """Mint a one-use PKCE sign-in ticket for a registered addon from a real Control session."""
    auth.session_credential("POST", headers, session_id)
    challenge = body.get("challenge")
    if not isinstance(challenge, str) or not PROOF.fullmatch(challenge):
        raise auth.AuthError("invalid addon sign-in challenge", status=400)
    target = _target(addon_id)
    clean_id = target["addonId"]
    audience = target["audience"]
    callback_path = target["callbackPath"]
    now = auth._clock()
    code = secrets.token_urlsafe(32)
    with database.transaction(owner=f"addon_auth.authorize.{clean_id}") as conn:
        _schema(conn)
        _prune(conn, now)
        parent = _parent(conn, auth._hash(session_id), now)
        conn.execute(
            "DELETE FROM dashboard_addon_tickets WHERE session_hash=? AND addon_id=?",
            (parent["id_hash"], clean_id),
        )
        conn.execute(
            "INSERT INTO dashboard_addon_tickets VALUES(?,?,?,?,?,?)",
            (auth._hash(code), clean_id, parent["id_hash"], challenge, audience, now + TICKET_SECONDS),
        )
    return {
        "redirectUrl": f"{audience}{callback_path}?code={code}",
        "addonId": clean_id,
        "name": target["name"],
    }


def exchange(addon_id: str, body: dict[str, Any], *, token_prefix: str = "las_") -> dict[str, Any]:
    """Exchange a one-use PKCE ticket + verifier for an opaque audience-bound addon session."""
    code, verifier = body.get("code"), body.get("verifier")
    if not all(isinstance(value, str) and PROOF.fullmatch(value) for value in (code, verifier)):
        raise auth.AuthError("invalid addon sign-in proof")
    target = _target(addon_id)
    clean_id = target["addonId"]
    audience = target["audience"]
    challenge = auth._b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    now = auth._clock()
    prefix = "lbs_" if (token_prefix == "lbs_" and clean_id == "builder") else "las_"
    token = prefix + secrets.token_urlsafe(32)
    with database.transaction(owner=f"addon_auth.exchange.{clean_id}") as conn:
        _schema(conn)
        _prune(conn, now)
        ticket = conn.execute(
            "SELECT * FROM dashboard_addon_tickets WHERE id_hash=?",
            (auth._hash(code),),
        ).fetchone()
        if (
            ticket is None
            or ticket["addon_id"] != clean_id
            or ticket["audience"] != audience
            or not secrets.compare_digest(ticket["challenge"], challenge)
        ):
            raise auth.AuthError("addon sign-in ticket is invalid or expired")
        parent = _parent(conn, ticket["session_hash"], now)
        conn.execute("DELETE FROM dashboard_addon_tickets WHERE id_hash=?", (auth._hash(code),))
        conn.execute(
            "INSERT INTO dashboard_addon_sessions VALUES(?,?,?,?,?,?)",
            (auth._hash(token), clean_id, parent["id_hash"], audience, now, parent["expires_at"]),
        )
        conn.execute(
            "DELETE FROM dashboard_addon_sessions WHERE id_hash IN ("
            "SELECT id_hash FROM dashboard_addon_sessions WHERE session_hash=? AND addon_id=? "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (parent["id_hash"], clean_id, MAX_SESSIONS_PER_PARENT),
        )
    return {
        "authenticated": True,
        "token": token,
        "expiresAt": int(parent["expires_at"]),
        "audience": audience,
        "addonId": clean_id,
    }


def verify(addon_id: str, token: str) -> dict[str, Any]:
    """Verify an addon session against its live Control parent session (never cached)."""
    target = _target(addon_id)
    clean_id = target["addonId"]
    audience = target["audience"]
    if not isinstance(token, str) or not _valid_session_token(clean_id, token):
        raise auth.AuthError("invalid addon browser session")
    now = auth._clock()
    token_hash = auth._hash(token)
    with auth._read() as conn:
        session = None
        if _has_table(conn, "dashboard_addon_sessions"):
            row = conn.execute(
                "SELECT * FROM dashboard_addon_sessions WHERE id_hash=?",
                (token_hash,),
            ).fetchone()
            if row is not None and row["addon_id"] == clean_id:
                session = row
        # Rolling-upgrade compatibility: accept pre-existing Builder sessions stored
        # in the legacy dashboard_builder_sessions table until they expire or log out.
        if session is None and clean_id == "builder" and _has_table(conn, "dashboard_builder_sessions"):
            session = conn.execute(
                "SELECT * FROM dashboard_builder_sessions WHERE id_hash=?",
                (token_hash,),
            ).fetchone()
        if session is None or session["audience"] != audience or session["expires_at"] <= now:
            raise auth.AuthError("invalid addon browser session")
        parent = _parent(conn, session["session_hash"], now)
    if now - parent["last_seen_at"] >= auth.SESSION_TOUCH_SECONDS:
        with database.transaction(owner=f"addon_auth.touch.{clean_id}") as conn:
            conn.execute("UPDATE dashboard_sessions SET last_seen_at=? WHERE id_hash=?", (now, parent["id_hash"]))
    from ..security_audit import session_actor

    session_actor(str(parent["public_id"]), str(parent["passkey_id"]))
    return {
        "authenticated": True,
        "audience": audience,
        "expiresAt": int(parent["expires_at"]),
        "addonId": clean_id,
        "actor": str(parent["public_id"]),
    }


def logout(addon_id: str, token: str) -> dict[str, Any]:
    """Sign out a single addon session without terminating the Control parent session."""
    info = verify(addon_id, token)
    clean_id = info["addonId"]
    token_hash = auth._hash(token)
    with database.transaction(owner=f"addon_auth.logout.{clean_id}") as conn:
        if _has_table(conn, "dashboard_addon_sessions"):
            conn.execute("DELETE FROM dashboard_addon_sessions WHERE id_hash=?", (token_hash,))
        if clean_id == "builder" and _has_table(conn, "dashboard_builder_sessions"):
            conn.execute("DELETE FROM dashboard_builder_sessions WHERE id_hash=?", (token_hash,))
    return {"signedOut": True}


def revoke_addon(addon_id: str) -> None:
    """Invalidate all pending tickets and active SSO sessions for an addon."""
    clean_id = addons.validate_addon_id(addon_id)
    with database.transaction(owner=f"addon_auth.revoke.{clean_id}") as conn:
        if _has_table(conn, "dashboard_addon_tickets"):
            conn.execute("DELETE FROM dashboard_addon_tickets WHERE addon_id=?", (clean_id,))
        if _has_table(conn, "dashboard_addon_sessions"):
            conn.execute("DELETE FROM dashboard_addon_sessions WHERE addon_id=?", (clean_id,))
        if clean_id == "builder":
            if _has_table(conn, "dashboard_builder_tickets"):
                conn.execute("DELETE FROM dashboard_builder_tickets")
            if _has_table(conn, "dashboard_builder_sessions"):
                conn.execute("DELETE FROM dashboard_builder_sessions")
