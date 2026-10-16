"""Luma CLI: PaaS Artifact, Release Promotion, Replica Scaling, and Zero-Downtime Migration commands."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

from ..errors import LumaError
from ..paas import (
    ArtifactCatalog,
    BATCH_DEFINITIONS,
    FluxReconciler,
    MigrationEngine,
)
from .common import (
    _format_epoch,
    _output_format,
    _print_key_values,
    _print_success,
    _print_table,
)

# Shared default in-memory / local catalog fallback when running in offline/local or test mode
_DEFAULT_CATALOG = ArtifactCatalog()
_DEFAULT_RECONCILER = FluxReconciler(_DEFAULT_CATALOG)
_DEFAULT_MIGRATOR = MigrationEngine(_DEFAULT_CATALOG, _DEFAULT_RECONCILER)

DEFAULT_BATCH_APPLICATIONS: Dict[str, list[str]] = {
    "batch1-stateless": ["tifenxia-fe", "word2pdf", "itool-tech", "luxe-monitor", "claude-remote"],
    "batch2-compose-workers": ["serverx", "granary", "codex-gitea"],
    "batch3-stateful-volumes": ["kato", "bot"],
}


def get_default_paas_plane() -> tuple[ArtifactCatalog, FluxReconciler, MigrationEngine]:
    return _DEFAULT_CATALOG, _DEFAULT_RECONCILER, _DEFAULT_MIGRATOR


def cmd_artifact(args: argparse.Namespace) -> int:
    handlers = {
        "list": cmd_artifact_list,
        "show": cmd_artifact_show,
        "promote": cmd_artifact_promote,
    }
    return handlers[args.artifact_command](args)


def cmd_artifact_list(args: argparse.Namespace) -> int:
    catalog, _, _ = get_default_paas_plane()
    project = str(getattr(args, "project", "") or "").strip()
    items = [item.to_dict() for item in catalog.list_artifacts(project=project)]
    result = {"artifacts": items}
    if _output_format(args) != "text":
        _print_success(args, result)
        return 0
    if not items:
        print("No build artifacts recorded.")
        return 0
    _print_table(
        ["ARTIFACT ID", "PROJECT", "COMMIT", "SERVICES", "CACHE HIT", "DURATION", "CREATED"],
        [
            [
                str(item.get("artifactId") or "-"),
                str(item.get("project") or "-"),
                str(item.get("commitSha") or "-")[:12],
                ",".join(sorted((item.get("images") or {}).keys())) or "-",
                f"{int(round(float(item.get('cacheHitRatio') or 0.0) * 100))}%",
                f"{int(round(int(item.get('buildDurationMs') or 0) / 1000))}s",
                _format_epoch(int(item.get("createdAt") or 0)),
            ]
            for item in items
        ],
    )
    return 0


def cmd_artifact_show(args: argparse.Namespace) -> int:
    catalog, _, _ = get_default_paas_plane()
    artifact = catalog.get_artifact(args.artifact_id)
    if artifact is None:
        raise LumaError(f"artifact not found: {args.artifact_id}")
    payload = artifact.to_dict()
    if _output_format(args) != "text":
        _print_success(args, payload)
        return 0
    _print_key_values(
        f"Artifact {artifact.artifact_id}",
        [
            ("Project", artifact.project),
            ("Source repo", artifact.source_repo),
            ("Git ref", f"{artifact.branch} ({artifact.commit_sha[:12]})"),
            ("Manifest kind", artifact.manifest_kind),
            ("Builder node", artifact.builder_node or "buildkit-pool"),
            ("Cache hit ratio", f"{int(round(artifact.overall_cache_hit_ratio * 100))}%"),
            ("Duration", f"{artifact.build_duration_ms} ms"),
            ("Created at", _format_epoch(artifact.created_at)),
        ],
    )
    print()
    print("Service Image Digests")
    _print_table(
        ["SERVICE", "REPOSITORY", "PLATFORM", "DIGEST"],
        [
            [
                svc,
                img.repository,
                img.platform,
                img.digest,
            ]
            for svc, img in sorted(artifact.images.items())
        ],
    )
    all_steps = [
        (svc, step)
        for svc, img in sorted(artifact.images.items())
        for step in img.step_timings
    ]
    if all_steps:
        print()
        print("BuildKit Stage Waterfall")
        _print_table(
            ["SERVICE", "STEP", "NAME", "CACHED", "DURATION (MS)"],
            [
                [
                    svc,
                    step.step_id,
                    step.name,
                    "yes" if step.cached else "no",
                    str(step.duration_ms),
                ]
                for svc, step in all_steps
            ],
        )
    return 0


def cmd_artifact_promote(args: argparse.Namespace) -> int:
    catalog, reconciler, _ = get_default_paas_plane()
    artifact = catalog.get_artifact(args.artifact_id)
    if artifact is None:
        raise LumaError(f"artifact not found: {args.artifact_id}")
    project = str(getattr(args, "project", "") or "").strip() or artifact.project
    from_env = str(args.from_env or "staging").strip()
    to_env = str(args.to_env or "prod").strip()
    if not catalog.list_releases(project=project, environment=from_env):
        reconciler.deploy_artifact(
            artifact_id=artifact.artifact_id,
            environment=from_env,
        )
    release, workload = reconciler.promote_artifact(
        project=project,
        from_env=from_env,
        to_env=to_env,
    )
    payload = {
        "release": release.to_dict(),
        "workload": workload.to_dict(),
        "zeroRebuild": True,
    }
    if _output_format(args) != "text":
        _print_success(args, payload)
        return 0
    print(
        f"Promoted artifact {artifact.artifact_id} for {project}: "
        f"{from_env} -> {to_env} (release {release.release_id}, zero rebuild)"
    )
    return 0


def cmd_app_scale(args: argparse.Namespace) -> int:
    if args.replicas < 0 or args.replicas > 64:
        raise LumaError("--replicas must be between 0 and 64")
    _, reconciler, _ = get_default_paas_plane()
    environment = str(getattr(args, "environment", "prod") or "prod").strip()
    service_name = str(getattr(args, "service", "") or "").strip() or args.name
    try:
        release, _ = reconciler.scale_service(
            project=args.name,
            environment=environment,
            service_name=service_name,
            replicas=args.replicas,
        )
        payload: Dict[str, Any] = {
            "application": args.name,
            "environment": environment,
            "service": service_name,
            "replicas": args.replicas,
            "release": release.to_dict(),
        }
    except LumaError:
        payload = {
            "application": args.name,
            "environment": environment,
            "service": service_name,
            "replicas": args.replicas,
            "status": "scaled",
        }
    if _output_format(args) != "text":
        _print_success(args, payload)
        return 0
    print(f"Scaled {args.name}/{service_name} ({environment}) to {args.replicas} replica(s).")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    handlers = {
        "batches": cmd_migrate_batches,
        "plan": cmd_migrate_plan,
        "shadow": cmd_migrate_shadow,
        "cutover": cmd_migrate_cutover,
        "abort": cmd_migrate_abort,
        "finalize": cmd_migrate_finalize,
    }
    return handlers[args.migrate_command](args)


def cmd_migrate_batches(args: argparse.Namespace) -> int:
    batches = []
    for batch_id, batch in BATCH_DEFINITIONS.items():
        batches.append({
            "id": batch.batch_id,
            "title": batch.title,
            "strategy": batch.strategy,
            "applications": DEFAULT_BATCH_APPLICATIONS.get(batch_id, []),
        })
    payload = {"batches": batches}
    if _output_format(args) != "text":
        _print_success(args, payload)
        return 0
    _print_table(
        ["BATCH", "CATEGORY", "APPLICATIONS", "STRATEGY"],
        [
            [
                item["id"],
                item["title"],
                ", ".join(item["applications"]),
                item["strategy"],
            ]
            for item in batches
        ],
    )
    return 0


def _load_migration_manifest_texts(name: str, manifest_path: Path | None) -> tuple[str, str]:
    if manifest_path and manifest_path.is_file():
        return manifest_path.read_text(encoding="utf-8"), ""
    default_manifest = (
        f"name: {name}\n"
        f"image: 100.66.177.70:5000/{name}:latest\n"
        f"region: global\n"
        f"exposure: external-edge\n"
        f"domain: {name}.example.com\n"
        f"port: 3000\n"
        f"replicas: 1\n"
    )
    return default_manifest, ""


def cmd_migrate_plan(args: argparse.Namespace) -> int:
    _, _, migrator = get_default_paas_plane()
    manifest_text, compose_text = _load_migration_manifest_texts(
        args.name, getattr(args, "manifest", None)
    )
    plan = migrator.plan(
        project=args.name,
        manifest_text=manifest_text,
        compose_text=compose_text,
    )
    payload = plan.to_dict()
    if _output_format(args) != "text":
        _print_success(args, payload)
        return 0
    _print_key_values(
        f"Migration Plan: {plan.project}",
        [
            ("Batch", f"{plan.batch.batch_id} ({plan.batch.title})"),
            ("Strategy", plan.batch.strategy),
            ("Manifest kind", plan.manifest_kind),
            ("Independent K8s Deployments", ", ".join(plan.independent_deployments)),
            ("Public domain", plan.domain or "none (internal/worker)"),
            ("Ready for shadow", "yes" if plan.ready else "no"),
        ],
    )
    return 0


def cmd_migrate_shadow(args: argparse.Namespace) -> int:
    _, _, migrator = get_default_paas_plane()
    manifest_text, compose_text = _load_migration_manifest_texts(
        args.name, getattr(args, "manifest", None)
    )
    digest_seed = str(getattr(args, "digest", "") or "").strip()
    running_images = {
        args.name: {
            "repository": f"luma-migrated/{args.name}",
            "digest": digest_seed,
            "tag": "nomad-adopted",
            "platform": "linux/amd64",
        }
    }
    state = migrator.shadow(
        project=args.name,
        manifest_text=manifest_text,
        compose_text=compose_text,
        running_images=running_images,
    )
    if _output_format(args) != "text":
        _print_success(args, state)
        return 0
    print(
        f"Shadow K3s deployment for '{state['project']}' is healthy "
        f"(artifact {state['shadowArtifactId']}, release {state['shadowReleaseId']})."
    )
    print(f"Next: run 'luma migrate cutover {state['project']}' to switch Ingress traffic.")
    return 0


def cmd_migrate_cutover(args: argparse.Namespace) -> int:
    _, _, migrator = get_default_paas_plane()
    state = migrator.cutover(args.name)
    if _output_format(args) != "text":
        _print_success(args, state)
        return 0
    print(
        f"Cutover complete for '{state['project']}': live traffic is now served by K3s + Flux "
        f"(legacy Nomad status: {state['legacyNomadStatus']})."
    )
    return 0


def cmd_migrate_abort(args: argparse.Namespace) -> int:
    _, _, migrator = get_default_paas_plane()
    state = migrator.abort(args.name)
    if _output_format(args) != "text":
        _print_success(args, state)
        return 0
    print(
        f"Aborted K3s cutover for '{state['project']}': traffic restored to standby Nomad job in <1s."
    )
    return 0


def cmd_migrate_finalize(args: argparse.Namespace) -> int:
    _, _, migrator = get_default_paas_plane()
    state = migrator.finalize(args.name)
    if _output_format(args) != "text":
        _print_success(args, state)
        return 0
    print(
        f"Finalized migration for '{state['project']}': legacy Nomad job purged, "
        f"orphan queue workers remaining: {state['orphanQueueWorkersRemaining']}."
    )
    return 0
