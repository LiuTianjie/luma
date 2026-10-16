## 2026-10-15T19:37:18Z
You are Worker M4-Fix2.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/worker_m4_fix2
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read GATE_STATUS.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/GATE_STATUS.md
Read Challenger M4-R2-2 handoff with bypass failure details:
/Users/turning4th/infra-stacks/.agents/teamwork/challenger_m4_r2_2/handoff.md
Read the three fix blueprints:
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_fix2_1/handoff.md (Gate 2 Node Pin Normalization & Validation)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_fix2_2/handoff.md (Gate 3 Architecture & Gate 4 Drift Normalization)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_fix2_3/handoff.md (Unit Tests for Pre-Cutover Validation Gate Bypasses)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT
hardcode test results, create dummy/facade implementations, or
circumvent the intended task. A teamwork_preview_auditor will independently
verify your work. Integrity violations WILL be detected and your
work WILL be rejected.

File Write Ownership (You own these files exclusively):
- `luma/paas/migration/migrator.py`
- `tests/test_paas_engine.py`

Tasks:
1. `luma/paas/migration/migrator.py`:
   - Failure Mode 1 (Gate 2 Node Pin Normalization):
     - Add helper `_normalize_node_pin(value: Any) -> str`: strips whitespace and returns empty string `""` if value is `None`, `""`, or case-insensitively in `{"none", "null", "~"}`.
     - In `plan()`: use `_normalize_node_pin` to extract volume node pin and manifest node pin. If `not node_name`, correctly emit warning and mark `plan.ready = False`.
     - In `shadow()`: persist normalized `node` pin in `adopted_volumes`.
     - In `cutover()`: check `node_pin = _normalize_node_pin(vol.get("node"))`. If `not node_pin`, raise `LumaError("stateful volume requires explicit node pin")`.
   - Failure Mode 2 (Gate 3 Architecture Match):
     - In `shadow()`: normalize platform for all images in `running_images` (defaulting to `"linux/amd64"` if omitted, None, null, or empty). Persist the resolved `platform` in `record["runningImages"]`.
     - In `cutover()`: default `img_platform` to `"linux/amd64"` if empty, and raise `LumaError("architecture mismatch")` if `target_arch not in img_platform`.
   - Failure Mode 3 (Gate 4 Image Drift Detection):
     - In `shadow()`: persist resolved image digest from artifact catalog in `record["runningImages"]` (never leave `digest: ""`).
     - In `cutover()`: if `current_running_images` supplies a new digest that differs from the resolved `orig_digest`, raise `LumaError("image digest drift detected")`.

2. `tests/test_paas_engine.py`:
   - Add unit tests designed in `explorer_m4_fix2_3/handoff.md`:
     - `test_cutover_rejects_node_none_or_null_stringification`
     - `test_cutover_rejects_architecture_mismatch_when_platform_omitted`
     - `test_cutover_detects_digest_drift_when_initial_digest_omitted`
   - Run tests: `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`.
   - Run ruff check: `./.venv/bin/python -m ruff check luma tests scripts`.
   - Verify all tests pass with 0 failures and 0 errors.

3. Report:
   - Write comprehensive report to `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m4_fix2/handoff.md`.
   - Send completion message to parent via send_message with the handoff path.
