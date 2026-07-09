from __future__ import annotations

import getpass
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable

from .errors import LumaError
from .io import atomic_write_text


DEFAULT_CONFIG_PATH = Path.home() / ".luma.config.json"

SECRET_KEYS = {
    "CLOUDFLARE_API_TOKEN",
    "EGRESS_SUBSCRIPTION_URL",
    "LUMA_SUDO_PASSWORD",
    "TAILSCALE_AUTHKEY",
}


@dataclass(frozen=True)
class ConfigPrompt:
    key: str
    label: str
    secret: bool
    required: bool
    help: str
    example: str = ""


ROLE_KEYS = {
    "manager": [
        ConfigPrompt(
            "CLOUDFLARE_API_TOKEN",
            "Cloudflare DNS API token",
            True,
            True,
            "Used on the manager to create/update DNS records for the control domain and public services. Create a Cloudflare token with Zone Read and DNS Edit for the domain zone.",
        ),
        ConfigPrompt(
            "LUMA_DNS_EDGE_TARGET",
            "Public DNS target for control and edge routes",
            False,
            False,
            "IP address or DNS name that Cloudflare records should point to when luma.yaml has no providers.dns.edgeTarget or edge node publicIp.",
            "203.0.113.10",
        ),
        ConfigPrompt(
            "TRAEFIK_ACME_EMAIL",
            "ACME certificate email",
            False,
            True,
            "Email address used by Traefik/Let's Encrypt for HTTPS certificate registration and expiration notices.",
            "ops@example.com",
        ),
        ConfigPrompt(
            "TAILSCALE_AUTHKEY",
            "Tailscale auth key",
            True,
            False,
            "Used only when this server must join a tailnet, such as private worker joins, home nodes, or tailscale-relay exposure.",
        ),
        ConfigPrompt(
            "EGRESS_SUBSCRIPTION_URL",
            "Egress proxy subscription URL",
            True,
            False,
            "Used by egress setup to build the Mihomo proxy config for Docker image pulls and services with proxy: true. Leave empty when using --skip-egress.",
        ),
        ConfigPrompt(
            "LUMA_SUDO_PASSWORD",
            "sudo password",
            True,
            False,
            "Optional fallback for sudo commands on servers without passwordless sudo. Stored only in the local user config file.",
        ),
    ],
    "worker": [
        ConfigPrompt(
            "TAILSCALE_AUTHKEY",
            "Tailscale auth key",
            True,
            False,
            "Used when this worker/home node needs to join the tailnet before joining the Luma cluster.",
        ),
        ConfigPrompt(
            "LUMA_SUDO_PASSWORD",
            "sudo password",
            True,
            False,
            "Optional fallback for sudo commands on servers without passwordless sudo. Stored only in the local user config file.",
        ),
    ],
    "client": [],
}


def user_config_path() -> Path:
    return Path(os.environ.get("LUMA_USER_CONFIG") or DEFAULT_CONFIG_PATH)


def load_user_config(path: Path | None = None, *, override: bool = False) -> list[str]:
    path = path or user_config_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LumaError(f"invalid user config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError(f"invalid user config {path}: expected JSON object")
    env = data.get("env", data)
    if not isinstance(env, dict):
        raise LumaError(f"invalid user config {path}: expected env object")
    loaded: list[str] = []
    for key, value in env.items():
        if value is None:
            continue
        name = str(key)
        if not _valid_env_name(name):
            raise LumaError(f"invalid env var name in user config {path}: {name!r}")
        if override or not os.environ.get(name):
            os.environ[name] = str(value)
            loaded.append(name)
    return loaded


def write_user_config(env: Dict[str, str], path: Path | None = None) -> Path:
    path = path or user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_config(path)
    merged_env = dict(existing.get("env") or {})
    for key, value in env.items():
        if value:
            merged_env[key] = value
    data = {
        "version": 1,
        "env": dict(sorted(merged_env.items())),
    }
    atomic_write_text(
        path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600
    )
    return path


def configured_keys(path: Path | None = None) -> list[str]:
    data = _read_config(path or user_config_path())
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    return sorted(str(key) for key, value in env.items() if value)


def interactive_configure(role: str, *, path: Path | None = None, input_fn=None) -> Path:
    if role not in ROLE_KEYS:
        raise LumaError(f"unknown configure role: {role}")
    values: Dict[str, str] = {}
    prompts = ROLE_KEYS[role]
    if not prompts:
        return write_user_config(values, path=path)
    if input_fn is None:
        input_fn = input
    existing = _read_config(path or user_config_path())
    existing_env = existing.get("env") if isinstance(existing.get("env"), dict) else {}
    for item in prompts:
        current = str(existing_env.get(item.key) or os.environ.get(item.key) or "")
        suffix = " [configured]" if current else (" [required]" if item.required else " [optional]")
        _print_prompt_help(item, suffix)
        prompt = f"{item.key}: "
        if item.secret:
            value = getpass.getpass(prompt)
        else:
            value = input_fn(prompt)
        value = value.strip()
        if value:
            values[item.key] = value
        elif current:
            values[item.key] = current
        elif item.required:
            raise LumaError(f"{item.key} is required for {role} configuration")
    return write_user_config(values, path=path)


def ensure_interactive_config(
    role: str,
    *,
    keys: Iterable[str] | None = None,
    required_keys: Iterable[str] | None = None,
    path: Path | None = None,
    input_fn=None,
) -> Path | None:
    if role not in ROLE_KEYS:
        raise LumaError(f"unknown configure role: {role}")
    wanted = set(keys or [item.key for item in ROLE_KEYS[role]])
    required = set(required_keys or [])
    prompts = [item for item in ROLE_KEYS[role] if item.key in wanted and not os.environ.get(item.key)]
    if not prompts:
        return None
    if not sys.stdin.isatty() and input_fn is None:
        missing_required = [item.key for item in prompts if item.required or item.key in required]
        if missing_required:
            missing = ", ".join(missing_required)
            raise LumaError(f"missing local config ({missing}). Run: luma configure --role {role}")
        return None
    if input_fn is None:
        input_fn = input
    values: Dict[str, str] = {}
    print(f"Missing local {role} configuration. Values will be saved to {path or user_config_path()}.")
    for item in prompts:
        is_required = item.required or item.key in required
        suffix = " [required]" if is_required else " [optional, press Enter to skip]"
        _print_prompt_help(item, suffix)
        prompt = f"{item.key}: "
        value = getpass.getpass(prompt) if item.secret else input_fn(prompt)
        value = value.strip()
        if value:
            values[item.key] = value
            os.environ[item.key] = value
        elif is_required:
            raise LumaError(f"{item.key} is required for {role} configuration")
    if not values:
        return None
    return write_user_config(values, path=path)


def masked_config_lines(keys: Iterable[str]) -> list[str]:
    lines = []
    for key in sorted(keys):
        marker = "***" if key in SECRET_KEYS or "TOKEN" in key or "PASSWORD" in key or "URL" in key else "set"
        lines.append(f"{key}={marker}")
    return lines


def _print_prompt_help(item: ConfigPrompt, suffix: str) -> None:
    print(f"\n{item.key}{suffix}")
    print(f"  {item.help}")
    if item.example:
        print(f"  Example: {item.example}")


def _read_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "env": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LumaError(f"invalid user config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError(f"invalid user config {path}: expected JSON object")
    if "env" not in data:
        return {"version": 1, "env": {key: value for key, value in data.items() if _valid_env_name(str(key))}}
    if not isinstance(data.get("env"), dict):
        raise LumaError(f"invalid user config {path}: expected env object")
    return data


def _valid_env_name(key: str) -> bool:
    return bool(key) and not key[0].isdigit() and key.replace("_", "").isalnum()
