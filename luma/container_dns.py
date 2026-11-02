"""Container resolver settings, independent of host and publication DNS."""
from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from .errors import LumaError


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
