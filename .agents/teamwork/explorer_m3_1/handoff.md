# Handoff Report: Feature 5 Investigation (Runtime Node Architecture Affinity & Sidecar Integrity)

**Author**: Explorer M3-1  
**Target File**: `luma/paas/release/helm_compiler.py`  
**Dependencies**: `luma/build_capabilities.py`, `luma/paas/build/artifact_store.py`, `tests/test_paas_engine.py`  
**Milestone**: M3 (Helm Compiler & Workload Placement)  
**Status**: Completed (Read-Only Investigation)

---

## 1. Observation

### 1.1 `nodeSelector` Architecture Affinity Omission
In `luma/paas/release/helm_compiler.py`:
- Lines 42–50 define `_build_region_node_selector`:
```python
42: def _build_region_node_selector(region: str, node_pin: str) -> Dict[str, str]:
43:     """Compile Luma's region + node placement contract into Kubernetes nodeSelector labels."""
44:     selector: Dict[str, str] = {}
45:     if region:
46:         selector["luma.io/region"] = region
47:         selector["topology.kubernetes.io/region"] = region
48:     if node_pin:
49:         selector["kubernetes.io/hostname"] = node_pin
50:     return selector
```
Observation: `_build_region_node_selector` only maps `region` and `node_pin` to Kubernetes labels (`luma.io/region`, `topology.kubernetes.io/region`, `kubernetes.io/hostname`). It does NOT take an architecture or platform argument, and NEVER emits `"kubernetes.io/arch"`.

- In `compile_service_release` (lines 442–445):
```python
442:     node_selector = _build_region_node_selector(region, node_pin)
443:     if node_selector:
444:         pod_spec["nodeSelector"] = node_selector
```
Observation: `compile_service_release` resolves `image_art = next(iter(artifact.images.values()))` (line 375), which contains `image_art.platform` (e.g., `"linux/arm64"` or `"linux/amd64"`). However, this platform is completely ignored when generating `nodeSelector`.

- In `compile_compose_release` (lines 704–706):
```python
704:         node_selector = _build_region_node_selector(svc_region, node_pin)
705:         if node_selector:
706:             pod_spec["nodeSelector"] = node_selector
```
Observation: `compile_compose_release` constructs independent deployments for each compose service, but also calls `_build_region_node_selector(svc_region, node_pin)` without extracting or attaching the container image architecture to `nodeSelector`.

### 1.2 Sidecar Linkage & Mixed-Architecture Rejection Gaps
In `luma/paas/release/helm_compiler.py`:
- In `compile_compose_release` (lines 596–605):
```python
596:     sidecar_pairs: Dict[str, str] = {}
597:     for svc_name, svc_body in compose_services.items():
598:         if not isinstance(svc_body, dict):
599:             continue
600:         net_mode = str(svc_body.get("network_mode") or "").strip()
601:         if net_mode.startswith("service:"):
602:             target_svc = net_mode.split(":", 1)[1].strip()
603:             if target_svc in compose_services:
604:                 sidecar_pairs[str(svc_name)] = target_svc
```
Observation:
1. If `target_svc` does NOT exist in `compose_services`, the compiler silently skips the sidecar without raising an error (violating TC-B-F5-02).
2. If `target_svc == svc_name` (self-referential), line 604 records `sidecar_pairs[svc_name] = svc_name`, which is later skipped in line 641, completely dropping the service without error (violating TC-B-F5-03).
3. If sidecars are chained transitively (`A -> B -> C`), lines 678–680 only match direct `parent_name == svc_name`, causing transitive sidecars to be dropped from Pod containers (violating TC-B-F5-01).

- In `compile_compose_release` sidecar container injection (lines 678–696):
```python
678:         for child_name, parent_name in sorted(sidecar_pairs.items()):
679:             if parent_name != svc_name:
680:                 continue
681:             child_body = compose_services[child_name]
682:             child_img = (
683:                 artifact.images[child_name].immutable_ref
684:                 if child_name in artifact.images
685:                 else str(child_body.get("image") or "")
686:             )
687:             child_container: Dict[str, Any] = {
688:                 "name": slugify(child_name),
689:                 "image": child_img,
690:                 "envFrom": [{"secretRef": {"name": secret_manifest["metadata"]["name"]}}],
691:                 "volumeMounts": list(secret_mounts),
692:             }
693:             if proxy_env:
694:                 child_container["env"] = proxy_env
695:             containers.append(child_container)
```
Observation: The compiler extracts `child_img` and appends `child_container` to `containers` with ZERO validation of the sidecar's platform vs the primary container's platform. If `svc_name` is `linux/amd64` and `child_name` is `linux/arm64`, it creates a single Pod manifest containing mixed-architecture containers.

### 1.3 Missing Architecture Placement Validation & Signatures
In `luma/paas/release/helm_compiler.py`:
- Function signatures for `compile_service_release` (line 360) and `compile_compose_release` (line 556):
```python
360: def compile_service_release(
361:     *,
362:     artifact: BuildArtifact,
363:     environment: str = "prod",
364:     generation: int = 1,
365:     replicas_override: int | None = None,
366:     secrets: Mapping[str, str] | None = None,
367:     mounted_secret_files: Mapping[str, str] | None = None,
368: ) -> CompiledWorkload:
```
Observation:
1. `artifact` is keyword-only (`*` at line 361), but TEST_INFRA.md (lines 546, 568, 593, 622) and standard Python conventions invoke `compile_service_release(conflict_art, ...)` positionally.
2. `node_architectures: Mapping[str, str] | None = None` is completely missing from both function signatures.
3. No check exists validating whether an artifact's platform (e.g. `linux/amd64`) conflicts with `region: home` (which strictly requires `arm64`) or a pinned node's architecture (e.g. `node: macbook-pro` which is ARM64).

### 1.4 Test Suite Impact in `tests/test_paas_engine.py`
In `tests/test_paas_engine.py` (lines 174–183):
```python
174:         dep = compiled.deployments[0]
175:         pod_spec = dep["spec"]["template"]["spec"]
176:         self.assertEqual(
177:             pod_spec["nodeSelector"],
178:             {
179:                 "luma.io/region": "home",
180:                 "topology.kubernetes.io/region": "home",
181:                 "kubernetes.io/hostname": "macbook-pro",
182:             },
183:         )
```
Observation: The existing regression test `test_luma_signature_features_in_service_and_compose_compiler` strictly asserts the exact dictionary content of `pod_spec["nodeSelector"]`. Because `home_artifact` specifies `platform="linux/arm64"`, injecting `"kubernetes.io/arch": "arm64"` will cause this test to fail unless the test assertion is updated to include `"kubernetes.io/arch": "arm64"`.

---

## 2. Logic Chain

1. **Kubernetes Runtime Constraint**: A Kubernetes Pod schedules onto a single physical/virtual node. All containers within a single Pod (primary application + sidecars sharing network or IPC namespaces) execute on that node's CPU architecture. Kubernetes nodes advertise their architecture via the standard node label `kubernetes.io/arch` (`arm64` or `amd64`).
2. **From Observation 1.1**: Because `_build_region_node_selector` only emits `region` and `hostname`, Pods compiled by `compile_service_release` and `compile_compose_release` have no architecture constraints in their `nodeSelector`. In a heterogeneous cluster (openSUSE MicroOS ARM64 + AMD64 nodes), an ARM64 container can be scheduled onto an AMD64 node (or vice versa), resulting in `exec format error` crashloops.
3. **From Observation 1.2**: In `compile_compose_release`, when a service defines `network_mode: service:<target>`, Docker Compose semantics mean the sidecar shares the target container's network namespace (co-located in the same Kubernetes Pod). Because the compiler never inspects `artifact.images[child_name].platform` against `artifact.images[parent_name].platform`, it compiles invalid mixed-architecture Pod manifests.
4. **From Observation 1.3**: When an operator specifies `region: home` or pins a deployment to a known ARM64 node (e.g., `macbook-pro`), compiling an AMD64 image targeting that placement is an irrecoverable configuration error. The compiler must intercept and reject this before emitting a Flux `HelmRelease`.
5. **Remediation Derivation**:
   - Normalize platforms via `validate_native_platform` (from `luma.build_capabilities`).
   - Extract the standard Kubernetes architecture label (`arm64` vs `amd64`) via `norm.split("/")[-1]`.
   - Validate architecture compatibility against `region` (`home` requires `arm64`) and `node_pin` (using `node_architectures` or known node heuristics).
   - Inject `"kubernetes.io/arch": <arch>` into `spec.template.spec.nodeSelector`.
   - Before attaching sidecars in Compose, verify `child_arch == parent_arch`; if unequal, raise `LumaError` with the required verbatim message.

---

## 3. Caveats

1. **Keyword vs Positional Argument Compatibility**:
   - `compile_service_release` and `compile_compose_release` currently begin with `*`, forcing keyword arguments. Changing to `artifact: BuildArtifact, *` allows both positional (`compile_service_release(art)`) and keyword (`compile_service_release(artifact=art)`) invocations without breaking existing callers in `flux_reconciler.py` or `tests/test_paas_engine.py`.
2. **Compose Services Without Pre-resolved Artifact Image**:
   - In rare cases where a compose service is not in `artifact.images` (e.g., dynamic mock definitions in legacy tests), fallback platform inference must occur: check `svc_body.get("platform")`, then infer via `infer_native_platform(target_node=node_pin, target_region=svc_region, node_architectures=node_architectures)`.
3. **Transitive Sidecar Chaining**:
   - Docker Compose allows `sidecar-2` to join `service:sidecar-1` where `sidecar-1` joins `service:web`. The compiler should flatten transitive dependencies to the root primary container (`web`) and verify that all containers in the chain share identical architecture.
4. **Existing Test Suite Assertion**:
   - Updating `nodeSelector` requires updating `tests/test_paas_engine.py:182` to expect `"kubernetes.io/arch": "arm64"`.

---

## 4. Conclusion & Concrete Remediation Plan

To fulfill Feature 5 and pass the Milestone 3 gate, the following changes must be applied to `luma/paas/release/helm_compiler.py`:

### 4.1 Imports
In `luma/paas/release/helm_compiler.py`:
```python
from ...build_capabilities import infer_native_platform, validate_native_platform
```

### 4.2 Helper Functions
Add `_extract_kubernetes_arch` and `_validate_workload_architecture`, and update `_build_region_node_selector`:

```python
def _extract_kubernetes_arch(platform: str) -> str:
    """Extract Kubernetes node architecture label (arm64, amd64) from platform string."""
    if not platform:
        return "amd64"
    norm = validate_native_platform(platform)
    arch = norm.split("/")[-1]
    if arch not in {"arm64", "amd64"}:
        raise LumaError(f"unsupported Kubernetes node architecture label: {arch!r}")
    return arch


def _build_region_node_selector(region: str, node_pin: str, arch: str = "") -> Dict[str, str]:
    """Compile Luma's region + node placement contract into Kubernetes nodeSelector labels."""
    selector: Dict[str, str] = {}
    if region:
        selector["luma.io/region"] = region
        selector["topology.kubernetes.io/region"] = region
    if node_pin:
        selector["kubernetes.io/hostname"] = node_pin
    if arch:
        selector["kubernetes.io/arch"] = arch
    return selector


def _validate_workload_architecture(
    platform: str,
    *,
    region: str = "",
    node_pin: str = "",
    node_architectures: Mapping[str, str] | None = None,
    service_name: str = "",
) -> str:
    """Validate platform against region and node placement; return normalized Kubernetes arch."""
    clean_platform = validate_native_platform(platform)
    k8s_arch = _extract_kubernetes_arch(clean_platform)
    svc_prefix = f"service '{service_name}' " if service_name else ""

    clean_region = str(region or "").strip().lower()
    if clean_region == "home" and k8s_arch != "arm64":
        raise LumaError(
            f"workload architecture mismatch: {svc_prefix}platform {clean_platform} "
            f"conflicts with region home (Apple Silicon home cluster requires arm64); "
            f"cross-architecture emulation is disabled"
        )

    clean_node = str(node_pin or "").strip()
    if clean_node:
        expected_arch = ""
        if node_architectures:
            node_arch = node_architectures.get(clean_node)
            if not node_arch and clean_node.lower() in node_architectures:
                node_arch = node_architectures[clean_node.lower()]
            if node_arch:
                norm_node = validate_native_platform(str(node_arch).strip())
                expected_arch = norm_node.split("/")[-1]

        if not expected_arch:
            lower_node = clean_node.lower()
            if lower_node in {"macbook-pro", "air3"} or "arm64" in lower_node or "aarch64" in lower_node:
                expected_arch = "arm64"
            elif "amd64" in lower_node or "x86_64" in lower_node:
                expected_arch = "amd64"

        if expected_arch and k8s_arch != expected_arch:
            raise LumaError(
                f"architecture mismatch: {svc_prefix}node {clean_node} ({expected_arch}) "
                f"does not match image platform {clean_platform} ({k8s_arch})"
            )

    return k8s_arch
```

### 4.3 Update `compile_service_release`
Update signature and lines 375–445:
```python
def compile_service_release(
    artifact: BuildArtifact,
    *,
    environment: str = "prod",
    generation: int = 1,
    replicas_override: int | Mapping[str, int] | None = None,
    secrets: Mapping[str, str] | None = None,
    mounted_secret_files: Mapping[str, str] | None = None,
    node_architectures: Mapping[str, str] | None = None,
) -> CompiledWorkload:
    ...
    image_art = next(iter(artifact.images.values()))
    image_platform = image_art.platform or "linux/amd64"
    arch = _validate_workload_architecture(
        image_platform,
        region=region,
        node_pin=node_pin,
        node_architectures=node_architectures,
        service_name=project,
    )
    ...
    node_selector = _build_region_node_selector(region, node_pin, arch=arch)
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
```

### 4.4 Update `compile_compose_release`
Update signature, sidecar validation, and lines 596–706:
```python
def compile_compose_release(
    artifact: BuildArtifact,
    *,
    environment: str = "prod",
    generation: int = 1,
    replicas_override: Mapping[str, int] | None = None,
    secrets: Mapping[str, str] | None = None,
    mounted_secret_files: Mapping[str, str] | None = None,
    node_architectures: Mapping[str, str] | None = None,
) -> CompiledWorkload:
    ...
    # 1. Parse and validate network_mode: service:<target>
    sidecar_pairs: Dict[str, str] = {}
    for svc_name, svc_body in compose_services.items():
        if not isinstance(svc_body, dict):
            continue
        net_mode = str(svc_body.get("network_mode") or "").strip()
        if net_mode.startswith("service:"):
            target_svc = net_mode.split(":", 1)[1].strip()
            if not target_svc or target_svc not in compose_services:
                raise LumaError(
                    f"compose service {svc_name!r} specifies network_mode 'service:{target_svc}', "
                    f"but target primary service {target_svc!r} does not exist in compose services"
                )
            if target_svc == svc_name:
                raise LumaError(
                    f"service {svc_name!r} specifies self-referential network_mode: service:{target_svc}"
                )
            sidecar_pairs[str(svc_name)] = target_svc

    # 2. Check for cycles and resolve root primary containers
    def _find_root_primary(s: str, visited: list[str]) -> str:
        if s in visited:
            raise LumaError(
                f"circular network_mode dependency detected: {' -> '.join(visited + [s])}"
            )
        parent = sidecar_pairs.get(s)
        if parent:
            return _find_root_primary(parent, visited + [s])
        return s

    root_primary_map: Dict[str, str] = {}
    for child in sidecar_pairs:
        root_primary_map[child] = _find_root_primary(child, [child])

    # 3. Validate sidecar architecture parity against primary service
    for child_name, parent_name in sorted(sidecar_pairs.items()):
        child_platform = (
            artifact.images[child_name].platform
            if child_name in artifact.images
            else str((compose_services.get(child_name) or {}).get("platform") or "linux/amd64")
        )
        parent_platform = (
            artifact.images[parent_name].platform
            if parent_name in artifact.images
            else str((compose_services.get(parent_name) or {}).get("platform") or "linux/amd64")
        )

        child_norm = validate_native_platform(child_platform)
        parent_norm = validate_native_platform(parent_platform)
        child_arch = _extract_kubernetes_arch(child_norm)
        parent_arch = _extract_kubernetes_arch(parent_norm)

        if child_arch != parent_arch:
            raise LumaError(
                f"cannot co-locate mixed-architecture containers in pod sidecar: "
                f"{child_name} ({child_norm}) vs {parent_name} ({parent_norm})"
            )

    ...
    # 4. In deployment loop for each primary service svc_name:
    if svc_name in artifact.images:
        svc_platform = artifact.images[svc_name].platform
    else:
        svc_platform = str(svc_body.get("platform") or "linux/amd64")

    svc_arch = _validate_workload_architecture(
        svc_platform,
        region=svc_region,
        node_pin=node_pin,
        node_architectures=node_architectures,
        service_name=svc_name,
    )
    ...
    node_selector = _build_region_node_selector(svc_region, node_pin, arch=svc_arch)
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
```

---

## 5. Verification Method

To independently verify the implementation:

1. **Unit Test Execution**:
   Run the full PaaS test suite:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
2. **Milestone 3 Test Suite Integration**:
   Add test suite `RuntimeArchitectureAffinityAndComposeIntegrityTests` (from `TEST_INFRA.md` lines 522–624) into `tests/test_paas_engine.py`. It verifies:
   - `test_kubernetes_arch_node_selector_injection` (TC-F5-01, TC-F5-02)
   - `test_node_architecture_conflict_rejection` (TC-F5-03, TC-F5-04)
   - `test_compose_sidecar_architecture_mismatch_rejection` (TC-F5-05)
   - Boundary tests for missing parent service, self-reference, and chain loops (TC-B-F5-01, TC-B-F5-02, TC-B-F5-03).
3. **Existing Test Invariant**:
   In `tests/test_paas_engine.py` line 182, verify that `pod_spec["nodeSelector"]` matches:
   ```python
   {
       "luma.io/region": "home",
       "topology.kubernetes.io/region": "home",
       "kubernetes.io/hostname": "macbook-pro",
       "kubernetes.io/arch": "arm64",
   }
   ```
4. **Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
