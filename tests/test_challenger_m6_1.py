"""Tier 5 Adversarial Coverage Hardening for the Core PaaS Engine.

Challenger M6-1 test suite covering:
1. Platform validation, normalization, and topology inference (luma/build_capabilities.py)
2. BuildController builder routing, slot claims, Tekton cancellation & timeouts (luma/paas/build/controller.py)
3. Zot GC retention pins, tag pruning beyond N=5, repository parsing & deduplication (luma/paas/build/artifact_store.py)
4. Flux reconciler rollback annotations, secret & mounted secret file retention, zero-rebuild promotion (luma/paas/release/flux_reconciler.py)
5. Helm compiler arch affinity, Compose splitting, PVC mounts, 6 exposure modes, sidecar cycle detection (luma/paas/release/helm_compiler.py)
"""
from __future__ import annotations

import json
from typing import Any, Dict
import unittest

from luma.build_capabilities import infer_native_platform, validate_native_platform
from luma.errors import LumaError
from luma.paas.build.artifact_store import (
    ArtifactCatalog,
    BuildArtifact,
    ServiceImageArtifact,
    ZotRegistryClient,
    normalize_repository,
)
from luma.paas.build.controller import BuildController, BuilderEndpoint
from luma.paas.release.flux_reconciler import FluxReconciler
from luma.paas.release.helm_compiler import compile_compose_release, compile_service_release
from luma.registry_management import RegistryResponse


class _MockRegistryHttpClient:
    """Mock RegistryHttpClient for network-isolated testing of Zot OCI Registry."""

    def __init__(self, existing_tags: Dict[str, list[str]] | None = None) -> None:
        self.requests: list[tuple[str, str, str, bytes | None]] = []
        self._tags: Dict[str, list[str]] = existing_tags or {}

    def manifest(self, repository: str, digest: str) -> tuple[dict, RegistryResponse]:
        body = b'{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"}'
        headers = {"Content-Type": "application/vnd.oci.image.manifest.v1+json"}
        return json.loads(body), RegistryResponse(status=200, headers=headers, body=body)

    def tags(self, repository: str) -> list[str]:
        return list(self._tags.get(repository, []))

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
        status = expected[0] if (isinstance(expected, (tuple, list, set)) and len(expected) > 0) else 200
        return RegistryResponse(status=status, headers={}, body=b"")


class TestPlatformEdgeCasesAndInference(unittest.TestCase):
    """Adversarial stress tests for platform validation, normalization, and inference."""

    def test_whitespace_and_mixed_case_normalization(self) -> None:
        """TC-ADV-01: Whitespace and casing in platform strings normalize cleanly."""
        self.assertEqual(validate_native_platform("   LiNuX  /  ArM64   "), "linux/arm64")
        self.assertEqual(validate_native_platform("\t\n  LINUX / AMD64  \r\n"), "linux/amd64")
        self.assertEqual(validate_native_platform("DARWIN/ARM64"), "darwin/arm64")
        self.assertEqual(validate_native_platform("darwin/amd64"), "darwin/amd64")

    def test_architecture_alias_resolution(self) -> None:
        """TC-ADV-02: Valid architecture aliases normalize to canonical platform."""
        self.assertEqual(validate_native_platform("linux/aarch64"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/x86_64"), "linux/amd64")
        self.assertEqual(validate_native_platform("darwin/aarch64"), "darwin/arm64")
        self.assertEqual(validate_native_platform("darwin/x86_64"), "darwin/amd64")
        self.assertEqual(validate_native_platform("aarch64"), "linux/arm64")
        self.assertEqual(validate_native_platform("x86_64"), "linux/amd64")
        self.assertEqual(validate_native_platform("arm64"), "linux/arm64")
        self.assertEqual(validate_native_platform("amd64"), "linux/amd64")
        self.assertEqual(validate_native_platform("arm64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("aarch64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/arm64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/aarch64/v8"), "linux/arm64")

    def test_unsupported_operating_systems_rejection(self) -> None:
        """TC-ADV-03: Non-Linux/Darwin operating systems are rejected."""
        unsupported = [
            "windows/amd64",
            "windows/arm64",
            "solaris/sparc",
            "freebsd/amd64",
            "openbsd/amd64",
            "netbsd/arm64",
        ]
        for plat in unsupported:
            with self.subTest(platform=plat):
                with self.assertRaises(LumaError) as ctx:
                    validate_native_platform(plat)
                msg = str(ctx.exception).lower()
                self.assertTrue("unsupported" in msg and ("platform" in msg or "architecture" in msg))

    def test_unsupported_architectures_and_variants_rejection(self) -> None:
        """TC-ADV-04: Non-arm64/amd64 architectures and invalid variants are rejected."""
        invalid = [
            "linux/mips64",
            "linux/riscv64",
            "linux/s390x",
            "linux/ppc64le",
            "linux/arm/v7",
            "linux/arm/v6",
            "linux/armhf",
            "linux/arm64/v9",
            "linux/arm64/v7",
            "linux/amd64/v1",
            "linux/amd64/v2",
            "darwin/arm64/v9",
            "darwin/amd64/v2",
        ]
        for plat in invalid:
            with self.subTest(platform=plat):
                with self.assertRaises(LumaError) as ctx:
                    validate_native_platform(plat)
                msg = str(ctx.exception).lower()
                self.assertTrue("unsupported" in msg or "variant" in msg)

    def test_multi_platform_comma_lists_rejection(self) -> None:
        """TC-ADV-05: Multi-platform compilation lists are strictly rejected (zero QEMU)."""
        multi = [
            "linux/amd64,linux/arm64",
            "linux/arm64, linux/amd64",
            "linux/amd64,linux/arm64,linux/arm/v7",
            "linux/amd64,",
            ",linux/arm64",
        ]
        for plat in multi:
            with self.subTest(platform=plat):
                with self.assertRaises(LumaError) as ctx:
                    validate_native_platform(plat)
                self.assertIn("cross-architecture emulation", str(ctx.exception).lower())

    def test_malformed_platform_strings_rejection(self) -> None:
        """TC-ADV-06: Malformed platform strings and null values raise LumaError."""
        malformed = ["", "   ", "///", "//", "linux/arm64/v8/extra", None]
        for plat in malformed:
            with self.subTest(platform=plat):
                with self.assertRaises(LumaError):
                    validate_native_platform(plat)

    def test_topology_platform_inference_matrix(self) -> None:
        """TC-ADV-07: Inferences derived from regions, node names, and explicit node maps."""
        # 1. Home region defaults to linux/arm64 (Apple Silicon)
        self.assertEqual(infer_native_platform(target_region="home"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_region="HOME"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_region="  home  "), "linux/arm64")

        # 2. Known ARM nodes infer linux/arm64
        self.assertEqual(infer_native_platform(target_node="macbook-pro"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="MACBOOK-PRO"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="air3"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="k3s-arm64-worker-1"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="node-aarch64-box"), "linux/arm64")

        # 3. Explicit node_architectures map
        node_map = {
            "custom-arm-host": "linux/arm64",
            "custom-darwin-host": "darwin/arm64",
            "custom-amd-host": "linux/amd64",
        }
        self.assertEqual(
            infer_native_platform(target_node="custom-arm-host", node_architectures=node_map),
            "linux/arm64",
        )
        # darwin/arm64 in node_architectures maps cleanly to container target linux/arm64
        self.assertEqual(
            infer_native_platform(target_node="custom-darwin-host", node_architectures=node_map),
            "linux/arm64",
        )
        self.assertEqual(
            infer_native_platform(target_node="custom-amd-host", node_architectures=node_map),
            "linux/amd64",
        )

        # 4. Explicit platform takes precedence in inference
        self.assertEqual(
            infer_native_platform(platform="linux/amd64", target_region="home"),
            "linux/amd64",
        )
        self.assertEqual(
            infer_native_platform(platform="linux/arm64", target_node="custom-amd-host", node_architectures=node_map),
            "linux/arm64",
        )

        # 5. Default fallback is linux/amd64
        self.assertEqual(infer_native_platform(), "linux/amd64")
        self.assertEqual(infer_native_platform(target_region="cn"), "linux/amd64")
        self.assertEqual(infer_native_platform(target_node="standard-prod-node"), "linux/amd64")


class TestBuildControllerTektonLifecycleAndConcurrency(unittest.TestCase):
    """Adversarial stress tests for BuildController routing, concurrency, and cancellation."""

    def setUp(self) -> None:
        self.builders = [
            BuilderEndpoint(
                name="builder-amd64-1",
                address="tcp://buildkit-amd64.svc:1234",
                platform="linux/amd64",
                max_concurrency=2,
            ),
            BuilderEndpoint(
                name="builder-arm64-1",
                address="tcp://buildkit-arm64.svc:1234",
                platform="linux/arm64",
                max_concurrency=2,
            ),
            BuilderEndpoint(
                name="builder-arm64-offline",
                address="tcp://offline-arm64.svc:1234",
                platform="linux/arm64",
                max_concurrency=2,
                ready=False,
            ),
        ]
        self.controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=self.builders,
        )

    def test_pipeline_run_manifest_invariants(self) -> None:
        """TC-ADV-08: Tekton terminationGracePeriodSeconds and timeouts are injected."""
        run = self.controller.submit_build(
            project="shop-web",
            repo_url="https://gcode.example.com/shop.git",
            revision="1" * 40,
            branch="main",
            platform="linux/amd64",
        )
        spec = run["pipelineRunManifest"]["spec"]
        self.assertEqual(spec["podTemplate"]["terminationGracePeriodSeconds"], 3)
        self.assertEqual(spec["timeouts"]["pipeline"], "45m")
        self.assertEqual(spec["timeouts"]["tasks"], "30m")
        self.assertEqual(spec["timeouts"]["finally"], "3m")

    def test_concurrency_saturation_and_queue_transition(self) -> None:
        """TC-ADV-09: Submitting beyond max_concurrency marks builds queued without slot leak."""
        # builder-amd64-1 has max_concurrency = 2
        run1 = self.controller.submit_build(
            project="svc-1",
            repo_url="https://repo.git",
            revision="a" * 40,
            branch="feat-1",
            platform="linux/amd64",
        )
        run2 = self.controller.submit_build(
            project="svc-2",
            repo_url="https://repo.git",
            revision="b" * 40,
            branch="feat-2",
            platform="linux/amd64",
        )
        self.assertEqual(run1["status"], "running")
        self.assertEqual(run2["status"], "running")

        builder = next(b for b in self.controller.list_builders() if b.name == "builder-amd64-1")
        self.assertEqual(builder.active_builds, 2)
        self.assertEqual(builder.available_slots, 0)

        # 3rd and 4th builds must transition to "queued" status
        run3 = self.controller.submit_build(
            project="svc-3",
            repo_url="https://repo.git",
            revision="c" * 40,
            branch="feat-3",
            platform="linux/amd64",
        )
        run4 = self.controller.submit_build(
            project="svc-4",
            repo_url="https://repo.git",
            revision="d" * 40,
            branch="feat-4",
            platform="linux/amd64",
        )
        self.assertEqual(run3["status"], "queued")
        self.assertEqual(run4["status"], "queued")

        # Active builds should still be 2 (queued builds do not claim active slots)
        builder = next(b for b in self.controller.list_builders() if b.name == "builder-amd64-1")
        self.assertEqual(builder.active_builds, 2)

    def test_slot_release_on_completion_and_cancellation(self) -> None:
        """TC-ADV-10: Slot accounting decrements on completion/cancellation without underflow."""
        run1 = self.controller.submit_build(
            project="app1",
            repo_url="https://repo.git",
            revision="1" * 40,
            branch="b1",
            platform="linux/arm64",
        )
        run2 = self.controller.submit_build(
            project="app2",
            repo_url="https://repo.git",
            revision="2" * 40,
            branch="b2",
            platform="linux/arm64",
        )
        run_queued = self.controller.submit_build(
            project="app3",
            repo_url="https://repo.git",
            revision="3" * 40,
            branch="b3",
            platform="linux/arm64",
        )
        self.assertEqual(run_queued["status"], "queued")

        # 1. Canceling a queued build must not decrement active_builds
        self.controller.cancel_build(run_queued["buildRunId"])
        builder = next(b for b in self.controller.list_builders() if b.name == "builder-arm64-1")
        self.assertEqual(builder.active_builds, 2)

        # 2. Canceling a running build must decrement active_builds by 1
        self.controller.cancel_build(run1["buildRunId"])
        builder = next(b for b in self.controller.list_builders() if b.name == "builder-arm64-1")
        self.assertEqual(builder.active_builds, 1)

        # 3. Completing a running build must decrement active_builds by 1
        self.controller.complete_build(run2["buildRunId"], status="succeeded")
        builder = next(b for b in self.controller.list_builders() if b.name == "builder-arm64-1")
        self.assertEqual(builder.active_builds, 0)
        self.assertEqual(builder.available_slots, 2)

    def test_double_cancellation_and_terminal_mutation_guards(self) -> None:
        """TC-ADV-11: Mutating or re-canceling terminal builds raises LumaError."""
        run = self.controller.submit_build(
            project="guard-test",
            repo_url="https://repo.git",
            revision="e" * 40,
            branch="main",
            platform="linux/amd64",
        )
        run_id = run["buildRunId"]

        # Cancel build
        self.controller.cancel_build(run_id, reason="Operator abort")

        # Attempt to cancel again
        with self.assertRaisesRegex(LumaError, "already in terminal state 'canceled'"):
            self.controller.cancel_build(run_id)

        # Attempt to complete an already canceled build
        with self.assertRaisesRegex(LumaError, "already in terminal state 'canceled'"):
            self.controller.complete_build(run_id, status="succeeded")

        # Create another build and succeed it
        run2 = self.controller.submit_build(
            project="guard-test-2",
            repo_url="https://repo.git",
            revision="f" * 40,
            branch="main",
            platform="linux/amd64",
        )
        run2_id = run2["buildRunId"]
        self.controller.complete_build(run2_id, status="succeeded")

        # Attempt to cancel succeeded build
        with self.assertRaisesRegex(LumaError, "already in terminal state 'succeeded'"):
            self.controller.cancel_build(run2_id)

        # Attempt to complete with invalid status
        run3 = self.controller.submit_build(
            project="guard-test-3",
            repo_url="https://repo.git",
            revision="0" * 40,
            branch="main",
            platform="linux/amd64",
        )
        with self.assertRaisesRegex(LumaError, "invalid completion status"):
            self.controller.complete_build(run3["buildRunId"], status="invalid_status")

    def test_branch_run_superseding(self) -> None:
        """TC-ADV-12: Submitting on same project:branch:platform supersedes in-flight runs."""
        run1 = self.controller.submit_build(
            project="supersede-app",
            repo_url="https://repo.git",
            revision="1" * 40,
            branch="main",
            platform="linux/amd64",
        )
        self.assertEqual(run1["status"], "running")

        run2 = self.controller.submit_build(
            project="supersede-app",
            repo_url="https://repo.git",
            revision="2" * 40,
            branch="main",
            platform="linux/amd64",
        )
        self.assertEqual(run2["status"], "running")
        self.assertIn(run1["buildRunId"], run2["supersededRuns"])

        # run1 should now be canceled
        updated_run1 = self.controller.get_build(run1["buildRunId"])
        self.assertIsNotNone(updated_run1)
        self.assertEqual(updated_run1["status"], "canceled")
        self.assertIn("Superseded", updated_run1["cancelReason"])

    def test_idempotency_key_deduplication(self) -> None:
        """TC-ADV-13: Identical idempotency keys return the existing record without duplicate slots."""
        run1 = self.controller.submit_build(
            project="idem-app",
            repo_url="https://repo.git",
            revision="3" * 40,
            branch="main",
            platform="linux/amd64",
            idempotency_key="key-unique-12345",
        )
        builder_before = next(b for b in self.controller.list_builders() if b.name == "builder-amd64-1")

        run2 = self.controller.submit_build(
            project="idem-app",
            repo_url="https://repo.git",
            revision="3" * 40,
            branch="main",
            platform="linux/amd64",
            idempotency_key="key-unique-12345",
        )
        builder_after = next(b for b in self.controller.list_builders() if b.name == "builder-amd64-1")

        self.assertEqual(run1["buildRunId"], run2["buildRunId"])
        self.assertEqual(builder_before.active_builds, builder_after.active_builds)

    def test_builder_selection_guards_and_rejections(self) -> None:
        """TC-ADV-14: Offline, mismatched, and region/node conflicting builders are rejected."""
        # 1. Offline builder
        with self.assertRaisesRegex(LumaError, "requested builder node is offline"):
            self.controller.select_native_builder("linux/arm64", preferred_node="builder-arm64-offline")

        # 2. Unregistered builder
        with self.assertRaisesRegex(LumaError, "not registered"):
            self.controller.select_native_builder("linux/arm64", preferred_node="non-existent-builder")

        # 3. Builder architecture mismatch
        with self.assertRaisesRegex(LumaError, "does not match native target platform"):
            self.controller.select_native_builder("linux/arm64", preferred_node="builder-amd64-1")

        # 4. Region home requires ARM64
        with self.assertRaisesRegex(LumaError, "conflicts with target region home"):
            self.controller.select_native_builder("linux/amd64", target_region="home")

        # 5. Unsupported target platform
        with self.assertRaisesRegex(LumaError, "unsupported native (?:platform|architecture)"):
            self.controller.select_native_builder("linux/mips64")


class TestZotRetentionPinningAndPruningBoundaries(unittest.TestCase):
    """Adversarial stress tests for Zot GC pins, tag pruning, and repository normalization."""

    def setUp(self) -> None:
        self.mock_http = _MockRegistryHttpClient()
        self.zot_client = ZotRegistryClient("100.66.177.70:5000", http_client=self.mock_http)
        self.catalog = ArtifactCatalog()
        self.flux = FluxReconciler(self.catalog, zot_client=self.zot_client)

    def test_repository_normalization_adversarial_inputs(self) -> None:
        """TC-ADV-15: Normalization handles ports, hosts, schemes, and rejects malformed inputs."""
        valid_cases = [
            ("100.66.177.70:5000/serverx-web", "serverx-web"),
            ("http://100.66.177.70:5000/serverx-web", "serverx-web"),
            ("https://registry.infra.lan:8443/teams/app", "teams/app"),
            ("localhost:5000/app", "app"),
            ("app", "app"),
            ("myorg/my-sub-repo", "myorg/my-sub-repo"),
        ]
        for raw, expected in valid_cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_repository(raw), expected)

        invalid_cases = [
            "invalid..repo",
            "repo with space",
            "/",
            "",
            None,
        ]
        for raw in invalid_cases:
            with self.subTest(raw=raw):
                with self.assertRaises(LumaError):
                    normalize_repository(raw)

    def test_rollback_window_boundary_one(self) -> None:
        """TC-ADV-16: Rollback window N=1 keeps exactly 1 active and 1 rollback pin."""
        releases = []
        for i in range(1, 5):
            art = BuildArtifact(
                artifact_id=f"art-win1-v{i}",
                project="proj-win1",
                source_repo="local",
                commit_sha=f"{i}" * 40,
                branch="main",
                manifest_kind="service",
                manifest_snapshot="name: proj-win1\nregion: cn\nport: 8080\n",
                images={
                    "proj-win1": ServiceImageArtifact(
                        service_name="proj-win1",
                        repository="100.66.177.70:5000/proj-win1",
                        digest=f"sha256:{i}" + "0" * 63,
                        tag=f"v{i}",
                    )
                },
                created_at=1000 + i,
            )
            self.catalog.record_artifact(art)
            rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")
            releases.append(rel)

        # Window N=1: keeps active (v4) and 1 rollback (v3). Older v2 and v1 are pruned.
        sync_result = self.catalog.sync_retention_pins(releases, rollback_window=1, client=self.zot_client)
        self.assertIn("luma-pin-active-proj-win1-prod", sync_result.retained_tags)
        self.assertIn("luma-pin-rollback-proj-win1-prod-v3", sync_result.retained_tags)
        self.assertIn("luma-pin-rollback-proj-win1-prod-v2", sync_result.deleted_tags)
        self.assertIn("luma-pin-rollback-proj-win1-prod-v1", sync_result.deleted_tags)
        self.assertEqual(len(sync_result.retained_tags), 2)
        self.assertEqual(len(sync_result.deleted_tags), 2)

    def test_rollback_window_boundary_clamped_from_zero_or_negative(self) -> None:
        """TC-ADV-17: Rollback window <= 0 is safely clamped to 1."""
        releases = []
        for i in range(1, 4):
            art = BuildArtifact(
                artifact_id=f"art-clamp-v{i}",
                project="proj-clamp",
                source_repo="local",
                commit_sha=f"{i}" * 40,
                branch="main",
                manifest_kind="service",
                manifest_snapshot="name: proj-clamp\nregion: cn\nport: 8080\n",
                images={
                    "proj-clamp": ServiceImageArtifact(
                        service_name="proj-clamp",
                        repository="100.66.177.70:5000/proj-clamp",
                        digest=f"sha256:{i}" + "0" * 63,
                        tag=f"v{i}",
                    )
                },
                created_at=1000 + i,
            )
            self.catalog.record_artifact(art)
            rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")
            releases.append(rel)

        # Passing rollback_window=0 should clamp to 1
        sync_result = self.catalog.sync_retention_pins(releases, rollback_window=0)
        self.assertEqual(len(sync_result.retained_tags), 2)

    def test_multi_service_duplicate_digests_and_distinct_repositories(self) -> None:
        """TC-ADV-18: Multi-service compose release pins deduplicate correctly across repos."""
        shared_digest = "sha256:" + "a" * 64
        unique_digest = "sha256:" + "b" * 64

        art = BuildArtifact(
            artifact_id="art-compose-dups",
            project="multi-stack",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="""
name: multi-stack
region: cn
services:
  web:
    port: 3000
    domain: web.example.com
  worker-a:
    port: 0
  worker-b:
    port: 0
""",
            compose_snapshot="""
services:
  web:
    image: 100.66.177.70:5000/web:v1
  worker-a:
    image: 100.66.177.70:5000/workers:v1
  worker-b:
    image: 100.66.177.70:5000/workers:v1
""",
            images={
                "web": ServiceImageArtifact("web", "100.66.177.70:5000/web", unique_digest, "v1"),
                # worker-a and worker-b share the exact same repository and digest
                "worker-a": ServiceImageArtifact("worker-a", "100.66.177.70:5000/workers", shared_digest, "v1"),
                "worker-b": ServiceImageArtifact("worker-b", "100.66.177.70:5000/workers", shared_digest, "v1"),
            },
            created_at=2000,
        )
        self.catalog.record_artifact(art)
        self.flux.deploy_artifact(art.artifact_id, environment="prod")

        protected = self.catalog.protected_digests(rollback_window=5)
        # Verify both digests are protected
        self.assertIn(shared_digest, protected)
        self.assertIn(unique_digest, protected)

        sync_info = self.zot_client.sync_retention_pins(protected)
        # Applied pin tags must be 2 (one for web, one for workers), despite 3 services!
        self.assertEqual(sync_info["appliedPinTags"], 2)

    def test_delete_tag_adversarial_validation(self) -> None:
        """TC-ADV-19: Malformed tags with path traversals or control characters are rejected."""
        malformed_tags = [
            "",
            "   ",
            "tag/with/slash",
            "tag?param=1",
            "tag#hash",
            "tag\0null",
            "tag\nnewline",
            "tag\rcarriage",
        ]
        for tag in malformed_tags:
            with self.subTest(tag=tag):
                with self.assertRaises(LumaError):
                    self.zot_client.delete_tag("serverx-web", tag)


class TestFluxReconcilerRollbackSecretParityAndPromotion(unittest.TestCase):
    """Adversarial stress tests for Flux HelmRelease rollback, promotion, and secret retention."""

    def setUp(self) -> None:
        self.catalog = ArtifactCatalog()
        self.flux = FluxReconciler(self.catalog)

    def test_reconciliation_annotation_utc_iso8601(self) -> None:
        """TC-ADV-20: HelmRelease contains reconcile.fluxcd.io/requestedAt timestamp."""
        art = BuildArtifact(
            artifact_id="art-flux-ts",
            project="probe-app",
            source_repo="local",
            commit_sha="1" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: probe-app\nregion: cn\nport: 8080\n",
            images={"probe-app": ServiceImageArtifact("probe-app", "repo/app", "sha256:" + "1" * 64, "v1")},
            created_at=1000,
        )
        self.catalog.record_artifact(art)
        rel, workload = self.flux.deploy_artifact(art.artifact_id, environment="prod")
        hr = workload.helm_release_manifest
        self.assertIn("reconcile.fluxcd.io/requestedAt", hr["metadata"]["annotations"])

    def test_rollback_preserves_target_secrets_and_mounted_files_when_omitted(self) -> None:
        """TC-ADV-21: Rollback without secrets retains target release secrets and mounted files."""
        art_v1 = BuildArtifact(
            artifact_id="art-v1",
            project="sec-svc",
            source_repo="local",
            commit_sha="1" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-svc\nregion: cn\nport: 8080\n",
            images={"sec-svc": ServiceImageArtifact("sec-svc", "repo/sec", "sha256:" + "1" * 64, "v1")},
            created_at=1000,
        )
        art_v2 = BuildArtifact(
            artifact_id="art-v2",
            project="sec-svc",
            source_repo="local",
            commit_sha="2" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-svc\nregion: cn\nport: 8080\n",
            images={"sec-svc": ServiceImageArtifact("sec-svc", "repo/sec", "sha256:" + "2" * 64, "v2")},
            created_at=2000,
        )
        self.catalog.record_artifact(art_v1)
        self.catalog.record_artifact(art_v2)

        # Deploy v1 with secrets & mounted files
        rel_v1, _ = self.flux.deploy_artifact(
            art_v1.artifact_id,
            environment="prod",
            secrets={"JWT_KEY": "jwt-secret-v1", "DB_PASS": "pass-v1"},
            mounted_secret_files={"TLS_KEY": "/app/secrets/tls.key"},
        )

        # Deploy v2 with different secrets and no mounted files
        rel_v2, _ = self.flux.deploy_artifact(
            art_v2.artifact_id,
            environment="prod",
            secrets={"JWT_KEY": "jwt-secret-v2"},
            mounted_secret_files={},
        )

        # Rollback to v1 passing NO secrets (None)
        rolled, _ = self.flux.rollback_release(
            project="sec-svc",
            environment="prod",
            target_release_id=rel_v1.release_id,
        )
        self.assertEqual(rolled.secrets["JWT_KEY"], "jwt-secret-v1")
        self.assertEqual(rolled.secrets["DB_PASS"], "pass-v1")
        self.assertEqual(rolled.mounted_secret_files["TLS_KEY"], "/app/secrets/tls.key")

    def test_rollback_allows_explicit_secret_override_and_empty_reset(self) -> None:
        """TC-ADV-22: Rollback with explicit secrets overrides target release secrets."""
        art_v1 = BuildArtifact(
            artifact_id="art-v1-reset",
            project="reset-svc",
            source_repo="local",
            commit_sha="1" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: reset-svc\nregion: cn\nport: 8080\n",
            images={"reset-svc": ServiceImageArtifact("reset-svc", "repo/reset", "sha256:" + "3" * 64, "v1")},
            created_at=1000,
        )
        art_v2 = BuildArtifact(
            artifact_id="art-v2-reset",
            project="reset-svc",
            source_repo="local",
            commit_sha="2" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: reset-svc\nregion: cn\nport: 8080\n",
            images={"reset-svc": ServiceImageArtifact("reset-svc", "repo/reset", "sha256:" + "4" * 64, "v2")},
            created_at=2000,
        )
        self.catalog.record_artifact(art_v1)
        self.catalog.record_artifact(art_v2)

        self.flux.deploy_artifact(art_v1.artifact_id, environment="prod", secrets={"KEY": "v1"})
        self.flux.deploy_artifact(art_v2.artifact_id, environment="prod", secrets={"KEY": "v2"})

        # Rollback with explicit empty dict reset
        rolled_empty, _ = self.flux.rollback_release(
            project="reset-svc",
            environment="prod",
            secrets={},
        )
        self.assertEqual(rolled_empty.secrets, {})

    def test_rollback_guards_and_boundary_rejections(self) -> None:
        """TC-ADV-23: Rollback to self, missing release, or single release raises LumaError."""
        art = BuildArtifact(
            artifact_id="art-single",
            project="single-app",
            source_repo="local",
            commit_sha="5" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: single-app\nregion: cn\nport: 8080\n",
            images={"single-app": ServiceImageArtifact("single-app", "repo/app", "sha256:" + "5" * 64, "v1")},
            created_at=1000,
        )
        self.catalog.record_artifact(art)
        rel1, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")

        # Rollback with only 1 release in history
        with self.assertRaisesRegex(LumaError, "no previous release available to roll back"):
            self.flux.rollback_release(project="single-app", environment="prod")

        # Rollback to currently active release
        with self.assertRaisesRegex(LumaError, "cannot rollback to currently active release"):
            self.flux.rollback_release(
                project="single-app",
                environment="prod",
                target_release_id=rel1.release_id,
            )

        # Rollback to non-existent target ID
        with self.assertRaisesRegex(LumaError, "not found"):
            self.flux.rollback_release(
                project="single-app",
                environment="prod",
                target_release_id="rel-invalid-9999",
            )

    def test_promotion_preserves_production_secrets_with_staging_artifact(self) -> None:
        """TC-ADV-24: Promoting staging artifact to prod preserves production environment secrets."""
        art = BuildArtifact(
            artifact_id="art-promote-code",
            project="promo-app",
            source_repo="local",
            commit_sha="6" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: promo-app\nregion: cn\nport: 8080\n",
            images={"promo-app": ServiceImageArtifact("promo-app", "repo/app", "sha256:" + "6" * 64, "v1")},
            created_at=1000,
        )
        self.catalog.record_artifact(art)

        # Deploy v0 in prod with production database password
        self.flux.deploy_artifact(
            art.artifact_id,
            environment="prod",
            secrets={"DB_URL": "postgres://prod-db:5432/db", "ENV": "production"},
        )

        # Deploy v1 in staging with staging database password
        self.flux.deploy_artifact(
            art.artifact_id,
            environment="staging",
            secrets={"DB_URL": "postgres://staging-db:5432/db", "ENV": "staging"},
        )

        # Promote from staging to prod without specifying target_secrets
        promoted, _ = self.flux.promote_artifact(
            project="promo-app",
            from_env="staging",
            to_env="prod",
        )
        self.assertEqual(promoted.secrets["DB_URL"], "postgres://prod-db:5432/db")
        self.assertEqual(promoted.secrets["ENV"], "production")


class TestComposeExposureModesAndSidecarCycles(unittest.TestCase):
    """Adversarial stress tests for Compose exposure modes, PVCs, and sidecar network_mode loops."""

    def test_exposure_modes_matrix(self) -> None:
        """TC-ADV-25: Ingress and IngressRouteTCP generation across all 6 exposure modes."""
        modes = [
            ("none", "cn", False),
            ("cn-edge", "cn", True),
            ("external-edge", "global", True),
            ("tailscale-relay", "home", True),
            ("tcp-relay", "cn", True),
            ("cloudflare-tunnel", "cn", True),
        ]
        for mode, reg, has_ingress in modes:
            with self.subTest(exposure=mode, region=reg):
                art = BuildArtifact(
                    artifact_id=f"art-exp-{mode}",
                    project=f"exp-{mode}",
                    source_repo="local",
                    commit_sha="a" * 40,
                    branch="main",
                    manifest_kind="service",
                    manifest_snapshot=f"""
name: exp-{mode}
region: {reg}
exposure: {mode}
domain: {mode}.example.com
port: 8080
publishPort: 18080
""",
                    images={f"exp-{mode}": ServiceImageArtifact(f"exp-{mode}", "repo/app", "sha256:" + "7" * 64, "v1", platform="linux/arm64" if reg == "home" else "linux/amd64")},
                    created_at=1000,
                )
                compiled = compile_service_release(art)
                if not has_ingress:
                    self.assertEqual(len(compiled.ingresses), 0)
                else:
                    self.assertGreater(len(compiled.ingresses), 0)
                    ing = compiled.ingresses[0]
                    if mode == "tcp-relay":
                        self.assertEqual(ing["kind"], "IngressRouteTCP")
                    else:
                        self.assertEqual(ing["kind"], "Ingress")

    def test_exposure_mode_region_mismatches_rejection(self) -> None:
        """TC-ADV-26: Invalid exposure mode or region-exposure conflict raises LumaError."""
        # cn-edge in global region
        art_bad_reg = BuildArtifact(
            artifact_id="art-bad-reg",
            project="bad-reg",
            source_repo="local",
            commit_sha="8" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: bad-reg\nregion: global\nexposure: cn-edge\ndomain: x.com\nport: 80\n",
            images={"bad-reg": ServiceImageArtifact("bad-reg", "repo/app", "sha256:" + "8" * 64, "v1")},
            created_at=1000,
        )
        with self.assertRaises(LumaError):
            compile_service_release(art_bad_reg)

        # Invalid exposure mode string
        art_bad_exp = BuildArtifact(
            artifact_id="art-bad-exp",
            project="bad-exp",
            source_repo="local",
            commit_sha="9" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: bad-exp\nregion: cn\nexposure: unknown-mesh\ndomain: x.com\nport: 80\n",
            images={"bad-exp": ServiceImageArtifact("bad-exp", "repo/app", "sha256:" + "9" * 64, "v1")},
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "exposure must be one of"):
            compile_service_release(art_bad_exp)

    def test_tcp_relay_reserved_ports_and_compose_collisions(self) -> None:
        """TC-ADV-27: Reserved ports and compose port collisions raise LumaError."""
        # 1. Reserved port 80
        art_reserved = BuildArtifact(
            artifact_id="art-res-80",
            project="res-80",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: res-80\nregion: cn\nexposure: tcp-relay\nport: 80\n",
            images={"res-80": ServiceImageArtifact("res-80", "repo/app", "sha256:" + "a" * 64, "v1")},
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "reserved Traefik port"):
            compile_service_release(art_reserved)

        # 2. Port collision between two services in compose
        art_collision = BuildArtifact(
            artifact_id="art-collision",
            project="port-collision",
            source_repo="local",
            commit_sha="b" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="""
name: port-collision
region: cn
services:
  db1:
    exposure: tcp-relay
    publishPort: 15432
  db2:
    exposure: tcp-relay
    publishPort: 15432
""",
            compose_snapshot="""
services:
  db1:
    image: repo/db:v1
  db2:
    image: repo/db:v1
""",
            images={
                "db1": ServiceImageArtifact("db1", "repo/db", "sha256:" + "1" * 64, "v1"),
                "db2": ServiceImageArtifact("db2", "repo/db", "sha256:" + "2" * 64, "v1"),
            },
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "TCP relay port collision"):
            compile_compose_release(art_collision)

    def test_sidecar_self_referential_and_cyclic_dependency_detection(self) -> None:
        """TC-ADV-28: Self-referential loops, 2-service, and 3-service dependency cycles raise LumaError."""
        # 1. Self-referential loop (A -> A)
        art_self = BuildArtifact(
            artifact_id="art-sidecar-self",
            project="sidecar-self",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: sidecar-self\nregion: cn\nservices:\n  app:\n    port: 8080\n",
            compose_snapshot="""
services:
  app:
    image: repo/app:v1
    network_mode: service:app
""",
            images={"app": ServiceImageArtifact("app", "repo/app", "sha256:" + "3" * 64, "v1")},
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "cannot declare self-referential network_mode"):
            compile_compose_release(art_self)

        # 2. 2-service cycle (A -> B and B -> A)
        art_cycle2 = BuildArtifact(
            artifact_id="art-cycle2",
            project="cycle2",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: cycle2\nregion: cn\n",
            compose_snapshot="""
services:
  svc-a:
    image: repo/a:v1
    network_mode: service:svc-b
  svc-b:
    image: repo/b:v1
    network_mode: service:svc-a
""",
            images={
                "svc-a": ServiceImageArtifact("svc-a", "repo/a", "sha256:" + "4" * 64, "v1"),
                "svc-b": ServiceImageArtifact("svc-b", "repo/b", "sha256:" + "5" * 64, "v1"),
            },
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "cyclic network_mode dependency loop detected"):
            compile_compose_release(art_cycle2)

        # 3. 3-service cycle (A -> B -> C -> A)
        art_cycle3 = BuildArtifact(
            artifact_id="art-cycle3",
            project="cycle3",
            source_repo="local",
            commit_sha="e" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: cycle3\nregion: cn\n",
            compose_snapshot="""
services:
  svc-1:
    image: repo/1:v1
    network_mode: service:svc-2
  svc-2:
    image: repo/2:v1
    network_mode: service:svc-3
  svc-3:
    image: repo/3:v1
    network_mode: service:svc-1
""",
            images={
                "svc-1": ServiceImageArtifact("svc-1", "repo/1", "sha256:" + "6" * 64, "v1"),
                "svc-2": ServiceImageArtifact("svc-2", "repo/2", "sha256:" + "7" * 64, "v1"),
                "svc-3": ServiceImageArtifact("svc-3", "repo/3", "sha256:" + "8" * 64, "v1"),
            },
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "cyclic network_mode dependency loop detected"):
            compile_compose_release(art_cycle3)

    def test_sidecar_transitive_chain_and_architecture_parity(self) -> None:
        """TC-ADV-29: Transitive chains resolve to primary Pod; mixed architectures are rejected."""
        # Valid transitive chain (C -> B -> A where A is root primary)
        art_transitive = BuildArtifact(
            artifact_id="art-transitive",
            project="transitive",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: transitive\nregion: cn\nservices:\n  primary:\n    port: 8080\n",
            compose_snapshot="""
services:
  primary:
    image: repo/primary:v1
  sidecar-b:
    image: repo/b:v1
    network_mode: service:primary
  sidecar-c:
    image: repo/c:v1
    network_mode: service:sidecar-b
""",
            images={
                "primary": ServiceImageArtifact("primary", "repo/primary", "sha256:" + "9" * 64, "v1"),
                "sidecar-b": ServiceImageArtifact("sidecar-b", "repo/b", "sha256:" + "a" * 64, "v1"),
                "sidecar-c": ServiceImageArtifact("sidecar-c", "repo/c", "sha256:" + "b" * 64, "v1"),
            },
            created_at=1000,
        )
        compiled = compile_compose_release(art_transitive)
        # All 3 containers should be co-located in the single primary Deployment!
        self.assertEqual(len(compiled.deployments), 1)
        containers = compiled.deployments[0]["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 3)

        # Transitive chain with architecture mismatch (C is arm64, primary is amd64)
        art_mismatch = BuildArtifact(
            artifact_id="art-mismatch-chain",
            project="mismatch-chain",
            source_repo="local",
            commit_sha="0" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: mismatch-chain\nregion: cn\n",
            compose_snapshot="""
services:
  primary:
    image: repo/primary:v1
  sidecar-b:
    image: repo/b:v1
    network_mode: service:primary
  sidecar-c:
    image: repo/c:v1
    network_mode: service:sidecar-b
""",
            images={
                "primary": ServiceImageArtifact("primary", "repo/primary", "sha256:" + "1" * 64, "v1", platform="linux/amd64"),
                "sidecar-b": ServiceImageArtifact("sidecar-b", "repo/b", "sha256:" + "2" * 64, "v1", platform="linux/amd64"),
                "sidecar-c": ServiceImageArtifact("sidecar-c", "repo/c", "sha256:" + "3" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "cannot co-locate mixed-architecture containers"):
            compile_compose_release(art_mismatch)

    def test_volume_mounts_and_pvc_adversarial_validation(self) -> None:
        """TC-ADV-30: Duplicate mount paths, invalid storage sizes, and read-only flags."""
        # 1. Duplicate mount path in same container
        art_dup_mount = BuildArtifact(
            artifact_id="art-dup-mount",
            project="dup-mount",
            source_repo="local",
            commit_sha="1" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="""
name: dup-mount
region: cn
volumes:
  - data1:/app/data
  - data2:/app/data
""",
            images={"dup-mount": ServiceImageArtifact("dup-mount", "repo/app", "sha256:" + "4" * 64, "v1")},
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "duplicate mount path"):
            compile_service_release(art_dup_mount)

        # 2. Storage size missing unit
        art_bad_size = BuildArtifact(
            artifact_id="art-bad-size",
            project="bad-size",
            source_repo="local",
            commit_sha="2" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="""
name: bad-size
region: cn
storage:
  media:
    size: 50
volumes:
  - media:/var/media
""",
            images={"bad-size": ServiceImageArtifact("bad-size", "repo/app", "sha256:" + "5" * 64, "v1")},
            created_at=1000,
        )
        with self.assertRaisesRegex(LumaError, "must specify Kubernetes resource quantity units"):
            compile_service_release(art_bad_size)

        # 3. Read-only volume flag :ro
        art_ro = BuildArtifact(
            artifact_id="art-ro",
            project="ro-app",
            source_repo="local",
            commit_sha="3" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="""
name: ro-app
region: cn
volumes:
  - data:/app/config:ro
""",
            images={"ro-app": ServiceImageArtifact("ro-app", "repo/app", "sha256:" + "6" * 64, "v1")},
            created_at=1000,
        )
        compiled_ro = compile_service_release(art_ro)
        mounts = compiled_ro.deployments[0]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        ro_mount = next(m for m in mounts if m["mountPath"] == "/app/config")
        self.assertTrue(ro_mount.get("readOnly"))


if __name__ == "__main__":
    unittest.main()
