# Handoff Report: Gate 2 Node Pin Bypass (Failure Mode 1) Remediation Design

## 1. Observation

### 1.1 Root Cause in `luma/paas/migration/migrator.py`

In `luma/paas/migration/migrator.py`, line 406 inside `MigrationEngine.plan()`:
```python
401:         for vol in raw_vol_list:
402:             if not isinstance(vol, Mapping):
403:                 continue
404:             vol_name = str(vol.get("name") or "data")
405:             host_path = str(vol.get("hostPath") or f"/var/lib/luma/volumes/{project_slug}/{vol_name}")
406:             node_name = str(vol.get("node") if "node" in vol else (parsed_manifest.get("node") or ""))
407:             adopted_vols.append(
408:                 {
409:                     "volumeName": vol_name,
410:                     "hostPath": host_path,
411:                     "node": node_name,
412:                     "fsGroup": int(vol.get("fsGroup") or 1000),
413:                     "reclaimPolicy": "Retain",
414:                 }
415:             )
416:             if not node_name:
417:                 warnings.append(
418:                     f"Volume '{vol_name}' has no explicit node pin; pin a node before cutover to keep local data in-place."
419:                 )
```

And in `MigrationEngine.cutover()`, lines 621–630:
```python
621:         # GATE 2: Verify stateful volume node pin
622:         adopted_vols = record.get("adoptedVolumes") or []
623:         for vol in adopted_vols:
624:             node_pin = str(vol.get("node") or "").strip()
625:             if not node_pin:
626:                 raise LumaError(
627:                     f"cannot cut over {project_slug}: stateful volume '{vol.get('volumeName', 'data')}' "
628:                     f"requires an explicit node pin matching legacy Nomad host before cutover"
629:                 )
```

Additionally, in `MigrationEngine.plan()`, lines 392–399 synthesize `raw_vol_list` from `parsed_manifest` when `existing_volumes` is empty:
```python
392:         if not raw_vol_list and ("volumes" in parsed_manifest or "storage" in parsed_manifest):
393:             for v_entry in parsed_manifest.get("volumes") or []:
394:                 if isinstance(v_entry, str) and ":" in v_entry:
395:                     v_name = v_entry.split(":", 1)[0].strip()
396:                     raw_vol_list.append({"name": v_name, "node": str(parsed_manifest.get("node") or "")})
397:             for s_name in (parsed_manifest.get("storage") or {}).keys():
398:                 if not any(v.get("name") == s_name for v in raw_vol_list if isinstance(v, Mapping)):
399:                     raw_vol_list.append({"name": str(s_name), "node": str(parsed_manifest.get("node") or "")})
```
And in `MigrationEngine.shadow()`, line 525:
```python
524:         if node_memory_headroom_mb:
525:             node_target = str(parsed_manifest.get("node") or "")
526:             if node_target and node_memory_headroom_mb.get(node_target, 1024) < 256:
```

### 1.2 Reproduction Execution & Verbatim Output

Running the standalone reproduction script at `/Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/reproduce_feature8_bugs.py`:
```bash
./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/reproduce_feature8_bugs.py
```
Output:
```
[BUG 1 REPRODUCED]: cutover() SUCCEEDED without LumaError despite node being None!
[BUG 2 REPRODUCED]: cutover() SUCCEEDED without LumaError despite amd64 image on arm64 node!
[BUG 3 REPRODUCED]: cutover() SUCCEEDED without LumaError despite image digest drift!
```

### 1.3 Deficiency in Existing Tests

In `tests/test_paas_engine.py`, lines 2615–2647 (TC-F8-03):
```python
2631: existing_volumes=[{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": ""}],
```
The test only supplied `node: ""` (empty string), where `str("")` happened to stay `""`. It omitted:
- `node: None` (Python `None` or YAML `node: null`, `node: ~`, `node:`)
- `node: "None"` (literal string `"None"`)
- `node: "null"` (literal string `"null"`)
- `node: "   "` (whitespace string)
- YAML manifests with implicit volumes where `node: null` or `node: None`

---

## 2. Logic Chain

1. **Volume Parsing in `plan()`**:
   - When a volume is adopted with `"node": None` (or parsed from YAML `node: null`), `"node" in vol` evaluates to `True`.
   - The expression `(vol.get("node") if "node" in vol else ...)` selects `vol.get("node")`, which is `None`.
   - Evaluating `str(None)` in Python yields the literal string `"None"`.
   - Similarly, if YAML manifest contains `node: None` or `node: "null"`, the parsed value is string `"None"` or `"null"`.

2. **Warning and Readiness Bypass in `plan()`**:
   - At line 416, `if not node_name:` checks whether `node_name` is falsy.
   - Because `"None"` and `"null"` are non-empty strings, `bool("None")` is `True`, so `not node_name` evaluates to `False`.
   - Consequently, no warning is appended to `plan.warnings`.
   - At line 440, `ready = len(warnings) == 0` evaluates to `True`. The unpinned stateful volume is erroneously certified as ready for migration!

3. **Persistence into Shadow State**:
   - At line 489 in `shadow()`, `plan.adopted_volumes` is stored into `state["adoptedVolumes"]` with `"node": "None"`.

4. **Gate 2 Bypass in `cutover()`**:
   - At line 624 in `cutover()`, `node_pin = str(vol.get("node") or "").strip()`.
   - `node_pin` is `"None"`, which is non-empty and truthy.
   - At line 625, `if not node_pin:` evaluates to `False`.
   - Gate 2 does not raise `LumaError`, completely bypassing the mandatory explicit node pin check required by Requirement R3!
   - As observed by Challenger M4-R2-2, the cutover then proceeds to deploy the stateful workload to K3s with no `kubernetes.io/hostname` pin in `nodeSelector`, creating catastrophic data loss / SQLite volume detachment risk on multi-node clusters.

5. **Remediation Invariant**:
   - A centralized normalization helper `_normalize_node_pin(value: Any) -> str` must be introduced:
     - `None` -> `""`
     - `""` and whitespace -> `""`
     - `"none"`, `"None"`, `"NONE"` -> `""`
     - `"null"`, `"Null"`, `"NULL"` -> `""`
     - `"~"` -> `""`
     - Any valid hostname (e.g. `"prod-cn-1"`) -> trimmed string `"prod-cn-1"`
   - Applying `_normalize_node_pin` in `plan()` ensures `plan.adopted_volumes` always stores `""` for unpinned volumes, guarantees `warnings` are emitted, and marks `plan.ready = False`.
   - Applying `_normalize_node_pin` in `cutover()` guarantees that even if legacy or external state contained `"None"` or `"null"`, Gate 2 will evaluate `not node_pin` as `True` and raise `LumaError`.

---

## 3. Caveats

1. **Read-Only Explorer Scope**: In accordance with the Explorer persona rules, no project source code was modified. All findings were verified through non-destructive reproduction scripts and empirical test execution.
2. **Related Failures in Iteration 4 Round 2**: Challenger M4-R2-2 reported three failure modes (Bug 1: Gate 2 Node Pin Bypass; Bug 2: Gate 3 Architecture Mismatch Bypass; Bug 3: Gate 4 Image Digest Drift Bypass). This report specifies the exact design and diffs for **Bug 1 (Gate 2 Node Pin Bypass)**. Worker M4-Fix2 must incorporate all three fixes.
3. **Valid Hostnames**: Node hostnames like `"prod-cn-1"`, `"worker-arm64-1"`, or `"node1.internal"` will be preserved as-is (with whitespace stripped).

---

## 4. Conclusion & Exact Proposed Implementation

### 4.1 Target File: `luma/paas/migration/migrator.py`

#### A. Add Helper Function `_normalize_node_pin` (around line 337, right above `MigrationEngine`):
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

#### B. Update `MigrationEngine.plan()` (lines 391–420):
```diff
--- a/luma/paas/migration/migrator.py
+++ b/luma/paas/migration/migrator.py
@@ -391,19 +399,20 @@ class MigrationEngine:
         raw_vol_list = list(existing_volumes)
+        manifest_node = _normalize_node_pin(parsed_manifest.get("node"))
         if not raw_vol_list and ("volumes" in parsed_manifest or "storage" in parsed_manifest):
             for v_entry in parsed_manifest.get("volumes") or []:
                 if isinstance(v_entry, str) and ":" in v_entry:
                     v_name = v_entry.split(":", 1)[0].strip()
-                    raw_vol_list.append({"name": v_name, "node": str(parsed_manifest.get("node") or "")})
+                    raw_vol_list.append({"name": v_name, "node": manifest_node})
             for s_name in (parsed_manifest.get("storage") or {}).keys():
                 if not any(v.get("name") == s_name for v in raw_vol_list if isinstance(v, Mapping)):
-                    raw_vol_list.append({"name": str(s_name), "node": str(parsed_manifest.get("node") or "")})
+                    raw_vol_list.append({"name": str(s_name), "node": manifest_node})
 
         for vol in raw_vol_list:
             if not isinstance(vol, Mapping):
                 continue
             vol_name = str(vol.get("name") or "data")
             host_path = str(vol.get("hostPath") or f"/var/lib/luma/volumes/{project_slug}/{vol_name}")
-            node_name = str(vol.get("node") if "node" in vol else (parsed_manifest.get("node") or ""))
+            raw_node = vol.get("node") if "node" in vol else parsed_manifest.get("node")
+            node_name = _normalize_node_pin(raw_node)
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
```

#### C. Update `MigrationEngine.shadow()` (line 525):
```diff
--- a/luma/paas/migration/migrator.py
+++ b/luma/paas/migration/migrator.py
@@ -524,3 +533,3 @@ class MigrationEngine:
         if node_memory_headroom_mb:
-            node_target = str(parsed_manifest.get("node") or "")
+            node_target = _normalize_node_pin(parsed_manifest.get("node"))
             if node_target and node_memory_headroom_mb.get(node_target, 1024) < 256:
```

#### D. Update `MigrationEngine.cutover()` (lines 623–630):
```diff
--- a/luma/paas/migration/migrator.py
+++ b/luma/paas/migration/migrator.py
@@ -623,7 +632,7 @@ class MigrationEngine:
         adopted_vols = record.get("adoptedVolumes") or []
         for vol in adopted_vols:
-            node_pin = str(vol.get("node") or "").strip()
+            node_pin = _normalize_node_pin(vol.get("node"))
             if not node_pin:
                 raise LumaError(
                     f"cannot cut over {project_slug}: stateful volume '{vol.get('volumeName', 'data')}' "
                     f"requires an explicit node pin matching legacy Nomad host before cutover"
                 )
```

---

### 4.2 Target File: `tests/test_paas_engine.py`

Expand TC-F8-03 or add `test_batch3_stateful_volume_unpinned_node_variants_block_cutover` in `Feature 8` test suite (around line 2647):

```python
    def test_batch3_stateful_volume_unpinned_node_variants_block_cutover(self) -> None:
        """TC-F8-03-EXT: Unpinned variants (None, 'None', 'null', '', whitespace) block cutover."""
        manifest_template = """
name: kato-unpinned-{idx}
region: cn
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        unpinned_variants = [None, "", "   ", "None", "none", "null", "NULL", "~"]
        for idx, variant in enumerate(unpinned_variants):
            slug = f"kato-unpinned-{idx}"
            manifest = manifest_template.format(idx=idx)
            existing_vols = [{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": variant}]
            
            # Step 1: plan() must mark ready=False, adopted volume node="", and emit warning
            plan = self.migrator.plan(project=slug, manifest_text=manifest, existing_volumes=existing_vols)
            self.assertEqual(plan.adopted_volumes[0]["node"], "", f"Variant {variant!r} did not normalize to empty string")
            self.assertFalse(plan.ready, f"Variant {variant!r} incorrectly marked plan as ready")
            self.assertTrue(any("node pin" in w.lower() for w in plan.warnings))

            # Step 2: shadow() deploys with replica: 0
            self.migrator.shadow(
                project=slug,
                manifest_text=manifest,
                running_images={slug: {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
                existing_volumes=existing_vols,
            )

            # Step 3: cutover() must raise LumaError with 'requires an explicit node pin'
            with self.assertRaises(LumaError) as ctx:
                self.migrator.cutover(slug)
            self.assertIn("requires an explicit node pin", str(ctx.exception))
```

And test implicit manifest-level `node: null` and `node: None`:
```python
    def test_batch3_manifest_node_null_blocks_cutover(self) -> None:
        """Manifests with node: null or node: None without explicit volume node pin block cutover."""
        for raw_node_val in ["null", "None", "~", ""]:
            manifest = f"name: kato-yaml-{raw_node_val}\nregion: cn\nnode: {raw_node_val}\nvolumes:\n  - data:/var/lib/kato\n"
            slug = f"kato-yaml-{raw_node_val}"
            plan = self.migrator.plan(project=slug, manifest_text=manifest)
            self.assertEqual(plan.adopted_volumes[0]["node"], "")
            self.assertFalse(plan.ready)
            self.assertTrue(any("node pin" in w.lower() for w in plan.warnings))
```

---

## 5. Verification Method

To independently verify the remediation once applied by Worker M4-Fix2:

1. **Run Challenger Bug Reproduction Script**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/24c268c2-c94b-4369-81aa-70d3b6c8cf31/scratch/reproduce_feature8_bugs.py
   ```
   *Expected Output for Bug 1 after fix*:
   ```
   [BUG 1 FIXED]: cutover() correctly raised LumaError: cannot cut over kato: stateful volume 'data' requires an explicit node pin matching legacy Nomad host before cutover
   ```

2. **Run PaaS Engine Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
   Must pass with 0 failures and 0 errors.

3. **Run Ruff Linting**:
   ```bash
   ./.venv/bin/ruff check luma/paas/migration/migrator.py
   ```
   Must return "All checks passed!".

4. **Run Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   Must exit with code 0.

5. **Invalidation Conditions**:
   - If `plan.adopted_volumes[0]["node"]` returns `"None"` or `"null"` when `vol["node"]` is `None` or `"None"`.
   - If `plan.ready` is `True` when any stateful volume lacks an explicit node pin.
   - If `migrator.cutover()` succeeds without raising `LumaError` on any volume where `node` is `None`, `""`, `"None"`, `"null"`, or whitespace.
