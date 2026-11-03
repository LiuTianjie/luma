"""Taskiq hooks with attempt cleanup independent of message and result storage.

Install before retry middleware. The serialized carrier contains only standard
propagation fields and a private invocation ID. Business arguments are read only
by an application-supplied ``attributes`` callback. Broker shutdown flushes
shared providers without closing them.
"""

from __future__ import annotations

import asyncio
from contextvars import Context, ContextVar, Token, copy_context
from dataclasses import dataclass
import os
from typing import Callable
import uuid
from weakref import ReferenceType, ref

from opentelemetry import context
from taskiq import TaskiqMiddleware as BaseMiddleware
from taskiq.exceptions import NoResultError

from luma_observe import force_flush, get_runtime, telemetry_disabled
from luma_observe.runtime import _activate_task, _deactivate_task
from luma_observe.instrumentation import (
    _attempt,
    _enrich,
    _finish_attempt,
    _prepare_carrier,
    _safe,
    _start_attempt,
)


_INVOCATION_LABEL = "luma.observe.invocation_id"


def _execution_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass(eq=False)
class _Invocation:
    """Own contexts without putting live objects on copyable framework messages."""

    pid: int
    identifier: str
    execution_context: Context
    task_ref: ReferenceType | None = None
    parent: _Invocation | None = None
    token: Token | None = None
    span_token: Token | None = None
    run_token: Token | None = None
    callback: Callable | None = None
    finished: bool = False


class TaskiqMiddleware(BaseMiddleware):
    """Observe tasks without taking ownership of their results, retries or ACKs."""

    def __init__(self, *, attributes: Callable | None = None):
        """``attributes(task_name, args, kwargs)`` may return bounded span fields for each attempt."""
        super().__init__()
        self._attributes = attributes
        self._invocation: ContextVar[_Invocation | None] = ContextVar("luma.observe.taskiq.invocation", default=None)

    def _current(self):
        invocation = self._invocation.get()
        owner = _execution_task()
        while invocation is not None and invocation.finished:
            invocation = invocation.parent
        if invocation is None or invocation.pid != os.getpid():
            return None
        expected_owner = invocation.task_ref() if invocation.task_ref is not None else None
        return invocation if expected_owner is owner else None

    def _find(self, message):
        identifier = message.labels.get(_INVOCATION_LABEL)
        invocation = self._current()
        while invocation is not None:
            if invocation.identifier == identifier:
                return invocation
            invocation = invocation.parent
        return None

    def _finish(self, invocation, outcome, *, restore):
        if invocation.finished or invocation.pid != os.getpid():
            return
        invocation.finished = True
        owner = invocation.task_ref() if invocation.task_ref is not None else None
        if owner is not None and invocation.callback is not None:
            _safe(owner.remove_done_callback, invocation.callback)
        invocation.callback = None
        try:
            # Runtime.task entered this exact Context. Ending it in the async
            # done callback's copied Context would invalidate OTel's tokens.
            _safe(invocation.execution_context.run, _finish_attempt, invocation, outcome)
        finally:
            if restore:
                if invocation.run_token is not None:
                    _safe(_deactivate_task, invocation.run_token)
                if invocation.span_token is not None:
                    _safe(context.detach, invocation.span_token)
                if invocation.token is not None:
                    _safe(self._invocation.reset, invocation.token)

    def _resume(self, invocation):
        """Close abandoned inline children before handling the matching parent."""
        current = self._current()
        while current is not None and current is not invocation:
            self._finish(current, "failed", restore=True)
            current = self._current()

    def _callback_done(self, task, invocation):
        # Do not retrieve task.exception(): doing so would hide asyncio's
        # unhandled business exception reporting. A caught hook failure still
        # represents an aborted attempt even if its enclosing task succeeded.
        outcome = "cancelled" if task.cancelled() else "failed"
        _safe(self._finish, invocation, outcome, restore=False)

    def startup(self):
        """Declare worker or scheduler roles and the worker's registered tasks."""
        def register():
            runtime = get_runtime()
            broker = self.broker
            queue = str(getattr(broker, "queue_name", "") or "")
            if getattr(broker, "is_worker_process", False):
                runtime.register_process("taskiq", "worker", queue)
                runtime.register_tasks(broker.get_all_tasks().keys(), "taskiq", queue)
            elif getattr(broker, "is_scheduler_process", False):
                runtime.register_process("taskiq", "scheduler", queue)

        _safe(register)

    def shutdown(self):
        # Linux worker processes can exit through os._exit without atexit.
        # The bounded flush leaves providers usable by any other broker.
        _safe(force_flush)

    def pre_send(self, message):
        """Inject propagation fields while preserving business transport semantics."""
        _safe(_prepare_carrier, message.labels, message.task_name, "taskiq", str(message.task_id))
        return message

    def pre_execute(self, message):
        """Start a context-owned attempt and register its owning Task's fallback."""
        def begin():
            if telemetry_disabled():
                return
            identifier = uuid.uuid4().hex
            # A string survives model_dump/reconstruction and deep copies. Each
            # delivery gets a fresh value even when task IDs and retries match.
            # If the carrier is immutable, fail open before allocating a frame.
            message.labels[_INVOCATION_LABEL] = identifier
            owner = _execution_task()
            invocation = _Invocation(os.getpid(), identifier, copy_context(),
                                     task_ref=ref(owner) if owner is not None else None,
                                     parent=self._current())
            invocation.token = self._invocation.set(invocation)
            if owner is not None:
                invocation.callback = lambda task: self._callback_done(task, invocation)
                owner.add_done_callback(invocation.callback, context=Context())
            extra = (_enrich(self._attributes, message.task_name, message.args, message.kwargs)
                     if self._attributes is not None and get_runtime().enabled else None)
            invocation.execution_context.run(
                _start_attempt,
                invocation,
                name=message.task_name,
                framework="taskiq",
                queue=str(message.labels.get("queue_name") or getattr(self.broker, "queue_name", "")),
                task_id=str(message.task_id),
                retries=int(message.labels.get("_retries", 0)),
                carrier=message.labels,
                extra=extra,
            )
            attempt = _attempt(invocation)
            if attempt is not None:
                invocation.span_token = context.attach(invocation.execution_context.run(context.get_current))
                invocation.run_token = _activate_task(attempt.run)

        _safe(begin)
        return message

    def on_error(self, message, result, exception):
        """Retain the original error type before the framework's final retry state."""
        def observe():
            invocation = self._find(message)
            if invocation is None:
                return
            self._resume(invocation)
            attempt = _attempt(invocation)
            if attempt is not None:
                attempt.original_error_is_skip = isinstance(exception, NoResultError)
                if not attempt.original_error_is_skip:
                    attempt.run.record_error(exception)

        _safe(observe)

    def post_execute(self, message, result):
        """Finish before result storage, including NoResultError skip paths."""
        def finish():
            invocation = self._find(message)
            if invocation is None:
                return
            self._resume(invocation)
            attempt = _attempt(invocation)
            if isinstance(result.error, asyncio.CancelledError):
                outcome = "cancelled"
            elif attempt is not None and attempt.original_error_is_skip:
                outcome = "skipped"
            elif isinstance(result.error, NoResultError):
                outcome = "retrying" if attempt is not None and attempt.original_error_is_skip is False else "skipped"
            else:
                outcome = "failed" if result.is_err else "succeeded"
            self._finish(invocation, outcome, restore=True)
            # The ID is only needed between execution hooks, not in stored
            # results. An abandoned attempt may retain its harmless string.
            message.labels.pop(_INVOCATION_LABEL, None)

        _safe(finish)
