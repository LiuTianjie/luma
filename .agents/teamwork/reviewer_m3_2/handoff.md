# Milestone 3 Review & Adversarial Challenge Report (Feature 6 Focus)

**Reviewer**: Reviewer M3-2 (Reviewer & Adversarial Critic)  
**Parent**: Project Orchestrator (`1387c386-51c1-4a2c-b02a-a266794155bd`)  
**Workspace**: `/Users/turning4th/infra-stacks`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/reviewer_m3_2`  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW**

---

## 1. Observation

Direct code inspection, adversarial stress-testing, and test execution on `luma/paas/release/helm_compiler.py` and `tests/test_paas_engine.py` yielded the following concrete observations:

### Observation 1: PVC Volume Synthesis & Pod/Container Attachment
- In `luma/paas/release/helm_compiler.py` (lines 180–253):
  `_resolve_service_volumes` parses volume specifications, distinguishes named volumes from hostPath binds, validates duplicate mount paths per container (lines 198–202), synthesizes PVC definitions with `luma.io/reclaim-policy: Retain` and requested storage sizes (lines 208–231), registers persistent volume claims in `existing_pod_volumes` (lines 232–236), and returns container mounts with `:ro` mapped to `readOnly: True` (lines 237–240).
- In `compile_service_release` (lines 625–693):
  `pod_volumes_by_name` is initialized with secret volumes, updated with service volumes, and assigned directly to `pod_spec["volumes"]` (line 690). `container_mounts` is assigned to `container_spec["volumeMounts"]` (line 680). Top-level unmounted storage declarations are preserved in `pvcs_by_name` (lines 647–669).
- In `compile_compose_release` (lines 1014–1104):
  `pod_volumes_by_name` is populated for each independent deployment. `main_mounts` is attached to `main_container["volumeMounts"]` (line 1043), `child_mounts` is attached to `child_container["volumeMounts"]` (line 1090), and all volumes are attached to `pod_spec["volumes"]` (line 1102). Unmounted storage definitions are synthesized into `pvcs_by_name` (lines 1201–1223).

### Observation 2: Read-Only Flag (`:ro`) Parsing
- In `luma/paas/release/helm_compiler.py` (lines 152–178):
  `_parse_volume_entry` splits entries by colon. For 3-part entries (e.g. `media-ro:/var/media/readonly:ro`), `parts[0]` is `source`, `parts[1]` is `target`, and `parts[2].lower()` is checked for `"ro"` (`read_only = True`) or `"rw"` (`read_only = False`). Invalid flags raise `LumaError(f"invalid volume access mode flag {parts[2]!r} in {entry!r}")`.
- In `_resolve_service_volumes` (lines 238–240 and 249–251):
  When `read_only` is true, `mount["readOnly"] = True` is set. Mount path contains solely `target` (`/var/media/readonly`), without `:ro` trailing contamination.

### Observation 3: Custom Sidecar `command` Preservation
- In `luma/paas/release/helm_compiler.py`:
  - Compose child containers (lines 1094–1096):
    ```python
    if child_body.get("command"):
        child_cmd = child_body["command"]
        child_container["command"] = child_cmd if isinstance(child_cmd, list) else ["sh", "-c", str(child_cmd)]
    ```
  - Compose main container (lines 1051–1053):
    ```python
    if svc_body.get("command"):
        cmd = svc_body["command"]
        main_container["command"] = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]
    ```
  - Single service (lines 684–686):
    ```python
    if parsed.get("command"):
        cmd = parsed["command"]
        container_spec["command"] = cmd if isinstance(cmd, list) else ["sh", "-c", str(cmd)]
    ```

### Observation 4: All 6 Exposure Modes & Ingress Synthesis
- In `luma/paas/release/helm_compiler.py` (lines 256–374):
  `_compile_exposure_ingress` validates `exposure in VALID_EXPOSURES` (`{"none", "cn-edge", "tailscale-relay", "cloudflare-tunnel", "external-edge", "tcp-relay"}`) and checks regional constraints via `validate_region_exposure`:
  1. `none`: returns `([], probe)`.
  2. `cn-edge`: requires region `cn`, generates Ingress with `luma.io/edge-gateway: "cn-edge"` and TLS router annotations.
  3. `external-edge`: requires region `global`, generates Ingress with `luma.io/edge-gateway: "global-edge"` and `external-dns.alpha.kubernetes.io/hostname`.
  4. `tailscale-relay`: requires region `home`, generates Ingress with `luma.io/edge-gateway: "tailscale-relay"`, `luma.io/relay-transport: "tailscale-mesh"`, `luma.io/target-region: "home"`.
  5. `cloudflare-tunnel`: allowed in `cn`, `global`, `home`, generates Ingress with `luma.io/edge-gateway: "cloudflare-tunnel"`, `luma.io/tunnel-hostname: domain`, and `luma.io/tunnel-service`.
  6. `tcp-relay`: generates Traefik `IngressRouteTCP` with entryPoint `tcp_entrypoint_name(relay_port)`.

### Observation 5: Ingress Suppression Under `exposure: none`
- In `compile_service_release` (lines 592–595 and 746–754):
  Explicit `exposure: none` bypasses default fallback logic and executes `_compile_exposure_ingress(..., exposure="none")`, returning an empty list for `compiled.ingresses` even when `domain` is specified.
- In `compile_compose_release` (lines 997–1005 and 1170–1187):
  Services declaring `exposure: none` completely bypass `_compile_exposure_ingress` and emit zero ingress manifests.

### Observation 6: TCP Relay Port Naming & Collision Detection
- In `compile_service_release` (lines 735–743):
  `port_name = "tcp" if exposure == "tcp-relay" else "http"` is set for the Service port.
- In `compile_compose_release` (lines 1153–1165):
  `port_name = "tcp" if exposure == "tcp-relay" else "http"` is set for the Service port.
- In `compile_compose_release` (line 1047):
  `port_name = "tcp" if exposure == "tcp-relay" else "http"` is set for container ports.
- In `compile_compose_release` (lines 1006–1012):
  `seen_tcp_ports: set[int]` records assigned ports; duplicate `publishPort` or `port` values raise `LumaError("TCP relay port collision: publishPort <N> already in use by another service in this release")`.
- In `_compile_exposure_ingress` (lines 280–281):
  Reserved ports 80 and 443 raise `LumaError(f"tcp-relay cannot use reserved Traefik port: {relay_port}")`.

### Observation 7: Integrity and Test Suite Execution
- Integrity Check:
  - No dummy or facade classes exist.
  - No hardcoded test identifiers, release names, or expected outputs are embedded in compiler logic.
  - Unit tests inspect real manifest structures, dictionary keys, annotations, and exception types.
- Test Commands and Results:
  - `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`: Ran 77 tests in 0.566s -> **OK**.
  - `./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeArchitectureAffinityAndComposeIntegrityTests -v`: Ran 25 tests in 0.019s -> **OK**.
  - `./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeNodeAffinityAndSidecarIntegrityTests -v`: Ran 25 tests in 0.019s -> **OK**.
  - `./.venv/bin/python -m ruff check luma tests scripts`: **All checks passed!**
  - `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh`: Full test gate passed cleanly -> **OK**.
  - Bilingual site check (`scripts/build-site.py` + `scripts/check-site.py`): 48 HTML pages validated, bilingual anchors match -> **OK**.

---

## 2. Logic Chain

1. **Volume Synthesis and Pod/Container Linkage**:
   - *Premise*: If PVC manifests are generated without corresponding Pod `spec.template.spec.volumes` and container `volumeMounts`, Kubernetes pods fail to schedule with unmounted storage errors.
   - *Observation*: Observations 1 and 2 demonstrate that `_resolve_service_volumes` creates both PVC manifests and container mounts, registering them in `pod_volumes_by_name`. Both `compile_service_release` and `compile_compose_release` pass these volumes to the Pod template and container specifications.
   - *Deduction*: Persistent volume management is complete and end-to-end connected in both single-service and Compose workloads.

2. **Mount Path Protection and Access Modes**:
   - *Premise*: Appending `:ro` directly into `mountPath` results in invalid POSIX directory targets in containers.
   - *Observation*: Observation 2 confirms that `_parse_volume_entry` strips `:ro` and maps it directly to the boolean field `readOnly: True`.
   - *Deduction*: Mount paths are sanitized and access modes conform strictly to Kubernetes API specifications.

3. **Exposure Mode Routing & Suppression**:
   - *Premise*: Fallback logic must not coerce internal services into public ingresses, and L4 TCP relays must not be assigned L7 HTTP port identifiers.
   - *Observation*: Observations 4, 5, and 6 verify that `exposure: none` unconditionally suppresses Ingress generation, and `exposure: tcp-relay` assigns port name `"tcp"`, verifies reserved ports, and rejects port collisions across services.
   - *Deduction*: All 6 exposure modes enforce secure, protocol-correct ingress routing.

4. **Adversarial Resilience**:
   - *Premise*: Workloads with self-referential sidecars, cyclic dependency chains, duplicate mount paths, unitless storage quantities, or mismatched architectures must fail fast at compilation time rather than crashing at runtime.
   - *Observation*: Observations 1, 3, 4, 6, and Suite 3 test cases confirm that all failure modes raise descriptive `LumaError` exceptions.
   - *Deduction*: The compiler provides defensive preflight validation preventing invalid cluster deployments.

---

## 3. Caveats

1. **HostPath Storage vs PVC**:
   - Named volumes generate `PersistentVolumeClaim` objects backed by `storageClass`. Path binds starting with `/`, `.`, or `~` generate `hostPath` Pod volumes without creating PVC manifests. This aligns with Kubernetes and Docker Compose host-mount conventions.
2. **Top-Level Storage Definitions**:
   - Storage entries declared at top level but not referenced in container volumes are synthesized into `compiled.pvcs` with reclaim policy `Retain` to preserve pre-provisioned storage across releases.

---

## 4. Conclusion & Verdict

**Verdict**: **APPROVE**

Milestone 3 implementation (`luma/paas/release/helm_compiler.py` and `tests/test_paas_engine.py`) satisfies all functional and non-functional requirements for Feature 5 and Feature 6:
- Volume mounting and `:ro` parsing operate cleanly.
- Sidecar commands are faithfully preserved.
- All 6 exposure modes compile into correct Kubernetes and Traefik CRDs.
- Ingress is suppressed under `exposure: none`.
- TCP relay services use port name `"tcp"` and reject collisions.
- Zero integrity violations detected; all automated test gates pass with 100% success.

---

## 5. Verification Method

To independently reproduce and verify this assessment:

1. **Run PaaS Engine and Native Builder Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected Output*: Ran 77 tests -> OK.

2. **Run Suite 3 Tests (Direct and Dynamic Alias)**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeArchitectureAffinityAndComposeIntegrityTests -v
   ./.venv/bin/python -m unittest tests.test_paas_engine.RuntimeNodeAffinityAndSidecarIntegrityTests -v
   ```
   *Expected Output*: Ran 25 tests -> OK.

3. **Run Ruff Code Quality Check**:
   ```bash
   ./.venv/bin/python -m ruff check luma tests scripts
   ```
   *Expected Output*: All checks passed!

4. **Run Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected Output*: Exit code 0.

5. **Run Bilingual Documentation & Site Validator**:
   ```bash
   mkdir -p /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch && \
   ./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && \
   ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
   ```
   *Expected Output*: Exit code 0 ("Validated local links and assets across 48 HTML pages; bilingual sections match.").
