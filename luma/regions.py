from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import LumaError

BUILTIN_REGION_NAMES = ("cn", "global", "home")
VALID_REGIONS = set(BUILTIN_REGION_NAMES)
RESERVED_REGION_NAMES = {"all", "any", "default", "manager", "none", "unknown"}
REGION_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
REGION_NAME_MAX_LEN = 32
EGRESS_PROXY = "proxy"
EGRESS_DIRECT = "direct"
VALID_EGRESS_MODES = {EGRESS_PROXY, EGRESS_DIRECT}
CUSTOM_REGION_EXPOSURES = ("none",)

BUILTIN_REGIONS: dict[str, dict[str, Any]] = {
    "cn": {
        "name": "cn",
        "builtin": True,
        "egress": EGRESS_PROXY,
        "exposures": ["none", "cn-edge", "cloudflare-tunnel", "tcp-relay"],
    },
    "global": {
        "name": "global",
        "builtin": True,
        "egress": EGRESS_DIRECT,
        "exposures": ["none", "external-edge", "cloudflare-tunnel", "tcp-relay"],
    },
    "home": {
        "name": "home",
        "builtin": True,
        "egress": EGRESS_PROXY,
        "exposures": ["none", "tailscale-relay", "cloudflare-tunnel", "tcp-relay"],
    },
}


def parse_region_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LumaError("region is required")
    name = value.strip().lower()
    if name in RESERVED_REGION_NAMES:
        raise LumaError(f"region name {name!r} is reserved")
    if len(name) > REGION_NAME_MAX_LEN or REGION_NAME_RE.fullmatch(name) is None:
        raise LumaError(
            "region name must be lowercase alphanumeric with optional hyphens, "
            f"start with a letter, and be at most {REGION_NAME_MAX_LEN} characters"
        )
    return name


def is_builtin_region(name: str) -> bool:
    return str(name or "").strip().lower() in VALID_REGIONS


def parse_egress_mode(value: Any, *, default: str = EGRESS_PROXY) -> str:
    raw = default if value is None or str(value).strip() == "" else str(value).strip().lower()
    if raw not in VALID_EGRESS_MODES:
        raise LumaError(f"region egress must be one of {sorted(VALID_EGRESS_MODES)}")
    return raw


def public_region_record(record: Mapping[str, Any]) -> dict[str, Any]:
    name = str(record.get("name") or "").strip()
    exposures = [
        str(item).strip()
        for item in (record.get("exposures") or [])
        if str(item).strip()
    ]
    return {
        "name": name,
        "builtin": bool(record.get("builtin")),
        "egress": str(record.get("egress") or EGRESS_DIRECT),
        "exposures": exposures,
        "createdAt": int(record.get("createdAt") or 0),
    }


def custom_region_record(name: str, *, egress: str, created_at: int = 0) -> dict[str, Any]:
    return {
        "name": name,
        "builtin": False,
        "egress": egress,
        "exposures": list(CUSTOM_REGION_EXPOSURES),
        "createdAt": int(created_at or 0),
    }


def _custom_regions_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        return {}
    raw = state.get("regions")
    return raw if isinstance(raw, dict) else {}


def region_catalog(state: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    catalog = {name: dict(spec) for name, spec in BUILTIN_REGIONS.items()}
    for raw_name, raw_value in _custom_regions_state(state).items():
        try:
            name = parse_region_name(raw_name)
        except LumaError:
            continue
        if name in catalog or not isinstance(raw_value, Mapping):
            continue
        egress = parse_egress_mode(raw_value.get("egress"), default=EGRESS_PROXY)
        created_at = int(raw_value.get("createdAt") or 0)
        catalog[name] = custom_region_record(name, egress=egress, created_at=created_at)
        if raw_value.get("exposures"):
            catalog[name]["exposures"] = [
                str(item).strip()
                for item in raw_value.get("exposures") or []
                if str(item).strip()
            ] or list(CUSTOM_REGION_EXPOSURES)
    return catalog


def listed_regions(state: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    catalog = region_catalog(state)
    builtin = [public_region_record(catalog[name]) for name in BUILTIN_REGION_NAMES]
    custom = [
        public_region_record(record)
        for name, record in sorted(catalog.items())
        if name not in VALID_REGIONS
    ]
    return builtin + custom


def require_registered_region(state: Mapping[str, Any] | None, region: Any) -> dict[str, Any]:
    name = parse_region_name(region)
    record = region_catalog(state).get(name)
    if record is None:
        known = ", ".join(item["name"] for item in listed_regions(state))
        raise LumaError(f"unknown region {name!r}; create it first with luma region create, or use one of: {known}")
    return record


def region_uses_egress_proxy(state: Mapping[str, Any] | None, region: str) -> bool:
    name = str(region or "").strip().lower()
    if not name:
        return False
    record = region_catalog(state).get(name)
    if record is not None:
        return str(record.get("egress") or "") == EGRESS_PROXY
    return name in {"cn", "home"}


def allowed_exposures_for_region(region: str, state: Mapping[str, Any] | None = None) -> set[str]:
    name = str(region or "").strip().lower()
    record = region_catalog(state).get(name)
    if record is not None:
        return {str(item) for item in record.get("exposures") or []}
    if is_builtin_region(name):
        return {str(item) for item in BUILTIN_REGIONS[name]["exposures"]}
    return set(CUSTOM_REGION_EXPOSURES)


def validate_region_exposure(region: str, exposure: str, *, state: Mapping[str, Any] | None = None) -> None:
    name = parse_region_name(region)
    if exposure == "cn-edge" and name != "cn":
        raise LumaError("exposure=cn-edge requires region=cn")
    if exposure == "external-edge" and name != "global":
        raise LumaError("exposure=external-edge requires region=global")
    if exposure == "tailscale-relay" and name != "home":
        raise LumaError("exposure=tailscale-relay requires region=home")
    allowed = allowed_exposures_for_region(name, state)
    if exposure not in allowed:
        raise LumaError(
            f"region {name} does not allow exposure={exposure}; allowed: {sorted(allowed)}"
        )


def require_region_exposure(
    state: Mapping[str, Any] | None,
    region: Any,
    exposure: str,
) -> dict[str, Any]:
    record = require_registered_region(state, region)
    validate_region_exposure(record["name"], exposure, state=state)
    return record


def ensure_regions_state(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("regions")
    if not isinstance(raw, dict):
        raw = {}
        state["regions"] = raw
    return raw


def region_names_in_use(nodes: Mapping[str, Any] | None, storage_classes: Mapping[str, Any] | None = None) -> set[str]:
    used: set[str] = set()
    for record in (nodes or {}).values():
        if not isinstance(record, Mapping):
            continue
        labels = record.get("labels") if isinstance(record.get("labels"), Mapping) else {}
        name = str(record.get("region") or labels.get("region") or "").strip().lower()
        if name:
            used.add(name)
    for record in (storage_classes or {}).values():
        if not isinstance(record, Mapping):
            continue
        for item in record.get("regions") or []:
            name = str(item or "").strip().lower()
            if name:
                used.add(name)
    return used

