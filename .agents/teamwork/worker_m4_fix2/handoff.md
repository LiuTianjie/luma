# Worker M4-Fix2 Handoff Report: Hardening Pre-Cutover Validation Gates Against Adversarial Bypasses

## 1. Observation

Adversarial stress testing conducted by Challenger M4-R2-2 identified three validation gate bypass vulnerabilities in `luma/paas/migration/migrator.py`:

1. **Failure Mode 1 (Gate 2: Stateful Volume Node Pin Stringification Bypass)**:
   - File: `luma/paas/migration/migrator.py`, line 406 (prior to edit).
   - In `MigrationEngine.plan()`:
     ```python
     node_name = str(vol.get("node") if "node" in vol else (parsed_manifest.get("node") or ""))
     ```
   - When `vol` contained `{"node": None}` (or YAML `node: null`), `str(None)` evaluated to the literal string `"None"`.
   - In `plan()`, `if not node_name:` evaluated to `False` (since `"None"` is non-empty), so no warning was emitted and `plan.ready` was set to `True`.
   - In `cutover()`, `node_pin = str(vol.get("node") or "").strip()`. Because `vol.get("node")` was `"None"`, Gate 2 treated `"None"` as a truthy node pin, bypassing the required explicit node pin validation and allowing unpinned stateful volumes to be scheduled to K3s without hostPath node affinity.

2. **Failure Mode 2 (Gate 3: Architecture Mismatch Bypass When Image Platform Omitted)**:
   - File: `luma/paas/migration/migrator.py`, lines 548, 591, and 634–640 (prior to edit).
   - In `shadow()`, image artifacts were defaulted to `linux/amd64` when `platform` was omitted:
     ```python
     platform=str(img_info.get("platform") or "linux/amd64")
     ```
   - However, `record["runningImages"]` stored the un-normalized input dictionary `{k: dict(v) for k, v in running_images.items()}`, where `platform` remained `None` or omitted.
   - In `cutover()`:
     ```python
     img_platform = str(img.get("platform") or "")
     if img_platform and target_arch not in img_platform:
         raise LumaError(...)
     ```
   - When `running_images` omitted `platform`, `img_platform` evaluated to `""`, causing `if img_platform and ...` to evaluate to `False`. Workloads compiled for `linux/amd64` were permitted to cut over onto native `arm64` nodes (e.g., openSUSE MicroOS ARM64 or Apple Silicon nodes) without error.

3. **Failure Mode 3 (Gate 4: Image Digest Drift Bypass When Initial Digest Omitted)**:
   - File: `luma/paas/migration/migrator.py`, lines 540–543, 591, and 646–653 (prior to edit).
   - In `shadow()`, when `running_images` omitted `digest`, a synthetic `sha256:` digest was generated for `ServiceImageArtifact.digest`. But `record["runningImages"]` preserved the raw dictionary with `digest: ""`.
   - In `cutover()`:
     ```python
     if curr_digest and orig_digest and curr_digest != orig_digest:
         raise LumaError(...)
     ```
   - Because `orig_digest` was `""`, comparing against a new `current_running_images` digest short-circuited to `False`, allowing upstream Nomad container drift to be ignored.

4. **Reproduction Verification**:
   Running the challenger's reproduction script (`reproduce_feature8_bugs.py`) on the unfixed codebase yielded:
   ```
   [BUG 1 REPRODUCED]: cutover() SUCCEEDED without LumaError despite node being None!
   [BUG 2 REPRODUCED]: cutover() SUCCEEDED without LumaError despite amd64 image on arm64 node!
   [BUG 3 REPRODUCED]: cutover() SUCCEEDED without LumaError despite image digest drift!
   ```

---

## 2. Logic Chain

1. **Stateful Volume Pinning Invariant (R3)**:
   - Stateful volumes (`kato`, `bot`, and Compose apps with local PVs) mount hostPath storage directly from the legacy Nomad allocation host. Cutting over to K3s without an explicit `kubernetes.io/hostname` pin causes Pods to mount non-existent host directories on other cluster nodes, causing data corruption or split-brain SQLite writes.
   - Introducing `_normalize_node_pin(value: Any) -> str` explicitly treats `None`, `""`, whitespace, `"none"`, `"null"`, and `"~"` case-insensitively as unpinned (`""`).
   - In `plan()`, both manifest-level and volume-level pins are normalized. If unpinned, a warning is recorded and `plan.ready` is set to `False`.
   - In `shadow()`, `adopted_volumes` records the normalized pin.
   - In `cutover()`, Gate 2 re-normalizes `vol.get("node")`. If unpinned, it raises `LumaError("stateful volume requires an explicit node pin matching legacy Nomad host before cutover")`.

2. **Architecture Affinity Invariant (R2 & R3)**:
   - Luma enforces strict native-only builds and prohibits running cross-compiled or mismatched binaries on `arm64` (e.g. openSUSE MicroOS ARM64) nodes.
   - When legacy Nomad applications are adopted without an explicit platform, the default target architecture is `linux/amd64`.
   - In `shadow()`, `images[svc_name]` normalizes `platform` to `"linux/amd64"` if omitted, None, null, or whitespace. This normalized platform is persisted into `record["runningImages"]`.
   - In `cutover()`, Gate 3 defaults `img_platform` to `"linux/amd64"` if empty/null, and verifies `target_arch not in img_platform`. If target node is `arm64`, it unconditionally raises `LumaError("architecture mismatch between image platform 'linux/amd64' and target node architecture 'arm64'")`.

3. **Image Digest Drift Detection Invariant (R3)**:
   - Upstream Nomad tasks must not drift to unverified digests between shadow deployment and cutover.
   - In `shadow()`, `ServiceImageArtifact.digest` is guaranteed to contain a valid `sha256:` digest (either supplied by user or deterministically synthesized). This resolved digest is stored into `record["runningImages"]`.
   - In `cutover()`, Gate 4 compares `curr_digest` from `current_running_images` against `orig_digest`. If `orig_digest` is missing in record state, it resolves it from the catalog's recorded artifact or deterministic synthetic digest. If `curr_digest` differs from `orig_digest`, it raises `LumaError("image digest drift detected")`.

4. **Unit Test Invariant**:
   - In `tests/test_paas_engine.py`, three new unit tests were added:
     - `test_cutover_rejects_node_none_or_null_stringification`: Parametrically tests `[None, "None", "null", "~", ""]`, asserting that `plan.ready` is `False`, `adopted_volumes[0]["node"] == ""`, and `cutover()` raises `LumaError`.
     - `test_cutover_rejects_architecture_mismatch_when_platform_omitted`: Asserts omitting `platform` defaults to `linux/amd64` and raises `LumaError` with `"architecture mismatch"` when `target_node_arch="arm64"`.
     - `test_cutover_detects_digest_drift_when_initial_digest_omitted`: Asserts omitting `digest` in shadow retains the resolved digest and raises `LumaError` with `"image digest drift detected"` when `current_running_images` provides a shifted digest.

---

## 3. Caveats

- **Scope Discipline**: Modifications were strictly confined to `luma/paas/migration/migrator.py` and `tests/test_paas_engine.py`. No other files were touched.
- **Backward Compatibility**: Valid node pins (e.g. `"prod-cn-1"`, `"worker-1"`) continue to pass without friction. Valid architectures (e.g. `linux/amd64` on `amd64` nodes, or `linux/arm64` on `arm64` nodes) continue to cut over seamlessly.

---

## 4. Conclusion

All three validation bypass failure modes identified in Challenger M4-R2-2 have been thoroughly resolved with genuine, defensive logic:
1. `_normalize_node_pin` handles all falsy and null-like string representations (`None`, `"None"`, `"null"`, `"~"`, whitespace), guaranteeing unpinned volumes cannot bypass Gate 2.
2. `shadow()` normalizes and persists the resolved `platform` and `digest` in `record["runningImages"]`.
3. `cutover()` defensively defaults `img_platform` to `"linux/amd64"`, blocks architecture mismatches against `arm64` nodes, and detects image digest shifts even when the initial shadow run omitted the digest.
4. Three new unit tests in `tests/test_paas_engine.py` provide permanent automated regression protection.

---

## 5. Verification Method

To independently verify the implementation:

1. **Run New Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_rejects_node_none_or_null_stringification \
                                  tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_rejects_architecture_mismatch_when_platform_omitted \
                                  tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_detects_digest_drift_when_initial_digest_omitted -v
   ```
   *Expected Output*: Ran 3 tests in ~0.008s, `OK`.

2. **Run Full PaaS Engine and Native Builder Test Suites**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected Output*: Ran 145 tests, 0 failures, 0 errors, `OK`.

3. **Run Challenger Stress Suite**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/stress_test_m4_feature8.py
   ```
   *Expected Output*: Ran 21 tests, 0 failures, 0 errors, `OK`.

4. **Run Ruff Linting**:
   ```bash
   ./.venv/bin/python -m ruff check luma tests scripts
   ```
   *Expected Output*: `All checks passed!`.

5. **Invalidation Conditions**:
   - If `migrator.plan()` sets `plan.ready = True` when `existing_volumes` contains `{"node": None}` or `{"node": "null"}`.
   - If `migrator.cutover()` succeeds on an adopted stateful volume whose node pin is `None`, `"None"`, `"null"`, `"~"`, or whitespace.
   - If `migrator.cutover()` allows a workload with unspecified image platform to cut over onto an `arm64` target node.
   - If `migrator.cutover()` fails to raise `LumaError` when `current_running_images` supplies a new digest different from the resolved shadow digest.
