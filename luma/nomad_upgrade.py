"""Independent manager supervisor for automatic Nomad security upgrades.

Installed by bootstrap / `luma update manager`. It waits for upgraded agents,
persists every step, and outlives Control and Nomad restarts. Nothing runs on
module import. The CA and administrator token stay outside Control's mount.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import shlex
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import LumaError
from .local import LocalExecutor
from .nomad_acl import activate_acl, prepare_acl, verify_acl
from .nomad_api import NomadApi
from .nomad_migration_node import CAPABILITY, execute, resume_operation, snapshot, version_path
from .nomad_pki import client_tls, endpoint, expires_soon, prepare_manager, sign_csr, sign_enrollments
from .nomad_security import ADMIN_ROOT, NOMAD_RELEASES, NOMAD_VERSION, ROLES, authority_digest, job_digest, nomad_token, prepare_job, private_read, private_write, read_private_json, security_root, validate_application, workload_policy_lock

JOURNAL = ADMIN_ROOT / "upgrade.json"


@contextmanager
def _journal_lock():
    ADMIN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(ADMIN_ROOT / "upgrade.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def public_status() -> dict[str, Any]:
    status = read_private_json(security_root() / "upgrade-status.json", optional=True)
    if not status:
        return {"status": "pending", "phase": "manager-update", "message": "Update the manager to start the automatic Nomad security migration", "targetVersion": NOMAD_VERSION}
    if status.get("status") == "running" and time.time() - float(status.get("updatedAt") or 0) > 900:
        return {**status, "status": "failed", "message": "The host security supervisor has stopped reporting progress; rerun the manager update to resume"}
    return status


def _save(journal: dict[str, Any], *, status: str, phase: str, message: str, waiting: list[str] | None = None) -> None:
    journal.update(status=status, phase=phase, message=message, updatedAt=int(time.time()))
    private_write(JOURNAL, json.dumps(journal, indent=2) + "\n")
    steps = journal.get("steps") or []
    public = {key: journal.get(key) for key in ("status", "phase", "message", "updatedAt", "id", "verifiedAt") if journal.get(key) is not None}
    public.update(targetVersion=NOMAD_VERSION, completedSteps=int(journal.get("next") or 0), totalSteps=len(steps), waitingNodes=waiting or [])
    public["nodes"] = [{"node": name, "version": node.get("version"), "verified": bool(node.get("verification"))} for name, node in (journal.get("nodes") or {}).items()]
    private_write(security_root() / "upgrade-status.json", json.dumps(public))


def _state() -> dict[str, Any]:
    from .control.state import load_state
    return load_state()


def prepare() -> dict[str, Any]:
    """Called by the upgraded host CLI before refreshing consumer jobs."""
    with _journal_lock():
        return _prepare_locked()


def _prepare_locked() -> dict[str, Any]:
    previous = read_private_json(JOURNAL, optional=True)
    recovered = resume_operation(allow_server=True, payload=previous.get("taskPayload"))
    if recovered and recovered.get("aclBootstrapPending"):
        activate_acl()
    try:
        facts = snapshot()
    except LumaError:
        previous = read_private_json(JOURNAL, optional=True)
        if previous.get("phase") != "activate" or not read_private_json(security_root() / "transport.json", optional=True).get("http"):
            raise
        activate_acl()
        facts = snapshot()
    if facts["role"] != "server" or facts["os"] != "linux":
        raise LumaError("automatic manager security migration requires the Linux Nomad server host")
    if facts["acl"] and not (ADMIN_ROOT / "acl-bootstrap.json").exists():
        raise LumaError("existing external Nomad ACL ownership must be imported explicitly; no ACL reset was attempted")
    if (facts["tlsHTTP"] or facts["tlsRPC"]) and not (ADMIN_ROOT / "ca" / "key.pem").exists():
        raise LumaError("existing external Nomad TLS ownership must be imported explicitly; its CA was not replaced")
    _require_single_server()
    prepare_acl(enforce_workloads=False)
    prepare_manager(facts["address"])
    journal = read_private_json(JOURNAL, optional=True)
    if not journal:
        journal = {"schema": 1, "id": "nomad-security-" + str(int(time.time())), "manager": facts["node"], "managerId": facts["nodeId"], "attempt": 0, "nodes": {facts["node"]: facts}, "next": 0}
    elif journal.get("managerId") != facts["nodeId"]:
        raise LumaError("the security journal belongs to a different Nomad server identity")
    if journal.get("status") == "failed":
        journal["attempt"] = int(journal.get("attempt") or 0) + 1
        if not journal.get("steps"):
            journal.pop("taskPayload", None)
            journal.update(nodes={facts["node"]: facts}, next=0)
    if journal.get("status") != "completed":
        _save(journal, status="running" if journal.get("steps") else "pending", phase=journal.get("phase") if journal.get("steps") else "agents", message="Waiting for all registered Nomad node agents to finish their normal Luma update")
    return public_status()


def install_supervisor() -> None:
    service = ("[Unit]\nDescription=Luma Nomad security migration and certificate enrollment\n"
               "Wants=network-online.target\nAfter=network-online.target\nStartLimitIntervalSec=0\n\n"
               "[Service]\nType=simple\nUser=root\nUMask=0077\n"
               f"ExecStart={shlex.join([sys.executable, '-m', 'luma.nomad_upgrade', 'run'])}\n"
               "Environment=LUMA_CONTROL_CONFIG=/opt/luma/luma.yaml\n"
               "Restart=always\nRestartSec=10\nKillMode=process\n\n[Install]\nWantedBy=multi-user.target\n")
    private_write(Path("/etc/systemd/system/luma-nomad-security.service"), service, mode=0o644)
    LocalExecutor().run("systemctl daemon-reload && systemctl enable luma-nomad-security && systemctl restart luma-nomad-security", timeout=60)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def control_task(state: Mapping[str, Any], name: str, key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    # Host-local Control is deliberately bound to loopback. Using it here
    # avoids depending on public ingress while replacing its discovery token.
    data = json.dumps({"nodeName": name, "operationKey": key, "payload": payload}).encode()
    request = urllib.request.Request("http://127.0.0.1:8080/v1/security/nomad/tasks", data=data,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(state["deployToken"])})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Do not echo a task response: it can contain diagnostic data from the
        # host. The API persists the useful, sanitized terminal message.
        raise LumaError(f"Control has not accepted the security step (HTTP {exc.code}); the migration remains incomplete") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise LumaError("Control is unavailable during the security upgrade; the saved step will resume") from None


def registered_nodes(state: Mapping[str, Any], manager_id: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    nodes: dict[str, dict[str, Any]] = {}
    waiting: list[str] = []
    seen: set[str] = set()
    for name, record in sorted((state.get("nodes") or {}).items()):
        agent = record.get("agent") or {}
        node_id = str(record.get("nomadNodeId") or record.get("nodeId") or agent.get("nodeId") or "")
        if not node_id or node_id == manager_id:
            continue
        if node_id in seen:
            raise LumaError("multiple Luma records claim the same Nomad node; resolve the registration before security migration")
        seen.add(node_id)
        nodes[name] = {"nodeId": node_id, "address": str(record.get("tailscaleIP") or "")}
        if CAPABILITY not in (agent.get("capabilities") or []) or time.time() - int(agent.get("lastSeen") or 0) > 120:
            waiting.append(name)
    return nodes, waiting


def _source_jobs(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    from .config import load_config
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "/opt/luma/luma.yaml")
    config = load_config(config_path)
    root = config.stack_root
    if not root.is_absolute():
        root = config_path.parent / root
    sources = {}
    records = state.get("deployments") or {}
    for kind in ("services", "compose"):
        for slug, record in (records.get(kind) or {}).items():
            if not isinstance(record, dict) or not record.get("manifest") or not re_slug(slug):
                continue
            spec = yaml.safe_load(record["manifest"]) or {}
            region = str(spec.get("region") or record.get("region") or "")
            path = root / "compose" / slug / (slug + ".nomad.json") if kind == "compose" else root / region / slug / (slug + ".nomad.json")
            custom = spec.get("stackPath")
            if custom:
                path = Path(custom)
                if not path.is_absolute():
                    path = config_path.parent / path
            if path.is_symlink() or root.resolve() not in path.resolve().parents:
                raise LumaError("recorded Nomad artifact escapes the managed stack directory")
            if path.is_file():
                job = json.loads(path.read_text()).get("Job")
                if isinstance(job, dict) and job.get("ID") == slug:
                    validate_application(job)
                    sources[slug] = job
    return sources


def re_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value))


def _matches_expected_job(job: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if not expected or authority_digest(expected) != authority_digest(job):
        return False
    expected_tasks = {(g["Name"], t["Name"]): t for g in expected.get("TaskGroups") or [] for t in g.get("Tasks") or []}
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            original = expected_tasks.get((group["Name"], task["Name"])) or {}
            if (original.get("Env") or {}) != (task.get("Env") or {}):
                return False
            for key in ("image", "command", "args", "entrypoint", "work_dir"):
                if ((original.get("Config") or {}).get(key) or None) != ((task.get("Config") or {}).get(key) or None):
                    return False
    return True


def _system_job(job: Mapping[str, Any]) -> bool:
    expected = read_private_json(ADMIN_ROOT / "system-jobs" / (str(job.get("ID")) + ".json"), optional=True).get("Job")
    if expected is None and job.get("ID") == "egress":
        # Manager refresh normally leaves egress running. Recognize its exact
        # installed renderer + configured image, not a job-name exemption.
        from .bootstrap import _egress_image
        from .config import load_config
        from .nomad_render import render_egress_job
        config = load_config(Path(os.environ.get("LUMA_CONTROL_CONFIG") or "/opt/luma/luma.yaml"))
        expected = json.loads(render_egress_job(image=_egress_image(config)))["Job"]
    return bool(expected) and _matches_expected_job(job, expected)


def _require_single_server() -> None:
    client = NomadApi("http://127.0.0.1:4646", token=nomad_token())
    configuration = client.request("GET", "/v1/operator/raft/configuration")
    servers = configuration.get("Servers") if isinstance(configuration, dict) else None
    if not isinstance(servers, list) or len(servers) != 1 or servers[0].get("Leader") is not True or servers[0].get("Voter") is not True:
        raise LumaError("automatic Nomad security migration requires exactly one healthy Raft server; the multi-server cluster was retained")


def inventory(state: Mapping[str, Any], nodes: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = NomadApi("http://127.0.0.1:4646", token=nomad_token())
    sources = _source_jobs(state)
    allocations = client.request("GET", "/v1/allocations") or []
    mounts: dict[str, Any] = {name: {"mounts": [], "systemMounts": [], "trustedHost": node["role"] == "server"} for name, node in nodes.items()}
    node_names = {value["nodeId"]: name for name, value in nodes.items()}
    jobs = []
    for stub in client.request("GET", "/v1/jobs?namespace=*") or []:
        if stub.get("Stop") or stub.get("Status") == "dead":
            continue
        namespace, job_id = str(stub.get("Namespace") or "default"), str(stub["ID"])
        if namespace != "default":
            raise LumaError("automatic security migration requires explicit policy for non-default Nomad namespaces")
        job = client.request("GET", "/v1/job/" + urllib.parse.quote(job_id, safe=""))
        system = job_id in {"luma-control", "traefik", "egress"} and _system_job(job)
        source = sources.get(job_id)
        if not system and (source is None or authority_digest(source) != authority_digest(job)):
            raise LumaError(f"Nomad job {job_id} does not match a saved Luma deployment; automatic migration will not trust unknown live jobs")
        if not system:
            validate_application(job)
        job_mounts = [m for g in job.get("TaskGroups") or [] for t in g.get("Tasks") or [] for m in (t.get("Config") or {}).get("mount") or []]
        for allocation in allocations:
            if allocation.get("JobID") != job_id or allocation.get("ClientStatus") in {"complete", "failed", "lost"}:
                continue
            name = node_names.get(allocation.get("NodeID"))
            if not name:
                raise LumaError(f"Nomad job {job_id} runs on a node without an authenticated Luma registration")
            if system and name != next(n for n, v in nodes.items() if v["role"] == "server"):
                raise LumaError("system host permissions require the manager's independently verified profile")
            mounts[name]["systemMounts" if system else "mounts"].extend(job_mounts)
            mounts[name]["trustedHost"] |= bool(job_mounts)
        jobs.append({"Job": job, "system": system, "authoritySHA256": authority_digest(job)})
    return jobs, mounts


def migration_steps(nodes: Mapping[str, Any], manager: str) -> list[dict[str, Any]]:
    order = [manager, *sorted(set(nodes) - {manager})]
    steps = [{"kind": "preflight", "node": name} for name in order]
    for version in NOMAD_RELEASES:
        for name in order:
            if version in version_path(nodes[name]["version"]):
                steps.append({"kind": "version", "node": name, "version": version})
    already_secure = all(n.get("acl") and n.get("tlsHTTP") and n.get("tlsRPC") and not n.get("rpcUpgradeMode") for n in nodes.values())
    if not already_secure:
        steps += [{"kind": "rpc", "node": name} for name in order]
        steps.append({"kind": "activate", "node": manager})
        steps += [{"kind": "secure", "node": name} for name in order if name != manager]
    steps.append({"kind": "consumers", "node": manager})
    steps += [{"kind": "verify", "node": name} for name in order]
    return steps


def _raft_snapshot(label: str) -> None:
    _require_single_server()
    path = ADMIN_ROOT / "backups" / label / "raft.snap"
    if path.exists() and path.stat().st_size:
        LocalExecutor().run(shlex.join(["/usr/local/bin/nomad", "snapshot", "inspect", str(path)]), timeout=30)
        return
    tls = client_tls()
    handlers: list[Any] = [urllib.request.ProxyHandler({}), _NoRedirect()]
    address = endpoint("http://127.0.0.1:4646")
    if address.startswith("https://"):
        if not tls:
            raise LumaError("Nomad snapshot requires the installed TLS identity")
        context = ssl.create_default_context(cafile=tls["ca_file"])
        context.load_cert_chain(tls["cert_file"], tls["key_file"])
        handlers.append(urllib.request.HTTPSHandler(context=context))
    token = private_read(ADMIN_ROOT / "management.token", optional=True).strip() or nomad_token()
    req = urllib.request.Request(address + "/v1/operator/snapshot", headers={"X-Nomad-Token": token})
    with urllib.request.build_opener(*handlers).open(req, timeout=60) as response:
        raw = response.read()
    if not raw:
        raise LumaError("Nomad returned an empty snapshot")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".pending")
    with temporary.open("wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    LocalExecutor().run(shlex.join(["/usr/local/bin/nomad", "snapshot", "inspect", str(path)]), timeout=30)


def _payload(journal: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    name = step["node"]
    node = journal["nodes"][name]
    manager = journal["nodes"][journal["manager"]]
    kind = step["kind"]
    payload = {"schema": 1, "op": kind, "nodeId": node["nodeId"], "address": node["address"],
               "managerAddress": manager["address"], "peers": [v["address"] for n, v in journal["nodes"].items() if n != journal["manager"]]}
    if kind == "preflight":
        payload.update(journal["mounts"][name])
    elif kind == "version":
        payload["version"] = step["version"]
        # The existing node's explicit proxy is only a download transport.
        proxy = journal.get("downloadProxy") or ""
        payload["proxy"] = proxy.replace(manager["address"], "127.0.0.1") if name == journal["manager"] else proxy
    elif kind in {"rpc", "secure", "activate"}:
        secure = kind in {"secure", "activate"} or node.get("acl")
        payload.update(op="configure", version=NOMAD_VERSION, baselineSHA256=node["baselineSHA256"],
                       acl=bool(secure), tlsHTTP=bool(secure), rpcUpgradeMode=kind == "rpc" and name == journal["manager"] and not secure,
                       trustedHost=journal["mounts"][name]["trustedHost"],
                       certificate=sign_csr(node["csr"], role=node["role"], address=node["address"]))
    return payload


def _consumers(journal: dict[str, Any], *, consumers_only: bool = False) -> None:
    client = NomadApi("http://127.0.0.1:4646", token=nomad_token())
    with workload_policy_lock():
        policy = read_private_json(security_root() / "workloads.json")
        policy["mode"] = "enforce"
        private_write(security_root() / "workloads.json", json.dumps(policy))
    for item in journal["jobs"]:
        job_id = str(item["Job"]["ID"])
        if consumers_only and job_id not in {"traefik", "luma-observe"}:
            continue
        route = "/v1/job/" + urllib.parse.quote(job_id, safe="")
        current = client.request("GET", route)
        intent = item.get("consumerIntent")
        if intent:
            # Both documents were accepted from trusted sources and persisted
            # before submission. A lost response may leave either revision live.
            if not any(_matches_expected_job(current, intent[key]) for key in ("prior", "target")):
                raise LumaError("Nomad consumer changed outside its saved upgrade intent: " + job_id)
            target = copy.deepcopy(intent["target"])
        else:
            if authority_digest(current) != item["authoritySHA256"]:
                raise LumaError(f"Nomad job {job_id} changed during migration; its permissions must be inspected again")
            if item["system"]:
                if not _system_job(current):
                    raise LumaError("system consumer differs from the trusted installed renderer: " + job_id)
            else:
                validate_application(current)
            target = copy.deepcopy(current)
            prepare_job(target, enforce=True)
            if job_id in {"traefik", "luma-observe"}:
                intent = item["consumerIntent"] = {"prior": current, "target": target}
                private_write(JOURNAL, json.dumps(journal))
        if item["system"]:
            grant = {"sha256": job_digest(target), "reason": "Verified installed Luma system renderer"}
        else:
            validate_application(target)
            grant = {"authoritySHA256": authority_digest(target), "reason": "Verified saved deployment and existing data mounts", "source": "luma-renderer"}
        with workload_policy_lock():
            policy = read_private_json(security_root() / "workloads.json")
            policy.setdefault("grants", {})["default/" + job_id] = grant
            private_write(security_root() / "workloads.json", json.dumps(policy))
        # Only infrastructure consumers need a restart. Business allocations
        # keep their existing images, environment, data and deployment owner.
        if intent:
            if not _matches_expected_job(current, target):
                response = client.request("POST", "/v1/jobs", {"Job": target, "EnforceIndex": True, "JobModifyIndex": current["JobModifyIndex"]})
                if not isinstance(response, dict) or not isinstance(response.get("JobModifyIndex"), int) or response["JobModifyIndex"] < 1:
                    raise LumaError("Nomad consumer submission did not return a verifiable revision")
                current = client.request("GET", route)
                if current.get("JobModifyIndex") != response["JobModifyIndex"] or not _matches_expected_job(current, target):
                    raise LumaError("Nomad consumer does not match its submitted security revision: " + job_id)
            item["consumerModifyIndex"] = current["JobModifyIndex"]
            item["authoritySHA256"] = authority_digest(current)
            item["Job"] = current
            private_write(JOURNAL, json.dumps(journal))
            if item["system"]:
                private_write(ADMIN_ROOT / "system-jobs" / (job_id + ".json"), json.dumps({"Job": current}))
            item.pop("consumerIntent", None)
            private_write(JOURNAL, json.dumps(journal))
    # Verify discovery/logging allocations correlate to the submitted revision.
    for item in journal["jobs"]:
        if not item.get("consumerModifyIndex"):
            continue
        job_id = item["Job"]["ID"]
        current = client.request("GET", "/v1/job/" + urllib.parse.quote(job_id, safe=""))
        if current.get("JobModifyIndex") != item["consumerModifyIndex"]:
            raise LumaError("Nomad consumer changed after its security update: " + job_id)
        allocs = client.request("GET", "/v1/job/" + urllib.parse.quote(job_id, safe="") + "/allocations") or []
        expected = sum(int(g.get("Count") or 0) for g in current.get("TaskGroups") or [])
        healthy = [a for a in allocs if a.get("JobVersion") == current.get("Version") and a.get("ClientStatus") == "running" and not any(s.get("Failed") for s in (a.get("TaskStates") or {}).values())]
        if len(healthy) < expected:
            raise Pending("Waiting for authenticated " + job_id + " consumers to become healthy")
        if job_id == "luma-observe":
            metrics = {"nomad-exporter": "luma_observe_nomad_up", "log-shipper": "luma_observe_log_shipper_nomad_up"}
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            for group in current.get("TaskGroups") or []:
                for task in group.get("Tasks") or []:
                    metric = metrics.get(task.get("Name"))
                    if not metric:
                        continue
                    port = int((task.get("Env") or {}).get("LUMA_OBSERVE_PORT") or (9107 if task["Name"] == "nomad-exporter" else 9108))
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
                            text = response.read(2 * 1024 * 1024).decode()
                        if not re.search(r"^" + re.escape(metric) + r" 1(?:\.0)?$", text, re.M):
                            raise Pending("Waiting for authenticated Nomad reads from " + task["Name"])
                    except (OSError, urllib.error.URLError):
                        raise Pending("Waiting for " + task["Name"] + " readiness") from None


class Pending(Exception):
    pass


def _maintain_certificates(journal: dict[str, Any], state: Mapping[str, Any]) -> None:
    now = int(time.time())
    if now - int(journal.get("certificateCheckAt") or 0) < 3600:
        return
    manager = journal["nodes"][journal["manager"]]
    changed = expires_soon(ADMIN_ROOT / "tls") or any(expires_soon(security_root() / (role + ".d")) for role in ROLES)
    if changed or journal.get("consumerRenewalPending"):
        if not journal.get("consumerRenewalPending"):
            # Normal application updates may have changed the saved sources
            # since migration. Revalidate current consumer specs before renewal.
            jobs, _ = inventory(state, journal["nodes"])
            journal["jobs"] = [item for item in journal["jobs"] if item["Job"]["ID"] not in {"traefik", "luma-observe"}] + [item for item in jobs if item["Job"]["ID"] in {"traefik", "luma-observe"}]
            journal["consumerRenewalPending"] = True
            private_write(JOURNAL, json.dumps(journal))
        if changed:
            prepare_manager(manager["address"])
        LocalExecutor().run("systemctl reload nomad", timeout=30)
        _consumers(journal, consumers_only=True)
        journal.pop("consumerRenewalPending", None)
        private_write(JOURNAL, json.dumps(journal))
    deferred = False
    for name, node in journal["nodes"].items():
        if name == journal["manager"]:
            continue
        agent = ((state.get("nodes") or {}).get(name) or {}).get("agent") or {}
        if CAPABILITY not in (agent.get("capabilities") or []) or now - int(agent.get("lastSeen") or 0) > 120:
            deferred = True
            continue
        # Renewal uses the node's own current CSR; private keys never leave it.
        epoch = now // 86400
        inspected = control_task(state, name, f"renew-inspect:{epoch}:{name}", {"schema": 1, "op": "certificate-request"})
        if inspected.get("status") in {"failed", "timeout", "interrupted"}:
            raise LumaError("Nomad certificate inspection failed on " + name + ": " + str(inspected.get("message") or "no valid response"))
        if inspected.get("status") != "succeeded":
            raise Pending("Checking Nomad certificate lifetime on " + name)
        facts = inspected.get("result") or {}
        if facts.get("nodeId") != node["nodeId"] or facts.get("address") != node["address"]:
            raise LumaError("certificate renewal does not match the enrolled node identity")
        if facts.get("certificateRenewalRequired"):
            renewal = journal.setdefault("renewals", {}).get(name)
            if renewal is None or renewal.get("epoch") != epoch:
                renewal = {"epoch": epoch, "certificate": sign_csr(facts["csr"], role="client", address=node["address"])}
                journal["renewals"][name] = renewal
                private_write(JOURNAL, json.dumps(journal))
            response = control_task(state, name, f"renew:{epoch}:{name}", {"schema": 1, "op": "renew", "nodeId": node["nodeId"], "address": node["address"], "certificate": renewal["certificate"]})
            if response.get("status") in {"failed", "timeout", "interrupted"}:
                raise LumaError("Nomad certificate renewal failed on " + name + ": " + str(response.get("message") or "no valid response"))
            if response.get("status") != "succeeded":
                raise Pending("Renewing Nomad certificate on " + name)
    journal["certificateCheckAt"] = now - 3540 if deferred else now
    private_write(JOURNAL, json.dumps(journal))


def reconcile_once() -> dict[str, Any]:
    with _journal_lock():
        return _reconcile_locked()


def _reconcile_locked() -> dict[str, Any]:
    journal = read_private_json(JOURNAL)
    if journal.get("status") == "failed":
        return public_status()
    state = _state()
    registered, waiting = registered_nodes(state, journal["managerId"])
    if journal.get("status") == "completed":
        expected = {node["nodeId"] for name, node in journal["nodes"].items() if name != journal["manager"]}
        if {node["nodeId"] for node in registered.values()} != expected:
            manager = journal["manager"]
            journal.update(nodes={manager: snapshot()}, steps=[], next=0, attempt=int(journal["attempt"]) + 1)
            journal.pop("verifiedAt", None)
            _save(journal, status="pending", phase="membership", message="Verifying newly registered Nomad nodes")
        else:
            try:
                _maintain_certificates(journal, state)
            except Pending:
                pass
            except LumaError as exc:
                _save(journal, status="failed", phase="certificates", message=str(exc))
            return public_status()
    if waiting:
        _save(journal, status="running" if journal.get("steps") else "pending", phase="agents", message="Waiting for upgraded, connected node agents: " + ", ".join(waiting), waiting=waiting)
        return public_status()
    if (state.get("controlMaintenance") or {}).get("status") == "held" or (not journal.get("steps") and any(r.get("status") in {"running", "finalizing"} for r in (state.get("buildRuns") or {}).values() if isinstance(r, dict))):
        _save(journal, status="running" if journal.get("steps") else "pending", phase=journal.get("phase") if journal.get("steps") else "maintenance", message="Waiting for the current manager update and active builds before migrating Nomad")
        return public_status()
    try:
        if not journal.get("steps"):
            _require_single_server()
            for name, expected in registered.items():
                if name in journal["nodes"]:
                    continue
                response = control_task(state, name, f"{journal['id']}:inspect:{name}:{journal['attempt']}", {"schema": 1, "op": "inspect"})
                if response.get("status") in {"failed", "timeout", "interrupted"}:
                    raise LumaError(str(response.get("message") or "Node security preflight failed"))
                if response.get("status") != "succeeded":
                    raise Pending("Inspecting Nomad configuration on " + name)
                node = response.get("result") or {}
                if node.get("nodeId") != expected["nodeId"] or node.get("address") != expected["address"] or node.get("role") != "client":
                    raise LumaError("agent security evidence does not match the registered node " + name)
                journal["nodes"][name] = node
                _save(journal, status="pending", phase="inspect", message="Inspected " + name)
            local = snapshot()
            journal["nodes"][journal["manager"]] = local
            journal["jobs"], journal["mounts"] = inventory(state, journal["nodes"])
            journal["downloadProxy"] = "http://" + local["address"] + ":7890" if any(item["Job"]["ID"] == "egress" and item["system"] for item in journal["jobs"]) else ""
            journal["steps"] = migration_steps(journal["nodes"], journal["manager"])
            _save(journal, status="running", phase="preflight", message="Verifying recorded workloads and host storage before the security migration")
        expected_ids = {n["nodeId"] for name, n in journal["nodes"].items() if name != journal["manager"]}
        if {n["nodeId"] for n in registered.values()} != expected_ids:
            raise LumaError("registered Nomad nodes changed during migration; rerun the manager update after reviewing membership")
        position = int(journal.get("next") or 0)
        if position == len(journal["steps"]):
            checks = verify_acl()
            if not checks["ok"]:
                raise LumaError("final ACL role verification failed")
            cluster = read_private_json(security_root() / "cluster.json")
            cluster.update(tlsRequired=True, nomadVersion=NOMAD_VERSION, securityVerifiedAt=int(time.time()))
            private_write(security_root() / "cluster.json", json.dumps(cluster))
            journal["verifiedAt"] = int(time.time())
            _save(journal, status="completed", phase="verified", message="All registered nodes passed Nomad version, ACL, mTLS, driver and network checks")
            return public_status()
        step = journal["steps"][position]
        kind, name = step["kind"], step["node"]
        _save(journal, status="running", phase=kind, message=f"{kind}: {name}" + (" → " + step["version"] if step.get("version") else ""))
        if kind == "consumers":
            _consumers(journal)
            result = {}
        else:
            if "taskPayload" not in journal:
                journal["taskPayload"] = _payload(journal, step)
                private_write(JOURNAL, json.dumps(journal))
            payload = journal["taskPayload"]
            if name == journal["manager"]:
                recovered = resume_operation(allow_server=True, payload=payload)
                if kind == "activate":
                    # A lost response after the ACL restart must reenter the
                    # saved bootstrap, never reset ACLs or revert the binary.
                    if recovered is None:
                        transport = read_private_json(security_root() / "transport.json", optional=True)
                        if not transport.get("http"):
                            _raft_snapshot("before-acl")
                        execute(payload, allow_server=True)
                    try:
                        activate_acl()
                    except LumaError as exc:
                        if any(word in str(exc).lower() for word in ("unavailable", "no cluster leader", "connection refused", "error 500", "error 503")):
                            raise Pending("Waiting for the restarted Nomad server to activate its saved ACL credentials") from None
                        raise
                    result = snapshot()
                else:
                    if recovered is None and kind == "version":
                        _raft_snapshot("before-" + step["version"])
                    result = recovered if recovered is not None else execute(payload, allow_server=True)
            else:
                key = f"{journal['id']}:{position}:{journal['attempt']}"
                response = control_task(state, name, key, payload)
                if response.get("status") in {"failed", "timeout", "interrupted"}:
                    raise LumaError(str(response.get("message") or "Node security step failed"))
                if response.get("status") != "succeeded":
                    raise Pending("Waiting for " + kind + " on " + name)
                result = response.get("result") or {}
        if kind == "verify":
            journal["nodes"][name]["verification"] = result
        elif result.get("baselineSHA256"):
            journal["nodes"][name].update(result)
        journal.pop("taskPayload", None)
        journal["next"] = position + 1
        _save(journal, status="running", phase=kind, message="Verified step: " + kind + " on " + name)
    except Pending as exc:
        _save(journal, status="pending" if not journal.get("steps") else "running", phase=journal.get("phase") or "inspect", message=str(exc))
    except LumaError as exc:
        transient = "Control is unavailable" in str(exc) or "HTTP 50" in str(exc)
        _save(journal, status="running" if transient and journal.get("steps") else "pending" if transient else "failed", phase=journal.get("phase") or "inspect", message=str(exc))
    return public_status()


def run() -> None:
    # One supervisor owns the journal, including after an installer restart.
    fd = os.open(ADMIN_ROOT / "supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.environ["LUMA_NOMAD_UPGRADE_OWNER"] = "1"

    def enrollment_loop():
        while True:
            try:
                sign_enrollments(_state())
            except Exception:
                # Certificate requests time out explicitly at Control. Keep
                # this independent of slow downloads and node restarts.
                pass
            time.sleep(1)

    threading.Thread(target=enrollment_loop, name="nomad-enrollment", daemon=True).start()
    while True:
        try:
            reconcile_once()
        except Exception as exc:
            with _journal_lock():
                journal = read_private_json(JOURNAL, optional=True)
                _save(journal, status="failed", phase=journal.get("phase") or "supervisor", message="Security supervisor failed: " + type(exc).__name__ + "; rerun the manager update to resume")
        time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "install", "run"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise LumaError("Nomad manager security supervisor requires root")
        if args.command == "prepare":
            print(json.dumps(prepare()))
        elif args.command == "install":
            install_supervisor()
            print("Automatic Nomad security migration supervisor installed")
        else:
            run()
        return 0
    except LumaError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
