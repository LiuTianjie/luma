# Project: Luma K8s-Native PaaS Architecture Hardening

## Architecture
Luma's K8s-native PaaS architecture integrates multi-platform build orchestration, artifact management with Zot online GC, declarative GitOps releases via Flux CD v2, and zero-downtime migration from Nomad to K3s.
- `luma/build_capabilities.py` & `luma/paas/build/`: Native-only ARM64/AMD64 compilation, BuildKit builder routing, Tekton PipelineRun lifecycle (<3s cancellation).
- `luma/paas/build/artifact_store.py`: OCI registry client, Zot online GC retention pinning (`luma-pin-active-*`, `luma-pin-rollback-*`), and obsolete pin pruning.
- `luma/paas/release/`: Helm release compilation, node architecture affinity (`kubernetes.io/arch`), Compose Deployment splitting and sidecars, 6 exposure modes, and Flux reconciler (<5s rollback & promotion with secret preservation).
- `luma/paas/migration/`: 4-step zero-downtime migration engine (`plan -> shadow -> cutover -> abort -> finalize`), Batch 2 queue-worker race prevention, Batch 3 stateful volume hostPath/fsGroup pinning, pre-cutover safety gates, and openSUSE MicroOS / Darwin node self-healing.
- `luma/cli/paas.py` & `dashboard-src/`: Operator CLI and Web UI for PaaS deployment, migration, and replica scaling.
- `docs/`: Bilingual architecture and migration guides conforming to `scripts/check-site.py`.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | Native ARM64/AMD64 Builder Enforcement | Strict native platform validation in `build_capabilities.py` (rejecting multi-platform / QEMU emulation), topology-based ARM64 inference, and native builder selection | M1 | R2 Survey |
| F2 | Tekton <3s Build Cancellation & Finally | Inject `terminationGracePeriodSeconds: 3` and timeouts into PipelineRun, shell signal traps in step scripts, finally block, and terminal state validation in `cancel_build` | M1 | R1 Survey |
| F3 | Zot Retention Pins & Obsolete Tag Pruning | Align pin tag names (`luma-pin-active-*`, `luma-pin-rollback-*`), prune obsolete pins exceeding rollback window N=5 via OCI API, strip registry host from repository validation, multi-service digest grouping | M2 | R1 Survey |
| F4 | Flux <5s Rollback & Promotion Secret Parity | Inject `reconcile.fluxcd.io/requestedAt` into HelmRelease, carry forward runtime secrets and mounted files in `rollback_release` and `promote_artifact` | M2 | R1 Survey |
| F5 | Runtime Architecture Affinity & Sidecar Integrity | Emit `kubernetes.io/arch` in `nodeSelector`, reject node/region architecture conflicts, and reject mixed-architecture sidecars under `network_mode: service:*` | M3 | R2 Survey |
| F6 | Compose PVC Mounting & Exposure Modes Hardening | Mount created PVCs to container volumes in Compose, preserve sidecar custom command, respect `exposure: none`, and fix `tcp-relay` service port name/container port | M3 | R1 Survey |
| F7 | openSUSE MicroOS & Darwin Node Self-Healing | Handle `opensuse-microos` transactional rootfs, btrfs scrub & trim, writable `/var/lib` paths, SELinux `restorecon`, and macOS `<local>` `NO_PROXY` in `evaluate_node_self_heal` & daemonset | M4 | R2 Survey |
| F8 | Zero-Downtime Migration Engine Reliability | Batch 2 shadow queue worker replicas at 0 and cutover scale-up/drain; Batch 3 stateful volume node pinning & hostPath/fsGroup preservation; pre-cutover gates for probes, secrets, arch | M4 | R3 Survey |
| F9 | CLI, Dashboard & Bilingual Documentation | CLI persistent state & multi-service shadow images; dashboard replica scaler API wiring; bilingual migration guide codeblock and heading anchor parity | M5 | R1/R3 Survey |
| F10 | Comprehensive Test Suite & Full Gate | Add 4 unit test suites in `tests/test_paas_engine.py` verifying all R1-R3 invariants; pass `check-luma.sh` and `scripts/check-site.py` | M6 | R1-R4 Survey |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Build & Native Architecture | F1 (native ARM64/AMD64 building, QEMU rejection, inference) + F2 (Tekton <3s cancellation, signal trapping) | none | **DONE** |
| M2 | Artifact Store & Release Reconciler | F3 (Zot retention pins, tag pruning, host validation) + F4 (Flux <5s rollback annotation, secret preservation) | M1 | **DONE** |
| M3 | Helm Compiler & Workload Placement | F5 (nodeSelector arch affinity, sidecar arch rejection) + F6 (Compose PVC mounting, exposure modes) | M1 | **DONE** |
| M4 | Node Self-Healing & Zero-Downtime Migration | F7 (MicroOS/Darwin self-healing daemonset) + F8 (Batch 2 replica 0 shadow & cutover, Batch 3 volume node pin, SQLite/flock single-instance isolation, Traefik port coexistence, pre-cutover gates, standalone `scripts/migrate-nomad-to-k3s.py`, Nomad decommission) | M2, M3 | **DONE** |
| M5 | CLI, Dashboard, DevEx & Bilingual Documentation | F9 (Clean public CLI hiding one-time migrate via `argparse.SUPPRESS`, 0-to-1 bootstrap & `.env` compatibility, seamless Cloudflare integration, dashboard scaler, bilingual migration guides & CLI reference) | M4 | **DONE** |
| M6 | Test Verification & Full Gate Hardening | F10 (4 automated test suites in `test_paas_engine.py`, fresh bootstrap regression checks, full `check-luma.sh`, static site checks) | M5 | **DONE** |

## Interface Contracts
### `luma.build_capabilities` ↔ `luma.paas.build.controller`
- `validate_native_platform(platform: str) -> str`: Normalizes platform, raises `LumaError` if comma-separated (cross-emulation) or unsupported.
- `infer_native_platform(*, platform: str = "", target_node: str = "", target_region: str = "", node_architectures: Mapping[str, str] | None = None) -> str`: Returns `"linux/arm64"` for ARM64 nodes or `home` region; defaults to `"linux/amd64"`.
- `BuildController.select_native_builder(platform: str, *, preferred_node: str = "", target_node: str = "", target_region: str = "") -> BuilderEndpoint`: Enforces strict native builder platform match, raises `LumaError` on mismatch.

### `luma.paas.build.artifact_store` ↔ `luma.paas.release.flux_reconciler`
- `sync_retention_pins(releases: Sequence[WorkloadRelease], rollback_window: int = 5) -> Sequence[str]`: Retains active and latest N=5 rollback tags, deletes stale tags matching `^luma-pin-.*` beyond window.
- `FluxReconciler.rollback_release` and `promote_artifact`: Retains runtime secrets and mounted secret files from current/target release instead of wiping to empty.

### `luma.paas.release.helm_compiler` ↔ `luma.paas.migration.migrator`
- `compile_service_release` and `compile_compose_release`: Injects `kubernetes.io/arch` into `nodeSelector` matching artifact platform; validates node and region compatibility; rejects sidecars with mismatched architectures. Supports `replicas_override: Mapping[str, int] | None`.
- `MigrationEngine.shadow`: Sets `replicas_override[worker] = 0` for all queue/background workers while web replicas remain >= 1.
- `MigrationEngine.cutover`: Validates `release.gateway_probe_ok is True`, secret parity, node pins on Batch 3 volumes, and image architecture match before cutover. Scales paused workers to target replicas.

## Code Layout
- `luma/build_capabilities.py`: Native architecture normalization, validation, and inference.
- `luma/paas/build/controller.py`: Tekton PipelineRun builder, timeout & grace period, native builder routing, build cancellation.
- `deploy/paas-stack/tekton/luma-build-pipeline.yaml`: Tekton pipeline definition, signal trapping, finally tasks.
- `luma/paas/build/artifact_store.py`: OCI Zot client, retention pin naming, pin pruning, repository validation.
- `luma/paas/release/helm_compiler.py`: Manifest compiler, nodeSelector `kubernetes.io/arch`, Compose PVC mounting, sidecar arch validation, exposure modes.
- `luma/paas/release/flux_reconciler.py`: Flux HelmRelease reconciliation annotation, secret preservation on rollback and promotion.
- `luma/paas/migration/migrator.py`: 4-step migration engine, Batch 2 replica 0 shadow, Batch 3 volume node pin, pre-cutover validation, node self-healing evaluation.
- `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`: Node self-heal daemonset for MicroOS and Darwin.
- `luma/cli/paas.py`: PaaS CLI commands (`luma app`, `luma migrate`).
- `dashboard-src/src/`: React frontend for release timeline, replica scaling, registry pins.
- `docs/migration-guide.md` & `docs/migration-guide.zh-CN.md`: Bilingual migration guides.
- `tests/test_paas_engine.py`: PaaS engine automated test suites.
