from __future__ import annotations

import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from luma import build_prerequisites as build
from luma.cli.nodes import cmd_node
from luma.errors import LumaError


class BuildPrerequisiteTests(unittest.TestCase):
    def test_join_installs_missing_tools_and_checks_again(self):
        with patch.object(build, "_missing_tools", side_effect=[["Buildx"], []]), \
             patch.object(build.sys, "platform", "linux"), \
             patch.object(build, "_install_linux") as install:
            build.prepare_build_dependencies()
        install.assert_called_once_with(["Buildx"])

    def test_failed_install_or_validation_never_reports_ready(self):
        for error in (None, LumaError("package manager failed")):
            with self.subTest(error=error), \
                 patch.object(build, "_missing_tools", return_value=["Buildx"]), \
                 patch.object(build.sys, "platform", "linux"), \
                 patch.object(build, "_install_linux", side_effect=error), \
                 self.assertRaises(LumaError):
                build.prepare_build_dependencies()

    def test_ready_host_does_not_install_or_restart_anything(self):
        with patch.object(build, "_missing_tools", return_value=[]), \
             patch.object(build, "_install_command") as install:
            build.prepare_build_dependencies()
        install.assert_not_called()

    def test_ubuntu_and_docker_ce_use_their_own_buildx_packages(self):
        for installed, package in ((False, "docker-buildx"), (True, "docker-buildx-plugin")):
            with self.subTest(installed=installed), \
                 patch.object(build.shutil, "which", return_value="/usr/bin/tool"), \
                 patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], 0 if installed else 1, "installed" if installed else "", "")), \
                 patch.object(build, "_apt_package_available", return_value=True), \
                 patch.object(build, "_install_command") as install:
                build._install_linux(["Buildx"])
            commands = [call.args[0] for call in install.call_args_list]
            self.assertEqual(commands[0], ["apt-get", "update"])
            self.assertEqual(commands[1][-1], package)
            self.assertIn("--no-remove", commands[1])
            self.assertNotIn("docker.io", commands[1])
            self.assertFalse(any("systemctl" in command for command in commands))

    def test_new_linux_host_installs_engine_git_and_buildx_then_starts_docker(self):
        with patch.object(build.shutil, "which", return_value="/usr/bin/tool"), \
             patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")), \
             patch.object(build, "_apt_package_available", return_value=True), \
             patch.object(build, "_install_command") as install:
            build._install_linux(["Git", "Docker", "Buildx"])
        self.assertEqual(install.call_args_list[1].args[0][-3:], ["git", "docker.io", "docker-buildx"])
        self.assertEqual(install.call_args_list[2].args[0], ["systemctl", "enable", "--now", "docker"])

    def test_macos_native_homebrew_covers_arm_intel_and_rosetta(self):
        for machine, translated, prefix in (("arm64", False, "/opt/homebrew"),
                                             ("x86_64", False, "/usr/local"),
                                             ("x86_64", True, "/opt/homebrew")):
            with self.subTest(machine=machine, translated=translated), \
                 patch.object(build.platform, "machine", return_value=machine), \
                 patch.object(build.os, "geteuid", return_value=501), \
                 patch.object(build.os, "access", return_value=True), \
                 patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], 0 if translated else 1, "1" if translated else "", "")), \
                 patch.object(build, "_install_command") as install:
                build._install_macos(["Git", "Buildx"])
            architecture = "-arm64" if prefix == "/opt/homebrew" else "-x86_64"
            install.assert_called_once_with(["/usr/bin/arch", architecture, prefix + "/bin/brew",
                                             "install", "git", "docker-buildx"])

    def test_macos_does_not_substitute_other_architecture_homebrew(self):
        with patch.object(build.platform, "machine", return_value="arm64"), \
             patch.object(build.os, "access", return_value=False), \
             self.assertRaisesRegex(LumaError, "/opt/homebrew"):
            build._native_brew()

    def test_probe_uses_private_build_config_and_cleans_it(self):
        captured = []

        def probe(argv, *, env=None):
            if "buildx" in argv:
                config = Path(env["DOCKER_CONFIG"])
                self.assertTrue((config / "config.json").is_file())
                self.assertEqual(env["DOCKER_HOST"], "unix:///native-docker.sock")
                captured.append(config)
            return True

        with patch.object(build.shutil, "which", return_value="/usr/bin/git"), \
             patch.object(build, "find_docker_binary", return_value="/usr/bin/docker"), \
             patch.object(build, "capture_docker_runtime", return_value={"dockerHost": "unix:///native-docker.sock"}), \
             patch.object(build, "_probe", side_effect=probe):
            self.assertEqual(build._missing_tools(), [])
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())

    def test_frontend_warmup_builds_only_scratch_on_native_engine_platform(self):
        for native in ("linux/amd64", "linux/arm64"):
            with self.subTest(native=native), \
                 patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                 contextlib.redirect_stderr(io.StringIO()):
                build._warm_frontend("docker", "luma-runner-egress", {"DOCKER_HOST": "unix:///engine"}, native)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--platform") + 1], native)
            self.assertIn("type=cacheonly", argv)
            self.assertNotIn("--push", argv)
            self.assertIn("# syntax=docker/dockerfile:1\nFROM scratch\n", run.call_args.kwargs["input"])

    def test_unreachable_frontend_fails_join_with_redacted_network_diagnostic(self):
        with patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, "registry-1.docker.io: connection refused via http://user:secret@proxy", "")), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(LumaError) as error:
            build._warm_frontend("docker", "luma-runner", {}, "linux/amd64")
        self.assertIn("join was not completed", str(error.exception))
        self.assertIn("BuildKit proxy", str(error.exception))
        self.assertNotIn("user:secret", str(error.exception))

    def test_runner_warmup_reuses_runtime_proxy_and_persistent_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = Mock(state_dir=Path(directory), docker_bin="docker", docker_runtime={},
                          local_proxy="http://proxy:7890", local_no_proxy="git.internal",
                          cache_keep_gb=25, max_parallelism=3, native_platform="linux/arm64")
            with patch.object(build, "_use_runner_buildx", return_value=contextlib.nullcontext("luma-runner-egress")) as builder, \
                 patch.object(build, "_warm_frontend") as warm:
                build.prepare_runner_buildkit(daemon)
            self.assertEqual(builder.call_args.kwargs["proxy"], "http://proxy:7890")
            self.assertIn("git.internal", builder.call_args.kwargs["no_proxy"])
            self.assertEqual(builder.call_args.kwargs["buildx_state_dir"], Path(directory) / "buildx")
            self.assertEqual(warm.call_args.args[1], "luma-runner-egress")
            warm_env = warm.call_args.args[2]
            self.assertEqual(warm_env["HTTP_PROXY"], "http://proxy:7890")
            self.assertEqual(warm_env["HTTPS_PROXY"], "http://proxy:7890")
            self.assertIn("git.internal", warm_env["NO_PROXY"])
            self.assertEqual(warm.call_args.args[3], "linux/arm64")


class NodeBuildGateTests(unittest.TestCase):
    def test_plain_nomad_node_join_skips_buildkit_warmup_and_rolls_back_on_nomad_failure(self):
        from types import SimpleNamespace
        args = SimpleNamespace(node_command="join", region="global", endpoint="https://control.test",
                               token="join-test", insecure=False, resolve_ip=None, name="test-node")
        client = Mock()
        client.register_node.return_value = {"nodeName": "test-node", "region": "global",
                                            "nomadRpcAddr": "100.64.0.1:4647", "egress": "proxy",
                                            "enrollmentToken": "enrollment-test"}
        with contextlib.ExitStack() as stack:
            for name in ("ensure_interactive_config", "configure_dns", "configure_journald_limit",
                         "install_docker", "prepare_build_dependencies", "setup_tailscale"):
                stack.enter_context(patch("luma.cli.nodes." + name, return_value=[]))
            stack.enter_context(patch("luma.cli.nodes._local_tailscale_ip", return_value="100.64.0.2"))
            stack.enter_context(patch("luma.cli.nodes._local_tailscale_connected", return_value=True))
            stack.enter_context(patch("luma.cli.common.ControlClient", return_value=client))
            warm = stack.enter_context(patch("luma.build_prerequisites.prepare_node_buildkit"))
            nomad = stack.enter_context(patch("luma.cli.nodes.install_nomad_node", side_effect=LumaError("nomad install failed")))
            agent = stack.enter_context(patch("luma.cli.nodes._install_node_agent_from_token"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(LumaError, "nomad install failed"):
                cmd_node(args)
        warm.assert_not_called()
        nomad.assert_called_once()
        client.unregister_node.assert_called_once_with(node_name="test-node", enrollment_token="enrollment-test")
        client.label_node.assert_not_called()
        agent.assert_not_called()
