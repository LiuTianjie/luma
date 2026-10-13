"""Freeze source once and produce a platform-specific, immutable build plan."""
from __future__ import annotations

import hashlib
import subprocess
import tarfile
import tempfile
from pathlib import Path

import yaml

from .. import gitops
from ..agent import (
    _compose_build_args, _compose_build_spec,
    _find_luma_deployment_manifest, _safe_repo_path_from, _safe_repo_subpath,
    _select_luma_compose_manifest,
)
from ..errors import LumaError
from ..service import slugify
from .model import fingerprint
from .placement import resolve_platform


def plan_source(root: Path, body: dict, context: dict, registry_project: str) -> dict:
    selected = body.get("composeSidecar")
    choice = ("compose", _select_luma_compose_manifest(root, selected)) if selected else _find_luma_deployment_manifest(root)
    manifest_text = str(body.get("manifest") or (choice[1].read_text() if choice else ""))
    manifest = yaml.safe_load(manifest_text) or {}
    if not isinstance(manifest, dict) or not manifest.get("name"):
        raise LumaError("a named Luma deployment manifest is required")
    kind = choice[0] if choice else "service"
    project = str(context["projectKey"])
    namespace = registry_project + "/" + slugify(project) + "-" + fingerprint(project)[:8]
    targets, compose = [], {}
    if kind == "compose":
        compose_path = _safe_repo_path_from(choice[1].parent, root, str(manifest.get("compose") or "docker-compose.yml"))
        compose = yaml.safe_load(compose_path.read_text()) or {}
        services = compose.get("services")
        if not isinstance(services, dict) or not services:
            raise LumaError("compose services are required")
        for name, service in services.items():
            spec = _compose_build_spec(service)
            if spec is None:
                continue
            context_dir = _safe_repo_path_from(compose_path.parent, root, str(body.get("context") or spec.get("context") or "."))
            dockerfile = _safe_repo_path_from(context_dir, root, str(body.get("dockerfile") or spec.get("dockerfile") or "Dockerfile"))
            sidecar = (manifest.get("services") or {}).get(name) or {}
            platform = resolve_platform(context, node=sidecar.get("node") or manifest.get("node"), region=body.get("region") or sidecar.get("region") or manifest.get("region"), requested=body.get("platform") or spec.get("platform") or service.get("platform"))
            targets.append({"key": name, "context": str(context_dir.relative_to(root)), "dockerfile": str(dockerfile.relative_to(root)),
                            "platforms": platform.split(","), "args": _compose_build_args(spec), "target": str(spec.get("target") or ""),
                            "repository": namespace + "/" + slugify(name) + "-" + fingerprint(name)[:8], "sourceImage": service.get("image") or ""})
    else:
        spec = manifest.get("build") or {}
        context_dir = _safe_repo_subpath(root, str(body.get("context") or spec.get("context") or "."))
        dockerfile = _safe_repo_subpath(root, str(body.get("dockerfile") or spec.get("dockerfile") or "Dockerfile"))
        platform = resolve_platform(context, node=manifest.get("node"), region=body.get("region") or manifest.get("region"), requested=body.get("platform") or spec.get("platform"))
        targets.append({"key": slugify(manifest["name"]), "context": str(context_dir.relative_to(root)), "dockerfile": str(dockerfile.relative_to(root)),
                        "platforms": platform.split(","), "args": _compose_build_args(spec), "target": str(spec.get("target") or ""), "repository": namespace})
    for target in targets:
        file = root / target["dockerfile"]
        if not file.is_file():
            raise LumaError("Dockerfile not found: " + target["dockerfile"])
        target["dockerfileHash"] = hashlib.sha256(file.read_bytes()).hexdigest()
        if any(platform not in {"linux/amd64", "linux/arm64"} for platform in target["platforms"]):
            raise LumaError("the build pool supports native linux/amd64 and linux/arm64 targets")
    platforms = sorted({platform for target in targets for platform in target["platforms"]})
    if body.get("buildNode") not in {None, "", "auto"} and len(platforms) > 1:
        raise LumaError("a single build node cannot execute multiple native architectures; use auto placement")
    return {"kind": kind, "manifest": manifest_text, "composeContent": yaml.safe_dump(compose, sort_keys=False) if compose else "",
            "composeSidecar": selected or "", "targets": targets, "platforms": platforms,
            "targetKey": slugify(manifest["name"]), "recipeHash": fingerprint({"project": project, "targets": targets}),
            "proxy": str(context.get("proxy") or "")}


def prepare(run: dict, context: dict, blobs, settings, store) -> dict:
    body = run["body"]
    with tempfile.TemporaryDirectory(prefix="luma-source-") as work:
        root = (Path(work) / "src").resolve()
        gitops.clone(context["repoUrl"], root, ref=str(run["prepared"].get("sha") or body.get("ref") or "") or None,
                     proxy=context.get("proxy") or None, token=context.get("gitToken") or None, username=context.get("gitUsername") or None)
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True, check=True, timeout=30).stdout.strip()
        # Freeze the resolved revision before archiving. Retries clone this SHA,
        # even if the original branch has moved in the meantime.
        frozen = store.freeze_revision(run["id"], sha)
        if frozen.get("sourceObject"):
            return frozen
        if frozen["sha"] != sha:
            return prepare({**run, "prepared": frozen}, context, blobs, settings, store)
        plan = plan_source(root, body, context, settings.registry_project)
        if not plan["targets"]:
            raise LumaError("repository contains no build targets; use compose deploy for prebuilt images")
        if body.get("autoDeploy") is not False:
            store.reserve_target(run["id"], plan["targetKey"])
        archive = Path(work) / "source.tar.gz"
        # Preserve the checkout (including Git metadata) without adding files to
        # the user's build context. Reject links escaping the source snapshot.
        for path in root.rglob("*"):
            if path.is_symlink():
                try:
                    path.resolve().relative_to(root.resolve())
                except ValueError as exc:
                    raise LumaError("source symlink escapes repository: " + str(path.relative_to(root))) from exc
        with tarfile.open(archive, "w:gz") as out:
            out.add(root, arcname=".")
        checksum = _file_digest(archive)
        key = f"sources/{run['id']}/{sha}/{checksum}.tar.gz"
        blobs.put_file(key, archive)
        return store.save_prepared(run["id"], {**plan, "sha": sha, "sourceObject": key,
                "sourceBytes": archive.stat().st_size, "sourceChecksum": checksum})


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def deployment_result(plan: dict, images: dict[str, str]) -> dict:
    manifest = yaml.safe_load(plan["manifest"])
    result = {"kind": plan["kind"], "sha": plan["sha"], "images": images, "image": next(iter(images.values()), ""), "manifest": plan["manifest"]}
    if plan["kind"] == "compose":
        compose = yaml.safe_load(plan["composeContent"])
        aliases = {}
        for target in plan["targets"]:
            if target.get("sourceImage"):
                aliases.setdefault(target["sourceImage"], set()).add(images[target["key"]])
        for name, service in compose["services"].items():
            if name in images:
                service["image"] = images[name]
                service.pop("build", None)
            elif service.get("image") in aliases and len(aliases[service["image"]]) == 1:
                service["image"] = next(iter(aliases[service["image"]]))
        result.update(composeContent=yaml.safe_dump(compose, sort_keys=False), composeSidecar=plan.get("composeSidecar") or "")
    else:
        manifest.pop("build", None)
        manifest["image"] = result["image"]
        result["manifest"] = yaml.safe_dump(manifest, sort_keys=False)
    return result
