from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict

import yaml

from .agent import (
    _auth_for_host,
    _build_compose_images,
    _buildx_environment,
    _docker_binary,
    _docker_buildx_available,
    _docker_buildx_build,
    _ensure_buildx_builder,
    _find_luma_deployment_manifest,
    _safe_repo_path_from,
    _safe_repo_subpath,
    _select_luma_compose_manifest,
    _write_docker_auth_config,
)
from .errors import LumaError


def _expose_local_docker_runtime(docker_config: Path) -> None:
    """Keep CLI plugins and named contexts visible with isolated registry auth."""
    user_docker_root = Path.home() / ".docker"
    buildx = user_docker_root / "cli-plugins" / "docker-buildx"
    if not buildx.is_file():
        buildx = None
    if buildx is not None:
        plugin_root = docker_config / "cli-plugins"
        plugin_root.mkdir(parents=True, exist_ok=True)
        destination = plugin_root / "docker-buildx"
        try:
            destination.symlink_to(buildx.resolve())
        except OSError:
            # Windows configurations may not permit unprivileged symlinks.
            shutil.copy2(buildx, destination)

    contexts = user_docker_root / "contexts"
    if contexts.is_dir():
        destination = docker_config / "contexts"
        try:
            destination.symlink_to(contexts.resolve(), target_is_directory=True)
        except OSError:
            shutil.copytree(contexts, destination)


def _current_docker_context_builder(
    docker: str, *, env: Dict[str, str]
) -> str:
    result = subprocess.run(
        [docker, "context", "show"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        timeout=15,
    )
    context = result.stdout.strip() if result.returncode == 0 else ""
    return context or "default"


def _git_output(source: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    result = subprocess.run(
        [git, "-C", str(source), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def local_source_metadata(source: Path, *, repo_url: str = "") -> Dict[str, str]:
    root = source.expanduser().resolve()
    if not root.is_dir():
        raise LumaError(f"local build path is not a directory: {root}")
    resolved_repo_url = str(repo_url or "").strip() or _git_output(root, "remote", "get-url", "origin")
    if not resolved_repo_url:
        raise LumaError("cannot infer the project repository; pass --repo-url or configure git remote origin")
    revision = _git_output(root, "rev-parse", "HEAD")
    if revision and _git_output(root, "status", "--porcelain"):
        revision += "-dirty"
    return {"path": str(root), "repoUrl": resolved_repo_url, "revision": revision}


def local_deployment_target(
    source: Path, *, compose_sidecar: str = "", region: str = ""
) -> Dict[str, Any]:
    """Read the checked-out deployment target before reserving a local build."""
    root = source.expanduser().resolve()
    selected_sidecar = str(compose_sidecar or "").strip()
    deployment_manifest = (
        ("compose", _select_luma_compose_manifest(root, selected_sidecar))
        if selected_sidecar
        else _find_luma_deployment_manifest(root)
    )
    if not deployment_manifest:
        raise LumaError("no Luma deployment manifest found in the local project")
    try:
        manifest = yaml.safe_load(deployment_manifest[1].read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid local Luma deployment manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise LumaError("local Luma deployment manifest must contain a YAML mapping")
    root_node = str(manifest.get("node") or "").strip()
    root_region = str(region or manifest.get("region") or "").strip()
    if deployment_manifest[0] != "compose":
        return {"targetNode": root_node, "targetRegion": root_region}
    nodes: set[str] = set()
    regions: set[str] = set()
    sidecar_services = manifest.get("services") if isinstance(manifest.get("services"), dict) else {}
    compose_value = str(manifest.get("compose") or "docker-compose.yml").strip() or "docker-compose.yml"
    compose_path = _safe_repo_path_from(deployment_manifest[1].parent, root, compose_value)
    if not compose_path.is_file():
        raise LumaError(f"Compose file not found in local project: {compose_value}")
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LumaError(f"invalid local Compose file: {exc}") from exc
    compose_services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(compose_services, dict) or not compose_services:
        raise LumaError("local Compose file requires a non-empty services mapping")
    for service_name in compose_services:
        service = sidecar_services.get(service_name)
        if not isinstance(service, dict):
            service = {}
        node = str(service.get("node") or root_node).strip()
        region = str(service.get("region") or root_region).strip()
        if node:
            nodes.add(node)
        elif region:
            regions.add(region)
    return {"targetNodes": sorted(nodes), "targetRegions": sorted(regions)}


def build_and_push_local_source(
    source: Path,
    *,
    registry_host: str,
    repository: str,
    tag: str,
    compose_sidecar: str = "",
    context: str = "",
    dockerfile: str = "",
    platform: str = "",
    builder: str = "",
    proxy: str = "",
    timeout: int = 7200,
    progress: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    root = source.expanduser().resolve()
    selected_builder = str(builder or "").strip()
    local_proxy = str(proxy or "").strip()
    if selected_builder and not all(
        char.isalnum() or char in "_.-" for char in selected_builder
    ):
        raise LumaError("local buildx builder name is invalid")
    user_buildx_root = Path.home() / ".docker" / "buildx" if selected_builder else None
    docker = _docker_binary()
    if not docker:
        raise LumaError("docker command not found on this computer")
    if not _docker_buildx_available():
        raise LumaError("docker buildx is not available on this computer")

    selected_sidecar = str(compose_sidecar or "").strip()
    deployment_manifest = (
        ("compose", _select_luma_compose_manifest(root, selected_sidecar))
        if selected_sidecar
        else _find_luma_deployment_manifest(root)
    )
    if not deployment_manifest:
        raise LumaError("no Luma deployment manifest found in the local project")

    def emit(event: Dict[str, Any]) -> None:
        line = str(event.get("line") or "").strip()
        if line and progress:
            progress(line)

    with tempfile.TemporaryDirectory(prefix="luma-local-build-") as workdir:
        docker_config = Path(workdir) / "docker-config"
        user_docker_config = Path.home() / ".docker" / "config.json"
        docker_config.mkdir(parents=True, exist_ok=True)
        if user_docker_config.is_file():
            shutil.copy2(user_docker_config, docker_config / "config.json")
        else:
            _write_docker_auth_config(docker_config, _auth_for_host(None, registry_host))
        _expose_local_docker_runtime(docker_config)
        if deployment_manifest[0] == "compose":
            payload: Dict[str, Any] = {}
            if context:
                payload["context"] = context
            if dockerfile:
                payload["dockerfile"] = dockerfile
            if platform:
                payload["platform"] = platform
            if selected_sidecar:
                payload["composeSidecar"] = selected_sidecar
            local_env = _buildx_environment(
                docker_config, buildx_config_root=user_buildx_root
            )
            local_builder = selected_builder or (
                _current_docker_context_builder(docker, env=local_env)
                if platform and "," not in platform
                else ""
            )
            return _build_compose_images(
                src=root,
                sidecar_path=deployment_manifest[1],
                docker=docker,
                docker_config=docker_config,
                registry_host=registry_host,
                push_host=registry_host,
                repo=repository,
                sha=tag,
                proxy=local_proxy,
                build_timeout=timeout,
                payload=payload,
                progress=emit,
                allow_repo_overrides=False,
                buildx_builder=local_builder,
                buildx_config_root=user_buildx_root,
            )

        manifest_path = deployment_manifest[1]
        manifest_text = manifest_path.read_text(encoding="utf-8")
        try:
            parsed = yaml.safe_load(manifest_text) or {}
        except yaml.YAMLError as exc:
            raise LumaError(f"invalid local Luma deployment manifest: {exc}") from exc
        build_block = parsed.get("build") if isinstance(parsed, dict) and isinstance(parsed.get("build"), dict) else {}
        context_rel = str(context or build_block.get("context") or ".").strip() or "."
        dockerfile_rel = str(dockerfile or build_block.get("dockerfile") or "Dockerfile").strip() or "Dockerfile"
        build_platform = str(platform or build_block.get("platform") or "linux/amd64").strip() or "linux/amd64"
        context_dir = _safe_repo_subpath(root, context_rel)
        dockerfile_path = _safe_repo_subpath(root, dockerfile_rel)
        if not dockerfile_path.is_file():
            raise LumaError(f"Dockerfile not found in local project: {dockerfile_rel}")
        buildx_env = _buildx_environment(
            docker_config, buildx_config_root=user_buildx_root
        )
        builder = selected_builder or (
            _current_docker_context_builder(docker, env=buildx_env)
            if "," not in build_platform
            else _ensure_buildx_builder(
                docker,
                proxy=local_proxy,
                no_proxy=f"localhost,127.0.0.1,::1,{registry_host}",
                registry_host=registry_host,
                env=buildx_env,
            )
        )
        image = _docker_buildx_build(
            docker=docker,
            builder=builder,
            docker_config=docker_config,
            push_host=registry_host,
            registry_host=registry_host,
            repo=repository,
            sha=tag,
            context_dir=context_dir,
            dockerfile_path=dockerfile_path,
            platform=build_platform,
            proxy=local_proxy,
            build_timeout=timeout,
            progress=emit,
            buildx_config_root=user_buildx_root,
        )
        return {
            "kind": "service",
            "image": image,
            "sha": tag,
            "manifest": manifest_text,
            "message": f"Built and pushed {image}",
        }
