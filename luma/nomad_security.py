"""Nomad credentials and admission checks shared by the installer and Control.

The bootstrap credential belongs to the host administrator, outside Control's
bind mount. A Nomad submit credential is a trusted scheduler credential: CE ACLs
are not an admission controller. Untrusted callers must go through Control.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .errors import LumaError

SECURITY_ROOT = Path("/opt/luma/security/nomad")
ADMIN_ROOT = Path("/etc/luma/nomad")
NOMAD_VERSION = "2.1.1"
# Published by HashiCorp in nomad_2.1.1_SHA256SUMS, reviewed 2026-11-09.
# Pin the bytes as well as the version; downloading a checksum alongside an
# archive without a trust anchor is not an integrity check.
NOMAD_SHA256 = {
    "linux_amd64": "073084f7690fdaea756ff1eff8b669b25c3a6c17778f19ae0c47606973ed3f39",
    "linux_arm64": "ed7222af2f9b924262a8c4e053688ad9e7a7c82fa9485d6d147261072b3b8d50",
    "darwin_amd64": "c63035fb9d8fa760189a664d7e25a1083d6f5dcf520060b454ace263f779ef0e",
    "darwin_arm64": "5aa9e30a8649608b790daad92177a40e7ca0109b03205a587ede9850811d1a77",
}
# Every intermediate minor is traversed server-first, then all clients. These
# hashes are pinned from the corresponding official SHA256SUMS documents.
NOMAD_RELEASES = {
    "1.10.5": {
        "darwin_amd64": "9087b0420b89c65cc52843c7f495bbb813ef8f7f3a4837ab2d6d379dea55e9a0",
        "darwin_arm64": "678efab703b58af5c2fee661fc1bbdbd92ad32b7199c25eb65b75b472204fe32",
        "linux_amd64": "4242244ab1f6887e963d6bb674ac490ee9b270a29aff6f826c51ef3187c77044",
        "linux_arm64": "a24f3f3d230ceacdef3c69cf264ffa913d2cf90fd862d26d36c87a6d04512e22",
    },
    "1.11.3": {
        "darwin_amd64": "ca8bf44452296db8750b411c73c6d38a872a9fae2632f93164edbc6573433814",
        "darwin_arm64": "8f8bbc6064a039df772fa7daa865185760111d2e2a1f2953ade2b126dff8e5dd",
        "linux_amd64": "19dac5642a2ba5305e6ff8efee06a708d760ebe4d1cd7936bc3dc526f477dc12",
        "linux_arm64": "902e44260fcd9297af7042fb33ccd5a29cc2202b57199724f17c96fcd67414d2",
    },
    "2.0.7": {
        "darwin_amd64": "085a1c11cd2de2e5be490cbcc4213b1faaa0af45a4e121a55b2340dcef0d6286",
        "darwin_arm64": "4ada34db43f8b75c80c4e09ce50f4753e661bcb4a3745f415dd4aa7d01483e58",
        "linux_amd64": "4c9b8a0850d6fd9caadbbab09b3e6fdf8b77aa777729543c70c61b85acca68c1",
        "linux_arm64": "19708097585efe0d60234f300b62892fa776ff93ca6ba5fb918360cbd118b71e",
    },
    NOMAD_VERSION: NOMAD_SHA256,
}
READ_CAPABILITIES = ["list-jobs", "read-job"]
CONTROL_CAPABILITIES = READ_CAPABILITIES + ["submit-job", "read-logs", "alloc-lifecycle"]
ROLES = ("control", "traefik", "exporter", "logs", "agent")


def private_read(path: Path, *, optional: bool = False) -> str:
    """Read a regular, private file without following its final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if optional:
            return ""
        raise LumaError(f"required Nomad security file is missing: {path}") from None
    except OSError:
        raise LumaError(f"cannot read Nomad security file: {path}") from None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise LumaError(f"Nomad security file must be regular and mode 0600 or 0400: {path}")
        if info.st_uid not in {0, os.geteuid()} and not (os.geteuid() == 0 and info.st_uid == 65534):
            raise LumaError(f"Nomad security file has an unexpected owner: {path}")
        if info.st_size > 16 * 1024 * 1024:
            raise LumaError(f"Nomad security file is too large: {path}")
        return handle.read()


def private_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Atomic replacement; no token is ever placed in an argv or error."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.stat()
    if path.parent.is_symlink() or parent.st_mode & 0o022 or parent.st_uid not in {0, os.geteuid()}:
        raise LumaError(f"unsafe Nomad security directory: {path.parent}")
    if path.is_symlink():
        raise LumaError(f"refusing to replace a symlink: {path}")
    fd, tmp = tempfile.mkstemp(prefix=".nomad-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_private_json(path: Path, *, optional: bool = False) -> dict[str, Any]:
    text = private_read(path, optional=optional)
    if not text and optional:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        raise LumaError(f"invalid Nomad security JSON: {path}") from None
    if not isinstance(value, dict):
        raise LumaError(f"Nomad security JSON must be an object: {path}")
    return value


def security_root() -> Path:
    return Path(os.environ.get("LUMA_NOMAD_SECURITY_DIR") or SECURITY_ROOT)


def token_path(role: str = "control") -> Path:
    if role not in ROLES:
        raise LumaError("invalid Nomad credential role")
    configured = os.environ.get(f"LUMA_NOMAD_{role.upper()}_TOKEN_FILE")
    if role == "control":
        configured = configured or os.environ.get("NOMAD_TOKEN_FILE")
    if configured:
        return Path(configured)
    return ADMIN_ROOT / "agent.token" if role == "agent" else service_token_path(security_root(), role)


def service_token_path(root: Path, role: str) -> Path:
    if role not in ROLES:
        raise LumaError("invalid Nomad credential role")
    return root / (role + ".d") / "token"


def nomad_token(state: Mapping[str, Any] | None = None, *, role: str = "control") -> str:
    path = token_path(role)
    explicit = bool(os.environ.get(f"LUMA_NOMAD_{role.upper()}_TOKEN_FILE")) or (
        role == "control" and bool(os.environ.get("NOMAD_TOKEN_FILE"))
    )
    profile = read_private_json(security_root() / "cluster.json", optional=True)
    required = explicit or profile.get("aclRequired") is True
    value = private_read(path, optional=not required).strip()
    if value:
        if any(c.isspace() for c in value):
            raise LumaError(f"invalid Nomad credential file: {path}")
        return value
    if required or path.exists():
        raise LumaError(f"Nomad credential file is empty: {path}")
    # Compatibility only until the staged ACL migration is activated. A broken
    # or missing file in an activated cluster must never downgrade to anonymous.
    return str((state or {}).get("nomadToken") or "") if role == "control" else ""


def mesh_ip(value: str) -> str:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        raise LumaError("Nomad peer must be a literal Tailscale IP") from None
    network = "100.64.0.0/10" if addr.version == 4 else "fd7a:115c:a1e0::/48"
    if addr not in ipaddress.ip_network(network):
        raise LumaError("Nomad peer must be inside the Tailscale address range")
    return str(addr)


def rpc_host(value: str) -> str:
    # No hostnames, URL credentials, broad CIDRs or shell fragments in joins.
    raw = str(value).strip()
    if raw.startswith("[") and raw.endswith("]:4647"):
        return mesh_ip(raw[1:-6])
    if raw.count(":") == 1 and raw.endswith(":4647"):
        return mesh_ip(raw[:-5])
    return mesh_ip(raw)


def acl_policies(namespaces: list[str] | None = None) -> dict[str, str]:
    namespaces = namespaces or ["default"]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", n) for n in namespaces):
        raise LumaError("ACL namespace must be an exact name, without wildcards")

    def ns(capabilities: list[str], *, secret_writer: bool = False) -> str:
        variables = ('\n  variables {\n'
                     '    path "luma/control/storage-key" { capabilities = ["read"] }\n'
                     '    path "nomad/jobs/*" { capabilities = ["read", "list", "write", "destroy"] }\n'
                     '  }') if secret_writer else ""
        return "\n".join(
            f"namespace {json.dumps(n)} {{\n  capabilities = {json.dumps(capabilities)}{variables}\n}}"
            for n in namespaces
        )

    return {
        # No ACL management, agent write, operator write, exec, CSI registration,
        # node exec, sentinel override, or unrestricted namespace wildcards.
        "control": ns(CONTROL_CAPABILITIES, secret_writer=True) + '\nnode { policy = "write" }\nagent { policy = "read" }\noperator { policy = "read" }\n',
        "traefik": ns(READ_CAPABILITIES) + "\n",
        "exporter": ns(READ_CAPABILITIES) + '\nnode { policy = "read" }\n',
        # Current shipper reads alloc/logs through the filesystem API. This is
        # intentionally separate from metrics/discovery: read-fs is sensitive.
        "logs": ns(READ_CAPABILITIES + ["read-logs", "read-fs"]) + "\n",
        "agent": ns(READ_CAPABILITIES) + '\nnode { policy = "read" }\nagent { policy = "read" }\n',
    }


def job_digest(job: Mapping[str, Any]) -> str:
    document = copy.deepcopy(dict(job))
    for key in ("CreateIndex", "ModifyIndex", "JobModifyIndex", "Version", "Status", "StatusDescription", "SubmitTime", "Stable", "TokenID"):
        document.pop(key, None)
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def needs_host_volumes(job: Mapping[str, Any]) -> bool:
    return any((task.get("Config") or {}).get(key) for group in job.get("TaskGroups") or []
               for task in group.get("Tasks") or [] for key in ("mount", "mounts", "volumes"))


def _implicit_group_constraints(job: Mapping[str, Any], group: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    """Exact defaults from Nomad's jobImpliedConstraints admission hook.

    Derive them from the job features that require them. Custom constraints,
    different version bounds, and constraints in other scopes remain pinned.
    """
    result: set[tuple[str, str, str]] = set()
    if any(network.get("Mode") == "bridge" for network in group.get("Networks") or []):
        result.update(("${attr.plugins.cni.version." + plugin + "}", ">= 0.4.0", "semver")
                      for plugin in ("bridge", "firewall", "host-local", "loopback", "portmap"))
    services = [*(group.get("Services") or []),
                *(service for task in group.get("Tasks") or [] for service in task.get("Services") or [])]
    native = [service for service in services if service.get("Provider") == "nomad"]
    if native:
        result.add(("${attr.nomad.service_discovery}", "true", "="))
    if any(service.get("Checks") for service in native):
        result.add(("${attr.nomad.version}", ">= 1.4.0", "semver"))
    # Nomad currently adds a required secret provider to every group in the job.
    if any(secret.get("Provider") == "nomad" for g in job.get("TaskGroups") or []
           for task in g.get("Tasks") or [] for secret in task.get("Secrets") or []):
        result.add(("${attr.plugins.secrets.nomad.version}", "", "is_set"))
    return result


def authority_shape(job: Mapping[str, Any]) -> dict[str, Any]:
    """Stable across Nomad's default expansion and ordinary image/env updates.

    This is used only for applications validated from a saved/rendered Luma
    deployment. Administrator/system grants still pin the complete job.
    """
    def constraints(value, implicit=()):
        keys = ("LTarget", "RTarget", "Operand")
        # Nomad returns an empty string for the omitted target of unary tests.
        return sorted(({key: (c.get(key) or "") if key == "RTarget" else c.get(key) for key in keys} for c in value or []
                       if tuple(c.get(key) or "" for key in keys) not in implicit), key=lambda c: json.dumps(c, sort_keys=True))

    groups = []
    for group in job.get("TaskGroups") or []:
        tasks = []
        for task in group.get("Tasks") or []:
            cfg = task.get("Config") or {}
            authority = {key: cfg[key] for key in ("privileged", "devices", "cap_add", "pid_mode", "ipc_mode", "uts_mode", "userns_mode", "security_opt", "sysctl", "runtime", "volume_driver", "volumes") if cfg.get(key)}
            authority["network_mode"] = cfg.get("network_mode") or "bridge"
            mounts = []
            for mount in [*(cfg.get("mount") or []), *(cfg.get("mounts") or [])]:
                role = {"nomad-exporter": "exporter", "log-shipper": "logs"}.get(task.get("Name")) if job.get("ID") == "luma-observe" else None
                if role and mount == {"type": "bind", "source": str(service_token_path(security_root(), role).parent), "target": "/run/luma-nomad-credential", "readonly": True}:
                    continue
                mounts.append({"type": mount.get("type"), "source": mount.get("source") or "", "target": mount.get("target") or "", "readonly": bool(mount.get("readonly")),
                               **{k: v for k, v in mount.items() if k not in {"type", "source", "target", "readonly"} and v}})
            authority["mounts"] = sorted(mounts, key=lambda m: json.dumps(m, sort_keys=True))
            tasks.append({"name": task.get("Name"), "driver": task.get("Driver"), "config": authority,
                          "constraints": constraints(task.get("Constraints")), "devices": (task.get("Resources") or {}).get("Devices") or [],
                          "csi": task.get("CSIPluginConfig") or {}, "volumes": task.get("VolumeMounts") or [],
                          "artifacts": task.get("Artifacts") or [], "templates": sorted(str(t.get("DestPath") or "") for t in task.get("Templates") or [])})
        groups.append({"name": group.get("Name"), "networks": sorted(str(n.get("Mode") or "host") for n in group.get("Networks") or []),
                       "volumes": group.get("Volumes") or {}, "constraints": constraints(group.get("Constraints"), _implicit_group_constraints(job, group)), "tasks": sorted(tasks, key=lambda t: t["name"])})
    # The trusted-host constraint is installed by admission, not declared by
    # the application. Exclude only this exact extra restriction for comparison.
    own_constraints = [c for c in job.get("Constraints") or [] if c != {"LTarget": "${meta.luma_host_access}", "Operand": "=", "RTarget": "trusted"}]
    return {"id": job.get("ID"), "namespace": job.get("Namespace") or "default", "type": job.get("Type") or "service",
            "nodePool": job.get("NodePool") or "default", "constraints": constraints(own_constraints), "groups": sorted(groups, key=lambda g: g["name"])}


def authority_digest(job: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(authority_shape(job), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def workload_policy_lock(path: Path | None = None):
    """Serialize grant updates from concurrent deployments and the host updater."""
    import fcntl
    path = path or security_root() / "workloads.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def validate_application(job: Mapping[str, Any]) -> None:
    inspected = copy.deepcopy(dict(job))
    # These exact, read-only reader directories are injected by prepare_job.
    # They never contain the submit token or a CA signing key.
    if inspected.get("ID") == "luma-observe":
        for group in inspected.get("TaskGroups") or []:
            for task in group.get("Tasks") or []:
                role = {"nomad-exporter": "exporter", "log-shipper": "logs"}.get(task.get("Name"))
                if role:
                    cfg = task.get("Config") or {}
                    allowed = {"type": "bind", "source": str(service_token_path(security_root(), role).parent), "target": "/run/luma-nomad-credential", "readonly": True}
                    cfg["mount"] = [m for m in cfg.get("mount") or [] if m != allowed]
    deny_host_root(inspected)
    permitted = {"host or custom network", "host bind mount", "persistent Docker volume", "unscoped Docker volume"}
    unexpected = set(job_risks(job)) - permitted
    if unexpected:
        raise LumaError("application requests unsupported host authority: " + ", ".join(sorted(unexpected)))


def authorize_application(job: dict[str, Any]) -> None:
    """Called only after a trusted Luma deployment renderer and ownership checks."""
    validate_application(job)
    root = security_root()
    with workload_policy_lock():
        policy = read_private_json(root / "workloads.json", optional=True)
        if not policy:
            return
        prepare_job(job)
        key = str(job.get("Namespace") or "default") + "/" + str(job["ID"])
        policy.setdefault("grants", {})[key] = {"authoritySHA256": authority_digest(job), "reason": "Validated Luma deployment and storage owner", "source": "luma-renderer"}
        private_write(root / "workloads.json", json.dumps(policy, indent=2) + "\n")


def job_risks(job: Mapping[str, Any]) -> list[str]:
    """Inventory the host authority of a complete job, independent of its name.

    Review grants pin the complete submitted spec (including image, arguments,
    templates, environment and placement). A name or metadata flag is never a
    grant, and a modified grant must be reviewed again.
    """
    findings: set[str] = set()
    job_id = str(job.get("ID") or "")
    if job.get("Type", "service") not in {"service", "batch"}:
        findings.add("system-wide scheduling")
    if job.get("NodePool") == "all":
        findings.add("all node pools")
    for group in job.get("TaskGroups") or []:
        if not isinstance(group, Mapping):
            raise LumaError("invalid Nomad task group")
        for network in group.get("Networks") or []:
            if str(network.get("Mode") or "host") != "bridge":
                findings.add("host or custom network")
        if group.get("Volumes"):
            findings.add("host or CSI volume")
        for task in group.get("Tasks") or []:
            if not isinstance(task, Mapping):
                raise LumaError("invalid Nomad task")
            driver = str(task.get("Driver") or "")
            if driver != "docker":
                findings.add("non-Docker driver")
            cfg = task.get("Config") or {}
            if not isinstance(cfg, Mapping):
                raise LumaError("invalid Nomad task configuration")
            if cfg.get("privileged"):
                findings.add("privileged container")
            if cfg.get("devices") or (task.get("Resources") or {}).get("Devices"):
                findings.add("host devices")
            if cfg.get("cap_add"):
                findings.add("extra Linux capabilities")
            if any(cfg.get(k) for k in ("pid_mode", "ipc_mode", "uts_mode", "userns_mode", "security_opt", "sysctl", "runtime", "volume_driver")):
                findings.add("host namespace or security override")
            if str(cfg.get("network_mode") or "bridge") not in {"bridge", "none"}:
                findings.add("host or custom network")
            if cfg.get("volumes"):
                findings.add("legacy Docker volume syntax")
            for mount in [*(cfg.get("mount") or []), *(cfg.get("mounts") or [])]:
                if not isinstance(mount, Mapping):
                    raise LumaError("invalid Nomad Docker mount")
                kind, source = str(mount.get("type") or ""), str(mount.get("source") or "")
                if kind == "bind":
                    findings.add("host bind mount")
                elif kind == "volume":
                    findings.add("persistent Docker volume")
                    # Existing shared/legacy volumes need an explicit grant;
                    # arbitrary names could otherwise attach another app's data.
                    if not job_id or not source.startswith(job_id + "_"):
                        findings.add("unscoped Docker volume")
                    if mount.get("volume_options"):
                        findings.add("Docker volume driver options")
                elif kind != "tmpfs":
                    findings.add("unknown mount type")
                if mount.get("bind_options"):
                    findings.add("mount propagation options")
            if task.get("CSIPluginConfig") or task.get("VolumeMounts"):
                findings.add("CSI plugin or host volume")
            if task.get("Artifacts"):
                findings.add("artifact download")
            for template in task.get("Templates") or []:
                destination = str(template.get("DestPath") or "")
                if destination.startswith("/") or ".." in Path(destination).parts:
                    findings.add("template outside task directory")
    return sorted(findings)


def prepare_job(job: dict[str, Any], *, enforce: bool | None = None) -> None:
    """Attach role-only credential directories and constrain privileged work.

    The result still has to pass admission. Recognizing a built-in consumer
    here confers no permission to run its image, mounts or command.
    """
    root = security_root()
    staged = read_private_json(root / "credentials.json", optional=True)
    if not staged and read_private_json(root / "cluster.json", optional=True).get("aclRequired"):
        raise LumaError("active Nomad cluster is missing its staged consumer credential manifest")
    job_id = str(job.get("ID") or "")
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            name = str(task.get("Name") or "")
            role = ""
            if job_id == "traefik" and name == "traefik":
                role = "traefik"
            elif job_id == "luma-observe" and name in {"nomad-exporter", "log-shipper"}:
                role = "exporter" if name == "nomad-exporter" else "logs"
            if not role or not staged:
                continue
            cfg = task.setdefault("Config", {})
            directory = str(service_token_path(root, role).parent)
            target = "/run/luma-nomad-credential"
            mount = {"type": "bind", "source": directory, "target": target, "readonly": True}
            mounts = cfg.setdefault("mount", [])
            if mount not in mounts:
                mounts.append(mount)
            env = task["Env"] = task.get("Env") or {}
            env["NOMAD_TOKEN_FILE"] = target + "/token"
            transport = read_private_json(root / "transport.json", optional=True)
            if transport.get("http"):
                env.update(NOMAD_ADDR="https://127.0.0.1:4646", NOMAD_CACERT=target + "/ca.pem", NOMAD_CLIENT_CERT=target + "/cert.pem", NOMAD_CLIENT_KEY=target + "/key.pem")
                certificate = private_read(service_token_path(root, role).parent / "cert.pem")
                env["LUMA_NOMAD_TLS_GENERATION"] = hashlib.sha256(certificate.encode()).hexdigest()
                if role == "traefik":
                    arguments = [str(a).replace("--providers.nomad.endpoint.address=http://", "--providers.nomad.endpoint.address=https://") for a in cfg.get("args") or []]
                    for setting, value in (("ca", "ca.pem"), ("cert", "cert.pem"), ("key", "key.pem")):
                        option = f"--providers.nomad.endpoint.tls.{setting}={target}/{value}"
                        if option not in arguments:
                            arguments.append(option)
                    cfg["args"] = arguments
                else:
                    script = "/app/nomad_exporter.py" if role == "exporter" else "/app/log_shipper.py"
                    # Nomad's command replaces Docker Cmd, not ENTRYPOINT.
                    # The Observe image already starts python3; use a neutral
                    # entrypoint so the credential adapter runs exactly once.
                    cfg["entrypoint"] = ["/usr/bin/env"]
                    cfg["command"] = "python3"
                    cfg["args"] = [target + "/client.py", script]
            if role == "traefik" and env.get("LUMA_NOMAD_CREDENTIAL_WRAPPER") != "1":
                # Traefik v3.6's Nomad provider defaults its token from
                # NOMAD_TOKEN. Only the shell reads the file; no secret occurs
                # in job JSON or process arguments. Restart after rotating it.
                args = list(cfg.get("args") or [])
                cfg["command"] = "/bin/sh"
                cfg["args"] = ["-ec", 'export NOMAD_TOKEN="$(cat "$NOMAD_TOKEN_FILE")"; test -n "$NOMAD_TOKEN"; exec /entrypoint.sh "$@"', "luma-nomad-auth", *args]
                env["LUMA_NOMAD_CREDENTIAL_WRAPPER"] = "1"
    policy = read_private_json(root / "workloads.json", optional=True)
    if (enforce if enforce is not None else policy.get("mode") == "enforce") and needs_host_volumes(job):
        constraint = {"LTarget": "${meta.luma_host_access}", "Operand": "=", "RTarget": "trusted"}
        constraints = job["Constraints"] = job.get("Constraints") or []
        if constraint not in constraints:
            constraints.append(constraint)


def admit_job(job: Mapping[str, Any], *, policy: Mapping[str, Any] | None = None) -> None:
    if policy is None:
        required = read_private_json(security_root() / "cluster.json", optional=True).get("aclRequired") is True
        policy = read_private_json(security_root() / "workloads.json", optional=not required)
    if policy and (policy.get("schema") != 1 or policy.get("mode") not in {"audit", "enforce"}):
        raise LumaError("invalid Nomad workload security policy; refusing submission")
    risks = job_risks(job)
    if not risks:
        return
    # Existing clusters explicitly stage the policy before activation. Without
    # a policy this check is observational; new bootstrap always installs one.
    if not policy or policy.get("mode") == "audit":
        return
    key = str(job.get("Namespace") or "default") + "/" + str(job.get("ID") or "")
    grant = (policy.get("grants") or {}).get(key, {})
    digest = job_digest(job)
    if isinstance(grant, Mapping) and grant.get("sha256") == digest and grant.get("reason"):
        return
    if isinstance(grant, Mapping) and grant.get("source") == "luma-renderer" and grant.get("authoritySHA256") == authority_digest(job):
        # An application grant cannot authorize dangerous fields just by
        # finding an old document with the same hash.
        validate_application(job)
        return
    review = security_root() / "review-pending" / (digest + ".json")
    private_write(review, json.dumps({"Job": job}, indent=2) + "\n")
    raise LumaError(
        f"Nomad workload {key} requires a reviewed security grant ({', '.join(risks)}); "
        f"private review artifact: {review}; use luma security review-job, then retry the recorded deployment workflow"
    )


def deny_host_root(job: Mapping[str, Any]) -> None:
    """Never let a Luma application renderer turn a host root into a data path.

    Does not depend on a policy, ACL state, or job name. System bootstrap is a
    separate administrator path. Preserve all legitimate data volume sources.
    """
    for group in job.get("TaskGroups") or []:
        for task in group.get("Tasks") or []:
            cfg = task.get("Config") or {}
            for mount in [*(cfg.get("mount") or []), *(cfg.get("mounts") or [])]:
                if mount.get("type") != "bind":
                    continue
                raw = str(mount.get("source") or "")
                # resolve lexically, not against the Control container's FS.
                path = os.path.normpath(raw)
                public_routes = path == "/opt/luma/routes" and mount.get("readonly") is True
                protected = ("/etc", "/private/etc", "/System", "/usr", "/run", "/root", "/proc", "/sys", "/dev", "/opt/luma", "/opt/nomad", "/var/run", "/private/var/run", "/private/var/root", "/private/var/db", "/var/lib/docker", "/var/lib/containerd", "/boot", "/bin", "/sbin", "/lib", "/lib64")
                if not os.path.isabs(path) or path in {"/", "/var", "/private", "/private/var", "/opt", "/home", "/Users", "/srv", "/mnt"} or (not public_routes and any(path == base or path.startswith(base + "/") for base in protected)):
                    raise LumaError("application bind mount exposes a protected host directory")
                if any(part in {".ssh", ".aws", ".kube", ".docker", ".luma", ".codex", "Keychains"} for part in Path(path).parts) or (path.startswith(("/home/", "/Users/")) and len(Path(path).parts) <= 3):
                    raise LumaError("application bind mount exposes host credentials")
                if path.endswith("/docker.sock") or path.endswith("/containerd.sock"):
                    raise LumaError("application bind mount exposes a container engine socket")
