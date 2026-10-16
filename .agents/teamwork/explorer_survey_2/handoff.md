# Survey Report: Requirement 2 (R2) — Native ARM64 Build & Runtime Enforcement (openSUSE MicroOS ARM64 + macOS Apple Silicon)

## 1. Observation

Direct examination of the relevant modules revealed eight critical disconnects, missing guards, and gaps across `luma/build_capabilities.py`, `luma/paas/build/controller.py`, `luma/paas/release/helm_compiler.py`, `luma/paas/migration/migrator.py`, and `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`:

### Observation 1: `BuildController` does not validate multi-platform requests or automatically infer ARM64
- **Path**: `/Users/turning4th/infra-stacks/luma/paas/build/controller.py`
- **Lines**: 162–198 (`select_native_builder`), 199–246 (`submit_build`)
- **Code Quote**:
  ```python
  169:         norm_platform = str(platform or "linux/amd64").strip().lower()
  170:         arm_platform = native_arm_build_platform([norm_platform])
  171:         target_platform = arm_platform or norm_platform
  ...
  206:         platform: str = "linux/amd64",
  ...
  215:         builder = self.select_native_builder(platform, preferred_node=preferred_builder)
  ```
- **Finding**:
  1. If `platform` is omitted, `submit_build` defaults to `"linux/amd64"`, even if the target is an ARM64 node (e.g. `node="macbook-pro"` or openSUSE MicroOS ARM64) or `region="home"`. It selects an AMD64 builder (`buildkit-amd64-0`) and produces an incompatible image.
  2. `submit_build` does not take `target_node` or `target_region` parameters, nor does it inspect `build_spec` for node or region placement.
  3. If a request provides a multi-platform list like `platform="linux/amd64,linux/arm64"`, `native_arm_build_platform` extracts `"linux/arm64"` and silently ignores the multi-platform request instead of rejecting QEMU/cross-compilation fallback.

### Observation 2: `luma/build_capabilities.py` lacks strict native-only validation
- **Path**: `/Users/turning4th/infra-stacks/luma/build_capabilities.py`
- **Lines**: 15–37 (`native_arm_build_platform`)
- **Code Quote**:
  ```python
  15: def native_arm_build_platform(platforms: Iterable[str]) -> str:
  ...
  34:     if len(required) > 1:
  35:         raise LumaError("builds targeting different ARM architectures must be submitted separately; ARM emulation is disabled")
  36:     return next(iter(required), "")
  ```
- **Finding**:
  `native_arm_build_platform` only flags multiple distinct *ARM* variants (e.g. `arm/v7` vs `arm64`). It does not reject mixed AMD64/ARM64 strings (`["linux/amd64", "linux/arm64"]`), nor does the module provide helper functions for single-architecture native platform validation (`validate_native_platform`) or topology-based inference (`infer_native_platform`).

### Observation 3: `compile_service_release` and `compile_compose_release` omit `kubernetes.io/arch` in `nodeSelector`
- **Path**: `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py`
- **Lines**: 41–49 (`_build_region_node_selector`), 422–425 (`compile_service_release`), 684–687 (`compile_compose_release`)
- **Code Quote**:
  ```python
  41: def _build_region_node_selector(region: str, node_pin: str) -> Dict[str, str]:
  42:     """Compile Luma's region + node placement contract into Kubernetes nodeSelector labels."""
  43:     selector: Dict[str, str] = {}
  44:     if region:
  45:         selector["luma.io/region"] = region
  46:         selector["topology.kubernetes.io/region"] = region
  47:     if node_pin:
  48:         selector["kubernetes.io/hostname"] = node_pin
  49:     return selector
  ```
- **Finding**:
  `_build_region_node_selector` only emits `luma.io/region`, `topology.kubernetes.io/region`, and `kubernetes.io/hostname`. It completely omits `kubernetes.io/arch`. Consequently, Kubernetes Pods compiled from `linux/arm64` or `linux/amd64` images lack runtime architecture affinity labels, permitting the Kubernetes scheduler to place an ARM64 binary on an AMD64 node (or vice-versa), resulting in `CrashLoopBackOff` (`exec format error`). In `tests/test_paas_engine.py:170–176`, the unit test tests `nodeSelector` without `kubernetes.io/arch`.

### Observation 4: `compile_compose_release` allows mixed-architecture Pod sidecars (`network_mode: service:*`)
- **Path**: `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py`
- **Lines**: 658–676 (`sidecar_pairs` loop in `compile_compose_release`)
- **Code Quote**:
  ```python
  658:         for child_name, parent_name in sorted(sidecar_pairs.items()):
  659:             if parent_name != svc_name:
  660:                 continue
  661:             child_body = compose_services[child_name]
  662:             child_img = (
  663:                 artifact.images[child_name].immutable_ref
  664:                 if child_name in artifact.images
  665:                 else str(child_body.get("image") or "")
  666:             )
  667:             child_container: Dict[str, Any] = {
  ...
  675:             containers.append(child_container)
  ```
- **Finding**:
  Containers sharing `network_mode: service:<primary>` are co-located in the same Pod spec. However, `compile_compose_release` performs zero validation that the child sidecar container's image architecture matches the primary service container's architecture. If the primary service is `linux/amd64` and the sidecar is `linux/arm64`, both are placed into the same Pod spec without error, causing fatal runtime crashes.

### Observation 5: Missing target node architecture conflict validation in compilation
- **Path**: `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py`
- **Lines**: 363 (`node_pin = str(parsed.get("node") or "").strip()`), 678 (`node_pin = str(luma_svc.get("node") or root_node).strip()`)
- **Finding**:
  Neither `compile_service_release` nor `compile_compose_release` validates whether `node_pin` or `region` conflicts with `image_art.platform`. Pinning `node: macbook-pro` (an Apple Silicon ARM64 node) or `region: home` with an `amd64` image compiles without error, producing an unschedulable or crashing Pod.

### Observation 6: `evaluate_node_self_heal` fails to handle openSUSE MicroOS invariants
- **Path**: `/Users/turning4th/infra-stacks/luma/paas/migration/migrator.py`
- **Lines**: 78–109 (`NodeSelfHealReport`), 124–185 (`evaluate_node_self_heal`)
- **Code Quote**:
  ```python
  132:     os_name = str(node.get("osName") or node.get("agentOs") or "linux").lower()
  ...
  159:         if disk_pct >= 75.0 or (os_name == "linux" and last_fstrim == 0):
  160:             last_fstrim = int(time.time())
  161:             disk_pct = min(disk_pct, 68.0)
  162:             repaired.append("executed containerd image GC and fstrim -v / for PVE thin pool")
  ```
- **Finding**:
  1. Line 159 checks `os_name == "linux"`. If `os_name == "opensuse-microos"`, `last_fstrim == 0` is bypassed completely.
  2. `evaluate_node_self_heal` does not verify openSUSE MicroOS specific requirements:
     - Transactional/read-only rootfs with designated writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`).
     - SELinux enforcing security contexts (`restorecon` for `/etc/rancher/k3s/registries.yaml`).
     - Btrfs filesystem maintenance (`btrfs scrub` and discard).
  3. `NodeSelfHealReport` lacks fields for `transactional_rootfs`, `writable_paths_converged`, `selinux_contexts_converged`, and `btrfs_scrub_at`.

### Observation 7: `node-self-heal-daemonset.yaml` lacks MicroOS writable mounts, SELinux restorecon, and btrfs maintenance
- **Path**: `/Users/turning4th/infra-stacks/deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`
- **Lines**: 40–75 (reconciliation script), 83–95 (volume mounts)
- **Finding**:
  1. Only `/etc` and `/var/run` host paths are mounted; `/var/lib/rancher` and `/var/lib/buildkit` are not mounted, preventing subvolume convergence verification on MicroOS.
  2. `reconcile_registries()` moves `/host/etc/rancher/k3s/registries.yaml` without restoring SELinux contexts via `restorecon`, risking access denial on MicroOS with SELinux Enforcing.
  3. `run_fstrim_and_status()` runs only `fstrim -v /`, failing to inspect btrfs scrub status on MicroOS.

### Observation 8: `DEFAULT_NO_PROXY` in `helm_compiler.py` lacks `<local>` alignment
- **Path**: `/Users/turning4th/infra-stacks/luma/paas/release/helm_compiler.py`
- **Lines**: 16–19 (`DEFAULT_NO_PROXY`)
- **Finding**:
  `DEFAULT_NO_PROXY` includes IP CIDRs and Gitea/Zot hosts, but omits `<local>`. macOS Darwin network stacks use `<local>` in `no_proxy` to bypass proxies for local `.local` domain and mDNS communication.

---

## 2. Logic Chain

1. **Zero-QEMU Native Building Guarantee**:
   - Building ARM64 images through QEMU CPU emulation on x86_64 builders introduces severe build time degradation (up to 44 minutes observed on complex packages like ffmpeg/v8) and intermittent JIT crashes.
   - To ensure production readiness, Luma provides dedicated native ARM64 endpoints: openSUSE MicroOS K3s ARM64 nodes and macOS Apple Silicon builders over Tailscale mTLS (`macbook-pro`, `air3`).
   - If a build request targets an ARM64 node or `region: home`, the controller must infer `linux/arm64` automatically when `platform` is not explicitly overridden.
   - Any multi-architecture string (e.g. `linux/amd64,linux/arm64`) or mismatched builder selection must be rejected with `LumaError` instead of falling back to QEMU.

2. **Runtime Architecture Affinity & Sidecar Integrity**:
   - In a heterogeneous cluster containing both AMD64 and ARM64 nodes, Kubernetes scheduling requires `kubernetes.io/arch: <arch>` in `nodeSelector`. Without this label, Pods are scheduled arbitrarily, causing binary execution failures.
   - A Kubernetes Pod represents a group of containers scheduled onto a single node kernel. When Compose services share `network_mode: service:*`, they become containers in the same Pod. If their image architectures diverge (e.g., AMD64 primary and ARM64 sidecar), the Pod is unschedulable and broken. Detecting this mismatch during compilation prevents invalid HelmRelease rollouts.
   - Node pinning (`node: <node>`) or region constraints (`region: home`) must be validated against image architecture during compilation.

3. **openSUSE MicroOS Transactional & SELinux Invariants**:
   - openSUSE MicroOS uses a read-only root filesystem with `transactional-update`. Standard package installations and root writes fail by design.
   - State must reside on designated writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`).
   - MicroOS enforces SELinux. DaemonSet reconciliation must invoke `restorecon` after writing `registries.yaml` so K3s containerd can access mirror configurations.
   - MicroOS root pools run on btrfs; self-healing must monitor `btrfs scrub` and run trim across both `/` and `/var` subvolumes.

---

## 3. Caveats

- **Read-Only Scope**: This report is strictly observational and analytical; no codebase modifications have been made.
- **Dynamic Node Topologies**: In live environments, node architectures are reported dynamically via Luma agent heartbeats or Kubernetes node status. Static testing fixtures (such as `macbook-pro` and `air3`) represent canonical ARM64 nodes, and inference functions must support both explicit node mappings and well-known node names.
- **MicroOS SELinux Environment**: `restorecon` inside the privileged DaemonSet container operates via `nsenter -t 1` on the host namespace. If `restorecon` is not installed on the host, fallback to `chcon -t etc_t` or graceful continuation must be implemented.

---

## 4. Conclusion & Actionable Remediation Plan

To fulfill Requirement 2 (R2), apply the following seven targeted remediations:

### Remediation 1: Extend `luma/build_capabilities.py`
1. Implement `validate_native_platform(platform: str) -> str`:
   - Enforce single-architecture strings: if `","` is present, raise `LumaError("cross-architecture emulation and multi-platform compilation are disabled; submit builds separately for each native architecture")`.
   - Normalize: `arm64/v8` -> `arm64`, `aarch64` -> `arm64`, `x86_64` -> `amd64`.
   - Validate target is either `linux/arm64` or `linux/amd64` (or valid Darwin for local build testing).
2. Implement `infer_native_platform(*, platform: str = "", target_node: str = "", target_region: str = "", node_architectures: Mapping[str, str] | None = None) -> str`:
   - If `platform` is provided and non-empty: validate via `validate_native_platform(platform)`.
   - If `platform` is empty:
     - If `target_region == "home"` -> return `"linux/arm64"`.
     - If `target_node` is in `node_architectures`: return `validate_native_platform(node_architectures[target_node])`.
     - If `target_node in {"macbook-pro", "air3"}` or `"arm64"` in `target_node.lower()` -> return `"linux/arm64"`.
     - Otherwise default to `"linux/amd64"`.

### Remediation 2: Harden `BuildController` (`luma/paas/build/controller.py`)
1. In `select_native_builder(self, platform: str, *, preferred_node: str = "", target_node: str = "", target_region: str = "")`:
   - If `platform` is empty: infer via `infer_native_platform(target_node=target_node, target_region=target_region)`.
   - Validate `platform` with `validate_native_platform`.
   - If `preferred_node` or `target_node`: verify builder platform matches target platform. Raise `LumaError` on mismatch.
   - Filter ready native builders matching `target_platform`. Raise `LumaError` if none are available.
2. In `submit_build`:
   - Add `target_node: str = ""` and `target_region: str = ""` parameters.
   - Extract `node`/`region` from `build_spec` if not explicitly passed.
   - Automatically infer `linux/arm64` when targeting ARM64 nodes or `region: home` if `platform` is not overridden.
   - If `platform` is explicitly provided and conflicts with `target_node`/`target_region`, raise `LumaError`.

### Remediation 3: Harden `compile_service_release` and `compile_compose_release` (`luma/paas/release/helm_compiler.py`)
1. Append `,<local>` to `DEFAULT_NO_PROXY`.
2. Update `_build_region_node_selector(region: str, node_pin: str, arch: str = "") -> Dict[str, str]`:
   - When `arch` is supplied, emit `selector["kubernetes.io/arch"] = arch`.
3. In `compile_service_release`:
   - Resolve `arch_label` (`arm64` vs `amd64`) from `image_art.platform`.
   - Verify that `node_pin` (e.g. `macbook-pro`, `air3`) or `region: home` does not conflict with `arch_label`. Raise `LumaError` on mismatch.
   - Pass `arch=arch_label` into `_build_region_node_selector`.
4. In `compile_compose_release`:
   - For each independent service, resolve its primary image architecture and emit `kubernetes.io/arch` in `nodeSelector`.
   - Verify node and region compatibility with `main_arch`.
   - In the `sidecar_pairs` loop, inspect child container image architecture:
     ```python
     if child_arch and main_arch and child_arch != main_arch:
         raise LumaError(
             f"mixed-architecture pod sidecar mismatch: main service '{svc_name}' has architecture {main_arch} "
             f"({main_platform}) while sidecar '{child_name}' has architecture {child_arch} ({child_platform}); "
             f"containers sharing network_mode: service:* must have matching architectures"
         )
     ```

### Remediation 4: Harden `evaluate_node_self_heal` & `NodeSelfHealReport` (`luma/paas/migration/migrator.py`)
1. Extend `NodeSelfHealReport` with fields:
   - `transactional_rootfs: bool = False`
   - `writable_paths_converged: bool = True`
   - `selinux_contexts_converged: bool = True`
   - `btrfs_scrub_at: int = 0`
   - Update `to_dict()`.
2. Update `evaluate_node_self_heal`:
   - Handle `osName: "opensuse-microos"`:
     - Set `transactional_rootfs = True`.
     - Evaluate `writablePathsConverged`, `selinuxContextsConverged`, and `btrfsScrubAt`.
     - When `auto_fix=True`, heal missing writable subvolumes, restore SELinux labels, and run btrfs scrub & fstrim.
     - Append descriptive repair actions.
   - Handle `osName: "darwin"`:
     - Validate that `<local>` is present in `noProxy`. If missing and `auto_fix=True`, append repair action.

### Remediation 5: Harden `node-self-heal-daemonset.yaml`
1. Add hostPath volumes and volumeMounts for `/var/lib/rancher` and `/var/lib/buildkit`.
2. In the reconciler shell script:
   - Detect `opensuse-microos` from `/host/etc/os-release`.
   - Ensure `/host/etc/rancher/k3s`, `/host/var/lib/rancher/k3s`, `/host/var/lib/buildkit`, and `/host/var/run/luma` exist.
   - Run `restorecon -v /etc/rancher/k3s/registries.yaml` via `nsenter` on MicroOS.
   - Check and record `btrfs scrub status /` when btrfs filesystem is detected.

### Remediation 6: Align Migration CLI Shadow Platform Inference (`luma/cli/paas.py`)
In `cmd_migrate_shadow`, inspect manifest region and node; if targeting `home` or an ARM64 node, set adopted image platform to `linux/arm64` instead of defaulting hardcoded to `linux/amd64`.

### Remediation 7: Expand `tests/test_paas_engine.py`
1. Update existing assertion at line 170 to verify `"kubernetes.io/arch": "arm64"`.
2. Add comprehensive test cases:
   - `test_strict_native_builder_selection_and_qemu_rejection`
   - `test_platform_auto_inference_for_arm64_and_home_region`
   - `test_runtime_architecture_affinity_and_sidecar_mismatch_rejection`
   - `test_node_self_heal_opensuse_microos_and_darwin`

---

## 5. Verification Method

To independently verify the implementation once applied:
1. **Run PaaS Engine Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
2. **Run Native Builders Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_native_builders -v
   ```
3. **Run Full Verification Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
4. **Inspect Generated Manifests**:
   Verify that compiled workloads have `kubernetes.io/arch` matching image platform and that mixed sidecar configurations raise `LumaError`.
