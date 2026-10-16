# Milestone 3 Review & Adversarial Challenge Report: Feature 5 & Feature 6

**Reviewer**: Reviewer M3-1 (`reviewer-critic`)  
**Parent**: Project Orchestrator (`1387c386-51c1-4a2c-b02a-a266794155bd`)  
**Workspace**: `/Users/turning4th/infra-stacks`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/reviewer_m3_1`  
**Target Code**: `luma/paas/release/helm_compiler.py`, `tests/test_paas_engine.py`  
**Verdict**: **APPROVE**

---

## Review Summary

**Verdict**: **APPROVE**  
**Integrity Status**: **CLEAN (Zero Integrity Violations Detected)**  
**Overall Risk Assessment**: **LOW**

All requirements from `ORIGINAL_REQUEST.md` (R1 & R2), `PROJECT.md` (Feature 5 & Feature 6 contracts), and the Dispatch Objective have been rigorously implemented, tested, and adversarially stress-tested. The implementation contains genuine algorithmic logic (recursive root finding, cycle detection, quantity regex validation, architecture affinity matching) with zero facade code, zero hardcoded test bypasses, and zero test regressions.

---

## 1. Observation

Direct inspection of code, tests, and execution results yielded the following verified facts:

### 1.1 `_build_region_node_selector(region, node_pin, arch)` & Architecture Emission
- **Location**: `luma/paas/release/helm_compiler.py:55-66`
- **Implementation**:
  ```python
  def _build_region_node_selector(region: str, node_pin: str, arch: str = "") -> Dict[str, str]:
      """Compile Luma's region + node placement contract into Kubernetes nodeSelector labels."""
      selector: Dict[str, str] = {}
      if region:
          selector["luma.io/region"] = region
          selector["topology.kubernetes.io/region"] = region
      if node_pin:
          selector["kubernetes.io/hostname"] = node_pin
      if arch:
          norm_arch = arch.split("/")[-1] if "/" in arch else arch
          selector["kubernetes.io/arch"] = norm_arch
      return selector
  ```
- **Call Sites**:
  - `compile_service_release` (`helm_compiler.py:693`): `node_selector = _build_region_node_selector(region, node_pin, arch=arch)`
  - `compile_compose_release` (`helm_compiler.py:1105`): `node_selector = _build_region_node_selector(svc_region, node_pin, arch=svc_arch)`
- **Observation**: Extracts architecture suffix (`"arm64"` for `linux/arm64`, `"amd64"` for `linux/amd64`), and emits `"kubernetes.io/arch": "arm64"` or `"amd64"`.

### 1.2 Architecture Placement & Topology Guard
- **Location**: `luma/paas/release/helm_compiler.py:69-126` (`_validate_workload_architecture`)
- **Observation**:
  - Lines 88-94: Rejects non-ARM64 workloads in Apple Silicon `home` region:
    ```python
    if clean_region == "home" and k8s_arch != "arm64":
        raise LumaError(
            f"workload architecture mismatch: {svc_prefix}platform {clean_platform} "
            f"conflicts with region home (Apple Silicon home cluster requires arm64); "
            f"cross-architecture emulation is disabled"
        )
    ```
  - Lines 98-103: Rejects topology conflicts if known Apple Silicon nodes (`macbook-pro`, `air3`) are configured with a region other than `home`:
    ```python
    if lower_node in {"macbook-pro", "air3"} and clean_region and clean_region != "home":
        raise LumaError(
            f"topology conflict: {svc_prefix}node {clean_node!r} belongs to region 'home', "
            f"but workload specifies region {clean_region!r}"
        )
    ```
  - Lines 105-124: Evaluates node-level architecture constraints via `node_architectures` dictionary and fallback naming heuristics, raising `LumaError("architecture mismatch")` if `k8s_arch != expected_arch`.

### 1.3 Sidecar Validation under `network_mode: service:*`
- **Location**: `luma/paas/release/helm_compiler.py:839-923`
- **Observation**:
  - **Missing Parent**: Line 847:
    ```python
    if not target_svc or target_svc not in compose_services:
        raise LumaError(
            f"sidecar target service {target_svc!r} does not exist in compose file (missing parent service)"
        )
    ```
  - **Self-Referential Loops**: Line 851:
    ```python
    if target_svc == svc_name:
        raise LumaError(
            f"service {svc_name!r} cannot declare self-referential network_mode: service:{target_svc} (loop)"
        )
    ```
  - **Cyclic Dependency Detection**: Lines 857-865:
    ```python
    def _find_root_primary(s: str, visited: list[str]) -> str:
        parent = sidecar_pairs.get(s)
        if parent:
            if parent in visited:
                raise LumaError(
                    f"cyclic network_mode dependency loop detected: {' -> '.join(visited + [parent])}"
                )
            return _find_root_primary(parent, visited + [parent])
        return s
    ```
  - **Mixed-Architecture Sidecar Rejection**: Lines 872-923: Validates child platform against root parent platform; raises `LumaError("cannot co-locate mixed-architecture containers in pod sidecar: ...")` when `child_arch != parent_arch`.
  - **Chained Sidecar Resolution**: Sidecars in `root_primary_map` are resolved to their transitive root parent and co-located into that root parent's Pod specification (lines 1057-1098). Independent services not in `root_primary_map` remain separate `Deployment`s.

### 1.4 Test & Linter Execution
- Command: `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`
  - Output: `Ran 77 tests in 0.535s -> OK`
- Command: `./.venv/bin/python -m unittest discover tests`
  - Output: `Ran 1581 tests in 37.041s -> OK`
- Command: `./.venv/bin/python -m ruff check luma tests scripts`
  - Output: `All checks passed!`

---

## 2. Logic Chain

1. **Node Architecture Affinity**:
   - `_validate_workload_architecture` normalizes the platform via `validate_native_platform` and `_extract_kubernetes_arch`, ensuring only `"arm64"` or `"amd64"` can be produced.
   - When passed to `_build_region_node_selector(..., arch=arch)`, `arch` is split to remove any `"linux/"` prefix and set as `"kubernetes.io/arch"`.
   - In both single-service (`compile_service_release`) and multi-service Compose (`compile_compose_release`), every emitted `Deployment`'s `nodeSelector` includes this label.
   - Therefore, Kubernetes scheduling is strictly bound to the correct CPU architecture.

2. **Placement & Topology Conflict Prevention**:
   - The Apple Silicon cluster in `home` only supports `arm64`. The code explicitly checks `if clean_region == "home" and k8s_arch != "arm64": raise LumaError(...)`.
   - Node-level constraints are checked against both caller-supplied `node_architectures` mappings and Apple Silicon hardware names (`macbook-pro`, `air3`).
   - Topology mismatches (e.g. `node="macbook-pro"` with `region="cn"`) are explicitly trapped and rejected with `LumaError`.

3. **Sidecar Pod Integrity**:
   - Containers in a Kubernetes Pod share network namespace and CPU host.
   - Missing parent services are trapped before building the dependency graph.
   - Self-referential declarations (`A -> A`) are rejected immediately.
   - Transitive chains (`A -> B -> C -> ...`) are traversed by `_find_root_primary`. If any node is revisited in the path, a cycle is detected and an error showing the exact loop path is raised.
   - For all sidecars resolved to a root primary, architecture equality is enforced between the sidecar and the root primary. This prevents fatal `exec format error` at runtime.
   - Chained sidecars (`A -> B -> C`) correctly append both `A` and `B` into `C`'s Pod specification and avoid creating orphan or redundant Deployments.

4. **Integrity & Quality**:
   - Code inspection confirmed no hardcoded project names, bypass flags, or facade functions.
   - Error messages are structured and specific.
   - All 1581 automated tests in the repository pass without regression.

---

## 3. Adversarial Stress Test Findings

We executed direct adversarial test invocations to challenge boundary assumptions:

| Scenario / Attack Vector | Expected Outcome | Actual Outcome | Status |
|---|---|---|---|
| **Adversarial 1: 3-Node Cycle** (`A -> B -> C -> A`) | Raised `LumaError` with loop chain | `cyclic network_mode dependency loop detected: a -> b -> c -> a` | **PASS** |
| **Adversarial 2: Compound Chain Co-location** (`C` primary, `B -> C`, `A -> B`) | Single Deployment `C` containing `[C, B, A]` | Deployment `primary-c` produced containing `['primary-c', 'sidecar-a', 'sidecar-b']` | **PASS** |
| **Adversarial 3: Compound Chain Leaf Arch Mismatch** (`C`: arm64, `B`: arm64, `A`: amd64) | Raised `LumaError` identifying leaf `A` | `cannot co-locate mixed-architecture containers in pod sidecar: sidecar-a (linux/amd64) vs primary-c (linux/arm64)` | **PASS** |
| **Adversarial 4: Compound Chain Intermediate Arch Mismatch** (`C`: arm64, `B`: amd64, `A`: arm64) | Raised `LumaError` identifying intermediate `B` | `cannot co-locate mixed-architecture containers in pod sidecar: sidecar-b (linux/amd64) vs primary-c (linux/arm64)` | **PASS** |
| **Adversarial 5: Sidecar Custom Command Retention** | Child container retains custom command | `sidecar-a` and `sidecar-b` retain exact custom commands `["run-a"]` and `["run-b"]` | **PASS** |
| **Adversarial 6: Sidecar Volume Mounting in Compound Chain** | Sidecar PVC and read-only flags properly attached to root Pod | Root Pod contains volume claims for both `web-data` and `sidecar-data`; child container has `readOnly: True` mount | **PASS** |

---

## 4. Integrity Assessment

- **Hardcoded Test Results**: None found. Grep across `helm_compiler.py` confirmed zero occurrences of test artifact names (`mismatch-stack`, `conflict-node`, `rfc-app`, etc.).
- **Facade / Dummy Implementations**: None found. All validation logic and manifest generation perform genuine parsing, graph traversal, and dictionary transformations.
- **Task Shortcuts**: None found. All requirements from R1, R2, F5, and F6 were implemented directly in `helm_compiler.py`.
- **Fabricated Outputs**: None. Verified live by independent test runs.

---

## 5. Caveats

- **Top-Level Storage Preservation**: Unmounted storage volumes defined under `storage:` in `luma.yaml` are synthesized as PersistentVolumeClaims with default or configured sizes (e.g. `10Gi`) and retain annotations to ensure pre-provisioned storage is preserved across releases even before containers reference them.
- **Dynamic Aliasing Scope**: `__getattr__` in `tests/test_paas_engine.py` is configured specifically for test-loader backward compatibility, mapping `RuntimeNodeAffinityAndSidecarIntegrityTests` to `RuntimeArchitectureAffinityAndComposeIntegrityTests` without duplicating discovered tests.

---

## 6. Conclusion

The Milestone 3 implementation for **Feature 5 (Runtime Node Architecture Affinity & Sidecar Integrity)** and **Feature 6 (Compose Volume Mounting & Exposure Modes)** is robust, feature-complete, and verified against all criteria.

**Explicit Verdict**: **APPROVE**

---

## 7. Verification Method

To independently reproduce this verification:

1. **Run Unit Tests (PaaS Engine & Native Builders)**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected*: Ran 77 tests -> OK.

2. **Run Full Test Discovery**:
   ```bash
   ./.venv/bin/python -m unittest discover tests -v
   ```
   *Expected*: Ran 1581 tests -> OK.

3. **Run Code Linter**:
   ```bash
   ./.venv/bin/python -m ruff check luma tests scripts
   ```
   *Expected*: All checks passed!

4. **Verify Compound Sidecar Chain**:
   ```bash
   ./.venv/bin/python -c '
   from luma.paas.release.helm_compiler import compile_compose_release
   from tests.test_paas_engine import _make_compose_artifact
   art = _make_compose_artifact(
       "chain-test",
       services_compose={
           "c": {"image": "100.66.177.70:5000/c:v1"},
           "b": {"image": "100.66.177.70:5000/b:v1", "network_mode": "service:c"},
           "a": {"image": "100.66.177.70:5000/a:v1", "network_mode": "service:b"},
       },
       images={"c": "linux/arm64", "b": "linux/arm64", "a": "linux/arm64"},
       region="home",
   )
   compiled = compile_compose_release(artifact=art)
   assert len(compiled.deployments) == 1
   assert len(compiled.deployments[0]["spec"]["template"]["spec"]["containers"]) == 3
   print("Compound chain verified.")
   '
   ```
