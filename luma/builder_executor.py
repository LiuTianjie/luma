from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Mapping

from . import gitops
from .builder_tasks import parse_external_image_reference, validate_builder_task_request
from .errors import LumaError


BUILDER_ANALYZE_CAPABILITY = "builder-analyze-v1"
BUILDER_TASKS_ENABLED_ENV = "LUMA_BUILDER_TASKS_ENABLED"
BUILDER_ANALYZE_IMAGE_ENV = "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST"
BUILDER_ANALYZE_DOCKER_HOST_ENV = "LUMA_BUILDER_ANALYZE_DOCKER_HOST"
BUILDER_SNAPSHOT_ROOT_ENV = "LUMA_BUILDER_SNAPSHOT_ROOT"
BUILDER_WORK_ROOT_ENV = "LUMA_BUILDER_WORK_ROOT"
BUILDER_EXTERNAL_REGISTRIES_ENV = "LUMA_BUILDER_EXTERNAL_REGISTRIES_JSON"
DEFAULT_SNAPSHOT_ROOT = Path("/var/lib/luma/builder/snapshots")
DEFAULT_MAX_SOURCE_FILES = 200_000
MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
PROCESS_TERMINATE_GRACE_SECONDS = 3.0
RUNNER_CLEANUP_ATTEMPTS = 3
DISK_WATCH_INTERVAL_SECONDS = 0.25
DOCKER_PROBE_TIMEOUT_SECONDS = 5

_IMAGE_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_ROOTLESS_DOCKER_HOST_RE = re.compile(r"^unix://(/run/user/([1-9][0-9]*)/docker\.sock)$")
_RUNNER_ARTIFACT_FILES = {
    "evidence": "evidence.json",
    "deploymentPlan": "deployment-plan.json",
    "buildPlan": "build-plan-proposal.json",
}
_RUNNER_ARTIFACT_MEDIA_TYPES = {
    "evidence": "application/vnd.lae.evidence+json",
    "deploymentPlan": "application/vnd.lae.deployment-plan+json",
    "buildPlan": "application/vnd.lae.build-plan-proposal+json",
}
_PUBLIC_ARTIFACT_FILES = {
    "evidence": "evidence.json",
    "deploymentPlan": "deployment-plan.json",
    "buildPlan": "build-plan-candidate.json",
}
_PUBLIC_ARTIFACT_MEDIA_TYPES = {
    "evidence": "application/vnd.lae.evidence+json",
    "deploymentPlan": "application/vnd.lae.deployment-plan+json",
    "buildPlan": "application/vnd.lae.build-plan-candidate+json",
}
_BUILDER_ANALYZE_CLEANUP_HEALTHY = True


@dataclass
class BuilderArtifactExport:
    stream: Any = field(repr=False)
    name: str
    digest: str
    media_type: str
    size_bytes: int

    def close(self) -> None:
        self.stream.close()


class BuilderTaskCanceled(LumaError):
    """Raised when Control has requested cancellation of a running task."""


class BuilderTaskTimedOut(LumaError):
    """Raised when a Builder task exhausts its end-to-end execution budget."""


class BuilderCleanupFailed(LumaError):
    """Raised when a runner container cannot be proven absent after execution."""


def builder_analyze_available(os_name: str) -> bool:
    """Return whether this node may truthfully advertise analyze-source.

    The feature is deliberately opt-in.  In addition to Linux, git and Docker,
    a node-local immutable runner allowlist is required; accepting the image
    reference from the task alone would turn the scoped API into arbitrary code
    execution on the builder.
    """

    if str(os.environ.get(BUILDER_TASKS_ENABLED_ENV) or "").strip() != "1":
        return False
    if not _BUILDER_ANALYZE_CLEANUP_HEALTHY:
        return False
    if str(os_name or "").lower() != "linux":
        return False
    pinned_image = str(os.environ.get(BUILDER_ANALYZE_IMAGE_ENV) or "").strip()
    if not _IMAGE_DIGEST_RE.fullmatch(pinned_image):
        return False
    try:
        _require_git_runtime()
        _require_crane_runtime()
        _local_external_registry_allowlist()
        _rootless_docker_runtime(pinned_image)
        for root in (snapshot_store_root(), _work_parent()):
            existing_parent = root
            while not existing_parent.exists() and existing_parent != existing_parent.parent:
                existing_parent = existing_parent.parent
            if not existing_parent.is_dir() or not os.access(existing_parent, os.W_OK | os.X_OK):
                return False
        return True
    except (LumaError, OSError, subprocess.SubprocessError, ValueError):
        return False


def snapshot_store_root() -> Path:
    raw = str(os.environ.get(BUILDER_SNAPSHOT_ROOT_ENV) or "").strip()
    root = Path(raw).expanduser() if raw else DEFAULT_SNAPSHOT_ROOT
    if not root.is_absolute():
        raise LumaError(f"{BUILDER_SNAPSHOT_ROOT_ENV} must be an absolute path")
    return root


def open_builder_analysis_artifact(
    payload: Dict[str, Any], *, cancel_event: Any = None
) -> BuilderArtifactExport:
    """Open one content-addressed analyzer artifact without trusting a path."""

    if not isinstance(payload, dict) or set(payload) != {
        "leaseId",
        "builderTaskId",
        "artifact",
    }:
        raise LumaError("builder artifact export payload is invalid")
    lease_id = str(payload.get("leaseId") or "")
    builder_task_id = str(payload.get("builderTaskId") or "")
    if not re.fullmatch(r"artdl_[A-Za-z0-9_-]{32,96}", lease_id):
        raise LumaError("builder artifact export lease is invalid")
    if not _REFERENCE_RE.fullmatch(builder_task_id):
        raise LumaError("builder artifact export task binding is invalid")
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "name",
        "digest",
        "mediaType",
        "sizeBytes",
    }:
        raise LumaError("builder artifact export descriptor is invalid")
    name = str(artifact.get("name") or "")
    digest = str(artifact.get("digest") or "")
    media_type = str(artifact.get("mediaType") or "")
    size_bytes = artifact.get("sizeBytes")
    if (
        name not in _PUBLIC_ARTIFACT_FILES
        or media_type != _PUBLIC_ARTIFACT_MEDIA_TYPES.get(name)
        or not _SHA256_RE.fullmatch(digest)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 1 <= size_bytes <= MAX_ARTIFACT_BYTES
    ):
        raise LumaError("builder artifact export descriptor is invalid")
    hexadecimal = digest.removeprefix("sha256:")
    root = snapshot_store_root().resolve()
    path = (
        root
        / "artifacts"
        / _artifact_store_name(name)
        / "sha256"
        / hexadecimal[:2]
        / f"{hexadecimal}.json"
    )
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LumaError("builder artifact is unavailable") from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(resolved, flags)
    stream = os.fdopen(fd, "rb")
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size != size_bytes:
            raise LumaError("builder artifact descriptor changed")
        hasher = hashlib.sha256()
        while True:
            _raise_if_canceled(cancel_event)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
        if not hmac.compare_digest("sha256:" + hasher.hexdigest(), digest):
            raise LumaError("builder artifact descriptor changed")
        stream.seek(0)
        return BuilderArtifactExport(
            stream=stream,
            name=name,
            digest=digest,
            media_type=media_type,
            size_bytes=size_bytes,
        )
    except BaseException:
        stream.close()
        raise


def analyze_source(
    payload: Dict[str, Any],
    *,
    cancel_event: Any = None,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Execute one LAE source analysis in the builder sandbox.

    Credentials may be injected into the leased payload as ``gitToken``.  They
    are used only through an ephemeral askpass directory and are never copied to
    metadata, process argv, task results, snapshots, or progress events.
    """

    normalized = _validate_analyze_payload(payload)
    _raise_if_canceled(cancel_event)
    deadline = time.monotonic() + normalized["limits"]["timeoutSeconds"]
    disk_budget_bytes = normalized["limits"]["diskMiB"] * 1024 * 1024
    work_parent = _work_parent()
    work_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_free_disk(work_parent, disk_budget_bytes)

    with tempfile.TemporaryDirectory(prefix="luma-analyze-", dir=str(work_parent)) as temporary:
        work = Path(temporary)
        checkout = work / "checkout"
        source_object = work / "source.object"
        snapshot_tar = work / "source.tar"
        materialized = work / "workspace"
        input_dir = work / "input"
        output_dir = work / "output"

        _emit_phase(progress, "source_fetch", "running")
        source_reference = normalized["sourceRef"]
        if source_reference.get("kind") == "object":
            _download_object_source(
                normalized["objectUrl"],
                source_object,
                expected_digest=source_reference["digest"],
                expected_size=source_reference["sizeBytes"],
                cancel_event=cancel_event,
                timeout=_remaining_seconds(deadline),
                workspace_root=work,
                disk_limit_bytes=disk_budget_bytes,
            )
            _materialize_object_source(
                source_object,
                checkout,
                media_type=source_reference["mediaType"],
                cancel_event=cancel_event,
                workspace_root=work,
                disk_limit_bytes=disk_budget_bytes,
            )
            resolved_commit = source_reference["digest"].removeprefix("sha256:")
            source_root = checkout
        else:
            _clone_source(
                source_reference["repository"],
                checkout,
                ref=source_reference.get("ref") or "",
                git_token=normalized.get("gitToken") or "",
                git_username=normalized.get("gitUsername") or "",
                cancel_event=cancel_event,
                timeout=_remaining_seconds(deadline),
                disk_watch_path=work,
                disk_limit_bytes=disk_budget_bytes,
            )
            resolved_commit = gitops.head_commit_full(checkout)
            source_root = _source_subdirectory(
                checkout, source_reference.get("subdirectory") or ""
            )
        _ensure_directory_budget(work, disk_budget_bytes)
        _raise_if_canceled(cancel_event)
        if not _FULL_COMMIT_RE.fullmatch(resolved_commit):
            raise LumaError("source resolved an invalid immutable object id")
        _emit_phase(progress, "source_fetch", "succeeded")

        _emit_phase(progress, "source_snapshot", "running")
        source_tree_digest, snapshot_digest, _source_bytes = _create_deterministic_snapshot(
            source_root,
            snapshot_tar,
            disk_limit_bytes=disk_budget_bytes,
            cancel_event=cancel_event,
            workspace_root=work,
        )
        source_snapshot_id = _snapshot_id(snapshot_digest, builder_task_id=normalized["builderTaskId"])
        _persist_content_file(
            snapshot_store_root(),
            namespace="sha256",
            digest=snapshot_digest,
            source=snapshot_tar,
            suffix=".tar",
        )
        # The immutable tar is already durable.  Drop the checkout (including
        # its Git object database) before materializing the runner view so the
        # task does not keep three complete source copies at once.
        shutil.rmtree(checkout)
        _ensure_projected_budget(work, snapshot_tar.stat().st_size, disk_budget_bytes)
        _materialize_snapshot(
            snapshot_tar,
            materialized,
            workspace_root=work,
            disk_limit_bytes=disk_budget_bytes,
        )
        snapshot_tar.unlink()
        _ensure_directory_budget(work, disk_budget_bytes)
        _emit_phase(progress, "source_snapshot", "succeeded")

        metadata = {
            "schemaVersion": "lae.agent-analysis-metadata/v1",
            "builderTaskId": normalized["builderTaskId"],
            "externalOperationId": normalized["externalOperationId"],
            "tenantRef": normalized["tenantRef"],
            "applicationRef": normalized["applicationRef"],
            "resolvedCommit": resolved_commit,
            "sourceTreeDigest": source_tree_digest,
            "sourceSnapshotId": source_snapshot_id,
            "sourceSnapshotDigest": snapshot_digest,
            "policyVersion": normalized["policyVersion"],
            "agentImageDigest": normalized["agentImageDigest"],
        }
        # Host ownership is handed to the verified rootless Docker peer only
        # immediately before execution.  Until then every task path remains
        # root-private.  The bind preparation step makes source/input 0500/0400
        # and output 0700; nothing is made world-readable or world-writable.
        input_dir.mkdir(mode=0o700)
        _write_json_file(input_dir / "metadata.json", metadata, mode=0o600)
        output_dir.mkdir(mode=0o700)

        _emit_phase(progress, "agent_analysis", "running")
        _run_analyzer_container(
            image=normalized["agentImageDigest"],
            builder_task_id=normalized["builderTaskId"],
            source_dir=materialized,
            input_dir=input_dir,
            output_dir=output_dir,
            limits=normalized["limits"],
            cancel_event=cancel_event,
            timeout=_remaining_seconds(deadline),
        )
        _raise_if_canceled(cancel_event)
        _ensure_directory_budget(work, disk_budget_bytes)
        validated = _validate_runner_output(output_dir, metadata)
        has_external_images = bool(validated["buildPlanProposal"].get("externalImages"))
        if has_external_images:
            _emit_phase(progress, "external_resolution", "running")
        candidate = _resolve_build_plan_proposal(
            validated["buildPlanProposal"],
            allowed_registries=normalized["externalRegistries"],
            cancel_event=cancel_event,
            timeout=_remaining_seconds(deadline),
        )
        candidate_path = output_dir / _PUBLIC_ARTIFACT_FILES["buildPlan"]
        candidate_bytes = _canonical_json_bytes(candidate)
        _write_private_bytes(candidate_path, candidate_bytes)
        _validate_enriched_build_plan_candidate(candidate, metadata)
        validated["artifacts"]["buildPlan"] = {
            "digest": "sha256:" + hashlib.sha256(candidate_bytes).hexdigest(),
            "sizeBytes": len(candidate_bytes),
        }
        if has_external_images:
            _emit_phase(progress, "external_resolution", "succeeded")
        artifact_summary: Dict[str, Any] = {}
        for name, artifact in validated["artifacts"].items():
            artifact_path = output_dir / _PUBLIC_ARTIFACT_FILES[name]
            _persist_content_file(
                snapshot_store_root(),
                namespace=f"artifacts/{_artifact_store_name(name)}/sha256",
                digest=artifact["digest"],
                source=artifact_path,
                suffix=".json",
            )
            artifact_summary[name] = {
                "digest": artifact["digest"],
                "mediaType": _PUBLIC_ARTIFACT_MEDIA_TYPES[name],
                "sizeBytes": artifact["sizeBytes"],
            }
        _emit_phase(progress, "agent_analysis", "succeeded")

        # This closed result contains no stdout, repository URL, host path,
        # runner-selected message, or other unconstrained text.
        return {
            "resolvedCommit": resolved_commit,
            "sourceTreeDigest": source_tree_digest,
            "sourceSnapshotId": source_snapshot_id,
            "sourceSnapshotDigest": snapshot_digest,
            "deploymentPlanDigest": artifact_summary["deploymentPlan"]["digest"],
            "buildPlanDigest": artifact_summary["buildPlan"]["digest"],
            "evidenceDigest": artifact_summary["evidence"]["digest"],
            "policyVersion": normalized["policyVersion"],
            "agentImageDigest": normalized["agentImageDigest"],
            "artifacts": artifact_summary,
        }


def _validate_analyze_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise LumaError("analyze-source payload must be an object")
    result: Dict[str, Any] = {}
    for key in ("builderTaskId", "externalOperationId", "tenantRef", "applicationRef", "credentialLeaseId", "policyVersion"):
        value = str(payload.get(key) or "").strip()
        if not _REFERENCE_RE.fullmatch(value):
            raise LumaError(f"analyze-source {key} is invalid")
        result[key] = value

    source = payload.get("sourceRef")
    if not isinstance(source, dict):
        raise LumaError("analyze-source sourceRef must be an object")
    if source.get("kind") == "object":
        if set(source) != {"kind", "digest", "mediaType", "sizeBytes"}:
            raise LumaError("analyze-source object sourceRef is invalid")
        digest = str(source.get("digest") or "")
        media_type = str(source.get("mediaType") or "")
        size_bytes = source.get("sizeBytes")
        if (
            not _SHA256_RE.fullmatch(digest)
            or media_type not in {"text/html", "application/zip"}
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= 536_870_912
        ):
            raise LumaError("analyze-source object sourceRef is invalid")
        result["sourceRef"] = {
            "kind": "object",
            "digest": digest,
            "mediaType": media_type,
            "sizeBytes": size_bytes,
        }
        allowed_host = _normalize_object_allowed_host(
            str(payload.get("objectAllowedHost") or "")
        )
        result["objectUrl"] = _normalize_object_url(
            str(payload.get("objectUrl") or ""),
            allowed_host=allowed_host,
        )
    else:
        repository = _normalize_repository(str(source.get("repository") or ""))
        ref = str(source.get("ref") or "").strip()
        if len(ref) > 512 or any(char in ref for char in ("\0", "\n", "\r")):
            raise LumaError("analyze-source sourceRef.ref is invalid")
        subdirectory = _normalize_subdirectory(str(source.get("subdirectory") or ""))
        result["sourceRef"] = {
            "repository": repository,
            **({"ref": ref} if ref else {}),
            **({"subdirectory": subdirectory} if subdirectory else {}),
        }
        if "objectUrl" in payload or "objectAllowedHost" in payload:
            raise LumaError("analyze-source object URL is not valid for Git")

    image = str(payload.get("agentImageDigest") or "").strip()
    if not _IMAGE_DIGEST_RE.fullmatch(image):
        raise LumaError("analyze-source agentImageDigest must be an immutable sha256 image reference")
    pinned = str(os.environ.get(BUILDER_ANALYZE_IMAGE_ENV) or "").strip()
    if not _IMAGE_DIGEST_RE.fullmatch(pinned):
        raise LumaError(f"builder runner allowlist is not configured in {BUILDER_ANALYZE_IMAGE_ENV}")
    if image != pinned:
        raise LumaError("analyze-source agentImageDigest is not allowlisted on this builder")
    result["agentImageDigest"] = image

    limits = payload.get("limits")
    if not isinstance(limits, dict):
        raise LumaError("analyze-source limits must be an object")
    cpu = _bounded_float(limits.get("cpu"), "limits.cpu", 0.1, 32.0)
    memory = _bounded_int(limits.get("memoryMiB"), "limits.memoryMiB", 128, 131_072)
    disk = _bounded_int(limits.get("diskMiB"), "limits.diskMiB", 256, 1_048_576)
    timeout = _bounded_int(limits.get("timeoutSeconds"), "limits.timeoutSeconds", 10, 14_400)
    result["limits"] = {"cpu": cpu, "memoryMiB": memory, "diskMiB": disk, "timeoutSeconds": timeout}

    leased_external_registries = _validate_external_registry_list(
        payload.get("externalRegistries"),
        label="analyze-source externalRegistries",
    )
    if leased_external_registries != _local_external_registry_allowlist():
        raise LumaError("analyze-source external registry lease does not match node policy")
    result["externalRegistries"] = leased_external_registries

    # Explicit credentials are accepted only as ephemeral lease enrichment.
    # The public Builder Task schema rejects them, and this function never
    # copies them into the result or metadata.
    git_token = str(payload.get("gitToken") or "")
    git_username = str(payload.get("gitUsername") or "")
    if git_token:
        if result["sourceRef"].get("kind") == "object":
            raise LumaError("analyze-source Git credential is not valid for object source")
        if len(git_token) > 8192 or any(char in git_token for char in ("\0", "\n", "\r")):
            raise LumaError("analyze-source leased Git credential is invalid")
        if len(git_username) > 256 or any(char in git_username for char in ("\0", "\n", "\r")):
            raise LumaError("analyze-source leased Git username is invalid")
        result["gitToken"] = git_token
        result["gitUsername"] = git_username or "x-access-token"
    return result


def _normalize_repository(value: str) -> str:
    repository = value.strip()
    if not repository or any(char in repository for char in ("\0", "\n", "\r")):
        raise LumaError("analyze-source repository is required")
    if not repository.startswith(("https://", "ssh://", "git@")):
        repository = f"https://{repository.lstrip('/')}"
    if not repository.startswith("https://"):
        raise LumaError("analyze-source currently supports HTTPS Git repositories only")
    parsed = urllib.parse.urlparse(repository)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LumaError("analyze-source repository must be an HTTPS URL without inline credentials")
    if len(repository) > 2048:
        raise LumaError("analyze-source repository is too long")
    return repository


def _normalize_subdirectory(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized in {"", "."}:
        return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\0" in normalized:
        raise LumaError("analyze-source subdirectory must stay within the repository")
    return path.as_posix()


def _normalize_object_allowed_host(value: str) -> str:
    host = value.strip()
    if (
        not host
        or host != host.lower()
        or len(host) > 253
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
            host,
        )
        is None
    ):
        raise LumaError("analyze-source leased object host is invalid")
    return host


def _normalize_object_url(value: str, *, allowed_host: str) -> str:
    if (
        not value
        or len(value) > 8192
        or any(character in value for character in ("\0", "\n", "\r", " ", "\t"))
    ):
        raise LumaError("analyze-source leased object URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise LumaError("analyze-source leased object URL is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.hostname != allowed_host
        or port is not None and not 1 <= port <= 65535
    ):
        raise LumaError("analyze-source leased object URL is invalid")
    return value


class _RejectObjectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _download_object_source(
    url: str,
    destination: Path,
    *,
    expected_digest: str,
    expected_size: int,
    cancel_event: Any,
    timeout: int,
    workspace_root: Path,
    disk_limit_bytes: int,
) -> None:
    if expected_size > disk_limit_bytes:
        raise LumaError("uploaded source exceeds the builder task disk limit")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RejectObjectRedirects()
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/octet-stream", "User-Agent": "luma-builder/1"},
    )
    try:
        response = opener.open(request, timeout=max(1, int(timeout)))
    except Exception:
        raise LumaError("uploaded source download failed") from None
    digest = hashlib.sha256()
    copied = 0
    try:
        try:
            raw_length = str(response.headers.get("Content-Length") or "")
            if raw_length and raw_length != str(expected_size):
                raise LumaError("uploaded source descriptor changed")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(destination, flags, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as output:
                    while True:
                        _raise_if_canceled(cancel_event)
                        chunk = response.read(
                            min(1024 * 1024, expected_size - copied + 1)
                        )
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > expected_size:
                            raise LumaError("uploaded source descriptor changed")
                        digest.update(chunk)
                        output.write(chunk)
                        _ensure_directory_budget(workspace_root, disk_limit_bytes)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(fd)
        except (BuilderTaskCanceled, BuilderTaskTimedOut, LumaError):
            raise
        except Exception:
            # Transport exceptions may embed the signed URL.  Collapse them to
            # a fixed message before they cross the worker boundary.
            raise LumaError("uploaded source download failed") from None
    finally:
        try:
            response.close()
        except Exception:
            pass
    if copied != expected_size or not hmac.compare_digest(
        "sha256:" + digest.hexdigest(), expected_digest
    ):
        raise LumaError("uploaded source descriptor changed")


def _materialize_object_source(
    source: Path,
    destination: Path,
    *,
    media_type: str,
    cancel_event: Any,
    workspace_root: Path,
    disk_limit_bytes: int,
) -> None:
    destination.mkdir(mode=0o700)
    if media_type == "text/html":
        target = destination / "index.html"
        os.replace(source, target)
        target.chmod(0o644)
        return
    if media_type != "application/zip":
        raise LumaError("uploaded source media type is unsupported")
    root = destination.resolve()
    seen: set[str] = set()
    expanded = 0
    try:
        archive = zipfile.ZipFile(source, mode="r")
    except (OSError, zipfile.BadZipFile):
        raise LumaError("uploaded source archive is invalid") from None
    with archive:
        members = archive.infolist()
        max_files = _bounded_env_int(
            "LUMA_BUILDER_MAX_SOURCE_FILES", DEFAULT_MAX_SOURCE_FILES, 1, 2_000_000
        )
        if len(members) > max_files:
            raise LumaError("uploaded source archive contains too many entries")
        for member in members:
            _raise_if_canceled(cancel_event)
            name = member.filename
            if (
                not name
                or "\0" in name
                or "\\" in name
                or member.flag_bits & 0x1
            ):
                raise LumaError("uploaded source archive contains an unsafe entry")
            relative = PurePosixPath(name.rstrip("/"))
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise LumaError("uploaded source archive contains an unsafe path")
            folded = relative.as_posix().casefold()
            if folded in seen:
                raise LumaError("uploaded source archive contains duplicate paths")
            seen.add(folded)
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            is_directory = member.is_dir()
            if file_type and file_type not in {
                stat.S_IFDIR if is_directory else stat.S_IFREG
            }:
                raise LumaError("uploaded source archive contains a special file")
            target = destination.joinpath(*relative.parts)
            try:
                target.parent.resolve().relative_to(root)
            except ValueError:
                raise LumaError("uploaded source archive escapes the workspace") from None
            if is_directory:
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            expanded += int(member.file_size)
            if expanded > disk_limit_bytes:
                raise LumaError("uploaded source archive exceeds the disk limit")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags, 0o644)
            copied = 0
            try:
                with archive.open(member, mode="r") as input_stream, os.fdopen(
                    fd, "wb", closefd=False
                ) as output:
                    while True:
                        _raise_if_canceled(cancel_event)
                        chunk = input_stream.read(
                            min(1024 * 1024, int(member.file_size) - copied + 1)
                        )
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > member.file_size:
                            raise LumaError("uploaded source archive size changed")
                        output.write(chunk)
                        _ensure_directory_budget(workspace_root, disk_limit_bytes)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(fd)
            if copied != member.file_size:
                raise LumaError("uploaded source archive size changed")
    source.unlink()
    if not (destination / "index.html").is_file():
        raise LumaError("uploaded source archive is missing index.html")


def _clone_source(
    repository: str,
    destination: Path,
    *,
    ref: str,
    git_token: str,
    git_username: str,
    cancel_event: Any,
    timeout: int,
    disk_watch_path: Path | None = None,
    disk_limit_bytes: int | None = None,
) -> None:
    environment = dict(os.environ)
    for name in list(environment):
        if name in {
            "GIT_ASKPASS",
            "GIT_ASKPASS_REQUIRE",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_TRACE",
            "GIT_TRACE2",
            "GIT_TRACE_PACKET",
            "GIT_TRACE_PERFORMANCE",
            "GIT_TRACE_SETUP",
            "GIT_TRACE_SHALLOW",
            "GIT_TRACE_CURL",
            "GIT_TRACE_CURL_NO_DATA",
            "GIT_CURL_VERBOSE",
            "GIT_CONFIG_COUNT",
            "SSH_ASKPASS",
        } or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    command = [
        _require_git_runtime(),
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "clone",
        "--depth",
        "1",
        "--no-tags",
        "--single-branch",
    ]
    if ref:
        command += ["--branch", ref]
    command += ["--", repository, str(destination)]

    with tempfile.TemporaryDirectory(prefix="luma-git-auth-") as auth_tmp:
        environment["HOME"] = auth_tmp
        environment["XDG_CONFIG_HOME"] = str(Path(auth_tmp) / "xdg")
        if git_token:
            askpass, username_file, password_file = gitops._write_git_askpass(
                Path(auth_tmp),
                username=git_username or "x-access-token",
                token=git_token,
            )
            environment.update(
                {
                    "GIT_ASKPASS": str(askpass),
                    "GIT_ASKPASS_REQUIRE": "force",
                    "LUMA_GIT_USERNAME_FILE": str(username_file),
                    "LUMA_GIT_PASSWORD_FILE": str(password_file),
                }
            )
        result = _run_cancellable_process(
            command,
            env=environment,
            timeout=timeout,
            cancel_event=cancel_event,
            redact_values=(git_token,),
            disk_watch_path=disk_watch_path,
            disk_limit_bytes=disk_limit_bytes,
        )
    if result.returncode != 0:
        detail = _safe_failure_category(result.output)
        raise LumaError(f"git clone failed ({detail})")


class _ProcessResult:
    def __init__(self, returncode: int, output: str):
        self.returncode = int(returncode)
        self.output = str(output or "")


def _run_cancellable_process(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: int,
    cancel_event: Any,
    redact_values: Iterable[str] = (),
    disk_watch_path: Path | None = None,
    disk_limit_bytes: int | None = None,
) -> _ProcessResult:
    _raise_if_canceled(cancel_event)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise LumaError(f"required command is not installed: {command[0]}") from exc

    deadline = time.monotonic() + max(int(timeout), 1)
    next_disk_check = time.monotonic()
    output = bytearray()
    try:
        while process.poll() is None:
            if _event_is_set(cancel_event):
                _terminate_process_group(process)
                raise BuilderTaskCanceled("builder task canceled")
            if time.monotonic() >= deadline:
                _terminate_process_group(process)
                raise BuilderTaskTimedOut("builder task timed out")
            if disk_watch_path is not None and disk_limit_bytes is not None and time.monotonic() >= next_disk_check:
                if _directory_usage_bytes(disk_watch_path, stop_after=disk_limit_bytes) > disk_limit_bytes:
                    _terminate_process_group(process)
                    raise LumaError("builder task workspace exceeded its disk budget")
                next_disk_check = time.monotonic() + DISK_WATCH_INTERVAL_SECONDS
            stream = process.stdout
            if stream is None:
                time.sleep(0.05)
                continue
            ready, _, _ = select.select([stream], [], [], 0.1)
            if ready:
                chunk = os.read(stream.fileno(), 65_536)
                if chunk:
                    output.extend(chunk)
                    if len(output) > MAX_PROCESS_OUTPUT_BYTES:
                        del output[: len(output) - MAX_PROCESS_OUTPUT_BYTES]
        stream = process.stdout
        if stream is not None:
            remainder = stream.read() or b""
            output.extend(remainder)
            if len(output) > MAX_PROCESS_OUTPUT_BYTES:
                del output[: len(output) - MAX_PROCESS_OUTPUT_BYTES]
        decoded = output.decode("utf-8", errors="replace")
        for secret in redact_values:
            if secret:
                decoded = decoded.replace(secret, "***")
        return _ProcessResult(process.returncode or 0, decoded)
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
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
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


def _source_subdirectory(checkout: Path, subdirectory: str) -> Path:
    checkout_root = checkout.resolve()
    source = (checkout / subdirectory).resolve() if subdirectory else checkout_root
    try:
        source.relative_to(checkout_root)
    except ValueError as exc:
        raise LumaError("analyze-source subdirectory escapes the repository") from exc
    if not source.is_dir():
        raise LumaError("analyze-source subdirectory does not exist or is not a directory")
    return source


def _create_deterministic_snapshot(
    source_root: Path,
    destination: Path,
    *,
    disk_limit_bytes: int,
    cancel_event: Any,
    workspace_root: Path | None = None,
) -> tuple[str, str, int]:
    entries = list(_source_entries(source_root, cancel_event=cancel_event))
    max_files = _bounded_env_int("LUMA_BUILDER_MAX_SOURCE_FILES", DEFAULT_MAX_SOURCE_FILES, 1, 2_000_000)
    if len(entries) > max_files:
        raise LumaError(f"source tree exceeds the {max_files} entry limit")
    estimated_tar_bytes = 10_240
    for _relative, _path, file_stat in entries:
        estimated_tar_bytes += 4096
        if stat.S_ISREG(file_stat.st_mode):
            estimated_tar_bytes += ((int(file_stat.st_size) + 511) // 512) * 512
    if workspace_root is not None:
        _ensure_projected_budget(workspace_root, estimated_tar_bytes, disk_limit_bytes)

    source_bytes = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw_tar:
        with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for relative, source_path, file_stat in entries:
                _raise_if_canceled(cancel_event)
                kind, mode, size = _source_entry_metadata(source_path, file_stat)
                source_bytes += size
                if source_bytes > disk_limit_bytes:
                    raise LumaError("source tree exceeds the builder task disk limit")
                info = tarfile.TarInfo(relative)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                info.mode = mode
                info.pax_headers = {}
                if kind == "directory":
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    archive.addfile(info)
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(source_path)
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.type = tarfile.REGTYPE
                    info.size = size
                    with source_path.open("rb") as source_file:
                        archive.addfile(info, source_file)
                if workspace_root is not None:
                    _ensure_directory_budget(workspace_root, disk_limit_bytes)
        raw_tar.flush()
        os.fsync(raw_tar.fileno())
    if destination.stat().st_size > disk_limit_bytes:
        raise LumaError("source snapshot exceeds the builder task disk limit")
    # Derive the reported tree digest from the finished tar, not a second view
    # of the checkout.  This closes the hash/archive TOCTOU gap and binds every
    # subsequent plan to the exact bytes that were persisted and materialized.
    archived_tree_digest, archived_source_bytes = _source_tree_digest_from_snapshot(
        destination,
        cancel_event=cancel_event,
    )
    return archived_tree_digest, _sha256_file(destination), archived_source_bytes


def _source_tree_digest_from_snapshot(snapshot: Path, *, cancel_event: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    source_bytes = 0
    with tarfile.open(snapshot, mode="r:") as archive:
        for member in archive.getmembers():
            _raise_if_canceled(cancel_event)
            if member.isdir():
                kind, payload_digest, size = "directory", "", 0
            elif member.issym():
                kind = "symlink"
                size = 0
                payload_digest = hashlib.sha256(member.linkname.encode("utf-8", errors="surrogateescape")).hexdigest()
            elif member.isfile():
                kind = "file"
                size = int(member.size)
                source_bytes += size
                file_digest = hashlib.sha256()
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise LumaError("source snapshot contains an unreadable file")
                with extracted:
                    for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                        _raise_if_canceled(cancel_event)
                        file_digest.update(chunk)
                payload_digest = file_digest.hexdigest()
            else:
                raise LumaError("source snapshot contains an unsupported entry")
            _update_tree_hash(digest, member.name, kind, int(member.mode), size, payload_digest)
    return f"sha256:{digest.hexdigest()}", source_bytes


def _source_entries(source_root: Path, *, cancel_event: Any) -> Iterable[tuple[str, Path, os.stat_result]]:
    root = source_root.resolve()
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        _raise_if_canceled(cancel_event)
        current_path = Path(current)
        if current_path == root:
            directory_names[:] = [name for name in directory_names if name != ".git"]
            file_names = [name for name in file_names if name != ".git"]
        directory_names.sort()
        file_names.sort()
        symlink_directories: list[str] = []
        for name in list(directory_names):
            path = current_path / name
            if path.is_symlink():
                symlink_directories.append(name)
                directory_names.remove(name)
        for name in sorted(directory_names + symlink_directories + file_names):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            file_stat = path.lstat()
            if not (stat.S_ISDIR(file_stat.st_mode) or stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode)):
                raise LumaError("source tree contains an unsupported file type")
            yield relative, path, file_stat


def _source_entry_metadata(_path: Path, file_stat: os.stat_result) -> tuple[str, int, int]:
    if stat.S_ISDIR(file_stat.st_mode):
        return "directory", 0o755, 0
    if stat.S_ISLNK(file_stat.st_mode):
        return "symlink", 0o777, 0
    mode = 0o755 if file_stat.st_mode & 0o111 else 0o644
    return "file", mode, int(file_stat.st_size)


def _update_tree_hash(digest: Any, path: str, kind: str, mode: int, size: int, payload_digest: str) -> None:
    record = json.dumps(
        {"path": path, "kind": kind, "mode": mode, "size": size, "digest": payload_digest},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest.update(len(record).to_bytes(8, "big"))
    digest.update(record)


def _materialize_snapshot(
    snapshot: Path,
    destination: Path,
    *,
    workspace_root: Path | None = None,
    disk_limit_bytes: int | None = None,
) -> None:
    destination.mkdir(mode=0o755)
    root = destination.resolve()
    with tarfile.open(snapshot, mode="r:") as archive:
        members = archive.getmembers()
        symlink_paths: set[PurePosixPath] = set()
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise LumaError("source snapshot contains an unsafe path")
            if any(parent in symlink_paths for parent in relative.parents):
                raise LumaError("source snapshot attempts to write through a symlink")
            target = destination.joinpath(*relative.parts)
            try:
                target.parent.resolve().relative_to(root)
            except ValueError as exc:
                raise LumaError("source snapshot path escapes the workspace") from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                os.symlink(member.linkname, target)
                symlink_paths.add(relative)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise LumaError("source snapshot contains an unreadable file")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(target, flags, member.mode & 0o777)
                try:
                    with extracted, os.fdopen(fd, "wb", closefd=False) as output:
                        shutil.copyfileobj(extracted, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    os.close(fd)
            else:
                raise LumaError("source snapshot contains an unsupported entry")
            if workspace_root is not None and disk_limit_bytes is not None:
                _ensure_directory_budget(workspace_root, disk_limit_bytes)


def _persist_content_file(
    root: Path,
    *,
    namespace: str,
    digest: str,
    source: Path,
    suffix: str,
) -> Path:
    if not _SHA256_RE.fullmatch(digest):
        raise LumaError("content store digest is invalid")
    source_file_digest = _sha256_file(source)
    if source_file_digest != digest:
        raise LumaError("content store source does not match its digest")
    hexadecimal = digest.split(":", 1)[1]
    namespace_parts = [part for part in namespace.split("/") if part]
    if any(not re.fullmatch(r"[a-z0-9-]+", part) for part in namespace_parts):
        raise LumaError("content store namespace is invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_root = root.resolve()
    directory = root.joinpath(*namespace_parts, hexadecimal[:2])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_directory = directory.resolve()
    try:
        resolved_directory.relative_to(resolved_root)
    except ValueError as exc:
        raise LumaError("content store path escapes its root") from exc
    destination = resolved_directory / f"{hexadecimal}{suffix}"
    if destination.exists():
        destination_digest = _sha256_file(destination) if destination.is_file() and not destination.is_symlink() else ""
        if destination.is_symlink() or not destination.is_file() or destination_digest != digest:
            raise LumaError("content store contains a corrupt digest path")
        return destination

    temporary = resolved_directory / f".{hexadecimal}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(resolved_directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def _run_analyzer_container(
    *,
    image: str,
    builder_task_id: str,
    source_dir: Path,
    input_dir: Path,
    output_dir: Path,
    limits: Dict[str, Any],
    cancel_event: Any,
    timeout: int,
) -> None:
    # Revalidate the endpoint immediately before every execution.  Capability
    # advertisement is only a routing hint and may be stale if the rootless
    # daemon or its socket was replaced after the last agent heartbeat.
    docker, docker_host = _rootless_docker_runtime(image)
    daemon_uid, daemon_gid = _rootless_docker_identity(docker_host)
    _prepare_rootless_bind_workspace(
        source_dir=source_dir,
        input_dir=input_dir,
        output_dir=output_dir,
        daemon_uid=daemon_uid,
        daemon_gid=daemon_gid,
    )
    container_name = f"luma-lae-analyze-{hashlib.sha256(builder_task_id.encode('utf-8')).hexdigest()[:16]}"
    pids_limit = _bounded_env_int("LUMA_BUILDER_ANALYZE_PIDS_LIMIT", 256, 32, 4096)
    cpu = f"{float(limits['cpu']):g}"
    memory = f"{int(limits['memoryMiB'])}m"
    command = [
        docker,
        "--host",
        docker_host,
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        container_name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        # UID 0 inside a rootless user namespace maps to the already verified
        # non-root daemon UID on the host.  This lets a 0700 output bind remain
        # writable without making it accessible to every local/container user.
        "--user",
        "0:0",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--cpus",
        cpu,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--mount",
        f"type=bind,source={source_dir},target=/workspace,readonly",
        "--mount",
        f"type=bind,source={input_dir},target=/input,readonly",
        "--mount",
        f"type=bind,source={output_dir},target=/output",
        "--entrypoint",
        "lae-agent-runner",
        image,
        "analyze",
        "--source",
        "/workspace",
        "--metadata",
        "/input/metadata.json",
        "--output-dir",
        "/output",
    ]
    execution_error: BaseException | None = None
    result: _ProcessResult | None = None
    cleanup_succeeded = False
    with tempfile.TemporaryDirectory(prefix="luma-docker-cli-") as docker_home:
        docker_environment = _isolated_docker_environment(Path(docker_home))
        try:
            result = _run_cancellable_process(
                command,
                env=docker_environment,
                timeout=timeout,
                cancel_event=cancel_event,
                disk_watch_path=source_dir.parent,
                disk_limit_bytes=int(limits["diskMiB"]) * 1024 * 1024,
            )
        except BaseException as exc:
            execution_error = exc
        cleanup_succeeded = _remove_runner_container(
            docker,
            docker_host,
            container_name,
            env=docker_environment,
        )
    if not cleanup_succeeded:
        global _BUILDER_ANALYZE_CLEANUP_HEALTHY
        _BUILDER_ANALYZE_CLEANUP_HEALTHY = False
        raise BuilderCleanupFailed("builder runner cleanup could not be verified") from execution_error
    if execution_error is not None:
        raise execution_error
    if result is None:
        raise LumaError("lae-agent-runner did not return a process result")
    if result.returncode != 0:
        raise LumaError(f"lae-agent-runner failed ({_safe_failure_category(result.output)})")
    _harden_rootless_output_tree(
        output_dir,
        daemon_uid=daemon_uid,
        daemon_gid=daemon_gid,
    )


@dataclass(frozen=True)
class _PathOwnershipState:
    path: Path
    uid: int
    gid: int
    mode: int
    is_directory: bool


def _prepare_rootless_bind_workspace(
    *,
    source_dir: Path,
    input_dir: Path,
    output_dir: Path,
    daemon_uid: int,
    daemon_gid: int,
) -> None:
    """Hand one analyzer task to the authenticated rootless Docker peer.

    The immutable snapshot store is deliberately outside this ownership plan.
    Only the disposable task directory and its three bind roots are changed.
    Work is kept root-private until every child has been opened with
    ``O_NOFOLLOW`` and restricted; the work root is handed off last so the
    daemon cannot race the preparation traversal.
    """

    if daemon_uid <= 0 or daemon_gid < 0:
        raise LumaError("rootless Docker daemon identity is invalid for bind ownership")
    work = source_dir.parent
    if input_dir.parent != work or output_dir.parent != work or len({source_dir, input_dir, output_dir}) != 3:
        raise LumaError("analyzer bind directories do not share one task workspace")
    _require_private_directory(work, "analyzer task workspace")
    _require_private_directory(source_dir, "analyzer source bind")
    _require_private_directory(input_dir, "analyzer input bind")
    _require_private_directory(output_dir, "analyzer output bind")
    expected_children = {source_dir.name, input_dir.name, output_dir.name}
    try:
        with os.scandir(work) as entries:
            actual_children = {entry.name for entry in entries}
    except OSError as exc:
        raise LumaError("analyzer task workspace could not be inspected") from exc
    if actual_children != expected_children:
        raise LumaError("analyzer task workspace contains unexpected paths")
    try:
        with os.scandir(output_dir) as entries:
            if next(entries, None) is not None:
                raise LumaError("analyzer output bind must be empty before execution")
    except OSError as exc:
        raise LumaError("analyzer output bind could not be inspected") from exc

    plan: list[tuple[Path, int, bool]] = []
    plan.extend(_restricted_tree_plan(source_dir, directory_mode=0o500, file_mode=0o400, allow_symlinks=True))
    plan.extend(_restricted_tree_plan(input_dir, directory_mode=0o500, file_mode=0o400, allow_symlinks=False))
    plan.append((output_dir, 0o700, True))
    # Handoff the common parent last.  Before this operation the daemon cannot
    # traverse a root-owned 0700 TemporaryDirectory even if it knows the path.
    plan.append((work, 0o700, True))
    _apply_scoped_ownership(plan, uid=daemon_uid, gid=daemon_gid)


def _harden_rootless_output_tree(path: Path, *, daemon_uid: int, daemon_gid: int) -> None:
    _require_private_directory(path, "analyzer output bind")
    plan = _restricted_tree_plan(path, directory_mode=0o700, file_mode=0o600, allow_symlinks=False)
    for candidate, _mode, _is_directory in plan:
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise LumaError("analyzer output ownership could not be inspected") from exc
        if int(candidate_stat.st_uid) != daemon_uid or int(candidate_stat.st_gid) != daemon_gid:
            raise LumaError("analyzer output was not created by the verified rootless Docker daemon")
    _apply_scoped_ownership(plan, uid=daemon_uid, gid=daemon_gid)


def _require_private_directory(path: Path, label: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise LumaError(f"{label} could not be inspected") from exc
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise LumaError(f"{label} must be a real directory")


def _restricted_tree_plan(
    root: Path,
    *,
    directory_mode: int,
    file_mode: int,
    allow_symlinks: bool,
) -> list[tuple[Path, int, bool]]:
    """Return a no-follow ownership plan with directories after their children."""

    result: list[tuple[Path, int, bool]] = []
    pending = [root]
    directories: list[Path] = []
    entries = 0
    while pending:
        directory = pending.pop()
        _require_private_directory(directory, "analyzer bind tree")
        directories.append(directory)
        try:
            with os.scandir(directory) as scanned:
                children = list(scanned)
        except OSError as exc:
            raise LumaError("analyzer bind tree could not be inspected") from exc
        entries += len(children)
        if entries > DEFAULT_MAX_SOURCE_FILES:
            raise LumaError("analyzer bind tree contains too many entries")
        for entry in children:
            candidate = Path(entry.path)
            try:
                candidate_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise LumaError("analyzer bind entry could not be inspected") from exc
            if stat.S_ISLNK(candidate_stat.st_mode):
                if not allow_symlinks:
                    raise LumaError("analyzer bind tree contains a symlink")
                # Symlink mode is not meaningful on Linux and changing owner is
                # unnecessary for traversal.  Most importantly, never follow it.
                continue
            if stat.S_ISDIR(candidate_stat.st_mode):
                pending.append(candidate)
            elif stat.S_ISREG(candidate_stat.st_mode):
                result.append((candidate, file_mode, False))
            else:
                raise LumaError("analyzer bind tree contains an unsupported file type")
    result.extend((directory, directory_mode, True) for directory in reversed(directories))
    return result


def _apply_scoped_ownership(plan: list[tuple[Path, int, bool]], *, uid: int, gid: int) -> None:
    states: list[_PathOwnershipState] = []
    try:
        for path, mode, is_directory in plan:
            fd = _open_bind_entry_no_follow(path, is_directory=is_directory)
            try:
                current = os.fstat(fd)
                states.append(
                    _PathOwnershipState(
                        path=path,
                        uid=int(current.st_uid),
                        gid=int(current.st_gid),
                        mode=stat.S_IMODE(current.st_mode),
                        is_directory=is_directory,
                    )
                )
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
    except (OSError, LumaError) as exc:
        rollback_errors = _restore_scoped_ownership(states)
        if rollback_errors:
            raise LumaError("analyzer bind ownership failed and rollback was incomplete") from exc
        raise LumaError("analyzer bind ownership could not be prepared") from exc


def _restore_scoped_ownership(states: list[_PathOwnershipState]) -> list[BaseException]:
    errors: list[BaseException] = []
    for state in reversed(states):
        try:
            fd = _open_bind_entry_no_follow(state.path, is_directory=state.is_directory)
            try:
                os.fchown(fd, state.uid, state.gid)
                os.fchmod(fd, state.mode)
            finally:
                os.close(fd)
        except (OSError, LumaError) as exc:
            errors.append(exc)
    return errors


def _open_bind_entry_no_follow(path: Path, *, is_directory: bool) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise LumaError("O_NOFOLLOW is required for analyzer bind ownership")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if is_directory:
        if not hasattr(os, "O_DIRECTORY"):
            raise LumaError("O_DIRECTORY is required for analyzer bind ownership")
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    file_stat = os.fstat(fd)
    expected = stat.S_ISDIR(file_stat.st_mode) if is_directory else stat.S_ISREG(file_stat.st_mode)
    if not expected:
        os.close(fd)
        raise LumaError("analyzer bind entry changed type during ownership preparation")
    return fd


def _remove_runner_container(
    docker: str,
    docker_host: str,
    container_name: str,
    *,
    env: Mapping[str, str],
) -> bool:
    for attempt in range(RUNNER_CLEANUP_ATTEMPTS):
        try:
            subprocess.run(
                [docker, "--host", docker_host, "rm", "-f", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                check=False,
                timeout=10,
                start_new_session=True,
            )
            inspected = subprocess.run(
                [docker, "--host", docker_host, "container", "inspect", container_name, "--format", "{{.Id}}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(env),
                text=True,
                check=False,
                timeout=10,
                start_new_session=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            inspected = None
        if inspected is not None and inspected.returncode != 0:
            detail = str(inspected.stdout or "").lower()
            if "no such object" in detail or "no such container" in detail:
                return True
        if attempt + 1 < RUNNER_CLEANUP_ATTEMPTS:
            time.sleep(0.2 * (attempt + 1))
    return False


def _validate_runner_output(output_dir: Path, metadata: Dict[str, Any]) -> Dict[str, Any]:
    result_path = _safe_output_file(output_dir, "result.json", max_bytes=MAX_RESULT_BYTES)
    result = _read_json_object(result_path, label="runner result")
    expected_top_level = {
        "schemaVersion",
        "status",
        "decision",
        "builderTaskId",
        "externalOperationId",
        "tenantRef",
        "applicationRef",
        "resolvedCommit",
        "sourceTreeDigest",
        "sourceSnapshotId",
        "sourceSnapshotDigest",
        "policyVersion",
        "agentImageDigest",
        "artifacts",
    }
    unknown = sorted(set(result) - expected_top_level)
    if unknown:
        raise LumaError("runner result contains unknown fields")
    required = {
        "schemaVersion",
        "status",
        "decision",
        "externalOperationId",
        "tenantRef",
        "applicationRef",
        "resolvedCommit",
        "sourceSnapshotId",
        "sourceSnapshotDigest",
        "policyVersion",
        "artifacts",
    }
    missing = sorted(required - set(result))
    if missing:
        raise LumaError(f"runner result is missing fields: {', '.join(missing)}")
    if result.get("schemaVersion") != "lae.agent-analysis-result/v1" or result.get("status") != "succeeded":
        raise LumaError("runner result has an invalid schemaVersion or status")
    if result.get("decision") not in {"allow", "needs_configuration", "deny"}:
        raise LumaError("runner result has an invalid decision")
    for key in (
        "externalOperationId",
        "tenantRef",
        "applicationRef",
        "resolvedCommit",
        "sourceSnapshotId",
        "sourceSnapshotDigest",
        "policyVersion",
    ):
        if result.get(key) != metadata.get(key):
            raise LumaError(f"runner result {key} does not match trusted metadata")
    if "builderTaskId" in result and result.get("builderTaskId") != metadata.get("builderTaskId"):
        raise LumaError("runner result builderTaskId does not match trusted metadata")
    for key in ("sourceTreeDigest", "agentImageDigest"):
        if key in result and result.get(key) != metadata.get(key):
            raise LumaError(f"runner result {key} does not match trusted metadata")

    raw_artifacts = result.get("artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != set(_RUNNER_ARTIFACT_FILES):
        raise LumaError("runner result must declare exactly the three analysis artifacts")
    artifacts: Dict[str, Any] = {}
    artifact_bodies: Dict[str, Dict[str, Any]] = {}
    for name, expected_filename in _RUNNER_ARTIFACT_FILES.items():
        descriptor = raw_artifacts.get(name)
        if not isinstance(descriptor, dict) or set(descriptor) != {"path", "digest", "mediaType", "sizeBytes"}:
            raise LumaError(f"runner artifact descriptor is invalid: {name}")
        if descriptor.get("path") != expected_filename or descriptor.get("mediaType") != _RUNNER_ARTIFACT_MEDIA_TYPES[name]:
            raise LumaError(f"runner artifact descriptor is invalid: {name}")
        artifact_path = _safe_output_file(output_dir, expected_filename, max_bytes=MAX_ARTIFACT_BYTES)
        artifact_body = _read_json_object(artifact_path, label=f"runner artifact {name}")
        if artifact_path.read_bytes() != _canonical_json_bytes(artifact_body):
            raise LumaError(f"runner artifact is not canonical JSON: {name}")
        digest = _logical_artifact_digest(name, artifact_body)
        if descriptor.get("digest") != digest:
            raise LumaError(f"runner artifact digest mismatch: {name}")
        size = artifact_path.stat().st_size
        declared_size = descriptor.get("sizeBytes")
        if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size != size:
            raise LumaError(f"runner artifact size mismatch: {name}")
        _validate_artifact_binding(name, artifact_body, metadata)
        artifact_bodies[name] = artifact_body
        artifacts[name] = {"digest": digest, "sizeBytes": size}
    deployment_policy = _require_mapping(artifact_bodies["deploymentPlan"].get("policy"), "deployment plan policy")
    if result.get("decision") != deployment_policy.get("decision"):
        raise LumaError("runner decision does not match deployment plan policy")
    return {
        "artifacts": artifacts,
        "buildPlanProposal": artifact_bodies["buildPlan"],
    }


def _validate_artifact_binding(name: str, artifact: Dict[str, Any], metadata: Dict[str, Any]) -> None:
    if name == "buildPlan":
        required = {
            "schemaVersion",
            "sourceSnapshotDigest",
            "resolvedCommit",
            "policyVersion",
            "builds",
            "externalImages",
        }
        if set(artifact) != required or artifact.get("schemaVersion") != "lae.build-plan-proposal/v1":
            raise LumaError("runner build plan proposal has an invalid closed schema")
        if artifact.get("sourceSnapshotDigest") != metadata.get("sourceSnapshotDigest"):
            raise LumaError("runner build plan proposal has a conflicting sourceSnapshotDigest")
        if artifact.get("resolvedCommit") != metadata.get("resolvedCommit"):
            raise LumaError("runner build plan proposal has a conflicting resolvedCommit")
        if artifact.get("policyVersion") != metadata.get("policyVersion"):
            raise LumaError("runner build plan proposal has a conflicting policyVersion")
        raw_external_images = artifact.get("externalImages")
        if not isinstance(raw_external_images, list):
            raise LumaError("runner build plan proposal has invalid externalImages")
        candidate_external_images: list[Dict[str, Any]] = []
        for item in raw_external_images:
            if not isinstance(item, dict) or not set(item).issubset(
                {"key", "ref", "platform", "resolvedDigest"}
            ):
                raise LumaError("runner build plan proposal has an invalid external image")
            parsed = parse_external_image_reference(item.get("ref"))
            pre_resolved = item.get("resolvedDigest")
            if not parsed["digest"] and pre_resolved is not None:
                raise LumaError("runner build plan proposal must not pre-resolve a tagged external image")
            if parsed["digest"] and pre_resolved is None:
                raise LumaError("runner build plan proposal must bind a digest external image")
            if pre_resolved is not None and (
                not isinstance(pre_resolved, str)
                or not _SHA256_RE.fullmatch(pre_resolved)
                or (parsed["digest"] and pre_resolved != parsed["digest"])
            ):
                raise LumaError("runner build plan proposal has a conflicting external image digest")
            candidate_external_images.append(
                {
                    "key": item.get("key"),
                    "ref": parsed["reference"],
                    "resolvedDigest": pre_resolved or parsed["digest"] or "sha256:" + ("0" * 64),
                    "platform": item.get("platform"),
                }
            )
        signed_plan = dict(artifact)
        signed_plan["schemaVersion"] = "lae.build-plan/v1"
        signed_plan["externalImages"] = candidate_external_images
        signed_plan["signature"] = {
            "keyId": "lae-plan-runner-validation",
            "value": "A" * 43,
        }
        validate_builder_task_request(
            {
                "schemaVersion": "luma.builder-task/v1",
                "kind": "build-plan",
                "externalOperationId": metadata.get("externalOperationId"),
                "tenantRef": metadata.get("tenantRef"),
                "applicationRef": metadata.get("applicationRef"),
                "payload": {
                    "sourceSnapshotId": metadata.get("sourceSnapshotId"),
                    "sourceSnapshotDigest": metadata.get("sourceSnapshotDigest"),
                    "signedBuildPlan": signed_plan,
                    "credentialLeaseId": "runner-artifact-validation",
                    "limits": {
                        "cpu": 1,
                        "memoryMiB": 256,
                        "diskMiB": 256,
                        "timeoutSeconds": 10,
                    },
                },
            }
        )
        return
    if name == "deploymentPlan":
        required = {
            "schemaVersion",
            "planId",
            "sourceRevisionId",
            "sourceDigest",
            "kind",
            "services",
            "routes",
            "volumes",
            "environment",
            "warnings",
            "blockers",
            "policy",
        }
        if set(artifact) != required or artifact.get("schemaVersion") != "lae.deployment-plan/v1":
            raise LumaError("runner deployment plan has an invalid closed schema")
        if artifact.get("sourceDigest") != metadata.get("sourceSnapshotDigest"):
            raise LumaError("runner deployment plan has a conflicting sourceDigest")
        if artifact.get("kind") not in {"service", "compose"}:
            raise LumaError("runner deployment plan has an invalid kind")
        for key in ("services", "routes", "volumes", "environment", "warnings", "blockers"):
            if not isinstance(artifact.get(key), list):
                raise LumaError("runner deployment plan has an invalid collection")
        if not artifact["services"]:
            raise LumaError("runner deployment plan must contain at least one service")
        policy = _require_mapping(artifact.get("policy"), "deployment plan policy")
        if set(policy) != {"version", "decision"}:
            raise LumaError("runner deployment plan has an invalid policy")
        if policy.get("version") != metadata.get("policyVersion"):
            raise LumaError("runner deployment plan has a conflicting policy version")
        if policy.get("decision") not in {"allow", "needs_configuration", "deny"}:
            raise LumaError("runner deployment plan has an invalid policy decision")
        return
    if name == "evidence":
        required = {
            "schemaVersion",
            "agentVersion",
            "adapter",
            "source",
            "inventory",
            "findings",
            "environment",
            "warnings",
            "blockers",
        }
        if set(artifact) != required or artifact.get("schemaVersion") != "lae.analysis-evidence/v1":
            raise LumaError("runner evidence has an invalid closed schema")
        source = _require_mapping(artifact.get("source"), "analysis evidence source")
        if set(source) != {"resolvedCommit", "sourceSnapshotId", "sourceSnapshotDigest"}:
            raise LumaError("runner evidence has an invalid source binding")
        for key in ("resolvedCommit", "sourceSnapshotId", "sourceSnapshotDigest"):
            if source.get(key) != metadata.get(key):
                raise LumaError(f"runner evidence has a conflicting {key}")
        if not isinstance(artifact.get("adapter"), dict):
            raise LumaError("runner evidence has an invalid adapter")
        for key in ("inventory", "findings", "environment", "warnings", "blockers"):
            if not isinstance(artifact.get(key), list):
                raise LumaError("runner evidence has an invalid collection")
        return
    raise LumaError("runner returned an unknown artifact kind")


def _resolve_build_plan_proposal(
    proposal: Dict[str, Any],
    *,
    allowed_registries: list[str],
    cancel_event: Any,
    timeout: int,
) -> Dict[str, Any]:
    """Turn a network-disabled runner proposal into a digest-bound candidate."""

    candidate = dict(proposal)
    candidate["schemaVersion"] = "lae.build-plan-candidate/v1"
    external_images = proposal.get("externalImages") or []
    if not external_images:
        candidate["externalImages"] = []
        return candidate
    deadline = time.monotonic() + max(int(timeout), 1)
    allowlist = set(allowed_registries)
    crane = ""
    resolved_images: list[Dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="luma-crane-cli-") as crane_home:
        environment = _isolated_docker_environment(Path(crane_home))
        for item in external_images:
            _raise_if_canceled(cancel_event)
            parsed = parse_external_image_reference(item.get("ref"))
            if parsed["registryHost"] not in allowlist:
                raise LumaError("external image registry is not allowlisted for source analysis")
            if parsed["digest"]:
                resolved_digest = parsed["digest"]
            else:
                if not crane:
                    crane = _require_crane_runtime()
                result = _run_cancellable_process(
                    [
                        crane,
                        "digest",
                        "--platform",
                        str(item.get("platform") or ""),
                        parsed["reference"],
                    ],
                    env=environment,
                    timeout=_remaining_seconds(deadline),
                    cancel_event=cancel_event,
                )
                if result.returncode != 0:
                    raise LumaError("anonymous external image resolution failed during source analysis")
                resolved_digest = _parse_crane_digest(result.output)
            expected_digest = str(item.get("resolvedDigest") or parsed["digest"] or "")
            if expected_digest and resolved_digest != expected_digest:
                raise LumaError("external image resolution does not match the runner proposal")
            resolved_images.append(
                {
                    "key": str(item.get("key") or ""),
                    "ref": parsed["reference"],
                    "resolvedDigest": resolved_digest,
                    "platform": str(item.get("platform") or ""),
                }
            )
    candidate["externalImages"] = resolved_images
    return candidate


def _parse_crane_digest(output: str) -> str:
    text = str(output or "").strip()
    if "\n" in text or "\r" in text or not _SHA256_RE.fullmatch(text):
        raise LumaError("external image resolver returned an invalid digest")
    return text


def _validate_enriched_build_plan_candidate(
    candidate: Dict[str, Any], metadata: Dict[str, Any]
) -> None:
    signed_plan = dict(candidate)
    signed_plan["schemaVersion"] = "lae.build-plan/v1"
    signed_plan["signature"] = {
        "keyId": "lae-plan-analyze-resolution",
        "value": "A" * 43,
    }
    validate_builder_task_request(
        {
            "schemaVersion": "luma.builder-task/v1",
            "kind": "build-plan",
            "externalOperationId": metadata.get("externalOperationId"),
            "tenantRef": metadata.get("tenantRef"),
            "applicationRef": metadata.get("applicationRef"),
            "payload": {
                "sourceSnapshotId": metadata.get("sourceSnapshotId"),
                "sourceSnapshotDigest": metadata.get("sourceSnapshotDigest"),
                "signedBuildPlan": signed_plan,
                "credentialLeaseId": "analyze-resolution-validation",
                "limits": {
                    "cpu": 1,
                    "memoryMiB": 256,
                    "diskMiB": 256,
                    "timeoutSeconds": 10,
                },
            },
        }
    )


def _require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LumaError(f"runner {label} must be an object")
    return value


def _logical_artifact_digest(_name: str, artifact: Dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(artifact)).hexdigest()}"


def _canonical_json_bytes(value: Dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LumaError("runner artifact is not canonical JSON") from exc


def _artifact_store_name(name: str) -> str:
    return {"evidence": "evidence", "deploymentPlan": "deployment-plan", "buildPlan": "build-plan-candidate"}[name]


def _safe_output_file(output_dir: Path, filename: str, *, max_bytes: int) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise LumaError("runner output filename is unsafe")
    root = output_dir.resolve()
    path = output_dir / filename
    if path.is_symlink() or not path.is_file():
        raise LumaError(f"runner output is missing or not a regular file: {filename}")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LumaError("runner output path escapes the output directory") from exc
    size = resolved.stat().st_size
    if size <= 0 or size > max_bytes:
        raise LumaError(f"runner output has an invalid size: {filename}")
    return resolved


def _read_json_object(path: Path, *, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(f"invalid JSON constant: {constant}")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise LumaError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise LumaError(f"{label} must be a JSON object")
    return value


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _write_json_file(path: Path, value: Dict[str, Any], *, mode: int) -> None:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    path.chmod(mode)


def _write_private_bytes(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _snapshot_id(digest: str, *, builder_task_id: str) -> str:
    if not _SHA256_RE.fullmatch(digest):
        raise LumaError("source snapshot digest is invalid")
    if not _REFERENCE_RE.fullmatch(str(builder_task_id or "")):
        raise LumaError("builder task id is invalid for source snapshot binding")
    # Content bytes remain globally de-duplicated by digest, while the public
    # snapshot handle is task-scoped.  This prevents equal source trees owned by
    # different tenants/applications from colliding in Control's binding table.
    binding = hashlib.sha256(f"{builder_task_id}\0{digest}".encode("utf-8")).hexdigest()
    return f"snapshot-{binding}"


def _rootless_docker_runtime(runner_image: str) -> tuple[str, str]:
    """Return a Docker CLI and an authenticated rootless daemon endpoint.

    The dedicated LAE analyzer lane never consults ``DOCKER_HOST`` or a Docker
    context.  The endpoint must be explicitly configured, must be owned by the
    same non-root UID as its ``/run/user/<uid>`` path, and the kernel-reported
    daemon peer UID must agree.  Docker's own security options are then checked
    as an independent rootless-mode proof.
    """

    if not sys.platform.startswith("linux"):
        raise LumaError("builder analyzer Docker runtime is Linux-only")
    pinned_image = str(os.environ.get(BUILDER_ANALYZE_IMAGE_ENV) or "").strip()
    if not _IMAGE_DIGEST_RE.fullmatch(pinned_image):
        raise LumaError("builder analyzer runner image allowlist is invalid")
    if runner_image != pinned_image:
        raise LumaError("builder analyzer runner image is not allowlisted")
    docker = _docker_binary()
    if not docker:
        raise LumaError("docker command is not available on the builder")
    docker_host = _rootless_docker_host()
    with tempfile.TemporaryDirectory(prefix="luma-docker-probe-") as docker_home:
        environment = _isolated_docker_environment(Path(docker_home))
        security_probe = subprocess.run(
            [docker, "--host", docker_host, "info", "--format", "{{json .SecurityOptions}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
            start_new_session=True,
        )
        if security_probe.returncode != 0:
            raise LumaError("rootless Docker daemon is unavailable")
        try:
            security_options = json.loads(str(security_probe.stdout or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LumaError("Docker daemon security options are invalid") from exc
        if not isinstance(security_options, list) or not any(
            str(option).strip().lower() in {"rootless", "name=rootless"}
            for option in security_options
        ):
            raise LumaError("Docker daemon did not prove rootless mode")

        # The runner must already exist under its immutable repo digest.  This
        # keeps the execution path free of Docker client credentials and makes
        # ``docker run --pull never`` deterministic.
        image_probe = subprocess.run(
            [docker, "--host", docker_host, "image", "inspect", runner_image, "--format", "{{json .RepoDigests}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
            start_new_session=True,
        )
        if image_probe.returncode != 0:
            raise LumaError("allowlisted analyzer runner image is unavailable")
        try:
            repository_digests = json.loads(str(image_probe.stdout or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LumaError("allowlisted analyzer runner image metadata is invalid") from exc
        if not isinstance(repository_digests, list) or runner_image not in repository_digests:
            raise LumaError("local analyzer runner image does not match its allowlisted digest")
    return docker, docker_host


def _rootless_docker_host() -> str:
    value = str(os.environ.get(BUILDER_ANALYZE_DOCKER_HOST_ENV) or "").strip()
    _rootless_docker_identity(value)
    return value


def _rootless_docker_identity(value: str) -> tuple[int, int]:
    """Return the kernel-authenticated rootless daemon UID/GID for one endpoint."""

    match = _ROOTLESS_DOCKER_HOST_RE.fullmatch(value)
    if not match:
        raise LumaError(
            f"{BUILDER_ANALYZE_DOCKER_HOST_ENV} must explicitly name unix:///run/user/<uid>/docker.sock"
        )
    socket_path = Path(match.group(1))
    expected_uid = int(match.group(2))
    try:
        socket_stat = socket_path.lstat()
        runtime_directory_stat = socket_path.parent.lstat()
    except OSError as exc:
        raise LumaError("rootless Docker socket could not be inspected") from exc
    if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid == 0:
        raise LumaError("Docker endpoint is not a non-root-owned Unix socket")
    if int(socket_stat.st_uid) != expected_uid:
        raise LumaError("Docker socket owner does not match its rootless runtime path")
    peer_pid, peer_uid, peer_gid = _unix_socket_peer_credentials(socket_path)
    if (
        peer_pid <= 0
        or peer_uid == 0
        or peer_uid != expected_uid
        or int(socket_stat.st_uid) != peer_uid
        or int(socket_stat.st_gid) != peer_gid
    ):
        raise LumaError("Docker daemon peer credentials do not match the non-root socket owner")
    if (
        not stat.S_ISDIR(runtime_directory_stat.st_mode)
        or stat.S_ISLNK(runtime_directory_stat.st_mode)
        or int(runtime_directory_stat.st_uid) != expected_uid
        or int(runtime_directory_stat.st_gid) != peer_gid
    ):
        raise LumaError("Docker rootless runtime directory ownership is invalid")
    return peer_uid, peer_gid


def _unix_socket_peer_credentials(path: Path) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise LumaError("Linux SO_PEERCRED is unavailable for Docker daemon verification")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(2.0)
        client.connect(str(path))
        raw = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError as exc:
        raise LumaError("Docker daemon peer credentials could not be verified") from exc
    finally:
        client.close()
    if len(raw) != struct.calcsize("3i"):
        raise LumaError("Docker daemon peer credentials are invalid")
    pid, uid, gid = struct.unpack("3i", raw)
    if pid <= 0 or uid < 0 or gid < 0:
        raise LumaError("Docker daemon peer credentials are invalid")
    return int(pid), int(uid), int(gid)


def _unix_socket_peer_uid(path: Path) -> int:
    """Compatibility wrapper for callers that only need the authenticated UID."""

    return _unix_socket_peer_credentials(path)[1]


def _isolated_docker_environment(config_directory: Path) -> Dict[str, str]:
    config_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_directory.chmod(0o700)
    # Deliberately do not copy the host environment.  In particular this drops
    # DOCKER_HOST/context/TLS variables, Docker credential config, HTTP proxy
    # variables and CLI tracing knobs.  The endpoint is always in argv.
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(config_directory),
        "DOCKER_CONFIG": str(config_directory),
        "LANG": "C.UTF-8",
    }


def _require_git_runtime() -> str:
    git = shutil.which("git")
    if not git:
        raise LumaError("git command is not available on the builder")
    git = str(Path(git).resolve())
    probe = subprocess.run(
        [git, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": os.devnull,
            "LANG": "C.UTF-8",
        },
        check=False,
        timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
        start_new_session=True,
    )
    if probe.returncode != 0:
        raise LumaError("git command is not runnable on the builder")
    return git


def _require_crane_runtime() -> str:
    crane = shutil.which("crane")
    if not crane:
        raise LumaError("crane command is not available on the builder")
    crane = str(Path(crane).resolve())
    probe = subprocess.run(
        [crane, "version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": os.devnull,
            "LANG": "C.UTF-8",
        },
        check=False,
        timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
        start_new_session=True,
    )
    if probe.returncode != 0:
        raise LumaError("crane command is not runnable on the builder")
    return crane


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


def _docker_binary() -> str | None:
    docker = shutil.which("docker")
    if docker:
        return str(Path(docker).resolve())
    for candidate in ("/usr/local/bin/docker", "/usr/bin/docker", "/snap/bin/docker"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def _work_parent() -> Path:
    raw = str(os.environ.get(BUILDER_WORK_ROOT_ENV) or "").strip()
    if not raw:
        return Path(tempfile.gettempdir())
    parent = Path(raw).expanduser()
    if not parent.is_absolute():
        raise LumaError(f"{BUILDER_WORK_ROOT_ENV} must be an absolute path")
    if "," in str(parent):
        raise LumaError(f"{BUILDER_WORK_ROOT_ENV} must not contain commas")
    return parent


def _require_free_disk(path: Path, task_budget_bytes: int) -> None:
    reserve_mib = _bounded_env_int("LUMA_BUILDER_FREE_DISK_RESERVE_MIB", 512, 0, 1_048_576)
    required = int(task_budget_bytes) + reserve_mib * 1024 * 1024
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        raise LumaError("builder workspace free disk could not be determined") from exc
    if free < required:
        raise LumaError("builder has insufficient free disk for the requested task budget")


def _directory_usage_bytes(path: Path, *, stop_after: int | None = None) -> int:
    if not path.exists():
        return 0
    total = 0
    pending = [path]
    seen_entries = 0
    max_entries = _bounded_env_int("LUMA_BUILDER_MAX_WORKSPACE_FILES", DEFAULT_MAX_SOURCE_FILES * 2, 1, 4_000_000)
    while pending:
        current = pending.pop()
        try:
            file_stat = current.lstat()
        except FileNotFoundError:
            continue
        allocated = int(getattr(file_stat, "st_blocks", 0) or 0) * 512
        total += max(int(file_stat.st_size), allocated)
        if stop_after is not None and total > stop_after:
            return total
        if not stat.S_ISDIR(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen_entries += 1
                    if seen_entries > max_entries:
                        if stop_after is not None:
                            return stop_after + 1
                        raise LumaError("builder task workspace exceeds the entry limit")
                    pending.append(Path(entry.path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LumaError("builder workspace disk usage could not be determined") from exc
    return total


def _ensure_directory_budget(path: Path, limit_bytes: int) -> None:
    if _directory_usage_bytes(path, stop_after=limit_bytes) > limit_bytes:
        raise LumaError("builder task workspace exceeded its disk budget")


def _ensure_projected_budget(path: Path, additional_bytes: int, limit_bytes: int) -> None:
    current = _directory_usage_bytes(path, stop_after=limit_bytes)
    if current > limit_bytes or current + max(int(additional_bytes), 0) > limit_bytes:
        raise LumaError("builder task workspace would exceed its disk budget")


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


def _emit_phase(progress: Callable[[Dict[str, Any]], None] | None, name: str, status_value: str) -> None:
    if progress:
        event_type = {
            "source_fetch": "source.fetch",
            "source_snapshot": "source.snapshot",
            "agent_analysis": "analysis",
            "external_resolution": "resolve",
        }.get(name)
        if event_type and status_value in {"running", "succeeded"}:
            # Control uses the allowlisted type to generate the durable message;
            # this fixed line exists only so the generic agent progress parser
            # accepts the event and never contains repository or runner text.
            progress({"type": event_type, "line": f"{event_type}:{status_value}"})


def _safe_failure_category(output: str) -> str:
    lowered = str(output or "").lower()
    if "authentication failed" in lowered or "could not read username" in lowered or "access denied" in lowered:
        return "authentication unavailable or rejected"
    if "not found" in lowered or "repository not found" in lowered:
        return "source or image not found"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return "process exited non-zero"


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise LumaError(f"analyze-source {label} must be between {minimum} and {maximum}")
    return int(value)


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LumaError(f"analyze-source {label} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum or normalized > maximum:
        raise LumaError(f"analyze-source {label} must be between {minimum} and {maximum}")
    return normalized


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)
