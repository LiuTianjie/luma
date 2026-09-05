from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Dict, TypeVar

from ..errors import LumaError


DEFAULT_STATE_DIR = Path("/opt/luma/control")
STATE_FILE = "control.json"
T = TypeVar("T")


def state_dir() -> Path:
    return Path(os.environ.get("LUMA_CONTROL_STATE_DIR") or DEFAULT_STATE_DIR)


def state_path() -> Path:
    return state_dir() / STATE_FILE


def _load_json_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise LumaError(f"control state not initialized: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LumaError(f"cannot read control state {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LumaError(
            f"control state {path} is corrupt (invalid JSON): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise LumaError(f"invalid control state: {path}")
    return data


def _save_json_state(data: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp_path.chmod(0o600)
        except PermissionError:
            pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _main_path(path: Path | None) -> bool:
    return path is None or Path(path).resolve() == state_path().resolve()


def is_initialized() -> bool:
    """Read-only existence check; does not create a default production path."""
    from .database import database_path
    if any((state_dir() / name).exists() for name in ("control-sqlite-authority.json", "control-sqlite-migration.json")):
        # Existing installation with a missing DB needs recovery, not new tokens.
        return True
    if state_path().is_file():
        return True
    path = database_path()
    if not path.is_file():
        return False
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        return conn.execute("SELECT 1 FROM database_meta WHERE key='state_initialized'").fetchone() is not None
    except sqlite3.Error:
        # A corrupt DB is present state: load_state must report corruption,
        # never silently replace it with freshly generated credentials.
        return True
    finally:
        if conn is not None:
            conn.close()


def load_state(path: Path | None = None) -> Dict[str, Any]:
    if not _main_path(path):
        return _load_json_state(Path(path))
    from .database import database_path, ensure_initialized, read_state, transaction
    if not is_initialized():
        raise LumaError(f"control state not initialized: {database_path()}; run luma bootstrap")
    # Import (only once) under a writer transaction before the read snapshot.
    ensure_initialized()
    with transaction(immediate=False) as conn:
        return read_state(conn)


def load_runtime_state() -> Dict[str, Any]:
    """Heartbeat/lease snapshot without builds, deployments or source history."""
    from .database import database_path, ensure_initialized, read_state, transaction
    if not is_initialized():
        raise LumaError(f"control state not initialized: {database_path()}; run luma bootstrap")
    ensure_initialized()
    with transaction(immediate=False) as conn:
        return read_state(conn, kinds={"nodes", "agentTasks", "builderTasks"})


def load_auth_state() -> Dict[str, Any]:
    """Read authentication/config keys without hydrating task/build history."""
    from .database import database_path, ensure_initialized, transaction
    if not is_initialized():
        raise LumaError(f"control state not initialized: {database_path()}; run luma bootstrap")
    ensure_initialized()
    with transaction(immediate=False) as conn:
        return {row[0]: json.loads(row[1]) for row in conn.execute("SELECT key,payload FROM control_config")}


def save_state(data: Dict[str, Any], path: Path | None = None) -> None:
    if not _main_path(path):
        _save_json_state(data, Path(path))
        return
    from .database import ensure_initialized, initialized, transaction, write_state
    with transaction() as conn:
        if not initialized(conn):
            ensure_initialized(conn, initial_state=data)
        write_state(conn, data)


def _detach(value: Any) -> Any:
    # A callback may return a collection, or a structure containing one. Never
    # let lazy database handles escape after their transaction has closed.
    if isinstance(value, dict):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detach(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_detach(item) for item in value)
    return value


def mutate_state(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    from .database import ensure_initialized, read_state, transaction, write_state
    with transaction() as conn:
        ensure_initialized(conn)
        state = read_state(conn, lazy=True)
        result = mutator(state)
        write_state(conn, state)
        return _detach(result)


def mutate_state_if_changed(mutator: Callable[[Dict[str, Any]], tuple[T, bool]]) -> T:
    """Claim/read/update atomically; idle polls perform no state-row writes."""
    from .database import ensure_initialized, read_state, transaction, write_state
    with transaction() as conn:
        ensure_initialized(conn)
        state = read_state(conn, lazy=True)
        result, changed = mutator(state)
        if changed:
            write_state(conn, state)
        return _detach(result)


def new_state(*, domain: str, cluster_id: str | None = None) -> Dict[str, Any]:
    return {
        "clusterId": cluster_id or f"luma-{secrets.token_hex(4)}",
        "domain": domain,
        "deployToken": secrets.token_urlsafe(32),
        "joinToken": secrets.token_urlsafe(32),
        "nomadAddr": "http://127.0.0.1:4646",
        "nomadRpcAddr": "",
        "createdBy": "luma",
    }


def init_state(*, domain: str, cluster_id: str | None = None, overwrite: bool = False) -> Dict[str, Any]:
    from .database import ensure_initialized, initialized, read_state, transaction, write_state
    with transaction() as conn:
        if initialized(conn) or state_path().exists():
            ensure_initialized(conn)
            if not overwrite:
                return read_state(conn)
        data = new_state(domain=domain, cluster_id=cluster_id)
        ensure_initialized(conn, initial_state=data)
        if overwrite:
            write_state(conn, data)
        return data


def require_token(state: Dict[str, Any], token: str, *, token_type: str) -> None:
    key = {
        "deploy": "deployToken",
        "join": "joinToken",
    }.get(token_type)
    if not key:
        raise LumaError(f"unknown token type: {token_type}")
    expected = str(state.get(key) or "")
    if not expected or not secrets.compare_digest(expected, token):
        raise LumaError("unauthorized")
