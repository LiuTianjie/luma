# Milestone 2 Unit Test Suite Design: Suite 2 (`ZotGCRetentionAndFluxRollbackTests`)

**Document Location**: `/Users/turning4th/infra-stacks/.agents/teamwork/explorer_m2_3/handoff.md`  
**Target Harness File**: `tests/test_paas_engine.py`  
**Target Milestone**: Milestone 2 (M2: Artifact Store & Release Reconciler)  
**Author**: Explorer M2-3 (Test Designer)  

---

## 1. Observation

### 1.1 Existing Test Harness Baseline
Execution of the current test suite:
```bash
./.venv/bin/python -m unittest tests.test_paas_engine -v
```
Result: 15 tests ran in 0.021s, exit code 0.
- `PaaSEngineTests`: 5 tests covering Dockerfile acceleration, waterfall parsing, service/compose compiler basics, compose independent deployments, and migration engine CLI surface.
- `NativeARM64AndTektonCancellationTests` (Suite 1): 10 tests covering native platform validation, topology inference, builder selection, PipelineRun grace period/timeouts, concurrency accounting, terminal guards, Tekton YAML signal trapping, and boundary cases.
- `Suite 2` (`ZotGCRetentionAndFluxRollbackTests`) is currently missing from `tests/test_paas_engine.py`.

### 1.2 Authoritative Specification Review (`TEST_INFRA.md` & `PROJECT.md`)
In `TEST_INFRA.md` lines 71–84 and lines 379–518, Suite 2 is specified with core requirements:
1. **Retention Pin Naming Contract**:
   - Active release tag: `luma-pin-active-<project>-<env>` (e.g., `luma-pin-active-granary-prod`).
   - Rollback release tag: `luma-pin-rollback-<project>-<env>-v<generation>` (e.g., `luma-pin-rollback-granary-prod-v1`).
   - In `luma/paas/build/artifact_store.py:323-337`, the current implementation generates inverted tags `luma-pin-{project}-{env}-active` and `luma-pin-{project}-{env}-rb{idx}`.
   - `ArtifactCatalog.protected_pin_tags(project, environment, rollback_window=5)` is called in `TEST_INFRA.md:415` but does not exist in `luma/paas/build/artifact_store.py`.
2. **Registry Repository Validation with Port**:
   - `luma/registry_management.py:40, 46`: `_REPOSITORY_RE` rejects colons (`:`). When passed repository URLs containing registry host/port (e.g., `100.66.177.70:5000/serverx-web` or `registry.internal:5050/subpath/app`), `validate_repository` raises `LumaError: invalid registry repository`.
   - `TEST_INFRA.md:418-423` specifies `catalog.normalize_repository(repo_with_port) -> str` which strips host/port to return `"serverx-web"`.
   - `ZotRegistryClient.pin_digest` and `delete_tag` directly invoke `validate_repository(repository)` at `artifact_store.py:436, 471` without stripping host/port.
3. **Obsolete Tag Pruning Beyond Window $N=5$**:
   - `TEST_INFRA.md:424-453`: When 8 releases are deployed with `rollback_window=5`, `catalog.sync_retention_pins(releases, rollback_window=5)` must return `{"deleted_tags": [...], "retained_tags": [...]}` with `len(deleted_tags) == 2` and `len(retained_tags) == 6`.
   - `ArtifactCatalog.sync_retention_pins` does not exist; only `ZotRegistryClient.sync_retention_pins` exists at `artifact_store.py:453` and only returns `{"protectedDigests": ..., "appliedPinTags": ...}` without identifying or deleting stale tags.
4. **Flux Immediate Reconciliation Annotation**:
   - In `luma/paas/release/helm_compiler.py:274-320` (`compile_flux_helm_release`), the emitted `HelmRelease` metadata contains `labels` but lacks `annotations`.
   - `TEST_INFRA.md:454-477` requires `hr["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"]` with an ISO-8601 UTC timestamp so FluxCD reconciles in $<5s$ instead of waiting for the 5-minute poll interval.
5. **Flux Rollback & Promotion Runtime Secret Preservation**:
   - In `luma/paas/release/flux_reconciler.py:194-226` (`rollback_release`), secrets are not passed into `deploy_artifact`, which defaults `secrets=None` (`{}`), wiping secrets on rollback.
   - In `luma/paas/release/flux_reconciler.py:136-162` (`promote_artifact`), target environment secrets are overwritten unless explicitly provided, and parameter naming is `secrets` rather than supporting both `target_secrets` and `secrets`.
   - `ReleaseRecord` at `artifact_store.py:153-170` records `secret_keys` and `env_digest`, but lacks `secrets: Dict[str, str] = field(default_factory=dict)`, which prevents `rel.secrets["API_KEY"]` assertions (`TEST_INFRA.md:506, 515`) from accessing preserved secret values.

---

## 2. Logic Chain

1. **Test-First Reliability**: To enable the M2 worker to safely modify `artifact_store.py`, `helm_compiler.py`, and `flux_reconciler.py` without regressions, the test suite must define executable assertions against both normal workflows and boundary conditions.
2. **Interface Parity**: 
   - `ArtifactCatalog` must expose `normalize_repository(repository: str) -> str`, `protected_pin_tags(project: str, environment: str, rollback_window: int = 5) -> Sequence[str]`, and `sync_retention_pins(releases: Sequence[ReleaseRecord], rollback_window: int = 5) -> Dict[str, list[str]]`.
   - `FluxReconciler.deploy_artifact` must accept positional or keyword `artifact_id`, inject `reconcile.fluxcd.io/requestedAt` into `HelmRelease`, and populate `ReleaseRecord.secrets`.
   - `FluxReconciler.promote_artifact` must accept `target_secrets` / `secrets` and `target_mounted_secret_files` / `mounted_secret_files`, preserving target environment secrets when not overridden.
   - `FluxReconciler.rollback_release` must carry forward secrets and mounted secret files from the target release.
3. **Deterministic, Zero-Network Execution**: Tests must not rely on a live Zot registry or external network. A lightweight in-memory `_MockRegistryHttpClient` recording HTTP requests (`PUT`, `DELETE`, `GET`) allows complete verification of OCI API contract adherence in microseconds.
4. **Comprehensive Boundary Coverage**: Testing beyond happy-path requires:
   - Empty releases list / catalog boundary ($N=0$).
   - Extreme rollback window ($N=1$).
   - Multi-service compose stack sharing identical image digests across distinct repositories.
   - Malformed digest and invalid repository URL rejection.
   - Tekton build cancellation isolation (canceled builds must not mutate catalog retention pins).

---

## 3. Caveats

- **Scope Boundary**: Explorer M2-3 is read-only and does not modify production code in `luma/` or test code in `tests/test_paas_engine.py`. The complete Python code is provided in this report for direct implementation by the M2 worker.
- **Backwards Compatibility**: Existing tests in `tests/test_paas_engine.py` (e.g. `PaaSEngineTests.test_compose_independent_deployments_flux_promotion_and_zot_gc_pins`) pass keyword argument `artifact_id=...` and verify `protected_digests`. The designed test suite maintains full backward compatibility with those existing tests.

---

## 4. Conclusion & Executable Python Test Suite

The unit test suite below is ready for direct drop-in into `tests/test_paas_engine.py` under the class name `ZotGCRetentionAndFluxRollbackTests`.

### 4.1 Mock HTTP Client Helper
```python
class _MockRegistryHttpClient:
    """Mock RegistryHttpClient for deterministic, network-free OCI API testing."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str, bytes | None]] = []

    def manifest(self, repository: str, digest: str) -> tuple[dict, RegistryResponse]:
        body = b'{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"}'
        headers = {"Content-Type": "application/vnd.oci.image.manifest.v1+json"}
        return json.loads(body), RegistryResponse(status=200, headers=headers, body=body)

    def request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
        body: bytes | None = None,
        content_type: str = "",
        expected: Any = (200, 201, 202),
    ) -> RegistryResponse:
        self.requests.append((method.upper(), path, content_type, body))
        return RegistryResponse(status=201, headers={}, body=b"")
```

### 4.2 Executable Test Suite Code (`ZotGCRetentionAndFluxRollbackTests`)
```python
class ZotGCRetentionAndFluxRollbackTests(unittest.TestCase):
    """Verifies Zot online GC retention pin conventions, obsolete tag pruning,
    and Flux CD <5s rollback and promotion secret parity."""

    def setUp(self) -> None:
        self.state: dict = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)

    def test_retention_pin_naming_convention(self) -> None:
        """TC-F3-01 & TC-F3-02: Verifies luma-pin-active-* and luma-pin-rollback-* tag generation."""
        art_v1 = BuildArtifact(
            artifact_id="art-pin-001",
            project="granary",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: granary\nregion: cn\nport: 8080\n",
            images={
                "granary": ServiceImageArtifact(
                    service_name="granary",
                    repository="100.66.177.70:5000/granary",
                    digest="sha256:" + "1" * 64,
                    tag="v1",
                )
            },
            created_at=1700000000,
        )
        self.catalog.record_artifact(art_v1)
        rel_v1, _ = self.flux.deploy_artifact(art_v1.artifact_id, environment="prod")

        # Active tag contract: luma-pin-active-<project>-<env>
        active_tag = "luma-pin-active-granary-prod"
        pins_v1 = self.catalog.protected_pin_tags("granary", "prod", rollback_window=5)
        self.assertIn(active_tag, pins_v1)

        # Deploy v2: v2 becomes active, v1 becomes rollback v1
        art_v2 = BuildArtifact(
            artifact_id="art-pin-002",
            project="granary",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: granary\nregion: cn\nport: 8080\n",
            images={
                "granary": ServiceImageArtifact(
                    service_name="granary",
                    repository="100.66.177.70:5000/granary",
                    digest="sha256:" + "2" * 64,
                    tag="v2",
                )
            },
            created_at=1700000100,
        )
        self.catalog.record_artifact(art_v2)
        rel_v2, _ = self.flux.deploy_artifact(art_v2.artifact_id, environment="prod")

        pins_v2 = self.catalog.protected_pin_tags("granary", "prod", rollback_window=5)
        self.assertIn("luma-pin-active-granary-prod", pins_v2)
        self.assertIn("luma-pin-rollback-granary-prod-v1", pins_v2)

        # Confirm inverted or obsolete tag naming schemes are not generated
        self.assertNotIn("luma-pin-granary-prod-active", pins_v2)
        self.assertNotIn("luma-pin-granary-prod-rb1", pins_v2)

        # Verify protected_digests catalog mapping
        digests = self.catalog.protected_digests(rollback_window=5)
        self.assertIn(art_v2.images["granary"].digest, digests)
        self.assertIn("luma-pin-active-granary-prod", digests[art_v2.images["granary"].digest]["pinTags"])
        self.assertIn(art_v1.images["granary"].digest, digests)
        self.assertIn("luma-pin-rollback-granary-prod-v1", digests[art_v1.images["granary"].digest]["pinTags"])

    def test_registry_repository_validation_with_port(self) -> None:
        """TC-F3-03: Verifies repository normalization and validation with host:port URLs."""
        # 1. IP with port
        self.assertEqual(
            self.catalog.normalize_repository("100.66.177.70:5000/serverx-web"),
            "serverx-web",
        )
        # 2. Hostname with port and multi-level subpath
        self.assertEqual(
            self.catalog.normalize_repository("registry.internal:5050/subpath/app"),
            "subpath/app",
        )
        # 3. Localhost with port
        self.assertEqual(
            self.catalog.normalize_repository("localhost:5000/my-app"),
            "my-app",
        )
        # 4. Bare repository name
        self.assertEqual(
            self.catalog.normalize_repository("serverx-web"),
            "serverx-web",
        )
        # 5. Nested repository without host
        self.assertEqual(
            self.catalog.normalize_repository("signal/serverx-web"),
            "signal/serverx-web",
        )

        # Invalid formats must raise LumaError
        with self.assertRaises(LumaError):
            self.catalog.normalize_repository("100.66.177.70:5000/")
        with self.assertRaises(LumaError):
            self.catalog.normalize_repository("")
        with self.assertRaises(LumaError):
            self.catalog.normalize_repository("bad//repo")

    def test_obsolete_pin_pruning_beyond_window(self) -> None:
        """TC-F3-04: Verifies tags beyond rollback window N=5 are pruned from registry."""
        releases = []
        for i in range(8):
            art = BuildArtifact(
                artifact_id=f"art-prune-{i}",
                project="kato",
                source_repo="local",
                commit_sha=f"{i}" * 40,
                branch="main",
                manifest_kind="service",
                manifest_snapshot="name: kato\nregion: cn\nport: 8080\n",
                images={
                    "kato": ServiceImageArtifact(
                        service_name="kato",
                        repository="100.66.177.70:5000/kato",
                        digest=f"sha256:{i}" + "0" * 63,
                        tag=f"v{i}",
                    )
                },
                created_at=1700000000 + i * 100,
            )
            self.catalog.record_artifact(art)
            rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")
            releases.append(rel)

        # 8 releases -> 1 active (gen 8) + 5 rollback (gen 7..3) = 6 kept, 2 pruned (gen 2, 1)
        pruned = self.catalog.sync_retention_pins(releases, rollback_window=5)
        self.assertEqual(len(pruned["deleted_tags"]), 2)
        self.assertEqual(len(pruned["retained_tags"]), 6)
        self.assertIn("luma-pin-rollback-kato-prod-v1", pruned["deleted_tags"])
        self.assertIn("luma-pin-rollback-kato-prod-v2", pruned["deleted_tags"])
        self.assertIn("luma-pin-active-kato-prod", pruned["retained_tags"])
        self.assertIn("luma-pin-rollback-kato-prod-v7", pruned["retained_tags"])

    def test_flux_reconciliation_annotation_injection(self) -> None:
        """TC-F4-01: Verifies reconcile.fluxcd.io/requestedAt is injected into HelmRelease."""
        art = BuildArtifact(
            artifact_id="art-flux-001",
            project="bot",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: bot\nregion: cn\nport: 9000\n",
            images={
                "bot": ServiceImageArtifact(
                    service_name="bot",
                    repository="100.66.177.70:5000/bot",
                    digest="sha256:" + "2" * 64,
                    tag="v1",
                )
            },
            created_at=1700000000,
        )
        self.catalog.record_artifact(art)
        rel, workload = self.flux.deploy_artifact(art.artifact_id, environment="prod")
        hr = workload.helm_release_manifest
        annotations = hr.get("metadata", {}).get("annotations", {})
        self.assertIn("reconcile.fluxcd.io/requestedAt", annotations)

        # Timestamp must be non-empty ISO-8601 UTC
        timestamp = annotations["reconcile.fluxcd.io/requestedAt"]
        self.assertTrue(
            bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", timestamp)),
            f"Expected ISO-8601 UTC timestamp, got {timestamp}",
        )

    def test_flux_rollback_and_promotion_secret_preservation(self) -> None:
        """TC-F4-02 & TC-F4-03: Verifies secrets and mounted secret files are preserved across rollback and promotion."""
        art_v1 = BuildArtifact(
            artifact_id="art-sec-v1",
            project="sec-test",
            source_repo="local",
            commit_sha="e" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-test\nregion: cn\nport: 8080\n",
            images={
                "sec-test": ServiceImageArtifact(
                    "sec-test",
                    "100.66.177.70:5000/sec",
                    "sha256:" + "3" * 64,
                    "v1",
                )
            },
            created_at=1700000000,
        )
        self.catalog.record_artifact(art_v1)
        rel_v1, _ = self.flux.deploy_artifact(
            art_v1.artifact_id,
            environment="staging",
            secrets={"API_KEY": "staging-secret"},
            mounted_secret_files={"KEY_FILE": "/app/keys/staging.pem"},
        )

        # Promote to prod with environment-specific secrets
        rel_prod, prod_workload = self.flux.promote_artifact(
            project="sec-test",
            from_env="staging",
            to_env="prod",
            target_secrets={"API_KEY": "prod-secret"},
            target_mounted_secret_files={"KEY_FILE": "/app/keys/prod.pem"},
        )
        self.assertEqual(rel_prod.secrets["API_KEY"], "prod-secret")
        self.assertEqual(rel_prod.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")
        self.assertEqual(prod_workload.secrets[0]["stringData"]["API_KEY"], "prod-secret")

        # Rollback prod without overwriting secrets to empty
        rolled, rolled_workload = self.flux.rollback_release(
            project="sec-test",
            environment="prod",
            target_release_id=rel_prod.release_id,
        )
        self.assertEqual(rolled.secrets["API_KEY"], "prod-secret")
        self.assertEqual(rolled.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")
        self.assertEqual(rolled_workload.secrets[0]["stringData"]["API_KEY"], "prod-secret")

    def test_zero_releases_retention_sync_boundary(self) -> None:
        """TC-B-F3-04: Verifies boundary handling when release list or catalog is empty."""
        pruned = self.catalog.sync_retention_pins([], rollback_window=5)
        self.assertEqual(pruned["deleted_tags"], [])
        self.assertEqual(pruned["retained_tags"], [])

        pins = self.catalog.protected_pin_tags("nonexistent", "prod", rollback_window=5)
        self.assertEqual(pins, [])

        digests = self.catalog.protected_digests(rollback_window=5)
        self.assertEqual(digests, {})

    def test_duplicate_digests_in_multi_service_stack(self) -> None:
        """TC-F3-05 & TC-B-F3-05: Verifies multi-service compose stacks sharing identical base digests
        correctly pin retention tags across each distinct repository."""
        shared_digest = "sha256:" + "9" * 64
        art = BuildArtifact(
            artifact_id="art-shared-digest",
            project="shared-app",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: shared-app\nregion: cn\nservices:\n  web:\n    port: 8080\n  worker1:\n    replicas: 1\n  worker2:\n    replicas: 1\n",
            compose_snapshot="""
services:
  web:
    image: 100.66.177.70:5000/shared-web:v1
  worker1:
    image: 100.66.177.70:5000/shared-worker1:v1
  worker2:
    image: 100.66.177.70:5000/shared-worker2:v1
""",
            images={
                "web": ServiceImageArtifact("web", "100.66.177.70:5000/shared-web", "sha256:" + "8" * 64, "v1"),
                "worker1": ServiceImageArtifact("worker1", "100.66.177.70:5000/shared-worker1", shared_digest, "v1"),
                "worker2": ServiceImageArtifact("worker2", "100.66.177.70:5000/shared-worker2", shared_digest, "v1"),
            },
            created_at=1700000000,
        )
        self.catalog.record_artifact(art)
        self.flux.deploy_artifact(art.artifact_id, environment="prod")

        protected = self.catalog.protected_digests(rollback_window=5)
        self.assertIn(shared_digest, protected)
        entry = protected[shared_digest]
        self.assertIn("luma-pin-active-shared-app-prod", entry["pinTags"])

        # Mock Zot client to verify pin_digest is called for both distinct repositories
        mock_http = _MockRegistryHttpClient()
        client = ZotRegistryClient("http://100.66.177.70:5000", http_client=mock_http)
        res = client.sync_retention_pins(protected)
        self.assertGreaterEqual(res["appliedPinTags"], 2)

        put_paths = [call[1] for call in mock_http.requests if call[0] == "PUT"]
        self.assertTrue(any("/v2/shared-worker1/manifests/" in p for p in put_paths))
        self.assertTrue(any("/v2/shared-worker2/manifests/" in p for p in put_paths))

    def test_rollback_window_one_boundary(self) -> None:
        """TC-B-F3-03: Verifies rollback window N=1 retains exactly 1 active and 1 rollback release."""
        releases = []
        for i in range(4):
            art = BuildArtifact(
                artifact_id=f"art-win1-{i}",
                project="tiny",
                source_repo="local",
                commit_sha=f"{i}" * 40,
                branch="main",
                manifest_kind="service",
                manifest_snapshot="name: tiny\nregion: cn\nport: 8080\n",
                images={
                    "tiny": ServiceImageArtifact(
                        service_name="tiny",
                        repository="100.66.177.70:5000/tiny",
                        digest=f"sha256:{i}" + "1" * 63,
                        tag=f"v{i}",
                    )
                },
                created_at=1700000000 + i * 100,
            )
            self.catalog.record_artifact(art)
            rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")
            releases.append(rel)

        pruned = self.catalog.sync_retention_pins(releases, rollback_window=1)
        # 4 releases -> 1 active (gen 4) + 1 rollback (gen 3) = 2 kept, 2 pruned (gen 2, 1)
        self.assertEqual(len(pruned["retained_tags"]), 2)
        self.assertEqual(len(pruned["deleted_tags"]), 2)
        self.assertIn("luma-pin-active-tiny-prod", pruned["retained_tags"])
        self.assertIn("luma-pin-rollback-tiny-prod-v3", pruned["retained_tags"])
        self.assertIn("luma-pin-rollback-tiny-prod-v2", pruned["deleted_tags"])
        self.assertIn("luma-pin-rollback-tiny-prod-v1", pruned["deleted_tags"])

    def test_zot_registry_client_host_port_normalization_and_manifest_delete(self) -> None:
        """TC-B-F3-01: Verifies ZotRegistryClient strips host:port in pin_digest and delete_tag."""
        mock_http = _MockRegistryHttpClient()
        client = ZotRegistryClient("http://100.66.177.70:5000", http_client=mock_http)
        digest = "sha256:" + "a" * 64

        # pin_digest with host:port
        client.pin_digest("100.66.177.70:5000/serverx-web", digest, "luma-pin-active-serverx-prod")
        self.assertTrue(
            any(c[0] == "PUT" and c[1] == "/v2/serverx-web/manifests/luma-pin-active-serverx-prod" for c in mock_http.requests)
        )

        # delete_tag with host:port
        client.delete_tag("100.66.177.70:5000/serverx-web", "luma-pin-rollback-serverx-prod-v1")
        self.assertTrue(
            any(c[0] == "DELETE" and c[1] == "/v2/serverx-web/manifests/luma-pin-rollback-serverx-prod-v1" for c in mock_http.requests)
        )

    def test_adversarial_malformed_digest_and_repo_rejection(self) -> None:
        """TC-B-F3-02: Verifies malformed digests and repositories are rejected with LumaError."""
        mock_http = _MockRegistryHttpClient()
        client = ZotRegistryClient("http://100.66.177.70:5000", http_client=mock_http)

        with self.assertRaises(LumaError):
            client.pin_digest("serverx-web", "sha256:short", "luma-pin-active-app")
        with self.assertRaises(LumaError):
            client.pin_digest("serverx-web", "md5:12345", "luma-pin-active-app")
        with self.assertRaises(LumaError):
            client.pin_digest("100.66.177.70:5000/", "sha256:" + "a" * 64, "luma-pin-active-app")

    def test_promotion_preserves_target_environment_existing_secrets(self) -> None:
        """TC-F4-05: Verifies promoting artifact without target_secrets preserves target env's existing secrets."""
        art_stg = BuildArtifact(
            artifact_id="art-promo-01",
            project="promo-app",
            source_repo="local",
            commit_sha="1" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: promo-app\nregion: cn\nport: 8080\n",
            images={"promo-app": ServiceImageArtifact("promo-app", "100.66.177.70:5000/promo", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        self.catalog.record_artifact(art_stg)

        # Initial prod release with production secrets
        rel_prod_1, _ = self.flux.deploy_artifact(
            art_stg.artifact_id,
            environment="prod",
            secrets={"DB_URL": "postgres://prod-db:5432/db", "CLUSTER_ENV": "prod"},
            mounted_secret_files={"TLS_KEY": "/app/keys/prod.key"},
        )

        # Staging release with staging secrets
        rel_stg_1, _ = self.flux.deploy_artifact(
            art_stg.artifact_id,
            environment="staging",
            secrets={"DB_URL": "postgres://staging-db:5432/db", "CLUSTER_ENV": "staging"},
            mounted_secret_files={"TLS_KEY": "/app/keys/staging.key"},
        )

        # Promote from staging to prod WITHOUT passing secrets
        promoted, _ = self.flux.promote_artifact(
            project="promo-app",
            from_env="staging",
            to_env="prod",
        )
        # Must retain existing prod secrets, NOT staging secrets and NOT empty
        self.assertEqual(promoted.secrets["DB_URL"], "postgres://prod-db:5432/db")
        self.assertEqual(promoted.secrets["CLUSTER_ENV"], "prod")
        self.assertEqual(promoted.mounted_secret_files["TLS_KEY"], "/app/keys/prod.key")

    def test_cancellation_does_not_pin_or_corrupt_catalog_retention(self) -> None:
        """TC-X-03: Verifies canceled builds do not create retention pins or alter healthy release retention."""
        art = BuildArtifact(
            artifact_id="art-healthy-1",
            project="cancel-test",
            source_repo="local",
            commit_sha="2" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: cancel-test\nregion: cn\nport: 8080\n",
            images={"cancel-test": ServiceImageArtifact("cancel-test", "100.66.177.70:5000/cancel-test", "sha256:" + "2" * 64, "v1")},
            created_at=1700000000,
        )
        self.catalog.record_artifact(art)
        self.flux.deploy_artifact(art.artifact_id, environment="prod")

        pins_before = set(self.catalog.protected_pin_tags("cancel-test", "prod", rollback_window=5))
        self.assertEqual(pins_before, {"luma-pin-active-cancel-test-prod"})

        # Submit build and cancel it
        builder = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[
                BuilderEndpoint(
                    name="buildkit-amd64-0",
                    address="tcp://buildkitd.luma-builds.svc:1234",
                    platform="linux/amd64",
                    os_name="linux",
                )
            ],
        )
        run = builder.submit_build(
            project="cancel-test",
            repo_url="https://example.com/repo.git",
            revision="3" * 40,
            platform="linux/amd64",
        )
        builder.cancel_build(run["buildRunId"])

        # Retention pins after cancellation must remain intact
        pins_after = set(self.catalog.protected_pin_tags("cancel-test", "prod", rollback_window=5))
        self.assertEqual(pins_after, pins_before)
```

---

## 5. Verification Method

To verify the test suite once implemented by the M2 worker:
1. Run the test command:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" ./.venv/bin/python -m unittest tests.test_paas_engine -v
   ```
   **Success Criteria**:
   - `ZotGCRetentionAndFluxRollbackTests` passes all 12 test methods.
   - Total test count in `tests.test_paas_engine` increases from 15 to 27.
   - Zero test regressions in `PaaSEngineTests` and `NativeARM64AndTektonCancellationTests`.
2. Inspect target source files to ensure contract compliance:
   - `luma/paas/build/artifact_store.py`: `normalize_repository`, `protected_pin_tags`, `sync_retention_pins`, `ReleaseRecord.secrets`.
   - `luma/paas/release/helm_compiler.py`: `reconcile.fluxcd.io/requestedAt` in `compile_flux_helm_release`.
   - `luma/paas/release/flux_reconciler.py`: secret preservation on `rollback_release` and `promote_artifact`.
3. Invalidation Conditions:
   - If `normalize_repository` fails to strip registry host:port (e.g. `100.66.177.70:5000/app`), `test_registry_repository_validation_with_port` will fail with `LumaError: invalid registry repository`.
   - If pin tags are named `luma-pin-granary-prod-active` instead of `luma-pin-active-granary-prod`, `test_retention_pin_naming_convention` will fail.
   - If `reconcile.fluxcd.io/requestedAt` is absent from `HelmRelease.metadata.annotations`, `test_flux_reconciliation_annotation_injection` will fail.
   - If `rollback_release` or `promote_artifact` wipes secrets to empty dict, `test_flux_rollback_and_promotion_secret_preservation` will fail on `self.assertEqual(rolled.secrets["API_KEY"], "prod-secret")`.
