# Forensic Audit Report: Milestone 3 — Helm Compiler & Workload Placement

**Auditor**: Forensic Auditor M3-1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/auditor_m3_1`  
**Target Milestone**: Milestone 3 (M3: Helm Compiler & Workload Placement)  
**Profile**: General Project (Development Mode, per `ORIGINAL_REQUEST.md:8`)  
**Verdict**: **CLEAN**

---

## 1. Observation

A forensic audit of all Milestone 3 targets was conducted across the codebase:
- `luma/paas/release/helm_compiler.py`
- `tests/test_paas_engine.py`

### 1.1 Source Code Verification (`helm_compiler.py`)
1. **Runtime Node Architecture Affinity (Feature 5, `helm_compiler.py:44-126`)**:
   - `_extract_kubernetes_arch(platform)` (lines 44–52): Validates platform via `validate_native_platform()`, extracts architecture suffix, and guarantees output is strictly `"arm64"` or `"amd64"`, raising `LumaError` for unsupported architectures.
   - `_build_region_node_selector(region, node_pin, arch="")` (lines 55–66): Emits `"kubernetes.io/arch": norm_arch` into `nodeSelector` alongside `"luma.io/region"`, `"topology.kubernetes.io/region"`, and `"kubernetes.io/hostname"`.
   - `_validate_workload_architecture(...)` (lines 69–126): Enforces strict architecture rules:
     - Infers native platform via `infer_native_platform()` and normalizes via `validate_native_platform()`.
     - Rejects any non-`arm64` workload targeting `region="home"` with `LumaError("conflicts with region home (Apple Silicon home cluster requires arm64); cross-architecture emulation is disabled")`.
     - Rejects cross-region topology conflicts (such as pinning `macbook-pro` or `air3` while targeting non-home regions like `cn`).
     - Validates against `node_architectures` mappings or node naming patterns, raising `LumaError("architecture mismatch")` on conflict.
2. **Compose Sidecar Graph Traversal & Mixed-Arch Rejection (Feature 5, `helm_compiler.py:839-923`)**:
   - Parses `network_mode: service:<target>` declarations in Compose services.
   - Validates that target service exists; raises `LumaError` for missing parent service (line 847).
   - Rejects self-referential sidecars (`target_svc == svc_name`) with `LumaError` (line 851).
   - Recursive cycle detection: `_find_root_primary(s, visited)` detects loops (`A -> B -> C -> A`) and raises `LumaError("cyclic network_mode dependency loop detected")` (lines 857–865).
   - Multi-tier sidecar chains (`A -> B -> C`) are cleanly flattened: `root_primary_map[child]` resolves each child container directly to the root primary service.
   - Architecture parity enforcement: Verifies that every sidecar container shares identical architecture with its root primary service; raises `LumaError(f"cannot co-locate mixed-architecture containers in pod sidecar: {child_name} ({child_norm}) vs {root_parent} ({parent_norm})")` on mismatch (lines 918–922).
3. **Compose Splitting & Volume Resolution (Feature 6, `helm_compiler.py:132-254, 931-1098`)**:
   - `_validate_storage_size(size, vol_name)` (lines 132–149): Validates against regex `^\d+(?:\.\d+)?(?:[eE][-+]?\d+)?([kKMGTPE]i?|[m])$`, explicitly rejecting unitless integers or raw digit strings with `LumaError`.
   - `_parse_volume_entry(entry)` (lines 152–178): Parses string (`source:target[:mode]`) and mapping definitions, extracting source, target mount path, and boolean `read_only` (mapping `:ro` to `True` and `:rw` to `False`).
   - `_resolve_service_volumes(...)` (lines 180–254):
     - Detects and rejects duplicate container mount paths within the same container (`seen_mount_paths`).
     - Distinguishes named volumes from hostPath binds.
     - Synthesizes `PersistentVolumeClaim` definitions with retain policy annotations (`luma.io/reclaim-policy: Retain`), region, node affinity, and resource requests.
     - Maps `:ro` to container volume mount `readOnly: True`.
   - Independent Deployment splitting (lines 947–1150): Non-sidecar services are compiled into distinct `apps/v1` `Deployment` manifests, allowing individual replica management and lifecycle independence.
   - Sidecar co-location (lines 1057–1098): All child containers linked to a root primary service are placed into the primary Pod spec's `containers` list, preserving sidecar custom `command` overrides (line 1096).
4. **Signature Exposure Modes Hardening (`helm_compiler.py:256-374, 1006-1012, 1169-1187`)**:
   - All 6 exposure modes (`none`, `cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`) execute distinct, authentic routing logic.
   - `exposure: none`: Produces exactly 0 Ingress resources, even when an explicit `domain` is specified in the manifest (lines 272–274).
   - `exposure: tcp-relay`: Compiles Traefik `IngressRouteTCP` with entryPoints (`tcp-<publishPort>`) and `HostSNI('*')` routing to the target container port. Sets Kubernetes Service port name to `"tcp"` (lines 735, 1154).
   - Reserved Traefik port protection: Rejects reserved ports (`80`, `443`, etc.) with `LumaError` (line 280).
   - TCP relay collision detection: In Compose stacks, tracks `seen_tcp_ports` and raises `LumaError` if multiple services attempt to bind the same `publishPort` (lines 1006–1012).
   - `exposure: tailscale-relay`: Attaches `luma.io/exposure: tailscale-relay`, `luma.io/relay-transport: tailscale-mesh`, and `luma.io/target-region: home` annotations.
   - `exposure: cloudflare-tunnel`: Attaches `luma.io/edge-gateway: cloudflare-tunnel`, `luma.io/tunnel-hostname`, and `luma.io/tunnel-service` annotations.

### 1.2 Cheating & Facade Analysis
- **Hardcoded test outputs**: Searched `luma/paas/release/helm_compiler.py` for test identifiers (`arm-service`, `pvc-test`, `tcp-collision`, `dup-mount`, `mesh-agent`, `serverx`, `granary`, `kato`, etc.). Zero occurrences found in production logic.
- **Facade detection**: No dummy stub methods, no unconditional fixed returns, no `NotImplementedError` placeholders, and no bypass decorators found.
- **Pre-populated artifacts**: Scanned workspace for pre-populated logs or attestation files predating current runs; none found for Milestone 3.
- **Test Integrity**: Unit tests in `tests/test_paas_engine.py` (lines 1386–2076) Suite 3 (`RuntimeArchitectureAffinityAndComposeIntegrityTests`) instantiate real `BuildArtifact` and `ServiceImageArtifact` instances, verifying real Kubernetes manifest dictionaries (`nodeSelector`, `containers`, `volumeMounts`, `volumes`, `ports`, `annotations`), real `LumaError` exceptions, and real regex patterns without mocks. Module `__getattr__` provides backward-compatible dynamic aliasing for `RuntimeNodeAffinityAndSidecarIntegrityTests`.

### 1.3 Adversarial Stress Testing Results
Empirical stress-testing scripts executed directly against `luma.paas.release.helm_compiler`:
1. *3-Level Sidecar Chain* (`leaf -> mid -> primary`):
   - Result: All 3 containers (`primary`, `mid`, `leaf`) successfully flattened and co-located into primary Pod. PASS.
2. *Chained Sidecar Architecture Mismatch* (`leaf` `linux/arm64` vs `primary` `linux/amd64` via `mid`):
   - Result: Correctly raised `LumaError: cannot co-locate mixed-architecture containers in pod sidecar: leaf (linux/arm64) vs primary (linux/amd64)`. PASS.
3. *Cyclic Dependency Loop* (`a -> b -> c -> a`):
   - Result: Correctly raised `LumaError: cyclic network_mode dependency loop detected: a -> b -> c -> a`. PASS.
4. *Self-Referential Network Mode* (`web -> service:web`):
   - Result: Correctly raised `LumaError: service 'web' cannot declare self-referential network_mode: service:web (loop)`. PASS.
5. *Missing Parent Service* (`agent -> service:nonexistent`):
   - Result: Correctly raised `LumaError: sidecar target service 'nonexistent' does not exist in compose file (missing parent service)`. PASS.
6. *Duplicate Container Volume Mounts* (`data1:/app/data`, `data2:/app/data`):
   - Result: Correctly raised `LumaError: duplicate mount path in container 'web': /app/data`. PASS.
7. *Unitless Storage Size* (`size: 50` or `size: "50"`):
   - Result: Correctly raised `LumaError: volume 'vol' storage size must specify Kubernetes resource quantity units (e.g. 10Gi, 500Mi): 50`. PASS.
8. *TCP Relay Port Collision Across Compose Services*:
   - Result: Correctly raised `LumaError: TCP relay port collision: publishPort 16379 already in use by another service in this release`. PASS.
9. *Exposure Mode `none` with Explicit Domain*:
   - Result: `len(compiled.ingresses) == 0`. Zero Ingresses produced. PASS.
10. *Service Port Name Under TCP Relay*:
    - Result: `spec.ports[0].name == "tcp"` for both service releases and compose releases. PASS.
11. *Sidecar Custom Command Override*:
    - Result: Child container retains exact command `["/bin/agent", "--foreground"]`. PASS.

### 1.4 Empirical Verification Results
- **Suite 3 Direct & Alias Unit Tests**:
  `./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeArchitectureAffinityAndComposeIntegrityTests tests.test_paas_engine.RuntimeNodeAffinityAndSidecarIntegrityTests -v`
  *Result*: 50 tests ran, 50 passed, 0 failures, 0 errors in 0.037s.
- **PaaS Engine & Native Builders Unit Tests**:
  `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`
  *Result*: 77 tests ran, 77 passed, 0 failures, 0 errors in 0.597s.
- **Combined PaaS Engine & Challenger Tests**:
  `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders tests.test_challenger_m1 tests.test_challenger_m2_2 -v`
  *Result*: 111 tests ran, 111 passed, 0 failures, 0 errors in 0.614s.
- **Ruff Linter Check**:
  `./.venv/bin/python -m ruff check luma tests scripts`
  *Result*: `All checks passed!`.
- **Full Repository Gate**:
  `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh`
  *Result*: Exit code 0 (110 Node tests, full test suite passed).
- **Static Site Build & Anchor Parity Check**:
  `./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site`
  *Result*: Exit code 0 ("Validated local links and assets across 48 HTML pages; bilingual sections match.").

---

## 2. Logic Chain

1. **Requirement Sourcing**:
   - `ORIGINAL_REQUEST.md` (R1 & R2) mandates:
     - Runtime architecture affinity: emitting `kubernetes.io/arch` (`arm64` vs `amd64`) in `nodeSelector` matching artifact platform.
     - Rejecting mixed-architecture pod sidecar mismatches under `network_mode: service:*`.
     - Rejecting non-ARM64 workloads on Apple Silicon (`home` region) nodes.
     - Proper Compose splitting vs sidecar co-location, PVC volume mounting, and hardening of all 6 exposure modes (`cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`, `none`).
2. **Empirical Verification of Invariants**:
   - `_extract_kubernetes_arch` and `_validate_workload_architecture` verify architecture compatibility before emitting manifests. Injected `nodeSelector` values match RFC 1123 label standards (`"arm64"`, `"amd64"`).
   - Sidecar graph resolution correctly traverses directed acyclic dependency chains, terminates cycles with explicit error diagnostics, and verifies architecture parity between leaf sidecars and the root primary Pod host.
   - Compose PVC generation parses top-level and service-level volume definitions, attaches PVC claims to `pod_spec["volumes"]`, attaches mounts to `volumeMounts`, preserves `:ro` read-only flags, and guards against duplicate container mount paths.
   - All 6 exposure modes conform to specifications: `none` generates no Ingress; `tcp-relay` generates Traefik `IngressRouteTCP` and sets Service port names to `"tcp"`; `tailscale-relay` and `cloudflare-tunnel` inject proper gateway and mesh metadata annotations.
3. **Adversarial Resilience**:
   - Recursive loop detection prevents infinite recursion on malformed compose specs.
   - Port collision detection prevents conflicting Traefik TCP bindings before deployment.
   - Unitless storage quantities are blocked before invalid Kubernetes manifests reach cluster admission controllers.

---

## 3. Caveats

1. **Unmounted Top-Level Storage Synthesis**:
   Top-level storage items declared under `storage:` in `luma.yaml` or `volumes:` in Compose that are not directly mounted in a service container are still synthesized into `compiled.pvcs` (using configured sizes or default `"10Gi"`). This preserves pre-provisioned persistent volumes across release cycles even when temporarily detached from running containers.
2. **Module-Level Test Aliasing**:
   `__getattr__` in `tests/test_paas_engine.py` dynamically routes `RuntimeNodeAffinityAndSidecarIntegrityTests` to `RuntimeArchitectureAffinityAndComposeIntegrityTests`. This ensures 100% backward compatibility with test runners and exploratory harnesses without duplicating test instances in `unittest discover`.

---

## 4. Conclusion

The work products delivered in Milestone 3 (`luma/paas/release/helm_compiler.py` and `tests/test_paas_engine.py`) fulfill all functional requirements, adhere strictly to architectural contracts, and pass all forensic checks under Development mode. No facade implementations, test evasions, or hardcoded shortcuts exist. The implementation exhibits genuine domain logic, robust error handling, and comprehensive test coverage.

**Final Verdict**: **CLEAN**

---

## 5. Verification Method

To independently reproduce this forensic audit:

```bash
# 1. Run PaaS engine and native builders unit tests (77 tests)
./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v

# 2. Run Suite 3 unit tests directly and via alias (50 tests)
./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeArchitectureAffinityAndComposeIntegrityTests tests.test_paas_engine.RuntimeNodeAffinityAndSidecarIntegrityTests -v

# 3. Run combined PaaS engine and challenger suites (111 tests)
./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders tests.test_challenger_m1 tests.test_challenger_m2_2 -v

# 4. Run Ruff linter check
./.venv/bin/python -m ruff check luma tests scripts

# 5. Run full repository gate
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh

# 6. Run bilingual documentation and anchor parity verification
./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
```

### Invalidation Conditions:
- If `kubernetes.io/arch` is omitted from `nodeSelector` in compiled service or compose releases, `test_service_nodeselector_kubernetes_arch_arm64_and_amd64` or `test_compose_nodeselector_kubernetes_arch_per_independent_service` will fail.
- If mixed-architecture sidecars under `network_mode: service:*` are permitted without raising `LumaError`, `test_compose_sidecar_architecture_mismatch_rejection` will fail.
- If cyclic sidecar chains cause infinite recursion or unhandled exceptions rather than raising `LumaError`, `test_compound_and_cyclic_sidecar_chains` will fail.
- If `exposure: none` generates an Ingress resource when a domain is present, `test_exposure_mode_none_suppression` will fail.
- If duplicate volume mount paths in the same container are accepted without error, `test_compose_duplicate_volume_mount_paths_rejection` will fail.
- If TCP relay port collisions across Compose services are not detected, `test_tcp_relay_port_collision_and_reserved_ports` will fail.
