# Explorer M4-Fix2-3 Handoff Report: Unit Tests for Pre-Cutover Validation Gate Bypasses

## 1. Observation

Adversarial testing by Challenger M4-R2-2 identified three bypass vulnerabilities in `luma/paas/migration/migrator.py` affecting the pre-cutover validation gates in `MigrationEngine`.

Direct code observations from `luma/paas/migration/migrator.py` and `tests/test_paas_engine.py`:

1. **Bypass Vector 1 (Gate 2: Stateful Volume Node Pin Stringification)**:
   - File: `luma/paas/migration/migrator.py`, line 406:
     ```python
     node_name = str(vol.get("node") if "node" in vol else (parsed_manifest.get("node") or ""))
     ```
   - When an adopted volume dictionary has `"node": None` (or `"None"`, `"null"`), `"node" in vol` is `True`.
   - `str(None)` evaluates to the literal string `"None"`.
   - In line 416, `if not node_name:` evaluates to `False`, so no warning is appended to `plan.warnings`, and `plan.ready` is set to `True`.
   - In `cutover()` line 624:
     ```python
     node_pin = str(vol.get("node") or "").strip()
     if not node_pin:
         raise LumaError(...)
     ```
   - Because `node_pin == "None"`, Gate 2 evaluates it as truthy and allows cutover on unpinned hostPath volumes.
   - Current execution output on unfixed code:
     ```
     Test 1 current plan.ready: True adopted node: None
     cutover() SUCCEEDED without LumaError despite node being None!
     ```

2. **Bypass Vector 2 (Gate 3: Architecture Mismatch When Image Platform Omitted)**:
   - File: `luma/paas/migration/migrator.py`, lines 548, 591, 634–640:
     In `shadow()`, line 548 sets the artifact platform:
     ```python
     platform=str(img_info.get("platform") or "linux/amd64")
     ```
     However, line 591 stores the raw un-normalized dictionary in migration state:
     ```python
     "runningImages": {k: dict(v) for k, v in running_images.items()}
     ```
     When `running_images={"web": {"image": "serverx:v1"}}` omits the `platform` key, `img.get("platform")` in `record["runningImages"]` is `None`.
     In `cutover()`, lines 635–637:
     ```python
     img_platform = str(img.get("platform") or "")
     if img_platform and target_arch not in img_platform:
         raise LumaError(...)
     ```
     Because `img_platform == ""`, the condition evaluates to `False`, allowing an image defaulting to `linux/amd64` to cut over onto an `arm64` node.
   - Current execution output on unfixed code:
     ```
     cutover() SUCCEEDED without LumaError despite amd64 image on arm64 node!
     ```

3. **Bypass Vector 3 (Gate 4: Image Digest Drift When Initial Digest Omitted)**:
   - File: `luma/paas/migration/migrator.py`, lines 540–543, 591, 646–653:
     In `shadow()`, a synthetic SHA256 digest is generated for `ServiceImageArtifact.digest` when omitted:
     ```python
     digest = str(img_info.get("digest") or "")
     if not digest.startswith("sha256:"):
         digest = "sha256:" + hashlib.sha256(f"{project_slug}:{svc_name}:{repo}".encode()).hexdigest()
     ```
     However, `record["runningImages"]` stores `img_info` with `digest=""` (or omitted).
     In `cutover()`, line 649:
     ```python
     if curr_digest and orig_digest and curr_digest != orig_digest:
         raise LumaError(...)
     ```
     Because `orig_digest == ""`, drift detection against `current_running_images` (e.g. `digest="sha256:drifted1234567890"`) evaluates to `False`, bypassing Gate 4.
   - Current execution output on unfixed code:
     ```
     cutover() SUCCEEDED without LumaError despite image digest drift!
     ```

---

## 2. Logic Chain

1. **Gate 2 Node Pin Guard Invariant**:
   - Any stateful volume adopted with `node: None`, `node: "None"`, `node: "null"`, or `node: ""` must be recognized as unpinned (`node = ""`).
   - `plan()` must record a warning and evaluate `plan.ready = False`.
   - `cutover()` must raise `LumaError` containing `"stateful volume"` and `"node pin"`.
   - Test method `test_cutover_rejects_node_none_or_null_stringification` parameterizes over `[None, "None", "null", ""]` using `self.subTest`, ensuring complete regression protection.

2. **Gate 3 Architecture Parity Invariant**:
   - Workloads adopted from Nomad often omit the explicit `platform` key in `running_images`.
   - The compiled release artifact defaults this platform to `linux/amd64`.
   - If cutover is attempted targeting an `arm64` node (`target_node_arch="arm64"`), Gate 3 must detect that `linux/amd64` does not contain `arm64` and raise `LumaError("architecture mismatch")`.
   - Test method `test_cutover_rejects_architecture_mismatch_when_platform_omitted` validates this behavior by omitting the `platform` key and passing `target_node_arch="arm64"`.

3. **Gate 4 Digest Drift Invariant**:
   - When `running_images` omits `digest`, the migration engine resolves a synthetic/artifact digest during `shadow()`.
   - During `cutover()`, if `current_running_images` provides an updated digest (e.g. upstream build or drift in Nomad), `cutover()` must compare against the resolved digest and raise `LumaError("image digest drift detected")`.
   - Test method `test_cutover_detects_digest_drift_when_initial_digest_omitted` validates that omitting the initial digest during shadow does not disable drift detection during cutover.

4. **Test Suite Integration**:
   - In `tests/test_paas_engine.py`, lines 2760–2795 already contain `test_pre_cutover_architecture_mismatch_blocks_cutover` and `test_pre_cutover_image_digest_drift_detection` under class `MigrationReliabilityAndNodeSelfHealingTests`.
   - The three new test methods directly follow line 2795 under section `# Feature 8: Pre-Cutover Safety Gates`.
   - When run against the current codebase, all 3 tests fail with `AssertionError`.
   - When run against the patched `MigrationEngine`, all 3 tests pass in 0.005 seconds with zero ruff lint errors.

---

## 3. Caveats

- **Read-Only Constraint**: In accordance with the Explorer identity rules, no files outside `.agents/teamwork/explorer_m4_fix2_3/` were modified.
- **Worker Coordination**: Explorer M4-Fix2-1 designed the Gate 2 fix in `migrator.py`, and Explorer M4-Fix2-2 designed the Gate 3 and Gate 4 fixes in `migrator.py`. Worker M4-Fix-2 will apply both the `migrator.py` fixes and add these three test methods to `tests/test_paas_engine.py`.

---

## 4. Conclusion & Ready-to-Apply Test Code

Here are the three complete, tested, and lint-clean Python test methods ready for Worker M4-Fix-2 to add into `tests/test_paas_engine.py` inside `MigrationReliabilityAndNodeSelfHealingTests` (immediately following line 2795):

```python
    def test_cutover_rejects_node_none_or_null_stringification(self) -> None:
        """TC-B-F8-06: Verifies node values of None, 'None', 'null', or empty string in existing_volumes
        are not stringified to truthy strings, marking plan.ready=False and blocking cutover with LumaError.
        """
        manifest_yaml = """
name: kato
region: cn
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        for bad_node in [None, "None", "null", ""]:
            with self.subTest(bad_node=bad_node):
                existing_vols = [{"name": "data", "path": "/data", "node": bad_node}]
                plan = self.migrator.plan(
                    project="kato",
                    manifest_text=manifest_yaml,
                    existing_volumes=existing_vols,
                )
                self.assertFalse(plan.ready, f"plan.ready must be False when node={bad_node!r}")
                self.assertTrue(
                    any("node pin" in w.lower() for w in plan.warnings),
                    f"plan.warnings must warn about missing node pin for node={bad_node!r}",
                )
                self.assertEqual(
                    plan.adopted_volumes[0]["node"],
                    "",
                    f"plan.adopted_volumes[0]['node'] must be empty string, not {bad_node!r}",
                )

                self.migrator.shadow(
                    project="kato",
                    manifest_text=manifest_yaml,
                    running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
                    existing_volumes=existing_vols,
                )

                with self.assertRaises(LumaError) as ctx:
                    self.migrator.cutover("kato")
                err_msg = str(ctx.exception).lower()
                self.assertIn("node pin", err_msg)
                self.assertIn("stateful volume", err_msg)

    def test_cutover_rejects_architecture_mismatch_when_platform_omitted(self) -> None:
        """TC-B-F8-07: Verifies that omitting the platform key in running_images defaults to linux/amd64
        and cutover() properly detects the architecture mismatch when target_node_arch is arm64.
        """
        manifest_yaml = "name: serverx\nregion: cn\nport: 8080\n"
        shadow = self.migrator.shadow(
            project="serverx",
            manifest_text=manifest_yaml,
            running_images={"web": {"image": "serverx:v1"}},
            target_node_arch="arm64",
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["targetNodeArch"], "arm64")

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("serverx")
        err_msg = str(ctx.exception).lower()
        self.assertIn("architecture mismatch", err_msg)
        self.assertIn("arm64", err_msg)

    def test_cutover_detects_digest_drift_when_initial_digest_omitted(self) -> None:
        """TC-B-F8-08: Verifies that omitting the digest key during shadow() retains the resolved/synthetic
        artifact digest, allowing cutover() to detect drift against a new current_running_images digest.
        """
        manifest_yaml = "name: serverx-drift\nregion: cn\nport: 8080\n"
        shadow = self.migrator.shadow(
            project="serverx-drift",
            manifest_text=manifest_yaml,
            running_images={"web": {"image": "serverx:v1"}},
        )
        self.assertEqual(shadow["state"], "shadow_ready")

        drifted_digest = "sha256:drifted1234567890"
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover(
                "serverx-drift",
                current_running_images={"web": {"image": "serverx:v1", "digest": drifted_digest}},
            )
        err_msg = str(ctx.exception).lower()
        self.assertIn("image digest drift detected", err_msg)
```

---

## 5. Verification Method

To verify these tests:

1. **Pre-Fix Failure Reproduction** (confirms tests fail on current unfixed code):
   ```bash
   ./.venv/bin/python -c '
   import unittest
   from tests.test_paas_engine import MigrationReliabilityAndNodeSelfHealingTests
   # After adding tests to tests/test_paas_engine.py
   suite = unittest.TestSuite()
   suite.addTest(MigrationReliabilityAndNodeSelfHealingTests("test_cutover_rejects_node_none_or_null_stringification"))
   suite.addTest(MigrationReliabilityAndNodeSelfHealingTests("test_cutover_rejects_architecture_mismatch_when_platform_omitted"))
   suite.addTest(MigrationReliabilityAndNodeSelfHealingTests("test_cutover_detects_digest_drift_when_initial_digest_omitted"))
   res = unittest.TextTestRunner(verbosity=2).run(suite)
   assert len(res.failures) == 3
   '
   ```

2. **Post-Fix Verification** (after Worker M4-Fix-2 applies remediations in `migrator.py`):
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_rejects_node_none_or_null_stringification -v
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_rejects_architecture_mismatch_when_platform_omitted -v
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests.test_cutover_detects_digest_drift_when_initial_digest_omitted -v
   ```
   *Expected output*: All 3 tests pass (`ok`).

3. **Full Suite 4 Execution**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   ```
   *Expected output*: Ran 34 tests (existing 31 + 3 new), exit code 0.

4. **Lint and Style Verification**:
   ```bash
   ./.venv/bin/ruff check tests/test_paas_engine.py
   ```
   *Expected output*: `All checks passed!`

5. **Invalidation Conditions**:
   - If `test_cutover_rejects_node_none_or_null_stringification` allows cutover when `node: None`.
   - If `test_cutover_rejects_architecture_mismatch_when_platform_omitted` succeeds on cutover when image platform is omitted and node is `arm64`.
   - If `test_cutover_detects_digest_drift_when_initial_digest_omitted` fails to detect drift when `running_images` omitted `digest`.
