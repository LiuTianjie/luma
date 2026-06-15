from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import LumaError
from .io import load_yaml


VALID_REGIONS = {"cn", "global", "home"}
VALID_EXPOSURES = {"none", "cn-edge", "tailscale-relay", "cloudflare-tunnel", "external-edge", "tcp-relay"}
VALID_ACCESS_MODES = {"ReadWriteOnce", "ReadWriteMany"}
VALID_ENGINES = {"nomad"}
TCP_RELAY_RESERVED_PORTS = {80, 443}
SERVICE_FIELDS = {
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
    "region",
    "relay",
    "replicas",
    "resources",
    "routePath",
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
class ServiceVolumeStorageSpec:
    name: str
    storage_class: str
    path: Optional[str] = None
    access_mode: str = "ReadWriteOnce"
    initialize: Optional[str] = None
    adopted: bool = False
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
    storage: Dict[str, ServiceVolumeStorageSpec] = field(default_factory=dict)
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
        if self.region == "global":
            return "global-internal-service"
        if self.region == "home":
            return "home-internal-service"
        return "internal-cn-service"


def tcp_relay_publish_port(service: ServiceSpec) -> int:
    port = int(service.publish_port or service.port or 0)
    if port < 1:
        raise LumaError("tcp-relay requires a valid port")
    return port


def tcp_entrypoint_name(port: int) -> str:
    if int(port) < 1:
        raise LumaError("tcp-relay requires a valid port")
    return f"tcp-{int(port)}"


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
    if not isinstance(image, str) or not image.strip():
        raise LumaError("service manifest requires string field: image")
    if region not in VALID_REGIONS:
        raise LumaError(f"service region must be one of {sorted(VALID_REGIONS)}")
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
    if exposure == "cn-edge" and region != "cn":
        raise LumaError("exposure=cn-edge requires region=cn")
    if exposure == "external-edge" and region != "global":
        raise LumaError("exposure=external-edge requires region=global")
    if exposure == "tailscale-relay" and region != "home":
        raise LumaError("exposure=tailscale-relay requires region=home")

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

    constraints = raw.get("constraints") or []
    labels = raw.get("labels") or []
    networks = raw.get("networks") or []
    volumes = raw.get("volumes") or []
    storage = _load_service_storage(raw.get("storage") or {}, volumes)
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
    return ServiceSpec(
        source=path,
        name=name.strip(),
        image=image.strip(),
        region=region,
        node=node.strip() if isinstance(node, str) else None,
        node_id=None,
        node_platform=None,
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
        storage=storage,
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
    )


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


def _load_service_storage(raw: Any, volumes: List[str]) -> Dict[str, ServiceVolumeStorageSpec]:
    if not isinstance(raw, dict):
        raise LumaError("storage must be a mapping")
    named_volumes = _named_volume_sources(volumes)
    result: Dict[str, ServiceVolumeStorageSpec] = {}
    for name, value in raw.items():
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise LumaError(f"storage.{name} must be a mapping")
        volume_name = str(name)
        if volume_name not in named_volumes:
            raise LumaError(f"storage.{name} references unknown named volume")
        storage_class = str(value.get("storageClass") or "").strip()
        if not storage_class:
            raise LumaError(f"storage.{name}.storageClass is required")
        access_mode = str(value.get("accessMode") or "ReadWriteOnce")
        if access_mode not in VALID_ACCESS_MODES:
            raise LumaError(f"storage.{name}.accessMode must be one of {sorted(VALID_ACCESS_MODES)}")
        initialize = value.get("initialize")
        if initialize is not None and str(initialize) != "empty":
            raise LumaError(f"storage.{name}.initialize only supports: empty")
        result[volume_name] = ServiceVolumeStorageSpec(
            name=volume_name,
            storage_class=storage_class,
            path=str(value["path"]).strip().strip("/") if value.get("path") else None,
            access_mode=access_mode,
            initialize=str(initialize) if initialize is not None else None,
            adopted=bool(value.get("adopted", False)),
            raw=dict(value),
        )
    return result


def _named_volume_sources(volumes: List[str]) -> set[str]:
    names: set[str] = set()
    for spec in volumes:
        source = spec.split(":", 1)[0].strip()
        if not source or source.startswith("/") or source.startswith(".") or source == "~":
            continue
        names.add(source)
    return names
