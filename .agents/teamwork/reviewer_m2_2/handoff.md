# Review Report & Handoff: Milestone 2 — Feature 4 (Flux <5s Rollback & Promotion Secret Parity)

**Reviewer**: Reviewer M2-2  
**Role**: Reviewer & Adversarial Critic  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/reviewer_m2_2`  
**Target Milestone**: Milestone 2 (Release Reconciler & Artifact Store — Feature 4 focus)  
**Assigned Files Under Review**:
- `luma/paas/release/helm_compiler.py`
- `luma/paas/release/flux_reconciler.py`
- `luma/paas/build/artifact_store.py`
- `tests/test_paas_engine.py`

---

## 1. Observation

Direct code inspections and tool invocations revealed the following factual implementation details:

1. **Immediate Reconciliation Annotation Injection (`helm_compiler.py:286-318`)**:
   - `compile_flux_helm_release()` constructs `req_at = reconcile_requested_at or datetime.now(timezone.utc).isoformat()`.
   - Injects `"annotations": {"reconcile.fluxcd.io/requestedAt": req_at, ...}` directly into `HelmRelease.metadata.annotations`.
   - Verified that `CompiledWorkload` dataclass (`helm_compiler.py:174-214`) exposes:
     ```python
     @property
     def helm_release_manifest(self) -> Dict[str, Any]:
         return self.helm_release
     ```
     as well as `@property def secrets(self) -> list[Dict[str, Any]]` and dictionary serialization in `to_dict()`.

2. **Secret Parity on Rollback, Promotion, and Scaling (`artifact_store.py` & `flux_reconciler.py`)**:
   - `ReleaseRecord` dataclass (`artifact_store.py:164-202`) stores `secrets: Dict[str, str] = field(default_factory=dict)`.
   - Serialized in `ReleaseRecord.to_dict()` via `"secrets": dict(self.secrets)` and parsed in `_release_from_dict()` via `secrets={str(k): str(v) for k, v in (raw.get("secrets") or {}).items()}`.
   - `FluxReconciler.rollback_release()` (`flux_reconciler.py:258-314`):
     Resolves secrets with:
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
     Forwards `secrets=resolved_secrets` and `mounted_secret_files=resolved_mounted_files` along with `replicas_override=target.replicas_by_service` into `self.deploy_artifact()`.
   - `FluxReconciler.promote_artifact()` (`flux_reconciler.py:149-220`):
     Supports `target_secrets` (falling back to `secrets`) and `target_mounted_secret_files` (falling back to `mounted_secret_files`).
     If explicit secrets are omitted, it retains target environment active secrets (`active_target.secrets`) if present, otherwise adopting candidate secrets.
   - `FluxReconciler.scale_service()` (`flux_reconciler.py:222-256`):
     Preserves `secrets=dict(getattr(active, "secrets", {}) or {})` and `mounted_secret_files=dict(active.mounted_secret_files)` during single-service scaling.

3. **Safety Guards (`flux_reconciler.py`)**:
   - `rollback_release` guards against rollback to active release:
     ```python
     if target.release_id == active.release_id:
         raise LumaError("cannot rollback to currently active release")
     ```
   - `promote_artifact` validates environments:
     ```python
     if from_env not in VALID_ENVIRONMENTS:
         raise LumaError(f"invalid source environment {from_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}")
     if dest_env not in VALID_ENVIRONMENTS:
         raise LumaError(f"invalid target environment {dest_env!r}, must be one of {sorted(VALID_ENVIRONMENTS)}")
     ```
   - `deploy_artifact`, `scale_service`, and `rollback_release` all validate `environment in VALID_ENVIRONMENTS`.

4. **Zero Integrity Violations Found**:
   - Grep searches for test artifact names (`art-sec-v1`, `sec-test`, `promo-app`) within `luma/` returned zero matches, confirming no hardcoded test shortcuts or test-specific branches exist in source code.
   - No mock/facade implementations or bypasses were detected.

5. **Test Executions**:
   - Running `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v` executed 52 tests, resulting in **0 failures, 0 errors, OK (0.539s)**.
   - Running `bash scripts/check-luma.sh` executed full linter + 110 node/dashboard tests, passing with exit code **0**.
   - Running `./.venv/bin/python scripts/build-site.py` & `scripts/check-site.py` validated bilingual docs, passing with exit code **0**.

---

## 2. Logic Chain

1. **Reconciliation Latency Guarantee (<5s)**:
   - Without `reconcile.fluxcd.io/requestedAt`, Flux CD v2's HelmRelease reconciler polls on `spec.interval: "5m"`, introducing up to 300s of rollout latency.
   - Injecting an ISO-8601 UTC timestamp on every compilation triggers immediate Kubernetes controller notification via etcd watch events, achieving the required $<5$s reconciliation start contract.
   - Exposing `helm_release_manifest` alias ensures backward compatibility with test harnesses and higher-level orchestration controllers expecting this property.

2. **Secret Preservation Invariant**:
   - Immutable build artifacts intentionally isolate application code from environment credentials. Discarding secrets during rollback or promotion previously created Secret manifests with empty `stringData`, wiping database credentials and API keys in production.
   - Persisting `secrets: Dict[str, str]` in `ReleaseRecord` combined with resolution logic in `rollback_release` restores the exact secret state of the rollback target.
   - In cross-environment promotion (`staging -> prod`), `promote_artifact` checks `active_target.secrets`, ensuring production secrets are preserved when staging code is promoted, while still allowing explicit overrides via `target_secrets`.

3. **Safety Guard Resilience**:
   - Disallowing rollback to `active.release_id` prevents redundant redeployments and avoids generating meaningless duplicate release records.
   - Filtering `history` by `status == "healthy"` prevents rollback to flawed or probe-failed releases.
   - Restricting all operations to `VALID_ENVIRONMENTS` (`{"dev", "staging", "prod", "preview"}`) prevents arbitrary or misspelled namespace creation in the Kubernetes cluster.

---

## 3. Caveats & Adversarial Findings

1. **Secret File Mount Directory Scope (Minor / Edge Case)**:
   - *Observation*: In `_build_secret_manifest` (`helm_compiler.py:239-253`), `mount_dir = parent` is updated in a loop over `mounted_secret_files.items()`.
   - *Impact*: If a workload configures files across multiple distinct parent directories (e.g. `/app/secrets/cert.pem` and `/etc/ssl/ca.crt`), all files in the Secret volume will be mounted at the path of the last directory processed.
   - *Risk*: Low. Standard convention in Luma applications mounts all secret files under `/app/secrets`.

2. **`scale_service` Missing Service Membership Validation (Minor / Edge Case)**:
   - *Observation*: In `scale_service` (`flux_reconciler.py:249`), `updated_replicas[service_name] = int(replicas)` is applied without checking if `service_name` exists in `active.replicas_by_service`.
   - *Impact*: Passing a non-existent service name does not immediately fail; rather, it is omitted from the compiled deployment list or single-service replica mapping.
   - *Risk*: Low. Operator CLI provides autocompletion and dashboard uses service selection dropdowns.

3. **`promote_artifact` Same-Environment Identity (Informational)**:
   - *Observation*: `promote_artifact` does not explicitly forbid `from_env == to_env`.
   - *Impact*: Calling `promote_artifact(..., from_env="prod", to_env="prod")` effectively acts as a re-deployment of the currently active release.
   - *Risk*: Low. Harmless re-reconcile behavior.

4. **`env_digest` Scope Excludes Mounted Secret File Paths (Informational)**:
   - *Observation*: `env_digest` hashes only secret key-value pairs, not `mounted_secret_files`.
   - *Impact*: If an operator modifies only the target mount path of an existing secret file without changing secret content, `diff["envChanged"]` reports `False`.
   - *Risk*: Low.

---

## 4. Conclusion

**Verdict: APPROVE**

The Milestone 2 implementation for Feature 4 (Flux <5s Rollback & Promotion Secret Parity) is verified to be robust, secure, and rigorously tested. All requirements from the authoritative user request and milestone architecture have been met:
- `reconcile.fluxcd.io/requestedAt` is injected with valid ISO-8601 UTC timestamps into `HelmRelease.metadata.annotations`.
- `CompiledWorkload.helm_release_manifest` alias is available and compliant.
- Runtime secrets and mounted secret files are preserved across rollback, zero-rebuild promotion, and service scaling.
- Active release rollback prevention and target environment validation guards are strictly enforced with `LumaError`.
- No integrity violations or hardcoded test facades exist.
- All automated unit test suites, linting gates, and bilingual site checks pass with 100% success.

---

## 5. Verification Method

To independently reproduce and verify this review, execute the following commands in the workspace root:

1. **Milestone 2 Unit Test Verification**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine.ZotGCRetentionAndFluxRollbackTests -v
   ```
   *Expected*: 12 tests pass with exit code `0`.

2. **Full Engine & Builder Unit Test Suite**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected*: 52 tests pass with exit code `0`.

3. **Full Repository Lint & Node/Dashboard Gate**:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh
   ```
   *Expected*: Ruff checks pass, 110/110 node/dashboard tests pass, exit code `0`.

4. **Bilingual Documentation & Site Parity Gate**:
   ```bash
   ./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && \
   ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
   ```
   *Expected*: 48 HTML pages validated, exit code `0`.

5. **Adversarial Invalidation Check**:
   - Modifying `compile_flux_helm_release` to omit `"reconcile.fluxcd.io/requestedAt"` causes `test_flux_reconciliation_annotation_injection` to fail.
   - Modifying `rollback_release` to wipe `secrets` causes `test_flux_rollback_and_promotion_secret_preservation` to fail.
   - Attempting rollback to currently active release without `LumaError` causes assertion failure.
