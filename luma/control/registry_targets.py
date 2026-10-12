"""Select managed registry consumers from the final rendered Nomad job.

This is the placement filter for registry transport preflight, not a replacement
for Nomad's resource, volume or allocation scheduler. Unsupported constraints
must stop the preflight instead of broadening the nodes that may be changed.
"""
from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from typing import Any

from ..errors import LumaError
from ..registry import normalize_registry_host, registry_host_from_image


_NODE_TARGETS = {
    "${node.unique.id}": "ID",
    "${node.unique.name}": "Name",
    "${node.datacenter}": "Datacenter",
    "${node.class}": "NodeClass",
    "${node.pool}": "NodePool",
}
_MAP_TARGET = re.compile(r"\$\{(meta|attr)\.([^${}]+)\}")
_OPERANDS = {"=", "!=", "is_set", "is_not_set"}


def _objects(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise LumaError(f"Registry preflight requires Nomad {field} to be an array of objects")
    return value


def _validate_target(target: Any) -> None:
    if not isinstance(target, str):
        raise LumaError("Registry preflight requires string constraint targets")
    if "${" in target and target not in _NODE_TARGETS and not _MAP_TARGET.fullmatch(target):
        raise LumaError(f"Registry preflight cannot evaluate constraint target: {target}")


def _validate_constraints(constraints: list[dict[str, Any]]) -> None:
    for constraint in constraints:
        operand = constraint.get("Operand")
        if not isinstance(operand, str) or operand not in _OPERANDS:
            raise LumaError(f"Registry preflight cannot evaluate constraint operand: {operand}")
        _validate_target(constraint.get("LTarget"))
        if operand in {"=", "!="}:
            _validate_target(constraint.get("RTarget"))


def registry_task_groups(job_json: str, managed_hosts: list[str]) -> list[dict[str, Any]]:
    """Return active groups pulling from the exact managed HTTP host allowlist.

    Each result contains name, registries, constraints, datacenters, nodePool
    and drivers. Constraints and drivers include every colocated task, even
    tasks whose image is not served by a managed registry.
    """
    if not managed_hosts:
        return []
    hosts = {normalize_registry_host(host) for host in managed_hosts}
    try:
        rendered = json.loads(job_json)
    except (TypeError, ValueError) as exc:
        raise LumaError("Registry preflight requires a valid rendered Nomad job") from exc
    if not isinstance(rendered, dict):
        raise LumaError("Registry preflight requires a rendered Nomad job object")
    job = rendered.get("Job", rendered)
    if not isinstance(job, dict):
        raise LumaError("Registry preflight requires a rendered Nomad job object")

    result = []
    for group in _objects(job.get("TaskGroups"), "TaskGroups"):
        count = group.get("Count")
        if count is None:
            count = 1
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise LumaError("Registry preflight requires a non-negative Nomad group Count")
        if count == 0:
            continue
        tasks = _objects(group.get("Tasks"), "Tasks")
        registries = set()
        for task in tasks:
            if task.get("Driver") != "docker":
                continue
            config = task.get("Config") or {}
            if not isinstance(config, dict):
                raise LumaError("Registry preflight requires a Nomad Docker Config object")
            image = config.get("image")
            if not isinstance(image, str) or not image:
                continue
            host = registry_host_from_image(image)
            if host in hosts:
                registries.add(host)
        if not registries:
            continue

        constraints = []
        for scope in [job, group, *tasks]:
            constraints.extend(_objects(scope.get("Constraints"), "Constraints"))
        _validate_constraints(constraints)
        drivers = set()
        for task in tasks:
            driver = task.get("Driver")
            if not isinstance(driver, str) or not driver:
                raise LumaError("Registry preflight requires every Nomad task Driver")
            drivers.add(driver)
        datacenters = job.get("Datacenters") or ["*"]
        if not isinstance(datacenters, list) or any(not isinstance(dc, str) or not dc for dc in datacenters):
            raise LumaError("Registry preflight requires Nomad Datacenters to be an array of strings")
        node_pool = job.get("NodePool") or "default"
        if not isinstance(node_pool, str):
            raise LumaError("Registry preflight requires a Nomad NodePool string")
        result.append({
            "name": str(group.get("Name") or ""),
            "registries": sorted(registries),
            "constraints": constraints,
            "datacenters": datacenters,
            "nodePool": node_pool,
            "drivers": sorted(drivers),
        })
    return result


def _resolve_target(target: str, node: dict[str, Any]) -> tuple[str, bool]:
    if target in _NODE_TARGETS:
        field = _NODE_TARGETS[target]
        default = "default" if field == "NodePool" else ""
        return str(node.get(field) or default), True
    match = _MAP_TARGET.fullmatch(target)
    if match:
        scope, key = match.groups()
        values = node.get("Meta" if scope == "meta" else "Attributes") or {}
        if not isinstance(values, dict):
            raise LumaError(f"Registry preflight received invalid Nomad node {scope}")
        return str(values.get(key, "")), key in values
    return target, True


def _matches_constraint(constraint: dict[str, Any], node: dict[str, Any]) -> bool:
    left, left_found = _resolve_target(constraint["LTarget"], node)
    operand = constraint["Operand"]
    if operand == "is_set":
        return left_found
    if operand == "is_not_set":
        return not left_found
    right, right_found = _resolve_target(constraint["RTarget"], node)
    if operand == "=":
        return left_found and right_found and left == right
    # Nomad compares inequality values even when an attribute is absent;
    # absent map entries resolve to the empty string, not a wildcard.
    return left != right


def registry_candidate_nodes(group: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter detailed Nomad nodes by placement and actual healthy task drivers.

    OS and Luma role are deliberately irrelevant: a Darwin or manager node with
    a healthy Docker driver can receive the same allocation as any other node.
    """
    constraints = _objects(group.get("constraints"), "constraints")
    _validate_constraints(constraints)
    result = []
    for node in nodes:
        if not isinstance(node, dict) or not node.get("ID"):
            continue
        if node.get("Status") != "ready" or node.get("SchedulingEligibility") != "eligible" or node.get("Drain"):
            continue
        if (node.get("NodePool") or "default") != group["nodePool"]:
            continue
        if not any(fnmatchcase(str(node.get("Datacenter") or ""), dc) for dc in group["datacenters"]):
            continue
        drivers = node.get("Drivers") or {}
        if not isinstance(drivers, dict):
            continue
        if not all(
            isinstance(drivers.get(driver), dict)
            and drivers[driver].get("Detected") is True
            and drivers[driver].get("Healthy") is True
            for driver in group["drivers"]
        ):
            continue
        if all(_matches_constraint(constraint, node) for constraint in constraints):
            result.append(node)
    return result
