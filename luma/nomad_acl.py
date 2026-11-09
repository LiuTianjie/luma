"""Staged, restart-safe ACL bootstrap and credential rotation.

Preparation is offline. Files can be installed in every consumer while ACLs
are still disabled. After the operator enables ACLs on the servers, activation
uploads those exact credentials; it never falls back to a reset/bootstrap file.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .errors import LumaError
from .nomad_security import ADMIN_ROOT, NOMAD_VERSION, ROLES, SECURITY_ROOT, acl_policies, private_read, private_write, read_private_json, service_token_path


def _token_record(role: str) -> dict[str, Any]:
    # Nomad's default maximum expiration TTL is 24h. These are protected
    # service credentials, not renewable leases: silently expiring them would
    # take Control/Traefik offline. Rotate explicitly using the staged protocol;
    # short-lived node introduction JWTs remain limited to ten minutes.
    return {
        "AccessorID": str(uuid.uuid4()), "SecretID": str(uuid.uuid4()),
        "Name": "luma-" + role, "Type": "client", "Policies": ["luma-" + role],
        "Global": False,
    }


def prepare_acl(*, admin_root: Path = ADMIN_ROOT, service_root: Path = SECURITY_ROOT, namespaces: list[str] | None = None, enforce_workloads: bool = False) -> dict[str, Any]:
    if admin_root.resolve() == service_root.resolve() or service_root.resolve() in admin_root.resolve().parents or admin_root.resolve() in service_root.resolve().parents:
        raise LumaError("Nomad bootstrap credentials must be outside the Control-mounted service credential directory")
    if admin_root.resolve() == Path("/opt/luma") or Path("/opt/luma") in admin_root.resolve().parents:
        raise LumaError("Nomad administrator credential directory must be outside Control's /opt/luma bind mount")
    # Create the credential roots explicitly; mkdir(parents=True) on a role
    # directory would otherwise use the caller's umask for these parents.
    admin_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    service_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = admin_root / "acl-bootstrap.json"
    bundle = read_private_json(path, optional=True)
    requested = sorted(set(namespaces or ["default"]))
    policies = acl_policies(requested)
    if bundle:
        if bundle.get("namespaces") != requested or bundle.get("schema") != 1:
            raise LumaError("ACL bundle already exists with different namespaces; review it before preparing a replacement")
    else:
        bundle = {
            "schema": 1, "managementSecret": str(uuid.uuid4()),
            "namespaces": requested, "tokens": {r: _token_record(r) for r in ROLES},
        }
        # Persist BEFORE contacting Nomad. A lost bootstrap response is safely
        # recovered with the same secret, never with a second bootstrap token.
        private_write(path, json.dumps(bundle, indent=2) + "\n")
    private_write(admin_root / "management.token", bundle["managementSecret"] + "\n")
    for role, record in bundle["tokens"].items():
        private_write(service_token_path(service_root, role), record["SecretID"] + "\n", mode=0o400)
        private_write(admin_root / "policies" / f"{role}.hcl", policies[role])
    private_write(admin_root / "agent.token", bundle["tokens"]["agent"]["SecretID"] + "\n")
    # Observe's image runs as nobody. Bind the directory, not individual files,
    # so atomic replacements are visible during rotation. The parent stays 0700.
    for role in ("exporter", "logs"):
        private_write(service_token_path(service_root, role).parent / "client.py", Path(__file__).with_name("nomad_consumer.py").read_text(), mode=0o400)
        if os.geteuid() == 0:
            service_token_path(service_root, role).parent.chmod(0o755)
            os.chown(service_token_path(service_root, role), 65534, 65534)
            os.chown(service_token_path(service_root, role).parent / "client.py", 65534, 65534)
    policy_path = service_root / "workloads.json"
    if not policy_path.exists():
        private_write(policy_path, json.dumps({"schema": 1, "mode": "enforce" if enforce_workloads else "audit", "grants": {}}, indent=2) + "\n")
    previous_manifest = read_private_json(service_root / "credentials.json", optional=True)
    manifest = {
        "schema": 1, "stage": previous_manifest.get("stage") or "prepared", "namespaces": requested,
        "roles": {role: {"accessor": token["AccessorID"], "expiresAt": token.get("ExpirationTime")} for role, token in bundle["tokens"].items()},
    }
    private_write(service_root / "credentials.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def activate_acl(*, address: str = "http://127.0.0.1:4646", admin_root: Path = ADMIN_ROOT, service_root: Path = SECURITY_ROOT, api_factory: Any = None) -> dict[str, Any]:
    from .nomad_api import NomadApi
    factory = api_factory or NomadApi
    bundle = read_private_json(admin_root / "acl-bootstrap.json")
    management = private_read(admin_root / "management.token").strip()
    api = factory(address, token=management)
    try:
        identity = api.request("GET", "/v1/acl/token/self")
    except LumaError:
        # ACL disabled and already-bootstrapped clusters both fail explicitly.
        # Never create an acl-bootstrap-reset file or disable ACLs on failure.
        identity = api.request("PUT", "/v1/acl/bootstrap", {"BootstrapSecret": management})
    if not isinstance(identity, dict) or identity.get("Type") != "management" or identity.get("SecretID") != management:
        raise LumaError("Nomad bootstrap identity does not match the saved administrator credential")
    policies = acl_policies(bundle["namespaces"])
    for role in ROLES:
        api.request("PUT", "/v1/acl/policy/luma-" + role, {"Name": "luma-" + role, "Rules": policies[role]})
        record = bundle["tokens"][role]
        result = api.request("PUT", "/v1/acl/token/" + record["AccessorID"], record)
        if not isinstance(result, dict) or result.get("SecretID") != record["SecretID"] or result.get("Type") != "client":
            raise LumaError(f"Nomad did not install the prepared {role} credential")
    # The service credential only gets operator:read. Do this one administrative
    # scheduler setup here, retaining every unrelated scheduler setting.
    scheduler = api.request("GET", "/v1/operator/scheduler/configuration")
    settings = scheduler.get("SchedulerConfig") if isinstance(scheduler, dict) else None
    if not isinstance(settings, dict):
        raise LumaError("Nomad returned invalid scheduler settings during ACL activation")
    if not settings.get("MemoryOversubscriptionEnabled"):
        api.request("PUT", "/v1/operator/scheduler/configuration", {**settings, "MemoryOversubscriptionEnabled": True})
    status = verify_acl(address=address, service_root=service_root, api_factory=factory)
    if not status["ok"]:
        raise LumaError("ACL verification failed; prepared credentials were retained for recovery")
    config = api.request("GET", "/v1/agent/self")
    version = str((config.get("config", {}).get("Version") or {}).get("Version") or NOMAD_VERSION)
    profile = read_private_json(service_root / "cluster.json", optional=True)
    profile.update({"schema": 1, "aclRequired": True, "nomadVersion": version})
    private_write(service_root / "cluster.json", json.dumps(profile, indent=2) + "\n")
    manifest = read_private_json(service_root / "credentials.json")
    manifest["stage"] = "active"
    private_write(service_root / "credentials.json", json.dumps(manifest, indent=2) + "\n")
    workload = read_private_json(service_root / "workloads.json")
    return {"stage": "active", "nomadVersion": version, "roles": manifest["roles"], "workloadMode": workload.get("mode"), "verification": status}


def verify_acl(*, address: str = "http://127.0.0.1:4646", service_root: Path = SECURITY_ROOT, api_factory: Any = None) -> dict[str, Any]:
    from .nomad_api import NomadApi
    factory = api_factory or NomadApi
    checks: dict[str, bool] = {}

    def denied(client: Any, path: str) -> bool:
        try:
            client.request("GET", path)
        except LumaError as exc:
            return "Nomad API error 403:" in str(exc)
        return False

    checks["anonymousJobsDenied"] = denied(factory(address, token=""), "/v1/jobs")
    # A list endpoint alone does not cover job Env or historical secret copies.
    # ACL denial is required even before this built-in job exists at bootstrap.
    checks["anonymousJobDetailsDenied"] = denied(factory(address, token=""), "/v1/job/luma-control")
    checks["anonymousJobVersionsDenied"] = denied(factory(address, token=""), "/v1/job/luma-control/versions")
    checks["anonymousVariablesDenied"] = denied(factory(address, token=""), "/v1/var/luma/control/storage-key")
    for role in ROLES:
        # Read role secrets as root during the local verification, even if the
        # runtime file is owned by nobody for the unprivileged observe process.
        token = private_read(service_token_path(service_root, role)).strip()
        client = factory(address, token=token)
        try:
            identity = client.request("GET", "/v1/acl/token/self")
            checks[role + "IsClient"] = identity.get("Type") == "client" and identity.get("Policies") == ["luma-" + role]
            client.request("GET", "/v1/jobs")
            checks[role + "CanRead"] = True
        except LumaError:
            checks[role + "CanRead"] = False
        checks[role + "CannotManageACL"] = denied(client, "/v1/acl/tokens")
        if role != "control":
            checks[role + "CannotDecryptControl"] = denied(client, "/v1/var/luma/control/storage-key")
            checks[role + "CannotReadApplicationSecrets"] = denied(client, "/v1/var/nomad/jobs/luma-control/luma-control/luma-control")
    return {"ok": all(checks.values()), "checks": checks}


def rotate_role(role: str, *, address: str = "http://127.0.0.1:4646", admin_root: Path = ADMIN_ROOT, service_root: Path = SECURITY_ROOT, api_factory: Any = None) -> dict[str, Any]:
    from .nomad_api import NomadApi
    if role not in ROLES:
        raise LumaError("invalid Nomad credential role")
    factory = api_factory or NomadApi
    bundle = read_private_json(admin_root / "acl-bootstrap.json")
    previous = bundle["tokens"][role]["AccessorID"]
    pending = admin_root / f"rotate-{role}.json"
    if pending.exists():
        saved = read_private_json(pending)
        previous, new = saved["previousAccessor"], saved["token"]
    else:
        new = _token_record(role)
        private_write(pending, json.dumps({"previousAccessor": previous, "token": new}, indent=2) + "\n")
    api = factory(address, token=private_read(admin_root / "management.token").strip())
    result = api.request("PUT", "/v1/acl/token/" + new["AccessorID"], new)
    if not isinstance(result, dict) or result.get("SecretID") != new["SecretID"]:
        raise LumaError("Nomad did not accept the replacement credential; old credential remains active")
    private_write(service_token_path(service_root, role), new["SecretID"] + "\n", mode=0o400)
    if role == "agent":
        private_write(admin_root / "agent.token", new["SecretID"] + "\n")
    if role in {"exporter", "logs"} and os.geteuid() == 0:
        os.chown(service_token_path(service_root, role), 65534, 65534)
    bundle["tokens"][role] = new
    retired = bundle.setdefault("retiredAccessors", [])
    item = {"role": role, "accessor": previous}
    if previous != new["AccessorID"] and item not in retired:
        retired.append(item)
    private_write(admin_root / "acl-bootstrap.json", json.dumps(bundle, indent=2) + "\n")
    manifest = read_private_json(service_root / "credentials.json")
    manifest["roles"][role] = {"accessor": new["AccessorID"], "expiresAt": new.get("ExpirationTime")}
    private_write(service_root / "credentials.json", json.dumps(manifest, indent=2) + "\n")
    pending.unlink()
    # Revocation is deliberately a separate, explicit operation after every
    # consumer (including disconnected nodes) reports the new generation.
    return {"role": role, "accessor": new["AccessorID"], "previousAccessor": previous, "previousStillValid": True, "expiresAt": new.get("ExpirationTime")}


def revoke_accessor(accessor: str, *, address: str = "http://127.0.0.1:4646", admin_root: Path = ADMIN_ROOT, api_factory: Any = None) -> dict[str, Any]:
    from .nomad_api import NomadApi
    factory = api_factory or NomadApi
    bundle = read_private_json(admin_root / "acl-bootstrap.json")
    retired = bundle.get("retiredAccessors") or []
    if not any(item.get("accessor") == accessor for item in retired):
        raise LumaError("only a recorded retired service accessor may be revoked with this command")
    if any(item.get("AccessorID") == accessor for item in bundle["tokens"].values()):
        raise LumaError("refusing to revoke an active service credential")
    factory(address, token=private_read(admin_root / "management.token").strip()).request("DELETE", "/v1/acl/token/" + accessor)
    return {"accessor": accessor, "revoked": True}
