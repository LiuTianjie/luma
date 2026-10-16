# Reviewer & Adversarial Critic M4-R3-2 Handoff Report: Hardening Verification of Feature 8 (Nomad to K3s Migration Engine)

## Explicit Verdict: `APPROVE`

---

## 1. Observation

A comprehensive code review and adversarial challenge of Milestone 4 Feature 8 was executed across `luma/paas/migration/migrator.py`, `scripts/migrate-nomad-to-k3s.py`, `luma/cli/parser.py`, and `tests/test_paas_engine.py` using `./.venv/bin/python`.

### 1.1 Direct Source Code Observations

1. **`_normalize_node_pin` Implementation** (`luma/paas/migration/migrator.py:338-345`):
   ```python
   def _normalize_node_pin(value: Any) -> str:
       """Normalize node pin name; treat None, empty, 'none', 'null', '~' as unpinned ("")."""
       if value is None:
           return ""
       val = str(value).strip()
       if val.lower() in {"", "none", "null", "~"}:
           return ""
       return val
   ```
   - Input `None` directly returns `""`.
   - String values `"None"`, `"none"`, `"null"`, `"NULL"`, `"~"`, `""`, and whitespace strings are mapped case-insensitively to `""`.
   - Valid node pins (e.g. `"prod-cn-1"`, `"worker-1"`, `123`) return their stripped string representation.

2. **Gate 2: Stateful Volume Node Pin Enforcement** (`luma/paas/migration/migrator.py`):
   - In `plan()` (`lines 402, 417-432`):
     ```python
     manifest_node = _normalize_node_pin(parsed_manifest.get("node"))
     ...
     raw_node = vol.get("node") if "node" in vol else parsed_manifest.get("node")
     node_name = _normalize_node_pin(raw_node)
     adopted_vols.append({..., "node": node_name, ...})
     if not node_name:
         warnings.append(
             f"Volume '{vol_name}' has no explicit node pin; pin a node before cutover to keep local data in-place."
         )
     ```
     When unpinned, `plan.ready` evaluates to `False` (`ready=len(warnings) == 0`).
   - In `shadow()` (`line 607`):
     ```python
     "adoptedVolumes": [{**vol, "node": _normalize_node_pin(vol.get("node"))} for vol in plan.adopted_volumes],
     ```
     Normalized node pin values are strictly persisted into migration record state.
   - In `cutover()` (`lines 649-657`):
     ```python
     adopted_vols = record.get("adoptedVolumes") or []
     for vol in adopted_vols:
         node_pin = _normalize_node_pin(vol.get("node"))
         if not node_pin:
             raise LumaError(
                 f"cannot cut over {project_slug}: stateful volume '{vol.get('volumeName', 'data')}' "
                 f"requires an explicit node pin matching legacy Nomad host before cutover"
             )
     ```
     Unpinned stateful volumes (`None`, `"None"`, `"null"`, `"~"`, `""`, whitespace) strictly raise `LumaError`.

3. **Gate 3: Architecture Matching & Platform Normalization** (`luma/paas/migration/migrator.py`):
   - In `shadow()` (`lines 555-567, 609-618`):
     ```python
     raw_plat = str(img_info.get("platform") or "").strip()
     plat = (
         "linux/amd64"
         if not raw_plat or raw_plat.lower() in {"none", "null"}
         else raw_plat
     )
     images[svc_name] = ServiceImageArtifact(
         ...,
         platform=plat,
     )
     ...
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
     ```
     Omitted, null, or empty platforms are normalized to `"linux/amd64"` and persisted in `record["runningImages"]`.
   - In `cutover()` (`lines 659-673`):
     ```python
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
     ```
     Workloads with omitted or amd64 platform are blocked from cutting over onto `arm64` nodes with `LumaError`.

4. **Gate 4: Image Digest Drift Detection** (`luma/paas/migration/migrator.py`):
   - In `shadow()` (`lines 552-554, 613`):
     Synthetic `sha256:` digests are computed when omitted, and the resolved digest is saved to `record["runningImages"][k]["digest"]`.
   - In `cutover()` (`lines 675-693`):
     ```python
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
     ```
     Comparing against `current_running_images` strictly detects digest changes and raises `LumaError`.

5. **Volume Isolation & Two-Phase Cutover** (`luma/paas/migration/migrator.py`):
   - In `shadow()` (`lines 515-533`):
     - Batch 3 stateful apps (`kato`, `bot`, or any app with local PV volumes) force `shadow_replicas = {svc: 0 for svc in plan.services}`.
     - Batch 2 Compose apps with workers (`serverx`, `granary`, `codex-gitea`) force worker replicas to `0` while maintaining web replicas at target.
   - In `cutover()` (`lines 704-724`):
     - For stateful workloads: `record["legacyNomadAllocationStopped"] = True` and `record["nomadStoppedAt"] = int(time.time())` BEFORE scaling K3s deployments.
     - For worker workloads: `record["legacyNomadWorkerStatus"] = "drained"` BEFORE scaling K3s worker deployments.

6. **Standalone Script & CLI Surface Cleanliness**:
   - `luma/cli/parser.py`: `migrate` subparser is configured with `help=argparse.SUPPRESS` (`line 467`) and omitted from `COMMAND_GROUPS` (`lines 22-54`).
   - `scripts/migrate-nomad-to-k3s.py`: Full standalone migration orchestrator with preflight checks (kubectl, nomad, 512MB RAM headroom, storage, Traefik port coexistence), batch migration execution, and post-migration Nomad decommission (`systemctl disable --now nomad`).

### 1.2 Verbatim Test & Tool Outputs

1. **Bug Reproduction Script Verification**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/reproduce_feature8_bugs.py
   ```
   Output:
   ```
   AssertionError: Expected 'None', got ''
   ```
   *Result*: The previously reported bypass (where `vol["node"]` was stringified to `"None"`) has been eradicated.

2. **Challenger Stress Test Suite**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/stress_test_m4_feature8.py
   ```
   Output:
   ```
   Ran 21 tests in 0.349s
   OK
   ```

3. **PaaS Engine and Native Builder Test Suites**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   Output:
   ```
   Ran 145 tests in 1.335s
   OK
   ```

4. **Repository Check Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   Output:
   - `bump-version.py --check`: Passed
   - `generate-cli-reference.py --check`: Passed
   - `ruff check luma tests scripts observe`: Passed (All checks passed!)
   - `validate-stacks.sh`: Passed
   - `npm run typecheck:dashboard`: Passed
   - `npm run build:dashboard`: Passed
   - `python -m unittest discover -s tests -p 'test_*.py'`: Passed (1688 tests)
   - `node --test tests/dashboard/*.test.mjs`: Passed (41 tests)
   - `node --test dashboard-src/tests/*.cjs`: Passed (69 tests)
   - `git diff --check`: Passed
   - Exit code: `0`

5. **CLI Help Inspection**:
   - `python -m luma.cli --help`: Does NOT list `migrate` in any command group.
   - `python -m luma.cli migrate --help`: Operates cleanly, displaying subcommands `batches`, `plan`, `shadow`, `cutover`, `abort`, `finalize`.
   - `python scripts/migrate-nomad-to-k3s.py --dry-run`: Completes successfully across batches 1, 2, 3 with exit code `0`.

---

## 2. Logic Chain

1. **Gate 2 Stateful Volume Node Pin Invariant**:
   - *Requirement*: Local PV hostPath volumes must retain explicit pinning to the legacy Nomad host to prevent data loss, unattached hostPaths, or multi-node split-brain writes.
   - *Direct Evidence*: `_normalize_node_pin` safely maps null-like representations (`None`, `"None"`, `"null"`, `"~"`, whitespace) to `""`. In `plan()`, unpinned volumes generate a warning and set `plan.ready = False`. In `cutover()`, `_normalize_node_pin` is re-applied, and an empty pin raises `LumaError`.
   - *Conclusion*: Gate 2 is fully hardened; bypass via stringified null or None is impossible.

2. **Gate 3 Architecture Matching Invariant**:
   - *Requirement*: Applications must not run on mismatched CPU architectures, especially preventing `linux/amd64` binaries from being cut over onto `linux/arm64` openSUSE MicroOS or Apple Silicon nodes.
   - *Direct Evidence*: When `platform` is omitted, `shadow()` normalizes it to `"linux/amd64"` and stores it in `record["runningImages"]`. Gate 3 in `cutover()` evaluates `target_arch not in img_platform`, rejecting `amd64` images on `arm64` nodes with `LumaError`.
   - *Conclusion*: Architecture validation cannot be skipped by omitting the platform field.

3. **Gate 4 Drift Detection Invariant**:
   - *Requirement*: Image drift between shadow observation and cutover must be detected.
   - *Direct Evidence*: `shadow()` persists resolved image digests into `record["runningImages"]`. In `cutover()`, comparing against `current_running_images` checks `curr_digest != orig_digest` with fallback to catalog artifacts.
   - *Conclusion*: Gate 4 reliably detects image digest modifications.

4. **Integrity & Code Quality Assessment**:
   - *Integrity Check*: No hardcoded project checks, fake mocks, or facades were added. The logic in `_normalize_node_pin`, `plan`, `shadow`, and `cutover` is general-purpose, robust, and clean.
   - *Automated Regression Testing*: Three dedicated unit tests (`test_cutover_rejects_node_none_or_null_stringification`, `test_cutover_rejects_architecture_mismatch_when_platform_omitted`, `test_cutover_detects_digest_drift_when_initial_digest_omitted`) guarantee permanent regression prevention.

---

## 3. Caveats

- **External Cluster Tooling**: `scripts/migrate-nomad-to-k3s.py` gracefully detects whether `kubectl` and `nomad` CLI binaries are present on the host. In development / dry-run environments where Nomad or K3s daemons are not running, it executes preflight warnings without blocking dry-run planning.
- **Decommission Safety**: `scripts/migrate-nomad-to-k3s.py` requires `--decommission-nomad` to be explicitly passed before attempting `systemctl disable --now nomad`, preventing accidental daemon shutdown during testing.

---

## 4. Conclusion

All three validation gate bypass failure modes identified in Round 2 have been thoroughly and definitively remediated. Volume isolation (replicas: 0 during shadow for Batch 2 queue workers and Batch 3 stateful apps, with Nomad stopped before K3s scale 1), CLI surface cleanliness, and standalone migration script functionality are fully verified.

Explicit Review Verdict:
**`APPROVE`**

---

## 5. Verification Method

To independently verify these findings:

1. **Run Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_rejects_node_none_or_null_stringification \
                                  tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_rejects_architecture_mismatch_when_platform_omitted \
                                  tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_detects_digest_drift_when_initial_digest_omitted -v
   ```
   *Expected Output*: Ran 3 tests, `OK`.

2. **Run All Engine and Builder Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected Output*: Ran 145 tests, 0 failures, 0 errors, `OK`.

3. **Run Feature 8 Adversarial Stress Suite**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/stress_test_m4_feature8.py
   ```
   *Expected Output*: Ran 21 tests, 0 failures, 0 errors, `OK`.

4. **Verify Standalone Script and CLI Surface**:
   ```bash
   ./.venv/bin/python -m luma.cli --help | grep -i migrate
   ./.venv/bin/python -m luma.cli migrate --help
   ./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run
   ```
   *Expected Output*: `luma --help` returns no matches for `migrate`, `luma migrate --help` outputs subcommands cleanly, and dry-run exits with code 0.

5. **Invalidation Conditions**:
   - If `migrator.plan()` reports `plan.ready == True` when a volume has `node: None`, `node: "None"`, `node: "null"`, or `node: "~"`.
   - If `migrator.cutover()` succeeds without `LumaError` on an unpinned stateful volume.
   - If `migrator.cutover()` succeeds without `LumaError` when an amd64 image targets an `arm64` node.
   - If `migrator.cutover()` succeeds without `LumaError` when `current_running_images` supplies a modified digest.
