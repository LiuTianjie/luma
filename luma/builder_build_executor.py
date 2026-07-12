from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Mapping

from .builder_executor import (
    BUILDER_SNAPSHOT_ROOT_ENV,
    BUILDER_TASKS_ENABLED_ENV,
    BuilderTaskCanceled,
    BuilderTaskTimedOut,
    snapshot_store_root,
)
from .builder_tasks import (
    builder_registry_repository,
    parse_external_image_reference,
    sanitize_builder_task_result,
    validate_builder_task_request,
)
from .errors import LumaError


BUILDER_BUILD_CAPABILITY = "builder-build-v1"
BUILDER_BUILD_ENABLED_ENV = "LUMA_BUILDER_BUILD_ENABLED"
BUILDKIT_ADDR_ENV = "LUMA_BUILDER_BUILDKIT_ADDR"
BUILDER_REGISTRY_PULL_HOST_ENV = "LUMA_BUILDER_REGISTRY_PULL_HOST"
BUILDER_REGISTRY_PUSH_HOST_ENV = "LUMA_BUILDER_REGISTRY_PUSH_HOST"
BUILDER_REGISTRY_INSECURE_ENV = "LUMA_BUILDER_REGISTRY_INSECURE"
BUILDER_ALLOW_ANONYMOUS_REGISTRY_ENV = "LUMA_BUILDER_ALLOW_ANONYMOUS_REGISTRY"
BUILDER_TRIVY_CACHE_ENV = "LUMA_BUILDER_TRIVY_CACHE_DIR"
BUILDER_WORK_ROOT_ENV = "LUMA_BUILDER_WORK_ROOT"
BUILDER_EXTERNAL_REGISTRIES_ENV = "LUMA_BUILDER_EXTERNAL_REGISTRIES_JSON"

_REGISTRY_LEASE_SCHEMA_VERSION = "luma.builder-registry-lease/v1"
_REGISTRY_LEASE_FIELDS = frozenset(
    {
        "schemaVersion",
        "pullHost",
        "pushHost",
        "repositories",
        "externalRegistries",
        "insecure",
        "authMode",
    }
)
_LEASE_FIELDS = frozenset(
    {
        "builderTaskId",
        "schemaVersion",
        "externalOperationId",
        "tenantRef",
        "applicationRef",
        "principalRef",
        "sourceSnapshotId",
        "sourceSnapshotDigest",
        "signedBuildPlan",
        "credentialLeaseId",
        "limits",
        "registry",
    }
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REGISTRY_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._/-]{0,250}[a-z0-9])?$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_MAX_PROVENANCE_BYTES = 16 * 1024 * 1024
_MAX_JSON_ARTIFACT_BYTES = 64 * 1024 * 1024
_PROCESS_TERMINATE_GRACE_SECONDS = 3.0
_MAX_SNAPSHOT_ENTRIES = 400_000
_REQUIRED_TOOLS = ("buildctl", "syft", "trivy", "cosign", "crane")
_OCI_INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_OCI_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_IN_TOTO_MEDIA_TYPE = "application/vnd.in-toto+json"
_BUILDKIT_REFERENCE_TYPE_ANNOTATION = "vnd.docker.reference.type"
_BUILDKIT_REFERENCE_DIGEST_ANNOTATION = "vnd.docker.reference.digest"
_BUILDKIT_ATTESTATION_MANIFEST_TYPE = "attestation-manifest"
_IN_TOTO_PREDICATE_ANNOTATION = "in-toto.io/predicate-type"
_IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
_SLSA_PROVENANCE_PREFIX = "https://slsa.dev/provenance/"
_TRUSTED_STATIC_ADAPTER_PATH = ".lae/adapters/static-v1.Dockerfile"
_TRUSTED_STATIC_ADAPTER_DOCKERFILE = b"""# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM golang:1.26.5-bookworm@sha256:18aedc16aa19b3fd7ded7245fc14b109e054d65d22ed53c355c899582bbb2113 AS build

WORKDIR /src

COPY <<LAE_STATIC_SERVER_GO /src/main.go
package main

import (
    "io"
    "log"
    "net/http"
    "time"
)

func main() {
    files := http.FileServer(http.Dir("/srv/www"))
    handler := http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
        if request.URL.Path == "/healthz" {
            response.Header().Set("Cache-Control", "no-store")
            response.Header().Set("Content-Type", "text/plain; charset=utf-8")
            if request.Method != http.MethodGet && request.Method != http.MethodHead {
                response.Header().Set("Allow", "GET, HEAD")
                http.Error(response, "method not allowed", http.StatusMethodNotAllowed)
                return
            }
            response.WriteHeader(http.StatusOK)
            if request.Method == http.MethodGet {
                _, _ = io.WriteString(response, "ok\\n")
            }
            return
        }
        if request.Method != http.MethodGet && request.Method != http.MethodHead {
            response.Header().Set("Allow", "GET, HEAD")
            http.Error(response, "method not allowed", http.StatusMethodNotAllowed)
            return
        }
        response.Header().Set("X-Content-Type-Options", "nosniff")
        files.ServeHTTP(response, request)
    })
    server := &http.Server{
        Addr:              ":8080",
        Handler:           handler,
        ReadHeaderTimeout: 5 * time.Second,
        IdleTimeout:       60 * time.Second,
        MaxHeaderBytes:    1 << 20,
    }
    log.Fatal(server.ListenAndServe())
}
LAE_STATIC_SERVER_GO

RUN mkdir -p /out && \\
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \\
      go build -buildvcs=false -trimpath -ldflags='-s -w -buildid=' \\
      -o /out/lae-static-server /src/main.go

COPY . /site
RUN test -f /site/index.html && rm -rf /site/.lae

FROM scratch

LABEL tech.itool.lae.adapter="static-v1"

COPY --from=build --chown=10001:10001 /out/lae-static-server /usr/local/bin/lae-static-server
COPY --from=build --chown=10001:10001 /site/ /srv/www/

USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/lae-static-server"]
"""
_TRUSTED_STATIC_ADAPTER_DOCKERIGNORE_PATH = _TRUSTED_STATIC_ADAPTER_PATH + ".dockerignore"
_TRUSTED_STATIC_ADAPTER_DOCKERIGNORE = (
    b"# LAE static-v1 consumes the complete signed source snapshot.\n"
)
_TRUSTED_STATIC_ADAPTER_ASSETS = {
    _TRUSTED_STATIC_ADAPTER_PATH: _TRUSTED_STATIC_ADAPTER_DOCKERFILE,
    _TRUSTED_STATIC_ADAPTER_DOCKERIGNORE_PATH: _TRUSTED_STATIC_ADAPTER_DOCKERIGNORE,
}

_TRUSTED_NODE_ADAPTER_PATH = ".lae/adapters/node-v1.Dockerfile"
_TRUSTED_NODE_ADAPTER_ENTRYPOINT_PATH = ".lae/adapters/node-v1-entrypoint.sh"
_TRUSTED_NODE_ADAPTER_DOCKERFILE = b"""# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM node:22.21.0-bookworm-slim@sha256:f9f7f95dcf1f007b007c4dcd44ea8f7773f931b71dc79d57c216e731c87a090b

LABEL tech.itool.lae.adapter="node-v1"

WORKDIR /app

RUN groupadd --gid 10001 lae && \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/lae lae

COPY --chmod=0555 .lae/adapters/node-v1-entrypoint.sh /usr/local/bin/lae-node-entrypoint
COPY . /app

RUN rm -rf /app/.lae && \
    corepack enable && \
    if [ -f pnpm-lock.yaml ]; then \
      corepack pnpm install --frozen-lockfile; \
    elif [ -f yarn.lock ]; then \
      corepack yarn install --immutable || corepack yarn install --frozen-lockfile; \
    elif [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then \
      npm ci --include=dev; \
    else \
      npm install --include=dev; \
    fi && \
    if node -e 'const p=require("./package.json");process.exit(p.scripts&&p.scripts.build?0:1)'; then \
      if [ -f pnpm-lock.yaml ]; then corepack pnpm run build; \
      elif [ -f yarn.lock ]; then corepack yarn run build; \
      else npm run build; fi; \
    fi

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    HOSTNAME=0.0.0.0

USER 10001:10001

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/lae-node-entrypoint"]
"""
_TRUSTED_NODE_ADAPTER_ENTRYPOINT = b"""#!/bin/sh
set -eu

export HOST="${HOST:-0.0.0.0}"
export HOSTNAME="${HOSTNAME:-0.0.0.0}"
if [ -z "${PORT:-}" ]; then
  if node -e 'const p=require("./package.json");const d={...(p.dependencies||{}),...(p.devDependencies||{})};process.exit(d.vite||d.astro?0:1)'; then
    PORT=4173
  else
    PORT=3000
  fi
fi
export PORT

has_script() {
  node -e 'const p=require("./package.json");process.exit(p.scripts&&p.scripts[process.argv[1]]?0:1)' "$1"
}

run_script() {
  script="$1"
  shift
  if [ -f pnpm-lock.yaml ]; then
    exec corepack pnpm run "$script" "$@"
  elif [ -f yarn.lock ]; then
    exec corepack yarn run "$script" "$@"
  else
    exec npm run "$script" -- "$@"
  fi
}

if has_script start; then
  run_script start
fi
if has_script preview; then
  run_script preview --host 0.0.0.0 --port "$PORT"
fi
for entrypoint in server.js index.js app.js src/server.js src/index.js; do
  if [ -f "$entrypoint" ]; then
    exec node "$entrypoint"
  fi
done

echo "LAE node-v1 could not find a start or preview script, or a conventional server entrypoint" >&2
exit 64
"""
_TRUSTED_NODE_ADAPTER_DOCKERIGNORE_PATH = _TRUSTED_NODE_ADAPTER_PATH + ".dockerignore"
_TRUSTED_NODE_ADAPTER_DOCKERIGNORE = (
    b"# LAE node-v1 consumes the complete signed source snapshot.\n"
)
_TRUSTED_NODE_ADAPTER_ASSETS = {
    _TRUSTED_NODE_ADAPTER_PATH: _TRUSTED_NODE_ADAPTER_DOCKERFILE,
    _TRUSTED_NODE_ADAPTER_DOCKERIGNORE_PATH: _TRUSTED_NODE_ADAPTER_DOCKERIGNORE,
    _TRUSTED_NODE_ADAPTER_ENTRYPOINT_PATH: _TRUSTED_NODE_ADAPTER_ENTRYPOINT,
}

_TRUSTED_PYTHON_ADAPTER_PATH = ".lae/adapters/python-v1.Dockerfile"
_TRUSTED_PYTHON_ADAPTER_RUNTIME_PATH = ".lae/adapters/python-v1-runtime.py"
_TRUSTED_PYTHON_ADAPTER_DOCKERFILE = b"""# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.12.13-alpine3.23@sha256:efc8538b7449b6d893de5d852c87a0dc2cffd0ec27b07dd98ba3e7edaadc26af

LABEL tech.itool.lae.adapter="python-v1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    PYTHONPATH=/usr/local/lib/lae:/app

WORKDIR /app

RUN addgroup -g 10001 lae && \
    adduser -D -u 10001 -G lae -h /home/lae lae && \
    mkdir -p /usr/local/lib/lae && \
    chmod 0555 /usr/local/lib/lae

COPY --chmod=0444 .lae/adapters/python-v1-runtime.py /usr/local/lib/lae/lae_python_runtime.py
COPY . /app

RUN rm -rf /app/.lae && \
    if [ -f requirements.txt ]; then \
      python -m pip install --no-cache-dir --requirement requirements.txt; \
    elif [ -f pyproject.toml ]; then \
      python -m pip install --no-cache-dir .; \
    elif [ -f Pipfile ]; then \
      python -m pip install --no-cache-dir pipenv && \
      if [ -f Pipfile.lock ]; then \
        PIPENV_IGNORE_VIRTUALENVS=1 pipenv install --system --deploy; \
      else \
        PIPENV_IGNORE_VIRTUALENVS=1 pipenv install --system --skip-lock; \
      fi; \
    else \
      echo "LAE python-v1 requires requirements.txt, pyproject.toml, or Pipfile" >&2; \
      exit 64; \
    fi && \
    (python -c 'import uvicorn' || python -m pip install --no-cache-dir 'uvicorn==0.35.0') && \
    (command -v gunicorn >/dev/null 2>&1 || python -m pip install --no-cache-dir 'gunicorn==23.0.0')

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["python", "-m", "lae_python_runtime"]
"""
_TRUSTED_PYTHON_ADAPTER_RUNTIME = b'''from __future__ import annotations

import importlib
import inspect
import os
import shutil
import sys
from collections.abc import Callable
from typing import Any


_CANDIDATES = (
    "main:app",
    "main:application",
    "main:api",
    "app:app",
    "app:application",
    "application:app",
    "src.main:app",
    "src.app:app",
)


def _load(spec: str) -> Any:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise RuntimeError("invalid LAE Python application target")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute)
    if not callable(target):
        raise RuntimeError("LAE Python application target is not callable")
    return target


def _discover() -> tuple[str, str]:
    for spec in _CANDIDATES:
        try:
            target = _load(spec)
        except (ImportError, AttributeError):
            continue
        call = getattr(target, "__call__", None)
        kind = "asgi" if inspect.iscoroutinefunction(call) else "wsgi"
        return spec, kind
    raise RuntimeError(
        "LAE python-v1 could not find a callable app in main.py, app.py, application.py, src/main.py, or src/app.py"
    )


class _ASGIHealth:
    def __init__(self, target: Callable[..., Any]):
        self.target = target

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/healthz":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b"ok\\n"})
            return
        await self.target(scope, receive, send)


class _WSGIHealth:
    def __init__(self, target: Callable[..., Any]):
        self.target = target

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        if environ.get("PATH_INFO") == "/healthz":
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store"), ("Content-Length", "3")])
            return [b"ok\\n"]
        return self.target(environ, start_response)


_target_spec = os.environ.get("LAE_PYTHON_TARGET", "")
_target_kind = os.environ.get("LAE_PYTHON_KIND", "")
if _target_spec:
    _target = _load(_target_spec)
    application = _ASGIHealth(_target) if _target_kind == "asgi" else _WSGIHealth(_target)


def _port() -> int:
    try:
        value = int(os.environ.get("PORT", "8000"))
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")
    return value


def main() -> None:
    target, kind = _discover()
    os.environ["LAE_PYTHON_TARGET"] = target
    os.environ["LAE_PYTHON_KIND"] = kind
    port = str(_port())
    if kind == "asgi":
        os.execv(
            sys.executable,
            [sys.executable, "-m", "uvicorn", "lae_python_runtime:application", "--host", "0.0.0.0", "--port", port],
        )
    gunicorn = shutil.which("gunicorn")
    if not gunicorn:
        raise RuntimeError("LAE python-v1 requires gunicorn for a WSGI application")
    os.execv(gunicorn, [gunicorn, "lae_python_runtime:application", "--bind", f"0.0.0.0:{port}", "--workers", "1"])


if __name__ == "__main__":
    main()
'''
_TRUSTED_PYTHON_ADAPTER_DOCKERIGNORE_PATH = _TRUSTED_PYTHON_ADAPTER_PATH + ".dockerignore"
_TRUSTED_PYTHON_ADAPTER_DOCKERIGNORE = (
    b"# LAE python-v1 consumes the complete signed source snapshot.\n"
)
_TRUSTED_PYTHON_ADAPTER_ASSETS = {
    _TRUSTED_PYTHON_ADAPTER_PATH: _TRUSTED_PYTHON_ADAPTER_DOCKERFILE,
    _TRUSTED_PYTHON_ADAPTER_DOCKERIGNORE_PATH: _TRUSTED_PYTHON_ADAPTER_DOCKERIGNORE,
    _TRUSTED_PYTHON_ADAPTER_RUNTIME_PATH: _TRUSTED_PYTHON_ADAPTER_RUNTIME,
}

_TRUSTED_ADAPTER_ASSETS = {
    _TRUSTED_STATIC_ADAPTER_PATH: _TRUSTED_STATIC_ADAPTER_ASSETS,
    _TRUSTED_NODE_ADAPTER_PATH: _TRUSTED_NODE_ADAPTER_ASSETS,
    _TRUSTED_PYTHON_ADAPTER_PATH: _TRUSTED_PYTHON_ADAPTER_ASSETS,
}


@dataclass(frozen=True)
class _RuntimePrerequisites:
    buildctl: str
    syft: str
    trivy: str
    cosign: str
    crane: str
    buildkit_addr: str
    trivy_cache: Path


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    output: bytes
    truncated: bool = False


def builder_build_available(os_name: str) -> bool:
    """Return true only for the complete, rootless build lane.

    There is intentionally no Docker/buildx fallback.  Advertising this
    capability means rootless BuildKit, SBOM generation, provenance retrieval,
    and an offline-ready vulnerability database are all usable.  Registry
    credentials and build secrets are not silently borrowed from legacy Control
    state; the current lane is explicitly anonymous-registry-only until a
    dedicated short-lived build credential broker exists.
    """

    if str(os_name or "").lower() != "linux":
        return False
    if str(os.environ.get(BUILDER_TASKS_ENABLED_ENV) or "").strip() != "1":
        return False
    if str(os.environ.get(BUILDER_BUILD_ENABLED_ENV) or "").strip() != "1":
        return False
    if str(os.environ.get(BUILDER_ALLOW_ANONYMOUS_REGISTRY_ENV) or "").strip() != "1":
        return False
    try:
        _local_registry_policy()
        _local_external_registry_allowlist()
        _runtime_prerequisites()
        root = snapshot_store_root()
        work = _work_parent()
        _writable_parent(root)
        _writable_parent(work)
    except (LumaError, OSError, subprocess.SubprocessError, ValueError):
        return False
    return True


def build_plan(
    payload: Dict[str, Any],
    *,
    cancel_event: Any = None,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Execute a signed, snapshot-bound LAE build plan.

    The source tar is addressed only by its expected digest and is re-hashed
    before extraction.  All paths and the dependency graph are checked again on
    the node even though Control has already validated and signed the plan.
    """

    normalized = _validate_build_lease(payload)
    _raise_if_canceled(cancel_event)
    deadline = time.monotonic() + normalized["limits"]["timeoutSeconds"]
    disk_limit_bytes = int(normalized["limits"]["diskMiB"]) * 1024 * 1024
    snapshot = _snapshot_path(normalized["sourceSnapshotDigest"])
    _verify_snapshot(snapshot, normalized["sourceSnapshotDigest"], disk_limit_bytes=disk_limit_bytes)

    work_parent = _work_parent()
    work_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_free_disk(work_parent, disk_limit_bytes)
    with tempfile.TemporaryDirectory(prefix="luma-build-plan-", dir=str(work_parent)) as temporary:
        work = Path(temporary)
        source = work / "source"
        artifacts_dir = work / "artifacts"
        artifacts_dir.mkdir(mode=0o700)
        _extract_snapshot_secure(
            snapshot,
            source,
            disk_limit_bytes=disk_limit_bytes,
            cancel_event=cancel_event,
        )
        builds = list(normalized["signedBuildPlan"]["builds"])
        external_images = list(normalized["signedBuildPlan"]["externalImages"])
        ordered_builds = _topological_builds(builds)
        _materialize_trusted_build_adapters(source, ordered_builds)
        resolved_paths = {
            build["key"]: _resolve_build_paths(source, build)
            for build in ordered_builds
        }
        _ensure_disk_budget(work, disk_limit_bytes)

        if not ordered_builds and not external_images:
            return _validated_result(
                normalized,
                images={},
                image_digests={},
                sbom_digests={},
                provenance_digests={},
                scan_digests={},
                artifacts={},
            )

        # Build arguments and BuildKit secret mounts require values from a
        # dedicated short-lived build credential redemption.  The current
        # broker only supports source Git credentials, so accepting any value
        # from this task payload would cross the trust boundary.
        for build in ordered_builds:
            if build.get("buildArgNames") or build.get("secretMountNames"):
                raise LumaError("build credential lease redemption is unavailable for build args or secret mounts")

        prerequisites = _runtime_prerequisites()
        _verify_runtime_registry_policy(normalized["registry"])
        docker_config = work / "docker-config"
        docker_config.mkdir(mode=0o700)
        (docker_config / "config.json").write_text('{"auths":{}}', encoding="utf-8")
        os.chmod(docker_config / "config.json", 0o600)
        command_env = _command_environment(docker_config)

        images: Dict[str, str] = {}
        image_digests: Dict[str, str] = {}
        sbom_digests: Dict[str, str] = {}
        provenance_digests: Dict[str, str] = {}
        scan_digests: Dict[str, str] = {}
        artifact_descriptors: Dict[str, Dict[str, Any]] = {}

        for build in ordered_builds:
            _raise_if_canceled(cancel_event)
            key = build["key"]
            context, dockerfile = resolved_paths[key]
            repository = normalized["registry"]["repositories"][key]
            unique_tag = "build-" + hashlib.sha256(
                f"{normalized['builderTaskId']}\0{key}".encode("utf-8")
            ).hexdigest()[:24]
            push_tag = f"{normalized['registry']['pushHost']}/{repository}:{unique_tag}"
            pull_repository = f"{normalized['registry']['pullHost']}/{repository}"
            metadata_path = artifacts_dir / f"{key}-build-metadata.json"

            _emit_phase(progress, key, "build", "running")
            build_command = _buildctl_command(
                prerequisites,
                build=build,
                context=context,
                dockerfile=dockerfile,
                push_tag=push_tag,
                metadata_path=metadata_path,
            )
            build_result = _run_command(
                build_command,
                env=command_env,
                timeout=_remaining_seconds(deadline),
                cancel_event=cancel_event,
            )
            if build_result.returncode != 0:
                raise LumaError("rootless BuildKit build failed")
            image_digest = _read_buildkit_image_digest(metadata_path)
            immutable_push = f"{normalized['registry']['pushHost']}/{repository}@{image_digest}"
            immutable_pull = f"{pull_repository}@{image_digest}"
            _emit_phase(progress, key, "build", "succeeded")

            _emit_phase(progress, key, "sbom", "running")
            sbom_path = artifacts_dir / f"{key}-sbom.json"
            sbom_command = [
                prerequisites.syft,
                "scan",
                immutable_push,
                "--output",
                f"cyclonedx-json={sbom_path}",
            ]
            sbom_result = _run_command(
                sbom_command,
                env=_syft_environment(
                    command_env,
                    insecure=normalized["registry"]["insecure"],
                ),
                timeout=_remaining_seconds(deadline),
                cancel_event=cancel_event,
            )
            if sbom_result.returncode != 0:
                raise LumaError("SBOM generation failed")
            _validate_cyclonedx(sbom_path)
            _emit_phase(progress, key, "sbom", "succeeded")

            _emit_phase(progress, key, "scan", "running")
            scan_path = artifacts_dir / f"{key}-scan.json"
            scan_command = [
                prerequisites.trivy,
                "image",
                "--cache-dir",
                str(prerequisites.trivy_cache),
                "--offline-scan",
                "--skip-db-update",
                "--scanners",
                "vuln",
                "--severity",
                "HIGH,CRITICAL",
                "--exit-code",
                # Vulnerability findings are persisted for policy and UI
                # decisions, but the default LAE admission path must not turn
                # every base-image finding into an opaque build failure.  A
                # non-zero exit now means the scanner itself failed; findings
                # remain available in the verified report.
                "0",
                "--format",
                "json",
                "--output",
                str(scan_path),
            ]
            if normalized["registry"]["insecure"]:
                scan_command.append("--insecure")
            scan_command.append(immutable_push)
            scan_result = _run_command(
                scan_command,
                env=command_env,
                timeout=_remaining_seconds(deadline),
                cancel_event=cancel_event,
            )
            if scan_result.returncode != 0:
                raise LumaError("image vulnerability scan execution failed")
            _validate_scan_report(scan_path)
            _emit_phase(progress, key, "scan", "succeeded")

            _emit_phase(progress, key, "provenance", "running")
            canonical_provenance = _retrieve_buildkit_provenance(
                prerequisites.crane,
                immutable_push,
                expected_index_digest=image_digest,
                insecure=normalized["registry"]["insecure"],
                env=command_env,
                timeout=_remaining_seconds(deadline),
                cancel_event=cancel_event,
            )
            provenance_path = artifacts_dir / f"{key}-provenance.json"
            _write_private_file(provenance_path, canonical_provenance)
            _emit_phase(progress, key, "provenance", "succeeded")

            sbom_descriptor = _persist_artifact(
                sbom_path,
                namespace="artifacts/build/sbom/sha256",
                media_type="application/vnd.cyclonedx+json",
            )
            provenance_descriptor = _persist_artifact(
                provenance_path,
                namespace="artifacts/build/provenance/sha256",
                media_type="application/vnd.in-toto+json",
            )
            scan_descriptor = _persist_artifact(
                scan_path,
                namespace="artifacts/build/scan/sha256",
                media_type="application/vnd.lae.scan-report+json",
            )
            images[key] = immutable_pull
            image_digests[key] = image_digest
            sbom_digests[key] = sbom_descriptor["digest"]
            provenance_digests[key] = provenance_descriptor["digest"]
            scan_digests[key] = scan_descriptor["digest"]
            artifact_descriptors[f"{key}-sbom"] = sbom_descriptor
            artifact_descriptors[f"{key}-provenance"] = provenance_descriptor
            artifact_descriptors[f"{key}-scan"] = scan_descriptor
            _ensure_disk_budget(work, disk_limit_bytes)

        for external_image in external_images:
            _raise_if_canceled(cancel_event)
            key = external_image["key"]
            parsed_reference = parse_external_image_reference(external_image["ref"])
            if parsed_reference["registryHost"] not in normalized["registry"]["externalRegistries"]:
                raise LumaError("external image registry is not allowlisted for this builder task")

            _emit_phase(progress, key, "resolve", "running")
            expected_digest = external_image["resolvedDigest"]
            if parsed_reference["digest"]:
                image_digest = parsed_reference["digest"]
            else:
                resolution = _run_command(
                    [
                        prerequisites.crane,
                        "digest",
                        "--platform",
                        external_image["platform"],
                        parsed_reference["reference"],
                    ],
                    env=command_env,
                    timeout=_remaining_seconds(deadline),
                    cancel_event=cancel_event,
                )
                if resolution.returncode != 0 or resolution.truncated:
                    raise LumaError("anonymous external image resolution failed")
                image_digest = _parse_resolved_digest(resolution.output)
            if image_digest != expected_digest:
                raise LumaError("external image resolver returned a digest that does not match the signed plan")
            immutable_reference = f"{parsed_reference['canonicalName']}@{image_digest}"
            _emit_phase(progress, key, "resolve", "succeeded")

            _emit_phase(progress, key, "sbom", "running")
            sbom_path = artifacts_dir / f"{key}-sbom.json"
            sbom_result = _run_command(
                [
                    prerequisites.syft,
                    "scan",
                    immutable_reference,
                    "--output",
                    f"cyclonedx-json={sbom_path}",
                ],
                env=command_env,
                timeout=_remaining_seconds(deadline),
                cancel_event=cancel_event,
            )
            if sbom_result.returncode != 0:
                raise LumaError("external image SBOM generation failed")
            _validate_cyclonedx(sbom_path)
            _emit_phase(progress, key, "sbom", "succeeded")

            _emit_phase(progress, key, "scan", "running")
            scan_path = artifacts_dir / f"{key}-scan.json"
            scan_result = _run_command(
                [
                    prerequisites.trivy,
                    "image",
                    "--cache-dir",
                    str(prerequisites.trivy_cache),
                    "--offline-scan",
                    "--skip-db-update",
                    "--scanners",
                    "vuln",
                    "--severity",
                    "HIGH,CRITICAL",
                    "--exit-code",
                    "0",
                    "--format",
                    "json",
                    "--output",
                    str(scan_path),
                    immutable_reference,
                ],
                env=command_env,
                timeout=_remaining_seconds(deadline),
                cancel_event=cancel_event,
            )
            if scan_result.returncode != 0:
                raise LumaError("external image vulnerability scan execution failed")
            _validate_scan_report(scan_path)
            _emit_phase(progress, key, "scan", "succeeded")

            provenance_path = artifacts_dir / f"{key}-provenance.json"
            _write_private_file(
                provenance_path,
                _external_resolution_statement(
                    source_reference=parsed_reference["reference"],
                    immutable_reference=immutable_reference,
                    image_digest=image_digest,
                    platform=external_image["platform"],
                    registry_host=parsed_reference["registryHost"],
                ),
            )
            sbom_descriptor = _persist_artifact(
                sbom_path,
                namespace="artifacts/external/sbom/sha256",
                media_type="application/vnd.cyclonedx+json",
            )
            provenance_descriptor = _persist_artifact(
                provenance_path,
                namespace="artifacts/external/resolution/sha256",
                media_type="application/vnd.lae.external-resolution+json",
            )
            scan_descriptor = _persist_artifact(
                scan_path,
                namespace="artifacts/external/scan/sha256",
                media_type="application/vnd.lae.scan-report+json",
            )
            images[key] = immutable_reference
            image_digests[key] = image_digest
            sbom_digests[key] = sbom_descriptor["digest"]
            provenance_digests[key] = provenance_descriptor["digest"]
            scan_digests[key] = scan_descriptor["digest"]
            artifact_descriptors[f"{key}-sbom"] = sbom_descriptor
            artifact_descriptors[f"{key}-provenance"] = provenance_descriptor
            artifact_descriptors[f"{key}-scan"] = scan_descriptor
            _ensure_disk_budget(work, disk_limit_bytes)

        return _validated_result(
            normalized,
            images=images,
            image_digests=image_digests,
            sbom_digests=sbom_digests,
            provenance_digests=provenance_digests,
            scan_digests=scan_digests,
            artifacts=artifact_descriptors,
        )


def _validate_build_lease(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise LumaError("build-plan lease payload must be an object")
    unknown = sorted(set(payload) - _LEASE_FIELDS)
    missing = sorted(_LEASE_FIELDS - set(payload))
    if unknown or missing:
        details = []
        if unknown:
            details.append("unknown fields: " + ", ".join(unknown))
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        raise LumaError("build-plan lease payload has a closed schema (" + "; ".join(details) + ")")
    builder_task_id = _reference(payload.get("builderTaskId"), "builderTaskId")
    principal_ref = _reference(payload.get("principalRef"), "principalRef")
    public_request = {
        "schemaVersion": payload.get("schemaVersion"),
        "kind": "build-plan",
        "externalOperationId": payload.get("externalOperationId"),
        "tenantRef": payload.get("tenantRef"),
        "applicationRef": payload.get("applicationRef"),
        "payload": {
            "sourceSnapshotId": payload.get("sourceSnapshotId"),
            "sourceSnapshotDigest": payload.get("sourceSnapshotDigest"),
            "signedBuildPlan": payload.get("signedBuildPlan"),
            "credentialLeaseId": payload.get("credentialLeaseId"),
            "limits": payload.get("limits"),
        },
    }
    validated = validate_builder_task_request(public_request)
    registry = _validate_registry_lease(
        payload.get("registry"),
        principal_ref=principal_ref,
        tenant_ref=validated["tenantRef"],
        application_ref=validated["applicationRef"],
        builds=validated["payload"]["signedBuildPlan"]["builds"],
        external_images=validated["payload"]["signedBuildPlan"]["externalImages"],
    )
    return {
        "builderTaskId": builder_task_id,
        "schemaVersion": validated["schemaVersion"],
        "externalOperationId": validated["externalOperationId"],
        "tenantRef": validated["tenantRef"],
        "applicationRef": validated["applicationRef"],
        "principalRef": principal_ref,
        **validated["payload"],
        "registry": registry,
    }


def _validate_registry_lease(
    value: Any,
    *,
    principal_ref: str,
    tenant_ref: str,
    application_ref: str,
    builds: Iterable[Dict[str, Any]],
    external_images: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REGISTRY_LEASE_FIELDS:
        raise LumaError("build-plan registry lease has a closed schema")
    if value.get("schemaVersion") != _REGISTRY_LEASE_SCHEMA_VERSION:
        raise LumaError("build-plan registry lease schemaVersion is unsupported")
    pull_host = _registry_host(value.get("pullHost"), "pullHost")
    push_host = _registry_host(value.get("pushHost"), "pushHost")
    if value.get("authMode") != "anonymous":
        raise LumaError("build registry credential broker is unavailable; authMode must be anonymous")
    if not isinstance(value.get("insecure"), bool):
        raise LumaError("build-plan registry lease insecure must be a boolean")
    raw_repositories = value.get("repositories")
    if not isinstance(raw_repositories, dict):
        raise LumaError("build-plan registry repositories must be an object")
    build_keys = [str(build.get("key") or "") for build in builds]
    if set(raw_repositories) != set(build_keys):
        raise LumaError("build-plan registry repositories do not match signed build keys")
    repositories: Dict[str, str] = {}
    for key in build_keys:
        repository = str(raw_repositories.get(key) or "").strip().lower()
        expected = builder_registry_repository(principal_ref, tenant_ref, application_ref, key)
        if repository != expected or not _REPOSITORY_RE.fullmatch(repository):
            raise LumaError(f"build-plan registry repository binding does not match signed scope for {key}")
        repositories[key] = repository
    raw_external_registries = value.get("externalRegistries")
    external_registries = _validate_external_registry_list(
        raw_external_registries,
        label="build-plan registry lease externalRegistries",
    )
    requested_external_registries = {
        parse_external_image_reference(str(item.get("ref") or ""))["registryHost"]
        for item in external_images
    }
    if not requested_external_registries.issubset(set(external_registries)):
        raise LumaError("build-plan external image registry is not allowlisted in the lease")
    return {
        "schemaVersion": _REGISTRY_LEASE_SCHEMA_VERSION,
        "pullHost": pull_host,
        "pushHost": push_host,
        "repositories": repositories,
        "externalRegistries": external_registries,
        "insecure": bool(value["insecure"]),
        "authMode": "anonymous",
    }


def _topological_builds(builds: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    by_key = {build["key"]: build for build in builds}
    indegree = {key: 0 for key in by_key}
    dependents: Dict[str, list[str]] = {key: [] for key in by_key}
    order_index = {build["key"]: index for index, build in enumerate(builds)}
    for build in builds:
        for dependency in build.get("dependsOnBuilds") or []:
            if dependency not in by_key:
                raise LumaError(f"build {build['key']} depends on unknown build {dependency}")
            indegree[build["key"]] += 1
            dependents[dependency].append(build["key"])
    ready = sorted((key for key, count in indegree.items() if count == 0), key=order_index.get)
    result: list[Dict[str, Any]] = []
    while ready:
        key = ready.pop(0)
        result.append(by_key[key])
        for dependent in sorted(dependents[key], key=order_index.get):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=order_index.get)
    if len(result) != len(builds):
        raise LumaError("signedBuildPlan build dependency graph contains a cycle")
    return result


def _snapshot_path(digest: str) -> Path:
    if not _SHA256_RE.fullmatch(str(digest or "")):
        raise LumaError("source snapshot digest is invalid")
    hexadecimal = digest.split(":", 1)[1]
    root = snapshot_store_root()
    return root / "sha256" / hexadecimal[:2] / f"{hexadecimal}.tar"


def _verify_snapshot(path: Path, expected_digest: str, *, disk_limit_bytes: int) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise LumaError("source snapshot is not present on this builder") from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise LumaError("source snapshot digest path is not a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > disk_limit_bytes:
        raise LumaError("source snapshot exceeds the build disk budget")
    actual = _sha256_file(path)
    if actual != expected_digest:
        raise LumaError("source snapshot digest mismatch")


def _extract_snapshot_secure(
    snapshot: Path,
    destination: Path,
    *,
    disk_limit_bytes: int,
    cancel_event: Any,
) -> None:
    destination.mkdir(mode=0o700)
    root = destination.resolve()
    total_bytes = 0
    seen: set[PurePosixPath] = set()
    symlinks: set[PurePosixPath] = set()
    with tarfile.open(snapshot, mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > _MAX_SNAPSHOT_ENTRIES:
            raise LumaError("source snapshot contains too many entries")
        for member in members:
            _raise_if_canceled(cancel_event)
            relative = _safe_tar_path(member.name)
            if relative in seen:
                raise LumaError("source snapshot contains duplicate paths")
            seen.add(relative)
            if any(parent in symlinks for parent in relative.parents):
                raise LumaError("source snapshot attempts to write through a symlink")
            target = destination.joinpath(*relative.parts)
            _assert_within_root(target.parent.resolve(), root, "source snapshot path escapes the workspace")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=member.mode & 0o755 or 0o755)
                continue
            if member.issym():
                link_target = PurePosixPath(member.linkname)
                if link_target.is_absolute() or "\0" in member.linkname:
                    raise LumaError("source snapshot contains an unsafe symlink")
                normalized_target = _normalize_relative_parts(relative.parent, link_target)
                if normalized_target is None:
                    raise LumaError("source snapshot symlink escapes the workspace")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.symlink(member.linkname, target)
                symlinks.add(relative)
                continue
            if not member.isfile() or member.islnk():
                raise LumaError("source snapshot contains an unsupported entry")
            total_bytes += int(member.size)
            if total_bytes > disk_limit_bytes:
                raise LumaError("source snapshot expands beyond the build disk budget")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise LumaError("source snapshot contains an unreadable file")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags, member.mode & 0o755 or 0o600)
            try:
                with extracted, os.fdopen(fd, "wb", closefd=False) as output:
                    shutil.copyfileobj(extracted, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(fd)


def _safe_tar_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\0" in value:
        raise LumaError("source snapshot contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LumaError("source snapshot contains an unsafe path")
    return path


def _normalize_relative_parts(base: PurePosixPath, target: PurePosixPath) -> PurePosixPath | None:
    parts: list[str] = []
    for part in (*base.parts, *target.parts):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts)


def _materialize_trusted_build_adapters(source: Path, builds: Iterable[Dict[str, Any]]) -> None:
    """Materialize only the platform adapter explicitly named by the signed plan.

    The source workspace is private to this build lease, but every parent and
    destination is still checked with lstat before using O_EXCL/O_NOFOLLOW.  A
    tenant snapshot can therefore neither replace the trusted adapter nor steer
    its write through a symlink.
    """

    requested = {str(build.get("dockerfile") or "") for build in builds}
    assets: Dict[str, bytes] = {}
    for adapter_path, adapter_assets in _TRUSTED_ADAPTER_ASSETS.items():
        if adapter_path in requested:
            assets.update(adapter_assets)
    if not assets:
        return

    root = source.resolve(strict=True)
    targets: list[tuple[Path, bytes]] = []
    for relative, content in assets.items():
        relative_path = PurePosixPath(relative)
        current = source
        for part in relative_path.parent.parts:
            current = current / part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                current_stat = current.lstat()
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise LumaError("reserved platform adapter path conflicts with the source snapshot")
            _assert_within_root(
                current.resolve(strict=True),
                root,
                "reserved platform adapter path escapes the source snapshot",
            )
        target = source.joinpath(*relative_path.parts)
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise LumaError("reserved platform adapter path conflicts with the source snapshot")
        targets.append((target, content))

    for target, content in targets:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(fd)


def _resolve_build_paths(source: Path, build: Dict[str, Any]) -> tuple[Path, Path]:
    root = source.resolve()
    context = _resolve_snapshot_path(root, str(build.get("context") or ""), expect_directory=True)
    dockerfile = _resolve_snapshot_path(root, str(build.get("dockerfile") or ""), expect_directory=False)
    _validate_context_symlinks(context, root)
    if not dockerfile.is_file():
        raise LumaError(f"build {build['key']} Dockerfile is not a regular file")
    return context, dockerfile


def _resolve_snapshot_path(root: Path, relative: str, *, expect_directory: bool) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or any(part == ".." for part in raw.parts) or "\0" in relative:
        raise LumaError("build path escapes the source snapshot")
    try:
        candidate = (root / raw).resolve(strict=True)
    except FileNotFoundError as exc:
        kind = "context" if expect_directory else "Dockerfile"
        raise LumaError(f"build {kind} is not present in the source snapshot") from exc
    _assert_within_root(candidate, root, "build path escapes the source snapshot through a symlink")
    if expect_directory and not candidate.is_dir():
        raise LumaError("build context is not a directory")
    if not expect_directory and not candidate.is_file():
        raise LumaError("build Dockerfile is not a file")
    return candidate


def _validate_context_symlinks(context: Path, source_root: Path) -> None:
    for current, directory_names, file_names in os.walk(context, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directory_names) + list(file_names):
            candidate = current_path / name
            if not candidate.is_symlink():
                continue
            raw_target = os.readlink(candidate)
            if os.path.isabs(raw_target):
                raise LumaError("build context contains an absolute symlink")
            resolved = (candidate.parent / raw_target).resolve(strict=False)
            _assert_within_root(resolved, source_root, "build context symlink escapes the source snapshot")


def _buildctl_command(
    prerequisites: _RuntimePrerequisites,
    *,
    build: Dict[str, Any],
    context: Path,
    dockerfile: Path,
    push_tag: str,
    metadata_path: Path,
) -> list[str]:
    command = [
        prerequisites.buildctl,
        "--addr",
        prerequisites.buildkit_addr,
        "build",
        "--frontend",
        "dockerfile.v0",
        "--local",
        f"context={context}",
        "--local",
        f"dockerfile={dockerfile.parent}",
        "--opt",
        f"filename={dockerfile.name}",
        "--opt",
        "platform=linux/amd64",
        "--output",
        f"type=image,name={push_tag},push=true",
        "--metadata-file",
        str(metadata_path),
        "--opt",
        "attest:provenance=mode=max",
    ]
    target = str(build.get("target") or "").strip()
    if target:
        command.extend(["--opt", f"target={target}"])
    return command


def _read_buildkit_image_digest(path: Path) -> str:
    body = _read_json_file(path, max_bytes=1024 * 1024, label="BuildKit metadata")
    digest = str(body.get("containerimage.digest") or "")
    descriptor = body.get("containerimage.descriptor")
    if not digest and isinstance(descriptor, dict):
        digest = str(descriptor.get("digest") or "")
    if not _SHA256_RE.fullmatch(digest):
        raise LumaError("BuildKit metadata did not return an immutable image digest")
    return digest


def _retrieve_buildkit_provenance(
    crane: str,
    immutable_reference: str,
    *,
    expected_index_digest: str,
    insecure: bool,
    env: Mapping[str, str],
    timeout: int,
    cancel_event: Any,
) -> bytes:
    """Retrieve BuildKit's attached OCI attestation and preserve the LAE DSSE bundle contract."""

    if not _SHA256_RE.fullmatch(expected_index_digest):
        raise LumaError("BuildKit provenance image index digest is invalid")
    try:
        repository, reference_digest = immutable_reference.rsplit("@", 1)
    except ValueError as exc:
        raise LumaError("BuildKit provenance image reference is not immutable") from exc
    if not repository or reference_digest != expected_index_digest:
        raise LumaError("BuildKit provenance image reference does not match build metadata")

    initial_timeout = max(int(timeout), 1)
    deadline = time.monotonic() + initial_timeout
    index = _crane_manifest_json(
        crane,
        immutable_reference,
        expected_digest=expected_index_digest,
        insecure=insecure,
        env=env,
        timeout=initial_timeout,
        cancel_event=cancel_event,
        label="BuildKit image index",
    )
    runnable_digest, attestation_digest, attestation_size = _select_buildkit_attestation(index)
    attestation = _crane_manifest_json(
        crane,
        f"{repository}@{attestation_digest}",
        expected_digest=attestation_digest,
        expected_size=attestation_size,
        insecure=insecure,
        env=env,
        timeout=_remaining_seconds(deadline),
        cancel_event=cancel_event,
        label="BuildKit attestation manifest",
    )
    layers = _select_buildkit_provenance_layers(attestation)

    statements: list[Dict[str, Any]] = []
    total_bytes = 0
    for layer_digest, predicate_type, layer_size in layers:
        command = [crane, "blob"]
        if insecure:
            command.append("--insecure")
        command.append(f"{repository}@{layer_digest}")
        result = _run_command(
            command,
            env=env,
            timeout=_remaining_seconds(deadline),
            cancel_event=cancel_event,
            max_output_bytes=layer_size,
        )
        if result.returncode != 0:
            raise LumaError("BuildKit provenance blob retrieval failed")
        if result.truncated:
            raise LumaError("BuildKit provenance output exceeds the artifact limit")
        if len(result.output) != layer_size:
            raise LumaError("BuildKit provenance blob does not match its OCI descriptor size")
        if "sha256:" + hashlib.sha256(result.output).hexdigest() != layer_digest:
            raise LumaError("BuildKit provenance blob does not match its OCI descriptor digest")
        total_bytes += len(result.output)
        if total_bytes > _MAX_PROVENANCE_BYTES:
            raise LumaError("BuildKit provenance output exceeds the artifact limit")
        statements.append(
            _validate_buildkit_statement(
                result.output,
                expected_subject_digest=runnable_digest,
                expected_predicate_type=predicate_type,
            )
        )

    envelopes = []
    for statement in statements:
        payload = json.dumps(
            statement,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        envelopes.append(
            {
                "payloadType": _IN_TOTO_MEDIA_TYPE,
                "payload": base64.b64encode(payload).decode("ascii"),
                "signatures": [],
            }
        )
    canonical = json.dumps(
        envelopes,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if not canonical or len(canonical) > _MAX_PROVENANCE_BYTES:
        raise LumaError("BuildKit provenance output exceeds the artifact limit")
    return canonical


def _crane_manifest_json(
    crane: str,
    reference: str,
    *,
    expected_digest: str,
    expected_size: int | None = None,
    insecure: bool,
    env: Mapping[str, str],
    timeout: int,
    cancel_event: Any,
    label: str,
) -> Dict[str, Any]:
    command = [crane, "manifest"]
    if insecure:
        command.append("--insecure")
    command.append(reference)
    result = _run_command(
        command,
        env=env,
        timeout=timeout,
        cancel_event=cancel_event,
        max_output_bytes=_MAX_COMMAND_OUTPUT_BYTES,
    )
    if result.returncode != 0 or result.truncated or not result.output:
        raise LumaError(f"{label} retrieval failed")
    if not _SHA256_RE.fullmatch(expected_digest):
        raise LumaError(f"{label} digest is invalid")
    if expected_size is not None and len(result.output) != expected_size:
        raise LumaError(f"{label} does not match its OCI descriptor size")
    if "sha256:" + hashlib.sha256(result.output).hexdigest() != expected_digest:
        raise LumaError(f"{label} does not match its OCI descriptor digest")
    try:
        body = json.loads(result.output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LumaError(f"{label} is not valid JSON") from exc
    if not isinstance(body, dict):
        raise LumaError(f"{label} JSON must be an object")
    return body


def _select_buildkit_attestation(index: Dict[str, Any]) -> tuple[str, str, int]:
    if index.get("schemaVersion") != 2 or index.get("mediaType") not in _OCI_INDEX_MEDIA_TYPES:
        raise LumaError("BuildKit provenance requires an OCI image index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or not manifests or len(manifests) > 1024:
        raise LumaError("BuildKit image index contains an invalid manifest list")

    runnable: list[str] = []
    attestation_descriptors: list[tuple[str, str, int]] = []
    for descriptor in manifests:
        if not isinstance(descriptor, dict):
            raise LumaError("BuildKit image index contains an invalid descriptor")
        digest = descriptor.get("digest")
        media_type = descriptor.get("mediaType")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise LumaError("BuildKit image index contains an invalid descriptor digest")
        descriptor_size = descriptor.get("size")
        if (
            isinstance(descriptor_size, bool)
            or not isinstance(descriptor_size, int)
            or descriptor_size <= 0
            or descriptor_size > _MAX_JSON_ARTIFACT_BYTES
        ):
            raise LumaError("BuildKit image index contains an invalid descriptor size")
        annotations = descriptor.get("annotations") or {}
        if not isinstance(annotations, dict):
            raise LumaError("BuildKit image index contains invalid descriptor annotations")
        if annotations.get(_BUILDKIT_REFERENCE_TYPE_ANNOTATION) == _BUILDKIT_ATTESTATION_MANIFEST_TYPE:
            referenced_digest = annotations.get(_BUILDKIT_REFERENCE_DIGEST_ANNOTATION)
            if not isinstance(referenced_digest, str) or not _SHA256_RE.fullmatch(referenced_digest):
                raise LumaError("BuildKit attestation descriptor has an invalid subject binding")
            if descriptor_size > _MAX_COMMAND_OUTPUT_BYTES:
                raise LumaError("BuildKit attestation manifest exceeds the artifact limit")
            attestation_descriptors.append((digest, referenced_digest, descriptor_size))
            continue
        platform = descriptor.get("platform")
        if (
            media_type in _OCI_MANIFEST_MEDIA_TYPES
            and isinstance(platform, dict)
            and platform.get("os") == "linux"
            and platform.get("architecture") == "amd64"
        ):
            runnable.append(digest)

    if len(runnable) != 1:
        raise LumaError("BuildKit image index must contain exactly one linux/amd64 manifest")
    runnable_digest = runnable[0]
    matching = [
        (digest, size)
        for digest, referenced_digest, size in attestation_descriptors
        if referenced_digest == runnable_digest
    ]
    if len(matching) != 1:
        raise LumaError("BuildKit attestation descriptor is not bound to the linux/amd64 manifest")
    return runnable_digest, matching[0][0], matching[0][1]


def _select_buildkit_provenance_layers(manifest: Dict[str, Any]) -> list[tuple[str, str, int]]:
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") not in _OCI_MANIFEST_MEDIA_TYPES:
        raise LumaError("BuildKit attestation manifest is invalid")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers or len(layers) > 128:
        raise LumaError("BuildKit attestation manifest contains an invalid layer list")
    selected: list[tuple[str, str, int]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            raise LumaError("BuildKit attestation manifest contains an invalid layer descriptor")
        if layer.get("mediaType") != _IN_TOTO_MEDIA_TYPE:
            continue
        digest = layer.get("digest")
        layer_size = layer.get("size")
        annotations = layer.get("annotations") or {}
        predicate_type = annotations.get(_IN_TOTO_PREDICATE_ANNOTATION) if isinstance(annotations, dict) else None
        if (
            isinstance(digest, str)
            and _SHA256_RE.fullmatch(digest)
            and not isinstance(layer_size, bool)
            and isinstance(layer_size, int)
            and 0 < layer_size <= _MAX_PROVENANCE_BYTES
            and isinstance(predicate_type, str)
            and predicate_type.startswith(_SLSA_PROVENANCE_PREFIX)
        ):
            selected.append((digest, predicate_type, layer_size))
    if not selected:
        raise LumaError("BuildKit attestation manifest contains no SLSA provenance layer")
    if sum(size for _digest, _predicate_type, size in selected) > _MAX_PROVENANCE_BYTES:
        raise LumaError("BuildKit provenance output exceeds the artifact limit")
    if len({digest for digest, _predicate_type, _size in selected}) != len(selected):
        raise LumaError("BuildKit attestation manifest contains duplicate provenance layers")
    return selected


def _validate_buildkit_statement(
    raw: bytes,
    *,
    expected_subject_digest: str,
    expected_predicate_type: str,
) -> Dict[str, Any]:
    if not raw or len(raw) > _MAX_PROVENANCE_BYTES:
        raise LumaError("BuildKit provenance statement is empty or too large")
    try:
        statement = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LumaError("BuildKit provenance statement is not valid JSON") from exc
    if not isinstance(statement, dict) or statement.get("_type") != _IN_TOTO_STATEMENT_TYPE:
        raise LumaError("BuildKit provenance is not an in-toto Statement")
    predicate_type = statement.get("predicateType")
    if (
        not isinstance(predicate_type, str)
        or not predicate_type.startswith(_SLSA_PROVENANCE_PREFIX)
        or predicate_type != expected_predicate_type
    ):
        raise LumaError("BuildKit provenance predicate type does not match its OCI descriptor")
    if not _SHA256_RE.fullmatch(expected_subject_digest):
        raise LumaError("BuildKit provenance subject digest is invalid")
    expected_hex = expected_subject_digest.split(":", 1)[1]
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not any(
        isinstance(subject, dict)
        and isinstance(subject.get("digest"), dict)
        and subject["digest"].get("sha256") == expected_hex
        for subject in subjects
    ):
        raise LumaError("BuildKit provenance is not bound to the linux/amd64 manifest digest")
    return statement


def _parse_resolved_digest(output: bytes) -> str:
    if not output or len(output) > 4096:
        raise LumaError("external image resolver returned an invalid digest")
    try:
        text = output.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise LumaError("external image resolver returned an invalid digest") from exc
    if "\n" in text or "\r" in text or not _SHA256_RE.fullmatch(text):
        raise LumaError("external image resolver returned an invalid digest")
    return text


def _external_resolution_statement(
    *,
    source_reference: str,
    immutable_reference: str,
    image_digest: str,
    platform: str,
    registry_host: str,
) -> bytes:
    """Create LAE-owned resolution evidence without claiming upstream provenance."""

    if not _SHA256_RE.fullmatch(image_digest):
        raise LumaError("external image resolution digest is invalid")
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": immutable_reference,
                "digest": {"sha256": image_digest.split(":", 1)[1]},
            }
        ],
        "predicateType": "https://itool.tech/lae/external-image-resolution/v1",
        "predicate": {
            "sourceReference": source_reference,
            "resolvedReference": immutable_reference,
            "platform": platform,
            "registryHost": registry_host,
            "resolver": {"name": "crane", "authentication": "anonymous"},
        },
    }
    return json.dumps(statement, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_cyclonedx(path: Path) -> None:
    body = _read_json_file(path, max_bytes=_MAX_JSON_ARTIFACT_BYTES, label="SBOM")
    if body.get("bomFormat") != "CycloneDX" or not isinstance(body.get("specVersion"), str):
        raise LumaError("SBOM output is not a CycloneDX document")


def _validate_scan_report(path: Path) -> None:
    body = _read_json_file(path, max_bytes=_MAX_JSON_ARTIFACT_BYTES, label="scan report")
    results = body.get("Results")
    if not isinstance(results, list):
        raise LumaError("scan output is not a Trivy JSON report")
    for result in results:
        if not isinstance(result, dict):
            raise LumaError("scan output contains an invalid result")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise LumaError("scan output contains an invalid vulnerability list")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise LumaError("scan output contains an invalid vulnerability")
            severity = vulnerability.get("Severity")
            if severity is not None and not isinstance(severity, str):
                raise LumaError("scan output contains an invalid vulnerability severity")


def _persist_artifact(path: Path, *, namespace: str, media_type: str) -> Dict[str, Any]:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise LumaError("build artifact is missing") from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise LumaError("build artifact is not a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > _MAX_JSON_ARTIFACT_BYTES:
        raise LumaError("build artifact size is invalid")
    digest = _sha256_file(path)
    hexadecimal = digest.split(":", 1)[1]
    root = snapshot_store_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_root = root.resolve()
    directory = resolved_root.joinpath(*namespace.split("/"), hexadecimal[:2])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_directory = directory.resolve()
    _assert_within_root(resolved_directory, resolved_root, "build artifact store path escapes its root")
    destination = resolved_directory / f"{hexadecimal}.json"
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or _sha256_file(destination) != digest:
            raise LumaError("build artifact store contains corrupt content")
    else:
        temporary = resolved_directory / f".{hexadecimal}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as output, path.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
    return {"digest": digest, "mediaType": media_type, "sizeBytes": int(file_stat.st_size)}


def _validated_result(
    request: Dict[str, Any],
    *,
    images: Dict[str, str],
    image_digests: Dict[str, str],
    sbom_digests: Dict[str, str],
    provenance_digests: Dict[str, str],
    scan_digests: Dict[str, str],
    artifacts: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    result = {
        "sourceSnapshotDigest": request["sourceSnapshotDigest"],
        "images": images,
        "imageDigests": image_digests,
        "sbomDigests": sbom_digests,
        "provenanceDigests": provenance_digests,
        "scanDigests": scan_digests,
        "artifacts": artifacts,
    }
    public_request = {
        "schemaVersion": request["schemaVersion"],
        "kind": "build-plan",
        "externalOperationId": request["externalOperationId"],
        "tenantRef": request["tenantRef"],
        "applicationRef": request["applicationRef"],
        "payload": {
            "sourceSnapshotId": request["sourceSnapshotId"],
            "sourceSnapshotDigest": request["sourceSnapshotDigest"],
            "signedBuildPlan": request["signedBuildPlan"],
            "credentialLeaseId": request["credentialLeaseId"],
            "limits": request["limits"],
        },
    }
    return sanitize_builder_task_result("build-plan", result, request=public_request)


def _runtime_prerequisites() -> _RuntimePrerequisites:
    tools = {name: shutil.which(name) for name in _REQUIRED_TOOLS}
    missing = sorted(name for name, path in tools.items() if not path)
    if missing:
        raise LumaError("builder build toolchain is incomplete")
    buildkit_addr = _rootless_buildkit_addr()
    buildctl = str(tools["buildctl"])
    probe = subprocess.run(
        [buildctl, "--addr", buildkit_addr, "debug", "workers", "--format", "{{json .}}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=5,
        start_new_session=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise LumaError("rootless BuildKit worker is unavailable")
    help_result = subprocess.run(
        [buildctl, "build", "--help"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=5,
        start_new_session=True,
    )
    # buildctl sends attestation settings through Dockerfile frontend options;
    # --attest is a buildx CLI flag and isn't part of buildctl.
    if help_result.returncode != 0 or b"--opt" not in help_result.stdout:
        raise LumaError("BuildKit does not support frontend options required for provenance attestations")
    version_commands = {
        "syft": [str(tools["syft"]), "version"],
        "trivy": [str(tools["trivy"]), "--version"],
        "cosign": [str(tools["cosign"]), "version"],
        "crane": [str(tools["crane"]), "version"],
    }
    for name, command in version_commands.items():
        probe = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            start_new_session=True,
        )
        if probe.returncode != 0:
            raise LumaError(f"builder {name} tool is not runnable")
    cache = _trivy_cache()
    return _RuntimePrerequisites(
        buildctl=buildctl,
        syft=str(tools["syft"]),
        trivy=str(tools["trivy"]),
        cosign=str(tools["cosign"]),
        crane=str(tools["crane"]),
        buildkit_addr=buildkit_addr,
        trivy_cache=cache,
    )


def _rootless_buildkit_addr() -> str:
    address = str(os.environ.get(BUILDKIT_ADDR_ENV) or "").strip()
    if not address.startswith("unix://") or any(char in address for char in ("\0", "\n", "\r", "?", "#")):
        raise LumaError("rootless BuildKit address must be an explicit unix socket")
    socket_path = Path(address[len("unix://") :])
    if not socket_path.is_absolute():
        raise LumaError("rootless BuildKit socket path must be absolute")
    try:
        socket_stat = socket_path.lstat()
    except FileNotFoundError as exc:
        raise LumaError("rootless BuildKit socket does not exist") from exc
    if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid == 0:
        raise LumaError("BuildKit endpoint is not a non-root-owned unix socket")
    match = re.match(r"^/run/user/([1-9][0-9]*)/", str(socket_path))
    if not match or int(match.group(1)) != int(socket_stat.st_uid):
        raise LumaError("BuildKit socket is not in its non-root runtime directory")
    peer_uid = _unix_socket_peer_uid(socket_path)
    if peer_uid == 0 or peer_uid != int(socket_stat.st_uid):
        raise LumaError("BuildKit daemon peer credentials do not match the non-root socket owner")
    return address


def _unix_socket_peer_uid(path: Path) -> int:
    """Authenticate the BuildKit daemon process behind a Unix socket.

    A non-root-owned pathname alone is insufficient because a rootful daemon
    can deliberately chown its listening socket.  Linux SO_PEERCRED binds the
    connected endpoint to the actual daemon UID at the kernel boundary.
    """

    if not hasattr(socket, "SO_PEERCRED"):
        raise LumaError("Linux SO_PEERCRED is unavailable for BuildKit verification")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(2.0)
        client.connect(str(path))
        raw = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError as exc:
        raise LumaError("BuildKit daemon peer credentials could not be verified") from exc
    finally:
        client.close()
    if len(raw) != struct.calcsize("3i"):
        raise LumaError("BuildKit daemon peer credentials are invalid")
    pid, uid, _gid = struct.unpack("3i", raw)
    if pid <= 0 or uid < 0:
        raise LumaError("BuildKit daemon peer credentials are invalid")
    return int(uid)


def _trivy_cache() -> Path:
    raw = str(os.environ.get(BUILDER_TRIVY_CACHE_ENV) or "").strip()
    cache = Path(raw) if raw else Path()
    if not raw or not cache.is_absolute() or cache.is_symlink() or not cache.is_dir():
        raise LumaError("Trivy cache directory is not configured")
    metadata_candidates = (cache / "db" / "metadata.json", cache / "metadata.json")
    if not any(path.is_file() and not path.is_symlink() for path in metadata_candidates):
        raise LumaError("Trivy vulnerability database is not ready")
    return cache


def _local_registry_policy() -> Dict[str, Any]:
    pull_host = _registry_host(os.environ.get(BUILDER_REGISTRY_PULL_HOST_ENV), BUILDER_REGISTRY_PULL_HOST_ENV)
    push_host = _registry_host(os.environ.get(BUILDER_REGISTRY_PUSH_HOST_ENV), BUILDER_REGISTRY_PUSH_HOST_ENV)
    insecure_raw = str(os.environ.get(BUILDER_REGISTRY_INSECURE_ENV) or "").strip()
    if insecure_raw not in {"0", "1"}:
        raise LumaError(f"{BUILDER_REGISTRY_INSECURE_ENV} must be 0 or 1")
    return {"pullHost": pull_host, "pushHost": push_host, "insecure": insecure_raw == "1"}


def _verify_runtime_registry_policy(registry: Dict[str, Any]) -> None:
    if str(os.environ.get(BUILDER_ALLOW_ANONYMOUS_REGISTRY_ENV) or "").strip() != "1":
        raise LumaError("anonymous build registry is not enabled on this node")
    local = _local_registry_policy()
    for field in ("pullHost", "pushHost", "insecure"):
        if registry.get(field) != local[field]:
            raise LumaError(f"build registry lease {field} does not match node policy")
    if registry.get("authMode") != "anonymous":
        raise LumaError("build registry credential broker is unavailable")
    if registry.get("externalRegistries") != _local_external_registry_allowlist():
        raise LumaError("external registry lease does not match node policy")


def _local_external_registry_allowlist() -> list[str]:
    raw = str(os.environ.get(BUILDER_EXTERNAL_REGISTRIES_ENV) or "").strip()
    if not raw:
        return []
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LumaError("builder external registry allowlist is invalid") from exc
    return _validate_external_registry_list(
        configured,
        label="builder external registry allowlist",
    )


def _validate_external_registry_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise LumaError(f"{label} must be a bounded array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item != item.strip().lower():
            raise LumaError(f"{label} contains an invalid host")
        try:
            parsed = parse_external_image_reference(f"{item}/lae/allowlist-probe:1")
        except LumaError as exc:
            raise LumaError(f"{label} contains an invalid host") from exc
        if parsed["registryHost"] != item:
            raise LumaError(f"{label} contains an invalid host")
        result.append(item)
    if len(result) != len(set(result)) or result != sorted(result):
        raise LumaError(f"{label} must contain sorted unique hosts")
    return result


def _registry_host(value: Any, label: str) -> str:
    host = str(value or "").strip().lower()
    if not host or "/" in host or "://" in host or not _REGISTRY_HOST_RE.fullmatch(host):
        raise LumaError(f"build registry {label} is invalid")
    if host.rsplit(":", 1)[-1].isdigit():
        port = int(host.rsplit(":", 1)[-1])
        if not 1 <= port <= 65535:
            raise LumaError(f"build registry {label} port is invalid")
    return host


def _reference(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _REFERENCE_RE.fullmatch(text):
        raise LumaError(f"build-plan lease {label} is invalid")
    return text


def _command_environment(docker_config: Path) -> Dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    environment["DOCKER_CONFIG"] = str(docker_config)
    environment["BUILDKIT_PROGRESS"] = "plain"
    return environment


def _syft_environment(
    command_environment: Mapping[str, str],
    *,
    insecure: bool,
) -> Dict[str, str]:
    environment = dict(command_environment)
    if insecure:
        # Syft 1.46 removed the former CLI flag. Its supported configuration
        # surface is environment-based and needs both settings for an HTTP
        # development/internal registry; skipping TLS verification alone still
        # makes Syft attempt HTTPS.
        environment["SYFT_REGISTRY_INSECURE_SKIP_TLS_VERIFY"] = "true"
        environment["SYFT_REGISTRY_INSECURE_USE_HTTP"] = "true"
    return environment


def _run_command(
    command: list[str],
    *,
    env: Mapping[str, str],
    timeout: int,
    cancel_event: Any,
    max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
) -> _CommandResult:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(env),
        start_new_session=True,
    )
    deadline = time.monotonic() + max(int(timeout), 1)
    output = bytearray()
    truncated = False
    try:
        while process.poll() is None:
            if _event_is_set(cancel_event):
                _terminate_process_group(process)
                raise BuilderTaskCanceled("builder task canceled")
            if time.monotonic() >= deadline:
                _terminate_process_group(process)
                raise BuilderTaskTimedOut("builder task timed out")
            stream = process.stdout
            if stream is None:
                time.sleep(0.05)
                continue
            ready, _, _ = select.select([stream], [], [], 0.1)
            if ready:
                chunk = os.read(stream.fileno(), 65536)
                if chunk:
                    output.extend(chunk)
                    if len(output) > max_output_bytes:
                        truncated = True
                        del output[: len(output) - max_output_bytes]
        if process.stdout is not None:
            remainder = process.stdout.read() or b""
            output.extend(remainder)
            if len(output) > max_output_bytes:
                truncated = True
                del output[: len(output) - max_output_bytes]
        return _CommandResult(int(process.returncode or 0), bytes(output), truncated=truncated)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        if process.stdout is not None:
            process.stdout.close()


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _read_json_file(path: Path, *, max_bytes: int, label: str) -> Dict[str, Any]:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise LumaError(f"{label} file is missing") from exc
    if path.is_symlink() or not path.is_file() or file_stat.st_size <= 0 or file_stat.st_size > max_bytes:
        raise LumaError(f"{label} file is invalid")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LumaError(f"{label} file is not valid JSON") from exc
    if not isinstance(body, dict):
        raise LumaError(f"{label} JSON must be an object")
    return body


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _work_parent() -> Path:
    raw = str(os.environ.get(BUILDER_WORK_ROOT_ENV) or "").strip()
    parent = Path(raw).expanduser() if raw else Path(tempfile.gettempdir())
    if not parent.is_absolute():
        raise LumaError(f"{BUILDER_WORK_ROOT_ENV} must be an absolute path")
    return parent


def _writable_parent(path: Path) -> None:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.is_dir() or not os.access(candidate, os.W_OK | os.X_OK):
        raise LumaError("builder storage parent is not writable")


def _require_free_disk(path: Path, budget: int) -> None:
    reserve_mib = int(str(os.environ.get("LUMA_BUILDER_FREE_DISK_RESERVE_MIB") or "512"))
    if reserve_mib < 0:
        raise LumaError("builder free disk reserve is invalid")
    if shutil.disk_usage(path).free < budget + reserve_mib * 1024 * 1024:
        raise LumaError("builder has insufficient free disk for the build task")


def _ensure_disk_budget(path: Path, limit: int) -> None:
    total = 0
    entries = 0
    for current, directory_names, file_names in os.walk(path, followlinks=False):
        entries += len(directory_names) + len(file_names)
        if entries > _MAX_SNAPSHOT_ENTRIES * 2:
            raise LumaError("builder workspace contains too many entries")
        for name in file_names:
            file_path = Path(current) / name
            try:
                file_stat = file_path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(file_stat.st_mode):
                total += max(int(file_stat.st_size), int(getattr(file_stat, "st_blocks", 0) or 0) * 512)
                if total > limit:
                    raise LumaError("builder workspace exceeded its disk budget")


def _assert_within_root(path: Path, root: Path, message: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LumaError(message) from exc


def _remaining_seconds(deadline: float) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise BuilderTaskTimedOut("builder task timed out")
    return remaining


def _raise_if_canceled(cancel_event: Any) -> None:
    if _event_is_set(cancel_event):
        raise BuilderTaskCanceled("builder task canceled")


def _event_is_set(cancel_event: Any) -> bool:
    return bool(cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set())


def _emit_phase(progress: Callable[[Dict[str, Any]], None] | None, key: str, phase: str, status: str) -> None:
    if progress is not None:
        progress({"type": "build", "line": f"{key} {phase} {status}"})
