## 2026-10-15T17:55:27Z
You are Worker M3.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/worker_m3
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read the thorough explorer blueprints at:
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m3_1/handoff.md (Feature 5: Architecture Affinity & Sidecars)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m3_2/handoff.md (Feature 6: Compose Splitting, PVCs & 6 Exposure Modes)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m3_3/handoff.md (Suite 3 Test Suite Design)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m3_3/suite_3_test_code.py (Executable Suite 3 Python Code)
- /Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT
hardcode test results, create dummy/facade implementations, or
circumvent the intended task. A teamwork_preview_auditor will independently
verify your work. Integrity violations WILL be detected and your
work WILL be rejected.

File Write Ownership (You own these files exclusively):
- `luma/paas/release/helm_compiler.py`
- `tests/test_paas_engine.py` (adding Suite 3 tests and updating legacy nodeSelector assertion)

Objective & Tasks:
Implement Milestone 3 (M3: Helm Compiler & Workload Placement) covering Feature 5 and Feature 6:

1. `luma/paas/release/helm_compiler.py`:
   - Feature 5 (Runtime Node Architecture Affinity & Sidecar Integrity):
     - Update `_build_region_node_selector(region: str, node_pin: str, arch: str = "") -> Dict[str, str]` to inject `kubernetes.io/arch: arch` when `arch` is non-empty. Normalizes `linux/arm64` -> `arm64`, `linux/amd64` -> `amd64`.
     - In `compile_service_release` and `compile_compose_release`: resolve container artifact platform, validate compatibility with `region: home` (requires ARM64) and any `node_architectures` mappings, and inject `kubernetes.io/arch` into Deployment `nodeSelector`.
     - In `compile_compose_release`: sidecar architecture and loop validation:
       - Validate sidecars linked via `network_mode: "service:<target>"`.
       - Check if `target` exists in `compose_services`; raise `LumaError(f"sidecar target service '{target_svc}' does not exist in compose file")` if missing.
       - Check for self-referential sidecars (`target_svc == svc_name`); raise `LumaError(f"service '{svc_name}' cannot declare network_mode: service:{svc_name}")`.
       - Handle chained sidecars (`A -> B -> C`) by resolving the ultimate root parent so all co-located containers are placed in the root Pod.
       - Extract platform for root parent and all sidecars; if architectures mismatch (e.g. `linux/arm64` vs `linux/amd64`), raise `LumaError(f"cannot co-locate mixed-architecture containers in pod sidecar: {child_name} ({child_arch}) vs {parent_name} ({parent_arch})")`.
   - Feature 6 (Compose Splitting, PVC Volume Mounting & 6 Exposure Modes):
     - Fix PVC volume attachment: attach synthesized PVCs to Pod `spec.template.spec.volumes` and container `volumeMounts` in compiled Deployment manifests!
     - Helper functions: implement `_validate_storage_size`, `_parse_volume_entry`, `_resolve_service_volumes` per `explorer_m3_2/handoff.md`.
     - Support `:ro` read-only flags properly without corrupting mount paths (`readOnly: True`).
     - Support custom sidecar `command` overrides (do not drop `command`).
     - Validate all 6 exposure modes: `cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`, `none`. Raise `LumaError` on invalid exposure mode.
     - Fix `exposure: none`: ensure no Ingress or Gateway Route is created even if `domain` is specified.
     - In `tcp-relay`: ensure Kubernetes Service port is named `"tcp"` (or `"tcp-" + str(port)`) instead of `"http"`.
     - Detect and reject TCP relay port collisions across Compose services.

2. `tests/test_paas_engine.py`:
   - Integrate Suite 3 (`RuntimeArchitectureAffinityAndComposeIntegrityTests` with all 25 unit tests) from `.agents/teamwork/explorer_m3_3/suite_3_test_code.py`.
   - Update legacy test assertion in `test_paas_engine.py` (around line 176–183) where `nodeSelector` now includes `"kubernetes.io/arch": "arm64"`.
   - Run tests using `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`.
   - Run ruff check: `./.venv/bin/python -m ruff check luma tests scripts`.
   - Verify all tests pass with 0 failures and 0 errors.

3. Report:
   - Write comprehensive report to `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m3/handoff.md`.
   - When finished, send a message to parent via send_message with the handoff path.
