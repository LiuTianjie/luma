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
