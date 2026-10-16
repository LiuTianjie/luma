## 2026-10-15T18:37:41Z
You are Worker M4.
Your working directory is: /Users/turning4th/infra-stacks/.agents/teamwork/worker_m4
Your parent is the Project Orchestrator (Conversation ID: 1387c386-51c1-4a2c-b02a-a266794155bd).
Project root: /Users/turning4th/infra-stacks

MANDATORY: Read the authoritative user request at:
/Users/turning4th/infra-stacks/.agents/teamwork/ORIGINAL_REQUEST.md
Read the project architecture and milestone plan at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/PROJECT.md
Read DISPATCH.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/orchestrator/DISPATCH.md
Read TEST_INFRA.md at:
/Users/turning4th/infra-stacks/.agents/teamwork/TEST_INFRA.md
Read the thorough explorer blueprints at:
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_1/handoff.md (Feature 7: openSUSE MicroOS & Darwin Node Self-Healing & DaemonSet YAML)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_2/handoff.md (Feature 8: Migration Engine, SQLite/flock Single-Instance Isolation, Standalone Script, CLI Cleanup)
- /Users/turning4th/infra-stacks/.agents/teamwork/explorer_m4_3/handoff.md (Suite 4 Unit Test Suite Implementation with 28 test methods)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT
hardcode test results, create dummy/facade implementations, or
circumvent the intended task. A teamwork_preview_auditor will independently
verify your work. Integrity violations WILL be detected and your
work WILL be rejected.

File Write Ownership (You own these files exclusively):
- `luma/paas/migration/migrator.py`
- `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`
- `scripts/migrate-nomad-to-k3s.py` (new standalone one-time migration script)
- `luma/cli/parser.py` (hide `migrate` from public COMMAND_GROUPS / --help)
- `tests/test_paas_engine.py` (adding Suite 4 `MigrationReliabilityAndNodeSelfHealingTests`)

Objective & Tasks:
Implement Milestone 4 (M4: Node Self-Healing & Zero-Downtime Migration) covering Feature 7 and Feature 8:

1. `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`:
   - Keep `kind: DaemonSet`, `hostPID: true`, `hostNetwork: true`, tolerations `operator: Exists`, `privileged: true`.
   - Add `nodeSelector: kubernetes.io/os: linux` so it does not schedule on Darwin nodes.
   - Ensure host mounts include `/host/etc`, `/host/var/run`, and `/host/var` (pointing to `/var` on host).
   - In container script:
     - Detect openSUSE MicroOS (`/host/etc/os-release` containing `microos`).
     - On MicroOS: verify writable subvolumes `/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`. Run `btrfs scrub start -B /var` and `fstrim -v /var` when disk usage is high (>=75%). Run `restorecon -Rv /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/buildkit`.

2. `luma/paas/migration/migrator.py`:
   - Feature 7: Enhance `evaluate_node_self_heal`:
     - openSUSE MicroOS (`osName: "opensuse-microos"`): detect transactional rootfs, verify writable paths, generate btrfs scrub/trim and SELinux restorecon remediation commands if disk usage >= 75% or registries misconfigured.
     - macOS Darwin (`osName: "darwin"`): enforce `<local>` in NO_PROXY (with `localhost`, `127.0.0.1`, Tailscale subnet) and verify direct Tailscale route.
   - Feature 8: Enhance `MigrationEngine`:
     - Implement `get_compiled_release(self, release_id: str) -> Any`: return `self.reconciler._compiled_by_release.get(release_id)`.
     - Update `shadow()`:
       - Parameters: `existing_volumes=()`, `gateway_probe_ok=True`, `gateway_probe=None`, `replicas_override=None`, `target_node_arch=None`, `available_memory_mb=None`.
       - Memory headroom: if `available_memory_mb is not None and available_memory_mb < 512`, raise `LumaError("insufficient node memory headroom for shadow release")`.
       - Same-node Traefik port conflict protection: ensure shadow does not bind duplicate hostPort 80/443.
       - Batch 2 queue-worker race prevention: in multi-service Compose applications (`serverx`, `granary`, `codex-gitea`), set worker container `replicas` to `0` during shadow while web/API runs at target replicas and is probed.
       - Batch 3 stateful SQLite / flock & single-instance bot isolation:
         - USER REQUIREMENT: NEVER simultaneously mount and write the same hostPath volume from both Nomad and K3s or run dual bot connections!
         - Stateful/exclusive-lock apps (`kato`, `bot`) must keep `replicas: 0` in `shadow()` (only pre-pull image, sync secrets, validate hostPath/UID/GID and node-pin).
         - Return dictionary must include both `releaseId` and `shadowReleaseId`, `state: "shadow_ready"`, `gatewayProbeOk: gateway_probe_ok`, etc.
     - Update `cutover()`:
       - Pre-cutover validation gates:
         - If `gateway_probe_ok is False`: raise `LumaError("gateway probe failed")`.
         - If stateful volume lacks explicit `node` pin matching legacy Nomad host: raise `LumaError("stateful volume requires explicit node pin")`.
         - If image platform conflicts with target node architecture: raise `LumaError("architecture mismatch")`.
         - If Nomad image digest drifted: raise `LumaError("image digest drift detected")`.
         - If secret parity failed: raise `LumaError("secret parity failure")`.
       - Two-phase execution:
         - Stop legacy Nomad allocation FIRST (`legacyNomadAllocationStopped=True`, `legacyNomadWorkerStatus="drained"`).
         - Scale K3s deployment to configured target replicas (`1` for single-instance/stateful, target replicas for queue workers).
         - Return dictionary with `releaseId`, `state: "cutover_complete"`, `legacyNomadStatus: "standby"`, `cutoverAt`, etc.
     - Update `abort()` and `finalize()`:
       - `abort()`: scale K3s to 0, mark Nomad active, restore traffic. Block `abort()` if state is already `finalized` (`LumaError`).
       - Block `cutover()` without prior `shadow()` (`LumaError`).

3. `scripts/migrate-nomad-to-k3s.py`:
   - Standalone, self-contained one-time migration script.
   - Follow blueprint in `explorer_m4_2/handoff.md §4`.
   - Supports `--dry-run`, `--batch [1,2,3]`, `--project <name>`, `--decommission-nomad`.
   - Performs preflight checks (K3s running, Traefik edge ports 80/443 free, memory headroom >= 512MB, storage path accessibility).
   - Orchestrates plan -> shadow -> probe -> cutover -> decommission (`systemctl disable --now nomad`).
   - Saves state in `.luma-migration-state.json`.

4. `luma/cli/parser.py`:
   - Remove `("migrate", ...)` from the public `COMMAND_GROUPS` tuple.
   - When registering the `migrate` subparser, set `help=argparse.SUPPRESS` so it is hidden from `luma --help` and auto-generated documentation, while remaining functional for legacy invocations.

5. `tests/test_paas_engine.py`:
   - Add Suite 4 `MigrationReliabilityAndNodeSelfHealingTests` (with alias `MigrationReliabilityAndMicroOSSelfHealTests = MigrationReliabilityAndNodeSelfHealingTests`).
   - Implement all 28 unit tests specified in `explorer_m4_3/handoff.md`.
   - Run tests: `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`.
   - Run ruff check: `./.venv/bin/python -m ruff check luma tests scripts`.
   - Verify all tests pass with 0 failures and 0 errors.

6. Report:
   - Write comprehensive report to `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m4/handoff.md`.
   - When finished, send a message to parent via send_message with the handoff path.
