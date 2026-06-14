from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict

from .errors import LumaError


DEFAULT_DOCKER_REGISTRY = "registry-1.docker.io"


def registry_host_from_image(image: str) -> str:
    image_ref = str(image or "").split("@", 1)[0]
    parts = image_ref.split("/", 1)
    if len(parts) > 1:
        first = parts[0]
    else:
        return DEFAULT_DOCKER_REGISTRY
    if "." in first or ":" in first or first == "localhost":
        return normalize_registry_host(first)
    return DEFAULT_DOCKER_REGISTRY


def normalize_registry_host(value: str) -> str:
    host = str(value or "").strip()
    if not host:
        raise LumaError("registry host is required")
    if "://" in host:
        raise LumaError("registry host must not include a URL scheme")
    host = host.rstrip("/")
    if "/" in host:
        raise LumaError("registry host must not include a path")
    if not re.match(r"^[A-Za-z0-9._:-]+$", host):
        raise LumaError("registry host contains invalid characters")
    return host.lower()


def public_registry_url(host: str) -> str:
    host = normalize_registry_host(host)
    if host == "docker.io":
        return DEFAULT_DOCKER_REGISTRY
    return host


def registry_auth_for_image(registries: Dict[str, Any], image: str) -> Dict[str, str] | None:
    host = registry_host_from_image(image)
    candidates = [host]
    if host == DEFAULT_DOCKER_REGISTRY:
        candidates.append("docker.io")
    elif host == "docker.io":
        candidates.append(DEFAULT_DOCKER_REGISTRY)
    for candidate in candidates:
        item = registries.get(candidate)
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "")
        password = str(item.get("password") or "")
        server_address = str(item.get("serverAddress") or candidate)
        if username and password:
            return {
                "username": username,
                "password": password,
                "serveraddress": public_registry_url(server_address),
            }
    return None


def docker_registry_auth_header(auth: Dict[str, str] | None) -> str | None:
    if not auth:
        return None
    payload = {
        "username": auth.get("username", ""),
        "password": auth.get("password", ""),
        "serveraddress": public_registry_url(auth.get("serveraddress") or ""),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def registry_auth_matches_image(auth: Dict[str, str] | None, image: str) -> bool:
    if not auth:
        return False
    auth_host = normalize_registry_host(public_registry_url(auth.get("serveraddress") or ""))
    image_host = normalize_registry_host(public_registry_url(registry_host_from_image(image)))
    aliases = {auth_host}
    if auth_host == DEFAULT_DOCKER_REGISTRY:
        aliases.add("docker.io")
    if auth_host == "docker.io":
        aliases.add(DEFAULT_DOCKER_REGISTRY)
    return image_host in aliases


def image_uses_mutable_latest_tag(image: str) -> bool:
    image_ref = str(image or "").strip()
    if not image_ref or "@" in image_ref:
        return False
    name = image_ref.rsplit("/", 1)[-1]
    if ":" not in name:
        return True
    return name.rsplit(":", 1)[-1] == "latest"


def registry_provider_type(host: str) -> int:
    host = normalize_registry_host(host)
    if host in {"docker.io", DEFAULT_DOCKER_REGISTRY}:
        return 6
    if host == "quay.io":
        return 1
    if host.endswith(".azurecr.io"):
        return 2
    if "gitlab" in host:
        return 4
    return 3
