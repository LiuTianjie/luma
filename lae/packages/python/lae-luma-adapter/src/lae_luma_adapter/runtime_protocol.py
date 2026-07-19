from __future__ import annotations

from typing import Protocol, runtime_checkable

from .runtime_models import (
    RuntimeCallContext,
    RuntimeDeployment,
    RuntimeManifest,
    RuntimeLogTail,
    RuntimeMetricsHistory,
    RuntimeMutation,
    RuntimeVolumeBinding,
    RuntimeVolumeSpec,
)


@runtime_checkable
class LumaRuntimeAdapter(Protocol):
    """Dedicated, scoped LAE runtime subset exposed by Luma Control."""

    def prepare_volumes(
        self,
        context: RuntimeCallContext,
        volumes: tuple[RuntimeVolumeSpec, ...],
        *,
        idempotency_key: str,
    ) -> tuple[RuntimeVolumeBinding, ...]: ...

    def deploy_revision(
        self,
        context: RuntimeCallContext,
        manifest: RuntimeManifest,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation: ...

    def get_runtime_deployment(
        self, context: RuntimeCallContext, deployment_ref: str
    ) -> RuntimeDeployment: ...

    def cancel_runtime_deployment(
        self, context: RuntimeCallContext, deployment_ref: str
    ) -> RuntimeMutation: ...

    def suspend_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation: ...

    def resume_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation: ...

    def restart_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation: ...

    def rollback_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        target_context: RuntimeCallContext,
        target_deployment_ref: str,
        idempotency_key: str,
    ) -> RuntimeMutation: ...

    def delete_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        volume_policy: str,
        idempotency_key: str,
    ) -> RuntimeMutation: ...

    def tail_runtime_logs(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
        *,
        tail: int = 120,
    ) -> RuntimeLogTail: ...

    def get_runtime_metrics_history(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
        *,
        window_seconds: int = 3600,
    ) -> RuntimeMetricsHistory: ...


__all__ = ["LumaRuntimeAdapter"]
