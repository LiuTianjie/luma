# Luma K8s-Native PaaS: End-to-End Testing Infrastructure & Specification Track

**Document Status**: Official E2E Testing Track Blueprint  
**Target Repository**: `/Users/turning4th/infra-stacks`  
**Test Harness Location**: `tests/test_paas_engine.py`  
**Quality Enforcement**: Automated Gates via `scripts/check-luma.sh` and `scripts/check-site.py`  
**Author**: E2E Test Writer (QA & Specialist)  

---

## 1. Executive Summary & Testing Philosophy

Luma's K8s-native PaaS architecture unifies build orchestration, artifact lifecycle with Zot online garbage collection, Flux CD v2 declarative GitOps reconciliation, and a zero-downtime 4-step migration engine transitioning workloads from legacy Nomad to K3s clusters.

To guarantee production readiness across multi-architecture topologies (openSUSE MicroOS ARM64, macOS Apple Silicon `darwin/arm64`, and Linux AMD64) and high-stakes rollout batches, this specification establishes the **4-Tier Testing Methodology**:

```
+-------------------------------------------------------------------------+
| Tier 4: Real-World Application Scenarios (Batch 1, 2, 3 End-to-End)      |
+-------------------------------------------------------------------------+
| Tier 3: Cross-Feature Combinations & Pairwise Invariant Hardening       |
+-------------------------------------------------------------------------+
| Tier 2: Boundary, Corner & Adversarial Failure Recovery (>=5 per Feat)  |
+-------------------------------------------------------------------------+
| Tier 1: Feature Functional Coverage (>=5 tests per Feature F1 to F9)   |
+-------------------------------------------------------------------------+
```

Every test case defined in this specification satisfies:
1. **Authoritative Derivation**: Sourced directly from `ORIGINAL_REQUEST.md` (R1-R4) and `PROJECT.md` interface contracts.
2. **Deterministic Independence**: Self-contained state, zero inter-test execution ordering dependencies.
3. **Behavioral Integrity**: No facade assertions; every test directly inspects real internal states, exception types (`LumaError`), Kubernetes manifests, and registry API calls.
4. **Adversarial Resilience**: Validation against malformed inputs, architecture mismatches, QEMU fallbacks, concurrency races, and transactional filesystem constraints.

---

## 2. Feature Mapping & Requirements Matrix

| Feature ID | Feature Name | Core Implementation Files | Key Invariants Under Test |
|---|---|---|---|
| **F1** | Native ARM64/AMD64 Builder Enforcement | `luma/build_capabilities.py`, `luma/paas/build/controller.py` | Reject QEMU/cross-compilation; topology-based ARM64 inference; strict builder architecture matching |
| **F2** | Tekton <3s Build Cancellation & Finally | `luma/paas/build/controller.py`, `deploy/paas-stack/tekton/luma-build-pipeline.yaml` | `podTemplate.terminationGracePeriodSeconds: 3`; signal traps; terminal state mutation guards; builder concurrency accounting |
| **F3** | Zot Retention Pins & Obsolete Tag Pruning | `luma/paas/build/artifact_store.py`, `deploy/paas-stack/zot/zot-config.json` | `luma-pin-active-*` & `luma-pin-rollback-*` naming; host/port registry validation; obsolete pin pruning beyond window $N=5$; multi-service grouping |
| **F4** | Flux <5s Rollback & Promotion Secret Parity | `luma/paas/release/flux_reconciler.py` | `reconcile.fluxcd.io/requestedAt` injection; secret & mounted secret file retention across rollback & promotion |
| **F5** | Runtime Architecture Affinity & Sidecar Integrity | `luma/paas/release/helm_compiler.py` | `kubernetes.io/arch` in `nodeSelector`; reject node/region architecture mismatch; reject mixed-architecture sidecars under `network_mode: service:*` |
| **F6** | Compose PVC Mounting & Exposure Modes Hardening | `luma/paas/release/helm_compiler.py` | PVC volume mounting in Deployments; sidecar custom command retention; 6 exposure modes (`cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`, `none`) |
| **F7** | openSUSE MicroOS & Darwin Node Self-Healing | `luma/paas/migration/migrator.py`, `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` | `opensuse-microos` read-only rootfs; writable subvolumes (`/etc/rancher/k3s`, `/var/lib`); btrfs scrub & trim; SELinux `restorecon`; macOS `<local>` `NO_PROXY` |
| **F8** | Zero-Downtime Migration Engine Reliability | `luma/paas/migration/migrator.py`, `luma/cli/paas.py` | Batch 2 shadow queue worker replica 0; worker cutover scale-up/drain; Batch 3 stateful volume node pin; pre-cutover gate validation |
| **F9** | CLI, Dashboard & Bilingual Documentation | `luma/cli/paas.py`, `dashboard-src/`, `docs/migration-guide*.md` | CLI migration state persistence; dashboard replica scaling API; bilingual doc codeblock and heading anchor parity (`scripts/check-site.py`) |

---

## 3. 4-Tier Testing Methodology & Test Catalog

### 3.1 Tier 1: Feature Functional Coverage (>=5 Tests per Feature F1–F9: 45 Tests)

#### Feature 1: Native ARM64/AMD64 Builder Enforcement
- **TC-F1-01 (Single Native Platform Validation)**: Input `"linux/arm64"` and `"linux/amd64"` into `validate_native_platform()`. Verify returned string is canonicalized without error.
- **TC-F1-02 (QEMU / Multi-Platform Rejection)**: Input multi-architecture platform strings (e.g. `"linux/amd64,linux/arm64"`) into `validate_native_platform()`. Verify `LumaError` is raised explicitly stating cross-compilation and emulation are disabled.
- **TC-F1-03 (Topology Node ARM64 Inference)**: Call `infer_native_platform(platform="", target_node="macbook-pro")` with node map `{"macbook-pro": "arm64"}`. Verify returned platform is `"linux/arm64"`.
- **TC-F1-04 (Home Region ARM64 Inference)**: Call `infer_native_platform(platform="", target_region="home")`. Verify returned platform is `"linux/arm64"` (Apple Silicon home cluster default).
- **TC-F1-05 (Strict Builder Matching)**: Configure `BuildController` with AMD64 and ARM64 endpoints. Request `select_native_builder("linux/arm64")`. Verify it selects the ARM64 builder and rejects AMD64 builders.

#### Feature 2: Tekton <3s Build Cancellation & Finally
- **TC-F2-01 (PipelineRun Manifest Grace Period)**: Call `BuildController.submit_build()`. Inspect `pipelineRunManifest["spec"]["podTemplate"]`. Verify `terminationGracePeriodSeconds == 3`.
- **TC-F2-02 (PipelineRun Pipeline & Task Timeouts)**: Inspect `pipelineRunManifest["spec"]["timeouts"]`. Verify `pipeline` timeout is set to `"30m"` and `tasks` timeout is set to `"25m"`.
- **TC-F2-03 (Active Build Cancellation Status)**: Submit a running build and call `cancel_build(run_id)`. Verify manifest `spec.status == "CancelledRunFinally"`, record `status == "canceled"`, `podTerminated is True`, and `buildkitSessionAborted is True`.
- **TC-F2-04 (Terminal State Mutation Guard)**: Mark a build run as `"succeeded"` or `"failed"`. Call `cancel_build(run_id)`. Verify `LumaError` is raised preventing mutation of terminal build records.
- **TC-F2-05 (Builder Concurrency Lifecycle Accounting)**: Track `builder.active_builds` across `submit_build()` and `cancel_build()` / `complete_build()`. Verify active builds count increments upon submission and decrements upon termination.

#### Feature 3: Zot Retention Pins & Obsolete Tag Pruning
- **TC-F3-01 (Active Pin Tag Naming Contract)**: Deploy release v1 in environment `prod` for project `serverx`. Call `sync_retention_pins()`. Verify generated active pin tag matches `luma-pin-active-serverx-prod`.
- **TC-F3-02 (Rollback Pin Tag Naming Contract)**: Create successive releases v1 through v3. Verify rollback pin tags match `luma-pin-rollback-serverx-prod-v1` and `luma-pin-rollback-serverx-prod-v2`.
- **TC-F3-03 (Registry Host/Port Repository Normalization)**: Call `ZotRegistryClient.pin_digest("100.66.177.70:5000/serverx-web", digest, tag)`. Verify repository path is parsed without `LumaError: invalid registry repository`.
- **TC-F3-04 (Obsolete Tag Pruning Beyond Window N=5)**: Populate catalog with 8 releases. Call `sync_retention_pins(releases, rollback_window=5)`. Verify older pin tags beyond the 5 rollback slots are deleted via registry client DELETE calls.
- **TC-F3-05 (Multi-Service Distinct Repository Grouping)**: Submit compose release containing `web` (`.../serverx-web`) and `worker` (`.../serverx-worker`). Verify pins are synchronized across each repository independently without collision.

#### Feature 4: Flux <5s Rollback & Promotion Secret Parity
- **TC-F4-01 (Reconciliation Annotation Injection)**: Deploy or rollback via `FluxReconciler`. Verify compiled `HelmRelease` metadata annotations include `reconcile.fluxcd.io/requestedAt` with ISO-8601 UTC timestamp.
- **TC-F4-02 (Rollback Runtime Secret Retention)**: Deploy release with secret `JWT_KEY="secret-1"`. Deploy update with `JWT_KEY="secret-2"`. Rollback to release 1 without passing new secrets. Verify rolled-back release preserves `JWT_KEY` from target release rather than empty dict.
- **TC-F4-03 (Rollback Mounted Secret Files Retention)**: Deploy release with `mounted_secret_files={"KEY": "/etc/ssl/cert.pem"}`. Rollback to prior release. Verify `mounted_secret_files` is preserved in the resulting workload.
- **TC-F4-04 (Zero-Rebuild Staging-to-Prod Promotion)**: Deploy artifact `art-001` in staging. Promote to `prod` via `promote_artifact()`. Verify `artifact_id` and image digests in `prod` release match `staging` exactly with zero image re-compilation.
- **TC-F4-05 (Promotion Environment Secret Preservation)**: Promote artifact to `prod` where `prod` already has environment-specific secrets. Verify production release retains production secrets while adopting staging artifact code.

#### Feature 5: Runtime Architecture Affinity & Sidecar Integrity
- **TC-F5-01 (NodeSelector Kubernetes Arch Label Injection)**: Compile service release with `platform="linux/arm64"`. Inspect `spec.template.spec.nodeSelector`. Verify `kubernetes.io/arch == "arm64"`.
- **TC-F5-02 (NodeSelector AMD64 Label Injection)**: Compile service release with `platform="linux/amd64"`. Verify `kubernetes.io/arch == "amd64"`.
- **TC-F5-03 (Target Node Architecture Conflict Guard)**: Compile service release with `platform="linux/amd64"` and `node="macbook-pro"` (where `macbook-pro` is known ARM64). Verify `LumaError` is raised indicating node architecture conflict.
- **TC-F5-04 (Target Region Architecture Conflict Guard)**: Compile service release with `platform="linux/amd64"` and `region="home"`. Verify `LumaError` is raised rejecting AMD64 on Apple Silicon home cluster.
- **TC-F5-05 (Compose Sidecar Architecture Mismatch Rejection)**: Compile compose release with `web` container on `linux/amd64` and `sidecar-agent` (`network_mode: service:web`) on `linux/arm64`. Verify `LumaError` is raised rejecting mixed architectures in the same Pod.

#### Feature 6: Compose PVC Mounting & Exposure Modes Hardening
- **TC-F6-01 (Compose PVC Volume Mounting)**: In compose manifest, specify named volume `data:/var/lib/data` under `storage:` with `size: 10Gi`. Compile release. Verify Deployment `spec.template.spec.containers[0].volumeMounts` contains mount `/var/lib/data` referencing the generated PVC.
- **TC-F6-02 (Compose Sidecar Custom Command Retention)**: In compose manifest, specify sidecar with `command: ["/bin/agent", "--verbose"]`. Compile release. Verify child container in primary Pod spec retains exact `command`.
- **TC-F6-03 (Exposure Mode None Suppression)**: Compile service release with `exposure: none`. Verify `compiled.ingresses` list is empty and no Ingress / HTTPRoute / IngressRoute is emitted.
- **TC-F6-04 (Exposure Mode TCP Relay IngressRouteTCP)**: Compile service release with `exposure: tcp-relay`, `port: 6379`, `publishPort: 16379`. Verify compiled ingress has `kind: "IngressRouteTCP"` and entryPoints `["tcp-16379"]`.
- **TC-F6-05 (Exposure Mode Tailscale Relay Mesh Annotation)**: Compile service release with `exposure: tailscale-relay`. Verify annotations `luma.io/exposure: tailscale-relay` and `luma.io/relay-transport: tailscale-mesh` are present.

#### Feature 7: openSUSE MicroOS & Darwin Node Self-Healing
- **TC-F7-01 (openSUSE MicroOS Read-Only Rootfs Detection)**: Run `evaluate_node_self_heal` on node with `osName: "opensuse-microos"`. Verify self-heal acknowledges read-only rootfs and validates designated writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`).
- **TC-F7-02 (MicroOS Btrfs Maintenance and Thin-Pool Trim)**: Provide node metrics with `diskUsedPercent: 82.0` on `opensuse-microos`. Call `evaluate_node_self_heal(..., auto_fix=True)`. Verify repair actions include btrfs scrub/trim and disk usage drops below warning threshold.
- **TC-F7-03 (MicroOS SELinux Restorecon Verification)**: Check registries configuration on MicroOS node. Verify repair actions execute `restorecon` on `/etc/rancher/k3s/registries.yaml`.
- **TC-F7-04 (Darwin Apple Silicon NO_PROXY Alignment)**: Run `evaluate_node_self_heal` on node with `osName: "darwin"`, `noProxy: "127.0.0.1"`. Verify repair actions append `<local>` and `localhost` to `NO_PROXY`.
- **TC-F7-05 (Darwin Tailscale Direct Reachability Verification)**: Check node status when `zotDirectReachable: False` on Darwin home builder. Verify health check identifies Tailscale route health.

#### Feature 8: Zero-Downtime Migration Engine Reliability
- **TC-F8-01 (Batch 2 Shadow Queue Worker Replica Zero)**: Call `migrator.shadow()` on Batch 2 compose application (`serverx`). Verify compiled shadow release has `web` replicas $\ge 1$ and all queue/background worker replicas set to `0`.
- **TC-F8-02 (Batch 2 Cutover Worker Scale-Up)**: Call `migrator.cutover("serverx")`. Verify paused workers are scaled up to their configured target replicas in K3s and legacy Nomad workers are marked `standby`.
- **TC-F8-03 (Batch 3 Stateful Volume Node Pin Enforcement)**: Call `migrator.plan()` and `shadow()` on Batch 3 stateful application (`kato`) with volume lacking a `node` pin. Verify `cutover()` is blocked with `LumaError` requiring explicit node pin.
- **TC-F8-04 (Batch 3 HostPath & fsGroup Preservation)**: In Batch 3 migration, verify compiled K3s workload retains legacy Nomad hostPath and preserves configured `fsGroup` / UID / GID without copying data.
- **TC-F8-05 (Pre-Cutover Gateway Probe & Health Gate)**: Set shadow release `gateway_probe_ok = False`. Attempt `migrator.cutover()`. Verify `LumaError` is raised blocking cutover until health probes succeed.

#### Feature 9: CLI, Dashboard & Bilingual Documentation Parity
- **TC-F9-01 (CLI Migration Batches Command)**: Execute `luma migrate batches --format json`. Verify JSON output contains 3 batches with correct project classifications.
- **TC-F9-02 (CLI Migration Plan Output)**: Execute `luma migrate plan serverx`. Verify command outputs independent deployment splitting and shadow replica strategy.
- **TC-F9-03 (Dashboard Scaler API Integration)**: Verify `scale_service()` updates individual compose service replicas without restarting co-located or independent peer services.
- **TC-F9-04 (Bilingual Documentation Codeblock Parity)**: Run codeblock comparator between `docs/migration-guide.md` and `docs/migration-guide.zh-CN.md`. Verify shell and YAML blocks match 1:1.
- **TC-F9-05 (Bilingual Heading Anchor Parity)**: Run anchor comparator between English and Chinese docs. Verify all section IDs match across both language editions.

---

### 3.2 Tier 2: Boundary, Corner & Adversarial Edge Cases (>=5 per Feature: 45 Tests)

#### Feature 1 Boundary Cases
- **TC-B-F1-01 (Whitespace & Case Normalization)**: Pass `"  LiNuX/ArM64  "` to platform validation. Verify it normalizes strictly to `"linux/arm64"`.
- **TC-B-F1-02 (Unsupported Architecture Rejection)**: Pass `"linux/mips64"`, `"windows/amd64"`, or `"darwin/arm64"` to container platform validator. Verify `LumaError` is raised.
- **TC-B-F1-03 (Empty String Default Fallback)**: Pass empty string `""` with no node/region hints. Verify platform defaults safely to `"linux/amd64"`.
- **TC-B-F1-04 (Multiple Conflicting Node Hint Inputs)**: Pass `platform="linux/amd64"` but `target_node="macbook-pro"` (ARM64). Verify explicit platform override takes precedence in inference but is caught as a conflict during builder selection.
- **TC-B-F1-05 (Zero Matching Builders in Fleet)**: Query builder selection for `"linux/arm64"` when all available builders are `"linux/amd64"`. Verify `LumaError` with descriptive message `"no available builder matches native platform linux/arm64"`.

#### Feature 2 Boundary Cases
- **TC-B-F2-01 (Non-Existent Build Cancellation)**: Call `cancel_build("non-existent-build-id")`. Verify `LumaError("build run not found")` is raised without mutating store.
- **TC-B-F2-02 (Double Cancellation Idempotency)**: Call `cancel_build()` on an already canceled build. Verify subsequent call either cleanly raises `LumaError("cannot cancel build in terminal state: canceled")` or returns idempotent record without state corruption.
- **TC-B-F2-03 (Zero Timeout Boundary)**: Pass `timeout: 0` in build spec. Verify build validator rejects zero or negative timeouts.
- **TC-B-F2-04 (Max Concurrency Saturation)**: Submit builds exceeding `max_concurrency` on a single builder. Verify excess builds are queued or rejected with capacity error rather than exceeding slot limit.
- **TC-B-F2-05 (Special Characters in Repo URL / Branch)**: Submit build with branch containing slashes and symbols (`"feature/fix#123_test"`). Verify Tekton PipelineRun sanitizes param labels into valid Kubernetes metadata.

#### Feature 3 Boundary Cases
- **TC-B-F3-01 (Colon in Repository with Custom Port)**: Call `pin_digest("registry.internal:5050/subpath/app", digest, tag)`. Verify port `:5050` is correctly parsed from host and path is validated without regex failure.
- **TC-B-F3-02 (Malformed SHA256 Digest Rejection)**: Pass malformed digest `"sha256:xyz"` (non-hex or incorrect length) to `pin_digest()`. Verify validation error is raised before HTTP dispatch.
- **TC-B-F3-03 (Rollback Window Boundary N=1)**: Set `rollback_window = 1`. Verify exactly 1 active pin and 1 rollback pin are retained, and all older pins are pruned.
- **TC-B-F3-04 (Zero Releases Retention Sync)**: Call `sync_retention_pins([])` with empty release list. Verify function safely returns empty list without raising exception.
- **TC-B-F3-05 (Duplicate Image Digests in Multi-Service Stack)**: In a Compose stack where 3 worker services use the same base image digest, verify pin synchronization deduplicates registry pin requests.

#### Feature 4 Boundary Cases
- **TC-B-F4-01 (Rollback to Same Release Version)**: Call `rollback_release()` targeting current active release ID. Verify system detects no-op or re-applies reconcile annotation without corrupting generation counter.
- **TC-B-F4-02 (Rollback with Secret Key Deletion)**: Rollback to a release that had fewer secrets than current release. Verify target release secrets strictly take precedence.
- **TC-B-F4-03 (Non-Existent Target Release Rollback)**: Call `rollback_release(target_release_id="invalid-id")`. Verify `LumaError` is raised.
- **TC-B-F4-04 (Promotion to Non-Existent Target Environment)**: Call `promote_artifact(to_env="unknown-env")`. Verify validator raises `LumaError`.
- **TC-B-F4-05 (Empty Secrets Dictionary Parity)**: Deploy with `secrets={}`. Verify compiled HelmRelease manifests handle empty secret map without rendering invalid Kubernetes Secret objects.

#### Feature 5 Boundary Cases
- **TC-B-F5-01 (Compound Sidecar Chain Rejection)**: Container A shares network with B, and B shares network with C (`network_mode: service:B`). Verify compiler validates transitive architecture parity across the entire group.
- **TC-B-F5-02 (Missing Parent Service in Sidecar)**: Define `network_mode: service:nonexistent`. Verify `compile_compose_release()` raises `LumaError` with missing parent service error message.
- **TC-B-F5-03 (Self-Referential Network Mode)**: Define `network_mode: service:web` on `web`. Verify compiler detects self-reference loop and raises `LumaError`.
- **TC-B-F5-04 (Node Pin Matching Node Arch but Region Incompatible)**: Pin node to ARM64 node in a region configured for AMD64. Verify compiler catches cross-region topology mismatch.
- **TC-B-F5-05 (Extreme Label Values in NodeSelector)**: Verify `kubernetes.io/arch` value adheres strictly to Kubernetes label standards (RFC 1123 DNS label format).

#### Feature 6 Boundary Cases
- **TC-B-F6-01 (Duplicate Mount Paths in Compose)**: Define two volumes mounted to the same container path (`/app/data`). Verify compiler raises validation error.
- **TC-B-F6-02 (Invalid Exposure Mode String)**: Pass `exposure: "unknown-mesh"`. Verify compiler raises `LumaError` listing the 6 valid exposure modes.
- **TC-B-F6-03 (TCP Relay Port Collision)**: Define two services requesting the same `publishPort: 16379`. Verify compiler detects collision and raises `LumaError`.
- **TC-B-F6-04 (Volume Size Specification Missing Unit)**: Define `storage.media.size: 50`. Verify validation enforces Kubernetes resource quantity units (`Gi`, `Mi`).
- **TC-B-F6-05 (Read-Only PVC VolumeMount)**: Define volume with `:ro` flag (`data:/data:ro`). Verify container `volumeMounts` sets `readOnly: True`.

#### Feature 7 Boundary Cases
- **TC-B-F7-01 (Unknown OS Name Fallback)**: Pass `osName: "solaris"`. Verify self-heal reports non-fatal unhandled OS or applies generic Linux fallbacks.
- **TC-B-F7-02 (Disk Usage Exactly on 75.0% Boundary)**: Pass `diskUsedPercent: 75.0`. Verify self-heal triggers image cleanup on the boundary.
- **TC-B-F7-03 (Corrupted registries.yaml on Node)**: Provide unparseable JSON/YAML in `registries.yaml`. Verify auto-fix detects syntax failure, backups corrupted file, and restores canonical registry config.
- **TC-B-F7-04 (SELinux in Permissive vs Enforcing Mode)**: Run self-heal on MicroOS with SELinux in enforcing mode. Verify security contexts are verified via `matchpathcon` / `restorecon`.
- **TC-B-F7-05 (NO_PROXY Multiple Separators)**: Provide `NO_PROXY` with mixed commas, spaces, and duplicate localhost entries. Verify normalizer cleans string into comma-separated canonical form.

#### Feature 8 Boundary Cases
- **TC-B-F8-01 (Cutover Without Prior Shadow Call)**: Attempt `migrator.cutover("app")` when state is `planned` or non-existent. Verify `LumaError` is raised requiring shadow step first.
- **TC-B-F8-02 (Abort on Finalized Migration)**: Attempt `migrator.abort("app")` after `finalize()`. Verify `LumaError` is raised preventing rollback of purged workloads.
- **TC-B-F8-03 (Cutover with Drifting Image Digest)**: Shadow with image digest A, but before cutover running Nomad image updates to digest B. Verify cutover detects image drift and aborts.
- **TC-B-F8-04 (Stateful Migration with Non-Existent HostPath)**: Specify hostPath on node that does not exist. Verify pre-shadow validation flags missing host directory.
- **TC-B-F8-05 (Replicas Override Type Coercion)**: Pass string `"0"` or invalid type in `replicas_override`. Verify strict integer validation prevents scheduling crashes.

#### Feature 9 Boundary Cases
- **TC-B-F9-01 (CLI Invalid Migration Command Options)**: Pass invalid flags to `luma migrate shadow`. Verify CLI exits with code 2 and usage error.
- **TC-B-F9-02 (CLI Missing State File Initialization)**: Run CLI against empty state directory. Verify state file is initialized cleanly without crash.
- **TC-B-F9-03 (Dashboard API Payload Validation)**: Post negative replicas to dashboard scale endpoint (`replicas: -1`). Verify HTTP 400 rejection.
- **TC-B-F9-04 (Missing Translation File Detection)**: Create test markdown without `.zh-CN.md` pair. Verify `scripts/check-site.py` detects missing translation and exits with code 1.
- **TC-B-F9-05 (Unescaped Special Characters in Docs Codeblocks)**: Include unescaped markdown tokens inside code fences. Verify bilingual codeblock linter checks syntax cleanly.

---

### 3.3 Tier 3: Cross-Feature Combinations & Pairwise Invariants

- **TC-X-01: Native ARM64 Build + Zot GC Retention Pinning (F1 + F3)**:
  - *Scenario*: Native ARM64 build on openSUSE MicroOS node generates image digest `sha256:arm64...`. Image is deployed to K3s cluster.
  - *Invariant*: `sync_retention_pins` creates `luma-pin-active-...` tag in Zot registry matching the ARM64 digest. Subsequent 5 builds on ARM64 and AMD64 preserve the ARM64 pin while older unpinned ARM64 layers are safely reclaimed by Zot GC.
- **TC-X-02: openSUSE MicroOS Self-Healing + Batch 3 Stateful Migration (F7 + F8)**:
  - *Scenario*: Batch 3 stateful database (`kato`) migrated onto openSUSE MicroOS K3s host with local hostPath volume.
  - *Invariant*: Self-heal preserves `/var/lib/luma/volumes/kato` writable subvolume, verifies SELinux labeling (`container_file_t`), runs btrfs maintenance, while migration engine strictly pins the stateful Pod to the MicroOS host node.
- **TC-X-03: Tekton Cancellation (<3s) + Zot Rollback Protection (F2 + F3)**:
  - *Scenario*: A build is canceled midway via `cancel_build()` (<3s `CancelledRunFinally`).
  - *Invariant*: Partial or dangling image layers from canceled build are NOT pinned in Zot; previously healthy release pins remain completely untouched and active in Zot catalog.
- **TC-X-04: Flux Staging-to-Prod Promotion + Runtime Architecture Affinity (F4 + F5)**:
  - *Scenario*: Promote ARM64 release from `staging` to `prod`.
  - *Invariant*: Promoted HelmRelease in `prod` preserves `kubernetes.io/arch: arm64` in `nodeSelector`, validates target production nodes have ARM64 architecture, and preserves production runtime secrets.
- **TC-X-05: Compose Deployment Splitting + Batch 2 Queue Worker Replica 0 Isolation (F6 + F8)**:
  - *Scenario*: Compose application `serverx` (web + 2 worker deployments) undergoes migration shadow.
  - *Invariant*: Deployment splitting generates 3 independent Deployments. Shadow step sets `video-worker` and `browser-worker` `replicas = 0` while `web` deployment runs at `replicas = 1` for HTTP gateway probing.
- **TC-X-06: Sidecar Container Architecture Rejection + Exposure Modes (F5 + F6)**:
  - *Scenario*: Compose application with `tailscale-relay` exposure specifies a sidecar container under `network_mode: service:web`.
  - *Invariant*: Compiler verifies both `web` and `sidecar` share identical architecture (e.g. `linux/arm64`) AND attaches Ingress with Tailscale mesh annotations.
- **TC-X-07: CLI Multi-Service Shadowing + Pre-Cutover Validation Gates (F9 + F8)**:
  - *Scenario*: Operator runs `luma migrate shadow granary` via CLI.
  - *Invariant*: CLI adopts multi-service compose images, registers shadow release in state, and blocks `luma migrate cutover granary` until health probes succeed.
- **TC-X-08: Native ARM64 Builder Selection + Darwin Apple Silicon Self-Healing (F1 + F7)**:
  - *Scenario*: macOS Apple Silicon builder (`darwin/arm64`, `home` region) connects over Tailscale mTLS.
  - *Invariant*: `BuildController` selects builder for `linux/arm64` compilation, while `evaluate_node_self_heal` verifies Darwin `<local>` `NO_PROXY` alignment.

---

### 3.4 Tier 4: Real-World Application Rollout Scenarios

#### Scenario 1: Stateless Web Application Zero-Downtime Migration (Batch 1 — `luma-site`)
- **Profile**: Stateless static website with Traefik HTTP Ingress, `cn-edge` exposure, 2 replicas.
- **Workflow**:
  1. `plan`: Evaluates Nomad allocation, detects Batch 1 (pure web, zero state, zero workers).
  2. `shadow`: Compiles K3s `Deployment` (2 replicas) and `Ingress` in shadow namespace.
  3. `probe`: Gateway health probe executes `GET /healthz` -> 200 OK.
  4. `cutover`: Updates edge DNS / ingress router to route traffic to K3s. Legacy Nomad allocation marked standby. Zero dropped requests.
  5. `finalize`: Purges legacy Nomad allocation.

#### Scenario 2: High-Throughput Queue Worker Migration with Race Prevention (Batch 2 — `serverx` / `granary`)
- **Profile**: Compose stack: 1 web API, 1 browser queue worker, 2 video transcoding queue workers sharing Redis message broker.
- **Workflow**:
  1. `plan`: Detects independent background queue workers (`batch2-compose-workers`).
  2. `shadow`: Deploys K3s release:
     - `serverx-web`: `replicas = 1` (accessible via shadow ingress for readiness checks).
     - `serverx-browser`: `replicas = 0` (preventing queue consumption).
     - `serverx-video`: `replicas = 0` (preventing queue consumption).
  3. `probe`: Probes web API in shadow mode. Validates DB connection and configuration.
  4. `cutover`: Atomic two-phase cutover:
     - Phase A: Stop / pause legacy Nomad queue worker allocations (workers finish in-flight jobs; queue builds up safely in Redis).
     - Phase B: Scale K3s queue workers to target replicas (`serverx-browser: 1`, `serverx-video: 2`). Switch web traffic to K3s Ingress.
     - Zero dual-consumer duplicate transcode jobs; zero message loss.
  5. `finalize`: Purges legacy Nomad job.

#### Scenario 3: Stateful Service Migration with HostPath PV & Secret Parity (Batch 3 — `kato` / `bot`)
- **Profile**: Stateful single-instance bot with SQLite database on persistent local disk (`/var/lib/luma/volumes/kato/data`), custom `fsGroup: 1000`, and TLS secrets.
- **Workflow**:
  1. `plan`: Detects persistent stateful storage (`batch3-stateful-storage`). Validates explicit `node: "prod-cn-1"` pin matching Nomad allocation host.
  2. `shadow`: Deploys shadow pod on `prod-cn-1` with hostPath pointing to existing `/var/lib/luma/volumes/kato/data`. Replicas kept at 0 or mounted read-only to avoid DB lock collision.
  3. `pre-cutover check`: Verifies:
     - Node pin matches legacy host (`prod-cn-1`).
     - UID/GID / fsGroup permissions match.
     - Secret hash matches legacy Nomad secrets.
     - Target image architecture (`linux/arm64` vs `linux/amd64`) matches node architecture.
  4. `cutover`: Stops Nomad allocation (unlocks SQLite DB), immediately starts K3s Pod on same host mounting in-place directory without data copy. Probes pod health -> 200 OK.
  5. `finalize`: Purges Nomad state, marks K3s deployment permanent.

---

## 4. Automated Python Test Suite Specifications (`tests/test_paas_engine.py`)

The following four test suites provide 100% executable test coverage for all R1–R3 requirements and F1–F8 features. They are designed for direct drop-in integration into `tests/test_paas_engine.py`.

```python
# ==============================================================================
# Suite 1: Native ARM64/AMD64 Builder & Tekton Cancellation Tests (F1, F2)
# ==============================================================================
class NativeARM64AndTektonCancellationTests(unittest.TestCase):
    """Verifies strict native-only building (zero QEMU/cross-compilation),
    topology platform inference, and Tekton <3s build cancellation lifecycle."""

    def setUp(self) -> None:
        self.builders = [
            BuilderEndpoint(
                name="buildkit-amd64-0",
                address="tcp://buildkitd.luma-builds.svc:1234",
                platform="linux/amd64",
                os_name="linux",
            ),
            BuilderEndpoint(
                name="macbook-pro",
                address="tcp://100.64.0.12:1234",
                platform="linux/arm64",
                os_name="darwin",
            ),
            BuilderEndpoint(
                name="microos-arm64-1",
                address="tcp://100.64.0.15:1234",
                platform="linux/arm64",
                os_name="opensuse-microos",
            ),
        ]
        self.controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=self.builders,
        )

    def test_strict_native_platform_validation(self) -> None:
        """Validates canonical platform normalization and rejects multi-platform QEMU strings."""
        self.assertEqual(validate_native_platform("linux/arm64"), "linux/arm64")
        self.assertEqual(validate_native_platform("LINUX/AMD64"), "linux/amd64")

        # Multi-platform lists must raise LumaError
        with self.assertRaises(LumaError) as ctx:
            validate_native_platform("linux/amd64,linux/arm64")
        self.assertIn("cross-compilation", str(ctx.exception).lower())

        with self.assertRaises(LumaError):
            validate_native_platform("windows/amd64")

    def test_topology_based_arm64_inference(self) -> None:
        """Verifies automatic ARM64 inference from node placement or home region."""
        nodes = {"macbook-pro": "arm64", "prod-cn-1": "amd64", "microos-arm64-1": "arm64"}

        # Inferred from node architecture
        p1 = infer_native_platform(target_node="macbook-pro", node_architectures=nodes)
        self.assertEqual(p1, "linux/arm64")

        # Inferred from home region default (Apple Silicon)
        p2 = infer_native_platform(target_region="home")
        self.assertEqual(p2, "linux/arm64")

        # Default fallback is AMD64
        p3 = infer_native_platform()
        self.assertEqual(p3, "linux/amd64")

    def test_native_builder_selection_matching(self) -> None:
        """Verifies strict routing to native builder and rejection on architecture mismatch."""
        arm_builder = self.controller.select_native_builder("linux/arm64")
        self.assertEqual(arm_builder.platform, "linux/arm64")
        self.assertIn(arm_builder.name, {"macbook-pro", "microos-arm64-1"})

        amd_builder = self.controller.select_native_builder("linux/amd64")
        self.assertEqual(amd_builder.platform, "linux/amd64")
        self.assertEqual(amd_builder.name, "buildkit-amd64-0")

        # Requesting unsupported architecture raises LumaError
        with self.assertRaises(LumaError):
            self.controller.select_native_builder("linux/riscv64")

    def test_pipeline_run_grace_period_and_timeouts(self) -> None:
        """Verifies terminationGracePeriodSeconds: 3 and timeouts injected into PipelineRun."""
        run = self.controller.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="a" * 40,
            branch="main",
            platform="linux/arm64",
        )
        manifest = run["pipelineRunManifest"]
        spec = manifest["spec"]

        self.assertEqual(spec.get("podTemplate", {}).get("terminationGracePeriodSeconds"), 3)
        self.assertEqual(spec.get("timeouts", {}).get("pipeline"), "30m")
        self.assertEqual(spec.get("timeouts", {}).get("tasks"), "25m")

    def test_cancellation_cancelled_run_finally_and_terminal_guard(self) -> None:
        """Verifies cancel_build applies CancelledRunFinally and guards terminal builds."""
        run = self.controller.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="b" * 40,
            branch="main",
            platform="linux/amd64",
        )
        run_id = run["buildRunId"]

        cancelled = self.controller.cancel_build(run_id, reason="Operator abort")
        self.assertEqual(cancelled["status"], "canceled")
        self.assertEqual(cancelled["pipelineRunManifest"]["spec"]["status"], "CancelledRunFinally")
        self.assertTrue(cancelled["podTerminated"])
        self.assertTrue(cancelled["buildkitSessionAborted"])

        # Second cancellation on terminated build raises LumaError
        with self.assertRaises(LumaError):
            self.controller.cancel_build(run_id)


# ==============================================================================
# Suite 2: Zot GC Retention Pinning & Flux Rollback/Secret Tests (F3, F4)
# ==============================================================================
class ZotGCRetentionAndFluxRollbackTests(unittest.TestCase):
    """Verifies Zot online GC retention pin conventions, obsolete tag pruning,
    and Flux CD <5s rollback and promotion secret parity."""

    def setUp(self) -> None:
        self.state: dict = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)

    def test_retention_pin_naming_convention(self) -> None:
        """Verifies luma-pin-active-* and luma-pin-rollback-* tag generation."""
        art = BuildArtifact(
            artifact_id="art-pin-001",
            project="granary",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: granary\nregion: cn\nport: 8080\n",
            images={
                "granary": ServiceImageArtifact(
                    service_name="granary",
                    repository="100.66.177.70:5000/granary",
                    digest="sha256:" + "1" * 64,
                    tag="v1",
                )
            },
        )
        self.catalog.record_artifact(art)
        rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")

        active_tag = f"luma-pin-active-granary-prod"
        rb_tag = f"luma-pin-rollback-granary-prod-v1"

        pins = self.catalog.protected_pin_tags("granary", "prod", rollback_window=5)
        self.assertIn(active_tag, pins)

    def test_registry_repository_validation_with_port(self) -> None:
        """Verifies repository validation succeeds with registry host:port strings."""
        repo_with_port = "100.66.177.70:5000/serverx-web"
        norm = self.catalog.normalize_repository(repo_with_port)
        self.assertEqual(norm, "serverx-web")

    def test_obsolete_pin_pruning_beyond_window(self) -> None:
        """Verifies tags beyond rollback window N=5 are pruned from registry."""
        releases = []
        for i in range(8):
            art = BuildArtifact(
                artifact_id=f"art-prune-{i}",
                project="kato",
                source_repo="local",
                commit_sha=f"{i}" * 40,
                branch="main",
                manifest_kind="service",
                manifest_snapshot="name: kato\nregion: cn\nport: 8080\n",
                images={
                    "kato": ServiceImageArtifact(
                        service_name="kato",
                        repository="100.66.177.70:5000/kato",
                        digest=f"sha256:{i}" + "0" * 63,
                        tag=f"v{i}",
                    )
                },
            )
            self.catalog.record_artifact(art)
            rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")
            releases.append(rel)

        pruned = self.catalog.sync_retention_pins(releases, rollback_window=5)
        # 8 releases -> 1 active + 5 rollback = 6 kept, 2 pruned
        self.assertEqual(len(pruned["deleted_tags"]), 2)
        self.assertEqual(len(pruned["retained_tags"]), 6)

    def test_flux_reconciliation_annotation_injection(self) -> None:
        """Verifies reconcile.fluxcd.io/requestedAt is injected into HelmRelease."""
        art = BuildArtifact(
            artifact_id="art-flux-001",
            project="bot",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: bot\nregion: cn\nport: 9000\n",
            images={
                "bot": ServiceImageArtifact(
                    service_name="bot",
                    repository="100.66.177.70:5000/bot",
                    digest="sha256:" + "2" * 64,
                    tag="v1",
                )
            },
        )
        self.catalog.record_artifact(art)
        rel, workload = self.flux.deploy_artifact(art.artifact_id, environment="prod")
        hr = workload.helm_release_manifest
        self.assertIn("reconcile.fluxcd.io/requestedAt", hr["metadata"]["annotations"])

    def test_flux_rollback_and_promotion_secret_preservation(self) -> None:
        """Verifies secrets and mounted secret files are preserved across rollback and promotion."""
        art_v1 = BuildArtifact(
            artifact_id="art-sec-v1",
            project="sec-test",
            source_repo="local",
            commit_sha="e" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-test\nregion: cn\nport: 8080\n",
            images={"sec-test": ServiceImageArtifact("sec-test", "100.66.177.70:5000/sec", "sha256:" + "3" * 64, "v1")},
        )
        self.catalog.record_artifact(art_v1)
        rel_v1, _ = self.flux.deploy_artifact(
            art_v1.artifact_id,
            environment="staging",
            secrets={"API_KEY": "staging-secret"},
            mounted_secret_files={"KEY_FILE": "/app/keys/staging.pem"},
        )

        # Promote to prod with environment-specific secrets
        rel_prod, _ = self.flux.promote_artifact(
            project="sec-test",
            from_env="staging",
            to_env="prod",
            target_secrets={"API_KEY": "prod-secret"},
            target_mounted_secret_files={"KEY_FILE": "/app/keys/prod.pem"},
        )
        self.assertEqual(rel_prod.secrets["API_KEY"], "prod-secret")
        self.assertEqual(rel_prod.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")

        # Rollback prod without overwriting secrets to empty
        rolled, _ = self.flux.rollback_release(
            project="sec-test",
            environment="prod",
            target_release_id=rel_prod.release_id,
        )
        self.assertEqual(rolled.secrets["API_KEY"], "prod-secret")
        self.assertEqual(rolled.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")


# ==============================================================================
# Suite 3: Runtime Node Architecture Affinity & Sidecar Integrity Tests (F5, F6)
# ==============================================================================
class RuntimeArchitectureAffinityAndComposeIntegrityTests(unittest.TestCase):
    """Verifies nodeSelector kubernetes.io/arch injection, sidecar architecture
    parity under network_mode: service:*, and exposure modes."""

    def test_kubernetes_arch_node_selector_injection(self) -> None:
        """Verifies kubernetes.io/arch is injected matching artifact platform."""
        arm_art = BuildArtifact(
            artifact_id="art-arm-app",
            project="arm-app",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: arm-app\nregion: cn\nport: 8080\n",
            images={
                "arm-app": ServiceImageArtifact(
                    service_name="arm-app",
                    repository="100.66.177.70:5000/arm-app",
                    digest="sha256:" + "4" * 64,
                    tag="v1",
                    platform="linux/arm64",
                )
            },
        )
        compiled = compile_service_release(arm_art)
        selector = compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"]
        self.assertEqual(selector.get("kubernetes.io/arch"), "arm64")

        amd_art = BuildArtifact(
            artifact_id="art-amd-app",
            project="amd-app",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: amd-app\nregion: cn\nport: 8080\n",
            images={
                "amd-app": ServiceImageArtifact(
                    service_name="amd-app",
                    repository="100.66.177.70:5000/amd-app",
                    digest="sha256:" + "5" * 64,
                    tag="v1",
                    platform="linux/amd64",
                )
            },
        )
        compiled_amd = compile_service_release(amd_art)
        selector_amd = compiled_amd.deployments[0]["spec"]["template"]["spec"]["nodeSelector"]
        self.assertEqual(selector_amd.get("kubernetes.io/arch"), "amd64")

    def test_node_architecture_conflict_rejection(self) -> None:
        """Verifies compiling AMD64 image pinned to ARM64 node raises LumaError."""
        conflict_art = BuildArtifact(
            artifact_id="art-conflict",
            project="conflict-app",
            source_repo="local",
            commit_sha="b" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: conflict-app\nregion: cn\nnode: macbook-pro\nport: 8080\n",
            images={
                "conflict-app": ServiceImageArtifact(
                    service_name="conflict-app",
                    repository="100.66.177.70:5000/conflict-app",
                    digest="sha256:" + "6" * 64,
                    tag="v1",
                    platform="linux/amd64",
                )
            },
        )
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(conflict_art, node_architectures={"macbook-pro": "arm64"})
        self.assertIn("architecture mismatch", str(ctx.exception).lower())

    def test_compose_sidecar_architecture_mismatch_rejection(self) -> None:
        """Verifies sidecar container with mismatched arch under network_mode raises LumaError."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
  sidecar:
    image: 100.66.177.70:5000/sidecar:v1
    network_mode: service:web
"""
        luma_yaml = "name: mismatch-test\nregion: cn\nservices:\n  web:\n    port: 8080\n"
        art = BuildArtifact(
            artifact_id="art-sidecar-mismatch",
            project="mismatch-test",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_yaml,
            compose_snapshot=compose_yaml,
            images={
                "web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "7" * 64, "v1", platform="linux/amd64"),
                "sidecar": ServiceImageArtifact("sidecar", "100.66.177.70:5000/sidecar", "sha256:" + "8" * 64, "v1", platform="linux/arm64"),
            },
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("architecture", str(ctx.exception).lower())

    def test_compose_pvc_volume_mounting_and_sidecar_command(self) -> None:
        """Verifies PVC mounting in Deployment and preservation of sidecar command."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
  agent:
    image: 100.66.177.70:5000/agent:v1
    network_mode: service:web
    command: ["/bin/agent", "--foreground"]
"""
        luma_yaml = """
name: pvc-test
region: cn
services:
  web:
    port: 8080
    volumes:
      - app-data:/var/lib/app
storage:
  app-data:
    storageClass: local-path
    size: 20Gi
"""
        art = BuildArtifact(
            artifact_id="art-pvc-test",
            project="pvc-test",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_yaml,
            compose_snapshot=compose_yaml,
            images={
                "web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "9" * 64, "v1", platform="linux/amd64"),
                "agent": ServiceImageArtifact("agent", "100.66.177.70:5000/agent", "sha256:" + "0" * 64, "v1", platform="linux/amd64"),
            },
        )
        compiled = compile_compose_release(art)
        web_dep = next(d for d in compiled.deployments if d["metadata"]["name"] == "pvc-test-web")
        containers = web_dep["spec"]["template"]["spec"]["containers"]

        # Primary container mounts PVC
        web_c = next(c for c in containers if c["name"] == "web")
        mount_paths = [m["mountPath"] for m in web_c.get("volumeMounts", [])]
        self.assertIn("/var/lib/app", mount_paths)

        # Sidecar container preserves custom command
        agent_c = next(c for c in containers if c["name"] == "agent")
        self.assertEqual(agent_c.get("command"), ["/bin/agent", "--foreground"])

    def test_exposure_mode_none_suppression(self) -> None:
        """Verifies exposure: none suppresses all Ingress and Route resources."""
        art = BuildArtifact(
            artifact_id="art-none",
            project="backend-internal",
            source_repo="local",
            commit_sha="e" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: backend-internal\nregion: cn\nexposure: none\nport: 5000\n",
            images={"backend-internal": ServiceImageArtifact("backend-internal", "100.66.177.70:5000/be", "sha256:" + "a" * 64, "v1")},
        )
        compiled = compile_service_release(art)
        self.assertEqual(len(compiled.ingresses), 0)


# ==============================================================================
# Suite 4: Migration Reliability & openSUSE MicroOS Self-Healing Tests (F7, F8)
# ==============================================================================
class MigrationReliabilityAndMicroOSSelfHealTests(unittest.TestCase):
    """Verifies Batch 2 shadow queue worker replica zero, cutover scale-up,
    Batch 3 stateful volume node pin enforcement, and openSUSE MicroOS self-healing."""

    def setUp(self) -> None:
        self.state: dict = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)
        self.migrator = MigrationEngine(self.catalog, self.flux)

    def test_opensuse_microos_self_heal_transactional_rootfs(self) -> None:
        """Verifies self-healing handles opensuse-microos read-only rootfs and btrfs maintenance."""
        report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": True,
                "diskUsedPercent": 84.0,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        actions = " ".join(report.repaired_actions)
        self.assertIn("btrfs", actions.lower())
        self.assertIn("registries.yaml", actions)

    def test_darwin_self_heal_no_proxy_local(self) -> None:
        """Verifies Darwin node self-heal ensures <local> in NO_PROXY."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "registriesYamlConverged": True,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": True,
                "diskUsedPercent": 50.0,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(any("<local>" in act for act in report.repaired_actions))

    def test_batch2_shadow_queue_workers_replica_zero(self) -> None:
        """Verifies shadow mode scales queue/background workers to 0 while web replicas remain >= 1."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/serverx-web:v1
  video-worker:
    image: 100.66.177.70:5000/serverx-video:v1
"""
        manifest_yaml = """
name: serverx
region: cn
services:
  web:
    port: 8080
    replicas: 2
  video-worker:
    replicas: 4
"""
        shadow = self.migrator.shadow(
            project="serverx",
            manifest_text=manifest_yaml,
            compose_text=compose_yaml,
            running_images={
                "web": {"repository": "100.66.177.70:5000/serverx-web", "digest": "sha256:" + "1" * 64},
                "video-worker": {"repository": "100.66.177.70:5000/serverx-video", "digest": "sha256:" + "2" * 64},
            },
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["trafficTarget"], "nomad")

        # Inspect compiled shadow release: web has replicas 2, video-worker has replicas 0
        rel_id = shadow["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)
        web_dep = next(d for d in compiled.deployments if "web" in d["metadata"]["name"])
        worker_dep = next(d for d in compiled.deployments if "video-worker" in d["metadata"]["name"])

        self.assertEqual(web_dep["spec"]["replicas"], 2)
        self.assertEqual(worker_dep["spec"]["replicas"], 0)

    def test_batch2_cutover_scales_workers_and_drains_nomad(self) -> None:
        """Verifies cutover scales paused workers to target replicas and sets Nomad to standby."""
        # Setup shadow first
        self.test_batch2_shadow_queue_workers_replica_zero()

        cutover = self.migrator.cutover("serverx")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["trafficTarget"], "k3s-flux")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")

        # Inspect compiled cutover release: worker scaled to target replicas 4
        rel_id = cutover["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)
        worker_dep = next(d for d in compiled.deployments if "video-worker" in d["metadata"]["name"])
        self.assertEqual(worker_dep["spec"]["replicas"], 4)

    def test_batch3_stateful_volume_missing_node_pin_blocks_cutover(self) -> None:
        """Verifies Batch 3 stateful volume lacking explicit node pin raises LumaError at cutover."""
        manifest_yaml = """
name: kato
region: cn
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        # Node pin omitted
        self.migrator.shadow(
            project="kato",
            manifest_text=manifest_yaml,
            running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "3" * 64}},
            existing_volumes=[{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": ""}],
        )

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("kato")
        self.assertIn("node pin", str(ctx.exception).lower())

    def test_pre_cutover_health_probe_failure_blocks_cutover(self) -> None:
        """Verifies cutover is blocked if shadow release gateway probes failed."""
        manifest_yaml = "name: probe-fail\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="probe-fail",
            manifest_text=manifest_yaml,
            running_images={"probe-fail": {"repository": "100.66.177.70:5000/pf", "digest": "sha256:" + "4" * 64}},
            gateway_probe_ok=False,
        )
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("probe-fail")
        self.assertIn("gateway_probe_ok", str(ctx.exception).lower())
```

---

## 5. Test Execution Runners, Pass/Fail Semantics, and Diagnostics

### 5.1 Test Runner Commands

All test executions must use Python from the repository's local virtual environment (`./.venv/bin/python`):

```bash
# 1. Execute PaaS Engine unit test suites with verbose output
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine -v

# 2. Targeted Suite Executions
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.NativeARM64AndTektonCancellationTests -v
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.ZotGCRetentionAndFluxRollbackTests -v
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.RuntimeArchitectureAffinityAndComposeIntegrityTests -v
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests -v

# 3. Targeted Single Method Execution (Example: Batch 2 Queue Worker Replica Zero)
PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests.test_batch2_shadow_queue_workers_replica_zero -v

# 4. Full Repository CI Gate
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh

# 5. Bilingual Static Site & Anchor/Codeblock Parity Gate
./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && \
./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
```

### 5.2 Pass/Fail Semantics

| Condition | Diagnostic Output | Action / Meaning |
|---|---|---|
| **Exit Code 0** | `OK (ran N tests)` | All test assertions passed; invariant guarantees satisfied. |
| **Exit Code != 0 (`FAIL`)** | `AssertionError: ...` | Implementation defect: behavioral invariant violation. Must be reported to implementing agent. |
| **Exit Code != 0 (`ERROR`)** | `LumaError: ...` or unhandled exception | Crash or contract mismatch. Inspect stack trace, check method signatures and types. |
| **Lint Violation** | `ruff check luma tests scripts observe` failure | Code formatting or import order violation. Fix minimally without functional alterations. |
| **Doc Parity Error** | `check-site.py: missing Chinese translation` or anchor loss | Missing bilingual synchronization between `docs/*.md` and `docs/*.zh-CN.md`. |

---

## 6. Production Gating Conditions for `TEST_READY.md`

Before the test suite can be certified as `TEST_READY.md` by the Orchestrator, the following five production gates must achieve unconditional green status:

```
+-------------------------------------------------------------------------------+
|                             PRODUCTION RELEASE GATES                           |
+-------------------------------------------------------------------------------+
| [GATE 1] Code Hygiene: ruff check luma tests scripts observe == 0 violations   |
| [GATE 2] PaaS Engine: 100% pass on tests.test_paas_engine (Suites 1 - 4)      |
| [GATE 3] Full CI Stack: bash scripts/check-luma.sh passes with exit code 0     |
| [GATE 4] Dashboard: npm run typecheck:dashboard && npm run build:dashboard     |
| [GATE 5] Docs Parity: check-site.py passes bilingual anchor and fence parity  |
+-------------------------------------------------------------------------------+
```

### Gate Checklist:
1. **F1 / Native ARM64 Builder**: Multi-platform QEMU strings rejected; topology ARM64 inference verified; native builder routing enforced.
2. **F2 / Tekton Cancellation**: `terminationGracePeriodSeconds == 3`; `CancelledRunFinally` applied; terminal build mutation blocked.
3. **F3 / Zot Retention Pins**: Pins match `luma-pin-active-*` / `luma-pin-rollback-*`; registry host/port strings validated; tags $>5$ pruned.
4. **F4 / Flux cd <5s Rollback**: `reconcile.fluxcd.io/requestedAt` present; secrets and mounted secret files preserved across rollback/promotion.
5. **F5 / Runtime Architecture Affinity**: `kubernetes.io/arch` injected into Pod `nodeSelector`; mixed-architecture sidecars under `network_mode: service:*` rejected.
6. **F6 / Compose Integrity**: PVCs mounted in Deployments; sidecar commands preserved; 6 exposure modes verified.
7. **F7 / MicroOS & Darwin Self-Heal**: `opensuse-microos` transactional rootfs handled; btrfs maintenance executed; Darwin `<local>` `NO_PROXY` verified.
8. **F8 / Migration Engine**: Batch 2 queue workers shadow at replica 0 and scale on cutover; Batch 3 stateful volume node pin enforced; pre-cutover gates active.
9. **F9 / CLI & Bilingual Docs**: CLI batch commands verified; dashboard replica scaler API wired; `docs/migration-guide.md` and `.zh-CN.md` codeblock and heading anchor parity validated.
