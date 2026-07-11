from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    AnalyzeSourceRequest,
    BuilderTask,
    BuilderTaskEventPage,
    BuilderTaskMutation,
    BuildPlanRequest,
    LumaCallContext,
)


@runtime_checkable
class LumaBuilderAdapter(Protocol):
    """The Builder Task subset consumed by the durable LAE worker."""

    def create_analyze_task(
        self,
        context: LumaCallContext,
        request: AnalyzeSourceRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation: ...

    def create_build_task(
        self,
        context: LumaCallContext,
        request: BuildPlanRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation: ...

    def get_builder_task(
        self, context: LumaCallContext, task_id: str
    ) -> BuilderTask: ...

    def get_builder_task_events(
        self,
        context: LumaCallContext,
        task_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> BuilderTaskEventPage: ...

    def cancel_builder_task(
        self, context: LumaCallContext, task_id: str
    ) -> BuilderTaskMutation: ...

    # Design-document vocabulary retained as typed aliases for worker code.
    def analyze_source(
        self,
        context: LumaCallContext,
        request: AnalyzeSourceRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation: ...

    def build_plan(
        self,
        context: LumaCallContext,
        request: BuildPlanRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation: ...

    def watch_builder_task(
        self,
        context: LumaCallContext,
        task_id: str,
        *,
        cursor: int = 0,
        limit: int = 200,
    ) -> BuilderTaskEventPage: ...
