from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from .config import LumaConfig
from .errors import LumaError
from .service import ServiceSpec


def deploy_with_portainer(
    config: LumaConfig,
    service: ServiceSpec,
    stack_content: str,
    state: Dict[str, Any],
    *,
    stack_env: list[dict[str, str]] | None = None,
) -> str:
    webhook_url, webhook_env = resolve_webhook(config, service)
    if not webhook_url:
        return upsert_stack(config, service, stack_content, state, missing_webhook_env=webhook_env, stack_env=stack_env)
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
    return f"Portainer webhook triggered for {service.name}: HTTP {status}"


def upsert_stack(
    config: LumaConfig,
    service: ServiceSpec,
    stack_content: str,
    state: Dict[str, Any],
    *,
    missing_webhook_env: str,
    stack_env: list[dict[str, str]] | None = None,
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
        client.request(
            "PUT",
            f"/stacks/{int(stack_id)}?{urllib.parse.urlencode({'endpointId': endpoint})}",
            {
                "StackFileContent": stack_content,
                "Env": stack_env or [],
                "Prune": True,
                "PullImage": False,
            },
            token=token,
        )
        return f"Portainer stack updated for {service.name}: {service.slug}"
    client.request(
        "POST",
        f"/stacks/create/swarm/string?{urllib.parse.urlencode({'endpointId': endpoint})}",
        {
            "Name": service.slug,
            "StackFileContent": stack_content,
            "SwarmID": swarm_id,
            "Env": stack_env or [],
        },
        token=token,
    )
    return f"Portainer stack created for {service.name}: {service.slug}"


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
    ) -> Any:
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.api_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.context) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"Portainer API error {exc.code}: {detail}") from exc
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
