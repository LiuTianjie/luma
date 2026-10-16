# Adversarial Handoff Report: Challenger M2-2 (Feature 4 Stress Testing)

**Challenger**: Challenger M2-2  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/challenger_m2_2`  
**Target Feature**: Feature 4 (Flux <5s Rollback & Secret Parity)  
**Assigned Artifacts**:
- `luma/paas/release/flux_reconciler.py`
- `luma/paas/release/helm_compiler.py`
- `luma/paas/build/artifact_store.py`
- `tests/test_challenger_m2_2.py`
**Explicit Verdict**: `APPROVE`

---

## 1. Observation

1. **`rollback_release` Guards & Active Release Protection**:
   - In `luma/paas/release/flux_reconciler.py:270-294`:
     ```python
     if environment not in VALID_ENVIRONMENTS:
         raise LumaError(
             f"invalid environment {environment!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
         )
     ...
     if target.release_id == active.release_id:
         raise LumaError("cannot rollback to currently active release")
     ```
   - Attempting to roll back to the currently active release ID raises verbatim:
     `LumaError("cannot rollback to currently active release")`.
   - Calling `rollback_release` with a single release in history raises:
     `LumaError("no previous release available to roll back srv-app in prod")`.
   - Calling `rollback_release` with empty history raises:
     `LumaError("no healthy release history found for srv-app in prod")`.
   - Targeting a nonexistent release ID raises:
     `LumaError("target release rel-srv-app-prod-v999-deadbeef not found for srv-app")`.
   - Targeting a failed release (`status == "rolled_back"`) raises:
     `LumaError("target release ... not found for srv-app")` because candidates are strictly filtered by `r.status == "healthy"`.

2. **Secret & Mounted File Preservation Across Rollbacks and Generations**:
   - In `luma/paas/release/flux_reconciler.py:295-313`:
     ```python
     resolved_secrets = (
         dict(secrets)
         if secrets is not None
         else dict(getattr(target, "secrets", {}) or {})
     )
     resolved_mounted_files = (
         dict(mounted_secret_files)
         if mounted_secret_files is not None
         else dict(getattr(target, "mounted_secret_files", {}) or {})
     )
     ```
   - Across a 12-generation cycle of consecutive deploys, rollbacks, and scale operations in `test_repeated_multi_generation_rollback_secret_parity`, runtime secrets and mounted secret files were never lost or wiped to `{}`.
   - Tested complex payloads (multiline PEM certificates, Unicode Chinese text, raw JSON strings, binary base64 strings, symbols `!@#$%^&*()_+-=[]{}|;':,./<>?`~`): all survived byte-for-byte across rollbacks.
   - Verified that `CompiledWorkload.secrets[0]["stringData"]` preserves secret key-values, and `CompiledWorkload.secrets[0]["data"]` contains base64-encoded file payloads with volume mount `defaultMode: 0o400` and `readOnly: True`.
   - Verified in `test_compose_multi_service_secret_and_rollback_preservation` that Compose stacks with multiple independent deployments (`web` and `worker`) both retain the `secretRef` across rollbacks.

3. **`promote_artifact` Validation & Precedence**:
   - In `luma/paas/release/flux_reconciler.py:165-210`:
     ```python
     dest_env = target_env if target_env is not None else to_env
     if from_env not in VALID_ENVIRONMENTS:
         raise LumaError(
             f"invalid source environment {from_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
         )
     if dest_env not in VALID_ENVIRONMENTS:
         raise LumaError(
             f"invalid target environment {dest_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}"
         )
     ```
   - Invalid environments (`qa`, `testing`, `invalid`, `production`, `""`, `dev_1`) are rejected with `LumaError` for `from_env`, `to_env`, and `target_env`.
   - Promoting when source environment has no healthy release raises `LumaError("no healthy release found for ... in staging to promote")`.
   - Secret precedence strictly honors:
     1. `target_secrets` override
     2. `secrets` legacy override
     3. Target environment existing active secrets (when no override provided)
     4. Source environment secrets (when target environment has no existing release)
   - Zero-rebuild guarantee verified: image repository, image digest, and platform in target environment match source environment artifact 100%.

4. **Timestamp Format in `reconcile.fluxcd.io/requestedAt`**:
   - In `luma/paas/release/helm_compiler.py:300-304`:
     ```python
     req_at = reconcile_requested_at or datetime.now(timezone.utc).isoformat()
     helm_annotations: Dict[str, str] = {
         "reconcile.fluxcd.io/requestedAt": req_at,
         **(dict(annotations) if annotations else {}),
     }
     ```
   - Generated timestamps across `compile_service_release`, `compile_compose_release`, `deploy_artifact`, `promote_artifact`, and `rollback_release` were verified against strict ISO 8601 UTC regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$`.
   - Parsed with `datetime.fromisoformat(ts)`: `dt.tzinfo` is present and `dt.utcoffset() == timedelta(0)` (strict UTC offset).

5. **Empirical Test Suite Execution**:
   - `tests/test_challenger_m2_2.py`: 23 adversarial tests executed via `./.venv/bin/python -m unittest tests.test_challenger_m2_2 -v` -> 23 passed, 0 failures, 0 errors in 0.016s.
   - Combined test run: `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders tests.test_challenger_m1 tests.test_challenger_m2_2 -v` -> 86 passed in 0.613s.
   - Full repository gate: `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh` -> exit code 0.
   - Site builder gate: `scripts/build-site.py` & `scripts/check-site.py` -> exit code 0.
   - Linter: `./.venv/bin/python -m ruff check tests/test_challenger_m2_2.py` -> All checks passed.

---

## 2. Logic Chain

1. **Active Release Guard Correctness**:
   - Observation 1 proves that `target.release_id == active.release_id` triggers `LumaError("cannot rollback to currently active release")`.
   - In `test_active_guard_dynamically_tracks_newly_rolled_back_release`, when Release 3 is created from rolling back Release 2 to Release 1, Release 3 becomes the top of `list_releases` (`history[0]`).
   - Targeting Release 3 immediately triggers the active guard, while targeting Release 2 (now `history[1]`) succeeds.
   - Therefore, the active release guard dynamically and correctly prevents self-rollback cycles and state corruption.

2. **Secret Parity & Immutability**:
   - Observation 2 demonstrates that `ReleaseRecord` stores `secrets: Dict[str, str]`, serializing it in `to_dict()` and deserializing in `_release_from_dict()`.
   - `rollback_release`, `promote_artifact`, and `scale_service` resolve secrets by checking explicit overrides first, target/active release records second, and source candidate records third.
   - Because `secrets` defaults to `target.secrets` rather than `None` or `{}`, zero-wipe invariant is preserved across arbitrary rollback chains.

3. **Cross-Environment Promotion Boundary Enforcement**:
   - Observation 3 confirms `VALID_ENVIRONMENTS = {"dev", "staging", "prod", "preview"}` is enforced across all environment arguments.
   - Non-canonical environments like `qa` or `testing` fail closed before any manifest compilation or state change occurs.
   - When promoting into `prod`, existing production secrets take precedence over staging secrets, preventing accidental credential overwrites while propagating identical artifact image digests.

4. **Flux Reconciler Timestamp Compliance**:
   - Observation 4 confirms `compile_flux_helm_release` uses `datetime.now(timezone.utc).isoformat()`, yielding timestamps like `2026-10-15T17:33:08.999209+00:00`.
   - Python's `datetime.fromisoformat()` and standard RFC 3339 / ISO 8601 parsers parse this with zero UTC offset.
   - Flux CD's helm-controller recognizes changes to `reconcile.fluxcd.io/requestedAt` immediately, fulfilling the `<5s` reconciliation trigger without waiting for the 5m interval.

---

## 3. Caveats

- **Zot Tag Deletion vs Blob Garbage Collection**: In-cluster Zot registry manifests are pruned immediately upon calling `delete_tag()`; however, physical layer blobs on disk are reclaimed asynchronously during Zot's background GC run (controlled by `gcDelay` in `zot-config.json`).
- **Secret File Mount Directory**: When multiple mounted secret files are defined with different parent directories, `helm_compiler.py` mounts the parent directory of the files. Workloads should place secret files within standard `/app/secrets` or specify individual volume mounts per path.
- No other caveats.

---

## 4. Conclusion

**Verdict**: `APPROVE`

Feature 4 (Flux <5s Rollback & Secret Parity) implemented by Worker M2 satisfies all requirements and invariants:
- Rollback guards prevent self-rollbacks, empty history rollbacks, non-healthy rollbacks, and invalid environment targets.
- Runtime secrets and mounted secret files are reliably preserved across repeated rollbacks, scale operations, and promotion without being lost or wiped.
- `promote_artifact` strictly validates environments, supports secret overrides, preserves target environment secrets by default, and maintains zero-rebuild image digest parity.
- HelmRelease manifests consistently inject valid ISO 8601 UTC timestamps under `reconcile.fluxcd.io/requestedAt`.

---

## 5. Verification Method

1. **Adversarial Stress Test Suite**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_challenger_m2_2 -v
   ```
   **Expected**: 23 tests pass with exit code 0.

2. **Combined Engine & Challenger Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders tests.test_challenger_m1 tests.test_challenger_m2_2 -v
   ```
   **Expected**: 86 tests pass with exit code 0.

3. **Repository Quality Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   **Expected**: All linters, TypeScript builds, unit tests, and gates pass with exit code 0.

4. **Invalidation Conditions**:
   - If `rollback_release` targeting `active.release_id` does not raise `LumaError("cannot rollback to currently active release")`, `test_rollback_to_currently_active_release_rejected` will fail.
   - If secrets or mounted secret files are wiped to `{}` or `None` on rollback, `test_repeated_multi_generation_rollback_secret_parity` will fail on equality assertion.
   - If `promote_artifact` accepts invalid environments (such as `qa` or `testing`), `test_promote_artifact_rejects_invalid_environments` will fail.
   - If `reconcile.fluxcd.io/requestedAt` is missing or not valid ISO 8601 UTC, `test_timestamp_format_across_*` will fail.
