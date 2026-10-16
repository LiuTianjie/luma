# Handoff Report: Survey of Requirement 3 (R3) & Test Verification Gates

## 1. Observation

### 1.1 Batch 2 Queue Worker Race Prevention in `luma/paas/migration/migrator.py`

- **Line 324 (`MigrationEngine.shadow`)**:
  ```python
  release, workload = self.reconciler.deploy_artifact(
      artifact_id=artifact_id,
      environment=environment,
      secrets=secrets,
      mounted_secret_files=mounted_secret_files,
  )
  ```
  `deploy_artifact` is called with no `replicas_override`.
- **`luma/paas/release/helm_compiler.py` Lines 624-630 (`compile_compose_release`)**:
  ```python
  default_replicas = int(luma_svc.get("replicas") or (svc_body.get("deploy") or {}).get("replicas") or 1)
  replicas = int(
      replicas_override[svc_name]
      if replicas_override and svc_name in replicas_override
      else default_replicas
  )
  ```
  Because `replicas_override` is omitted in `shadow()`, every compose service (e.g., `video-worker`, `browser-worker`, `worker`) in stacks like `serverx`, `granary`, and `codex-gitea` is scheduled with replicas $\ge 1$.
- **Consequence**: Both the legacy Nomad allocation and the newly launched K3s shadow release run worker pods simultaneously against the same production message queue / database, creating dual-consumer race conditions during shadow testing.
- **Line 343-356 (`MigrationEngine.cutover`)**:
  ```python
  def cutover(self, project: str) -> Dict[str, Any]:
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
  `cutover()` performs no worker orchestration. Paused workers are not scaled up, and legacy Nomad workers are not drained or paused.
- **Line 357-369 (`MigrationEngine.abort`)**:
  `abort()` only updates `trafficTarget="nomad"` and does not scale K3s workers back to 0.

---

### 1.2 Batch 3 Stateful Volume & Secret Preflight in `migrator.py` & `helm_compiler.py`

- **`migrator.py` Lines 232-251 (`plan`)**:
  ```python
  adopted_vols: list[Dict[str, Any]] = []
  for vol in existing_volumes:
      if not isinstance(vol, Mapping):
          continue
      vol_name = str(vol.get("name") or "data")
      host_path = str(vol.get("hostPath") or f"/var/lib/luma/volumes/{project_slug}/{vol_name}")
      node_name = str(vol.get("node") or parsed_manifest.get("node") or "")
      adopted_vols.append(...)
      if not node_name:
          warnings.append(
              f"Volume '{vol_name}' has no explicit node pin; pin a node before cutover to keep local data in-place."
          )
  ```
  - Volumes declared inside `manifest_text` (`volumes:` or `storage:`) and `compose_text` are not inspected if `existing_volumes` argument is empty. Thus `kato` and `bot` can be mistakenly classified as Batch 1.
  - A missing `node` pin only generates a warning in `plan()`; it does not block anything.
- **`migrator.py` Lines 288-293 (`shadow`)**:
  `shadow()` invokes `plan()` without forwarding `existing_volumes` or checking `plan.ready`. Any volume metadata analyzed in `plan()` is wiped out.
- **`migrator.py` Lines 343-356 (`cutover`)**:
  `cutover()` contains ZERO validation gates:
  1. No check for missing `node` pin on adopted stateful volumes.
  2. No check for `gateway_probe_ok` (even if `release.gateway_probe_ok == False` or `release.status != "healthy"`, cutover succeeds).
  3. No check for secret + mounted secret file parity or `env_digest` drift.
  4. No check that the shadow release image architecture matches the target host node architecture.
- **`luma/paas/release/helm_compiler.py` Line 420 (`compile_service_release`)**:
  ```python
  "securityContext": {"fsGroup": 1000},
  ```
  `fsGroup` is hardcoded to `1000` instead of reading the manifest's configured `fsGroup` / UID / GID.
- **`luma/paas/release/helm_compiler.py` Lines 41-49 (`_build_region_node_selector`)**:
  `kubernetes.io/arch` is never injected into `nodeSelector`.
- **`luma/paas/release/helm_compiler.py` Lines 658-676 (`compile_compose_release`)**:
  No validation exists to ensure sidecar containers (`network_mode: service:*`) share the same CPU architecture as the primary container.

---

### 1.3 CLI Disconnects in `luma/cli/paas.py` and `luma/cli/parser.py`

- **`luma/cli/paas.py` Lines 245-258 (`_load_migration_manifest_texts`)**:
  When `--manifest` is omitted, it defaults to a generic single-service manifest (`port: 3000`), ignoring multi-service compose applications in `DEFAULT_BATCH_APPLICATIONS` (e.g. `serverx`, `granary`, `codex-gitea`).
- **`luma/cli/paas.py` Lines 294-301 (`cmd_migrate_shadow`)**:
  ```python
  running_images = {
      args.name: {
          "repository": f"luma-migrated/{args.name}",
          "digest": digest_seed,
          "tag": "nomad-adopted",
          "platform": "linux/amd64",
      }
  }
  ```
  `platform` is hardcoded to `"linux/amd64"`, breaking ARM64 services on openSUSE MicroOS or Apple Silicon macOS. Furthermore, only a single image key (`args.name`) is passed, omitting compose child services.

---

### 1.4 Documentation & Static Site Constraints

- **`scripts/check-site.py` Lines 65-71**:
  ```python
  fences = lambda text: re.findall(r'^```[^\n]*\n.*?^```[^\n]*', text, re.M | re.S)
  if fences(source.read_text()) != fences(translated.read_text()):
      errors.append(f'{relative}: translated command/code examples differ')
  original_page = root / 'docs' / relative.with_suffix('.html')
  original_ids = pages[original_page].ids
  if not original_ids <= pages[output_path].ids:
      errors.append(f'{relative}: translation loses heading anchors {original_ids - pages[output_path].ids}')
  ```
  Every code block in `docs/migration-guide.md` must have an exact byte-for-byte matching counterpart in `docs/migration-guide.zh-CN.md`. All HTML anchors and heading IDs must align.
- **Current documentation (`docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`)**:
  Both files are high-level overviews (56 lines) that omit the concrete technical details of Batch 2 queue worker pause/cutover orchestration, Batch 3 node-pinning invariants, secret parity checks, and pre-cutover safety blocks.

---

### 1.5 Test Suite Coverage in `tests/test_paas_engine.py`

- `tests/test_paas_engine.py` currently contains only 5 unit tests:
  1. `test_dockerfile_apt_apk_https_mirror_acceleration`
  2. `test_buildkit_waterfall_and_multirepo_git_contexts`
  3. `test_luma_signature_features_in_service_and_compose_compiler`
  4. `test_compose_independent_deployments_flux_promotion_and_zot_gc_pins`
  5. `test_four_step_migration_engine_and_cli_surface`
- There are no test cases verifying:
  - Batch 2 shadow keeping queue worker replicas at 0 while web replicas are 1.
  - Cutover scaling of paused queue workers and drain of legacy Nomad workers.
  - Abort restoring traffic to Nomad and setting K3s workers to 0.
  - Batch 3 missing `node` pin blocking `cutover` with `LumaError`.
  - Batch 3 preserving `hostPath` and `fsGroup` without data copying.
  - Shadow health probe failure (`gateway_probe_ok=False`) blocking `cutover`.
  - Secret out-of-sync or mounted secret file mismatch blocking `cutover`.
  - Image architecture mismatch against target node architecture blocking `cutover`.

---

### 1.6 Acceptance Gates in `scripts/check-luma.sh`

- The full repository gate runs 10 steps:
  1. `python scripts/bump-version.py --check`
  2. `python scripts/generate-cli-reference.py --check` (CLI doc generator fails if parser help is changed without updating docs)
  3. `python -m ruff check luma tests scripts observe`
  4. `bash scripts/validate-stacks.sh`
  5. `npm run typecheck:dashboard`
  6. `npm run build:dashboard`
  7. `python -m unittest discover -s tests -p 'test_*.py'` (1500 unit tests)
  8. `node --experimental-strip-types --test tests/dashboard/*.test.mjs`
  9. `node --test dashboard-src/tests/*.cjs`
  10. `git diff --check`
- **Flaky timing observation**: In `tests/test_product_config.py` line 1225 (`test_local_executor_timeout_returns_text_output`), `LocalExecutor().run_result("printf before-timeout; sleep 2", timeout=1)` can fail under high CPU load when `bash -lc` login shell takes >1s before `printf` executes.

---

## 2. Logic Chain

1. **Premise 1**: In `MigrationEngine.shadow()`, the shadow K3s release is deployed via `reconciler.deploy_artifact()` with no replica overrides.
2. **Premise 2**: In multi-service Compose applications (`serverx`, `granary`, `codex-gitea`), background worker containers pull jobs from shared databases or message queues (e.g. Redis, RabbitMQ).
3. **Inference 1**: Deploying workers at normal replica count during shadow mode creates an active dual-consumer race condition between the live Nomad allocation and the shadow K3s release, violating R3.
4. **Remediation 1**: `MigrationEngine.shadow()` must classify services into web/API vs queue/background workers (using name heuristics `worker`, `queue`, `consumer` or absence of public ports/domains). It must pass `replicas_override` with `replicas: 0` for all worker services, while preserving web/API replicas at $\ge 1$ for gateway probe verification.
5. **Premise 3**: In `cutover()`, live traffic transitions from Nomad to K3s.
6. **Inference 2**: When cutover occurs, the shadow workers at replica 0 must be scaled up to their configured target count, while Nomad workers are drained/marked standby. If aborted, K3s workers must be scaled back to 0.
7. **Premise 4**: Stateful applications (`kato`, `bot`) depend on local persistent volumes stored at specific host paths on a specific Nomad node.
8. **Premise 5**: K3s local PVs require `nodeAffinity` matching the exact host where the data resides. Moving or deploying without a node pin results in volume loss or empty host paths.
9. **Inference 3**: `MigrationEngine` must enforce that stateful volumes have an explicit `node` pin matching the legacy Nomad host, preserve `hostPath` and `fsGroup`, and `cutover()` must explicitly reject any stateful application lacking a node pin.
10. **Premise 6**: A cutover cannot be considered zero-downtime if the shadow release failed its health probes, if secrets are out of sync, or if the container binary architecture does not match the node architecture.
11. **Inference 4**: `MigrationEngine.cutover()` must validate `release.gateway_probe_ok`, verify secret parity, and verify image architecture before permitting cutover.

---

## 3. Caveats

- **Existing Volumes Extraction**: `plan()` should support both explicitly supplied `existing_volumes` and volume definitions embedded directly in `manifest_text` / `compose_text`.
- **Heuristic vs Explicit Worker Classification**: While names like `video-worker` and `browser-worker` are standard in Luma compose files, the engine should also support explicit annotations (e.g., `role: worker` or `worker: true`) and default any service without ports/ingress to worker behavior.
- **Node Self-Heal in Sandbox**: Unit tests for `evaluate_node_self_heal` must test `opensuse-microos` with btrfs scrub/trim and transactional rootfs in memory without invoking root-level host mutations.
- **CLI Reference Generator**: If CLI flags in `luma/cli/parser.py` are altered, `python scripts/generate-cli-reference.py` must be run to prevent `scripts/check-luma.sh` from failing on `generate-cli-reference.py --check`.

---

## 4. Conclusion

The current implementation of the 4-step migration engine in `luma/paas/migration/migrator.py` is incomplete and unsafe for production across all three rollout batches:
1. **Batch 2**: Suffers from queue worker dual-consumption during shadow mode because worker replicas are not paused at `0`. Cutover does not orchestrate worker scale-up or drain.
2. **Batch 3**: Lacks node-pin validation, volume extraction from compose YAML, and secret parity verification. Cutover proceeds unconditionally even with missing node pins.
3. **Pre-Cutover Gates**: Completely absent in `cutover()`; probe failures, secret drifts, and architecture mismatches are silently ignored.
4. **Docs & Tests**: Documentation lacks architectural and CLI details; `test_paas_engine.py` lacks automated test cases verifying these invariants.

### Concrete Actionable Remediation Plan

1. **Update `luma/paas/migration/migrator.py`**:
   - In `classify_application_batch`: Inspect `manifest_text` and `compose_text` for volumes when `existing_volumes` is omitted.
   - In `plan`: Identify worker services vs web services; record `queue_workers` in `MigrationPlan`.
   - In `shadow`: Compute `replicas_override` setting queue/background worker replicas to `0`; pass `replicas_override` to `deploy_artifact`; record paused worker targets; enforce secret parity preflight.
   - In `cutover`:
     - Block cutover (`raise LumaError`) if:
       - Target release has `gateway_probe_ok is False` or `status != "healthy"`.
       - Batch 3 stateful volumes lack an explicit `node` pin.
       - Secrets or mounted secret files are out of sync.
       - Shadow image architecture does not match target node architecture.
     - Scale up paused queue workers to target replica counts via `reconciler.scale_service`.
     - Record `workerCutoverComplete=True` and legacy Nomad status as standby/drained.
   - In `abort`: Scale K3s worker replicas back down to `0`.
   - In `finalize`: Verify `orphanQueueWorkersRemaining == 0`.
2. **Update `luma/paas/release/helm_compiler.py`**:
   - In `compile_service_release` and `compile_compose_release`: Inject `kubernetes.io/arch` (`arm64` / `amd64`) into `nodeSelector` matching the image platform; preserve `fsGroup` from manifest/volume; reject mismatched container/sidecar architectures.
3. **Update `tests/test_paas_engine.py`**:
   - Add `test_batch2_queue_worker_race_prevention_and_cutover_scaling`.
   - Add `test_batch3_stateful_volume_node_pin_and_cutover_gate`.
   - Add `test_migration_secret_parity_and_probe_cutover_guards`.
   - Add `test_migration_image_architecture_node_match_guard`.
4. **Update `docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`**:
   - Expand Batch 2 queue worker race prevention and two-phase cutover documentation.
   - Expand Batch 3 local PV adoption, hostPath/fsGroup preservation, and preflight gate documentation.
   - Ensure byte-for-byte codeblock parity and matching heading anchor IDs for `check-site.py`.

---

## 5. Verification Method

### 5.1 Test Execution Commands

```bash
# 1. Run unit tests for PaaS Engine and verify all migration invariants
./.venv/bin/python -m unittest tests.test_paas_engine -v

# 2. Build and validate bilingual documentation parity
./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site

# 3. Regenerate CLI reference if argparse options or docstrings were modified
./.venv/bin/python scripts/generate-cli-reference.py --check

# 4. Full repository test and lint gate
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
```

### 5.2 Files to Inspect

- `/Users/turning4th/infra-stacks/luma/paas/migration/migrator.py`
- `/Users/turning4th/infra-stacks/luma/cli/paas.py`
- `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py`
- `/Users/turning4th/infra-stacks/docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`
- `/Users/turning4th/infra-stacks/tests/test_paas_engine.py`

### 5.3 Invalidation Conditions

The remediation is considered invalidated if:
1. `shadow()` deploys worker replicas $> 0$ for Batch 2 applications.
2. `cutover()` succeeds when a Batch 3 stateful volume has an empty `node` pin.
3. `cutover()` succeeds when `gateway_probe_ok` is `False`.
4. `cutover()` succeeds when secrets mismatch the shadow release `env_digest`.
5. Code fences or header IDs differ between `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md`.
6. `scripts/check-site.py` or `scripts/check-luma.sh` returns non-zero.
