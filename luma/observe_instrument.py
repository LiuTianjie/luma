"""OTel env Luma injects when luma-observe is selected.

Apps use the official OpenTelemetry distro, not a Luma SDK. Traefik keeps
sending unauthenticated OTLP to manager loopback; application tasks talk to
the mesh listener with a Control-issued bearer token.
"""
from __future__ import annotations

import secrets
from typing import Any, Mapping

OBSERVE_STACK = "luma-observe"
OTLP_MESH_PORT = 4319
TRACES_SAMPLER_RATIO = "0.1"


def mint_otlp_token() -> str:
    return secrets.token_urlsafe(32)


def manager_tailscale_ip(nodes: Mapping[str, Any] | None) -> str:
    for name, record in (nodes or {}).items():
        if not isinstance(record, dict):
            continue
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        manager = (
            str(record.get("status") or "").lower() == "manager"
            or str(name) == "manager"
            or str(labels.get("role.nomad-manager") or "").lower() == "true"
        )
        ip = str(record.get("tailscaleIP") or "").strip()
        if manager and ip:
            return ip
    return "127.0.0.1"


def otlp_mesh_endpoint(mesh_bind: str) -> str:
    bind = str(mesh_bind or "127.0.0.1").strip() or "127.0.0.1"
    return f"http://{bind}:{OTLP_MESH_PORT}"


def collector_env(*, token: str, mesh_bind: str) -> dict[str, str]:
    return {
        "LUMA_OTLP_TOKEN": str(token),
        "LUMA_OTLP_MESH_BIND": str(mesh_bind or "127.0.0.1").strip() or "127.0.0.1",
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
    return env
