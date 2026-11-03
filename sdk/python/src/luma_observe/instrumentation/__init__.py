"""Optional framework adapters built on public framework hooks and OTel APIs."""

from __future__ import annotations

import inspect
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping

from opentelemetry import context, propagate, trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, SpanKind

from luma_observe import get_runtime, telemetry_disabled
from luma_observe.runtime import _note_error


ENQUEUED_AT = "luma.observe.enqueued_at"
ATTEMPT_ATTRIBUTE = "_luma_observe_attempt"


def _safe(callback, *args, **kwargs):
    """Keep instrumentation callback failures separate from task execution."""
    try:
        return callback(*args, **kwargs)
    except Exception:
        _note_error()
        return None


def _wants_arguments(callback) -> bool:
    """Pass bound arguments only to callbacks that declare an ``arguments`` parameter."""
    try:
        return "arguments" in inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False


def _bind(function, args, kwargs) -> dict:
    """Map positional and keyword values to the task function's parameter names.

    Positional values are named by position, keyword values keep their names, and
    keywords the signature does not declare are still passed through. A message that
    no longer matches the current signature therefore still yields what can be named.
    ``inspect.signature`` follows decorator ``__wrapped__`` chains and drops the bound
    ``self`` of a method, such as a Celery ``bind=True`` task's ``run``.
    """
    kwargs = dict(kwargs or {})
    if function is None:
        return kwargs
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return kwargs
    named = {}
    positional = [parameter for parameter in parameters
                  if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    for parameter, value in zip(positional, args or ()):
        named[parameter.name] = value
    named.update(kwargs)
    return named


def _enrich(callback, name: str, args, kwargs, function=None) -> Mapping | None:
    """Ask the application for attempt attributes; its failure never blocks the attempt."""
    if callback is None:
        return None

    def call():
        options = {"arguments": _bind(function, args, kwargs)} if _wants_arguments(callback) else {}
        value = callback(name, tuple(args or ()), dict(kwargs or {}), **options)
        return dict(value) if isinstance(value, Mapping) else None

    return _safe(call)


def _queue_wait(carrier: dict) -> float | None:
    """Ignore unavailable or invalid producer clocks in queue wait metrics."""
    try:
        wait = time.time() - float(carrier[ENQUEUED_AT])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return wait if math.isfinite(wait) and 0 <= wait <= 31 * 86400 else None


def _prepare_carrier(carrier: dict, name: str, framework: str, task_id: str) -> None:
    """Timestamp every enabled publication and link re-published context."""
    if telemetry_disabled():
        return
    # Producers may only send messages while workers export telemetry. A local
    # OTLP endpoint is unnecessary for the worker's queue-wait measurement.
    carrier[ENQUEUED_AT] = time.time()
    previous = trace.get_current_span(propagate.extract(carrier, context=Context())).get_span_context()
    runtime = get_runtime()
    if not runtime.enabled:
        # With no local preparation span, preserve an already-carried context.
        # An ambient span from another OTel integration can still be propagated.
        if not previous.is_valid:
            propagate.inject(carrier)
        return
    tracer = runtime.get_tracer("luma.observe." + framework)
    with tracer.start_as_current_span(
        "task.prepare/" + name,
        kind=SpanKind.PRODUCER,
        attributes={
            "task.name": name,
            "task.framework": framework,
            "messaging.message.id": task_id,
        },
        links=[Link(previous)] if previous.is_valid else (),
        record_exception=False,
        set_status_on_exception=False,
    ) as preparation:
        # A disabled signal or head sampler cannot export the link. Preserve
        # the old carrier in that case rather than replacing its only identity.
        if preparation.is_recording() or not previous.is_valid:
            propagate.inject(carrier)


@dataclass
class _Attempt:
    """Keep one attempt on an invocation holder or request, including duplicates."""

    manager: Any
    run: Any
    pid: int
    original_error_is_skip: bool | None = None


@contextmanager
def _execution_context(manager, execution_context):
    """Activate propagated baggage as well as the execution span, then restore."""
    token = context.attach(execution_context)
    try:
        with manager as run:
            yield run
    finally:
        context.detach(token)


def _start_attempt(
    owner: Any,
    *,
    name: str,
    framework: str,
    queue: str,
    task_id: str,
    retries: int,
    carrier: dict,
    extra: Mapping | None = None,
) -> None:
    """Open a task context in the actual execution process and keep its owner."""
    if _attempt(owner) is not None:
        return
    if telemetry_disabled():
        return
    runtime = get_runtime()
    if not runtime.enabled:
        return
    remote = propagate.extract(carrier, context=Context())
    source = trace.get_current_span(remote).get_span_context()
    execution_context = trace.set_span_in_context(trace.INVALID_SPAN, remote)
    manager = runtime.task(
        name,
        framework=framework,
        queue=queue,
        # Each execution gets a fresh trace so a finished producer span cannot
        # decide its sampling before a queued task even starts. Baggage remains
        # available while a SpanLink keeps the standard cross-service identity.
        context=execution_context,
        links=[Link(source)] if source.is_valid else (),
        queue_wait_seconds=_queue_wait(carrier),
        # Framework fields come first: application fields cannot replace them
        # and are the ones dropped when an attempt exceeds the attribute bound.
        attributes={
            "messaging.message.id": task_id,
            "task.retry.attempt": retries,
            **{key: value for key, value in (extra or {}).items() if key not in ("messaging.message.id", "task.retry.attempt")},
        },
    )
    manager = _execution_context(manager, execution_context)
    run = manager.__enter__()
    try:
        object.__setattr__(owner, ATTEMPT_ATTRIBUTE, _Attempt(manager, run, os.getpid()))
    except Exception:
        _safe(manager.__exit__, None, None, None)
        raise


def _attempt(owner: Any) -> _Attempt | None:
    value = getattr(owner, ATTEMPT_ATTRIBUTE, None)
    return value if isinstance(value, _Attempt) and value.pid == os.getpid() else None


def _finish_attempt(owner: Any, outcome: str) -> None:
    """Detach each attempt once, including framework paths without saved results."""
    attempt = _attempt(owner)
    if attempt is None:
        return
    try:
        object.__delattr__(owner, ATTEMPT_ATTRIBUTE)
    except AttributeError:
        pass
    try:
        attempt.run.set_framework_outcome(outcome)
    finally:
        _safe(attempt.manager.__exit__, None, None, None)
