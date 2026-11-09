"""Content-free mutation audit for Nomad Community and Luma Control.

This is application auditing, not Nomad Enterprise audit logging. It covers
requests passing through Luma; direct Nomad administrator actions must be
collected separately. Never log bodies, token values, headers, or job JSON.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import LumaError

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("luma_security_actor", default=None)
_FIELDS = frozenset({"method", "path", "job", "namespace", "sha256", "status", "modifyIndex", "evaluation", "outcome", "credentialRole", "accessor"})


def credential_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:24] if token else "anonymous"


def authenticated(kind: str, identifier: str = "") -> None:
    context = _context.get()
    if context is not None:
        context["authenticated"] = True
        # Retain the passkey/session actor when a downstream handler checks the
        # management token to which the session has already been resolved.
        if context.get("actorKind") != "session":
            context["actorKind"] = kind
            if identifier:
                context["actor"] = identifier


def session_actor(public_id: str, passkey_id: str) -> None:
    context = _context.get()
    if context is not None:
        context.update({"actorKind": "session", "actor": public_id, "passkey": credential_id(passkey_id) if passkey_id else "", "authenticated": True})


def record(kind: str, **fields: Any) -> None:
    configured = os.environ.get("LUMA_SECURITY_AUDIT_FILE")
    if not configured:
        return
    path = Path(configured)
    context = _context.get()
    actor = dict(context) if context else {"actorKind": "local-process", "actor": f"uid:{os.geteuid()}", "authenticated": True, "requestId": uuid.uuid4().hex}
    event = {"schema": 1, "at": time.time(), "event": kind, **actor}
    event.update({k: v for k, v in fields.items() if k in _FIELDS and isinstance(v, (str, bool, int, float, type(None)))})
    if "path" in event:
        event["path"] = str(event["path"]).split("?", 1)[0][:512]
    raw = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise OSError("audit sink is not a private regular file")
            if os.write(fd, raw) != len(raw):
                raise OSError("short audit write")
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise LumaError("security audit is unavailable; mutation was not authorized to proceed") from None


class SecurityAuditMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        bearer = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        credential = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        method = scope.get("method") or "WEBSOCKET"
        path = str(scope.get("path") or "").split("?", 1)[0]
        # uvicorn must run with proxy_headers=False so this remains the socket
        # peer. Forwarded headers are user claims and are deliberately omitted.
        context = {
            "requestId": uuid.uuid4().hex, "actorKind": "bearer" if credential else "anonymous",
            "actor": credential_id(credential), "authenticated": False,
            "peer": str((scope.get("client") or ("", 0))[0])[:128],
        }
        marker = _context.set(context)
        mutation = method not in {"GET", "HEAD", "OPTIONS"}
        status = 500
        audit_failed = False
        try:
            if mutation:
                try:
                    record("request.begin", method=method, path=path)
                except LumaError:
                    audit_failed = True
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1011})
                    else:
                        await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]})
                        await send({"type": "http.response.body", "body": b'{"error":"security audit unavailable"}'})
                    return

            async def capture(message: dict[str, Any]) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    message = dict(message)
                    message["headers"] = [*message.get("headers", []), (b"x-luma-request-id", context["requestId"].encode())]
                elif message["type"] == "websocket.accept":
                    status = 101
                await send(message)

            await self.app(scope, receive, capture)
        finally:
            try:
                if not audit_failed and (mutation or status in {401, 403}):
                    record("request.end", method=method, path=path, status=status)
            finally:
                _context.reset(marker)
