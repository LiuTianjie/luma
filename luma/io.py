from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Dict

import yaml

from .errors import LumaError


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically.

    Writes to a temporary file in the same directory, fsyncs it, then renames
    over the target with ``os.replace`` so an interrupted write (crash, OOM,
    kill) can never leave a truncated or partially written file behind. When
    ``mode`` is given it is applied to the temp file before the rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            try:
                tmp_path.chmod(mode)
            except PermissionError:
                pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


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


def write_yaml(path: Path, data: Dict[str, Any]) -> None:
    atomic_write_text(path, dump_yaml(data))
