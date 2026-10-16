# BRIEFING — 2026-10-15T16:27:30Z

## Mission
Investigate and specify the exact implementation requirements for native platform validation and inference in `luma/build_capabilities.py` for Milestone 1.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigation, synthesis
- Working directory: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m1_1
- Original parent: 1387c386-51c1-4a2c-b02a-a266794155bd
- Milestone: M1 (Build & Native Architecture)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Enforce single-architecture strings: reject multi-platform/comma strings with exact error: "cross-architecture emulation and multi-platform compilation are disabled; submit builds separately for each native architecture"
- Normalize architecture aliases (`arm64/v8`, `aarch64` -> `arm64`, `x86_64` -> `amd64`)
- Validate target is either `linux/arm64` or `linux/amd64` (or valid `darwin/arm64` / `darwin/amd64` for local evaluation)
- Provide exact code snippets, function signatures, and unit test assertions
- Maintain backwards compatibility for `native_arm_build_platform` while enforcing strict native-only guarantees

## Current Parent
- Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `luma/build_capabilities.py`
  - `luma/paas/build/controller.py`
  - `luma/regions.py`
  - `luma/control/server.py`
  - `luma/agent.py`
  - `tests/test_native_builders.py`
  - `tests/test_paas_engine.py`
- **Key findings**:
  - `native_arm_build_platform` must remain untouched to satisfy existing tests (`test_multiple_arm_architectures_cannot_fall_back_to_emulation`) and legacy Nomad agent/server logic.
  - `BuildController` currently calls `native_arm_build_platform` directly, allowing comma-separated platforms to silently pass by extracting the first ARM platform rather than raising `LumaError`.
  - Defined complete specification and code for `validate_native_platform` and `infer_native_platform` with full alias normalization, strict comma rejection, and topology inference rules (`home` region -> `linux/arm64`, ARM nodes -> `linux/arm64`, default -> `linux/amd64`).
- **Unexplored areas**: None for M1-1 scope.

## Key Decisions Made
- Confirmed dual-function architecture: retain `native_arm_build_platform` for backwards compatibility, introduce `validate_native_platform` and `infer_native_platform` for strict PaaS native-only enforcement.
- Map Darwin host architectures in `infer_native_platform` to container Linux counterparts (`linux/arm64`, `linux/amd64`), while permitting `darwin/*` in `validate_native_platform` for local host evaluation.

## Artifact Index
- `DISPATCH.md` — Initial task dispatch
- `progress.md` — Liveness heartbeat and status
- `BRIEFING.md` — Working memory and context
- `handoff.md` — Full 5-component handoff report
