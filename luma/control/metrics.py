"""Time-series retention for node and service metrics.

Kept deliberately separate from control.json: the main control state is
rewritten in full under a global lock on every node heartbeat (see
control/state.py). Appending an ever-growing history into that file would
make every heartbeat rewrite the whole history and serialize the control
plane. So history lives in its own file with its own lock, written with the
same atomic tmp+fsync+os.replace pattern as save_state.

Reads do not take the lock: os.replace is atomic, so a reader always sees a
complete old or new file, never a torn write.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import fcntl
from pathlib import Path
from typing import Any, Dict, List, Optional

from .state import state_dir


HISTORY_FILE = "metrics-history.json"
LOCK_FILE = "metrics-history.lock"
SCHEMA_VERSION = 1

# Default ~6h at a 30s sample cadence; override via env for longer windows.
DEFAULT_MAX_POINTS = 720
MIN_MAX_POINTS = 60
MAX_MAX_POINTS = 10000

# A node's per-service contribution is dropped from the cross-node sum once it
# goes stale, so a downed node stops inflating a service's total. Sits above
# the 30s cadence and the 120s agent-stale threshold.
SERVICE_SCRATCH_TTL_SECONDS = 180

NODE_SERIES = ("cpuPercent", "memoryUsedPercent")
SERVICE_SERIES = ("cpuPercent", "memoryUsageBytes")

_SCRATCH_KEY = "_serviceScratch"


def history_path() -> Path:
    return state_dir() / HISTORY_FILE


def _lock_path() -> Path:
    return state_dir() / LOCK_FILE


def max_points() -> int:
    raw = os.environ.get("LUMA_METRICS_HISTORY_POINTS")
    if not raw:
        return DEFAULT_MAX_POINTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_POINTS
    return max(MIN_MAX_POINTS, min(MAX_MAX_POINTS, value))


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _empty() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "nodes": {}, "services": {}, _SCRATCH_KEY: {}}


def _load_raw() -> Dict[str, Any]:
    path = history_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Never let a corrupt history file take down the control plane; the
        # cost of a reset is a gap in trend charts, not an outage.
        print(f"metrics history unreadable, starting fresh: {exc}", file=sys.stderr, flush=True)
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    for key in ("nodes", "services", _SCRATCH_KEY):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    data["version"] = SCHEMA_VERSION
    return data


def _save_raw(data: Dict[str, Any]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":")) + "\n"
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp_path.chmod(0o600)
        except PermissionError:
            pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _append_point(bucket: Dict[str, Any], series_key: str, ts: int, value: float, limit: int) -> None:
    series = bucket.get(series_key)
    if not isinstance(series, list):
        series = []
    rounded = round(value, 2)
    series.append([ts, rounded])
    if len(series) > limit:
        del series[: len(series) - limit]
    bucket[series_key] = series


def _node_point_values(node_metrics: Dict[str, Any]) -> Dict[str, float]:
    values: Dict[str, float] = {}
    cpu = _coerce_number(node_metrics.get("cpuPercent"))
    if cpu is None:
        # Darwin agents report loadPercent instead of cpuPercent; the UI uses
        # the same fallback (metrics.cpuPercent ?? metrics.loadPercent).
        cpu = _coerce_number(node_metrics.get("loadPercent"))
    if cpu is not None:
        values["cpuPercent"] = cpu
    mem = _coerce_number(node_metrics.get("memoryUsedPercent"))
    if mem is not None:
        values["memoryUsedPercent"] = mem
    return values


def _service_totals_from_scratch(scratch: Dict[str, Any], cutoff: int) -> Dict[str, Dict[str, float]]:
    totals: Dict[str, Dict[str, float]] = {}
    for full_name, by_node in scratch.items():
        if not isinstance(by_node, dict):
            continue
        cpu_sum = 0.0
        mem_sum = 0.0
        live = False
        for contribution in by_node.values():
            if not isinstance(contribution, dict):
                continue
            if int(contribution.get("ts") or 0) < cutoff:
                continue
            live = True
            cpu_sum += float(contribution.get("cpuPercent") or 0.0)
            mem_sum += float(contribution.get("memoryUsageBytes") or 0.0)
        if live:
            totals[full_name] = {"cpuPercent": cpu_sum, "memoryUsageBytes": mem_sum}
    return totals


def _prune_scratch(scratch: Dict[str, Any], cutoff: int) -> None:
    for full_name in list(scratch.keys()):
        by_node = scratch.get(full_name)
        if not isinstance(by_node, dict):
            del scratch[full_name]
            continue
        for node_name in list(by_node.keys()):
            contribution = by_node.get(node_name)
            if not isinstance(contribution, dict) or int(contribution.get("ts") or 0) < cutoff:
                del by_node[node_name]
        if not by_node:
            del scratch[full_name]


def record_samples(
    node_name: str,
    node_metrics: Optional[Dict[str, Any]],
    container_stats: Optional[List[Any]],
    *,
    now: Optional[int] = None,
) -> None:
    """Append one node sample and refresh service totals for this heartbeat.

    Node series are keyed by node name (clean, one writer per node). Service
    series are the cross-node SUM of each service's live container
    contributions: we stash this node's contribution in scratch, sum across
    all non-stale nodes, and append the total. That avoids a multi-node
    service's chart sawtoothing between per-node partial values.
    """
    node_name = str(node_name or "").strip()
    ts = _now(now)
    limit = max_points()
    cutoff = ts - SERVICE_SCRATCH_TTL_SECONDS

    # Group this heartbeat's containers into per-service contributions.
    this_node_services: Dict[str, Dict[str, float]] = {}
    for raw in container_stats or []:
        if not isinstance(raw, dict):
            continue
        full_name = str(raw.get("service") or "").strip()
        if not full_name:
            continue
        entry = this_node_services.setdefault(full_name, {"cpuPercent": 0.0, "memoryUsageBytes": 0.0})
        cpu = _coerce_number(raw.get("cpuPercent"))
        mem = _coerce_number(raw.get("memoryUsageBytes"))
        if cpu is not None:
            entry["cpuPercent"] += cpu
        if mem is not None:
            entry["memoryUsageBytes"] += mem

    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load_raw()
        nodes = data["nodes"]
        services = data["services"]
        scratch = data[_SCRATCH_KEY]

        if node_name:
            node_values = _node_point_values(node_metrics or {})
            if node_values:
                bucket = nodes.get(node_name)
                if not isinstance(bucket, dict):
                    bucket = {}
                for series_key, value in node_values.items():
                    _append_point(bucket, series_key, ts, value, limit)
                nodes[node_name] = bucket

            # Refresh this node's contribution for every service it reported.
            for full_name, contribution in this_node_services.items():
                by_node = scratch.get(full_name)
                if not isinstance(by_node, dict):
                    by_node = {}
                by_node[node_name] = {
                    "ts": ts,
                    "cpuPercent": round(contribution["cpuPercent"], 2),
                    "memoryUsageBytes": int(contribution["memoryUsageBytes"]),
                }
                scratch[full_name] = by_node

        _prune_scratch(scratch, cutoff)

        # Append a summed point only for services touched this heartbeat, so a
        # node reporting nothing for a service does not stamp duplicate points.
        totals = _service_totals_from_scratch(scratch, cutoff)
        for full_name in this_node_services:
            total = totals.get(full_name)
            if total is None:
                continue
            bucket = services.get(full_name)
            if not isinstance(bucket, dict):
                bucket = {}
            _append_point(bucket, "cpuPercent", ts, total["cpuPercent"], limit)
            _append_point(bucket, "memoryUsageBytes", ts, total["memoryUsageBytes"], limit)
            services[full_name] = bucket

        _save_raw(data)


def load_history(
    kind: str,
    name: str,
    *,
    window: Optional[int] = None,
    now: Optional[int] = None,
) -> Dict[str, List[List[float]]]:
    """Return {seriesKey: [[ts, value], ...]} for one node or service.

    Reads without the lock (atomic replace guarantees a complete file).
    Filters to ts >= now - window when window is given.
    """
    bucket_key = {"node": "nodes", "service": "services"}.get(str(kind))
    if not bucket_key:
        return {}
    name = str(name or "").strip()
    if not name:
        return {}
    data = _load_raw()
    bucket = data.get(bucket_key, {})
    obj = bucket.get(name)
    if not isinstance(obj, dict):
        return {}
    floor = None
    if window:
        floor = _now(now) - int(window)
    result: Dict[str, List[List[float]]] = {}
    for series_key, series in obj.items():
        if not isinstance(series, list):
            continue
        points: List[List[float]] = []
        for point in series:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue
            ts = int(point[0])
            if floor is not None and ts < floor:
                continue
            points.append([ts, point[1]])
        result[series_key] = points
    return result


def sustained_breach(
    kind: str,
    name: str,
    series_key: str,
    *,
    threshold: float,
    duration_seconds: int,
    min_fraction: float = 0.8,
    now: Optional[int] = None,
) -> Optional[float]:
    """Return the peak value if a series has stayed above ``threshold`` for the
    window, else None. Used to turn instantaneous spikes into "sustained"
    alerts. Guards against false positives: needs enough points, enough time
    coverage, a current breach, and a majority of breaching samples — so a
    single transient spike or an already-resolved problem does not alert."""
    points = load_history(kind, name, window=duration_seconds, now=now).get(series_key) or []
    if len(points) < 3:
        return None
    first_ts, last_ts = int(points[0][0]), int(points[-1][0])
    # Require the samples to actually span most of the window, so a freshly
    # joined node with one high reading is not flagged as "sustained".
    if last_ts - first_ts < duration_seconds * 0.6:
        return None
    # The problem must still be happening, not already recovered.
    if float(points[-1][1]) < threshold:
        return None
    breaching = sum(1 for _, value in points if float(value) >= threshold)
    if breaching / len(points) < min_fraction:
        return None
    return round(max(float(value) for _, value in points), 1)
