"""Render Docker-owned allocation networks for macOS/OrbStack Compose jobs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .compose import ComposeDeploymentSpec
from .errors import LumaError

NETWORK_TASKS = ("luma-network-init", "luma-network-watch", "luma-network-cleanup")
NETWORK_CONTAINER = "luma-compose-net-${NOMAD_ALLOC_ID}"
DEFAULT_NETWORK_IMAGE = "python:3.13-alpine"


def compose_network_image(config: Any) -> str:
    return str((config.defaults.get("images") or {}).get("composeNetwork") or DEFAULT_NETWORK_IMAGE)


def _node_is_mac(record: Mapping[str, Any]) -> bool:
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    attrs = record.get("nomadAttributes") or record.get("attributes") or {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    values = (
        record.get("nodePlatform"), record.get("platform"), record.get("os"),
        record.get("operatingSystem"), agent.get("os"), attrs.get("os.name"), attrs.get("kernel.name"),
    )
    return any(
        str((value.get("os") or value.get("OS")) if isinstance(value, Mapping) else value or "").lower().split("/")[0]
        in {"darwin", "macos", "mac os", "mac os x"}
        for value in values
    )


def uses_macos_compose_network(deployment: ComposeDeploymentSpec, node_records: Mapping[str, Any] | None) -> bool:
    services = deployment.compose.get("services") or {}
    if services and all(str(body.get("network_mode") or "") == "host" for body in services.values()):
        return False
    # A single internal task already has its own ordinary Docker namespace.
    # Keep region-scheduled worker pools lightweight and unpinned.
    if len(services) == 1 and all(service.exposure == "none" for service in deployment.services.values()):
        return False
    pins = {service.node for service in deployment.services.values() if service.node}
    pins.update(volume.local_node for volume in deployment.volumes.values() if volume.kind == "local" and volume.local_node)
    if len(pins) > 1:
        raise LumaError("a Compose network runs on one node; service and volume pins must agree")
    regions = {service.region for service in deployment.services.values() if service.region}
    region = next(iter(regions), deployment.region)
    records = node_records or {}
    for name, record in records.items():
        if not isinstance(record, Mapping) or not _node_is_mac(record):
            continue
        if pins.intersection({name, record.get("name"), record.get("displayName")}):
            return True
        node_region = record.get("region") or (record.get("labels") or {}).get("region")
        if not pins and node_region == region:
            raise LumaError("Compose in a region with Mac nodes requires an explicit node pin")
    return False


def macos_compose_ports(deployment: ComposeDeploymentSpec) -> list[dict[str, int]]:
    ports = []
    used = set()
    for service in deployment.services.values():
        if service.exposure == "none":
            continue
        if service.exposure not in {"tailscale-relay", "tcp-relay"}:
            raise LumaError(f"Mac Compose service {service.name} requires exposure none, tailscale-relay or tcp-relay")
        port = service.port
        published = service.publish_port or port
        if any(type(value) is not int or not 1 <= value <= 65535 for value in (port, published)):
            raise LumaError(f"Mac Compose service {service.name} requires ports from 1 to 65535")
        if published in used:
            raise LumaError(f"Mac Compose published port {published} is used by multiple services")
        used.add(published)
        ports.append({"port": port, "published": published})
    return ports


def macos_compose_network_tasks(deployment: ComposeDeploymentSpec, config: Any, registry_auth_resolver: Any = None) -> list[dict[str, Any]]:
    services = deployment.compose.get("services") or {}
    if any(name.startswith("luma-network-") for name in services):
        raise LumaError("Compose service names beginning with luma-network- are reserved")
    ports = macos_compose_ports(deployment)
    image = deployment.network_image or compose_network_image(config)
    source = Path(__file__).with_name("compose_network_runtime.py").read_text(encoding="utf-8")
    tasks = []
    for name, action, hook, sidecar in (
        (NETWORK_TASKS[0], "prepare", "prestart", False),
        (NETWORK_TASKS[1], "watch", "poststart", True),
        (NETWORK_TASKS[2], "cleanup", "poststop", False),
    ):
        args = ["/local/luma-compose-network.py", action, "--allocation", "${NOMAD_ALLOC_ID}", "--job", deployment.slug]
        if action == "prepare":
            args += ["--image", image, "--services", json.dumps(list(services)), "--ports", json.dumps(ports)]
        docker_config: dict[str, Any] = {
            "image": image, "command": "python", "args": args, "network_mode": "none",
            "mount": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock", "readonly": False}],
        }
        auth = registry_auth_resolver(image) if callable(registry_auth_resolver) else None
        if auth and auth.get("username") and auth.get("password"):
            docker_config["auth"] = {
                "username": str(auth["username"]), "password": str(auth["password"]),
                "server_address": str(auth.get("serveraddress") or auth.get("serverAddress") or ""),
            }
        task = {
            "Name": name, "Driver": "docker", "Config": docker_config,
            "Meta": {"luma.compose-network": "true"},
            "Lifecycle": {"Hook": hook, "Sidecar": sidecar},
            # The watch task also reserves the namespace holder's small budget.
            "Resources": {"CPU": 20, "MemoryMB": 64},
            "Templates": [{
                "DestPath": "local/luma-compose-network.py", "EmbeddedTmpl": source,
                "ChangeMode": "noop", "LeftDelim": "[[[", "RightDelim": "]]]",
            }],
        }
        if action == "watch":
            task["RestartPolicy"] = {"Attempts": 0, "Interval": 60_000_000_000, "Delay": 5_000_000_000, "Mode": "fail"}
        tasks.append(task)
    return tasks
