"""Management-only operations routing and independent background evaluation."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..errors import LumaError
from .state import is_initialized, load_auth_state, load_runtime_state, require_token

LOG = logging.getLogger(__name__)


def handles(path: str) -> bool:
    return path.startswith(("/v1/alerting/", "/v1/governance/"))


def dispatch(token: str, method: str, path: str, body: dict[str, Any] | None = None,
             query: dict[str, str] | None = None) -> dict[str, Any]:
    state = load_auth_state()
    require_token(state, token, token_type="deploy")
    body, query = body or {}, query or {}
    if path.startswith("/v1/alerting/"):
        from . import alerting
        return alerting.dispatch(method, path.removeprefix("/v1/alerting/"), body=body, query=query)
    if path.startswith("/v1/governance/"):
        from . import storage_governance
        from . import server
        resource = path.removeprefix("/v1/governance/")
        if resource == "builder" and method == "POST":
            return server.handle_builder_storage_request(token, body)
        if resource.startswith("builder/") and method == "GET":
            return server.handle_builder_storage_status(token, resource.removeprefix("builder/"))
        result = storage_governance.dispatch(method, resource, body=body, query=query)
        if resource == "inventory" and method == "GET":
            state = load_runtime_state()
            nodes = state.get("nodes", {})
            result["builders"] = [{"name": name, "status": server._node_agent_status(record)}
                for name, record in nodes.items() if isinstance(record, dict)
                and "builder-storage-v1" in (record.get("agent", {}).get("capabilities") or [])]
            tasks = state.get("agentTasks", {})
            items = [task for task in tasks.values() if isinstance(task, dict) and task.get("action") == "builder-storage"]
            items.sort(key=lambda task: int(task.get("createdAt") or 0), reverse=True)
            result["builderTasks"] = [server._builder_storage_public_task(task) for task in items[:50]]
            from . import database
            conn = database.connect()
            try:
                row = conn.execute("SELECT value FROM database_meta WHERE key='last_backup_at'").fetchone()
                result["backup"] = {"latestAt": int(row[0]) if row else None,
                    "note": "Control-state backup only; Nomad and application volumes require separate backups."}
            finally:
                conn.close()
        return result
    raise LumaError("unknown operations endpoint")


class OperationsWorker:
    """One loop per serving process; SQL leases arbitrate outbound deliveries.

    No process is started merely by importing modules or constructing the app.
    Lifecycle startup owns the loop, and an idle/uninitialized store is ignored.
    """

    def __init__(self, interval: float = 15.0):
        self.interval = max(1.0, interval)
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.delivery_thread: threading.Thread | None = None
        self.last_inventory_at = 0.0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self.run, name="luma-operations", daemon=True)
        self.thread.start()
        self.delivery_thread = threading.Thread(target=self.run_deliveries, name="luma-notifications", daemon=True)
        self.delivery_thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=6)
        if self.delivery_thread:
            self.delivery_thread.join(timeout=6)

    def run_once(self) -> None:
        if not is_initialized():
            return
        from . import alerting
        state = alerting.load_evaluation_state()
        alerting.tick(state)
        if time.monotonic() - self.last_inventory_at >= 3600:
            from .storage_governance import storage_inventory
            storage_inventory()
            self.last_inventory_at = time.monotonic()

    def run_deliveries(self) -> None:
        from . import alerting
        while not self.stop.is_set():
            try:
                if is_initialized():
                    alerting.deliver_pending(limit=1)
            except Exception:
                LOG.error("Notification delivery iteration failed; pending work remains durable")
            self.stop.wait(1)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Do not include exception strings: notification failures can
                # contain webhook credentials or a provider response body.
                LOG.error("Operations evaluation failed; retrying on the next interval")
            self.stop.wait(self.interval)
