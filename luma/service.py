from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .cdn import CdnSpec, parse_cdn_block, validate_cdn_exposure
from .container_dns import parse_dns_servers
from .errors import LumaError
from .dependencies import validate_requirement_block
from .io import load_yaml
from .regions import parse_region_name, validate_region_exposure_syntax

VALID_EXPOSURES = {"none", "cn-edge", "tailscale-relay", "cloudflare-tunnel", "external-edge", "tcp-relay", "node-edge"}
VALID_ENGINES = {"nomad"}
TCP_RELAY_RESERVED_PORTS = {80, 443}
SERVICE_FIELDS = {
    "build",
    "cdn",
    "command",
    "constraints",
    "dns",
    "domain",
    "engine",
    "env",
    "environment",
    "exposure",
    "healthcheck",
    "image",
    "labels",
    "name",
    "networks",
    "node",
    "port",
    "proxy",
    "public",
    "publishPort",
    "redirectFrom",
    "region",
    "relay",
    "replicas",
    "resources",
    "routePath",
    "requirements",
    "stackPath",
    "storage",
    "tcp",
    "tunnel",
    "volumes",
}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise LumaError("service name must contain at least one letter or number")
    return slug


@dataclass(frozen=True)
class ServiceBuildSpec:
    context: str = "."
    dockerfile: str = "Dockerfile"
    platform: str = "linux/amd64"
    repo: Optional[str] = None
    target: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceSpec:
    source: Path
    name: str
    image: str
    region: str
    node: Optional[str] = None
    node_id: Optional[str] = None
    node_platform: Optional[str] = None
    build: Optional[ServiceBuildSpec] = None
    public: bool = False
    exposure: str = "none"
    domain: Optional[str] = None
    port: Optional[int] = None
    publish_port: Optional[int] = None
    replicas: int = 1
    command: Optional[Any] = None
    environment: Dict[str, Any] = field(default_factory=dict)
    constraints: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)
    volumes: List[str] = field(default_factory=list)
    resources: Dict[str, Any] = field(default_factory=dict)
    healthcheck: Dict[str, Any] = field(default_factory=dict)
    stack_path: Optional[Path] = None
    route_path: Optional[Path] = None
    dns: Dict[str, Any] = field(default_factory=dict)
    relay: Dict[str, Any] = field(default_factory=dict)
    tunnel: Dict[str, Any] = field(default_factory=dict)
    tcp: Dict[str, Any] = field(default_factory=dict)
    proxy: bool = False
    engine: str = ""
    requirements: Dict[str, Any] = field(default_factory=dict)
    cdn: Optional[CdnSpec] = None
    # Extra hostnames that redirect GET/HEAD to ``domain`` and pass every other
    # method straight to the service (webhooks keep working). Never on the CDN.
    redirect_from: List[str] = field(default_factory=list)
    # Paths on those hosts served directly for every method (no redirect).
    redirect_except: List[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def service_kind(self) -> str:
        if self.exposure == "cn-edge":
            return "public-cn-service"
        if self.exposure == "external-edge":
            return "public-global-service"
        if self.exposure == "tailscale-relay":
            return "home-tailscale-relay"
        if self.exposure == "cloudflare-tunnel":
            return "cloudflare-tunnel-service"
        if self.exposure == "tcp-relay":
            return "tcp-relay-service"
        if self.exposure == "node-edge":
            return "node-edge-service"
        if self.region == "global":
            return "global-internal-service"
        if self.region == "home":
            return "home-internal-service"
        if self.region == "cn":
            return "internal-cn-service"
        return "internal-region-service"


def tcp_relay_publish_port(service: ServiceSpec) -> int:
    port = int(service.publish_port or service.port or 0)
    if port < 1:
        raise LumaError("tcp-relay requires a valid port")
    return port


def tcp_entrypoint_name(port: int) -> str:
    if int(port) < 1:
        raise LumaError("tcp-relay requires a valid port")
    return f"tcp-{int(port)}"


_FORBIDDEN_INTERNAL_TARGETS = ("api@internal", "dashboard@internal", "traefik@internal")
_FORBIDDEN_ROUTER_PROPS = frozenset({"rule", "priority", "service", "entrypoints"})


def validate_service_labels(labels: List[str], service_slug: str) -> None:
    """Validate service.labels to prevent Traefik router hijacking and dashboard exposure."""
    expected_slug = service_slug.strip().lower()
    for raw_label in labels:
        label = str(raw_label)
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
            raise LumaError("service label must not contain newlines or control characters")
        lowered = label.lower()
        for forbidden in _FORBIDDEN_INTERNAL_TARGETS:
            if forbidden in lowered:
                raise LumaError(f"service label must not reference {forbidden}")
        key, sep, value = label.partition("=")
        key_lower = key.strip().lower()
        value_lower = value.strip().lower()
        if key_lower == "luma.edge":
            raise LumaError("service label must not override luma.edge")
        if not (key_lower.startswith("traefik.") or key_lower.startswith("lumaedge.")):
            continue
        if not sep:
            raise LumaError(f"Traefik service label must use key=value syntax: {label!r}")
        parts = key_lower.split(".")
        # Expected shape: <prefix>.<proto>.<section>.<name>.<prop...>
        if len(parts) >= 3 and parts[1] in {"http", "tcp", "udp"}:
            section = parts[2]
            if section == "routers":
                if len(parts) < 5 or not parts[3] or not parts[4]:
                    raise LumaError(f"invalid Traefik router label: {label!r}")
                router_name = parts[3]
                prop = parts[4]
                if router_name != expected_slug:
                    raise LumaError(
                        f"service label cannot define or modify Traefik router {router_name!r}; "
                        f"only router {expected_slug!r} is allowed"
                    )
                if prop in _FORBIDDEN_ROUTER_PROPS:
                    raise LumaError(
                        f"service label cannot override Traefik router property {prop!r}"
                    )
            elif section == "services":
                if len(parts) < 5 or not parts[3] or not parts[4]:
                    raise LumaError(f"invalid Traefik service label: {label!r}")
                target_service = parts[3]
                if target_service != expected_slug:
                    raise LumaError(
                        f"service label cannot modify Traefik service {target_service!r}; "
                        f"only service {expected_slug!r} is allowed"
                    )
                rest = ".".join(parts[4:])
                if rest == "loadbalancer.servers" or rest.startswith("loadbalancer.servers.") or rest.startswith("loadbalancer.servers["):
                    raise LumaError("service label cannot override Traefik loadBalancer.servers")
        if "@internal" in value_lower:
            raise LumaError("service label must not reference Traefik @internal providers")


def load_service(path: Path) -> ServiceSpec:
    raw = load_yaml(path)
    unknown_fields = sorted(str(key) for key in raw if str(key) not in SERVICE_FIELDS)
    if unknown_fields:
        raise LumaError(f"unsupported service manifest field(s): {', '.join(unknown_fields)}")
    name = raw.get("name")
    image = raw.get("image")
    region = raw.get("region")
    if not isinstance(name, str) or not name.strip():
        raise LumaError("service manifest requires string field: name")
    build = _load_service_build(raw.get("build"), slugify(name))
    has_image = isinstance(image, str) and bool(image.strip())
    if not has_image and build is None:
        raise LumaError("service manifest requires string field: image (or a build block)")
    if not has_image:
        image = ""
    region = parse_region_name(region)
    node = raw.get("node")
    if node is not None and (not isinstance(node, str) or not node.strip()):
        raise LumaError("node must be a non-empty string when provided")

    explicit_exposure = raw.get("exposure")
    if explicit_exposure is None:
        if "public" in raw:
            raise LumaError("public is no longer supported; use exposure")
        exposure = "none"
    else:
        exposure = str(explicit_exposure)
    if exposure not in VALID_EXPOSURES:
        raise LumaError(f"exposure must be one of {sorted(VALID_EXPOSURES)}")

    engine = str(raw.get("engine") or "")
    if engine and engine not in VALID_ENGINES:
        raise LumaError(f"engine must be one of {sorted(VALID_ENGINES)}")

    public = exposure != "none"

    domain = raw.get("domain")
    port = raw.get("port")
    if public:
        if not isinstance(domain, str) or not domain.strip():
            raise LumaError("public service requires string field: domain")
        if not isinstance(port, int):
            raise LumaError("public service requires integer field: port")
    validate_region_exposure_syntax(region, exposure)
    if exposure == "node-edge" and not node:
        raise LumaError("exposure=node-edge requires node: the node whose public address serves the domain")
    cdn = parse_cdn_block(raw.get("cdn"))
    validate_cdn_exposure(cdn, exposure)
    redirect_from, redirect_except = parse_redirect_from(raw.get("redirectFrom"), domain=domain, exposure=exposure)

    relay = raw.get("relay") or {}
    if not isinstance(relay, dict):
        raise LumaError("relay must be a mapping")
    tunnel = raw.get("tunnel") or {}
    if not isinstance(tunnel, dict):
        raise LumaError("tunnel must be a mapping")
    tcp = raw.get("tcp") or {}
    if not isinstance(tcp, dict):
        raise LumaError("tcp must be a mapping")
    publish_port = _positive_int(raw["publishPort"], "publishPort") if "publishPort" in raw else None
    if exposure == "tcp-relay":
        relay_port = int(publish_port or port or 0)
        if relay_port in TCP_RELAY_RESERVED_PORTS:
            raise LumaError(f"tcp-relay cannot use reserved Traefik port: {relay_port}")

    replicas = _positive_int(raw.get("replicas", 1), "replicas")
    if replicas < 1:
        raise LumaError("replicas must be >= 1")

    environment = raw.get("env") or raw.get("environment") or {}
    if not isinstance(environment, dict):
        raise LumaError("env/environment must be a mapping")

    requirements = validate_requirement_block(raw.get("requirements") or {})

    constraints = raw.get("constraints") or []
    labels = raw.get("labels") or []
    networks = raw.get("networks") or []
    volumes = raw.get("volumes") or []
    if raw.get("storage") is not None and raw.get("storage") != {}:
        raise LumaError("storage classes are not supported; use local volumes instead")
    resources = raw.get("resources") or {}
    healthcheck = raw.get("healthcheck") or {}
    for field_name, value in {
        "constraints": constraints,
        "labels": labels,
        "networks": networks,
        "volumes": volumes,
    }.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LumaError(f"{field_name} must be a list of strings")
    validate_service_labels(labels, slugify(name))
    if not isinstance(resources, dict):
        raise LumaError("resources must be a mapping")
    resources = _normalize_service_resources(resources)
    if not isinstance(healthcheck, dict):
        raise LumaError("healthcheck must be a mapping")

    stack_path = raw.get("stackPath")
    route_path = raw.get("routePath")
    dns = raw.get("dns") or {}
    if not isinstance(dns, dict):
        raise LumaError("dns must be a mapping")
    if exposure == "node-edge" and dns.get("target"):
        raise LumaError("dns.target is not allowed with exposure=node-edge; the record always points at the node's public ingress address")
    if "servers" in dns:
        dns = {**dns, "servers": parse_dns_servers(dns["servers"], field_name="dns.servers")}
    return ServiceSpec(
        source=path,
        name=name.strip(),
        image=image.strip(),
        region=region,
        node=node.strip() if isinstance(node, str) else None,
        node_id=None,
        node_platform=None,
        build=build,
        public=public,
        exposure=exposure,
        domain=domain.strip() if isinstance(domain, str) else None,
        port=port,
        publish_port=publish_port,
        replicas=replicas,
        command=raw.get("command"),
        environment=environment,
        constraints=constraints,
        labels=labels,
        networks=networks,
        volumes=volumes,
        resources=resources,
        healthcheck=healthcheck,
        stack_path=Path(stack_path) if stack_path else None,
        route_path=Path(route_path) if route_path else None,
        dns=dns,
        relay=relay,
        tunnel=tunnel,
        tcp=tcp,
        proxy=bool(raw.get("proxy", False)),
        engine=engine,
        requirements=requirements,
        cdn=cdn,
        redirect_from=redirect_from,
        redirect_except=redirect_except,
    )


REDIRECT_EXPOSURES = frozenset({"cn-edge", "external-edge", "tailscale-relay", "node-edge"})
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def parse_redirect_from(
    raw: Any, *, domain: Any, exposure: str, label: str = "redirectFrom"
) -> Tuple[List[str], List[str]]:
    """Return ``(hosts, except_paths)`` for hosts that redirect to ``domain``.

    Accepts a list of hostnames, or ``{hosts: [...], except: [...]}`` where
    ``except`` lists paths (``/api/*`` for a prefix) served directly on those
    hosts for every method, for clients that drop credentials on redirects.
    """
    if raw is None:
        return [], []
    except_raw: Any = []
    if isinstance(raw, Mapping):
        unknown = sorted(str(key) for key in raw if str(key) not in {"hosts", "except"})
        if unknown:
            raise LumaError(f"unsupported {label} field(s): {', '.join(unknown)}")
        except_raw = raw.get("except") or []
        raw = raw.get("hosts")
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw):
        raise LumaError(f"{label} must be a list of hostnames or a mapping with hosts")
    if exposure not in REDIRECT_EXPOSURES:
        raise LumaError(f"{label} requires exposure {', '.join(sorted(REDIRECT_EXPOSURES))}; got {exposure}")
    primary = str(domain or "").strip().lower().rstrip(".")
    hosts: List[str] = []
    for item in raw:
        host = item.strip().lower().rstrip(".")
        if not _HOSTNAME.match(host):
            raise LumaError(f"{label} entries must be hostnames: {item!r}")
        if host == primary:
            raise LumaError(f"{label} must not repeat domain {primary}")
        if host not in hosts:
            hosts.append(host)
    if not isinstance(except_raw, list) or any(not isinstance(item, str) for item in except_raw):
        raise LumaError(f"{label}.except must be a list of URL paths")
    paths: List[str] = []
    for item in except_raw:
        path = item.strip()
        if not path.startswith("/") or "*" in path[:-1] or any(ch.isspace() or ch in "`\"" for ch in path):
            raise LumaError(f"{label}.except entries must be URL paths starting with / and may only end with *: {item!r}")
        if path not in paths:
            paths.append(path)
    return hosts, paths


def _load_service_build(raw: Any, slug: str) -> Optional["ServiceBuildSpec"]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LumaError("build must be a mapping")
    context = str(raw.get("context") or ".").strip() or "."
    dockerfile = str(raw.get("dockerfile") or "Dockerfile").strip() or "Dockerfile"
    platform = str(raw.get("platform") or "linux/amd64").strip() or "linux/amd64"
    repo_value = raw.get("repo")
    repo = str(repo_value).strip() if repo_value is not None and str(repo_value).strip() else None
    unknown = sorted(k for k in raw if k not in {"context", "dockerfile", "platform", "repo", "target", "x-luma-git-contexts"})
    if unknown:
        raise LumaError(f"unsupported build field(s): {', '.join(unknown)}")
    target = build_target(raw)
    if "x-luma-git-contexts" in raw:
        from .build_sources import validate_git_contexts_spec

        validate_git_contexts_spec(raw)
    return ServiceBuildSpec(
        context=context,
        dockerfile=dockerfile,
        platform=platform,
        repo=repo,
        target=target or None,
        raw=dict(raw),
    )


def build_target(spec: Mapping[str, Any]) -> str:
    """Validate a literal Dockerfile stage name shared by both build lanes."""
    if "target" not in spec:
        return ""
    target = spec["target"]
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target):
        raise LumaError("build.target must be a literal Dockerfile stage name")
    return target


def _normalize_service_resources(resources: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for section_name, section in resources.items():
        if section_name not in {"limits", "reservations"}:
            raise LumaError("resources only supports limits and reservations")
        if not isinstance(section, dict):
            raise LumaError(f"resources.{section_name} must be a mapping")
        normalized_section = dict(section)
        if "cpus" in normalized_section and normalized_section["cpus"] is not None:
            cpu_value = normalized_section["cpus"]
            if isinstance(cpu_value, bool) or not isinstance(cpu_value, (str, int, float)):
                raise LumaError(f"resources.{section_name}.cpus must be a string or number")
            normalized_section["cpus"] = str(cpu_value)
        normalized[section_name] = normalized_section
    return normalized


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise LumaError(f"{field_name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LumaError(f"{field_name} must be a positive integer") from exc
    if parsed < 1:
        raise LumaError(f"{field_name} must be a positive integer")
    return parsed
