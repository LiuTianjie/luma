"""Flux HelmRelease coordinator: zero-rebuild deploy, environment promotion, and 5s rollback."""
from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Dict, Mapping

from ...errors import LumaError
from ...service import slugify
from ..build.artifact_store import (
    ArtifactCatalog,
    ReleaseRecord,
    ZotRegistryClient,
    compute_release_diff,
    hash_secret_map,
)
from .helm_compiler import CompiledWorkload, compile_compose_release, compile_service_release


VALID_ENVIRONMENTS: set[str] = {"dev", "staging", "prod", "preview"}


class FluxReconciler:
    """Manages declarative Releases on K3s via Flux HelmRelease with automatic Digest protection."""

    def __init__(
        self,
        catalog: ArtifactCatalog,
        *,
        zot_client: ZotRegistryClient | None = None,
        rollback_window: int = 5,
    ) -> None:
        self.catalog = catalog
        self.zot_client = zot_client
        self.rollback_window = max(int(rollback_window), 1)
        self._compiled_by_release: Dict[str, CompiledWorkload] = {}

    def deploy_artifact(
        self,
        artifact_id: str,
        *,
        environment: str = "prod",
        replicas_override: Mapping[str, int] | None = None,
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        expected_min_generation: int = 0,
        gateway_probe: Callable[[CompiledWorkload], bool] | None = None,
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Deploy an immutable BuildArtifact without rebuilding any container image."""
        if environment not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        artifact = self.catalog.get_artifact(artifact_id)
        if artifact is None:
            raise LumaError(f"artifact not found: {artifact_id}")

        project = slugify(artifact.project)
        existing_releases = self.catalog.list_releases(project=project, environment=environment)
        latest = existing_releases[0] if existing_releases else None
        next_gen = (latest.generation + 1) if latest else 1
        if expected_min_generation and next_gen < expected_min_generation:
            raise LumaError(
                f"stale release rejected: next generation {next_gen} is older than {expected_min_generation}"
            )

        clean_secrets = {str(k): str(v) for k, v in (secrets or {}).items()}
        clean_mounted_files = {str(k): str(v) for k, v in (mounted_secret_files or {}).items()}

        if artifact.manifest_kind == "compose":
            compiled = compile_compose_release(
                artifact=artifact,
                environment=environment,
                generation=next_gen,
                replicas_override=replicas_override,
                secrets=clean_secrets,
                mounted_secret_files=clean_mounted_files,
            )
        else:
            single_replicas = None
            if replicas_override and project in replicas_override:
                single_replicas = int(replicas_override[project])
            elif replicas_override and len(replicas_override) == 1:
                single_replicas = int(next(iter(replicas_override.values())))
            compiled = compile_service_release(
                artifact=artifact,
                environment=environment,
                generation=next_gen,
                replicas_override=single_replicas,
                secrets=clean_secrets,
                mounted_secret_files=clean_mounted_files,
            )

        now = int(time.time())
        seed = f"{project}:{environment}:v{next_gen}:{artifact_id}:{now}"
        release_id = f"rel-{project}-{environment}-v{next_gen}-{hashlib.sha256(seed.encode()).hexdigest()[:6]}"
        probe_ok = True if gateway_probe is None else bool(gateway_probe(compiled))

        if not probe_ok and latest is not None:
            # Flux remediation.strategy=rollback automatically restores the previous healthy HelmRelease
            failed_record = ReleaseRecord(
                release_id=release_id,
                project=project,
                environment=environment,
                generation=next_gen,
                artifact_id=artifact_id,
                status="rolled_back",
                replicas_by_service=compiled.replicas_by_service,
                env_digest=hash_secret_map(clean_secrets),
                secret_keys=tuple(sorted(clean_secrets.keys())),
                mounted_secret_files=clean_mounted_files,
                previous_release_id=latest.release_id,
                created_at=now,
                updated_at=now,
                message=f"Gateway probe failed; Flux automatically rolled back to {latest.release_id} ({latest.artifact_id})",
                gateway_probe_ok=False,
                secrets=clean_secrets,
            )
            self.catalog.record_release(failed_record)
            self._compiled_by_release[release_id] = compiled
            self._sync_pins()
            return failed_record, compiled

        status = "healthy" if probe_ok else "failed"
        record = ReleaseRecord(
            release_id=release_id,
            project=project,
            environment=environment,
            generation=next_gen,
            artifact_id=artifact_id,
            status=status,
            replicas_by_service=compiled.replicas_by_service,
            env_digest=hash_secret_map(clean_secrets),
            secret_keys=tuple(sorted(clean_secrets.keys())),
            mounted_secret_files=clean_mounted_files,
            previous_release_id=latest.release_id if latest else "",
            created_at=now,
            updated_at=now,
            message=f"Deployed {artifact_id} (generation v{next_gen}) via Flux HelmRelease",
            gateway_probe_ok=probe_ok,
            secrets=clean_secrets,
        )
        self.catalog.record_release(record)
        self._compiled_by_release[release_id] = compiled
        self._sync_pins()
        return record, compiled

    def promote_artifact(
        self,
        *,
        project: str,
        from_env: str = "staging",
        to_env: str = "prod",
        target_env: str | None = None,
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        target_secrets: Mapping[str, str] | None = None,
        target_mounted_secret_files: Mapping[str, str] | None = None,
        replicas_override: Mapping[str, int] | None = None,
        gateway_probe: Callable[[CompiledWorkload], bool] | None = None,
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Promote the active healthy BuildArtifact from one environment to another without rebuilding."""
        dest_env = target_env if target_env is not None else to_env
        if from_env not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid source environment {from_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )
        if dest_env not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid target environment {dest_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        project_slug = slugify(project)
        source_releases = [
            r
            for r in self.catalog.list_releases(project=project_slug, environment=from_env)
            if r.status == "healthy"
        ]
        if not source_releases:
            raise LumaError(f"no healthy release found for {project_slug} in {from_env} to promote")
        candidate = source_releases[0]

        target_releases = [
            r
            for r in self.catalog.list_releases(project=project_slug, environment=dest_env)
            if r.status == "healthy"
        ]
        active_target = target_releases[0] if target_releases else None

        explicit_secrets = target_secrets if target_secrets is not None else secrets
        if explicit_secrets is not None:
            resolved_secrets = dict(explicit_secrets)
        elif active_target is not None:
            resolved_secrets = dict(getattr(active_target, "secrets", {}) or {})
        else:
            resolved_secrets = dict(getattr(candidate, "secrets", {}) or {})

        explicit_mounted = (
            target_mounted_secret_files
            if target_mounted_secret_files is not None
            else mounted_secret_files
        )
        if explicit_mounted is not None:
            resolved_mounted_files = dict(explicit_mounted)
        elif active_target is not None:
            resolved_mounted_files = dict(getattr(active_target, "mounted_secret_files", {}) or {})
        else:
            resolved_mounted_files = dict(getattr(candidate, "mounted_secret_files", {}) or {})

        effective_replicas = replicas_override or candidate.replicas_by_service

        return self.deploy_artifact(
            artifact_id=candidate.artifact_id,
            environment=dest_env,
            replicas_override=effective_replicas,
            secrets=resolved_secrets,
            mounted_secret_files=resolved_mounted_files,
            gateway_probe=gateway_probe,
        )

    def scale_service(
        self,
        *,
        project: str,
        service_name: str,
        replicas: int,
        environment: str = "prod",
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Scale an individual service (e.g. video-worker) in <5s without rebuilding or restarting sibling services."""
        if replicas < 0 or replicas > 64:
            raise LumaError("replicas must be between 0 and 64")
        if environment not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )
        project_slug = slugify(project)
        active = next(
            (
                r
                for r in self.catalog.list_releases(project=project_slug, environment=environment)
                if r.status == "healthy"
            ),
            None,
        )
        if active is None:
            raise LumaError(f"no active release for {project_slug} in {environment}")
        updated_replicas = dict(active.replicas_by_service)
        updated_replicas[service_name] = int(replicas)
        return self.deploy_artifact(
            artifact_id=active.artifact_id,
            environment=environment,
            replicas_override=updated_replicas,
            secrets=dict(getattr(active, "secrets", {}) or {}),
            mounted_secret_files=dict(active.mounted_secret_files),
        )

    def rollback_release(
        self,
        *,
        project: str,
        environment: str = "prod",
        target_release_id: str = "",
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        gateway_probe: Callable[[CompiledWorkload], bool] | None = None,
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
        """Instantly roll back to the previous (or target) healthy release's immutable Artifact digest."""
        if environment not in VALID_ENVIRONMENTS:
            raise LumaError(
                f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
            )
        project_slug = slugify(project)
        history = [
            r
            for r in self.catalog.list_releases(project=project_slug, environment=environment)
            if r.status == "healthy"
        ]
        if not history:
            raise LumaError(f"no healthy release history found for {project_slug} in {environment}")

        active = history[0]
        if target_release_id:
            target = next((r for r in history if r.release_id == target_release_id), None)
            if target is None:
                raise LumaError(f"target release {target_release_id} not found for {project_slug}")
        else:
            if len(history) < 2:
                raise LumaError(f"no previous release available to roll back {project_slug} in {environment}")
            target = history[1]

        if target.release_id == active.release_id:
            raise LumaError("cannot rollback to currently active release")

        resolved_secrets = (
            dict(secrets)
            if secrets is not None
            else dict(getattr(target, "secrets", {}) or {})
        )
        resolved_mounted_files = (
            dict(mounted_secret_files)
            if mounted_secret_files is not None
            else dict(getattr(target, "mounted_secret_files", {}) or {})
        )

        return self.deploy_artifact(
            artifact_id=target.artifact_id,
            environment=environment,
            replicas_override=target.replicas_by_service,
            secrets=resolved_secrets,
            mounted_secret_files=resolved_mounted_files,
            gateway_probe=gateway_probe,
        )

    def release_timeline_with_diffs(
        self,
        *,
        project: str,
        environment: str = "prod",
    ) -> list[Dict[str, Any]]:
        project_slug = slugify(project)
        releases = list(reversed(self.catalog.list_releases(project=project_slug, environment=environment)))
        artifacts = {a.artifact_id: a for a in self.catalog.list_artifacts(project=project_slug)}
        timeline: list[Dict[str, Any]] = []
        prev: ReleaseRecord | None = None
        for rel in releases:
            diff = compute_release_diff(prev, rel, artifacts=artifacts)
            timeline.append({**rel.to_dict(), "diff": diff})
            prev = rel
        timeline.reverse()
        return timeline

    def _sync_pins(self) -> Dict[str, Any]:
        protected = self.catalog.protected_digests(rollback_window=self.rollback_window)
        if self.zot_client is not None:
            return self.zot_client.sync_retention_pins(protected)
        return {"protectedDigests": len(protected), "appliedPinTags": 0}
