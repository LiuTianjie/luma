"""Publish optional luma-observe Grafana on the Control domain.

Traefik already shares the manager host network with Grafana. Control writes a
file-provider route so Dashboard can embed `/grafana` without a second domain
or a Tailscale URL. Other Luma clusters get the same path on their Control
domain after upgrading Control and deploying observe. A Traefik forwardAuth
to Control limits it to signed-in dashboard sessions and the management token.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

import yaml

GRAFANA_PATH = "/grafana"
GRAFANA_ROUTE_NAME = "luma-observe-grafana.yml"
GRAFANA_PUBLIC_ROUTE_NAME = "luma-observe-grafana-public.yml"
GRAFANA_UPSTREAM = "http://127.0.0.1:3100"
# Control and Traefik both use the manager host network; Control listens on :8080.
GRAFANA_AUTH_ADDRESS = "http://127.0.0.1:8080/v1/auth/forward"
GRAFANA_PUBLIC_AUTH_ADDRESS = "http://127.0.0.1:8080/v1/auth/grafana-public/forward"


def grafana_route_yaml(
    domain: str,
    *,
    entrypoint: str = "websecure",
    cert_resolver: str = "letsencrypt",
    upstream: str = GRAFANA_UPSTREAM,
    auth_address: str = GRAFANA_AUTH_ADDRESS,
) -> str:
    """Traefik route for `/grafana`, gated by a dashboard session or the token.

    Grafana keeps anonymous Viewer access so the embedded iframe needs no
    second login; Control's forwardAuth decides who reaches it at all. The
    Authorization header is dropped after the check so a management token
    never reaches Grafana, which would also reject it as an unknown API key.
    """
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
        "      middlewares:\n"
        "      - luma-observe-grafana-auth\n"
        "      - luma-observe-grafana-strip-auth\n"
        "      service: luma-observe-grafana\n"
        "  middlewares:\n"
        "    luma-observe-grafana-auth:\n"
        "      forwardAuth:\n"
        f"        address: {auth_address}\n"
        # Drop client X-Forwarded-*; Traefik then sets the real method itself.
        "        trustForwardHeader: false\n"
        "    luma-observe-grafana-strip-auth:\n"
        "      headers:\n"
        "        customRequestHeaders:\n"
        "          Authorization: \"\"\n"
        "  services:\n"
        "    luma-observe-grafana:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        f"        - url: {upstream}\n"
    )


def write_grafana_route(routes_dir: Path, domain: str, **kwargs: str) -> Path:
    path = Path(routes_dir) / GRAFANA_ROUTE_NAME
    write_route_file(path, grafana_route_yaml(domain, **kwargs))
    return path


def write_route_file(path: Path, content: str) -> None:
    """Publish one complete file-provider document, never a partially written route."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.stem}-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def grafana_public_route_yaml(domain: str, *, entrypoint: str = "websecure",
                              cert_resolver: str = "letsencrypt", upstream: str = GRAFANA_UPSTREAM,
                              auth_address: str = GRAFANA_PUBLIC_AUTH_ADDRESS) -> str:
    """Password-protect Grafana's native public endpoints and static assets.

    Grafana validates the share token and loads saved panel queries itself. This
    route does not grant access to dashboard search, datasource proxies or edits.
    """
    host = domain.strip().rstrip(".")
    if not host or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("a valid Control domain is required")
    source = Path(__file__).parent / "assets" / "observe-grafana-public.yml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    http = document["http"]
    for router in http["routers"].values():
        router["rule"] = router["rule"].replace("luma.example.com", host)
        router["entryPoints"] = [entrypoint]
        router["tls"]["certResolver"] = cert_resolver
    http["services"]["luma-observe-grafana-public"]["loadBalancer"]["servers"] = [{"url": upstream}]
    http["middlewares"]["luma-observe-grafana-public-auth"]["forwardAuth"]["address"] = auth_address
    return yaml.safe_dump(document, sort_keys=False)


def write_grafana_public_route(routes_dir: Path, domain: str, *, enabled: bool, **kwargs: str) -> None:
    path = Path(routes_dir) / GRAFANA_PUBLIC_ROUTE_NAME
    if enabled:
        write_route_file(path, grafana_public_route_yaml(domain, **kwargs))
    else:
        path.unlink(missing_ok=True)
