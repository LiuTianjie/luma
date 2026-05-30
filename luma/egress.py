from __future__ import annotations

import base64
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

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
        "proxy-groups": [{"name": "EGRESS", "type": "select", "proxies": [proxy["name"] for proxy in proxies]}],
        "rules": ["MATCH,EGRESS"],
    }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def minimal_mihomo_config_from_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Clash.Meta"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return minimal_mihomo_config_from_bytes(response.read())
    except Exception as exc:
        raise LumaError(f"failed to download egress subscription: {exc}") from exc


def minimal_mihomo_config_from_file(path: Path) -> str:
    return minimal_mihomo_config_from_bytes(path.read_bytes())
