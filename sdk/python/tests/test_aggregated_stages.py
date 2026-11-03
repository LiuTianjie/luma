"""Bounded stage events, exception details and failure isolation owned by the SDK."""

import asyncio
from dataclasses import replace
import importlib
import json
import queue
from types import SimpleNamespace

import pytest

import luma_observe as observe
from luma_observe.diagnostics import ERROR_MESSAGE_BYTES, ERROR_STACK_BYTES, MAX_PHASES
from test_task_runtime import observed as observed

module = importlib.import_module("luma_observe.runtime")
diagnostics = importlib.import_module("luma_observe.diagnostics")


@pytest.fixture
def aggregated(observed):
    runtime, exporter, reader = observed
    runtime.configuration = replace(runtime.configuration, capture_error_details=True)
    runtime._events = queue.Queue(maxsize=256)
    return runtime, exporter, reader


def test_batch_aggregates_on_one_root_with_fixed_event_count(aggregated, monkeypatch):
    runtime, exporter, _reader = aggregated
    clock = [10.0]
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(time=lambda: 1000.0, monotonic=lambda: clock[0]))
    with runtime.task("batch", attributes={"messaging.message.id": "attempt-one"}):
        for index in range(2000):
            with observe.stage("read", {"object.id": str(index)}, aggregate=True):
                clock[0] += 0.001
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    summaries = [event for event in spans[0].events if event.name == "task.phase.finished"]
    assert len(summaries) == 1
    assert summaries[0].attributes["phase.count"] == 2000
    assert summaries[0].attributes["phase.duration_ms"] == pytest.approx(2000)
    records = list(runtime._events.queue)
    assert [record["event"] for record in records] == [
        "task.started", "task.phase.started", "task.phase.finished", "task.finished",
    ]
    assert all(record["attributes"]["messaging.message.id"] == "attempt-one" for record in records)


def test_overflow_keeps_original_failure_and_one_bounded_exception(aggregated):
    runtime, exporter, _reader = aggregated
    original = ValueError("detail " + "字" * 9000)
    with pytest.raises(ValueError) as caught:
        with runtime.task("batch"):
            for index in range(MAX_PHASES + 1):
                with observe.stage(f"step-{index}", {"object.id": str(index)}, aggregate=True):
                    if index == MAX_PHASES:
                        try:
                            raise OSError("source failed")
                        except OSError as cause:
                            raise original from cause
    assert caught.value is original
    span = exporter.get_finished_spans()[0]
    summaries = [event for event in span.events if event.name == "task.phase.finished"]
    assert len(summaries) == MAX_PHASES
    failure = next(event for event in span.events if event.name == "task.failure")
    assert failure.attributes["task.stage"] == f"step-{MAX_PHASES}"
    assert failure.attributes["object.id"] == str(MAX_PHASES)
    assert failure.attributes["error.type"] == "ValueError"
    assert failure.attributes["error.truncated"] is True
    records = [record for record in runtime._events.queue if record["event"] == "task.failure.detail"]
    assert len(records) == 1
    detail = records[0]["exception"]
    assert records[0]["trace_id"] == format(span.context.trace_id, "032x")
    assert "OSError: source failed" in detail["stacktrace"]
    assert len(detail["error_message"].encode("utf-8")) <= ERROR_MESSAGE_BYTES
    assert len(detail["stacktrace"].encode("utf-8")) <= ERROR_STACK_BYTES
    assert detail["truncated"] is True
    assert next(event for event in span.events if event.name == "task.phases.omitted").attributes["phase.omitted"] == 1


def test_changing_attribute_names_stay_bounded_and_preserve_first_failed_item(aggregated):
    runtime, exporter, _reader = aggregated
    with runtime.task("batch") as run:
        for index in range(2000):
            with observe.stage("read", {f"item.field.{index}": index, "phase.count": -1,
                                        "task.stage": "caller-value"}, aggregate=True):
                pass
        assert len(json.dumps(run._stages.rows)) < 1024
        row = run._stages.completed()[0][0]
        assert row["item.field.1999"] == 1999 and "item.field.0" not in row
        assert row["phase.count"] == 2000 and row["task.stage"] == "read"
        with pytest.raises(ValueError):
            with observe.stage("read", {"item.id": "failed"}, aggregate=True):
                raise ValueError("item failed")
        with observe.stage("read", {"item.id": "later"}, aggregate=True):
            pass
    summary = next(event for event in exporter.get_finished_spans()[0].events
                   if event.name == "task.phase.finished")
    assert summary.attributes["phase.count"] == 2002
    assert summary.attributes["phase.outcome"] == "failed"
    assert summary.attributes["item.id"] == "failed"
    first = next(record for record in runtime._events.queue if record["event"] == "task.phase.started")
    assert first["attributes"]["phase.count"] == 0
    assert first["attributes"]["task.stage"] == "read"


@pytest.mark.parametrize("hook", ["start", "finish", "completed", "claim_failure", "emit", "add_event", "mark_progress", "current_task", "exception_details"])
@pytest.mark.parametrize("fails", [False, True])
def test_observation_faults_preserve_business_return_and_exception(aggregated, monkeypatch, hook, fails):
    runtime, _exporter, _reader = aggregated

    def broken(*args, **kwargs):
        raise RuntimeError("telemetry failure")

    target = (diagnostics.StageSummaries if hook in {"start", "finish", "completed", "claim_failure"}
              else module.TaskRun if hook in {"add_event", "mark_progress"}
              else module if hook in {"current_task", "exception_details"} else runtime)
    monkeypatch.setattr(target, hook, broken)
    original = ValueError("business failure")

    def work():
        with runtime.task("sample"):
            with observe.stage("work", aggregate=True):
                if fails:
                    raise original
                return "business result"

    if fails:
        with pytest.raises(ValueError) as caught:
            work()
        assert caught.value is original
    else:
        assert work() == "business result"


def test_full_lifecycle_buffer_drops_telemetry_and_keeps_result(aggregated):
    runtime, exporter, _reader = aggregated
    for _ in range(runtime._events.maxsize):
        runtime._events.put_nowait({"event": "existing"})
    with runtime.task("sample"):
        with observe.stage("work", aggregate=True):
            result = 42
    assert result == 42
    assert runtime._events.qsize() == runtime._events.maxsize
    assert runtime.dropped_events == 4
    assert len(exporter.get_finished_spans()) == 1


def test_error_details_are_opt_in_and_existing_stage_still_creates_child_span(observed):
    runtime, exporter, _reader = observed
    runtime._events = queue.Queue(maxsize=256)
    with pytest.raises(ValueError):
        with runtime.task("sample"):
            with observe.stage("child"):
                pass
            with observe.stage("work", aggregate=True):
                raise ValueError("private text")
    assert len(exporter.get_finished_spans()) == 2
    assert "private text" not in repr(list(runtime._events.queue))
    assert "private text" not in repr([list(span.events) for span in exporter.get_finished_spans()])
    assert not any(record["event"] == "task.failure.detail" for record in runtime._events.queue)


@pytest.mark.asyncio
async def test_concurrent_attempts_and_decorators_keep_independent_summaries(aggregated):
    runtime, exporter, _reader = aggregated
    entered = asyncio.Event()
    count = [0]

    @observe.observed_stage("work", aggregate=True)
    async def step():
        count[0] += 1
        if count[0] == 2:
            entered.set()
        await entered.wait()
        return observe.current_task()._fields["task.name"]

    async def attempt(name):
        with runtime.task(name):
            assert await step() == name

    await asyncio.gather(attempt("one"), attempt("two"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert len(span.events) == 1
        assert span.events[0].attributes["phase.count"] == 1


def test_disabled_sdk_keeps_stage_body_and_emits_nothing(monkeypatch):
    monkeypatch.setattr(module, "_runtime", None)
    monkeypatch.setattr(module, "_configuration", module.Configuration())
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    runtime = observe.configure(capture_error_details=True, lifecycle_logs=True)
    with runtime.task("disabled"):
        with observe.stage("work", aggregate=True):
            result = 42
    assert result == 42
    assert not runtime.enabled
    assert runtime._events is None
    runtime.shutdown()
