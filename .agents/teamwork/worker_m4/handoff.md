# Milestone 4 Implementation Handoff Report: Node Self-Healing & Zero-Downtime Migration (Feature 7 & Feature 8)

## 1. Observation

### 1.1 Direct Observations & Baseline State
Prior to Milestone 4 implementation, direct inspection of the codebase revealed several architectural gaps across node self-healing, migration safety, CLI visibility, and automated test coverage:
1. **`deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`**:
   - Lacked `nodeSelector: kubernetes.io/os: linux`. In hybrid clusters with Darwin (macOS Apple Silicon) nodes, Kubernetes would attempt to schedule the Alpine Linux daemonset container on Darwin nodes, causing container execution errors.
   - Mounted `/host/etc` and `/host/var/run`, but omitted host `/var` (`/host/var`), preventing reconciliation of host `/var/lib/rancher/k3s` and `/var/lib/buildkit`.
   - Lacked openSUSE MicroOS detection (`/host/etc/os-release`), Btrfs thin-pool scrub (`btrfs scrub start -B /var`), `/var` fstrim (`fstrim -v /var`), and SELinux context restoration (`restorecon -Rv /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/buildkit`).
2. **`luma/paas/migration/migrator.py`**:
   - `evaluate_node_self_heal` only emitted generic `"executed containerd image GC and fstrim -v / for PVE thin pool"` without Btrfs maintenance or SELinux `restorecon` repair on MicroOS.
   - Darwin self-healing did not normalize multi-separator `NO_PROXY` strings or explicitly ensure `<local>` and `localhost` were present, nor did it repair direct Tailscale mTLS routes.
   - `NodeSelfHealReport` lacked dictionary mapping access (`__getitem__`, `get`), `no_proxy_canonical`, and `read_only_rootfs` fields.
   - `MigrationEngine.get_compiled_release(release_id)` was missing, causing `AttributeError` when inspectable compiled releases were requested.
   - `MigrationEngine.shadow()` lacked volume passthrough (`existing_volumes`), gateway probe overrides (`gateway_probe_ok`, `gateway_probe`), replica overrides, and available memory headroom enforcement (<512MB).
   - In `shadow()`, multi-service Compose background workers (Batch 2) were scheduled with default replicas (>= 1) instead of `replicas: 0`, risking dual-consumer message races against active Nomad workers.
   - In `shadow()`, stateful SQLite / flock workloads and single-instance bots (`kato`, `bot`) were scheduled with `replicas: 1` instead of `replicas: 0`, risking concurrent hostPath volume mounting and SQLite WAL shared-memory corruption.
   - In `cutover()`, pre-cutover safety gates were missing: failed gateway health probes, missing explicit volume `node` pins, architecture mismatches, and image digest drift did not abort cutovers. Furthermore, Nomad allocations were not stopped before scaling K3s deployments.
   - In `abort()`, finalized migrations were not blocked from aborting.
3. **`scripts/migrate-nomad-to-k3s.py`**:
   - The standalone operator migration script did not exist.
4. **`luma/cli/parser.py`**:
   - `("migrate", ...)` was listed in public `COMMAND_GROUPS` under `"Operate"`, exposing one-time migration commands to open-source users in `luma --help` and generated documentation.
5. **`tests/test_paas_engine.py`**:
   - Suite 4 (`MigrationReliabilityAndNodeSelfHealingTests`) was completely missing from the test suite.

### 1.2 Tool Executions & Verbatim Test Results
- **Unit Test Execution (Suite 4)**:
  `./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v`
  Result:
  ```
  Ran 28 tests in 0.019s
  OK
  ```
- **Module Alias Verification**:
  `./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests -v`
  Result:
  ```
  Ran 28 tests in 0.020s
  OK
  ```
- **PaaS Engine & Native Builders Combined**:
  `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`
  Result:
  ```
  Ran 133 tests in 0.598s
  OK
  ```
- **Full Repository Test Discovery**:
  `./.venv/bin/python -m unittest discover -s tests -p "test_*.py"`
  Result:
  ```
  Ran 1685 tests in 36.748s
  OK
  ```
- **Repository Linting**:
  `./.venv/bin/python -m ruff check luma tests scripts`
  Result:
  ```
  All checks passed!
  ```
- **CLI Reference Check**:
  `./.venv/bin/python scripts/generate-cli-reference.py --check`
  Result: Returncode 0.
- **Static Site & Documentation Verification**:
  `./.venv/bin/python scripts/build-site.py --output /tmp/luma-site-test && ./.venv/bin/python scripts/check-site.py /tmp/luma-site-test`
  Result:
  ```
  Built site and 46 documentation pages in /private/tmp/luma-site-test
  Validated local links and assets across 48 HTML pages; bilingual sections match.
  ```
- **Standalone Migration Script Dry-Run**:
  `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch all`
  Result: Returncode 0; executed preflight checks, planned batches 1-3, verified zero mutations.
- **CLI Help Verification**:
  `./.venv/bin/python -c "from luma.cli import build_parser; p = build_parser(); p.print_help()"`
  Result: `migrate` is omitted from `Operate:` commands in top-level help, while `luma migrate --help` remains functional.

---

## 2. Logic Chain

1. *Observation 1.1.1 -> Remediation in DaemonSet*:
   Because Darwin nodes running ARM64 macOS join the cluster as remote builders, applying a privileged Linux Alpine DaemonSet to all nodes without a `nodeSelector` causes scheduler failure on Darwin. Adding `nodeSelector: kubernetes.io/os: linux` guarantees execution strictly on Linux worker nodes. Mounting host `/var` (`/host/var`) enables the container script to verify writable subvolumes (`/etc/rancher/k3s`, `/var/lib/rancher/k3s`, `/var/lib/buildkit`) and run `btrfs scrub start -B /var` and `fstrim -v /var` on MicroOS nodes. Checking `/host/etc/os-release` allows automatic detection of openSUSE MicroOS and invocation of SELinux `restorecon -Rv` on modified configuration and cache directories.
2. *Observation 1.1.2 -> Remediation in `evaluate_node_self_heal`*:
   On openSUSE MicroOS, the root filesystem is mounted read-only via transactional Btrfs snapshots, and SELinux is enforcing. The doctor function `evaluate_node_self_heal` now detects `opensuse-microos`, validates writable subvolumes, appends `restorecon on /etc/rancher/k3s/registries.yaml` when reconciling registries, and generates `btrfs scrub start -B /var and fstrim -v /var` actions when disk usage is >= 75% or `last_fstrim_at == 0`. For Darwin nodes, `canonicalize_no_proxy` parses mixed commas, semicolons, and spaces, ensuring `<local>`, `localhost`, and `127.0.0.1` are present to bypass egress proxies for local and mDNS routing, and repairs direct Tailscale routes to the Zot registry (`100.66.177.70:5000`).
3. *Observation 1.1.2 -> Remediation in `MigrationEngine.shadow()`*:
   - Memory Headroom: To prevent node OOM crashes when co-locating shadow K3s pods with legacy Nomad containers on the same physical host, `shadow()` verifies that available memory is >= 512MB, raising `LumaError("insufficient node memory headroom for shadow release")` otherwise.
   - Port Coexistence: Workloads compiled for shadow releases use Traefik dynamic Ingress routing without binding duplicate `hostPort: 80` or `hostPort: 443`, preventing bind collisions.
   - Batch 2 Worker Isolation: In multi-service Compose applications (`serverx`, `granary`, `codex-gitea`), running background/queue workers concurrently with Nomad workers causes dual consumers to compete for the same message queue items, resulting in duplicate processing or message corruption. Setting `shadow_replicas[worker] = 0` while web/API runs at target replicas allows edge health probing without message queue competition.
   - Batch 3 SQLite / flock Isolation: Single-instance services (`kato`, `bot`) with local SQLite databases cannot have their `hostPath` mounted simultaneously by both Nomad and K3s; doing so corrupts SQLite WAL index (`.shm`) files and file locks, and causes bot polling to fail with HTTP 409 Conflict. Setting `shadow_replicas = 0` for stateful services ensures images are pre-pulled and secrets synced without touching active disk data.
4. *Observation 1.1.2 -> Remediation in `MigrationEngine.cutover()`*:
   Before switching ingress traffic:
   - Gate 1: Verifies `gateway_probe_ok is True` on the shadow release, raising `LumaError("gateway_probe_ok is False")` if health probing failed.
   - Gate 2: Verifies that every adopted volume has an explicit `node` pin matching the legacy Nomad host, raising `LumaError("node pin")` to prevent data loss across nodes.
   - Gate 3: Verifies that image platforms match target node architectures, raising `LumaError("architecture mismatch")`.
   - Gate 4: Compares current running image digests against shadow digests, raising `LumaError("image digest drift detected")` if upstream Nomad images drifted during observation.
   - Gate 5: Verifies secret parity (`syncedSecrets`), raising `LumaError("secret parity failure")` if secrets are missing or inconsistent.
   - Two-Phase Cutover Execution: Nomad allocation is stopped FIRST (`legacyNomadAllocationStopped = True`, `legacyNomadWorkerStatus = "drained"`), ensuring SQLite WAL and message consumer locks are fully released before K3s Deployments are scaled to their target replicas.
5. *Observation 1.1.3 & 1.1.4 -> Standalone Script & CLI Cleanup*:
   - Created `scripts/migrate-nomad-to-k3s.py` as a standalone operator tool orchestrating preflight checks, batch planning, shadow deployment, cutover, and final Nomad decommissioning (`systemctl disable --now nomad`).
   - In `luma/cli/parser.py`, removed `migrate` from `COMMAND_GROUPS` and registered the subparser with `help=argparse.SUPPRESS`. This ensures `luma --help` and auto-generated CLI reference documentation remain clean for standard open-source users, while preserving `luma migrate ...` for operators.
6. *Observation 1.1.5 -> Suite 4 Test Suite*:
   Implemented all 28 unit tests in `tests/test_paas_engine.py` covering Feature 7 (TC-F7-01 to TC-F7-05, TC-B-F7-01 to TC-B-F7-05), Feature 8 (TC-F8-01 to TC-F8-05, TC-B-F8-01 to TC-B-F8-05), Cross-feature invariants (TC-X-02, TC-X-08), and DaemonSet manifest verification. Aliased `MigrationReliabilityAndMicroOSSelfHealTests = MigrationReliabilityAndNodeSelfHealingTests` and updated `__getattr__` for complete runner interoperability.

---

## 3. Caveats

- **Host Service Management**: `scripts/migrate-nomad-to-k3s.py` attempts to execute `systemctl disable --now nomad`. When executed in non-root or non-systemd environments (e.g. macOS dev workstations or containerized CI), it catches `FileNotFoundError` and non-zero exit codes, printing manual instructions for operators rather than failing catastrophically.
- **Exclusive File Ownership**: Only files assigned under exclusive write ownership (`deploy/paas-stack/k8s/node-self-heal-daemonset.yaml`, `luma/paas/migration/migrator.py`, `scripts/migrate-nomad-to-k3s.py`, `luma/cli/parser.py`, `tests/test_paas_engine.py`) were modified.
- **Documentation Parity**: Generated CLI reference and static site documentation remain 100% in sync; no docs files were mutated outside the assigned ownership scope.

---

## 4. Conclusion

Milestone 4 (M4: Node Self-Healing & Zero-Downtime Migration) is completely implemented, verified, and ready for integration:
1. **DaemonSet**: `deploy/paas-stack/k8s/node-self-heal-daemonset.yaml` enforces Linux node scheduling, mounts `/host/var`, detects openSUSE MicroOS, performs Btrfs scrub and trim, and restores SELinux contexts.
2. **Self-Healing Doctor**: `evaluate_node_self_heal` canonicalizes NO_PROXY, handles MicroOS transactional rootfs, and aligns Darwin Apple Silicon network routing.
3. **Migration Engine**: `MigrationEngine` enforces memory headroom, isolates Batch 2 queue workers (replica 0) and Batch 3 SQLite/flock single-instance workloads (replica 0), validates 5 pre-cutover gates, and stops Nomad before scaling K3s.
4. **CLI Cleanliness**: `luma/cli/parser.py` hides `migrate` from top-level help while maintaining full backward compatibility.
5. **Standalone Script**: `scripts/migrate-nomad-to-k3s.py` provides self-contained preflight and batch rollout workflows.
6. **Test Suite**: All 28 tests in Suite 4 pass, bringing total passing tests to 133 in PaaS engine/builder suites and 1685 across the entire repository with zero failures and clean ruff linting.

---

## 5. Verification Method

To independently verify this milestone:

1. **Run Suite 4 Tests Directly**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine.MigrationReliabilityAndMicroOSSelfHealTests -v
   ```
   *Expected*: 28 tests run and pass with `OK`.

2. **Run PaaS Engine and Native Builder Test Suites**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected*: 133 tests run and pass with `OK`.

3. **Verify Linting**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" ruff check luma tests scripts
   ```
   *Expected*: `All checks passed!`

4. **Verify Standalone Migration Script**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python scripts/migrate-nomad-to-k3s.py --dry-run --batch all
   ```
   *Expected*: Exit code 0, preflight and planning success for batches 1, 2, and 3.

5. **Verify CLI Help Visibility**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" python -c "from luma.cli import build_parser; p = build_parser(); p.print_help()"
   ```
   *Expected*: `migrate` is absent from the command list in top-level help.

6. **Full Repository CI Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected*: Exit code 0.

### Invalidation Conditions
- If `evalute_node_self_heal` does not contain `"btrfs"` or `"restorecon"` in `repaired_actions` for `opensuse-microos`.
- If `shadow()` sets `replicas > 0` for queue workers in Batch 2 or single-instance stateful apps (`kato`, `bot`) in Batch 3.
- If `cutover()` proceeds without prior `shadow()`, when `gateway_probe_ok=False`, or when an adopted volume lacks an explicit `node` pin.
- If `luma --help` displays `migrate` under public commands.
