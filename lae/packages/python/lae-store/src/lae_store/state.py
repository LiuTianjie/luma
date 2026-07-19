from __future__ import annotations

from enum import StrEnum

from .errors import InvalidOperationTransition


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD = "dead"


TERMINAL_OPERATION_STATUSES = frozenset(
    {OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.CANCELED}
)
ACTIVE_OPERATION_STATUSES = frozenset({OperationStatus.QUEUED, OperationStatus.RUNNING})

_TRANSITIONS = {
    OperationStatus.QUEUED: frozenset(
        {OperationStatus.RUNNING, OperationStatus.CANCELED}
    ),
    OperationStatus.RUNNING: TERMINAL_OPERATION_STATUSES,
    OperationStatus.SUCCEEDED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.CANCELED: frozenset(),
}


def require_transition(
    current: str | OperationStatus, target: str | OperationStatus
) -> None:
    try:
        current_status = OperationStatus(current)
        target_status = OperationStatus(target)
    except ValueError as exc:
        raise InvalidOperationTransition(f"unknown operation status: {exc}") from exc
    if target_status not in _TRANSITIONS[current_status]:
        raise InvalidOperationTransition(
            f"operation cannot transition from {current_status.value} to {target_status.value}"
        )


def cancellation_result(status: str | OperationStatus) -> tuple[OperationStatus, bool]:
    """Pure cancellation transition used by the DB service and unit fakes.

    The boolean tells the caller whether a running worker must be signalled.
    """

    current = OperationStatus(status)
    if current is OperationStatus.QUEUED:
        return OperationStatus.CANCELED, False
    if current is OperationStatus.RUNNING:
        return OperationStatus.RUNNING, True
    return current, False
