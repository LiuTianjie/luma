from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import LumaConfig
from .io import dump_yaml
from .service import ServiceSpec, tcp_entrypoint_name, tcp_relay_publish_port


def stack_path(config: LumaConfig, service: ServiceSpec) -> Path:
    if service.stack_path:
        return service.stack_path
    return config.stack_root / service.region / service.slug / f"{service.slug}.nomad.json"


def route_path(config: LumaConfig, service: ServiceSpec) -> Path:
    if service.route_path:
        return service.route_path
    return config.routes_root / f"{service.slug}.yml"


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


def render_tcp_route(config: LumaConfig, service: ServiceSpec) -> str:
    service_name = service.slug
    publish_port = tcp_relay_publish_port(service)
    tcp_entrypoint = tcp_entrypoint_name(publish_port)
    addresses = service.tcp.get("addresses")
    if isinstance(addresses, list) and addresses:
        servers = [{"address": str(address)} for address in addresses]
    else:
        address = service.tcp.get("address")
        if not address:
            host = service.tcp.get("host") or service.node or f"auto-{service.region}-node"
            port = service.tcp.get("port", service.publish_port or service.port)
            address = f"{host}:{port}"
        servers = [{"address": str(address)}]

    route: Dict[str, Any] = {
        "tcp": {
            "routers": {
                service_name: {
                    "rule": "HostSNI(`*`)",
                    "entryPoints": [tcp_entrypoint],
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
