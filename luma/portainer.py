from __future__ import annotations

import json
import os
import ssl
import base64
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from .config import LumaConfig
from .errors import LumaError
from .registry import normalize_registry_host, public_registry_url, registry_provider_type
from .service import ServiceSpec


def deploy_with_portainer(
    config: LumaConfig,
    service: ServiceSpec,
    stack_content: str,
    state: Dict[str, Any],
    *,
    stack_env: list[dict[str, str]] | None = None,
    registry_auth: dict[str, str] | None = None,
) -> str:
    webhook_url, webhook_env = resolve_webhook(config, service)
    if not webhook_url or registry_auth:
        return upsert_stack(
            config,
            service,
            stack_content,
            state,
            missing_webhook_env=webhook_env,
            stack_env=stack_env,
            registry_auth=registry_auth,
        )
    return trigger_webhook_url(service, webhook_url)


def trigger_webhook(config: LumaConfig, service: ServiceSpec) -> str:
    webhook_url, webhook_env = resolve_webhook(config, service)
    if not webhook_url:
        raise LumaError(f"missing Portainer webhook for {service.name}: set {webhook_env}")
    return trigger_webhook_url(service, webhook_url)


def trigger_webhook_url(service: ServiceSpec, webhook_url: str) -> str:
    req = urllib.request.Request(webhook_url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LumaError(f"Portainer webhook error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LumaError(f"Portainer webhook unavailable at {webhook_url}: {exc.reason}") from exc
    return f"Portainer webhook triggered for {service.name}: HTTP {status}"


def upsert_stack(
    config: LumaConfig,
    service: ServiceSpec,
    stack_content: str,
    state: Dict[str, Any],
    *,
    missing_webhook_env: str,
    stack_env: list[dict[str, str]] | None = None,
    registry_auth: dict[str, str] | None = None,
) -> str:
    api_url = str(state.get("portainerApiUrl") or config.portainer.get("apiUrl") or "")
    username = str(state.get("portainerAdminUsername") or config.portainer.get("adminUsername") or "admin")
    password = str(state.get("portainerAdminPassword") or "")
    endpoint_id = state.get("portainerEndpointId") or config.portainer.get("endpointId")
    swarm_id = str(state.get("swarmId") or config.portainer.get("swarmId") or "")
    if not api_url or not password or not endpoint_id or not swarm_id:
        raise LumaError(
            f"missing Portainer API binding for {service.name}: rerun luma bootstrap manager or set {missing_webhook_env}"
        )
    client = PortainerApi(api_url, username=username, password=password)
    token = client.authenticate()
    registry_id = ensure_portainer_registry(client, token, endpoint_id=int(endpoint_id), auth=registry_auth)
    stacks = client.request("GET", "/stacks", token=token)
    if not isinstance(stacks, list):
        raise LumaError("Portainer returned an invalid stack list")
    stack_id = None
    for item in stacks:
        if isinstance(item, dict) and item.get("Name") == service.slug:
            stack_id = item.get("Id")
            break
    endpoint = int(endpoint_id)
    if stack_id:
        update_body: Dict[str, Any] = {
            "StackFileContent": stack_content,
            "Env": stack_env or [],
            "Prune": True,
            "PullImage": bool(registry_id),
        }
        stack_headers = _portainer_registry_auth_header(registry_id)
        request_kwargs: Dict[str, Any] = {"token": token}
        if stack_headers:
            request_kwargs["headers"] = stack_headers
        client.request(
            "PUT",
            f"/stacks/{int(stack_id)}?{urllib.parse.urlencode({'endpointId': endpoint})}",
            update_body,
            **request_kwargs,
        )
        return f"Portainer stack updated for {service.name}: {service.slug}"
    create_body: Dict[str, Any] = {
        "Name": service.slug,
        "StackFileContent": stack_content,
        "SwarmID": swarm_id,
        "Env": stack_env or [],
    }
    stack_headers = _portainer_registry_auth_header(registry_id)
    request_kwargs = {"token": token}
    if stack_headers:
        request_kwargs["headers"] = stack_headers
    client.request(
        "POST",
        f"/stacks/create/swarm/string?{urllib.parse.urlencode({'endpointId': endpoint})}",
        create_body,
        **request_kwargs,
    )
    return f"Portainer stack created for {service.name}: {service.slug}"


def ensure_portainer_registry(
    client: "PortainerApi",
    token: str,
    *,
    endpoint_id: int,
    auth: dict[str, str] | None,
) -> int | None:
    if not auth:
        return None
    server_address = public_registry_url(auth.get("serveraddress") or "")
    host = normalize_registry_host(server_address)
    username = str(auth.get("username") or "")
    password = str(auth.get("password") or "")
    if not username or not password:
        return None
    registries = client.request("GET", "/registries", token=token)
    if not isinstance(registries, list):
        raise LumaError("Portainer returned an invalid registry list")
    luma_name = _luma_registry_name(host)
    registry_id = _luma_registry_id(registries, host)
    create_body = {
        "Name": luma_name,
        "URL": host,
        "Authentication": True,
        "Username": username,
        "Password": password,
        "Type": registry_provider_type(host),
        "TLS": True,
    }
    if registry_id is None:
        created = client.request("POST", "/registries", create_body, token=token)
        if not isinstance(created, dict) or (created.get("Id") is None and created.get("ID") is None):
            raise LumaError("Portainer registry create did not return an id")
        registry_id = int(created.get("Id") or created.get("ID"))
    else:
        update_body = {
            "Name": create_body["Name"],
            "URL": host,
            "Authentication": True,
            "Username": username,
            "Password": password,
        }
        client.request("PUT", f"/registries/{registry_id}", update_body, token=token)
    access_body = {"UserAccessPolicies": {}, "TeamAccessPolicies": {}, "Namespaces": []}
    client.request("PUT", f"/endpoints/{endpoint_id}/registries/{registry_id}", access_body, token=token)
    return registry_id


def remove_luma_portainer_registry(config: LumaConfig, state: Dict[str, Any], host: str) -> bool:
    host = normalize_registry_host(public_registry_url(host))
    api_url = str(state.get("portainerApiUrl") or config.portainer.get("apiUrl") or "")
    username = str(state.get("portainerAdminUsername") or config.portainer.get("adminUsername") or "admin")
    password = str(state.get("portainerAdminPassword") or "")
    if not api_url or not password:
        return False
    client = PortainerApi(api_url, username=username, password=password)
    token = client.authenticate()
    registries = client.request("GET", "/registries", token=token)
    if not isinstance(registries, list):
        raise LumaError("Portainer returned an invalid registry list")
    registry_id = _luma_registry_id(registries, host)
    if registry_id is None:
        return False
    client.request("DELETE", f"/registries/{registry_id}", token=token)
    return True


def _portainer_registry_auth_header(registry_id: int | None) -> dict[str, str] | None:
    if registry_id is None:
        return None
    raw = json.dumps({"registryId": registry_id}, separators=(",", ":")).encode("utf-8")
    return {"X-Registry-Auth": base64.b64encode(raw).decode("ascii")}


def _luma_registry_name(host: str) -> str:
    return f"luma-{host.replace(':', '-').replace('.', '-')}"


def _luma_registry_id(registries: list[Any], host: str) -> int | None:
    luma_name = _luma_registry_name(host)
    for item in registries:
        if not isinstance(item, dict):
            continue
        if str(item.get("Name") or "") != luma_name:
            continue
        raw_url = str(item.get("URL") or item.get("Url") or "").strip()
        if not raw_url:
            continue
        parsed_url = urllib.parse.urlparse(raw_url)
        try:
            url = normalize_registry_host(parsed_url.netloc or raw_url)
        except LumaError:
            continue
        if url == host:
            candidate_id = item.get("Id") or item.get("ID")
            if candidate_id is not None:
                return int(candidate_id)
    return None


class PortainerApi:
    def __init__(self, api_url: str, *, username: str, password: str):
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.password = password
        self.context = ssl._create_unverified_context()

    def authenticate(self) -> str:
        payload = self.request("POST", "/auth", {"Username": self.username, "Password": self.password})
        if not isinstance(payload, dict) or not payload.get("jwt"):
            raise LumaError("Portainer authentication did not return a token")
        return str(payload["jwt"])

    def request(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        *,
        token: str | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Any:
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(self.api_url + path, data=data, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.context) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"Portainer API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LumaError(
                f"Portainer API unavailable at {self.api_url}: {exc.reason}. "
                "Check that Portainer is running and that portainerApiUrl in /opt/luma/control/control.json "
                "is reachable from the luma-control container."
            ) from exc
        if not raw:
            return None
        return json.loads(raw)


def resolve_webhook(config: LumaConfig, service: ServiceSpec) -> tuple[str | None, str]:
    portainer = config.portainer
    service_portainer = service.portainer

    webhook_url = (
        service_portainer.get("webhookUrl")
        or service.dns.get("portainerWebhookUrl")
    )
    webhook_env = service_portainer.get("webhookUrlEnv")
    if not webhook_env:
        webhooks = portainer.get("webhooks") or {}
        if isinstance(webhooks, dict):
            webhook_env = webhooks.get(service.name) or webhooks.get(service.slug)
    webhook_env = str(webhook_env or portainer.get("webhookUrlEnv", "PORTAINER_WEBHOOK_URL"))
    webhook_url = webhook_url or os.environ.get(webhook_env) or portainer.get("webhookUrl")
    return webhook_url, webhook_env


def configured_webhook(config: LumaConfig, service: ServiceSpec | None = None) -> str | None:
    if service is not None:
        return resolve_webhook(config, service)[0]
    portainer = config.portainer
    webhook_url = portainer.get("webhookUrl")
    webhook_env = str(portainer.get("webhookUrlEnv", "PORTAINER_WEBHOOK_URL"))
    return webhook_url or os.environ.get(webhook_env)
