"""Executable test suite design for Milestone 3 (F5, F6) in tests/test_paas_engine.py."""
from __future__ import annotations

import re
import time
from typing import Any, Mapping
import unittest
import yaml

from luma.build_capabilities import infer_native_platform, validate_native_platform
from luma.errors import LumaError
from luma.paas import (
    ArtifactCatalog,
    BuildArtifact,
    FluxReconciler,
    ServiceImageArtifact,
    compile_compose_release,
    compile_service_release,
)


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


def __getattr__(name: str) -> Any:
    if name == "RuntimeNodeAffinityAndSidecarIntegrityTests":
        return RuntimeArchitectureAffinityAndComposeIntegrityTests
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    unittest.main()
