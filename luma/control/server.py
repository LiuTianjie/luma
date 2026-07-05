from __future__ import annotations

import argparse
import asyncio
import base64
import functools
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, AsyncIterator

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
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
    storage_summary,
)
from ..config import LumaConfig, load_config
from ..errors import LumaError
from ..io import load_yaml
from ..local import LocalExecutor
from ..nomad_api import NomadApi, deploy_to_nomad, remove_from_nomad, revert_job, job_versions, nomad_addr, nomad_status_summary, nomad_services_summary
from ..nomad_render import render_nomad_job, render_compose_job
from ..registry import (
    docker_registry_auth_header,
    public_registry_url,
    image_uses_mutable_latest_tag,
    normalize_registry_host,
    registry_host_from_image,
    registry_auth_for_image,
    registry_auth_matches_image,
)
from ..render import named_volume_sources, render_tailscale_route, render_tcp_route, route_path, stack_path
from ..service import TCP_RELAY_RESERVED_PORTS, VALID_REGIONS, ServiceSpec, load_service, slugify, tcp_entrypoint_name
from .. import __version__
from .metrics import load_history, record_samples, sustained_breach
from .state import init_state, load_state, mutate_state, require_token
# Re-exported from the secrets leaf module so existing imports of these names
# from luma.control.server (callers and tests) keep working unchanged.
from .secrets import (
    _apply_state_secrets,
    _render_secrets,
    _request_env_secrets,
    _referenced_env_names,
    _valid_env_name,
)
# Re-exported from the resources leaf module (image-ref parsing, registry auth,
# docker egress-proxy inspection + egress constants). The image-pull
# orchestration that drives node-agent dispatch stays in server.py and imports
# these downward.
from .resources import (
    EGRESS_PROXY_URL,
    EGRESS_NO_PROXY,
    DEFAULT_EGRESS_PULL_REGISTRIES,
    _docker_info_no_proxy,
    _docker_info_no_proxy_contains,
    _docker_info_uses_egress_proxy,
    _docker_pull_error_message,
    _egress_pull_registries,
    _image_registry_host,
    _image_repo_from_repo_url,
    _image_repository,
    _merge_no_proxy,
    _no_proxy_entries_for_registry,
    _registry_auth_for_image,
    _registry_auth_for_service,
    _split_image_tag,
    normalize_import_repo_url,
)

AGENT_STALE_SECONDS = int(os.environ.get("LUMA_NODE_AGENT_STALE_SECONDS", "120"))
AGENT_TASK_TIMEOUT_SECONDS = int(os.environ.get("LUMA_NODE_AGENT_TASK_TIMEOUT_SECONDS", "300"))
# How long a finished (succeeded/failed/timeout) agent task is kept in
# control.json before it is garbage-collected. Without this, agentTasks grows
# without bound — every remote-node deploy adds an entry that is never deleted,
# and the whole file is re-serialized + fsynced on every heartbeat.
AGENT_TASK_RETENTION_SECONDS = int(os.environ.get("LUMA_NODE_AGENT_TASK_RETENTION_SECONDS", str(24 * 3600)))
TERMINAL_SESSION_LIMIT_PER_NODE = int(os.environ.get("LUMA_TERMINAL_SESSION_LIMIT_PER_NODE", "2"))
TERMINAL_IDLE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_TERMINAL_IDLE_TIMEOUT_SECONDS", "1800"))
# Sustained-breach alerting: a metric must stay above the threshold for the
# whole window before it becomes an issue, so a momentary spike does not page.
ALERT_SUSTAINED_SECONDS = int(os.environ.get("LUMA_ALERT_SUSTAINED_SECONDS", "300"))
ALERT_NODE_MEMORY_PERCENT = float(os.environ.get("LUMA_ALERT_NODE_MEMORY_PERCENT", "85"))
ALERT_NODE_CPU_PERCENT = float(os.environ.get("LUMA_ALERT_NODE_CPU_PERCENT", "90"))
TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS", "300"))
DEFAULT_BUILD_NODE_NAME = "builder"


def _control_config_path() -> Path:
    return Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")


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


def _node_agent_identity_ids(record: Dict[str, Any]) -> set[str]:
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    values = {str(agent.get("nodeId") or ""), str(agent.get("currentNodeId") or "")}
    for key in ("knownNodeIds", "legacyNodeIds"):
        raw = agent.get(key)
        if isinstance(raw, list):
            values.update(str(value) for value in raw)
        elif isinstance(raw, str):
            values.add(raw)
    return {value.strip() for value in values if value and value.strip()}


def _node_record_identity_ids(record: Dict[str, Any]) -> set[str]:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    values = {
        str(record.get("nodeId") or ""),
        str(record.get("nomadNodeId") or ""),
        str(labels.get("luma.node.id") or ""),
    }
    values.update(_node_agent_identity_ids(record))
    return {value.strip() for value in values if value and value.strip()}


def _remember_node_agent_identity(record: Dict[str, Any], node_id: str) -> None:
    value = str(node_id or "").strip()
    if not value:
        return
    agent = _node_agent_record(record)
    known = _node_agent_identity_ids(record)
    known.add(value)
    agent.setdefault("nodeId", value)
    agent["knownNodeIds"] = sorted(known)


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
        _remember_node_agent_identity(record, node_id)
        record["nodeId"] = node_id
    return token


def _node_record_entry_for_name_or_id(nodes: Dict[str, Any], node_name: str, node_id: str = "") -> tuple[str, Dict[str, Any]] | None:
    if node_name:
        direct = nodes.get(node_name)
        if isinstance(direct, dict):
            return node_name, direct
        for key, value in nodes.items():
            if isinstance(value, dict) and node_name in _node_record_names(str(key), value):
                return str(key), value
    for key, value in nodes.items():
        if not isinstance(value, dict):
            continue
        values = {str(value.get("displayName") or "")}
        values.update(_node_record_identity_ids(value))
        values.update(_node_record_names(str(key), value))
        if node_id and node_id in values:
            return str(key), value
        if node_name and node_name in values:
            return str(key), value
    return None


def _require_node_agent_token_entry(state: Dict[str, Any], token: str, node_name: str, *, node_id: str = "") -> tuple[str, Dict[str, Any]]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id=node_id)
    if entry is None:
        raise LumaError("unauthorized")
    canonical_name, record = entry
    if node_id:
        known_ids = _node_record_identity_ids(record)
        if known_ids and node_id not in known_ids:
            raise LumaError("unauthorized")
    agent = _node_agent_record(record)
    expected = str(agent.get("tokenHash") or "")
    if not expected or not secrets.compare_digest(expected, _hash_agent_token(token)):
        raise LumaError("unauthorized")
    return canonical_name, record


def _require_node_agent_token(state: Dict[str, Any], token: str, node_name: str, *, node_id: str = "") -> Dict[str, Any]:
    return _require_node_agent_token_entry(state, token, node_name, node_id=node_id)[1]


def _update_agent_heartbeat(
    record: Dict[str, Any],
    body: Dict[str, Any],
    *,
    config: Any | None = None,
    state: Dict[str, Any] | None = None,
) -> list[Dict[str, Any]]:
    agent = _node_agent_record(record)
    capabilities = body.get("capabilities")
    metrics = body.get("metrics")
    container_stats = body.get("containerStats")
    diagnostics = body.get("diagnostics")
    normalized_container_stats: list[Dict[str, Any]] = []
    agent.update(
        {
            "status": "online",
            "lastSeen": int(time.time()),
            "os": str(body.get("os") or agent.get("os") or ""),
            "arch": str(body.get("arch") or agent.get("arch") or ""),
            "capabilities": [str(value) for value in capabilities] if isinstance(capabilities, list) else agent.get("capabilities", []),
            "version": str(body.get("version") or agent.get("version") or __version__),
        }
    )
    if isinstance(metrics, dict):
        agent["metrics"] = _agent_metrics(metrics)
    if isinstance(container_stats, list):
        normalized_container_stats = _container_stats(container_stats)
        agent["containerStats"] = normalized_container_stats
    if isinstance(diagnostics, dict):
        agent["diagnostics"] = diagnostics
    return normalized_container_stats


def _record_metrics_history(
    node_name: str,
    body: Dict[str, Any],
    *,
    container_stats: list[Dict[str, Any]] | None = None,
    config: Any | None = None,
    state: Dict[str, Any] | None = None,
) -> None:
    """Append one time-series sample for this heartbeat, outside the global
    state lock. Metrics retention must never break a heartbeat, so any failure
    is logged and swallowed."""
    try:
        metrics = body.get("metrics") if isinstance(body.get("metrics"), dict) else {}
        if container_stats is None:
            raw_container_stats = body.get("containerStats") if isinstance(body.get("containerStats"), list) else []
            container_stats = _normalize_container_stats_for_engine(raw_container_stats, config=config, state=state)
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


def _normalize_container_stats_for_engine(
    raw_items: list[Any],
    *,
    config: Any | None = None,
    state: Dict[str, Any] | None = None,
    allocation_index: dict[str, Dict[str, str]] | None = None,
) -> list[Dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    try:
        cfg = config or load_config(_control_config_path())
    except Exception:
        cfg = None
    current_state = state if isinstance(state, dict) else {}
    defaults = getattr(cfg, "defaults", {}) if cfg else {}
    engine = str(defaults.get("engine") or "nomad")
    if engine != "nomad":
        return _container_stats(raw_items)
    if allocation_index is None:
        allocation_index = _nomad_allocation_service_index(cfg, current_state) if cfg else {}
    try:
        _, compose_stacks = _dashboard_deployment_service_index(current_state)
    except Exception:
        compose_stacks = set()
    normalized: list[Dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        service = str(item.get("service") or "").strip()
        allocation_id = str(item.get("nomadAllocId") or item.get("allocationId") or "").strip()
        if service.startswith("nomad:") and not allocation_id:
            allocation_id = service.split(":", 1)[1].strip()
        if allocation_id:
            item["nomadAllocId"] = allocation_id
            meta = _lookup_nomad_allocation(allocation_index, allocation_id)
            if meta:
                base_service = str(meta.get("service") or service)
                nomad_task = str(item.get("nomadTask") or meta.get("task") or "")
                item["service"] = (
                    _dashboard_task_full_name(base_service, nomad_task, compose=True)
                    if base_service in compose_stacks and nomad_task
                    else base_service
                )
                item["nomadTask"] = nomad_task
                item["nomadGroup"] = str(meta.get("group") or item.get("nomadGroup") or "")
                item["nomadNode"] = str(meta.get("node") or "")
                item["nomadNodeId"] = str(meta.get("nodeId") or "")
        normalized.append(item)
    return _container_stats(normalized)


def _nomad_allocation_service_index(config: Any, state: Dict[str, Any]) -> dict[str, Dict[str, str]]:
    try:
        allocations = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or "")).request("GET", "/v1/allocations")
    except Exception:
        return {}
    if not isinstance(allocations, list):
        return {}
    index: dict[str, Dict[str, str]] = {}
    for allocation in allocations:
        if not isinstance(allocation, dict):
            continue
        allocation_id = str(allocation.get("ID") or "").strip()
        job_id = str(allocation.get("JobID") or "").strip()
        if not allocation_id or not job_id:
            continue
        task_states = allocation.get("TaskStates") if isinstance(allocation.get("TaskStates"), dict) else {}
        task_names = [str(name) for name in task_states if str(name)]
        meta = {
            "allocationId": allocation_id,
            "service": job_id,
            "group": str(allocation.get("TaskGroup") or ""),
            "task": task_names[0] if len(task_names) == 1 else "",
            "node": str(allocation.get("NodeName") or ""),
            "nodeId": str(allocation.get("NodeID") or ""),
        }
        for key in {allocation_id, allocation_id[:12], allocation_id[:8]}:
            if key:
                index[key] = meta
    return index


def _lookup_nomad_allocation(index: dict[str, Dict[str, str]], allocation_id: str) -> Dict[str, str]:
    allocation_id = str(allocation_id or "").strip()
    if not allocation_id:
        return {}
    direct = index.get(allocation_id)
    if direct:
        return direct
    for key, value in index.items():
        if key.startswith(allocation_id) or allocation_id.startswith(key):
            return value
    return {}


def _container_stats(raw_items: list[Any]) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    for raw in raw_items[:250]:
        if not isinstance(raw, dict):
            continue
        service = str(raw.get("service") or "").strip()
        nomad_alloc_id = str(raw.get("nomadAllocId") or raw.get("allocationId") or "").strip()
        if not service and nomad_alloc_id:
            service = f"nomad:{nomad_alloc_id}"
        container_id = str(raw.get("containerId") or "").strip()
        if not service or not container_id:
            continue
        item: Dict[str, Any] = {
            "service": service,
            "containerId": container_id[:12],
            "name": str(raw.get("name") or ""),
            "taskId": str(raw.get("taskId") or ""),
        }
        for key in ("nomadAllocId", "nomadTask", "nomadGroup", "nomadNode", "nomadNodeId"):
            value = str(raw.get(key) or "").strip()
            if value:
                item[key] = value
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
    if status == "ready":
        return "ready"
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


def _build_config(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.get("build") if isinstance(state.get("build"), dict) else {}


def _declared_build_node_names(state: Dict[str, Any]) -> list[str]:
    config = _build_config(state)
    raw_nodes = config.get("nodes")
    names: list[str] = []
    if isinstance(raw_nodes, list):
        names.extend(str(value).strip() for value in raw_nodes)
    elif isinstance(raw_nodes, dict):
        names.extend(str(name).strip() for name, enabled in raw_nodes.items() if enabled)

    default_node = str(config.get("defaultNode") or "").strip()
    if default_node:
        names.append(default_node)

    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for name, record in nodes.items():
        if isinstance(record, dict) and _node_record_is_declared_builder(record):
            names.append(str(name).strip())

    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _node_record_is_declared_builder(record: Dict[str, Any]) -> bool:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    roles = {str(value).strip().lower() for value in record.get("roles") or []}
    role_values = {
        str(record.get("role") or "").strip().lower(),
        str(labels.get("role") or "").strip().lower(),
        str(labels.get("luma.role") or "").strip().lower(),
        str(labels.get("luma.node.role") or "").strip().lower(),
    }
    return (
        "builder" in roles
        or "build" in roles
        or "builder" in role_values
        or "build" in role_values
        or str(labels.get("role.builder") or "").strip().lower() == "true"
        or str(labels.get("luma.builder") or "").strip().lower() == "true"
    )


def _build_node_records(state: Dict[str, Any], *, require_ready: bool = True) -> list[Dict[str, Any]]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    declared = _declared_build_node_names(state)
    if not declared and DEFAULT_BUILD_NODE_NAME in nodes:
        declared = [DEFAULT_BUILD_NODE_NAME]
    records: list[Dict[str, Any]] = []
    for name in declared:
        record = _node_record_for_name(nodes, name)
        if not isinstance(record, dict):
            continue
        if require_ready and not _node_agent_is_ready(record, required_capability="docker-build"):
            continue
        item = dict(record)
        item["name"] = name
        records.append(item)
    return records


def _require_build_node(state: Dict[str, Any], node_name: str, *, purpose: str) -> str:
    value = str(node_name or "").strip()
    if not value:
        raise LumaError("buildNode is required")
    allowed = {str(record.get("name") or "").strip() for record in _build_node_records(state, require_ready=True)}
    if value not in allowed:
        declared = _declared_build_node_names(state)
        suffix = f" Declared build nodes: {', '.join(declared)}." if declared else " No build nodes are declared."
        raise LumaError(f"{purpose} must target a declared, ready builder node with docker-build capability: {value}.{suffix}")
    return value


def _agent_tasks(state: Dict[str, Any]) -> Dict[str, Any]:
    tasks = state.setdefault("agentTasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        state["agentTasks"] = tasks
    return tasks


def _build_runs(state: Dict[str, Any]) -> Dict[str, Any]:
    runs = state.setdefault("buildRuns", {})
    if not isinstance(runs, dict):
        runs = {}
        state["buildRuns"] = runs
    return runs


def _redact_build_request(body: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in body.items():
        if key == "envSecrets" and isinstance(value, dict):
            result["envSecretNames"] = sorted(str(name) for name in value)
            continue
        if key in {"gitToken", "registryAuth", "token", "password"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        elif isinstance(value, list):
            result[key] = [item for item in value if isinstance(item, (str, int, float, bool))]
        elif isinstance(value, dict):
            result[key] = {str(k): v for k, v in value.items() if isinstance(v, (str, int, float, bool))}
    return result


def _create_build_run(body: Dict[str, Any], *, source: str, build_node: str) -> str:
    run_id = f"build-{secrets.token_hex(8)}"
    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        runs = _build_runs(state)
        runs[run_id] = {
            "id": run_id,
            "status": "running",
            "source": source,
            "buildNode": build_node,
            "request": _redact_build_request(body),
            "events": [],
            "createdAt": now,
            "updatedAt": now,
        }
        _prune_build_runs(state)

    _mutate_control_state(mutate)
    return run_id


def _append_build_run_event(run_id: str, event: Dict[str, Any]) -> None:
    safe_event = {str(key): str(value) for key, value in event.items() if value is not None}

    def mutate(state: Dict[str, Any]) -> None:
        run = _build_runs(state).get(run_id)
        if not isinstance(run, dict):
            return
        events = run.get("events")
        if not isinstance(events, list):
            events = []
            run["events"] = events
        events.append({**safe_event, "ts": int(time.time())})
        run["updatedAt"] = int(time.time())
        status = str(safe_event.get("status") or "")
        if status == "fail":
            run["status"] = "failed"
            run["message"] = str(safe_event.get("message") or "")

    _mutate_control_state(mutate)


def _complete_build_run(run_id: str, status: str, *, result: Dict[str, Any] | None = None, message: str = "") -> None:
    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        run = _build_runs(state).get(run_id)
        if not isinstance(run, dict):
            return
        run["status"] = status
        run["updatedAt"] = now
        run["completedAt"] = now
        if message:
            run["message"] = message
        if isinstance(result, dict):
            run["result"] = _build_run_result_summary(result)

    _mutate_control_state(mutate)


def _restart_build_run(run_id: str, body: Dict[str, Any], *, source: str, build_node: str) -> None:
    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        run = _build_runs(state).get(run_id)
        if not isinstance(run, dict):
            raise LumaError(f"build run not found: {run_id}")
        run["status"] = "running"
        run["source"] = source
        run["buildNode"] = build_node
        run["request"] = _redact_build_request(body)
        run["events"] = []
        run["message"] = ""
        run.pop("result", None)
        run.pop("completedAt", None)
        run["updatedAt"] = now

    _mutate_control_state(mutate)


def _build_run_result_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in ("service", "deployment", "image", "images", "dns", "orchestrator"):
        value = result.get(key)
        if value not in (None, "", [], {}):
            summary[key] = value
    return summary


def _prune_build_runs(state: Dict[str, Any], *, limit: int = 100) -> None:
    runs = _build_runs(state)
    if len(runs) <= limit:
        return
    ordered = sorted(
        ((str(run_id), run) for run_id, run in runs.items() if isinstance(run, dict)),
        key=lambda item: int(item[1].get("updatedAt") or item[1].get("createdAt") or 0),
        reverse=True,
    )
    keep = {run_id for run_id, _run in ordered[:limit]}
    for run_id in list(runs):
        if run_id not in keep:
            runs.pop(run_id, None)


def _prune_agent_tasks(state: Dict[str, Any], *, now: int | None = None) -> None:
    """Drop finished agent tasks older than the retention window.

    Terminal tasks (succeeded/failed/timeout) are kept only briefly for history;
    queued/running tasks are never pruned (a deploy may still be polling them).
    Called from the same mutate that inserts a new task, so it rides an existing
    state write and adds no extra fsync.
    """
    now = int(time.time()) if now is None else now
    cutoff = now - AGENT_TASK_RETENTION_SECONDS
    tasks = _agent_tasks(state)
    terminal = {"succeeded", "failed", "timeout"}
    stale = [
        task_id
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("status") or "") in terminal
        and int(task.get("completedAt") or task.get("updatedAt") or 0) < cutoff
    ]
    for task_id in stale:
        tasks.pop(task_id, None)


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
    normalized_container_stats: list[Dict[str, Any]] = []
    config = load_config(_control_config_path())
    wait_seconds = min(max(int(body.get("waitSeconds") or 0), 0), 30)
    deadline = time.time() + wait_seconds
    while True:
        def mutate(state: Dict[str, Any]) -> Dict[str, Any] | None:
            nonlocal normalized_container_stats
            nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
            entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
            canonical_node_name = entry[0] if entry else node_name
            record = _require_node_agent_token(state, token, node_name, node_id=node_id)
            normalized_container_stats = _update_agent_heartbeat(record, body, config=config, state=state)
            tasks = _agent_tasks(state)
            now = int(time.time())
            for task_id in sorted(tasks):
                task = tasks.get(task_id)
                if not isinstance(task, dict):
                    continue
                if task.get("nodeName") != canonical_node_name or task.get("status") != "queued":
                    continue
                task["status"] = "running"
                task["leasedAt"] = now
                task["updatedAt"] = now
                return {
                    "id": task_id,
                    "action": task.get("action"),
                    "payload": _agent_task_lease_payload(state, task),
                }
            return None

        leased = _mutate_control_state(mutate)
        if leased or time.time() >= deadline:
            break
        time.sleep(1)
    state = load_state()
    entry = _node_record_entry_for_name_or_id(state.get("nodes") if isinstance(state.get("nodes"), dict) else {}, node_name, node_id)
    _record_metrics_history(entry[0] if entry else node_name, body, config=config, state=state)
    return {"task": leased}


def handle_node_agent_heartbeat(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    if not node_name:
        raise LumaError("nodeName is required")
    canonical_node_name = node_name
    normalized_container_stats: list[Dict[str, Any]] = []
    config = load_config(_control_config_path())

    def mutate(state: Dict[str, Any]) -> None:
        nonlocal canonical_node_name, normalized_container_stats
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
        canonical_node_name = entry[0] if entry else node_name
        record = _require_node_agent_token(state, token, node_name, node_id=node_id)
        normalized_container_stats = _update_agent_heartbeat(record, body, config=config, state=state)

    _mutate_control_state(mutate)
    state = load_state()
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, canonical_node_name) or {}
    _record_metrics_history(canonical_node_name, body, container_stats=normalized_container_stats, config=config, state=state)
    return {"nodeName": canonical_node_name, "status": _node_agent_status(record)}


def _agent_task_lease_payload(state: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(task.get("payload") if isinstance(task.get("payload"), dict) else {})
    if task.get("action") in {"resolve-docker-image", "diagnose-docker-pull"} and not payload.get("registryAuth"):
        image = str(payload.get("image") or "")
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        registry_auth = registry_auth_for_image(registries, image)
        if registry_auth:
            payload["registryAuth"] = registry_auth
    if task.get("action") == "join-nomad" and not payload.get("tailscaleAuthKey"):
        secrets_state = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
        tailscale_authkey = str(secrets_state.get("TAILSCALE_AUTHKEY") or "") or os.environ.get("TAILSCALE_AUTHKEY") or ""
        if tailscale_authkey:
            payload["tailscaleAuthKey"] = tailscale_authkey
    if task.get("action") == "build-image":
        # Inject credentials at lease time so they are never persisted in
        # agentTasks state (see also resolve-docker-image / join-nomad above).
        if not payload.get("gitToken"):
            git_token = ""
            provider_id = str(payload.get("gitProviderId") or "").strip()
            if provider_id:
                git_providers = state.get("gitProviders") if isinstance(state.get("gitProviders"), dict) else {}
                provider = git_providers.get(provider_id)
                if isinstance(provider, dict):
                    git_token = str(provider.get("token") or "")
                    git_username = str(provider.get("username") or "").strip()
                    if git_username and not payload.get("gitUsername"):
                        payload["gitUsername"] = git_username
            elif not git_token:
                secrets_state = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
                git_token = str(secrets_state.get("GITHUB_TOKEN") or "") or os.environ.get("GITHUB_TOKEN") or ""
            if git_token:
                payload["gitToken"] = git_token
        if not payload.get("registryAuth"):
            push_host = str(payload.get("pushHost") or "")
            repo = str(payload.get("repo") or "")
            if push_host and repo:
                registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
                registry_auth = registry_auth_for_image(registries, f"{push_host}/{repo}:latest")
                if registry_auth:
                    payload["registryAuth"] = registry_auth
    return payload


def handle_node_agent_complete(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    task_id = str(body.get("taskId") or "").strip()
    status = str(body.get("status") or "").strip()
    if status not in {"succeeded", "failed"}:
        raise LumaError("status must be succeeded or failed")
    if not node_name or not task_id:
        raise LumaError("nodeName and taskId are required")

    normalized_container_stats: list[Dict[str, Any]] = []
    config = load_config(_control_config_path())

    def mutate(state: Dict[str, Any]) -> None:
        nonlocal normalized_container_stats
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
        canonical_node_name = entry[0] if entry else node_name
        record = _require_node_agent_token(state, token, node_name, node_id=node_id)
        normalized_container_stats = _update_agent_heartbeat(record, body, config=config, state=state)
        tasks = _agent_tasks(state)
        task = tasks.get(task_id)
        if not isinstance(task, dict) or task.get("nodeName") != canonical_node_name:
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
    state = load_state()
    entry = _node_record_entry_for_name_or_id(state.get("nodes") if isinstance(state.get("nodes"), dict) else {}, node_name, node_id)
    _record_metrics_history(entry[0] if entry else node_name, body, config=config, state=state)
    return {"taskId": task_id, "status": status}


def handle_node_agent_progress(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    task_id = str(body.get("taskId") or "").strip()
    if not node_name or not task_id:
        raise LumaError("nodeName and taskId are required")
    raw_events = body.get("events") if isinstance(body.get("events"), list) else []
    events = _agent_progress_events(raw_events)
    if not events:
        return {"taskId": task_id, "events": 0}

    def mutate(state: Dict[str, Any]) -> None:
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
        canonical_node_name = entry[0] if entry else node_name
        _require_node_agent_token(state, token, node_name, node_id=node_id)
        tasks = _agent_tasks(state)
        task = tasks.get(task_id)
        if not isinstance(task, dict) or task.get("nodeName") != canonical_node_name:
            raise LumaError(f"agent task not found: {task_id}")
        progress = task.get("progress") if isinstance(task.get("progress"), list) else []
        progress.extend(events)
        task["progress"] = progress[-5000:]
        task["updatedAt"] = int(time.time())

    _mutate_control_state(mutate)
    return {"taskId": task_id, "events": len(events)}


def _agent_progress_events(raw_events: list[Any]) -> list[Dict[str, Any]]:
    now = int(time.time())
    events: list[Dict[str, Any]] = []
    for raw in raw_events[:50]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "output").strip() or "output"
        line = str(raw.get("line") or raw.get("message") or "")
        if not line.strip():
            continue
        events.append({"type": kind[:40], "line": line[:4000], "ts": int(raw.get("ts") or now)})
    return events


def _run_node_agent_task(
    state: Dict[str, Any],
    node_name: str,
    action: str,
    payload: Dict[str, Any],
    *,
    timeout: int | None = None,
    required_capability: str | None = "nfs-host",
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    task_id = _queue_node_agent_task(state, node_name, action, payload, required_capability=required_capability)
    return _wait_node_agent_task(task_id, node_name, action, timeout=timeout, progress=progress)


def _queue_node_agent_task(
    state: Dict[str, Any],
    node_name: str,
    action: str,
    payload: Dict[str, Any],
    *,
    required_capability: str | None = "nfs-host",
) -> str:
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
        _prune_agent_tasks(current, now=now)
        _agent_tasks(current)[task_id] = {
            "id": task_id,
            "nodeName": node_name,
            "action": action,
            "payload": dict(payload),
            "progress": [],
            "status": "queued",
            "createdAt": now,
            "updatedAt": now,
        }

    _mutate_control_state(mutate)
    return task_id


def _wait_node_agent_task(
    task_id: str,
    node_name: str,
    action: str,
    *,
    timeout: int | None = None,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    deadline = time.time() + float(timeout or AGENT_TASK_TIMEOUT_SECONDS)
    cursor = 0
    while time.time() < deadline:
        current = load_state()
        task = (current.get("agentTasks") if isinstance(current.get("agentTasks"), dict) else {}).get(task_id)
        if isinstance(task, dict):
            task_progress = task.get("progress") if isinstance(task.get("progress"), list) else []
            safe_cursor = min(max(cursor, 0), len(task_progress))
            for event in [item for item in task_progress[safe_cursor:] if isinstance(item, dict)]:
                line = str(event.get("line") or event.get("message") or "").strip()
                if line:
                    _emit_progress(progress, {"name": _agent_task_progress_step_name(action), "status": "progress", "message": line})
            cursor = len(task_progress)
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


def _agent_task_progress_step_name(action: str) -> str:
    if action == "build-image":
        return "Build image"
    if action == "diagnose-docker-pull":
        return "Docker pull"
    return "Agent task"


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
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    registered_nodes = _registered_nodes_summary(nodes)
    orchestrator = nomad_status_summary(config, state)
    nomad_services = nomad_services_summary(config, state)
    result = {
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
        "nodes": {
            "registered": len(registered_nodes),
            "names": [item["name"] for item in registered_nodes],
            "items": registered_nodes,
        },
        "nomad": {
            "available": bool(orchestrator.get("available")),
            "leader": orchestrator.get("leader", ""),
            "nodes": orchestrator.get("nodes", []),
        },
        "services": nomad_services,
        "storage": {
            "storageClasses": _storage_classes_summary(state),
        },
        "build": _build_summary(state),
    }
    return result


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
    errors: list[str] = []

    engine = _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    nomad_summary = nomad_status_summary(config, state)
    raw_nodes = nomad_summary.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    registered_nodes = _registered_nodes_summary(
        state.get("nodes") if isinstance(state.get("nodes"), dict) else {},
    )
    nodes = _dashboard_nodes(registered_nodes, raw_nodes, terminal_nodes=TERMINAL_BROKER.connected_nodes())

    route_files = _dashboard_route_files(config, config_path, errors)
    services = _dashboard_nomad_services(nomad_services_summary(config, state), route_files, state=state)
    service_stats = _service_stats_by_name(registered_nodes, config=config, state=state)
    for service in services:
        _attach_service_actual_resources(service, service_stats.get(str(service.get("fullName") or ""), []))
    traffic_paths = _dashboard_traffic_paths(services, route_files, dns_target)
    storage = _dashboard_storage(services, _storage_classes_summary(state))
    public_services = [_public_dashboard_service(item) for item in services]
    issues = _dashboard_issues(nodes, public_services)

    readiness = {
        "dns": {
            "ready": not dns_missing,
            "provider": dns_provider,
            "zone": str(dns.get("zone") or ""),
            "target": dns_target,
            "missing": dns_missing,
        },
        "nomad": {
            "available": bool(nomad_summary.get("available")),
            "engine": engine,
            "leader": str(nomad_summary.get("leader") or ""),
            "error": str(nomad_summary.get("error") or ""),
        },
    }

    return {
        "cluster": {
            "id": str(state.get("clusterId") or ""),
            "version": __version__,
            "configPath": str(config_path),
        },
        "readiness": readiness,
        "nodes": nodes,
        "services": public_services,
        "trafficPaths": traffic_paths,
        "storage": storage,
        "build": _build_summary(state),
        "issues": issues,
        "errors": errors,
    }


def handle_dashboard_logs(token: str, service_name: str, *, tail: int = 120, since: str = "") -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    service = service_name.strip()
    if not service:
        raise LumaError("service is required")
    tail = min(max(int(tail or 120), 1), 500)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    return {
        "service": service,
        "logs": _nomad_log_lines(config, state, service, tail=tail),
        "tail": tail,
        "since": since,
        "updatedAt": int(time.time()),
    }


def handle_build_run_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    runs = state.get("buildRuns") if isinstance(state.get("buildRuns"), dict) else {}
    items = [_build_run_public_summary(run) for run in runs.values() if isinstance(run, dict)]
    items.sort(key=lambda item: int(item.get("updatedAt") or item.get("createdAt") or 0), reverse=True)
    return {"runs": items}


def handle_build_run_get(token: str, build_id: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    run = (state.get("buildRuns") if isinstance(state.get("buildRuns"), dict) else {}).get(build_id)
    if not isinstance(run, dict):
        raise LumaError(f"build run not found: {build_id}")
    return {"run": _build_run_public(run)}


def handle_build_run_retry(token: str, build_id: str, *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    run = (state.get("buildRuns") if isinstance(state.get("buildRuns"), dict) else {}).get(build_id)
    if not isinstance(run, dict):
        raise LumaError(f"build run not found: {build_id}")
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    body = {key: value for key, value in request.items() if key != "envSecretNames"}
    if not body:
        raise LumaError(f"build run cannot be retried: {build_id}")
    return handle_build_deploy(token, body, progress=progress, build_run_id=build_id)


def handle_build_config_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    nodes_raw = body.get("nodes")
    node = str(body.get("node") or "").strip()
    nodes = [str(value).strip() for value in nodes_raw] if isinstance(nodes_raw, list) else []
    if node:
        nodes = [node]
    nodes = [value for value in nodes if value]
    default_node = str(body.get("defaultNode") or (nodes[0] if nodes else "")).strip()

    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        require_token(state, token, token_type="deploy")
        registered = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        for name in nodes:
            if _node_record_for_name(registered, name) is None:
                raise LumaError(f"unknown Luma node: {name}")
        if default_node and default_node not in nodes:
            nodes.insert(0, default_node)
        build = state.get("build") if isinstance(state.get("build"), dict) else {}
        build = dict(build)
        if nodes:
            build["nodes"] = nodes
        if default_node:
            build["defaultNode"] = default_node
        for key in ("registryHost", "pushHost"):
            if body.get(key) is not None:
                value = str(body.get(key) or "").strip()
                if value:
                    build[key] = value
                else:
                    build.pop(key, None)
        state["build"] = build
        return _build_summary(state)

    return {"build": _mutate_control_state(mutate)}


def _build_run_public_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    return {
        "id": str(run.get("id") or ""),
        "status": str(run.get("status") or ""),
        "source": str(run.get("source") or request.get("repoUrl") or request.get("repository") or ""),
        "buildNode": str(run.get("buildNode") or request.get("buildNode") or ""),
        "providerId": str(request.get("providerId") or ""),
        "repository": str(request.get("repository") or ""),
        "ref": str(request.get("ref") or ""),
        "message": str(run.get("message") or ""),
        "createdAt": int(run.get("createdAt") or 0),
        "updatedAt": int(run.get("updatedAt") or 0),
        "completedAt": int(run.get("completedAt") or 0),
    }


def _build_run_public(run: Dict[str, Any]) -> Dict[str, Any]:
    result = _build_run_public_summary(run)
    result["request"] = run.get("request") if isinstance(run.get("request"), dict) else {}
    result["events"] = [event for event in run.get("events") or [] if isinstance(event, dict)]
    result["result"] = run.get("result") if isinstance(run.get("result"), dict) else {}
    return result


def handle_service_pull_diagnostics(token: str, service_name: str, *, timeout: int = 600) -> Dict[str, Any]:
    started = _start_service_pull_diagnostics(token, service_name, timeout=timeout)
    result = _wait_node_agent_task(
        str(started["taskId"]),
        str(started["node"]),
        "diagnose-docker-pull",
        timeout=int(started["timeout"]) + 30,
    )
    return _service_pull_diagnostics_result(started, result)


def _start_service_pull_diagnostics(token: str, service_name: str, *, timeout: int = 600) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    service = service_name.strip()
    if not service:
        raise LumaError("service is required")
    timeout = min(max(int(timeout or 600), 30), 1800)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    target = _nomad_pull_diagnostic_target(config, state, service)
    registry_auth = registry_auth_for_image(
        state.get("registries") if isinstance(state.get("registries"), dict) else {},
        target["image"],
    )
    payload: Dict[str, Any] = {"image": target["image"], "timeout": timeout}
    if target.get("platform"):
        payload["platform"] = str(target["platform"])
    task_id = _queue_node_agent_task(
        state,
        str(target["node"]),
        "diagnose-docker-pull",
        payload,
        required_capability="docker-image",
    )
    return {
        "service": service,
        "job": target["job"],
        "task": target["task"],
        "allocId": target["allocId"],
        "node": target["node"],
        "image": target["image"],
        "registryAuth": bool(registry_auth),
        "taskId": task_id,
        "timeout": timeout,
        "updatedAt": int(time.time()),
    }


def _service_pull_diagnostics_result(started: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    lines = result.get("lines") if isinstance(result.get("lines"), list) else []
    return {
        "service": str(started.get("service") or ""),
        "job": str(started.get("job") or ""),
        "task": str(started.get("task") or ""),
        "allocId": str(started.get("allocId") or ""),
        "node": str(started.get("node") or ""),
        "image": str(started.get("image") or ""),
        "registryAuth": bool(started.get("registryAuth")),
        "ok": bool(result.get("ok")),
        "exitCode": int(result.get("exitCode") or 0),
        "output": str(result.get("output") or ""),
        "lines": [str(line) for line in lines],
        "taskId": str(started.get("taskId") or result.get("taskId") or ""),
        "updatedAt": int(time.time()),
    }


def _agent_task_progress_snapshot(task_id: str, cursor: int) -> tuple[list[Dict[str, Any]], int, str, Dict[str, Any], str]:
    state = load_state()
    task = (state.get("agentTasks") if isinstance(state.get("agentTasks"), dict) else {}).get(task_id)
    if not isinstance(task, dict):
        return [], cursor, "missing", {}, "agent task not found"
    progress = task.get("progress") if isinstance(task.get("progress"), list) else []
    safe_cursor = min(max(int(cursor or 0), 0), len(progress))
    events = [event for event in progress[safe_cursor:] if isinstance(event, dict)]
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return events, len(progress), str(task.get("status") or ""), result, str(task.get("message") or "")


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
        require_control_node_token(state, token)
        _remember_node(state, node_name, region=region, status="registered")

    mutate_state(mutate)
    state = load_state()
    config = load_config(_control_config_path())
    nomad_rpc_addr = _nomad_rpc_addr_for_join(config, state)
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "region": region,
        "nomadRpcAddr": nomad_rpc_addr,
        "nomadServerAddr": nomad_rpc_addr,
    }


def _nomad_rpc_addr_for_join(config: Any, state: Dict[str, Any]) -> str:
    configured = str(state.get("nomadRpcAddr") or state.get("nomadServerAddr") or config.defaults.get("nomadServer") or "").strip()
    if configured:
        return _with_default_port(configured, 4647)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for record in nodes.values():
        if not isinstance(record, dict):
            continue
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        is_manager = (
            str(record.get("status") or "") == "manager"
            or "nomad-manager" in {str(value) for value in record.get("roles") or []}
            or str(labels.get("role.nomad-manager") or "").lower() == "true"
        )
        if not is_manager:
            continue
        host = str(record.get("tailscaleIP") or record.get("advertiseAddr") or record.get("publicIp") or record.get("publicIP") or "").strip()
        if host:
            return _with_default_port(host, 4647)
    nomad_http = str(state.get("nomadAddr") or config.defaults.get("nomadAddr") or "").strip()
    if nomad_http:
        parsed = urllib.parse.urlparse(nomad_http if "://" in nomad_http else f"http://{nomad_http}")
        if parsed.hostname and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return _with_default_port(parsed.hostname, 4647)
    raise LumaError("cannot resolve Nomad RPC address for node join; set defaults.nomadServer or register a nomad-manager node with tailscaleIP")


def _with_default_port(host: str, port: int) -> str:
    value = str(host or "").strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        hostname = parsed.hostname or ""
        if not hostname:
            return value
        return f"[{hostname}]:{parsed.port or port}" if ":" in hostname and not hostname.startswith("[") else f"{hostname}:{parsed.port or port}"
    if value.startswith("[") and "]" in value:
        return value if value.rsplit(":", 1)[-1].isdigit() else f"{value}:{port}"
    if value.count(":") == 0:
        return f"{value}:{port}"
    if value.count(":") == 1 and value.rsplit(":", 1)[-1].isdigit():
        return value
    return f"[{value}]:{port}"


def _host_from_hostport(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    if text.count(":") > 1:
        return text
    return text


def _nomad_join_egress_proxy(region: str, server_addr: str) -> str:
    if region not in {"cn", "home"}:
        return ""
    host = _host_from_hostport(server_addr)
    return f"http://{host}:7890" if host else ""


def _egress_proxy_for_region(config: Any, state: Dict[str, Any], region: str) -> str:
    # Runtime egress proxy for a deployed service's container. cn/home services
    # reach the internet through the manager's egress gateway (same address as
    # image pulls and node join); global services have direct internet -> no
    # proxy. Returns "" when no gateway is resolvable so render injects nothing.
    if str(region or "").strip() not in {"cn", "home"}:
        return ""
    try:
        server_addr = _nomad_rpc_addr_for_join(config, state)
    except LumaError:
        return ""
    return _nomad_join_egress_proxy(region, server_addr)


def _egress_proxy_for_node(config: Any, state: Dict[str, Any], node_name: str) -> str:
    # git clone runs on the build node's host. cn/home nodes reach the internet
    # through the manager's egress gateway (same as image pulls and node join);
    # global nodes have direct internet, so no proxy.
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if not record:
        return ""
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    region = str(record.get("region") or labels.get("region") or "").strip()
    if region not in {"cn", "home"}:
        return ""
    try:
        server_addr = _nomad_rpc_addr_for_join(config, state)
    except LumaError:
        return ""
    return _nomad_join_egress_proxy(region, server_addr)


def _docker_daemon_proxy_for_node(config: Any, state: Dict[str, Any], node_name: str, node_record: Dict[str, Any]) -> str:
    agent = node_record.get("agent") if isinstance(node_record.get("agent"), dict) else {}
    diagnostics = agent.get("diagnostics") if isinstance(agent.get("diagnostics"), dict) else {}
    docker = diagnostics.get("docker") if isinstance(diagnostics.get("docker"), dict) else {}
    proxy = docker.get("proxy") if isinstance(docker.get("proxy"), dict) else {}
    configured_proxy = str(proxy.get("http") or proxy.get("https") or "").strip()
    if configured_proxy:
        return configured_proxy
    return _egress_proxy_for_node(config, state, node_name)


def handle_node_label(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    node_name = str(body.get("nodeName") or "").strip()
    registered_name = str(body.get("registeredName") or "").strip()
    node_id = str(body.get("nodeId") or "").strip()
    tailscale_ip = str(body.get("tailscaleIP") or "").strip()
    tailscale_name = str(body.get("tailscaleName") or "").strip()
    region = str(body.get("region") or "").strip()
    if not node_name or not region:
        raise LumaError("nodeName and region are required")
    if not node_id:
        raise LumaError("nodeId is required; update the Luma CLI on this node and rerun luma node join")
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    luma_name = registered_name or node_name
    labels = labels_for_node(region, luma_name=luma_name, node_id=node_id)
    values: Dict[str, Any] = {
        "region": region,
        "status": "labeled",
        "labels": labels,
        "displayName": luma_name,
        "hostname": node_name,
        "nodeId": node_id,
        "nomadNodeId": node_id,
        "nomadHostname": node_name,
    }
    if tailscale_ip:
        values["tailscaleIP"] = tailscale_ip
    if tailscale_name:
        values["tailscaleName"] = tailscale_name
    def mutate(state: Dict[str, Any]) -> Dict[str, str]:
        require_control_node_token(state, token)
        previous = _node_record_for_name(state.get("nodes") if isinstance(state.get("nodes"), dict) else {}, luma_name)
        previous_labels = previous.get("labels") if isinstance(previous, dict) and isinstance(previous.get("labels"), dict) else {}
        previous_node_id = str((previous or {}).get("nodeId") or (previous or {}).get("nomadNodeId") or previous_labels.get("luma.node.id") or "").strip() if isinstance(previous, dict) else ""
        _remember_node(state, luma_name, **values)
        agent_token = _issue_node_agent_token(state, luma_name, node_id=node_id)
        return {"agentToken": agent_token, "previousNodeId": previous_node_id}

    mutation_result = mutate_state(mutate)
    agent_token = mutation_result["agentToken"]
    previous_node_id = mutation_result.get("previousNodeId") or ""
    state = load_state()
    result = {
        "clusterId": state["clusterId"],
        "nodeName": luma_name,
        "hostname": node_name,
        "nodeId": node_id,
        "previousNodeId": previous_node_id,
        "displayName": luma_name,
        "tailscaleIP": tailscale_ip,
        "tailscaleName": tailscale_name,
        "agentToken": agent_token,
        "labels": labels,
        "message": f"Node labels applied: {luma_name}",
        "nomadHostname": node_name,
        "nomadNodeId": node_id,
    }
    return result


def handle_node_nomad_join(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    config = load_config(_control_config_path())
    node_name = str(body.get("nodeName") or "").strip()
    if not node_name:
        raise LumaError("nodeName is required")
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if record is None:
        raise LumaError(f"Luma node is not registered: {node_name}")
    region = str(body.get("region") or record.get("region") or "").strip()
    if region not in VALID_REGIONS:
        raise LumaError(f"node region must be one of {sorted(VALID_REGIONS)}")
    timeout = int(body.get("timeout") or 1200)
    timeout = min(max(timeout, 60), 3600)
    server_addr = str(body.get("serverAddr") or _nomad_rpc_addr_for_join(config, state)).strip()
    egress_proxy = str(body.get("egressProxy") if body.get("egressProxy") is not None else _nomad_join_egress_proxy(region, server_addr)).strip()
    payload: Dict[str, Any] = {
        "nodeName": node_name,
        "region": region,
        "serverAddr": server_addr,
    }
    if egress_proxy:
        payload["egressProxy"] = egress_proxy

    result = _run_node_agent_task(
        state,
        node_name,
        "join-nomad",
        payload,
        timeout=timeout,
        required_capability="nomad-join",
    )
    actual_node_name = str(result.get("nodeName") or node_name).strip()
    nomad_node_id = str(result.get("nodeId") or result.get("nomadNodeId") or "").strip()
    tailscale_ip = str(result.get("tailscaleIP") or "").strip()
    if not nomad_node_id:
        raise LumaError(f"Nomad join on {node_name} did not return a Nomad node ID")

    def mutate(current: Dict[str, Any]) -> Dict[str, Any]:
        require_token(current, token, token_type="deploy")
        current_nodes = current.get("nodes") if isinstance(current.get("nodes"), dict) else {}
        current_record = _node_record_for_name(current_nodes, node_name)
        if current_record is None:
            raise LumaError(f"Luma node is not registered: {node_name}")
        previous_ids = _node_record_identity_ids(current_record) - {nomad_node_id}
        for previous_id in previous_ids:
            _remember_node_agent_identity(current_record, previous_id)
        labels = labels_for_node(region, luma_name=node_name, node_id=nomad_node_id)
        values: Dict[str, Any] = {
            "region": region,
            "status": "labeled",
            "labels": labels,
            "displayName": node_name,
            "hostname": actual_node_name,
            "nodeId": nomad_node_id,
            "nomadNodeId": nomad_node_id,
            "nomadHostname": actual_node_name,
        }
        if tailscale_ip:
            values["tailscaleIP"] = tailscale_ip
        _remember_node(current, node_name, **values)
        saved = _node_record_for_name(current.get("nodes") if isinstance(current.get("nodes"), dict) else {}, node_name) or {}
        agent = saved.get("agent") if isinstance(saved.get("agent"), dict) else {}
        return {
            "agentNodeIds": sorted(_node_agent_identity_ids(saved)),
            "agentStatus": _node_agent_status(saved),
            "agentVersion": str(agent.get("version") or ""),
        }

    mutation_result = mutate_state(mutate)
    state = load_state()
    saved = _node_record_for_name(state.get("nodes") if isinstance(state.get("nodes"), dict) else {}, node_name) or {}
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "hostname": actual_node_name,
        "region": region,
        "nodeId": nomad_node_id,
        "nomadNodeId": nomad_node_id,
        "tailscaleIP": tailscale_ip,
        "agentStatus": mutation_result.get("agentStatus") or _node_agent_status(saved),
        "agentNodeIds": mutation_result.get("agentNodeIds") or sorted(_node_agent_identity_ids(saved)),
        "taskId": str(result.get("taskId") or ""),
        "message": f"Nomad node joined through node agent: {node_name}",
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
    if isinstance(removed, dict) and _node_record_is_manager(removed):
        raise LumaError(f"refusing to unregister Nomad manager node: {node_name}")
    config = load_config(_control_config_path())
    nomad_node_id = _node_record_nomad_node_id(removed) if isinstance(removed, dict) else ""
    if not nomad_node_id:
        nomad_node_id = _find_nomad_node_id_for_unregister(config, state, node_name=node_name)
    nomad_drained = False
    if nomad_node_id:
        _drain_nomad_node_for_unregister(config, state, node_name=node_name, node_id=nomad_node_id)
        nomad_drained = True
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
    removed_any = registered_removed or nomad_drained
    message = f"Node removed: {node_name}" if removed_any else f"Node not registered: {node_name}"
    return {
        "clusterId": state["clusterId"],
        "nodeName": node_name,
        "removed": removed_any,
        "registeredRemoved": registered_removed,
        "nomadDrained": nomad_drained,
        "nomadNodeId": nomad_node_id,
        "message": message,
    }


def _node_record_nomad_node_id(record: Dict[str, Any]) -> str:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return str(record.get("nomadNodeId") or record.get("nodeId") or labels.get("luma.node.id") or "").strip()


def _find_nomad_node_id_for_unregister(config: Any, state: Dict[str, Any], *, node_name: str) -> str:
    client = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    try:
        nodes = client.request("GET", "/v1/nodes")
    except LumaError:
        return ""
    if not isinstance(nodes, list):
        return ""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("ID") or "").strip()
        if not node_id:
            continue
        if _nomad_node_name_matches_unregister(node, node_name):
            return node_id
        try:
            detail = client.request("GET", f"/v1/node/{urllib.parse.quote(node_id, safe='')}")
        except LumaError:
            continue
        if isinstance(detail, dict) and _nomad_node_name_matches_unregister(detail, node_name):
            return node_id
    return ""


def _nomad_node_name_matches_unregister(node: Dict[str, Any], node_name: str) -> bool:
    meta = node.get("Meta") if isinstance(node.get("Meta"), dict) else {}
    values = {
        str(node.get("ID") or "").strip(),
        str(node.get("Name") or "").strip(),
        str(node.get("NodeName") or "").strip(),
        str(node.get("LumaNode") or node.get("lumaNode") or "").strip(),
        str(meta.get("luma_node_name") or "").strip(),
        str(meta.get("luma.node.name") or "").strip(),
    }
    return node_name in values


def _drain_nomad_node_for_unregister(config: Any, state: Dict[str, Any], *, node_name: str, node_id: str) -> None:
    client = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    client.request(
        "POST",
        f"/v1/node/{urllib.parse.quote(node_id, safe='')}/drain",
        {
            "DrainSpec": {"Deadline": 0, "IgnoreSystemJobs": True},
            "MarkEligible": False,
            "Meta": {"message": f"removed by Luma node remove: {node_name}"},
        },
    )
    client.request(
        "POST",
        f"/v1/node/{urllib.parse.quote(node_id, safe='')}/eligibility",
        {"Eligibility": "ineligible"},
    )


def _load_service_manifest(manifest: str) -> ServiceSpec:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as fh:
        fh.write(manifest)
        service_path = Path(fh.name)
    try:
        return load_service(service_path)
    finally:
        service_path.unlink(missing_ok=True)




def handle_build_deploy(
    token: str,
    body: Dict[str, Any],
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
    build_run_id: str = "",
) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    build_config = _build_config(state)
    provider_id = str(body.get("providerId") or "").strip()
    repository = str(body.get("repository") or "").strip()
    repo_url = normalize_import_repo_url(str(body.get("repoUrl") or "").strip())
    if not repo_url:
        if not provider_id or not repository:
            raise LumaError("repoUrl or providerId + repository is required")
        provider = _require_git_provider(state, provider_id)
        repo_url = _git_provider_clone_url(provider, repository)
    build_node = _require_build_node(
        state,
        str(body.get("buildNode") or build_config.get("defaultNode") or DEFAULT_BUILD_NODE_NAME).strip(),
        purpose="repository build",
    )
    ref = str(body.get("ref") or "").strip()
    steps: list[dict[str, str]] = []

    registry_host = str(body.get("registryHost") or "").strip()
    if not registry_host:
        registry_host = str(build_config.get("registryHost") or "").strip() or f"{_nomad_route_host_for_node(state, build_node)}:5000"
    repo = _image_repo_from_repo_url(repo_url)

    # git clone runs on the build node's host; cn/home nodes reach GitHub through
    # the manager egress gateway. An explicit body.proxy overrides auto-resolution.
    config = load_config(Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml"))
    proxy = str(body.get("proxy") or "").strip() or _egress_proxy_for_node(config, state, build_node)

    # gitToken and registryAuth are injected at lease time (see
    # _agent_task_lease_payload) so they are never persisted in agentTasks state.
    build_payload: Dict[str, Any] = {
        "repoUrl": repo_url,
        "ref": ref,
        "registryHost": registry_host,
        "pushHost": str(body.get("pushHost") or build_config.get("pushHost") or "localhost:5000"),
        "repo": repo,
        "proxy": proxy,
        "buildTimeout": int(body.get("buildTimeout") or 1800),
    }
    if provider_id:
        build_payload["gitProviderId"] = provider_id
    for key in ("context", "dockerfile", "platform"):
        if body.get(key):
            build_payload[key] = str(body.get(key))

    run_body = dict(body)
    run_body["repoUrl"] = repo_url
    run_body["buildNode"] = build_node
    if provider_id:
        run_body["providerId"] = provider_id
    if build_run_id:
        _restart_build_run(build_run_id, run_body, source=repo_url, build_node=build_node)
    else:
        build_run_id = _create_build_run(run_body, source=repo_url, build_node=build_node)

    def run_progress(event: dict[str, str]) -> None:
        _append_build_run_event(build_run_id, event)
        _emit_progress(progress, event)

    try:
        build_result = _deploy_step(
            steps,
            "Build image",
            lambda: _run_node_agent_task(
                state,
                build_node,
                "build-image",
                build_payload,
                timeout=int(body.get("buildTimeout") or 1800) + 120,
                required_capability="docker-build",
                progress=run_progress,
            ),
            progress=run_progress,
        )
        built_image = str(build_result.get("image") or "").strip()
        built_images = build_result.get("images") if isinstance(build_result.get("images"), dict) else {}
        if not built_image and not built_images:
            raise LumaError("build did not return an image reference")
        repo_manifest = str(build_result.get("manifest") or "").strip()
        repo_compose_content = str(build_result.get("composeContent") or "").strip()

        manifest_text = str(body.get("manifest") or "").strip() or repo_manifest
        if not manifest_text:
            raise LumaError("no Luma deployment manifest found in repository and no manifest provided")

        if str(build_result.get("kind") or "") == "compose" or repo_compose_content or body.get("composeContent"):
            compose_content = str(body.get("composeContent") or "").strip() or repo_compose_content
            if not compose_content:
                raise LumaError("luma.compose.yml found but composeContent is missing")
            ignored_overrides = [key for key in ("exposure", "domain", "port") if body.get(key)]
            if ignored_overrides:
                step = {
                    "name": "Import warning",
                    "status": "ok",
                    "message": "Compose import ignores service-level override(s): "
                    + ", ".join(ignored_overrides)
                    + ". Set them in luma.compose.yml services instead.",
                }
                steps.append(step)
                run_progress(step)

            def _inject_compose() -> tuple[str, str]:
                data = yaml.safe_load(manifest_text) or {}
                if not isinstance(data, dict):
                    raise LumaError("luma.compose.yml must contain a YAML mapping")
                if body.get("region"):
                    data["region"] = body.get("region")
                return yaml.safe_dump(data, sort_keys=False, allow_unicode=False), compose_content

            final_manifest, final_compose_content = _deploy_step(steps, "Resolve compose manifest", _inject_compose, progress=run_progress)
            deploy_body = dict(body)
            deploy_body["manifest"] = final_manifest
            deploy_body["composeContent"] = final_compose_content
            deploy_body["sourceName"] = repo_url
            deploy_body.pop("repoUrl", None)
            result = handle_compose_deployment(token, deploy_body, progress=run_progress)
            if isinstance(result, dict):
                merged_steps = steps + list(result.get("steps") or [])
                result = {**result, "steps": merged_steps, "image": built_image, "images": built_images, "buildRunId": build_run_id}
            _complete_build_run(build_run_id, "succeeded", result=result if isinstance(result, dict) else {})
            return result

        def _inject() -> str:
            data = yaml.safe_load(manifest_text) or {}
            if not isinstance(data, dict):
                raise LumaError(".luma.yml must contain a YAML mapping")
            data.pop("build", None)
            data["image"] = built_image
            for key in ("region", "exposure", "domain"):
                if body.get(key):
                    data[key] = body.get(key)
            if body.get("port"):
                data["port"] = int(body["port"])
            return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)

        final_manifest = _deploy_step(steps, "Resolve manifest", _inject, progress=run_progress)

        deploy_body = dict(body)
        deploy_body["manifest"] = final_manifest
        deploy_body["sourceName"] = repo_url
        deploy_body.pop("repoUrl", None)
        result = handle_deployment(token, deploy_body, progress=run_progress)
        if isinstance(result, dict):
            merged_steps = steps + list(result.get("steps") or [])
            result = {**result, "steps": merged_steps, "image": built_image, "buildRunId": build_run_id}
        _complete_build_run(build_run_id, "succeeded", result=result if isinstance(result, dict) else {})
        return result
    except LumaError as exc:
        _complete_build_run(build_run_id, "failed", message=str(exc))
        raise
    except Exception as exc:
        _complete_build_run(build_run_id, "failed", message=str(exc))
        raise


def handle_registry_serve(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    build_node = str(body.get("node") or body.get("buildNode") or "").strip()
    if not build_node:
        raise LumaError("node is required (the docker-build node that hosts the registry)")
    build_node = _require_build_node(state, build_node, purpose="registry serve")
    port = int(body.get("port") or 5000)
    image = str(body.get("image") or "registry:2").strip()
    name = str(body.get("name") or "luma-registry").strip()
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, build_node)
    if not record:
        names = ", ".join(sorted(str(n) for n in nodes)) or "none"
        raise LumaError(f"unknown Luma node: {build_node}. Registered nodes: {names}")
    region = str(record.get("region") or (record.get("labels") or {}).get("region") or "cn").strip() or "cn"
    registry_host = f"{_nomad_route_host_for_node(state, build_node)}:{port}"

    steps: list[dict[str, str]] = []

    manifest = {
        "name": name,
        "image": image,
        "region": region,
        "exposure": "none",
        "node": build_node,
        "port": port,
        "publishPort": port,
        "volumes": [f"{name}-data:/var/lib/registry"],
        "storage": {f"{name}-data": {"storageClass": str(body.get("storageClass") or "local")}},
    }
    manifest_text = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False)

    deploy_body = {"manifest": manifest_text, "sourceName": f"{name} (luma registry serve)"}
    deploy_result = handle_deployment(token, deploy_body, progress=progress)
    if isinstance(deploy_result, dict):
        steps.extend(s for s in (deploy_result.get("steps") or []) if isinstance(s, dict))

    # Configure insecure-registries on every ready Linux node so any of them can
    # pull from the in-cluster registry (unpinned services schedule anywhere).
    # Skip the manager: Control runs in a container there, and restarting its
    # Docker daemon would kill this very request mid-stream. The manager's daemon
    # must be configured out-of-band if it also runs pulled workloads.
    configured: list[str] = []
    skipped: list[str] = []
    registry_no_proxy = _merge_no_proxy(EGRESS_NO_PROXY, *_no_proxy_entries_for_registry(registry_host))
    config = load_config(Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml"))
    for node_name in sorted(str(n) for n in nodes):
        node_record = nodes.get(node_name)
        if not isinstance(node_record, dict):
            continue
        agent = node_record.get("agent") if isinstance(node_record.get("agent"), dict) else {}
        if _node_record_is_manager(node_record):
            skipped.append(f"{node_name} (manager: configure docker daemon out-of-band)")
            continue
        if str(agent.get("os") or "") != "linux" or not _node_agent_is_ready(node_record):
            skipped.append(node_name)
            continue
        try:
            _run_node_agent_task(
                state,
                node_name,
                "configure-insecure-registry",
                {"registry": registry_host},
                timeout=240,
                required_capability=None,
            )
            daemon_proxy = _docker_daemon_proxy_for_node(config, state, node_name, node_record)
            if daemon_proxy:
                _run_node_agent_task(
                    state,
                    node_name,
                    "configure-docker-egress-proxy",
                    {"proxy": daemon_proxy, "noProxy": registry_no_proxy},
                    timeout=240,
                    required_capability=None,
                )
            configured.append(node_name)
        except LumaError as exc:
            skipped.append(f"{node_name} ({exc})")
    insecure_step = {
        "name": "Configure insecure-registries",
        "status": "ok",
        "message": f"configured: {', '.join(configured) or 'none'}; skipped: {', '.join(skipped) or 'none'}",
    }
    steps.append(insecure_step)
    _emit_progress(progress, insecure_step)

    return {
        "service": name,
        "registryHost": registry_host,
        "pushHost": f"localhost:{port}",
        "configuredNodes": configured,
        "skippedNodes": skipped,
        "steps": steps,
    }


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


def _skip_orchestrator(body: Dict[str, Any]) -> bool:
    return bool(body.get("skipOrchestrator"))


def _require_nomad_engine(engine: str) -> str:
    value = str(engine or "nomad").strip() or "nomad"
    if value != "nomad":
        raise LumaError("Nomad is the only supported deployment engine")
    return value


def _compose_tcp_relay_ports(deployment: ComposeDeploymentSpec) -> list[int]:
    return [
        int(service.publish_port or service.port or 0)
        for service in deployment.services.values()
        if service.exposure == "tcp-relay" and int(service.publish_port or service.port or 0) > 0
    ]


def _ensure_compose_exposure_supported_on_nodes(state: Dict[str, Any], deployment: ComposeDeploymentSpec) -> None:
    """Reject a compose service pinned to a Mac node with a bridge-port exposure.

    render_compose_job always maps exposed ports via a Nomad bridge ReservedPort
    (nomad_render.py); it has no docker host-mode path (compose tasks share one
    netns, which conflicts with host mode). On macOS/OrbStack that bridge mapping
    binds a Mac host NIC IP absent inside the OrbStack VM, so the port silently
    502s. Native manifests get host mode on Mac; compose does not, so fail fast
    and point at the working alternatives instead of deploying an unreachable job.
    """
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    bridge_exposures = {"tcp-relay", "tailscale-relay", "cn-edge", "external-edge"}
    # A compose deployment renders as ONE Nomad group constrained to a single
    # node (render_compose_job: the union of every service's node pin, capped at
    # one distinct node). The exposed service need not be the one carrying the
    # pin — a sibling (e.g. a DB needing a local volume) can pin the whole group
    # to a Mac node while the exposed service declares no node of its own. So the
    # guard keys on the group's resolved node, not each service's own .node.
    exposed = [
        service
        for service in deployment.services.values()
        if service.exposure in bridge_exposures and service.port
    ]
    if not exposed:
        return
    pinned_nodes = {service.node for service in deployment.services.values() if service.node}
    for node_name in pinned_nodes:
        record = _node_record_for_name(nodes, node_name)
        if not record:
            continue
        platform = _nomad_node_platform_from_record(record)
        if (platform or "").split("/")[0] == "darwin":
            culprit = exposed[0]
            raise LumaError(
                f"compose service {culprit.name} exposure={culprit.exposure} would run on "
                f"Mac/OrbStack node {node_name} (the deployment group is pinned there), but "
                "compose port mapping uses Nomad bridge mode which is unreachable on macOS. "
                "Deploy this group on a Linux node, or use a native luma manifest (which "
                "renders docker host mode on Mac)."
            )


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


def _tcp_relay_ports_needing_ingress_refresh(state: Dict[str, Any], ports: list[int]) -> list[int]:
    """Return tcp-relay ports this deploy introduces that no active deployment
    already publishes.

    Traefik TCP entrypoints (and the host firewall) are static: they are only
    (re)built from Control state by bootstrap / `luma update manager`, never by a
    plain deploy (file-provider can hot-reload routers/services but NOT
    entrypoints). So a deploy that introduces a brand-new tcp-relay port writes a
    route referencing an entrypoint Traefik does not have — the job comes up but
    the port is unreachable with no error. Surface that as an advisory so the user
    knows to run `luma update manager`; a port already served by another active
    deployment needs no refresh.
    """
    wanted = {int(port) for port in ports if int(port) > 0}
    if not wanted:
        return []
    deployments = _deployments_state(state)
    # The set of tcp-relay ports Traefik's static entrypoints were last built from
    # is every ACTIVE deployment's ports (see bootstrap._state_tcp_relay_ports) —
    # including this slug's own existing active record, so redeploying an
    # already-active service on an unchanged port does NOT warn (Traefik already
    # has that entrypoint).
    already_served: set[int] = set()
    for bucket in ("services", "compose"):
        for record in deployments[bucket].values():
            if not isinstance(record, dict) or str(record.get("status") or "") != "active":
                continue
            already_served.update(_record_tcp_relay_ports(record))
    return sorted(wanted - already_served)


def _emit_tcp_ingress_refresh_advisory(
    steps: list[dict[str, str]],
    progress: Callable[[dict[str, str]], None] | None,
    state: Dict[str, Any],
    ports: list[int],
) -> None:
    """Warn (non-fatally) when a deploy introduces tcp-relay ports Traefik has no
    static entrypoint for yet. The job will run but the port stays unreachable
    until `luma update manager` rebuilds Traefik entrypoints + firewall from state.
    """
    new_ports = _tcp_relay_ports_needing_ingress_refresh(state, ports)
    if not new_ports:
        return
    port_list = ", ".join(str(port) for port in new_ports)
    step = {
        "name": "TCP ingress refresh required",
        "status": "ok",
        "message": (
            f"new tcp-relay port(s) {port_list} need a Traefik entrypoint + firewall opening; "
            "run `luma update manager` on the manager or the port stays unreachable"
        ),
    }
    steps.append(step)
    _emit_progress(progress, step)


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
        "storageBackends": _compose_storage_backend_signatures(deployment),
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
        # storageBackends is the baseline the storage-switch guard compares
        # against, and it must reflect only what was ACTUALLY APPLIED. Capture
        # the prior applied signatures BEFORE re-registering (which would
        # overwrite them with the new — possibly rejected — manifest's
        # signatures). Only a successful "active" mark means the new backend was
        # really deployed; for pending/failed_partial we restore the prior
        # baseline, otherwise a rejected storage switch poisons the baseline and
        # a retry slips past the guard onto a different backend, orphaning the
        # old volume's data.
        prior = _deployments_state(state)["compose"].get(deployment.slug)
        prior_backends = _compose_storage_backend_signatures_from_record(prior) if isinstance(prior, dict) else None
        _register_compose_deployment(state, deployment, body, source_name)
        record = _deployments_state(state)["compose"][deployment.slug]
        if status != "active":
            if prior_backends is not None:
                record["storageBackends"] = prior_backends
            else:
                record.pop("storageBackends", None)
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


# Serializes the state-touching deploy handlers. Each does a read-modify-write
# of control.json (load_state snapshot -> checks -> render -> mutate_state); two
# concurrent deploys could otherwise interleave on slug-availability checks and
# scopedSecrets writes (TOCTOU). RLock so a deploy that nests another (none do
# today) cannot self-deadlock. Read-only paths (preview/dry-run) are NOT wrapped
# and stay concurrent. Long pre-state work (e.g. docker build in
# handle_build_deploy) runs OUTSIDE this lock — only the handler below it holds
# it — so the build of one deploy does not block an unrelated deploy.
_DEPLOY_LOCK = threading.RLock()


def _serialize_deploy(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _DEPLOY_LOCK:
            return func(*args, **kwargs)

    return wrapper


@_serialize_deploy
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
    effective_engine = _require_nomad_engine(service.engine or str(body.get("engine") or config.defaults.get("engine") or "nomad"))
    _require_nomad_engine(effective_engine)
    _ensure_deployment_slug_available(state, "service", service.slug, service.name)
    parse_step = {"name": "Parse manifest", "status": "ok", "message": f"{service.name} -> {service.region}/{service.exposure}"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    # cloudflared's tunnel token is referenced via the plain manifest field
    # tunnel.tokenEnv (not a ${...} placeholder), so _render_secrets cannot see
    # it in the manifest text. Pass it explicitly or the scoped/--env paths drop
    # it and the deploy fails with "missing deployment secret".
    extra_secret_names: set[str] = set()
    if service.exposure == "cloudflare-tunnel":
        extra_secret_names.add(str(service.tunnel.get("tokenEnv", "CLOUDFLARE_TUNNEL_TOKEN")))
    secrets, secret_result = _render_secrets(state, scope=service.slug, body=body, texts=[manifest], extra_referenced=extra_secret_names)
    if secret_result["scoped"] or body.get("envSecrets") is not None:
        secret_step = {
            "name": "Load scoped env",
            "status": "ok",
            "message": f"{secret_result['scope']}: imported {secret_result['imported']} of {len(secret_result['referenced'])} referenced secret(s)",
        }
        steps.append(secret_step)
        _emit_progress(progress, secret_step)
    _mark_service_deployment(service, manifest, source_name, status="pending", steps=steps)

    try:
        service = _deploy_step(steps, "Resolve node pin", lambda: resolve_service_node_pin(service, state, engine=effective_engine), progress=progress)
        registry_auth = _registry_auth_for_service(state, service)
        service, image_result = _deploy_step(
            steps,
            "Resolve image",
            lambda: resolve_service_image(config, service, registry_auth=registry_auth, state=state),
            progress=progress,
        )
        _deploy_step(
            steps,
            "Check TCP relay ports",
            lambda: _ensure_tcp_relay_ports_available(state, kind="service", slug=service.slug, ports=_service_tcp_relay_ports(service)) or "TCP relay ports available",
            progress=progress,
        )
        _emit_tcp_ingress_refresh_advisory(steps, progress, state, _service_tcp_relay_ports(service))
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
            "Render Nomad job",
            lambda: render_nomad_job(config, service, registry_auth=registry_auth, secrets=secrets, egress_proxy_url=_egress_proxy_for_region(config, state, service.region)),
            progress=progress,
        )
        _deploy_step(steps, "Write Nomad job", lambda: target.write_text(stack_text, encoding="utf-8"), progress=progress)
        written = [str(target)]
        dns_result = _deploy_step(steps, "Sync DNS", lambda: "DNS skipped: --skip-dns" if body.get("skipDns") else sync_dns(config, service), progress=progress)
        orchestrator_result = _deploy_step(
            steps,
            "Deploy Nomad job",
            lambda: "Orchestrator deploy skipped"
            if _skip_orchestrator(body)
            else deploy_to_nomad(config, stack_text, state, slug=service.slug),
            progress=progress,
        )
        cni_hostports = (
            {}
            if _skip_orchestrator(body)
            else _deploy_step(
                steps,
                "Refresh Nomad CNI hostports",
                lambda: _refresh_nomad_cni_hostports_for_job(
                    config,
                    state,
                    service.slug,
                    fallback_nodes=_service_cni_fallback_nodes(service),
                    ports=_service_cni_host_ports(service),
                ),
                progress=progress,
            )
        )
        if service.exposure in {"tailscale-relay", "tcp-relay"}:
            route_target = _resolve_control_path(route_path(config, service), config_path)
            route_target.parent.mkdir(parents=True, exist_ok=True)
            route_service = service
            relay_is_explicit = bool(service.relay.get("url") or service.relay.get("host")) if service.exposure == "tailscale-relay" else bool(service.tcp.get("address") or service.tcp.get("host"))
            if _skip_orchestrator(body) and not relay_is_explicit:
                _deploy_step(steps, "Write route", lambda: f"Route skipped: orchestrator deploy is required to infer {service.exposure}", progress=progress)
            else:
                route_service = _deploy_step(
                    steps,
                    "Resolve relay",
                    lambda: resolve_nomad_static_route_target(service, state),
                    progress=progress,
                )
                route_text = render_tailscale_route(config, route_service) if service.exposure == "tailscale-relay" else render_tcp_route(config, route_service)
                _deploy_step(steps, "Write route", lambda: route_target.write_text(route_text, encoding="utf-8"), progress=progress)
                written.append(str(route_target))
        probe_result = _deploy_step(
            steps,
            "Probe public route",
            lambda: "Public route probe skipped: orchestrator deploy skipped" if _skip_orchestrator(body) else _probe_public_route(service),
            progress=progress,
        )
    except Exception as exc:
        # Any failure (LumaError, OSError from a full/read-only disk, raw socket
        # errors from the Nomad/DNS calls) must drive the record to a terminal
        # state. Leaving it at "pending" strands a ghost deploy that also blocks
        # later deploys (pending counts as occupying tcp-relay ports). bare raise
        # preserves the original exception for the caller.
        _mark_service_deployment(service, manifest, source_name, status="failed_partial", steps=steps, error=str(exc))
        raise
    _mark_service_deployment(service, manifest, source_name, status="active", steps=steps)
    result = {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "written": written,
        "image": image_result,
        "dns": dns_result,
        "orchestrator": orchestrator_result,
        "probe": probe_result,
        "cniHostports": cni_hostports,
        "storagePreparation": storage_preparation,
        "steps": steps,
    }
    return result


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
    effective_engine = _require_nomad_engine(service.engine or str(body.get("engine") or config.defaults.get("engine") or "nomad"))
    _require_nomad_engine(effective_engine)
    service = resolve_service_node_pin(service, state, engine=effective_engine)
    stack_text = render_nomad_job(
        config,
        service,
        registry_auth=_registry_auth_for_service(state, service),
        resolve_secrets=False,
    )
    artifacts = [
        {
            "kind": "job",
            "path": str(stack_path(config, service)),
            "content": stack_text,
        }
    ]
    if service.exposure in {"tailscale-relay", "tcp-relay"}:
        route_service = resolve_nomad_static_route_target(service, state)
        artifacts.append(
            {
                "kind": "route",
                "path": str(route_path(config, service)),
                "content": render_tailscale_route(config, route_service) if service.exposure == "tailscale-relay" else render_tcp_route(config, route_service),
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
            "secrets": [],
        },
        "artifacts": artifacts,
        "warnings": [],
    }


def handle_service_remove(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    name = _remove_request_name(body)
    if body.get("deleteStorage") and _skip_orchestrator(body):
        raise LumaError("--delete-storage cannot be combined with skipping the orchestrator")
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
    raise LumaError(f"deployment not found: {name}")


def handle_service_rollback(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Roll a Nomad-engine service back to a prior version (new capability)."""
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LumaError("name is required")
    version = body.get("version")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise LumaError("version must be an integer") from exc
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    slug = slugify(name)
    message = revert_job(config, state, slug=slug, version=version)
    return {"clusterId": state["clusterId"], "service": name, "slug": slug, "message": message}


def handle_service_history(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Return a Nomad-engine service's version history (for `luma history`)."""
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LumaError("name is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    slug = slugify(name)
    versions = job_versions(config, state, slug=slug)
    return {"clusterId": state["clusterId"], "service": name, "slug": slug, "versions": versions}


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
    _require_nomad_engine(service.engine or str(config.defaults.get("engine") or "nomad"))
    stack_target = _generated_stack_remove_target(config, service, config_path)
    route_target = _resolve_control_path(route_path(config, service), config_path) if service.exposure in {"tailscale-relay", "tcp-relay"} else None
    files = [str(stack_target)]
    if route_target:
        files.append(str(route_target))
    storage_task_nodes = _service_volume_cleanup_nodes(service, state) if body.get("deleteStorage") and _service_docker_volume_names(service) and not dry_run else []

    dns_result = _deploy_step(
        steps,
        "Delete DNS",
        lambda: "DNS skipped: --skip-dns"
        if body.get("skipDns")
        else (_planned_delete_dns_message(service) if dry_run else delete_dns(config, service)),
        progress=progress,
    )
    orchestrator_result = _deploy_step(
        steps,
        "Remove Nomad job",
        lambda: "Orchestrator remove skipped"
        if _skip_orchestrator(body)
        else (
            _planned_remove_message("Nomad job would be removed", service.slug)
            if dry_run
            else remove_from_nomad(config, state, slug=service.slug)
        ),
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
    if not dry_run and not _skip_orchestrator(body):
        def forget(state: Dict[str, Any]) -> None:
            _forget_service_deployment(state, service)

        mutate_state(forget)
    result = {
        "clusterId": state["clusterId"],
        "service": service.name,
        "sourceName": source_name,
        "files": files,
        "dns": dns_result,
        "orchestrator": orchestrator_result,
        "generatedFiles": files_result,
        "storageCleanup": storage_cleanup,
        "dryRun": dry_run,
        "steps": steps,
    }
    return result


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
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
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
    orchestrator_result = _deploy_step(
        steps,
        "Remove Nomad job",
        lambda: "Orchestrator remove skipped"
        if _skip_orchestrator(body)
        else (
            _planned_remove_message("Nomad job would be removed", deployment.slug)
            if dry_run
            else remove_from_nomad(config, state, slug=deployment.slug)
        ),
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
    if not dry_run and not _skip_orchestrator(body):
        def forget(state: Dict[str, Any]) -> None:
            _forget_compose_deployment(state, deployment)

        mutate_state(forget)
    result = {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "files": files,
        "dns": dns_results,
        "orchestrator": orchestrator_result,
        "generatedFiles": files_result,
        "storageCleanup": storage_cleanup,
        "dryRun": dry_run,
        "steps": steps,
    }
    return result


SYSTEM_STACKS = {"traefik", "egress", "luma-control"}


def handle_application_restart(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    stack = str(body.get("stack") or "").strip()
    service_name = str(body.get("service") or "").strip()
    mode = _application_restart_mode(body.get("mode"), service_name=service_name)
    if not stack:
        raise LumaError("stack is required")
    if _is_system_stack(stack):
        raise LumaError(f"system stack cannot be restarted from application management: {stack}")
    config = load_config(_control_config_path())
    api = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    allocations = api.request("GET", f"/v1/job/{urllib.parse.quote(stack, safe='')}/allocations")
    if not isinstance(allocations, list):
        raise LumaError(f"Nomad returned invalid allocations for job: {stack}")
    restarted = []
    recreated_node_names: set[str] = set()
    recreated_host_ports: set[int] = set()
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        client_status = str(alloc.get("ClientStatus") or alloc.get("client_status") or "").lower()
        if client_status and client_status != "running":
            continue
        alloc_id = str(alloc.get("ID") or alloc.get("ID") or "").strip()
        if not alloc_id:
            continue
        task_states = alloc.get("TaskStates") if isinstance(alloc.get("TaskStates"), dict) else {}
        if mode == "recreate":
            if service_name and service_name not in task_states:
                continue
            api.request("POST", f"/v1/allocation/{urllib.parse.quote(alloc_id, safe='')}/stop", None)
            restarted.append({"allocId": alloc_id, "task": service_name or "*", "mode": mode})
            node_name = _luma_node_name_for_nomad_allocation(state, alloc)
            if node_name:
                recreated_node_names.add(node_name)
            recreated_host_ports.update(_nomad_allocation_host_ports(alloc))
            continue
        task_names = [service_name] if service_name else [str(name) for name in task_states] or [""]
        for task_name in task_names:
            payload = {"TaskName": task_name} if task_name else {}
            api.request("POST", f"/v1/client/allocation/{urllib.parse.quote(alloc_id, safe='')}/restart", payload)
            restarted.append({"allocId": alloc_id, "task": task_name or "*", "mode": mode})
    if not restarted:
        suffix = f"/{service_name}" if service_name else ""
        raise LumaError(f"application allocation not found: {stack}{suffix}")
    result = {
        "clusterId": state["clusterId"],
        "stack": stack,
        "service": service_name,
        "mode": mode,
        "restarted": restarted,
    }
    cni_hostports = _refresh_nomad_cni_hostports_for_nodes(state, recreated_node_names, ports=sorted(recreated_host_ports))
    if recreated_node_names or cni_hostports.get("results") or cni_hostports.get("skipped"):
        result["cniHostports"] = cni_hostports
    return result


def _application_restart_mode(value: Any, *, service_name: str = "") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "task" if service_name else "recreate"
    if raw in {"recreate", "reschedule", "replace", "allocation", "alloc"}:
        return "recreate"
    if raw in {"task", "restart", "in-place", "inplace"}:
        return "task"
    raise LumaError("restart mode must be one of: recreate, task")


def _luma_node_name_for_nomad_allocation(state: Dict[str, Any], alloc: Dict[str, Any]) -> str:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    node_id = str(alloc.get("NodeID") or alloc.get("node_id") or "").strip()
    if node_id:
        for name, record in nodes.items():
            if isinstance(record, dict) and node_id in _node_record_identity_ids(record):
                return str(name)
    node_name = str(alloc.get("NodeName") or alloc.get("node_name") or "").strip()
    if node_name:
        entry = _node_record_entry_for_name_or_id(nodes, node_name)
        if entry:
            return entry[0]
    return ""


def _service_cni_fallback_nodes(service: ServiceSpec) -> list[str]:
    if not service.node or not service.port:
        return []
    if service.exposure in {"tailscale-relay", "tcp-relay"} and not service.publish_port:
        return []
    return [service.node]


def _service_cni_host_ports(service: ServiceSpec) -> list[int]:
    if service.publish_port:
        return [int(service.publish_port)]
    return []


def _compose_cni_fallback_nodes(deployment: ComposeDeploymentSpec) -> list[str]:
    exposed = [
        service
        for service in deployment.services.values()
        if service.port and service.exposure in {"tailscale-relay", "tcp-relay", "cn-edge", "external-edge"}
    ]
    if not exposed:
        return []
    return sorted({str(service.node) for service in deployment.services.values() if service.node})


def _compose_cni_host_ports(deployment: ComposeDeploymentSpec) -> list[int]:
    ports: set[int] = set()
    for service in deployment.services.values():
        if not service.port:
            continue
        if service.publish_port:
            ports.add(int(service.publish_port))
            continue
        if service.exposure in {"tailscale-relay", "tcp-relay"}:
            ports.add(int(service.port))
    return sorted(ports)


def _refresh_nomad_cni_hostports_for_job(
    config: LumaConfig,
    state: Dict[str, Any],
    job_id: str,
    *,
    fallback_nodes: list[str] | None = None,
    ports: list[int] | None = None,
) -> Dict[str, Any]:
    node_names = {str(node).strip() for node in (fallback_nodes or []) if str(node).strip()}
    host_ports = {int(port) for port in (ports or []) if int(port) > 0}
    lookup_error = ""
    if job_id:
        try:
            allocations = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or "")).request(
                "GET",
                f"/v1/job/{urllib.parse.quote(job_id, safe='')}/allocations",
            )
            if isinstance(allocations, list):
                for allocation in allocations:
                    if not _nomad_allocation_is_current(allocation):
                        continue
                    node_name = _luma_node_name_for_nomad_allocation(state, allocation)
                    if node_name:
                        node_names.add(node_name)
                    host_ports.update(_nomad_allocation_host_ports(allocation))
            else:
                lookup_error = f"Nomad returned invalid allocations for job: {job_id}"
        except Exception as exc:
            lookup_error = str(exc)
    result = _refresh_nomad_cni_hostports_for_nodes(state, node_names, ports=sorted(host_ports))
    if lookup_error:
        result["lookupError"] = lookup_error
        if not node_names:
            result.setdefault("skipped", []).append({"node": "", "reason": lookup_error})
    return result


def _nomad_allocation_is_current(allocation: Any) -> bool:
    if not isinstance(allocation, dict):
        return False
    desired_status = str(allocation.get("DesiredStatus") or allocation.get("desired_status") or "run").lower()
    if desired_status and desired_status not in {"run", "running"}:
        return False
    client_status = str(allocation.get("ClientStatus") or allocation.get("client_status") or "").lower()
    return not client_status or client_status in {"running", "pending"}


def _nomad_allocation_host_ports(allocation: Dict[str, Any]) -> set[int]:
    ports: set[int] = set()
    allocated = allocation.get("AllocatedResources") if isinstance(allocation.get("AllocatedResources"), dict) else {}
    shared = allocated.get("Shared") if isinstance(allocated.get("Shared"), dict) else {}
    ports.update(_nomad_port_values(shared.get("Ports")))
    tasks = allocated.get("Tasks") if isinstance(allocated.get("Tasks"), dict) else {}
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        networks = task.get("Networks") if isinstance(task.get("Networks"), list) else []
        for network in networks:
            if isinstance(network, dict):
                ports.update(_nomad_port_values(network.get("ReservedPorts")))
                ports.update(_nomad_port_values(network.get("DynamicPorts")))
                ports.update(_nomad_port_values(network.get("Ports")))
    resources = allocation.get("Resources") if isinstance(allocation.get("Resources"), dict) else {}
    networks = resources.get("Networks") if isinstance(resources.get("Networks"), list) else []
    for network in networks:
        if isinstance(network, dict):
            ports.update(_nomad_port_values(network.get("ReservedPorts")))
            ports.update(_nomad_port_values(network.get("DynamicPorts")))
            ports.update(_nomad_port_values(network.get("Ports")))
    return ports


def _nomad_port_values(raw_ports: Any) -> set[int]:
    ports: set[int] = set()
    if not isinstance(raw_ports, list):
        return ports
    for item in raw_ports:
        if not isinstance(item, dict):
            continue
        raw = item.get("Value") or item.get("HostPort") or item.get("host_port") or item.get("port")
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if port > 0:
            ports.add(port)
    return ports


def _refresh_nomad_cni_hostports_for_nodes(state: Dict[str, Any], node_names: set[str] | list[str], *, ports: list[int] | None = None) -> Dict[str, Any]:
    results: list[Dict[str, Any]] = []
    skipped: list[Dict[str, str]] = []
    host_ports = sorted({int(port) for port in (ports or []) if int(port) > 0})
    normalized_nodes = sorted({str(node).strip() for node in node_names if str(node).strip()})
    if not host_ports:
        return {
            "nodes": [],
            "results": [],
            "skipped": [{"node": node_name, "reason": "no Nomad CNI host ports discovered for this job"} for node_name in normalized_nodes],
            "hostPorts": [],
        }
    for node_name in normalized_nodes:
        if not _node_agent_has_capability(state, node_name, "nomad-cni-repair"):
            skipped.append({"node": node_name, "reason": "node agent is not ready for nomad-cni-repair"})
            continue
        try:
            task_id = _queue_node_agent_task(
                state,
                node_name,
                "repair-nomad-cni-hostports",
                {"ports": host_ports},
                required_capability="nomad-cni-repair",
            )
            repair = _wait_node_agent_task(task_id, node_name, "repair-nomad-cni-hostports", timeout=120)
            item = {"node": node_name}
            item.update(repair)
            results.append(item)
        except LumaError as exc:
            skipped.append({"node": node_name, "reason": str(exc)})
    return {"nodes": [str(item.get("node") or "") for item in results if item.get("node")], "results": results, "skipped": skipped, "hostPorts": host_ports}


def handle_certificate_retry(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    domain = str(body.get("domain") or "").strip().lower()
    route_id = str(body.get("routeId") or "").strip()
    if not domain:
        raise LumaError("domain is required")

    config_path = _control_config_path()
    config = load_config(config_path)
    routes_root = _resolve_control_path(config.routes_root, config_path)
    if not routes_root.exists():
        raise LumaError("route files directory not found")
    candidates: list[tuple[Dict[str, Any], Path]] = []
    for path in sorted([*routes_root.glob("*.yml"), *routes_root.glob("*.yaml")]):
        route = _dashboard_route_file(path.stem, load_yaml(path))
        if (
            route
            and route.get("kind") == "http"
            and str(route.get("domain") or "").strip().lower() == domain
            and (not route_id or str(route.get("id") or "") == route_id)
        ):
            candidates.append((route, path))
    if not candidates:
        suffix = f" routeId={route_id}" if route_id else ""
        raise LumaError(f"HTTP route file not found for domain: {domain}{suffix}")
    if len(candidates) > 1 and not route_id:
        raise LumaError(f"multiple route files match domain {domain}; routeId is required")

    route, path = candidates[0]
    if not path.exists() or not path.is_file():
        raise LumaError(f"route file unavailable for domain: {domain}")
    cert_resolver = str(route.get("certResolver") or "").strip()
    if not cert_resolver:
        raise LumaError(f"route has no TLS certResolver: {domain}")

    original = path.read_text(encoding="utf-8")
    tmp = path.with_name(f".{path.name}.retry.tmp")
    tmp.write_text(original, encoding="utf-8")
    try:
        shutil.copymode(path, tmp)
    except OSError:
        pass
    tmp.replace(path)
    os.utime(path, None)
    return {
        "clusterId": state["clusterId"],
        "domain": domain,
        "routeId": str(route.get("id") or ""),
        "mode": "route-file-reload",
        "certResolver": cert_resolver,
        "path": str(path),
        "message": f"Route file reloaded for {domain}; Traefik will retry ACME if needed.",
    }


def handle_fleet_update(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    install_ref = str(body.get("installRef") or "").strip()
    include_all = bool(body.get("includeAll"))
    include_manager = bool(body.get("includeManager"))
    per_node_timeout = int(body.get("timeout") or 900)
    per_node_timeout = min(max(per_node_timeout, 60), 3600)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    results: list[Dict[str, Any]] = []
    for node_name in sorted(str(name) for name in nodes):
        record = nodes.get(node_name)
        if not isinstance(record, dict):
            continue
        agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
        if not include_all and not _node_agent_is_ready(record):
            continue
        item: Dict[str, Any] = {
            "nodeName": node_name,
            "region": str(record.get("region") or ""),
            "os": str(agent.get("os") or ""),
            "status": "pending",
        }
        if _node_record_is_manager(record) and not include_manager:
            item["status"] = "skipped"
            item["message"] = "manager node is skipped by fleet update; run luma update manager on the manager"
            results.append(item)
            continue
        agent_status = _node_agent_status(record)
        capabilities = {str(value) for value in agent.get("capabilities") or []}
        if agent_status != "ready":
            item["status"] = "skipped"
            item["message"] = f"node agent is not ready; status={agent_status}"
            results.append(item)
            continue
        if "luma-update" not in capabilities:
            item["status"] = "skipped"
            item["message"] = "node agent does not support fleet update; run luma update on this node once to refresh the agent"
            results.append(item)
            continue
        try:
            result = _run_node_agent_task(
                state,
                node_name,
                "update-luma",
                {"installRef": install_ref},
                timeout=per_node_timeout,
                required_capability="luma-update",
            )
            item["status"] = "succeeded"
            item["message"] = str(result.get("message") or "Luma installer finished")
            item["taskId"] = str(result.get("taskId") or "")
            if result.get("installRef"):
                item["installRef"] = str(result.get("installRef"))
            if result.get("output"):
                item["output"] = str(result.get("output"))
        except LumaError as exc:
            item["status"] = "failed"
            item["message"] = str(exc)
        results.append(item)
    succeeded = sum(1 for item in results if item.get("status") == "succeeded")
    failed = sum(1 for item in results if item.get("status") == "failed")
    skipped = sum(1 for item in results if item.get("status") == "skipped")
    return {
        "clusterId": state["clusterId"],
        "installRef": install_ref,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


def _is_system_stack(stack: str) -> bool:
    return stack in SYSTEM_STACKS or stack.startswith("luma-storage")


@_serialize_deploy
def handle_compose_deployment(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    steps: list[dict[str, str]] = []
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    previous_record = dict(_compose_deployment_record(state, deployment.slug) or {})
    _ensure_deployment_slug_available(state, "compose", deployment.slug, deployment.name)
    _ensure_tcp_relay_ports_available(state, kind="compose", slug=deployment.slug, ports=_compose_tcp_relay_ports(deployment))
    _ensure_compose_exposure_supported_on_nodes(state, deployment)
    parse_step = {"name": "Parse compose deployment", "status": "ok", "message": f"{deployment.name} ({len(deployment.compose.get('services', {}))} services)"}
    steps.append(parse_step)
    _emit_progress(progress, parse_step)
    _emit_tcp_ingress_refresh_advisory(steps, progress, state, _compose_tcp_relay_ports(deployment))
    compose_content = str(body.get("composeContent") or "")
    secrets, secret_result = _render_secrets(state, scope=deployment.slug, body=body, texts=[str(body.get("manifest") or ""), compose_content])
    if secret_result["scoped"] or body.get("envSecrets") is not None:
        secret_step = {
            "name": "Load scoped env",
            "status": "ok",
            "message": f"{secret_result['scope']}: imported {secret_result['imported']} of {len(secret_result['referenced'])} referenced secret(s)",
        }
        steps.append(secret_step)
        _emit_progress(progress, secret_step)
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
        _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
        stack_text = _deploy_step(
            steps,
            "Render compose Nomad job",
            lambda: render_compose_job(
                config,
                deployment,
                registry_auth_resolver=lambda image: _registry_auth_for_image(state, image),
                secrets=secrets,
                egress_proxy_url=_egress_proxy_for_region(config, state, deployment.region),
            ),
            progress=progress,
        )
        _deploy_step(
            steps,
            "Check storage backend",
            lambda: _guard_compose_storage_switch(target, stack_text, deployment, previous_record=previous_record),
            progress=progress,
        )
        _deploy_step(steps, "Write compose job", lambda: target.write_text(stack_text, encoding="utf-8"), progress=progress)
        written = [str(target)]

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

        orchestrator_result = _deploy_step(
            steps,
            "Deploy compose Nomad job",
            lambda: "Orchestrator deploy skipped"
            if _skip_orchestrator(body)
            else deploy_to_nomad(config, stack_text, state, slug=deployment.slug),
            progress=progress,
        )
        cni_hostports = (
            {}
            if _skip_orchestrator(body)
            else _deploy_step(
                steps,
                "Refresh Nomad CNI hostports",
                lambda: _refresh_nomad_cni_hostports_for_job(
                    config,
                    state,
                    deployment.slug,
                    fallback_nodes=_compose_cni_fallback_nodes(deployment),
                    ports=_compose_cni_host_ports(deployment),
                ),
                progress=progress,
            )
        )

        for service_name, service in deployment.services.items():
            if service.exposure not in {"tailscale-relay", "tcp-relay"} or not service.domain or not service.port:
                continue
            route_target = _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
            route_target.parent.mkdir(parents=True, exist_ok=True)
            service_spec = _compose_service_as_service_spec(deployment, service)
            relay_is_explicit = bool(service.relay.get("url") or service.relay.get("host")) if service.exposure == "tailscale-relay" else bool(service.tcp.get("address") or service.tcp.get("host"))
            if _skip_orchestrator(body) and not relay_is_explicit:
                _deploy_step(
                    steps,
                    f"Write route {service_name}",
                    lambda service=service: f"Route skipped: orchestrator deploy is required to infer {service.exposure}",
                    progress=progress,
                )
                continue
            if not _skip_orchestrator(body):
                service_spec = _deploy_step(
                    steps,
                    f"Resolve relay {service.name}",
                    lambda service_spec=service_spec: resolve_nomad_static_route_target(service_spec, state, prefer_publish_port=True),
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
                    lambda service_spec=service_spec: "Public route probe skipped: orchestrator deploy skipped" if _skip_orchestrator(body) else _probe_public_route(service_spec),
                    progress=progress,
                )
            )
    except Exception as exc:
        # See handle_deployment: any failure (not just LumaError — OSError, raw
        # socket errors) must reach a terminal state so the record never strands
        # at "pending" and blocks later deploys.
        _mark_compose_deployment(deployment, body, source_name, status="failed_partial", steps=steps, error=str(exc))
        raise
    _mark_compose_deployment(deployment, body, source_name, status="active", steps=steps)
    result = {
        "clusterId": state["clusterId"],
        "deployment": deployment.name,
        "sourceName": source_name,
        "written": written,
        "dns": dns_results,
        "orchestrator": orchestrator_result,
        "probe": probe_results,
        "cniHostports": cni_hostports,
        "storagePreparation": storage_preparation,
        "storage": storage_summary(deployment, node_records=_state_nodes(state)),
        "steps": steps,
    }
    return result


def handle_compose_deployment_preview(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    source_name = str(body.get("sourceName") or "luma.compose.yml")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    deployment = _load_compose_request(body, source_name)
    target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    _ensure_compose_exposure_supported_on_nodes(state, deployment)
    stack_text = render_compose_job(
        config,
        deployment,
        registry_auth_resolver=lambda image: _registry_auth_for_image(state, image),
        resolve_secrets=False,
    )
    storage_guard = "skipped: nomad job preview"
    route_texts: Dict[str, str] = {}
    for service_name, service in deployment.services.items():
        if service.exposure not in {"tailscale-relay", "tcp-relay"} or not service.domain or not service.port:
            continue
        service_spec = resolve_nomad_static_route_target(
            _compose_service_as_service_spec(deployment, service),
            state,
            prefer_publish_port=True,
        )
        route_texts[service_name] = (
            render_tailscale_route(config, service_spec)
            if service.exposure == "tailscale-relay"
            else render_tcp_route(config, service_spec)
        )
    artifacts = [
        {
            "kind": "job",
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
        lambda: render_compose_job(config, deployment, resolve_secrets=False),
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
    local_values = {
        str(info.get("Name") or ""),
        str(info.get("ID") or ""),
    }
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    record_values = {
        str(record.get("nodeId") or ""),
        str(record.get("nomadNodeId") or ""),
        str(labels.get("luma.node.id") or ""),
        str(record.get("hostname") or ""),
        str(record.get("name") or node_name),
    }
    return bool((local_values - {""}) & (record_values - {""}))


def _storage_apply_available(state: Dict[str, Any]) -> bool:
    return bool(state.get("nomadAddr") or state.get("nomadToken") is not None)


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
        return {"name": name, "removed": "pending: storage apply is not available", "export": export_removed}
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    stack_name = f"luma-storage-{slugify(storage_class.name)}"
    removed = remove_from_nomad(config, state, slug=stack_name)
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
    # Native render (_apply_volume_mounts) creates the docker named volume under
    # the RAW source name from the manifest (e.g. "gitea_data:/data" -> docker
    # volume "gitea_data") — Nomad's docker driver applies no slug/project
    # prefix to a mount{type=volume,source=...} block. Cleanup MUST delete that
    # same literal name; prefixing it with "{slug}_" targets a volume that was
    # never created, so --delete-storage silently orphans the real data while
    # reporting success. (Compose volumes are cleaned via the compose path.)
    return list(named_volume_sources(service.volumes))


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


def _service_volume_cleanup_nodes(service: ServiceSpec, state: Dict[str, Any]) -> list[dict[str, str]]:
    if not service.node:
        return []
    record = _node_record_for_name(_state_nodes(state), service.node)
    if not record:
        return []
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return [
        {
            "id": str(record.get("nodeId") or record.get("nomadNodeId") or labels.get("luma.node.id") or service.node),
            "hostname": str(record.get("hostname") or record.get("tailscaleName") or record.get("displayName") or service.node),
            "lumaNode": service.node,
            "agentStatus": _node_agent_status(record),
        }
    ]


def _remove_docker_volume_across_nodes(volume_name: str, task_nodes: list[dict[str, str]], state: Dict[str, Any]) -> dict[str, str]:
    if not task_nodes:
        return _remove_local_docker_volume(volume_name)
    statuses: list[str] = []
    seen: set[str] = set()
    for node in task_nodes:
        key = str(node.get("id") or node.get("lumaNode") or "")
        if key in seen:
            continue
        seen.add(key)
        if _node_cleanup_target_is_local(node):
            result = _remove_local_docker_volume(volume_name)
            statuses.append(result["status"])
            continue
        node_name = str(node.get("lumaNode") or "").strip()
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


def _node_cleanup_target_is_local(node: dict[str, str]) -> bool:
    try:
        info = docker_request("GET", "/info")
    except (LumaError, AssertionError):
        return False
    if not isinstance(info, dict):
        return False
    local_values = {
        str(info.get("Name") or ""),
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
        node_id = str(record.get("nodeId") or record.get("nomadNodeId") or labels.get("luma.node.id") or "").strip()
        if not node_id:
            raise LumaError(f"Luma node {node_name} has no Nomad node ID; rerun luma node join on that node")
        return node_id

    return resolve


def _state_nodes(state: Dict[str, Any]) -> Dict[str, Any]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    expanded: Dict[str, Any] = {}
    for name, value in nodes.items():
        if not isinstance(value, dict):
            continue
        record = dict(value)
        key = str(name)
        expanded[key] = record
        for alias in _node_record_names(key, record):
            expanded.setdefault(alias, record)
    return expanded


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
    )


def _stack_service_spec(deployment: ComposeDeploymentSpec) -> ServiceSpec:
    return ServiceSpec(source=deployment.source, name=deployment.name, image="compose", region=deployment.region)


def _storage_stack_service_spec(stack_name: str) -> ServiceSpec:
    return ServiceSpec(source=Path("storage"), name=stack_name, image="storage", region="cn")


def _guard_compose_storage_switch(
    target: Path,
    stack_text: str,
    deployment: ComposeDeploymentSpec,
    *,
    previous_record: Dict[str, Any] | None = None,
) -> str:
    previous_volumes = _compose_storage_backend_signatures_from_record(previous_record)
    source = "deployment record"
    if previous_volumes is None:
        if not target.exists():
            return "No previous compose job"
        previous_volumes = _compose_storage_backend_signatures_from_artifact(target.read_text(encoding="utf-8"))
        source = "generated artifact"
    if previous_volumes is None:
        previous_volumes = {}
        source = "unknown generated artifact"
    current_volumes = _compose_storage_backend_signatures(deployment)
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
    return f"Storage backend unchanged from {source}"


def _compose_storage_backend_signatures(deployment: ComposeDeploymentSpec) -> Dict[str, Dict[str, Any]]:
    signatures: Dict[str, Dict[str, Any]] = {}
    for name, spec in deployment.volumes.items():
        if spec.storage_class:
            signatures[name] = {
                "kind": "storageClass",
                "storageClass": spec.storage_class,
                "path": spec.path or "",
                "accessMode": spec.access_mode,
            }
        elif spec.local_node or spec.local_path:
            signatures[name] = {
                "kind": "local",
                "node": spec.local_node or "",
                "path": spec.local_path or "",
            }
        else:
            signatures[name] = {"kind": "unmanaged"}
    return signatures


def _compose_storage_backend_signatures_from_record(record: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]] | None:
    if not record:
        return None
    stored = record.get("storageBackends")
    if isinstance(stored, dict):
        return {str(name): dict(value) for name, value in stored.items() if isinstance(value, dict)}
    manifest = record.get("manifest")
    compose_content = record.get("composeContent")
    if not isinstance(manifest, str) or not manifest.strip() or not isinstance(compose_content, str) or not compose_content.strip():
        return None
    try:
        deployment = _load_compose_request(
            {"manifest": manifest, "composeContent": compose_content},
            str(record.get("sourceName") or "luma.compose.yml"),
        )
    except LumaError:
        return None
    return _compose_storage_backend_signatures(deployment)


def _compose_storage_backend_signatures_from_artifact(text: str) -> Dict[str, Dict[str, Any]] | None:
    previous = _safe_yaml_mapping(text)
    if "Job" in previous:
        return None
    volumes = previous.get("volumes") if isinstance(previous.get("volumes"), dict) else None
    if volumes is None:
        return None
    signatures: Dict[str, Dict[str, Any]] = {}
    for name, value in volumes.items():
        if isinstance(value, dict) and value.get("driver_opts"):
            signatures[str(name)] = {
                "kind": "driver",
                "driver": str(value.get("driver") or ""),
                "driver_opts": value.get("driver_opts") or {},
            }
        elif isinstance(value, dict) and "local" in value:
            local = value.get("local") if isinstance(value.get("local"), dict) else {}
            signatures[str(name)] = {
                "kind": "local",
                "node": str(local.get("node") or ""),
                "path": str(local.get("path") or ""),
            }
        else:
            signatures[str(name)] = {"kind": "unmanaged"}
    return signatures


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
            names = ", ".join(sorted(str(key) for key in nodes)) or "none"
            raise LumaError(f"managed storage class {name} references unknown Luma node: {item.get('node')}. Registered nodes: {names}")
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        if not str(record.get("region") or labels.get("region") or "").strip():
            raise LumaError(f"managed storage node {node_name} has no region; rerun luma node join")
    regions = item.get("regions") if isinstance(item.get("regions"), list) else []
    for region in regions:
        if region not in VALID_REGIONS:
            raise LumaError(f"storage class {name} region must be one of {sorted(VALID_REGIONS)}")

def handle_secret_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    scoped = state.get("scopedSecrets") if isinstance(state.get("scopedSecrets"), dict) else {}
    names = sorted(str(key) for key in secrets)
    for scope, values in scoped.items():
        if not isinstance(values, dict):
            continue
        names.extend(f"{scope}/{key}" for key in sorted(str(key) for key in values))
    return {"secrets": sorted(names)}


def handle_secret_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name") or "").strip()
    raw_scope = str(body.get("scope") or "").strip()
    scope = slugify(raw_scope) if raw_scope else ""
    value = body.get("value")
    if not _valid_env_name(name):
        raise LumaError("secret name must be a valid environment variable name")
    if value is None or str(value) == "":
        raise LumaError("secret value is required")

    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        if scope:
            scoped = state.setdefault("scopedSecrets", {})
            if not isinstance(scoped, dict):
                scoped = {}
                state["scopedSecrets"] = scoped
            secrets = scoped.setdefault(scope, {})
            if not isinstance(secrets, dict):
                secrets = {}
                scoped[scope] = secrets
            secrets[name] = str(value)
            return
        secrets = state.setdefault("secrets", {})
        if not isinstance(secrets, dict):
            secrets = {}
            state["secrets"] = secrets
        secrets[name] = str(value)

    mutate_state(mutate)
    return {"name": name, "scope": scope, "saved": True}


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
    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        registries.pop(host, None)
        state["registries"] = registries

    mutate_state(mutate)
    return {"host": host, "removed": removed}


def _normalize_git_provider_type(provider_type: str) -> str:
    normalized = str(provider_type or "").strip().lower()
    if normalized in {"git", "gitea"}:
        return "gitea"
    if normalized == "github":
        return "github"
    raise LumaError("git provider type must be github or gitea")


def _normalize_git_provider_account(account: str) -> str:
    normalized = str(account or "").strip()
    if not normalized:
        raise LumaError("git provider account is required")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise LumaError("git provider account may contain only letters, numbers, dots, underscores, and dashes")
    return normalized


def _git_provider_id(provider_type: str, account: str) -> str:
    return f"{_normalize_git_provider_type(provider_type)}:{_normalize_git_provider_account(account)}"


def _git_provider_public_item(provider_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    provider_type = _normalize_git_provider_type(str(item.get("type") or ""))
    account = str(item.get("account") or provider_id.split(":", 1)[-1])
    return {
        "id": provider_id,
        "type": provider_type,
        "account": account,
        "baseUrl": str(item.get("baseUrl") or ""),
        "cloneBaseUrl": str(item.get("cloneBaseUrl") or ""),
        "username": str(item.get("username") or ""),
        "configured": bool(item.get("token")),
        "updatedAt": int(item.get("updatedAt") or 0),
    }


def _git_providers_state(state: Dict[str, Any]) -> Dict[str, Any]:
    providers = state.get("gitProviders")
    if not isinstance(providers, dict):
        providers = {}
        state["gitProviders"] = providers
    return providers


def _require_git_provider(state: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        raise LumaError("git provider id is required")
    providers = state.get("gitProviders") if isinstance(state.get("gitProviders"), dict) else {}
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        raise LumaError(f"git provider not found: {provider_id}")
    return provider


def _git_provider_api_base(provider: Dict[str, Any]) -> str:
    provider_type = _normalize_git_provider_type(str(provider.get("type") or ""))
    base = str(provider.get("baseUrl") or "").strip().rstrip("/")
    if provider_type == "github":
        return base or "https://api.github.com"
    if not base:
        raise LumaError("gitea provider baseUrl is required")
    return base


def _git_provider_clone_base(provider: Dict[str, Any]) -> str:
    provider_type = _normalize_git_provider_type(str(provider.get("type") or ""))
    clone_base = str(provider.get("cloneBaseUrl") or "").strip().rstrip("/")
    if clone_base:
        return clone_base
    if provider_type == "github":
        return "https://github.com"
    return _git_provider_api_base(provider)


def _git_provider_auth_headers(provider: Dict[str, Any]) -> Dict[str, str]:
    token = str(provider.get("token") or "").strip()
    if not token:
        raise LumaError("git provider token is not configured")
    provider_type = _normalize_git_provider_type(str(provider.get("type") or ""))
    if provider_type == "github":
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    return {"Authorization": f"token {token}", "Accept": "application/json"}


def _git_provider_json(provider: Dict[str, Any], url: str) -> Any:
    request = urllib.request.Request(url, headers=_git_provider_auth_headers(provider))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LumaError(f"git provider API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LumaError(f"git provider API unavailable: {exc}") from exc


def _git_provider_json_pages(provider: Dict[str, Any], url_for_page: Callable[[int], str], *, page_size: int = 100) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for page in range(1, 101):
        payload = _git_provider_json(provider, url_for_page(page))
        if not isinstance(payload, list):
            raise LumaError("git provider paged response must be a list")
        page_items = [item for item in payload if isinstance(item, dict)]
        items.extend(page_items)
        if len(payload) < page_size:
            break
    return items


def _normalize_repository_full_name(repository: str) -> str:
    repo = str(repository or "").strip().strip("/")
    repo = repo[:-4] if repo.lower().endswith(".git") else repo
    parts = [urllib.parse.unquote(part) for part in repo.split("/") if part]
    if len(parts) != 2:
        raise LumaError("repository must be owner/repo")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise LumaError("repository owner and name may contain only letters, numbers, dots, underscores, and dashes")
    return f"{parts[0]}/{parts[1]}"


def _git_provider_clone_url(provider: Dict[str, Any], repository: str) -> str:
    full_name = _normalize_repository_full_name(repository)
    return f"{_git_provider_clone_base(provider)}/{full_name}.git"


def _github_repo_api_base(provider: Dict[str, Any], repository: str) -> str:
    return f"{_git_provider_api_base(provider)}/repos/{urllib.parse.quote(_normalize_repository_full_name(repository), safe='/')}"


def _gitea_repo_api_base(provider: Dict[str, Any], repository: str) -> str:
    return f"{_git_provider_api_base(provider)}/api/v1/repos/{urllib.parse.quote(_normalize_repository_full_name(repository), safe='/')}"


def _normalized_repository_item(provider: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    full_name = str(raw.get("full_name") or raw.get("fullName") or "")
    if not full_name and isinstance(raw.get("owner"), dict):
        full_name = f"{raw['owner'].get('login') or raw['owner'].get('username')}/{raw.get('name') or ''}"
    full_name = _normalize_repository_full_name(full_name)
    clone_url = str(raw.get("clone_url") or raw.get("cloneUrl") or "").strip() or _git_provider_clone_url(provider, full_name)
    return {
        "fullName": full_name,
        "cloneUrl": clone_url,
        "defaultBranch": str(raw.get("default_branch") or raw.get("defaultBranch") or ""),
        "private": bool(raw.get("private")),
    }


def handle_git_provider_list(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    providers = state.get("gitProviders") if isinstance(state.get("gitProviders"), dict) else {}
    items = []
    for provider_id, item in sorted(providers.items()):
        if isinstance(item, dict):
            items.append(_git_provider_public_item(str(provider_id), item))
    return {"providers": items}


def handle_git_provider_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    provider_type = _normalize_git_provider_type(str(body.get("type") or ""))
    account = _normalize_git_provider_account(str(body.get("account") or ""))
    provider_id = _git_provider_id(provider_type, account)
    username = str(body.get("username") or "").strip()
    token_value = str(body.get("token") or "").strip()
    if not token_value:
        raise LumaError("git provider token is required")
    base_url = str(body.get("baseUrl") or "").strip().rstrip("/")
    clone_base_url = str(body.get("cloneBaseUrl") or "").strip().rstrip("/")
    if provider_type == "github":
        base_url = base_url or "https://api.github.com"
        clone_base_url = clone_base_url or "https://github.com"
    elif not base_url:
        raise LumaError("gitea provider baseUrl is required")
    if provider_type == "gitea":
        clone_base_url = clone_base_url or base_url

    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        providers = _git_providers_state(state)
        providers[provider_id] = {
            "type": provider_type,
            "account": account,
            "baseUrl": base_url,
            "cloneBaseUrl": clone_base_url,
            "username": username,
            "token": token_value,
            "updatedAt": int(time.time()),
        }

    mutate_state(mutate)
    return {"id": provider_id, "type": provider_type, "account": account, "username": username, "saved": True}


def handle_git_provider_remove(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = str(body.get("id") or "").strip()
    if not provider_id:
        provider_id = _git_provider_id(str(body.get("type") or ""), str(body.get("account") or ""))
    state = load_state()
    require_token(state, token, token_type="deploy")
    providers = state.get("gitProviders") if isinstance(state.get("gitProviders"), dict) else {}
    removed = bool(providers.get(provider_id))

    def mutate(state: Dict[str, Any]) -> None:
        require_token(state, token, token_type="deploy")
        providers = _git_providers_state(state)
        providers.pop(provider_id, None)

    mutate_state(mutate)
    return {"id": provider_id, "removed": removed}


def handle_git_provider_repositories(token: str, provider_id: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    provider = _require_git_provider(state, provider_id)
    provider_type = _normalize_git_provider_type(str(provider.get("type") or ""))
    if provider_type == "github":
        url_for_page = lambda page: f"{_git_provider_api_base(provider)}/user/repos?per_page=100&sort=updated&page={page}"
    else:
        url_for_page = lambda page: f"{_git_provider_api_base(provider)}/api/v1/user/repos?limit=100&page={page}"
    payload = _git_provider_json_pages(provider, url_for_page)
    return {"repositories": [_normalized_repository_item(provider, item) for item in payload]}


def handle_git_provider_refs(token: str, provider_id: str, repository: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    provider = _require_git_provider(state, provider_id)
    provider_type = _normalize_git_provider_type(str(provider.get("type") or ""))
    if provider_type == "github":
        base = _github_repo_api_base(provider, repository)
        branches_url_for_page = lambda page: f"{base}/branches?per_page=100&page={page}"
        tags_url_for_page = lambda page: f"{base}/tags?per_page=100&page={page}"
    else:
        base = _gitea_repo_api_base(provider, repository)
        branches_url_for_page = lambda page: f"{base}/branches?limit=100&page={page}"
        tags_url_for_page = lambda page: f"{base}/tags?limit=100&page={page}"
    branches = _git_provider_json_pages(provider, branches_url_for_page)
    tags = _git_provider_json_pages(provider, tags_url_for_page)
    refs: list[Dict[str, str]] = []
    refs.extend({"name": str(item.get("name") or ""), "type": "branch"} for item in branches if item.get("name"))
    refs.extend({"name": str(item.get("name") or ""), "type": "tag"} for item in tags if item.get("name"))
    return {"refs": refs}


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
    _require_egress_gateway_running(state)
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














def _require_egress_gateway_running(state: Dict[str, Any]) -> str:
    return _running_egress_gateway_node_name(state)


def _running_egress_gateway_node_name(state: Dict[str, Any]) -> str:
    try:
        config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
        config = load_config(config_path)
        _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
        allocations = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or "")).request("GET", "/v1/job/egress/allocations")
    except LumaError as exc:
        raise LumaError("image pull egress requires a running Nomad egress job; run `luma egress setup` on the manager") from exc
    if not isinstance(allocations, list):
        raise LumaError("image pull egress requires a running Nomad egress job; run `luma egress setup` on the manager")
    for allocation in allocations:
        if not _nomad_allocation_is_running(allocation):
            continue
        node_name = str(allocation.get("NodeName") or allocation.get("node_name") or "").strip()
        node_id = str(allocation.get("NodeID") or allocation.get("node_id") or "").strip()
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id=node_id)
        if entry:
            return entry[0]
        if node_name:
            return node_name
    raise LumaError("image pull egress requires a running Nomad egress job; run `luma egress setup` on the manager")


def _nomad_allocation_is_running(allocation: Any) -> bool:
    if not isinstance(allocation, dict):
        return False
    client_status = str(allocation.get("ClientStatus") or allocation.get("client_status") or "").lower()
    desired_status = str(allocation.get("DesiredStatus") or allocation.get("desired_status") or "run").lower()
    return client_status == "running" and desired_status in {"run", "running"}


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




def _local_control_node_name(state: Dict[str, Any], info: Dict[str, Any]) -> str:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    local_ids = {str(info.get("ID") or ""), str(info.get("Name") or "")} - {""}
    for name, record in nodes.items():
        if not isinstance(record, dict):
            continue
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        record_ids = {
            str(record.get("nodeId") or ""),
            str(record.get("nomadNodeId") or ""),
            str(record.get("hostname") or ""),
            str(record.get("name") or ""),
            str(labels.get("luma.node.id") or ""),
        } - {""}
        if local_ids & record_ids:
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
    if service.exposure not in {"cn-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel"}:
        return "Public route probe skipped: service is not an HTTP route"
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
    if isinstance(result, dict):
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        image = result[1]
        selected = image.get("selected")
        requested = image.get("requested")
        if selected and requested and selected != requested:
            return f"{requested} -> {selected}"
        if selected:
            return str(selected)
    if isinstance(result, str):
        if "\n" in result or len(result) > 200:
            return "generated"
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


def labels_for_region(region: str) -> Dict[str, str]:
    return {"region": region}


def labels_for_node(region: str, *, luma_name: str, node_id: str = "") -> Dict[str, str]:
    labels = labels_for_region(region)
    labels["luma.node.name"] = luma_name
    if node_id:
        labels["luma.node.id"] = node_id
    return labels


def resolve_service_node_pin(service: ServiceSpec, state: Dict[str, Any], *, engine: str = "nomad") -> ServiceSpec:
    _require_nomad_engine(engine)
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
    _ensure_nomad_node_record_schedulable(service.node, record)
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    node_id = str(record.get("nodeId") or record.get("nomadNodeId") or labels.get("luma.node.id") or "").strip()
    node_platform = _nomad_node_platform_from_record(record)
    if service.publish_port and (node_platform or "").split("/")[0] == "darwin":
        # Nomad bridge port mapping is unreachable on macOS/OrbStack: the
        # published port binds to a Mac host NIC IP that does not exist inside
        # the OrbStack VM where the container runs, so the port silently 502s.
        # Mac nodes must use docker host mode (no publishPort); the route then
        # targets the container's real port. Fail fast instead of deploying a
        # job that comes up "healthy" yet is unreachable.
        raise LumaError(
            f"service {service.slug} pins to Mac/OrbStack node {service.node} with "
            f"publishPort {service.publish_port}, but Nomad bridge port mapping is "
            "unreachable on macOS. Omit publishPort on Mac nodes; the route targets "
            "the container's real port."
        )
    return replace(service, node_id=node_id or None, node_platform=node_platform or None)


def _ensure_nomad_node_record_schedulable(node_name: str, record: Dict[str, Any]) -> None:
    status = str(record.get("nomadStatus") or record.get("nomadState") or "").strip().lower()
    if status in {"down", "missing", "offline", "disconnected", "unknown"}:
        raise LumaError(f"Luma node {node_name} Nomad node is {status}; restore the node before deploying pinned services")
    eligibility = str(record.get("schedulingEligibility") or record.get("nomadSchedulingEligibility") or "").strip().lower()
    if eligibility in {"ineligible", "disabled"}:
        raise LumaError(f"Luma node {node_name} Nomad scheduling eligibility is {eligibility}; set it eligible before deploying pinned services")
    if bool(record.get("drain") or record.get("nomadDrain")):
        raise LumaError(f"Luma node {node_name} Nomad node is draining; stop drain before deploying pinned services")


def _nomad_node_platform_from_record(record: Dict[str, Any]) -> str:
    direct = record.get("nodePlatform") or record.get("platform")
    parsed = _nomad_platform_from_value(direct)
    if parsed:
        return parsed
    attrs = record.get("nomadAttributes") or record.get("attributes")
    if not isinstance(attrs, dict):
        attrs = {}
    # The node agent reports os/arch on every heartbeat, stored in the nested
    # "agent" record. Node join / registration never writes top-level
    # platform/os/arch, so without this fallback the resolver always returns ""
    # for a real record and the Mac/OrbStack deploy guards never fire.
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    os_name = (
        record.get("os")
        or record.get("operatingSystem")
        or agent.get("os")
        or attrs.get("os.name")
        or attrs.get("kernel.name")
        or attrs.get("driver.docker.os_type")
    )
    arch = (
        record.get("arch")
        or record.get("architecture")
        or agent.get("arch")
        or attrs.get("cpu.arch")
        or attrs.get("unique.platform.arch")
        or attrs.get("driver.docker.arch")
    )
    return _compose_platform(os_name, arch)


def _nomad_platform_from_value(value: Any) -> str:
    if isinstance(value, dict):
        return _compose_platform(value.get("os") or value.get("OS"), value.get("arch") or value.get("architecture") or value.get("Architecture"))
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if "/" in raw:
        os_name, arch = raw.split("/", 1)
        return _compose_platform(os_name, arch)
    return ""


def _compose_platform(os_name: Any, arch: Any) -> str:
    normalized_os = _normalize_platform_os(str(os_name or ""))
    normalized_arch = _normalize_platform_arch(str(arch or ""))
    if not normalized_os or not normalized_arch:
        return ""
    return f"{normalized_os}/{normalized_arch}"


def _normalize_platform_os(value: str) -> str:
    value = value.strip().lower()
    if value in {"darwin", "macos", "mac os", "mac os x"}:
        return "darwin"
    if value in {"linux", "windows"}:
        return value
    return value


def _normalize_platform_arch(value: str) -> str:
    value = value.strip().lower()
    aliases = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "arm64/v8": "arm64",
    }
    return aliases.get(value, value)


def _node_record_for_name(nodes: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    entry = _node_record_entry_for_name_or_id(nodes, name)
    return entry[1] if entry else None


def _node_record_names(key: str, record: Dict[str, Any]) -> set[str]:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    names = {
        str(key or "").strip(),
        str(record.get("name") or "").strip(),
        str(record.get("displayName") or "").strip(),
        str(record.get("hostname") or "").strip(),
        str(labels.get("luma.node.name") or "").strip(),
        str(labels.get("luma_node_name") or "").strip(),
    }
    aliases = record.get("aliases")
    if isinstance(aliases, list):
        names.update(str(value).strip() for value in aliases)
    elif isinstance(aliases, str):
        names.add(aliases.strip())
    return {value for value in names if value}


def _node_record_is_manager(record: Dict[str, Any]) -> bool:
    if str(record.get("status") or "").lower() == "manager":
        return True
    if str(record.get("nomadRole") or "").lower() == "server":
        return True
    if bool(record.get("nomadServer")):
        return True
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return str(labels.get("role.nomad-manager") or "").lower() == "true"


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
    if merge_from and merge_from != node_name:
        aliases = current.get("aliases")
        if not isinstance(aliases, list):
            aliases = [str(aliases)] if aliases else []
        if merge_from not in aliases:
            aliases.append(merge_from)
        current["aliases"] = aliases
    nodes[node_name] = current


def _registered_nodes_summary(nodes: Dict[str, Any]) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for name in sorted(str(key) for key in nodes):
        raw = nodes.get(name)
        if not isinstance(raw, dict):
            raw = {}
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        aliases = sorted(_node_record_names(name, raw) - {name, str(raw.get("displayName") or name)})
        agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
        metrics = agent.get("metrics") if isinstance(agent.get("metrics"), dict) else {}
        container_stats = agent.get("containerStats") if isinstance(agent.get("containerStats"), list) else []
        diagnostics = agent.get("diagnostics") if isinstance(agent.get("diagnostics"), dict) else {}
        item = {
            "name": name,
            "displayName": str(raw.get("displayName") or name),
            "aliases": aliases,
            "hostname": str(raw.get("hostname") or ""),
            "nodeId": str(raw.get("nodeId") or raw.get("nomadNodeId") or labels.get("luma.node.id") or ""),
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
            "diagnostics": diagnostics,
        }
        items.append(item)
    return items


def _build_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    config = _build_config(state)
    nodes: list[Dict[str, Any]] = []
    for record in _build_node_records(state, require_ready=False):
        agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
        nodes.append(
            {
                "name": str(record.get("name") or ""),
                "region": str(record.get("region") or ""),
                "agentStatus": _node_agent_status(record),
                "agentOs": str(agent.get("os") or ""),
                "storageCapabilities": [str(value) for value in agent.get("capabilities") or []],
                "ready": _node_agent_is_ready(record, required_capability="docker-build"),
            }
        )
    return {
        "defaultNode": str(config.get("defaultNode") or DEFAULT_BUILD_NODE_NAME),
        "registryHost": str(config.get("registryHost") or ""),
        "pushHost": str(config.get("pushHost") or "localhost:5000"),
        "nodes": nodes,
    }


def _service_stats_by_name(
    registered_nodes: list[Dict[str, Any]],
    *,
    config: Any | None = None,
    state: Dict[str, Any] | None = None,
) -> dict[str, list[Dict[str, Any]]]:
    result: dict[str, list[Dict[str, Any]]] = {}
    allocation_index: dict[str, Dict[str, str]] | None = None
    try:
        cfg = config or load_config(_control_config_path())
        defaults = getattr(cfg, "defaults", {})
        if str(defaults.get("engine") or "nomad") == "nomad":
            allocation_index = _nomad_allocation_service_index(cfg, state if isinstance(state, dict) else {})
        else:
            allocation_index = {}
    except Exception:
        allocation_index = {}
    for node in registered_nodes:
        node_name = str(node.get("name") or "")
        raw_stats = node.get("containerStats") if isinstance(node.get("containerStats"), list) else []
        stats = _normalize_container_stats_for_engine(raw_stats, config=config, state=state, allocation_index=allocation_index)
        for raw in stats:
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


def _dashboard_nodes(
    registered_nodes: list[Dict[str, Any]],
    raw_nodes: list[Dict[str, Any]],
    *,
    terminal_nodes: set[str] | None = None,
) -> list[Dict[str, Any]]:
    merged: dict[str, Dict[str, Any]] = {}
    connected_terminals = terminal_nodes or set()
    for node in registered_nodes:
        name = str(node.get("name") or "")
        if not name:
            continue
        merged.setdefault(name, {})["registered"] = node
    for raw_node in raw_nodes:
        node = raw_node
        name = str(node.get("lumaNode") or node.get("rawId") or node.get("id") or "")
        if not name:
            continue
        merged.setdefault(name, {})["orchestrator"] = node

    rows: list[Dict[str, Any]] = []
    for name in sorted(merged):
        registered = merged[name].get("registered") if isinstance(merged[name].get("registered"), dict) else {}
        orchestrator = merged[name].get("orchestrator") if isinstance(merged[name].get("orchestrator"), dict) else {}
        display = str(registered.get("displayName") or orchestrator.get("hostname") or name)
        capabilities = [str(value) for value in registered.get("storageCapabilities") or []]
        agent_status = str(registered.get("agentStatus") or "missing")
        terminal_capable = "terminal" in capabilities
        terminal_names = _node_record_names(name, registered) if registered else {name}
        terminal_connected = bool(terminal_names & connected_terminals)
        terminal_status = "connected" if terminal_connected else "waiting" if agent_status == "ready" and terminal_capable else "unsupported"
        rows.append(
            {
                "name": name,
                "displayName": display,
                "hostname": str(registered.get("hostname") or orchestrator.get("hostname") or ""),
                "nodeId": str(registered.get("nodeId") or orchestrator.get("rawId") or orchestrator.get("lumaNodeId") or orchestrator.get("id") or ""),
                "region": str(registered.get("region") or orchestrator.get("region") or ""),
                "role": str(orchestrator.get("role") or ""),
                "state": str(orchestrator.get("state") or "missing"),
                "availability": str(orchestrator.get("availability") or ""),
                "leader": bool(orchestrator.get("leader")),
                "agentStatus": agent_status,
                "agentOs": str(registered.get("agentOs") or ""),
                "agentLastSeen": int(registered.get("agentLastSeen") or 0),
                "storageCapabilities": capabilities,
                "terminalConnected": terminal_connected,
                "terminalStatus": terminal_status,
                "metrics": registered.get("metrics") if isinstance(registered.get("metrics"), dict) else {},
                "capacity": orchestrator.get("capacity") if isinstance(orchestrator.get("capacity"), dict) else {},
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
    cert_resolver = ""
    for router in routers.values():
        if not isinstance(router, dict):
            continue
        domain = _host_from_rule(str(router.get("rule") or ""))
        tls = router.get("tls") if isinstance(router.get("tls"), dict) else {}
        if tls.get("certResolver"):
            cert_resolver = str(tls.get("certResolver") or "")
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
    return {"id": route_id, "kind": "http", "domain": domain, "upstreams": upstreams, "certResolver": cert_resolver}


def _dashboard_nomad_services(
    nomad_services: list[Dict[str, Any]],
    route_files: dict[str, Dict[str, Any]],
    *,
    state: Dict[str, Any] | None = None,
) -> list[Dict[str, Any]]:
    """Assemble the dashboard service list from Nomad jobs + Traefik file routes.

    Single-service deployments remain job-level. Compose deployments are a
    single Nomad job with multiple tasks, so those are expanded to task-level
    dashboard services and enriched from the stored Luma manifest.
    """
    deployment_index, compose_stacks = _dashboard_deployment_service_index(state or {})
    out: list[Dict[str, Any]] = []
    for svc in nomad_services:
        job_name = str(svc.get("jobId") or svc.get("name") or "")
        if not job_name:
            continue
        tasks = svc.get("tasks") if isinstance(svc.get("tasks"), list) else []
        is_compose = bool(svc.get("compose")) or job_name in compose_stacks
        if tasks and is_compose:
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                item = _dashboard_service_from_nomad_task(svc, task, route_files, deployment_index)
                if item:
                    out.append(item)
            continue
        item = _dashboard_service_from_nomad_job(svc, route_files, deployment_index)
        if item:
            out.append(item)
    out.sort(key=lambda s: str(s.get("name") or ""))
    return out


def _dashboard_service_from_nomad_job(
    svc: Dict[str, Any],
    route_files: dict[str, Dict[str, Any]],
    deployment_index: dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    name = str(svc.get("jobId") or svc.get("name") or "")
    if not name:
        return {}
    config = _dashboard_deployment_config(deployment_index, name, name, name, route_id=name)
    route = _dashboard_route_for_service(route_files, name, name, name, route_id=name)
    edge = _nomad_edge_route_from_task(svc)
    domain = str(config.get("domain") or route.get("domain") or edge.get("domain") or "")
    exposure = _dashboard_exposure(config, route, edge, region=str(svc.get("region") or ""))
    task_rollup = _dashboard_job_task_rollup(svc)
    running = task_rollup["running"] if task_rollup else int(svc.get("running") or 0)
    desired = task_rollup["desired"] if task_rollup else int(svc.get("desired") or running)
    pending = task_rollup["pending"] if task_rollup else int(svc.get("pending") or 0)
    failed = task_rollup["failed"] if task_rollup else int(svc.get("failed") or 0)
    nodes = task_rollup["nodes"] if task_rollup else [str(node) for node in svc.get("nodes") or [] if node]
    route_id = str(route.get("id") or config.get("routeId") or "")
    item: Dict[str, Any] = {
        "name": name,
        "stack": str(config.get("stack") or name),
        "fullName": str(config.get("fullName") or name),
        "status": str(svc.get("status") or ""),
        "region": str(config.get("region") or svc.get("region") or ""),
        "exposure": exposure,
        "domain": domain,
        "routeId": route_id,
        "upstreams": route.get("upstreams") if isinstance(route.get("upstreams"), list) else [],
        "targetPort": str(config.get("targetPort") or _route_file_target_port(route) or ""),
        "running": running,
        "desired": desired,
        "failed": failed,
        "pending": pending,
        "nodes": nodes,
        "tasks": svc.get("tasks") if isinstance(svc.get("tasks"), list) else [],
        "storage": svc.get("storage") if isinstance(svc.get("storage"), list) else [],
        "resources": svc.get("resources") if isinstance(svc.get("resources"), dict) else {},
    }
    if route:
        item["_routeFile"] = route
    return item


def _dashboard_job_task_rollup(svc: Dict[str, Any]) -> Dict[str, Any] | None:
    tasks = [task for task in svc.get("tasks") or [] if isinstance(task, dict)]
    if not tasks:
        return None
    if len(tasks) == 1:
        rollup_tasks = tasks
    else:
        primary_name = str(svc.get("jobId") or svc.get("name") or "")
        rollup_tasks = [task for task in tasks if str(task.get("name") or "") == primary_name] or tasks[:1]
    nodes = sorted({str(node) for task in tasks for node in task.get("nodes") or [] if node})
    if not nodes:
        nodes = sorted({
            str(row.get("node") or "")
            for task in tasks
            for row in (task.get("tasks") if isinstance(task.get("tasks"), list) else [])
            if isinstance(row, dict) and row.get("node")
        })
    return {
        "running": sum(int(task.get("running") or 0) for task in rollup_tasks),
        "desired": sum(int(task.get("desired") or 0) for task in rollup_tasks),
        "pending": sum(int(task.get("pending") or 0) for task in rollup_tasks),
        "failed": sum(int(task.get("failed") or 0) for task in rollup_tasks),
        "nodes": nodes,
    }


def _dashboard_service_from_nomad_task(
    job: Dict[str, Any],
    task: Dict[str, Any],
    route_files: dict[str, Dict[str, Any]],
    deployment_index: dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    stack = str(task.get("stack") or job.get("jobId") or job.get("name") or "")
    name = str(task.get("name") or "")
    if not stack or not name:
        return {}
    full_name = str(task.get("fullName") or _dashboard_task_full_name(stack, name, compose=True))
    route_id = f"{stack}-{slugify(name)}"
    config = _dashboard_deployment_config(deployment_index, stack, name, full_name, route_id=route_id)
    route = _dashboard_route_for_service(route_files, stack, name, full_name, route_id=str(config.get("routeId") or route_id))
    edge = _nomad_edge_route_from_task(task)
    domain = str(config.get("domain") or route.get("domain") or edge.get("domain") or "")
    exposure = _dashboard_exposure(config, route, edge, region=str(config.get("region") or task.get("region") or job.get("region") or ""))
    tasks = task.get("tasks") if isinstance(task.get("tasks"), list) else []
    nodes = [str(node) for node in task.get("nodes") or [] if node]
    if not nodes:
        nodes = sorted({str(item.get("node") or "") for item in tasks if isinstance(item, dict) and item.get("node")})
    item: Dict[str, Any] = {
        "name": name,
        "stack": str(config.get("stack") or stack),
        "fullName": str(config.get("fullName") or full_name),
        "status": str(task.get("status") or job.get("status") or ""),
        "region": str(config.get("region") or task.get("region") or job.get("region") or ""),
        "node": str(config.get("node") or ""),
        "exposure": exposure,
        "domain": domain,
        "routeId": str(route.get("id") or config.get("routeId") or ""),
        "upstreams": route.get("upstreams") if isinstance(route.get("upstreams"), list) else [],
        "targetPort": str(config.get("targetPort") or task.get("targetPort") or _route_file_target_port(route) or ""),
        "publishPort": str(config.get("publishPort") or task.get("publishPort") or ""),
        "image": str(task.get("image") or ""),
        "running": int(task.get("running") or 0),
        "desired": int(task.get("desired") or 0),
        "failed": int(task.get("failed") or 0),
        "pending": int(task.get("pending") or 0),
        "nodes": nodes,
        "tasks": tasks,
        "storage": task.get("storage") if isinstance(task.get("storage"), list) else [],
        "resources": task.get("resources") if isinstance(task.get("resources"), dict) else {},
    }
    item["diagnostics"] = _service_diagnostics(
        int(item["desired"] or 0),
        {"running": int(item["running"] or 0), "failed": int(item["failed"] or 0), "pending": int(item["pending"] or 0)},
        {},
    )
    if route:
        item["_routeFile"] = route
    return item


def _dashboard_deployment_service_index(state: Dict[str, Any]) -> tuple[dict[str, Dict[str, Any]], set[str]]:
    deployments = state.get("deployments") if isinstance(state.get("deployments"), dict) else {}
    index: dict[str, Dict[str, Any]] = {}
    compose_stacks: set[str] = set()
    for raw_slug, record in (deployments.get("services") if isinstance(deployments.get("services"), dict) else {}).items():
        if not isinstance(record, dict):
            continue
        data = _safe_manifest_dict(record.get("manifest"))
        name = slugify(str(data.get("name") or record.get("name") or raw_slug))
        entry = {
            "kind": "service",
            "stack": name,
            "name": name,
            "fullName": name,
            "region": str(data.get("region") or ""),
            "exposure": str(data.get("exposure") or "none"),
            "domain": str(data.get("domain") or ""),
            "targetPort": _string_port(data.get("port")),
            "node": str(data.get("node") or ""),
            "routeId": name,
        }
        _put_dashboard_deployment_entry(index, entry)
    for raw_slug, record in (deployments.get("compose") if isinstance(deployments.get("compose"), dict) else {}).items():
        if not isinstance(record, dict):
            continue
        data = _safe_manifest_dict(record.get("manifest"))
        stack = slugify(str(data.get("name") or record.get("name") or raw_slug))
        if not stack:
            continue
        compose_stacks.add(stack)
        default_region = str(data.get("region") or "")
        raw_services = data.get("services") if isinstance(data.get("services"), dict) else {}
        for service_name, raw_config in raw_services.items():
            config = raw_config if isinstance(raw_config, dict) else {}
            name = str(service_name)
            route_id = f"{stack}-{slugify(name)}"
            entry = {
                "kind": "compose",
                "stack": stack,
                "name": name,
                "fullName": _dashboard_task_full_name(stack, name, compose=True),
                "region": str(config.get("region") or default_region),
                "node": str(config.get("node") or ""),
                "exposure": str(config.get("exposure") or "none"),
                "domain": str(config.get("domain") or ""),
                "targetPort": _string_port(config.get("port")),
                "publishPort": _string_port(config.get("publishPort") or config.get("publish_port")),
                "routeId": route_id,
            }
            _put_dashboard_deployment_entry(index, entry)
    return index, compose_stacks


def _safe_manifest_dict(value: Any) -> Dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _put_dashboard_deployment_entry(index: dict[str, Dict[str, Any]], entry: Dict[str, Any]) -> None:
    stack = str(entry.get("stack") or "")
    name = str(entry.get("name") or "")
    full_name = str(entry.get("fullName") or "")
    route_id = str(entry.get("routeId") or "")
    for key in {
        f"{stack}:{name}",
        f"{stack}:{slugify(name)}",
        f"full:{full_name}",
        f"route:{route_id}",
    }:
        if key and not key.endswith(":"):
            index[key] = entry
    if entry.get("kind") == "service":
        index[f"job:{stack}"] = entry


def _dashboard_deployment_config(
    index: dict[str, Dict[str, Any]],
    stack: str,
    name: str,
    full_name: str,
    *,
    route_id: str,
) -> Dict[str, Any]:
    for key in (
        f"{stack}:{name}",
        f"{stack}:{slugify(name)}",
        f"full:{full_name}",
        f"route:{route_id}",
        f"job:{stack}",
    ):
        item = index.get(key)
        if isinstance(item, dict):
            return item
    return {}


def _dashboard_route_for_service(
    route_files: dict[str, Dict[str, Any]],
    stack: str,
    name: str,
    full_name: str,
    *,
    route_id: str,
) -> Dict[str, Any]:
    for key in (route_id, f"{stack}-{slugify(name)}", slugify(full_name), stack):
        route = route_files.get(key)
        if isinstance(route, dict):
            return route
    return {}


def _dashboard_exposure(config: Dict[str, Any], route: Dict[str, Any], edge: Dict[str, str], *, region: str) -> str:
    configured = str(config.get("exposure") or "")
    if configured:
        return configured
    if route.get("kind") == "tcp":
        return "tcp-relay"
    if route.get("kind") == "http":
        return "tailscale-relay"
    if edge.get("domain"):
        return "external-edge" if region == "global" else "cn-edge"
    return "none"


def _nomad_edge_route_from_task(item: Dict[str, Any]) -> Dict[str, str]:
    services = item.get("nomadServices") if isinstance(item.get("nomadServices"), list) else []
    for service in services:
        if not isinstance(service, dict):
            continue
        for tag in service.get("Tags") or []:
            domain = _host_from_rule(str(tag))
            if domain:
                return {"domain": domain}
    return {}


def _dashboard_task_full_name(stack: str, name: str, *, compose: bool = False) -> str:
    if not compose and name == stack:
        return stack
    return f"{stack}_{name}"


def _string_port(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if text else ""


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
                "certificateRetry": _dashboard_certificate_retry_capability(
                    service.get("_routeFile") if isinstance(service.get("_routeFile"), dict) else route_files.get(route_id, {}),
                    exposure,
                ),
                "destinations": _traffic_path_destinations(
                    service,
                    service.get("_routeFile") if isinstance(service.get("_routeFile"), dict) else route_files.get(route_id, {}),
                ),
            }
        )
    return paths


def _dashboard_certificate_retry_capability(route_file: Dict[str, Any], exposure: str) -> Dict[str, Any]:
    if exposure not in {"tailscale-relay"}:
        return {"available": False, "reason": "not a route-file HTTP exposure"}
    if not isinstance(route_file, dict) or route_file.get("kind") != "http":
        return {"available": False, "reason": "HTTP route file unavailable"}
    if not route_file.get("domain"):
        return {"available": False, "reason": "route has no public domain"}
    if not route_file.get("certResolver"):
        return {"available": False, "reason": "route has no TLS certResolver"}
    return {
        "available": True,
        "mode": "route-file-reload",
        "routeId": str(route_file.get("id") or ""),
        "certResolver": str(route_file.get("certResolver") or ""),
    }


def _traffic_path_destinations(service: Dict[str, Any], route_file: Dict[str, Any]) -> list[Dict[str, str]]:
    tasks = service.get("tasks") if isinstance(service.get("tasks"), list) else []
    running_tasks = [task for task in tasks if isinstance(task, dict) and str(task.get("state") or "") == "running"]
    upstreams = route_file.get("upstreams") if isinstance(route_file, dict) and isinstance(route_file.get("upstreams"), list) else []
    destinations: list[Dict[str, str]] = []
    for index, task in enumerate(running_tasks):
        node = str(task.get("node") or "")
        node_address = str(task.get("nodeAddress") or "")
        address = _upstream_for_node(node_address, upstreams)
        destinations.append(
            {
                "service": str(service.get("fullName") or service.get("name") or ""),
                "region": str(task.get("region") or service.get("region") or ""),
                "node": node,
                "nodeAddress": node_address,
                "address": address or (str(upstreams[index]) if index < len(upstreams) else ""),
                "state": str(task.get("state") or ""),
            }
        )
    if destinations:
        return destinations
    return [
        {
            "service": str(service.get("fullName") or service.get("name") or ""),
            "region": str(service.get("region") or ""),
            "node": "",
            "nodeAddress": "",
            "address": "",
            "state": "unresolved",
        }
    ]


def _upstream_for_node(node_address: str, upstreams: list[Any]) -> str:
    if not node_address:
        return ""
    for upstream in upstreams:
        value = str(upstream)
        if node_address in value:
            return value
    return ""


def _node_id_matches(candidate: str, wanted: str) -> bool:
    candidate = candidate.strip()
    wanted = wanted.strip()
    return bool(candidate and wanted and (candidate == wanted or candidate.startswith(wanted) or wanted.startswith(candidate)))


def _nomad_log_target_from_state(state: Dict[str, Any], service: str) -> tuple[str, str]:
    service = str(service or "").strip()
    if not service:
        return "", ""
    deployment_index, compose_stacks = _dashboard_deployment_service_index(state)
    seen: set[int] = set()
    for entry in deployment_index.values():
        if not isinstance(entry, dict):
            continue
        identity = id(entry)
        if identity in seen:
            continue
        seen.add(identity)
        stack = str(entry.get("stack") or "")
        name = str(entry.get("name") or "")
        full_name = str(entry.get("fullName") or "")
        route_id = str(entry.get("routeId") or "")
        if service in {full_name, route_id, f"{stack}/{name}", f"{stack}:{name}"}:
            return stack, name if entry.get("kind") == "compose" else ""
    if "/" in service:
        stack, task = service.split("/", 1)
        if stack in compose_stacks:
            return stack, task
    if "_" in service:
        stack, task = service.split("_", 1)
        if stack in compose_stacks:
            return stack, task
    return service, ""


def _nomad_pull_diagnostic_target(config: Any, state: Dict[str, Any], service: str) -> Dict[str, str]:
    client = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    job_id, task_filter = _nomad_log_target_from_state(state, service)
    if not job_id:
        raise LumaError("service is required")
    try:
        job = client.request("GET", f"/v1/job/{urllib.parse.quote(job_id, safe='')}")
        allocations = client.request("GET", f"/v1/job/{urllib.parse.quote(job_id, safe='')}/allocations")
    except LumaError as exc:
        raise LumaError(f"pull diagnostics unavailable for {service}: {exc}") from exc
    if not isinstance(job, dict):
        raise LumaError(f"pull diagnostics unavailable for {service}: invalid job detail")
    if not isinstance(allocations, list):
        raise LumaError(f"pull diagnostics unavailable for {service}: invalid allocation list")
    image, task_name = _nomad_job_task_image(job, task_filter or job_id)
    if not image and not task_filter:
        image, task_name = _nomad_job_task_image(job, "")
    if not image:
        raise LumaError(f"pull diagnostics unavailable for {service}: image not found in Nomad job")
    allocation = _latest_nomad_allocation_for_task(allocations, task_name)
    if not allocation:
        raise LumaError(f"pull diagnostics unavailable for {service}: no allocation found")
    node_name = _luma_node_name_for_allocation(state, allocation)
    if not node_name:
        node_id = str(allocation.get("NodeID") or allocation.get("NodeName") or "")
        raise LumaError(f"pull diagnostics unavailable for {service}: allocation node is not a ready Luma node ({node_id or 'unknown'})")
    return {
        "job": job_id,
        "task": task_name,
        "allocId": str(allocation.get("ID") or ""),
        "node": node_name,
        "image": image,
        "platform": "",
    }


def _nomad_job_task_image(job: Dict[str, Any], task_name: str) -> tuple[str, str]:
    groups = job.get("TaskGroups") if isinstance(job.get("TaskGroups"), list) else []
    fallback: tuple[str, str] = ("", "")
    for group in groups:
        if not isinstance(group, dict):
            continue
        tasks = group.get("Tasks") if isinstance(group.get("Tasks"), list) else []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            name = str(task.get("Name") or "")
            config = task.get("Config") if isinstance(task.get("Config"), dict) else {}
            image = str(config.get("image") or "")
            if image and not fallback[0]:
                fallback = (image, name)
            if image and task_name and name == task_name:
                return image, name
    return fallback if not task_name else ("", task_name)


def _latest_nomad_allocation_for_task(allocations: list[Any], task_name: str) -> Dict[str, Any] | None:
    candidates: list[Dict[str, Any]] = []
    for allocation in allocations:
        if not isinstance(allocation, dict):
            continue
        task_states = allocation.get("TaskStates") if isinstance(allocation.get("TaskStates"), dict) else {}
        if task_name and task_states and task_name not in task_states:
            continue
        candidates.append(allocation)
    if not candidates:
        return None
    running = [
        item for item in candidates
        if item.get("DesiredStatus") == "run" and str(item.get("ClientStatus") or "") in {"running", "pending"}
    ]
    selected = running or candidates
    selected.sort(key=lambda item: int(item.get("CreateTime") or item.get("CreateIndex") or 0), reverse=True)
    return selected[0]


def _luma_node_name_for_allocation(state: Dict[str, Any], allocation: Dict[str, Any]) -> str:
    node_id = str(allocation.get("NodeID") or "").strip()
    node_name = str(allocation.get("NodeName") or "").strip()
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for name, record in nodes.items():
        if not isinstance(record, dict):
            continue
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        candidates = {
            str(name),
            str(record.get("name") or ""),
            str(record.get("displayName") or ""),
            str(record.get("hostname") or ""),
            str(record.get("nodeId") or ""),
            str(labels.get("luma.node.id") or ""),
            str(labels.get("luma.node.name") or ""),
        }
        candidates.update(str(alias) for alias in (record.get("aliases") or []) if alias)
        if (node_id and node_id in candidates) or (node_name and node_name in candidates):
            return str(name)
    return ""


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


def _nomad_log_lines(config: Any, state: Dict[str, Any], service: str, *, tail: int = 120) -> list[str]:
    client = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    job_id, task_filter = _nomad_log_target_from_state(state, service)
    if not job_id:
        raise LumaError("service is required")
    try:
        allocations = client.request("GET", f"/v1/job/{urllib.parse.quote(job_id, safe='')}/allocations")
    except LumaError as exc:
        raise LumaError(f"Nomad logs unavailable for {service}: {exc}") from exc
    if not isinstance(allocations, list):
        raise LumaError(f"Nomad logs unavailable for {service}: invalid allocation list")
    active = [
        item for item in allocations
        if isinstance(item, dict)
        and item.get("DesiredStatus") == "run"
        and item.get("ClientStatus") == "running"
    ]
    candidates = active or [item for item in allocations if isinstance(item, dict)]
    if not candidates:
        raise LumaError(f"Nomad logs unavailable for {service}: no allocations found")
    candidates.sort(key=lambda item: int(item.get("CreateTime") or item.get("CreateIndex") or 0), reverse=True)

    sources: list[tuple[str, str, str]] = []
    for allocation in candidates[:2]:
        alloc_id = str(allocation.get("ID") or "")
        task_states = allocation.get("TaskStates") if isinstance(allocation.get("TaskStates"), dict) else {}
        if task_filter:
            task_names = [task_filter] if (not task_states or task_filter in task_states) else []
        else:
            task_names = [str(name) for name in task_states.keys() if str(name)]
        if not task_names:
            task_name = str(allocation.get("TaskGroup") or job_id)
            task_names = [task_name] if task_name else []
        for task_name in task_names:
            sources.append((alloc_id, task_name, "stdout"))
            sources.append((alloc_id, task_name, "stderr"))

    lines: list[str] = []
    label_sources = len({task for _, task, _ in sources}) > 1
    for alloc_id, task_name, log_type in sources:
        if not alloc_id or not task_name:
            continue
        query = urllib.parse.urlencode({
            "task": task_name,
            "type": log_type,
            "origin": "end",
            "offset": max(int(tail or 120) * 2048, 65536),
        })
        try:
            raw_payload = client.request_text("GET", f"/v1/client/fs/logs/{urllib.parse.quote(alloc_id, safe='')}?{query}")
            payload = _decode_nomad_log_payload(raw_payload)
        except LumaError:
            continue
        if not isinstance(payload, dict) or not payload.get("Data"):
            continue
        try:
            raw = base64.b64decode(str(payload.get("Data") or ""), validate=False)
        except Exception:
            continue
        prefix = f"[{task_name}/{log_type}] " if label_sources else ""
        for line in raw.decode("utf-8", errors="replace").splitlines():
            stripped = line.rstrip("\r")
            if stripped.strip():
                lines.append(prefix + stripped)
    if not lines:
        return []
    return lines[-tail:]


def _decode_nomad_log_payload(raw_payload: str) -> Any:
    text = str(raw_payload or "").lstrip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        try:
            payload, _end = decoder.raw_decode(text)
        except json.JSONDecodeError:
            return None
        return payload


def resolve_nomad_static_route_target(
    service: ServiceSpec,
    state: Dict[str, Any],
    *,
    prefer_publish_port: bool = False,
) -> ServiceSpec:
    """Resolve Nomad relay routes from Luma node records.

    Nomad tailscale-relay/tcp-relay jobs expose a host port on the target node.
    Routes come from the stable Luma node identity plus the node's recorded
    Tailscale/address metadata.
    """
    if service.exposure not in {"tailscale-relay", "tcp-relay"}:
        return service
    if service.exposure == "tailscale-relay" and (service.relay.get("url") or service.relay.get("host")):
        return service
    if service.exposure == "tcp-relay" and (service.tcp.get("address") or service.tcp.get("host")):
        return service
    if not service.node:
        raise LumaError(f"{service.exposure} on Nomad requires node or an explicit relay/tcp host")
    host = _nomad_route_host_for_node(state, service.node)
    port = int(service.publish_port or service.port or 0)
    if service.exposure == "tailscale-relay":
        relay = dict(service.relay)
        relay["host"] = host
        relay["port"] = port
        return replace(service, relay=relay)
    tcp = dict(service.tcp)
    tcp["host"] = host
    tcp["port"] = port
    return replace(service, tcp=tcp)


def _nomad_route_host_for_node(state: Dict[str, Any], node_name: str) -> str:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if not record:
        names = ", ".join(sorted(str(name) for name in nodes)) or "none"
        raise LumaError(f"unknown Luma node: {node_name}. Registered nodes: {names}")
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    for key in ("tailscaleIP", "tailscaleIp", "tailscaleName", "address", "hostname", "name", "displayName"):
        value = str(record.get(key) or labels.get(key) or "").strip()
        if value:
            return value
    return node_name


def resolve_service_image(
    config: Any,
    service: ServiceSpec,
    *,
    registry_auth: Dict[str, str] | None = None,
    state: Dict[str, Any] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    if state is not None:
        return _resolve_service_image_for_deployment(config, service, state, registry_auth=registry_auth)

    images = [service.image, *_fallback_images(config, service.image)]
    errors: list[str] = []
    platform = str(service.node_platform or "").strip()
    for image in images:
        image_registry_auth = registry_auth if registry_auth_matches_image(registry_auth, image) else None
        try:
            force_pull = image_uses_mutable_latest_tag(image)
            resolved_image = ensure_image_present(image, registry_auth=image_registry_auth, force_pull=force_pull, platform=platform)
            deploy_image = resolved_image or image
            result = {
                "requested": service.image,
                "selected": image,
                "deployed": deploy_image,
                "fallback": image != service.image,
                "registryAuth": bool(image_registry_auth),
                "forcePull": force_pull,
                "platform": platform,
            }
            return replace(service, image=deploy_image), result
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError("unable to pull service image; tried " + "; ".join(errors))


def _resolve_service_image_for_deployment(
    config: Any,
    service: ServiceSpec,
    state: Dict[str, Any],
    *,
    registry_auth: Dict[str, str] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    if not service.node:
        resolved = _resolve_mutable_service_image_from_registry(service, registry_auth=registry_auth)
        if resolved:
            return resolved
        return _deferred_service_image(config, service, registry_auth=registry_auth, reason="Nomad will pull on the scheduled node")

    if not _node_agent_has_capability(state, service.node, "docker-image"):
        resolved = _resolve_mutable_service_image_from_registry(service, registry_auth=registry_auth, node=service.node)
        if resolved:
            return resolved
        return _deferred_service_image(
            config,
            service,
            registry_auth=registry_auth,
            reason=f"Target node agent on {service.node} does not advertise docker-image",
        )

    images = [service.image, *_fallback_images(config, service.image)]
    errors: list[str] = []
    platform = str(service.node_platform or "").strip()
    for image in images:
        image_registry_auth = registry_auth if registry_auth_matches_image(registry_auth, image) else None
        force_pull = image_uses_mutable_latest_tag(image)
        node_image = image
        resolved_by = "target-node"
        if force_pull:
            node_image = resolve_registry_image_digest(image, registry_auth=image_registry_auth)
            resolved_by = "registry"
            force_pull = False
        payload: Dict[str, Any] = {
            "image": node_image,
            "forcePull": force_pull,
            "platform": platform,
        }
        if image_registry_auth:
            payload["registryAuth"] = image_registry_auth
        try:
            result = _resolve_image_on_target_node(state, service.node, node_image, payload)
            deploy_image = str(result.get("deployed") or result.get("digest") or node_image)
            image_result = {
                "requested": service.image,
                "selected": image,
                "deployed": deploy_image,
                "fallback": image != service.image,
                "registryAuth": bool(image_registry_auth),
                "forcePull": force_pull,
                "platform": platform,
                "node": service.node,
                "resolvedBy": resolved_by,
            }
            return replace(service, image=deploy_image), image_result
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError(f"unable to pull service image on target node {service.node}; tried " + "; ".join(errors))


def _resolve_mutable_service_image_from_registry(
    service: ServiceSpec,
    *,
    registry_auth: Dict[str, str] | None = None,
    node: str = "",
) -> tuple[ServiceSpec, Dict[str, Any]] | None:
    if not image_uses_mutable_latest_tag(service.image):
        return None
    digest_image = resolve_registry_image_digest(service.image, registry_auth=registry_auth)
    return replace(service, image=digest_image), {
        "requested": service.image,
        "selected": service.image,
        "deployed": digest_image,
        "fallback": False,
        "registryAuth": bool(registry_auth and registry_auth_matches_image(registry_auth, service.image)),
        "forcePull": False,
        "platform": str(service.node_platform or "").strip(),
        "node": node,
        "resolvedBy": "registry",
    }


def _resolve_image_on_target_node(state: Dict[str, Any], node_name: str, image: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _run_node_agent_task(
            state,
            node_name,
            "resolve-docker-image",
            payload,
            timeout=600,
            required_capability="docker-image",
        )
    except LumaError as exc:
        if not _target_image_pull_proxy_applicable(state, node_name, image, exc):
            raise
        _configure_target_image_pull_proxy(state, node_name, image)
        try:
            return _run_node_agent_task(
                state,
                node_name,
                "resolve-docker-image",
                payload,
                timeout=600,
                required_capability="docker-image",
            )
        except LumaError as retry_exc:
            raise LumaError(f"{exc}; retry after target node proxy setup failed: {retry_exc}") from retry_exc


def _target_image_pull_proxy_applicable(state: Dict[str, Any], node_name: str, image: str, error: Exception) -> bool:
    if not image_pull_requires_egress(image):
        return False
    if not _node_agent_has_capability(state, node_name, "docker-egress-proxy"):
        return False
    lowered = str(error).lower()
    markers = ("failed to do request", "eof", "timeout", "connection reset", "cannot reach the registry")
    if not any(marker in lowered for marker in markers):
        return False
    return bool(_target_image_pull_proxy_url(state, node_name))


def _proxy_url_is_loopback(proxy_url: str) -> bool:
    host = (urllib.parse.urlparse(str(proxy_url or "")).hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _canonical_node_name(state: Dict[str, Any], node_name: str) -> str:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    entry = _node_record_entry_for_name_or_id(nodes, node_name)
    return entry[0] if entry else str(node_name or "").strip()


def _target_image_pull_proxy_url(state: Dict[str, Any], node_name: str, *, gateway_node: str = "") -> str:
    if not _proxy_url_is_loopback(EGRESS_PROXY_URL):
        return EGRESS_PROXY_URL
    gateway = str(gateway_node or "").strip() or _running_egress_gateway_node_name(state)
    if gateway and _canonical_node_name(state, node_name) == _canonical_node_name(state, gateway):
        return EGRESS_PROXY_URL
    return _egress_gateway_proxy_url(state, gateway_node=gateway)


def _egress_gateway_proxy_url(state: Dict[str, Any], *, gateway_node: str = "") -> str:
    name = str(gateway_node or "").strip() or _running_egress_gateway_node_name(state)
    try:
        host = _nomad_route_host_for_node(state, name)
    except LumaError:
        return ""
    if host:
        return f"http://{host}:7890"
    return ""


def _configure_target_image_pull_proxy(state: Dict[str, Any], node_name: str, image: str) -> None:
    gateway_node = _require_egress_gateway_running(state)
    proxy = _target_image_pull_proxy_url(state, node_name, gateway_node=gateway_node)
    if not proxy:
        raise LumaError(f"image pull egress is running, but no reachable egress proxy address was found for {node_name}")
    _run_node_agent_task(
        state,
        node_name,
        "configure-docker-egress-proxy",
        {"proxy": proxy, "noProxy": EGRESS_NO_PROXY},
        timeout=180,
        required_capability="docker-egress-proxy",
    )


def _deferred_service_image(
    config: Any,
    service: ServiceSpec,
    *,
    registry_auth: Dict[str, str] | None,
    reason: str,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    return service, {
        "requested": service.image,
        "selected": service.image,
        "deployed": service.image,
        "fallback": False,
        "registryAuth": bool(registry_auth and registry_auth_matches_image(registry_auth, service.image)),
        "forcePull": False,
        "platform": str(service.node_platform or "").strip(),
        "node": service.node or "",
        "resolvedBy": "scheduled-node",
        "deferred": True,
        "reason": reason,
    }


REGISTRY_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def resolve_registry_image_digest(image: str, *, registry_auth: Dict[str, str] | None = None) -> str:
    if "@" in str(image or ""):
        return image
    host = registry_host_from_image(image)
    registry = public_registry_url(host)
    repo_path, reference = _registry_manifest_path(image, host)
    digest = _registry_manifest_digest(registry, repo_path, reference, registry_auth=registry_auth)
    return f"{_image_repository(image)}@{digest}"


def _registry_manifest_path(image: str, host: str) -> tuple[str, str]:
    repository, reference = _split_image_tag(image)
    parts = repository.split("/")
    if parts:
        first = parts[0]
        if "." in first or ":" in first or first == "localhost":
            parts = parts[1:]
    if host in {"docker.io", "registry-1.docker.io"} and len(parts) == 1:
        parts = ["library", parts[0]]
    repo_path = "/".join(parts).strip("/")
    if not repo_path:
        raise LumaError(f"cannot resolve registry repository for image: {image}")
    return repo_path, reference


def _registry_manifest_digest(
    registry: str,
    repo_path: str,
    reference: str,
    *,
    registry_auth: Dict[str, str] | None = None,
) -> str:
    url = f"https://{registry}/v2/{repo_path}/manifests/{urllib.parse.quote(reference, safe='')}"
    headers = {"Accept": REGISTRY_MANIFEST_ACCEPT, "User-Agent": "luma-control-registry-resolver"}
    if registry_auth:
        headers["Authorization"] = _basic_registry_auth(registry_auth)
    try:
        return _registry_head_digest(url, headers)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"registry manifest lookup failed for {repo_path}:{reference}: HTTP {exc.code}: {detail}") from exc
        token = _registry_bearer_token(exc.headers.get("WWW-Authenticate", ""), registry_auth=registry_auth)
        retry_headers = dict(headers)
        retry_headers["Authorization"] = f"Bearer {token}"
        return _registry_head_digest(url, retry_headers)


def _registry_head_digest(url: str, headers: Dict[str, str]) -> str:
    req = urllib.request.Request(url, method="HEAD", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        digest = resp.headers.get("Docker-Content-Digest") or resp.headers.get("docker-content-digest")
    if not digest or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise LumaError(f"registry manifest lookup did not return a sha256 digest: {url}")
    return digest.lower()


def _registry_bearer_token(challenge: str, *, registry_auth: Dict[str, str] | None = None) -> str:
    params = _parse_www_authenticate_bearer(challenge)
    realm = params.get("realm", "")
    if not realm:
        raise LumaError("registry requested bearer auth without a realm")
    query = {key: value for key, value in params.items() if key in {"service", "scope"} and value}
    token_url = realm
    if query:
        token_url += ("&" if "?" in token_url else "?") + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json", "User-Agent": "luma-control-registry-resolver"}
    if registry_auth:
        headers["Authorization"] = _basic_registry_auth(registry_auth)
    req = urllib.request.Request(token_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise LumaError("registry token endpoint returned invalid JSON") from exc
    token = str(payload.get("token") or payload.get("access_token") or "").strip()
    if not token:
        raise LumaError("registry token endpoint did not return a token")
    return token


def _parse_www_authenticate_bearer(value: str) -> Dict[str, str]:
    text = str(value or "").strip()
    if not text.lower().startswith("bearer "):
        return {}
    return {match.group(1): match.group(2) for match in re.finditer(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"', text[7:])}


def _basic_registry_auth(registry_auth: Dict[str, str]) -> str:
    raw = f"{registry_auth.get('username', '')}:{registry_auth.get('password', '')}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _node_agent_has_capability(state: Dict[str, Any], node_name: str, capability: str) -> bool:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if not isinstance(record, dict):
        return False
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    capabilities = {str(value) for value in agent.get("capabilities") or []}
    return capability in capabilities and _node_agent_is_ready(record, required_capability=capability)


def ensure_image_present(
    image: str,
    *,
    registry_auth: Dict[str, str] | None = None,
    force_pull: bool = False,
    platform: str = "",
) -> str | None:
    encoded = urllib.parse.quote(image, safe="")
    platform = str(platform or "").strip()
    expected_digest = ""
    if force_pull and not platform and image_uses_mutable_latest_tag(image):
        expected_digest = resolve_registry_image_digest(image, registry_auth=registry_auth)
        status, raw = docker_request_raw("GET", f"/images/{urllib.parse.quote(expected_digest, safe='')}/json")
        if status == 200:
            return expected_digest
        status, raw = docker_request_raw("GET", f"/images/{encoded}/json")
        if status == 200 and _image_details_has_repo_digest(raw, expected_digest):
            return expected_digest
    if not force_pull and not platform:
        status, _ = docker_request_raw("GET", f"/images/{encoded}/json")
        if status == 200:
            return None
    from_image = urllib.parse.quote(image, safe="")
    query = f"fromImage={from_image}"
    if platform:
        query += f"&platform={urllib.parse.quote(platform, safe='/')}"
    headers = {}
    auth_header = docker_registry_auth_header(registry_auth)
    if auth_header:
        headers["X-Registry-Auth"] = auth_header
    status, raw = docker_request_raw("POST", f"/images/create?{query}", headers=headers)
    if status >= 400:
        raise LumaError(_docker_pull_error_message(status, raw, registry_auth=registry_auth, platform=platform))
    if '"error"' in raw:
        raise LumaError(_docker_pull_error_message(status, raw, registry_auth=registry_auth, platform=platform))
    if force_pull or platform:
        return _image_pull_repo_digest(image, raw) or expected_digest or _image_repo_digest(image)
    return None




def _image_details_has_repo_digest(raw: str, expected_digest: str) -> bool:
    if not expected_digest:
        return False
    try:
        details = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return False
    digests = details.get("RepoDigests")
    if not isinstance(digests, list):
        return False
    return any(str(item) == expected_digest for item in digests)


def _image_pull_repo_digest(image: str, raw: str) -> str:
    match = re.search(r"Digest:\s*(sha256:[0-9a-fA-F]+)", raw or "")
    if not match:
        return ""
    return f"{_image_repository(image)}@{match.group(1).lower()}"


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




def _fallback_images(config: Any, image: str) -> list[str]:
    if _has_registry(image):
        return []
    defaults = config.defaults if hasattr(config, "defaults") else {}
    mirrors = defaults.get("imageMirrors")
    if mirrors is None:
        mirrors = [
            "docker.1panel.live",
            "docker.1ms.run",
            "docker.m.daocloud.io",
        ]
    if not isinstance(mirrors, list):
        return []
    return [f"{mirror}/{image}" for mirror in mirrors if isinstance(mirror, str) and mirror]


def _has_registry(image: str) -> bool:
    if "/" not in image:
        return False
    first = image.split("/", 1)[0]
    return "." in first or ":" in first or first == "localhost"


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


class _TerminalSession:
    def __init__(self, session_id: str, node_name: str, browser: WebSocket, agent: "_TerminalAgentConnection"):
        self.id = session_id
        self.node_name = node_name
        self.browser = browser
        self.agent = agent
        self.last_activity = time.time()
        self.browser_lock = asyncio.Lock()
        self.closed = False

    def touch(self) -> None:
        self.last_activity = time.time()


class _TerminalAgentConnection:
    def __init__(self, node_name: str, websocket: WebSocket):
        self.node_name = node_name
        self.websocket = websocket
        self.send_lock: asyncio.Lock | None = None

    def send_lock_for_loop(self) -> asyncio.Lock:
        if self.send_lock is None:
            self.send_lock = asyncio.Lock()
        return self.send_lock


class TerminalBroker:
    def __init__(self, *, per_node_limit: int, idle_timeout_seconds: int):
        self.per_node_limit = max(int(per_node_limit or 2), 1)
        self.idle_timeout_seconds = max(int(idle_timeout_seconds or 1800), 60)
        self._agents: dict[str, _TerminalAgentConnection] = {}
        self._sessions: dict[str, _TerminalSession] = {}
        self._lock: asyncio.Lock | None = None
        self._pending_auth: asyncio.Semaphore | None = None

    def _lock_for_loop(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _pending_auth_for_loop(self) -> asyncio.Semaphore:
        if self._pending_auth is None:
            self._pending_auth = asyncio.Semaphore(max(self.per_node_limit * 4, 8))
        return self._pending_auth

    def connected_nodes(self) -> set[str]:
        return set(self._agents)

    async def connect_browser(self, websocket: WebSocket) -> None:
        node_name = str(websocket.query_params.get("node") or "").strip()
        if not node_name:
            await websocket.close(code=1008)
            return
        if not await self._acquire_pending_auth(websocket):
            return
        try:
            await websocket.accept()
            try:
                auth = await asyncio.wait_for(websocket.receive_json(), timeout=10)
                if not isinstance(auth, dict) or str(auth.get("type") or "") != "auth":
                    raise LumaError("terminal auth required")
                token = str(auth.get("token") or "").strip()
                if not token:
                    raise LumaError("terminal auth token required")
            except Exception:
                await websocket.close(code=1008)
                return
        finally:
            self._pending_auth_for_loop().release()
        try:
            state = load_state()
            require_token(state, token, token_type="deploy")
            nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
            entry = _node_record_entry_for_name_or_id(nodes, node_name)
            if entry is None:
                raise LumaError(f"node is not registered: {node_name}")
            node_name = entry[0]
        except LumaError:
            await websocket.close(code=1008)
            return
        session: _TerminalSession | None = None
        idle_task: asyncio.Task[Any] | None = None
        try:
            async with self._lock_for_loop():
                agent = self._agents.get(node_name)
                active = [item for item in self._sessions.values() if item.node_name == node_name and not item.closed]
                if not agent:
                    await websocket.send_json({"type": "error", "message": f"terminal agent is not connected on {node_name}"})
                    await websocket.close(code=1013)
                    return
                if len(active) >= self.per_node_limit:
                    await websocket.send_json({"type": "error", "message": f"terminal session limit reached on {node_name}"})
                    await websocket.close(code=1013)
                    return
                session_id = f"term-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
                session = _TerminalSession(session_id, node_name, websocket, agent)
                self._sessions[session_id] = session
            await websocket.send_json({"type": "open", "sessionId": session.id, "node": node_name})
            await self._send_agent(agent, {"type": "open", "sessionId": session.id, "node": node_name, "cols": 120, "rows": 32})
            idle_task = asyncio.create_task(self._watch_idle(session))
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                kind = str(message.get("type") or "")
                if kind not in {"input", "resize", "close", "ping"}:
                    continue
                session.touch()
                payload = dict(message)
                payload["sessionId"] = session.id
                await self._send_agent(session.agent, payload)
                if kind == "close":
                    break
        except Exception:
            pass
        finally:
            if idle_task:
                idle_task.cancel()
            if session:
                await self.close_session(session.id, notify_agent=True)

    async def connect_agent(self, websocket: WebSocket) -> None:
        node_name = str(websocket.query_params.get("node") or "").strip()
        node_id = str(websocket.query_params.get("nodeId") or "").strip()
        if not node_name:
            await websocket.close(code=1008)
            return
        if not await self._acquire_pending_auth(websocket):
            return
        try:
            await websocket.accept()
            try:
                auth = await asyncio.wait_for(websocket.receive_json(), timeout=10)
                if not isinstance(auth, dict) or str(auth.get("type") or "") != "auth":
                    raise LumaError("terminal auth required")
                token = str(auth.get("token") or "").strip()
                if not token:
                    raise LumaError("terminal auth token required")
            except Exception:
                await websocket.close(code=1008)
                return
        finally:
            self._pending_auth_for_loop().release()
        try:
            state = load_state()
            canonical_node_name, _record = _require_node_agent_token_entry(state, token, node_name, node_id=node_id)
        except LumaError:
            await websocket.close(code=1008)
            return
        connection = _TerminalAgentConnection(canonical_node_name, websocket)
        async with self._lock_for_loop():
            previous = self._agents.get(canonical_node_name)
            self._agents[canonical_node_name] = connection
        if previous:
            try:
                await previous.websocket.close(code=1012)
            except Exception:
                pass
        try:
            await websocket.send_json({"type": "ready", "node": canonical_node_name})
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                session_id = str(message.get("sessionId") or "")
                if not session_id:
                    continue
                await self.forward_to_browser(session_id, message)
        except Exception:
            pass
        finally:
            async with self._lock_for_loop():
                if self._agents.get(canonical_node_name) is connection:
                    self._agents.pop(canonical_node_name, None)
            session_ids = await self._session_ids_for_agent(canonical_node_name, connection)
            for session_id in session_ids:
                await self.close_session(session_id, notify_agent=False, browser_message="terminal agent disconnected")

    async def forward_to_browser(self, session_id: str, message: Dict[str, Any]) -> None:
        async with self._lock_for_loop():
            session = self._sessions.get(session_id)
        if not session or session.closed:
            return
        session.touch()
        kind = str(message.get("type") or "")
        try:
            async with session.browser_lock:
                await session.browser.send_json(dict(message))
        except Exception:
            await self.close_session(session_id, notify_agent=True)
            return
        if kind in {"exit", "error", "close"}:
            await self.close_session(session_id, notify_agent=kind in {"exit", "error"})

    async def close_session(self, session_id: str, *, notify_agent: bool, browser_message: str = "") -> None:
        async with self._lock_for_loop():
            session = self._sessions.pop(session_id, None)
            if session:
                session.closed = True
        if not session:
            return
        if browser_message:
            try:
                async with session.browser_lock:
                    await session.browser.send_json({"type": "error", "sessionId": session_id, "message": browser_message})
            except Exception:
                pass
        try:
            async with session.browser_lock:
                await session.browser.close(code=1000)
        except Exception:
            pass
        if notify_agent:
            await self._send_agent(session.agent, {"type": "close", "sessionId": session_id})

    async def close_agent(self, node_name: str) -> None:
        async with self._lock_for_loop():
            agent = self._agents.pop(node_name, None)
        if agent:
            try:
                await agent.websocket.close(code=1012)
            except Exception:
                pass

    async def _session_ids_for_agent(self, node_name: str, agent: _TerminalAgentConnection) -> list[str]:
        async with self._lock_for_loop():
            return [sid for sid, item in self._sessions.items() if item.node_name == node_name and item.agent is agent]

    async def _acquire_pending_auth(self, websocket: WebSocket) -> bool:
        try:
            await asyncio.wait_for(self._pending_auth_for_loop().acquire(), timeout=0.1)
            return True
        except Exception:
            try:
                await websocket.close(code=1013)
            except Exception:
                pass
            return False

    async def _watch_idle(self, session: _TerminalSession) -> None:
        try:
            while not session.closed:
                await asyncio.sleep(10)
                if time.time() - session.last_activity < self.idle_timeout_seconds:
                    continue
                try:
                    async with session.browser_lock:
                        await session.browser.send_json({"type": "error", "sessionId": session.id, "message": "terminal session idle timeout"})
                except Exception:
                    pass
                await self.close_session(session.id, notify_agent=True)
                return
        except asyncio.CancelledError:
            return

    async def _send_agent(self, agent: _TerminalAgentConnection, message: Dict[str, Any]) -> None:
        try:
            async with agent.send_lock_for_loop():
                await agent.websocket.send_json(message)
        except Exception:
            await self.close_agent(agent.node_name)


TERMINAL_BROKER = TerminalBroker(
    per_node_limit=TERMINAL_SESSION_LIMIT_PER_NODE,
    idle_timeout_seconds=TERMINAL_IDLE_TIMEOUT_SECONDS,
)


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
                cache_control = "no-store"
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
                    "capabilities": ["node-region", "service-proxy", "dashboard", "service-remove", "node-agent-storage", "terminal"],
                },
            )
            return
        try:
            token = bearer_token(self.headers)
            if parsed_path == "/v1/git-providers":
                self._json(200, handle_git_provider_list(token))
                return
            git_repos_match = re.fullmatch(r"/v1/git-providers/([^/]+)/repositories", parsed_path)
            if git_repos_match:
                provider_id = urllib.parse.unquote(git_repos_match.group(1))
                self._json(200, handle_git_provider_repositories(token, provider_id))
                return
            git_refs_match = re.fullmatch(r"/v1/git-providers/([^/]+)/repositories/([^/]+)/([^/]+)/refs", parsed_path)
            if git_refs_match:
                provider_id = urllib.parse.unquote(git_refs_match.group(1))
                repository = f"{urllib.parse.unquote(git_refs_match.group(2))}/{urllib.parse.unquote(git_refs_match.group(3))}"
                self._json(200, handle_git_provider_refs(token, provider_id, repository))
                return
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
            if parsed_path == "/v1/builds":
                self._json(200, handle_build_run_list(token))
                return
            build_match = re.fullmatch(r"/v1/builds/([^/]+)", parsed_path)
            if build_match:
                self._json(200, handle_build_run_get(token, urllib.parse.unquote(build_match.group(1))))
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
            if self.path == "/v1/nodes/nomad-join":
                self._json(200, handle_node_nomad_join(token, body))
                return
            if self.path == "/v1/nodes/agent-token":
                self._json(200, handle_node_agent_token(token, body))
                return
            if self.path == "/v1/node-agent/lease":
                self._json(200, handle_node_agent_lease(token, body))
                return
            if self.path == "/v1/node-agent/heartbeat":
                self._json(200, handle_node_agent_heartbeat(token, body))
                return
            if self.path == "/v1/node-agent/tasks/complete":
                self._json(200, handle_node_agent_complete(token, body))
                return
            if self.path == "/v1/node-agent/tasks/progress":
                self._json(200, handle_node_agent_progress(token, body))
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
            build_retry_match = re.fullmatch(r"/v1/builds/([^/]+)/retry", self.path)
            if build_retry_match:
                self._json(200, handle_build_run_retry(token, urllib.parse.unquote(build_retry_match.group(1))))
                return
            build_retry_stream_match = re.fullmatch(r"/v1/builds/([^/]+)/retry/stream", self.path)
            if build_retry_stream_match:
                self._stream_build_run_retry(token, urllib.parse.unquote(build_retry_stream_match.group(1)))
                return
            if self.path == "/v1/builds/config":
                self._json(200, handle_build_config_set(token, body))
                return
            if self.path == "/v1/builds/stream":
                self._stream_build_deploy(token, body)
                return
            if self.path == "/v1/builds":
                self._json(200, handle_build_deploy(token, body))
                return
            if self.path == "/v1/registry/serve/stream":
                self._stream_registry_serve(token, body)
                return
            if self.path == "/v1/registry/serve":
                self._json(200, handle_registry_serve(token, body))
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
            if self.path == "/v1/services/rollback":
                self._json(200, handle_service_rollback(token, body))
                return
            if self.path == "/v1/services/history":
                self._json(200, handle_service_history(token, body))
                return
            if self.path == "/v1/applications/restart":
                self._json(200, handle_application_restart(token, body))
                return
            if self.path == "/v1/dashboard/pull-diagnostics":
                service = str(body.get("service") or "")
                timeout = int(body.get("timeout") or 600)
                self._json(200, handle_service_pull_diagnostics(token, service, timeout=timeout))
                return
            if self.path == "/v1/dashboard/pull-diagnostics/stream":
                self._stream_pull_diagnostics(token, body)
                return
            if self.path == "/v1/certificates/retry":
                self._json(200, handle_certificate_retry(token, body))
                return
            if self.path == "/v1/fleet/update":
                self._json(200, handle_fleet_update(token, body))
                return
            if self.path == "/v1/secrets":
                self._json(200, handle_secret_set(token, body))
                return
            if self.path == "/v1/git-providers":
                self._json(200, handle_git_provider_set(token, body))
                return
            if self.path == "/v1/git-providers/remove":
                self._json(200, handle_git_provider_remove(token, body))
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
        except (BrokenPipeError, ConnectionResetError):
            return
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
        except (BrokenPipeError, ConnectionResetError):
            return
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
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream compose deployment internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_build_deploy(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_build_deploy(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream build deploy internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_build_run_retry(self, token: str, build_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_build_run_retry(token, build_id, progress=emit)
            emit({"status": "done", "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream build retry internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_registry_serve(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_registry_serve(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream registry serve internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_pull_diagnostics(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            service = str(body.get("service") or "")
            timeout = int(body.get("timeout") or 600)
            started = _start_service_pull_diagnostics(token, service, timeout=timeout)
            emit({"status": "start", **started})
            cursor = 0
            deadline = time.time() + int(started["timeout"]) + 45
            while time.time() < deadline:
                events, cursor, status, result, message = _agent_task_progress_snapshot(str(started["taskId"]), cursor)
                for event in events:
                    emit({"status": "progress", **event})
                if status == "succeeded":
                    emit({"status": "done", "result": _service_pull_diagnostics_result(started, result)})
                    return
                if status in {"failed", "timeout"}:
                    emit({"status": "fail", "message": message or f"agent task {status}"})
                    return
                time.sleep(1)
            emit({"status": "fail", "message": "Docker pull diagnostic timed out"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream pull diagnostics internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_service_logs(self, token: str, service: str, since: str, tail: int) -> None:
        state = load_state()
        require_token(state, token, token_type="deploy")
        service = service.strip()
        if not service:
            raise LumaError("service is required")
        tail = min(max(int(tail or 120), 1), 1000)
        config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
        config = load_config(config_path)
        _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
        lines = _nomad_log_lines(config, state, service, tail=tail)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"status": "start", "service": service}, separators=(",", ":")).encode("utf-8") + b"\n")
            for line in lines:
                self.wfile.write(json.dumps({"line": line, "ts": int(time.time())}, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

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


def _json_response(status: int, payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def _asgi_error(status: int, exc: Exception, *, code: str = "luma_error") -> JSONResponse:
    request_id = _request_id()
    if status >= 500:
        print(f"requestId={request_id} control API error: {exc}", file=sys.stderr, flush=True)
    return _json_response(status, _error_payload(code, str(exc), request_id=request_id))


async def _asgi_dashboard_asset(request: Request) -> Response:
    parsed_path = request.url.path
    if parsed_path == "/dashboard":
        return RedirectResponse("/dashboard/", status_code=308)
    try:
        body, content_type = await run_in_threadpool(_dashboard_asset, parsed_path)
        cache_control = "no-store"
        return Response(body, media_type=content_type, headers={"Cache-Control": cache_control})
    except (LumaError, OSError):
        return _json_response(404, {"error": "not found"})


async def _asgi_health(_: Request) -> JSONResponse:
    return _json_response(
        200,
        {
            "ok": True,
            "version": __version__,
            "nodeJoinModel": "region-first",
            "capabilities": ["node-region", "service-proxy", "dashboard", "service-remove", "node-agent-storage", "terminal"],
        },
    )


async def _asgi_authenticated_get(request: Request) -> Response:
    parsed_path = request.url.path
    try:
        token = bearer_token(request.headers)
        if parsed_path == "/v1/git-providers":
            return _json_response(200, await run_in_threadpool(handle_git_provider_list, token))
        git_repos_match = re.fullmatch(r"/v1/git-providers/([^/]+)/repositories", parsed_path)
        if git_repos_match:
            provider_id = urllib.parse.unquote(git_repos_match.group(1))
            return _json_response(200, await run_in_threadpool(handle_git_provider_repositories, token, provider_id))
        git_refs_match = re.fullmatch(r"/v1/git-providers/([^/]+)/repositories/([^/]+)/([^/]+)/refs", parsed_path)
        if git_refs_match:
            provider_id = urllib.parse.unquote(git_refs_match.group(1))
            repository = f"{urllib.parse.unquote(git_refs_match.group(2))}/{urllib.parse.unquote(git_refs_match.group(3))}"
            return _json_response(200, await run_in_threadpool(handle_git_provider_refs, token, provider_id, repository))
        if parsed_path == "/v1/registries":
            return _json_response(200, await run_in_threadpool(handle_registry_list, token))
        if parsed_path == "/v1/secrets":
            return _json_response(200, await run_in_threadpool(handle_secret_list, token))
        if parsed_path == "/v1/storage":
            return _json_response(200, await run_in_threadpool(handle_storage_list, token))
        if parsed_path == "/v1/status":
            return _json_response(200, await run_in_threadpool(handle_control_status, token))
        if parsed_path == "/v1/builds":
            return _json_response(200, await run_in_threadpool(handle_build_run_list, token))
        build_match = re.fullmatch(r"/v1/builds/([^/]+)", parsed_path)
        if build_match:
            return _json_response(200, await run_in_threadpool(handle_build_run_get, token, urllib.parse.unquote(build_match.group(1))))
        if parsed_path == "/v1/dashboard":
            return _json_response(200, await run_in_threadpool(handle_dashboard, token))
        if parsed_path == "/v1/dashboard/logs":
            service = str(request.query_params.get("service") or "")
            since = str(request.query_params.get("since") or "")
            download = str(request.query_params.get("download") or "").lower() in {"1", "true", "yes"}
            try:
                tail = int(str(request.query_params.get("tail") or "120") or "120")
            except ValueError as exc:
                raise LumaError("tail must be a number") from exc
            logs = await run_in_threadpool(handle_dashboard_logs, token, service, tail=tail, since=since)
            if download:
                filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", service).strip("-") or "service"
                return PlainTextResponse(
                    "\n".join(str(line) for line in logs.get("logs") or []) + "\n",
                    headers={"Content-Disposition": f"attachment; filename={filename}.log", "Cache-Control": "no-store"},
                )
            return _json_response(200, logs)
        if parsed_path == "/v1/dashboard/metrics/history":
            kind = str(request.query_params.get("kind") or "node")
            name = str(request.query_params.get("name") or "")
            try:
                window = int(str(request.query_params.get("window") or "3600") or "3600")
            except ValueError as exc:
                raise LumaError("window must be a number") from exc
            return _json_response(200, await run_in_threadpool(handle_metrics_history, token, kind, name, window=window))
        if parsed_path == "/v1/dashboard/logs/stream":
            service = str(request.query_params.get("service") or "")
            since = str(request.query_params.get("since") or "")
            try:
                tail = int(str(request.query_params.get("tail") or "120") or "120")
            except ValueError as exc:
                raise LumaError("tail must be a number") from exc
            return await _asgi_stream_service_logs(token, service, since, tail)
        config_match = re.fullmatch(r"/v1/deployments/([^/]+)/config", parsed_path)
        if config_match:
            name = urllib.parse.unquote(config_match.group(1))
            return _json_response(200, await run_in_threadpool(handle_deployment_config, token, name))
    except LumaError as exc:
        code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
        return _asgi_error(code, exc)
    return _json_response(404, _error_payload("not_found", "not found", request_id=_request_id()))


async def _asgi_authenticated_post(request: Request) -> Response:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise LumaError("request body must be a JSON object")
        token = bearer_token(request.headers)
        path = request.url.path
        routes: dict[str, Callable[..., Dict[str, Any]]] = {
            "/v1/auth/login/verify": handle_login_verify,
            "/v1/nodes/register": handle_node_register,
            "/v1/nodes/label": handle_node_label,
            "/v1/nodes/nomad-join": handle_node_nomad_join,
            "/v1/nodes/agent-token": handle_node_agent_token,
            "/v1/node-agent/lease": handle_node_agent_lease,
            "/v1/node-agent/heartbeat": handle_node_agent_heartbeat,
            "/v1/node-agent/tasks/complete": handle_node_agent_complete,
            "/v1/node-agent/tasks/progress": handle_node_agent_progress,
            "/v1/nodes/unregister": handle_node_unregister,
            "/v1/deployments": handle_deployment,
            "/v1/deployments/preview": handle_deployment_preview,
            "/v1/compose-deployments": handle_compose_deployment,
            "/v1/compose-deployments/preview": handle_compose_deployment_preview,
            "/v1/builds": handle_build_deploy,
            "/v1/registry/serve": handle_registry_serve,
            "/v1/storage/apply": handle_storage_apply,
            "/v1/storage": handle_storage_set,
            "/v1/storage/remove": handle_storage_remove,
            "/v1/services/remove": handle_service_remove,
            "/v1/services/rollback": handle_service_rollback,
            "/v1/services/history": handle_service_history,
            "/v1/applications/restart": handle_application_restart,
            "/v1/certificates/retry": handle_certificate_retry,
            "/v1/fleet/update": handle_fleet_update,
            "/v1/secrets": handle_secret_set,
            "/v1/git-providers": handle_git_provider_set,
            "/v1/git-providers/remove": handle_git_provider_remove,
            "/v1/registries": handle_registry_set,
            "/v1/registries/remove": handle_registry_remove,
        }
        if path == "/v1/deployments/stream":
            return _asgi_stream_deployment(token, body, compose=False)
        if path == "/v1/compose-deployments/stream":
            return _asgi_stream_deployment(token, body, compose=True)
        build_retry_match = re.fullmatch(r"/v1/builds/([^/]+)/retry", path)
        if build_retry_match:
            return _json_response(200, await run_in_threadpool(handle_build_run_retry, token, urllib.parse.unquote(build_retry_match.group(1))))
        build_retry_stream_match = re.fullmatch(r"/v1/builds/([^/]+)/retry/stream", path)
        if build_retry_stream_match:
            return _asgi_stream_build_run_retry(token, urllib.parse.unquote(build_retry_stream_match.group(1)))
        if path == "/v1/builds/config":
            return _json_response(200, await run_in_threadpool(handle_build_config_set, token, body))
        if path == "/v1/builds/stream":
            return _asgi_stream_build_deploy(token, body)
        if path == "/v1/registry/serve/stream":
            return _asgi_stream_registry_serve(token, body)
        if path == "/v1/dashboard/pull-diagnostics/stream":
            return _asgi_stream_pull_diagnostics(token, body)
        if path == "/v1/auth/login/verify":
            return _json_response(200, await run_in_threadpool(handle_login_verify, token))
        if path == "/v1/dashboard/pull-diagnostics":
            service = str(body.get("service") or "")
            timeout = int(body.get("timeout") or 600)
            return _json_response(200, await run_in_threadpool(handle_service_pull_diagnostics, token, service, timeout=timeout))
        handler = routes.get(path)
        if handler:
            return _json_response(200, await run_in_threadpool(handler, token, body))
        return _json_response(404, _error_payload("not_found", "not found", request_id=_request_id()))
    except LumaError as exc:
        code = 401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
        return _asgi_error(code, exc)
    except Exception as exc:
        return _asgi_error(500, exc, code="internal_error")


def _asgi_stream_deployment(token: str, body: Dict[str, Any], *, compose: bool) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        def run() -> None:
            try:
                handler = handle_compose_deployment if compose else handle_deployment
                result = handler(token, body, progress=emit)
                emit({"status": "done", "result": result})
            except LumaError as exc:
                emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
            except Exception as exc:
                request_id = _request_id()
                print(f"requestId={request_id} stream deployment internal error: {exc}", file=sys.stderr, flush=True)
                emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()
        while True:
            event = await queue.get()
            if event is None:
                break
            yield json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _asgi_stream_build_deploy(token: str, body: Dict[str, Any]) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        def run() -> None:
            try:
                result = handle_build_deploy(token, body, progress=emit)
                emit({"status": "done", "result": result})
            except LumaError as exc:
                emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
            except Exception as exc:
                request_id = _request_id()
                print(f"requestId={request_id} stream build deploy internal error: {exc}", file=sys.stderr, flush=True)
                emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()
        while True:
            event = await queue.get()
            if event is None:
                break
            yield json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _asgi_stream_build_run_retry(token: str, build_id: str) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        def run() -> None:
            try:
                result = handle_build_run_retry(token, build_id, progress=emit)
                emit({"status": "done", "result": result})
            except LumaError as exc:
                emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
            except Exception as exc:
                request_id = _request_id()
                print(f"requestId={request_id} stream build retry internal error: {exc}", file=sys.stderr, flush=True)
                emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()
        while True:
            event = await queue.get()
            if event is None:
                break
            yield json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _asgi_stream_registry_serve(token: str, body: Dict[str, Any]) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        def run() -> None:
            try:
                result = handle_registry_serve(token, body, progress=emit)
                emit({"status": "done", "result": result})
            except LumaError as exc:
                emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
            except Exception as exc:
                request_id = _request_id()
                print(f"requestId={request_id} stream registry serve internal error: {exc}", file=sys.stderr, flush=True)
                emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()
        while True:
            event = await queue.get()
            if event is None:
                break
            yield json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _asgi_stream_pull_diagnostics(token: str, body: Dict[str, Any]) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        try:
            service = str(body.get("service") or "")
            timeout = int(body.get("timeout") or 600)
            started = await run_in_threadpool(_start_service_pull_diagnostics, token, service, timeout=timeout)
            yield json.dumps({"status": "start", **started}, separators=(",", ":")).encode("utf-8") + b"\n"
            cursor = 0
            deadline = time.time() + int(started["timeout"]) + 45
            while time.time() < deadline:
                events, cursor, status, result, message = await run_in_threadpool(_agent_task_progress_snapshot, str(started["taskId"]), cursor)
                for event in events:
                    yield json.dumps({"status": "progress", **event}, separators=(",", ":")).encode("utf-8") + b"\n"
                if status == "succeeded":
                    final = _service_pull_diagnostics_result(started, result)
                    yield json.dumps({"status": "done", "result": final}, separators=(",", ":")).encode("utf-8") + b"\n"
                    return
                if status in {"failed", "timeout", "missing"}:
                    yield json.dumps({"status": "fail", "message": message or f"agent task {status}"}, separators=(",", ":")).encode("utf-8") + b"\n"
                    return
                await asyncio.sleep(1)
            yield json.dumps({"status": "fail", "message": "Docker pull diagnostic timed out"}, separators=(",", ":")).encode("utf-8") + b"\n"
        except LumaError as exc:
            yield json.dumps({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)}, separators=(",", ":")).encode("utf-8") + b"\n"
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream pull diagnostics internal error: {exc}", file=sys.stderr, flush=True)
            yield json.dumps({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)}, separators=(",", ":")).encode("utf-8") + b"\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _asgi_stream_service_logs(token: str, service: str, since: str, tail: int) -> StreamingResponse:
    state = load_state()
    require_token(state, token, token_type="deploy")
    service = service.strip()
    if not service:
        raise LumaError("service is required")
    tail = min(max(int(tail or 120), 1), 1000)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))

    async def generate_nomad() -> AsyncIterator[bytes]:
        yield json.dumps({"status": "start", "service": service}, separators=(",", ":")).encode("utf-8") + b"\n"
        lines = await run_in_threadpool(_nomad_log_lines, config, state, service, tail=tail)
        now = int(time.time())
        for line in lines:
            yield json.dumps({"line": line, "ts": now}, separators=(",", ":")).encode("utf-8") + b"\n"

    return StreamingResponse(generate_nomad(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _browser_terminal_ws(websocket: WebSocket) -> None:
    await TERMINAL_BROKER.connect_browser(websocket)


async def _agent_terminal_ws(websocket: WebSocket) -> None:
    await TERMINAL_BROKER.connect_agent(websocket)


def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/dashboard", _asgi_dashboard_asset, methods=["GET"]),
            Route("/dashboard/{path:path}", _asgi_dashboard_asset, methods=["GET"]),
            Route("/v1/health", _asgi_health, methods=["GET"]),
            Route("/v1/{path:path}", _asgi_authenticated_get, methods=["GET"]),
            Route("/v1/{path:path}", _asgi_authenticated_post, methods=["POST"]),
            WebSocketRoute("/v1/terminal/browser", _browser_terminal_ws),
            WebSocketRoute("/v1/terminal/agent", _agent_terminal_ws),
        ]
    )


def serve(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level=os.environ.get("LUMA_CONTROL_LOG_LEVEL", "info"))


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
