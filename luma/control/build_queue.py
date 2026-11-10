"""Durable management build/deploy queue, independent of HTTP request lifetime.

A queued repository import moves through explicit stages, each admitted here:

``build``   planning and image builds. Builds are pure (every attempt owns an
            immutable image tag), so they never wait for a deployment target.
            They wait only for a compatible builder. Auto-selected work names
            a ranked candidate list; the first compatible agent to lease the
            pooled task runs it (see ``server.handle_node_agent_lease``).
``deploy``  runtime deployment of a finished image. Deployments of one
            application (the manifest ``name``) run one at a time in queue
            order, behind earlier work of the same repository branch whose
            application is not known yet. Uploaded local builds enter here.

Within a repository branch a newer request supersedes older waiting work. Waiting for
a busy builder or an earlier deployment never consumes a Control execution
slot: work returns to the queue between stages and keeps its FIFO order.

Only queued work is resumed after restart. An interrupted active stage is
failed explicitly rather than replaying possibly completed runtime side effects.
Payloads live in private Control state, never public build history. In particular
per-attempt envSecrets are not mixed into other attempts or the public request.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import secrets
import threading
import time
import urllib.parse
from typing import Any

from ..errors import LumaError
from .database_errors import persist_receipt
from .state import is_initialized, load_auth_state, mutate_state, mutate_state_if_changed, require_token

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "canceled"}
ACTIVE = {"running", "canceling", "finalizing"}
# Silence can trigger cancellation, but never proves that a worker has exited.
FINALIZING_STALE_SECONDS = 600
# A deployment can wait silently on the runtime lock or a cold image pull;
# keep its silence window above Nomad's rollout timeout.
DEPLOY_STALE_SECONDS = 3600
CANCELING_STALE_SECONDS = 180
State = dict[str, Any]
WorkItem = tuple[str, dict[str, Any]]
QUEUE_METADATA_KINDS = {"buildRuns", "agentTasks"}
# Deployments already serialize on the runtime lock; admitting more would only
# park execution threads behind it.
DEPLOY_SLOTS = 1


def _runs(state: State) -> State:
    from .server import _build_runs
    return _build_runs(state)


def _active(runs: State, statuses: set[str] | None = None) -> list[State]:
    statuses = ACTIVE | {"queued"} if statuses is None else statuses
    values = getattr(runs, "active_values", None)
    return list(values(statuses)) if callable(values) else [
        run for run in runs.values() if isinstance(run, dict) and run.get("status") in statuses
    ]


def _current_runs(state: State) -> list[State]:
    runs = _runs(state)
    current = {run["id"]: run for run in _active(runs)}
    # A terminal progress event is not a worker-exit receipt.
    for build_id in state.get("buildQueue") or {}:
        run = runs.get(build_id)
        if isinstance(run, dict) and run.get("queueExecuting"):
            current[build_id] = run
    return list(current.values())


def scheduling_state() -> State:
    """Read current builder ownership without loading historical log streams.

    Admission and post-plan selection run outside a writer transaction. A
    full detached snapshot would hydrate every build when ranking builders,
    potentially aging its node heartbeats past the readiness window.
    """
    from .database import ensure_initialized, read_state, transaction
    ensure_initialized()
    with transaction(immediate=False) as conn:
        state = read_state(conn, lazy=True, kinds={"nodes", *QUEUE_METADATA_KINDS},
                           metadata_kinds=QUEUE_METADATA_KINDS)
        runs = {run["id"]: run for run in _current_runs(state)}
        tasks = state.get("agentTasks") or {}
        current_tasks = {task["id"]: task for task in _active(tasks, {"queued", "running"})}
        for run in runs.values():
            task_id = run.get("agentTaskId")
            task = tasks.get(task_id)
            if isinstance(task, dict):
                # A completed child releases its builder even while its run
                # still owns a queue slot. Keep that receipt, without logs.
                current_tasks[task_id] = task
        state["nodes"] = (state.get("nodes") or {}).copy()
        state["buildRuns"] = runs
        state["agentTasks"] = current_tasks
        return state


def _caller_upload(run: State) -> bool:
    """A local build still running on the caller holds no queue position."""
    return (
        run.get("mode") == "local"
        and run.get("request", {}).get("queue") is True
        and not run.get("queueManaged")
    )


def _holds_project(run: State) -> bool:
    return bool(run.get("queueExecuting")) or (run.get("status") in ACTIVE and not _caller_upload(run))


def _stage(state: State, run: State) -> str:
    """Return the queue stage of a run: ``deploy`` once an image exists."""
    payload = (state.get("buildQueue") or {}).get(run.get("id"))
    if isinstance(payload, dict) and (payload.get("kind") == "local" or isinstance(payload.get("built"), dict)):
        return "deploy"
    return "build"


def deploy_key_for_manifest(manifest: Any) -> str:
    """Identify the runtime deployment a manifest replaces (its Nomad job name)."""
    if not isinstance(manifest, str) or not manifest.strip():
        return ""
    import yaml
    from ..service import slugify
    try:
        data = yaml.safe_load(manifest)
        name = data.get("name") if isinstance(data, dict) else None
        return json.dumps(["deploy", slugify(name)], separators=(",", ":")) if isinstance(name, str) and name.strip() else ""
    except (yaml.YAMLError, LumaError):
        return ""


def candidates(run: State) -> list[str]:
    """Builders that may run this run's next agent task, best first."""
    raw = run.get("buildCandidates")
    names = [str(name) for name in raw if str(name or "").strip()] if isinstance(raw, list) else []
    return names or [str(run.get("buildNode") or "")]


def _digest(body: State) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replacement_key(run: State, body: State) -> str:
    """Only replace a known deployment target, never every build in a repo."""
    body = _request_body(run, body)
    scope = _scope(run, body)
    sidecar = str(body.get("composeSidecar") or "").strip()
    if sidecar:
        path = posixpath.normpath(sidecar)
        if path.startswith("/") or path == ".." or path.startswith("../"):
            raise LumaError("composeSidecar must be a repository-relative path")
        return json.dumps([*scope, "sidecar", path], separators=(",", ":"))
    manifest = body.get("manifest") or (body.get("buildResult") or {}).get("manifest")
    if isinstance(manifest, str) and manifest.strip():
        import yaml
        from ..service import slugify
        data = yaml.safe_load(manifest)
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"].strip():
            return json.dumps([*scope, "manifest", slugify(data["name"])], separators=(",", ":"))
    # Bare repository imports do not identify a service before source analysis.
    return ""


def _request_body(run: State, body: State | None = None) -> State:
    request = run.get("request")
    return {**(request if isinstance(request, dict) else {}), **(body or {})}


def _scope(run: State, body: State) -> list[str]:
    source = str(run.get("source") or body.get("repoUrl") or run.get("projectKey") or "").strip().rstrip("/")
    from .resources import normalize_import_repo_url
    source = normalize_import_repo_url(source)
    if "://" not in source and "@" in source and ":" in source:
        host, path = source.split(":", 1)
        source = "ssh://" + host + "/" + path
    parsed = urllib.parse.urlsplit(source)
    if parsed.hostname:
        # HTTPS/SSH and case variants already share Luma's image namespace.
        # Keep the host and full path so unrelated Git servers cannot collide.
        port = f":{parsed.port}" if parsed.port and parsed.port not in {22, 80, 443} else ""
        source = parsed.hostname.lower() + port + "/" + parsed.path.strip("/").lower()
    if source.endswith(".git"):
        source = source[:-4]
    ref = str(body.get("ref") or "HEAD").strip() or "HEAD"
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    return ["build-v2", source, ref]


def queue_identity(run: State, body: State | None = None) -> str:
    """Recompute legacy keys so an upgrade does not retain repo-wide fences."""
    stored = str(run.get("queueKey") or "")
    if body is None and run.get("queueIdentityVersion") == 2 and stored:
        return stored
    request = _request_body(run, body)
    key = replacement_key(run, request)
    if key:
        return key
    scope = _scope(run, request)
    return json.dumps([*scope, "project", "*"], separators=(",", ":")) if scope[1] else ""


def identities_conflict(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a, b = json.loads(left), json.loads(right)
    return a[:3] == b[:3] and (a == b or a[3] == "project" or b[3] == "project")


def _request_stop(state: State, run: State, now: int) -> bool:
    task = (state.get("agentTasks") or {}).get(run.get("agentTaskId"))
    if isinstance(task, dict):
        if task.get("status") == "queued":
            task.update(status="canceled", message="Superseded before execution",
                        completedAt=now, updatedAt=now, cancelRequestedAt=now)
            return True
        elif task.get("status") == "running" and not task.get("cancelRequestedAt"):
            task["cancelRequestedAt"] = now
            return True
    return False


def _safe_to_release(state: State, run: State) -> bool:
    """A worker unwind is not proof that a dispatched remote child has stopped."""
    task_id = run.get("agentTaskId")
    if not task_id:
        return True
    task = (state.get("agentTasks") or {}).get(task_id)
    if isinstance(task, dict) and task.get("status") in {"succeeded", "failed", "canceled"}:
        return True
    if isinstance(task, dict) and task.get("status") in {"queued", "running"}:
        return False
    # Missing/expired receipt: require a fresh authenticated heartbeat proving
    # this serial agent no longer owns the child. Offline is not stopped.
    from . import server as srv
    node = srv._node_record_for_name(state.get("nodes") or {}, str(run.get("buildNode") or ""))
    if not isinstance(node, dict) or not srv._node_agent_is_ready(node):
        return False
    agent = node.get("agent") or {}
    return ("activeTaskId" in agent
            and int(agent.get("activeTaskObservedAt") or 0) > int(run.get("queueOwnerReleasedAt") or run.get("updatedAt") or 0)
            and str(agent.get("activeTaskId") or "") != task_id)


def reconcile_released(state: State) -> bool:
    changed = reap_stale(state)
    for build_id in list(state.setdefault("buildQueue", {})):
        run = _runs(state).get(build_id)
        if isinstance(run, dict) and run.get("queueOwnerReleased"):
            changed = _request_stop(state, run, int(time.time())) or changed
            if _safe_to_release(state, run):
                discard(state, run)
                changed = True
    return changed


def reap_stale(state: State, *, now: int | None = None) -> bool:
    """Request a stalled owner's exit; only execute/recover can release it."""
    current = int(now if now is not None else time.time())
    changed = False
    for run in list(_active(_runs(state))):
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "")
        updated = int(run.get("updatedAt") or run.get("createdAt") or 0)
        if status == "canceling":
            requested = int(run.get("cancelRequestedAt") or updated)
            if current - max(requested, 0) < CANCELING_STALE_SECONDS:
                continue
            run.update(
                status="canceled",
                completedAt=current,
                canceledAt=current,
                updatedAt=current,
                message="Canceled after waiting for queue unwind",
            )
            _request_stop(state, run, current)
            changed = True
        elif status == "finalizing" and updated and current - updated >= (
                DEPLOY_STALE_SECONDS if _stage(state, run) == "deploy" else FINALIZING_STALE_SECONDS):
            run.update(
                status="failed",
                completedAt=current,
                updatedAt=current,
                message="Queued deployment stalled without progress",
                cancelRequestedAt=current,
            )
            _request_stop(state, run, current)
            changed = True
    return changed


def attach(state: State, run: State, kind: str, body: State) -> None:
    # Called inside the same transaction that creates/claims the build record.
    private = dict(body)
    private.pop("queue", None)
    for key in ("token", "gitToken", "registryAuth", "password"):
        private.pop(key, None)  # credentials are resolved from Control at execution
    state.setdefault("buildQueue", {})[run["id"]] = {"kind": kind, "body": private}
    key = replacement_key(run, body)
    if key:
        run["replacementKey"] = key
        run["queueKey"] = key
        now = int(time.time())
        runs = _runs(state)
        for older_id, older_payload in list(state["buildQueue"].items()):
            older = runs.get(older_id)
            if older_id == run["id"] or not isinstance(older, dict):
                continue
            older_key = replacement_key(older, older_payload.get("body") or {})
            if older_key != key or older.get("status") in TERMINAL and not older.get("queueExecuting"):
                continue
            _cancel_superseded(state, older, run["id"], now)
    run["queueKey"] = queue_identity(run, body)
    run["queueIdentityVersion"] = 2
    order = int(state.get("buildQueueSequence") or 0) + 1
    state["buildQueueSequence"] = order
    run.update(status="queued", queueOrder=order, queueManaged=True,
               queueRequestHash=_digest(private), message="Waiting in branch deployment queue")


def discard(state: State, run: State) -> None:
    state.setdefault("buildQueue", {}).pop(run["id"], None)
    run.pop("queueExecuting", None)
    run.pop("queueOwnerReleased", None)
    run.pop("queueOwnerReleasedAt", None)
    run.pop("queueClaimToken", None)


def _cancel_superseded(state: State, older: State, newer_id: str, now: int) -> None:
    if _deploying(state, older):
        # Interrupting a rollout between steps would leave it half applied;
        # the newer work deploys after it instead (see _deploy_ahead).
        return
    older.update(supersededBy=newer_id, cancelRequestedAt=now,
                 updatedAt=now, message="Superseded by " + newer_id)
    if older.get("queueExecuting"):
        if older.get("status") not in TERMINAL:
            older["status"] = "canceling"
        _request_stop(state, older, now)
    else:
        older.update(status="canceled", completedAt=now, canceledAt=now)
        discard(state, older)


def _supersede_key(state: State, run: State) -> str:
    """Deployed application within its repository branch: latest submission wins."""
    key = str(run.get("deployKey") or "")
    ident = _identity(state, run)
    if not key or not ident:
        return ""
    return json.dumps([*json.loads(ident)[:3], key], separators=(",", ":"))


def enter_deploy_stage(state: State, run: State, manifest: Any, now: int) -> bool:
    """Bind a built run to its deployment; return False when it is obsolete.

    Within one repository branch the latest submitted request for a deployed
    application wins: older work still waiting to deploy it is dropped, and
    this run is dropped when newer work already deployed it or is ahead.
    Other branches deploying the same application are only ordered, never
    dropped (see _deploy_ahead).
    """
    key = deploy_key_for_manifest(manifest)
    if not key:
        return True
    run["deployKey"] = key
    mine = _supersede_key(state, run)
    order = _order(run)
    deployed = int((state.get("deployedQueueOrder") or {}).get(mine) or 0) if mine else 0
    runs = _runs(state)
    newer = ""
    for other_id in list(state.get("buildQueue") or {}):
        other = runs.get(other_id)
        if (other_id == run["id"] or not isinstance(other, dict) or other.get("status") in TERMINAL
                or _stage(state, other) != "deploy" or not mine or _supersede_key(state, other) != mine):
            continue
        if _order(other) > order:
            newer = newer or other_id
        else:
            _cancel_superseded(state, other, run["id"], now)
    if newer or deployed > order[0]:
        reason = newer or "a newer deployment"
        run.update(status="canceled", completedAt=now, canceledAt=now, updatedAt=now,
                   supersededBy=newer, message="Superseded by " + reason)
        discard(state, run)
        return False
    return True


def record_deployed(state: State, run: State) -> None:
    key = _supersede_key(state, run)
    if not key:
        return
    deployed = state.setdefault("deployedQueueOrder", {})
    deployed[key] = max(int(deployed.get(key) or 0), _order(run)[0])
    if len(deployed) > 1000:
        for stale in sorted(deployed, key=lambda name: deployed[name])[:len(deployed) - 1000]:
            deployed.pop(stale, None)


def submit_local(token: str, build_id: str, body: State) -> State:
    from . import server as srv
    def enqueue(state):
        require_token(state, token, token_type="deploy")
        run = _runs(state).get(build_id)
        if not isinstance(run, dict) or run.get("mode") != "local":
            raise LumaError(f"local build run not found: {build_id}")
        private = {key: value for key, value in body.items() if key != "queue"}
        if run.get("queueManaged"):
            if run.get("queueRequestHash") != _digest(private):
                raise LumaError("local build was already submitted with a different result")
            return
        if run.get("status") != "running":
            raise LumaError(f"local build run is not active: {build_id}")
        if int(run.get("expiresAt") or 0) <= int(time.time()):
            raise LumaError(f"local build lease expired: {build_id}")
        if not isinstance(private.get("buildResult"), dict):
            raise LumaError("local build result must be an object")
        srv._validate_local_build_result(run, private["buildResult"])
        srv._request_env_secrets(private)
        attach(state, run, "local", private)
        if not enter_deploy_stage(state, run, private["buildResult"].get("manifest"), int(time.time())):
            return
        # Protect the immutable uploaded images from Registry retention while
        # waiting; a result image is not deployment success (status is queued).
        run["result"] = srv._build_run_result_summary(private["buildResult"])
    mutate_state(enqueue)
    return {"queued": True, "buildRunId": build_id, **srv.handle_build_run_get(token, build_id)}


def _concurrency() -> int:
    try:
        value = int(os.environ.get("LUMA_BUILD_QUEUE_CONCURRENCY", "4"))
    except ValueError as exc:
        raise LumaError("LUMA_BUILD_QUEUE_CONCURRENCY must be an integer from 1 to 32") from exc
    if not 1 <= value <= 32:
        raise LumaError("LUMA_BUILD_QUEUE_CONCURRENCY must be an integer from 1 to 32")
    return value


def _order(run: State) -> tuple[int, str]:
    return int(run.get("queueOrder") or 0), str(run["id"])


def _identity(state: State, run: State) -> str:
    if run.get("queueIdentityVersion") == 2 and run.get("queueKey"):
        return queue_identity(run)
    payload = (state.get("buildQueue") or {}).get(run["id"])
    return queue_identity(run, payload.get("body") if isinstance(payload, dict) else None)


def _node_ready(state: State, node: str) -> bool:
    from . import server as srv
    record = srv._node_record_for_name(state.get("nodes") or {}, node)
    return isinstance(record, dict) and srv._node_agent_is_ready(record, required_capability="docker-build")


def _builder_owners(state: State, current: list[State]) -> dict[str, str]:
    """Reserve serial builders before dispatch and until their child stops.

    Bound or leased agent tasks own their node. Admitted work that has not
    leased yet reserves its node, or for a pooled run one idle candidate in
    queue order, so admission never promises the same builder twice.
    """
    tasks = state.get("agentTasks") or {}
    owners: dict[str, str] = {}
    for task in sorted(_active(tasks, {"queued", "running"}), key=lambda t: (t.get("createdAt", 0), t.get("id", ""))):
        node = str(task.get("nodeName") or "")
        if node:
            owners.setdefault(node, str(task.get("buildRunId") or task.get("id") or ""))
    for run in sorted(current, key=_order):
        if run.get("mode") == "local" or not _holds_project(run) or _stage(state, run) == "deploy":
            continue
        task = tasks.get(run.get("agentTaskId"))
        if isinstance(task, dict) and task.get("status") in TERMINAL:
            continue  # Deployment can continue while the next image builds.
        if isinstance(task, dict) and task.get("status") in {"queued", "running"} and task.get("nodeName"):
            continue  # Already owns its node above.
        names = [str(task["nodeName"])] if isinstance(task, dict) and task.get("nodeName") else candidates(run)
        if len(names) == 1:
            owners.setdefault(names[0], str(run["id"]))
            continue
        for node in names:
            if node not in owners and _node_ready(state, node):
                owners[node] = str(run["id"])
                break
    return owners


def _deploying(state: State, run: State) -> bool:
    return bool(run.get("queueExecuting")) and _stage(state, run) == "deploy"


def _deploy_ahead(state: State, run: State, current: list[State]) -> list[State]:
    """Work that must deploy before ``run``: the same application, in order.

    Known deployed names are compared exactly. Work whose application is not
    known yet (still building) is fenced by its repository/branch identity.
    """
    key = str(run.get("deployKey") or "")
    ident = _identity(state, run)
    ahead = []
    for other in current:
        if other["id"] == run["id"] or _caller_upload(other):
            continue
        other_key = str(other.get("deployKey") or "")
        same = other_key == key if key and other_key else identities_conflict(ident, _identity(state, other))
        if same and (_deploying(state, other) or _order(other) < _order(run)):
            ahead.append(other)
    ahead.sort(key=lambda other: (not _deploying(state, other), _order(other)))
    return ahead


def _blocker(state: State, run: State, current: list[State], builders: dict[str, str], concurrency: int, *, describe: bool = False) -> State:
    if _stage(state, run) == "deploy":
        ahead = _deploy_ahead(state, run, current)
        if ahead:
            return {"queuePosition": len(ahead) + 1, "waitingFor": ahead[0]["id"], "waitReason": "target"}
        deploying = [other for other in current if _deploying(state, other)]
        if len(deploying) >= DEPLOY_SLOTS:
            return {"queuePosition": 1, "waitingFor": min(deploying, key=_order)["id"], "waitReason": "deploy"}
        return {}
    names = candidates(run)
    ready = [node for node in names if _node_ready(state, node)]
    if not ready:
        return {"queuePosition": 1, "waitingFor": "", "waitingForNode": names[0], "waitReason": "builder-offline"}
    idle = [node for node in ready if node not in builders]
    ahead: list[State] = []
    if describe:
        pool = set(names)
        ahead = sorted((other for other in current if other["id"] != run["id"] and other.get("status") == "queued"
                        and _stage(state, other) == "build" and _order(other) < _order(run)
                        and pool.intersection(candidates(other))), key=_order)
    if len(idle) <= len(ahead):
        busy = next((node for node in ready if node in builders), ready[0])
        return {"queuePosition": 1 + len(ahead) + int(not idle),
                "waitingFor": builders[busy] if not idle else ahead[0]["id"],
                "waitingForNode": busy if not idle else idle[0], "waitReason": "builder"}
    building = [other for other in current if other.get("queueExecuting") and _stage(state, other) == "build"]
    if len(building) >= concurrency:
        return {"queuePosition": 1, "waitingFor": min(building, key=_order)["id"], "waitReason": "capacity"}
    return {}


def position(build_id: str) -> State:
    # Polling must not hydrate every historical build and log stream.
    from .database import ensure_initialized, read_state, transaction
    ensure_initialized()
    with transaction(immediate=False) as conn:
        state = read_state(conn, lazy=True, metadata_kinds=QUEUE_METADATA_KINDS)
        run = _runs(state).get(build_id)
        if not isinstance(run, dict) or run.get("status") != "queued":
            return {}
        current = _current_runs(state)
        return _blocker(state, run, current, _builder_owners(state, current), _concurrency(), describe=True) or {
            "queuePosition": 1, "waitingFor": "", "waitReason": "ready",
        }


def claim(*, concurrency: int | None = None) -> WorkItem | None:
    from . import server as srv
    limit = _concurrency() if concurrency is None else concurrency
    def mutate(state):
        runs = _runs(state)
        changed = srv._expire_stale_local_build_runs(runs, int(time.time()))
        changed = reconcile_released(state) or changed
        current = _current_runs(state)
        if sum(bool(run.get("queueExecuting")) for run in current) >= limit + DEPLOY_SLOTS:
            return None, changed
        payloads = state.setdefault("buildQueue", {})
        builders = _builder_owners(state, current)
        queued = sorted((r for r in current if r.get("status") == "queued"), key=_order)
        for run in queued:
            payload = payloads.get(run["id"])
            if not isinstance(payload, dict):
                run.update(status="failed", message="Queued request is unavailable", completedAt=int(time.time()))
                changed = True
                continue
            if _blocker(state, run, current, builders, limit):
                continue
            deploy = _stage(state, run) == "deploy"
            # The claim token fences receipts: a retried cleanup of an earlier
            # stage must never release the worker that owns a later stage.
            claim_token = secrets.token_hex(8)
            run.update(status="finalizing" if deploy else "running",
                       queueExecuting=True, queueClaimToken=claim_token,
                       controlProcessInstanceId=srv._CONTROL_PROCESS_INSTANCE_ID,
                       updatedAt=int(time.time()), message="Deploying built image" if deploy and payload.get("built") else "Executing queued deployment")
            return (run["id"], {**payload, "claimToken": claim_token}), True
        return None, changed
    return mutate_state_if_changed(mutate, metadata_kinds=QUEUE_METADATA_KINDS)


def execute(item: WorkItem) -> None:
    from . import server as srv
    build_id, payload = item
    claim_token = payload.get("claimToken")
    reroute: State | None = None
    built: State | None = None
    deployed = False
    try:
        token = str(load_auth_state().get("deployToken") or "")
        body = dict(payload["body"])
        workflow = body.pop("workflow", None)
        if payload["kind"] == "local":
            result = srv.handle_local_build_complete(token, build_id, body, _from_queue=True)
        elif isinstance(payload.get("built"), dict):
            result = srv.handle_queued_repository_deploy(token, build_id, body, payload["built"])
        else:
            plan = payload.get("buildPlan")
            result = srv.handle_build_deploy(
                token, body, _queued_run_id=build_id,
                **({"_build_plan": plan} if isinstance(plan, dict) else {}),
            )
            if isinstance(result.get("requeueBuild"), dict):
                reroute = result["requeueBuild"]
                return
            if isinstance(result.get("requeueDeploy"), dict):
                built = result["requeueDeploy"]
                return
        deployed = True
        if isinstance(workflow, dict):
            # Recording belongs to the accepted task too, not the waiting CLI.
            # Keep the existing expectedRevision guard; never overwrite a newer
            # recipe just because an older queued request eventually finished.
            try:
                name = result.get("service") or result.get("deployment")
                image = result.get("image")
                if isinstance(image, dict):
                    image = image.get("selected") or image.get("requested")
                srv.handle_workflow_record(token, {
                    **workflow, "name": name, "source": "cli-success",
                    "evidence": {"image": image, "buildId": build_id, "revision": result.get("revision")},
                })
                recorded = {"saved": True, "name": name}
            except Exception as exc:
                recorded = {"saved": False, "warning": str(exc)}
            def record_outcome(state):
                run = _runs(state).get(build_id)
                if isinstance(run, dict) and run.get("status") == "succeeded":
                    run.setdefault("result", {})["workflow"] = recorded
            persist_receipt(lambda: mutate_state(record_outcome, metadata_kinds=QUEUE_METADATA_KINDS), owner="queue.workflow_receipt")
    except Exception as exc:
        srv._complete_build_run(build_id, "failed", message=str(exc))
    finally:
        def cleanup(state):
            run = _runs(state).get(build_id)
            if not isinstance(run, dict):
                return
            if claim_token and run.get("queueClaimToken") != claim_token:
                # This stage was already handed back to the queue (an ambiguous
                # commit is being retried) or the next stage owns the run now.
                return
            now = int(time.time())
            if ((reroute is not None or built is not None) and run.get("status") == "running"
                    and not run.get("cancelRequestedAt") and _safe_to_release(state, run)):
                # Preserve FIFO identity/order and the private request. Until
                # this transaction commits the stage keeps its reservation;
                # afterward the next stage is admitted like any queued work.
                stored = state.setdefault("buildQueue", {}).get(build_id)
                if isinstance(stored, dict):
                    run.pop("agentTaskId", None)
                    run.pop("queueExecuting", None)
                    run.pop("queueClaimToken", None)
                    if reroute is not None:
                        stored["buildPlan"] = reroute
                        names = [str(name) for name in reroute.get("candidates") or [] if str(name)] or [reroute["node"]]
                        run.update(status="queued", buildNode=reroute["node"], buildCandidates=names,
                                   buildPlatforms=reroute["platforms"], plannedCommit=reroute["commit"],
                                   updatedAt=now, message="Waiting for builder " + ", ".join(names))
                        run.setdefault("request", {})["buildNode"] = reroute["node"]
                        return
                    stored["built"] = built
                    srv._remember_build_affinity(state, str(built.get("projectKey") or run.get("projectKey") or ""),
                                                 str(built.get("builtOn") or run.get("buildNode") or ""))
                    build_result = built.get("buildResult") if isinstance(built.get("buildResult"), dict) else {}
                    # The pushed images must survive Registry retention while
                    # waiting; a result image is not deployment success.
                    run.update(status="queued", updatedAt=now, message="Image built; waiting to deploy",
                               result=srv._build_run_result_summary(build_result))
                    # A fallback manifest deploys when the repository has none.
                    body = stored.get("body") if isinstance(stored.get("body"), dict) else {}
                    enter_deploy_stage(state, run, build_result.get("manifest") or body.get("manifest"), now)
                    return
            if deployed and run.get("status") == "succeeded":
                record_deployed(state, run)
            if run.get("status") not in TERMINAL:
                canceled = bool(run.get("cancelRequestedAt"))
                run.update(status="canceled" if canceled else "failed", completedAt=now,
                           updatedAt=now,
                           message="Build canceled" if canceled else "Execution ended without a recorded result; inspect runtime before retrying")
            run["queueOwnerReleased"] = True
            run.setdefault("queueOwnerReleasedAt", now)
            _request_stop(state, run, now)
            if _safe_to_release(state, run):
                discard(state, run)
        persist_receipt(lambda: mutate_state(cleanup, metadata_kinds=QUEUE_METADATA_KINDS), owner="queue.release")


def recover() -> None:
    """Keep queued requests; never silently replay interrupted deployments."""
    if not is_initialized():
        return
    from . import server as srv
    def mutate(state):
        runs = _runs(state)
        for build_id in list(state.setdefault("buildQueue", {})):
            run = runs.get(build_id)
            if not isinstance(run, dict):
                state["buildQueue"].pop(build_id, None)
                continue
            if run.get("status") == "queued":
                continue
            if run.get("controlProcessInstanceId") == srv._CONTROL_PROCESS_INSTANCE_ID and run.get("queueExecuting"):
                continue
            if run.get("status") not in TERMINAL:
                run.update(status="failed", message="Deployment interrupted by Control restart; inspect runtime before retrying",
                           completedAt=int(time.time()), updatedAt=int(time.time()))
            run["queueOwnerReleased"] = True
            run.setdefault("queueOwnerReleasedAt", int(time.time()))
            _request_stop(state, run, int(time.time()))
            if _safe_to_release(state, run):
                discard(state, run)
    mutate_state(mutate, metadata_kinds=QUEUE_METADATA_KINDS)


class BuildQueueWorker:
    def __init__(self, concurrency: int | None = None):
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.concurrency = _concurrency() if concurrency is None else concurrency

    def start(self) -> None:
        if any(thread.is_alive() for thread in self.threads):
            return
        self.stop.clear()
        self.threads.clear()
        recover()
        # Build stages share ``concurrency`` slots; deployments have their own
        # so a finished image never waits behind long-running builds.
        for index in range(self.concurrency + DEPLOY_SLOTS):
            thread = threading.Thread(target=self.run, name=f"luma-build-queue-{index}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=2)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                item = claim(concurrency=self.concurrency) if is_initialized() else None
                if item:
                    execute(item)
                    continue
            except Exception:
                LOG.error("Build queue iteration failed; queued requests remain durable")
            self.stop.wait(1)
