"""Bounded application telemetry built on official OpenTelemetry providers."""

from __future__ import annotations

import atexit
import functools
import inspect
import json
import logging
import math
import os
import queue
import sys
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
from .diagnostics import StageSummaries, error_attributes, error_identity, exception_details
from .process import ProcessMetricExporter, ProcessSpanExporter, register_provider_cleanup


_EVENT_LOG = logging.getLogger("luma_observe.events")
_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 7200)
_OUTCOMES = frozenset(("succeeded", "failed", "retrying", "skipped", "cancelled"))
# Metric label for business outcomes the application did not declare.
_UNDECLARED_OUTCOME = "other"
_MAX_ACTIVE_RUNS = 8192
_MAX_TASK_KEYS = 2048
_OVERFLOW = object()
_current_task = ContextVar("luma_observe_current_task", default=None)
# Swallowed instrumentation exceptions in this process, exported as observe.sdk.dropped.
_instrumentation_errors = [0]


def _note_error() -> None:
    _instrumentation_errors[0] += 1


def _safe(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except Exception:
        _note_error()
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
        self._marker = None
        self._fields = dict(_fields or {})
        self._outcome = outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "succeeded"
        self._explicit_outcome = False
        self._stages = StageSummaries()
        self._failure_detail = None

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

    def escalate_business_outcome(self, value: str) -> None:
        """Keep the value that ranks first in ``configure(business_outcomes=...)``, e.g. one failed item in a batch.

        Undeclared values rank last; among equal ranks the latest value wins, so
        without a declaration this behaves like assigning ``business_outcome``.
        """
        if not isinstance(value, str):
            return
        order = self._runtime.configuration.business_outcomes if self._runtime is not None else ()
        rank = {item: index for index, item in enumerate(order)}
        current = self.business_outcome
        if current is None or rank.get(value, len(rank)) <= rank.get(current, len(rank)):
            self.business_outcome = value

    def set_business_status(self, value: str) -> None:
        self.business_status = value

    def record_business_outcome(self, outcome: str | None, *, status: str | None = None,
                                failed: bool = False, error_type: str = "") -> None:
        """Record the caller's classification using configured priority and SDK events."""
        if self._finished or self.pid != os.getpid():
            return
        if status is not None:
            self.business_status = status
        if outcome is None:
            return
        self.escalate_business_outcome(outcome)
        if failed:
            _safe(self.span.set_status, trace.StatusCode.ERROR)
            _safe(self.span.set_attribute, "error.type", error_type[:256])
        _safe(self.add_event, "business_outcome", {"outcome": outcome, "error_type": error_type})

    def record_error(self, exception: BaseException) -> None:
        """Record failure types and associate opt-in details with this attempt."""
        if self._finished or self.pid != os.getpid():
            return
        self._fields["error.type"] = type(exception).__name__
        _safe(self.span.set_attribute, "error.type", type(exception).__name__)
        _safe(self.span.set_status, trace.StatusCode.ERROR)
        _safe(self.span.add_event, "exception", {"exception.type": type(exception).__name__})
        _safe(self._record_failure, exception, {"task.stage": "task.execution"})

    def _record_failure(self, exception: BaseException, attributes: dict, *, terminal: bool = True):
        """Keep bounded error snapshots, preferring the exception propagated to the framework."""
        if self._runtime is None or not self._runtime.enabled:
            return
        capture = self._runtime.configuration.capture_error_details
        if not capture and not self._stages.rows and not self._stages.omitted:
            return
        fields = {**attributes, **error_attributes(exception, capture)}
        if not self._stages.claim_failure(error_identity(exception), fields, terminal=terminal):
            return
        self._failure_detail = _safe(exception_details, exception) if capture else None

    def _finish_stages(self):
        """Emit one selected execution error and the accumulated stage summaries."""
        if self._stages.failure is not None:
            fields = self._stages.failure
            _safe(self.add_event, "task.failure", fields)
            if self._failure_detail is not None:
                _safe(self._runtime.emit, "task.failure.detail", {**self._fields, **fields}, self.span,
                      exception_detail=self._failure_detail)
        self._failure_detail = None
        rows, omitted = self._stages.completed()
        for fields in rows:
            _safe(self.add_event, "task.phase.finished", fields)
        if omitted:
            _safe(self.add_event, "task.phases.omitted", {"phase.omitted": omitted})

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

    def mark_progress(self) -> None:
        """Reset this attempt's task.no_progress_age without creating telemetry."""
        if self._finished or self.pid != os.getpid() or self._runtime is None:
            return
        _safe(self._runtime._track_progress, self._marker)

    @contextmanager
    def stage(self, name: str, attributes: Mapping | None = None, *, aggregate: bool = False):
        """Record a child span, or aggregate same-name intervals on the task's root span."""
        if self._finished or self.pid != os.getpid() or self._runtime is None or not self._runtime.enabled:
            yield trace.INVALID_SPAN
            return
        if aggregate:
            with self._aggregate_stage(name, attributes) as span:
                yield span
            return
        fields = _safe(lambda: {**_attributes(attributes, limit=24), "task.name": self._fields.get("task.name", "")}) or {}
        self.mark_progress()
        # The child span already records the stage on the trace; only the lifecycle
        # log gets an event, so many stages cannot exhaust the attempt span's event limit.
        _safe(self._runtime.emit, "task.stage", {**self._fields, "task.stage": str(name)[:128]}, self.span)
        try:
            with self._runtime.span(name, attributes=fields) as span:
                yield span
        finally:
            self.mark_progress()

    @contextmanager
    def _aggregate_stage(self, name: str, attributes: Mapping | None):
        name = str(name)[:128]
        fields = _attributes(attributes, limit=16)
        _safe(self.mark_progress)
        state = _safe(self._stages.start, name, fields)
        if state is not None and state[1] is not None:
            _safe(self._runtime.emit, "task.phase.started", {**self._fields, **state[1]}, self.span)
        error = {}
        try:
            yield self.span
        except BaseException as exception:
            error = _safe(error_attributes, exception, self._runtime.configuration.capture_error_details) or {
                "error.type": type(exception).__name__,
            }
            _safe(self._record_failure, exception, {**fields, "task.stage": name}, terminal=False)
            raise
        finally:
            _safe(self._stages.finish, name, state[0] if state is not None else None, fields, error)
            _safe(self.mark_progress)


def _attach_stream_handler(stream_name: str) -> None:
    """Route lifecycle JSON to stdout or stderr once, without touching the root logger."""
    stream = sys.stdout if stream_name == "stdout" else sys.stderr
    if any(getattr(handler, "_luma_observe_stream", None) == stream_name for handler in _EVENT_LOG.handlers):
        return
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._luma_observe_stream = stream_name
    _EVENT_LOG.addHandler(handler)
    _EVENT_LOG.setLevel(logging.INFO)
    _EVENT_LOG.propagate = False


def current_task() -> TaskRun | None:
    """Return this execution context's live attempt, without initializing the SDK."""
    run = _current_task.get()
    return run if run is not None and run.pid == os.getpid() and not run._finished else None


def record_business_outcome(outcome: str | None, *, status: str | None = None,
                            failed: bool = False, error_type: str = "") -> None:
    """Record application-owned outcome/status without changing task control flow."""
    run = _safe(current_task)
    if run is not None:
        _safe(run.record_business_outcome, outcome, status=status, failed=failed, error_type=error_type)


@contextmanager
def stage(name: str, attributes: Mapping | None = None, *, aggregate: bool = False):
    """Run ``TaskRun.stage`` for the current attempt; outside a task the body runs unobserved.

    Instrumentation failures on entry or exit are swallowed; the body's own
    return value and exceptions are unchanged.
    """
    run = _safe(current_task)
    manager = _safe(run.stage, name, attributes, aggregate=aggregate) if run is not None else None
    span = _safe(manager.__enter__) if manager is not None else None
    if span is None:
        yield trace.INVALID_SPAN
        return
    try:
        yield span
    except BaseException:
        _safe(manager.__exit__, *sys.exc_info())
        raise
    else:
        _safe(manager.__exit__, None, None, None)


def observed_stage(name: str, attributes: Mapping | None = None, *, aggregate: bool = False):
    """Decorate a sync or async function so each call runs inside ``stage(name)``."""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_wrapper(*args, **kwargs):
                with stage(name, attributes, aggregate=aggregate):
                    return await function(*args, **kwargs)
            return async_wrapper

        @functools.wraps(function)
        def sync_wrapper(*args, **kwargs):
            with stage(name, attributes, aggregate=aggregate):
                return function(*args, **kwargs)
        return sync_wrapper
    return decorate


def mark_progress() -> None:
    """Reset the current attempt's ``task.no_progress_age``; no-op outside a task."""
    run = current_task()
    if run is not None:
        run.mark_progress()


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
        self._last_times = {}
        self._registered = set()
        self._roles = set()
        self.started_at = time.time()
        self.instance_id = ""
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
            self.instance_id = str(resource_values["service.instance.id"])
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
            meter.create_observable_gauge("task.no_progress_age", unit="s", callbacks=[self._observe_no_progress],
                                          description="Longest time since an active attempt started, entered or left a stage, or marked progress")
            meter.create_observable_gauge("task.last_started", unit="s", callbacks=[self._observe_last("started")],
                                          description="Unix time this process last started an attempt of the task")
            meter.create_observable_gauge("task.last_finished", unit="s", callbacks=[self._observe_last("finished")],
                                          description="Unix time this process last finished an attempt of the task with the outcome")
            meter.create_observable_gauge("task.registered", unit="", callbacks=[self._observe_registered],
                                          description="Tasks a worker process registered with its framework")
            meter.create_observable_gauge("task.process.started", unit="s", callbacks=[self._observe_roles],
                                          description="Unix start time of this process for each declared framework role")
            meter.create_observable_counter("observe.sdk.dropped", unit="1", callbacks=[self._observe_dropped],
                                            description="Telemetry this process discarded inside the SDK")
            self._publications = meter.create_counter("task.publications", unit="1")
            self._business_outcomes = meter.create_counter("task.business_outcomes", unit="1")
            if configuration.lifecycle_logs:
                if configuration.log_stream:
                    _attach_stream_handler(configuration.log_stream)
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
            # Start time, metric labels and last progress, all monotonic.
            self._active_runs[marker] = [started, key, started]
            return marker

    def _track_end(self, marker) -> None:
        if marker is None or self.pid != os.getpid():
            return
        with self._activity_lock:
            if marker is _OVERFLOW:
                self._tracking_overflow -= 1
            else:
                self._active_runs.pop(marker, None)

    def _track_progress(self, marker) -> None:
        if marker is None or marker is _OVERFLOW or self.pid != os.getpid():
            return
        with self._activity_lock:
            record = self._active_runs.get(marker)
            if record is not None:
                record[2] = time.monotonic()

    def _record_time(self, kind: str, labels: Mapping, outcome: str | None = None) -> None:
        """Keep the latest wall time per task key; the key set is bounded like task names."""
        key = (kind, tuple(labels.items()) + ((("outcome", outcome),) if outcome else ()))
        with self._activity_lock:
            if key in self._last_times or len(self._last_times) < _MAX_TASK_KEYS:
                self._last_times[key] = time.time()

    def register_process(self, framework: str, role: str, queue: str = "") -> None:
        """Declare a framework role of this process, exported as task.process.started."""
        if not self._live():
            return
        key = (("task.framework", str(framework)[:64]), ("task.role", str(role)[:64]), ("task.queue", str(queue)[:256]))
        with self._activity_lock:
            if len(self._roles) < 64:
                self._roles.add(key)

    def register_tasks(self, names, framework: str, queue: str = "") -> None:
        """Declare the tasks a worker process serves, exported as task.registered."""
        if not self._live():
            return
        added = []
        with self._activity_lock:
            for name in names:
                if len(self._registered) >= _MAX_TASK_KEYS:
                    break
                key = (("task.name", str(name)[:256]), ("task.framework", str(framework)[:64]), ("task.queue", str(queue)[:256]))
                if key not in self._registered:
                    self._registered.add(key)
                    added.append(dict(key))
        # A zero point lets "registered but never started here" read as idle,
        # instead of a missing task.active series that looks like missing data.
        for labels in added:
            _safe(self._active.add, 0, labels)

    def _observe_heartbeat(self, _options):
        return [metrics.Observation(time.time())] if self._live() else []

    def _observe_no_progress(self, _options):
        if not self._live():
            return []
        with self._activity_lock:
            if self._tracking_overflow:
                return []
            stalled = {}
            for _started, key, progress in self._active_runs.values():
                stalled[key] = min(stalled.get(key, progress), progress)
        now = time.monotonic()
        return [metrics.Observation(max(0.0, now - progress), attributes=dict(key)) for key, progress in stalled.items()]

    def _observe_last(self, kind: str):
        def observe(_options):
            if not self._live():
                return []
            with self._activity_lock:
                values = [(key, value) for (recorded, key), value in self._last_times.items() if recorded == kind]
            return [metrics.Observation(value, attributes=dict(key)) for key, value in values]
        return observe

    def _observe_registered(self, _options):
        if not self._live():
            return []
        with self._activity_lock:
            keys = tuple(self._registered)
        return [metrics.Observation(1, attributes=dict(key)) for key in keys]

    def _observe_roles(self, _options):
        if not self._live():
            return []
        with self._activity_lock:
            keys = tuple(self._roles)
        return [metrics.Observation(self.started_at, attributes=dict(key)) for key in keys]

    def _observe_dropped(self, _options):
        if not self._live():
            return []
        return [metrics.Observation(self.dropped_events, attributes={"reason": "lifecycle_logs"}),
                metrics.Observation(_instrumentation_errors[0], attributes={"reason": "instrumentation_error"})]

    def _observe_oldest(self, _options):
        if not self._live():
            return []
        with self._activity_lock:
            if self._tracking_overflow:
                return []
            oldest = {}
            for started, key, _progress in self._active_runs.values():
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
                marker = run._marker = _safe(self._track_start, started, labels)
                _safe(self._record_time, "started", labels)
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
                _safe(run._record_failure, exception, {"task.stage": "task.execution"})
                raise
            finally:
                _safe(run._finish_stages)
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
                    _safe(self._record_time, "finished", labels, outcome)
                    business = run._fields.get("task.business.outcome")
                    declared = self.configuration.business_outcomes
                    # Spans and logs keep the application's value; the metric label stays a declared enum.
                    if business and declared:
                        label = business if business in declared else _UNDECLARED_OUTCOME
                        _safe(self._business_outcomes.add, 1, {**labels, "task.business.outcome": label})
                    _safe(self.emit, "task.finished", {**run._fields, "outcome": outcome, "duration_seconds": elapsed}, span)

    @contextmanager
    def publication(self, name: str, *, framework: str = "manual", queue: str = "", task_id: str = ""):
        """Observe one application transport send; the original return and exception are preserved."""
        labels = {"task.name": str(name)[:256], "task.framework": str(framework)[:64], "task.queue": str(queue)[:256]}
        fields = {**labels, "messaging.message.id": str(task_id)[:256]} if task_id else labels
        outcome = "sent"
        with self.span("task.publish/" + labels["task.name"], attributes=fields, kind=trace.SpanKind.PRODUCER) as span:
            try:
                yield span
            except BaseException:
                outcome = "failed"
                raise
            finally:
                if self._live():
                    _safe(self._publications.add, 1, {**labels, "outcome": outcome})
                    _safe(self.emit, "task.published", {**fields, "outcome": outcome}, span)

    def emit(self, event: str, attributes: Mapping | None = None, span: trace.Span | None = None,
             *, exception: BaseException | None = None, exception_detail: dict | None = None):
        """Queue opt-in structured events; callers never wait for a log sink."""
        if self._events is None or self._closed.is_set() or self.pid != os.getpid():
            return
        context = (span or trace.get_current_span()).get_span_context()
        record = {"attributes": _attributes(attributes), "event": event[:128], "timestamp": time.time(),
                  "service.name": self.configuration.service_name or os.getenv("OTEL_SERVICE_NAME", ""),
                  "service.instance.id": self.instance_id,
                  "trace_id": trace.format_trace_id(context.trace_id) if context.is_valid else "",
                  "span_id": trace.format_span_id(context.span_id) if context.is_valid else ""}
        if self.configuration.capture_error_details:
            if exception_detail is not None:
                record["exception"] = exception_detail
            elif exception is not None:
                record["exception"] = exception_details(exception)
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
        deadline = time.monotonic() + budget

        def close():
            _safe(self.meter_provider.shutdown, timeout_millis=max(1, budget * 1000)) if hasattr(self.meter_provider, "shutdown") else None
            _safe(self.tracer_provider.shutdown) if hasattr(self.tracer_provider, "shutdown") else None

        if self.enabled:
            self._shutdown_thread = threading.Thread(target=close, name="luma-observe-shutdown", daemon=True)
            self._shutdown_thread.start()
            # The closed writer drains already queued lifecycle events, then exits.
            # A process exits right after shutdown, so wait for it within the same budget.
            if self._writer is not None:
                self._writer.join(timeout=max(0, deadline - time.monotonic()))
            self._shutdown_thread.join(timeout=max(0, deadline - time.monotonic()))
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
    # Cumulative counters belong to one process identity.
    _instrumentation_errors[0] = 0


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
