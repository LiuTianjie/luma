"""Serialize conflicting deployments without parking unrelated rollouts."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import threading
from typing import Any, Callable

import yaml

from ..errors import LumaError
from ..service import slugify


def request_resources(handler: str, body: dict[str, Any]) -> set[str] | None:
    """None means a cluster-wide operation, otherwise lock only named resources."""
    if handler == "handle_application_restart":
        stack = str(body.get("stack") or "").strip()
        return {"job:" + stack} if stack else None
    if handler in {"handle_service_remove", "handle_service_rollback"}:
        name = str(body.get("name") or "").strip()
        if not name:
            return None
        try:
            return {"job:" + slugify(name)}
        except LumaError:
            return None
    if handler == "handle_certificate_retry":
        domain = str(body.get("domain") or "").strip().lower().rstrip(".")
        return {"host:" + domain} if domain else None
    if handler not in {"handle_deployment", "handle_compose_deployment"}:
        return None
    try:
        manifest = yaml.safe_load(body.get("manifest") or "")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
            return None  # The handler still reports the normal validation error.
        resources = {"job:" + slugify(manifest["name"])}
        services = (manifest.get("services") or {}).values() if handler == "handle_compose_deployment" else [manifest]
        for service in services:
            if not isinstance(service, dict):
                return None
            hosts = [service.get("domain"), *(service.get("redirectFrom") or [])]
            for host in hosts:
                if host:
                    if not isinstance(host, str) or "${" in host:
                        return None
                    resources.add("host:" + host.strip().lower().rstrip("."))
            if service.get("exposure") == "tcp-relay":
                resources.add("tcp-relay:" + str(int(service.get("publishPort") or service.get("port") or 0)))
        return resources
    except (yaml.YAMLError, LumaError, TypeError, ValueError, AttributeError):
        return None


class DeploymentLocks:
    def __init__(self, lock: Any):
        self.condition = threading.Condition(lock)
        self.owners: dict[int, Counter[str]] = {}
        self._wildcard_waiters: list[int] = []
        self._next_ticket = 0
        self._version = 0

    @contextmanager
    def hold(self, resources: Callable[[], set[str] | None], cancel_requested: Callable[[], bool]):
        ident = threading.get_ident()
        ticket: int | None = None
        try:
            while True:
                # Invoke cancel/resource callbacks outside self.condition so DB
                # reads never stall unrelated lock acquisitions or releases.
                if cancel_requested():
                    raise LumaError("build canceled while waiting for runtime deployment")
                with self.condition:
                    version_before = self._version
                requested = resources()
                wanted = set(requested) if requested is not None else {"*"}
                needs_wildcard = "*" in wanted
                with self.condition:
                    if needs_wildcard and ticket is None:
                        self._next_ticket += 1
                        ticket = self._next_ticket
                        self._wildcard_waiters.append(ticket)
                    elif not needs_wildcard and ticket is not None:
                        if ticket in self._wildcard_waiters:
                            self._wildcard_waiters.remove(ticket)
                            self.condition.notify_all()
                        ticket = None

                    already_owner = ident in self.owners
                    others = set().union(*(set(keys) for owner, keys in self.owners.items() if owner != ident))
                    # FIFO fairness for cluster-wide ("*") operations prevents
                    # starvation behind overlapping per-app deploys; existing
                    # owners keep re-entrancy so nested locks never deadlock.
                    wildcard_ahead = (
                        bool(self._wildcard_waiters)
                        and not already_owner
                        and (not needs_wildcard or self._wildcard_waiters[0] != ticket)
                    )
                    can_acquire = (
                        not wildcard_ahead
                        and (not others or (not needs_wildcard and "*" not in others and wanted.isdisjoint(others)))
                    )
                    if can_acquire:
                        if self._version != version_before and not already_owner:
                            # A concurrent deployment released while we computed
                            # resources outside the lock; refresh once so our
                            # resource set reflects the latest committed state.
                            continue
                        if ticket is not None and ticket in self._wildcard_waiters:
                            self._wildcard_waiters.remove(ticket)
                            ticket = None
                        self.owners.setdefault(ident, Counter()).update(wanted)
                        self._version += 1
                        break
                    self.condition.wait(0.25)
        except BaseException:
            if ticket is not None:
                with self.condition:
                    if ticket in self._wildcard_waiters:
                        self._wildcard_waiters.remove(ticket)
                        self.condition.notify_all()
            raise
        try:
            yield
        finally:
            with self.condition:
                owned = self.owners[ident]
                owned.subtract(wanted)
                owned += Counter()  # Drop zero-count keys after nested calls.
                if not owned:
                    del self.owners[ident]
                self._version += 1
                self.condition.notify_all()
