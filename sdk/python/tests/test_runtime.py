"""Exercise business contracts and failure boundaries using the official SDK."""

import asyncio
import importlib
import threading
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

module = importlib.import_module("luma_observe.runtime")


@pytest.fixture
def observed(monkeypatch):
    for name in tuple(module.os.environ):
        if name.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    monkeypatch.setattr(module, "OTLPSpanExporter", lambda **_: exporter)
    monkeypatch.setattr(module, "OTLPMetricExporter", lambda **_: Mock(_preferred_temporality=None, _preferred_aggregation=None))
    monkeypatch.setattr(module, "PeriodicExportingMetricReader", lambda *_a, **_k: reader)
    monkeypatch.setattr(module, "BatchSpanProcessor", lambda exporter, **_: SimpleSpanProcessor(exporter))
    runtime = module.Runtime(module.Configuration(service_name="sample-worker"))
    assert runtime.enabled
    yield runtime, exporter, reader
    runtime.shutdown()


def points(reader, name):
    data = reader.get_metrics_data()
    return [point for resource in data.resource_metrics for scope in resource.scope_metrics
            for metric in scope.metrics if metric.name == name for point in metric.data.data_points]


@pytest.mark.parametrize("switch", ["missing_endpoint", "LUMA_OBSERVE_ENABLED", "OTEL_SDK_DISABLED", "explicit"])
def test_full_disable_creates_no_providers_or_event_thread(monkeypatch, switch):
    for key in tuple(module.os.environ):
        if key.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(key)
    if switch != "missing_endpoint":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    if switch == "LUMA_OBSERVE_ENABLED":
        monkeypatch.setenv(switch, "false")
    if switch == "OTEL_SDK_DISABLED":
        monkeypatch.setenv(switch, "true")
    providers = Mock(side_effect=AssertionError("disabled SDK must not initialize"))
    monkeypatch.setattr(module, "TracerProvider", providers)
    monkeypatch.setattr(module, "MeterProvider", providers)
    runtime = module.Runtime(module.Configuration(enabled=False if switch == "explicit" else None, lifecycle_logs=True))
    with runtime.task("sample.run") as run:
        assert not run.span.is_recording()
    assert not runtime.enabled and runtime._events is None and runtime._writer is None
    providers.assert_not_called()


def test_outcomes_and_user_fields_use_separate_metric_dimensions(observed):
    runtime, exporter, reader = observed
    for outcome in ("succeeded", "retrying", "skipped", "cancelled", "failed"):
        with runtime.task("document.convert", framework="manual", queue="documents",
                          attributes={"document.format": "pdf", "document.id": "one-id"}, queue_wait_seconds=2) as run:
            run.set_outcome(outcome)
            if outcome == "failed":
                run.record_error(ValueError("private document content"))
    counts = points(reader, "task.executions")
    assert {row.attributes["outcome"]: row.value for row in counts} == {
        "succeeded": 1, "retrying": 1, "skipped": 1, "cancelled": 1, "failed": 1,
    }
    assert all(set(row.attributes) == {"task.name", "task.framework", "task.queue", "outcome"} for row in counts)
    assert sum(row.value for row in points(reader, "task.active")) == 0
    assert points(reader, "task.queue_wait")[0].sum == 10
    spans = exporter.get_finished_spans()
    assert spans[-1].attributes["document.id"] == "one-id"
    assert spans[-1].status.status_code is trace.StatusCode.ERROR
    assert "private document content" not in str([span.to_json() for span in spans])


def test_original_exception_and_context_survive_telemetry_errors(observed, monkeypatch):
    runtime, exporter, _reader = observed
    original = ValueError("application failure")
    monkeypatch.setattr(runtime._executions, "add", Mock(side_effect=RuntimeError("metric failure")))
    with runtime.span("caller") as parent:
        with pytest.raises(ValueError) as caught:
            with runtime.task("document.convert"):
                raise original
        assert caught.value is original
        assert trace.get_current_span() is parent
    assert not trace.get_current_span().get_span_context().is_valid
    assert any(span.name == "document.convert" and span.status.status_code is trace.StatusCode.ERROR
               for span in exporter.get_finished_spans())


@pytest.mark.asyncio
async def test_concurrent_attempts_propagate_standard_parent_and_cleanup(observed):
    runtime, exporter, reader = observed
    carrier, started = {}, asyncio.Event()
    active = []
    with runtime.span("publish") as parent:
        module.propagate.inject(carrier)

    async def execute():
        with runtime.task("document.convert", carrier=carrier) as run:
            active.append(run.span.get_span_context().span_id)
            if len(active) == 2:
                started.set()
            await started.wait()
            assert trace.get_current_span() is run.span
        assert not trace.get_current_span().get_span_context().is_valid

    await asyncio.gather(execute(), execute())
    spans = [span for span in exporter.get_finished_spans() if span.name == "document.convert"]
    assert len(set(active)) == 2
    assert all(span.parent.span_id == parent.get_span_context().span_id for span in spans)
    assert sum(point.value for point in points(reader, "task.executions")) == 2


def test_official_sampler_and_resource_environment_are_honored(observed, monkeypatch):
    _runtime, exporter, _reader = observed
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=staging,team=documents")
    runtime = module.Runtime(module.Configuration(service_name="conversion"))
    try:
        with runtime.task("document.convert") as run:
            assert not run.span.is_recording()
        assert runtime.tracer_provider.resource.attributes["service.name"] == "conversion"
        assert runtime.tracer_provider.resource.attributes["deployment.environment.name"] == "staging"
        assert not exporter.get_finished_spans()
    finally:
        runtime.shutdown()


def test_blocked_export_and_saturated_buffer_do_not_wait_in_task(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockedExporter(SpanExporter):
        def export(self, _spans):
            entered.set()
            release.wait(timeout=5)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            release.set()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("LUMA_OBSERVE_ENABLED", raising=False)
    monkeypatch.setattr(module, "OTLPSpanExporter", lambda **_: BlockedExporter())
    runtime = module.Runtime(module.Configuration(queue_size=2, batch_size=1, export_interval_ms=100))

    def execute():
        for _ in range(100):
            with runtime.task("document.convert"):
                pass
        finished.set()

    worker = threading.Thread(target=execute)
    try:
        with runtime.task("prime"):
            pass
        assert entered.wait(timeout=1)
        worker.start()
        assert finished.wait(timeout=2), "task execution waited for export"
    finally:
        release.set()
        worker.join(timeout=2) if worker.ident else None
        runtime.shutdown()


def test_broken_exporter_initialization_preserves_task_execution(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("LUMA_OBSERVE_ENABLED", raising=False)
    monkeypatch.setattr(module, "OTLPSpanExporter", Mock(side_effect=ValueError("invalid SDK configuration")))
    runtime = module.Runtime(module.Configuration())
    assert not runtime.enabled
    original = RuntimeError("business error")
    with pytest.raises(RuntimeError) as caught:
        with runtime.task("document.convert"):
            raise original
    assert caught.value is original


def test_shutdown_bounds_wait_for_stalled_provider(observed, monkeypatch):
    runtime, _exporter, _reader = observed
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stalled_shutdown():
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(runtime.tracer_provider, "shutdown", stalled_shutdown)
    worker = threading.Thread(target=lambda: (runtime.shutdown(timeout=0.01), finished.set()))
    try:
        worker.start()
        assert entered.wait(timeout=1)
        assert finished.wait(timeout=1), "shutdown caller exceeded its bounded wait"
        assert not runtime.enabled
    finally:
        release.set()
        worker.join(timeout=2)
        if runtime._shutdown_thread:
            runtime._shutdown_thread.join(timeout=2)
