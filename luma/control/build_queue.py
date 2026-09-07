"""Durable management build/deploy FIFO, independent of HTTP request lifetime.

Only queued work is resumed after restart. An interrupted active deployment is
failed explicitly rather than replaying possibly completed runtime side effects.
Payloads live in private Control state, never public build history. In particular
per-attempt envSecrets are not mixed into other attempts or the public request.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

from ..errors import LumaError
from .state import is_initialized, load_state, mutate_state, require_token

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "canceled"}
ACTIVE = {"running", "canceling", "finalizing"}
State = dict[str, Any]
WorkItem = tuple[str, dict[str, Any]]


def _runs(state: State) -> State:
    from .server import _build_runs
    return _build_runs(state)


def _active(runs: State) -> list[State]:
    values = getattr(runs, "active_values", None)
    return list(values(ACTIVE | {"queued"})) if callable(values) else list(runs.values())


def _holds_project(run: State) -> bool:
    caller_upload = (
        run.get("mode") == "local"
        and run.get("request", {}).get("queue") is True
        and not run.get("queueManaged")
    )
    return bool(run.get("queueExecuting")) or (run.get("status") in ACTIVE and not caller_upload)


def _digest(body: State) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def attach(state: State, run: State, kind: str, body: State) -> None:
    # Called inside the same transaction that creates/claims the build record.
    private = dict(body)
    private.pop("queue", None)
    for key in ("token", "gitToken", "registryAuth", "password"):
        private.pop(key, None)  # credentials are resolved from Control at execution
    state.setdefault("buildQueue", {})[run["id"]] = {"kind": kind, "body": private}
    order = int(state.get("buildQueueSequence") or 0) + 1
    state["buildQueueSequence"] = order
    run.update(status="queued", queueOrder=order, queueManaged=True,
               queueRequestHash=_digest(private), message="Waiting in project deployment queue")


def discard(state: State, run: State) -> None:
    state.setdefault("buildQueue", {}).pop(run["id"], None)
    run.pop("queueExecuting", None)


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


def position(build_id: str) -> State:
    state = load_state()
    runs = _runs(state)
    run = runs.get(build_id)
    if not isinstance(run, dict) or run.get("status") != "queued":
        return {}
    same = [r for r in _active(runs) if r.get("projectKey") == run.get("projectKey") and r.get("id") != build_id]
    ahead = [r for r in same if _holds_project(r)
             or r.get("status") == "queued" and (r.get("queueOrder", 0), r["id"]) < (run.get("queueOrder", 0), build_id)]
    ahead.sort(key=lambda r: (r.get("status") == "queued", r.get("queueOrder", 0), r["id"]))
    return {"queuePosition": len(ahead) + 1, "waitingFor": str(ahead[0]["id"]) if ahead else ""}


def claim() -> WorkItem | None:
    from . import server as srv
    def mutate(state):
        runs = _runs(state)
        srv._expire_stale_local_build_runs(runs, int(time.time()))
        active = _active(runs)
        # queueExecuting also fences a worker whose progress event set failed
        # before it has actually unwound its runtime mutation.
        busy = {r.get("projectKey") for r in active if _holds_project(r)}
        payloads = state.setdefault("buildQueue", {})
        for run_id in payloads:
            record = runs.get(run_id)
            if isinstance(record, dict) and record.get("queueExecuting"):
                busy.add(record.get("projectKey"))
        queued = sorted((r for r in active if r.get("status") == "queued"),
                        key=lambda r: (r.get("queueOrder", 0), r["id"]))
        for run in queued:
            if run.get("projectKey") in busy:
                continue
            payload = payloads.get(run["id"])
            if not isinstance(payload, dict):
                run.update(status="failed", message="Queued request is unavailable", completedAt=int(time.time()))
                continue
            run.update(status="finalizing" if payload["kind"] == "local" else "running",
                       queueExecuting=True, controlProcessInstanceId=srv._CONTROL_PROCESS_INSTANCE_ID,
                       updatedAt=int(time.time()), message="Executing queued deployment")
            return run["id"], dict(payload)
        return None
    return mutate_state(mutate)


def execute(item: WorkItem) -> None:
    from . import server as srv
    build_id, payload = item
    try:
        token = str(load_state().get("deployToken") or "")
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
                discard(state, run)
        mutate_state(cleanup)


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
            discard(state, run)
    mutate_state(mutate)


class BuildQueueWorker:
    def __init__(self, concurrency: int = 4):
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.concurrency = concurrency

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
                item = claim() if is_initialized() else None
                if item:
                    execute(item)
                    continue
            except Exception:
                LOG.error("Build queue iteration failed; queued requests remain durable")
            self.stop.wait(1)
