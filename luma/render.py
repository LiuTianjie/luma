from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .compose import (
    ComposeVolumeSpec,
    _database_volume_name,
    _load_storage_classes,
    _normalize_storage_workload,
    _render_storage_class_volume,
    _storage_class_supports_workload,
    _storage_class_verified_for_workload,
    _storage_endpoint_for_region,
    _validate_storage_class_service_use,
)
from .config import LumaConfig
from .errors import LumaError
from .io import dump_yaml
from .service import ServiceSpec


def stack_path(config: LumaConfig, service: ServiceSpec) -> Path:
    if service.stack_path:
        return service.stack_path
    return config.stack_root / service.region / service.slug / "stack.yml"


def route_path(config: LumaConfig, service: ServiceSpec) -> Path:
    if service.route_path:
        return service.route_path
    return config.routes_root / f"{service.slug}.yml"


def uses_traefik_labels(service: ServiceSpec) -> bool:
    return service.exposure in {"cn-edge", "external-edge"}


def named_volume_sources(volumes: List[str]) -> List[str]:
    names: List[str] = []
    for spec in volumes:
        source = spec.split(":", 1)[0].strip()
        if not source or source.startswith("/") or source.startswith("."):
            continue
        names.append(source)
    return names


def render_tailscale_route(config: LumaConfig, service: ServiceSpec) -> str:
    service_name = service.slug
    upstream_urls = service.relay.get("urls")
    if isinstance(upstream_urls, list) and upstream_urls:
        servers = [{"url": str(url)} for url in upstream_urls]
    else:
        upstream_url = service.relay.get("url")
        if not upstream_url:
            scheme = service.relay.get("scheme", "http")
            host = service.relay.get("host") or service.node or f"auto-{service.region}-node"
            port = service.relay.get("port", service.publish_port or service.port)
            upstream_url = f"{scheme}://{host}:{port}"
        servers = [{"url": upstream_url}]

    route: Dict[str, Any] = {
        "http": {
            "routers": {
                service_name: {
                    "rule": f"Host(`{service.domain}`)",
                    "entryPoints": [config.entrypoint],
                    "tls": {"certResolver": config.cert_resolver},
                    "service": service_name,
                }
            },
            "services": {
                service_name: {
                    "loadBalancer": {
                        "servers": servers,
                    }
                }
            },
        }
    }
    return dump_yaml(route)

def render_stack(
    config: LumaConfig,
    service: ServiceSpec,
    *,
    storage_classes: Dict[str, Any] | None = None,
    node_records: Dict[str, Any] | None = None,
) -> str:
    service_name = service.slug
    storage_context = _service_storage_context(service, storage_classes, node_records)
    constraints = [f"node.labels.region == {service.region}"]
    if service.node_id:
        constraints.append(f"node.labels.luma.node.id == {service.node_id}")
    elif service.node:
        constraints.append(f"node.labels.luma.node.name == {service.node}")
    elif storage_context["node_pin"]:
        constraints.append(f"node.labels.luma.node.name == {storage_context['node_pin']}")
    constraints.extend(service.constraints)

    deploy: Dict[str, Any] = {
        "replicas": service.replicas,
        "placement": {"constraints": constraints},
    }
    if service.resources:
        deploy["resources"] = service.resources

    labels: List[str] = [*service.labels, *storage_context["labels"]]
    if uses_traefik_labels(service):
        labels.extend(
            [
                "traefik.enable=true",
                f"traefik.http.routers.{service_name}.rule=Host(`{service.domain}`)",
                f"traefik.http.routers.{service_name}.entrypoints={config.entrypoint}",
                f"traefik.http.routers.{service_name}.tls.certresolver={config.cert_resolver}",
                f"traefik.http.services.{service_name}.loadbalancer.server.port={service.port}",
                f"traefik.swarm.network={config.public_network}",
            ]
        )
        deploy["labels"] = labels
    elif storage_context["labels"]:
        deploy["labels"] = labels

    networks = list(service.networks)
    if uses_traefik_labels(service) and config.public_network not in networks:
        networks.insert(0, config.public_network)
    if service.proxy and config.egress_network not in networks:
        networks.append(config.egress_network)

    service_body: Dict[str, Any] = {
        "image": service.image,
        "deploy": deploy,
    }
    if service.command is not None:
        service_body["command"] = service.command
    environment = dict(service.environment)
    if service.proxy:
        environment.setdefault("HTTP_PROXY", "http://egress_mihomo:7890")
        environment.setdefault("HTTPS_PROXY", "http://egress_mihomo:7890")
    if environment:
        service_body["environment"] = environment
    if service.exposure == "tailscale-relay":
        service_body["ports"] = [
            {
                "target": service.port,
                "published": service.publish_port or service.port,
                "protocol": "tcp",
                "mode": "host",
            }
        ]
    if service.volumes:
        service_body["volumes"] = service.volumes
    if service.healthcheck:
        service_body["healthcheck"] = service.healthcheck
    if networks:
        service_body["networks"] = networks

    stack: Dict[str, Any] = {"services": {service_name: service_body}}
    if service.exposure == "cloudflare-tunnel":
        token_env = service.tunnel.get("tokenEnv", "CLOUDFLARE_TUNNEL_TOKEN")
        stack["services"]["cloudflared"] = {
            "image": "cloudflare/cloudflared:latest",
            "command": "tunnel --no-autoupdate run",
            "environment": {
                "TUNNEL_TOKEN": f"${{{token_env}}}",
            },
            "deploy": {
                "replicas": 1,
                "placement": {"constraints": list(constraints)},
            },
        }
    if networks:
        stack["networks"] = {
            name: {"external": True}
            for name in networks
        }
    volume_names = named_volume_sources(service.volumes)
    if volume_names:
        stack["volumes"] = {
            name: storage_context["volumes"].get(name, {})
            for name in volume_names
        }
    return dump_yaml(stack)


def _service_storage_context(
    service: ServiceSpec,
    storage_classes_raw: Dict[str, Any] | None,
    node_records: Dict[str, Any] | None,
) -> Dict[str, Any]:
    if not service.storage:
        return {"volumes": {}, "labels": [], "node_pin": ""}
    if storage_classes_raw is None:
        raise LumaError("service storage requires manager storageClasses; run with a control context or deploy through Luma Control")
    storage_classes = _load_storage_classes(storage_classes_raw)
    warnings = service_database_storage_warnings(service, storage_classes_raw)
    if warnings:
        raise LumaError("; ".join(warnings))
    labels: List[str] = []
    volumes: Dict[str, Any] = {}
    node_pins: set[str] = set()
    for name, spec in service.storage.items():
        storage_class = storage_classes.get(spec.storage_class)
        if not storage_class:
            raise LumaError(f"storage.{name}.storageClass references unknown storage class: {spec.storage_class}")
        _validate_storage_class_service_use(
            storage_class,
            service_name=service.name,
            region=service.region,
            explicit_node=service.node,
        )
        if storage_class.nodes:
            if len(storage_class.nodes) == 1 and not service.node:
                node_pins.add(storage_class.nodes[0])
            elif len(storage_class.nodes) > 1 and not service.node:
                raise LumaError(f"service {service.name} must set node because storageClass {storage_class.name} allows multiple nodes")
        endpoint, network_path = _storage_endpoint_for_region(storage_class, service.region, node_records)
        volume = ComposeVolumeSpec(
            name=spec.name,
            storage_class=spec.storage_class,
            path=spec.path,
            access_mode=spec.access_mode,
            initialize=spec.initialize,
            adopted=spec.adopted,
            raw=spec.raw,
        )
        volumes[name] = _render_storage_class_volume(storage_class, volume, endpoint)
        labels.extend(
            [
                f"luma.storage.{name}=storageClass",
                f"luma.storageClass.{name}={storage_class.name}",
                f"luma.storageEndpoint.{name}={endpoint}",
                f"luma.storagePath.{name}={network_path}",
            ]
        )
    if len(node_pins) > 1:
        raise LumaError(f"service {service.name} has conflicting storageClass node pins: {sorted(node_pins)}")
    return {"volumes": volumes, "labels": labels, "node_pin": next(iter(node_pins), "")}


def service_database_storage_warnings(service: ServiceSpec, storage_classes_raw: Dict[str, Any] | None) -> List[str]:
    if not service.storage or storage_classes_raw is None:
        return []
    storage_classes = _load_storage_classes(storage_classes_raw)
    warnings: List[str] = []
    for volume in service.volumes:
        source, target = _service_volume_source_target(volume)
        if not source or source not in service.storage:
            continue
        database_name = _database_volume_name(service.name, service.image, target)
        if not database_name:
            continue
        spec = service.storage[source]
        storage_class = storage_classes.get(spec.storage_class)
        if not storage_class:
            continue
        if not _storage_class_supports_workload(storage_class, database_name):
            required_workload = _normalize_storage_workload(database_name)
            warnings.append(
                f"volume {source} mounts {database_name} data directory {target} on storageClass {storage_class.name}, "
                f"but the storageClass workloads are {storage_class.workloads or ['filesystem']}; "
                f"provision a database-capable storage service and register it with workload {required_workload}"
            )
            continue
        if not _storage_class_verified_for_workload(storage_class, database_name):
            required_workload = _normalize_storage_workload(database_name)
            warnings.append(
                f"volume {source} mounts {database_name} data directory {target} on storageClass {storage_class.name}, "
                f"but workload {required_workload} has not been verified; run luma storage probe {storage_class.name} --workload {required_workload}"
            )
    return warnings


def guard_service_database_storage(service: ServiceSpec, storage_classes: Dict[str, Any] | None) -> str:
    warnings = service_database_storage_warnings(service, storage_classes)
    if warnings:
        raise LumaError("; ".join(warnings))
    return "Database storage check passed"


def _service_volume_source_target(volume: str) -> tuple[str, str]:
    parts = volume.split(":")
    if len(parts) < 2:
        return "", ""
    source = parts[0].strip()
    if not source or source.startswith("/") or source.startswith(".") or source == "~":
        return "", ""
    return source, parts[1].strip().rstrip("/")
