"""Deterministic batch jobs with explicit resource ownership and receipts."""
from __future__ import annotations

import json
import re
import time
import threading
import secrets
import urllib.error
import urllib.parse
import urllib.request

from ..errors import LumaError
from .model import Conflict
from .placement import selected_nodes


class SubmissionRejected(Conflict):
    """Nomad explicitly rejected an API request before committing it."""


class Nomad:
    def __init__(self, settings):
        self.settings = settings
        self._node_cache = {}
        self._node_lock = threading.Lock()

    def request(self, method, path, body=None, *, missing_ok=False):
        separator = "&" if "?" in path else "?"
        url = self.settings.nomad_url + path + separator + urllib.parse.urlencode({"namespace": self.settings.nomad_namespace})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "X-Nomad-Token": self.settings.nomad_token}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=15) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            if exc.code == 409:
                raise Conflict("Nomad conditional submission conflicted") from exc
            raise (SubmissionRejected if 400 <= exc.code < 500 else LumaError)(f"Nomad {method} {path} returned HTTP {exc.code}") from exc

    @staticmethod
    def variable_path(job_id):
        return "nomad/jobs/" + job_id + "/build/executor"

    def eligible(self, platform, nodes, explicit=''):
        selected = selected_nodes(nodes, platform, explicit)
        result = []
        for node in selected:
            node_id = node['nodeId']
            with self._node_lock:
                cached = self._node_cache.get(node_id)
            if cached and time.monotonic() - cached[0] < 5:
                live = cached[1]
            else:
                live = self.request('GET', '/v1/node/' + urllib.parse.quote(node_id, safe=''), missing_ok=True)
                with self._node_lock:
                    self._node_cache[node_id] = (time.monotonic(), live)
            if not live:
                continue
            attrs = live.get('Attributes') or {}
            docker = (live.get('Drivers') or {}).get('docker') or {}
            if live.get('Status') != 'ready' or live.get('SchedulingEligibility') != 'eligible' or live.get('Drain'):
                continue
            if live.get('NodePool', 'default') != self.settings.build_pool:
                continue
            if attrs.get('kernel.name') != 'linux' or 'linux/' + attrs.get('cpu.arch', '') != platform:
                continue
            if not docker.get('Detected') or not docker.get('Healthy'):
                continue
            if float(attrs.get('cpu.totalcompute') or 0) < self.settings.cpu or int(attrs.get('memory.totalbytes') or 0) < self.settings.memory_mb * 1024 * 1024:
                continue
            result.append(node_id)
        return sorted(set(result))

    def submit(self, store, run: dict, number: int, allowed_nodes=None) -> dict:
        job_id = "luma-" + run["id"] + f"-a{number}"
        existing = self.request("GET", "/v1/job/" + job_id, missing_ok=True)
        if existing:
            attempt = store.attempt(run["id"], number)
            if existing.get("Meta", {}).get("luma.attempt") != attempt["id"]:
                raise Conflict("Nomad job identity does not match this attempt")
            store.submitted_attempt(attempt["id"])
            return attempt
        var_path = self.variable_path(job_id)
        variable = self.request("GET", "/v1/var/" + var_path, missing_ok=True)
        if variable is None:
            bootstrap = {"endpoint": self.settings.public_url, "attemptId": run["id"] + f"-a{number}", "token": secrets.token_urlsafe(32)}
            try:
                variable = self.request("PUT", "/v1/var/" + var_path + "?cas=0", {"Items": {"bootstrap": json.dumps(bootstrap)}})
            except Conflict:
                variable = self.request("GET", "/v1/var/" + var_path)
        bootstrap = json.loads(variable["Items"]["bootstrap"])
        attempt = store.attempt(run["id"], number, token=bootstrap["token"])
        existing = self.request("GET", "/v1/job/" + job_id, missing_ok=True)
        if existing:
            if existing.get("Meta", {}).get("luma.attempt") != attempt["id"]:
                raise Conflict("Nomad job identity does not match this attempt")
            store.submitted_attempt(attempt["id"])
            return attempt
        platform = run["prepared"]["platforms"][number - 1]
        architecture = platform.split("/")[1]
        if not allowed_nodes:
            if attempt['status'] == 'pending':
                store.finish_attempt(attempt['id'], 'rejected')
                self.revoke(attempt)
            elif attempt['status'] == 'submitting':
                if self.fence_submission(attempt)['terminal']:
                    store.finish_attempt(attempt['id'], 'rejected')
                    self.revoke(attempt)
            raise Conflict('no explicitly approved native build node is available')
        constraints = [{"LTarget": "${attr.kernel.name}", "Operand": "=", "RTarget": "linux"},
                       {"LTarget": "${attr.cpu.arch}", "Operand": "=", "RTarget": architecture},
                       {"LTarget": "${node.unique.id}", "Operand": "regexp", "RTarget": "^(?:" + "|".join(re.escape(node) for node in allowed_nodes) + ")$"}]
        task = {"Name": "executor", "Driver": "docker", "Config": {"image": self.settings.executor_image, "privileged": True, "cpu_hard_limit": True},
                "Env": {"LUMA_EXECUTOR_BOOTSTRAP": "${NOMAD_SECRETS_DIR}/bootstrap.json", "TZ": "Asia/Shanghai", "LUMA_EXECUTOR_NODE_ID": "${node.unique.id}", "LUMA_EXECUTOR_HOST_ARCH": "${attr.cpu.arch}"},
                "Resources": {"CPU": self.settings.cpu, "MemoryMB": self.settings.memory_mb},
                "KillTimeout": 30_000_000_000,
                "Templates": [{"DestPath": "secrets/bootstrap.json", "EmbeddedTmpl": '{{ with nomadVar "' + var_path + '" }}{{ .bootstrap }}{{ end }}', "Perms": "0400", "ChangeMode": "noop", "Once": True}],
                "LogConfig": {"MaxFiles": 4, "MaxFileSizeMB": 16}}
        job = {"ID": job_id, "Name": job_id, "Type": "batch", "Namespace": self.settings.nomad_namespace,
               "Datacenters": ["*"], "NodePool": self.settings.build_pool, "Meta": {"luma.build": run["id"], "luma.attempt": attempt["id"]}, "Constraints": constraints,
               "TaskGroups": [{"Name": "build", "Count": 1, "Tasks": [task],
                               "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                               "ReschedulePolicy": {"Attempts": 0, "Unlimited": False},
                               "EphemeralDisk": {"SizeMB": self.settings.disk_mb}}]}
        store.submitting_attempt(attempt['id'])
        try:
            self.request("POST", "/v1/jobs", {"Job": job, "EnforceIndex": True, "JobModifyIndex": 0})
        except SubmissionRejected:
            store.finish_attempt(attempt["id"], "rejected")
            self.revoke(attempt)
            raise
        except Conflict:
            existing = self.request("GET", "/v1/job/" + job_id)
            if existing.get("Meta", {}).get("luma.attempt") != attempt["id"]:
                raise
        store.submitted_attempt(attempt["id"])
        return attempt

    def fence_submission(self, attempt):
        # A stopped create-only job is a tombstone for an uncertain initial
        # POST. Whichever registration wins, EnforceIndex=0 prevents the other
        # from resurrecting work after capacity is returned.
        job_id = attempt['job_id']
        current = self.request('GET', '/v1/job/' + job_id, missing_ok=True)
        if current is None:
            job = {'ID': job_id, 'Name': job_id, 'Type': 'batch', 'Stop': True,
                   'Namespace': self.settings.nomad_namespace, 'Datacenters': ['*'],
                   'Meta': {'luma.attempt': attempt['id']},
                   'TaskGroups': [{'Name': 'build', 'Count': 1, 'Tasks': [{'Name': 'executor', 'Driver': 'docker', 'Config': {'image': self.settings.executor_image}}]}]}
            try:
                self.request('POST', '/v1/jobs', {'Job': job, 'EnforceIndex': True, 'JobModifyIndex': 0})
            except Conflict:
                pass
            current = self.request('GET', '/v1/job/' + job_id)
        if (current.get('Meta') or {}).get('luma.attempt') != attempt['id']:
            raise Conflict('cannot fence a Nomad job belonging to another attempt')
        return self.stop(job_id)

    def observe(self, job_id: str) -> dict:
        job = self.request("GET", "/v1/job/" + job_id, missing_ok=True)
        if job is None:
            return {"status": "missing", "terminal": False}
        allocations = self.request("GET", "/v1/job/" + job_id + "/allocations") or []
        states = [allocation.get("ClientStatus") for allocation in allocations]
        if any(state in {"running", "pending", "unknown", "lost"} for state in states):
            return {"status": "unknown" if any(state in {"unknown", "lost"} for state in states) else "running" if "running" in states else "starting" if any(item.get('NodeID') for item in allocations) else "pending", "terminal": False}
        if not allocations:
            return {"status": "canceled" if job.get("Stop") else "pending", "terminal": bool(job.get("Stop"))}
        latest = max(allocations, key=lambda value: int(value.get("CreateIndex") or 0))
        task = (latest.get("TaskStates") or {}).get("executor") or {}
        exits = [event for event in task.get("Events") or [] if event.get("Type") == "Terminated"]
        if job.get("Stop"):
            return {"status": "canceled", "terminal": True}
        if task.get("State") == "dead" and not task.get("Failed") and exits and exits[-1].get("ExitCode") == 0:
            return {"status": "succeeded", "terminal": True}
        if latest.get("ClientStatus") in {"failed", "complete"}:
            return {"status": "failed", "terminal": True, "message": "Nomad executor terminated unsuccessfully"}
        return {"status": "pending", "terminal": False}

    def stop(self, job_id: str) -> dict:
        self.request("DELETE", "/v1/job/" + job_id + "?purge=false", missing_ok=True)
        return self.observe(job_id)

    def revoke(self, attempt: dict):
        self.request("DELETE", "/v1/var/" + self.variable_path(attempt["job_id"]), missing_ok=True)
