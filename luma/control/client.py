from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator

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

    def request(self, method: str, path: str, body: Dict[str, Any] | None = None, *, timeout: int = 30) -> Dict[str, Any]:
        with self._open(method, path, body, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise LumaError("control API returned invalid JSON")
        return payload

    def stream(self, method: str, path: str, body: Dict[str, Any] | None = None, *, timeout: int = 30) -> Iterator[Dict[str, Any]]:
        response = self._open(method, path, body, timeout=timeout)
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise LumaError("control API returned invalid stream JSON")
                yield payload

    def _open(self, method: str, path: str, body: Dict[str, Any] | None = None, *, timeout: int = 30) -> Any:
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
            kwargs: Dict[str, Any] = {"timeout": timeout}
            if self.insecure:
                kwargs["context"] = ssl._create_unverified_context()
            return urllib.request.urlopen(req, **kwargs)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if _looks_like_legacy_node_api_error(detail):
                raise LumaError(
                    "control API is older than this CLI and still expects node join profiles. "
                    "Update the manager first: run the installer on the manager, then run "
                    "`luma update` "
                    "or rerun `luma bootstrap manager --domain <control-domain>`."
                ) from exc
            raise LumaError(f"control API error {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LumaError(_timeout_message(path, timeout)) from exc
        except urllib.error.URLError as exc:
            raise LumaError(f"control API unavailable: {exc}") from exc

    def _request_url(self, path: str) -> str:
        if not self.resolve_ip:
            return self.endpoint + path
        parsed = urllib.parse.urlparse(self.endpoint)
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{self.resolve_ip}{port}"
        return urllib.parse.urlunparse((parsed.scheme, netloc, path, "", "", ""))

    def verify_login(self) -> Dict[str, Any]:
        return self.request("POST", "/v1/auth/login/verify", {})

    def health(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/health")

    def status(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/status")

    def register_node(self, *, node_name: str, region: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/nodes/register",
            {"nodeName": node_name, "region": region},
        )

    def label_node(
        self,
        *,
        node_name: str,
        region: str,
        registered_name: str | None = None,
        node_id: str | None = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"nodeName": node_name, "region": region}
        if registered_name and registered_name != node_name:
            body["registeredName"] = registered_name
        if node_id:
            body["nodeId"] = node_id
        return self.request("POST", "/v1/nodes/label", body, timeout=120)

    def unregister_node(self, *, node_name: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/nodes/unregister", {"nodeName": node_name})

    def deploy(
        self,
        *,
        manifest: str,
        source_name: str,
        skip_dns: bool = False,
        skip_webhook: bool = False,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/deployments",
            {
                "manifest": manifest,
                "sourceName": source_name,
                "skipDns": skip_dns,
                "skipWebhook": skip_webhook,
            },
            timeout=timeout,
        )

    def deploy_events(
        self,
        *,
        manifest: str,
        source_name: str,
        skip_dns: bool = False,
        skip_webhook: bool = False,
        timeout: int = 1800,
    ) -> Iterator[Dict[str, Any]]:
        return self.stream(
            "POST",
            "/v1/deployments/stream",
            {
                "manifest": manifest,
                "sourceName": source_name,
                "skipDns": skip_dns,
                "skipWebhook": skip_webhook,
            },
            timeout=timeout,
        )

    def remove_service(
        self,
        *,
        manifest: str,
        source_name: str,
        skip_dns: bool = False,
        skip_portainer: bool = False,
        dry_run: bool = False,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/services/remove",
            {
                "manifest": manifest,
                "sourceName": source_name,
                "skipDns": skip_dns,
                "skipPortainer": skip_portainer,
                "dryRun": dry_run,
            },
            timeout=timeout,
        )

    def list_secrets(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/secrets")

    def set_secret(self, *, name: str, value: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/secrets", {"name": name, "value": value})

    def list_registries(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/registries")

    def set_registry(self, *, host: str, username: str, password: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/registries", {"host": host, "username": username, "password": password})

    def remove_registry(self, *, host: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/registries/remove", {"host": host})


def _looks_like_legacy_node_api_error(detail: str) -> bool:
    return "nodeName, profile, and region are required" in detail


def _timeout_message(path: str, timeout: int) -> str:
    message = f"control API timed out after {timeout}s waiting for {path}"
    if path == "/v1/deployments":
        message += (
            "; the manager may still be applying the deployment. "
            "Check `docker service logs -f luma-control_luma-control` and Portainer stack status, "
            "or retry with `luma deploy <service.yaml> --timeout 3600`."
        )
    return message
