"""Validate Docker proxy facts and match existing NO_PROXY exclusions."""


def docker_proxy_settings(info: dict[str, object]) -> tuple[str, str, str]:
    """Read the exact Engine API JSON shape; missing facts are not direct access."""
    keys = ("HttpProxy", "HttpsProxy", "NoProxy")
    if not isinstance(info, dict) or any(not isinstance(info.get(key), str) for key in keys):
        raise RuntimeError("Docker returned incomplete or invalid proxy information")
    return info["HttpProxy"], info["HttpsProxy"], info["NoProxy"]


def proxy_bypasses_registry_hosts(info: dict[str, object], hosts: list[str]) -> bool:
    """Require bypass for each exact HTTP registry when Docker uses a proxy."""
    http_proxy, https_proxy, no_proxy = docker_proxy_settings(info)
    return not (http_proxy or https_proxy) or no_proxy_bypasses_registry_hosts(no_proxy, hosts)


def no_proxy_bypasses_registry_hosts(no_proxy: str, hosts: list[str]) -> bool:
    """Match NO_PROXY host, port, IP and CIDR rules without changing policy.

    These stdlib-only helpers are embedded in both runtime setup processes.
    """
    import ipaddress
    import re
    import urllib.parse

    entries = [entry.strip().lower() for entry in no_proxy.split(",") if entry.strip()]
    for host in hosts:
        parsed = urllib.parse.urlsplit("http://" + host)
        hostname = parsed.hostname or ""
        port = parsed.port or 80
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        matched = False
        for entry in entries:
            if entry == "*":
                matched = True
                break
            if "/" in entry:
                try:
                    if address is not None and address in ipaddress.ip_network(entry, strict=False):
                        matched = True
                        break
                except ValueError:
                    pass
                continue
            candidate, candidate_port = entry, None
            if entry.startswith("["):
                if not re.fullmatch(r"\[[0-9a-f:.]+\](?::[0-9]+)?", entry):
                    continue
                try:
                    parsed_entry = urllib.parse.urlsplit("http://" + entry)
                    candidate, candidate_port = parsed_entry.hostname or "", parsed_entry.port
                except ValueError:
                    continue
            elif entry.count(":") == 1:
                candidate, port_text = entry.rsplit(":", 1)
                if not port_text.isdigit():
                    continue
                candidate_port = int(port_text)
            if candidate_port is not None and candidate_port != port:
                continue
            if address is not None:
                try:
                    matched = address == ipaddress.ip_address(candidate)
                except ValueError:
                    matched = False
            else:
                if candidate.startswith("*."):
                    candidate = candidate[1:]
                subdomains_only = candidate.startswith(".")
                domain = candidate.lstrip(".")
                matched = bool(domain) and (
                    hostname.endswith("." + domain) or (not subdomains_only and hostname == domain)
                )
            if matched:
                break
        if not matched:
            return False
    return True
