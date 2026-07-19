from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from ._codec import (
    context_headers,
    parse_event_page,
    parse_task_envelope,
    parse_task_mutation,
    safe_request_id,
    task_request_body,
    validate_event_query,
    validate_idempotency_key,
    validate_limits,
    validate_principal,
    validate_task_id,
)
from .errors import AdapterErrorCode, LumaAdapterError, protocol_error
from .models import (
    AnalyzeSourceRequest,
    BuilderTask,
    BuilderTaskEventPage,
    BuilderTaskMutation,
    BuildPlanRequest,
    LumaCallContext,
    ServicePrincipal,
)

LOGGER = logging.getLogger("lae_luma_adapter.http")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024


class HttpLumaBuilderAdapter:
    """Standard-library HTTP implementation of Luma Builder Task v1."""

    def __init__(
        self,
        endpoint: str,
        principal: ServicePrincipal,
        *,
        timeout_seconds: float = 30,
        ssl_context: ssl.SSLContext | None = None,
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
        validate_principal(principal.principal_id, principal.token)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise LumaAdapterError(AdapterErrorCode.INVALID_REQUEST)
        self._endpoint = endpoint.rstrip("/")
        self._principal = principal
        self._timeout_seconds = float(timeout_seconds)
        self._ssl_context = ssl_context

    def create_analyze_task(
        self,
        context: LumaCallContext,
        request: AnalyzeSourceRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        self._validate_limits(request)
        body = task_request_body(
            context, kind="analyze-source", payload=request.to_wire()
        )
        payload = self._request(
            "POST",
            "/v1/builder/tasks",
            context=context,
            body=body,
            extra_headers={
                "Idempotency-Key": validate_idempotency_key(idempotency_key)
            },
        )
        mutation = parse_task_mutation(payload, context)
        if mutation.task.kind != "analyze-source":
            raise protocol_error()
        return mutation

    def create_build_task(
        self,
        context: LumaCallContext,
        request: BuildPlanRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        self._validate_limits(request)
        body = task_request_body(context, kind="build-plan", payload=request.to_wire())
        payload = self._request(
            "POST",
            "/v1/builder/tasks",
            context=context,
            body=body,
            extra_headers={
                "Idempotency-Key": validate_idempotency_key(idempotency_key)
            },
        )
        mutation = parse_task_mutation(payload, context)
        if mutation.task.kind != "build-plan":
            raise protocol_error()
        return mutation

    def get_builder_task(self, context: LumaCallContext, task_id: str) -> BuilderTask:
        task_id = validate_task_id(task_id)
        path = self._task_path(task_id)
        task = parse_task_envelope(self._request("GET", path, context=context), context)
        if task.task_id != task_id:
            raise protocol_error()
        return task

    def get_builder_task_events(
        self,
        context: LumaCallContext,
        task_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> BuilderTaskEventPage:
        task_id = validate_task_id(task_id)
        validate_event_query(after, limit)
        # The current Luma events envelope does not repeat tenant/application.
        # Verify immutable task ownership before fetching the page so one broad
        # service principal cannot accidentally cross tenant contexts.
        self.get_builder_task(context, task_id)
        query = urllib.parse.urlencode({"after": after, "limit": limit})
        value = self._request(
            "GET", f"{self._task_path(task_id)}/events?{query}", context=context
        )
        return parse_event_page(value, task_id=task_id, after=after)

    def cancel_builder_task(
        self, context: LumaCallContext, task_id: str
    ) -> BuilderTaskMutation:
        task_id = validate_task_id(task_id)
        # Cancel is mutating. Preflight the task's tenant/app binding because
        # Luma's current ownership check is principal-level.
        self.get_builder_task(context, task_id)
        value = self._request(
            "POST", f"{self._task_path(task_id)}/cancel", context=context, body={}
        )
        mutation = parse_task_mutation(value, context)
        if mutation.task.task_id != task_id:
            raise protocol_error()
        return mutation

    def analyze_source(
        self,
        context: LumaCallContext,
        request: AnalyzeSourceRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        return self.create_analyze_task(
            context, request, idempotency_key=idempotency_key
        )

    def build_plan(
        self,
        context: LumaCallContext,
        request: BuildPlanRequest,
        *,
        idempotency_key: str,
    ) -> BuilderTaskMutation:
        return self.create_build_task(context, request, idempotency_key=idempotency_key)

    def watch_builder_task(
        self,
        context: LumaCallContext,
        task_id: str,
        *,
        cursor: int = 0,
        limit: int = 200,
    ) -> BuilderTaskEventPage:
        return self.get_builder_task_events(context, task_id, after=cursor, limit=limit)

    @staticmethod
    def _validate_limits(request: AnalyzeSourceRequest | BuildPlanRequest) -> None:
        limits = request.limits
        validate_limits(
            limits.cpu, limits.memory_mib, limits.disk_mib, limits.timeout_seconds
        )

    @staticmethod
    def _task_path(task_id: str) -> str:
        return f"/v1/builder/tasks/{urllib.parse.quote(task_id, safe='')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        context: LumaCallContext,
        body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._principal.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            **context_headers(context),
            **dict(extra_headers or {}),
        }
        encoded_body = (
            json.dumps(
                body,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            self._endpoint + path,
            data=encoded_body,
            headers=headers,
            method=method,
        )
        try:
            open_kwargs: dict[str, Any] = {"timeout": self._timeout_seconds}
            if self._ssl_context is not None:
                open_kwargs["context"] = self._ssl_context
            with urllib.request.urlopen(request, **open_kwargs) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            raw_error = exc.read(_MAX_ERROR_BYTES + 1)
            request_id, upstream_code = self._error_metadata(raw_error)
            error = self._map_http_error(
                exc.code, upstream_code=upstream_code, request_id=request_id
            )
            LOGGER.info(
                "luma builder request failed method=%s path=%s status=%s",
                method,
                self._log_path(path),
                exc.code,
            )
            raise error from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
            LOGGER.info(
                "luma builder request unavailable method=%s path=%s",
                method,
                self._log_path(path),
            )
            raise LumaAdapterError(
                AdapterErrorCode.UPSTREAM_UNAVAILABLE, retryable=True
            ) from None

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise protocol_error()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise protocol_error() from None
        if not isinstance(payload, dict):
            raise protocol_error()
        LOGGER.debug(
            "luma builder request completed method=%s path=%s status=%s",
            method,
            self._log_path(path),
            status,
        )
        return payload

    @staticmethod
    def _log_path(path: str) -> str:
        if path == "/v1/builder/tasks":
            return path
        if path.endswith("/events") or "/events?" in path:
            return "/v1/builder/tasks/{task_id}/events"
        if path.endswith("/cancel"):
            return "/v1/builder/tasks/{task_id}/cancel"
        if path.startswith("/v1/builder/tasks/"):
            return "/v1/builder/tasks/{task_id}"
        return "{luma_path}"

    @staticmethod
    def _error_metadata(raw: bytes) -> tuple[str | None, str | None]:
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
            error_info = {}
        request_id = safe_request_id(
            error_info.get("requestId") or payload.get("requestId")
        )
        upstream_code = error_info.get("code")
        return request_id, upstream_code if isinstance(upstream_code, str) else None

    @staticmethod
    def _map_http_error(
        status: int,
        *,
        upstream_code: str | None,
        request_id: str | None,
    ) -> LumaAdapterError:
        if status in {401, 403}:
            code = AdapterErrorCode.UNAUTHORIZED
            retryable = False
        elif status == 404:
            code = AdapterErrorCode.NOT_FOUND
            retryable = False
        elif status == 409:
            code = AdapterErrorCode.IDEMPOTENCY_CONFLICT
            retryable = False
        elif status == 410:
            code = AdapterErrorCode.CURSOR_EXPIRED
            retryable = False
        elif status in {429, 503}:
            code = AdapterErrorCode.CAPACITY_UNAVAILABLE
            retryable = True
        elif status in {400, 422}:
            code = AdapterErrorCode.INVALID_REQUEST
            retryable = False
        else:
            code = AdapterErrorCode.UPSTREAM_UNAVAILABLE
            retryable = status >= 500
        # ``upstream_code`` is deliberately not interpolated into an exception
        # message. It is parsed only to make future explicit mappings possible.
        _ = upstream_code
        return LumaAdapterError(
            code, http_status=status, retryable=retryable, request_id=request_id
        )
