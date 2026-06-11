from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict

import yaml

from ..assets import asset_path
from ..cloudflare import delete_dns, sync_dns
from ..compose import (
    ComposeDeploymentSpec,
    DEFAULT_NFS_MOUNT_OPTIONS,
    StorageClassSpec,
    compose_public_services,
    compose_route_path,
    compose_stack_path,
    load_compose_deployment,
    render_compose_routes,
    render_compose_stack,
    storage_summary,
)
from ..config import load_config
from ..errors import LumaError
from ..io import load_yaml
from ..local import LocalExecutor
from ..portainer import deploy_with_portainer, remove_luma_portainer_registry, remove_stack, upsert_stack
from ..registry import (
    docker_registry_auth_header,
    public_registry_url,
    image_uses_mutable_latest_tag,
    normalize_registry_host,
    registry_auth_for_image,
    registry_auth_matches_image,
)
from ..render import named_volume_sources, render_stack, render_tailscale_route, render_tcp_route, route_path, stack_path
from ..service import TCP_RELAY_RESERVED_PORTS, VALID_REGIONS, ServiceSpec, load_service, slugify, tcp_entrypoint_name
from .. import __version__
from .metrics import load_history, record_samples, sustained_breach
from .state import init_state, load_state, mutate_state, require_token

AGENT_STALE_SECONDS = int(os.environ.get("LUMA_NODE_AGENT_STALE_SECONDS", "120"))
AGENT_TASK_TIMEOUT_SECONDS = int(os.environ.get("LUMA_NODE_AGENT_TASK_TIMEOUT_SECONDS", "300"))
# Sustained-breach alerting: a metric must stay above the threshold for the
# whole window before it becomes an issue, so a momentary spike does not page.
ALERT_SUSTAINED_SECONDS = int(os.environ.get("LUMA_ALERT_SUSTAINED_SECONDS", "300"))
ALERT_NODE_MEMORY_PERCENT = float(os.environ.get("LUMA_ALERT_NODE_MEMORY_PERCENT", "85"))
ALERT_NODE_CPU_PERCENT = float(os.environ.get("LUMA_ALERT_NODE_CPU_PERCENT", "90"))
TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS", "300"))
EGRESS_PROXY_URL = os.environ.get("LUMA_EGRESS_PROXY_URL", "http://127.0.0.1:7890")
EGRESS_NO_PROXY = os.environ.get(
    "LUMA_EGRESS_NO_PROXY",
    "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,docker.1panel.live,docker.m.daocloud.io,docker.1ms.run",
)
DEFAULT_EGRESS_PULL_REGISTRIES = {
    "docker.io",
    "index.docker.io",
    "registry-1.docker.io",
    "ghcr.io",
    "quay.io",
    "gcr.io",
    "k8s.gcr.io",
    "registry.k8s.io",
    "mcr.microsoft.com",
    "public.ecr.aws",
    "nvcr.io",
}


def bearer_token(headers: Any) -> str:
    value = headers.get("Authorization") or ""
    prefix = "Bearer "
    if not value.startswith(prefix):
        raise LumaError("missing bearer token")
    return value[len(prefix):].strip()


def handle_login_verify(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    return {"clusterId": state["clusterId"], "endpoint": state.get("domain", "")}


def require_control_node_token(state: Dict[str, Any], token: str) -> None:
    try:
        require_token(state, token, token_type="deploy")
        return
    except LumaError:
        pass
    require_token(state, token, token_type="join")


def _token_has_type(state: Dict[str, Any], token: str, token_type: str) -> bool:
    try:
        require_token(state, token, token_type=token_type)
        return True
    except LumaError:
        return False


def _hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _node_agent_record(record: Dict[str, Any]) -> Dict[str, Any]:
    agent = record.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        record["agent"] = agent
    return agent


def _issue_node_agent_token(state: Dict[str, Any], node_name: str, *, node_id: str = "") -> str:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if record is None:
        raise LumaError(f"node is not registered: {node_name}")
    token = secrets.token_urlsafe(32)
    agent = _node_agent_record(record)
    agent.update(
        {
            "tokenHash": _hash_agent_token(token),
            "status": str(agent.get("status") or "provisioned"),
            "updatedAt": int(time.time()),
        }
    )
    if node_id:
        record["swarmNodeId"] = node_id
    return token


def _node_record_entry_for_name_or_id(nodes: Dict[str, Any], node_name: str, node_id: str = "") -> tuple[str, Dict[str, Any]] | None:
    if node_name:
        direct = nodes.get(node_name)
        if isinstance(direct, dict):
            return node_name, direct
        for key, value in nodes.items():
            if isinstance(value, dict) and value.get("displayName") == node_name:
                return str(key), value
    for key, value in nodes.items():
        if not isinstance(value, dict):
            continue
        labels = value.get("labels") if isinstance(value.get("labels"), dict) else {}
        values = {
            str(value.get("swarmNodeId") or ""),
            str(labels.get("luma.node.id") or ""),
            str(value.get("swarmHostname") or ""),
            str(value.get("displayName") or ""),
        }
        if node_id and node_id in values:
            return str(key), value
        if node_name and node_name in values:
            return str(key), value
    return None


def _require_node_agent_token(state: Dict[str, Any], token: str, node_name: str, *, node_id: str = "") -> Dict[str, Any]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if record is None:
        raise LumaError("unauthorized")
    if node_id:
        known_id = str(record.get("swarmNodeId") or "")
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        known_label_id = str(labels.get("luma.node.id") or "")
        known_ids = {value for value in {known_id, known_label_id} if value}
        if known_ids and node_id not in known_ids:
            raise LumaError("unauthorized")
    agent = _node_agent_record(record)
    expected = str(agent.get("tokenHash") or "")
    if not expected or not secrets.compare_digest(expected, _hash_agent_token(token)):
        raise LumaError("unauthorized")
    return record


def _update_agent_heartbeat(record: Dict[str, Any], body: Dict[str, Any]) -> None:
    agent = _node_agent_record(record)
    capabilities = body.get("capabilities")
    metrics = body.get("metrics")
    container_stats = body.get("containerStats")
    agent.update(
        {
            "status": "online",
            "lastSeen": int(time.time()),
            "os": str(body.get("os") or agent.get("os") or ""),
            "capabilities": [str(value) for value in capabilities] if isinstance(capabilities, list) else agent.get("capabilities", []),
            "version": str(body.get("version") or agent.get("version") or __version__),
        }
    )
    if isinstance(metrics, dict):
        agent["metrics"] = _agent_metrics(metrics)
    if isinstance(container_stats, list):
        agent["containerStats"] = _container_stats(container_stats)


def _record_metrics_history(node_name: str, body: Dict[str, Any]) -> None:
    """Append one time-series sample for this heartbeat, outside the global
    state lock. Metrics retention must never break a heartbeat, so any failure
    is logged and swallowed."""
    try:
        metrics = body.get("metrics") if isinstance(body.get("metrics"), dict) else {}
        container_stats = body.get("containerStats") if isinstance(body.get("containerStats"), list) else []
        if not metrics and not container_stats:
            return
        record_samples(node_name, metrics, container_stats)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"metrics history record failed for {node_name or '-'}: {exc}", file=sys.stderr, flush=True)


def _agent_metrics(raw: Dict[str, Any]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    numeric_keys = {
        "cpuPercent",
        "cpuCount",
        "load1",
        "loadPercent",
        "memoryTotalBytes",
        "memoryAvailableBytes",
        "memoryUsedPercent",
    }
    for key in numeric_keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            metrics[key] = round(float(value), 1) if "Percent" in key or key == "load1" else int(value)
        elif isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                continue
            metrics[key] = round(parsed, 1) if "Percent" in key or key == "load1" else int(parsed)
    return metrics


def _container_stats(raw_items: list[Any]) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    for raw in raw_items[:250]:
        if not isinstance(raw, dict):
            continue
        service = str(raw.get("service") or "").strip()
        container_id = str(raw.get("containerId") or "").strip()
        if not service or not container_id:
            continue
        item: Dict[str, Any] = {
            "service": service,
            "containerId": container_id[:12],
            "name": str(raw.get("name") or ""),
            "taskId": str(raw.get("taskId") or ""),
        }
        for key in ("cpuPercent", "memoryPercent", "memoryUsageBytes", "memoryLimitBytes"):
            value = raw.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                item[key] = round(float(value), 2) if "Percent" in key else int(value)
            elif isinstance(value, str):
                try:
                    parsed = float(value)
                except ValueError:
                    continue
                item[key] = round(parsed, 2) if "Percent" in key else int(parsed)
        result.append(item)
    return result


def _node_agent_status(record: Dict[str, Any]) -> str:
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    status = str(agent.get("status") or "")
    last_seen = int(agent.get("lastSeen") or 0)
    if status == "online" and last_seen and int(time.time()) - last_seen <= AGENT_STALE_SECONDS:
        return "ready"
    if status == "provisioned":
        return "provisioned"
    if status:
        return "offline"
    if agent.get("tokenHash"):
        return "provisioned"
    return "missing"


def _node_agent_supports_storage(record: Dict[str, Any]) -> bool:
    return _node_agent_is_ready(record, required_capability="nfs-host")


def _node_agent_is_ready(record: Dict[str, Any], *, required_capability: str | None = None) -> bool:
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    capabilities = {str(value) for value in agent.get("capabilities") or []}
    if _node_agent_status(record) != "ready":
        return False
    return required_capability is None or required_capability in capabilities


def _agent_tasks(state: Dict[str, Any]) -> Dict[str, Any]:
    tasks = state.setdefault("agentTasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        state["agentTasks"] = tasks
    return tasks


def _mutate_control_state(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    return mutate_state(mutator)


def handle_node_agent_token(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()

    def mutate(state: Dict[str, Any]) -> tuple[str, str]:
        is_deploy_token = _token_has_type(state, token, "deploy")
        if not is_deploy_token:
            require_token(state, token, token_type="join")
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        if is_deploy_token:
            entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
        else:
            if not node_id:
                raise LumaError("nodeId is required when refreshing node agent credentials with a node join token")
            entry = _node_record_entry_for_name_or_id(nodes, "", node_id)
        if entry is None:
            raise LumaError("nodeName or nodeId must match a registered node")
        matched_name, _record = entry
        return matched_name, _issue_node_agent_token(state, matched_name, node_id=node_id)

    matched_name, agent_token = _mutate_control_state(mutate)
    state = load_state()
    return {
        "nodeName": matched_name,
        "nodeId": node_id,
        "agentToken": agent_token,
        "endpoint": state.get("domain", ""),
    }


def handle_node_agent_lease(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    if not node_name:
        raise LumaError("nodeName is required")
    leased: Dict[str, Any] | None = None
    wait_seconds = min(max(int(body.get("waitSeconds") or 0), 0), 30)
    deadline = time.time() + wait_seconds
    while True:
        def mutate(state: Dict[str, Any]) -> Dict[str, Any] | None:
            record = _require_node_agent_token(state, token, node_name, node_id=node_id)
            _update_agent_heartbeat(record, body)
            tasks = _agent_tasks(state)
            now = int(time.time())
            for task_id in sorted(tasks):
                task = tasks.get(task_id)
                if not isinstance(task, dict):
                    continue
                if task.get("nodeName") != node_name or task.get("status") != "queued":
                    continue
                task["status"] = "running"
                task["leasedAt"] = now
                task["updatedAt"] = now
                return {
                    "id": task_id,
                    "action": task.get("action"),
                    "payload": task.get("payload") if isinstance(task.get("payload"), dict) else {},
                }
            return None

        leased = _mutate_control_state(mutate)
        if leased or time.time() >= deadline:
            break
        time.sleep(1)
    _record_metrics_history(node_name, body)
    return {"task": leased}


def handle_node_agent_complete(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    task_id = str(body.get("taskId") or "").strip()
    status = str(body.get("status") or "").strip()
    if status not in {"succeeded", "failed"}:
        raise LumaError("status must be succeeded or failed")
    if not node_name or not task_id:
        raise LumaError("nodeName and taskId are required")

    def mutate(state: Dict[str, Any]) -> None:
        record = _require_node_agent_token(state, token, node_name, node_id=node_id)
        _update_agent_heartbeat(record, body)
        tasks = _agent_tasks(state)
        task = tasks.get(task_id)
        if not isinstance(task, dict) or task.get("nodeName") != node_name:
            raise LumaError(f"agent task not found: {task_id}")
        now = int(time.time())
        task.update(
            {
                "status": status,
                "message": str(body.get("message") or ""),
                "result": body.get("result") if isinstance(body.get("result"), dict) else {},
                "completedAt": now,
                "updatedAt": now,
            }
        )

    _mutate_control_state(mutate)
    _record_metrics_history(node_name, body)
    return {"taskId": task_id, "status": status}


def _run_node_agent_task(
    state: Dict[str, Any],
    node_name: str,
    action: str,
    payload: Dict[str, Any],
    *,
    timeout: int | None = None,
    required_capability: str | None = "nfs-host",
) -> Dict[str, Any]:
    task_id = f"task-{int(time.time() * 1000)}-{secrets.token_hex(4)}"

    def mutate(current: Dict[str, Any]) -> None:
        nodes = current.get("nodes") if isinstance(current.get("nodes"), dict) else {}
        record = _node_record_for_name(nodes, node_name)
        if record is None:
            raise LumaError(f"Luma node is not registered: {node_name}")
        if not _node_agent_is_ready(record, required_capability=required_capability):
            agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
            raise LumaError(
                f"node agent is not ready on {node_name}; "
                f"status={_node_agent_status(record)}, os={agent.get('os') or 'unknown'}, "
                f"capabilities={','.join(str(value) for value in agent.get('capabilities') or []) or '-'}"
            )
        now = int(time.time())
        _agent_tasks(current)[task_id] = {
            "id": task_id,
            "nodeName": node_name,
            "action": action,
            "payload": dict(payload),
            "status": "queued",
            "createdAt": now,
            "updatedAt": now,
        }

    _mutate_control_state(mutate)
    deadline = time.time() + float(timeout or AGENT_TASK_TIMEOUT_SECONDS)
    while time.time() < deadline:
        current = load_state()
        task = (current.get("agentTasks") if isinstance(current.get("agentTasks"), dict) else {}).get(task_id)
        if isinstance(task, dict):
            status = str(task.get("status") or "")
            if status == "succeeded":
                result = task.get("result") if isinstance(task.get("result"), dict) else {}
                return {"taskId": task_id, **result}
            if status == "failed":
                raise LumaError(str(task.get("message") or f"agent task failed: {task_id}"))
        time.sleep(1)
    def mark_timeout(state: Dict[str, Any]) -> None:
        tasks = _agent_tasks(state)
        task = tasks.get(task_id)
        if isinstance(task, dict) and task.get("status") in {"queued", "running"}:
            task["status"] = "timeout"
            task["message"] = "agent task timed out"
            task["updatedAt"] = int(time.time())

    mutate_state(mark_timeout)
    raise LumaError(f"node agent task timed out on {node_name}: {action}")


def handle_control_status(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    dns = config.dns
    dns_provider = str(dns.get("provider") or "not configured")
    token_env = str(dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    zone_id = os.environ.get(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID"))) or dns.get("zoneId")
    dns_target = dns.get("edgeTarget") or config.default_dns_target()
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    dns_token_configured = bool(os.environ.get(token_env) or token_env in secrets)
    dns_missing = _dns_missing_reasons(dns_provider, zone_id=zone_id, token_configured=dns_token_configured, token_env=token_env, target=dns_target)
    portainer_api_url = str(state.get("portainerApiUrl") or config.portainer.get("apiUrl") or "")
    portainer_endpoint_id = state.get("portainerEndpointId") or config.portainer.get("endpointId")
    swarm_id = str(state.get("swarmId") or config.portainer.get("swarmId") or "")
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    registered_nodes = _registered_nodes_summary(nodes)
    return {
        "clusterId": state["clusterId"],
        "version": __version__,
        "configPath": str(config_path),
        "dns": {
            "provider": dns_provider,
            "zone": str(dns.get("zone") or ""),
            "zoneIdConfigured": bool(zone_id),
            "tokenEnv": token_env,
            "tokenConfigured": dns_token_configured,
            "target": str(dns_target or ""),
            "ready": not dns_missing,
            "missing": dns_missing,
        },
        "portainer": {
            "apiUrl": _redact_url(portainer_api_url),
            "apiConfigured": bool(portainer_api_url),
            "endpointIdConfigured": bool(portainer_endpoint_id),
            "swarmIdConfigured": bool(swarm_id),
            "ready": bool(portainer_api_url and portainer_endpoint_id and swarm_id),
        },
        "nodes": {
            "registered": len(registered_nodes),
            "names": [item["name"] for item in registered_nodes],
            "items": registered_nodes,
        },
        "swarm": _swarm_nodes_summary(),
        "storage": {
            "storageClasses": _storage_classes_summary(state),
        },
    }


def handle_dashboard(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    dns = config.dns
    dns_provider = str(dns.get("provider") or "not configured")
    token_env = str(dns.get("apiTokenEnv", "CLOUDFLARE_API_TOKEN"))
    zone_id = os.environ.get(str(dns.get("zoneIdEnv", "CLOUDFLARE_ZONE_ID"))) or dns.get("zoneId")
    dns_target = str(dns.get("edgeTarget") or config.default_dns_target() or "")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    dns_token_configured = bool(os.environ.get(token_env) or token_env in secrets)
    dns_missing = _dns_missing_reasons(dns_provider, zone_id=zone_id, token_configured=dns_token_configured, token_env=token_env, target=dns_target)
    portainer_endpoint_id = state.get("portainerEndpointId") or config.portainer.get("endpointId")
    swarm_id = str(state.get("swarmId") or config.portainer.get("swarmId") or "")
    errors: list[str] = []

    raw_nodes = _dashboard_docker_list("/nodes", "nodes", errors)
    node_by_id = _dashboard_node_map(raw_nodes)
    registered_nodes = _registered_nodes_summary(state.get("nodes") if isinstance(state.get("nodes"), dict) else {})
    nodes = _dashboard_nodes(registered_nodes, raw_nodes)

    raw_services = _dashboard_docker_list("/services", "services", errors)
    raw_tasks = _dashboard_docker_list("/tasks", "tasks", errors)
    route_files = _dashboard_route_files(config, config_path, errors)
    services = _dashboard_services(raw_services, raw_tasks, node_by_id, route_files)
    service_stats = _service_stats_by_name(registered_nodes)
    for service in services:
        _attach_service_actual_resources(service, service_stats.get(str(service.get("fullName") or ""), []))
    traffic_paths = _dashboard_traffic_paths(services, route_files, dns_target)
    storage = _dashboard_storage(services, _storage_classes_summary(state))
    public_services = [_public_dashboard_service(item) for item in services]
    issues = _dashboard_issues(nodes, public_services)

    return {
        "cluster": {
            "id": str(state.get("clusterId") or ""),
            "version": __version__,
            "configPath": str(config_path),
        },
        "readiness": {
            "dns": {
                "ready": not dns_missing,
                "provider": dns_provider,
                "zone": str(dns.get("zone") or ""),
                "target": dns_target,
                "missing": dns_missing,
            },
            "portainer": {
                "ready": bool((state.get("portainerApiUrl") or config.portainer.get("apiUrl")) and portainer_endpoint_id and swarm_id),
                "apiConfigured": bool(state.get("portainerApiUrl") or config.portainer.get("apiUrl")),
                "endpointConfigured": bool(portainer_endpoint_id),
            },
            "swarm": {
                "available": bool(raw_nodes),
            },
        },
        "nodes": nodes,
        "services": public_services,
        "trafficPaths": traffic_paths,
        "storage": storage,
        "issues": issues,
        "errors": errors,
    }


def handle_dashboard_logs(token: str, service_name: str, *, tail: int = 120, since: str = "") -> Dict[str, Any]:
    require_token(load_state(), token, token_type="deploy")
    service = service_name.strip()
    if not service:
        raise LumaError("service is required")
    tail = min(max(int(tail or 120), 1), 500)
    query_values: Dict[str, Any] = {"stdout": 1, "stderr": 1, "timestamps": 1, "tail": tail}
    if since:
        query_values["since"] = since
    query = urllib.parse.urlencode(query_values)
    status, raw = docker_request_bytes("GET", f"/services/{urllib.parse.quote(service, safe='')}/logs?{query}")
    if status >= 400:
        raise LumaError(f"Docker service logs unavailable for {service}: {raw.decode('utf-8', errors='replace')}")
    return {
        "service": service,
        "logs": _decode_docker_log_lines(raw)[-tail:],
        "tail": tail,
        "since": since,
        "updatedAt": int(time.time()),
    }


def handle_metrics_history(token: str, kind: str, name: str, *, window: int = 3600) -> Dict[str, Any]:
    require_token(load_state(), token, token_type="deploy")
    if kind not in {"node", "service"}:
        raise LumaError("kind must be node or service")
    if not str(name or "").strip():
        raise LumaError("name is required")
    window = min(max(int(window or 3600), 60), 7 * 24 * 3600)
    series = load_history(kind, name, window=window)
    return {
        "kind": kind,
        "name": name,
        "window": window,
        "series": series,
        "updatedAt": int(time.time()),
    }


def _dns_missing_reasons(dns_provider: str, *, zone_id: object, token_configured: bool, token_env: str, target: object) -> list[str]:
    missing: list[str] = []
    if dns_provider != "cloudflare":
        missing.append("provider")
    if not zone_id:
        missing.append("zoneId")
    if not token_configured:
        missing.append(f"token:{token_env}")
    if not target:
        missing.append("target")
    return missing


def handle_node_register(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not region:
        raise LumaError("nodeName and region are required")
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="join")
        _remember_node(state, node_name, region=region, status="registered")

    mutate_state(mutate)
    state = load_state()
    return {
        "clusterId": state["clusterId"],
        "managerAddr": state.get("managerAddr", ""),
        "swarmJoinToken": state.get("swarmJoinToken", ""),
        "nodeName": node_name,
        "region": region,
    }


def handle_node_label(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    registered_name = str(body.get("registeredName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    tailscale_ip = str(body.get("tailscaleIP") or "").strip()
    tailscale_name = str(body.get("tailscaleName") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not region:
        raise LumaError("nodeName and region are required")
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    luma_name = registered_name or node_name
    labels = labels_for_node(region, luma_name=luma_name, node_id=node_id)
    label_swarm_node(node_name, labels, node_id=node_id)
    values: Dict[str, Any] = {
        "region": region,
        "status": "labeled",
        "labels": labels,
        "displayName": luma_name,
        "swarmHostname": node_name,
    }
    if node_id:
        values["swarmNodeId"] = node_id
    if tailscale_ip:
        values["tailscaleIP"] = tailscale_ip
    if tailscale_name:
        values["tailscaleName"] = tailscale_name
    def mutate(state: Dict[str, Any]) -> str:
        require_token(state, token, token_type="join")
        _remember_node(state, luma_name, **values)
        return _issue_node_agent_token(state, luma_name, node_id=node_id)

    agent_token = mutate_state(mutate)
    state = load_state()
    return {
        "clusterId": state["clusterId"],
        "nodeName": luma_name,
        "swarmHostname": node_name,
        "swarmNodeId": node_id,
        "displayName": luma_name,
        "tailscaleIP": tailscale_ip,
        "tailscaleName": tailscale_name,
        "agentToken": agent_token,
        "labels": labels,
        "message": f"Node labels applied: {luma_name}",
    }


def handle_node_unregister(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_control_node_token(state, token)
    node_name = str(body.get("nodeName") or "").strip()
    if not node_name:
        raise LumaError("nodeName is required")
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    removed = nodes.get(node_name)
    removed_key = node_name if removed is not None else ""
    if removed is None:
        for key, value in nodes.items():
            if isinstance(value, dict) and value.get("displayName") == node_name:
                removed = value
                removed_key = str(key)
                break
    swarm_result = remove_swarm_node(node_name, removed if isinstance(removed, dict) else None)
    def mutate(state: Dict[str, Any]) -> None:
        require_control_node_token(state, token)
        nodes = state.get("nodes")
        if not isinstance(nodes, dict):
            nodes = {}
            state["nodes"] = nodes
        if removed_key:
            nodes.pop(removed_key, None)
        else:
            nodes.pop(node_name, None)

    mutate_state(mutate)
    registered_removed = bool(removed)
    swarm_removed = bool(swarm_result.get("removed"))
    if registered_removed and swarm_removed:
        message = f"Node removed: {node_name}; Swarm node removed: {swarm_result.get('nodeId')}"
    elif registered_removed:
        message = f"Node removed: {node_name}; {swarm_result.get('message')}"
    elif swarm_removed:
        message = f"Node not registered: {node_name}; Swarm node removed: {swarm_result.get('nodeId')}"
    else:
        message = f"Node not registered: {node_name}; {swarm_result.get('message')}"
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "removed": registered_removed or swarm_removed,
        "registeredRemoved": registered_removed,
        "swarmRemoved": swarm_removed,
        "swarmNodeId": str(swarm_result.get("nodeId") or ""),
        "message": message,
    }


def _load_service_manifest(manifest: str) -> ServiceSpec:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
        fh.write(manifest)
        service_path = Path(fh.name)
    try:
        return load_service(service_path)
    finally:
        service_path.unlink(missing_ok=True)


def _deployments_state(state: Dict[str, Any]) -> Dict[str, Any]:
    deployments = state.get("deployments")
    if not isinstance(deployments, dict):
        deployments = {}
        state["deployments"] = deployments
    for key in ("services", "compose"):
        if not isinstance(deployments.get(key), dict):
            deployments[key] = {}
    return deployments


def _ensure_deployment_slug_available(state: Dict[str, Any], kind: str, slug: str, name: str) -> None:
    deployments = _deployments_state(state)
    for bucket_kind in ("services", "compose"):
        existing = deployments[bucket_kind].get(slug)
        if not isinstance(existing, dict):
            continue
        existing_name = str(existing.get("name") or slug)
        existing_kind = "service" if bucket_kind == "services" else "compose"
        if existing_kind != kind or existing_name != name:
            raise LumaError(f"deployment name already exists: {existing_name}")


def _service_tcp_relay_ports(service: ServiceSpec) -> list[int]:
    if service.exposure != "tcp-relay":
        return []
    port = int(service.publish_port or service.port or 0)
    return [port] if port > 0 else []


def _compose_tcp_relay_ports(deployment: ComposeDeploymentSpec) -> list[int]:
    return [
        int(service.publish_port or service.port or 0)
        for service in deployment.services.values()
        if service.exposure == "tcp-relay" and int(service.publish_port or service.port or 0) > 0
    ]


def _ensure_tcp_relay_ports_available(state: Dict[str, Any], *, kind: str, slug: str, ports: list[int]) -> None:
    wanted = [int(port) for port in ports if int(port) > 0]
    duplicates = sorted({port for port in wanted if wanted.count(port) > 1})
    if duplicates:
        raise LumaError(f"tcp-relay publishPort must be unique in one deployment: {duplicates}")
    reserved = sorted(set(wanted) & TCP_RELAY_RESERVED_PORTS)
    if reserved:
        raise LumaError(f"tcp-relay cannot use reserved Traefik ports: {reserved}")
    deployments = _deployments_state(state)
    conflicts: list[str] = []
    for bucket_kind, records in (("service", deployments["services"]), ("compose", deployments["compose"])):
        for record_slug, record in records.items():
            if bucket_kind == kind and str(record_slug) == slug:
                continue
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "")
            if status and status not in {"active", "pending"}:
                continue
            used_ports = set(_record_tcp_relay_ports(record))
            overlap = sorted(set(wanted) & used_ports)
            if overlap:
                conflicts.append(f"{record.get('name') or record_slug}: {overlap}")
    if conflicts:
        raise LumaError("tcp-relay publishPort conflicts with existing deployment: " + "; ".join(conflicts))


def _record_tcp_relay_ports(record: Dict[str, Any]) -> list[int]:
    ports: set[int] = set()
    for value in record.get("tcpRelayPorts") or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            ports.add(parsed)
    if ports:
        return sorted(ports)
    manifest = str(record.get("manifest") or "")
    if not manifest.strip():
        return []
    try:
        data = yaml.safe_load(manifest) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    if str(record.get("kind") or "") == "service":
        if str(data.get("exposure") or "") != "tcp-relay":
            return []
        port = data.get("publishPort") or data.get("port")
        try:
            parsed = int(port)
        except (TypeError, ValueError):
            return []
        return [parsed] if parsed > 0 else []
    services = data.get("services") if isinstance(data.get("services"), dict) else {}
    for service in services.values():
        if not isinstance(service, dict) or str(service.get("exposure") or "") != "tcp-relay":
            continue
        port = service.get("publishPort") or service.get("port")
        try:
            parsed = int(port)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            ports.add(parsed)
    return sorted(ports)


def _register_service_deployment(state: Dict[str, Any], service: ServiceSpec, manifest: str, source_name: str) -> None:
    deployments = _deployments_state(state)
    deployments["services"][service.slug] = {
        "kind": "service",
        "name": service.name,
        "slug": service.slug,
        "manifest": manifest,
        "sourceName": source_name,
        "tcpRelayPorts": _service_tcp_relay_ports(service),
        "updatedAt": int(time.time()),
    }


def _mark_service_deployment(
    service: ServiceSpec,
    manifest: str,
    source_name: str,
    *,
    status: str,
    steps: list[dict[str, str]] | None = None,
    error: str = "",
) -> None:
    def mutate(state: Dict[str, Any]) -> None:
        _register_service_deployment(state, service, manifest, source_name)
        record = _deployments_state(state)["services"][service.slug]
        record["status"] = status
        record["lastError"] = error
        record["steps"] = list(steps or [])
        record["updatedAt"] = int(time.time())

    mutate_state(mutate)


def _register_compose_deployment(state: Dict[str, Any], deployment: ComposeDeploymentSpec, body: Dict[str, Any], source_name: str) -> None:
    deployments = _deployments_state(state)
    record: Dict[str, Any] = {
        "kind": "compose",
        "name": deployment.name,
        "slug": deployment.slug,
        "manifest": str(body.get("manifest") or ""),
        "sourceName": source_name,
        "tcpRelayPorts": _compose_tcp_relay_ports(deployment),
        "updatedAt": int(time.time()),
    }
    compose_content = body.get("composeContent")
    if isinstance(compose_content, str):
        record["composeContent"] = compose_content
    deployments["compose"][deployment.slug] = record


def _mark_compose_deployment(
    deployment: ComposeDeploymentSpec,
    body: Dict[str, Any],
    source_name: str,
    *,
    status: str,
    steps: list[dict[str, str]] | None = None,
    error: str = "",
) -> None:
    def mutate(state: Dict[str, Any]) -> None:
        _register_compose_deployment(state, deployment, body, source_name)
        record = _deployments_state(state)["compose"][deployment.slug]
        record["status"] = status
        record["lastError"] = error
        record["steps"] = list(steps or [])
        record["updatedAt"] = int(time.time())

    mutate_state(mutate)


def _service_deployment_record(state: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    services = _deployments_state(state)["services"]
    record = services.get(slugify(name))
    return record if isinstance(record, dict) else None


def _compose_deployment_record(state: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    compose = _deployments_state(state)["compose"]
    record = compose.get(slugify(name))
    return record if isinstance(record, dict) else None


def handle_deployment_config(token: str, name: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    wanted = str(name or "").strip()
    if not wanted:
        raise LumaError("deployment name is required")
    record = _service_deployment_record(state, wanted) or _compose_deployment_record(state, wanted)
    if not record:
        raise LumaError(f"deployment not found: {wanted}")
    return {
        "kind": str(record.get("kind") or ""),
        "name": str(record.get("name") or wanted),
        "slug": str(record.get("slug") or slugify(wanted)),
        "sourceName": str(record.get("sourceName") or ""),
        "updatedAt": record.get("updatedAt") or 0,
        "manifest": str(record.get("manifest") or ""),
        "composeContent": str(record.get("composeContent") or ""),
    }


def _live_service_remove_request(config: Any, config_path: Path, name: str) -> tuple[ServiceSpec, str] | None:
    service = _live_service_for_remove(config, config_path, name)
    if not service:
        return None
    return service, f"live:{service.slug}"


def _live_service_for_remove(config: Any, config_path: Path, name: str) -> ServiceSpec | None:
    try:
        raw_services = docker_request("GET", "/services")
    except LumaError as exc:
        raise LumaError(f"deployment not found in Luma state and Docker services are unavailable: {exc}") from exc
    if not isinstance(raw_services, list):
        raise LumaError("Docker API returned invalid service list")
    route_files = _dashboard_route_files(config, config_path, [])
    items = [
        _dashboard_service(service, [], {}, route_files)
        for service in raw_services
        if isinstance(service, dict)
    ]
    wanted = slugify(name)
    matches = [
        item
        for item in items
        if item and _live_service_matches(item, wanted)
    ]
    if not matches:
        return None
    stacks = {
        str(item.get("stack") or item.get("name") or "").strip()
        for item in matches
        if str(item.get("stack") or item.get("name") or "").strip()
    }
    if len(stacks) > 1:
        choices = ", ".join(sorted(stacks))
        raise LumaError(f"multiple live deployments match {name}: {choices}; remove by exact stack name")
    stack_name = next(iter(stacks), "")
    if not stack_name:
        return None
    if _is_system_stack(stack_name):
        raise LumaError(f"system stack cannot be removed through service remove: {stack_name}")
    public_items = [item for item in matches if str(item.get("domain") or "").strip()]
    if len({str(item.get("domain") or "") for item in public_items}) > 1:
        raise LumaError(f"live deployment {stack_name} has multiple public services; remove it from Portainer or redeploy it through Luma first")
    item = public_items[0] if public_items else matches[0]
    exposure = str(item.get("exposure") or "none")
    domain = str(item.get("domain") or "").strip() or None
    port_text = str(item.get("targetPort") or "").strip()
    port = int(port_text) if port_text.isdigit() else None
    return ServiceSpec(
        source=Path("live"),
        name=stack_name,
        image=str(item.get("image") or "live"),
        region=str(item.get("region") or "cn"),
        node=str(item.get("node") or "") or None,
        public=exposure != "none",
        exposure=exposure,
        domain=domain,
        port=port,
        replicas=int(item.get("desired") or 1),
    )


def _live_service_matches(item: Dict[str, Any], wanted: str) -> bool:
    values = [
        item.get("stack"),
        item.get("name"),
        item.get("fullName"),
        item.get("routeId"),
    ]
    return any(slugify(str(value)) == wanted for value in values if str(value or "").strip())


def _remove_request_name(body: Dict[str, Any]) -> str:
    name = str(body.get("name") or "").strip()
    if not name:
        raise LumaError("service name is required")
    return name


def _service_remove_request(record: Dict[str, Any], name: str) -> tuple[ServiceSpec, str]:
    manifest = record.get("manifest")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError(f"service deployment manifest is missing: {name}")
    return _load_service_manifest(manifest), str(record.get("sourceName") or f"{name}.yaml")


def _compose_remove_request(record: Dict[str, Any], name: str) -> tuple[ComposeDeploymentSpec, str]:
    manifest = record.get("manifest")
    compose_content = record.get("composeContent")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError(f"compose deployment manifest is missing: {name}")
    if not isinstance(compose_content, str) or not compose_content.strip():
        raise LumaError(f"compose deployment content is missing: {name}")
    source_name = str(record.get("sourceName") or "luma.compose.yml")
    return _load_compose_request({"manifest": manifest, "composeContent": compose_content}, source_name), source_name


def _forget_service_deployment(state: Dict[str, Any], service: ServiceSpec) -> bool:
    services = _deployments_state(state)["services"]
    return services.pop(service.slug, None) is not None


def _forget_compose_deployment(state: Dict[str, Any], deployment: ComposeDeploymentSpec) -> bool:
    compose = _deployments_state(state)["compose"]
    return compose.pop(deployment.slug, None) is not None


def handle_deployment(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    manifest = body.get("manifest")
    source_name = str(body.get("sourceName") or "service.yaml")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    service = _load_service_manifest(manifest)
    _ensure_deployment_slug_available(state, "service", service.slug, service.name)
    parse_step = {"name": "Parse manifest", "status": "ok", "message": f"{service.name} -> {service.region}/{service.exposure}"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    _mark_service_deployment(service, manifest, source_name, status="pending", steps=steps)

    try:
        if image_pull_requires_egress(service.image) or _registry_auth_for_service(state, service):
            _deploy_step(
                steps,
                "Prepare image pull network",
                lambda: ensure_image_pull_network(state, service.image),
                progress=progress,
            )

        registry_auth = _registry_auth_for_service(state, service)
        service, image_result = _deploy_step(
            steps,
            "Resolve image",
            lambda: resolve_service_image(config, service, registry_auth=registry_auth),
            progress=progress,
        )
        service = _deploy_step(steps, "Resolve node pin", lambda: resolve_service_node_pin(service, state), progress=progress)
        _deploy_step(
            steps,
            "Check TCP relay ports",
            lambda: _ensure_tcp_relay_ports_available(state, kind="service", slug=service.slug, ports=_service_tcp_relay_ports(service)) or "TCP relay ports available",
            progress=progress,
        )
        _mark_service_deployment(service, manifest, source_name, status="pending", steps=steps)
        storage_preparation = _deploy_step(
            steps,
            "Prepare managed storage",
            lambda: _prepare_service_managed_storage(service, state),
            progress=progress,
        )
        target = _resolve_control_path(stack_path(config, service), config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stack_text = _deploy_step(
            steps,
            "Render stack",
            lambda: render_stack(
                config,
                service,
                storage_classes=_state_storage_classes(state),
                node_records=_state_nodes(state),
            ),
            progress=progress,
        )
        stack_env = _deploy_step(steps, "Resolve stack secrets", lambda: _stack_env_for_text(stack_text), progress=progress)
        _deploy_step(steps, "Write stack", lambda: target.write_text(stack_text, encoding="utf-8"), progress=progress)
        written = [str(target)]
        if service.exposure == "tcp-relay":
            _deploy_step(
                steps,
                "Ensure TCP ingress",
                lambda: _ensure_tcp_relay_ingress([int(service.publish_port or service.port or 0)]) if not body.get("skipPortainer") else "TCP ingress skipped: --skip-portainer",
                progress=progress,
            )
        dns_result = _deploy_step(steps, "Sync DNS", lambda: "DNS skipped: --skip-dns" if body.get("skipDns") else sync_dns(config, service), progress=progress)
        portainer_result = _deploy_step(
            steps,
            "Deploy Portainer stack",
            lambda: "Portainer deploy skipped: --skip-portainer"
            if body.get("skipPortainer")
            else deploy_with_portainer(config, service, stack_text, state, stack_env=stack_env, registry_auth=registry_auth),
            progress=progress,
        )
        if service.exposure in {"tailscale-relay", "tcp-relay"}:
            route_target = _resolve_control_path(route_path(config, service), config_path)
            route_target.parent.mkdir(parents=True, exist_ok=True)
            route_service = service
            relay_is_explicit = bool(service.relay.get("url") or service.relay.get("host")) if service.exposure == "tailscale-relay" else bool(service.tcp.get("address") or service.tcp.get("host"))
            if body.get("skipPortainer") and not relay_is_explicit:
                _deploy_step(steps, "Write route", lambda: f"Route skipped: --skip-portainer requires deploy to infer {service.exposure}", progress=progress)
            else:
                if not body.get("skipPortainer"):
                    route_service = _deploy_step(
                        steps,
                        "Resolve relay",
                        lambda: resolve_tailscale_relay(service) if service.exposure == "tailscale-relay" else resolve_tcp_relay(service),
                        progress=progress,
                    )
                route_text = render_tailscale_route(config, route_service) if service.exposure == "tailscale-relay" else render_tcp_route(config, route_service)
                _deploy_step(steps, "Write route", lambda: route_target.write_text(route_text, encoding="utf-8"), progress=progress)
                written.append(str(route_target))
        probe_result = _deploy_step(
            steps,
            "Probe public route",
            lambda: "Public route probe skipped: --skip-portainer" if body.get("skipPortainer") else _probe_public_route(service),
            progress=progress,
        )
    except LumaError as exc:
        _mark_service_deployment(service, manifest, source_name, status="failed_partial", steps=steps, error=str(exc))
        raise
    _mark_service_deployment(service, manifest, source_name, status="active", steps=steps)
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "written": written,
        "image": image_result,
        "dns": dns_result,
        "portainer": portainer_result,
        "probe": probe_result,
        "storagePreparation": storage_preparation,
        "steps": steps,
    }


def handle_deployment_preview(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    manifest = body.get("manifest")
    source_name = str(body.get("sourceName") or "service.yaml")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
        fh.write(manifest)
        service_path = Path(fh.name)
    try:
        service = load_service(service_path)
    finally:
        service_path.unlink(missing_ok=True)
    service = resolve_service_node_pin(service, state)
    stack_text = render_stack(
        config,
        service,
        storage_classes=_state_storage_classes(state),
        node_records=_state_nodes(state),
    )
    stack_env = _stack_env_for_text(stack_text)
    artifacts = [
        {
            "kind": "stack",
            "path": str(stack_path(config, service)),
            "content": stack_text,
        }
    ]
    if service.exposure in {"tailscale-relay", "tcp-relay"}:
        artifacts.append(
            {
                "kind": "route",
                "path": str(route_path(config, service)),
                "content": render_tailscale_route(config, service) if service.exposure == "tailscale-relay" else render_tcp_route(config, service),
            }
        )
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "summary": {
            "name": service.name,
            "image": service.image,
            "region": service.region,
            "node": service.node or "",
            "exposure": service.exposure,
            "domain": service.domain or "",
            "port": service.port,
            "replicas": service.replicas,
            "proxy": service.proxy,
            "secrets": [item["name"] for item in stack_env],
        },
        "artifacts": artifacts,
        "warnings": [],
    }


def handle_service_remove(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    name = _remove_request_name(body)
    if body.get("deleteStorage") and body.get("skipPortainer"):
        raise LumaError("--delete-storage cannot be combined with --skip-portainer")
    service_record = _service_deployment_record(state, name)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    if service_record:
        service, source_name = _service_remove_request(service_record, name)
        return _remove_service_deployment(state, config, config_path, service, source_name, body, progress=progress)
    compose_record = _compose_deployment_record(state, name)
    if compose_record:
        deployment, source_name = _compose_remove_request(compose_record, name)
        return _remove_compose_deployment(state, config, config_path, deployment, source_name, body, progress=progress)
    live_request = _live_service_remove_request(config, config_path, name)
    if live_request:
        service, source_name = live_request
        return _remove_service_deployment(state, config, config_path, service, source_name, body, progress=progress)
    raise LumaError(f"deployment not found: {name}")


def _remove_service_deployment(
    state: Dict[str, Any],
    config: Any,
    config_path: Path,
    service: ServiceSpec,
    source_name: str,
    body: Dict[str, Any],
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    steps: list[dict[str, str]] = []
    parse_step = {"name": "Parse manifest", "status": "ok", "message": f"{service.name} -> {service.region}/{service.exposure}"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)

    dry_run = bool(body.get("dryRun"))
    stack_target = _generated_stack_remove_target(config, service, config_path)
    route_target = _resolve_control_path(route_path(config, service), config_path) if service.exposure in {"tailscale-relay", "tcp-relay"} else None
    files = [str(stack_target)]
    if route_target:
        files.append(str(route_target))
    storage_task_nodes = _service_task_nodes(service) if body.get("deleteStorage") and _service_docker_volume_names(service) and not dry_run else []

    dns_result = _deploy_step(
        steps,
        "Delete DNS",
        lambda: "DNS skipped: --skip-dns"
        if body.get("skipDns")
        else (_planned_delete_dns_message(service) if dry_run else delete_dns(config, service)),
        progress=progress,
    )
    portainer_result = _deploy_step(
        steps,
        "Remove Portainer stack",
        lambda: "Portainer remove skipped: --skip-portainer"
        if body.get("skipPortainer")
        else (_planned_remove_message("Portainer stack would be removed", service.slug) if dry_run else remove_stack(config, service, state)),
        progress=progress,
    )
    files_result = _deploy_step(
        steps,
        "Delete generated files",
        lambda: _planned_remove_message("Generated files would be removed", ", ".join(files))
        if dry_run
        else _remove_generated_files(stack_target, route_target),
        progress=progress,
    )
    storage_cleanup = _deploy_step(
        steps,
        "Delete storage",
        lambda: _cleanup_service_storage(service, state, dry_run=dry_run, task_nodes=storage_task_nodes)
        if body.get("deleteStorage")
        else "Storage cleanup skipped",
        progress=progress,
    )
    if not dry_run and not body.get("skipPortainer"):
        def forget(state: Dict[str, Any]) -> None:
            _forget_service_deployment(state, service)

        mutate_state(forget)
    return {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "files": files,
        "dns": dns_result,
        "portainer": portainer_result,
        "generatedFiles": files_result,
        "storageCleanup": storage_cleanup,
        "dryRun": dry_run,
        "steps": steps,
    }


def _remove_compose_deployment(
    state: Dict[str, Any],
    config: Any,
    config_path: Path,
    deployment: ComposeDeploymentSpec,
    source_name: str,
    body: Dict[str, Any],
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    steps: list[dict[str, str]] = []
    parse_step = {"name": "Parse compose deployment", "status": "ok", "message": deployment.name}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    dry_run = bool(body.get("dryRun"))
    stack_target = _resolve_control_path(compose_stack_path(config, deployment), config_path).parent
    route_targets = [
        _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
        for service_name, service in deployment.services.items()
        if service.exposure in {"tailscale-relay", "tcp-relay"}
    ]
    files = [str(stack_target), *[str(path) for path in route_targets]]
    dns_results = []
    for service in compose_public_services(deployment):
        service_spec = _compose_service_as_service_spec(deployment, service)
        dns_results.append(
            _deploy_step(
                steps,
                f"Delete DNS {service.name}",
                lambda service_spec=service_spec: "DNS skipped: --skip-dns"
                if body.get("skipDns")
                else (_planned_delete_dns_message(service_spec) if dry_run else delete_dns(config, service_spec)),
                progress=progress,
            )
        )
    portainer_result = _deploy_step(
        steps,
        "Remove Portainer stack",
        lambda: "Portainer remove skipped: --skip-portainer"
        if body.get("skipPortainer")
        else (_planned_remove_message("Portainer stack would be removed", deployment.slug) if dry_run else remove_stack(config, _stack_service_spec(deployment), state)),
        progress=progress,
    )
    files_result = _deploy_step(
        steps,
        "Delete generated files",
        lambda: _planned_remove_message("Generated files would be removed", ", ".join(files))
        if dry_run
        else _remove_generated_files(stack_target, *route_targets),
        progress=progress,
    )
    storage_cleanup = _deploy_step(
        steps,
        "Delete storage",
        lambda: _cleanup_compose_managed_storage(deployment, state, dry_run=dry_run)
        if body.get("deleteStorage")
        else "Storage cleanup skipped",
        progress=progress,
    )
    if not dry_run and not body.get("skipPortainer"):
        def forget(state: Dict[str, Any]) -> None:
            _forget_compose_deployment(state, deployment)

        mutate_state(forget)
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "files": files,
        "dns": dns_results,
        "portainer": portainer_result,
        "generatedFiles": files_result,
        "storageCleanup": storage_cleanup,
        "dryRun": dry_run,
        "steps": steps,
    }


SYSTEM_STACKS = {"traefik", "portainer", "egress", "luma-control"}


def handle_application_restart(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    stack = str(body.get("stack") or "").strip()
    service_name = str(body.get("service") or "").strip()
    if not stack:
        raise LumaError("stack is required")
    if _is_system_stack(stack):
        raise LumaError(f"system stack cannot be restarted from application management: {stack}")
    services = docker_request("GET", "/services")
    if not isinstance(services, list):
        raise LumaError("Docker API returned invalid service list")
    targets = []
    for service in services:
        if not isinstance(service, dict):
            continue
        spec = service.get("Spec") if isinstance(service.get("Spec"), dict) else {}
        full_name = str(spec.get("Name") or service.get("ID") or "")
        item_stack, item_service = _split_swarm_service_name(full_name)
        if item_stack != stack:
            continue
        if service_name and item_service != service_name:
            continue
        service_id = str(service.get("ID") or "")
        if service_id:
            targets.append({"id": service_id, "name": full_name, "service": item_service})
    if not targets:
        suffix = f"/{service_name}" if service_name else ""
        raise LumaError(f"application service not found: {stack}{suffix}")
    restarted = []
    for target in targets:
        restarted.append(_force_update_service(str(target["id"]), str(target["name"])))
    return {
        "clusterId": state["clusterId"],
        "stack": stack,
        "service": service_name,
        "restarted": restarted,
    }


def _is_system_stack(stack: str) -> bool:
    return stack in SYSTEM_STACKS or stack.startswith("luma-storage")


def _force_update_service(service_id: str, display_name: str) -> Dict[str, Any]:
    inspected = docker_request("GET", f"/services/{urllib.parse.quote(service_id, safe='')}")
    if not isinstance(inspected, dict):
        raise LumaError(f"Docker API returned invalid service detail: {display_name}")
    version = inspected.get("Version", {}).get("Index")
    spec = inspected.get("Spec")
    if not version or not isinstance(spec, dict):
        raise LumaError(f"Docker API returned invalid service spec: {display_name}")
    task_template = spec.setdefault("TaskTemplate", {})
    if not isinstance(task_template, dict):
        raise LumaError(f"Docker API returned invalid task template: {display_name}")
    task_template["ForceUpdate"] = int(task_template.get("ForceUpdate") or 0) + 1
    docker_request(
        "POST",
        f"/services/{urllib.parse.quote(service_id, safe='')}/update?version={version}",
        spec,
    )
    return {"id": service_id, "name": display_name, "forceUpdate": task_template["ForceUpdate"]}


def handle_compose_deployment(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    _ensure_deployment_slug_available(state, "compose", deployment.slug, deployment.name)
    _ensure_tcp_relay_ports_available(state, kind="compose", slug=deployment.slug, ports=_compose_tcp_relay_ports(deployment))
    parse_step = {"name": "Parse compose deployment", "status": "ok", "message": f"{deployment.name} ({len(deployment.compose.get('services', {}))} services)"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    _emit_compose_warnings(steps, progress, deployment)
    _mark_compose_deployment(deployment, body, source_name, status="pending", steps=steps)

    try:
        storage_preparation = _deploy_step(
            steps,
            "Prepare managed storage",
            lambda: _prepare_compose_managed_storage(deployment, state),
            progress=progress,
        )
        target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stack_text = _deploy_step(
            steps,
            "Render compose stack",
            lambda: render_compose_stack(
                config,
                deployment,
                node_id_resolver=_compose_node_id_resolver(state),
                node_records=_state_nodes(state),
            ),
            progress=progress,
        )
        _deploy_step(steps, "Check storage migration", lambda: _guard_compose_storage_switch(target, stack_text, deployment), progress=progress)
        stack_env = _deploy_step(steps, "Resolve stack secrets", lambda: _stack_env_for_text(stack_text), progress=progress)
        _deploy_step(steps, "Write compose stack", lambda: target.write_text(stack_text, encoding="utf-8"), progress=progress)
        written = [str(target)]
        tcp_ports = [
            int(service.publish_port or service.port or 0)
            for service in deployment.services.values()
            if service.exposure == "tcp-relay"
        ]
        if tcp_ports:
            _deploy_step(
                steps,
                "Ensure TCP ingress",
                lambda: _ensure_tcp_relay_ingress(tcp_ports) if not body.get("skipPortainer") else "TCP ingress skipped: --skip-portainer",
                progress=progress,
            )

        dns_results: list[str] = []
        for service in compose_public_services(deployment):
            service_spec = _compose_service_as_service_spec(deployment, service)
            dns_results.append(
                _deploy_step(
                    steps,
                    f"Sync DNS {service.name}",
                    lambda service_spec=service_spec: "DNS skipped: --skip-dns" if body.get("skipDns") else sync_dns(config, service_spec),
                    progress=progress,
                )
            )

        portainer_result = _deploy_step(
            steps,
            "Deploy Portainer stack",
            lambda: "Portainer deploy skipped: --skip-portainer"
            if body.get("skipPortainer")
            else upsert_stack(
                config,
                _stack_service_spec(deployment),
                stack_text,
                state,
                stack_env=stack_env,
                registry_auth=None,
            ),
            progress=progress,
        )

        for service_name, service in deployment.services.items():
            if service.exposure not in {"tailscale-relay", "tcp-relay"} or not service.domain or not service.port:
                continue
            route_target = _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
            route_target.parent.mkdir(parents=True, exist_ok=True)
            service_spec = _compose_service_as_service_spec(deployment, service)
            relay_is_explicit = bool(service.relay.get("url") or service.relay.get("host")) if service.exposure == "tailscale-relay" else bool(service.tcp.get("address") or service.tcp.get("host"))
            if body.get("skipPortainer") and not relay_is_explicit:
                _deploy_step(
                    steps,
                    f"Write route {service_name}",
                    lambda service=service: f"Route skipped: --skip-portainer requires deploy to infer {service.exposure}",
                    progress=progress,
                )
                continue
            if not body.get("skipPortainer"):
                service_spec = _deploy_step(
                    steps,
                    f"Resolve relay {service.name}",
                    lambda service_spec=service_spec, service=service: resolve_tailscale_relay(service_spec) if service.exposure == "tailscale-relay" else resolve_tcp_relay(service_spec),
                    progress=progress,
                )
            route_text = render_tailscale_route(config, service_spec) if service.exposure == "tailscale-relay" else render_tcp_route(config, service_spec)
            _deploy_step(steps, f"Write route {service_name}", lambda route_target=route_target, route_text=route_text: route_target.write_text(route_text, encoding="utf-8"), progress=progress)
            written.append(str(route_target))

        probe_results: list[str] = []
        for service in compose_public_services(deployment):
            service_spec = _compose_service_as_service_spec(deployment, service)
            probe_results.append(
                _deploy_step(
                    steps,
                    f"Probe public route {service.name}",
                    lambda service_spec=service_spec: "Public route probe skipped: --skip-portainer" if body.get("skipPortainer") else _probe_public_route(service_spec),
                    progress=progress,
                )
            )
    except LumaError as exc:
        _mark_compose_deployment(deployment, body, source_name, status="failed_partial", steps=steps, error=str(exc))
        raise
    _mark_compose_deployment(deployment, body, source_name, status="active", steps=steps)
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "written": written,
        "dns": dns_results,
        "portainer": portainer_result,
        "probe": probe_results,
        "storagePreparation": storage_preparation,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "steps": steps,
    }


def handle_compose_deployment_preview(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    stack_text = render_compose_stack(
        config,
        deployment,
        node_id_resolver=_compose_node_id_resolver(state),
        node_records=_state_nodes(state),
    )
    target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
    storage_guard = _guard_compose_storage_switch(target, stack_text, deployment)
    _stack_env_for_text(stack_text)
    route_texts = render_compose_routes(config, deployment)
    artifacts = [
        {
            "kind": "stack",
            "path": str(compose_stack_path(config, deployment)),
            "content": stack_text,
        }
    ]
    for service_name, route_text in route_texts.items():
        artifacts.append(
            {
                "kind": "route",
                "path": str(compose_route_path(config, deployment, service_name)),
                "content": route_text,
            }
        )
    services = []
    for service_name, service in sorted(deployment.services.items()):
        services.append(
            {
                "name": service_name,
                "region": service.region or deployment.region,
                "node": service.node or "",
                "exposure": service.exposure,
                "domain": service.domain or "",
                "port": service.port,
                "publishPort": service.publish_port,
                "replicas": service.replicas,
                "proxy": service.proxy,
            }
        )
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "summary": {
            "name": deployment.name,
            "region": deployment.region,
            "services": services,
            "storageGuard": storage_guard,
        },
        "artifacts": artifacts,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "warnings": deployment.warnings,
    }


def handle_storage_apply(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    _deploy_step(
        steps,
        "Resolve storage endpoints",
        lambda: render_compose_stack(config, deployment, node_records=_state_nodes(state)),
        progress=progress,
    )
    applied = _deploy_step(
        steps,
        "Prepare managed storage",
        lambda: _prepare_compose_managed_storage(deployment, state),
        progress=progress,
    )
    return {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "applied": applied,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "steps": steps,
    }


def handle_storage_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    return {"storageClasses": _storage_classes_summary(state)}


def handle_storage_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = str(body.get("name") or "").strip()
    if not name:
        raise LumaError("storage class name is required")
    provider = str(body.get("provider") or "nfs").strip()
    if provider != "nfs":
        raise LumaError("storage provider must be: nfs")
    mode = "external" if bool(body.get("external")) else "managed"
    item = {
        "provider": provider,
        "mode": mode,
        "node": str(body.get("node") or "").strip(),
        "endpoint": str(body.get("endpoint") or "").strip(),
        "path": str(body.get("path") or "").strip(),
        "mountOptions": str(body.get("mountOptions") or DEFAULT_NFS_MOUNT_OPTIONS).strip(),
        "regions": [str(value) for value in body.get("regions") or [] if str(value)],
        "nodes": [str(value) for value in body.get("nodes") or [] if str(value)],
        "updatedAt": int(time.time()),
    }
    def validate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        _validate_storage_class_record(name, item, state)

    mutate_state(validate)
    state = load_state()
    storage_class_record = {key: value for key, value in item.items() if value not in ("", [], None)}
    storage_host = _apply_managed_storage_class(name, storage_class_record, state)

    def save_storage_class(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        storage_classes = state.setdefault("storageClasses", {})
        if not isinstance(storage_classes, dict):
            storage_classes = {}
            state["storageClasses"] = storage_classes
        storage_classes[name] = storage_class_record

    mutate_state(save_storage_class)
    result = {"name": name, "saved": True, "storageClass": _public_storage_class(name, storage_class_record)}
    if storage_host:
        result["storageHost"] = storage_host
    return result


def _apply_managed_storage_class(name: str, item: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, str] | None:
    storage_class = _storage_class_spec_from_record(name, item)
    if storage_class.mode != "managed":
        return None
    host_result = _prepare_managed_nfs_host(storage_class, state)
    return host_result


def _storage_class_spec_from_record(name: str, item: Dict[str, Any]) -> StorageClassSpec:
    return StorageClassSpec(
        name=name,
        provider=str(item.get("provider") or "nfs"),
        mode=str(item.get("mode") or "managed"),
        node=str(item.get("node") or "") or None,
        path=str(item.get("path") or "") or None,
        endpoint=str(item.get("endpoint") or "") or None,
        mount_options=str(item.get("mountOptions") or DEFAULT_NFS_MOUNT_OPTIONS),
        nodes=[str(value) for value in item.get("nodes") or []],
        regions=[str(value) for value in item.get("regions") or []],
        raw=dict(item),
    )


def _prepare_managed_nfs_host(storage_class: StorageClassSpec, state: Dict[str, Any]) -> Dict[str, str]:
    if storage_class.provider != "nfs":
        raise LumaError(f"managed storage provider not supported yet: {storage_class.provider}")
    if not storage_class.node:
        raise LumaError(f"managed storageClass {storage_class.name}.node is required")
    if not storage_class.path:
        raise LumaError(f"managed storageClass {storage_class.name}.path is required")
    storage_root = Path(storage_class.path)
    if not storage_root.is_absolute():
        raise LumaError(f"managed storage path must be absolute: {storage_class.path}")
    if ".." in storage_root.parts:
        raise LumaError(f"managed storage path must not contain ..: {storage_class.path}")
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, storage_class.node) or {}
    if not _storage_node_is_local(record, storage_class.node):
        result = _run_node_agent_task(
            state,
            storage_class.node,
            "prepare-managed-nfs-host",
            {"name": storage_class.name, "path": storage_class.path},
        )
        return {
            "name": storage_class.name,
            "node": storage_class.node,
            "path": storage_class.path,
            "prepared": str(result.get("message") or "host NFS export ready"),
            "taskId": str(result.get("taskId") or ""),
        }
    _prepare_local_nfs_export(storage_class, record)
    return {
        "name": storage_class.name,
        "node": storage_class.node,
        "path": storage_class.path,
        "prepared": "host NFS export ready",
    }


def _prepare_local_nfs_export(storage_class: StorageClassSpec, record: Dict[str, Any] | None = None) -> None:
    if not storage_class.path:
        raise LumaError(f"managed storageClass {storage_class.name}.path is required")
    try:
        from ..agent import _linux_prepare_nfs_command, _macos_prepare_nfs_command

        agent = record.get("agent") if isinstance(record, dict) and isinstance(record.get("agent"), dict) else {}
        if str(agent.get("os") or "") == "darwin":
            LocalExecutor().sudo(_macos_prepare_nfs_command(storage_class.name, storage_class.path))
        else:
            _run_host_prep_command(_linux_prepare_nfs_command(storage_class.name, storage_class.path))
    except LumaError as exc:
        raise LumaError(f"failed to prepare managed NFS storage {storage_class.name} on local node: {exc}") from exc


def _run_host_prep_command(command: str) -> str:
    docker_error = None
    try:
        return _run_host_prep_container(command)
    except LumaError as exc:
        docker_error = exc
    try:
        return LocalExecutor().sudo(command)
    except LumaError as exc:
        raise LumaError(f"Docker host-prep failed: {docker_error}; local sudo failed: {exc}") from exc


def _run_host_prep_container(command: str) -> str:
    image = os.environ.get("LUMA_HOST_PREP_IMAGE", "ubuntu:22.04")
    _ensure_host_prep_image(image)
    name = f"luma-host-prep-{os.getpid()}-{int(time.time() * 1000)}"
    create_body = {
        "Image": image,
        "Cmd": ["chroot", "/host", "bash", "-lc", command],
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": True,
        "HostConfig": {
            "Privileged": True,
            "PidMode": "host",
            "NetworkMode": "host",
            "Binds": ["/:/host"],
        },
    }
    container_id = ""
    try:
        created = docker_request("POST", f"/containers/create?{urllib.parse.urlencode({'name': name})}", create_body)
        if not isinstance(created, dict) or not created.get("Id"):
            raise LumaError("Docker did not return a host-prep container id")
        container_id = str(created["Id"])
        docker_request("POST", f"/containers/{container_id}/start")
        waited = docker_request("POST", f"/containers/{container_id}/wait")
        status_code = int(waited.get("StatusCode", 1)) if isinstance(waited, dict) else 1
        logs = _docker_container_logs(container_id)
        if status_code != 0:
            raise LumaError(f"host-prep container exited {status_code}: {logs.strip()}")
        return logs
    finally:
        if container_id:
            try:
                docker_request("DELETE", f"/containers/{container_id}?{urllib.parse.urlencode({'force': 'true', 'v': 'true'})}")
            except LumaError:
                pass


def _ensure_host_prep_image(image: str) -> None:
    try:
        docker_request("GET", f"/images/{urllib.parse.quote(image, safe='')}/json")
        return
    except LumaError:
        pass
    repo, tag = _split_image_tag(image)
    status, raw = docker_request_raw(
        "POST",
        f"/images/create?{urllib.parse.urlencode({'fromImage': repo, 'tag': tag})}",
    )
    if status >= 400:
        raise LumaError(f"Docker image pull failed for {image}: {raw}")


def _split_image_tag(image: str) -> tuple[str, str]:
    if ":" in image.rsplit("/", 1)[-1]:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, "latest"


def _docker_container_logs(container_id: str) -> str:
    status, raw = docker_request_raw(
        "GET",
        f"/containers/{container_id}/logs?{urllib.parse.urlencode({'stdout': 1, 'stderr': 1})}",
    )
    if status >= 400:
        return ""
    return raw


def _storage_node_is_local(record: Dict[str, Any], node_name: str) -> bool:
    try:
        info = docker_request("GET", "/info")
    except (LumaError, AssertionError):
        return False
    if not isinstance(info, dict):
        return False
    swarm = info.get("Swarm") if isinstance(info.get("Swarm"), dict) else {}
    local_values = {
        str(info.get("Name") or ""),
        str(swarm.get("NodeID") or ""),
    }
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    record_values = {
        str(node_name or ""),
        str(record.get("displayName") or ""),
        str(record.get("swarmHostname") or ""),
        str(record.get("swarmNodeId") or ""),
        str(labels.get("luma.node.name") or ""),
        str(labels.get("luma.node.id") or ""),
    }
    return bool((local_values - {""}) & (record_values - {""}))


def _storage_apply_available(state: Dict[str, Any]) -> bool:
    return bool(
        state.get("portainerApiUrl")
        and state.get("portainerEndpointId")
        and state.get("portainerAdminPassword")
    )


def handle_storage_remove(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = str(body.get("name") or "").strip()
    if not name:
        raise LumaError("storage class name is required")
    storage_classes = state.get("storageClasses") if isinstance(state.get("storageClasses"), dict) else {}
    existing = storage_classes.get(name)
    removed = bool(existing)
    storage_host = _remove_managed_storage_class(name, existing, state) if isinstance(existing, dict) else None
    def remove_storage_class(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        storage_classes = state.get("storageClasses") if isinstance(state.get("storageClasses"), dict) else {}
        storage_classes.pop(name, None)
        state["storageClasses"] = storage_classes

    mutate_state(remove_storage_class)
    result = {"name": name, "removed": removed}
    if storage_host:
        result["storageHost"] = storage_host
    return result


def _remove_managed_storage_class(name: str, item: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, str] | None:
    storage_class = _storage_class_spec_from_record(name, item)
    if storage_class.mode != "managed":
        return None
    export_removed = _remove_local_nfs_export(storage_class, state)
    if not _storage_apply_available(state):
        return {"name": name, "removed": "pending: Portainer is not configured", "export": export_removed}
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    stack_name = f"luma-storage-{slugify(storage_class.name)}"
    removed = remove_stack(config, _storage_stack_service_spec(stack_name), state)
    return {"name": name, "removed": removed, "export": export_removed}


def _remove_local_nfs_export(storage_class: StorageClassSpec, state: Dict[str, Any]) -> str:
    if not storage_class.node:
        return "skipped: no node"
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, storage_class.node) or {}
    if not _storage_node_is_local(record, storage_class.node):
        try:
            result = _run_node_agent_task(
                state,
                storage_class.node,
                "remove-managed-nfs-export",
                {"name": storage_class.name},
            )
            return f"removed by node agent: {result.get('taskId') or ''}".rstrip()
        except LumaError as exc:
            return f"skipped: node agent remove failed: {exc}"
    try:
        from ..agent import _linux_remove_nfs_command, _macos_remove_nfs_command

        agent = record.get("agent") if isinstance(record, dict) and isinstance(record.get("agent"), dict) else {}
        if str(agent.get("os") or "") == "darwin":
            LocalExecutor().sudo(_macos_remove_nfs_command(storage_class.name))
        else:
            _run_host_prep_command(_linux_remove_nfs_command(storage_class.name))
    except LumaError as exc:
        raise LumaError(f"failed to remove managed NFS export {storage_class.name} on local node: {exc}") from exc
    return "removed local NFS export"


def _load_compose_request(body: Dict[str, Any], source_name: str) -> ComposeDeploymentSpec:
    manifest = body.get("manifest")
    compose_content = body.get("composeContent")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError("manifest is required")
    if not isinstance(compose_content, str) or not compose_content.strip():
        raise LumaError("composeContent is required")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sidecar_name = Path(source_name).name or "luma.compose.yml"
        sidecar_path = root / sidecar_name
        sidecar_path.write_text(manifest, encoding="utf-8")
        sidecar_data = load_yaml(sidecar_path)
        compose_value = sidecar_data.get("compose", "docker-compose.yml")
        if not isinstance(compose_value, str) or not compose_value.strip():
            raise LumaError("compose deployment requires string field: compose")
        compose_path = _safe_compose_upload_path(sidecar_path, compose_value)
        compose_path.parent.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(compose_content, encoding="utf-8")
        state = load_state()
        return load_compose_deployment(
            sidecar_path,
            storage_classes=_state_storage_classes(state),
            allow_sidecar_storage_classes=False,
        )


def _safe_compose_upload_path(sidecar_path: Path, compose_value: str) -> Path:
    compose_path = Path(compose_value)
    if compose_path.is_absolute() or ".." in compose_path.parts or not compose_path.name:
        raise LumaError("compose path must be a relative path without .. when deploying through Luma Control")
    return sidecar_path.parent / compose_path


def _prepare_compose_managed_storage(deployment: ComposeDeploymentSpec, state: Dict[str, Any]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    prepared_classes: set[str] = set()
    for volume in deployment.volumes.values():
        if not volume.storage_class:
            continue
        storage_class = deployment.storage_classes[volume.storage_class]
        if storage_class.mode == "managed":
            _managed_volume_relative_path(volume.path or volume.name)
    for storage_class in deployment.storage_classes.values():
        if storage_class.mode != "managed":
            continue
        prepared_classes.add(storage_class.name)
        results.append(_prepare_managed_nfs_host(storage_class, state))
    for volume in deployment.volumes.values():
        if not volume.storage_class:
            continue
        storage_class = deployment.storage_classes[volume.storage_class]
        if storage_class.mode != "managed":
            continue
        if storage_class.name not in prepared_classes:
            results.append(_prepare_managed_nfs_host(storage_class, state))
            prepared_classes.add(storage_class.name)
        results.append(_prepare_managed_volume_path(storage_class, volume.path or volume.name, state))
    return results


def _prepare_service_managed_storage(service: ServiceSpec, state: Dict[str, Any]) -> list[dict[str, str]]:
    if not service.storage:
        return []
    results: list[dict[str, str]] = []
    prepared_classes: set[str] = set()
    storage_records = _state_storage_classes(state)
    for volume in service.storage.values():
        record = storage_records.get(volume.storage_class)
        if not record:
            raise LumaError(f"storage.{volume.name}.storageClass references unknown storage class: {volume.storage_class}")
        storage_class = _storage_class_spec_from_record(volume.storage_class, record)
        if storage_class.mode != "managed":
            continue
        _managed_volume_relative_path(volume.path or volume.name)
        if storage_class.name not in prepared_classes:
            results.append(_prepare_managed_nfs_host(storage_class, state))
            prepared_classes.add(storage_class.name)
        results.append(_prepare_managed_volume_path(storage_class, volume.path or volume.name, state))
    return results


def _prepare_managed_volume_path(storage_class: StorageClassSpec, sub_path: str, state: Dict[str, Any]) -> dict[str, str]:
    if not storage_class.node or not storage_class.path:
        raise LumaError(f"managed storageClass {storage_class.name} requires node and path")
    relative = _managed_volume_relative_path(sub_path)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, storage_class.node) or {}
    full_path = Path(storage_class.path) / relative
    if not _storage_node_is_local(record, storage_class.node):
        result = _run_node_agent_task(
            state,
            storage_class.node,
            "prepare-managed-volume-path",
            {"root": storage_class.path, "relative": str(relative)},
        )
        return {
            "storageClass": storage_class.name,
            "path": str(full_path),
            "prepared": str(result.get("message") or "volume path ready"),
            "taskId": str(result.get("taskId") or ""),
        }
    command = f"install -d -m 755 {shlex.quote(str(full_path))}"
    try:
        _run_host_prep_command(command)
    except LumaError as exc:
        raise LumaError(f"failed to create managed storage volume path {full_path}: {exc}") from exc
    return {"storageClass": storage_class.name, "path": str(full_path), "prepared": "volume path ready"}


def _cleanup_service_storage(
    service: ServiceSpec,
    state: Dict[str, Any],
    *,
    dry_run: bool,
    task_nodes: list[dict[str, str]] | None = None,
) -> str:
    messages: list[str] = []
    if service.storage:
        messages.append(_cleanup_service_managed_storage(service, state, dry_run=dry_run))
    volume_names = _service_docker_volume_names(service)
    if not volume_names:
        return "; ".join(messages) if messages else "No named Docker volumes referenced"
    if dry_run:
        messages.append("Docker volumes would be removed: " + ", ".join(volume_names))
        return "; ".join(messages)
    targets = [_remove_docker_volume_across_nodes(volume_name, task_nodes or [], state) for volume_name in volume_names]
    removed = sum(1 for item in targets if "removed" in item.get("status", ""))
    skipped = sum(1 for item in targets if item.get("status", "").startswith("skipped"))
    messages.append(f"Docker volume cleanup finished: removed={removed}, skipped={skipped}")
    return "; ".join(messages)


def _service_docker_volume_names(service: ServiceSpec) -> list[str]:
    return [f"{service.slug}_{source}" for source in named_volume_sources(service.volumes)]


def _cleanup_service_managed_storage(service: ServiceSpec, state: Dict[str, Any], *, dry_run: bool) -> str:
    targets: list[dict[str, str]] = []
    storage_records = _state_storage_classes(state)
    for volume in service.storage.values():
        record = storage_records.get(volume.storage_class)
        if not record:
            targets.append(
                {
                    "volume": volume.name,
                    "storageClass": volume.storage_class,
                    "path": volume.path or volume.name,
                    "status": "skipped: storageClass is not configured",
                }
            )
            continue
        storage_class = _storage_class_spec_from_record(volume.storage_class, record)
        if storage_class.mode != "managed":
            targets.append(
                {
                    "volume": volume.name,
                    "storageClass": storage_class.name,
                    "path": volume.path or volume.name,
                    "status": "skipped: storageClass is not managed",
                }
            )
            continue
        targets.append(_remove_managed_volume_path(storage_class, volume.path or volume.name, state, dry_run=dry_run))
    if not targets:
        return "No managed storage paths referenced"
    if dry_run:
        paths = ", ".join(item["path"] for item in targets if not item["status"].startswith("skipped"))
        skipped = sum(1 for item in targets if item["status"].startswith("skipped"))
        suffix = f"; skipped={skipped}" if skipped else ""
        return f"Managed storage paths would be removed: {paths or '-'}{suffix}"
    removed = sum(1 for item in targets if "removed" in item["status"])
    skipped = sum(1 for item in targets if item["status"].startswith("skipped"))
    return f"Managed storage cleanup finished: removed={removed}, skipped={skipped}"


def _service_task_nodes(service: ServiceSpec) -> list[dict[str, str]]:
    node_by_id = _swarm_node_map()
    service_name = service.swarm_service_name or f"{service.slug}_{service.slug}"
    filters = urllib.parse.quote(json.dumps({"service": {service_name: True}, "desired-state": {"running": True}}), safe="")
    tasks = docker_request("GET", f"/tasks?filters={filters}")
    if not isinstance(tasks, list):
        raise LumaError("Docker API returned invalid task list")
    nodes: list[dict[str, str]] = []
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        node_id = str(task.get("NodeID") or "")
        node = node_by_id.get(node_id)
        if not node:
            continue
        key = str(node.get("id") or node_id)
        if key in seen:
            continue
        seen.add(key)
        nodes.append(node)
    if not nodes and service.node:
        for node in node_by_id.values():
            if service.node in {str(node.get("lumaNode") or ""), str(node.get("hostname") or "")}:
                nodes.append(node)
                break
    return nodes


def _remove_docker_volume_across_nodes(volume_name: str, task_nodes: list[dict[str, str]], state: Dict[str, Any]) -> dict[str, str]:
    if not task_nodes:
        return _remove_local_docker_volume(volume_name)
    statuses: list[str] = []
    seen: set[str] = set()
    for node in task_nodes:
        key = str(node.get("id") or node.get("hostname") or node.get("lumaNode") or "")
        if key in seen:
            continue
        seen.add(key)
        if _swarm_node_is_local(node):
            result = _remove_local_docker_volume(volume_name)
            statuses.append(result["status"])
            continue
        node_name = str(node.get("lumaNode") or node.get("hostname") or "").strip()
        if not node_name:
            statuses.append("skipped: task node has no Luma node name")
            continue
        result = _run_node_agent_task(
            state,
            node_name,
            "remove-docker-volume",
            {"name": volume_name},
            required_capability="docker-volume",
        )
        statuses.append(str(result.get("message") or "removed by node agent"))
    status = "; ".join(statuses) if statuses else "skipped: no task nodes"
    return {"name": volume_name, "status": status}


def _remove_local_docker_volume(volume_name: str) -> dict[str, str]:
    from ..agent import _safe_docker_volume_name

    safe_name = _safe_docker_volume_name(volume_name)
    encoded = urllib.parse.quote(safe_name, safe="")
    deadline = time.monotonic() + 30
    while True:
        status, raw = docker_request_raw("DELETE", f"/volumes/{encoded}?force=true")
        if status == 404:
            return {"name": safe_name, "status": "skipped: Docker volume not found"}
        if status < 400:
            return {"name": safe_name, "status": "removed local Docker volume"}
        if status == 409 and time.monotonic() < deadline:
            time.sleep(2)
            continue
        raise LumaError(f"failed to remove Docker volume {safe_name}: Docker API error {status}: {raw}")


def _swarm_node_is_local(node: dict[str, str]) -> bool:
    try:
        info = docker_request("GET", "/info")
    except (LumaError, AssertionError):
        return False
    if not isinstance(info, dict):
        return False
    swarm = info.get("Swarm") if isinstance(info.get("Swarm"), dict) else {}
    local_values = {
        str(info.get("Name") or ""),
        str(swarm.get("NodeID") or ""),
    }
    node_values = {
        str(node.get("id") or ""),
        str(node.get("hostname") or ""),
        str(node.get("lumaNode") or ""),
        str(node.get("lumaNodeId") or ""),
    }
    return bool((local_values - {""}) & (node_values - {""}))


def _cleanup_compose_managed_storage(deployment: ComposeDeploymentSpec, state: Dict[str, Any], *, dry_run: bool) -> str:
    targets: list[dict[str, str]] = []
    for volume in deployment.volumes.values():
        if not volume.storage_class:
            continue
        storage_class = deployment.storage_classes[volume.storage_class]
        if storage_class.mode != "managed":
            targets.append(
                {
                    "volume": volume.name,
                    "storageClass": storage_class.name,
                    "path": volume.path or volume.name,
                    "status": "skipped: storageClass is not managed",
                }
            )
            continue
        targets.append(_remove_managed_volume_path(storage_class, volume.path or volume.name, state, dry_run=dry_run))
    if not targets:
        return "No managed storage paths referenced"
    if dry_run:
        paths = ", ".join(item["path"] for item in targets if not item["status"].startswith("skipped"))
        skipped = sum(1 for item in targets if item["status"].startswith("skipped"))
        suffix = f"; skipped={skipped}" if skipped else ""
        return f"Managed storage paths would be removed: {paths or '-'}{suffix}"
    removed = sum(1 for item in targets if "removed" in item["status"])
    skipped = sum(1 for item in targets if item["status"].startswith("skipped"))
    return f"Managed storage cleanup finished: removed={removed}, skipped={skipped}"


def _remove_managed_volume_path(storage_class: StorageClassSpec, sub_path: str, state: Dict[str, Any], *, dry_run: bool) -> dict[str, str]:
    if not storage_class.node or not storage_class.path:
        raise LumaError(f"managed storageClass {storage_class.name} requires node and path")
    relative = _managed_volume_relative_path(sub_path)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, storage_class.node) or {}
    full_path = Path(storage_class.path) / relative
    item = {"storageClass": storage_class.name, "path": str(full_path)}
    if dry_run:
        return {**item, "status": "planned"}
    if not _storage_node_is_local(record, storage_class.node):
        result = _run_node_agent_task(
            state,
            storage_class.node,
            "remove-managed-volume-path",
            {"root": storage_class.path, "relative": str(relative)},
        )
        return {
            **item,
            "status": str(result.get("message") or "removed by node agent"),
            "taskId": str(result.get("taskId") or ""),
        }
    command = _remove_managed_volume_command(storage_class.path, str(relative))
    try:
        _run_host_prep_command(command)
    except LumaError as exc:
        raise LumaError(f"failed to remove managed storage volume path {full_path}: {exc}") from exc
    return {**item, "status": "removed local path"}


def _remove_managed_volume_command(root: str, relative: str) -> str:
    from ..agent import _remove_volume_path_command

    return _remove_volume_path_command(root, relative)


def _managed_volume_relative_path(sub_path: str) -> Path:
    relative = Path(str(sub_path).strip().strip("/"))
    if relative.is_absolute() or ".." in relative.parts or not str(relative):
        raise LumaError(f"managed storage volume path must be relative and cannot contain ..: {sub_path}")
    return relative


def _emit_compose_warnings(
    steps: list[dict[str, str]],
    progress: Callable[[dict[str, str]], None] | None,
    deployment: ComposeDeploymentSpec,
) -> None:
    for warning in deployment.warnings:
        step = {"name": "Compose warning", "status": "ok", "message": warning}
        steps.append(step)
        _emit_progress(progress, step)


def _compose_node_id_resolver(state: Dict[str, Any]) -> Callable[[str], str | None]:
    def resolve(node_name: str) -> str | None:
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        record = _node_record_for_name(nodes, node_name)
        if not record:
            names = ", ".join(sorted(str(name) for name in nodes)) or "none"
            raise LumaError(f"unknown Luma node: {node_name}. Registered nodes: {names}")
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        node_id = str(record.get("swarmNodeId") or labels.get("luma.node.id") or "").strip()
        if not node_id:
            raise LumaError(f"Luma node {node_name} has no Swarm NodeID; rerun luma node join on that node")
        return node_id

    return resolve


def _state_nodes(state: Dict[str, Any]) -> Dict[str, Any]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    return {str(name): dict(value) for name, value in nodes.items() if isinstance(value, dict)}


def _compose_service_as_service_spec(deployment: ComposeDeploymentSpec, service: Any) -> ServiceSpec:
    return ServiceSpec(
        source=deployment.source,
        name=f"{deployment.slug}-{service.name}",
        image="compose",
        region=service.region or deployment.region,
        node=service.node,
        public=service.exposure != "none",
        exposure=service.exposure,
        domain=service.domain,
        port=service.port,
        publish_port=service.publish_port,
        replicas=service.replicas or 1,
        relay=service.relay,
        tunnel=service.tunnel,
        tcp=service.tcp,
        proxy=service.proxy,
        swarm_service_name=f"{deployment.slug}_{service.name}",
    )


def _stack_service_spec(deployment: ComposeDeploymentSpec) -> ServiceSpec:
    return ServiceSpec(source=deployment.source, name=deployment.name, image="compose", region=deployment.region)


def _storage_stack_service_spec(stack_name: str) -> ServiceSpec:
    return ServiceSpec(source=Path("storage"), name=stack_name, image="storage", region="cn")


def _guard_compose_storage_switch(target: Path, stack_text: str, deployment: ComposeDeploymentSpec) -> str:
    if not target.exists():
        return "No previous compose stack"
    previous = _safe_yaml_mapping(target.read_text(encoding="utf-8"))
    current = _safe_yaml_mapping(stack_text)
    previous_volumes = previous.get("volumes") if isinstance(previous.get("volumes"), dict) else {}
    current_volumes = current.get("volumes") if isinstance(current.get("volumes"), dict) else {}
    changed = []
    for name, spec in deployment.volumes.items():
        if not spec.storage_class:
            continue
        if spec.initialize == "empty" or spec.adopted:
            continue
        before = previous_volumes.get(name)
        after = current_volumes.get(name)
        if before != after:
            changed.append(name)
    if changed:
        raise LumaError(
            "storage backend changed for "
            + ", ".join(sorted(changed))
            + "; run luma storage migrate and set adopted: true after verification, or set initialize: empty for a fresh volume"
        )
    return "Storage backend unchanged"


def _safe_yaml_mapping(text: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid generated YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError("generated YAML must be a mapping")
    return data


def _state_storage_classes(state: Dict[str, Any]) -> Dict[str, Any]:
    storage_classes = state.get("storageClasses") if isinstance(state.get("storageClasses"), dict) else {}
    return {str(name): dict(value) for name, value in storage_classes.items() if isinstance(value, dict)}


def _storage_classes_summary(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [_public_storage_class(name, value) for name, value in sorted(_state_storage_classes(state).items())]


def _public_storage_class(name: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": str(name),
        "provider": str(value.get("provider") or ""),
        "mode": str(value.get("mode") or "external"),
        "node": str(value.get("node") or ""),
        "endpoint": str(value.get("endpoint") or ""),
        "path": str(value.get("path") or ""),
        "mountOptions": str(value.get("mountOptions") or ""),
        "regions": [str(item) for item in value.get("regions") or []],
        "nodes": [str(item) for item in value.get("nodes") or []],
        "updatedAt": value.get("updatedAt") or "",
    }


def _validate_storage_class_record(name: str, item: Dict[str, Any], state: Dict[str, Any]) -> None:
    if item["mode"] == "external":
        if not item.get("endpoint"):
            raise LumaError(f"external storage class {name} requires endpoint")
        if not item.get("regions"):
            raise LumaError(f"external storage class {name} requires at least one region")
        if item.get("node") or item.get("path"):
            raise LumaError(f"external storage class {name} cannot set node or path")
    if item["mode"] == "managed":
        if not item.get("node") or not item.get("path"):
            raise LumaError(f"managed storage class {name} requires node and path")
        if item.get("endpoint"):
            raise LumaError(f"managed storage class {name} cannot set endpoint")
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        node_name = str(item.get("node") or "")
        record = _node_record_for_name(nodes, node_name)
        if not record:
            _adopt_swarm_node_for_storage(state, node_name)
            nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
            record = _node_record_for_name(nodes, node_name)
        if not record:
            names = ", ".join(sorted(str(key) for key in nodes)) or "none"
            raise LumaError(f"managed storage class {name} references unknown Luma node: {item.get('node')}. Registered nodes: {names}")
        _ensure_storage_node_swarm_label(node_name, record)
    regions = item.get("regions") if isinstance(item.get("regions"), list) else []
    for region in regions:
        if region not in VALID_REGIONS:
            raise LumaError(f"storage class {name} region must be one of {sorted(VALID_REGIONS)}")

def handle_secret_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    return {"secrets": sorted(str(key) for key in secrets)}


def handle_secret_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name") or "").strip()
    value = body.get("value")
    if not _valid_env_name(name):
        raise LumaError("secret name must be a valid environment variable name")
    if value is None or str(value) == "":
        raise LumaError("secret value is required")

    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        secrets = state.setdefault("secrets", {})
        if not isinstance(secrets, dict):
            secrets = {}
            state["secrets"] = secrets
        secrets[name] = str(value)

    mutate_state(mutate)
    return {"name": name, "saved": True}


def handle_registry_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    items = []
    for host, item in sorted(registries.items()):
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "host": str(host),
                "serverAddress": str(item.get("serverAddress") or host),
                "username": str(item.get("username") or ""),
                "configured": bool(item.get("username") and item.get("password")),
            }
        )
    return {"registries": items}


def handle_registry_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    host = normalize_registry_host(str(body.get("host") or body.get("serverAddress") or ""))
    username = str(body.get("username") or "").strip()
    password = body.get("password")
    if not username:
        raise LumaError("registry username is required")
    if password is None or str(password) == "":
        raise LumaError("registry password is required")

    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        registries = state.setdefault("registries", {})
        if not isinstance(registries, dict):
            registries = {}
            state["registries"] = registries
        registries[host] = {
            "serverAddress": host,
            "username": username,
            "password": str(password),
            "updatedAt": int(time.time()),
        }

    mutate_state(mutate)
    return {"host": host, "username": username, "saved": True}


def handle_registry_remove(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    host = normalize_registry_host(str(body.get("host") or ""))
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    removed = bool(registries.get(host))
    portainer_removed = False
    warning = None
    try:
        config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
        config = load_config(config_path)
        portainer_removed = remove_luma_portainer_registry(config, state, host)
    except LumaError as exc:
        warning = f"Portainer registry cleanup failed: {exc}"
    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        registries.pop(host, None)
        state["registries"] = registries

    mutate_state(mutate)
    result: Dict[str, Any] = {"host": host, "removed": removed, "portainerRegistryRemoved": portainer_removed}
    if warning:
        result["warning"] = warning
    return result


def _planned_remove_message(prefix: str, target: str) -> str:
    return f"{prefix}: {target}"


def _planned_delete_dns_message(service: ServiceSpec) -> str:
    if not service.public:
        return "DNS skipped: service is not public"
    if service.exposure == "cloudflare-tunnel":
        return "DNS skipped: Cloudflare Tunnel public hostname is managed by the tunnel"
    return _planned_remove_message("DNS would be deleted", service.domain or service.name)


def _generated_stack_remove_target(config: Any, service: ServiceSpec, config_path: Path) -> Path:
    target = _resolve_control_path(stack_path(config, service), config_path)
    if service.stack_path:
        return target
    return target.parent


def _remove_generated_files(stack_target: Path, *route_targets: Path | None) -> str:
    removed = []
    missing = []
    for target in [stack_target, *route_targets]:
        if target is None:
            continue
        result = _remove_path(target)
        if result:
            removed.append(str(target))
        else:
            missing.append(str(target))
    if removed and missing:
        return f"Generated files removed: {', '.join(removed)}; not found: {', '.join(missing)}"
    if removed:
        return f"Generated files removed: {', '.join(removed)}"
    return f"Generated files not found: {', '.join(missing)}"


def _remove_path(path: Path) -> bool:
    if path.is_dir():
        shutil.rmtree(path)
        return True
    if path.exists():
        path.unlink()
        return True
    return False


def _resolve_control_path(path: Path, config_path: Path) -> Path:
    if path.is_absolute():
        return path
    return config_path.resolve().parent / path


def _apply_state_secrets(state: Dict[str, Any]) -> None:
    secrets = state.get("secrets") or {}
    if not isinstance(secrets, dict):
        return
    for key, value in secrets.items():
        if value is None:
            continue
        os.environ[str(key)] = str(value)


def _registry_auth_for_service(state: Dict[str, Any], service: ServiceSpec) -> Dict[str, str] | None:
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    return registry_auth_for_image(registries, service.image)


def image_pull_requires_egress(image: str) -> bool:
    registry = _image_registry_host(image)
    return registry in _egress_pull_registries()


def ensure_image_pull_network(state: Dict[str, Any], image: str) -> str:
    registry = _image_registry_host(image)
    if registry in _egress_pull_registries():
        return ensure_image_pull_egress_proxy(state, image)

    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    if not registry_auth_for_image(registries, image):
        return f"Image pull network unchanged: no private registry credential for {registry}"

    info = _docker_info_with_retry()
    if not _docker_info_uses_egress_proxy(info):
        return f"Image pull network ready for {registry}: Docker daemon proxy disabled"

    required_no_proxy = _no_proxy_entries_for_registry(registry)
    if _docker_info_no_proxy_contains(info, required_no_proxy):
        return f"Image pull network ready for {registry}: Docker daemon proxy bypass configured"

    node_name = _local_control_node_name(state, info)
    if not node_name:
        raise LumaError(f"image pull proxy bypass requires Docker daemon access for {registry}, but the control manager node is not registered")

    no_proxy = _merge_no_proxy(_docker_info_no_proxy(info), EGRESS_NO_PROXY, *required_no_proxy)
    result = _run_node_agent_task(
        state,
        node_name,
        "configure-docker-egress-proxy",
        {"proxy": EGRESS_PROXY_URL, "noProxy": no_proxy},
        timeout=180,
        required_capability="docker-egress-proxy",
    )
    info = _docker_info_with_retry()
    if not _docker_info_no_proxy_contains(info, required_no_proxy):
        raise LumaError(f"image pull proxy bypass was configured on {node_name}, but Docker daemon did not report NO_PROXY for {registry}")
    return str(result.get("message") or f"Image pull proxy bypass configured for {registry}")


def ensure_image_pull_egress_proxy(state: Dict[str, Any], image: str) -> str:
    registry = _image_registry_host(image)
    if registry not in _egress_pull_registries():
        return f"Image pull egress not required: {registry}"
    _require_egress_gateway_running()
    info = _docker_info_with_retry()
    if _docker_info_uses_egress_proxy(info):
        return f"Image pull egress ready for {registry}: Docker daemon proxy {EGRESS_PROXY_URL}"
    node_name = _local_control_node_name(state, info)
    if not node_name:
        raise LumaError(f"image pull egress requires Docker daemon proxy for {registry}, but the control manager node is not registered")
    result = _run_node_agent_task(
        state,
        node_name,
        "configure-docker-egress-proxy",
        {"proxy": EGRESS_PROXY_URL, "noProxy": EGRESS_NO_PROXY},
        timeout=180,
        required_capability="docker-egress-proxy",
    )
    info = _docker_info_with_retry()
    if not _docker_info_uses_egress_proxy(info):
        raise LumaError(f"image pull egress proxy was configured on {node_name}, but Docker daemon did not report {EGRESS_PROXY_URL}")
    return str(result.get("message") or f"Image pull egress configured for {registry}")


def _egress_pull_registries() -> set[str]:
    raw = os.environ.get("LUMA_EGRESS_PULL_REGISTRIES")
    if raw is None:
        return set(DEFAULT_EGRESS_PULL_REGISTRIES)
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if values == {"none"}:
        return set()
    return values


def _image_registry_host(image: str) -> str:
    image_ref = image.split("@", 1)[0]
    if "/" not in image_ref:
        return "docker.io"
    first = image_ref.split("/", 1)[0].lower()
    if "." in first or ":" in first or first == "localhost":
        return normalize_registry_host(first)
    return "docker.io"


def _no_proxy_entries_for_registry(registry: str) -> tuple[str, ...]:
    host = normalize_registry_host(public_registry_url(registry))
    entries = [host]
    if ":" in host:
        entries.append(host.rsplit(":", 1)[0])
    return tuple(entries)


def _docker_info_no_proxy(info: Dict[str, Any]) -> str:
    for key in ("NoProxy", "NOProxy", "NO_PROXY", "No_proxy", "no_proxy"):
        value = info.get(key)
        if value:
            return str(value)
    return ""


def _docker_info_no_proxy_contains(info: Dict[str, Any], required: tuple[str, ...]) -> bool:
    entries = {item.strip().lower() for item in _docker_info_no_proxy(info).split(",") if item.strip()}
    return all(item.lower() in entries for item in required)


def _merge_no_proxy(*values: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in str(value or "").split(","):
            entry = item.strip()
            if not entry:
                continue
            key = entry.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return ",".join(merged)


def _require_egress_gateway_running() -> None:
    try:
        docker_request("GET", "/services/egress_mihomo")
        filters = urllib.parse.quote(json.dumps({"service": {"egress_mihomo": True}, "desired-state": {"running": True}}), safe="")
        tasks = docker_request("GET", f"/tasks?filters={filters}")
    except LumaError as exc:
        raise LumaError("image pull egress requires egress_mihomo; run `luma egress setup` on the manager") from exc
    if not isinstance(tasks, list) or not any(_docker_task_is_running(task) for task in tasks):
        raise LumaError("image pull egress requires a running egress_mihomo task; run `luma egress setup` on the manager")


def _docker_task_is_running(task: Any) -> bool:
    if not isinstance(task, dict):
        return False
    status = task.get("Status") if isinstance(task.get("Status"), dict) else {}
    return str(status.get("State") or "").lower() == "running"


def _docker_info_with_retry() -> Dict[str, Any]:
    last_error: LumaError | None = None
    for _attempt in range(30):
        try:
            info = docker_request("GET", "/info")
            if isinstance(info, dict):
                return info
            raise LumaError("Docker API returned invalid info")
        except LumaError as exc:
            last_error = exc
            time.sleep(1)
    raise LumaError(f"Docker daemon unavailable after egress proxy update: {last_error}")


def _docker_info_uses_egress_proxy(info: Dict[str, Any]) -> bool:
    expected = EGRESS_PROXY_URL.rstrip("/")
    values = [
        str(info.get("HTTPProxy") or ""),
        str(info.get("HTTPSProxy") or ""),
        str(info.get("HttpProxy") or ""),
        str(info.get("HttpsProxy") or ""),
    ]
    return any(value.rstrip("/") == expected for value in values)


def _local_control_node_name(state: Dict[str, Any], info: Dict[str, Any]) -> str:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    swarm = info.get("Swarm") if isinstance(info.get("Swarm"), dict) else {}
    local_ids = {str(info.get("ID") or ""), str(swarm.get("NodeID") or "")} - {""}
    local_names = {str(info.get("Name") or "")} - {""}
    for name, record in nodes.items():
        if not isinstance(record, dict):
            continue
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        record_ids = {
            str(record.get("swarmNodeId") or ""),
            str(labels.get("luma.node.id") or ""),
        } - {""}
        record_names = {
            str(name or ""),
            str(record.get("displayName") or ""),
            str(record.get("swarmHostname") or ""),
            str(labels.get("luma.node.name") or ""),
        } - {""}
        if local_ids & record_ids or local_names & record_names:
            return str(name)
    return ""


def _stack_env_for_text(stack_text: str) -> list[dict[str, str]]:
    names = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", stack_text)))
    env: list[dict[str, str]] = []
    missing = []
    for name in names:
        value = os.environ.get(name)
        if value is None:
            missing.append(name)
        else:
            env.append({"name": name, "value": value})
    if missing:
        raise LumaError("missing deployment secrets: " + ", ".join(missing) + ". Run: luma secret set <NAME>")
    return env


def _probe_public_route(service: ServiceSpec) -> str:
    if service.exposure not in {"cn-edge", "external-edge"}:
        return "Public route probe skipped: service is not exposed through Traefik"
    if not service.domain:
        return "Public route probe skipped: service has no domain"
    url = f"https://{service.domain}/"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "luma-control-route-probe"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _probe_status_message(url, int(resp.status))
    except urllib.error.HTTPError as exc:
        return _probe_status_message(url, int(exc.code))
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        return f"Public route probe inconclusive: {url} ({exc})"


def _probe_status_message(url: str, status: int) -> str:
    if status == 404:
        return f"Public route reachable: {url} -> HTTP 404 (the app may not serve /)"
    return f"Public route reachable: {url} -> HTTP {status}"


def _deploy_step(steps: list[dict[str, str]], name: str, action: Any, *, progress: Callable[[dict[str, str]], None] | None = None) -> Any:
    _emit_progress(progress, {"name": name, "status": "start", "message": "started"})
    try:
        result = action()
    except LumaError as exc:
        step = {"name": name, "status": "fail", "message": str(exc)}
        steps.append(step)
        _emit_progress(progress, step)
        raise LumaError(f"{name} failed: {exc}") from exc
    message = _step_message(result)
    step = {"name": name, "status": "ok", "message": message}
    steps.append(step)
    _emit_progress(progress, step)
    return result


def _emit_progress(progress: Callable[[dict[str, str]], None] | None, event: dict[str, str]) -> None:
    if progress:
        progress(event)


def _step_message(result: Any) -> str:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        image = result[1]
        selected = image.get("selected")
        requested = image.get("requested")
        if selected and requested and selected != requested:
            return f"{requested} -> {selected}"
        if selected:
            return str(selected)
    if isinstance(result, str):
        return result
    if isinstance(result, int):
        return "written"
    return "ok"


def _redact_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    if not parsed.netloc:
        return value
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _valid_env_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def labels_for_region(region: str) -> Dict[str, str]:
    return {"region": region}


def labels_for_node(region: str, *, luma_name: str, node_id: str = "") -> Dict[str, str]:
    labels = labels_for_region(region)
    labels["luma.node.name"] = luma_name
    if node_id:
        labels["luma.node.id"] = node_id
    return labels


def resolve_service_node_pin(service: ServiceSpec, state: Dict[str, Any]) -> ServiceSpec:
    if not service.node:
        return service
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, service.node)
    if not record:
        names = ", ".join(sorted(str(name) for name in nodes)) or "none"
        raise LumaError(f"unknown Luma node: {service.node}. Registered nodes: {names}")
    region = str(record.get("region") or "")
    if region and region != service.region:
        raise LumaError(f"Luma node {service.node} is in region {region}, not {service.region}")
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    node_id = str(record.get("swarmNodeId") or labels.get("luma.node.id") or "").strip()
    if not node_id:
        raise LumaError(f"Luma node {service.node} has no Swarm NodeID; rerun luma node join on that node")
    return replace(service, node_id=node_id)


def _node_record_for_name(nodes: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    direct = nodes.get(name)
    if isinstance(direct, dict):
        return direct
    for value in nodes.values():
        if isinstance(value, dict) and value.get("displayName") == name:
            return value
    return None


def _adopt_swarm_node_for_storage(state: Dict[str, Any], node_name: str) -> None:
    node_name = str(node_name or "").strip()
    if not node_name:
        return
    try:
        nodes = docker_request("GET", "/nodes")
    except LumaError:
        return
    if not isinstance(nodes, list):
        return
    match = _match_luma_swarm_node(nodes, node_name=node_name)
    if not match:
        return
    spec = match.get("Spec") if isinstance(match.get("Spec"), dict) else {}
    labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
    description = match.get("Description") if isinstance(match.get("Description"), dict) else {}
    manager_status = match.get("ManagerStatus") if isinstance(match.get("ManagerStatus"), dict) else {}
    hostname = str(description.get("Hostname") or "")
    swarm_node_id = str(match.get("ID") or "").strip()
    luma_name = str(node_name or labels.get("luma.node.name") or hostname or swarm_node_id[:12]).strip()
    region = str(labels.get("region") or "").strip()
    values: Dict[str, Any] = {
        "status": "adopted",
        "displayName": luma_name,
        "swarmHostname": hostname,
        "labels": {str(key): str(value) for key, value in labels.items()},
    }
    if region:
        values["region"] = region
    if swarm_node_id:
        values["swarmNodeId"] = swarm_node_id
    if spec.get("Role"):
        values["swarmRole"] = str(spec.get("Role") or "")
    if manager_status:
        values["swarmManager"] = True
    _remember_node(state, luma_name, **values)
    if node_name != luma_name and not _node_record_for_name(_state_nodes(state), node_name):
        _remember_node(state, node_name, **values)


def _ensure_storage_node_swarm_label(node_name: str, record: Dict[str, Any]) -> None:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    if labels.get("luma.node.name") == node_name:
        return
    node_id = str(record.get("swarmNodeId") or labels.get("luma.node.id") or "").strip()
    hostname = str(record.get("swarmHostname") or "").strip()
    if not node_id and not hostname:
        return
    region = str(record.get("region") or labels.get("region") or "").strip()
    if not region:
        raise LumaError(f"managed storage node {node_name} has no region; rerun luma update manager or luma node join")
    applied = labels_for_node(region, luma_name=node_name, node_id=node_id)
    try:
        label_swarm_node(hostname or node_name, applied, node_id=node_id)
    except LumaError as exc:
        raise LumaError(f"managed storage node {node_name} could not be labeled for storage placement: {exc}") from exc
    merged = {str(key): str(value) for key, value in labels.items()}
    merged.update(applied)
    record["labels"] = merged
    record["status"] = str(record.get("status") or "labeled")
    if node_id:
        record["swarmNodeId"] = node_id


def _remember_node(state: Dict[str, Any], node_name: str, *, merge_from: str = "", **values: Any) -> None:
    nodes = state.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        state["nodes"] = nodes
    current = nodes.pop(merge_from, None) if merge_from and merge_from != node_name else nodes.get(node_name)
    if not isinstance(current, dict):
        current = {}
    existing = nodes.get(node_name)
    if isinstance(existing, dict):
        current.update(existing)
    current.update(values)
    nodes[node_name] = current


def _registered_nodes_summary(nodes: Dict[str, Any]) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for name in sorted(str(key) for key in nodes):
        raw = nodes.get(name)
        if not isinstance(raw, dict):
            raw = {}
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
        metrics = agent.get("metrics") if isinstance(agent.get("metrics"), dict) else {}
        container_stats = agent.get("containerStats") if isinstance(agent.get("containerStats"), list) else []
        items.append(
            {
                "name": name,
                "displayName": str(raw.get("displayName") or name),
                "swarmHostname": str(raw.get("swarmHostname") or ""),
                "swarmNodeId": str(raw.get("swarmNodeId") or labels.get("luma.node.id") or ""),
                "region": str(raw.get("region") or labels.get("region") or ""),
                "tailscaleIP": str(raw.get("tailscaleIP") or ""),
                "tailscaleName": str(raw.get("tailscaleName") or ""),
                "status": str(raw.get("status") or "registered"),
                "labels": {str(key): str(value) for key, value in labels.items()},
                "agentStatus": _node_agent_status(raw),
                "agentOs": str(agent.get("os") or ""),
                "agentLastSeen": int(agent.get("lastSeen") or 0),
                "storageCapabilities": [str(value) for value in agent.get("capabilities") or []],
                "metrics": _agent_metrics(metrics),
                "containerStats": _container_stats(container_stats),
            }
        )
    return items


def _service_stats_by_name(registered_nodes: list[Dict[str, Any]]) -> dict[str, list[Dict[str, Any]]]:
    result: dict[str, list[Dict[str, Any]]] = {}
    for node in registered_nodes:
        node_name = str(node.get("name") or "")
        for raw in node.get("containerStats") or []:
            if not isinstance(raw, dict):
                continue
            service = str(raw.get("service") or "").strip()
            if not service:
                continue
            item = dict(raw)
            item["node"] = node_name
            result.setdefault(service, []).append(item)
    return result


def _attach_service_actual_resources(service: Dict[str, Any], stats: list[Dict[str, Any]]) -> None:
    tasks = service.get("tasks") if isinstance(service.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        container_id = str(task.get("containerId") or "")
        match = next((item for item in stats if _node_id_matches(str(item.get("containerId") or ""), container_id)), None)
        if match:
            task["cpuPercent"] = float(match.get("cpuPercent") or 0)
            task["memoryUsageBytes"] = int(match.get("memoryUsageBytes") or 0)
            task["memoryPercent"] = float(match.get("memoryPercent") or 0)
    resources = service.get("resources") if isinstance(service.get("resources"), dict) else {}
    total_cpu = round(sum(float(item.get("cpuPercent") or 0) for item in stats), 2)
    total_memory = sum(int(item.get("memoryUsageBytes") or 0) for item in stats)
    total_limit = sum(int(item.get("memoryLimitBytes") or 0) for item in stats)
    actual: Dict[str, Any] = {
        "containers": len(stats),
        "cpuPercent": total_cpu,
        "memoryUsageBytes": total_memory,
        "nodes": sorted({str(item.get("node") or "") for item in stats if item.get("node")}),
    }
    if total_limit:
        actual["memoryLimitBytes"] = total_limit
        actual["memoryPercent"] = round(total_memory / total_limit * 100, 2)
    elif stats:
        actual["memoryPercent"] = round(sum(float(item.get("memoryPercent") or 0) for item in stats), 2)
    resources["actual"] = actual
    service["resources"] = resources


def _dashboard_issues(nodes: list[Dict[str, Any]], services: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    issues: list[Dict[str, Any]] = []
    for node in nodes:
        name = str(node.get("name") or "-")
        state = str(node.get("state") or "").lower()
        availability = str(node.get("availability") or "").lower()
        agent_status = str(node.get("agentStatus") or "").lower()
        if state and state not in {"ready"}:
            issues.append({"severity": "critical", "kind": "node-state", "target": name, "message": f"Node {name} is {state}"})
        if availability in {"drain", "pause"}:
            issues.append({"severity": "warning", "kind": "node-availability", "target": name, "message": f"Node {name} availability is {availability}"})
        if agent_status in {"offline", "missing", "provisioned"}:
            issues.append({"severity": "warning", "kind": "agent", "target": name, "message": f"Node agent on {name} is {agent_status}"})
        sustained_mins = max(1, ALERT_SUSTAINED_SECONDS // 60)
        mem_peak = sustained_breach(
            "node", name, "memoryUsedPercent",
            threshold=ALERT_NODE_MEMORY_PERCENT, duration_seconds=ALERT_SUSTAINED_SECONDS,
        )
        if mem_peak is not None:
            issues.append({"severity": "warning", "kind": "node-memory", "target": name, "message": f"Node {name} memory stayed above {ALERT_NODE_MEMORY_PERCENT:.0f}% for {sustained_mins}m (peak {mem_peak:.1f}%)"})
        cpu_peak = sustained_breach(
            "node", name, "cpuPercent",
            threshold=ALERT_NODE_CPU_PERCENT, duration_seconds=ALERT_SUSTAINED_SECONDS,
        )
        if cpu_peak is not None:
            issues.append({"severity": "warning", "kind": "node-cpu", "target": name, "message": f"Node {name} CPU stayed above {ALERT_NODE_CPU_PERCENT:.0f}% for {sustained_mins}m (peak {cpu_peak:.1f}%)"})
    for service in services:
        full_name = str(service.get("fullName") or service.get("name") or "-")
        running = int(service.get("running") or 0)
        desired = int(service.get("desired") or 0)
        pending = int(service.get("pending") or 0)
        failed = int(service.get("failed") or 0)
        resources = service.get("resources") if isinstance(service.get("resources"), dict) else {}
        actual = resources.get("actual") if isinstance(resources.get("actual"), dict) else {}
        if desired > 0 and running == 0:
            issues.append({"severity": "critical", "kind": "service-running", "target": full_name, "message": f"Service {full_name} has no running tasks"})
        if failed > 0:
            issues.append({"severity": "critical", "kind": "service-failed", "target": full_name, "message": f"Service {full_name} has {failed} failed task(s)"})
        if pending > 0:
            issues.append({"severity": "warning", "kind": "service-pending", "target": full_name, "message": f"Service {full_name} has {pending} pending task(s)"})
        memory_percent = float(actual.get("memoryPercent") or 0)
        if memory_percent >= 85:
            issues.append({"severity": "warning", "kind": "service-memory", "target": full_name, "message": f"Service {full_name} memory is {memory_percent:.1f}%"})
    issues.sort(key=lambda item: (0 if item.get("severity") == "critical" else 1, str(item.get("target") or "")))
    return issues


def _dashboard_docker_list(path: str, label: str, errors: list[str]) -> list[Dict[str, Any]]:
    try:
        value = docker_request("GET", path)
    except LumaError as exc:
        errors.append(f"Docker {label} unavailable: {exc}")
        return []
    if not isinstance(value, list):
        errors.append(f"Docker {label} response was not a list")
        return []
    return [item for item in value if isinstance(item, dict)]


def _dashboard_node_map(raw_nodes: list[Dict[str, Any]]) -> dict[str, Dict[str, Any]]:
    result: dict[str, Dict[str, Any]] = {}
    for node in raw_nodes:
        node_id = str(node.get("ID") or "")
        if not node_id:
            continue
        result[node_id] = _swarm_node_summary_item(node)
    return result


def _dashboard_nodes(registered_nodes: list[Dict[str, Any]], raw_nodes: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    merged: dict[str, Dict[str, Any]] = {}
    for node in registered_nodes:
        name = str(node.get("name") or "")
        if not name:
            continue
        merged.setdefault(name, {})["registered"] = node
    for raw_node in raw_nodes:
        node = _swarm_node_summary_item(raw_node)
        name = str(node.get("lumaNode") or node.get("hostname") or node.get("id") or "")
        if not name:
            continue
        merged.setdefault(name, {})["swarm"] = node

    rows: list[Dict[str, Any]] = []
    for name in sorted(merged):
        registered = merged[name].get("registered") if isinstance(merged[name].get("registered"), dict) else {}
        swarm = merged[name].get("swarm") if isinstance(merged[name].get("swarm"), dict) else {}
        display = str(registered.get("displayName") or swarm.get("hostname") or name)
        rows.append(
            {
                "name": name,
                "displayName": display,
                "swarmHostname": str(registered.get("swarmHostname") or swarm.get("hostname") or ""),
                "swarmNodeId": str(registered.get("swarmNodeId") or swarm.get("lumaNodeId") or swarm.get("id") or ""),
                "region": str(registered.get("region") or swarm.get("region") or ""),
                "role": str(swarm.get("role") or ""),
                "state": str(swarm.get("state") or "missing"),
                "availability": str(swarm.get("availability") or ""),
                "leader": bool(swarm.get("leader")),
                "agentStatus": str(registered.get("agentStatus") or "missing"),
                "agentOs": str(registered.get("agentOs") or ""),
                "agentLastSeen": int(registered.get("agentLastSeen") or 0),
                "storageCapabilities": [str(value) for value in registered.get("storageCapabilities") or []],
                "metrics": registered.get("metrics") if isinstance(registered.get("metrics"), dict) else {},
                "capacity": swarm.get("capacity") if isinstance(swarm.get("capacity"), dict) else {},
            }
        )
    return rows


def _dashboard_route_files(config: Any, config_path: Path, errors: list[str]) -> dict[str, Dict[str, Any]]:
    routes_root = _resolve_control_path(config.routes_root, config_path)
    if not routes_root.exists():
        return {}
    result: dict[str, Dict[str, Any]] = {}
    try:
        files = sorted([*routes_root.glob("*.yml"), *routes_root.glob("*.yaml")])
    except OSError as exc:
        errors.append(f"Route files unavailable: {exc}")
        return result
    for path in files:
        try:
            data = load_yaml(path)
            route = _dashboard_route_file(path.stem, data)
            if route:
                result[path.stem] = route
        except (LumaError, OSError) as exc:
            errors.append(f"Route file {path.name} unreadable: {exc}")
    return result


def _dashboard_route_file(route_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    http = data.get("http") if isinstance(data.get("http"), dict) else {}
    tcp = data.get("tcp") if isinstance(data.get("tcp"), dict) else {}
    if tcp:
        routers = tcp.get("routers") if isinstance(tcp.get("routers"), dict) else {}
        services = tcp.get("services") if isinstance(tcp.get("services"), dict) else {}
        domain = ""
        for router in routers.values():
            if not isinstance(router, dict):
                continue
            domain = _host_from_rule(str(router.get("rule") or ""))
            if domain and domain != "*":
                break
        if domain == "*":
            domain = ""
        upstreams: list[str] = []
        for service in services.values():
            if not isinstance(service, dict):
                continue
            load_balancer = service.get("loadBalancer") if isinstance(service.get("loadBalancer"), dict) else {}
            servers = load_balancer.get("servers") if isinstance(load_balancer.get("servers"), list) else []
            for server in servers:
                if isinstance(server, dict) and server.get("address"):
                    upstreams.append(str(server["address"]))
        if not domain and not upstreams:
            return {}
        return {"id": route_id, "kind": "tcp", "domain": domain, "upstreams": upstreams}
    routers = http.get("routers") if isinstance(http.get("routers"), dict) else {}
    services = http.get("services") if isinstance(http.get("services"), dict) else {}
    domain = ""
    for router in routers.values():
        if not isinstance(router, dict):
            continue
        domain = _host_from_rule(str(router.get("rule") or ""))
        if domain:
            break
    upstreams: list[str] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        load_balancer = service.get("loadBalancer") if isinstance(service.get("loadBalancer"), dict) else {}
        servers = load_balancer.get("servers") if isinstance(load_balancer.get("servers"), list) else []
        for server in servers:
            if isinstance(server, dict) and server.get("url"):
                upstreams.append(str(server["url"]))
    if not domain and not upstreams:
        return {}
    return {"id": route_id, "kind": "http", "domain": domain, "upstreams": upstreams}


def _dashboard_services(
    raw_services: list[Dict[str, Any]],
    raw_tasks: list[Dict[str, Any]],
    node_by_id: dict[str, Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
) -> list[Dict[str, Any]]:
    tasks_by_service: dict[str, list[Dict[str, Any]]] = {}
    for task in raw_tasks:
        service_id = str(task.get("ServiceID") or "")
        if service_id:
            tasks_by_service.setdefault(service_id, []).append(task)

    services: list[Dict[str, Any]] = []
    for service in raw_services:
        item = _dashboard_service(service, tasks_by_service.get(str(service.get("ID") or ""), []), node_by_id, route_files)
        if item:
            services.append(item)

    cloudflare_tunnel_stacks = {item["stack"] for item in services if item.get("name") == "cloudflared" and item.get("stack")}
    for item in services:
        if item.get("stack") in cloudflare_tunnel_stacks and item.get("name") != "cloudflared" and item.get("exposure") == "none":
            item["exposure"] = "cloudflare-tunnel"
    services.sort(key=lambda item: (str(item.get("stack") or ""), str(item.get("name") or "")))
    return services


def _dashboard_service(
    service: Dict[str, Any],
    tasks: list[Dict[str, Any]],
    node_by_id: dict[str, Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    spec = service.get("Spec") if isinstance(service.get("Spec"), dict) else {}
    full_name = str(spec.get("Name") or service.get("ID") or "")
    if not full_name:
        return {}
    stack, name = _split_swarm_service_name(full_name)
    template = spec.get("TaskTemplate") if isinstance(spec.get("TaskTemplate"), dict) else {}
    container = template.get("ContainerSpec") if isinstance(template.get("ContainerSpec"), dict) else {}
    placement = template.get("Placement") if isinstance(template.get("Placement"), dict) else {}
    constraints = placement.get("Constraints") if isinstance(placement.get("Constraints"), list) else []
    labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
    route = _traefik_route_from_labels(labels)
    compose_stack = str(labels.get("luma.compose.stack") or "").strip()
    compose_service = str(labels.get("luma.compose.service") or "").strip()
    route_id = f"{compose_stack}-{slugify(compose_service)}" if compose_stack and compose_service else stack or name
    route_file = route_files.get(route_id) or route_files.get(stack or name) or route_files.get(name)
    counts, task_nodes, task_rows = _dashboard_task_counts(tasks, node_by_id)
    desired = _service_desired_replicas(spec, counts)
    region = _constraint_value(constraints, "node.labels.region")
    exposure = "none"
    if route.get("domain"):
        exposure = "external-edge" if region == "global" else "cn-edge"
    elif route_file:
        if route_file.get("kind") == "tcp":
            exposure = "tcp-relay"
        else:
            exposure = "tailscale-relay"
    return {
        "stack": stack,
        "name": name,
        "fullName": full_name,
        "image": str(container.get("Image") or ""),
        "desired": desired,
        "running": counts["running"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        "nodes": task_nodes,
        "region": region,
        "node": _constraint_value(constraints, "node.labels.luma.node.name") or _constraint_value(constraints, "node.labels.luma.node.id") or _constraint_value(constraints, "node.hostname"),
        "exposure": exposure,
        "routeId": route_id,
        "domain": str(route.get("domain") or (route_file or {}).get("domain") or ""),
        "targetPort": str(route.get("port") or _route_file_target_port(route_file) or ""),
        "network": str(route.get("network") or ""),
        "health": _service_health(desired, counts),
        "storage": _storage_from_labels(labels),
        "resources": _service_resources(template),
        "tasks": task_rows,
        "diagnostics": _service_diagnostics(desired, counts, labels),
        "_routeFile": route_file or {},
    }


def _public_dashboard_service(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _route_file_target_port(route_file: Any) -> str:
    if not isinstance(route_file, dict):
        return ""
    upstreams = route_file.get("upstreams") if isinstance(route_file.get("upstreams"), list) else []
    if not upstreams:
        return ""
    first = str(upstreams[0])
    if ":" not in first:
        return ""
    return first.rsplit(":", 1)[1]


def _storage_from_labels(labels: Dict[str, Any]) -> list[Dict[str, str]]:
    volumes: dict[str, Dict[str, str]] = {}
    for key, value in labels.items():
        label = str(key)
        raw_value = str(value)
        if label.startswith("luma.storage."):
            name = label.removeprefix("luma.storage.")
            volumes.setdefault(name, {"name": name})["kind"] = raw_value
        elif label.startswith("luma.storageClass."):
            name = label.removeprefix("luma.storageClass.")
            volumes.setdefault(name, {"name": name})["storageClass"] = raw_value
        elif label.startswith("luma.storageEndpoint."):
            name = label.removeprefix("luma.storageEndpoint.")
            volumes.setdefault(name, {"name": name})["endpoint"] = raw_value
        elif label.startswith("luma.storagePath."):
            name = label.removeprefix("luma.storagePath.")
            volumes.setdefault(name, {"name": name})["networkPath"] = raw_value
        elif label.startswith("luma.storageNode."):
            name = label.removeprefix("luma.storageNode.")
            volumes.setdefault(name, {"name": name})["node"] = raw_value
    return [volumes[name] for name in sorted(volumes)]


def _service_diagnostics(desired: int, counts: Dict[str, int], labels: Dict[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    if desired > 0 and counts["running"] == 0:
        diagnostics.append("No running tasks")
    if counts["failed"] > 0:
        diagnostics.append(f"{counts['failed']} failed task(s)")
    if counts["pending"] > 0:
        diagnostics.append(f"{counts['pending']} pending task(s)")
    for volume in _storage_from_labels(labels):
        if volume.get("kind") == "unmanaged":
            diagnostics.append(f"Volume {volume['name']} is unmanaged by Luma storage")
    return diagnostics


def _dashboard_storage(services: list[Dict[str, Any]], storage_classes: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    volumes: dict[str, Dict[str, Any]] = {}
    warnings: list[str] = []
    for service in services:
        service_name = str(service.get("fullName") or service.get("name") or "")
        for item in service.get("storage") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            volume = volumes.setdefault(
                name,
                {
                    "name": name,
                    "kind": str(item.get("kind") or "unmanaged"),
                    "storageClass": str(item.get("storageClass") or ""),
                    "node": str(item.get("node") or ""),
                    "endpoint": str(item.get("endpoint") or ""),
                    "networkPath": str(item.get("networkPath") or ""),
                    "services": [],
                },
            )
            volume["services"].append(service_name)
            if volume["kind"] == "unmanaged":
                warning = f"Volume {name} is unmanaged by Luma; rescheduling may use node-local data"
                if warning not in warnings:
                    warnings.append(warning)
    return {"storageClasses": storage_classes or [], "volumes": list(volumes.values()), "warnings": warnings}


def _split_swarm_service_name(name: str) -> tuple[str, str]:
    if "_" not in name:
        return "", name
    stack, service = name.split("_", 1)
    return stack, service


def _service_desired_replicas(spec: Dict[str, Any], counts: Dict[str, int]) -> int:
    mode = spec.get("Mode") if isinstance(spec.get("Mode"), dict) else {}
    replicated = mode.get("Replicated") if isinstance(mode.get("Replicated"), dict) else {}
    replicas = replicated.get("Replicas")
    if isinstance(replicas, int):
        return replicas
    total = counts["running"] + counts["pending"] + counts["failed"]
    return total or 1


def _dashboard_task_counts(tasks: list[Dict[str, Any]], node_by_id: dict[str, Dict[str, Any]]) -> tuple[Dict[str, int], list[str], list[Dict[str, Any]]]:
    current_tasks = [task for task in tasks if str(task.get("DesiredState") or "") == "running"]
    if not current_tasks:
        current_tasks = tasks
    counts = {"running": 0, "failed": 0, "pending": 0}
    nodes: list[str] = []
    task_rows: list[Dict[str, Any]] = []
    for task in current_tasks:
        status = task.get("Status") if isinstance(task.get("Status"), dict) else {}
        state = str(status.get("State") or "").lower()
        if state == "running":
            counts["running"] += 1
        elif state in {"failed", "rejected"}:
            counts["failed"] += 1
        elif state and state not in {"shutdown", "complete", "remove"}:
            counts["pending"] += 1
        node_id = str(task.get("NodeID") or "")
        node = node_by_id.get(node_id)
        hostname = str((node or {}).get("hostname") or "")
        if hostname and state not in {"failed", "rejected", "shutdown", "complete", "remove"} and hostname not in nodes:
            nodes.append(hostname)
        container_status = status.get("ContainerStatus") if isinstance(status.get("ContainerStatus"), dict) else {}
        task_rows.append(
            {
                "id": str(task.get("ID") or "")[:12],
                "node": hostname or node_id[:12],
                "state": state or "unknown",
                "desiredState": str(task.get("DesiredState") or ""),
                "containerId": str(container_status.get("ContainerID") or "")[:12],
                "message": str(status.get("Message") or ""),
                "error": str(status.get("Err") or ""),
            }
        )
    return counts, nodes, task_rows


def _service_resources(template: Dict[str, Any]) -> Dict[str, Any]:
    resources = template.get("Resources") if isinstance(template.get("Resources"), dict) else {}
    result: Dict[str, Any] = {}
    for section_name, output_name in (("Limits", "limits"), ("Reservations", "reservations")):
        section = resources.get(section_name) if isinstance(resources.get(section_name), dict) else {}
        values: Dict[str, Any] = {}
        nano_cpus = int(section.get("NanoCPUs") or 0)
        memory = int(section.get("MemoryBytes") or 0)
        if nano_cpus:
            values["cpus"] = round(nano_cpus / 1_000_000_000, 3)
        if memory:
            values["memoryBytes"] = memory
        if values:
            result[output_name] = values
    return result


def _service_health(desired: int, counts: Dict[str, int]) -> str:
    if desired > 0 and counts["running"] >= desired and counts["failed"] == 0 and counts["pending"] == 0:
        return "running"
    if counts["running"] > 0:
        return "degraded"
    if counts["pending"] > 0:
        return "pending"
    if counts["failed"] > 0:
        return "failed"
    return "unknown"


def _constraint_value(constraints: list[Any], key: str) -> str:
    for constraint in constraints:
        text = str(constraint)
        match = re.search(rf"{re.escape(key)}\s*==\s*([^\s]+)", text)
        if match:
            return match.group(1)
    return ""


def _traefik_route_from_labels(labels: Dict[str, Any]) -> Dict[str, str]:
    route: Dict[str, str] = {}
    for key, value in labels.items():
        label_key = str(key)
        label_value = str(value)
        if label_key.startswith("traefik.http.routers.") and label_key.endswith(".rule") and not route.get("domain"):
            host = _host_from_rule(label_value)
            if host:
                route["domain"] = host
        elif label_key.endswith(".loadbalancer.server.port") and not route.get("port"):
            route["port"] = label_value
        elif label_key == "traefik.swarm.network":
            route["network"] = label_value
    return route


def _host_from_rule(rule: str) -> str:
    for pattern in (
        r"Host\(`([^`]+)`\)",
        r'Host\("([^"]+)"\)',
        r"Host\('([^']+)'\)",
        r"Host\(([^),]+)\)",
        r"HostSNI\(`([^`]+)`\)",
        r'HostSNI\("([^"]+)"\)',
        r"HostSNI\('([^']+)'\)",
        r"HostSNI\(([^),]+)\)",
    ):
        match = re.search(pattern, rule)
        if match:
            return match.group(1).strip(" `\"'")
    return ""


def _dashboard_traffic_paths(
    services: list[Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
    dns_target: str,
) -> list[Dict[str, Any]]:
    paths: list[Dict[str, Any]] = []
    for service in services:
        route_id = str(service.get("routeId") or service.get("name") or "")
        exposure = str(service.get("exposure") or "none")
        nodes = [str(node) for node in service.get("nodes", []) if node]
        service_target = str(service.get("name") or "")
        if service.get("targetPort"):
            service_target = f"{service_target}:{service['targetPort']}"
        if exposure in {"cn-edge", "external-edge"}:
            segments = ["Cloudflare DNS", dns_target or "DNS target missing", "Traefik", service_target]
            segments.extend(nodes or ["no running tasks"])
        elif exposure == "tailscale-relay":
            route_file = service.get("_routeFile") if isinstance(service.get("_routeFile"), dict) else route_files.get(route_id, {})
            upstreams = [str(item) for item in route_file.get("upstreams", [])] if isinstance(route_file, dict) else []
            segments = ["Cloudflare DNS", dns_target or "DNS target missing", "Traefik", "Tailscale"]
            segments.extend(upstreams or nodes or ["upstream unresolved"])
        elif exposure == "tcp-relay":
            route_file = service.get("_routeFile") if isinstance(service.get("_routeFile"), dict) else route_files.get(route_id, {})
            upstreams = [str(item) for item in route_file.get("upstreams", [])] if isinstance(route_file, dict) else []
            segments = ["Cloudflare DNS", dns_target or "DNS target missing", "Traefik TCP"]
            segments.extend(upstreams or nodes or ["upstream unresolved"])
        elif exposure == "cloudflare-tunnel":
            segments = ["Cloudflare", "cloudflared", service_target]
            segments.extend(nodes or ["no running tasks"])
        else:
            segments = ["client/internal", service_target]
            segments.extend(nodes or ["no running tasks"])
        paths.append(
            {
                "id": route_id,
                "kind": exposure,
                "domain": str(service.get("domain") or ""),
                "segments": segments,
            }
        )
    return paths


def _swarm_node_summary_item(node: Dict[str, Any]) -> Dict[str, Any]:
    spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
    description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
    resources = description.get("Resources") if isinstance(description.get("Resources"), dict) else {}
    status = node.get("Status") if isinstance(node.get("Status"), dict) else {}
    manager_status = node.get("ManagerStatus") if isinstance(node.get("ManagerStatus"), dict) else {}
    labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
    return {
        "id": str(node.get("ID") or "")[:12],
        "hostname": str(description.get("Hostname") or ""),
        "role": str(spec.get("Role") or ""),
        "availability": str(spec.get("Availability") or ""),
        "state": str(status.get("State") or ""),
        "addr": str(status.get("Addr") or ""),
        "region": str(labels.get("region") or ""),
        "lumaNode": str(labels.get("luma.node.name") or ""),
        "lumaNodeId": str(labels.get("luma.node.id") or ""),
        "ingress": str(labels.get("ingress") or ""),
        "leader": bool(manager_status.get("Leader")),
        "reachability": str(manager_status.get("Reachability") or ""),
        "capacity": {
            "cpus": round(int(resources.get("NanoCPUs") or 0) / 1_000_000_000, 3) if resources.get("NanoCPUs") else 0,
            "memoryBytes": int(resources.get("MemoryBytes") or 0),
        },
        "labels": {str(key): str(value) for key, value in labels.items()},
    }


def _swarm_nodes_summary() -> Dict[str, Any]:
    try:
        nodes = docker_request("GET", "/nodes")
    except LumaError as exc:
        return {"available": False, "error": str(exc), "nodes": []}
    if not isinstance(nodes, list):
        return {"available": False, "error": "Docker API returned invalid node list", "nodes": []}
    items: list[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        items.append(_swarm_node_summary_item(node))
    items.sort(key=lambda item: item.get("hostname") or item.get("id") or "")
    return {"available": True, "nodes": items}


def remove_swarm_node(node_name: str, record: Dict[str, Any] | None = None) -> Dict[str, Any]:
    nodes = docker_request("GET", "/nodes")
    if not isinstance(nodes, list):
        raise LumaError("Docker API returned invalid node list")
    match = _match_luma_swarm_node(nodes, node_name=node_name, record=record)
    if not match:
        return {"removed": False, "message": "Swarm node not found"}
    node_id = str(match.get("ID") or "")
    if not node_id:
        raise LumaError("Docker API returned invalid node id")
    spec = match.get("Spec") if isinstance(match.get("Spec"), dict) else {}
    manager_status = match.get("ManagerStatus") if isinstance(match.get("ManagerStatus"), dict) else {}
    role = str(spec.get("Role") or "")
    if role == "manager" or manager_status:
        raise LumaError(f"refusing to remove Swarm manager node: {node_name}")
    docker_request("DELETE", f"/nodes/{urllib.parse.quote(node_id, safe='')}?force=1")
    return {"removed": True, "nodeId": node_id, "message": f"Swarm node removed: {node_id}"}


def _match_luma_swarm_node(
    nodes: list[Any],
    *,
    node_name: str,
    record: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    record = record or {}
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    wanted_ids = {
        str(value).strip()
        for value in [
            node_name,
            record.get("swarmNodeId"),
            labels.get("luma.node.id"),
        ]
        if str(value or "").strip()
    }
    wanted_luma_names = {
        str(value).strip()
        for value in [
            node_name,
            record.get("displayName"),
            labels.get("luma.node.name"),
        ]
        if str(value or "").strip()
    }
    wanted_hostnames = {
        str(value).strip()
        for value in [
            node_name,
            record.get("swarmHostname"),
        ]
        if str(value or "").strip()
    }
    node_items: list[tuple[Dict[str, Any], str, dict[str, Any], str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("ID") or "")
        spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
        node_labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        hostname = str(description.get("Hostname") or "")
        node_items.append((node, node_id, node_labels, hostname))

    id_match = _unique_luma_swarm_node_match(
        [
            node
            for node, node_id, node_labels, _hostname in node_items
            if any(
                _node_id_matches(candidate, wanted)
                for candidate in {node_id, str(node_labels.get("luma.node.id") or "")}
                for wanted in wanted_ids
            )
        ],
        node_name,
    )
    if id_match:
        return id_match

    name_match = _unique_luma_swarm_node_match(
        [
            node
            for node, _node_id, node_labels, _hostname in node_items
            if str(node_labels.get("luma.node.name") or "") in wanted_luma_names
        ],
        node_name,
    )
    if name_match:
        return name_match

    return _unique_luma_swarm_node_match(
        [node for node, _node_id, _node_labels, hostname in node_items if hostname in wanted_hostnames],
        node_name,
    )


def _unique_luma_swarm_node_match(matches: list[Dict[str, Any]], node_name: str) -> Dict[str, Any] | None:
    unique: dict[str, Dict[str, Any]] = {str(node.get("ID") or ""): node for node in matches if str(node.get("ID") or "")}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        raise LumaError(f"multiple Swarm nodes match {node_name}; remove by exact Swarm NodeID")
    return None


def _node_id_matches(candidate: str, wanted: str) -> bool:
    candidate = candidate.strip()
    wanted = wanted.strip()
    return bool(candidate and wanted and (candidate == wanted or candidate.startswith(wanted) or wanted.startswith(candidate)))


class DockerSocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_request(method: str, path: str, body: Dict[str, Any] | None = None) -> Any:
    conn = DockerSocketConnection()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        api_version = os.environ.get("DOCKER_API_VERSION", "1.44")
        conn.request(method, f"/v{api_version}" + path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
    except OSError as exc:
        raise LumaError("Docker socket unavailable to Luma Control") from exc
    finally:
        conn.close()
    if response.status >= 400:
        raise LumaError(f"Docker API error {response.status}: {raw}")
    if not raw:
        return None
    return json.loads(raw)


def docker_request_raw(method: str, path: str, *, headers: Dict[str, str] | None = None) -> tuple[int, str]:
    status, raw = docker_request_bytes(method, path, headers=headers)
    return status, raw.decode("utf-8", errors="replace")


def docker_request_bytes(method: str, path: str, *, headers: Dict[str, str] | None = None) -> tuple[int, bytes]:
    conn = DockerSocketConnection()
    try:
        api_version = os.environ.get("DOCKER_API_VERSION", "1.44")
        conn.request(method, f"/v{api_version}" + path, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        return response.status, raw
    except OSError as exc:
        raise LumaError("Docker socket unavailable to Luma Control") from exc
    finally:
        conn.close()


def _decode_docker_log_lines(raw: bytes) -> list[str]:
    chunks: list[bytes] = []
    index = 0
    while index + 8 <= len(raw):
        stream_type = raw[index]
        size = int.from_bytes(raw[index + 4:index + 8], "big")
        if stream_type not in {0, 1, 2} or size < 0 or index + 8 + size > len(raw):
            break
        chunks.append(raw[index + 8:index + 8 + size])
        index += 8 + size
    if not chunks:
        chunks = [raw]
    elif index < len(raw):
        chunks.append(raw[index:])
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return [line.rstrip("\r") for line in text.splitlines() if line.strip()]


def _iter_docker_log_stream(response: Any):
    """Yield decoded log lines from a streaming (follow) Docker logs response.

    Non-TTY containers multiplex stdout/stderr with an 8-byte frame header
    (stream_type, 3 reserved, 4-byte big-endian size); TTY containers send raw
    bytes. We keep a persistent buffer and only consume complete frames, so a
    read that splits a frame mid-way never loses bytes. Framing is decided from
    the first 8 bytes, mirroring _decode_docker_log_lines."""
    buffer = bytearray()
    framed: bool | None = None
    text_pending = ""

    def take_lines(text: str, *, final: bool = False):
        nonlocal text_pending
        text_pending += text
        parts = text_pending.split("\n")
        text_pending = "" if final else parts.pop()
        out = [line.rstrip("\r") for line in parts if line.strip()]
        if final and text_pending.strip():
            out.append(text_pending.rstrip("\r"))
            text_pending = ""
        return out

    while True:
        chunk = response.read(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if framed is None:
            if len(buffer) < 8:
                continue
            framed = buffer[0] in (0, 1, 2) and int.from_bytes(buffer[4:8], "big") < (1 << 30)
        if framed:
            while len(buffer) >= 8:
                size = int.from_bytes(buffer[4:8], "big")
                if len(buffer) < 8 + size:
                    break
                payload = bytes(buffer[8:8 + size])
                del buffer[: 8 + size]
                for line in take_lines(payload.decode("utf-8", errors="replace")):
                    yield line
        else:
            text = bytes(buffer).decode("utf-8", errors="replace")
            buffer.clear()
            for line in take_lines(text):
                yield line
    for line in take_lines("", final=True):
        yield line


def resolve_tailscale_relay(service: ServiceSpec) -> ServiceSpec:
    if service.exposure != "tailscale-relay":
        return service
    if service.relay.get("url") or service.relay.get("host"):
        return service
    upstream_urls = _swarm_task_upstream_urls(service)
    relay = dict(service.relay)
    relay["urls"] = upstream_urls
    return replace(service, relay=relay)


def resolve_tcp_relay(service: ServiceSpec) -> ServiceSpec:
    if service.exposure != "tcp-relay":
        return service
    if service.tcp.get("address") or service.tcp.get("host"):
        return service
    upstream_addresses = _swarm_task_upstream_addresses(service)
    tcp = dict(service.tcp)
    tcp["addresses"] = upstream_addresses
    return replace(service, tcp=tcp)


def _ensure_tcp_relay_ingress(ports: list[int]) -> str:
    wanted_ports = sorted({int(port) for port in ports if int(port) > 0})
    if not wanted_ports:
        return "No TCP ingress ports required"
    service = docker_request("GET", "/services/traefik_traefik")
    if not isinstance(service, dict):
        raise LumaError("Docker API returned invalid Traefik service")
    spec = service.get("Spec")
    version = service.get("Version", {}).get("Index") if isinstance(service.get("Version"), dict) else None
    service_id = str(service.get("ID") or "traefik_traefik")
    if not isinstance(spec, dict) or not version:
        raise LumaError("Traefik service is missing update metadata")
    task_template = spec.setdefault("TaskTemplate", {})
    if not isinstance(task_template, dict):
        raise LumaError("Traefik service TaskTemplate is invalid")
    container = task_template.setdefault("ContainerSpec", {})
    if not isinstance(container, dict):
        raise LumaError("Traefik service ContainerSpec is invalid")
    args = container.setdefault("Args", [])
    if not isinstance(args, list):
        raise LumaError("Traefik service Args is invalid")
    endpoint = spec.setdefault("EndpointSpec", {})
    if not isinstance(endpoint, dict):
        raise LumaError("Traefik service EndpointSpec is invalid")
    published_ports = endpoint.setdefault("Ports", [])
    if not isinstance(published_ports, list):
        raise LumaError("Traefik service EndpointSpec.Ports is invalid")
    changed = False
    for port in wanted_ports:
        name = tcp_entrypoint_name(port)
        command = f"--entrypoints.{name}.address=:{port}"
        if command not in args:
            args.append(command)
            changed = True
        existing = [
            item
            for item in published_ports
            if isinstance(item, dict)
            and int(item.get("PublishedPort") or 0) == port
            and str(item.get("Protocol") or "tcp").lower() == "tcp"
        ]
        incompatible = [
            item
            for item in existing
            if int(item.get("TargetPort") or 0) != port or str(item.get("PublishMode") or "").lower() != "host"
        ]
        if incompatible:
            raise LumaError(f"tcp-relay port {port} conflicts with existing Traefik published port")
        if not existing:
            published_ports.append(
                {
                    "Protocol": "tcp",
                    "TargetPort": port,
                    "PublishedPort": port,
                    "PublishMode": "host",
                }
            )
            changed = True
    if not changed:
        return "TCP ingress already configured: " + ", ".join(str(port) for port in wanted_ports)
    docker_request("POST", f"/services/{urllib.parse.quote(service_id, safe='')}/update?version={version}", spec)
    return "TCP ingress configured: " + ", ".join(str(port) for port in wanted_ports)


def _swarm_task_upstream_urls(service: ServiceSpec) -> list[str]:
    port = int(service.publish_port or service.port or 0)
    if port < 1:
        raise LumaError("tailscale-relay requires a valid port")
    deadline = time.monotonic() + TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS
    last_count = 0
    while True:
        urls, running_count = _running_task_upstream_urls(service, port)
        if running_count >= service.replicas and urls:
            return urls
        last_count = running_count
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise LumaError(
        f"tailscale-relay service {service.slug} has {last_count}/{service.replicas} running tasks; "
        "wait for the service to become ready or check luma status"
    )


def _swarm_task_upstream_addresses(service: ServiceSpec) -> list[str]:
    port = int(service.publish_port or service.port or 0)
    if port < 1:
        raise LumaError("tcp-relay requires a valid port")
    deadline = time.monotonic() + TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS
    last_count = 0
    while True:
        addresses, running_count = _running_task_upstream_addresses(service, port)
        if running_count >= service.replicas and addresses:
            return addresses
        last_count = running_count
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise LumaError(
        f"tcp-relay service {service.slug} has {last_count}/{service.replicas} running tasks; "
        "wait for the service to become ready or check luma status"
    )


def _running_task_upstream_urls(service: ServiceSpec, port: int) -> tuple[list[str], int]:
    addresses, running_count = _running_task_upstream_addresses(service, port)
    return [f"http://{address}" for address in addresses], running_count


def _running_task_upstream_addresses(service: ServiceSpec, port: int) -> tuple[list[str], int]:
    node_by_id = _swarm_node_map()
    service_name = service.swarm_service_name or f"{service.slug}_{service.slug}"
    filters = urllib.parse.quote(json.dumps({"service": {service_name: True}, "desired-state": {"running": True}}), safe="")
    tasks = docker_request("GET", f"/tasks?filters={filters}")
    if not isinstance(tasks, list):
        raise LumaError("Docker API returned invalid task list")
    urls: list[str] = []
    running_count = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task.get("Status") if isinstance(task.get("Status"), dict) else {}
        if str(status.get("State") or "") != "running":
            continue
        node_id = str(task.get("NodeID") or "")
        node = node_by_id.get(node_id)
        if not node:
            continue
        hostname = str(node.get("hostname") or "")
        if service.node_id and node_id != service.node_id:
            continue
        if service.node and not service.node_id and str(node.get("lumaNode") or hostname) != service.node:
            continue
        region = str(node.get("region") or "")
        if region != service.region:
            raise LumaError(f"service task is on node {hostname or node_id} in region {region or '-'}, not {service.region}")
        addr = str(node.get("addr") or "")
        if not addr:
            raise LumaError(f"node {hostname or node_id} has no reachable Docker node address")
        running_count += 1
        address = f"{addr}:{port}"
        if address not in urls:
            urls.append(address)
    return urls, running_count


def _swarm_node_map() -> dict[str, dict[str, str]]:
    nodes = docker_request("GET", "/nodes")
    if not isinstance(nodes, list):
        raise LumaError("Docker API returned invalid node list")
    result: dict[str, dict[str, str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("ID") or "")
        if not node_id:
            continue
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        spec = node.get("Spec") if isinstance(node.get("Spec"), dict) else {}
        labels = spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {}
        status = node.get("Status") if isinstance(node.get("Status"), dict) else {}
        result[node_id] = {
            "id": node_id,
            "hostname": str(description.get("Hostname") or ""),
            "region": str(labels.get("region") or ""),
            "lumaNode": str(labels.get("luma.node.name") or ""),
            "lumaNodeId": str(labels.get("luma.node.id") or ""),
            "addr": str(status.get("Addr") or ""),
        }
    return result


def resolve_service_image(
    config: Any,
    service: ServiceSpec,
    *,
    registry_auth: Dict[str, str] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    images = [service.image, *_fallback_images(config, service.image)]
    errors: list[str] = []
    for image in images:
        image_registry_auth = registry_auth if registry_auth_matches_image(registry_auth, image) else None
        try:
            force_pull = image_uses_mutable_latest_tag(image)
            resolved_image = ensure_image_present(image, registry_auth=image_registry_auth, force_pull=force_pull)
            deploy_image = resolved_image or image
            result = {
                "requested": service.image,
                "selected": image,
                "deployed": deploy_image,
                "fallback": image != service.image,
                "registryAuth": bool(image_registry_auth),
                "forcePull": force_pull,
            }
            return replace(service, image=deploy_image), result
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError("unable to pull service image; tried " + "; ".join(errors))


def ensure_image_present(
    image: str,
    *,
    registry_auth: Dict[str, str] | None = None,
    force_pull: bool = False,
) -> str | None:
    encoded = urllib.parse.quote(image, safe="")
    if not force_pull:
        status, _ = docker_request_raw("GET", f"/images/{encoded}/json")
        if status == 200:
            return None
    from_image = urllib.parse.quote(image, safe="")
    headers = {}
    auth_header = docker_registry_auth_header(registry_auth)
    if auth_header:
        headers["X-Registry-Auth"] = auth_header
    status, raw = docker_request_raw("POST", f"/images/create?fromImage={from_image}", headers=headers)
    if status >= 400:
        raise LumaError(_docker_pull_error_message(status, raw))
    if '"error"' in raw:
        raise LumaError(f"Docker pull failed: {raw.strip()}")
    if force_pull:
        return _image_repo_digest(image)
    return None


def _docker_pull_error_message(status: int, raw: str) -> str:
    detail = raw.strip()
    message = f"Docker pull failed with HTTP {status}: {detail}"
    lowered = detail.lower()
    if status >= 500 and any(marker in lowered for marker in ("failed to do request", "eof", "timeout", "connection reset")):
        message += (
            "; Docker daemon could not reach the registry. Verify the Luma manager egress gateway and Docker daemon proxy "
            "with `luma egress setup` and `docker info` HTTPProxy/HTTPSProxy."
        )
    return message


def _image_repo_digest(image: str) -> str:
    encoded = urllib.parse.quote(image, safe="")
    status, raw = docker_request_raw("GET", f"/images/{encoded}/json")
    if status >= 400:
        raise LumaError(f"Docker image inspect failed with HTTP {status}: {raw.strip()}")
    try:
        details = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise LumaError(f"Docker image inspect returned invalid JSON: {raw.strip()}") from exc
    digests = details.get("RepoDigests")
    if not isinstance(digests, list):
        digests = []
    repository = _image_repository(image)
    for digest in digests:
        if not isinstance(digest, str) or "@sha256:" not in digest:
            continue
        digest_repository = digest.split("@", 1)[0]
        if digest_repository == repository:
            return digest
    for digest in digests:
        if isinstance(digest, str) and "@sha256:" in digest:
            return digest
    raise LumaError(f"Docker pull succeeded but no repo digest was found for {image}; use a pinned digest or fixed tag")


def _image_repository(image: str) -> str:
    image_ref = image.split("@", 1)[0]
    slash = image_ref.rfind("/")
    colon = image_ref.rfind(":")
    if colon > slash:
        return image_ref[:colon]
    return image_ref


def _fallback_images(config: Any, image: str) -> list[str]:
    if _has_registry(image):
        return []
    mirrors = config.defaults.get("imageMirrors") or [
        "docker.1panel.live",
        "docker.1ms.run",
        "docker.m.daocloud.io",
    ]
    if not isinstance(mirrors, list):
        return []
    return [f"{mirror}/{image}" for mirror in mirrors if isinstance(mirror, str) and mirror]


def _has_registry(image: str) -> bool:
    first = image.split("/", 1)[0]
    return "." in first or ":" in first or first == "localhost"


def label_swarm_node(node_name: str, labels: Dict[str, str], *, node_id: str = "") -> None:
    deadline = time.monotonic() + 60
    match = None
    while True:
        nodes = docker_request("GET", "/nodes")
        if not isinstance(nodes, list):
            raise LumaError("Docker API returned invalid node list")
        match = _match_swarm_node(nodes, node_name=node_name, node_id=node_id)
        if match or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if not match:
        target = node_id or node_name
        raise LumaError(f"swarm node not found: {target}")
    node_id = match["ID"]
    inspected = docker_request("GET", f"/nodes/{urllib.parse.quote(node_id, safe='')}")
    version = inspected.get("Version", {}).get("Index")
    spec = inspected.get("Spec")
    if not version or not isinstance(spec, dict):
        raise LumaError(f"Docker API returned invalid node spec: {node_name}")
    current_labels = spec.get("Labels") or {}
    if not isinstance(current_labels, dict):
        current_labels = {}
    spec["Labels"] = {**current_labels, **labels}
    docker_request("POST", f"/nodes/{urllib.parse.quote(node_id, safe='')}/update?version={version}", spec)


def _match_swarm_node(nodes: list[Any], *, node_name: str, node_id: str = "") -> Dict[str, Any] | None:
    if node_id:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            candidate = str(node.get("ID") or "")
            if candidate == node_id or candidate.startswith(node_id) or node_id.startswith(candidate):
                return node
    matches: list[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        description = node.get("Description") if isinstance(node.get("Description"), dict) else {}
        if description.get("Hostname") == node_name:
            matches.append(node)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise LumaError(f"multiple swarm nodes have hostname {node_name}; rerun node join with a CLI that sends Swarm NodeID")
    return None


DASHBOARD_ASSETS = {
    "/dashboard/": ("dashboard/index.html", "text/html; charset=utf-8"),
    "/dashboard/app.js": ("dashboard/app.js", "application/javascript; charset=utf-8"),
    "/dashboard/styles.css": ("dashboard/styles.css", "text/css; charset=utf-8"),
    "/dashboard/asset-luma-logo-mark.png": ("dashboard/asset-luma-logo-mark.png", "image/png"),
}


def _dashboard_asset(path: str) -> tuple[bytes, str]:
    if path not in DASHBOARD_ASSETS:
        raise LumaError("dashboard asset not found")
    relative_path, content_type = DASHBOARD_ASSETS[path]
    return asset_path(relative_path).read_bytes(), content_type


def _request_id() -> str:
    return f"req-{secrets.token_hex(6)}"


def _error_payload(code: str, message: str, *, request_id: str, include_error: bool = True) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "requestId": request_id,
        "errorInfo": {
            "code": code,
            "message": message,
            "requestId": request_id,
        },
    }
    if include_error:
        payload["error"] = message
    return payload


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "LumaControl/0.1"

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path == "/dashboard":
            self.send_response(308)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        if parsed_path.startswith("/dashboard/"):
            try:
                body, content_type = _dashboard_asset(parsed_path)
                cache_control = "no-store" if parsed_path == "/dashboard/" else "public, max-age=60"
                self._bytes(200, body, content_type, cache_control=cache_control)
            except (LumaError, OSError):
                self._json(404, {"error": "not found"})
            return
        if parsed_path == "/v1/health":
            self._json(
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "nodeJoinModel": "region-first",
                    "capabilities": ["node-region", "service-proxy", "dashboard", "service-remove", "node-agent-storage"],
                },
            )
            return
        try:
            token = bearer_token(self.headers)
            if parsed_path == "/v1/registries":
                self._json(200, handle_registry_list(token))
                return
            if parsed_path == "/v1/secrets":
                self._json(200, handle_secret_list(token))
                return
            if parsed_path == "/v1/storage":
                self._json(200, handle_storage_list(token))
                return
            if parsed_path == "/v1/status":
                self._json(200, handle_control_status(token))
                return
            if parsed_path == "/v1/dashboard":
                self._json(200, handle_dashboard(token))
                return
            if parsed_path == "/v1/dashboard/logs":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                service = str((query.get("service") or [""])[0])
                since = str((query.get("since") or [""])[0])
                download = str((query.get("download") or [""])[0]).lower() in {"1", "true", "yes"}
                try:
                    tail = int(str((query.get("tail") or ["120"])[0]) or "120")
                except ValueError as exc:
                    raise LumaError("tail must be a number") from exc
                logs = handle_dashboard_logs(token, service, tail=tail, since=since)
                if download:
                    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", service).strip("-") or "service"
                    self._download_text(f"{filename}.log", "\n".join(str(line) for line in logs.get("logs") or []) + "\n")
                else:
                    self._json(200, logs)
                return
            if parsed_path == "/v1/dashboard/metrics/history":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                kind = str((query.get("kind") or ["node"])[0])
                name = str((query.get("name") or [""])[0])
                try:
                    window = int(str((query.get("window") or ["3600"])[0]) or "3600")
                except ValueError as exc:
                    raise LumaError("window must be a number") from exc
                self._json(200, handle_metrics_history(token, kind, name, window=window))
                return
            if parsed_path == "/v1/dashboard/logs/stream":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                service = str((query.get("service") or [""])[0])
                since = str((query.get("since") or [""])[0])
                try:
                    tail = int(str((query.get("tail") or ["120"])[0]) or "120")
                except ValueError as exc:
                    raise LumaError("tail must be a number") from exc
                self._stream_service_logs(token, service, since, tail)
                return
            config_match = re.fullmatch(r"/v1/deployments/([^/]+)/config", parsed_path)
            if config_match:
                name = urllib.parse.unquote(config_match.group(1))
                self._json(200, handle_deployment_config(token, name))
                return
        except LumaError as exc:
            code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            self._error(code, exc)
            return
        self._json(404, _error_payload("not_found", "not found", request_id=_request_id()))

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            token = bearer_token(self.headers)
            if self.path == "/v1/auth/login/verify":
                self._json(200, handle_login_verify(token))
                return
            if self.path == "/v1/nodes/register":
                self._json(200, handle_node_register(token, body))
                return
            if self.path == "/v1/nodes/label":
                self._json(200, handle_node_label(token, body))
                return
            if self.path == "/v1/nodes/agent-token":
                self._json(200, handle_node_agent_token(token, body))
                return
            if self.path == "/v1/node-agent/lease":
                self._json(200, handle_node_agent_lease(token, body))
                return
            if self.path == "/v1/node-agent/tasks/complete":
                self._json(200, handle_node_agent_complete(token, body))
                return
            if self.path == "/v1/nodes/unregister":
                self._json(200, handle_node_unregister(token, body))
                return
            if self.path == "/v1/deployments":
                self._json(200, handle_deployment(token, body))
                return
            if self.path == "/v1/deployments/preview":
                self._json(200, handle_deployment_preview(token, body))
                return
            if self.path == "/v1/deployments/stream":
                self._stream_deployment(token, body)
                return
            if self.path == "/v1/compose-deployments":
                self._json(200, handle_compose_deployment(token, body))
                return
            if self.path == "/v1/compose-deployments/preview":
                self._json(200, handle_compose_deployment_preview(token, body))
                return
            if self.path == "/v1/compose-deployments/stream":
                self._stream_compose_deployment(token, body)
                return
            if self.path == "/v1/storage/apply":
                self._json(200, handle_storage_apply(token, body))
                return
            if self.path == "/v1/storage":
                self._json(200, handle_storage_set(token, body))
                return
            if self.path == "/v1/storage/remove":
                self._json(200, handle_storage_remove(token, body))
                return
            if self.path == "/v1/services/remove":
                self._json(200, handle_service_remove(token, body))
                return
            if self.path == "/v1/applications/restart":
                self._json(200, handle_application_restart(token, body))
                return
            if self.path == "/v1/secrets":
                self._json(200, handle_secret_set(token, body))
                return
            if self.path == "/v1/registries":
                self._json(200, handle_registry_set(token, body))
                return
            if self.path == "/v1/registries/remove":
                self._json(200, handle_registry_remove(token, body))
                return
            self._json(404, _error_payload("not_found", "not found", request_id=_request_id()))
        except LumaError as exc:
            code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            self._error(code, exc)
        except Exception as exc:
            self._error(500, exc, code="internal_error")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _stream_deployment(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_deployment(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream deployment internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_compose_deployment(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_compose_deployment(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream compose deployment internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_service_logs(self, token: str, service: str, since: str, tail: int) -> None:
        # Authenticate and open the Docker stream BEFORE sending headers, so a
        # failure can still return a proper error status instead of a 200 with
        # an error body.
        require_token(load_state(), token, token_type="deploy")
        service = service.strip()
        if not service:
            raise LumaError("service is required")
        tail = min(max(int(tail or 120), 1), 1000)
        query_values: Dict[str, Any] = {"stdout": 1, "stderr": 1, "timestamps": 1, "follow": 1, "tail": tail}
        if since:
            query_values["since"] = since
        query = urllib.parse.urlencode(query_values)
        api_version = os.environ.get("DOCKER_API_VERSION", "1.44")
        conn = DockerSocketConnection()
        try:
            conn.request("GET", f"/v{api_version}/services/{urllib.parse.quote(service, safe='')}/logs?{query}")
            response = conn.getresponse()
        except OSError as exc:
            conn.close()
            raise LumaError("Docker socket unavailable to Luma Control") from exc
        if response.status >= 400:
            detail = response.read().decode("utf-8", errors="replace")
            conn.close()
            raise LumaError(f"Docker service logs unavailable for {service}: {detail}")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"status": "start", "service": service}, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()
            for line in _iter_docker_log_stream(response):
                event = {"line": line, "ts": int(time.time())}
                self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client navigated away / closed the tab; normal for a live tail.
            pass
        finally:
            conn.close()

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise LumaError("request body must be a JSON object")
        return data

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._bytes(status, raw, "application/json")

    def _error(self, status: int, exc: Exception, *, code: str = "luma_error") -> None:
        request_id = _request_id()
        if status >= 500:
            print(f"requestId={request_id} control API error: {exc}", file=sys.stderr, flush=True)
        self._json(status, _error_payload(code, str(exc), request_id=request_id))

    def _bytes(self, status: int, body: bytes, content_type: str, *, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def _download_text(self, filename: str, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), ControlHandler)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="luma-control")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--domain", required=True)
    init.add_argument("--cluster-id")
    init.add_argument("--overwrite", action="store_true")
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--host", default="0.0.0.0")
    serve_cmd.add_argument("--port", type=int, default=int(os.environ.get("LUMA_CONTROL_PORT", "8080")))
    args = parser.parse_args(argv)
    if args.command == "init":
        state = init_state(domain=args.domain, cluster_id=args.cluster_id, overwrite=args.overwrite)
        print(f"Cluster: {state['clusterId']}")
        print(f"Management token: {state['deployToken']}")
        print(f"Node join token: {state['joinToken']}")
        return 0
    if args.command == "serve":
        serve(args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
