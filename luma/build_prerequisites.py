"""Install build tools and warm BuildKit before node or runner enrollment."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from .builder_runner import (
    _apply_docker_runtime_env,
    _write_docker_config,
    _use_runner_buildx,
    DEFAULT_BASE_NO_PROXY,
    capture_docker_runtime,
    detect_native_engine_platform,
    find_docker_binary,
    merge_no_proxy,
)
from .errors import LumaError


def _probe(argv: list[str], *, env: dict | None = None) -> bool:
    try:
        return subprocess.run(argv, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _missing_tools() -> list[str]:
    missing = []
    git = shutil.which("git")
    if not git or not _probe([git, "--version"]):
        missing.append("Git")
    try:
        docker = find_docker_binary()
    except LumaError:
        return [*missing, "Docker", "Buildx"]
    # Builds use private registry credentials. Validate plugin discovery in that
    # same isolated Docker config, not just in the operator's interactive shell.
    runtime = capture_docker_runtime(docker)
    with tempfile.TemporaryDirectory(prefix="luma-runner-preflight-") as directory:
        config_dir = Path(directory)
        _write_docker_config(config_dir, [], docker_runtime=runtime)
        env = _apply_docker_runtime_env(dict(os.environ), docker_config=config_dir,
                                        docker_runtime=runtime)
        if not _probe([docker, "buildx", "version"], env=env):
            missing.append("Buildx")
    return missing


def verify_build_dependencies() -> None:
    """Fail before leasing work; a service never installs packages on restart."""
    missing = _missing_tools()
    if missing:
        raise LumaError("Build dependencies unavailable: " + ", ".join(missing)
                        + ". Rerun 'luma runner join' or 'luma node join' to install and verify them.")


def _install_command(argv: list[str], *, privileged: bool = False) -> None:
    if privileged and os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise LumaError("Installing build dependencies requires root or sudo; join was not completed")
        # Match service installation: authenticate on the terminal, never log or
        # pass the runner token to a package manager.
        if subprocess.run(["sudo", "-v"], check=False).returncode:
            raise LumaError("sudo authentication failed; join was not completed")
        argv = ["sudo", "-n", *argv]
    print("Installing build dependencies: " + " ".join(argv), file=sys.stderr, flush=True)
    try:
        result = subprocess.run(argv, stdout=sys.stderr, stderr=sys.stderr,
                                timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LumaError("Build dependency installation could not finish; join was not completed") from exc
    if result.returncode:
        raise LumaError("Build dependency installation failed; join was not completed")


def _apt_package_available(name: str) -> bool:
    result = subprocess.run(["apt-cache", "policy", name], capture_output=True,
                            text=True, timeout=20, check=False, env={**os.environ, "LC_ALL": "C"})
    return result.returncode == 0 and any(
        line.strip().startswith("Candidate:") and line.split(":", 1)[1].strip() not in {"", "(none)"}
        for line in result.stdout.splitlines()
    )


def _install_linux(missing: list[str]) -> None:
    if not shutil.which("apt-get") or not shutil.which("apt-cache"):
        raise LumaError("Automatic runner dependency installation requires apt-based Linux. "
                        "Install native Docker, Buildx and Git, then rerun 'luma runner join'.")
    _install_command(["apt-get", "update"], privileged=True)
    packages = ["git"] if "Git" in missing else []
    if "Docker" in missing:
        packages.append("docker.io")
    if "Buildx" in missing:
        # Do not mix the Ubuntu docker.io engine with Docker CE packages.
        result = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status}", "docker-ce-cli"],
                                capture_output=True, text=True, timeout=20, check=False)
        official = result.returncode == 0 and result.stdout.strip() == "installed"
        candidates = ["docker-buildx-plugin"] if official else ["docker-buildx"]
        package = next((name for name in candidates if _apt_package_available(name)), None)
        if not package:
            raise LumaError("No matching Buildx package is available from the configured apt repositories; "
                            "join was not completed. Install Buildx for the existing Docker distribution.")
        packages.append(package)
    _install_command(["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l",
                      "apt-get", "install", "-y", "--no-install-recommends", "--no-remove", *packages],
                     privileged=True)
    if "Docker" in missing:
        _install_command(["systemctl", "enable", "--now", "docker"], privileged=True)


def _native_brew() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        translated = subprocess.run(["sysctl", "-in", "sysctl.proc_translated"],
                                    capture_output=True, text=True, timeout=10, check=False)
        if translated.returncode == 0 and translated.stdout.strip() == "1":
            machine = "arm64"
    prefixes = {"arm64": "/opt/homebrew", "aarch64": "/opt/homebrew",
                "x86_64": "/usr/local", "amd64": "/usr/local"}
    prefix = prefixes.get(machine)
    if not prefix:
        raise LumaError(f"Unsupported macOS runner architecture: {machine}")
    brew = str(Path(prefix) / "bin/brew")
    if not os.access(brew, os.X_OK):
        raise LumaError(f"Install native Homebrew for {machine} at {prefix}, then rerun 'luma runner join'. "
                        "The other architecture's Homebrew will not be used.")
    return brew


def _install_macos(missing: list[str]) -> None:
    if "Docker" in missing:
        raise LumaError("Install and start OrbStack or Docker Desktop for this Mac's architecture, "
                        "then rerun 'luma runner join'. A Docker CLI alone is not a container engine.")
    if os.geteuid() == 0:
        raise LumaError("Run 'luma runner join' as the macOS user who owns Docker and Homebrew, without sudo.")
    packages = [name for tool, name in (("Git", "git"), ("Buildx", "docker-buildx")) if tool in missing]
    brew = _native_brew()
    architecture = "-arm64" if brew.startswith("/opt/homebrew/") else "-x86_64"
    _install_command(["/usr/bin/arch", architecture, brew, "install", *packages])


def prepare_build_dependencies() -> None:
    missing = _missing_tools()
    if missing:
        if sys.platform == "linux":
            _install_linux(missing)
        elif sys.platform == "darwin":
            _install_macos(missing)
        else:
            raise LumaError("Build dependency installation supports Linux and macOS only")
    verify_build_dependencies()


def _warm_frontend(docker: str, builder: str, env: dict, native_platform: str) -> None:
    from .runner_service import _diagnostic

    # This downloads the same syntax frontend used by application Dockerfiles.
    # No application image is built or pushed; BuildKit retains its own cache.
    print("Preparing BuildKit and Dockerfile frontend for " + native_platform,
          file=sys.stderr, flush=True)
    argv = [docker, "buildx", "build", "--builder", builder, "--platform", native_platform,
            "--progress", "plain", "--output", "type=cacheonly", "-"]
    try:
        result = subprocess.run(argv, env=env, text=True,
                                input="# syntax=docker/dockerfile:1\nFROM scratch\nLABEL luma.preflight=true\n",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LumaError("BuildKit frontend preflight did not finish; join was not completed. "
                        "Check BuildKit proxy, DNS and Docker Hub access.") from exc
    if result.returncode:
        raise LumaError("BuildKit cannot prepare the Dockerfile frontend; join was not completed. "
                        "Configure a reachable BuildKit proxy; Docker daemon proxy settings alone are insufficient.\n"
                        + _diagnostic(result.stdout or ""))
    print("BuildKit and Dockerfile frontend ready", file=sys.stderr, flush=True)


def prepare_runner_buildkit(daemon) -> None:
    state_dir = daemon.state_dir / "buildx"
    no_proxy = merge_no_proxy(DEFAULT_BASE_NO_PROXY, daemon.local_no_proxy)
    with tempfile.TemporaryDirectory(prefix="luma-runner-preflight-") as directory:
        config = Path(directory)
        _write_docker_config(config, [], docker_runtime=daemon.docker_runtime)
        env = _apply_docker_runtime_env(dict(os.environ), docker_config=config,
                                        buildx_state_dir=state_dir, docker_runtime=daemon.docker_runtime)
        with _use_runner_buildx(docker_bin=daemon.docker_bin, buildx_state_dir=state_dir,
                               docker_config=config, proxy=daemon.local_proxy, no_proxy=no_proxy,
                               http_hosts=[], cache_keep_gb=daemon.cache_keep_gb,
                               max_parallelism=daemon.max_parallelism,
                               docker_runtime=daemon.docker_runtime) as builder:
            _warm_frontend(daemon.docker_bin, builder, env, daemon.native_platform)


def prepare_node_buildkit(*, proxy: str = "") -> None:
    from .agent import _ensure_buildx_builder, _luma_buildx_state_root

    docker = find_docker_binary()
    runtime = capture_docker_runtime(docker)
    native_platform = detect_native_engine_platform(docker, docker_runtime=runtime)
    state_dir = _luma_buildx_state_root()
    state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="luma-node-preflight-") as directory:
        config = Path(directory)
        _write_docker_config(config, [], docker_runtime=runtime)
        env = _apply_docker_runtime_env(dict(os.environ), docker_config=config,
                                        buildx_state_dir=state_dir, docker_runtime=runtime)
        builder = _ensure_buildx_builder(docker, proxy=proxy, no_proxy=DEFAULT_BASE_NO_PROXY, env=env)
        _warm_frontend(docker, builder, env, native_platform)
