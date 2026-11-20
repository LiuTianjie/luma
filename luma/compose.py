from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cdn import CdnSpec, parse_cdn_block, validate_cdn_exposure
from .config import LumaConfig
from .container_dns import parse_dns_servers, parse_extra_hosts
from .dns import cert_resolver_for
from .errors import LumaError
from .dependencies import validate_requirement_block
from .io import dump_yaml, load_yaml
from .regions import parse_region_name, validate_region_exposure_syntax
from .service import VALID_EXPOSURES, parse_redirect_from, slugify, tcp_entrypoint_name


VALID_ACCESS_MODES = {"ReadWriteOnce", "ReadWriteMany"}


@dataclass(frozen=True)
class ComposeVolumeSpec:
    name: str
    path: Optional[str] = None
    access_mode: str = "ReadWriteOnce"
    local_node: Optional[str] = None
    local_path: Optional[str] = None
    initialize: Optional[str] = None
    adopted: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        if self.local_node or self.local_path:
            return "local"
        return "unmanaged"


@dataclass(frozen=True)
class ComposeServiceSpec:
    name: str
    region: Optional[str] = None
    node: Optional[str] = None
    exposure: str = "none"
    domain: Optional[str] = None
    port: Optional[int] = None
    publish_port: Optional[int] = None
    replicas: Optional[int] = None
    proxy: bool = False
    relay: Dict[str, Any] = field(default_factory=dict)
    tunnel: Dict[str, Any] = field(default_factory=dict)
    tcp: Dict[str, Any] = field(default_factory=dict)
    requirements: Dict[str, Any] = field(default_factory=dict)
    cdn: Optional[CdnSpec] = None
    redirect_from: List[str] = field(default_factory=list)
    redirect_except: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeDeploymentSpec:
    source: Path
    compose_path: Path
    compose: Dict[str, Any]
    name: str
    region: str
    volumes: Dict[str, ComposeVolumeSpec]
    services: Dict[str, ComposeServiceSpec]
    warnings: List[str] = field(default_factory=list)
    network_image: Optional[str] = None

    @property
    def slug(self) -> str:
        return slugify(self.name)


def load_compose_deployment(
    path: Path,
    *,
    allow_build_services: bool = False,
) -> ComposeDeploymentSpec:
    sidecar = load_yaml(path)
    name = sidecar.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LumaError("compose deployment requires string field: name")
    region = parse_region_name(sidecar.get("region", "cn"))
    compose_value = sidecar.get("compose", "docker-compose.yml")
    if not isinstance(compose_value, str) or not compose_value.strip():
        raise LumaError("compose deployment requires string field: compose")
    compose_path = Path(compose_value)
    if not compose_path.is_absolute():
        compose_path = path.parent / compose_path
    compose = load_yaml(compose_path)
    if not isinstance(compose.get("services"), dict) or not compose["services"]:
        raise LumaError(f"{compose_path} requires a non-empty services mapping")

    if sidecar.get("storageClasses") is not None and sidecar.get("storageClasses") != {}:
        raise LumaError("storageClasses are not supported; use local storage instead")
    volumes = _load_compose_volumes(sidecar.get("volumes") or {})
    services = _load_compose_services(sidecar.get("services") or {})
    warnings = validate_compose_deployment_data(
        compose,
        volumes,
        services,
        default_region=str(region),
        allow_build_services=allow_build_services,
    )
    return ComposeDeploymentSpec(
        source=path,
        compose_path=compose_path,
        compose=compose,
        name=name.strip(),
        region=str(region),
        volumes=volumes,
        services=services,
        warnings=warnings,
    )


def compose_stack_path(config: LumaConfig, deployment: ComposeDeploymentSpec) -> Path:
    return config.stack_root / "compose" / deployment.slug / f"{deployment.slug}.nomad.json"


def compose_route_path(config: LumaConfig, deployment: ComposeDeploymentSpec, service_name: str) -> Path:
    return config.routes_root / f"{deployment.slug}-{slugify(service_name)}.yml"


def render_compose_routes(config: LumaConfig, deployment: ComposeDeploymentSpec) -> Dict[str, str]:
    routes: Dict[str, str] = {}
    for service_name, override in deployment.services.items():
        if override.exposure not in {"tailscale-relay", "tcp-relay"}:
            continue
        if not override.domain or not override.port:
            continue
        service_id = f"{deployment.slug}-{slugify(service_name)}"
        if override.exposure == "tcp-relay":
            tcp_entrypoint = tcp_entrypoint_name(int(override.publish_port or override.port or 0))
            addresses = override.tcp.get("addresses")
            if isinstance(addresses, list) and addresses:
                servers = [{"address": str(address)} for address in addresses]
            else:
                address = override.tcp.get("address")
                if not address:
                    host = override.tcp.get("host") or override.node or f"{deployment.slug}_{service_name}"
                    port = override.tcp.get("port", override.publish_port or override.port)
                    address = f"{host}:{port}"
                servers = [{"address": str(address)}]
            route = {
                "tcp": {
                    "routers": {
                        service_id: {
                            "rule": "HostSNI(`*`)",
                            "entryPoints": [tcp_entrypoint],
                            "service": service_id,
                        }
                    },
                    "services": {service_id: {"loadBalancer": {"servers": servers}}},
                }
            }
            routes[service_name] = dump_yaml(route)
            continue
        upstream_urls = override.relay.get("urls")
        if isinstance(upstream_urls, list) and upstream_urls:
            servers = [{"url": str(url)} for url in upstream_urls]
        else:
            upstream_url = override.relay.get("url")
            if not upstream_url:
                scheme = override.relay.get("scheme", "http")
                host = override.relay.get("host") or override.node or f"{deployment.slug}_{service_name}"
                port = override.relay.get("port", override.publish_port or override.port)
                upstream_url = f"{scheme}://{host}:{port}"
            servers = [{"url": upstream_url}]
        route = {
            "http": {
                "routers": {
                    service_id: {
                        "rule": f"Host(`{override.domain}`)",
                        # Same recovery guarantee as native relay routes: a
                        # stale Nomad-provider router for this Host must not win.
                        "priority": int(override.relay.get("priority", 1000)),
                        "entryPoints": [config.entrypoint],
                        "tls": {"certResolver": cert_resolver_for(config, str(override.domain))},
                        "service": service_id,
                    }
                },
                "services": {service_id: {"loadBalancer": {"servers": servers}}},
            }
        }
        routes[service_name] = dump_yaml(route)
    return routes


def compose_public_services(deployment: ComposeDeploymentSpec) -> List[ComposeServiceSpec]:
    return [service for service in deployment.services.values() if service.exposure != "none"]


def storage_summary(deployment: ComposeDeploymentSpec, *, node_records: Dict[str, Any] | None = None) -> Dict[str, Any]:
    unmanaged = sorted(
        volume_name
        for volume_name in _compose_declared_volume_names(deployment.compose)
        if volume_name not in deployment.volumes
    )
    return {
        "volumes": [
            {
                "name": item.name,
                "kind": item.kind,
                "accessMode": item.access_mode,
                "node": item.local_node or "",
                "path": item.path or item.local_path or "",
                "initialize": item.initialize or "",
                "adopted": item.adopted,
            }
            for item in deployment.volumes.values()
        ],
        "unmanagedVolumes": unmanaged,
        "warnings": list(deployment.warnings),
    }


def init_compose_sidecar(compose_path: Path, output: Path) -> Dict[str, Any]:
    compose = load_yaml(compose_path)
    services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    volumes = compose.get("volumes") if isinstance(compose.get("volumes"), dict) else {}
    data: Dict[str, Any] = {
        "name": slugify(compose_path.parent.name or "app-stack"),
        "compose": str(compose_path),
        "region": "cn",
        "volumes": {str(name): {} for name in volumes.keys()},
        "services": {str(name): {"region": "cn", "exposure": "none"} for name in services.keys()},
    }
    output.write_text(dump_yaml(data), encoding="utf-8")
    return data


def validate_compose_deployment_data(
    compose: Dict[str, Any],
    volumes: Dict[str, ComposeVolumeSpec],
    services: Dict[str, ComposeServiceSpec],
    *,
    default_region: str,
    allow_build_services: bool = False,
) -> List[str]:
    warnings: List[str] = []
    compose_services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    for service_name, service in compose_services.items():
        if not isinstance(service, dict):
            raise LumaError(f"compose service {service_name} must be a mapping")
        parse_dns_servers(service.get("dns"), field_name=f"compose service {service_name} dns")
        parse_extra_hosts(
            service.get("extra_hosts"),
            field_name=f"compose service {service_name} extra_hosts",
            allow_placeholders=True,
        )
        if not isinstance(service.get("image"), str) or not service.get("image"):
            if service.get("build") is not None and allow_build_services:
                warnings.append(
                    f"compose service {service_name} uses build; luma import will build it, but direct compose deploy requires an image"
                )
            else:
                raise LumaError(
                    f"compose service {service_name} requires image; direct compose deploy does not build images. "
                    "Use luma compose validate --import-mode for repository import checks."
                )
    for service_name in services:
        if service_name not in compose_services:
            raise LumaError(f"luma.compose.yml references unknown compose service: {service_name}")
    compose_replica_count(compose, services, volumes)
    declared_volume_names = _compose_declared_volume_names(compose)
    for volume_name in volumes:
        if declared_volume_names and volume_name not in declared_volume_names:
            warnings.append(f"volume {volume_name} is declared in Luma sidecar but not in compose top-level volumes")
    # Named volumes use the deployment node's Docker storage.
    # Control persists that node before submitting the first job.
    usage = _compose_service_volume_usage(compose)
    local_nodes = _local_volume_nodes(volumes)
    for service_name, used_volumes in usage.items():
        nodes = {local_nodes[name] for name in used_volumes if name in local_nodes}
        override = services.get(service_name)
        if override and override.node:
            nodes.add(override.node)
        if len(nodes) > 1:
            raise LumaError(f"compose service {service_name} has conflicting local volume nodes: {sorted(nodes)}")
    for service_name, override in services.items():
        region = parse_region_name(override.region or default_region)
        if override.exposure not in VALID_EXPOSURES:
            raise LumaError(f"compose service {service_name} exposure must be one of {sorted(VALID_EXPOSURES)}")
        if override.exposure == "cloudflare-tunnel":
            # render_compose_job has no cloudflared sidecar path (only native
            # render_nomad_job does). Without this guard a tunnel compose service
            # passes validation, deploys "successfully", yet renders no tunnel, no
            # port, no route — silently unreachable. Fail fast instead.
            raise LumaError(
                f"compose service {service_name} exposure=cloudflare-tunnel is not supported "
                "for compose deployments; deploy it as a native luma manifest"
            )
        if override.exposure != "none":
            if not override.domain:
                raise LumaError(f"compose service {service_name} public exposure requires domain")
            if not isinstance(override.port, int):
                raise LumaError(f"compose service {service_name} public exposure requires integer port")
        if override.exposure == "node-edge" and not override.node:
            raise LumaError(f"compose service {service_name} exposure=node-edge requires node")
        validate_cdn_exposure(override.cdn, override.exposure, label=f"services.{service_name}.cdn")
        try:
            validate_region_exposure_syntax(region, override.exposure)
        except LumaError as exc:
            raise LumaError(f"compose service {service_name} {exc}") from exc
    tcp_ports: dict[int, str] = {}
    for service_name, override in services.items():
        if override.exposure != "tcp-relay":
            continue
        port = int(override.publish_port or override.port or 0)
        if port in {80, 443}:
            raise LumaError(f"compose service {service_name} tcp-relay cannot use reserved Traefik port: {port}")
        if port in tcp_ports:
            raise LumaError(f"compose tcp-relay publishPort {port} is already used by service {tcp_ports[port]}")
        tcp_ports[port] = service_name
    container_ports: dict[int, str] = {}
    published_ports: dict[int, str] = {}
    for service_name, override in services.items():
        if override.port is not None:
            container_port = int(override.port)
            if container_port in container_ports:
                raise LumaError(
                    f"compose services {container_ports[container_port]} and {service_name} "
                    f"share a network namespace and cannot both use container port {container_port}"
                )
            container_ports[container_port] = service_name
        published: int | None = None
        if override.publish_port is not None:
            published = int(override.publish_port)
        elif override.exposure in {"tailscale-relay", "tcp-relay"} and override.port is not None:
            published = int(override.port)
        if published is not None:
            if published in published_ports:
                raise LumaError(
                    f"compose services {published_ports[published]} and {service_name} "
                    f"cannot both publish host port {published}"
                )
            published_ports[published] = service_name
    return warnings


def _replica_count(value: Any, field_name: str) -> int:
    # YAML floats and bools must not be silently truncated/coerced by int().
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise LumaError(f"{field_name} must be a positive integer")
    return _positive_int(value, field_name)


def compose_replica_count(
    compose: Dict[str, Any], services: Dict[str, ComposeServiceSpec],
    volumes: Dict[str, ComposeVolumeSpec] | None = None,
) -> int:
    """Resolve one executable replica contract for validation and rendering.

    Compose siblings share loopback inside one Nomad group. Independent service
    counts therefore need separate deployments, not a larger Count on that group.
    """
    counts: Dict[str, int] = {}
    for name, body in (compose.get("services") or {}).items():
        if not isinstance(body, dict):
            raise LumaError(f"compose service {name} must be a mapping")
        deploy = body.get("deploy", {})
        if not isinstance(deploy, dict):
            raise LumaError(f"compose service {name} deploy must be a mapping")
        if deploy.get("mode", "replicated") != "replicated":
            raise LumaError(f"compose service {name} deploy.mode must be replicated; global and job modes are not supported")
        declared: Dict[str, int] = {}
        for key, raw in (("deploy.replicas", deploy), ("scale", body)):
            field = key.split(".")[-1]
            if field in raw:
                declared[key] = _replica_count(raw[field], f"services.{name}.{key}")
        override = services.get(str(name))
        if override is not None and override.replicas is not None:
            declared["sidecar replicas"] = _replica_count(override.replicas, f"services.{name}.replicas")
        if len(set(declared.values())) > 1:
            detail = ", ".join(f"{key}={value}" for key, value in declared.items())
            raise LumaError(f"compose service {name} has conflicting replica counts: {detail}; make all declarations agree")
        counts[str(name)] = next(iter(declared.values()), 1)

    if not any(count > 1 for count in counts.values()):
        return 1
    if len(counts) != 1:
        raise LumaError(
            "compose replicas > 1 requires exactly one service; multi-service stacks share a Nomad "
            "network namespace and require replicas: 1 for every service. Use separate deployments to scale workers"
        )
    if volumes:
        raise LumaError("compose replicas > 1 cannot declare sidecar volumes; use a mount-free deployment")
    name = next(iter(counts))
    body = compose["services"][name]
    override = services.get(name)
    if body.get("volumes"):
        raise LumaError(f"compose service {name} replicas > 1 does not support volume mounts (including read-only mounts); use a mount-free deployment")
    if body.get("network_mode", "bridge") != "bridge":
        raise LumaError(f"compose service {name} replicas > 1 requires bridge networking")
    if body.get("container_name") or body.get("ports"):
        raise LumaError(f"compose service {name} replicas > 1 cannot set container_name or Compose ports; use dynamic Luma edge ports")
    if override and (override.publish_port is not None or override.exposure in {"tailscale-relay", "tcp-relay", "cloudflare-tunnel"}):
        raise LumaError(f"compose service {name} replicas > 1 requires exposure none or a dynamic edge port without publishPort")
    for field in ("placement", "update_config", "rollback_config"):
        if (body.get("deploy") or {}).get(field):
            raise LumaError(
                f"compose service {name} replicas > 1 does not support deploy.{field}; "
                "use Luma region/node placement and the managed rolling update policy"
            )
    return counts[name]


def _load_compose_volumes(raw: Any) -> Dict[str, ComposeVolumeSpec]:
    if not isinstance(raw, dict):
        raise LumaError("volumes must be a mapping")
    result: Dict[str, ComposeVolumeSpec] = {}
    for name, value in raw.items():
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise LumaError(f"volumes.{name} must be a mapping")
        if value.get("storageClass"):
            raise LumaError("storageClasses are not supported; use local storage instead")
        local = value.get("local") or {}
        if local and not isinstance(local, dict):
            raise LumaError(f"volumes.{name}.local must be a mapping")
        access_mode = str(value.get("accessMode") or "ReadWriteOnce")
        if access_mode not in VALID_ACCESS_MODES:
            raise LumaError(f"volumes.{name}.accessMode must be one of {sorted(VALID_ACCESS_MODES)}")
        initialize = value.get("initialize")
        if initialize is not None and str(initialize) != "empty":
            raise LumaError(f"volumes.{name}.initialize only supports: empty")
        adopted = bool(value.get("adopted", False))
        result[str(name)] = ComposeVolumeSpec(
            name=str(name),
            path=str(value["path"]).strip().strip("/") if value.get("path") else None,
            access_mode=access_mode,
            local_node=str(local["node"]).strip() if local.get("node") else None,
            local_path=str(local["path"]).strip() if local.get("path") else None,
            initialize=str(initialize) if initialize is not None else None,
            adopted=adopted,
            raw=dict(value),
        )
        if result[str(name)].kind == "local" and not result[str(name)].local_path:
            raise LumaError(f"volumes.{name}.local requires path; node defaults to the deployment node")
        if result[str(name)].kind == "local":
            local_path = Path(str(result[str(name)].local_path))
            if not local_path.is_absolute() or ".." in local_path.parts or local_path == Path("/"):
                raise LumaError(
                    f"volumes.{name}.local.path must be an absolute path other than / without .."
                )
    return result


def _load_compose_services(raw: Any) -> Dict[str, ComposeServiceSpec]:
    if not isinstance(raw, dict):
        raise LumaError("services must be a mapping")
    result: Dict[str, ComposeServiceSpec] = {}
    for name, value in raw.items():
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise LumaError(f"services.{name} must be a mapping")
        relay = value.get("relay") or {}
        tunnel = value.get("tunnel") or {}
        tcp = value.get("tcp") or {}
        requirements = validate_requirement_block(value.get("requirements") or {}, label=f"services.{name}.requirements")
        if not isinstance(relay, dict):
            raise LumaError(f"services.{name}.relay must be a mapping")
        if not isinstance(tunnel, dict):
            raise LumaError(f"services.{name}.tunnel must be a mapping")
        if not isinstance(tcp, dict):
            raise LumaError(f"services.{name}.tcp must be a mapping")
        redirect = parse_redirect_from(
            value.get("redirectFrom"),
            domain=value.get("domain"),
            exposure=str(value.get("exposure") or "none"),
            label=f"services.{name}.redirectFrom",
        )
        result[str(name)] = ComposeServiceSpec(
            name=str(name),
            region=parse_region_name(value["region"]) if value.get("region") else None,
            node=str(value["node"]).strip() if value.get("node") else None,
            exposure=str(value.get("exposure") or "none"),
            domain=str(value["domain"]).strip() if value.get("domain") else None,
            port=_positive_int(value["port"], f"services.{name}.port") if value.get("port") is not None else None,
            publish_port=_positive_int(value["publishPort"], f"services.{name}.publishPort") if value.get("publishPort") is not None else None,
            replicas=_replica_count(value["replicas"], f"services.{name}.replicas") if "replicas" in value else None,
            proxy=bool(value.get("proxy", False)),
            relay=dict(relay),
            tunnel=dict(tunnel),
            tcp=dict(tcp),
            requirements=requirements,
            cdn=parse_cdn_block(value.get("cdn"), label=f"services.{name}.cdn"),
            redirect_from=redirect[0],
            redirect_except=redirect[1],
            raw=dict(value),
        )
    return result


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


def _compose_declared_volume_names(compose: Dict[str, Any]) -> set[str]:
    names = set()
    top_volumes = compose.get("volumes")
    if isinstance(top_volumes, dict):
        names.update(str(name) for name in top_volumes.keys())
    for used in _compose_service_volume_usage(compose).values():
        names.update(used)
    return names


def _compose_service_volume_usage(compose: Dict[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    mounts = _compose_service_volume_mounts(compose)
    for service_name, values in mounts.items():
        result[service_name] = [source for source, _target in values]
    return result


def _compose_service_volume_mounts(compose: Dict[str, Any]) -> Dict[str, List[tuple[str, str]]]:
    result: Dict[str, List[tuple[str, str]]] = {}
    services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        used: List[tuple[str, str]] = []
        for volume in service.get("volumes") or []:
            source = _volume_source(volume)
            if source and _is_named_volume(source):
                target = _volume_target(volume)
                used.append((source, target))
        result[str(service_name)] = used
    return result


def _volume_source(volume: Any) -> str:
    if isinstance(volume, str):
        source = volume.split(":", 1)[0].strip()
        return source
    if isinstance(volume, dict):
        if volume.get("type") == "volume" or "source" in volume:
            return str(volume.get("source") or "").strip()
    return ""


def _volume_target_and_suffix(volume: Any) -> tuple[str, str]:
    if isinstance(volume, str):
        parts = volume.split(":")
        if len(parts) < 2:
            raise LumaError(f"volume {volume} does not include a target path")
        target = parts[1]
        suffix = ":" + ":".join(parts[2:]) if len(parts) > 2 else ""
        return target, suffix
    if isinstance(volume, dict):
        target = str(volume.get("target") or "").strip()
        if not target:
            raise LumaError("volume mapping requires target")
        read_only = volume.get("read_only")
        return target, ":ro" if read_only else ""
    raise LumaError("unsupported volume entry")


def _volume_target(volume: Any) -> str:
    try:
        target, _suffix = _volume_target_and_suffix(volume)
        return target.rstrip("/")
    except LumaError:
        return ""


def _is_named_volume(source: str) -> bool:
    return bool(source and not source.startswith("/") and not source.startswith(".") and source != "~")


def _local_volume_nodes(volumes: Dict[str, ComposeVolumeSpec]) -> Dict[str, str]:
    return {name: spec.local_node or "" for name, spec in volumes.items() if spec.kind == "local" and spec.local_node}
