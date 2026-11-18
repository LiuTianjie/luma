"""Stateless, pluggable BuildKit runner for Linux containers, Linux hosts, and macOS (OrbStack).

Key properties:
- Zero stored business secrets: Git tokens and Registry push credentials arrive
  in memory on lease and live only inside a mode-0700 tempdir for that single build.
- Two-tier BuildKit cache:
  1) Persistent local BuildKit daemon cache with automatic LRU ``gcpolicy`` cap.
  2) Remote OCI Registry cache (``--cache-from`` / ``--cache-to``) consolidated
     into a single ``luma-buildcache`` repository (Aliyun ACR quota friendly).
- Dynamic HTTP vs HTTPS registry config: never forces ``http=true`` onto TLS
  registries like Aliyun ACR, while still supporting Tailscale HTTP registries.
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
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping

import yaml

from . import __version__
from .deployer import _NoRedirectHandler, extract_manifest_target_hints
from .errors import BuildCanceled, BuildError
from .registry_naming import (
    DEFAULT_BASE_NO_PROXY,
    derive_cache_variant,
    format_cache_reference,
    format_image_repository,
    merge_no_proxy,
    normalize_registry_host,
    normalize_repo_url,
    registry_no_proxy_entries,
    uses_inline_registry_cache,
    validate_git_ref,
)
from .scheduler import normalize_platform, parse_platforms, required_native_arm_platform

ORBSTACK_XBIN = "/Applications/OrbStack.app/Contents/MacOS/xbin"
DOCKER_APP_BIN = "/Applications/Docker.app/Contents/Resources/bin"
_BUILDX_COND = threading.Condition()
_ACTIVE_BUILDER_JOBS: dict[str, int] = {}


class PlatformRequeueRequired(BuildError):
    """Raised when the cloned repository manifest targets a different native runner platform."""

    def __init__(self, platforms: list[str]) -> None:
        self.platforms = platforms
        super().__init__(f"manifest requires native {', '.join(platforms)} runner")


def _url_bypasses_proxy(url: str, no_proxy: str) -> bool:
    if not url or not no_proxy:
        return False
    parsed = urllib.parse.urlsplit(url if "://" in url else "ssh://" + url.replace(":", "/", 1))
    host = (parsed.hostname or "").strip("[]").lower()
    if not host:
        return False
    host_ip = None
    with contextlib.suppress(ValueError):
        host_ip = ipaddress.ip_address(host)
    for raw_item in no_proxy.split(","):
        entry = raw_item.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if "/" in entry and host_ip is not None:
            with contextlib.suppress(ValueError):
                net = ipaddress.ip_network(entry, strict=False)
                if host_ip.version == net.version and host_ip in net:
                    return True
            continue
        entry_host = entry.rsplit(":", 1)[0] if (":" in entry and not entry.startswith("[")) else entry.strip("[]")
        if host == entry_host or (entry_host.startswith(".") and host.endswith(entry_host)) or host.endswith("." + entry_host):
            return True
    return False


def find_docker_binary() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", f"{ORBSTACK_XBIN}/docker", f"{DOCKER_APP_BIN}/docker"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise BuildError("docker CLI not found on runner host")


def extra_cli_plugin_dirs() -> list[str]:
    dirs: list[str] = []
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


def _collect_context_tree(
    src_root: Path, rel_prefix: str, out: dict[str, bytes], *, max_bytes: int = 1024 * 1024
) -> None:
    if not src_root.is_dir():
        return
    used = sum(len(v) for v in out.values())
    with contextlib.suppress(OSError):
        for p in sorted(src_root.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            data = p.read_bytes()
            if used + len(data) > max_bytes:
                break
            used += len(data)
            rel = p.relative_to(src_root).as_posix()
            out[f"{rel_prefix.strip('/')}/{rel}"] = data


def _snapshot_docker_config_context(base_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env_src = dict(os.environ if base_env is None else base_env)
    explicit_host = str(env_src.get("DOCKER_HOST") or "").strip()
    explicit_ctx = str(env_src.get("DOCKER_CONTEXT") or "").strip()
    tls_verify = str(env_src.get("DOCKER_TLS_VERIFY") or "").strip()
    cert_path = str(env_src.get("DOCKER_CERT_PATH") or "").strip()
    orig_cfg_dir = Path(env_src.get("DOCKER_CONFIG") or (Path.home() / ".docker")).expanduser()

    if explicit_host and not explicit_ctx:
        return {
            "dockerHost": explicit_host,
            "resolvedEndpoint": explicit_host,
            "tlsVerify": tls_verify,
            "certPath": cert_path,
            "currentContext": "",
            "contextFiles": {},
        }

    ctx_name = explicit_ctx
    if not ctx_name:
        cfg_json_path = orig_cfg_dir / "config.json"
        if cfg_json_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                loaded = json.loads(cfg_json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    ctx_name = str(loaded.get("currentContext") or "").strip()

    if ctx_name and ctx_name != "default":
        context_files: dict[str, bytes] = {}
        _collect_context_tree(orig_cfg_dir / "contexts", "contexts", context_files)
        return {
            "dockerHost": "",
            "resolvedEndpoint": "",
            "tlsVerify": "",
            "certPath": "",
            "currentContext": ctx_name,
            "contextFiles": context_files,
        }

    return {
        "dockerHost": explicit_host,
        "resolvedEndpoint": explicit_host,
        "tlsVerify": tls_verify,
        "certPath": cert_path,
        "currentContext": "",
        "contextFiles": {},
    }


def capture_docker_runtime(
    docker_bin: str, *, base_env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Resolve and snapshot the active Docker context/endpoint at runner startup."""
    snap = _snapshot_docker_config_context(base_env=base_env)
    if snap["dockerHost"]:
        return snap
    env_src = dict(os.environ if base_env is None else base_env)
    cmd = [docker_bin, "context", "inspect"]
    if snap["currentContext"]:
        cmd.append(snap["currentContext"])
    proc = subprocess.run(
        cmd,
        env=env_src,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if proc.returncode == 0:
        with contextlib.suppress(ValueError, TypeError, KeyError):
            items = json.loads(proc.stdout or "")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                info = items[0]
                inspected_name = str(info.get("Name") or snap["currentContext"] or "default").strip()
                ep_docker = (info.get("Endpoints") or {}).get("docker") or {}
                ep_host = str(ep_docker.get("Host") or "").strip()
                storage = info.get("Storage") if isinstance(info.get("Storage"), dict) else {}
                ctx_files: dict[str, bytes] = {}
                meta_path = str(storage.get("MetadataPath") or "").strip()
                tls_path = str(storage.get("TLSPath") or "").strip()
                if meta_path:
                    mp = Path(meta_path)
                    _collect_context_tree(mp, f"contexts/meta/{mp.name}", ctx_files)
                if tls_path:
                    tp = Path(tls_path)
                    _collect_context_tree(tp, f"contexts/tls/{tp.name}", ctx_files)
                if not ctx_files:
                    ctx_files = dict(snap["contextFiles"])
                if inspected_name == "default":
                    return {
                        "dockerHost": ep_host,
                        "resolvedEndpoint": ep_host,
                        "tlsVerify": snap["tlsVerify"],
                        "certPath": snap["certPath"],
                        "currentContext": "",
                        "contextFiles": {},
                    }
                return {
                    "dockerHost": "",
                    "resolvedEndpoint": ep_host,
                    "tlsVerify": "",
                    "certPath": "",
                    "currentContext": inspected_name,
                    "contextFiles": ctx_files,
                }
    return snap


def _apply_docker_runtime_env(
    env: dict[str, str],
    *,
    docker_config: Path | None = None,
    buildx_state_dir: Path | None = None,
    docker_runtime: Mapping[str, Any] | None = None,
    force_no_attestations: bool = False,
) -> dict[str, str]:
    if docker_config is not None:
        env["DOCKER_CONFIG"] = str(docker_config)
    if buildx_state_dir is not None:
        env["BUILDX_CONFIG"] = str(buildx_state_dir)
    if force_no_attestations:
        env["BUILDX_NO_DEFAULT_ATTESTATIONS"] = "1"
    else:
        env.setdefault("BUILDX_NO_DEFAULT_ATTESTATIONS", "1")
    if docker_runtime is not None:
        ctx_name = str(docker_runtime.get("currentContext") or "").strip()
        ctx_files = (
            docker_runtime.get("contextFiles")
            if isinstance(docker_runtime.get("contextFiles"), Mapping)
            else {}
        )
        d_host = str(docker_runtime.get("dockerHost") or "").strip()
        res_ep = str(docker_runtime.get("resolvedEndpoint") or "").strip()
        if ctx_name and (ctx_files or docker_config is None):
            env["DOCKER_CONTEXT"] = ctx_name
            env.pop("DOCKER_HOST", None)
        elif d_host or res_ep:
            env["DOCKER_HOST"] = d_host or res_ep
            env.pop("DOCKER_CONTEXT", None)
            if docker_runtime.get("tlsVerify"):
                env["DOCKER_TLS_VERIFY"] = str(docker_runtime["tlsVerify"])
            if docker_runtime.get("certPath"):
                env["DOCKER_CERT_PATH"] = str(docker_runtime["certPath"])
    return env


def detect_native_engine_platform(
    docker_bin: str, *, docker_runtime: Mapping[str, Any] | None = None
) -> str:
    run_kwargs: dict[str, Any] = {}
    if docker_runtime is not None:
        run_kwargs["env"] = _apply_docker_runtime_env(dict(os.environ), docker_runtime=docker_runtime)
    proc = subprocess.run(
        [docker_bin, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=15,
        **run_kwargs,
    )
    if proc.returncode != 0:
        raise BuildError(f"unable to query Docker daemon on runner: {(proc.stdout or '').strip()}")
    return normalize_platform(proc.stdout.strip())


def _safe_subpath(root: Path, rel: str) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / (rel or ".")).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise BuildError(f"path escapes repository root: {rel}")
    return target


def _discover_manifest(
    repo_root: Path, compose_sidecar: str = "", manifest_path: str = ""
) -> tuple[str, Path] | None:
    if compose_sidecar:
        target = _safe_subpath(repo_root, compose_sidecar)
        if not target.is_file():
            raise BuildError(f"composeSidecar not found in repository: {compose_sidecar}")
        return "compose", target
    if manifest_path:
        target = _safe_subpath(repo_root, manifest_path)
        if not target.is_file():
            raise BuildError(f"manifestPath not found in repository: {manifest_path}")
        return "service", target

    service_names = (".luma.yml", ".luma.yaml", "luma.yml", "luma.yaml")
    compose_names = (
        "luma.compose.yml",
        "luma.compose.yaml",
        ".luma.compose.yml",
        ".luma.compose.yaml",
        "docker-compose.luma.yml",
        "docker-compose.luma.yaml",
    )
    for name in compose_names:
        candidate = repo_root / name
        if candidate.is_file():
            return "compose", candidate
    for name in service_names:
        candidate = repo_root / name
        if candidate.is_file():
            return "service", candidate
    return None


def _write_docker_config(
    config_dir: Path,
    registry_auths: list[dict[str, str]],
    *,
    docker_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rt = dict(docker_runtime) if docker_runtime is not None else _snapshot_docker_config_context()
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.chmod(0o700)
    auths_obj: dict[str, Any] = {}
    for entry in registry_auths:
        host = str(entry.get("host") or "").strip()
        user = str(entry.get("username") or "")
        pw = str(entry.get("password") or "")
        if host and user and pw:
            token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
            auths_obj[host] = {"auth": token}
    payload: dict[str, Any] = {}
    if auths_obj:
        payload["auths"] = auths_obj
    plugin_dirs = extra_cli_plugin_dirs()
    if plugin_dirs:
        payload["cliPluginsExtraDirs"] = plugin_dirs
    ctx_name = str(rt.get("currentContext") or "").strip()
    ctx_files = rt.get("contextFiles") if isinstance(rt.get("contextFiles"), Mapping) else {}
    if ctx_name and ctx_files:
        payload["currentContext"] = ctx_name
        cfg_root = config_dir.resolve()
        for rel_path, raw_bytes in ctx_files.items():
            clean_parts = [p for p in PurePosixPath(str(rel_path)).parts if p not in {"", ".", "..", "/"}]
            if not clean_parts or clean_parts[0] != "contexts":
                continue
            dest_file = cfg_root.joinpath(*clean_parts)
            dest_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest_file.write_bytes(bytes(raw_bytes))
            dest_file.chmod(0o600)
    cfg_file = config_dir / "config.json"
    cfg_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    cfg_file.chmod(0o600)
    return rt


def _clone_repo(
    *,
    repo_url: str,
    ref: str,
    dest: Path,
    git_auth: dict[str, str] | None,
    proxy: str = "",
    no_proxy: str = "",
) -> tuple[str, str]:
    safe_url = normalize_repo_url(repo_url)
    safe_ref = validate_git_ref(ref)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if proxy and not _url_bypasses_proxy(safe_url, no_proxy):
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        if no_proxy:
            env["NO_PROXY"] = no_proxy
            env["no_proxy"] = no_proxy
    elif no_proxy and _url_bypasses_proxy(safe_url, no_proxy):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy

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

        is_commit = bool(re.fullmatch(r"[0-9a-fA-F]{40}", safe_ref))
        if is_commit:
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
                raise BuildError(f"git fetch failed ({cmd[1]}): {out}")

    short_sha = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "--short=12", "HEAD"], text=True, timeout=15).strip()
    full_sha = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True, timeout=15).strip()
    return short_sha, full_sha


_GIT_CONTEXT_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_GIT_CONTEXT_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GIT_CONTEXT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_CONTEXT_RESERVED = {"context", "dockerfile", "scratch"}
_MAX_GIT_CONTEXTS = 16
_MAX_LOCK_BYTES = 64 * 1024
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 4 * _MAX_ARCHIVE_BYTES
_MAX_ARCHIVE_MEMBERS = 100_000


def _https_origin(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port if parsed.port is not None else 443
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
        or "\\" in url
        or not parsed.path.strip("/")
        or not (0 < port <= 65535)
    ):
        raise BuildError("x-luma-git-contexts requires HTTPS repository URLs without credentials or query")
    return parsed.hostname.lower(), port


def _safe_archive_relpath(raw_name: str) -> PurePosixPath:
    if not raw_name or "\\" in raw_name or any(ord(c) < 32 or ord(c) == 127 for c in raw_name):
        raise BuildError("Git context archive contains an invalid path")
    p = PurePosixPath(raw_name)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in raw_name.split("/")):
        raise BuildError("Git context archive path escapes root")
    if any(part.casefold() == ".git" for part in p.parts):
        raise BuildError("Git context archive must not contain .git metadata")
    return p


def _extract_verified_tar_gz(archive: Path, destination: Path, cancel_event: threading.Event) -> None:
    try:
        with tarfile.open(archive, "r:gz") as source:
            members: dict[PurePosixPath, tarfile.TarInfo] = {}
            total_size = 0
            for member in source:
                if cancel_event.is_set():
                    raise BuildCanceled("build canceled")
                name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
                rel = _safe_archive_relpath(name)
                if rel in members:
                    raise BuildError("Git context archive contains duplicate path")
                if not (member.isdir() or member.isreg() or member.issym()) or member.sparse or member.size < 0:
                    raise BuildError("Git context archive contains unsupported entry")
                total_size += member.size
                if len(members) >= _MAX_ARCHIVE_MEMBERS or total_size > _MAX_EXTRACTED_BYTES:
                    raise BuildError("Git context archive exceeds extraction limit")
                if member.issym() and (
                    not member.linkname
                    or PurePosixPath(member.linkname).is_absolute()
                    or "\\" in member.linkname
                    or any(ord(c) < 32 for c in member.linkname)
                ):
                    raise BuildError("Git context archive contains invalid symlink")
                members[rel] = member
            for rel in members:
                for parent in rel.parents:
                    anc = members.get(parent)
                    if anc is not None and not anc.isdir():
                        raise BuildError("Git context archive writes through non-directory")
            destination.mkdir(mode=0o755)
            for rel, member in members.items():
                if cancel_event.is_set():
                    raise BuildCanceled("build canceled")
                target = destination.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                elif member.isreg():
                    reader = source.extractfile(member)
                    if reader is None:
                        raise BuildError("unable to read regular archive member")
                    with reader, target.open("xb") as writer:
                        while True:
                            if cancel_event.is_set():
                                raise BuildCanceled("build canceled")
                            chunk = reader.read(1024 * 1024)
                            if not chunk:
                                break
                            writer.write(chunk)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
            for rel, member in members.items():
                if member.issym():
                    destination.joinpath(*rel.parts).symlink_to(member.linkname)
            dest_root = destination.resolve(strict=True)
            for rel, member in members.items():
                if member.issym():
                    target = destination.joinpath(*rel.parts)
                    if not target.resolve(strict=False).is_relative_to(dest_root):
                        raise BuildError("Git context symlink escapes root")
                    with contextlib.suppress(FileNotFoundError):
                        target.stat()
    except (BuildError, BuildCanceled):
        raise
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        raise BuildError("Git context archive cannot be safely extracted") from exc


def _prepare_git_contexts(
    src_dir: Path,
    build_spec: Mapping[str, Any],
    *,
    primary_repo_url: str,
    workspace: Path,
    git_auth: dict[str, str] | None,
    proxy: str,
    no_proxy: str = "",
    on_line: Callable[[str], None] | None,
    cancel_event: threading.Event,
) -> dict[str, Path]:
    if "x-luma-git-contexts" not in build_spec:
        return {}
    raw_contexts = build_spec["x-luma-git-contexts"]
    if not isinstance(raw_contexts, Mapping) or not raw_contexts or len(raw_contexts) > _MAX_GIT_CONTEXTS:
        raise BuildError(f"build.x-luma-git-contexts must declare 1..{_MAX_GIT_CONTEXTS} named contexts")
    primary_origin = _https_origin(primary_repo_url)
    out_root = Path(tempfile.mkdtemp(prefix="luma-git-ctx-", dir=workspace))
    prepared: dict[str, Path] = {}
    for alias, item in raw_contexts.items():
        if cancel_event.is_set():
            raise BuildCanceled("build canceled")
        if not isinstance(alias, str) or not _GIT_CONTEXT_ALIAS_RE.fullmatch(alias) or alias in _GIT_CONTEXT_RESERVED:
            raise BuildError(f"invalid or reserved x-luma-git-contexts alias: {alias!r}")
        if not isinstance(item, Mapping) or set(item) != {"lock"} or not isinstance(item["lock"], str):
            raise BuildError(f"Git context {alias} must declare only a lock path")
        lock_path = _safe_subpath(src_dir, item["lock"])
        if not lock_path.is_file() or lock_path.stat().st_size > _MAX_LOCK_BYTES:
            raise BuildError(f"Git context {alias} lock file missing or too large: {item['lock']}")
        try:
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise BuildError(f"Git context {alias} lock is invalid JSON") from exc
        if not isinstance(lock_data, dict) or set(lock_data) != {"repository", "commit", "archive_sha256"}:
            raise BuildError(f"Git context {alias} lock must contain repository, commit, and archive_sha256")
        ctx_repo = str(lock_data["repository"])
        ctx_commit = str(lock_data["commit"])
        ctx_sha256 = str(lock_data["archive_sha256"])
        if _https_origin(ctx_repo) != primary_origin:
            raise BuildError(f"Git context {alias} must share the primary repository HTTPS origin")
        ctx_git_auth = git_auth
        if ctx_git_auth and ctx_git_auth.get("pathPrefix"):
            allowed_prefix = str(ctx_git_auth["pathPrefix"]).strip("/").lower()
            if allowed_prefix.endswith(".git"):
                allowed_prefix = allowed_prefix[:-4].rstrip("/")
            ctx_path = urllib.parse.urlsplit(ctx_repo).path.strip("/").lower()
            if ctx_path.endswith(".git"):
                ctx_path = ctx_path[:-4].rstrip("/")
            if allowed_prefix and not (ctx_path == allowed_prefix or ctx_path.startswith(allowed_prefix + "/")):
                ctx_git_auth = None
        if not _GIT_CONTEXT_COMMIT_RE.fullmatch(ctx_commit) or not _GIT_CONTEXT_SHA256_RE.fullmatch(ctx_sha256):
            raise BuildError(f"Git context {alias} lock requires lowercase commit and archive_sha256")
        if on_line:
            on_line(f"Preparing pinned Git context {alias} ({ctx_commit[:12]})...")
        with tempfile.TemporaryDirectory(prefix="luma-ctx-fetch-", dir=workspace) as fetch_raw:
            fetch_dir = Path(fetch_raw)
            cloned = fetch_dir / "repo"
            archive = fetch_dir / "source.tar.gz"
            _, got_full = _clone_repo(
                repo_url=ctx_repo,
                ref=ctx_commit,
                dest=cloned,
                git_auth=ctx_git_auth,
                proxy=proxy,
                **({"no_proxy": no_proxy} if (proxy and no_proxy) else {}),
            )
            if got_full != ctx_commit:
                raise BuildError(f"Git context {alias} checkout commit mismatch")
            with archive.open("xb") as stream:
                ar_res = subprocess.run(
                    ["git", "-C", str(cloned), "archive", "--format=tar.gz", ctx_commit],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=180,
                )
            if ar_res.returncode != 0 or archive.stat().st_size > _MAX_ARCHIVE_BYTES:
                raise BuildError(f"failed to archive pinned Git context {alias}")
            digest = hashlib.sha256()
            with archive.open("rb") as stream:
                while True:
                    if cancel_event.is_set():
                        raise BuildCanceled("build canceled")
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != ctx_sha256:
                raise BuildError(f"Git context {alias} archive SHA256 does not match reviewed lock")
            extracted = out_root / alias
            _extract_verified_tar_gz(archive, extracted, cancel_event)
            prepared[alias] = extracted
    return prepared


def _render_buildkitd_toml(
    *,
    http_hosts: list[str],
    cache_keep_gb: int = 20,
    max_parallelism: int = 0,
) -> str:
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
    for host in sorted(set(h for h in http_hosts if h)):
        lines.extend(
            [
                "",
                f'[registry."{host}"]',
                "  http = true",
                "  insecure = true",
            ]
        )
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def _use_runner_buildx(
    *,
    docker_bin: str,
    buildx_state_dir: Path,
    docker_config: Path,
    proxy: str,
    no_proxy: str,
    http_hosts: list[str],
    cache_keep_gb: int,
    max_parallelism: int,
    docker_runtime: Mapping[str, Any] | None = None,
) -> Iterator[str]:
    with _BUILDX_COND:
        builder_name = _ensure_runner_buildx(
            docker_bin=docker_bin,
            buildx_state_dir=buildx_state_dir,
            docker_config=docker_config,
            proxy=proxy,
            no_proxy=no_proxy,
            http_hosts=http_hosts,
            cache_keep_gb=cache_keep_gb,
            max_parallelism=max_parallelism,
            docker_runtime=docker_runtime,
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


def _ensure_runner_buildx(
    *,
    docker_bin: str,
    buildx_state_dir: Path,
    docker_config: Path,
    proxy: str,
    no_proxy: str,
    http_hosts: list[str],
    cache_keep_gb: int,
    max_parallelism: int,
    docker_runtime: Mapping[str, Any] | None = None,
) -> str:
    with _BUILDX_COND:
        return _ensure_runner_buildx_locked(
            docker_bin=docker_bin,
            buildx_state_dir=buildx_state_dir,
            docker_config=docker_config,
            proxy=proxy,
            no_proxy=no_proxy,
            http_hosts=http_hosts,
            cache_keep_gb=cache_keep_gb,
            max_parallelism=max_parallelism,
            docker_runtime=docker_runtime,
        )


def _ensure_runner_buildx_locked(
    *,
    docker_bin: str,
    buildx_state_dir: Path,
    docker_config: Path,
    proxy: str,
    no_proxy: str,
    http_hosts: list[str],
    cache_keep_gb: int,
    max_parallelism: int,
    docker_runtime: Mapping[str, Any] | None = None,
) -> str:
    name = "luma-runner-egress" if proxy else "luma-runner"
    buildx_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = buildx_state_dir / f".{name}.spec.json"
    prev_spec: dict[str, Any] = {}
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
    desired_spec: dict[str, Any] = {
        "toml": toml_body,
        "proxy": proxy,
        "noProxy": merged_no_proxy,
        "httpHosts": merged_http_hosts,
    }
    if docker_runtime is not None:
        ep_key = str(
            docker_runtime.get("resolvedEndpoint")
            or docker_runtime.get("dockerHost")
            or docker_runtime.get("currentContext")
            or ""
        ).strip()
        if ep_key:
            desired_spec["endpoint"] = ep_key
    fingerprint = hashlib.sha256(json.dumps(desired_spec, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    env = _apply_docker_runtime_env(
        dict(os.environ),
        docker_config=docker_config,
        buildx_state_dir=buildx_state_dir,
        docker_runtime=docker_runtime,
    )

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

    # Wait for any concurrent local worker using this builder to finish before reconfiguring
    while _ACTIVE_BUILDER_JOBS.get(name, 0) > 0:
        _BUILDX_COND.wait(timeout=1.0)

    if inspect_res.returncode == 0:
        # Preserve the persistent BuildKit SSD cache volume (--keep-state) across config updates
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
        raise BuildError(f"failed to initialize BuildKit builder {name}:\n{(created.stdout or '').strip()}")
    marker.write_text(
        json.dumps({**desired_spec, "fingerprint": fingerprint}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return name


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
    platforms: list[str],
    push_insecure: bool,
    cache_mode: str,
    cache_ref: str,
    no_cache: bool,
    proxy: str,
    no_proxy: str,
    build_args: dict[str, str],
    target: str,
    build_contexts: Mapping[str, Path] | None = None,
    docker_runtime: Mapping[str, Any] | None = None,
    timeout: int,
    metadata_file: Path,
    on_line: Callable[[str], None] | None,
    cancel_event: threading.Event,
) -> tuple[str, str]:
    push_tag = f"{push_host}/{image_repo}:{tag}"
    platform_arg = ",".join(platforms)
    inline_cache = (not push_insecure) and uses_inline_registry_cache(push_host)
    output_spec = (
        "type=image,push=true"
        + (",oci-mediatypes=false" if inline_cache else "")
        + (",registry.insecure=true" if push_insecure else "")
    )

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
            raise BuildError(f"invalid Dockerfile target stage: {target!r}")
        cmd.append(f"--target={target}")
    if no_cache:
        cmd.append("--no-cache")
    elif cache_ref and cache_mode in {"read-write", "read-only"}:
        insecure_suffix = ",registry.insecure=true" if push_insecure else ""
        if inline_cache:
            cache_suffix = cache_ref.rsplit(":", 1)[1] if ":" in cache_ref else "latest"
            inline_ref = f"{push_host}/{image_repo}:cache-{cache_suffix[-96:]}"
            cmd.extend(["--cache-from", f"type=registry,ref={inline_ref}{insecure_suffix}"])
            if cache_mode == "read-write":
                cmd.extend(["-t", inline_ref, "--cache-to", "type=inline"])
        else:
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
    for ctx_alias, ctx_path in sorted((build_contexts or {}).items()):
        if not _GIT_CONTEXT_ALIAS_RE.fullmatch(str(ctx_alias)):
            raise BuildError(f"invalid build context alias: {ctx_alias!r}")
        cmd.extend(["--build-context", f"{ctx_alias}={ctx_path}"])
    for k, v in sorted(build_args.items()):
        key_clean = str(k).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_clean):
            raise BuildError(f"invalid build arg name: {k!r}")
        cmd.append(f"--build-arg={key_clean}={v}")
    cmd.extend(["--", str(context_dir)])

    env = _apply_docker_runtime_env(
        dict(os.environ),
        docker_config=docker_config,
        buildx_state_dir=buildx_state_dir,
        docker_runtime=docker_runtime,
        force_no_attestations=inline_cache,
    )
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
    tail_lines: list[str] = []
    line_queue: queue.Queue[str | None] = queue.Queue()

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
                raise BuildCanceled("build canceled")
            if time.monotonic() > deadline:
                _terminate_process_group(proc)
                raise BuildError(f"docker buildx build timed out after {timeout}s")
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
        raise BuildCanceled("build canceled")
    if rc != 0:
        raise BuildError("docker buildx build failed:\n" + "\n".join(tail_lines[-40:]))

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


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
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


def _resolve_post_clone_platforms(
    task: Mapping[str, Any],
    *,
    manifest_text: str,
    compose_data: Mapping[str, Any] | None = None,
    fallback: list[str],
) -> list[str]:
    if task.get("platformExplicit"):
        return fallback

    hints = extract_manifest_target_hints(manifest_text)
    explicit: list[str] = list(hints["platforms"])
    if isinstance(compose_data, Mapping) and isinstance(compose_data.get("services"), Mapping):
        for svc in compose_data["services"].values():
            if not isinstance(svc, Mapping):
                continue
            raw_svc_plat = svc.get("platform")
            if raw_svc_plat:
                for p in parse_platforms(raw_svc_plat):
                    if p not in explicit:
                        explicit.append(p)
            raw_b = svc.get("build")
            if isinstance(raw_b, Mapping):
                b_plat = raw_b.get("platforms") or raw_b.get("platform")
                if b_plat:
                    for p in parse_platforms(b_plat):
                        if p not in explicit:
                            explicit.append(p)
    if explicit:
        return explicit

    node_map = task.get("nodePlatforms") if isinstance(task.get("nodePlatforms"), Mapping) else {}
    region_map = task.get("regionPlatforms") if isinstance(task.get("regionPlatforms"), Mapping) else {}
    req_node = str(task.get("node") or "").strip()
    req_region = str(task.get("region") or "").strip()
    target_nodes = [req_node] if req_node else hints["nodes"]
    target_regions = [] if target_nodes else ([req_region] if req_region else hints["regions"])
    inferred: list[str] = []
    for n in target_nodes:
        plat = str(node_map.get(n.strip().lower()) or "")
        if plat and plat not in inferred:
            inferred.append(plat)
    for r in target_regions:
        for plat in region_map.get(r.strip().lower()) or []:
            if plat and plat not in inferred:
                inferred.append(plat)
    return inferred or fallback


def _check_runner_platform_compatibility(
    platforms: list[str],
    *,
    native_platform: str,
    task: Mapping[str, Any],
) -> None:
    arm_needed = required_native_arm_platform(platforms)
    compatible = (
        (native_platform == arm_needed)
        if arm_needed
        else (len(platforms) != 1 or native_platform == platforms[0])
    )
    if compatible:
        return
    if task.get("pinnedRunner") or int(task.get("requeueCount") or 0) >= 1:
        if arm_needed:
            raise BuildError(
                f"ARM build requires native {arm_needed} engine, but runner has {native_platform}; "
                "ARM QEMU emulation is disabled"
            )
        raise BuildError(
            f"build requires native {platforms[0]} engine, but runner has {native_platform}"
        )
    raise PlatformRequeueRequired(platforms)


def execute_build_task(
    task: dict[str, Any],
    *,
    docker_bin: str,
    native_platform: str,
    buildx_state_dir: Path,
    local_proxy: str = "",
    local_no_proxy: str = "",
    cache_keep_gb: int = 20,
    max_parallelism: int = 0,
    docker_runtime: Mapping[str, Any] | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
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
        raise BuildError(
            f"ARM build requires native {arm_needed} engine, but runner has {native_platform}; "
            "ARM QEMU emulation is disabled"
        )

    http_hosts: list[str] = []
    if push_insecure:
        http_hosts.append(push_host)
    if pull_insecure:
        http_hosts.append(pull_host)
    no_proxy = merge_no_proxy(
        DEFAULT_BASE_NO_PROXY,
        os.environ.get("NO_PROXY", ""),
        os.environ.get("no_proxy", ""),
        local_no_proxy,
        *registry_no_proxy_entries(push_host),
        *registry_no_proxy_entries(pull_host),
    )

    with tempfile.TemporaryDirectory(prefix="luma-build-job-") as tmp_raw:
        tmp = Path(tmp_raw)
        tmp.chmod(0o700)
        src_dir = tmp / "src"
        docker_cfg = tmp / "docker-config"
        effective_runtime = _write_docker_config(
            docker_cfg, list(task.get("registryAuths") or []), docker_runtime=docker_runtime
        )

        if on_line:
            on_line(f"Cloning {repo_url} ({ref})...")
        short_sha, full_sha = _clone_repo(
            repo_url=repo_url,
            ref=ref,
            dest=src_dir,
            git_auth=task.get("gitAuth"),
            proxy=local_proxy,
            **({"no_proxy": no_proxy} if (local_proxy or local_no_proxy) else {}),
        )
        if cancel.is_set():
            raise BuildCanceled("build canceled")

        discovered = _discover_manifest(
            src_dir,
            compose_sidecar=str(task.get("composeSidecar") or ""),
            manifest_path=str(task.get("manifestPath") or ""),
        )
        pre_compose_data: dict[str, Any] | None = None
        if discovered and discovered[0] == "compose":
            manifest_text = discovered[1].read_text(encoding="utf-8")
            sidecar_preview = yaml.safe_load(manifest_text) or {}
            if isinstance(sidecar_preview, dict):
                c_rel = str(sidecar_preview.get("compose") or "docker-compose.yml")
                c_path = (discovered[1].parent / c_rel).resolve()
                if (src_dir.resolve() in c_path.parents or c_path == src_dir.resolve()) and c_path.is_file():
                    loaded_compose = yaml.safe_load(c_path.read_text(encoding="utf-8")) or {}
                    if isinstance(loaded_compose, dict):
                        pre_compose_data = loaded_compose
        else:
            manifest_text = discovered[1].read_text(encoding="utf-8") if discovered else str(task.get("manifest") or "")

        platforms = _resolve_post_clone_platforms(
            task,
            manifest_text=manifest_text,
            compose_data=pre_compose_data,
            fallback=platforms,
        )
        _check_runner_platform_compatibility(
            platforms,
            native_platform=native_platform,
            task=task,
        )

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
            docker_runtime=effective_runtime,
        ) as builder_name:
            if discovered and discovered[0] == "compose":
                return _build_compose_project(
                    src_dir=src_dir,
                    sidecar_path=discovered[1],
                    task=task,
                    docker_bin=docker_bin,
                    builder_name=builder_name,
                    buildx_state_dir=buildx_state_dir,
                    docker_cfg=docker_cfg,
                    docker_runtime=effective_runtime,
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
                raise BuildError(f"Dockerfile not found: {df_rel}")

            image_repo = format_image_repository(
                repo_slug=repo_slug,
                single_service=True,
                namespace=namespace,
                flatten=flatten,
            )
            cache_variant = derive_cache_variant(
                manifest_path=str(task.get("manifestPath") or ""),
                context=ctx_rel,
                dockerfile=df_rel,
                target=target,
                single_service=True,
            )
            cache_ref = format_cache_reference(
                push_host=push_host,
                repo_slug=repo_slug,
                service_name=cache_variant,
                platform=platforms[0],
                namespace=namespace,
            )
            extra_contexts = _prepare_git_contexts(
                src_dir,
                build_block,
                primary_repo_url=repo_url,
                workspace=tmp,
                git_auth=task.get("gitAuth"),
                proxy=local_proxy,
                no_proxy=no_proxy,
                on_line=on_line,
                cancel_event=cancel,
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
                build_contexts=extra_contexts,
                docker_runtime=effective_runtime,
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


def _build_compose_project(
    *,
    src_dir: Path,
    sidecar_path: Path,
    task: dict[str, Any],
    docker_bin: str,
    builder_name: str,
    buildx_state_dir: Path,
    docker_cfg: Path,
    docker_runtime: Mapping[str, Any] | None = None,
    push_host: str,
    pull_host: str,
    repo_slug: str,
    namespace: str,
    flatten: bool | None,
    tag: str,
    short_sha: str,
    full_sha: str,
    platforms: list[str],
    push_insecure: bool,
    cache_mode: str,
    no_cache: bool,
    proxy: str,
    no_proxy: str,
    timeout: int,
    tmp: Path,
    on_line: Callable[[str], None] | None,
    cancel: threading.Event,
) -> dict[str, Any]:
    sidecar_text = sidecar_path.read_text(encoding="utf-8")
    sidecar = yaml.safe_load(sidecar_text) or {}
    if not isinstance(sidecar, dict):
        raise BuildError("luma.compose.yml must be a YAML mapping")
    compose_rel = str(sidecar.get("compose") or "docker-compose.yml")
    compose_path = (sidecar_path.parent / compose_rel).resolve()
    if src_dir.resolve() not in compose_path.parents and compose_path != src_dir.resolve():
        raise BuildError(f"compose file escapes repository: {compose_rel}")
    if not compose_path.is_file():
        raise BuildError(f"compose file not found: {compose_rel}")

    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    services = compose_data.get("services") if isinstance(compose_data, dict) else None
    if not isinstance(services, dict) or not services:
        raise BuildError("docker-compose.yml must define a non-empty services mapping")

    build_targets: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    declared_aliases: dict[str, str] = {}
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
    built_images: dict[str, str] = {}
    alias_replacements: dict[str, str] = {}
    ambiguous_aliases: set[str] = set()

    for idx, (svc_name, svc_body, spec) in enumerate(build_targets):
        ctx_dir = (compose_path.parent / str(spec.get("context") or ".")).resolve()
        if src_dir.resolve() not in ctx_dir.parents and ctx_dir != src_dir.resolve():
            raise BuildError(f"service {svc_name} build context escapes repository")
        df_rel = str(spec.get("dockerfile") or "Dockerfile")
        df_path = (ctx_dir / df_rel).resolve()
        if not df_path.is_file():
            raise BuildError(f"Dockerfile not found for compose service {svc_name}: {df_rel}")

        raw_args = spec.get("args")
        svc_args: dict[str, str] = {}
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
        cache_variant = derive_cache_variant(
            compose_sidecar=str(task.get("composeSidecar") or ""),
            service_name=svc_name,
            single_service=single_build,
        )
        cache_ref = format_cache_reference(
            push_host=push_host,
            repo_slug=repo_slug,
            service_name=cache_variant,
            platform=platforms[0],
            namespace=namespace,
        )
        if on_line:
            on_line(f"Building compose service {svc_name} -> {push_host}/{image_repo}:{tag}")

        extra_contexts = _prepare_git_contexts(
            src_dir,
            spec,
            primary_repo_url=str(task["repoUrl"]),
            workspace=tmp,
            git_auth=task.get("gitAuth"),
            proxy=proxy,
            no_proxy=no_proxy,
            on_line=on_line,
            cancel_event=cancel,
        )
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
            build_contexts=extra_contexts,
            docker_runtime=docker_runtime,
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


class RunnerDaemon:
    """Reverse-polling runner daemon; zero inbound ports required."""

    def __init__(
        self,
        *,
        server_url: str,
        runner_token: str,
        state_dir: Path,
        local_proxy: str = "",
        local_no_proxy: str = "",
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
        self.local_no_proxy = local_no_proxy.strip()
        self.cache_mode = cache_mode.strip()
        self.cache_keep_gb = cache_keep_gb
        self.max_parallelism = max_parallelism
        self.workers = max(1, min(int(workers), 16))
        self.insecure_tls = insecure_tls
        self.poll_interval = max(poll_interval, 0.5)
        self.stop_event = threading.Event()
        self.docker_bin = find_docker_binary()
        self.docker_runtime = capture_docker_runtime(self.docker_bin)
        self.native_platform = detect_native_engine_platform(
            self.docker_bin, docker_runtime=self.docker_runtime
        )

    def _post(self, path: str, body: dict[str, Any], *, timeout: int = 35) -> dict[str, Any]:
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
        handlers: list[Any] = [_NoRedirectHandler()]
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
                    raise
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

    def _run_leased_task(self, task: dict[str, Any]) -> None:
        build_id = str(task["buildId"])
        lease_id = str(task.get("leaseId") or "")
        cancel_event = threading.Event()
        log_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        hb_stop = threading.Event()

        def flush_logs(*, drain: bool = False) -> None:
            while True:
                batch: list[dict[str, Any]] = []
                while not log_queue.empty() and len(batch) < 80:
                    try:
                        batch.append(log_queue.get_nowait())
                    except queue.Empty:
                        break
                if not batch:
                    break
                try:
                    res = self._post(
                        "/v1/runner/progress",
                        {"buildId": build_id, "leaseId": lease_id, "events": batch},
                        timeout=15,
                    )
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
                    hb = self._post(
                        "/v1/runner/heartbeat",
                        {"buildId": build_id, "leaseId": lease_id},
                        timeout=10,
                    )
                    if hb.get("cancelRequested"):
                        cancel_event.set()
                except Exception:
                    pass

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        hb_thread.start()
        status = "succeeded"
        message = ""
        result: dict[str, Any] = {}
        try:
            result = execute_build_task(
                task,
                docker_bin=self.docker_bin,
                native_platform=self.native_platform,
                buildx_state_dir=self.state_dir / "buildx",
                local_proxy=self.local_proxy,
                local_no_proxy=self.local_no_proxy,
                cache_keep_gb=self.cache_keep_gb,
                max_parallelism=self.max_parallelism,
                docker_runtime=self.docker_runtime,
                on_line=lambda line: log_queue.put({"kind": "log", "line": line, "ts": int(time.time())}),
                cancel_event=cancel_event,
            )
            message = f"Built and pushed {result.get('image') or ''}"
        except PlatformRequeueRequired as exc:
            status = "requeue"
            result = {"requiredPlatforms": exc.platforms}
            message = f"Manifest requires {','.join(exc.platforms)}; handing off from {self.native_platform} runner"
        except BuildCanceled as exc:
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
                        "leaseId": lease_id,
                        "status": status,
                        "result": result,
                        "message": message,
                    },
                    timeout=20,
                )
                return
            except urllib.error.HTTPError as exc:
                if exc.code in {400, 401, 403, 404, 409}:
                    detail = ""
                    with contextlib.suppress(Exception):
                        detail = exc.read().decode("utf-8", errors="replace").strip()
                    print(
                        f"[luma-runner] dropping rejected completion report for {build_id} (HTTP {exc.code}): {detail or exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                delay = min(15.0, 0.5 * (2 ** min(attempt - 1, 5)))
                print(
                    f"[luma-runner] retrying completion report for {build_id} in {delay:.1f}s: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                self.stop_event.wait(delay)
            except Exception as exc:
                delay = min(15.0, 0.5 * (2 ** min(attempt - 1, 5)))
                print(
                    f"[luma-runner] retrying completion report for {build_id} in {delay:.1f}s: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                self.stop_event.wait(delay)
