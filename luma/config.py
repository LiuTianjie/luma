from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import LumaError
from .io import load_yaml, write_yaml


@dataclass(frozen=True)
class NodeConfig:
    name: str
    host: str
    public_ip: Optional[str] = None
    region: str = "cn"
    roles: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass(frozen=True)
class LumaConfig:
    raw: Dict[str, Any]
    path: Optional[Path]

    @property
    def project_name(self) -> str:
        project = self.raw.get("project", "luma")
        if isinstance(project, dict):
            return str(project.get("name", "luma"))
        return str(project)

    @property
    def defaults(self) -> Dict[str, Any]:
        return dict(self.raw.get("defaults") or {})

    @property
    def stack_root(self) -> Path:
        value = self.defaults.get("stackRoot", "stacks")
        return Path(value)

    @property
    def public_network(self) -> str:
        return str(self.defaults.get("publicNetwork", "public"))

    @property
    def egress_network(self) -> str:
        return str(self.defaults.get("egressNetwork", "egress"))

    @property
    def routes_root(self) -> Path:
        value = self.defaults.get("routesRoot", "routes")
        return Path(value)

    @property
    def cert_resolver(self) -> str:
        return str(self.defaults.get("certResolver", "letsencrypt"))

    @property
    def acme_dns_provider(self) -> str:
        """Return the ACME DNS provider, when wildcard issuance is available.

        Cloudflare-backed installations opt into DNS-01 automatically.  This
        keeps randomly generated application hostnames from each requiring a
        separate, failure-prone HTTP-01 validation through the public edge.
        Set ``defaults.acmeDnsProvider`` to an empty value to retain HTTP-01.
        """

        if "acmeDnsProvider" in self.defaults:
            return str(self.defaults.get("acmeDnsProvider") or "").strip()
        dns = self.dns
        zone = str(dns.get("zone") or "").strip().strip(".")
        if dns.get("provider") == "cloudflare" and zone and zone != "example.com":
            return "cloudflare"
        return ""

    @property
    def acme_domains(self) -> List[str]:
        configured = self.defaults.get("acmeDomains")
        if configured is not None:
            if not isinstance(configured, list):
                raise LumaError("defaults.acmeDomains must be a list")
            return [str(value).strip().strip(".") for value in configured if str(value).strip()]
        zone = str(self.dns.get("zone") or "").strip().strip(".")
        return [zone] if self.acme_dns_provider and zone and zone != "example.com" else []

    @property
    def entrypoint(self) -> str:
        return str(self.defaults.get("entrypoint", "websecure"))

    @property
    def dns(self) -> Dict[str, Any]:
        providers = self.raw.get("providers") or {}
        dns = dict(providers.get("dns") or self.raw.get("dns") or {})
        if "type" in dns and "provider" not in dns:
            dns["provider"] = dns["type"]
        if "provider" not in dns and dns:
            dns["provider"] = "cloudflare"
        return dns

    @property
    def git(self) -> Dict[str, Any]:
        return dict(self.raw.get("git") or {})

    @property
    def nodes(self) -> Dict[str, NodeConfig]:
        nodes_raw = self.raw.get("nodes") or {}
        nodes: Dict[str, NodeConfig] = {}
        for name, value in nodes_raw.items():
            if not isinstance(value, dict):
                continue
            roles = value.get("roles") or []
            nodes[str(name)] = NodeConfig(
                name=str(name),
                host=str(value.get("host") or name),
                public_ip=value.get("publicIp") or value.get("public_ip"),
                region=str(value.get("region", "cn")),
                roles=[str(role) for role in roles],
                raw=dict(value),
            )
        return nodes

    def get_node(self, name: str) -> NodeConfig:
        nodes = self.nodes
        if name not in nodes:
            raise LumaError(f"unknown node: {name}. Add it to luma.yaml nodes.")
        return nodes[name]

    def find_node(self, *, role: Optional[str] = None, region: Optional[str] = None) -> Optional[NodeConfig]:
        for node in self.nodes.values():
            if role and role not in node.roles:
                continue
            if region and region != node.region:
                continue
            return node
        return None

    def default_manager(self) -> Optional[NodeConfig]:
        return (
            self.find_node(role="nomad-manager")
            or self.find_node(role="edge")
            or next(iter(self.nodes.values()), None)
        )

    def default_dns_target(self) -> Optional[str]:
        dns = self.dns
        if dns.get("edgeTarget"):
            return str(dns["edgeTarget"])
        edge = self.find_node(role="edge")
        if edge and edge.public_ip:
            return edge.public_ip
        return None

    def dns_target_for(self, *, exposure: str, region: str) -> Optional[str]:
        dns = self.dns
        if exposure in {"cn-edge", "tailscale-relay", "tcp-relay"}:
            if dns.get("edgeTarget"):
                return str(dns["edgeTarget"])
            edge = self.find_node(role="edge", region="cn") or self.find_node(role="edge")
            return edge.public_ip if edge else None
        if exposure == "external-edge":
            global_edge = self.find_node(role="edge", region=region) or self.find_node(region=region)
            return global_edge.public_ip if global_edge else None
        return self.default_dns_target()


def load_config(path: Optional[Path]) -> LumaConfig:
    if path:
        return LumaConfig(load_yaml(path), path)
    default = Path("luma.yaml")
    if default.exists():
        return LumaConfig(load_yaml(default), default)
    return LumaConfig({}, None)


def save_config(config: LumaConfig) -> None:
    if not config.path:
        raise LumaError("cannot save config: no luma.yaml path")
    write_yaml(config.path, config.raw)
