"""One-use, PKCE-bound sign-in tickets for the configured Builder console.

Builder receives an opaque, audience-bound session, never the Control cookie or
management token. Every verification checks the parent browser session, so its
expiry, logout and revocation apply to Builder without an authentication cache.
"""
from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from . import dashboard_auth as auth, database
from .builder import service_url
from .state import load_auth_state

PROOF = re.compile(r"[A-Za-z0-9_-]{43}")
SESSION = re.compile(r"lbs_[A-Za-z0-9_-]{43}")
TICKET_SECONDS = 60
MAX_SESSIONS_PER_PARENT = 8


def _audience() -> str:
    url = service_url(load_auth_state())
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.path:
        raise auth.AuthError("Builder browser sign-in requires an HTTPS origin", status=400, code="builder_url_invalid")
    return url


def _schema(conn) -> None:
    auth._schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS dashboard_builder_tickets(id_hash TEXT PRIMARY KEY,session_hash TEXT NOT NULL,challenge TEXT NOT NULL,audience TEXT NOT NULL,expires_at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS dashboard_builder_sessions(id_hash TEXT PRIMARY KEY,session_hash TEXT NOT NULL,audience TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL)")


def _prune(conn, now: float) -> None:
    for table in ("dashboard_builder_tickets", "dashboard_builder_sessions"):
        conn.execute(f"DELETE FROM {table} WHERE expires_at<=? OR session_hash NOT IN (SELECT id_hash FROM dashboard_sessions WHERE expires_at>? AND last_seen_at>?)", (now, now, now - auth.SESSION_IDLE_SECONDS))


def _parent(conn, session_hash: str, now: float):
    row = conn.execute("SELECT * FROM dashboard_sessions WHERE id_hash=? AND expires_at>? AND last_seen_at>?", (session_hash, now, now - auth.SESSION_IDLE_SECONDS)).fetchone()
    if row is None:
        raise auth.AuthError("Control browser session has expired")
    return row


def authorize(session_id: str, headers, body: dict[str, Any]) -> dict[str, Any]:
    # Only the browser's real Control session can authorize this handoff.
    auth.session_credential("POST", headers, session_id)
    challenge = body.get("challenge")
    if not isinstance(challenge, str) or not PROOF.fullmatch(challenge):
        raise auth.AuthError("invalid Builder sign-in challenge", status=400)
    audience, now = _audience(), auth._clock()
    code = secrets.token_urlsafe(32)
    with database.transaction(owner="builder_auth.authorize") as conn:
        _schema(conn)
        _prune(conn, now)
        parent = _parent(conn, auth._hash(session_id), now)
        # One pending handoff per Control session also bounds abandoned tickets.
        conn.execute("DELETE FROM dashboard_builder_tickets WHERE session_hash=?", (parent["id_hash"],))
        conn.execute("INSERT INTO dashboard_builder_tickets VALUES(?,?,?,?,?)", (auth._hash(code), parent["id_hash"], challenge, audience, now + TICKET_SECONDS))
    return {"redirectUrl": f"{audience}/auth/callback?code={code}"}


def exchange(body: dict[str, Any]) -> dict[str, Any]:
    code, verifier = body.get("code"), body.get("verifier")
    if not all(isinstance(value, str) and PROOF.fullmatch(value) for value in (code, verifier)):
        raise auth.AuthError("invalid Builder sign-in proof")
    import hashlib

    challenge = auth._b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    audience, now = _audience(), auth._clock()
    token = "lbs_" + secrets.token_urlsafe(32)
    with database.transaction(owner="builder_auth.exchange") as conn:
        _schema(conn)
        _prune(conn, now)
        ticket = conn.execute("SELECT * FROM dashboard_builder_tickets WHERE id_hash=?", (auth._hash(code),)).fetchone()
        if ticket is None or ticket["audience"] != audience or not secrets.compare_digest(ticket["challenge"], challenge):
            raise auth.AuthError("Builder sign-in ticket is invalid or expired")
        parent = _parent(conn, ticket["session_hash"], now)
        conn.execute("DELETE FROM dashboard_builder_tickets WHERE id_hash=?", (auth._hash(code),))
        conn.execute("INSERT INTO dashboard_builder_sessions VALUES(?,?,?,?,?)", (auth._hash(token), parent["id_hash"], audience, now, parent["expires_at"]))
        conn.execute("DELETE FROM dashboard_builder_sessions WHERE id_hash IN (SELECT id_hash FROM dashboard_builder_sessions WHERE session_hash=? ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (parent["id_hash"], MAX_SESSIONS_PER_PARENT))
    return {"authenticated": True, "token": token, "expiresAt": int(parent["expires_at"]), "audience": audience}


def verify(token: str) -> dict[str, Any]:
    if not SESSION.fullmatch(token):
        raise auth.AuthError("invalid Builder browser session")
    audience, now = _audience(), auth._clock()
    with auth._read() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_builder_sessions'").fetchone():
            raise auth.AuthError("invalid Builder browser session")
        session = conn.execute("SELECT * FROM dashboard_builder_sessions WHERE id_hash=?", (auth._hash(token),)).fetchone()
        if session is None or session["audience"] != audience or session["expires_at"] <= now:
            raise auth.AuthError("invalid Builder browser session")
        parent = _parent(conn, session["session_hash"], now)
    if now - parent["last_seen_at"] >= auth.SESSION_TOUCH_SECONDS:
        with database.transaction(owner="builder_auth.touch") as conn:
            conn.execute("UPDATE dashboard_sessions SET last_seen_at=? WHERE id_hash=?", (now, parent["id_hash"]))
    from ..security_audit import session_actor

    session_actor(str(parent["public_id"]), str(parent["passkey_id"]))
    return {"authenticated": True, "audience": audience, "expiresAt": int(parent["expires_at"])}


def logout(token: str) -> dict[str, Any]:
    verify(token)
    with database.transaction(owner="builder_auth.logout") as conn:
        conn.execute("DELETE FROM dashboard_builder_sessions WHERE id_hash=?", (auth._hash(token),))
    return {"signedOut": True}
