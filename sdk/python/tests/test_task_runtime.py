"""Current execution context, worker liveness and bounded flush contracts."""

import asyncio
from contextvars import copy_context
import importlib
import os
import select
import signal
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import luma_observe as observe

module = importlib.import_module("luma_observe.runtime")


@pytest.fixture
def observed(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    monkeypatch.setattr(module, "OTLPSpanExporter", lambda **_: exporter)
    monkeypatch.setattr(module, "OTLPMetricExporter", lambda **_: Mock(_preferred_temporality=None, _preferred_aggregation=None))
    monkeypatch.setattr(module, "PeriodicExportingMetricReader", lambda *_a, **_k: reader)
    monkeypatch.setattr(module, "BatchSpanProcessor", lambda exporter, **_: SimpleSpanProcessor(exporter))
    config = module.Configuration(service_name="sample-worker")
    runtime = module.Runtime(config)
    monkeypatch.setattr(module, "_runtime", runtime)
    monkeypatch.setattr(module, "_configuration", config)
    assert runtime.enabled
    yield runtime, exporter, reader
    runtime.shutdown()


def points(reader, name):
    data = reader.get_metrics_data()
    return [point for resource in data.resource_metrics for scope in resource.scope_metrics
            for metric in scope.metrics if metric.name == name for point in metric.data.data_points]


def test_current_task_restores_nested_context_and_keeps_business_fields_off_metrics(observed):
    runtime, exporter, reader = observed
    assert observe.current_task() is None
    with runtime.task("outer") as outer:
        assert observe.current_task() is outer
        with runtime.task("convert", attributes={"document.id": "private-id"}) as run:
            assert observe.current_task() is run
            run.set_outcome("skipped")
            run.set_business_outcome("requires_review")
            run.business_status = "waiting_for_editor"
            run.set_framework_outcome("succeeded")
            assert run.outcome == "skipped"
            run.set_outcome("arbitrary-domain-value")
            assert run.outcome == "skipped"
        assert observe.current_task() is outer
    assert observe.current_task() is None
    finished = next(span for span in exporter.get_finished_spans() if span.name == "convert")
    assert finished.attributes["task.business.outcome"] == "requires_review"
    assert finished.attributes["task.business.status"] == "waiting_for_editor"
    assert finished.attributes["task.outcome"] == "skipped"
    for point in points(reader, "task.executions"):
        assert set(point.attributes) == {"task.name", "task.framework", "task.queue", "outcome"}


def test_business_success_cannot_mask_framework_failure_and_original_exception(observed):
    runtime, exporter, _reader = observed
    original = ValueError("application-owned error text")
    with pytest.raises(ValueError) as caught:
        with runtime.task("failed-attempt") as run:
            run.business_outcome = "business_processed"
            run.set_business_status("x" * 300)
            run.set_outcome("skipped")
            raise original
    assert caught.value is original
    span = exporter.get_finished_spans()[0]
    assert span.attributes["task.outcome"] == "failed"
    assert span.attributes["task.business.outcome"] == "business_processed"
    assert len(span.attributes["task.business.status"]) == 256
    assert span.status.status_code is trace.StatusCode.ERROR
    assert observe.current_task() is None


@pytest.mark.asyncio
async def test_current_task_isolated_between_async_executions_and_threads(observed):
    runtime, _exporter, _reader = observed
    barrier = asyncio.Event()
    attempts = []

    async def execute(name):
        with runtime.task(name) as run:
            attempts.append(run)
            if len(attempts) == 2:
                barrier.set()
            await barrier.wait()
            assert observe.current_task() is run
            found = []
            thread = threading.Thread(target=lambda: found.append(observe.current_task()))
            thread.start()
            thread.join(timeout=1)
            assert found == [None]
        assert observe.current_task() is None

    await asyncio.gather(execute("one"), execute("two"))
    assert attempts[0] is not attempts[1]


def test_finished_private_attempt_is_not_visible_in_activated_business_context(observed):
    runtime, _exporter, _reader = observed
    private = copy_context()
    manager = runtime.task("private-lifetime")
    run = private.run(manager.__enter__)
    assert observe.current_task() is None
    token = module._activate_task(run)
    try:
        assert observe.current_task() is run
        private.run(manager.__exit__, None, None, None)
        assert observe.current_task() is None
    finally:
        module._deactivate_task(token)


def test_worker_heartbeat_idle_oldest_and_overflow_distinguish_missing_data(observed, monkeypatch):
    runtime, _exporter, reader = observed
    clock = {"wall": 1000.0, "monotonic": 10.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]))
    monkeypatch.setattr(module, "_MAX_ACTIVE_RUNS", 1)
    assert points(reader, "worker.heartbeat")[0].value == 1000
    assert points(reader, "task.oldest_active_age") == []
    with runtime.task("long-running", queue="documents", framework="manual"):
        clock.update(wall=900.0, monotonic=18.0)
        assert points(reader, "worker.heartbeat")[0].value == 900
        oldest = points(reader, "task.oldest_active_age")
        assert oldest[0].value == 8
        assert dict(oldest[0].attributes) == {"task.name": "long-running", "task.queue": "documents", "task.framework": "manual"}
        with runtime.task("overflow"):
            assert points(reader, "task.oldest_active_age") == []
            assert points(reader, "task.active_tracking_overflow")[0].value == 1
            assert len(runtime._active_runs) == 1
        assert points(reader, "task.oldest_active_age")[0].value == 8
        assert points(reader, "task.active_tracking_overflow")[0].value == 0
    assert points(reader, "task.oldest_active_age") == []
    assert sum(point.value for point in points(reader, "task.active")) == 0
    runtime.shutdown()
    assert runtime._observe_heartbeat(None) == []
    assert runtime._observe_oldest(None) == []


def test_oldest_age_groups_active_attempts_by_name_queue_and_framework(observed, monkeypatch):
    runtime, _exporter, reader = observed
    clock = {"monotonic": 10.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 1000, monotonic=lambda: clock["monotonic"]))
    managers = []

    def start(name, queue, framework, at):
        clock["monotonic"] = at
        manager = runtime.task(name, queue=queue, framework=framework)
        execution_context = copy_context()
        execution_context.run(manager.__enter__)
        managers.append((manager, execution_context))

    def ages():
        return {(point.attributes["task.name"], point.attributes["task.queue"], point.attributes["task.framework"]): point.value
                for point in points(reader, "task.oldest_active_age")}

    try:
        start("render", "documents", "taskiq", 10)
        start("render", "documents", "taskiq", 12)
        start("render", "priority", "taskiq", 13)
        start("index", "documents", "taskiq", 14)
        start("render", "documents", "celery", 15)
        clock["monotonic"] = 20
        assert ages() == {("render", "documents", "taskiq"): 10,
                          ("render", "priority", "taskiq"): 7,
                          ("index", "documents", "taskiq"): 6,
                          ("render", "documents", "celery"): 5}
        manager, execution_context = managers.pop(0)
        execution_context.run(manager.__exit__, None, None, None)
        assert ages()[("render", "documents", "taskiq")] == 8
        manager, execution_context = managers.pop()
        execution_context.run(manager.__exit__, None, None, None)
        assert ("render", "documents", "celery") not in ages()
        # Completed label sets are not retained in an ever-growing seen-key cache.
        assert len(runtime._active_runs) == 3
    finally:
        for manager, execution_context in reversed(managers):
            execution_context.run(manager.__exit__, None, None, None)
    assert ages() == {}
    assert runtime._active_runs == {}


def test_flush_exports_and_keeps_runtime_usable(observed):
    runtime, exporter, reader = observed
    with runtime.task("before-flush"):
        pass
    assert observe.force_flush(timeout=1)
    assert runtime.enabled and not runtime._closed.is_set()
    with runtime.task("after-flush"):
        pass
    assert runtime.force_flush(timeout=1)
    assert len(exporter.get_finished_spans()) == 2
    assert sum(point.value for point in points(reader, "task.executions")) == 2


def test_stalled_flush_wait_is_bounded_and_does_not_accumulate_workers(observed, monkeypatch):
    runtime, _exporter, _reader = observed
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked_flush(**_kwargs):
        calls.append(1)
        entered.set()
        release.wait(timeout=3)
        return True

    monkeypatch.setattr(runtime.tracer_provider, "force_flush", blocked_flush)
    finished = threading.Event()

    def check():
        assert not runtime.force_flush(timeout=0.01)
        assert not runtime.force_flush(timeout=0.01)
        finished.set()

    caller = threading.Thread(target=check)
    try:
        caller.start()
        assert entered.wait(timeout=1)
        assert finished.wait(timeout=1), "flush caller waited for the blocked exporter"
        assert calls == [1]
        assert runtime.enabled and not runtime._closed.is_set()
    finally:
        release.set()
        caller.join(timeout=2)
        assert runtime._flush_state["done"].wait(timeout=2)


def test_concurrent_flush_covers_spans_added_after_the_previous_trace_flush(observed, monkeypatch):
    _runtime, _exporter, _reader = observed
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setattr(module, "OTLPSpanExporter", lambda **_: exporter)
    monkeypatch.setattr(module, "BatchSpanProcessor", BatchSpanProcessor)
    runtime = module.Runtime(module.Configuration(export_interval_ms=60000))
    meter_entered, release_meter, second_waiting = threading.Event(), threading.Event(), threading.Event()
    meter_calls = []

    def meter_flush(**_kwargs):
        meter_calls.append(1)
        if len(meter_calls) == 1:
            meter_entered.set()
            release_meter.wait(timeout=3)
        return True

    runtime.meter_provider = SimpleNamespace(force_flush=meter_flush, shutdown=lambda **_: None)
    results = []
    first = threading.Thread(target=lambda: results.append(runtime.force_flush(timeout=2)))
    second = threading.Thread(target=lambda: results.append(runtime.force_flush(timeout=2)))
    try:
        with runtime.span("before-first-flush"):
            pass
        first.start()
        assert meter_entered.wait(timeout=1)
        assert [span.name for span in exporter.get_finished_spans()] == ["before-first-flush"]
        with runtime.span("before-second-flush"):
            pass

        class ObserveWait:
            def __init__(self, event):
                self.event = event

            def is_set(self):
                return self.event.is_set()

            def set(self):
                self.event.set()

            def wait(self, timeout=None):
                if threading.current_thread() is second:
                    second_waiting.set()
                return self.event.wait(timeout)

        # Wait deterministically until call B is sharing A's pending operation,
        # whose trace flush is already complete while its metric flush stalls.
        runtime._flush_state["done"] = ObserveWait(runtime._flush_state["done"])
        second.start()
        assert second_waiting.wait(timeout=1)
        release_meter.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert results == [True, True]
        assert [span.name for span in exporter.get_finished_spans()] == ["before-first-flush", "before-second-flush"]
        assert len(meter_calls) == 2
    finally:
        release_meter.set()
        first.join(timeout=3) if first.ident else None
        second.join(timeout=3) if second.ident else None
        runtime.shutdown()


@pytest.mark.parametrize("switch", ["none", "environment", "otel", "configured"])
def test_explicit_disable_and_flush_queries_do_not_initialize_runtime(monkeypatch, switch):
    for name in tuple(os.environ):
        if name.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(name)
    if switch == "environment":
        monkeypatch.setenv("LUMA_OBSERVE_ENABLED", "false")
    if switch == "otel":
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(module, "_configuration", module.Configuration(enabled=False if switch == "configured" else None))
    monkeypatch.setattr(module, "_runtime", None)
    constructor = Mock(side_effect=AssertionError("read-only query must not initialize providers"))
    monkeypatch.setattr(module, "Runtime", constructor)
    assert observe.telemetry_disabled() is (switch != "none")
    assert observe.force_flush()
    constructor.assert_not_called()


def test_explicit_root_context_and_link_do_not_inherit_ambient_parent(observed):
    runtime, exporter, _reader = observed
    with runtime.span("producer") as producer:
        carrier = {}
        module.propagate.inject(carrier)
        with runtime.task("execution", carrier=carrier, context=Context(),
                          links=[trace.Link(producer.get_span_context())]) as run:
            assert run.span.get_span_context().trace_id != producer.get_span_context().trace_id
    execution = next(span for span in exporter.get_finished_spans() if span.name == "execution")
    assert execution.parent is None
    assert execution.links[0].context == producer.get_span_context()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_does_not_inherit_current_attempt_or_liveness(observed):
    runtime, _exporter, _reader = observed
    read_fd, write_fd = os.pipe()
    with runtime.task("parent-attempt") as parent:
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                healthy = (observe.current_task() is None and runtime._observe_heartbeat(None) == []
                           and runtime._observe_oldest(None) == [] and not runtime.force_flush())
                os.write(write_fd, b"ok" if healthy else b"inherited")
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], 3)
            assert ready, "child did not finish within the process-isolation deadline"
            assert os.read(read_fd, 64) == b"ok"
            assert observe.current_task() is parent
        finally:
            os.close(read_fd)
            if not ready:
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_progress_stage_last_times_and_business_outcome(observed, monkeypatch):
    runtime, exporter, reader = observed
    clock = {"wall": 1000.0, "monotonic": 10.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]))
    labels = {"task.name": "render", "task.framework": "manual", "task.queue": "documents"}
    with runtime.task("render", queue="documents") as run:
        clock.update(monotonic=40.0)
        assert points(reader, "task.no_progress_age")[0].value == 30
        with run.stage("render.pages", attributes={"pages": 3}):
            clock.update(monotonic=55.0)
            assert points(reader, "task.no_progress_age")[0].value == 15
        clock.update(monotonic=60.0)
        assert points(reader, "task.no_progress_age")[0].value == 5
        run.mark_progress()
        assert points(reader, "task.no_progress_age")[0].value == 0
        run.business_outcome = "needs_review"
        run.business_outcome = "failed"
        clock.update(wall=1100.0)
    assert points(reader, "task.no_progress_age") == []
    started = points(reader, "task.last_started")
    finished = points(reader, "task.last_finished")
    assert [(dict(p.attributes), p.value) for p in started] == [(labels, 1000.0)]
    assert [(dict(p.attributes), p.value) for p in finished] == [({**labels, "outcome": "succeeded"}, 1100.0)]
    business = points(reader, "task.business_outcomes")
    assert [(dict(p.attributes), p.value) for p in business] == [({**labels, "task.business.outcome": "failed"}, 1)]
    stage = next(span for span in exporter.get_finished_spans() if span.name == "render.pages")
    root = next(span for span in exporter.get_finished_spans() if span.name == "render")
    assert stage.parent.span_id == root.context.span_id
    assert stage.attributes["pages"] == 3 and stage.attributes["task.name"] == "render"
    assert [event.name for event in root.events] == ["task.stage"]


def test_publication_counts_outcomes_and_preserves_exception(observed):
    runtime, exporter, reader = observed
    with runtime.publication("render", framework="taskiq", queue="documents", task_id="job-1") as span:
        assert span.is_recording()
    failure = ConnectionError("broker unavailable")
    with pytest.raises(ConnectionError) as caught:
        with observe.publication("render", framework="taskiq", queue="documents"):
            raise failure
    assert caught.value is failure
    counts = {p.attributes["outcome"]: p.value for p in points(reader, "task.publications")}
    assert counts == {"sent": 1, "failed": 1}
    spans = [span for span in exporter.get_finished_spans() if span.name == "task.publish/render"]
    assert [span.kind for span in spans] == [trace.SpanKind.PRODUCER] * 2
    assert spans[0].attributes["messaging.message.id"] == "job-1"
    assert spans[1].status.status_code is trace.StatusCode.ERROR


def test_registration_dropped_counters_and_lifecycle_identity(monkeypatch, caplog):
    for name in tuple(os.environ):
        if name.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    reader = InMemoryMetricReader()
    monkeypatch.setattr(module, "OTLPSpanExporter", lambda **_: InMemorySpanExporter())
    monkeypatch.setattr(module, "OTLPMetricExporter", lambda **_: Mock(_preferred_temporality=None, _preferred_aggregation=None))
    monkeypatch.setattr(module, "PeriodicExportingMetricReader", lambda *_a, **_k: reader)
    monkeypatch.setattr(module, "_MAX_TASK_KEYS", 2)
    monkeypatch.setattr(module, "_instrumentation_errors", [0])
    runtime = module.Runtime(module.Configuration(service_name="sample-worker", lifecycle_logs=True))
    try:
        runtime.register_process("taskiq", "worker", "documents")
        runtime.register_tasks(["render", "index", "beyond-bound"], "taskiq", "documents")
        assert {p.attributes["task.name"] for p in points(reader, "task.registered")} == {"render", "index"}
        roles = points(reader, "task.process.started")
        assert [dict(p.attributes) for p in roles] == [{"task.framework": "taskiq", "task.role": "worker", "task.queue": "documents"}]
        assert roles[0].value == runtime.started_at
        module._safe(lambda: 1 / 0)
        dropped = {p.attributes["reason"]: p.value for p in points(reader, "observe.sdk.dropped")}
        assert dropped == {"lifecycle_logs": 0, "instrumentation_error": 1}
        with caplog.at_level("INFO", logger="luma_observe.events"):
            with runtime.task("render"):
                pass
            assert runtime.force_flush(timeout=2)
        records = [module.json.loads(record.getMessage()) for record in caplog.records if record.name == "luma_observe.events"]
        assert [record["event"] for record in records] == ["task.started", "task.finished"]
        assert {record["service.instance.id"] for record in records} == {runtime.instance_id}
    finally:
        runtime.shutdown()
