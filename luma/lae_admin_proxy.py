from __future__ import annotations

import json
import os
import stat
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import LumaError


_RESOURCES = frozenset({"users", "tenants", "applications", "operations", "usage"})
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class LaeAdminProxyError(LumaError):
    pass


@dataclass(frozen=True)
class LaeAdminProxyConfig:
    endpoint: str
    token: str = field(repr=False)
    timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.endpoint)
            port = parsed.port
        except ValueError:
            raise LaeAdminProxyError("LAE admin API is unavailable") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or port is not None and not 1 <= port <= 65535
            or not 32 <= len(self.token) <= 512
            or any(not 33 <= ord(character) <= 126 for character in self.token)
            or not 1 <= float(self.timeout_seconds) <= 30
        ):
            raise LaeAdminProxyError("LAE admin API is unavailable")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def load_lae_admin_proxy_config(
    environ: Mapping[str, str] | None = None,
) -> LaeAdminProxyConfig:
    values = os.environ if environ is None else environ
    endpoint = str(values.get("LUMA_LAE_ADMIN_API_URL") or "").strip()
    token_file = str(values.get("LUMA_LAE_ADMIN_TOKEN_FILE") or "").strip()
    if not endpoint or not token_file:
        raise LaeAdminProxyError("LAE admin API is unavailable")
    path = Path(token_file)
    try:
        metadata = path.lstat()
    except OSError:
        raise LaeAdminProxyError("LAE admin API is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or not 1 <= metadata.st_size <= 4096
    ):
        raise LaeAdminProxyError("LAE admin API is unavailable")
    try:
        token = path.read_text(encoding="utf-8").strip()
        timeout = float(values.get("LUMA_LAE_ADMIN_TIMEOUT_SECONDS") or "8")
    except (OSError, UnicodeError, TypeError, ValueError):
        raise LaeAdminProxyError("LAE admin API is unavailable") from None
    return LaeAdminProxyConfig(endpoint.rstrip("/"), token, timeout)


def fetch_lae_admin_resource(
    resource: str,
    *,
    limit: int = 100,
    offset: int = 0,
    config: LaeAdminProxyConfig | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> Dict[str, Any]:
    if (
        resource not in _RESOURCES
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 200
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 1_000_000
    ):
        raise LaeAdminProxyError("LAE admin query is invalid")
    selected = config or load_lae_admin_proxy_config()
    query = urllib.parse.urlencode({"limit": limit, "offset": offset})
    url = (
        selected.endpoint
        + "/internal/v1/admin/"
        + urllib.parse.quote(resource, safe="")
        + "?"
        + query
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {selected.token}",
            "User-Agent": "luma-control-lae-admin/1",
        },
    )
    transport = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with transport.open(request, timeout=selected.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            content_type = str(response.headers.get("Content-Type") or "").split(
                ";", 1
            )[0].strip().lower()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except Exception:
        # HTTP errors can contain the request URL or response body.  Neither is
        # allowed to cross into Control logs or the dashboard error payload.
        raise LaeAdminProxyError("LAE admin API is unavailable") from None
    if (
        status != 200
        or content_type != "application/json"
        or len(raw) > _MAX_RESPONSE_BYTES
    ):
        raise LaeAdminProxyError("LAE admin API is unavailable")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise LaeAdminProxyError("LAE admin API is unavailable") from None
    _validate_response(body, resource=resource, limit=limit, offset=offset)
    return body


def _validate_response(
    body: object, *, resource: str, limit: int, offset: int
) -> None:
    if not isinstance(body, dict) or set(body) != {resource, "page"}:
        raise LaeAdminProxyError("LAE admin API is unavailable")
    items = body.get(resource)
    page = body.get("page")
    if (
        not isinstance(items, list)
        or len(items) > limit
        or any(not isinstance(item, dict) for item in items)
        or not isinstance(page, dict)
        or set(page) != {"limit", "offset", "total"}
        or page.get("limit") != limit
        or page.get("offset") != offset
        or isinstance(page.get("total"), bool)
        or not isinstance(page.get("total"), int)
        or page["total"] < len(items) + offset
    ):
        raise LaeAdminProxyError("LAE admin API is unavailable")
    encoded = json.dumps(body, ensure_ascii=True, allow_nan=False)
    for forbidden in (
        '"credential"',
        '"deployToken"',
        '"password"',
        '"secret"',
        '"token"',
        '"valueCiphertext"',
    ):
        if forbidden in encoded:
            raise LaeAdminProxyError("LAE admin API is unavailable")


__all__ = [
    "LaeAdminProxyConfig",
    "LaeAdminProxyError",
    "fetch_lae_admin_resource",
    "load_lae_admin_proxy_config",
]
