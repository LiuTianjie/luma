"""Read-only Prometheus exposition from the last persisted agent heartbeat.

Scrapes never contact Nomad or nodes and never mutate Control state. Resource
samples expire independently of liveness, so a fresh lease cannot make an old
CPU sample appear current. This endpoint describes agent observations, not
application availability or historical SLOs.
"""
from __future__ import annotations

import math
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import LumaError
from .state import state_dir

SAMPLE_TTL_SECONDS = 120
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def require_metrics_token(state: dict[str, Any], token: str) -> None:
    """A dedicated token grants only the metrics endpoint, never management."""
    management = str(state.get("deployToken") or "")
    if management and token.isascii() and secrets.compare_digest(management, token):
        return
    configured = os.environ.get("LUMA_METRICS_TOKEN_FILE", "").strip() or str(state_dir() / "metrics-token")
    descriptor = -1
    try:
        path = Path(configured)
        if not configured or not path.is_absolute():
            raise ValueError()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or not metadata.st_mode & stat.S_IRUSR or not 32 <= metadata.st_size <= 4096:
            raise ValueError()
        with os.fdopen(descriptor, "r", encoding="ascii") as handle:
            descriptor = -1
            expected = handle.read(4097).strip()
        if not 32 <= len(expected) <= 4096 or any(c.isspace() for c in expected):
            raise ValueError()
        if not token.isascii() or not secrets.compare_digest(expected, token):
            raise ValueError()
    except (OSError, UnicodeError, ValueError):
        raise LumaError("unauthorized") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _labels(values: dict[str, Any]) -> str:
    def escape(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return "{" + ",".join(f'{key}="{escape(value)}"' for key, value in sorted(values.items())) + "}"


def render_metrics(state: dict[str, Any], *, now: float | None = None, compose_jobs: set[str] | None = None) -> str:
    current = time.time() if now is None else now
    families: dict[str, tuple[str, list[str]]] = {}

    def emit(name: str, help_text: str, value: Any, labels: dict[str, Any]) -> None:
        number = _number(value)
        if number is None:
            return
        if name not in families:
            families[name] = (help_text, [])
        families[name][1].append(f"{name}{_labels(labels)} {number:g}")

    cluster = {"cluster": str(state.get("clusterId") or "")}
    emit("luma_control_info", "Control package version; scrape success is represented by Prometheus up.", 1, {**cluster, "version": __version__})
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for node, record in sorted(nodes.items()):
        if not isinstance(record, dict):
            continue
        agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
        labels = {**cluster, "node": node, "region": str(record.get("region") or "")}
        last_seen = _number(agent.get("lastSeen"))
        fresh = bool(last_seen and 0 <= current - last_seen <= SAMPLE_TTL_SECONDS)
        emit("luma_node_agent_up", "Whether an agent heartbeat was received within 120 seconds; not Nomad eligibility.", int(fresh), labels)
        if last_seen:
            emit("luma_node_heartbeat_timestamp_seconds", "Unix time of the latest agent heartbeat.", last_seen, labels)
        metrics = agent.get("metrics") if isinstance(agent.get("metrics"), dict) else {}
        # Older agents are compatible; their Control did not record collection timestamps.
        sampled_at = _number(agent.get("metricsCollectedAt", last_seen))
        if fresh and sampled_at and 0 <= current - sampled_at <= SAMPLE_TTL_SECONDS:
            emit("luma_node_metrics_timestamp_seconds", "Unix time of the last received host resource sample.", sampled_at, labels)
            mapping = {
                "memoryTotalBytes": ("memory_total_bytes", "Host physical memory capacity in bytes.", 1),
                "memoryAvailableBytes": ("memory_available_bytes", "Host available physical memory in bytes.", 1),
                "memoryUsedPercent": ("memory_used_ratio", "Host physical memory used fraction.", 0.01),
                "diskTotalBytes": ("filesystem_size_bytes", "Capacity of the sampled host filesystem in bytes.", 1),
                "diskAvailableBytes": ("filesystem_available_bytes", "Available non-reserved bytes on the sampled host filesystem.", 1),
                "diskUsedPercent": ("filesystem_used_ratio", "Used fraction of usable filesystem capacity, excluding reserved blocks.", 0.01),
                "inodesUsedPercent": ("filesystem_inodes_used_ratio", "Used fraction of inode capacity, omitted when unsupported.", 0.01),
            }
            for key, (suffix, description, scale) in mapping.items():
                value = _number(metrics.get(key))
                if value is not None:
                    dimension = {**labels, "path": str(metrics.get("metricsPath") or "unknown")} if suffix.startswith("filesystem_") else labels
                    emit("luma_node_" + suffix, description, value * scale, dimension)
            cpu = _number(metrics.get("cpuPercent"))
            if cpu is not None and agent.get("os") != "darwin":
                emit("luma_node_cpu_used_ratio", "Host CPU utilization fraction; Darwin load estimate is not exported as CPU utilization.", cpu * 0.01, labels)
            load = _number(metrics.get("load1"))
            if load is not None:
                emit("luma_node_load1", "Host one-minute load average.", load, labels)
        stats_at = _number(agent.get("containerStatsCollectedAt", last_seen))
        if not fresh or not stats_at or not 0 <= current - stats_at <= SAMPLE_TTL_SECONDS:
            continue
        stats = agent.get("containerStats") if isinstance(agent.get("containerStats"), list) else []
        totals: dict[tuple[str, str], dict[str, float]] = {}
        unresolved = 0
        for item in stats:
            if not isinstance(item, dict) or not item.get("service"):
                continue
            service = str(item["service"])
            if service.startswith("nomad:"):
                unresolved += 1
                continue
            task = str(item.get("nomadTask") or item.get("taskId") or "")
            if service in (compose_jobs or set()) and task:
                service = f"{service}_{task}"
            total = totals.setdefault((service, task), {"containers": 0})
            total["containers"] += 1
            for key in ("cpuPercent", "memoryUsageBytes"):
                number = _number(item.get(key))
                if number is not None:
                    total[key] = total.get(key, 0) + number
        emit("luma_node_unresolved_containers", "Observed containers without a persisted job identity, omitted from service resource series.", unresolved, labels)
        for (service, task), total in sorted(totals.items()):
            dimensions = {**labels, "service": service, "task": task}
            emit("luma_service_observed_containers", "Containers in the latest agent stats sample; not desired replicas.", total["containers"], dimensions)
            emit("luma_service_metrics_timestamp_seconds", "Unix time of the last received container stats sample.", stats_at, dimensions)
            if "cpuPercent" in total:
                emit("luma_service_cpu_cores", "Observed container CPU usage in cores, summed per service and node.", total["cpuPercent"] / 100, dimensions)
            if "memoryUsageBytes" in total:
                emit("luma_service_memory_bytes", "Observed container memory usage, summed per service and node.", total["memoryUsageBytes"], dimensions)

    for collection, kind in (("agentTasks", "agent"), ("builderTasks", "builder"), ("buildRuns", "build")):
        tasks = state.get(collection) if isinstance(state.get(collection), dict) else {}
        for status in ("queued", "running"):
            matching = [item for item in tasks.values() if isinstance(item, dict) and item.get("status") == status]
            labels = {**cluster, "kind": kind, "status": status}
            emit("luma_tasks", "Persisted task records currently in this state; task kinds may refer to the same workflow.", len(matching), labels)
            if status == "queued":
                starts = [value for item in matching if (value := _number(item.get("createdAt"))) is not None and 0 < value <= current]
                emit("luma_task_queue_oldest_age_seconds", "Age of the oldest queued task; zero when no dated queued tasks exist.", current - min(starts) if starts else 0, labels)
    return "\n".join(line for name, (description, samples) in sorted(families.items()) for line in [f"# HELP {name} {description}", f"# TYPE {name} gauge", *samples]) + "\n"
