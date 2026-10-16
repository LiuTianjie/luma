# Explorer M4-Fix-3 Handoff Report: Test Updates Design for `tests/test_paas_engine.py`

## Executive Summary
This report provides the exact, executable test suite enhancements for `tests/test_paas_engine.py` to resolve all findings raised by Reviewer M4-1:
1. Explicit assertions in `test_node_self_heal_daemonset_yaml_manifest_verification` for `nodeSelector: kubernetes.io/os: linux` and `host-var` mount (`mountPath: /host/var`).
2. New unit test method `test_opensuse_microos_corrupted_registries_yaml_recovery` (TC-B-F7-03) verifying malformed YAML syntax detection in `registriesYamlContent` and auto-recovery.
3. New unit test assertions verifying `NodeSelfHealReport` mapping access (`report["actions"]`), membership checking (`"actions" in report`, `"repairedActions" in report`), and `dict(report)` dictionary conversion.
4. New unit test method `test_darwin_no_proxy_ip_substring_does_not_satisfy_exact_match` proving that `"127.0.0.100"` does NOT satisfy `"127.0.0.1"` through un-tokenized substring matching.

---

## 1. Observation

### Exact File Paths & Lines Inspected
- `tests/test_paas_engine.py`:
  - Lines 2104–2123: `test_opensuse_microos_self_heal_transactional_rootfs` tests self-healing attributes, but lacks mapping access tests (`report["actions"]`).
  - Lines 2145–2161: `test_opensuse_microos_selinux_restorecon` tests SELinux restorecon on MicroOS, but no test exercises corrupted `registriesYamlContent` (TC-B-F7-03).
  - Lines 2163–2211: Darwin proxy tests (`test_darwin_self_heal_no_proxy_local`, `test_no_proxy_multiple_separators_and_duplicate_entries`) test basic NO_PROXY alignment, but do not test the IP substring collision edge case (`127.0.0.100` vs `127.0.0.1`).
  - Lines 2229–2269: `test_node_self_heal_daemonset_yaml_manifest_verification` verifies `hostPID`, `hostNetwork`, `tolerations`, `privileged`, `host-etc`, and `host-run`, but omits `nodeSelector: kubernetes.io/os: linux` and `host-var` (`mountPath: /host/var`).
- `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`:
  - Lines 18–19: `nodeSelector: kubernetes.io/os: linux` is present in the manifest.
  - Lines 125–126: `volumeMounts`: `- name: host-var\n  mountPath: /host/var` is present.
  - Lines 133–135: `volumes`: `- name: host-var\n  hostPath:\n    path: /var` is present.
- `luma/paas/migration/migrator.py`:
  - Lines 108–138: `NodeSelfHealReport.__getitem__` only maps `to_dict()` keys and `snake_map` keys without `"actions"`. `__contains__` and `__iter__` are not defined.
  - Line 211: `missing_no_proxy = [h for h in req_hosts if not (h in no_proxy_raw or h in existing_tokens)]` performs un-tokenized substring matching against `no_proxy_raw`.
  - Lines 216–227: `evaluate_node_self_heal` parses `registriesYamlContent` with `yaml.safe_load(raw_yaml)` and sets `corrupted_registries = True` if malformed or missing `mirrors`/`configs`.

### Empirical Bug Reproduction Commands & Verbatim Outputs
1. **NodeSelfHealReport mapping access & membership failure**:
   ```bash
   ./.venv/bin/python -c '
   from luma.paas.migration.migrator import evaluate_node_self_heal
   r = evaluate_node_self_heal({"osName": "opensuse-microos", "diskUsedPercent": 80.0}, auto_fix=True)
   r["actions"]
   '
   # Verbatim output: KeyError: 'actions'
   
   ./.venv/bin/python -c '
   from luma.paas.migration.migrator import evaluate_node_self_heal
   r = evaluate_node_self_heal({"osName": "opensuse-microos", "diskUsedPercent": 80.0}, auto_fix=True)
   "actions" in r
   '
   # Verbatim output: KeyError: 0
   
   ./.venv/bin/python -c '
   from luma.paas.migration.migrator import evaluate_node_self_heal
   r = evaluate_node_self_heal({"osName": "opensuse-microos", "diskUsedPercent": 80.0}, auto_fix=True)
   dict(r)
   '
   # Verbatim output: KeyError: 0
   ```
2. **Substring false-positive match (`127.0.0.100` satisfying `127.0.0.1`)**:
   ```bash
   ./.venv/bin/python -c '
   from luma.paas.migration.migrator import evaluate_node_self_heal
   r = evaluate_node_self_heal({"osName": "darwin", "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000"}, auto_fix=False)
   print("no_proxy_aligned:", r.no_proxy_aligned)
   '
   # Verbatim output: no_proxy_aligned: True   <-- BUG: 127.0.0.1 is missing but falsely marked aligned!
   ```

---

## 2. Logic Chain

1. *Observation*: `test_node_self_heal_daemonset_yaml_manifest_verification` only asserts `host-etc` and `host-run`.
   *Inference*: Because the production DaemonSet in `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` defines `nodeSelector: kubernetes.io/os: linux` and `host-var` (`mountPath: /host/var`), adding assertions for `pod_spec.get("nodeSelector", {}).get("kubernetes.io/os") == "linux"` and `mounts["host-var"] == "/host/var"` guarantees manifest integrity against regressions without modifying the YAML.
2. *Observation*: `evaluate_node_self_heal` contains full support for detecting invalid `registriesYamlContent` (both unparseable syntax and semantic missing keys) and auto-recovering with canonical config and SELinux `restorecon`.
   *Inference*: Adding test method `test_opensuse_microos_corrupted_registries_yaml_recovery` (TC-B-F7-03) with unparseable YAML (`:::malformed yaml::: {bad syntax`) and semantically invalid YAML validates that `unfixed.registries_yaml_converged is False`, `unfixed.healthy is False`, and `fixed.registries_yaml_converged is True` with `"syntax failure"` and `"restorecon"` recorded in `repaired_actions`.
3. *Observation*: Calling `report["actions"]` raises `KeyError: 'actions'`, calling `"actions" in report` raises `KeyError: 0` because Python sequence indexing fallback starts at index `0`, and `dict(report)` raises `KeyError: 0`.
   *Inference*: Adding `test_node_self_heal_report_mapping_access_and_membership` directly asserts `report["actions"]`, `"actions" in report`, `"repairedActions" in report`, `report.get("actions")`, and `dict(report)`. This ensures that `NodeSelfHealReport` behaves as a full mapping compliant with the evaluation checklist.
4. *Observation*: Checking `h in no_proxy_raw` allows `127.0.0.1` to match as a substring of `127.0.0.100`.
   *Inference*: Adding `test_darwin_no_proxy_ip_substring_does_not_satisfy_exact_match` with `noProxy: "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000"` ensures that under `auto_fix=False`, `no_proxy_aligned` is `False`. Under `auto_fix=True`, `127.0.0.1` is appended while preserving `127.0.0.100`.

---

## 3. Caveats

- **Read-Only Investigation**: As an explorer, no production or test files in the repository were altered. All proposed code is validated through isolated execution in Python.
- **Worker Execution Sequence**:
  - The worker should apply the implementation fixes in `luma/paas/migration/migrator.py` (adding `"actions"` mapping, `__contains__`, `__iter__`, `keys()`, `values()`, `items()` to `NodeSelfHealReport`, and changing `h not in existing_tokens` in `evaluate_node_self_heal`) either before or concurrently with updating `tests/test_paas_engine.py` so tests pass immediately.

---

## 4. Conclusion & Complete Executable Test Code

### Proposed Updates to `tests/test_paas_engine.py`

#### Update 1: Assertions in `test_node_self_heal_daemonset_yaml_manifest_verification`
**Target File**: `tests/test_paas_engine.py`  
**Location**: In class `MigrationReliabilityAndNodeSelfHealingTests`, method `test_node_self_heal_daemonset_yaml_manifest_verification` (lines 2229–2269).

```python
    def test_node_self_heal_daemonset_yaml_manifest_verification(self) -> None:
        """Manifest verification of deploy/paas-stack/k8s/node-self-heal-daemonset.yaml."""
        manifest_path = Path("deploy/paas-stack/k8s/node-self-heal-daemonset.yaml")
        self.assertTrue(manifest_path.exists(), f"Manifest file missing: {manifest_path}")

        manifest_docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
        ds = next(d for d in manifest_docs if d and d.get("kind") == "DaemonSet")

        self.assertEqual(ds["apiVersion"], "apps/v1")
        self.assertEqual(ds["metadata"]["name"], "luma-node-self-heal")
        self.assertEqual(ds["metadata"]["namespace"], "luma-system")

        pod_spec = ds["spec"]["template"]["spec"]
        self.assertEqual(
            pod_spec.get("nodeSelector", {}).get("kubernetes.io/os"),
            "linux",
            "DaemonSet must target linux nodes via nodeSelector kubernetes.io/os: linux",
        )
        self.assertTrue(pod_spec.get("hostPID"), "DaemonSet must run with hostPID: true")
        self.assertTrue(pod_spec.get("hostNetwork"), "DaemonSet must run with hostNetwork: true")

        self.assertTrue(
            any(t.get("operator") == "Exists" for t in pod_spec.get("tolerations", [])),
            "DaemonSet must tolerate all taints to run on MicroOS and builder nodes",
        )

        containers = pod_spec.get("containers", [])
        self.assertGreaterEqual(len(containers), 1)
        c = containers[0]
        self.assertTrue(c.get("securityContext", {}).get("privileged"), "Container must be privileged")

        mounts = {m["name"]: m["mountPath"] for m in c.get("volumeMounts", [])}
        self.assertIn("host-etc", mounts)
        self.assertEqual(mounts["host-etc"], "/host/etc")
        self.assertIn("host-var", mounts)
        self.assertEqual(mounts["host-var"], "/host/var")
        self.assertIn("host-run", mounts)
        self.assertEqual(mounts["host-run"], "/host/var/run")

        volumes = {v["name"]: v.get("hostPath", {}).get("path") for v in pod_spec.get("volumes", [])}
        self.assertIn("host-var", volumes)
        self.assertEqual(volumes["host-var"], "/var")

        env_map = {e["name"]: e.get("value", "") for e in c.get("env", [])}
        self.assertIn("LUMA_ZOT_REGISTRY_HOST", env_map)
        self.assertEqual(env_map["LUMA_ZOT_REGISTRY_HOST"], "100.66.177.70:5000")
        self.assertIn("LUMA_INTERNAL_GIT_HOSTS", env_map)

        cmd = " ".join(c.get("command", []))
        self.assertIn("reconcile_registries", cmd)
        self.assertTrue("fstrim" in cmd or "btrfs" in cmd)
```

---

#### Update 2: New Test Method `test_opensuse_microos_corrupted_registries_yaml_recovery` (TC-B-F7-03)
**Target File**: `tests/test_paas_engine.py`  
**Location**: In class `MigrationReliabilityAndNodeSelfHealingTests`, insert immediately after `test_opensuse_microos_selinux_restorecon` (around line 2161).

```python
    def test_opensuse_microos_corrupted_registries_yaml_recovery(self) -> None:
        """TC-B-F7-03: Malformed YAML in registriesYamlContent detected and auto-recovered on MicroOS."""
        node_syntax_error = {
            "name": "microos-worker-arm64",
            "osName": "opensuse-microos",
            "registriesYamlConverged": True,  # corrupted content must override initial True flag
            "registriesYamlContent": ":::malformed yaml::: {bad syntax [unbalanced",
            "noProxy": "localhost,127.0.0.1",
            "zotDirectReachable": True,
            "diskUsedPercent": 40.0,
        }
        unfixed = evaluate_node_self_heal(node_syntax_error, auto_fix=False)
        self.assertFalse(unfixed.healthy, "Node with malformed registries.yaml must evaluate as unhealthy")
        self.assertFalse(
            unfixed.registries_yaml_converged,
            "registries_yaml_converged must be False when registries.yaml has syntax errors",
        )
        self.assertEqual(len(unfixed.repaired_actions), 0)

        fixed = evaluate_node_self_heal(node_syntax_error, auto_fix=True)
        self.assertTrue(fixed.healthy, "Auto-fix must restore health after corrupted registries.yaml")
        self.assertTrue(
            fixed.registries_yaml_converged,
            "Auto-fix must converge registries_yaml_converged to True",
        )
        actions = " ".join(fixed.repaired_actions).lower()
        self.assertIn("syntax failure", actions)
        self.assertIn("restorecon", actions)
        self.assertIn("registries.yaml", actions)

        # Semantic corruption test: Valid YAML syntax but missing mirrors/configs keys
        node_semantic_error = {
            "name": "microos-worker-arm64",
            "osName": "opensuse-microos",
            "registriesYamlConverged": True,
            "registriesYamlContent": "unrelated_root:\n  nested_field: 12345\n",
            "noProxy": "localhost,127.0.0.1",
            "zotDirectReachable": True,
            "diskUsedPercent": 40.0,
        }
        unfixed_sem = evaluate_node_self_heal(node_semantic_error, auto_fix=False)
        self.assertFalse(unfixed_sem.healthy)
        self.assertFalse(unfixed_sem.registries_yaml_converged)

        fixed_sem = evaluate_node_self_heal(node_semantic_error, auto_fix=True)
        self.assertTrue(fixed_sem.healthy)
        self.assertTrue(fixed_sem.registries_yaml_converged)
        actions_sem = " ".join(fixed_sem.repaired_actions).lower()
        self.assertIn("syntax failure", actions_sem)
        self.assertIn("restorecon", actions_sem)
```

---

#### Update 3: New Test Method `test_node_self_heal_report_mapping_access_and_membership` (TC-B-F7-06)
**Target File**: `tests/test_paas_engine.py`  
**Location**: In class `MigrationReliabilityAndNodeSelfHealingTests`, insert immediately after `test_opensuse_microos_corrupted_registries_yaml_recovery`.

```python
    def test_node_self_heal_report_mapping_access_and_membership(self) -> None:
        """Verifies NodeSelfHealReport mapping protocol (getitem, in, dict, get) for actions and keys."""
        report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "diskUsedPercent": 84.0,
            },
            auto_fix=True,
        )
        # 1. Membership test with 'in' operator (must not trigger sequence index KeyError: 0)
        self.assertIn("actions", report)
        self.assertIn("repairedActions", report)
        self.assertIn("repaired_actions", report)
        self.assertIn("healthy", report)
        self.assertIn("nodeName", report)
        self.assertIn("node_name", report)
        self.assertIn("disk_used_percent", report)
        self.assertNotIn("unknown_key", report)

        # 2. Key mapping access via report[...]
        actions = report["actions"]
        self.assertIsInstance(actions, (list, tuple))
        self.assertTrue(len(actions) > 0)
        self.assertEqual(list(actions), list(report["repairedActions"]))
        self.assertEqual(list(actions), list(report["repaired_actions"]))

        # 3. get() method support
        self.assertEqual(list(report.get("actions", [])), list(actions))
        self.assertEqual(report.get("nonexistent", "fallback"), "fallback")

        # 4. KeyError on missing key
        with self.assertRaises(KeyError):
            _ = report["nonexistent_property"]

        # 5. dict(report) conversion support
        report_dict = dict(report)
        self.assertIsInstance(report_dict, dict)
        self.assertIn("repairedActions", report_dict)
        self.assertIn("nodeName", report_dict)
        self.assertEqual(report_dict["nodeName"], "microos-node-1")
        self.assertEqual(report_dict["repairedActions"], list(report.repaired_actions))
        self.assertTrue(report_dict["healthy"])
```

In addition, update `test_opensuse_microos_self_heal_transactional_rootfs` (lines 2104–2123) by adding mapping assertions:
```python
        # Mapping access verification
        self.assertIn("actions", report)
        self.assertIn("repairedActions", report)
        self.assertEqual(list(report["actions"]), list(report.repaired_actions))
        self.assertTrue(dict(report)["healthy"])
```

---

#### Update 4: New Test Method `test_darwin_no_proxy_ip_substring_does_not_satisfy_exact_match` (TC-B-F7-07)
**Target File**: `tests/test_paas_engine.py`  
**Location**: In class `MigrationReliabilityAndNodeSelfHealingTests`, insert immediately after `test_no_proxy_multiple_separators_and_duplicate_entries` (around line 2211).

```python
    def test_darwin_no_proxy_ip_substring_does_not_satisfy_exact_match(self) -> None:
        """Verifies un-tokenized substring matching is rejected: 127.0.0.100 does not satisfy 127.0.0.1."""
        node = {
            "name": "macbook-m1",
            "osName": "darwin",
            "registriesYamlConverged": True,
            "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": True,
            "diskUsedPercent": 50.0,
        }

        unfixed = evaluate_node_self_heal(node, auto_fix=False)
        self.assertFalse(
            unfixed.no_proxy_aligned,
            "127.0.0.100 must NOT satisfy required 127.0.0.1 via substring matching",
        )
        self.assertFalse(unfixed.healthy)

        fixed = evaluate_node_self_heal(node, auto_fix=True)
        self.assertTrue(fixed.no_proxy_aligned)
        self.assertTrue(fixed.healthy)

        tokens = [t.strip() for t in fixed.no_proxy_canonical.split(",") if t.strip()]
        self.assertIn("127.0.0.1", tokens, "127.0.0.1 must be appended when missing")
        self.assertIn("127.0.0.100", tokens, "Original token 127.0.0.100 must be preserved")
        self.assertIn("<local>", tokens)
        self.assertIn("localhost", tokens)
```

---

### Implementation Context for Worker (`luma/paas/migration/migrator.py`)
For reference, the worker's changes in `luma/paas/migration/migrator.py` to make these tests pass:

1. In `NodeSelfHealReport`:
```python
    def __getitem__(self, key: str) -> Any:
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
        if not isinstance(key, str):
            return False
        return key in self.to_dict() or key in {
            "node_name", "os_name", "arch", "role",
            "registries_yaml_converged", "no_proxy_aligned", "no_proxy_canonical",
            "zot_direct_reachable", "buildkit_cache_bytes", "buildkit_cache_limit_bytes",
            "last_fstrim_at", "disk_used_percent", "read_only_rootfs", "healthy",
            "repaired_actions", "actions",
        }

    def __iter__(self):
        return iter(self.to_dict())

    def keys(self):
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def items(self):
        return self.to_dict().items()
```

2. In `evaluate_node_self_heal` (line 211):
Replace:
```python
    missing_no_proxy = [h for h in req_hosts if not (h in no_proxy_raw or h in existing_tokens)]
```
With:
```python
    missing_no_proxy = [h for h in req_hosts if h not in existing_tokens]
```

---

## 5. Verification Method

To verify these tests after the worker applies them:

1. **Run Suite 4 unit tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   ```
   *Expected Result*: All 31 tests pass with returncode 0 (was 28 tests).
2. **Run full PaaS engine test suite**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
   *Expected Result*: All tests pass with 0 failures, 0 errors.
3. **Run repository gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected Result*: Lint, CLI reference check, typecheck, dashboard build, and test suites all pass with exit code 0.
