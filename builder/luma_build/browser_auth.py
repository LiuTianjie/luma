"""Browser SSO through Control's public APIs; no shared database or master token."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from .deployer import ControlAuthVerifier
from .errors import BuildError

SESSION_COOKIE = "__Host-luma-build-session"
LOGIN_COOKIE = "__Host-luma-build-login"
PROOF = re.compile(r"[A-Za-z0-9_-]{43}")
SESSION = re.compile(r"lbs_[A-Za-z0-9_-]{43}")
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


class BrowserAuth:
    def __init__(self, control: ControlAuthVerifier) -> None:
        self.control = control

    def _request(self, action: str, body=None, token: str = "") -> dict:
        if not self.control.enabled:
            raise BuildError("Luma Control browser sign-in is not configured", status_code=401)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.control.endpoint}/v1/auth/builder/{action}",
            data=json.dumps(body or {}).encode(), method="POST", headers=headers,
        )
        try:
            with self.control._opener().open(request, timeout=8) as response:
                payload = json.loads(response.read().decode())
                if response.status != 200 or not isinstance(payload, dict):
                    raise ValueError("invalid Control response")
                return payload
        except urllib.error.HTTPError as exc:
            status = exc.code if exc.code in {401, 403} else 503
            raise BuildError("Luma browser session expired" if status == 401 else "Luma Control browser sign-in is unavailable", status_code=status) from None
        except (OSError, ValueError) as exc:
            raise BuildError("Luma Control browser sign-in is unavailable", status_code=503) from exc

    async def require_session(self, request: Request) -> dict:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not SESSION.fullmatch(token):
            raise BuildError("Luma browser sign-in required", status_code=401)
        # Do not cache this result: parent logout/revocation must take effect.
        info = await run_in_threadpool(self._request, "verify", None, token)
        if info.get("authenticated") is not True or not info.get("audience"):
            raise BuildError("invalid Luma browser session", status_code=401)
        audience = urlsplit(info["audience"])
        if audience.scheme != "https" or audience.netloc.lower() != request.url.netloc.lower():
            raise BuildError("Luma browser session belongs to another Builder", status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin", "").rstrip("/") != info["audience"]:
                raise BuildError("request origin does not match the Builder console", status_code=403)
        return info

    async def start(self, _request: Request):
        if not self.control.enabled:
            return JSONResponse({"error": "Luma Control browser sign-in is not configured"}, status_code=404, headers=HEADERS)
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = RedirectResponse(f"{self.control.endpoint}/dashboard/builder-login?challenge={challenge}", status_code=303, headers=HEADERS)
        response.set_cookie(LOGIN_COOKIE, verifier, max_age=300, path="/", secure=True, httponly=True, samesite="lax")
        return response

    async def callback(self, request: Request):
        code, verifier = request.query_params.get("code", ""), request.cookies.get(LOGIN_COOKIE, "")
        try:
            if not PROOF.fullmatch(code) or not PROOF.fullmatch(verifier):
                raise BuildError("invalid sign-in handoff", status_code=401)
            info = await run_in_threadpool(self._request, "exchange", {"code": code, "verifier": verifier})
            token = info.get("token", "")
            lifetime = int(info.get("expiresAt", 0)) - int(time.time())
            audience = urlsplit(info.get("audience", ""))
            if (info.get("authenticated") is not True or not isinstance(token, str)
                    or not SESSION.fullmatch(token) or lifetime <= 0
                    or audience.scheme != "https" or audience.netloc.lower() != request.url.netloc.lower()):
                raise BuildError("invalid sign-in response", status_code=401)
            response = RedirectResponse("/", status_code=303, headers=HEADERS)
            response.set_cookie(SESSION_COOKIE, token, max_age=lifetime, path="/", secure=True, httponly=True, samesite="strict")
        except (BuildError, ValueError, TypeError):
            response = RedirectResponse("/?sso=failed", status_code=303, headers=HEADERS)
        response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    async def session(self, request: Request):
        try:
            info = await self.require_session(request)
            return JSONResponse({"authenticated": True, "expiresAt": info["expiresAt"]}, headers=HEADERS)
        except BuildError as exc:
            return JSONResponse({"authenticated": False, "error": str(exc)}, status_code=exc.status_code, headers=HEADERS)

    async def logout(self, request: Request):
        try:
            await self.require_session(request)
            await run_in_threadpool(self._request, "logout", None, request.cookies[SESSION_COOKIE])
            response = JSONResponse({"signedOut": True}, headers=HEADERS)
        except BuildError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=exc.status_code, headers=HEADERS)
            if exc.status_code != 401:
                return response
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    def routes(self) -> list[Route]:
        return [
            Route("/auth/login", self.start, methods=["GET"]),
            Route("/auth/callback", self.callback, methods=["GET"]),
            Route("/v1/auth/session", self.session, methods=["GET"]),
            Route("/v1/auth/logout", self.logout, methods=["POST"]),
        ]
