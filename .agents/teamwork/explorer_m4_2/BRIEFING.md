# BRIEFING — 2026-10-15T18:37:00Z

## Mission
Investigate Milestone 4 Feature 8: 4-Step Zero-Downtime Migration Engine Hardening (`migrator.py`) and Standalone Migration Script (`scripts/migrate-nomad-to-k3s.py`), with strict SQLite/flock isolation, queue worker race prevention, same-node port conflict handling, and pre-cutover validation gates.

## 🔒 My Identity
- Archetype: Explorer
- Roles: Read-only investigation, analysis, synthesis
- Working directory: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2
- Original parent: 1387c386-51c1-4a2c-b02a-a266794155bd
- Milestone: Milestone 4 (M4: Node Self-Healing & Zero-Downtime Migration) - Feature 8

## 🔒 Key Constraints
- Read-only investigation — do NOT implement / modify project code
- Never simultaneously mount and write the same hostPath volume from both Nomad and K3s (prevents SQLite WAL / flock corruption)
- Never run dual bot long-polling connections
- During shadow: stateful/exclusive-lock apps must keep replicas: 0; worker replicas: 0
- During cutover: stop Nomad allocation before scaling K3s Deployment to 1 for stateful/bot apps; drain Nomad workers before scaling K3s workers
- Report exact file paths, line numbers, concrete findings, method signatures, diffs, and verification steps

## Current Parent
- Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd
- Updated: 2026-10-15T18:37:00Z

## Investigation State
- **Explored paths**:
  - `luma/paas/migration/migrator.py` (lines 1-384)
  - `luma/paas/release/flux_reconciler.py` (lines 1-155)
  - `luma/paas/release/helm_compiler.py` (lines 565-650, 800-850)
  - `luma/cli/paas.py` (lines 1-355)
  - `luma/cli/parser.py` (lines 1-140, 460-492)
  - `tests/test_paas_engine.py` (lines 400-480, 2000-2085)
  - `TEST_INFRA.md` (lines 106-256, 690-835)
  - `docs/migration-guide*.md`
  - `scripts/check-luma.sh`, `scripts/generate-cli-reference.py`
- **Key findings**:
  - `MigrationEngine.get_compiled_release` is missing, causing Suite 4 tests to raise AttributeError.
  - `shadow()` missing `existing_volumes`, `gateway_probe_ok`, `gateway_probe`, `replicas_override` parameters.
  - Batch 2 shadow must set queue worker replicas to 0 while web runs at target replicas; cutover must drain Nomad workers before scaling K3s workers.
  - Batch 3 stateful apps (`kato`, `bot`) must have replicas: 0 during shadow; cutover must stop Nomad allocation BEFORE scaling K3s Deployment to 1.
  - Pre-cutover validation gates for gateway health probe (`gateway_probe_ok is True`) and stateful volume node pin must raise `LumaError`.
  - Standalone script `scripts/migrate-nomad-to-k3s.py` designed with preflight diagnostics, batch orchestration, state persistence, and Nomad decommissioning (`systemctl disable --now nomad`).
  - CLI parser cleaned up: `migrate` removed from public `COMMAND_GROUPS` and suppressed in help, keeping CLI clean while fully backward-compatible.
- **Unexplored areas**: None for Feature 8. Ready for implementation by Worker M4.

## Key Decisions Made
- Fully specified method signatures, diffs, standalone script design, and verification gates in `handoff.md`.

## Artifact Index
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2/DISPATCH.md — Dispatch log
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2/progress.md — Liveness heartbeat
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2/handoff.md — Final handoff report
