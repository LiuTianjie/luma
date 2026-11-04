"""Optional asynchronous reads from the existing Luma observability backends.

Install the ``query`` extra. Importing this module creates no telemetry providers,
background threads, cache, database or additional service.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import time
from typing import Mapping, Sequence
from urllib.parse import urlencode, urlsplit

import httpx

from ._query_data import EVENT_FIELDS, EVENT_NAMES, TRACE_ID, finite, normalize_event, normalize_trace
from .diagnostics import ERROR_MESSAGE_BYTES, ERROR_STACK_BYTES, clip_utf8


QUERY_TIMEOUT_SECONDS = 8
METRIC_ATTEMPT_TIMEOUT_SECONDS = 3
EVIDENCE_TIMEOUT_SECONDS = 8
QUERY_MAX_BYTES = 2 * 1024 * 1024
MAX_LOG_ROWS = 100


@dataclass(frozen=True)
class ReadEndpoints:
    """Explicit read addresses and credentials, independent of OTLP write settings."""

    metrics: str = ""
    logs: str = ""
    traces: str = ""
    token: str = field(default="", repr=False)
    grafana: str = ""

    @classmethod
    def from_origin(cls, origin: str, *, token: str = "") -> "ReadEndpoints":
        """Use the standard backend paths on a configured Luma query origin."""
        origin = origin.rstrip("/")
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            return cls()
        return cls(**{name: origin + "/" + name for name in ("metrics", "logs", "traces", "grafana")}, token=token)

    def valid_url(self, kind: str) -> str:
        """Return only configured HTTP backend addresses without embedded credentials."""
        value = getattr(self, kind)
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return ""
        return value.rstrip("/")


class QueryUnavailable(Exception):
    """携带固定查询失败分类，便于页面解释数据缺口。"""


class ObserveClient:
    def __init__(self, endpoints: ReadEndpoints, *, apps: Sequence[str],
                 resource_attributes: Mapping[str, str], attribute_aliases: Mapping[str, str] | None = None,
                 problem_business_outcomes: Sequence[str] = ("failed",),
                 query_timeout: float = QUERY_TIMEOUT_SECONDS, evidence_timeout: float = EVIDENCE_TIMEOUT_SECONDS):
        """Create a read-only client with caller-owned application and resource scope."""
        if not apps or isinstance(apps, str) or not resource_attributes:
            raise ValueError("Explicit application and trace resource scopes are required")
        self.endpoints = endpoints
        self.apps = tuple(apps)
        self.resource_attributes = dict(resource_attributes)
        self.attribute_aliases = dict(attribute_aliases or {})
        self.problem_business_outcomes = tuple(problem_business_outcomes)
        if self.attribute_aliases.keys() & EVENT_FIELDS:
            raise ValueError("Custom aliases must preserve standard event fields")
        self.query_timeout, self.evidence_timeout = query_timeout, evidence_timeout
        self.http = httpx.AsyncClient(timeout=evidence_timeout, trust_env=False, follow_redirects=False,
                                     limits=httpx.Limits(max_connections=3, max_keepalive_connections=3))
        self.slots = asyncio.Semaphore(3)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.http.aclose()

    async def request(self, kind: str, path: str, params: dict) -> bytes:
        """以总预算覆盖排队和读取，指标瞬时连接故障最多重试一次。"""
        base = self.endpoints.valid_url(kind)
        if not base:
            raise QueryUnavailable("not_configured")
        headers = {"Authorization": f"Bearer {self.endpoints.token}"} if self.endpoints.token else {}
        budget = self.query_timeout if kind == "metrics" else self.evidence_timeout
        if kind == "traces":
            headers["Accept"] = "application/json"

        async def read():
            """释放失败请求的并发名额，让其余查询继续读取。"""
            attempts = 2 if kind == "metrics" else 1
            for attempt in range(attempts):
                try:
                    async with self.slots:
                        attempt_timeout = min(budget, METRIC_ATTEMPT_TIMEOUT_SECONDS) if kind == "metrics" and attempt == 0 else budget
                        return await asyncio.wait_for(self._read(base + path, params, headers), attempt_timeout)
                except (httpx.TransportError, asyncio.TimeoutError):
                    if attempt + 1 == attempts:
                        raise

        try:
            return await asyncio.wait_for(read(), budget)
        except QueryUnavailable:
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise QueryUnavailable("timeout") from None
        except httpx.HTTPError:
            raise QueryUnavailable("unreachable") from None

    async def _read(self, url: str, params: dict, headers: dict) -> bytes:
        """流式读取有限响应，对认证失败、超时和上游错误提供固定分类。"""
        async with self.http.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code in {401, 403}:
                raise QueryUnavailable("unauthorized")
            if response.status_code == 404:
                raise QueryUnavailable("not_found")
            if response.status_code == 429:
                raise QueryUnavailable("rate_limited")
            if response.status_code != 200:
                raise QueryUnavailable("upstream_error")
            result = bytearray()
            async for chunk in response.aiter_bytes():
                result.extend(chunk)
                if len(result) > QUERY_MAX_BYTES:
                    raise QueryUnavailable("response_limit")
            return bytes(result)

    async def metrics(self, queries: Mapping[str, str], *, require_data: Sequence[str] = ()) -> dict:
        """Read named Prometheus vector queries; preserve each source's availability."""
        async def one(name: str, query: str):
            """解析 Prometheus 向量，并保留每个查询的可用性。"""
            try:
                body = json.loads(await self.request("metrics", "/api/v1/query", {"query": query, "timeout": "2s"}))
                if body.get("status") != "success" or body["data"]["resultType"] != "vector":
                    raise QueryUnavailable("invalid_response")
                if name in require_data and not body["data"]["result"]:
                    raise QueryUnavailable("no_data")
                return name, {"state": "ok", "rows": body["data"]["result"]}
            except (QueryUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
                return name, {"state": str(exc) if isinstance(exc, QueryUnavailable) else "invalid_response", "rows": []}
        return dict(await asyncio.gather(*(one(name, query) for name, query in queries.items())))

    async def events(self, *, task_name: str = "", task_id: str = "", attributes: Mapping[str, str] | None = None,
                     hours: int = 24, outcome: str = "all", offset: int = 0, until: int | None = None) -> dict:
        """在固定时间窗口分页读取生命周期，失败筛选由存储执行。"""
        if not 1 <= hours <= 72 or not 0 <= offset <= 2000 or outcome not in {"all", "problem"}:
            raise ValueError("Use a 1-72 hour window, offset 0-2000 and all/problem outcome")
        end = min(until or int(time.time()), int(time.time()))
        apps = " OR ".join(f'app:={json.dumps(app)}' for app in self.apps)
        base = f'({apps}) _msg:"event" | unpack_json | filter '
        filters = ["(" + " OR ".join(f"event:={json.dumps(name)}" for name in EVENT_NAMES) + ")"]
        selectors = {**(attributes or {}), "task.name": task_name, "messaging.message.id": task_id}
        for key, value in selectors.items():
            if value:
                filters.append(f"{json.dumps('attributes.' + key)}:={json.dumps(value)}")
        if outcome == "problem":
            problem_outcomes = [f'"attributes.task.business.outcome":={json.dumps(value)}'
                                for value in self.problem_business_outcomes]
            filters.append('(event:="task.failure" OR (event:="task.finished" AND '
                           '("attributes.outcome":="failed" OR "attributes.outcome":="retrying" '
                           + ''.join(f'OR {condition} ' for condition in problem_outcomes) + ')))')

        async def read(selected_filters, page_offset=0):
            """读取一页及下一条证据，用明确游标区分已加载数量与更多结果。"""
            query = base + " ".join(selected_filters) + (
                f" | sort by (_time desc, _stream_id, _msg) offset {page_offset} limit {MAX_LOG_ROWS + 1}"
            )
            body = await self.request("logs", "/select/logsql/query", {
                "query": query, "limit": MAX_LOG_ROWS + 1, "timeout": "7s", "start": end - hours * 3600, "end": end,
            })
            return [normalize_event(json.loads(line), self.attribute_aliases) for line in body.splitlines()[:MAX_LOG_ROWS + 1]]

        meta = {"limit": MAX_LOG_ROWS, "hours": hours, "until": end, "outcome": outcome,
                "next_offset": None, "related_truncated": False}
        try:
            selected = await read(filters, offset)
            more = len(selected) > MAX_LOG_ROWS
            rows = selected[:MAX_LOG_ROWS]
            truncated = more
            if outcome == "all" and (task_id or attributes):
                identities = sorted({row.get("trace_id", "") for row in rows if TRACE_ID.fullmatch(row.get("trace_id", ""))})
                if identities:
                    related = await read([filters[0], "(" + " OR ".join(f"trace_id:={json.dumps(value)}" for value in identities[:20]) + ")"])
                    meta["related_truncated"] = len(related) > MAX_LOG_ROWS or len(identities) > 20
                    truncated |= meta["related_truncated"]
                    rows = list({json.dumps(row, sort_keys=True): row for row in [*rows, *related[:MAX_LOG_ROWS]]}.values())
            rows.sort(key=lambda row: finite(row.get("timestamp")) or 0, reverse=True)
            identities = {}
            for row in rows:
                known = identities.setdefault(row.get("trace_id", ""), {})
                for key in ("task_id", "task_name", "runtime", "queue", "retry"):
                    if row.get(key) is not None:
                        known.setdefault(key, row[key])
            for row in rows:
                for key, value in identities.get(row.get("trace_id", ""), {}).items():
                    row.setdefault(key, value)
            return {**meta, "state": "ok", "items": rows,
                    "next_offset": offset + MAX_LOG_ROWS if more and offset < 2000 else None,
                    "possibly_truncated": truncated}
        except (QueryUnavailable, ValueError, TypeError, AttributeError) as exc:
            return {**meta, "state": str(exc) if isinstance(exc, QueryUnavailable) else "invalid_response", "items": []}

    async def logs(self, trace_id: str, *, hours: int = 24, until: int | None = None) -> dict:
        """以本次 Trace 定位原始异常，旧日志读取同一 Worker 的有限上下文。"""
        if not TRACE_ID.fullmatch(trace_id) or not 1 <= hours <= 72:
            raise ValueError("Use a 32-character lowercase Trace ID and a 1-72 hour window")
        apps = " OR ".join(f'app:={json.dumps(app)}' for app in self.apps)
        end = min(until or int(time.time()), int(time.time()))
        pattern = re.escape(trace_id)

        async def read(selector, start, stop, limit):
            """返回有限原始行，完整堆栈保留在结构化异常记录中。"""
            query = f'({apps}) {selector} | sort by (_time desc, _stream_id, _msg) limit {limit + 1}'
            query += ' | fields _time, _msg, app, alloc, task, stream'
            body = await self.request("logs", "/select/logsql/query", {
                "query": query, "start": start, "end": stop, "limit": limit + 1, "timeout": "7s",
            })
            return [json.loads(line) for line in body.splitlines()[:limit + 1]]

        try:
            matches = await read(f'_msg:~{json.dumps(pattern)}', end - hours * 3600, end, 60)
            diagnostic = None
            anchor = None
            for row in matches:
                try:
                    record = json.loads(row.get("_msg", ""))
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("trace_id") != trace_id:
                    continue
                anchor = anchor or row
                if record.get("event") != "task.failure.detail":
                    continue
                detail = record.get("exception")
                if isinstance(detail, dict):
                    diagnostic = {key: clip_utf8(str(detail.get(key, "")), limit) for key, limit in (
                        ("description", 256), ("error_type", 256),
                        ("error_message", ERROR_MESSAGE_BYTES), ("stacktrace", ERROR_STACK_BYTES))}
                    diagnostic["truncated"] = bool(detail.get("truncated")) or any(
                        diagnostic[key] != str(detail.get(key, "")) for key in diagnostic
                    )
                    break
            rows, context = matches, False
            if diagnostic is None and anchor and anchor.get("alloc") and anchor.get("task"):
                moment = datetime.fromisoformat(anchor["_time"].replace("Z", "+00:00")).timestamp()
                selector = " ".join(f"{key}:={json.dumps(anchor[key])}" for key in ("app", "alloc", "task", "stream") if anchor.get(key))
                rows = await read(selector, max(end - hours * 3600, moment - 10), min(end, moment + 10), 200)
                context = True
            limit = 200 if context else 60
            items = [{"timestamp": row.get("_time", ""), "line": str(row.get("_msg", ""))[:32768],
                      "app": row.get("app", ""), "task": row.get("task", ""), "stream": row.get("stream", ""),
                      "matched": bool(re.search(pattern, str(row.get("_msg", ""))))} for row in rows[:limit]]
            return {"state": "ok", "items": items, "diagnostic": diagnostic, "context": context,
                    "possibly_truncated": len(rows) > limit or any(len(str(row.get("_msg", ""))) > 32768 for row in rows[:limit])}
        except (QueryUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
            return {"state": str(exc) if isinstance(exc, QueryUnavailable) else "invalid_response", "items": []}

    def traces(self, events: dict) -> dict:
        """List attempts from lifecycle evidence, independently of trace sampling."""
        if events.get("state") != "ok":
            return {"state": events.get("state", "no_data"), "items": [], "sampled": True, "limit": 20}
        attempts = {}
        for row in events.get("items", []):
            trace_id = row.get("trace_id", "")
            # 投递事件属于生产者的 Trace，列表只收录执行。
            if not TRACE_ID.fullmatch(trace_id) or row.get("event") not in {"task_started", "task_finished", "task_stage", "business_outcome", "phase_started", "phase_finished", "task_failure"}:
                continue
            item = attempts.setdefault(trace_id, {"traceID": trace_id, "rootServiceName": "", "rootTraceName": row.get("task_name", ""),
                                                  "startTimeUnixNano": None, "durationMs": None})
            if row.get("event") == "task_started" and finite(row.get("timestamp")) is not None:
                item["startTimeUnixNano"] = str(int(finite(row["timestamp"]) * 1e9))
            if row.get("event") == "task_finished" and finite(row.get("elapsed_seconds")) is not None:
                item["durationMs"] = round(finite(row["elapsed_seconds"]) * 1000)
            item["rootTraceName"] = item["rootTraceName"] or row.get("task_name", "")
        items = sorted(attempts.values(), key=lambda item: int(item["startTimeUnixNano"] or 0), reverse=True)[:20]
        for item in items:
            item["url"] = self.trace_url(item["traceID"])
        return {"state": "ok", "items": items, "sampled": True, "limit": 20}

    async def trace_detail(self, trace_id: str) -> dict:
        """Read a retained trace within the configured resource scope and query budget."""
        if not TRACE_ID.fullmatch(trace_id):
            raise ValueError("Use a 32-character lowercase Trace ID")
        try:
            body = json.loads(await self.request("traces", f"/api/traces/{trace_id}", {}))
            return {**normalize_trace(body, trace_id, resource_attributes=self.resource_attributes,
                                      extra_attributes=tuple(self.attribute_aliases.values())), "url": self.trace_url(trace_id)}
        except (QueryUnavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
            return {"state": str(exc) if isinstance(exc, QueryUnavailable) else "invalid_response", "traceId": trace_id, "spans": []}

    def trace_url(self, trace_id: str) -> str:
        """构造 Grafana Explore 的标准 Tempo 查询链接。"""
        base = self.endpoints.valid_url("grafana")
        if not base or not TRACE_ID.fullmatch(trace_id):
            return ""
        panes = {"task": {"datasource": "tempo", "queries": [{"refId": "A", "queryType": "traceql", "query": trace_id}], "range": {"from": "now-24h", "to": "now"}}}
        return base + "/explore?" + urlencode({"schemaVersion": 1, "panes": json.dumps(panes, separators=(",", ":"))})
