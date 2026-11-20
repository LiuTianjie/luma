"""Backwards-compatible Builder SSO facade over ``luma.control.addon_auth``."""
from __future__ import annotations

from typing import Any

from . import addon_auth

PROOF = addon_auth.PROOF
SESSION = addon_auth.SESSION_BUILDER_LEGACY
TICKET_SECONDS = addon_auth.TICKET_SECONDS
MAX_SESSIONS_PER_PARENT = addon_auth.MAX_SESSIONS_PER_PARENT


def authorize(session_id: str, headers, body: dict[str, Any]) -> dict[str, Any]:
    return addon_auth.authorize("builder", session_id, headers, body)


def exchange(body: dict[str, Any]) -> dict[str, Any]:
    return addon_auth.exchange("builder", body, token_prefix="lbs_")


def verify(token: str) -> dict[str, Any]:
    return addon_auth.verify("builder", token)


def logout(token: str) -> dict[str, Any]:
    return addon_auth.logout("builder", token)
