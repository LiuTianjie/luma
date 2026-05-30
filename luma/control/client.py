from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from ..errors import LumaError


class ControlClient:
    def __init__(self, endpoint: str, token: str, *, insecure: bool = False, resolve_ip: str | None = None):
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "https":
            raise LumaError("control endpoint must use https")
        if not parsed.hostname:
            raise LumaError("control endpoint must include a hostname")
        if resolve_ip and not insecure:
            raise LumaError("--resolve-ip requires --insecure; use DNS for verified TLS")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.insecure = insecure
        self.resolve_ip = resolve_ip
        self._host_header = parsed.netloc

    def request(self, method: str, path: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.resolve_ip:
            headers["Host"] = self._host_header
        req = urllib.request.Request(
            self._request_url(path),
            data=data,
            method=method,
            headers=headers,
        )
        try:
            kwargs: Dict[str, Any] = {"timeout": 30}
            if self.insecure:
                kwargs["context"] = ssl._create_unverified_context()
            with urllib.request.urlopen(req, **kwargs) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"control API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LumaError(f"control API unavailable: {exc}") from exc
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise LumaError("control API returned invalid JSON")
        return payload

    def _request_url(self, path: str) -> str:
        if not self.resolve_ip:
            return self.endpoint + path
        parsed = urllib.parse.urlparse(self.endpoint)
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{self.resolve_ip}{port}"
        return urllib.parse.urlunparse((parsed.scheme, netloc, path, "", "", ""))

    def verify_login(self) -> Dict[str, Any]:
        return self.request("POST", "/v1/auth/login/verify", {})

    def register_node(self, *, node_name: str, profile: str, region: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/nodes/register",
            {"nodeName": node_name, "profile": profile, "region": region},
        )

    def label_node(self, *, node_name: str, profile: str, region: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/nodes/label",
            {"nodeName": node_name, "profile": profile, "region": region},
        )

    def deploy(self, *, manifest: str, source_name: str, skip_dns: bool = False, skip_webhook: bool = False) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/deployments",
            {
                "manifest": manifest,
                "sourceName": source_name,
                "skipDns": skip_dns,
                "skipWebhook": skip_webhook,
            },
        )
