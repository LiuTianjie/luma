"""Manager storage inventory and reviewed, transactional history retention.

SQLite history deletion reuses database pages; estimated payload sizes are not a
promise of immediate filesystem space reclamation. Remote stores are measured
by their owning Agent, never by scanning an unrelated Manager directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from ..errors import LumaError
from . import database
from .state import state_dir

DAY = 86400
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "canceled", "completed", "success", "error", "timed_out", "timeout"})
DEFAULT_POLICY = {"summaryDays": 90, "detailDays": 14, "graceHours": 24}
HISTORY_KINDS = ("buildRuns", "deploymentEvents")


def _schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS storage_inventory_samples (bucket INTEGER PRIMARY KEY, measured_at REAL NOT NULL, components TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS storage_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS storage_history_plans (id TEXT PRIMARY KEY, created_at REAL NOT NULL, eligible_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, policy TEXT NOT NULL, candidates TEXT NOT NULL, result TEXT, alert_plan TEXT)")
    if "alert_plan" not in {row[1] for row in conn.execute("PRAGMA table_info(storage_history_plans)")}:
        conn.execute("ALTER TABLE storage_history_plans ADD COLUMN alert_plan TEXT")


def _expire_plans(conn, now: float) -> None:
    conn.execute("DELETE FROM storage_history_plans WHERE expires_at < ?", (now - 30 * DAY,))


def _policy(conn):
    row = conn.execute("SELECT payload FROM storage_settings WHERE id=1").fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT_POLICY)


def retention_policy(body: dict[str, Any] | None = None) -> dict[str, Any]:
    with database.transaction() as conn:
        database.ensure_initialized(conn)
        _schema(conn)
        policy = _policy(conn)
        if body is not None:
            for key, value in body.items():
                if key not in DEFAULT_POLICY or isinstance(value, bool) or not isinstance(value, int):
                    raise LumaError("retention policy expects integer summaryDays, detailDays and graceHours")
                policy[key] = value
            if not 7 <= policy["summaryDays"] <= 3650 or not 1 <= policy["detailDays"] <= policy["summaryDays"] or not 24 <= policy["graceHours"] <= 720:
                raise LumaError("retention policy requires summaryDays 7..3650, detailDays 1..summaryDays, graceHours 24..720")
            conn.execute("INSERT INTO storage_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (json.dumps(policy),))
        return {**policy, "mode": "reviewed", "automaticDeletion": False}


def _digests(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            result.update(_digests(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_digests(child))
    elif isinstance(value, str):
        result.update(re.findall(r"sha256:[a-f0-9]{64}", value))
    return result


def builder_reference_manifest(state: dict[str, Any], node_name: str = "", *, exclude_task_id: str = "", now: float | None = None) -> dict[str, Any]:
    """Keep every recorded digest, including retries and current/rollback config.

    Old orphan files are candidates only when the complete logical Manager state
    was supplied. No age-based expiration of source snapshot bindings is assumed.
    """
    reasons = []
    reference_state = dict(state)
    for collection in ("agentTasks", "builderTasks", "buildRuns"):
        records = state.get(collection)
        if isinstance(records, dict):
            reference_state[collection] = {key: task for key, task in records.items() if str(key) != exclude_task_id and not (isinstance(task, dict) and task.get("action") == "builder-storage")}
    protected = _digests(reference_state)
    for collection in ("builderTasks", "agentTasks", "buildRuns"):
        records = state.get(collection, {})
        if not isinstance(records, dict):
            reasons.append(f"{collection} reference collection is malformed")
            continue
        for task_id, task in records.items():
            if str(task_id) == exclude_task_id:
                continue
            if not isinstance(task, dict):
                reasons.append(f"{collection} contains malformed task metadata")
                continue
            assigned = str(task.get("nodeName") or task.get("builderNode") or task.get("buildNode") or task.get("node") or "")
            if node_name and assigned and node_name != assigned:
                continue
            action = str(task.get("action") or task.get("kind") or "")
            if action == "builder-storage":
                continue
            if str(task.get("status") or "").lower() not in TERMINAL:
                reasons.append("Active or retryable tasks protect the builder store")
    for name in ("builderSourceSnapshots", "deployments"):
        if name in state and not isinstance(state[name], dict):
            reasons.append(f"{name} reference collection is malformed")
    required = ("builderTasks", "agentTasks", "buildRuns", "builderSourceSnapshots", "deployments")
    complete = bool(state.get("clusterId")) and (state.get("_storageReferenceCoverage") is True or all(name in state for name in required))
    if not complete:
        reasons.append("Full Manager reference inventory was not supplied")
    return {"protectedDigests": sorted(protected), "coverageComplete": complete and not any("malformed" in reason for reason in reasons), "collectedAt": time.time() if now is None else now, "blockedReasons": sorted(set(reasons))}


def storage_inventory(state: dict[str, Any] | None = None, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    root = state_dir()
    files = [("database", "Manager database", database.database_path()), ("databaseWal", "SQLite write-ahead log", Path(str(database.database_path()) + "-wal")), ("databaseShm", "SQLite shared memory", Path(str(database.database_path()) + "-shm")), ("legacyConfig", "Legacy configuration / migration source", root / "control.json"), ("metrics", "Local metric history", root / "metrics-history.json")]
    components = []
    for key, label, path in files:
        try:
            size = path.lstat().st_size if path.exists() and not path.is_symlink() else (None if path.is_symlink() else 0)
        except OSError:
            size = None
        components.append({"id": key, "label": label, "location": "manager", "bytes": size, "status": "measured" if size is not None else "unknown"})
    # Migration backups are only counted under the Manager's own state directory.
    backup_bytes = 0
    backup_count = 0
    for path in root.glob("control.json*"):
        if path.name == "control.json" or path.is_symlink() or not path.is_file():
            continue
        backup_bytes += path.stat().st_size
        backup_count += 1
    components.append({"id": "migrationBackups", "label": "Migration backups", "location": "manager", "bytes": backup_bytes, "status": "measured", "fileCount": backup_count})
    for key, label, reason in (("builder", "Builder source / analysis artifacts", "Run an inventory task on the builder Agent"), ("registry", "Registry images", "Separate registry retention and garbage collection capability"), ("buildkit", "BuildKit cache", "Not measured by the Manager"), ("trivy", "Trivy cache", "Not measured by the Manager"), ("volumes", "Application volumes", "Outside Luma history cleanup scope")):
        components.append({"id": key, "label": label, "location": "external", "bytes": None, "status": "unknown", "reason": reason})
    with database.transaction() as conn:
        database.ensure_initialized(conn)
        _schema(conn)
        _expire_plans(conn, now)
        previous = conn.execute("SELECT measured_at,components FROM storage_inventory_samples WHERE bucket < ? ORDER BY bucket DESC LIMIT 1", (int(now // 3600),)).fetchone()
        previous_components = {item["id"]: item for item in json.loads(previous["components"])} if previous else {}
        for item in components:
            old = previous_components.get(item["id"], {}).get("bytes")
            item["growthBytes"] = item["bytes"] - old if item["bytes"] is not None and old is not None else None
        conn.execute("INSERT OR REPLACE INTO storage_inventory_samples VALUES(?,?,?)", (int(now // 3600), now, json.dumps(components)))
        conn.execute("DELETE FROM storage_inventory_samples WHERE measured_at < ?", (now - 90 * DAY,))
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        counts = {row[0]: row[1] for row in conn.execute("SELECT kind,COUNT(*) FROM control_entities GROUP BY kind")}
        plans = [dict(row) for row in conn.execute("SELECT id AS planId,created_at AS createdAt,eligible_at AS eligibleAfter,expires_at AS expiresAt,status FROM storage_history_plans ORDER BY created_at DESC LIMIT 20")]
    return {"components": components, "measuredAt": now, "growthSince": previous["measured_at"] if previous else None, "totalKnownBytes": sum(item["bytes"] or 0 for item in components), "databaseAllocatedBytes": page_count * page_size, "databaseReusableBytes": free_pages * page_size, "recordCounts": counts, "historyPlans": plans, "policy": retention_policy(), "note": "Payload deletion makes SQLite pages reusable; it does not shrink the database file immediately. External stores and application volumes are not included in known bytes."}


def _references(conn) -> set[str]:
    values: set[str] = set()
    def visit(item):
        if isinstance(item, str):
            values.add(item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    for row in conn.execute("SELECT kind,status,payload FROM control_entities WHERE kind NOT IN ('buildRuns','deploymentEvents')"):
        if row["kind"] in {"agentTasks", "builderTasks"} and row["status"] in TERMINAL:
            continue
        visit(json.loads(row["payload"]))
    for row in conn.execute("SELECT payload FROM control_config WHERE key IN ('deployments','laeRuntime','build')"):
        visit(json.loads(row[0]))
    # Active history items can retain IDs of predecessors that they retry.
    for row in conn.execute("SELECT payload FROM control_entities WHERE kind IN ('buildRuns','deploymentEvents') AND status NOT IN (" + ",".join("?" for _ in TERMINAL) + ")", tuple(TERMINAL)):
        visit(json.loads(row[0]))
    return values


def _fingerprint(conn, row) -> tuple[str, int, int]:
    events = list(conn.execute("SELECT stream,position,payload FROM control_events WHERE kind=? AND entity_id=? ORDER BY stream,position", (row["kind"], row["id"])))
    blob = json.dumps([dict(row), [tuple(event) for event in events]], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest(), len(row["payload"].encode()), sum(len(event["payload"].encode()) for event in events)


def preview_history(*, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    with database.transaction() as conn:
        database.ensure_initialized(conn)
        _schema(conn)
        policy = _policy(conn)
        _expire_plans(conn, now)
        refs = _references(conn)
        candidates = []
        protected_count = 0
        rows = conn.execute("SELECT * FROM control_entities WHERE kind IN ('buildRuns','deploymentEvents') AND updated_at > 0 AND updated_at < ? ORDER BY updated_at ASC", (now - policy["detailDays"] * DAY,))
        truncated = False
        for row in rows:
            if row["status"] not in TERMINAL or row["id"] in refs:
                protected_count += 1
                continue
            fingerprint, summary_bytes, detail_bytes = _fingerprint(conn, row)
            delete_summary = row["updated_at"] < now - policy["summaryDays"] * DAY
            if not delete_summary and not detail_bytes:
                continue
            if len(candidates) >= 1000:
                truncated = True
                break
            candidates.append({"kind": row["kind"], "id": row["id"], "app": row["app"], "updatedAt": row["updated_at"], "action": "summary" if delete_summary else "details", "estimatedBytes": detail_bytes + (summary_bytes if delete_summary else 0), "fingerprint": fingerprint})
        from .alerting import retention_plan
        alert_plan = retention_plan(conn, cutoff=now - policy["summaryDays"] * DAY)
        encoded_alert = json.dumps(alert_plan)
        plan_id = secrets.token_hex(16)
        eligible = now + policy["graceHours"] * 3600
        expires = eligible + 7 * DAY
        encoded = json.dumps(candidates)
        encoded_policy = json.dumps(policy)
        existing = conn.execute("SELECT id,created_at,eligible_at,expires_at FROM storage_history_plans WHERE status='preview' AND expires_at>? AND policy=? AND candidates=? AND alert_plan=? ORDER BY created_at DESC LIMIT 1", (now, encoded_policy, encoded, encoded_alert)).fetchone()
        if existing:
            plan_id, created, eligible, expires = tuple(existing)
        else:
            if conn.execute("SELECT COUNT(*) FROM storage_history_plans WHERE status='preview' AND expires_at>?", (now,)).fetchone()[0] >= 100:
                raise LumaError("too many active history cleanup previews; wait for existing plans to expire or apply them")
            created = now
            conn.execute("INSERT INTO storage_history_plans (id,created_at,eligible_at,expires_at,status,policy,candidates,result,alert_plan) VALUES(?,?,?,?,?,?,?,NULL,?)", (plan_id, now, eligible, expires, "preview", encoded_policy, encoded, encoded_alert))
        return {"planId": plan_id, "status": "preview", "createdAt": created, "eligibleAfter": eligible, "expiresAt": expires, "policy": policy, "alertHistory": alert_plan, "candidateCount": len(candidates), "protectedCount": protected_count, "estimatedReclaimableBytes": sum(item["estimatedBytes"] for item in candidates), "candidates": [{key: value for key, value in item.items() if key != "fingerprint"} for item in candidates], "truncated": truncated, "note": "Estimate covers serialized payloads. Running, retry-referenced and deployment-referenced records are protected. Apply is manual after the grace period."}


def apply_history(body: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if body.get("confirmed") is not True:
        raise LumaError("history cleanup requires explicit confirmation")
    with database.transaction() as conn:
        database.ensure_initialized(conn)
        _schema(conn)
        plan = conn.execute("SELECT * FROM storage_history_plans WHERE id=?", (str(body.get("planId") or ""),)).fetchone()
        if not plan:
            raise LumaError("history cleanup plan not found")
        if plan["status"] == "applied":
            return json.loads(plan["result"])
        if now >= plan["expires_at"]:
            raise LumaError("history cleanup plan expired")
        if now < plan["eligible_at"]:
            raise LumaError("history cleanup grace period has not elapsed")
        if _policy(conn) != json.loads(plan["policy"]):
            raise LumaError("retention policy changed; create a new preview")
        refs = _references(conn)
        removed = 0
        skipped = 0
        estimated = 0
        for candidate in json.loads(plan["candidates"]):
            row = conn.execute("SELECT * FROM control_entities WHERE kind=? AND id=?", (candidate["kind"], candidate["id"])).fetchone()
            if not row or row["status"] not in TERMINAL or row["id"] in refs or _fingerprint(conn, row)[0] != candidate["fingerprint"]:
                skipped += 1
                continue
            if candidate["action"] == "summary":
                conn.execute("DELETE FROM control_entities WHERE kind=? AND id=?", (candidate["kind"], candidate["id"]))
            else:
                conn.execute("DELETE FROM control_events WHERE kind=? AND entity_id=?", (candidate["kind"], candidate["id"]))
                summary = json.loads(row["payload"])
                if isinstance(summary, dict):
                    summary["detailsExpiredAt"] = int(now)
                    summary["detailsRetentionDays"] = json.loads(plan["policy"])["detailDays"]
                    conn.execute("UPDATE control_entities SET payload=? WHERE kind=? AND id=?", (json.dumps(summary, separators=(",", ":"), sort_keys=True), candidate["kind"], candidate["id"]))
            removed += 1
            estimated += candidate["estimatedBytes"]
        from .alerting import prune
        alert_result = prune(conn, json.loads(plan["alert_plan"])) if plan["alert_plan"] else {"incidentsDeleted": 0, "deliveriesDeleted": 0}
        result = {"alertHistory": alert_result, "planId": plan["id"], "status": "applied", "appliedAt": now, "removedCount": removed, "skippedCount": skipped, "estimatedReclaimedBytes": estimated}
        conn.execute("UPDATE storage_history_plans SET status='applied',result=? WHERE id=?", (json.dumps(result), plan["id"]))
        return result


def dispatch(method: str, resource: str, body: dict[str, Any] | None = None, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
    resource = resource.strip("/")
    if method == "GET" and resource.startswith("history/plans/"):
        plan_id = resource.removeprefix("history/plans/")
        with database.transaction() as conn:
            database.ensure_initialized(conn)
            _schema(conn)
            row = conn.execute("SELECT * FROM storage_history_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise LumaError("history cleanup plan not found")
            candidates = json.loads(row["candidates"])
            return {"planId": row["id"], "createdAt": row["created_at"], "eligibleAfter": row["eligible_at"], "expiresAt": row["expires_at"], "status": row["status"], "policy": json.loads(row["policy"]), "alertHistory": json.loads(row["alert_plan"]) if row["alert_plan"] else None, "candidateCount": len(candidates), "estimatedReclaimableBytes": sum(item["estimatedBytes"] for item in candidates), "candidates": [{key: value for key, value in item.items() if key != "fingerprint"} for item in candidates], "result": json.loads(row["result"]) if row["result"] else None}
    if method == "GET" and resource == "inventory":
        return storage_inventory()
    if resource == "policy" and method in {"GET", "PUT", "POST"}:
        return retention_policy(None if method == "GET" else body or {})
    if resource == "history/preview" and method == "POST":
        return preview_history()
    if resource == "history/apply" and method == "POST":
        return apply_history(body or {})
    raise LumaError("unknown storage governance operation")
