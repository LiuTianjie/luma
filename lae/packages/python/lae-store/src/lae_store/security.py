from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "privatekey",
)
_SENSITIVE_VALUE = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\blae_dt_[0-9A-HJKMNP-TV-Z]{10}_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    re.compile(r"\blae_ss_v[1-9][0-9]{0,8}_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    re.compile(r"\blae_cs_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    re.compile(
        r"\blae_em_emc_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"(?i)[?&](?:X-Amz-Signature|Signature|sig)=[^&\s]+"),
)


def _key_is_identifier_or_metadata(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    return compact.endswith(
        ("id", "ids", "prefix", "name", "version")
    ) or normalized in {
        "configured",
        "credential_lease_id",
    }


def _key_may_contain_secret(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in compact for part in _SENSITIVE_KEY_PARTS)


def ensure_persistable_payload(value: Any, *, path: str = "$") -> None:
    """Reject likely plaintext credentials before durable persistence.

    This is a final safety net, not a substitute for API schemas. Identifiers,
    prefixes and key names remain allowed; secret-bearing values do not.
    """

    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError(f"persistent payload object is too large at {path}")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError(f"persistent payload key is invalid at {path}")
            if _key_may_contain_secret(key) and not _key_is_identifier_or_metadata(key):
                raise ValueError(f"secret-bearing field is forbidden at {path}.{key}")
            ensure_persistable_payload(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 1024:
            raise ValueError(f"persistent payload array is too large at {path}")
        for index, item in enumerate(value):
            ensure_persistable_payload(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 64 * 1024:
            raise ValueError(f"persistent payload string is too large at {path}")
        if any(pattern.search(value) for pattern in _SENSITIVE_VALUE):
            raise ValueError(f"credential-like value is forbidden at {path}")
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError(f"unsupported persistent payload value at {path}")


def ensure_safe_message(message: str) -> str:
    if not isinstance(message, str) or not message or len(message) > 512:
        raise ValueError("event/error message must contain 1-512 characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in message):
        raise ValueError("event/error message must be a single printable line")
    ensure_persistable_payload(message, path="$.message")
    return message
