from __future__ import annotations

import json
import os
import secrets
import fcntl
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


def load_state(path: Path | None = None) -> Dict[str, Any]:
    path = path or state_path()
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


def save_state(data: Dict[str, Any], path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
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


def mutate_state(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    lock_path = state_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        result = mutator(state)
        save_state(state)
        return result


def mutate_state_if_changed(
    mutator: Callable[[Dict[str, Any]], tuple[T, bool]],
) -> T:
    """Mutate Control state while allowing an explicit no-write result.

    Long-polling paths must still hold the state lock while deciding whether a
    task can be claimed, but an idle poll must not rewrite and fsync the entire
    state file.  The callback therefore returns ``(result, changed)`` and the
    durable write happens only when ``changed`` is true.
    """

    lock_path = state_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        result, changed = mutator(state)
        if changed:
            save_state(state)
        return result


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
    path = state_path()
    if path.exists() and not overwrite:
        return load_state(path)
    data = new_state(domain=domain, cluster_id=cluster_id)
    save_state(data, path)
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
