from __future__ import annotations

import json
import os
import sqlite3
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
from luma.control import build_queue as queue
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
            "targetPlatformsByNode": {"air3": "linux/arm64"},
            "targetPlatformsByRegion": {"home": ["linux/amd64"], "render": ["linux/arm64"]},
        }

    def write_project(self, root, *, compose=False):
        root.mkdir()
        (root / "Dockerfile").write_text("FROM scratch\n")
        build = {"context": ".", "x-luma-git-contexts": {"extra": {"lock": "extra.lock.json"}}}
        if compose:
            (root / "luma.compose.yml").write_text("name: app\nregion: home\nservices:\n  web:\n    node: air3\n")
            (root / "docker-compose.yml").write_text(yaml.safe_dump({"services": {"web": {"build": build}, "db": {"image": "postgres:16"}}}))
        else:
            (root / ".luma.yml").write_text(yaml.safe_dump({"name": "app", "node": "air3", "region": "home", "build": build}))

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
        state["build"] = {"nodes": ["builder", "air3"], "defaultNode": "builder",
                          "registryHost": "100.64.0.70:5000", "pushHost": "100.64.0.70:5000"}
        state["nodes"] = {
            name: {"name": name, "region": region, "nomadStatus": "ready",
                   "agent": {"status": "ready", "os": os_name, "arch": arch, "capabilities": CAPABILITIES}}
            for name, region, os_name, arch in (("builder", "home", "linux", "amd64"), ("air3", "render", "darwin", "arm64"))
        }
        save_state(state)
        self.token = state["deployToken"]
        self.calls = []
        self.platforms = ["linux/arm64"]
        proxy = patch.object(srv, "_build_proxy_for_request", return_value="")
        proxy.start()
        self.addCleanup(proxy.stop)

    def submit(self, **kwargs):
        return srv.handle_build_deploy(self.token, {"repoUrl": "https://git.example.com/acme/app.git", "ref": "main", "queue": True, **kwargs})["buildRunId"]

    def task(self, state, node, action, payload, **kwargs):
        self.calls.append((node, payload))
        task_id = srv._queue_node_agent_task(state, node, action, payload,
                                           required_capability=kwargs["required_capability"], build_run_id=kwargs["build_run_id"])
        srv._mutate_control_state(lambda current: current["agentTasks"][task_id].update(status="succeeded"))
        if payload.get("planOnly"):
            return {"kind": "build-plan", "commit": COMMIT, "platforms": self.platforms, "buildCapabilities": [PLAN, NATIVE]}
        return {"kind": "service", "image": "100.64.0.70:5000/acme/app:aaaaaaa", "buildCapabilities": [PLAN, NATIVE],
                "manifest": "name: app\nregion: render\nexposure: none\n"}

    def execute(self, item):
        with patch.object(srv, "_run_node_agent_task", side_effect=self.task), patch.object(srv, "handle_deployment", return_value={"service": "app"}) as deploy:
            queue.execute(item)
        return deploy

    def test_arm_import_transfers_queue_and_pins_commit_without_leaking_secrets(self):
        build_id = self.submit(envSecrets={"PASSWORD": "private-value"})
        first = queue.claim()
        self.execute(first).assert_not_called()
        state = load_state()
        run = state["buildRuns"][build_id]
        self.assertEqual((run["status"], run["buildNode"]), ("queued", "air3"))
        self.assertNotIn("queueExecuting", run)
        self.assertEqual(state["buildQueue"][build_id]["body"]["envSecrets"], {"PASSWORD": "private-value"})
        self.assertNotIn("private-value", json.dumps(srv.handle_build_run_get(self.token, build_id)))
        queue.recover()
        second = queue.claim()
        deploy = self.execute(second)
        self.assertEqual([node for node, _ in self.calls], ["builder", "air3"])
        actual = self.calls[1][1]
        self.assertEqual(actual["ref"], COMMIT)
        self.assertEqual(actual["expectedCommit"], COMMIT)
        self.assertEqual(actual["nativePlatform"], "linux/arm64")
        self.assertEqual(actual["plannedPlatforms"], ["linux/arm64"])
        self.assertEqual(deploy.call_args.args[1]["envSecrets"], {"PASSWORD": "private-value"})
        self.assertEqual(deploy.call_args.args[1]["gitSource"]["buildNodeMode"], "auto")
        self.assertEqual(load_state()["buildRuns"][build_id]["status"], "succeeded")
        self.assertNotIn(build_id, load_state()["buildQueue"])

    def test_busy_or_offline_native_builder_waits_without_blocking_amd_builder(self):
        for offline in (False, True):
            with self.subTest(offline=offline):
                build_id = self.submit()
                self.execute(queue.claim())
                if offline:
                    srv._mutate_control_state(lambda state: state["nodes"]["air3"]["agent"].update(status="offline"))
                else:
                    srv._mutate_control_state(lambda state: state.setdefault("agentTasks", {}).update({"busy": {"id": "busy", "nodeName": "air3", "status": "running"}}))
                wait = queue.position(build_id)
                self.assertEqual(wait["waitReason"], "builder-offline" if offline else "builder")
                self.assertIsNone(queue.claim())
                other = srv._create_build_run({"ref": "main"}, source="https://git.example.com/acme/other.git", build_node="builder", project_key="acme/other", queued_work={"ref": "main"})
                self.assertEqual(queue.claim()[0], other)
                srv._complete_build_run(other, "failed")
                srv._mutate_control_state(lambda state: queue.discard(state, state["buildRuns"][other]))
                srv.handle_build_run_cancel(self.token, build_id)
                srv._mutate_control_state(lambda state: state["agentTasks"].pop("busy", None))
                srv._mutate_control_state(lambda state: state["nodes"]["air3"]["agent"].update(status="ready"))

    def test_cancel_during_transfer_does_not_resurrect_run(self):
        build_id = self.submit()
        item = queue.claim()

        def canceled_result(*_args, **_kwargs):
            srv.handle_build_run_cancel(self.token, build_id)
            return {"requeueBuild": {"node": "air3", "commit": COMMIT, "platforms": ["linux/arm64"]}}

        with patch.object(srv, "handle_build_deploy", side_effect=canceled_result):
            queue.execute(item)
        self.assertEqual(load_state()["buildRuns"][build_id]["status"], "canceled")
        self.assertNotIn(build_id, load_state()["buildQueue"])

    def test_transfer_receipt_retry_cannot_release_next_worker(self):
        build_id = self.submit()
        item = queue.claim()
        mutate = queue.mutate_state
        next_items = []

        def ambiguous_commit(fn, **kwargs):
            result = mutate(fn, **kwargs)
            if not next_items:
                next_items.append(queue.claim())
                raise sqlite3.OperationalError("database is locked")
            return result

        with patch.object(queue, "mutate_state", side_effect=ambiguous_commit), patch("luma.control.database_errors.time.sleep"):
            self.execute(item)
        run = load_state()["buildRuns"][build_id]
        self.assertEqual((run["status"], run["buildNode"]), ("running", "air3"))
        self.assertTrue(run["queueExecuting"])
        self.execute(next_items[0])
        self.assertEqual(load_state()["buildRuns"][build_id]["status"], "succeeded")

    def test_busy_default_uses_idle_planner_then_queues_amd_build_on_default(self):
        srv._mutate_control_state(lambda state: state.setdefault("agentTasks", {}).update({"busy": {"id": "busy", "nodeName": "builder", "status": "running"}}))
        self.platforms = ["linux/amd64"]
        build_id = self.submit()
        self.execute(queue.claim())
        self.assertEqual(self.calls[0][0], "air3")
        self.assertEqual(load_state()["buildRuns"][build_id]["buildNode"], "builder")
        self.assertEqual(queue.position(build_id)["waitReason"], "builder")
        self.assertIsNone(queue.claim())

    def test_explicit_builder_is_honored(self):
        self.platforms = ["linux/amd64"]
        build_id = self.submit(buildNode="builder")
        self.execute(queue.claim())
        self.execute(queue.claim())
        self.assertEqual(load_state()["buildRuns"][build_id]["status"], "succeeded")
        self.assertEqual([node for node, _ in self.calls], ["builder", "builder"])

    def test_explicit_amd_pin_is_rejected_after_planning_without_building(self):
        build_id = self.submit(buildNode="builder")
        self.execute(queue.claim()).assert_not_called()
        run = load_state()["buildRuns"][build_id]
        self.assertEqual(run["status"], "failed")
        self.assertIn("pinned builder builder", run["message"])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1]["planOnly"])

    def test_compatible_arm_pin_stays_pinned(self):
        build_id = self.submit(buildNode="air3")
        self.execute(queue.claim())
        self.execute(queue.claim())
        self.assertEqual(load_state()["buildRuns"][build_id]["status"], "succeeded")
        self.assertEqual([node for node, _ in self.calls], ["air3", "air3"])

    def test_mixed_platform_request_must_use_native_arm_builder(self):
        self.platforms = ["linux/amd64", "linux/arm64"]
        self.submit()
        self.execute(queue.claim())
        self.execute(queue.claim())
        self.assertEqual([node for node, _ in self.calls], ["builder", "air3"])
        self.assertEqual(self.calls[1][1]["nativePlatform"], "linux/arm64")
        self.assertEqual(self.calls[1][1]["plannedPlatforms"], ["linux/amd64", "linux/arm64"])

    def test_offline_native_candidate_is_selected_and_undeclared_nodes_are_not(self):
        state = load_state()
        state["nodes"]["air3"]["agent"]["status"] = "offline"
        self.assertEqual(srv._select_native_build_node(state, ["linux/arm64"], fallback="builder", required=[PLAN]), ("air3", True))
        state["build"]["nodes"] = ["builder"]
        with self.assertRaisesRegex(LumaError, "no declared native builder"):
            srv._select_native_build_node(state, ["linux/arm64"], fallback="builder", required=[PLAN])

    def test_old_agent_cannot_bypass_arm_policy_in_a_single_builder_cluster(self):
        def old_cluster(state):
            state["build"]["nodes"] = ["builder"]
            state["nodes"]["builder"]["agent"]["capabilities"] = ["docker-build", PLAN]
        srv._mutate_control_state(old_cluster)
        with self.assertRaisesRegex(LumaError, NATIVE):
            self.submit(buildNode="builder")
        self.assertFalse(load_state().get("buildRuns"))

    def test_direct_agent_queue_cannot_bypass_native_arm_capability(self):
        srv._mutate_control_state(lambda state: state["nodes"]["builder"]["agent"].update(capabilities=["docker-build"]))
        with self.assertRaisesRegex(LumaError, NATIVE):
            srv._queue_node_agent_task(load_state(), "builder", "build-image", {}, required_capability="docker-build")
        self.assertFalse(load_state().get("agentTasks"))

    def test_old_local_cli_is_blocked_before_arm_build_and_result_must_confirm_enforcement(self):
        body = {"repoUrl": "https://git.example.com/acme/local.git", "targetNode": "air3"}
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

    def test_old_planner_is_rejected_before_accepting_work(self):
        srv._mutate_control_state(lambda state: [node["agent"].update(capabilities=["docker-build"]) for node in state["nodes"].values()])
        with self.assertRaisesRegex(LumaError, "update the builder node agent"):
            self.submit()
        self.assertFalse(load_state().get("buildRuns"))

    def test_retry_workflow_keeps_auto_selection_and_explicit_pins_distinct(self):
        for mode in ("auto", "pinned"):
            with self.subTest(mode=mode):
                args = build_parser().parse_args(["--no-env", "build", "retry", "build-1"])
                args._workflow_retry_run = {"request": {"repoUrl": "https://git.example.com/acme/app.git",
                                                       "buildNode": "air3", "buildNodeMode": mode}}
                fields = comparison_fields(validate_recipe(make_recipe(args)))
                self.assertEqual(fields.get("build_node"), None if mode == "auto" else "air3")
