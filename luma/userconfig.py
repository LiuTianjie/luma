from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable

from .errors import LumaError


DEFAULT_CONFIG_PATH = Path.home() / ".luma.config.json"

SECRET_KEYS = {
    "CLOUDFLARE_API_TOKEN",
    "EGRESS_SUBSCRIPTION_URL",
    "LUMA_SUDO_PASSWORD",
    "TAILSCALE_AUTHKEY",
}

ROLE_KEYS = {
    "manager": [
        ("CLOUDFLARE_API_TOKEN", "Cloudflare API token", True, True),
        ("LUMA_DNS_EDGE_TARGET", "Public DNS target for control and edge routes", False, False),
        ("TRAEFIK_ACME_EMAIL", "ACME certificate email", False, True),
        ("TAILSCALE_AUTHKEY", "Tailscale auth key", True, False),
        ("EGRESS_SUBSCRIPTION_URL", "Egress subscription URL", True, False),
        ("LUMA_SUDO_PASSWORD", "sudo password", True, False),
    ],
    "worker": [
        ("TAILSCALE_AUTHKEY", "Tailscale auth key", True, False),
        ("LUMA_SUDO_PASSWORD", "sudo password", True, False),
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
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except PermissionError:
        pass
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
    for key, label, secret, required in prompts:
        current = str(existing_env.get(key) or os.environ.get(key) or "")
        suffix = " [configured]" if current else (" [required]" if required else " [optional]")
        prompt = f"{label}{suffix}: "
        if secret:
            value = getpass.getpass(prompt)
        else:
            value = input_fn(prompt)
        value = value.strip()
        if value:
            values[key] = value
        elif current:
            values[key] = current
        elif required:
            raise LumaError(f"{key} is required for {role} configuration")
    return write_user_config(values, path=path)


def ensure_interactive_config(role: str, *, keys: Iterable[str] | None = None, path: Path | None = None, input_fn=None) -> Path | None:
    if role not in ROLE_KEYS:
        raise LumaError(f"unknown configure role: {role}")
    wanted = set(keys or [item[0] for item in ROLE_KEYS[role]])
    prompts = [item for item in ROLE_KEYS[role] if item[0] in wanted and not os.environ.get(item[0])]
    if not prompts:
        return None
    if not sys.stdin.isatty() and input_fn is None:
        missing_required = [item[0] for item in prompts if item[3]]
        if missing_required:
            missing = ", ".join(missing_required)
            raise LumaError(f"missing local config ({missing}). Run: luma configure --role {role}")
        return None
    if input_fn is None:
        input_fn = input
    values: Dict[str, str] = {}
    print(f"Missing local {role} configuration. Values will be saved to {path or user_config_path()}.")
    for key, label, secret, required in prompts:
        suffix = " [required]" if required else " [optional, press Enter to skip]"
        prompt = f"{label}{suffix}: "
        value = getpass.getpass(prompt) if secret else input_fn(prompt)
        value = value.strip()
        if value:
            values[key] = value
            os.environ[key] = value
        elif required:
            raise LumaError(f"{key} is required for {role} configuration")
    if not values:
        return None
    return write_user_config(values, path=path)


def masked_config_lines(keys: Iterable[str]) -> list[str]:
    lines = []
    for key in sorted(keys):
        marker = "***" if key in SECRET_KEYS or "TOKEN" in key or "PASSWORD" in key or "URL" in key else "set"
        lines.append(f"{key}={marker}")
    return lines


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
