"""Bounded, read-only summaries. Missing samples are never synthesized as zero."""
from __future__ import annotations

import concurrent.futures
import json
import math
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .observe import observe_base_url
from .state import load_auth_state, require_token
from ..errors import LumaError

_SLOTS = threading.BoundedSemaphore(2)
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}
MAX_BODY = 2 * 1024 * 1024


def read_json(url: str, params: dict | None = None) -> dict:
    started = time.monotonic()
    try:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=2) as response:
            body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return {"state": "response_limit"}
        return {"state": "ok", "body": json.loads(body), "durationMs": round((time.monotonic()-started)*1000)}
    except urllib.error.HTTPError as exc:
        return {"state": "unauthorized" if exc.code in (401, 403) else "rate_limited" if exc.code == 429 else "upstream_error"}
    except TimeoutError:
        return {"state": "timeout"}
    except (ValueError, UnicodeError):
        return {"state": "invalid_response"}
    except OSError:
        return {"state": "unreachable"}


def metric(query: str, at: int) -> dict:
    result = read_json(observe_base_url() + "/api/v1/query", {"query": query, "time": at, "timeout": "2s"})
    if result["state"] != "ok":
        return result
    body = result.pop("body")
    if not isinstance(body, dict) or body.get("status") != "success":
        return {"state": "invalid_response"}
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        return {"state": "invalid_response"}
    items = []
    for row in data["result"]:
        try:
            value = float(row["value"][1])
            if math.isfinite(value):
                items.append({"labels": row.get("metric", {}), "value": value})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return {**result, "state": "ok" if items else "empty", "items": items,
            "value": items[0]["value"] if len(items) == 1 else None}


PLATFORM_QUERIES = {
    "input_metrics": "sum(rate(otelcol_receiver_accepted_metric_points_total[5m]))",
    "input_spans": "sum(rate(otelcol_receiver_accepted_spans_total[5m]))",
    "refused": "sum(increase(otelcol_receiver_refused_metric_points_total[15m])) + sum(increase(otelcol_receiver_refused_spans_total[15m]))",
    "export_failures": "sum(increase(otelcol_exporter_send_failed_metric_points_total[15m])) + sum(increase(otelcol_exporter_send_failed_spans_total[15m]))",
    "queue_ratio": "max(otelcol_exporter_queue_size / otelcol_exporter_queue_capacity)",
    "log_drops": "sum(increase(luma_observe_log_shipper_dropped_lines_total[15m]))",
    "unresolved": "sum(luma_node_unresolved_containers)",
    "query_p99": 'max(vm_request_duration_seconds{job="victoria",path="/api/v1/query",quantile="0.99"})',
    "logs_bytes": 'sum(vl_data_size_bytes{type="storage"})',
    "logs_limit": "max(vl_max_disk_space_usage_bytes)",
    "free_bytes": "min(vm_free_disk_space_bytes)",
    "tempo_poll_errors": 'sum(increase(luma_observe_tempo_poll_errors_total[15m]))',
    "coverage_age": "max(time()-luma_observe_log_coverage_timestamp_seconds)",
    "log_backlog_bytes": "sum(luma_observe_log_backlog_bytes)",
    "targets": "up",
    "freshness": "time()-max(timestamp(otelcol_receiver_accepted_spans_total))",
}


def application_queries(app: str, params: dict) -> dict[str, str]:
    from .observe_tasks import task_query
    q = task_query({**params, "app": app})
    labels = ['luma_stack=' + json.dumps(app)]
    for key, label in (("environment", "deployment_environment_name"), ("service", "service_name"), ("instance", "service_instance_id")):
        if q.get(key):
            labels.append(label + "=" + json.dumps(q[key]))
    scope = ",".join(labels)
    window = str(q["until"]-q["since"]) + "s"
    native = f'task_executions_total{{{scope},task_framework="taskiq"}}'
    spans = f'task_span_calls_total{{{scope},task_framework="celery",luma_observe_span_metrics="service"}}'
    total = f'(sum(increase({native}[{window}])) or vector(0)) + (sum(increase({spans}[{window}])) or vector(0))'
    failures = f'(sum(increase({native[:-1]},outcome="failed"}}[{window}])) or vector(0)) + (sum(increase({spans[:-1]},task_outcome="failed"}}[{window}])) or vector(0))'
    # A zero ratio exists only after observing an actual execution denominator.
    return {
        "task_error_ratio": f'({failures}) / (({total}) > 0)',
        "task_p95": f'max(histogram_quantile(0.95,sum by(le)(rate(task_duration_seconds_bucket{{{scope}}}[{window}]))))',
        "active": f'sum(task_active{{{scope}}} and on(service_name,service_instance_id) (time()-worker_heartbeat_seconds{{{scope}}} < 120))',
        "heartbeat_age": f'max(time()-worker_heartbeat_seconds{{{scope}}})',
        "freshness": f'time()-max(timestamp(worker_heartbeat_seconds{{{scope}}}))',
        "http_error_ratio": f'luma_app:http_errors:rate5m{{app={json.dumps(app)}}} / (luma_app:http_requests:rate5m{{app={json.dumps(app)}}} > 0)',
        "http_p95": f'luma_app:http_duration_seconds:p95{{app={json.dumps(app)}}}',
    }


def handle_observe_status(token: str, params: dict) -> dict:
    require_token(load_auth_state(), token, token_type="deploy")
    app = str(params.get("app") or "")
    if len(app) > 160:
        raise LumaError("invalid application")
    now = int(time.time())
    queries = application_queries(app, params) if app else PLATFORM_QUERIES
    from .observe_tasks import _number
    at = min(_number(params, "until", now, 1, 2**40), now)
    key = json.dumps([app, queries, at // 15], sort_keys=True)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now-cached[0] < 15:
            return cached[1]
    if not _SLOTS.acquire(blocking=False):
        return {"state": "rate_limited", "signals": {}, "sampledAt": now}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            signals = dict(pool.map(lambda item: (item[0], metric(item[1], at)), queries.items()))
        coverage = read_json("http://127.0.0.1:9108/coverage")
        if coverage["state"] == "ok":
            rows = coverage.pop("body")
            coverage["items"] = [r for r in rows if isinstance(r, dict) and (not app or r.get("app") == app)] if isinstance(rows, list) else []
            if not isinstance(rows, list):
                coverage["state"] = "invalid_response"
            elif not coverage["items"]:
                coverage["state"] = "empty"
        control_alerts = {"state": "empty"}
        if app:
            try:
                from .alerting import dispatch
                overview = dispatch("GET", "overview", query={"app": app})
                control_alerts = {"state": "ok", **overview}
            except (sqlite3.Error, LumaError, OSError):
                control_alerts = {"state": "unreachable"}
        result = {"state": "ok", "controlAlerts": control_alerts, "signals": signals, "coverage": coverage, "sampledAt": now,
                  "historical": at < now-120}
        with _CACHE_LOCK:
            if len(_CACHE) >= 64:
                _CACHE.clear()
            _CACHE[key] = (now, result)
        return result
    finally:
        _SLOTS.release()


def handle_external_alerts(token: str, app: str = "") -> dict:
    require_token(load_auth_state(), token, token_type="deploy")
    result = read_json("http://127.0.0.1:9093/api/v2/alerts")
    if result["state"] != "ok":
        return {**result, "items": []}
    rows = result.get("body")
    if not isinstance(rows, list):
        return {"state": "invalid_response", "items": []}
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        labels, annotations = row.get("labels", {}), row.get("annotations", {})
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            continue
        if app and labels.get("app") != app:
            continue
        items.append({"id": row.get("fingerprint", ""), "source": "Alertmanager", "app": labels.get("app", ""),
            "task": labels.get("task", ""), "name": labels.get("alertname", ""), "severity": labels.get("severity", "warning"),
            "summary": str(annotations.get("summary", ""))[:500], "description": str(annotations.get("description", ""))[:2000],
            "startsAt": row.get("startsAt"), "updatedAt": row.get("updatedAt"), "status": row.get("status", {}).get("state", "unknown")})
    # Display only: never feed these back into Control notification delivery.
    return {"state": "ok", "items": items[:200], "sampledAt": int(time.time()), "truncated": len(items) > 200}
