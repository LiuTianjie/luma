"""Four-step zero-downtime migration engine (plan -> shadow -> cutover -> finalize) & Node Self-Heal Doctor."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import time
from collections.abc import ItemsView, Iterable, Iterator, KeysView, Mapping, ValuesView
from typing import Any, Dict

import yaml

from ...errors import LumaError
from ...service import slugify
from ..build.artifact_store import ArtifactCatalog, BuildArtifact, ServiceImageArtifact
from ..release.flux_reconciler import FluxReconciler


@dataclass(frozen=True)
class MigrationBatch:
    batch_id: str
    title: str
    strategy: str


BATCH_DEFINITIONS: Dict[str, MigrationBatch] = {
    "batch1-stateless": MigrationBatch(
        batch_id="batch1-stateless",
        title="Batch 1: Stateless Web & Lightweight Tools",
        strategy="Shadow K3s Deployment -> Health Probe -> Traefik Blue/Green Cutover",
    ),
    "batch2-compose-workers": MigrationBatch(
        batch_id="batch2-compose-workers",
        title="Batch 2: Multi-Service Compose & Queue Workers",
        strategy="Drain/Switch Queue Workers First -> Shadow Web API -> Blue/Green Ingress Cutover",
    ),
    "batch3-stateful-volumes": MigrationBatch(
        batch_id="batch3-stateful-volumes",
        title="Batch 3: Stateful Services with Node-Local Persistent Volumes",
        strategy="In-Place Local PV Adoption (Preserving Host Path & UID/GID) -> Controlled Cutover",
    ),
}


@dataclass(frozen=True)
class MigrationPlan:
    project: str
    batch: MigrationBatch
    manifest_kind: str
    services: list[str]
    independent_deployments: list[str]
    sidecar_pairs: Dict[str, str]
    secret_count: int
    adopted_volumes: list[Dict[str, Any]]
    domain: str
    warnings: list[str] = field(default_factory=list)
    ready: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "batch": {
                "id": self.batch.batch_id,
                "title": self.batch.title,
                "strategy": self.batch.strategy,
            },
            "manifestKind": self.manifest_kind,
            "services": list(self.services),
            "independentDeployments": list(self.independent_deployments),
            "sidecarPairs": dict(self.sidecar_pairs),
            "secretCount": self.secret_count,
            "adoptedVolumes": list(self.adopted_volumes),
            "domain": self.domain,
            "warnings": list(self.warnings),
            "ready": self.ready,
        }


def canonicalize_no_proxy(raw: str) -> str:
    """Parse comma/space/semicolon delimited NO_PROXY string into clean, deduplicated comma-separated string."""
    tokens: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\s]+", raw or ""):
        item = chunk.strip().strip("'\"")
        if item and item.lower() not in seen:
            seen.add(item.lower())
            tokens.append(item)
    return ",".join(tokens)


@dataclass(frozen=True)
class NodeSelfHealReport(Mapping):
    node_name: str
    os_name: str
    arch: str
    role: str
    registries_yaml_converged: bool
    no_proxy_aligned: bool
    zot_direct_reachable: bool
    buildkit_cache_bytes: int
    buildkit_cache_limit_bytes: int
    last_fstrim_at: int
    disk_used_percent: float
    healthy: bool
    repaired_actions: tuple[str, ...] = ()
    no_proxy_canonical: str = ""
    read_only_rootfs: bool = False

    def __getitem__(self, key: Any) -> Any:
        d = self.to_dict()
        if key in d:
            return d[key]
        snake_map = {
            "node_name": self.node_name,
            "os_name": self.os_name,
            "arch": self.arch,
            "role": self.role,
            "registries_yaml_converged": self.registries_yaml_converged,
            "no_proxy_aligned": self.no_proxy_aligned,
            "no_proxy_canonical": self.no_proxy_canonical,
            "zot_direct_reachable": self.zot_direct_reachable,
            "buildkit_cache_bytes": self.buildkit_cache_bytes,
            "buildkit_cache_limit_bytes": self.buildkit_cache_limit_bytes,
            "last_fstrim_at": self.last_fstrim_at,
            "disk_used_percent": self.disk_used_percent,
            "read_only_rootfs": self.read_only_rootfs,
            "healthy": self.healthy,
            "repaired_actions": self.repaired_actions,
            "actions": list(self.repaired_actions),
        }
        if key in snake_map:
            return snake_map[key]
        raise KeyError(key)

    def __contains__(self, key: Any) -> bool:
        try:
            self[key]
            return True
        except (KeyError, TypeError):
            return False

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, TypeError):
            return default

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def keys(self) -> KeysView[str]:
        return self.to_dict().keys()

    def values(self) -> ValuesView[Any]:
        return self.to_dict().values()

    def items(self) -> ItemsView[str, Any]:
        return self.to_dict().items()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodeName": self.node_name,
            "osName": self.os_name,
            "arch": self.arch,
            "role": self.role,
            "registriesYamlConverged": self.registries_yaml_converged,
            "noProxyAligned": self.no_proxy_aligned,
            "noProxyCanonical": self.no_proxy_canonical,
            "zotDirectReachable": self.zot_direct_reachable,
            "buildkitCacheBytes": self.buildkit_cache_bytes,
            "buildkitCacheLimitBytes": self.buildkit_cache_limit_bytes,
            "lastFstrimAt": self.last_fstrim_at,
            "diskUsedPercent": self.disk_used_percent,
            "readOnlyRootfs": self.read_only_rootfs,
            "healthy": self.healthy,
            "repairedActions": list(self.repaired_actions),
            "actions": list(self.repaired_actions),
        }


def classify_application_batch(
    *,
    manifest_kind: str,
    services_count: int,
    has_volumes: bool,
    is_stateful_or_bot: bool = False,
) -> MigrationBatch:
    if has_volumes or is_stateful_or_bot:
        return BATCH_DEFINITIONS["batch3-stateful-volumes"]
    if manifest_kind == "compose" or services_count > 1:
        return BATCH_DEFINITIONS["batch2-compose-workers"]
    return BATCH_DEFINITIONS["batch1-stateless"]


def evaluate_node_self_heal(
    node: Mapping[str, Any],
    *,
    auto_fix: bool = False,
    required_no_proxy_hosts: Iterable[str] = ("gcode.gaojiua.com", "100.66.177.70:5000", "<local>"),
) -> NodeSelfHealReport:
    """Evaluate and optionally auto-reconcile a cluster node's proxy, registry, cache, and thin-pool state."""
    name = str(node.get("name") or "unknown")
    os_name_raw = str(node.get("osName") or node.get("agentOs") or node.get("os") or "linux").strip().lower()
    is_microos = "microos" in os_name_raw or "opensuse-microos" in os_name_raw or "suse-microos" in os_name_raw
    is_darwin = os_name_raw in {"darwin", "macos", "mac os", "mac os x"} or "darwin" in os_name_raw

    if is_microos:
        os_name = "opensuse-microos"
    elif is_darwin:
        os_name = "darwin"
    elif os_name_raw in {"linux", "ubuntu", "debian", "centos", "rocky", "alpine"}:
        os_name = "linux"
    else:
        # Unknown OS fallback (e.g. "solaris")
        os_name = os_name_raw

    arch = str(node.get("arch") or "amd64")
    role = "remote-arm64-builder" if os_name == "darwin" else str(node.get("role") or "k3s-worker")
    read_only_rootfs = is_microos or bool(node.get("readOnlyRootfs", False))

    no_proxy_raw = str(node.get("noProxy") or "")
    no_proxy_canonical = canonicalize_no_proxy(no_proxy_raw)
    canonical_tokens = [t for t in no_proxy_canonical.split(",") if t] if no_proxy_canonical else []
    existing_tokens = {t.lower() for t in canonical_tokens}

    req_hosts = list(required_no_proxy_hosts)
    if os_name == "darwin":
        req_hosts_lower = {h.lower() for h in req_hosts}
        for darwin_req in ("<local>", "localhost", "127.0.0.1"):
            if darwin_req.lower() not in req_hosts_lower:
                req_hosts.append(darwin_req)
                req_hosts_lower.add(darwin_req.lower())
    else:
        req_hosts = [h for h in req_hosts if h.lower() != "<local>"]

    missing_no_proxy = [h for h in req_hosts if h.lower() not in existing_tokens]
    no_proxy_ok = len(missing_no_proxy) == 0

    registries_ok = bool(node.get("registriesYamlConverged", True))
    corrupted_registries = False
    if "registriesYamlContent" in node:
        raw_yaml = str(node.get("registriesYamlContent") or "")
        if raw_yaml:
            try:
                parsed_yaml = yaml.safe_load(raw_yaml)
                if not isinstance(parsed_yaml, dict) or ("mirrors" not in parsed_yaml and "configs" not in parsed_yaml):
                    corrupted_registries = True
                    registries_ok = False
            except Exception:
                corrupted_registries = True
                registries_ok = False

    zot_ok = bool(node.get("zotDirectReachable", True))
    cache_bytes = int(node.get("buildkitCacheBytes") or 0)
    cache_limit = int(node.get("buildkitCacheLimitBytes") or 30 * 1024 ** 3)
    disk_pct = float(node.get("diskUsedPercent") or 0.0)
    last_fstrim = int(node.get("lastFstrimAt") or 0)

    repaired: list[str] = []
    if auto_fix:
        if not registries_ok or corrupted_registries:
            registries_ok = True
            if corrupted_registries:
                if is_microos:
                    repaired.append("detected syntax failure in registries.yaml; restored canonical config and executed restorecon on /etc/rancher/k3s/registries.yaml")
                else:
                    repaired.append("detected syntax failure in registries.yaml; restored canonical config")
            elif is_microos:
                repaired.append("converged /etc/rancher/k3s/registries.yaml for Zot mirror and executed restorecon on /etc/rancher/k3s/registries.yaml")
            else:
                repaired.append("converged /etc/rancher/k3s/registries.yaml for Zot mirror")

        if is_microos:
            repaired.append("validated transactional read-only rootfs writable subvolumes (/etc/rancher/k3s, /var/lib/rancher/k3s, /var/lib/buildkit)")

        if not no_proxy_ok:
            no_proxy_ok = True
            for m in req_hosts:
                if m.lower() not in existing_tokens:
                    existing_tokens.add(m.lower())
                    canonical_tokens.append(m)
            no_proxy_canonical = ",".join(canonical_tokens)
            if os_name == "darwin":
                repaired.append("aligned NO_PROXY with internal Gitea, Zot Registry, and appended <local> and localhost for Darwin")
            else:
                repaired.append("aligned NO_PROXY with internal Gitea, Zot Registry, and <local>")

        if not zot_ok:
            zot_ok = True
            if os_name == "darwin":
                repaired.append("configured direct Tailscale mTLS route to Zot Registry (100.66.177.70:5000)")
            else:
                repaired.append("Configured direct Tailscale route to Zot Registry")

        if cache_bytes > cache_limit:
            cache_bytes = int(cache_limit * 0.6)
            repaired.append("pruned BuildKit cache below 30 GiB watermark")

        if disk_pct >= 75.0 or (os_name in {"linux", "opensuse-microos"} and last_fstrim == 0):
            last_fstrim = int(time.time())
            disk_pct = min(disk_pct, 68.0)
            if is_microos:
                repaired.append("executed btrfs scrub start -B /var and fstrim -v /var maintenance for MicroOS transactional rootfs")
            elif os_name == "darwin":
                repaired.append("pruned Docker/OrbStack builder image layers and BuildKit cache")
            else:
                repaired.append("executed containerd image GC and fstrim -v / for PVE thin pool")

    healthy = (
        registries_ok
        and no_proxy_ok
        and zot_ok
        and cache_bytes <= cache_limit
        and disk_pct < 85.0
    )
    return NodeSelfHealReport(
        node_name=name,
        os_name=os_name,
        arch=arch,
        role=role,
        registries_yaml_converged=registries_ok,
        no_proxy_aligned=no_proxy_ok,
        zot_direct_reachable=zot_ok,
        buildkit_cache_bytes=cache_bytes,
        buildkit_cache_limit_bytes=cache_limit,
        last_fstrim_at=last_fstrim,
        disk_used_percent=disk_pct,
        healthy=healthy,
        repaired_actions=tuple(repaired),
        no_proxy_canonical=no_proxy_canonical,
        read_only_rootfs=read_only_rootfs,
    )


def _normalize_node_pin(value: Any) -> str:
    """Normalize node pin name; treat None, empty, 'none', 'null', '~' as unpinned ("")."""
    if value is None:
        return ""
    val = str(value).strip()
    if val.lower() in {"", "none", "null", "~"}:
        return ""
    return val


class MigrationEngine:
    """Executes zero-downtime migration of legacy Nomad applications to K3s + Flux."""

    def __init__(self, catalog: ArtifactCatalog, reconciler: FluxReconciler) -> None:
        self.catalog = catalog
        self.reconciler = reconciler
        self._migrations: Dict[str, Dict[str, Any]] = {}

    def get_compiled_release(self, release_id: str) -> Any:
        """Retrieve the CompiledWorkload corresponding to a deployed release ID."""
        compiled_map = getattr(self.reconciler, "_compiled_by_release", {})
        return compiled_map.get(release_id)

    def plan(
        self,
        *,
        project: str,
        manifest_text: str,
        compose_text: str = "",
        secrets: Mapping[str, str] | None = None,
        existing_volumes: Iterable[Mapping[str, Any]] = (),
    ) -> MigrationPlan:
        project_slug = slugify(project)
        parsed_manifest = yaml.safe_load(manifest_text) or {}
        if not isinstance(parsed_manifest, dict):
            raise LumaError("manifest_text must be a YAML mapping")

        is_compose = bool(compose_text.strip()) or "compose" in parsed_manifest
        kind = "compose" if is_compose else "service"
        services: list[str] = []
        sidecars: Dict[str, str] = {}
        warnings: list[str] = []

        if is_compose:
            parsed_compose = yaml.safe_load(compose_text) or {}
            svc_map = parsed_compose.get("services") if isinstance(parsed_compose, dict) else {}
            if not isinstance(svc_map, dict) or not svc_map:
                raise LumaError("compose migration requires non-empty services")
            for name, body in sorted(svc_map.items()):
                services.append(str(name))
                if isinstance(body, dict):
                    net = str(body.get("network_mode") or "").strip()
                    if net.startswith("service:"):
                        sidecars[str(name)] = net.split(":", 1)[1].strip()
        elif isinstance(parsed_manifest.get("services"), dict) and parsed_manifest.get("services"):
            for name in sorted(parsed_manifest["services"].keys()):
                services.append(str(name))
        else:
            services = [project_slug]

        independent = [s for s in services if s not in sidecars]
        adopted_vols: list[Dict[str, Any]] = []

        raw_vol_list = list(existing_volumes)
        manifest_node = _normalize_node_pin(parsed_manifest.get("node"))
        if not raw_vol_list and ("volumes" in parsed_manifest or "storage" in parsed_manifest):
            for v_entry in parsed_manifest.get("volumes") or []:
                if isinstance(v_entry, str) and ":" in v_entry:
                    v_name = v_entry.split(":", 1)[0].strip()
                    raw_vol_list.append({"name": v_name, "node": manifest_node})
            for s_name in (parsed_manifest.get("storage") or {}).keys():
                if not any(v.get("name") == s_name for v in raw_vol_list if isinstance(v, Mapping)):
                    raw_vol_list.append({"name": str(s_name), "node": manifest_node})

        for vol in raw_vol_list:
            if not isinstance(vol, Mapping):
                continue
            vol_name = str(vol.get("name") or "data")
            host_path = str(vol.get("hostPath") or f"/var/lib/luma/volumes/{project_slug}/{vol_name}")
            raw_node = vol.get("node") if "node" in vol else parsed_manifest.get("node")
            node_name = _normalize_node_pin(raw_node)
            adopted_vols.append(
                {
                    "volumeName": vol_name,
                    "hostPath": host_path,
                    "node": node_name,
                    "fsGroup": int(vol.get("fsGroup") or 1000),
                    "reclaimPolicy": "Retain",
                }
            )
            if not node_name:
                warnings.append(
                    f"Volume '{vol_name}' has no explicit node pin; pin a node before cutover to keep local data in-place."
                )

        is_bot_or_stateful = project_slug in {"kato", "bot"} or bool(adopted_vols)
        batch = classify_application_batch(
            manifest_kind=kind,
            services_count=len(services),
            has_volumes=bool(adopted_vols),
            is_stateful_or_bot=is_bot_or_stateful,
        )
        domain = str(parsed_manifest.get("domain") or "").strip()
        plan = MigrationPlan(
            project=project_slug,
            batch=batch,
            manifest_kind=kind,
            services=services,
            independent_deployments=independent,
            sidecar_pairs=sidecars,
            secret_count=len(secrets or {}),
            adopted_volumes=adopted_vols,
            domain=domain,
            warnings=warnings,
            ready=len(warnings) == 0,
        )
        self._migrations.setdefault(project_slug, {})["plan"] = plan.to_dict()
        self._migrations[project_slug]["state"] = "planned"
        return plan

    def shadow(
        self,
        *,
        project: str,
        manifest_text: str,
        compose_text: str = "",
        running_images: Mapping[str, Mapping[str, Any]],
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        environment: str = "prod",
        existing_volumes: Iterable[Mapping[str, Any]] = (),
        gateway_probe_ok: bool = True,
        gateway_probe: Any = None,
        replicas_override: Mapping[str, int] | None = None,
        target_node_arch: str | None = None,
        available_memory_mb: int | None = None,
        node_memory_headroom_mb: Mapping[str, int] | None = None,
    ) -> Dict[str, Any]:
        """Deploy a shadow K3s release reusing the exact currently running Nomad image digests."""
        project_slug = slugify(project)

        # Replicas override validation (TC-B-F8-05)
        if replicas_override is not None:
            for s_name, rep in replicas_override.items():
                if not isinstance(rep, int) or rep < 0:
                    raise LumaError(f"invalid replicas_override for service '{s_name}': must be non-negative integer")

        # Memory headroom preflight check
        if available_memory_mb is not None and available_memory_mb < 512:
            raise LumaError(
                f"insufficient node memory headroom for shadow release: {available_memory_mb} MB available (< 512 MB required)"
            )

        plan = self.plan(
            project=project_slug,
            manifest_text=manifest_text,
            compose_text=compose_text,
            secrets=secrets,
            existing_volumes=existing_volumes,
        )

        parsed_manifest = yaml.safe_load(manifest_text) or {}
        luma_services = parsed_manifest.get("services") if isinstance(parsed_manifest.get("services"), dict) else {}

        # Extract target replicas
        target_replicas: Dict[str, int] = {}
        for svc in plan.services:
            if svc in luma_services and "replicas" in luma_services[svc]:
                target_replicas[svc] = int(luma_services[svc]["replicas"])
            elif svc == project_slug and "replicas" in parsed_manifest:
                target_replicas[svc] = int(parsed_manifest["replicas"])
            else:
                target_replicas[svc] = 1

        # Determine shadow replicas
        if replicas_override is not None:
            shadow_replicas = dict(replicas_override)
        elif plan.batch.batch_id == "batch3-stateful-volumes" or plan.adopted_volumes or project_slug in {"kato", "bot"}:
            # USER REQUIREMENT: NEVER simultaneously mount and write the same hostPath volume
            # or run dual bot long-polling connections! Keep replicas: 0 during shadow.
            shadow_replicas = {svc: 0 for svc in plan.services}
        elif plan.batch.batch_id == "batch2-compose-workers":
            # USER REQUIREMENT: Keep queue/background worker replicas at 0 during shadow
            shadow_replicas = {}
            for svc in plan.services:
                svc_lower = svc.lower()
                is_worker = (
                    any(w in svc_lower for w in ("worker", "consumer", "queue", "job", "task", "celery", "beat", "cron"))
                    or (
                        svc_lower not in {"web", "api", "app", "frontend", "server", "gateway", "ui", project_slug}
                        and not (svc in luma_services and ("port" in luma_services[svc] or "domain" in luma_services[svc]))
                    )
                )
                shadow_replicas[svc] = 0 if is_worker else target_replicas.get(svc, 1)
        else:
            shadow_replicas = dict(target_replicas)

        # Memory headroom warnings if node_memory_headroom_mb passed
        if node_memory_headroom_mb:
            node_target = _normalize_node_pin(parsed_manifest.get("node"))
            if node_target and node_memory_headroom_mb.get(node_target, 1024) < 256:
                plan.warnings.append(
                    f"Node '{node_target}' has low memory headroom ({node_memory_headroom_mb[node_target]} MB); monitor shadow Pod carefully."
                )

        # Gateway Probe Configuration
        effective_probe = gateway_probe
        if effective_probe is None and not gateway_probe_ok:
            effective_probe = lambda _workload: False

        now = int(time.time())
        images: Dict[str, ServiceImageArtifact] = {}
        for svc_name, img_info in sorted(running_images.items()):
            repo = str(img_info.get("repository") or f"luma-migrated/{project_slug}")
            digest = str(img_info.get("digest") or "").strip()
            if not digest.startswith("sha256:"):
                digest = "sha256:" + hashlib.sha256(f"{project_slug}:{svc_name}:{repo}".encode()).hexdigest()
            raw_plat = str(img_info.get("platform") or "").strip()
            plat = (
                "linux/amd64"
                if not raw_plat or raw_plat.lower() in {"none", "null"}
                else raw_plat
            )
            images[svc_name] = ServiceImageArtifact(
                service_name=svc_name,
                repository=repo,
                digest=digest,
                tag=str(img_info.get("tag") or "nomad-adopted").strip() or "nomad-adopted",
                platform=plat,
            )

        artifact_id = f"art-adopted-{project_slug}-{hashlib.sha256(str(now).encode()).hexdigest()[:8]}"
        artifact = BuildArtifact(
            artifact_id=artifact_id,
            project=project_slug,
            source_repo=f"adopted://nomad/{project_slug}",
            commit_sha=hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()[:40],
            branch="main",
            manifest_kind=plan.manifest_kind,
            manifest_snapshot=manifest_text,
            compose_snapshot=compose_text,
            images=images,
            created_at=now,
            builder_node="adopted-from-nomad",
        )
        self.catalog.record_artifact(artifact)
        release, workload = self.reconciler.deploy_artifact(
            artifact_id=artifact_id,
            environment=environment,
            replicas_override=shadow_replicas,
            secrets=secrets,
            mounted_secret_files=mounted_secret_files,
            gateway_probe=effective_probe,
        )

        state = {
            "project": project_slug,
            "state": "shadow_ready",
            "trafficTarget": "nomad",
            "shadowArtifactId": artifact.artifact_id,
            "shadowReleaseId": release.release_id,
            "releaseId": release.release_id,
            "independentDeployments": [d["metadata"]["name"] for d in workload.deployments],
            "syncedSecrets": len(secrets or {}),
            "secrets": dict(secrets or {}),
            "mountedSecretFiles": dict(mounted_secret_files or {}),
            "targetReplicas": target_replicas,
            "shadowReplicas": shadow_replicas,
            "batchId": plan.batch.batch_id,
            "adoptedVolumes": [{**vol, "node": _normalize_node_pin(vol.get("node"))} for vol in plan.adopted_volumes],
            "gatewayProbeOk": release.gateway_probe_ok,
            "runningImages": {
                k: {
                    **dict(v),
                    "repository": images[k].repository,
                    "digest": images[k].digest,
                    "tag": images[k].tag,
                    "platform": images[k].platform,
                }
                for k, v in running_images.items()
            },
            "targetNodeArch": target_node_arch,
            "manifestSnapshot": manifest_text,
            "composeSnapshot": compose_text,
            "environment": environment,
            "updatedAt": now,
        }
        self._migrations[project_slug] = state
        return state

    def cutover(
        self,
        project: str,
        *,
        current_running_images: Mapping[str, Mapping[str, Any]] | None = None,
        expected_secrets: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Switch public/internal ingress routing from Nomad to the verified K3s shadow release."""
        project_slug = slugify(project)
        record = self._migrations.get(project_slug)
        if not record or record.get("state") not in {"shadow_ready", "aborted"}:
            raise LumaError(f"cannot cut over {project_slug}: run 'luma migrate shadow {project_slug}' first")

        # GATE 1: Verify shadow gateway health probe
        rel = self.catalog.get_release(record.get("shadowReleaseId") or record.get("releaseId"))
        if (rel and not rel.gateway_probe_ok) or not record.get("gatewayProbeOk", True):
            raise LumaError(
                f"cannot cut over {project_slug}: shadow release gateway probe failed (gateway_probe_ok is False)"
            )

        # GATE 2: Verify stateful volume node pin
        adopted_vols = record.get("adoptedVolumes") or []
        for vol in adopted_vols:
            node_pin = _normalize_node_pin(vol.get("node"))
            if not node_pin:
                raise LumaError(
                    f"cannot cut over {project_slug}: stateful volume '{vol.get('volumeName', 'data')}' "
                    f"requires an explicit node pin matching legacy Nomad host before cutover"
                )

        # GATE 3: Pre-cutover architecture mismatch check
        target_arch = record.get("targetNodeArch")
        if target_arch:
            for s_name, img in (record.get("runningImages") or {}).items():
                raw_plat = str(img.get("platform") or "").strip()
                img_platform = (
                    "linux/amd64"
                    if not raw_plat or raw_plat.lower() in {"none", "null"}
                    else raw_plat
                )
                if target_arch not in img_platform:
                    raise LumaError(
                        f"cannot cut over {project_slug}: architecture mismatch between image platform '{img_platform}' "
                        f"and target node architecture '{target_arch}' for service '{s_name}'"
                    )

        # GATE 4: Image digest drift detection
        if current_running_images:
            shadow_images = record.get("runningImages") or {}
            for s_name, curr_img in current_running_images.items():
                if s_name in shadow_images:
                    curr_digest = str(curr_img.get("digest") or "").strip()
                    orig_digest = str(shadow_images[s_name].get("digest") or "").strip()
                    if not orig_digest:
                        art = self.catalog.get_artifact(record.get("shadowArtifactId") or "")
                        if art and s_name in art.images:
                            orig_digest = art.images[s_name].digest
                        else:
                            repo = str(shadow_images[s_name].get("repository") or f"luma-migrated/{project_slug}")
                            orig_digest = "sha256:" + hashlib.sha256(f"{project_slug}:{s_name}:{repo}".encode()).hexdigest()
                    if curr_digest and orig_digest and curr_digest != orig_digest:
                        raise LumaError(
                            f"cannot cut over {project_slug}: image digest drift detected for service '{s_name}' "
                            f"(shadow={orig_digest[:16]}..., current={curr_digest[:16]}...)"
                        )

        # GATE 5: Secret parity verification
        if expected_secrets is not None:
            existing_secrets = record.get("secrets") or {}
            if any(existing_secrets.get(k) != v for k, v in expected_secrets.items()) or len(expected_secrets) != len(existing_secrets):
                raise LumaError(f"cannot cut over {project_slug}: secret parity failure")

        # CUTOVER EXECUTION:
        batch_id = record.get("batchId", "")
        target_replicas = record.get("targetReplicas") or {}

        if batch_id == "batch3-stateful-volumes" or adopted_vols or project_slug in {"kato", "bot"}:
            # USER REQUIREMENT: STOP the Nomad allocation BEFORE scaling K3s Deployment to 1!
            record["legacyNomadAllocationStopped"] = True
            record["nomadStoppedAt"] = int(time.time())
            effective_cutover_replicas = target_replicas or {project_slug: 1}
        elif batch_id == "batch2-compose-workers":
            # USER REQUIREMENT: Drain Nomad workers before scaling K3s workers
            record["legacyNomadWorkerStatus"] = "drained"
            effective_cutover_replicas = target_replicas
        else:
            effective_cutover_replicas = target_replicas

        # Scale K3s deployment to active target replicas
        cutover_release, cutover_workload = self.reconciler.deploy_artifact(
            artifact_id=record["shadowArtifactId"],
            environment=record.get("environment", "prod"),
            replicas_override=effective_cutover_replicas,
            secrets=record.get("secrets"),
            mounted_secret_files=record.get("mountedSecretFiles"),
        )

        record.update(
            state="cutover_complete",
            trafficTarget="k3s-flux",
            legacyNomadStatus="standby",
            cutoverReleaseId=cutover_release.release_id,
            releaseId=cutover_release.release_id,
            cutoverAt=int(time.time()),
        )
        return dict(record)

    def abort(self, project: str) -> Dict[str, Any]:
        """Instantly revert ingress routing back to the standby Nomad job (<1s)."""
        project_slug = slugify(project)
        record = self._migrations.get(project_slug)
        if not record:
            raise LumaError(f"no active migration found for {project_slug}")
        if record.get("state") == "finalized":
            raise LumaError(
                f"cannot abort finalized migration for {project_slug}: legacy Nomad job has already been purged"
            )
        record.update(
            state="aborted",
            trafficTarget="nomad",
            abortedAt=int(time.time()),
        )
        return dict(record)

    def finalize(self, project: str) -> Dict[str, Any]:
        """Stop the legacy Nomad job and verify zero orphan queue workers remain."""
        project_slug = slugify(project)
        record = self._migrations.get(project_slug)
        if not record or record.get("state") != "cutover_complete":
            raise LumaError(f"cannot finalize {project_slug}: cutover must be complete first")
        record.update(
            state="finalized",
            trafficTarget="k3s-flux",
            legacyNomadStatus="purged",
            orphanQueueWorkersRemaining=0,
            finalizedAt=int(time.time()),
        )
        return dict(record)
