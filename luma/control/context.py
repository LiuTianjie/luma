from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from ..errors import LumaError
from ..io import atomic_write_text


def config_home() -> Path:
    return Path(os.environ.get("LUMA_CONFIG_HOME") or Path.home() / ".config" / "luma")


def contexts_dir() -> Path:
    return config_home() / "contexts"


def current_path() -> Path:
    return config_home() / "current-context"


def _context_path(cluster_id: str) -> Path:
    safe = "".join(ch for ch in cluster_id if ch.isalnum() or ch in {"-", "_", "."})
    if not safe:
        raise LumaError("invalid cluster id")
    return contexts_dir() / f"{safe}.json"


def save_context(
    *,
    endpoint: str,
    cluster_id: str,
    token: str,
    insecure: bool = False,
    resolve_ip: str | None = None,
) -> None:
    contexts_dir().mkdir(parents=True, exist_ok=True)
    data = {
        "endpoint": endpoint.rstrip("/"),
        "clusterId": cluster_id,
        "token": token,
        "insecure": bool(insecure),
    }
    if resolve_ip:
        data["resolveIp"] = resolve_ip
    path = _context_path(cluster_id)
    atomic_write_text(
        path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600
    )
    atomic_write_text(current_path(), cluster_id + "\n")


def list_contexts() -> List[Dict[str, Any]]:
    if not contexts_dir().exists():
        return []
    items = []
    current = current_context_name(required=False)
    for path in sorted(contexts_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # A truncated/hand-edited context file must not crash `context list`;
            # skip the unreadable entry rather than aborting the whole listing.
            continue
        if isinstance(data, dict):
            data = dict(data)
            data["current"] = data.get("clusterId") == current
            data.pop("token", None)
            items.append(data)
    return items


def current_context_name(*, required: bool = True) -> str | None:
    path = current_path()
    if not path.exists():
        if required:
            raise LumaError("not logged in: run luma login <control-url> --token <management-token>")
        return None
    name = path.read_text(encoding="utf-8").strip()
    if not name and required:
        raise LumaError("current context is empty: run luma login again")
    return name or None


def load_current_context() -> Dict[str, Any]:
    name = current_context_name(required=True)
    assert name is not None
    path = _context_path(name)
    if not path.exists():
        raise LumaError(f"current context not found: {name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LumaError(
            f"invalid context: {name} (file is corrupt — run luma login again)"
        ) from exc
    if not isinstance(data, dict) or not data.get("endpoint") or not data.get("token"):
        raise LumaError(f"invalid context: {name}")
    return data


def use_context(cluster_id: str) -> None:
    path = _context_path(cluster_id)
    if not path.exists():
        raise LumaError(f"unknown context: {cluster_id}")
    atomic_write_text(current_path(), cluster_id + "\n")
