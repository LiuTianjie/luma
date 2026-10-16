"""Tekton PipelineRun + Remote BuildKit Pool controller for Luma PaaS."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import time
from typing import Any, Dict, Iterable, Mapping

from ...build_capabilities import (
    infer_native_platform,
    validate_native_platform,
)
from ...build_sources import validate_git_contexts_spec
from ...errors import LumaError
from ...service import slugify
from .artifact_store import BuildStepTiming

TERMINAL_STATUSES: set[str] = {"succeeded", "failed", "canceled"}


_APT_HTTP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"http://deb\.debian\.org\b"), "https://deb.debian.org"),
    (re.compile(r"http://security\.debian\.org\b"), "https://security.debian.org"),
    (re.compile(r"http://archive\.ubuntu\.com\b"), "https://archive.ubuntu.com"),
    (re.compile(r"http://ports\.ubuntu\.com\b"), "https://ports.ubuntu.com"),
    (re.compile(r"http://dl-cdn\.alpinelinux\.org\b"), "https://dl-cdn.alpinelinux.org"),
)


@dataclass(frozen=True)
class BuilderEndpoint:
    name: str
    address: str
    platform: str
    os_name: str = "linux"
    max_concurrency: int = 4
    active_builds: int = 0
    cache_used_bytes: int = 0
    cache_limit_bytes: int = 30 * 1024 ** 3
    ready: bool = True

    @property
    def available_slots(self) -> int:
        if not self.ready:
            return 0
        return max(self.max_concurrency - self.active_builds, 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "platform": self.platform,
            "osName": self.os_name,
            "maxConcurrency": self.max_concurrency,
            "activeBuilds": self.active_builds,
            "availableSlots": self.available_slots,
            "cacheUsedBytes": self.cache_used_bytes,
            "cacheLimitBytes": self.cache_limit_bytes,
            "cacheUsePercent": round((self.cache_used_bytes / self.cache_limit_bytes) * 100, 1)
            if self.cache_limit_bytes > 0
            else 0.0,
            "ready": self.ready,
        }


def accelerate_dockerfile_mirrors(dockerfile_text: str) -> tuple[str, bool]:
    """Upgrade throttled plain-HTTP Debian/Ubuntu/Alpine package mirrors to HTTPS.

    In historical builds (e.g., serverx video worker installing ffmpeg), plain HTTP
    apt downloads over cross-border links were throttled to ~36 KB/s (44 minutes).
    Switching to HTTPS avoids DPI throttling while preserving official GPG verification.
    """
    updated = str(dockerfile_text or "")
    for pattern, replacement in _APT_HTTP_PATTERNS:
        updated = pattern.sub(replacement, updated)
    return updated, updated != dockerfile_text


def parse_buildkit_vertex_waterfall(
    events: Iterable[Mapping[str, Any]],
) -> tuple[list[BuildStepTiming], float]:
    """Parse BuildKit rawjson progress vertices into a step waterfall and cache hit ratio."""
    vertices: Dict[str, Dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        vertex_list = event.get("vertexes") or event.get("vertices") or []
        if isinstance(vertex_list, list):
            for v in vertex_list:
                if not isinstance(v, Mapping):
                    continue
                digest = str(v.get("digest") or v.get("id") or "").strip()
                if not digest:
                    continue
                if digest not in vertices:
                    order.append(digest)
                    vertices[digest] = {
                        "step_id": digest,
                        "name": str(v.get("name") or digest),
                        "cached": bool(v.get("cached")),
                        "started_at": int(v.get("started") or v.get("startedAt") or 0),
                        "completed_at": int(v.get("completed") or v.get("completedAt") or 0),
                        "duration_ms": int(v.get("durationMs") or 0),
                        "layer_bytes": int(v.get("layerBytes") or 0),
                    }
                else:
                    entry = vertices[digest]
                    if v.get("name"):
                        entry["name"] = str(v["name"])
                    entry["cached"] = entry["cached"] or bool(v.get("cached"))
                    if v.get("started") or v.get("startedAt"):
                        entry["started_at"] = int(v.get("started") or v.get("startedAt") or 0)
                    if v.get("completed") or v.get("completedAt"):
                        entry["completed_at"] = int(v.get("completed") or v.get("completedAt") or 0)
                    if v.get("durationMs") is not None:
                        entry["duration_ms"] = int(v["durationMs"])
                    if v.get("layerBytes") is not None:
                        entry["layer_bytes"] = int(v["layerBytes"])

    timings: list[BuildStepTiming] = []
    for digest in order:
        item = vertices[digest]
        duration_ms = int(item["duration_ms"])
        if duration_ms <= 0 and item["completed_at"] >= item["started_at"] > 0:
            duration_ms = (item["completed_at"] - item["started_at"]) * 1000
        timings.append(
            BuildStepTiming(
                step_id=item["step_id"],
                name=item["name"],
                duration_ms=max(duration_ms, 0),
                cached=bool(item["cached"]),
                started_at=int(item["started_at"]),
                completed_at=int(item["completed_at"]),
                layer_bytes=int(item["layer_bytes"]),
            )
        )

    if not timings:
        return [], 0.0
    cached_count = sum(1 for step in timings if step.cached)
    return timings, round(cached_count / len(timings), 4)


class BuildController:
    """Schedules native BuildKit builds via Tekton PipelineRuns with instant cancellation."""

    def __init__(
        self,
        *,
        registry_host: str,
        builders: Iterable[BuilderEndpoint],
        namespace: str = "luma-builds",
    ) -> None:
        self.registry_host = str(registry_host or "").strip().rstrip("/")
        if not self.registry_host:
            raise LumaError("BuildController requires a Zot registry_host")
        self.namespace = namespace
        self._builders: Dict[str, BuilderEndpoint] = {b.name: b for b in builders}
        self._pipeline_runs: Dict[str, Dict[str, Any]] = {}

    def list_builders(self) -> list[BuilderEndpoint]:
        return [self._builders[name] for name in sorted(self._builders)]

    def _claim_slot(self, builder_name: str) -> None:
        builder = self._builders.get(builder_name)
        if builder:
            self._builders[builder_name] = replace(
                builder,
                active_builds=builder.active_builds + 1,
            )

    def _release_slot(self, builder_name: str) -> None:
        builder = self._builders.get(builder_name)
        if builder and builder.active_builds > 0:
            self._builders[builder_name] = replace(
                builder,
                active_builds=builder.active_builds - 1,
            )

    def select_native_builder(
        self,
        platform: str = "",
        *,
        preferred_node: str = "",
        target_node: str = "",
        target_region: str = "",
    ) -> BuilderEndpoint:
        """Select a ready native BuilderEndpoint for the target platform (never QEMU)."""
        node_archs = {b.name: b.platform for b in self._builders.values()}

        if platform and str(platform).strip():
            target_platform = validate_native_platform(platform)

            # Check explicit platform conflicts with target_region
            region = str(target_region or "").strip().lower()
            if region == "home" and target_platform != "linux/arm64":
                raise LumaError(
                    f"explicit platform {target_platform} conflicts with target region "
                    f"{target_region} (requires linux/arm64); cross-architecture emulation is disabled"
                )

            # Check explicit platform conflicts with target_node
            node = str(target_node or "").strip()
            if node:
                expected_arch = None
                if node in node_archs:
                    expected_arch = validate_native_platform(node_archs[node])
                    if expected_arch.startswith("darwin/"):
                        expected_arch = f"linux/{expected_arch.partition('/')[2]}"
                elif node.lower() in {"macbook-pro", "air3"} or "arm64" in node.lower() or "aarch64" in node.lower():
                    expected_arch = "linux/arm64"

                if expected_arch and target_platform != expected_arch:
                    raise LumaError(
                        f"explicit platform {target_platform} conflicts with target node "
                        f"{target_node} ({expected_arch}); cross-architecture emulation is disabled"
                    )
        else:
            target_platform = infer_native_platform(
                platform="",
                target_node=target_node or preferred_node,
                target_region=target_region,
                node_architectures=node_archs,
            )

        if preferred_node:
            candidate = self._builders.get(preferred_node)
            if candidate is None:
                raise LumaError(f"requested builder node is not registered: {preferred_node}")
            if not candidate.ready:
                raise LumaError(f"requested builder node is offline: {preferred_node}")
            candidate_platform = validate_native_platform(candidate.platform)
            if candidate_platform != target_platform:
                raise LumaError(
                    f"builder {preferred_node} ({candidate.platform}) does not match native target "
                    f"platform {target_platform}; cross-architecture emulation is disabled"
                )
            return candidate

        if target_node and target_node in self._builders:
            target_b = self._builders[target_node]
            target_b_platform = validate_native_platform(target_b.platform)
            if target_b_platform != target_platform:
                raise LumaError(
                    f"target node {target_node} ({target_b.platform}) does not match native target "
                    f"platform {target_platform}; cross-architecture emulation is disabled"
                )

        matching = [
            b
            for b in self._builders.values()
            if b.ready and validate_native_platform(b.platform) == target_platform
        ]
        if not matching:
            raise LumaError(
                f"no ready native BuildKit endpoint available for {target_platform}; "
                "register an online builder for this architecture"
            )
        matching.sort(
            key=lambda b: (
                b.name != target_node,
                -b.available_slots,
                b.cache_used_bytes,
                b.name,
            )
        )
        return matching[0]

    def submit_build(
        self,
        *,
        project: str,
        repo_url: str,
        revision: str,
        branch: str = "main",
        platform: str = "",
        target_node: str = "",
        target_region: str = "",
        context_subpath: str = ".",
        dockerfile_path: str = "Dockerfile",
        build_spec: Mapping[str, Any] | None = None,
        preferred_builder: str = "",
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        """Compile and submit a Tekton PipelineRun and automatically supersede older branch runs."""
        project_slug = slugify(project)

        resolved_node = str(target_node or "").strip()
        resolved_region = str(target_region or "").strip()
        if isinstance(build_spec, Mapping):
            if not resolved_node:
                resolved_node = str(
                    build_spec.get("target_node")
                    or build_spec.get("targetNode")
                    or build_spec.get("node")
                    or (build_spec.get("placement") or {}).get("node")
                    or ""
                ).strip()
            if not resolved_region:
                resolved_region = str(
                    build_spec.get("target_region")
                    or build_spec.get("targetRegion")
                    or build_spec.get("region")
                    or (build_spec.get("placement") or {}).get("region")
                    or ""
                ).strip()

        builder = self.select_native_builder(
            platform,
            preferred_node=preferred_builder,
            target_node=resolved_node,
            target_region=resolved_region,
        )
        git_contexts = validate_git_contexts_spec(build_spec or {})

        replacement_key = f"{project_slug}:{branch}:{builder.platform}"
        if idempotency_key:
            for existing in self._pipeline_runs.values():
                if existing.get("idempotencyKey") == idempotency_key:
                    return existing

        now = int(time.time())
        superseded_ids: list[str] = []
        for run_id, run in list(self._pipeline_runs.items()):
            if (
                run.get("replacementKey") == replacement_key
                and run.get("status") in {"queued", "running"}
            ):
                self.cancel_build(run_id, reason=f"Superseded by newer commit on {branch}")
                superseded_ids.append(run_id)

        digest_seed = f"{project_slug}:{repo_url}:{revision}:{builder.platform}:{now}:{ len(self._pipeline_runs) + 1 }"
        short_id = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:12]
        run_name = f"bld-{project_slug}-{short_id}"
        arch_slug = builder.platform.replace("/", "-")
        image_repo = f"{self.registry_host}/{project_slug}"
        image_ref = f"{image_repo}:build-{short_id}"
        cache_ref = f"{image_repo}:cache-{arch_slug}"

        extra_flags = " ".join(
            f"--local context:{alias}=/workspace/source/contexts/{alias}"
            for alias in sorted(git_contexts)
        )

        pipeline_run_manifest = {
            "apiVersion": "tekton.dev/v1",
            "kind": "PipelineRun",
            "metadata": {
                "name": run_name,
                "namespace": self.namespace,
                "labels": {
                    "luma.io/project": project_slug,
                    "luma.io/branch": slugify(branch) or "main",
                    "luma.io/builder": builder.name,
                    "luma.io/platform": arch_slug,
                },
                "annotations": {
                    "luma.io/replacement-key": replacement_key,
                    "luma.io/idempotency-key": idempotency_key or run_name,
                },
            },
            "spec": {
                "pipelineRef": {"name": "luma-paas-build-pipeline"},
                "podTemplate": {
                    "terminationGracePeriodSeconds": 3,
                },
                "timeouts": {
                    "pipeline": "45m",
                    "tasks": "30m",
                    "finally": "3m",
                },
                "params": [
                    {"name": "repo-url", "value": repo_url},
                    {"name": "revision", "value": revision},
                    {"name": "buildkit-addr", "value": builder.address},
                    {"name": "platform", "value": builder.platform},
                    {"name": "context-subpath", "value": context_subpath},
                    {"name": "dockerfile-path", "value": dockerfile_path},
                    {"name": "image-ref", "value": image_ref},
                    {"name": "cache-ref", "value": cache_ref},
                    {"name": "git-contexts-json", "value": json.dumps(git_contexts, sort_keys=True)},
                    {"name": "extra-contexts-flags", "value": extra_flags},
                ],
                "workspaces": [
                    {
                        "name": "shared-source",
                        "EmptyDir": {},
                    }
                ],
            },
        }

        # Track active concurrency on builder (fresh lookup after superseded cancellations)
        current_builder = self._builders[builder.name]
        status = "running" if current_builder.available_slots > 0 else "queued"
        if status == "running":
            self._claim_slot(current_builder.name)

        record: Dict[str, Any] = {
            "buildRunId": run_name,
            "project": project_slug,
            "repoUrl": repo_url,
            "revision": revision,
            "branch": branch,
            "platform": builder.platform,
            "builderNode": builder.name,
            "builderAddress": builder.address,
            "imageRef": image_ref,
            "cacheRef": cache_ref,
            "gitContexts": git_contexts,
            "status": status,
            "targetNode": resolved_node,
            "targetRegion": resolved_region,
            "replacementKey": replacement_key,
            "idempotencyKey": idempotency_key or run_name,
            "supersededRuns": superseded_ids,
            "createdAt": now,
            "updatedAt": now,
            "pipelineRunManifest": pipeline_run_manifest,
        }
        self._pipeline_runs[run_name] = record
        return record

    def cancel_build(self, build_run_id: str, *, reason: str = "Canceled by operator") -> Dict[str, Any]:
        """Hard-cancel a Tekton PipelineRun via CancelledRunFinally (<3s pod & BuildKit termination)."""
        record = self._pipeline_runs.get(build_run_id)
        if not isinstance(record, dict):
            raise LumaError(f"build run not found: {build_run_id}")
        current_status = str(record.get("status") or "")
        if current_status in TERMINAL_STATUSES:
            raise LumaError(
                f"cannot cancel build {build_run_id}: build is already in terminal state '{current_status}'"
            )
        now = int(time.time())
        if current_status == "running":
            self._release_slot(str(record.get("builderNode") or ""))

        manifest = record["pipelineRunManifest"]
        manifest.setdefault("spec", {})["status"] = "CancelledRunFinally"
        record.update(
            status="canceled",
            cancelReason=reason,
            canceledAt=now,
            completedAt=now,
            updatedAt=now,
            podTerminated=True,
            buildkitSessionAborted=True,
        )
        return record

    def complete_build(
        self,
        build_run_id: str,
        *,
        status: str = "succeeded",
        message: str = "",
    ) -> Dict[str, Any]:
        """Mark a build run as completed (succeeded or failed) and release its builder concurrency slot."""
        record = self._pipeline_runs.get(build_run_id)
        if not isinstance(record, dict):
            raise LumaError(f"build run not found: {build_run_id}")
        current_status = str(record.get("status") or "")
        if current_status in TERMINAL_STATUSES:
            raise LumaError(
                f"cannot complete build {build_run_id}: build is already in terminal state '{current_status}'"
            )
        if status not in {"succeeded", "failed"}:
            raise LumaError(f"invalid completion status: {status} (must be 'succeeded' or 'failed')")

        now = int(time.time())
        if current_status == "running":
            self._release_slot(str(record.get("builderNode") or ""))

        record.update(
            status=status,
            message=message,
            completedAt=now,
            updatedAt=now,
        )
        return record

    def get_build(self, build_run_id: str) -> Dict[str, Any] | None:
        return self._pipeline_runs.get(build_run_id)
