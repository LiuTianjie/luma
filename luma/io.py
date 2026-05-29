from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from .errors import LumaError


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise LumaError(f"file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError(f"{path} must contain a YAML mapping")
    return data


def dump_yaml(data: Dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
