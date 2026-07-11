from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .client import ApiClient
from .errors import CliError


_TERMINAL = {"succeeded", "failed", "canceled"}
_EVENT_MESSAGES = {
    "operation.created": "Operation created",
    "builder.fetch.started": "Source fetch started",
    "builder.analyze.progress": "Source analysis updated",
    "builder.build.progress": "Image build updated",
    "deployment.preview.succeeded": "Deployment preview succeeded",
    "deployment.succeeded": "Deployment verification succeeded",
    "operation.failed": "Operation failed",
    "operation.cancelled": "Operation canceled",
}


@dataclass(frozen=True, slots=True)
class WatchResult:
    operation_id: str
    status: str
    cursor: int
    operation: dict[str, Any]


def watch_operation(
    client: ApiClient,
    operation_id: str,
    *,
    after: int = 0,
    timeout_seconds: float = 0,
    poll_seconds: float = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> WatchResult:
    if after < 0 or timeout_seconds < 0 or not 0 <= poll_seconds <= 60:
        raise CliError("LAE_CLI_ARGUMENT_INVALID", "Watch arguments are invalid.", 2)
    started = monotonic()
    cursor = after
    while True:
        page = client.get(
            f"/operations/{operation_id}/events",
            query={"after": cursor, "limit": 100},
        )
        raw_events = page.get("events")
        if not isinstance(raw_events, list):
            raise CliError(
                "LAE_API_PROTOCOL_ERROR", "LAE returned invalid operation events.", 9
            )
        for raw_event in raw_events:
            event = _safe_event(raw_event, operation_id=operation_id, after=cursor)
            cursor = event["cursor"]
            if on_event is not None:
                on_event(event)
        status = page.get("status")
        if not isinstance(status, str):
            raise CliError(
                "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid operation status.", 9
            )
        # A terminal operation may span more than one retained event page.
        # Stop only when the API proves this cursor has replayed through the
        # terminal snapshot, not merely because the operation status changed.
        if page.get("terminal") is True:
            operation = client.get(f"/operations/{operation_id}")
            terminal_status = operation.get("status")
            if terminal_status not in _TERMINAL:
                raise CliError(
                    "LAE_API_PROTOCOL_ERROR",
                    "LAE returned an inconsistent terminal operation.",
                    9,
                )
            return WatchResult(operation_id, terminal_status, cursor, operation)
        if timeout_seconds and monotonic() - started >= timeout_seconds:
            raise CliError(
                "LAE_OPERATION_WATCH_TIMEOUT",
                "The operation is still running; resume with its ID and cursor.",
                9,
                retryable=True,
                details={"operationId": operation_id, "cursor": cursor},
            )
        sleeper(poll_seconds)


def _safe_event(value: Any, *, operation_id: str, after: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid operation event.", 9
        )
    event_operation = value.get("operationId")
    cursor = value.get("cursor")
    if (
        event_operation != operation_id
        or isinstance(cursor, bool)
        or not isinstance(cursor, int)
        or cursor <= after
    ):
        raise CliError(
            "LAE_API_PROTOCOL_ERROR", "LAE returned an invalid operation cursor.", 9
        )
    safe: dict[str, Any] = {"operationId": operation_id, "cursor": cursor}
    for key in ("type", "phase", "status"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or len(item) > 512:
                raise CliError(
                    "LAE_API_PROTOCOL_ERROR",
                    "LAE returned an invalid operation event.",
                    9,
                )
            safe[key] = item
    event_type = safe.get("type")
    safe["message"] = _EVENT_MESSAGES.get(
        event_type if isinstance(event_type, str) else "",
        "Operation progress updated",
    )
    return safe
