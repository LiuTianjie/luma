"""Use real OTel workers and a real fork, with file-only recording exporters."""

from __future__ import annotations

import json
import importlib
import math
import os
import select
import signal
import threading
import traceback

import pytest
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from luma_observe.process import ProcessMetricExporter, ProcessSpanExporter, register_provider_cleanup
import luma_observe as observe


def write_record(destination, **fields):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps({"pid": os.getpid(), **fields}) + "\n").encode())
    finally:
        os.close(descriptor)


class RecordingSpanExporter(SpanExporter):
    def __init__(self, destination):
        self.destination = destination
        self.closed = threading.Event()
        self.last_spans = None

    def export(self, spans):
        self.last_spans = spans
        for span in spans:
            write_record(self.destination, signal="span", instance=span.resource.attributes["service.instance.id"],
                         name=span.name)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis=30_000):
        return True

    def shutdown(self):
        self.closed.set()


class RecordingMetricExporter(MetricExporter):
    def __init__(self, destination, **kwargs):
        super().__init__(**kwargs)
        self.destination = destination
        self.closed = threading.Event()
        self.last_data = None

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        self.last_data = metrics_data
        for resource in metrics_data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        if hasattr(point, "value"):
                            write_record(self.destination, signal="metric", instance=resource.resource.attributes["service.instance.id"],
                                         name=metric.name, value=point.value)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        self.closed.set()


def test_metric_reader_preserves_exporter_temporality_and_aggregation(tmp_path):
    exporter = RecordingMetricExporter(
        tmp_path / "metrics.jsonl",
        preferred_temporality={Counter: AggregationTemporality.DELTA},
        preferred_aggregation={Histogram: ExplicitBucketHistogramAggregation(boundaries=(1.0, 5.0))},
    )
    reader = PeriodicExportingMetricReader(ProcessMetricExporter(exporter), export_interval_millis=math.inf)
    provider = MeterProvider(metric_readers=[reader], resource=Resource({"service.instance.id": "preferences"}),
                             shutdown_on_exit=False)
    try:
        meter = provider.get_meter("test")
        counter, histogram = meter.create_counter("executions"), meter.create_histogram("duration")
        counter.add(1)
        histogram.record(2)
        assert provider.force_flush()
        counter.add(2)
        assert provider.force_flush()
        metrics = {metric.name: metric for resource in exporter.last_data.resource_metrics
                   for scope in resource.scope_metrics for metric in scope.metrics}
        assert metrics["executions"].data.aggregation_temporality == AggregationTemporality.DELTA
        assert metrics["executions"].data.data_points[0].value == 2
        assert metrics["duration"].data.data_points[0].explicit_bounds == (1.0, 5.0)
    finally:
        provider.shutdown()
    assert exporter.closed.is_set()


def make_providers(destination, instance):
    resource = Resource({"service.name": "sample-worker", "service.instance.id": instance})
    spans, metrics = RecordingSpanExporter(destination), RecordingMetricExporter(destination)
    span_exporter, metric_exporter = ProcessSpanExporter(spans), ProcessMetricExporter(metrics)
    trace_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    trace_provider.add_span_processor(BatchSpanProcessor(span_exporter, schedule_delay_millis=60_000))
    metric_provider = MeterProvider(
        resource=resource, shutdown_on_exit=False,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60_000)],
    )
    register_provider_cleanup(trace_provider, metric_provider)
    return trace_provider, metric_provider, spans, metrics, span_exporter, metric_exporter


def run_in_child(callback, destination):
    """Run a real fork with a completion pipe and a bounded parent wait."""
    read_pipe, write_pipe = os.pipe()
    child_pid = None
    try:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_pipe)
            exit_code = 0
            try:
                callback()
            except BaseException:
                write_record(destination, signal="child-error", error=traceback.format_exc())
                exit_code = 1
            finally:
                os.write(write_pipe, b"done")
                os.close(write_pipe)
                os._exit(exit_code)
        os.close(write_pipe)
        write_pipe = None
        ready, _, _ = select.select([read_pipe], [], [], 10)
        assert ready, "child did not finish within the fork cleanup deadline"
        assert os.read(read_pipe, 16) == b"done"
        finished_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert os.waitstatus_to_exitcode(status) == 0, destination.read_text()
        return finished_pid
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        os.close(read_pipe)
        if write_pipe is not None:
            os.close(write_pipe)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_drops_inherited_telemetry_closes_workers_and_preserves_parent(tmp_path):
    destination = tmp_path / "fork.jsonl"
    providers = make_providers(destination, "parent-instance")
    parent_trace, parent_meter, inherited_spans, inherited_metrics, span_exporter, metric_exporter = providers
    tracer = parent_trace.get_tracer("test")
    counter = parent_meter.get_meter("test").create_counter("executions")
    with tracer.start_as_current_span("parent-before"):
        pass
    counter.add(1)
    assert parent_trace.force_flush() and parent_meter.force_flush()
    parent_pid = os.getpid()

    def child_work():
        # Fence both direct calls and inherited background readers.
        assert span_exporter.export(inherited_spans.last_spans) is SpanExportResult.FAILURE
        assert metric_exporter.export(inherited_metrics.last_data) is MetricExportResult.FAILURE
        assert span_exporter.force_flush() is False
        assert metric_exporter.force_flush() is False
        assert inherited_spans.closed.wait(timeout=3), "inherited span worker did not close"
        assert inherited_metrics.closed.wait(timeout=3), "inherited metric worker did not close"
        with tracer.start_as_current_span("inherited-child-span"):
            pass
        counter.add(100)
        fresh_trace, fresh_meter, *_ = make_providers(destination, "child-instance")
        try:
            with fresh_trace.get_tracer("test").start_as_current_span("child"):
                pass
            fresh_meter.get_meter("test").create_counter("executions").add(7)
            assert fresh_trace.force_flush() and fresh_meter.force_flush()
        finally:
            fresh_trace.shutdown()
            fresh_meter.shutdown()

    try:
        run_in_child(child_work, destination)
        assert not inherited_spans.closed.is_set() and not inherited_metrics.closed.is_set()
        with tracer.start_as_current_span("parent-after"):
            pass
        counter.add(2)
        assert parent_trace.force_flush() and parent_meter.force_flush()
    finally:
        parent_trace.shutdown()
        parent_meter.shutdown()

    records = [json.loads(line) for line in destination.read_text().splitlines()]
    spans = [record for record in records if record["signal"] == "span"]
    assert {record["name"] for record in spans} == {"parent-before", "parent-after", "child"}
    assert all(record["instance"] == ("parent-instance" if record["pid"] == parent_pid else "child-instance")
               for record in records)
    assert any(record["signal"] == "metric" and record["instance"] == "child-instance" and record["value"] == 7
               for record in records)
    assert any(record["signal"] == "metric" and record["instance"] == "parent-instance" and record["value"] == 3
               for record in records)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_predeclared_counter_rebinds_after_configure_and_fork_without_retained_runtime(tmp_path, monkeypatch):
    module = importlib.import_module("luma_observe.runtime")
    for name in tuple(os.environ):
        if name.startswith(("OTEL_", "LUMA_OBSERVE_")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(module, "_runtime", None)
    monkeypatch.setattr(module, "_configuration", module.Configuration())
    destination = tmp_path / "runtime-fork.jsonl"
    span_exporters, metric_exporters = [], []

    def create_spans(**_):
        span_exporters.append(RecordingSpanExporter(destination))
        return span_exporters[-1]

    def create_metrics(**_):
        metric_exporters.append(RecordingMetricExporter(destination))
        return metric_exporters[-1]

    monkeypatch.setattr(module, "OTLPSpanExporter", create_spans)
    monkeypatch.setattr(module, "OTLPMetricExporter", create_metrics)
    # A module-level handle can already have been used while telemetry was off.
    completed = observe.counter("predeclared.executions")
    completed.add(123)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    # Deliberately retain no Runtime/tracer/meter references. The child reset of
    # the module singleton would otherwise collect the old TracerProvider before
    # its later cleanup callback could resolve a weak reference.
    observe.configure(service_name="sample-worker")
    with observe.span("sdk-parent-before"):
        pass
    observe.counter("baseline.executions").add(1)
    assert observe.get_runtime().tracer_provider.force_flush()
    assert observe.get_runtime().meter_provider.force_flush()
    inherited_spans, inherited_metrics = span_exporters[0], metric_exporters[0]

    def child_work():
        assert inherited_spans.closed.wait(timeout=3), "unretained inherited span provider was not cleaned up"
        assert inherited_metrics.closed.wait(timeout=3), "unretained inherited metric provider was not cleaned up"
        completed.add(7)
        with observe.span("sdk-child"):
            pass
        assert observe.get_runtime().pid == os.getpid()
        assert observe.get_runtime().tracer_provider.force_flush()
        assert observe.get_runtime().meter_provider.force_flush()
        observe.shutdown()

    try:
        child_pid = run_in_child(child_work, destination)
        assert not inherited_spans.closed.is_set() and not inherited_metrics.closed.is_set()
        completed.add(3)
        assert observe.get_runtime().meter_provider.force_flush()
    finally:
        observe.shutdown()

    records = [json.loads(line) for line in destination.read_text().splitlines()]
    assert all(record["instance"].rsplit(":", 2)[1] == str(record["pid"]) for record in records)
    points = [record for record in records if record["signal"] == "metric" and record["name"] == "predeclared.executions"]
    assert {(point["pid"], point["value"]) for point in points} == {(os.getpid(), 3), (child_pid, 7)}
    assert {record["name"] for record in records if record["signal"] == "span"} == {"sdk-parent-before", "sdk-child"}
