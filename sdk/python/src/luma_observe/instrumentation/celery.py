"""Process-wide Celery signals using official OTel propagation and SDK contexts.

Call instrument_celery once during Celery application setup. Providers are first
created in worker children, Beat, or the process that publishes a task.
"""

from __future__ import annotations

import functools
import sys
import weakref
from typing import Callable

from celery import current_app, signals

from luma_observe import get_runtime, shutdown, telemetry_disabled
from luma_observe.instrumentation import (
    _attempt,
    _enrich,
    _finish_attempt,
    _prepare_carrier,
    _safe,
    _start_attempt,
)


_instrumentation = None
# Each app's SDK sender state. Identity, not attributes that functools.wraps copies,
# decides whether restoring is safe.
_senders = weakref.WeakKeyDictionary()


def _routing_key(app, options) -> str:
    """Resolve the routing key Celery's sender publishes with, which the worker reads back.

    Mirrors Celery's ``send_task_message``: a direct exchange (the default) routes by
    queue name; another exchange type uses the explicit key, then the queue's own key.
    """
    amqp = app.amqp
    exchange, routing_key, queue = options.get("exchange"), options.get("routing_key"), options.get("queue")
    exchange_type = options.get("exchange_type")
    name = queue
    if queue is None and exchange is None:
        queue = amqp.default_queue
    if queue is not None:
        if isinstance(queue, str):
            name, queue = queue, amqp.queues[queue]
        else:
            name = queue.name
    if exchange_type is None:
        try:
            exchange_type = queue.exchange.type
        except AttributeError:
            exchange_type = "direct"
    if (not exchange or not routing_key) and exchange_type == "direct":
        return str(name or "")
    if exchange is None:
        return str(routing_key or getattr(queue, "routing_key", None) or app.conf.task_default_routing_key or "")
    return str(routing_key or "")


def _publication(app, name, message, options):
    if telemetry_disabled():
        return None
    runtime = get_runtime()
    if not runtime.enabled:
        return None
    headers = getattr(message, "headers", None) or {}
    body = getattr(message, "body", None)
    task_id = headers.get("id") or (body.get("id") if isinstance(body, dict) else "")
    queue = _safe(_routing_key, app, options) or ""
    return runtime.publication(str(name), framework="celery", queue=queue, task_id=str(task_id or ""))


def _calls(sender, wrapper) -> bool:
    """Whether ``sender`` is ``wrapper`` or a later wrapper chained to it through ``__wrapped__``."""
    for _ in range(32):
        if sender is wrapper:
            return True
        sender = getattr(sender, "__wrapped__", None)
        if sender is None:
            return False
    return False


def _observe_sender(app) -> None:
    """Wrap the app's actual sender: Celery signals cannot report a failed send."""
    amqp = app.amqp
    state = _senders.get(app)
    if state is not None and _calls(amqp.send_task_message, state["wrapper"]):
        state["active"] = True
        return
    original = amqp.send_task_message
    state = {"original": original, "active": True}

    @functools.wraps(original)
    def send_task_message(producer, name, message, *args, **kwargs):
        # Celery passes these as keywords; positional callers follow its signature order.
        options = {**dict(zip(("exchange", "routing_key", "queue"), args)), **kwargs}
        manager = _safe(_publication, app, name, message, options) if state["active"] else None
        entered = manager is not None and _safe(manager.__enter__) is not None
        if not entered:
            return original(producer, name, message, *args, **kwargs)
        try:
            result = original(producer, name, message, *args, **kwargs)
        except BaseException:
            _safe(manager.__exit__, *sys.exc_info())
            raise
        _safe(manager.__exit__, None, None, None)
        return result

    state["wrapper"] = send_task_message
    _senders[app] = state
    amqp.send_task_message = send_task_message


def _restore_sender(app) -> None:
    """Put Celery's sender back only while the SDK wrapper is outermost.

    A component that wrapped the sender later keeps its wrapper; the SDK layer under
    it only stops recording.
    """
    state = _senders.get(app)
    if state is None:
        return
    state["active"] = False
    if app.amqp.send_task_message is state["wrapper"]:
        app.amqp.send_task_message = state["original"]
        del _senders[app]


class CeleryInstrumentation:
    """Own signal subscriptions for one SDK installation in the current process."""

    def __init__(self, *, attributes: Callable | None = None, app=None):
        self.installed = False
        self.attributes = attributes
        self.app = app
        self._sender_app = None
        self._receivers = (
            (signals.before_task_publish, self._before_publish),
            (signals.task_prerun, self._prerun),
            (signals.task_postrun, self._postrun),
            (signals.task_failure, self._failure),
            (signals.task_retry, self._retry),
            (signals.worker_process_init, self._worker_started),
            (signals.beat_init, self._beat_started),
            (signals.worker_process_shutdown, self._shutdown),
            (signals.worker_shutdown, self._shutdown),
        )

    def _install(self):
        """Attach supported signals without changing Celery's task functions."""
        if self.installed:
            return
        for signal, receiver in self._receivers:
            signal.connect(receiver, weak=False)
        self.installed = True

    def _publications(self, enabled: bool):
        """Observe sends of one app; a later call without the option restores Celery's sender."""
        app = (self.app or current_app._get_current_object()) if enabled else None
        if self._sender_app is not None and self._sender_app is not app:
            _restore_sender(self._sender_app)
            self._sender_app = None
        if app is not None:
            _observe_sender(app)
            self._sender_app = app

    def uninstrument(self):
        """Remove subscriptions after active task executions have drained."""
        self._publications(False)
        if not self.installed:
            return
        for signal, receiver in self._receivers:
            signal.disconnect(receiver)
        self.installed = False

    def _worker_started(self, **_kwargs):
        """Declare the pool process role and the application's registered tasks."""
        def register():
            runtime = get_runtime()
            runtime.register_process("celery", "worker")
            tasks = (self.app or current_app).tasks
            runtime.register_tasks((name for name in tasks.keys() if not name.startswith("celery.")), "celery")

        _safe(register)

    def _beat_started(self, **_kwargs):
        _safe(lambda: get_runtime().register_process("celery", "scheduler"))

    def _shutdown(self, **_kwargs):
        _safe(shutdown)

    def _before_publish(self, sender=None, headers=None, **_kwargs):
        """Only inspect transport headers; task bodies remain with Celery."""
        if headers is not None:
            _safe(_prepare_carrier, headers, str(sender or "unknown"), "celery", str(headers.get("id", "")))

    def _prerun(self, sender=None, task=None, task_id=None, args=None, kwargs=None, **_kwargs):
        """Open an execution context on Celery's invocation-local request object."""
        def begin():
            target = task or sender
            if target is None:
                return
            request = target.request
            delivery = request.delivery_info or {}
            extra = (_enrich(self.attributes, target.name, args, kwargs, function=getattr(target, "run", None))
                     if self.attributes is not None and get_runtime().enabled else None)
            _start_attempt(
                request,
                name=target.name,
                framework="celery",
                queue=str(delivery.get("routing_key") or ""),
                task_id=str(task_id or request.id or ""),
                retries=int(request.retries or 0),
                carrier=request.headers or {},
                extra=extra,
            )

        _safe(begin)

    def _postrun(self, sender=None, task=None, state=None, **_kwargs):
        """Use Celery's resulting state and always close the observation context."""
        def finish():
            target = task or sender
            if target is None:
                return
            outcome = {
                "SUCCESS": "succeeded",
                "RETRY": "retrying",
                "REVOKED": "cancelled",
                "IGNORED": "skipped",
                "REJECTED": "skipped",
            }.get(state, "failed")
            _finish_attempt(target.request, outcome)

        _safe(finish)

    def _failure(self, sender=None, exception=None, **_kwargs):
        """Record exception type through the runtime's redacted error recorder."""
        def record():
            attempt = _attempt(sender.request) if sender is not None else None
            if attempt is not None and exception is not None:
                attempt.run.record_error(exception)

        _safe(record)

    def _retry(self, sender=None, request=None, reason=None, **_kwargs):
        """Keep retry diagnostics on the current attempt; Celery owns rescheduling."""
        def record():
            owner = request or (sender.request if sender is not None else None)
            attempt = _attempt(owner)
            if attempt is not None and reason is not None:
                attempt.run.record_error(reason)

        _safe(record)


def instrument_celery(*, attributes: Callable | None = None, app=None, publications: bool = False) -> CeleryInstrumentation:
    """Install idempotent, process-wide Celery hooks without starting exporters.

    ``attributes(task_name, args, kwargs)`` may return bounded span fields for each
    attempt. ``app`` selects the registry for task.registered (default: current app).
    ``publications=True`` also records each actual send of that app as sent or failed
    in ``task.publications``. The latest call's options apply to the shared installation.
    """
    global _instrumentation
    if _instrumentation is None:
        _instrumentation = CeleryInstrumentation()
    _instrumentation.attributes = attributes
    _instrumentation.app = app
    _instrumentation._install()
    _instrumentation._publications(publications)
    return _instrumentation
