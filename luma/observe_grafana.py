"""Publish optional luma-observe Grafana on the Control domain.

Traefik already shares the manager host network with Grafana. Control writes a
file-provider route so Dashboard can embed `/grafana` without a second domain
or a Tailscale URL. Other Luma clusters get the same path on their Control
domain after upgrading Control and deploying observe.
"""
from __future__ import annotations

from pathlib import Path

GRAFANA_PATH = "/grafana"
GRAFANA_ROUTE_NAME = "luma-observe-grafana.yml"
GRAFANA_UPSTREAM = "http://127.0.0.1:3100"


def grafana_route_yaml(
    domain: str,
    *,
    entrypoint: str = "websecure",
    cert_resolver: str = "letsencrypt",
    upstream: str = GRAFANA_UPSTREAM,
) -> str:
    host = domain.strip().rstrip(".")
    if not host:
        raise ValueError("domain is required")
    return (
        "http:\n"
        "  routers:\n"
        "    luma-observe-grafana:\n"
        f"      rule: Host(`{host}`) && PathPrefix(`/grafana`)\n"
        "      entryPoints:\n"
        f"      - {entrypoint}\n"
        "      priority: 1000\n"
        "      tls:\n"
        f"        certResolver: {cert_resolver}\n"
        "      service: luma-observe-grafana\n"
        "  services:\n"
        "    luma-observe-grafana:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        f"        - url: {upstream}\n"
    )


def write_grafana_route(routes_dir: Path, domain: str, **kwargs: str) -> Path:
    path = Path(routes_dir) / GRAFANA_ROUTE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(grafana_route_yaml(domain, **kwargs), encoding="utf-8")
    return path
