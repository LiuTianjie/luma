"""Indexed, cursor-paged Control history reads from the manager SQLite database.

Authentication belongs to the HTTP handler. These functions never materialize
all Control state and never delete operational or historical records.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from ..errors import LumaError
from .database import ensure_initialized, transaction

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
KINDS = {"build": "buildRuns", "deployment": "deploymentEvents"}
PUBLIC_KINDS = {value: key for key, value in KINDS.items()}
_SOURCE_SQL = "CASE WHEN e.kind='buildRuns' THEN 'build' ELSE COALESCE(NULLIF(json_extract(e.payload,'$.origin'),''),'cli') END"


def _limit(query: Mapping[str, Any]) -> int:
    value = query.get("limit", DEFAULT_LIMIT)
    try:
        if isinstance(value, bool) or str(value).strip() != str(int(value)):
            raise ValueError
        limit = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LumaError("limit must be an integer between 1 and 100") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise LumaError("limit must be an integer between 1 and 100")
    return limit


def _time(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            stamp = int(text)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            stamp = int(parsed.timestamp())
        if not 0 <= stamp <= 253402300799:
            raise ValueError
        return stamp
    except (TypeError, ValueError, OverflowError) as exc:
        raise LumaError(f"{name} must be Unix seconds or an RFC3339 timestamp with timezone") from exc


def _filters(query: Mapping[str, Any], *, fixed_kind: str = "") -> dict[str, Any]:
    result = {key: str(query.get(key) or "").strip() for key in ("app", "status", "source", "kind")}
    if fixed_kind:
        if result["kind"] and result["kind"] != fixed_kind:
            raise LumaError("kind does not match this history endpoint")
        result["kind"] = fixed_kind
    if result["kind"] and result["kind"] not in KINDS:
        raise LumaError("kind must be build or deployment")
    if result["source"] and result["source"] not in {"build", "cli", "dashboard"}:
        raise LumaError("source must be build, cli, or dashboard")
    if len(result["app"]) > 255 or len(result["status"]) > 80:
        raise LumaError("history app or status filter is too long")
    result["since"] = _time(query.get("since"), "since")
    result["until"] = _time(query.get("until"), "until")
    if result["since"] is not None and result["until"] is not None and result["since"] > result["until"]:
        raise LumaError("since must not be later than until")
    return result


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _encode_cursor(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(raw: Any, *, scope: str) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or len(raw) > 4096:
        raise LumaError("invalid history cursor")
    try:
        value = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise LumaError("invalid history cursor") from exc
    if not isinstance(value, dict) or value.get("v") != 1 or value.get("scope") != scope:
        raise LumaError("history cursor does not match the requested filters or record")
    return value


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9223372036854775807


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"])
    except (ValueError, TypeError) as exc:
        raise LumaError("stored history record is invalid") from exc
    if not isinstance(value, dict):
        raise LumaError("stored history record is invalid")
    value.pop("__luma_event_streams__", None)
    return value


def _build_summary(run: dict[str, Any]) -> dict[str, Any]:
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    return {
        "id": str(run.get("id") or ""), "status": str(run.get("status") or ""),
        "source": str(run.get("source") or request.get("repoUrl") or request.get("repository") or ""),
        "buildNode": str(run.get("buildNode") or request.get("buildNode") or ""),
        "mode": str(run.get("mode") or "builder"), "projectKey": str(run.get("projectKey") or ""),
        "providerId": str(request.get("providerId") or ""), "repository": str(request.get("repository") or ""),
        "ref": str(request.get("ref") or ""), "message": str(run.get("message") or "")[:500],
        "createdAt": int(run.get("createdAt") or 0), "updatedAt": int(run.get("updatedAt") or 0),
        "completedAt": int(run.get("completedAt") or 0),
        "retryOf": str(run.get("retryOf") or ""), "retryRootId": str(run.get("retryRootId") or ""),
        "detailsExpiredAt": int(run.get("detailsExpiredAt") or 0),
        "detailsRetentionDays": int(run.get("detailsRetentionDays") or 0),
    }


def _summary(row: Any) -> dict[str, Any]:
    data = _payload(row)
    build = row["kind"] == "buildRuns"
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    git = data.get("gitSource") if isinstance(data.get("gitSource"), dict) else {}
    repository = str(request.get("repository") or request.get("repoUrl") or data.get("source") or "") if build else str(git.get("repository") or git.get("repoUrl") or "")
    return {
        "id": row["id"], "kind": PUBLIC_KINDS[row["kind"]], "application": row["app"] or "",
        "source": "build" if build else str(data.get("origin") or "cli"),
        "status": row["status"] or "", "createdAt": row["created_at"] or 0, "updatedAt": row["updated_at"] or 0,
        "title": str(row["app"] or data.get("name") or repository or row["id"]),
        "repository": repository, "ref": str(request.get("ref") or "") if build else str(git.get("ref") or ""),
        "message": str(data.get("message") or data.get("error") or "")[:500],
        "buildNode": str(data.get("buildNode") or request.get("buildNode") or "") if build else "",
        "stepCount": int(row["step_count"] or 0),
        "retryOf": str(data.get("retryOf") or "") if build else "",
        "retryRootId": str(data.get("retryRootId") or "") if build else "",
        "detailsExpiredAt": int(data.get("detailsExpiredAt") or 0),
        "detailsRetentionDays": int(data.get("detailsRetentionDays") or 0),
    }


def _list(query: Mapping[str, Any], *, fixed_kind: str = "", legacy: str = "") -> dict[str, Any]:
    limit = _limit(query)
    filters = _filters(query, fixed_kind=fixed_kind)
    insertion_order = legacy == "events"
    scope = _fingerprint({"list": filters, "order": "deployment-insertion" if insertion_order else "timeline"})
    cursor = _decode_cursor(query.get("cursor"), scope=scope)
    if cursor:
        key = cursor.get("key")
        valid_key = isinstance(key, list) and len(key) == (2 if insertion_order else 3) and _integer(key[0])
        if valid_key:
            valid_key = _integer(key[1]) if insertion_order else (
                isinstance(key[1], str) and isinstance(key[2], str) and key[2] in PUBLIC_KINDS
            )
        if not valid_key or not _integer(cursor.get("watermark")):
            raise LumaError("invalid history cursor")
    conditions = ["e.kind IN ('buildRuns','deploymentEvents')"]
    params: list[Any] = []
    for key, column in (("kind", "e.kind"), ("app", "e.app"), ("status", "e.status"), ("source", _SOURCE_SQL)):
        value = filters[key]
        if value:
            conditions.append(column + " = ?")
            params.append(KINDS[value] if key == "kind" else value)
    for key, op in (("since", ">="), ("until", "<=")):
        if filters[key] is not None:
            conditions.append(f"e.created_at {op} ?")
            params.append(filters[key])
    with transaction(immediate=False) as conn:
        ensure_initialized(conn)
        watermark = cursor["watermark"] if cursor else int(conn.execute("SELECT COALESCE(MAX(rowid),0) FROM control_entities").fetchone()[0])
        conditions.append("e.rowid <= ?")
        params.append(watermark)
        if cursor:
            conditions.append("(e.created_at,e.rowid) < (?,?)" if insertion_order else "(e.created_at,e.id,e.kind) < (?,?,?)")
            params.extend(cursor["key"])
        ordering = "e.created_at DESC,e.rowid DESC" if insertion_order else "e.created_at DESC,e.id DESC,e.kind DESC"
        rows = conn.execute(
            "SELECT e.rowid AS insertion_order,e.*, (SELECT COUNT(*) FROM control_events s WHERE s.kind=e.kind AND s.entity_id=e.id AND s.stream=CASE WHEN e.kind='buildRuns' THEN 'events' ELSE 'steps' END) AS step_count "
            "FROM control_entities e WHERE " + " AND ".join(conditions) + " ORDER BY " + ordering + " LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last = rows[-1]
        key = [last["created_at"], last["insertion_order"]] if insertion_order else [last["created_at"], last["id"], last["kind"]]
        next_cursor = _encode_cursor({"v": 1, "scope": scope, "watermark": watermark, "key": key})
    page = {"limit": limit, "nextCursor": next_cursor, "hasMore": has_more}
    if legacy == "runs":
        return {"runs": [_build_summary(_payload(row)) for row in rows], "page": page}
    if legacy == "events":
        return {"events": [{key: value for key, value in _payload(row).items() if key != "steps"} for row in rows], "page": page}
    return {"items": [_summary(row) for row in rows], "page": page}


def list_history(query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _list(query or {})


def list_builds(query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _list(query or {}, fixed_kind="build", legacy="runs")


def list_deployments(query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _list(query or {}, fixed_kind="deployment", legacy="events")


def get_history(kind: str, record_id: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    if kind not in KINDS:
        raise LumaError("kind must be build or deployment")
    limit = _limit(query)
    scope = _fingerprint({"detail": [kind, record_id]})
    cursor = _decode_cursor(query.get("cursor"), scope=scope)
    if cursor and (not _integer(cursor.get("after")) or not _integer(cursor.get("through")) or cursor["after"] > cursor["through"]):
        raise LumaError("invalid history cursor")
    internal_kind, stream = KINDS[kind], "events" if kind == "build" else "steps"
    with transaction(immediate=False) as conn:
        ensure_initialized(conn)
        row = conn.execute("SELECT e.*,(SELECT COUNT(*) FROM control_events s WHERE s.kind=e.kind AND s.entity_id=e.id AND s.stream=?) AS step_count FROM control_entities e WHERE e.kind=? AND e.id=?", (stream, internal_kind, record_id)).fetchone()
        if row is None:
            raise LumaError(f"{kind} history record not found: {record_id}")
        through = cursor["through"] if cursor else int(conn.execute("SELECT COALESCE(MAX(position),-1) FROM control_events WHERE kind=? AND entity_id=? AND stream=?", (internal_kind, record_id, stream)).fetchone()[0])
        after = cursor["after"] if cursor else -1
        steps = conn.execute("SELECT position,payload FROM control_events WHERE kind=? AND entity_id=? AND stream=? AND position>? AND position<=? ORDER BY position LIMIT ?", (internal_kind, record_id, stream, after, through, limit + 1)).fetchall()
    has_more = len(steps) > limit
    steps = steps[:limit]
    next_cursor = _encode_cursor({"v": 1, "scope": scope, "after": steps[-1]["position"], "through": through}) if has_more else None
    data = _payload(row)
    if kind == "build":
        record = _build_summary(data)
        record["message"] = str(data.get("message") or "")[:4000]
        record["request"] = data.get("request") if isinstance(data.get("request"), dict) else {}
        record["result"] = data.get("result") if isinstance(data.get("result"), dict) else {}
    else:
        record = {key: value for key, value in data.items() if key != "steps"}
    return {"item": _summary(row), "record": record, "events": [_payload(step) for step in steps],
            "page": {"limit": limit, "nextCursor": next_cursor, "hasMore": has_more}}


def get_build(record_id: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    detail = get_history("build", record_id, query)
    return {"run": {**detail["record"], "events": detail["events"]}, "eventsPage": detail["page"]}


def get_deployment(record_id: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    detail = get_history("deployment", record_id, query)
    return {"event": {**detail["record"], "steps": detail["events"]}, "stepsPage": detail["page"]}
