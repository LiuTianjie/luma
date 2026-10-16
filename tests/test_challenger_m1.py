"""Adversarial stress-testing suite for Milestone 1 (M1) Build & Native Architecture.

Tests cover:
- Extreme / malformed inputs to validate_native_platform and infer_native_platform
- Builder concurrency saturation, queueing, superseding runs, and slot releasing
- Builder conflict rejection (explicit platform vs node or region)
- Terminal state mutations and idempotency
- Multi-builder sorting and load balancing
- Complex multi-branch saturation + superseding interactions
- Dockerfile mirror acceleration edge cases
- BuildKit waterfall parser resilience with malformed inputs
- Tekton manifest and YAML pipeline signal trapping invariants
"""
from __future__ import annotations

from pathlib import Path
import unittest
import yaml

from luma.build_capabilities import infer_native_platform, validate_native_platform
from luma.errors import LumaError
from luma.paas.build.controller import (
    BuildController,
    BuilderEndpoint,
    accelerate_dockerfile_mirrors,
    parse_buildkit_vertex_waterfall,
)


class ChallengerM1StressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.amd64_builder_0 = BuilderEndpoint(
            name="buildkit-amd64-0",
            address="tcp://buildkit-amd64-0.luma-builds.svc:1234",
            platform="linux/amd64",
            os_name="linux",
            max_concurrency=2,
            active_builds=0,
            cache_used_bytes=2000,
        )
        self.amd64_builder_1 = BuilderEndpoint(
            name="buildkit-amd64-1",
            address="tcp://buildkit-amd64-1.luma-builds.svc:1234",
            platform="linux/amd64",
            os_name="linux",
            max_concurrency=2,
            active_builds=0,
            cache_used_bytes=1000,
        )
        self.arm64_builder_0 = BuilderEndpoint(
            name="macbook-pro",
            address="tcp://100.64.0.12:1234",
            platform="linux/arm64",
            os_name="darwin",
            max_concurrency=2,
            active_builds=0,
            cache_used_bytes=500,
        )
        self.arm64_builder_1 = BuilderEndpoint(
            name="microos-arm64-0",
            address="tcp://100.64.0.15:1234",
            platform="linux/arm64",
            os_name="linux",
            max_concurrency=2,
            active_builds=0,
            cache_used_bytes=3000,
        )
        self.builders = [
            self.amd64_builder_0,
            self.amd64_builder_1,
            self.arm64_builder_0,
            self.arm64_builder_1,
        ]
        self.controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=self.builders,
        )

    # -------------------------------------------------------------------------
    # 1. Extreme & Malformed Inputs to validate_native_platform
    # -------------------------------------------------------------------------
    def test_validate_native_platform_malformed_and_extreme_inputs(self) -> None:
        # None, empty, whitespace
        for empty_val in [None, "", "   ", "\t\n\r", "   \n"]:
            with self.subTest(empty=empty_val):
                with self.assertRaisesRegex(LumaError, "native build platform is required"):
                    validate_native_platform(empty_val)  # type: ignore

        # Comma-separated multi-arch and QEMU attempts
        comma_cases = [
            "linux/amd64,linux/arm64",
            "linux/arm64,linux/amd64",
            ",linux/arm64",
            "linux/arm64,",
            ",",
            ",,",
            "linux/arm64, linux/amd64",
            "linux/arm64 , linux/amd64",
        ]
        for c in comma_cases:
            with self.subTest(comma=c):
                with self.assertRaisesRegex(LumaError, "cross-architecture emulation and multi-platform compilation are disabled"):
                    validate_native_platform(c)

        # Excessive slash components (> 3 components)
        excessive_cases = [
            "linux/arm64/v8/extra",
            "a/b/c/d",
            "linux/arm64/v8/extra/too/many",
        ]
        for ec in excessive_cases:
            with self.subTest(excessive=ec):
                with self.assertRaisesRegex(LumaError, "invalid platform format"):
                    validate_native_platform(ec)

        # Only slashes
        for only_slashes in ["/", "//", "///", "////"]:
            with self.subTest(slashes=only_slashes):
                with self.assertRaisesRegex(LumaError, "invalid platform format"):
                    validate_native_platform(only_slashes)

        # Unsupported Operating Systems
        unsupported_os = [
            "windows/amd64",
            "freebsd/amd64",
            "solaris/x86_64",
            "openbsd/arm64",
            "netbsd/amd64",
            "unknown/arm64",
        ]
        for uos in unsupported_os:
            with self.subTest(os=uos):
                with self.assertRaisesRegex(LumaError, "unsupported native platform"):
                    validate_native_platform(uos)

        # Unsupported Architectures
        unsupported_archs = [
            "linux/riscv64",
            "linux/mips",
            "linux/mips64",
            "linux/s390x",
            "linux/ppc64le",
            "linux/i386",
            "linux/386",
            "linux/arm",
            "linux/armv7l",
            "linux/armhf",
            "riscv64",
            "mips64",
        ]
        for uarch in unsupported_archs:
            with self.subTest(arch=uarch):
                with self.assertRaisesRegex(LumaError, "unsupported native architecture"):
                    validate_native_platform(uarch)

        # Unsupported Variants (with OS prefix)
        unsupported_variants = [
            "linux/arm64/v7",
            "linux/arm64/v9",
            "linux/arm64/v6",
            "linux/amd64/v2",
            "linux/amd64/v3",
            "linux/amd64/v8",
            "linux/x86_64/v2",
        ]
        for uvar in unsupported_variants:
            with self.subTest(variant=uvar):
                with self.assertRaisesRegex(LumaError, "unsupported architecture variant"):
                    validate_native_platform(uvar)

        # Two-part non-standard aliases (e.g. arm64/v7, arm64/v9 without os)
        # These are rejected by the parser as unsupported architecture
        for two_part in ["arm64/v7", "arm64/v9"]:
            with self.subTest(two_part=two_part):
                with self.assertRaisesRegex(LumaError, "unsupported native architecture"):
                    validate_native_platform(two_part)

        # Valid Aliases and Normalization
        valid_matrix = [
            ("linux/arm64", "linux/arm64"),
            ("linux/arm64/v8", "linux/arm64"),
            ("arm64", "linux/arm64"),
            ("arm64/v8", "linux/arm64"),
            ("aarch64", "linux/arm64"),
            ("aarch64/v8", "linux/arm64"),
            ("ARM64", "linux/arm64"),
            ("AARCH64", "linux/arm64"),
            ("LINUX/ARM64/V8", "linux/arm64"),
            ("  linux/arm64  ", "linux/arm64"),
            ("linux/amd64", "linux/amd64"),
            ("amd64", "linux/amd64"),
            ("x86_64", "linux/amd64"),
            ("AMD64", "linux/amd64"),
            ("X86_64", "linux/amd64"),
            ("linux/x86_64", "linux/amd64"),
            ("LINUX/X86_64", "linux/amd64"),
            ("  linux/amd64  ", "linux/amd64"),
            ("darwin/arm64", "darwin/arm64"),
            ("darwin/arm64/v8", "darwin/arm64"),
            ("darwin/aarch64", "darwin/arm64"),
            ("DARWIN/ARM64", "darwin/arm64"),
            ("darwin/amd64", "darwin/amd64"),
            ("darwin/x86_64", "darwin/amd64"),
            ("DARWIN/AMD64", "darwin/amd64"),
        ]
        for raw_input, expected in valid_matrix:
            with self.subTest(raw=raw_input):
                self.assertEqual(validate_native_platform(raw_input), expected)

    # -------------------------------------------------------------------------
    # 2. Extreme & Malformed Inputs to infer_native_platform
    # -------------------------------------------------------------------------
    def test_infer_native_platform_edge_cases(self) -> None:
        # 1. Non-empty platform always validates and overrides topology
        self.assertEqual(
            infer_native_platform(platform="arm64", target_region="us-east", target_node="amd-box"),
            "linux/arm64",
        )
        self.assertEqual(
            infer_native_platform(platform="linux/amd64", target_region="home", target_node="macbook-pro"),
            "linux/amd64",
        )
        with self.assertRaises(LumaError):
            infer_native_platform(platform="linux/riscv64", target_region="home")

        # 2. target_region == "home" (case-insensitive and trimmed)
        for home_reg in ["home", "HOME", "Home", "  home  "]:
            with self.subTest(reg=home_reg):
                self.assertEqual(infer_native_platform(target_region=home_reg), "linux/arm64")

        # 3. node_architectures mapping
        node_map = {
            "worker-arm": "linux/arm64",
            "worker-darwin-arm": "darwin/arm64",
            "worker-darwin-amd": "darwin/amd64",
            "worker-amd": "linux/amd64",
            "worker-alias": "aarch64",
        }
        self.assertEqual(
            infer_native_platform(target_node="worker-arm", node_architectures=node_map),
            "linux/arm64",
        )
        # darwin/arm64 mapped to linux/arm64 for container workloads
        self.assertEqual(
            infer_native_platform(target_node="worker-darwin-arm", node_architectures=node_map),
            "linux/arm64",
        )
        # darwin/amd64 mapped to linux/amd64 for container workloads
        self.assertEqual(
            infer_native_platform(target_node="worker-darwin-amd", node_architectures=node_map),
            "linux/amd64",
        )
        self.assertEqual(
            infer_native_platform(target_node="worker-alias", node_architectures=node_map),
            "linux/arm64",
        )
        # Case insensitive match in node_architectures
        self.assertEqual(
            infer_native_platform(target_node="WORKER-ARM", node_architectures=node_map),
            "linux/arm64",
        )

        # 4. Node name heuristics when not in node_architectures
        heuristics_arm = [
            "macbook-pro",
            "air3",
            "MACBOOK-PRO",
            "AIR3",
            "k3s-arm64-worker-1",
            "opensuse-microos-aarch64-node",
            "node-arm64",
            "node-aarch64",
        ]
        for node_name in heuristics_arm:
            with self.subTest(node=node_name):
                self.assertEqual(infer_native_platform(target_node=node_name), "linux/arm64")

        # 5. Default fallback to linux/amd64
        default_nodes = [
            "",
            "   ",
            "worker-1",
            "prod-k3s-agent-01",
            "node-x86_64",
            "generic-server",
        ]
        for def_node in default_nodes:
            with self.subTest(def_node=def_node):
                self.assertEqual(infer_native_platform(target_node=def_node), "linux/amd64")

    # -------------------------------------------------------------------------
    # 3. Builder Conflict Rejection & Platform Mismatch
    # -------------------------------------------------------------------------
    def test_builder_conflict_rejection(self) -> None:
        # Conflict: explicit AMD64 targeting home region
        for home_reg in ["home", "HOME", "Home", " home "]:
            with self.subTest(home_reg=home_reg):
                with self.assertRaisesRegex(LumaError, "conflicts with target region"):
                    self.controller.select_native_builder("linux/amd64", target_region=home_reg)

        # Conflict: explicit AMD64 targeting known ARM64 builder
        with self.assertRaisesRegex(LumaError, "conflicts with target node macbook-pro"):
            self.controller.select_native_builder("linux/amd64", target_node="macbook-pro")
        with self.assertRaisesRegex(LumaError, "conflicts with target node microos-arm64-0"):
            self.controller.select_native_builder("linux/amd64", target_node="microos-arm64-0")

        # Conflict: explicit AMD64 targeting known ARM64 heuristic node
        for arm_node in ["air3", "k3s-arm64-node", "my-aarch64-host"]:
            with self.subTest(arm_node=arm_node):
                with self.assertRaisesRegex(LumaError, f"conflicts with target node {arm_node}"):
                    self.controller.select_native_builder("linux/amd64", target_node=arm_node)

        # Conflict: explicit ARM64 targeting known AMD64 builder
        with self.assertRaisesRegex(LumaError, "conflicts with target node buildkit-amd64-0"):
            self.controller.select_native_builder("linux/arm64", target_node="buildkit-amd64-0")
        with self.assertRaisesRegex(LumaError, "conflicts with target node buildkit-amd64-1"):
            self.controller.select_native_builder("linux/arm64", target_node="buildkit-amd64-1")

        # Preferred node mismatch
        with self.assertRaisesRegex(LumaError, "does not match native target platform"):
            self.controller.select_native_builder("linux/arm64", preferred_node="buildkit-amd64-0")
        with self.assertRaisesRegex(LumaError, "does not match native target platform"):
            self.controller.select_native_builder("linux/amd64", preferred_node="macbook-pro")

        # Preferred node offline
        offline_controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[
                BuilderEndpoint(
                    name="offline-arm64",
                    address="tcp://100.64.0.99:1234",
                    platform="linux/arm64",
                    ready=False,
                )
            ],
        )
        with self.assertRaisesRegex(LumaError, "requested builder node is offline: offline-arm64"):
            offline_controller.select_native_builder("linux/arm64", preferred_node="offline-arm64")

        # Preferred node not registered
        with self.assertRaisesRegex(LumaError, "requested builder node is not registered: non-existent"):
            self.controller.select_native_builder("linux/arm64", preferred_node="non-existent")

        # Zero QEMU fallback: ARM requested but only AMD available
        amd_only_controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[self.amd64_builder_0, self.amd64_builder_1],
        )
        with self.assertRaisesRegex(LumaError, "no ready native BuildKit endpoint available for linux/arm64"):
            amd_only_controller.select_native_builder("linux/arm64")

        # Zero QEMU fallback: AMD requested but only ARM available
        arm_only_controller = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[self.arm64_builder_0, self.arm64_builder_1],
        )
        with self.assertRaisesRegex(LumaError, "no ready native BuildKit endpoint available for linux/amd64"):
            arm_only_controller.select_native_builder("linux/amd64")

    # -------------------------------------------------------------------------
    # 4. Builder Concurrency Saturation, Queueing & Slot Releasing
    # -------------------------------------------------------------------------
    def test_builder_concurrency_saturation_and_queueing(self) -> None:
        # Create a single builder with max_concurrency=2
        single_b = BuilderEndpoint(
            name="microos-arm64",
            address="tcp://100.64.0.15:1234",
            platform="linux/arm64",
            max_concurrency=2,
            active_builds=0,
        )
        ctrl = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[single_b],
        )

        # Submit 1st build on branch feat-1
        run1 = ctrl.submit_build(
            project="test-svc",
            repo_url="https://example.com/repo.git",
            revision="rev1",
            branch="feat-1",
            platform="linux/arm64",
        )
        self.assertEqual(run1["status"], "running")
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 1)
        self.assertEqual(b.available_slots, 1)

        # Submit 2nd build on branch feat-2
        run2 = ctrl.submit_build(
            project="test-svc",
            repo_url="https://example.com/repo.git",
            revision="rev2",
            branch="feat-2",
            platform="linux/arm64",
        )
        self.assertEqual(run2["status"], "running")
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 2)
        self.assertEqual(b.available_slots, 0)

        # Submit 3rd build on branch feat-3 (builder is saturated!)
        run3 = ctrl.submit_build(
            project="test-svc",
            repo_url="https://example.com/repo.git",
            revision="rev3",
            branch="feat-3",
            platform="linux/arm64",
        )
        self.assertEqual(run3["status"], "queued", "3rd build must be queued when slots are 0")
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 2, "Queued build must NOT claim an active slot")
        self.assertEqual(b.available_slots, 0)

        # Canceling the queued 3rd build must NOT decrement active_builds
        ctrl.cancel_build(run3["buildRunId"])
        self.assertEqual(ctrl.get_build(run3["buildRunId"])["status"], "canceled")
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 2, "Canceling queued build must not decrement active_builds")
        self.assertEqual(b.available_slots, 0)

        # Completing the 2nd build releases a slot
        ctrl.complete_build(run2["buildRunId"], status="succeeded")
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 1)
        self.assertEqual(b.available_slots, 1)

        # Completing the 1st build with status 'failed' also releases its slot
        ctrl.complete_build(run1["buildRunId"], status="failed")
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 0)
        self.assertEqual(b.available_slots, 2)

    # -------------------------------------------------------------------------
    # 5. Superseding Builds Lifecycle & Accounting
    # -------------------------------------------------------------------------
    def test_superseding_runs_and_accounting(self) -> None:
        single_b = BuilderEndpoint(
            name="macbook-pro",
            address="tcp://100.64.0.12:1234",
            platform="linux/arm64",
            max_concurrency=2,
            active_builds=0,
        )
        ctrl = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[single_b],
        )

        # Build 1 on main
        run1 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="commit-1",
            branch="main",
            platform="linux/arm64",
        )
        self.assertEqual(run1["status"], "running")
        self.assertEqual(ctrl.list_builders()[0].active_builds, 1)

        # Build 2 on main -> supersedes Build 1!
        run2 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="commit-2",
            branch="main",
            platform="linux/arm64",
        )
        self.assertEqual(run2["status"], "running")
        self.assertEqual(run2["supersededRuns"], [run1["buildRunId"]])

        # Verify Build 1 was canceled and terminated properly
        run1_updated = ctrl.get_build(run1["buildRunId"])
        self.assertEqual(run1_updated["status"], "canceled")
        self.assertEqual(run1_updated["cancelReason"], "Superseded by newer commit on main")
        self.assertEqual(run1_updated["pipelineRunManifest"]["spec"]["status"], "CancelledRunFinally")
        self.assertTrue(run1_updated["podTerminated"])
        self.assertTrue(run1_updated["buildkitSessionAborted"])

        # Crucial invariant: active_builds must remain 1 (slot released by run1, claimed by run2)
        b = ctrl.list_builders()[0]
        self.assertEqual(b.active_builds, 1, "active_builds must remain 1 after superseding run")
        self.assertEqual(b.available_slots, 1)

        # Build 3 on main -> supersedes Build 2!
        run3 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="commit-3",
            branch="main",
            platform="linux/arm64",
        )
        self.assertEqual(run3["status"], "running")
        self.assertEqual(run3["supersededRuns"], [run2["buildRunId"]])
        self.assertEqual(ctrl.list_builders()[0].active_builds, 1)

        # Completing Run 3 leaves active_builds at 0
        ctrl.complete_build(run3["buildRunId"], status="succeeded")
        self.assertEqual(ctrl.list_builders()[0].active_builds, 0)
        self.assertEqual(ctrl.list_builders()[0].available_slots, 2)

    # -------------------------------------------------------------------------
    # 6. Complex Multi-Branch Saturation & Superseding Interaction
    # -------------------------------------------------------------------------
    def test_multi_branch_saturation_and_superseding(self) -> None:
        single_b = BuilderEndpoint(
            name="microos-arm64",
            address="tcp://100.64.0.15:1234",
            platform="linux/arm64",
            max_concurrency=1,
            active_builds=0,
        )
        ctrl = BuildController(
            registry_host="100.66.177.70:5000",
            builders=[single_b],
        )

        # Branch 'main' build takes the only slot
        run_main_1 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="m1",
            branch="main",
            platform="linux/arm64",
        )
        self.assertEqual(run_main_1["status"], "running")
        self.assertEqual(ctrl.list_builders()[0].active_builds, 1)

        # Branch 'feat' build is queued because max_concurrency=1
        run_feat_1 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="f1",
            branch="feat",
            platform="linux/arm64",
        )
        self.assertEqual(run_feat_1["status"], "queued")
        self.assertEqual(ctrl.list_builders()[0].active_builds, 1)

        # Newer commit on branch 'feat' arrives -> supersedes queued run_feat_1
        run_feat_2 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="f2",
            branch="feat",
            platform="linux/arm64",
        )
        self.assertEqual(run_feat_2["status"], "queued")
        self.assertEqual(run_feat_2["supersededRuns"], [run_feat_1["buildRunId"]])
        self.assertEqual(ctrl.get_build(run_feat_1["buildRunId"])["status"], "canceled")
        # Invariant: active_builds must NOT decrement because run_feat_1 was queued!
        self.assertEqual(ctrl.list_builders()[0].active_builds, 1)

        # Newer commit on branch 'main' arrives -> supersedes running run_main_1!
        run_main_2 = ctrl.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="m2",
            branch="main",
            platform="linux/arm64",
        )
        self.assertEqual(run_main_2["status"], "running")
        self.assertEqual(ctrl.get_build(run_main_1["buildRunId"])["status"], "canceled")
        # Invariant: active_builds must remain exactly 1 (released by main_1, claimed by main_2)
        self.assertEqual(ctrl.list_builders()[0].active_builds, 1)

        # Complete main_2
        ctrl.complete_build(run_main_2["buildRunId"], status="succeeded")
        self.assertEqual(ctrl.list_builders()[0].active_builds, 0)
        self.assertEqual(ctrl.list_builders()[0].available_slots, 1)

    # -------------------------------------------------------------------------
    # 7. Idempotency & Terminal State Invariants
    # -------------------------------------------------------------------------
    def test_idempotency_and_terminal_state_invariants(self) -> None:
        run = self.controller.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="rev-1",
            branch="main",
            platform="linux/arm64",
            idempotency_key="idemp-key-12345",
        )
        b_before = next(b for b in self.controller.list_builders() if b.name == run["builderNode"])
        active_before = b_before.active_builds

        # Submitting again with identical idempotency_key returns identical record without new slot
        run_dup = self.controller.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="rev-1",
            branch="main",
            platform="linux/arm64",
            idempotency_key="idemp-key-12345",
        )
        self.assertEqual(run["buildRunId"], run_dup["buildRunId"])
        b_after = next(b for b in self.controller.list_builders() if b.name == run["builderNode"])
        self.assertEqual(b_after.active_builds, active_before)

        # Cancel build
        self.controller.cancel_build(run["buildRunId"])

        # Terminal state guards: double cancel
        with self.assertRaisesRegex(LumaError, "already in terminal state 'canceled'"):
            self.controller.cancel_build(run["buildRunId"])

        # Terminal state guards: complete on canceled
        with self.assertRaisesRegex(LumaError, "already in terminal state 'canceled'"):
            self.controller.complete_build(run["buildRunId"], status="succeeded")

        # Success followed by cancel
        run_succeed = self.controller.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="rev-2",
            branch="feat-succeed",
            platform="linux/arm64",
        )
        self.controller.complete_build(run_succeed["buildRunId"], status="succeeded")
        with self.assertRaisesRegex(LumaError, "already in terminal state 'succeeded'"):
            self.controller.cancel_build(run_succeed["buildRunId"])
        with self.assertRaisesRegex(LumaError, "already in terminal state 'succeeded'"):
            self.controller.complete_build(run_succeed["buildRunId"], status="succeeded")

        # Failure followed by cancel
        run_fail = self.controller.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="rev-3",
            branch="feat-fail",
            platform="linux/arm64",
        )
        self.controller.complete_build(run_fail["buildRunId"], status="failed")
        with self.assertRaisesRegex(LumaError, "already in terminal state 'failed'"):
            self.controller.cancel_build(run_fail["buildRunId"])

        # Invalid completion status
        run_inv = self.controller.submit_build(
            project="serverx",
            repo_url="https://example.com/serverx.git",
            revision="rev-4",
            branch="feat-inv",
            platform="linux/arm64",
        )
        with self.assertRaisesRegex(LumaError, "invalid completion status"):
            self.controller.complete_build(run_inv["buildRunId"], status="unknown")

    # -------------------------------------------------------------------------
    # 8. Multi-Builder Selection & Load Balancing
    # -------------------------------------------------------------------------
    def test_multi_builder_selection_and_load_balancing(self) -> None:
        # Two AMD64 builders:
        # buildkit-amd64-0: cache=2000, active=0, avail=2
        # buildkit-amd64-1: cache=1000, active=0, avail=2
        # With equal available slots, should pick builder with lower cache_used_bytes (buildkit-amd64-1)
        selected = self.controller.select_native_builder("linux/amd64")
        self.assertEqual(selected.name, "buildkit-amd64-1")

        # Submit a build on AMD64 -> claims slot on buildkit-amd64-1
        self.controller.submit_build(
            project="test-amd",
            repo_url="https://example.com/test.git",
            revision="rev-amd",
            branch="main",
            platform="linux/amd64",
        )
        # Now buildkit-amd64-1 has avail=1, while buildkit-amd64-0 has avail=2.
        # Next selection must pick buildkit-amd64-0 (more available slots: 2 > 1)
        selected2 = self.controller.select_native_builder("linux/amd64")
        self.assertEqual(selected2.name, "buildkit-amd64-0")

        # Specifying target_node takes absolute precedence even if other builder has more slots
        selected_pinned = self.controller.select_native_builder("linux/amd64", target_node="buildkit-amd64-1")
        self.assertEqual(selected_pinned.name, "buildkit-amd64-1")

    # -------------------------------------------------------------------------
    # 9. Dockerfile Mirror Acceleration Edge Cases
    # -------------------------------------------------------------------------
    def test_dockerfile_mirror_acceleration_edge_cases(self) -> None:
        # Empty string returns changed=False
        self.assertEqual(accelerate_dockerfile_mirrors(""), ("", False))
        # Note: None input coerces to "" but updated != dockerfile_text yields True ("" != None)
        self.assertEqual(accelerate_dockerfile_mirrors(None), ("", True))  # type: ignore

        # Already HTTPS
        already_https = "RUN sed -i 's|https://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list"
        text, changed = accelerate_dockerfile_mirrors(already_https)
        self.assertFalse(changed)
        self.assertEqual(text, already_https)

        # Multi-mirror mixture
        mixed = (
            "FROM debian:bookworm\n"
            "RUN echo http://deb.debian.org/debian && echo http://security.debian.org\n"
            "RUN echo http://archive.ubuntu.com/ubuntu && echo http://ports.ubuntu.com\n"
            "RUN echo http://dl-cdn.alpinelinux.org/alpine\n"
        )
        rewritten, changed = accelerate_dockerfile_mirrors(mixed)
        self.assertTrue(changed)
        self.assertNotIn("http://deb.debian.org", rewritten)
        self.assertNotIn("http://security.debian.org", rewritten)
        self.assertNotIn("http://archive.ubuntu.com", rewritten)
        self.assertNotIn("http://ports.ubuntu.com", rewritten)
        self.assertNotIn("http://dl-cdn.alpinelinux.org", rewritten)
        self.assertIn("https://deb.debian.org", rewritten)
        self.assertIn("https://security.debian.org", rewritten)
        self.assertIn("https://archive.ubuntu.com", rewritten)
        self.assertIn("https://ports.ubuntu.com", rewritten)
        self.assertIn("https://dl-cdn.alpinelinux.org", rewritten)

    # -------------------------------------------------------------------------
    # 10. BuildKit Vertex Waterfall Parser Resilience
    # -------------------------------------------------------------------------
    def test_buildkit_vertex_waterfall_parser_resilience(self) -> None:
        # Empty events
        stages, hit_ratio = parse_buildkit_vertex_waterfall([])
        self.assertEqual(stages, [])
        self.assertEqual(hit_ratio, 0.0)

        # Malformed non-mapping events
        stages, hit_ratio = parse_buildkit_vertex_waterfall(["not-a-dict", 123, None])  # type: ignore
        self.assertEqual(stages, [])
        self.assertEqual(hit_ratio, 0.0)

        # Partial event without durationMs but with timestamps
        events = [
            {
                "vertexes": [
                    {
                        "digest": "sha256:calc-duration",
                        "name": "step-calc",
                        "cached": True,
                        "started": 100,
                        "completed": 105,
                    }
                ]
            }
        ]
        stages, hit_ratio = parse_buildkit_vertex_waterfall(events)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].duration_ms, 5000)
        self.assertEqual(hit_ratio, 1.0)

    # -------------------------------------------------------------------------
    # 11. Tekton Manifest & YAML Pipeline Signal Trapping Invariants
    # -------------------------------------------------------------------------
    def test_tekton_manifest_and_yaml_invariants(self) -> None:
        # Verify submit_build manifest invariants
        run = self.controller.submit_build(
            project="test-proj",
            repo_url="https://example.com/test.git",
            revision="12345",
        )
        spec = run["pipelineRunManifest"]["spec"]
        self.assertEqual(spec["podTemplate"], {"terminationGracePeriodSeconds": 3})
        self.assertEqual(spec["timeouts"], {"pipeline": "45m", "tasks": "30m", "finally": "3m"})

        # Verify YAML structure
        tekton_yaml_path = Path("deploy/paas-stack/tekton/luma-build-pipeline.yaml")
        self.assertTrue(tekton_yaml_path.exists())
        with open(tekton_yaml_path, "r", encoding="utf-8") as f:
            docs = list(yaml.safe_load_all(f))

        tasks = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Task"}
        pipelines = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Pipeline"}

        # Task: buildkit build-push must trap SIGTERM and SIGINT
        build_task = tasks["luma-buildkit-build-push"]
        build_script = build_task["spec"]["steps"][0]["script"]
        self.assertIn("trap 'kill -TERM ${BUILDCTL_PID} 2>/dev/null || true' TERM INT", build_script)
        self.assertIn("wait ${BUILDCTL_PID}", build_script)
        self.assertIn("trap - TERM INT", build_script)

        # Task: cleanup finally task exists and removes sockets & workspaces
        self.assertIn("luma-build-cleanup-finally", tasks)
        cleanup_script = tasks["luma-build-cleanup-finally"]["spec"]["steps"][0]["script"]
        self.assertIn("/tmp/*.sock", cleanup_script)
        self.assertIn("/run/buildkit/*.sock", cleanup_script)
        self.assertIn("primary", cleanup_script)
        self.assertIn("contexts", cleanup_script)

        # Pipeline: finally block references cleanup task
        pipeline = pipelines["luma-paas-build-pipeline"]
        finally_tasks = pipeline["spec"].get("finally", [])
        self.assertTrue(any(t["name"] == "build-cleanup-finally" for t in finally_tasks))


if __name__ == "__main__":
    unittest.main()
