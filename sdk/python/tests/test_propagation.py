"""Task messages cross queue delays and retries without sharing execution traces."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from celery import Celery
from opentelemetry import baggage, context, propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from taskiq import InMemoryBroker, TaskiqMessage

import luma_observe.runtime as core
import luma_observe.instrumentation as hooks
from luma_observe.instrumentation.celery import CeleryInstrumentation
from luma_observe.instrumentation.taskiq import TaskiqMiddleware


@pytest.fixture
def observed(monkeypatch, request):
    for key in tuple(core.os.environ):
        if key.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    for key, value in getattr(request, "param", {}).items():
        monkeypatch.setenv(key, value)
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    monkeypatch.setattr(core, "OTLPSpanExporter", lambda **_: exporter)
    monkeypatch.setattr(core, "OTLPMetricExporter", lambda **_: Mock(_preferred_temporality=None, _preferred_aggregation=None))
    monkeypatch.setattr(core, "BatchSpanProcessor", lambda exporter, **_: SimpleSpanProcessor(exporter))
    monkeypatch.setattr(core, "PeriodicExportingMetricReader", lambda *_a, **_k: reader)
    configuration = core.Configuration(service_name="sample-worker")
    monkeypatch.setattr(core, "_configuration", configuration)
    runtime = core.Runtime(configuration)
    assert runtime.enabled
    monkeypatch.setattr(hooks, "get_runtime", lambda: runtime)
    yield runtime, reader, exporter
    runtime.shutdown()


def span_context(carrier):
    return trace.get_current_span(propagate.extract(carrier, context=Context())).get_span_context()


def begin(owner, carrier, retries=0):
    hooks._start_attempt(owner, name="sample.execute", framework="taskiq", queue="samples", task_id="message-1",
                         retries=retries, carrier=carrier)


def test_queued_execution_is_a_new_trace_with_source_link_and_current_context(observed, monkeypatch):
    runtime, reader, exporter = observed
    clock = [1_000.0]
    monkeypatch.setattr(hooks, "time", SimpleNamespace(time=lambda: clock[0]))
    carrier, owner = {}, SimpleNamespace()
    producer_context = baggage.set_baggage("workspace", "sample", context=Context())
    token = context.attach(producer_context)
    try:
        with runtime.span("upstream.request"):
            hooks._prepare_carrier(carrier, "sample.execute", "taskiq", "message-1")
    finally:
        context.detach(token)
    source = span_context(carrier)
    # The producer has already finished long before execution. No real wait or
    # Collector is needed to prove its trace cannot contain the execution span.
    clock[0] += 400
    with runtime.span("worker.ambient") as ambient:
        begin(owner, carrier)
        try:
            run = hooks._attempt(owner).run
            assert trace.get_current_span() is run.span
            assert baggage.get_baggage("workspace") == "sample"
            assert run.span.get_span_context().trace_id not in (source.trace_id, ambient.get_span_context().trace_id)
            run.add_event("sample.progress", {"stage": "render"})
        finally:
            hooks._finish_attempt(owner, "succeeded")
        assert trace.get_current_span() is ambient
        assert baggage.get_baggage("workspace") is None
    execution = next(span for span in exporter.get_finished_spans() if span.name == "sample.execute")
    assert execution.parent is None
    assert len(execution.links) == 1 and execution.links[0].context == source
    assert execution.events[0].name == "sample.progress"
    assert not trace.get_current_span().get_span_context().is_valid
    data = reader.get_metrics_data()
    queue_wait = [point for resource in data.resource_metrics for scope in resource.scope_metrics
                  for metric in scope.metrics if metric.name == "task.queue_wait" for point in metric.data.data_points]
    assert queue_wait[0].sum == 400


@pytest.mark.parametrize("sampled", [False, True])
def test_external_w3c_context_is_linked_without_inheriting_remote_sampling(observed, sampled):
    _runtime, _reader, exporter = observed
    remote = trace.SpanContext(
        trace_id=0x123456789ABCDEF123456789ABCDEF12,
        span_id=0x123456789ABCDEF1,
        is_remote=True,
        trace_flags=trace.TraceFlags(1 if sampled else 0),
        trace_state=trace.TraceState([("vendor", "value")]),
    )
    carrier, owner = {}, SimpleNamespace()
    propagate.inject(carrier, context=trace.set_span_in_context(trace.NonRecordingSpan(remote), Context()))
    begin(owner, carrier)
    hooks._finish_attempt(owner, "succeeded")
    execution = exporter.get_finished_spans()[0]
    assert execution.parent is None and execution.context.trace_id != remote.trace_id
    assert execution.links[0].context == remote
    assert execution.links[0].context.trace_state == remote.trace_state
    assert execution.context.trace_flags.sampled


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ["taskiq", "celery"])
async def test_framework_business_context_keeps_remote_baggage_and_execution_span(observed, framework):
    _runtime, _reader, exporter = observed
    remote = trace.SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=trace.TraceFlags(1))
    remote_context = baggage.set_baggage("workspace", "sample", context=Context())
    carrier = {}
    propagate.inject(carrier, context=trace.set_span_in_context(trace.NonRecordingSpan(remote), remote_context))

    def business_result():
        assert baggage.get_baggage("workspace") == "sample"
        assert trace.get_current_span() is core.current_task().span
        return 42

    if framework == "taskiq":
        broker = InMemoryBroker(await_inplace=True).with_middlewares(TaskiqMiddleware())

        @broker.task(task_name="sample.execute")
        async def work():
            return business_result()

        message = TaskiqMessage(task_id="message-1", task_name=work.task_name, labels=carrier, args=[], kwargs={})
        try:
            await broker.receiver.callback(broker.formatter.dumps(message).message)
            assert (await broker.result_backend.get_result(message.task_id)).return_value == 42
        finally:
            await broker.shutdown()
    else:
        app = Celery("propagation-test", broker="memory://", backend="cache+memory://")
        instrumentation = CeleryInstrumentation()
        instrumentation._install()

        @app.task(name="sample.execute")
        def work():
            return business_result()

        try:
            assert work.apply(headers=carrier, throw=True).get() == 42
        finally:
            instrumentation.uninstrument()
            app.close()
    execution = next(span for span in exporter.get_finished_spans() if span.name == "sample.execute")
    assert execution.parent is None and execution.context.trace_id != remote.trace_id
    assert execution.links[0].context == remote
    assert baggage.get_baggage("workspace") is None
    assert not trace.get_current_span().get_span_context().is_valid
    assert core.current_task() is None


def test_scheduler_republication_links_carried_context_then_execution_links_new_prepare(observed, monkeypatch):
    _runtime, _reader, exporter = observed
    clock = [1_000.0]
    monkeypatch.setattr(hooks, "time", SimpleNamespace(time=lambda: clock[0]))
    middleware = TaskiqMiddleware()
    message = TaskiqMessage(task_id="message-1", task_name="sample.execute", labels={"custom": "retained"}, args=[], kwargs={})
    assert middleware.pre_send(message) is message
    original = span_context(message.labels)
    # Scheduling persists and reconstructs labels after the original execution
    # context is gone; it must not need an in-process parent span to keep a link.
    retried = TaskiqMessage.model_validate(message.model_dump())
    retried.labels["_retries"] = 1
    clock[0] += 60
    assert middleware.pre_send(retried) is retried
    current = span_context(retried.labels)
    assert current.trace_id != original.trace_id
    assert retried.labels[hooks.ENQUEUED_AT] == clock[0]
    assert retried.labels["custom"] == "retained"
    owner = SimpleNamespace()
    begin(owner, retried.labels, retries=1)
    hooks._finish_attempt(owner, "succeeded")
    preparations = [span for span in exporter.get_finished_spans() if span.name == "task.prepare/sample.execute"]
    assert len(preparations) == 2 and preparations[1].links[0].context == original
    execution = next(span for span in exporter.get_finished_spans() if span.name == "sample.execute")
    assert execution.parent is None and execution.links[0].context == current
    assert execution.context.trace_id not in (current.trace_id, original.trace_id)
    assert execution.attributes["task.retry.attempt"] == 1


@pytest.mark.parametrize("framework", ["taskiq", "celery"])
@pytest.mark.parametrize("disabled_by", [None, "OTEL_SDK_DISABLED", "LUMA_OBSERVE_ENABLED", "configure"])
def test_producer_without_endpoint_timestamps_unless_explicitly_disabled(monkeypatch, framework, disabled_by):
    for key in tuple(core.os.environ):
        if key.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(key)
    monkeypatch.setattr(core, "_runtime", None)
    monkeypatch.setattr(core, "_configuration", core.Configuration())
    providers = Mock(side_effect=AssertionError("no-endpoint producer must not create providers"))
    threads = Mock(side_effect=AssertionError("no-endpoint producer must not start workers"))
    monkeypatch.setattr(core, "TracerProvider", providers)
    monkeypatch.setattr(core, "MeterProvider", providers)
    monkeypatch.setattr(core.threading, "Thread", threads)
    if disabled_by == "configure":
        core.configure(enabled=False)
    elif disabled_by is not None:
        monkeypatch.setenv(disabled_by, "false" if disabled_by == "LUMA_OBSERVE_ENABLED" else "true")
    monkeypatch.setattr(hooks, "time", SimpleNamespace(time=lambda: 1_000.0))
    carrier = {"custom": "retained"}
    # A separate OTel integration may supply context even without Luma export.
    remote = trace.SpanContext(trace_id=1, span_id=2, is_remote=False, trace_flags=trace.TraceFlags(1))
    with trace.use_span(trace.NonRecordingSpan(remote)):
        if framework == "taskiq":
            message = TaskiqMessage(task_id="message-1", task_name="sample.execute", labels=carrier, args=[], kwargs={})
            TaskiqMiddleware().pre_send(message)
            carrier = message.labels
        else:
            CeleryInstrumentation()._before_publish(sender="sample.execute", headers=carrier)
    if disabled_by:
        assert carrier == {"custom": "retained"}
    else:
        assert carrier[hooks.ENQUEUED_AT] == 1_000.0
        assert span_context(carrier).trace_id == remote.trace_id
    providers.assert_not_called()
    threads.assert_not_called()


def test_producer_without_endpoint_preserves_carried_trace_when_no_link_can_be_exported(monkeypatch):
    for key in tuple(core.os.environ):
        if key.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(key)
    monkeypatch.setattr(core, "_runtime", None)
    monkeypatch.setattr(core, "_configuration", core.Configuration())
    original = trace.SpanContext(trace_id=1, span_id=2, is_remote=False, trace_flags=trace.TraceFlags(1))
    unrelated = trace.SpanContext(trace_id=3, span_id=4, is_remote=False, trace_flags=trace.TraceFlags(1))
    carrier = {}
    propagate.inject(carrier, context=trace.set_span_in_context(trace.NonRecordingSpan(original), Context()))
    before = dict(carrier)
    with trace.use_span(trace.NonRecordingSpan(unrelated)):
        hooks._prepare_carrier(carrier, "sample.execute", "taskiq", "message-1")
    assert carrier["traceparent"] == before["traceparent"]
    assert hooks.ENQUEUED_AT in carrier


@pytest.mark.parametrize("observed", [
    {"OTEL_TRACES_EXPORTER": "none"},
    {"OTEL_TRACES_SAMPLER": "always_off"},
], indirect=True, ids=["metrics-only", "head-sampled-out"])
def test_unrecorded_preparation_preserves_existing_trace_identity(observed):
    _runtime, _reader, exporter = observed
    original = trace.SpanContext(trace_id=1, span_id=2, is_remote=False, trace_flags=trace.TraceFlags(1))
    unrelated = trace.SpanContext(trace_id=3, span_id=4, is_remote=False, trace_flags=trace.TraceFlags(1))
    carrier = {}
    propagate.inject(carrier, context=trace.set_span_in_context(trace.NonRecordingSpan(original), Context()))
    before = dict(carrier)
    with trace.use_span(trace.NonRecordingSpan(unrelated)):
        hooks._prepare_carrier(carrier, "sample.execute", "taskiq", "message-1")
    assert carrier["traceparent"] == before["traceparent"]
    assert hooks.ENQUEUED_AT in carrier
    assert not exporter.get_finished_spans()
