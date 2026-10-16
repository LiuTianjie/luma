# Forensic Audit Report: Milestone 2 — Artifact Store & Release Reconciler

**Auditor**: Forensic Auditor M2-1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/auditor_m2_1`  
**Target Milestone**: Milestone 2 (M2: Artifact Store & Release Reconciler)  
**Profile**: General Project (Development Mode, per `ORIGINAL_REQUEST.md:8`)  
**Verdict**: **CLEAN**

---

## 1. Observation

A forensic audit of all Milestone 2 targets was conducted across the codebase:
- `luma/paas/build/artifact_store.py`
- `luma/paas/release/helm_compiler.py`
- `luma/paas/release/flux_reconciler.py`
- `tests/test_paas_engine.py`

### 1.1 Source Code Verification
1. **Repository Normalization (`artifact_store.py:17-29, 601-620`)**:
   - `normalize_repository(value)` parses and strips URL protocol prefixes (`http://`, `https://`) and host/port segments (`first_seg` with `:` or `.` or `localhost`), forwarding canonical repository paths to `validate_repository()`.
   - `ZotRegistryClient.delete_tag`, `ZotRegistryClient.pin_digest`, and `ZotRegistryClient.list_tags` call `normalize_repository()` prior to issuing OCI API requests.
2. **Retention Pinning & Pruning (`artifact_store.py:275-372, 501-578`)**:
   - `compute_protected_digests()` sets active tags to `luma-pin-active-{project}-{env}` and rollback tags to `luma-pin-rollback-{project}-{env}-v{rel.generation}`.
   - `healthy_history` excludes `active.release_id`, preserving exactly `rollback_window` ($N=5$) rollback releases in addition to 1 active release.
   - Distinct repositories sharing image digests in Compose stacks are recorded in `repositoryPinTags: dict[repo, set[tag]]` so both OCI repositories receive tag PUT requests.
   - `ArtifactCatalog.sync_retention_pins()` computes `all_retained` and `all_deleted`, queries remote tags via `client.list_tags(repo)`, and deletes obsolete pin tags matching `pin_regex`.
3. **Dual-Interface RetentionSyncResult (`artifact_store.py:375-422`)**:
   - Inherits from `list` with `deleted_tags`, `retained_tags`, `protected_digests`, and `applied_pin_tags`.
   - Implements `__getitem__`, `get()`, and `to_dict()`, supporting both list indexing/iteration and dict key subscription (`res["deleted_tags"]`).
4. **Flux Immediate Reconciliation Annotation (`helm_compiler.py:286-318`)**:
   - `compile_flux_helm_release()` injects `"reconcile.fluxcd.io/requestedAt": req_at` into `HelmRelease.metadata.annotations` using ISO-8601 UTC timestamp format.
   - `CompiledWorkload` exposes `@property def helm_release_manifest` and `@property def secrets`.
5. **Runtime Secret Parity & Rollback Guards (`flux_reconciler.py:20, 38-314`)**:
   - `VALID_ENVIRONMENTS = {"dev", "staging", "prod", "preview"}` is enforced across `deploy_artifact`, `promote_artifact`, `scale_service`, and `rollback_release`.
   - `ReleaseRecord` dataclass stores and serializes `secrets: Dict[str, str]`.
   - `rollback_release()` raises `LumaError("cannot rollback to currently active release")` when `target.release_id == active.release_id`.
   - `rollback_release()` and `promote_artifact()` retain secrets and mounted secret files from target releases.
   - `scale_service()` carries forward active release secrets and mounted files.

### 1.2 Cheating & Facade Analysis
- **Hardcoded test results**: Searched for test-specific strings (`"granary"`, `"kato"`, `"sec-test"`, `"promo-app"`, `"shared-app"`) in `luma/paas/`. Zero hardcoded occurrences found in production logic.
- **Facade detection**: No dummy stub methods, no unconditional returns, no `NotImplementedError`, and no bypass decorators found.
- **Pre-populated artifacts**: Scanned workspace for pre-populated logs or attestation files predating current runs; none found for Milestone 2.
- **Test Integrity**: Unit tests in `tests/test_paas_engine.py` (lines 819–1285) instantiate real `BuildArtifact`, `ServiceImageArtifact`, `ReleaseRecord`, and `FluxReconciler` objects, verifying real `LumaError` exceptions, real manifest dictionaries, and real OCI PUT/DELETE requests recorded via `_MockRegistryHttpClient`.

### 1.3 Empirical Verification Results
- **Suite 2 Unit Tests**:
  `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`
  Result: 52 tests ran, 52 passed, 0 failures, 0 errors.
- **Challenger Tests**:
  `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders tests.test_challenger_m1 -v`
  Result: 63 tests ran, 63 passed, 0 failures, 0 errors.
- **Ruff Linter**:
  `./.venv/bin/python -m ruff check luma tests scripts`
  Result: `All checks passed!`
- **Full Repository Gate**:
  `PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh`
  Result: Exit code 0 (110 Node/Vite tests, all Python unit tests passed).
- **Static Site Build & Anchor Check**:
  `./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site`
  Result: Exit code 0 ("Validated local links and assets across 48 HTML pages; bilingual sections match.").

---

## 2. Logic Chain

1. **Requirement Mapping**:
   `ORIGINAL_REQUEST.md` (R1) mandates Zot online GC retention pinning (`luma-pin-active-*` and `luma-pin-rollback-*`), Flux CD $<5$s rollback and zero-rebuild cross-environment promotion without secret loss.
2. **Empirical Verification of Invariants**:
   - Active pin tags follow `luma-pin-active-{project}-{env}` and rollback pin tags follow `luma-pin-rollback-{project}-{env}-v{generation}`.
   - `normalize_repository()` was stress-tested against IPv4 with ports, internal domain names with ports, localhost, bare repositories, subpaths, and malformed inputs. All behaved according to specification without escaping validation.
   - `ReleaseRecord` preserves secrets through JSON serialization and deserialization cycles (`to_dict` and `_release_from_dict`), preventing runtime secret loss during rollback and environment promotion.
   - The dual interface of `RetentionSyncResult` satisfies both list iteration and dictionary key subscription without type errors or attribute lookups failing.
   - `reconcile.fluxcd.io/requestedAt` is injected with an ISO-8601 UTC timestamp into `HelmRelease.metadata.annotations`.
3. **Adversarial Resilience**:
   - Malformed digests (`sha256:short`, `md5:...`) and invalid repository strings (`"100.66.177.70:5000/"`) correctly raise `LumaError`.
   - Attempting to roll back to the currently active release correctly raises `LumaError("cannot rollback to currently active release")`.
   - Out-of-bounds replicas (`-1` or `65`) in `scale_service()` correctly raise `LumaError`.
   - Invalid environments in `deploy_artifact`, `rollback_release`, `promote_artifact`, and `scale_service` raise `LumaError`.

---

## 3. Caveats

1. **Shared Digest Tag Scope in Multi-Environment Workloads**:
   In zero-rebuild promotions where an image artifact digest is shared across multiple environments (e.g., identical digest in `staging` and `prod`), `compute_protected_digests` groups pin tags under the image digest. Calling `protected_pin_tags(project, environment)` for `staging` will return all pin tags associated with that shared digest (including the `prod` tag). While both tags legitimately protect that same underlying OCI digest, consumers expecting strictly environment-filtered tag names should be aware of this shared-digest aggregation.
2. **Network Isolation in Unit Tests**:
   Physical network interaction with an external Zot OCI registry was evaluated through `_MockRegistryHttpClient` in unit tests, conforming to hermetic testing standards. Real HTTP payload serialization and header matching were verified through mock recording.

---

## 4. Conclusion

The work product delivered in Milestone 2 fulfills all functional requirements and passes all forensic checks under Development mode. No facade implementations, test evasions, or hardcoded shortcuts exist. The implementation displays authentic logic, robust edge-case validation, and full test suite compatibility.

**Final Verdict**: **CLEAN**

---

## 5. Verification Method

To independently verify this verdict:

```bash
# 1. Run unit tests for PaaS engine and native builders
./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v

# 2. Run lint check
./.venv/bin/python -m ruff check luma tests scripts

# 3. Run full repository gate
PATH="$PWD/.venv/bin:$PATH" bash scripts/check-luma.sh

# 4. Run bilingual documentation and anchor parity check
./.venv/bin/python scripts/build-site.py --output /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site && ./.venv/bin/python scripts/check-site.py /Users/turning4th/.gemini/antigravity/brain/f357934f-23b1-4c82-8085-bcb5e022f5d1/scratch/luma-site
```

### Invalidation Conditions:
- If `normalize_repository` fails to strip port prefixes from IP addresses, `test_registry_repository_validation_with_port` will raise `LumaError`.
- If pin tags deviate from `luma-pin-active-*` or `luma-pin-rollback-*-v*`, `test_retention_pin_naming_convention` will fail.
- If `reconcile.fluxcd.io/requestedAt` is absent from `HelmRelease`, `test_flux_reconciliation_annotation_injection` will fail.
- If secrets are erased during rollback or promotion, `test_flux_rollback_and_promotion_secret_preservation` will fail.
- If rolling back to the currently active release does not raise `LumaError`, `test_flux_rollback_and_promotion_secret_preservation` will fail.
