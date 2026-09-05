"""Conservative, explicitly reviewed cleanup of builder content-addressed files.

Only the configured builder snapshot store is in scope. Registry/BuildKit caches
have different ownership and are never inferred from this directory.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import LumaError

MIN_GRACE_SECONDS = 86400
PLAN_TTL_SECONDS = 7 * 86400
MAX_FILES = 100000
CONTENT_PATH = re.compile(r"(?:sha256|artifacts/(?:[a-z0-9-]+/){1,2}sha256)/([a-f0-9]{2})/([a-f0-9]{64})\.(tar|json)$")


def _safe_root(root: Path | None) -> Path:
    if root is None:
        from .builder_executor import snapshot_store_root
        root = snapshot_store_root()
    root = Path(root)
    if not root.is_absolute():
        raise LumaError("builder storage root must be absolute")
    # A configured root must not redirect into another store via symlinks.
    for part in (root, *root.parents):
        if part.is_symlink():
            raise LumaError("builder storage root contains a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
        raise LumaError("builder storage root must be owned by this Agent and not writable by other users")
    return root


@contextmanager
def execution_guard(*, root: Path | None = None):
    """Cross-process exclusion with builds/exports on this builder store."""
    root = _safe_root(root)
    lockpath = root.parent / ".luma-builder-storage-execution.lock"
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LumaError("builder storage is busy with another build, export or cleanup operation") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def _metadata(root: Path):
    lockpath = root / ".governance.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lockpath, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        dbpath = root / ".governance.sqlite3"
        if dbpath.is_symlink():
            raise LumaError("builder storage metadata is a symlink")
        metadata_fd = os.open(dbpath, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(metadata_fd)
        dbpath.chmod(0o600)
        conn = sqlite3.connect(dbpath)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, eligible REAL NOT NULL, status TEXT NOT NULL, files TEXT NOT NULL, grace INTEGER NOT NULL DEFAULT 86400)")
        if "grace" not in {row[1] for row in conn.execute("PRAGMA table_info(plans)")}:
            conn.execute("ALTER TABLE plans ADD COLUMN grace INTEGER NOT NULL DEFAULT 86400")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _identity(path: Path) -> dict[str, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LumaError("builder content must be a regular file with one link")
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size, "mtimeNs": info.st_mtime_ns}


def _path(root: Path, relative: str) -> Path:
    if not CONTENT_PATH.fullmatch(relative):
        raise LumaError("invalid content-addressed path")
    path = root / relative
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise LumaError("builder content path contains a symlink")
        if ancestor != path and ancestor.exists():
            info = ancestor.stat()
            if info.st_mode & 0o022 or info.st_uid != os.geteuid():
                raise LumaError("builder content directory is writable by another user")
    return path


def _manifest(payload: dict[str, Any], now: float) -> tuple[set[str], list[str]]:
    reference = payload.get("references")
    if not isinstance(reference, dict):
        return set(), ["Manager reference inventory is unavailable"]
    values = reference.get("protectedDigests")
    reasons = list(reference.get("blockedReasons") or [])
    if reference.get("coverageComplete") is not True:
        reasons.append("Manager reference coverage is incomplete")
    try:
        age = now - float(reference.get("collectedAt", 0))
    except (TypeError, ValueError):
        age = 1e99
    if age < -30 or age > 300:
        reasons.append("Manager reference inventory is stale; request a new operation")
    if not isinstance(values, list) or any(not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in values):
        reasons.append("Manager reference inventory is invalid")
        values = []
    return set(values), reasons


def _scan(root: Path, protected: set[str], now: float, age_seconds: int) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    reasons: list[str] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".trash"]
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                reasons.append("Snapshot store contains a symlink directory")
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative.startswith(".governance"):
                continue
            match = CONTENT_PATH.fullmatch(relative)
            if not match or match[1] != match[2][:2]:
                reasons.append("Snapshot store contains unrecognized files")
                try:
                    identity = _identity(path)
                    items.append({"path": relative, "digest": "", **identity, "status": "unrecognized"})
                except (OSError, LumaError):
                    pass
                continue
            if len(items) >= MAX_FILES:
                raise LumaError("snapshot store scan limit exceeded; cleanup refused")
            try:
                identity = _identity(_path(root, relative))
            except (OSError, LumaError):
                reasons.append("Snapshot content changed or contains unsafe files")
                continue
            digest = "sha256:" + match[2]
            status = "protected" if digest in protected else ("recent" if now - identity["mtimeNs"] / 1e9 < age_seconds else "candidate")
            items.append({"path": relative, "digest": digest, **identity, "status": status})
    return items, sorted(set(reasons))


def execute_builder_storage(payload: dict[str, Any], *, root: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Execute a trusted Agent storage task; requests never supply filesystem paths.

    Preview persists a plan with a seven-day execution window after eligibility.
    Quarantine waits a minimum 24 hours after preview and purge waits another 24 hours. Every destructive operation needs
    a fresh Manager manifest and explicit confirmation. Restore never overwrites.
    """
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise LumaError("builder storage payload must be an object")
    operation = str(payload.get("operation") or "inventory")
    if operation not in {"inventory", "preview", "quarantine", "restore", "purge"}:
        raise LumaError("unknown builder storage operation")
    root = _safe_root(root)
    protected, reasons = _manifest(payload, now)
    raw_grace = payload.get("graceSeconds", MIN_GRACE_SECONDS)
    if isinstance(raw_grace, bool) or not isinstance(raw_grace, int):
        raise LumaError("builder cleanup graceSeconds must be an integer")
    grace = max(MIN_GRACE_SECONDS, min(30 * 86400, raw_grace))
    with execution_guard(root=root), _metadata(root) as conn:
        conn.execute("DELETE FROM plans WHERE status IN ('preview','restored','purged') AND expires < ?", (now - 30 * MIN_GRACE_SECONDS,))
        if operation in {"inventory", "preview"}:
            items, scan_reasons = _scan(root, protected, now, grace)
            reasons += scan_reasons
            all_candidates = [item for item in items if item["status"] == "candidate"]
            candidates = all_candidates[:1000] if operation == "preview" else all_candidates
            plans = [dict(row) for row in conn.execute("SELECT id AS planId,created AS createdAt,expires AS expiresAt,eligible AS eligibleAfter,status FROM plans ORDER BY created DESC LIMIT 20")]
            quarantine_bytes = 0
            for directory, dirs, files in os.walk(root / ".trash", followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for name in files:
                    try:
                        quarantine_bytes += _identity(Path(directory) / name)["bytes"]
                    except (OSError, LumaError):
                        pass
            result: dict[str, Any] = {"plans": plans, "quarantinedBytes": quarantine_bytes, "operation": operation, "root": str(root), "measuredAt": now, "totalBytes": sum(item["bytes"] for item in items), "protectedBytes": sum(item["bytes"] for item in items if item["status"] != "candidate"), "reclaimableBytes": sum(item["bytes"] for item in candidates) if not reasons else 0, "blockedReasons": sorted(set(reasons)), "fileCount": len(items), "candidateCount": len(candidates), "files": candidates if operation == "preview" else items[:1000], "filesTruncated": len(all_candidates) > 1000 if operation == "preview" else len(items) > 1000, "buildkitBytes": None, "trivyBytes": None}
            if operation == "preview" and not reasons:
                plan_id = secrets.token_hex(16)
                encoded = json.dumps(candidates)
                existing = conn.execute("SELECT id,expires,eligible FROM plans WHERE status='preview' AND expires>? AND files=? AND grace=? ORDER BY created DESC LIMIT 1", (now, encoded, grace)).fetchone()
                if existing:
                    plan_id, expires, eligible = tuple(existing)
                else:
                    if conn.execute("SELECT COUNT(*) FROM plans WHERE status='preview' AND expires>?", (now,)).fetchone()[0] >= 100:
                        raise LumaError("too many active builder cleanup previews; wait for expiry or complete existing plans")
                    eligible = now + grace
                    expires = eligible + PLAN_TTL_SECONDS
                    conn.execute("INSERT INTO plans (id,created,expires,eligible,status,files,grace) VALUES (?,?,?,?,?,?,?)", (plan_id, now, expires, eligible, "preview", encoded, grace))
                result.update(planId=plan_id, expiresAt=expires, eligibleAfter=eligible, status="preview")
            return result
        plan_id = str(payload.get("planId") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise LumaError("invalid storage cleanup plan id")
        row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise LumaError("storage cleanup plan not found")
        grace = int(row["grace"])
        if payload.get("confirmed") is not True:
            raise LumaError("storage cleanup operation requires explicit confirmation")
        transition = {"quarantine": "quarantining", "restore": "restoring", "purge": "purging"}[operation]
        final_status = {"quarantine": "quarantined", "restore": "restored", "purge": "purged"}[operation]
        items = json.loads(row["files"])
        if row["status"] == final_status:
            return {"operation": operation, "planId": plan_id, "status": final_status, "fileCount": len(items), "bytes": sum(item["bytes"] for item in items), "eligibleAfter": row["eligible"], "expiresAt": row["expires"]}
        if operation != "restore":
            if reasons:
                raise LumaError("storage cleanup blocked: " + "; ".join(reasons))
            if now >= row["expires"] and row["status"] != transition:
                raise LumaError("storage cleanup plan expired; create a new preview")
            if now < row["eligible"]:
                raise LumaError("storage cleanup grace period has not elapsed")
        expected = {"preview", "quarantining"} if operation == "quarantine" else ({"quarantined", "quarantining", "restoring"} if operation == "restore" else {"quarantined", "purging"})
        if row["status"] not in expected:
            raise LumaError("storage cleanup plan is not in the required state")
        # Preflight the complete plan before changing anything. Transitional
        # states are durable before moves, so a crashed operation can reconcile
        # files already moved/deleted and continue or restore a partial move.
        sources: list[tuple[dict[str, Any], Path, Path | None]] = []
        for item in items:
            if operation != "restore" and item["digest"] in protected:
                raise LumaError("storage cleanup plan now includes referenced content")
            original = _path(root, item["path"])
            quarantined = root / ".trash" / plan_id / item["path"]
            for part in (quarantined, *quarantined.parents):
                if part == root:
                    break
                if part.is_symlink():
                    raise LumaError("storage quarantine path contains a symlink")
            source = original if operation == "quarantine" else quarantined
            destination = quarantined if operation == "quarantine" else (original if operation == "restore" else None)
            identity = {key: item[key] for key in ("device", "inode", "bytes", "mtimeNs")}
            if not source.exists():
                resumable = row["status"] == transition or (operation == "restore" and row["status"] == "quarantining")
                if resumable and (operation == "purge" or (destination is not None and destination.exists() and _identity(destination) == identity)):
                    continue
                raise LumaError("storage content disappeared since preview")
            if _identity(source) != identity:
                raise LumaError("storage content changed since preview")
            if destination is not None and destination.exists():
                raise LumaError("storage destination already exists; refusing to overwrite")
            sources.append((item, source, destination))
        conn.execute("UPDATE plans SET status=? WHERE id=?", (transition, plan_id))
        conn.commit()  # durable recovery intent before the first filesystem mutation
        for item, source, destination in sources:
            # The store is owner-write-only and all Luma writers share the
            # execution lock. Recheck immediately as defense against changes by
            # external tools running under the same trusted account.
            for part in (source, *source.parents):
                if part == root:
                    break
                if part.is_symlink():
                    raise LumaError("storage source path changed to a symlink")
            if _identity(source) != {key: item[key] for key in ("device", "inode", "bytes", "mtimeNs")}:
                raise LumaError("storage content changed during cleanup")
            if operation == "purge":
                source.unlink()
                _fsync_directory(source.parent)
                continue
            assert destination is not None
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for part in (destination, *destination.parents):
                if part == root:
                    break
                if part.is_symlink():
                    raise LumaError("storage destination contains a symlink")
            if destination.exists():
                raise LumaError("storage destination already exists")
            source.rename(destination)
            _fsync_directory(source.parent)
            _fsync_directory(destination.parent)
        eligible = now + grace
        expires = eligible + PLAN_TTL_SECONDS
        conn.execute("UPDATE plans SET status=?,eligible=?,expires=? WHERE id=?", (final_status, eligible, expires, plan_id))
        return {"operation": operation, "planId": plan_id, "status": final_status, "fileCount": len(items), "bytes": sum(item["bytes"] for item in items), "eligibleAfter": eligible, "expiresAt": expires}
