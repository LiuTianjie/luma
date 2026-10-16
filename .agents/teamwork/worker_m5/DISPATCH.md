## 2026-10-15T20:05:20Z
You are Worker M5.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/worker_m5
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read TEST_INFRA.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md

Read the 3 Explorer handoff reports:
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_1/handoff.md (0-to-1 DevEx, .env propagation, Cloudflare integration)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_2/handoff.md (Dashboard Frontend, Scaler API, Registry warnings)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/handoff.md (Bilingual Docs, CLI reference generation, Site verification)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/proposed_migration-guide.md
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_3/proposed_migration-guide.zh-CN.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT
hardcode test results, create dummy/facade implementations, or
circumvent the intended task. A teamwork_preview_auditor will independently
verify your work. Integrity violations WILL be detected and your
work WILL be rejected.

File Write Ownership (You own these files exclusively):
- `docs/migration-guide.md`
- `docs/migration-guide.zh-CN.md`
- `luma/envfile.py`
- `luma/control/server.py`
- `dashboard-src/src/lifecycleApi.ts`
- `dashboard-src/src/components/ReleaseTimelinePanel.tsx`
- `dashboard-src/src/pages/RegistryPage.tsx`
- `tests/test_paas_engine.py`

Tasks to Implement:
1. Bilingual Documentation Updates:
   - Apply the verified proposed content from `explorer_m5_3/proposed_migration-guide.md` to `docs/migration-guide.md`.
   - Apply the verified proposed content from `explorer_m5_3/proposed_migration-guide.zh-CN.md` to `docs/migration-guide.zh-CN.md`.
   - Ensure 100% heading anchor parity and code block parity.
   - Run verification:
     `./.venv/bin/python scripts/build-site.py --output scratch/luma-site`
     `./.venv/bin/python scripts/check-site.py scratch/luma-site`
     `./.venv/bin/python scripts/generate-cli-reference.py --check`

2. 0-to-1 DevEx & .env Setting Propagation:
   - In `luma/envfile.py`: Add `"CLOUDFLARE_ZONE_ID"` to `LUMA_SETTING_NAMES` so it is not dropped when loaded from `./.env`.
   - Verify `is_luma_setting("CLOUDFLARE_ZONE_ID")` is True.

3. Control API & Dashboard Replica Scaler:
   - In `luma/control/server.py`:
     - Implement `handle_service_scale(token: str, body: Dict[str, Any]) -> Dict[str, Any]` and register `"/v1/services/scale"` in `routes`.
     - Wire to `FluxReconciler.scale_service` (or gracefully update replicas and release record in state if catalog is available).
   - In `dashboard-src/src/lifecycleApi.ts`:
     - Export `scaleService({ token, project, service, replicas, environment })`.
   - In `dashboard-src/src/components/ReleaseTimelinePanel.tsx`:
     - Wire `ServiceReplicaScaler` to call `scaleService` when a token/context is available while maintaining reactive UI feedback.
     - Use `svc.name || stack` for short service key resolution.
   - In `dashboard-src/src/pages/RegistryPage.tsx`:
     - Remove the obsolete "Registry paused during GC" warning (line ~501) to align with Zot Online GC.

4. Test Verification:
   - Run typecheck and dashboard build:
     `npm run typecheck:dashboard`
     `npm run build:dashboard`
   - Run dashboard tests:
     `node --experimental-strip-types --test tests/dashboard/*.test.mjs`
     `node --test dashboard-src/tests/*.cjs`
   - Run PaaS engine and builder tests:
     `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`
   - Run full repository gate:
     `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh`
