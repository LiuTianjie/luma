from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import LumaConfig
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


def render_tailscale_route(config: LumaConfig, service: ServiceSpec) -> str:
    service_name = service.slug
    upstream_url = service.relay.get("url")
    if not upstream_url:
        scheme = service.relay.get("scheme", "http")
        host = service.relay["host"]
        port = service.relay.get("port", service.publish_port or service.port)
        upstream_url = f"{scheme}://{host}:{port}"

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
                        "servers": [{"url": upstream_url}],
                    }
                }
            },
        }
    }
    return dump_yaml(route)


def render_stack(config: LumaConfig, service: ServiceSpec) -> str:
    service_name = service.slug
    constraints = [f"node.labels.region == {service.region}"]
    constraints.extend(service.constraints)

    deploy: Dict[str, Any] = {
        "replicas": service.replicas,
        "placement": {"constraints": constraints},
    }

    labels: List[str] = list(service.labels)
    if uses_traefik_labels(service):
        labels.extend(
            [
                "traefik.enable=true",
                f"traefik.http.routers.{service_name}.rule=Host(`{service.domain}`)",
                f"traefik.http.routers.{service_name}.entrypoints={config.entrypoint}",
                f"traefik.http.routers.{service_name}.tls.certresolver={config.cert_resolver}",
                f"traefik.http.services.{service_name}.loadbalancer.server.port={service.port}",
            ]
        )
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
    return dump_yaml(stack)
