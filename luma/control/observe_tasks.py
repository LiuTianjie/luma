"""Application task investigations through the published Observe query SDK.

Control supplies authentication, fixed local endpoints and the application scope.
The SDK owns bounded transport, lifecycle decoding and trace normalization.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time

from starlette.concurrency import run_in_threadpool

from ..errors import LumaError
from .observe import observe_base_url
from .state import load_state, require_token

READ_SLOTS = asyncio.Semaphore(6)
VIEWS = {"overview", "events", "traces", "trace", "logs"}


def _authorize(token: str) -> None:
    require_token(load_state(), token, token_type="deploy")


def _number(params: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(params.get(key, default))
    except (ValueError, TypeError) as exc:
        raise LumaError(f"invalid {key}") from exc
    if not minimum <= value <= maximum:
        raise LumaError(f"invalid {key}")
    return value


def task_query(params: dict) -> dict:
    """Validate typed filters before building any backend query."""
    app = str(params.get("app", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", app):
        raise LumaError("invalid application")
    query = {"app": app, "hours": _number(params, "hours", 1, 1, 72),
             "offset": _number(params, "offset", 0, 0, 2000)}
    for key, maximum in (("task_name", 160), ("task_id", 100), ("queue", 160),
                         ("attribute", 100), ("value", 256), ("environment", 100), ("service", 160),
                         ("instance", 200), ("keyword", 256)):
        value = str(params.get(key, ""))
        if len(value) > maximum or any(ord(char) < 32 for char in value):
            raise LumaError(f"invalid {key}")
        query[key] = value
    if query["attribute"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", query["attribute"]):
        raise LumaError("invalid attribute")
    if bool(query["attribute"]) != bool(query["value"]):
        raise LumaError("attribute and value are required together")
    query["outcome"] = str(params.get("outcome", "all"))
    if query["outcome"] not in {"all", "problem"}:
        raise LumaError("invalid outcome")
    query["until"] = min(_number(params, "until", int(time.time()), 1, 2**40), int(time.time()))
    query["since"] = _number(params, "since", query["until"]-query["hours"]*3600, 1, 2**40)
    if not 0 < query["until"]-query["since"] <= 72*3600:
        raise LumaError("time range must be between 1 second and 72 hours")
    query["min_duration_ms"] = _number(params, "min_duration_ms", 0, 0, 259200000)
    query["trace_id"] = str(params.get("trace_id", "")).lower()
    if query["trace_id"] and not re.fullmatch(r"[0-9a-f]{32}", query["trace_id"]):
        raise LumaError("invalid trace_id")
    return query


def metric_queries(app: str, hours: int, query: dict | None = None) -> dict[str, str]:
    """Read framework signals with freshness and tracking-overflow guards."""
    scope = "luma_stack=" + json.dumps(app)
    for key, label in (("environment", "deployment_environment_name"), ("service", "service_name"), ("instance", "service_instance_id")):
        if (query or {}).get(key):
            scope += "," + label + "=" + json.dumps(query[key])
    group = "task_name,task_framework,task_queue"
    live = f"(time()-worker_heartbeat_seconds{{{scope}}} < 120)"
    join = "on(service_name,service_instance_id)"
    active = f"task_active{{{scope}}} and {join} {live}"
    tracking = (f"and on(service_name,service_instance_id,task_name,task_framework,task_queue) "
                f"(task_active{{{scope}}} > 0) and {join} {live} and {join} "
                f"(task_active_tracking_overflow{{{scope}}} == 0)")
    native = scope + ',task_framework="taskiq"'
    span = scope + ',task_framework="celery",luma_observe_span_metrics="service"'
    window = str(query["until"]-query["since"]) + "s" if query else f"{hours}h"
    return {
        "registered": f"max by({group})(task_registered{{{scope}}} and {join} {live})",
        "active": f"sum by({group})({active})",
        "oldest": f"max by({group})(task_oldest_active_age_seconds{{{scope}}} {tracking})",
        "no_progress": f"max by({group})(task_no_progress_age_seconds{{{scope}}} {tracking})",
        "executions": f"sum by({group},outcome)(increase(task_executions_total{{{native}}}[{window}]))",
        "span_executions": f"sum by({group},task_outcome)(increase(task_span_calls_total{{{span}}}[{window}]))",
        "duration": f"histogram_quantile(0.95,sum by({group},le)(rate(task_duration_seconds_bucket{{{native}}}[{window}])))",
        "span_duration": f"histogram_quantile(0.95,sum by({group},le)(rate(task_span_duration_seconds_bucket{{{span}}}[{window}])))",
        "wait": f"histogram_quantile(0.95,sum by({group},le)(rate(task_queue_wait_seconds_bucket{{{native}}}[{window}])))",
        "last_finished": f"max by(task_name,task_framework,outcome)(max_over_time(task_last_finished_seconds{{{scope}}}[72h]))",
        "processes": f'max by(__name__,service_name,service_instance_id,task_framework,task_role,task_queue)({{__name__=~"worker_heartbeat_seconds|task_process_started_seconds",{scope}}})',
        "drops": f"sum by(reason)(increase(observe_sdk_dropped_total{{{scope}}}[{window}]))",
    }


def trace_query(query: dict) -> str:
    """Construct TraceQL from escaped equality filters and a numeric duration."""
    filters = ["resource.luma.stack = " + json.dumps(query["app"])]
    for key, field in (("task_name", "task.name"), ("task_id", "messaging.message.id"), ("queue", "task.queue")):
        if query[key]:
            filters.append(f"span.{field} = {json.dumps(query[key])}")
    for key, attribute in (("environment", "deployment.environment.name"), ("service", "service.name"), ("instance", "service.instance.id")):
        if query.get(key):
            filters.append(f"resource.{attribute} = {json.dumps(query[key])}")
    if query.get("keyword"):
        filters.append("name =~ " + json.dumps(".*" + re.escape(query["keyword"]) + ".*"))
    if query["attribute"]:
        filters.append(f'span.{query["attribute"]} = {json.dumps(query["value"])}')
    if query["outcome"] == "problem":
        filters.append('(status = error || span.task.outcome = "failed" || span.task.outcome = "retrying" || span.task.business.outcome = "failed")')
    if query["min_duration_ms"]:
        filters.append(f'duration >= {query["min_duration_ms"]}ms')
    return "{ " + " && ".join(filters) + " }"


async def _read(view: str, query: dict) -> dict:
    # Control-only optional dependency; importing the CLI remains Python 3.9 safe.
    from luma_observe.query import ObserveClient, QueryUnavailable, ReadEndpoints

    endpoints = ReadEndpoints(metrics=observe_base_url(), logs="http://127.0.0.1:9428",
                              traces="http://127.0.0.1:3200")
    class ScopedClient(ObserveClient):
        async def request(self, kind, path, params=None):
            params = dict(params or {})
            if kind == "metrics":
                params["time"] = query["until"]
            elif kind == "logs":
                params["start"] = max(int(params.get("start", query["since"])), query["since"])
                params["end"] = min(int(params.get("end", query["until"])), query["until"])
                filters = []
                for key, field in (("environment", "environment"), ("service", "service_name"), ("instance", "service_instance_id")):
                    if query.get(key):
                        filters.append(f"{field}:={json.dumps(query[key])}")
                if query.get("keyword"):
                    filters.append("_msg:" + json.dumps(query["keyword"]))
                if filters:
                    params["query"] = " ".join(filters) + " " + params.get("query", "")
            return await super().request(kind, path, params)

    async with ScopedClient(endpoints, apps=[query["app"]],
                             resource_attributes={"luma.stack": query["app"]}) as client:
        if view == "overview":
            from .task_results import read as completion_results
            metrics = metric_queries(query["app"], query["hours"], query)
            metrics.pop("executions", None)
            metrics.pop("span_executions", None)
            sources, completions = await asyncio.gather(client.metrics(metrics), run_in_threadpool(completion_results, query))
            sources["executions"] = completions
            return {"state": "ok", "sources": sources, "sampled_at": query["until"]}
        if view == "events":
            attributes = {"task.queue": query["queue"]} if query["queue"] else {}
            if query["attribute"]:
                attributes[query["attribute"]] = query["value"]
            return await client.events(task_name=query["task_name"], task_id=query["task_id"],
                                       attributes=attributes, hours=query["hours"], outcome=query["outcome"],
                                       offset=query["offset"], until=query["until"])
        if view == "trace":
            return await client.trace_detail(query["trace_id"])
        if view == "logs":
            return await client.logs(query["trace_id"], hours=query["hours"], until=query["until"])
        try:
            body = json.loads(await client.request("traces", "/api/search", {
                "q": trace_query(query), "start": query["since"],
                "end": query["until"], "limit": 51,
            }))
            traces = body["traces"] if "traces" in body else []
            items = []
            for row in traces[:50]:
                trace_id = str(row["traceID"])
                duration = float(row.get("durationMs", 0))
                if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or not math.isfinite(duration):
                    continue
                items.append({"traceID": trace_id, "rootServiceName": str(row.get("rootServiceName", ""))[:200],
                              "rootTraceName": str(row.get("rootTraceName", ""))[:200],
                              "startTimeUnixNano": str(int(row.get("startTimeUnixNano", 0))), "durationMs": duration})
            return {"state": "ok", "items": items, "sampled": True, "limit": 50,
                    "possibly_truncated": len(traces) > 50}
        except (QueryUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
            return {"state": str(exc) if isinstance(exc, QueryUnavailable) else "invalid_response", "items": []}


async def handle_task_observe(token: str, view: str, params: dict) -> dict:
    """Authenticate every read and bound aggregate dashboard query concurrency."""
    await run_in_threadpool(_authorize, token)
    if view not in VIEWS:
        raise LumaError("unknown task observation view")
    query = task_query(params)
    if view in {"trace", "logs"} and not query["trace_id"]:
        raise LumaError("trace_id is required")
    try:
        await asyncio.wait_for(READ_SLOTS.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        return {"state": "rate_limited", "items": [], "spans": []}
    try:
        return await asyncio.wait_for(_read(view, query), timeout=12)
    except asyncio.TimeoutError:
        return {"state": "timeout", "items": [], "spans": []}
    except ImportError:
        return {"state": "not_configured", "items": [], "spans": []}
    finally:
        READ_SLOTS.release()
