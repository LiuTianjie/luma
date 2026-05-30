from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from ..errors import LumaError


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
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except PermissionError:
        pass
    current_path().parent.mkdir(parents=True, exist_ok=True)
    current_path().write_text(cluster_id + "\n", encoding="utf-8")


def list_contexts() -> List[Dict[str, Any]]:
    if not contexts_dir().exists():
        return []
    items = []
    current = current_context_name(required=False)
    for path in sorted(contexts_dir().glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
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
            raise LumaError("not logged in: run luma login <control-url> --token <token>")
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
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("endpoint") or not data.get("token"):
        raise LumaError(f"invalid context: {name}")
    return data


def use_context(cluster_id: str) -> None:
    path = _context_path(cluster_id)
    if not path.exists():
        raise LumaError(f"unknown context: {cluster_id}")
    current_path().parent.mkdir(parents=True, exist_ok=True)
    current_path().write_text(cluster_id + "\n", encoding="utf-8")
