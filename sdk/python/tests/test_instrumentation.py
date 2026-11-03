"""Exercise supported framework execution paths with real in-memory OTel providers."""

import asyncio
from copy import deepcopy
import json
import os
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from celery import Celery
from celery.exceptions import Ignore, Reject
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from taskiq import AckableMessage, AsyncBroker, InMemoryBroker, SmartRetryMiddleware, TaskiqMessage
from taskiq import TaskiqMiddleware as BaseMiddleware
from taskiq.exceptions import NoResultError

import luma_observe.runtime as core
import luma_observe.instrumentation as hooks
import luma_observe.instrumentation.celery as celery_hooks
import luma_observe.instrumentation.taskiq as taskiq_hooks


@pytest.fixture
def observed(monkeypatch):
    """Swap only network exporters/readers while exercising actual Runtime behavior."""
    for key, value in {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_TRACES_SAMPLER": "always_on",
        "OTEL_SDK_DISABLED": "false",
        "LUMA_OBSERVE_ENABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    monkeypatch.setattr(core, "OTLPSpanExporter", lambda **_kwargs: exporter)
    monkeypatch.setattr(core, "OTLPMetricExporter", lambda **_kwargs: Mock(_preferred_temporality=None, _preferred_aggregation=None))
    monkeypatch.setattr(core, "BatchSpanProcessor", lambda exporter, **_kwargs: SimpleSpanProcessor(exporter))
    monkeypatch.setattr(core, "PeriodicExportingMetricReader", lambda *_args, **_kwargs: reader)
    runtime = core.Runtime(core.Configuration(service_name="adapter-test"))
    assert runtime.enabled
    monkeypatch.setattr(core, "_configuration", runtime.configuration)
    monkeypatch.setattr(core, "_runtime", runtime)
    for module in (hooks, taskiq_hooks, celery_hooks):
        monkeypatch.setattr(module, "get_runtime", lambda: runtime)
    yield runtime, reader, exporter
    runtime.shutdown()


def points(reader, name):
    data = reader.get_metrics_data()
    return [point for resource in data.resource_metrics for scope in resource.scope_metrics
            for metric in scope.metrics if metric.name == name for point in metric.data.data_points]


def outcomes(reader):
    result = {}
    for point in points(reader, "task.executions"):
        outcome = point.attributes["outcome"]
        result[outcome] = result.get(outcome, 0) + point.value
    return result


class CopyingMiddleware(BaseMiddleware):
    """A supported downstream middleware may return a copied or rebuilt message."""

    def __init__(self, mode="rebuild"):
        super().__init__()
        self.mode = mode

    def pre_execute(self, message):
        if self.mode == "deepcopy":
            return deepcopy(message)
        if self.mode == "model_copy":
            return message.model_copy(deep=True)
        return TaskiqMessage.model_validate(message.model_dump())


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_mode", ["deepcopy", "model_copy", "rebuild"])
async def test_taskiq_message_replacement_preserves_execution_ack_and_observation(observed, copy_mode):
    """Downstream copying must preserve business outcomes and completion telemetry."""
    runtime, reader, exporter = observed
    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(), CopyingMiddleware(copy_mode))

    @broker.task(task_name="document.copyable")
    async def work(value):
        if value < 0:
            raise ValueError("application failure")
        return value * 2

    try:
        with runtime.span("caller") as parent:
            for value in (21, -1):
                message = TaskiqMessage(task_id=str(value), task_name=work.task_name, labels={}, args=[value], kwargs={})
                acknowledgement = AsyncMock()
                await broker.receiver.callback(
                    AckableMessage(data=broker.formatter.dumps(message).message, ack=acknowledgement),
                    raise_err=True,
                )
                acknowledgement.assert_awaited_once()
                result = await broker.result_backend.get_result(message.task_id)
                if value > 0:
                    assert result.return_value == 42 and not result.is_err
                else:
                    assert result.is_err and isinstance(result.error, ValueError)
                assert trace.get_current_span() is parent
        assert outcomes(reader) == {"succeeded": 1, "failed": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        attempts = [span for span in exporter.get_finished_spans() if span.name == work.task_name]
        assert len(attempts) == 2
        assert {span.attributes["task.outcome"] for span in attempts} == {"succeeded", "failed"}
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", [False, True])
async def test_taskiq_receiver_preserves_results_and_ack_without_post_save(observed, skip):
    """Actual Receiver still acknowledges skipped work and leaves its result absent."""
    _runtime, reader, exporter = observed
    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware())

    @broker.task(task_name="document.render")
    async def render(value):
        assert core.current_task() is not None
        assert core.current_task().span is trace.get_current_span()
        if skip:
            raise NoResultError()
        return value * 2

    message = TaskiqMessage(task_id="job-001", task_name=render.task_name, labels={}, args=[21], kwargs={})
    broker.middlewares[0].pre_send(message)
    acknowledgement = AsyncMock()
    try:
        await broker.receiver.callback(
            AckableMessage(data=broker.formatter.dumps(message).message, ack=acknowledgement),
            raise_err=True,
        )
        acknowledgement.assert_awaited_once()
        assert await broker.result_backend.is_result_ready(message.task_id) is not skip
        if not skip:
            assert (await broker.result_backend.get_result(message.task_id)).return_value == 42
        assert outcomes(reader) == {"skipped" if skip else "succeeded": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        preparation, execution = exporter.get_finished_spans()
        assert execution.parent is None
        assert execution.links[0].context.span_id == preparation.context.span_id
        assert execution.context.trace_id != preparation.context.trace_id
        assert execution.attributes["messaging.message.id"] == "job-001"
        assert not trace.get_current_span().get_span_context().is_valid
        for point in points(reader, "task.executions"):
            assert "messaging.message.id" not in point.attributes
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_real_retry_middleware_keeps_success_and_separate_attempts(observed):
    """SmartRetry's NoResultError conversion cannot overwrite the successful retry."""
    _runtime, reader, exporter = observed
    retry = SmartRetryMiddleware(default_retry_count=2, default_retry_label=True,
                                 default_delay=0, use_jitter=False)
    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(), CopyingMiddleware(), retry)
    calls = []

    @broker.task(task_name="document.retry")
    async def work():
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("private exception content")
        return "delivered"

    try:
        task = await work.kiq()
        result = await broker.result_backend.get_result(task.task_id)
        assert result.return_value == "delivered" and not result.is_err
        assert len(calls) == 2
        assert outcomes(reader) == {"succeeded": 1, "retrying": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        assert "private exception content" not in str([span.to_json() for span in exporter.get_finished_spans()])
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_concurrent_same_id_contexts_are_isolated(observed):
    """Duplicate deliveries have independent contexts even with the same message ID."""
    _runtime, reader, exporter = observed
    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(), CopyingMiddleware())
    ready = asyncio.Event()
    contexts = []

    @broker.task(task_name="document.concurrent")
    async def work():
        context = trace.get_current_span().get_span_context()
        contexts.append(context)
        if len(contexts) == 2:
            ready.set()
        await ready.wait()
        assert trace.get_current_span().get_span_context() == context
        return "done"

    async def receive():
        message = TaskiqMessage(task_id="duplicate", task_name=work.task_name, args=[], kwargs={}, labels={})
        await broker.receiver.callback(broker.formatter.dumps(message).message)

    try:
        await asyncio.wait_for(asyncio.gather(receive(), receive()), timeout=2)
        assert len({context.span_id for context in contexts}) == 2
        assert outcomes(reader) == {"succeeded": 2}
        assert len(exporter.get_finished_spans()) == 2
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_broker_lifecycles_do_not_close_shared_runtime(observed, monkeypatch):
    """Standard broker lifecycles may end or restart while another broker is active."""
    runtime, reader, exporter = observed
    monkeypatch.setattr(core, "_runtime", runtime)
    flushed = Mock(wraps=runtime.force_flush)
    monkeypatch.setattr(runtime, "force_flush", flushed)
    first = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware())
    second = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware())

    @first.task(task_name="document.first")
    async def first_task():
        return 1

    @second.task(task_name="document.second")
    async def second_task():
        return 2

    # InMemoryBroker skips middleware lifecycle hooks. Exercise the public base
    # lifecycle used by production brokers, retaining its real in-memory receiver.
    try:
        await AsyncBroker.startup(first)
        await AsyncBroker.startup(second)
        initial = await first_task.kiq()
        assert (await initial.wait_result()).return_value == 1
        await AsyncBroker.shutdown(first)
        flushed.assert_called_once_with(None)
        assert runtime.enabled and not runtime._closed.is_set()

        continuing = await second_task.kiq()
        assert (await continuing.wait_result()).return_value == 2
        await AsyncBroker.startup(first)
        restarted = await first_task.kiq()
        assert (await restarted.wait_result()).return_value == 1

        assert outcomes(reader) == {"succeeded": 3}
        assert len(exporter.get_finished_spans()) == 6
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        await AsyncBroker.shutdown(first)
        await AsyncBroker.shutdown(second)
        await first.shutdown()
        await second.shutdown()


@pytest.mark.asyncio
async def test_taskiq_nested_setup_failure_does_not_finish_parent_attempt(observed):
    """An unobserved inline child must not consume its parent's execution state."""
    _runtime, reader, exporter = observed
    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(), CopyingMiddleware())

    @broker.task(task_name="document.child")
    async def child():
        return 21

    @broker.task(task_name="document.parent")
    async def parent():
        current = trace.get_current_span()
        # Invalid optional telemetry metadata makes SDK setup fail open.
        submitted = await child.kicker().with_labels(_retries="invalid").kiq()
        result = await submitted.wait_result()
        assert trace.get_current_span() is current and current.is_recording()
        return result.return_value * 2

    try:
        submitted = await parent.kiq()
        assert (await submitted.wait_result()).return_value == 42
        counts = points(reader, "task.executions")
        assert [(point.attributes["task.name"], point.value) for point in counts] == [(parent.task_name, 1)]
        attempts = [span for span in exporter.get_finished_spans() if span.name == parent.task_name]
        assert len(attempts) == 1 and attempts[0].attributes["task.outcome"] == "succeeded"
        assert sum(point.value for point in points(reader, "task.active")) == 0
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_observation_initialization_failure_keeps_execution(observed, monkeypatch):
    """An SDK setup error cannot prevent enqueueing or processing the business task."""
    monkeypatch.setattr(hooks, "get_runtime", Mock(side_effect=RuntimeError("setup failed")))
    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware())

    @broker.task(task_name="document.unobserved")
    async def work():
        return 42

    try:
        task = await work.kiq()
        assert (await broker.result_backend.get_result(task.task_id)).return_value == 42
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pre_execute", "on_error", "post_execute"])
async def test_taskiq_aborted_hooks_close_without_changing_exception_or_ack(observed, caplog, stage):
    """A successful enclosing Task can contain an aborted Receiver invocation."""
    _runtime, reader, exporter = observed
    expected = RuntimeError("downstream middleware unavailable")
    release = asyncio.Event()
    inherited_tasks = []

    class FailingMiddleware(BaseMiddleware):
        def pre_execute(self, message):
            async def inherited_context():
                await release.wait()
                return core.current_task()

            inherited_tasks.append(asyncio.create_task(inherited_context()))
            assert core.current_task() is not None
            if stage == "pre_execute":
                raise expected
            return message

        def on_error(self, message, result, exception):
            if stage == "on_error":
                raise expected

        def post_execute(self, message, result):
            if stage == "post_execute":
                raise expected

    broker = InMemoryBroker(await_inplace=True).with_middlewares(
        taskiq_hooks.TaskiqMiddleware(), CopyingMiddleware(), FailingMiddleware(),
    )

    @broker.task(task_name="document.aborted")
    async def work():
        if stage == "on_error":
            raise ValueError("business error")
        return 42

    acknowledgement = AsyncMock()
    message = TaskiqMessage(task_id="aborted", task_name=work.task_name, labels={}, args=[], kwargs={})

    async def receive_and_catch():
        try:
            await broker.receiver.callback(
                AckableMessage(data=broker.formatter.dumps(message).message, ack=acknowledgement),
                raise_err=True,
            )
        except RuntimeError as error:
            assert error is expected
            return "caught by application"
        raise AssertionError("original middleware exception was swallowed")

    try:
        assert await asyncio.create_task(receive_and_catch()) == "caught by application"
        await asyncio.sleep(0)  # Give the owning Task's done callbacks a turn.
        release.set()
        assert await asyncio.gather(*inherited_tasks) == [None]
        acknowledgement.assert_not_awaited()
        assert not await broker.result_backend.is_result_ready(message.task_id)
        assert outcomes(reader) == {"failed": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        attempts = [span for span in exporter.get_finished_spans() if span.name == work.task_name]
        assert len(attempts) == 1 and attempts[0].attributes["task.outcome"] == "failed"
        assert core.current_task() is None
        assert not trace.get_current_span().get_span_context().is_valid
        assert "Failed to detach context" not in caplog.text
    finally:
        release.set()
        await asyncio.gather(*inherited_tasks)
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_cancelled_pre_hook_closes_attempt(observed, caplog):
    _runtime, reader, exporter = observed
    entered = asyncio.Event()

    class WaitingMiddleware(BaseMiddleware):
        async def pre_execute(self, message):
            entered.set()
            await asyncio.Event().wait()
            return message

    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(), WaitingMiddleware())

    @broker.task(task_name="document.cancelled")
    async def work():
        raise AssertionError("cancelled pre-hook must not execute business work")

    message = TaskiqMessage(task_id="cancelled", task_name=work.task_name, labels={}, args=[], kwargs={})
    acknowledgement = AsyncMock()
    owner = asyncio.create_task(broker.receiver.callback(
        AckableMessage(data=broker.formatter.dumps(message).message, ack=acknowledgement),
    ))
    try:
        await entered.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await asyncio.sleep(0)
        assert outcomes(reader) == {"cancelled": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        assert len(exporter.get_finished_spans()) == 1
        acknowledgement.assert_not_awaited()
        assert "Failed to detach context" not in caplog.text
    finally:
        if not owner.done():
            owner.cancel()
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_cancelled_body_uses_framework_post_execute_and_keeps_ack(observed, caplog):
    """Receiver catches body cancellation into its result and still completes hooks."""
    _runtime, reader, exporter = observed
    entered = asyncio.Event()
    completed_results = []

    class CompletionMiddleware(BaseMiddleware):
        def post_execute(self, message, result):
            completed_results.append(result)

    broker = InMemoryBroker(await_inplace=True).with_middlewares(
        taskiq_hooks.TaskiqMiddleware(), CompletionMiddleware(),
    )

    @broker.task(task_name="document.cancelled-body")
    async def work():
        entered.set()
        await asyncio.Event().wait()

    message = TaskiqMessage(task_id="cancelled-body", task_name=work.task_name, labels={}, args=[], kwargs={})
    acknowledgement = AsyncMock()
    owner = asyncio.create_task(broker.receiver.callback(
        AckableMessage(data=broker.formatter.dumps(message).message, ack=acknowledgement),
        raise_err=True,
    ))
    try:
        await entered.wait()
        owner.cancel()
        assert await owner is None
        assert not owner.cancelled()
        assert len(completed_results) == 1
        result = await broker.result_backend.get_result(message.task_id)
        assert result.is_err and isinstance(result.error, asyncio.CancelledError)
        acknowledgement.assert_awaited_once()
        assert outcomes(reader) == {"cancelled": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        attempts = exporter.get_finished_spans()
        assert len(attempts) == 1 and attempts[0].attributes["task.outcome"] == "cancelled"
        assert core.current_task() is None
        assert not trace.get_current_span().get_span_context().is_valid
        assert "Failed to detach context" not in caplog.text
    finally:
        if not owner.done():
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        await broker.shutdown()


@pytest.mark.asyncio
async def test_taskiq_nested_same_id_rebuilt_aborted_child_keeps_parent_completion(observed, caplog):
    """Invocation IDs distinguish inline attempts even when message identity matches."""
    runtime, reader, exporter = observed
    expected = RuntimeError("child pre-hook failed")
    child_ack = AsyncMock()

    class FailSecondAttempt(BaseMiddleware):
        calls = 0

        def pre_execute(self, message):
            self.calls += 1
            if self.calls == 2:
                raise expected
            return message

    broker = InMemoryBroker(await_inplace=True).with_middlewares(
        taskiq_hooks.TaskiqMiddleware(), CopyingMiddleware(), FailSecondAttempt(),
    )

    @broker.task(task_name="document.same-id")
    async def work():
        assert core.current_task() is not None
        child = TaskiqMessage(task_id="same-id", task_name=work.task_name, labels={}, args=[], kwargs={})
        try:
            await broker.receiver.callback(
                AckableMessage(data=broker.formatter.dumps(child).message, ack=child_ack), raise_err=True,
            )
        except RuntimeError as error:
            assert error is expected
        else:
            raise AssertionError("child pre-hook error was not preserved")
        return 42

    message = TaskiqMessage(task_id="same-id", task_name=work.task_name, labels={}, args=[], kwargs={})
    parent_ack = AsyncMock()
    try:
        with runtime.span("caller") as caller:
            await broker.receiver.callback(
                AckableMessage(data=broker.formatter.dumps(message).message, ack=parent_ack), raise_err=True,
            )
            assert trace.get_current_span() is caller
            assert core.current_task() is None
        assert (await broker.result_backend.get_result(message.task_id)).return_value == 42
        parent_ack.assert_awaited_once()
        child_ack.assert_not_awaited()
        assert outcomes(reader) == {"failed": 1, "succeeded": 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        attempts = [span for span in exporter.get_finished_spans() if span.name == work.task_name]
        assert len(attempts) == 2
        assert len({span.context.span_id for span in attempts}) == 2
        assert {span.attributes["task.outcome"] for span in attempts} == {"failed", "succeeded"}
        assert "Failed to detach context" not in caplog.text
    finally:
        await broker.shutdown()


def test_taskiq_read_only_labels_fail_open_without_consuming_parent(observed):
    runtime, reader, _exporter = observed
    middleware = taskiq_hooks.TaskiqMiddleware()
    middleware.set_broker(SimpleNamespace(queue_name="documents"))
    message = TaskiqMessage(task_id="read-only", task_name="document.read-only", labels={}, args=[], kwargs={})
    object.__setattr__(message, "labels", MappingProxyType({"custom": "unchanged"}))
    with runtime.task("application.parent") as parent:
        assert middleware.pre_execute(message) is message
        middleware.post_execute(message, SimpleNamespace(is_err=False, error=None))
        assert core.current_task() is parent
    assert message.labels == {"custom": "unchanged"}
    assert outcomes(reader) == {"succeeded": 1}


def test_taskiq_shutdown_flushes_batch_before_process_exit():
    """An os._exit worker bypasses atexit; the broker hook must export its batch."""
    source = '''
import asyncio
import json
import os
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from taskiq import AsyncBroker, InMemoryBroker
import luma_observe.runtime as core
from luma_observe.instrumentation.taskiq import TaskiqMiddleware

class Sink(SpanExporter):
    def export(self, spans):
        print(json.dumps([span.name for span in spans]), flush=True)
        return SpanExportResult.SUCCESS

core.OTLPSpanExporter = lambda **kwargs: Sink()
core.configure("flush-worker", export_interval_ms=60000)
broker = InMemoryBroker(await_inplace=True).with_middlewares(TaskiqMiddleware())

@broker.task(task_name="document.flush")
async def work():
    return 42

async def main():
    submitted = await work.kiq()
    assert (await submitted.wait_result()).return_value == 42
    await AsyncBroker.shutdown(broker)
    os._exit(0)

asyncio.run(main())
'''
    process_env = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid",
                   "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", "OTEL_TRACES_EXPORTER": "otlp",
                   "OTEL_METRICS_EXPORTER": "none", "OTEL_TRACES_SAMPLER": "always_on",
                   "OTEL_SDK_DISABLED": "false", "LUMA_OBSERVE_ENABLED": "true"}
    result = subprocess.run([sys.executable, "-c", source], env=process_env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    exported = [name for line in result.stdout.splitlines() for name in json.loads(line)]
    assert exported == ["task.prepare/document.flush", "document.flush"]


def test_disabled_adapters_leave_carriers_untouched(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    message = TaskiqMessage(task_id="job-001", task_name="document.render", labels={"custom": "value"}, args=[], kwargs={})
    middleware = taskiq_hooks.TaskiqMiddleware()
    middleware.set_broker(SimpleNamespace(queue_name="documents"))
    assert middleware.pre_send(message) is message
    assert middleware.pre_execute(message) is message
    assert message.labels == {"custom": "value"}
    assert hooks._attempt(message) is None
    middleware.post_execute(message, SimpleNamespace(is_err=False, error=None))
    headers = {"id": "job-001"}
    celery_hooks.CeleryInstrumentation()._before_publish(sender="document.render", headers=headers)
    assert headers == {"id": "job-001"}


@pytest.mark.parametrize("mode,expected", [
    ("success", "succeeded"), ("failure", "failed"), ("ignore", "skipped"), ("reject", "skipped"),
])
def test_celery_eager_execution_preserves_outcomes_and_cleans_context(observed, mode, expected):
    """Run real Celery tracing, including signal paths that skip result storage."""
    _runtime, reader, exporter = observed
    instrumentation = celery_hooks.instrument_celery()
    assert celery_hooks.instrument_celery() is instrumentation
    app = Celery("adapter-test", broker="memory://", backend="cache+memory://")

    @app.task(name="document.celery")
    def work(value):
        if mode == "failure":
            raise ValueError("private exception content")
        if mode == "ignore":
            raise Ignore()
        if mode == "reject":
            raise Reject(requeue=False)
        return value * 2

    try:
        result = work.apply(args=[21], throw=False)
        if mode == "success":
            assert result.get() == 42
        elif mode == "failure":
            assert result.failed() and isinstance(result.result, ValueError)
        assert outcomes(reader) == {expected: 1}
        assert sum(point.value for point in points(reader, "task.active")) == 0
        assert not trace.get_current_span().get_span_context().is_valid
        assert "private exception content" not in str([span.to_json() for span in exporter.get_finished_spans()])
    finally:
        instrumentation.uninstrument()
        app.close()


def test_celery_retry_executes_original_task_again(observed):
    """Eager Celery owns the retry and final result; SDK records both attempts."""
    _runtime, reader, _exporter = observed
    instrumentation = celery_hooks.instrument_celery()
    app = Celery("adapter-retry", broker="memory://", backend="cache+memory://")

    @app.task(name="document.celery.retry", bind=True, max_retries=1)
    def work(self):
        if not self.request.retries:
            raise self.retry(exc=ValueError("private retry content"), countdown=0)
        return 42

    try:
        assert work.apply(throw=False).get() == 42
        assert outcomes(reader) == {"retrying": 1, "succeeded": 1}
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        instrumentation.uninstrument()
        app.close()


def test_celery_observation_failure_preserves_task_return(observed, monkeypatch):
    monkeypatch.setattr(hooks, "get_runtime", Mock(side_effect=RuntimeError("setup failed")))
    instrumentation = celery_hooks.instrument_celery()
    app = Celery("adapter-disabled", broker="memory://", backend="cache+memory://")

    @app.task(name="document.celery.unobserved")
    def work():
        return 42

    try:
        assert work.apply(throw=False).get() == 42
    finally:
        instrumentation.uninstrument()
        app.close()


def test_celery_shutdown_signal_uses_bounded_runtime_shutdown(monkeypatch):
    stopped = Mock()
    monkeypatch.setattr(celery_hooks, "shutdown", stopped)
    instrumentation = celery_hooks.instrument_celery()
    try:
        celery_hooks.signals.worker_process_shutdown.send(sender=None)
        stopped.assert_called_once_with()
    finally:
        instrumentation.uninstrument()


@pytest.mark.asyncio
async def test_taskiq_attribute_callback_and_worker_registration(observed):
    """Application fields reach the attempt; a failing callback never blocks execution."""
    _runtime, reader, exporter = observed
    calls = []

    def attributes(name, args, kwargs):
        calls.append((name, args, kwargs))
        if kwargs.get("explode"):
            raise RuntimeError("application callback bug")
        return {"document.id": args[0], "messaging.message.id": "spoofed", "ignored": object()}

    middleware = taskiq_hooks.TaskiqMiddleware(attributes=attributes)
    broker = InMemoryBroker(await_inplace=True).with_middlewares(middleware)
    broker.is_worker_process = True

    @broker.task(task_name="document.render")
    async def render(document_id, explode=False):
        return document_id

    await broker.startup()
    # InMemoryBroker skips middleware startup; network brokers call it in every process.
    middleware.startup()
    try:
        assert (await (await render.kiq("doc-1")).wait_result()).return_value == "doc-1"
        assert (await (await render.kiq("doc-2", explode=True)).wait_result()).return_value == "doc-2"
    finally:
        await broker.shutdown()
    assert calls[0] == ("document.render", ("doc-1",), {})
    attempts = [span for span in exporter.get_finished_spans() if span.name == "document.render"]
    assert attempts[0].attributes["document.id"] == "doc-1"
    assert attempts[0].attributes["messaging.message.id"] != "spoofed"
    assert "ignored" not in attempts[0].attributes
    assert "document.id" not in attempts[1].attributes
    assert outcomes(reader) == {"succeeded": 2}
    registered = {(p.attributes["task.name"], p.attributes["task.framework"]) for p in points(reader, "task.registered")}
    assert ("document.render", "taskiq") in registered
    assert [p.attributes["task.role"] for p in points(reader, "task.process.started")] == ["worker"]


def test_celery_attribute_callback_and_pool_registration(observed):
    _runtime, reader, exporter = observed
    app = Celery("adapter-attributes", broker="memory://", backend="cache+memory://")
    instrumentation = celery_hooks.instrument_celery(attributes=lambda name, args, kwargs: {"document.id": args[0]}, app=app)

    @app.task(name="document.export")
    def export(document_id):
        return document_id

    try:
        assert export.apply(args=["doc-9"]).get() == "doc-9"
        instrumentation._worker_started()
        instrumentation._beat_started()
    finally:
        instrumentation.uninstrument()
        celery_hooks.instrument_celery().uninstrument()
        app.close()
    span = next(span for span in exporter.get_finished_spans() if span.name == "document.export")
    assert span.attributes["document.id"] == "doc-9"
    names = {p.attributes["task.name"] for p in points(reader, "task.registered")}
    assert "document.export" in names and not any(name.startswith("celery.") for name in names)
    assert sorted(p.attributes["task.role"] for p in points(reader, "task.process.started")) == ["scheduler", "worker"]
    assert celery_hooks.instrument_celery().attributes is None


@pytest.mark.asyncio
async def test_attribute_callback_receives_bound_arguments_when_declared(observed):
    _runtime, _reader, exporter = observed
    seen = []

    def with_arguments(name, args, kwargs, arguments):
        seen.append(arguments)
        return {"document.id": arguments.get("document_id", "")}

    def legacy(name, args, kwargs):
        return {"legacy": len(args)}

    broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(attributes=with_arguments))
    legacy_broker = InMemoryBroker(await_inplace=True).with_middlewares(taskiq_hooks.TaskiqMiddleware(attributes=legacy))

    @broker.task(task_name="document.bind")
    async def bind(document_id, mode="fast"):
        return document_id

    @legacy_broker.task(task_name="document.legacy")
    async def legacy_task(document_id):
        return document_id

    await bind.kiq("doc-1")
    await bind.kiq(document_id="doc-2", mode="slow")
    await legacy_task.kiq("doc-3")
    assert seen == [{"document_id": "doc-1"}, {"document_id": "doc-2", "mode": "slow"}]
    # A keyword the signature does not declare must not hide the positional binding.
    assert hooks._bind(bind.original_func, ["doc-4"], {"legacy_flag": True}) == {"document_id": "doc-4", "legacy_flag": True}
    attempts = {span.name: span for span in exporter.get_finished_spans() if span.name.startswith("document.")}
    assert attempts["document.bind"].attributes["document.id"] == "doc-2"
    assert attempts["document.legacy"].attributes["legacy"] == 1

    app = Celery("adapter-bind", broker="memory://", backend="cache+memory://")
    instrumentation = celery_hooks.instrument_celery(attributes=with_arguments, app=app)

    @app.task(name="document.celery_bind")
    def celery_bind(document_id, mode="fast"):
        return document_id

    try:
        assert celery_bind.apply(args=["doc-9"], kwargs={"mode": "slow"}).get() == "doc-9"
    finally:
        instrumentation.uninstrument()
        celery_hooks.instrument_celery().uninstrument()
        app.close()
    assert seen[-1] == {"document_id": "doc-9", "mode": "slow"}


def test_attribute_lookup_failure_does_not_block_attempt(observed):
    """A broker without find_task (or one that raises) still yields an observed attempt."""
    _runtime, reader, exporter = observed
    middleware = taskiq_hooks.TaskiqMiddleware(attributes=lambda name, args, kwargs, arguments: {"seen": len(arguments)})
    middleware.set_broker(SimpleNamespace(queue_name="documents"))
    message = TaskiqMessage(task_id="t-1", task_name="document.plain", labels={}, args=["a"], kwargs={})
    middleware.pre_execute(message)
    assert core.current_task() is not None
    middleware.post_execute(message, SimpleNamespace(is_err=False, error=None))
    span = next(span for span in exporter.get_finished_spans() if span.name == "document.plain")
    assert span.attributes["seen"] == 0
    assert outcomes(reader) == {"succeeded": 1}
