from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass

from ._codec import validate_idempotency_key
from .errors import AdapterErrorCode, LumaAdapterError
from .runtime_models import (
    RuntimeCallContext,
    RuntimeDeployment,
    RuntimeManifest,
    RuntimeLogTail,
    RuntimeMetricsHistory,
    RuntimeMutation,
    RuntimeServicePrincipal,
    RuntimeVolumeBinding,
    RuntimeVolumeSpec,
)


@dataclass(slots=True)
class _RuntimeRecord:
    owner: str
    context: RuntimeCallContext
    manifest: RuntimeManifest
    deployment: RuntimeDeployment


class FakeLumaRuntime:
    """Deterministic scoped runtime backend for deployment worker tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._principals: dict[str, str] = {}
        self._deployments: dict[str, _RuntimeRecord] = {}
        self._deploy_idempotency: dict[tuple[str, ...], tuple[str, str]] = {}
        self._volume_idempotency: dict[
            tuple[str, ...], tuple[str, tuple[RuntimeVolumeBinding, ...]]
        ] = {}
        self._lifecycle_idempotency: dict[
            tuple[str, ...], tuple[str, RuntimeDeployment]
        ] = {}
        self._counter = 0
        self.fail_next_deploy = False
        self.fail_next_prepare = False

    def bind(self, principal: RuntimeServicePrincipal) -> "FakeLumaRuntimeAdapter":
        with self._lock:
            existing = self._principals.get(principal.principal_id)
            if existing is not None and not secrets.compare_digest(
                existing, principal.token
            ):
                raise LumaAdapterError(AdapterErrorCode.UNAUTHORIZED)
            self._principals[principal.principal_id] = principal.token
        return FakeLumaRuntimeAdapter(self, principal)

    def set_health(
        self,
        deployment_ref: str,
        *,
        status: str = "running",
        services: dict[str, str] | None = None,
        routes: dict[str, str] | None = None,
    ) -> RuntimeDeployment:
        with self._lock:
            record = self._deployments.get(deployment_ref)
            if record is None:
                raise LumaAdapterError(AdapterErrorCode.NOT_FOUND, http_status=404)
            service_statuses = services or {
                service.key: "healthy" for service in record.manifest.services
            }
            route_statuses = routes or {
                route.hostname: "ready" for route in record.manifest.routes
            }
            record.deployment = RuntimeDeployment(
                deployment_ref=deployment_ref,
                status=status,
                manifest_digest=record.manifest.manifest_digest,
                service_statuses=service_statuses,
                route_statuses=route_statuses,
                volume_bindings=record.deployment.volume_bindings,
            )
            return record.deployment

    def _authenticate(self, principal: RuntimeServicePrincipal) -> str:
        expected = self._principals.get(principal.principal_id)
        if expected is None or not secrets.compare_digest(expected, principal.token):
            raise LumaAdapterError(AdapterErrorCode.UNAUTHORIZED)
        return principal.principal_id

    @staticmethod
    def _scope(
        principal_id: str, context: RuntimeCallContext, key: str
    ) -> tuple[str, ...]:
        return (
            principal_id,
            context.tenant_ref,
            context.application_ref,
            context.operation_ref,
            context.revision_ref,
            context.deployment_ref,
            validate_idempotency_key(key),
        )

    @staticmethod
    def _hash(value: object) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()

    def _prepare(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        volumes: tuple[RuntimeVolumeSpec, ...],
        *,
        idempotency_key: str,
    ) -> tuple[RuntimeVolumeBinding, ...]:
        principal_id = self._authenticate(principal)
        scope = self._scope(principal_id, context, idempotency_key)
        request_hash = self._hash([volume.to_wire() for volume in volumes])
        with self._lock:
            existing = self._volume_idempotency.get(scope)
            if existing is not None:
                if existing[0] != request_hash:
                    raise LumaAdapterError(
                        AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                    )
                return existing[1]
            if self.fail_next_prepare:
                self.fail_next_prepare = False
                raise LumaAdapterError(
                    AdapterErrorCode.UPSTREAM_UNAVAILABLE, retryable=False
                )
            bindings = tuple(
                RuntimeVolumeBinding(
                    volume.key,
                    volume.existing_ref
                    or f"lv_{hashlib.sha256((context.application_ref + ':' + volume.key).encode()).hexdigest()[:24]}",
                )
                for volume in volumes
            )
            self._volume_idempotency[scope] = (request_hash, bindings)
            return bindings

    def _deploy(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        manifest: RuntimeManifest,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        principal_id = self._authenticate(principal)
        scope = self._scope(principal_id, context, idempotency_key)
        request_hash = self._hash(manifest.to_wire())
        with self._lock:
            existing = self._deploy_idempotency.get(scope)
            if existing is not None:
                if existing[0] != request_hash:
                    raise LumaAdapterError(
                        AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                    )
                return RuntimeMutation(
                    self._deployments[existing[1]].deployment, replayed=True
                )
            if self.fail_next_deploy:
                self.fail_next_deploy = False
                raise LumaAdapterError(
                    AdapterErrorCode.UPSTREAM_UNAVAILABLE, retryable=False
                )
            self._counter += 1
            deployment_ref = f"lae-run-{self._counter:08d}"
            bindings = tuple(
                RuntimeVolumeBinding(
                    volume.key,
                    volume.existing_ref
                    or f"lv_{hashlib.sha256((context.application_ref + ':' + volume.key).encode()).hexdigest()[:24]}",
                )
                for volume in manifest.volumes
            )
            deployment = RuntimeDeployment(
                deployment_ref=deployment_ref,
                status="deploying",
                manifest_digest=manifest.manifest_digest,
                service_statuses={
                    service.key: "pending" for service in manifest.services
                },
                route_statuses={route.hostname: "pending" for route in manifest.routes},
                volume_bindings=bindings,
            )
            self._deployments[deployment_ref] = _RuntimeRecord(
                owner=principal_id,
                context=context,
                manifest=manifest,
                deployment=deployment,
            )
            self._deploy_idempotency[scope] = (request_hash, deployment_ref)
            return RuntimeMutation(deployment, replayed=False)

    def _get(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        deployment_ref: str,
    ) -> RuntimeDeployment:
        principal_id = self._authenticate(principal)
        with self._lock:
            record = self._deployments.get(deployment_ref)
            if (
                record is None
                or record.owner != principal_id
                or record.context != context
            ):
                raise LumaAdapterError(AdapterErrorCode.NOT_FOUND, http_status=404)
            return record.deployment

    def _cancel(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        deployment_ref: str,
    ) -> RuntimeMutation:
        current = self._get(principal, context, deployment_ref)
        with self._lock:
            record = self._deployments[deployment_ref]
            if current.status == "canceled":
                return RuntimeMutation(current, replayed=True)
            record.deployment = RuntimeDeployment(
                deployment_ref=deployment_ref,
                status="canceled",
                manifest_digest=current.manifest_digest,
                service_statuses=current.service_statuses,
                route_statuses=current.route_statuses,
                volume_bindings=current.volume_bindings,
            )
            return RuntimeMutation(record.deployment, replayed=False)

    def _lifecycle(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        deployment_ref: str,
        action: str,
        *,
        idempotency_key: str,
        volume_policy: str = "retain",
    ) -> RuntimeMutation:
        if action not in {"suspend", "resume", "restart", "delete"}:
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        if action == "delete" and volume_policy not in {"retain", "delete"}:
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        principal_id = self._authenticate(principal)
        scope = (
            action,
            *self._scope(principal_id, context, idempotency_key),
        )
        request_hash = self._hash(
            {
                "deploymentRef": deployment_ref,
                "action": action,
                "volumePolicy": volume_policy if action == "delete" else None,
            }
        )
        with self._lock:
            existing = self._lifecycle_idempotency.get(scope)
            if existing is not None:
                if existing[0] != request_hash:
                    raise LumaAdapterError(
                        AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                    )
                return RuntimeMutation(existing[1], replayed=True)
            record = self._deployments.get(deployment_ref)
            if (
                record is None
                or record.owner != principal_id
                or record.context != context
            ):
                raise LumaAdapterError(AdapterErrorCode.NOT_FOUND, http_status=404)
            current = record.deployment
            allowed = {
                "suspend": {"running", "degraded", "deploying", "suspended"},
                "resume": {"suspended", "running", "deploying"},
                "restart": {"running", "degraded"},
                "delete": {
                    "running",
                    "degraded",
                    "deploying",
                    "failed",
                    "suspended",
                    "deleted",
                },
            }[action]
            if current.status not in allowed:
                raise LumaAdapterError(
                    AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                )
            status = {
                "suspend": "suspended",
                "resume": "deploying",
                "restart": "deploying",
                "delete": "deleted",
            }[action]
            service_status = (
                "suspended"
                if action == "suspend"
                else "deleted"
                if action == "delete"
                else "pending"
            )
            route_status = service_status
            deployment = RuntimeDeployment(
                deployment_ref=deployment_ref,
                status=status,
                manifest_digest=current.manifest_digest,
                service_statuses={
                    key: service_status for key in current.service_statuses
                },
                route_statuses={
                    key: route_status for key in current.route_statuses
                },
                volume_bindings=current.volume_bindings,
            )
            record.deployment = deployment
            self._lifecycle_idempotency[scope] = (request_hash, deployment)
            return RuntimeMutation(deployment, replayed=False)

    def _rollback(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        target_context: RuntimeCallContext,
        target_deployment_ref: str,
        idempotency_key: str,
    ) -> RuntimeMutation:
        if (
            context.tenant_ref != target_context.tenant_ref
            or context.application_ref != target_context.application_ref
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        principal_id = self._authenticate(principal)
        scope = ("rollback", *self._scope(principal_id, context, idempotency_key))
        request_hash = self._hash(
            {
                "deploymentRef": deployment_ref,
                "targetDeploymentRef": target_deployment_ref,
                "targetContext": target_context.headers(),
            }
        )
        with self._lock:
            existing = self._lifecycle_idempotency.get(scope)
            if existing is not None:
                if existing[0] != request_hash:
                    raise LumaAdapterError(
                        AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                    )
                return RuntimeMutation(existing[1], replayed=True)
            current_record = self._deployments.get(deployment_ref)
            target_record = self._deployments.get(target_deployment_ref)
            if (
                current_record is None
                or target_record is None
                or current_record.owner != principal_id
                or target_record.owner != principal_id
                or current_record.context != context
                or target_record.context != target_context
                or current_record.deployment.status not in {"running", "degraded"}
                or target_record.deployment.status
                not in {"running", "degraded", "suspended", "superseded"}
            ):
                raise LumaAdapterError(
                    AdapterErrorCode.IDEMPOTENCY_CONFLICT, http_status=409
                )
            current = current_record.deployment
            current_record.deployment = RuntimeDeployment(
                deployment_ref=current.deployment_ref,
                status="superseded",
                manifest_digest=current.manifest_digest,
                service_statuses=current.service_statuses,
                route_statuses=current.route_statuses,
                volume_bindings=current.volume_bindings,
            )
            target = target_record.deployment
            rolled_back = RuntimeDeployment(
                deployment_ref=target.deployment_ref,
                status="deploying",
                manifest_digest=target.manifest_digest,
                service_statuses={key: "pending" for key in target.service_statuses},
                route_statuses={key: "pending" for key in target.route_statuses},
                volume_bindings=target.volume_bindings,
            )
            target_record.deployment = rolled_back
            self._lifecycle_idempotency[scope] = (request_hash, rolled_back)
            return RuntimeMutation(rolled_back, replayed=False)

    def _observability_target(
        self,
        principal: RuntimeServicePrincipal,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
    ) -> _RuntimeRecord:
        self._get(principal, context, deployment_ref)
        with self._lock:
            record = self._deployments[deployment_ref]
            if service_key not in {service.key for service in record.manifest.services}:
                raise LumaAdapterError(AdapterErrorCode.NOT_FOUND, http_status=404)
            return record


class FakeLumaRuntimeAdapter:
    def __init__(
        self, backend: FakeLumaRuntime, principal: RuntimeServicePrincipal
    ) -> None:
        self._backend = backend
        self._principal = principal

    def prepare_volumes(
        self,
        context: RuntimeCallContext,
        volumes: tuple[RuntimeVolumeSpec, ...],
        *,
        idempotency_key: str,
    ) -> tuple[RuntimeVolumeBinding, ...]:
        return self._backend._prepare(
            self._principal,
            context,
            volumes,
            idempotency_key=idempotency_key,
        )

    def deploy_revision(
        self,
        context: RuntimeCallContext,
        manifest: RuntimeManifest,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._backend._deploy(
            self._principal,
            context,
            manifest,
            idempotency_key=idempotency_key,
        )

    def get_runtime_deployment(
        self, context: RuntimeCallContext, deployment_ref: str
    ) -> RuntimeDeployment:
        return self._backend._get(self._principal, context, deployment_ref)

    def cancel_runtime_deployment(
        self, context: RuntimeCallContext, deployment_ref: str
    ) -> RuntimeMutation:
        return self._backend._cancel(self._principal, context, deployment_ref)

    def suspend_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._backend._lifecycle(
            self._principal,
            context,
            deployment_ref,
            "suspend",
            idempotency_key=idempotency_key,
        )

    def resume_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._backend._lifecycle(
            self._principal,
            context,
            deployment_ref,
            "resume",
            idempotency_key=idempotency_key,
        )

    def restart_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._backend._lifecycle(
            self._principal,
            context,
            deployment_ref,
            "restart",
            idempotency_key=idempotency_key,
        )

    def rollback_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        target_context: RuntimeCallContext,
        target_deployment_ref: str,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._backend._rollback(
            self._principal,
            context,
            deployment_ref,
            target_context=target_context,
            target_deployment_ref=target_deployment_ref,
            idempotency_key=idempotency_key,
        )

    def delete_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        volume_policy: str,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._backend._lifecycle(
            self._principal,
            context,
            deployment_ref,
            "delete",
            idempotency_key=idempotency_key,
            volume_policy=volume_policy,
        )

    def tail_runtime_logs(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
        *,
        tail: int = 120,
    ) -> RuntimeLogTail:
        if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= 500:
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        record = self._backend._observability_target(
            self._principal, context, deployment_ref, service_key
        )
        return RuntimeLogTail(
            record.manifest.name,
            service_key,
            tail,
            (),
            False,
            0,
        )

    def get_runtime_metrics_history(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
        *,
        window_seconds: int = 3600,
    ) -> RuntimeMetricsHistory:
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= 7 * 24 * 3600
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        record = self._backend._observability_target(
            self._principal, context, deployment_ref, service_key
        )
        return RuntimeMetricsHistory(
            record.manifest.name,
            service_key,
            window_seconds,
            {"cpuPercent": (), "memoryUsageBytes": ()},
            0,
        )


__all__ = ["FakeLumaRuntime", "FakeLumaRuntimeAdapter"]
