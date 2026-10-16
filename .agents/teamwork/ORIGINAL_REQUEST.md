# Original User Request

## Initial Request — 2026-10-15T16:00:33Z

Conduct a rigorous adversarial code review and production-readiness hardening of Luma's K8s-native PaaS architecture (`luma/paas/`, `deploy/paas-stack/`, `luma/cli/`, `luma/control/`, `dashboard-src/`, `docs/`) in `/Users/turning4th/infra-stacks`. Directly fix any architectural disconnects, edge-case bugs, or missing guards found during the review, and verify all fixes with automated tests.

Working directory: /Users/turning4th/infra-stacks
Integrity mode: development

## Requirements

### R1. End-to-End Implementation Audit & Realization of PaaS Advantages
Perform an adversarial review of `luma/paas/`, `deploy/paas-stack/`, `luma/cli/paas.py`, `dashboard-src/`, and their integration with existing Luma modules (`luma/service.py`, `luma/compose.py`, `luma/regions.py`, `luma/build_capabilities.py`, `luma/control/`). Identify and fix any places where the new architecture's core advantages—Tekton `<3s` `CancelledRunFinally` build termination, Zot online GC with `luma-pin-active-*`/`luma-pin-rollback-*` retention pins, Flux `<5s` rollback and zero-rebuild cross-environment promotion, Compose independent `Deployment` splitting vs `network_mode: service:*` Pod sidecars, and all 6 `exposure` modes (`cn-edge`, `external-edge`, `tailscale-relay`, `tcp-relay`, `cloudflare-tunnel`, `none`)—have edge-case bugs, incomplete validations, or disconnects from Luma's CLI/control workflows.

### R2. Native ARM64 Build & Runtime Enforcement (openSUSE MicroOS ARM64 + macOS Apple Silicon)
Audit and harden end-to-end ARM64 (`linux/arm64`) support across both **ARM64 Linux nodes (including openSUSE MicroOS K3s nodes)** and **macOS Apple Silicon (`darwin/arm64`, `home` region) nodes**:
- **Strict Native-Only Building (Zero Cross-Compilation / Zero QEMU)**: Enforce across `BuildController`, `luma/build_capabilities.py`, and Tekton/BuildKit compilation that `linux/arm64` images **must** be built on a native `arm64` BuildKit endpoint (either an in-cluster/MicroOS `linux/arm64` builder or the `home` macOS Apple Silicon builder over Tailscale mTLS), and `linux/amd64` images **must** be built on a native `amd64` builder. Explicitly reject any cross-compilation or QEMU emulation fallback, and automatically infer `linux/arm64` when targeting an `arm64` node or `home` region if platform is not overridden.
- **ARM64 Runtime Architecture Affinity & MicroOS Self-Healing**: Ensure `compile_service_release` and `compile_compose_release` emit `kubernetes.io/arch` (`arm64` vs `amd64`) in `nodeSelector` matching the resolved `ServiceImageArtifact.platform`, reject mixed-architecture pod sidecar mismatches, and harden `evaluate_node_self_heal` + `node-self-heal-daemonset.yaml` for openSUSE MicroOS transactional/read-only root filesystems (writable `/etc/rancher/k3s` & `/var/lib/rancher/k3s`, `/var/lib/buildkit`, SELinux labels, and btrfs/thin-pool maintenance) as well as macOS `<local>` `NO_PROXY` alignment.

### R3. Completeness & Reliability of the 4-Step Zero-Downtime Migration Plan & Engine
Audit and harden the 4-step migration workflow (`luma migrate plan -> shadow -> cutover -> abort -> finalize` in `luma/paas/migration/migrator.py`, `luma/cli/paas.py`, and `docs/migration-guide*.md`) so it is production-safe across all 3 rollout batches:
- **Batch 2 Queue Worker Race Prevention (`serverx`, `granary`, `codex-gitea`)**: Prevent dual-consumer queue races during `shadow` by keeping queue/background worker replicas at `0` (or paused) in the shadow K3s release until worker drain/cutover, while probing web/API deployments in shadow mode, or providing explicit two-phase worker/web cutover orchestration.
- **Batch 3 Stateful Volume & Secret Preflight (`kato`, `bot`)**: Enforce strict pre-shadow and pre-cutover validation that stateful volumes have an explicit `node` pin matching the legacy Nomad allocation host, preserve hostPath + `fsGroup`/UID/GID without data copy, verify secret + mounted secret file parity, and verify shadow release readiness (`gateway_probe_ok` and matching image architecture) before `cutover` is permitted.

### R4. Controlled Workspace & Sandbox Execution
All temporary build outputs (such as `scripts/build-site.py --output`) must be written inside `/Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/` or the workspace `/Users/turning4th/infra-stacks` (never `/tmp`), and all repository commands must use `./.venv/bin/python` (`PATH="$PWD/.venv/bin:$PATH"`).

## Verification Resources

- Full repository gate: `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh`
- PaaS engine unit tests: `./.venv/bin/python -m unittest tests.test_paas_engine -v`
- Bilingual static site build & anchor/codeblock parity check:
  `./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site`

## Acceptance Criteria

### Audit Findings & Direct Fixes
- [ ] A structured adversarial audit report is produced detailing every issue/gap found across implementation, ARM/MicroOS handling, and migration reliability, along with the exact code/doc fixes applied.
- [ ] All identified code, CLI, dashboard, and documentation gaps are fixed directly in `/Users/turning4th/infra-stacks`.

### Native ARM64 & openSUSE MicroOS Guarantees
- [ ] `BuildController` and manifest compilation strictly enforce native-only builds (`linux/arm64` on native ARM64 MicroOS or macOS builders; `linux/amd64` on native AMD64 builders) with zero QEMU cross-compilation, and automatically infer `linux/arm64` when targeting an `arm64` node.
- [ ] `compile_service_release` and `compile_compose_release` set `kubernetes.io/arch` (`arm64` / `amd64`) in `nodeSelector` derived from the artifact's `platform` and raise `LumaError` if a main container and its `network_mode: service:*` sidecar have mismatched architectures or if the target node's architecture conflicts with the image platform.
- [ ] `evaluate_node_self_heal` and `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` explicitly handle openSUSE MicroOS (`osName: "opensuse-microos"` / transactional rootfs / btrfs scrub & trim / writable `/var/lib` paths) as well as Debian/Ubuntu and macOS (`darwin`).

### Migration Reliability & Automated Gate Verification
- [ ] `MigrationEngine` prevents Batch 2 shadow queue-worker double-consumption (shadowing web replicas while keeping queue worker replicas at `0` until worker cutover) and blocks `cutover` if shadow health probes fail, secrets are out of sync, or Batch 3 stateful volumes lack a node pin.
- [ ] Unit tests in `tests/test_paas_engine.py` verify all new ARM64/MicroOS, architecture-mismatch, and migration safety invariants.
- [ ] `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh` and `scripts/check-site.py` pass with exit code `0`.

## Follow-up — 2026-10-15T18:10:55Z

URGENT USER FEEDBACK & REQUIREMENTS UPDATE — Please forward immediately to the Orchestrator (`1387c386-51c1-4a2c-b02a-a266794155bd`) and incorporate into M4/M5/M6:

The user reviewed our progress and raised two critical architectural & product requirements:

1. **Migration is a ONE-TIME operation (Do NOT pollute the permanent CLI surface, and cover real cluster/node migration hazards)**:
   - The user noted: "我看你是把这些迁移步骤写到了命令里，还是写到了文件里？因为这是迁移操作，我只用一次。理论上迁移并验证整个业务没问题之后，这些东西就不需要了。我后续把这些节点集群迁移过来要注意什么？"
   - **Action Required**:
     a) Provide a standalone, self-contained one-time migration script (`scripts/migrate-nomad-to-k3s.py`) that the operator can run once to migrate existing Nomad workloads to K3s and then discard/ignore after migration is complete.
     b) In `luma/cli/parser.py`, hide `migrate` from the public `COMMAND_GROUPS` / top-level `--help` (using `argparse.SUPPRESS` on the subparser or keeping it out of `COMMAND_GROUPS` and regenerated `docs/luma-cli-reference*.md`) so new open-source users do not see one-time legacy Nomad migration commands cluttering `luma --help`! (Keep the handler working if invoked or via `scripts/migrate-nomad-to-k3s.py`).
     c) In `luma/paas/migration/migrator.py` and `docs/migration-guide*.md`, explicitly address real-world node & workload migration hazards:
        - **Same-node coexistence & port conflicts**: Edge Traefik (`80`/`443` and `tcp-relay` ports) must NOT conflict during migration; transition traffic via Traefik dynamic routing without binding duplicate host ports `80`/`443`. Check node memory headroom before starting shadow Pods on the same host.
        - **Stateful SQLite / flock volumes (`kato`, `bot`) & single-instance bots**: Must NEVER simultaneously mount and write the same hostPath volume from both Nomad and K3s (which corrupts SQLite WAL/flock) or run dual Telegram/Feishu bot long-polling connections! For stateful/exclusive-lock apps, `shadow` must only pre-pull images, sync secrets, and validate hostPath/node-pin with `replicas: 0`, and `cutover` must stop the Nomad allocation BEFORE scaling the K3s Deployment to `1`.
        - **Post-migration cleanup**: Document and script the final one-time Nomad decommission step (`systemctl disable --now nomad`, cleanup) once all workloads are verified.

2. **Open-Source Developer Experience (0-to-1 Setup, `luma bootstrap`, Cloudflare & `.env` 100% Compatibility)**:
   - The user noted: "因为我们这是一个开源项目，这次重构还是比较大的，需要关注一下开发者友好度。比如，新用户从 0 开始安装我们的东西能不能跑通？你这里面改的东西，会不会影响最开始的 setup 流程？包括 Cloudflare 那些东西，这些配置都变了吗？"
   - **Action Required**:
     a) Verify and guarantee **zero breaking changes** to the 0-to-1 setup flow (`luma bootstrap --domain ...`, `luma login`, `luma init`, `luma node join`, `luma manager cloudflare --zone ...`, `luma manager egress`, `.env` settings `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, `EGRESS_SUBSCRIPTION_URL`, `LUMA_DEPLOY_TOKEN`).
     b) Ensure Cloudflare DNS & Cloudflare Tunnel integration in `luma/cloudflare.py` and `luma/paas/release/helm_compiler.py` works seamlessly with Luma's existing Cloudflare configuration (`luma manager cloudflare --zone ...` / `CLOUDFLARE_API_TOKEN`) rather than requiring users to manually configure Kubernetes `external-dns` or `cloudflared` CRDs!
     c) Document the 0-to-1 fresh install experience vs. existing user compatibility clearly in `docs/migration-guide*.md` and `docs/build-and-artifacts*.md`.

