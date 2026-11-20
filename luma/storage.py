from __future__ import annotations

from typing import Any, Dict

from .compose import ComposeDeploymentSpec
from .errors import LumaError


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
    if not target.local_path:
        raise LumaError(f"volume {volume} does not target a local storage path")
    nodes = {service.node for service in deployment.services.values() if service.node}
    if target.local_node:
        nodes.add(target.local_node)
    if len(nodes) != 1:
        raise LumaError("local storage migration requires one explicit destination deployment node")
    return {
        "volume": volume,
        "fromNode": from_node,
        "fromVolume": from_volume,
        "toNode": next(iter(nodes)),
        "toPath": target.local_path,
        "status": "manual-required",
        "message": (
            "Stop all writers and capture a recoverable backup. Copy the data to the deployment node, "
            "preserving numeric ownership and permissions; verify the copy before setting adopted: true "
            "and redeploying. Keep the source data for rollback. This command does not copy or stop anything."
        ),
    }
