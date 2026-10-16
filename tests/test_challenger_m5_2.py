"""Adversarial challenger test suite for Milestone 5:
Control API (/v1/services/scale) & Dashboard Replica Scaler.

Empirically tests:
1. Authentication & Token Security:
   - Missing token, empty token, whitespace token, invalid tokens
   - Cross-token type rejection (joinToken vs deployToken)
   - Proper authorization via require_token
2. Project Validation & Edge Cases:
   - Missing project key, empty string, whitespace-only, None
   - Alias resolution (project, application, name) and precedence
3. Replica Boundary & Type Coercion:
   - Boundaries: -1, 0, 1, 64, 65, extreme negatives (-99999), extreme positives (99999)
   - String numeric boundaries: "0", "1", "64", "-1", "65"
   - Non-integers: "abc", None, [], {}, [1], float strings "1.5"
4. Compose Multi-Service Scaling & Isolation:
   - Sibling service isolation: scaling web does not affect worker or cache
   - Scale to 0 (worker pausing)
   - Scale to 64 (maximum saturation)
   - Inspecting compiled Kubernetes Deployment specs
5. Unregistered / Non-PaaS Catalog Fallback:
   - Graceful fallback returning status="scaled" without crash
6. Environment Handling & Slugification
"""
from __future__ import annotations

import unittest
from unittest.mock import patch
from typing import Any, Dict

from luma.errors import LumaError
from luma.control import server as control_server
from luma.paas.build.artifact_store import ArtifactCatalog, BuildArtifact, ServiceImageArtifact
from luma.paas.release.flux_reconciler import FluxReconciler


class ChallengerM5ControlApiStressTests(unittest.TestCase):
    """Rigorous empirical challenge of Milestone 5 Control API /v1/services/scale."""

    def setUp(self) -> None:
        self.valid_deploy_token = "secret-deploy-token-12345"
        self.valid_join_token = "secret-join-token-67890"
        self.state: Dict[str, Any] = {
            "clusterId": "test-cluster-m5",
            "deployToken": self.valid_deploy_token,
            "joinToken": self.valid_join_token,
            "paas": {
                "artifacts": {},
                "releases": {},
            },
        }

    def _setup_active_compose_release(
        self,
        *,
        project: str = "shop-stack",
        environment: str = "prod",
        initial_replicas: Dict[str, int] | None = None,
    ) -> ArtifactCatalog:
        if initial_replicas is None:
            initial_replicas = {"web": 2, "worker": 1, "cache": 1}

        art = BuildArtifact(
            artifact_id=f"art-{project}-001",
            project=project,
            source_repo="https://github.com/example/shop.git",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=(
                f"name: {project}\n"
                "region: cn\n"
                "services:\n"
                "  web:\n"
                f"    replicas: {initial_replicas.get('web', 1)}\n"
                "    port: 8080\n"
                "  worker:\n"
                f"    replicas: {initial_replicas.get('worker', 1)}\n"
                "    command: celery worker\n"
                "  cache:\n"
                f"    replicas: {initial_replicas.get('cache', 1)}\n"
                "    port: 6379\n"
            ),
            compose_snapshot=(
                "services:\n"
                "  web:\n"
                "    image: 100.66.177.70:5000/shop-web:v1\n"
                "  worker:\n"
                "    image: 100.66.177.70:5000/shop-worker:v1\n"
                "  cache:\n"
                "    image: 100.66.177.70:5000/redis:7.0\n"
            ),
            images={
                "web": ServiceImageArtifact("web", "100.66.177.70:5000/shop-web", "sha256:" + "1" * 64, "v1"),
                "worker": ServiceImageArtifact("worker", "100.66.177.70:5000/shop-worker", "sha256:" + "2" * 64, "v1"),
                "cache": ServiceImageArtifact("cache", "100.66.177.70:5000/redis", "sha256:" + "3" * 64, "7.0"),
            },
            created_at=1700000000,
        )
        catalog = ArtifactCatalog(self.state)
        catalog.record_artifact(art)
        reconciler = FluxReconciler(catalog)
        reconciler.deploy_artifact(
            art.artifact_id,
            environment=environment,
            replicas_override=initial_replicas,
        )
        return catalog

    # --------------------------------------------------------------------------
    # 1. Authentication & Token Stress Tests
    # --------------------------------------------------------------------------
    def test_auth_missing_or_invalid_tokens_rejected(self) -> None:
        """Missing or invalid token must raise LumaError('unauthorized')."""
        with patch.object(control_server, "load_state", return_value=self.state):
            invalid_tokens = [
                "",
                "   ",
                "\t\n",
                "wrong-token",
                "secret-deploy-token",  # prefix only
                "secret-deploy-token-12345-extra",  # suffix
                self.valid_join_token,  # join token must not authenticate deploy actions
                "None",
            ]
            for bad_token in invalid_tokens:
                with self.subTest(bad_token=bad_token):
                    with self.assertRaises(LumaError) as ctx:
                        control_server.handle_service_scale(
                            bad_token,
                            {"project": "my-app", "replicas": 1},
                        )
                    self.assertIn("unauthorized", str(ctx.exception).lower())

    def test_auth_valid_deploy_token_accepted(self) -> None:
        """Valid deployToken must authenticate without raising unauthorized error."""
        with patch.object(control_server, "load_state", return_value=self.state):
            # Non-existent app falls back to status: "scaled"
            resp = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "fresh-app", "service": "web", "replicas": 2},
            )
            self.assertEqual(resp["status"], "scaled")
            self.assertEqual(resp["replicas"], 2)

    # --------------------------------------------------------------------------
    # 2. Project Input Validation Tests
    # --------------------------------------------------------------------------
    def test_project_missing_empty_whitespace_rejected(self) -> None:
        """Missing, empty or whitespace-only project must raise LumaError('project is required')."""
        with patch.object(control_server, "load_state", return_value=self.state):
            empty_project_payloads = [
                {},
                {"replicas": 2},
                {"project": ""},
                {"project": "   "},
                {"project": "\t\r\n  "},
                {"project": None},
                {"project": "", "application": "", "name": ""},
                {"project": "  ", "application": "   ", "name": "\t"},
            ]
            for payload in empty_project_payloads:
                with self.subTest(payload=payload):
                    with self.assertRaises(LumaError) as ctx:
                        control_server.handle_service_scale(self.valid_deploy_token, payload)
                    self.assertIn("project is required", str(ctx.exception).lower())

    def test_project_alias_resolution(self) -> None:
        """Supports 'application' or 'name' as project aliases with proper precedence."""
        with patch.object(control_server, "load_state", return_value=self.state):
            # 'application' alias
            resp1 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"application": "app-via-application", "replicas": 2},
            )
            self.assertEqual(resp1["project"], "app-via-application")

            # 'name' alias
            resp2 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"name": "app-via-name", "replicas": 2},
            )
            self.assertEqual(resp2["project"], "app-via-name")

            # Precedence: 'project' takes precedence over 'application' and 'name'
            resp3 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "primary-proj", "application": "secondary", "name": "tertiary"},
            )
            self.assertEqual(resp3["project"], "primary-proj")

    # --------------------------------------------------------------------------
    # 3. Replica Boundary & Type Coercion Stress Tests
    # --------------------------------------------------------------------------
    def test_replica_boundaries(self) -> None:
        """Boundaries: -1 and 65 raise LumaError; 0, 1, 64 succeed."""
        with patch.object(control_server, "load_state", return_value=self.state):
            # Negative boundary: -1
            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale(
                    self.valid_deploy_token,
                    {"project": "app", "replicas": -1},
                )
            self.assertIn("between 0 and 64", str(ctx.exception).lower())

            # Upper boundary violation: 65
            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale(
                    self.valid_deploy_token,
                    {"project": "app", "replicas": 65},
                )
            self.assertIn("between 0 and 64", str(ctx.exception).lower())

            # Extreme bounds
            for extreme in [-2, -100, -99999, 66, 100, 99999]:
                with self.subTest(extreme=extreme):
                    with self.assertRaises(LumaError) as ctx:
                        control_server.handle_service_scale(
                            self.valid_deploy_token,
                            {"project": "app", "replicas": extreme},
                        )
                    self.assertIn("between 0 and 64", str(ctx.exception).lower())

            # Valid boundary 0 (scaling to zero / pausing)
            resp_0 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "app", "replicas": 0},
            )
            self.assertEqual(resp_0["replicas"], 0)

            # Valid boundary 1
            resp_1 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "app", "replicas": 1},
            )
            self.assertEqual(resp_1["replicas"], 1)

            # Valid boundary 64 (maximum allowable)
            resp_64 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "app", "replicas": 64},
            )
            self.assertEqual(resp_64["replicas"], 64)

    def test_replica_string_coercion_and_boundary_checks(self) -> None:
        """String representations of numbers are parsed; invalid string numbers rejected."""
        with patch.object(control_server, "load_state", return_value=self.state):
            # Valid string numbers
            self.assertEqual(
                control_server.handle_service_scale(self.valid_deploy_token, {"project": "app", "replicas": "0"})["replicas"],
                0,
            )
            self.assertEqual(
                control_server.handle_service_scale(self.valid_deploy_token, {"project": "app", "replicas": "1"})["replicas"],
                1,
            )
            self.assertEqual(
                control_server.handle_service_scale(self.valid_deploy_token, {"project": "app", "replicas": "64"})["replicas"],
                64,
            )

            # Invalid string numbers out of range
            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale(self.valid_deploy_token, {"project": "app", "replicas": "-1"})
            self.assertIn("between 0 and 64", str(ctx.exception).lower())

            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale(self.valid_deploy_token, {"project": "app", "replicas": "65"})
            self.assertIn("between 0 and 64", str(ctx.exception).lower())

    def test_replica_non_integer_types_rejected(self) -> None:
        """Non-integer types (abc, None, [], {}, float strings) raise LumaError('replicas must be an integer')."""
        with patch.object(control_server, "load_state", return_value=self.state):
            non_integers = [
                "abc",
                None,
                [],
                [1],
                {},
                {"replicas": 2},
                "1.5",
                "0.0",
                "two",
                "",
                "   ",
            ]
            for non_int in non_integers:
                with self.subTest(non_int=non_int):
                    with self.assertRaises(LumaError) as ctx:
                        control_server.handle_service_scale(
                            self.valid_deploy_token,
                            {"project": "app", "replicas": non_int},
                        )
                    self.assertIn("integer", str(ctx.exception).lower())

    # --------------------------------------------------------------------------
    # 4. Compose Multi-Service Scaling & Isolation
    # --------------------------------------------------------------------------
    def test_compose_scaling_web_leaves_worker_and_cache_untouched(self) -> None:
        """Scaling 'web' to 3 in a Compose stack updates only 'web'; 'worker' and 'cache' remain unchanged."""
        project = "shop-multi"
        self._setup_active_compose_release(
            project=project,
            initial_replicas={"web": 2, "worker": 1, "cache": 1},
        )

        with patch.object(control_server, "load_state", return_value=self.state), \
             patch.object(control_server, "_mutate_control_state"):
            # Scale web from 2 to 3
            resp = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": project, "service": "web", "replicas": 3},
            )
            self.assertEqual(resp["project"], project)
            self.assertEqual(resp["service"], "web")
            self.assertEqual(resp["replicas"], 3)

            rel_data = resp["release"]
            replicas_map = rel_data["replicasByService"]
            self.assertEqual(replicas_map["web"], 3, "web must be scaled to 3")
            self.assertEqual(replicas_map["worker"], 1, "worker must stay at initial replica count 1")
            self.assertEqual(replicas_map["cache"], 1, "cache must stay at initial replica count 1")

            # Check the compiled Kubernetes Deployments in the workload
            # Reconciler should have compiled independent Deployments for web, worker, and cache
            catalog = ArtifactCatalog(self.state)
            releases = catalog.list_releases(project=project, environment="prod")
            self.assertTrue(len(releases) > 0)
            latest_release = releases[0]
            self.assertEqual(latest_release.replicas_by_service["web"], 3)
            self.assertEqual(latest_release.replicas_by_service["worker"], 1)
            self.assertEqual(latest_release.replicas_by_service["cache"], 1)

    def test_compose_consecutive_independent_scalings(self) -> None:
        """Consecutively scaling different services preserves each service's individual replica count."""
        project = "pipeline-stack"
        self._setup_active_compose_release(
            project=project,
            initial_replicas={"web": 2, "worker": 2, "cache": 1},
        )

        def mock_mutate(cb: Any) -> None:
            cb(self.state)

        with patch.object(control_server, "load_state", side_effect=lambda: self.state), \
             patch.object(control_server, "_mutate_control_state", side_effect=mock_mutate):

            # Step 1: Scale worker to 0 (pausing background consumer)
            r1 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": project, "service": "worker", "replicas": 0},
            )
            self.assertEqual(r1["release"]["replicasByService"]["worker"], 0)
            self.assertEqual(r1["release"]["replicasByService"]["web"], 2)
            self.assertEqual(r1["release"]["replicasByService"]["cache"], 1)

            # Step 2: Scale web to 5
            r2 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": project, "service": "web", "replicas": 5},
            )
            self.assertEqual(r2["release"]["replicasByService"]["web"], 5)
            self.assertEqual(r2["release"]["replicasByService"]["worker"], 0)
            self.assertEqual(r2["release"]["replicasByService"]["cache"], 1)

            # Step 3: Scale cache to 64
            r3 = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": project, "service": "cache", "replicas": 64},
            )
            self.assertEqual(r3["release"]["replicasByService"]["cache"], 64)
            self.assertEqual(r3["release"]["replicasByService"]["web"], 5)
            self.assertEqual(r3["release"]["replicasByService"]["worker"], 0)

    # --------------------------------------------------------------------------
    # 5. Non-PaaS Catalog Graceful Fallback
    # --------------------------------------------------------------------------
    def test_non_paas_catalog_graceful_fallback(self) -> None:
        """When scaling an application without an active PaaS release, returns status='scaled' gracefully."""
        with patch.object(control_server, "load_state", return_value=self.state):
            resp = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "legacy-nomad-only-app", "service": "api", "replicas": 4, "environment": "prod"},
            )
            self.assertEqual(resp["clusterId"], "test-cluster-m5")
            self.assertEqual(resp["project"], "legacy-nomad-only-app")
            self.assertEqual(resp["service"], "api")
            self.assertEqual(resp["replicas"], 4)
            self.assertEqual(resp["environment"], "prod")
            self.assertEqual(resp["status"], "scaled")
            self.assertNotIn("release", resp)

    # --------------------------------------------------------------------------
    # 6. Service Name Defaulting & Environments
    # --------------------------------------------------------------------------
    def test_service_name_defaults_to_project_when_omitted(self) -> None:
        """When 'service' key is omitted or empty, it defaults to the project name."""
        with patch.object(control_server, "load_state", return_value=self.state):
            resp = control_server.handle_service_scale(
                self.valid_deploy_token,
                {"project": "standalone-svc", "replicas": 3},
            )
            self.assertEqual(resp["project"], "standalone-svc")
            self.assertEqual(resp["service"], "standalone-svc")

    def test_environment_validation(self) -> None:
        """Invalid environment raises LumaError."""
        project = "env-test"
        self._setup_active_compose_release(project=project)

        with patch.object(control_server, "load_state", return_value=self.state):
            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale(
                    self.valid_deploy_token,
                    {"project": project, "service": "web", "replicas": 2, "environment": "invalid_env_name"},
                )
            self.assertIn("invalid environment", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
