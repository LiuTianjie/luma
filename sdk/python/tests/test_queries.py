"""Query contracts use the existing observability stores with bounded reads."""

import asyncio
import json
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from luma_observe.query import ObserveClient, QueryUnavailable, QUERY_MAX_BYTES, ReadEndpoints, normalize_event
from test_task_runtime import observed as observed


RESOURCE_SCOPE = {"service.namespace": "documents", "deployment.environment.name": "production"}


def query_client(endpoints, **options):
    return ObserveClient(endpoints, apps=("document-workers",), resource_attributes=RESOURCE_SCOPE, **options)


@pytest.mark.asyncio
async def test_query_reads_keep_runtime_uninitialized_and_isolate_bad_sources(monkeypatch):
    runtime = importlib.import_module("luma_observe.runtime")
    initialize = Mock(side_effect=AssertionError("query must not initialize telemetry"))
    monkeypatch.setattr(runtime, "get_runtime", initialize)

    def respond(request):
        if request.url.params["query"] == "broken":
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})

    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.metrics({"valid": "count", "broken": "broken", "coverage": "started"},
                                      require_data=("coverage",))
    assert {name: item["state"] for name, item in result.items()} == {
        "valid": "ok", "broken": "invalid_response", "coverage": "no_data",
    }
    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_real_sdk_trace_keeps_parentage_and_explicit_resource_scope(observed):
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    import luma_observe as observe

    runtime, exporter, _reader = observed
    with runtime.task("document.convert", attributes={"document.id": "doc-7", "private": "hidden"}):
        with observe.stage("document.render"):
            observe.record_business_outcome("failed", failed=True, error_type="conversion_failed")
    spans = exporter.get_finished_spans()
    trace_id = format(spans[0].context.trace_id, "032x")
    body = MessageToDict(encode_spans(spans))
    foreign = json.loads(json.dumps(body["resourceSpans"][0]))
    foreign["resource"]["attributes"] = [{"key": "service.name", "value": {"stringValue": "another-service"}}]
    body["resourceSpans"].append(foreign)
    async with ObserveClient(ReadEndpoints(traces="https://observe.example.com/traces"), apps=("document-workers",),
                             resource_attributes={"service.name": "sample-worker"},
                             attribute_aliases={"document_id": "document.id"}) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
        detail = await client.trace_detail(trace_id)
    assert detail["state"] == "ok" and detail["partial"]
    root = next(span for span in detail["spans"] if span["name"] == "document.convert")
    child = next(span for span in detail["spans"] if span["name"] == "document.render")
    assert len(detail["spans"]) == 2 and child["parentSpanId"] == root["spanId"]
    assert root["status"] == "error" and root["attributes"]["document.id"] == "doc-7"
    assert "hidden" not in json.dumps(detail)


@pytest.mark.asyncio
async def test_queries_reject_unbounded_or_invalid_identifiers_before_request():
    async with query_client(ReadEndpoints()) as client:
        with pytest.raises(ValueError):
            await client.events(hours=73)
        with pytest.raises(ValueError):
            await client.events(offset=2001)
        with pytest.raises(ValueError):
            await client.logs("")
        with pytest.raises(ValueError):
            await client.trace_detail("../search")


def test_query_scope_and_aliases_preserve_task_identity():
    with pytest.raises(ValueError):
        ObserveClient(ReadEndpoints(), apps=(), resource_attributes=RESOURCE_SCOPE)
    with pytest.raises(ValueError):
        query_client(ReadEndpoints(), attribute_aliases={"trace_id": "document.id"})
    record = {"event": "task.finished", "trace_id": "a" * 32,
              "attributes": {"document.id": "doc-7", "private": "hidden"}}
    normalized = normalize_event(record, {"document_id": "document.id"})
    assert normalized["trace_id"] == "a" * 32 and normalized["document_id"] == "doc-7"
    assert "hidden" not in json.dumps(normalized)


def test_flattened_sdk_json_preserves_only_declared_task_fields():
    """VictoriaLogs 展开的 SDK 字段映射到现有接口，业务参数保持裁剪。"""
    record = {"event": "task.finished", "timestamp": 1, "attributes.task.name": "sample",
              "attributes.messaging.message.id": "task", "attributes.task.framework": "celery",
              "attributes.task.queue": "print", "attributes.task.business.status": "ready",
              "attributes.outcome": "succeeded", "attributes.duration_seconds": 0.2, "attributes.args": "private"}
    event = normalize_event(record)
    assert event["task_name"] == "sample" and event["task_id"] == "task" and event["runtime"] == "celery"
    assert event["business_status"] == "ready" and event["elapsed_seconds"] == 0.2
    assert "private" not in json.dumps(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,state", [(401, "unauthorized"), (403, "unauthorized"), (429, "rate_limited"),
                                          (500, "upstream_error"), (302, "upstream_error")])
async def test_query_errors_and_redirects_remain_explicit(status, state):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status)
    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with pytest.raises(QueryUnavailable, match=state):
            await client.request("metrics", "/api/v1/query", {})
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
async def test_metrics_recovers_one_transient_transport_failure(error):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise error("temporary failure", request=request)
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.metrics({"current": "task_active"})
    assert result["current"]["state"] == "ok" and len(requests) == 2
    assert requests[0].url == requests[1].url


@pytest.mark.asyncio
async def test_repeated_metrics_timeouts_stop_after_one_retry():
    requests = []
    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout("store unavailable", request=request)
    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.metrics({"current": "task_active"})
    assert result["current"] == {"state": "timeout", "rows": []}
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_stalled_first_read_retries_within_total_budget(monkeypatch):
    monkeypatch.setattr("luma_observe.query.METRIC_ATTEMPT_TIMEOUT_SECONDS", 0.02)
    requests = []
    closed = asyncio.Event()
    async def respond(request):
        requests.append(request)
        if len(requests) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics"), query_timeout=1) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.metrics({"current": "task_active"})
    assert result["current"]["state"] == "ok" and len(requests) == 2
    assert closed.is_set()


@pytest.mark.asyncio
async def test_query_deadline_covers_queued_reads_and_releases_connections():
    active = peak = 0
    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics"), query_timeout=0.05) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await asyncio.wait_for(client.metrics({str(index): "task_active" for index in range(11)}), 1)
    assert {value["state"] for value in result.values()} == {"timeout"}
    assert peak == 3 and active == 0


@pytest.mark.asyncio
async def test_cancelling_query_propagates_without_retry():
    entered = asyncio.Event()
    requests = []
    async def respond(request):
        requests.append(request)
        entered.set()
        await asyncio.Event().wait()
    async with query_client(ReadEndpoints(metrics="https://observe.example.com/metrics")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        pending = asyncio.create_task(client.metrics({"current": "task_active"}))
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_query_response_size_is_bounded():
    async with query_client(ReadEndpoints(logs="https://observe.example.com/logs")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"*(QUERY_MAX_BYTES+1))))
        with pytest.raises(QueryUnavailable, match="response_limit"):
            await client.request("logs", "/select/logsql/query", {})


@pytest.mark.asyncio
async def test_missing_query_configuration_produces_unknown_not_empty_success():
    async with query_client(ReadEndpoints()) as client:
        events = await client.events(task_id="task")
        traces = client.traces(events)
        metrics = await client.metrics({"count": "task_executions_total"})
    assert events["state"] == traces["state"] == "not_configured"
    assert all(value["state"] == "not_configured" for value in metrics.values())


def test_trace_list_comes_from_execution_events_without_trace_search():
    """Trace 列表由生命周期日志整理：每次执行一条，带耗时；投递事件和无效编号不进入列表。"""
    execution, producer = "a" * 32, "b" * 32
    events = {"state": "ok", "items": [
        {"event": "task_published", "trace_id": producer, "task_name": "sample", "timestamp": 1.0},
        {"event": "task_started", "trace_id": execution, "task_name": "sample", "timestamp": 2.0},
        {"event": "task_stage", "trace_id": execution, "task_name": "sample", "timestamp": 2.5},
        {"event": "task_finished", "trace_id": execution, "task_name": "sample", "timestamp": 3.0, "elapsed_seconds": 1.25},
        {"event": "task_started", "trace_id": "not-a-trace", "task_name": "sample", "timestamp": 4.0},
    ]}
    client = SimpleNamespace(trace_url=lambda trace_id: "https://grafana/" + trace_id)
    traces = ObserveClient.traces(client, events)
    assert traces["state"] == "ok"
    assert traces["items"] == [{"traceID": execution, "rootServiceName": "", "rootTraceName": "sample",
                                "startTimeUnixNano": str(2 * 10**9), "durationMs": 1250, "url": "https://grafana/" + execution}]
    assert ObserveClient.traces(client, {"state": "unreachable", "items": []})["state"] == "unreachable"


@pytest.mark.asyncio
async def test_problem_filter_and_pagination_use_one_frozen_window():
    """失败条件先于分页执行，同一时间戳的事件使用固定排序和窗口继续读取。"""
    requests = []

    def respond(request):
        """模拟时间相同的多条失败事件，第二页仅剩一条。"""
        requests.append(request)
        offset = 0 if len(requests) == 1 else 100
        rows = [{"event": "task.finished", "timestamp": 1000, "trace_id": f"{index:032x}",
                 "attributes.task.name": "sample", "attributes.outcome": "failed"}
                for index in range(offset, 101)]
        return httpx.Response(200, content="\n".join(json.dumps(row) for row in rows))

    async with query_client(ReadEndpoints(logs="https://observe.example.com/logs")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        first = await client.events(task_name="sample", outcome="problem", until=2000, hours=1)
        second = await client.events(task_name="sample", outcome="problem", until=first["until"], offset=first["next_offset"], hours=1)
    assert len(first["items"]) == 100 and first["next_offset"] == 100 and first["possibly_truncated"]
    assert len(second["items"]) == 1 and second["next_offset"] is None and not second["possibly_truncated"]
    query = requests[0].url.params["query"]
    assert query.index('"attributes.outcome":="failed"') < query.index("sort by")
    assert '"attributes.outcome":="retrying"' in query and '"attributes.task.business.outcome":="failed"' in query
    assert "(_time desc, _stream_id, _msg)" in query and "offset 100" in requests[1].url.params["query"]
    assert all(request.url.params["start"] == "-1600" and request.url.params["end"] == "2000" for request in requests)


@pytest.mark.asyncio
async def test_problem_filter_uses_application_business_outcomes_before_pagination():
    """Business retries can return framework success and remain discoverable by the caller's policy."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=json.dumps({
            "event": "task.finished", "timestamp": 1000, "trace_id": "a" * 32,
            "attributes.outcome": "succeeded", "attributes.task.business.outcome": "deferred",
        }))

    async with query_client(ReadEndpoints(logs="https://observe.example.com/logs"),
                            problem_business_outcomes=("failed", "deferred")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.events(task_name="sample", outcome="problem")
    query = requests[0].url.params["query"]
    assert query.index('"attributes.task.business.outcome":="deferred"') < query.index("sort by")
    assert '"attributes.task.business.outcome":="failed"' in query
    assert '"attributes.task.business.outcome":="retrying"' not in query
    assert result["items"][0]["outcome"] == "succeeded"
    assert result["items"][0]["business_outcome"] == "deferred"


@pytest.mark.asyncio
async def test_raw_logs_use_exact_attempt_trace_and_preserve_full_error():
    """原始异常以 Trace 精确对应尝试，即使其他日志引用该 Trace 也按记录身份区分。"""
    trace_id, other = "a" * 32, "b" * 32
    requests = []
    stack = "Traceback (most recent call last):\n  worker.py:42\nRuntimeError: 写锁超时"

    def respond(request):
        """另一尝试只在错误正文提到当前 Trace，真实诊断在后一条日志。"""
        requests.append(request)
        rows = [{"_time": "2026-11-02T23:00:00Z", "_msg": json.dumps({
            "event": "task.failure.detail", "trace_id": value, "exception": {
                "error_type": "RuntimeError", "error_message": trace_id if value == other else "详细错误" * 100,
                "stacktrace": "other attempt" if value == other else stack, "truncated": False,
            },
        })} for value in (other, trace_id)]
        return httpx.Response(200, content="\n".join(json.dumps(row) for row in rows))

    async with query_client(ReadEndpoints(logs="https://observe.example.com/logs")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.logs(trace_id, until=1793664000)
    assert len(requests) == 1 and f'_msg:~"{trace_id}"' in requests[0].url.params["query"]
    assert result["diagnostic"]["stacktrace"] == stack
    assert len(result["diagnostic"]["error_message"]) > 256 and not result["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["exact", "referenced", "reference_only"])
async def test_legacy_log_context_is_scoped_to_exact_trace_anchor(case):
    """旧执行只有生命周期 Trace 时，只补读同一容器的二十秒相邻日志并显式标记。"""
    trace_id = "a" * 32
    requests = []

    def respond(request):
        """模拟真实终态、其他任务引用该编号，以及按精确终态读取的上下文。"""
        requests.append(request)
        anchor = {"_time": "2026-11-02T23:00:00Z", "app": "document-workers", "alloc": "allocation-1",
                  "task": "celery-worker", "stream": "stdout", "_msg": json.dumps({"event": "task.finished", "trace_id": trace_id})}
        unrelated = {**anchor, "alloc": "allocation-other", "_msg": json.dumps({
            "event": "task.finished", "trace_id": "b" * 32, "message": f"referenced task {trace_id}",
        })}
        rows = ([unrelated] if case != "exact" else []) + ([anchor] if case != "reference_only" else [])
        if len(requests) > 1:
            rows = [anchor, {**anchor, "_msg": "RuntimeError: 等待写锁超时"}]
        return httpx.Response(200, content="\n".join(json.dumps(row) for row in rows))

    async with query_client(ReadEndpoints(logs="https://observe.example.com/logs")) as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await client.logs(trace_id, until=1793664000)
    if case == "reference_only":
        assert len(requests) == 1 and not result["context"] and result["diagnostic"] is None
        return
    assert len(requests) == 2 and result["context"] and result["diagnostic"] is None
    assert 'alloc:="allocation-1"' in requests[1].url.params["query"] and 'task:="celery-worker"' in requests[1].url.params["query"]
    assert float(requests[1].url.params["end"]) - float(requests[1].url.params["start"]) == 20
    assert [row["matched"] for row in result["items"]] == [True, False]
