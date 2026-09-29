from __future__ import annotations

import os
import shlex
from pathlib import Path

from .errors import LumaError


def load_env_file(path: Path, *, override: bool = False) -> list[str]:
    values = parse_env_file(path)
    loaded = []
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise LumaError(f"invalid env file line {path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise LumaError(f"invalid env var name {path}:{lineno}: {key!r}")
        value = _parse_env_value(value.strip(), path=path, lineno=lineno)
        values[key] = value
    return values


def _parse_env_value(value: str, *, path: Path, lineno: int) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        try:
            return shlex.split(value, comments=False, posix=True)[0]
        except ValueError as exc:
            raise LumaError(f"invalid quoted env value {path}:{lineno}: {exc}") from exc
    return value.split(" #", 1)[0].strip()


# Settings Luma reads from the implicit ./.env. Control credentials are excluded
# because that file usually belongs to the application being deployed.
LUMA_SETTING_NAMES = frozenset(
    {"CLOUDFLARE_API_TOKEN", "EGRESS_SUBSCRIPTION_URL", "TAILSCALE_AUTHKEY", "TRAEFIK_ACME_EMAIL"}
)
LUMA_CREDENTIAL_NAMES = frozenset({"LUMA_CONTROL_URL", "LUMA_DEPLOY_TOKEN", "LUMA_CONTROL_CONTEXT"})


def is_luma_setting(name: str) -> bool:
    return name in LUMA_SETTING_NAMES or (name.startswith("LUMA_") and name not in LUMA_CREDENTIAL_NAMES)


def load_luma_settings(path: Path) -> list[str]:
    """Load only Luma settings from ``path``; return warnings instead of raising."""
    if not path.is_file():
        return []
    skipped = 0
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not is_luma_setting(key):
            continue
        try:
            parsed = _parse_env_value(value.strip(), path=path, lineno=lineno)
        except LumaError:
            skipped += 1
            continue
        if key not in os.environ:
            os.environ[key] = parsed
    if skipped:
        return [f"ignored {skipped} unreadable Luma setting(s) in {path}"]
    return []
