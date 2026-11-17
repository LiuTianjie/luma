"""Public ingress served by a node itself (``exposure: node-edge``).

A node with public ingress runs its own Traefik on 80/443. DNS for its
``node-edge`` services points at the node's public address, so request and
response bytes never cross the Manager. Control only renders the Traefik job,
records the address and checks that the ports answer from outside.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import yaml

from ..errors import LumaError

PROBE_PORTS = (80, 443)
PROBE_TIMEOUT_SECONDS = 5.0


def parse_public_ip(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise LumaError("public ingress needs the node's public IP address; pass publicIp (CLI: --public-ip)")
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise LumaError(f"public IP must be an IPv4 or IPv6 address, got {text!r}") from exc
    if not address.is_global:
        raise LumaError(f"{address} is not a public address; use the address clients reach from the internet")
    return str(address)


def canonical_node_name(key: str, record: Mapping[str, Any]) -> str:
    """The name Nomad knows the node by (``meta.luma_node_name``)."""
    labels = record.get("labels") if isinstance(record.get("labels"), Mapping) else {}
    return str(labels.get("luma.node.name") or record.get("displayName") or key).strip()


def ingress_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    raw = record.get("publicIngress")
    return dict(raw) if isinstance(raw, Mapping) else {}


def is_enabled(record: Mapping[str, Any]) -> bool:
    ingress = ingress_record(record)
    return bool(ingress.get("enabled") and ingress.get("publicIp"))


def public_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    ingress = ingress_record(record)
    if not ingress:
        return {"enabled": False}
    return {
        "enabled": bool(ingress.get("enabled")),
        "publicIp": str(ingress.get("publicIp") or ""),
        "status": str(ingress.get("status") or ""),
        "detail": str(ingress.get("detail") or ""),
        "jobId": str(ingress.get("jobId") or ""),
        "updatedAt": int(ingress.get("updatedAt") or 0),
    }


def probe_ports(address: str, ports: Iterable[int] = PROBE_PORTS, *, connect: Callable[..., Any] = socket.create_connection) -> Dict[int, str]:
    """TCP-connect from Control to each port. Returns ``{port: ""}`` or an error."""
    results: Dict[int, str] = {}
    for port in ports:
        try:
            conn = connect((address, int(port)), timeout=PROBE_TIMEOUT_SECONDS)
        except OSError as exc:
            results[int(port)] = str(exc) or exc.__class__.__name__
        else:
            try:
                conn.close()
            except OSError:
                pass
            results[int(port)] = ""
    return results


def probe_summary(address: str, results: Mapping[int, str]) -> tuple[str, str]:
    failed = {port: error for port, error in results.items() if error}
    if not failed:
        return "ready", f"{address} answers on ports {', '.join(str(port) for port in sorted(results))}"
    ports = ", ".join(str(port) for port in sorted(failed))
    return "unreachable", (
        f"{address} does not answer on port(s) {ports} from the Manager ({'; '.join(sorted(set(failed.values())))}). "
        "Allow inbound TCP 80 and 443 in the cloud security group and host firewall; "
        "a node in mainland China also needs the domain's ICP filing at that cloud provider"
    )


def _manifest(record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    try:
        manifest = yaml.safe_load(str(record.get("manifest") or "")) or {}
    except yaml.YAMLError:
        return None
    return manifest if isinstance(manifest, Mapping) else None


def node_edge_users(state: Mapping[str, Any], names: Iterable[str]) -> List[str]:
    """Deployments whose node-edge services are pinned to any of ``names``."""
    wanted = {str(name) for name in names if name}
    deployments = state.get("deployments") if isinstance(state.get("deployments"), Mapping) else {}
    users: set[str] = set()
    for kind in ("services", "compose"):
        records = deployments.get(kind) if isinstance(deployments.get(kind), Mapping) else {}
        for slug, record in records.items():
            if not isinstance(record, Mapping):
                continue
            manifest = _manifest(record)
            if manifest is None:
                continue
            bodies = [manifest] if kind == "services" else list((manifest.get("services") or {}).values())
            for body in bodies:
                if isinstance(body, Mapping) and body.get("exposure") == "node-edge" and str(body.get("node") or "") in wanted:
                    users.add(str(record.get("name") or slug))
    return sorted(users)


def ingress_state(
    *,
    enabled: bool,
    public_ip: str,
    job_id: str,
    node_name: str,
    status: str,
    detail: str,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "enabled": enabled,
        "publicIp": public_ip,
        "jobId": job_id,
        "nodeName": node_name,
        "status": status,
        "detail": detail,
        "updatedAt": int(now if now is not None else time.time()),
    }
