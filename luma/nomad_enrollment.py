"""Control-side node enrollment using scoped identities, never admin tokens."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .errors import LumaError
from .nomad_security import NOMAD_VERSION, mesh_ip, nomad_token, private_read, read_private_json, security_root, service_token_path


def agent_credential(*, capabilities: Any = None) -> dict[str, Any]:
    from .nomad_migration_node import CAPABILITY
    if CAPABILITY not in (capabilities or []):
        return {}
    root = security_root()
    profile = read_private_json(root / "cluster.json", optional=True)
    if not profile.get("aclRequired") and not (root / "credentials.json").exists():
        return {}
    metadata = read_private_json(root / "credentials.json")
    role = (metadata.get("roles") or {}).get("agent") or {}
    return {"schema": 1, "agentToken": private_read(service_token_path(root, "agent")).strip(), "accessor": role.get("accessor"), "expiresAt": role.get("expiresAt")}


def join_settings(*, node_name: str, tailscale_ip: str, state: Mapping[str, Any]) -> dict[str, Any]:
    from .nomad_api import NomadApi
    from .nomad_firewall import stage_peer
    root = security_root()
    profile = read_private_json(root / "cluster.json", optional=True)
    if not profile.get("aclRequired"):
        return {"schema": 1, "aclRequired": False, "nomadVersion": profile.get("nomadVersion") or "legacy"}
    if profile.get("nomadVersion") != NOMAD_VERSION:
        raise LumaError("finish the reviewed Nomad version migration before enrolling new nodes")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", node_name):
        raise LumaError("invalid Nomad enrollment node name")
    address = mesh_ip(tailscale_ip)
    from .nomad_pki import request_enrollment
    certificate = request_enrollment(node_name, address)
    stage_peer(address)
    api = NomadApi(str(state.get("nomadAddr") or "http://127.0.0.1:4646"), token=nomad_token(state))
    identity = api.request("PUT", "/v1/acl/identity/client-introduction-token", {"NodeName": node_name, "NodePool": "default", "TTL": 600_000_000_000})
    if not isinstance(identity, dict) or not identity.get("JWT"):
        raise LumaError("Nomad introduction identity was not issued")
    return {
        **agent_credential(capabilities=["nomad-security-upgrade-v1"]), "aclRequired": True, "nomadVersion": NOMAD_VERSION,
        "trustedHost": False, "introToken": identity["JWT"], "tlsBundle": certificate,
    }


def install_reader_update(remote: Any, payload: Mapping[str, Any]) -> None:
    """Accept only the reader part of a heartbeat response on the target host."""
    import uuid
    from .nomad_security import ADMIN_ROOT
    if payload.get("schema") != 1:
        raise LumaError("unsupported Nomad credential update")
    value = str(payload.get("agentToken") or "")
    try:
        uuid.UUID(value)
    except ValueError:
        raise LumaError("invalid scoped Nomad reader credential") from None
    previous = read_private_json(ADMIN_ROOT / "agent-credential.json", optional=True)
    if previous.get("accessor") == payload.get("accessor") and private_read(ADMIN_ROOT / "agent.token", optional=True).strip() == value:
        return
    remote.sudo(f"install -d -m 700 {ADMIN_ROOT}")
    remote.write_secret(value + "\n", str(ADMIN_ROOT / "agent.token"))
    remote.write_secret(json.dumps({"accessor": payload.get("accessor"), "expiresAt": payload.get("expiresAt")}) + "\n", str(ADMIN_ROOT / "agent-credential.json"))


def reader_accessor() -> str:
    from .nomad_security import ADMIN_ROOT
    return str(read_private_json(ADMIN_ROOT / "agent-credential.json", optional=True).get("accessor") or "")
