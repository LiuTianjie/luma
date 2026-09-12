"""Read projections for Dashboard pages; full resource handlers stay compatible."""
from typing import Any

from ..errors import LumaError

SCOPES = {"full", "overview", "applications", "application", "nodes", "setup", "deploy", "directory", "fleet", "network", "storage", "metrics"}
SUMMARY_FIELDS = {"name", "fullName", "stack", "managedBy", "region", "node", "nodes", "exposure", "domain", "image", "running", "desired", "pending", "failed", "status", "health"}


def service_summary(service: dict[str, Any], *, placement: bool = False) -> dict[str, Any]:
    item = {key: value for key, value in service.items() if key in SUMMARY_FIELDS}
    # Preserve workload counts and placement without allocation logs/container data.
    if placement:
        item["tasks"] = [{"node": task.get("node", "")} for task in service.get("tasks", []) if isinstance(task, dict)]
    return item


def app_status(services: list[dict[str, Any]]) -> str:
    def status(service):
        return str(service.get("status") or service.get("health") or "").lower()
    if any(service.get("failed", 0) > 0 or status(service) in {"failed", "dead", "lost", "error"} for service in services):
        return "failed"
    if any(service.get("pending", 0) > 0 for service in services):
        return "pending"
    def healthy(service):
        if service.get("desired", 0) > 0:
            return service.get("running", 0) >= service["desired"]
        return status(service) in {"running", "healthy", "complete"}
    return "running" if all(healthy(service) for service in services) else "degraded"


def is_application(service: dict[str, Any]) -> bool:
    stack = str(service.get("stack") or service.get("name") or "")
    return bool(stack and not service.get("managedBy") and stack not in {"traefik", "egress", "luma-control"} and not stack.startswith("luma-storage") and service.get("name") != "cloudflared")


def application_page(services: list[dict[str, Any]], query: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        offset, limit = int(query.get("offset", "0")), int(query.get("limit", "50"))
    except ValueError as exc:
        raise LumaError("offset and limit must be integers") from exc
    if offset < 0 or not 1 <= limit <= 200:
        raise LumaError("offset must be non-negative and limit must be between 1 and 200")
    groups: dict[str, list[dict[str, Any]]] = {}
    for service in services:
        if is_application(service):
            groups.setdefault(str(service.get("stack") or service.get("name")), []).append(service)
    counts = {"total": len(groups), "healthy": 0, "degraded": 0, "failed": 0}
    statuses, regions, matches = set(), set(), []
    needle = query.get("q", "").strip().lower()
    for stack, items in sorted(groups.items()):
        status = app_status(items)
        counts["healthy" if status == "running" else "failed" if status == "failed" else "degraded"] += 1
        statuses.add(status)
        app_regions = {str(item.get("region") or "-") for item in items}
        regions.update(app_regions)
        haystack = " ".join([stack] + [str(item.get(key) or "") for item in items for key in ("name", "fullName", "image", "domain", "node")] + [str(node) for item in items for node in item.get("nodes", [])]).lower()
        if needle and needle not in haystack:
            continue
        if query.get("status", "all") not in {"all", "", status}:
            continue
        if query.get("region", "all") not in {"all", ""} and query["region"] not in app_regions:
            continue
        matches.append(items)
    return [service_summary(item) for items in matches[offset:offset + limit] for item in items], {
        "offset": offset, "limit": limit, "total": len(matches), "hasMore": offset + limit < len(matches),
        "counts": counts, "statuses": sorted(statuses), "regions": sorted(regions),
    }
