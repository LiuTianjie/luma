# BRIEFING — 2026-10-15T20:02:00Z

## Mission
Investigate Bilingual Documentation Parity & CLI Reference Generation for Milestone 5, producing exact edit specifications for docs and CLI reference generators.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigation, synthesis
- Working directory: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3
- Original parent: 1387c386-51c1-4a2c-b02a-a266794155bd
- Milestone: Milestone 5

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Strict bilingual parity: heading structure, anchors, code block count, commands must match 100% between EN and zh-CN
- Verify static site build and check passes (`scripts/build-site.py`, `scripts/check-site.py`, `generate-cli-reference.py --check`)

## Current Parent
- Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd
- Updated: 2026-10-15T20:02:00Z

## Investigation State
- **Explored paths**:
  - `docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`
  - `scripts/migrate-nomad-to-k3s.py`
  - `scripts/generate-cli-reference.py` & `docs/luma-cli-reference*.md`
  - `scripts/build-site.py` & `scripts/check-site.py`
  - `luma/cli/parser.py` (suppression of `migrate`)
  - `luma/paas/migration/migrator.py` (shadow replica 0, cutover stopping Nomad allocation before scale)
  - `tests/test_paas_engine.py` (all 120 tests pass)
  - Full repo unit tests (1697 tests pass)
- **Key findings**:
  1. CLI reference generation already omits `migrate` due to `help=argparse.SUPPRESS` in `luma/cli/parser.py:467`; `scripts/generate-cli-reference.py --check` passes cleanly with exit code 0.
  2. `scripts/migrate-nomad-to-k3s.py` is fully implemented as the canonical standalone runner with `--dry-run`, `--batch [1,2,3,all]`, `--project`/`--app`, `--state-file`, `--decommission-nomad`.
  3. `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md` need expansion to document: standalone runner, real-world hazards (same-node Traefik port coexistence & memory headroom >= 512 MB), stateful SQLite/flock volume isolation & single-instance bot protection (shadow `replicas: 0`, cutover stops Nomad allocation before scaling K3s to 1), post-migration Nomad decommission (`systemctl disable --now nomad`), and 0-to-1 fresh install vs existing migration compatibility.
  4. Sandbox simulation in scratch directory proves that `proposed_migration-guide.md` and `proposed_migration-guide.zh-CN.md` achieve 100% heading anchor match, 100% codeblock byte-for-byte match, and pass `build-site.py` + `check-site.py` with exit code 0.
- **Unexplored areas**: None. Investigation complete.

## Key Decisions Made
- Formatted `proposed_migration-guide.md` and `proposed_migration-guide.zh-CN.md` with explicit `{#anchor-id}` tags on all headings to guarantee 100% anchor parity across languages.
- Mirrored all 5 code blocks byte-for-byte in both English and Simplified Chinese to satisfy `check-site.py` `fences()` assertion.
- Delivered full replacement files in explorer folder for zero-friction handoff to the implementer.

## Artifact Index
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/DISPATCH.md — incoming dispatch instructions
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/BRIEFING.md — persistent state
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/progress.md — heartbeat progress log
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/proposed_migration-guide.md — verified English replacement
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/proposed_migration-guide.zh-CN.md — verified Chinese replacement
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/handoff.md — 5-component handoff report
