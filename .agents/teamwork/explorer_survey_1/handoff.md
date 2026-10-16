# Survey Report: R1 End-to-End Implementation Audit & Realization of PaaS Advantages

**Surveyor**: Explorer Survey 1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_survey_1`  
**Target Requirement**: Requirement 1 (R1) — End-to-End Implementation Audit & Realization of PaaS Advantages  
**Scope**: `luma/paas/`, `deploy/paas-stack/`, `luma/cli/paas.py`, `dashboard-src/`, integration with `luma/service.py`, `luma/compose.py`, `luma/regions.py`, `luma/build_capabilities.py`, `luma/control/`  

---

## 1. Observation

Direct observations from source inspection and test executions:

### 1.1 Tekton `<3s` `CancelledRunFinally` Build Termination
1. **Missing Pod `terminationGracePeriodSeconds` & Timeouts**:
   - In `luma/paas/build/controller.py` lines 247–285 (`submit_build`), `pipeline_run_manifest` is constructed:
     ```python
     pipeline_run_manifest = {
         "apiVersion": "tekton.dev/v1",
         "kind": "PipelineRun",
         "metadata": { ... },
         "spec": {
             "pipelineRef": {"name": "luma-paas-build-pipeline"},
             "params": [ ... ],
             "workspaces": [ ... ],
         },
     }
     ```
     Neither `spec.podTemplate: {"terminationGracePeriodSeconds": 3}` nor `spec.timeouts` is defined.
   - In `deploy/paas-stack/tekton/luma-build-pipeline.yaml` lines 43–100 (`luma-buildkit-build-push`), no `stepTemplate` or `terminationGracePeriodSeconds` is set. Kubernetes defaults to a 30-second `terminationGracePeriodSeconds`.
2. **Process Signal Trapping Missing in Step Script**:
   - In `deploy/paas-stack/tekton/luma-build-pipeline.yaml` lines 78–97:
     ```sh
     script: |
       #!/bin/sh
       set -eu
       ...
       buildctl --addr "$(params.buildkit-addr)" build ...
     ```
     `buildctl` is executed directly as a foreground child in `/bin/sh` without `exec` or `trap 'kill -TERM $! 2>/dev/null' TERM INT`.
3. **Pipeline Defines Zero `finally` Tasks**:
   - In `deploy/paas-stack/tekton/luma-build-pipeline.yaml` lines 102–176, `luma-paas-build-pipeline` contains `tasks: [prepare-sources, buildkit-build-push]` but completely lacks a `finally:` block.
4. **Completed / Failed Build Runs Can Be Mutated to "canceled"**:
   - In `luma/paas/build/controller.py` lines 310–327 (`cancel_build`):
     ```python
     record = self._pipeline_runs.get(build_run_id)
     if not isinstance(record, dict):
         raise LumaError(f"build run not found: {build_run_id}")
     now = int(time.time())
     manifest = record["pipelineRunManifest"]
     manifest.setdefault("spec", {})["status"] = "CancelledRunFinally"
     record.update(status="canceled", cancelReason=reason, canceledAt=now, completedAt=now, updatedAt=now, podTerminated=True, buildkitSessionAborted=True)
     ```
     There is no check whether `record.get("status")` is already in a terminal state (`succeeded`, `failed`, or `canceled`).
5. **Inactive Concurrency Accounting on `BuilderEndpoint`**:
   - In `luma/paas/build/controller.py` lines 28–60, `BuilderEndpoint` has `active_builds: int = 0` and is frozen (`@dataclass(frozen=True)`). In `submit_build` (lines 199–308) and `cancel_build` (lines 310–327), `active_builds` is never modified or incremented. `available_slots` permanently equals `max_concurrency` (4).

---

### 1.2 Zot Online GC & Retention Pins (`luma-pin-active-*` / `luma-pin-rollback-*`)
1. **Tag Pin Pattern Inversion**:
   - In `luma/paas/build/artifact_store.py`:
     - Line 323: `pin_tag=f"luma-pin-{project}-{env}-active"`
     - Line 337: `pin_tag=f"luma-pin-{project}-{env}-rb{idx}"`
   - In contracts, docs, and dashboard:
     - `CLAUDE.md:106`: `luma-pin-active-* / luma-pin-rollback-*`
     - `docs/build-and-artifacts.md:31`: `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v<gen>`
     - `dashboard-src/src/components/ReleaseTimelinePanel.tsx:214-215`:
       ```typescript
       selectedDiffIdx === 0
         ? `luma-pin-active-${stack}-prod`
         : `luma-pin-rollback-${stack}-prod-v${currentNum}`
       ```
     - `dashboard-src/src/pages/RegistryPage.tsx:475`: `luma-pin-active-*, luma-pin-rollback-*`
2. **Obsolete Retention Pins Are Never Pruned From Zot**:
   - In `luma/paas/build/artifact_store.py` lines 453–468 (`sync_retention_pins`), `self.pin_digest(repo, digest, str(tag))` issues HTTP PUTs for currently protected items. It never lists or deletes pin tags from previous releases that fell out of the `rollback_window` ($N=5$).
   - In `deploy/paas-stack/zot/zot-config.json` lines 18–19:
     ```json
     "keepTags": [
       { "patterns": ["^luma-pin-.*", "^release-.*"] }, ...
     ]
     ```
     Because tags matching `^luma-pin-.*` are retained forever by Zot GC, unpruned pin tags permanently prevent blob garbage collection.
3. **Repository Name Validation Crash in `ZotRegistryClient`**:
   - In `luma/paas/build/artifact_store.py` lines 436 and 471:
     ```python
     def pin_digest(self, repository: str, digest: str, pin_tag: str) -> None:
         repository = validate_repository(repository)
     ```
   - In `luma/registry_management.py` lines 40–47:
     ```python
     _REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
     def validate_repository(value: Any) -> str:
         repository = str(value or "").strip().lower().strip("/")
         if not repository or not _REPOSITORY_RE.fullmatch(repository):
             raise LumaError(f"invalid registry repository: {value}")
         return repository
     ```
     When `repository` contains a host and port (e.g., `"100.66.177.70:5000/home-app"` from `ServiceImageArtifact.repository`), `validate_repository` raises `LumaError: invalid registry repository: 100.66.177.70:5000/home-app` because `:` is rejected.
4. **Digest Grouping Overwrites Multi-Service Repositories**:
   - In `luma/paas/build/artifact_store.py` lines 292–309:
     ```python
     entry = protected.setdefault(
         img.digest,
         {
             "digest": img.digest,
             "repository": img.repository,
             ...
         },
     )
     ```
     If two services or projects share a digest but have different repositories, only the first repository is recorded; subsequent services are not pinned in their respective repositories.

---

### 1.3 Flux `<5s` Rollback & Zero-Rebuild Cross-Environment Promotion
1. **Missing Immediate Reconcile Annotation in `HelmRelease`**:
   - In `luma/paas/release/helm_compiler.py` lines 274–337 (`compile_flux_helm_release`), `spec.interval` is `"5m"`. No `metadata.annotations` are emitted.
   - Flux CD v2 requires `reconcile.fluxcd.io/requestedAt` on the `HelmRelease` object to trigger an instant reconciliation rather than waiting for the 5-minute polling tick.
2. **Rollback and Promotion Drop Secrets to Empty**:
   - In `luma/paas/release/flux_reconciler.py` lines 144–161 (`promote_artifact`) and lines 220–225 (`rollback_release`):
     ```python
     return self.deploy_artifact(
         artifact_id=target.artifact_id,
         environment=environment,
         replicas_override=target.replicas_by_service,
         mounted_secret_files=target.mounted_secret_files,
     )
     ```
     `secrets` is omitted (defaults to `None`). `_build_secret_manifest` renders `stringData: {}`, wiping out environment variables and secrets on rollback or promotion.

---

### 1.4 Compose Independent `Deployment` Splitting vs `network_mode: service:*` Pod Sidecars
1. **PVCs Created But Never Mounted in Compose Workloads**:
   - In `luma/paas/release/helm_compiler.py`:
     - Lines 594–619: Appends `PersistentVolumeClaim` manifests to `pvcs`.
     - Lines 679–684: Builds `pod_spec`:
       ```python
       pod_spec: Dict[str, Any] = {
           "containers": containers,
           "volumes": list(secret_vols),
           "securityContext": {"fsGroup": int(luma_svc.get("fsGroup") or 1000)},
       }
       ```
     - Lines 644–649: Builds `main_container`:
       ```python
       "volumeMounts": list(secret_mounts)
       ```
     The PVCs in `pvcs` are never appended to `pod_spec["volumes"]` and never referenced in `volumeMounts`. Containers run without their persistent storage.
2. **Sidecar Container Drops Custom `command` and Inline Configurations**:
   - In `luma/paas/release/helm_compiler.py` lines 658–676: `child_container` does not check `child_body.get("command")` (unlike `main_container`), dropping command-line arguments.
3. **Architecture Mismatch Between Pod Containers Not Enforced**:
   - In `luma/paas/release/helm_compiler.py` lines 658–676: No validation compares `child_img.platform` with `main_container.platform`.
4. **Missing `kubernetes.io/arch` in Node Selector**:
   - In `luma/paas/release/helm_compiler.py` lines 41–49 (`_build_region_node_selector`), only `luma.io/region`, `topology.kubernetes.io/region`, and `kubernetes.io/hostname` are set. `kubernetes.io/arch` is missing.
5. **Unvalidated Chained or Cyclic Sidecars**:
   - In `luma/paas/release/helm_compiler.py` lines 576–585, `sidecar_pairs` does not verify whether `target_svc` is itself a sidecar or in a cycle.

---

### 1.5 All 6 Exposure Modes
1. **Explicit `exposure: none` Forcibly Overridden**:
   - In `luma/paas/release/helm_compiler.py` lines 473–493:
     ```python
     if exposure != "none" or not domain:
         ingresses, probe_url = _compile_exposure_ingress(...)
     else:
         ingresses, probe_url = _compile_exposure_ingress(
             name=project,
             namespace=namespace,
             region=region or "cn",
             exposure="cn-edge" if (region or "cn") == "cn" else "external-edge",
             domain=domain,
             port=port,
             publish_port=publish_port,
         )
     ```
     If a manifest specifies `exposure: none` with a residual `domain:`, the `else` branch overrides `exposure` to `"cn-edge"` or `"external-edge"`.
2. **`tcp-relay` Service Port Named `"http"`**:
   - In `luma/paas/release/helm_compiler.py` lines 469 and 748, the K8s Service port is named `"http"` instead of `"tcp"`.
3. **`tcp-relay` Container Port Defaults to 3000 Instead of `publishPort`**:
   - In `luma/paas/release/helm_compiler.py` line 357, `port = int(parsed.get("port") or 3000)`. If `port` is omitted and only `publishPort` is set, `containerPort` becomes 3000.

---

### 1.6 CLI & Control Server Disconnects
1. **In-Memory CLI State Prevents Multi-Command Workflows**:
   - In `luma/cli/paas.py` lines 24–26 and 35–36:
     ```python
     _DEFAULT_CATALOG = ArtifactCatalog()
     _DEFAULT_RECONCILER = FluxReconciler(_DEFAULT_CATALOG)
     _DEFAULT_MIGRATOR = MigrationEngine(_DEFAULT_CATALOG, _DEFAULT_RECONCILER)
     ```
     State is kept in ephemeral module-level variables. Each CLI command runs in a new process, losing all artifact and migration state between commands.
2. **`luma app scale` Silently Swallows `LumaError` and Fakes Success**:
   - In `luma/cli/paas.py` lines 190–197:
     ```python
     try:
         release, _ = reconciler.scale_service(...)
     except LumaError:
         payload = {
             "application": args.name,
             "environment": environment,
             "service": service_name,
             "replicas": args.replicas,
             "status": "scaled",
         }
     ```
     `cmd_app_scale` swallows errors and outputs a mocked success message.
3. **Web Dashboard `ServiceReplicaScaler` Completely Disconnected From Backend**:
   - In `dashboard-src/src/components/ReleaseTimelinePanel.tsx` lines 41–54, `adjustReplica` only mutates React component state (`setReplicaOverrides`) and displays a fake alert without sending any API call.
4. **`luma deploy` and `luma compose-deploy` Ignore `--artifact`**:
   - In `luma/cli/parser.py` lines 212 and 235, `--artifact` is registered. In `luma/cli/deploy.py` lines 97–200 and lines 278–360, `args.artifact` is never read or passed to `FluxReconciler`.
5. **`cmd_migrate_shadow` Does Not Support Multi-Service Compose**:
   - In `luma/cli/paas.py` lines 294–301, `running_images` only contains `{args.name: ...}`, failing to supply images for individual services in a Compose stack.

---

## 2. Logic Chain

1. **Premise**: Tekton promises `<3s` build cancellation (`CancelledRunFinally`), Flux promises `<5s` rollback, Zot guarantees online GC with retention pins, Compose guarantees independent Deployment scaling with sidecar pairing, and all 6 exposure modes must be valid.
2. **Tekton `<3s` Invalidation**:
   - From Obs 1.1: Without `terminationGracePeriodSeconds: 3` on the PipelineRun pod template, Kubernetes defaults to 30s.
   - From Obs 1.2: Without `exec` or signal traps in `/bin/sh`, PID 1 does not forward SIGTERM to `buildctl`.
   - **Inference**: Pod termination requires 30s until SIGKILL is sent by the kubelet, directly violating the `<3s` guarantee.
3. **Zot GC Invalidation**:
   - From Obs 2.1: `artifact_store.py` produces `luma-pin-<proj>-<env>-active` and `luma-pin-<proj>-<env>-rb<idx>`, whereas `zot-config.json`, docs, and UI expect `luma-pin-active-*` and `luma-pin-rollback-*`.
   - From Obs 2.2: Stale rollback pin tags are never deleted via OCI manifest delete API. Because `zot-config.json` protects `^luma-pin-.*`, expired images are pinned indefinitely and cannot be reclaimed by Zot GC.
   - From Obs 2.3: `ZotRegistryClient.pin_digest` passes `repository` containing host/port to `validate_repository`, which crashes on `:` with `LumaError`.
   - **Inference**: In production, tag pinning throws an exception on real registry URLs, and if bypassed, pins accumulate indefinitely, causing registry disk exhaustion.
4. **Flux `<5s` Invalidation**:
   - From Obs 3.1: Without `reconcile.fluxcd.io/requestedAt` annotation on the `HelmRelease`, Flux only reconciles at its 5-minute interval (`spec.interval: "5m"`).
   - From Obs 3.2: Rollback and promotion pass `secrets=None`, which clears `stringData`, wiping out runtime application configuration.
   - **Inference**: Rollback does not take `<5s` (takes up to 300s) and results in degraded workloads with missing secrets.
5. **Compose & Sidecar Invalidation**:
   - From Obs 4.1: Volume definitions in Compose create PVCs but never add them to `pod_spec["volumes"]` or container `volumeMounts`.
   - From Obs 4.2 & 4.3: Sidecar containers drop command args and ignore architecture mismatches.
   - **Inference**: Stateful Compose applications fail to mount storage, and cross-arch sidecars crash on deployment.
6. **Exposure Modes & CLI Invalidation**:
   - From Obs 5.1: `compile_service_release` forcibly overrides `exposure: none` to `cn-edge` or `external-edge` if `domain` is present.
   - From Obs 6.1–6.5: CLI commands do not persist state across process invocations, `cmd_app_scale` mocks success upon failure, the Web Dashboard scaler sends zero network requests, and `--artifact` is ignored in `luma deploy`.
   - **Inference**: The PaaS CLI and Dashboard surfaces are largely decoupled mock frontends rather than integrated production controls.

---

## 3. Caveats

- **Existing Legacy Nomad Engine**: The repository currently operates primarily with Nomad as the active deployment target (`luma/nomad_render.py`, `luma/control/`). PaaS is designed as the next-generation Kubernetes/Flux engine (`luma/paas/`), so fixes must avoid breaking legacy Nomad commands and passing gates.
- **Test In-Memory Isolation**: Existing tests in `tests/test_paas_engine.py` execute within a single Python process and initialize `ArtifactCatalog(dict())`, which masks CLI process exit data loss and `validate_repository` URL host errors (because mocks/unit tests didn't call real HTTP endpoints).

---

## 4. Conclusion

The architectural foundations in `luma/paas/` are well-structured, but critical realization gaps and bugs prevent the advertised advantages from functioning in production:
1. **Tekton Cancellation**: Takes 30s instead of `<3s` due to missing `podTemplate.terminationGracePeriodSeconds` and untrapped shell script execution.
2. **Zot Retention Pins**: Inverted tag naming conventions (`luma-pin-<proj>-<env>-active` vs `luma-pin-active-<proj>-<env>`), lack of stale tag deletion (unbounded disk growth), and repository host validation crashes.
3. **Flux Rollback & Promotion**: Missing `reconcile.fluxcd.io/requestedAt` causes 5-minute delays, and secret omission strips all secrets on rollback/promotion.
4. **Compose Workload Compilation**: PVCs are created but never mounted to containers; sidecar command/arch validation is missing.
5. **Exposure Modes**: `exposure: none` is overridden when a domain string is present, and TCP relay Service ports are misconfigured as HTTP.
6. **CLI & Dashboard Disconnects**: Ephemeral in-memory CLI state prevents real multi-step workflows, `luma app scale` swallows errors, UI slider does not make API calls, and `luma deploy --artifact` ignores the flag.

---

## 5. Verification Method

To independently verify these observations and validate downstream fixes:

1. **Verify Unit Tests Pass**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
2. **Verify Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
3. **Verify Documentation & Bilingual Site Build**:
   ```bash
   ./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
   ```
4. **Specific Code Inspection Points**:
   - `luma/paas/build/controller.py`: Check lines 264–285 (`spec.podTemplate` and `spec.timeouts`), lines 310–327 (terminal state checks in `cancel_build`).
   - `deploy/paas-stack/tekton/luma-build-pipeline.yaml`: Check line 79 (`exec buildctl` or signal trap).
   - `luma/paas/build/artifact_store.py`: Check lines 323, 337 (pin tag naming), line 436 (`validate_repository` stripping host), and lines 453–468 (stale pin tag deletion).
   - `luma/paas/release/helm_compiler.py`: Check lines 290–337 (`reconcile.fluxcd.io/requestedAt` annotation), lines 594–684 (PVC volume mounts in Compose), line 473 (respect `exposure: none`).
   - `luma/paas/release/flux_reconciler.py`: Check lines 144–161 and 220–225 (preserving secrets during rollback and promotion).
   - `luma/cli/paas.py`: Check persistent state integration and real error handling in `cmd_app_scale`.
