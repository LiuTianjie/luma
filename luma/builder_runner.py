"""Stateless BuildKit runner daemon for the decoupled ``luma-build`` service.

Executed by ``luma builder runner``. Connects strictly to a ``luma-build`` HTTP
endpoint with a ``lbr_...`` runner token—never touches ``luma-control`` or Nomad.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import platform as py_platform
import queue
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import yaml

from . import __version__
from .errors import LumaError

ORBSTACK_XBIN = "/Applications/OrbStack.app/Contents/MacOS/xbin"
DOCKER_APP_BIN = "/Applications/Docker.app/Contents/Resources/bin"
_TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SAFE_HOST_RE = re.compile(r"^(?:\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?$")
_GIT_REF_INVALID_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")

_BUILDX_COND = threading.Condition()
_ACTIVE_BUILDER_JOBS: Dict[str, int] = {}


class _RunnerCanceled(LumaError):
    """Raised when a leased build is canceled or superseded."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, f"unexpected redirect ({code})", headers, fp)


def normalize_registry_host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise LumaError("registry host is required")
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise LumaError(f"registry host must not include a path or query: {value}")
        raw = parsed.netloc
    raw = raw.strip().rstrip("/")
    if not raw or not _SAFE_HOST_RE.fullmatch(raw):
        raise LumaError(f"invalid registry host: {value}")
    try:
        parsed = urllib.parse.urlsplit("http://" + raw)
        port = parsed.port
    except ValueError as exc:
        raise LumaError(f"invalid registry host: {value}") from exc
    if not parsed.hostname or parsed.username or parsed.password:
        raise LumaError(f"invalid registry host: {value}")
    if port is not None and not (1 <= port <= 65535):
        raise LumaError(f"invalid registry port in host: {value}")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port else host


def normalize_repo_url(repo_url: str) -> str:
    url = str(repo_url or "").strip()
    if not url or url.startswith("-") or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        raise LumaError(f"invalid repository URL: {url}")
    if not url.startswith(("https://", "http://", "ssh://", "git@")):
        raise LumaError(f"unsupported repository URL scheme: {url}")
    parsed = urllib.parse.urlsplit(url if "://" in url else "ssh://" + url.replace(":", "/", 1))
    if parsed.query or parsed.fragment:
        raise LumaError("repoUrl must not contain query parameters or fragments")
    if url.startswith(("https://", "http://")) and (parsed.username or parsed.password):
        raise LumaError("repoUrl must not embed inline credentials")
    return url


def validate_git_ref(value: str) -> str:
    ref = str(value or "main").strip() or "main"
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/") :]
    if (
        not ref
        or len(ref) > 200
        or ref.startswith("-")
        or ref.endswith(".")
        or ref.endswith(".lock")
        or ".." in ref
        or "@{" in ref
        or "//" in ref
        or _GIT_REF_INVALID_RE.search(ref)
    ):
        raise LumaError(f"invalid Git ref: {value!r}")
    return ref


def normalize_platform(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if len(parts) < 2:
        raise LumaError(f"invalid container platform: {value}")
    os_name, arch = parts[0], parts[1]
    if os_name == "darwin":
        os_name = "linux"
    if arch in {"x86_64", "amd64"}:
        arch = "amd64"
    elif arch in {"aarch64", "arm64"}:
        arch = "arm64"
    elif arch in {"armv7l", "armhf", "arm"}:
        return f"{os_name}/arm/v7"
    if len(parts) >= 3 and parts[2] not in {"v8"}:
        return f"{os_name}/{arch}/{parts[2]}"
    return f"{os_name}/{arch}"


def parse_platforms(value: Any) -> List[str]:
    if not value:
        return ["linux/amd64"]
    items: Iterable[Any] = [value] if isinstance(value, str) else list(value)
    result: List[str] = []
    for item in items:
        for part in str(item).split(","):
            norm = normalize_platform(part)
            if norm and norm not in result:
                result.append(norm)
    return result or ["linux/amd64"]


def required_native_arm_platform(platforms: Iterable[str]) -> str:
    arm_targets = {p for p in parse_platforms(list(platforms)) if "/arm64" in p or "/arm/" in p}
    if len(arm_targets) > 1:
        raise LumaError("cannot combine multiple ARM architectures in one build; ARM emulation is disabled")
    return next(iter(arm_targets), "")


def _slugify_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if not token:
        raise LumaError(f"cannot slugify empty identifier: {value!r}")
    return token


def format_image_repository(
    *,
    repo_slug: str,
    service_name: str = "",
    single_service: bool = True,
    namespace: str = "",
    flatten: Optional[bool] = None,
) -> str:
    ns = str(namespace or "").strip().strip("/").lower()
    if ns and not _NAMESPACE_RE.fullmatch(ns):
        raise LumaError(f"invalid registry namespace: {namespace}")
    should_flatten = bool(ns) if flatten is None else bool(flatten)
    segments = [p for p in repo_slug.strip("/").lower().split("/") if p]
    if service_name and not single_service:
        segments.append(_slugify_token(service_name))
    if should_flatten:
        leaf = "-".join(_slugify_token(seg) for seg in segments)[:120].strip("-")
        return f"{ns}/{leaf}" if ns else leaf
    path = "/".join(segments)
    return f"{ns}/{path}" if ns else path


def format_cache_reference(
    *,
    push_host: str,
    repo_slug: str,
    service_name: str = "",
    platform: str = "linux/amd64",
    namespace: str = "",
    cache_repo: str = "luma-buildcache",
) -> str:
    host = normalize_registry_host(push_host)
    ns = str(namespace or "").strip().strip("/").lower()
    repo_name = _slugify_token(cache_repo)
    full_repo = f"{host}/{ns}/{repo_name}" if ns else f"{host}/{repo_name}"
    parts = [_slugify_token(seg) for seg in repo_slug.strip("/").split("/") if seg]
    if service_name:
        parts.append(_slugify_token(service_name))
    arch_slug = _slugify_token(platform.replace("/", "-"))
    return f"{full_repo}:{'-'.join(parts)[:96]}-{arch_slug}"


def find_docker_binary() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        f"{ORBSTACK_XBIN}/docker",
        f"{DOCKER_APP_BIN}/docker",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise LumaError("docker CLI not found on runner host")


def extra_cli_plugin_dirs() -> List[str]:
    dirs: List[str] = []
    for candidate in (
        ORBSTACK_XBIN,
        "/usr/libexec/docker/cli-plugins",
        "/usr/local/lib/docker/cli-plugins",
        "/opt/homebrew/lib/docker/cli-plugins",
        f"{DOCKER_APP_BIN}/cli-plugins",
    ):
        if os.path.isfile(os.path.join(candidate, "docker-buildx")):
            dirs.append(candidate)
    return dirs


def detect_native_engine_platform(docker_bin: str) -> str:
    proc = subprocess.run(
        [docker_bin, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0:
        raise LumaError(f"unable to query Docker daemon on runner: {(proc.stdout or '').strip()}")
    return normalize_platform(proc.stdout.strip())


def _safe_subpath(root: Path, rel: str) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / (rel or ".")).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise LumaError(f"path escapes repository root: {rel}")
    return target


def _discover_manifest(repo_root: Path, compose_sidecar: str = "") -> Optional[Tuple[str, Path]]:
    if compose_sidecar:
        target = _safe_subpath(repo_root, compose_sidecar)
        if not target.is_file():
            raise LumaError(f"composeSidecar not found in repository: {compose_sidecar}")
        return "compose", target
    for name in (
        "luma.compose.yml",
        "luma.compose.yaml",
        ".luma.compose.yml",
        ".luma.compose.yaml",
        "docker-compose.luma.yml",
        "docker-compose.luma.yaml",
    ):
        candidate = repo_root / name
        if candidate.is_file():
            return "compose", candidate
    for name in (".luma.yml", ".luma.yaml", "luma.yml", "luma.yaml"):
        candidate = repo_root / name
        if candidate.is_file():
            return "service", candidate
    return None


def _write_docker_config(config_dir: Path, registry_auths: List[Dict[str, str]]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.chmod(0o700)
    auths_obj: Dict[str, Any] = {}
    for entry in registry_auths:
        host = str(entry.get("host") or "").strip()
        user = str(entry.get("username") or "")
        pw = str(entry.get("password") or "")
        if host and user and pw:
            token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
            auths_obj[host] = {"auth": token}
    payload: Dict[str, Any] = {}
    if auths_obj:
        payload["auths"] = auths_obj
    plugin_dirs = extra_cli_plugin_dirs()
    if plugin_dirs:
        payload["cliPluginsExtraDirs"] = plugin_dirs
    cfg_file = config_dir / "config.json"
    cfg_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    cfg_file.chmod(0o600)


def _clone_repo(
    *,
    repo_url: str,
    ref: str,
    dest: Path,
    git_auth: Optional[Dict[str, str]],
    proxy: str = "",
) -> Tuple[str, str]:
    safe_url = normalize_repo_url(repo_url)
    safe_ref = validate_git_ref(ref)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy

    git_base = [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "protocol.http.allow=always",
        "-c",
        "protocol.ssh.allow=always",
        "-c",
        "http.followRedirects=false",
        "-c",
        f"core.hooksPath={os.devnull}",
    ]

    with tempfile.TemporaryDirectory(prefix="luma-runner-git-") as auth_dir_raw:
        auth_dir = Path(auth_dir_raw)
        auth_dir.chmod(0o700)
        if git_auth and git_auth.get("token") and safe_url.startswith(("https://", "http://")):
            ufile = auth_dir / "u"
            pfile = auth_dir / "p"
            askpass = auth_dir / "askpass.sh"
            ufile.write_text(str(git_auth.get("username") or "x-access-token"), encoding="utf-8")
            pfile.write_text(str(git_auth["token"]), encoding="utf-8")
            askpass.write_text(
                '#!/bin/sh\ncase "$1" in *Username*|*username*) cat "$LUMA_U" ;; *) cat "$LUMA_P" ;; esac\n',
                encoding="utf-8",
            )
            ufile.chmod(0o600)
            pfile.chmod(0o600)
            askpass.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass)
            env["LUMA_U"] = str(ufile)
            env["LUMA_P"] = str(pfile)

        if re.fullmatch(r"[0-9a-fA-F]{40}", safe_ref):
            cmds = [
                [*git_base, "init", "--", str(dest)],
                [*git_base, "-C", str(dest), "remote", "add", "origin", "--", safe_url],
                [*git_base, "-C", str(dest), "fetch", "--depth", "1", "--no-tags", "origin", safe_ref],
                [*git_base, "-C", str(dest), "checkout", "--detach", "FETCH_HEAD"],
            ]
        else:
            cmds = [[*git_base, "clone", "--depth", "1", "--branch", safe_ref, "--", safe_url, str(dest)]]

        for cmd in cmds:
            res = subprocess.run(
                cmd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=300,
            )
            if res.returncode != 0:
                out = (res.stdout or "").strip()
                if git_auth and git_auth.get("token"):
                    out = out.replace(git_auth["token"], "***")
                raise LumaError(f"git fetch failed: {out}")

    short_sha = subprocess.check_output(
        ["git", "-C", str(dest), "rev-parse", "--short=12", "HEAD"], text=True, timeout=15
    ).strip()
    full_sha = subprocess.check_output(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], text=True, timeout=15
    ).strip()
    return short_sha, full_sha


def _render_buildkitd_toml(*, http_hosts: List[str], cache_keep_gb: int = 20, max_parallelism: int = 0) -> str:
    keep_bytes = max(int(cache_keep_gb), 2) * 1024 * 1024 * 1024
    lines = ["[worker.oci]", "  enabled = true", "  gc = true"]
    if max_parallelism > 0:
        lines.append(f"  max-parallelism = {int(max_parallelism)}")
    lines.extend(
        [
            "",
            "[[worker.oci.gcpolicy]]",
            f"  keepBytes = {keep_bytes}",
            "  keepDuration = 604800",
            '  filters = ["type==source.local", "type==exec.cachemount", "type==source.git.checkout"]',
            "",
            "[[worker.oci.gcpolicy]]",
            f"  keepBytes = {keep_bytes}",
            "  all = true",
        ]
    )
    for host in sorted({h for h in http_hosts if h}):
        lines.extend(["", f'[registry."{host}"]', "  http = true", "  insecure = true"])
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def _use_runner_buildx(
    *,
    docker_bin: str,
    buildx_state_dir: Path,
    docker_config: Path,
    proxy: str,
    no_proxy: str,
    http_hosts: List[str],
    cache_keep_gb: int,
    max_parallelism: int,
) -> Iterator[str]:
    with _BUILDX_COND:
        builder_name = _ensure_runner_buildx_locked(
            docker_bin=docker_bin,
            buildx_state_dir=buildx_state_dir,
            docker_config=docker_config,
            proxy=proxy,
            no_proxy=no_proxy,
            http_hosts=http_hosts,
            cache_keep_gb=cache_keep_gb,
            max_parallelism=max_parallelism,
        )
        _ACTIVE_BUILDER_JOBS[builder_name] = _ACTIVE_BUILDER_JOBS.get(builder_name, 0) + 1
    try:
        yield builder_name
    finally:
        with _BUILDX_COND:
            remaining = max(_ACTIVE_BUILDER_JOBS.get(builder_name, 1) - 1, 0)
            if remaining:
                _ACTIVE_BUILDER_JOBS[builder_name] = remaining
            else:
                _ACTIVE_BUILDER_JOBS.pop(builder_name, None)
            _BUILDX_COND.notify_all()


def _ensure_runner_buildx_locked(
    *,
    docker_bin: str,
    buildx_state_dir: Path,
    docker_config: Path,
    proxy: str,
    no_proxy: str,
    http_hosts: List[str],
    cache_keep_gb: int,
    max_parallelism: int,
) -> str:
    name = "luma-runner-egress" if proxy else "luma-runner"
    buildx_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = buildx_state_dir / f".{name}.spec.json"
    prev_spec: Dict[str, Any] = {}
    if marker.is_file():
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_spec = loaded

    merged_http_hosts = sorted(
        {normalize_registry_host(h) for h in [*(prev_spec.get("httpHosts") or []), *http_hosts] if h}
    )
    prev_no_proxy = [p.strip() for p in str(prev_spec.get("noProxy") or "").split(",") if p.strip()]
    curr_no_proxy = [p.strip() for p in str(no_proxy or "").split(",") if p.strip()]
    merged_no_proxy = ",".join(dict.fromkeys([*prev_no_proxy, *curr_no_proxy]))

    toml_body = _render_buildkitd_toml(
        http_hosts=merged_http_hosts,
        cache_keep_gb=cache_keep_gb,
        max_parallelism=max_parallelism,
    )
    desired_spec = {
        "toml": toml_body,
        "proxy": proxy,
        "noProxy": merged_no_proxy,
        "httpHosts": merged_http_hosts,
    }
    fingerprint = hashlib.sha256(json.dumps(desired_spec, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    env = dict(os.environ)
    env["DOCKER_CONFIG"] = str(docker_config)
    env["BUILDX_CONFIG"] = str(buildx_state_dir)

    inspect_res = subprocess.run(
        [docker_bin, "buildx", "inspect", name],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    if inspect_res.returncode == 0 and prev_spec.get("fingerprint") == fingerprint:
        return name

    while _ACTIVE_BUILDER_JOBS.get(name, 0) > 0:
        _BUILDX_COND.wait(timeout=1.0)

    if inspect_res.returncode == 0:
        rm_keep = subprocess.run(
            [docker_bin, "buildx", "rm", "--keep-state", "-f", name],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        if rm_keep.returncode != 0:
            subprocess.run(
                [docker_bin, "buildx", "rm", "-f", name],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
            )
    subprocess.run(
        [docker_bin, "rm", "-f", f"buildx_buildkit_{name}0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )

    toml_path = buildx_state_dir / f"{name}.buildkitd.toml"
    toml_path.write_text(toml_body, encoding="utf-8")
    create_cmd = [
        docker_bin,
        "buildx",
        "create",
        "--name",
        name,
        "--driver",
        "docker-container",
        "--driver-opt",
        "network=host",
        "--buildkitd-config",
        str(toml_path),
    ]
    if proxy:
        no_proxy_opt = f'"env.NO_PROXY={merged_no_proxy}"' if "," in merged_no_proxy else f"env.NO_PROXY={merged_no_proxy}"
        create_cmd.extend(
            [
                "--driver-opt",
                f"env.HTTP_PROXY={proxy}",
                "--driver-opt",
                f"env.HTTPS_PROXY={proxy}",
                "--driver-opt",
                no_proxy_opt,
            ]
        )
    create_cmd.append("--bootstrap")
    created = subprocess.run(
        create_cmd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=180,
    )
    if created.returncode != 0:
        raise LumaError(f"failed to initialize BuildKit builder {name}:\n{(created.stdout or '').strip()}")
    marker.write_text(
        json.dumps({**desired_spec, "fingerprint": fingerprint}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return name


def _terminate_process_group(proc: Any) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def _run_buildx_command(
    *,
    docker_bin: str,
    builder_name: str,
    buildx_state_dir: Path,
    docker_config: Path,
    push_host: str,
    pull_host: str,
    image_repo: str,
    tag: str,
    context_dir: Path,
    dockerfile_path: Path,
    platforms: List[str],
    push_insecure: bool,
    cache_mode: str,
    cache_ref: str,
    no_cache: bool,
    proxy: str,
    no_proxy: str,
    build_args: Dict[str, str],
    target: str,
    timeout: int,
    metadata_file: Path,
    on_line: Optional[Callable[[str], None]],
    cancel_event: threading.Event,
) -> Tuple[str, str]:
    push_tag = f"{push_host}/{image_repo}:{tag}"
    platform_arg = ",".join(platforms)
    output_spec = "type=image,push=true" + (",registry.insecure=true" if push_insecure else "")

    cmd = [
        docker_bin,
        "buildx",
        "build",
        "--builder",
        builder_name,
        "--platform",
        platform_arg,
        "--metadata-file",
        str(metadata_file),
        "--output",
        output_spec,
        "-t",
        push_tag,
        "-f",
        str(dockerfile_path),
    ]
    if target:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", target):
            raise LumaError(f"invalid Dockerfile target stage: {target!r}")
        cmd.append(f"--target={target}")
    if no_cache:
        cmd.append("--no-cache")
    elif cache_ref and cache_mode in {"read-write", "read-only"}:
        insecure_suffix = ",registry.insecure=true" if push_insecure else ""
        cmd.extend(["--cache-from", f"type=registry,ref={cache_ref}{insecure_suffix}"])
        if cache_mode == "read-write":
            cmd.extend(
                [
                    "--cache-to",
                    f"type=registry,ref={cache_ref},mode=min,image-manifest=true,oci-mediatypes=true{insecure_suffix}",
                ]
            )

    if proxy:
        for k, v in (("HTTP_PROXY", proxy), ("HTTPS_PROXY", proxy), ("NO_PROXY", no_proxy)):
            cmd.append(f"--build-arg={k}={v}")
    for k, v in sorted(build_args.items()):
        key_clean = str(k).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_clean):
            raise LumaError(f"invalid build arg name: {k!r}")
        cmd.append(f"--build-arg={key_clean}={v}")
    cmd.extend(["--", str(context_dir)])

    env = dict(os.environ)
    env["DOCKER_CONFIG"] = str(docker_config)
    env["BUILDX_CONFIG"] = str(buildx_state_dir)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = no_proxy

    proc = subprocess.Popen(
        cmd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + max(timeout, 60)
    tail_lines: List[str] = []
    line_queue: queue.Queue = queue.Queue()

    def _stdout_reader() -> None:
        stream = proc.stdout
        assert stream is not None
        try:
            for raw_line in iter(stream.readline, ""):
                line_queue.put(raw_line)
        finally:
            with contextlib.suppress(OSError):
                stream.close()
            line_queue.put(None)

    reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
    reader_thread.start()
    eof_seen = False
    try:
        while not eof_seen:
            if cancel_event.is_set():
                _terminate_process_group(proc)
                raise _RunnerCanceled("build canceled")
            if time.monotonic() > deadline:
                _terminate_process_group(proc)
                raise LumaError(f"docker buildx build timed out after {timeout}s")
            try:
                item = line_queue.get(timeout=0.4)
            except queue.Empty:
                if proc.poll() is not None and line_queue.empty():
                    break
                continue
            if item is None:
                eof_seen = True
                break
            clean = item.rstrip("\r\n")
            if clean:
                tail_lines.append(clean)
                if len(tail_lines) > 80:
                    tail_lines.pop(0)
                if on_line:
                    on_line(clean)
        rc = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            _terminate_process_group(proc)
        reader_thread.join(timeout=2)
        if proc.stdout is not None and not proc.stdout.closed:
            with contextlib.suppress(OSError):
                proc.stdout.close()

    if cancel_event.is_set():
        raise _RunnerCanceled("build canceled")
    if rc != 0:
        raise LumaError("docker buildx build failed:\n" + "\n".join(tail_lines[-40:]))

    digest = ""
    if metadata_file.is_file():
        try:
            meta = json.loads(metadata_file.read_text(encoding="utf-8"))
            raw_digest = str(meta.get("containerimage.digest") or "").strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", raw_digest):
                digest = raw_digest
        except (OSError, ValueError):
            digest = ""

    pull_tag_ref = f"{pull_host}/{image_repo}:{tag}"
    immutable_ref = f"{pull_host}/{image_repo}@{digest}" if digest else pull_tag_ref
    return immutable_ref, pull_tag_ref


def _build_compose_project(
    *,
    src_dir: Path,
    sidecar_path: Path,
    docker_bin: str,
    builder_name: str,
    buildx_state_dir: Path,
    docker_cfg: Path,
    push_host: str,
    pull_host: str,
    repo_slug: str,
    namespace: str,
    flatten: Optional[bool],
    tag: str,
    short_sha: str,
    full_sha: str,
    platforms: List[str],
    push_insecure: bool,
    cache_mode: str,
    no_cache: bool,
    proxy: str,
    no_proxy: str,
    timeout: int,
    tmp: Path,
    on_line: Optional[Callable[[str], None]],
    cancel: threading.Event,
) -> Dict[str, Any]:
    sidecar_text = sidecar_path.read_text(encoding="utf-8")
    sidecar = yaml.safe_load(sidecar_text) or {}
    if not isinstance(sidecar, dict):
        raise LumaError("luma.compose.yml must be a YAML mapping")
    compose_rel = str(sidecar.get("compose") or "docker-compose.yml")
    compose_path = (sidecar_path.parent / compose_rel).resolve()
    if src_dir.resolve() not in compose_path.parents and compose_path != src_dir.resolve():
        raise LumaError(f"compose file escapes repository: {compose_rel}")
    if not compose_path.is_file():
        raise LumaError(f"compose file not found: {compose_rel}")

    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    services = compose_data.get("services") if isinstance(compose_data, dict) else None
    if not isinstance(services, dict) or not services:
        raise LumaError("docker-compose.yml must define a non-empty services mapping")

    build_targets: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    declared_aliases: Dict[str, str] = {}
    for svc_name, svc_body in services.items():
        if not isinstance(svc_body, dict):
            continue
        raw_build = svc_body.get("build")
        if isinstance(raw_build, str):
            spec = {"context": raw_build}
        elif isinstance(raw_build, dict):
            spec = dict(raw_build)
        else:
            continue
        build_targets.append((str(svc_name), svc_body, spec))
        if svc_body.get("image"):
            declared_aliases[str(svc_name)] = str(svc_body["image"])

    single_build = len(build_targets) == 1
    built_images: Dict[str, str] = {}
    alias_replacements: Dict[str, str] = {}
    ambiguous_aliases: set = set()

    for idx, (svc_name, svc_body, spec) in enumerate(build_targets):
        ctx_dir = (compose_path.parent / str(spec.get("context") or ".")).resolve()
        if src_dir.resolve() not in ctx_dir.parents and ctx_dir != src_dir.resolve():
            raise LumaError(f"service {svc_name} build context escapes repository")
        df_rel = str(spec.get("dockerfile") or "Dockerfile")
        df_path = (ctx_dir / df_rel).resolve()
        if not df_path.is_file():
            raise LumaError(f"Dockerfile not found for compose service {svc_name}: {df_rel}")

        raw_args = spec.get("args")
        svc_args: Dict[str, str] = {}
        if isinstance(raw_args, dict):
            svc_args = {str(k): str(v) for k, v in raw_args.items()}
        elif isinstance(raw_args, list):
            for entry in raw_args:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    svc_args[k] = v

        image_repo = format_image_repository(
            repo_slug=repo_slug,
            service_name=svc_name,
            single_service=single_build,
            namespace=namespace,
            flatten=flatten,
        )
        cache_ref = format_cache_reference(
            push_host=push_host,
            repo_slug=repo_slug,
            service_name="" if single_build else svc_name,
            platform=platforms[0],
            namespace=namespace,
        )
        if on_line:
            on_line(f"Building compose service {svc_name} -> {push_host}/{image_repo}:{tag}")

        immutable_ref, _ = _run_buildx_command(
            docker_bin=docker_bin,
            builder_name=builder_name,
            buildx_state_dir=buildx_state_dir,
            docker_config=docker_cfg,
            push_host=push_host,
            pull_host=pull_host,
            image_repo=image_repo,
            tag=tag,
            context_dir=ctx_dir,
            dockerfile_path=df_path,
            platforms=platforms,
            push_insecure=push_insecure,
            cache_mode=cache_mode,
            cache_ref=cache_ref,
            no_cache=no_cache,
            proxy=proxy,
            no_proxy=no_proxy,
            build_args=svc_args,
            target=str(spec.get("target") or ""),
            timeout=timeout,
            metadata_file=tmp / f"meta-{idx}.json",
            on_line=on_line,
            cancel_event=cancel,
        )
        svc_body["image"] = immutable_ref
        svc_body.pop("build", None)
        built_images[svc_name] = immutable_ref
        if svc_name in declared_aliases:
            alias = declared_aliases[svc_name]
            prev = alias_replacements.get(alias)
            if prev and prev != immutable_ref:
                ambiguous_aliases.add(alias)
            else:
                alias_replacements[alias] = immutable_ref

    for svc_name, svc_body in services.items():
        if svc_name in built_images or not isinstance(svc_body, dict):
            continue
        orig_img = str(svc_body.get("image") or "")
        if orig_img and orig_img not in ambiguous_aliases and orig_img in alias_replacements:
            svc_body["image"] = alias_replacements[orig_img]

    return {
        "kind": "compose",
        "image": next(iter(built_images.values()), ""),
        "images": built_images,
        "commit": full_sha,
        "shortSha": short_sha,
        "manifest": sidecar_text,
        "composeContent": yaml.safe_dump(compose_data, sort_keys=False, allow_unicode=False),
        "cacheMode": cache_mode,
    }


def execute_build_task(
    task: Dict[str, Any],
    *,
    docker_bin: str,
    native_platform: str,
    buildx_state_dir: Path,
    local_proxy: str = "",
    cache_keep_gb: int = 20,
    max_parallelism: int = 0,
    on_line: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    cancel = cancel_event or threading.Event()
    repo_url = str(task["repoUrl"])
    repo_slug = str(task["repoSlug"])
    ref = str(task.get("ref") or "main")
    pull_host = normalize_registry_host(str(task["registryHost"]))
    push_host = normalize_registry_host(str(task.get("pushHost") or pull_host))
    namespace = str(task.get("registryNamespace") or "")
    flatten = task.get("registryFlatten")
    platforms = parse_platforms(task.get("platforms") or [native_platform])
    push_insecure = bool(task.get("pushInsecure", False))
    pull_insecure = bool(task.get("pullInsecure", False))
    cache_mode = str(task.get("cacheMode") or "read-write")
    no_cache = bool(task.get("noCache", False))
    timeout = int(task.get("buildTimeout") or 7200)

    arm_needed = required_native_arm_platform(platforms)
    if arm_needed and native_platform != arm_needed:
        raise LumaError(
            f"ARM build requires native {arm_needed} engine, but runner has {native_platform}; "
            "ARM QEMU emulation is disabled"
        )

    http_hosts: List[str] = []
    if push_insecure:
        http_hosts.append(push_host)
    if pull_insecure:
        http_hosts.append(pull_host)
    no_proxy = ",".join(dict.fromkeys(["localhost", "127.0.0.1", "::1", "100.64.0.0/10", push_host, pull_host]))

    with tempfile.TemporaryDirectory(prefix="luma-build-job-") as tmp_raw:
        tmp = Path(tmp_raw)
        tmp.chmod(0o700)
        src_dir = tmp / "src"
        docker_cfg = tmp / "docker-config"
        _write_docker_config(docker_cfg, list(task.get("registryAuths") or []))

        if on_line:
            on_line(f"Cloning {repo_url} ({ref})...")
        short_sha, full_sha = _clone_repo(
            repo_url=repo_url,
            ref=ref,
            dest=src_dir,
            git_auth=task.get("gitAuth"),
            proxy=local_proxy,
        )
        if cancel.is_set():
            raise _RunnerCanceled("build canceled")

        tag = f"{short_sha}-{os.urandom(4).hex()}"
        with _use_runner_buildx(
            docker_bin=docker_bin,
            buildx_state_dir=buildx_state_dir,
            docker_config=docker_cfg,
            proxy=local_proxy,
            no_proxy=no_proxy,
            http_hosts=http_hosts,
            cache_keep_gb=cache_keep_gb,
            max_parallelism=max_parallelism,
        ) as builder_name:
            discovered = _discover_manifest(src_dir, str(task.get("composeSidecar") or ""))
            if discovered and discovered[0] == "compose":
                return _build_compose_project(
                    src_dir=src_dir,
                    sidecar_path=discovered[1],
                    docker_bin=docker_bin,
                    builder_name=builder_name,
                    buildx_state_dir=buildx_state_dir,
                    docker_cfg=docker_cfg,
                    push_host=push_host,
                    pull_host=pull_host,
                    repo_slug=repo_slug,
                    namespace=namespace,
                    flatten=flatten,
                    tag=tag,
                    short_sha=short_sha,
                    full_sha=full_sha,
                    platforms=platforms,
                    push_insecure=push_insecure,
                    cache_mode=cache_mode,
                    no_cache=no_cache,
                    proxy=local_proxy,
                    no_proxy=no_proxy,
                    timeout=timeout,
                    tmp=tmp,
                    on_line=on_line,
                    cancel=cancel,
                )

            manifest_text = discovered[1].read_text(encoding="utf-8") if discovered else str(task.get("manifest") or "")
            parsed = yaml.safe_load(manifest_text) if manifest_text.strip() else {}
            build_block = parsed.get("build") if isinstance(parsed, dict) and isinstance(parsed.get("build"), dict) else {}

            ctx_rel = str(task.get("context") or build_block.get("context") or ".")
            df_rel = str(task.get("dockerfile") or build_block.get("dockerfile") or "Dockerfile")
            target = str(task.get("target") or build_block.get("target") or "")
            merged_args = {
                **({str(k): str(v) for k, v in (build_block.get("args") or {}).items()} if isinstance(build_block.get("args"), dict) else {}),
                **({str(k): str(v) for k, v in (task.get("buildArgs") or {}).items()} if isinstance(task.get("buildArgs"), dict) else {}),
            }

            context_dir = _safe_subpath(src_dir, ctx_rel)
            dockerfile_path = _safe_subpath(src_dir, df_rel)
            if not dockerfile_path.is_file():
                raise LumaError(f"Dockerfile not found: {df_rel}")

            image_repo = format_image_repository(
                repo_slug=repo_slug,
                single_service=True,
                namespace=namespace,
                flatten=flatten,
            )
            cache_ref = format_cache_reference(
                push_host=push_host,
                repo_slug=repo_slug,
                platform=platforms[0],
                namespace=namespace,
            )
            immutable_ref, tag_ref = _run_buildx_command(
                docker_bin=docker_bin,
                builder_name=builder_name,
                buildx_state_dir=buildx_state_dir,
                docker_config=docker_cfg,
                push_host=push_host,
                pull_host=pull_host,
                image_repo=image_repo,
                tag=tag,
                context_dir=context_dir,
                dockerfile_path=dockerfile_path,
                platforms=platforms,
                push_insecure=push_insecure,
                cache_mode=cache_mode,
                cache_ref=cache_ref,
                no_cache=no_cache,
                proxy=local_proxy,
                no_proxy=no_proxy,
                build_args=merged_args,
                target=target,
                timeout=timeout,
                metadata_file=tmp / "meta-service.json",
                on_line=on_line,
                cancel_event=cancel,
            )
            return {
                "kind": "service",
                "image": immutable_ref,
                "tagImage": tag_ref,
                "commit": full_sha,
                "shortSha": short_sha,
                "manifest": manifest_text,
                "cacheMode": cache_mode,
                "cacheRef": cache_ref if cache_mode != "local-only" else "",
            }


class RunnerDaemon:
    """Reverse-polling BuildKit runner daemon."""

    def __init__(
        self,
        *,
        server_url: str,
        runner_token: str,
        state_dir: Path,
        local_proxy: str = "",
        cache_mode: str = "",
        cache_keep_gb: int = 20,
        max_parallelism: int = 0,
        workers: int = 1,
        insecure_tls: bool = False,
        poll_interval: float = 3.0,
    ) -> None:
        self.server_url = server_url.strip().rstrip("/")
        self.runner_token = runner_token.strip()
        self.state_dir = Path(state_dir).expanduser()
        self.local_proxy = local_proxy.strip()
        self.cache_mode = cache_mode.strip()
        self.cache_keep_gb = cache_keep_gb
        self.max_parallelism = max_parallelism
        self.workers = max(1, min(int(workers), 16))
        self.insecure_tls = insecure_tls
        self.poll_interval = max(poll_interval, 0.5)
        self.stop_event = threading.Event()
        self.docker_bin = find_docker_binary()
        self.native_platform = detect_native_engine_platform(self.docker_bin)

    def _post(self, path: str, body: Dict[str, Any], *, timeout: int = 35) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.server_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.runner_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        handlers: List[Any] = [NoRedirectHandler()]
        if self.insecure_tls:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def run_once_or_forever(self, *, once: bool = False) -> int:
        print(
            f"[luma-runner] online -> {self.server_url} | native={self.native_platform} "
            f"| workers={self.workers} | cache_keep={self.cache_keep_gb}GiB "
            f"| proxy={'set' if self.local_proxy else 'direct'}",
            flush=True,
        )
        if once or self.workers == 1:
            return self._worker_loop(once=once)
        threads = [
            threading.Thread(target=self._worker_loop, kwargs={"once": False}, daemon=True, name=f"luma-runner-{i}")
            for i in range(self.workers)
        ]
        for t in threads:
            t.start()
        try:
            while not self.stop_event.wait(1.0):
                if not any(t.is_alive() for t in threads):
                    break
        except KeyboardInterrupt:
            self.stop_event.set()
            return 130
        return 0

    def _worker_loop(self, *, once: bool = False) -> int:
        os_name = sys.platform
        arch = py_platform.machine().lower()
        while not self.stop_event.is_set():
            try:
                lease = self._post(
                    "/v1/runner/lease",
                    {
                        "nativePlatform": self.native_platform,
                        "os": os_name,
                        "arch": arch,
                        "version": __version__,
                        "cacheMode": self.cache_mode,
                        "waitSeconds": 0 if once else 15,
                    },
                    timeout=30,
                )
            except Exception as exc:
                if once:
                    raise LumaError(f"runner poll failed: {exc}") from exc
                print(f"[luma-runner] poll error: {exc}", file=sys.stderr, flush=True)
                self.stop_event.wait(self.poll_interval)
                continue

            task = lease.get("task")
            if isinstance(task, dict) and task.get("buildId"):
                self._run_leased_task(task)
                if once:
                    return 0
                continue
            if once:
                return 0
            self.stop_event.wait(1.0)
        return 0

    def _run_leased_task(self, task: Dict[str, Any]) -> None:
        build_id = str(task["buildId"])
        cancel_event = threading.Event()
        log_queue: queue.Queue = queue.Queue()
        hb_stop = threading.Event()

        def flush_logs(*, drain: bool = False) -> None:
            while True:
                batch: List[Dict[str, Any]] = []
                while not log_queue.empty() and len(batch) < 80:
                    try:
                        batch.append(log_queue.get_nowait())
                    except queue.Empty:
                        break
                if not batch:
                    break
                try:
                    res = self._post("/v1/runner/progress", {"buildId": build_id, "events": batch}, timeout=15)
                    if res.get("cancelRequested"):
                        cancel_event.set()
                except Exception:
                    break
                if not drain:
                    break

        def heartbeat_loop() -> None:
            while not hb_stop.wait(2.5):
                flush_logs()
                try:
                    hb = self._post("/v1/runner/heartbeat", {"buildId": build_id}, timeout=10)
                    if hb.get("cancelRequested"):
                        cancel_event.set()
                except Exception:
                    pass

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        hb_thread.start()
        status = "succeeded"
        message = ""
        result: Dict[str, Any] = {}
        try:
            result = execute_build_task(
                task,
                docker_bin=self.docker_bin,
                native_platform=self.native_platform,
                buildx_state_dir=self.state_dir / "buildx",
                local_proxy=self.local_proxy,
                cache_keep_gb=self.cache_keep_gb,
                max_parallelism=self.max_parallelism,
                on_line=lambda line: log_queue.put({"kind": "log", "line": line, "ts": int(time.time())}),
                cancel_event=cancel_event,
            )
            message = f"Built and pushed {result.get('image') or ''}"
        except _RunnerCanceled as exc:
            status = "canceled"
            message = str(exc)
        except Exception as exc:
            status = "canceled" if cancel_event.is_set() else "failed"
            message = str(exc)
        finally:
            hb_stop.set()
            hb_thread.join(timeout=3)
            flush_logs(drain=True)

        attempt = 0
        while not self.stop_event.is_set():
            attempt += 1
            try:
                self._post(
                    "/v1/runner/complete",
                    {
                        "buildId": build_id,
                        "status": status,
                        "result": result,
                        "message": message,
                    },
                    timeout=20,
                )
                return
            except Exception as exc:
                delay = min(15.0, 0.5 * (2 ** min(attempt - 1, 5)))
                print(
                    f"[luma-runner] retrying completion report for {build_id} in {delay:.1f}s: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
