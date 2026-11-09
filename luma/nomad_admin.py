"""Private host-side entry points used by the installer (no secret argv/stdout)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .errors import LumaError
from .nomad_security import job_digest, nomad_token, prepare_job, private_write, read_private_json, security_root, workload_policy_lock


def client_introduction(node_name: str, *, token: str | None = None) -> str:
    from .nomad_api import NomadApi
    response = NomadApi("http://127.0.0.1:4646", token=token if token is not None else nomad_token()).request(
        "PUT", "/v1/acl/identity/client-introduction-token",
        {"NodeName": node_name, "NodePool": "default", "TTL": 600_000_000_000},
    )
    if not isinstance(response, dict) or not isinstance(response.get("JWT"), str) or not response["JWT"]:
        raise LumaError("Nomad did not issue a node introduction identity")
    return response["JWT"]


def bootstrap_manager(node_name: str) -> None:
    from .nomad_acl import activate_acl, prepare_acl
    prepare_acl(enforce_workloads=True)
    activate_acl()
    profile = read_private_json(security_root() / "cluster.json")
    profile["tlsRequired"] = True
    private_write(security_root() / "cluster.json", json.dumps(profile))
    identity = client_introduction(node_name)
    private_write(Path("/opt/nomad/data/intro_token"), identity + "\n")
    print("Nomad ACL and scoped service credentials initialized; administrator secret retained only on the host")


def local_exec(role: str, args: list[str]) -> None:
    from . import security_audit
    from .nomad_pki import client_tls, endpoint
    token = nomad_token(role=role)
    os.environ.pop("NOMAD_TOKEN", None)
    if token:
        os.environ["NOMAD_TOKEN"] = token
    os.environ["NOMAD_ADDR"] = endpoint(os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646")
    tls = client_tls(role)
    if tls:
        os.environ.update(NOMAD_CACERT=tls["ca_file"], NOMAD_CLIENT_CERT=tls["cert_file"], NOMAD_CLIENT_KEY=tls["key_file"])
    # No proxy may receive this local control-plane credential.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)
    security_audit.record("nomad.cli", method="EXEC", path="/".join(args[:2]), credentialRole=role)
    os.execvp("nomad", ["nomad", *args])


def submit_system(path: Path) -> None:
    from .nomad_api import NomadApi
    document = read_private_json(path)
    job = document.get("Job")
    if not isinstance(job, dict) or job.get("ID") not in {"luma-control", "traefik", "egress"}:
        raise LumaError("host bootstrap submit accepts only its built-in system jobs")
    root = security_root()
    prepare_job(job)
    from .nomad_security import ADMIN_ROOT
    private_write(ADMIN_ROOT / "system-jobs" / (job["ID"] + ".json"), json.dumps({"Job": job}))
    with workload_policy_lock():
        policy = read_private_json(root / "workloads.json", optional=True)
        if policy:
            policy.setdefault("grants", {})["default/" + job["ID"]] = {"sha256": job_digest(job), "reason": "Rendered by the local administrator's Luma system bootstrap"}
            private_write(root / "workloads.json", json.dumps(policy, indent=2) + "\n")
    NomadApi("http://127.0.0.1:4646", token=nomad_token()).request("POST", "/v1/jobs", {"Job": job})
    print("Nomad system job accepted")


def local_info(address: str = "http://127.0.0.1:4646") -> None:
    from .nomad_api import NomadApi
    api = NomadApi(address, token=nomad_token(role="agent"), credential_role="agent")
    own = api.request("GET", "/v1/agent/self")
    node_id = str((own.get("stats", {}).get("client") or {}).get("node_id") or "")
    if not node_id:
        raise LumaError("Nomad local client is not ready")
    detail = api.request("GET", "/v1/node/" + node_id)
    meta = detail.get("Meta") or {}
    print(json.dumps({"ID": node_id, "Name": meta.get("luma_node_name") or detail.get("Name"), "Meta": {"luma_node_name": meta.get("luma_node_name") or ""}}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap-manager")
    bootstrap.add_argument("--node", required=True)
    execute = sub.add_parser("exec")
    execute.add_argument("--role", choices=("control", "agent"), default="control")
    execute.add_argument("args", nargs=argparse.REMAINDER)
    submit = sub.add_parser("submit-system")
    submit.add_argument("path", type=Path)
    info = sub.add_parser("local-info")
    info.add_argument("--address", default="http://127.0.0.1:4646")
    pki = sub.add_parser("prepare-pki")
    pki.add_argument("--address", required=True)
    sub.add_parser("verify-node")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise LumaError("Nomad host security helper requires root")
        if args.command == "bootstrap-manager":
            bootstrap_manager(args.node)
        elif args.command == "submit-system":
            submit_system(args.path)
        elif args.command == "local-info":
            local_info(args.address)
        elif args.command == "prepare-pki":
            from .nomad_pki import prepare_manager
            prepare_manager(args.address)
            print("Nomad host CA and service TLS identities prepared")
        elif args.command == "verify-node":
            from .nomad_migration_node import execute, snapshot
            facts = snapshot()
            result = execute({"schema": 1, "op": "verify", "nodeId": facts["nodeId"], "address": facts["address"]}, allow_server=True)
            print(json.dumps(result))
        else:
            command = args.args[1:] if args.args[:1] == ["--"] else args.args
            local_exec(args.role, command)
        return 0
    except LumaError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
