"""Adversarial challenger test suite for Milestone 3 - Feature 5:
Runtime Architecture Affinity & Sidecar Integrity.

Empirically tests:
1. Sidecar dependency graphs:
   - Deep linear chains (4 containers, 10 containers) co-locating into single root Pod
   - Diamond / tree dependencies (multi-branch inverted tree) co-locating into single root Pod
   - High-cardinality star topology (20 sidecars on single primary)
   - Multi-primary forest (multiple independent Compose primaries, each with sidecars)
   - Preservation of custom command overrides and volumeMounts across deep sidecars

2. Cyclic and malformed dependency graphs:
   - Direct self-loop (service:web on web)
   - Direct self-loop with whitespace padding
   - Two-node mutual cycles (A -> B -> A)
   - Three-node cycles (A -> B -> C -> A)
   - Deep cycle at tail (A -> B -> C -> D -> E -> C)
   - Disjoint cycles alongside valid primaries failing compilation
   - Self-referential intermediate service (A -> B -> B)
   - Missing parent service reference
   - Empty or blank target service reference

3. Architecture mismatch detection in deep chains & trees:
   - Mismatch at leaf of deep chain raises LumaError
   - Mismatch at intermediate node 1 of deep chain raises LumaError
   - Mismatch at intermediate node 2 of deep chain raises LumaError
   - Mismatch at root primary of deep chain raises LumaError
   - Mismatch in diamond tree leaf raises LumaError
   - Mismatch in diamond tree intermediate node raises LumaError
   - Single mismatch among 20 sidecars raises LumaError naming the culprit
   - Inferred node architecture mismatch across sidecars raises LumaError
   - node_architectures mapping mismatch across sidecars raises LumaError

4. Architecture inference and nodeSelector permutations:
   - Platform aliases (arm64, aarch64, arm64/v8, ARM64, darwin/arm64) -> kubernetes.io/arch: arm64
   - Platform aliases (amd64, x86_64, AMD64, X86_64, darwin/amd64) -> kubernetes.io/arch: amd64
   - Invalid / unsupported / multi-arch platforms rejected with LumaError
   - Region home Apple Silicon strict arm64 enforcement & amd64 rejection
   - Topology conflicts (macbook-pro / air3 in cn / global regions) rejected with LumaError
   - Node name heuristics (arm64, aarch64 vs amd64, x86_64) and conflict rejection
   - node_architectures mapping lookup and case-insensitive clean_node handling
   - Neutral node names allowing either architecture without false conflicts
   - Compose multi-service independent deployments with distinct architectures (ARM64 & AMD64)
"""
from __future__ import annotations

import unittest
import yaml

from luma.errors import LumaError
from luma.paas.build.artifact_store import BuildArtifact, ServiceImageArtifact
from luma.paas.release.helm_compiler import (
    compile_compose_release,
    compile_service_release,
)


class SidecarDependencyGraphStressTests(unittest.TestCase):
    """Stress tests deep and complex sidecar dependency graphs."""

    def test_deep_chain_4_containers_colocation(self) -> None:
        """Verify deep linear chain A -> B -> C -> D co-locates 4 containers in D's Pod."""
        compose_yaml = """
services:
  c_root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/arm64
  c_mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/arm64
    network_mode: service:c_root
  c_mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/arm64
    network_mode: service:c_mid2
  c_leaf:
    image: 100.66.177.70:5000/leaf:v1
    platform: linux/arm64
    network_mode: service:c_mid1
"""
        art = BuildArtifact(
            artifact_id="art-chain-4",
            project="chain-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: chain-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={
                "c_root": ServiceImageArtifact("c_root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
                "c_mid2": ServiceImageArtifact("c_mid2", "100.66.177.70:5000/mid2", "sha256:" + "2" * 64, "v1", platform="linux/arm64"),
                "c_mid1": ServiceImageArtifact("c_mid1", "100.66.177.70:5000/mid1", "sha256:" + "3" * 64, "v1", platform="linux/arm64"),
                "c_leaf": ServiceImageArtifact("c_leaf", "100.66.177.70:5000/leaf", "sha256:" + "4" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 1)
        dep = compiled.deployments[0]
        self.assertEqual(dep["metadata"]["name"], "chain-test-c-root")
        containers = dep["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 4)
        c_names = [c["name"] for c in containers]
        self.assertEqual(c_names[0], "c-root")
        self.assertCountEqual(c_names, ["c-root", "c-leaf", "c-mid1", "c-mid2"])
        node_selector = dep["spec"]["template"]["spec"].get("nodeSelector", {})
        self.assertEqual(node_selector.get("kubernetes.io/arch"), "arm64")

    def test_deep_chain_10_containers_colocation(self) -> None:
        """Verify chain of 10 containers c0 -> c1 -> ... -> c9 co-locates all into c9's Pod."""
        services_dict = {
            "c9": {
                "image": "100.66.177.70:5000/c9:v1",
                "platform": "linux/amd64",
            }
        }
        images_dict = {
            "c9": ServiceImageArtifact("c9", "100.66.177.70:5000/c9", "sha256:" + "9" * 64, "v1", platform="linux/amd64")
        }
        for i in range(8, -1, -1):
            curr_name = f"c{i}"
            parent_name = f"c{i+1}"
            services_dict[curr_name] = {
                "image": f"100.66.177.70:5000/{curr_name}:v1",
                "platform": "linux/amd64",
                "network_mode": f"service:{parent_name}",
            }
            images_dict[curr_name] = ServiceImageArtifact(
                curr_name, f"100.66.177.70:5000/{curr_name}", f"sha256:{i}" + "0" * 63, "v1", platform="linux/amd64"
            )

        compose_yaml = yaml.dump({"services": services_dict})
        art = BuildArtifact(
            artifact_id="art-chain-10",
            project="chain-10",
            source_repo="local",
            commit_sha="b" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: chain-10\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images=images_dict,
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 1)
        dep = compiled.deployments[0]
        self.assertEqual(dep["metadata"]["name"], "chain-10-c9")
        containers = dep["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 10)
        self.assertEqual(containers[0]["name"], "c9")
        self.assertEqual(dep["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"), "amd64")

    def test_diamond_dependency_tree(self) -> None:
        """Verify inverted diamond tree co-locates all branches into the single root Pod."""
        compose_yaml = """
services:
  root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/arm64
  mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/arm64
    network_mode: service:root
  mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/arm64
    network_mode: service:root
  leaf1:
    image: 100.66.177.70:5000/leaf1:v1
    platform: linux/arm64
    network_mode: service:mid1
  leaf2:
    image: 100.66.177.70:5000/leaf2:v1
    platform: linux/arm64
    network_mode: service:mid1
  leaf3:
    image: 100.66.177.70:5000/leaf3:v1
    platform: linux/arm64
    network_mode: service:mid2
  leaf4:
    image: 100.66.177.70:5000/leaf4:v1
    platform: linux/arm64
    network_mode: service:mid2
"""
        images = {
            name: ServiceImageArtifact(name, f"100.66.177.70:5000/{name}", f"sha256:{i}" + "1" * 63, "v1", platform="linux/arm64")
            for i, name in enumerate(["root", "mid1", "mid2", "leaf1", "leaf2", "leaf3", "leaf4"])
        }
        art = BuildArtifact(
            artifact_id="art-diamond",
            project="diamond-app",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: diamond-app\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images=images,
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 1)
        containers = compiled.deployments[0]["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 7)
        self.assertEqual(containers[0]["name"], "root")
        c_names = {c["name"] for c in containers}
        self.assertEqual(c_names, {"root", "mid1", "mid2", "leaf1", "leaf2", "leaf3", "leaf4"})

    def test_many_sidecars_single_primary_star_topology(self) -> None:
        """Verify high-cardinality star topology (20 sidecars pointing to 1 primary)."""
        services_dict = {
            "primary": {
                "image": "100.66.177.70:5000/primary:v1",
                "platform": "linux/arm64",
            }
        }
        images_dict = {
            "primary": ServiceImageArtifact("primary", "100.66.177.70:5000/primary", "sha256:" + "0" * 64, "v1", platform="linux/arm64")
        }
        for i in range(1, 21):
            name = f"sidecar_{i:02d}"
            services_dict[name] = {
                "image": f"100.66.177.70:5000/{name}:v1",
                "platform": "linux/arm64",
                "network_mode": "service:primary",
            }
            images_dict[name] = ServiceImageArtifact(
                name, f"100.66.177.70:5000/{name}", f"sha256:{i:02d}" + "1" * 62, "v1", platform="linux/arm64"
            )

        compose_yaml = yaml.dump({"services": services_dict})
        art = BuildArtifact(
            artifact_id="art-20-sidecars",
            project="star-stack",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: star-stack\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images=images_dict,
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 1)
        containers = compiled.deployments[0]["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(containers), 21)
        self.assertEqual(containers[0]["name"], "primary")

    def test_multiple_independent_primaries_forest(self) -> None:
        """Verify multiple independent primaries each compile to distinct Deployments with their own sidecars."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
    platform: linux/arm64
  web_sidecar1:
    image: 100.66.177.70:5000/ws1:v1
    platform: linux/arm64
    network_mode: service:web
  web_sidecar2:
    image: 100.66.177.70:5000/ws2:v1
    platform: linux/arm64
    network_mode: service:web
  worker:
    image: 100.66.177.70:5000/worker:v1
    platform: linux/arm64
  worker_sidecar:
    image: 100.66.177.70:5000/wks:v1
    platform: linux/arm64
    network_mode: service:worker
  cron:
    image: 100.66.177.70:5000/cron:v1
    platform: linux/arm64
"""
        images = {
            name: ServiceImageArtifact(name, f"100.66.177.70:5000/{name}", f"sha256:{i}" + "2" * 63, "v1", platform="linux/arm64")
            for i, name in enumerate(["web", "web_sidecar1", "web_sidecar2", "worker", "worker_sidecar", "cron"])
        }
        art = BuildArtifact(
            artifact_id="art-forest",
            project="forest-app",
            source_repo="local",
            commit_sha="e" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: forest-app\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images=images,
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 3)
        deps_by_name = {d["metadata"]["name"]: d for d in compiled.deployments}
        self.assertIn("forest-app-web", deps_by_name)
        self.assertIn("forest-app-worker", deps_by_name)
        self.assertIn("forest-app-cron", deps_by_name)

        web_containers = [c["name"] for c in deps_by_name["forest-app-web"]["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(len(web_containers), 3)
        self.assertEqual(web_containers[0], "web")

        worker_containers = [c["name"] for c in deps_by_name["forest-app-worker"]["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(len(worker_containers), 2)
        self.assertEqual(worker_containers[0], "worker")

        cron_containers = [c["name"] for c in deps_by_name["forest-app-cron"]["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(len(cron_containers), 1)
        self.assertEqual(cron_containers[0], "cron")

    def test_sidecar_custom_command_and_volume_retention_in_chain(self) -> None:
        """Verify custom command overrides and distinct volumeMounts are preserved in deep chain."""
        compose_yaml = """
services:
  main_app:
    image: 100.66.177.70:5000/main:v1
    command: ["/bin/main-server", "--bind=0.0.0.0"]
    volumes:
      - main-data:/var/lib/main
  mid_agent:
    image: 100.66.177.70:5000/mid:v1
    network_mode: service:main_app
    command: ["/bin/mid-daemon", "--mode=sync"]
    volumes:
      - mid-data:/var/lib/mid:ro
  leaf_exporter:
    image: 100.66.177.70:5000/leaf:v1
    network_mode: service:mid_agent
    command: ["/bin/prometheus-exporter", "-v"]
    volumes:
      - leaf-data:/metrics
"""
        luma_yaml = """
name: chained-volumes
region: cn
storage:
  main-data:
    size: 50Gi
  mid-data:
    size: 10Gi
  leaf-data:
    size: 2Gi
"""
        images = {
            name: ServiceImageArtifact(name, f"100.66.177.70:5000/{name}", f"sha256:{i}" + "3" * 63, "v1", platform="linux/arm64")
            for i, name in enumerate(["main_app", "mid_agent", "leaf_exporter"])
        }
        art = BuildArtifact(
            artifact_id="art-chain-vols",
            project="chained-volumes",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_yaml,
            compose_snapshot=compose_yaml,
            images=images,
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 1)
        pod = compiled.deployments[0]["spec"]["template"]["spec"]
        containers_by_name = {c["name"]: c for c in pod["containers"]}

        self.assertEqual(containers_by_name["main-app"]["command"], ["/bin/main-server", "--bind=0.0.0.0"])
        self.assertEqual(containers_by_name["mid-agent"]["command"], ["/bin/mid-daemon", "--mode=sync"])
        self.assertEqual(containers_by_name["leaf-exporter"]["command"], ["/bin/prometheus-exporter", "-v"])

        mid_mount = next(m for m in containers_by_name["mid-agent"]["volumeMounts"] if m["name"] == "mid-data")
        self.assertTrue(mid_mount.get("readOnly"))
        self.assertEqual(mid_mount["mountPath"], "/var/lib/mid")

        leaf_mount = next(m for m in containers_by_name["leaf-exporter"]["volumeMounts"] if m["name"] == "leaf-data")
        self.assertFalse(leaf_mount.get("readOnly", False))
        self.assertEqual(leaf_mount["mountPath"], "/metrics")

        # Verify PVC requests
        pvcs_by_name = {p["metadata"]["name"]: p for p in compiled.pvcs}
        self.assertEqual(pvcs_by_name["chained-volumes-main-data"]["spec"]["resources"]["requests"]["storage"], "50Gi")
        self.assertEqual(pvcs_by_name["chained-volumes-mid-data"]["spec"]["resources"]["requests"]["storage"], "10Gi")
        self.assertEqual(pvcs_by_name["chained-volumes-leaf-data"]["spec"]["resources"]["requests"]["storage"], "2Gi")


class CyclicAndMalformedSidecarGraphTests(unittest.TestCase):
    """Stress tests cyclic dependency detection and malformed sidecar references."""

    def test_direct_self_loop_rejection(self) -> None:
        """Verify service declaring network_mode pointing to itself is rejected immediately."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
    network_mode: service:web
"""
        art = BuildArtifact(
            artifact_id="art-self-loop",
            project="loop-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: loop-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("self-referential", str(ctx.exception).lower())

    def test_direct_self_loop_with_whitespace(self) -> None:
        """Verify whitespace around target in network_mode does not bypass self-loop detection."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
    network_mode: "service:   web  "
"""
        art = BuildArtifact(
            artifact_id="art-self-loop-ws",
            project="loop-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: loop-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("self-referential", str(ctx.exception).lower())

    def test_two_node_cycle_rejection(self) -> None:
        """Verify mutual dependency cycle A -> B -> A is detected and raises LumaError."""
        compose_yaml = """
services:
  svc_a:
    image: 100.66.177.70:5000/a:v1
    network_mode: service:svc_b
  svc_b:
    image: 100.66.177.70:5000/b:v1
    network_mode: service:svc_a
"""
        art = BuildArtifact(
            artifact_id="art-cycle-2",
            project="cycle-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: cycle-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"svc_a": ServiceImageArtifact("svc_a", "100.66.177.70:5000/a", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cyclic network_mode dependency loop detected", str(ctx.exception))

    def test_three_node_cycle_rejection(self) -> None:
        """Verify three-node cycle A -> B -> C -> A is detected."""
        compose_yaml = """
services:
  c_a:
    image: 100.66.177.70:5000/a:v1
    network_mode: service:c_b
  c_b:
    image: 100.66.177.70:5000/b:v1
    network_mode: service:c_c
  c_c:
    image: 100.66.177.70:5000/c:v1
    network_mode: service:c_a
"""
        art = BuildArtifact(
            artifact_id="art-cycle-3",
            project="cycle-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: cycle-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"c_a": ServiceImageArtifact("c_a", "100.66.177.70:5000/a", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cyclic network_mode dependency loop detected", str(ctx.exception))
        self.assertIn("c_a -> c_b -> c_c -> c_a", str(ctx.exception))

    def test_deep_cycle_at_tail_rejection(self) -> None:
        """Verify cycle in tail of deep chain (A -> B -> C -> D -> E -> C) is detected."""
        compose_yaml = """
services:
  c_a:
    image: 100.66.177.70:5000/a:v1
    network_mode: service:c_b
  c_b:
    image: 100.66.177.70:5000/b:v1
    network_mode: service:c_c
  c_c:
    image: 100.66.177.70:5000/c:v1
    network_mode: service:c_d
  c_d:
    image: 100.66.177.70:5000/d:v1
    network_mode: service:c_e
  c_e:
    image: 100.66.177.70:5000/e:v1
    network_mode: service:c_c
"""
        art = BuildArtifact(
            artifact_id="art-cycle-tail",
            project="cycle-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: cycle-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"c_a": ServiceImageArtifact("c_a", "100.66.177.70:5000/a", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cyclic network_mode dependency loop detected", str(ctx.exception))

    def test_disjoint_cycle_with_valid_primary_fails_compilation(self) -> None:
        """Verify isolated cyclic services beside a valid primary fail the entire release."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
  web_sidecar:
    image: 100.66.177.70:5000/ws:v1
    network_mode: service:web
  bad1:
    image: 100.66.177.70:5000/b1:v1
    network_mode: service:bad2
  bad2:
    image: 100.66.177.70:5000/b2:v1
    network_mode: service:bad1
"""
        art = BuildArtifact(
            artifact_id="art-disjoint-cycle",
            project="disjoint-test",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: disjoint-test\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cyclic network_mode dependency loop detected", str(ctx.exception))

    def test_self_referential_intermediate_service_rejection(self) -> None:
        """Verify chain pointing to a self-referential intermediate service (A -> B -> B) is rejected."""
        compose_yaml = """
services:
  svc_a:
    image: 100.66.177.70:5000/a:v1
    network_mode: service:svc_b
  svc_b:
    image: 100.66.177.70:5000/b:v1
    network_mode: service:svc_b
"""
        art = BuildArtifact(
            artifact_id="art-inter-self",
            project="inter-self",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: inter-self\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"svc_a": ServiceImageArtifact("svc_a", "100.66.177.70:5000/a", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("self-referential network_mode", str(ctx.exception))

    def test_missing_parent_service_rejection(self) -> None:
        """Verify sidecar referencing a non-existent parent raises LumaError."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
  orphan_sidecar:
    image: 100.66.177.70:5000/orphan:v1
    network_mode: service:ghost_primary
"""
        art = BuildArtifact(
            artifact_id="art-missing-parent",
            project="missing-parent",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: missing-parent\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("missing parent service", str(ctx.exception))

    def test_empty_target_service_rejection(self) -> None:
        """Verify empty service: declaration is rejected."""
        compose_yaml = """
services:
  web:
    image: 100.66.177.70:5000/web:v1
    network_mode: "service:  "
"""
        art = BuildArtifact(
            artifact_id="art-blank-parent",
            project="blank-parent",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: blank-parent\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"web": ServiceImageArtifact("web", "100.66.177.70:5000/web", "sha256:" + "1" * 64, "v1")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("missing parent service", str(ctx.exception))


class ArchitectureMismatchInSidecarChainsTests(unittest.TestCase):
    """Stress tests architecture mismatch rejection across sidecars in deep chains and trees."""

    def test_mismatch_at_leaf_of_deep_chain(self) -> None:
        """Verify mismatch at leaf container of chain (A:amd64 -> B:arm64 -> C:arm64 -> D:arm64) raises LumaError."""
        compose_yaml = """
services:
  c_root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/arm64
  c_mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/arm64
    network_mode: service:c_root
  c_mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/arm64
    network_mode: service:c_mid2
  c_leaf:
    image: 100.66.177.70:5000/leaf:v1
    platform: linux/amd64
    network_mode: service:c_mid1
"""
        art = BuildArtifact(
            artifact_id="art-mismatch-leaf",
            project="mismatch-leaf",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: mismatch-leaf\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={
                "c_root": ServiceImageArtifact("c_root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
                "c_mid2": ServiceImageArtifact("c_mid2", "100.66.177.70:5000/mid2", "sha256:" + "2" * 64, "v1", platform="linux/arm64"),
                "c_mid1": ServiceImageArtifact("c_mid1", "100.66.177.70:5000/mid1", "sha256:" + "3" * 64, "v1", platform="linux/arm64"),
                "c_leaf": ServiceImageArtifact("c_leaf", "100.66.177.70:5000/leaf", "sha256:" + "4" * 64, "v1", platform="linux/amd64"),
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("c_leaf (linux/amd64) vs c_root (linux/arm64)", str(ctx.exception))

    def test_mismatch_at_intermediate_node1_of_deep_chain(self) -> None:
        """Verify mismatch at mid1 container (A:arm64 -> B:amd64 -> C:arm64 -> D:arm64) raises LumaError."""
        compose_yaml = """
services:
  c_root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/arm64
  c_mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/arm64
    network_mode: service:c_root
  c_mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/amd64
    network_mode: service:c_mid2
  c_leaf:
    image: 100.66.177.70:5000/leaf:v1
    platform: linux/arm64
    network_mode: service:c_mid1
"""
        art = BuildArtifact(
            artifact_id="art-mismatch-mid1",
            project="mismatch-mid1",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: mismatch-mid1\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={
                "c_root": ServiceImageArtifact("c_root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
                "c_mid2": ServiceImageArtifact("c_mid2", "100.66.177.70:5000/mid2", "sha256:" + "2" * 64, "v1", platform="linux/arm64"),
                "c_mid1": ServiceImageArtifact("c_mid1", "100.66.177.70:5000/mid1", "sha256:" + "3" * 64, "v1", platform="linux/amd64"),
                "c_leaf": ServiceImageArtifact("c_leaf", "100.66.177.70:5000/leaf", "sha256:" + "4" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("c_mid1 (linux/amd64) vs c_root (linux/arm64)", str(ctx.exception))

    def test_mismatch_at_intermediate_node2_of_deep_chain(self) -> None:
        """Verify mismatch at mid2 container (A:arm64 -> B:arm64 -> C:amd64 -> D:arm64) raises LumaError."""
        compose_yaml = """
services:
  c_root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/arm64
  c_mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/amd64
    network_mode: service:c_root
  c_mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/arm64
    network_mode: service:c_mid2
  c_leaf:
    image: 100.66.177.70:5000/leaf:v1
    platform: linux/arm64
    network_mode: service:c_mid1
"""
        art = BuildArtifact(
            artifact_id="art-mismatch-mid2",
            project="mismatch-mid2",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: mismatch-mid2\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={
                "c_root": ServiceImageArtifact("c_root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
                "c_mid2": ServiceImageArtifact("c_mid2", "100.66.177.70:5000/mid2", "sha256:" + "2" * 64, "v1", platform="linux/amd64"),
                "c_mid1": ServiceImageArtifact("c_mid1", "100.66.177.70:5000/mid1", "sha256:" + "3" * 64, "v1", platform="linux/arm64"),
                "c_leaf": ServiceImageArtifact("c_leaf", "100.66.177.70:5000/leaf", "sha256:" + "4" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("c_mid2 (linux/amd64) vs c_root (linux/arm64)", str(ctx.exception))

    def test_mismatch_at_root_of_deep_chain(self) -> None:
        """Verify mismatch when root is AMD64 and all sidecars are ARM64."""
        compose_yaml = """
services:
  c_root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/amd64
  c_mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/arm64
    network_mode: service:c_root
  c_mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/arm64
    network_mode: service:c_mid2
  c_leaf:
    image: 100.66.177.70:5000/leaf:v1
    platform: linux/arm64
    network_mode: service:c_mid1
"""
        art = BuildArtifact(
            artifact_id="art-mismatch-root",
            project="mismatch-root",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: mismatch-root\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={
                "c_root": ServiceImageArtifact("c_root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/amd64"),
                "c_mid2": ServiceImageArtifact("c_mid2", "100.66.177.70:5000/mid2", "sha256:" + "2" * 64, "v1", platform="linux/arm64"),
                "c_mid1": ServiceImageArtifact("c_mid1", "100.66.177.70:5000/mid1", "sha256:" + "3" * 64, "v1", platform="linux/arm64"),
                "c_leaf": ServiceImageArtifact("c_leaf", "100.66.177.70:5000/leaf", "sha256:" + "4" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("vs c_root (linux/amd64)", str(ctx.exception))

    def test_mismatch_in_diamond_tree_leaf(self) -> None:
        """Verify mismatch in a single leaf of diamond dependency tree raises LumaError."""
        compose_yaml = """
services:
  root:
    image: 100.66.177.70:5000/root:v1
    platform: linux/arm64
  mid1:
    image: 100.66.177.70:5000/mid1:v1
    platform: linux/arm64
    network_mode: service:root
  mid2:
    image: 100.66.177.70:5000/mid2:v1
    platform: linux/arm64
    network_mode: service:root
  leaf1:
    image: 100.66.177.70:5000/leaf1:v1
    platform: linux/arm64
    network_mode: service:mid1
  leaf2:
    image: 100.66.177.70:5000/leaf2:v1
    platform: linux/arm64
    network_mode: service:mid1
  leaf3:
    image: 100.66.177.70:5000/leaf3:v1
    platform: linux/arm64
    network_mode: service:mid2
  leaf4:
    image: 100.66.177.70:5000/leaf4:v1
    platform: linux/amd64
    network_mode: service:mid2
"""
        art = BuildArtifact(
            artifact_id="art-diamond-mismatch",
            project="diamond-mismatch",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: diamond-mismatch\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images={"root": ServiceImageArtifact("root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64")},
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("leaf4 (linux/amd64) vs root (linux/arm64)", str(ctx.exception))

    def test_single_mismatch_among_20_sidecars(self) -> None:
        """Verify that when 1 out of 20 sidecars is mismatched, it is identified and blocked."""
        services_dict = {
            "primary": {
                "image": "100.66.177.70:5000/primary:v1",
                "platform": "linux/arm64",
            }
        }
        images_dict = {
            "primary": ServiceImageArtifact("primary", "100.66.177.70:5000/primary", "sha256:" + "0" * 64, "v1", platform="linux/arm64")
        }
        for i in range(1, 21):
            name = f"sidecar_{i:02d}"
            arch = "linux/amd64" if i == 14 else "linux/arm64"
            services_dict[name] = {
                "image": f"100.66.177.70:5000/{name}:v1",
                "platform": arch,
                "network_mode": "service:primary",
            }
            images_dict[name] = ServiceImageArtifact(
                name, f"100.66.177.70:5000/{name}", f"sha256:{i:02d}" + "1" * 62, "v1", platform=arch
            )

        compose_yaml = yaml.dump({"services": services_dict})
        art = BuildArtifact(
            artifact_id="art-20-sidecars-mismatch",
            project="star-mismatch",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: star-mismatch\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images=images_dict,
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("sidecar_14 (linux/amd64) vs primary (linux/arm64)", str(ctx.exception))

    def test_inferred_node_architecture_mismatch_across_sidecars(self) -> None:
        """Verify sidecar node pin heuristic inference catches mismatch without explicit platform."""
        compose_yaml = """
services:
  root:
    image: 100.66.177.70:5000/root:v1
  sidecar:
    image: 100.66.177.70:5000/sidecar:v1
    network_mode: service:root
"""
        luma_yaml = """
name: inferred-mismatch
region: cn
services:
  root:
    node: k3s-arm64-worker-01
  sidecar:
    node: k3s-amd64-worker-01
"""
        art = BuildArtifact(
            artifact_id="art-inferred-mismatch",
            project="inferred-mismatch",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_yaml,
            compose_snapshot=compose_yaml,
            images={
                "root": ServiceImageArtifact("root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art)
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("sidecar (linux/amd64) vs root (linux/arm64)", str(ctx.exception))

    def test_node_architectures_mapping_mismatch_across_sidecars(self) -> None:
        """Verify sidecar node mapping inference catches mismatch against primary."""
        compose_yaml = """
services:
  root:
    image: 100.66.177.70:5000/root:v1
  sidecar:
    image: 100.66.177.70:5000/sidecar:v1
    network_mode: service:root
"""
        luma_yaml = """
name: mapping-mismatch
region: cn
services:
  root:
    node: node-arm
  sidecar:
    node: node-amd
"""
        art = BuildArtifact(
            artifact_id="art-mapping-mismatch",
            project="mapping-mismatch",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot=luma_yaml,
            compose_snapshot=compose_yaml,
            images={
                "root": ServiceImageArtifact("root", "100.66.177.70:5000/root", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_compose_release(art, node_architectures={"node-arm": "arm64", "node-amd": "amd64"})
        self.assertIn("cannot co-locate mixed-architecture containers in pod sidecar", str(ctx.exception))
        self.assertIn("sidecar (linux/amd64) vs root (linux/arm64)", str(ctx.exception))


class ArchitectureInferenceAndNodeSelectorStressTests(unittest.TestCase):
    """Stress tests architecture normalization, inference, and nodeSelector generation."""

    def test_platform_alias_normalization_arm64_matrix(self) -> None:
        """Verify all arm64 platform aliases normalize to kubernetes.io/arch: arm64 in nodeSelector."""
        arm_aliases = [
            "linux/arm64",
            "linux/aarch64",
            "linux/arm64/v8",
            "arm64",
            "aarch64",
            "ARM64",
            "AARCH64",
            "arm64/v8",
            "darwin/arm64",
        ]
        for alias in arm_aliases:
            with self.subTest(alias=alias):
                art = BuildArtifact(
                    artifact_id=f"art-arm-{alias.replace('/', '-')}",
                    project="arm-alias-app",
                    source_repo="local",
                    commit_sha="a" * 40,
                    branch="main",
                    manifest_kind="service",
                    manifest_snapshot="name: arm-alias-app\nregion: cn\nport: 8080\n",
                    images={
                        "arm-alias-app": ServiceImageArtifact(
                            "arm-alias-app", "100.66.177.70:5000/app", "sha256:" + "1" * 64, "v1", platform=alias
                        )
                    },
                    created_at=1700000000,
                )
                compiled = compile_service_release(art)
                selector = compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"]
                self.assertEqual(
                    selector.get("kubernetes.io/arch"),
                    "arm64",
                    f"Platform alias {alias!r} failed to emit 'arm64' in nodeSelector",
                )

    def test_platform_alias_normalization_amd64_matrix(self) -> None:
        """Verify all amd64 platform aliases normalize to kubernetes.io/arch: amd64 in nodeSelector."""
        amd_aliases = [
            "linux/amd64",
            "linux/x86_64",
            "amd64",
            "x86_64",
            "AMD64",
            "X86_64",
            "darwin/amd64",
        ]
        for alias in amd_aliases:
            with self.subTest(alias=alias):
                art = BuildArtifact(
                    artifact_id=f"art-amd-{alias.replace('/', '-')}",
                    project="amd-alias-app",
                    source_repo="local",
                    commit_sha="b" * 40,
                    branch="main",
                    manifest_kind="service",
                    manifest_snapshot="name: amd-alias-app\nregion: cn\nport: 8080\n",
                    images={
                        "amd-alias-app": ServiceImageArtifact(
                            "amd-alias-app", "100.66.177.70:5000/app", "sha256:" + "2" * 64, "v1", platform=alias
                        )
                    },
                    created_at=1700000000,
                )
                compiled = compile_service_release(art)
                selector = compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"]
                self.assertEqual(
                    selector.get("kubernetes.io/arch"),
                    "amd64",
                    f"Platform alias {alias!r} failed to emit 'amd64' in nodeSelector",
                )

    def test_invalid_and_unsupported_platform_matrix(self) -> None:
        """Verify invalid, multi-architecture, and unsupported platforms raise LumaError."""
        invalid_platforms = [
            "linux/mips64",
            "windows/amd64",
            "linux/riscv64",
            "linux/armv7l",
            "linux/amd64,linux/arm64",
            "arm64/v7",
            "amd64/v2",
        ]
        for p in invalid_platforms:
            with self.subTest(platform=p):
                with self.assertRaises(LumaError):
                    art = BuildArtifact(
                        artifact_id=f"art-inv-{p.replace('/', '-').replace(',', '-')}",
                        project="inv-app",
                        source_repo="local",
                        commit_sha="c" * 40,
                        branch="main",
                        manifest_kind="service",
                        manifest_snapshot="name: inv-app\nregion: cn\nport: 8080\n",
                        images={
                            "inv-app": ServiceImageArtifact(
                                "inv-app", "100.66.177.70:5000/app", "sha256:" + "3" * 64, "v1", platform=p
                            )
                        },
                        created_at=1700000000,
                    )
                    compile_service_release(art)

    def test_home_region_arm64_strict_affinity_and_rejections(self) -> None:
        """Verify region home strictly enforces arm64 and rejects AMD64."""
        # 1. Empty platform infers arm64 in home
        art_home_empty = BuildArtifact(
            artifact_id="art-home-empty",
            project="home-empty",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: home-empty\nregion: home\nport: 8080\n",
            images={
                "home-empty": ServiceImageArtifact(
                    "home-empty", "100.66.177.70:5000/app", "sha256:" + "4" * 64, "v1", platform="linux/arm64"
                )
            },
            created_at=1700000000,
        )
        compiled = compile_service_release(art_home_empty)
        self.assertEqual(
            compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

        # 2. Explicit AMD64 in home raises LumaError
        for amd_plat in ["linux/amd64", "amd64", "darwin/amd64"]:
            with self.subTest(platform=amd_plat):
                art_home_amd = BuildArtifact(
                    artifact_id=f"art-home-{amd_plat.replace('/', '-')}",
                    project="home-amd",
                    source_repo="local",
                    commit_sha="e" * 40,
                    branch="main",
                    manifest_kind="service",
                    manifest_snapshot="name: home-amd\nregion: home\nport: 8080\n",
                    images={
                        "home-amd": ServiceImageArtifact(
                            "home-amd", "100.66.177.70:5000/app", "sha256:" + "5" * 64, "v1", platform=amd_plat
                        )
                    },
                    created_at=1700000000,
                )
                with self.assertRaises(LumaError) as ctx:
                    compile_service_release(art_home_amd)
                self.assertIn("conflicts with region home (Apple Silicon home cluster requires arm64)", str(ctx.exception))

    def test_apple_silicon_node_pins_topology_conflicts(self) -> None:
        """Verify macbook-pro / air3 node pins in non-home regions raise topology conflicts."""
        for node in ["macbook-pro", "air3"]:
            for region in ["cn", "global"]:
                with self.subTest(node=node, region=region):
                    art = BuildArtifact(
                        artifact_id=f"art-topo-{node}-{region}",
                        project="topo-app",
                        source_repo="local",
                        commit_sha="f" * 40,
                        branch="main",
                        manifest_kind="service",
                        manifest_snapshot=f"name: topo-app\nregion: {region}\nnode: {node}\nport: 8080\n",
                        images={
                            "topo-app": ServiceImageArtifact(
                                "topo-app", "100.66.177.70:5000/app", "sha256:" + "6" * 64, "v1", platform="linux/arm64"
                            )
                        },
                        created_at=1700000000,
                    )
                    with self.assertRaises(LumaError) as ctx:
                        compile_service_release(art)
                    self.assertIn(f"node '{node}' belongs to region 'home'", str(ctx.exception))

    def test_node_name_heuristic_affinity(self) -> None:
        """Verify node name heuristic inference and conflict detection."""
        # 1. Matching heuristics
        arm_node_art = BuildArtifact(
            artifact_id="art-heur-arm",
            project="heur-arm",
            source_repo="local",
            commit_sha="a" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: heur-arm\nregion: cn\nnode: k3s-arm64-worker-01\nport: 8080\n",
            images={
                "heur-arm": ServiceImageArtifact(
                    "heur-arm", "100.66.177.70:5000/app", "sha256:" + "7" * 64, "v1", platform="linux/arm64"
                )
            },
            created_at=1700000000,
        )
        compiled_arm = compile_service_release(arm_node_art)
        self.assertEqual(
            compiled_arm.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

        # 2. Conflicting heuristic: ARM64 node with AMD64 image
        conflict_art = BuildArtifact(
            artifact_id="art-heur-conflict",
            project="heur-conflict",
            source_repo="local",
            commit_sha="b" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: heur-conflict\nregion: cn\nnode: k3s-arm64-worker-01\nport: 8080\n",
            images={
                "heur-conflict": ServiceImageArtifact(
                    "heur-conflict", "100.66.177.70:5000/app", "sha256:" + "8" * 64, "v1", platform="linux/amd64"
                )
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(conflict_art)
        self.assertIn("architecture mismatch", str(ctx.exception).lower())
        self.assertIn("k3s-arm64-worker-01 (arm64) does not match image platform linux/amd64 (amd64)", str(ctx.exception))

    def test_node_architectures_mapping_lookup_and_case_handling(self) -> None:
        """Verify node_architectures dictionary lookup and case normalization."""
        mapping = {"node-alpha": "arm64", "node-beta": "linux/amd64"}

        # Uppercase node pin query matches lowercase mapping key
        art_case = BuildArtifact(
            artifact_id="art-case-match",
            project="case-match",
            source_repo="local",
            commit_sha="c" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: case-match\nregion: cn\nnode: NODE-ALPHA\nport: 8080\n",
            images={
                "case-match": ServiceImageArtifact(
                    "case-match", "100.66.177.70:5000/app", "sha256:" + "9" * 64, "v1", platform="linux/arm64"
                )
            },
            created_at=1700000000,
        )
        compiled = compile_service_release(art_case, node_architectures=mapping)
        self.assertEqual(
            compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
            "arm64",
        )

        # Mismatch against mapping
        art_mismatch = BuildArtifact(
            artifact_id="art-case-mismatch",
            project="case-mismatch",
            source_repo="local",
            commit_sha="d" * 40,
            branch="main",
            manifest_kind="service",
            manifest_snapshot="name: case-mismatch\nregion: cn\nnode: NODE-BETA\nport: 8080\n",
            images={
                "case-mismatch": ServiceImageArtifact(
                    "case-mismatch", "100.66.177.70:5000/app", "sha256:" + "0" * 64, "v1", platform="linux/arm64"
                )
            },
            created_at=1700000000,
        )
        with self.assertRaises(LumaError) as ctx:
            compile_service_release(art_mismatch, node_architectures=mapping)
        self.assertIn("NODE-BETA (amd64) does not match image platform linux/arm64 (arm64)", str(ctx.exception))

    def test_neutral_node_name_allows_either_architecture(self) -> None:
        """Verify neutral node name (no arch clues, not in mapping) allows either architecture."""
        for plat, expected_arch in [("linux/arm64", "arm64"), ("linux/amd64", "amd64")]:
            with self.subTest(platform=plat):
                art = BuildArtifact(
                    artifact_id=f"art-neutral-{expected_arch}",
                    project="neutral-app",
                    source_repo="local",
                    commit_sha="e" * 40,
                    branch="main",
                    manifest_kind="service",
                    manifest_snapshot="name: neutral-app\nregion: cn\nnode: generic-worker-01\nport: 8080\n",
                    images={
                        "neutral-app": ServiceImageArtifact(
                            "neutral-app", "100.66.177.70:5000/app", "sha256:" + "1" * 64, "v1", platform=plat
                        )
                    },
                    created_at=1700000000,
                )
                compiled = compile_service_release(art)
                self.assertEqual(
                    compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"),
                    expected_arch,
                )
                self.assertEqual(
                    compiled.deployments[0]["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/hostname"),
                    "generic-worker-01",
                )

    def test_cross_architecture_independent_deployments_in_single_compose(self) -> None:
        """Verify Compose stack with independent ARM64 and AMD64 services compiles with independent nodeSelectors."""
        compose_yaml = """
services:
  arm_web:
    image: 100.66.177.70:5000/arm-web:v1
    platform: linux/arm64
  arm_sidecar:
    image: 100.66.177.70:5000/arm-sidecar:v1
    platform: linux/arm64
    network_mode: service:arm_web
  amd_worker:
    image: 100.66.177.70:5000/amd-worker:v1
    platform: linux/amd64
  amd_sidecar:
    image: 100.66.177.70:5000/amd-sidecar:v1
    platform: linux/amd64
    network_mode: service:amd_worker
"""
        images = {
            "arm_web": ServiceImageArtifact("arm_web", "100.66.177.70:5000/arm-web", "sha256:" + "1" * 64, "v1", platform="linux/arm64"),
            "arm_sidecar": ServiceImageArtifact("arm_sidecar", "100.66.177.70:5000/arm-sidecar", "sha256:" + "2" * 64, "v1", platform="linux/arm64"),
            "amd_worker": ServiceImageArtifact("amd_worker", "100.66.177.70:5000/amd-worker", "sha256:" + "3" * 64, "v1", platform="linux/amd64"),
            "amd_sidecar": ServiceImageArtifact("amd_sidecar", "100.66.177.70:5000/amd-sidecar", "sha256:" + "4" * 64, "v1", platform="linux/amd64"),
        }
        art = BuildArtifact(
            artifact_id="art-multi-arch-compose",
            project="multi-arch-app",
            source_repo="local",
            commit_sha="f" * 40,
            branch="main",
            manifest_kind="compose",
            manifest_snapshot="name: multi-arch-app\nregion: cn\n",
            compose_snapshot=compose_yaml,
            images=images,
            created_at=1700000000,
        )
        compiled = compile_compose_release(art)
        self.assertEqual(len(compiled.deployments), 2)
        deps_by_name = {d["metadata"]["name"]: d for d in compiled.deployments}

        # ARM64 Deployment
        arm_dep = deps_by_name["multi-arch-app-arm-web"]
        arm_containers = [c["name"] for c in arm_dep["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(len(arm_containers), 2)
        self.assertEqual(arm_dep["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"), "arm64")

        # AMD64 Deployment
        amd_dep = deps_by_name["multi-arch-app-amd-worker"]
        amd_containers = [c["name"] for c in amd_dep["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(len(amd_containers), 2)
        self.assertEqual(amd_dep["spec"]["template"]["spec"]["nodeSelector"].get("kubernetes.io/arch"), "amd64")


if __name__ == "__main__":
    unittest.main()
