"""Adversarial challenger test suite for Milestone 2 - Feature 4: Flux <5s Rollback & Secret Parity.

Empirically tests:
1. rollback_release guards:
   - Rollback to currently active release raises LumaError("cannot rollback to currently active release")
   - Rollback with single release history raises LumaError
   - Rollback with empty release history raises LumaError
   - Rollback with nonexistent target_release_id raises LumaError
   - Rollback targeting non-healthy (failed / rolled_back) release raises LumaError
   - Rollback with invalid environments (qa, testing, invalid, empty) raises LumaError
   - Active release guard after multiple successful rollbacks

2. Secret preservation across repeated rollbacks and generations:
   - Zero-wipe guarantee: secrets and mounted_secret_files never reset to {} or None
   - Multi-generation rollback cycle (10+ generations back and forth)
   - Secret parity in both ReleaseRecord and CompiledWorkload (K8s Secret stringData & data)
   - Mounted secret file permissions (mode 0o400), volume mounts, and decoded payloads
   - Multiline, PEM, Unicode, JSON, and special character secret preservation
   - Scale service secret preservation

3. promote_artifact stress tests:
   - Rejection of invalid environments (qa, testing, invalid, production, empty)
   - Promotion without healthy source release raises LumaError
   - Secret preservation: target environment retains existing secrets when not overridden
   - Secret override: target_secrets and legacy secrets parameter override behavior
   - Clean promotion: promotes to empty target env defaulting to source secrets
   - Zero-rebuild guarantee: image digests and platforms match 100%

4. Timestamp format in reconcile.fluxcd.io/requestedAt:
   - Strict ISO 8601 UTC validation across service, compose, promote, rollback, and scale
   - UTC timezone offset verification (utcoffset == 0)
   - Freshness verification (timestamp generated at execution time)
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import re
import unittest

from luma.errors import LumaError
from luma.paas.build.artifact_store import (
    ArtifactCatalog,
    BuildArtifact,
    ServiceImageArtifact,
)
from luma.paas.release.flux_reconciler import FluxReconciler, VALID_ENVIRONMENTS
from luma.paas.release.helm_compiler import (
    compile_service_release,
    compile_compose_release,
)


class ChallengerM2Feature4StressTests(unittest.TestCase):
    """Rigorous empirical stress test harness for Feature 4."""

    def setUp(self) -> None:
        self.state: dict = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)

    def _create_service_artifact(
        self,
        artifact_id: str,
        project: str = "srv-app",
        tag: str = "v1",
        digest_suffix: str = "1",
        platform: str = "linux/amd64",
        port: int = 8080,
    ) -> BuildArtifact:
        art = BuildArtifact(
            artifact_id=artifact_id,
            project=project,
            source_repo="https://gcode.gaojiua.com/Signal/srv-app.git",
            commit_sha=digest_suffix * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot=f"name: {project}\nregion: cn\nport: {port}\n",
            images={
                project: ServiceImageArtifact(
                    service_name=project,
                    repository=f"100.66.177.70:5000/{project}",
                    digest=f"sha256:{(digest_suffix * 64)[:64]}",
                    tag=tag,
                    platform=platform,
                )
            },
            created_at=1700000000,
        )
        self.catalog.record_artifact(art)
        return art

    def _create_compose_artifact(
        self,
        artifact_id: str,
        project: str = "comp-app",
        tag: str = "v1",
    ) -> BuildArtifact:
        art = BuildArtifact(
            artifact_id=artifact_id,
            project=project,
            source_repo="https://gcode.gaojiua.com/Signal/comp-app.git",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=f"name: {project}\nregion: cn\nservices:\n  web:\n    port: 8080\n  worker:\n    replicas: 2\n",
            compose_snapshot="""
services:
  web:
    image: 100.66.177.70:5000/comp-web:v1
  worker:
    image: 100.66.177.70:5000/comp-worker:v1
""",
            images={
                "web": ServiceImageArtifact(
                    service_name="web",
                    repository="100.66.177.70:5000/comp-web",
                    digest="sha256:" + "a" * 64,
                    tag=tag,
                    platform="linux/amd64",
                ),
                "worker": ServiceImageArtifact(
                    service_name="worker",
                    repository="100.66.177.70:5000/comp-worker",
                    digest="sha256:" + "b" * 64,
                    tag=tag,
                    platform="linux/amd64",
                ),
            },
            created_at=1700000000,
        )
        self.catalog.record_artifact(art)
        return art

    # =========================================================================
    # 1. Rollback Guards Stress Tests
    # =========================================================================

    def test_rollback_to_currently_active_release_rejected(self) -> None:
        """Verify rollback targeting currently active release ID explicitly raises LumaError."""
        art1 = self._create_service_artifact("art-rb-01", digest_suffix="1")
        art2 = self._create_service_artifact("art-rb-02", digest_suffix="2")

        rel1, _ = self.flux.deploy_artifact(art1.artifact_id, environment="prod")
        rel2, _ = self.flux.deploy_artifact(art2.artifact_id, environment="prod")

        # rel2 is currently active. Attempting to rollback to rel2 must raise LumaError
        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id=rel2.release_id,
            )
        self.assertIn("cannot rollback to currently active release", str(ctx.exception))

    def test_rollback_with_single_release_in_history_rejected(self) -> None:
        """Verify rollback without target_release_id when only 1 release exists raises LumaError."""
        art = self._create_service_artifact("art-single", digest_suffix="1")
        rel, _ = self.flux.deploy_artifact(art.artifact_id, environment="prod")

        # No previous release available
        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertIn("no previous release available to roll back", str(ctx.exception))

        # Explicitly targeting the only release also raises active guard
        with self.assertRaises(LumaError) as ctx2:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id=rel.release_id,
            )
        self.assertIn("cannot rollback to currently active release", str(ctx2.exception))

    def test_rollback_with_empty_history_rejected(self) -> None:
        """Verify rollback when no releases exist raises LumaError."""
        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertIn("no healthy release history found", str(ctx.exception))

    def test_rollback_to_nonexistent_target_release_id_rejected(self) -> None:
        """Verify rollback targeting a nonexistent release ID raises LumaError."""
        art1 = self._create_service_artifact("art-rb-none-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-rb-none-2", digest_suffix="2")
        self.flux.deploy_artifact(art1.artifact_id, environment="prod")
        self.flux.deploy_artifact(art2.artifact_id, environment="prod")

        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id="rel-srv-app-prod-v999-deadbeef",
            )
        self.assertIn("target release rel-srv-app-prod-v999-deadbeef not found", str(ctx.exception))

    def test_rollback_targeting_failed_or_rolled_back_release_rejected(self) -> None:
        """Verify rollback cannot target releases with status != healthy."""
        art1 = self._create_service_artifact("art-fail-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-fail-2", digest_suffix="2")
        art3 = self._create_service_artifact("art-fail-3", digest_suffix="3")

        rel1, _ = self.flux.deploy_artifact(art1.artifact_id, environment="prod")
        # Deploy rel2 with failing gateway probe -> becomes rolled_back
        rel2_failed, _ = self.flux.deploy_artifact(
            art2.artifact_id,
            environment="prod",
            gateway_probe=lambda workload: False,
        )
        self.assertEqual(rel2_failed.status, "rolled_back")

        # Deploy rel3 healthy
        rel3, _ = self.flux.deploy_artifact(art3.artifact_id, environment="prod")

        # Attempt to target rel2_failed must be rejected because it is not healthy history
        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id=rel2_failed.release_id,
            )
        self.assertIn("not found", str(ctx.exception))

    def test_rollback_environment_validation(self) -> None:
        """Verify rollback rejects invalid environments (qa, testing, invalid, empty)."""
        self.assertEqual(VALID_ENVIRONMENTS, {"dev", "staging", "prod", "preview"})
        invalid_envs = ["qa", "testing", "invalid", "prod-2", "DEV", ""]
        for env in invalid_envs:
            with self.subTest(env=env):
                with self.assertRaises(LumaError) as ctx:
                    self.flux.rollback_release(project="srv-app", environment=env)
                self.assertIn(f"invalid environment {env!r}", str(ctx.exception))

    def test_active_guard_dynamically_tracks_newly_rolled_back_release(self) -> None:
        """Verify that after rolling back, the newly created release immediately becomes active and guarded."""
        art1 = self._create_service_artifact("art-chain-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-chain-2", digest_suffix="2")

        rel1, _ = self.flux.deploy_artifact(art1.artifact_id, environment="prod")
        rel2, _ = self.flux.deploy_artifact(art2.artifact_id, environment="prod")

        # Rollback to rel1 -> creates rel3 (generation 3)
        rel3, _ = self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertEqual(rel3.generation, 3)
        self.assertEqual(rel3.artifact_id, art1.artifact_id)

        # Now rel3 is active. Attempting to rollback to rel3 must be rejected
        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id=rel3.release_id,
            )
        self.assertIn("cannot rollback to currently active release", str(ctx.exception))

        # But we CAN rollback to rel2 (which is now in previous history)
        rel4, _ = self.flux.rollback_release(
            project="srv-app",
            environment="prod",
            target_release_id=rel2.release_id,
        )
        self.assertEqual(rel4.generation, 4)
        self.assertEqual(rel4.artifact_id, art2.artifact_id)

        # Now rel4 is active. Attempting to rollback to rel4 must be rejected
        with self.assertRaises(LumaError) as ctx2:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id=rel4.release_id,
            )
        self.assertIn("cannot rollback to currently active release", str(ctx2.exception))

    # =========================================================================
    # 2. Secret Preservation Stress Tests Across Repeated Generations
    # =========================================================================

    def test_repeated_multi_generation_rollback_secret_parity(self) -> None:
        """Stress test: 12 consecutive deploy and rollback operations verifying secrets and mounted files never wipe to {}."""
        # 3 base artifacts with distinct secret sets
        art1 = self._create_service_artifact("art-sec-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-sec-2", digest_suffix="2")
        art3 = self._create_service_artifact("art-sec-3", digest_suffix="3")

        sec_gen1 = {"DB_PASS": "pass_v1", "API_TOKEN": "tok_v1", "SHARED": "common"}
        sec_gen2 = {"DB_PASS": "pass_v2", "API_TOKEN": "tok_v2", "NEW_KEY": "val2", "SHARED": "common"}
        sec_gen3 = {"DB_PASS": "pass_v3", "API_TOKEN": "tok_v3", "SHARED": "common_v3"}

        files_gen1 = {"TLS_CERT": "/app/secrets/cert1.pem"}
        files_gen2 = {"TLS_CERT": "/app/secrets/cert2.pem", "EXTRA_FILE": "/app/secrets/extra.txt"}
        files_gen3 = {"TLS_CERT": "/app/secrets/cert3.pem"}

        # Gen 1, 2, 3
        rel1, w1 = self.flux.deploy_artifact(art1.artifact_id, environment="prod", secrets=sec_gen1, mounted_secret_files=files_gen1)
        rel2, w2 = self.flux.deploy_artifact(art2.artifact_id, environment="prod", secrets=sec_gen2, mounted_secret_files=files_gen2)
        rel3, w3 = self.flux.deploy_artifact(art3.artifact_id, environment="prod", secrets=sec_gen3, mounted_secret_files=files_gen3)

        # Rollback Cycle 1: Rollback from Gen 3 -> should target Gen 2 (default previous)
        rel4, w4 = self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertEqual(rel4.generation, 4)
        self.assertEqual(rel4.artifact_id, art2.artifact_id)
        self.assertEqual(rel4.secrets, sec_gen2)
        self.assertEqual(rel4.mounted_secret_files, files_gen2)
        self.assertEqual(w4.secrets[0]["stringData"], sec_gen2)

        # Rollback Cycle 2: Rollback from Gen 4 -> should target Gen 3 (previous healthy)
        rel5, w5 = self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertEqual(rel5.generation, 5)
        self.assertEqual(rel5.artifact_id, art3.artifact_id)
        self.assertEqual(rel5.secrets, sec_gen3)
        self.assertEqual(rel5.mounted_secret_files, files_gen3)
        self.assertEqual(w5.secrets[0]["stringData"], sec_gen3)

        # Rollback Cycle 3: Explicitly target Gen 1
        rel6, w6 = self.flux.rollback_release(project="srv-app", environment="prod", target_release_id=rel1.release_id)
        self.assertEqual(rel6.generation, 6)
        self.assertEqual(rel6.artifact_id, art1.artifact_id)
        self.assertEqual(rel6.secrets, sec_gen1)
        self.assertEqual(rel6.mounted_secret_files, files_gen1)
        self.assertEqual(w6.secrets[0]["stringData"], sec_gen1)

        # Rollback Cycle 4: Rollback from Gen 6 -> default targets Gen 5 (art3 / sec_gen3)
        rel7, w7 = self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertEqual(rel7.generation, 7)
        self.assertEqual(rel7.artifact_id, art3.artifact_id)
        self.assertEqual(rel7.secrets, sec_gen3)
        self.assertEqual(rel7.mounted_secret_files, files_gen3)

        # Scale service: scale to 3 replicas without losing secrets
        rel8, w8 = self.flux.scale_service(project="srv-app", service_name="srv-app", replicas=3, environment="prod")
        self.assertEqual(rel8.generation, 8)
        self.assertEqual(rel8.replicas_by_service["srv-app"], 3)
        self.assertEqual(rel8.secrets, sec_gen3)
        self.assertEqual(rel8.mounted_secret_files, files_gen3)
        self.assertEqual(w8.secrets[0]["stringData"], sec_gen3)

        # Rollback Cycle 5: Rollback from scale release (rel8) -> targets rel7
        rel9, w9 = self.flux.rollback_release(project="srv-app", environment="prod")
        self.assertEqual(rel9.generation, 9)
        self.assertEqual(rel9.secrets, sec_gen3)
        self.assertEqual(rel9.mounted_secret_files, files_gen3)

        # Rollback Cycle 6: Rollback with explicit secret override
        override_sec = {"OVERRIDE": "new_secret_val"}
        rel10, w10 = self.flux.rollback_release(
            project="srv-app",
            environment="prod",
            target_release_id=rel2.release_id,
            secrets=override_sec,
        )
        self.assertEqual(rel10.generation, 10)
        self.assertEqual(rel10.secrets, override_sec)
        self.assertEqual(w10.secrets[0]["stringData"], override_sec)
        # Mounted files should still come from target rel2
        self.assertEqual(rel10.mounted_secret_files, files_gen2)

        # Rollback Cycle 7: Rollback targeting rel10's parent (rel9)
        rel11, w11 = self.flux.rollback_release(
            project="srv-app",
            environment="prod",
            target_release_id=rel9.release_id,
        )
        self.assertEqual(rel11.generation, 11)
        self.assertEqual(rel11.secrets, sec_gen3)
        self.assertEqual(rel11.mounted_secret_files, files_gen3)

        # Rollback Cycle 8: Target rel6 (gen 1 secrets)
        rel12, w12 = self.flux.rollback_release(
            project="srv-app",
            environment="prod",
            target_release_id=rel6.release_id,
        )
        self.assertEqual(rel12.generation, 12)
        self.assertEqual(rel12.secrets, sec_gen1)
        self.assertEqual(rel12.mounted_secret_files, files_gen1)

    def test_special_characters_multiline_pem_and_unicode_secrets(self) -> None:
        """Verify secrets with special characters, multiline PEM, base64, Unicode, and JSON strings remain uncorrupted."""
        art = self._create_service_artifact("art-unicode", digest_suffix="c")

        pem_cert = """-----BEGIN CERTIFICATE-----
MIIDXTCCAkWgAwIBAgIJAL9+Fw4Y1234MA0GCSqGSIb3DQEBCwUAMEUxCzAJBgNV
BAYTAkNOMRAwDgYDVQQIDAdCZWlqaW5nMRAwDgYDVQQHDAdCZWlqaW5nMREwDwYD
VQQKDAhTaWduYWxDQTELMAkGA1UECwwCVjExEjAQBgNVBAMMCWxvY2FsaG9zdDAe
Fw0yNjEwMTUxNzAwMDBaFw0yNzEwMTUxNzAwMDBaMEUxCzAJBgNVBAYTAkNOMRAw
-----END CERTIFICATE-----"""

        complex_secrets = {
            "MULTILINE_PEM": pem_cert,
            "JSON_CONFIG": '{"nested": {"array": [1, 2, "three"], "active": true, "emoji": "🚀"}}',
            "SPECIAL_CHARS": "!@#$%^&*()_+-=[]{}|;':,./<>?`~",
            "UNICODE_TEXT": "测试中文字符串 - 平台工程 PaaS 架构",
            "BASE64_BLOB": base64.b64encode(b"\x00\x01\x02\xff\xfe\xfd\xaa\xbb").decode("ascii"),
        }
        mounted_files = {
            "MULTILINE_PEM": "/app/secrets/tls.crt",
            "JSON_CONFIG": "/app/config/config.json",
        }

        rel1, w1 = self.flux.deploy_artifact(
            art.artifact_id,
            environment="prod",
            secrets=complex_secrets,
            mounted_secret_files=mounted_files,
        )

        # Deploy v2 with different secrets
        art2 = self._create_service_artifact("art-unicode-2", digest_suffix="d")
        rel2, _ = self.flux.deploy_artifact(art2.artifact_id, environment="prod", secrets={"K": "v2"})

        # Rollback to rel1
        rolled, rolled_workload = self.flux.rollback_release(
            project="srv-app",
            environment="prod",
            target_release_id=rel1.release_id,
        )

        # Check exact byte-for-byte fidelity in ReleaseRecord
        for k, v in complex_secrets.items():
            self.assertEqual(rolled.secrets[k], v, f"Secret {k} corrupted across rollback")
        self.assertEqual(rolled.mounted_secret_files, mounted_files)

        # Check CompiledWorkload Secret manifest
        sec_manifest = rolled_workload.secrets[0]
        string_data = sec_manifest["stringData"]
        for k, v in complex_secrets.items():
            self.assertEqual(string_data[k], v, f"stringData[{k}] corrupted")

        # Verify mounted secret files items and mode
        binary_data = sec_manifest["data"]
        self.assertIn("file__multiline-pem", binary_data)
        self.assertIn("file__json-config", binary_data)

        # Check volume defaultMode: 0o400 (256 in decimal)
        vol = next((v for v in rolled_workload.deployments[0]["spec"]["template"]["spec"]["volumes"] if v["name"] == "luma-secret-files"), None)
        self.assertIsNotNone(vol)
        self.assertEqual(vol["secret"]["defaultMode"], 0o400)

    def test_compose_multi_service_secret_and_rollback_preservation(self) -> None:
        """Verify compose stacks with multi-service Deployments preserve secrets and volume mounts across rollback."""
        art1 = self._create_compose_artifact("art-comp-1", tag="v1")
        art2 = self._create_compose_artifact("art-comp-2", tag="v2")

        sec1 = {"SHARED_DB": "postgres://user:pass1@db:5432/main", "REDIS_URL": "redis://cache:6379/1"}
        files1 = {"SHARED_DB": "/app/secrets/db_conn.txt"}

        rel1, w1 = self.flux.deploy_artifact(
            art1.artifact_id,
            environment="prod",
            secrets=sec1,
            mounted_secret_files=files1,
        )
        self.assertEqual(len(w1.deployments), 2)  # independent deployments for web and worker

        rel2, w2 = self.flux.deploy_artifact(
            art2.artifact_id,
            environment="prod",
            secrets={"SHARED_DB": "postgres://user:pass2@db:5432/main"},
        )

        # Rollback compose to rel1
        rolled_rel, rolled_w = self.flux.rollback_release(
            project="comp-app",
            environment="prod",
            target_release_id=rel1.release_id,
        )
        self.assertEqual(rolled_rel.secrets, sec1)
        self.assertEqual(rolled_rel.mounted_secret_files, files1)

        # Both web and worker deployments must have the secretRef mounted
        for dep in rolled_w.deployments:
            containers = dep["spec"]["template"]["spec"]["containers"]
            for c in containers:
                secret_refs = [e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e]
                self.assertIn("comp-app-secrets", secret_refs)

    # =========================================================================
    # 3. promote_artifact Stress Tests
    # =========================================================================

    def test_promote_artifact_rejects_invalid_environments(self) -> None:
        """Verify promote_artifact rejects invalid source, target, and to_env strings with LumaError."""
        art = self._create_service_artifact("art-promo-envs")
        self.flux.deploy_artifact(art.artifact_id, environment="staging")

        invalid_targets = ["qa", "testing", "invalid", "production", "live", "dev_1", ""]
        for env in invalid_targets:
            # Test as target_env
            with self.subTest(target_env=env):
                with self.assertRaises(LumaError) as ctx:
                    self.flux.promote_artifact(
                        project="srv-app",
                        from_env="staging",
                        target_env=env,
                    )
                self.assertIn("invalid target environment", str(ctx.exception))

            # Test as to_env
            with self.subTest(to_env=env):
                with self.assertRaises(LumaError) as ctx2:
                    self.flux.promote_artifact(
                        project="srv-app",
                        from_env="staging",
                        to_env=env,
                    )
                self.assertIn("invalid target environment", str(ctx2.exception))

            # Test as from_env
            with self.subTest(from_env=env):
                with self.assertRaises(LumaError) as ctx3:
                    self.flux.promote_artifact(
                        project="srv-app",
                        from_env=env,
                        to_env="prod",
                    )
                self.assertIn("invalid source environment", str(ctx3.exception))

    def test_promote_artifact_rejects_when_source_has_no_healthy_releases(self) -> None:
        """Verify promote_artifact raises LumaError when from_env has no healthy releases."""
        art = self._create_service_artifact("art-no-src")
        # Record artifact but do NOT deploy to staging
        with self.assertRaises(LumaError) as ctx:
            self.flux.promote_artifact(
                project="srv-app",
                from_env="staging",
                to_env="prod",
            )
        self.assertIn("no healthy release found for srv-app in staging to promote", str(ctx.exception))

        # Even if staging has a failed release, it must be rejected
        self.flux.deploy_artifact(
            art.artifact_id,
            environment="staging",
            gateway_probe=lambda workload: False,
        )
        with self.assertRaises(LumaError) as ctx2:
            self.flux.promote_artifact(
                project="srv-app",
                from_env="staging",
                to_env="prod",
            )
        self.assertIn("no healthy release found", str(ctx2.exception))

    def test_promote_artifact_target_secret_precedence_matrix(self) -> None:
        """Verify all branches of secret resolution in promote_artifact:
        1. target_secrets explicit override
        2. legacy secrets parameter override
        3. target_secrets taking precedence over secrets if both provided
        4. target env existing secrets preserved when no overrides given
        5. source env secrets used when promoting to a brand new environment
        """
        art_stg = self._create_service_artifact("art-promo-matrix", digest_suffix="e")

        # Deploy in staging with staging secrets
        stg_secrets = {"DB_URL": "postgres://stg:5432/db", "ENV": "staging"}
        stg_files = {"KEY": "/app/keys/stg.pem"}
        self.flux.deploy_artifact(
            art_stg.artifact_id,
            environment="staging",
            secrets=stg_secrets,
            mounted_secret_files=stg_files,
        )

        # 1. Clean promotion to 'dev' (which has NO prior release): inherits staging secrets
        rel_dev, _ = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="dev",
        )
        self.assertEqual(rel_dev.secrets, stg_secrets)
        self.assertEqual(rel_dev.mounted_secret_files, stg_files)

        # Setup 'prod' with production secrets
        prod_existing_secrets = {"DB_URL": "postgres://prod:5432/db", "ENV": "prod", "PROD_ONLY": "xyz"}
        prod_existing_files = {"KEY": "/app/keys/prod.pem"}
        rel_prod_orig, _ = self.flux.deploy_artifact(
            art_stg.artifact_id,
            environment="prod",
            secrets=prod_existing_secrets,
            mounted_secret_files=prod_existing_files,
        )

        # 2. Promote to prod WITHOUT secrets: MUST retain prod_existing_secrets, NOT staging secrets
        rel_prod_retained, _ = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="prod",
        )
        self.assertEqual(rel_prod_retained.secrets, prod_existing_secrets)
        self.assertEqual(rel_prod_retained.mounted_secret_files, prod_existing_files)
        self.assertNotIn("xyz", stg_secrets)
        self.assertEqual(rel_prod_retained.secrets["PROD_ONLY"], "xyz")

        # 3. Promote to prod with explicit target_secrets: overrides
        rel_prod_override, _ = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="prod",
            target_secrets={"DB_URL": "postgres://prod-new:5432/db"},
            target_mounted_secret_files={"KEY": "/app/keys/prod-new.pem"},
        )
        self.assertEqual(rel_prod_override.secrets, {"DB_URL": "postgres://prod-new:5432/db"})
        self.assertEqual(rel_prod_override.mounted_secret_files, {"KEY": "/app/keys/prod-new.pem"})

        # 4. Promote to prod with legacy secrets parameter: overrides
        rel_prod_legacy, _ = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="prod",
            secrets={"DB_URL": "postgres://prod-legacy:5432/db"},
            mounted_secret_files={"KEY": "/app/keys/prod-legacy.pem"},
        )
        self.assertEqual(rel_prod_legacy.secrets, {"DB_URL": "postgres://prod-legacy:5432/db"})
        self.assertEqual(rel_prod_legacy.mounted_secret_files, {"KEY": "/app/keys/prod-legacy.pem"})

        # 5. When both target_secrets and secrets are passed, target_secrets takes precedence
        rel_prod_both, _ = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="prod",
            target_secrets={"DB_URL": "postgres://wins:5432/db"},
            secrets={"DB_URL": "postgres://loses:5432/db"},
        )
        self.assertEqual(rel_prod_both.secrets, {"DB_URL": "postgres://wins:5432/db"})

    def test_promote_artifact_zero_rebuild_invariant(self) -> None:
        """Verify promoted release strictly reuses the exact same immutable image repository, digest, and platform."""
        art = self._create_service_artifact("art-promo-digest", digest_suffix="7", platform="linux/arm64")
        self.flux.deploy_artifact(art.artifact_id, environment="staging")

        rel_prod, prod_workload = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="prod",
        )
        self.assertEqual(rel_prod.artifact_id, art.artifact_id)
        # Check container image in compiled Deployment
        container = prod_workload.deployments[0]["spec"]["template"]["spec"]["containers"][0]
        expected_image = f"100.66.177.70:5000/srv-app@sha256:{'7' * 64}"
        self.assertEqual(container["image"], expected_image)

    # =========================================================================
    # 4. Timestamp format in reconcile.fluxcd.io/requestedAt
    # =========================================================================

    def _assert_valid_iso8601_utc(self, timestamp: str) -> None:
        """Helper to verify strict ISO 8601 UTC timestamp format."""
        self.assertIsInstance(timestamp, str)
        self.assertGreater(len(timestamp), 0)

        # Strict ISO 8601 UTC regex matching YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+00:00)
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$"
        self.assertTrue(
            bool(re.match(pattern, timestamp)),
            f"Timestamp '{timestamp}' does not strictly match ISO 8601 UTC regex {pattern}",
        )

        # Parse with datetime.fromisoformat
        dt = datetime.fromisoformat(timestamp)
        self.assertIsNotNone(dt.tzinfo, "Timestamp lacks timezone info")
        self.assertEqual(
            dt.utcoffset(),
            timedelta(0),
            f"Timestamp timezone offset {dt.utcoffset()} is not UTC (timedelta(0))",
        )

        # Freshness: timestamp should be within 30 seconds of current UTC time
        now_utc = datetime.now(timezone.utc)
        diff = abs((now_utc - dt).total_seconds())
        self.assertLess(diff, 30.0, f"Timestamp {timestamp} differs from now ({now_utc}) by {diff}s")

    def test_timestamp_format_across_service_deploy(self) -> None:
        """Verify compile_service_release and deploy_artifact inject valid ISO 8601 UTC timestamp."""
        art = self._create_service_artifact("art-ts-svc")
        # Direct compilation test
        compiled = compile_service_release(artifact=art)
        ts_direct = compiled.helm_release_manifest["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"]
        self._assert_valid_iso8601_utc(ts_direct)

        # Deploy artifact test
        rel, workload = self.flux.deploy_artifact(art.artifact_id, environment="prod")
        annotations = workload.helm_release_manifest.get("metadata", {}).get("annotations", {})
        self.assertIn("reconcile.fluxcd.io/requestedAt", annotations)
        ts = annotations["reconcile.fluxcd.io/requestedAt"]
        self._assert_valid_iso8601_utc(ts)

    def test_timestamp_format_across_compose_deploy(self) -> None:
        """Verify compile_compose_release injects valid ISO 8601 UTC timestamp."""
        art = self._create_compose_artifact("art-ts-comp")
        # Direct compilation test
        compiled = compile_compose_release(artifact=art)
        ts_direct = compiled.helm_release_manifest["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"]
        self._assert_valid_iso8601_utc(ts_direct)

        # Deploy artifact test
        rel, workload = self.flux.deploy_artifact(art.artifact_id, environment="prod")
        annotations = workload.helm_release_manifest.get("metadata", {}).get("annotations", {})
        self.assertIn("reconcile.fluxcd.io/requestedAt", annotations)
        ts = annotations["reconcile.fluxcd.io/requestedAt"]
        self._assert_valid_iso8601_utc(ts)

    def test_timestamp_format_across_rollback_and_promotion(self) -> None:
        """Verify rollback_release and promote_artifact generate fresh ISO 8601 UTC timestamps."""
        art1 = self._create_service_artifact("art-ts-rel-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-ts-rel-2", digest_suffix="2")

        rel1, w1 = self.flux.deploy_artifact(art1.artifact_id, environment="staging")
        rel2, w2 = self.flux.deploy_artifact(art2.artifact_id, environment="staging")

        # Rollback
        rolled_rel, rolled_w = self.flux.rollback_release(project="srv-app", environment="staging")
        rolled_ts = rolled_w.helm_release_manifest["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"]
        self._assert_valid_iso8601_utc(rolled_ts)

        # Promotion
        promoted_rel, promoted_w = self.flux.promote_artifact(
            project="srv-app",
            from_env="staging",
            to_env="prod",
        )
        promoted_ts = promoted_w.helm_release_manifest["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"]
        self._assert_valid_iso8601_utc(promoted_ts)

    def test_custom_reconcile_requested_at_passthrough(self) -> None:
        """Verify compile_service_release / compile_flux_helm_release respects explicit reconcile_requested_at when provided."""
        from luma.paas.release.helm_compiler import compile_flux_helm_release
        art = self._create_service_artifact("art-custom-ts")

        custom_ts = "2026-10-15T12:34:56.789012Z"
        hr = compile_flux_helm_release(
            project="srv-app",
            environment="prod",
            namespace="luma-prod",
            generation=1,
            artifact=art,
            values={},
            reconcile_requested_at=custom_ts,
        )
        self.assertEqual(hr["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"], custom_ts)

    # =========================================================================
    # 5. Advanced Adversarial & Concurrency Invariants
    # =========================================================================

    def test_rollback_release_diff_integrity_without_secret_leak(self) -> None:
        """Verify release timeline diffs track secret changes and digest diffs without leaking plaintext secrets."""
        art1 = self._create_service_artifact("art-diff-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-diff-2", digest_suffix="2")

        rel1, _ = self.flux.deploy_artifact(art1.artifact_id, environment="prod", secrets={"S1": "super-secret-1", "SHARED": "v1"})
        rel2, _ = self.flux.deploy_artifact(art2.artifact_id, environment="prod", secrets={"S2": "super-secret-2", "SHARED": "v2"})

        rolled, _ = self.flux.rollback_release(project="srv-app", environment="prod")

        timeline = self.flux.release_timeline_with_diffs(project="srv-app", environment="prod")
        self.assertEqual(len(timeline), 3)

        # The newest release in timeline is the rolled-back release (gen 3)
        latest_entry = timeline[0]
        diff = latest_entry["diff"]

        # Diff must show environment changed
        self.assertTrue(diff["envChanged"])
        self.assertIn("S1", diff["addedSecrets"])
        self.assertIn("S2", diff["removedSecrets"])

        # Crucial security invariant: raw secret values must NOT appear in diff dictionary
        diff_str = str(diff)
        self.assertNotIn("super-secret-1", diff_str)
        self.assertNotIn("super-secret-2", diff_str)

    def test_stale_release_rejection_expected_min_generation(self) -> None:
        """Verify expected_min_generation prevents race conditions and out-of-order reconciliation."""
        art = self._create_service_artifact("art-stale")
        self.flux.deploy_artifact(art.artifact_id, environment="prod")
        self.flux.deploy_artifact(art.artifact_id, environment="prod")

        # Currently next generation is 3. If expected_min_generation is 5, it must be rejected
        with self.assertRaises(LumaError) as ctx:
            self.flux.deploy_artifact(
                art.artifact_id,
                environment="prod",
                expected_min_generation=5,
            )
        self.assertIn("stale release rejected", str(ctx.exception))

    def test_gateway_probe_failure_preserves_active_release_invariance(self) -> None:
        """Verify when a deploy fails its gateway probe, the prior healthy release remains active."""
        art1 = self._create_service_artifact("art-probe-1", digest_suffix="1")
        art2 = self._create_service_artifact("art-probe-2", digest_suffix="2")

        rel1, _ = self.flux.deploy_artifact(art1.artifact_id, environment="prod", secrets={"KEY": "stable"})
        self.assertEqual(rel1.status, "healthy")

        # Deploy rel2 with failed probe
        failed_rel, _ = self.flux.deploy_artifact(
            art2.artifact_id,
            environment="prod",
            secrets={"KEY": "broken"},
            gateway_probe=lambda workload: False,
        )
        self.assertEqual(failed_rel.status, "rolled_back")
        self.assertFalse(failed_rel.gateway_probe_ok)

        # The active release must still be rel1!
        releases = self.catalog.list_releases(project="srv-app", environment="prod")
        healthy = [r for r in releases if r.status == "healthy"]
        self.assertEqual(healthy[0].release_id, rel1.release_id)

        # Rolling back to rel1 must be blocked because rel1 IS the active release!
        with self.assertRaises(LumaError) as ctx:
            self.flux.rollback_release(
                project="srv-app",
                environment="prod",
                target_release_id=rel1.release_id,
            )
        self.assertIn("cannot rollback to currently active release", str(ctx.exception))

    def test_isolated_multi_project_rollback_independence(self) -> None:
        """Verify rollbacks on one project do not touch or leak secrets/state of another project in the same environment."""
        art_a1 = self._create_service_artifact("art-proj-a1", project="app-alpha", digest_suffix="1")
        art_a2 = self._create_service_artifact("art-proj-a2", project="app-alpha", digest_suffix="2")
        art_b1 = self._create_service_artifact("art-proj-b1", project="app-beta", digest_suffix="3")
        art_b2 = self._create_service_artifact("art-proj-b2", project="app-beta", digest_suffix="4")

        sec_alpha_1 = {"ALPHA_SEC": "alpha_1"}
        sec_alpha_2 = {"ALPHA_SEC": "alpha_2"}
        sec_beta_1 = {"BETA_SEC": "beta_1"}
        sec_beta_2 = {"BETA_SEC": "beta_2"}

        self.flux.deploy_artifact(art_a1.artifact_id, environment="prod", secrets=sec_alpha_1)
        rel_a2, _ = self.flux.deploy_artifact(art_a2.artifact_id, environment="prod", secrets=sec_alpha_2)

        self.flux.deploy_artifact(art_b1.artifact_id, environment="prod", secrets=sec_beta_1)
        rel_b2, _ = self.flux.deploy_artifact(art_b2.artifact_id, environment="prod", secrets=sec_beta_2)

        # Rollback app-alpha
        rolled_a, _ = self.flux.rollback_release(project="app-alpha", environment="prod")
        self.assertEqual(rolled_a.project, "app-alpha")
        self.assertEqual(rolled_a.secrets, sec_alpha_1)

        # Verify app-beta remains on rel_b2 with sec_beta_2 completely untouched
        beta_releases = self.catalog.list_releases(project="app-beta", environment="prod")
        beta_active = next(r for r in beta_releases if r.status == "healthy")
        self.assertEqual(beta_active.release_id, rel_b2.release_id)
        self.assertEqual(beta_active.secrets, sec_beta_2)

        # Rollback app-beta
        rolled_b, _ = self.flux.rollback_release(project="app-beta", environment="prod")
        self.assertEqual(rolled_b.project, "app-beta")
        self.assertEqual(rolled_b.secrets, sec_beta_1)

        # Alpha remains on rolled_a
        alpha_releases = self.catalog.list_releases(project="app-alpha", environment="prod")
        alpha_active = next(r for r in alpha_releases if r.status == "healthy")
        self.assertEqual(alpha_active.release_id, rolled_a.release_id)
        self.assertEqual(alpha_active.secrets, sec_alpha_1)

    def test_empty_secrets_dict_boundary_handling(self) -> None:
        """Verify explicit empty secrets ({}) is handled gracefully without producing invalid Kubernetes Secret."""
        art1 = self._create_service_artifact("art-empty-sec", digest_suffix="1")
        rel1, w1 = self.flux.deploy_artifact(art1.artifact_id, environment="prod", secrets={})
        self.assertEqual(rel1.secrets, {})
        self.assertEqual(w1.secrets[0]["stringData"], {})

        art2 = self._create_service_artifact("art-empty-sec-2", digest_suffix="2")
        rel2, w2 = self.flux.deploy_artifact(art2.artifact_id, environment="prod", secrets={"SOMETHING": "1"})

        # Rollback to rel1 with empty secrets
        rolled, rolled_w = self.flux.rollback_release(project="srv-app", environment="prod", target_release_id=rel1.release_id)
        self.assertEqual(rolled.secrets, {})
        self.assertEqual(rolled_w.secrets[0]["stringData"], {})


if __name__ == "__main__":
    unittest.main()

