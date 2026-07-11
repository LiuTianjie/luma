from __future__ import annotations

import json
import logging
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from ._codec import safe_request_id, validate_idempotency_key
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


LOGGER = logging.getLogger("lae_luma_adapter.runtime_http")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
_SERVICE_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


class HttpLumaRuntimeAdapter:
    """HTTPS client for Luma's dedicated scoped LAE runtime API.

    This adapter never calls the management API, never accepts redirects and
    carries tenant/application/operation/revision/deployment binding on every
    request. The credential type is a distinct runtime principal whose
    audience cannot be confused with ``LUMA_DEPLOY_TOKEN``.
    """

    def __init__(
        self,
        endpoint: str,
        principal: RuntimeServicePrincipal,
        *,
        timeout_seconds: float = 20.0,
        ssl_context: ssl.SSLContext | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= float(timeout_seconds) <= 120
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        self._endpoint = endpoint.rstrip("/")
        self._principal = principal
        self._timeout_seconds = float(timeout_seconds)
        self._opener = opener or urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl_context)
        )

    def prepare_volumes(
        self,
        context: RuntimeCallContext,
        volumes: tuple[RuntimeVolumeSpec, ...],
        *,
        idempotency_key: str,
    ) -> tuple[RuntimeVolumeBinding, ...]:
        body = self._request(
            "POST",
            "/v1/lae/runtime/volumes:prepare",
            context=context,
            body={
                "schemaVersion": "luma.lae-runtime/v1",
                "volumes": [volume.to_wire() for volume in volumes],
            },
            idempotency_key=idempotency_key,
        )
        if body.get("schemaVersion") != "luma.lae-runtime/v1":
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        raw = body.get("volumes")
        if not isinstance(raw, list) or len(raw) != len(volumes):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        bindings: list[RuntimeVolumeBinding] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"key", "volumeRef"}:
                raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
            try:
                bindings.append(RuntimeVolumeBinding(item["key"], item["volumeRef"]))
            except (TypeError, LumaAdapterError):
                raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR) from None
        if {binding.key for binding in bindings} != {volume.key for volume in volumes}:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        return tuple(bindings)

    def deploy_revision(
        self,
        context: RuntimeCallContext,
        manifest: RuntimeManifest,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        body = self._request(
            "POST",
            "/v1/lae/runtime/deployments",
            context=context,
            body={
                "schemaVersion": "luma.lae-runtime/v1",
                "manifest": manifest.to_wire(),
            },
            idempotency_key=idempotency_key,
        )
        mutation = self._parse_mutation(body)
        if mutation.deployment.manifest_digest != manifest.manifest_digest:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        return mutation

    def get_runtime_deployment(
        self, context: RuntimeCallContext, deployment_ref: str
    ) -> RuntimeDeployment:
        path = self._deployment_path(deployment_ref)
        return self._parse_envelope(
            self._request("GET", path, context=context)
        )

    def cancel_runtime_deployment(
        self, context: RuntimeCallContext, deployment_ref: str
    ) -> RuntimeMutation:
        path = self._deployment_path(deployment_ref) + "/cancel"
        return self._parse_mutation(
            self._request("POST", path, context=context, body={})
        )

    def suspend_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._lifecycle(
            "suspend",
            context,
            deployment_ref,
            idempotency_key=idempotency_key,
        )

    def resume_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._lifecycle(
            "resume",
            context,
            deployment_ref,
            idempotency_key=idempotency_key,
        )

    def restart_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
    ) -> RuntimeMutation:
        return self._lifecycle(
            "restart",
            context,
            deployment_ref,
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
        if (
            context.tenant_ref != target_context.tenant_ref
            or context.application_ref != target_context.application_ref
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        body = {
            "schemaVersion": "luma.lae-runtime/v1",
            "target": {
                "runtimeDeploymentRef": target_deployment_ref,
                "operationRef": target_context.operation_ref,
                "revisionRef": target_context.revision_ref,
                "deploymentRef": target_context.deployment_ref,
            },
        }
        mutation = self._parse_mutation(
            self._request(
                "POST",
                self._deployment_path(deployment_ref) + "/rollback",
                context=context,
                body=body,
                idempotency_key=idempotency_key,
            )
        )
        if mutation.deployment.deployment_ref != target_deployment_ref:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        return mutation

    def delete_runtime_deployment(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        volume_policy: str,
        idempotency_key: str,
    ) -> RuntimeMutation:
        if volume_policy not in {"retain", "delete"}:
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        return self._lifecycle(
            "delete",
            context,
            deployment_ref,
            idempotency_key=idempotency_key,
            volume_policy=volume_policy,
        )

    def _lifecycle(
        self,
        action: str,
        context: RuntimeCallContext,
        deployment_ref: str,
        *,
        idempotency_key: str,
        volume_policy: str | None = None,
    ) -> RuntimeMutation:
        body: dict[str, Any] = {"schemaVersion": "luma.lae-runtime/v1"}
        if action == "delete":
            body["volumePolicy"] = volume_policy
        return self._parse_mutation(
            self._request(
                "POST",
                self._deployment_path(deployment_ref) + "/" + action,
                context=context,
                body=body,
                idempotency_key=idempotency_key,
            )
        )

    def tail_runtime_logs(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
        *,
        tail: int = 120,
    ) -> RuntimeLogTail:
        if (
            not isinstance(service_key, str)
            or _SERVICE_KEY.fullmatch(service_key) is None
            or isinstance(tail, bool)
            or not isinstance(tail, int)
            or not 1 <= tail <= 500
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        path = (
            self._deployment_path(deployment_ref)
            + "/services/"
            + urllib.parse.quote(service_key, safe="")
            + "/logs?tail="
            + str(tail)
        )
        body = self._request("GET", path, context=context)
        if set(body) != {
            "schemaVersion",
            "lumaName",
            "serviceKey",
            "tail",
            "logs",
            "truncated",
            "updatedAt",
        } or body.get("schemaVersion") != "luma.lae-runtime/v1":
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        if body.get("serviceKey") != service_key or body.get("tail") != tail:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        logs = body.get("logs")
        if not isinstance(logs, list) or any(
            not isinstance(line, str) for line in logs
        ):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        try:
            return RuntimeLogTail(
                luma_name=body["lumaName"],
                service_key=body["serviceKey"],
                tail=body["tail"],
                logs=tuple(logs),
                truncated=body["truncated"],
                updated_at=body["updatedAt"],
            )
        except (KeyError, TypeError, LumaAdapterError):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR) from None

    def get_runtime_metrics_history(
        self,
        context: RuntimeCallContext,
        deployment_ref: str,
        service_key: str,
        *,
        window_seconds: int = 3600,
    ) -> RuntimeMetricsHistory:
        if (
            not isinstance(service_key, str)
            or _SERVICE_KEY.fullmatch(service_key) is None
            or isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= 7 * 24 * 3600
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        path = (
            self._deployment_path(deployment_ref)
            + "/services/"
            + urllib.parse.quote(service_key, safe="")
            + "/metrics?window="
            + str(window_seconds)
        )
        body = self._request("GET", path, context=context)
        if set(body) != {
            "schemaVersion",
            "lumaName",
            "serviceKey",
            "windowSeconds",
            "series",
            "updatedAt",
        } or body.get("schemaVersion") != "luma.lae-runtime/v1":
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        if (
            body.get("serviceKey") != service_key
            or body.get("windowSeconds") != window_seconds
        ):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        raw_series = body.get("series")
        if not isinstance(raw_series, dict) or set(raw_series) != {
            "cpuPercent",
            "memoryUsageBytes",
        }:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        series: dict[str, tuple[tuple[int, float | int], ...]] = {}
        for key, raw_points in raw_series.items():
            if not isinstance(raw_points, list) or len(raw_points) > 10_000:
                raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
            points: list[tuple[int, float | int]] = []
            for point in raw_points:
                if (
                    not isinstance(point, list)
                    or len(point) != 2
                    or isinstance(point[0], bool)
                    or not isinstance(point[0], int)
                    or isinstance(point[1], bool)
                    or not isinstance(point[1], (int, float))
                    or not math.isfinite(float(point[1]))
                ):
                    raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
                points.append((point[0], point[1]))
            series[str(key)] = tuple(points)
        try:
            return RuntimeMetricsHistory(
                luma_name=body["lumaName"],
                service_key=body["serviceKey"],
                window_seconds=body["windowSeconds"],
                series=series,
                updated_at=body["updatedAt"],
            )
        except (KeyError, TypeError, LumaAdapterError):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR) from None

    @staticmethod
    def _deployment_path(deployment_ref: str) -> str:
        if (
            not isinstance(deployment_ref, str)
            or not deployment_ref
            or len(deployment_ref) > 256
            or any(character in deployment_ref for character in "\x00\r\n")
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        return "/v1/lae/runtime/deployments/" + urllib.parse.quote(
            deployment_ref, safe=""
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        context: RuntimeCallContext,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        encoded = (
            json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
            if body is not None
            else None
        )
        headers = {
            "Authorization": f"Bearer {self._principal.token}",
            "X-Luma-Principal-Audience": self._principal.audience,
            "Accept": "application/json",
            "Content-Type": "application/json",
            **context.headers(),
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = validate_idempotency_key(idempotency_key)
        request = urllib.request.Request(
            self._endpoint + path,
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Redirects arrive here because the opener deliberately refuses to
            # follow them. Never forward the runtime bearer to another origin.
            raw_error = exc.read(_MAX_ERROR_BYTES + 1)
            request_id, upstream_code = self._error_metadata(raw_error)
            code, retryable = self._map_error(
                exc.code, upstream_code=upstream_code
            )
            LOGGER.info(
                "luma runtime request failed method=%s path=%s status=%s",
                method,
                self._log_path(path),
                exc.code,
            )
            raise LumaAdapterError(
                code,
                http_status=exc.code,
                retryable=retryable,
                request_id=request_id,
            ) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
            raise LumaAdapterError(
                AdapterErrorCode.UPSTREAM_UNAVAILABLE, retryable=True
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        try:
            payload = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR) from None
        if not isinstance(payload, dict):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        return payload

    @staticmethod
    def _error_metadata(raw: bytes) -> tuple[str | None, str | None]:
        """Read only the closed, safe fields from a bounded Luma error body."""

        if len(raw) > _MAX_ERROR_BYTES:
            return None, None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        error_info = payload.get("errorInfo")
        if not isinstance(error_info, dict):
            return None, None
        request_id = safe_request_id(
            error_info.get("requestId") or payload.get("requestId")
        )
        upstream_code = error_info.get("code")
        return request_id, upstream_code if isinstance(upstream_code, str) else None

    @staticmethod
    def _map_error(
        status: int, *, upstream_code: str | None
    ) -> tuple[AdapterErrorCode, bool]:
        # Luma deliberately uses HTTP 409 for both an idempotency conflict and
        # a managed-volume placement conflict. The status alone is therefore
        # insufficient. Only these closed upstream codes influence mapping;
        # messages and arbitrary fields are never retained or exposed.
        if status == 409 and upstream_code == "volume_placement_incompatible":
            return AdapterErrorCode.CAPACITY_UNAVAILABLE, False
        if status in {401, 403}:
            return AdapterErrorCode.UNAUTHORIZED, False
        if status == 404:
            return AdapterErrorCode.NOT_FOUND, False
        if status == 409 and upstream_code == "conflict":
            return AdapterErrorCode.IDEMPOTENCY_CONFLICT, False
        if status == 409:
            return AdapterErrorCode.PROTOCOL_ERROR, False
        if status in {400, 422}:
            return AdapterErrorCode.INVALID_REQUEST, False
        if status in {429, 503}:
            return AdapterErrorCode.CAPACITY_UNAVAILABLE, True
        return AdapterErrorCode.UPSTREAM_UNAVAILABLE, status >= 500

    @staticmethod
    def _log_path(path: str) -> str:
        if path == "/v1/lae/runtime/volumes:prepare":
            return path
        if path == "/v1/lae/runtime/deployments":
            return path
        for action in ("cancel", "suspend", "resume", "restart", "delete"):
            if path.endswith("/" + action):
                return (
                    "/v1/lae/runtime/deployments/{deployment_ref}/" + action
                )
        if "/services/" in path and "/logs?" in path:
            return "/v1/lae/runtime/deployments/{deployment_ref}/services/{service_key}/logs"
        if "/services/" in path and "/metrics?" in path:
            return "/v1/lae/runtime/deployments/{deployment_ref}/services/{service_key}/metrics"
        return "/v1/lae/runtime/deployments/{deployment_ref}"

    def _parse_mutation(self, body: Mapping[str, Any]) -> RuntimeMutation:
        replayed = body.get("replayed")
        if not isinstance(replayed, bool):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        return RuntimeMutation(self._parse_envelope(body), replayed)

    @staticmethod
    def _parse_envelope(body: Mapping[str, Any]) -> RuntimeDeployment:
        if body.get("schemaVersion") != "luma.lae-runtime/v1":
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        raw = body.get("deployment")
        if not isinstance(raw, dict):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        allowed = {
            "deploymentRef",
            "status",
            "manifestDigest",
            "serviceStatuses",
            "routeStatuses",
            "volumeBindings",
        }
        if set(raw) != allowed:
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        volume_bindings = raw.get("volumeBindings")
        if not isinstance(volume_bindings, list):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
        try:
            bindings = tuple(
                RuntimeVolumeBinding(item["key"], item["volumeRef"])
                for item in volume_bindings
                if isinstance(item, dict) and set(item) == {"key", "volumeRef"}
            )
            if len(bindings) != len(volume_bindings):
                raise ValueError("invalid volume binding")
            return RuntimeDeployment(
                deployment_ref=raw["deploymentRef"],
                status=raw["status"],
                manifest_digest=raw["manifestDigest"],
                service_statuses=raw["serviceStatuses"],
                route_statuses=raw["routeStatuses"],
                volume_bindings=bindings,
            )
        except (KeyError, TypeError, ValueError, LumaAdapterError):
            raise LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR) from None


__all__ = ["HttpLumaRuntimeAdapter"]
