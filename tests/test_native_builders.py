from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from luma import agent
from luma.cli import build_parser
from luma.build_capabilities import REPOSITORY_BUILD_PLAN_CAPABILITY as PLAN
from luma.build_capabilities import NATIVE_ARM_BUILD_CAPABILITY as NATIVE, native_arm_build_platform
from luma.control import server as srv
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError
from luma.deploy_workflow import comparison_fields, make_recipe, validate_recipe


COMMIT = "a" * 40
CAPABILITIES = ["docker-build", "repository-build-inputs-v1", PLAN, NATIVE]


class NativeBuildAgentTests(unittest.TestCase):
    def payload(self):
        return {
            "repoUrl": "https://git.example.com/acme/app.git", "repo": "acme/app",
            "registryHost": "100.64.0.70:5000", "pushHost": "100.64.0.70:5000",
            "targetPlatformsByNode": {"arm-3": "linux/arm64"},
            "targetPlatformsByRegion": {"home": ["linux/amd64"], "render": ["linux/arm64"]},
        }

    def write_project(self, root, *, compose=False):
        root.mkdir()
        (root / "Dockerfile").write_text("FROM scratch\n")
        build = {"context": ".", "x-luma-git-contexts": {"extra": {"lock": "extra.lock.json"}}}
        if compose:
            (root / "luma.compose.yml").write_text("name: app\nregion: home\nservices:\n  web:\n    node: arm-3\n")
            (root / "docker-compose.yml").write_text(yaml.safe_dump({"services": {"web": {"build": build}, "db": {"image": "postgres:16"}}}))
        else:
            (root / ".luma.yml").write_text(yaml.safe_dump({"name": "app", "node": "arm-3", "region": "home", "build": build}))

    def test_plan_resolves_repository_and_compose_placement_without_building_or_fetching_extra_sources(self):
        for compose in (False, True):
            with self.subTest(compose=compose):
                roots = []

                def clone(_url, root, **_kwargs):
                    roots.append(root)
                    self.write_project(root, compose=compose)

                with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="aaaaaaa"), patch(
                    "luma.gitops.head_commit_full", return_value=COMMIT
                ), patch.object(agent, "_docker_binary", return_value="docker"), patch.object(
                    agent, "_docker_buildx_available", return_value=True
                ), patch.object(agent, "_ensure_buildx_builder") as ensure, patch.object(
                    agent, "_docker_buildx_build"
                ) as build, patch("luma.build_sources.prepare_git_contexts") as sources:
                    result = agent.build_image({**self.payload(), "planOnly": True})
                self.assertEqual(result["kind"], "build-plan")
                self.assertEqual(result["platforms"], ["linux/arm64"])
                self.assertEqual(result["commit"], COMMIT)
                self.assertIn(PLAN, result["buildCapabilities"])
                ensure.assert_not_called()
                build.assert_not_called()
                sources.assert_not_called()
                self.assertFalse(roots[0].exists())

    def test_plan_uses_fallback_only_without_repository_manifest(self):
        def clone(_url, root, **_kwargs):
            root.mkdir()
            (root / "Dockerfile").write_text("FROM scratch\n")

        with patch("luma.gitops.clone", side_effect=clone), patch("luma.gitops.head_commit", return_value="aaaaaaa"), patch(
            "luma.gitops.head_commit_full", return_value=COMMIT
        ), patch.object(agent, "_docker_binary", return_value="docker"), patch.object(agent, "_docker_buildx_available", return_value=True):
            result = agent.build_image({**self.payload(), "planOnly": True, "fallbackPlacement": {"region": "render"}})
        self.assertEqual(result["platforms"], ["linux/arm64"])

    def test_commit_or_placement_drift_stops_before_buildkit(self):
        for expected_commit, platforms, error in (
            ("b" * 40, ["linux/arm64"], "commit changed"),
            (COMMIT, ["linux/amd64"], "architecture changed"),
        ):
            with self.subTest(error=error), patch("luma.gitops.clone", side_effect=lambda _url, root, **_kw: self.write_project(root)), patch(
                "luma.gitops.head_commit", return_value="aaaaaaa"
            ), patch("luma.gitops.head_commit_full", return_value=COMMIT), patch.object(
                agent, "_docker_binary", return_value="docker"
            ), patch.object(agent, "_docker_buildx_available", return_value=True), patch.object(
                agent, "_ensure_buildx_builder"
            ) as ensure, patch.object(agent, "_docker_buildx_build") as build:
                with self.assertRaisesRegex(LumaError, error):
                    agent.build_image({**self.payload(), "expectedCommit": expected_commit, "plannedPlatforms": platforms})
                ensure.assert_not_called()
                build.assert_not_called()

    def test_native_check_uses_docker_engine_not_emulated_buildx_platforms(self):
        with patch.object(agent.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="linux/aarch64\n")):
            agent._verify_native_build_platform("docker", "linux/arm64")
        with patch.object(agent.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="linux/x86_64\n")):
            with self.assertRaisesRegex(LumaError, "requires Docker engine linux/arm64"):
                agent._verify_native_build_platform("docker", "linux/arm64")

    def test_arm_build_cannot_start_on_an_emulating_builder_even_without_a_native_hint(self):
        for platform in ("linux/arm64", "linux/amd64,linux/arm64", "linux/arm64/v8"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                responses = [
                    SimpleNamespace(returncode=0, stdout="Driver: docker-container\nEndpoint: amd64-context\nPlatforms: linux/arm64, linux/amd64\n"),
                    SimpleNamespace(returncode=0, stdout="linux/x86_64\n"),
                ]
                with patch.object(agent.subprocess, "run", side_effect=responses) as run, patch.object(agent, "_run_process_streaming") as build:
                    with self.assertRaisesRegex(LumaError, "ARM builds cannot use emulation"):
                        agent._docker_buildx_build(docker="docker", builder="custom", docker_config=root,
                            push_host="registry:5000", registry_host="registry:5000", repo="test/app", sha="test",
                            context_dir=root, dockerfile_path=root / "Dockerfile", platform=platform, proxy="", build_timeout=60)
                    self.assertEqual(run.call_args.args[0][:3], ["docker", "--context", "amd64-context"])
                    build.assert_not_called()

    def test_native_worker_allows_mixed_build_and_unknown_or_emulating_workers_are_rejected(self):
        for driver, endpoints, rejected in (("docker-container", ["arm"], False),
                                            ("docker-container", ["arm", "amd"], True),
                                            ("remote", ["arm"], True)):
            with self.subTest(driver=driver, endpoints=endpoints):
                def run(command, **_kwargs):
                    if "inspect" in command:
                        text = f"Driver: {driver}\n" + "".join(f"Endpoint: {endpoint}\n" for endpoint in endpoints)
                    else:
                        text = "linux/aarch64" if command[2] == "arm" else "linux/x86_64"
                    return SimpleNamespace(returncode=0, stdout=text)
                with patch.object(agent.subprocess, "run", side_effect=run):
                    if rejected:
                        with self.assertRaises(LumaError):
                            agent._verify_native_arm_builder("docker", "custom", "linux/arm64")
                    else:
                        agent._verify_native_arm_builder("docker", "custom", "linux/arm64")

    def test_multiple_arm_architectures_cannot_fall_back_to_emulation(self):
        self.assertEqual(native_arm_build_platform(["linux/amd64", "linux/arm64/v8"]), "linux/arm64")
        self.assertEqual(native_arm_build_platform(["linux/arm/v7"]), "linux/arm/v7")
        with self.assertRaisesRegex(LumaError, "must be submitted separately"):
            native_arm_build_platform(["linux/arm/v7", "linux/arm64"])

    def test_mixed_plan_accepts_arm_native_engine_and_preserves_all_planned_platforms(self):
        platforms = ["linux/amd64", "linux/arm64"]
        self.assertIsNone(agent._repository_build_plan(
            {"nativePlatform": "linux/arm64", "plannedPlatforms": platforms}, Path("."), platforms))

    def test_orbstack_plugin_is_available_to_root_and_isolated_docker_config(self):
        directory = "/Applications/OrbStack.app/Contents/MacOS/xbin"
        with tempfile.TemporaryDirectory() as temp, patch.object(agent, "_BUILDX_AVAILABLE", None), patch.object(
            agent.shutil, "which", return_value=None
        ), patch.object(agent.os.path, "isfile", side_effect=lambda path: path == directory + "/docker-buildx"):
            self.assertIn(PLAN, agent.node_agent_capabilities("darwin"))
            config_dir = Path(temp) / "docker"
            agent._write_docker_auth_config(config_dir, None)
            config = json.loads((config_dir / "config.json").read_text())
            self.assertEqual(config, {"cliPluginsExtraDirs": [directory]})
            self.assertEqual((config_dir / "config.json").stat().st_mode & 0o777, 0o600)


class NativeBuildRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / "state"), LUMA_CONTROL_CONFIG=str(root / "luma.yaml"))
        env.start()
        self.addCleanup(env.stop)
        (root / "luma.yaml").write_text("providers: {}\n")
        state = init_state(domain="luma.example.com", cluster_id="native-test", overwrite=True)
        state["build"] = {"nodes": ["builder", "arm-3"], "defaultNode": "builder",
                          "registryHost": "100.64.0.70:5000", "pushHost": "100.64.0.70:5000"}
        state["nodes"] = {
            name: {"name": name, "region": region, "nomadStatus": "ready",
                   "agent": {"status": "ready", "os": os_name, "arch": arch, "capabilities": CAPABILITIES}}
            for name, region, os_name, arch in (("builder", "home", "linux", "amd64"), ("arm-3", "render", "darwin", "arm64"))
        }
        save_state(state)
        self.token = state["deployToken"]

    def test_offline_native_candidate_is_selected_and_undeclared_nodes_are_not(self):
        state = load_state()
        state["nodes"]["arm-3"]["agent"]["status"] = "offline"
        self.assertEqual(srv._select_native_build_node(state, ["linux/arm64"], fallback="builder", required=[PLAN]), ("arm-3", True))
        state["build"]["nodes"] = ["builder"]
        with self.assertRaisesRegex(LumaError, "no declared native builder"):
            srv._select_native_build_node(state, ["linux/arm64"], fallback="builder", required=[PLAN])

    def test_direct_agent_queue_cannot_bypass_native_arm_capability(self):
        srv._mutate_control_state(lambda state: state["nodes"]["builder"]["agent"].update(capabilities=["docker-build"]))
        with self.assertRaisesRegex(LumaError, NATIVE):
            srv._queue_node_agent_task(load_state(), "builder", "build-image", {}, required_capability="docker-build")
        self.assertFalse(load_state().get("agentTasks"))

    def test_old_local_cli_is_blocked_before_arm_build_and_result_must_confirm_enforcement(self):
        body = {"repoUrl": "https://git.example.com/acme/local.git", "targetNode": "arm-3"}
        with self.assertRaisesRegex(LumaError, "update it before building ARM"):
            srv.handle_local_build_prepare(self.token, body)
        self.assertFalse(load_state().get("buildRuns"))
        prepared = srv.handle_local_build_prepare(self.token, {**body, "buildCapabilities": [NATIVE]})
        self.assertEqual(prepared["upload"]["platform"], "linux/arm64")
        run = load_state()["buildRuns"][prepared["run"]["id"]]
        result = {"kind": "service", "image": prepared["upload"]["image"]}
        with self.assertRaisesRegex(LumaError, "Local builder did not confirm"):
            srv._validate_local_build_result(run, result)
        srv._validate_local_build_result(run, {**result, "buildCapabilities": [NATIVE]})

    def test_invalid_or_unversioned_plan_is_rejected(self):
        for result in ({"kind": "build-plan", "commit": COMMIT, "platforms": ["linux/arm64"]},
                       {"kind": "build-plan", "commit": "main", "platforms": ["linux/arm64"], "buildCapabilities": [PLAN]}):
            with self.subTest(result=result), self.assertRaises(LumaError):
                srv._checked_repository_build_plan(result)

    def test_legacy_remote_build_deploy_is_retired(self):
        with self.assertRaisesRegex(LumaError, "Legacy built-in remote build queue has been retired"):
            srv.handle_build_deploy(self.token, {"repoUrl": "https://git.example.com/acme/app.git", "ref": "main", "queue": True})
        self.assertFalse(load_state().get("buildRuns"))

    def test_retry_workflow_keeps_auto_selection_and_explicit_pins_distinct(self):
        for mode in ("auto", "pinned"):
            with self.subTest(mode=mode):
                args = build_parser().parse_args(["--no-env", "build", "retry", "build-1"])
                args._workflow_retry_run = {"request": {"repoUrl": "https://git.example.com/acme/app.git",
                                                       "buildNode": "arm-3", "buildNodeMode": mode}}
                fields = comparison_fields(validate_recipe(make_recipe(args)))
                self.assertEqual(fields.get("build_node"), None if mode == "auto" else "arm-3")
