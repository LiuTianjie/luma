from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .compose import ComposeDeploymentSpec, StorageClassSpec, resolve_storage_mounts
from .errors import LumaError


@dataclass(frozen=True)
class StorageStack:
    name: str
    storage_class: str
    content: str


def managed_storage_stacks(deployment: ComposeDeploymentSpec) -> List[StorageStack]:
    stacks: List[StorageStack] = []
    for storage_class in deployment.storage_classes.values():
        stack = managed_storage_stack(storage_class)
        if stack:
            stacks.append(stack)
    return stacks


def managed_storage_stack(storage_class: StorageClassSpec) -> StorageStack | None:
    # Managed NFS is prepared on the storage host by Luma Control during
    # storage set/apply. Older versions rendered a Swarm storage stack here.
    if storage_class.mode != "managed":
        return None
    if storage_class.provider != "nfs":
        raise LumaError(f"managed storage provider not supported yet: {storage_class.provider}")
    return None


def storage_check_plan(deployment: ComposeDeploymentSpec, *, node_records: Dict[str, Any] | None = None) -> Dict[str, Any]:
    checks = []
    for storage_class in deployment.storage_classes.values():
        checks.append(
            {
                "name": storage_class.name,
                "provider": storage_class.provider,
                "mode": storage_class.mode,
                "endpoint": storage_class.endpoint or "",
                "node": storage_class.node or "",
                "nodes": storage_class.nodes,
                "regions": storage_class.regions,
                "workloads": storage_class.workloads or ["filesystem"],
                "verifiedWorkloads": storage_class.verified_workloads,
                "status": "planned",
                "message": _check_message(storage_class),
            }
        )
    return {"storageClasses": checks, "mounts": resolve_storage_mounts(deployment, node_records=node_records), "warnings": deployment.warnings}


def storage_migration_plan(
    deployment: ComposeDeploymentSpec,
    *,
    volume: str,
    from_node: str,
    from_volume: str,
) -> Dict[str, Any]:
    if volume not in deployment.volumes:
        raise LumaError(f"unknown Luma volume: {volume}")
    target = deployment.volumes[volume]
    if not target.storage_class:
        raise LumaError(f"volume {volume} does not target a storageClass")
    return {
        "volume": volume,
        "fromNode": from_node,
        "fromVolume": from_volume,
        "toStorageClass": target.storage_class,
        "toPath": target.path or volume,
        "status": "manual-required",
        "message": (
            "Migration is intentionally explicit. Run a one-off copy job or maintenance command on the source node, "
            "verify the copied data, set adopted: true on the Luma volume entry, then redeploy the compose stack."
        ),
    }


def _check_message(storage_class: StorageClassSpec) -> str:
    if storage_class.provider == "nfs":
        return "Verify each eligible Docker node can mount the NFS endpoint before deploying dependent services."
    return "Provider check is not implemented yet; use an external readiness check."
