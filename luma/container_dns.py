"""Container resolver settings, independent of host and publication DNS."""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Mapping

from .errors import LumaError

_HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SECRET_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def _validate_extra_hostname(hostname: str, *, field_name: str) -> str:
    clean = hostname.strip()
    if not clean or len(clean) > 253 or clean.startswith(".") or clean.endswith("."):
        raise LumaError(f"{field_name} contains an invalid hostname: {hostname!r}")
    labels = clean.split(".")
    if any(not _HOSTNAME_LABEL_RE.fullmatch(label) for label in labels):
        raise LumaError(f"{field_name} contains an invalid hostname: {hostname!r}")
    try:
        ipaddress.ip_address(clean)
    except ValueError:
        return clean
    raise LumaError(f"{field_name} hostname cannot be an IP literal: {hostname!r}")


def _validate_extra_host_target(
    target: str,
    *,
    field_name: str,
    allow_placeholders: bool = False,
) -> str:
    clean = target.strip()
    if clean == "host-gateway":
        return clean
    if allow_placeholders and _SECRET_PLACEHOLDER_RE.fullmatch(clean):
        return clean
    unbracketed = clean[1:-1] if clean.startswith("[") and clean.endswith("]") else clean
    try:
        return str(ipaddress.ip_address(unbracketed))
    except ValueError as exc:
        raise LumaError(
            f"{field_name} contains an invalid IP address or host-gateway target: {target!r}"
        ) from exc


def parse_extra_hosts(
    value: Any,
    *,
    field_name: str,
    allow_placeholders: bool = False,
) -> list[str]:
    """Validate Docker Compose ``extra_hosts`` (list or mapping) into ``host:ip`` strings."""
    if value is None:
        return []
    raw_pairs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for raw_host, raw_target in value.items():
            if not isinstance(raw_host, str):
                raise LumaError(f"{field_name} mapping keys must be hostnames")
            targets = raw_target if isinstance(raw_target, list) else [raw_target]
            if not targets or any(not isinstance(t, str) for t in targets):
                raise LumaError(f"{field_name} mapping values must be an IP string or list of IP strings")
            for item in targets:
                raw_pairs.append((raw_host, item))
    elif isinstance(value, list):
        for entry in value:
            if not isinstance(entry, str):
                raise LumaError(f"{field_name} entries must be 'hostname:ip' strings")
            text = entry.strip()
            if "=" in text and (":" not in text or text.index("=") < text.index(":")):
                raw_host, raw_target = text.split("=", 1)
            elif ":" in text:
                raw_host, raw_target = text.split(":", 1)
            else:
                raise LumaError(f"{field_name} entry must be 'hostname:ip' or 'hostname=ip': {entry!r}")
            raw_pairs.append((raw_host, raw_target))
    else:
        raise LumaError(f"{field_name} must be a list or mapping of host-to-IP entries")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_host, raw_target in raw_pairs:
        host = _validate_extra_hostname(raw_host, field_name=field_name)
        target = _validate_extra_host_target(
            raw_target,
            field_name=field_name,
            allow_placeholders=allow_placeholders,
        )
        formatted = f"{host}:{target}"
        if formatted not in seen:
            seen.add(formatted)
            normalized.append(formatted)
    return normalized


def shared_extra_hosts(services: Mapping[str, list[str]]) -> list[str]:
    settings = {tuple(entries) for entries in services.values()}
    if len(settings) > 1:
        raise LumaError(
            "Compose services sharing a Mac allocation network must declare the same extra_hosts "
            "on every service; use separate deployments for different host mappings"
        )
    return list(next(iter(settings), ()))


def parse_dns_servers(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise LumaError(f"{field_name} must be an IP address or a list of IP addresses")
    servers = []
    for item in values:
        try:
            servers.append(str(ipaddress.ip_address(item)))
        except ValueError as exc:
            raise LumaError(f"{field_name} contains an invalid DNS server IP address: {item!r}") from exc
    return servers


def shared_dns_servers(services: Mapping[str, list[str]]) -> list[str]:
    settings = {tuple(servers) for servers in services.values()}
    if len(settings) > 1:
        raise LumaError(
            "Compose services sharing an allocation network must declare the same dns "
            "on every service; use separate deployments for different resolvers"
        )
    return list(next(iter(settings), ()))
