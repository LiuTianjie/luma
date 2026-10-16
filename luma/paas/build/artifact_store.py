"""Immutable Artifact and Release catalog with Zot OCI Registry retention protection."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from typing import Any, Dict, Iterable, Mapping, Sequence
import urllib.parse
import urllib.request

from ...errors import LumaError
from ...registry_management import RegistryHttpClient, validate_digest, validate_repository


def normalize_repository(value: Any) -> str:
    """Strip registry host and port (e.g. '100.66.177.70:5000/app' -> 'app') and validate OCI repository path."""
    raw = str(value or "").strip()
    if raw.startswith("http://"):
        raw = raw[7:]
    elif raw.startswith("https://"):
        raw = raw[8:]
    if "/" in raw:
        first_seg, rest = raw.split("/", 1)
        if ":" in first_seg or "." in first_seg or first_seg.lower() == "localhost":
            raw = rest
    return validate_repository(raw)


@dataclass(frozen=True)
class BuildStepTiming:
    step_id: str
    name: str
    duration_ms: int
    cached: bool
    started_at: int = 0
    completed_at: int = 0
    layer_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stepId": self.step_id,
            "name": self.name,
            "durationMs": self.duration_ms,
            "cached": self.cached,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "layerBytes": self.layer_bytes,
        }


@dataclass(frozen=True)
class ZotLayerMetadata:
    digest: str
    size_bytes: int
    command: str = ""
    media_type: str = "application/vnd.oci.image.layer.v1.tar+gzip"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest": self.digest,
            "sizeBytes": self.size_bytes,
            "command": self.command,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True)
class ServiceImageArtifact:
    service_name: str
    repository: str
    digest: str
    tag: str
    platform: str = "linux/amd64"
    size_bytes: int = 0
    cache_hit_ratio: float = 0.0
    step_timings: tuple[BuildStepTiming, ...] = ()
    layers: tuple[ZotLayerMetadata, ...] = ()

    def __post_init__(self) -> None:
        normalize_repository(self.repository)
        validate_digest(self.digest)
        if not self.service_name or not self.platform:
            raise LumaError("ServiceImageArtifact requires service_name and platform")

    @property
    def immutable_ref(self) -> str:
        return f"{self.repository}@{self.digest}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "serviceName": self.service_name,
            "repository": self.repository,
            "digest": self.digest,
            "tag": self.tag,
            "platform": self.platform,
            "sizeBytes": self.size_bytes,
            "cacheHitRatio": round(self.cache_hit_ratio, 4),
            "stepTimings": [step.to_dict() for step in self.step_timings],
            "layers": [layer.to_dict() for layer in self.layers],
            "immutableRef": self.immutable_ref,
        }


@dataclass(frozen=True)
class BuildArtifact:
    artifact_id: str
    project: str
    source_repo: str
    commit_sha: str
    branch: str
    manifest_kind: str
    manifest_snapshot: str
    images: Dict[str, ServiceImageArtifact]
    created_at: int
    build_duration_ms: int = 0
    builder_node: str = ""
    compose_snapshot: str = ""
    git_contexts: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.project:
            raise LumaError("BuildArtifact requires artifact_id and project")
        if self.manifest_kind not in {"service", "compose"}:
            raise LumaError(f"unsupported artifact manifest_kind: {self.manifest_kind}")
        if not self.images:
            raise LumaError("BuildArtifact must contain at least one service image")

    @property
    def overall_cache_hit_ratio(self) -> float:
        steps = [step for img in self.images.values() for step in img.step_timings]
        if steps:
            cached = sum(1 for step in steps if step.cached)
            return round(cached / len(steps), 4)
        ratios = [img.cache_hit_ratio for img in self.images.values()]
        return round(sum(ratios) / len(ratios), 4) if ratios else 0.0

    @property
    def total_size_bytes(self) -> int:
        return sum(img.size_bytes for img in self.images.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "project": self.project,
            "sourceRepo": self.source_repo,
            "commitSha": self.commit_sha,
            "branch": self.branch,
            "manifestKind": self.manifest_kind,
            "manifestSnapshot": self.manifest_snapshot,
            "composeSnapshot": self.compose_snapshot,
            "gitContexts": dict(self.git_contexts),
            "images": {name: img.to_dict() for name, img in sorted(self.images.items())},
            "createdAt": self.created_at,
            "buildDurationMs": self.build_duration_ms,
            "builderNode": self.builder_node,
            "cacheHitRatio": self.overall_cache_hit_ratio,
            "totalSizeBytes": self.total_size_bytes,
        }


@dataclass(frozen=True)
class ReleaseRecord:
    release_id: str
    project: str
    environment: str
    generation: int
    artifact_id: str
    status: str
    replicas_by_service: Dict[str, int]
    env_digest: str
    secret_keys: tuple[str, ...] = ()
    mounted_secret_files: Dict[str, str] = field(default_factory=dict)
    previous_release_id: str = ""
    created_at: int = 0
    updated_at: int = 0
    message: str = ""
    gateway_probe_ok: bool = True
    secrets: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "releaseId": self.release_id,
            "project": self.project,
            "environment": self.environment,
            "generation": self.generation,
            "artifactId": self.artifact_id,
            "status": self.status,
            "replicasByService": dict(self.replicas_by_service),
            "envDigest": self.env_digest,
            "secretKeys": list(self.secret_keys),
            "mountedSecretFiles": dict(self.mounted_secret_files),
            "previousReleaseId": self.previous_release_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "message": self.message,
            "gatewayProbeOk": self.gateway_probe_ok,
            "secrets": dict(self.secrets),
        }


def compute_release_diff(
    previous: ReleaseRecord | None,
    current: ReleaseRecord,
    *,
    artifacts: Mapping[str, BuildArtifact] | None = None,
) -> Dict[str, Any]:
    """Compute a human- and UI-friendly diff between two releases without exposing secret values."""
    if previous is None:
        return {
            "initialRelease": True,
            "artifactChanged": True,
            "previousArtifactId": "",
            "currentArtifactId": current.artifact_id,
            "envChanged": bool(current.secret_keys),
            "addedSecrets": list(current.secret_keys),
            "removedSecrets": [],
            "replicasChanged": dict(current.replicas_by_service),
            "serviceDigestDiffs": {},
            "summary": "Initial release",
        }

    artifact_changed = previous.artifact_id != current.artifact_id
    env_changed = previous.env_digest != current.env_digest
    prev_secrets = set(previous.secret_keys)
    curr_secrets = set(current.secret_keys)
    added_secrets = sorted(curr_secrets - prev_secrets)
    removed_secrets = sorted(prev_secrets - curr_secrets)

    replicas_changed: Dict[str, Dict[str, int]] = {}
    all_services = sorted(set(previous.replicas_by_service) | set(current.replicas_by_service))
    for svc in all_services:
        before = int(previous.replicas_by_service.get(svc, 1))
        after = int(current.replicas_by_service.get(svc, 1))
        if before != after:
            replicas_changed[svc] = {"from": before, "to": after}

    digest_diffs: Dict[str, Dict[str, str]] = {}
    if artifacts and artifact_changed:
        prev_art = artifacts.get(previous.artifact_id)
        curr_art = artifacts.get(current.artifact_id)
        if prev_art and curr_art:
            svc_names = sorted(set(prev_art.images) | set(curr_art.images))
            for svc in svc_names:
                d1 = prev_art.images[svc].digest if svc in prev_art.images else ""
                d2 = curr_art.images[svc].digest if svc in curr_art.images else ""
                if d1 != d2:
                    digest_diffs[svc] = {"from": d1, "to": d2}

    parts: list[str] = []
    if artifact_changed:
        parts.append(f"artifact {previous.artifact_id} -> {current.artifact_id}")
    if env_changed:
        parts.append("secrets/env updated")
    if replicas_changed:
        parts.append(
            "replicas "
            + ", ".join(f"{svc}:{change['from']}->{change['to']}" for svc, change in replicas_changed.items())
        )
    return {
        "initialRelease": False,
        "artifactChanged": artifact_changed,
        "previousArtifactId": previous.artifact_id,
        "currentArtifactId": current.artifact_id,
        "envChanged": env_changed,
        "addedSecrets": added_secrets,
        "removedSecrets": removed_secrets,
        "replicasChanged": replicas_changed,
        "serviceDigestDiffs": digest_diffs,
        "summary": "; ".join(parts) if parts else "No configuration changes (re-reconcile)",
    }


def compute_protected_digests(
    artifacts: Mapping[str, BuildArtifact],
    releases: Iterable[ReleaseRecord],
    *,
    rollback_window: int = 5,
) -> Dict[str, Dict[str, Any]]:
    """Determine every OCI digest protected by active deployments or rollback windows.

    Zot Registry's online GC retains tags matching ``^luma-pin-.*``. This function
    computes the exact set of digests that must remain pinned so neither active
    workloads nor promised instant rollbacks can ever lose their image blobs.
    """
    rollback_window = max(int(rollback_window), 1)
    by_target: Dict[tuple[str, str], list[ReleaseRecord]] = {}
    for rel in releases:
        if rel.status in {"healthy", "reconciling", "probing", "rolled_back"}:
            by_target.setdefault((rel.project, rel.environment), []).append(rel)

    protected: Dict[str, Dict[str, Any]] = {}

    def _mark(
        img: ServiceImageArtifact,
        *,
        project: str,
        environment: str,
        release_id: str,
        status: str,
        pin_tag: str,
    ) -> None:
        entry = protected.setdefault(
            img.digest,
            {
                "digest": img.digest,
                "repository": img.repository,
                "repositories": set(),
                "status": status,
                "projects": set(),
                "environments": set(),
                "releases": set(),
                "pinTags": set(),
                "repositoryPinTags": {},
            },
        )
        if status == "active":
            entry["status"] = "active"
        entry["repositories"].add(img.repository)
        entry["projects"].add(project)
        entry["environments"].add(environment)
        entry["releases"].add(release_id)
        entry["pinTags"].add(pin_tag)
        entry["repositoryPinTags"].setdefault(img.repository, set()).add(pin_tag)

    for (project, env), target_releases in by_target.items():
        ordered = sorted(target_releases, key=lambda r: (r.generation, r.created_at), reverse=True)
        active = next((r for r in ordered if r.status in {"healthy", "reconciling", "probing"}), None)
        if active and active.artifact_id in artifacts:
            art = artifacts[active.artifact_id]
            for img in art.images.values():
                _mark(
                    img,
                    project=project,
                    environment=env,
                    release_id=active.release_id,
                    status="active",
                    pin_tag=f"luma-pin-active-{project}-{env}",
                )
        healthy_history = [
            r for r in ordered
            if r.status in {"healthy", "rolled_back"} and (active is None or r.release_id != active.release_id)
        ][:rollback_window]
        for rel in healthy_history:
            art = artifacts.get(rel.artifact_id)
            if not art:
                continue
            for img in art.images.values():
                _mark(
                    img,
                    project=project,
                    environment=env,
                    release_id=rel.release_id,
                    status="rollback",
                    pin_tag=f"luma-pin-rollback-{project}-{env}-v{rel.generation}",
                )

    result: Dict[str, Dict[str, Any]] = {}
    for digest, data in protected.items():
        result[digest] = {
            "digest": digest,
            "repository": data["repository"],
            "repositories": sorted(data["repositories"]),
            "status": data["status"],
            "projects": sorted(data["projects"]),
            "environments": sorted(data["environments"]),
            "releases": sorted(data["releases"]),
            "pinTags": sorted(data["pinTags"]),
            "repositoryPinTags": {r: sorted(tags) for r, tags in data["repositoryPinTags"].items()},
        }
    return result


class RetentionSyncResult(list):
    """Dual-interface result: acts as a Sequence[str] of deleted tags and as a dict with deleted_tags/retained_tags."""

    def __init__(
        self,
        deleted_tags: Iterable[str],
        retained_tags: Iterable[str],
        protected_digests: int = 0,
        applied_pin_tags: int = 0,
    ) -> None:
        del_list = sorted(set(deleted_tags))
        ret_list = sorted(set(retained_tags))
        super().__init__(del_list)
        self.deleted_tags = del_list
        self.retained_tags = ret_list
        self.protected_digests = protected_digests
        self.applied_pin_tags = applied_pin_tags

    def __getitem__(self, item: Any) -> Any:
        if item == "deleted_tags":
            return self.deleted_tags
        if item == "retained_tags":
            return self.retained_tags
        if item == "protected_digests":
            return self.protected_digests
        if item == "applied_pin_tags":
            return self.applied_pin_tags
        return super().__getitem__(item)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "deleted_tags":
            return self.deleted_tags
        if key == "retained_tags":
            return self.retained_tags
        if key == "protected_digests":
            return self.protected_digests
        if key == "applied_pin_tags":
            return self.applied_pin_tags
        return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deleted_tags": self.deleted_tags,
            "retained_tags": self.retained_tags,
            "protected_digests": self.protected_digests,
            "applied_pin_tags": self.applied_pin_tags,
        }


class ArtifactCatalog:
    """In-memory and JSON-serializable catalog for BuildArtifacts and ReleaseRecords."""

    def __init__(self, state: Dict[str, Any] | None = None) -> None:
        self._state = state if state is not None else {}
        self._state.setdefault("paasArtifacts", {})
        self._state.setdefault("paasReleases", {})

    def record_artifact(self, artifact: BuildArtifact) -> BuildArtifact:
        self._state["paasArtifacts"][artifact.artifact_id] = artifact.to_dict()
        return artifact

    def get_artifact(self, artifact_id: str) -> BuildArtifact | None:
        raw = (self._state.get("paasArtifacts") or {}).get(artifact_id)
        if not isinstance(raw, dict):
            return None
        return _artifact_from_dict(raw)

    def list_artifacts(self, *, project: str = "") -> list[BuildArtifact]:
        items: list[BuildArtifact] = []
        for raw in (self._state.get("paasArtifacts") or {}).values():
            if not isinstance(raw, dict):
                continue
            if project and raw.get("project") != project:
                continue
            items.append(_artifact_from_dict(raw))
        return sorted(items, key=lambda a: (a.created_at, a.artifact_id), reverse=True)

    def record_release(self, release: ReleaseRecord) -> ReleaseRecord:
        self._state["paasReleases"][release.release_id] = release.to_dict()
        return release

    def get_release(self, release_id: str) -> ReleaseRecord | None:
        raw = (self._state.get("paasReleases") or {}).get(release_id)
        if not isinstance(raw, dict):
            return None
        return _release_from_dict(raw)

    def list_releases(self, *, project: str = "", environment: str = "") -> list[ReleaseRecord]:
        items: list[ReleaseRecord] = []
        for raw in (self._state.get("paasReleases") or {}).values():
            if not isinstance(raw, dict):
                continue
            if project and raw.get("project") != project:
                continue
            if environment and raw.get("environment") != environment:
                continue
            items.append(_release_from_dict(raw))
        return sorted(items, key=lambda r: (r.generation, r.created_at), reverse=True)

    def next_generation(self, project: str, environment: str) -> int:
        existing = self.list_releases(project=project, environment=environment)
        return (max((r.generation for r in existing), default=0)) + 1

    def normalize_repository(self, repository: str) -> str:
        return normalize_repository(repository)

    def protected_digests(self, *, rollback_window: int = 5) -> Dict[str, Dict[str, Any]]:
        artifacts = {art.artifact_id: art for art in self.list_artifacts()}
        releases = self.list_releases()
        return compute_protected_digests(artifacts, releases, rollback_window=rollback_window)

    def protected_pin_tags(
        self,
        project: str,
        environment: str = "prod",
        rollback_window: int = 5,
    ) -> Sequence[str]:
        """Return all active and rollback pin tags currently protected for project and environment."""
        digests = self.protected_digests(rollback_window=rollback_window)
        pins: set[str] = set()
        for info in digests.values():
            if project in info.get("projects", []) and environment in info.get("environments", []):
                for tag in info.get("pinTags", []):
                    pins.add(tag)
        return sorted(pins)

    def sync_retention_pins(
        self,
        releases: Sequence[ReleaseRecord] | None = None,
        rollback_window: int = 5,
        client: ZotRegistryClient | None = None,
        zot_client: ZotRegistryClient | None = None,
    ) -> RetentionSyncResult:
        """Synchronize retention pins and prune obsolete tags beyond the rollback window."""
        actual_client = client or zot_client
        rollback_window = max(int(rollback_window), 1)
        rel_list = list(releases if releases is not None else self.list_releases())
        if not rel_list:
            return RetentionSyncResult([], [])

        by_target: Dict[tuple[str, str], list[ReleaseRecord]] = {}
        for r in rel_list:
            by_target.setdefault((r.project, r.environment), []).append(r)

        all_retained: set[str] = set()
        all_deleted: set[str] = set()

        for (proj, env), target_releases in by_target.items():
            ordered = sorted(target_releases, key=lambda r: (r.generation, r.created_at), reverse=True)
            active = next((r for r in ordered if r.status in {"healthy", "reconciling", "probing"}), None)
            target_retained: set[str] = set()
            if active:
                target_retained.add(f"luma-pin-active-{proj}-{env}")
            healthy_history = [
                r for r in ordered
                if r.status in {"healthy", "rolled_back"} and (active is None or r.release_id != active.release_id)
            ][:rollback_window]
            for r in healthy_history:
                target_retained.add(f"luma-pin-rollback-{proj}-{env}-v{r.generation}")
            all_retained.update(target_retained)

            older_releases = [
                r for r in ordered
                if (active is None or r.release_id != active.release_id) and r not in healthy_history
            ]
            for r in older_releases:
                tag = f"luma-pin-rollback-{proj}-{env}-v{r.generation}"
                if tag not in target_retained:
                    all_deleted.add(tag)

            if actual_client is not None:
                project_artifacts = self.list_artifacts(project=proj)
                repos: set[str] = set()
                for art in project_artifacts:
                    for img in art.images.values():
                        repos.add(img.repository)
                pin_regex = re.compile(
                    rf"^luma-pin-(?:(?:active|rollback)-{re.escape(proj)}-{re.escape(env)}(?:-v\d+)?|{re.escape(proj)}-{re.escape(env)}-.*)$"
                )
                for repo in repos:
                    try:
                        existing_tags = actual_client.list_tags(repo)
                    except LumaError:
                        continue
                    for tag in existing_tags:
                        if pin_regex.fullmatch(tag) and tag not in target_retained:
                            all_deleted.add(tag)
                            try:
                                actual_client.delete_tag(repo, tag)
                            except LumaError:
                                pass

        applied_pin_tags = 0
        protected = self.protected_digests(rollback_window=rollback_window)
        if actual_client is not None:
            sync_res = actual_client.sync_retention_pins(protected)
            applied_pin_tags = int(sync_res.get("appliedPinTags", 0))

        return RetentionSyncResult(
            sorted(all_deleted),
            sorted(all_retained),
            protected_digests=len(protected),
            applied_pin_tags=applied_pin_tags,
        )


class ZotRegistryClient:
    """Client for Zot OCI Registry v1.1 + Search GraphQL extension + Zero-Downtime Pinning."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str = "",
        password: str = "",
        timeout: float = 15.0,
        http_client: RegistryHttpClient | None = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.http = http_client or RegistryHttpClient(
            self.base_url,
            username=username,
            password=password,
            timeout=timeout,
        )

    def normalize_repository(self, repository: str) -> str:
        return normalize_repository(repository)

    def delete_tag(self, repository: str, tag: str) -> None:
        """Delete an obsolete tag reference from Zot OCI Registry so online GC can reclaim blobs."""
        repository = normalize_repository(repository)
        tag = str(tag or "").strip()
        if not tag or any(c in tag for c in ("/", "?", "#", "\0", "\n", "\r")):
            raise LumaError(f"invalid registry tag: {tag}")
        encoded_repo = urllib.parse.quote(repository, safe="/")
        encoded_tag = urllib.parse.quote(tag, safe="")
        self.http.request(
            "DELETE",
            f"/v2/{encoded_repo}/manifests/{encoded_tag}",
            expected=(200, 202, 204, 404),
        )

    def list_tags(self, repository: str) -> list[str]:
        return self.http.tags(normalize_repository(repository))

    def pin_digest(self, repository: str, digest: str, pin_tag: str) -> None:
        """Retag a manifest digest in-place via OCI Manifest PUT without pulling blobs."""
        repository = normalize_repository(repository)
        digest = validate_digest(digest)
        _, response = self.http.manifest(repository, digest)
        content_type = (
            response.headers.get("Content-Type")
            or response.headers.get("content-type")
            or "application/vnd.oci.image.manifest.v1+json"
        ).split(";", 1)[0].strip()
        encoded_repo = urllib.parse.quote(repository, safe="/")
        self.http.request(
            "PUT",
            f"/v2/{encoded_repo}/manifests/{urllib.parse.quote(pin_tag, safe='')}",
            content_type=content_type,
            body=response.body,
            expected=(200, 201, 202),
        )

    def sync_retention_pins(
        self,
        protected: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Synchronize retention pin tags across all repositories independently with deduplication."""
        pinned_count = 0
        seen_pins: set[tuple[str, str, str]] = set()
        for digest, info in protected.items():
            repo_pins = info.get("repositoryPinTags")
            if isinstance(repo_pins, Mapping) and repo_pins:
                for repo, tags in repo_pins.items():
                    for tag in tags:
                        key = (str(repo), digest, str(tag))
                        if key not in seen_pins:
                            seen_pins.add(key)
                            self.pin_digest(str(repo), digest, str(tag))
                            pinned_count += 1
            else:
                repos = info.get("repositories") or ([info.get("repository")] if info.get("repository") else [])
                for repo in repos:
                    for tag in info.get("pinTags") or []:
                        key = (str(repo), digest, str(tag))
                        if key not in seen_pins:
                            seen_pins.add(key)
                            self.pin_digest(str(repo), digest, str(tag))
                            pinned_count += 1
        return {
            "protectedDigests": len(protected),
            "appliedPinTags": pinned_count,
            "syncedAt": int(time.time()),
        }

    def inspect_image_layers(self, repository: str, digest: str) -> list[ZotLayerMetadata]:
        """Inspect OCI image layers and history commands for the Web Console Layer Explorer."""
        repository = normalize_repository(repository)
        digest = validate_digest(digest)
        manifest_payload, _ = self.http.manifest(repository, digest)
        layers_raw = manifest_payload.get("layers") or []
        config_desc = manifest_payload.get("config") or {}
        config_digest = str(config_desc.get("digest") or "")
        commands: list[str] = []
        if config_digest.startswith("sha256:"):
            try:
                config_blob = self.http.blob_json(repository, config_digest)
                for hist in config_blob.get("history") or []:
                    if isinstance(hist, dict) and not hist.get("empty_layer"):
                        commands.append(str(hist.get("created_by") or "").strip())
            except LumaError:
                commands = []

        layers: list[ZotLayerMetadata] = []
        for idx, item in enumerate(layers_raw):
            if not isinstance(item, dict):
                continue
            layer_digest = str(item.get("digest") or "")
            if not layer_digest:
                continue
            cmd = commands[idx] if idx < len(commands) else ""
            layers.append(
                ZotLayerMetadata(
                    digest=layer_digest,
                    size_bytes=int(item.get("size") or 0),
                    command=cmd,
                    media_type=str(item.get("mediaType") or "application/vnd.oci.image.layer.v1.tar+gzip"),
                )
            )
        return layers


def _artifact_from_dict(raw: Mapping[str, Any]) -> BuildArtifact:
    images: Dict[str, ServiceImageArtifact] = {}
    for svc_name, img_raw in (raw.get("images") or {}).items():
        if not isinstance(img_raw, dict):
            continue
        steps = tuple(
            BuildStepTiming(
                step_id=str(s.get("stepId") or ""),
                name=str(s.get("name") or ""),
                duration_ms=int(s.get("durationMs") or 0),
                cached=bool(s.get("cached")),
                started_at=int(s.get("startedAt") or 0),
                completed_at=int(s.get("completedAt") or 0),
                layer_bytes=int(s.get("layerBytes") or 0),
            )
            for s in (img_raw.get("stepTimings") or [])
            if isinstance(s, dict)
        )
        layers = tuple(
            ZotLayerMetadata(
                digest=str(l.get("digest") or ""),
                size_bytes=int(l.get("sizeBytes") or 0),
                command=str(l.get("command") or ""),
                media_type=str(l.get("mediaType") or "application/vnd.oci.image.layer.v1.tar+gzip"),
            )
            for l in (img_raw.get("layers") or [])
            if isinstance(l, dict)
        )
        images[str(svc_name)] = ServiceImageArtifact(
            service_name=str(img_raw.get("serviceName") or svc_name),
            repository=str(img_raw.get("repository") or ""),
            digest=str(img_raw.get("digest") or ""),
            tag=str(img_raw.get("tag") or ""),
            platform=str(img_raw.get("platform") or "linux/amd64"),
            size_bytes=int(img_raw.get("sizeBytes") or 0),
            cache_hit_ratio=float(img_raw.get("cacheHitRatio") or 0.0),
            step_timings=steps,
            layers=layers,
        )
    return BuildArtifact(
        artifact_id=str(raw.get("artifactId") or ""),
        project=str(raw.get("project") or ""),
        source_repo=str(raw.get("sourceRepo") or ""),
        commit_sha=str(raw.get("commitSha") or ""),
        branch=str(raw.get("branch") or "main"),
        manifest_kind=str(raw.get("manifestKind") or "service"),
        manifest_snapshot=str(raw.get("manifestSnapshot") or ""),
        compose_snapshot=str(raw.get("composeSnapshot") or ""),
        git_contexts=dict(raw.get("gitContexts") or {}),
        images=images,
        created_at=int(raw.get("createdAt") or 0),
        build_duration_ms=int(raw.get("buildDurationMs") or 0),
        builder_node=str(raw.get("builderNode") or ""),
    )


def _release_from_dict(raw: Mapping[str, Any]) -> ReleaseRecord:
    return ReleaseRecord(
        release_id=str(raw.get("releaseId") or ""),
        project=str(raw.get("project") or ""),
        environment=str(raw.get("environment") or "prod"),
        generation=int(raw.get("generation") or 1),
        artifact_id=str(raw.get("artifactId") or ""),
        status=str(raw.get("status") or "healthy"),
        replicas_by_service={str(k): int(v) for k, v in (raw.get("replicasByService") or {}).items()},
        env_digest=str(raw.get("envDigest") or ""),
        secret_keys=tuple(str(k) for k in (raw.get("secretKeys") or [])),
        mounted_secret_files={str(k): str(v) for k, v in (raw.get("mountedSecretFiles") or {}).items()},
        previous_release_id=str(raw.get("previousReleaseId") or ""),
        created_at=int(raw.get("createdAt") or 0),
        updated_at=int(raw.get("updatedAt") or 0),
        message=str(raw.get("message") or ""),
        gateway_probe_ok=bool(raw.get("gatewayProbeOk", True)),
        secrets={str(k): str(v) for k, v in (raw.get("secrets") or {}).items()},
    )


def hash_secret_map(secrets: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(secrets.items())), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
