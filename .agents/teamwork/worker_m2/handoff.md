# Handoff Report: Milestone 2 — Artifact Store & Release Reconciler (Features 3 & 4)

**Worker**: Worker M2  
**Working Directory**: `/Users/turning4th/infra-stacks/.agents/teamwork/worker_m2`  
**Target Milestone**: Milestone 2 (M2: Artifact Store & Release Reconciler)  
**Assigned Files**:
- `luma/paas/build/artifact_store.py`
- `luma/paas/release/helm_compiler.py`
- `luma/paas/release/flux_reconciler.py`
- `tests/test_paas_engine.py` (Suite 2 tests)

---

## 1. Observation

Prior to modifications, the following discrepancies were identified:

1. **Repository Normalization & Validation Failure on Host:Port URLs**:
   In `luma/registry_management.py:40-47`, `_REPOSITORY_RE` rejected `:` (colons). Calling `ZotRegistryClient.pin_digest("100.66.177.70:5000/serverx-web", digest, tag)` or `"registry.internal:5050/subpath/app"` directly triggered `LumaError("invalid registry repository: 100.66.177.70:5000/serverx-web")`. Furthermore, `ArtifactCatalog` lacked `normalize_repository()`.
2. **Retention Pin Naming & Rollback Window Disconnect**:
   In `luma/paas/build/artifact_store.py:323-339`, active tags were generated as `luma-pin-{project}-{env}-active` and rollback tags as `luma-pin-{project}-{env}-rb{idx}`. This conflicted with the contract established across `docs/build-and-artifacts.md`, `dashboard-src/`, and `TEST_INFRA.md` (`luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v{generation}`). Additionally, `healthy_history` did not exclude `active`, reducing the effective protected rollback window from $N=5$ to $N-1=4$.
3. **Missing Tag Pruning & Registry Client Methods**:
   `ArtifactCatalog` lacked `sync_retention_pins()` and `protected_pin_tags()`. `ZotRegistryClient` lacked `delete_tag()` and `list_tags()`. Without active tag deletion (`DELETE /v2/<repo>/manifests/<tag>`), Zot's online GC could not prune obsolete manifests beyond the rollback window.
4. **Secret Erasure on Release Operations**:
   `ReleaseRecord` in `artifact_store.py:153-170` stored `env_digest` and `secret_keys`, but discarded the actual `secrets: Dict[str, str]` mapping. In `flux_reconciler.py`, `rollback_release()`, `promote_artifact()`, and `scale_service()` called `deploy_artifact` with `secrets=None`, causing Secret manifests to be rendered with `stringData = {}` and erasing runtime secrets in production.
5. **Missing Immediate Reconciliation Annotation**:
   In `helm_compiler.py:286-298`, `compile_flux_helm_release()` omitted `metadata.annotations`, leaving Flux CD v2 to wait for `spec.interval: "5m"` rather than reconciling in $<5$s. In addition, `CompiledWorkload` lacked the `helm_release_manifest` property alias expected by the test specifications.
6. **Missing Guard on Rollback to Currently Active Release**:
   `flux_reconciler.py:rollback_release()` allowed targeting `active.release_id`, attempting redundant redeployment instead of rejecting the operation with `LumaError("cannot rollback to currently active release")`.

---

## 2. Logic Chain

1. **Repository Normalization**:
   - OCI Distribution Specification endpoints route repositories relative to registry root (e.g. `/v2/<repository>/manifests/...`).
   - Container image references in multi-region environments frequently carry host and port prefixes (e.g. `100.66.177.70:5000/serverx-web`).
   - Implementing `normalize_repository()` to strip protocol prefixes (`http://`, `https://`) and host:port segment (`first_seg` with `:` or `.` or `localhost`) before invoking `validate_repository()` ensures correct endpoint paths and resolves syntax rejections across `ArtifactCatalog`, `ZotRegistryClient.pin_digest()`, and `ZotRegistryClient.delete_tag()`.
2. **Deterministic Retention Pinning**:
   - Canonical pin naming `luma-pin-active-{project}-{env}` and monotonic `luma-pin-rollback-{project}-{env}-v{generation}` ensures idempotency and aligns with dashboard timeline rendering.
   - Filtering `healthy_history` to exclude `active.release_id` guarantees that exactly `rollback_window` ($N=5$) rollback releases are preserved in addition to the 1 active release (total 6 releases protected).
   - In multi-service Compose stacks sharing base image digests across multiple repositories (e.g. `shared-worker1` and `shared-worker2`), tracking `repositoryPinTags: dict[repo, set[tag]]` ensures each repository's manifest receives the OCI `PUT` pin request without dropping repositories.
3. **Dual-Interface RetentionSyncResult**:
   - `TEST_INFRA.md` requires dict subscription `pruned["deleted_tags"]` and `pruned["retained_tags"]`, while `PROJECT.md` specifies `Sequence[str]` return type.
   - Creating `RetentionSyncResult(list)` inheriting from `list` with custom `__getitem__`, `.get()`, and attribute access satisfies sequence iteration, dict indexing, and object attribute access simultaneously.
4. **Flux Immediate Reconciliation & Properties**:
   - Injecting `"annotations": {"reconcile.fluxcd.io/requestedAt": datetime.now(timezone.utc).isoformat()}` into `HelmRelease.metadata` triggers immediate FluxCD helm-controller reconciliation within $<5$s.
   - Adding `@property def helm_release_manifest(self) -> Dict[str, Any]` and `@property def secrets(self) -> list[Dict[str, Any]]` to `CompiledWorkload` provides contract compliance without disrupting internal structure.
5. **Runtime Secret Parity & Active Release Guard**:
   - Adding `secrets: Dict[str, str] = field(default_factory=dict)` to `ReleaseRecord`, serializing it in `to_dict()`, and deserializing in `_release_from_dict()` allows the catalog to persist runtime secret mappings.
   - In `rollback_release()`: raising `LumaError("cannot rollback to currently active release")` when `target.release_id == active.release_id` prevents corrupted states. Forwarding `secrets=dict(target.secrets)` and `mounted_secret_files=dict(target.mounted_secret_files)` into `deploy_artifact()` restores runtime secrets from the target release.
   - In `promote_artifact()`: validating `target_env in VALID_ENVIRONMENTS` (`{"dev", "staging", "prod", "preview"}`), accepting `target_secrets` / `target_mounted_secret_files`, and retaining active target environment secrets when not explicitly overridden ensures zero-rebuild cross-environment security.
   - In `scale_service()`: passing `secrets=dict(active.secrets)` and `mounted_secret_files=dict(active.mounted_secret_files)` maintains secret parity during replica scaling.

---

## 3. Caveats

- **Zot Offline vs Online GC**: Deleting tags via `delete_tag` removes manifest tags from Zot OCI Registry immediately. Unreferenced blobs are unlinked during Zot's periodic online GC interval (controlled by `gcDelay` in `zot-config.json`). Unit tests verify tag removal via OCI API DELETE requests rather than filesystem blob unlinking.
- **Environment Scope**: `VALID_ENVIRONMENTS` is defined as `{"dev", "staging", "prod", "preview"}` per specification. Any deployment targeting environments outside this set will raise `LumaError`.

---

## 4. Conclusion

All requirements for Milestone 2 (Features 3 and 4) have been implemented and verified:
1. `luma/paas/build/artifact_store.py`:
   - `normalize_repository()` function and method on `ArtifactCatalog` and `ZotRegistryClient`.
   - `ZotRegistryClient.pin_digest()` and `ZotRegistryClient.delete_tag()` normalize repositories before validation.
   - `ZotRegistryClient.delete_tag()` issues HTTP `DELETE /v2/<repo>/manifests/<tag>` accepting `(200, 202, 204, 404)`.
   - `ZotRegistryClient.list_tags()` queries repository tags via `self.http.tags()`.
   - Retention tag naming aligns to `luma-pin-active-<project>-<env>` and `luma-pin-rollback-<project>-<env>-v{generation}`.
   - Multi-service stacks sharing digests track distinct repositories independently in `compute_protected_digests` and `sync_retention_pins`.
   - `ArtifactCatalog.protected_pin_tags()` and `ArtifactCatalog.sync_retention_pins()` implemented with `RetentionSyncResult`.
   - `ReleaseRecord` dataclass stores and serializes `secrets: Dict[str, str]`.
2. `luma/paas/release/helm_compiler.py`:
   - `compile_flux_helm_release()` injects `"annotations": {"reconcile.fluxcd.io/requestedAt": <ISO8601 UTC>}`.
   - `CompiledWorkload` exposes `@property def helm_release_manifest` and `@property def secrets`.
3. `luma/paas/release/flux_reconciler.py`:
   - `VALID_ENVIRONMENTS` validation added across `deploy_artifact`, `promote_artifact`, `scale_service`, and `rollback_release`.
   - `deploy_artifact` accepts positional or keyword `artifact_id` and records `secrets` in `ReleaseRecord`.
   - `rollback_release` raises `LumaError("cannot rollback to currently active release")` when `target.release_id == active.release_id` and forwards target secrets and mounted files.
   - `promote_artifact` supports `target_secrets` and `target_mounted_secret_files` and retains target environment secrets when not overridden.
   - `scale_service` carries forward active release secrets and mounted files.
4. `tests/test_paas_engine.py`:
   - Added `_MockRegistryHttpClient` and `ZotGCRetentionAndFluxRollbackTests` with all 12 test methods.
   - Ran test suite: 52 tests ran, 52 passed, 0 failures, 0 errors.

---

## 5. Verification Method

1. **Unit Test Suite Execution**:
   Run the full unit test suite including existing tests and the new Suite 2 tests:
   ```bash
   ./.venv/bin/python -m unittest tests.test_paas_engine tests.test_native_builders -v
   ```
   **Observed Output**:
   ```
   Ran 52 tests in 0.525s
   OK
   ```
   All 12 tests in `ZotGCRetentionAndFluxRollbackTests` pass:
   - `test_retention_pin_naming_convention`: ok
   - `test_registry_repository_validation_with_port`: ok
   - `test_obsolete_pin_pruning_beyond_window`: ok
   - `test_flux_reconciliation_annotation_injection`: ok
   - `test_flux_rollback_and_promotion_secret_preservation`: ok
   - `test_zero_releases_retention_sync_boundary`: ok
   - `test_duplicate_digests_in_multi_service_stack`: ok
   - `test_rollback_window_one_boundary`: ok
   - `test_zot_registry_client_host_port_normalization_and_manifest_delete`: ok
   - `test_adversarial_malformed_digest_and_repo_rejection`: ok
   - `test_promotion_preserves_target_environment_existing_secrets`: ok
   - `test_cancellation_does_not_pin_or_corrupt_catalog_retention`: ok

2. **Ruff Linter Gate**:
   Execute Ruff across the repository:
   ```bash
   ./.venv/bin/python -m ruff check luma tests scripts
   ```
   **Observed Output**:
   ```
   All checks passed!
   ```

3. **Invalidation Conditions**:
   - If repository URLs with port numbers (e.g. `100.66.177.70:5000/repo`) fail to normalize, `test_registry_repository_validation_with_port` will raise `LumaError: invalid registry repository`.
   - If pin tags are formatted as legacy `luma-pin-*-active` instead of `luma-pin-active-*`, `test_retention_pin_naming_convention` will fail on `assertIn`.
   - If `reconcile.fluxcd.io/requestedAt` is absent from `HelmRelease.metadata.annotations`, `test_flux_reconciliation_annotation_injection` will fail.
   - If `rollback_release` or `promote_artifact` wipes secrets to empty dict, `test_flux_rollback_and_promotion_secret_preservation` will fail on secret equality assertions.
   - If rolling back to the currently active release does not raise `LumaError`, `test_flux_rollback_and_promotion_secret_preservation` active-release check will fail.
