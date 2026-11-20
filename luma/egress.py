from __future__ import annotations

import base64
import os
import secrets
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

from .errors import LumaError

DEFAULT_PROVIDER_NAME = "egress-sub"
DEFAULT_PROVIDER_PATH = "./providers/egress-sub.yaml"
DEFAULT_PROVIDER_INTERVAL = 3600
DEFAULT_HEALTH_CHECK_URL = "https://www.gstatic.com/generate_204"
DEFAULT_HEALTH_CHECK_INTERVAL = 300
DEFAULT_HEALTH_CHECK_TIMEOUT = 5000
DEFAULT_URL_TEST_TOLERANCE = 150


def _egress_secret() -> str:
    return str(os.environ.get("LUMA_EGRESS_SECRET") or "").strip() or secrets.token_hex(16)


def _validate_subscription_url(url: str) -> str:
    cleaned = str(url or "").strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise LumaError("egress subscription URL must be a valid http or https URL")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LumaError("egress subscription URL must use http or https")
    return cleaned


def _base_mihomo_config(*, secret: str | None = None) -> Dict[str, Any]:
    resolved_secret = str(secret or "").strip() or _egress_secret()
    return {
        "mixed-port": 7890,
        "allow-lan": True,
        # Mihomo runs inside a Nomad bridge-mode Docker container with port 7890
        # mapped from the host (and reached by worker/build nodes over Tailscale,
        # while firewall guards drop public 7890 traffic). Binding 0.0.0.0 inside
        # the container is required so Docker port forwarding reaches port 7890.
        "bind-address": "0.0.0.0",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "secret": resolved_secret,
        "dns": {
            "enable": True,
            "listen": "127.0.0.1:1053",
            "ipv6": False,
            "enhanced-mode": "redir-host",
            "nameserver": ["223.5.5.5", "119.29.29.29", "1.1.1.1"],
        },
    }


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


def _extract_usable_proxies(raw: bytes) -> List[Dict[str, Any]]:
    data = _decode_subscription(raw)
    raw_proxies = data.get("proxies")
    if not isinstance(raw_proxies, list):
        raise LumaError("subscription contains no usable proxies")
    proxies: List[Dict[str, Any]] = [
        proxy
        for proxy in raw_proxies
        if isinstance(proxy, dict) and proxy.get("name") and proxy.get("type")
    ]
    if not proxies:
        raise LumaError("subscription contains no usable proxies")
    return proxies


def minimal_mihomo_config_from_bytes(raw: bytes) -> str:
    proxies = _extract_usable_proxies(raw)
    proxy_names = [str(proxy["name"]) for proxy in proxies]
    fallback_proxies = [
        "EGRESS-AUTO",
        *[name for name in proxy_names if name not in {"EGRESS-AUTO", "EGRESS"}],
    ]
    config = _base_mihomo_config()
    config["proxies"] = proxies
    config["proxy-groups"] = [
        {
            "name": "EGRESS-AUTO",
            "type": "url-test",
            "proxies": proxy_names,
            "url": DEFAULT_HEALTH_CHECK_URL,
            "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
            "tolerance": DEFAULT_URL_TEST_TOLERANCE,
            "lazy": True,
        },
        {
            "name": "EGRESS",
            "type": "fallback",
            "proxies": fallback_proxies,
            "url": DEFAULT_HEALTH_CHECK_URL,
            "interval": DEFAULT_HEALTH_CHECK_INTERVAL,
            "lazy": True,
        },
    ]
    config["rules"] = ["MATCH,EGRESS"]
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def minimal_mihomo_config_from_text(text: str) -> str:
    return minimal_mihomo_config_from_bytes(str(text).encode("utf-8"))


def minimal_mihomo_provider_config(
    url: str,
    *,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    provider_path: str = DEFAULT_PROVIDER_PATH,
    interval: int = DEFAULT_PROVIDER_INTERVAL,
    health_check_url: str = DEFAULT_HEALTH_CHECK_URL,
    health_check_interval: int = DEFAULT_HEALTH_CHECK_INTERVAL,
    health_check_timeout: int = DEFAULT_HEALTH_CHECK_TIMEOUT,
    tolerance: int = DEFAULT_URL_TEST_TOLERANCE,
) -> str:
    validated_url = _validate_subscription_url(url)
    config = _base_mihomo_config()
    config["proxy-providers"] = {
        provider_name: {
            "type": "http",
            "url": validated_url,
            "path": provider_path,
            "interval": int(interval),
            "health-check": {
                "enable": True,
                "url": health_check_url,
                "interval": int(health_check_interval),
                "timeout": int(health_check_timeout),
                "lazy": True,
            },
        }
    }
    config["proxy-groups"] = [
        {
            "name": "EGRESS-AUTO",
            "type": "url-test",
            "use": [provider_name],
            "url": health_check_url,
            "interval": int(health_check_interval),
            "tolerance": int(tolerance),
            "lazy": True,
        },
        {
            "name": "EGRESS",
            "type": "fallback",
            "proxies": ["EGRESS-AUTO"],
            "use": [provider_name],
            "url": health_check_url,
            "interval": int(health_check_interval),
            "lazy": True,
        },
    ]
    config["rules"] = ["MATCH,EGRESS"]
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


def minimal_mihomo_config_from_url(url: str, *, validate: bool = True) -> str:
    validated_url = _validate_subscription_url(url)
    if validate:
        request = urllib.request.Request(validated_url, headers={"User-Agent": "Clash.Meta"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                _extract_usable_proxies(response.read())
        except Exception as exc:
            raise LumaError(f"failed to download egress subscription: {exc}") from exc
    return minimal_mihomo_provider_config(validated_url)


def minimal_mihomo_config_from_file(path: Path) -> str:
    return minimal_mihomo_config_from_bytes(path.read_bytes())
