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
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping

from .config import LumaConfig
from .errors import LumaError


# Application jobs allow a 30-minute cold-pull health window and a 40-minute
# progress deadline.  Control must remain attached beyond both or it can report
# a false timeout while Nomad is still converging a valid rollout.
NOMAD_ROLLOUT_TIMEOUT_SECONDS = 2700.0
NOMAD_ROLLOUT_POLL_INTERVAL_SECONDS = 1.0


class NomadRolloutError(LumaError):
    """A submitted rollout reached a terminal failure for this revision.

    This is distinct from transport and correlation failures: callers may
    safely stop retrying the same immutable deployment while still treating a
    Nomad API outage as transient.
    """


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
    rollout_timeout_seconds: float | None = None,
) -> str:
    """Register a job and wait for this exact Nomad rollout to be healthy.

    job_json is the {"Job": {...}} document produced by render_nomad_job.
    Registering an existing job ID updates it in place — same idempotency the
    deploy path relies on.  Success is deliberately correlated through the
    registration's JobModifyIndex, evaluation, deployment, JobVersion, and
    allocations.  A still-running allocation from the previous job version can
    therefore never make a new rollout look healthy.

    Nomad returns an empty EvalID for a no-op registration.  That path still
    verifies the returned JobModifyIndex and the allocations for its exact
    current JobVersion before returning success.
    """
    client = NomadApi(nomad_addr(config, state), token=_token(state))
    try:
        parsed = json.loads(job_json)
    except json.JSONDecodeError as exc:
        raise LumaError(f"invalid Nomad job JSON for {slug}: {exc}") from exc
    job = parsed.get("Job") if isinstance(parsed, dict) else None
    if not isinstance(job, dict):
        raise LumaError(f"rendered Nomad job for {slug} missing top-level Job object")
    job_id = str(job.get("ID") or slug).strip()
    if not job_id:
        raise LumaError(f"rendered Nomad job for {slug} missing Job.ID")

    timeout_seconds = _rollout_timeout_seconds(
        config,
        rollout_timeout_seconds,
    )
    deadline = time.monotonic() + timeout_seconds
    resp = client.request("POST", "/v1/jobs", {"Job": job})
    if not isinstance(resp, dict):
        raise LumaError(
            f"Nomad registration response for {slug} is invalid; rollout cannot be verified"
        )
    eval_id = resp.get("EvalID") if isinstance(resp, dict) else None
    eval_id = str(eval_id or "").strip()
    target_modify_index = _positive_index(resp.get("JobModifyIndex"))
    if target_modify_index is None:
        raise LumaError(
            f"Nomad registration response for {slug} missing JobModifyIndex; "
            "rollout cannot be correlated safely"
        )

    deployment_id = ""
    evaluation: Dict[str, Any] | None = None
    if eval_id:
        evaluation = _wait_for_evaluation(
            client,
            job_id=job_id,
            slug=slug,
            eval_id=eval_id,
            target_modify_index=target_modify_index,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
        )
        deployment_id = str(evaluation.get("DeploymentID") or "").strip()

    target_job = _read_target_job(
        client,
        job_id=job_id,
        slug=slug,
        target_modify_index=target_modify_index,
    )
    target_version = _nonnegative_int(target_job.get("Version"))
    if target_version is None:
        raise LumaError(
            f"Nomad job {slug} is missing Version for JobModifyIndex {target_modify_index}"
        )
    expected_groups = _desired_group_counts_from_job(target_job, slug=slug)

    deployment: Dict[str, Any] | None = None
    if deployment_id:
        deployment = _wait_for_deployment(
            client,
            job_id=job_id,
            slug=slug,
            deployment_id=deployment_id,
            target_modify_index=target_modify_index,
            target_version=target_version,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
        )
    else:
        # Empty EvalID is Nomad's normal no-op response.  A matching deployment
        # may nevertheless still be converging from an earlier identical
        # registration, so monitor it rather than trusting old allocations.
        matching = _target_deployment(
            client,
            job_id=job_id,
            target_modify_index=target_modify_index,
            target_version=target_version,
        )
        if matching is not None:
            deployment_id = str(matching.get("ID") or "").strip()
            matching_status = str(matching.get("Status") or "").strip().lower()
            if deployment_id and matching_status not in {
                "successful",
                "failed",
                "cancelled",
                "canceled",
            }:
                deployment = _wait_for_deployment(
                    client,
                    job_id=job_id,
                    slug=slug,
                    deployment_id=deployment_id,
                    target_modify_index=target_modify_index,
                    target_version=target_version,
                    deadline=deadline,
                    timeout_seconds=timeout_seconds,
                    initial=matching,
                )
            elif matching_status != "successful":
                # A no-op registration did not create this historical terminal
                # deployment. Its exact current-version allocations are the
                # source of truth; a later reschedule may already have healed
                # them without creating a new deployment record.
                deployment_id = ""
            else:
                # Successful history proves nothing beyond what the allocation
                # barrier below verifies and is not a deployment created by
                # this no-op submission.
                deployment_id = ""

    if deployment is not None:
        deployment_groups = _desired_group_counts_from_deployment(deployment)
        if deployment_groups and deployment_groups != expected_groups:
            raise LumaError(
                f"Nomad rollout correlation failed for {slug}: deployment {deployment_id} "
                f"desired groups {deployment_groups} do not match job v{target_version} "
                f"groups {expected_groups}"
            )

    healthy_allocations = _wait_for_healthy_allocations(
        client,
        job_id=job_id,
        slug=slug,
        target_modify_index=target_modify_index,
        target_version=target_version,
        expected_groups=expected_groups,
        deadline=deadline,
        timeout_seconds=timeout_seconds,
    )

    if not eval_id:
        suffix = f", deployment {deployment_id}" if deployment_id else ""
        return (
            f"Nomad job {slug} already healthy "
            f"(no-op, v{target_version}, {healthy_allocations} allocations{suffix})"
        )
    suffix = f", deployment {deployment_id}" if deployment_id else ""
    return (
        f"Nomad job {slug} rollout healthy "
        f"(v{target_version}, {healthy_allocations} allocations, eval {eval_id}{suffix})"
    )


def _rollout_timeout_seconds(
    config: LumaConfig,
    override: float | None,
) -> float:
    raw: Any = override
    if raw is None:
        raw = config.defaults.get(
            "nomadRolloutTimeoutSeconds",
            NOMAD_ROLLOUT_TIMEOUT_SECONDS,
        )
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        raise LumaError("defaults.nomadRolloutTimeoutSeconds must be a positive number") from None
    if not 0 < timeout <= 3600:
        raise LumaError(
            "defaults.nomadRolloutTimeoutSeconds must be greater than 0 and at most 3600 seconds"
        )
    return timeout


def _positive_index(value: Any) -> int | None:
    parsed = _nonnegative_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _deadline_expired(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _poll(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    time.sleep(min(NOMAD_ROLLOUT_POLL_INTERVAL_SECONDS, remaining))


def _rollout_timeout(
    slug: str,
    timeout_seconds: float,
    waiting_for: str,
    last_status: str,
) -> NomadRolloutError:
    detail = f"; last status: {last_status}" if last_status else ""
    return NomadRolloutError(
        f"Nomad rollout timed out for {slug} after {timeout_seconds:g}s "
        f"while waiting for {waiting_for}{detail}"
    )


def _wait_for_evaluation(
    client: "NomadApi",
    *,
    job_id: str,
    slug: str,
    eval_id: str,
    target_modify_index: int,
    deadline: float,
    timeout_seconds: float,
) -> Dict[str, Any]:
    current_eval_id = eval_id
    last_status = ""
    visited: set[str] = set()
    while True:
        if _deadline_expired(deadline):
            raise _rollout_timeout(
                slug,
                timeout_seconds,
                f"evaluation {current_eval_id}",
                last_status,
            )
        if current_eval_id in visited:
            raise LumaError(
                f"Nomad rollout correlation failed for {slug}: evaluation chain loops at "
                f"{current_eval_id}"
            )
        visited.add(current_eval_id)
        evaluation = client.request(
            "GET",
            f"/v1/evaluation/{_q(current_eval_id)}",
        )
        if not isinstance(evaluation, dict):
            raise LumaError(
                f"Nomad returned no evaluation {current_eval_id} for {slug}"
            )
        _require_rollout_identity(
            evaluation,
            kind="evaluation",
            object_id=current_eval_id,
            job_id=job_id,
            slug=slug,
            target_modify_index=target_modify_index,
            require_modify_index=current_eval_id == eval_id,
        )
        status = str(evaluation.get("Status") or "").strip().lower()
        last_status = status or "unknown"
        if status in {"failed", "cancelled", "canceled"}:
            raise _rollout_terminal_error(
                slug,
                kind="evaluation",
                object_id=current_eval_id,
                status=status,
                description=_evaluation_description(evaluation),
            )
        blocked_eval = str(evaluation.get("BlockedEval") or "").strip()
        if status == "blocked" or blocked_eval:
            blocked_id = blocked_eval or current_eval_id
            raise _rollout_terminal_error(
                slug,
                kind="evaluation",
                object_id=blocked_id,
                status="blocked",
                description=_evaluation_description(evaluation),
            )
        if status == "complete":
            next_eval = str(evaluation.get("NextEval") or "").strip()
            if evaluation.get("DeploymentID") or not next_eval:
                if evaluation.get("FailedTGAllocs") and not evaluation.get("DeploymentID"):
                    raise _rollout_terminal_error(
                        slug,
                        kind="evaluation",
                        object_id=current_eval_id,
                        status="blocked",
                        description=_evaluation_description(evaluation),
                    )
                return evaluation
            current_eval_id = next_eval
            # The next evaluation may not have committed by the time its ID is
            # visible on the current one.
            _poll(deadline)
            continue
        # Pending evaluations retain the same ID.  It must be removed from the
        # cycle guard before the next poll.
        visited.remove(current_eval_id)
        _poll(deadline)


def _require_rollout_identity(
    record: Mapping[str, Any],
    *,
    kind: str,
    object_id: str,
    job_id: str,
    slug: str,
    target_modify_index: int,
    require_modify_index: bool = True,
) -> None:
    record_job_id = str(record.get("JobID") or "").strip()
    if not record_job_id:
        raise LumaError(
            f"Nomad rollout correlation failed for {slug}: {kind} {object_id} "
            "is missing JobID"
        )
    if record_job_id != job_id:
        raise LumaError(
            f"Nomad rollout correlation failed for {slug}: {kind} {object_id} "
            f"belongs to job {record_job_id}, not {job_id}"
        )
    record_index = _positive_index(record.get("JobModifyIndex"))
    if record_index is None:
        if require_modify_index:
            raise LumaError(
                f"Nomad rollout correlation failed for {slug}: {kind} {object_id} "
                "is missing JobModifyIndex"
            )
        return
    if record_index != target_modify_index:
        raise LumaError(
            f"Nomad rollout correlation failed for {slug}: {kind} {object_id} "
            f"has JobModifyIndex {record_index}, expected {target_modify_index}"
        )


def _evaluation_description(evaluation: Mapping[str, Any]) -> str:
    parts: List[str] = []
    description = str(evaluation.get("StatusDescription") or "").strip()
    if description:
        parts.append(description)
    failures = evaluation.get("FailedTGAllocs")
    if isinstance(failures, dict) and failures:
        groups = ", ".join(sorted(str(group) for group in failures))
        parts.append(f"placement failures in task groups: {groups}")
    return "; ".join(parts)


def _rollout_terminal_error(
    slug: str,
    *,
    kind: str,
    object_id: str,
    status: str,
    description: str,
) -> NomadRolloutError:
    detail = f": {description}" if description else ""
    return NomadRolloutError(
        f"Nomad rollout {status} for {slug}: {kind} {object_id}{detail}"
    )


def _read_target_job(
    client: "NomadApi",
    *,
    job_id: str,
    slug: str,
    target_modify_index: int,
) -> Dict[str, Any]:
    job = client.request("GET", f"/v1/job/{_q(job_id)}")
    if not isinstance(job, dict):
        raise LumaError(f"Nomad returned no job for {slug} after registration")
    actual_job_id = str(job.get("ID") or "").strip()
    if actual_job_id and actual_job_id != job_id:
        raise LumaError(
            f"Nomad rollout correlation failed for {slug}: expected job {job_id}, "
            f"got {actual_job_id}"
        )
    actual_index = _positive_index(job.get("JobModifyIndex"))
    if actual_index != target_modify_index:
        rendered = str(actual_index) if actual_index is not None else "missing"
        raise LumaError(
            f"Nomad rollout superseded for {slug}: current JobModifyIndex {rendered}, "
            f"submitted {target_modify_index}"
        )
    return job


def _desired_group_counts_from_job(
    job: Mapping[str, Any],
    *,
    slug: str,
) -> Dict[str, int]:
    groups = job.get("TaskGroups")
    if not isinstance(groups, list) or not groups:
        raise LumaError(f"Nomad job {slug} has no task groups to verify")
    result: Dict[str, int] = {}
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            continue
        name = str(raw_group.get("Name") or "").strip()
        count = _nonnegative_int(raw_group.get("Count", 1))
        if not name or count is None:
            raise LumaError(f"Nomad job {slug} has an invalid task group")
        result[name] = count
    if not result:
        raise LumaError(f"Nomad job {slug} has no task groups to verify")
    return result


def _desired_group_counts_from_deployment(
    deployment: Mapping[str, Any],
) -> Dict[str, int]:
    groups = deployment.get("TaskGroups")
    if not isinstance(groups, dict):
        return {}
    result: Dict[str, int] = {}
    for name, raw_group in groups.items():
        if not isinstance(raw_group, dict):
            continue
        count = _nonnegative_int(raw_group.get("DesiredTotal"))
        if count is not None:
            result[str(name)] = count
    return result


def _target_deployment(
    client: "NomadApi",
    *,
    job_id: str,
    target_modify_index: int,
    target_version: int,
) -> Dict[str, Any] | None:
    deployments = client.request(
        "GET",
        f"/v1/job/{_q(job_id)}/deployments",
    )
    if not isinstance(deployments, list):
        return None
    matches: List[Dict[str, Any]] = []
    for deployment in deployments:
        if not isinstance(deployment, dict):
            continue
        if str(deployment.get("JobID") or job_id) != job_id:
            continue
        if _positive_index(deployment.get("JobModifyIndex")) != target_modify_index:
            continue
        if _nonnegative_int(deployment.get("JobVersion")) != target_version:
            continue
        matches.append(deployment)
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: _nonnegative_int(item.get("CreateIndex")) or 0,
    )


def _wait_for_deployment(
    client: "NomadApi",
    *,
    job_id: str,
    slug: str,
    deployment_id: str,
    target_modify_index: int,
    target_version: int,
    deadline: float,
    timeout_seconds: float,
    initial: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    last_status = ""
    current: Mapping[str, Any] | None = initial
    while True:
        if _deadline_expired(deadline):
            raise _rollout_timeout(
                slug,
                timeout_seconds,
                f"deployment {deployment_id}",
                last_status,
            )
        if current is None:
            current = client.request(
                "GET",
                f"/v1/deployment/{_q(deployment_id)}",
            )
        if not isinstance(current, dict):
            raise LumaError(
                f"Nomad returned no deployment {deployment_id} for {slug}"
            )
        _require_rollout_identity(
            current,
            kind="deployment",
            object_id=deployment_id,
            job_id=job_id,
            slug=slug,
            target_modify_index=target_modify_index,
        )
        deployment_version = _nonnegative_int(current.get("JobVersion"))
        if deployment_version != target_version:
            rendered = str(deployment_version) if deployment_version is not None else "missing"
            raise LumaError(
                f"Nomad rollout correlation failed for {slug}: deployment {deployment_id} "
                f"has JobVersion {rendered}, expected {target_version}"
            )
        status = str(current.get("Status") or "").strip().lower()
        last_status = status or "unknown"
        if status == "successful":
            return dict(current)
        if status in {"failed", "cancelled", "canceled", "blocked", "paused"}:
            raise _rollout_terminal_error(
                slug,
                kind="deployment",
                object_id=deployment_id,
                status=status,
                description=str(current.get("StatusDescription") or "").strip(),
            )
        current = None
        _poll(deadline)


def _allocation_is_healthy(allocation: Mapping[str, Any]) -> bool:
    if str(allocation.get("DesiredStatus") or "run").strip().lower() not in {
        "run",
        "running",
    }:
        return False
    if str(allocation.get("ClientStatus") or "").strip().lower() != "running":
        return False
    task_states = allocation.get("TaskStates")
    tasks_are_running = isinstance(task_states, dict) and bool(task_states) and all(
        isinstance(task, dict)
        and str(task.get("State") or "").strip().lower() == "running"
        and task.get("Failed") is not True
        for task in task_states.values()
    )
    if isinstance(task_states, dict) and task_states and not tasks_are_running:
        return False
    deployment_status = allocation.get("DeploymentStatus")
    if isinstance(deployment_status, dict) and "Healthy" in deployment_status:
        return deployment_status.get("Healthy") is True
    return tasks_are_running


def _allocation_health(
    allocations: List[Any],
    *,
    target_version: int,
    expected_groups: Mapping[str, int],
) -> tuple[bool, int, str]:
    active_by_group: Dict[str, List[Mapping[str, Any]]] = {
        name: [] for name in expected_groups
    }
    for allocation in allocations:
        if not isinstance(allocation, dict):
            continue
        if _nonnegative_int(allocation.get("JobVersion")) != target_version:
            continue
        if str(allocation.get("DesiredStatus") or "run").strip().lower() not in {
            "run",
            "running",
        }:
            continue
        # Failed/complete allocations may retain DesiredStatus=run until Nomad
        # garbage-collects them, including after a successful reschedule.  They
        # are history, not an extra active replica; the live replacement still
        # has to satisfy the exact desired count below.
        if str(allocation.get("ClientStatus") or "").strip().lower() in {
            "complete",
            "dead",
            "failed",
            "lost",
        }:
            continue
        group = str(allocation.get("TaskGroup") or "").strip()
        if group in active_by_group:
            active_by_group[group].append(allocation)

    ready = True
    healthy_total = 0
    parts: List[str] = []
    for group, desired in expected_groups.items():
        active = active_by_group.get(group, [])
        healthy = [allocation for allocation in active if _allocation_is_healthy(allocation)]
        healthy_total += len(healthy)
        if len(active) != desired or len(healthy) != desired:
            ready = False
        states = sorted(
            {
                str(allocation.get("ClientStatus") or "unknown").strip().lower()
                for allocation in active
            }
        )
        parts.append(
            f"{group}: desired={desired}, active={len(active)}, healthy={len(healthy)}, "
            f"states={','.join(states) or 'none'}"
        )
    return ready, healthy_total, "; ".join(parts)


def _wait_for_healthy_allocations(
    client: "NomadApi",
    *,
    job_id: str,
    slug: str,
    target_modify_index: int,
    target_version: int,
    expected_groups: Mapping[str, int],
    deadline: float,
    timeout_seconds: float,
) -> int:
    last_status = ""
    while True:
        if _deadline_expired(deadline):
            raise _rollout_timeout(
                slug,
                timeout_seconds,
                f"healthy allocations for job v{target_version}",
                last_status,
            )
        _read_target_job(
            client,
            job_id=job_id,
            slug=slug,
            target_modify_index=target_modify_index,
        )
        allocations = client.request(
            "GET",
            f"/v1/job/{_q(job_id)}/allocations",
        )
        if not isinstance(allocations, list):
            allocations = []
        ready, healthy_total, last_status = _allocation_health(
            allocations,
            target_version=target_version,
            expected_groups=expected_groups,
        )
        if ready:
            return healthy_total
        _poll(deadline)


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
            alloc_rows = _task_allocations(job_id, group_name, task_name, allocations, region=region, desired=desired)
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
        for raw in network.get("ReservedPorts") or []:
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
    desired: int = 1,
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
        # An allocation that Nomad has rescheduled keeps DesiredStatus="run"
        # but carries a NextAllocation pointer to its live replacement. Once it
        # has reached a terminal client status it is a stale corpse, not the
        # running service — counting it would report a healthy rescheduled
        # service as "failed" until Nomad GCs the dead alloc (~1h). Skip it.
        next_alloc = str(allocation.get("NextAllocation") or "").strip()
        client_status = str(allocation.get("ClientStatus") or "").lower()
        if next_alloc and client_status in {"failed", "complete", "lost"}:
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
    running_count = sum(1 for row in rows if str(row.get("state") or "").lower() == "running")
    if running_count >= max(int(desired or 1), 1):
        terminal_states = {"dead", "failed", "lost", "complete"}
        rows = [
            row
            for row in rows
            if str(row.get("state") or "").lower() not in terminal_states and str(row.get("error") or "").lower() != "true"
        ]
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
    # Bound each request by intent rather than a single 10-minute timeout: a
    # stalled read-only status/dashboard poll must not block a request handler
    # for 10 minutes, while write/deploy operations still get a generous budget.
    READ_TIMEOUT = 30
    WRITE_TIMEOUT = 120

    def __init__(self, api_url: str, *, token: str = ""):
        self.api_url = api_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        raw = self.request_text(method, path, body, timeout=timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise LumaError(
                f"Nomad API {method} {path} returned a non-JSON response: {exc}"
            ) from exc

    def _default_timeout(self, method: str) -> float:
        return self.READ_TIMEOUT if method.upper() == "GET" else self.WRITE_TIMEOUT

    def request_text(
        self,
        method: str,
        path: str,
        body: Dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Nomad-Token"] = self.token
        req = urllib.request.Request(self.api_url + path, data=data, method=method, headers=headers)
        effective_timeout = timeout if timeout is not None else self._default_timeout(method)
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LumaError(f"Nomad API error {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LumaError(
                f"Nomad API {method} {path} timed out after {effective_timeout:g}s "
                f"at {self.api_url}. Check that the Nomad agent is responsive."
            ) from exc
        except urllib.error.URLError as exc:
            raise LumaError(
                f"Nomad API unavailable at {self.api_url}: {exc.reason}. "
                "Check that the Nomad agent is running and nomadAddr is reachable "
                "from the luma-control container."
            ) from exc
        return raw

    def put_variable(self, path: str, items: Dict[str, str]) -> Dict[str, Any]:
        """Create/update a Nomad Variable without exposing values in a job spec.

        The caller owns path/ACL policy. Values are sent only to Nomad's
        encrypted Variables store and this method never interpolates them into
        errors or log messages.
        """

        normalized = str(path or "").strip().strip("/")
        if (
            not normalized
            or ".." in normalized.split("/")
            or not isinstance(items, dict)
            or not items
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                for key, value in items.items()
            )
        ):
            raise LumaError("invalid Nomad variable request")
        try:
            result = self.request(
                "PUT",
                "/v1/var/" + urllib.parse.quote(normalized, safe="/"),
                {"Items": dict(items)},
            )
        except LumaError:
            # Nomad should not echo Items, but keep this boundary generic even
            # if a future server version changes its error payload.
            raise LumaError("Nomad variable write failed") from None
        if not isinstance(result, dict):
            raise LumaError("Nomad variable write failed")
        return result

    def get_variable(self, path: str) -> Dict[str, str]:
        """Read one variable for in-memory redaction at a trusted boundary.

        Values are returned only to the caller and are never included in this
        method's errors. LAE uses this solely to remove exact secret values
        from application logs before returning them to a tenant.
        """

        normalized = str(path or "").strip().strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise LumaError("invalid Nomad variable request")
        try:
            result = self.request(
                "GET",
                "/v1/var/" + urllib.parse.quote(normalized, safe="/"),
            )
        except LumaError:
            raise LumaError("Nomad variable read failed") from None
        items = result.get("Items") if isinstance(result, dict) else None
        if not isinstance(items, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in items.items()
        ):
            raise LumaError("Nomad variable read failed")
        return {str(key): str(value) for key, value in items.items()}

    def delete_variable(self, path: str) -> None:
        normalized = str(path or "").strip().strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise LumaError("invalid Nomad variable request")
        try:
            self.request(
                "DELETE",
                "/v1/var/" + urllib.parse.quote(normalized, safe="/"),
            )
        except LumaError as exc:
            # DELETE is used by retryable cancel/delete flows. A missing lease
            # is already the requested end state and must stay idempotent.
            if "Nomad API error 404" in str(exc):
                return
            raise LumaError("Nomad variable delete failed") from None
