"""Bounded application telemetry built on official OpenTelemetry providers."""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Mapping, Sequence

from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import Configuration
from .process import ProcessMetricExporter, ProcessSpanExporter, register_provider_cleanup


_EVENT_LOG = logging.getLogger("luma_observe.events")
_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 7200)
_OUTCOMES = frozenset(("succeeded", "failed", "retrying", "skipped", "cancelled"))
_MAX_ACTIVE_RUNS = 8192
_OVERFLOW = object()
_current_task = ContextVar("luma_observe_current_task", default=None)


def _safe(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except Exception:
        return None


def _attributes(values: Mapping | None, limit: int = 32) -> dict:
    """Copy explicitly supplied bounded scalar fields, suitable for OTel and JSON."""
    result = {}
    for key, value in (values or {}).items():
        if len(result) >= limit:
            break
        if not isinstance(key, str) or len(key) > 128:
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:256]
    return result


class SafeInstrument:
    """A small failure boundary for application-owned counters and histograms."""

    def __init__(self, instrument=None, *, factory=None):
        self._instrument = instrument
        self._factory = factory
        self._runtime = None

    def _invoke(self, method, amount, attributes, kwargs):
        try:
            if self._factory is not None:
                runtime = get_runtime()
                if runtime is not self._runtime:
                    self._instrument = self._factory(runtime)
                    self._runtime = runtime
            getattr(self._instrument, method)(amount, attributes=attributes, **kwargs)
        except Exception:
            pass

    def add(self, amount, attributes=None, **kwargs):
        self._invoke("add", amount, attributes, kwargs)

    def record(self, amount, attributes=None, **kwargs):
        self._invoke("record", amount, attributes, kwargs)


class TaskRun:
    """One execution attempt; business result fields stay on its span and logs."""

    def __init__(self, span: trace.Span, outcome: str = "succeeded", *, _runtime=None, _fields=None):
        self.span = span
        self.pid = os.getpid()
        self._finished = False
        self._runtime = _runtime
        self._fields = dict(_fields or {})
        self._outcome = outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "succeeded"
        self._explicit_outcome = False

    @property
    def outcome(self) -> str:
        return self._outcome

    @outcome.setter
    def outcome(self, value: str) -> None:
        if isinstance(value, str) and value in _OUTCOMES:
            self._outcome = value
            self._explicit_outcome = True

    @property
    def business_outcome(self) -> str | None:
        return self._fields.get("task.business.outcome")

    @business_outcome.setter
    def business_outcome(self, value: str) -> None:
        self._business_field("task.business.outcome", value)

    @property
    def business_status(self) -> str | None:
        return self._fields.get("task.business.status")

    @business_status.setter
    def business_status(self, value: str) -> None:
        self._business_field("task.business.status", value)

    def _business_field(self, name: str, value: str) -> None:
        if not isinstance(value, str) or self._finished or self.pid != os.getpid():
            return
        self._fields[name] = value[:256]
        _safe(self.span.set_attribute, name, value[:256])

    def set_business_outcome(self, value: str) -> None:
        self.business_outcome = value

    def set_business_status(self, value: str) -> None:
        self.business_status = value

    def record_error(self, exception: BaseException) -> None:
        """Keep failure types available while payloads stay with the application."""
        if self._finished or self.pid != os.getpid():
            return
        self._fields["error.type"] = type(exception).__name__
        _safe(self.span.set_attribute, "error.type", type(exception).__name__)
        _safe(self.span.set_status, trace.StatusCode.ERROR)
        _safe(self.span.add_event, "exception", {"exception.type": type(exception).__name__})

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome

    def set_framework_outcome(self, outcome: str) -> None:
        """Keep explicit application classification unless the framework failed."""
        if isinstance(outcome, str) and outcome in _OUTCOMES:
            if outcome != "succeeded" or not self._explicit_outcome:
                self._outcome = outcome

    def add_event(self, name: str, attributes: Mapping | None = None) -> None:
        """Record a stage in the span and optional asynchronous lifecycle log."""
        if self._finished or self.pid != os.getpid():
            return
        fields = _safe(_attributes, attributes) or {}
        _safe(self.span.add_event, name, fields)
        if self._runtime is not None:
            _safe(self._runtime.emit, name, {**self._fields, **fields}, self.span)


def current_task() -> TaskRun | None:
    """Return this execution context's live attempt, without initializing the SDK."""
    run = _current_task.get()
    return run if run is not None and run.pid == os.getpid() and not run._finished else None


def _activate_task(run: TaskRun):
    """Let adapters activate a privately owned attempt in the business context."""
    return _current_task.set(run)


def _deactivate_task(token) -> None:
    _current_task.reset(token)


class Runtime:
    def __init__(self, configuration: Configuration):
        self.configuration = configuration
        self.pid = os.getpid()
        self.enabled = False
        self.initialization_error = None
        self.tracer_provider = trace.NoOpTracerProvider()
        self.meter_provider = metrics.NoOpMeterProvider()
        self._closed = threading.Event()
        self._writer = None
        self._events = None
        self._shutdown_thread = None
        self._flush_lock = threading.Lock()
        self._flush_state = None
        self._activity_lock = threading.Lock()
        self._active_runs = {}
        self._tracking_overflow = 0
        self.dropped_events = 0
        created = []
        try:
            configuration.validate()
            if not configuration.active():
                return
            traces_on = configuration.signal_active("traces")
            metrics_on = configuration.signal_active("metrics")
            if not (traces_on or metrics_on or configuration.lifecycle_logs):
                return
            resource_values = dict(configuration.resource_attributes or {})
            if configuration.service_name:
                resource_values["service.name"] = configuration.service_name
            resource_values.setdefault("service.instance.id", f"{os.getenv('NOMAD_ALLOC_ID', 'process')}:{self.pid}:{uuid.uuid4().hex[:8]}")
            resource = Resource.create(resource_values)
            if traces_on:
                provider = TracerProvider(resource=resource, shutdown_on_exit=False,
                                         span_limits=SpanLimits(max_attributes=32, max_events=16, max_links=8, max_attribute_length=256))
                created.append(provider)
                provider.add_span_processor(BatchSpanProcessor(
                    ProcessSpanExporter(OTLPSpanExporter(timeout=configuration.export_timeout_seconds)),
                    max_queue_size=configuration.queue_size, max_export_batch_size=configuration.batch_size,
                    schedule_delay_millis=configuration.export_interval_ms,
                    export_timeout_millis=configuration.export_timeout_seconds * 1000,
                ))
                self.tracer_provider = provider
            if metrics_on:
                reader = PeriodicExportingMetricReader(
                    ProcessMetricExporter(OTLPMetricExporter(timeout=configuration.export_timeout_seconds)),
                    export_interval_millis=configuration.metric_interval_ms,
                    export_timeout_millis=configuration.export_timeout_seconds * 1000,
                )
                # Keep a handle before constructing the provider so failed setup
                # also releases an already-started periodic reader.
                created.append(reader)
                provider = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False,
                                         views=[View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(_BUCKETS))
                                                for name in ("task.duration", "task.queue_wait")])
                created.remove(reader)
                created.append(provider)
                self.meter_provider = provider
            register_provider_cleanup(*created)
            meter = self.get_meter()
            self._executions = meter.create_counter("task.executions", unit="1")
            self._duration = meter.create_histogram("task.duration", unit="s")
            self._queue_wait = meter.create_histogram("task.queue_wait", unit="s")
            self._active = meter.create_up_down_counter("task.active", unit="1")
            meter.create_observable_gauge("worker.heartbeat", unit="s", callbacks=[self._observe_heartbeat],
                                          description="Exporter process collection time, not business progress")
            meter.create_observable_gauge("task.oldest_active_age", unit="s", callbacks=[self._observe_oldest],
                                          description="Oldest active attempt age per task name, queue and framework; idle/overflow are absent")
            meter.create_observable_gauge("task.active_tracking_overflow", unit="", callbacks=[self._observe_overflow])
            if configuration.lifecycle_logs:
                self._events = queue.Queue(maxsize=256)
                self._writer = threading.Thread(target=self._write_events, name="luma-observe-events", daemon=True)
                self._writer.start()
            self.enabled = True
        except Exception as error:
            self.initialization_error = type(error).__name__
            self.tracer_provider = trace.NoOpTracerProvider()
            self.meter_provider = metrics.NoOpMeterProvider()
            self._closed.set()
            # Official exporters may be supplied with custom transports. Cleanup
            # stays outside application work even if such a transport blocks.
            if created:
                threading.Thread(target=lambda: [_safe(value.shutdown) for value in reversed(created)], daemon=True).start()

    def get_tracer(self, name: str = "luma.observe"):
        return self.tracer_provider.get_tracer(name)

    def get_meter(self, name: str = "luma.observe"):
        return self.meter_provider.get_meter(name)

    def counter(self, name: str, *, unit: str = "1", description: str = "") -> SafeInstrument:
        instrument = _safe(self.get_meter().create_counter, name, unit=unit, description=description)
        if instrument is None:
            instrument = metrics.NoOpMeterProvider().get_meter("luma.observe").create_counter(name)
        return SafeInstrument(instrument)

    def histogram(self, name: str, *, unit: str = "s", description: str = "") -> SafeInstrument:
        instrument = _safe(self.get_meter().create_histogram, name, unit=unit, description=description)
        if instrument is None:
            instrument = metrics.NoOpMeterProvider().get_meter("luma.observe").create_histogram(name)
        return SafeInstrument(instrument)

    def _live(self) -> bool:
        return self.enabled and not self._closed.is_set() and self.pid == os.getpid()

    def _track_start(self, started: float, labels: Mapping):
        key = tuple(labels.items())
        hash(key)
        with self._activity_lock:
            if len(self._active_runs) >= _MAX_ACTIVE_RUNS:
                self._tracking_overflow += 1
                return _OVERFLOW
            marker = object()
            self._active_runs[marker] = (started, key)
            return marker

    def _track_end(self, marker) -> None:
        if marker is None or self.pid != os.getpid():
            return
        with self._activity_lock:
            if marker is _OVERFLOW:
                self._tracking_overflow -= 1
            else:
                self._active_runs.pop(marker, None)

    def _observe_heartbeat(self, _options):
        return [metrics.Observation(time.time())] if self._live() else []

    def _observe_oldest(self, _options):
        if not self._live():
            return []
        with self._activity_lock:
            if self._tracking_overflow:
                return []
            oldest = {}
            for started, key in self._active_runs.values():
                oldest[key] = min(oldest.get(key, started), started)
        now = time.monotonic()
        # Only live keys are retained. Dashboard queries must join task.active
        # on the same labels to exclude a finished task's old stored sample.
        return [metrics.Observation(max(0.0, now - started), attributes=dict(key))
                for key, started in oldest.items()]

    def _observe_overflow(self, _options):
        if not self._live():
            return []
        with self._activity_lock:
            count = self._tracking_overflow
        return [metrics.Observation(count)]

    @contextmanager
    def span(self, name: str, *, attributes: Mapping | None = None, carrier: Mapping | None = None,
             kind: trace.SpanKind = trace.SpanKind.INTERNAL, context: Context | None = None,
             links: Sequence[trace.Link] | None = None):
        """Activate an official span while preserving application exceptions."""
        span, activation = trace.INVALID_SPAN, None
        if self.enabled and not self._closed.is_set() and self.pid == os.getpid():
            try:
                parent = context if context is not None else propagate.extract(carrier) if carrier is not None else None
                span = self.get_tracer().start_span(name, context=parent, kind=kind, attributes=_attributes(attributes), links=links)
                activation = trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False)
                activation.__enter__()
            except Exception:
                _safe(span.end)
                span, activation = trace.INVALID_SPAN, None
        try:
            yield span
        except BaseException as exception:
            _safe(span.set_attribute, "error.type", type(exception).__name__)
            _safe(span.set_status, trace.StatusCode.ERROR)
            _safe(span.add_event, "exception", {"exception.type": type(exception).__name__})
            raise
        finally:
            if activation is not None:
                _safe(activation.__exit__, None, None, None)
            _safe(span.end)

    @contextmanager
    def task(self, name: str, *, framework: str = "manual", queue: str = "", attributes: Mapping | None = None,
             carrier: Mapping | None = None, queue_wait_seconds: float | None = None,
             context: Context | None = None, links: Sequence[trace.Link] | None = None):
        """Observe an execution attempt with fixed metric dimensions and explicit span fields."""
        labels = {"task.name": name, "task.framework": framework, "task.queue": queue}
        fields = {**(_safe(_attributes, attributes, limit=24) or {}), **labels}
        started = time.monotonic()
        with self.span(name, attributes=fields, carrier=carrier, context=context, links=links,
                       kind=trace.SpanKind.CONSUMER) as span:
            run = TaskRun(span=span, _runtime=self, _fields=fields)
            activation = _activate_task(run)
            marker = None
            if self._live():
                marker = _safe(self._track_start, started, labels)
                _safe(self._active.add, 1, labels)
                if isinstance(queue_wait_seconds, (int, float)) and 0 <= queue_wait_seconds <= 31 * 86400:
                    _safe(self._queue_wait.record, queue_wait_seconds, labels)
                _safe(self.emit, "task.started", fields, span)
            try:
                yield run
            except BaseException as exception:
                outcome = "cancelled" if isinstance(exception, (KeyboardInterrupt, SystemExit)) or type(exception).__name__ == "CancelledError" else "failed"
                run.set_framework_outcome(outcome)
                run._fields["error.type"] = type(exception).__name__
                raise
            finally:
                run._finished = True
                _safe(_deactivate_task, activation)
                _safe(self._track_end, marker)
                if self._live():
                    outcome = run.outcome if run.outcome in _OUTCOMES else "failed"
                    elapsed = time.monotonic() - started
                    _safe(span.set_attribute, "task.outcome", outcome)
                    if outcome in ("failed", "retrying"):
                        _safe(span.set_status, trace.StatusCode.ERROR)
                    _safe(self._executions.add, 1, {**labels, "outcome": outcome})
                    _safe(self._duration.record, elapsed, labels)
                    _safe(self._active.add, -1, labels)
                    _safe(self.emit, "task.finished", {**run._fields, "outcome": outcome, "duration_seconds": elapsed}, span)

    def emit(self, event: str, attributes: Mapping | None = None, span: trace.Span | None = None):
        """Queue opt-in structured events; callers never wait for a log sink."""
        if self._events is None or self._closed.is_set() or self.pid != os.getpid():
            return
        context = (span or trace.get_current_span()).get_span_context()
        record = {"attributes": _attributes(attributes), "event": event[:128], "timestamp": time.time(),
                  "service.name": self.configuration.service_name or os.getenv("OTEL_SERVICE_NAME", ""),
                  "trace_id": trace.format_trace_id(context.trace_id) if context.is_valid else "",
                  "span_id": trace.format_span_id(context.span_id) if context.is_valid else ""}
        try:
            self._events.put_nowait(record)
        except queue.Full:
            self.dropped_events += 1

    def _write_events(self):
        """Use application-configured logging from a dedicated bounded worker."""
        while not self._closed.is_set() or not self._events.empty():
            try:
                record = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                _EVENT_LOG.info(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            except Exception:
                self.dropped_events += 1
            finally:
                self._events.task_done()

    def force_flush(self, timeout: float | None = None) -> bool:
        """Flush queued telemetry within a caller budget without closing providers."""
        if self.pid != os.getpid() or self._closed.is_set():
            return False
        if not self.enabled:
            return True
        try:
            budget = float(self.configuration.shutdown_timeout_seconds if timeout is None else timeout)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(budget) or budget <= 0:
            return False
        budget = min(budget, 10.0)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            with self._flush_lock:
                state = self._flush_state
                own_flush = state is None or state["done"].is_set()
                if own_flush:
                    state = {"done": threading.Event(), "result": False}
                    self._flush_state = state

                    def flush(operation=state):
                        success = True
                        try:
                            for provider in (self.tracer_provider, self.meter_provider):
                                callback = getattr(provider, "force_flush", None)
                                if callback is None:
                                    continue
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    success = False
                                    break
                                if callback(timeout_millis=max(1, remaining * 1000)) is False:
                                    success = False
                            while self._events is not None and self._events.unfinished_tasks:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    success = False
                                    break
                                time.sleep(min(0.01, remaining))
                            operation["result"] = success and time.monotonic() <= deadline
                        except Exception:
                            operation["result"] = False
                        finally:
                            operation["done"].set()

                    threading.Thread(target=flush, name="luma-observe-flush", daemon=True).start()
            if not state["done"].wait(timeout=max(0, deadline - time.monotonic())):
                return False
            if own_flush:
                return state["result"]
            # A previous call may already have flushed traces before this call
            # enqueued new ones. Its completion cannot satisfy our watermark.
            # Start another round within our original budget, never in parallel.
        return False

    def shutdown(self, timeout: float | None = None):
        """Bound the caller's wait even when an exporter or log handler stalls."""
        if self.pid != os.getpid() or self._closed.is_set():
            return
        self._closed.set()
        budget = min(max(timeout if timeout is not None else self.configuration.shutdown_timeout_seconds, 0), 10)

        def close():
            _safe(self.meter_provider.shutdown, timeout_millis=max(1, budget * 1000)) if hasattr(self.meter_provider, "shutdown") else None
            _safe(self.tracer_provider.shutdown) if hasattr(self.tracer_provider, "shutdown") else None

        if self.enabled:
            self._shutdown_thread = threading.Thread(target=close, name="luma-observe-shutdown", daemon=True)
            self._shutdown_thread.start()
            self._shutdown_thread.join(timeout=budget)
        self.enabled = False


_configuration = Configuration()
_runtime: Runtime | None = None
_lock = threading.RLock()


def _after_fork():
    """Discard inherited providers and locks; the child creates its own workers."""
    global _runtime, _lock
    _runtime = None
    _lock = threading.RLock()
    _current_task.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def configure(service_name: str | None = None, *, enabled: bool | None = None,
              resource_attributes: Mapping | None = None, **options) -> Runtime:
    """Configure once per process; identical calls reuse the same providers."""
    global _configuration, _runtime
    configuration = Configuration(service_name=service_name, enabled=enabled,
                                  resource_attributes=dict(resource_attributes or {}), **options)
    with _lock:
        if (_runtime is not None and _runtime.pid == os.getpid()
                and not _runtime._closed.is_set() and configuration == _configuration):
            return _runtime
        if _runtime is not None:
            _runtime.shutdown()
        _configuration = configuration
        _runtime = Runtime(configuration)
        return _runtime


def get_runtime() -> Runtime:
    global _runtime
    with _lock:
        if _runtime is None or _runtime.pid != os.getpid():
            _runtime = Runtime(_configuration)
        return _runtime


def telemetry_disabled() -> bool:
    """Read explicit SDK disablement without allocating a runtime or exporters."""
    return _configuration.disabled()


def force_flush(timeout: float | None = None) -> bool:
    """Flush an existing process runtime; this does not initialize or shut it down."""
    return _runtime.force_flush(timeout) if _runtime is not None else True


def shutdown(timeout: float | None = None):
    """Release this process's SDK workers without creating a runtime at exit."""
    if _runtime is not None:
        _runtime.shutdown(timeout)


atexit.register(shutdown)
