"""Authenticated, bounded reads of multiple metric histories from one snapshot."""

from __future__ import annotations

import time
from typing import Any

from luma.errors import LumaError

from .metrics import history_metadata, load_history, load_history_snapshot, retention_seconds
from .state import load_auth_state, require_token


MAX_HISTORY_TARGETS = 32


def handle_metrics_history_batch(token: str, body: dict[str, Any]) -> dict[str, Any]:
    # Auth does not need nodes, task receipts or deployment/build history.
    require_token(load_auth_state(), token, token_type="deploy")
    if not isinstance(body, dict):
        raise LumaError("body must be an object")
    targets = body.get("targets")
    if not isinstance(targets, list) or len(targets) > MAX_HISTORY_TARGETS:
        raise LumaError(f"targets must be an array with at most {MAX_HISTORY_TARGETS} entries")
    raw_window = body.get("window", 3600)
    try:
        if isinstance(raw_window, bool) or not isinstance(raw_window, (int, str)):
            raise ValueError()
        requested_window = int(raw_window)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LumaError("window must be an integer") from exc
    window = min(max(requested_window, 60), retention_seconds())
    now = int(time.time())
    # One JSON parse per request and a common clock for every target. Duplicate
    # targets keep their response position but do not repeat filtering/compaction.
    snapshot = None
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    results = []
    for target in targets:
        kind = target.get("kind") if isinstance(target, dict) else None
        name = target.get("name") if isinstance(target, dict) else None
        kind = kind if isinstance(kind, str) else ""
        name = name if isinstance(name, str) else ""
        item: dict[str, Any] = {"kind": kind, "name": name}
        try:
            if kind not in {"node", "service"}:
                raise LumaError("kind must be node or service")
            if not name.strip():
                raise LumaError("name is required")
            if len(name) > 512:
                raise LumaError("name must be at most 512 characters")
            key = (kind, name)
            if key not in cache:
                if snapshot is None:
                    snapshot = load_history_snapshot()
                series = load_history(kind, name, window=window, now=now, snapshot=snapshot)
                cache[key] = {
                    "kind": kind,
                    "name": name,
                    "series": series,
                    **history_metadata(series, requested_window, now=now),
                }
            item["payload"] = cache[key]
        except (LumaError, OSError, TypeError, ValueError, OverflowError) as exc:
            # A bad target must not discard other targets' successful histories.
            item["error"] = str(exc)
        results.append(item)
    return {"results": results, "updatedAt": now, "maxTargets": MAX_HISTORY_TARGETS}
