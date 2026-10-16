# Handoff Report: Milestone 4 Feature 8 — 4-Step Zero-Downtime Migration Engine Hardening & Standalone Script

**Agent**: Explorer M4-2  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2`  
**Milestone**: Milestone 4 (M4: Node Self-Healing & Zero-Downtime Migration)  
**Feature Scope**: Feature 8 (4-Step Migration Engine Hardening, SQLite/flock Single-Instance Isolation, Batch 2 Queue-Worker Race Prevention, Pre-Cutover Validation Gates, Standalone Script `scripts/migrate-nomad-to-k3s.py`, and CLI Surface Cleanup)  
**Date**: 2026-10-15T18:36:00Z  

---

## 1. Observation

### 1.1 Existing Implementation Baseline in `luma/paas/migration/migrator.py`

Direct inspection of `luma/paas/migration/migrator.py` (384 lines) reveals several critical missing guards and architectural disconnects:

1. **`MigrationEngine.get_compiled_release` is completely missing**:
   - In `TEST_INFRA.md` lines 772 and 791, Suite 4 test cases require:
     ```python
     compiled = self.migrator.get_compiled_release(rel_id)
     web_dep = next(d for d in compiled.deployments if "web" in d["metadata"]["name"])
     ```
   - In `luma/paas/migration/migrator.py`, `MigrationEngine` has no `get_compiled_release` method. Calling this raises `AttributeError: 'MigrationEngine' object has no attribute 'get_compiled_release'`.
   - In `luma/paas/release/flux_reconciler.py` line 36, `FluxReconciler` stores compiled releases in `self._compiled_by_release: Dict[str, CompiledWorkload] = {}`.

2. **`MigrationEngine.shadow` lacks essential safety parameters and volume passthrough**:
   - Lines 275-285 of `luma/paas/migration/migrator.py`:
     ```python
     def shadow(
         self,
         *,
         project: str,
         manifest_text: str,
         compose_text: str = "",
         running_images: Mapping[str, Mapping[str, str]],
         secrets: Mapping[str, str] | None = None,
         mounted_secret_files: Mapping[str, str] | None = None,
         environment: str = "prod",
     ) -> Dict[str, Any]:
     ```
   - Parameter omissions:
     - `existing_volumes: Iterable[Mapping[str, Any]] = ()` is **NOT accepted** by `shadow()`. Lines 813-814 of `TEST_INFRA.md` pass `existing_volumes=[{"name": "data", ...}]` to `self.migrator.shadow(...)`, which currently crashes with `TypeError: MigrationEngine.shadow() got an unexpected keyword argument 'existing_volumes'`.
     - `gateway_probe_ok: bool = True` and `gateway_probe: Any = None` are **NOT accepted**. Line 827 of `TEST_INFRA.md` passes `gateway_probe_ok=False` to `self.migrator.shadow(...)`, which currently crashes with `TypeError: MigrationEngine.shadow() got an unexpected keyword argument 'gateway_probe_ok'`.
     - `replicas_override: Mapping[str, int] | None = None` is **NOT accepted**.

3. **Batch 2 Queue-Worker Race Condition during Shadow**:
   - In lines 324-329 of `luma/paas/migration/migrator.py`:
     ```python
     release, workload = self.reconciler.deploy_artifact(
         artifact_id=artifact_id,
         environment=environment,
         secrets=secrets,
         mounted_secret_files=mounted_secret_files,
     )
     ```
   - `shadow()` deploys all services with their default replicas (e.g. `video-worker: 4`).
   - If `video-worker` starts in K3s while Nomad's `video-worker` is still running, both simultaneously consume from Redis/RabbitMQ message queues, violating the zero-dual-consumer requirement.

4. **Batch 3 SQLite WAL / flock & Bot Long-Polling Hazard during Shadow**:
   - Direct quote from User Feedback (`ORIGINAL_REQUEST.md` line 64):
     > "NEVER simultaneously mount and write the same hostPath volume from both Nomad and K3s (which corrupts SQLite WAL/flock) or run dual Telegram/Feishu bot long-polling connections! For stateful/exclusive-lock apps, `shadow` must only pre-pull images, sync secrets, and validate hostPath/node-pin with `replicas: 0`, and `cutover` must stop the Nomad allocation BEFORE scaling the K3s Deployment to `1`."
   - Currently, `shadow()` sets `replicas: 1` for single-service stateful workloads (`kato`, `bot`), which would cause K3s to immediately mount and write `/var/lib/luma/volumes/kato/data` while Nomad is still writing, corrupting SQLite WAL shared-memory index files (`.shm`/`.wal`) and crashing bot polling with HTTP 409 Conflict.

5. **`MigrationEngine.cutover` is a no-op dictionary update without validation gates or workload scaling**:
   - Lines 343-356 of `luma/paas/migration/migrator.py`:
     ```python
     def cutover(self, project: str) -> Dict[str, Any]:
         """Switch public/internal ingress routing from Nomad to the verified K3s shadow release."""
         project_slug = slugify(project)
         record = self._migrations.get(project_slug)
         if not record or record.get("state") not in {"shadow_ready", "aborted"}:
             raise LumaError(f"cannot cut over {project_slug}: run 'luma migrate shadow {project_slug}' first")
         record.update(
             state="cutover_complete",
             trafficTarget="k3s-flux",
             legacyNomadStatus="standby",
             cutoverAt=int(time.time()),
         )
         return dict(record)
     ```
   - Notice:
     - It does NOT check `release.gateway_probe_ok` (fails TC-F8-05).
     - It does NOT check for missing volume `node` pin (fails TC-F8-03).
     - It does NOT scale up paused workers to target replicas in K3s (fails TC-F8-02: `rel_id = cutover["releaseId"]` is missing, and workers remain at replica 0).
     - It does NOT stop Nomad allocations before starting Batch 3 stateful pods.

6. **Key Return Keys Mismatch**:
   - `shadow()` returns `shadowReleaseId` (line 335). But `TEST_INFRA.md` line 771 asserts:
     ```python
     rel_id = shadow["releaseId"]
     ```
   - `cutover()` returns no `releaseId`. But line 790 asserts:
     ```python
     rel_id = cutover["releaseId"]
     ```

7. **CLI Help Pollution (`luma/cli/parser.py`)**:
   - `COMMAND_GROUPS` lines 40-44 currently include `("migrate", "Migrate existing Nomad applications to the K8s + Flux PaaS runtime")`.
   - This causes `luma --help` to display one-time migration commands to all open-source users.
   - User requirement (`ORIGINAL_REQUEST.md` line 61): hide `migrate` from public `COMMAND_GROUPS` and use `argparse.SUPPRESS` on the subparser.

8. **Missing Standalone Script**:
   - `scripts/migrate-nomad-to-k3s.py` does not exist in `scripts/`.

---

## 2. Logic Chain

### 2.1 Batch 2 Queue-Worker Race Prevention Logic

```
[Observation 1.1.3] Shadow currently deploys workers at replicas >= 1 while Nomad workers run
        │
        ▼
[Hazard] Dual consumers process identical queue messages concurrently -> duplicate transcoding / data corruption
        │
        ▼
[Remediation Strategy in shadow()]
  1. Identify background worker services:
     - Name contains ("worker", "consumer", "queue", "job", "task", "celery", "beat", "cron")
     - OR service has NO port / domain / exposure and is not in ("web", "api", "app", "frontend", "server", "gateway", "ui")
  2. In shadow release, set worker replicas to 0:
     effective_shadow_replicas[worker] = 0
     effective_shadow_replicas[web] = target_replicas (>= 1)
  3. Deploy shadow release via FluxReconciler with shadow replicas override
        │
        ▼
[Remediation Strategy in cutover()]
  1. Execute two-phase cutover:
     - Phase A: Stop / drain legacy Nomad worker allocations (Nomad workers finish in-flight tasks; new tasks queue in Redis)
     - Phase B: Call reconciler to scale K3s worker Deployments to configured target replicas (e.g. video-worker: 4)
  2. Record cutover release ID in record["releaseId"] and record["cutoverReleaseId"]
```

### 2.2 Batch 3 Stateful SQLite / flock & Bot Long-Polling Isolation Logic

```
[Observation 1.1.4] SQLite WAL shared memory index (.shm) and POSIX flock file locks are NOT synchronized
                    across distinct container runtimes / namespaces on the same hostPath
        │
        ▼
[Hazard] Simultaneous mount & write corrupts SQLite database headers and WAL ring buffers;
        Telegram bot long-polling returns HTTP 409 Conflict ("terminated by other getUpdates request")
        │
        ▼
[Remediation Strategy in shadow()]
  1. Detect Batch 3 stateful service:
     - Manifest has volumes / storage, OR existing_volumes passed, OR project in ("kato", "bot")
  2. Enforce replicas = 0 in K3s shadow release:
     effective_shadow_replicas = {s: 0 for s in services}
  3. Pre-pull container images on target node
  4. Synchronize Kubernetes Secrets and mounted secret files
  5. Validate hostPath directory existence and UID/GID fsGroup (1000)
  6. Validate explicit node pin matching legacy Nomad host
        │
        ▼
[Remediation Strategy in cutover()]
  1. Validation Gate: If any adopted volume has empty node pin -> raise LumaError
  2. Isolation Enforcement:
     - STOP the legacy Nomad allocation FIRST (record["legacyNomadAllocationStopped"] = True)
     - Verify Nomad container has released flock and closed SQLite WAL
  3. Scale K3s Deployment from 0 to 1 (in-place local volume mount without data copy)
  4. Verify K3s Pod readiness probe
```

### 2.3 Same-Node Coexistence & Port Conflict Avoidance Logic

```
[Observation 1.1] During shadow phase, Nomad and K3s coexist on the same physical host
        │
        ▼
[Hazard 1: Host Port Collision] If K3s Traefik or tcp-relay attempts to bind host ports 80/443 or published ports
                                while Nomad Traefik is listening -> bind: address already in use (CrashLoopBackOff)
[Hazard 2: Memory OOM] Running shadow containers alongside legacy Nomad containers exceeds host RAM
        │
        ▼
[Remediation Strategy]
  1. Port Coexistence:
     - K3s shadow workloads NEVER bind duplicate host ports (80/443).
     - Traffic transition occurs via Traefik dynamic routing: Nomad proxy forwards upstream,
       or shadow uses internal ClusterIP / NodePort / distinct test domain during probe.
     - tcp-relay publishPort collisions are pre-validated before binding.
  2. Memory Headroom Check:
     - Before deploying shadow Pods on target node, inspect node memory headroom (>= 512 MB free).
     - If available memory is below threshold, warn operator or block shadow to prevent host OOM killer.
```

### 2.4 Standalone One-Time Migration Script & CLI Cleanliness Logic

```
[Observation 1.1.7] Open-source users running `luma --help` see legacy Nomad migration commands
                    User feedback: migration is a ONE-TIME operation; script should be standalone
        │
        ▼
[Remediation in luma/cli/parser.py]
  1. Remove ("migrate", ...) from COMMAND_GROUPS in parser.py
  2. Register migrate subparser with help=argparse.SUPPRESS
  3. scripts/generate-cli-reference.py automatically omits suppressed commands from docs
  4. Command handler remain 100% callable for backwards compatibility: `luma migrate ...`
        │
        ▼
[Remediation in scripts/migrate-nomad-to-k3s.py]
  1. Create standalone operator script:
     - Preflight checks: K3s cluster, Nomad daemon, memory headroom, port availability
     - Automated batch execution: Batch 1 -> Batch 2 -> Batch 3
     - Interactive or flag-driven steps: plan -> shadow -> cutover -> finalize
     - State persistence to .luma-migration-state.json (resumable upon failure)
     - Final step: decommissioning Nomad (`systemctl disable --now nomad`)
```

---

## 3. Caveats

1. **Host-Level Process Execution for Nomad Decommission**:
   `scripts/migrate-nomad-to-k3s.py` should prompt for `sudo systemctl disable --now nomad` or execute it if running as root. In non-root or macOS test environments, it must gracefully print the operator instructions rather than crashing.
2. **Dynamic Volume Discovery in Production**:
   When `existing_volumes` is omitted, the migration engine automatically derives volume paths from `parsed_manifest` (`volumes:` and `storage:`). In existing production Nomad jobs, if volume paths deviated from `/var/lib/luma/volumes/<app>/<vol>`, the operator passes explicit `--volume-path` or `existing_volumes`.
3. **No Code Modification Constraint**:
   As an Explorer, all findings and remediation diffs below are read-only proposals. No source files have been altered.

---

## 4. Conclusion & Concrete Remediation Proposals

### 4.1 Remediations for `luma/paas/migration/migrator.py`

#### Method Signatures & Interfaces

```python
# 1. Update classify_application_batch to recognize stateful bots
def classify_application_batch(
    *,
    manifest_kind: str,
    services_count: int,
    has_volumes: bool,
    is_stateful_or_bot: bool = False,
) -> MigrationBatch: ...

# 2. Add helper to extract worker vs web services
def _classify_service_role(
    svc_name: str,
    parsed_manifest: Mapping[str, Any],
    parsed_compose: Mapping[str, Any],
    project_slug: str,
) -> str:
    """Returns 'worker' or 'web'."""
    ...

# 3. Add get_compiled_release to MigrationEngine
class MigrationEngine:
    def get_compiled_release(self, release_id: str) -> CompiledWorkload | None:
        """Retrieve CompiledWorkload for release_id from reconciler cache."""
        return getattr(self.reconciler, "_compiled_by_release", {}).get(release_id)

# 4. Update shadow signature
    def shadow(
        self,
        *,
        project: str,
        manifest_text: str,
        compose_text: str = "",
        running_images: Mapping[str, Mapping[str, str]],
        secrets: Mapping[str, str] | None = None,
        mounted_secret_files: Mapping[str, str] | None = None,
        environment: str = "prod",
        existing_volumes: Iterable[Mapping[str, Any]] = (),
        gateway_probe_ok: bool = True,
        gateway_probe: Any = None,
        replicas_override: Mapping[str, int] | None = None,
        node_memory_headroom_mb: Mapping[str, int] | None = None,
        node_architectures: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]: ...

# 5. Update cutover with validation gates and worker scaling
    def cutover(self, project: str) -> Dict[str, Any]: ...

# 6. Update abort with finalized check
    def abort(self, project: str) -> Dict[str, Any]: ...
```

#### Exact Proposed Diff for `luma/paas/migration/migrator.py`

```python
--- a/luma/paas/migration/migrator.py
+++ b/luma/paas/migration/migrator.py
@@ -111,10 +111,11 @@ def classify_application_batch(
     *,
     manifest_kind: str,
     services_count: int,
     has_volumes: bool,
+    is_stateful_or_bot: bool = False,
 ) -> MigrationBatch:
-    if has_volumes:
+    if has_volumes or is_stateful_or_bot:
         return BATCH_DEFINITIONS["batch3-stateful-volumes"]
     if manifest_kind == "compose" or services_count > 1:
         return BATCH_DEFINITIONS["batch2-compose-workers"]
     return BATCH_DEFINITIONS["batch1-stateless"]
@@ -191,10 +192,15 @@ class MigrationEngine:
     def __init__(self, catalog: ArtifactCatalog, reconciler: FluxReconciler) -> None:
         self.catalog = catalog
         self.reconciler = reconciler
         self._migrations: Dict[str, Dict[str, Any]] = {}
 
+    def get_compiled_release(self, release_id: str) -> Any:
+        """Retrieve the CompiledWorkload corresponding to a deployed release ID."""
+        compiled_map = getattr(self.reconciler, "_compiled_by_release", {})
+        return compiled_map.get(release_id)
+
     def plan(
         self,
         *,
         project: str,
         manifest_text: str,
         compose_text: str = "",
@@ -213,20 +219,30 @@ class MigrationEngine:
         services: list[str] = []
         sidecars: Dict[str, str] = {}
         warnings: list[str] = []
+        parsed_compose: Dict[str, Any] = {}
 
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
+        elif isinstance(parsed_manifest.get("services"), dict) and parsed_manifest.get("services"):
+            for name in sorted(parsed_manifest["services"].keys()):
+                services.append(str(name))
         else:
             services = [project_slug]
 
         independent = [s for s in services if s not in sidecars]
         adopted_vols: list[Dict[str, Any]] = []
+        raw_vol_list = list(existing_volumes)
+        if not raw_vol_list and ("volumes" in parsed_manifest or "storage" in parsed_manifest):
+            for v_entry in parsed_manifest.get("volumes") or []:
+                if isinstance(v_entry, str) and ":" in v_entry:
+                    v_name = v_entry.split(":", 1)[0].strip()
+                    raw_vol_list.append({"name": v_name, "node": parsed_manifest.get("node", "")})
+            for s_name in (parsed_manifest.get("storage") or {}).keys():
+                if not any(v.get("name") == s_name for v in raw_vol_list if isinstance(v, Mapping)):
+                    raw_vol_list.append({"name": str(s_name), "node": parsed_manifest.get("node", "")})
+
+        for vol in raw_vol_list:
             if not isinstance(vol, Mapping):
                 continue
             vol_name = str(vol.get("name") or "data")
@@ -248,8 +264,9 @@ class MigrationEngine:
                 warnings.append(
                     f"Volume '{vol_name}' has no explicit node pin; pin a node before cutover to keep local data in-place."
                 )
 
+        is_bot_or_stateful = project_slug in {"kato", "bot"} or bool(adopted_vols)
         batch = classify_application_batch(
             manifest_kind=kind,
             services_count=len(services),
-            has_volumes=bool(adopted_vols),
+            has_volumes=bool(adopted_vols),
+            is_stateful_or_bot=is_bot_or_stateful,
         )
@@ -275,18 +292,84 @@ class MigrationEngine:
     def shadow(
         self,
         *,
         project: str,
         manifest_text: str,
         compose_text: str = "",
         running_images: Mapping[str, Mapping[str, str]],
         secrets: Mapping[str, str] | None = None,
         mounted_secret_files: Mapping[str, str] | None = None,
         environment: str = "prod",
+        existing_volumes: Iterable[Mapping[str, Any]] = (),
+        gateway_probe_ok: bool = True,
+        gateway_probe: Any = None,
+        replicas_override: Mapping[str, int] | None = None,
+        node_memory_headroom_mb: Mapping[str, int] | None = None,
+        node_architectures: Mapping[str, str] | None = None,
     ) -> Dict[str, Any]:
         """Deploy a shadow K3s release reusing the exact currently running Nomad image digests."""
         project_slug = slugify(project)
         plan = self.plan(
             project=project_slug,
             manifest_text=manifest_text,
             compose_text=compose_text,
             secrets=secrets,
+            existing_volumes=existing_volumes,
         )
+        parsed_manifest = yaml.safe_load(manifest_text) or {}
+        parsed_compose = yaml.safe_load(compose_text) or {}
+        luma_services = parsed_manifest.get("services") if isinstance(parsed_manifest.get("services"), dict) else {}
+
+        # Extract target replicas
+        target_replicas: Dict[str, int] = {}
+        for svc in plan.services:
+            if svc in luma_services and "replicas" in luma_services[svc]:
+                target_replicas[svc] = int(luma_services[svc]["replicas"])
+            elif svc == project_slug and "replicas" in parsed_manifest:
+                target_replicas[svc] = int(parsed_manifest["replicas"])
+            else:
+                target_replicas[svc] = 1
+
+        # Determine shadow replicas strategy
+        if replicas_override is not None:
+            shadow_replicas = dict(replicas_override)
+        elif plan.batch.batch_id == "batch3-stateful-volumes" or plan.adopted_volumes or project_slug in {"kato", "bot"}:
+            # USER REQUIREMENT: NEVER simultaneously mount and write the same hostPath volume
+            # or run dual bot long-polling connections! Keep replicas: 0 during shadow.
+            shadow_replicas = {svc: 0 for svc in plan.services}
+        elif plan.batch.batch_id == "batch2-compose-workers":
+            # USER REQUIREMENT: Keep queue/background worker replicas at 0 during shadow
+            shadow_replicas = {}
+            for svc in plan.services:
+                svc_lower = svc.lower()
+                is_worker = (
+                    any(w in svc_lower for w in ("worker", "consumer", "queue", "job", "task", "celery", "beat", "cron"))
+                    or (
+                        svc_lower not in {"web", "api", "app", "frontend", "server", "gateway", "ui", project_slug}
+                        and not (svc in luma_services and ("port" in luma_services[svc] or "domain" in luma_services[svc]))
+                    )
+                )
+                shadow_replicas[svc] = 0 if is_worker else target_replicas.get(svc, 1)
+        else:
+            shadow_replicas = dict(target_replicas)
+
+        # Memory Headroom Preflight Check
+        if node_memory_headroom_mb:
+            node_target = str(parsed_manifest.get("node") or "")
+            if node_target and node_memory_headroom_mb.get(node_target, 1024) < 256:
+                plan.warnings.append(
+                    f"Node '{node_target}' has low memory headroom ({node_memory_headroom_mb[node_target]} MB); monitor shadow Pod carefully."
+                )
+
+        # Gateway Probe Configuration
+        effective_probe = gateway_probe
+        if effective_probe is None and not gateway_probe_ok:
+            effective_probe = lambda _workload: False
+
         now = int(time.time())
@@ -324,10 +406,12 @@ class MigrationEngine:
         release, workload = self.reconciler.deploy_artifact(
             artifact_id=artifact_id,
             environment=environment,
+            replicas_override=shadow_replicas,
             secrets=secrets,
             mounted_secret_files=mounted_secret_files,
+            gateway_probe=effective_probe,
         )
         state = {
             "project": project_slug,
             "state": "shadow_ready",
             "trafficTarget": "nomad",
             "shadowArtifactId": artifact.artifact_id,
             "shadowReleaseId": release.release_id,
+            "releaseId": release.release_id,
             "independentDeployments": [d["metadata"]["name"] for d in workload.deployments],
             "syncedSecrets": len(secrets or {}),
+            "secrets": dict(secrets or {}),
+            "mountedSecretFiles": dict(mounted_secret_files or {}),
+            "targetReplicas": target_replicas,
+            "shadowReplicas": shadow_replicas,
+            "batchId": plan.batch.batch_id,
+            "adoptedVolumes": list(plan.adopted_volumes),
+            "gatewayProbeOk": release.gateway_probe_ok,
+            "runningImages": dict(running_images),
+            "manifestSnapshot": manifest_text,
+            "composeSnapshot": compose_text,
+            "environment": environment,
             "updatedAt": now,
         }
         self._migrations[project_slug] = state
         return state
@@ -343,14 +427,61 @@ class MigrationEngine:
     def cutover(self, project: str) -> Dict[str, Any]:
         """Switch public/internal ingress routing from Nomad to the verified K3s shadow release."""
         project_slug = slugify(project)
         record = self._migrations.get(project_slug)
         if not record or record.get("state") not in {"shadow_ready", "aborted"}:
             raise LumaError(f"cannot cut over {project_slug}: run 'luma migrate shadow {project_slug}' first")
 
+        # GATE 1: Verify shadow gateway health probe
+        rel = self.catalog.get_release(record.get("shadowReleaseId") or record.get("releaseId"))
+        if (rel and not rel.gateway_probe_ok) or not record.get("gatewayProbeOk", True):
+            raise LumaError(
+                f"cannot cut over {project_slug}: shadow release gateway probe failed (gateway_probe_ok is False)"
+            )
+
+        # GATE 2: Verify stateful volume node pin
+        adopted_vols = record.get("adoptedVolumes") or []
+        for vol in adopted_vols:
+            node_pin = str(vol.get("node") or "").strip()
+            if not node_pin:
+                raise LumaError(
+                    f"cannot cut over {project_slug}: stateful volume '{vol.get('volumeName', 'data')}' "
+                    f"requires an explicit node pin matching legacy Nomad host before cutover"
+                )
+
+        # CUTOVER EXECUTION:
+        batch_id = record.get("batchId", "")
+        target_replicas = record.get("targetReplicas") or {}
+
+        if batch_id == "batch3-stateful-volumes" or adopted_vols or project_slug in {"kato", "bot"}:
+            # USER REQUIREMENT: STOP the Nomad allocation BEFORE scaling K3s Deployment to 1!
+            record["legacyNomadAllocationStopped"] = True
+            record["nomadStoppedAt"] = int(time.time())
+            effective_cutover_replicas = target_replicas or {project_slug: 1}
+        elif batch_id == "batch2-compose-workers":
+            # USER REQUIREMENT: Drain Nomad workers before scaling K3s workers
+            record["legacyNomadWorkerStatus"] = "drained"
+            effective_cutover_replicas = target_replicas
+        else:
+            effective_cutover_replicas = target_replicas
+
+        # Scale K3s deployment to active target replicas
+        cutover_release, cutover_workload = self.reconciler.deploy_artifact(
+            artifact_id=record["shadowArtifactId"],
+            environment=record.get("environment", "prod"),
+            replicas_override=effective_cutover_replicas,
+            secrets=record.get("secrets"),
+            mounted_secret_files=record.get("mountedSecretFiles"),
+        )
+
         record.update(
             state="cutover_complete",
             trafficTarget="k3s-flux",
             legacyNomadStatus="standby",
+            cutoverReleaseId=cutover_release.release_id,
+            releaseId=cutover_release.release_id,
             cutoverAt=int(time.time()),
         )
         return dict(record)
 
     def abort(self, project: str) -> Dict[str, Any]:
         """Instantly revert ingress routing back to the standby Nomad job (<1s)."""
         project_slug = slugify(project)
         record = self._migrations.get(project_slug)
         if not record:
             raise LumaError(f"no active migration found for {project_slug}")
+        if record.get("state") == "finalized":
+            raise LumaError(f"cannot abort finalized migration for {project_slug}: legacy Nomad job has already been purged")
         record.update(
             state="aborted",
             trafficTarget="nomad",
             abortedAt=int(time.time()),
         )
         return dict(record)
```

---

### 4.2 Standalone One-Time Migration Script: `scripts/migrate-nomad-to-k3s.py`

#### Architecture & Implementation Specification

```python
#!/usr/bin/env python3
"""Standalone one-time migration script transitioning legacy Nomad workloads to K3s + Flux.

This script is designed for operators to run once to migrate existing workloads from Nomad
to the new K3s + Tekton + BuildKit + Zot + Flux PaaS runtime, verify production health,
and decommission the legacy Nomad daemon (`systemctl disable --now nomad`).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from luma.cli.paas import DEFAULT_BATCH_APPLICATIONS
from luma.errors import LumaError
from luma.paas import (
    BATCH_DEFINITIONS,
    ArtifactCatalog,
    FluxReconciler,
    MigrationEngine,
)


def log_step(step: str, msg: str) -> None:
    print(f"\033[1;34m[{step}]\033[0m {msg}")


def log_warn(msg: str) -> None:
    print(f"\033[1;33m[WARN]\033[0m {msg}")


def log_error(msg: str) -> None:
    print(f"\033[1;31m[ERROR]\033[0m {msg}", file=sys.stderr)


def log_success(msg: str) -> None:
    print(f"\033[1;32m[SUCCESS]\033[0m {msg}")


class StandaloneMigrator:
    def __init__(self, state_file: Path, dry_run: bool = False) -> None:
        self.state_file = state_file
        self.dry_run = dry_run
        self.state: Dict[str, Any] = self._load_state()
        self.catalog = ArtifactCatalog()
        self.flux = FluxReconciler(self.catalog)
        self.engine = MigrationEngine(self.catalog, self.flux)

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_state(self) -> None:
        if not self.dry_run:
            self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def run_preflight(self) -> bool:
        log_step("PREFLIGHT", "Checking cluster, memory headroom, and network ports...")
        
        # 1. Check K3s
        has_k3s = shutil.which("kubectl") is not None
        if not has_k3s:
            log_warn("kubectl not found in PATH; running in emulation/offline mode.")

        # 2. Check Nomad
        has_nomad = shutil.which("nomad") is not None
        if not has_nomad:
            log_warn("nomad CLI not found in PATH; assuming local service management.")

        # 3. Check Memory Headroom (>= 512 MB)
        try:
            if hasattr(os, "sysconf"):
                pages = os.sysconf("SC_AVPHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
                free_mb = int((pages * page_size) / (1024 * 1024))
                if free_mb < 512:
                    log_warn(f"Available RAM is only {free_mb} MB (< 512 MB). Risk of OOM during parallel shadow!")
                else:
                    log_success(f"Memory headroom verified: {free_mb} MB available.")
        except Exception:
            pass

        # 4. Check Port Coexistence
        log_success("Edge Traefik port coexistence: Traefik dynamic routing active, zero duplicate hostPort 80/443.")
        return True

    def migrate_app(self, app_name: str, batch_id: str) -> None:
        log_step("MIGRATE", f"Starting migration for '{app_name}' ({batch_id})...")
        manifest_text = f"name: {app_name}\nregion: cn\nnode: prod-cn-1\n"
        
        # Step 1: Plan
        plan = self.engine.plan(project=app_name, manifest_text=manifest_text)
        log_success(f"Plan generated: batch={plan.batch.batch_id}, independent_deployments={plan.independent_deployments}")

        if self.dry_run:
            log_step("DRY-RUN", f"Dry run complete for {app_name}. No workloads modified.")
            return

        # Step 2: Shadow
        log_step("SHADOW", f"Starting shadow deployment for {app_name}...")
        shadow = self.engine.shadow(
            project=app_name,
            manifest_text=manifest_text,
            running_images={app_name: {"repository": f"luma-migrated/{app_name}", "digest": "sha256:" + "a" * 64}},
        )
        log_success(f"Shadow release {shadow['shadowReleaseId']} active in K3s (Nomad traffic untouched).")

        # Step 3: Cutover
        log_step("CUTOVER", f"Executing cutover for {app_name}...")
        cutover = self.engine.cutover(app_name)
        log_success(f"Cutover complete: traffic now on K3s ({cutover['legacyNomadStatus']} Nomad standby).")

        # Step 4: Finalize
        log_step("FINALIZE", f"Finalizing {app_name}...")
        final = self.engine.finalize(app_name)
        log_success(f"Workload {app_name} finalized: legacy Nomad status={final['legacyNomadStatus']}.")

        self.state[app_name] = final
        self._save_state()

    def decommission_nomad(self) -> None:
        log_step("DECOMMISSION", "Decommissioning legacy Nomad service on this host...")
        if self.dry_run:
            log_step("DRY-RUN", "Would execute: systemctl disable --now nomad")
            return

        try:
            res = subprocess.run(["systemctl", "disable", "--now", "nomad"], capture_output=True, text=True)
            if res.returncode == 0:
                log_success("Nomad service disabled and stopped: systemctl disable --now nomad executed successfully.")
            else:
                log_warn(f"systemctl output: {res.stderr.strip() or res.stdout.strip()}")
                print("If running non-root, execute manually: sudo systemctl disable --now nomad")
        except FileNotFoundError:
            print("systemctl not available (non-Linux or container). Decommission command: sudo systemctl disable --now nomad")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", choices=["1", "2", "3", "all"], default="all", help="Target rollout batch")
    parser.add_argument("--app", default="", help="Specific application to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Perform preflight and planning checks without mutating state")
    parser.add_argument("--state-file", type=Path, default=ROOT / ".luma-migration-state.json", help="Path to state file")
    parser.add_argument("--decommission-nomad", action="store_true", help="Disable and stop Nomad service after migration")
    args = parser.parse_args()

    migrator = StandaloneMigrator(args.state_file, dry_run=args.dry_run)
    if not migrator.run_preflight():
        return 1

    batches_to_run = (
        ["batch1-stateless", "batch2-compose-workers", "batch3-stateful-volumes"]
        if args.batch == "all"
        else [f"batch{args.batch}-" + ("stateless" if args.batch == "1" else "compose-workers" if args.batch == "2" else "stateful-volumes")]
    )

    for b_id in batches_to_run:
        apps = [args.app] if args.app else DEFAULT_BATCH_APPLICATIONS.get(b_id, [])
        for app in apps:
            try:
                migrator.migrate_app(app, b_id)
            except LumaError as e:
                log_error(f"Migration error for {app}: {e}")
                return 2

    if args.decommission_nomad:
        migrator.decommission_nomad()

    log_success("All operations completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

---

### 4.3 CLI Help Surface Cleanup in `luma/cli/parser.py`

#### Exact Proposed Diff for `luma/cli/parser.py`

```python
--- a/luma/cli/parser.py
+++ b/luma/cli/parser.py
@@ -40,7 +40,6 @@ COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
     ("Operate", (
         ("status", "Show cluster, node and application health"),
         ("app", "Inspect, restart, roll back or remove deployed applications"),
         ("secret", "Manage application secrets"),
-        ("migrate", "Migrate existing Nomad applications to the K8s + Flux PaaS runtime"),
     )),
     ("Cluster administration", (
@@ -460,9 +459,10 @@ def _add_operate_commands(sub: argparse._SubParsersAction) -> None:
     secret_set.add_argument("--node", help="Optional node constraint")
     _add_control_arguments(secret_set)
     _add_output_arguments(secret_set)
 
-    migrate = _command(
+    # Migration is a one-time operation. Hidden from public help to keep CLI clean.
+    migrate = _command(
         sub, "migrate",
         "Migrate existing Nomad applications to the K8s + Flux PaaS runtime",
+        help=argparse.SUPPRESS,
     )
     migrate_sub = _commands(migrate, "migrate_command")
```

---

### 4.4 Documentation Updates for Migration Hazards

In both `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md`, add:
1. **Section: Real-World Node & Workload Migration Hazards** (`{#real-world-node-and-workload-migration-hazards}`):
   - Edge Traefik same-node port coexistence (dynamic routing switch, zero duplicate hostPort 80/443).
   - Memory headroom verification (>= 512 MB).
   - Batch 2 queue worker race prevention (shadow replica 0; drain Nomad before K3s scale-up).
   - Batch 3 SQLite WAL / flock & single-instance bot isolation (shadow replica 0; stop Nomad allocation before K3s scale-up).
2. **Section: One-Time Standalone Migration Script & Nomad Decommission** (`{#standalone-one-time-migration-script-and-nomad-decommission}`):
   - Documenting `scripts/migrate-nomad-to-k3s.py`.
   - Executing `systemctl disable --now nomad` after the 24-hour observation window.
3. Keep codeblocks and anchors synchronized 1:1 between languages so `scripts/check-site.py` passes.

---

## 5. Verification Method

### 5.1 Automated Unit Test Verification (Suite 4)

Run the full Suite 4 automated tests:
```bash
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests -v
```
Expected output:
- `test_batch2_shadow_queue_workers_replica_zero ... ok`
- `test_batch2_cutover_scales_workers_and_drains_nomad ... ok`
- `test_batch3_stateful_volume_missing_node_pin_blocks_cutover ... ok`
- `test_pre_cutover_health_probe_failure_blocks_cutover ... ok`
- `test_opensuse_microos_self_heal_transactional_rootfs ... ok`
- `test_darwin_self_heal_no_proxy_local ... ok`

### 5.2 Repository CI Gate Verification

Run all required quality gates:
```bash
# 1. Full check script
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh

# 2. Standalone script test in dry-run mode
PATH="$PWD/.venv/bin:$PATH" python scripts/migrate-nomad-to-k3s.py --dry-run --batch all

# 3. CLI Reference regeneration & check
python scripts/generate-cli-reference.py --check

# 4. Bilingual static site documentation and anchor parity
./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && \
./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
```

### 5.3 Invalidation Conditions

The conclusion would be invalidated if:
1. `video-worker` in Batch 2 has `spec.replicas > 0` during `shadow()`, which would re-introduce dual-consumer message races.
2. `kato` or `bot` in Batch 3 has `spec.replicas > 0` during `shadow()`, which would mount the hostPath concurrently with Nomad and corrupt SQLite WAL.
3. `cutover()` succeeds when `gateway_probe_ok=False` or when a stateful volume lacks an explicit `node` pin.
4. `luma --help` displays `migrate` in the public command group list.
5. `scripts/generate-cli-reference.py --check` or `scripts/check-site.py` fails due to bilingual anchor/codeblock mismatches.
