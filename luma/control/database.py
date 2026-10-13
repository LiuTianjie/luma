"""Local Manager SQLite storage. One writer transaction covers each state mutation.

The legacy JSON is a retained migration artifact, never a second writable source.
Additional Control subsystems can use ``transaction`` for their own tables.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

from ..errors import LumaError
from .write_gate import WriterGate

DATABASE_FILE = "control.sqlite3"
SCHEMA_VERSION = 1
ENTITY_KINDS = frozenset({"nodes", "agentTasks", "builderTasks", "builderTaskIdempotency", "builderSourceSnapshots", "buildRuns", "deploymentEvents"})
EVENT_STREAMS = ("events", "progress", "steps")
LOG = logging.getLogger(__name__)
WRITER_WAIT_SECONDS = 30
SLOW_TRANSACTION_SECONDS = 1
_writer_gate = WriterGate()


def _reset_writer_gate() -> None:
    global _writer_gate
    _writer_gate = WriterGate()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_writer_gate)


def database_path() -> Path:
    from .state import state_dir
    return state_dir() / DATABASE_FILE


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        # SQLite can remove WAL/SHM when the last other connection closes.
        pass


def _create_database_file(path: Path) -> None:
    if path.exists():
        return
    # Never open/close an existing database outside SQLite: POSIX close()
    # drops this process's locks, including those held by other connections.
    # Close a private new inode before publishing it, so simultaneous first
    # connections cannot see the file until the non-SQLite close is finished.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass  # Another initializer published its file first.
    finally:
        tmp.unlink(missing_ok=True)


def _log_sqlite_error(exc: sqlite3.Error) -> None:
    LOG.error("Control SQLite operation failed: %s (sqlite_errorcode=%s sqlite_errorname=%s)",
              exc, getattr(exc, "sqlite_errorcode", None), getattr(exc, "sqlite_errorname", None))


def ensure_schema(conn: sqlite3.Connection) -> None:
    # execute individually: executescript would commit a caller's transaction.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_meta'").fetchone()
    if not exists:
        conn.execute("CREATE TABLE database_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM database_meta WHERE key='schema_version'").fetchone()
    if row and int(row[0]) > SCHEMA_VERSION:
        raise LumaError("control database schema is newer than this Luma version")
    if row and int(row[0]) == SCHEMA_VERSION:
        return
    statements = (
        "CREATE TABLE IF NOT EXISTS control_config (key TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS control_collections (kind TEXT PRIMARY KEY, shape TEXT NOT NULL CHECK(shape IN ('dict','list')))",
        "CREATE TABLE IF NOT EXISTS control_entities (kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT '', app TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0, ordinal INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,id), FOREIGN KEY(kind) REFERENCES control_collections(kind) ON DELETE CASCADE)",
        "CREATE TABLE IF NOT EXISTS control_event_streams (kind TEXT NOT NULL, entity_id TEXT NOT NULL, stream TEXT NOT NULL, PRIMARY KEY(kind,entity_id,stream), FOREIGN KEY(kind,entity_id) REFERENCES control_entities(kind,id) ON DELETE CASCADE)",
        "CREATE TABLE IF NOT EXISTS control_events (kind TEXT NOT NULL, entity_id TEXT NOT NULL, stream TEXT NOT NULL, position INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,entity_id,stream,position), FOREIGN KEY(kind,entity_id) REFERENCES control_entities(kind,id) ON DELETE CASCADE)",
        "CREATE INDEX IF NOT EXISTS control_entities_created ON control_entities(kind,created_at DESC,id DESC)",
        "CREATE INDEX IF NOT EXISTS control_entities_status ON control_entities(kind,status,updated_at DESC,id DESC)",
        "CREATE INDEX IF NOT EXISTS control_history_created ON control_entities(created_at DESC,id DESC,kind DESC) WHERE kind IN ('buildRuns','deploymentEvents')",
        "CREATE INDEX IF NOT EXISTS control_entities_app ON control_entities(kind,app,created_at DESC,id DESC)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO database_meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))


def connect(path: Path | None = None, *, timeout: float = 30) -> sqlite3.Connection:
    path = Path(path or database_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive mode before SQLite opens it, including under a
    # permissive process umask. WAL/SHM inherit the main database permissions.
    _create_database_file(path)
    _private(path)
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        ensure_schema(conn)
        for suffix in ("-wal", "-shm"):
            _private(Path(str(path) + suffix))
        return conn
    except (sqlite3.Error, ValueError, LumaError) as exc:
        if isinstance(exc, sqlite3.Error):
            _log_sqlite_error(exc)
        if conn is not None:
            conn.close()
        if isinstance(exc, LumaError):
            raise
        raise LumaError(f"cannot open control database {path}: {exc}") from exc


def _migration_manifest_path() -> Path:
    return database_path().with_name("control-sqlite-migration.json")


def _authority_path() -> Path:
    return database_path().with_name("control-sqlite-authority.json")


def _verify_authority(conn: sqlite3.Connection) -> None:
    if not _authority_path().exists():
        return
    try:
        authority = json.loads(_authority_path().read_text())
        identity = conn.execute("SELECT value FROM database_meta WHERE key='database_id'").fetchone()
        if not isinstance(authority, dict) or not identity or authority.get("databaseId") != identity[0]:
            raise ValueError("database identity does not match the committed Manager state")
    except (ValueError, OSError) as exc:
        raise LumaError(f"control database authority check failed: {exc}; restore a verified backup") from exc


def _record_authority(conn: sqlite3.Connection) -> None:
    if not initialized(conn):
        return
    _verify_authority(conn)
    if not _authority_path().exists():
        identity = conn.execute("SELECT value FROM database_meta WHERE key='database_id'").fetchone()
        if identity is None:
            raise LumaError("initialized control database has no identity; restore a verified backup")
        _atomic_json(_authority_path(), {"databaseId": identity[0], "schemaVersion": SCHEMA_VERSION, "createdAt": int(time.time())})


@contextmanager
def transaction(*, immediate: bool = True, owner: str = "control.transaction") -> Iterator[sqlite3.Connection]:
    started = time.monotonic()
    admission = _writer_gate.acquire(owner, timeout=WRITER_WAIT_SECONDS) if immediate else nullcontext(None)
    with admission as deadline:
        timeout = max(0, deadline - time.monotonic()) if deadline is not None else WRITER_WAIT_SECONDS
        conn = connect(timeout=timeout) if immediate else connect()
        with _transaction_body(conn, immediate=immediate, owner=owner, started=started, deadline=deadline):
            yield conn


@contextmanager
def _transaction_body(conn, *, immediate, owner, started, deadline):
    had_manifest = _migration_manifest_path().exists()
    committed = False
    acquired = None
    try:
        if deadline is not None:
            conn.execute(f"PRAGMA busy_timeout={max(0, int((deadline - time.monotonic()) * 1000))}")
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        acquired = time.monotonic()
        if initialized(conn):
            _verify_authority(conn)
        elif _authority_path().exists() or _migration_manifest_path().exists():
            raise LumaError("previous SQLite cutover detected but initialized database is missing; refusing stale control.json import; restore a verified backup")
        yield conn
        conn.commit()
        committed = True
        # This external marker survives a lost/truncated SQLite file. It is
        # deliberately created only after commit, never after a rolled-back
        # import. A prepared manifest also blocks automatic stale JSON reuse
        # after a crash between commit and this marker write.
        _record_authority(conn)
    except BaseException as exc:
        if isinstance(exc, sqlite3.Error):
            _log_sqlite_error(exc)
            LOG.error("Control transaction failed: owner=%s phase=%s elapsed=%.3fs",
                      owner, "write" if acquired is not None else "acquire", time.monotonic() - started)
        if not committed:
            conn.rollback()
            if not had_manifest:
                _migration_manifest_path().unlink(missing_ok=True)
        raise
    finally:
        conn.close()
        finished = time.monotonic()
        wait = (acquired if acquired is not None else finished) - started
        held = finished - acquired if acquired is not None else 0
        if immediate and max(wait, held) >= SLOW_TRANSACTION_SECONDS:
            LOG.warning("Slow Control write transaction: owner=%s wait=%.3fs held=%.3fs committed=%s",
                        owner, wait, held, committed)


def initialized(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM database_meta WHERE key='state_initialized'").fetchone() is not None


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    # Include tables owned by alert/governance modules in backup verification.
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        if name != "database_meta":
            counts[name] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return counts


def ensure_initialized(conn: sqlite3.Connection | None = None, *, initial_state: dict[str, Any] | None = None) -> None:
    """Open existing state, import actual legacy state, or seed a fresh database.

    Only installation/initialization callers supply initial_state. Ordinary
    reads must never silently create new credentials for a missing Manager.
    """
    if conn is None:
        current = connect()
        try:
            if initialized(current):
                _verify_authority(current)
                _record_authority(current)
                return
        finally:
            current.close()
        with transaction() as current:
            ensure_initialized(current, initial_state=initial_state)
        return
    if initialized(conn):
        _verify_authority(conn)
        return
    if _authority_path().exists() or _migration_manifest_path().exists():
        raise LumaError("previous SQLite cutover detected but initialized database is missing; refusing stale control.json import; restore a verified backup")
    from .state import state_path
    source = state_path()
    if not source.exists():
        if initial_state is None:
            raise LumaError(f"control state not initialized: {database_path()}; run luma bootstrap")
        write_state(conn, initial_state)
        conn.execute("INSERT INTO database_meta(key,value) VALUES('database_id',?)", (secrets.token_hex(16),))
        conn.execute("INSERT INTO database_meta(key,value) VALUES('state_initialized','1')")
        return
    try:
        raw = source.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise LumaError(f"control state {source} is corrupt or unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise LumaError(f"invalid control state: {source}")
    backup = source.with_name("control.json.pre-sqlite.bak")
    digest = hashlib.sha256(raw).hexdigest()
    if not backup.exists():
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
        raise LumaError("legacy state backup checksum mismatch; refusing to import")
    write_state(conn, data)
    if read_state(conn) != data:
        raise LumaError("control database migration verification failed")
    manifest = {"schemaVersion": SCHEMA_VERSION, "source": source.name, "backup": backup.name,
        "sha256": digest, "counts": _counts(conn), "importedAt": int(time.time())}
    _atomic_json(source.with_name("control-sqlite-migration.json"), manifest)
    conn.execute("INSERT INTO database_meta(key,value) VALUES('legacy_import',?)", (_json(manifest),))
    conn.execute("INSERT INTO database_meta(key,value) VALUES('database_id',?)", (secrets.token_hex(16),))
    conn.execute("INSERT INTO database_meta(key,value) VALUES('state_initialized','1')")


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _entity_fields(value: Any) -> tuple[str, str, int, int]:
    obj = value if isinstance(value, dict) else {}
    request = obj.get("request") if isinstance(obj.get("request"), dict) else {}
    result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
    app = obj.get("applicationRef") or obj.get("slug") or result.get("service") or result.get("deployment") or request.get("name") or request.get("application") or obj.get("name") or ""
    if not isinstance(app, (str, int)):
        app = ""
    created = _integer(obj.get("createdAt"))
    return str(obj.get("status") or ""), str(app), created, _integer(obj.get("updatedAt")) or created


def _write_entity(conn: sqlite3.Connection, kind: str, identifier: str, value: Any, ordinal: int = 0) -> None:
    payload = dict(value) if isinstance(value, dict) else value
    streams = {}
    if isinstance(payload, dict):
        for stream in EVENT_STREAMS:
            if isinstance(payload.get(stream), list):
                streams[stream] = payload.pop(stream)
    status, app, created, updated = _entity_fields(value)
    conn.execute("""INSERT INTO control_entities(kind,id,payload,status,app,created_at,updated_at,ordinal)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET
        payload=excluded.payload,status=excluded.status,app=excluded.app,
        updated_at=excluded.updated_at,ordinal=excluded.ordinal
        WHERE payload<>excluded.payload OR status<>excluded.status OR app<>excluded.app
        OR updated_at<>excluded.updated_at OR ordinal<>excluded.ordinal""",
        (kind, identifier, _json(payload), status, app, created, updated, ordinal))
    previous_streams = {row[0] for row in conn.execute(
        "SELECT stream FROM control_event_streams WHERE kind=? AND entity_id=?", (kind, identifier))}
    for stream in set(streams) - previous_streams:
        conn.execute("INSERT INTO control_event_streams VALUES(?,?,?)", (kind, identifier, stream))
    for stream in previous_streams - set(streams):
        conn.execute("DELETE FROM control_event_streams WHERE kind=? AND entity_id=? AND stream=?", (kind, identifier, stream))
    existing = {(row[0], row[1]): row[2] for row in conn.execute(
        "SELECT stream,position,payload FROM control_events WHERE kind=? AND entity_id=?", (kind, identifier))}
    keep = set()
    for stream, entries in streams.items():
        for position, entry in enumerate(entries):
            key = (stream, position)
            keep.add(key)
            encoded = _json(entry)
            if existing.get(key) != encoded:
                conn.execute("INSERT INTO control_events VALUES(?,?,?,?,?) ON CONFLICT(kind,entity_id,stream,position) DO UPDATE SET payload=excluded.payload", (kind, identifier, stream, position, encoded))
    for stream, position in existing.keys() - keep:
        conn.execute("DELETE FROM control_events WHERE kind=? AND entity_id=? AND stream=? AND position=?", (kind, identifier, stream, position))


def read_entity_metadata(conn: sqlite3.Connection, kind: str, identifier: str) -> Any:
    """Read a task summary without materializing any of its log streams."""
    row = conn.execute("SELECT payload FROM control_entities WHERE kind=? AND id=?", (kind, identifier)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return json.loads(row[0])


def update_entity_metadata(conn: sqlite3.Connection, kind: str, identifier: str, value: dict[str, Any]) -> None:
    """Update an existing summary while preserving event rows and list order.

    The caller owns the transaction. Stream mutation must use its own storage
    operation, never a read/modify/write of the complete event history.
    """
    if any(key in value for key in EVENT_STREAMS):
        raise ValueError("entity metadata must not contain event streams")
    status, app, _, updated = _entity_fields(value)
    result = conn.execute(
        "UPDATE control_entities SET payload=?,status=?,app=?,updated_at=? WHERE kind=? AND id=?",
        (_json(value), status, app, updated, kind, identifier),
    )
    if result.rowcount != 1:
        raise KeyError(identifier)


def append_entity_events(
    conn: sqlite3.Connection,
    kind: str,
    identifier: str,
    stream: str,
    entries: list[dict[str, Any]],
) -> int:
    """Append only the new batch, with positions assigned under a writer lock.

    Use inside transaction() so concurrent producers cannot reserve the same
    position and summary/terminal changes commit atomically with the batch.
    The primary key finds the tail without reading historical event payloads.
    """
    if stream not in EVENT_STREAMS:
        raise ValueError(f"unsupported event stream: {stream}")
    if not entries:
        return 0
    conn.execute("INSERT OR IGNORE INTO control_event_streams VALUES(?,?,?)", (kind, identifier, stream))
    tail = conn.execute(
        "SELECT position FROM control_events WHERE kind=? AND entity_id=? AND stream=? ORDER BY position DESC LIMIT 1",
        (kind, identifier, stream),
    ).fetchone()
    start = int(tail[0]) + 1 if tail is not None else 0
    conn.executemany(
        "INSERT INTO control_events(kind,entity_id,stream,position,payload) VALUES(?,?,?,?,?)",
        ((kind, identifier, stream, start + index, _json(entry)) for index, entry in enumerate(entries)),
    )
    return len(entries)


def read_entity(conn: sqlite3.Connection, kind: str, identifier: str) -> Any:
    row = conn.execute("SELECT payload FROM control_entities WHERE kind=? AND id=?", (kind, identifier)).fetchone()
    if row is None:
        raise KeyError(identifier)
    value = json.loads(row[0])
    if isinstance(value, dict):
        for row in conn.execute("SELECT stream FROM control_event_streams WHERE kind=? AND entity_id=?", (kind, identifier)):
            value[row[0]] = []
        for event in conn.execute("SELECT stream,payload FROM control_events WHERE kind=? AND entity_id=? ORDER BY stream,position", (kind, identifier)):
            value.setdefault(event[0], []).append(json.loads(event[1]))
    return value


class EntityMap(dict):
    """dict-compatible lazy collection, used only inside a live transaction.

    Keys are cheap to list; JSON/event payloads load when a callback touches a
    record. Unvisited collections and records are never rewritten on heartbeat.
    """
    def __init__(self, conn: sqlite3.Connection, kind: str):
        self.conn, self.kind = conn, kind
        self.original_keys = {row[0] for row in conn.execute("SELECT id FROM control_entities WHERE kind=?", (kind,))}
        self.loaded: set[str] = set()
        super().__init__((key, None) for key in self.original_keys)

    def __getitem__(self, key):
        if key not in self:
            raise KeyError(key)
        if key not in self.loaded:
            super().__setitem__(key, read_entity(self.conn, self.kind, key))
            self.loaded.add(key)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self.loaded.add(key)
        super().__setitem__(key, value)

    def get(self, key, default=None):
        return self[key] if key in self else default

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __iter__(self):
        return super().__iter__()

    def keys(self):
        return super().keys()

    def items(self):
        return ((key, self[key]) for key in self)

    def values(self):
        return (self[key] for key in self)

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        super().__delitem__(key)
        return value

    def popitem(self):
        if not self:
            raise KeyError("popitem(): dictionary is empty")
        key = next(reversed(self))
        return key, self.pop(key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def copy(self):
        return dict(self.items())

    def __eq__(self, other):
        return self.copy() == other

    def __repr__(self):
        return repr(self.copy())

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        import copy
        return copy.deepcopy(self.copy(), memo)

    def active_values(self, statuses):
        for _key, value in self.active_items(statuses):
            yield value

    def active_items(self, statuses):
        wanted = set(statuses)
        if not wanted:
            return
        placeholders = ",".join("?" for _ in wanted)
        ids = {row[0] for row in self.conn.execute(
            f"SELECT id FROM control_entities WHERE kind=? AND status IN ({placeholders})", (self.kind, *wanted))}
        # Include records whose status changed in this transaction, including
        # newly queued tasks not yet persisted.
        for key in (ids | self.loaded) & self.keys():
            value = self[key]
            if isinstance(value, dict) and value.get("status") in wanted:
                yield key, value

    def flush(self):
        for key in self.original_keys - self.keys():
            self.conn.execute("DELETE FROM control_entities WHERE kind=? AND id=?", (self.kind, key))
        for key in self.loaded & self.keys():
            _write_entity(self.conn, self.kind, str(key), self[key])


def _list_id(value: Any, index: int, used: set[str]) -> str:
    identifier = str(value.get("id") or "") if isinstance(value, dict) else ""
    if not identifier or identifier in used:
        identifier = f"__position_{index}"
    suffix = 0
    while identifier in used:
        suffix += 1
        identifier = f"__position_{index}_{suffix}"
    return identifier


class EntityList(list):
    """Lazy ordered deployment history; appending does not load older entries."""
    def __init__(self, conn: sqlite3.Connection, kind: str):
        self.conn, self.kind = conn, kind
        self.ids = [row[0] for row in conn.execute("SELECT id FROM control_entities WHERE kind=? ORDER BY ordinal,id", (kind,))]
        self.original_ids = set(self.ids)
        self.loaded = set()
        super().__init__(None for _ in self.ids)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self))) ]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("list index out of range")
        if index not in self.loaded:
            super().__setitem__(index, read_entity(self.conn, self.kind, self.ids[index]))
            self.loaded.add(index)
        return super().__getitem__(index)

    def __iter__(self):
        return (self[index] for index in range(len(self)))

    def _materialize(self):
        for index in range(len(self)):
            self[index]

    def _reindexed(self):
        self.ids = [None] * len(self)
        self.loaded = set(range(len(self)))

    def __setitem__(self, index, value):
        self._materialize()
        super().__setitem__(index, value)
        self._reindexed()

    def __delitem__(self, index):
        self._materialize()
        super().__delitem__(index)
        self._reindexed()

    def append(self, value):
        self.loaded.add(len(self))
        self.ids.append(None)
        super().append(value)

    def extend(self, values):
        for value in values:
            self.append(value)

    def insert(self, index, value):
        self._materialize()
        super().insert(index, value)
        self._reindexed()

    def pop(self, index=-1):
        self._materialize()
        value = super().pop(index)
        self._reindexed()
        return value

    def remove(self, value):
        self._materialize()
        super().remove(value)
        self._reindexed()

    def clear(self):
        super().clear()
        self._reindexed()

    def copy(self):
        return self[:]

    def reverse(self):
        self._materialize()
        super().reverse()
        self._reindexed()

    def sort(self, *args, **kwargs):
        self._materialize()
        super().sort(*args, **kwargs)
        self._reindexed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __contains__(self, value):
        return any(item == value for item in self)

    def __eq__(self, other):
        return self.copy() == other

    def __repr__(self):
        return repr(self.copy())

    def count(self, value):
        return sum(item == value for item in self)

    def index(self, value, start=0, stop=None):
        return self.copy().index(value, start, len(self) if stop is None else stop)

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        import copy
        return copy.deepcopy(self.copy(), memo)

    def __add__(self, values):
        return self.copy() + values

    def __mul__(self, count):
        return self.copy() * count

    __rmul__ = __mul__

    def __imul__(self, count):
        self._materialize()
        super().__imul__(count)
        self._reindexed()
        return self

    def flush(self):
        keep = set()
        for index in range(len(self)):
            identifier = self.ids[index]
            if identifier is None:
                value = self[index]
                identifier = _list_id(value, index, keep)
            keep.add(identifier)
            if index in self.loaded:
                _write_entity(self.conn, self.kind, identifier, self[index], index)
        for identifier in self.original_ids - keep:
            self.conn.execute("DELETE FROM control_entities WHERE kind=? AND id=?", (self.kind, identifier))


def read_state(conn: sqlite3.Connection, *, lazy: bool = False, kinds: set[str] | None = None) -> dict[str, Any]:
    state = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT key,payload FROM control_config")}
    for kind, shape in conn.execute("SELECT kind,shape FROM control_collections"):
        if kinds is not None and kind not in kinds:
            continue
        if shape == "dict":
            collection = EntityMap(conn, kind)
            state[kind] = collection if lazy else collection.copy()
        else:
            collection = EntityList(conn, kind)
            state[kind] = collection if lazy else collection.copy()
    return state


def write_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    config_keys, collection_keys = set(), set()
    for key, value in state.items():
        if key in ENTITY_KINDS and isinstance(value, (dict, list)):
            shape = "list" if isinstance(value, list) else "dict"
            collection_keys.add(key)
            old = conn.execute("SELECT shape FROM control_collections WHERE kind=?", (key,)).fetchone()
            if old and old[0] != shape:
                conn.execute("DELETE FROM control_collections WHERE kind=?", (key,))
            conn.execute("INSERT OR IGNORE INTO control_collections VALUES(?,?)", (key, shape))
            if isinstance(value, (EntityMap, EntityList)) and value.conn is conn and value.kind == key:
                value.flush()
                continue
            records = value.items() if shape == "dict" else enumerate(value)
            keep = set()
            for index, (identifier, entity) in enumerate(records):
                if shape == "list":
                    identifier = _list_id(entity, index, keep)
                identifier = str(identifier)
                keep.add(identifier)
                _write_entity(conn, key, identifier, entity, index if shape == "list" else 0)
            for row in conn.execute("SELECT id FROM control_entities WHERE kind=?", (key,)).fetchall():
                if row[0] not in keep:
                    conn.execute("DELETE FROM control_entities WHERE kind=? AND id=?", (key, row[0]))
        else:
            config_keys.add(key)
            conn.execute("INSERT INTO control_config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload WHERE payload<>excluded.payload", (key, _json(value)))
    for row in conn.execute("SELECT key FROM control_config").fetchall():
        if row[0] not in config_keys:
            conn.execute("DELETE FROM control_config WHERE key=?", (row[0],))
    for row in conn.execute("SELECT kind FROM control_collections").fetchall():
        if row[0] not in collection_keys:
            conn.execute("DELETE FROM control_collections WHERE kind=?", (row[0],))


def check_database(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or database_path())
    if not path.is_file():
        raise LumaError(f"control database does not exist: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        meta = dict(conn.execute("SELECT key,value FROM database_meta"))
        invalid_json = sum(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE NOT json_valid(payload)").fetchone()[0]
            for table in ("control_config", "control_entities", "control_events"))
        initialized_state = meta.get("state_initialized") == "1" and bool(meta.get("database_id"))
        return {"ok": integrity == ["ok"] and not foreign_keys and not invalid_json and initialized_state,
            "integrity": integrity, "foreignKeyViolations": len(foreign_keys),
            "invalidJsonRows": invalid_json, "initialized": initialized_state,
            "databaseId": meta.get("database_id"), "counts": _counts(conn),
            "schemaVersion": int(meta["schema_version"])}
    except sqlite3.Error as exc:
        raise LumaError(f"control database check failed: {exc}") from exc
    finally:
        conn.close()


def backup_database(destination: Path) -> dict[str, Any]:
    destination = Path(destination)
    if destination.resolve() == database_path().resolve() or destination.exists():
        raise LumaError("backup destination must be a new path distinct from the live database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with transaction() as source:
        ensure_initialized(source)
    source = connect()
    tmp = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
    target = None
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(tmp)
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        target.close()
        target = None
        report = check_database(tmp)
        if not report["ok"]:
            raise LumaError("database backup integrity check failed")
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, destination)
        _fsync_directory(destination.parent)
        manifest = {**report, "file": destination.name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "createdAt": int(time.time())}
        _atomic_json(destination.with_name(destination.name + ".manifest.json"), manifest)
        return manifest
    finally:
        if target is not None:
            target.close()
        source.close()
        tmp.unlink(missing_ok=True)
