"""Independent manager supervisor for automatic Nomad security upgrades.

Installed by bootstrap / `luma update manager`. It waits for upgraded agents,
persists every step, and outlives Control and Nomad restarts. Nothing runs on
module import. The CA and administrator token stay outside Control's mount.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
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
from .nomad_api import NomadApi, _allocation_health, _desired_group_counts_from_job
from .nomad_migration_node import CAPABILITY, DOWNLOAD_CAPABILITY, execute, resume_operation, snapshot, version_path
from .nomad_pki import client_tls, endpoint, expires_soon, prepare_manager, sign_csr, sign_enrollments
from .nomad_security import ADMIN_ROOT, NOMAD_RELEASES, NOMAD_VERSION, ROLES, authority_digest, job_digest, nomad_token, prepare_job, private_read, private_write, read_private_json, security_root, validate_application, workload_policy_lock

JOURNAL = ADMIN_ROOT / "upgrade.json"
WORKLOAD_STABLE_SECONDS = 30
WORKLOAD_RECOVERY_SECONDS = 600
WORKLOAD_STEPS = {"version", "rpc", "activate", "secure", "privacy", "history", "consumers", "secrets"}
DOWNLOAD_PREFETCH_NODES = 3


def _read_journal(*, optional: bool = False) -> dict[str, Any]:
    from .secret_storage import context, loads
    raw = private_read(JOURNAL, optional=optional)
    if not raw and optional:
        return {}
    value = loads(raw, context("nomad-upgrade"))
    if not isinstance(value, dict):
        raise LumaError("invalid Nomad upgrade journal")
    return value


def _write_journal(journal: Mapping[str, Any]) -> None:
    from .secret_storage import context, dumps
    private_write(JOURNAL, dumps(journal, context("nomad-upgrade")) + "\n")


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
    _write_journal(journal)
    steps = journal.get("steps") or []
    public = {key: journal.get(key) for key in ("status", "phase", "message", "updatedAt", "id", "verifiedAt") if journal.get(key) is not None}
    public.update(targetVersion=NOMAD_VERSION, completedSteps=int(journal.get("next") or 0), totalSteps=len(steps), waitingNodes=waiting or [])
    public["nodes"] = [{"node": name, "version": node.get("version"), "verified": bool(node.get("verification"))} for name, node in (journal.get("nodes") or {}).items()]
    secrets = journal.get("secretMigration") or {}
    public["secrets"] = {"encryptedStorage": bool(secrets.get("databaseEncrypted")),
                         "taskIdentitiesVerified": bool(secrets.get("verifiedAt")), "verifiedAt": secrets.get("verifiedAt")}
    public["downloads"] = journal.get("downloads") or []
    private_write(security_root() / "upgrade-status.json", json.dumps(public))


def _state() -> dict[str, Any]:
    from .control.state import load_state
    return load_state()


def prepare() -> dict[str, Any]:
    """Called by the upgraded host CLI before refreshing consumer jobs."""
    with _journal_lock():
        return _prepare_locked()


def _prepare_locked() -> dict[str, Any]:
    previous = _read_journal(optional=True)
    recovered = resume_operation(allow_server=True, payload=previous.get("taskPayload"))
    if recovered and recovered.get("aclBootstrapPending"):
        activate_acl()
    try:
        facts = snapshot()
    except LumaError:
        previous = _read_journal(optional=True)
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
    journal = _read_journal(optional=True)
    if not journal:
        journal = {"schema": 1, "id": "nomad-security-" + str(int(time.time())), "manager": facts["node"], "managerId": facts["nodeId"], "attempt": 0, "nodes": {facts["node"]: facts}, "next": 0}
    elif journal.get("managerId") != facts["nodeId"]:
        raise LumaError("the security journal belongs to a different Nomad server identity")
    if journal.get("status") == "completed" and not (journal.get("secretMigration") or {}).get("verifiedAt"):
        journal.update(nodes={facts["node"]: facts}, next=0, steps=[], status="pending")
        journal.pop("verifiedAt", None)
    elif journal.get("steps") and not any(step.get("kind") == "secrets" for step in journal["steps"]):
        index = next((i for i, step in enumerate(journal["steps"]) if step.get("kind") == "verify"), len(journal["steps"]))
        additions = [{"kind": "privacy", "node": name} for name in journal["nodes"]]
        additions += [{"kind": "secrets", "node": journal["manager"]}, {"kind": "history", "node": journal["manager"]}]
        journal["steps"][index:index] = additions
        journal["next"] = min(int(journal.get("next") or 0), index)
    if journal.get("status") == "failed":
        journal["attempt"] = int(journal.get("attempt") or 0) + 1
        journal.pop("workloadHealth", None)
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
    if (job.get("Meta") or {}).get("luma.secrets.version") != (expected.get("Meta") or {}).get("luma.secrets.version"):
        return False
    expected_tasks = {(g["Name"], t["Name"]): t for g in expected.get("TaskGroups") or [] for t in g.get("Tasks") or []}
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            original = expected_tasks.get((group["Name"], task["Name"])) or {}
            if (original.get("Env") or {}) != (task.get("Env") or {}):
                return False
            def secret_bindings(value):
                return [{"Name": item.get("Name"), "Provider": item.get("Provider"), "Path": item.get("Path"), "Config": item.get("Config") or {}, "Env": item.get("Env") or {}} for item in value or []]
            if secret_bindings(original.get("Secrets")) != secret_bindings(task.get("Secrets")):
                return False
            if (original.get("Identity") or {}).get("Env", False) != (task.get("Identity") or {}).get("Env", False) or (original.get("Identity") or {}).get("File", False) != (task.get("Identity") or {}).get("File", False):
                return False
            for key in ("image", "command", "args", "entrypoint", "work_dir", "auth"):
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
        if _matches_expected_job(job, expected):
            return True
        # Existing egress installations may use Docker's volume shorthand for
        # this same fixed config bind. Reconstruct that exact profile from the
        # installed renderer, never from an untrusted live path or argument.
        legacy = copy.deepcopy(expected)
        cfg = legacy["TaskGroups"][0]["Tasks"][0]["Config"]
        mount = cfg.pop("mount")[0]
        cfg["volumes"] = [mount["source"] + ":" + mount["target"]]
        return _matches_expected_job(job, legacy)
    return bool(expected) and _matches_expected_job(job, expected)


def _require_single_server() -> None:
    # Raft inspection belongs to the host administrator. The Control token
    # deliberately has no operator access once ACLs have been activated.
    token = private_read(ADMIN_ROOT / "management.token", optional=True).strip() or nomad_token()
    client = NomadApi("http://127.0.0.1:4646", token=token)
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
        if not system and (source is None or not _matches_expected_job(job, source)):
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
    else:
        steps += [{"kind": "privacy", "node": name} for name in order
                  if name == manager or not nodes[name].get("isolatedHostEnvironment")]
    steps.append({"kind": "consumers", "node": manager})
    steps.append({"kind": "secrets", "node": manager})
    steps.append({"kind": "history", "node": manager})
    steps += [{"kind": "verify", "node": name} for name in order]
    return steps


def _raft_snapshot(label: str) -> None:
    _require_single_server()
    path = ADMIN_ROOT / "backups" / label / "raft.snap"
    if path.exists() and path.stat().st_size:
        LocalExecutor().run(shlex.join(["/usr/local/bin/nomad", "operator", "snapshot", "inspect", str(path)]), timeout=30)
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
    LocalExecutor().run(shlex.join(["/usr/local/bin/nomad", "operator", "snapshot", "inspect", str(path)]), timeout=30)


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
    elif kind in {"rpc", "secure", "activate", "privacy", "history"}:
        secure = kind in {"secure", "activate", "privacy", "history"} or node.get("acl")
        payload.update(op="configure", version=NOMAD_VERSION, baselineSHA256=node["baselineSHA256"],
                       acl=bool(secure), tlsHTTP=bool(secure), rpcUpgradeMode=kind == "rpc" and name == journal["manager"] and not secure,
                       trustedHost=journal["mounts"][name]["trustedHost"],
                       secretMigration=kind != "history",
                       certificate=sign_csr(node["csr"], role=node["role"], address=node["address"]))
    return payload


def _prefetch_downloads(journal: dict[str, Any], state: Mapping[str, Any]) -> None:
    """Prepare a bounded window of clients; activation remains server-first and serial."""
    selected: set[str] = set()
    downloads = []
    for step in journal["steps"][int(journal.get("next") or 0):]:
        name = step["node"]
        if step["kind"] != "version" or name == journal["manager"] or name in selected:
            continue
        agent = ((state.get("nodes") or {}).get(name) or {}).get("agent") or {}
        if DOWNLOAD_CAPABILITY not in (agent.get("capabilities") or []):
            continue
        selected.add(name)
        node = journal["nodes"][name]
        payload = {"schema": 1, "op": "preflight", "nodeId": node["nodeId"], "address": node["address"],
                   "downloadVersion": step["version"], "proxy": journal.get("downloadProxy") or ""}
        key = f"{journal['id']}:download:{name}:{step['version']}:{journal['attempt']}"
        try:
            response = control_task(state, name, key, payload)
            status = str(response.get("status") or "queued")
        except LumaError:
            # Optional preparation cannot complete or replay a node upgrade.
            # The version step still verifies/downloads its own pinned archive.
            status = "unavailable"
        downloads.append({"node": name, "version": step["version"], "status": status})
        if len(selected) == DOWNLOAD_PREFETCH_NODES:
            break
    journal["downloads"] = downloads


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
                _write_journal(journal)
        if item["system"]:
            grant = {"sha256": job_digest(target), "reason": "Verified installed Luma system renderer"}
        else:
            validate_application(target)
            grant = {"authoritySHA256": authority_digest(target), "reason": "Verified saved deployment and existing data mounts", "source": "luma-renderer"}
        with workload_policy_lock():
            policy = read_private_json(security_root() / "workloads.json")
            policy.setdefault("grants", {})["default/" + job_id] = grant
            private_write(security_root() / "workloads.json", json.dumps(policy))
        # This step updates infrastructure consumers. The following secret
        # migration replaces business allocations after preserving their data.
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
            _write_journal(journal)
            if item["system"]:
                private_write(ADMIN_ROOT / "system-jobs" / (job_id + ".json"), json.dumps({"Job": current}))
            item.pop("consumerIntent", None)
            _write_journal(journal)
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
                    from .nomad_secrets import task_environment
                    environment = task_environment(client, current, group, task)
                    port = int(environment.get("LUMA_OBSERVE_PORT") or (9107 if task["Name"] == "nomad-exporter" else 9108))
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
                            text = response.read(2 * 1024 * 1024).decode()
                        if not re.search(r"^" + re.escape(metric) + r" 1(?:\.0)?$", text, re.M):
                            raise Pending("Waiting for authenticated Nomad reads from " + task["Name"])
                    except (OSError, urllib.error.URLError):
                        raise Pending("Waiting for " + task["Name"] + " readiness") from None


class Pending(Exception):
    pass


def _workload_health(journal: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Read current service health without modifying jobs or exposing check output."""
    jobs = [item["Job"] for item in journal.get("jobs") or []
            if item["Job"].get("Type") == "service" and not item["Job"].get("Stop")]
    if not jobs:
        return "", []
    client = NomadApi("http://127.0.0.1:4646", token=nomad_token())
    evidence, unhealthy = [], []
    for saved in jobs:
        job_id = str(saved["ID"])
        route = "/v1/job/" + urllib.parse.quote(job_id, safe="")
        try:
            current = client.request("GET", route, timeout=5)
            if current.get("Stop") or current.get("Type") != "service":
                unhealthy.append(job_id + ": service stopped or changed type")
                continue
            expected = _desired_group_counts_from_job(current, slug=job_id)
            groups = {g["Name"]: g for g in current["TaskGroups"]}
            lifecycles = {name: {t["Name"]: t.get("Lifecycle") or {} for t in g.get("Tasks") or []}
                          for name, g in groups.items()}
            allocations = client.request("GET", route + "/allocations", timeout=5) or []
            healthy, _, detail = _allocation_health(allocations, target_version=current["Version"],
                                                    expected_groups=expected, task_lifecycles=lifecycles)
            if not healthy:
                unhealthy.append(job_id + ": " + detail)
                continue
            evidence.append([job_id, current.get("JobModifyIndex"), current["Version"], expected])
            for allocation in sorted(allocations, key=lambda a: a.get("ID", "")):
                if (allocation.get("JobVersion") != current["Version"] or allocation.get("ClientStatus") != "running"
                        or allocation.get("DesiredStatus", "run") not in {"run", "running"}):
                    continue
                group = groups[allocation["TaskGroup"]]
                tasks = allocation.get("TaskStates") or {}
                if not set(lifecycles[group["Name"]]).issubset(tasks):
                    unhealthy.append(job_id + ": incomplete task health")
                    continue
                # Restart counters and start times prevent a crash/restart
                # between polls from counting toward the stable interval.
                evidence.append([allocation["ID"], allocation.get("NodeID"),
                                 {name: {key: task.get(key) for key in ("State", "StartedAt", "Restarts", "LastRestart")}
                                  for name, task in tasks.items()}])
                services = list(group.get("Services") or []) + [s for t in group.get("Tasks") or [] for s in t.get("Services") or []]
                checks = [check for service in services if service.get("Provider") == "nomad" for check in service.get("Checks") or []]
                if checks:
                    results = client.request("GET", "/v1/client/allocation/" + allocation["ID"] + "/checks", timeout=5) or {}
                    max_age = max(30, *(2 * (int(c.get("Interval") or 0) + int(c.get("Timeout") or 0)) / 1e9 for c in checks))
                    now = time.time()
                    if (len(results) != len(checks) or any(
                            check.get("Status") != "success" or not -5 <= now - float(check.get("Timestamp") or 0) <= max_age
                            for check in results.values())):
                        unhealthy.append(job_id + ": Nomad service checks are failing, missing or stale")
        except LumaError:
            # Check output and API bodies may contain application secrets.
            unhealthy.append(job_id + ": health query unavailable")
            break
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(), unhealthy


def _wait_for_workloads(journal: dict[str, Any], *, after: bool) -> None:
    position = int(journal.get("next") or 0)
    stage = str(position) + (":after" if after else ":before")
    now = time.time()
    health = journal.get("workloadHealth") or {}
    if health.get("stage") != stage:
        health = journal["workloadHealth"] = {"stage": stage, "startedAt": now}
    fingerprint, unhealthy = _workload_health(journal)
    now = time.time()
    if unhealthy:
        health.pop("healthySince", None)
        health.pop("fingerprint", None)
        message = "Waiting for healthy workloads: " + "; ".join(unhealthy[:6])
    elif not fingerprint or not after:
        journal.pop("workloadHealth", None)
        return
    else:
        if (health.get("fingerprint") != fingerprint or now < float(health.get("lastObservedAt") or 0)
                or now - float(health.get("lastObservedAt") or 0) > 2 * WORKLOAD_STABLE_SECONDS):
            health["healthySince"] = now
        health.update(fingerprint=fingerprint, lastObservedAt=now)
        elapsed = now - health["healthySince"]
        if elapsed >= WORKLOAD_STABLE_SECONDS:
            journal.pop("workloadHealth", None)
            return
        message = f"Waiting for stable workloads after node changes: {int(elapsed)}/{WORKLOAD_STABLE_SECONDS}s"
    if now - health["startedAt"] >= WORKLOAD_RECOVERY_SECONDS:
        raise LumaError("Workloads did not recover within 10 minutes; further node changes stopped. " + message)
    raise Pending(message)


def _repair_rpc_transition(journal: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Resume old server transitions that enforced client certificates too early."""
    if not result.get("rpcUpgradeMode"):
        return result
    position = int(journal["next"])
    repair = journal.get("rpcTransitionRepair") or {}
    if repair and repair.get("position") != position:
        raise LumaError("saved RPC transition recovery does not match the migration position")
    if repair.get("status") == "completed" or result.get("verifyServerHostname") is False:
        return result
    if not repair:
        facts = snapshot()
        if facts["baselineSHA256"] != result["baselineSHA256"]:
            raise LumaError("RPC transition recovery requires the unchanged pre-ACL server configuration")
        if not facts.get("rpcUpgradeMode") or not facts.get("verifyServerHostname"):
            return result
        if (facts.get("role") != "server" or facts.get("acl") or facts.get("tlsHTTP")
                or not facts.get("tlsRPC")):
            raise LumaError("RPC transition recovery requires the unchanged pre-ACL server configuration")
        payload = {**journal["taskPayload"], "baselineSHA256": facts["baselineSHA256"], "rpcCompatibilityVersion": 1}
        repair = journal["rpcTransitionRepair"] = {
            "position": position, "status": "prepared", "startedAt": int(time.time()),
            "request": payload, "priorResult": result, "priorHealth": journal.get("workloadHealth"),
        }
        _write_journal(journal)
    # The normal host intent validates hashes and survives a crash after either
    # writing the config or restarting. Preserve the original step's payload.
    repaired = execute(repair["request"], allow_server=True)
    if repaired.get("verifyServerHostname") is not False or not repaired.get("rpcUpgradeMode"):
        raise LumaError("RPC transition compatibility was not applied")
    journal["stepResult"]["result"] = repaired
    repair.update(status="completed", completedAt=int(time.time()))
    journal["workloadHealth"] = {"stage": str(position) + ":after", "startedAt": time.time()}
    _write_journal(journal)
    return repaired


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
            _write_journal(journal)
        if changed:
            prepare_manager(manager["address"])
        LocalExecutor().run("systemctl reload nomad", timeout=30)
        _consumers(journal, consumers_only=True)
        journal.pop("consumerRenewalPending", None)
        _write_journal(journal)
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
                _write_journal(journal)
            response = control_task(state, name, f"renew:{epoch}:{name}", {"schema": 1, "op": "renew", "nodeId": node["nodeId"], "address": node["address"], "certificate": renewal["certificate"]})
            if response.get("status") in {"failed", "timeout", "interrupted"}:
                raise LumaError("Nomad certificate renewal failed on " + name + ": " + str(response.get("message") or "no valid response"))
            if response.get("status") != "succeeded":
                raise Pending("Renewing Nomad certificate on " + name)
    journal["certificateCheckAt"] = now - 3540 if deferred else now
    _write_journal(journal)


def reconcile_once() -> dict[str, Any]:
    with _journal_lock():
        return _reconcile_locked()


def _reconcile_locked() -> dict[str, Any]:
    journal = _read_journal()
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
            _wait_for_workloads(journal, after=True)
            checks = verify_acl()
            if not checks["ok"]:
                raise LumaError("final ACL role verification failed")
            if not (journal.get("secretMigration") or {}).get("verifiedAt"):
                raise LumaError("encrypted storage and application secret identities have not completed verification")
            cluster = read_private_json(security_root() / "cluster.json")
            cluster.update(tlsRequired=True, nomadVersion=NOMAD_VERSION, securityVerifiedAt=int(time.time()))
            private_write(security_root() / "cluster.json", json.dumps(cluster))
            journal["verifiedAt"] = int(time.time())
            _save(journal, status="completed", phase="verified", message="All nodes, encrypted Control storage, task secret identities and legacy job API cleanup passed verification")
            return public_status()
        step = journal["steps"][position]
        kind, name = step["kind"], step["node"]
        if kind == "version":
            _prefetch_downloads(journal, state)
        else:
            journal.pop("downloads", None)
        _save(journal, status="running", phase=kind, message=f"{kind}: {name}" + (" → " + step["version"] if step.get("version") else ""))
        completed = journal.get("stepResult")
        if completed is not None and completed.get("position") != position:
            raise LumaError("saved Nomad step result does not match the migration position")
        if kind in WORKLOAD_STEPS and completed is None and "taskPayload" not in journal:
            _wait_for_workloads(journal, after=False)
        if completed is not None:
            result = completed["result"]
        elif kind == "consumers":
            _consumers(journal)
            result = {}
        elif kind == "secrets":
            from .secret_migration import migrate
            result = migrate(journal, state)
        else:
            if "taskPayload" not in journal:
                journal["taskPayload"] = _payload(journal, step)
                _write_journal(journal)
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
        if completed is None:
            # Persist completion before waiting. Resuming health verification
            # must not repeat downloads, restarts or consumer submissions.
            journal["stepResult"] = {"position": position, "result": result}
            _write_journal(journal)
        if kind == "rpc" and name == journal["manager"]:
            result = _repair_rpc_transition(journal, result)
        if kind == "verify":
            journal["nodes"][name]["verification"] = result
        elif result.get("baselineSHA256"):
            journal["nodes"][name].update(result)
        if kind in WORKLOAD_STEPS:
            _wait_for_workloads(journal, after=True)
        journal.pop("taskPayload", None)
        journal.pop("stepResult", None)
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
                journal = _read_journal(optional=True)
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
