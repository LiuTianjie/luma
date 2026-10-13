"""Append-only build events, separate from whole-state mutation.

This adapter retains the existing history schema and public cursors. Writes
touch one build summary and the new events only; they do not hydrate previous
logs or unrelated Control collections. Event batches and their outcome update
share one transaction, including when multiple producers report concurrently.
"""
from __future__ import annotations

import time
from typing import Any

from .database import (
    append_entity_events,
    ensure_initialized,
    read_entity_metadata,
    transaction,
    update_entity_metadata,
)


def append_build_run_events(
    run_id: str,
    events: list[dict[str, Any]],
    *,
    message_limit: int = 4000,
) -> None:
    if not events:
        return
    entries = [
        {str(key): str(value)[:message_limit] for key, value in event.items() if value is not None}
        for event in events
    ]
    with transaction(owner="append_build_run_events") as conn:
        ensure_initialized(conn)
        try:
            run = read_entity_metadata(conn, "buildRuns", run_id)
        except KeyError:
            return  # Preserve late/unknown build progress's existing no-op contract.
        if not isinstance(run, dict):
            return
        now = int(time.time())
        for entry in entries:
            entry["ts"] = now
            if entry.get("status") == "fail" and run.get("status") not in {"canceling", "canceled"}:
                run["status"] = "failed"
                run["message"] = entry.get("message", "")
        run["updatedAt"] = now
        append_entity_events(conn, "buildRuns", run_id, "events", entries)
        update_entity_metadata(conn, "buildRuns", run_id, run)
