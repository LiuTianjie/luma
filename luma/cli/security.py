"""Explicit local security preparation and maintenance commands.

Nothing here is invoked by a normal CLI/Control image update. Applying a node
bundle restarts that node only and requires the digest of the reviewed bundle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ..errors import LumaError
from ..local import LocalExecutor
from ..nomad_security import (
    ADMIN_ROOT, NOMAD_VERSION, SECURITY_ROOT, ROLES, job_digest,
    job_risks, nomad_token, private_read, private_write, read_private_json,
    rpc_host, security_root, prepare_job, workload_policy_lock,
)


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("security", help="Prepare and apply reviewed Nomad security migrations")
    commands = parser.add_subparsers(dest="security_command", required=True)
    plan = commands.add_parser("plan", help="Read Nomad versions, ACL state, peers and host-access requirements")
    plan.add_argument("--output", type=Path, required=True, help="Private JSON file for the observed cluster plan")
    review = commands.add_parser("review-job", help="Create a grant for one exact, reviewed Nomad job specification")
    review.add_argument("--job", type=Path, required=True, help="Private file containing the complete reviewed job")
    review.add_argument("--policy", type=Path, required=True, help="Private workload policy file to update")
    review.add_argument("--reason", required=True, help="Record why this exact job requires host access")
    review.add_argument("--enforce", action="store_true", help="Enable enforcement in the output policy")
    prepare = commands.add_parser("prepare-acl", help="Create private credentials and policies offline; does not enable ACL")
    prepare.add_argument("--namespace", action="append", dest="namespaces", help="Exact permitted namespace; repeat for multiple namespaces")
    prepare.add_argument("--enforce-workloads", action="store_true", help="Enable job admission when creating a new workload policy")
    activate = commands.add_parser("activate-acl", help="Bootstrap/apply the prepared policies after servers enable ACL")
    verify = commands.add_parser("verify-acl", help="Verify anonymous denial and each installed service credential")
    rotate = commands.add_parser("rotate", help="Publish a replacement role credential, retaining the old token")
    rotate.add_argument("--role", choices=ROLES, required=True, help="Service role whose credential will be replaced")
    revoke = commands.add_parser("revoke", help="Revoke a recorded retired service accessor after consumer verification")
    revoke.add_argument("--accessor", required=True, help="Previously retired accessor ID to revoke after all consumers update")
    for command in (plan, prepare, activate, verify, rotate, revoke):
        command.add_argument("--nomad-addr", default="http://127.0.0.1:4646", help="Local Nomad origin; managed HTTPS is selected automatically")
    for command in (prepare, activate, rotate, revoke):
        command.add_argument("--admin-dir", type=Path, default=ADMIN_ROOT, help="Host-only administrator credential directory outside Control's mount")
    for command in (prepare, activate, verify, rotate):
        command.add_argument("--service-dir", type=Path, default=SECURITY_ROOT, help="Private service-role credential directory")
    node = commands.add_parser("prepare-node", help="Write a reviewable node config/firewall bundle without changing the host")
    node.add_argument("--output", type=Path, required=True, help="Private file in which to save the reviewable node bundle")
    node.add_argument("--name", required=True, help="Registered Luma node name")
    node.add_argument("--region", required=True, help="Existing Luma scheduling region")
    node.add_argument("--role", choices=("server", "client"), required=True, help="Nomad role of this host")
    node.add_argument("--mesh-ip", required=True, help="This host's literal Tailscale address")
    node.add_argument("--server", action="append", default=[], help="Enrolled server RPC address; repeat for each server")
    node.add_argument("--rpc-peer", action="append", default=[], help="Exact mesh peer allowed to reach RPC; repeat as needed")
    node.add_argument("--http-peer", action="append", default=[], help="Exact control-plane mesh peer allowed to reach the HTTP API")
    node.add_argument("--serf-peer", action="append", default=[], help="Exact server mesh peer allowed to reach Serf")
    node.add_argument("--os", dest="os_name", choices=("linux", "darwin"), default="darwin" if sys.platform == "darwin" else "linux", help="Operating system of the target host")
    node.add_argument("--mesh-interface", default="", help="Verified macOS Tailscale utun interface")
    node.add_argument("--host-access", choices=("isolated", "trusted"), default="isolated", help="Whether this reviewed host may run jobs with persistent host volumes")
    node.add_argument("--version", choices=("1.9.7", NOMAD_VERSION), default=NOMAD_VERSION, help="1.9.7 is for temporary ACL/network containment only; it is not installed")
    node.add_argument("--stage-acl", action="store_true", help="Stage network/config only, leaving ACL disabled on a legacy node")
    node.add_argument("--replace-config", action="store_true", help="Acknowledge replacing the reviewed existing nomad.hcl")
    node.add_argument("--base-config", type=Path, default=Path("/etc/nomad.d/nomad.hcl"), help="Existing config whose digest must match when applying")
    node.add_argument("--tls-ca", help="Path to the enrolled CA certificate on the target host")
    node.add_argument("--tls-cert", help="Path to this node's TLS certificate")
    node.add_argument("--tls-key", help="Path to this node's private TLS key; never pass key contents")
    apply = commands.add_parser("apply-node", help="Apply a reviewed node bundle, preserving backups and restarting only this node")
    apply.add_argument("--bundle", type=Path, required=True, help="Reviewed private node bundle to apply")
    apply.add_argument("--expected-sha256", required=True, help="SHA-256 of the exact reviewed bundle")


def _local_admin_address(address: str) -> str:
    parsed = urllib.parse.urlsplit(address)
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise LumaError("administrator ACL operations must target the local Nomad agent; run this command on the manager")
    return address


def _require_root() -> None:
    if os.geteuid() != 0:
        raise LumaError("run this local administrator operation with sudo; credentials must be root-owned")


def security_plan(address: str, output: Path) -> dict[str, Any]:
    from .. import __version__
    from ..nomad_api import NomadApi
    api = NomadApi(address, token=nomad_token())
    own = api.request("GET", "/v1/agent/self")
    cfg = own.get("config") or {}
    nodes = api.request("GET", "/v1/nodes") or []
    node_rows = []
    for node in nodes:
        detail = api.request("GET", "/v1/node/" + urllib.parse.quote(str(node["ID"]), safe=""))
        attrs, meta = detail.get("Attributes") or {}, detail.get("Meta") or {}
        node_rows.append({
            "id": node["ID"], "name": meta.get("luma_node_name") or detail.get("Name"),
            "nomadVersion": attrs.get("nomad.version"), "os": attrs.get("os.name"),
            "status": detail.get("Status"), "meshIP": meta.get("luma_tailscale_ip"),
            "pool": detail.get("NodePool") or "default", "hostAccess": meta.get("luma_host_access") or "unclassified",
        })
    jobs = []
    for stub in api.request("GET", "/v1/jobs?namespace=*") or []:
        namespace = str(stub.get("Namespace") or "default")
        query = urllib.parse.urlencode({"namespace": namespace})
        job = api.request("GET", "/v1/job/" + urllib.parse.quote(str(stub["ID"]), safe="") + "?" + query)
        jobs.append({"id": stub["ID"], "namespace": namespace, "modifyIndex": job.get("JobModifyIndex"), "sha256": job_digest(job), "hostAuthority": job_risks(job), "stopped": bool(job.get("Stop"))})
    current = str((cfg.get("Version") or {}).get("Version") or "unknown")
    result = {
        "schema": 1, "observedAt": int(time.time()), "lumaCLI": __version__,
        "nomadVersion": current, "targetVersion": NOMAD_VERSION,
        "aclEnabled": (cfg.get("ACL") or {}).get("Enabled") is True,
        "tlsHTTP": (cfg.get("TLSConfig") or {}).get("EnableHTTP") is True,
        "tlsRPC": (cfg.get("TLSConfig") or {}).get("EnableRPC") is True,
        "addresses": cfg.get("Addresses"), "nodes": node_rows, "jobs": jobs,
        "migration": "automatic server-first migration after manager and node-agent updates; see the system update security status",
        "requirements": ["stage all consumers before ACL activation", "review each host-access job; stopped or unknown jobs are never auto-approved", "inspect mount sources and symlinks on their actual nodes", "take and verify a private Nomad snapshot", "Nomad downgrades are unsupported; restore into a separate cluster", "Mac Docker VM and host-network tasks are trusted-host workloads"],
    }
    private_write(output, json.dumps(result, indent=2) + "\n")
    return {"plan": str(output), "nomadVersion": current, "targetVersion": NOMAD_VERSION, "nodes": len(nodes), "jobsNeedingReview": sum(bool(j["hostAuthority"]) for j in jobs), "aclEnabled": result["aclEnabled"]}


def review_job(job_file: Path, policy_file: Path, reason: str, *, enforce: bool = False) -> dict[str, Any]:
    # A rendered job can contain environment/registry secrets. Require the same
    # private-file handling as the policy and never echo its contents.
    document = read_private_json(job_file)
    job = document.get("Job", document)
    if not isinstance(job, dict) or not job.get("ID"):
        raise LumaError("review requires a complete Nomad Job with an ID")
    if len(reason.strip()) < 8:
        raise LumaError("record a concrete reason for this host-access grant")
    with workload_policy_lock(policy_file):
        policy = read_private_json(policy_file, optional=True) or {"schema": 1, "mode": "audit", "grants": {}}
        # Hash the spec that enforcement will submit, including its extra
        # trusted-node restriction when this review activates enforcement.
        if enforce:
            policy["mode"] = "enforce"
        prepare_job(job, enforce=policy.get("mode") == "enforce")
        key = str(job.get("Namespace") or "default") + "/" + str(job["ID"])
        grant = {"sha256": job_digest(job), "reason": reason.strip(), "reviewedAt": int(time.time()), "hostAuthority": job_risks(job)}
        policy.setdefault("grants", {})[key] = grant
        private_write(policy_file, json.dumps(policy, indent=2) + "\n")
    return {"job": key, "sha256": grant["sha256"], "hostAuthority": grant["hostAuthority"], "policy": str(policy_file), "mode": policy["mode"]}


def prepare_node(args: argparse.Namespace) -> dict[str, Any]:
    from ..nomad_node import render_agent_config
    from ..nomad_firewall import network_policy
    raw = args.base_config.read_bytes() if args.base_config.exists() else b""
    if raw and not args.replace_config:
        raise LumaError("existing Nomad config found; review and preserve its custom settings, then prepare with --replace-config")
    tls = None
    if any((args.tls_ca, args.tls_cert, args.tls_key)):
        if not all((args.tls_ca, args.tls_cert, args.tls_key)):
            raise LumaError("provide all three TLS file paths")
        tls = {"ca_file": args.tls_ca, "cert_file": args.tls_cert, "key_file": args.tls_key}
    if re.search(rb"\btls\s*\{", raw) and not tls:
        raise LumaError("existing TLS configuration must be preserved explicitly; refusing a TLS downgrade")
    # Preserve the existing Luma node's scheduling metadata and CPU override.
    # These carry ingress/egress placement and Mac capacity; dropping them
    # during a security migration can strand healthy system jobs.
    metadata: dict[str, str] = {}
    block = re.search(rb"\bmeta\s*\{([^{}]*)\}", raw, re.S)
    if block:
        for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*("(?:\\.|[^"\\])*")', block.group(1).decode()):
            if key not in {"region", "luma_node_name", "luma_tailscale_ip", "luma_host_access"}:
                metadata[key] = json.loads(value)
    cpu = re.search(rb"\bcpu_total_compute\s*=\s*([0-9]+)", raw)
    servers = [rpc_host(s) for s in args.server]
    policy = network_policy(os_name=args.os_name, address=args.mesh_ip, rpc_peers=args.rpc_peer or servers, http_peers=args.http_peer or servers, serf_peers=args.serf_peer, interface=args.mesh_interface)
    config = render_agent_config(
        os_name=args.os_name, role=args.role, tailscale_ip=args.mesh_ip,
        region=args.region, node_name=args.name, server_addrs=args.server,
        acl_enabled=not args.stage_acl, trusted_host=args.host_access == "trusted",
        version=args.version, tls=tls,
        extra_meta=metadata, cpu_total_compute=int(cpu.group(1)) if cpu else None,
    )
    bundle = {
        "schema": 1, "node": args.name, "os": args.os_name, "role": args.role,
        "nomadVersion": args.version, "aclRequired": not args.stage_acl,
        "trustedHost": args.host_access == "trusted", "network": policy,
        "config": config, "baselineSHA256": hashlib.sha256(raw).hexdigest(),
        "baseConfig": str(args.base_config), "replaceConfig": args.replace_config,
        "tls": tls,
    }
    text = json.dumps(bundle, indent=2) + "\n"
    private_write(args.output, text)
    return {"bundle": str(args.output), "sha256": hashlib.sha256(text.encode()).hexdigest(), "restartsNomadWhenApplied": True, "nomadVersion": args.version, "hostAccess": args.host_access}


def apply_node(path: Path, expected: str) -> dict[str, Any]:
    from ..nomad_node import install_nomad_commands
    from ..nomad_firewall import install_guard
    _require_root()
    raw = private_read(path)
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or hashlib.sha256(raw.encode()).hexdigest() != expected:
        raise LumaError("node bundle differs from the reviewed SHA-256")
    bundle = json.loads(raw)
    if bundle.get("schema") != 1 or bundle.get("baseConfig") != "/etc/nomad.d/nomad.hcl":
        raise LumaError("unsupported node bundle; the active config path must be /etc/nomad.d/nomad.hcl")
    config_path = Path(bundle["baseConfig"])
    existing = config_path.read_bytes() if config_path.exists() else b""
    fragments = [p for p in config_path.parent.glob("*") if p.is_file() and p.suffix in {".hcl", ".json"} and p.name != config_path.name]
    if fragments:
        raise LumaError("additional Nomad config fragments could override security settings; consolidate and review them before applying a single-file bundle")
    if hashlib.sha256(existing).hexdigest() != bundle["baselineSHA256"]:
        raise LumaError("Nomad configuration changed after this bundle was prepared")
    if existing and not bundle.get("replaceConfig"):
        raise LumaError("the bundle does not authorize replacing existing config")
    remote = LocalExecutor()
    version = remote.run("nomad version").splitlines()[0]
    match = re.search(r"Nomad v?(\d+\.\d+\.\d+)", version)
    if not match or match.group(1) != bundle["nomadVersion"]:
        raise LumaError("install the reviewed Nomad version first; apply-node never upgrades or downgrades a binary")
    if bundle.get("aclRequired") and not private_read(ADMIN_ROOT / "agent.token", optional=True).strip():
        raise LumaError("stage this node's read-only agent credential before enabling ACL")
    if bundle.get("role") == "server" and bundle.get("aclRequired"):
        read_private_json(security_root() / "credentials.json")
    # Validate the complete merged directory before writing any active config.
    with tempfile.TemporaryDirectory(prefix="luma-nomad-validate-") as directory:
        import shutil
        stage = Path(directory)
        for source in config_path.parent.glob("*"):
            if source.is_file() and source.name != config_path.name and source.suffix in {".hcl", ".json"}:
                shutil.copy2(source, stage / source.name)
        private_write(stage / config_path.name, bundle["config"])
        remote.run("nomad config validate " + shlex.quote(str(stage)))
    from ..nomad_api import NomadApi
    api = NomadApi("http://127.0.0.1:4646", token=nomad_token(role="agent"), credential_role="agent")
    own = api.request("GET", "/v1/agent/self")
    current_cfg = own.get("config") or {}
    if (current_cfg.get("ACL") or {}).get("Enabled") and not bundle.get("aclRequired"):
        raise LumaError("apply-node cannot turn off an active ACL configuration")
    node_id = (own.get("stats", {}).get("client") or {}).get("node_id")
    if node_id and not bundle.get("trustedHost"):
        allocations = api.request("GET", "/v1/node/" + str(node_id) + "/allocations") or []
        if any(job_risks(a.get("Job") or {}) for a in allocations if a.get("ClientStatus") not in {"complete", "failed", "lost"}):
            raise LumaError("this node has workloads with host authority; migrate/drain them or prepare an explicitly trusted-host bundle")
    backup = ADMIN_ROOT / "backups" / (str(int(time.time())) + "-" + expected[:12])
    private_write(backup / "nomad.hcl", existing.decode())
    service_path = Path("/etc/systemd/system/nomad.service" if bundle["os"] == "linux" else "/Library/LaunchDaemons/io.luma.nomad.plist")
    if service_path.exists():
        private_write(backup / service_path.name, service_path.read_text())
    # Firewall first. If installation or validation fails, Nomad is not started
    # against the new configuration. An old running agent remains protected.
    install_guard(remote, bundle["network"])
    private_write(config_path, bundle["config"])
    private_write(security_root() / "transport.json", json.dumps({"http": bool(bundle.get("tls")), "address": bundle["network"]["address"]}))
    unit = install_nomad_commands(os_name=bundle["os"], arch=os.uname().machine, config_hcl=bundle["config"])
    if bundle["os"] == "linux":
        private_write(Path("/etc/systemd/system/nomad.service"), unit["service_unit"], mode=0o644)
        remote.run("systemctl daemon-reload && systemctl restart nomad")
    else:
        private_write(Path("/Library/LaunchDaemons/io.luma.nomad.plist"), unit["service_unit"], mode=0o644)
        from ..launchd import restart_service
        restart_service(remote, label="io.luma.nomad", plist="/Library/LaunchDaemons/io.luma.nomad.plist")
    private_write(security_root() / "node.json", json.dumps({**{k: bundle[k] for k in ("schema", "node", "role", "nomadVersion", "aclRequired", "trustedHost", "tls")}, "configSHA256": hashlib.sha256(bundle["config"].encode()).hexdigest()}, indent=2) + "\n")
    return {"node": bundle["node"], "applied": True, "backup": str(backup), "verificationRequired": "verify-acl and remote/network probes; a restart return code is not fleet acceptance"}


def cmd_security(args: argparse.Namespace) -> int:
    from ..nomad_acl import activate_acl, prepare_acl, revoke_accessor, rotate_role, verify_acl
    command = args.security_command
    if command == "plan":
        result = security_plan(args.nomad_addr, args.output)
    elif command == "review-job":
        result = review_job(args.job, args.policy, args.reason, enforce=args.enforce)
    elif command == "prepare-node":
        result = prepare_node(args)
    elif command == "apply-node":
        result = apply_node(args.bundle, args.expected_sha256)
    else:
        _require_root()
        address = _local_admin_address(args.nomad_addr)
        if command == "prepare-acl":
            result = prepare_acl(admin_root=args.admin_dir, service_root=args.service_dir, namespaces=args.namespaces, enforce_workloads=args.enforce_workloads)
        elif command == "activate-acl":
            result = activate_acl(address=address, admin_root=args.admin_dir, service_root=args.service_dir)
        elif command == "verify-acl":
            result = verify_acl(address=address, service_root=args.service_dir)
        elif command == "rotate":
            result = rotate_role(args.role, address=address, admin_root=args.admin_dir, service_root=args.service_dir)
        elif command == "revoke":
            result = revoke_accessor(args.accessor, address=address, admin_root=args.admin_dir)
        else:
            raise LumaError("unknown security command")
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 1
