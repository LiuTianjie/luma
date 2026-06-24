from __future__ import annotations

"""Nomad HTTP API client for the Luma control plane.

Control talks to the Nomad server over its HTTP API (default :4646). Job ID ==
service slug, so create/update/remove operations are idempotent and easy to
correlate with deployment records. Uses only the standard library.

Nomad keeps every job version, so revert_job() restores any prior version in one
call, and rendered jobs carry Update.AutoRevert so a failed deploy rolls back on
its own.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from .config import LumaConfig
from .errors import LumaError


def nomad_addr(config: LumaConfig, state: Dict[str, Any]) -> str:
    """Resolve the Nomad HTTP endpoint.

    Control is colocated with the Nomad server on the manager, so the default
    targets the local agent. Overridable via state['nomadAddr'] or
    defaults.nomadAddr for non-colocated control planes.
    """
    addr = str(
        state.get("nomadAddr")
        or config.defaults.get("nomadAddr")
        or "http://127.0.0.1:4646"
    ).rstrip("/")
    return addr


def deploy_to_nomad(
    config: LumaConfig,
    job_json: str,
    state: Dict[str, Any],
    *,
    slug: str,
) -> str:
    """Register (create or update) a Nomad job from rendered job JSON.

    job_json is the {"Job": {...}} document produced by render_nomad_job.
    Registering an existing job ID updates it in place — same idempotency the
    deploy path relies on.
    """
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    try:
        parsed = json.loads(job_json)
    except json.JSONDecodeError as exc:
        raise LumaError(f"invalid Nomad job JSON for {slug}: {exc}") from exc
    job = parsed.get("Job") if isinstance(parsed, dict) else None
    if not isinstance(job, dict):
        raise LumaError(f"rendered Nomad job for {slug} missing top-level Job object")
    resp = client.request("POST", "/v1/jobs", {"Job": job})
    eval_id = resp.get("EvalID") if isinstance(resp, dict) else None
    if eval_id:
        return f"Nomad job registered for {slug} (eval {eval_id})"
    return f"Nomad job registered for {slug}"


def remove_from_nomad(config: LumaConfig, state: Dict[str, Any], *, slug: str) -> str:
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    client.request("DELETE", f"/v1/job/{_q(slug)}?purge=true")
    return f"Nomad job removed: {slug}"


def nomad_job_status(config: LumaConfig, state: Dict[str, Any], *, slug: str) -> Dict[str, Any]:
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    job = client.request("GET", f"/v1/job/{_q(slug)}")
    if not isinstance(job, dict):
        raise LumaError(f"Nomad returned no job for {slug}")
    return job


def job_versions(config: LumaConfig, state: Dict[str, Any], *, slug: str) -> List[Dict[str, Any]]:
    """Return the version history of a job (newest first), for `luma history`."""
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    resp = client.request("GET", f"/v1/job/{_q(slug)}/versions")
    versions = resp.get("Versions") if isinstance(resp, dict) else None
    if not isinstance(versions, list):
        raise LumaError(f"Nomad returned no versions for {slug}")
    out: List[Dict[str, Any]] = []
    for v in versions:
        if not isinstance(v, dict):
            continue
        out.append(
            {
                "version": v.get("Version"),
                "stable": v.get("Stable"),
                "submitTime": v.get("SubmitTime"),
                "image": _first_image(v),
            }
        )
    return out


def nomad_status_summary(config: LumaConfig, state: Dict[str, Any]) -> Dict[str, Any]:
    """Cluster + node summary from Nomad for status/dashboard consumers."""
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    try:
        leader = client.request("GET", "/v1/status/leader")
        nodes = client.request("GET", "/v1/nodes")
    except LumaError as exc:
        return {"available": False, "error": str(exc), "leader": "", "nodes": []}
    if not isinstance(nodes, list):
        return {"available": False, "error": "Nomad returned invalid node list", "leader": "", "nodes": []}
    leader_addr = str(leader or "")
    items: List[Dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        node_addr = str(n.get("Address") or "")
        region, luma_name = "", ""
        try:
            detail = client.request("GET", f"/v1/node/{_q(str(n.get('ID')))}")
            meta = detail.get("Meta") if isinstance(detail, dict) else None
            if isinstance(meta, dict):
                region = str(meta.get("region") or "")
                luma_name = str(meta.get("luma_node_name") or "")
            if not node_addr and isinstance(detail, dict):
                node_addr = str((detail.get("Attributes") or {}).get("unique.network.ip-address") or "")
        except LumaError:
            pass
        items.append({
            "name": luma_name or str(n.get("Name") or ""),
            "lumaNode": luma_name,
            "hostname": str(n.get("Name") or ""),
            "region": region,
            # `state` is kept as a display alias; `status` is the canonical key.
            "state": str(n.get("Status") or ""),
            "status": str(n.get("Status") or ""),
            "role": "client",
            "availability": str(n.get("SchedulingEligibility") or ""),
            "drain": bool(n.get("Drain")),
            "address": node_addr,
            "leader": bool(leader_addr and node_addr and node_addr in leader_addr),
        })
    items.sort(key=lambda i: i.get("name") or i.get("hostname") or "")
    return {"available": True, "leader": leader_addr, "nodes": items}


def nomad_services_summary(config: LumaConfig, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Service list from Nomad jobs.

    Returns job summaries enriched with task-level details when the Nomad
    jobspec is available. Compose deployments are one Nomad job with multiple
    tasks, so dashboard consumers need the task shape to render per-service
    exposure, logs, and storage correctly.
    """
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    try:
        jobs = client.request("GET", "/v1/jobs")
    except LumaError:
        return []
    if not isinstance(jobs, list):
        return []
    out: List[Dict[str, Any]] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if str(j.get("Type") or "") not in {"service", ""}:
            continue
        job_id = str(j.get("ID") or j.get("Name") or "")
        if not job_id:
            continue
        summary = (j.get("JobSummary") or {}).get("Summary") or {}
        running = 0
        for grp in summary.values():
            if isinstance(grp, dict):
                running += int(grp.get("Running") or 0)
        meta = j.get("Meta") if isinstance(j.get("Meta"), dict) else {}
        item: Dict[str, Any] = {
            "name": str(j.get("Name") or job_id),
            "jobId": job_id,
            "status": str(j.get("Status") or ""),
            "running": running,
            "region": str(meta.get("luma.region") or ""),
            "compose": str(meta.get("luma.compose") or "").lower() == "true",
        }
        try:
            detail = client.request("GET", f"/v1/job/{_q(job_id)}")
        except LumaError:
            detail = {}
        if isinstance(detail, dict):
            detail_meta = detail.get("Meta") if isinstance(detail.get("Meta"), dict) else {}
            if detail_meta:
                item["region"] = str(detail_meta.get("luma.region") or item.get("region") or "")
                item["compose"] = str(detail_meta.get("luma.compose") or item.get("compose") or "").lower() == "true"
        try:
            allocations = client.request("GET", f"/v1/job/{_q(job_id)}/allocations")
        except LumaError:
            allocations = []
        tasks = _job_task_summaries(
            job_id,
            detail if isinstance(detail, dict) else {},
            summary if isinstance(summary, dict) else {},
            allocations if isinstance(allocations, list) else [],
            item,
        )
        if tasks:
            item["tasks"] = tasks
        out.append(item)
    out.sort(key=lambda s: s.get("name") or "")
    return out


def _job_task_summaries(
    job_id: str,
    detail: Dict[str, Any],
    summary: Dict[str, Any],
    allocations: List[Any],
    job_item: Dict[str, Any],
) -> List[Dict[str, Any]]:
    groups = detail.get("TaskGroups") if isinstance(detail.get("TaskGroups"), list) else []
    if not groups:
        return []
    meta = detail.get("Meta") if isinstance(detail.get("Meta"), dict) else {}
    region = str(meta.get("luma.region") or job_item.get("region") or "")
    result: List[Dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("Name") or job_id)
        group_summary = summary.get(group_name) if isinstance(summary.get(group_name), dict) else {}
        desired = max(1, int(group.get("Count") or 1))
        reserved_ports = _reserved_ports_by_label(group)
        group_services = [svc for svc in group.get("Services") or [] if isinstance(svc, dict)]
        tasks = group.get("Tasks") if isinstance(group.get("Tasks"), list) else []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_name = str(task.get("Name") or "")
            if not task_name:
                continue
            config = task.get("Config") if isinstance(task.get("Config"), dict) else {}
            port_label = _task_port_label(task, group_services)
            port = reserved_ports.get(port_label, {}) if port_label else {}
            alloc_rows = _task_allocations(job_id, group_name, task_name, allocations, region=region)
            running = len([row for row in alloc_rows if str(row.get("state") or "") == "running"])
            if not alloc_rows:
                running = int(group_summary.get("Running") or job_item.get("running") or 0)
            counts = _active_allocation_counts(alloc_rows) if alloc_rows else _group_counts(group_summary)
            result.append(
                {
                    "name": task_name,
                    "stack": job_id,
                    "fullName": _task_full_name(job_id, task_name, compose=str(meta.get("luma.compose") or "").lower() == "true"),
                    "status": _task_status(str(job_item.get("status") or ""), running, desired, counts),
                    "region": region,
                    "image": str(config.get("image") or ""),
                    "portLabel": port_label,
                    "targetPort": str(port.get("to") or ""),
                    "publishPort": str(port.get("value") or ""),
                    "running": running,
                    "desired": desired,
                    "pending": counts["pending"],
                    "failed": counts["failed"],
                    "nodes": sorted({str(row.get("node") or "") for row in alloc_rows if row.get("node")}),
                    "tasks": alloc_rows,
                    "storage": _task_storage(task),
                    "resources": _task_resources(task),
                    "nomadServices": _matching_group_services(task_name, port_label, group_services),
                }
            )
    return result


def _task_full_name(job_id: str, task_name: str, *, compose: bool = False) -> str:
    if not compose and task_name == job_id:
        return job_id
    return f"{job_id}_{task_name}"


def _reserved_ports_by_label(group: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    ports: Dict[str, Dict[str, str]] = {}
    networks = group.get("Networks") if isinstance(group.get("Networks"), list) else []
    for network in networks:
        if not isinstance(network, dict):
            continue
        for raw in [*(network.get("ReservedPorts") or []), *(network.get("DynamicPorts") or [])]:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("Label") or "")
            if not label:
                continue
            ports[label] = {
                "value": str(raw.get("Value") or ""),
                "to": str(raw.get("To") or ""),
            }
    return ports


def _task_port_label(task: Dict[str, Any], group_services: List[Dict[str, Any]]) -> str:
    config = task.get("Config") if isinstance(task.get("Config"), dict) else {}
    ports = config.get("ports") if isinstance(config.get("ports"), list) else []
    if ports:
        return str(ports[0])
    task_name = str(task.get("Name") or "")
    for svc in group_services:
        if str(svc.get("Name") or "") == task_name and svc.get("PortLabel"):
            return str(svc["PortLabel"])
    return ""


def _matching_group_services(task_name: str, port_label: str, group_services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for svc in group_services:
        if str(svc.get("Name") or "") == task_name or (port_label and str(svc.get("PortLabel") or "") == port_label):
            result.append(svc)
    return result


def _group_counts(group_summary: Dict[str, Any]) -> Dict[str, int]:
    pending = 0
    for key in ("Pending", "Queued", "Starting", "Unknown"):
        pending += int(group_summary.get(key) or 0)
    return {
        "pending": pending,
        "failed": int(group_summary.get("Failed") or 0) + int(group_summary.get("Lost") or 0),
    }


def _active_allocation_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    pending_states = {"pending", "starting", "queued", "unknown"}
    failed_states = {"dead", "failed", "lost"}
    pending = 0
    failed = 0
    for row in rows:
        state = str(row.get("state") or "").lower()
        if state in pending_states:
            pending += 1
        if state in failed_states or str(row.get("error") or "").lower() == "true":
            failed += 1
    return {"pending": pending, "failed": failed}


def _task_status(job_status: str, running: int, desired: int, counts: Dict[str, int]) -> str:
    if counts.get("failed", 0) > 0:
        return "failed"
    if desired > 0 and running >= desired:
        return "running"
    if counts.get("pending", 0) > 0:
        return "pending"
    return job_status or "unknown"


def _task_allocations(
    job_id: str,
    group_name: str,
    task_name: str,
    allocations: List[Any],
    *,
    region: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for allocation in allocations:
        if not isinstance(allocation, dict):
            continue
        if str(allocation.get("JobID") or job_id) != job_id:
            continue
        if str(allocation.get("TaskGroup") or group_name) != group_name:
            continue
        desired_status = str(allocation.get("DesiredStatus") or "run").lower()
        if desired_status not in {"run", "running"}:
            continue
        task_states = allocation.get("TaskStates") if isinstance(allocation.get("TaskStates"), dict) else {}
        task_state = task_states.get(task_name) if isinstance(task_states.get(task_name), dict) else {}
        if task_states and not task_state:
            continue
        state = str(task_state.get("State") or allocation.get("ClientStatus") or "")
        rows.append(
            {
                "id": str(allocation.get("ID") or ""),
                "node": str(allocation.get("NodeName") or ""),
                "nodeId": str(allocation.get("NodeID") or ""),
                "nodeAddress": str(allocation.get("NodeAddress") or ""),
                "region": region,
                "state": state,
                "desiredState": str(allocation.get("DesiredStatus") or ""),
                "message": str(task_state.get("Message") or allocation.get("StatusDescription") or ""),
                "error": str(task_state.get("Failed") or ""),
            }
        )
    rows.sort(key=lambda row: row.get("id") or "")
    return rows


def _task_storage(task: Dict[str, Any]) -> List[Dict[str, str]]:
    config = task.get("Config") if isinstance(task.get("Config"), dict) else {}
    mounts = config.get("mount") if isinstance(config.get("mount"), list) else []
    storage: List[Dict[str, str]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        source = str(mount.get("source") or "")
        if not source:
            continue
        kind = "docker-volume" if str(mount.get("type") or "") == "volume" else "bind"
        storage.append({"name": source, "kind": kind, "target": str(mount.get("target") or "")})
    return storage


def _task_resources(task: Dict[str, Any]) -> Dict[str, Any]:
    raw = task.get("Resources") if isinstance(task.get("Resources"), dict) else {}
    reservations: Dict[str, Any] = {}
    limits: Dict[str, Any] = {}
    cpu = raw.get("CPU")
    if isinstance(cpu, (int, float)) and cpu:
        reservations["cpus"] = round(float(cpu) / 1000, 3)
    memory = raw.get("MemoryMB")
    if isinstance(memory, (int, float)) and memory:
        reservations["memoryBytes"] = int(memory) * 1024 * 1024
    memory_max = raw.get("MemoryMaxMB")
    if isinstance(memory_max, (int, float)) and memory_max:
        limits["memoryBytes"] = int(memory_max) * 1024 * 1024
    result: Dict[str, Any] = {}
    if reservations:
        result["reservations"] = reservations
    if limits:
        result["limits"] = limits
    return result


def revert_job(
    config: LumaConfig,
    state: Dict[str, Any],
    *,
    slug: str,
    version: int | None = None,
) -> str:
    """Roll a job back to a prior version (default: the previous one).

    """
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    if version is None:
        versions = job_versions(config, state, slug=slug)
        if len(versions) < 2:
            raise LumaError(f"no previous version to roll back to for {slug}")
        version = int(versions[1]["version"])
    body = {"JobID": slug, "JobVersion": int(version)}
    resp = client.request("POST", f"/v1/job/{_q(slug)}/revert", body)
    eval_id = resp.get("EvalID") if isinstance(resp, dict) else None
    if eval_id:
        return f"Nomad job {slug} reverted to v{version} (eval {eval_id})"
    return f"Nomad job {slug} reverted to v{version}"


def _first_image(version_job: Dict[str, Any]) -> str:
    groups = version_job.get("TaskGroups") or []
    for g in groups:
        for t in (g.get("Tasks") or []) if isinstance(g, dict) else []:
            cfg = t.get("Config") if isinstance(t, dict) else None
            if isinstance(cfg, dict) and cfg.get("image"):
                return str(cfg["image"])
    return ""


def _token(state: Dict[str, Any]) -> str:
    return str(state.get("nomadToken") or "")


def _q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


class NomadApi:
    def __init__(self, api_url: str, *, token: str = ""):
        self.api_url = api_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
    ) -> Any:
        raw = self.request_text(method, path, body)
        if not raw:
            return None
        return json.loads(raw)

    def request_text(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
    ) -> str:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Nomad-Token"] = self.token
        req = urllib.request.Request(self.api_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"Nomad API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LumaError(
                f"Nomad API unavailable at {self.api_url}: {exc.reason}. "
                "Check that the Nomad agent is running and nomadAddr is reachable "
                "from the luma-control container."
            ) from exc
        return raw
