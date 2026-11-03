"""Process-wide Celery signals using official OTel propagation and SDK contexts.

Call instrument_celery once during Celery application setup. Providers are first
created in worker children, Beat, or the process that publishes a task.
"""

from __future__ import annotations

from typing import Callable

from celery import current_app, signals

from luma_observe import get_runtime, shutdown
from luma_observe.instrumentation import (
    _attempt,
    _enrich,
    _finish_attempt,
    _prepare_carrier,
    _safe,
    _start_attempt,
)


_instrumentation = None


class CeleryInstrumentation:
    """Own signal subscriptions for one SDK installation in the current process."""

    def __init__(self, *, attributes: Callable | None = None, app=None):
        self.installed = False
        self.attributes = attributes
        self.app = app
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
        """Attach supported signals without changing Celery's send or task functions."""
        if self.installed:
            return
        for signal, receiver in self._receivers:
            signal.connect(receiver, weak=False)
        self.installed = True

    def uninstrument(self):
        """Remove subscriptions after active task executions have drained."""
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
            extra = (_enrich(self.attributes, target.name, args, kwargs)
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


def instrument_celery(*, attributes: Callable | None = None, app=None) -> CeleryInstrumentation:
    """Install idempotent, process-wide Celery hooks without starting exporters.

    ``attributes(task_name, args, kwargs)`` may return bounded span fields for each
    attempt. ``app`` selects the registry for task.registered (default: current app).
    The latest call's options apply to the shared installation.
    """
    global _instrumentation
    if _instrumentation is None:
        _instrumentation = CeleryInstrumentation()
    _instrumentation.attributes = attributes
    _instrumentation.app = app
    _instrumentation._install()
    return _instrumentation
