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

    @contextmanager
    def hold(self, resources: Callable[[], set[str] | None], cancel_requested: Callable[[], bool]):
        ident = threading.get_ident()
        with self.condition:
            while True:
                if cancel_requested():
                    raise LumaError("build canceled while waiting for runtime deployment")
                requested = resources()
                wanted = requested if requested is not None else {"*"}
                others = set().union(*(set(keys) for owner, keys in self.owners.items() if owner != ident))
                if not others or ("*" not in wanted and "*" not in others and wanted.isdisjoint(others)):
                    break
                self.condition.wait(0.25)
            self.owners.setdefault(ident, Counter()).update(wanted)
        try:
            yield
        finally:
            with self.condition:
                owned = self.owners[ident]
                owned.subtract(wanted)
                owned += Counter()  # Drop zero-count keys after nested calls.
                if not owned:
                    del self.owners[ident]
                self.condition.notify_all()
