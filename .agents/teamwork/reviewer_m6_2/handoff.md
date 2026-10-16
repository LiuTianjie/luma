# Handoff Report: Reviewer M6-2 — Final Code & Adversarial Review of Migration, Self-Healing, DevEx, and Documentation

**Reviewer**: Reviewer M6-2 (Reviewer & Adversarial Critic)  
**Parent**: Project Orchestrator (Conversation ID: `1387c386-51c1-4a2c-b02a-a266794155bd`)  
**Project Root**: `/Users/turning4th/infra-stacks`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/reviewer_m6_2`  
**Review Target**: Milestone M6-2 (Migration Engine, Standalone Script, Self-Healing, DevEx, Dashboard, Documentation, and Gate Hardening)  
**Verdict**: **`APPROVE`**  

---

## 1. Observation

### 1.1 Integrity Violation Audit
A strict adversarial audit was performed across all modified and newly created modules for potential integrity violations:
- **Hardcoded test results / expected outputs**: Verified absence in `luma/paas/migration/migrator.py`, `scripts/migrate-nomad-to-k3s.py`, `luma/control/server.py`, `luma/envfile.py`, and `luma/cli/parser.py`.
- **Facade implementations**: Evaluated `MigrationEngine`, `StandaloneMigrator`, and `handle_service_scale`. Confirmed real state mutations via `ArtifactCatalog`, `FluxReconciler.deploy_artifact()`, `_mutate_control_state()`, and authentic manifest compilations rather than no-op stubs.
- **Shortcuts / task bypassing**: Confirmed full 4-step state machine (`plan -> shadow -> cutover -> abort -> finalize`), authentic Batch 2 replica 0 shadow, genuine Batch 3 local volume adoption, 5 pre-cutover safety gates, and authentic MicroOS/Darwin node self-healing checks.
- **Fabricated verification outputs**: All test suites, linters, and verification scripts were run directly from source with real exit code `0` outputs.
- **Self-certifying work**: Independent verification executed across 1,741 repository unit tests, 110 dashboard frontend tests, static site checks, and standalone dry-run migration runner.
- **Result**: Zero integrity violations found.

---

### 1.2 Migration Engine & Standalone Script
1. **Four-Step Zero-Downtime State Machine** (`luma/paas/migration/migrator.py:348-766`):
   - `plan()` (lines 361-456): Parses manifest/compose, normalizes volume node pins via `_normalize_node_pin()`, classifies application into rollout batches via `classify_application_batch()`, saves plan to `self._migrations[project_slug]["plan"]`, and sets state to `"planned"`.
   - `shadow()` (lines 458-626): Adopts running image digests into an immutable `BuildArtifact` (`art-adopted-<app>-...`), invokes `self.reconciler.deploy_artifact()`, deploys K3s HelmRelease while keeping production traffic on Nomad (`trafficTarget: "nomad"`), and verifies gateway health probes.
   - `cutover()` (lines 628-734): Enforces state prerequisite (`shadow_ready` or `aborted`), runs 5 pre-cutover safety gates, safely sequences legacy Nomad stoppage before scaling K3s deployments, updates routing to `k3s-flux`, and retains legacy Nomad in warm `standby`.
   - `abort()` (lines 735-751): Reverts traffic target back to Nomad in `< 1s` (`trafficTarget: "nomad"`), blocks abort if migration is already `finalized`, and allows re-cutover.
   - `finalize()` (lines 752-766): Blocks finalization unless cutover is complete, marks legacy Nomad status as `"purged"`, and confirms `orphanQueueWorkersRemaining = 0`.

2. **Batch 2 Queue Worker Dual-Consumer Race Prevention** (`luma/paas/migration/migrator.py:519-532, 709-713`):
   - During `shadow()`, worker services matching queue keywords (`worker`, `consumer`, `queue`, `job`, `task`, `celery`, `beat`, `cron`) or background services without web ports/domains have `shadow_replicas[svc] = 0`, while web/API deployments run target replicas.
   - During `cutover()`, Nomad workers are drained (`legacyNomadWorkerStatus = "drained"`), and K3s worker Deployments are scaled to target replicas.

3. **Batch 3 Stateful Volume & Single-Instance Bot Isolation** (`luma/paas/migration/migrator.py:433, 515-518, 704-708`):
   - For `batch3-stateful-volumes`, applications declaring adopted volumes, or apps named `kato`/`bot`, `shadow()` assigns `shadow_replicas = {svc: 0 for svc in plan.services}`. Pods are not scheduled, preventing dual Telegram/Feishu bot long-polling conflicts and SQLite WAL/flock concurrent write corruption.
   - In `cutover()`, `record["legacyNomadAllocationStopped"] = True` and timestamp `record["nomadStoppedAt"]` are recorded **strictly before** compiling and scaling the K3s Deployment to `1`.
   - In-place local volume adoption preserves `hostPath` (default `/var/lib/luma/volumes/{project}/{volume}`) and `fsGroup` (default `1000`) without data copy.

4. **All 5 Pre-Cutover Validation Gates** (`luma/paas/migration/migrator.py:641-698`):
   - **Gate 1 (Gateway Probe)**: Validates `rel.gateway_probe_ok is True` and `record["gatewayProbeOk"] is True`; raises `LumaError` on probe failure.
   - **Gate 2 (Volume Node Pin)**: Evaluates `_normalize_node_pin(vol.get("node"))`. Rejects empty string, `None`, `"none"`, `"null"`, and `"~"` with `LumaError`.
   - **Gate 3 (Image Architecture Matching)**: Checks `target_arch in img_platform` (defaulting omitted platforms to `linux/amd64`). Rejects cross-architecture mismatches (e.g. AMD64 image on ARM64 MicroOS node) with `LumaError`.
   - **Gate 4 (Image Digest Drift)**: Compares current running Nomad image digests against shadow image digests. If different, raises `LumaError`.
   - **Gate 5 (Secret Parity)**: Compares shadow secrets against expected secrets; raises `LumaError` on mismatch or length difference.

5. **openSUSE MicroOS & Darwin Node Self-Healing** (`luma/paas/migration/migrator.py:198-336`, `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`):
   - MicroOS: Detects transactional read-only rootfs (`read_only_rootfs: True`), validates writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`), executes SELinux `restorecon` on `registries.yaml`, and runs `btrfs scrub start -B /var` and `fstrim -v /var` on disk usage >= 75% or first boot (`last_fstrim_at == 0`). Auto-recovers malformed syntax or semantic errors in `registries.yaml`.
   - Darwin: Aligns `NO_PROXY` with `<local>`, `localhost`, and `127.0.0.1` (rejecting false-positive substring matches such as `127.0.0.100`), and establishes Tailscale mTLS routes to Zot registry (`100.66.177.70:5000`).
   - DaemonSet manifest: Configures privileged container with `hostPID: true`, `hostNetwork: true`, tolerating all taints, mounting `/host/etc`, `/host/var`, and `/host/var/run`, and writing regular heartbeats.

6. **Standalone One-Time Migration Runner** (`scripts/migrate-nomad-to-k3s.py`):
   - Implements preflight checks: verifies `kubectl`, detects Nomad CLI, verifies memory headroom (`SC_AVPHYS_PAGES`, >= 512 MB required to avoid OOM killer), checks storage volume paths, and confirms Traefik port coexistence without duplicate hostPort 80/443.
   - Supports `--dry-run`, `--batch [1, 2, 3, all]`, `--app` / `--project`, `--state-file`, and `--decommission-nomad` (`systemctl disable --now nomad`).

---

### 1.3 DevEx, Dashboard, and Documentation
1. **Control Plane Service Scaling Endpoint** (`luma/control/server.py:7048-7108, 14725`):
   - Endpoint: `POST /v1/services/scale`.
   - Verified authentication via `require_token(..., token_type="deploy")`.
   - Validates `project` presence and bounds check `0 <= replicas <= 64` (raising `LumaError` on non-integers, negative numbers, or > 64).
   - Calls `FluxReconciler.scale_service()` to update `replicas_by_service` for the target service without restarting sibling services in Compose stacks. Persists state via `_mutate_control_state()`.

2. **0-to-1 DevEx & .env Setting Propagation** (`luma/envfile.py:54-62`):
   - `CLOUDFLARE_ZONE_ID` added to `LUMA_SETTING_NAMES` alongside `CLOUDFLARE_API_TOKEN`, `EGRESS_SUBSCRIPTION_URL`, `TAILSCALE_AUTHKEY`, and `TRAEFIK_ACME_EMAIL`.
   - `load_luma_settings()` correctly loads and exports `CLOUDFLARE_ZONE_ID` into the environment while filtering non-Luma application variables.

3. **Clean Public CLI Surface** (`luma/cli/parser.py:22-55, 463-469`):
   - Excluded `migrate` from top-level `COMMAND_GROUPS`.
   - Registered `migrate` subparser with `help=argparse.SUPPRESS`.
   - Tested: `luma --help` hides `migrate` completely; `luma migrate --help` remains fully functional for manual low-level operations.

4. **Dashboard Replica Scaler & Zot Online GC Notices** (`dashboard-src/`):
   - `dashboard-src/src/lifecycleApi.ts`: Exports `scaleService()` and `ServiceScaleResult`.
   - `dashboard-src/src/components/ReleaseTimelinePanel.tsx`: `ServiceReplicaScaler` clamps `[0, 64]`, uses `svc.name || stack` key resolution, dispatches `/v1/services/scale`, and provides instantaneous reactive UI feedback.
   - `dashboard-src/src/pages/RegistryPage.tsx:223, 501`: Replaced obsolete offline GC warning with bilingual Zot Online GC notification confirming unpinned data is reclaimed in the background while the registry remains operational.

5. **Bilingual Documentation Parity** (`docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`):
   - 100% heading anchor parity across all 10 anchors (`{#zero-downtime-migration-guide-nomad-to-k3s-flux-paas}`, `{#one-time-standalone-migration-runner}`, `{#four-step-migration-state-machine}`, `{#rollout-batches-for-existing-applications}`, `{#real-world-migration-hazards-and-safeguards}`, `{#same-node-traefik-port-coexistence-and-memory-headroom}`, `{#stateful-sqlite-flock-volumes-and-single-instance-bot-protection}`, `{#post-migration-nomad-decommission}`, `{#fresh-0-to-1-install-vs-existing-migration-compatibility}`, `{#cli-reference}`).
   - 100% codeblock parity across all 5 code fences.

---

### 1.4 Empirical Verification Commands & Results

| # | Command | Output Summary | Result |
|---|---|---|:---:|
| 1 | `./.venv/bin/python scripts/build-site.py --output scratch/luma-site` | `Built site and 46 documentation pages in scratch/luma-site` | **PASS (0)** |
| 2 | `./.venv/bin/python scripts/check-site.py scratch/luma-site` | `Validated local links and assets across 48 HTML pages; bilingual sections match.` | **PASS (0)** |
| 3 | `./.venv/bin/python scripts/generate-cli-reference.py --check` | 0 modifications required; CLI reference is up-to-date and clean | **PASS (0)** |
| 4 | `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run` | Preflight checks succeeded; planned 10 applications across Batches 1, 2, 3 with 0 errors | **PASS (0)** |
| 5 | `./.venv/bin/python -m unittest tests.test_paas_engine -v` | Ran 122 tests in 0.136s | **PASS (0)** |
| 6 | `npm run typecheck:dashboard` | TypeScript typecheck passed with 0 errors | **PASS (0)** |
| 7 | `node --experimental-strip-types --test tests/dashboard/*.test.mjs` | 41 frontend tests passed | **PASS (0)** |
| 8 | `node --test dashboard-src/tests/*.cjs` | 69 frontend unit tests passed | **PASS (0)** |
| 9 | `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh` | Full repository gate: ruff, validate-stacks, dashboard build, 1741 Python tests, 110 Node tests, git diff check | **PASS (0)** |

---

## 2. Logic Chain

1. **Adversarial Resilience & Invariant Verification**:
   - The user requirements explicitly identified that migration is a one-time operation, requiring clean CLI separation, stateful volume protection, dual-consumer avoidance, and zero breaking changes to 0-to-1 setup.
   - Siding with the user requirement to never dual-poll bots or dual-write SQLite databases, `luma/paas/migration/migrator.py` physically enforces `replicas: 0` during shadow mode for `kato`, `bot`, and any application with local volumes.
   - For Batch 2 queue workers, worker replicas are held at `0` during shadow mode and Nomad workers are drained prior to scaling K3s workers, eliminating dual-consumer message processing.
   - All 5 pre-cutover gates (health probes, volume node pins, architecture matches, image digest drift, secret parity) evaluate fail-closed, raising `LumaError` whenever any check fails.
   - The standalone migration script (`scripts/migrate-nomad-to-k3s.py`) checks memory headroom (>= 512 MB) and Traefik port coexistence to safeguard shared-node stability.
2. **0-to-1 Compatibility & DevEx Verification**:
   - Adding `CLOUDFLARE_ZONE_ID` to `LUMA_SETTING_NAMES` guarantees that users configuring Cloudflare through `.env` do not experience silent setting drops.
   - Hiding `migrate` from public CLI `--help` via `argparse.SUPPRESS` and excluding it from `COMMAND_GROUPS` prevents one-time legacy migration commands from polluting the CLI experience of new open-source adopters.
   - The bilingual documentation and static site pass all anchor and codeblock parity checks (`scripts/check-site.py`).
3. **Control Plane & UI Verification**:
   - The new `/v1/services/scale` endpoint is authenticated, bounds-checked (`0 <= replicas <= 64`), and wired to `FluxReconciler.scale_service()`.
   - The dashboard replica scaler updates individual service replicas without restarting sibling services in multi-service stacks, and the registry deletion alert accurately describes Zot Online GC.
4. **Comprehensive Test Suite & Quality Gate**:
   - Over 1,741 automated unit tests and 110 frontend tests run and pass without a single failure.
   - Linters (`ruff`), formatters, and TypeScript compiler report 0 issues.

---

## 3. Caveats

- **Physical Node Daemons**: Hardware-level Btrfs maintenance and physical `systemctl disable --now nomad` execution were tested in simulated environments and dry-run mode rather than mutative actions on the host machine.
- **External Cloudflare API**: Live HTTP calls to Cloudflare's public API endpoints were verified through unit test mocks and payload format inspection rather than dispatching requests to live DNS zones.
- **No other caveats**: All observations are backed by direct tool outputs and code inspection.

---

## 4. Conclusion

The implementation and hardening of the Migration Engine, Standalone Migration Runner, openSUSE MicroOS/Darwin Self-Healing, DevEx `.env` propagation, Clean CLI surface, Dashboard Replica Scaler, and Bilingual Documentation meet all requirements set forth in `ORIGINAL_REQUEST.md` (R1–R4 and Follow-up).

Zero integrity violations were detected. All verification gates and adversarial test suites passed cleanly with exit code `0`.

**Explicit Verdict**: **`APPROVE`**

---

## 5. Verification Method

To independently verify these findings:

1. **Build and Check Bilingual Static Documentation**:
   ```bash
   ./.venv/bin/python scripts/build-site.py --output scratch/luma-site
   ./.venv/bin/python scripts/check-site.py scratch/luma-site
   ```
   *Expected Output*: Exit code `0`, 48 HTML pages validated, 0 anchor or codeblock mismatches.

2. **Verify CLI Reference & Clean CLI Surface**:
   ```bash
   ./.venv/bin/python scripts/generate-cli-reference.py --check
   ./.venv/bin/luma --help | grep -i migrate || echo "Clean: migrate hidden from public help"
   ./.venv/bin/luma migrate --help
   ```
   *Expected Output*: Exit code `0`, `migrate` is absent from `luma --help`, but functional in `luma migrate --help`.

3. **Verify Standalone Migration Script Dry-Run**:
   ```bash
   ./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run
   ```
   *Expected Output*: Preflight checks pass, plans all 10 applications across 3 batches, exits with code `0`.

4. **Run PaaS Engine Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
   *Expected Output*: 122 tests pass (`OK`).

5. **Run Full Repository Quality Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected Output*: All 1,741 Python tests, 110 Node tests, linters, and typechecks pass with exit code `0`.
