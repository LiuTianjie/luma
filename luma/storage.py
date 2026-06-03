from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .compose import ComposeDeploymentSpec, StorageClassSpec
from .errors import LumaError
from .io import dump_yaml
from .service import slugify


@dataclass(frozen=True)
class StorageStack:
    name: str
    storage_class: str
    content: str


def managed_storage_stacks(deployment: ComposeDeploymentSpec) -> List[StorageStack]:
    stacks: List[StorageStack] = []
    for storage_class in deployment.storage_classes.values():
        if storage_class.mode != "managed":
            continue
        if storage_class.provider != "nfs":
            raise LumaError(f"managed storage provider not supported yet: {storage_class.provider}")
        stacks.append(_nfs_storage_stack(storage_class))
    return stacks


def storage_check_plan(deployment: ComposeDeploymentSpec) -> Dict[str, Any]:
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
                "status": "planned",
                "message": _check_message(storage_class),
            }
        )
    return {"storageClasses": checks, "warnings": deployment.warnings}


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


def _nfs_storage_stack(storage_class: StorageClassSpec) -> StorageStack:
    if not storage_class.node:
        raise LumaError(f"managed storageClasses.{storage_class.name}.node is required")
    export_root = storage_class.export_root or _export_path_from_endpoint(storage_class.endpoint or "")
    if not export_root:
        raise LumaError(f"managed storageClasses.{storage_class.name}.exportRoot is required")
    stack_name = f"luma-storage-{slugify(storage_class.name)}"
    stack = {
        "services": {
            "nfs": {
                "image": "itsthenetwork/nfs-server-alpine:12",
                "environment": {
                    "SHARED_DIRECTORY": export_root,
                },
                "volumes": [
                    f"{export_root}:{export_root}",
                    "/lib/modules:/lib/modules:ro",
                ],
                "ports": [
                    {"target": 2049, "published": 2049, "protocol": "tcp", "mode": "host"},
                ],
                "deploy": {
                    "replicas": 1,
                    "placement": {
                        "constraints": [
                            f"node.labels.luma.node.name == {storage_class.node}",
                        ]
                    },
                },
                "cap_add": ["SYS_ADMIN", "SETPCAP"],
            }
        }
    }
    return StorageStack(name=stack_name, storage_class=storage_class.name, content=dump_yaml(stack))


def _export_path_from_endpoint(endpoint: str) -> str:
    if endpoint.startswith("nfs://"):
        endpoint = endpoint[len("nfs://") :]
    _, sep, path = endpoint.partition(":")
    return path if sep else ""


def _check_message(storage_class: StorageClassSpec) -> str:
    if storage_class.provider == "nfs":
        return "Verify each eligible Docker node can mount the NFS endpoint before deploying dependent services."
    return "Provider check is not implemented yet; use an external readiness check."
