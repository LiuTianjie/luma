from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import copy
import errno
import functools
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
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
from ..artifact_leases import (
    ArtifactLeaseBinding,
    ArtifactLeaseManager,
    ArtifactLeaseRecord,
    MAX_ARTIFACT_BYTES,
)
from ..builder_tasks import (
    BUILDER_TASK_SCHEMA_VERSION,
    builder_action_for_kind,
    builder_plan_content_digest,
    builder_plan_signature_payload,
    builder_registry_repository,
    builder_task_request_hash,
    parse_external_image_reference,
    sanitize_builder_task_progress_event,
    sanitize_builder_task_result,
    validate_builder_task_request,
)
from ..cloudflare import delete_dns, sync_dns
from ..credential_broker import (
    CredentialLeaseBinding,
    ObjectSourceLeaseBinding,
    RedeemedCredential,
    RedeemedObjectSource,
    redeem_builder_credential,
    redeem_builder_object_source,
)
from ..compose import (
    ComposeDeploymentSpec,
    ComposeServiceSpec,
    ComposeVolumeSpec,
    DEFAULT_NFS_MOUNT_OPTIONS,
    StorageClassSpec,
    compose_public_services,
    compose_route_path,
    compose_stack_path,
    load_compose_deployment,
    render_storage_class_volume,
    render_compose_routes,
    resolve_storage_mounts,
    storage_summary,
)
from ..config import LumaConfig, load_config
from ..errors import LumaError
from ..io import load_yaml
from ..local import LocalExecutor
from ..nomad_api import NomadApi, NomadRolloutError, deploy_to_nomad, remove_from_nomad, revert_job, job_versions, nomad_addr, nomad_status_summary, nomad_services_summary
from ..nomad_render import EDGE_EXPOSURES, render_nomad_job, render_compose_job
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
from ..repo_paths import normalize_repo_relative_path
from ..service import TCP_RELAY_RESERVED_PORTS, VALID_REGIONS, ServiceSpec, load_service, slugify, tcp_entrypoint_name
from ..lae_runtime import (
    MAX_RUNTIME_REQUEST_BYTES,
    RUNTIME_AUDIENCE,
    RUNTIME_SECRETS,
    SCHEMA_VERSION as LAE_RUNTIME_SCHEMA_VERSION,
    SCOPE_DEPLOYMENTS_READ,
    SCOPE_DEPLOYMENTS_WRITE,
    SCOPE_LOGS_READ,
    SCOPE_METRICS_READ,
    SCOPE_SECRETS_ISSUE,
    SCOPE_VOLUMES_PREPARE,
    LumaRuntimeError,
    RuntimeBinding,
    canonical_hash as _lae_runtime_hash,
    conflict as _lae_runtime_conflict,
    deployment_failed as _lae_runtime_deployment_failed,
    forbidden as _lae_runtime_forbidden,
    invalid as _lae_runtime_invalid,
    normalize_idempotency_key as _normalize_lae_runtime_idempotency_key,
    not_found as _lae_runtime_not_found,
    unauthorized as _lae_runtime_unauthorized,
    unavailable as _lae_runtime_unavailable,
    validate_deploy_body as _validate_lae_runtime_deploy_body,
    validate_lifecycle_body as _validate_lae_runtime_lifecycle_body,
    validate_secret_issue_body as _validate_lae_runtime_secret_issue_body,
    validate_volume_prepare_body as _validate_lae_runtime_volume_prepare_body,
)
from ..lae_placement import (
    PlacementDecision,
    PlacementFailure,
    REASON_NO_CAPACITY as LAE_PLACEMENT_NO_CAPACITY,
    REASON_UNAVAILABLE as LAE_PLACEMENT_UNAVAILABLE,
    REASON_VOLUME_INCOMPATIBLE as LAE_PLACEMENT_VOLUME_INCOMPATIBLE,
    plan_lae_placement,
    validate_nomad_plan as _validate_lae_nomad_plan,
)
from ..lae_admin_proxy import (
    LaeAdminProxyError,
    fetch_lae_admin_resource,
    load_lae_admin_proxy_config,
)
from .. import __version__
from .metrics import load_history, record_samples, sustained_breach
from .state import (
    init_state,
    load_state,
    mutate_state,
    mutate_state_if_changed,
    require_token,
    save_state,
    state_dir,
    state_path,
)
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
# Queue time is capacity waiting, not task execution time.  Keep a separate
# bound so a serial node can finish its current task without stealing the next
# task's execution budget.
AGENT_TASK_QUEUE_TIMEOUT_SECONDS = int(
    os.environ.get("LUMA_NODE_AGENT_TASK_QUEUE_TIMEOUT_SECONDS", str(60 * 60))
)
# A lease is persisted before optional credential redemption finishes.  A
# concurrent long-poller must not classify that just-leased task as orphaned
# while Control is still handing it to the agent.
AGENT_TASK_HANDOFF_GRACE_SECONDS = int(
    os.environ.get("LUMA_NODE_AGENT_TASK_HANDOFF_GRACE_SECONDS", "60")
)
# How long a finished (succeeded/failed/timeout) agent task is kept in
# control.json before it is garbage-collected. Without this, agentTasks grows
# without bound — every remote-node deploy adds an entry that is never deleted,
# and the whole file is re-serialized + fsynced on every heartbeat.
AGENT_TASK_RETENTION_SECONDS = int(os.environ.get("LUMA_NODE_AGENT_TASK_RETENTION_SECONDS", str(15 * 60)))
AGENT_TASK_PROGRESS_LIMIT = int(os.environ.get("LUMA_NODE_AGENT_TASK_PROGRESS_LIMIT", "300"))
BUILDER_TASK_RETENTION_SECONDS = int(os.environ.get("LUMA_BUILDER_TASK_RETENTION_SECONDS", str(7 * 24 * 3600)))
BUILDER_TASK_IDEMPOTENCY_SECONDS = int(os.environ.get("LUMA_BUILDER_TASK_IDEMPOTENCY_SECONDS", str(24 * 3600)))
BUILDER_TASK_EVENT_LIMIT = int(os.environ.get("LUMA_BUILDER_TASK_EVENT_LIMIT", "1000"))
BUILD_RUN_EVENT_LIMIT = int(os.environ.get("LUMA_BUILD_RUN_EVENT_LIMIT", "300"))
BUILD_RUN_MESSAGE_LIMIT = int(os.environ.get("LUMA_BUILD_RUN_MESSAGE_LIMIT", "4000"))
BUILD_RUN_SUMMARY_MESSAGE_LIMIT = int(os.environ.get("LUMA_BUILD_RUN_SUMMARY_MESSAGE_LIMIT", "500"))
LAE_RUNTIME_RECORD_RETENTION_SECONDS = int(
    os.environ.get("LUMA_LAE_RUNTIME_RECORD_RETENTION_SECONDS", str(30 * 24 * 3600))
)
LAE_RUNTIME_IDEMPOTENCY_SECONDS = int(
    os.environ.get("LUMA_LAE_RUNTIME_IDEMPOTENCY_SECONDS", str(7 * 24 * 3600))
)
TERMINAL_SESSION_LIMIT_PER_NODE = int(os.environ.get("LUMA_TERMINAL_SESSION_LIMIT_PER_NODE", "2"))
TERMINAL_IDLE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_TERMINAL_IDLE_TIMEOUT_SECONDS", "1800"))
# Sustained-breach alerting: a metric must stay above the threshold for the
# whole window before it becomes an issue, so a momentary spike does not page.
ALERT_SUSTAINED_SECONDS = int(os.environ.get("LUMA_ALERT_SUSTAINED_SECONDS", "300"))
ALERT_NODE_MEMORY_PERCENT = float(os.environ.get("LUMA_ALERT_NODE_MEMORY_PERCENT", "85"))
ALERT_NODE_CPU_PERCENT = float(os.environ.get("LUMA_ALERT_NODE_CPU_PERCENT", "90"))
TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS", "300"))
DEFAULT_BUILD_NODE_NAME = "builder"
RECOVERABLE_ROUTE_HTTP_STATUSES = {502, 503, 504}
PUBLIC_ROUTE_SETTLE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_PUBLIC_ROUTE_SETTLE_TIMEOUT_SECONDS", "120"))
PUBLIC_ROUTE_SETTLE_INTERVAL_SECONDS = float(os.environ.get("LUMA_PUBLIC_ROUTE_SETTLE_INTERVAL_SECONDS", "2"))
TRAEFIK_PROVIDER_SETTLE_TIMEOUT_SECONDS = int(os.environ.get("LUMA_TRAEFIK_PROVIDER_SETTLE_TIMEOUT_SECONDS", "20"))
DOCKER_RESTART_RECOVERY_TIMEOUT_SECONDS = int(os.environ.get("LUMA_DOCKER_RESTART_RECOVERY_TIMEOUT_SECONDS", "180"))
ARTIFACT_DOWNLOADS = ArtifactLeaseManager(
    temporary_root=(
        Path(os.environ["LUMA_ARTIFACT_RENDEZVOUS_ROOT"])
        if os.environ.get("LUMA_ARTIFACT_RENDEZVOUS_ROOT")
        else None
    )
)

# Serializes every control-plane deployment path, including the dedicated LAE
# runtime API.  A single lock prevents a normal management deploy and an LAE
# deploy from racing on job slugs, domains, routes, storage baselines, or the
# generated Nomad artifact.
_DEPLOY_LOCK = threading.RLock()
_FLEET_UPDATE_LOCK = threading.RLock()
_FLEET_UPDATE_THREADS: dict[str, threading.Thread] = {}
_CONTROL_IMAGE_PREPARE_LOCK = threading.RLock()
_CONTROL_IMAGE_PREPARE_THREADS: dict[str, threading.Thread] = {}
_LAE_RUNTIME_DEPLOY_THREAD_LOCK = threading.RLock()
_LAE_RUNTIME_DEPLOY_THREADS: dict[str, threading.Thread] = {}
# Repository Import is a request-owned workflow rather than a resumable
# durable operation.  Persist its owning Control process so a replacement
# instance can close records whose request thread no longer exists.
_CONTROL_PROCESS_INSTANCE_ID = f"control-{secrets.token_hex(16)}"


def _serialize_deploy(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _DEPLOY_LOCK:
            return func(*args, **kwargs)

    return wrapper


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


def _builder_tasks(state: Dict[str, Any]) -> Dict[str, Any]:
    tasks = state.setdefault("builderTasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        state["builderTasks"] = tasks
    return tasks


def _builder_task_idempotency(state: Dict[str, Any]) -> Dict[str, Any]:
    records = state.setdefault("builderTaskIdempotency", {})
    if not isinstance(records, dict):
        records = {}
        state["builderTaskIdempotency"] = records
    return records


def _builder_source_snapshots(state: Dict[str, Any]) -> Dict[str, Any]:
    snapshots = state.setdefault("builderSourceSnapshots", {})
    if not isinstance(snapshots, dict):
        snapshots = {}
        state["builderSourceSnapshots"] = snapshots
    return snapshots


def _builder_source_snapshot_scope(
    principal_ref: str,
    tenant_ref: str,
    application_ref: str,
    snapshot_id: str,
) -> str:
    scope = {
        "principalRef": str(principal_ref),
        "tenantRef": str(tenant_ref),
        "applicationRef": str(application_ref),
        "sourceSnapshotId": str(snapshot_id),
    }
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _LaePrincipalFileError(Exception):
    pass


def _read_lae_private_file(path: Path, *, max_bytes: int) -> str:
    raw_path = str(path)
    if (
        not path.is_absolute()
        or not raw_path
        or len(raw_path) > 4096
        or any(character in raw_path for character in ("\0", "\n", "\r"))
    ):
        raise _LaePrincipalFileError()
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or not metadata.st_mode & stat.S_IRUSR
            or not 1 <= metadata.st_size <= max_bytes
        ):
            raise _LaePrincipalFileError()
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = -1
            value = source.read(max_bytes + 1)
    except (OSError, UnicodeError, _LaePrincipalFileError):
        raise _LaePrincipalFileError() from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(value.encode("utf-8")) > max_bytes:
        raise _LaePrincipalFileError()
    return value


def _load_lae_principal_file(env_name: str) -> tuple[Path, Dict[str, Any]]:
    raw_path = str(os.environ.get(env_name) or "").strip()
    if not raw_path:
        raise _LaePrincipalFileError()
    path = Path(raw_path)
    try:
        decoded = json.loads(_read_lae_private_file(path, max_bytes=128 * 1024))
    except (ValueError, _LaePrincipalFileError):
        raise _LaePrincipalFileError() from None
    if not isinstance(decoded, dict) or not decoded:
        raise _LaePrincipalFileError()
    return path, decoded


def _read_lae_principal_token(config_path: Path, token_file: Any) -> str:
    raw = str(token_file or "")
    if (
        not raw
        or raw != raw.strip()
        or any(character in raw for character in ("\0", "\n", "\r"))
    ):
        raise _LaePrincipalFileError()
    requested = Path(raw)
    if requested.is_absolute():
        selected = requested
    elif len(requested.parts) == 1 and requested.name not in {"", ".", ".."}:
        selected = config_path.parent / requested
    else:
        raise _LaePrincipalFileError()
    if (
        selected.parent != config_path.parent
        or selected.name in {"", ".", ".."}
    ):
        raise _LaePrincipalFileError()
    token = _read_lae_private_file(selected, max_bytes=4096).strip()
    if (
        not 16 <= len(token) <= 512
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise _LaePrincipalFileError()
    return token


def _lae_service_principals() -> list[Dict[str, Any]]:
    raw = str(os.environ.get("LUMA_LAE_SERVICE_PRINCIPALS_JSON") or "").strip()
    file_path = str(
        os.environ.get("LUMA_LAE_SERVICE_PRINCIPALS_FILE") or ""
    ).strip()
    legacy_token = str(os.environ.get("LUMA_LAE_SERVICE_TOKEN") or "").strip()
    principals: list[Dict[str, Any]] = []
    if file_path:
        if raw or legacy_token:
            raise LumaError("LAE service principal configuration is invalid")
        try:
            config_path, configured = _load_lae_principal_file(
                "LUMA_LAE_SERVICE_PRINCIPALS_FILE"
            )
        except _LaePrincipalFileError:
            raise LumaError(
                "LAE service principal configuration is invalid"
            ) from None
        allowed_fields = {"tokenFile", "tenantRefs", "applicationRefs"}
        for principal_ref, value in configured.items():
            if (
                not isinstance(value, dict)
                or set(value) != allowed_fields
            ):
                raise LumaError(
                    "LAE service principal configuration is invalid"
                )
            principal_id = str(principal_ref or "").strip()
            tenant_refs = value.get("tenantRefs")
            application_refs = value.get("applicationRefs")
            try:
                principal_token = _read_lae_principal_token(
                    config_path, value.get("tokenFile")
                )
            except _LaePrincipalFileError:
                raise LumaError(
                    "LAE service principal configuration is invalid"
                ) from None
            if (
                not principal_id
                or not isinstance(tenant_refs, list)
                or not tenant_refs
                or not all(
                    isinstance(item, str) and item.strip()
                    for item in tenant_refs
                )
                or not isinstance(application_refs, list)
                or not application_refs
                or not all(
                    isinstance(item, str) and item.strip()
                    for item in application_refs
                )
            ):
                raise LumaError(
                    "LAE service principal configuration is invalid"
                )
            principals.append(
                {
                    "id": principal_id,
                    "token": principal_token,
                    "tenantRefs": [str(item).strip() for item in tenant_refs],
                    "applicationRefs": [
                        str(item).strip() for item in application_refs
                    ],
                }
            )
    elif raw:
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LumaError("LAE service principal configuration is invalid") from exc
        if not isinstance(configured, dict):
            raise LumaError("LAE service principal configuration is invalid")
        for principal_ref, value in configured.items():
            if not isinstance(value, dict):
                raise LumaError("LAE service principal configuration is invalid")
            principal_id = str(principal_ref or "").strip()
            principal_token = str(value.get("token") or "").strip()
            tenant_refs = value.get("tenantRefs", ["*"])
            application_refs = value.get("applicationRefs", ["*"])
            if (
                not principal_id
                or not principal_token
                or not isinstance(tenant_refs, list)
                or not tenant_refs
                or not all(isinstance(item, str) and item.strip() for item in tenant_refs)
                or not isinstance(application_refs, list)
                or not application_refs
                or not all(isinstance(item, str) and item.strip() for item in application_refs)
            ):
                raise LumaError("LAE service principal configuration is invalid")
            principals.append(
                {
                    "id": principal_id,
                    "token": principal_token,
                    "tenantRefs": [str(item).strip() for item in tenant_refs],
                    "applicationRefs": [str(item).strip() for item in application_refs],
                }
            )
    if legacy_token:
        principals.append(
            {
                "id": str(os.environ.get("LUMA_LAE_SERVICE_PRINCIPAL_REF") or "lae-service").strip(),
                "token": legacy_token,
                "tenantRefs": ["*"],
                "applicationRefs": ["*"],
            }
        )
    token_fingerprints = {
        hashlib.sha256(str(item["token"]).encode("utf-8")).digest()
        for item in principals
    }
    if len(token_fingerprints) != len(principals):
        raise LumaError("LAE service principal configuration is invalid")
    return principals


def _require_lae_service_principal(state: Dict[str, Any], token: str) -> Dict[str, Any]:
    management_token = str(state.get("deployToken") or "")
    if management_token and token and secrets.compare_digest(management_token, token):
        raise LumaError("unauthorized")
    supplied = str(token or "")
    for principal in _lae_service_principals():
        expected = str(principal.get("token") or "")
        if expected and supplied and secrets.compare_digest(expected, supplied):
            return {key: value for key, value in principal.items() if key != "token"}
    raise LumaError("unauthorized")


def _lae_runtime_service_principals() -> list[Dict[str, Any]]:
    """Load the dedicated LAE runtime identities.

    Runtime identities deliberately live in a different environment variable
    from Builder identities.  A token copied from the management or Builder
    plane is rejected even if its text is accidentally duplicated here.
    """

    raw = str(
        os.environ.get("LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_JSON") or ""
    ).strip()
    file_path = str(
        os.environ.get("LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE") or ""
    ).strip()
    if not raw and not file_path:
        return []
    config_path: Path | None = None
    if file_path:
        if raw:
            raise _lae_runtime_unavailable(
                "LAE runtime service principal configuration is invalid"
            )
        try:
            config_path, configured = _load_lae_principal_file(
                "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE"
            )
        except _LaePrincipalFileError:
            raise _lae_runtime_unavailable(
                "LAE runtime service principal configuration is invalid"
            ) from None
    else:
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _lae_runtime_unavailable(
                "LAE runtime service principal configuration is invalid"
            ) from exc
    if not isinstance(configured, dict) or not configured:
        raise _lae_runtime_unavailable(
            "LAE runtime service principal configuration is invalid"
        )
    token_field = "tokenFile" if config_path is not None else "token"
    allowed_fields = {
        token_field,
        "tenantRefs",
        "applicationRefs",
        "builderPrincipalRefs",
        "scopes",
    }
    allowed_scopes = {
        SCOPE_VOLUMES_PREPARE,
        SCOPE_DEPLOYMENTS_WRITE,
        SCOPE_DEPLOYMENTS_READ,
        SCOPE_LOGS_READ,
        SCOPE_METRICS_READ,
        SCOPE_SECRETS_ISSUE,
    }
    principals: list[Dict[str, Any]] = []
    for raw_id, raw_value in configured.items():
        if (
            not isinstance(raw_id, str)
            or not raw_id.strip()
            or not isinstance(raw_value, dict)
            or set(raw_value) - allowed_fields
            or token_field not in raw_value
        ):
            raise _lae_runtime_unavailable(
                "LAE runtime service principal configuration is invalid"
            )
        if config_path is not None:
            try:
                token = _read_lae_principal_token(
                    config_path, raw_value.get("tokenFile")
                )
            except _LaePrincipalFileError:
                raise _lae_runtime_unavailable(
                    "LAE runtime service principal configuration is invalid"
                ) from None
        else:
            token = raw_value.get("token")
        tenant_refs = raw_value.get("tenantRefs", ["*"])
        application_refs = raw_value.get("applicationRefs", ["*"])
        builder_principal_refs = raw_value.get("builderPrincipalRefs")
        scopes = raw_value.get("scopes")
        if (
            not isinstance(token, str)
            or len(token) < 16
            or len(token) > 512
            or any(not 33 <= ord(character) <= 126 for character in token)
            or not isinstance(tenant_refs, list)
            or not tenant_refs
            or not all(
                isinstance(item, str) and item.strip() for item in tenant_refs
            )
            or not isinstance(application_refs, list)
            or not application_refs
            or not all(
                isinstance(item, str) and item.strip()
                for item in application_refs
            )
            or not isinstance(builder_principal_refs, list)
            or not builder_principal_refs
            or not all(
                isinstance(item, str) and item.strip()
                for item in builder_principal_refs
            )
            or not isinstance(scopes, list)
            or not scopes
            or not all(
                isinstance(item, str) and item in allowed_scopes
                for item in scopes
            )
            or len(set(scopes)) != len(scopes)
        ):
            raise _lae_runtime_unavailable(
                "LAE runtime service principal configuration is invalid"
            )
        principals.append(
            {
                "id": raw_id.strip(),
                "token": token,
                "tenantRefs": [str(item).strip() for item in tenant_refs],
                "applicationRefs": [
                    str(item).strip() for item in application_refs
                ],
                "builderPrincipalRefs": [
                    str(item).strip() for item in builder_principal_refs
                ],
                "scopes": list(scopes),
            }
        )
    token_fingerprints = {
        hashlib.sha256(str(item["token"]).encode("utf-8")).digest()
        for item in principals
    }
    if len(token_fingerprints) != len(principals):
        raise _lae_runtime_unavailable(
            "LAE runtime service principal configuration is invalid"
        )
    return principals


def _require_lae_runtime_principal(
    state: Dict[str, Any],
    token: str,
    *,
    audience: str,
    scope: str,
    binding: RuntimeBinding,
) -> Dict[str, Any]:
    if audience != RUNTIME_AUDIENCE:
        raise _lae_runtime_unauthorized()
    supplied = str(token or "")
    if not supplied:
        raise _lae_runtime_unauthorized()
    management_token = str(state.get("deployToken") or "")
    if management_token and secrets.compare_digest(management_token, supplied):
        raise _lae_runtime_unauthorized()
    # Builder and runtime tokens must be independently rotatable.  Refuse a
    # duplicate rather than letting configuration blur the two audiences.
    for builder_principal in _lae_service_principals():
        builder_token = str(builder_principal.get("token") or "")
        if builder_token and secrets.compare_digest(builder_token, supplied):
            raise _lae_runtime_unauthorized()
    principal: Dict[str, Any] | None = None
    for candidate in _lae_runtime_service_principals():
        expected = str(candidate.get("token") or "")
        if expected and secrets.compare_digest(expected, supplied):
            principal = candidate
            break
    if principal is None:
        raise _lae_runtime_unauthorized()
    if scope not in set(str(item) for item in principal.get("scopes") or []):
        raise _lae_runtime_forbidden()
    tenant_refs = set(str(item) for item in principal.get("tenantRefs") or [])
    application_refs = set(
        str(item) for item in principal.get("applicationRefs") or []
    )
    if (
        "*" not in tenant_refs
        and binding.tenant_ref not in tenant_refs
        or "*" not in application_refs
        and binding.application_ref not in application_refs
    ):
        raise _lae_runtime_forbidden()
    return {key: value for key, value in principal.items() if key != "token"}


def _authorize_lae_builder_scope(principal: Dict[str, Any], request: Dict[str, Any]) -> None:
    tenant_ref = str(request.get("tenantRef") or "")
    application_ref = str(request.get("applicationRef") or "")
    tenant_refs = set(str(item) for item in principal.get("tenantRefs") or [])
    application_refs = set(str(item) for item in principal.get("applicationRefs") or [])
    if "*" not in tenant_refs and tenant_ref not in tenant_refs:
        raise LumaError("unauthorized")
    if "*" not in application_refs and application_ref not in application_refs:
        raise LumaError("unauthorized")


def _require_builder_task_owner(task: Dict[str, Any], principal: Dict[str, Any], task_id: str) -> None:
    if str(task.get("principalRef") or "") != str(principal.get("id") or ""):
        # Do not reveal task existence across service principals.
        raise LumaError(f"builder task not found: {task_id}")


def _builder_task_idempotency_scope(
    principal: Dict[str, Any],
    request: Dict[str, Any],
    idempotency_key: str,
) -> str:
    scope = {
        "principalRef": str(principal.get("id") or ""),
        "route": "POST /v1/builder/tasks",
        "tenantRef": str(request.get("tenantRef") or ""),
        "applicationRef": str(request.get("applicationRef") or ""),
        "idempotencyKey": idempotency_key,
    }
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_builder_agent_image_allowlist(request: Dict[str, Any]) -> None:
    if str(request.get("kind") or "") != "analyze-source":
        return
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    supplied = str(payload.get("agentImageDigest") or "")
    expected = str(os.environ.get("LUMA_BUILDER_ANALYZE_IMAGE_DIGEST") or "").strip()
    if not expected:
        raise LumaError("builder analyzer image allowlist is unavailable")
    if not secrets.compare_digest(expected, supplied):
        raise LumaError("agentImageDigest is not allowlisted by Luma Control")
    _lae_external_registry_allowlist()


def _lae_plan_signing_keys() -> Dict[str, bytes]:
    raw = str(os.environ.get("LUMA_LAE_PLAN_SIGNING_KEYS_JSON") or "").strip()
    file_path = str(
        os.environ.get("LUMA_LAE_PLAN_SIGNING_KEYS_FILE") or ""
    ).strip()
    if raw and file_path:
        raise LumaError("LAE plan signing key configuration is ambiguous")
    if not raw and not file_path:
        raise LumaError("LAE plan signing key configuration is unavailable")
    if file_path:
        try:
            _, configured = _load_lae_principal_file(
                "LUMA_LAE_PLAN_SIGNING_KEYS_FILE"
            )
        except _LaePrincipalFileError:
            raise LumaError(
                "LAE plan signing key configuration is invalid"
            ) from None
    else:
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LumaError("LAE plan signing key configuration is invalid") from exc
    if not isinstance(configured, dict) or not configured:
        raise LumaError("LAE plan signing key configuration is invalid")
    result: Dict[str, bytes] = {}
    for key_id, raw_secret in configured.items():
        if not isinstance(key_id, str) or not key_id.startswith("lae-plan-") or not isinstance(raw_secret, str):
            raise LumaError("LAE plan signing key configuration is invalid")
        secret_text = raw_secret.strip()
        try:
            secret = base64.b64decode(secret_text[7:], validate=True) if secret_text.startswith("base64:") else secret_text.encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise LumaError("LAE plan signing key configuration is invalid") from exc
        if len(secret) < 32:
            raise LumaError("LAE plan signing keys must contain at least 256 bits")
        result[key_id] = secret
    return result


def _verify_lae_plan_signature(request: Dict[str, Any]) -> None:
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    plan = payload.get("signedBuildPlan") if isinstance(payload.get("signedBuildPlan"), dict) else {}
    signature = plan.get("signature") if isinstance(plan.get("signature"), dict) else {}
    key_id = str(signature.get("keyId") or "")
    key = _lae_plan_signing_keys().get(key_id)
    if key is None:
        raise LumaError("signedBuildPlan signature key is not trusted")
    expected = base64.urlsafe_b64encode(
        hmac.new(key, builder_plan_signature_payload(request), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    supplied = str(signature.get("value") or "")
    if not supplied or not secrets.compare_digest(expected, supplied):
        raise LumaError("signedBuildPlan signature verification failed")


def _record_builder_source_snapshot(state: Dict[str, Any], task: Dict[str, Any], result: Dict[str, Any], *, now: int) -> None:
    snapshot_id = str(result.get("sourceSnapshotId") or "")
    record = {
        "id": snapshot_id,
        "digest": str(result.get("sourceSnapshotDigest") or ""),
        "sourceTreeDigest": str(result.get("sourceTreeDigest") or ""),
        "resolvedCommit": str(result.get("resolvedCommit") or ""),
        "buildPlanDigest": str(result.get("buildPlanDigest") or ""),
        "deploymentPlanDigest": str(result.get("deploymentPlanDigest") or ""),
        "evidenceDigest": str(result.get("evidenceDigest") or ""),
        "policyVersion": str(result.get("policyVersion") or ""),
        "agentImageDigest": str(result.get("agentImageDigest") or ""),
        "tenantRef": str(task.get("tenantRef") or ""),
        "applicationRef": str(task.get("applicationRef") or ""),
        "principalRef": str(task.get("principalRef") or ""),
        "builderTaskId": str(task.get("id") or ""),
        "createdAt": now,
    }
    snapshots = _builder_source_snapshots(state)
    snapshot_scope = _builder_source_snapshot_scope(
        record["principalRef"],
        record["tenantRef"],
        record["applicationRef"],
        snapshot_id,
    )
    existing = snapshots.get(snapshot_scope)
    if isinstance(existing, dict):
        immutable_fields = (
            "digest",
            "sourceTreeDigest",
            "resolvedCommit",
            "buildPlanDigest",
            "tenantRef",
            "applicationRef",
            "principalRef",
        )
        if any(str(existing.get(field) or "") != str(record.get(field) or "") for field in immutable_fields):
            raise LumaError("source snapshot id is already bound to different immutable content")
        return
    snapshots[snapshot_scope] = record


def _validate_bound_build_request(state: Dict[str, Any], request: Dict[str, Any], principal: Dict[str, Any]) -> None:
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    snapshot_id = str(payload.get("sourceSnapshotId") or "")
    snapshot_scope = _builder_source_snapshot_scope(
        str(principal.get("id") or ""),
        str(request.get("tenantRef") or ""),
        str(request.get("applicationRef") or ""),
        snapshot_id,
    )
    snapshot = _builder_source_snapshots(state).get(snapshot_scope)
    if not isinstance(snapshot, dict):
        raise LumaError("build-plan source snapshot is unknown or expired")
    plan = payload.get("signedBuildPlan") if isinstance(payload.get("signedBuildPlan"), dict) else {}
    bindings = {
        "digest": str(payload.get("sourceSnapshotDigest") or ""),
        "resolvedCommit": str(plan.get("resolvedCommit") or ""),
        "policyVersion": str(plan.get("policyVersion") or ""),
        "tenantRef": str(request.get("tenantRef") or ""),
        "applicationRef": str(request.get("applicationRef") or ""),
        "principalRef": str(principal.get("id") or ""),
    }
    for field, expected in bindings.items():
        if str(snapshot.get(field) or "") != expected:
            raise LumaError(f"build-plan source snapshot {field} binding does not match")
    if str(snapshot.get("buildPlanDigest") or "") != builder_plan_content_digest(request):
        raise LumaError("build-plan content does not match the analyzed snapshot plan")
    _verify_lae_plan_signature(request)


def _builder_build_registry_lease(
    state: Dict[str, Any],
    request: Dict[str, Any],
    principal: Dict[str, Any],
) -> Dict[str, Any]:
    """Derive an ephemeral, platform-owned registry target for build-plan.

    This value is created only for the node lease and is never copied into
    ``agentTasks``.  Staging can explicitly lease the platform registry's
    Basic credential to a trusted builder; the credential remains absent from
    durable task state and build results.  Anonymous registries remain an
    independently gated mode.
    """
    build_config = _build_config(state)
    pull_host_raw = str(build_config.get("registryHost") or "").strip()
    push_host_raw = str(build_config.get("pushHost") or "").strip()
    if not pull_host_raw or not push_host_raw:
        raise LumaError("LAE build registryHost and pushHost must be configured by Control")
    pull_host = normalize_registry_host(pull_host_raw)
    push_host = normalize_registry_host(push_host_raw)
    insecure_raw = str(os.environ.get("LUMA_LAE_BUILDER_REGISTRY_INSECURE") or "").strip()
    if insecure_raw not in {"0", "1"}:
        raise LumaError("LUMA_LAE_BUILDER_REGISTRY_INSECURE must be explicitly set to 0 or 1")
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    plan = payload.get("signedBuildPlan") if isinstance(payload.get("signedBuildPlan"), dict) else {}
    builds = plan.get("builds") if isinstance(plan.get("builds"), list) else []
    external_images = plan.get("externalImages") if isinstance(plan.get("externalImages"), list) else []
    principal_ref = str(principal.get("id") or "")
    tenant_ref = str(request.get("tenantRef") or "")
    application_ref = str(request.get("applicationRef") or "")
    repositories = {
        str(build.get("key") or ""): builder_registry_repository(
            principal_ref,
            tenant_ref,
            application_ref,
            str(build.get("key") or ""),
        )
        for build in builds
        if isinstance(build, dict)
    }
    if len(repositories) != len(builds):
        raise LumaError("signedBuildPlan contains an invalid build for registry derivation")
    external_registries = _lae_external_registry_allowlist()
    if any(not isinstance(item, dict) for item in external_images):
        raise LumaError("signedBuildPlan contains an invalid external image")
    requested_external_registries = {
        parse_external_image_reference(str(item.get("ref") or ""))["registryHost"]
        for item in external_images
    }
    denied_registries = sorted(requested_external_registries - set(external_registries))
    if denied_registries:
        raise LumaError("signedBuildPlan external image registry is not allowlisted by Luma Control")
    registry_lease = {
        "schemaVersion": "luma.builder-registry-lease/v1",
        "pullHost": pull_host,
        "pushHost": push_host,
        "repositories": repositories,
        "externalRegistries": external_registries,
        "insecure": insecure_raw == "1",
    }
    allow_basic = str(os.environ.get("LUMA_LAE_BUILDER_ALLOW_BASIC_REGISTRY") or "").strip() == "1"
    if allow_basic:
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        registry_auth = registry_auth_for_image(registries, f"{push_host}/lae/lease-probe:task")
        if not registry_auth:
            raise LumaError("LAE Basic build registry credential is not configured")
        auth_host = normalize_registry_host(public_registry_url(registry_auth.get("serveraddress") or ""))
        if auth_host != push_host:
            raise LumaError("LAE Basic build registry credential does not match pushHost")
        registry_lease["authMode"] = "basic"
        registry_lease["registryAuth"] = registry_auth
        return registry_lease

    if str(os.environ.get("LUMA_LAE_BUILDER_ALLOW_ANONYMOUS_REGISTRY") or "").strip() != "1":
        raise LumaError("LAE build registry authentication mode is not explicitly enabled")
    registry_lease["authMode"] = "anonymous"
    return registry_lease


def _lae_external_registry_allowlist() -> list[str]:
    raw = str(os.environ.get("LUMA_LAE_BUILDER_EXTERNAL_REGISTRIES_JSON") or "").strip()
    if not raw:
        return []
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LumaError("LAE external registry allowlist configuration is invalid") from exc
    if not isinstance(configured, list) or len(configured) > 32:
        raise LumaError("LAE external registry allowlist configuration is invalid")
    result: list[str] = []
    for value in configured:
        if not isinstance(value, str) or value != value.strip().lower():
            raise LumaError("LAE external registry allowlist configuration is invalid")
        try:
            parsed = parse_external_image_reference(f"{value}/lae/allowlist-probe:1")
        except LumaError as exc:
            raise LumaError("LAE external registry allowlist configuration is invalid") from exc
        if parsed["registryHost"] != value:
            raise LumaError("LAE external registry allowlist configuration is invalid")
        result.append(value)
    if len(result) != len(set(result)):
        raise LumaError("LAE external registry allowlist configuration is invalid")
    return sorted(result)


def _builder_task_http_status(exc: Exception) -> int:
    message = str(exc)
    lowered = message.lower()
    if message == "unauthorized" or "bearer token" in lowered:
        return 401
    if "builder task not found" in lowered:
        return 404
    if "idempotency-key is already bound" in lowered:
        return 409
    if "event cursor expired" in lowered:
        return 410
    if (
        "no declared, ready builder" in lowered
        or "allowlist is unavailable" in lowered
        or "signing key configuration is unavailable" in lowered
        or "signing key configuration is invalid" in lowered
        or "build registry" in lowered
        or "anonymous build registry" in lowered
    ):
        return 503
    return 400


def _builder_task_error_code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        404: "not_found",
        409: "conflict",
        410: "gone",
        503: "service_unavailable",
    }.get(int(status), "luma_error")


def _normalize_builder_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise LumaError("Idempotency-Key header is required")
    if len(key) > 200:
        raise LumaError("Idempotency-Key header exceeds 200 characters")
    if any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise LumaError("Idempotency-Key header contains unsupported characters")
    return key


def _builder_task_event(task: Dict[str, Any], event: Dict[str, Any], *, now: int | None = None) -> Dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    events = task.get("events")
    if not isinstance(events, list):
        events = []
        task["events"] = events
    cursor = int(task.get("nextEventCursor") or 1)
    message = str(event.get("message") or event.get("line") or "").strip()[:4000]
    item: Dict[str, Any] = {
        "cursor": cursor,
        "seq": cursor,
        "ts": now,
        "type": str(event.get("type") or "status").strip()[:40] or "status",
    }
    for key in ("name", "status"):
        value = str(event.get(key) or "").strip()
        if value:
            item[key] = value[:80]
    if message:
        item["message"] = message
    events.append(item)
    limit = max(int(BUILDER_TASK_EVENT_LIMIT), 100)
    if len(events) > limit:
        del events[: len(events) - limit]
    task["nextEventCursor"] = cursor + 1
    task["oldestEventCursor"] = int(events[0].get("cursor") or cursor) if events else cursor + 1
    task["updatedAt"] = now
    return item


def _builder_task_public(task: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "id": str(task.get("id") or ""),
        "schemaVersion": str(task.get("schemaVersion") or BUILDER_TASK_SCHEMA_VERSION),
        "kind": str(task.get("kind") or ""),
        "externalOperationId": str(task.get("externalOperationId") or ""),
        "tenantRef": str(task.get("tenantRef") or ""),
        "applicationRef": str(task.get("applicationRef") or ""),
        "status": str(task.get("status") or ""),
        "builderNode": str(task.get("builderNode") or ""),
        "message": str(task.get("message") or ""),
        "createdAt": int(task.get("createdAt") or 0),
        "updatedAt": int(task.get("updatedAt") or 0),
        "startedAt": int(task.get("startedAt") or 0),
        "completedAt": int(task.get("completedAt") or 0),
        "lastCursor": max(int(task.get("nextEventCursor") or 1) - 1, 0),
    }
    stored_result = task.get("result")
    if isinstance(stored_result, dict):
        result["result"] = dict(stored_result)
    return result


def _prune_builder_tasks(state: Dict[str, Any], *, now: int | None = None) -> None:
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max(int(BUILDER_TASK_RETENTION_SECONDS), 3600)
    terminal = {"canceled", "succeeded", "failed", "timed_out"}
    tasks = _builder_tasks(state)
    stale_ids = {
        str(task_id)
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("status") or "") in terminal
        and int(task.get("completedAt") or task.get("updatedAt") or 0) < cutoff
    }
    for task_id in stale_ids:
        tasks.pop(task_id, None)
    idempotency = _builder_task_idempotency(state)
    for key, record in list(idempotency.items()):
        if not isinstance(record, dict):
            idempotency.pop(key, None)
            continue
        task_id = str(record.get("taskId") or "")
        if int(record.get("expiresAt") or 0) <= now or task_id not in tasks:
            idempotency.pop(key, None)


def _builder_task_capabilities(kind: str) -> tuple[str, str]:
    specific = {
        "analyze-source": "builder-analyze-v1",
        "build-plan": "builder-build-v1",
    }.get(str(kind or ""))
    if not specific:
        raise LumaError(f"unsupported builder task kind: {kind}")
    return specific, "builder-task-v1"


def _node_agent_has_any_capability(record: Dict[str, Any], capabilities: tuple[str, ...]) -> bool:
    if _node_agent_status(record) != "ready":
        return False
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    advertised = {str(value) for value in agent.get("capabilities") or []}
    return bool(advertised.intersection(capabilities))


def _select_builder_task_node(state: Dict[str, Any], kind: str) -> str:
    required_capabilities = _builder_task_capabilities(kind)
    config = _build_config(state)
    candidates: list[str] = []
    default_node = str(config.get("defaultNode") or "").strip()
    if default_node:
        candidates.append(default_node)
    candidates.extend(_declared_build_node_names(state))
    if not candidates and DEFAULT_BUILD_NODE_NAME in (state.get("nodes") or {}):
        candidates.append(DEFAULT_BUILD_NODE_NAME)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    seen: set[str] = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        record = _node_record_for_name(nodes, name)
        if isinstance(record, dict) and _node_agent_has_any_capability(record, required_capabilities):
            return name
    declared = ", ".join(_declared_build_node_names(state)) or "none"
    raise LumaError(
        f"no declared, ready builder supports {' or '.join(required_capabilities)}; "
        f"update a builder node agent first (declared builders: {declared})"
    )


def _builder_task_for_agent_task(state: Dict[str, Any], agent_task: Dict[str, Any]) -> Dict[str, Any] | None:
    builder_task_id = str(agent_task.get("builderTaskId") or "").strip()
    if not builder_task_id:
        return None
    task = _builder_tasks(state).get(builder_task_id)
    return task if isinstance(task, dict) else None


def _builder_task_terminal(status: str) -> bool:
    return status in {"canceled", "succeeded", "failed", "timed_out"}


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


def _git_source_from_build_body(body: Dict[str, Any], *, repo_url: str, build_node: str, build_run_id: str) -> Dict[str, Any]:
    source = _redact_build_request(body)
    source["repoUrl"] = repo_url
    source["buildNode"] = build_node
    source["buildRunId"] = build_run_id
    return _sanitize_git_source(source)


def _deployment_git_source_from_body(body: Dict[str, Any]) -> Dict[str, Any] | None:
    source = body.get("gitSource")
    if not isinstance(source, dict):
        return None
    cleaned = _sanitize_git_source(source)
    return cleaned or None


def _sanitize_git_source(source: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "repoUrl",
        "providerId",
        "repository",
        "ref",
        "buildNode",
        "context",
        "dockerfile",
        "platform",
        "registryHost",
        "pushHost",
        "manifest",
        "composeContent",
        "composeSidecar",
        "proxyMode",
        "buildRunId",
    }
    cleaned: Dict[str, Any] = {}
    for key in allowed:
        value = source.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                cleaned[key] = text
        elif isinstance(value, (int, float, bool)):
            cleaned[key] = value
    return cleaned


def _create_build_run(body: Dict[str, Any], *, source: str, build_node: str) -> str:
    run_id = f"build-{secrets.token_hex(8)}"
    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        runs = _build_runs(state)
        runs[run_id] = {
            "id": run_id,
            "status": "running",
            "controlProcessInstanceId": _CONTROL_PROCESS_INSTANCE_ID,
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
    safe_event = {
        str(key): str(value)[:BUILD_RUN_MESSAGE_LIMIT]
        for key, value in event.items()
        if value is not None
    }

    def mutate(state: Dict[str, Any]) -> None:
        run = _build_runs(state).get(run_id)
        if not isinstance(run, dict):
            return
        events = run.get("events")
        if not isinstance(events, list):
            events = []
            run["events"] = events
        events.append({**safe_event, "ts": int(time.time())})
        limit = max(int(BUILD_RUN_EVENT_LIMIT), 100)
        if len(events) > limit:
            del events[: len(events) - limit]
        run["updatedAt"] = int(time.time())
        status = str(safe_event.get("status") or "")
        if status == "fail" and str(run.get("status") or "") not in {"canceling", "canceled"}:
            run["status"] = "failed"
            run["message"] = str(safe_event.get("message") or "")

    _mutate_control_state(mutate)


def _complete_build_run(run_id: str, status: str, *, result: Dict[str, Any] | None = None, message: str = "") -> None:
    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        run = _build_runs(state).get(run_id)
        if not isinstance(run, dict):
            return
        current_status = str(run.get("status") or "")
        final_status = "canceled" if current_status in {"canceling", "canceled"} and status != "canceled" else status
        run["status"] = final_status
        run["updatedAt"] = now
        run["completedAt"] = now
        if final_status == "canceled":
            run["message"] = "build canceled"
            run["canceledAt"] = now
            run.pop("result", None)
        elif message:
            run["message"] = str(message)[:BUILD_RUN_MESSAGE_LIMIT]
        if final_status != "canceled" and isinstance(result, dict):
            run["result"] = _build_run_result_summary(result)

    _mutate_control_state(mutate)


def _restart_build_run(run_id: str, body: Dict[str, Any], *, source: str, build_node: str) -> None:
    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        run = _build_runs(state).get(run_id)
        if not isinstance(run, dict):
            raise LumaError(f"build run not found: {run_id}")
        run["status"] = "running"
        run["controlProcessInstanceId"] = _CONTROL_PROCESS_INSTANCE_ID
        run["source"] = source
        run["buildNode"] = build_node
        run["request"] = _redact_build_request(body)
        run["events"] = []
        run["message"] = ""
        run.pop("result", None)
        run.pop("completedAt", None)
        run.pop("canceledAt", None)
        run.pop("cancelRequestedAt", None)
        run.pop("agentTaskId", None)
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
    event_limit = max(int(BUILD_RUN_EVENT_LIMIT), 100)
    for run in runs.values():
        if not isinstance(run, dict):
            continue
        events = run.get("events") if isinstance(run.get("events"), list) else []
        if len(events) > event_limit:
            run["events"] = events[-event_limit:]
        if run.get("message"):
            run["message"] = str(run.get("message"))[:BUILD_RUN_MESSAGE_LIMIT]
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


def _reconcile_orphaned_build_runs_after_control_restart() -> int:
    """Close request-owned imports left behind by an older Control process.

    A Repository Import spans the HTTP request thread, a serial node-agent
    build, and the deployment that follows it.  Unlike LAE Builder operations,
    that workflow cannot be resumed safely after the Control process exits.
    Leaving its record as ``running`` is worse than an explicit interruption:
    users cannot tell whether retry/cancel is safe and the linked child may
    continue occupying Builder capacity.  A per-process owner fence lets a new
    Control instance fail only work whose request thread cannot still exist.
    """

    # `create_app()` is also used by contract-only clients/tests before a
    # Control state exists.  Startup reconciliation must be a no-op there and
    # must not attempt to create the production default directory.
    if not state_path().is_file():
        return 0

    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> int:
        reconciled = 0
        tasks = _agent_tasks(state)
        for run in _build_runs(state).values():
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "")
            if status not in {"running", "canceling"}:
                continue
            if str(run.get("controlProcessInstanceId") or "") == _CONTROL_PROCESS_INSTANCE_ID:
                continue

            canceled = status == "canceling" or bool(run.get("cancelRequestedAt"))
            terminal_status = "canceled" if canceled else "failed"
            message = (
                "build canceled during Control restart"
                if canceled
                else "build interrupted by Control restart"
            )
            run.update(
                {
                    "status": terminal_status,
                    "message": message,
                    "updatedAt": now,
                    "completedAt": now,
                }
            )
            if canceled:
                run["canceledAt"] = now
            events = run.get("events") if isinstance(run.get("events"), list) else []
            events.append(
                {
                    "name": "Build image",
                    "status": "canceled" if canceled else "fail",
                    "message": message,
                    "ts": now,
                }
            )
            run["events"] = events[-max(int(BUILD_RUN_EVENT_LIMIT), 100) :]

            task_id = str(run.get("agentTaskId") or "")
            task = tasks.get(task_id)
            if isinstance(task, dict):
                task_status = str(task.get("status") or "")
                if task_status == "queued":
                    task.update(
                        {
                            "status": "canceled",
                            "message": "build owner Control restarted before task lease",
                            "result": {},
                            "completedAt": now,
                            "updatedAt": now,
                        }
                    )
                elif task_status == "running":
                    task["cancelRequestedAt"] = now
                    task["updatedAt"] = now
            reconciled += 1
        _prune_build_runs(state)
        return reconciled

    return int(_mutate_control_state(mutate) or 0)


def _deployment_events(state: Dict[str, Any]) -> list:
    events = state.get("deploymentEvents")
    if not isinstance(events, list):
        events = []
        state["deploymentEvents"] = events
    return events


def _record_deployment_event(
    *,
    kind: str,
    name: str,
    slug: str,
    source_name: str,
    origin: str,
    status: str,
    error: str = "",
    steps: list[dict[str, str]] | None = None,
    git_source: Dict[str, Any] | None = None,
) -> None:
    """Append an immutable deployment-history entry (CLI or dashboard `luma deploy` /
    `compose deploy`). This is a separate append-only log from the single latest-state
    record in state["deployments"], so the Deployments timeline can show every attempt
    with its origin. Pruned to the most recent 200. Mirrors the buildRuns pattern."""
    now = int(time.time())
    safe_steps = [
        {str(k): str(v) for k, v in step.items() if v is not None}
        for step in (steps or [])
        if isinstance(step, dict)
    ]
    entry = {
        "id": f"deploy-{secrets.token_hex(8)}",
        "kind": kind,
        "name": name,
        "slug": slug,
        "sourceName": source_name,
        "origin": origin or "cli",
        "status": status,
        "stepCount": len(safe_steps),
        "steps": safe_steps,
        "createdAt": now,
    }
    if error:
        entry["error"] = error
    if git_source:
        entry["gitSource"] = _sanitize_git_source(git_source)

    def mutate(state: Dict[str, Any]) -> None:
        events = _deployment_events(state)
        events.append(entry)
        if len(events) > 200:
            del events[: len(events) - 200]

    mutate_state(mutate)


def _deployment_origin(body: Dict[str, Any] | None) -> str:
    """Where a deploy came from. Dashboard sends origin=dashboard explicitly; the CLI
    omits it, so anything else defaults to cli."""
    origin = str((body or {}).get("origin") or "").strip().lower()
    return "dashboard" if origin == "dashboard" else "cli"


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
    progress_limit = max(int(AGENT_TASK_PROGRESS_LIMIT), 50)
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        progress = task.get("progress") if isinstance(task.get("progress"), list) else []
        if len(progress) > progress_limit:
            task["progress"] = progress[-progress_limit:]
        if task.get("message"):
            task["message"] = str(task.get("message"))[:BUILD_RUN_MESSAGE_LIMIT]

    # A running task for a node that is no longer registered can never report
    # completion: its agent token and canonical identity no longer exist.
    # Reuse the normal interruption path so linked Builder tasks/build runs are
    # finalized consistently instead of retaining permanent ghost activity.
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    unregistered_running_nodes = {
        str(task.get("nodeName") or "")
        for task in tasks.values()
        if isinstance(task, dict)
        and str(task.get("status") or "") == "running"
        and str(task.get("nodeName") or "")
        and _node_record_entry_for_name_or_id(nodes, str(task.get("nodeName") or "")) is None
    }
    for node_name in sorted(unregistered_running_nodes):
        _reconcile_interrupted_agent_tasks(state, node_name, "", now=now)

    terminal = {"succeeded", "failed", "timeout", "canceled"}
    stale = [
        task_id
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("status") or "") in terminal
        and int(task.get("completedAt") or task.get("updatedAt") or 0) < cutoff
    ]
    for task_id in stale:
        tasks.pop(task_id, None)


def _reconcile_interrupted_agent_tasks(
    state: Dict[str, Any],
    node_name: str,
    active_task_id: str,
    *,
    now: int | None = None,
) -> None:
    """Fail running tasks that the node agent no longer owns.

    A task is leased before it is executed.  If the agent process then exits
    before reporting a terminal result, the durable task used to remain
    ``running`` forever.  The next authenticated heartbeat is authoritative:
    its ``activeTaskId`` is the one task still executing on that serial agent;
    every other running task for the node is an orphan from an earlier agent
    process.  Failing rather than replaying an arbitrary host mutation keeps
    recovery safe and lets the owning operation retry through its normal
    idempotency boundary.
    """

    now = int(time.time()) if now is None else now
    tasks = _agent_tasks(state)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for task_id, task in tasks.items():
        task_node_name = str(task.get("nodeName") or "") if isinstance(task, dict) else ""
        task_node_entry = _node_record_entry_for_name_or_id(nodes, task_node_name)
        canonical_task_node = task_node_entry[0] if task_node_entry else task_node_name
        leased_at = int(task.get("leasedAt") or 0) if isinstance(task, dict) else 0
        if (
            not isinstance(task, dict)
            or canonical_task_node != node_name
            or str(task.get("status") or "") != "running"
            or task_id == active_task_id
            or (
                not active_task_id
                and leased_at > 0
                and now - leased_at < max(int(AGENT_TASK_HANDOFF_GRACE_SECONDS), 1)
            )
        ):
            continue

        builder_task = _builder_task_for_agent_task(state, task)
        canceled = bool(task.get("cancelRequestedAt")) or (
            isinstance(builder_task, dict)
            and str(builder_task.get("status") or "") in {"cancel_requested", "canceled"}
        )
        status = "canceled" if canceled else "failed"
        message = "agent task canceled" if canceled else "node agent restarted before task completion"
        task.update(
            {
                "status": status,
                "message": message,
                "result": {},
                "completedAt": now,
                "updatedAt": now,
            }
        )

        if isinstance(builder_task, dict) and not _builder_task_terminal(str(builder_task.get("status") or "")):
            builder_message = "builder task canceled" if canceled else "builder task interrupted by node agent restart"
            builder_task.update(
                {
                    "status": status,
                    "message": builder_message,
                    "result": {},
                    "completedAt": now,
                    "updatedAt": now,
                }
            )
            _builder_task_event(
                builder_task,
                {"type": "status", "status": status, "message": builder_message},
                now=now,
            )

        build_run_id = str(task.get("buildRunId") or "")
        build_run = _build_runs(state).get(build_run_id) if build_run_id else None
        if isinstance(build_run, dict) and str(build_run.get("status") or "") not in {
            "succeeded",
            "failed",
            "canceled",
        }:
            build_status = "canceled" if canceled else "failed"
            build_message = "build canceled" if canceled else "build interrupted by node agent restart"
            build_run.update(
                {
                    "status": build_status,
                    "message": build_message,
                    "updatedAt": now,
                    "completedAt": now,
                }
            )
            if canceled:
                build_run["canceledAt"] = now
            events = build_run.get("events") if isinstance(build_run.get("events"), list) else []
            events.append(
                {
                    "name": "Build image",
                    "status": "canceled" if canceled else "fail",
                    "message": build_message,
                    "ts": now,
                }
            )
            build_run["events"] = events[-max(int(BUILD_RUN_EVENT_LIMIT), 100) :]


def _mutate_control_state(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    return mutate_state(mutator)


def _mutate_control_state_if_changed(
    mutator: Callable[[Dict[str, Any]], tuple[Any, bool]],
) -> Any:
    return mutate_state_if_changed(mutator)


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


_BUILDER_CREDENTIAL_FAILURE_MESSAGE = "builder credential lease redemption failed"


def _strip_builder_object_source_ephemeral(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove every object-source delivery field from a durable payload copy."""

    sanitized = dict(payload)
    sanitized.pop("objectUrl", None)
    sanitized.pop("objectAllowedHost", None)
    source_ref = sanitized.get("sourceRef")
    if isinstance(source_ref, dict):
        source_copy = dict(source_ref)
        source_copy.pop("objectUrl", None)
        source_copy.pop("objectAllowedHost", None)
        sanitized["sourceRef"] = source_copy
    return sanitized


def _builder_analyze_credential_binding(
    task: Dict[str, Any],
    builder_task: Dict[str, Any] | None,
) -> CredentialLeaseBinding | ObjectSourceLeaseBinding | None:
    if not isinstance(builder_task, dict) or str(builder_task.get("kind") or "") != "analyze-source":
        return None
    request = (
        builder_task.get("request")
        if isinstance(builder_task.get("request"), dict)
        else {}
    )
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    source_ref = payload.get("sourceRef") if isinstance(payload.get("sourceRef"), dict) else {}
    common = {
        "lease_id": str(payload.get("credentialLeaseId") or ""),
        "builder_task_id": str(builder_task.get("id") or task.get("builderTaskId") or ""),
        "external_operation_id": str(builder_task.get("externalOperationId") or ""),
        "principal_ref": str(builder_task.get("principalRef") or ""),
        "tenant_ref": str(builder_task.get("tenantRef") or ""),
        "application_ref": str(builder_task.get("applicationRef") or ""),
    }
    if source_ref.get("kind") == "object":
        return ObjectSourceLeaseBinding(
            **common,
            object_descriptor=dict(source_ref),
        )
    return CredentialLeaseBinding(
        **common,
        repository=str(source_ref.get("repository") or ""),
    )


def _fail_builder_credential_redemption(agent_task_id: str, builder_task_id: str) -> None:
    """Atomically fail a claimed builder task without persisting broker output."""

    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        agent_task = _agent_tasks(state).get(agent_task_id)
        builder_task = _builder_tasks(state).get(builder_task_id)
        if not isinstance(agent_task, dict) or not isinstance(builder_task, dict):
            return
        if str(agent_task.get("builderTaskId") or "") != builder_task_id:
            return
        if str(agent_task.get("status") or "") != "running" or str(builder_task.get("status") or "") != "running":
            # Cancellation or another terminal transition won while the broker
            # call was in flight.  Never revive or overwrite that state.
            return
        agent_task.update(
            {
                "status": "failed",
                "message": _BUILDER_CREDENTIAL_FAILURE_MESSAGE,
                "result": {},
                "completedAt": now,
                "updatedAt": now,
            }
        )
        builder_task.update(
            {
                "status": "failed",
                "message": _BUILDER_CREDENTIAL_FAILURE_MESSAGE,
                "result": {},
                "completedAt": now,
                "updatedAt": now,
            }
        )
        _builder_task_event(
            builder_task,
            {
                "type": "status",
                "status": "failed",
                "message": _BUILDER_CREDENTIAL_FAILURE_MESSAGE,
            },
            now=now,
        )

    _mutate_control_state(mutate)


def _finalize_unhanded_builder_cancellation(agent_task_id: str, builder_task_id: str) -> None:
    """Finish a cancellation that won while no agent had received the task."""

    now = int(time.time())

    def mutate(state: Dict[str, Any]) -> None:
        agent_task = _agent_tasks(state).get(agent_task_id)
        builder_task = _builder_tasks(state).get(builder_task_id)
        if not isinstance(agent_task, dict) or not isinstance(builder_task, dict):
            return
        if (
            str(agent_task.get("builderTaskId") or "") != builder_task_id
            or str(agent_task.get("status") or "") != "running"
            or str(builder_task.get("status") or "") != "cancel_requested"
        ):
            return
        message = "builder task canceled before credential delivery"
        agent_task.update(
            {
                "status": "canceled",
                "message": message,
                "result": {},
                "completedAt": now,
                "updatedAt": now,
            }
        )
        builder_task.update(
            {
                "status": "canceled",
                "message": message,
                "result": {},
                "completedAt": now,
                "updatedAt": now,
            }
        )
        _builder_task_event(
            builder_task,
            {"type": "status", "status": "canceled", "message": message},
            now=now,
        )

    _mutate_control_state(mutate)


def _builder_credential_delivery_allowed(agent_task_id: str, builder_task_id: str) -> bool:
    """Re-check cancellation/terminal state after broker network I/O."""

    def mutate(state: Dict[str, Any]) -> bool:
        agent_task = _agent_tasks(state).get(agent_task_id)
        builder_task = _builder_tasks(state).get(builder_task_id)
        return bool(
            isinstance(agent_task, dict)
            and isinstance(builder_task, dict)
            and str(agent_task.get("builderTaskId") or "") == builder_task_id
            and str(agent_task.get("status") or "") == "running"
            and not agent_task.get("cancelRequestedAt")
            and str(builder_task.get("status") or "") == "running"
        )

    return bool(_mutate_control_state(mutate))


def _enrich_builder_analyze_lease(
    leased_task: Dict[str, Any],
    redemption: RedeemedCredential | RedeemedObjectSource,
) -> Dict[str, Any]:
    payload = _strip_builder_object_source_ephemeral(
        leased_task.get("payload")
        if isinstance(leased_task.get("payload"), dict)
        else {}
    )
    # Even a manually corrupted state file cannot bypass the broker by placing
    # legacy credentials in the child payload.
    payload.pop("gitToken", None)
    payload.pop("gitUsername", None)
    if isinstance(redemption, RedeemedObjectSource):
        source_ref = payload.get("sourceRef") if isinstance(payload.get("sourceRef"), dict) else {}
        if source_ref.get("kind") != "object":
            raise LumaError(_BUILDER_CREDENTIAL_FAILURE_MESSAGE)
        payload["objectUrl"] = redemption.object_url
        payload["objectAllowedHost"] = redemption.allowed_host
    elif redemption.kind == "git-https":
        payload["gitUsername"] = redemption.username
        payload["gitToken"] = redemption.password
    leased_task["payload"] = payload
    return leased_task


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
    first_poll = True
    while True:
        persist_heartbeat = first_poll

        def mutate(state: Dict[str, Any]) -> tuple[Any, bool]:
            nonlocal normalized_container_stats
            nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
            entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
            canonical_node_name = entry[0] if entry else node_name
            record = _require_node_agent_token(state, token, node_name, node_id=node_id)
            changed = False
            if persist_heartbeat:
                _prune_agent_tasks(state)
                _prune_build_runs(state)
                normalized_container_stats = _update_agent_heartbeat(
                    record, body, config=config, state=state
                )
                # Idle serial agents poll this endpoint instead of the busy
                # heartbeat endpoint. Their lease heartbeat therefore proves
                # that no earlier running task is still owned by this process.
                _reconcile_interrupted_agent_tasks(
                    state,
                    canonical_node_name,
                    str(body.get("activeTaskId") or "").strip(),
                )
                changed = True
            tasks = _agent_tasks(state)
            now = int(time.time())
            for task_id in sorted(tasks):
                task = tasks.get(task_id)
                if not isinstance(task, dict):
                    continue
                if task.get("nodeName") != canonical_node_name or task.get("status") != "queued":
                    continue
                required_capabilities_raw = task.get("requiredCapabilitiesAny")
                required_capabilities = tuple(
                    str(value).strip()
                    for value in required_capabilities_raw
                    if str(value).strip()
                ) if isinstance(required_capabilities_raw, list) else ()
                required_capability = str(task.get("requiredCapability") or "").strip()
                if required_capabilities and not _node_agent_has_any_capability(record, required_capabilities):
                    continue
                if not required_capabilities and required_capability and not _node_agent_is_ready(record, required_capability=required_capability):
                    continue
                builder_task = _builder_task_for_agent_task(state, task)
                if builder_task is not None:
                    builder_status = str(builder_task.get("status") or "")
                    if builder_status != "queued":
                        if builder_status in {"cancel_requested", "canceled"}:
                            task["status"] = "canceled"
                            task["message"] = "builder task canceled before lease"
                            task["completedAt"] = now
                            task["updatedAt"] = now
                            changed = True
                        continue
                # Ephemeral source fields must never survive a prior crash or
                # manual state corruption. Remove them from the durable child
                # before producing the in-memory lease payload.
                durable_payload = _strip_builder_object_source_ephemeral(
                    task.get("payload")
                    if isinstance(task.get("payload"), dict)
                    else {}
                )
                task["payload"] = durable_payload
                task["status"] = "running"
                task["leasedAt"] = now
                task["updatedAt"] = now
                if builder_task is not None:
                    builder_task["status"] = "running"
                    builder_task["startedAt"] = now
                    builder_task["updatedAt"] = now
                    _builder_task_event(
                        builder_task,
                        {"type": "status", "status": "running", "message": "builder task leased"},
                        now=now,
                    )
                leased_task = {
                    "id": task_id,
                    "action": task.get("action"),
                    "payload": _agent_task_lease_payload(state, task),
                }
                redemption_binding = _builder_analyze_credential_binding(task, builder_task)
                if redemption_binding is not None:
                    parent_request = (
                        builder_task.get("request")
                        if isinstance(builder_task, dict)
                        and isinstance(builder_task.get("request"), dict)
                        else {}
                    )
                    parent_payload = (
                        parent_request.get("payload")
                        if isinstance(parent_request.get("payload"), dict)
                        else {}
                    )
                    # The durable parent request is the source-of-truth. A
                    # corrupted child cannot redirect either Git or object
                    # redemption to a different source.
                    leased_task["payload"]["sourceRef"] = dict(
                        parent_payload.get("sourceRef")
                        if isinstance(parent_payload.get("sourceRef"), dict)
                        else {}
                    )
                    leased_task["payload"]["credentialLeaseId"] = str(
                        parent_payload.get("credentialLeaseId") or ""
                    )
                    # The public request schema already forbids these fields.
                    # Strip again at the final trust boundary so old global
                    # gitProviders/secrets and manually edited state can never
                    # participate in an LAE analyze-source lease.
                    leased_task["payload"].pop("gitToken", None)
                    leased_task["payload"].pop("gitUsername", None)
                    leased_task["payload"].pop("objectUrl", None)
                    leased_task["payload"].pop("objectAllowedHost", None)
                return (leased_task, redemption_binding), True
            return None, changed

        candidate = _mutate_control_state_if_changed(mutate)
        first_poll = False
        if candidate is not None:
            leased, redemption_binding = candidate
            if redemption_binding is not None:
                try:
                    # Deliberately outside mutate_state's process lock: broker
                    # I/O must not block heartbeats, cancellation, or other
                    # task leases.
                    redemption = (
                        redeem_builder_object_source(redemption_binding)
                        if isinstance(redemption_binding, ObjectSourceLeaseBinding)
                        else redeem_builder_credential(redemption_binding)
                    )
                except Exception:
                    _fail_builder_credential_redemption(
                        str(leased.get("id") or ""),
                        redemption_binding.builder_task_id,
                    )
                    _finalize_unhanded_builder_cancellation(
                        str(leased.get("id") or ""),
                        redemption_binding.builder_task_id,
                    )
                    leased = None
                else:
                    if _builder_credential_delivery_allowed(
                        str(leased.get("id") or ""),
                        redemption_binding.builder_task_id,
                    ):
                        leased = _enrich_builder_analyze_lease(
                            leased, redemption
                        )
                    else:
                        # Cancellation or a terminal transition happened while
                        # redeeming.  Drop the in-memory credential and return
                        # no task; nothing is written to Control state.
                        _finalize_unhanded_builder_cancellation(
                            str(leased.get("id") or ""),
                            redemption_binding.builder_task_id,
                        )
                        leased = None
            break
        if time.time() >= deadline:
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
        _prune_agent_tasks(state)
        _prune_build_runs(state)
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
        canonical_node_name = entry[0] if entry else node_name
        record = _require_node_agent_token(state, token, node_name, node_id=node_id)
        normalized_container_stats = _update_agent_heartbeat(record, body, config=config, state=state)
        _reconcile_interrupted_agent_tasks(
            state,
            canonical_node_name,
            str(body.get("activeTaskId") or "").strip(),
        )

    _mutate_control_state(mutate)
    state = load_state()
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, canonical_node_name) or {}
    _record_metrics_history(canonical_node_name, body, container_stats=normalized_container_stats, config=config, state=state)
    active_task_id = str(body.get("activeTaskId") or "").strip()
    cancel_requested = False
    if active_task_id:
        active_task = _agent_tasks(state).get(active_task_id)
        if isinstance(active_task, dict) and str(active_task.get("nodeName") or "") == canonical_node_name:
            builder_task = _builder_task_for_agent_task(state, active_task)
            cancel_requested = bool(active_task.get("cancelRequestedAt")) or (
                isinstance(builder_task, dict)
                and str(builder_task.get("status") or "") in {"cancel_requested", "canceled"}
            )
    return {
        "nodeName": canonical_node_name,
        "status": _node_agent_status(record),
        "activeTaskId": active_task_id,
        "cancelRequested": cancel_requested,
    }


def _agent_task_lease_payload(state: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(task.get("payload") if isinstance(task.get("payload"), dict) else {})
    if task.get("action") == "analyze-source":
        payload = _strip_builder_object_source_ephemeral(payload)
        # The analyzer discovers image refs only after the network-disabled
        # runner finishes, so Control leases its complete public-registry
        # allowlist for analyze-side digest binding. This derived policy is not
        # persisted in the child task body.
        payload["externalRegistries"] = _lae_external_registry_allowlist()
    if task.get("action") == "build-plan":
        builder_task = _builder_task_for_agent_task(state, task)
        if not isinstance(builder_task, dict):
            raise LumaError("build-plan task is missing its durable parent")
        request = builder_task.get("request") if isinstance(builder_task.get("request"), dict) else {}
        principal = {"id": str(builder_task.get("principalRef") or "")}
        payload["principalRef"] = principal["id"]
        payload["registry"] = _builder_build_registry_lease(state, request, principal)
    if task.get("action") in {"resolve-docker-image", "diagnose-docker-pull"} and not payload.get("registryAuth"):
        image = str(payload.get("image") or "")
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        registry_auth = registry_auth_for_image(registries, image)
        if registry_auth:
            payload["registryAuth"] = registry_auth
    if task.get("action") in {"mirror-control-image", "mirror-system-image"} and not payload.get("registryAuth"):
        # Registry credentials are leased ephemerally to the agent. They must
        # never be persisted in agentTasks/control-image operation records.
        push_image = str(payload.get("pushImage") or "")
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        registry_auth = registry_auth_for_image(registries, push_image)
        if registry_auth:
            payload["registryAuth"] = registry_auth
    if task.get("action") == "cache-runtime-image":
        # Source and destination credentials are independently leased.  A
        # private external image must be readable by the Builder without ever
        # persisting its password in agentTasks, while an authenticated
        # internal registry may require a different write credential.
        registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
        source_image = str(payload.get("sourceImage") or "")
        push_image = str(payload.get("pushImage") or "")
        source_auth = registry_auth_for_image(registries, source_image)
        destination_auth = registry_auth_for_image(registries, push_image)
        if source_auth:
            payload["sourceRegistryAuth"] = source_auth
        if destination_auth:
            payload["destinationRegistryAuth"] = destination_auth
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
    if status not in {"succeeded", "failed", "canceled"}:
        raise LumaError("status must be succeeded, failed, or canceled")
    if not node_name or not task_id:
        raise LumaError("nodeName and taskId are required")

    normalized_container_stats: list[Dict[str, Any]] = []
    config = load_config(_control_config_path())
    final_status = status

    def mutate(state: Dict[str, Any]) -> None:
        nonlocal final_status, normalized_container_stats
        nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        entry = _node_record_entry_for_name_or_id(nodes, node_name, node_id)
        canonical_node_name = entry[0] if entry else node_name
        record = _require_node_agent_token(state, token, node_name, node_id=node_id)
        normalized_container_stats = _update_agent_heartbeat(record, body, config=config, state=state)
        tasks = _agent_tasks(state)
        task = tasks.get(task_id)
        if not isinstance(task, dict) or task.get("nodeName") != canonical_node_name:
            raise LumaError(f"agent task not found: {task_id}")
        existing_status = str(task.get("status") or "")
        if existing_status in {"succeeded", "failed", "canceled"}:
            # Completion is an idempotent terminal acknowledgement.  The node
            # agent retries until it receives this response because the host
            # mutation may already have happened even when a gateway drops the
            # first response.  Never append a second Builder event or rewrite
            # a terminal decision on a replay.
            final_status = existing_status
            return
        now = int(time.time())
        builder_task = _builder_task_for_agent_task(state, task)
        effective_status = status
        effective_message = str(body.get("message") or "")
        raw_result = body.get("result") if isinstance(body.get("result"), dict) else {}
        stored_result = raw_result
        if builder_task is not None and str(builder_task.get("status") or "") in {"cancel_requested", "canceled"}:
            effective_status = "canceled"
            effective_message = "builder task canceled"
        if builder_task is not None:
            stored_result = {}
            if effective_status == "succeeded":
                try:
                    stored_result = sanitize_builder_task_result(
                        str(builder_task.get("kind") or ""),
                        raw_result,
                        request=builder_task.get("request") if isinstance(builder_task.get("request"), dict) else None,
                    )
                    if str(builder_task.get("kind") or "") == "analyze-source":
                        _record_builder_source_snapshot(state, builder_task, stored_result, now=now)
                    effective_message = "builder task succeeded"
                except LumaError:
                    effective_status = "failed"
                    effective_message = "invalid builder task result"
            else:
                # Node-agent completion messages are untrusted process output.
                # Persist only a Control-generated terminal description.
                effective_message = f"builder task {effective_status}"
        elif str(task.get("action") or "") == "export-builder-artifact":
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
            expected = {
                "leaseId": str(payload.get("leaseId") or ""),
                "digest": str(artifact.get("digest") or ""),
                "sizeBytes": artifact.get("sizeBytes"),
                "message": "builder artifact exported",
            }
            if effective_status == "succeeded" and raw_result == expected:
                stored_result = {
                    "leaseId": expected["leaseId"],
                    "digest": expected["digest"],
                    "sizeBytes": expected["sizeBytes"],
                }
                effective_message = "builder artifact exported"
            else:
                stored_result = {}
                if effective_status == "succeeded":
                    effective_status = "failed"
                effective_message = f"builder artifact export {effective_status}"
                ARTIFACT_DOWNLOADS.revoke(str(payload.get("leaseId") or ""))
        final_status = effective_status
        task.update(
            {
                "status": effective_status,
                "message": effective_message,
                "result": stored_result,
                "completedAt": now,
                "updatedAt": now,
            }
        )
        if builder_task is not None:
            builder_task["status"] = effective_status
            builder_task["message"] = effective_message
            builder_task["result"] = stored_result
            builder_task["completedAt"] = now
            builder_task["updatedAt"] = now
            _builder_task_event(
                builder_task,
                {
                    "type": "status",
                    "status": effective_status,
                    "message": effective_message or f"builder task {effective_status}",
                },
                now=now,
            )
        build_run_id = str(task.get("buildRunId") or "")
        build_run = _build_runs(state).get(build_run_id) if build_run_id else None
        if isinstance(build_run, dict) and effective_status in {"failed", "canceled"}:
            canceled = effective_status == "canceled" or str(build_run.get("status") or "") in {"canceling", "canceled"}
            build_run["status"] = "canceled" if canceled else "failed"
            build_run["message"] = "build canceled" if canceled else (effective_message or "build failed")
            build_run["updatedAt"] = now
            build_run["completedAt"] = now
            if canceled:
                build_run["canceledAt"] = now
            events = build_run.get("events") if isinstance(build_run.get("events"), list) else []
            events.append(
                {
                    "name": "Build image",
                    "status": "canceled" if canceled else "fail",
                    "message": build_run["message"],
                    "ts": now,
                }
            )
            build_run["events"] = events[-max(int(BUILD_RUN_EVENT_LIMIT), 100) :]

    _mutate_control_state(mutate)
    state = load_state()
    entry = _node_record_entry_for_name_or_id(state.get("nodes") if isinstance(state.get("nodes"), dict) else {}, node_name, node_id)
    _record_metrics_history(entry[0] if entry else node_name, body, config=config, state=state)
    return {"taskId": task_id, "status": final_status}


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
        builder_task = _builder_task_for_agent_task(state, task)
        stored_events = [sanitize_builder_task_progress_event(event) for event in events] if builder_task is not None else events
        progress = task.get("progress") if isinstance(task.get("progress"), list) else []
        progress.extend(stored_events)
        task["progress"] = progress[-max(int(AGENT_TASK_PROGRESS_LIMIT), 50) :]
        task["updatedAt"] = int(time.time())
        if builder_task is not None and not _builder_task_terminal(str(builder_task.get("status") or "")):
            for event in stored_events:
                _builder_task_event(builder_task, event)

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
    build_run_id: str = "",
) -> Dict[str, Any]:
    task_id = _queue_node_agent_task(
        state,
        node_name,
        action,
        payload,
        required_capability=required_capability,
        build_run_id=build_run_id,
    )
    return _wait_node_agent_task(task_id, node_name, action, timeout=timeout, progress=progress)


def _queue_node_agent_task(
    state: Dict[str, Any],
    node_name: str,
    action: str,
    payload: Dict[str, Any],
    *,
    required_capability: str | None = "nfs-host",
    build_run_id: str = "",
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
        task = {
            "id": task_id,
            "nodeName": node_name,
            "action": action,
            "payload": dict(payload),
            "progress": [],
            "status": "queued",
            "createdAt": now,
            "updatedAt": now,
        }
        if build_run_id:
            run = _build_runs(current).get(build_run_id)
            if not isinstance(run, dict):
                raise LumaError(f"build run not found: {build_run_id}")
            if str(run.get("status") or "") in {"canceling", "canceled"}:
                raise LumaError("build canceled")
            task["buildRunId"] = build_run_id
            run["agentTaskId"] = task_id
            run["updatedAt"] = now
        _agent_tasks(current)[task_id] = task

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
    wait_started = time.time()
    execution_timeout = float(timeout or AGENT_TASK_TIMEOUT_SECONDS)
    queue_deadline = _agent_task_wait_deadline({}, wait_started, execution_timeout)
    cursor = 0
    while True:
        current = load_state()
        task = (current.get("agentTasks") if isinstance(current.get("agentTasks"), dict) else {}).get(task_id)
        status = "missing"
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
            if status == "canceled":
                raise LumaError(str(task.get("message") or f"agent task canceled: {task_id}"))
            if status == "timeout":
                raise LumaError(str(task.get("message") or f"agent task timed out: {task_id}"))
        now = time.time()
        deadline = (
            _agent_task_wait_deadline(task, wait_started, execution_timeout)
            if status == "running" and isinstance(task, dict)
            else queue_deadline
        )
        if now >= deadline:
            break
        time.sleep(1)

    def mark_timeout(state: Dict[str, Any]) -> None:
        tasks = _agent_tasks(state)
        task = tasks.get(task_id)
        if isinstance(task, dict) and task.get("status") in {"queued", "running"}:
            now = int(time.time())
            task["status"] = "timeout"
            task["message"] = "agent task timed out"
            # A running child must not continue consuming Builder/worker
            # capacity after its caller has already received a timeout.
            task["cancelRequestedAt"] = now
            task["completedAt"] = now
            task["updatedAt"] = now

    mutate_state(mark_timeout)
    raise LumaError(f"node agent task timed out on {node_name}: {action}")


def _agent_task_wait_deadline(
    task: Dict[str, Any],
    wait_started: float,
    execution_timeout: float,
) -> float:
    if str(task.get("status") or "") == "running":
        return float(task.get("leasedAt") or wait_started) + execution_timeout
    return wait_started + max(
        execution_timeout,
        float(AGENT_TASK_QUEUE_TIMEOUT_SECONDS),
    )


def _agent_task_progress_step_name(action: str) -> str:
    if action == "build-image":
        return "Build image"
    if action == "diagnose-docker-pull":
        return "Docker pull"
    if action == "cache-runtime-image":
        return "Cache image on Builder"
    return "Agent task"


def handle_control_status(token: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    runtime_config = _config_with_state_nodes(config, state)
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

    lae_admin_available = _lae_admin_proxy_available()
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
        "laeAdmin": {
            "available": lae_admin_available,
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


_LAE_ADMIN_DASHBOARD_RESOURCES = frozenset(
    {"users", "tenants", "applications", "operations", "placements", "usage"}
)
_LAE_ADMIN_QUERY_ERROR = "LAE admin query is invalid"
_LAE_ADMIN_UNAVAILABLE_ERROR = "LAE admin API is unavailable"


def _lae_admin_proxy_available() -> bool:
    """Report only whether the local server-side proxy config is usable."""

    try:
        load_lae_admin_proxy_config()
    except LaeAdminProxyError:
        return False
    return True


def _parse_dashboard_lae_admin_query(
    resource: str,
    raw_query: str,
) -> tuple[int, int]:
    if resource not in _LAE_ADMIN_DASHBOARD_RESOURCES:
        raise LaeAdminProxyError(_LAE_ADMIN_QUERY_ERROR)
    try:
        pairs = urllib.parse.parse_qsl(
            str(raw_query or ""),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except (TypeError, ValueError):
        raise LaeAdminProxyError(_LAE_ADMIN_QUERY_ERROR) from None
    if (
        len(pairs) != len({key for key, _value in pairs})
        or any(key not in {"limit", "offset"} for key, _value in pairs)
    ):
        raise LaeAdminProxyError(_LAE_ADMIN_QUERY_ERROR)
    values = dict(pairs)
    raw_limit = values.get("limit", "100")
    raw_offset = values.get("offset", "0")
    if (
        re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None
        or re.fullmatch(r"(?:0|[1-9][0-9]{0,6})", raw_offset) is None
    ):
        raise LaeAdminProxyError(_LAE_ADMIN_QUERY_ERROR)
    limit = int(raw_limit)
    offset = int(raw_offset)
    if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
        raise LaeAdminProxyError(_LAE_ADMIN_QUERY_ERROR)
    return limit, offset


def _dashboard_lae_active_allocations(
    client: NomadApi,
    job_slug: str,
) -> tuple[list[Dict[str, str]], str]:
    if not job_slug:
        return [], "not_submitted"
    try:
        allocations = client.request(
            "GET",
            f"/v1/job/{urllib.parse.quote(job_slug, safe='')}/allocations",
        )
    except LumaError:
        return [], "unavailable"
    if not isinstance(allocations, list):
        return [], "unavailable"
    result: list[Dict[str, str]] = []
    for raw in allocations:
        if not isinstance(raw, dict):
            continue
        desired = str(raw.get("DesiredStatus") or "").lower()
        status = str(raw.get("ClientStatus") or "").lower()
        if desired not in {"", "run", "running"}:
            continue
        if status in {"complete", "dead", "failed", "lost"}:
            continue
        node_id = str(raw.get("NodeID") or "").strip()
        node_name = str(raw.get("NodeName") or "").strip()
        allocation_id = str(raw.get("ID") or "").strip()
        if not node_id and not node_name:
            continue
        result.append(
            {
                "allocationId": allocation_id,
                "nodeId": node_id,
                "nodeName": node_name,
                "status": status or "unknown",
            }
        )
    result.sort(
        key=lambda item: (
            item["nodeName"],
            item["nodeId"],
            item["allocationId"],
        )
    )
    return result, "observed"


def _dashboard_lae_runtime_placements(
    state: Dict[str, Any],
    *,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    """Return topology only to the Luma management-token dashboard."""

    raw_runtime = state.get("laeRuntime")
    runtime = raw_runtime if isinstance(raw_runtime, dict) else {}
    raw_deployments = runtime.get("deployments")
    deployments = raw_deployments if isinstance(raw_deployments, dict) else {}
    records = [
        record
        for record in deployments.values()
        if isinstance(record, dict) and isinstance(record.get("placement"), dict)
    ]
    records.sort(
        key=lambda record: (
            int(record.get("updatedAt") or record.get("createdAt") or 0),
            str(record.get("runtimeDeploymentRef") or ""),
        ),
        reverse=True,
    )
    selected = records[offset : offset + limit]
    client: NomadApi | None = None
    if selected:
        try:
            config_path = Path(
                os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml"
            )
            config = load_config(config_path)
            client = NomadApi(
                nomad_addr(config, state),
                token=str(state.get("nomadToken") or ""),
            )
        except (LumaError, OSError, ValueError):
            client = None

    items: list[Dict[str, Any]] = []
    for record in selected:
        placement = dict(record.get("placement") or {})
        summary = (
            dict(placement.get("summary"))
            if isinstance(placement.get("summary"), dict)
            else {}
        )
        manifest = (
            dict(record.get("manifest"))
            if isinstance(record.get("manifest"), dict)
            else {}
        )
        job_slug = str(record.get("jobSlug") or "")
        active, observation = (
            _dashboard_lae_active_allocations(client, job_slug)
            if client is not None
            else ([], "unavailable" if job_slug else "not_submitted")
        )
        candidate_node_ids = sorted(
            {
                str(value)
                for value in placement.get("candidateNodeIds") or []
                if isinstance(value, str) and value
            }
        )
        item: Dict[str, Any] = {
            "runtimeDeploymentRef": str(
                record.get("runtimeDeploymentRef") or ""
            ),
            "tenantRef": str(record.get("tenantRef") or ""),
            "applicationRef": str(record.get("applicationRef") or ""),
            "deploymentRef": str(record.get("deploymentRef") or ""),
            "jobSlug": job_slug,
            "status": str(record.get("status") or "unknown"),
            "region": str(summary.get("region") or manifest.get("region") or ""),
            "stateful": bool(summary.get("stateful")),
            "continuity": str(summary.get("continuity") or "unknown"),
            "candidateNodeIds": candidate_node_ids,
            "preferredNodeId": str(placement.get("preferredNodeId") or ""),
            "activeAllocations": active,
            "observationStatus": observation,
            "decisionDigest": str(summary.get("decisionDigest") or ""),
            "updatedAt": int(
                record.get("updatedAt") or record.get("createdAt") or 0
            ),
        }
        preferred_domain = placement.get("preferredFailureDomain")
        if isinstance(preferred_domain, dict):
            item["preferredFailureDomain"] = {
                "metaKey": str(preferred_domain.get("metaKey") or ""),
                "value": str(preferred_domain.get("value") or ""),
            }
        items.append(item)
    return {
        "placements": items,
        "page": {"limit": limit, "offset": offset, "total": len(records)},
    }


def handle_dashboard_lae_admin(
    token: str,
    resource: str,
    raw_query: str = "",
) -> Dict[str, Any]:
    # Authenticate with the Control management token before parsing resource or
    # query details and before reading the server-side LAE admin token file.
    state = load_state()
    require_token(state, token, token_type="deploy")
    limit, offset = _parse_dashboard_lae_admin_query(resource, raw_query)
    if resource == "placements":
        return _dashboard_lae_runtime_placements(
            state,
            limit=limit,
            offset=offset,
        )
    try:
        return fetch_lae_admin_resource(
            resource,
            limit=limit,
            offset=offset,
        )
    except LaeAdminProxyError:
        raise
    except Exception:
        # Defensive boundary: an unexpected transport implementation must not
        # copy its URL, headers, body, or token into Control's error/log path.
        raise LaeAdminProxyError(_LAE_ADMIN_UNAVAILABLE_ERROR) from None


def _lae_admin_proxy_http_status(exc: LaeAdminProxyError) -> int:
    return 400 if str(exc) == _LAE_ADMIN_QUERY_ERROR else 503


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


def handle_dashboard_runtime_events(token: str, service_name: str) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    service = service_name.strip()
    if not service:
        raise LumaError("service is required")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    client = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    job_id, task_filter = _nomad_log_target_from_state(state, service)
    job, allocations, job_id, task_filter = _runtime_job_and_allocations(client, job_id, task_filter, service)
    image, task_name = _nomad_job_task_image(job, task_filter or job_id)
    if not image and not task_filter:
        image, task_name = _nomad_job_task_image(job, "")
    allocation = _latest_nomad_allocation_for_task(allocations, task_name)
    if not allocation:
        return {
            "service": service,
            "job": job_id,
            "task": task_name,
            "image": image,
            "status": "unknown",
            "events": [],
            "updatedAt": int(time.time()),
        }
    alloc_id = str(allocation.get("ID") or "")
    allocation_detail = _nomad_allocation_detail(client, alloc_id) or allocation
    task_state = _allocation_task_state(allocation_detail, task_name)
    node_name = _luma_node_name_for_allocation(state, allocation_detail) or _luma_node_name_for_allocation(state, allocation)
    status = str(task_state.get("State") or allocation_detail.get("ClientStatus") or allocation.get("ClientStatus") or "")
    events = _runtime_task_events(task_state, allocation_detail, task_name)
    events.extend(_node_recent_pull_events(state, node_name, alloc_id=alloc_id, task_name=task_name, image=image))
    return {
        "service": service,
        "job": job_id,
        "task": task_name,
        "allocId": alloc_id,
        "node": node_name or str(allocation_detail.get("NodeName") or allocation.get("NodeName") or ""),
        "image": image,
        "status": status or "unknown",
        "events": _dedupe_runtime_events(events)[-80:],
        "updatedAt": int(time.time()),
    }


def _lae_runtime_state(state: Dict[str, Any]) -> Dict[str, Any]:
    runtime = state.setdefault("laeRuntime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        state["laeRuntime"] = runtime
    for key in (
        "volumes",
        "volumeIdempotency",
        "deployments",
        "deploymentIdempotency",
        "lifecycleIdempotency",
        "applicationBindings",
        "hostnameBindings",
    ):
        if not isinstance(runtime.get(key), dict):
            runtime[key] = {}
    return runtime


def _lae_runtime_binding_matches(
    value: Any, binding: RuntimeBinding, *, principal_ref: str
) -> bool:
    if not isinstance(value, dict):
        return False
    expected = {"principalRef": principal_ref, **binding.state_body()}
    return all(str(value.get(key) or "") == expected_value for key, expected_value in expected.items())


def _lae_runtime_volume_owner_matches(
    value: Any, binding: RuntimeBinding, *, principal_ref: str
) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        str(value.get("principalRef") or "") == principal_ref
        and str(value.get("tenantRef") or "") == binding.tenant_ref
        and str(value.get("applicationRef") or "") == binding.application_ref
    )


def _lae_runtime_prune(state: Dict[str, Any], *, now: int) -> None:
    runtime = _lae_runtime_state(state)
    retention = max(int(LAE_RUNTIME_RECORD_RETENTION_SECONDS), 3600)
    # A running/degraded record is the saved manifest and runtime identity used
    # by lifecycle resume/restart. Never age an active application out of the
    # control state merely because it has been healthy for a long time.
    terminal = {"failed", "canceled", "deleted"}
    expired_deployments = {
        deployment_ref
        for deployment_ref, record in runtime["deployments"].items()
        if isinstance(record, dict)
        and str(record.get("status") or "") in terminal
        and int(record.get("updatedAt") or record.get("createdAt") or 0)
        < now - retention
    }
    for deployment_ref in expired_deployments:
        runtime["deployments"].pop(deployment_ref, None)
    for bucket_name in (
        "volumeIdempotency",
        "deploymentIdempotency",
        "lifecycleIdempotency",
    ):
        bucket = runtime[bucket_name]
        for key, record in list(bucket.items()):
            if (
                not isinstance(record, dict)
                or int(record.get("expiresAt") or 0) <= now
                or (
                    bucket_name in {"deploymentIdempotency", "lifecycleIdempotency"}
                    and str(record.get("runtimeDeploymentRef") or "")
                    in expired_deployments
                )
            ):
                bucket.pop(key, None)


def _lae_runtime_idempotency_scope(
    principal_ref: str,
    binding: RuntimeBinding,
    route: str,
    idempotency_key: str,
) -> str:
    return _lae_runtime_hash(
        {
            "principalRef": principal_ref,
            **binding.state_body(),
            "route": route,
            "idempotencyKey": _normalize_lae_runtime_idempotency_key(
                idempotency_key
            ),
        }
    )


def _lae_runtime_storage_class(
    state: Dict[str, Any], *, region: str | None = None
) -> tuple[str, StorageClassSpec]:
    name = str(os.environ.get("LUMA_LAE_RUNTIME_STORAGE_CLASS") or "").strip()
    if not name:
        raise _lae_runtime_unavailable(
            "LAE managed storage is not configured"
        )
    raw = _state_storage_classes(state).get(name)
    if not isinstance(raw, dict):
        raise _lae_runtime_unavailable(
            "LAE managed storage class is unavailable"
        )
    spec = _storage_class_spec_from_record(name, raw)
    if spec.provider != "nfs" or spec.mode != "managed":
        raise _lae_runtime_unavailable(
            "LAE managed storage class is unsupported"
        )
    if region is not None and spec.regions and region not in set(spec.regions):
        raise _lae_runtime_unavailable(
            "LAE managed storage is unavailable in the deployment region"
        )
    return name, spec


def _lae_runtime_volume_ref(binding: RuntimeBinding, key: str) -> str:
    digest = hashlib.sha256(
        (
            binding.tenant_ref
            + "\0"
            + binding.application_ref
            + "\0"
            + key
        ).encode("utf-8")
    ).hexdigest()
    return "lv_" + digest[:32]


def _lae_runtime_volume_path(binding: RuntimeBinding, key: str) -> str:
    tenant = hashlib.sha256(binding.tenant_ref.encode("utf-8")).hexdigest()[:20]
    application = hashlib.sha256(
        binding.application_ref.encode("utf-8")
    ).hexdigest()[:20]
    return f"lae/tenants/{tenant}/apps/{application}/volumes/{key}"


def handle_lae_runtime_volume_prepare(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    body: Dict[str, Any],
    *,
    idempotency_key: str,
) -> Dict[str, Any]:
    volumes = _validate_lae_runtime_volume_prepare_body(body)
    request_hash = _lae_runtime_hash(
        {"schemaVersion": LAE_RUNTIME_SCHEMA_VERSION, "volumes": list(volumes)}
    )
    normalized_idempotency = _normalize_lae_runtime_idempotency_key(
        idempotency_key
    )
    now = int(time.time())

    def mutate(current: Dict[str, Any]) -> tuple[list[Dict[str, str]], bool]:
        principal = _require_lae_runtime_principal(
            current,
            token,
            audience=audience,
            scope=SCOPE_VOLUMES_PREPARE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        _lae_runtime_prune(current, now=now)
        runtime = _lae_runtime_state(current)
        scope = _lae_runtime_idempotency_scope(
            principal_ref,
            binding,
            "POST /v1/lae/runtime/volumes:prepare",
            normalized_idempotency,
        )
        existing_idempotency = runtime["volumeIdempotency"].get(scope)
        if isinstance(existing_idempotency, dict):
            if not secrets.compare_digest(
                str(existing_idempotency.get("requestHash") or ""),
                request_hash,
            ):
                raise _lae_runtime_conflict()
            refs = existing_idempotency.get("volumeRefs")
            if isinstance(refs, list):
                result: list[Dict[str, str]] = []
                for ref in refs:
                    record = runtime["volumes"].get(str(ref))
                    if not isinstance(record, dict) or not _lae_runtime_binding_matches(
                        record, binding, principal_ref=principal_ref
                    ):
                        raise _lae_runtime_conflict(
                            "volume idempotency record is unavailable"
                        )
                    result.append(
                        {
                            "key": str(record["key"]),
                            "volumeRef": str(record["volumeRef"]),
                        }
                    )
                return result, True

        storage_class_name = ""
        if volumes:
            storage_class_name, _ = _lae_runtime_storage_class(current)
        bindings: list[Dict[str, str]] = []
        refs: list[str] = []
        for volume in volumes:
            key = str(volume["key"])
            volume_ref = _lae_runtime_volume_ref(binding, key)
            existing_ref = str(volume.get("existingRef") or "")
            if existing_ref and existing_ref != volume_ref:
                raise _lae_runtime_not_found()
            stored = runtime["volumes"].get(volume_ref)
            if isinstance(stored, dict):
                if (
                    not _lae_runtime_volume_owner_matches(
                        stored, binding, principal_ref=principal_ref
                    )
                    or str(stored.get("key") or "") != key
                    or str(stored.get("storageClass") or "")
                    != storage_class_name
                    or int(volume["requestedBytes"])
                    < int(stored.get("requestedBytes") or 0)
                    or str(stored.get("accessMode") or "")
                    != str(volume["accessMode"])
                    or list(stored.get("mounts") or [])
                    != list(volume["mounts"])
                ):
                    raise _lae_runtime_conflict(
                        "managed volume binding is immutable"
                    )
                stored["requestedBytes"] = int(volume["requestedBytes"])
                stored.update(binding.state_body())
                stored["updatedAt"] = now
            else:
                if existing_ref:
                    raise _lae_runtime_not_found()
                runtime["volumes"][volume_ref] = {
                    "volumeRef": volume_ref,
                    "principalRef": principal_ref,
                    **binding.state_body(),
                    "key": key,
                    "requestedBytes": int(volume["requestedBytes"]),
                    "accessMode": str(volume["accessMode"]),
                    "mounts": list(volume["mounts"]),
                    "storageClass": storage_class_name,
                    "path": _lae_runtime_volume_path(binding, key),
                    "status": "prepared",
                    "createdAt": now,
                    "updatedAt": now,
                }
            refs.append(volume_ref)
            bindings.append({"key": key, "volumeRef": volume_ref})
        runtime["volumeIdempotency"][scope] = {
            "requestHash": request_hash,
            "volumeRefs": refs,
            "createdAt": now,
            "expiresAt": now + max(int(LAE_RUNTIME_IDEMPOTENCY_SECONDS), 60),
        }
        return bindings, False

    bindings, replayed = _mutate_control_state(mutate)
    return {
        "schemaVersion": LAE_RUNTIME_SCHEMA_VERSION,
        "volumes": bindings,
        "replayed": replayed,
    }


def handle_lae_runtime_secret_issue(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    body: Dict[str, Any],
    *,
    idempotency_key: str,
) -> Dict[str, Any]:
    request = _validate_lae_runtime_secret_issue_body(body)
    state = load_state()
    principal = _require_lae_runtime_principal(
        state,
        token,
        audience=audience,
        scope=SCOPE_SECRETS_ISSUE,
        binding=binding,
    )
    lease, replayed = RUNTIME_SECRETS.issue(
        principal_ref=str(principal["id"]),
        binding=binding,
        request=request,
        idempotency_key=idempotency_key,
    )
    return {
        "schemaVersion": LAE_RUNTIME_SCHEMA_VERSION,
        "replayed": replayed,
        "secret": lease.public_body(),
    }


def _lae_runtime_application_scope(
    principal_ref: str, binding: RuntimeBinding
) -> str:
    return _lae_runtime_hash(
        {
            "principalRef": principal_ref,
            "tenantRef": binding.tenant_ref,
            "applicationRef": binding.application_ref,
        }
    )


def _lae_runtime_job_slug(binding: RuntimeBinding) -> str:
    digest = hashlib.sha256(
        (binding.tenant_ref + "\0" + binding.application_ref).encode("utf-8")
    ).hexdigest()
    return "lae-" + digest[:28]


def _lae_runtime_task_name(service_key: str, revision_ref: str) -> str:
    revision = hashlib.sha256(revision_ref.encode("utf-8")).hexdigest()[:10]
    return f"{service_key}-r{revision}"


def _lae_runtime_variable_path(
    job_slug: str, group_name: str, task_name: str
) -> str:
    return f"nomad/jobs/{job_slug}/{group_name}/{task_name}"


def _lae_runtime_resolve_images(
    state: Dict[str, Any],
    builder_principal_refs: set[str],
    binding: RuntimeBinding,
    manifest: Dict[str, Any],
) -> Dict[str, str]:
    images: Dict[str, str] = {}
    for service in manifest["services"]:
        image_binding = service["image"]
        task_id = str(image_binding["builderTaskRef"])
        task = _builder_tasks(state).get(task_id)
        if (
            not isinstance(task, dict)
            or str(task.get("principalRef") or "") not in builder_principal_refs
            or str(task.get("tenantRef") or "") != binding.tenant_ref
            or str(task.get("applicationRef") or "") != binding.application_ref
            or str(task.get("externalOperationId") or "") != binding.operation_ref
            or str(task.get("kind") or "") != "build-plan"
            or str(task.get("status") or "") != "succeeded"
        ):
            raise _lae_runtime_not_found()
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        result_images = result.get("images") if isinstance(result.get("images"), dict) else {}
        result_digests = (
            result.get("imageDigests")
            if isinstance(result.get("imageDigests"), dict)
            else {}
        )
        build_key = str(image_binding["buildKey"])
        supplied_digest = str(image_binding["imageDigest"])
        resolved_image = str(result_images.get(build_key) or "")
        resolved_digest = str(result_digests.get(build_key) or "")
        if (
            resolved_digest != supplied_digest
            or not resolved_image.endswith("@" + supplied_digest)
        ):
            raise _lae_runtime_invalid(
                "runtime image binding does not match the verified Builder output"
            )
        images[str(service["key"])] = resolved_image
    return images


def _lae_runtime_validate_volumes(
    state: Dict[str, Any],
    principal_ref: str,
    binding: RuntimeBinding,
    manifest: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    runtime = _lae_runtime_state(state)
    result: Dict[str, Dict[str, Any]] = {}
    for volume in manifest["volumes"]:
        volume_ref = str(volume.get("existingRef") or "")
        record = runtime["volumes"].get(volume_ref)
        if (
            not isinstance(record, dict)
            or not _lae_runtime_binding_matches(
                record, binding, principal_ref=principal_ref
            )
            or str(record.get("key") or "") != str(volume["key"])
            or int(record.get("requestedBytes") or 0)
            != int(volume["requestedBytes"])
            or str(record.get("accessMode") or "")
            != str(volume["accessMode"])
            or list(record.get("mounts") or []) != list(volume["mounts"])
        ):
            raise _lae_runtime_not_found()
        result[str(volume["key"])] = dict(record)
    return result


def _lae_runtime_hostname_conflicts(
    state: Dict[str, Any],
    *,
    job_slug: str,
    hostnames: set[str],
) -> bool:
    deployments = _deployments_state(state)
    for bucket in (deployments["services"], deployments["compose"]):
        for slug, record in bucket.items():
            if str(slug) == job_slug or not isinstance(record, dict):
                continue
            data = _safe_manifest_dict(record.get("manifest"))
            candidates: set[str] = set()
            domain = data.get("domain")
            if isinstance(domain, str):
                candidates.add(domain)
            services = data.get("services") if isinstance(data.get("services"), dict) else {}
            for service in services.values():
                if isinstance(service, dict) and isinstance(service.get("domain"), str):
                    candidates.add(str(service["domain"]))
            if candidates & hostnames:
                return True
    return False


def _lae_runtime_compose_spec(
    state: Dict[str, Any],
    binding: RuntimeBinding,
    manifest: Dict[str, Any],
    images: Dict[str, str],
    volume_records: Dict[str, Dict[str, Any]],
    *,
    job_slug: str,
    placement: PlacementDecision | None = None,
) -> ComposeDeploymentSpec:
    runtime_storage_class = ""
    if manifest["volumes"]:
        runtime_storage_class, _ = _lae_runtime_storage_class(
            state, region=str(manifest["region"])
        )
        if any(
            str(record.get("storageClass") or "")
            != runtime_storage_class
            for record in volume_records.values()
        ):
            raise _lae_runtime_conflict(
                "managed storage class binding changed"
            )
    routes = {str(item["serviceKey"]): item for item in manifest["routes"]}
    compose_services: Dict[str, Any] = {}
    sidecar_services: Dict[str, ComposeServiceSpec] = {}
    for service in manifest["services"]:
        key = str(service["key"])
        body: Dict[str, Any] = {
            "image": images[key],
            "depends_on": list(service["dependencies"]),
            "deploy": {
                "resources": {
                    "limits": {
                        "cpus": str(service["resources"]["cpu"]),
                        "memory": f"{int(service['resources']['memoryMiB'])}M",
                    },
                    "reservations": {
                        "cpus": str(service["resources"]["cpu"]),
                        "memory": f"{int(service['resources']['memoryMiB'])}M",
                    },
                }
            },
        }
        if service.get("command") is not None:
            body["command"] = str(service["command"])
        if service.get("healthcheck") is not None:
            health = service["healthcheck"]
            body["healthcheck"] = {
                "test": [
                    "CMD",
                    "wget",
                    "--spider",
                    "-q",
                    f"http://127.0.0.1:{int(service['port'])}{health['path']}",
                ],
                "interval": f"{int(health['intervalSeconds'])}s",
            }
        mounts: list[Dict[str, Any]] = []
        for volume in manifest["volumes"]:
            for mount in volume["mounts"]:
                if mount["serviceKey"] != key:
                    continue
                mounts.append(
                    {
                        "type": "volume",
                        "source": str(volume["key"]),
                        "target": str(mount["mountPath"]),
                        "read_only": bool(mount["readOnly"]),
                    }
                )
        if mounts:
            body["volumes"] = mounts
        route = routes.get(key)
        if route is not None:
            body["expose"] = [int(route["containerPort"])]
        compose_services[key] = body
        sidecar_services[key] = ComposeServiceSpec(
            name=key,
            region=str(manifest["region"]),
            exposure=(str(route["exposure"]) if route is not None else "none"),
            domain=(str(route["hostname"]) if route is not None else None),
            port=(int(route["containerPort"]) if route is not None else None),
            replicas=1,
        )

    selected_storage_classes: Dict[str, StorageClassSpec] = {}
    compose_volumes: Dict[str, Any] = {}
    sidecar_volumes: Dict[str, ComposeVolumeSpec] = {}
    for volume in manifest["volumes"]:
        key = str(volume["key"])
        stored = volume_records[key]
        class_name = str(stored["storageClass"])
        raw_class = _state_storage_classes(state).get(class_name)
        if not isinstance(raw_class, dict):
            raise _lae_runtime_unavailable(
                "LAE managed storage class is unavailable"
            )
        selected_storage_classes[class_name] = _storage_class_spec_from_record(
            class_name, raw_class
        )
        compose_volumes[key] = {}
        sidecar_volumes[key] = ComposeVolumeSpec(
            name=key,
            storage_class=class_name,
            path=str(stored["path"]),
            access_mode=str(volume["accessMode"]),
        )

    compose = {
        "services": compose_services,
        **({"volumes": compose_volumes} if compose_volumes else {}),
    }
    spec = ComposeDeploymentSpec(
        source=Path("lae-runtime"),
        compose_path=Path("lae-runtime/docker-compose.yml"),
        compose=compose,
        name=job_slug,
        region=str(manifest["region"]),
        storage_classes=selected_storage_classes,
        volumes=sidecar_volumes,
        services=sidecar_services,
        warnings=[],
    )
    # This validates region/storage reachability and rejects any accidental
    # local/host backend before a job is rendered.
    resolve_storage_mounts(
        spec,
        node_records=_state_nodes(state),
        admitted_nodes=(placement.candidate_node_names if placement else ()),
    )
    return spec


def _lae_runtime_safe_compose_payload(
    spec: ComposeDeploymentSpec,
) -> tuple[str, str, Dict[str, Any]]:
    compose_content = yaml.safe_dump(
        spec.compose, sort_keys=True, allow_unicode=False
    )
    sidecar = {
        "name": spec.name,
        "compose": "docker-compose.yml",
        "region": spec.region,
        "volumes": {
            key: {
                "storageClass": volume.storage_class,
                "path": volume.path,
                "accessMode": volume.access_mode,
            }
            for key, volume in spec.volumes.items()
        },
        "services": {
            key: {
                "region": service.region or spec.region,
                "exposure": service.exposure,
                **({"domain": service.domain} if service.domain else {}),
                **({"port": service.port} if service.port else {}),
            }
            for key, service in spec.services.items()
        },
    }
    sidecar_text = yaml.safe_dump(sidecar, sort_keys=True, allow_unicode=False)
    return sidecar_text, compose_content, {
        "manifest": sidecar_text,
        "composeContent": compose_content,
        "sourceName": "lae.runtime.compose.yml",
        "origin": "lae-runtime",
    }


def _lae_runtime_template(path: str, names: list[str]) -> Dict[str, Any]:
    lines = [f'{{{{ with nomadVar "{path}" }}}}']
    for name in sorted(names):
        lines.append(f"{name}={{{{ .{name}.Value | toJSON }}}}")
    lines.append("{{ end }}")
    return {
        "DestPath": "secrets/lae-runtime.env",
        "EmbeddedTmpl": "\n".join(lines) + "\n",
        "Envvars": True,
        "ChangeMode": "restart",
        "Perms": "0600",
    }


def _raise_lae_runtime_placement_failure(exc: PlacementFailure) -> None:
    """Translate internal topology failures to stable, non-sensitive errors."""

    if exc.reason == LAE_PLACEMENT_NO_CAPACITY:
        raise LumaRuntimeError(
            "runtime capacity is temporarily unavailable in this region",
            status=503,
            code="capacity_unavailable",
        ) from exc
    if exc.reason == LAE_PLACEMENT_VOLUME_INCOMPATIBLE:
        raise LumaRuntimeError(
            "managed volume is incompatible with runtime placement",
            status=409,
            code="volume_placement_incompatible",
        ) from exc
    raise LumaRuntimeError(
        "runtime placement is temporarily unavailable",
        status=503,
        code="placement_unavailable",
    ) from exc


def _lae_runtime_node_allowlist() -> tuple[str, ...]:
    """Load the positive-admission runner set without exposing topology.

    A generic ready Docker node is not sufficient evidence that the host is
    isolated for untrusted tenant workloads.  The operator must explicitly
    admit every LAE runtime node; missing or ambiguous configuration fails
    closed as a placement control-plane outage.
    """

    raw = str(
        os.environ.get("LUMA_LAE_RUNTIME_NODE_ALLOWLIST_JSON") or ""
    ).strip()
    try:
        configured = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise PlacementFailure(LAE_PLACEMENT_UNAVAILABLE) from None
    if (
        not isinstance(configured, list)
        or not configured
        or len(configured) > 64
        or any(not isinstance(item, str) for item in configured)
    ):
        raise PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
    nodes = [str(item) for item in configured]
    if (
        nodes != sorted(nodes)
        or len(nodes) != len(set(nodes))
        or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) is None
            for item in nodes
        )
    ):
        raise PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
    return tuple(nodes)


def _lae_runtime_nomad_placement_nodes(
    config: LumaConfig,
    state: Dict[str, Any],
) -> tuple[NomadApi, list[Dict[str, Any]]]:
    """Read current Nomad node detail without projecting it to LAE callers."""

    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    try:
        summaries = client.request("GET", "/v1/nodes")
    except LumaError as exc:
        _raise_lae_runtime_placement_failure(
            PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
        )
        raise AssertionError("unreachable") from exc
    if not isinstance(summaries, list):
        _raise_lae_runtime_placement_failure(
            PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
        )
    result: list[Dict[str, Any]] = []
    detail_failures = 0
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        node_id = str(summary.get("ID") or "").strip()
        if not node_id:
            continue
        try:
            detail = client.request(
                "GET", f"/v1/node/{urllib.parse.quote(node_id, safe='')}"
            )
        except LumaError:
            # One unreachable/down node must not block rescheduling to another
            # ready runtime node. If every detail lookup fails, the snapshot is
            # unusable and placement fails closed below.
            detail_failures += 1
            continue
        if not isinstance(detail, dict):
            detail_failures += 1
            continue
        merged = dict(summary)
        merged.update(detail)
        merged["ID"] = node_id
        result.append(merged)
    if not result and detail_failures:
        _raise_lae_runtime_placement_failure(
            PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
        )
    return client, result


def _lae_runtime_prior_nomad_node(
    client: NomadApi,
    job_slug: str,
) -> str:
    try:
        allocations = client.request(
            "GET",
            f"/v1/job/{urllib.parse.quote(job_slug, safe='')}/allocations",
        )
    except LumaError as exc:
        if "Nomad API error 404" in str(exc):
            return ""
        _raise_lae_runtime_placement_failure(
            PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
        )
        raise AssertionError("unreachable") from exc
    if not isinstance(allocations, list):
        _raise_lae_runtime_placement_failure(
            PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
        )
    active: list[Dict[str, Any]] = []
    for raw in allocations:
        if not isinstance(raw, dict) or not str(raw.get("NodeID") or "").strip():
            continue
        desired = str(raw.get("DesiredStatus") or "").lower()
        client_status = str(raw.get("ClientStatus") or "").lower()
        if desired in {"run", ""} and client_status in {
            "running",
            "pending",
            "starting",
            "unknown",
            "",
        }:
            active.append(raw)
    active.sort(
        key=lambda value: (
            int(value.get("ModifyIndex") or 0),
            int(value.get("CreateIndex") or 0),
            str(value.get("ID") or ""),
        ),
        reverse=True,
    )
    return str(active[0].get("NodeID") or "") if active else ""


def _lae_runtime_plan_placement(
    config: LumaConfig,
    state: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    job_slug: str,
) -> tuple[PlacementDecision, NomadApi]:
    client, nomad_nodes = _lae_runtime_nomad_placement_nodes(config, state)
    prior_node_id = _lae_runtime_prior_nomad_node(client, job_slug)
    storage_class: Dict[str, Any] | None = None
    if manifest["volumes"]:
        try:
            class_name, _ = _lae_runtime_storage_class(
                state, region=str(manifest["region"])
            )
        except LumaRuntimeError as exc:
            _raise_lae_runtime_placement_failure(
                PlacementFailure(LAE_PLACEMENT_VOLUME_INCOMPATIBLE)
            )
            raise AssertionError("unreachable") from exc
        raw_class = _state_storage_classes(state).get(class_name)
        if isinstance(raw_class, dict):
            storage_class = {"name": class_name, **raw_class}
    try:
        allowed_runtime_nodes = _lae_runtime_node_allowlist()
        decision = plan_lae_placement(
            manifest=manifest,
            registered_nodes=(
                state.get("nodes")
                if isinstance(state.get("nodes"), dict)
                else {}
            ),
            nomad_nodes=nomad_nodes,
            declared_builder_nodes=_declared_build_node_names(state),
            allowed_runtime_nodes=allowed_runtime_nodes,
            storage_class=storage_class,
            prior_node_id=prior_node_id,
            agent_stale_seconds=AGENT_STALE_SECONDS,
        )
    except PlacementFailure as exc:
        _raise_lae_runtime_placement_failure(exc)
        raise AssertionError("unreachable") from exc
    return decision, client


def _lae_runtime_validate_placement_plan(
    client: NomadApi,
    *,
    job_slug: str,
    stack_text: str,
) -> None:
    try:
        rendered = json.loads(stack_text)
        job = rendered.get("Job") if isinstance(rendered, dict) else None
        if not isinstance(job, dict):
            raise ValueError("missing Job")
        plan = client.request(
            "POST",
            f"/v1/job/{urllib.parse.quote(job_slug, safe='')}/plan",
            {"Job": job, "Diff": False, "PolicyOverride": False},
        )
        _validate_lae_nomad_plan(plan)
    except PlacementFailure as exc:
        _raise_lae_runtime_placement_failure(exc)
    except (LumaError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _raise_lae_runtime_placement_failure(
            PlacementFailure(LAE_PLACEMENT_UNAVAILABLE)
        )
        raise AssertionError("unreachable") from exc


def _lae_runtime_render_job(
    state: Dict[str, Any],
    config: LumaConfig,
    binding: RuntimeBinding,
    manifest: Dict[str, Any],
    spec: ComposeDeploymentSpec,
    secret_values: Dict[str, str],
    *,
    runtime_deployment_ref: str,
    placement: PlacementDecision | None = None,
) -> tuple[str, Dict[str, Dict[str, str]], list[str]]:
    runtime_config = _config_with_state_nodes(config, state)
    rendered = render_compose_job(
        runtime_config,
        spec,
        as_json=False,
        registry_auth_resolver=lambda image: _registry_auth_for_image(state, image),
        resolve_secrets=False,
        egress_proxy_url=_egress_proxy_for_region(
            config, state, str(manifest["region"])
        ),
        node_records=_state_nodes(state),
        admitted_nodes=(placement.candidate_node_names if placement else ()),
        render_storage=False,
    )
    if not isinstance(rendered, dict) or not isinstance(rendered.get("Job"), dict):
        raise _lae_runtime_unavailable("Luma runtime renderer is unavailable")
    job = rendered["Job"]
    groups = job.get("TaskGroups") if isinstance(job.get("TaskGroups"), list) else []
    if len(groups) != 1 or not isinstance(groups[0], dict):
        raise _lae_runtime_unavailable("Luma runtime renderer is unavailable")
    group = groups[0]
    # Tenant images are intentionally pulled from the Builder-owned registry on
    # first placement.  That cold pull can take materially longer than the
    # generic Compose three-minute healthy deadline, especially for multi-GB
    # images or a runtime node connected over the tailnet.  Once Nomad marks an
    # allocation unhealthy for missing HealthyDeadline it never promotes that
    # allocation later, even if every task subsequently starts.  Give LAE
    # runtime jobs an explicit cold-pull window while keeping the operation
    # bounded by ProgressDeadline and LAE's own verification timeout.
    update = group.get("Update") if isinstance(group.get("Update"), dict) else {}
    update.update(
        {
            "HealthyDeadline": 1_800_000_000_000,  # 30m in ns
            "ProgressDeadline": 2_400_000_000_000,  # 40m in ns
        }
    )
    group["Update"] = update
    group_name = str(group.get("Name") or spec.slug)
    tasks = group.get("Tasks") if isinstance(group.get("Tasks"), list) else []
    task_names: Dict[str, str] = {}
    variable_items: Dict[str, Dict[str, str]] = {}
    refs_by_service: Dict[str, list[Dict[str, Any]]] = {}
    for item in manifest["secretRefs"]:
        refs_by_service.setdefault(str(item["serviceKey"]), []).append(item)

    storage_mounts = resolve_storage_mounts(
        spec,
        node_records=_state_nodes(state),
        admitted_nodes=(placement.candidate_node_names if placement else ()),
    )
    endpoint_by_volume = {
        str(item["volume"]): str(item["endpoint"])
        for item in storage_mounts
    }
    application_volume_prefix = hashlib.sha256(
        binding.application_ref.encode("utf-8")
    ).hexdigest()[:16]
    variable_paths: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise _lae_runtime_unavailable("Luma runtime renderer is unavailable")
        service_key = str(task.get("Name") or "")
        if service_key not in {str(item["key"]) for item in manifest["services"]}:
            raise _lae_runtime_unavailable("Luma runtime renderer is unavailable")
        runtime_task_name = _lae_runtime_task_name(
            service_key, binding.revision_ref
        )
        task["Name"] = runtime_task_name
        task_names[service_key] = runtime_task_name
        config_body = task.get("Config") if isinstance(task.get("Config"), dict) else {}
        mounts = config_body.get("mount") if isinstance(config_body.get("mount"), list) else []
        for mount in mounts:
            if not isinstance(mount, dict) or str(mount.get("type") or "") != "volume":
                raise _lae_runtime_invalid("LAE runtime rejected a non-managed mount")
            volume_key = str(mount.get("source") or "")
            volume = spec.volumes.get(volume_key)
            storage_class = (
                spec.storage_classes.get(str(volume.storage_class))
                if volume is not None
                else None
            )
            endpoint = endpoint_by_volume.get(volume_key)
            if volume is None or storage_class is None or endpoint is None:
                raise _lae_runtime_invalid("LAE runtime volume binding is invalid")
            driver = render_storage_class_volume(
                storage_class, volume, endpoint
            )
            mount["source"] = (
                f"lae-{application_volume_prefix}-{slugify(volume_key)}"
            )
            mount["volume_options"] = {
                "no_copy": False,
                "driver_config": {
                    "name": str(driver.get("driver") or "local"),
                    # Nomad's Docker driver accepts ``options`` as a map in
                    # HCL, but the task-driver JSON representation requires
                    # maps to be wrapped in a single-element list. Sending a
                    # bare object is accepted by the jobs API and then fails
                    # on the client before container creation.
                    "options": [dict(driver.get("driver_opts") or {})],
                },
            }
        refs = refs_by_service.get(service_key, [])
        if refs:
            path = _lae_runtime_variable_path(
                spec.slug, group_name, runtime_task_name
            )
            items: Dict[str, str] = {}
            for item in refs:
                ref = str(item["secretRef"])
                if ref not in secret_values:
                    raise _lae_runtime_invalid(
                        "runtime secret reference is unavailable"
                    )
                items[str(item["name"])] = secret_values[ref]
            task["Templates"] = [
                _lae_runtime_template(path, list(items))
            ]
            variable_items[path] = items
            variable_paths.append(path)

    route_by_service = {
        str(item["serviceKey"]): item for item in manifest["routes"]
    }
    manifest_service_by_key = {
        str(item["key"]): item for item in manifest["services"]
    }
    for service_block in group.get("Services") or []:
        if not isinstance(service_block, dict):
            continue
        route = route_by_service.get(str(service_block.get("Name") or ""))
        if route is None:
            continue
        manifest_service = manifest_service_by_key.get(
            str(service_block.get("Name") or ""), {}
        )
        healthcheck = (
            manifest_service.get("healthcheck")
            if isinstance(manifest_service, dict)
            and isinstance(manifest_service.get("healthcheck"), dict)
            else {}
        )
        service_block["Checks"] = [
            {
                "Name": "lae-http-health",
                "Type": "http",
                "Path": str(route["healthPath"]),
                "Interval": int(
                    healthcheck.get("intervalSeconds") or 10
                )
                * 1_000_000_000,
                "Timeout": 3_000_000_000,
            }
        ]
    meta = job.get("Meta") if isinstance(job.get("Meta"), dict) else {}
    meta.update(
        {
            "luma.lae": "true",
            "luma.lae.tenant": binding.tenant_ref,
            "luma.lae.application": binding.application_ref,
            "luma.lae.operation": binding.operation_ref,
            "luma.lae.revision": binding.revision_ref,
            "luma.lae.deployment": binding.deployment_ref,
            "luma.lae.runtimeDeployment": runtime_deployment_ref,
            "luma.lae.manifestDigest": str(manifest["manifestDigest"]),
        }
    )
    job["Meta"] = meta
    if placement is not None:
        try:
            placement.apply_to_job(rendered)
        except PlacementFailure as exc:
            _raise_lae_runtime_placement_failure(exc)
    # ``variable_items`` is returned separately and is never serialized into
    # the job or control state.
    return (
        json.dumps(rendered, indent=2, sort_keys=True, ensure_ascii=True),
        variable_items,
        variable_paths,
    )


def _lae_runtime_nomad_job_version(
    config: LumaConfig, state: Dict[str, Any], slug: str
) -> int | None:
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    try:
        value = client.request(
            "GET", f"/v1/job/{urllib.parse.quote(slug, safe='')}"
        )
    except LumaError as exc:
        if "Nomad API error 404" in str(exc):
            return None
        raise _lae_runtime_unavailable("Nomad runtime is unavailable") from exc
    if not isinstance(value, dict):
        raise _lae_runtime_unavailable("Nomad runtime is unavailable")
    version = value.get("Version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _lae_runtime_unavailable("Nomad runtime is unavailable")
    return version


def _lae_runtime_submit_nomad_job(
    config: LumaConfig,
    state: Dict[str, Any],
    stack_text: str,
    *,
    job_slug: str,
) -> Dict[str, Any]:
    """Register an exact LAE revision without blocking the Control request.

    LAE already observes the immutable revision task names, metadata, service
    health, and public routes through ``GET /runtime/deployments/{ref}``.  The
    generic management deploy path instead waits synchronously for Nomad's
    rollout barrier, which kept the runtime record in ``preparing`` for up to
    15 minutes and made every status request return 503.  Persist the exact
    registration correlation immediately and let the dedicated observer own
    convergence.
    """

    try:
        parsed = json.loads(stack_text)
    except json.JSONDecodeError as exc:
        raise _lae_runtime_unavailable("Luma runtime renderer is unavailable") from exc
    job = parsed.get("Job") if isinstance(parsed, dict) else None
    if not isinstance(job, dict) or str(job.get("ID") or "") != job_slug:
        raise _lae_runtime_unavailable("Luma runtime renderer is unavailable")
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    response = client.request("POST", "/v1/jobs", {"Job": job})
    if not isinstance(response, dict):
        raise _lae_runtime_unavailable("Nomad runtime submission is unavailable")
    modify_index = response.get("JobModifyIndex")
    if (
        isinstance(modify_index, bool)
        or not isinstance(modify_index, int)
        or modify_index <= 0
    ):
        raise _lae_runtime_unavailable("Nomad runtime submission is unavailable")
    detail = client.request(
        "GET", f"/v1/job/{urllib.parse.quote(job_slug, safe='')}"
    )
    if not isinstance(detail, dict):
        raise _lae_runtime_unavailable("Nomad runtime submission is unavailable")
    observed_index = detail.get("JobModifyIndex")
    version = detail.get("Version")
    if (
        observed_index != modify_index
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 0
    ):
        raise _lae_runtime_unavailable("Nomad runtime submission correlation failed")
    evaluation_id = str(response.get("EvalID") or "").strip()
    return {
        "nomadVersion": version,
        "nomadJobModifyIndex": modify_index,
        **({"nomadEvaluationId": evaluation_id} if evaluation_id else {}),
    }


def _install_lae_runtime_variables(
    config: LumaConfig,
    state: Dict[str, Any],
    variable_items: Dict[str, Dict[str, str]],
) -> None:
    if not variable_items:
        return
    # A Nomad management token is required only when Nomad ACLs are enabled.
    # ACL-disabled clusters legitimately use the Variables API without a
    # token, just like the plan and deploy paths below.  Rejecting an empty
    # token here made LAE the sole deployment path that could not operate on
    # an otherwise healthy ACL-disabled Luma cluster.  Let Nomad remain the
    # authority: an ACL-enabled server will reject the unauthenticated write
    # and the error is converted to the same closed LAE availability error.
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    written: list[str] = []
    try:
        for path, items in variable_items.items():
            client.put_variable(path, items)
            written.append(path)
    except LumaError as exc:
        for path in written:
            try:
                client.delete_variable(path)
            except LumaError:
                pass
        raise _lae_runtime_unavailable(
            "secure runtime secret installation is unavailable"
        ) from exc


def _delete_lae_runtime_variables(
    config: LumaConfig, state: Dict[str, Any], paths: list[str]
) -> None:
    if not paths:
        return
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    for path in paths:
        try:
            client.delete_variable(path)
        except LumaError as exc:
            if "Nomad API error 404" in str(exc):
                continue
            raise _lae_runtime_unavailable(
                "secure runtime secret cleanup is unavailable"
            ) from exc


def _execute_lae_runtime_deployment(
    *,
    token: str,
    audience: str,
    principal_ref: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    manifest: Dict[str, Any],
    images: Dict[str, str],
    volume_records: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    state = load_state()
    current_principal = _require_lae_runtime_principal(
        state,
        token,
        audience=audience,
        scope=SCOPE_DEPLOYMENTS_WRITE,
        binding=binding,
    )
    if str(current_principal.get("id") or "") != principal_ref:
        raise _lae_runtime_forbidden()
    secret_values = RUNTIME_SECRETS.resolve_manifest(
        principal_ref=principal_ref,
        binding=binding,
        secret_refs=list(manifest["secretRefs"]),
    )
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    job_slug = _lae_runtime_job_slug(binding)
    # Placement is a Luma-internal admission decision. The caller supplied
    # only ``region``; current ready runtime nodes, builder isolation, managed
    # volume compatibility, and prior allocation continuity are resolved here.
    placement, placement_client = _lae_runtime_plan_placement(
        config,
        state,
        manifest,
        job_slug=job_slug,
    )
    spec = _lae_runtime_compose_spec(
        state,
        binding,
        manifest,
        images,
        volume_records,
        job_slug=job_slug,
        placement=placement,
    )
    _lae_runtime_storage_class(
        state, region=str(manifest["region"])
    ) if manifest["volumes"] else None
    previous_version = _lae_runtime_nomad_job_version(config, state, job_slug)
    stack_text, variable_items, variable_paths = _lae_runtime_render_job(
        state,
        config,
        binding,
        manifest,
        spec,
        secret_values,
        runtime_deployment_ref=runtime_deployment_ref,
        placement=placement,
    )
    # Nomad's plan endpoint is the authoritative capacity check. It evaluates
    # the exact rendered group (aggregate CPU/memory/ports and the internal
    # candidate constraints) without registering the job.
    _lae_runtime_validate_placement_plan(
        placement_client,
        job_slug=job_slug,
        stack_text=stack_text,
    )
    # The plaintext exists only in ``secret_values``/``variable_items`` and the
    # encrypted Nomad Variables request. Neither mapping is returned or saved.
    _install_lae_runtime_variables(config, state, variable_items)
    try:
        _prepare_compose_managed_storage(spec, state)
        target = _resolve_control_path(
            compose_stack_path(config, spec), config_path
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stack_text, encoding="utf-8")
        dns_secrets = (
            state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
        )
        for route in manifest["routes"]:
            sync_dns(
                config,
                _compose_service_as_service_spec(
                    spec, spec.services[str(route["serviceKey"])]
                ),
                secrets=dns_secrets,
            )
        submission = _lae_runtime_submit_nomad_job(
            config,
            state,
            stack_text,
            job_slug=job_slug,
        )
    except BaseException:
        # Variables remain for an idempotent retry. They are encrypted in Nomad
        # and bound to the not-yet-running revision-specific task identities.
        raise
    sidecar_text, compose_content, normal_body = _lae_runtime_safe_compose_payload(
        spec
    )
    _mark_compose_deployment(
        spec,
        normal_body,
        "lae.runtime.compose.yml",
        status="active",
        steps=[
            {
                "name": "LAE runtime deployment",
                "status": "ok",
                "message": "Submitted through the dedicated LAE runtime API",
            }
        ],
    )
    return {
        "jobSlug": job_slug,
        "taskNames": {
            str(service["key"]): _lae_runtime_task_name(
                str(service["key"]), binding.revision_ref
            )
            for service in manifest["services"]
        },
        "variablePaths": variable_paths,
        "previousNomadVersion": previous_version,
        **submission,
        "composeManifest": sidecar_text,
        "composeContent": compose_content,
        # Control-state only. The LAE public projection intentionally omits it.
        "placement": placement.internal_state(),
    }


def _lae_runtime_deployment_public(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "deploymentRef": str(record["runtimeDeploymentRef"]),
        "status": str(record["status"]),
        "manifestDigest": str(record["manifestDigest"]),
        "serviceStatuses": dict(record.get("serviceStatuses") or {}),
        "routeStatuses": dict(record.get("routeStatuses") or {}),
        "volumeBindings": [
            {"key": str(item["key"]), "volumeRef": str(item["volumeRef"])}
            for item in record.get("volumeBindings") or []
        ],
    }


def _lae_runtime_deployment_envelope(
    record: Dict[str, Any], *, replayed: bool | None = None
) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "schemaVersion": LAE_RUNTIME_SCHEMA_VERSION,
        "deployment": _lae_runtime_deployment_public(record),
    }
    if replayed is not None:
        value["replayed"] = replayed
    return value


def _run_lae_runtime_deployment(
    *,
    token: str,
    audience: str,
    binding: RuntimeBinding,
    record: Dict[str, Any],
) -> None:
    runtime_ref = str(record["runtimeDeploymentRef"])
    try:
        with _DEPLOY_LOCK:
            execution = _execute_lae_runtime_deployment(
                token=token,
                audience=audience,
                principal_ref=str(record["principalRef"]),
                binding=binding,
                runtime_deployment_ref=runtime_ref,
                manifest=dict(record["manifest"]),
                images=dict(record["images"]),
                volume_records={
                    str(key): dict(value)
                    for key, value in dict(record["volumeRecords"]).items()
                },
            )

            def complete_submit(current: Dict[str, Any]) -> None:
                stored = _lae_runtime_state(current)["deployments"].get(runtime_ref)
                if not isinstance(stored, dict) or not _lae_runtime_binding_matches(
                    stored, binding, principal_ref=str(record["principalRef"])
                ):
                    return
                stored.update(execution)
                stored.pop("volumeRecords", None)
                stored["status"] = "deploying"
                stored["retryable"] = True
                stored["submittedAt"] = int(time.time())
                stored["updatedAt"] = int(time.time())

            _mutate_control_state(complete_submit)
    except LumaRuntimeError as exc:
        def fail_runtime(current: Dict[str, Any]) -> None:
            stored = _lae_runtime_state(current)["deployments"].get(runtime_ref)
            if isinstance(stored, dict):
                stored["status"] = "failed"
                stored["retryable"] = exc.status in {429, 503}
                stored["updatedAt"] = int(time.time())

        _mutate_control_state(fail_runtime)
    except NomadRolloutError:
        def fail_rollout(current: Dict[str, Any]) -> None:
            stored = _lae_runtime_state(current)["deployments"].get(runtime_ref)
            if isinstance(stored, dict):
                stored["status"] = "failed"
                stored["retryable"] = False
                stored["updatedAt"] = int(time.time())

        _mutate_control_state(fail_rollout)
    except (LumaError, OSError, TimeoutError):
        def fail_unavailable(current: Dict[str, Any]) -> None:
            stored = _lae_runtime_state(current)["deployments"].get(runtime_ref)
            if isinstance(stored, dict):
                stored["status"] = "failed"
                stored["retryable"] = True
                stored["updatedAt"] = int(time.time())

        _mutate_control_state(fail_unavailable)
    except Exception:
        # The request has already been accepted, so no exception can be
        # surfaced to that connection. Persist a terminal, non-retryable
        # failure instead of leaving the deployment stuck in ``preparing``.
        def fail_internal(current: Dict[str, Any]) -> None:
            stored = _lae_runtime_state(current)["deployments"].get(runtime_ref)
            if isinstance(stored, dict):
                stored["status"] = "failed"
                stored["retryable"] = False
                stored["updatedAt"] = int(time.time())

        _mutate_control_state(fail_internal)
    finally:
        with _LAE_RUNTIME_DEPLOY_THREAD_LOCK:
            _LAE_RUNTIME_DEPLOY_THREADS.pop(runtime_ref, None)


def _start_lae_runtime_deployment(
    *,
    token: str,
    audience: str,
    binding: RuntimeBinding,
    record: Dict[str, Any],
) -> None:
    runtime_ref = str(record["runtimeDeploymentRef"])
    with _LAE_RUNTIME_DEPLOY_THREAD_LOCK:
        current = _LAE_RUNTIME_DEPLOY_THREADS.get(runtime_ref)
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(
            target=_run_lae_runtime_deployment,
            kwargs={
                "token": token,
                "audience": audience,
                "binding": binding,
                "record": dict(record),
            },
            name=f"luma-lae-deploy-{runtime_ref[-12:]}",
            daemon=True,
        )
        _LAE_RUNTIME_DEPLOY_THREADS[runtime_ref] = thread
        thread.start()


@_serialize_deploy
def handle_lae_runtime_deployment_create(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    body: Dict[str, Any],
    *,
    idempotency_key: str,
) -> Dict[str, Any]:
    manifest = _validate_lae_runtime_deploy_body(body, binding)
    normalized_idempotency = _normalize_lae_runtime_idempotency_key(
        idempotency_key
    )
    logical_hash = _lae_runtime_hash(
        {
            "manifestDigest": manifest["manifestDigest"],
            "normalizedComposeDigest": manifest.get("normalizedComposeDigest"),
        }
    )
    now = int(time.time())

    def reserve(current: Dict[str, Any]) -> tuple[Dict[str, Any], bool, bool]:
        principal = _require_lae_runtime_principal(
            current,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        _lae_runtime_prune(current, now=now)
        runtime = _lae_runtime_state(current)
        idempotency_scope = _lae_runtime_idempotency_scope(
            principal_ref,
            binding,
            "POST /v1/lae/runtime/deployments",
            normalized_idempotency,
        )
        existing_idem = runtime["deploymentIdempotency"].get(
            idempotency_scope
        )
        if isinstance(existing_idem, dict):
            if not secrets.compare_digest(
                str(existing_idem.get("requestHash") or ""), logical_hash
            ):
                raise _lae_runtime_conflict()
            existing = runtime["deployments"].get(
                str(existing_idem.get("runtimeDeploymentRef") or "")
            )
            if not isinstance(existing, dict) or not _lae_runtime_binding_matches(
                existing, binding, principal_ref=principal_ref
            ):
                raise _lae_runtime_conflict(
                    "runtime deployment idempotency record is unavailable"
                )
            if str(existing.get("status") or "") in {
                "preparing",
                "failed",
            } and bool(existing.get("retryable", True)):
                existing["manifest"] = manifest
                existing["status"] = "preparing"
                existing["retryable"] = True
                existing["updatedAt"] = now
                return dict(existing), True, True
            return dict(existing), True, False

        application_scope = _lae_runtime_application_scope(
            principal_ref, binding
        )
        application = runtime["applicationBindings"].get(application_scope)
        job_slug = _lae_runtime_job_slug(binding)
        if isinstance(application, dict):
            if (
                str(application.get("name") or "") != str(manifest["name"])
                or str(application.get("jobSlug") or "") != job_slug
            ):
                raise _lae_runtime_conflict(
                    "application runtime identity is immutable"
                )
            current_ref = str(application.get("currentRuntimeDeploymentRef") or "")
            current_record = runtime["deployments"].get(current_ref)
            if isinstance(current_record, dict) and str(
                current_record.get("status") or ""
            ) in {
                "preparing",
                "deploying",
                "canceling",
                "suspending",
                "resuming",
                "restarting",
                "deleting",
            }:
                raise LumaRuntimeError(
                    "another runtime deployment is in progress",
                    status=429,
                    code="capacity_unavailable",
                )
        else:
            application = {
                "principalRef": principal_ref,
                "tenantRef": binding.tenant_ref,
                "applicationRef": binding.application_ref,
                "name": str(manifest["name"]),
                "jobSlug": job_slug,
                "createdAt": now,
            }
            runtime["applicationBindings"][application_scope] = application

        hostnames = {str(item["hostname"]) for item in manifest["routes"]}
        if _lae_runtime_hostname_conflicts(
            current, job_slug=job_slug, hostnames=hostnames
        ):
            raise _lae_runtime_conflict(
                "managed hostname is already bound"
            )
        for hostname in hostnames:
            owner = runtime["hostnameBindings"].get(hostname)
            if isinstance(owner, dict) and (
                str(owner.get("principalRef") or "") != principal_ref
                or str(owner.get("tenantRef") or "") != binding.tenant_ref
                or str(owner.get("applicationRef") or "")
                != binding.application_ref
            ):
                raise _lae_runtime_conflict(
                    "managed hostname is already bound"
                )

        images = _lae_runtime_resolve_images(
            current,
            set(
                str(item)
                for item in principal.get("builderPrincipalRefs") or []
            ),
            binding,
            manifest,
        )
        volume_records = _lae_runtime_validate_volumes(
            current, principal_ref, binding, manifest
        )
        runtime_ref = "lae-run-" + secrets.token_hex(16)
        previous_runtime_ref = str(
            application.get("currentRuntimeDeploymentRef") or ""
        )
        record: Dict[str, Any] = {
            "runtimeDeploymentRef": runtime_ref,
            "principalRef": principal_ref,
            **binding.state_body(),
            "manifest": manifest,
            "manifestDigest": str(manifest["manifestDigest"]),
            "requestHash": logical_hash,
            "status": "preparing",
            "retryable": True,
            "jobSlug": job_slug,
            "images": images,
            "volumeRecords": volume_records,
            "volumeBindings": [
                {
                    "key": str(item["key"]),
                    "volumeRef": str(item["existingRef"]),
                }
                for item in manifest["volumes"]
            ],
            "serviceStatuses": {
                str(item["key"]): "pending"
                for item in manifest["services"]
            },
            "routeStatuses": {
                str(item["hostname"]): "pending"
                for item in manifest["routes"]
            },
            "previousRuntimeDeploymentRef": previous_runtime_ref,
            "createdAt": now,
            "updatedAt": now,
        }
        runtime["deployments"][runtime_ref] = record
        runtime["deploymentIdempotency"][idempotency_scope] = {
            "requestHash": logical_hash,
            "runtimeDeploymentRef": runtime_ref,
            "createdAt": now,
            "expiresAt": now
            + max(int(LAE_RUNTIME_IDEMPOTENCY_SECONDS), 60),
        }
        application["currentRuntimeDeploymentRef"] = runtime_ref
        application["updatedAt"] = now
        for hostname in hostnames:
            runtime["hostnameBindings"][hostname] = {
                "principalRef": principal_ref,
                "tenantRef": binding.tenant_ref,
                "applicationRef": binding.application_ref,
                "runtimeDeploymentRef": runtime_ref,
                "updatedAt": now,
            }
        return dict(record), False, True

    record, replayed, execute = _mutate_control_state(reserve)
    if execute:
        _start_lae_runtime_deployment(
            token=token,
            audience=audience,
            binding=binding,
            record=record,
        )
    return _lae_runtime_deployment_envelope(record, replayed=replayed)


def _lae_runtime_probe_route(hostname: str, health_path: str) -> str:
    request = urllib.request.Request(
        f"https://{hostname}{health_path}",
        method="GET",
        headers={"User-Agent": "luma-lae-runtime-health/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError):
        return "pending"
    return "ready" if 200 <= status < 400 else "unhealthy"


def _observe_lae_runtime_deployment(
    state: Dict[str, Any], record: Dict[str, Any]
) -> Dict[str, Any]:
    if str(record.get("status") or "") in {
        "canceled",
        "failed",
        "suspended",
        "suspending",
        "rolling_back",
        "deleting",
        "deleted",
    }:
        return dict(record)
    job_slug = str(record.get("jobSlug") or "")
    manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    task_names = record.get("taskNames") if isinstance(record.get("taskNames"), dict) else {}
    if not job_slug or not manifest or not task_names:
        raise _lae_runtime_unavailable("runtime deployment state is incomplete")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    try:
        detail = client.request(
            "GET", f"/v1/job/{urllib.parse.quote(job_slug, safe='')}"
        )
    except LumaError as exc:
        if "Nomad API error 404" in str(exc):
            detail = None
        else:
            raise _lae_runtime_unavailable(
                "Nomad runtime status is unavailable"
            ) from exc
    deadline = int(record.get("submittedAt") or record.get("createdAt") or 0) + int(
        os.environ.get("LUMA_LAE_RUNTIME_VERIFY_TIMEOUT_SECONDS", "2400")
    )
    timed_out = int(time.time()) >= deadline
    if detail is None:
        value = dict(record)
        if timed_out:
            value["status"] = "failed"
            value["retryable"] = False
        return value
    if not isinstance(detail, dict):
        raise _lae_runtime_unavailable("Nomad runtime status is unavailable")
    meta = detail.get("Meta") if isinstance(detail.get("Meta"), dict) else {}
    expected_meta = {
        "luma.lae": "true",
        "luma.lae.tenant": str(record["tenantRef"]),
        "luma.lae.application": str(record["applicationRef"]),
        "luma.lae.operation": str(record["operationRef"]),
        "luma.lae.revision": str(record["revisionRef"]),
        "luma.lae.deployment": str(record["deploymentRef"]),
        "luma.lae.runtimeDeployment": str(record["runtimeDeploymentRef"]),
        "luma.lae.manifestDigest": str(record["manifestDigest"]),
    }
    if any(str(meta.get(key) or "") != expected for key, expected in expected_meta.items()):
        value = dict(record)
        value["status"] = "failed"
        value["retryable"] = False
        value["serviceStatuses"] = {
            str(item["key"]): "missing" for item in manifest["services"]
        }
        return value
    jobs = nomad_services_summary(config, state)
    job = next(
        (
            item
            for item in jobs
            if isinstance(item, dict)
            and str(item.get("jobId") or item.get("name") or "") == job_slug
        ),
        None,
    )
    if not isinstance(job, dict):
        raise _lae_runtime_unavailable("Nomad runtime status is unavailable")
    runtime_tasks = {
        str(item.get("name") or ""): item
        for item in job.get("tasks") or []
        if isinstance(item, dict)
    }
    service_statuses: Dict[str, str] = {}
    for service in manifest["services"]:
        service_key = str(service["key"])
        task = runtime_tasks.get(str(task_names.get(service_key) or ""))
        status = str(task.get("status") or "") if isinstance(task, dict) else ""
        if status in {"running", "healthy"}:
            service_statuses[service_key] = "healthy"
        elif status in {"failed", "dead", "lost"}:
            service_statuses[service_key] = "failed"
        elif status in {"pending", "starting", "queued"}:
            service_statuses[service_key] = "starting"
        else:
            service_statuses[service_key] = "missing" if timed_out else "unknown"
    route_statuses: Dict[str, str] = {}
    for route in manifest["routes"]:
        hostname = str(route["hostname"])
        if service_statuses.get(str(route["serviceKey"])) != "healthy":
            route_statuses[hostname] = "failed" if timed_out else "pending"
            continue
        observed = _lae_runtime_probe_route(hostname, str(route["healthPath"]))
        route_statuses[hostname] = (
            "failed" if observed == "unhealthy" and timed_out else observed
        )
    required = {
        str(item["key"]) for item in manifest["services"] if item["required"]
    }
    required_failed = any(
        service_statuses.get(key) in {"failed", "missing"} for key in required
    )
    routes_failed = any(value == "failed" for value in route_statuses.values())
    ready = all(service_statuses.get(key) == "healthy" for key in required) and all(
        value == "ready" for value in route_statuses.values()
    )
    value = dict(record)
    value["serviceStatuses"] = service_statuses
    value["routeStatuses"] = route_statuses
    if required_failed or routes_failed:
        value["status"] = "failed"
        value["retryable"] = False
    elif ready:
        value["status"] = "running"
        value["retryable"] = False
    else:
        value["status"] = "deploying"
    version = detail.get("Version")
    if isinstance(version, int) and not isinstance(version, bool) and version >= 0:
        value["nomadVersion"] = version
    return value


def handle_lae_runtime_deployment_get(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
) -> Dict[str, Any]:
    state = load_state()
    principal = _require_lae_runtime_principal(
        state,
        token,
        audience=audience,
        scope=SCOPE_DEPLOYMENTS_READ,
        binding=binding,
    )
    principal_ref = str(principal["id"])
    record = _lae_runtime_state(state)["deployments"].get(
        str(runtime_deployment_ref or "")
    )
    if not isinstance(record, dict) or not _lae_runtime_binding_matches(
        record, binding, principal_ref=principal_ref
    ):
        raise _lae_runtime_not_found()
    application_scope = _lae_runtime_application_scope(principal_ref, binding)
    application = _lae_runtime_state(state)["applicationBindings"].get(
        application_scope
    )
    is_current = isinstance(application, dict) and str(
        application.get("currentRuntimeDeploymentRef") or ""
    ) == str(runtime_deployment_ref)
    if is_current and str(record.get("status") or "") == "preparing":
        # A Control restart intentionally forgets in-memory deployment threads.
        # The immutable manifest, image map, volume records, idempotency key and
        # runtime identity remain in durable state, so resume the same submit
        # instead of leaving the application permanently stuck in preparing.
        if all(
            isinstance(record.get(key), dict)
            for key in ("manifest", "images", "volumeRecords")
        ):
            _start_lae_runtime_deployment(
                token=token,
                audience=audience,
                binding=binding,
                record=dict(record),
            )
            observed = dict(record)
        else:
            observed = {
                **record,
                "status": "failed",
                "retryable": False,
            }
    else:
        observed = (
            _observe_lae_runtime_deployment(state, dict(record))
            if is_current
            else dict(record)
        )
    if observed != record:
        def update_observation(current: Dict[str, Any]) -> Dict[str, Any]:
            current_principal = _require_lae_runtime_principal(
                current,
                token,
                audience=audience,
                scope=SCOPE_DEPLOYMENTS_READ,
                binding=binding,
            )
            stored = _lae_runtime_state(current)["deployments"].get(
                str(runtime_deployment_ref)
            )
            if not isinstance(stored, dict) or not _lae_runtime_binding_matches(
                stored,
                binding,
                principal_ref=str(current_principal["id"]),
            ):
                raise _lae_runtime_not_found()
            for key in (
                "status",
                "retryable",
                "serviceStatuses",
                "routeStatuses",
                "nomadVersion",
            ):
                if key in observed:
                    stored[key] = observed[key]
            stored["updatedAt"] = int(time.time())
            return dict(stored)

        observed = _mutate_control_state(update_observation)
    return _lae_runtime_deployment_envelope(observed)


LAE_RUNTIME_LOG_TAIL_DEFAULT = 120
LAE_RUNTIME_LOG_TAIL_MAX = 500
LAE_RUNTIME_LOG_LINE_BYTES_MAX = 2048
LAE_RUNTIME_LOG_RESPONSE_BYTES_MAX = 1024 * 1024
LAE_RUNTIME_METRICS_WINDOW_DEFAULT = 3600
LAE_RUNTIME_METRICS_WINDOW_MAX = 7 * 24 * 3600
LAE_RUNTIME_METRICS_POINTS_MAX = 10_000


def _parse_lae_runtime_observability_query(
    raw_query: str, *, kind: str
) -> int:
    if kind == "logs":
        key = "tail"
        default = LAE_RUNTIME_LOG_TAIL_DEFAULT
        maximum = LAE_RUNTIME_LOG_TAIL_MAX
    elif kind == "metrics":
        key = "window"
        default = LAE_RUNTIME_METRICS_WINDOW_DEFAULT
        maximum = LAE_RUNTIME_METRICS_WINDOW_MAX
    else:
        raise _lae_runtime_invalid("runtime observability request is invalid")
    try:
        pairs = urllib.parse.parse_qsl(
            str(raw_query or ""),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except ValueError as exc:
        raise _lae_runtime_invalid(
            "runtime observability query is invalid"
        ) from exc
    if not pairs:
        return default
    if len(pairs) != 1 or pairs[0][0] != key or not pairs[0][1]:
        raise _lae_runtime_invalid(
            "runtime observability query is invalid"
        )
    try:
        value = int(pairs[0][1])
    except ValueError as exc:
        raise _lae_runtime_invalid(
            f"runtime observability {key} is invalid"
        ) from exc
    if isinstance(value, bool) or not 1 <= value <= maximum:
        raise _lae_runtime_invalid(
            f"runtime observability {key} exceeds the platform limit"
        )
    return value


def _lae_runtime_observability_target(
    state: Dict[str, Any],
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    service_key: str,
    *,
    scope: str,
) -> tuple[Dict[str, Any], Dict[str, Any], str, str]:
    principal = _require_lae_runtime_principal(
        state,
        token,
        audience=audience,
        scope=scope,
        binding=binding,
    )
    principal_ref = str(principal["id"])
    record = _lae_runtime_state(state)["deployments"].get(
        str(runtime_deployment_ref or "")
    )
    if not isinstance(record, dict) or not _lae_runtime_binding_matches(
        record, binding, principal_ref=principal_ref
    ):
        raise _lae_runtime_not_found()
    manifest = record.get("manifest")
    if not isinstance(manifest, dict):
        raise _lae_runtime_unavailable("runtime deployment state is incomplete")
    service = next(
        (
            item
            for item in manifest.get("services") or []
            if isinstance(item, dict)
            and str(item.get("key") or "") == str(service_key or "")
        ),
        None,
    )
    if not isinstance(service, dict):
        # Do not reveal whether another task/job exists. The only selectable
        # unit is a serviceKey from this bound saved manifest.
        raise _lae_runtime_not_found()
    job_slug = str(record.get("jobSlug") or "")
    task_names = (
        record.get("taskNames")
        if isinstance(record.get("taskNames"), dict)
        else {}
    )
    task_name = str(task_names.get(str(service_key)) or "")
    if (
        job_slug != _lae_runtime_job_slug(binding)
        or task_name
        != _lae_runtime_task_name(str(service_key), binding.revision_ref)
    ):
        raise _lae_runtime_unavailable("runtime deployment state is incomplete")
    return dict(record), service, job_slug, task_name


def _lae_runtime_log_secret_values(
    config: LumaConfig, state: Dict[str, Any], record: Dict[str, Any]
) -> tuple[set[str], set[str]]:
    manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    secret_refs = [
        item
        for item in manifest.get("secretRefs") or []
        if isinstance(item, dict)
    ]
    names = {str(item.get("name") or "") for item in secret_refs if item.get("name")}
    if not secret_refs:
        return names, set()
    paths = [str(item) for item in record.get("variablePaths") or [] if str(item)]
    task_names = record.get("taskNames") if isinstance(record.get("taskNames"), dict) else {}
    if not paths or not task_names:
        raise _lae_runtime_unavailable(
            "runtime log redaction material is unavailable"
        )
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    values: set[str] = set()
    for service_key in {str(item.get("serviceKey") or "") for item in secret_refs}:
        task_name = str(task_names.get(service_key) or "")
        matching_paths = [path for path in paths if path.endswith("/" + task_name)]
        if not task_name or len(matching_paths) != 1:
            raise _lae_runtime_unavailable(
                "runtime log redaction material is unavailable"
            )
        try:
            items = client.get_variable(matching_paths[0])
        except LumaError as exc:
            raise _lae_runtime_unavailable(
                "runtime log redaction material is unavailable"
            ) from exc
        expected_names = {
            str(item.get("name") or "")
            for item in secret_refs
            if str(item.get("serviceKey") or "") == service_key
        }
        if not expected_names.issubset(items):
            raise _lae_runtime_unavailable(
                "runtime log redaction material is unavailable"
            )
        values.update(str(items[name]) for name in expected_names if items[name])
    return names, values


_LAE_RUNTIME_LOG_SENSITIVE_NAME = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)\b"
)
_LAE_RUNTIME_LOG_BEARER = re.compile(
    r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+"
)
_LAE_RUNTIME_LOG_URL_USERINFO = re.compile(
    r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@"
)


def _redact_lae_runtime_log_line(
    raw_line: str, *, secret_names: set[str], secret_values: set[str]
) -> str:
    line = str(raw_line)
    for value in sorted(secret_values, key=len, reverse=True):
        line = line.replace(value, "[REDACTED]")
    line = re.sub(r"lsec_[A-Za-z0-9][A-Za-z0-9._-]{7,122}", "[REDACTED]", line)
    line = _LAE_RUNTIME_LOG_BEARER.sub(r"\1[REDACTED]", line)
    line = _LAE_RUNTIME_LOG_URL_USERINFO.sub(r"\1[REDACTED]@", line)
    candidate_names = set(secret_names)
    candidate_names.update(_LAE_RUNTIME_LOG_SENSITIVE_NAME.findall(line))
    upper = line.upper()
    cut_at: int | None = None
    for name in candidate_names:
        if not name:
            continue
        start = upper.find(name.upper())
        while start >= 0:
            separator_positions = [
                position
                for position in (
                    line.find("=", start + len(name), start + len(name) + 8),
                    line.find(":", start + len(name), start + len(name) + 8),
                )
                if position >= 0
            ]
            if separator_positions:
                cut_at = min(separator_positions) + 1
                break
            start = upper.find(name.upper(), start + len(name))
        if cut_at is not None:
            break
    if cut_at is not None:
        line = line[:cut_at] + " [REDACTED]"
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) > LAE_RUNTIME_LOG_LINE_BYTES_MAX:
        line = (
            encoded[:LAE_RUNTIME_LOG_LINE_BYTES_MAX]
            .decode("utf-8", errors="ignore")
            + "…"
        )
    return line


def handle_lae_runtime_logs(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    service_key: str,
    *,
    tail: int,
) -> Dict[str, Any]:
    if isinstance(tail, bool) or not 1 <= int(tail) <= LAE_RUNTIME_LOG_TAIL_MAX:
        raise _lae_runtime_invalid("runtime observability tail is invalid")
    state = load_state()
    record, _service, job_slug, task_name = _lae_runtime_observability_target(
        state,
        token,
        audience,
        binding,
        runtime_deployment_ref,
        service_key,
        scope=SCOPE_LOGS_READ,
    )
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    _lae_runtime_verified_job(config, state, record, required=True)
    secret_names, secret_values = _lae_runtime_log_secret_values(
        config, state, record
    )
    try:
        raw_lines = _nomad_log_lines(
            config,
            state,
            f"{job_slug}/{task_name}",
            tail=int(tail),
            bound_job_id=job_slug,
            bound_task_name=task_name,
        )
    except LumaError as exc:
        raise _lae_runtime_unavailable(
            "runtime service logs are temporarily unavailable"
        ) from exc
    selected: list[str] = []
    total = 0
    truncated = False
    for raw_line in reversed(raw_lines[-int(tail) :]):
        line = _redact_lae_runtime_log_line(
            str(raw_line),
            secret_names=secret_names,
            secret_values=secret_values,
        )
        size = len(line.encode("utf-8"))
        if total + size > LAE_RUNTIME_LOG_RESPONSE_BYTES_MAX:
            truncated = True
            break
        selected.append(line)
        total += size
    selected.reverse()
    return {
        "schemaVersion": LAE_RUNTIME_SCHEMA_VERSION,
        "lumaName": str(record["manifest"]["name"]),
        "serviceKey": str(service_key),
        "tail": int(tail),
        "logs": selected,
        "truncated": truncated or len(raw_lines) > len(selected),
        "updatedAt": int(time.time()),
    }


def handle_lae_runtime_metrics(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    service_key: str,
    *,
    window: int,
) -> Dict[str, Any]:
    if (
        isinstance(window, bool)
        or not 1 <= int(window) <= LAE_RUNTIME_METRICS_WINDOW_MAX
    ):
        raise _lae_runtime_invalid("runtime observability window is invalid")
    state = load_state()
    record, _service, job_slug, task_name = _lae_runtime_observability_target(
        state,
        token,
        audience,
        binding,
        runtime_deployment_ref,
        service_key,
        scope=SCOPE_METRICS_READ,
    )
    internal_name = _dashboard_task_full_name(
        job_slug, task_name, compose=True
    )
    raw_series = load_history("service", internal_name, window=int(window))
    series: Dict[str, list[list[float | int]]] = {
        "cpuPercent": [],
        "memoryUsageBytes": [],
    }
    for key in series:
        raw_points = raw_series.get(key) if isinstance(raw_series, dict) else []
        if not isinstance(raw_points, list):
            continue
        for point in raw_points[-LAE_RUNTIME_METRICS_POINTS_MAX:]:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue
            timestamp, value = point
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(timestamp))
                or not math.isfinite(float(value))
            ):
                continue
            series[key].append([int(timestamp), value])
    return {
        "schemaVersion": LAE_RUNTIME_SCHEMA_VERSION,
        "lumaName": str(record["manifest"]["name"]),
        "serviceKey": str(service_key),
        "windowSeconds": int(window),
        "series": series,
        "updatedAt": int(time.time()),
    }


def _execute_lae_runtime_cancel(record: Dict[str, Any]) -> None:
    state = load_state()
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    job_slug = str(record.get("jobSlug") or "")
    previous_version = record.get("previousNomadVersion")
    try:
        if isinstance(previous_version, int) and not isinstance(previous_version, bool):
            revert_job(
                config, state, slug=job_slug, version=int(previous_version)
            )
        else:
            remove_from_nomad(config, state, slug=job_slug)
    except LumaError as exc:
        if "Nomad API error 404" not in str(exc):
            raise _lae_runtime_unavailable(
                "runtime cancellation is temporarily unavailable"
            ) from exc
    _delete_lae_runtime_variables(
        config,
        state,
        [str(item) for item in record.get("variablePaths") or []],
    )


@_serialize_deploy
def handle_lae_runtime_deployment_cancel(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    if body != {}:
        raise _lae_runtime_invalid("runtime cancel request body must be empty")

    def begin(current: Dict[str, Any]) -> tuple[Dict[str, Any], bool, bool]:
        principal = _require_lae_runtime_principal(
            current,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        runtime = _lae_runtime_state(current)
        record = runtime["deployments"].get(str(runtime_deployment_ref or ""))
        if not isinstance(record, dict) or not _lae_runtime_binding_matches(
            record, binding, principal_ref=principal_ref
        ):
            raise _lae_runtime_not_found()
        status = str(record.get("status") or "")
        if status == "canceled":
            return dict(record), True, False
        application = runtime["applicationBindings"].get(
            _lae_runtime_application_scope(principal_ref, binding)
        )
        active = isinstance(application, dict) and str(
            application.get("currentRuntimeDeploymentRef") or ""
        ) == str(runtime_deployment_ref)
        record["status"] = "canceling"
        record["updatedAt"] = int(time.time())
        return dict(record), status == "canceling", active

    record, replayed, execute = _mutate_control_state(begin)
    if execute:
        _execute_lae_runtime_cancel(record)

    def finish(current: Dict[str, Any]) -> Dict[str, Any]:
        principal = _require_lae_runtime_principal(
            current,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        runtime = _lae_runtime_state(current)
        stored = runtime["deployments"].get(str(runtime_deployment_ref))
        if not isinstance(stored, dict) or not _lae_runtime_binding_matches(
            stored, binding, principal_ref=principal_ref
        ):
            raise _lae_runtime_not_found()
        stored["status"] = "canceled"
        stored["retryable"] = False
        stored["updatedAt"] = int(time.time())
        application = runtime["applicationBindings"].get(
            _lae_runtime_application_scope(principal_ref, binding)
        )
        if isinstance(application, dict) and str(
            application.get("currentRuntimeDeploymentRef") or ""
        ) == str(runtime_deployment_ref):
            previous_ref = str(stored.get("previousRuntimeDeploymentRef") or "")
            application["currentRuntimeDeploymentRef"] = previous_ref
            application["updatedAt"] = int(time.time())
            for hostname in list(runtime["hostnameBindings"]):
                owner = runtime["hostnameBindings"].get(hostname)
                if not isinstance(owner, dict) or str(
                    owner.get("runtimeDeploymentRef") or ""
                ) != str(runtime_deployment_ref):
                    continue
                if previous_ref:
                    owner["runtimeDeploymentRef"] = previous_ref
                    owner["updatedAt"] = int(time.time())
                else:
                    runtime["hostnameBindings"].pop(hostname, None)
        return dict(stored)

    canceled = _mutate_control_state(finish)
    return _lae_runtime_deployment_envelope(canceled, replayed=replayed)


_LAE_RUNTIME_LIFECYCLE_ACTIONS = {"suspend", "resume", "restart", "rollback", "delete"}
_LAE_RUNTIME_LIFECYCLE_TRANSITIONS = {
    "suspend": "suspending",
    "resume": "resuming",
    "restart": "restarting",
    "delete": "deleting",
}


def _lae_runtime_binding_from_record(record: Dict[str, Any]) -> RuntimeBinding:
    try:
        return RuntimeBinding(
            tenant_ref=str(record["tenantRef"]),
            application_ref=str(record["applicationRef"]),
            operation_ref=str(record["operationRef"]),
            revision_ref=str(record["revisionRef"]),
            deployment_ref=str(record["deploymentRef"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _lae_runtime_unavailable(
            "runtime deployment state is incomplete"
        ) from exc


def _lae_runtime_expected_job_meta(record: Dict[str, Any]) -> Dict[str, str]:
    return {
        "luma.lae": "true",
        "luma.lae.tenant": str(record["tenantRef"]),
        "luma.lae.application": str(record["applicationRef"]),
        "luma.lae.operation": str(record["operationRef"]),
        "luma.lae.revision": str(record["revisionRef"]),
        "luma.lae.deployment": str(record["deploymentRef"]),
        "luma.lae.runtimeDeployment": str(record["runtimeDeploymentRef"]),
        "luma.lae.manifestDigest": str(record["manifestDigest"]),
    }


def _lae_runtime_verified_job(
    config: LumaConfig,
    state: Dict[str, Any],
    record: Dict[str, Any],
    *,
    required: bool,
) -> Dict[str, Any] | None:
    job_slug = str(record.get("jobSlug") or "")
    if not job_slug or job_slug != _lae_runtime_job_slug(
        _lae_runtime_binding_from_record(record)
    ):
        raise _lae_runtime_unavailable("runtime deployment state is incomplete")
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    try:
        detail = client.request(
            "GET", f"/v1/job/{urllib.parse.quote(job_slug, safe='')}"
        )
    except LumaError as exc:
        if "Nomad API error 404" in str(exc) and not required:
            return None
        raise _lae_runtime_unavailable(
            "Nomad runtime lifecycle is unavailable"
        ) from exc
    if not isinstance(detail, dict):
        raise _lae_runtime_unavailable("Nomad runtime lifecycle is unavailable")
    meta = detail.get("Meta") if isinstance(detail.get("Meta"), dict) else {}
    if any(
        str(meta.get(key) or "") != expected
        for key, expected in _lae_runtime_expected_job_meta(record).items()
    ):
        # Never mutate a job whose tenant/revision ownership cannot be proven,
        # even when the stable application slug happens to match.
        raise _lae_runtime_conflict("runtime job binding does not match")
    return detail


def _lae_runtime_bound_compose_spec(
    state: Dict[str, Any],
    record: Dict[str, Any],
    *,
    volume_binding: RuntimeBinding | None = None,
) -> ComposeDeploymentSpec:
    manifest = record.get("manifest")
    images = record.get("images")
    if not isinstance(manifest, dict) or not isinstance(images, dict):
        raise _lae_runtime_unavailable("runtime deployment state is incomplete")
    binding = _lae_runtime_binding_from_record(record)
    volume_records = _lae_runtime_validate_volumes(
        state,
        str(record.get("principalRef") or ""),
        volume_binding or binding,
        manifest,
    )
    placement_state = record.get("placement")
    placement: PlacementDecision | None = None
    if isinstance(placement_state, dict):
        candidate_ids = tuple(
            str(value)
            for value in placement_state.get("candidateNodeIds") or []
            if str(value)
        )
        candidate_names = tuple(
            str(value)
            for value in placement_state.get("candidateNodeNames") or []
            if str(value)
        )
        if candidate_ids and candidate_names:
            placement = PlacementDecision(
                region=str(manifest.get("region") or ""),
                requested_cpu_mhz=0,
                requested_memory_mib=0,
                stateful=bool(manifest.get("volumes")),
                candidate_node_ids=candidate_ids,
                candidate_node_names=candidate_names,
            )
    return _lae_runtime_compose_spec(
        state,
        binding,
        manifest,
        {str(key): str(value) for key, value in images.items()},
        volume_records,
        job_slug=str(record["jobSlug"]),
        placement=placement,
    )


def _lae_runtime_delete_routes(
    config: LumaConfig,
    spec: ComposeDeploymentSpec,
    *,
    state: Dict[str, Any],
) -> None:
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    for service in compose_public_services(spec):
        delete_dns(
            config,
            _compose_service_as_service_spec(spec, service),
            secrets=secrets,
        )


def _lae_runtime_restore_routes(
    config: LumaConfig,
    spec: ComposeDeploymentSpec,
    *,
    state: Dict[str, Any],
) -> None:
    secrets = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
    for service in compose_public_services(spec):
        sync_dns(
            config,
            _compose_service_as_service_spec(spec, service),
            secrets=secrets,
        )


def _execute_lae_runtime_lifecycle(
    action: str,
    record: Dict[str, Any],
    *,
    token: str,
    audience: str,
    binding: RuntimeBinding,
    principal_ref: str,
    volume_policy: str = "retain",
) -> Dict[str, Any]:
    """Execute a lifecycle action using only the saved, bound runtime record."""

    state = load_state()
    current_principal = _require_lae_runtime_principal(
        state,
        token,
        audience=audience,
        scope=SCOPE_DEPLOYMENTS_WRITE,
        binding=binding,
    )
    if str(current_principal.get("id") or "") != principal_ref:
        raise _lae_runtime_forbidden()
    stored = _lae_runtime_state(state)["deployments"].get(
        str(record.get("runtimeDeploymentRef") or "")
    )
    if (
        not isinstance(stored, dict)
        or not _lae_runtime_binding_matches(
            stored, binding, principal_ref=principal_ref
        )
        or str(stored.get("manifestDigest") or "")
        != str(record.get("manifestDigest") or "")
        or str(stored.get("status") or "")
        != _LAE_RUNTIME_LIFECYCLE_TRANSITIONS[action]
    ):
        raise _lae_runtime_conflict("runtime lifecycle binding changed")
    record = dict(stored)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    job_slug = str(record.get("jobSlug") or "")
    spec = _lae_runtime_bound_compose_spec(state, record)
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )

    try:
        if action == "suspend":
            detail = _lae_runtime_verified_job(
                config, state, record, required=True
            )
            assert detail is not None
            # On a retry after Luma stopped Nomad but before it checkpointed
            # completion, GET returns the new ``Stop=true`` version. Keep the
            # previously checkpointed active version so resume can never
            # restore the stopped job by accident.
            version = (
                record.get("nomadVersion")
                if bool(detail.get("Stop"))
                else detail.get("Version")
            )
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                raise _lae_runtime_unavailable(
                    "Nomad runtime lifecycle is unavailable"
                )
            client.request(
                "DELETE",
                f"/v1/job/{urllib.parse.quote(job_slug, safe='')}?purge=false",
            )
            _lae_runtime_delete_routes(config, spec, state=state)
            return {"suspendedNomadVersion": version}

        if action == "resume":
            # A non-purged stop preserves the encrypted Nomad Variables. Revert
            # to the exact version captured at suspend; no caller-supplied job
            # or manifest is accepted at this boundary.
            _lae_runtime_verified_job(config, state, record, required=True)
            version = record.get("suspendedNomadVersion")
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                raise _lae_runtime_conflict(
                    "runtime deployment has no resumable saved version"
                )
            revert_job(config, state, slug=job_slug, version=version)
            _lae_runtime_restore_routes(config, spec, state=state)
            current_version = _lae_runtime_nomad_job_version(
                config, state, job_slug
            )
            return {"nomadVersion": current_version}

        if action == "restart":
            _lae_runtime_verified_job(config, state, record, required=True)
            allocations = client.request(
                "GET",
                f"/v1/job/{urllib.parse.quote(job_slug, safe='')}/allocations",
            )
            if not isinstance(allocations, list):
                raise _lae_runtime_unavailable(
                    "Nomad runtime lifecycle is unavailable"
                )
            stopped_allocation_ids = {
                str(value).strip()
                for value in record.get("restartAllocationIds") or []
                if str(value).strip()
            }
            if not stopped_allocation_ids:
                stopped_allocation_ids = {
                    str(allocation.get("ID") or "").strip()
                    for allocation in allocations
                    if isinstance(allocation, dict)
                    and str(allocation.get("ClientStatus") or "").lower()
                    == "running"
                    and str(allocation.get("ID") or "").strip()
                }
                if not stopped_allocation_ids:
                    raise _lae_runtime_unavailable(
                        "runtime deployment has no restartable allocation"
                    )

                def checkpoint_restart(current: Dict[str, Any]) -> None:
                    stored_restart = _lae_runtime_state(current)["deployments"].get(
                        str(record.get("runtimeDeploymentRef") or "")
                    )
                    if (
                        not isinstance(stored_restart, dict)
                        or not _lae_runtime_binding_matches(
                            stored_restart, binding, principal_ref=principal_ref
                        )
                        or str(stored_restart.get("status") or "") != "restarting"
                    ):
                        raise _lae_runtime_conflict(
                            "runtime restart checkpoint changed"
                        )
                    stored_restart["restartAllocationIds"] = sorted(
                        stopped_allocation_ids
                    )
                    stored_restart["updatedAt"] = int(time.time())

                # Persist the exact old allocation set before mutating Nomad.
                # A retry after a timeout will continue waiting for replacements
                # instead of stopping the newly-created allocation again.
                _mutate_control_state(checkpoint_restart)
                record["restartAllocationIds"] = sorted(stopped_allocation_ids)
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    continue
                if str(allocation.get("ClientStatus") or "").lower() != "running":
                    continue
                allocation_id = str(allocation.get("ID") or "").strip()
                if allocation_id not in stopped_allocation_ids:
                    continue
                client.request(
                    "POST",
                    f"/v1/allocation/{urllib.parse.quote(allocation_id, safe='')}/stop",
                    None,
                )
            stopped = len(stopped_allocation_ids)
            # A lifecycle restart is a replacement operation, not a best-effort
            # signal.  Do not report success while Nomad can still be running
            # only the allocations we just stopped: that leaves LAE showing a
            # successful restart even when no new runtime was created.
            replacement_ids = _wait_for_nomad_job_replacement(
                client,
                job_slug,
                stopped_allocation_ids,
                min_running=stopped,
            )
            # DNS is derived delivery state outside the allocation. Reconcile
            # it after the replacement exists so restart completion means the
            # application can converge through its managed public routes too.
            _lae_runtime_restore_routes(config, spec, state=state)
            return {
                "restartedAllocations": stopped,
                "replacementAllocationIds": replacement_ids,
            }

        if action == "delete":
            _lae_runtime_verified_job(config, state, record, required=False)
            _lae_runtime_delete_routes(config, spec, state=state)
            try:
                remove_from_nomad(config, state, slug=job_slug)
            except LumaError as exc:
                if "Nomad API error 404" not in str(exc):
                    raise
            _delete_lae_runtime_variables(
                config,
                state,
                [str(item) for item in record.get("variablePaths") or []],
            )
            if volume_policy == "delete":
                _cleanup_compose_managed_storage(spec, state, dry_run=False)
            stack_target = _resolve_control_path(
                compose_stack_path(config, spec), config_path
            )
            _remove_generated_files(stack_target.parent)
            return {
                "deletedVolumeRefs": (
                    [
                        str(item.get("volumeRef") or "")
                        for item in record.get("volumeBindings") or []
                        if isinstance(item, dict)
                    ]
                    if volume_policy == "delete"
                    else []
                )
            }
    except LumaRuntimeError:
        raise
    except (LumaError, OSError, TimeoutError) as exc:
        raise _lae_runtime_unavailable(
            f"runtime {action} is temporarily unavailable"
        ) from exc
    raise _lae_runtime_invalid("runtime lifecycle action is invalid")


def _execute_lae_runtime_rollback(
    current: Dict[str, Any], target: Dict[str, Any]
) -> Dict[str, Any]:
    """Revert the stable application job to one previously verified revision."""

    state = load_state()
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    current_slug = str(current.get("jobSlug") or "")
    target_slug = str(target.get("jobSlug") or "")
    target_version = target.get("nomadVersion")
    if (
        not current_slug
        or current_slug != target_slug
        or isinstance(target_version, bool)
        or not isinstance(target_version, int)
        or target_version < 0
    ):
        raise _lae_runtime_conflict("runtime rollback target is incomplete")
    client = NomadApi(
        nomad_addr(config, state), token=str(state.get("nomadToken") or "")
    )
    try:
        detail = client.request(
            "GET", f"/v1/job/{urllib.parse.quote(current_slug, safe='')}"
        )
        if not isinstance(detail, dict):
            raise _lae_runtime_unavailable("Nomad runtime rollback is unavailable")
        meta = detail.get("Meta") if isinstance(detail.get("Meta"), dict) else {}
        current_meta = _lae_runtime_expected_job_meta(current)
        target_meta = _lae_runtime_expected_job_meta(target)
        matches_current = all(
            str(meta.get(key) or "") == expected
            for key, expected in current_meta.items()
        )
        matches_target = all(
            str(meta.get(key) or "") == expected
            for key, expected in target_meta.items()
        )
        if not matches_current and not matches_target:
            raise _lae_runtime_conflict("runtime rollback job binding changed")
        if matches_current:
            current_spec = _lae_runtime_bound_compose_spec(state, current)
            _lae_runtime_delete_routes(config, current_spec, state=state)
            revert_job(config, state, slug=current_slug, version=target_version)
        # Route publication is an independent side effect. Always repair it
        # on retry, including the crash window after Nomad reverted but before
        # the target routes were restored.
        # Named volumes are application-scoped but their durable ownership is
        # rebound to each active revision.  Before the rollback transaction is
        # committed they still carry the current revision binding, so validate
        # that binding while rendering the immutable target routes.  The state
        # mutation below atomically rebinds the same volumes to the target.
        target_spec = _lae_runtime_bound_compose_spec(
            state,
            target,
            volume_binding=_lae_runtime_binding_from_record(current),
        )
        _lae_runtime_restore_routes(config, target_spec, state=state)
        return {
            "nomadVersion": _lae_runtime_nomad_job_version(
                config, state, current_slug
            )
        }
    except LumaRuntimeError:
        raise
    except (LumaError, OSError, TimeoutError) as exc:
        raise _lae_runtime_unavailable(
            "runtime rollback is temporarily unavailable"
        ) from exc


def _handle_lae_runtime_deployment_rollback(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    request: Dict[str, Any],
    *,
    idempotency_key: str,
) -> Dict[str, Any]:
    normalized_idempotency = _normalize_lae_runtime_idempotency_key(
        idempotency_key
    )
    target_request = request.get("target")
    if not isinstance(target_request, dict):
        raise _lae_runtime_invalid("runtime rollback target is invalid")
    target_ref = str(target_request.get("runtimeDeploymentRef") or "")
    request_hash = _lae_runtime_hash(
        {"action": "rollback", "request": request}
    )
    now = int(time.time())

    def begin(
        current_state: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any], bool, bool]:
        principal = _require_lae_runtime_principal(
            current_state,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        _lae_runtime_prune(current_state, now=now)
        runtime = _lae_runtime_state(current_state)
        current = runtime["deployments"].get(str(runtime_deployment_ref or ""))
        target = runtime["deployments"].get(target_ref)
        if (
            not isinstance(current, dict)
            or not _lae_runtime_binding_matches(
                current, binding, principal_ref=principal_ref
            )
            or not isinstance(target, dict)
            or str(target.get("principalRef") or "") != principal_ref
            or str(target.get("tenantRef") or "") != binding.tenant_ref
            or str(target.get("applicationRef") or "")
            != binding.application_ref
            or str(target.get("operationRef") or "")
            != str(target_request.get("operationRef") or "")
            or str(target.get("revisionRef") or "")
            != str(target_request.get("revisionRef") or "")
            or str(target.get("deploymentRef") or "")
            != str(target_request.get("deploymentRef") or "")
            or target_ref == str(runtime_deployment_ref)
            or str(target.get("jobSlug") or "")
            != str(current.get("jobSlug") or "")
        ):
            raise _lae_runtime_not_found()
        application = runtime["applicationBindings"].get(
            _lae_runtime_application_scope(principal_ref, binding)
        )
        if not isinstance(application, dict):
            raise _lae_runtime_conflict("runtime application binding is unavailable")
        scope = _lae_runtime_idempotency_scope(
            principal_ref,
            binding,
            "POST /v1/lae/runtime/deployments/{runtime-ref}/rollback",
            normalized_idempotency,
        )
        existing = runtime["lifecycleIdempotency"].get(scope)
        if isinstance(existing, dict):
            if (
                not secrets.compare_digest(
                    str(existing.get("requestHash") or ""), request_hash
                )
                or str(existing.get("runtimeDeploymentRef") or "")
                != str(runtime_deployment_ref)
                or str(existing.get("targetRuntimeDeploymentRef") or "")
                != target_ref
            ):
                raise _lae_runtime_conflict()
            return (
                dict(current),
                dict(target),
                True,
                not bool(existing.get("completed")),
            )
        recovering = any(
            isinstance(checkpoint, dict)
            and str(checkpoint.get("action") or "") == "rollback"
            and not bool(checkpoint.get("completed"))
            and str(checkpoint.get("runtimeDeploymentRef") or "")
            == str(runtime_deployment_ref)
            and str(checkpoint.get("targetRuntimeDeploymentRef") or "")
            == target_ref
            for checkpoint in runtime["lifecycleIdempotency"].values()
        )
        if str(application.get("currentRuntimeDeploymentRef") or "") != str(
            runtime_deployment_ref
        ):
            raise _lae_runtime_conflict(
                "runtime rollback source is not the active revision"
            )
        current_status = str(current.get("status") or "")
        if current_status not in {"running", "degraded"} and not (
            current_status == "rolling_back" and recovering
        ):
            raise _lae_runtime_conflict("runtime rollback source is not healthy")
        if str(target.get("status") or "") not in {
            "running",
            "degraded",
            "suspended",
            "superseded",
        }:
            raise _lae_runtime_conflict("runtime rollback target is unavailable")
        target_version = target.get("nomadVersion")
        if (
            isinstance(target_version, bool)
            or not isinstance(target_version, int)
            or target_version < 0
        ):
            raise _lae_runtime_conflict("runtime rollback target is incomplete")
        runtime["lifecycleIdempotency"][scope] = {
            "requestHash": request_hash,
            "runtimeDeploymentRef": str(runtime_deployment_ref),
            "targetRuntimeDeploymentRef": target_ref,
            "action": "rollback",
            "completed": False,
            "createdAt": now,
            "expiresAt": now + max(int(LAE_RUNTIME_IDEMPOTENCY_SECONDS), 60),
        }
        current["status"] = "rolling_back"
        current["retryable"] = True
        current["updatedAt"] = now
        return dict(current), dict(target), False, True

    current, target, replayed, execute = _mutate_control_state(begin)
    if execute:
        result = _execute_lae_runtime_rollback(current, target)
    else:
        result = {}

    def finish(current_state: Dict[str, Any]) -> Dict[str, Any]:
        principal = _require_lae_runtime_principal(
            current_state,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        runtime = _lae_runtime_state(current_state)
        stored_current = runtime["deployments"].get(str(runtime_deployment_ref))
        stored_target = runtime["deployments"].get(target_ref)
        if not isinstance(stored_current, dict) or not isinstance(
            stored_target, dict
        ):
            raise _lae_runtime_not_found()
        application = runtime["applicationBindings"].get(
            _lae_runtime_application_scope(principal_ref, binding)
        )
        if not isinstance(application, dict):
            raise _lae_runtime_conflict("runtime application binding is unavailable")
        scope = _lae_runtime_idempotency_scope(
            principal_ref,
            binding,
            "POST /v1/lae/runtime/deployments/{runtime-ref}/rollback",
            normalized_idempotency,
        )
        idem = runtime["lifecycleIdempotency"].get(scope)
        if not isinstance(idem, dict):
            raise _lae_runtime_conflict("runtime rollback checkpoint is unavailable")
        if bool(idem.get("completed")):
            return dict(stored_target)
        if str(stored_current.get("status") or "") != "rolling_back":
            raise _lae_runtime_conflict("runtime rollback state changed")
        stored_current["status"] = "superseded"
        stored_current["retryable"] = False
        stored_current["updatedAt"] = int(time.time())
        stored_target.update(result)
        stored_target["status"] = "deploying"
        stored_target["retryable"] = False
        stored_target["serviceStatuses"] = {
            str(key): "pending"
            for key in dict(stored_target.get("serviceStatuses") or {})
        }
        stored_target["routeStatuses"] = {
            str(key): "pending"
            for key in dict(stored_target.get("routeStatuses") or {})
        }
        stored_target["submittedAt"] = int(time.time())
        stored_target["updatedAt"] = int(time.time())
        target_binding = _lae_runtime_binding_from_record(stored_target)
        for item in stored_target.get("volumeBindings") or []:
            if not isinstance(item, dict):
                raise _lae_runtime_conflict(
                    "runtime rollback volume binding is incomplete"
                )
            volume_ref = str(item.get("volumeRef") or "")
            volume = runtime["volumes"].get(volume_ref)
            if (
                not isinstance(volume, dict)
                or not _lae_runtime_volume_owner_matches(
                    volume, target_binding, principal_ref=principal_ref
                )
                or str(volume.get("key") or "") != str(item.get("key") or "")
            ):
                raise _lae_runtime_conflict(
                    "runtime rollback volume binding is unavailable"
                )
            volume.update(target_binding.state_body())
            volume["updatedAt"] = int(time.time())
        application["currentRuntimeDeploymentRef"] = target_ref
        application["updatedAt"] = int(time.time())
        for hostname, owner in list(runtime["hostnameBindings"].items()):
            if (
                isinstance(owner, dict)
                and str(owner.get("principalRef") or "") == principal_ref
                and str(owner.get("tenantRef") or "") == binding.tenant_ref
                and str(owner.get("applicationRef") or "")
                == binding.application_ref
            ):
                runtime["hostnameBindings"].pop(hostname, None)
        manifest = stored_target.get("manifest")
        routes = manifest.get("routes") if isinstance(manifest, dict) else []
        for route in routes if isinstance(routes, list) else []:
            if not isinstance(route, dict):
                continue
            hostname = str(route.get("hostname") or "")
            if hostname:
                runtime["hostnameBindings"][hostname] = {
                    "principalRef": principal_ref,
                    "tenantRef": binding.tenant_ref,
                    "applicationRef": binding.application_ref,
                    "runtimeDeploymentRef": target_ref,
                    "updatedAt": int(time.time()),
                }
        idem["completed"] = True
        idem["updatedAt"] = int(time.time())
        return dict(stored_target)

    completed = _mutate_control_state(finish)
    return _lae_runtime_deployment_envelope(completed, replayed=replayed)


@_serialize_deploy
def handle_lae_runtime_deployment_lifecycle(
    token: str,
    audience: str,
    binding: RuntimeBinding,
    runtime_deployment_ref: str,
    action: str,
    body: Dict[str, Any],
    *,
    idempotency_key: str,
) -> Dict[str, Any]:
    if action not in _LAE_RUNTIME_LIFECYCLE_ACTIONS:
        raise _lae_runtime_invalid("runtime lifecycle action is invalid")
    request = _validate_lae_runtime_lifecycle_body(action, body)
    if action == "rollback":
        return _handle_lae_runtime_deployment_rollback(
            token,
            audience,
            binding,
            runtime_deployment_ref,
            request,
            idempotency_key=idempotency_key,
        )
    normalized_idempotency = _normalize_lae_runtime_idempotency_key(
        idempotency_key
    )
    request_hash = _lae_runtime_hash(
        {"action": action, "request": request}
    )
    transition = _LAE_RUNTIME_LIFECYCLE_TRANSITIONS[action]
    now = int(time.time())

    def begin(current: Dict[str, Any]) -> tuple[Dict[str, Any], bool, bool]:
        principal = _require_lae_runtime_principal(
            current,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        _lae_runtime_prune(current, now=now)
        runtime = _lae_runtime_state(current)
        record = runtime["deployments"].get(str(runtime_deployment_ref or ""))
        if not isinstance(record, dict) or not _lae_runtime_binding_matches(
            record, binding, principal_ref=principal_ref
        ):
            raise _lae_runtime_not_found()
        scope = _lae_runtime_idempotency_scope(
            principal_ref,
            binding,
            f"POST /v1/lae/runtime/deployments/{{runtime-ref}}/{action}",
            normalized_idempotency,
        )
        existing = runtime["lifecycleIdempotency"].get(scope)
        if isinstance(existing, dict):
            if (
                not secrets.compare_digest(
                    str(existing.get("requestHash") or ""), request_hash
                )
                or str(existing.get("runtimeDeploymentRef") or "")
                != str(runtime_deployment_ref)
            ):
                raise _lae_runtime_conflict()
            return dict(record), True, not bool(existing.get("completed"))

        status = str(record.get("status") or "")
        application = runtime["applicationBindings"].get(
            _lae_runtime_application_scope(principal_ref, binding)
        )
        active = isinstance(application, dict) and str(
            application.get("currentRuntimeDeploymentRef") or ""
        ) == str(runtime_deployment_ref)
        if status != "deleted" and not active:
            raise _lae_runtime_conflict(
                "runtime lifecycle target is not the active application revision"
            )
        in_progress = set(_LAE_RUNTIME_LIFECYCLE_TRANSITIONS.values()) | {
            "preparing",
            "canceling",
        }
        if status in in_progress and status != transition:
            raise LumaRuntimeError(
                "another runtime mutation is in progress",
                status=429,
                code="capacity_unavailable",
            )
        no_op = (
            action == "suspend" and status == "suspended"
            or action == "resume" and status in {"running", "deploying"}
            or action == "delete" and status == "deleted"
        )
        allowed = {
            "suspend": {"running", "degraded", "deploying"},
            "resume": {"suspended"},
            "restart": {"running", "degraded"},
            "delete": {
                "running",
                "degraded",
                "deploying",
                "failed",
                "suspended",
            },
        }[action]
        if not no_op and status not in allowed and status != transition:
            raise _lae_runtime_conflict(
                f"runtime deployment cannot {action} from status {status or 'unknown'}"
            )
        runtime["lifecycleIdempotency"][scope] = {
            "requestHash": request_hash,
            "runtimeDeploymentRef": str(runtime_deployment_ref),
            "action": action,
            "completed": no_op,
            "createdAt": now,
            "expiresAt": now
            + max(int(LAE_RUNTIME_IDEMPOTENCY_SECONDS), 60),
        }
        if no_op:
            return dict(record), False, False
        record["status"] = transition
        record["retryable"] = True
        record["updatedAt"] = now
        return dict(record), False, True

    record, replayed, execute = _mutate_control_state(begin)
    if not execute:
        return _lae_runtime_deployment_envelope(record, replayed=replayed)

    result = _execute_lae_runtime_lifecycle(
        action,
        record,
        token=token,
        audience=audience,
        binding=binding,
        principal_ref=str(record["principalRef"]),
        volume_policy=str(request.get("volumePolicy") or "retain"),
    )

    def finish(current: Dict[str, Any]) -> Dict[str, Any]:
        principal = _require_lae_runtime_principal(
            current,
            token,
            audience=audience,
            scope=SCOPE_DEPLOYMENTS_WRITE,
            binding=binding,
        )
        principal_ref = str(principal["id"])
        runtime = _lae_runtime_state(current)
        stored = runtime["deployments"].get(str(runtime_deployment_ref))
        if not isinstance(stored, dict) or not _lae_runtime_binding_matches(
            stored, binding, principal_ref=principal_ref
        ):
            raise _lae_runtime_not_found()
        stored.update(result)
        stored["retryable"] = False
        if action == "suspend":
            stored["status"] = "suspended"
            stored["serviceStatuses"] = {
                str(key): "suspended"
                for key in dict(stored.get("serviceStatuses") or {})
            }
            stored["routeStatuses"] = {
                str(key): "suspended"
                for key in dict(stored.get("routeStatuses") or {})
            }
        elif action == "resume":
            stored["status"] = "deploying"
            stored["serviceStatuses"] = {
                str(key): "pending"
                for key in dict(stored.get("serviceStatuses") or {})
            }
            stored["routeStatuses"] = {
                str(key): "pending"
                for key in dict(stored.get("routeStatuses") or {})
            }
            stored["submittedAt"] = int(time.time())
        elif action == "restart":
            stored.pop("restartAllocationIds", None)
            stored["status"] = "deploying"
            stored["serviceStatuses"] = {
                str(key): "starting"
                for key in dict(stored.get("serviceStatuses") or {})
            }
            stored["routeStatuses"] = {
                str(key): "pending"
                for key in dict(stored.get("routeStatuses") or {})
            }
            stored["submittedAt"] = int(time.time())
        else:
            stored["status"] = "deleted"
            stored["serviceStatuses"] = {
                str(key): "deleted"
                for key in dict(stored.get("serviceStatuses") or {})
            }
            stored["routeStatuses"] = {
                str(key): "deleted"
                for key in dict(stored.get("routeStatuses") or {})
            }
            application = runtime["applicationBindings"].get(
                _lae_runtime_application_scope(principal_ref, binding)
            )
            if isinstance(application, dict) and str(
                application.get("currentRuntimeDeploymentRef") or ""
            ) == str(runtime_deployment_ref):
                application["currentRuntimeDeploymentRef"] = ""
                application["updatedAt"] = int(time.time())
            for hostname, owner in list(runtime["hostnameBindings"].items()):
                if isinstance(owner, dict) and str(
                    owner.get("runtimeDeploymentRef") or ""
                ) == str(runtime_deployment_ref):
                    runtime["hostnameBindings"].pop(hostname, None)
            deleted_refs = {
                str(item)
                for item in result.get("deletedVolumeRefs") or []
                if str(item)
            }
            for volume_ref in deleted_refs:
                runtime["volumes"].pop(volume_ref, None)
            if deleted_refs:
                stored["volumeBindings"] = []
                for key, item in list(runtime["volumeIdempotency"].items()):
                    if not isinstance(item, dict) or deleted_refs & {
                        str(value) for value in item.get("volumeRefs") or []
                    }:
                        runtime["volumeIdempotency"].pop(key, None)
            _deployments_state(current)["compose"].pop(
                str(stored.get("jobSlug") or ""), None
            )
            stored["variablePaths"] = []
        stored["updatedAt"] = int(time.time())
        scope = _lae_runtime_idempotency_scope(
            principal_ref,
            binding,
            f"POST /v1/lae/runtime/deployments/{{runtime-ref}}/{action}",
            normalized_idempotency,
        )
        idem = runtime["lifecycleIdempotency"].get(scope)
        if isinstance(idem, dict):
            idem["completed"] = True
            idem["updatedAt"] = int(time.time())
        return dict(stored)

    completed = _mutate_control_state(finish)
    return _lae_runtime_deployment_envelope(completed, replayed=replayed)


def handle_builder_task_create(
    token: str,
    body: Dict[str, Any],
    *,
    idempotency_key: str,
) -> Dict[str, Any]:
    state = load_state()
    principal = _require_lae_service_principal(state, token)
    normalized = validate_builder_task_request(body)
    _authorize_lae_builder_scope(principal, normalized)
    _require_builder_agent_image_allowlist(normalized)
    request_hash = builder_task_request_hash(normalized)
    idempotency_key = _normalize_builder_idempotency_key(idempotency_key)
    idempotency_scope = _builder_task_idempotency_scope(principal, normalized, idempotency_key)
    kind = str(normalized["kind"])
    action = builder_action_for_kind(kind)
    now = int(time.time())

    def mutate(current: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        current_principal = _require_lae_service_principal(current, token)
        _authorize_lae_builder_scope(current_principal, normalized)
        _require_builder_agent_image_allowlist(normalized)
        _prune_builder_tasks(current, now=now)
        tasks = _builder_tasks(current)
        idempotency = _builder_task_idempotency(current)
        existing = idempotency.get(idempotency_scope)
        if isinstance(existing, dict):
            if str(existing.get("requestHash") or "") != request_hash:
                raise LumaError("Idempotency-Key is already bound to a different builder task request")
            existing_task = tasks.get(str(existing.get("taskId") or ""))
            if isinstance(existing_task, dict):
                return _builder_task_public(existing_task), True
            idempotency.pop(idempotency_scope, None)

        if kind == "build-plan":
            _validate_bound_build_request(current, normalized, current_principal)
            # Fail before enqueueing if Control cannot derive an authenticated
            # platform-owned registry target.  The returned lease is discarded
            # here and recreated only in memory when the node actually leases.
            _builder_build_registry_lease(current, normalized, current_principal)

        builder_node = _select_builder_task_node(current, kind)
        builder_task_id = f"builder-{secrets.token_hex(12)}"
        agent_task_id = f"task-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        builder_task: Dict[str, Any] = {
            "id": builder_task_id,
            "schemaVersion": BUILDER_TASK_SCHEMA_VERSION,
            "kind": kind,
            "action": action,
            "externalOperationId": str(normalized["externalOperationId"]),
            "tenantRef": str(normalized["tenantRef"]),
            "applicationRef": str(normalized["applicationRef"]),
            "principalRef": str(current_principal["id"]),
            "status": "queued",
            "builderNode": builder_node,
            "agentTaskId": agent_task_id,
            "requestHash": request_hash,
            "request": normalized,
            "events": [],
            "nextEventCursor": 1,
            "createdAt": now,
            "updatedAt": now,
        }
        _builder_task_event(
            builder_task,
            {"type": "status", "status": "queued", "message": f"{kind} task queued"},
            now=now,
        )
        tasks[builder_task_id] = builder_task
        _prune_agent_tasks(current, now=now)
        child_payload: Dict[str, Any] = {
            "builderTaskId": builder_task_id,
            "schemaVersion": BUILDER_TASK_SCHEMA_VERSION,
            "externalOperationId": str(normalized["externalOperationId"]),
            "tenantRef": str(normalized["tenantRef"]),
            "applicationRef": str(normalized["applicationRef"]),
            **dict(normalized["payload"]),
        }
        _agent_tasks(current)[agent_task_id] = {
            "id": agent_task_id,
            "nodeName": builder_node,
            "action": action,
            "payload": child_payload,
            "builderTaskId": builder_task_id,
            "requiredCapabilitiesAny": list(_builder_task_capabilities(kind)),
            "progress": [],
            "status": "queued",
            "createdAt": now,
            "updatedAt": now,
        }
        idempotency[idempotency_scope] = {
            "taskId": builder_task_id,
            "requestHash": request_hash,
            "principalRef": str(current_principal["id"]),
            "tenantRef": str(normalized["tenantRef"]),
            "applicationRef": str(normalized["applicationRef"]),
            "createdAt": now,
            "expiresAt": now + max(int(BUILDER_TASK_IDEMPOTENCY_SECONDS), 60),
        }
        return _builder_task_public(builder_task), False

    task, replayed = _mutate_control_state(mutate)
    return {"task": task, "replayed": replayed}


def _builder_artifact_lease_binding(
    task: Dict[str, Any],
    principal: Dict[str, Any],
    body: Dict[str, Any],
    *,
    task_id: str,
) -> tuple[ArtifactLeaseBinding, int]:
    required = {
        "schemaVersion",
        "tenantRef",
        "applicationRef",
        "externalOperationId",
        "builderTaskId",
        "artifact",
        "ttlSeconds",
    }
    if set(body) != required or body.get("schemaVersion") != "luma.artifact-download-lease/v1":
        raise LumaError("artifact download lease request is invalid")
    if str(body.get("builderTaskId") or "") != task_id:
        raise LumaError(f"builder task not found: {task_id}")
    expected_scope = {
        "tenantRef": str(task.get("tenantRef") or ""),
        "applicationRef": str(task.get("applicationRef") or ""),
        "externalOperationId": str(task.get("externalOperationId") or ""),
    }
    if any(str(body.get(key) or "") != value for key, value in expected_scope.items()):
        raise LumaError(f"builder task not found: {task_id}")
    artifact = body.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "name",
        "digest",
        "mediaType",
        "sizeBytes",
    }:
        raise LumaError("artifact download lease request is invalid")
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    descriptors = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    artifact_name = str(artifact.get("name") or "")
    stored_descriptor = descriptors.get(artifact_name)
    expected_descriptor = {
        "digest": str(artifact.get("digest") or ""),
        "mediaType": str(artifact.get("mediaType") or ""),
        "sizeBytes": artifact.get("sizeBytes"),
    }
    if (
        str(task.get("kind") or "") != "analyze-source"
        or str(task.get("status") or "") != "succeeded"
        or not isinstance(stored_descriptor, dict)
        or stored_descriptor != expected_descriptor
    ):
        raise LumaError("builder artifact is unavailable")
    ttl = body.get("ttlSeconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 5 <= ttl <= 300:
        raise LumaError("artifact download lease request is invalid")
    return (
        ArtifactLeaseBinding(
            principal_ref=str(principal.get("id") or ""),
            tenant_ref=expected_scope["tenantRef"],
            application_ref=expected_scope["applicationRef"],
            external_operation_id=expected_scope["externalOperationId"],
            builder_task_id=task_id,
            artifact_name=artifact_name,
            digest=expected_descriptor["digest"],
            media_type=expected_descriptor["mediaType"],
            size_bytes=int(expected_descriptor["sizeBytes"]),
        ),
        ttl,
    )


def handle_builder_artifact_lease_create(
    token: str,
    task_id: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    state = load_state()
    principal = _require_lae_service_principal(state, token)
    task = _builder_tasks(state).get(str(task_id or "").strip())
    if not isinstance(task, dict):
        raise LumaError(f"builder task not found: {task_id}")
    _require_builder_task_owner(task, principal, task_id)
    binding, ttl = _builder_artifact_lease_binding(
        task, principal, body, task_id=task_id
    )
    node_name = str(task.get("builderNode") or "")
    record, download_token = ARTIFACT_DOWNLOADS.issue(
        binding, node_name=node_name, ttl_seconds=ttl
    )
    try:
        _queue_node_agent_task(
            state,
            node_name,
            "export-builder-artifact",
            {
                "leaseId": record.lease_id,
                "builderTaskId": task_id,
                "artifact": {
                    "name": binding.artifact_name,
                    "digest": binding.digest,
                    "mediaType": binding.media_type,
                    "sizeBytes": binding.size_bytes,
                },
            },
            required_capability="builder-artifact-export-v1",
        )
    except BaseException:
        ARTIFACT_DOWNLOADS.revoke(record.lease_id)
        raise
    expires_at = datetime.fromtimestamp(record.expires_at, timezone.utc)
    return {
        "schemaVersion": "luma.artifact-download-lease/v1",
        "leaseId": record.lease_id,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        "downloadToken": download_token,
        "binding": binding.public_body(),
    }


def _require_current_artifact_lease(record: ArtifactLeaseRecord) -> None:
    state = load_state()
    task = _builder_tasks(state).get(record.binding.builder_task_id)
    if (
        not isinstance(task, dict)
        or str(task.get("kind") or "") != "analyze-source"
        or str(task.get("status") or "") != "succeeded"
        or str(task.get("principalRef") or "") != record.binding.principal_ref
        or str(task.get("tenantRef") or "") != record.binding.tenant_ref
        or str(task.get("applicationRef") or "") != record.binding.application_ref
        or str(task.get("externalOperationId") or "")
        != record.binding.external_operation_id
    ):
        raise LumaError("artifact download lease not found")


def _artifact_upload_context(
    token: str,
    lease_id: str,
    *,
    node_name: str,
    node_id: str,
    media_type: str,
    digest: str,
    content_length: int,
) -> ArtifactLeaseRecord:
    state = load_state()
    _require_node_agent_token(state, token, node_name, node_id=node_id)
    record = ARTIFACT_DOWNLOADS.get_record(lease_id)
    _require_current_artifact_lease(record)
    if (
        record.node_name != node_name
        or record.binding.media_type != media_type
        or record.binding.digest != digest
        or record.binding.size_bytes != content_length
    ):
        raise LumaError("artifact upload binding is invalid")
    return record


def handle_node_agent_artifact_upload(
    token: str,
    lease_id: str,
    *,
    node_name: str,
    node_id: str,
    media_type: str,
    digest: str,
    content_length: int,
    chunks: Any,
) -> Dict[str, Any]:
    _artifact_upload_context(
        token,
        lease_id,
        node_name=node_name,
        node_id=node_id,
        media_type=media_type,
        digest=digest,
        content_length=content_length,
    )
    ARTIFACT_DOWNLOADS.accept_upload(
        lease_id,
        node_name=node_name,
        media_type=media_type,
        digest=digest,
        content_length=content_length,
        chunks=chunks,
    )
    return {"leaseId": lease_id, "accepted": True}


async def handle_node_agent_artifact_upload_async(
    token: str,
    lease_id: str,
    *,
    node_name: str,
    node_id: str,
    media_type: str,
    digest: str,
    content_length: int,
    chunks: AsyncIterator[bytes],
) -> Dict[str, Any]:
    await run_in_threadpool(
        functools.partial(
            _artifact_upload_context,
            token,
            lease_id,
            node_name=node_name,
            node_id=node_id,
            media_type=media_type,
            digest=digest,
            content_length=content_length,
        )
    )
    await ARTIFACT_DOWNLOADS.accept_upload_async(
        lease_id,
        node_name=node_name,
        media_type=media_type,
        digest=digest,
        content_length=content_length,
        chunks=chunks,
    )
    return {"leaseId": lease_id, "accepted": True}


def handle_builder_artifact_download(
    token: str, lease_id: str
) -> ArtifactLeaseRecord:
    record = ARTIFACT_DOWNLOADS.redeem(lease_id, token)
    try:
        _require_current_artifact_lease(record)
        return record
    except BaseException:
        ARTIFACT_DOWNLOADS.complete(lease_id)
        raise


def handle_builder_task_get(token: str, task_id: str) -> Dict[str, Any]:
    state = load_state()
    principal = _require_lae_service_principal(state, token)
    task = _builder_tasks(state).get(str(task_id or "").strip())
    if not isinstance(task, dict):
        raise LumaError(f"builder task not found: {task_id}")
    _require_builder_task_owner(task, principal, task_id)
    return {"task": _builder_task_public(task)}


def handle_builder_task_events(
    token: str,
    task_id: str,
    *,
    after: int = 0,
    limit: int = 200,
) -> Dict[str, Any]:
    state = load_state()
    principal = _require_lae_service_principal(state, token)
    task = _builder_tasks(state).get(str(task_id or "").strip())
    if not isinstance(task, dict):
        raise LumaError(f"builder task not found: {task_id}")
    _require_builder_task_owner(task, principal, task_id)
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise LumaError("after must be a non-negative integer cursor")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 500:
        raise LumaError("limit must be an integer between 1 and 500")
    events = [event for event in task.get("events") or [] if isinstance(event, dict)]
    oldest_cursor = int(events[0].get("cursor") or 0) if events else int(task.get("nextEventCursor") or 1)
    if after and events and after < oldest_cursor - 1:
        raise LumaError(f"builder task event cursor expired; oldest available cursor is {oldest_cursor}")
    remaining = [event for event in events if int(event.get("cursor") or 0) > after]
    page = remaining[:limit]
    next_cursor = int(page[-1].get("cursor") or after) if page else after
    return {
        "taskId": str(task.get("id") or task_id),
        "status": str(task.get("status") or ""),
        "events": page,
        "nextCursor": next_cursor,
        "oldestCursor": oldest_cursor,
        "hasMore": len(remaining) > len(page),
        "terminal": _builder_task_terminal(str(task.get("status") or "")),
    }


def handle_builder_task_cancel(token: str, task_id: str, _body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = load_state()
    principal = _require_lae_service_principal(state, token)
    now = int(time.time())

    def mutate(current: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        current_principal = _require_lae_service_principal(current, token)
        task = _builder_tasks(current).get(str(task_id or "").strip())
        if not isinstance(task, dict):
            raise LumaError(f"builder task not found: {task_id}")
        _require_builder_task_owner(task, current_principal, task_id)
        status = str(task.get("status") or "")
        if _builder_task_terminal(status) or status == "cancel_requested":
            return _builder_task_public(task), True
        agent_task_id = str(task.get("agentTaskId") or "")
        agent_task = _agent_tasks(current).get(agent_task_id)
        agent_status = str(agent_task.get("status") or "") if isinstance(agent_task, dict) else ""
        if status == "queued" and agent_status in {"", "queued"}:
            task["status"] = "canceled"
            task["message"] = "builder task canceled before lease"
            task["completedAt"] = now
            task["updatedAt"] = now
            if isinstance(agent_task, dict):
                agent_task["status"] = "canceled"
                agent_task["message"] = "builder task canceled before lease"
                agent_task["completedAt"] = now
                agent_task["updatedAt"] = now
            _builder_task_event(
                task,
                {"type": "status", "status": "canceled", "message": "builder task canceled before lease"},
                now=now,
            )
        else:
            task["status"] = "cancel_requested"
            task["cancelRequestedAt"] = now
            task["updatedAt"] = now
            if isinstance(agent_task, dict):
                agent_task["cancelRequestedAt"] = now
                agent_task["updatedAt"] = now
            _builder_task_event(
                task,
                {"type": "status", "status": "cancel_requested", "message": "builder task cancellation requested"},
                now=now,
            )
        return _builder_task_public(task), False

    task, replayed = _mutate_control_state(mutate)
    return {"task": task, "replayed": replayed}


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


def _build_run_agent_task(
    state: Dict[str, Any], run: Dict[str, Any]
) -> tuple[str, Dict[str, Any] | None]:
    tasks = _agent_tasks(state)
    linked_id = str(run.get("agentTaskId") or "")
    linked = tasks.get(linked_id)
    if linked_id and isinstance(linked, dict):
        return linked_id, linked

    # Build runs created by older Control versions were not linked to their
    # node-agent task. Match only the same source/node and a very small creation
    # window so an upgraded manager can still cancel an in-flight legacy build
    # without ever touching a different repository import.
    source = str(run.get("source") or "")
    node_name = str(run.get("buildNode") or "")
    created_at = int(run.get("createdAt") or 0)
    candidates: list[tuple[int, int, str, Dict[str, Any]]] = []
    for task_id, task in tasks.items():
        if not isinstance(task, dict) or str(task.get("action") or "") != "build-image":
            continue
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        if node_name and str(task.get("nodeName") or "") != node_name:
            continue
        if source and str(payload.get("repoUrl") or "") != source:
            continue
        task_created_at = int(task.get("createdAt") or 0)
        distance = abs(task_created_at - created_at)
        if created_at and task_created_at and distance > 30:
            continue
        candidates.append((distance, -task_created_at, str(task_id), task))
    if not candidates:
        return "", None
    _distance, _created, task_id, task = min(candidates, key=lambda item: item[:3])
    return task_id, task


def _build_run_cancel_requested(build_id: str) -> bool:
    state = load_state()
    run = _build_runs(state).get(build_id)
    return isinstance(run, dict) and str(run.get("status") or "") in {"canceling", "canceled"}


def handle_build_run_cancel(
    token: str, build_id: str, body: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    if body not in (None, {}):
        raise LumaError("build cancel request body must be empty")
    state = load_state()
    require_token(state, token, token_type="deploy")
    now = int(time.time())

    def mutate(current: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        require_token(current, token, token_type="deploy")
        run = _build_runs(current).get(build_id)
        if not isinstance(run, dict):
            raise LumaError(f"build run not found: {build_id}")
        run_status = str(run.get("status") or "")
        if run_status in {"succeeded", "failed", "canceled", "canceling"}:
            return _build_run_public(run), True

        task_id, task = _build_run_agent_task(current, run)
        task_status = str(task.get("status") or "") if isinstance(task, dict) else ""
        if isinstance(task, dict) and task_status not in {"queued", "running", "canceled"}:
            raise LumaError("build phase is already complete and cannot be canceled")

        message = "build canceled before it started" if task_status in {"", "queued"} else "build cancellation requested"
        if isinstance(task, dict):
            task["buildRunId"] = build_id
            run["agentTaskId"] = task_id
            if task_status == "queued":
                task.update(
                    {
                        "status": "canceled",
                        "message": message,
                        "result": {},
                        "completedAt": now,
                        "updatedAt": now,
                    }
                )
            elif task_status == "running":
                task["cancelRequestedAt"] = now
                task["updatedAt"] = now

        terminal = task_status in {"", "queued", "canceled"}
        run["status"] = "canceled" if terminal else "canceling"
        run["message"] = "build canceled" if terminal else message
        run["cancelRequestedAt"] = now
        run["updatedAt"] = now
        if terminal:
            run["completedAt"] = now
            run["canceledAt"] = now
        events = run.get("events") if isinstance(run.get("events"), list) else []
        events.append(
            {
                "name": "Build image",
                "status": "canceled" if terminal else "canceling",
                "message": run["message"],
                "ts": now,
            }
        )
        run["events"] = events[-max(int(BUILD_RUN_EVENT_LIMIT), 100) :]
        return _build_run_public(run), False

    run, replayed = _mutate_control_state(mutate)
    return {"run": run, "replayed": replayed}


def handle_deployment_history(token: str) -> Dict[str, Any]:
    """Return the append-only deployment-event log (native + compose deploys, CLI and
    dashboard), most recent first, for the Deployments timeline. Steps are omitted from
    the list to keep it lean; fetch a single entry for full step detail."""
    state = load_state()
    require_token(state, token, token_type="deploy")
    events = state.get("deploymentEvents") if isinstance(state.get("deploymentEvents"), list) else []
    # Stored in append (chronological) order. Reverse first so that same-second events
    # tie-break to insertion order (last appended = newest), then stable-sort by ts desc.
    items = [{k: v for k, v in event.items() if k != "steps"} for event in reversed(events) if isinstance(event, dict)]
    items.sort(key=lambda item: int(item.get("createdAt") or 0), reverse=True)
    return {"events": items}


def handle_deployment_history_get(token: str, event_id: str) -> Dict[str, Any]:
    """Return one deployment-event entry with its full step log."""
    state = load_state()
    require_token(state, token, token_type="deploy")
    events = state.get("deploymentEvents") if isinstance(state.get("deploymentEvents"), list) else []
    for event in events:
        if isinstance(event, dict) and str(event.get("id")) == event_id:
            return {"event": dict(event)}
    raise LumaError(f"deployment event not found: {event_id}")


def _build_run_retry_overrides(body: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    overrides: Dict[str, Any] = {}
    if body.get("envSecrets") is not None:
        overrides["envSecrets"] = _request_env_secrets(body)
    return overrides


def handle_build_run_retry(
    token: str,
    build_id: str,
    body: Dict[str, Any] | None = None,
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    run = (state.get("buildRuns") if isinstance(state.get("buildRuns"), dict) else {}).get(build_id)
    if not isinstance(run, dict):
        raise LumaError(f"build run not found: {build_id}")
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    retry_body = {key: value for key, value in request.items() if key != "envSecretNames"}
    retry_body.update(_build_run_retry_overrides(body))
    if not retry_body:
        raise LumaError(f"build run cannot be retried: {build_id}")
    return handle_build_deploy(token, retry_body, progress=progress, build_run_id=build_id)


def handle_application_update(
    token: str,
    body: Dict[str, Any],
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    name = str(body.get("name") or body.get("stack") or "").strip()
    if not name:
        raise LumaError("application name is required")
    record = _service_deployment_record(state, name) or _compose_deployment_record(state, name)
    if not isinstance(record, dict):
        raise LumaError(f"deployment not found: {name}")
    git_source = record.get("gitSource") if isinstance(record.get("gitSource"), dict) else {}
    if not git_source:
        raise LumaError(f"application {name} was not deployed from Git; open the editor to update it")
    update_body = {
        key: value
        for key, value in _sanitize_git_source(git_source).items()
        if key != "buildRunId"
    }
    if body.get("envSecrets") is not None:
        update_body["envSecrets"] = _request_env_secrets(body)
    if not (update_body.get("repoUrl") or (update_body.get("providerId") and update_body.get("repository"))):
        raise LumaError(f"application {name} has an incomplete Git source; open the editor to update it")
    return handle_build_deploy(token, update_body, progress=progress)


def handle_build_config_set(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    nodes_raw = body.get("nodes")
    node = str(body.get("node") or "").strip()
    nodes = [str(value).strip() for value in nodes_raw] if isinstance(nodes_raw, list) else []
    if node:
        nodes = [node]
    nodes = [value for value in nodes if value]
    default_node = str(body.get("defaultNode") or (nodes[0] if nodes else "")).strip()
    direct_egress_raw = body.get("directEgressNodes")
    if direct_egress_raw is not None and not isinstance(direct_egress_raw, list):
        raise LumaError("directEgressNodes must be a list")

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
        if direct_egress_raw is not None:
            direct_egress_nodes: list[str] = []
            for value in direct_egress_raw:
                name = str(value or "").strip()
                if not name:
                    continue
                entry = _node_record_entry_for_name_or_id(registered, name)
                if entry is None:
                    raise LumaError(f"unknown Luma node: {name}")
                direct_egress_nodes.append(entry[0])
            if direct_egress_nodes:
                build["directEgressNodes"] = sorted(set(direct_egress_nodes))
            else:
                build.pop("directEgressNodes", None)
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
        "message": str(run.get("message") or "")[:BUILD_RUN_SUMMARY_MESSAGE_LIMIT],
        "createdAt": int(run.get("createdAt") or 0),
        "updatedAt": int(run.get("updatedAt") or 0),
        "completedAt": int(run.get("completedAt") or 0),
    }


def _build_run_public(run: Dict[str, Any]) -> Dict[str, Any]:
    result = _build_run_public_summary(run)
    result["message"] = str(run.get("message") or "")[:BUILD_RUN_MESSAGE_LIMIT]
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
    entry = _node_record_entry_for_name_or_id(nodes, node_name)
    if not entry:
        return ""
    canonical_name, record = entry
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    build = state.get("build") if isinstance(state.get("build"), dict) else {}
    configured_direct = build.get("directEgressNodes") if isinstance(build.get("directEgressNodes"), list) else []
    if any(
        (candidate := _node_record_entry_for_name_or_id(nodes, str(value or "").strip()))
        and candidate[0] == canonical_name
        for value in configured_direct
    ) or str(labels.get("luma.egress.direct") or labels.get("egress.direct") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return ""
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
    config = load_config(_control_config_path())
    nomad_node_id = _node_record_nomad_node_id(removed) if isinstance(removed, dict) else ""
    shared_manager_identity = bool(
        nomad_node_id
        and _node_identity_is_owned_by_other_manager(
            nodes,
            removed_key=removed_key or node_name,
            node_id=nomad_node_id,
        )
    )
    if (
        isinstance(removed, dict)
        and _node_record_is_manager(removed)
        and not shared_manager_identity
    ):
        raise LumaError(f"refusing to unregister Nomad manager node: {node_name}")
    if shared_manager_identity:
        # Old Control versions could leave a worker alias pointing at the
        # manager's Nomad ID. Draining that ID while deleting the stale alias
        # would evict the control plane. Remove only the bad registration; the
        # real manager record remains authoritative and schedulable.
        nomad_node_id = ""
    if not nomad_node_id:
        if not shared_manager_identity:
            nomad_node_id = _find_nomad_node_id_for_unregister(
                config, state, node_name=node_name
            )
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
        "nomadDrainSkipped": (
            "shared_manager_identity" if shared_manager_identity else ""
        ),
        "message": message,
    }


def _node_record_nomad_node_id(record: Dict[str, Any]) -> str:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return str(record.get("nomadNodeId") or record.get("nodeId") or labels.get("luma.node.id") or "").strip()


def _node_identity_is_owned_by_other_manager(
    nodes: Dict[str, Any], *, removed_key: str, node_id: str
) -> bool:
    wanted = str(node_id or "").strip()
    if not wanted:
        return False
    for key, candidate in nodes.items():
        if str(key) == removed_key or not isinstance(candidate, dict):
            continue
        if not _node_record_is_manager(candidate):
            continue
        if wanted in _node_record_identity_ids(candidate):
            return True
    return False


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


def _build_proxy_for_request(
    config: LumaConfig,
    state: Dict[str, Any],
    build_node: str,
    body: Dict[str, Any],
) -> str:
    proxy_mode = str(body.get("proxyMode") or "auto").strip().lower()
    if proxy_mode not in {"auto", "direct"}:
        raise LumaError("proxyMode must be auto or direct")
    if proxy_mode == "direct":
        return ""
    if "proxy" in body:
        # An explicitly empty string is a supported direct override for API
        # clients that predate proxyMode; omission alone selects auto policy.
        return str(body.get("proxy") or "").strip()
    return _egress_proxy_for_node(config, state, build_node)


def _compose_import_image_aliases(build_result: Dict[str, Any]) -> Dict[str, str]:
    """Validate the image-alias contract returned by a current Builder."""

    explicit = build_result.get("imageAliases")
    if not isinstance(explicit, dict):
        raise LumaError(
            "Builder result is missing imageAliases; upgrade the Builder node"
        )
    aliases: Dict[str, str] = {}
    for source, target in explicit.items():
        source_text = str(source or "").strip()
        target_text = str(target or "").strip()
        if (
            not source_text
            or not target_text
            or len(source_text) > 512
            or len(target_text) > 512
        ):
            raise LumaError("Builder returned an invalid imageAliases mapping")
        aliases[source_text] = target_text
    return aliases


def _rewrite_compose_import_images(
    compose_content: str,
    *,
    build_result: Dict[str, Any],
) -> str:
    aliases = _compose_import_image_aliases(build_result)
    if not aliases:
        return compose_content
    try:
        compose = yaml.safe_load(compose_content) or {}
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid Builder Compose YAML: {exc}") from exc
    services = (
        compose.get("services")
        if isinstance(compose, dict) and isinstance(compose.get("services"), dict)
        else None
    )
    if services is None:
        raise LumaError("Builder Compose result requires a services mapping")
    for service in services.values():
        if not isinstance(service, dict):
            continue
        source_image = str(service.get("image") or "").strip()
        replacement = aliases.get(source_image)
        if replacement:
            service["image"] = replacement
    return yaml.safe_dump(compose, sort_keys=False, allow_unicode=False)




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
    compose_sidecar = str(body.get("composeSidecar") or "")
    if compose_sidecar:
        compose_sidecar = normalize_repo_relative_path(
            compose_sidecar, label="composeSidecar"
        )
        if str(body.get("manifest") or "").strip():
            raise LumaError("composeSidecar cannot be combined with manifest")
    steps: list[dict[str, str]] = []

    registry_host = str(body.get("registryHost") or "").strip()
    if not registry_host:
        registry_host = str(build_config.get("registryHost") or "").strip() or f"{_nomad_route_host_for_node(state, build_node)}:5000"
    repo = _image_repo_from_repo_url(repo_url)

    # git clone and BuildKit run on the build node. Preserve the distinction
    # between a missing proxy (auto policy) and an explicitly empty proxy/direct
    # mode; `body.get("proxy") or auto` would incorrectly erase that intent.
    config = load_config(Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml"))
    proxy = _build_proxy_for_request(config, state, build_node, body)

    # gitToken and registryAuth are injected at lease time (see
    # _agent_task_lease_payload) so they are never persisted in agentTasks state.
    build_payload: Dict[str, Any] = {
        "repoUrl": repo_url,
        "ref": ref,
        "registryHost": registry_host,
        "pushHost": str(body.get("pushHost") or build_config.get("pushHost") or "localhost:5000"),
        "repo": repo,
        "proxy": proxy,
        "buildTimeout": int(body.get("buildTimeout") or 7200),
    }
    if provider_id:
        build_payload["gitProviderId"] = provider_id
    for key in ("context", "dockerfile", "platform"):
        if body.get(key):
            build_payload[key] = str(body.get(key))
    if compose_sidecar:
        build_payload["composeSidecar"] = compose_sidecar

    run_body = dict(body)
    run_body["repoUrl"] = repo_url
    run_body["buildNode"] = build_node
    if provider_id:
        run_body["providerId"] = provider_id
    if build_run_id:
        _restart_build_run(build_run_id, run_body, source=repo_url, build_node=build_node)
    else:
        build_run_id = _create_build_run(run_body, source=repo_url, build_node=build_node)
    git_source = _git_source_from_build_body(run_body, repo_url=repo_url, build_node=build_node, build_run_id=build_run_id)

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
                timeout=int(body.get("buildTimeout") or 7200) + 120,
                required_capability="docker-build",
                progress=run_progress,
                build_run_id=build_run_id,
            ),
            progress=run_progress,
        )
        if _build_run_cancel_requested(build_run_id):
            raise LumaError("build canceled")
        built_image = str(build_result.get("image") or "").strip()
        built_images = build_result.get("images") if isinstance(build_result.get("images"), dict) else {}
        if not built_image and not built_images:
            raise LumaError("build did not return an image reference")
        repo_manifest = str(build_result.get("manifest") or "").strip()
        repo_compose_content = str(build_result.get("composeContent") or "").strip()
        if compose_sidecar and (
            str(build_result.get("kind") or "") != "compose"
            or str(build_result.get("composeSidecar") or "") != compose_sidecar
        ):
            raise LumaError(
                "builder did not honor the selected composeSidecar; update the builder node agent"
            )

        manifest_text = str(body.get("manifest") or "").strip() or repo_manifest
        if not manifest_text:
            raise LumaError("no Luma deployment manifest found in repository and no manifest provided")

        if str(build_result.get("kind") or "") == "compose" or repo_compose_content or body.get("composeContent"):
            # The Builder result is authoritative for repository imports.  It
            # replaces every service that consumes a built Compose image with
            # the immutable internal-registry reference.  Reusing a
            # caller-supplied/source copy here would silently discard that
            # rewrite and make sibling workers pull the original logical or
            # retired external tag.
            compose_content = repo_compose_content or str(
                body.get("composeContent") or ""
            ).strip()
            if not compose_content:
                raise LumaError("luma.compose.yml found but composeContent is missing")
            compose_content = _rewrite_compose_import_images(
                compose_content,
                build_result=build_result,
            )
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
            deploy_body["gitSource"] = git_source
            deploy_body.pop("repoUrl", None)
            result = handle_compose_deployment(token, deploy_body, progress=run_progress)
            if isinstance(result, dict):
                merged_steps = steps + list(result.get("steps") or [])
                result = {
                    **result,
                    "steps": merged_steps,
                    "image": built_image,
                    "images": built_images,
                    "buildRunId": build_run_id,
                    **({"composeSidecar": compose_sidecar} if compose_sidecar else {}),
                }
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
        deploy_body["gitSource"] = git_source
        deploy_body.pop("repoUrl", None)
        result = handle_deployment(token, deploy_body, progress=run_progress)
        if isinstance(result, dict):
            merged_steps = steps + list(result.get("steps") or [])
            result = {**result, "steps": merged_steps, "image": built_image, "buildRunId": build_run_id}
        _complete_build_run(build_run_id, "succeeded", result=result if isinstance(result, dict) else {})
        return result
    except LumaError as exc:
        _complete_build_run(
            build_run_id,
            "canceled" if _build_run_cancel_requested(build_run_id) else "failed",
            message=str(exc),
        )
        raise
    except Exception as exc:
        _complete_build_run(
            build_run_id,
            "canceled" if _build_run_cancel_requested(build_run_id) else "failed",
            message=str(exc),
        )
        raise


def _docker_restart_allocation_ids(result: Any) -> set[str]:
    if not isinstance(result, dict) or not bool(result.get("dockerRestarted")):
        return set()
    raw = result.get("affectedAllocationIds")
    if not isinstance(raw, list):
        return set()
    return {str(value).strip() for value in raw if str(value).strip()}


def _nomad_running_allocation_counts(allocations: Any, *, excluded_ids: set[str] | None = None) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    excluded = excluded_ids or set()
    if not isinstance(allocations, list):
        return counts
    for allocation in allocations:
        if not _nomad_allocation_is_running(allocation):
            continue
        allocation_id = str(allocation.get("ID") or allocation.get("id") or "").strip()
        if allocation_id in excluded:
            continue
        group = str(allocation.get("TaskGroup") or allocation.get("task_group") or "").strip()
        counts[group] = counts.get(group, 0) + 1
    return counts


def _reconcile_allocations_after_docker_restart(
    state: Dict[str, Any],
    node_name: str,
    allocation_ids: set[str] | list[str],
    *,
    timeout: int = DOCKER_RESTART_RECOVERY_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Recreate allocations whose Docker containers survived with detached CNI.

    Docker restarts can bring Nomad init/app containers back while leaving their
    CNI namespace with only loopback.  The node agent records the active Nomad
    allocation ids before restarting Docker; Control validates those ids against
    Nomad and then replaces them, waiting for the original per-job running count
    before declaring the daemon change complete.
    """

    requested_ids = {str(value).strip() for value in allocation_ids if str(value).strip()}
    if not requested_ids:
        return {
            "node": node_name,
            "affectedAllocationIds": [],
            "recreatedAllocationIds": [],
            "jobs": [],
            "message": "Docker restarted with no active Nomad Docker allocations",
        }

    config = load_config(_control_config_path())
    api = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    validated: Dict[str, Dict[str, Any]] = {}
    skipped: list[str] = []
    host_ports: set[int] = set()
    for allocation_id in sorted(requested_ids):
        try:
            allocation = api.request("GET", f"/v1/allocation/{urllib.parse.quote(allocation_id, safe='')}")
        except LumaError as exc:
            if "Nomad API error 404" in str(exc):
                skipped.append(allocation_id)
                continue
            raise
        if not isinstance(allocation, dict):
            raise LumaError(f"Nomad returned invalid allocation detail after Docker restart: {allocation_id}")
        allocation_node = _luma_node_name_for_nomad_allocation(state, allocation)
        if not allocation_node or _canonical_node_name(state, allocation_node) != _canonical_node_name(state, node_name):
            raise LumaError(
                f"refusing to recreate allocation {allocation_id}: Nomad places it on "
                f"{allocation_node or 'an unknown node'}, not {node_name}"
            )
        job_id = str(allocation.get("JobID") or allocation.get("job_id") or "").strip()
        if not job_id:
            raise LumaError(f"Nomad allocation has no job id after Docker restart: {allocation_id}")
        desired_status = str(allocation.get("DesiredStatus") or allocation.get("desired_status") or "run").lower()
        if desired_status not in {"run", "running"}:
            skipped.append(allocation_id)
            continue
        validated[allocation_id] = allocation
        host_ports.update(_nomad_allocation_host_ports(allocation))

    if not validated:
        return {
            "node": node_name,
            "affectedAllocationIds": sorted(requested_ids),
            "recreatedAllocationIds": [],
            "skippedAllocationIds": sorted(skipped),
            "jobs": [],
            "message": "Docker restart allocations were already terminal or absent in Nomad",
        }

    baselines: Dict[str, Dict[str, int]] = {}
    for allocation in validated.values():
        job_id = str(allocation.get("JobID") or allocation.get("job_id") or "").strip()
        if job_id in baselines:
            continue
        allocations = api.request("GET", f"/v1/job/{urllib.parse.quote(job_id, safe='')}/allocations")
        if not isinstance(allocations, list):
            raise LumaError(f"Nomad returned invalid allocations for job after Docker restart: {job_id}")
        baselines[job_id] = _nomad_running_allocation_counts(allocations)

    for allocation_id in sorted(validated):
        api.request("POST", f"/v1/allocation/{urllib.parse.quote(allocation_id, safe='')}/stop", None)

    deadline = time.monotonic() + max(1, int(timeout))
    last_counts: Dict[str, Dict[str, int]] = {}
    while True:
        ready = True
        last_counts = {}
        for job_id, expected in baselines.items():
            allocations = api.request("GET", f"/v1/job/{urllib.parse.quote(job_id, safe='')}/allocations")
            if not isinstance(allocations, list):
                raise LumaError(f"Nomad returned invalid allocations while recovering Docker restart: {job_id}")
            current = _nomad_running_allocation_counts(allocations, excluded_ids=set(validated))
            last_counts[job_id] = current
            affected_groups = {
                str(item.get("TaskGroup") or item.get("task_group") or "").strip()
                for item in validated.values()
                if str(item.get("JobID") or item.get("job_id") or "").strip() == job_id
            }
            for group in affected_groups:
                required = max(1, int(expected.get(group) or 0))
                if int(current.get(group) or 0) < required:
                    ready = False
        if ready:
            break
        if time.monotonic() >= deadline:
            details = "; ".join(
                f"{job_id}: expected={baselines[job_id]}, running={last_counts.get(job_id, {})}"
                for job_id in sorted(baselines)
            )
            raise LumaError(f"Nomad allocations did not recover after Docker restart on {node_name}: {details}")
        time.sleep(1)

    cni_hostports = _refresh_nomad_cni_hostports_for_nodes(state, {node_name}, ports=sorted(host_ports))
    jobs = sorted(baselines)
    return {
        "node": node_name,
        "affectedAllocationIds": sorted(requested_ids),
        "recreatedAllocationIds": sorted(validated),
        "skippedAllocationIds": sorted(skipped),
        "jobs": jobs,
        "running": last_counts,
        "cniHostports": cni_hostports,
        "message": f"Recovered {len(validated)} Nomad allocation(s) after Docker restart on {node_name}",
    }


def _require_registry_node(state: Dict[str, Any], node_name: str) -> Dict[str, Any]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if not isinstance(record, dict):
        raise LumaError(f"unknown Luma node: {node_name}")
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    if str(agent.get("os") or "") != "linux" or not _node_agent_is_ready(
        record, required_capability="docker-image"
    ):
        raise LumaError(
            f"registry node must be a ready Linux node with docker-image capability: {node_name}"
        )
    return record


def _mirror_registry_runtime_image(
    state: Dict[str, Any],
    config: LumaConfig,
    source_image: str,
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> str:
    build = _build_config(state)
    build_node = _require_build_node(
        state,
        str(build.get("defaultNode") or DEFAULT_BUILD_NODE_NAME),
        purpose="registry system image mirror",
    )
    push_host = normalize_registry_host(str(build.get("pushHost") or ""))
    pull_host = normalize_registry_host(str(build.get("registryHost") or ""))
    tag = hashlib.sha256(source_image.encode("utf-8")).hexdigest()[:20]
    push_image = f"{push_host}/luma-system/registry-runtime:{tag}"
    destination_image = f"{pull_host}/luma-system/registry-runtime:{tag}"
    insecure_raw = str(
        os.environ.get("LUMA_LAE_BUILDER_REGISTRY_INSECURE") or ""
    ).strip()
    if insecure_raw not in {"0", "1"}:
        raise LumaError(
            "LUMA_LAE_BUILDER_REGISTRY_INSECURE must be explicitly set to 0 or 1"
        )
    step = {
        "name": "Mirror registry runtime image",
        "status": "start",
        "message": f"{source_image} via {build_node}",
    }
    _emit_progress(progress, step)
    result = _run_node_agent_task(
        state,
        build_node,
        "mirror-system-image",
        {
            "sourceImage": source_image,
            "pushImage": push_image,
            "destinationImage": destination_image,
            "platform": "linux/amd64",
            "proxy": _egress_proxy_for_node(config, state, build_node),
            "insecure": insecure_raw == "1",
            "timeout": 900,
        },
        timeout=960,
        required_capability="system-image-mirror-v1",
    )
    digest = str(result.get("digest") or "").strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise LumaError("registry system image mirror returned no verified digest")
    _emit_progress(
        progress,
        {
            "name": "Mirror registry runtime image",
            "status": "ok",
            "message": digest,
        },
    )
    return f"{destination_image}@{digest}"


def _registry_basic_auth_labels(
    service_name: str, username: str, password: str
) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
        raise LumaError("registry username is invalid")
    if len(password) < 32 or len(password) > 512:
        raise LumaError("registry password must contain between 32 and 512 characters")
    middleware = f"{slugify(service_name)}-auth"
    verifier = base64.b64encode(
        hashlib.sha1(password.encode("utf-8")).digest()
    ).decode("ascii")
    return [
        f"traefik.http.middlewares.{middleware}.basicauth.users={username}:{{SHA}}{verifier}",
        f"traefik.http.routers.{slugify(service_name)}.middlewares={middleware}",
    ]


def _verify_authenticated_registry(
    domain: str, username: str, password: str, *, timeout: int = 300
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(10, int(timeout))
    authorization = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")
    last_error = ""
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"https://{domain}/v2/",
            headers={"Authorization": f"Basic {authorization}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if int(response.status) == 200:
                    return {"status": 200, "authenticated": True}
                last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise LumaError(
        f"authenticated registry endpoint did not become ready: {domain}: {last_error}"
    )


def handle_registry_serve(token: str, body: Dict[str, Any], *, progress: Callable[[dict[str, str]], None] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    _apply_state_secrets(state)
    build_node = str(body.get("node") or body.get("buildNode") or "").strip()
    if not build_node:
        raise LumaError("node is required (the Linux node that hosts the registry)")
    record = _require_registry_node(state, build_node)
    port = int(body.get("port") or 5000)
    image = str(body.get("image") or "registry:2").strip()
    name = str(body.get("name") or "luma-registry").strip()
    domain = str(body.get("domain") or "").strip().lower().rstrip(".")
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    secure_public = bool(domain)
    if secure_public and (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
        or not username
        or not password
    ):
        raise LumaError(
            "public registry requires a valid domain, username, and password"
        )
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    region = str(record.get("region") or (record.get("labels") or {}).get("region") or "cn").strip() or "cn"
    if secure_public and region not in {"cn", "global"}:
        raise LumaError(
            "public TLS registry must run on a cn or global edge node"
        )
    registry_host = domain if secure_public else f"{_nomad_route_host_for_node(state, build_node)}:{port}"

    steps: list[dict[str, str]] = []
    config = load_config(Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml"))
    runtime_image = image
    if secure_public:
        current_registry = str(_build_config(state).get("registryHost") or "")
        if not current_registry or normalize_registry_host(
            registry_host_from_image(image)
        ) != normalize_registry_host(current_registry):
            runtime_image = _mirror_registry_runtime_image(
                state, config, image, progress=progress
            )

    manifest = {
        "name": name,
        "image": runtime_image,
        "region": region,
        "exposure": (
            "cn-edge"
            if secure_public and region == "cn"
            else "external-edge"
            if secure_public
            else "none"
        ),
        "node": build_node,
        "port": port,
        "volumes": [f"{name}-data:/var/lib/registry"],
    }
    storage_class = str(body.get("storageClass") or "").strip()
    if storage_class:
        manifest["storage"] = {
            f"{name}-data": {"storageClass": storage_class}
        }
    if secure_public:
        manifest["domain"] = domain
        manifest["labels"] = _registry_basic_auth_labels(
            name, username, password
        )
    else:
        manifest["publishPort"] = port
    manifest_text = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False)

    # Configure insecure-registries on every ready Linux node so any of them can
    # pull from the in-cluster registry (unpinned services schedule anywhere).
    # Do this before creating the registry allocation: configuring the builder
    # may restart Docker, which otherwise leaves the freshly-created Nomad CNI
    # namespace detached while the task is restarted inside the old allocation.
    # Skip the manager: Control runs in a container there, and restarting its
    # Docker daemon would kill this very request mid-stream. The manager's daemon
    # must be configured out-of-band if it also runs pulled workloads.
    configured: list[str] = []
    skipped: list[str] = []
    docker_recoveries: list[Dict[str, Any]] = []
    registry_no_proxy = _merge_no_proxy(EGRESS_NO_PROXY, *_no_proxy_entries_for_registry(registry_host))
    for node_name in ([] if secure_public else sorted(str(n) for n in nodes)):
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
        affected_allocation_ids: set[str] = set()
        configuration_error: LumaError | None = None
        try:
            registry_result = _run_node_agent_task(
                state,
                node_name,
                "configure-insecure-registry",
                {"registry": registry_host},
                timeout=240,
                required_capability=None,
            )
            affected_allocation_ids.update(_docker_restart_allocation_ids(registry_result))
            daemon_proxy = _docker_daemon_proxy_for_node(config, state, node_name, node_record)
            if daemon_proxy:
                proxy_result = _run_node_agent_task(
                    state,
                    node_name,
                    "configure-docker-egress-proxy",
                    {"proxy": daemon_proxy, "noProxy": registry_no_proxy},
                    timeout=240,
                    required_capability=None,
                )
                affected_allocation_ids.update(_docker_restart_allocation_ids(proxy_result))
        except LumaError as exc:
            configuration_error = exc
        if affected_allocation_ids:
            try:
                recovery = _reconcile_allocations_after_docker_restart(state, node_name, affected_allocation_ids)
            except LumaError as exc:
                raise LumaError(
                    f"Docker restarted on {node_name}, but its Nomad allocations did not recover automatically: {exc}"
                ) from exc
            docker_recoveries.append(recovery)
            recovery_step = {
                "name": f"Recover Docker restart on {node_name}",
                "status": "ok",
                "message": str(recovery.get("message") or "Nomad allocations recovered"),
            }
            steps.append(recovery_step)
            _emit_progress(progress, recovery_step)
        if configuration_error is not None:
            skipped.append(f"{node_name} ({configuration_error})")
        else:
            configured.append(node_name)
    registry_transport_step = {
        "name": (
            "Configure authenticated TLS registry"
            if secure_public
            else "Configure insecure-registries"
        ),
        "status": "ok",
        "message": (
            f"https://{domain} uses Traefik Basic Auth; Docker daemon restarts are not required"
            if secure_public
            else f"configured: {', '.join(configured) or 'none'}; skipped: {', '.join(skipped) or 'none'}"
        ),
    }
    steps.append(registry_transport_step)
    _emit_progress(progress, registry_transport_step)

    deploy_body = {"manifest": manifest_text, "sourceName": f"{name} (luma registry serve)"}
    deploy_result = handle_deployment(token, deploy_body, progress=progress)
    if isinstance(deploy_result, dict):
        steps.extend(s for s in (deploy_result.get("steps") or []) if isinstance(s, dict))

    verification: Dict[str, Any] = {}
    activated = False
    if secure_public:
        verification = _verify_authenticated_registry(
            domain, username, password
        )
        handle_registry_set(
            token,
            {"host": domain, "username": username, "password": password},
        )
        if bool(body.get("activate", True)):
            handle_build_config_set(
                token,
                {"registryHost": domain, "pushHost": domain},
            )
            activated = True

    return {
        "service": name,
        "registryHost": registry_host,
        "pushHost": domain if secure_public else f"localhost:{port}",
        "secure": secure_public,
        "authenticated": bool(verification.get("authenticated")),
        "activated": activated,
        "configuredNodes": configured,
        "skippedNodes": skipped,
        "dockerRestartRecoveries": docker_recoveries,
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


def _register_service_deployment(state: Dict[str, Any], service: ServiceSpec, manifest: str, source_name: str, *, git_source: Dict[str, Any] | None = None) -> None:
    deployments = _deployments_state(state)
    record: Dict[str, Any] = {
        "kind": "service",
        "name": service.name,
        "slug": service.slug,
        "manifest": manifest,
        "sourceName": source_name,
        "tcpRelayPorts": _service_tcp_relay_ports(service),
        "updatedAt": int(time.time()),
    }
    if git_source:
        record["gitSource"] = _sanitize_git_source(git_source)
    deployments["services"][service.slug] = record


def _mark_service_deployment(
    service: ServiceSpec,
    manifest: str,
    source_name: str,
    *,
    status: str,
    steps: list[dict[str, str]] | None = None,
    error: str = "",
    git_source: Dict[str, Any] | None = None,
) -> None:
    def mutate(state: Dict[str, Any]) -> None:
        _register_service_deployment(state, service, manifest, source_name, git_source=git_source)
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
    git_source = _deployment_git_source_from_body(body)
    if git_source:
        record["gitSource"] = git_source
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
        "gitSource": record.get("gitSource") if isinstance(record.get("gitSource"), dict) else None,
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
    git_source = _deployment_git_source_from_body(body)
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    runtime_config = _config_with_state_nodes(config, state)
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
    _mark_service_deployment(service, manifest, source_name, status="pending", steps=steps, git_source=git_source)

    try:
        service = _deploy_step(steps, "Resolve node pin", lambda: resolve_service_node_pin(service, state, engine=effective_engine), progress=progress)
        registry_auth = _registry_auth_for_service(state, service)
        service, image_result = _deploy_step(
            steps,
            "Cache image on Builder",
            lambda: resolve_service_image(
                config,
                service,
                registry_auth=registry_auth,
                state=state,
                progress=progress,
            ),
            progress=progress,
        )
        # The source credential was leased only to Builder.  Runtime receives
        # credentials for the internal pull reference, if that registry is
        # authenticated, never credentials for the external source registry.
        registry_auth = _registry_auth_for_service(state, service)
        _deploy_step(
            steps,
            "Check TCP relay ports",
            lambda: _ensure_tcp_relay_ports_available(state, kind="service", slug=service.slug, ports=_service_tcp_relay_ports(service)) or "TCP relay ports available",
            progress=progress,
        )
        _emit_tcp_ingress_refresh_advisory(steps, progress, state, _service_tcp_relay_ports(service))
        _mark_service_deployment(service, manifest, source_name, status="pending", steps=steps, git_source=git_source)
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
            lambda: render_nomad_job(runtime_config, service, registry_auth=registry_auth, secrets=secrets, egress_proxy_url=_egress_proxy_for_region(config, state, service.region)),
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
        routes_root = _resolve_control_path(config.routes_root, config_path)
        route_target: Path | None = None
        if service.exposure in EDGE_EXPOSURES:
            route_target = _resolve_control_path(route_path(config, service), config_path)
            _deploy_step(
                steps,
                "Remove stale file-provider route",
                lambda: _remove_nomad_provider_shadow_route(route_target),
                progress=progress,
            )
        elif service.exposure in {"tailscale-relay", "tcp-relay"}:
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
                _deploy_step(steps, "Write route", lambda: _write_route_file(route_target, route_text, routes_root=routes_root), progress=progress)
                written.append(str(route_target))
        probe_result = _deploy_step(
            steps,
            "Probe public route",
            lambda: _probe_public_route_with_recovery(
                token,
                service,
                stack=service.slug,
                skip_orchestrator=_skip_orchestrator(body),
                steps=steps,
                route_file=route_target if service.exposure == "tailscale-relay" else None,
                routes_root=routes_root,
                progress=progress,
            ),
            progress=progress,
        )
    except Exception as exc:
        # Any failure (LumaError, OSError from a full/read-only disk, raw socket
        # errors from the Nomad/DNS calls) must drive the record to a terminal
        # state. Leaving it at "pending" strands a ghost deploy that also blocks
        # later deploys (pending counts as occupying tcp-relay ports). bare raise
        # preserves the original exception for the caller.
        _mark_service_deployment(service, manifest, source_name, status="failed_partial", steps=steps, error=str(exc), git_source=git_source)
        _record_deployment_event(kind="service", name=service.name, slug=service.slug, source_name=source_name, origin=_deployment_origin(body), status="failed_partial", error=str(exc), steps=steps, git_source=git_source)
        raise
    _mark_service_deployment(service, manifest, source_name, status="active", steps=steps, git_source=git_source)
    _record_deployment_event(kind="service", name=service.name, slug=service.slug, source_name=source_name, origin=_deployment_origin(body), status="active", steps=steps, git_source=git_source)
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
    runtime_config = _config_with_state_nodes(config, state)
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
        runtime_config,
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
    route_target = (
        _resolve_control_path(route_path(config, service), config_path)
        if service.exposure in {*EDGE_EXPOSURES, "tailscale-relay", "tcp-relay"}
        else None
    )
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
        if service.exposure in {*EDGE_EXPOSURES, "tailscale-relay", "tcp-relay"}
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


@_serialize_deploy
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
    job_path = f"/v1/job/{urllib.parse.quote(stack, safe='')}"
    try:
        allocations = api.request("GET", f"{job_path}/allocations")
    except LumaError as exc:
        if "Nomad API error 404" not in str(exc):
            raise
        if mode != "recreate":
            raise LumaError(
                f"application job is missing: {stack}; use application-level restart to restore it"
            ) from exc
        return _restore_application_from_deployment_record(
            token,
            state,
            api,
            stack,
            service_name=service_name,
        )
    if not isinstance(allocations, list):
        raise LumaError(f"Nomad returned invalid allocations for job: {stack}")
    restarted = []
    recreated_allocation_ids: set[str] = set()
    known_allocation_ids: set[str] = set()
    application_node_names: set[str] = set()
    recreated_node_names: set[str] = set()
    recreated_host_ports: set[int] = set()
    force_evaluation = False
    stop_evaluation_id = ""
    for alloc in allocations:
        if not isinstance(alloc, dict):
            continue
        client_status = str(alloc.get("ClientStatus") or alloc.get("client_status") or "").lower()
        desired_value = alloc.get("DesiredStatus") or alloc.get("desired_status")
        desired_status = str(
            desired_value
            or ("stop" if client_status in {"complete", "failed", "lost"} else "run")
        ).lower()
        alloc_id = str(alloc.get("ID") or alloc.get("id") or "").strip()
        if alloc_id:
            known_allocation_ids.add(alloc_id)
        if mode == "recreate":
            # Application-level restart is also the recovery action for an
            # allocation stuck before task start (image pull, setup, etc.). A
            # pending or disconnected/unknown allocation still owns the
            # desired slot and must be stopped so Nomad creates a fresh
            # replacement. A terminal allocation without an existing
            # replacement needs a forced evaluation instead.
            if desired_status == "stop":
                continue
            if client_status not in {"running", "pending", "unknown"}:
                if not str(alloc.get("NextAllocation") or alloc.get("next_allocation") or "").strip():
                    force_evaluation = True
                continue
        elif client_status != "running":
            # Nomad's in-place task restart endpoint only applies to a running
            # allocation. Recovery of pending allocations belongs to recreate.
            continue
        if not alloc_id:
            continue
        allocation_node_name = _luma_node_name_for_nomad_allocation(state, alloc)
        if allocation_node_name:
            application_node_names.add(allocation_node_name)
        task_states = alloc.get("TaskStates") if isinstance(alloc.get("TaskStates"), dict) else {}
        if mode == "recreate":
            if service_name and service_name not in task_states:
                continue
            stop_result = api.request("POST", f"/v1/allocation/{urllib.parse.quote(alloc_id, safe='')}/stop", None)
            if isinstance(stop_result, dict) and stop_result.get("EvalID"):
                stop_evaluation_id = str(stop_result.get("EvalID") or "").strip()
            restarted.append({"allocId": alloc_id, "task": service_name or "*", "mode": mode})
            recreated_allocation_ids.add(alloc_id)
            node_name = allocation_node_name
            if node_name:
                recreated_node_names.add(node_name)
            recreated_host_ports.update(_nomad_allocation_host_ports(alloc))
            continue
        task_names = [service_name] if service_name else [str(name) for name in task_states] or [""]
        for task_name in task_names:
            payload = {"TaskName": task_name} if task_name else {}
            api.request("POST", f"/v1/client/allocation/{urllib.parse.quote(alloc_id, safe='')}/restart", payload)
            restarted.append({"allocId": alloc_id, "task": task_name or "*", "mode": mode})
    if mode == "task" and not restarted:
        suffix = f"/{service_name}" if service_name else ""
        raise LumaError(
            f"application has no running allocation to restart in place: {stack}{suffix}; "
            "use application-level restart to recover it"
        )
    job: Dict[str, Any] | None = None
    forced_evaluation_id = ""
    if mode == "recreate" and (not restarted or force_evaluation):
        raw_job = api.request("GET", job_path)
        if not isinstance(raw_job, dict):
            raise LumaError(f"Nomad returned invalid job definition: {stack}")
        job = raw_job
        evaluation = api.request(
            "POST",
            f"{job_path}/evaluate",
            {"JobID": stack, "EvalOptions": {"ForceReschedule": True}},
        )
        forced_evaluation_id = str(evaluation.get("EvalID") or "").strip() if isinstance(evaluation, dict) else ""
        if not forced_evaluation_id:
            raise LumaError(f"Nomad did not create a recovery evaluation for application: {stack}")
    result = {
        "clusterId": state["clusterId"],
        "stack": stack,
        "service": service_name,
        "mode": mode,
        "restarted": restarted,
    }
    try:
        if mode == "recreate":
            old_allocation_ids = known_allocation_ids or recreated_allocation_ids
            expected = (
                _nomad_job_desired_allocation_count(job, service_name=service_name)
                if job is not None
                else len(recreated_allocation_ids)
            )
            result["replacementAllocations"] = _wait_for_nomad_job_replacement(
                api,
                stack,
                old_allocation_ids,
                min_running=max(1, expected),
                evaluation_id=forced_evaluation_id or stop_evaluation_id,
            )
            if forced_evaluation_id:
                result["recovery"] = {
                    "strategy": "force-evaluate",
                    "evaluationId": forced_evaluation_id,
                }
        cni_hostports = _refresh_nomad_cni_hostports_for_nodes(state, recreated_node_names, ports=sorted(recreated_host_ports))
        if recreated_node_names or cni_hostports.get("results") or cni_hostports.get("skipped"):
            result["cniHostports"] = cni_hostports
        result["delivery"] = _reconcile_application_delivery(
            state,
            config,
            stack,
            allocation_node_names=application_node_names,
        )
    except Exception as exc:
        if mode == "recreate":
            _mark_application_restart_outcome(stack, status="failed_partial", error=str(exc))
        raise
    if mode == "recreate":
        _mark_application_restart_outcome(stack, status="active")
    return result


def _nomad_job_desired_allocation_count(job: Dict[str, Any] | None, *, service_name: str = "") -> int:
    if not isinstance(job, dict):
        return 1
    groups = [group for group in job.get("TaskGroups") or [] if isinstance(group, dict)]
    if service_name:
        matching = [
            group
            for group in groups
            if str(group.get("Name") or "") == service_name
            or any(
                isinstance(task, dict) and str(task.get("Name") or "") == service_name
                for task in group.get("Tasks") or []
            )
        ]
        if matching:
            groups = matching
    return max(1, sum(max(0, int(group.get("Count") or 1)) for group in groups))


def _mark_application_restart_outcome(stack: str, *, status: str, error: str = "") -> None:
    slug = slugify(stack)

    def mutate(state: Dict[str, Any]) -> None:
        deployments = _deployments_state(state)
        record = deployments["services"].get(slug) or deployments["compose"].get(slug)
        if not isinstance(record, dict):
            return
        record["status"] = status
        record["lastError"] = error
        record["updatedAt"] = int(time.time())

    mutate_state(mutate)


def _restore_application_from_deployment_record(
    token: str,
    state: Dict[str, Any],
    api: NomadApi,
    stack: str,
    *,
    service_name: str = "",
) -> Dict[str, Any]:
    """Recreate a GC'd Nomad job from Luma's saved deployment source of truth."""
    record = _service_deployment_record(state, stack)
    kind = "service"
    if not isinstance(record, dict):
        record = _compose_deployment_record(state, stack)
        kind = "compose"
    if not isinstance(record, dict):
        raise LumaError(
            f"application deployment record not found: {stack}; deploy or import it once before restart"
        )
    manifest = record.get("manifest")
    if not isinstance(manifest, str) or not manifest.strip():
        raise LumaError(f"saved application deployment manifest is missing: {stack}")
    deploy_body: Dict[str, Any] = {
        "manifest": manifest,
        "sourceName": str(record.get("sourceName") or f"{stack}.yaml"),
        "origin": "application-restart-recovery",
    }
    if isinstance(record.get("gitSource"), dict):
        deploy_body["gitSource"] = dict(record["gitSource"])
    if kind == "compose":
        compose_content = record.get("composeContent")
        if not isinstance(compose_content, str) or not compose_content.strip():
            raise LumaError(f"saved application compose content is missing: {stack}")
        deploy_body["composeContent"] = compose_content
        deployment_result = handle_compose_deployment(token, deploy_body)
    else:
        deployment_result = handle_deployment(token, deploy_body)
    allocations = api.request(
        "GET",
        f"/v1/job/{urllib.parse.quote(stack, safe='')}/allocations",
    )
    if not isinstance(allocations, list):
        raise LumaError(f"Nomad returned invalid allocations after restoring job: {stack}")
    replacements = sorted(
        str(allocation.get("ID") or allocation.get("id") or "").strip()
        for allocation in allocations
        if _nomad_allocation_is_running(allocation)
    )
    replacements = [allocation_id for allocation_id in replacements if allocation_id]
    if not replacements:
        raise LumaError(f"restored application has no running allocation: {stack}")
    return {
        "clusterId": state["clusterId"],
        "stack": stack,
        "service": service_name,
        "mode": "recreate",
        "restarted": [],
        "replacementAllocations": replacements,
        "recovery": {"strategy": "stored-deployment", "kind": kind},
        "delivery": {"status": "ready", "deployment": deployment_result},
    }


def _reconcile_application_delivery(
    state: Dict[str, Any],
    config: Any,
    stack: str,
    *,
    allocation_node_names: set[str] | None = None,
) -> Dict[str, Any]:
    """Restore the externally observable delivery state after an app restart.

    Nomad recreates allocations, but file-provider routes and DNS live outside
    Nomad. A restart is only complete after those derived resources have been
    reconstructed from Luma's stored deployment record and the public HTTP
    endpoints have converged again.
    """
    deployments = _deployments_state(state)
    slug = slugify(stack)
    service_record = deployments["services"].get(slug)
    compose_record = deployments["compose"].get(slug)
    if not isinstance(service_record, dict) and not isinstance(compose_record, dict):
        return {
            "status": "skipped",
            "message": "delivery reconcile skipped: deployment record is unavailable",
            "routes": [],
            "dns": [],
            "probes": [],
        }

    config_path = _control_config_path()
    routes_root = _resolve_control_path(config.routes_root, config_path)
    route_results: list[str] = []
    dns_results: list[str] = []
    probe_results: list[str] = []
    runtime_nodes = sorted({str(value).strip() for value in (allocation_node_names or set()) if str(value).strip()})

    file_route_exposures = {"tailscale-relay", "tcp-relay"}
    managed_route_exposures = {*EDGE_EXPOSURES, *file_route_exposures}

    def bind_runtime_node(service: ServiceSpec) -> ServiceSpec:
        # Nomad-provider edge routes already carry their replacement
        # allocation address. Only file-provider relays need one concrete node
        # written into a static upstream.
        if service.exposure not in file_route_exposures:
            return service
        if service.node or not runtime_nodes:
            return service
        if len(runtime_nodes) != 1:
            raise LumaError(
                f"{service.exposure} delivery reconcile cannot infer one node from allocations: "
                + ", ".join(runtime_nodes)
            )
        return replace(service, node=runtime_nodes[0])

    def reconcile_service(service: ServiceSpec, route_target: Path | None) -> None:
        route_service = service
        if service.exposure in EDGE_EXPOSURES:
            if route_target is None:
                raise LumaError(f"route target is required for {service.name}")
            route_results.append(_remove_nomad_provider_shadow_route(route_target))
        elif service.exposure in file_route_exposures:
            if route_target is None:
                raise LumaError(f"route target is required for {service.name}")
            route_service = resolve_nomad_static_route_target(
                service,
                state,
                prefer_publish_port=service.exposure == "tailscale-relay",
            )
            route_text = (
                render_tailscale_route(config, route_service)
                if service.exposure == "tailscale-relay"
                else render_tcp_route(config, route_service)
            )
            route_target.parent.mkdir(parents=True, exist_ok=True)
            route_results.append(_write_route_file(route_target, route_text, routes_root=routes_root))
        dns_results.append(sync_dns(config, route_service))
        if route_service.exposure in {"cn-edge", "external-edge", "tailscale-relay", "cloudflare-tunnel"} and route_service.domain:
            probe_results.append(_wait_for_public_route(route_service))

    if isinstance(service_record, dict):
        service, _source_name = _service_remove_request(service_record, stack)
        service = resolve_service_node_pin(service, state, engine="nomad")
        service = bind_runtime_node(service)
        route_target = (
            _resolve_control_path(route_path(config, service), config_path)
            if service.exposure in managed_route_exposures
            else None
        )
        reconcile_service(service, route_target)
        kind = "service"
    else:
        deployment, _source_name = _compose_remove_request(compose_record, stack)
        for service_name, service in deployment.services.items():
            if service.exposure == "none":
                continue
            service_spec = bind_runtime_node(_compose_service_as_service_spec(deployment, service))
            route_target = (
                _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
                if service.exposure in managed_route_exposures
                else None
            )
            reconcile_service(service_spec, route_target)
        kind = "compose"

    return {
        "status": "ready",
        "kind": kind,
        "routes": route_results,
        "dns": dns_results,
        "probes": probe_results,
    }


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
    # Rewrite the file's own bytes to trigger a Traefik reload; do not re-validate an
    # unchanged, already-live route file (it may predate the current renderer shape).
    _write_route_file(path, original, routes_root=routes_root, validate=False)
    return {
        "clusterId": state["clusterId"],
        "domain": domain,
        "routeId": str(route.get("id") or ""),
        "mode": "route-file-reload",
        "certResolver": cert_resolver,
        "path": str(path),
        "message": f"Route file reloaded for {domain}; Traefik will retry ACME if needed.",
    }


def handle_fleet_update(
    token: str,
    body: Dict[str, Any],
    *,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    install_ref = str(body.get("installRef") or "").strip()
    include_all = bool(body.get("includeAll"))
    include_manager = bool(body.get("includeManager"))
    per_node_timeout = int(body.get("timeout") or 900)
    per_node_timeout = min(max(per_node_timeout, 60), 3600)
    wait_ready_seconds = min(max(int(body.get("waitReadySeconds") or 0), 0), 300)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    raw_targets = body.get("nodeNames")
    if raw_targets is not None and not isinstance(raw_targets, list):
        raise LumaError("nodeNames must be a list")
    target_names = {
        str(value).strip()
        for value in (raw_targets or [])
        if str(value or "").strip()
    }
    unknown = sorted(target_names - {str(name) for name in nodes})
    if unknown:
        raise LumaError("unknown fleet update node(s): " + ", ".join(unknown))
    candidates = sorted(target_names if raw_targets is not None else {str(name) for name in nodes})
    results: list[Dict[str, Any]] = []
    for node_name in candidates:
        current_state = load_state()
        require_token(current_state, token, token_type="deploy")
        current_nodes = current_state.get("nodes") if isinstance(current_state.get("nodes"), dict) else {}
        record = current_nodes.get(node_name)
        if not isinstance(record, dict):
            continue
        ready_deadline = time.monotonic() + wait_ready_seconds
        while wait_ready_seconds and not _node_agent_is_ready(record) and time.monotonic() < ready_deadline:
            time.sleep(min(2.0, max(ready_deadline - time.monotonic(), 0.1)))
            current_state = load_state()
            current_nodes = current_state.get("nodes") if isinstance(current_state.get("nodes"), dict) else {}
            refreshed = current_nodes.get(node_name)
            if isinstance(refreshed, dict):
                record = refreshed
        agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
        if not include_all and raw_targets is None and not _node_agent_is_ready(record):
            continue
        item: Dict[str, Any] = {
            "nodeName": node_name,
            "region": str(record.get("region") or ""),
            "os": str(agent.get("os") or ""),
            "agentVersionBefore": str(agent.get("version") or ""),
            "status": "pending",
        }
        if progress:
            progress(dict(item))
        if _node_record_is_manager(record) and not include_manager:
            item["status"] = "skipped"
            item["message"] = (
                "manager node is skipped by default and updated separately by the control-plane rollout"
            )
            results.append(item)
            if progress:
                progress(dict(item))
            continue
        agent_status = _node_agent_status(record)
        capabilities = {str(value) for value in agent.get("capabilities") or []}
        if agent_status != "ready":
            item["status"] = "skipped"
            item["message"] = f"node agent is not ready; status={agent_status}"
            results.append(item)
            if progress:
                progress(dict(item))
            continue
        if "luma-update" not in capabilities:
            item["status"] = "skipped"
            item["message"] = (
                "node agent does not support fleet update; managed fleet update capability is missing"
            )
            results.append(item)
            if progress:
                progress(dict(item))
            continue
        config = load_config(_control_config_path())
        install_proxy = _egress_proxy_for_node(config, current_state, node_name)
        if install_proxy and "luma-update-proxy-v1" not in capabilities:
            item["status"] = "failed"
            item["message"] = (
                "node requires egress for the Luma installer, but its current agent predates "
                "luma-update-proxy-v1; perform the documented one-time exact-ref bootstrap "
                "through the root terminal, then use fleet updates normally"
            )
            results.append(item)
            if progress:
                progress(dict(item))
            continue
        try:
            item["status"] = "installing"
            item["message"] = f"Downloading and installing Luma {install_ref or 'latest'}."
            if progress:
                progress(dict(item))

            def install_progress(event: Dict[str, Any]) -> None:
                message = str(event.get("message") or event.get("line") or "").strip()
                if message:
                    item["status"] = "installing"
                    item["message"] = message
                    if progress:
                        progress(dict(item))

            install_payload = {"installRef": install_ref}
            if install_proxy:
                install_payload["proxy"] = install_proxy
            result = _run_node_agent_task(
                current_state,
                node_name,
                "update-luma",
                install_payload,
                timeout=per_node_timeout,
                required_capability="luma-update",
                progress=install_progress,
            )
            item["status"] = "succeeded"
            item["message"] = str(result.get("message") or "Luma installer finished")
            item["taskId"] = str(result.get("taskId") or "")
            if result.get("installRef"):
                item["installRef"] = str(result.get("installRef"))
            installed_version = str(result.get("installedVersion") or "").strip()
            if installed_version:
                item["installedVersion"] = installed_version
            if result.get("output"):
                item["output"] = str(result.get("output"))
            if wait_ready_seconds:
                item["status"] = "verifying"
                item["message"] = "Installer finished; waiting for the updated node agent to reconnect."
                if progress:
                    progress(dict(item))
                completed_at = int(time.time())
                expected_version = ""
                if re.fullmatch(r"v[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?", install_ref):
                    expected_version = install_ref[1:]
                elif installed_version:
                    expected_version = installed_version
                else:
                    raise LumaError(
                        "Installer did not report the installed version for this commit/branch update; "
                        "update this node with a release tag once before retrying an untagged ref."
                    )
                verify_deadline = time.monotonic() + wait_ready_seconds
                verified_agent: Dict[str, Any] | None = None
                while time.monotonic() < verify_deadline:
                    time.sleep(min(2.0, max(verify_deadline - time.monotonic(), 0.1)))
                    verified_state = load_state()
                    verified_nodes = (
                        verified_state.get("nodes")
                        if isinstance(verified_state.get("nodes"), dict)
                        else {}
                    )
                    verified_record = verified_nodes.get(node_name)
                    if not isinstance(verified_record, dict) or not _node_agent_is_ready(verified_record):
                        continue
                    candidate_agent = (
                        verified_record.get("agent")
                        if isinstance(verified_record.get("agent"), dict)
                        else {}
                    )
                    version_after = str(candidate_agent.get("version") or "")
                    if int(candidate_agent.get("lastSeen") or 0) <= completed_at:
                        continue
                    if expected_version and version_after != expected_version:
                        continue
                    verified_agent = candidate_agent
                    break
                if verified_agent is None:
                    item["status"] = "failed"
                    item["message"] = (
                        "Installer finished, but the updated node agent did not reconnect with the expected version "
                        f"within {wait_ready_seconds} seconds. Retry this node from the update center."
                    )
                else:
                    item["status"] = "succeeded"
                    item["agentVersionAfter"] = str(verified_agent.get("version") or "")
                    item["message"] = "Luma updated and the node agent reconnected successfully."
        except LumaError as exc:
            item["status"] = "failed"
            item["message"] = str(exc)
        results.append(item)
        if progress:
            progress(dict(item))
    succeeded = sum(1 for item in results if item.get("status") == "succeeded")
    failed = sum(1 for item in results if item.get("status") == "failed")
    skipped = sum(1 for item in results if item.get("status") == "skipped")
    return {
        "clusterId": str(state.get("clusterId") or ""),
        "installRef": install_ref,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


def _fleet_update_operation_dir() -> Path:
    return state_dir() / "fleet-updates"


def _fleet_update_operation_path(operation_id: str) -> Path:
    safe_id = str(operation_id or "").strip()
    if not re.fullmatch(r"fleet-[0-9]{10,}-[a-f0-9]{8}", safe_id):
        raise LumaError("invalid fleet update operation id")
    return _fleet_update_operation_dir() / f"{safe_id}.json"


def _fleet_update_operation_write(record: Dict[str, Any]) -> None:
    record["updatedAt"] = int(time.time())
    save_state(record, _fleet_update_operation_path(str(record.get("id") or "")))


def _fleet_update_operation_read(operation_id: str) -> Dict[str, Any]:
    path = _fleet_update_operation_path(operation_id)
    if not path.exists():
        raise LumaError("fleet update operation not found")
    return load_state(path)


def _fleet_update_public(record: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(record)
    # Installer output is useful for server-side support but far too noisy for
    # the dashboard and can contain host-local paths. The UI gets the bounded
    # message and structured phase for every node instead.
    nodes: list[Dict[str, Any]] = []
    for raw in result.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        item = {key: value for key, value in raw.items() if key != "output"}
        nodes.append(item)
    result["nodes"] = nodes
    final = result.get("result") if isinstance(result.get("result"), dict) else None
    if final:
        public_final = dict(final)
        public_final["results"] = [
            {key: value for key, value in item.items() if key != "output"}
            for item in public_final.get("results") or []
            if isinstance(item, dict)
        ]
        result["result"] = public_final
    return result


def handle_fleet_update_operation_start(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    install_ref = str(body.get("installRef") or "").strip()
    if not install_ref or len(install_ref) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", install_ref):
        raise LumaError("installRef must be a Git tag, branch, or commit")
    raw_targets = body.get("nodeNames")
    if raw_targets is not None and not isinstance(raw_targets, list):
        raise LumaError("nodeNames must be a list")
    request_body: Dict[str, Any] = {
        "installRef": install_ref,
        "includeAll": True,
        "includeManager": False,
        "timeout": min(max(int(body.get("timeout") or 900), 60), 3600),
        "waitReadySeconds": min(max(int(body.get("waitReadySeconds") or 30), 0), 300),
    }
    if raw_targets is not None:
        request_body["nodeNames"] = [str(value).strip() for value in raw_targets if str(value or "").strip()]
    now = int(time.time())
    operation_id = f"fleet-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    record: Dict[str, Any] = {
        "schemaVersion": "luma.fleet-update/v1",
        "id": operation_id,
        "clusterId": str(state.get("clusterId") or ""),
        "installRef": install_ref,
        "status": "queued",
        "createdAt": now,
        "updatedAt": now,
        "nodes": [],
        "request": {key: value for key, value in request_body.items() if key != "timeout"},
    }
    _fleet_update_operation_write(record)

    def update_node(event: Dict[str, Any]) -> None:
        with _FLEET_UPDATE_LOCK:
            current = _fleet_update_operation_read(operation_id)
            current["status"] = "running"
            nodes = [dict(item) for item in current.get("nodes") or [] if isinstance(item, dict)]
            node_name = str(event.get("nodeName") or "")
            public_event = {key: value for key, value in event.items() if key != "output"}
            replaced = False
            for index, item in enumerate(nodes):
                if str(item.get("nodeName") or "") == node_name:
                    nodes[index] = public_event
                    replaced = True
                    break
            if not replaced:
                nodes.append(public_event)
            current["nodes"] = nodes
            _fleet_update_operation_write(current)

    def run() -> None:
        try:
            with _FLEET_UPDATE_LOCK:
                current = _fleet_update_operation_read(operation_id)
                current["status"] = "running"
                current["startedAt"] = int(time.time())
                _fleet_update_operation_write(current)
            result = handle_fleet_update(token, request_body, progress=update_node)
            with _FLEET_UPDATE_LOCK:
                current = _fleet_update_operation_read(operation_id)
                current["result"] = result
                current["status"] = "succeeded" if not int(result.get("failed") or 0) and not int(result.get("skipped") or 0) else "attention"
                current["finishedAt"] = int(time.time())
                _fleet_update_operation_write(current)
        except Exception as exc:
            with _FLEET_UPDATE_LOCK:
                current = _fleet_update_operation_read(operation_id)
                current["status"] = "failed"
                current["message"] = str(exc)
                current["finishedAt"] = int(time.time())
                _fleet_update_operation_write(current)
        finally:
            with _FLEET_UPDATE_LOCK:
                _FLEET_UPDATE_THREADS.pop(operation_id, None)

    thread = threading.Thread(target=run, name=f"luma-{operation_id}", daemon=True)
    with _FLEET_UPDATE_LOCK:
        _FLEET_UPDATE_THREADS[operation_id] = thread
    thread.start()
    return _fleet_update_public(record)


def handle_fleet_update_operation_get(token: str, operation_id: str = "") -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    if operation_id:
        with _FLEET_UPDATE_LOCK:
            record = _fleet_update_operation_read(operation_id)
            if str(record.get("status") or "") in {"queued", "running"} and operation_id not in _FLEET_UPDATE_THREADS:
                record["status"] = "interrupted"
                record["message"] = "Control restarted while the fleet update was running; retry the unfinished nodes from this page."
                record["finishedAt"] = int(time.time())
                _fleet_update_operation_write(record)
        return _fleet_update_public(record)
    root = _fleet_update_operation_dir()
    records: list[Dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.glob("fleet-*.json"), key=lambda value: value.stat().st_mtime, reverse=True)[:20]:
            try:
                records.append(_fleet_update_public(load_state(path)))
            except (LumaError, OSError):
                continue
    return {"clusterId": str(state.get("clusterId") or ""), "operations": records}


def _manager_update_target(state: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    managers = [
        (str(name), record)
        for name, record in nodes.items()
        if isinstance(record, dict) and _node_record_is_manager(record)
    ]
    if len(managers) != 1:
        raise LumaError(f"expected exactly one manager node, found {len(managers)}")
    return managers[0]


def _default_control_image_for_ref(install_ref: str) -> str:
    if re.fullmatch(r"v[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?", install_ref):
        return f"ghcr.io/liutianjie/luma-control:{install_ref}"
    if re.fullmatch(r"[a-f0-9]{40}", install_ref):
        return f"ghcr.io/liutianjie/luma-control:sha-{install_ref[:7]}"
    return ""


def _control_image_prepare_dir() -> Path:
    return state_dir() / "control-image-preparations"


def _control_image_prepare_path(operation_id: str) -> Path:
    safe_id = str(operation_id or "").strip()
    if not re.fullmatch(r"image-[0-9]{10,}-[a-f0-9]{8}", safe_id):
        raise LumaError("invalid control image preparation id")
    return _control_image_prepare_dir() / f"{safe_id}.json"


def _control_image_prepare_write(record: Dict[str, Any]) -> None:
    record["updatedAt"] = int(time.time())
    save_state(record, _control_image_prepare_path(str(record.get("id") or "")))


def _control_image_prepare_read(operation_id: str) -> Dict[str, Any]:
    path = _control_image_prepare_path(operation_id)
    if not path.exists():
        raise LumaError("control image preparation not found")
    return load_state(path)


def _control_image_prepare_tag(install_ref: str, source_image: str) -> str:
    if re.fullmatch(r"v[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?", install_ref):
        return install_ref
    if re.fullmatch(r"[a-f0-9]{40}", install_ref):
        return f"sha-{install_ref[:7]}"
    tail = source_image.rsplit("/", 1)[-1]
    tag = tail.rsplit(":", 1)[-1] if ":" in tail and "@" not in tail else ""
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise LumaError("controlImage must have a tag when preparing a branch or non-standard release")
    return tag


def _control_image_prepare_plan(state: Dict[str, Any], install_ref: str, source_image: str) -> Dict[str, Any]:
    source = str(source_image or "").strip()
    if not source or any(ch.isspace() for ch in source) or any(ch in source for ch in ("'", '"', "`", "$", ";", "|", "&", "<", ">")):
        raise LumaError("controlImage is invalid")
    build = _build_config(state)
    pull_host_raw = str(build.get("registryHost") or "").strip()
    if not pull_host_raw:
        raise LumaError("internal build registryHost is not configured; configure Builder registry settings before updating Control")
    pull_host = normalize_registry_host(pull_host_raw)
    source_host = normalize_registry_host(registry_host_from_image(source))
    if source_host == pull_host:
        return {
            "sourceImage": source,
            "destinationImage": source,
            "alreadyInternal": True,
        }
    push_host_raw = str(build.get("pushHost") or "").strip()
    if not push_host_raw:
        raise LumaError("internal build pushHost is not configured; configure Builder registry settings before updating Control")
    push_host = normalize_registry_host(push_host_raw)
    build_node = _require_build_node(
        state,
        str(build.get("defaultNode") or DEFAULT_BUILD_NODE_NAME).strip(),
        purpose="control image preparation",
    )
    _manager_name, manager_record = _manager_update_target(state)
    platform = _nomad_node_platform_from_record(manager_record) or "linux/amd64"
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise LumaError(
            f"manager platform is not supported for Control image preparation: {platform}"
        )
    tag = _control_image_prepare_tag(install_ref, source)
    insecure_raw = str(os.environ.get("LUMA_LAE_BUILDER_REGISTRY_INSECURE") or "").strip()
    if insecure_raw not in {"0", "1"}:
        raise LumaError("LUMA_LAE_BUILDER_REGISTRY_INSECURE must be explicitly set to 0 or 1")
    config = load_config(_control_config_path())
    return {
        "sourceImage": source,
        "pushImage": f"{push_host}/luma-control:{tag}",
        "destinationImage": f"{pull_host}/luma-control:{tag}",
        "builderNode": build_node,
        "platform": platform,
        "proxy": _egress_proxy_for_node(config, state, build_node),
        "insecure": insecure_raw == "1",
        "alreadyInternal": False,
    }


def _control_image_prepare_public(record: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(record)
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    if plan:
        result["plan"] = {key: value for key, value in plan.items() if key != "proxy"}
    return result


def handle_control_image_prepare_start(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    install_ref = str(body.get("installRef") or "").strip()
    if not install_ref or len(install_ref) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", install_ref):
        raise LumaError("installRef must be a Git tag, branch, or commit")
    source_image = str(body.get("controlImage") or _default_control_image_for_ref(install_ref)).strip()
    if not source_image:
        raise LumaError("controlImage is required for a branch or non-standard ref")
    plan = _control_image_prepare_plan(state, install_ref, source_image)
    now = int(time.time())
    operation_id = f"image-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    record: Dict[str, Any] = {
        "schemaVersion": "luma.control-image-preparation/v1",
        "id": operation_id,
        "clusterId": str(state.get("clusterId") or ""),
        "installRef": install_ref,
        "sourceImage": source_image,
        "destinationImage": str(plan.get("destinationImage") or source_image),
        "builderNode": str(plan.get("builderNode") or ""),
        "status": "queued",
        "message": "Waiting for the Builder to cache the Control image.",
        "createdAt": now,
        "updatedAt": now,
        "plan": {key: value for key, value in plan.items() if key != "proxy"},
        "log": [],
    }
    if plan.get("alreadyInternal"):
        record.update(
            {
                "status": "succeeded",
                "message": "Control image already uses the internal registry.",
                "finishedAt": now,
                "result": {
                    "sourceImage": source_image,
                    "destinationImage": source_image,
                    "message": "Control image already uses the internal registry.",
                },
            }
        )
        _control_image_prepare_write(record)
        return _control_image_prepare_public(record)
    _control_image_prepare_write(record)

    def update_progress(event: Dict[str, Any]) -> None:
        message = str(event.get("message") or event.get("line") or "").strip()
        if not message:
            return
        with _CONTROL_IMAGE_PREPARE_LOCK:
            current = _control_image_prepare_read(operation_id)
            current["status"] = "running"
            current["message"] = message[:1000]
            lines = [str(value) for value in current.get("log") or []]
            lines.append(message[:4000])
            current["log"] = lines[-80:]
            _control_image_prepare_write(current)

    def run() -> None:
        try:
            with _CONTROL_IMAGE_PREPARE_LOCK:
                current = _control_image_prepare_read(operation_id)
                current["status"] = "running"
                current["startedAt"] = int(time.time())
                _control_image_prepare_write(current)
            result = _run_node_agent_task(
                state,
                str(plan.get("builderNode") or ""),
                "mirror-control-image",
                {
                    "sourceImage": str(plan.get("sourceImage") or ""),
                    "pushImage": str(plan.get("pushImage") or ""),
                    "destinationImage": str(plan.get("destinationImage") or ""),
                    "platform": str(plan.get("platform") or ""),
                    "proxy": str(plan.get("proxy") or ""),
                    "insecure": bool(plan.get("insecure")),
                    "timeout": 900,
                },
                timeout=960,
                required_capability="control-image-mirror-v1",
                progress=update_progress,
            )
            with _CONTROL_IMAGE_PREPARE_LOCK:
                current = _control_image_prepare_read(operation_id)
                current["status"] = "succeeded"
                current["message"] = str(result.get("message") or "Control image cached in the internal registry.")
                current["result"] = {key: value for key, value in result.items() if key != "taskId"}
                current["finishedAt"] = int(time.time())
                _control_image_prepare_write(current)
        except Exception as exc:
            with _CONTROL_IMAGE_PREPARE_LOCK:
                current = _control_image_prepare_read(operation_id)
                current["status"] = "failed"
                current["message"] = str(exc)[:2000]
                current["finishedAt"] = int(time.time())
                _control_image_prepare_write(current)
        finally:
            with _CONTROL_IMAGE_PREPARE_LOCK:
                _CONTROL_IMAGE_PREPARE_THREADS.pop(operation_id, None)

    thread = threading.Thread(target=run, name=f"luma-{operation_id}", daemon=True)
    with _CONTROL_IMAGE_PREPARE_LOCK:
        _CONTROL_IMAGE_PREPARE_THREADS[operation_id] = thread
    thread.start()
    return _control_image_prepare_public(record)


def handle_control_image_prepare_get(token: str, operation_id: str = "") -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    with _CONTROL_IMAGE_PREPARE_LOCK:
        if operation_id:
            record = _control_image_prepare_read(operation_id)
        else:
            root = _control_image_prepare_dir()
            candidates = sorted(root.glob("image-*.json"), key=lambda path: path.stat().st_mtime, reverse=True) if root.exists() else []
            if not candidates:
                return {"status": "none", "message": "No Control image preparation has been recorded."}
            record = load_state(candidates[0])
        record_id = str(record.get("id") or "")
        if str(record.get("status") or "") in {"queued", "running"} and record_id not in _CONTROL_IMAGE_PREPARE_THREADS:
            record["status"] = "interrupted"
            record["message"] = "Control restarted while the image was being prepared; retry from this page."
            record["finishedAt"] = int(time.time())
            _control_image_prepare_write(record)
        return _control_image_prepare_public(record)


def handle_manager_update_start(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    install_ref = str(body.get("installRef") or "").strip()
    if not install_ref or len(install_ref) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", install_ref):
        raise LumaError("installRef must be a Git tag, branch, or commit")
    control_image = str(body.get("controlImage") or _default_control_image_for_ref(install_ref)).strip()
    if not control_image:
        raise LumaError("controlImage is required for a branch or non-standard ref")
    domain = str(state.get("domain") or "").strip()
    if not domain:
        raise LumaError("control domain is not configured")
    control_environment_raw = body.get("controlEnvironment")
    if control_environment_raw is None:
        control_environment: Dict[str, str] = {}
    elif isinstance(control_environment_raw, dict):
        from ..nomad_render import CONTROL_JOB_ENV_ALLOWLIST, control_job_environment

        unknown = sorted(set(control_environment_raw) - CONTROL_JOB_ENV_ALLOWLIST)
        if unknown:
            raise LumaError(
                f"controlEnvironment key is not allowlisted: {unknown[0]}"
            )
        control_environment = control_job_environment(
            {str(name): str(value) for name, value in control_environment_raw.items()}
        )
        if set(control_environment) != set(control_environment_raw):
            raise LumaError("controlEnvironment values must not be empty")
    else:
        raise LumaError("controlEnvironment must be an object")
    node_name, _record = _manager_update_target(state)
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    watchdog_peers = sorted(
        {
            str(record.get("tailscaleIP") or "").strip()
            for name, record in nodes.items()
            if str(name) != node_name
            and isinstance(record, dict)
            and _node_agent_is_ready(record)
            and str(record.get("tailscaleIP") or "").strip()
        }
    )
    result = _run_node_agent_task(
        state,
        node_name,
        "start-manager-update",
        {
            "installRef": install_ref,
            "controlImage": control_image,
            "domain": domain,
            "controlEnvironment": control_environment,
            "tailscaleWatchdogPeers": watchdog_peers,
        },
        timeout=90,
        required_capability="manager-update-v1",
    )
    return {
        "clusterId": str(state.get("clusterId") or ""),
        "managerNode": node_name,
        **{key: value for key, value in result.items() if key != "taskId"},
        "taskId": str(result.get("taskId") or ""),
    }


def handle_manager_update_status(token: str, update_id: str = "") -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    node_name, _record = _manager_update_target(state)
    result = _run_node_agent_task(
        state,
        node_name,
        "manager-update-status",
        {"updateId": str(update_id or "")},
        timeout=60,
        required_capability="manager-update-v1",
    )
    result.pop("taskId", None)
    return {"clusterId": str(state.get("clusterId") or ""), "managerNode": node_name, **result}


def _sentinel_probe_public_route(domain: str) -> Dict[str, Any]:
    started = time.monotonic()
    request = urllib.request.Request(
        f"https://{domain}/",
        method="GET",
        headers={"User-Agent": f"Luma-Route-Sentinel/{__version__}"},
    )
    status = 0
    error = ""
    response_sample = ""
    content_type = ""
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            status = int(response.status or 0)
            response_headers = response.headers or {}
            content_type = str(response_headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        response_headers = exc.headers or {}
        content_type = str(response_headers.get("Content-Type") or "").lower()
        try:
            response_sample = exc.read(512).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            response_sample = ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        error = str(getattr(exc, "reason", exc))[:240]
    latency_ms = int((time.monotonic() - started) * 1000)
    # Authentication, redirects, and an application-owned JSON/HTML 404 prove
    # that Traefik published the route. Only Traefik's plain default 404 means
    # the router disappeared. Gateway errors remain failures.
    default_404 = bool(
        status == 404
        and (
            not response_sample
            or (
                "text/plain" in content_type
                and response_sample.strip().lower() == "404 page not found"
            )
        )
    )
    ok = bool(status and status < 500 and not default_404)
    return {"domain": domain, "status": status, "ok": ok, "latencyMs": latency_ms, "error": error}


def _sentinel_active_http_domains(
    config: Any,
    state: Dict[str, Any],
    route_files: dict[str, Dict[str, Any]],
    errors: list[str],
) -> set[str]:
    """Return public domains backed by a current job or active deployment.

    Route files are durable configuration and can outlive a removed job. They
    are therefore not, by themselves, proof that a route still belongs to the
    live fleet. The sentinel deliberately joins them with Nomad jobs and Luma's
    active deployment records so an old file cannot poison every Control
    upgrade baseline.
    """

    domains: set[str] = set()
    try:
        services = _dashboard_nomad_services(
            nomad_services_summary(config, state),
            route_files,
            state=state,
        )
    except Exception as exc:
        errors.append(f"Active route inventory unavailable: {exc}")
        services = []
    for service in services:
        domain = str(service.get("domain") or "").strip().lower()
        exposure = str(service.get("exposure") or "none")
        desired = int(service.get("desired") or 0)
        if domain and exposure != "none" and desired > 0:
            domains.add(domain)

    deployment_index, _compose_stacks = _dashboard_deployment_service_index(state)
    seen_entries: set[tuple[str, str, str]] = set()
    for entry in deployment_index.values():
        if not isinstance(entry, dict):
            continue
        identity = (
            str(entry.get("stack") or ""),
            str(entry.get("name") or ""),
            str(entry.get("routeId") or ""),
        )
        if identity in seen_entries:
            continue
        seen_entries.add(identity)
        if str(entry.get("deploymentStatus") or "") != "active":
            continue
        exposure = str(entry.get("exposure") or "none")
        if exposure in {"none", "tcp-relay"}:
            continue
        domain = str(entry.get("domain") or "").strip().lower()
        if domain:
            domains.add(domain)
    return domains


def handle_route_sentinel(token: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "luma.yaml")
    config = load_config(config_path)
    errors: list[str] = []
    route_files = _dashboard_route_files(config, config_path, errors)
    known_domains = _sentinel_active_http_domains(config, state, route_files, errors)
    requested = (body or {}).get("domains")
    if requested is not None and not isinstance(requested, list):
        raise LumaError("domains must be a list")
    domains = sorted(
        {
            str(value).strip().lower()
            for value in (requested or known_domains)
            if str(value or "").strip().lower() in known_domains
        }
    )
    if requested is not None:
        unknown = sorted({str(value).strip().lower() for value in requested if str(value or "").strip()} - known_domains)
        if unknown:
            raise LumaError("unknown route domain(s): " + ", ".join(unknown))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max(len(domains), 1), 12)) as executor:
        results = list(executor.map(_sentinel_probe_public_route, domains))
    failed = sum(1 for item in results if not item.get("ok"))
    return {
        "clusterId": str(state.get("clusterId") or ""),
        "checkedAt": int(time.time()),
        "total": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
        "results": results,
        "errors": errors,
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
    runtime_config = _config_with_state_nodes(config, state)
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
        target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
        deployment, image_cache = _deploy_step(
            steps,
            "Cache compose images on Builder",
            lambda: _cache_compose_images_on_builder(
                config,
                state,
                deployment,
                progress=progress,
            ),
            progress=progress,
        )
        stack_text = _deploy_step(
            steps,
            "Render compose Nomad job",
            lambda: render_compose_job(
                runtime_config,
                deployment,
                registry_auth_resolver=lambda image: _registry_auth_for_image(state, image),
                secrets=secrets,
                egress_proxy_url=_egress_proxy_for_region(config, state, deployment.region),
                node_records=_state_nodes(state),
            ),
            progress=progress,
        )
        _deploy_step(
            steps,
            "Check storage backend",
            lambda: _guard_compose_storage_switch(target, stack_text, deployment, previous_record=previous_record),
            progress=progress,
        )
        storage_preparation = _deploy_step(
            steps,
            "Prepare managed storage",
            lambda: _prepare_compose_managed_storage(deployment, state),
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

        routes_root = _resolve_control_path(config.routes_root, config_path)
        for service_name, service in deployment.services.items():
            if service.exposure in EDGE_EXPOSURES:
                route_target = _resolve_control_path(compose_route_path(config, deployment, service_name), config_path)
                _deploy_step(
                    steps,
                    f"Remove stale file-provider route {service_name}",
                    lambda route_target=route_target: _remove_nomad_provider_shadow_route(route_target),
                    progress=progress,
                )
                continue
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
            _deploy_step(
                steps,
                f"Write route {service_name}",
                lambda route_target=route_target, route_text=route_text, routes_root=routes_root: _write_route_file(route_target, route_text, routes_root=routes_root),
                progress=progress,
            )
            written.append(str(route_target))

        probe_results: list[str] = []
        for service in compose_public_services(deployment):
            service_spec = _compose_service_as_service_spec(deployment, service)
            route_file = (
                _resolve_control_path(compose_route_path(config, deployment, service.name), config_path)
                if service.exposure == "tailscale-relay"
                else None
            )
            probe_results.append(
                _deploy_step(
                    steps,
                    f"Probe public route {service.name}",
                    lambda service_spec=service_spec, route_file=route_file: _probe_public_route_with_recovery(
                        token,
                        service_spec,
                        stack=deployment.slug,
                        skip_orchestrator=_skip_orchestrator(body),
                        steps=steps,
                        route_file=route_file,
                        routes_root=routes_root,
                        progress=progress,
                    ),
                    progress=progress,
                )
            )
    except Exception as exc:
        # See handle_deployment: any failure (not just LumaError — OSError, raw
        # socket errors) must reach a terminal state so the record never strands
        # at "pending" and blocks later deploys.
        _mark_compose_deployment(deployment, body, source_name, status="failed_partial", steps=steps, error=str(exc))
        _record_deployment_event(kind="compose", name=deployment.name, slug=deployment.slug, source_name=source_name, origin=_deployment_origin(body), status="failed_partial", error=str(exc), steps=steps)
        raise
    _mark_compose_deployment(deployment, body, source_name, status="active", steps=steps)
    _record_deployment_event(kind="compose", name=deployment.name, slug=deployment.slug, source_name=source_name, origin=_deployment_origin(body), status="active", steps=steps)
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
        "imageCache": image_cache,
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
    runtime_config = _config_with_state_nodes(config, state)
    deployment = _load_compose_request(body, source_name)
    target = _resolve_control_path(compose_stack_path(config, deployment), config_path)
    _require_nomad_engine(str(config.defaults.get("engine") or "nomad"))
    _ensure_compose_exposure_supported_on_nodes(state, deployment)
    stack_text = render_compose_job(
        runtime_config,
        deployment,
        registry_auth_resolver=lambda image: _registry_auth_for_image(state, image),
        resolve_secrets=False,
        node_records=_state_nodes(state),
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
    # Storage preparation is independent from image resolution.  In
    # particular, repositories deployed through ``luma import`` legitimately
    # contain build-only services whose immutable images do not exist until
    # the Builder phase runs.  Requiring deploy-ready images here made the
    # supported preflight path unusable for exactly those applications.
    deployment = _load_compose_request(body, source_name, allow_build_services=True)
    _deploy_step(
        steps,
        "Resolve storage endpoints",
        lambda: resolve_storage_mounts(deployment, node_records=_state_nodes(state)),
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
    if mode == "managed":
        storage_class_record["exportName"] = _managed_storage_export_name(
            name, storage_class_record, state
        )
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
    export_name = str(item.get("exportName") or name)
    if export_name != name:
        return {
            "name": name,
            "node": str(storage_class.node or ""),
            "path": str(storage_class.path or ""),
            "prepared": "host NFS export reused",
            "reusedFrom": export_name,
        }
    host_result = _prepare_managed_nfs_host(storage_class, state)
    return host_result


def _managed_storage_export_name(
    name: str, item: Dict[str, Any], state: Dict[str, Any]
) -> str:
    storage_classes = (
        state.get("storageClasses")
        if isinstance(state.get("storageClasses"), dict)
        else {}
    )
    existing = storage_classes.get(name)
    if (
        isinstance(existing, dict)
        and _managed_storage_export_key(existing, state)
        == _managed_storage_export_key(item, state)
    ):
        return str(existing.get("exportName") or name)
    wanted = _managed_storage_export_key(item, state)
    for other_name, other in storage_classes.items():
        if str(other_name) == name or not isinstance(other, dict):
            continue
        if _managed_storage_export_key(other, state) == wanted:
            return str(other.get("exportName") or other_name)
    return name


def _managed_storage_export_key(
    item: Dict[str, Any], state: Dict[str, Any]
) -> tuple[str, str, str] | None:
    if str(item.get("mode") or "managed") != "managed":
        return None
    if str(item.get("provider") or "nfs") != "nfs":
        return None
    node_name = str(item.get("node") or "").strip()
    path = str(item.get("path") or "").strip()
    if not node_name or not path:
        return None
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    node_identity = (
        _node_record_nomad_node_id(record)
        if isinstance(record, dict)
        else node_name
    ) or node_name
    return ("nfs", node_identity, str(Path(path)))


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
    export_name = str(item.get("exportName") or name)
    storage_classes = (
        state.get("storageClasses")
        if isinstance(state.get("storageClasses"), dict)
        else {}
    )
    shared_by = next(
        (
            str(other_name)
            for other_name, other in storage_classes.items()
            if str(other_name) != name
            and isinstance(other, dict)
            and str(other.get("exportName") or other_name) == export_name
        ),
        "",
    )
    if shared_by:
        export_removed = f"retained: shared by {shared_by}"
    else:
        export_record = dict(item)
        export_record["exportName"] = export_name
        export_storage_class = _storage_class_spec_from_record(
            export_name, export_record
        )
        export_removed = _remove_local_nfs_export(export_storage_class, state)
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


def _load_compose_request(
    body: Dict[str, Any],
    source_name: str,
    *,
    allow_build_services: bool = False,
) -> ComposeDeploymentSpec:
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
            allow_build_services=allow_build_services,
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
    for volume in deployment.volumes.values():
        if volume.kind == "local":
            results.append(_prepare_local_volume_path(volume, state))
    return results


def _prepare_local_volume_path(volume: ComposeVolumeSpec, state: Dict[str, Any]) -> dict[str, str]:
    node_name = str(volume.local_node or "").strip()
    raw_path = str(volume.local_path or "").strip()
    path = Path(raw_path)
    if not node_name or not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise LumaError(
            f"local volume {volume.name} requires a node and an absolute path other than / without .."
        )
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if not record:
        names = ", ".join(sorted(str(name) for name in nodes)) or "none"
        raise LumaError(f"local volume {volume.name} references unknown Luma node: {node_name}. Registered nodes: {names}")

    root = str(path.parent)
    relative = path.name
    if not _storage_node_is_local(record, node_name):
        result = _run_node_agent_task(
            state,
            node_name,
            "prepare-managed-volume-path",
            {"root": root, "relative": relative},
        )
        return {
            "volume": volume.name,
            "node": node_name,
            "path": str(path),
            "prepared": str(result.get("message") or "local volume path ready"),
            "taskId": str(result.get("taskId") or ""),
        }

    try:
        _run_host_prep_command(
            f"install -d -m 0777 {shlex.quote(str(path))}; chmod 0777 {shlex.quote(str(path))}"
        )
    except LumaError as exc:
        raise LumaError(f"failed to create local volume path {path} on {node_name}: {exc}") from exc
    return {
        "volume": volume.name,
        "node": node_name,
        "path": str(path),
        "prepared": "local volume path ready",
    }


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
    command = (
        f"install -d -m 0777 {shlex.quote(str(full_path))}; "
        f"chmod 0777 {shlex.quote(str(full_path))}"
    )
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


def _config_with_state_nodes(config: LumaConfig, state: Dict[str, Any]) -> LumaConfig:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    if not nodes:
        return config
    raw = dict(config.raw)
    raw_nodes = dict(raw.get("nodes") or {})
    for node_name, record in nodes.items():
        if not isinstance(record, dict):
            continue
        key = str(node_name)
        labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
        merged = dict(raw_nodes.get(key) or {})
        host = str(record.get("hostname") or record.get("displayName") or labels.get("luma.node.name") or key)
        merged.setdefault("host", host)
        region = str(record.get("region") or labels.get("region") or "")
        if region:
            merged["region"] = region
        if _node_record_is_manager(record):
            merged["lumaLocalIngress"] = True
        for field in ("tailscaleIP", "tailscaleIp", "tailscaleName", "advertiseAddr", "publicIp", "public_ip"):
            value = str(record.get(field) or "").strip()
            if value:
                merged[field] = value
        raw_nodes[key] = merged
    raw["nodes"] = raw_nodes
    return LumaConfig(raw, config.path)


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
        if spec.kind == "unmanaged":
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


def _remove_nomad_provider_shadow_route(path: Path) -> str:
    """Remove a historical file-provider route for a Nomad edge service.

    ``cn-edge`` and ``external-edge`` are discovered through Traefik's Nomad
    provider.  A file-provider route with the same router name takes precedence
    and freezes the upstream at the allocation address/port that happened to be
    current when the file was written.  After an allocation recreate that stale
    file produces 404/502 responses even though the replacement task is healthy.

    Deleting the derived file is the reconciliation operation: Traefik then uses
    the allocation-aware Nomad service registration and follows every future
    dynamic-port change without another Control-side rewrite.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return f"Nomad provider route active; no file-provider shadow: {path}"
    except IsADirectoryError as exc:
        raise LumaError(f"route shadow path is not a file: {path}") from exc
    _fsync_directory(path.parent)
    return f"Removed stale file-provider route: {path}"


def _write_route_file(path: Path, text: str, *, routes_root: Path, validate: bool = True) -> str:
    # Freshly rendered route text is validated to catch a truncated/garbled write
    # before it reaches Traefik. Rewriting a file's own unchanged bytes (e.g. a cert
    # retry that just touches the file to trigger a reload) skips validation: the
    # on-disk content may predate the current renderer's shape, and re-validating it
    # would newly reject an untouched, working route file.
    if validate:
        _validate_route_file_text(path, text)
    return _atomic_write_text(path, text, temp_dir=_route_staging_dir(path, routes_root))


def _validate_route_file_text(path: Path, text: str) -> None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid route file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError(f"invalid route file {path}: expected mapping")
    http = data.get("http")
    tcp = data.get("tcp")
    if _route_section_has_router_and_service(http) or _route_section_has_router_and_service(tcp):
        return
    raise LumaError(f"invalid route file {path}: expected http/tcp routers and services")


def _route_section_has_router_and_service(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    routers = section.get("routers")
    services = section.get("services")
    return isinstance(routers, dict) and bool(routers) and isinstance(services, dict) and bool(services)


def _route_staging_dir(path: Path, routes_root: Path) -> Path:
    route_root = routes_root.resolve()
    target_parent = path.parent.resolve()
    if target_parent == route_root or route_root in target_parent.parents:
        return route_root.parent / ".luma-route-staging"
    return target_parent.parent / ".luma-route-staging"


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8", temp_dir: Path | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = temp_dir or path.parent
    staging_dir.mkdir(parents=True, exist_ok=True)
    tmp = staging_dir / f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    try:
        with tmp.open("w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        _fsync_directory(tmp.parent)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            fallback_dir = path.parent / ".luma-route-staging"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            same_device_tmp = fallback_dir / f".{path.stem}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
            try:
                shutil.copy2(tmp, same_device_tmp)
                with same_device_tmp.open("rb") as handle:
                    os.fsync(handle.fileno())
                _fsync_directory(same_device_tmp.parent)
                os.replace(same_device_tmp, path)
            finally:
                try:
                    same_device_tmp.unlink()
                except FileNotFoundError:
                    pass
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return f"File written atomically: {path}"


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def _guard_manager_docker_daemon_change(state: Dict[str, Any], node_name: str) -> None:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = _node_record_for_name(nodes, node_name)
    if isinstance(record, dict) and _node_record_is_manager(record):
        raise LumaError(
            f"Docker daemon configuration on manager node {node_name} requires a host-owned maintenance operation; "
            "Control will not restart the Docker daemon that is running itself. Configure the manager daemon on the host, then retry."
        )


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

    _guard_manager_docker_daemon_change(state, node_name)
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
    _guard_manager_docker_daemon_change(state, node_name)
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
            return _probe_status_message(url, int(resp.status), headers=resp.headers)
    except urllib.error.HTTPError as exc:
        body_sample = _read_probe_error_sample(exc)
        headers = exc.headers
        if int(exc.code) == 404 and not body_sample:
            headers, body_sample = _fetch_probe_body_sample(url, fallback_headers=headers)
        return _probe_status_message(url, int(exc.code), headers=headers, body_sample=body_sample)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        return f"Public route probe inconclusive: {url} ({exc})"


def _probe_public_route_with_recovery(
    token: str,
    service: ServiceSpec,
    *,
    stack: str,
    skip_orchestrator: bool,
    steps: list[dict[str, str]],
    route_file: Path | None = None,
    routes_root: Path | None = None,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> str:
    if skip_orchestrator:
        return "Public route probe skipped: orchestrator deploy skipped"
    try:
        return _probe_public_route(service)
    except LumaError as exc:
        if not _public_route_error_is_recoverable(exc):
            raise
        if _public_route_error_is_traefik_miss(exc):
            provider_message = _deploy_step(
                steps,
                "Reconcile Traefik provider",
                lambda: _reconcile_traefik_provider(route_file=route_file, routes_root=routes_root),
                progress=progress,
            )
            try:
                retry = _wait_for_public_route(
                    service,
                    timeout=TRAEFIK_PROVIDER_SETTLE_TIMEOUT_SECONDS,
                )
                return f"Recovered public route after provider reconciliation ({provider_message}): {retry}"
            except LumaError as provider_exc:
                if _public_route_error_is_traefik_miss(provider_exc):
                    _deploy_step(
                        steps,
                        "Recover Traefik ingress",
                        _recover_traefik_ingress,
                        progress=progress,
                    )
                    retry = _wait_for_public_route(service)
                    return f"Recovered public route after Traefik recreate: {retry}"
                exc = provider_exc

        # Gateway errors during an update are normally rollout convergence, not
        # a broken allocation.  Wait for the exact deployment first; only if the
        # route remains unhealthy after the full settle budget do we replace the
        # application allocation.
        try:
            retry = _wait_for_public_route(service)
            return f"Public route settled after rollout: {retry}"
        except LumaError as settled_exc:
            if not _public_route_error_is_gateway_failure(settled_exc):
                raise
        _deploy_step(
            steps,
            "Recover application upstream",
            lambda: _recover_public_route_allocation(token, stack),
            progress=progress,
        )
        retry = _wait_for_public_route(service)
        return f"Recovered public route after allocation recreate: {retry}"


def _public_route_error_is_recoverable(exc: LumaError) -> bool:
    message = str(exc)
    if "Public route unhealthy:" not in message:
        return False
    return _public_route_error_is_traefik_miss(exc) or _public_route_error_is_gateway_failure(exc)


def _public_route_error_is_traefik_miss(exc: LumaError) -> bool:
    return "Public route unhealthy:" in str(exc) and "Traefik router not found" in str(exc)


def _public_route_error_is_gateway_failure(exc: LumaError) -> bool:
    message = str(exc)
    return "Public route unhealthy:" in message and any(
        f"HTTP {status}" in message for status in RECOVERABLE_ROUTE_HTTP_STATUSES
    )


def _wait_for_public_route(
    service: ServiceSpec,
    *,
    timeout: int = PUBLIC_ROUTE_SETTLE_TIMEOUT_SECONDS,
    interval: float = PUBLIC_ROUTE_SETTLE_INTERVAL_SECONDS,
) -> str:
    deadline = time.monotonic() + max(0, float(timeout))
    last_error: LumaError | None = None
    while True:
        try:
            result = _probe_public_route(service)
            if not result.startswith("Public route probe inconclusive:"):
                return result
            last_error = LumaError(result)
        except LumaError as exc:
            if not _public_route_error_is_recoverable(exc):
                raise
            last_error = exc
        if time.monotonic() >= deadline:
            raise last_error or LumaError("Public route did not become ready")
        time.sleep(max(0.05, float(interval)))


def _reconcile_traefik_provider(*, route_file: Path | None, routes_root: Path | None) -> str:
    if route_file is None:
        return "waiting for Nomad provider convergence"
    if routes_root is None:
        raise LumaError("Traefik route root is unavailable")
    if not route_file.exists() or not route_file.is_file():
        raise LumaError(f"Traefik route file is unavailable: {route_file}")
    original = route_file.read_text(encoding="utf-8")
    _write_route_file(route_file, original, routes_root=routes_root, validate=False)
    return f"republished {route_file.name}"


def _wait_for_nomad_job_replacement(
    api: NomadApi,
    job_id: str,
    old_allocation_ids: set[str] | list[str],
    *,
    min_running: int = 1,
    timeout: int = PUBLIC_ROUTE_SETTLE_TIMEOUT_SECONDS,
    evaluation_id: str = "",
) -> list[str]:
    old_ids = {str(value).strip() for value in old_allocation_ids if str(value).strip()}
    deadline = time.monotonic() + max(1, int(timeout))
    last: list[str] = []
    while True:
        allocations = api.request("GET", f"/v1/job/{urllib.parse.quote(job_id, safe='')}/allocations")
        if not isinstance(allocations, list):
            raise LumaError(f"Nomad returned invalid allocations while recovering job: {job_id}")
        last = sorted(
            str(allocation.get("ID") or allocation.get("id") or "").strip()
            for allocation in allocations
            if _nomad_allocation_is_running(allocation)
            and str(allocation.get("ID") or allocation.get("id") or "").strip() not in old_ids
        )
        last = [value for value in last if value]
        if len(last) >= max(1, int(min_running)):
            return last
        if evaluation_id:
            evaluation = api.request(
                "GET",
                f"/v1/evaluation/{urllib.parse.quote(evaluation_id, safe='')}",
            )
            if isinstance(evaluation, dict):
                status = str(evaluation.get("Status") or "").lower()
                failed_task_groups = evaluation.get("FailedTGAllocs")
                # Nomad commonly completes the job-register evaluation and
                # records the unschedulable work in FailedTGAllocs while a
                # queued-allocs child evaluation becomes blocked. Treat the
                # placement failure as terminal here instead of waiting for
                # the replacement timeout just because the parent says
                # "complete".
                if status == "blocked" or (
                    isinstance(failed_task_groups, dict) and bool(failed_task_groups)
                ):
                    raise LumaError(_nomad_blocked_evaluation_message(job_id, evaluation))
                if status in {"cancelled", "failed"}:
                    description = str(evaluation.get("StatusDescription") or status)
                    raise LumaError(
                        f"application recovery evaluation {evaluation_id} {status}: {description}"
                    )
        if time.monotonic() >= deadline:
            raise LumaError(
                f"Nomad job {job_id} did not replace {len(old_ids)} allocation(s) within {timeout}s"
            )
        time.sleep(1)


def _nomad_blocked_evaluation_message(job_id: str, evaluation: Dict[str, Any]) -> str:
    failed = evaluation.get("FailedTGAllocs") if isinstance(evaluation.get("FailedTGAllocs"), dict) else {}
    constraints: list[str] = []
    requested_node = ""
    for detail in failed.values():
        if not isinstance(detail, dict):
            continue
        filtered = detail.get("ConstraintFiltered") if isinstance(detail.get("ConstraintFiltered"), dict) else {}
        for reason, count in filtered.items():
            reason_text = str(reason)
            constraints.append(f"{reason_text} ({count} node(s))")
            match = re.search(r"\$\{meta\.luma_node_name\}\s*=\s*(\S+)", reason_text)
            if match:
                requested_node = match.group(1)
    evaluation_id = str(evaluation.get("ID") or "").strip()
    if requested_node:
        message = (
            f"application {job_id} cannot be scheduled: requested node {requested_node} "
            "is unavailable, down, or scheduling-ineligible"
        )
    elif constraints:
        message = f"application {job_id} cannot be scheduled: all candidate nodes were filtered"
    else:
        message = f"application {job_id} cannot be scheduled: Nomad evaluation is blocked"
    if constraints:
        message += f"; constraints: {', '.join(sorted(set(constraints)))}"
    if evaluation_id:
        message += f"; evaluation {evaluation_id}"
    return message


def _recover_traefik_ingress() -> str:
    state = load_state()
    config = load_config(_control_config_path())
    api = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    allocations = api.request("GET", "/v1/job/traefik/allocations")
    if not isinstance(allocations, list):
        raise LumaError("Nomad returned invalid allocations for Traefik")
    running_ids = {
        str(allocation.get("ID") or allocation.get("id") or "").strip()
        for allocation in allocations
        if _nomad_allocation_is_running(allocation)
    }
    running_ids.discard("")
    if not running_ids:
        raise LumaError("Traefik has no running allocation to recover")
    for allocation_id in sorted(running_ids):
        api.request("POST", f"/v1/allocation/{urllib.parse.quote(allocation_id, safe='')}/stop", None)
    replacements = _wait_for_nomad_job_replacement(api, "traefik", running_ids, min_running=1)
    return f"Traefik allocation recreated ({', '.join(replacements)})"


def _recover_public_route_allocation(token: str, stack: str) -> str:
    result = handle_application_restart(token, {"stack": stack, "mode": "recreate"})
    restarted = result.get("restarted") if isinstance(result, dict) else []
    allocation_ids = {
        str(item.get("allocId") or "").strip()
        for item in restarted
        if isinstance(item, dict) and str(item.get("allocId") or "").strip()
    } if isinstance(restarted, list) else set()
    if not allocation_ids:
        raise LumaError(f"application allocation recreate returned no allocation ids: {stack}")
    state = load_state()
    config = load_config(_control_config_path())
    api = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    replacements = _wait_for_nomad_job_replacement(api, stack, allocation_ids, min_running=1)
    return f"allocation recreate completed ({len(allocation_ids)} replaced by {len(replacements)} running allocation(s))"


def _read_probe_error_sample(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read(1024)
    except Exception:
        return b""


def _fetch_probe_body_sample(url: str, *, fallback_headers: Any | None = None) -> tuple[Any | None, bytes]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "luma-control-route-probe", "Range": "bytes=0-512"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.headers, resp.read(1024)
    except urllib.error.HTTPError as exc:
        body_sample = _read_probe_error_sample(exc)
        return exc.headers or fallback_headers, body_sample
    except Exception:
        return fallback_headers, b""


def _probe_status_message(url: str, status: int, *, headers: Any | None = None, body_sample: bytes = b"") -> str:
    if status in RECOVERABLE_ROUTE_HTTP_STATUSES:
        raise LumaError(f"Public route unhealthy: {url} -> HTTP {status}")
    if status == 404:
        if _is_traefik_route_miss(headers, body_sample):
            raise LumaError(f"Public route unhealthy: {url} -> HTTP 404 (Traefik router not found)")
        return f"Public route reachable: {url} -> HTTP 404 (the app may not serve /)"
    return f"Public route reachable: {url} -> HTTP {status}"


def _is_traefik_route_miss(headers: Any | None, body_sample: bytes) -> bool:
    body = body_sample.decode("utf-8", errors="ignore").strip().lower()
    if body != "404 page not found":
        return False
    server = ""
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            server = str(getter("Server") or getter("server") or "")
    return "traefik" in server.lower()


def _deploy_step(steps: list[dict[str, str]], name: str, action: Any, *, progress: Callable[[dict[str, str]], None] | None = None) -> Any:
    _emit_progress(progress, {"name": name, "status": "start", "message": "started"})
    try:
        result = action()
    except LumaError as exc:
        step = {"name": name, "status": "fail", "message": str(exc)}
        steps.append(step)
        _emit_progress(progress, step)
        raise LumaError(f"{name} failed: {exc}") from exc
    except Exception as exc:
        # A non-LumaError (socket timeout, ConnectionError from sync_dns/Nomad,
        # etc.) previously bypassed step recording entirely, so the step list and
        # NDJSON stream lost which step failed. Record the failing step and emit
        # the event, but re-raise the ORIGINAL exception unchanged: the deploy
        # wrapper keys off the exception type to drive the record to
        # "failed_partial" (vs a LumaError's terminal handling), so wrapping it
        # here would strand the deploy record.
        step = {"name": name, "status": "fail", "message": f"{type(exc).__name__}: {exc}"}
        steps.append(step)
        _emit_progress(progress, step)
        raise
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
            "agentVersion": str(agent.get("version") or ""),
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
        "directEgressNodes": [str(value) for value in config.get("directEgressNodes") or []],
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
        deployment_status = str(service.get("deploymentStatus") or "")
        if deployment_status in {"failed", "failed_partial"}:
            last_error = str(service.get("lastError") or "").strip()
            message = last_error or f"deployment status is {deployment_status}"
            issues.append({"severity": "critical", "kind": "deployment", "target": full_name, "message": f"Service {full_name}: {message}"})
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
                "agentVersion": str(registered.get("agentVersion") or ""),
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
    configured_node = str(config.get("node") or "")
    if not nodes and configured_node:
        # A blocked job has no allocation and therefore no runtime NodeName,
        # but the saved manifest is still the authoritative placement intent.
        # Keep it visible instead of rendering a misleading "-" in the UI.
        nodes = [configured_node]
    route_id = str(route.get("id") or config.get("routeId") or "")
    item: Dict[str, Any] = {
        "name": name,
        "stack": str(config.get("stack") or name),
        "fullName": str(config.get("fullName") or name),
        "status": str(svc.get("status") or ""),
        "deploymentStatus": str(config.get("deploymentStatus") or ""),
        "lastError": str(config.get("lastError") or ""),
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
        "node": configured_node,
        "nodes": nodes,
        "tasks": svc.get("tasks") if isinstance(svc.get("tasks"), list) else [],
        "storage": svc.get("storage") if isinstance(svc.get("storage"), list) else [],
        "resources": svc.get("resources") if isinstance(svc.get("resources"), dict) else {},
    }
    item["diagnostics"] = _service_diagnostics(
        int(item["desired"] or 0),
        {"running": int(item["running"] or 0), "failed": int(item["failed"] or 0), "pending": int(item["pending"] or 0)},
        {},
    ) + _deployment_diagnostics(config)
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
        "deploymentStatus": str(config.get("deploymentStatus") or ""),
        "lastError": str(config.get("lastError") or ""),
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
    ) + _deployment_diagnostics(config)
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
            "deploymentStatus": str(record.get("status") or ""),
            "lastError": str(record.get("lastError") or ""),
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
                "deploymentStatus": str(record.get("status") or ""),
                "lastError": str(record.get("lastError") or ""),
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


def _deployment_diagnostics(config: Dict[str, Any]) -> list[str]:
    status = str(config.get("deploymentStatus") or "")
    if status not in {"failed", "failed_partial"}:
        return []
    last_error = str(config.get("lastError") or "").strip()
    return [last_error or f"Deployment status is {status}"]


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


def _runtime_job_and_allocations(
    client: NomadApi,
    job_id: str,
    task_filter: str,
    service: str,
) -> tuple[Dict[str, Any], list[Any], str, str]:
    candidates: list[tuple[str, str]] = []
    if "_" in service:
        stack, task = service.split("_", 1)
        candidates.append((stack, task))
    if "/" in service:
        stack, task = service.split("/", 1)
        candidates.append((stack, task))
    if job_id:
        candidates.append((job_id, task_filter))
    seen: set[tuple[str, str]] = set()
    last_error = ""
    for candidate_job, candidate_task in candidates:
        key = (candidate_job, candidate_task)
        if not candidate_job or key in seen:
            continue
        seen.add(key)
        try:
            job = client.request("GET", f"/v1/job/{urllib.parse.quote(candidate_job, safe='')}")
            allocations = client.request("GET", f"/v1/job/{urllib.parse.quote(candidate_job, safe='')}/allocations")
        except LumaError as exc:
            last_error = str(exc)
            continue
        if not isinstance(job, dict):
            last_error = f"Nomad returned invalid job detail for {candidate_job}"
            continue
        if not isinstance(allocations, list):
            last_error = f"Nomad returned invalid allocations for {candidate_job}"
            continue
        return job, allocations, candidate_job, candidate_task
    raise LumaError(f"runtime events unavailable for {service}: {last_error or 'job not found'}")


def _nomad_allocation_detail(client: NomadApi, alloc_id: str) -> Dict[str, Any] | None:
    if not alloc_id:
        return None
    try:
        detail = client.request("GET", f"/v1/allocation/{urllib.parse.quote(alloc_id, safe='')}")
    except LumaError:
        return None
    return detail if isinstance(detail, dict) else None


def _allocation_task_state(allocation: Dict[str, Any], task_name: str) -> Dict[str, Any]:
    task_states = allocation.get("TaskStates") if isinstance(allocation.get("TaskStates"), dict) else {}
    if task_name and isinstance(task_states.get(task_name), dict):
        return task_states[task_name]
    for value in task_states.values():
        if isinstance(value, dict):
            return value
    return {}


def _runtime_task_events(task_state: Dict[str, Any], allocation: Dict[str, Any], task_name: str) -> list[Dict[str, Any]]:
    events: list[Dict[str, Any]] = []
    raw_events = task_state.get("Events") if isinstance(task_state.get("Events"), list) else []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        display = str(raw.get("DisplayMessage") or raw.get("Message") or raw.get("Type") or "").strip()
        message = str(raw.get("Message") or "").strip()
        if display and message and message not in display:
            text = f"{display}: {message}"
        else:
            text = display or message
        if not text:
            continue
        events.append(
            {
                "source": "nomad",
                "type": str(raw.get("Type") or ""),
                "task": task_name,
                "message": text,
                "time": int(raw.get("Time") or 0),
                "failed": bool(raw.get("FailsTask") or raw.get("Failed")),
            }
        )
    alloc_message = str(allocation.get("StatusDescription") or "").strip()
    if alloc_message:
        events.append({"source": "nomad", "type": "allocation", "task": task_name, "message": alloc_message, "time": 0, "failed": False})
    task_message = str(task_state.get("Message") or "").strip()
    if task_message:
        events.append({"source": "nomad", "type": "task", "task": task_name, "message": task_message, "time": 0, "failed": bool(task_state.get("Failed"))})
    events.sort(key=lambda item: int(item.get("time") or 0))
    return events


def _node_recent_pull_events(
    state: Dict[str, Any],
    node_name: str,
    *,
    alloc_id: str,
    task_name: str,
    image: str,
) -> list[Dict[str, Any]]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    record = nodes.get(node_name) if node_name else None
    if not isinstance(record, dict):
        return []
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    diagnostics = agent.get("diagnostics") if isinstance(agent.get("diagnostics"), dict) else {}
    lines = diagnostics.get("recentImagePullErrors") if isinstance(diagnostics.get("recentImagePullErrors"), list) else []
    result: list[Dict[str, Any]] = []
    for raw in lines:
        line = str(raw or "").strip()
        if not line:
            continue
        if alloc_id and alloc_id not in line:
            continue
        if task_name and f"task={task_name}" not in line and image and image not in line:
            continue
        result.append({"source": "node-agent", "type": "docker-pull", "task": task_name, "message": line, "time": 0, "failed": "failed=true" in line})
    return result


def _dedupe_runtime_events(events: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        message = str(event.get("message") or "").strip()
        if not message:
            continue
        key = (str(event.get("source") or ""), str(event.get("type") or ""), message)
        if key in seen:
            continue
        seen.add(key)
        item = dict(event)
        item["message"] = message
        result.append(item)
    return result


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
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
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


def _nomad_log_lines(
    config: Any,
    state: Dict[str, Any],
    service: str,
    *,
    tail: int = 120,
    bound_job_id: str = "",
    bound_task_name: str = "",
) -> list[str]:
    client = NomadApi(nomad_addr(config, state), token=str(state.get("nomadToken") or ""))
    # Dedicated runtime observability passes the already authenticated,
    # manifest-derived target explicitly. Dashboard callers retain the legacy
    # state lookup, but they can never redirect an LAE request to another job.
    if bound_job_id:
        job_id, task_filter = str(bound_job_id), str(bound_task_name)
    else:
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
    progress: Callable[[dict[str, str]], None] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    if state is not None:
        return _resolve_service_image_for_deployment(
            config,
            service,
            state,
            registry_auth=registry_auth,
            progress=progress,
        )

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
    progress: Callable[[dict[str, str]], None] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    build = _build_config(state)
    if not str(build.get("registryHost") or "").strip() and not str(
        build.get("pushHost") or ""
    ).strip():
        # Existing clusters may upgrade Control before adopting a Builder. Keep
        # their historical pull path available, but any cluster with Builder
        # registry settings takes the centralized cache path exclusively.
        return _resolve_service_image_without_builder(
            config, service, state, registry_auth=registry_auth
        )
    images = [service.image, *_fallback_images(config, service.image)]
    errors: list[str] = []
    platform = str(service.node_platform or "").strip()
    for image in images:
        image_registry_auth = registry_auth if registry_auth_matches_image(registry_auth, image) else None
        try:
            cached = _cache_runtime_image_on_builder(
                config,
                state,
                image,
                platform=platform,
                progress=progress,
            )
            deploy_image = str(cached["deployed"])
            image_result = {
                "requested": service.image,
                "selected": image,
                "deployed": deploy_image,
                "fallback": image != service.image,
                "registryAuth": bool(image_registry_auth),
                "forcePull": False,
                "platform": platform,
                "node": service.node or "",
                "builderNode": cached["builderNode"],
                "cached": cached["cached"],
                "cacheImage": cached["cacheImage"],
                "resolvedBy": "builder-cache",
            }
            return replace(service, image=deploy_image), image_result
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError("Builder could not cache the service image; tried " + "; ".join(errors))


def _resolve_service_image_without_builder(
    config: Any,
    service: ServiceSpec,
    state: Dict[str, Any],
    *,
    registry_auth: Dict[str, str] | None = None,
) -> tuple[ServiceSpec, Dict[str, Any]]:
    if not service.node:
        resolved = _resolve_mutable_service_image_from_registry(
            service, registry_auth=registry_auth
        )
        if resolved:
            return resolved
        return _deferred_service_image(
            config,
            service,
            registry_auth=registry_auth,
            reason="Nomad will pull on the scheduled node",
        )
    if not _node_agent_has_capability(state, service.node, "docker-image"):
        resolved = _resolve_mutable_service_image_from_registry(
            service, registry_auth=registry_auth, node=service.node
        )
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
        image_registry_auth = (
            registry_auth
            if registry_auth_matches_image(registry_auth, image)
            else None
        )
        force_pull = image_uses_mutable_latest_tag(image)
        payload: Dict[str, Any] = {
            "image": image,
            "forcePull": force_pull,
            "platform": platform,
        }
        if image_registry_auth:
            payload["registryAuth"] = image_registry_auth
        try:
            result = _resolve_image_on_target_node(
                state, service.node, image, payload
            )
            deploy_image = str(
                result.get("deployed") or result.get("digest") or image
            )
            return replace(service, image=deploy_image), {
                "requested": service.image,
                "selected": image,
                "deployed": deploy_image,
                "fallback": image != service.image,
                "registryAuth": bool(image_registry_auth),
                "forcePull": force_pull,
                "platform": platform,
                "node": service.node,
                "resolvedBy": "target-node",
            }
        except LumaError as exc:
            errors.append(f"{image}: {exc}")
    raise LumaError(
        f"unable to pull service image on target node {service.node}; tried "
        + "; ".join(errors)
    )


def _runtime_image_cache_repository(image: str) -> str:
    host = normalize_registry_host(registry_host_from_image(image))
    repository = _image_repository(image)
    parts = repository.split("/")
    if parts and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    if host in {"docker.io", "registry-1.docker.io"} and len(parts) == 1:
        parts.insert(0, "library")
    safe_host = re.sub(r"[^a-z0-9._-]+", "-", host.lower()).strip("-.")
    safe_parts = [
        re.sub(r"[^a-z0-9._-]+", "-", part.lower()).strip("-.")
        for part in parts
    ]
    safe_parts = [part for part in safe_parts if part]
    if not safe_host or not safe_parts:
        raise LumaError(f"cannot derive Builder cache repository for image: {image}")
    return "/".join(["luma-cache", safe_host, *safe_parts])


def _rewrite_internal_push_ref(image: str, push_host: str, pull_host: str) -> str:
    source_host = normalize_registry_host(registry_host_from_image(image))
    if source_host != push_host or push_host == pull_host:
        return image
    prefix, separator, remainder = image.partition("/")
    if not separator or normalize_registry_host(prefix) != push_host:
        return image
    return f"{pull_host}/{remainder}"


def _cache_runtime_image_on_builder(
    config: Any,
    state: Dict[str, Any],
    image: str,
    *,
    platform: str = "",
    progress: Callable[[dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    build = _build_config(state)
    pull_host_raw = str(build.get("registryHost") or "").strip()
    push_host_raw = str(build.get("pushHost") or "").strip()
    if not pull_host_raw or not push_host_raw:
        raise LumaError(
            "Builder registry is not configured; set both build.registryHost and build.pushHost"
        )
    pull_host = normalize_registry_host(pull_host_raw)
    push_host = normalize_registry_host(push_host_raw)
    build_node = _require_build_node(
        state,
        str(build.get("defaultNode") or DEFAULT_BUILD_NODE_NAME),
        purpose="runtime image cache",
    )
    source_host = normalize_registry_host(registry_host_from_image(image))
    if source_host in {pull_host, push_host}:
        internal_image = _rewrite_internal_push_ref(image, push_host, pull_host)
        return {
            "deployed": internal_image,
            "cacheImage": internal_image,
            "builderNode": build_node,
            "cached": False,
        }

    selected_platform = str(platform or "").strip()
    if selected_platform and selected_platform not in {"linux/amd64", "linux/arm64"}:
        raise LumaError(f"unsupported runtime image platform: {selected_platform}")
    repository = _runtime_image_cache_repository(image)
    cache_key = hashlib.sha256(
        f"{image}\n{selected_platform or 'multi-platform'}".encode("utf-8")
    ).hexdigest()[:20]
    push_image = f"{push_host}/{repository}:{cache_key}"
    destination_image = f"{pull_host}/{repository}:{cache_key}"
    insecure_raw = str(
        os.environ.get("LUMA_LAE_BUILDER_REGISTRY_INSECURE") or ""
    ).strip()
    if insecure_raw not in {"0", "1"}:
        raise LumaError(
            "LUMA_LAE_BUILDER_REGISTRY_INSECURE must be explicitly set to 0 or 1"
        )
    result = _run_node_agent_task(
        state,
        build_node,
        "cache-runtime-image",
        {
            "sourceImage": image,
            "pushImage": push_image,
            "destinationImage": destination_image,
            "platform": selected_platform,
            "proxy": _egress_proxy_for_node(config, state, build_node),
            "insecure": insecure_raw == "1",
            "timeout": 1800,
        },
        timeout=1860,
        required_capability="runtime-image-cache-v1",
        progress=progress,
    )
    digest = str(result.get("digest") or "").strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise LumaError("Builder image cache returned no verified digest")
    return {
        "deployed": f"{_image_repository(destination_image)}@{digest}",
        "cacheImage": destination_image,
        "builderNode": build_node,
        "cached": True,
    }


def _cache_compose_images_on_builder(
    config: Any,
    state: Dict[str, Any],
    deployment: ComposeDeploymentSpec,
    *,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> tuple[ComposeDeploymentSpec, Dict[str, Any]]:
    build = _build_config(state)
    if not str(build.get("registryHost") or "").strip() and not str(
        build.get("pushHost") or ""
    ).strip():
        return deployment, {
            "message": "Builder Registry is not configured; Compose images keep their existing pull references",
            "images": {},
            "legacy": True,
        }
    compose = copy.deepcopy(deployment.compose)
    services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    cached_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    results: Dict[str, Dict[str, Any]] = {}
    for service_name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            continue
        image = str(raw_service.get("image") or "").strip()
        if not image:
            continue
        override = deployment.services.get(str(service_name))
        platform = ""
        if override and override.node:
            nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
            record = _node_record_for_name(nodes, override.node)
            if isinstance(record, dict):
                platform = _nomad_node_platform_from_record(record)
        key = (image, platform)
        cached = cached_by_key.get(key)
        if cached is None:
            candidates = [image, *_fallback_images(config, image)]
            errors: list[str] = []
            for candidate in candidates:
                try:
                    cached = _cache_runtime_image_on_builder(
                        config,
                        state,
                        candidate,
                        platform=platform,
                        progress=progress,
                    )
                    cached = {
                        **cached,
                        "requested": image,
                        "selected": candidate,
                        "fallback": candidate != image,
                    }
                    break
                except LumaError as exc:
                    errors.append(f"{candidate}: {exc}")
            if cached is None:
                raise LumaError(
                    f"Builder could not cache Compose image for {service_name}; tried "
                    + "; ".join(errors)
                )
            cached_by_key[key] = cached
        raw_service["image"] = str(cached["deployed"])
        results[str(service_name)] = dict(cached)
    cached_count = sum(1 for item in results.values() if item.get("cached"))
    return replace(deployment, compose=compose), {
        "message": f"{len(results)} Compose image(s) ready in Builder Registry ({cached_count} cached)",
        "images": results,
    }


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
    markers = (
        "failed to do request",
        "eof",
        "timeout",
        "connection reset",
        "connection refused",
        "network is unreachable",
        "no route to host",
        "temporary failure in name resolution",
        "cannot reach the registry",
    )
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
    _guard_manager_docker_daemon_change(state, node_name)
    result = _run_node_agent_task(
        state,
        node_name,
        "configure-docker-egress-proxy",
        {"proxy": proxy, "noProxy": EGRESS_NO_PROXY},
        timeout=180,
        required_capability="docker-egress-proxy",
    )
    affected_allocation_ids = _docker_restart_allocation_ids(result)
    if affected_allocation_ids:
        _reconcile_allocations_after_docker_restart(state, node_name, affected_allocation_ids)


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


# Any path whose last segment has a file extension is treated as a static asset:
# if it is not in the allowlist it is a genuine miss (404), never the HTML index.
# Extensionless paths are SPA client routes and fall back to index.html.
def _dashboard_path_looks_like_asset(path: str) -> bool:
    last_segment = path.rsplit("/", 1)[-1]
    return "." in last_segment


def _dashboard_asset(path: str) -> tuple[bytes, str]:
    entry = DASHBOARD_ASSETS.get(path)
    if entry is not None:
        relative_path, content_type = entry
        return asset_path(relative_path).read_bytes(), content_type
    # A request that looks like a static asset (has a file extension) but is not in the
    # allowlist is a genuine miss -> let the caller 404 (do not return HTML for it).
    if _dashboard_path_looks_like_asset(path):
        raise LumaError("dashboard asset not found")
    # Otherwise this is a client-side route (e.g. /dashboard/apps/foo) served by the SPA
    # router -> fall back to index.html. index.html references assets with absolute
    # /dashboard/* URLs, so deep routes still load app.js/styles.css correctly.
    relative_path, content_type = DASHBOARD_ASSETS["/dashboard/"]
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
                    "capabilities": [
                        "node-region",
                        "service-proxy",
                        "dashboard",
                        "service-remove",
                        "node-agent-storage",
                        "terminal",
                        "builder-task-api-v1",
                        "builder-artifact-download-v1",
                        "repository-compose-sidecar-v1",
                        "build-proxy-mode-v1",
                        "lae-runtime-api-v1",
                        "lae-runtime-lifecycle-v1",
                        "lae-runtime-observability-v1",
                        "nomad-variables-secrets-v1",
                        "system-update-v1",
                        "control-image-preparation-v1",
                        "route-sentinel-v1",
                    ],
                },
            )
            return
        try:
            token = bearer_token(self.headers)
            lae_runtime_observability_match = re.fullmatch(
                r"/v1/lae/runtime/deployments/([^/]+)/services/([^/]+)/(logs|metrics)",
                parsed_path,
            )
            if lae_runtime_observability_match:
                kind = str(lae_runtime_observability_match.group(3))
                limit = _parse_lae_runtime_observability_query(
                    urllib.parse.urlparse(self.path).query,
                    kind=kind,
                )
                common = (
                    token,
                    str(
                        self.headers.get("X-Luma-Principal-Audience") or ""
                    ),
                    RuntimeBinding.from_headers(self.headers),
                    urllib.parse.unquote(
                        lae_runtime_observability_match.group(1)
                    ),
                    urllib.parse.unquote(
                        lae_runtime_observability_match.group(2)
                    ),
                )
                result = (
                    handle_lae_runtime_logs(*common, tail=limit)
                    if kind == "logs"
                    else handle_lae_runtime_metrics(*common, window=limit)
                )
                self._json(200, result)
                return
            lae_runtime_deployment_match = re.fullmatch(
                r"/v1/lae/runtime/deployments/([^/]+)", parsed_path
            )
            if lae_runtime_deployment_match:
                self._json(
                    200,
                    handle_lae_runtime_deployment_get(
                        token,
                        str(
                            self.headers.get("X-Luma-Principal-Audience") or ""
                        ),
                        RuntimeBinding.from_headers(self.headers),
                        urllib.parse.unquote(
                            lae_runtime_deployment_match.group(1)
                        ),
                    ),
                )
                return
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
            builder_artifact_match = re.fullmatch(
                r"/v1/builder/artifact-downloads/([^/]+)", parsed_path
            )
            if builder_artifact_match:
                lease_id = urllib.parse.unquote(builder_artifact_match.group(1))
                record = handle_builder_artifact_download(token, lease_id)
                assert record.temporary_path is not None
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", record.binding.media_type)
                    self.send_header("Content-Length", str(record.binding.size_bytes))
                    self.send_header("X-Luma-Artifact-Digest", record.binding.digest)
                    self.send_header("X-Luma-Artifact-Lease-Id", record.lease_id)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with record.temporary_path.open("rb") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                finally:
                    ARTIFACT_DOWNLOADS.complete(record.lease_id)
                return
            builder_events_match = re.fullmatch(r"/v1/builder/tasks/([^/]+)/events", parsed_path)
            if builder_events_match:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    after = int(str((query.get("after") or ["0"])[0]) or "0")
                    limit = int(str((query.get("limit") or ["200"])[0]) or "200")
                except ValueError as exc:
                    raise LumaError("after and limit must be integers") from exc
                self._json(
                    200,
                    handle_builder_task_events(
                        token,
                        urllib.parse.unquote(builder_events_match.group(1)),
                        after=after,
                        limit=limit,
                    ),
                )
                return
            builder_task_match = re.fullmatch(r"/v1/builder/tasks/([^/]+)", parsed_path)
            if builder_task_match:
                self._json(200, handle_builder_task_get(token, urllib.parse.unquote(builder_task_match.group(1))))
                return
            if parsed_path == "/v1/deployments/history":
                self._json(200, handle_deployment_history(token))
                return
            deploy_event_match = re.fullmatch(r"/v1/deployments/history/([^/]+)", parsed_path)
            if deploy_event_match:
                self._json(200, handle_deployment_history_get(token, urllib.parse.unquote(deploy_event_match.group(1))))
                return
            build_match = re.fullmatch(r"/v1/builds/([^/]+)", parsed_path)
            if build_match:
                self._json(200, handle_build_run_get(token, urllib.parse.unquote(build_match.group(1))))
                return
            lae_admin_match = re.fullmatch(
                r"/v1/dashboard/lae/([^/]+)", parsed_path
            )
            if lae_admin_match:
                self._json(
                    200,
                    handle_dashboard_lae_admin(
                        token,
                        urllib.parse.unquote(lae_admin_match.group(1)),
                        urllib.parse.urlparse(self.path).query,
                    ),
                )
                return
            fleet_update_match = re.fullmatch(r"/v1/dashboard/updates/fleet/([^/]+)", parsed_path)
            if fleet_update_match:
                self._json(200, handle_fleet_update_operation_get(token, urllib.parse.unquote(fleet_update_match.group(1))))
                return
            control_image_match = re.fullmatch(r"/v1/dashboard/updates/control-image/([^/]+)", parsed_path)
            if control_image_match:
                self._json(200, handle_control_image_prepare_get(token, urllib.parse.unquote(control_image_match.group(1))))
                return
            if parsed_path == "/v1/dashboard/updates/control-image":
                self._json(200, handle_control_image_prepare_get(token))
                return
            if parsed_path == "/v1/dashboard/updates/fleet":
                self._json(200, handle_fleet_update_operation_get(token))
                return
            if parsed_path == "/v1/dashboard/updates/manager":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                update_id = str((query.get("updateId") or [""])[0])
                self._json(200, handle_manager_update_status(token, update_id))
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
            if parsed_path == "/v1/dashboard/runtime-events":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                service = str((query.get("service") or [""])[0])
                self._json(200, handle_dashboard_runtime_events(token, service))
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
        except LaeAdminProxyError as exc:
            status = _lae_admin_proxy_http_status(exc)
            self._error(
                status,
                exc,
                code=(
                    "lae_admin_unavailable"
                    if status == 503
                    else "luma_error"
                ),
            )
            return
        except LumaRuntimeError as exc:
            self._error(exc.status, exc, code=exc.code)
            return
        except LumaError as exc:
            code = _builder_task_http_status(exc) if parsed_path.startswith("/v1/builder/tasks") else (
                401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            )
            self._error(code, exc)
            return
        self._json(404, _error_payload("not_found", "not found", request_id=_request_id()))

    def do_POST(self) -> None:
        try:
            parsed_path = urllib.parse.urlparse(self.path).path
            artifact_upload_match = re.fullmatch(
                r"/v1/node-agent/artifact-downloads/([^/]+)/content",
                parsed_path,
            )
            if artifact_upload_match:
                lease_id = urllib.parse.unquote(artifact_upload_match.group(1))
                try:
                    content_length = int(self.headers.get("Content-Length") or "")
                except ValueError as exc:
                    raise LumaError("artifact upload content length is invalid") from exc
                if not 1 <= content_length <= MAX_ARTIFACT_BYTES:
                    raise LumaError("artifact upload content length is invalid")

                def chunks() -> Any:
                    remaining = content_length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

                self._json(
                    200,
                    handle_node_agent_artifact_upload(
                        bearer_token(self.headers),
                        lease_id,
                        node_name=str(self.headers.get("X-Luma-Node-Name") or ""),
                        node_id=str(self.headers.get("X-Luma-Node-Id") or ""),
                        media_type=str(self.headers.get("Content-Type") or ""),
                        digest=str(self.headers.get("X-Luma-Artifact-Digest") or ""),
                        content_length=content_length,
                        chunks=chunks(),
                    ),
                )
                return
            body = self._read_json(
                max_bytes=(
                    MAX_RUNTIME_REQUEST_BYTES
                    if parsed_path.startswith("/v1/lae/runtime/")
                    else None
                )
            )
            token = bearer_token(self.headers)
            if parsed_path == "/v1/lae/runtime/volumes:prepare":
                self._json(
                    200,
                    handle_lae_runtime_volume_prepare(
                        token,
                        str(
                            self.headers.get("X-Luma-Principal-Audience") or ""
                        ),
                        RuntimeBinding.from_headers(self.headers),
                        body,
                        idempotency_key=str(
                            self.headers.get("Idempotency-Key") or ""
                        ),
                    ),
                )
                return
            if parsed_path == "/v1/lae/runtime/secrets:issue":
                self._json(
                    201,
                    handle_lae_runtime_secret_issue(
                        token,
                        str(
                            self.headers.get("X-Luma-Principal-Audience") or ""
                        ),
                        RuntimeBinding.from_headers(self.headers),
                        body,
                        idempotency_key=str(
                            self.headers.get("Idempotency-Key") or ""
                        ),
                    ),
                )
                return
            if parsed_path == "/v1/lae/runtime/deployments":
                self._json(
                    202,
                    handle_lae_runtime_deployment_create(
                        token,
                        str(
                            self.headers.get("X-Luma-Principal-Audience") or ""
                        ),
                        RuntimeBinding.from_headers(self.headers),
                        body,
                        idempotency_key=str(
                            self.headers.get("Idempotency-Key") or ""
                        ),
                    ),
                )
                return
            lae_runtime_cancel_match = re.fullmatch(
                r"/v1/lae/runtime/deployments/([^/]+)/cancel", parsed_path
            )
            if lae_runtime_cancel_match:
                self._json(
                    200,
                    handle_lae_runtime_deployment_cancel(
                        token,
                        str(
                            self.headers.get("X-Luma-Principal-Audience") or ""
                        ),
                        RuntimeBinding.from_headers(self.headers),
                        urllib.parse.unquote(
                            lae_runtime_cancel_match.group(1)
                        ),
                        body,
                    ),
                )
                return
            lae_runtime_lifecycle_match = re.fullmatch(
                r"/v1/lae/runtime/deployments/([^/]+)/(suspend|resume|restart|rollback|delete)",
                parsed_path,
            )
            if lae_runtime_lifecycle_match:
                self._json(
                    200,
                    handle_lae_runtime_deployment_lifecycle(
                        token,
                        str(
                            self.headers.get("X-Luma-Principal-Audience") or ""
                        ),
                        RuntimeBinding.from_headers(self.headers),
                        urllib.parse.unquote(
                            lae_runtime_lifecycle_match.group(1)
                        ),
                        str(lae_runtime_lifecycle_match.group(2)),
                        body,
                        idempotency_key=str(
                            self.headers.get("Idempotency-Key") or ""
                        ),
                    ),
                )
                return
            if parsed_path == "/v1/builder/tasks":
                self._json(
                    202,
                    handle_builder_task_create(
                        token,
                        body,
                        idempotency_key=str(self.headers.get("Idempotency-Key") or ""),
                    ),
                )
                return
            builder_artifact_lease_match = re.fullmatch(
                r"/v1/builder/tasks/([^/]+)/artifact-download-leases",
                parsed_path,
            )
            if builder_artifact_lease_match:
                self._json(
                    201,
                    handle_builder_artifact_lease_create(
                        token,
                        urllib.parse.unquote(builder_artifact_lease_match.group(1)),
                        body,
                    ),
                )
                return
            builder_cancel_match = re.fullmatch(r"/v1/builder/tasks/([^/]+)/cancel", parsed_path)
            if builder_cancel_match:
                self._json(
                    200,
                    handle_builder_task_cancel(token, urllib.parse.unquote(builder_cancel_match.group(1)), body),
                )
                return
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
                self._json(200, handle_build_run_retry(token, urllib.parse.unquote(build_retry_match.group(1)), body))
                return
            build_cancel_match = re.fullmatch(r"/v1/builds/([^/]+)/cancel", self.path)
            if build_cancel_match:
                self._json(200, handle_build_run_cancel(token, urllib.parse.unquote(build_cancel_match.group(1)), body))
                return
            build_retry_stream_match = re.fullmatch(r"/v1/builds/([^/]+)/retry/stream", self.path)
            if build_retry_stream_match:
                self._stream_build_run_retry(token, urllib.parse.unquote(build_retry_stream_match.group(1)), body)
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
            if self.path == "/v1/applications/update/stream":
                self._stream_application_update(token, body)
                return
            if self.path == "/v1/applications/update":
                self._json(200, handle_application_update(token, body))
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
            if self.path == "/v1/dashboard/updates/fleet":
                self._json(202, handle_fleet_update_operation_start(token, body))
                return
            if self.path == "/v1/dashboard/updates/control-image":
                self._json(202, handle_control_image_prepare_start(token, body))
                return
            if self.path == "/v1/dashboard/updates/manager":
                self._json(202, handle_manager_update_start(token, body))
                return
            if self.path == "/v1/dashboard/route-sentinel":
                self._json(200, handle_route_sentinel(token, body))
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
        except LumaRuntimeError as exc:
            self._error(exc.status, exc, code=exc.code)
        except LumaError as exc:
            request_path = urllib.parse.urlparse(self.path).path
            code = _builder_task_http_status(exc) if request_path.startswith("/v1/builder/tasks") else (
                401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
            )
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

    def _stream_build_run_retry(self, token: str, build_id: str, body: Dict[str, Any] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_build_run_retry(token, build_id, body, progress=emit)
            emit({"status": "done", "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream build retry internal error: {exc}", file=sys.stderr, flush=True)
            emit({"status": "fail", "message": str(exc), **_error_payload("internal_error", str(exc), request_id=request_id, include_error=False)})

    def _stream_application_update(self, token: str, body: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: Dict[str, Any]) -> None:
            self.wfile.write(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()

        try:
            result = handle_application_update(token, body, progress=emit)
            emit({"status": "done", "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LumaError as exc:
            emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
        except Exception as exc:
            request_id = _request_id()
            print(f"requestId={request_id} stream application update internal error: {exc}", file=sys.stderr, flush=True)
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

    def _read_json(self, *, max_bytes: int | None = None) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise LumaError("request content length is invalid") from exc
        if max_bytes is not None and not 0 <= length <= max_bytes:
            raise _lae_runtime_invalid("LAE runtime request body is too large")
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if max_bytes is not None:
                raise _lae_runtime_invalid(
                    "LAE runtime request contains malformed JSON"
                ) from exc
            raise LumaError("request body contains malformed JSON") from exc
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
            "capabilities": [
                "node-region",
                "service-proxy",
                "dashboard",
                "service-remove",
                "node-agent-storage",
                "terminal",
                "builder-task-api-v1",
                "builder-artifact-download-v1",
                "repository-compose-sidecar-v1",
                "build-proxy-mode-v1",
                "lae-runtime-api-v1",
                "lae-runtime-lifecycle-v1",
                "lae-runtime-observability-v1",
                "nomad-variables-secrets-v1",
                "system-update-v1",
                "control-image-preparation-v1",
                "route-sentinel-v1",
            ],
        },
    )


async def _asgi_authenticated_get(request: Request) -> Response:
    parsed_path = request.url.path
    try:
        token = bearer_token(request.headers)
        lae_runtime_observability_match = re.fullmatch(
            r"/v1/lae/runtime/deployments/([^/]+)/services/([^/]+)/(logs|metrics)",
            parsed_path,
        )
        if lae_runtime_observability_match:
            kind = str(lae_runtime_observability_match.group(3))
            limit = _parse_lae_runtime_observability_query(
                request.url.query, kind=kind
            )
            common = (
                token,
                str(
                    request.headers.get("X-Luma-Principal-Audience") or ""
                ),
                RuntimeBinding.from_headers(request.headers),
                urllib.parse.unquote(
                    lae_runtime_observability_match.group(1)
                ),
                urllib.parse.unquote(
                    lae_runtime_observability_match.group(2)
                ),
            )
            result = await run_in_threadpool(
                functools.partial(
                    handle_lae_runtime_logs,
                    *common,
                    tail=limit,
                )
                if kind == "logs"
                else functools.partial(
                    handle_lae_runtime_metrics,
                    *common,
                    window=limit,
                )
            )
            response = _json_response(200, result)
            response.headers["Cache-Control"] = "no-store"
            return response
        lae_runtime_deployment_match = re.fullmatch(
            r"/v1/lae/runtime/deployments/([^/]+)", parsed_path
        )
        if lae_runtime_deployment_match:
            return _json_response(
                200,
                await run_in_threadpool(
                    handle_lae_runtime_deployment_get,
                    token,
                    str(
                        request.headers.get("X-Luma-Principal-Audience") or ""
                    ),
                    RuntimeBinding.from_headers(request.headers),
                    urllib.parse.unquote(
                        lae_runtime_deployment_match.group(1)
                    ),
                ),
            )
        builder_artifact_match = re.fullmatch(
            r"/v1/builder/artifact-downloads/([^/]+)", parsed_path
        )
        if builder_artifact_match:
            record = await run_in_threadpool(
                handle_builder_artifact_download,
                token,
                urllib.parse.unquote(builder_artifact_match.group(1)),
            )
            assert record.temporary_path is not None

            async def artifact_stream() -> AsyncIterator[bytes]:
                try:
                    with record.temporary_path.open("rb") as source:
                        while True:
                            chunk = await run_in_threadpool(source.read, 1024 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    ARTIFACT_DOWNLOADS.complete(record.lease_id)

            return StreamingResponse(
                artifact_stream(),
                media_type=record.binding.media_type,
                headers={
                    "Content-Length": str(record.binding.size_bytes),
                    "X-Luma-Artifact-Digest": record.binding.digest,
                    "X-Luma-Artifact-Lease-Id": record.lease_id,
                    "Cache-Control": "no-store",
                },
            )
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
        builder_events_match = re.fullmatch(r"/v1/builder/tasks/([^/]+)/events", parsed_path)
        if builder_events_match:
            try:
                after = int(str(request.query_params.get("after") or "0") or "0")
                limit = int(str(request.query_params.get("limit") or "200") or "200")
            except ValueError as exc:
                raise LumaError("after and limit must be integers") from exc
            return _json_response(
                200,
                await run_in_threadpool(
                    functools.partial(
                        handle_builder_task_events,
                        token,
                        urllib.parse.unquote(builder_events_match.group(1)),
                        after=after,
                        limit=limit,
                    )
                ),
            )
        builder_task_match = re.fullmatch(r"/v1/builder/tasks/([^/]+)", parsed_path)
        if builder_task_match:
            return _json_response(
                200,
                await run_in_threadpool(handle_builder_task_get, token, urllib.parse.unquote(builder_task_match.group(1))),
            )
        if parsed_path == "/v1/deployments/history":
            return _json_response(200, await run_in_threadpool(handle_deployment_history, token))
        deploy_event_match = re.fullmatch(r"/v1/deployments/history/([^/]+)", parsed_path)
        if deploy_event_match:
            return _json_response(200, await run_in_threadpool(handle_deployment_history_get, token, urllib.parse.unquote(deploy_event_match.group(1))))
        build_match = re.fullmatch(r"/v1/builds/([^/]+)", parsed_path)
        if build_match:
            return _json_response(200, await run_in_threadpool(handle_build_run_get, token, urllib.parse.unquote(build_match.group(1))))
        lae_admin_match = re.fullmatch(
            r"/v1/dashboard/lae/([^/]+)", parsed_path
        )
        if lae_admin_match:
            response = _json_response(
                200,
                await run_in_threadpool(
                    handle_dashboard_lae_admin,
                    token,
                    urllib.parse.unquote(lae_admin_match.group(1)),
                    request.url.query,
                ),
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        fleet_update_match = re.fullmatch(r"/v1/dashboard/updates/fleet/([^/]+)", parsed_path)
        if fleet_update_match:
            return _json_response(
                200,
                await run_in_threadpool(
                    handle_fleet_update_operation_get,
                    token,
                    urllib.parse.unquote(fleet_update_match.group(1)),
                ),
            )
        if parsed_path == "/v1/dashboard/updates/fleet":
            return _json_response(200, await run_in_threadpool(handle_fleet_update_operation_get, token))
        control_image_match = re.fullmatch(r"/v1/dashboard/updates/control-image/([^/]+)", parsed_path)
        if control_image_match:
            return _json_response(
                200,
                await run_in_threadpool(
                    handle_control_image_prepare_get,
                    token,
                    urllib.parse.unquote(control_image_match.group(1)),
                ),
            )
        if parsed_path == "/v1/dashboard/updates/control-image":
            return _json_response(200, await run_in_threadpool(handle_control_image_prepare_get, token))
        if parsed_path == "/v1/dashboard/updates/manager":
            return _json_response(
                200,
                await run_in_threadpool(
                    handle_manager_update_status,
                    token,
                    str(request.query_params.get("updateId") or ""),
                ),
            )
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
        if parsed_path == "/v1/dashboard/runtime-events":
            service = str(request.query_params.get("service") or "")
            return _json_response(200, await run_in_threadpool(handle_dashboard_runtime_events, token, service))
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
    except LaeAdminProxyError as exc:
        status = _lae_admin_proxy_http_status(exc)
        response = _asgi_error(
            status,
            exc,
            code=(
                "lae_admin_unavailable"
                if status == 503
                else "luma_error"
            ),
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    except LumaRuntimeError as exc:
        return _asgi_error(exc.status, exc, code=exc.code)
    except LumaError as exc:
        code = _builder_task_http_status(exc) if parsed_path.startswith("/v1/builder/tasks") else (
            401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
        )
        response = _asgi_error(code, exc, code=_builder_task_error_code(code) if parsed_path.startswith("/v1/builder/tasks") else "luma_error")
        if parsed_path.startswith("/v1/dashboard/lae/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    response = _json_response(404, _error_payload("not_found", "not found", request_id=_request_id()))
    if parsed_path.startswith("/v1/dashboard/lae/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def _asgi_lae_runtime_json(request: Request) -> Dict[str, Any]:
    raw_length = request.headers.get("Content-Length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise _lae_runtime_invalid(
                "LAE runtime request content length is invalid"
            ) from exc
        if not 0 <= content_length <= MAX_RUNTIME_REQUEST_BYTES:
            raise _lae_runtime_invalid("LAE runtime request body is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_RUNTIME_REQUEST_BYTES:
            raise _lae_runtime_invalid("LAE runtime request body is too large")
        chunks.append(bytes(chunk))
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _lae_runtime_invalid(
            "LAE runtime request contains malformed JSON"
        ) from exc
    if not isinstance(body, dict):
        raise _lae_runtime_invalid("LAE runtime request body must be an object")
    return body


async def _asgi_authenticated_post(request: Request) -> Response:
    try:
        path = request.url.path
        artifact_upload_match = re.fullmatch(
            r"/v1/node-agent/artifact-downloads/([^/]+)/content", path
        )
        if artifact_upload_match:
            try:
                content_length = int(request.headers.get("Content-Length") or "")
            except ValueError as exc:
                raise LumaError("artifact upload content length is invalid") from exc
            if not 1 <= content_length <= MAX_ARTIFACT_BYTES:
                raise LumaError("artifact upload content length is invalid")
            result = await handle_node_agent_artifact_upload_async(
                bearer_token(request.headers),
                urllib.parse.unquote(artifact_upload_match.group(1)),
                node_name=str(request.headers.get("X-Luma-Node-Name") or ""),
                node_id=str(request.headers.get("X-Luma-Node-Id") or ""),
                media_type=str(request.headers.get("Content-Type") or ""),
                digest=str(request.headers.get("X-Luma-Artifact-Digest") or ""),
                content_length=content_length,
                chunks=request.stream(),
            )
            return _json_response(200, result)
        if path.startswith("/v1/lae/runtime/"):
            body = await _asgi_lae_runtime_json(request)
        else:
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise LumaError("request body contains malformed JSON") from exc
        if not isinstance(body, dict):
            raise LumaError("request body must be a JSON object")
        token = bearer_token(request.headers)
        if path == "/v1/lae/runtime/volumes:prepare":
            result = await run_in_threadpool(
                functools.partial(
                    handle_lae_runtime_volume_prepare,
                    token,
                    str(
                        request.headers.get("X-Luma-Principal-Audience") or ""
                    ),
                    RuntimeBinding.from_headers(request.headers),
                    body,
                    idempotency_key=str(
                        request.headers.get("Idempotency-Key") or ""
                    ),
                )
            )
            return _json_response(200, result)
        if path == "/v1/lae/runtime/secrets:issue":
            result = await run_in_threadpool(
                functools.partial(
                    handle_lae_runtime_secret_issue,
                    token,
                    str(
                        request.headers.get("X-Luma-Principal-Audience") or ""
                    ),
                    RuntimeBinding.from_headers(request.headers),
                    body,
                    idempotency_key=str(
                        request.headers.get("Idempotency-Key") or ""
                    ),
                )
            )
            return _json_response(201, result)
        if path == "/v1/lae/runtime/deployments":
            result = await run_in_threadpool(
                functools.partial(
                    handle_lae_runtime_deployment_create,
                    token,
                    str(
                        request.headers.get("X-Luma-Principal-Audience") or ""
                    ),
                    RuntimeBinding.from_headers(request.headers),
                    body,
                    idempotency_key=str(
                        request.headers.get("Idempotency-Key") or ""
                    ),
                )
            )
            return _json_response(202, result)
        lae_runtime_cancel_match = re.fullmatch(
            r"/v1/lae/runtime/deployments/([^/]+)/cancel", path
        )
        if lae_runtime_cancel_match:
            result = await run_in_threadpool(
                handle_lae_runtime_deployment_cancel,
                token,
                str(
                    request.headers.get("X-Luma-Principal-Audience") or ""
                ),
                RuntimeBinding.from_headers(request.headers),
                urllib.parse.unquote(lae_runtime_cancel_match.group(1)),
                body,
            )
            return _json_response(200, result)
        lae_runtime_lifecycle_match = re.fullmatch(
            r"/v1/lae/runtime/deployments/([^/]+)/(suspend|resume|restart|rollback|delete)",
            path,
        )
        if lae_runtime_lifecycle_match:
            result = await run_in_threadpool(
                functools.partial(
                    handle_lae_runtime_deployment_lifecycle,
                    token,
                    str(
                        request.headers.get("X-Luma-Principal-Audience") or ""
                    ),
                    RuntimeBinding.from_headers(request.headers),
                    urllib.parse.unquote(
                        lae_runtime_lifecycle_match.group(1)
                    ),
                    str(lae_runtime_lifecycle_match.group(2)),
                    body,
                    idempotency_key=str(
                        request.headers.get("Idempotency-Key") or ""
                    ),
                )
            )
            return _json_response(200, result)
        if path == "/v1/builder/tasks":
            result = await run_in_threadpool(
                functools.partial(
                    handle_builder_task_create,
                    token,
                    body,
                    idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
                )
            )
            return _json_response(202, result)
        builder_artifact_lease_match = re.fullmatch(
            r"/v1/builder/tasks/([^/]+)/artifact-download-leases", path
        )
        if builder_artifact_lease_match:
            result = await run_in_threadpool(
                handle_builder_artifact_lease_create,
                token,
                urllib.parse.unquote(builder_artifact_lease_match.group(1)),
                body,
            )
            return _json_response(201, result)
        builder_cancel_match = re.fullmatch(r"/v1/builder/tasks/([^/]+)/cancel", path)
        if builder_cancel_match:
            return _json_response(
                200,
                await run_in_threadpool(
                    handle_builder_task_cancel,
                    token,
                    urllib.parse.unquote(builder_cancel_match.group(1)),
                    body,
                ),
            )
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
            "/v1/applications/update": handle_application_update,
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
            return _json_response(200, await run_in_threadpool(handle_build_run_retry, token, urllib.parse.unquote(build_retry_match.group(1)), body))
        build_cancel_match = re.fullmatch(r"/v1/builds/([^/]+)/cancel", path)
        if build_cancel_match:
            return _json_response(200, await run_in_threadpool(handle_build_run_cancel, token, urllib.parse.unquote(build_cancel_match.group(1)), body))
        build_retry_stream_match = re.fullmatch(r"/v1/builds/([^/]+)/retry/stream", path)
        if build_retry_stream_match:
            return _asgi_stream_build_run_retry(token, urllib.parse.unquote(build_retry_stream_match.group(1)), body)
        if path == "/v1/builds/config":
            return _json_response(200, await run_in_threadpool(handle_build_config_set, token, body))
        if path == "/v1/builds/stream":
            return _asgi_stream_build_deploy(token, body)
        if path == "/v1/applications/update/stream":
            return _asgi_stream_application_update(token, body)
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
        if path == "/v1/dashboard/updates/fleet":
            return _json_response(202, await run_in_threadpool(handle_fleet_update_operation_start, token, body))
        if path == "/v1/dashboard/updates/control-image":
            return _json_response(202, await run_in_threadpool(handle_control_image_prepare_start, token, body))
        if path == "/v1/dashboard/updates/manager":
            return _json_response(202, await run_in_threadpool(handle_manager_update_start, token, body))
        if path == "/v1/dashboard/route-sentinel":
            return _json_response(200, await run_in_threadpool(handle_route_sentinel, token, body))
        handler = routes.get(path)
        if handler:
            return _json_response(200, await run_in_threadpool(handler, token, body))
        return _json_response(404, _error_payload("not_found", "not found", request_id=_request_id()))
    except LumaRuntimeError as exc:
        return _asgi_error(exc.status, exc, code=exc.code)
    except LumaError as exc:
        code = _builder_task_http_status(exc) if request.url.path.startswith("/v1/builder/tasks") else (
            401 if str(exc) == "unauthorized" or "bearer token" in str(exc) else 400
        )
        return _asgi_error(
            code,
            exc,
            code=_builder_task_error_code(code) if request.url.path.startswith("/v1/builder/tasks") else "luma_error",
        )
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
                request_id = _request_id()
                # Log server-side too: a client-only failure event leaves no
                # trace in the manager logs to correlate a failed deploy with.
                print(f"requestId={request_id} stream deployment failed: {exc}", file=sys.stderr, flush=True)
                emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=request_id, include_error=False)})
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


def _asgi_stream_build_run_retry(token: str, build_id: str, body: Dict[str, Any] | None = None) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        def run() -> None:
            try:
                result = handle_build_run_retry(token, build_id, body, progress=emit)
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


def _asgi_stream_application_update(token: str, body: Dict[str, Any]) -> StreamingResponse:
    async def generate() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        def run() -> None:
            try:
                result = handle_application_update(token, body, progress=emit)
                emit({"status": "done", "result": result})
            except LumaError as exc:
                emit({"status": "fail", "message": str(exc), **_error_payload("luma_error", str(exc), request_id=_request_id(), include_error=False)})
            except Exception as exc:
                request_id = _request_id()
                print(f"requestId={request_id} stream application update internal error: {exc}", file=sys.stderr, flush=True)
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
    _reconcile_orphaned_build_runs_after_control_restart()
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
