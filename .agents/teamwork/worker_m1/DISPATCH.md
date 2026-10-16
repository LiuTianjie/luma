## 2026-10-15T16:30:53Z
You are Worker M1.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/worker_m1
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read the thorough explorer blueprints at:
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m1_1/handoff.md
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m1_2/handoff.md
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m1_3/handoff.md
- /Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT
hardcode test results, create dummy/facade implementations, or
circumvent the intended task. A teamwork_preview_auditor will independently
verify your work. Integrity violations WILL be detected and your
work WILL be rejected.

File Write Ownership (You own these files exclusively):
- `luma/build_capabilities.py`
- `luma/paas/build/controller.py`
- `deploy/paas-stack/tekton/luma-build-pipeline.yaml`
- `tests/test_paas_engine.py` (adding Milestone 1 test cases)

Objective & Tasks:
Implement Milestone 1 (M1: Build & Native Architecture) covering Feature 1 (Native ARM64/AMD64 Builder Enforcement) and Feature 2 (Tekton <3s Build Cancellation & Finally):

1. `luma/build_capabilities.py`:
   - Implement `validate_native_platform(platform: str) -> str`:
     - Reject comma-separated multi-platform strings with `LumaError("cross-architecture emulation and multi-platform compilation are disabled; submit builds separately for each native architecture")`.
     - Normalize architecture aliases (`arm64/v8`, `aarch64` -> `arm64`, `x86_64` -> `amd64`).
     - Validate allowed platforms: `{"linux/arm64", "linux/amd64", "darwin/arm64", "darwin/amd64"}`.
   - Implement `infer_native_platform(*, platform: str = "", target_node: str = "", target_region: str = "", node_architectures: Mapping[str, str] | None = None) -> str`:
     - If `platform` is provided and non-empty: return `validate_native_platform(platform)`.
     - If empty: infer `linux/arm64` if `target_region == "home"`, or if `target_node` in `node_architectures` (translating Darwin host to Linux container arch), or if `target_node in {"macbook-pro", "air3"}` or `"arm64"` in `target_node.lower()`. Otherwise default to `linux/amd64`.
   - Preserve `native_arm_build_platform` unchanged for legacy Nomad compatibility.

2. `luma/paas/build/controller.py`:
   - Update `select_native_builder`:
     - Signature: `(self, platform: str = "", *, preferred_node: str = "", target_node: str = "", target_region: str = "") -> BuilderEndpoint`.
     - Use `infer_native_platform` and `validate_native_platform`.
     - Reject architecture conflicts between explicit `platform` and `target_region` (e.g. `linux/amd64` in `home`) or `target_node` (e.g. `linux/amd64` on `macbook-pro`).
     - Verify `preferred_node` and registered `target_node` match target architecture.
     - Enforce zero QEMU fallback (raise `LumaError` if no ready native builder endpoint matches).
     - Sort matching builders: prefer target_node, then -available_slots, then cache_used_bytes, then name.
   - Update `submit_build`:
     - Accept `target_node: str = ""` and `target_region: str = ""`. Inspect `build_spec` for node/region placement if omitted.
     - In `pipelineRunManifest["spec"]`: inject `podTemplate: {"terminationGracePeriodSeconds": 3}` and `timeouts: {"pipeline": "45m", "tasks": "30m", "finally": "3m"}`.
     - Implement `_claim_slot` and `_release_slot` on `BuildController` using `replace(builder, active_builds=...)` to track concurrency. Claim slot when status is `running`.
   - Update `cancel_build`:
     - Enforce terminal state validation: raise `LumaError` if build run is already in `{"succeeded", "failed", "canceled"}`.
     - Release builder active concurrency slot via `_release_slot` if previously `running`.
   - Implement `complete_build`:
     - Marks build run as `succeeded` or `failed`, releases concurrency slot, blocks double terminal transitions.

3. `deploy/paas-stack/tekton/luma-build-pipeline.yaml`:
   - In `luma-buildkit-build-push` task step script: add signal trapping (`BUILDCTL_PID=$!`, `trap 'kill -TERM ${BUILDCTL_PID} 2>/dev/null || true' TERM INT`, `wait ${BUILDCTL_PID}`, `trap - TERM INT`).
   - Add Task `luma-build-cleanup-finally` cleaning up `/tmp/*.sock`, `/run/buildkit/*.sock`, `${WORKSPACE}/primary`, `${WORKSPACE}/contexts`.
   - Add `finally:` block to `luma-paas-build-pipeline` calling `luma-build-cleanup-finally`.

4. Verification:
   - Add unit tests to `tests/test_paas_engine.py` covering all new functionality as outlined in the explorer blueprints.
   - Run tests using `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`.
   - Verify code compiles and passes without regressions.

5. Report:
   - Write comprehensive report to `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m1/handoff.md`.
   - When finished, send a message to parent via send_message with the handoff path.
