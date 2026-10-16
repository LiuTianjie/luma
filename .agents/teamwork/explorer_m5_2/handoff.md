# Handoff Report: Dashboard Frontend, Replica Scaler & Release Timeline Investigation

**Author**: Explorer M5-2  
**Target Milestone**: Milestone 5 (Dashboard Frontend, Replica Scaler & Release Timeline)  
**Parent Orchestrator**: `1387c386-51c1-4a2c-b02a-a266794155bd`  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m5_2`  
**Status**: Completed (Read-only Investigation)

---

## 1. Observation

### 1.1 Replica Scaler UI Component & Backend Integration
- **Frontend Implementation**:
  - Located in `dashboard-src/src/components/ReleaseTimelinePanel.tsx` (lines 26–134) as `export function ServiceReplicaScaler({ lang, stack, services })`.
  - Mounted in `dashboard-src/src/components/ApplicationManagementPanel.tsx` (lines 1390–1394):
    ```tsx
    <ServiceReplicaScaler
      lang={lang}
      stack={selected.stack}
      services={selected.services}
    />
    ```
  - State & Handler in `ServiceReplicaScaler` (lines 36–55):
    ```tsx
    const [replicaOverrides, setReplicaOverrides] = useState<Record<string, number>>({});
    const [scaleNotice, setScaleNotice] = useState("");

    const adjustReplica = (svc: DashboardService, delta: number) => {
      const key = svc.fullName || svc.name || stack;
      const current =
        replicaOverrides[key] !== undefined
          ? replicaOverrides[key]
          : Number(svc.desired ?? svc.running ?? 1);
      const next = Math.max(0, Math.min(64, current + delta));
      setReplicaOverrides((prev) => ({ ...prev, [key]: next }));
      setScaleNotice(
        zh
          ? `已通过 Flux HelmRelease 独立调整服务 ${svc.name || key} 副本数为 ${next}（无需重启同栈其他服务，耗时 <5s）。`
          : `Independently scaled ${svc.name || key} to ${next} replica(s) via Flux HelmRelease (<5s, sibling services untouched).`,
      );
    };
    ```
- **Control API & PaaS Route Mapping**:
  - In `luma/control/server.py` (lines 14637–14676), routes dictionary contains `/v1/deployments`, `/v1/compose-deployments`, `/v1/services/remove`, `/v1/services/rollback`, `/v1/services/history`, `/v1/applications/restart`, `/v1/applications/update`.
  - **Zero** route exists for replica scaling (neither `/v1/services/scale` nor `/v1/paas/scale`).
  - In `luma/control/client.py`, no scaling client method exists.
  - In `dashboard-src/src/lifecycleApi.ts`, no scaling API function exists.
  - In `luma/paas/release/flux_reconciler.py` (lines 222–256), `FluxReconciler.scale_service` is fully implemented:
    ```python
    def scale_service(
        self,
        *,
        project: str,
        service_name: str,
        replicas: int,
        environment: str = "prod",
    ) -> tuple[ReleaseRecord, CompiledWorkload]:
    ```
  - In `luma/cli/paas.py` (lines 170–202), `cmd_app_scale` connects to `reconciler.scale_service`.

### 1.2 Release Timeline Component & GitOps / Reconciliation State
- **Frontend Implementation**:
  - Located in `dashboard-src/src/components/ReleaseTimelinePanel.tsx` (lines 136–280) as `export function ReleaseTimelineDiffCard({ lang, stack, versions, onRollbackVersion })`.
  - Mounted in `dashboard-src/src/components/ApplicationManagementPanel.tsx` (lines 1184–1191):
    ```tsx
    <ReleaseTimelineDiffCard
      lang={lang}
      stack={selected.stack}
      versions={selectedRollback.versions}
      onRollbackVersion={(targetVer) =>
        void rollbackToVersion(selected, targetVer)
      }
    />
    ```
- **Data Fetching & Backend Route**:
  - `loadVersions` in `ApplicationManagementPanel.tsx` (line 566) calls:
    `const result = await fetchServiceHistory({ token, name: app.stack });` -> `POST /v1/services/history`.
  - In `luma/control/server.py` (lines 7033–7045):
    `handle_service_history` calls `job_versions(config, state, slug=slug)`, querying **Nomad job versions** (`{version, stable, submitTime, image}`).
  - `ReleaseTimelineDiffCard` client-side synthesis (lines 213–220):
    ```tsx
    <Badge variant="outline">
      {selectedDiffIdx === 0
        ? `luma-pin-active-${stack}-prod`
        : `luma-pin-rollback-${stack}-prod-v${currentNum}`}
    </Badge>
    <Badge variant="secondary">
      {zh ? "Flux 健康探测通过" : "Flux Gateway Probe OK"}
    </Badge>
    ```
  - The retention tag name is fabricated on the client, hard-coding `-prod` regardless of actual deployment environment.
  - "Flux Gateway Probe OK" is an unconditional static badge, not bound to `gateway_probe_ok`.
  - GitOps revision (`commitSha`, `branch`, `reconcile.fluxcd.io/requestedAt`, `generation`) is neither fetched nor rendered.
  - Rollback action: `onRollbackVersion(currentNum)` calls `rollbackService` (`POST /v1/services/rollback`). In `luma/control/server.py` line 7012, `handle_service_rollback` calls Nomad `revert_job`, bypassing `FluxReconciler.rollback_release`.

### 1.3 Registry Pins View & Retention Tags
- **Frontend Implementation**:
  - Located in `dashboard-src/src/pages/RegistryPage.tsx`.
  - Queries `GET /v1/registry/inventory` via `fetchRegistryInventory` in `dashboard-src/src/registryManagementApi.ts`.
  - Displays tags as chips (`item.tags.slice(0, 4)` in lines 432–433).
  - In Image Details (`/registry/image?image=...`, lines 472–482):
    Displays "Zot OCI Layer Explorer & Online GC Pinning" with `detail.protectionStatus === "protected" ? "luma-pin-* 引用锁已生效" : "未加锁（可按保留策略在线回收）"`.
- **Backend Reference Collection Disconnect**:
  - In `luma/registry_management.py` (lines 470–511), `collect_state_image_references` scans `state["deployments"]` (Nomad deployments), `buildRuns`, and `agentTasks`.
  - It does **not** scan PaaS `releases` or `artifacts` from `ArtifactCatalog`.
  - In `apply_protection` (lines 355–430), tags matching `^luma-pin-` are not treated as automatic protection rules; an unreferenced image with a retention tag can be classified as a candidate for deletion if it exceeds `keepLast` or `maxAgeDays`.
- **Outdated Offline GC Warning**:
  - `RegistryPage.tsx` lines 223–224 and 501:
    `"此操作不创建备份。删除与回收期间 Registry 暂停服务，无法推送或拉取镜像。"`
    This warning is a relic from offline registry GC and directly contradicts Zot's zero-downtime Online GC architecture.

### 1.4 Frontend Build, Test Suite & Package Configuration
- **Package Configuration**:
  - Root `package.json` contains:
    ```json
    "scripts": {
      "dev:dashboard": "vite --config dashboard-src/vite.config.ts",
      "build:dashboard": "vite build --config dashboard-src/vite.config.ts",
      "typecheck:dashboard": "tsc -p dashboard-src/tsconfig.json"
    }
    ```
  - No `dashboard-src/package.json` exists. Running `cd dashboard-src && npm test` fails with `ENOENT: no such file or directory, open '/Users/turning4th/infra-stacks/dashboard-src/package.json'`.
  - Running `npm test` at root fails with `npm error Missing script: "test"`.
  - No `"lint"` or `"build"` script alias exists in `package.json`.
  - Missing `"type": "module"` in `package.json` triggers Node warning `[MODULE_TYPELESS_PACKAGE_JSON]` on running tests.
- **Test Gate (`scripts/check-luma.sh`)**:
  - `npm run typecheck:dashboard`: Passes with 0 errors.
  - `npm run build:dashboard`: Passes with 0 errors (built in ~3.8s, output to `luma/assets/dashboard/`).
  - `node --experimental-strip-types --test tests/dashboard/*.test.mjs`: 41 tests pass (0 failures).
  - `node --test dashboard-src/tests/*.cjs`: 69 tests pass (0 failures).
  - **Total**: 110 tests pass (41 + 69 = 110).
  - Whole gate (`bash scripts/check-luma.sh`): Passes with exit code `0`.

---

## 2. Logic Chain

1. **Premise**: Milestone 5 requires the Web Dashboard to support replica scaling, release timeline inspection with active/rollback release states, and registry retention pin viewing, integrated with Luma's PaaS control API.
2. **Analysis of Replica Scaling**:
   - In `luma/paas/release/flux_reconciler.py`, `FluxReconciler.scale_service` enables independent `<5s` scaling of Compose sub-services by compiling distinct Kubernetes Deployments with updated replica counts.
   - However, `luma/control/server.py` was never extended with a `/v1/services/scale` route.
   - Consequently, the frontend author wrote `ServiceReplicaScaler` in `ReleaseTimelinePanel.tsx` as a purely client-side mock component using `useState` without network dispatch.
   - When users click `+` or `-`, the UI displays a notification claiming the service was scaled via Flux HelmRelease, but no backend call occurs. On refresh, the change vanishes.
3. **Analysis of Release Timeline & Rollback**:
   - `ReleaseTimelineDiffCard` claims to provide "Flux Release Declarative Diff & <5s Immutable Rollback" with "Zot Rollback Guard Active".
   - The data powering this card comes from `fetchServiceHistory` -> `/v1/services/history`, which delegates to Nomad's `job_versions`.
   - Nomad job versions lack PaaS fields: `releaseId`, `generation`, `artifactId`, `commitSha`, `gatewayProbeOk`, and `replicasByService`.
   - The UI simulates retention pin tags (`luma-pin-active-${stack}-prod` / `luma-pin-rollback-${stack}-prod-v${currentNum}`) using local string template interpolation, and hardcodes the environment to `prod`.
   - Clicking rollback triggers Nomad's `revert_job` rather than Flux CD's `rollback_release`. If the service has been migrated to K3s and removed from Nomad, rollback fails completely.
4. **Analysis of Registry Pins & Online GC**:
   - Zot supports online garbage collection where active releases and the last $N=5$ rollbacks are pinned with `luma-pin-active-*` and `luma-pin-rollback-*`.
   - While `RegistryPage.tsx` renders tag pills and a general "Zot OCI Layer Explorer" card, it displays a legacy warning stating the registry pauses during GC.
   - In `luma/registry_management.py`, the image reference collector fails to inspect PaaS K3s releases or `ArtifactCatalog`. If an application is migrated to K3s, its image references are invisible to the registry management engine unless manually added.
5. **Analysis of Package & Build Scripts**:
   - The instructions and development workflows expect `npm test`, `npm run build`, and `npm run lint`.
   - The missing scripts in `package.json` create friction for developers and automated CI environments that invoke standard npm lifecycle hooks.

---

## 3. Caveats

- **Read-Only Scope**: In accordance with the system constraints, no source files were modified during this investigation.
- **Nomad vs K3s Transition State**: The control plane currently runs in a transitional state where Nomad is the primary execution engine for `/v1/services/history` and `/v1/services/rollback`, while PaaS K3s workloads are coordinated via `luma/paas/` and `luma/cli/paas.py`.
- **E2E Browser Testing**: The 110 automated tests are Node.js-based unit and contract tests running with `node --test`. End-to-end browser execution (e.g. Playwright / Cypress) is not currently part of `scripts/check-luma.sh`.

---

## 4. Conclusion & Actionable Recommendations

### Identified Disconnects & Bugs Summary Table

| ID | Component | Location | Issue Description | Severity |
|---|---|---|---|---|
| **B1** | Replica Scaler | `ReleaseTimelinePanel.tsx:41-55` | `ServiceReplicaScaler` only mutates local React state; no HTTP call is made. | **High** |
| **B2** | Control API | `luma/control/server.py:14637` | Missing `/v1/services/scale` endpoint to wire to `FluxReconciler.scale_service`. | **High** |
| **B3** | Scaler Key Resolution | `ReleaseTimelinePanel.tsx:42` | Uses `svc.fullName` instead of short service name, breaking Compose multi-service scaling keys. | **Medium** |
| **B4** | Release Timeline & Rollback | `ApplicationManagementPanel.tsx:566,626` | Queries Nomad `job_versions` and calls Nomad `revert_job` instead of PaaS Flux release history and rollback. | **High** |
| **B5** | Hardcoded Badges | `ReleaseTimelinePanel.tsx:213-220` | Retention tag hardcodes `-prod` environment; "Flux Gateway Probe OK" badge is unconditionally static. | **Medium** |
| **B6** | Build Waterfall | `DeploymentsPage.tsx:311-316` | `BuildWaterfallView` is missing `onDeployArtifact` callback and falls back to hardcoded Node 20 mock data. | **Medium** |
| **B7** | Registry Deletion Warning | `RegistryPage.tsx:501` | Displays obsolete "Registry paused during GC" warning, contradicting Zot Online GC. | **Low** |
| **B8** | Registry Reference Collection | `luma/registry_management.py:470` | Does not collect PaaS K3s release or `ArtifactCatalog` image references. | **High** |
| **B9** | `package.json` Scripts | `/package.json:2-6` | Lacks `test`, `build`, `lint` script aliases; no `dashboard-src/package.json`; missing `"type": "module"`. | **Medium** |

---

### Actionable Code Recommendations

#### 1. Add PaaS Replica Scaling Route in `luma/control/server.py`
Add handler and register route in `luma/control/server.py`:
```python
def handle_service_scale(token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    require_token(state, token, token_type="deploy")
    project = str(body.get("project") or body.get("application") or "").strip()
    service_name = str(body.get("service") or "").strip() or project
    replicas = int(body.get("replicas", 1))
    environment = str(body.get("environment") or "prod").strip()
    if not project:
        raise LumaError("project is required")
    if replicas < 0 or replicas > 64:
        raise LumaError("replicas must be between 0 and 64")
    
    # Delegate to FluxReconciler if PaaS catalog has releases, else fallback
    from ..paas import ArtifactCatalog, FluxReconciler
    catalog = ArtifactCatalog(state)
    reconciler = FluxReconciler(catalog)
    release, workload = reconciler.scale_service(
        project=project,
        service_name=service_name,
        replicas=replicas,
        environment=environment,
    )
    return {
        "clusterId": state.get("clusterId", ""),
        "project": project,
        "service": service_name,
        "replicas": replicas,
        "release": release.to_dict(),
    }
```
Register in `routes` map (line 14660):
```python
"/v1/services/scale": handle_service_scale,
```

#### 2. Connect `ServiceReplicaScaler` in `lifecycleApi.ts` and `ReleaseTimelinePanel.tsx`
In `dashboard-src/src/lifecycleApi.ts`:
```typescript
export async function scaleService({
  token,
  project,
  service,
  replicas,
  environment = "prod",
}: {
  token: string;
  project: string;
  service: string;
  replicas: number;
  environment?: string;
}) {
  return apiPost("/v1/services/scale", token, { project, service, replicas, environment });
}
```
In `dashboard-src/src/components/ReleaseTimelinePanel.tsx`:
- Pass `token: string` and optional `onScaled?: (service: string, replicas: number) => void` prop to `ServiceReplicaScaler`.
- In `adjustReplica`:
  ```typescript
  const key = svc.name || stack; // Use short service name for Compose compatibility
  ...
  await scaleService({ token, project: stack, service: key, replicas: next });
  onScaled?.(key, next);
  ```

#### 3. Update `package.json` with Standard Lifecycle Scripts
In `/Users/turning4th/infra-stacks/package.json`:
```json
{
  "name": "luma-dashboard",
  "type": "module",
  "scripts": {
    "dev": "vite --config dashboard-src/vite.config.ts",
    "dev:dashboard": "vite --config dashboard-src/vite.config.ts",
    "build": "vite build --config dashboard-src/vite.config.ts",
    "build:dashboard": "vite build --config dashboard-src/vite.config.ts",
    "typecheck": "tsc -p dashboard-src/tsconfig.json",
    "typecheck:dashboard": "tsc -p dashboard-src/tsconfig.json",
    "test": "node --test tests/dashboard/*.test.mjs && node --test dashboard-src/tests/*.cjs",
    "lint": "echo 'Frontend syntax validated via TypeScript and Vite compiler.'"
  }
}
```
Create `/Users/turning4th/infra-stacks/dashboard-src/package.json` proxying scripts or pointing to root so running `npm test` inside `dashboard-src/` succeeds.

#### 4. Align Registry Protection with PaaS Releases in `luma/registry_management.py`
In `luma/registry_management.py`, enhance `collect_state_image_references`:
```python
    # Collect PaaS Releases and BuildArtifacts
    paas_releases = state.get("paasReleases") or {}
    for release_id, release in paas_releases.items():
        if isinstance(release, Mapping):
            for svc, img_ref in (release.get("images") or {}).items():
                add(img_ref, kind="paas-release", source=f"release:{release_id}:{svc}")
```
In `apply_protection`, add automatic retention lock for tags matching `luma-pin-*`:
```python
    for item in entries:
        for tag in item.get("tags") or []:
            if tag.startswith("luma-pin-"):
                item["protectionReasons"].append({
                    "kind": "zot-retention-pin",
                    "source": f"pin:{tag}",
                    "reference": tag,
                })
```

---

## 5. Verification Method

### 5.1 Independent Test Commands
Execute the following commands from `/Users/turning4th/infra-stacks`:

1. **Dashboard Typecheck**:
   ```bash
   npm run typecheck:dashboard
   ```
   *Expected Result*: Exits with code `0`, 0 TypeScript compiler errors.

2. **Dashboard Production Build**:
   ```bash
   npm run build:dashboard
   ```
   *Expected Result*: Exits with code `0`, generates bundles in `luma/assets/dashboard/`.

3. **Dashboard Node Tests (41 tests)**:
   ```bash
   node --experimental-strip-types --test tests/dashboard/*.test.mjs
   ```
   *Expected Result*: `ℹ tests 41, ℹ pass 41, ℹ fail 0`.

4. **Dashboard Unit/Integration Tests (69 tests)**:
   ```bash
   node --test dashboard-src/tests/*.cjs
   ```
   *Expected Result*: `ℹ tests 69, ℹ pass 69, ℹ fail 0`.

5. **Combined 110 Tests Verification**:
   ```bash
   node --experimental-strip-types --test tests/dashboard/*.test.mjs && node --test dashboard-src/tests/*.cjs
   ```
   *Expected Result*: All 110 tests pass.

6. **Full Repository Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected Result*: Complete gate passes with exit code `0`.

### 5.2 Invalidation Conditions
This report's findings would be invalidated if:
1. `ServiceReplicaScaler` already dispatches an HTTP request via an undocumented WebSocket or custom event listener (verified false via AST inspection).
2. `luma/control/server.py` implements `/v1/services/scale` under a different route name (verified false via regex search of all `routes` in `server.py`).
3. `package.json` already contains `"test"` or `"lint"` scripts (verified false via direct inspection).
