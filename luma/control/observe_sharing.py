"""Password access limited to Grafana's explicitly shared dashboards.

Guest cookies never resolve to a management credential. Their signatures are
bound to the saved password hash, so password changes revoke every guest session
without adding a session store or writing on panel-query requests.
"""
from __future__ import annotations

from collections import deque
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from ..errors import LumaError
from .dashboard_auth import AuthError, require_same_origin
from .state import load_auth_state

SESSION_COOKIE = "__Host-luma-grafana-share"
SESSION_SECONDS = 8 * 3600
PASSWORD_HASH_KEY = "grafanaPublicSharingPasswordHash"
PASSWORD_ITERATIONS = 600_000
_HASH_PREFIX = f"pbkdf2-sha256${PASSWORD_ITERATIONS}$"
_HASH_PATTERN = re.compile(re.escape(_HASH_PREFIX) + r"[a-f0-9]{32}\$[a-f0-9]{64}")
_PAGE_PATH = re.compile(r"/grafana/public-dashboards/([a-fA-F0-9]{32})/?")
_API_PATH = re.compile(r"/grafana/api/public/dashboards/[a-fA-F0-9]{32}(/annotations)?/?")
_QUERY_PATH = re.compile(r"/grafana/api/public/dashboards/[a-fA-F0-9]{32}/panels/[0-9]+/query/?")
_ASSET_PATH = re.compile(r"/grafana/public/(build|img|fonts|locales|plugins)/[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
_attempts: deque[tuple[float, str]] = deque()
_attempt_lock = threading.Lock()


def _password_hash(state: Mapping[str, Any]) -> str:
    settings = state.get("observeSettings")
    value = settings.get(PASSWORD_HASH_KEY) if isinstance(settings, dict) else None
    return value if isinstance(value, str) and _HASH_PATTERN.fullmatch(value) else ""


def public_sharing_enabled(state: Mapping[str, Any]) -> bool:
    settings = state.get("observeSettings")
    # Old enabled settings without a password must fail closed after an upgrade.
    return isinstance(settings, dict) and settings.get("grafanaPublicSharingEnabled") is True and bool(_password_hash(state))


def hash_password(password: Any) -> str:
    if not isinstance(password, str) or not 8 <= len(password) <= 128 or not password.strip():
        raise LumaError("sharing password must contain 8 to 128 characters and cannot be blank")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    return f"{_HASH_PREFIX}{salt}${digest.hex()}"


def _check_attempt(peer: str) -> None:
    """Bound both guessing and password-hash work, including behind a proxy."""
    now = time.monotonic()
    with _attempt_lock:
        while _attempts and _attempts[0][0] <= now - 60:
            _attempts.popleft()
        if len(_attempts) >= 30 or sum(address == peer for _, address in _attempts) >= 5:
            raise AuthError("too many password attempts; try again in a minute", status=429, code="sharing_rate_limited")
        _attempts.append((now, peer))


def _signature(state: Mapping[str, Any], payload: str) -> str:
    key = str(state.get("deployToken") or "")
    if not key:
        raise AuthError("sharing is unavailable", status=503, code="sharing_unavailable")
    message = f"grafana-public\0{_password_hash(state)}\0{payload}"
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(body: Any, headers: Mapping[str, str], peer: str) -> str:
    state = load_auth_state()
    require_same_origin(headers, state)
    if not public_sharing_enabled(state):
        raise AuthError("external dashboard sharing is disabled", status=404, code="sharing_disabled")
    if not isinstance(body, dict) or set(body) != {"password"} or not isinstance(body["password"], str):
        raise LumaError("request body must contain only a sharing password")
    _check_attempt(peer)
    password = body["password"]
    if not 8 <= len(password) <= 128:
        raise AuthError("incorrect sharing password", code="sharing_password_incorrect")
    salt, expected = _password_hash(state).removeprefix(_HASH_PREFIX).split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    if not hmac.compare_digest(digest.hex(), expected):
        raise AuthError("incorrect sharing password", code="sharing_password_incorrect")
    payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
    return f"{payload}.{_signature(state, payload)}"


def valid_session(state: Mapping[str, Any], value: str) -> bool:
    if not public_sharing_enabled(state) or not re.fullmatch(r"[0-9]{1,12}\.[a-f0-9]{32}\.[a-f0-9]{64}", value):
        return False
    payload, signature = value.rsplit(".", 1)
    expires = int(payload.split(".", 1)[0])
    now = time.time()
    if not now < expires <= now + SESSION_SECONDS:
        return False
    return hmac.compare_digest(signature, _signature(state, payload))


async def sign_in(request: Request) -> Response:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise AuthError("a JSON request is required", status=415, code="unsupported_media_type")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise AuthError("request body is too large", status=413, code="body_too_large")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LumaError("request body contains malformed JSON") from exc
    session = await run_in_threadpool(create_session, body, request.headers, request.client.host if request.client else "")
    response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(SESSION_COOKIE, session, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="lax", path="/")
    return response


async def forward_auth(request: Request) -> Response:
    method = request.headers.get("x-forwarded-method", "").upper()
    uri = request.headers.get("x-forwarded-uri", "")
    if not uri.startswith("/grafana/") or len(uri) > 8192:
        raise AuthError("not a shared dashboard request", status=403, code="sharing_path_forbidden")
    target = urlsplit(uri)
    page = _PAGE_PATH.fullmatch(target.path)
    readable = method in {"GET", "HEAD"} and (page or _API_PATH.fullmatch(target.path) or _ASSET_PATH.fullmatch(target.path))
    query = method == "POST" and _QUERY_PATH.fullmatch(target.path)
    if target.fragment or not (readable or query):
        raise AuthError("not a shared dashboard request", status=403, code="sharing_path_forbidden")
    state = await run_in_threadpool(load_auth_state)
    if not public_sharing_enabled(state):
        raise AuthError("external dashboard sharing is disabled", status=404, code="sharing_disabled")
    if valid_session(state, request.cookies.get(SESSION_COOKIE, "")):
        if query:
            require_same_origin(request.headers, state)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})
    if page:
        destination = f"/dashboard/share/{page[1]}"
        if target.query:
            destination += f"?{target.query}"
        return RedirectResponse(destination, status_code=303, headers={"Cache-Control": "no-store"})
    raise AuthError("a sharing password is required", code="sharing_password_required")
