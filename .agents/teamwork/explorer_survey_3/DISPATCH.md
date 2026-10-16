## 2026-10-15T16:03:45Z
You are Explorer Survey 3.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_survey_3
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md

Scope & Objective:
Perform an in-depth codebase survey for Requirement 3 (R3) & Verification: Completeness & Reliability of the 4-Step Zero-Downtime Migration Plan & Engine and Test Gates.
Examine:
- `luma/paas/migration/migrator.py`
- `luma/cli/paas.py`
- `docs/migration-guide*.md`
- `tests/test_paas_engine.py` and other test files
- `scripts/check-luma.sh`, `scripts/build-site.py`, `scripts/check-site.py`

Specifically investigate and map all gaps/bugs/edge cases regarding:
1. 4-step migration workflow (`luma migrate plan -> shadow -> cutover -> abort -> finalize`):
   - Batch 2 Queue Worker Race Prevention (`serverx`, `granary`, `codex-gitea`): preventing dual-consumer queue races during `shadow` by keeping queue/background worker replicas at `0` (or paused) in the shadow K3s release until worker drain/cutover, while probing web/API deployments in shadow mode.
   - Batch 3 Stateful Volume & Secret Preflight (`kato`, `bot`): enforcing strict pre-shadow and pre-cutover validation that stateful volumes have an explicit `node` pin matching the legacy Nomad allocation host, preserve hostPath + `fsGroup`/UID/GID without data copy, verify secret + mounted secret file parity, and verify shadow release readiness (`gateway_probe_ok` and matching image architecture) before `cutover`.
2. Documentation alignment in `docs/migration-guide*.md` and bilingual site requirements.
3. Test suite coverage in `tests/test_paas_engine.py`: what tests currently exist, what tests are needed to verify R1, R2, R3 invariants.
4. Acceptance verification gates: check requirements of `bash scripts/check-luma.sh` and `scripts/check-site.py`.
