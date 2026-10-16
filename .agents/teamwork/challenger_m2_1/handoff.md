# Empirical Challenge & Verification Report: Milestone 2 Feature 3 (Zot GC Retention Pinning & Obsolete Tag Pruning)

**Challenger**: Challenger M2-1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/challenger_m2_1`  
**Target Milestone**: Milestone 2 (Feature 3: Zot GC Retention Pinning & Obsolete Tag Pruning)  
**Assigned Review Scope**:
- `luma/paas/build/artifact_store.py` (`normalize_repository`, `compute_protected_digests`, `ArtifactCatalog.sync_retention_pins`, `ZotRegistryClient.sync_retention_pins`, `ZotRegistryClient.delete_tag`, `ZotRegistryClient.pin_digest`)
- `tests/test_paas_engine.py` (Suite 2 tests: `ZotGCRetentionAndFluxRollbackTests`)
- Adversarial test harness: `/Users/turning4th/.gemini/antigravity/brain/377c165e-cb67-4931-a8e9-e851b36c10e2/test_feature3_stress.py`

**Explicit Verdict**: **`APPROVE`** (with 2 non-blocking advisory hardening findings documented below).

---

## 1. Observation

### Observation 1.1: Repository Normalization Across Standard and Edge Cases
Ran empirical tests against `normalize_repository()` in `luma/paas/build/artifact_store.py:17-29`:
```python
def normalize_repository(value: Any) -> str:
    """Strip registry host and port (e.g. '100.66.177.70:5000/app' -> 'app') and validate OCI repository path."""
    raw = str(value or "").strip()
    if raw.startswith("http://"):
        raw = raw[7:]
    elif raw.startswith("https://"):
        raw = raw[8:]
    if "/" in raw:
        first_seg, rest = raw.split("/", 1)
        if ":" in first_seg or "." in first_seg or first_seg.lower() == "localhost":
            raw = rest
    return validate_repository(raw)
```
- **Port numbers**:
  - `normalize_repository("host:5000/app")` -> `'app'`
  - `normalize_repository("100.66.177.70:5000/app")` -> `'app'`
  - `normalize_repository("registry.internal:5050/subpath/app")` -> `'subpath/app'`
  - `normalize_repository("localhost:80/app")` -> `'app'`
  - `normalize_repository("localhost:8080/my-service")` -> `'my-service'`
  - `normalize_repository("127.0.0.1:443/app")` -> `'app'`
- **Multi-level subpaths**:
  - `normalize_repository("registry/sub1/sub2/app")` -> `'registry/sub1/sub2/app'` (valid OCI repository path without host)
  - `normalize_repository("host:5000/sub1/sub2/app")` -> `'sub1/sub2/app'`
  - `normalize_repository("registry.example.com/sub1/sub2/app")` -> `'sub1/sub2/app'`
  - `normalize_repository("registry.internal:5050/org/team/project/app")` -> `'org/team/project/app'`
- **Scheme prefixes**:
  - `normalize_repository("http://host:5000/app")` -> `'app'`
  - `normalize_repository("https://host:5000/app")` -> `'app'`
  - `normalize_repository("http://100.66.177.70:5000/app")` -> `'app'`
  - `normalize_repository("https://100.66.177.70:5000/app/")` -> `'app'`
- **Boundary slashes**:
  - `normalize_repository("/app")` -> `'app'`
  - `normalize_repository("app/")` -> `'app'`
  - `normalize_repository("/app/")` -> `'app'`
  - `normalize_repository("//app//")` -> `'app'`
  - `normalize_repository("/sub1/sub2/app/")` -> `'sub1/sub2/app'`
  - `normalize_repository("host:5000/app/")` -> `'app'`
  - `normalize_repository("host:5000/sub1/sub2/app/")` -> `'sub1/sub2/app'`
- **Leading slash with host prefix corner case**:
  - `normalize_repository("/host:5000/app")` raises `luma.errors.LumaError: invalid registry repository: /host:5000/app`.
  - `normalize_repository("http:///host:5000/app")` raises `luma.errors.LumaError: invalid registry repository: /host:5000/app`.
- **Invalid inputs**:
  - `""`, `"   "`, `None`, `"100.66.177.70:5000/"`, `"http://"`, `"bad//repo"`, `"app:latest"` all raise `LumaError` as expected.

### Observation 1.2: Boundary Stress Scenarios for `sync_retention_pins`
Ran empirical boundary checks with varying release counts and generation distributions:
- **0 releases**: `catalog.sync_retention_pins([])` returns `retained_tags == []` and `deleted_tags == []` without error.
- **1 release**: Generation 1.
  - `retained_tags`: `['luma-pin-active-test-proj-prod']` (exactly 1 active, 0 rollback).
  - `deleted_tags`: `[]`.
- **5 releases**: Generations 1 through 5.
  - `retained_tags`: `['luma-pin-active-test-proj-prod', 'luma-pin-rollback-test-proj-prod-v1', 'luma-pin-rollback-test-proj-prod-v2', 'luma-pin-rollback-test-proj-prod-v3', 'luma-pin-rollback-test-proj-prod-v4']` (1 active + 4 rollback = 5 retained).
  - `deleted_tags`: `[]`.
- **6 releases**: Generations 1 through 6.
  - `retained_tags`: 1 active (gen 6) + 5 rollback (gens 1..5) = 6 retained.
  - `deleted_tags`: `[]`.
- **20 releases with non-linear generation gaps**:
  Generations: `[1, 5, 12, 17, 25, 33, 42, 50, 61, 75, 80, 92, 105, 120, 150, 180, 210, 250, 300, 350]`.
  - Exactly 1 active tag retained: `luma-pin-active-gap-proj-prod` (generation 350).
  - Exactly 5 rollback tags retained: `v300`, `v250`, `v210`, `v180`, `v150`.
  - Total retained: 6 tags.
  - Exactly 14 older rollback tags marked for deletion: `v1`, `v5`, `v12`, `v17`, `v25`, `v33`, `v42`, `v50`, `v61`, `v75`, `v80`, `v92`, `v105`, `v120`.
  - Total deleted: 14 tags.
- **Status filtering**:
  - `failed` releases are excluded from `healthy_history` and do not consume rollback slots.
  - `rolled_back` releases are included in `healthy_history` and protected.
- **Legacy pin pruning**:
  - Registry tags `luma-pin-legacy-app-prod-active` and `luma-pin-legacy-app-prod-rb1` were deleted via OCI DELETE API calls (`/v2/legacy-app/manifests/luma-pin-legacy-app-prod-active`).
  - Standard user image tags (`v1.0.0`, `v2.0.0-rc1`) were NOT deleted.

### Observation 1.3: Multi-Service Compose Stacks Sharing Identical Image Digests
Empirically tested a Compose stack with 4 services (`web`, `worker-a`, `worker-b`, `worker-c`) sharing the identical base digest `sha256:eeee...` across 4 distinct repositories (`100.66.177.70:5000/compose-web`, `100.66.177.70:5000/compose-worker-a`, `100.66.177.70:5000/compose-worker-b`, `100.66.177.70:5000/compose-worker-c`):
- `compute_protected_digests()` populated `entry["repositories"]` with all 4 distinct repositories and `entry["repositoryPinTags"]` with pin tags for all 4 repositories.
- `ZotRegistryClient.sync_retention_pins()` issued exactly 4 OCI manifest PUT requests:
  - `PUT /v2/compose-web/manifests/luma-pin-active-stack4-prod`
  - `PUT /v2/compose-worker-a/manifests/luma-pin-active-stack4-prod`
  - `PUT /v2/compose-worker-b/manifests/luma-pin-active-stack4-prod`
  - `PUT /v2/compose-worker-c/manifests/luma-pin-active-stack4-prod`
- Distinct repositories were not dropped or collapsed into one.
- In a separate test where 3 services shared the same digest AND the same repository (`shared-same-repo`), `sync_retention_pins` issued exactly 1 PUT request, confirming deduplication.

### Observation 1.4: `sync_retention_pins` Parameter Scoping Anomaly
In `luma/paas/build/artifact_store.py:511-570`:
- Line 511 assigns `rel_list = list(releases if releases is not None else self.list_releases())` and uses it to compute `all_retained` and `all_deleted`.
- Line 568 calls `protected = self.protected_digests(rollback_window=rollback_window)`.
- `protected_digests()` in lines 481-484 calls `self.list_releases()`, ignoring the `releases` sequence passed into `sync_retention_pins`.
- Consequently, if a caller passes `releases` that were not previously persisted into `catalog._state["paasReleases"]`, `protected` evaluates to `{}` and 0 OCI PUT pin requests are issued. Conversely, if a caller passes a filtered subset of releases (e.g. only project A), `self.protected_digests()` pins all releases in the catalog (projects A and B).

---

## 2. Logic Chain

1. **Repository Normalization Integrity**:
   - `normalize_repository()` correctly handles all standard OCI container image references containing host:port prefixes, multi-level namespaces, leading/trailing slashes, and HTTP/HTTPS schemes.
   - The failure on `"/host:5000/app"` occurs because `raw.split("/", 1)` splits at index 0, producing `first_seg = ""`, which bypasses the host:port removal condition. However, leading slashes before a hostname are non-standard in container image references.
2. **Deterministic Retention and Pruning**:
   - By sorting releases by `(r.generation, r.created_at)` descending and picking the first active release, exactly 1 active tag (`luma-pin-active-<proj>-<env>`) is retained.
   - By slicing `healthy_history[:rollback_window]` after excluding the active release, exactly $\le N$ rollback tags (`luma-pin-rollback-<proj>-<env>-v{generation}`) are retained.
   - In the 20-release test with non-linear generation gaps, exactly 1 active + 5 rollback tags were retained, and all 14 older tags were marked for deletion.
   - The regex `^luma-pin-(?:(?:active|rollback)-{proj}-{env}(?:-v\d+)?|{proj}-{env}-.*)$` correctly matches current and legacy pin patterns while ignoring regular user release tags (`v1.0.0`), preventing unintended registry deletion.
3. **Multi-Service Grouping and Pinning**:
   - In `compute_protected_digests()`, grouping by image digest while maintaining `entry["repositoryPinTags"][repo]` ensures that when multiple services share an image digest across distinct repository names, every repository receives its respective OCI PUT request.
   - The presence of `seen_pins = set()` in `ZotRegistryClient.sync_retention_pins()` guarantees deduplication when services share both repository and digest.
4. **Architectural Scoping**:
   - `sync_retention_pins()` calling `self.protected_digests()` at line 568 is safe in standard runtime workflows because `FluxReconciler` always records releases in the catalog via `self.catalog.record_release(record)` prior to pin synchronization. However, line 568 should ideally invoke `compute_protected_digests({a.artifact_id: a for a in self.list_artifacts()}, rel_list, rollback_window=rollback_window)` for complete decoupling.

---

## 3. Caveats

- **Zot Offline Blob Collection**: Tests verify tag lifecycle at the OCI Distribution API level (`PUT` to pin, `DELETE` to unpin). Physical storage reclamation on disk is subject to Zot's periodic online GC interval configuration (`gcDelay` in `zot-config.json`).
- **Peer Test File Lints**: Running `scripts/check-luma.sh` surfaced 3 unused import lints in `tests/test_challenger_m2_2.py` belonging to concurrent Challenger M2-2. In accordance with the Review-Only constraint, Challenger M2-1 did not touch peer agent files. Running ruff strictly on `luma` and `tests/test_paas_engine.py` yielded zero errors (`All checks passed!`).

---

## 4. Conclusion

**Verdict: `APPROVE`**

Worker M2's implementation of Feature 3 (Zot GC Retention Pinning & Obsolete Tag Pruning) satisfies all requirements from `ORIGINAL_REQUEST.md`, `PROJECT.md`, and `TEST_INFRA.md`.
- Active and rollback pin naming conventions match the specification.
- Obsolete pin pruning strictly preserves 1 active and up to $N=5$ rollback releases and deletes older pins beyond the window.
- Multi-service Compose stacks sharing digests across 3+ distinct repositories correctly pin all repositories with proper deduplication.
- All 52 automated tests in `test_paas_engine.py` and `test_native_builders.py` pass cleanly.
- All 13 newly executed adversarial stress tests in `test_feature3_stress.py` pass cleanly.

**Advisory Recommendations for Future Hardening**:
1. In `normalize_repository()`: prepend `raw = raw.lstrip("/")` before `if "/" in raw:` to safely normalize accidental leading slashes in `"/host:5000/app"`.
2. In `ArtifactCatalog.sync_retention_pins()`: replace `self.protected_digests()` on line 568 with `compute_protected_digests({a.artifact_id: a for a in self.list_artifacts()}, rel_list, rollback_window=rollback_window)` to decouple pin synchronization from pre-existing catalog release state.

---

## 5. Verification Method

To independently verify all findings and stress tests:

1. **Run the Adversarial Stress Suite**:
   ```bash
   ./.venv/bin/python /Users/turning4th/.gemini/antigravity/brain/377c165e-cb67-4931-a8e9-e851b36c10e2/test_feature3_stress.py -v
   ```
   **Expected Output**:
   ```
   Ran 13 tests in 0.003s
   OK
   ```

2. **Run Full PaaS Engine & Native Builder Test Suites**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   **Expected Output**:
   ```
   Ran 52 tests in 0.584s
   OK
   ```

3. **Verify Linter Cleanliness on Feature 3 Files**:
   ```bash
   ./.venv/bin/python -m ruff check luma/paas/build/artifact_store.py tests/test_paas_engine.py
   ```
   **Expected Output**:
   ```
   All checks passed!
   ```

4. **Invalidation Conditions**:
   - If `normalize_repository("100.66.177.70:5000/serverx-web")` fails to return `"serverx-web"`, `test_port_numbers_in_host` will fail.
   - If `sync_retention_pins` with 20 releases fails to retain exactly 1 active and 5 rollback releases or fails to mark the 14 older releases for deletion, `test_boundary_twenty_releases_with_gaps` will fail.
   - If 4 Compose services sharing an image digest across distinct repositories drop any repository from receiving a `PUT /v2/<repo>/manifests/...` pin request, `test_three_plus_services_sharing_identical_digest` will fail.
