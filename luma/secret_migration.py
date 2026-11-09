"""Resumable conversion of an existing cluster to encrypted task-bound secrets.

Runs only in the independent manager supervisor, after every node has upgraded
and ACL/mTLS are active. No live operations occur on import.
"""
from __future__ import annotations

import copy
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import LumaError, NomadUpgradePending
from .nomad_api import NomadApi, _allocation_health, _desired_group_counts_from_job
from .nomad_secrets import _status, assert_protected_job, protect_job, prune_job_variables, validate_workload_acl
from .nomad_security import ADMIN_ROOT, authority_digest, job_digest, nomad_token, prepare_job, private_read, private_write, read_private_json, security_root, workload_policy_lock
from .secret_storage import context, dumps, enabled, encrypt_file, provision, verify_archive


def _client(*, administrator: bool = False) -> NomadApi:
    token = private_read(ADMIN_ROOT / "management.token").strip() if administrator else nomad_token()
    return NomadApi("http://127.0.0.1:4646", token=token)


def _save(journal: dict[str, Any]) -> None:
    from .nomad_upgrade import _write_journal
    _write_journal(journal)


def _pending(message: str) -> None:
    raise NomadUpgradePending(message)


def _protected(job: Mapping[str, Any]) -> bool:
    try:
        assert_protected_job(job)
        return True
    except LumaError:
        return False


def _private_backup(journal: dict[str, Any], client: NomadApi) -> None:
    from .control import database
    directory = ADMIN_ROOT / "backups" / (journal["id"] + "-secrets")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_copy = directory / "control.sqlite3"
    encrypted = Path(str(database_copy) + ".enc")
    if encrypted.exists():
        verify_archive(encrypted)
    else:
        if not database_copy.exists():
            database.backup_database(database_copy)
        encrypt_file(database_copy, encrypted)
    database_copy.unlink(missing_ok=True)
    history = directory / "nomad-jobs.encrypted.json"
    if not history.exists():
        jobs = []
        for stub in client.request("GET", "/v1/jobs?namespace=*") or []:
            namespace, name = str(stub.get("Namespace") or "default"), str(stub["ID"])
            route = "/v1/job/" + urllib.parse.quote(name, safe="") + "/versions?namespace=" + urllib.parse.quote(namespace, safe="")
            try:
                versions = client.request("GET", route)
            except LumaError as exc:
                if _status(exc) == 404:
                    continue
                raise
            jobs.append({"namespace": namespace, "id": name, "history": versions})
        private_write(history, dumps(jobs, context("nomad-history-backup"), required=True) + "\n")
    journal["secretMigration"]["backup"] = str(directory)
    _save(journal)


def _system_secrets(job: dict[str, Any]) -> None:
    """Move the legacy DNS token file into the task's encrypted Variable."""
    if job.get("ID") != "traefik":
        return
    from .bootstrap import TRAEFIK_DNS_TOKEN_FILE
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            config = task.get("Config") or {}
            mounts = config.get("mount") or []
            secret_mounts = [mount for mount in mounts if mount.get("source") == TRAEFIK_DNS_TOKEN_FILE]
            if secret_mounts:
                from .secret_storage import read_credential_file
                value = read_credential_file(Path(TRAEFIK_DNS_TOKEN_FILE)).strip()
                if not value:
                    raise LumaError("Traefik DNS credential is empty; migration did not remove its mount")
                environment = task.setdefault("Env", {})
                environment.pop("CF_DNS_API_TOKEN_FILE", None)
                environment["CF_DNS_API_TOKEN"] = value
                config["mount"] = [mount for mount in mounts if mount not in secret_mounts]


def _grant(job: Mapping[str, Any], *, system: bool) -> None:
    value = ({"sha256": job_digest(job), "reason": "Verified Luma system job with task-bound secrets"} if system else
             {"authoritySHA256": authority_digest(job), "reason": "Verified application and unchanged storage during secret migration", "source": "luma-renderer"})
    with workload_policy_lock():
        policy = read_private_json(security_root() / "workloads.json")
        policy.setdefault("grants", {})[str(job.get("Namespace") or "default") + "/" + str(job["ID"])] = value
        private_write(security_root() / "workloads.json", json.dumps(policy))


def _artifact_paths(state: Mapping[str, Any], job_id: str) -> list[Path]:
    from .config import load_config
    config_path = Path(os.environ.get("LUMA_CONTROL_CONFIG") or "/opt/luma/luma.yaml")
    config = load_config(config_path)
    root = config.stack_root
    if not root.is_absolute():
        root = config_path.parent / root
    paths = []
    for kind in ("services", "compose"):
        record = ((state.get("deployments") or {}).get(kind) or {}).get(job_id)
        if not isinstance(record, dict):
            continue
        spec = yaml.safe_load(record.get("manifest") or "") or {}
        region = str(spec.get("region") or record.get("region") or "")
        path = root / "compose" / job_id / (job_id + ".nomad.json") if kind == "compose" else root / region / job_id / (job_id + ".nomad.json")
        if spec.get("stackPath"):
            path = Path(spec["stackPath"])
            if not path.is_absolute():
                path = config_path.parent / path
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise LumaError("secret migration artifact escapes the managed stack directory")
        paths.append(path)
    return paths


def _write_artifacts(state: Mapping[str, Any], current: dict[str, Any], *, system: bool) -> None:
    document = json.dumps({"Job": current}, indent=2) + "\n"
    for path in _artifact_paths(state, str(current["ID"])):
        if path.exists():
            previous = json.loads(path.read_text()).get("Job") or {}
            if not _protected(previous):
                encrypt_file(path, Path(str(path) + ".before-secrets.enc"))
        private_write(path, document)
    if system:
        private_write(ADMIN_ROOT / "system-jobs" / (str(current["ID"]) + ".json"), document)


def _retain_revision(target: dict[str, Any], version: int) -> None:
    # With job_tracked_versions=1, Nomad can evict a new, not-yet-stable
    # revision in favor of the previous stable one. Attach the tag in the
    # registration itself: tagging it in a later request is already too late.
    target["VersionTag"] = {"Name": "luma-secret-migration-" + str(version),
                            "Description": "Retain protected job during encrypted-secret migration",
                            "TaggedTime": time.time_ns()}


def _untagged_digest(job: Mapping[str, Any]) -> str:
    document = dict(job)
    document.pop("VersionTag", None)
    return job_digest(document)


def _wait_for_job(current: Mapping[str, Any], client: NomadApi, submitted_at: int) -> None:
    job_id = str(current["ID"])
    expected = _desired_group_counts_from_job(current, slug=job_id)
    allocations = client.request("GET", "/v1/job/" + urllib.parse.quote(job_id, safe="") + "/allocations") or []
    lifecycles = {group["Name"]: {task["Name"]: task.get("Lifecycle") or {} for task in group.get("Tasks") or []} for group in current["TaskGroups"]}
    healthy, _, _ = _allocation_health(allocations, target_version=current["Version"], expected_groups=expected, task_lifecycles=lifecycles)
    if not healthy:
        if time.time() - submitted_at > 2700:
            raise LumaError("application did not become healthy after its secret migration: " + job_id)
        _pending("Waiting for task-bound secrets and healthy allocations: " + job_id)


def _upgrade_job(journal: dict[str, Any], item: dict[str, Any], state: Mapping[str, Any], client: NomadApi) -> None:
    from .nomad_upgrade import _matches_expected_job
    job_id = str(item["Job"]["ID"])
    route = "/v1/job/" + urllib.parse.quote(job_id, safe="")
    current = client.request("GET", route)
    intent = item.get("secretIntent")
    if not intent:
        if authority_digest(current) != item["authoritySHA256"]:
            raise LumaError("application authority changed during secret migration: " + job_id)
        target = copy.deepcopy(current)
        target.pop("VersionTag", None)
        _system_secrets(target)
        prepare_job(target, enforce=True)
        protect_job(target, client, preserve_runtime=True)
        assert_protected_job(target)
        # Never automatically roll a failed migration back to an inline-secret
        # job. Subsequent normal deployments can use protected rollback again.
        if isinstance(target.get("Update"), dict):
            target["Update"]["AutoRevert"] = False
        for group in target.get("TaskGroups") or []:
            if isinstance(group.get("Update"), dict):
                group["Update"]["AutoRevert"] = False
        intent = item["secretIntent"] = {"prior": current, "target": target, "submittedAt": int(time.time())}
        _save(journal)
    target = intent["target"]
    if not any(_matches_expected_job(current, intent[name]) for name in ("prior", "target")):
        raise LumaError("application changed outside its saved secret migration: " + job_id)
    if not _matches_expected_job(current, target):
        _retain_revision(target, int(current["Version"]) + 1)
        _save(journal)
        _grant(target, system=bool(item["system"]))
        response = client.request("POST", "/v1/jobs", {"Job": target, "EnforceIndex": True, "JobModifyIndex": current["JobModifyIndex"],
                                                        "Submission": {"Source": "", "Format": "json", "Variables": "", "VariableFlags": {}}})
        if not isinstance(response, dict) or not int(response.get("JobModifyIndex") or 0):
            raise LumaError("secret migration did not receive a verifiable job revision")
        intent["modifyIndex"] = response["JobModifyIndex"]
        intent["submittedAt"] = int(time.time())
        _save(journal)
        current = client.request("GET", route)
    if not _matches_expected_job(current, target):
        raise LumaError("application does not match its submitted secret revision: " + job_id)
    _wait_for_job(current, client, int(intent.get("submittedAt") or time.time()))
    _write_artifacts(state, current, system=bool(item["system"]))
    _grant(current, system=bool(item["system"]))
    item.update(Job=current, authoritySHA256=authority_digest(current), secretsMigrated=True)
    item.pop("secretIntent", None)
    _save(journal)


def _restore_history(journal: dict[str, Any], item: dict[str, Any], client: NomadApi) -> None:
    """Recover a protected current job evicted by an older migration version.

    Registering the same spec is a Nomad no-op. A temporary VersionTag makes
    one retained revision without changing task configuration or scheduling
    priority. Persist its intent before submission, including for lost replies.
    """
    job_id = str(item["Job"]["ID"])
    route = "/v1/job/" + urllib.parse.quote(job_id, safe="")
    current = client.request("GET", route)
    intent = item.get("secretHistoryIntent")
    if not intent:
        if current["Version"] != item["Job"]["Version"] or _untagged_digest(current) != _untagged_digest(item["Job"]):
            raise LumaError("application changed before secret history cleanup: " + job_id)
        versions = (client.request("GET", route + "/versions") or {}).get("Versions") or []
        if any(job["Version"] == current["Version"] for job in versions):
            return
        assert_protected_job(current)
        target = copy.deepcopy(current)
        _retain_revision(target, int(current["Version"]) + 1)
        intent = item["secretHistoryIntent"] = {"prior": current, "target": target, "submittedAt": int(time.time())}
        _save(journal)
    prior, target = intent["prior"], intent["target"]
    if current["Version"] == prior["Version"] and job_digest(current) == job_digest(prior):
        _grant(target, system=bool(item["system"]))
        plan = client.request("POST", route + "/plan", {"Job": target})
        updates = (plan.get("Annotations") or {}).get("DesiredTGUpdates")
        if not isinstance(updates, dict) or plan.get("FailedTGAllocs") or (plan.get("Annotations") or {}).get("PreemptedAllocs") or any(
                count for group in updates.values() for kind, count in group.items() if kind not in {"Ignore", "InPlaceUpdate"}):
            raise LumaError("secret history recovery would disrupt allocations: " + job_id)
        response = client.request("POST", "/v1/jobs", {"Job": target, "EnforceIndex": True, "JobModifyIndex": current["JobModifyIndex"],
                                                        "Submission": {"Source": "", "Format": "json", "Variables": "", "VariableFlags": {}}})
        if not isinstance(response, dict) or not int(response.get("JobModifyIndex") or 0):
            raise LumaError("secret history recovery did not receive a verifiable job revision")
        intent["modifyIndex"] = response["JobModifyIndex"]
        _save(journal)
        current = client.request("GET", route)
    if current["Version"] != int(prior["Version"]) + 1 or job_digest(current) != job_digest(target):
        raise LumaError("application changed outside its saved secret history recovery: " + job_id)
    _wait_for_job(current, client, int(intent["submittedAt"]))
    versions = (client.request("GET", route + "/versions") or {}).get("Versions") or []
    if not any(job["Version"] == current["Version"] and _protected(job) for job in versions):
        raise LumaError("recovered protected job is still missing from Nomad history: " + job_id)
    item["Job"] = current
    item.pop("secretHistoryIntent", None)
    _save(journal)


def _retire_history(client: NomadApi, current: Mapping[str, Any]) -> None:
    job_id, version = str(current["ID"]), int(current["Version"])
    route = "/v1/job/" + urllib.parse.quote(job_id, safe="")
    # A repeated stable=true is a no-op in Nomad. A temporary version tag
    # invokes history pruning without changing tasks or allocation versions.
    # Keep the current version stable so removing the tag retains it, rather
    # than retaining a previous stable (plaintext) version instead.
    client.request("PUT", route + "/stable", {"JobID": job_id, "JobVersion": version, "Stable": True})
    temporary_tag = "luma-secret-migration-" + str(version)
    for _ in range(16):
        versions = (client.request("GET", route + "/versions") or {}).get("Versions") or []
        old = [job for job in versions if job.get("Version") != version and not _protected(job)]
        if not old:
            if any(job.get("Version") == version and (job.get("VersionTag") or {}).get("Name") == temporary_tag for job in versions):
                client.request("DELETE", route + "/versions/" + temporary_tag + "/tag")
            return
        for job in old:
            tag = (job.get("VersionTag") or {}).get("Name")
            if tag:
                client.request("DELETE", route + "/versions/" + urllib.parse.quote(str(tag), safe="") + "/tag")
        client.request("PUT", route + "/versions/" + temporary_tag + "/tag", {"Version": version, "Description": "Temporary encrypted-secret history cleanup"})
        client.request("DELETE", route + "/versions/" + temporary_tag + "/tag")
    _pending("Removing archived inline-secret job versions: " + job_id)


def _archive_legacy_files(journal: dict[str, Any]) -> None:
    from .control.state import state_dir
    paths = [state_dir() / "control.json", state_dir() / "control.json.pre-sqlite.bak", *state_dir().glob(".control-install-*.tmp")]
    for directory in (ADMIN_ROOT / "backups", *state_dir().parent.glob("control-pre-sqlite-*")):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file() and
                         (path.name == "raft.snap" or path.name == "control.sqlite3" or path.name in {"control.json", "control.json.pre-sqlite.bak"} or path.name.endswith(".nomad.json")))
    for path in paths:
        if path.is_file():
            encrypt_file(path)
            path.unlink()
    from .bootstrap import TRAEFIK_DNS_TOKEN_FILE
    dns = Path(TRAEFIK_DNS_TOKEN_FILE)
    if dns.is_file():
        encrypt_file(dns)
        dns.unlink()
    journal["secretMigration"]["legacyFilesArchived"] = True
    _save(journal)


def migrate(journal: dict[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    from .control import database
    progress = journal.setdefault("secretMigration", {})
    if not progress.get("policiesApplied"):
        from .nomad_acl import activate_acl
        activate_acl()
        progress["policiesApplied"] = True
        _save(journal)
    if not enabled():
        provision()
    # From here even saved prior job definitions are encrypted on disk.
    _save(journal)
    client = _client()
    if not progress.get("backup"):
        _private_backup(journal, client)
    if not progress.get("databaseEncrypted"):
        try:
            database.migrate_encryption()
        except database.EncryptionCleanupPending as exc:
            started = progress.setdefault("databaseCleanupStartedAt", int(time.time()))
            _save(journal)
            if time.time() - started >= 600:
                raise LumaError("SQLite cleanup was blocked for 10 minutes; encrypted data was retained") from exc
            _pending(str(exc))
        progress["databaseEncrypted"] = True
        progress.pop("databaseCleanupStartedAt", None)
        _save(journal)
    for item in journal["jobs"]:
        if not item.get("secretsMigrated"):
            _upgrade_job(journal, item, state, client)
        if not item.get("secretHistoryRetired"):
            _restore_history(journal, item, client)
            _retire_history(client, item["Job"])
            # Tags change the full system grant digest too. Persist the final
            # tag-free job so a later retry/deploy uses its actual definition.
            current = client.request("GET", "/v1/job/" + urllib.parse.quote(str(item["Job"]["ID"]), safe=""))
            if current["Version"] != item["Job"]["Version"] or _untagged_digest(current) != _untagged_digest(item["Job"]):
                raise LumaError("application changed during secret history cleanup: " + str(current["ID"]))
            assert_protected_job(current)
            _write_artifacts(state, current, system=bool(item["system"]))
            _grant(current, system=bool(item["system"]))
            item.update(Job=current, secretHistoryRetired=True)
            _save(journal)
    if not progress.get("legacyFilesArchived"):
        _archive_legacy_files(journal)
    # Nomad's GC retains running allocations. Collect retired server records
    # and their client directories only after every replacement is healthy.
    if time.time() - int(progress.get("gcRequestedAt") or 0) >= 15:
        admin = _client(administrator=True)
        admin.request("PUT", "/v1/system/gc")
        for node in journal["nodes"].values():
            admin.request("GET", "/v1/client/gc?node_id=" + urllib.parse.quote(str(node["nodeId"]), safe=""))
        progress["gcRequestedAt"] = int(time.time())
        _save(journal)
    # Check API-visible copies too; hiding current Env alone leaves historical
    # allocation Job documents readable with an ordinary read-job identity.
    for stub in client.request("GET", "/v1/jobs?namespace=*") or []:
        route = "/v1/job/" + urllib.parse.quote(str(stub["ID"]), safe="")
        try:
            versions = (client.request("GET", route + "/versions") or {}).get("Versions") or []
        except LumaError as exc:
            if _status(exc) == 404:
                continue
            raise
        if any(not _protected(job) for job in versions):
            _pending("Waiting for archived job history to leave the Nomad read API")
    for stub in client.request("GET", "/v1/allocations") or []:
        try:
            allocation = client.request("GET", "/v1/allocation/" + urllib.parse.quote(str(stub["ID"]), safe=""))
        except LumaError as exc:
            if _status(exc) == 404:
                continue
            raise
        if not _protected(allocation.get("Job") or {}):
            _pending("Waiting for retired inline-secret allocations to be collected")
    for item in journal["jobs"]:
        prune_job_variables(client, item["Job"])
    validate_workload_acl(_client(administrator=True))
    progress["verifiedAt"] = int(time.time())
    _save(journal)
    return {"encryptedStorage": True, "taskIdentitySecrets": True, "legacyApiCopiesRemoved": True}
