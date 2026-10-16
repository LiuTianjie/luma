# Handoff Report: Milestone 5 (Worker M5) — Final Polish, Bilingual Docs, DevEx & Replica Scaler

**Worker**: Worker M5  
**Parent**: Project Orchestrator (Conversation ID: `1387c386-51c1-4a2c-b02a-a266794155bd`)  
**Project Root**: `/Users/turning4th/infra-stacks`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m5`  
**Status**: Task Complete (Hard Handoff)  

---

## 1. Observation

### 1.1 Documentation Updates (`docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`)
- **Initial State**:
  The existing `docs/migration-guide.md` (56 lines) and `docs/migration-guide.zh-CN.md` (56 lines) covered the basic 4-step state machine (`plan -> shadow -> cutover -> finalize`) and rollout batches, but omitted:
  1. The standalone one-time migration runner (`scripts/migrate-nomad-to-k3s.py`).
  2. The rationale for one-time migration and hiding `luma migrate` from public `--help` via `argparse.SUPPRESS`.
  3. Real-world coexistence hazards: same-node Traefik dynamic routing avoiding duplicate `hostPort: 80/443`, host memory headroom check (>= 512 MB free RAM), stateful SQLite / `flock` volume lock corruption risks (`kato`, `bot`), single-instance bot polling conflict avoidance (shadow keeping `replicas: 0`, stopping Nomad before scaling K3s to 1), and Nomad decommissioning (`sudo systemctl disable --now nomad`).
  4. 0-to-1 fresh installation experience (`luma bootstrap`, `luma init`, `luma manager cloudflare`) and `.env` backward compatibility.
- **Applied Modifications**:
  - Replaced `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md` with the verified contents from `explorer_m5_3/proposed_migration-guide.md` and `explorer_m5_3/proposed_migration-guide.zh-CN.md`.
  - Maintained 100% heading anchor parity (`{#zero-downtime-migration-guide-nomad-to-k3s-flux-paas}`, `{#one-time-standalone-migration-runner}`, `{#four-step-migration-state-machine}`, `{#rollout-batches-for-existing-applications}`, `{#real-world-migration-hazards-and-safeguards}`, `{#same-node-traefik-port-coexistence-and-memory-headroom}`, `{#stateful-sqlite-flock-volumes-and-single-instance-bot-protection}`, `{#post-migration-nomad-decommission}`, `{#fresh-0-to-1-install-vs-existing-migration-compatibility}`, `{#cli-reference}`).
  - Maintained 100% codeblock parity across all 5 code fences.
- **Validation**:
  Executed:
  ```bash
  ./.venv/bin/python scripts/build-site.py --output scratch/luma-site
  ./.venv/bin/python scripts/check-site.py scratch/luma-site
  ./.venv/bin/python scripts/generate-cli-reference.py --check
  ```
  Output:
  `Built site and 46 documentation pages in /Users/turning4th/infra-stacks/scratch/luma-site`
  `Validated local links and assets across 48 HTML pages; bilingual sections match.`
  All three commands exited with code `0`.

### 1.2 0-to-1 DevEx & .env Setting Propagation (`luma/envfile.py`)
- **Initial State**:
  In `luma/envfile.py` lines 54–56:
  ```python
  LUMA_SETTING_NAMES = frozenset(
      {"CLOUDFLARE_API_TOKEN", "EGRESS_SUBSCRIPTION_URL", "TAILSCALE_AUTHKEY", "TRAEFIK_ACME_EMAIL"}
  )
  ```
  `CLOUDFLARE_ZONE_ID` was missing from `LUMA_SETTING_NAMES`, causing `load_luma_settings` to drop `CLOUDFLARE_ZONE_ID` when specified in `./.env`.
- **Applied Modifications**:
  In `luma/envfile.py`, added `"CLOUDFLARE_ZONE_ID"` to `LUMA_SETTING_NAMES`:
  ```python
  LUMA_SETTING_NAMES = frozenset(
      {
          "CLOUDFLARE_API_TOKEN",
          "CLOUDFLARE_ZONE_ID",
          "EGRESS_SUBSCRIPTION_URL",
          "TAILSCALE_AUTHKEY",
          "TRAEFIK_ACME_EMAIL",
      }
  )
  ```
- **Validation**:
  Executed:
  `./.venv/bin/python -c 'from luma.envfile import is_luma_setting; assert is_luma_setting("CLOUDFLARE_ZONE_ID") is True; print("CLOUDFLARE_ZONE_ID verified:", is_luma_setting("CLOUDFLARE_ZONE_ID"))'`
  Output: `CLOUDFLARE_ZONE_ID verified: True`.

### 1.3 Control API & Dashboard Replica Scaler (`luma/control/server.py`, `dashboard-src/src/lifecycleApi.ts`, `dashboard-src/src/components/ReleaseTimelinePanel.tsx`, `dashboard-src/src/pages/RegistryPage.tsx`)
- **Initial State**:
  1. `luma/control/server.py` had no `/v1/services/scale` endpoint.
  2. `dashboard-src/src/lifecycleApi.ts` had no `scaleService` export.
  3. `dashboard-src/src/components/ReleaseTimelinePanel.tsx`'s `ServiceReplicaScaler` only modified local React state without network dispatch, and resolved keys using `svc.fullName || svc.name || stack`.
  4. `dashboard-src/src/pages/RegistryPage.tsx` contained an obsolete warning: `"此操作不创建备份。删除与回收期间 Registry 暂停服务，无法推送或拉取镜像。"` which contradicts Zot Online GC.
- **Applied Modifications**:
  1. In `luma/control/server.py`:
     - Implemented `handle_service_scale(token: str, body: Dict[str, Any]) -> Dict[str, Any]` (lines 7048–7110), validating `project` presence, `replicas` integer bounds `[0, 64]`, wiring to `FluxReconciler.scale_service` with `_mutate_control_state`, and providing graceful status fallback if no PaaS release is found.
     - Registered `"/v1/services/scale": handle_service_scale` in the `routes` dictionary (line 14786).
  2. In `dashboard-src/src/lifecycleApi.ts`:
     - Exported `scaleService` and `ServiceScaleResult` calling `apiPost<ServiceScaleResult>("/v1/services/scale", token, { project, service, replicas, environment })`.
  3. In `dashboard-src/src/components/ReleaseTimelinePanel.tsx`:
     - Imported `scaleService` from `../lifecycleApi`.
     - Added optional `token?: string` and `onScaled?: (service: string, replicas: number) => void` to `ServiceReplicaScaler` props with fallback token resolution via `localStorage.getItem("luma.dashboard.deployToken")`.
     - Changed key resolution to `const key = svc.name || stack` for Compose short service name compatibility.
     - Called `scaleService` on replica adjustment while maintaining instant reactive UI feedback.
  4. In `dashboard-src/src/pages/RegistryPage.tsx`:
     - Updated deletion alert on line 501 to:
       `{zh ? "此操作不创建备份。未加锁的镜像数据由 Zot 在线垃圾回收（Online GC）自动释放，期间 Registry 保持正常运行。" : "No backup is created. Unpinned image data is reclaimed via Zot Online GC while the registry remains fully operational."}`
     - Updated comment on line 223 to reflect Zot Online GC.
- **Validation**:
  - `npm run typecheck:dashboard`: Exited with code `0`, 0 errors.
  - `npm run build:dashboard`: Built assets into `luma/assets/dashboard/` in 3.73s, exited with code `0`.
  - `node --experimental-strip-types --test tests/dashboard/*.test.mjs`: 41 tests passed (0 failures).
  - `node --test dashboard-src/tests/*.cjs`: 69 tests passed (0 failures).

### 1.4 Automated Test Suite Enhancements (`tests/test_paas_engine.py`)
- Added two comprehensive automated tests to `PaaSEngineTests` in `tests/test_paas_engine.py`:
  1. `test_cloudflare_zone_id_env_propagation`: Verifies `is_luma_setting("CLOUDFLARE_ZONE_ID") is True`, loads a temporary `.env` containing `CLOUDFLARE_ZONE_ID` and `CLOUDFLARE_API_TOKEN`, verifies both are propagated into `os.environ`, and verifies non-Luma variables are not leaked into the environment.
  2. `test_control_handle_service_scale_endpoint`: Verifies input validation on `/v1/services/scale` (missing project raises `LumaError`, negative replicas or >64 raises `LumaError`, non-integer raises `LumaError`), tests graceful fallback when no PaaS release exists, and tests live scaling on an active Compose release where `replicas_by_service` updates to the target count while peer services retain their replicas.
- **Validation**:
  - `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`: Ran 147 tests in 1.434s -> **OK**.
  - `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh`: Full gate including linters, typecheck, dashboard builds, Node unit tests, and Python test suites passed with exit code `0`.

---

## 2. Logic Chain

1. **User Requirement (Feedback & R3/R4)**:
   The operator specified:
   - Migration is a one-time operation. It must be self-contained (`scripts/migrate-nomad-to-k3s.py`), excluded from public `luma --help` via `argparse.SUPPRESS` to prevent cluttering daily CLI workflows, and must explicitly document real-world coexistence hazards (Traefik port collision avoidance, memory headroom preflight, SQLite/flock single-instance lock safety, and Nomad decommissioning).
   - 0-to-1 open-source developer setup must have zero breaking changes, and `.env` settings (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, `EGRESS_SUBSCRIPTION_URL`, `LUMA_DEPLOY_TOKEN`) must load seamlessly.
   - Cloudflare DNS and Tunnels must require zero manual CRDs.
   - Dashboard replica scaling must be genuinely wired to the Control plane / Flux reconciler.
   - Zot Online GC must not claim the registry pauses during garbage collection.
2. **Implementation Verification**:
   - Applying `proposed_migration-guide.md` and `proposed_migration-guide.zh-CN.md` satisfied all documentation requirements with 100% heading anchor and codeblock parity verified by `scripts/check-site.py`.
   - Adding `CLOUDFLARE_ZONE_ID` to `LUMA_SETTING_NAMES` in `luma/envfile.py` guarantees `.env` configurations are loaded transparently without manual shell exports.
   - Implementing `handle_service_scale` in `luma/control/server.py`, exporting `scaleService` in `dashboard-src/src/lifecycleApi.ts`, and connecting `ServiceReplicaScaler` in `dashboard-src/src/components/ReleaseTimelinePanel.tsx` completes the end-to-end API path for per-service replica scaling.
   - Updating `dashboard-src/src/pages/RegistryPage.tsx` aligns the web UI with Zot's zero-downtime Online GC design.
   - Adding unit tests in `tests/test_paas_engine.py` guarantees regression prevention.

---

## 3. Caveats

- **Scope Boundary Compliance**: All modifications were strictly confined to the 8 files explicitly owned by Worker M5 (`docs/migration-guide.md`, `docs/migration-guide.zh-CN.md`, `luma/envfile.py`, `luma/control/server.py`, `dashboard-src/src/lifecycleApi.ts`, `dashboard-src/src/components/ReleaseTimelinePanel.tsx`, `dashboard-src/src/pages/RegistryPage.tsx`, `tests/test_paas_engine.py`).
- **Offline Cloudflare Integration**: Direct HTTP calls to `api.cloudflare.com` require live Cloudflare API credentials; automated tests verify configuration loading and payload structure offline.
- **No other caveats**: All observations are backed by direct tool execution and passing automated gates.

---

## 4. Conclusion

Milestone 5 implementation and verification is 100% complete:
- 0-to-1 setup compatibility and `.env` setting propagation (`CLOUDFLARE_ZONE_ID`) verified.
- Bilingual migration documentation updated with 100% anchor parity and codeblock parity, passing `check-site.py`.
- One-time migration commands remain cleanly suppressed from public CLI help and reference generation (`generate-cli-reference.py --check`).
- Dashboard replica scaler wired to Control API `/v1/services/scale` and Flux reconciler.
- Obsolete offline registry GC warning removed in favor of Zot Online GC notices.
- All 147 Python tests, 110 Node dashboard tests, TypeScript typechecks, and the full repository gate (`scripts/check-luma.sh`) pass with exit code `0`.

---

## 5. Verification Method

To independently verify these conclusions:

1. **Bilingual Documentation & CLI Reference Check**:
   ```bash
   ./.venv/bin/python scripts/build-site.py --output scratch/luma-site
   ./.venv/bin/python scripts/check-site.py scratch/luma-site
   ./.venv/bin/python scripts/generate-cli-reference.py --check
   ```
   *Expected Output*: Exit code `0`, 48 HTML pages validated, 0 anchor or codeblock mismatches.

2. **0-to-1 DevEx .env Setting Check**:
   ```bash
   ./.venv/bin/python -c 'from luma.envfile import is_luma_setting; assert is_luma_setting("CLOUDFLARE_ZONE_ID") is True; print("OK")'
   ```
   *Expected Output*: `OK` with exit code `0`.

3. **Dashboard Build & Typecheck**:
   ```bash
   npm run typecheck:dashboard
   npm run build:dashboard
   ```
   *Expected Output*: Exit code `0`, 0 TypeScript compiler errors.

4. **Dashboard Node Tests**:
   ```bash
   node --experimental-strip-types --test tests/dashboard/*.test.mjs
   node --test dashboard-src/tests/*.cjs
   ```
   *Expected Output*: 110 tests pass (41 + 69 = 110), 0 failures.

5. **PaaS Engine and Native Builder Test Suites**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected Output*: 147 tests pass (`OK`).

6. **Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected Output*: Clean pass across all linters, TypeScript checks, and test suites with exit code `0`.

7. **Invalidation Conditions**:
   - Any failure in `check-site.py`, `generate-cli-reference.py --check`, or `scripts/check-luma.sh`.
   - Any return of `False` from `is_luma_setting("CLOUDFLARE_ZONE_ID")`.
   - Any failure of `/v1/services/scale` to scale an active PaaS release.
