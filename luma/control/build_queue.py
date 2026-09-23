"""Durable management build/deploy FIFO, independent of HTTP request lifetime.

Queue ownership is per repository, ref and deployment target. Unknown targets
keep a conservative lock within that repository/ref, never across branches.
Builder admission follows the serial node-agent's capacity; waiting for a busy
builder must not consume a Control execution slot.

Only queued work is resumed after restart. An interrupted active deployment is
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
import sqlite3
import threading
import time
import urllib.parse
from typing import Any

from ..errors import LumaError
from .state import is_initialized, load_auth_state, mutate_state, mutate_state_if_changed, require_token

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "canceled"}
ACTIVE = {"running", "canceling", "finalizing"}
# Silence can trigger cancellation, but never proves that a worker has exited.
FINALIZING_STALE_SECONDS = 600
CANCELING_STALE_SECONDS = 180
State = dict[str, Any]
WorkItem = tuple[str, dict[str, Any]]


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


def _holds_project(run: State) -> bool:
    caller_upload = (
        run.get("mode") == "local"
        and run.get("request", {}).get("queue") is True
        and not run.get("queueManaged")
    )
    return bool(run.get("queueExecuting")) or (run.get("status") in ACTIVE and not caller_upload)


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
        elif status == "finalizing" and updated and current - updated >= FINALIZING_STALE_SECONDS:
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
            older.update(supersededBy=run["id"], cancelRequestedAt=now,
                         updatedAt=now, message="Superseded by " + run["id"])
            if older.get("queueExecuting"):
                if older.get("status") not in TERMINAL:
                    older["status"] = "canceling"
                _request_stop(state, older, now)
            else:
                older.update(status="canceled", completedAt=now, canceledAt=now)
                discard(state, older)
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


def _builder_owners(state: State, current: list[State]) -> dict[str, str]:
    """Reserve a serial builder before dispatch and until its child stops."""
    tasks = state.get("agentTasks") or {}
    owners = {}
    for task in sorted(_active(tasks, {"queued", "running"}), key=lambda t: (t.get("createdAt", 0), t.get("id", ""))):
        owners.setdefault(str(task.get("nodeName") or ""), str(task.get("buildRunId") or task.get("id") or ""))
    for run in sorted(current, key=_order):
        if run.get("mode") == "local" or not _holds_project(run):
            continue
        task = tasks.get(run.get("agentTaskId"))
        if isinstance(task, dict) and task.get("status") in TERMINAL:
            continue  # Deployment can continue while the next image builds.
        owners.setdefault(str(run.get("buildNode") or ""), str(run["id"]))
    return owners


def _target_ahead(state: State, run: State, current: list[State]) -> list[State]:
    ident = _identity(state, run)
    ahead = [other for other in current if other["id"] != run["id"]
             and identities_conflict(ident, _identity(state, other))
             and (_holds_project(other) or other.get("status") == "queued" and _order(other) < _order(run))]
    ahead.sort(key=lambda other: (not _holds_project(other), _order(other)))
    return ahead


def _blocker(state: State, run: State, current: list[State], builders: dict[str, str], concurrency: int, *, describe: bool = False) -> State:
    from . import server as srv
    ahead = _target_ahead(state, run, current)
    if ahead:
        return {"queuePosition": len(ahead) + 1, "waitingFor": ahead[0]["id"], "waitReason": "target"}
    if run.get("mode") != "local":
        node = str(run.get("buildNode") or "")
        node_ahead = sorted((other for other in current if other.get("status") == "queued"
                            and other.get("mode") != "local" and other.get("buildNode") == node
                            and _order(other) < _order(run) and not _target_ahead(state, other, current)), key=_order) if describe else []
        rank = 1 + len(node_ahead) + int(node in builders)
        record = srv._node_record_for_name(state.get("nodes") or {}, node)
        if not isinstance(record, dict) or not srv._node_agent_is_ready(record, required_capability="docker-build"):
            return {"queuePosition": rank, "waitingFor": "", "waitingForNode": node, "waitReason": "builder-offline"}
        if node in builders or node_ahead:
            return {"queuePosition": rank, "waitingFor": builders[node] if node in builders else node_ahead[0]["id"],
                    "waitingForNode": node, "waitReason": "builder"}
    executing = [other for other in current if other.get("queueExecuting")]
    if len(executing) >= concurrency:
        return {"queuePosition": 1, "waitingFor": min(executing, key=_order)["id"], "waitReason": "capacity"}
    return {}


def position(build_id: str) -> State:
    # Polling must not hydrate every historical build and log stream.
    from .database import ensure_initialized, read_state, transaction
    ensure_initialized()
    with transaction(immediate=False) as conn:
        state = read_state(conn, lazy=True)
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
        if sum(bool(run.get("queueExecuting")) for run in current) >= limit:
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
            run.update(status="finalizing" if payload["kind"] == "local" else "running",
                       queueExecuting=True, controlProcessInstanceId=srv._CONTROL_PROCESS_INSTANCE_ID,
                       updatedAt=int(time.time()), message="Executing queued deployment")
            return (run["id"], dict(payload)), True
        return None, changed
    return mutate_state_if_changed(mutate)


def execute(item: WorkItem) -> None:
    from . import server as srv
    build_id, payload = item
    try:
        token = str(load_auth_state().get("deployToken") or "")
        body = dict(payload["body"])
        workflow = body.pop("workflow", None)
        if payload["kind"] == "local":
            result = srv.handle_local_build_complete(token, build_id, body, _from_queue=True)
        else:
            result = srv.handle_build_deploy(token, body, _queued_run_id=build_id)
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
            mutate_state(record_outcome)
    except Exception as exc:
        srv._complete_build_run(build_id, "failed", message=str(exc))
    finally:
        def cleanup(state):
            run = _runs(state).get(build_id)
            if isinstance(run, dict):
                if run.get("status") not in TERMINAL:
                    canceled = bool(run.get("cancelRequestedAt"))
                    run.update(status="canceled" if canceled else "failed", completedAt=int(time.time()),
                               updatedAt=int(time.time()),
                               message="Build canceled" if canceled else "Execution ended without a recorded result; inspect runtime before retrying")
                run["queueOwnerReleased"] = True
                run.setdefault("queueOwnerReleasedAt", int(time.time()))
                _request_stop(state, run, int(time.time()))
                if _safe_to_release(state, run):
                    discard(state, run)
        while True:
            try:
                mutate_state(cleanup)
                break
            except (sqlite3.OperationalError, LumaError) as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                # Host side effects already happened. Retry only the receipt,
                # never the deployment, and retain this worker slot meanwhile.
                LOG.warning("Build execution finished; retrying queue ownership release after database contention")
                time.sleep(1)


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
    mutate_state(mutate)


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
        for index in range(self.concurrency):
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
