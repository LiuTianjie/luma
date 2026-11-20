"""Zero-dependency Dual-Track telemetry & Ring Buffer flight-recorder SDK for Luma Eye."""
from __future__ import annotations

import atexit
import collections
import contextvars
import json
import queue
import sys
import threading
import time
import traceback
import urllib.request
from typing import Any, Deque, Dict, List, Optional


_ctx_ring: contextvars.ContextVar[Optional[Deque[Dict[str, Any]]]] = contextvars.ContextVar(
    "luma_eye_breadcrumbs", default=None
)


class LumaEyeClient:
    def __init__(
        self,
        endpoint: str,
        key: str,
        project: str,
        *,
        env: str = "production",
        release: str = "",
        breadcrumb_capacity: int = 50,
        flush_interval: float = 2.0,
        queue_size: int = 2048,
        timeout: float = 3.0,
    ) -> None:
        base = endpoint.rstrip("/")
        self.ingest_url = base if base.endswith("/_e/b") or base.endswith("/v1/b") else f"{base}/_e/b"
        self.key = key
        self.project = project
        self.env = env
        self.release = release
        self.breadcrumb_capacity = max(1, breadcrumb_capacity)
        self.flush_interval = max(0.2, flush_interval)
        self.timeout = timeout

        self._global_ring: Deque[Dict[str, Any]] = collections.deque(maxlen=self.breadcrumb_capacity)
        self._ring_lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, Dict[str, Any]]]" = queue.Queue(maxsize=max(16, queue_size))
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="luma-eye-batcher", daemon=True)
        self._worker.start()

    def _get_ring(self) -> Deque[Dict[str, Any]]:
        r = _ctx_ring.get()
        if r is None:
            return self._global_ring
        return r

    def bind_context_breadcrumbs(self) -> contextvars.Token:
        """Create an isolated per-request / per-task Ring Buffer in the current contextvar scope."""
        return _ctx_ring.set(collections.deque(maxlen=self.breadcrumb_capacity))

    def reset_context_breadcrumbs(self, token: contextvars.Token) -> None:
        _ctx_ring.reset(token)

    def add_breadcrumb(
        self,
        category: str,
        message: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        level: str = "info",
    ) -> None:
        item: Dict[str, Any] = {
            "ts": time.time(),
            "category": category[:64],
            "level": level,
            "message": message[:512],
        }
        if data:
            item["data"] = data
        ring = self._get_ring()
        if ring is self._global_ring:
            with self._ring_lock:
                ring.append(item)
        else:
            ring.append(item)

    def get_breadcrumbs_snapshot(self) -> List[Dict[str, Any]]:
        ring = self._get_ring()
        if ring is self._global_ring:
            with self._ring_lock:
                return list(ring)
        return list(ring)

    def track(
        self,
        name: str,
        *,
        actor_id: str = "",
        session_id: str = "",
        props: Optional[Dict[str, Any]] = None,
        trace_id: str = "",
    ) -> None:
        ev: Dict[str, Any] = {
            "ts": time.time(),
            "type": "custom",
            "name": name,
            "trace_id": trace_id,
            "props": props or {},
            "_actor_id": actor_id,
            "_session_id": session_id,
        }
        self._enqueue("event", ev)

    def log(
        self,
        level: str,
        message: str,
        *,
        attrs: Optional[Dict[str, Any]] = None,
        trace_id: str = "",
    ) -> None:
        self.add_breadcrumb("log", message, data=attrs, level=level)
        if level.lower() == "debug":
            # Track 1 optimization: debug stays in Ring Buffer unless Track 2 dumps.
            return
        self._enqueue(
            "log",
            {
                "ts": time.time(),
                "level": level.lower(),
                "message": message,
                "trace_id": trace_id,
                "attrs": attrs or {},
            },
        )

    def capture_exception(
        self,
        exc: Optional[BaseException] = None,
        *,
        level: str = "error",
        actor_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        tags: Optional[Dict[str, str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if exc is None:
            _, exc, tb = sys.exc_info()
            if exc is None:
                return False
        else:
            tb = exc.__traceback__

        frames: List[Dict[str, Any]] = []
        if tb is not None:
            for fs in traceback.extract_tb(tb):
                frames.append(
                    {
                        "filename": fs.filename,
                        "abs_path": fs.filename,
                        "function": fs.name,
                        "lineno": fs.lineno,
                        "colno": 1,
                        "in_app": "site-packages" not in fs.filename and "/usr/lib/" not in fs.filename,
                    }
                )

        bcs = self.get_breadcrumbs_snapshot()
        err_item = {
            "ts": time.time(),
            "level": level,
            "type": type(exc).__name__,
            "message": str(exc),
            "frames": frames,
            "breadcrumbs": bcs,
            "trace_id": trace_id,
            "tags": tags or {},
            "extra": extra or {},
        }
        payload = {
            "key": self.key,
            "project": self.project,
            "track": "urgent",
            "env": self.env,
            "release": self.release,
            "actor_id": actor_id,
            "session_id": session_id,
            "errors": [err_item],
        }
        return self._post_payload(payload)

    def _enqueue(self, kind: str, item: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((kind, item))
        except queue.Full:
            pass

    def flush(self) -> None:
        events: List[Dict[str, Any]] = []
        logs: List[Dict[str, Any]] = []
        actor_id = ""
        session_id = ""

        while True:
            try:
                kind, item = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                actor_id = item.pop("_actor_id", actor_id) or actor_id
                session_id = item.pop("_session_id", session_id) or session_id
                events.append(item)
            elif kind == "log":
                logs.append(item)

        if not events and not logs:
            return
        payload = {
            "key": self.key,
            "project": self.project,
            "track": "normal",
            "env": self.env,
            "release": self.release,
            "actor_id": actor_id,
            "session_id": session_id,
            "events": events,
            "logs": logs,
        }
        self._post_payload(payload)

    def close(self) -> None:
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self.flush()

    def _run_worker(self) -> None:
        while not self._stop_event.wait(self.flush_interval):
            self.flush()

    def _post_payload(self, payload: Dict[str, Any]) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.ingest_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False


_default_client: Optional[LumaEyeClient] = None


def init(
    endpoint: str,
    key: str,
    project: str,
    *,
    env: str = "production",
    release: str = "",
) -> LumaEyeClient:
    global _default_client
    if _default_client is not None:
        _default_client.close()
    _default_client = LumaEyeClient(endpoint, key, project, env=env, release=release)
    atexit.register(_default_client.close)
    return _default_client


def track(name: str, **kwargs: Any) -> None:
    if _default_client is not None:
        _default_client.track(name, **kwargs)


def add_breadcrumb(category: str, message: str, **kwargs: Any) -> None:
    if _default_client is not None:
        _default_client.add_breadcrumb(category, message, **kwargs)


def log(level: str, message: str, **kwargs: Any) -> None:
    if _default_client is not None:
        _default_client.log(level, message, **kwargs)


def capture_exception(exc: Optional[BaseException] = None, **kwargs: Any) -> bool:
    if _default_client is not None:
        return _default_client.capture_exception(exc, **kwargs)
    return False
