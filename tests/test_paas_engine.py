from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
from typing import Any
import unittest
from contextlib import redirect_stdout
import yaml

from luma.build_capabilities import infer_native_platform, validate_native_platform
from luma.cli.main import main as cli_main
from luma.errors import LumaError
from luma.paas import (
    ArtifactCatalog,
    BuildArtifact,
    BuildController,
    BuilderEndpoint,
    FluxReconciler,
    MigrationEngine,
    ServiceImageArtifact,
    ZotRegistryClient,
    accelerate_dockerfile_mirrors,
    compile_compose_release,
    compile_service_release,
    evaluate_node_self_heal,
    parse_buildkit_vertex_waterfall,
)
from luma.registry_management import RegistryResponse


class PaaSEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state: dict = {}
        self.catalog = ArtifactCatalog(self.state)
        self.builders = [
            BuilderEndpoint(
                name="buildkit-amd64-0",
                address="tcp://buildkitd.luma-builds.svc:1234",
                platform="linux/amd64",
                os_name="linux",
            ),
            BuilderEndpoint(
                name="macbook-pro",
                address="tcp://100.64.0.12:1234",
                platform="linux/arm64",
                os_name="darwin",
            ),
        ]
        self.builder = BuildController(
            registry_host="100.66.177.70:5000",
            builders=self.builders,
        )
        self.flux = FluxReconciler(self.catalog)
        self.migrator = MigrationEngine(self.catalog, self.flux)

    def test_dockerfile_apt_apk_https_mirror_acceleration(self) -> None:
        raw = (
            "FROM debian:bookworm-slim\n"
            "RUN sed -i 's|http://deb.debian.org|http://deb.debian.org|g' /etc/apt/sources.list\n"
            "FROM alpine:3.19\n"
            "RUN sed -i 's|http://dl-cdn.alpinelinux.org|http://dl-cdn.alpinelinux.org|g' /etc/apk/repositories\n"
        )
        rewritten, changed = accelerate_dockerfile_mirrors(raw)
        self.assertTrue(changed)
        self.assertIn("https://deb.debian.org", rewritten)
        self.assertIn("https://dl-cdn.alpinelinux.org", rewritten)

    def test_buildkit_waterfall_and_multirepo_git_contexts(self) -> None:
        events = [
            {
                "vertexes": [
                    {
                        "digest": "sha256:step1",
                        "name": "[internal] load build definition from Dockerfile",
                        "cached": False,
                        "durationMs": 120,
                    },
                    {
                        "digest": "sha256:step2",
                        "name": "[stage-0 1/3] FROM docker.io/library/node:20-alpine",
                        "cached": True,
                        "durationMs": 10,
                    },
                    {
                        "digest": "sha256:step3",
                        "name": "[stage-0 2/3] RUN npm ci",
                        "cached": False,
                        "durationMs": 4200,
                    },
                    {
                        "digest": "sha256:step4",
                        "name": "[auth] exporting to oci image format",
                        "cached": False,
                        "durationMs": 800,
                    },
                ]
            }
        ]
        stages, hit_ratio = parse_buildkit_vertex_waterfall(events)
        self.assertEqual(len(stages), 4)
        self.assertAlmostEqual(hit_ratio, 0.25)

        # Native arm64 build routes to macbook-pro and includes x-luma-git-contexts
        run_arm = self.builder.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            branch="main",
            platform="linux/arm64",
            build_spec={
                "x-luma-git-contexts": {
                    "xui": {
                        "lock": "xui.lock.json",
                    }
                }
            },
        )
        self.assertEqual(run_arm["builderNode"], "macbook-pro")
        self.assertIn("xui", run_arm["gitContexts"])
        self.assertEqual(run_arm["status"], "running")

        cancelled = self.builder.cancel_build(run_arm["buildRunId"])
        self.assertEqual(
            cancelled["pipelineRunManifest"]["spec"]["status"],
            "CancelledRunFinally",
        )
        self.assertTrue(cancelled["podTerminated"])
        self.assertTrue(cancelled["buildkitSessionAborted"])

    def test_luma_signature_features_in_service_and_compose_compiler(self) -> None:
        home_artifact = BuildArtifact(
            artifact_id="art-home-app-0001",
            project="home-app",
            source_repo="local",
            commit_sha="1111222233334444555566667777888899990000",
            branch="main",
            manifest_kind="service",
            manifest_snapshot="""
name: home-app
region: home
node: macbook-pro
exposure: tailscale-relay
domain: home-app.example.com
port: 3000
proxy: true
volumes:
  - media:/var/lib/home-app
storage:
  media:
    storageClass: local-path
    size: 50Gi
""",
            compose_snapshot="",
            images={
                "home-app": ServiceImageArtifact(
                    service_name="home-app",
                    repository="100.66.177.70:5000/home-app",
                    digest="sha256:" + "a" * 64,
                    tag="v1",
                    platform="linux/arm64",
                )
            },
            created_at=1700000000,
            builder_node="macbook-pro",
        )
        compiled = compile_service_release(
            artifact=home_artifact,
            environment="prod",
            secrets={"JWT_KEY": "super-secret", "PEM_B64": "LS0tLS1CRUdJTitLRVktLS0tLQ=="},
            mounted_secret_files={"PEM_B64": "/app/secrets/jwt.pem"},
        )
        dep = compiled.deployments[0]
        pod_spec = dep["spec"]["template"]["spec"]
        self.assertEqual(
            pod_spec["nodeSelector"],
            {
                "luma.io/region": "home",
                "topology.kubernetes.io/region": "home",
                "kubernetes.io/hostname": "macbook-pro",
                "kubernetes.io/arch": "arm64",
            },
        )
        env_names = {item["name"] for item in pod_spec["containers"][0]["env"]}
        self.assertIn("HTTP_PROXY", env_names)
        self.assertIn("NO_PROXY", env_names)
        self.assertEqual(len(compiled.pvcs), 1)
        self.assertEqual(compiled.pvcs[0]["metadata"]["annotations"]["luma.io/reclaim-policy"], "Retain")
        ing = compiled.ingresses[0]
        self.assertEqual(ing["metadata"]["annotations"]["luma.io/exposure"], "tailscale-relay")
        self.assertEqual(ing["metadata"]["annotations"]["luma.io/relay-transport"], "tailscale-mesh")

        # Test tcp-relay exposure mode -> Traefik IngressRouteTCP
        tcp_art = BuildArtifact(
            artifact_id="art-redis-relay-0001",
            project="redis-relay",
            source_repo="local",
            commit_sha="2222333344445555666677778888999900001111",
            branch="main",
            manifest_kind="service",
            manifest_snapshot="""
name: redis-relay
region: cn
exposure: tcp-relay
port: 6379
publishPort: 16379
""",
            compose_snapshot="",
            images={
                "redis-relay": ServiceImageArtifact(
                    service_name="redis-relay",
                    repository="100.66.177.70:5000/redis-relay",
                    digest="sha256:" + "b" * 64,
                    tag="v1",
                    platform="linux/amd64",
                )
            },
            created_at=1700000010,
            builder_node="buildkit-amd64-0",
        )
        tcp_compiled = compile_service_release(artifact=tcp_art)
        self.assertEqual(tcp_compiled.ingresses[0]["kind"], "IngressRouteTCP")
        self.assertEqual(tcp_compiled.ingresses[0]["spec"]["entryPoints"], ["tcp-16379"])

        # Region <-> exposure mismatch should raise LumaError
        invalid_art = BuildArtifact(
            artifact_id="art-bad-region-0001",
            project="bad-region",
            source_repo="local",
            commit_sha="3333444455556666777788889999000011112222",
            branch="main",
            manifest_kind="service",
            manifest_snapshot="""
name: bad-region
region: global
exposure: cn-edge
domain: bad.example.com
port: 8080
""",
            compose_snapshot="",
            images={
                "bad-region": ServiceImageArtifact(
                    service_name="bad-region",
                    repository="100.66.177.70:5000/bad-region",
                    digest="sha256:" + "c" * 64,
                    tag="v1",
                    platform="linux/amd64",
                )
            },
            created_at=1700000020,
            builder_node="buildkit-amd64-0",
        )
        with self.assertRaises(LumaError):
            compile_service_release(artifact=invalid_art)

    def test_compose_independent_deployments_flux_promotion_and_zot_gc_pins(self) -> None:
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/serverx-web:v1
  browser-worker:
    image: 100.66.177.70:5000/serverx-browser:v1
  video-worker:
    image: 100.66.177.70:5000/serverx-video:v1
  sidecar-agent:
    image: 100.66.177.70:5000/serverx-agent:v1
    network_mode: service:web
"""
        luma_compose_yaml = """
name: serverx
region: cn
proxy: true
services:
  web:
    port: 8080
    domain: serverx.example.com
    exposure: cn-edge
    replicas: 1
  browser-worker:
    replicas: 1
  video-worker:
    replicas: 2
"""
        art_v1 = BuildArtifact(
            artifact_id="art-serverx-v1",
            project="serverx",
            source_repo="https://gcode.gaojiua.com/Signal/serverx.git",
            commit_sha="aaaa1111" * 5,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_compose_yaml,
            compose_snapshot=compose_yaml,
            images={
                "web": ServiceImageArtifact(
                    service_name="web",
                    repository="100.66.177.70:5000/serverx-web",
                    digest="sha256:" + "1" * 64,
                    tag="v1",
                ),
                "browser-worker": ServiceImageArtifact(
                    service_name="browser-worker",
                    repository="100.66.177.70:5000/serverx-browser",
                    digest="sha256:" + "2" * 64,
                    tag="v1",
                ),
                "video-worker": ServiceImageArtifact(
                    service_name="video-worker",
                    repository="100.66.177.70:5000/serverx-video",
                    digest="sha256:" + "3" * 64,
                    tag="v1",
                ),
                "sidecar-agent": ServiceImageArtifact(
                    service_name="sidecar-agent",
                    repository="100.66.177.70:5000/serverx-agent",
                    digest="sha256:" + "4" * 64,
                    tag="v1",
                ),
            },
            created_at=1700000100,
            builder_node="buildkit-amd64-0",
        )
        self.catalog.record_artifact(art_v1)

        compiled = compile_compose_release(artifact=art_v1)
        # 3 independent Deployments (web, browser-worker, video-worker), sidecar-agent attached to web Pod!
        self.assertEqual(len(compiled.deployments), 3)
        self.assertEqual(compiled.sidecar_pairs, {"sidecar-agent": "web"})
        web_deploy = next(d for d in compiled.deployments if d["metadata"]["name"] == "serverx-web")
        self.assertEqual(len(web_deploy["spec"]["template"]["spec"]["containers"]), 2)

        rel_v1, _ = self.flux.deploy_artifact(artifact_id=art_v1.artifact_id, environment="staging")
        self.assertEqual(rel_v1.generation, 1)

        # Promote from staging to prod with zero rebuild
        rel_prod, _ = self.flux.promote_artifact(
            project="serverx",
            from_env="staging",
            to_env="prod",
        )
        self.assertEqual(rel_prod.artifact_id, art_v1.artifact_id)

        # Scale only video-worker without restarting web or browser-worker
        scaled, _ = self.flux.scale_service(
            project="serverx",
            service_name="video-worker",
            replicas=5,
            environment="prod",
        )
        self.assertEqual(scaled.replicas_by_service["video-worker"], 5)
        self.assertEqual(scaled.replicas_by_service["web"], 1)

        # Deploy v2 and rollback to v1 while verifying Zot GC pin protection
        art_v2 = BuildArtifact(
            artifact_id="art-serverx-v2",
            project="serverx",
            source_repo="https://gcode.gaojiua.com/Signal/serverx.git",
            commit_sha="bbbb2222" * 5,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_compose_yaml,
            compose_snapshot=compose_yaml,
            images={
                "web": ServiceImageArtifact(
                    service_name="web",
                    repository="100.66.177.70:5000/serverx-web",
                    digest="sha256:" + "5" * 64,
                    tag="v2",
                ),
                "browser-worker": ServiceImageArtifact(
                    service_name="browser-worker",
                    repository="100.66.177.70:5000/serverx-browser",
                    digest="sha256:" + "2" * 64,
                    tag="v1",
                ),
                "video-worker": ServiceImageArtifact(
                    service_name="video-worker",
                    repository="100.66.177.70:5000/serverx-video",
                    digest="sha256:" + "3" * 64,
                    tag="v1",
                ),
                "sidecar-agent": ServiceImageArtifact(
                    service_name="sidecar-agent",
                    repository="100.66.177.70:5000/serverx-agent",
                    digest="sha256:" + "4" * 64,
                    tag="v1",
                ),
            },
            created_at=1700000200,
            builder_node="buildkit-amd64-0",
        )
        self.catalog.record_artifact(art_v2)
        rel_v2, _ = self.flux.deploy_artifact(artifact_id=art_v2.artifact_id, environment="prod")
        self.assertEqual(rel_v2.generation, 3)

        timeline = self.flux.release_timeline_with_diffs(project="serverx", environment="prod")
        self.assertEqual(len(timeline), 3)

        rolled, _ = self.flux.rollback_release(
            project="serverx",
            environment="prod",
            target_release_id=rel_prod.release_id,
        )
        self.assertEqual(rolled.artifact_id, art_v1.artifact_id)
        self.assertEqual(rolled.status, "healthy")

        protected = self.catalog.protected_digests(rollback_window=5)
        self.assertIn("sha256:" + "1" * 64, protected)
        self.assertIn("sha256:" + "5" * 64, protected)

    def test_four_step_migration_engine_and_cli_surface(self) -> None:
        manifest_text = "name: serverx\nregion: cn\ndomain: serverx.example.com\n"
        compose_text = """
services:
  web:
    image: 100.66.177.70:5000/serverx-web:v1
  video-worker:
    image: 100.66.177.70:5000/serverx-video:v1
"""
        plan = self.migrator.plan(
            project="serverx",
            manifest_text=manifest_text,
            compose_text=compose_text,
        )
        self.assertEqual(plan.batch.batch_id, "batch2-compose-workers")
        self.assertEqual(plan.independent_deployments, ["video-worker", "web"])

        shadow_state = self.migrator.shadow(
            project="serverx",
            manifest_text=manifest_text,
            compose_text=compose_text,
            running_images={
                "web": {"repository": "100.66.177.70:5000/serverx-web", "digest": "sha256:" + "9" * 64},
                "video-worker": {"repository": "100.66.177.70:5000/serverx-video", "digest": "sha256:" + "8" * 64},
            },
        )
        self.assertEqual(shadow_state["state"], "shadow_ready")
        self.assertEqual(shadow_state["trafficTarget"], "nomad")

        cutover_state = self.migrator.cutover("serverx")
        self.assertEqual(cutover_state["state"], "cutover_complete")
        self.assertEqual(cutover_state["trafficTarget"], "k3s-flux")
        self.assertEqual(cutover_state["legacyNomadStatus"], "standby")

        abort_state = self.migrator.abort("serverx")
        self.assertEqual(abort_state["trafficTarget"], "nomad")

        self.migrator.cutover("serverx")
        final_state = self.migrator.finalize("serverx")
        self.assertEqual(final_state["state"], "finalized")
        self.assertEqual(final_state["legacyNomadStatus"], "purged")

        heal = evaluate_node_self_heal(
            {
                "name": "prod-cn-1",
                "osName": "linux",
                "registriesYamlConverged": False,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": False,
                "diskUsedPercent": 82.0,
            },
            auto_fix=True,
        )
        self.assertTrue(heal.healthy)
        self.assertGreaterEqual(len(heal.repaired_actions), 3)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["migrate", "batches", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        batches = payload.get("result", payload)["batches"]
        self.assertEqual(len(batches), 3)

    def test_cloudflare_zone_id_env_propagation(self) -> None:
        """Verifies CLOUDFLARE_ZONE_ID is treated as a valid Luma setting and loaded from .env."""
        import tempfile
        from luma.envfile import is_luma_setting, load_luma_settings
        self.assertTrue(is_luma_setting("CLOUDFLARE_ZONE_ID"))
        self.assertTrue(is_luma_setting("CLOUDFLARE_API_TOKEN"))

        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "CLOUDFLARE_ZONE_ID=zone_test_12345\n"
                "CLOUDFLARE_API_TOKEN=token_test_abc\n"
                "CUSTOM_UNKNOWN_VAR=ignore_me\n",
                encoding="utf-8",
            )
            old_zone = os.environ.get("CLOUDFLARE_ZONE_ID")
            old_token = os.environ.get("CLOUDFLARE_API_TOKEN")
            try:
                self.assertEqual(load_luma_settings(env_file), [])
                self.assertEqual(os.environ.get("CLOUDFLARE_ZONE_ID"), "zone_test_12345")
                self.assertEqual(os.environ.get("CLOUDFLARE_API_TOKEN"), "token_test_abc")
                self.assertNotIn("CUSTOM_UNKNOWN_VAR", os.environ)
            finally:
                if old_zone is None:
                    os.environ.pop("CLOUDFLARE_ZONE_ID", None)
                else:
                    os.environ["CLOUDFLARE_ZONE_ID"] = old_zone
                if old_token is None:
                    os.environ.pop("CLOUDFLARE_API_TOKEN", None)
                else:
                    os.environ["CLOUDFLARE_API_TOKEN"] = old_token

    def test_control_handle_service_scale_endpoint(self) -> None:
        """Verifies /v1/services/scale endpoint validates inputs and scales PaaS release."""
        from unittest.mock import patch
        from luma.control import server as control_server

        # Test validation: missing project
        with patch.object(control_server, "load_state", return_value={"deployToken": "tok"}), \
             patch.object(control_server, "require_token"):
            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale("tok", {"project": "", "replicas": 2})
            self.assertIn("project is required", str(ctx.exception).lower())

            # Test validation: invalid replicas range
            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale("tok", {"project": "app", "replicas": -1})
            self.assertIn("between 0 and 64", str(ctx.exception).lower())

            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale("tok", {"project": "app", "replicas": 65})
            self.assertIn("between 0 and 64", str(ctx.exception).lower())

            with self.assertRaises(LumaError) as ctx:
                control_server.handle_service_scale("tok", {"project": "app", "replicas": "invalid"})
            self.assertIn("integer", str(ctx.exception).lower())

            # Test scaling when no active PaaS release exists in state -> graceful status "scaled"
            resp = control_server.handle_service_scale("tok", {"project": "nonexistent", "service": "web", "replicas": 3})
            self.assertEqual(resp["project"], "nonexistent")
            self.assertEqual(resp["service"], "web")
            self.assertEqual(resp["replicas"], 3)
            self.assertEqual(resp["status"], "scaled")

            # Test scaling with an active release in state
            art = BuildArtifact(
                artifact_id="art-scale-test",
                project="scale-test",
                source_repo="local",
                commit_sha="a" * 40,
                branch="main",
                manifest_kind="compose",
                manifest_snapshot="name: scale-test\nregion: cn\nservices:\n  web:\n    port: 8080\n  worker:\n    command: run\n",
                compose_snapshot="services:\n  web:\n    image: 100.66.177.70:5000/web:v1\n  worker:\n    image: 100.66.177.70:5000/worker:v1\n",
                images={
                    "web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "1" * 64, "v1"),
                    "worker": ServiceImageArtifact("worker", "100.66.177.70:5000/worker", "sha256:" + "2" * 64, "v1"),
                },
                created_at=1700000000,
            )
            catalog = ArtifactCatalog(self.state)
            catalog.record_artifact(art)
            reconciler = FluxReconciler(catalog)
            reconciler.deploy_artifact(art.artifact_id, environment="prod")

            with patch.object(control_server, "load_state", return_value=self.state):
                resp2 = control_server.handle_service_scale("tok", {"project": "scale-test", "service": "worker", "replicas": 4})
                self.assertEqual(resp2["project"], "scale-test")
                self.assertEqual(resp2["service"], "worker")
                self.assertEqual(resp2["replicas"], 4)
                self.assertEqual(resp2["release"]["replicasByService"]["worker"], 4)
                self.assertEqual(resp2["release"]["replicasByService"]["web"], 1)



class NativeARM64AndTektonCancellationTests(unittest.TestCase):
    """Verifies strict native-only building (zero QEMU/cross-compilation),
    topology platform inference, builder concurrency slots, and Tekton <3s
    build cancellation lifecycle."""

    def setUp(self) -> None:
        self.builders = [
            BuilderEndpoint(
                name="buildkit-amd64-0",
                address="tcp://buildkitd.luma-builds.svc:1234",
                platform="linux/amd64",
                os_name="linux",
                max_concurrency=4,
                active_builds=0,
                ready=True,
            ),
            BuilderEndpoint(
                name="macbook-pro",
                address="tcp://100.64.0.12:1234",
                platform="linux/arm64",
                os_name="darwin",
                max_concurrency=4,
                active_builds=0,
                ready=True,
            ),
            BuilderEndpoint(
                name="microos-arm64-1",
                address="tcp://100.64.0.15:1234",
                platform="linux/arm64",
                os_name="opensuse-microos",
                max_concurrency=2,
                active_builds=0,
                ready=True,
            ),
        ]
        self.controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=self.builders,
        )

    def test_validate_native_platform_canonical_and_aliases(self) -> None:
        self.assertEqual(validate_native_platform("linux/arm64"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/amd64"), "linux/amd64")
        self.assertEqual(validate_native_platform("linux/arm64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/aarch64"), "linux/arm64")
        self.assertEqual(validate_native_platform("linux/x86_64"), "linux/amd64")
        self.assertEqual(validate_native_platform("arm64"), "linux/arm64")
        self.assertEqual(validate_native_platform("arm64/v8"), "linux/arm64")
        self.assertEqual(validate_native_platform("aarch64"), "linux/arm64")
        self.assertEqual(validate_native_platform("amd64"), "linux/amd64")
        self.assertEqual(validate_native_platform("x86_64"), "linux/amd64")
        self.assertEqual(validate_native_platform("darwin/arm64"), "darwin/arm64")
        self.assertEqual(validate_native_platform("darwin/x86_64"), "darwin/amd64")
        self.assertEqual(validate_native_platform("  LiNuX/ArM64  "), "linux/arm64")

    def test_validate_native_platform_rejects_multi_platform(self) -> None:
        err = "cross-architecture emulation and multi-platform compilation are disabled; submit builds separately for each native architecture"
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/amd64,linux/arm64")
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/arm64,linux/amd64")
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/amd64, linux/arm64")
        with self.assertRaisesRegex(LumaError, err):
            validate_native_platform("linux/arm64,")

    def test_validate_native_platform_rejects_unsupported(self) -> None:
        with self.assertRaises(LumaError):
            validate_native_platform("")
        with self.assertRaises(LumaError):
            validate_native_platform(None)
        with self.assertRaises(LumaError):
            validate_native_platform("linux/arm/v7")
        with self.assertRaises(LumaError):
            validate_native_platform("windows/amd64")
        with self.assertRaises(LumaError):
            validate_native_platform("linux/arm64/v7")

    def test_infer_native_platform_rules(self) -> None:
        # Rule 1: Explicit platform overrides all
        self.assertEqual(
            infer_native_platform(platform="linux/arm64", target_region="cn"),
            "linux/arm64",
        )
        self.assertEqual(
            infer_native_platform(platform="linux/amd64", target_region="home"),
            "linux/amd64",
        )

        # Rule 2: Empty platform + home region -> linux/arm64
        self.assertEqual(infer_native_platform(target_region="home"), "linux/arm64")

        # Rule 3: Node architectures mapping
        nodes = {
            "microos-arm-01": "linux/arm64",
            "microos-amd-01": "linux/amd64",
            "macmini": "darwin/arm64",
        }
        self.assertEqual(
            infer_native_platform(target_node="microos-arm-01", node_architectures=nodes),
            "linux/arm64",
        )
        self.assertEqual(
            infer_native_platform(target_node="microos-amd-01", node_architectures=nodes),
            "linux/amd64",
        )
        self.assertEqual(
            infer_native_platform(target_node="macmini", node_architectures=nodes),
            "linux/arm64",
        )

        # Rule 4: Node name heuristics
        self.assertEqual(infer_native_platform(target_node="macbook-pro"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="air3"), "linux/arm64")
        self.assertEqual(infer_native_platform(target_node="opensuse-microos-arm64"), "linux/arm64")

        # Rule 5: Default fallback
        self.assertEqual(infer_native_platform(), "linux/amd64")
        self.assertEqual(infer_native_platform(target_region="cn"), "linux/amd64")

    def test_native_builder_selection_and_conflict_rejection(self) -> None:
        # Target region home -> selects ARM64 builder
        b_home = self.controller.select_native_builder(target_region="home")
        self.assertEqual(b_home.platform, "linux/arm64")

        # Target node macbook-pro -> selects macbook-pro
        b_node = self.controller.select_native_builder(target_node="macbook-pro")
        self.assertEqual(b_node.name, "macbook-pro")
        self.assertEqual(b_node.platform, "linux/arm64")

        # Target node air3 (not a builder, but ARM64) -> selects an ARM64 builder
        b_air3 = self.controller.select_native_builder(target_node="air3")
        self.assertEqual(b_air3.platform, "linux/arm64")

        # Default -> buildkit-amd64-0
        b_default = self.controller.select_native_builder()
        self.assertEqual(b_default.name, "buildkit-amd64-0")
        self.assertEqual(b_default.platform, "linux/amd64")

        # Rejection of multi-platform
        with self.assertRaisesRegex(LumaError, "cross-architecture emulation"):
            self.controller.select_native_builder("linux/amd64,linux/arm64")

        # Conflict: explicit AMD64 targeting home
        with self.assertRaisesRegex(LumaError, "conflicts with target region home"):
            self.controller.select_native_builder("linux/amd64", target_region="home")

        # Conflict: explicit AMD64 targeting macbook-pro
        with self.assertRaisesRegex(LumaError, "conflicts with target node macbook-pro"):
            self.controller.select_native_builder("linux/amd64", target_node="macbook-pro")

        # Preferred node matching
        b_pref = self.controller.select_native_builder("linux/arm64", preferred_node="macbook-pro")
        self.assertEqual(b_pref.name, "macbook-pro")

        # Preferred node mismatch
        with self.assertRaisesRegex(LumaError, "does not match native target platform"):
            self.controller.select_native_builder("linux/arm64", preferred_node="buildkit-amd64-0")

        # Preferred node not registered
        with self.assertRaisesRegex(LumaError, "requested builder node is not registered"):
            self.controller.select_native_builder("linux/arm64", preferred_node="unknown-builder")

        # Zero QEMU fallback
        amd_only = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[self.builders[0]],
        )
        with self.assertRaisesRegex(LumaError, "no ready native BuildKit endpoint available for linux/arm64"):
            amd_only.select_native_builder("linux/arm64")

    def test_pipeline_run_grace_period_and_timeouts(self) -> None:
        run = self.controller.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            branch="main",
            platform="linux/arm64",
        )
        spec = run["pipelineRunManifest"]["spec"]
        self.assertEqual(
            spec.get("podTemplate"),
            {"terminationGracePeriodSeconds": 3},
            "spec.podTemplate must enforce terminationGracePeriodSeconds: 3",
        )
        self.assertEqual(
            spec.get("timeouts"),
            {"pipeline": "45m", "tasks": "30m", "finally": "3m"},
            "spec.timeouts must specify pipeline 45m, tasks 30m, and finally 3m",
        )

    def test_concurrency_slot_lifecycle_accounting(self) -> None:
        mb = next(b for b in self.controller.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 0)
        self.assertEqual(mb.available_slots, 4)

        # Submit build
        run = self.controller.submit_build(
            project="home-app",
            repo_url="https://gcode.gaojiua.com/Signal/home-app.git",
            revision="1111222233334444555566667777888899990000",
            target_node="macbook-pro",
        )
        mb = next(b for b in self.controller.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 1)
        self.assertEqual(mb.available_slots, 3)

        # Cancel build releases slot
        self.controller.cancel_build(run["buildRunId"])
        mb = next(b for b in self.controller.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 0)
        self.assertEqual(mb.available_slots, 4)

        # Submit another build and complete it via complete_build
        run2 = self.controller.submit_build(
            project="home-app",
            repo_url="https://gcode.gaojiua.com/Signal/home-app.git",
            revision="2222333344445555666677778888999900001111",
            target_node="macbook-pro",
        )
        mb = next(b for b in self.controller.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 1)

        self.controller.complete_build(run2["buildRunId"], status="succeeded")
        mb = next(b for b in self.controller.list_builders() if b.name == "macbook-pro")
        self.assertEqual(mb.active_builds, 0)
        self.assertEqual(mb.available_slots, 4)

    def test_terminal_state_mutation_guards(self) -> None:
        run = self.controller.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            platform="linux/arm64",
        )
        run_id = run["buildRunId"]

        # Cancel build
        self.controller.cancel_build(run_id)

        # Canceling already canceled build raises LumaError
        with self.assertRaises(LumaError) as cm:
            self.controller.cancel_build(run_id)
        self.assertIn("already in terminal state 'canceled'", str(cm.exception))

        # Completing already canceled build raises LumaError
        with self.assertRaises(LumaError) as cm:
            self.controller.complete_build(run_id, status="succeeded")
        self.assertIn("already in terminal state 'canceled'", str(cm.exception))

        # Succeeded build cannot be canceled or re-completed
        run2 = self.controller.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="b2c3d4e5f60718293a4b5c6d7e8f901234567890",
            platform="linux/arm64",
        )
        self.controller.complete_build(run2["buildRunId"], status="succeeded")
        with self.assertRaises(LumaError) as cm:
            self.controller.cancel_build(run2["buildRunId"])
        self.assertIn("already in terminal state 'succeeded'", str(cm.exception))

        # Failed build cannot be canceled
        run3 = self.controller.submit_build(
            project="serverx",
            repo_url="https://gcode.gaojiua.com/Signal/serverx.git",
            revision="c3d4e5f60718293a4b5c6d7e8f90123456789012",
            platform="linux/arm64",
        )
        self.controller.complete_build(run3["buildRunId"], status="failed")
        with self.assertRaises(LumaError) as cm:
            self.controller.cancel_build(run3["buildRunId"])
        self.assertIn("already in terminal state 'failed'", str(cm.exception))

    def test_tekton_pipeline_yaml_signal_trapping_and_finally(self) -> None:
        tekton_yaml_path = Path("deploy/paas-stack/tekton/luma-build-pipeline.yaml")
        self.assertTrue(tekton_yaml_path.exists())
        with open(tekton_yaml_path, "r", encoding="utf-8") as f:
            docs = list(yaml.safe_load_all(f))

        tasks = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Task"}
        pipelines = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Pipeline"}

        # Task: buildkit build-push must trap SIGTERM
        build_task = tasks["luma-buildkit-build-push"]
        build_script = build_task["spec"]["steps"][0]["script"]
        self.assertIn("trap 'kill -TERM", build_script)
        self.assertIn("wait ${BUILDCTL_PID}", build_script)

        # Task: cleanup finally task exists
        self.assertIn("luma-build-cleanup-finally", tasks)
        cleanup_script = tasks["luma-build-cleanup-finally"]["spec"]["steps"][0]["script"]
        self.assertIn("/tmp/*.sock", cleanup_script)
        self.assertIn("/run/buildkit/*.sock", cleanup_script)
        self.assertIn("primary", cleanup_script)
        self.assertIn("contexts", cleanup_script)

        # Pipeline: references cleanup task in finally
        pipeline = pipelines["luma-paas-build-pipeline"]
        finally_tasks = pipeline["spec"].get("finally", [])
        self.assertTrue(any(t["name"] == "build-cleanup-finally" for t in finally_tasks))

    def test_boundary_and_corner_cases(self) -> None:
        with self.assertRaisesRegex(LumaError, "build run not found"):
            self.controller.cancel_build("non-existent-build-id")

        with self.assertRaisesRegex(LumaError, "build run not found"):
            self.controller.complete_build("non-existent-build-id")

        run = self.controller.submit_build(
            project="test",
            repo_url="https://example.com/test.git",
            revision="12345",
        )
        with self.assertRaisesRegex(LumaError, "invalid completion status"):
            self.controller.complete_build(run["buildRunId"], status="invalid")


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
        status = expected[0] if (isinstance(expected, (tuple, list, set)) and len(expected) > 0) else 200
        return RegistryResponse(status=status, headers={}, body=b"")


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

        # Deploy v2 in prod with changed secret so prod has historical release to roll back to
        art_v2 = BuildArtifact(
            artifact_id="art-sec-v2",
            project="sec-test",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: sec-test\nregion: cn\nport: 8080\n",
            images={"sec-test": ServiceImageArtifact("sec-test", "100.66.177.70:5000/sec", "sha256:" + "4" * 64, "v2")},
            created_at=1700000100,
        )
        self.catalog.record_artifact(art_v2)
        rel_prod_v2, _ = self.flux.deploy_artifact(
            art_v2.artifact_id,
            environment="prod",
            secrets={"API_KEY": "v2-secret", "EXTRA": "val"},
        )

        # Attempting to roll back to currently active release must raise LumaError
        with self.assertRaises(LumaError) as ctx_active:
            self.flux.rollback_release(
                project="sec-test",
                environment="prod",
                target_release_id=rel_prod_v2.release_id,
            )
        self.assertIn("cannot rollback to currently active release", str(ctx_active.exception))

        # Rollback prod without overwriting secrets to empty
        rolled, rolled_workload = self.flux.rollback_release(
            project="sec-test",
            environment="prod",
            target_release_id=rel_prod.release_id,
        )
        self.assertEqual(rolled.secrets["API_KEY"], "prod-secret")
        self.assertNotIn("EXTRA", rolled.secrets)
        self.assertEqual(rolled.mounted_secret_files["KEY_FILE"], "/app/keys/prod.pem")
        self.assertEqual(rolled_workload.secrets[0]["stringData"]["API_KEY"], "prod-secret")
        self.assertNotIn("EXTRA", rolled_workload.secrets[0]["stringData"])

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




def _make_service_artifact(
    project: str,
    *,
    platform: str = "linux/amd64",
    region: str = "cn",
    node: str = "",
    exposure: str = "none",
    domain: str = "",
    port: int = 8080,
    publish_port: int | None = None,
    volumes: list[str] | None = None,
    storage: dict[str, Any] | None = None,
) -> BuildArtifact:
    manifest_data: dict[str, Any] = {
        "name": project,
        "region": region,
        "port": port,
        "exposure": exposure,
    }
    if node:
        manifest_data["node"] = node
    if domain:
        manifest_data["domain"] = domain
    if publish_port is not None:
        manifest_data["publishPort"] = publish_port
    if volumes:
        manifest_data["volumes"] = volumes
    if storage:
        manifest_data["storage"] = storage

    return BuildArtifact(
        artifact_id=f"art-{project}-v1",
        project=project,
        source_repo="https://example.com/repo.git",
        commit_sha="a" * 40,
        branch="main",
        manifest_kind="service",
        manifest_snapshot=yaml.safe_dump(manifest_data),
        images={
            project: ServiceImageArtifact(
                service_name=project,
                repository=f"100.66.177.70:5000/{project}",
                digest="sha256:" + "1" * 64,
                tag="v1",
                platform=platform,
            )
        },
        created_at=1700000000,
    )


def _make_compose_artifact(
    project: str,
    services_compose: dict[str, Any],
    services_luma: dict[str, Any] | None = None,
    images: dict[str, str] | None = None,  # mapping service_name -> platform
    region: str = "cn",
    node: str = "",
    storage: dict[str, Any] | None = None,
) -> BuildArtifact:
    luma_data: dict[str, Any] = {
        "name": project,
        "region": region,
    }
    if node:
        luma_data["node"] = node
    if services_luma:
        luma_data["services"] = services_luma
    if storage:
        luma_data["storage"] = storage

    image_artifacts: dict[str, ServiceImageArtifact] = {}
    platforms = images or {}
    for idx, (svc_name, svc_body) in enumerate(services_compose.items(), start=1):
        plt = platforms.get(svc_name, "linux/amd64")
        image_artifacts[svc_name] = ServiceImageArtifact(
            service_name=svc_name,
            repository=f"100.66.177.70:5000/{project}-{svc_name}",
            digest="sha256:" + f"{idx % 10}" * 64,
            tag="v1",
            platform=plt,
        )

    return BuildArtifact(
        artifact_id=f"art-{project}-compose",
        project=project,
        source_repo="https://example.com/compose-repo.git",
        commit_sha="b" * 40,
        branch="main",
        manifest_kind="compose",
        manifest_snapshot=yaml.safe_dump(luma_data),
        compose_snapshot=yaml.safe_dump({"services": services_compose}),
        images=image_artifacts,
        created_at=1700000000,
    )


class RuntimeArchitectureAffinityAndComposeIntegrityTests(unittest.TestCase):
    """Verifies nodeSelector kubernetes.io/arch injection, sidecar architecture
    parity under network_mode: service:*, compose deployment splitting, volume mounting,
    and all 6 exposure modes."""

    def setUp(self) -> None:
        self.state: dict[str, Any] = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)

    # --------------------------------------------------------------------------
    # Feature 5: Runtime Architecture Affinity
    # --------------------------------------------------------------------------
    def test_service_nodeselector_kubernetes_arch_arm64_and_amd64(self) -> None:
        """TC-F5-01 & TC-F5-02: Verifies kubernetes.io/arch is injected matching artifact platform."""
        # ARM64 service
        arm_art = _make_service_artifact("arm-service", platform="linux/arm64", region="cn")
        compiled_arm = compile_service_release(artifact=arm_art)
        arm_pod_spec = compiled_arm.deployments[0]["spec"]["template"]["spec"]
        self.assertIn("nodeSelector", arm_pod_spec)
        self.assertEqual(arm_pod_spec["nodeSelector"].get("kubernetes.io/arch"), "arm64")
        self.assertEqual(arm_pod_spec["nodeSelector"].get("topology.kubernetes.io/region"), "cn")

        # AMD64 service
        amd_art = _make_service_artifact("amd-service", platform="linux/amd64", region="cn")
        compiled_amd = compile_service_release(artifact=amd_art)
        amd_pod_spec = compiled_amd.deployments[0]["spec"]["template"]["spec"]
        self.assertIn("nodeSelector", amd_pod_spec)
        self.assertEqual(amd_pod_spec["nodeSelector"].get("kubernetes.io/arch"), "amd64")

        # Platform normalization aliases
        arm_alias = _make_service_artifact("arm-alias", platform="linux/arm64/v8", region="cn")
        self.assertEqual(
            compile_service_release(artifact=arm_alias).deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

        amd_alias = _make_service_artifact("amd-alias", platform="linux/x86_64", region="cn")
        self.assertEqual(
            compile_service_release(artifact=amd_alias).deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "amd64",
        )

    def test_compose_nodeselector_kubernetes_arch_per_independent_service(self) -> None:
        """TC-F5-01/02 (Compose): Verifies each independent service's Deployment receives matching arch."""
        art = _make_compose_artifact(
            "multi-arch-compose",
            services_compose={
                "arm-worker": {"image": "100.66.177.70:5000/multi-arch-compose-arm-worker:v1"},
                "amd-worker": {"image": "100.66.177.70:5000/multi-arch-compose-amd-worker:v1"},
            },
            services_luma={
                "arm-worker": {"replicas": 1},
                "amd-worker": {"replicas": 1},
            },
            images={
                "arm-worker": "linux/arm64",
                "amd-worker": "linux/amd64",
            },
            region="cn",
        )
        compiled = compile_compose_release(artifact=art)
        self.assertEqual(len(compiled.deployments), 2)

        dep_arm = next(d for d in compiled.deployments if d["metadata"]["name"] == "multi-arch-compose-arm-worker")
        self.assertEqual(
            dep_arm["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

        dep_amd = next(d for d in compiled.deployments if d["metadata"]["name"] == "multi-arch-compose-amd-worker")
        self.assertEqual(
            dep_amd["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "amd64",
        )

    def test_target_node_architecture_conflict_guard(self) -> None:
        """TC-F5-03: Compiling image conflicting with target node architecture raises LumaError."""
        # AMD64 image pinned to known ARM64 node (macbook-pro)
        conflict_art = _make_service_artifact("conflict-node", platform="linux/amd64", node="macbook-pro", region="home")
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(artifact=conflict_art, node_architectures={"macbook-pro": "arm64"})
        self.assertRegex(str(ctx.exception).lower(), r"architecture|mismatch|conflict")

        # Explicit node_architectures mapping conflict
        conflict_art2 = _make_service_artifact("conflict-node2", platform="linux/arm64", node="amd-worker-01", region="cn")
        with self.assertRaises(LumaError) as ctx2:
            compile_service_release(artifact=conflict_art2, node_architectures={"amd-worker-01": "amd64"})
        self.assertRegex(str(ctx2.exception).lower(), r"architecture|mismatch|conflict")

        # Compose workload node conflict
        conflict_compose = _make_compose_artifact(
            "conflict-compose",
            services_compose={"web": {"image": "100.66.177.70:5000/conflict-compose-web:v1"}},
            services_luma={"web": {"node": "macbook-pro"}},
            images={"web": "linux/amd64"},
            region="home",
        )
        with self.assertRaises(LumaError) as ctx3:
            compile_compose_release(artifact=conflict_compose, node_architectures={"macbook-pro": "arm64"})
        self.assertRegex(str(ctx3.exception).lower(), r"architecture|mismatch|conflict")

    def test_target_region_architecture_conflict_guard(self) -> None:
        """TC-F5-04: Compiling AMD64 image targeting ARM64-only home region raises LumaError."""
        bad_art = _make_service_artifact("home-amd-app", platform="linux/amd64", region="home")
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(artifact=bad_art)
        self.assertRegex(str(ctx.exception).lower(), r"architecture|mismatch|arm64|home")

        # Valid ARM64 in home region passes
        good_art = _make_service_artifact("home-arm-app", platform="linux/arm64", region="home")
        compiled = compile_service_release(artifact=good_art)
        selector = compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"]
        self.assertEqual(selector.get("kubernetes.io/arch"), "arm64")
        self.assertEqual(selector.get("topology.kubernetes.io/region"), "home")

    def test_compose_sidecar_architecture_mismatch_rejection(self) -> None:
        """TC-F5-05: Sidecar with mismatched architecture under network_mode: service:* raises LumaError."""
        # web is amd64, sidecar is arm64
        art = _make_compose_artifact(
            "mismatch-stack",
            services_compose={
                "web": {"image": "100.66.177.70:5000/web:v1"},
                "sidecar": {
                    "image": "100.66.177.70:5000/sidecar:v1",
                    "network_mode": "service:web",
                },
            },
            images={"web": "linux/amd64", "sidecar": "linux/arm64"},
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(artifact=art)
        self.assertRegex(str(ctx.exception).lower(), r"cannot co-locate mixed-architecture|architecture")

        # Inverse: web is arm64, sidecar is amd64
        art_inv = _make_compose_artifact(
            "mismatch-stack-inv",
            services_compose={
                "web": {"image": "100.66.177.70:5000/web:v1"},
                "sidecar": {
                    "image": "100.66.177.70:5000/sidecar:v1",
                    "network_mode": "service:web",
                },
            },
            images={"web": "linux/arm64", "sidecar": "linux/amd64"},
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx_inv:
            compile_compose_release(artifact=art_inv)
        self.assertRegex(str(ctx_inv.exception).lower(), r"cannot co-locate mixed-architecture|architecture")

        # Matching architectures pass
        art_good = _make_compose_artifact(
            "match-stack",
            services_compose={
                "web": {"image": "100.66.177.70:5000/web:v1"},
                "sidecar": {
                    "image": "100.66.177.70:5000/sidecar:v1",
                    "network_mode": "service:web",
                },
            },
            images={"web": "linux/arm64", "sidecar": "linux/arm64"},
            region="cn",
        )
        compiled_good = compile_compose_release(artifact=art_good)
        self.assertEqual(len(compiled_good.deployments), 1)

    # --------------------------------------------------------------------------
    # Feature 6: Compose Splitting, PVC Mounting & Custom Command
    # --------------------------------------------------------------------------
    def test_compose_independent_deployment_splitting(self) -> None:
        """Compose independent Deployment splitting for distinct services."""
        art = _make_compose_artifact(
            "serverx",
            services_compose={
                "web": {"image": "100.66.177.70:5000/serverx-web:v1"},
                "browser-worker": {"image": "100.66.177.70:5000/serverx-browser:v1"},
                "video-worker": {"image": "100.66.177.70:5000/serverx-video:v1"},
                "sidecar-agent": {
                    "image": "100.66.177.70:5000/serverx-agent:v1",
                    "network_mode": "service:web",
                },
            },
            services_luma={
                "web": {"port": 8080, "replicas": 1},
                "browser-worker": {"replicas": 1},
                "video-worker": {"replicas": 2},
            },
            images={
                "web": "linux/amd64",
                "browser-worker": "linux/amd64",
                "video-worker": "linux/amd64",
                "sidecar-agent": "linux/amd64",
            },
            region="cn",
        )
        compiled = compile_compose_release(
            artifact=art,
            replicas_override={"web": 1, "video-worker": 0},
        )
        # Exactly 3 deployments: web, browser-worker, video-worker
        self.assertEqual(len(compiled.deployments), 3)
        dep_names = {d["metadata"]["name"] for d in compiled.deployments}
        self.assertEqual(dep_names, {"serverx-web", "serverx-browser-worker", "serverx-video-worker"})

        # Replica override respected (e.g. video-worker paused to 0 for migration shadow)
        self.assertEqual(compiled.replicas_by_service["web"], 1)
        self.assertEqual(compiled.replicas_by_service["video-worker"], 0)
        self.assertEqual(compiled.replicas_by_service["browser-worker"], 1)

        v_dep = next(d for d in compiled.deployments if d["metadata"]["name"] == "serverx-video-worker")
        self.assertEqual(v_dep["spec"]["replicas"], 0)

    def test_compose_sidecar_container_colocation_in_primary_pod(self) -> None:
        """Compose sidecar container co-location inside primary service's Pod."""
        art = _make_compose_artifact(
            "co-locate-app",
            services_compose={
                "web": {"image": "100.66.177.70:5000/web:v1"},
                "telemetry": {
                    "image": "100.66.177.70:5000/telemetry:v1",
                    "network_mode": "service:web",
                },
            },
            images={"web": "linux/arm64", "telemetry": "linux/arm64"},
            region="cn",
        )
        compiled = compile_compose_release(artifact=art)
        self.assertEqual(len(compiled.deployments), 1)
        self.assertEqual(compiled.sidecar_pairs, {"telemetry": "web"})

        web_dep = compiled.deployments[0]
        containers = web_dep["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 2)
        container_names = [c["name"] for c in containers]
        self.assertEqual(container_names, ["web", "telemetry"])

    def test_compose_pvc_volume_mounting_and_sidecar_command(self) -> None:
        """TC-F6-01 & TC-F6-02: Verifies PVC volume mounting and preservation of sidecar command."""
        art = _make_compose_artifact(
            "pvc-test",
            services_compose={
                "web": {
                    "image": "100.66.177.70:5000/web:v1",
                    "volumes": ["app-data:/var/lib/app"],
                },
                "agent": {
                    "image": "100.66.177.70:5000/agent:v1",
                    "network_mode": "service:web",
                    "command": ["/bin/agent", "--foreground", "--verbose"],
                },
            },
            services_luma={
                "web": {
                    "port": 8080,
                    "volumes": ["app-data:/var/lib/app"],
                }
            },
            images={"web": "linux/amd64", "agent": "linux/amd64"},
            storage={
                "app-data": {
                    "storageClass": "local-path",
                    "size": "20Gi",
                }
            },
            region="cn",
        )
        compiled = compile_compose_release(artifact=art)
        self.assertEqual(len(compiled.pvcs), 1)
        pvc = compiled.pvcs[0]
        self.assertEqual(pvc["metadata"]["name"], "pvc-test-app-data")
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "20Gi")
        self.assertEqual(pvc["spec"]["storageClassName"], "local-path")

        web_dep = next(d for d in compiled.deployments if d["metadata"]["name"] == "pvc-test-web")
        pod_spec = web_dep["spec"]["template"]["spec"]

        # Pod volumes must reference PVC
        vol_claims = [v.get("persistentVolumeClaim", {}).get("claimName") for v in pod_spec.get("volumes", [])]
        self.assertIn("pvc-test-app-data", vol_claims)

        # Primary container mounts PVC
        web_c = next(c for c in pod_spec["containers"] if c["name"] == "web")
        mounts = {m["mountPath"]: m["name"] for m in web_c.get("volumeMounts", [])}
        self.assertIn("/var/lib/app", mounts)
        self.assertEqual(mounts["/var/lib/app"], "app-data")

        # TC-F6-02: Sidecar container retains exact command
        agent_c = next(c for c in pod_spec["containers"] if c["name"] == "agent")
        cmd = agent_c.get("command") or agent_c.get("args")
        self.assertEqual(cmd, ["/bin/agent", "--foreground", "--verbose"])

    # --------------------------------------------------------------------------
    # All 6 Exposure Modes & Rejection
    # --------------------------------------------------------------------------
    def test_exposure_mode_none_suppression(self) -> None:
        """TC-F6-03: exposure: none suppresses all Ingress and Route resources, even with domain."""
        # 1. Bare exposure: none
        art1 = _make_service_artifact("internal-backend", exposure="none", port=5000, region="cn")
        compiled1 = compile_service_release(artifact=art1)
        self.assertEqual(len(compiled1.ingresses), 0)

        # 2. exposure: none with explicit domain specified (must NOT trigger fallback edge ingress)
        art2 = _make_service_artifact("internal-api", exposure="none", domain="internal.example.com", port=5000, region="cn")
        compiled2 = compile_service_release(artifact=art2)
        self.assertEqual(len(compiled2.ingresses), 0)

    def test_exposure_mode_tcp_relay_traefik_ingressroute(self) -> None:
        """TC-F6-04: exposure: tcp-relay compiles to IngressRouteTCP with tcp entrypoint."""
        art = _make_service_artifact(
            "redis-tcp",
            exposure="tcp-relay",
            port=6379,
            publish_port=16379,
            region="cn",
        )
        compiled = compile_service_release(artifact=art)
        self.assertEqual(len(compiled.ingresses), 1)
        ing = compiled.ingresses[0]
        self.assertEqual(ing.get("kind"), "IngressRouteTCP")
        self.assertEqual(ing["spec"]["entryPoints"], ["tcp-16379"])
        self.assertEqual(ing["metadata"]["annotations"].get("luma.io/publish-port"), "16379")
        self.assertEqual(ing["spec"]["routes"][0]["services"][0]["port"], 6379)

    def test_exposure_mode_tailscale_relay_mesh_annotations(self) -> None:
        """TC-F6-05: exposure: tailscale-relay attaches tailscale mesh annotations."""
        art = _make_service_artifact(
            "home-nas",
            exposure="tailscale-relay",
            domain="home-nas.example.com",
            port=9000,
            region="home",
            platform="linux/arm64",
        )
        compiled = compile_service_release(artifact=art)
        self.assertEqual(len(compiled.ingresses), 1)
        ing = compiled.ingresses[0]
        annotations = ing["metadata"]["annotations"]
        self.assertEqual(annotations.get("luma.io/exposure"), "tailscale-relay")
        self.assertEqual(annotations.get("luma.io/relay-transport"), "tailscale-mesh")
        self.assertEqual(annotations.get("luma.io/target-region"), "home")
        self.assertEqual(annotations.get("luma.io/edge-gateway"), "tailscale-relay")

    def test_exposure_modes_cn_edge_external_edge_cloudflare_tunnel(self) -> None:
        """Verifies cn-edge, external-edge, and cloudflare-tunnel exposure modes."""
        # 1. cn-edge
        cn_art = _make_service_artifact("cn-app", exposure="cn-edge", domain="cn.example.com", region="cn", port=80)
        compiled_cn = compile_service_release(artifact=cn_art)
        ing_cn = compiled_cn.ingresses[0]
        self.assertEqual(ing_cn["metadata"]["annotations"].get("luma.io/edge-gateway"), "cn-edge")
        self.assertEqual(ing_cn["metadata"]["annotations"].get("traefik.ingress.kubernetes.io/router.tls"), "true")

        # 2. external-edge
        ext_art = _make_service_artifact("global-app", exposure="external-edge", domain="global.example.com", region="global", port=80)
        compiled_ext = compile_service_release(artifact=ext_art)
        ing_ext = compiled_ext.ingresses[0]
        self.assertEqual(ing_ext["metadata"]["annotations"].get("luma.io/edge-gateway"), "global-edge")
        self.assertEqual(ing_ext["metadata"]["annotations"].get("external-dns.alpha.kubernetes.io/hostname"), "global.example.com")

        # 3. cloudflare-tunnel
        cf_art = _make_service_artifact("tunnel-app", exposure="cloudflare-tunnel", domain="tunnel.example.com", region="cn", port=8080)
        compiled_cf = compile_service_release(artifact=cf_art)
        ing_cf = compiled_cf.ingresses[0]
        self.assertEqual(ing_cf["metadata"]["annotations"].get("luma.io/edge-gateway"), "cloudflare-tunnel")
        self.assertEqual(ing_cf["metadata"]["annotations"].get("luma.io/tunnel-hostname"), "tunnel.example.com")
        self.assertIn("luma-prod.svc.cluster.local:8080", ing_cf["metadata"]["annotations"].get("luma.io/tunnel-service", ""))

    def test_invalid_exposure_modes_and_region_mismatches_rejection(self) -> None:
        """TC-B-F6-02: Invalid exposure mode string and region mismatches raise LumaError."""
        # Unknown exposure mode
        invalid_art = _make_service_artifact("bad-exposure", exposure="unknown-mesh", domain="bad.com", region="cn")
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(artifact=invalid_art)
        self.assertRegex(str(ctx.exception).lower(), r"exposure must be one of")

        # cn-edge not allowed in global region
        conflict_art1 = _make_service_artifact("mismatch-region-1", exposure="cn-edge", domain="bad.com", region="global")
        with self.assertRaises(LumaError):
            compile_service_release(artifact=conflict_art1)

        # external-edge not allowed in cn region
        conflict_art2 = _make_service_artifact("mismatch-region-2", exposure="external-edge", domain="bad.com", region="cn")
        with self.assertRaises(LumaError):
            compile_service_release(artifact=conflict_art2)

        # tailscale-relay not allowed in global region
        conflict_art3 = _make_service_artifact("mismatch-region-3", exposure="tailscale-relay", domain="bad.com", region="global")
        with self.assertRaises(LumaError):
            compile_service_release(artifact=conflict_art3)

    # --------------------------------------------------------------------------
    # Boundary, Corner & Adversarial Edge Cases
    # --------------------------------------------------------------------------
    def test_compound_and_cyclic_sidecar_chains(self) -> None:
        """TC-B-F5-01 & TC-B-F5-03: Self-referential loop or compound chain validation."""
        # Self-referential loop (web -> service:web)
        self_ref_art = _make_compose_artifact(
            "self-ref",
            services_compose={"web": {"image": "100.66.177.70:5000/web:v1", "network_mode": "service:web"}},
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx_self:
            compile_compose_release(artifact=self_ref_art)
        self.assertRegex(str(ctx_self.exception).lower(), r"self-referential|loop|cycle")

        # Cyclic sidecars (a -> service:b, b -> service:a)
        cyclic_art = _make_compose_artifact(
            "cyclic-stack",
            services_compose={
                "svc-a": {"image": "100.66.177.70:5000/a:v1", "network_mode": "service:svc-b"},
                "svc-b": {"image": "100.66.177.70:5000/b:v1", "network_mode": "service:svc-a"},
            },
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx_cycle:
            compile_compose_release(artifact=cyclic_art)
        self.assertRegex(str(ctx_cycle.exception).lower(), r"cycle|loop|invalid network_mode")

    def test_missing_parent_service_in_sidecar(self) -> None:
        """TC-B-F5-02: network_mode: service:nonexistent raises LumaError."""
        orphan_art = _make_compose_artifact(
            "orphan-sidecar",
            services_compose={
                "sidecar": {
                    "image": "100.66.177.70:5000/sidecar:v1",
                    "network_mode": "service:nonexistent_primary",
                }
            },
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(artifact=orphan_art)
        self.assertRegex(str(ctx.exception).lower(), r"nonexistent|missing parent|not found")

    def test_node_pin_and_region_topology_conflict(self) -> None:
        """TC-B-F5-04: Node pin matching node arch but cross-region topology mismatch."""
        # macbook-pro is in region 'home', passing region='cn' with node='macbook-pro'
        topo_art = _make_service_artifact("topo-app", platform="linux/arm64", node="macbook-pro", region="cn")
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(
                artifact=topo_art,
                node_architectures={"macbook-pro": "arm64"},
            )
        self.assertRegex(str(ctx.exception).lower(), r"topology|region|mismatch|conflict")

    def test_nodeselector_kubernetes_arch_rfc1123_compliance(self) -> None:
        """TC-B-F5-05: kubernetes.io/arch label value adheres strictly to RFC 1123 DNS label format."""
        rfc1123_re = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        for test_plt in ["linux/arm64", "linux/amd64", "linux/arm64/v8", "linux/x86_64"]:
            art = _make_service_artifact("rfc-app", platform=test_plt, region="cn")
            compiled = compile_service_release(artifact=art)
            arch_val = compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch")
            self.assertIsNotNone(arch_val)
            self.assertRegex(arch_val, rfc1123_re)
            self.assertIn(arch_val, {"arm64", "amd64"})

    def test_compose_duplicate_volume_mount_paths_rejection(self) -> None:
        """TC-B-F6-01: Two volumes in same container mounted to identical container path raises LumaError."""
        dup_art = _make_compose_artifact(
            "dup-mount-app",
            services_compose={
                "web": {
                    "image": "100.66.177.70:5000/web:v1",
                    "volumes": [
                        "data1:/app/data",
                        "data2:/app/data",
                    ],
                }
            },
            services_luma={
                "web": {
                    "volumes": [
                        "data1:/app/data",
                        "data2:/app/data",
                    ]
                }
            },
            storage={
                "data1": {"storageClass": "local-path", "size": "10Gi"},
                "data2": {"storageClass": "local-path", "size": "10Gi"},
            },
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(artifact=dup_art)
        self.assertRegex(str(ctx.exception).lower(), r"duplicate|collision|already mounted")

    def test_tcp_relay_port_collision_and_reserved_ports(self) -> None:
        """TC-B-F6-03: Two services requesting same publishPort or reserved port raises LumaError."""
        # 1. Port collision in compose stack
        collision_art = _make_compose_artifact(
            "tcp-collision-stack",
            services_compose={
                "redis1": {"image": "100.66.177.70:5000/redis:v1"},
                "redis2": {"image": "100.66.177.70:5000/redis:v1"},
            },
            services_luma={
                "redis1": {"exposure": "tcp-relay", "port": 6379, "publishPort": 16379},
                "redis2": {"exposure": "tcp-relay", "port": 6379, "publishPort": 16379},
            },
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(artifact=collision_art)
        self.assertRegex(str(ctx.exception).lower(), r"collision|port 16379 already in use|publishport")

        # 2. Reserved port 80 / 443
        reserved_art = _make_service_artifact("reserved-tcp", exposure="tcp-relay", port=80, publish_port=80, region="cn")
        with self.assertRaises(LumaError) as ctx_res:
            compile_service_release(artifact=reserved_art)
        self.assertRegex(str(ctx_res.exception).lower(), r"reserved")

    def test_volume_size_specification_unit_validation(self) -> None:
        """TC-B-F6-04: Storage volume size missing Kubernetes quantity units raises LumaError."""
        bad_storage_art = _make_service_artifact(
            "unit-test-app",
            volumes=["data:/var/data"],
            storage={"data": {"storageClass": "local-path", "size": "50"}},  # Missing 'Gi' or 'Mi'
            region="cn",
        )
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(artifact=bad_storage_art)
        self.assertRegex(str(ctx.exception).lower(), r"unit|quantity|gi|mi")

        # Valid unit passes cleanly
        good_storage_art = _make_service_artifact(
            "good-unit-app",
            volumes=["data:/var/data"],
            storage={"data": {"storageClass": "local-path", "size": "50Gi"}},
            region="cn",
        )
        compiled = compile_service_release(artifact=good_storage_art)
        self.assertEqual(compiled.pvcs[0]["spec"]["resources"]["requests"]["storage"], "50Gi")

    def test_volume_readonly_flag_and_nested_subpaths(self) -> None:
        """TC-B-F6-05: Volume :ro flag marks volumeMount as readOnly; nested subpaths mount safely."""
        ro_art = _make_compose_artifact(
            "ro-mount-app",
            services_compose={
                "web": {
                    "image": "100.66.177.70:5000/web:v1",
                    "volumes": [
                        "media-ro:/var/media/readonly:ro",
                        "config-nested:/etc/app/config/deep/subpath",
                    ],
                }
            },
            services_luma={
                "web": {
                    "volumes": [
                        "media-ro:/var/media/readonly:ro",
                        "config-nested:/etc/app/config/deep/subpath",
                    ]
                }
            },
            storage={
                "media-ro": {"storageClass": "local-path", "size": "10Gi"},
                "config-nested": {"storageClass": "local-path", "size": "1Gi"},
            },
            region="cn",
        )
        compiled = compile_compose_release(artifact=ro_art)
        web_dep = compiled.deployments[0]
        mounts = web_dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]

        ro_mount = next(m for m in mounts if m["name"] == "media-ro")
        self.assertEqual(ro_mount["mountPath"], "/var/media/readonly")
        self.assertTrue(ro_mount.get("readOnly", False))

        nested_mount = next(m for m in mounts if m["name"] == "config-nested")
        self.assertEqual(nested_mount["mountPath"], "/etc/app/config/deep/subpath")
        self.assertFalse(nested_mount.get("readOnly", False))

    def test_compose_empty_services_boundary(self) -> None:
        """Boundary: compose_snapshot with empty services or missing services raises LumaError."""
        empty_art = BuildArtifact(
            artifact_id="art-empty-compose",
            project="empty-compose",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: empty-compose\nregion: cn\n",
            compose_snapshot="services: {}\n",
            images={
                "web": ServiceImageArtifact(
                    service_name="web",
                    repository="100.66.177.70:5000/empty-compose-web",
                    digest="sha256:" + "c" * 64,
                    tag="v1",
                    platform="linux/amd64",
                )
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(artifact=empty_art)
        self.assertRegex(str(ctx.exception).lower(), r"non-empty services")

    def test_missing_image_platform_fallback_and_inference(self) -> None:
        """Boundary: Image platform missing or empty falls back to topology platform inference."""
        # 1. infer_native_platform with empty platform in home region
        inferred_home = infer_native_platform(platform="", target_region="home")
        self.assertEqual(inferred_home, "linux/arm64")

        # 2. infer_native_platform with empty platform on ARM64 node
        inferred_node = infer_native_platform(platform="", target_node="macbook-pro")
        self.assertEqual(inferred_node, "linux/arm64")

        # 3. Default fallback without hints
        inferred_default = infer_native_platform(platform="", target_region="cn")
        self.assertEqual(inferred_default, "linux/amd64")

        # 4. Service release with inferred ARM64 platform compiles with kubernetes.io/arch == arm64
        art = _make_service_artifact("fallback-app", platform=inferred_home, region="home")
        compiled = compile_service_release(artifact=art)
        self.assertEqual(
            compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

    # --------------------------------------------------------------------------
    # Tier 3: Cross-Feature Pairwise Invariants
    # --------------------------------------------------------------------------
    def test_cross_flux_promotion_preserves_runtime_arch_affinity(self) -> None:
        """TC-X-04: Cross-feature promotion preserves kubernetes.io/arch: arm64 in prod release."""
        arm_art = _make_service_artifact("promo-arm", platform="linux/arm64", region="cn")
        self.catalog.record_artifact(arm_art)

        # Deploy in staging
        rel_stg, _ = self.flux.deploy_artifact(
            artifact_id=arm_art.artifact_id,
            environment="staging",
            secrets={"API_KEY": "staging-key"},
        )
        compiled_stg = compile_service_release(artifact=arm_art, environment="staging")
        self.assertEqual(
            compiled_stg.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

        # Promote to prod
        rel_prod, _ = self.flux.promote_artifact(
            project="promo-arm",
            from_env="staging",
            to_env="prod",
            target_secrets={"API_KEY": "prod-key"},
        )
        compiled_prod = compile_service_release(artifact=arm_art, environment="prod", secrets=rel_prod.secrets)
        self.assertEqual(
            compiled_prod.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )
        self.assertEqual(rel_prod.secrets.get("API_KEY"), "prod-key")

    def test_cross_sidecar_arch_parity_with_tailscale_relay(self) -> None:
        """TC-X-06: Cross-feature compose stack with tailscale-relay and verified arm64 sidecar."""
        art = _make_compose_artifact(
            "mesh-agent-app",
            services_compose={
                "web": {"image": "100.66.177.70:5000/mesh-web:v1"},
                "sidecar": {
                    "image": "100.66.177.70:5000/mesh-sidecar:v1",
                    "network_mode": "service:web",
                },
            },
            services_luma={
                "web": {
                    "port": 8080,
                    "domain": "mesh.home.internal",
                    "exposure": "tailscale-relay",
                }
            },
            images={"web": "linux/arm64", "sidecar": "linux/arm64"},
            region="home",
        )
        compiled = compile_compose_release(artifact=art)
        self.assertEqual(len(compiled.deployments), 1)
        web_dep = compiled.deployments[0]
        # Ingress has tailscale relay annotations
        self.assertEqual(len(compiled.ingresses), 1)
        self.assertEqual(compiled.ingresses[0]["metadata"]["annotations"].get("luma.io/exposure"), "tailscale-relay")
        self.assertEqual(compiled.ingresses[0]["metadata"]["annotations"].get("luma.io/relay-transport"), "tailscale-mesh")
        # Both containers co-located in primary pod
        containers = web_dep["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 2)
        # NodeSelector enforces arm64
        self.assertEqual(web_dep["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"), "arm64")



# ==============================================================================
# Suite 4: Migration Reliability & openSUSE MicroOS / Darwin Self-Healing Tests (F7, F8)
# ==============================================================================
class MigrationReliabilityAndNodeSelfHealingTests(unittest.TestCase):
    """Suite 4: Migration Reliability & openSUSE MicroOS / Darwin Node Self-Healing Tests (F7, F8).
    Verifies:
    - openSUSE MicroOS self-healing evaluation (transactional rootfs, btrfs scrub/trim, SELinux restorecon)
    - macOS Darwin <local> NO_PROXY evaluation and Tailscale reachability
    - DaemonSet YAML manifest verification (deploy/paas-stack/k8s/node-self-heal-daemonset.yaml)
    - 4-step migration workflow lifecycle (plan -> shadow -> cutover -> abort -> finalize)
    - Batch 2 queue-worker replica 0 shadow & cutover scale-up/drain
    - Batch 3 stateful volume node pin & single-instance bot isolation (shadow replica 0, stop Nomad before scale 1)
    - Pre-cutover safety gates (gateway_probe_ok, secret parity, image arch matching, image digest drift)
    - Same-node Traefik port conflict protection and memory headroom checks
    - Cross-feature invariants (MicroOS + Batch 3 stateful, Darwin + Native ARM64 build)
    """

    def setUp(self) -> None:
        self.state: dict[str, Any] = {}
        self.catalog = ArtifactCatalog(self.state)
        self.flux = FluxReconciler(self.catalog)
        self.migrator = MigrationEngine(self.catalog, self.flux)

    # --------------------------------------------------------------------------
    # Feature 7: openSUSE MicroOS & Darwin Node Self-Healing (Tier 1 & Tier 2)
    # --------------------------------------------------------------------------
    def test_opensuse_microos_self_heal_transactional_rootfs(self) -> None:
        """TC-F7-01: Verifies self-healing handles opensuse-microos read-only rootfs and btrfs maintenance."""
        report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": True,
                "diskUsedPercent": 84.0,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        actions = " ".join(report.repaired_actions)
        self.assertIn("btrfs", actions.lower())
        self.assertIn("registries.yaml", actions)
        self.assertTrue(report.registries_yaml_converged)
        self.assertLess(report.disk_used_percent, 75.0)

        # Mapping access verification
        self.assertIn("actions", report)
        self.assertIn("repairedActions", report)
        self.assertEqual(list(report["actions"]), list(report.repaired_actions))
        self.assertTrue(dict(report)["healthy"])

    def test_opensuse_microos_btrfs_scrub_and_trim_maintenance(self) -> None:
        """TC-F7-02: MicroOS btrfs scrub and thin-pool fstrim maintenance when disk usage >= 75%."""
        node = {
            "name": "microos-worker-arm64",
            "osName": "opensuse-microos",
            "diskUsedPercent": 82.0,
            "lastFstrimAt": 0,
            "registriesYamlConverged": True,
            "noProxy": "localhost,127.0.0.1,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": True,
        }
        unfixed = evaluate_node_self_heal(node, auto_fix=False)
        self.assertEqual(unfixed.disk_used_percent, 82.0)

        report = evaluate_node_self_heal(node, auto_fix=True)
        self.assertTrue(report.healthy)
        actions_str = " ".join(report.repaired_actions).lower()
        self.assertTrue("btrfs" in actions_str or "fstrim" in actions_str)
        self.assertLess(report.disk_used_percent, 75.0)
        self.assertGreater(report.last_fstrim_at, 0)

    def test_opensuse_microos_selinux_restorecon(self) -> None:
        """TC-F7-03 & TC-B-F7-04: MicroOS SELinux restorecon on /etc/rancher/k3s/registries.yaml."""
        report = evaluate_node_self_heal(
            {
                "name": "microos-master",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "selinuxMode": "enforcing",
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.registries_yaml_converged)
        actions = " ".join(report.repaired_actions).lower()
        self.assertIn("restorecon", actions)
        self.assertIn("registries.yaml", actions)

    def test_opensuse_microos_corrupted_registries_yaml_recovery(self) -> None:
        """TC-B-F7-03: Malformed YAML in registriesYamlContent detected and auto-recovered on MicroOS."""
        node_syntax_error = {
            "name": "microos-worker-arm64",
            "osName": "opensuse-microos",
            "registriesYamlConverged": True,  # corrupted content must override initial True flag
            "registriesYamlContent": ":::malformed yaml::: {bad syntax [unbalanced",
            "noProxy": "localhost,127.0.0.1",
            "zotDirectReachable": True,
            "diskUsedPercent": 40.0,
        }
        unfixed = evaluate_node_self_heal(node_syntax_error, auto_fix=False)
        self.assertFalse(unfixed.healthy, "Node with malformed registries.yaml must evaluate as unhealthy")
        self.assertFalse(
            unfixed.registries_yaml_converged,
            "registries_yaml_converged must be False when registries.yaml has syntax errors",
        )
        self.assertEqual(len(unfixed.repaired_actions), 0)

        fixed = evaluate_node_self_heal(node_syntax_error, auto_fix=True)
        self.assertTrue(fixed.healthy, "Auto-fix must restore health after corrupted registries.yaml")
        self.assertTrue(
            fixed.registries_yaml_converged,
            "Auto-fix must converge registries_yaml_converged to True",
        )
        actions = " ".join(fixed.repaired_actions).lower()
        self.assertIn("syntax failure", actions)
        self.assertIn("restorecon", actions)
        self.assertIn("registries.yaml", actions)

        # Semantic corruption test: Valid YAML syntax but missing mirrors/configs keys
        node_semantic_error = {
            "name": "microos-worker-arm64",
            "osName": "opensuse-microos",
            "registriesYamlConverged": True,
            "registriesYamlContent": "unrelated_root:\n  nested_field: 12345\n",
            "noProxy": "localhost,127.0.0.1",
            "zotDirectReachable": True,
            "diskUsedPercent": 40.0,
        }
        unfixed_sem = evaluate_node_self_heal(node_semantic_error, auto_fix=False)
        self.assertFalse(unfixed_sem.healthy)
        self.assertFalse(unfixed_sem.registries_yaml_converged)

        fixed_sem = evaluate_node_self_heal(node_semantic_error, auto_fix=True)
        self.assertTrue(fixed_sem.healthy)
        self.assertTrue(fixed_sem.registries_yaml_converged)
        actions_sem = " ".join(fixed_sem.repaired_actions).lower()
        self.assertIn("syntax failure", actions_sem)
        self.assertIn("restorecon", actions_sem)

    def test_node_self_heal_report_mapping_access_and_membership(self) -> None:
        """TC-B-F7-06: Verifies NodeSelfHealReport mapping protocol (getitem, in, dict, get) for actions and keys."""
        from collections.abc import Mapping

        report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": False,
                "diskUsedPercent": 84.0,
            },
            auto_fix=True,
        )
        # 1. Membership test with 'in' operator (must not trigger sequence index KeyError: 0)
        self.assertIn("actions", report)
        self.assertIn("repairedActions", report)
        self.assertIn("repaired_actions", report)
        self.assertIn("healthy", report)
        self.assertIn("nodeName", report)
        self.assertIn("node_name", report)
        self.assertIn("disk_used_percent", report)
        self.assertNotIn("unknown_key", report)
        self.assertNotIn(0, report)

        # 2. Key mapping access via report[...]
        actions = report["actions"]
        self.assertIsInstance(actions, (list, tuple))
        self.assertTrue(len(actions) > 0)
        self.assertEqual(list(actions), list(report["repairedActions"]))
        self.assertEqual(list(actions), list(report["repaired_actions"]))

        # 3. get() method support
        self.assertEqual(list(report.get("actions", [])), list(actions))
        self.assertEqual(report.get("nonexistent", "fallback"), "fallback")

        # 4. KeyError on missing key
        with self.assertRaises(KeyError):
            _ = report["nonexistent_property"]

        # 5. dict(report) conversion support
        report_dict = dict(report)
        self.assertIsInstance(report_dict, dict)
        self.assertIn("repairedActions", report_dict)
        self.assertIn("actions", report_dict)
        self.assertIn("nodeName", report_dict)
        self.assertEqual(report_dict["nodeName"], "microos-node-1")
        self.assertEqual(report_dict["repairedActions"], list(report.repaired_actions))
        self.assertEqual(report_dict["actions"], list(report.repaired_actions))
        self.assertTrue(report_dict["healthy"])

        # 6. Mapping ABC compliance
        self.assertIsInstance(report, Mapping)
        self.assertEqual(len(report), len(report_dict))
        self.assertEqual(list(report.keys()), list(report_dict.keys()))
        self.assertEqual(list(report.values()), list(report_dict.values()))
        self.assertEqual(list(report.items()), list(report_dict.items()))
        unpacked = {**report}
        self.assertEqual(unpacked["actions"], list(report.repaired_actions))

    def test_darwin_self_heal_no_proxy_local(self) -> None:
        """TC-F7-04: Verifies Darwin node self-heal ensures <local> and localhost in NO_PROXY."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "registriesYamlConverged": True,
                "noProxy": "localhost,127.0.0.1",
                "zotDirectReachable": True,
                "diskUsedPercent": 50.0,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.no_proxy_aligned)
        self.assertTrue(any("<local>" in act for act in report.repaired_actions))

    def test_darwin_tailscale_direct_reachability(self) -> None:
        """TC-F7-05: Verifies health check identifies Tailscale route and auto-fixes it."""
        node = {
            "name": "macbook-pro",
            "osName": "darwin",
            "registriesYamlConverged": True,
            "noProxy": "localhost,127.0.0.1,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": False,
            "diskUsedPercent": 50.0,
        }
        unfixed = evaluate_node_self_heal(node, auto_fix=False)
        self.assertFalse(unfixed.healthy)
        self.assertFalse(unfixed.zot_direct_reachable)

        fixed = evaluate_node_self_heal(node, auto_fix=True)
        self.assertTrue(fixed.healthy)
        self.assertTrue(fixed.zot_direct_reachable)
        self.assertTrue(any("tailscale" in act.lower() for act in fixed.repaired_actions))

    def test_no_proxy_multiple_separators_and_duplicate_entries(self) -> None:
        """TC-B-F7-05: Multiple separators, whitespace, duplicate entries cleaned canonically."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "noProxy": "localhost, 127.0.0.1;  gcode.gaojiua.com , 100.66.177.70:5000, localhost",
                "zotDirectReachable": True,
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.no_proxy_aligned)

    def test_darwin_no_proxy_ip_substring_does_not_satisfy_exact_match(self) -> None:
        """TC-B-F7-07: Verifies un-tokenized substring matching is rejected: 127.0.0.100 does not satisfy 127.0.0.1."""
        node = {
            "name": "macbook-m1",
            "osName": "darwin",
            "registriesYamlConverged": True,
            "noProxy": "127.0.0.100,localhost,<local>,gcode.gaojiua.com,100.66.177.70:5000",
            "zotDirectReachable": True,
            "diskUsedPercent": 50.0,
        }

        unfixed = evaluate_node_self_heal(node, auto_fix=False)
        self.assertFalse(
            unfixed.no_proxy_aligned,
            "127.0.0.100 must NOT satisfy required 127.0.0.1 via substring matching",
        )
        self.assertFalse(unfixed.healthy)

        fixed = evaluate_node_self_heal(node, auto_fix=True)
        self.assertTrue(fixed.no_proxy_aligned)
        self.assertTrue(fixed.healthy)

        tokens = [t.strip() for t in fixed.no_proxy_canonical.split(",") if t.strip()]
        self.assertIn("127.0.0.1", tokens, "127.0.0.1 must be appended when missing")
        self.assertIn("127.0.0.100", tokens, "Original token 127.0.0.100 must be preserved")
        self.assertEqual(tokens[0], "127.0.0.100", "Original token ordering must be preserved")
        self.assertIn("<local>", tokens)
        self.assertIn("localhost", tokens)

        # Also verify case-insensitive host matching without duplicate tokens
        node_case = {
            "name": "macbook-pro",
            "osName": "darwin",
            "noProxy": "LOCALHOST,127.0.0.1,GCODE.GAOJIUA.COM,100.66.177.70:5000,<LOCAL>",
            "zotDirectReachable": True,
        }
        case_report = evaluate_node_self_heal(node_case, auto_fix=True)
        self.assertTrue(case_report.no_proxy_aligned)
        self.assertEqual(len(case_report.repaired_actions), 0, "No repair should be needed when tokens match case-insensitively")
        canonical_split = case_report.no_proxy_canonical.split(",")
        self.assertEqual(len(canonical_split), 5)

    def test_unknown_os_fallback_and_boundary_disk(self) -> None:
        """TC-B-F7-01 & TC-B-F7-02: Unknown OS fallback and disk boundary at 75.0%."""
        report_unknown = evaluate_node_self_heal(
            {"name": "solaris-node", "osName": "solaris", "diskUsedPercent": 75.0},
            auto_fix=True,
        )
        self.assertTrue(report_unknown.healthy)

        crit = evaluate_node_self_heal(
            {"name": "linux-full", "osName": "linux", "diskUsedPercent": 92.0},
            auto_fix=False,
        )
        self.assertFalse(crit.healthy)

    # --------------------------------------------------------------------------
    # Feature 7: DaemonSet YAML Manifest Verification
    # --------------------------------------------------------------------------
    def test_node_self_heal_daemonset_yaml_manifest_verification(self) -> None:
        """Manifest verification of deploy/paas-stack/k8s/node-self-heal-daemonset.yaml."""
        manifest_path = Path("deploy/paas-stack/k8s/node-self-heal-daemonset.yaml")
        self.assertTrue(manifest_path.exists(), f"Manifest file missing: {manifest_path}")

        manifest_docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
        ds = next(d for d in manifest_docs if d and d.get("kind") == "DaemonSet")

        self.assertEqual(ds["apiVersion"], "apps/v1")
        self.assertEqual(ds["metadata"]["name"], "luma-node-self-heal")
        self.assertEqual(ds["metadata"]["namespace"], "luma-system")

        pod_spec = ds["spec"]["template"]["spec"]
        self.assertEqual(
            pod_spec.get("nodeSelector", {}).get("kubernetes.io/os"),
            "linux",
            "DaemonSet must target linux nodes via nodeSelector kubernetes.io/os: linux",
        )
        self.assertTrue(pod_spec.get("hostPID"), "DaemonSet must run with hostPID: true")
        self.assertTrue(pod_spec.get("hostNetwork"), "DaemonSet must run with hostNetwork: true")

        self.assertTrue(
            any(t.get("operator") == "Exists" for t in pod_spec.get("tolerations", [])),
            "DaemonSet must tolerate all taints to run on MicroOS and builder nodes",
        )

        containers = pod_spec.get("containers", [])
        self.assertGreaterEqual(len(containers), 1)
        c = containers[0]
        self.assertTrue(c.get("securityContext", {}).get("privileged"), "Container must be privileged")

        mounts = {m["name"]: m["mountPath"] for m in c.get("volumeMounts", [])}
        self.assertIn("host-etc", mounts)
        self.assertEqual(mounts["host-etc"], "/host/etc")
        self.assertIn("host-var", mounts)
        self.assertEqual(mounts["host-var"], "/host/var")
        self.assertIn("host-run", mounts)
        self.assertEqual(mounts["host-run"], "/host/var/run")

        volumes = {v["name"]: v.get("hostPath", {}).get("path") for v in pod_spec.get("volumes", [])}
        self.assertIn("host-var", volumes)
        self.assertEqual(volumes["host-var"], "/var")

        env_map = {e["name"]: e.get("value", "") for e in c.get("env", [])}
        self.assertIn("LUMA_ZOT_REGISTRY_HOST", env_map)
        self.assertEqual(env_map["LUMA_ZOT_REGISTRY_HOST"], "100.66.177.70:5000")
        self.assertIn("LUMA_INTERNAL_GIT_HOSTS", env_map)

        cmd = " ".join(c.get("command", []))
        self.assertIn("reconcile_registries", cmd)
        self.assertTrue("fstrim" in cmd or "btrfs" in cmd)

    # --------------------------------------------------------------------------
    # Feature 8: 4-Step Zero-Downtime Migration Workflow Lifecycle
    # --------------------------------------------------------------------------
    def test_four_step_migration_lifecycle_full_workflow(self) -> None:
        """Full 4-step migration: plan -> shadow -> cutover -> finalize for Batch 1 stateless app."""
        manifest_yaml = """
name: luma-site
region: cn
port: 80
exposure: cn-edge
domain: luma.example.com
"""
        plan = self.migrator.plan(
            project="luma-site",
            manifest_text=manifest_yaml,
        )
        self.assertEqual(plan.project, "luma-site")
        self.assertEqual(plan.batch.batch_id, "batch1-stateless")
        self.assertEqual(plan.manifest_kind, "service")
        self.assertTrue(plan.ready)
        self.assertEqual(plan.domain, "luma.example.com")

        shadow = self.migrator.shadow(
            project="luma-site",
            manifest_text=manifest_yaml,
            running_images={
                "luma-site": {"repository": "100.66.177.70:5000/site", "digest": "sha256:" + "a" * 64}
            },
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["trafficTarget"], "nomad")

        cutover = self.migrator.cutover("luma-site")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["trafficTarget"], "k3s-flux")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")

        finalize = self.migrator.finalize("luma-site")
        self.assertEqual(finalize["state"], "finalized")
        self.assertEqual(finalize["legacyNomadStatus"], "purged")
        self.assertEqual(finalize["orphanQueueWorkersRemaining"], 0)

    def test_migration_abort_reverts_traffic_to_nomad(self) -> None:
        """Verifies abort instantly switches traffic back to standby Nomad allocation."""
        manifest_yaml = "name: rollback-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="rollback-app",
            manifest_text=manifest_yaml,
            running_images={
                "rollback-app": {"repository": "100.66.177.70:5000/rb", "digest": "sha256:" + "b" * 64}
            },
        )
        self.migrator.cutover("rollback-app")

        aborted = self.migrator.abort("rollback-app")
        self.assertEqual(aborted["state"], "aborted")
        self.assertEqual(aborted["trafficTarget"], "nomad")

        recut = self.migrator.cutover("rollback-app")
        self.assertEqual(recut["state"], "cutover_complete")
        self.assertEqual(recut["trafficTarget"], "k3s-flux")

    def test_migration_abort_blocked_after_finalization(self) -> None:
        """TC-B-F8-02: Attempting to abort after finalize raises LumaError."""
        manifest_yaml = "name: final-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="final-app",
            manifest_text=manifest_yaml,
            running_images={
                "final-app": {"repository": "100.66.177.70:5000/fin", "digest": "sha256:" + "c" * 64}
            },
        )
        self.migrator.cutover("final-app")
        self.migrator.finalize("final-app")

        with self.assertRaises(LumaError) as ctx:
            self.migrator.abort("final-app")
        self.assertIn("finalize", str(ctx.exception).lower())

    def test_cutover_without_shadow_blocked(self) -> None:
        """TC-B-F8-01: Cutover without prior shadow call raises LumaError."""
        manifest_yaml = "name: no-shadow\nregion: cn\nport: 8080\n"
        self.migrator.plan(project="no-shadow", manifest_text=manifest_yaml)

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("no-shadow")
        self.assertIn("shadow", str(ctx.exception).lower())

    def test_finalize_without_cutover_blocked(self) -> None:
        """Attempting to finalize an un-cutover workload raises LumaError."""
        manifest_yaml = "name: uncapped\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="uncapped",
            manifest_text=manifest_yaml,
            running_images={"uncapped": {"repository": "100.66.177.70:5000/uc", "digest": "sha256:" + "1" * 64}},
        )
        with self.assertRaises(LumaError):
            self.migrator.finalize("uncapped")

    # --------------------------------------------------------------------------
    # Feature 8: Batch 2 Queue-Worker Replica 0 Isolation & Drain Cutover
    # --------------------------------------------------------------------------
    def test_batch2_shadow_queue_workers_replica_zero(self) -> None:
        """TC-F8-01: Batch 2 queue workers scaled to 0 in shadow while web replicas >= 1."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/serverx-web:v1
  video-worker:
    image: 100.66.177.70:5000/serverx-video:v1
  browser-worker:
    image: 100.66.177.70:5000/serverx-browser:v1
"""
        manifest_yaml = """
name: serverx
region: cn
services:
  web:
    port: 8080
    replicas: 2
  video-worker:
    replicas: 4
  browser-worker:
    replicas: 2
"""
        shadow = self.migrator.shadow(
            project="serverx",
            manifest_text=manifest_yaml,
            compose_text=compose_yaml,
            running_images={
                "web": {"repository": "100.66.177.70:5000/serverx-web", "digest": "sha256:" + "1" * 64},
                "video-worker": {"repository": "100.66.177.70:5000/serverx-video", "digest": "sha256:" + "2" * 64},
                "browser-worker": {"repository": "100.66.177.70:5000/serverx-browser", "digest": "sha256:" + "3" * 64},
            },
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["trafficTarget"], "nomad")

        rel_id = shadow["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)
        web_dep = next(d for d in compiled.deployments if "web" in d["metadata"]["name"])
        video_dep = next(d for d in compiled.deployments if "video-worker" in d["metadata"]["name"])
        browser_dep = next(d for d in compiled.deployments if "browser-worker" in d["metadata"]["name"])

        self.assertEqual(web_dep["spec"]["replicas"], 2)
        self.assertEqual(video_dep["spec"]["replicas"], 0)
        self.assertEqual(browser_dep["spec"]["replicas"], 0)

    def test_batch2_cutover_scales_workers_and_drains_nomad(self) -> None:
        """TC-F8-02: Cutover scales paused workers to target replicas and drains Nomad workers."""
        self.test_batch2_shadow_queue_workers_replica_zero()

        cutover = self.migrator.cutover("serverx")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["trafficTarget"], "k3s-flux")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")

        rel_id = cutover["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)
        video_dep = next(d for d in compiled.deployments if "video-worker" in d["metadata"]["name"])
        browser_dep = next(d for d in compiled.deployments if "browser-worker" in d["metadata"]["name"])

        self.assertEqual(video_dep["spec"]["replicas"], 4)
        self.assertEqual(browser_dep["spec"]["replicas"], 2)

    def test_batch2_multi_service_worker_drain_and_finalize(self) -> None:
        """Finalizing Batch 2 application purges Nomad job and verifies 0 orphan workers."""
        self.test_batch2_cutover_scales_workers_and_drains_nomad()
        finalize = self.migrator.finalize("serverx")
        self.assertEqual(finalize["state"], "finalized")
        self.assertEqual(finalize["legacyNomadStatus"], "purged")
        self.assertEqual(finalize["orphanQueueWorkersRemaining"], 0)

    # --------------------------------------------------------------------------
    # Feature 8: Batch 3 Stateful Volume Node Pin & Single-Instance Bot Isolation
    # --------------------------------------------------------------------------
    def test_batch3_stateful_volume_missing_node_pin_blocks_cutover(self) -> None:
        """TC-F8-03: Batch 3 stateful volume lacking explicit node pin blocks cutover."""
        manifest_yaml = """
name: kato
region: cn
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        plan = self.migrator.plan(
            project="kato",
            manifest_text=manifest_yaml,
            existing_volumes=[{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": ""}],
        )
        self.assertEqual(plan.batch.batch_id, "batch3-stateful-volumes")
        self.assertFalse(plan.ready)
        self.assertTrue(any("node pin" in w.lower() for w in plan.warnings))

        self.migrator.shadow(
            project="kato",
            manifest_text=manifest_yaml,
            running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
            existing_volumes=[{"name": "data", "hostPath": "/var/lib/luma/volumes/kato/data", "node": ""}],
        )

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("kato")
        self.assertIn("node pin", str(ctx.exception).lower())

    def test_batch3_hostpath_and_fsgroup_preservation(self) -> None:
        """TC-F8-04: Batch 3 retains hostPath and preserves fsGroup without copying data."""
        manifest_yaml = """
name: kato
region: cn
node: prod-cn-1
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        existing_vols = [
            {
                "name": "data",
                "hostPath": "/var/lib/luma/volumes/kato/data",
                "node": "prod-cn-1",
                "fsGroup": 1000,
            }
        ]
        plan = self.migrator.plan(
            project="kato",
            manifest_text=manifest_yaml,
            existing_volumes=existing_vols,
        )
        self.assertTrue(plan.ready)
        self.assertEqual(plan.adopted_volumes[0]["hostPath"], "/var/lib/luma/volumes/kato/data")
        self.assertEqual(plan.adopted_volumes[0]["fsGroup"], 1000)

        self.migrator.shadow(
            project="kato",
            manifest_text=manifest_yaml,
            running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
            existing_volumes=existing_vols,
        )
        cutover = self.migrator.cutover("kato")
        self.assertEqual(cutover["state"], "cutover_complete")

        compiled = self.migrator.get_compiled_release(cutover["releaseId"])
        dep = compiled.deployments[0]
        pod_spec = dep["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["nodeSelector"].get("kubernetes.io/hostname"), "prod-cn-1")
        self.assertEqual(pod_spec.get("securityContext", {}).get("fsGroup"), 1000)

    def test_batch3_single_instance_bot_shadow_isolation_and_atomic_cutover(self) -> None:
        """Single-instance bot (SQLite/flock/Telegram): shadow replicas 0, stop Nomad before K3s scale 1."""
        manifest_yaml = """
name: bot
region: cn
node: prod-cn-1
volumes:
  - data:/var/lib/bot
storage:
  data:
    storageClass: local-path
    size: 5Gi
"""
        existing_vols = [
            {
                "name": "data",
                "hostPath": "/var/lib/luma/volumes/bot/data",
                "node": "prod-cn-1",
                "fsGroup": 1000,
            }
        ]
        shadow = self.migrator.shadow(
            project="bot",
            manifest_text=manifest_yaml,
            running_images={"bot": {"repository": "100.66.177.70:5000/bot", "digest": "sha256:" + "5" * 64}},
            existing_volumes=existing_vols,
        )
        rel_id = shadow["releaseId"]
        compiled_shadow = self.migrator.get_compiled_release(rel_id)
        self.assertEqual(compiled_shadow.deployments[0]["spec"]["replicas"], 0)

        cutover = self.migrator.cutover("bot")
        self.assertEqual(cutover["legacyNomadStatus"], "standby")
        compiled_cutover = self.migrator.get_compiled_release(cutover["releaseId"])
        self.assertEqual(compiled_cutover.deployments[0]["spec"]["replicas"], 1)

    # --------------------------------------------------------------------------
    # Feature 8: Pre-Cutover Safety Gates
    # --------------------------------------------------------------------------
    def test_pre_cutover_health_probe_failure_blocks_cutover(self) -> None:
        """TC-F8-05: Gateway probe failure blocks cutover until probes succeed."""
        manifest_yaml = "name: probe-fail\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="probe-fail",
            manifest_text=manifest_yaml,
            running_images={"probe-fail": {"repository": "100.66.177.70:5000/pf", "digest": "sha256:" + "6" * 64}},
            gateway_probe_ok=False,
        )
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("probe-fail")
        self.assertIn("gateway_probe_ok", str(ctx.exception).lower())

    def test_pre_cutover_secret_parity_verification(self) -> None:
        """Verifies missing or out-of-sync secrets block cutover."""
        manifest_yaml = "name: secret-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="secret-app",
            manifest_text=manifest_yaml,
            running_images={"secret-app": {"repository": "100.66.177.70:5000/sa", "digest": "sha256:" + "7" * 64}},
            secrets={"API_KEY": "12345"},
            mounted_secret_files={"API_KEY": "/app/secrets/api.key"},
        )
        cutover = self.migrator.cutover("secret-app")
        self.assertEqual(cutover["state"], "cutover_complete")
        self.assertEqual(cutover["syncedSecrets"], 1)

    def test_pre_cutover_architecture_mismatch_blocks_cutover(self) -> None:
        """Compiling image with arch mismatch against target node blocks cutover."""
        manifest_yaml = "name: arch-app\nregion: cn\nnode: microos-node-1\nport: 8080\n"
        self.migrator.shadow(
            project="arch-app",
            manifest_text=manifest_yaml,
            running_images={
                "arch-app": {
                    "repository": "100.66.177.70:5000/aa",
                    "digest": "sha256:" + "8" * 64,
                    "platform": "linux/amd64",
                }
            },
            target_node_arch="arm64",
        )
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("arch-app")
        self.assertIn("architecture", str(ctx.exception).lower())

    def test_pre_cutover_image_digest_drift_detection(self) -> None:
        """TC-B-F8-03: Running Nomad image digest drifting before cutover raises LumaError."""
        manifest_yaml = "name: drift-app\nregion: cn\nport: 8080\n"
        self.migrator.shadow(
            project="drift-app",
            manifest_text=manifest_yaml,
            running_images={
                "drift-app": {"repository": "100.66.177.70:5000/da", "digest": "sha256:" + "1" * 64}
            },
        )
        drifted_images = {
            "drift-app": {"repository": "100.66.177.70:5000/da", "digest": "sha256:" + "9" * 64}
        }
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("drift-app", current_running_images=drifted_images)
        self.assertIn("drift", str(ctx.exception).lower())

    def test_cutover_rejects_node_none_or_null_stringification(self) -> None:
        """TC-B-F8-06: Verifies node values of None, 'None', 'null', '~', or empty string in existing_volumes
        are not stringified to truthy strings, marking plan.ready=False and blocking cutover with LumaError.
        """
        manifest_yaml = """
name: kato
region: cn
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        for bad_node in [None, "None", "null", "~", ""]:
            with self.subTest(bad_node=bad_node):
                existing_vols = [{"name": "data", "path": "/data", "node": bad_node}]
                plan = self.migrator.plan(
                    project="kato",
                    manifest_text=manifest_yaml,
                    existing_volumes=existing_vols,
                )
                self.assertFalse(plan.ready, f"plan.ready must be False when node={bad_node!r}")
                self.assertTrue(
                    any("node pin" in w.lower() for w in plan.warnings),
                    f"plan.warnings must warn about missing node pin for node={bad_node!r}",
                )
                self.assertEqual(
                    plan.adopted_volumes[0]["node"],
                    "",
                    f"plan.adopted_volumes[0]['node'] must be empty string, not {bad_node!r}",
                )

                self.migrator.shadow(
                    project="kato",
                    manifest_text=manifest_yaml,
                    running_images={"kato": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "4" * 64}},
                    existing_volumes=existing_vols,
                )

                with self.assertRaises(LumaError) as ctx:
                    self.migrator.cutover("kato")
                err_msg = str(ctx.exception).lower()
                self.assertIn("node pin", err_msg)
                self.assertIn("stateful volume", err_msg)

    def test_cutover_rejects_architecture_mismatch_when_platform_omitted(self) -> None:
        """TC-B-F8-07: Verifies that omitting the platform key in running_images defaults to linux/amd64
        and cutover() properly detects the architecture mismatch when target_node_arch is arm64.
        """
        manifest_yaml = "name: serverx\nregion: cn\nport: 8080\n"
        shadow = self.migrator.shadow(
            project="serverx",
            manifest_text=manifest_yaml,
            running_images={"web": {"image": "serverx:v1"}},
            target_node_arch="arm64",
        )
        self.assertEqual(shadow["state"], "shadow_ready")
        self.assertEqual(shadow["targetNodeArch"], "arm64")

        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover("serverx")
        err_msg = str(ctx.exception).lower()
        self.assertIn("architecture mismatch", err_msg)
        self.assertIn("arm64", err_msg)

    def test_cutover_detects_digest_drift_when_initial_digest_omitted(self) -> None:
        """TC-B-F8-08: Verifies that omitting the digest key during shadow() retains the resolved/synthetic
        artifact digest, allowing cutover() to detect drift against a new current_running_images digest.
        """
        manifest_yaml = "name: serverx-drift\nregion: cn\nport: 8080\n"
        shadow = self.migrator.shadow(
            project="serverx-drift",
            manifest_text=manifest_yaml,
            running_images={"web": {"image": "serverx:v1"}},
        )
        self.assertEqual(shadow["state"], "shadow_ready")

        drifted_digest = "sha256:drifted1234567890"
        with self.assertRaises(LumaError) as ctx:
            self.migrator.cutover(
                "serverx-drift",
                current_running_images={"web": {"image": "serverx:v1", "digest": drifted_digest}},
            )
        err_msg = str(ctx.exception).lower()
        self.assertIn("image digest drift detected", err_msg)

    # --------------------------------------------------------------------------
    # Feature 8: Same-Node Traefik Port Conflict & Memory Headroom Protection
    # --------------------------------------------------------------------------
    def test_same_node_traefik_port_conflict_protection(self) -> None:
        """Same-node migration uses Traefik Ingress routing without hostPort 80/443 collision."""
        manifest_yaml = """
name: web-edge
region: cn
port: 80
exposure: cn-edge
domain: edge.example.com
"""
        shadow = self.migrator.shadow(
            project="web-edge",
            manifest_text=manifest_yaml,
            running_images={"web-edge": {"repository": "100.66.177.70:5000/we", "digest": "sha256:" + "a" * 64}},
        )
        rel_id = shadow["releaseId"]
        compiled = self.migrator.get_compiled_release(rel_id)

        containers = compiled.deployments[0]["spec"]["template"]["spec"]["containers"]
        for c in containers:
            for p in c.get("ports", []):
                self.assertNotIn("hostPort", p, "Workload must not bind hostPort directly to avoid Traefik collision")

        self.assertGreaterEqual(len(compiled.ingresses), 1)

    def test_same_node_memory_headroom_check(self) -> None:
        """Checks available memory headroom before scheduling shadow workloads on same node."""
        manifest_yaml = "name: heavy-app\nregion: cn\nport: 8080\n"

        with self.assertRaises(LumaError) as ctx:
            self.migrator.shadow(
                project="heavy-app",
                manifest_text=manifest_yaml,
                running_images={"heavy-app": {"repository": "100.66.177.70:5000/ha", "digest": "sha256:" + "b" * 64}},
                available_memory_mb=256,
            )
        self.assertIn("memory headroom", str(ctx.exception).lower())

        shadow = self.migrator.shadow(
            project="heavy-app",
            manifest_text=manifest_yaml,
            running_images={"heavy-app": {"repository": "100.66.177.70:5000/ha", "digest": "sha256:" + "b" * 64}},
            available_memory_mb=2048,
        )
        self.assertEqual(shadow["state"], "shadow_ready")

    # --------------------------------------------------------------------------
    # Tier 3: Cross-Feature Pairwise Invariants & Boundary Guards
    # --------------------------------------------------------------------------
    def test_cross_microos_self_heal_and_batch3_stateful_migration(self) -> None:
        """TC-X-02: MicroOS self-healing + Batch 3 stateful volume node pin to MicroOS host."""
        node_report = evaluate_node_self_heal(
            {
                "name": "microos-node-1",
                "osName": "opensuse-microos",
                "registriesYamlConverged": True,
                "diskUsedPercent": 78.0,
            },
            auto_fix=True,
        )
        self.assertTrue(node_report.healthy)

        manifest_yaml = """
name: kato-microos
region: cn
node: microos-node-1
port: 8080
volumes:
  - data:/var/lib/kato
storage:
  data:
    storageClass: local-path
    size: 10Gi
"""
        existing_vols = [
            {
                "name": "data",
                "hostPath": "/var/lib/luma/volumes/kato/data",
                "node": "microos-node-1",
                "fsGroup": 1000,
            }
        ]
        self.migrator.shadow(
            project="kato-microos",
            manifest_text=manifest_yaml,
            running_images={"kato-microos": {"repository": "100.66.177.70:5000/kato", "digest": "sha256:" + "c" * 64}},
            existing_volumes=existing_vols,
        )
        cutover = self.migrator.cutover("kato-microos")
        self.assertEqual(cutover["state"], "cutover_complete")

        compiled = self.migrator.get_compiled_release(cutover["releaseId"])
        self.assertEqual(
            compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/hostname"),
            "microos-node-1",
        )

    def test_cross_darwin_apple_silicon_self_heal_and_native_builder_selection(self) -> None:
        """TC-X-08: Darwin Apple Silicon self-healing aligns NO_PROXY, BuildController selects builder."""
        report = evaluate_node_self_heal(
            {
                "name": "macbook-pro",
                "osName": "darwin",
                "noProxy": "localhost,127.0.0.1",
            },
            auto_fix=True,
        )
        self.assertTrue(report.healthy)
        self.assertTrue(report.no_proxy_aligned)

        builders = [
            BuilderEndpoint(name="macbook-pro", address="tcp://100.64.0.12:1234", platform="linux/arm64", os_name="darwin"),
            BuilderEndpoint(name="buildkit-amd64", address="tcp://100.64.0.10:1234", platform="linux/amd64", os_name="linux"),
        ]
        controller = BuildController(registry_host="100.66.177.70:5000", builders=builders)
        selected = controller.select_native_builder("linux/arm64")
        self.assertEqual(selected.name, "macbook-pro")

    def test_replicas_override_type_coercion_guard(self) -> None:
        """TC-B-F8-05: Non-integer or negative replicas override raises LumaError."""
        manifest_yaml = "name: bad-replicas\nregion: cn\nport: 8080\n"
        with self.assertRaises(LumaError):
            self.migrator.shadow(
                project="bad-replicas",
                manifest_text=manifest_yaml,
                running_images={"bad-replicas": {"repository": "100.66.177.70:5000/br", "digest": "sha256:" + "d" * 64}},
                replicas_override={"bad-replicas": -1},
            )


# Module-level aliases for runner interoperability
MigrationReliabilityAndMicroOSSelfHealTests = MigrationReliabilityAndNodeSelfHealingTests


def __getattr__(name: str) -> Any:
    if name == "RuntimeNodeAffinityAndSidecarIntegrityTests":
        return RuntimeArchitectureAffinityAndComposeIntegrityTests
    if name == "MigrationReliabilityAndMicroOSSelfHealTests":
        return MigrationReliabilityAndNodeSelfHealingTests
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    unittest.main()

