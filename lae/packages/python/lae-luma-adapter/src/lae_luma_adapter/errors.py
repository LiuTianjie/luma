from __future__ import annotations

from enum import StrEnum


class AdapterErrorCode(StrEnum):
    """Stable LAE-side error codes for the Luma Builder boundary."""

    INVALID_REQUEST = "LAE_LUMA_VALIDATION_FAILED"
    UNAUTHORIZED = "LAE_LUMA_UNAUTHORIZED"
    NOT_FOUND = "LAE_NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "LAE_IDEMPOTENCY_KEY_REUSED"
    CURSOR_EXPIRED = "LAE_LUMA_CURSOR_EXPIRED"
    CAPACITY_UNAVAILABLE = "LAE_CAPACITY_UNAVAILABLE"
    UPSTREAM_UNAVAILABLE = "LAE_LUMA_UNAVAILABLE"
    PROTOCOL_ERROR = "LAE_LUMA_PROTOCOL_ERROR"


_SAFE_MESSAGES = {
    AdapterErrorCode.INVALID_REQUEST: "Luma rejected the request.",
    AdapterErrorCode.UNAUTHORIZED: "The LAE service principal is not authorized by Luma.",
    AdapterErrorCode.NOT_FOUND: "The requested Luma resource was not found.",
    AdapterErrorCode.IDEMPOTENCY_CONFLICT: "The idempotency key is already bound to another request.",
    AdapterErrorCode.CURSOR_EXPIRED: "The builder task event cursor has expired.",
    AdapterErrorCode.CAPACITY_UNAVAILABLE: "No compatible Luma capacity is currently available.",
    AdapterErrorCode.UPSTREAM_UNAVAILABLE: "Luma is currently unavailable.",
    AdapterErrorCode.PROTOCOL_ERROR: "Luma returned an invalid response.",
}


class LumaAdapterError(RuntimeError):
    """An intentionally redacted, stable adapter failure.

    Raw upstream response bodies are never retained on this exception. This
    prevents a Luma validation response from accidentally becoming a tenant log
    or operation error containing a credential or internal address.
    """

    def __init__(
        self,
        code: AdapterErrorCode,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.request_id = request_id
        super().__init__(_SAFE_MESSAGES[code])

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": str(self),
            "retryable": self.retryable,
            **({"requestId": self.request_id} if self.request_id else {}),
        }


def protocol_error() -> LumaAdapterError:
    return LumaAdapterError(AdapterErrorCode.PROTOCOL_ERROR)
