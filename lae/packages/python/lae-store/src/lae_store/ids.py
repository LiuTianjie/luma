from __future__ import annotations

import re
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PREFIX = re.compile(r"^[a-z][a-z0-9]{1,11}$")
_OPAQUE_ID = re.compile(r"^[a-z][a-z0-9]{1,11}_[0-9A-HJKMNP-TV-Z]{26}$")


def _encode_crockford(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        value, remainder = divmod(value, 32)
        chars[index] = _CROCKFORD[remainder]
    if value:
        raise ValueError("value does not fit requested Crockford length")
    return "".join(chars)


def new_id(prefix: str, *, timestamp_ms: int | None = None) -> str:
    """Return an opaque, time-sortable prefix + ULID-style identifier.

    The random component is 80 bits and never derives from tenant, email, name
    or any other business value.
    """

    if not _PREFIX.fullmatch(prefix):
        raise ValueError("ID prefix must be 2-12 lowercase alphanumeric characters")
    milliseconds = int(
        time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    )
    if not 0 <= milliseconds < (1 << 48):
        raise ValueError("timestamp_ms must fit 48 bits")
    value = (milliseconds << 80) | secrets.randbits(80)
    return f"{prefix}_{_encode_crockford(value, 26)}"


def require_opaque_id(value: str, *, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ValueError("resource ID must be an opaque prefix + 26-character ULID")
    if prefix is not None and not value.startswith(f"{prefix}_"):
        raise ValueError(f"resource ID must use {prefix!r} prefix")
    return value
