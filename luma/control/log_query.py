"""Bounded, authenticated historical log reads; storage remains in luma-observe."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import LumaError
from ..log_alert_rules import clean_sample_text
from .state import load_state, require_token

MAX_WINDOW = 86400
MAX_OFFSET = 10000
MAX_RESPONSE = 8 * 1024 * 1024
QUERY_SLOTS = threading.BoundedSemaphore(4)
FILTERS = ("task", "allocation", "stream", "contains", "exclude", "errors")
ERROR_PATTERN = r"(?i)(\b(error|critical|fatal|exception|traceback)\b|失败|超时)"


def _stamp(value: str, default: float, now: float) -> float:
    if not value:
        return default
    if len(value) > 64:
        raise LumaError("Invalid log query timestamp")
    relative = re.fullmatch(r"([1-9][0-9]*)([smhd])", value)
    if relative:
        return now - int(relative[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[relative[2]]
    try:
        result = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            result = parsed.timestamp()
        except (ValueError, OverflowError) as exc:
            raise LumaError("Use a Unix timestamp, RFC3339 with timezone, or duration such as 1h") from exc
    if not math.isfinite(result) or not 0 <= result <= 253402300799:
        raise LumaError("Invalid log query timestamp")
    return result


def _integer(value, name: str, low: int, high: int) -> int:
    try:
        number = int(str(value))
    except (ValueError, TypeError) as exc:
        raise LumaError(f"{name} must be an integer") from exc
    if not low <= number <= high:
        raise LumaError(f"{name} must be between {low} and {high}")
    return number


def normalize(params: dict, now: float) -> tuple[dict, int, int]:
    """Freeze the time range in cursors and revalidate every client-supplied value."""
    if set(params) - {"app", "since", "until", "limit", "cursor", *FILTERS}:
        raise LumaError("Unknown log query parameter")
    limit = _integer(params.get("limit", 100), "limit", 1, 500)
    offset = 0
    values = dict(params)
    cursor = params.get("cursor", "")
    if cursor:
        if any(params.get(key) for key in (*FILTERS, "since", "until")):
            raise LumaError("A cursor already contains filters and time range; pass only app, cursor and limit")
        try:
            if len(cursor) > 12000:
                raise ValueError("oversized cursor")
            packed = json.loads(base64.urlsafe_b64decode(cursor))
            if packed["v"] != 1 or packed["query"]["app"] != params.get("app"):
                raise ValueError("cursor scope mismatch")
            values = packed["query"]
            offset = _integer(packed["offset"], "cursor offset", 0, MAX_OFFSET)
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise LumaError("Invalid log query cursor for this application") from exc
    result = {}
    for key in ("app", *FILTERS[:-1]):
        value = values.get(key, "")
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise LumaError(f"Invalid log query {key}")
        result[key] = value
    if not result["app"].strip() or result["app"] == "*":
        raise LumaError("Specify one application ID from 'luma app list'")
    if result["stream"] not in ("", "stdout", "stderr"):
        raise LumaError("stream must be stdout or stderr")
    errors = str(values.get("errors", "")).lower()
    if errors not in ("", "false", "true", "0", "1"):
        raise LumaError("errors must be true or false")
    result["errors"] = errors in ("true", "1")
    end = _stamp(str(values.get("until", "")), now, now)
    start = _stamp(str(values.get("since", "")), end - 3600, now)
    if not 0 < end - start <= MAX_WINDOW:
        raise LumaError("Log query range must be positive and at most 24 hours; split larger investigations")
    if start < 0 or end > now + 60:
        raise LumaError("Log query range must not be in the future or before Unix epoch")
    result.update(since=start, until=end)
    return result, limit, offset


def compile_query(query: dict, limit: int, offset: int) -> str:
    """Only escaped literals and a fixed error heuristic enter the LogsQL pipeline."""
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    selectors = [f'app={quote(query["app"])}']
    for key in ("task", "stream"):
        if query[key]:
            selectors.append(f"{key}={quote(query[key])}")
    parts = ["{" + ",".join(selectors) + "}"]
    if query["allocation"]:
        parts.append("alloc:=" + quote(query["allocation"]))
    for key, prefix in (("contains", ""), ("exclude", "NOT ")):
        if query[key]:
            parts.append(prefix + "_msg:~" + quote(re.escape(query[key])))
    if query["errors"]:
        parts.append("_msg:~" + quote(ERROR_PATTERN))
    # Secondary keys keep same-timestamp batches deterministic; never deduplicate messages.
    return " AND ".join(parts) + (
        f" | sort by (_time desc, _stream_id, alloc, _msg) offset {offset} limit {limit + 1}"
        " | fields _time, _msg, app, task, alloc, stream, node"
    )


def _read_rows(query: dict, limit: int, offset: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "query": compile_query(query, limit, offset), "start": query["since"],
        "end": query["until"], "timeout": "5s",
    })
    request = urllib.request.Request(
        "http://127.0.0.1:9428/select/logsql/query", data=params.encode(), method="POST",
    )
    if not QUERY_SLOTS.acquire(blocking=False):
        raise LumaError("Historical log queries are busy; retry shortly")
    try:
        with urllib.request.urlopen(request, timeout=7) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise LumaError("Log response exceeds 8 MiB; reduce the limit or narrow filters")
        rows = [json.loads(line) for line in raw.splitlines() if line]
        if len(rows) > limit + 1 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid rows")
        return rows
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LumaError("Historical log query unavailable or timed out; check luma-observe and narrow the time range") from exc
    except (ValueError, UnicodeError) as exc:
        raise LumaError("Invalid historical log response from luma-observe") from exc
    finally:
        QUERY_SLOTS.release()


def handle_log_query(token: str, params: dict) -> dict:
    require_token(load_state(), token, token_type="deploy")
    now = time.time()
    query, limit, offset = normalize(params, now)
    rows = _read_rows(query, limit, offset)
    has_more = len(rows) > limit
    entries = []
    for row in rows[:limit]:
        raw = str(row.get("_msg", ""))
        entries.append({
            "observedAt": row.get("_time", ""), "app": row.get("app", ""),
            "task": row.get("task", ""), "allocationId": row.get("alloc", ""),
            "stream": row.get("stream", ""), "node": row.get("node", ""),
            "line": clean_sample_text(raw, 32768),
            "truncated": len(raw.encode("utf-8")) > 32768,
        })
    warnings = [
        "Time filters use collector observation time, not timestamps embedded in messages.",
        "Results cover retained collected logs only; empty results do not prove no errors.",
        "Pagination freezes the range but is not a storage snapshot; late ingestion or retention can change pages.",
    ]
    if query["errors"]:
        warnings.append("Error matching is a text heuristic, not severity parsing; query without it for traceback context.")
    next_cursor = None
    if has_more and offset + limit <= MAX_OFFSET:
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "v": 1, "query": query, "offset": offset + limit,
        }).encode()).decode()
    elif has_more:
        warnings.append("Pagination limit reached; narrow the time range or filters to continue.")
    return {
        "source": "luma-observe", "app": query["app"], "query": query,
        "range": {"since": datetime.fromtimestamp(query["since"], timezone.utc).isoformat(),
                  "until": datetime.fromtimestamp(query["until"], timezone.utc).isoformat()},
        "entries": entries, "returned": len(entries),
        "page": {"limit": limit, "hasMore": has_more, "nextCursor": next_cursor},
        "warnings": warnings, "updatedAt": now,
    }
