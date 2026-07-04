from __future__ import annotations

"""Image reference parsing, registry auth lookup, and Docker egress-proxy
inspection for Luma Control.

A lower layer in the control package: depends only on stdlib, the registry
helpers, and the state-shaped dicts passed in — never back on server.py. The
image-pull ORCHESTRATION (ensure_image_pull_*) stays in server.py because it
drives node-agent dispatch; it imports these leaf helpers downward.
"""

import os
import re
from typing import Any, Dict

from ..errors import LumaError
from ..service import ServiceSpec
from ..registry import normalize_registry_host, public_registry_url, registry_auth_for_image


EGRESS_PROXY_URL = os.environ.get("LUMA_EGRESS_PROXY_URL", "http://127.0.0.1:7890")


EGRESS_NO_PROXY = os.environ.get(
    "LUMA_EGRESS_NO_PROXY",
    "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,docker.1panel.live,docker.m.daocloud.io,docker.1ms.run",
)


DEFAULT_EGRESS_PULL_REGISTRIES = {
    "docker.io",
    "index.docker.io",
    "registry-1.docker.io",
    "ghcr.io",
    "quay.io",
    "gcr.io",
    "k8s.gcr.io",
    "registry.k8s.io",
    "mcr.microsoft.com",
    "public.ecr.aws",
    "nvcr.io",
}


def _image_repo_from_repo_url(url: str) -> str:
    text = str(url or "").strip()
    text = re.sub(r"^[a-z]+://", "", text)
    text = re.sub(r"^[^@/]+@", "", text)  # strip user@ from scp-style urls
    text = text.split("?", 1)[0].split("#", 1)[0]  # drop query string / fragment
    text = text.replace(":", "/", 1) if "/" not in text.split(":", 1)[0] else text
    parts = [p for p in re.split(r"[/]", text) if p]
    if len(parts) >= 2:
        owner, name = parts[-2], parts[-1]
    elif parts:
        owner, name = "luma", parts[-1]
    else:
        raise LumaError(f"cannot derive image name from repo url: {url}")
    name = re.sub(r"\.git$", "", name)
    repo = f"{owner}/{name}".lower()
    repo = re.sub(r"[^a-z0-9._/-]+", "-", repo).strip("-/")
    if not repo:
        raise LumaError(f"cannot derive image name from repo url: {url}")
    return repo


def normalize_import_repo_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+(?:\.git)?", text):
        suffix = "" if text.endswith(".git") else ".git"
        return f"https://github.com/{text}{suffix}"
    return text


def _split_image_tag(image: str) -> tuple[str, str]:
    if ":" in image.rsplit("/", 1)[-1]:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, "latest"


def _registry_auth_for_service(state: Dict[str, Any], service: ServiceSpec) -> Dict[str, str] | None:
    return _registry_auth_for_image(state, service.image)


def _registry_auth_for_image(state: Dict[str, Any], image: str) -> Dict[str, str] | None:
    registries = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    return registry_auth_for_image(registries, image)


def _egress_pull_registries() -> set[str]:
    raw = os.environ.get("LUMA_EGRESS_PULL_REGISTRIES")
    if raw is None:
        return set(DEFAULT_EGRESS_PULL_REGISTRIES)
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if values == {"none"}:
        return set()
    return values


def _image_registry_host(image: str) -> str:
    image_ref = image.split("@", 1)[0]
    if "/" not in image_ref:
        return "docker.io"
    first = image_ref.split("/", 1)[0].lower()
    if "." in first or ":" in first or first == "localhost":
        return normalize_registry_host(first)
    return "docker.io"


def _no_proxy_entries_for_registry(registry: str) -> tuple[str, ...]:
    host = normalize_registry_host(public_registry_url(registry))
    entries = [host]
    if ":" in host:
        entries.append(host.rsplit(":", 1)[0])
    return tuple(entries)


def _docker_info_no_proxy(info: Dict[str, Any]) -> str:
    for key in ("NoProxy", "NOProxy", "NO_PROXY", "No_proxy", "no_proxy"):
        value = info.get(key)
        if value:
            return str(value)
    return ""


def _docker_info_no_proxy_contains(info: Dict[str, Any], required: tuple[str, ...]) -> bool:
    entries = {item.strip().lower() for item in _docker_info_no_proxy(info).split(",") if item.strip()}
    return all(item.lower() in entries for item in required)


def _merge_no_proxy(*values: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in str(value or "").split(","):
            entry = item.strip()
            if not entry:
                continue
            key = entry.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return ",".join(merged)


def _docker_info_uses_egress_proxy(info: Dict[str, Any]) -> bool:
    expected = EGRESS_PROXY_URL.rstrip("/")
    values = [
        str(info.get("HTTPProxy") or ""),
        str(info.get("HTTPSProxy") or ""),
        str(info.get("HttpProxy") or ""),
        str(info.get("HttpsProxy") or ""),
    ]
    return any(value.rstrip("/") == expected for value in values)


def _docker_pull_error_message(status: int, raw: str, *, registry_auth: Dict[str, str] | None = None, platform: str = "") -> str:
    detail = raw.strip()
    if status >= 400:
        message = f"Docker pull failed with HTTP {status}: {detail}"
    else:
        message = f"Docker pull failed: {detail}"
    lowered = detail.lower()
    if platform and any(marker in lowered for marker in ("no matching manifest", "no match for platform", "not found")):
        message += f"; image does not provide a manifest for target platform {platform}. Build and push a multi-arch image before deploying to this node."
    if status >= 500 and any(marker in lowered for marker in ("failed to do request", "eof", "timeout", "connection reset")):
        if registry_auth:
            message += (
                "; Docker daemon could not reach the private registry. Luma does not route private registry pulls through egress; "
                "verify the local Docker daemon proxy bypass with `docker info` HTTPProxy/HTTPSProxy/NoProxy."
            )
        else:
            message += (
                "; Docker daemon could not reach the registry. Verify the local Luma egress gateway and Docker daemon proxy "
                "with `luma egress setup` and `docker info` HTTPProxy/HTTPSProxy."
            )
    return message


def _image_repository(image: str) -> str:
    image_ref = image.split("@", 1)[0]
    slash = image_ref.rfind("/")
    colon = image_ref.rfind(":")
    if colon > slash:
        return image_ref[:colon]
    return image_ref
