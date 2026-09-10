"""OTel env Luma injects when luma-observe is selected.

Apps use the official OpenTelemetry distro, not a Luma SDK. Traefik keeps
sending unauthenticated OTLP to manager loopback; application tasks talk to
the mesh listener with a Control-issued bearer token.
"""
from __future__ import annotations

import secrets
from dataclasses import replace
from typing import Any, Mapping

from .errors import LumaError

OBSERVE_STACK = "luma-observe"
OTLP_MESH_PORT = 4319
TRACES_SAMPLER_RATIO = "0.1"


def mint_otlp_token() -> str:
    return secrets.token_urlsafe(32)


def _is_manager_node(name: str, record: Mapping[str, Any]) -> bool:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    roles = {str(value) for value in (record.get("roles") or [])}
    return (
        str(record.get("status") or "").lower() == "manager"
        or str(record.get("nomadRole") or "").lower() == "server"
        or bool(record.get("nomadServer"))
        or "nomad-manager" in roles
        or str(labels.get("role.nomad-manager") or "").lower() == "true"
    )


def _node_region(record: Mapping[str, Any]) -> str:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return str(record.get("region") or labels.get("region") or "").strip()


def manager_placement(nodes: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return (luma_node_name, region) for the Control/Traefik node."""
    fallback = ("", "")
    for name, record in (nodes or {}).items():
        if not isinstance(record, dict):
            continue
        region = _node_region(record)
        if _is_manager_node(str(name), record):
            return str(name), region
        if str(name) == "manager" and not fallback[0]:
            fallback = (str(name), region)
    return fallback


def manager_tailscale_ip(nodes: Mapping[str, Any] | None) -> str:
    fallback_ip = ""
    for name, record in (nodes or {}).items():
        if not isinstance(record, dict):
            continue
        ip = str(record.get("tailscaleIP") or "").strip()
        if _is_manager_node(str(name), record) and ip:
            return ip
        if str(name) == "manager" and ip and not fallback_ip:
            fallback_ip = ip
    return fallback_ip or "127.0.0.1"


def pin_observe_placement(deployment: Any, nodes: Mapping[str, Any] | None) -> Any:
    """Colocate luma-observe with Control/Traefik. Sidecar node/region are ignored."""
    if str(getattr(deployment, "slug", "")) != OBSERVE_STACK:
        return deployment
    node_name, region = manager_placement(nodes)
    if not node_name:
        raise LumaError(
            "luma-observe must colocate with Control and Traefik; no manager node is registered"
        )
    region = region or str(getattr(deployment, "region", "") or "")
    services = {
        name: replace(svc, node=node_name, region=region or svc.region)
        for name, svc in deployment.services.items()
    }
    volumes = {
        name: replace(vol, local_node=node_name) if vol.kind == "local" else vol
        for name, vol in deployment.volumes.items()
    }
    return replace(deployment, region=region or deployment.region, services=services, volumes=volumes)


def otlp_mesh_endpoint(mesh_bind: str) -> str:
    bind = str(mesh_bind or "127.0.0.1").strip() or "127.0.0.1"
    return f"http://{bind}:{OTLP_MESH_PORT}"


def collector_env(*, token: str, mesh_bind: str) -> dict[str, str]:
    return {
        "LUMA_OTLP_TOKEN": str(token),
        "LUMA_OTLP_MESH_BIND": str(mesh_bind or "127.0.0.1").strip() or "127.0.0.1",
    }


def grafana_server_env(domain: Any) -> dict[str, str]:
    host = str(domain or "").strip().split("/")[0].split(":")[0]
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return {}
    return {
        "GF_SERVER_DOMAIN": host,
        "GF_SERVER_ROOT_URL": f"https://{host}/grafana",
    }


def app_env(
    *,
    service_name: str,
    stack: str,
    task: str,
    region: str,
    endpoint: str,
    token: str,
) -> dict[str, str]:
    name = str(service_name or task or stack).strip() or "luma-app"
    return {
        "OTEL_SERVICE_NAME": name,
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"luma.stack={stack},luma.task={task},luma.region={region}"
        ),
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {token}",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": TRACES_SAMPLER_RATIO,
        "OTEL_EXPORTER_OTLP_TIMEOUT": "5000",
    }


def apply_env(
    env: dict[str, str],
    *,
    stack: str,
    task: str,
    region: str,
    observe_otlp: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge observe env without overwriting values the app already set."""
    if not observe_otlp:
        return env
    token = str(observe_otlp.get("token") or "")
    mesh_bind = str(observe_otlp.get("mesh_bind") or "127.0.0.1")
    endpoint = str(observe_otlp.get("endpoint") or otlp_mesh_endpoint(mesh_bind))
    if not token:
        return env
    if str(stack) == OBSERVE_STACK:
        injected = collector_env(token=token, mesh_bind=mesh_bind)
    else:
        injected = app_env(
            service_name=f"{stack}-{task}" if stack and task and stack != task else (task or stack),
            stack=stack,
            task=task,
            region=region,
            endpoint=endpoint,
            token=token,
        )
    for key, value in injected.items():
        env.setdefault(key, value)
    if str(stack) == OBSERVE_STACK and str(task) == "grafana":
        env.update(grafana_server_env(observe_otlp.get("grafana_domain")))
    return env
