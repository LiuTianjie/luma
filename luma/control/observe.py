"""Read-only luma-observe queries for the Dashboard.

VictoriaMetrics stays outside Control. Control only proxies allowlisted PromQL
to the manager-local observe stack and never evaluates alerts here.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import LumaError
from .state import load_state, require_token

QUERIES = {
    "http_rate": "sum by (router) (rate(traefik_router_requests_total[5m]))",
    "http_errors": 'sum by (router) (rate(traefik_router_requests_total{code=~"5.."}[5m]))',
    "job_failed": "luma_observe_job_failed",
    "job_running": "luma_observe_job_running",
}
MAX_SERIES = 24
REQUEST_TIMEOUT = 3.0


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _points(values: Any) -> list[list[float]]:
    points: list[list[float]] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts, raw = _num(item[0]), _num(item[1])
        if ts is None or raw is None:
            continue
        points.append([int(ts), round(raw, 6)])
    return points


def observe_base_url(state: dict[str, Any] | None = None) -> str:
    configured = os.environ.get("LUMA_OBSERVE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    current = state if isinstance(state, dict) else load_state()
    nodes = current.get("nodes") if isinstance(current.get("nodes"), dict) else {}
    for name, record in nodes.items():
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
            return f"http://{ip}:8428"
    return "http://host.docker.internal:8428"


def _query_range(base: str, expr: str, *, window: int, now: float) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "query": expr,
            "start": str(int(now - window)),
            "end": str(int(now)),
            "step": str(max(15, window // 120)),
        }
    )
    request = urllib.request.Request(
        f"{base}/api/v1/query_range?{params}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise LumaError("luma-observe is not deployed") from exc
    if payload.get("status") != "success":
        raise LumaError("luma-observe is not deployed")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("result") if isinstance(data.get("result"), list) else []
    series: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metric = row.get("metric") if isinstance(row.get("metric"), dict) else {}
        points = _points(row.get("values"))
        if not points:
            continue
        series.append({"labels": {str(k): str(v) for k, v in metric.items()}, "points": points})
        if len(series) >= MAX_SERIES:
            break
    return series


def _latest(points: list[list[float]]) -> float | None:
    if not points:
        return None
    return points[-1][1]


def _label(labels: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(labels.get(key) or "").strip()
        if value:
            return value.replace("@nomad", "")
    return ""


def handle_observe_apps(token: str, *, window: int = 3600) -> dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    requested = int(window or 3600)
    window = min(max(requested, 300), 86400)
    now = time.time()
    base = observe_base_url(state)
    try:
        http_rate = _query_range(base, QUERIES["http_rate"], window=window, now=now)
        http_errors = _query_range(base, QUERIES["http_errors"], window=window, now=now)
        job_failed = _query_range(base, QUERIES["job_failed"], window=window, now=now)
        job_running = _query_range(base, QUERIES["job_running"], window=window, now=now)
    except LumaError as exc:
        return {
            "available": False,
            "message": str(exc),
            "window": window,
            "http": [],
            "jobs": [],
            "updatedAt": int(now),
        }
    errors_by_router = {_label(item["labels"], "router"): item["points"] for item in http_errors}
    http: list[dict[str, Any]] = []
    for item in http_rate:
        router = _label(item["labels"], "router")
        if not router:
            continue
        requests = item["points"]
        errors = errors_by_router.get(router) or []
        error_latest = _latest(errors) or 0.0
        request_latest = _latest(requests) or 0.0
        http.append(
            {
                "id": router,
                "requests": requests,
                "errors": errors,
                "requestRate": request_latest,
                "errorRate": error_latest,
                "errorRatio": (error_latest / request_latest) if request_latest > 0 else 0.0,
            }
        )
    http.sort(key=lambda row: row["requestRate"], reverse=True)
    running_by_job = {
        (_label(item["labels"], "job"), _label(item["labels"], "task_group")): item["points"]
        for item in job_running
    }
    jobs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in job_failed + job_running:
        job = _label(item["labels"], "job")
        group = _label(item["labels"], "task_group")
        key = (job, group)
        if not job or key in seen:
            continue
        seen.add(key)
        failed_points = next((row["points"] for row in job_failed if _label(row["labels"], "job") == job and _label(row["labels"], "task_group") == group), [])
        running_points = running_by_job.get(key) or []
        jobs.append(
            {
                "id": f"{job}/{group}" if group else job,
                "job": job,
                "taskGroup": group,
                "failed": _latest(failed_points) or 0.0,
                "running": _latest(running_points) or 0.0,
                "failedPoints": failed_points,
                "runningPoints": running_points,
            }
        )
    jobs.sort(key=lambda row: (row["failed"], row["job"]), reverse=True)
    return {
        "available": True,
        "message": "",
        "window": window,
        "http": http[:MAX_SERIES],
        "jobs": jobs[:MAX_SERIES],
        "updatedAt": int(now),
    }
