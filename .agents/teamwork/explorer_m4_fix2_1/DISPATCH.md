## 2026-10-15T19:30:14Z
You are Explorer M4-Fix2-1.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_fix2_1
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read GATE_STATUS.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/GATE_STATUS.md
Read Challenger M4-R2-2 handoff with failure details at:
/Users/turning4th/infra-stacks/.agents/teamwork/challenger_m4_r2_2/handoff.md

Objective:
Investigate and design the exact fix for Gate 2 Node Pin Bypass (Failure Mode 1):
In `luma/paas/migration/migrator.py`:
- In `plan()`, `node_name = str(vol.get("node") if "node" in vol else ...)` causes `"node": None` or `node: null` to become the literal string `"None"`.
- `if not node_name:` evaluates to False, allowing unpinned stateful volumes to be marked `ready: True`.
- In `cutover()`, `vol.get("node")` is `"None"`, which is truthy, completely bypassing the required explicit node pin check!
Design the complete remediation for `plan()` and `cutover()` so:
- `node` values of `None`, `""`, `"None"`, `"null"` are all treated as empty/unpinned.
- Stateful volumes without an explicit valid node pin trigger plan warnings and raise `LumaError("stateful volume requires explicit node pin")` during `cutover()`.
- Provide exact lines and code diffs.

Constraints:
- You are read-only. DO NOT modify any code.
- Write your report to `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_fix2_1/handoff.md`.
- Send a completion message to parent.
