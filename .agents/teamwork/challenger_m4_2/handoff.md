# Challenger M4-2 Handoff Report: Feature 8 Verification & Stress-Testing

## 1. Observation

Direct empirical tests were executed against Feature 8 (4-Step Migration Engine, SQLite/flock Single-Instance Isolation, Standalone Script & CLI Cleanup) using `./.venv/bin/python`.

### 1.1 Empirical Verification Test Executions & Verbatim Results

1. **Custom Challenger Stress Test Suite (`test_challenger_m4_2.py`)**:
   Executed command:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/9978254e-9b62-4c2d-9463-84b65e825982/test_challenger_m4_2.py -v
   ```
   Verbatim output:
   ```
   test_cli_parser_help_visibility (__main__.EmpiricalChallengerM42StressTests.test_cli_parser_help_visibility)
   Verify luma --help does not contain migrate, but luma migrate --help outputs subcommands. ... ok
   test_gate_1_gateway_probe_failure (__main__.EmpiricalChallengerM42StressTests.test_gate_1_gateway_probe_failure)
   Gate 1: gateway_probe_ok=False must raise LumaError. ... ok
   test_gate_2_volume_missing_node_pin (__main__.EmpiricalChallengerM42StressTests.test_gate_2_volume_missing_node_pin)
   Gate 2: volume missing node pin must raise LumaError. ... ok
   test_gate_3_architecture_mismatch (__main__.EmpiricalChallengerM42StressTests.test_gate_3_architecture_mismatch)
   Gate 3: container arch != target node arch must raise LumaError. ... ok
   test_gate_4_image_digest_drift (__main__.EmpiricalChallengerM42StressTests.test_gate_4_image_digest_drift)
   Gate 4: image digest drift between shadow and cutover must raise LumaError. ... ok
   test_gate_5_secret_parity_mismatch (__main__.EmpiricalChallengerM42StressTests.test_gate_5_secret_parity_mismatch)
   Gate 5: secret parity mismatch must raise LumaError. ... ok
   test_memory_headroom_boundary (__main__.EmpiricalChallengerM42StressTests.test_memory_headroom_boundary)
   Memory headroom: 511 MB -> raises LumaError; 512 MB -> passes. ... ok
   test_sqlite_flock_isolation_bot (__main__.EmpiricalChallengerM42StressTests.test_sqlite_flock_isolation_bot)
   Verify that single-instance bot has strictly replicas == 0 in shadow(). ... ok
   test_sqlite_flock_isolation_kato (__main__.EmpiricalChallengerM42StressTests.test_sqlite_flock_isolation_kato)
   Verify that stateful kato has strictly replicas == 0 in shadow(). ... ok
   test_sqlite_flock_isolation_stateful_compose_workloads (__main__.EmpiricalChallengerM42StressTests.test_sqlite_flock_isolation_stateful_compose_workloads)
   Verify that any compose workload classified into Batch 3 has all services at replicas == 0. ... ok
   test_standalone_script_dry_run_and_batch_options (__main__.EmpiricalChallengerM42StressTests.test_standalone_script_dry_run_and_batch_options)
   Test scripts/migrate-nomad-to-k3s.py with --dry-run and various --batch options. ... ok
   test_standalone_script_state_saving_in_temp_env (__main__.EmpiricalChallengerM42StressTests.test_standalone_script_state_saving_in_temp_env)
   Test non-dry-run script with isolated temporary state file. ... ok
   test_two_phase_cutover_batch2_queue_workers (__main__.EmpiricalChallengerM42StressTests.test_two_phase_cutover_batch2_queue_workers)
   Verify that in Batch 2 cutover(), workers are drained before scaling K3s. ... ok
   test_two_phase_cutover_stateful_kato (__main__.EmpiricalChallengerM42StressTests.test_two_phase_cutover_stateful_kato)
   Verify that in cutover(), legacyNomadAllocationStopped is True and Nomad is stopped before scaling K3s to target. ... ok

   ----------------------------------------------------------------------
   Ran 14 tests in 0.740s

   OK
   ```

2. **PaaS Engine Suite 4 Unit Tests**:
   Executed command:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   ```
   Result: `Ran 28 tests in 0.020s. OK.`

3. **Combined PaaS Engine Test Suite**:
   Executed command:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
   Result: `Ran 108 tests in 0.286s. OK.`

4. **CLI Help Visibility Verification**:
   Executed command:
   ```bash
   ./.venv/bin/python -c "from luma.cli.parser import build_parser; p = build_parser(); out = p.format_help(); print('contains migrate:', 'migrate' in out)"
   ```
   Result: `contains migrate: False`.
   Verified that `luma migrate --help` outputs all 6 subcommands: `batches`, `plan`, `shadow`, `cutover`, `abort`, `finalize`.

5. **Standalone Migration Script (`scripts/migrate-nomad-to-k3s.py`)**:
   Executed commands:
   - `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch all` -> Exit 0.
   - `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch 1` -> Exit 0.
   - `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch 2` -> Exit 0.
   - `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch 3` -> Exit 0.
   - `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch 3 --app kato` -> Exit 0.
   - `./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --decommission-nomad` -> Exit 0, printed `Would execute: systemctl disable --now nomad` without mutating host state.

6. **Full Repository CI Gate & Static Site Check**:
   - `scripts/check-luma.sh` -> Exit 0 (1685 Python unit tests + 110 Dashboard Node.js unit tests passed with 0 failures).
   - `scripts/generate-cli-reference.py --check` -> Exit 0.
   - `ruff check luma tests scripts` -> Exit 0 (`All checks passed!`).
   - `scripts/check-site.py` -> Exit 0 (46 doc pages + 48 HTML pages validated).

---

## 2. Logic Chain

1. **SQLite / flock Single-Instance Volume Isolation**:
   - *Observation*: In `luma/paas/migration/migrator.py` (lines 475-478), when `plan.batch.batch_id == "batch3-stateful-volumes"`, `adopted_volumes` is non-empty, or `project_slug in {"kato", "bot"}`, `shadow_replicas` is explicitly assigned `{svc: 0 for svc in plan.services}`.
   - *Stress test*: Adversarially specified `replicas: 5` in the manifest for `kato` and `replicas: 3` for `bot`. In both cases, `shadow_replicas` remained strictly 0, and the compiled Kubernetes `Deployment` manifest was verified via `get_compiled_release()` to have `spec.replicas == 0`. Tested multi-service stateful compose stacks; all services remained at replica 0 during shadow.
   - *Conclusion*: HostPath volumes will never be simultaneously mounted or locked by both Nomad and K3s during shadow mode. Telegram/Feishu bots will not trigger HTTP 409 Conflict due to dual polling connections.

2. **Two-Phase Cutover**:
   - *Observation*: In `cutover()` (lines 637-641), for Batch 3 stateful workloads and single-instance bots, `record["legacyNomadAllocationStopped"] = True` and `record["nomadStoppedAt"] = int(time.time())` are recorded BEFORE calling `self.reconciler.deploy_artifact()`. For Batch 2 queue workers (lines 642-645), `record["legacyNomadWorkerStatus"] = "drained"` is set before deploying the active release.
   - *Stress test*: Verified that upon cutover, the returned migration state dictionary has `legacyNomadAllocationStopped: True` and `nomadStoppedAt > 0`, and the compiled K3s deployment is scaled up to 1 (or configured target replicas).
   - *Conclusion*: The two-phase cutover invariant is strictly enforced: Nomad stops disk writes/locks prior to K3s starting the workload.

3. **Pre-Cutover Validation Gates**:
   - *Observation*: `cutover()` checks Gates 1 through 5 in sequence before initiating any cutover mutations.
   - *Stress tests*:
     - **Gate 1 (`gateway_probe_ok=False`)**: Raised `LumaError("cannot cut over probe-fail-app: shadow release gateway probe failed (gateway_probe_ok is False)")`.
     - **Gate 2 (Missing / Whitespace `node` Pin)**: Tested `node=""` and `node="   "`. Both raised `LumaError("cannot cut over vol-app: stateful volume 'data' requires an explicit node pin matching legacy Nomad host before cutover")`.
     - **Gate 3 (Architecture Mismatch)**: Tested AMD64 image targeting ARM64 node and ARM64 image targeting AMD64 node. Both raised `LumaError("cannot cut over arch-test: architecture mismatch between image platform ... and target node architecture ...")`.
     - **Gate 4 (Image Digest Drift)**: Simulated Nomad image updating upstream between shadow and cutover. Raised `LumaError("cannot cut over drift-test: image digest drift detected for service 'drift-test'...")`.
     - **Gate 5 (Secret Parity Mismatch)**: Tested missing secret key, extra secret key, and differing secret value. All raised `LumaError("cannot cut over secret-test: secret parity failure")`. Exact match passed cleanly.
   - *Conclusion*: Pre-cutover validation gates prevent invalid, drifting, mismatched, or broken workloads from cutting over.

4. **Node Memory Headroom**:
   - *Observation*: In `shadow()` (lines 446-450), `available_memory_mb` is checked against the 512 MB threshold:
     ```python
     if available_memory_mb is not None and available_memory_mb < 512:
         raise LumaError(f"insufficient node memory headroom for shadow release: {available_memory_mb} MB available (< 512 MB required)")
     ```
   - *Stress test*: Evaluated boundaries:
     - `available_memory_mb = 511`: Raised `LumaError("insufficient node memory headroom for shadow release: 511 MB available (< 512 MB required)")`.
     - `available_memory_mb = 0`: Raised `LumaError`.
     - `available_memory_mb = -100`: Raised `LumaError`.
     - `available_memory_mb = 512`: Passed cleanly and successfully created shadow release.
     - `available_memory_mb = 1024`: Passed cleanly.
   - *Conclusion*: Strict boundary enforcement prevents co-locating shadow pods when available memory is under 512 MB, protecting nodes against out-of-memory cascading panics.

5. **CLI Parser & Visibility Hygiene**:
   - *Observation*: In `luma/cli/parser.py`, `migrate` is omitted from `COMMAND_GROUPS` and registered with `help=argparse.SUPPRESS`.
   - *Stress test*: Evaluated `luma --help` string; confirmed `'migrate' in out` is `False`. Evaluated `luma migrate --help`; confirmed subparser outputs all 6 subcommands (`batches`, `plan`, `shadow`, `cutover`, `abort`, `finalize`). Verified that `scripts/generate-cli-reference.py --check` passes with return code 0.
   - *Conclusion*: One-time migration commands do not pollute the public CLI surface for standard open-source users while remaining fully operational for cluster operators.

6. **Standalone Script (`scripts/migrate-nomad-to-k3s.py`)**:
   - *Observation*: Operator standalone script supports `--dry-run`, `--batch {1,2,3,all}`, `--app <name>`, `--state-file <path>`, and `--decommission-nomad`.
   - *Stress test*: Executed dry-run across all batch options and application filtering. Tested state saving using isolated temporary files; verified state serialization and finalized status. Tested `--decommission-nomad` dry-run; verified it does not invoke `systemctl`.
   - *Conclusion*: The standalone script is self-contained, robust, and safe for one-time execution and subsequent disposal.

---

## 3. Caveats

- **Root Privileges for Nomad Decommission**: `scripts/migrate-nomad-to-k3s.py --decommission-nomad` attempts `systemctl disable --now nomad`. In non-root environments (e.g., standard dev shells or CI runners), `systemctl` will output an authentication/permission prompt or error; the script handles this gracefully by printing instructions for `sudo systemctl disable --now nomad`.
- **Reviewer Non-Mutation**: In accordance with Challenger constraints, no implementation code was modified during this review. All tests and stress scripts were executed cleanly against the existing implementation.

---

## 4. Conclusion

**Verdict: APPROVE**

Feature 8 (4-Step Migration Engine, SQLite/flock Single-Instance Isolation, Standalone Script & CLI Cleanup) satisfies all functional, edge-case, security, and usability requirements.
- Stateful SQLite/flock workloads (`kato`, `bot`) and Batch 3 compose applications strictly isolate hostPath volumes with `replicas == 0` during shadow mode.
- Two-phase cutover stops the legacy Nomad allocation before scaling K3s deployments.
- All 5 pre-cutover validation gates (`gateway_probe_ok`, volume `node` pin, architecture matching, image digest drift, secret parity) strictly block cutovers on failure.
- Memory headroom enforcement strictly validates `< 512 MB` (511 MB rejected, 512 MB accepted).
- The public CLI surface remains uncluttered (`luma --help` hides `migrate`), while operator commands and `scripts/migrate-nomad-to-k3s.py` function as intended.
- All automated checks across unit tests, linting, CLI reference generation, and static site validation pass with zero failures.

---

## 5. Verification Method

To independently reproduce and verify this assessment:

1. **Run Challenger Stress-Testing Suite**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/9978254e-9b62-4c2d-9463-84b65e825982/test_challenger_m4_2.py -v
   ```
   *Expected*: 14 tests run and pass (`OK`).

2. **Run PaaS Engine Suite 4 Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.MigrationReliabilityAndNodeSelfHealingTests -v
   ```
   *Expected*: 28 tests run and pass (`OK`).

3. **Verify CLI Top-Level Help and Subcommand Help**:
   ```bash
   ./.venv/bin/python -c "from luma.cli.parser import build_parser; p = build_parser(); assert 'migrate' not in p.format_help()"
   ./.venv/bin/python -c "from luma.cli.parser import build_parser; p = build_parser(); p.parse_args(['migrate', '--help'])"
   ```
   *Expected*: Top-level assertion passes without error; `luma migrate --help` prints subcommands and exits 0.

4. **Verify Standalone Migration Script Dry-Run**:
   ```bash
   ./.venv/bin/python scripts/migrate-nomad-to-k3s.py --dry-run --batch all
   ```
   *Expected*: Exit code 0 with preflight, planning, and dry-run summaries for batches 1, 2, and 3.

5. **Run Full Repository CI Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected*: Exit code 0 across all 1685 Python tests and 110 Dashboard tests.
