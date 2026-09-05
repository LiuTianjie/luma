from __future__ import annotations

"""Node ownership for persistent application mounts.

Local volumes belong to a deployment node, not to a separately scheduled
storage service. Keep mount sources unchanged when adopting older deployments:
renaming a Docker volume is a data migration, even on the same machine.
"""

from typing import Any, Iterable

from .compose import ComposeDeploymentSpec
from .errors import LumaError
from .service import ServiceSpec


def persistent_mounts(values: Iterable[Any]) -> list[dict[str, str]]:
    mounts = []
    for value in values:
        if isinstance(value, dict):
            source = str(value.get("source") or "").strip()
            target = str(value.get("target") or "").strip()
            kind = str(value.get("type") or "")
            readonly = bool(value.get("read_only") or value.get("readonly"))
        else:
            parts = str(value).split(":")
            if len(parts) < 2:
                continue
            source, target = parts[:2]
            kind = ""
            readonly = len(parts) > 2 and "ro" in parts[2].split(",")
        if not source or not target or readonly or kind == "tmpfs" or source.endswith(".sock"):
            continue
        kind = kind or ("bind" if source.startswith(("/", ".", "~")) else "volume")
        if kind not in {"bind", "volume"}:
            continue
        mounts.append({"type": kind, "source": source, "target": target})
    return mounts


def service_persistent_mounts(service: ServiceSpec) -> list[dict[str, str]]:
    return persistent_mounts(service.volumes)


def compose_persistent_mounts(deployment: ComposeDeploymentSpec) -> list[dict[str, str]]:
    mounts = []
    for name, body in (deployment.compose.get("services") or {}).items():
        if not isinstance(body, dict):
            continue
        for mount in persistent_mounts(body.get("volumes") or []):
            volume = deployment.volumes.get(mount["source"]) if mount["type"] == "volume" else None
            # An existing shared-storage deployment retains its old backend
            # until an explicit data migration. Local mounts in mixed stacks
            # still determine the owning node.
            if volume and volume.storage_class:
                continue
            if volume and volume.local_path:
                mount = {**mount, "type": "bind", "source": volume.local_path}
            mounts.append({"service": str(name), **mount})
    return mounts


def choose_storage_node(
    *, slug: str, requested: Iterable[str], previous: str = "", candidates: Iterable[str] = (),
) -> str:
    requested_nodes = {node for node in requested if node}
    if len(requested_nodes) > 1:
        raise LumaError(f"deployment {slug} has conflicting local storage nodes: {sorted(requested_nodes)}")
    wanted = next(iter(requested_nodes), "")
    if previous:
        if wanted and wanted != previous:
            raise LumaError(
                f"deployment {slug} stores persistent data on node {previous}; cannot move it to {wanted}. "
                "Migrate and verify the data before changing the storage owner."
            )
        return previous
    if wanted:
        return wanted
    available = sorted(set(candidates))
    if not available:
        raise LumaError(f"deployment {slug} requires a ready deployment node for its local persistent volumes")
    return available[0]


def guard_storage_sources(previous: Iterable[dict[str, str]], current: Iterable[dict[str, str]]) -> None:
    old = {mount["target"]: (mount["type"], mount["source"]) for mount in previous}
    for mount in current:
        target = mount["target"]
        if target in old and old[target] != (mount["type"], mount["source"]):
            raise LumaError(f"persistent storage source changed for {target}; migrate and verify the data before replacing its mount")


def storage_owner_from_job(job: dict[str, Any], allocations: list[Any], node_name_for_id) -> str:
    pins = set()
    for scope in [job, *(job.get("TaskGroups") or [])]:
        for constraint in scope.get("Constraints") or []:
            if constraint.get("Operand") != "=":
                continue
            target = str(constraint.get("RTarget") or "")
            if constraint.get("LTarget") == "${meta.luma_node_name}" and target:
                pins.add(target)
            elif constraint.get("LTarget") == "${node.unique.id}" and target:
                name = node_name_for_id(target)
                if not name:
                    raise LumaError("persistent job references an unknown storage node; restore its node registration")
                pins.add(name)
    active = [a for a in allocations if isinstance(a, dict) and a.get("ClientStatus") in {"running", "unknown"}]
    if not active and not pins:
        previous = [a for a in allocations if isinstance(a, dict) and a.get("NodeID")]
        if previous:
            # A failed/down allocation still owns data. Never interpret the
            # lack of a running allocation as an empty, freely movable volume.
            latest = max(int(a.get("CreateIndex") or 0) for a in previous)
            active = [a for a in previous if int(a.get("CreateIndex") or 0) == latest]
    owners = set(pins)
    for allocation in active:
        owner = node_name_for_id(str(allocation.get("NodeID") or ""))
        if not owner:
            raise LumaError("cannot identify the node holding existing persistent data; restore its node registration")
        owners.add(owner)
    if len(owners) != 1:
        raise LumaError("existing persistent storage has ambiguous node ownership; inspect the data before redeploying")
    return next(iter(owners))
