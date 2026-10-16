# Reviewer & Adversarial Audit Handoff Report: Milestone 2 — Feature 3 (Zot GC Retention Pinning & Obsolete Tag Pruning)

**Reviewer**: Reviewer M2-1  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/reviewer_m2_1`  
**Target Milestone**: Milestone 2 (Feature 3: Zot GC Retention Pinning & Obsolete Tag Pruning)  
**Assigned Files**:
- `luma/paas/build/artifact_store.py`
- `tests/test_paas_engine.py`

---

## 1. Observation

Direct code inspections and execution results:

1. **Retention Tag Naming & Rollback Window Offset**:
   - In `luma/paas/build/artifact_store.py:339`: Active pin tag is generated as `pin_tag=f"luma-pin-active-{project}-{env}"`.
   - In `luma/paas/build/artifact_store.py:356`: Rollback pin tag is generated as `pin_tag=f"luma-pin-rollback-{project}-{env}-v{rel.generation}"`.
   - In `luma/paas/build/artifact_store.py:343` (`compute_protected_digests`):
     ```python
     healthy_history = [
         r for r in ordered
         if r.status in {"healthy", "rolled_back"} and (active is None or r.release_id != active.release_id)
     ][:rollback_window]
     ```
   - In `luma/paas/build/artifact_store.py:530` (`ArtifactCatalog.sync_retention_pins`):
     ```python
     healthy_history = [
         r for r in ordered
         if r.status in {"healthy", "rolled_back"} and (active is None or r.release_id != active.release_id)
     ][:rollback_window]
     ```
   - Both implementations explicitly filter out `active.release_id` from `healthy_history`, ensuring exactly `rollback_window` ($N=5$) rollback releases are preserved in addition to the single active release.

2. **Repository Normalization**:
   - In `luma/paas/build/artifact_store.py:17-29`:
     ```python
     def normalize_repository(value: Any) -> str:
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
   - `ArtifactCatalog.normalize_repository(repository)` (line 478) and `ZotRegistryClient.normalize_repository(repository)` (line 601) expose this normalizer directly.
   - `ZotRegistryClient.pin_digest` (line 623), `ZotRegistryClient.delete_tag` (line 606), `ZotRegistryClient.list_tags` (line 619), and `ServiceImageArtifact.__post_init__` (line 82) all route repository validation through `normalize_repository()`.

3. **Obsolete Tag Pruning & Dual-Interface RetentionSyncResult**:
   - In `luma/paas/build/artifact_store.py:375-422`: `class RetentionSyncResult(list)` subclasses `list` (enabling sequence iteration and numeric indexing) and overrides `__getitem__` and `get()` to handle string keys (`"deleted_tags"`, `"retained_tags"`, `"protected_digests"`, `"applied_pin_tags"`).
   - In `luma/paas/build/artifact_store.py:536-544`: Older releases beyond `active` and `healthy_history` are collected into `all_deleted`.
   - In `luma/paas/build/artifact_store.py:545-566`: When `actual_client` is supplied, `actual_client.list_tags(repo)` is queried for project repositories, matching tags against regex `^luma-pin-(?:(?:active|rollback)-{proj}-{env}(?:-v\d+)?|{proj}-{env}-.*)$`, and `actual_client.delete_tag(repo, tag)` is invoked for any matching un-retained tag.
   - In `ZotRegistryClient.delete_tag` (lines 604-616), an HTTP `DELETE /v2/<repo>/manifests/<tag>` is issued, accepting status codes `(200, 202, 204, 404)` to delete the tag reference so Zot online GC can reclaim blobs.

4. **Multi-Service Distinct Repositories**:
   - In `luma/paas/build/artifact_store.py:315-325`: `protected` digest map records `repositoryPinTags: {repo: set[pin_tag]}` and `repositories: set[repo]`.
   - In `luma/paas/build/artifact_store.py:648-656`: `ZotRegistryClient.sync_retention_pins` iterates over all `(repo, digest, tag)` tuples, maintaining `seen_pins` deduplication while issuing independent `pin_digest(str(repo), digest, str(tag))` PUT requests per distinct repository.

5. **Test Suite Verification**:
   - Ran unit test suite:
     `./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v`
     **Output**:
     ```
     Ran 52 tests in 0.618s
     OK
     ```
     All 12 tests in `ZotGCRetentionAndFluxRollbackTests` passed without failure or error.
   - Ran Ruff check:
     `./.venv/bin/python -m ruff check luma tests scripts`
     **Output**: `All checks passed!`

6. **Integrity & Anti-Cheat Audit**:
   - Source code review of `luma/paas/build/artifact_store.py` confirmed absence of hardcoded test inputs, dummy facades, or skipped validations.
   - All tests execute actual parsing, regex matching, set operations, and HTTP client invocations.

---

## 2. Logic Chain

1. **Tag Format & Rollback Window Completeness**:
   - *Observation*: Active tag conforms to `luma-pin-active-{project}-{env}`; rollback tags conform to `luma-pin-rollback-{project}-{env}-v{generation}`.
   - *Logic*: Prior implementation conflated active and rollback naming (`luma-pin-{project}-{env}-active` and `rb{idx}`) and did not exclude active release from `healthy_history`, thereby protecting only $N-1$ rollback releases.
   - *Deduction*: By filtering `r.release_id != active.release_id` prior to slicing `[:rollback_window]`, `healthy_history` contains exactly $N=5$ healthy rollback targets. Together with the single active release, exactly $N+1=6$ releases remain pinned in Zot registry.

2. **Repository Normalization Safety**:
   - *Observation*: OCI Distribution endpoints are structured as `/v2/<name>/manifests/<reference>`. `normalize_repository()` strips leading protocol schemes (`http://`, `https://`) and host:port segments before calling `validate_repository()`.
   - *Logic*: In multi-region deployments, images are referenced via cluster-internal registry host prefixes (e.g. `100.66.177.70:5000/serverx-web` or `registry.internal:5050/subpath/app`). Because `_REPOSITORY_RE` rejects colons and IP syntax, normalizing the repository path before validating OCI compliance prevents false validation errors while maintaining strict path security.
   - *Deduction*: Exposing `normalize_repository` on `ArtifactCatalog` and calling it within `pin_digest` and `delete_tag` ensures repository paths are correctly formatted across all OCI interactions.

3. **Pruning Semantics & Dual-Interface Compatibility**:
   - *Observation*: `RetentionSyncResult` inherits from `list` while exposing custom `__getitem__` mapping for `"deleted_tags"`, `"retained_tags"`, `"protected_digests"`, and `"applied_pin_tags"`.
   - *Logic*: Existing interfaces and test suites expect both sequence iteration (`Sequence[str]`) and dictionary subscription (`pruned["deleted_tags"]`). Subclassing `list` satisfies both type signatures without adapter boilerplate.
   - *Deduction*: When `sync_retention_pins` runs, manifests beyond the rollback window are scheduled for tag removal via `actual_client.delete_tag`, triggering Zot online GC blob unlinking while preserving active and rollback manifests.

4. **Multi-Service Compose Digest Deduping**:
   - *Observation*: Compose applications can deploy multiple services sharing identical base layers and image digests across distinct repositories (e.g. `shared-worker1` and `shared-worker2`).
   - *Logic*: If protected digests were tracked solely by digest key without repository grouping, only the first encountered repository would receive a retention pin, leaving sibling repositories unprotected from GC pruning.
   - *Deduction*: By storing `repositoryPinTags: {repo: set[tag]}` and iterating per repository, `sync_retention_pins` guarantees each repository's manifest receives the OCI PUT pin request while deduplicating redundant calls for identical `(repo, digest, tag)` triplets.

---

## 3. Caveats & Critic Observations

1. **Registry Tag Listing Resilience**:
   In `ArtifactCatalog.sync_retention_pins` (lines 554-566), `actual_client.delete_tag(repo, tag)` is invoked for tags enumerated by `actual_client.list_tags(repo)`. If `list_tags` raises a `LumaError` (e.g. registry tag listing endpoint temporarily unavailable or restricted), `sync_retention_pins` catches the error and skips physical tag deletion rather than failing the synchronization pipeline. This ensures high availability, but physical pruning in Zot will be deferred until the next successful sync cycle.
2. **Offline vs Online GC Verification**:
   Unit tests verify that tag references are deleted via OCI HTTP `DELETE /v2/<repo>/manifests/<tag>` requests accepting `(200, 202, 204, 404)`. Physical blob unlinking on the storage filesystem is performed asynchronously by Zot's online GC worker thread based on its configured `gcDelay`.
3. **Repository Name Uniformity in Compose Stacks**:
   `seen_pins` in `ZotRegistryClient.sync_retention_pins` keys deduplication by `(str(repo), digest, str(tag))`. In standard practice, image repository strings within an artifact are consistent. `pin_digest` normalizes the repository path before dispatch.

---

## 4. Conclusion

**Verdict: APPROVE**

The Milestone 2 implementation for Feature 3 (Zot GC Retention Pinning & Obsolete Tag Pruning) meets all requirements:
1. Canonical pin naming strictly follows `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v{generation}`.
2. Active release exclusion guarantees exactly $N=5$ rollback releases (total 6 releases protected).
3. `normalize_repository` correctly handles host:port URLs across IP addresses, ports, and subpaths.
4. `ArtifactCatalog.sync_retention_pins` computes obsolete tags, invokes `delete_tag` on registry client, and returns a dual-interface `RetentionSyncResult`.
5. Multi-service Compose stacks sharing identical digests across distinct repositories are tracked and pinned per repository.
6. Zero integrity violations or facades were identified; all 52 unit tests pass cleanly.

---

## 5. Verification Method

To independently verify this evaluation:

1. **Execute Unit Tests**:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   *Expected Result*: 52 tests run, 52 pass, 0 failures, 0 errors.

2. **Execute Linter**:
   ```bash
   ./.venv/bin/python -m ruff check luma tests scripts
   ```
   *Expected Result*: All checks pass cleanly.

3. **Verify Specific Feature 3 Test Cases**:
   Inspect `tests/test_paas_engine.py`:
   - `test_retention_pin_naming_convention` (TC-F3-01 & TC-F3-02)
   - `test_registry_repository_validation_with_port` (TC-F3-03)
   - `test_obsolete_pin_pruning_beyond_window` (TC-F3-04)
   - `test_duplicate_digests_in_multi_service_stack` (TC-F3-05 & TC-B-F3-05)
   - `test_rollback_window_one_boundary` (TC-B-F3-03)
   - `test_zero_releases_retention_sync_boundary` (TC-B-F3-04)
   - `test_zot_registry_client_host_port_normalization_and_manifest_delete` (TC-B-F3-01)
   - `test_adversarial_malformed_digest_and_repo_rejection` (TC-B-F3-02)

4. **Invalidation Conditions**:
   - If `normalize_repository("100.66.177.70:5000/app")` raises `LumaError: invalid registry repository`, TC-F3-03 will fail.
   - If active pin tag reverts to `luma-pin-*-active` or rollback tag reverts to `*-rb*`, TC-F3-01 / TC-F3-02 will fail.
   - If `pruned["deleted_tags"]` fails dict indexing on `RetentionSyncResult`, TC-F3-04 will raise `TypeError`.
