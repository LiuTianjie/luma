"""Dashboard adapter for the independent Builder's public API."""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..errors import LumaError
from .state import load_auth_state, require_token


def service_url(state: dict[str, Any]) -> str:
    from . import addons
    url = addons.resolve_addon_url(state, "builder")
    parsed = urllib.parse.urlsplit(url)
    if not url:
        raise LumaError("独立 Builder 尚未配置：请运行 `luma addon install builder` 部署 luma-build，或在 Control 设置 LUMA_BUILD_URL")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LumaError("invalid configured Builder URL")
    return url


def _client(token: str):
    from ..cli.builder import BuilderClient

    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    # The endpoint is operator configuration, never a browser-supplied URL.
    # Reuse SSO server-side; the browser keeps only its HttpOnly Control session.
    return BuilderClient(service_url(state), token), state


def status(token: str) -> dict[str, Any]:
    client, _ = _client(token)
    return client.request("GET", "/v1/status")


def submit(token: str, body: dict[str, Any]) -> dict[str, Any]:
    from . import server

    client, state = _client(token)
    server._reject_control_maintenance(state)
    # Runner selection and the ACR destination belong to Builder configuration.
    # Ignore old form fields rather than pinning a cluster node or localhost registry.
    allowed = {"ref", "region", "exposure", "domain", "port", "manifest", "platform", "context", "dockerfile", "envSecrets"}
    request = {key: value for key, value in body.items() if key in allowed}
    provider_id = str(body.get("providerId") or "").strip()
    repository = str(body.get("repository") or "").strip()
    if provider_id and repository:
        provider = server._require_git_provider(state, provider_id)
        request["repoUrl"] = server._git_provider_clone_url(provider, repository)
    else:
        request["repoUrl"] = server.normalize_import_repo_url(str(body.get("repoUrl") or "").strip())
    if not request["repoUrl"]:
        raise LumaError("repoUrl or providerId + repository is required")
    # Control's saved accounts browse repositories. Builder resolves its own
    # scoped clone credential by the resulting URL, not a Control credential ID.
    request["deploy"] = True
    return client.request("POST", "/v1/builds", request)


def detail(token: str, build_id: str, after_seq: str = "0") -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", build_id):
        raise LumaError("invalid Builder build ID")
    try:
        after = max(0, int(after_seq))
    except ValueError as exc:
        raise LumaError("afterSeq must be an integer") from exc
    client, _ = _client(token)
    return client.request("GET", f"/v1/builds/{build_id}?afterSeq={after}")
