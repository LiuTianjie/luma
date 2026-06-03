from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import LumaConfig
from .errors import LumaError
from .io import dump_yaml, load_yaml
from .service import VALID_EXPOSURES, VALID_REGIONS, slugify


VALID_STORAGE_PROVIDERS = {"nfs"}
VALID_STORAGE_MODES = {"managed", "external"}
VALID_ACCESS_MODES = {"ReadWriteOnce", "ReadWriteMany"}


@dataclass(frozen=True)
class StorageClassSpec:
    name: str
    provider: str
    mode: str = "managed"
    node: Optional[str] = None
    path: Optional[str] = None
    endpoint: Optional[str] = None
    mount_options: str = "nfsvers=4,rw"
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
    warnings = validate_compose_deployment_data(compose, storage_classes_loaded, volumes, services, default_region=str(region))
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
    return config.stack_root / "compose" / deployment.slug / "stack.yml"


def compose_route_path(config: LumaConfig, deployment: ComposeDeploymentSpec, service_name: str) -> Path:
    return config.routes_root / f"{deployment.slug}-{slugify(service_name)}.yml"


def render_compose_stack(
    config: LumaConfig,
    deployment: ComposeDeploymentSpec,
    *,
    node_id_resolver: Callable[[str], str | None] | None = None,
    node_records: Dict[str, Any] | None = None,
) -> str:
    rendered = copy.deepcopy(deployment.compose)
    rendered.pop("version", None)
    services = rendered.get("services")
    if not isinstance(services, dict):
        raise LumaError("compose file requires services mapping")
    top_volumes: Dict[str, Any] = dict(rendered.get("volumes") or {})
    rendered["services"] = services

    service_volume_usage = _compose_service_volume_usage(deployment.compose)
    volume_nodes = _local_volume_nodes(deployment.volumes)
    resolved_mounts = resolve_storage_mounts(deployment, node_records=node_records)
    mount_lookup = {
        (str(item["service"]), str(item["volume"])): item
        for item in resolved_mounts
    }
    volume_endpoints: Dict[str, str] = {}
    for item in resolved_mounts:
        volume_name = str(item["volume"])
        endpoint = str(item["endpoint"])
        previous = volume_endpoints.get(volume_name)
        if previous and previous != endpoint:
            raise LumaError(
                f"compose volume {volume_name} is used from multiple regions with different storage endpoints; split it into region-specific volumes"
            )
        volume_endpoints[volume_name] = endpoint
    extra_services: Dict[str, Any] = {}

    for service_name, service_body in list(services.items()):
        if not isinstance(service_body, dict):
            raise LumaError(f"compose service {service_name} must be a mapping")
        override = deployment.services.get(str(service_name))
        region = (override.region if override and override.region else deployment.region)
        constraints = [f"node.labels.region == {region}"]
        explicit_node = override.node if override else None
        local_nodes = {
            node
            for volume_name in service_volume_usage.get(str(service_name), [])
            for node in [volume_nodes.get(volume_name)]
            if node
        }
        storage_classes = {
            volume.storage_class
            for volume_name in service_volume_usage.get(str(service_name), [])
            for volume in [deployment.volumes.get(volume_name)]
            if volume and volume.storage_class
        }
        for storage_class_name in sorted(storage_classes):
            storage_class = deployment.storage_classes[str(storage_class_name)]
            if storage_class.regions and region not in storage_class.regions:
                raise LumaError(
                    f"compose service {service_name} region {region} is not allowed by storageClass {storage_class.name}"
                )
            if storage_class.nodes:
                if explicit_node and explicit_node not in storage_class.nodes:
                    raise LumaError(
                        f"compose service {service_name} node {explicit_node} is not allowed by storageClass {storage_class.name}"
                    )
                if len(storage_class.nodes) == 1:
                    local_nodes.add(storage_class.nodes[0])
                elif not explicit_node:
                    raise LumaError(
                        f"compose service {service_name} must set node because storageClass {storage_class.name} allows multiple nodes"
                    )
        if explicit_node:
            local_nodes.add(explicit_node)
        if len(local_nodes) > 1:
            raise LumaError(f"compose service {service_name} has conflicting node pins: {sorted(local_nodes)}")
        if local_nodes:
            node_name = next(iter(local_nodes))
            node_id = node_id_resolver(node_name) if node_id_resolver else None
            if node_id:
                constraints.append(f"node.labels.luma.node.id == {node_id}")
            else:
                constraints.append(f"node.labels.luma.node.name == {node_name}")

        _merge_deploy(service_body, constraints=constraints, replicas=override.replicas if override else None)
        _apply_luma_labels(
            deployment,
            str(service_name),
            service_body,
            service_volume_usage.get(str(service_name), []),
            mount_lookup=mount_lookup,
        )
        extra_services.update(_apply_service_exposure(config, deployment, str(service_name), service_body, override))
        _apply_proxy(config, service_body, bool(override and override.proxy))
        service_body["volumes"] = [
            _render_service_volume(volume, deployment.volumes)
            for volume in service_body.get("volumes", [])
        ]
    services.update(extra_services)

    for volume_name, volume in deployment.volumes.items():
        if volume.storage_class:
            storage_class = deployment.storage_classes[volume.storage_class]
            endpoint = volume_endpoints.get(volume_name) or _storage_endpoint_for_region(
                storage_class,
                deployment.region,
                node_records,
            )[0]
            top_volumes[volume_name] = _render_storage_class_volume(storage_class, volume, endpoint)

    if top_volumes:
        rendered["volumes"] = top_volumes
    _ensure_external_networks(rendered, config)
    rendered = _drop_empty_top_level(rendered)
    return dump_yaml(rendered)


def render_compose_routes(config: LumaConfig, deployment: ComposeDeploymentSpec) -> Dict[str, str]:
    routes: Dict[str, str] = {}
    for service_name, override in deployment.services.items():
        if override.exposure != "tailscale-relay":
            continue
        if not override.domain or not override.port:
            continue
        service_id = f"{deployment.slug}-{slugify(service_name)}"
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
) -> List[str]:
    warnings: List[str] = []
    compose_services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    for service_name, service in compose_services.items():
        if not isinstance(service, dict):
            raise LumaError(f"compose service {service_name} must be a mapping")
        if not isinstance(service.get("image"), str) or not service.get("image"):
            raise LumaError(f"compose service {service_name} requires image; remote compose deploy does not build images")
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
            mount_options=str(value.get("mountOptions") or "nfsvers=4,rw"),
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
        result[str(name)] = ComposeServiceSpec(
            name=str(name),
            region=str(value["region"]) if value.get("region") else None,
            node=str(value["node"]).strip() if value.get("node") else None,
            exposure=str(value.get("exposure") or "none"),
            domain=str(value["domain"]).strip() if value.get("domain") else None,
            port=int(value["port"]) if value.get("port") is not None else None,
            publish_port=int(value["publishPort"]) if value.get("publishPort") is not None else None,
            replicas=int(value["replicas"]) if value.get("replicas") is not None else None,
            proxy=bool(value.get("proxy", False)),
            relay=dict(value.get("relay") or {}),
            tunnel=dict(value.get("tunnel") or {}),
            raw=dict(value),
        )
    return result


def _merge_deploy(service_body: Dict[str, Any], *, constraints: List[str], replicas: Optional[int]) -> None:
    deploy = service_body.setdefault("deploy", {})
    if not isinstance(deploy, dict):
        raise LumaError("compose service deploy must be a mapping")
    placement = deploy.setdefault("placement", {})
    if not isinstance(placement, dict):
        raise LumaError("compose service deploy.placement must be a mapping")
    current_constraints = placement.get("constraints") if isinstance(placement.get("constraints"), list) else []
    merged = list(current_constraints)
    for constraint in constraints:
        if constraint not in merged:
            merged.append(constraint)
    placement["constraints"] = merged
    if replicas is not None:
        if replicas < 1:
            raise LumaError("service replicas must be >= 1")
        deploy.setdefault("replicas", replicas)


def _apply_service_exposure(
    config: LumaConfig,
    deployment: ComposeDeploymentSpec,
    service_name: str,
    service_body: Dict[str, Any],
    override: ComposeServiceSpec | None,
) -> Dict[str, Any]:
    if not override:
        return {}
    service_id = f"{deployment.slug}-{slugify(service_name)}"
    deploy = service_body.setdefault("deploy", {})
    labels: List[str] = _labels_as_list(deploy)
    if override.exposure in {"cn-edge", "external-edge"}:
        labels.extend(
            [
                "traefik.enable=true",
                f"traefik.http.routers.{service_id}.rule=Host(`{override.domain}`)",
                f"traefik.http.routers.{service_id}.entrypoints={config.entrypoint}",
                f"traefik.http.routers.{service_id}.tls.certresolver={config.cert_resolver}",
                f"traefik.http.services.{service_id}.loadbalancer.server.port={override.port}",
                f"traefik.swarm.network={config.public_network}",
                f"luma.compose.stack={deployment.slug}",
                f"luma.compose.service={service_name}",
            ]
        )
        deploy["labels"] = _dedupe(labels)
        networks = service_body.get("networks") if isinstance(service_body.get("networks"), list) else []
        if config.public_network not in networks:
            service_body["networks"] = [config.public_network, *networks]
    elif override.exposure == "tailscale-relay":
        service_body["ports"] = [
            {
                "target": override.port,
                "published": override.publish_port or override.port,
                "protocol": "tcp",
                "mode": "host",
            }
        ]
    elif override.exposure == "cloudflare-tunnel":
        token_env = override.tunnel.get("tokenEnv", "CLOUDFLARE_TUNNEL_TOKEN")
        tunnel_name = f"{service_name}-cloudflared"
        return {
            tunnel_name: {
            "image": "cloudflare/cloudflared:latest",
            "command": "tunnel --no-autoupdate run",
            "environment": {"TUNNEL_TOKEN": f"${{{token_env}}}"},
            "deploy": copy.deepcopy(service_body.get("deploy") or {}),
            }
        }
    return {}


def _apply_luma_labels(
    deployment: ComposeDeploymentSpec,
    service_name: str,
    service_body: Dict[str, Any],
    used_volumes: List[str],
    *,
    mount_lookup: Dict[tuple[str, str], Dict[str, Any]] | None = None,
) -> None:
    deploy = service_body.setdefault("deploy", {})
    labels = _labels_as_list(deploy)
    labels.extend(
        [
            f"luma.compose.stack={deployment.slug}",
            f"luma.compose.service={service_name}",
        ]
    )
    for volume_name in used_volumes:
        spec = deployment.volumes.get(volume_name)
        kind = spec.kind if spec else "unmanaged"
        labels.append(f"luma.storage.{volume_name}={kind}")
        if spec and spec.storage_class:
            labels.append(f"luma.storageClass.{volume_name}={spec.storage_class}")
        mount = (mount_lookup or {}).get((service_name, volume_name))
        if mount:
            labels.append(f"luma.storageEndpoint.{volume_name}={mount.get('endpoint', '')}")
            labels.append(f"luma.storagePath.{volume_name}={mount.get('networkPath', '')}")
        if spec and spec.local_node:
            labels.append(f"luma.storageNode.{volume_name}={spec.local_node}")
    deploy["labels"] = _dedupe(labels)


def _apply_proxy(config: LumaConfig, service_body: Dict[str, Any], enabled: bool) -> None:
    if not enabled:
        return
    environment = service_body.get("environment") or {}
    if isinstance(environment, list):
        env_map: Dict[str, str] = {}
        for item in environment:
            key, _, value = str(item).partition("=")
            env_map[key] = value
        environment = env_map
    if not isinstance(environment, dict):
        raise LumaError("compose service environment must be a mapping or list")
    environment.setdefault("HTTP_PROXY", "http://egress_mihomo:7890")
    environment.setdefault("HTTPS_PROXY", "http://egress_mihomo:7890")
    service_body["environment"] = environment
    networks = service_body.get("networks") if isinstance(service_body.get("networks"), list) else []
    if config.egress_network not in networks:
        service_body["networks"] = [*networks, config.egress_network]


def _render_service_volume(volume: Any, volumes: Dict[str, ComposeVolumeSpec]) -> Any:
    source = _volume_source(volume)
    if not source or source not in volumes:
        return volume
    spec = volumes[source]
    if spec.kind == "local":
        target, suffix = _volume_target_and_suffix(volume)
        return f"{spec.local_path}:{target}{suffix}"
    return volume


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
        return f"{storage_class.node}:{storage_class.path}", "same-region"
    tailscale_endpoint = str(record.get("tailscaleIP") or record.get("tailscaleName") or "").strip()
    if not tailscale_endpoint:
        raise LumaError(
            f"managed storageClass {storage_class.name} crosses {region}->{node_region} but node {storage_class.node} has no tailscaleIP; rerun luma node join"
        )
    return f"{tailscale_endpoint}:{storage_class.path}", "tailscale"


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
    services = compose.get("services") if isinstance(compose.get("services"), dict) else {}
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        used: List[str] = []
        for volume in service.get("volumes") or []:
            source = _volume_source(volume)
            if source and _is_named_volume(source):
                used.append(source)
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


def _is_named_volume(source: str) -> bool:
    return bool(source and not source.startswith("/") and not source.startswith(".") and source != "~")


def _local_volume_nodes(volumes: Dict[str, ComposeVolumeSpec]) -> Dict[str, str]:
    return {name: spec.local_node or "" for name, spec in volumes.items() if spec.kind == "local" and spec.local_node}


def _labels_as_list(service_body: Dict[str, Any]) -> List[str]:
    labels = service_body.get("labels") or []
    if isinstance(labels, list):
        return [str(item) for item in labels]
    if isinstance(labels, dict):
        return [f"{key}={value}" for key, value in labels.items()]
    raise LumaError("compose service labels must be a list or mapping")


def _ensure_external_networks(rendered: Dict[str, Any], config: LumaConfig) -> None:
    used_networks: set[str] = set()
    services = rendered.get("services") if isinstance(rendered.get("services"), dict) else {}
    for service in services.values():
        if not isinstance(service, dict):
            continue
        networks = service.get("networks")
        if isinstance(networks, list):
            used_networks.update(str(item) for item in networks)
        elif isinstance(networks, dict):
            used_networks.update(str(item) for item in networks.keys())
    external = {config.public_network, config.egress_network}.intersection(used_networks)
    if not external:
        return
    networks_raw = rendered.get("networks") if isinstance(rendered.get("networks"), dict) else {}
    networks = dict(networks_raw)
    for name in external:
        networks.setdefault(name, {"external": True})
    rendered["networks"] = networks


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _drop_empty_top_level(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if value not in ({}, [], None)}


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LumaError("expected a list of strings")
    return [item for item in value if item]
