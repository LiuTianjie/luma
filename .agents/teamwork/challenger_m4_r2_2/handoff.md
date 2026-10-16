# Challenger M4-R2-2 Handoff Report: Adversarial Stress Testing of Feature 8 (Nomad to K3s Zero-Downtime Migration Engine)

## Explicit Verdict: `REQUEST_CHANGES`

---

## 1. Observation

Adversarial stress-testing of Feature 8 was conducted against the implementation in `luma/paas/migration/migrator.py`, `luma/cli/parser.py`, and `scripts/migrate-nomad-to-k3s.py` using `./.venv/bin/python`. 

### 1.1 Direct Observations & Test Executions

1. **Vector 1: SQLite / flock Volume Isolation (Single-Instance Workloads `kato`, `bot`)**:
   - Manifest compilation for `kato` and `bot` (and any workload with local PV volumes) sets `shadow_replicas = 0` in `shadow()`.
   - Inspection of compiled K3s deployment manifests in `reconciler.deploy_artifact()` confirmed `spec.replicas == 0`.
   - Multi-service applications with volumes (`stateful-compose`) set all service deployments to `spec.replicas == 0` during shadow.
   - Result: **PASS** across all tests in `test_v1_*`.

2. **Vector 2: Two-Phase Cutover Execution Order**:
   - Instrumented spy on `FluxReconciler.deploy_artifact` inside `cutover()`:
     At the exact moment `deploy_artifact` is called:
     - For stateful workloads (`kato`, `bot`), `record["legacyNomadAllocationStopped"]` is `True` and `record["nomadStoppedAt"] <= time.time()`.
     - For Batch 2 queue workers (`serverx`), `record["legacyNomadWorkerStatus"]` is `"drained"`.
   - Following cutover, K3s Deployments are scaled to target replicas (`1` for single-instance, `4` and `2` for workers).
   - Result: **PASS** across all tests in `test_v2_*`.

3. **Vector 4: Memory Headroom Boundary**:
   - Executing `migrator.shadow(..., available_memory_mb=511)` raised `LumaError("insufficient node memory headroom for shadow release: 511 MB available (< 512 MB required)")`.
   - Executing `migrator.shadow(..., available_memory_mb=512)` succeeded without error.
   - Values `0`, `256`, `510`, `-1` all raised `LumaError`; values `513`, `1024`, `4096` all succeeded.
   - Result: **PASS** across all tests in `test_v4_*`.

4. **Vector 5: CLI Parser & Visibility**:
   - Programmatic inspection of `COMMAND_GROUPS` and `SUMMARIES` confirmed `migrate` is absent from all public groups.
   - `python -m luma.cli --help` output does not contain `migrate` in the command list.
   - `python -m luma.cli migrate --help` succeeded with exit code 0 and displayed subcommands: `batches`, `plan`, `shadow`, `cutover`, `abort`, `finalize`.
   - Result: **PASS** across all tests in `test_v5_*`.

5. **Vector 3: Pre-Cutover Validation Gates — Three Critical Failure Modes Observed**:

   - **Failure Mode 1 (CRITICAL — Gate 2 Node Pin Bypass)**:
     - File: `luma/paas/migration/migrator.py`, line 406:
       ```python
       node_name = str(vol.get("node") if "node" in vol else (parsed_manifest.get("node") or ""))
       ```
     - When an adopted volume dictionary has `"node": None` (or parsed from YAML `node: null` / `node:`), `"node" in vol` is `True`.
     - Evaluating `str(vol.get("node"))` results in `str(None) == "None"`.
     - Consequently, `node_name` becomes the literal string `"None"`.
     - In line 416, `if not node_name:` evaluates to `False`, so no warning is emitted in `plan()`, and `plan.ready` is set to `True`.
     - In `cutover()` lines 624–629:
       ```python
       adopted_vols = record.get("adoptedVolumes") or []
       for vol in adopted_vols:
           node_pin = str(vol.get("node") or "").strip()
           if not node_pin:
               raise LumaError(...)
       ```
       `vol.get("node")` is `"None"`. `node_pin = "None"`, which is truthy.
     - **Verbatim execution output**:
       ```
       CUTOVER SUCCEEDED DESPITE node: None!
       nodeSelector: {'luma.io/region': 'cn', 'topology.kubernetes.io/region': 'cn', 'kubernetes.io/arch': 'amd64'}
       ```
       Cutover proceeds to scale K3s to active traffic without any node pin on the hostPath volume, directly violating Requirement R3!

   - **Failure Mode 2 (HIGH — Gate 3 Architecture Mismatch Bypass)**:
     - File: `luma/paas/migration/migrator.py`, lines 548, 591, and 634–640:
       In `shadow()`, line 548 sets the artifact platform:
       ```python
       platform=str(img_info.get("platform") or "linux/amd64")
       ```
       However, line 591 records the raw input dictionary in migration state:
       ```python
       "runningImages": {k: dict(v) for k, v in running_images.items()}
       ```
       When `running_images` omits `"platform"`, `img.get("platform")` in `record["runningImages"]` is `None`.
       In `cutover()`, lines 635–637:
       ```python
       img_platform = str(img.get("platform") or "")
       if img_platform and target_arch not in img_platform:
           raise LumaError(...)
       ```
       Because `img_platform` evaluates to `""`, the condition `if img_platform and ...` evaluates to `False`. Gate 3 is completely bypassed!
     - **Verbatim execution output**:
       ```
       CUTOVER ALLOWED when platform not specified, even though target_node_arch is arm64!
       ```
       A workload defaulted to `linux/amd64` was allowed to cut over onto an `arm64` node without error.

   - **Failure Mode 3 (MEDIUM — Gate 4 Drift Detection Bypass)**:
     - File: `luma/paas/migration/migrator.py`, lines 540–543, 591, and 646–653:
       In `shadow()`, if `running_images` does not supply an explicit `sha256:` digest, a synthetic digest is computed for `BuildArtifact.images`. But `record["runningImages"]` retains the un-synthesized input (`digest=""`).
       In `cutover()` line 649:
       ```python
       if curr_digest and orig_digest and curr_digest != orig_digest:
           raise LumaError(...)
       ```
       `orig_digest` is `""`. Therefore, comparing against a newly supplied `current_running_images` (`curr_digest = "sha256:222..."`) evaluates to `False`, bypassing Gate 4 drift detection.
     - **Verbatim execution output**:
       ```
       DRIFT DETECTION BYPASSED when original running_images had no digest!
       ```

---

## 2. Logic Chain

1. **Stateful Volume Pinning Invariant**:
   - *Requirement*: Requirement R3 requires: "Enforce strict pre-shadow and pre-cutover validation that stateful volumes have an explicit `node` pin matching the legacy Nomad allocation host... before `cutover` is permitted."
   - *Observation*: `plan()` converts `vol["node"] = None` into string `"None"`. Gate 2 in `cutover()` checks `node_pin = str(vol.get("node") or "").strip()`.
   - *Deduction*: Because `"None"` is non-empty, Gate 2 treats `"None"` as a valid node hostname. As a result, workloads with unpinned volumes (`node: None` or `node: null`) bypass Gate 2 and cut over to K3s without a `kubernetes.io/hostname` pin. Any Kubernetes node could be scheduled, leading to unattached hostPath directories or SQLite data loss.

2. **Architecture Affinity Invariant**:
   - *Requirement*: Requirement R2 and F8 survey require pre-cutover rejection if the running image platform mismatches the target node architecture.
   - *Observation*: `shadow()` defaults unspecified image platform to `"linux/amd64"` in the compiled artifact, but preserves `None` in `record["runningImages"]`. Gate 3 checks `if img_platform and target_arch not in img_platform:`.
   - *Deduction*: When `running_images` omits `"platform"` (a common case during legacy adoption), `img_platform` evaluates to `""`, causing the guard to be bypassed. A container compiled for `linux/amd64` is allowed to cut over onto an `arm64` node (e.g. openSUSE MicroOS ARM64 or Darwin Apple Silicon).

3. **Drift Detection Invariant**:
   - *Requirement*: Gate 4 is intended to detect when an upstream Nomad image has drifted between `shadow` and `cutover`.
   - *Observation*: `shadow()` synthesizes a digest for `BuildArtifact` when none is provided, but leaves `digest=""` in `record["runningImages"]`.
   - *Deduction*: When `current_running_images` with a real digest is passed to `cutover()`, `orig_digest` is empty, causing drift detection to be silently ignored.

---

## 3. Caveats

- **Scope Discipline**: Under the empirical challenger identity rules, no implementation code was modified in `luma/` or `deploy/`. All failure modes were reproduced using non-destructive empirical test scripts in `/Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/`.
- **Existing Tests**: The existing 31 tests in `tests/test_paas_engine.py` (Suite 4) pass because they only test empty string `node=""` rather than `node=None`, and they supply explicit `platform` and `digest` values in `running_images`.

---

## 4. Conclusion

While Vectors 1, 2, 4, and 5 fully pass their empirical stress tests, **Vector 3 (Pre-Cutover Validation Gates) contains three authentic bypass vulnerabilities in `luma/paas/migration/migrator.py`**:
1. `node: None` is stringified to `"None"`, bypassing Gate 2 stateful volume node pin validation.
2. Missing `platform` in `running_images` bypasses Gate 3 architecture mismatch validation.
3. Missing `digest` in `running_images` bypasses Gate 4 image drift detection.

Therefore, the explicit verdict for Milestone 4 Feature 8 is:
**`REQUEST_CHANGES`**

### Required Remediations for Worker M4-Fix-2:
1. In `luma/paas/migration/migrator.py`, line 406:
   Safely extract node name without stringifying `None` or `"None"`:
   ```python
   raw_node = vol.get("node") if "node" in vol else parsed_manifest.get("node")
   node_name = str(raw_node or "").strip()
   if node_name.lower() in {"none", "null"}:
       node_name = ""
   ```
   And in `cutover()` line 624:
   ```python
   node_pin = str(vol.get("node") or "").strip()
   if not node_pin or node_pin.lower() in {"none", "null"}:
       raise LumaError(
           f"cannot cut over {project_slug}: stateful volume '{vol.get('volumeName', 'data')}' "
           f"requires an explicit node pin matching legacy Nomad host before cutover"
       )
   ```
2. In `luma/paas/migration/migrator.py`, line 591 (`shadow()`):
   Store resolved image artifacts in `record["runningImages"]` (or normalize `platform` to `"linux/amd64"` and `digest` to the resolved artifact digest), so Gate 3 and Gate 4 evaluate against the actual resolved properties:
   ```python
   "runningImages": {
       k: {
           "repository": img.repository,
           "digest": img.digest,
           "tag": img.tag,
           "platform": img.platform,
       }
       for k, img in images.items()
   },
   ```
3. Add unit tests for `node=None`, `platform=None` (with target `arm64`), and synthetic digest drift in `tests/test_paas_engine.py`.

---

## 5. Verification Method

To independently verify these findings and reproduce the failures:

1. **Run Standalone Reproduction Script**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/reproduce_feature8_bugs.py
   ```
   *Expected Current Output*:
   ```
   [BUG 1 REPRODUCED]: cutover() SUCCEEDED without LumaError despite node being None!
   [BUG 2 REPRODUCED]: cutover() SUCCEEDED without LumaError despite amd64 image on arm64 node!
   [BUG 3 REPRODUCED]: cutover() SUCCEEDED without LumaError despite image digest drift!
   ```

2. **Run Full Feature 8 Stress Test Suite**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/stress_test_m4_feature8.py
   ```

3. **Invalidation Conditions**:
   - If `migrator.cutover("kato")` raises `LumaError` with `"requires an explicit node pin"` when `existing_volumes=[{"name": "data", "node": None}]`.
   - If `migrator.cutover("arch-test")` raises `LumaError` with `"architecture mismatch"` when `running_images` omits `platform` and `target_node_arch="arm64"`.
   - If `migrator.cutover("drift-test")` raises `LumaError` with `"image digest drift"` when `running_images` omits `digest` and `current_running_images` supplies a new digest.
