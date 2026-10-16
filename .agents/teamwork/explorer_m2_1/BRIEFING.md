# BRIEFING — 2026-10-15T17:01:00Z

## Mission
Investigate M2 Feature 3: Zot Retention Pins & Obsolete Tag Pruning in `luma/paas/build/artifact_store.py` and `deploy/paas-stack/zot/zot-config.json`.

## 🔒 My Identity
- Archetype: explorer
- Roles: explorer, analyzer, investigator
- Working directory: /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m2_1
- Original parent: 1387c386-51c1-4a2c-b02a-a266794155bd
- Milestone: M2 (Feature 3: Zot Retention Pins & Obsolete Tag Pruning)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement / modify source code.
- Report all file paths, line numbers, concrete findings, and exact remediation steps.
- Write survey report to `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m2_1/handoff.md`.
- Send completion message to parent via send_message.

## Current Parent
- Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd
- Updated: 2026-10-15T17:01:00Z

## Investigation State
- **Explored paths**:
  - `luma/paas/build/artifact_store.py` (lines 1-585)
  - `deploy/paas-stack/zot/zot-config.json` (lines 1-59)
  - `luma/registry_management.py` (lines 1-698)
  - `luma/paas/release/flux_reconciler.py` (lines 1-250)
  - `tests/test_paas_engine.py` (lines 1-791)
  - `docs/build-and-artifacts.md` (lines 30-35)
  - `dashboard-src/src/components/ReleaseTimelinePanel.tsx` (lines 210-220)
  - `TEST_INFRA.md` (lines 43, 71-76, 138-144, 381-453)
- **Key findings**:
  1. Retention tag format in `artifact_store.py:323, 337` is currently inverted (`luma-pin-<proj>-<env>-active` and `...-rb{idx}`) instead of the contract `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v<gen>`.
  2. No obsolete tag pruning exists in `artifact_store.py`; `ArtifactCatalog` lacks `sync_retention_pins` and `protected_pin_tags`; `ZotRegistryClient` lacks `delete_tag` and tag listing.
  3. `ZotRegistryClient.pin_digest` directly calls `validate_repository` without host/port stripping, which fails on URLs like `100.66.177.70:5000/app` or `registry.internal:5050/subpath/app`.
  4. Multi-service Compose stacks sharing digests have an architectural bug: `compute_protected_digests` keys purely on `digest`, losing secondary repositories (e.g. `serverx-worker` if sharing base digest with `serverx-web`).
- **Unexplored areas**: None within Feature 3 scope.

## Key Decisions Made
- Designed comprehensive remediation plan covering:
  - Exact tag renaming logic using release generation.
  - Dual sequence/dict `RetentionSyncResult` for `sync_retention_pins`.
  - Canonical `normalize_repository` implementation and wiring.
  - Per-repository digest tracking and deduplication in `compute_protected_digests` and `sync_retention_pins`.

## Artifact Index
- DISPATCH.md — incoming dispatch records
- BRIEFING.md — persistent state memory
- progress.md — liveness heartbeat
- handoff.md — final comprehensive handoff report
