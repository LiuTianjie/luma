from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import LumaConfig
from .errors import LumaError
from .io import dump_yaml, load_yaml
from .service import VALID_EXPOSURES, VALID_REGIONS, slugify, tcp_entrypoint_name


VALID_STORAGE_PROVIDERS = {"nfs"}
VALID_STORAGE_MODES = {"managed", "external"}
VALID_ACCESS_MODES = {"ReadWriteOnce", "ReadWriteMany"}
DEFAULT_NFS_MOUNT_OPTIONS = "nfsvers=4,rw,soft,timeo=100,retrans=10,noresvport"


@dataclass(frozen=True)
class StorageClassSpec:
    name: str
    provider: str
    mode: str = "managed"
    node: Optional[str] = None
    path: Optional[str] = None
    endpoint: Optional[str] = None
    mount_options: str = DEFAULT_NFS_MOUNT_OPTIONS
    nodes: List[str] = field(default_factory=list)
    regions: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeVolumeSpec:
    name: str
    storage_class: Optional[str] = None
    path: Optional[str] = None
    access_mode: str = "ReadWriteOnce"
    local_node: Optional[str] = None
    local_path: Optional[str] = None
    initialize: Optional[str] = None
    adopted: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        if self.storage_class:
            return "storageClass"
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
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeDeploymentSpec:
    source: Path
    compose_path: Path
    compose: Dict[str, Any]
    name: str
    region: str
    storage_classes: Dict[str, StorageClassSpec]
    volumes: Dict[str, ComposeVolumeSpec]
    services: Dict[str, ComposeServiceSpec]
    warnings: List[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.name)


def load_compose_deployment(
    path: Path,
    *,
    storage_classes: Dict[str, Any] | None = None,
    allow_sidecar_storage_classes: bool = True,
    allow_build_services: bool = False,
) -> ComposeDeploymentSpec:
    sidecar = load_yaml(path)
    name = sidecar.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LumaError("compose deployment requires string field: name")
    region = sidecar.get("region", "cn")
    if region not in VALID_REGIONS:
        raise LumaError(f"compose deployment region must be one of {sorted(VALID_REGIONS)}")
    compose_value = sidecar.get("compose", "docker-compose.yml")
    if not isinstance(compose_value, str) or not compose_value.strip():
        raise LumaError("compose deployment requires string field: compose")
    compose_path = Path(compose_value)
    if not compose_path.is_absolute():
        compose_path = path.parent / compose_path
    compose = load_yaml(compose_path)
    if not isinstance(compose.get("services"), dict) or not compose["services"]:
        raise LumaError(f"{compose_path} requires a non-empty services mapping")

    if storage_classes is not None:
        if not allow_sidecar_storage_classes and sidecar.get("storageClasses"):
            raise LumaError("storageClasses are managed by Luma Control; register them with luma storage set")
        storage_raw = storage_classes
    else:
        storage_raw = sidecar.get("storageClasses") or {}
    storage_classes_loaded = _load_storage_classes(storage_raw)
    volumes = _load_compose_volumes(sidecar.get("volumes") or {}, storage_classes_loaded)
    referenced_storage_classes = {
        volume.storage_class
        for volume in volumes.values()
        if volume.storage_class
    }
    storage_classes_loaded = {
        name: storage_classes_loaded[name]
        for name in sorted(referenced_storage_classes)
        if name in storage_classes_loaded
    }
    services = _load_compose_services(sidecar.get("services") or {})
    warnings = validate_compose_deployment_data(
        compose,
        storage_classes_loaded,
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
        storage_classes=storage_classes_loaded,
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
        upstream_url = override.relay.get("url")
        if not upstream_url:
            scheme = override.relay.get("scheme", "http")
            host = override.relay.get("host") or override.node or f"{deployment.slug}_{service_name}"
            port = override.relay.get("port", override.publish_port or override.port)
            upstream_url = f"{scheme}://{host}:{port}"
        route = {
            "http": {
                "routers": {
                    service_id: {
                        "rule": f"Host(`{override.domain}`)",
                        "entryPoints": [config.entrypoint],
                        "tls": {"certResolver": config.cert_resolver},
                        "service": service_id,
                    }
                },
                "services": {service_id: {"loadBalancer": {"servers": [{"url": upstream_url}]}}},
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
        "storageClasses": [
            {
                "name": item.name,
                "provider": item.provider,
                "mode": item.mode,
                "node": item.node or "",
                "endpoint": item.endpoint or "",
                "path": item.path or "",
                "regions": item.regions,
                "nodes": item.nodes,
            }
            for item in deployment.storage_classes.values()
        ],
        "volumes": [
            {
                "name": item.name,
                "kind": item.kind,
                "storageClass": item.storage_class or "",
                "accessMode": item.access_mode,
                "node": item.local_node or "",
                "path": item.path or item.local_path or "",
                "initialize": item.initialize or "",
                "adopted": item.adopted,
            }
            for item in deployment.volumes.values()
        ],
        "mounts": resolve_storage_mounts(deployment, node_records=node_records) if node_records is not None else [],
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
        "storageClasses": {},
        "volumes": {str(name): {} for name in volumes.keys()},
        "services": {str(name): {"region": "cn", "exposure": "none"} for name in services.keys()},
    }
    output.write_text(dump_yaml(data), encoding="utf-8")
    return data


def validate_compose_deployment_data(
    compose: Dict[str, Any],
    storage_classes: Dict[str, StorageClassSpec],
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
    declared_volume_names = _compose_declared_volume_names(compose)
    for volume_name in volumes:
        if declared_volume_names and volume_name not in declared_volume_names:
            warnings.append(f"volume {volume_name} is declared in Luma sidecar but not in compose top-level volumes")
    for volume_name in sorted(declared_volume_names):
        if volume_name not in volumes:
            warnings.append(
                f"volume {volume_name} is unmanaged by Luma; if the service is rescheduled, Docker may use a different node-local volume"
            )
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
        region = override.region or default_region
        if region not in VALID_REGIONS:
            raise LumaError(f"compose service {service_name} region must be one of {sorted(VALID_REGIONS)}")
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
        if override.exposure == "cn-edge" and region != "cn":
            raise LumaError(f"compose service {service_name} exposure=cn-edge requires region=cn")
        if override.exposure == "external-edge" and region != "global":
            raise LumaError(f"compose service {service_name} exposure=external-edge requires region=global")
        if override.exposure == "tailscale-relay" and region != "home":
            raise LumaError(f"compose service {service_name} exposure=tailscale-relay requires region=home")
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
    return warnings


def _load_storage_classes(raw: Any) -> Dict[str, StorageClassSpec]:
    if not isinstance(raw, dict):
        raise LumaError("storageClasses must be a mapping")
    result: Dict[str, StorageClassSpec] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            raise LumaError(f"storageClasses.{name} must be a mapping")
        provider = str(value.get("provider") or "")
        if provider not in VALID_STORAGE_PROVIDERS:
            raise LumaError(f"storageClasses.{name}.provider must be one of {sorted(VALID_STORAGE_PROVIDERS)}")
        mode = str(value.get("mode") or "managed")
        if mode not in VALID_STORAGE_MODES:
            raise LumaError(f"storageClasses.{name}.mode must be one of {sorted(VALID_STORAGE_MODES)}")
        if mode == "external" and not value.get("endpoint"):
            raise LumaError(f"storageClasses.{name}.endpoint is required for external nfs")
        if mode == "external" and (value.get("node") or value.get("path")):
            raise LumaError(f"storageClasses.{name} external nfs cannot set node or path")
        if mode == "managed" and (not value.get("node") or not value.get("path")):
            raise LumaError(f"storageClasses.{name} managed nfs requires node and path")
        if mode == "managed" and value.get("endpoint"):
            raise LumaError(f"storageClasses.{name} managed nfs endpoint is resolved automatically")
        nodes = _string_list(value.get("nodes") or [])
        regions = _string_list(value.get("regions") or [])
        if mode == "external" and not regions:
            raise LumaError(f"storageClasses.{name}.regions requires at least one region for external nfs")
        for region in regions:
            if region not in VALID_REGIONS:
                raise LumaError(f"storageClasses.{name}.regions contains invalid region: {region}")
        result[str(name)] = StorageClassSpec(
            name=str(name),
            provider=provider,
            mode=mode,
            node=str(value["node"]).strip() if value.get("node") else None,
            path=str(value["path"]).strip() if value.get("path") else None,
            endpoint=str(value["endpoint"]).strip() if value.get("endpoint") else None,
            mount_options=str(value.get("mountOptions") or DEFAULT_NFS_MOUNT_OPTIONS),
            nodes=nodes,
            regions=regions,
            raw=dict(value),
        )
    return result


def _load_compose_volumes(raw: Any, storage_classes: Dict[str, StorageClassSpec]) -> Dict[str, ComposeVolumeSpec]:
    if not isinstance(raw, dict):
        raise LumaError("volumes must be a mapping")
    result: Dict[str, ComposeVolumeSpec] = {}
    for name, value in raw.items():
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise LumaError(f"volumes.{name} must be a mapping")
        storage_class = value.get("storageClass")
        local = value.get("local") or {}
        if local and not isinstance(local, dict):
            raise LumaError(f"volumes.{name}.local must be a mapping")
        if storage_class and local:
            raise LumaError(f"volumes.{name} cannot use both storageClass and local")
        if storage_class and str(storage_class) not in storage_classes:
            raise LumaError(f"volumes.{name}.storageClass references unknown storage class: {storage_class}")
        access_mode = str(value.get("accessMode") or "ReadWriteOnce")
        if access_mode not in VALID_ACCESS_MODES:
            raise LumaError(f"volumes.{name}.accessMode must be one of {sorted(VALID_ACCESS_MODES)}")
        initialize = value.get("initialize")
        if initialize is not None and str(initialize) != "empty":
            raise LumaError(f"volumes.{name}.initialize only supports: empty")
        adopted = bool(value.get("adopted", False))
        result[str(name)] = ComposeVolumeSpec(
            name=str(name),
            storage_class=str(storage_class) if storage_class else None,
            path=str(value["path"]).strip().strip("/") if value.get("path") else None,
            access_mode=access_mode,
            local_node=str(local["node"]).strip() if local.get("node") else None,
            local_path=str(local["path"]).strip() if local.get("path") else None,
            initialize=str(initialize) if initialize is not None else None,
            adopted=adopted,
            raw=dict(value),
        )
        if result[str(name)].kind == "local" and (not result[str(name)].local_node or not result[str(name)].local_path):
            raise LumaError(f"volumes.{name}.local requires node and path")
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
        if not isinstance(relay, dict):
            raise LumaError(f"services.{name}.relay must be a mapping")
        if not isinstance(tunnel, dict):
            raise LumaError(f"services.{name}.tunnel must be a mapping")
        if not isinstance(tcp, dict):
            raise LumaError(f"services.{name}.tcp must be a mapping")
        result[str(name)] = ComposeServiceSpec(
            name=str(name),
            region=str(value["region"]) if value.get("region") else None,
            node=str(value["node"]).strip() if value.get("node") else None,
            exposure=str(value.get("exposure") or "none"),
            domain=str(value["domain"]).strip() if value.get("domain") else None,
            port=_positive_int(value["port"], f"services.{name}.port") if value.get("port") is not None else None,
            publish_port=_positive_int(value["publishPort"], f"services.{name}.publishPort") if value.get("publishPort") is not None else None,
            replicas=_positive_int(value["replicas"], f"services.{name}.replicas") if value.get("replicas") is not None else None,
            proxy=bool(value.get("proxy", False)),
            relay=dict(relay),
            tunnel=dict(tunnel),
            tcp=dict(tcp),
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


def _render_storage_class_volume(storage_class: StorageClassSpec, volume: ComposeVolumeSpec, endpoint: str) -> Dict[str, Any]:
    if storage_class.provider != "nfs":
        return {"driver": "local"}
    host, export_path = _parse_nfs_endpoint(storage_class, endpoint)
    sub_path = volume.path or volume.name
    device = f":{export_path.rstrip('/')}/{sub_path.strip('/')}"
    options = storage_class.mount_options
    if "addr=" not in options:
        options = f"addr={host},{options}" if options else f"addr={host}"
    return {
        "driver": "local",
        "driver_opts": {
            "type": "nfs",
            "o": options,
            "device": device,
        },
    }


def render_storage_class_volume(
    storage_class: StorageClassSpec,
    volume: ComposeVolumeSpec,
    endpoint: str,
) -> Dict[str, Any]:
    """Render Docker volume-driver options for a resolved storage class.

    Kept as a public wrapper so trusted control-plane renderers can translate
    the same storage policy into Nomad Docker ``mount.volume_options`` without
    duplicating NFS endpoint parsing or mount option handling.
    """

    return _render_storage_class_volume(storage_class, volume, endpoint)


def _parse_nfs_endpoint(storage_class: StorageClassSpec, endpoint: str) -> tuple[str, str]:
    if endpoint.startswith("nfs://"):
        endpoint = endpoint[len("nfs://") :]
    host, sep, export_path = endpoint.partition(":")
    if not sep or not host or not export_path:
        raise LumaError(f"storage class {storage_class.name} endpoint must look like host:/export/path")
    return host, export_path


def resolve_storage_mounts(
    deployment: ComposeDeploymentSpec,
    *,
    node_records: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    usage = _compose_service_volume_usage(deployment.compose)
    mounts: List[Dict[str, Any]] = []
    for service_name, used_volumes in usage.items():
        override = deployment.services.get(service_name)
        region = override.region if override and override.region else deployment.region
        for volume_name in used_volumes:
            volume = deployment.volumes.get(volume_name)
            if not volume or not volume.storage_class:
                continue
            storage_class = deployment.storage_classes[volume.storage_class]
            _validate_storage_class_service_use(storage_class, service_name=service_name, region=region, explicit_node=override.node if override else None)
            endpoint, network_path = _storage_endpoint_for_region(storage_class, region, node_records)
            mounts.append(
                {
                    "service": service_name,
                    "volume": volume_name,
                    "storageClass": storage_class.name,
                    "provider": storage_class.provider,
                    "mode": storage_class.mode,
                    "region": region,
                    "endpoint": endpoint,
                    "networkPath": network_path,
                    "path": volume.path or volume.name,
                }
            )
    return mounts


def _validate_storage_class_service_use(
    storage_class: StorageClassSpec,
    *,
    service_name: str,
    region: str,
    explicit_node: str | None,
) -> None:
    if storage_class.regions and region not in storage_class.regions:
        raise LumaError(
            f"compose service {service_name} region {region} is not allowed by storageClass {storage_class.name}"
        )
    if storage_class.nodes:
        if explicit_node and explicit_node not in storage_class.nodes:
            raise LumaError(
                f"compose service {service_name} node {explicit_node} is not allowed by storageClass {storage_class.name}"
            )
        if len(storage_class.nodes) > 1 and not explicit_node:
            raise LumaError(
                f"compose service {service_name} must set node because storageClass {storage_class.name} allows multiple nodes"
            )


def _storage_endpoint_for_region(
    storage_class: StorageClassSpec,
    region: str,
    node_records: Dict[str, Any] | None,
) -> tuple[str, str]:
    if storage_class.mode == "external":
        if not storage_class.endpoint:
            raise LumaError(f"external storageClass {storage_class.name} requires endpoint")
        return storage_class.endpoint, "external"
    if not storage_class.node or not storage_class.path:
        raise LumaError(f"managed storageClass {storage_class.name} requires node and path")
    record = _node_record_for_name(node_records or {}, storage_class.node)
    if not record:
        raise LumaError(f"managed storageClass {storage_class.name} references unknown Luma node: {storage_class.node}")
    node_region = str(record.get("region") or "")
    if not node_region:
        raise LumaError(f"managed storageClass {storage_class.name} node {storage_class.node} has no region; rerun luma node join")
    if node_region == region:
        endpoint_host = _same_region_storage_host(storage_class, record)
        return f"{endpoint_host}:{storage_class.path}", "same-region"
    tailscale_endpoint = str(record.get("tailscaleIP") or record.get("tailscaleName") or "").strip()
    if not tailscale_endpoint:
        raise LumaError(
            f"managed storageClass {storage_class.name} crosses {region}->{node_region} but node {storage_class.node} has no tailscaleIP; rerun luma node join"
        )
    return f"{tailscale_endpoint}:{storage_class.path}", "tailscale"


def _same_region_storage_host(storage_class: StorageClassSpec, record: Dict[str, Any]) -> str:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    for value in (
        record.get("hostname"),
        labels.get("luma.node.hostname"),
        storage_class.node,
    ):
        host = str(value or "").strip()
        if host:
            return host
    raise LumaError(f"managed storageClass {storage_class.name} node {storage_class.node} has no usable storage host")


def _node_record_for_name(nodes: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    direct = nodes.get(name)
    if isinstance(direct, dict):
        return direct
    for value in nodes.values():
        if isinstance(value, dict) and value.get("displayName") == name:
            return value
    return None


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


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LumaError("expected a list of strings")
    return [item for item in value if item]
