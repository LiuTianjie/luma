from __future__ import annotations

import base64
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

from .errors import LumaError


def _decode_subscription(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if "proxies:" not in text:
        compact = "".join(text.split())
        try:
            text = base64.b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8", errors="replace")
        except Exception:
            pass
    if "proxies:" not in text:
        raise LumaError("subscription did not return a mihomo/clash YAML config")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise LumaError("subscription YAML must be a mapping")
    return data


def minimal_mihomo_config_from_bytes(raw: bytes) -> str:
    data = _decode_subscription(raw)
    proxies: List[Dict[str, Any]] = [
        proxy
        for proxy in data.get("proxies", [])
        if isinstance(proxy, dict) and proxy.get("name") and proxy.get("type")
    ]
    if not proxies:
        raise LumaError("subscription contains no usable proxies")
    config: Dict[str, Any] = {
        "mixed-port": 7890,
        "allow-lan": True,
        "bind-address": "0.0.0.0",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:1053",
            "ipv6": False,
            "enhanced-mode": "redir-host",
            "nameserver": ["223.5.5.5", "119.29.29.29", "1.1.1.1"],
        },
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "EGRESS",
                "type": "url-test",
                "proxies": [proxy["name"] for proxy in proxies],
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 50,
            }
        ],
        "rules": ["MATCH,EGRESS"],
    }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def ensure_mihomo_direct_domains(config_text: str, domains: Iterable[str]) -> tuple[str, bool]:
    """Put trusted internal domains before the catch-all egress rule."""
    try:
        data = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid installed egress YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError("installed egress YAML must be a mapping")
    normalized = sorted(
        {
            str(domain).strip().lower().rstrip(".")
            for domain in domains
            if str(domain).strip()
        }
    )
    normalized = [domain for domain in normalized if domain and "," not in domain and "/" not in domain]
    if not normalized:
        return config_text, False
    wanted = [f"DOMAIN,{domain},DIRECT" for domain in normalized]
    rules = [str(rule) for rule in data.get("rules") or [] if str(rule).strip()]
    wanted_keys = {rule.lower() for rule in wanted}
    remaining = [rule for rule in rules if rule.lower() not in wanted_keys]
    updated_rules = [*wanted, *remaining]
    if updated_rules == rules:
        return config_text, False
    data["rules"] = updated_rules
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False), True


def minimal_mihomo_config_from_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Clash.Meta"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return minimal_mihomo_config_from_bytes(response.read())
    except Exception as exc:
        raise LumaError(f"failed to download egress subscription: {exc}") from exc


def minimal_mihomo_config_from_file(path: Path) -> str:
    return minimal_mihomo_config_from_bytes(path.read_bytes())
