# Milestone 3 Handoff Report: Helm Compiler & Workload Placement (Feature 5 & Feature 6)

**Author**: Worker M3 (Implementer, QA, Specialist)  
**Parent**: Project Orchestrator (`1387c386-51c1-4a2c-b02a-a266794155bd`)  
**Workspace**: `/Users/turning4th/infra-stacks`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m3`  
**Modified Files**:
- `luma/paas/release/helm_compiler.py`
- `tests/test_paas_engine.py`

---

## 1. Observation

Direct inspection of `luma/paas/release/helm_compiler.py` and existing tests revealed several architectural gaps and defects:

1. **Missing `kubernetes.io/arch` Node Selector Injection**:
   - `_build_region_node_selector(region, node_pin)` in `luma/paas/release/helm_compiler.py` previously accepted only `region` and `node_pin`, emitting only `luma.io/region`, `topology.kubernetes.io/region`, and `kubernetes.io/hostname`.
   - In `compile_service_release` and `compile_compose_release`, the artifact container platform (`linux/arm64` vs `linux/amd64`) was ignored, omitting `kubernetes.io/arch` from Pod `nodeSelector`.
   - Workload placement failed to enforce that Apple Silicon (`home` region) strictly requires `arm64`, and failed to validate against node architecture mappings (`node_architectures`).

2. **Compose Sidecar Validation & Mixed-Architecture Rejection Gaps**:
   - In `compile_compose_release`, services declaring `network_mode: service:<target>` had no validation if `target` did not exist in compose services (causing silent compilation of orphaned deployments), no validation for self-referential sidecars (`service:web` on `web`), and no cycle detection for chained dependencies (`A -> B -> A`).
   - Containers co-located in the same Pod via sidecar linkage were never checked for architecture parity. An AMD64 container and ARM64 sidecar could be co-located in a single Pod specification, leading to fatal `exec format error` scheduling failures on heterogeneous clusters.
   - Chained sidecars (`A -> B -> C`) only linked immediate children to parents, dropping indirect sidecars from the root Pod.

3. **Compose PVC Volume Mounting Omission in Deployments**:
   - In `compile_compose_release`, while PVC manifests were synthesized into `compiled.pvcs`, `pod_spec["volumes"]` and container `volumeMounts` were populated ONLY with secret mounts (`list(secret_vols)` and `list(secret_mounts)`). User volume declarations (e.g. `volumes: ["app-data:/var/lib/app"]`) were never attached to Pods or containers.
   - Top-level `storage:` definitions in Compose manifests were ignored when parsing volumes.
   - `:ro` access mode flags in volume entries were not parsed and instead corrupted mount paths (e.g. `"/var/media/readonly:ro"`).
   - Unitless storage size quantities (e.g. `size: "50"`) were not validated against Kubernetes quantity conventions.
   - Duplicate container mount paths in the same container were not caught or rejected.

4. **Exposure Mode Gaps & Bugs**:
   - In `compile_service_release`, setting `exposure: none` with an explicit `domain` caused execution to fall into a fallback `else` branch that erroneously coerced exposure to `"cn-edge"`, emitting an active public Ingress resource.
   - Under `exposure: tcp-relay`, Kubernetes Service ports were hardcoded with `"name": "http"` rather than `"tcp"`, causing L7 protocol mismatches in reverse proxies like Traefik.
   - Port collisions across independent Compose services using `tcp-relay` on identical `publishPort` values were not detected.
   - Custom sidecar `command` overrides in Compose were dropped and not passed to child containers.

---

## 2. Logic Chain

1. **Runtime Node Architecture Affinity (Feature 5)**:
   - *Premise*: Heterogeneous Kubernetes clusters contain both ARM64 nodes (such as Apple Silicon nodes in `home` region or openSUSE MicroOS ARM64 nodes) and AMD64 worker nodes. A container compiled for `linux/arm64` crashes immediately with `exec format error` if scheduled on an AMD64 node, and vice-versa.
   - *Implementation*:
     - Updated `_build_region_node_selector(region, node_pin, arch="")` to extract the architecture suffix (`arm64` or `amd64`) and inject `"kubernetes.io/arch": arch` into `nodeSelector`.
     - Created `_extract_kubernetes_arch(platform)` and `_validate_workload_architecture(platform, region=..., node_pin=..., node_architectures=...)` which:
       1. Validates and normalizes platform strings via `validate_native_platform` and infers defaults via `infer_native_platform`.
       2. Rejects any non-ARM64 workload targeting `region="home"`.
       3. Rejects cross-region topology conflicts (such as known `home` nodes `macbook-pro` or `air3` targeted with non-home regions like `cn`).
       4. Checks node-level architecture constraints against `node_architectures` mappings or node name heuristics, raising `LumaError("architecture mismatch")` on conflict.
     - Added `node_architectures: Mapping[str, str] | None = None` to both `compile_service_release` and `compile_compose_release` and enabled both positional and keyword invocation.

2. **Compose Sidecar Integrity & Co-location (Feature 5)**:
   - *Premise*: In Docker Compose and Kubernetes Pod design, containers sharing network namespaces must run on the same physical host CPU.
   - *Implementation*:
     - In `compile_compose_release`, validated that any `target_svc` in `network_mode: service:<target>` exists in `compose_services`, raising `LumaError` with missing parent information if absent.
     - Rejected self-referential sidecars (`target_svc == svc_name`) and circular dependency chains via recursive cycle detection `_find_root_primary(s, visited)`.
     - Resolved root primary containers so all multi-level sidecars (`A -> B -> C`) are properly flattened and co-located in the root primary Pod.
     - Verified that every sidecar's architecture matches the root primary container's architecture; raised `LumaError(f"cannot co-locate mixed-architecture containers in pod sidecar: {child_name} ({child_norm}) vs {root_parent} ({parent_norm})")` on mismatch.

3. **Compose Splitting, Volume Attachment & Exposure Modes (Feature 6)**:
   - *Premise*: Applications with persistent storage require both PVC resource definitions and container/pod volume mounting. Exposure modes must strictly govern Ingress creation and protocol routing.
   - *Implementation*:
     - Created helper functions:
       - `_validate_storage_size(size, vol_name)`: validates resource quantity units (`Gi`, `Mi`, regex `^\d+(\.\d+)?([kKMGTPE]i?|m)$`), rejecting bare integers or unitless numbers.
       - `_parse_volume_entry(entry)`: parses string or dict entries, extracting `(source, target, read_only)` and properly parsing `:ro` and `:rw` mode flags.
       - `_resolve_service_volumes(...)`: distinguishes named volumes from hostPath binds, synthesizes PVCs with retain annotations, registers volumes in `pod_volumes_by_name`, and returns container `volumeMounts` with `:ro` mapped to `readOnly: True`. Validates and rejects duplicate mount paths per container.
     - Corrected `compile_compose_release` to attach resolved volume mounts to main and sidecar containers, and attach synthesized volumes to Pod `spec.template.spec.volumes`.
     - Preserved sidecar custom `command` overrides in `child_container["command"]`.
     - Fixed `exposure: none` suppression bug: `_compile_exposure_ingress` is invoked directly; when `exposure == "none"`, no Ingress manifests are produced even if a domain is specified.
     - Under `exposure: tcp-relay`, set Kubernetes Service port name to `"tcp"` instead of `"http"`.
     - Detected and rejected TCP relay port collisions across Compose services sharing the same `publishPort`.

4. **Test Suite Integration**:
   - Integrated all 25 unit tests from `explorer_m3_3/suite_3_test_code.py` as `RuntimeArchitectureAffinityAndComposeIntegrityTests` in `tests/test_paas_engine.py`.
   - Added module-level `__getattr__` dynamic aliasing to support both `RuntimeArchitectureAffinityAndComposeIntegrityTests` and `RuntimeNodeAffinityAndSidecarIntegrityTests`.
   - Updated existing legacy test assertion in `PaaSEngineTests.test_luma_signature_features_in_service_and_compose_compiler` to expect `"kubernetes.io/arch": "arm64"` in `nodeSelector`.

---

## 3. Caveats

1. **Unmounted Top-Level Storage**:
   - Top-level storage specifications in `luma.yaml` (`storage:` or `volumes:`) that are not explicitly referenced by a container mount entry are still synthesized into `compiled.pvcs` (with default size `"10Gi"` or configured size) to ensure pre-provisioned persistent storage is preserved across releases.
2. **Dynamic Aliasing Scope**:
   - `__getattr__` in `tests/test_paas_engine.py` is configured for test loader compatibility. It allows referencing `RuntimeNodeAffinityAndSidecarIntegrityTests` without causing test runners to discover duplicate test cases.

---

## 4. Conclusion

Milestone 3 (Helm Compiler & Workload Placement) is complete, fully functional, and verified:
- `luma/paas/release/helm_compiler.py`: Fully hardened for Feature 5 (Runtime Architecture Affinity & Sidecar Integrity) and Feature 6 (Compose Splitting, Volume Mounting & 6 Exposure Modes).
- `tests/test_paas_engine.py`: Enhanced with Suite 3 (25 unit tests covering Tier 1, Tier 2, and Tier 3 requirements) and legacy assertions updated.
- All 77 unit tests in `test_paas_engine` and `test_native_builders` pass cleanly in ~0.59s.
- Full repository test suite (1581 tests) passes with 0 failures and 0 errors.
- Ruff linter check passes with 0 violations.

---

## 5. Verification Method

To independently verify this milestone:

1. **Unit Test Execution (PaaS Engine & Native Builders)**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected Result*: Ran 77 tests in ~0.59s -> OK.

2. **Suite 3 Execution (Direct & Alias)**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeArchitectureAffinityAndComposeIntegrityTests -v
   ./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeNodeAffinityAndSidecarIntegrityTests -v
   ```
   *Expected Result*: Ran 25 tests -> OK.

3. **Linter Gate**:
   ```bash
   ./.venv/bin/python -m ruff check luma tests scripts
   ```
   *Expected Result*: "All checks passed!"

4. **Full Repository Test Suite Discovery**:
   ```bash
   ./.venv/bin/python -m unittest discover tests -v
   ```
   *Expected Result*: Ran 1581 tests -> OK.
