"""Bounded resource history stored in a dedicated SQLite WAL database.

Metrics live in ``metrics-history.sqlite3`` inside the Control state directory,
separate from ``control.sqlite3``, so high-frequency node heartbeats never
contend with Control deployment, auth, or state transactions. Legacy
``metrics-history.json`` data is imported automatically once on first open.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .state import state_dir


DATABASE_FILE = "metrics-history.sqlite3"
LEGACY_HISTORY_FILE = "metrics-history.json"
HISTORY_FILE = DATABASE_FILE
SCHEMA_VERSION = 3
SAMPLE_INTERVAL_SECONDS = 30

# Default ~6h at a 30s sample cadence; override via env for longer windows.
DEFAULT_MAX_POINTS = 720
MIN_MAX_POINTS = 60
MAX_MAX_POINTS = 10000

# A node's per-service contribution is dropped from the cross-node sum once it
# goes stale, so a downed node stops inflating a service's total. Sits above
# the 30s cadence and the 120s agent-stale threshold.
SERVICE_SCRATCH_TTL_SECONDS = 180

NODE_SERIES = ("cpuPercent", "memoryUsedPercent", "diskUsedPercent", "inodesUsedPercent")
SERVICE_SERIES = ("cpuPercent", "memoryUsageBytes")

_SCRATCH_KEY = "_serviceScratch"
_BUSY_TIMEOUT_MS = 10000
_BUSY_RETRY_ATTEMPTS = 6
_VACUUM_PAGES = 64
_VACUUM_DELETE_THRESHOLD = 32
_VACUUM_WRITE_INTERVAL = 256
_WAL_AUTOCHECKPOINT_PAGES = 256
_MAX_INIT_CACHE = 64

_SQLITE_CORRUPT_CODES = frozenset({11, 26})  # SQLITE_CORRUPT, SQLITE_NOTADB
_SQLITE_BUSY_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED

_UPSERT_SAMPLE_SQL = (
    "INSERT INTO metric_samples (scope, target_id, series_key, bucket, ts, value) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(scope, target_id, series_key, bucket) DO UPDATE SET "
    "ts = excluded.ts, value = excluded.value "
    "WHERE excluded.ts >= metric_samples.ts"
)

_UPSERT_SCRATCH_SQL = (
    "INSERT INTO service_scratch (service_name, node_name, ts, cpu_percent, memory_usage_bytes) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(service_name, node_name) DO UPDATE SET "
    "ts = excluded.ts, cpu_percent = excluded.cpu_percent, "
    "memory_usage_bytes = excluded.memory_usage_bytes"
)


class _CachedConnection:
    """Thread-local SQLite connection wrapper that closes deterministically on GC."""

    __slots__ = ("key", "conn")

    def __init__(self, key: Tuple[str, int, int], conn: sqlite3.Connection) -> None:
        self.key = key
        self.conn: Optional[sqlite3.Connection] = conn

    def close(self) -> None:
        conn = self.conn
        if conn is not None:
            self.conn = None
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def __del__(self) -> None:
        self.close()


_local = threading.local()
_state_lock = threading.Lock()
_initialized_dbs: Set[Tuple[str, int, int]] = set()
_writer_locks: Dict[str, threading.RLock] = {}
_deleted_since_vacuum: Dict[str, int] = {}
_writes_since_vacuum: Dict[str, int] = {}


def _reset_after_fork() -> None:
    global _local, _state_lock, _initialized_dbs, _writer_locks, _deleted_since_vacuum, _writes_since_vacuum
    _local = threading.local()
    _state_lock = threading.Lock()
    _initialized_dbs = set()
    _writer_locks = {}
    _deleted_since_vacuum = {}
    _writes_since_vacuum = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def history_path() -> Path:
    return state_dir() / DATABASE_FILE


def database_path() -> Path:
    return history_path()


def legacy_history_path() -> Path:
    return state_dir() / LEGACY_HISTORY_FILE


def close_connection() -> None:
    """Close the current thread's cached metrics SQLite connection, if open."""
    entry: Optional[_CachedConnection] = getattr(_local, "entry", None)
    if entry is not None:
        _local.entry = None
        entry.close()


atexit.register(close_connection)


def max_points() -> int:
    raw = os.environ.get("LUMA_METRICS_HISTORY_POINTS")
    if not raw:
        return DEFAULT_MAX_POINTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_POINTS
    return max(MIN_MAX_POINTS, min(MAX_MAX_POINTS, value))


def retention_seconds() -> int:
    """The point cap represents a fixed duration, independent of node count."""
    return max_points() * SAMPLE_INTERVAL_SECONDS


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _empty() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "nodes": {}, "services": {}, _SCRATCH_KEY: {}}


def _private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except (FileNotFoundError, PermissionError, OSError):
        pass


def _create_database_file(path: Path) -> None:
    if path.exists():
        return
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        os.close(fd)
        _private(tmp)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _writer_lock_for(resolved_path: str) -> threading.RLock:
    with _state_lock:
        lock = _writer_locks.get(resolved_path)
        if lock is None:
            if len(_writer_locks) >= _MAX_INIT_CACHE:
                _writer_locks.clear()
            lock = threading.RLock()
            _writer_locks[resolved_path] = lock
        return lock


def _is_busy_error(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in _SQLITE_BUSY_CODES:
        return True
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _is_corruption_error(exc: sqlite3.Error) -> bool:
    if _is_busy_error(exc):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in _SQLITE_CORRUPT_CODES:
        return True
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    return any(
        needle in msg
        for needle in (
            "not a database",
            "malformed",
            "corrupt",
            "disk image",
            "file is encrypted",
            "unsupported file format",
        )
    )


def _db_file_looks_corrupt(st_size: int, *, must_be_initialized: bool) -> bool:
    if st_size == 0:
        return must_be_initialized
    return st_size < 512 or (st_size % 512) != 0


def _reset_corrupt_database(path: Path, exc: object) -> None:
    print(f"metrics history unreadable, starting fresh: {exc}", file=sys.stderr, flush=True)
    close_connection()
    resolved = str(path.resolve())
    with _state_lock:
        stale = [key for key in _initialized_dbs if key[0] == resolved]
        for key in stale:
            _initialized_dbs.discard(key)
        _deleted_since_vacuum.pop(resolved, None)
        _writes_since_vacuum.pop(resolved, None)
    for target in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _valid_points(series: Any) -> List[List[float]]:
    points = []
    for point in series if isinstance(series, list) else []:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        ts, value = _coerce_number(point[0]), _coerce_number(point[1])
        if ts is not None and value is not None:
            points.append([int(ts), value])
    return points


def _compact_points(series: Any, floor: int, ceiling: int, limit: int) -> List[List[float]]:
    # One last-observation point per wall-clock 30s bucket, keeping the
    # observation timestamp so freshness is not overstated.
    buckets: Dict[int, List[float]] = {}
    for ts, value in sorted(_valid_points(series), key=lambda point: point[0]):
        if floor <= ts <= ceiling:
            buckets[int(ts) // SAMPLE_INTERVAL_SECONDS] = [ts, value]
    return [buckets[key] for key in sorted(buckets)][-limit:]


def _import_legacy_json(conn: sqlite3.Connection) -> None:
    legacy_path = legacy_history_path()
    if not legacy_path.exists():
        return
    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"metrics history unreadable, starting fresh: {exc}", file=sys.stderr, flush=True)
        return
    if not isinstance(raw, dict):
        return

    limit = max_points()
    retention = retention_seconds()
    sample_rows: List[Tuple[str, str, str, int, int, float]] = []
    for scope, bucket_key in (("node", "nodes"), ("service", "services")):
        entities = raw.get(bucket_key)
        if not isinstance(entities, dict):
            continue
        for target_id, series_map in entities.items():
            name = str(target_id or "").strip()
            if not name or not isinstance(series_map, dict):
                continue
            for series_key, raw_series in series_map.items():
                key = str(series_key or "").strip()
                if not key or not isinstance(raw_series, list):
                    continue
                points = _valid_points(raw_series)
                if not points:
                    continue
                ceiling = max(int(point[0]) for point in points)
                compacted = _compact_points(points, ceiling - retention, ceiling, limit)
                for ts_val, num_val in compacted:
                    ts_int = int(ts_val)
                    sample_rows.append(
                        (scope, name, key, ts_int // SAMPLE_INTERVAL_SECONDS, ts_int, float(num_val))
                    )
    if sample_rows:
        conn.executemany(_UPSERT_SAMPLE_SQL, sample_rows)

    scratch = raw.get(_SCRATCH_KEY)
    if isinstance(scratch, dict):
        scratch_rows: List[Tuple[str, str, int, Optional[float], Optional[float]]] = []
        for full_name, by_node in scratch.items():
            svc_name = str(full_name or "").strip()
            if not svc_name or not isinstance(by_node, dict):
                continue
            for node_name, contribution in by_node.items():
                n_name = str(node_name or "").strip()
                if not n_name or not isinstance(contribution, dict):
                    continue
                c_ts = _coerce_number(contribution.get("ts"))
                if c_ts is None:
                    continue
                cpu = _coerce_number(contribution.get("cpuPercent"))
                mem = _coerce_number(contribution.get("memoryUsageBytes"))
                scratch_rows.append(
                    (
                        svc_name,
                        n_name,
                        int(c_ts),
                        round(cpu, 2) if cpu is not None else None,
                        round(mem, 2) if mem is not None else None,
                    )
                )
        if scratch_rows:
            conn.executemany(_UPSERT_SCRATCH_SQL, scratch_rows)


def _schema_already_ready(conn: sqlite3.Connection) -> bool:
    try:
        rows = conn.execute(
            "SELECT key, value FROM metrics_meta WHERE key IN ('schema_version', 'legacy_json_imported')"
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    meta = {row[0]: row[1] for row in rows}
    return meta.get("schema_version") == str(SCHEMA_VERSION) and meta.get("legacy_json_imported") == "1"


def _initialize_database(conn: sqlite3.Connection, path: Path, db_key: Tuple[str, int, int]) -> None:
    resolved_path = db_key[0]
    with _state_lock:
        if db_key in _initialized_dbs:
            return

    # Fast read-only check so a second process opening an already-initialized
    # DB never takes a write lock on reads.
    if _schema_already_ready(conn):
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        if not mode_row or str(mode_row[0]).lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        with _state_lock:
            if len(_initialized_dbs) >= _MAX_INIT_CACHE:
                _initialized_dbs.clear()
            _initialized_dbs.add(db_key)
        return

    lock = _writer_lock_for(resolved_path)
    with lock:
        with _state_lock:
            if db_key in _initialized_dbs:
                return

        for attempt in range(_BUSY_RETRY_ATTEMPTS):
            try:
                # Set auto_vacuum BEFORE journal_mode=WAL so page 1 is created
                # with incremental auto_vacuum active on a brand-new database.
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute(f"PRAGMA wal_autocheckpoint = {_WAL_AUTOCHECKPOINT_PAGES}")

                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS metrics_meta ("
                        "key TEXT PRIMARY KEY, "
                        "value TEXT NOT NULL"
                        ")"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS metric_samples ("
                        "scope TEXT NOT NULL, "
                        "target_id TEXT NOT NULL, "
                        "series_key TEXT NOT NULL, "
                        "bucket INTEGER NOT NULL, "
                        "ts INTEGER NOT NULL, "
                        "value REAL NOT NULL, "
                        "PRIMARY KEY (scope, target_id, series_key, bucket)"
                        ")"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_metric_samples_scope_target_ts "
                        "ON metric_samples (scope, target_id, ts)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_metric_samples_ts "
                        "ON metric_samples (ts)"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS service_scratch ("
                        "service_name TEXT NOT NULL, "
                        "node_name TEXT NOT NULL, "
                        "ts INTEGER NOT NULL, "
                        "cpu_percent REAL, "
                        "memory_usage_bytes REAL, "
                        "PRIMARY KEY (service_name, node_name)"
                        ")"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_service_scratch_node "
                        "ON service_scratch (node_name)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_service_scratch_ts "
                        "ON service_scratch (ts)"
                    )
                    conn.execute(
                        "INSERT INTO metrics_meta (key, value) VALUES ('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(SCHEMA_VERSION),),
                    )
                    imported = conn.execute(
                        "SELECT value FROM metrics_meta WHERE key = 'legacy_json_imported'"
                    ).fetchone()
                    if not imported or imported[0] != "1":
                        _import_legacy_json(conn)
                        conn.execute(
                            "INSERT INTO metrics_meta (key, value) VALUES ('legacy_json_imported', '1') "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                        )
                    conn.execute("COMMIT")
                except BaseException:
                    _rollback_quietly(conn)
                    raise
                break
            except sqlite3.Error as exc:
                if _is_busy_error(exc) and attempt + 1 < _BUSY_RETRY_ATTEMPTS:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise

        for target in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            _private(target)

        with _state_lock:
            if len(_initialized_dbs) >= _MAX_INIT_CACHE:
                _initialized_dbs.clear()
            _initialized_dbs.add(db_key)


def _get_connection(*, create: bool) -> Optional[sqlite3.Connection]:
    path = history_path()
    resolved = str(path.resolve())
    cached: Optional[_CachedConnection] = getattr(_local, "entry", None)

    try:
        st = path.stat()
    except FileNotFoundError:
        if cached is not None:
            close_connection()
        if not create and not legacy_history_path().exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        _create_database_file(path)
        st = path.stat()
    else:
        db_key = (resolved, st.st_dev, st.st_ino)
        if cached is not None and cached.key == db_key and cached.conn is not None:
            if _db_file_looks_corrupt(st.st_size, must_be_initialized=True):
                raise sqlite3.DatabaseError("file is not a database")
            return cached.conn
        if _db_file_looks_corrupt(st.st_size, must_be_initialized=False):
            raise sqlite3.DatabaseError("file is not a database")

    db_key = (resolved, st.st_dev, st.st_ino)
    if cached is not None:
        close_connection()

    conn = sqlite3.connect(
        str(path),
        timeout=_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA wal_autocheckpoint = {_WAL_AUTOCHECKPOINT_PAGES}")
        _initialize_database(conn, path, db_key)
    except BaseException:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise

    _local.entry = _CachedConnection(db_key, conn)
    return conn


def _maybe_incremental_vacuum(conn: sqlite3.Connection, resolved_path: str, deleted_rows: int) -> None:
    should_vacuum = False
    with _state_lock:
        if deleted_rows > 0:
            _deleted_since_vacuum[resolved_path] = _deleted_since_vacuum.get(resolved_path, 0) + deleted_rows
        writes = _writes_since_vacuum.get(resolved_path, 0) + 1
        _writes_since_vacuum[resolved_path] = writes
        if _deleted_since_vacuum.get(resolved_path, 0) >= _VACUUM_DELETE_THRESHOLD:
            _deleted_since_vacuum[resolved_path] = 0
            _writes_since_vacuum[resolved_path] = 0
            should_vacuum = True
        elif writes >= _VACUUM_WRITE_INTERVAL:
            _writes_since_vacuum[resolved_path] = 0
            should_vacuum = True

    if not should_vacuum:
        return
    try:
        freelist = conn.execute("PRAGMA freelist_count").fetchone()
        if freelist and int(freelist[0]) > 0:
            conn.execute(f"PRAGMA incremental_vacuum({_VACUUM_PAGES})")
    except sqlite3.Error:
        # Best-effort space reclamation; never fail a heartbeat write.
        pass


def _node_point_values(node_metrics: Dict[str, Any]) -> Dict[str, float]:
    values: Dict[str, float] = {}
    cpu = _coerce_number(node_metrics.get("cpuPercent"))
    if cpu is None:
        # Darwin agents report loadPercent instead of cpuPercent; the UI uses
        # the same fallback (metrics.cpuPercent ?? metrics.loadPercent).
        cpu = _coerce_number(node_metrics.get("loadPercent"))
    if cpu is not None:
        values["cpuPercent"] = cpu
    mem = _coerce_number(node_metrics.get("memoryUsedPercent"))
    if mem is not None:
        values["memoryUsedPercent"] = mem
    for key in ("diskUsedPercent", "inodesUsedPercent"):
        value = _coerce_number(node_metrics.get(key))
        if value is not None:
            values[key] = value
    return values


def _query_service_totals(
    conn: sqlite3.Connection,
    service_names: Sequence[str],
    cutoff: int,
) -> Dict[str, Dict[str, float]]:
    totals: Dict[str, Dict[str, float]] = {}
    chunk_size = 500
    for idx in range(0, len(service_names), chunk_size):
        chunk = service_names[idx : idx + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        params: List[Any] = list(chunk)
        params.append(cutoff)
        rows = conn.execute(
            f"SELECT service_name, SUM(cpu_percent), SUM(memory_usage_bytes) "
            f"FROM service_scratch "
            f"WHERE service_name IN ({placeholders}) AND ts >= ? "
            f"GROUP BY service_name",
            params,
        ).fetchall()
        for svc_name, cpu_sum, mem_sum in rows:
            total: Dict[str, float] = {}
            if cpu_sum is not None:
                total["cpuPercent"] = float(cpu_sum)
            if mem_sum is not None:
                total["memoryUsageBytes"] = float(mem_sum)
            if total:
                totals[svc_name] = total
    return totals


def _write_samples(
    conn: sqlite3.Connection,
    node_name: str,
    node_values: Dict[str, float],
    this_node_services: Dict[str, Dict[str, float]],
    container_stats_provided: bool,
    now: Optional[int],
) -> int:
    retention = retention_seconds()
    for attempt in range(_BUSY_RETRY_ATTEMPTS):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Timestamp inside the write transaction so concurrent heartbeats
                # cannot write an earlier timestamp after a newer one.
                ts = _now(now)
                bucket = ts // SAMPLE_INTERVAL_SECONDS
                retention_floor = ts - retention
                scratch_cutoff = ts - SERVICE_SCRATCH_TTL_SECONDS

                del_cur = conn.execute("DELETE FROM metric_samples WHERE ts < ?", (retention_floor,))
                deleted_rows = del_cur.rowcount if del_cur.rowcount and del_cur.rowcount > 0 else 0

                affected_services: Set[str] = set(this_node_services)
                if node_name:
                    # A provided list is a full snapshot. A disappeared container must
                    # stop contributing immediately; omitted stats retain their TTL.
                    if container_stats_provided:
                        prev_rows = conn.execute(
                            "SELECT service_name FROM service_scratch WHERE node_name = ?",
                            (node_name,),
                        ).fetchall()
                        for (svc_name,) in prev_rows:
                            affected_services.add(svc_name)
                        conn.execute("DELETE FROM service_scratch WHERE node_name = ?", (node_name,))

                    if node_values:
                        conn.executemany(
                            _UPSERT_SAMPLE_SQL,
                            [
                                ("node", node_name, series_key, bucket, ts, round(value, 2))
                                for series_key, value in node_values.items()
                            ],
                        )

                    if this_node_services:
                        conn.executemany(
                            _UPSERT_SCRATCH_SQL,
                            [
                                (
                                    full_name,
                                    node_name,
                                    ts,
                                    round(contribution["cpuPercent"], 2) if "cpuPercent" in contribution else None,
                                    round(contribution["memoryUsageBytes"], 2)
                                    if "memoryUsageBytes" in contribution
                                    else None,
                                )
                                for full_name, contribution in this_node_services.items()
                            ],
                        )

                conn.execute("DELETE FROM service_scratch WHERE ts < ?", (scratch_cutoff,))

                if affected_services:
                    totals = _query_service_totals(conn, sorted(affected_services), scratch_cutoff)
                    service_rows: List[Tuple[str, str, str, int, int, float]] = []
                    for full_name, total in totals.items():
                        for series_key, value in total.items():
                            service_rows.append(
                                ("service", full_name, series_key, bucket, ts, round(value, 2))
                            )
                    if service_rows:
                        conn.executemany(_UPSERT_SAMPLE_SQL, service_rows)

                conn.execute("COMMIT")
                return deleted_rows
            except BaseException:
                _rollback_quietly(conn)
                raise
        except sqlite3.Error as exc:
            if _is_busy_error(exc) and attempt + 1 < _BUSY_RETRY_ATTEMPTS:
                time.sleep(0.01 * (attempt + 1))
                continue
            raise
    return 0


def record_samples(
    node_name: str,
    node_metrics: Optional[Dict[str, Any]],
    container_stats: Optional[List[Any]],
    *,
    now: Optional[int] = None,
) -> None:
    """Append one node sample and refresh service totals for this heartbeat.

    Node series are keyed by node name (clean, one writer per node). Service
    series are the cross-node SUM of each service's live container
    contributions: we stash this node's contribution in ``service_scratch``,
    sum across all non-stale nodes, and replace the current 30-second bucket.
    That avoids a multi-node service's chart sawtoothing between per-node
    partial values.
    """
    node_name = str(node_name or "").strip()
    node_values = _node_point_values(node_metrics or {}) if node_name else {}

    # Group this heartbeat's containers into per-service contributions.
    this_node_services: Dict[str, Dict[str, float]] = {}
    for raw in container_stats or []:
        if isinstance(raw, dict) and (
            raw.get("containerRole") == "infrastructure"
            or str(raw.get("service", "")).startswith("nomad:")
        ):
            continue
        if not isinstance(raw, dict):
            continue
        full_name = str(raw.get("service") or "").strip()
        if not full_name:
            continue
        entry = this_node_services.setdefault(full_name, {})
        cpu = _coerce_number(raw.get("cpuPercent"))
        mem = _coerce_number(raw.get("memoryUsageBytes"))
        if cpu is not None:
            entry["cpuPercent"] = entry.get("cpuPercent", 0.0) + cpu
        if mem is not None:
            entry["memoryUsageBytes"] = entry.get("memoryUsageBytes", 0.0) + mem

    path = history_path()
    resolved = str(path.resolve())
    writer_lock = _writer_lock_for(resolved)

    for corruption_attempt in range(2):
        with writer_lock:
            try:
                conn = _get_connection(create=True)
                assert conn is not None
                deleted_rows = _write_samples(
                    conn,
                    node_name,
                    node_values,
                    this_node_services,
                    container_stats is not None,
                    now,
                )
                _maybe_incremental_vacuum(conn, resolved, deleted_rows)
                return
            except sqlite3.Error as exc:
                if _is_corruption_error(exc) and corruption_attempt == 0:
                    _reset_corrupt_database(path, exc)
                    continue
                raise


def _load_raw() -> Dict[str, Any]:
    path = history_path()
    try:
        conn = _get_connection(create=False)
        if conn is None:
            return _empty()

        limit = max_points()
        conn.execute("BEGIN")
        try:
            sample_rows = conn.execute(
                "SELECT scope, target_id, series_key, ts, value "
                "FROM metric_samples "
                "ORDER BY scope, target_id, series_key, bucket"
            ).fetchall()
            scratch_rows = conn.execute(
                "SELECT service_name, node_name, ts, cpu_percent, memory_usage_bytes "
                "FROM service_scratch"
            ).fetchall()
            conn.execute("COMMIT")
        except BaseException:
            _rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        if _is_corruption_error(exc):
            _reset_corrupt_database(path, exc)
            return _empty()
        raise

    data = _empty()
    scope_map = {"node": data["nodes"], "service": data["services"]}
    for scope, target_id, series_key, ts, value in sample_rows:
        bucket = scope_map.get(scope)
        if bucket is None:
            continue
        target_bucket = bucket.setdefault(target_id, {})
        target_bucket.setdefault(series_key, []).append([int(ts), float(value)])

    for bucket in (data["nodes"], data["services"]):
        for series_map in bucket.values():
            for key, points in series_map.items():
                if len(points) > limit:
                    series_map[key] = points[-limit:]

    scratch = data[_SCRATCH_KEY]
    for service_name, node_name, ts, cpu, mem in scratch_rows:
        entry: Dict[str, Any] = {"ts": int(ts)}
        if cpu is not None:
            entry["cpuPercent"] = float(cpu)
        if mem is not None:
            entry["memoryUsageBytes"] = float(mem)
        scratch.setdefault(service_name, {})[node_name] = entry

    return data


def load_history_snapshot() -> Dict[str, Any]:
    """Read one complete sample snapshot for a batch of related queries."""
    return _load_raw()


def _load_history_from_snapshot(
    snapshot: Dict[str, Any],
    bucket_key: str,
    name: str,
    *,
    window: Optional[int],
    now: Optional[int],
) -> Dict[str, List[List[float]]]:
    bucket = snapshot.get(bucket_key, {}) if isinstance(snapshot, dict) else {}
    obj = bucket.get(name) if isinstance(bucket, dict) else None
    if not isinstance(obj, dict):
        return {}
    retention = retention_seconds()
    limit = max_points()
    floor = _now(now) - min(int(window), retention) if window else None
    result: Dict[str, List[List[float]]] = {}
    for series_key, series in obj.items():
        if not isinstance(series, list):
            continue
        points = _valid_points(series)
        ceiling = _now(now) if window else max((int(p[0]) for p in points), default=0)
        result[series_key] = _compact_points(
            points,
            floor if floor is not None else ceiling - retention,
            ceiling,
            limit,
        )
    return result


def load_history(
    kind: str,
    name: str,
    *,
    window: Optional[int] = None,
    now: Optional[int] = None,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[List[float]]]:
    """Return {seriesKey: [[ts, value], ...]} for one node or service.

    Reads concurrently via SQLite WAL without acquiring any writer lock.
    Filters to ts >= now - window when window is given.
    """
    bucket_key = {"node": "nodes", "service": "services"}.get(str(kind))
    if not bucket_key:
        return {}
    name = str(name or "").strip()
    if not name:
        return {}
    if snapshot is not None:
        return _load_history_from_snapshot(snapshot, bucket_key, name, window=window, now=now)

    scope = str(kind)
    path = history_path()
    retention = retention_seconds()
    limit = max_points()

    try:
        conn = _get_connection(create=False)
        if conn is None:
            return {}

        conn.execute("BEGIN")
        try:
            key_rows = conn.execute(
                "SELECT DISTINCT series_key FROM metric_samples WHERE scope = ? AND target_id = ?",
                (scope, name),
            ).fetchall()
            if not key_rows:
                conn.execute("COMMIT")
                return {}

            result: Dict[str, List[List[float]]] = {row[0]: [] for row in key_rows}
            if window:
                ceiling = _now(now)
                floor = ceiling - min(int(window), retention)
                rows = conn.execute(
                    "SELECT series_key, ts, value FROM metric_samples "
                    "WHERE scope = ? AND target_id = ? AND ts >= ? AND ts <= ? "
                    "ORDER BY series_key, bucket",
                    (scope, name, floor, ceiling),
                ).fetchall()
                conn.execute("COMMIT")
                for series_key, ts, value in rows:
                    result.setdefault(series_key, []).append([int(ts), float(value)])
                for series_key, points in result.items():
                    if len(points) > limit:
                        result[series_key] = points[-limit:]
                return result

            rows = conn.execute(
                "SELECT series_key, ts, value FROM metric_samples "
                "WHERE scope = ? AND target_id = ? "
                "ORDER BY series_key, bucket",
                (scope, name),
            ).fetchall()
            conn.execute("COMMIT")
        except BaseException:
            _rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        if _is_corruption_error(exc):
            _reset_corrupt_database(path, exc)
            return {}
        raise

    raw_by_key: Dict[str, List[List[float]]] = {key: [] for key in result}
    for series_key, ts, value in rows:
        raw_by_key.setdefault(series_key, []).append([int(ts), float(value)])

    for series_key, points in raw_by_key.items():
        if not points:
            result[series_key] = []
            continue
        ceiling = int(points[-1][0])
        floor = ceiling - retention
        filtered = [pt for pt in points if floor <= pt[0] <= ceiling]
        result[series_key] = filtered[-limit:]
    return result


def history_metadata(series: Dict[str, Any], requested_window: int, *, now: Optional[int] = None) -> Dict[str, Any]:
    timestamps = [point[0] for points in series.values() for point in _valid_points(points)]
    return {
        "requestedWindow": requested_window,
        "window": min(max(requested_window, 60), retention_seconds()),
        "retentionSeconds": retention_seconds(),
        "sampleIntervalSeconds": SAMPLE_INTERVAL_SECONDS,
        "availableFrom": min(timestamps) if timestamps else None,
        "latestSampleAt": max(timestamps) if timestamps else None,
        "updatedAt": _now(now),
    }


def sustained_breach(
    kind: str,
    name: str,
    series_key: str,
    *,
    threshold: float,
    duration_seconds: int,
    min_fraction: float = 0.8,
    now: Optional[int] = None,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Return the peak value if a series has stayed above ``threshold`` for the
    window, else None. Used to turn instantaneous spikes into "sustained"
    alerts. Guards against false positives: needs enough points, enough time
    coverage, a current breach, and a majority of breaching samples — so a
    single transient spike or an already-resolved problem does not alert."""
    points = load_history(kind, name, window=duration_seconds, now=now, snapshot=snapshot).get(series_key) or []
    if len(points) < 3:
        return None
    first_ts, last_ts = int(points[0][0]), int(points[-1][0])
    # Require the samples to actually span most of the window, so a freshly
    # joined node with one high reading is not flagged as "sustained".
    if last_ts - first_ts < duration_seconds * 0.6:
        return None
    # The problem must still be happening, not already recovered.
    if _now(now) - last_ts > SERVICE_SCRATCH_TTL_SECONDS:
        return None
    if float(points[-1][1]) < threshold:
        return None
    breaching = sum(1 for _, value in points if float(value) >= threshold)
    if breaching / len(points) < min_fraction:
        return None
    return round(max(float(value) for _, value in points), 1)
