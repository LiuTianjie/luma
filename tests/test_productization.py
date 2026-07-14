import base64
import errno
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, Mock, call, patch

import yaml

from luma import __version__
from luma.agent import (
    _ContainerStatsSampler,
    _agent_executable_args,
    _agent_install_command,
    _agent_node_diagnostics,
    _complete_agent_task,
    _install_layout_from_executable,
    _node_tailscale_watchdog_install_command,
    _node_tailscale_watchdog_script,
    repair_nomad_cni_hostports,
    _systemd_unit,
    execute_agent_task,
    node_agent_container_stats,
    update_luma_install,
)
from luma.assets import asset_path, asset_text
from luma.config import LumaConfig
from luma.compose import load_compose_deployment
from luma.cloudflare import CloudflareClient, delete_dns, sync_control_dns
from luma.bootstrap import (
    _acme_email,
    _deploy_nomad_job,
    _ensure_control_image,
    _ensure_control_image_pull_egress,
    _ensure_control_registry_direct_route,
    _is_tailscale_manager_addr,
    _last_command_value,
    _nomad_tmpfs_compat_status,
    _parse_kernel_version,
    _parse_nomad_version,
    _resolve_control_image,
    _traefik_ports,
    bootstrap_node,
    bootstrap_manager_local,
    configure_firewall,
    configure_public_port_guards,
    configure_tailscale_watchdog,
    deploy_control_stack,
    install_control_config,
    _merge_control_config,
    install_docker,
    install_nomad_node,
    local_host_name,
    refresh_manager_control_local,
    setup_tailscale,
    sync_nomad_tailscale_service_metadata,
    verify_local_nomad_node,
)
from luma.control.client import ControlClient
from luma.control.context import load_current_context, save_context
from luma.control.server import ControlHandler, TAILSCALE_RELAY_RESOLVE_TIMEOUT_SECONDS, _DEPLOY_LOCK, _ensure_compose_exposure_supported_on_nodes, _node_record_for_name, _normalize_container_stats_for_engine, _run_host_prep_container, _tcp_relay_ports_needing_ingress_refresh, _service_stats_by_name, _state_nodes, ensure_image_present, ensure_image_pull_egress_proxy, ensure_image_pull_network, handle_application_restart, handle_certificate_retry, handle_compose_deployment, handle_compose_deployment_preview, handle_control_status, handle_dashboard, handle_dashboard_logs, handle_deployment, handle_deployment_config, handle_deployment_preview, handle_fleet_update, handle_git_provider_list, handle_git_provider_refs, handle_git_provider_remove, handle_git_provider_repositories, handle_git_provider_set, handle_node_agent_complete, handle_node_agent_lease, handle_node_agent_token, handle_node_label, handle_node_nomad_join, handle_node_register, handle_node_unregister, handle_registry_list, handle_registry_remove, handle_registry_set, handle_secret_list, handle_secret_set, handle_service_history, handle_service_pull_diagnostics, handle_service_remove, handle_service_rollback, handle_storage_apply, handle_storage_list, handle_storage_remove, handle_storage_set, image_pull_requires_egress, resolve_registry_image_digest, resolve_service_image, resolve_service_node_pin
from luma.compose import DEFAULT_NFS_MOUNT_OPTIONS
from luma.control.state import init_state, load_state, save_state
from luma.envfile import load_env_file
from luma.egress import ensure_mihomo_direct_domains, minimal_mihomo_config_from_bytes
from luma.errors import LumaError
from luma.local import LocalExecutor
from luma.profiles import PROFILES
from luma.registry import DEFAULT_DOCKER_REGISTRY, registry_host_from_image, registry_provider_type
from luma.service import ServiceSpec, load_service
from luma.cli import _node_join_examples, _run_with_wait_heartbeat, build_parser, exit_local_node, main
from luma.userconfig import configured_keys, ensure_interactive_config, load_user_config


class ProductConfigTests(unittest.TestCase):
    def test_version_files_stay_in_sync(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        init_file = (root / "luma" / "__init__.py").read_text(encoding="utf-8")
        asset_pyproject = (root / "luma" / "assets" / "pyproject.toml").read_text(encoding="utf-8")

        pyproject_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
        init_version = re.search(r'^__version__ = "([^"]+)"$', init_file, re.MULTILINE)
        asset_version = re.search(r'^version = "([^"]+)"$', asset_pyproject, re.MULTILINE)

        self.assertIsNotNone(pyproject_version)
        self.assertIsNotNone(init_version)
        self.assertIsNotNone(asset_version)
        self.assertEqual(pyproject_version.group(1), init_version.group(1))
        self.assertEqual(pyproject_version.group(1), asset_version.group(1))

    def test_pyproject_has_publish_metadata(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('name = "luma-infra"', pyproject)
        self.assertIn('luma = "luma.cli:main"', pyproject)
        self.assertIn("[project.urls]", pyproject)
        self.assertIn('license = "MIT"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)

    def test_python39_contract_does_not_use_dataclass_slots(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.9"', pyproject)

        incompatible = []
        for path in sorted((root / "luma").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if re.search(r"@dataclass\([^)]*\bslots\s*=", source):
                incompatible.append(str(path.relative_to(root)))
        self.assertEqual(incompatible, [], "dataclass slots require Python 3.10+: " + ", ".join(incompatible))

    def test_asset_pyproject_keeps_control_runtime_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        asset_pyproject = (root / "luma" / "assets" / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('"starlette>=0.49.0"', asset_pyproject)
        self.assertIn('"uvicorn[standard]>=0.38.0"', asset_pyproject)
        self.assertIn('"websockets>=15.0.1"', asset_pyproject)
        self.assertIn('"python-socks[asyncio]>=2.4.0"', asset_pyproject)

    def test_pty_session_emits_from_reader_thread_through_event_loop(self):
        from luma.agent import _PtySession

        session = _PtySession.__new__(_PtySession)
        session.loop = Mock()
        session.outbound = Mock()
        event = {"type": "output", "sessionId": "term-1", "data": "ok"}
        session._emit(event)
        session.loop.call_soon_threadsafe.assert_called_once_with(session.outbound.put_nowait, event)

    def test_pty_session_close_kills_process_group_after_timeout(self):
        import signal
        import subprocess
        from luma.agent import _PtySession

        session = _PtySession.__new__(_PtySession)
        session.closed = threading.Event()
        session.master_fd = 99
        session.process = Mock()
        session.process.pid = 1234
        session.process.poll.return_value = None
        session.process.wait.side_effect = [subprocess.TimeoutExpired("shell", 2), None]

        with patch("luma.agent.os.getpgid", return_value=9876), patch("luma.agent.os.killpg") as killpg, patch("luma.agent.os.close") as close:
            session.close()

        self.assertEqual(killpg.call_args_list[0].args, (9876, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (9876, signal.SIGKILL))
        close.assert_called_once_with(99)

    def test_terminal_supervisor_stop_kills_process_group_after_timeout(self):
        import signal
        import subprocess
        from luma.agent import _TerminalSupervisorProcess

        supervisor = _TerminalSupervisorProcess(Path("/tmp/luma-agent.json"))
        supervisor.process = Mock()
        supervisor.process.pid = 4321
        supervisor.process.poll.return_value = None
        supervisor.process.wait.side_effect = subprocess.TimeoutExpired("terminal-supervisor", 3)

        with patch("luma.agent.os.getpgid", return_value=8765), patch("luma.agent.os.killpg") as killpg:
            supervisor.stop()

        self.assertEqual(killpg.call_args_list[0].args, (8765, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (8765, signal.SIGKILL))
        supervisor.process.kill.assert_not_called()

    def test_terminal_supervisor_signal_requests_graceful_shutdown(self):
        import signal
        from luma.agent import _terminal_supervisor_shutdown_signal

        with self.assertRaises(KeyboardInterrupt):
            _terminal_supervisor_shutdown_signal(signal.SIGTERM, None)

    def test_terminal_supervisor_lock_is_singleton_per_node(self):
        from luma.agent import _acquire_terminal_supervisor_lock

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"nodeName": "home-mac-mini"}), encoding="utf-8")
            with patch("luma.agent.tempfile.gettempdir", return_value=tmp):
                first = _acquire_terminal_supervisor_lock(config)
                self.assertIsNotNone(first)
                try:
                    self.assertIsNone(_acquire_terminal_supervisor_lock(config))
                finally:
                    first.close()
                second = _acquire_terminal_supervisor_lock(config)
                self.assertIsNotNone(second)
                second.close()

    def test_node_agent_continues_after_transient_lease_failure(self):
        from luma.agent import run_node_agent

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(
                json.dumps({"endpoint": "https://luma.example.com", "token": "agent-token", "nodeName": "home-mac-mini"}),
                encoding="utf-8",
            )
            client = Mock()
            client.lease_agent_task.side_effect = [LumaError("temporary control failure"), {"task": {}}]
            stats_sampler = Mock()
            stats_sampler.snapshot.return_value = []
            terminal_supervisor = Mock()
            with patch("luma.control.client.ControlClient", return_value=client), patch(
                "luma.agent._ContainerStatsSampler", return_value=stats_sampler
            ), patch("luma.agent._TerminalSupervisorProcess", return_value=terminal_supervisor), patch(
                "luma.agent.node_agent_metrics", return_value={}
            ), patch("luma.agent.time.sleep", side_effect=[None, KeyboardInterrupt]), patch("sys.stderr"):
                with self.assertRaises(KeyboardInterrupt):
                    run_node_agent(config, poll_interval=1)

            self.assertEqual(client.lease_agent_task.call_count, 2)
            terminal_supervisor.stop.assert_called_once()
            stats_sampler.stop.assert_called_once()

    def test_node_agent_heartbeats_while_task_runs(self):
        client = Mock()
        task = {"id": "task-pull", "action": "diagnose-docker-pull", "payload": {"image": "registry.example.com/app:slow"}}
        heartbeat_seen = threading.Event()

        def fake_heartbeat_agent(**_kwargs):
            heartbeat_seen.set()
            return {"ok": True}

        def fake_execute_agent_task(_task, **_kwargs):
            heartbeat_seen.wait(0.2)
            return {"message": "pull diagnostic finished"}

        client.heartbeat_agent.side_effect = fake_heartbeat_agent
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 0.01}), encoding="utf-8")
            with patch("luma.agent.execute_agent_task", side_effect=fake_execute_agent_task), patch(
                "luma.agent.node_agent_os", return_value="linux"
            ), patch("luma.agent.node_agent_arch", return_value="x86_64"), patch(
                "luma.agent.node_agent_capabilities", return_value=["docker-image"]
            ), patch(
                "luma.agent.node_agent_metrics", return_value={"cpuPercent": 1.0}
            ):
                restart = _complete_agent_task(client, node_name="lab", node_id="node-1", task=task, config_path=config)

        self.assertFalse(restart)
        self.assertTrue(heartbeat_seen.is_set())
        client.heartbeat_agent.assert_called()
        client.complete_agent_task.assert_called_once_with(
            task_id="task-pull",
            node_name="lab",
            node_id="node-1",
            status="succeeded",
            message="pull diagnostic finished",
            result={"message": "pull diagnostic finished"},
        )

    def test_successful_task_report_failure_is_not_inverted_to_failed(self):
        # A task that executes successfully but whose SUCCESS report to Control
        # fails (network drop) must NOT be re-reported as "failed" — the host
        # mutation already happened. The report failure is swallowed+logged so
        # the poll loop survives, and no "failed" report is ever sent.
        client = Mock()
        client.complete_agent_task.side_effect = LumaError("control unreachable")
        task = {"id": "task-x", "action": "noop", "payload": {}}
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 0.01}), encoding="utf-8")
            with patch("luma.agent.execute_agent_task", return_value={"message": "done"}), patch(
                "luma.agent.node_agent_os", return_value="linux"
            ), patch("luma.agent.node_agent_arch", return_value="x86_64"), patch(
                "luma.agent.node_agent_capabilities", return_value=["docker-image"]
            ), patch("luma.agent.node_agent_metrics", return_value={}), patch("sys.stderr"):
                restart = _complete_agent_task(client, node_name="lab", node_id="node-1", task=task, config_path=config)

        self.assertFalse(restart)
        # Exactly one report attempt, with status "succeeded" — never inverted
        # to a "failed" report despite the reporting error.
        client.complete_agent_task.assert_called_once()
        self.assertEqual(client.complete_agent_task.call_args.kwargs["status"], "succeeded")

    def test_node_agent_batches_progress_without_blocking_task_output(self):
        client = Mock()
        task = {"id": "task-build", "action": "build-image", "payload": {}}

        def fake_execute_agent_task(_task, *, progress, **_kwargs):
            for index in range(250):
                progress({"type": "output", "line": f"build line {index}"})
            return {"message": "image built"}

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 60}), encoding="utf-8")
            with patch("luma.agent.execute_agent_task", side_effect=fake_execute_agent_task), patch(
                "luma.agent.node_agent_metrics", return_value={}
            ):
                restart = _complete_agent_task(
                    client,
                    node_name="builder",
                    node_id="builder-node",
                    task=task,
                    config_path=config,
                )

        self.assertFalse(restart)
        progress_calls = client.progress_agent_task.call_args_list
        self.assertEqual(len(progress_calls), 5)
        reported = [
            event["line"]
            for call in progress_calls
            for event in call.kwargs["events"]
        ]
        self.assertEqual(reported, [f"build line {index}" for index in range(250)])
        client.complete_agent_task.assert_called_once_with(
            task_id="task-build",
            node_name="builder",
            node_id="builder-node",
            status="succeeded",
            message="image built",
            result={"message": "image built"},
        )

    def test_progress_outage_does_not_block_terminal_task_result(self):
        client = Mock()
        client.progress_agent_task.side_effect = LumaError("control unavailable")

        def fake_execute_agent_task(_task, *, progress, **_kwargs):
            for index in range(250):
                progress({"type": "output", "line": f"build line {index}"})
            return {"message": "image built"}

        with patch("luma.agent.execute_agent_task", side_effect=fake_execute_agent_task), patch(
            "luma.agent.node_agent_metrics", return_value={}
        ), patch("sys.stderr"):
            restart = _complete_agent_task(
                client,
                node_name="builder",
                node_id="builder-node",
                task={"id": "task-build-outage", "action": "build-image", "payload": {}},
            )

        self.assertFalse(restart)
        self.assertEqual(client.progress_agent_task.call_count, 2)
        client.complete_agent_task.assert_called_once_with(
            task_id="task-build-outage",
            node_name="builder",
            node_id="builder-node",
            status="succeeded",
            message="image built",
            result={"message": "image built"},
        )

    def test_terminal_shell_prefers_zsh_on_macos(self):
        from luma.agent import _terminal_shell

        with patch.dict(os.environ, {"SHELL": ""}, clear=False), patch("luma.agent.node_agent_os", return_value="darwin"), patch(
            "luma.agent.Path.exists", return_value=True
        ):
            self.assertEqual(_terminal_shell(), "/bin/zsh")

    def test_doctor_checks_control_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "config"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.verify_login.return_value = {"clusterId": "luma-test"}
                client.status.return_value = {
                    "dns": {"ready": False, "missing": ["dns.token"]},
                    "nomad": {"available": True},
                    "portainer": {"ready": True},
                    "swarm": {"available": True, "nodes": []},
                    "nodes": {
                        "items": [
                            {
                                "name": "manager",
                                "agentStatus": "ready",
                                "diagnostics": {
                                    "docker": {
                                        "mirrors": [
                                            {"url": "https://bad.mirror", "ok": False, "message": "DNS lookup failed"},
                                            {"url": "https://ok.mirror", "ok": True, "message": "reachable"},
                                        ],
                                        "proxy": {"http": "", "https": "", "noProxy": "localhost,gcode.gaojiua.com"},
                                    },
                                    "nomad": {"dockerDriver": {"pullActivityTimeout": "30m"}},
                                    "recentImagePullErrors": ["image pull aborted due to inactivity"],
                                },
                            }
                        ]
                    },
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["--no-env", "doctor", "--deep"])

                self.assertEqual(code, 1)
                client.status.assert_called_once()
                output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("Control status: ok", output)
                self.assertIn("DNS readiness: fail", output)
                self.assertIn("Nomad readiness: ok", output)
                self.assertIn("Scheduler availability: ok", output)
                self.assertIn("Registered nodes: ok", output)
                self.assertIn("Node agent heartbeats: ok", output)
                self.assertIn("Node manager docker mirrors: fail", output)
                self.assertIn("bad mirror https://bad.mirror: DNS lookup failed", output)
                self.assertIn("Node manager Docker NO_PROXY: ok", output)
                self.assertIn("Node manager Nomad Docker pull timeout: ok", output)
                self.assertIn("Node manager recent image pulls: fail", output)
                self.assertIn("missing: dns.token", output)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_node_agent_unit_uses_python_module_when_invoked_from_stdin(self):
        with patch(
            "luma.agent._installed_luma_executable",
            return_value="/usr/bin/python3",
        ), patch("sys.argv", ["-"]):
            args = _agent_executable_args(Path("/opt/luma/node-agent/agent.json"))
            unit = _systemd_unit(Path("/opt/luma/node-agent/agent.json"))

        self.assertIn("-m", args)
        self.assertIn("luma.cli", args)
        self.assertNotIn("ExecStart=- ", unit)
        self.assertIn("node-agent run --config /opt/luma/node-agent/agent.json", unit)

    def test_node_agent_systemd_unit_pulls_up_nomad_and_keeps_retrying(self):
        unit = _systemd_unit(Path("/opt/luma/node-agent/agent.json"))

        self.assertIn("Wants=network-online.target docker.service nomad.service", unit)
        self.assertIn("After=network-online.target docker.service nomad.service", unit)
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertIn("EnvironmentFile=-/etc/default/luma-node-agent", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("RestartSec=5", unit)

    def test_node_agent_systemd_unit_preserves_running_install_layout(self):
        with patch(
            "luma.agent._installed_luma_executable",
            return_value="/home/tao/.local/bin/luma",
        ):
            unit = _systemd_unit(Path("/opt/luma/node-agent/agent.json"))

        self.assertIn(
            "ExecStart=/home/tao/.local/bin/luma node-agent run --config /opt/luma/node-agent/agent.json",
            unit,
        )
        self.assertNotIn("/root/.local/bin/luma", unit)

    def test_node_agent_install_includes_linux_tailscale_watchdog(self):
        command = _node_tailscale_watchdog_install_command("linux")

        self.assertIn("luma-node-tailscale-watchdog.service", command)
        self.assertIn("luma-node-tailscale-watchdog.timer", command)
        self.assertIn("systemctl restart tailscaled", command)
        self.assertIn("LUMA_NODE_TAILSCALE_WATCHDOG_PORTS:-4647", command)
        self.assertIn("tailscale ping --timeout=3s --c 2", command)
        self.assertNotIn("docker info --format", command)

    def test_node_agent_install_includes_macos_tailscale_watchdog(self):
        command = _node_tailscale_watchdog_install_command("darwin")

        self.assertIn("io.luma.tailscale-watchdog.plist", command)
        self.assertIn("StartInterval", command)
        self.assertIn("/opt/homebrew/bin:/usr/local/bin", command)
        self.assertIn("launchctl bootstrap system", command)
        self.assertIn("launchctl kickstart -k system/io.luma.tailscale-watchdog", command)
        self.assertIn("W5364U7YZB.io.tailscale.ipn.macsys.network-extension", command)

    def test_node_agent_install_command_installs_watchdog_after_agent(self):
        with patch("luma.agent.node_agent_os", return_value="linux"):
            command = _agent_install_command(Path("/opt/luma/node-agent/agent.json"))

        self.assertIn("luma-node-agent.service", command)
        self.assertIn("cp -a /etc/systemd/system/luma-node-agent.service", command)
        self.assertIn("systemctl restart luma-node-agent.service", command)
        self.assertIn("systemctl reset-failed luma-node-agent.service", command)
        self.assertIn("luma-node-tailscale-watchdog.timer", command)

    def test_node_agent_update_refreshes_linux_service_before_restart(self):
        executor = Mock()
        completed = Mock(returncode=0, stdout="installer ok\nLuma version: 0.1.222\n")
        with patch("luma.agent.subprocess.run", return_value=completed), patch("luma.agent.LocalExecutor", return_value=executor), patch(
            "luma.agent.node_agent_os", return_value="linux"
        ), patch("luma.agent._installed_luma_executable", return_value="/root/.local/bin/luma"):
            result = update_luma_install(install_ref="main", config_path=Path("/custom/agent.json"))

        self.assertTrue(result["restartAgent"])
        self.assertIn("node agent service refreshed", result["message"])
        service_command = executor.sudo.call_args_list[0].args[0]
        self.assertIn("/root/.local/bin/luma node-agent run --config /custom/agent.json", service_command)
        self.assertIn("cp -a /etc/systemd/system/luma-node-agent.service", service_command)
        self.assertIn("systemctl daemon-reload", service_command)
        self.assertIn("systemctl reset-failed luma-node-agent.service", service_command)
        self.assertNotIn("systemctl restart luma-node-agent.service", service_command)

    def test_node_agent_update_schedules_macos_launchd_reload(self):
        executor = Mock()
        completed = Mock(returncode=0, stdout="installer ok\nLuma version: 0.1.222\n")
        with patch("luma.agent.subprocess.run", return_value=completed), patch("luma.agent.LocalExecutor", return_value=executor), patch(
            "luma.agent.node_agent_os", return_value="darwin"
        ), patch("luma.agent._installed_luma_executable", return_value="/Users/tao/.local/bin/luma"):
            result = update_luma_install(install_ref="main", config_path=Path("/opt/luma/node-agent/agent.json"))

        self.assertFalse(result["restartAgent"])
        self.assertIn("node agent launchd reload scheduled", result["message"])
        service_command = executor.sudo.call_args_list[0].args[0]
        self.assertIn("sh -c", service_command)
        self.assertIn("( sleep ${LUMA_AGENT_RELOAD_DELAY_SECONDS:-20};", service_command)
        self.assertIn("/Users/tao/.local/bin/luma", service_command)
        self.assertIn("launchctl bootout system/io.luma.node-agent", service_command)

    def test_node_agent_update_reuses_current_user_install_layout_for_root_launchd(self):
        executor = Mock()
        completed = Mock(returncode=0, stdout="installer ok\nLuma version: 0.1.222\n")
        run = Mock(return_value=completed)
        with patch("luma.agent.subprocess.run", run), patch("luma.agent.LocalExecutor", return_value=executor), patch(
            "luma.agent.node_agent_os", return_value="darwin"
        ), patch("sys.argv", ["/Users/gaojiu/.local/share/luma/venv/bin/luma"]), patch.dict(
            os.environ, {"HOME": "/var/root", "LUMA_AGENT_EXECUTABLE": ""}, clear=False
        ):
            result = update_luma_install(install_ref="main", config_path=Path("/opt/luma/node-agent/agent.json"))

        self.assertFalse(result["restartAgent"])
        install_env = run.call_args.kwargs["env"]
        self.assertEqual(install_env["LUMA_USER_HOME"], "/Users/gaojiu")
        self.assertEqual(install_env["LUMA_INSTALL_HOME"], "/Users/gaojiu/.local/share/luma")
        self.assertEqual(install_env["LUMA_BIN_DIR"], "/Users/gaojiu/.local/bin")
        service_command = executor.sudo.call_args_list[0].args[0]
        self.assertIn("/Users/gaojiu/.local/bin/luma", service_command)
        self.assertNotIn("/var/root/.local/bin/luma", service_command)

    def test_install_layout_from_executable_supports_venv_and_shim_paths(self):
        venv_layout = _install_layout_from_executable("/Users/tao/.local/share/luma/venv/bin/luma")
        shim_layout = _install_layout_from_executable("/Users/tao/.local/bin/luma")

        self.assertEqual(tuple(str(part) for part in venv_layout or ()), ("/Users/tao", "/Users/tao/.local/share/luma", "/Users/tao/.local/bin"))
        self.assertEqual(tuple(str(part) for part in shim_layout or ()), ("/Users/tao", "/Users/tao/.local/share/luma", "/Users/tao/.local/bin"))

    def test_node_watchdog_script_checks_nomad_server_rpc(self):
        script = _node_tailscale_watchdog_script("linux")

        self.assertIn("retry_join", script)
        self.assertIn('PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"', script)
        self.assertIn("4647", script)
        self.assertIn("manager TCP probe failed", script)
        self.assertIn("manager Tailscale ping failed", script)
        self.assertIn("systemctl restart tailscaled", script)

    def test_node_agent_container_stats_parse_nomad_alloc_stats(self):
        allocation_id = "e3e43c17-59ac-1c40-1973-02ef2c05a4d2"
        ps = Mock(
            returncode=0,
            stdout=f"abc123\ttraefik-{allocation_id}\t{allocation_id}\t<no value>\t<no value>\t<no value>\n",
        )
        stats = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "ID": "abc123",
                    "Name": f"traefik-{allocation_id}",
                    "CPUPerc": "0.12%",
                    "MemUsage": "64MiB / 256MiB",
                    "MemPerc": "25.00%",
                }
            )
            + "\n",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"), patch("subprocess.run", side_effect=[ps, stats]):
            result = node_agent_container_stats()

        self.assertEqual(result[0]["service"], f"nomad:{allocation_id}")
        self.assertEqual(result[0]["nomadAllocId"], allocation_id)
        self.assertEqual(result[0]["containerId"], "abc123")
        self.assertEqual(result[0]["cpuPercent"], 0.12)
        self.assertEqual(result[0]["memoryUsageBytes"], 67108864)

    def test_nomad_container_stats_normalize_alloc_id_to_job_service(self):
        allocation_id = "e3e43c17-59ac-1c40-1973-02ef2c05a4d2"
        config = LumaConfig({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}, None)

        def request(_client, method, path, body=None):
            self.assertEqual((method, path), ("GET", "/v1/allocations"))
            return [
                {
                    "ID": allocation_id,
                    "JobID": "traefik",
                    "TaskGroup": "traefik",
                    "NodeID": "node-1",
                    "NodeName": "aly-host",
                    "TaskStates": {"traefik": {"State": "running"}},
                }
            ]

        with patch("luma.control.server.NomadApi.request", request):
            result = _normalize_container_stats_for_engine(
                [
                    {
                        "service": f"nomad:{allocation_id}",
                        "nomadAllocId": allocation_id,
                        "containerId": "abc123",
                        "cpuPercent": 0.12,
                        "memoryUsageBytes": 67108864,
                    }
                ],
                config=config,
                state={},
            )

        self.assertEqual(result[0]["service"], "traefik")
        self.assertEqual(result[0]["nomadAllocId"], allocation_id)
        self.assertEqual(result[0]["nomadTask"], "traefik")
        self.assertEqual(result[0]["nomadGroup"], "traefik")
        self.assertEqual(result[0]["nomadNode"], "aly-host")

    def test_service_stats_by_name_maps_nomad_alloc_stats_to_job(self):
        allocation_id = "e3e43c17-59ac-1c40-1973-02ef2c05a4d2"
        config = LumaConfig({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}, None)

        def request(_client, method, path, body=None):
            self.assertEqual((method, path), ("GET", "/v1/allocations"))
            return [{"ID": allocation_id, "JobID": "traefik", "TaskGroup": "traefik", "TaskStates": {"traefik": {"State": "running"}}}]

        with patch("luma.control.server.NomadApi.request", request):
            result = _service_stats_by_name(
                [
                    {
                        "name": "aly",
                        "containerStats": [
                            {
                                "service": f"nomad:{allocation_id}",
                                "nomadAllocId": allocation_id,
                                "containerId": "abc123",
                                "cpuPercent": 0.12,
                                "memoryUsageBytes": 67108864,
                            }
                        ],
                    }
                ],
                config=config,
                state={},
            )

        self.assertIn("traefik", result)
        self.assertEqual(result["traefik"][0]["node"], "aly")
        self.assertEqual(result["traefik"][0]["memoryUsageBytes"], 67108864)

    def test_node_agent_container_stats_sampler_snapshot_does_not_block_on_slow_docker(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_stats():
            entered.set()
            release.wait(timeout=2)
            return [{"service": "api_api", "containerId": "abc123", "cpuPercent": 8.5}]

        sampler = _ContainerStatsSampler(60, stats_func=slow_stats)
        try:
            sampler.start()
            self.assertTrue(entered.wait(timeout=1))
            started = time.monotonic()
            self.assertEqual(sampler.snapshot(), [])
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            release.set()
            sampler.stop()

    def test_node_agent_can_remove_managed_volume_path(self):
        with patch("luma.agent._run_fixed_host_task") as run:
            result = execute_agent_task(
                {
                    "action": "remove-managed-volume-path",
                    "payload": {"root": "/srv/luma", "relative": "nextcloud/nextcloud-db"},
                }
            )
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("/srv/luma/nextcloud/nextcloud-db", command)
        self.assertIn("rm -rf", command)
        self.assertEqual(result["path"], "/srv/luma/nextcloud/nextcloud-db")

    def test_node_agent_can_remove_docker_volume(self):
        with patch("luma.agent._run_fixed_host_task") as run:
            result = execute_agent_task({"action": "remove-docker-volume", "payload": {"name": "nextcloud_nextcloud-db"}})
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn('"$docker_cli" volume inspect nextcloud_nextcloud-db', command)
        self.assertIn('"$docker_cli" volume rm -f nextcloud_nextcloud-db', command)
        self.assertFalse(run.call_args.kwargs.get("prefer_container", True))
        self.assertEqual(result["name"], "nextcloud_nextcloud-db")

    def test_node_agent_can_resolve_docker_image_with_registry_auth(self):
        pull_result = Mock(code=0, output="pulled\n")
        inspect_result = Mock(code=0, output='["ghcr.io/acme/api@sha256:abc123"]\n')
        with patch("luma.agent.LocalExecutor") as executor:
            executor.return_value.run_result.side_effect = [pull_result, inspect_result]
            result = execute_agent_task(
                {
                    "action": "resolve-docker-image",
                    "payload": {
                        "image": "ghcr.io/acme/api:latest",
                        "platform": "linux/arm64",
                        "forcePull": True,
                        "registryAuth": {
                            "serveraddress": "ghcr.io",
                            "username": "octo",
                            "password": "secret",
                        },
                    },
                }
            )
        commands = "\n".join(call.args[0] for call in executor.return_value.run_result.call_args_list)
        self.assertIn("\"$docker_cli\" pull --platform linux/arm64 ghcr.io/acme/api:latest", commands)
        self.assertIn("DOCKER_CONFIG=", commands)
        self.assertNotIn("secret", commands)
        self.assertEqual(result["deployed"], "ghcr.io/acme/api@sha256:abc123")
        self.assertTrue(result["pulled"])

    def test_node_agent_can_diagnose_docker_pull_with_raw_output(self):
        pull_result = Mock(code=0, output="latest: Pulling from acme/api\n1a2b: Downloading\nDigest: sha256:abc123\n")
        with patch("luma.agent._run_command_streaming", return_value=pull_result) as run_streaming:
            result = execute_agent_task(
                {
                    "action": "diagnose-docker-pull",
                    "payload": {
                        "image": "ghcr.io/acme/api:latest",
                        "registryAuth": {
                            "serveraddress": "ghcr.io",
                            "username": "octo",
                            "password": "secret",
                        },
                    },
                }
            )
        command = run_streaming.call_args.args[0]
        self.assertIn("\"$docker_cli\" pull ghcr.io/acme/api:latest", command)
        self.assertIn("DOCKER_CONFIG=", command)
        self.assertNotIn("secret", command)
        self.assertTrue(result["ok"])
        self.assertEqual(result["image"], "ghcr.io/acme/api:latest")
        self.assertIn("1a2b: Downloading", result["output"])
        self.assertIn("Digest: sha256:abc123", result["lines"])

    def test_node_agent_docker_pull_diagnostic_emits_progress_lines(self):
        seen = []

        def fake_stream(_command, *, timeout, on_line=None):
            self.assertEqual(timeout, 600)
            self.assertIsNotNone(on_line)
            on_line("1a2b: Downloading")
            on_line("Digest: sha256:abc123")
            return Mock(code=0, output="1a2b: Downloading\nDigest: sha256:abc123\n")

        with patch("luma.agent._run_command_streaming", side_effect=fake_stream):
            result = execute_agent_task(
                {
                    "id": "task-1",
                    "action": "diagnose-docker-pull",
                    "payload": {"image": "ghcr.io/acme/api:latest"},
                },
                progress=lambda event: seen.append(event),
            )

        self.assertTrue(result["ok"])
        self.assertEqual([item["line"] for item in seen], ["1a2b: Downloading", "Digest: sha256:abc123"])
        self.assertTrue(all(item["type"] == "output" for item in seen))

    def test_node_agent_can_update_luma_install(self):
        completed = Mock(returncode=0, stdout="installed\nLuma version: 0.1.222\n")
        events = []
        with patch("luma.agent.subprocess.run", return_value=completed) as run, patch("luma.agent.LocalExecutor") as executor, patch(
            "luma.agent.node_agent_os", return_value="linux"
        ), patch("luma.agent._installed_luma_executable", return_value="/root/.local/bin/luma"):
            executor.return_value.sudo.return_value = ""
            result = execute_agent_task(
                {"action": "update-luma", "payload": {"installRef": "main"}},
                progress=events.append,
            )
        run.assert_called_once()
        self.assertEqual(executor.return_value.sudo.call_count, 2)
        self.assertIn("install-luma.sh", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["LUMA_INSTALL_REF"], "main")
        self.assertEqual(result["installRef"], "main")
        self.assertEqual(result["installedVersion"], "0.1.222")
        self.assertEqual(result["message"], "Luma installer finished; node agent service refreshed; Tailscale watchdog installed")
        self.assertTrue(result["restartAgent"])
        self.assertEqual(
            [event["line"] for event in events],
            [
                "Downloading and installing Luma main.",
                "Package installed; refreshing the node agent service definition.",
                "Node agent service refreshed; refreshing the node watchdog.",
                "Update prepared successfully; Tailscale watchdog installed.",
            ],
        )

    def test_node_agent_update_scopes_validated_proxy_to_installer_process(self):
        completed = Mock(returncode=0, stdout="Luma version: 0.1.235\n")
        with patch("luma.agent.subprocess.run", return_value=completed) as run, patch(
            "luma.agent.LocalExecutor"
        ) as executor, patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent._installed_luma_executable", return_value="/root/.local/bin/luma"
        ):
            executor.return_value.sudo.return_value = ""
            update_luma_install(
                install_ref="v0.1.235",
                proxy="http://100.106.154.3:7890",
            )

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["HTTP_PROXY"], "http://100.106.154.3:7890")
        self.assertEqual(env["HTTPS_PROXY"], "http://100.106.154.3:7890")
        self.assertEqual(env["http_proxy"], "http://100.106.154.3:7890")
        self.assertEqual(env["https_proxy"], "http://100.106.154.3:7890")
        self.assertIn("100.64.0.0/10", env["NO_PROXY"].split(","))
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])

    def test_node_agent_update_falls_back_directly_without_mutating_host_proxy(self):
        failed = Mock(returncode=56, stdout="curl: (56) Proxy CONNECT aborted\n")
        succeeded = Mock(returncode=0, stdout="Luma version: 0.1.236\n")
        events = []
        with patch("luma.agent.subprocess.run", side_effect=[failed, succeeded]) as run, patch(
            "luma.agent.LocalExecutor"
        ) as executor, patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent._installed_luma_executable", return_value="/root/.local/bin/luma"
        ), patch.dict(os.environ, {"ALL_PROXY": "socks5://127.0.0.1:1080"}, clear=False):
            executor.return_value.sudo.return_value = ""
            result = update_luma_install(
                install_ref="v0.1.236",
                proxy="http://100.106.154.3:7890",
                progress=events.append,
            )

        self.assertEqual(run.call_count, 2)
        first_env = run.call_args_list[0].kwargs["env"]
        direct_env = run.call_args_list[1].kwargs["env"]
        self.assertEqual(first_env["HTTPS_PROXY"], "http://100.106.154.3:7890")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            self.assertNotIn(key, direct_env)
        self.assertEqual(result["installedVersion"], "0.1.236")
        self.assertTrue(
            any(
                event["line"].startswith(
                    "Configured node egress failed; retrying the same exact Luma release"
                )
                for event in events
            )
        )

    def test_node_agent_update_rejects_credentialed_proxy(self):
        with patch("luma.agent.subprocess.run") as run:
            with self.assertRaisesRegex(LumaError, "without credentials"):
                update_luma_install(
                    install_ref="v0.1.235",
                    proxy="http://user:secret@100.106.154.3:7890",
                )
        run.assert_not_called()

    def test_node_agent_update_rejects_installer_without_version_proof(self):
        completed = Mock(returncode=0, stdout="installer completed without version\n")
        with patch("luma.agent.subprocess.run", return_value=completed), patch(
            "luma.agent.LocalExecutor"
        ) as executor:
            with self.assertRaisesRegex(LumaError, "without reporting the installed version"):
                update_luma_install(install_ref="bad-ref")
        executor.assert_not_called()

    def test_node_agent_can_join_nomad_node(self):
        with patch("luma.bootstrap.install_nomad_node", return_value=["Nomad agent ready"]) as install_nomad, patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("bot-host", "nomad-node-id")
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.80.0.20"):
            result = execute_agent_task(
                {
                    "action": "join-nomad",
                    "payload": {
                        "nodeName": "bot",
                        "region": "global",
                        "serverAddr": "100.113.204.125:4647",
                        "tailscaleAuthKey": "ts-key",
                    },
                }
            )

        install_nomad.assert_called_once()
        install_kwargs = install_nomad.call_args.kwargs
        self.assertEqual(install_kwargs["role"], "client")
        self.assertEqual(install_kwargs["region"], "global")
        self.assertEqual(install_kwargs["node_name"], "bot")
        self.assertEqual(install_kwargs["server_addrs"], ["100.113.204.125:4647"])
        self.assertEqual(install_kwargs["tailscale_authkey"], "ts-key")
        self.assertEqual(result["nodeName"], "bot-host")
        self.assertEqual(result["nomadNodeId"], "nomad-node-id")
        self.assertEqual(result["tailscaleIP"], "100.80.0.20")

    def test_node_agent_update_task_requests_restart_after_completion(self):
        client = Mock()
        task = {"id": "task-1", "action": "update-luma", "payload": {}}
        with patch("luma.agent.execute_agent_task", return_value={"message": "updated", "restartAgent": True}):
            restart = _complete_agent_task(client, node_name="aly", node_id="node-1", task=task)

        self.assertTrue(restart)
        client.complete_agent_task.assert_called_once_with(
            task_id="task-1",
            node_name="aly",
            node_id="node-1",
            status="succeeded",
            message="updated",
            result={"message": "updated", "restartAgent": True},
        )

    def test_node_agent_capabilities_include_fleet_update_and_terminal(self):
        from luma.agent import node_agent_capabilities

        self.assertIn("luma-update", node_agent_capabilities("linux"))
        self.assertIn("luma-update", node_agent_capabilities("darwin"))
        self.assertIn("luma-update-proxy-v1", node_agent_capabilities("linux"))
        self.assertIn("luma-update-proxy-v1", node_agent_capabilities("darwin"))
        self.assertIn("luma-update-egress-fallback-v1", node_agent_capabilities("linux"))
        self.assertIn("luma-update-egress-fallback-v1", node_agent_capabilities("darwin"))
        self.assertIn("docker-image", node_agent_capabilities("linux"))
        self.assertIn("docker-image", node_agent_capabilities("darwin"))
        self.assertIn("nomad-join", node_agent_capabilities("linux"))
        self.assertIn("nomad-join", node_agent_capabilities("darwin"))
        self.assertIn("nomad-cni-repair", node_agent_capabilities("linux"))
        self.assertNotIn("nomad-cni-repair", node_agent_capabilities("darwin"))
        self.assertIn("terminal", node_agent_capabilities("linux"))
        self.assertIn("terminal", node_agent_capabilities("darwin"))
        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"):
            self.assertIn("control-image-mirror-v1", node_agent_capabilities("linux"))
            self.assertIn("system-image-mirror-v1", node_agent_capabilities("linux"))
            self.assertNotIn("control-image-mirror-v1", node_agent_capabilities("darwin"))
            self.assertNotIn("system-image-mirror-v1", node_agent_capabilities("darwin"))

    def test_node_agent_diagnostics_reports_docker_mirrors_proxy_nomad_and_pull_errors(self):
        executor = Mock()
        executor.run_result.side_effect = [
            Mock(code=0, output='["https://bad.mirror","https://ok.mirror"]\n'),
            Mock(code=0, output='{"url":"https://bad.mirror","ok":false,"message":"DNS lookup failed"}\n'),
            Mock(code=0, output='{"url":"https://ok.mirror","ok":true,"message":"reachable"}\n'),
            Mock(code=0, output="HTTPProxy=http://proxy:7890 HTTPSProxy=http://proxy:7890 NoProxy=localhost,gcode.gaojiua.com\n"),
            Mock(code=0, output='pull_activity_timeout = "30m"\n'),
            Mock(code=0, output="Jul 04 nomad[1]: image pull aborted due to inactivity\nJul 04 nomad[1]: context canceled\n"),
            Mock(
                code=0,
                output=(
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"old-alloc\\"" '
                    "-m multiport --dports 14173 -j CNI-DN-old\n"
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"current-alloc\\"" '
                    "-m multiport --dports 14173 -j CNI-DN-current\n"
                ),
            ),
            Mock(
                code=0,
                output=(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                    "\t/nomad_init_current\tcurrent-alloc\tlo\n"
                ),
            ),
        ]

        with patch("luma.agent.node_agent_os", return_value="linux"):
            diagnostics = _agent_node_diagnostics(executor=executor)

        self.assertEqual(diagnostics["docker"]["mirrors"][0]["url"], "https://bad.mirror")
        self.assertFalse(diagnostics["docker"]["mirrors"][0]["ok"])
        self.assertEqual(diagnostics["docker"]["mirrors"][1]["url"], "https://ok.mirror")
        self.assertTrue(diagnostics["docker"]["mirrors"][1]["ok"])
        self.assertEqual(diagnostics["docker"]["proxy"]["http"], "http://proxy:7890")
        self.assertEqual(diagnostics["nomad"]["dockerDriver"]["pullActivityTimeout"], "30m")
        self.assertEqual(
            diagnostics["nomad"]["cniHostPorts"]["conflicts"],
            [
                {
                    "protocol": "tcp",
                    "port": "14173",
                    "allocIds": ["old-alloc", "current-alloc"],
                    "shadowedAllocIds": ["current-alloc"],
                    "ruleCount": 2,
                }
            ],
        )
        self.assertEqual(
            diagnostics["nomad"]["cniHostPorts"]["missingNetworks"],
            [
                {
                    "allocId": "current-alloc",
                    "container": "0123456789ab",
                    "name": "nomad_init_current",
                    "interfaces": ["lo"],
                }
            ],
        )
        self.assertIn("image pull aborted due to inactivity", diagnostics["recentImagePullErrors"][0])

    def test_parse_nomad_cni_missing_networks_only_reports_loopback_only_init_containers(self):
        from luma.agent import _parse_nomad_cni_missing_networks

        output = (
            "aaaaaaaaaaaaaaaaaaaaaaaa\t/nomad_init_broken\talloc-broken\tlo\n"
            "bbbbbbbbbbbbbbbbbbbbbbbb\t/nomad_init_healthy\talloc-healthy\tlo,eth0\n"
            "cccccccccccccccccccccccc\t/not_nomad_init\talloc-other\tlo\n"
            "not-a-container-id\t/nomad_init_invalid\talloc-invalid\tlo\n"
            "dddddddddddddddddddddddd\t/nomad_init_unsafe\talloc unsafe\tlo\n"
            "eeeeeeeeeeeeeeeeeeeeeeee\t/nomad_init_unknown\talloc-unknown\t\n"
        )

        self.assertEqual(
            _parse_nomad_cni_missing_networks(output),
            [
                {
                    "allocId": "alloc-broken",
                    "container": "aaaaaaaaaaaa",
                    "name": "nomad_init_broken",
                    "interfaces": ["lo"],
                }
            ],
        )

    def test_diagnostic_nomad_cni_missing_networks_is_empty_without_init_containers_or_tools(self):
        from luma.agent import _diagnostic_nomad_cni_hostports

        no_init_executor = Mock()
        no_init_executor.run_result.side_effect = [
            Mock(code=0, output=""),
            Mock(code=0, output=""),
        ]
        with patch("luma.agent.node_agent_os", return_value="linux"):
            no_init = _diagnostic_nomad_cni_hostports(no_init_executor)
        self.assertEqual(no_init, {"conflicts": [], "missingNetworks": []})

        unavailable_executor = Mock()
        unavailable_executor.run_result.side_effect = [
            Mock(code=0, output=""),
            Mock(code=1, output="docker command not found"),
        ]
        with patch("luma.agent.node_agent_os", return_value="linux"):
            unavailable = _diagnostic_nomad_cni_hostports(unavailable_executor)
        self.assertEqual(unavailable, {"conflicts": [], "missingNetworks": []})

    def test_diagnostic_nomad_cni_missing_networks_is_not_applicable_on_macos(self):
        from luma.agent import _diagnostic_nomad_cni_hostports

        executor = Mock()
        with patch("luma.agent.node_agent_os", return_value="darwin"):
            result = _diagnostic_nomad_cni_hostports(executor)

        self.assertEqual(result, {"conflicts": [], "missingNetworks": []})
        executor.run_result.assert_not_called()

    def test_doctor_deep_rejects_duplicate_cni_hostport_rules(self):
        old_home = _set_env("LUMA_CONFIG_HOME", tempfile.mkdtemp())
        try:
            save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="token")
            client = Mock()
            client.verify.return_value = {"clusterId": "luma-test"}
            client.status.return_value = {
                "cluster": {"id": "luma-test"},
                "nomad": {"available": True},
                "nodes": {
                    "items": [
                        {
                            "name": "blg",
                            "agentStatus": "ready",
                            "diagnostics": {
                                "docker": {"mirrors": [], "proxy": {}},
                                "nomad": {
                                    "dockerDriver": {"pullActivityTimeout": "30m"},
                                    "cniHostPorts": {
                                        "conflicts": [
                                            {
                                                "protocol": "tcp",
                                                "port": "8081",
                                                "allocIds": ["old-granary", "current-granary"],
                                                "shadowedAllocIds": ["current-granary"],
                                                "ruleCount": 2,
                                            }
                                        ]
                                    },
                                },
                                "recentImagePullErrors": [],
                            },
                        }
                    ]
                },
            }
            with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                code = main(["--no-env", "doctor", "--deep"])

            self.assertEqual(code, 1)
            output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
            self.assertIn("Node blg Nomad CNI hostports: fail", output)
            self.assertIn("tcp/8081 old-granary -> current-granary", output)
        finally:
            _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_doctor_deep_rejects_loopback_only_nomad_cni_namespace(self):
        old_home = _set_env("LUMA_CONFIG_HOME", tempfile.mkdtemp())
        try:
            save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="token")
            client = Mock()
            client.verify.return_value = {"clusterId": "luma-test"}
            client.status.return_value = {
                "cluster": {"id": "luma-test"},
                "nomad": {"available": True},
                "nodes": {
                    "items": [
                        {
                            "name": "blg",
                            "agentStatus": "ready",
                            "diagnostics": {
                                "docker": {"mirrors": [], "proxy": {}},
                                "nomad": {
                                    "dockerDriver": {"pullActivityTimeout": "30m"},
                                    "cniHostPorts": {
                                        "conflicts": [],
                                        "missingNetworks": [
                                            {
                                                "allocId": "alloc-broken",
                                                "container": "0123456789ab",
                                                "name": "nomad_init_broken",
                                                "interfaces": ["lo"],
                                            }
                                        ],
                                    },
                                },
                                "recentImagePullErrors": [],
                            },
                        }
                    ]
                },
            }
            with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                code = main(["--no-env", "doctor", "--deep"])

            self.assertEqual(code, 1)
            output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
            self.assertIn("Node blg Nomad CNI hostports: fail", output)
            self.assertIn("alloc-broken", output)
            self.assertIn("only loopback", output)
        finally:
            _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_node_agent_repair_nomad_cni_hostports_deletes_stale_duplicate_rules(self):
        executor = Mock()
        executor.run_result.side_effect = [
            Mock(code=0, output="current-alloc\nother-current\n"),
            Mock(
                code=0,
                output=(
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"old-alloc\\"" '
                    "-m multiport --dports 14173 -j CNI-DN-old\n"
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"current-alloc\\"" '
                    "-m multiport --dports 14173 -j CNI-DN-current\n"
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"other-old\\"" '
                    "-m multiport --dports 8081 -j CNI-DN-other-old\n"
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"other-current\\"" '
                    "-m multiport --dports 8081 -j CNI-DN-other-current\n"
                ),
            ),
        ]

        with patch("luma.agent.node_agent_os", return_value="linux"), patch("luma.agent._run_fixed_host_task") as run_host:
            result = repair_nomad_cni_hostports(executor=executor, ports=[14173])

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["staleAllocIds"], ["old-alloc"])
        self.assertEqual(result["hostPorts"], ["14173"])
        command = run_host.call_args.args[0]
        self.assertIn("-D CNI-HOSTPORT-DNAT", command)
        self.assertIn("old-alloc", command)
        self.assertNotIn("current-alloc", command)
        self.assertNotIn("other-old", command)
        self.assertNotIn("other-current", command)

    def test_node_agent_repair_nomad_cni_hostports_keeps_rules_without_active_replacement(self):
        executor = Mock()
        executor.run_result.side_effect = [
            Mock(code=0, output="old-alloc\n"),
            Mock(
                code=0,
                output=(
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"old-alloc\\"" '
                    "-m multiport --dports 14173 -j CNI-DN-old\n"
                    '-A CNI-HOSTPORT-DNAT -p tcp -m comment --comment "dnat name: \\"nomad\\" id: \\"new-but-not-running\\"" '
                    "-m multiport --dports 14173 -j CNI-DN-new\n"
                ),
            ),
        ]

        with patch("luma.agent.node_agent_os", return_value="linux"), patch("luma.agent._run_fixed_host_task") as run_host:
            result = repair_nomad_cni_hostports(executor=executor)

        self.assertEqual(result["deleted"], 0)
        run_host.assert_not_called()

    def test_doctor_deep_rejects_short_nomad_pull_activity_timeout(self):
        old_home = _set_env("LUMA_CONFIG_HOME", tempfile.mkdtemp())
        try:
            save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="token")
            client = Mock()
            client.verify.return_value = {"clusterId": "luma-test"}
            client.status.return_value = {
                "cluster": {"id": "luma-test"},
                "nomad": {"available": True},
                "portainer": {"ready": True},
                "swarm": {"available": True, "nodes": []},
                "nodes": {
                    "items": [
                        {
                            "name": "blg",
                            "agentStatus": "ready",
                            "diagnostics": {
                                "docker": {"mirrors": [], "proxy": {}},
                                "nomad": {"dockerDriver": {"pullActivityTimeout": "5m"}},
                                "recentImagePullErrors": [],
                            },
                        }
                    ]
                },
            }
            with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                code = main(["--no-env", "doctor", "--deep"])

            self.assertEqual(code, 1)
            output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
            self.assertIn("Node blg Nomad Docker pull timeout: fail", output)
            self.assertIn("pull_activity_timeout = \"30m\"", output)
        finally:
            _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_local_executor_timeout_returns_text_output(self):
        result = LocalExecutor().run_result("printf before-timeout; sleep 2", timeout=1)
        self.assertEqual(result.code, 124)
        self.assertIn("before-timeout", result.output)
        self.assertIn("command timed out after 1s", result.output)

    def test_installer_does_not_change_system_dns(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-luma.sh").read_text(encoding="utf-8")
        self.assertNotIn("resolvectl dns", installer)
        self.assertNotIn("/etc/systemd/resolved.conf.d/luma.conf", installer)

    def test_installer_resolves_home_before_install_paths(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-luma.sh").read_text(encoding="utf-8")

        self.assertIn('LUMA_USER_HOME="${LUMA_USER_HOME:-${HOME:-}}"', installer)
        self.assertIn('HOME="$LUMA_USER_HOME"', installer)
        self.assertIn('export HOME', installer)
        self.assertIn('INSTALL_HOME="${LUMA_INSTALL_HOME:-$LUMA_USER_HOME/.local/share/luma}"', installer)
        self.assertIn('BIN_DIR="${LUMA_BIN_DIR:-$LUMA_USER_HOME/.local/bin}"', installer)
        self.assertLess(
            installer.index('LUMA_USER_HOME="${LUMA_USER_HOME:-${HOME:-}}"'),
            installer.index("INSTALL_HOME="),
        )

    def test_installer_update_path_does_not_require_build_isolation_download(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-luma.sh").read_text(encoding="utf-8")

        self.assertIn("pip install --no-build-isolation", installer)
        self.assertIn("LUMA_PIP_BUILD_ISOLATION", installer)
        self.assertIn("pip upgrade failed; continuing with existing pip", installer)
        self.assertIn("build backend install failed; continuing with existing build backend", installer)
        self.assertIn('setuptools>=77', installer)
        self.assertIn("set +e", installer)
        self.assertIn('return "$code"', installer)
        self.assertIn("package install failed; using source checkout with existing venv dependencies", installer)
        self.assertIn("prune_stale_luma_metadata()", installer)
        self.assertIn('luma_infra-*.dist-info', installer)
        self.assertIn('expected="luma_infra-${source_version}.dist-info"', installer)
        self.assertIn('PYTHONPATH="$SOURCE_DIR\\${PYTHONPATH:+:\\$PYTHONPATH}"', installer)
        self.assertIn('exec "$VENV_DIR/bin/python" -m luma.cli', installer)

    def test_installer_refreshes_existing_node_agent_service_to_shim(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-luma.sh").read_text(encoding="utf-8")

        self.assertIn("refresh_node_agent_service()", installer)
        self.assertIn('LUMA_USER_HOME="${LUMA_USER_HOME:-${HOME:-}}"', installer)
        self.assertIn('agent_config="/opt/luma/node-agent/agent.json"', installer)
        self.assertIn("EnvironmentFile=-/etc/default/luma-node-agent", installer)
        self.assertIn("ExecStart=$BIN_DIR/luma node-agent run --config $agent_config", installer)
        self.assertIn("systemctl daemon-reload", installer)
        self.assertIn("Luma node agent systemd restart scheduled", installer)
        self.assertIn("Luma node agent launchd reload scheduled", installer)
        self.assertIn('LUMA_SKIP_NODE_AGENT_SERVICE_REFRESH:-0', installer)
        self.assertIn("Luma node agent service refresh deferred", installer)
        self.assertIn("LUMA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS", installer)
        self.assertIn("LUMA_DOWNLOAD_MAX_TIME_SECONDS", installer)
        self.assertIn("LUMA_DOWNLOAD_RETRIES", installer)
        self.assertIn("--retry-all-errors", installer)

    def test_installer_restores_user_install_ownership_after_root_update(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "scripts" / "install-luma.sh").read_text(encoding="utf-8")

        self.assertIn("resolve_install_owner()", installer)
        self.assertIn("chown_install_paths()", installer)
        self.assertIn("repair_install_ownership()", installer)
        self.assertIn("OWNER_SPEC=\"$(stat -c '%u:%g' \"$LUMA_USER_HOME\" 2>/dev/null)\"", installer)
        self.assertIn("OWNER_SPEC=\"$(stat -f '%u:%g' \"$LUMA_USER_HOME\" 2>/dev/null)\"", installer)
        self.assertIn('chown -R "$OWNER_SPEC" "$INSTALL_HOME"', installer)
        self.assertIn('find "$INSTALL_HOME/src" ! -user "$(id -u)"', installer)
        self.assertIn('run_sudo chown -R "$(id -u):$(id -g)" "$INSTALL_HOME"', installer)
        self.assertIn('chown "$OWNER_SPEC" "$BIN_DIR/luma"', installer)
        self.assertLess(
            installer.index("refresh_node_agent_service"),
            installer.rindex("chown_install_paths"),
        )
        self.assertLess(
            installer.index("run_sudo()"),
            installer.index("download_source()"),
        )
        self.assertLess(
            installer.index("repair_install_ownership"),
            installer.index('rm -rf "$INSTALL_HOME/src"'),
        )

    def test_public_port_guards_install_docker_user_proxy_guard(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""

        result = configure_public_port_guards(remote)

        self.assertEqual(result, "Public port guards installed")
        command = remote.sudo.call_args.args[0]
        self.assertIn("luma-public-port-guards.service", command)
        self.assertIn("restrict_nomad_public=no", command)
        self.assertIn("add_input_drop tcp 7890", command)
        self.assertIn("add_prerouting_drop tcp 7890", command)
        self.assertIn("add_docker_drop tcp 7890", command)
        self.assertIn("-t raw -I PREROUTING", command)
        self.assertIn("DOCKER-USER", command)
        self.assertIn("systemctl enable luma-public-port-guards.service", command)
        self.assertIn("systemctl restart luma-public-port-guards.service", command)

    def test_configure_firewall_restricts_nomad_public_when_tailscale_is_present(self):
        remote = Mock()

        def run_result(command):
            if command == "uname -s":
                return Mock(code=0, output="Linux\n")
            if "tailscale ip -4" in command:
                return Mock(code=0, output="100.64.0.10\n")
            return Mock(code=1, output="")

        remote.run_result.side_effect = run_result
        remote.sudo.return_value = ""

        result = configure_firewall(remote)

        self.assertEqual(result, "Firewall configured")
        ufw_command = remote.sudo.call_args_list[0].args[0]
        guard_command = remote.sudo.call_args_list[1].args[0]
        self.assertIn("ufw deny 7890/tcp", ufw_command)
        self.assertIn("ufw allow 4647/tcp", ufw_command)
        self.assertIn("restrict_nomad_public=yes", guard_command)
        self.assertIn("add_input_drop tcp 4647", guard_command)
        self.assertIn("add_input_drop udp 4648", guard_command)

    def test_configure_firewall_allows_configured_tcp_ports(self):
        remote = Mock()

        def run_result(command):
            if command == "uname -s":
                return Mock(code=0, output="Linux\n")
            if "tailscale ip -4" in command:
                return Mock(code=1, output="")
            return Mock(code=1, output="")

        remote.run_result.side_effect = run_result
        remote.sudo.return_value = ""

        configure_firewall(remote, tcp_ports=[3306])

        ufw_command = remote.sudo.call_args_list[0].args[0]
        self.assertIn("ufw allow 3306/tcp", ufw_command)

    def test_configure_tailscale_watchdog_installs_systemd_timer(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""

        result = configure_tailscale_watchdog(
            remote, peers=["100.69.154.50", "not-a-tailnet-address"]
        )

        self.assertEqual(result, "Tailscale watchdog installed")
        command = remote.sudo.call_args.args[0]
        self.assertIn("luma-tailscale-watchdog.service", command)
        self.assertIn("luma-tailscale-watchdog.timer", command)
        self.assertIn("tailscale ping --timeout=3s --c 2", command)
        self.assertIn("port=${LUMA_TAILSCALE_WATCHDOG_PORT:-0}", command)
        self.assertIn(
            "LUMA_TAILSCALE_WATCHDOG_PEERS=100.69.154.50", command
        )
        self.assertNotIn("not-a-tailnet-address", command)
        self.assertIn('log "tailnet ping failed: $addr"', command)
        self.assertIn('if [ "$port" -gt 0 ]', command)
        self.assertIn("control_state=/opt/luma/control/control.json", command)
        self.assertIn("state.get('nodes')", command)
        self.assertIn('300', command)
        self.assertIn("local_ips=$(tailscale ip", command)
        self.assertNotIn("addr=${addr%%:*}", command)
        self.assertIn("threshold=${LUMA_TAILSCALE_WATCHDOG_THRESHOLD:-3}", command)
        self.assertIn("systemctl restart tailscaled", command)
        self.assertIn("systemctl enable --now luma-tailscale-watchdog.timer", command)

    def test_new_config_model_reads_nodes_and_provider_dns(self):
        config = LumaConfig(
            {
                "project": "example",
                "providers": {
                    "dns": {"type": "cloudflare", "zone": "example.com"},
                },
                "nodes": {
                    "manager-1": {
                        "host": "manager-1",
                        "publicIp": "203.0.113.10",
                        "region": "cn",
                        "roles": ["swarm-manager", "edge", "egress"],
                    }
                },
            },
            None,
        )
        self.assertEqual(config.project_name, "example")
        self.assertEqual(config.dns["provider"], "cloudflare")
        self.assertEqual(config.default_dns_target(), "203.0.113.10")
        self.assertTrue(config.get_node("manager-1").has_role("edge"))

    def test_control_dns_without_target_is_skipped_with_fix_hint(self):
        old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
        try:
            config = LumaConfig(
                {"providers": {"dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "zone-id"}}},
                None,
            )
            result = sync_control_dns(config, "luma.example.com")
            self.assertIn("Control DNS skipped: missing DNS target", result)
            self.assertIn("luma configure --role manager", result)
        finally:
            _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_delete_dns_removes_matching_cloudflare_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}}}, None)
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                client = Mock()
                client.request.side_effect = [{"result": [{"id": "record-1"}]}, {"result": {}}]
                with patch("luma.cloudflare.CloudflareClient", return_value=client):
                    result = delete_dns(config, service)
                self.assertEqual(result, "DNS deleted: api.example.com")
                client.request.assert_any_call("GET", "/zones/zone-id/dns_records?type=A&name=api.example.com")
                client.request.assert_any_call("DELETE", "/zones/zone-id/dns_records/record-1")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_delete_dns_accepts_control_state_secret_without_process_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig(
                {"providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}}},
                None,
            )
            old_token = os.environ.pop("CLOUDFLARE_API_TOKEN", None)
            try:
                client = Mock()
                client.request.side_effect = [
                    {"result": [{"id": "record-1"}]},
                    {"result": {}},
                ]
                with patch("luma.cloudflare.CloudflareClient", return_value=client) as factory:
                    result = delete_dns(
                        config,
                        service,
                        secrets={"CLOUDFLARE_API_TOKEN": "state-token"},
                    )
                self.assertEqual(result, "DNS deleted: api.example.com")
                factory.assert_called_once_with("state-token")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_cloudflare_client_retries_transient_network_errors(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"success": true, "result": []}'
        with patch(
            "luma.cloudflare.urllib.request.urlopen",
            side_effect=[urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")), response],
        ) as urlopen, patch("luma.cloudflare.time.sleep") as sleep:
            payload = CloudflareClient("cf-token").request("GET", "/zones")

        self.assertTrue(payload["success"])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_profiles_have_expected_roles(self):
        self.assertIn("edge", PROFILES["single-node"].roles)
        self.assertIn("egress", PROFILES["egress-gateway"].roles)
        self.assertEqual(PROFILES["home-node"].labels["region"], "home")

    def test_external_edge_dns_target_prefers_global_node(self):
        config = LumaConfig(
            {
                "nodes": {
                    "manager-1": {
                        "host": "manager-1",
                        "publicIp": "203.0.113.10",
                        "region": "cn",
                        "roles": ["edge"],
                    },
                    "sg": {
                        "host": "sg",
                        "publicIp": "203.0.113.9",
                        "region": "global",
                        "roles": ["edge"],
                    },
                }
            },
            None,
        )
        self.assertEqual(config.dns_target_for(exposure="external-edge", region="global"), "203.0.113.9")

    def test_traefik_ports_can_be_overridden_for_cloud_hosts(self):
        config = LumaConfig({"defaults": {"ports": {"traefikHttp": 10080, "traefikHttps": 10443}}}, None)
        self.assertEqual(_traefik_ports(config), (10080, 10443))

    def test_acme_email_defaults_from_dns_zone(self):
        config = LumaConfig({"providers": {"dns": {"zone": "example.net"}}}, None)
        self.assertEqual(_acme_email(config), "admin@example.net")

    def test_sudo_prompt_is_stripped_from_command_values(self):
        self.assertEqual(_last_command_value("[sudo] password for user: SWMTKN-1-token\n"), "SWMTKN-1-token")

    def test_tailscale_manager_addr_detection(self):
        self.assertTrue(_is_tailscale_manager_addr("100.64.0.1:2377"))
        self.assertTrue(_is_tailscale_manager_addr("100.127.255.254"))
        self.assertFalse(_is_tailscale_manager_addr("100.128.0.1:2377"))
        self.assertFalse(_is_tailscale_manager_addr("203.0.113.10:2377"))

    def test_cli_nomad_detection_checks_common_install_paths(self):
        from luma.cli import _find_nomad_cli

        with patch("luma.cli.shutil.which", return_value=None), patch("luma.cli.Path.exists", return_value=True), patch(
            "luma.cli.os.access", return_value=True
        ):
            self.assertEqual(_find_nomad_cli(), "/usr/local/bin/nomad")

    def test_remote_nomad_detection_checks_common_install_paths(self):
        from luma.bootstrap import local_nomad_node_info

        remote = Mock()
        remote.run_result.return_value = Mock(code=1, output="")

        local_nomad_node_info(remote)

        command = remote.run_result.call_args_list[0].args[0]
        self.assertIn("elif test -x /usr/local/bin/nomad", command)
        self.assertIn("elif test -x /opt/homebrew/bin/nomad", command)
        self.assertIn('"$nomad_bin" node status -self -json', command)

    def test_nomad_job_deploy_uses_unique_tmp_file(self):
        remote = Mock()
        result = _deploy_nomad_job(remote, '{"Job":{"ID":"traefik"}}', "traefik")

        command = remote.run.call_args.args[0]
        self.assertEqual(result, "Nomad job deployed: traefik")
        self.assertIn("mktemp /tmp/luma-nomad-job.", command)
        self.assertIn("trap 'rm -f \"$tmp\"' EXIT", command)
        self.assertIn('nomad job run -json "$tmp"', command)
        self.assertNotIn("/tmp/traefik.nomad.json", command)

    def test_nomad_job_deploy_explains_tmpfs_noswap_failure(self):
        remote = Mock()
        remote.run.side_effect = LumaError(
            "local command failed:\n"
            "prestart hook \"task_dir\" failed: mount: invalid argument\n"
            "tmpfs: Unknown parameter 'noswap'"
        )

        with self.assertRaisesRegex(LumaError, "Nomad failed while preparing the task secrets tmpfs"):
            _deploy_nomad_job(remote, '{"Job":{"ID":"luma-control"}}', "luma-control")

    def test_nomad_tmpfs_compat_reports_fallback_on_old_kernel(self):
        remote = Mock()

        def run_result(command):
            if "uname -s" in command:
                return Mock(code=0, output="Linux\n")
            if "uname -r" in command:
                return Mock(code=0, output="5.15.0-126-generic\n")
            if "nomad version" in command:
                return Mock(code=0, output="Nomad v1.9.7\n")
            return Mock(code=1, output="")

        remote.run_result.side_effect = run_result

        result = _nomad_tmpfs_compat_status(remote)

        self.assertIn("fallback available", result)
        self.assertIn("5.15.0-126-generic", result)

    def test_nomad_tmpfs_compat_reports_kernel_support(self):
        remote = Mock()

        def run_result(command):
            if "uname -s" in command:
                return Mock(code=0, output="Linux\n")
            if "uname -r" in command:
                return Mock(code=0, output="6.8.0\n")
            if "nomad version" in command:
                return Mock(code=0, output="Nomad v1.9.7\n")
            return Mock(code=1, output="")

        remote.run_result.side_effect = run_result

        self.assertIn("supported by Linux kernel 6.8.0", _nomad_tmpfs_compat_status(remote))

    def test_nomad_and_kernel_version_parsing(self):
        self.assertEqual(_parse_nomad_version("Nomad v1.9.7"), (1, 9, 7))
        self.assertEqual(_parse_nomad_version("Nomad v1.10.3+ent"), (1, 10, 3))
        self.assertIsNone(_parse_nomad_version("nomad not installed"))
        self.assertEqual(_parse_kernel_version("5.15.0-126-generic"), (5, 15))
        self.assertIsNone(_parse_kernel_version(""))

    def test_packaged_dashboard_assets_are_available(self):
        self.assertIn("Luma · 控制台", asset_text("dashboard/index.html"))
        self.assertIn("/v1/dashboard", asset_text("dashboard/app.js"))
        self.assertGreater(asset_path("dashboard/asset-luma-logo-mark.png").stat().st_size, 0)
        root = Path(__file__).resolve().parents[1]
        self.assertIn('"assets/dashboard/*"', (root / "pyproject.toml").read_text(encoding="utf-8"))

    def test_luma_control_nomad_job_uses_autorevert_and_node_pin(self):
        from luma.nomad_render import render_control_job

        job = render_control_job(image="ghcr.io/gaojiu/luma-control:0.1.0", node_name="aly", as_json=False)["Job"]
        self.assertEqual(job["ID"], "luma-control")
        self.assertEqual(job["Update"]["AutoRevert"], True)
        self.assertEqual(job["Update"]["MinHealthyTime"], 6_000_000_000)
        self.assertEqual(job["Update"]["HealthyDeadline"], 120_000_000_000)
        self.assertEqual(job["Constraints"][0]["LTarget"], "${meta.luma_node_name}")
        self.assertEqual(job["Constraints"][0]["RTarget"], "aly")


class EgressConfigTests(unittest.TestCase):
    def test_egress_config_generation_accepts_base64_subscription(self):
        subscription = yaml.safe_dump(
            {
                "proxies": [
                    {"name": "proxy-a", "type": "trojan", "server": "example.com", "port": 443, "password": "x"}
                ],
                "proxy-groups": [{"name": "old", "type": "select", "proxies": ["proxy-a"]}],
                "rule-providers": {"remote": {"type": "http", "url": "https://example.com/rules"}},
                "rules": ["MATCH,old"],
            },
            allow_unicode=True,
        ).encode()
        generated = yaml.safe_load(minimal_mihomo_config_from_bytes(base64.b64encode(subscription)))
        self.assertEqual(generated["mixed-port"], 7890)
        self.assertEqual(generated["mode"], "rule")
        self.assertEqual(generated["rules"], ["MATCH,EGRESS"])
        self.assertNotIn("rule-providers", generated)
        group = generated["proxy-groups"][0]
        self.assertEqual(group["type"], "url-test")
        self.assertEqual(group["proxies"], ["proxy-a"])
        self.assertEqual(group["url"], "https://www.gstatic.com/generate_204")
        self.assertEqual(group["interval"], 300)

    def test_internal_registry_direct_rule_precedes_catch_all_proxy(self):
        current = yaml.safe_dump({"mixed-port": 7890, "rules": ["MATCH,EGRESS"]})

        updated, changed = ensure_mihomo_direct_domains(
            current,
            ["registry.itool.tech", "registry.itool.tech"],
        )

        self.assertTrue(changed)
        self.assertEqual(
            yaml.safe_load(updated)["rules"],
            ["DOMAIN,registry.itool.tech,DIRECT", "MATCH,EGRESS"],
        )
        unchanged, changed_again = ensure_mihomo_direct_domains(updated, ["registry.itool.tech"])
        self.assertFalse(changed_again)
        self.assertEqual(unchanged, updated)


class CliTests(unittest.TestCase):
    def test_last_command_value_preserves_digest_colon_after_sudo_prompt(self):
        output = '[sudo] password for tao: ["ghcr.io/liutianjie/luma-control@sha256:abc123"]\n'

        self.assertEqual(_last_command_value(output), '["ghcr.io/liutianjie/luma-control@sha256:abc123"]')

    def test_init_writes_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "luma.yaml"
            code = main(["--config", str(path), "init"])
            self.assertEqual(code, 0)
            data = yaml.safe_load(path.read_text())
            self.assertIn("providers", data)
            self.assertIn("nodes", data)

    def test_parser_exposes_tailscale_connect(self):
        args = build_parser().parse_args(["tailscale", "connect"])
        self.assertEqual(args.command, "tailscale")
        self.assertEqual(args.tailscale_command, "connect")

    def test_repair_commands_reject_remote_node_argument(self):
        for argv in (["tailscale", "connect", "manager-1"], ["portainer", "setup", "manager-1"], ["egress", "setup", "manager-1"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_parser_exposes_preflight(self):
        args = build_parser().parse_args(["preflight"])
        self.assertEqual(args.command, "preflight")

    def test_parser_exposes_configure(self):
        args = build_parser().parse_args(["configure", "--role", "worker"])
        self.assertEqual(args.command, "configure")
        self.assertEqual(args.role, "worker")

    def test_bootstrap_supports_skip_egress(self):
        args = build_parser().parse_args(["node", "bootstrap", "manager-1", "--profile", "single-node", "--skip-egress"])
        self.assertEqual(args.command, "node")
        self.assertEqual(args.node_command, "bootstrap")
        self.assertTrue(args.skip_egress)

    def test_bootstrap_manager_supports_public_port_overrides(self):
        args = build_parser().parse_args(
            ["bootstrap", "manager", "--domain", "luma.example.com", "--http-port", "10080", "--https-port", "10443"]
        )
        self.assertEqual(args.command, "bootstrap")
        self.assertEqual(args.bootstrap_command, "manager")
        self.assertEqual(args.http_port, 10080)
        self.assertEqual(args.https_port, 10443)

    def test_deploy_defaults_to_control_plane(self):
        args = build_parser().parse_args(["deploy", "app.yaml"])
        self.assertEqual(args.command, "deploy")
        self.assertEqual(args.timeout, 1800)

    def test_service_remove_parser_defaults_to_full_cleanup(self):
        args = build_parser().parse_args(["service", "remove", "app.yaml"])
        self.assertEqual(args.command, "service")
        self.assertEqual(args.service_command, "remove")
        self.assertFalse(args.skip_dns)
        self.assertFalse(args.skip_orchestrator)
        self.assertFalse(args.delete_storage)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout, 300)
        args = build_parser().parse_args(["service", "remove", "app", "--delete-storage"])
        self.assertTrue(args.delete_storage)

    def test_compose_and_storage_parsers_accept_planned_commands(self):
        args = build_parser().parse_args(["compose", "deploy", "luma.compose.yml", "--dry-run"])
        self.assertEqual(args.command, "compose")
        self.assertEqual(args.compose_command, "deploy")
        self.assertTrue(args.dry_run)
        args = build_parser().parse_args(["compose", "validate", "luma.compose.yml", "--import-mode"])
        self.assertEqual(args.compose_command, "validate")
        self.assertTrue(args.import_mode)
        args = build_parser().parse_args(
            [
                "secret",
                "set",
                "DATABASE_URL",
                "--value",
                "postgres://secret",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.secret_command, "set")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "registry",
                "login",
                "ghcr.io",
                "--username",
                "bot",
                "--password-stdin",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.registry_command, "login")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(["compose", "validate", "luma.compose.yml", "--control-url", "https://luma.example.com", "--token", "deploy-token"])
        self.assertEqual(args.compose_command, "validate")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "migrate",
                "luma.compose.yml",
                "--volume",
                "pg-data",
                "--from-node",
                "home",
                "--from-volume",
                "pg-data",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.command, "storage")
        self.assertEqual(args.storage_command, "migrate")
        self.assertEqual(args.volume, "pg-data")
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "set",
                "home-nfs",
                "--provider",
                "nfs",
                "--node",
                "home-nas",
                "--path",
                "/srv/luma",
                "--control-url",
                "https://luma.example.com",
                "--token",
                "deploy-token",
            ]
        )
        self.assertEqual(args.storage_command, "set")
        self.assertEqual(args.name, "home-nfs")
        self.assertEqual(args.node, "home-nas")
        self.assertEqual(args.path, "/srv/luma")
        self.assertEqual(args.timeout, 360)
        self.assertFalse(args.external)
        self.assertEqual(args.control_url, "https://luma.example.com")
        args = build_parser().parse_args(
            [
                "storage",
                "set",
                "company-nfs",
                "--external",
                "--endpoint",
                "nfs.example.com:/srv/luma",
                "--region",
                "cn",
            ]
        )
        self.assertTrue(args.external)
        self.assertEqual(args.endpoint, "nfs.example.com:/srv/luma")
        self.assertEqual(args.regions, ["cn"])
        for argv in (
            ["storage", "set", "home-nfs", "--mode", "managed"],
            ["storage", "set", "home-nfs", "--export-root", "/srv/luma"],
            ["storage", "set", "home-nfs", "--provider", "external"],
        ):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(argv)

    def test_storage_set_command_validates_new_shape_before_control_call(self):
        cases = (
            ["storage", "set", "home-nfs", "--node", "home-nas"],
            ["storage", "set", "home-nfs", "--node", "home-nas", "--path", "/srv/luma", "--endpoint", "home-nas:/srv/luma"],
            ["storage", "set", "company-nfs", "--external", "--endpoint", "nfs.example.com:/srv/luma"],
        )
        for argv in cases:
            with self.subTest(argv=argv), patch("luma.cli.ControlClient") as client_cls, patch("builtins.print"):
                code = main(argv)
            self.assertEqual(code, 1)
            client_cls.assert_not_called()

    def test_service_remove_rejects_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text("name: api\nimage: nginx:alpine\nregion: cn\nexposure: none\n", encoding="utf-8")
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print"):
                    code = main(["service", "remove", str(service_path), "--timeout", "12", "--dry-run"])
                self.assertEqual(code, 1)
                client_cls.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_service_remove_submits_name_to_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.remove_service.return_value = {
                    "service": "api",
                    "dryRun": True,
                    "portainer": "Nomad job would be removed: api",
                    "generatedFiles": "Generated files would be removed: /opt/luma/stacks/cn/api",
                    "steps": [
                        {"name": "Remove Nomad job", "status": "ok", "message": "Nomad job would be removed: api"},
                    ],
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["service", "remove", "api", "--dry-run"])
                self.assertEqual(code, 0)
                client.remove_service.assert_called_once()
                kwargs = client.remove_service.call_args.kwargs
                self.assertEqual(kwargs["name"], "api")
                self.assertTrue(kwargs["dry_run"])
                self.assertFalse(kwargs["delete_storage"])
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Submit remove: api", printed_text)
                self.assertIn("[ok] Remove dry run finished: api", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_prints_progress_and_passes_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.deploy_events.side_effect = LumaError("control API error 404: not found")
                client.deploy.return_value = {
                    "service": "api",
                    "image": {"selected": "nginx:alpine"},
                    "portainer": "Nomad job deployed for api: api",
                    "steps": [
                        {"name": "Sync DNS", "status": "ok", "message": "DNS skipped: service is not public"},
                        {"name": "Deploy Nomad job", "status": "ok", "message": "Nomad job deployed for api: api"},
                    ],
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["deploy", str(service_path), "--timeout", "42"])
                self.assertEqual(code, 0)
                client.deploy.assert_called_once()
                self.assertEqual(client.deploy.call_args.kwargs["timeout"], 42)
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Load deploy context", printed_text)
                self.assertIn("[start] Waiting for control plane response (timeout 42s)", printed_text)
                self.assertIn("[ok] Sync DNS: DNS skipped: service is not public", printed_text)
                self.assertIn("[ok] Deploy Nomad job: Nomad job deployed for api: api", printed_text)
                self.assertIn("[ok] Deploy finished: api", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_streams_current_control_plane_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.deploy_events.return_value = iter(
                    [
                        {"name": "Resolve image", "status": "start", "message": "started"},
                        {"name": "Resolve image", "status": "ok", "message": "nginx:alpine"},
                        {"status": "done", "result": {"service": "api", "image": {"selected": "nginx:alpine"}}},
                    ]
                )
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["deploy", str(service_path)])
                self.assertEqual(code, 0)
                client.deploy_events.assert_called_once()
                client.deploy.assert_not_called()
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[start] Resolve image: started", printed_text)
                self.assertIn("[ok] Resolve image: nginx:alpine", printed_text)
                self.assertIn("[ok] Deploy finished: api", printed_text)
                self.assertNotIn("Waiting for control plane response", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_compose_deploy_submits_sidecar_and_compose_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}}),
                encoding="utf-8",
            )
            sidecar_path.write_text(
                yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"}),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.deploy_compose_events.side_effect = LumaError("control API error 404: not found")
                client.deploy_compose.return_value = {"deployment": "app-stack", "steps": []}
                with patch("luma.cli.ControlClient", return_value=client):
                    code = main(["compose", "deploy", str(sidecar_path), "--timeout", "12"])
                self.assertEqual(code, 0)
                client.deploy_compose.assert_called_once()
                kwargs = client.deploy_compose.call_args.kwargs
                self.assertIn("app-stack", kwargs["manifest"])
                self.assertIn("nginx:alpine", kwargs["compose_content"])
                self.assertEqual(kwargs["timeout"], 12)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_compose_deploy_stream_interrupted_does_not_redeploy(self):
        # Regression: if the event stream emits steps but ends WITHOUT a `done`
        # result (connection dropped mid-deploy), the deploy already ran on the
        # manager. Re-issuing via the non-streaming endpoint would silently
        # deploy a second time. Must raise instead, like the native path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}}),
                encoding="utf-8",
            )
            sidecar_path.write_text(
                yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"}),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                # stream yields progress but no {"status": "done", ...} result
                client.deploy_compose_events.return_value = iter(
                    [
                        {"name": "Render compose Nomad job", "status": "start", "message": "started"},
                        {"name": "Render compose Nomad job", "status": "ok", "message": "rendered"},
                    ]
                )
                with patch("luma.cli.ControlClient", return_value=client):
                    code = main(["compose", "deploy", str(sidecar_path), "--timeout", "12"])
                # non-zero exit (LumaError surfaced) and NO silent second deploy
                self.assertNotEqual(code, 0)
                client.deploy_compose.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_streams_ndjson_with_env_context_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            old_insecure = _set_env("LUMA_INSECURE", "")
            old_resolve = _set_env("LUMA_RESOLVE_IP", "")
            try:
                client = Mock()
                client.deploy_events.return_value = iter(
                    [
                        {"name": "Resolve image", "status": "start", "message": "started"},
                        {"name": "Resolve image", "status": "ok", "message": "nginx:alpine"},
                        {"status": "done", "result": {"service": "api", "image": {"selected": "nginx:alpine"}}},
                    ]
                )
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
                    code = main(["--no-env", "deploy", str(service_path), "--format", "ndjson"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)
                _restore_env("LUMA_INSECURE", old_insecure)
                _restore_env("LUMA_RESOLVE_IP", old_resolve)

            self.assertEqual(code, 0)
            client_cls.assert_called_once_with("https://luma.example.com", "deploy-token", insecure=False, resolve_ip=None)
            client.deploy_events.assert_called_once()
            client.deploy.assert_not_called()
            lines = [json.loads(call.args[0]) for call in printed.call_args_list]
            self.assertTrue(all(isinstance(line, dict) for line in lines))
            self.assertEqual(lines[0]["type"], "event")
            self.assertEqual(lines[-1]["type"], "result")
            self.assertTrue(lines[-1]["ok"])
            self.assertEqual(lines[-1]["result"]["service"], "api")

    def test_import_cli_supports_provider_repository_manifest_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            manifest_path = root / "service.luma.yml"
            env_path = root / "service.env"
            manifest_path.write_text(
                "name: api\nregion: cn\nexposure: none\nenv:\n  DATABASE_URL: ${DATABASE_URL}\n",
                encoding="utf-8",
            )
            env_path.write_text("DATABASE_URL=postgres://secret\nUNUSED=value\n", encoding="utf-8")
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.build_deploy_events.side_effect = LumaError("control API error 404: not found")
                client.build_deploy.return_value = {"service": "api", "image": "100.66.177.70:5000/acme/app:abc123", "steps": []}
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print"):
                    code = main(
                        [
                            "import",
                            "--provider-id",
                            "gitea:lin",
                            "--repository",
                            "acme/app",
                            "--build-node",
                            "builder",
                            "--proxy-mode",
                            "direct",
                            "--manifest",
                            str(manifest_path),
                            "--env",
                            str(env_path),
                        ]
                    )

                self.assertEqual(code, 0)
                kwargs = client.build_deploy.call_args.kwargs
                self.assertEqual(kwargs["provider_id"], "gitea:lin")
                self.assertEqual(kwargs["repository"], "acme/app")
                self.assertEqual(kwargs["repo_url"], "")
                self.assertEqual(kwargs["proxy_mode"], "direct")
                self.assertIn("DATABASE_URL", kwargs["manifest"])
                self.assertEqual(kwargs["env_secrets"], {"DATABASE_URL": "postgres://secret", "UNUSED": "value"})
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_compose_validate_import_mode_accepts_build_only_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            compose_path.write_text(
                "services:\n  web:\n    build:\n      context: .\n      dockerfile: Dockerfile\n",
                encoding="utf-8",
            )
            sidecar_path.write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (root / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
            old_home = _set_env("LUMA_CONFIG_HOME", str(home))
            try:
                with patch("builtins.print"):
                    code = main(["compose", "validate", str(sidecar_path), "--import-mode"])
                self.assertEqual(code, 0)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_git_provider_cli_set_reads_token_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_git_provider.return_value = {"id": "gitea:lin", "saved": True}
                with patch("luma.cli.ControlClient", return_value=client), patch("sys.stdin", io.StringIO("gitea-secret\n")), patch("builtins.print"):
                    code = main(
                        [
                            "git-provider",
                            "set",
                            "gitea",
                            "lin",
                            "--base-url",
                            "https://gcode.example.com",
                            "--username",
                            "lin",
                            "--token-stdin",
                        ]
                    )

                self.assertEqual(code, 0)
                client.set_git_provider.assert_called_once_with(
                    provider_type="gitea",
                    account="lin",
                    token="gitea-secret",
                    base_url="https://gcode.example.com",
                    clone_base_url="",
                    username="lin",
                )
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_git_provider_cli_lists_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.list_git_provider_repositories.return_value = {
                    "repositories": [{"fullName": "acme/app", "defaultBranch": "main", "private": True}]
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["git-provider", "repos", "gitea:lin"])

                self.assertEqual(code, 0)
                client.list_git_provider_repositories.assert_called_once_with(provider_id="gitea:lin")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("acme/app", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_dry_run_json_does_not_create_control_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print") as printed:
                code = main(["deploy", str(service_path), "--dry-run", "--format", "json"])

        self.assertEqual(code, 0)
        client_cls.assert_not_called()
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["dryRun"])
        self.assertEqual(payload["result"]["service"]["name"], "api")
        self.assertEqual(payload["result"]["artifacts"][0]["kind"], "job")

    def test_compose_validate_json_reports_degraded_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_config_home = _set_env("LUMA_CONFIG_HOME", str(root / "config-home"))
            config_path = root / "luma.yaml"
            compose_path = root / "docker-compose.yml"
            sidecar_path = root / "luma.compose.yml"
            try:
                config_path.write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose_path.write_text(yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}}), encoding="utf-8")
                sidecar_path.write_text(
                    yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"}),
                    encoding="utf-8",
                )

                with patch("builtins.print") as printed:
                    code = main(
                        [
                            "--no-env",
                            "--config",
                            str(config_path),
                            "compose",
                            "validate",
                            str(sidecar_path),
                            "--format",
                            "json",
                        ]
                    )
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_config_home)

        self.assertEqual(code, 0)
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["validationMode"], "degraded")
        self.assertTrue(any("Storage classes" in warning for warning in payload["result"]["warnings"]))
        self.assertTrue(any("Node records" in warning for warning in payload["result"]["warnings"]))

    def test_wait_heartbeat_prints_during_slow_deploy_request(self):
        def slow_action():
            time.sleep(0.03)
            return {"ok": True}

        with patch("builtins.print") as printed:
            result = _run_with_wait_heartbeat(slow_action, timeout=42, interval=0.01)
        self.assertEqual(result, {"ok": True})
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[wait] Control plane still working", printed_text)
        self.assertIn("timeout 42s", printed_text)

    def test_login_writes_context_without_printing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            client = Mock()
            client.verify_login.return_value = {"clusterId": "luma-test"}
            try:
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
                    code = main(
                        [
                            "login",
                            "https://luma.example.com",
                            "--token",
                            "secret-token",
                            "--insecure",
                            "--resolve-ip",
                            "203.0.113.10",
                        ]
                    )
                self.assertEqual(code, 0)
                client_cls.assert_called_once_with(
                    "https://luma.example.com",
                    "secret-token",
                    insecure=True,
                    resolve_ip="203.0.113.10",
                )
                context = load_current_context()
                self.assertEqual(context["clusterId"], "luma-test")
                self.assertEqual(context["endpoint"], "https://luma.example.com")
                self.assertEqual(context["token"], "secret-token")
                self.assertTrue(context["insecure"])
                self.assertEqual(context["resolveIp"], "203.0.113.10")
                printed_text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
                self.assertNotIn("secret-token", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_configure_writes_user_config_without_printing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            try:
                secret_values = iter(["cf-token", "ts-key", "sub-url", "sudo-pass"])
                with patch("luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)), patch(
                    "builtins.input", return_value="ops@example.com"
                ), patch("builtins.print") as printed:
                    code = main(["configure", "--role", "manager"])
                self.assertEqual(code, 0)
                self.assertTrue(config_path.exists())
                keys = configured_keys(config_path)
                self.assertIn("CLOUDFLARE_API_TOKEN", keys)
                self.assertIn("LUMA_DNS_EDGE_TARGET", keys)
                self.assertIn("TRAEFIK_ACME_EMAIL", keys)
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("cf-token", printed_text)
                self.assertNotIn("sudo-pass", printed_text)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)

    def test_bootstrap_prompts_for_missing_manager_config_during_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            project_config = Path(tmp) / "luma.yaml"
            project_config.write_text("{}\n", encoding="utf-8")
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            old_email = _set_env("TRAEFIK_ACME_EMAIL", "")
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_egress = _set_env("EGRESS_SUBSCRIPTION_URL", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                secret_values = iter(["cf-token", "ts-key", "sub-url", "sudo-pass"])
                def bootstrap_side_effect(_config, _node, _profile, _domain, state, **_kwargs):
                    state["portainerApiUrl"] = "https://203.0.113.10:9443/api"
                    state["portainerAdminUsername"] = "admin"
                    state["portainerAdminPassword"] = "portainer-secret"
                    return []

                input_values = iter(["203.0.113.10", "ops@example.com"])
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("builtins.input", side_effect=lambda _prompt: next(input_values)), patch(
                    "luma.cli.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch(
                    "luma.cli.find_zone", return_value={"id": "zone-example"}
                ), patch("builtins.print") as printed:
                    code = main(
                        [
                            "--config",
                            str(project_config),
                            "bootstrap",
                            "manager",
                            "--domain",
                            "luma.example.com",
                            "--profile",
                            "single-node",
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertTrue(config_path.exists())
                self.assertIn("LUMA_DNS_EDGE_TARGET", configured_keys(config_path))
                self.assertIn("EGRESS_SUBSCRIPTION_URL", configured_keys(config_path))
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("CLOUDFLARE_API_TOKEN [required]", printed_text)
                self.assertIn("Zone Read and DNS Edit", printed_text)
                self.assertIn("LUMA_DNS_EDGE_TARGET [optional", printed_text)
                self.assertIn("EGRESS_SUBSCRIPTION_URL [optional", printed_text)
                self.assertNotIn("portainer-secret", printed_text)
                self.assertIn(
                    "cn worker: luma node join https://luma.example.com --token",
                    printed_text,
                )
                self.assertIn("--region global --name global-sg-1", printed_text)
                self.assertIn("--region home --name home-mac-mini", printed_text)
                self.assertNotIn("--egress", printed_text)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)
                _restore_env("TRAEFIK_ACME_EMAIL", old_email)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("EGRESS_SUBSCRIPTION_URL", old_egress)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_bootstrap_infers_cloudflare_dns_from_interactive_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "providers": {"portainer": {}},
                        "nodes": {
                            "manager": {
                                "host": "localhost",
                                "publicIp": "203.0.113.10",
                                "region": "cn",
                                "roles": ["swarm-manager", "edge"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            old_email = _set_env("TRAEFIK_ACME_EMAIL", "ops@example.com")
            old_ts = _set_env("TAILSCALE_AUTHKEY", "ts-key")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "sudo-pass")
            try:
                captured = {}

                def bootstrap_side_effect(config, _node, _profile, _domain, state, **_kwargs):
                    captured["config"] = config.raw
                    state["portainerApiUrl"] = "https://203.0.113.10:9443/api"
                    state["portainerAdminUsername"] = "admin"
                    state["portainerAdminPassword"] = "portainer-secret"
                    return []

                def find_zone_side_effect(_config, zone_name):
                    if zone_name == "itool.tech":
                        return {"id": "zone-itool"}
                    raise LumaError("not found")

                with patch("luma.cli.find_zone", side_effect=find_zone_side_effect), patch(
                    "luma.cli.bootstrap_manager_local", side_effect=bootstrap_side_effect
                ), patch("builtins.print"):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "bootstrap",
                            "manager",
                            "--domain",
                            "luma.itool.tech",
                            "--node",
                            "manager",
                            "--skip-egress",
                        ]
                    )
                self.assertEqual(code, 0)
                dns = captured["config"]["providers"]["dns"]
                self.assertEqual(dns["type"], "cloudflare")
                self.assertEqual(dns["zone"], "itool.tech")
                self.assertEqual(dns["zoneId"], "zone-itool")
                self.assertEqual(dns["edgeTarget"], "203.0.113.10")
                saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["providers"]["dns"]["zoneId"], "zone-itool")
                self.assertEqual(saved["providers"]["dns"]["edgeTarget"], "203.0.113.10")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)
                _restore_env("TRAEFIK_ACME_EMAIL", old_email)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_examples_are_region_first(self):
        examples = _node_join_examples("https://luma.example.com", "join-token")
        commands = "\n".join(command for _label, command in examples)
        self.assertIn("--region cn --name cn-worker-1", commands)
        self.assertIn("--region global --name global-sg-1", commands)
        self.assertIn("--region home --name home-mac-mini", commands)
        self.assertNotIn("--profile", commands)
        self.assertNotIn("--egress", commands)

    def test_update_manager_installs_cli_then_refreshes_control_only(self):
        state = {"clusterId": "luma-test", "domain": "luma.example.com", "deployToken": "deploy", "joinToken": "join"}
        with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update") as reexec, patch(
            "luma.cli._existing_control_state", return_value=state
        ), patch(
            "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
        ) as refresh:
            code = main(
                [
                    "update",
                    "manager",
                    "--domain",
                    "luma.example.com",
                    "--profile",
                    "single-node",
                    "--install-ref",
                    "main",
                ]
            )

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref="main", skip_node_agent_refresh=True)
        reexec.assert_called_once()
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], "luma.example.com")
        self.assertIs(refresh.call_args.args[3], state)

    def test_installer_bootstrap_uses_the_same_tag_ref_as_source_archive(self):
        from luma.installer import luma_installer_command

        command, exact_ref = luma_installer_command("staging/a4b02a3", environ={})

        self.assertEqual(exact_ref, "staging/a4b02a3")
        self.assertIn(
            "https://raw.githubusercontent.com/LiuTianjie/luma/staging/a4b02a3/scripts/install-luma.sh",
            command,
        )
        self.assertNotIn("/main/scripts/install-luma.sh", command)
        self.assertNotIn("| sh", command)
        self.assertIn("curl -fsSL", command)
        self.assertIn('-o "$installer"', command)

    def test_installer_bootstrap_propagates_download_failure(self):
        from luma.installer import luma_installer_command

        with patch("luma.installer.LUMA_INSTALLER_RAW_BASE", "http://127.0.0.1:1"):
            command, _exact_ref = luma_installer_command("missing-ref", environ={})

        completed = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_local_update_pins_bootstrap_installer_and_archive_to_install_ref(self):
        with patch("luma.cli.subprocess.run") as run:
            from luma.cli import _run_luma_installer

            _run_luma_installer(install_ref="v0.1.168")

        self.assertIn("/v0.1.168/scripts/install-luma.sh", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["LUMA_INSTALL_REF"], "v0.1.168")

    def test_manager_installer_preserves_running_operator_layout_and_defers_agent_restart(self):
        with patch("luma.cli._current_install_layout", return_value=(
            Path("/home/tao"),
            Path("/home/tao/.local/share/luma"),
            Path("/home/tao/.local/bin"),
        )), patch("luma.cli.subprocess.run") as run:
            from luma.cli import _run_luma_installer

            _run_luma_installer(install_ref="v0.1.173", skip_node_agent_refresh=True)

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["LUMA_USER_HOME"], "/home/tao")
        self.assertEqual(env["LUMA_INSTALL_HOME"], "/home/tao/.local/share/luma")
        self.assertEqual(env["LUMA_BIN_DIR"], "/home/tao/.local/bin")
        self.assertEqual(env["LUMA_SKIP_NODE_AGENT_SERVICE_REFRESH"], "1")

    def test_node_agent_update_pins_bootstrap_installer_and_archive_to_install_ref(self):
        executor = Mock()
        completed = Mock(returncode=0, stdout="installer ok\nLuma version: 0.1.222\n")
        with patch("luma.agent.subprocess.run", return_value=completed) as run, patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent._installed_luma_executable", return_value="/root/.local/bin/luma"
        ):
            update_luma_install(install_ref="staging/a4b02a3")

        self.assertIn("/staging/a4b02a3/scripts/install-luma.sh", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["LUMA_INSTALL_REF"], "staging/a4b02a3")

    def test_detached_manager_update_starts_before_installer_or_control_refresh(self):
        with patch("luma.cli._start_detached_manager_update", return_value=0) as detached, patch(
            "luma.cli._run_luma_installer"
        ) as installer, patch("luma.cli._refresh_manager_control") as refresh:
            code = main(["update", "manager", "--detach", "--install-ref", "v0.1.168"])

        self.assertEqual(code, 0)
        detached.assert_called_once()
        installer.assert_not_called()
        refresh.assert_not_called()

    def test_detach_before_manager_subcommand_is_not_overwritten_by_subparser_defaults(self):
        with patch("luma.cli._start_detached_manager_update", return_value=0) as detached:
            code = main(["update", "--detach", "manager"])

        self.assertEqual(code, 0)
        detached.assert_called_once()

    def test_detached_manager_child_does_not_spawn_recursively(self):
        old_detached = _set_env("LUMA_UPDATE_DETACHED", "1")
        old_reexec = _set_env("LUMA_UPDATE_REEXECED", "1")
        try:
            with patch("luma.cli._start_detached_manager_update") as detached, patch(
                "luma.cli._refresh_manager_control"
            ) as refresh, patch("luma.cli._try_refresh_manager_agent"):
                code = main(["update", "manager", "--detach"])
        finally:
            _restore_env("LUMA_UPDATE_DETACHED", old_detached)
            _restore_env("LUMA_UPDATE_REEXECED", old_reexec)

        self.assertEqual(code, 0)
        detached.assert_not_called()
        refresh.assert_called_once()

    def test_detached_manager_update_uses_new_session_private_log_and_status_file(self):
        from luma.cli import _start_detached_manager_update

        process = Mock(pid=4321)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_STATE_HOME": tmp}, clear=False
        ), patch("luma.cli._current_luma_command", return_value=["/opt/luma/bin/luma"]), patch(
            "luma.cli._manager_update_needs_transient_unit", return_value=False
        ), patch(
            "luma.cli.subprocess.Popen", return_value=process
        ) as popen, patch("builtins.print"):
            code = _start_detached_manager_update(
                Mock(_raw_argv=["update", "manager", "--detach", "--install-ref", "deadbeef"])
            )
            logs = list((Path(tmp) / "luma" / "updates").glob("*.log"))
            log_mode = logs[0].stat().st_mode & 0o777

        self.assertEqual(code, 0)
        self.assertEqual(len(logs), 1)
        self.assertEqual(log_mode, 0o600)
        invocation = popen.call_args.args[0]
        self.assertEqual(invocation[-5:], ["update", "manager", "--detach", "--install-ref", "deadbeef"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        detached_env = popen.call_args.kwargs["env"]
        self.assertEqual(detached_env["LUMA_UPDATE_DETACHED"], "1")
        self.assertTrue(detached_env["LUMA_UPDATE_STATUS_PATH"].endswith(".status"))

    def test_detached_manager_update_escapes_node_agent_systemd_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": tmp,
                "INVOCATION_ID": "agent-service-invocation",
                "LUMA_CONTROL_IMAGE": "ghcr.io/example/luma-control:v9",
                "CLOUDFLARE_API_TOKEN": "secret",
            },
            clear=False,
        ), patch("luma.cli._current_luma_command", return_value=["/home/tao/.local/bin/luma"]), patch(
            "luma.cli._manager_update_needs_transient_unit", return_value=True
        ), patch("luma.cli.subprocess.run") as run, patch("builtins.print"):
            from luma.cli import _start_detached_manager_update

            code = _start_detached_manager_update(
                Mock(_raw_argv=["update", "manager", "--detach", "--install-ref", "deadbeef"])
            )

        self.assertEqual(code, 0)
        invocation = run.call_args.args[0]
        self.assertEqual(invocation[0], "systemd-run")
        self.assertIn("--collect", invocation)
        self.assertIn("--no-block", invocation)
        self.assertIn("--setenv=LUMA_UPDATE_DETACHED=1", invocation)
        self.assertIn("--setenv=LUMA_CONTROL_IMAGE=ghcr.io/example/luma-control:v9", invocation)
        self.assertNotIn("--setenv=CLOUDFLARE_API_TOKEN=secret", invocation)
        self.assertIn("/home/tao/.local/bin/luma", invocation)

    def test_detach_is_rejected_for_fleet_update(self):
        with patch("luma.cli._run_luma_installer") as installer:
            code = main(["update", "--detach", "fleet"])

        self.assertEqual(code, 1)
        installer.assert_not_called()

    def test_update_manager_infers_cloudflare_dns_before_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "nodes": {
                            "manager": {
                                "host": "localhost",
                                "publicIp": "203.0.113.10",
                                "region": "cn",
                                "roles": ["swarm-manager", "edge"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            state = {"clusterId": "luma-test", "domain": "luma.itool.tech", "deployToken": "deploy", "joinToken": "join"}
            old_cf = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            old_target = _set_env("LUMA_DNS_EDGE_TARGET", "")
            try:
                captured = {}

                def refresh_side_effect(config, _node, _domain, current_state, **_kwargs):
                    captured["config"] = config.raw
                    captured["state"] = dict(current_state)
                    return ["Control refreshed"]

                def find_zone_side_effect(_config, zone_name):
                    if zone_name == "itool.tech":
                        return {"id": "zone-itool"}
                    raise LumaError("not found")

                with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch(
                    "luma.cli._existing_control_state", return_value=state
                ), patch("luma.cli.find_zone", side_effect=find_zone_side_effect), patch(
                    "luma.cli.refresh_manager_control_local", side_effect=refresh_side_effect
                ), patch("builtins.print"):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "update",
                            "manager",
                            "--domain",
                            "luma.itool.tech",
                            "--node",
                            "manager",
                        ]
                    )

                self.assertEqual(code, 0)
                dns = captured["config"]["providers"]["dns"]
                self.assertEqual(dns["type"], "cloudflare")
                self.assertEqual(dns["zone"], "itool.tech")
                self.assertEqual(dns["zoneId"], "zone-itool")
                self.assertEqual(dns["edgeTarget"], "203.0.113.10")
                self.assertEqual(captured["state"]["secrets"]["CLOUDFLARE_API_TOKEN"], "cf-token")
                saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["providers"]["dns"]["zoneId"], "zone-itool")
                self.assertEqual(saved["providers"]["dns"]["edgeTarget"], "203.0.113.10")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_cf)
                _restore_env("LUMA_DNS_EDGE_TARGET", old_target)

    def test_update_infers_domain_and_refreshes_when_manager_state_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "control.json").write_text(
                json.dumps({"clusterId": "luma-test", "domain": "luma.example.com"}) + "\n",
                encoding="utf-8",
            )
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(state_dir))
            try:
                with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
                    "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
                ) as refresh:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=True)
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2], "luma.example.com")

    def test_linux_managed_nfs_prepare_reuses_identical_export_path(self):
        from luma.agent import _linux_prepare_nfs_command

        command = _linux_prepare_nfs_command("lae-staging-runtime-nfs", "/srv/luma")
        self.assertIn("glob.glob('/etc/exports.d/luma-*.exports')", command)
        self.assertIn("if export_line in lines", command)
        self.assertIn("target.unlink(missing_ok=True)", command)
        self.assertIn("already exists with different options", command)

    def test_managed_volume_path_is_writable_by_arbitrary_container_uid(self):
        from luma.agent import _volume_path_command

        command = _volume_path_command("/srv/luma/lae/tenant/app/volume")

        self.assertIn("install -d -m 0777", command)
        self.assertIn("chmod 0777", command)

    def test_update_refreshes_manager_even_when_control_version_matches_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "control.json").write_text(
                json.dumps({"clusterId": "luma-test", "domain": "luma.example.com"}) + "\n",
                encoding="utf-8",
            )
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(state_dir))
            try:
                with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
                    "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
                ) as refresh, patch("builtins.print") as printed:
                    code = main(["update"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=True)
        refresh.assert_called_once()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Manager control-plane refresh required", printed_text)
        self.assertIn("local manager control state found", printed_text)

    def test_update_joined_node_skips_agent_refresh_when_control_is_too_old(self):
        with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch("luma.cli._local_agent_config", return_value=None), patch("luma.cli._safe_local_nomad_node_id", return_value="node-1"), patch(
            "luma.cli._control_context",
            return_value=("https://luma.example.com", "management-token", False, None),
        ), patch(
            "luma.cli._refresh_local_node_agent",
            side_effect=LumaError("control API does not support node-agent credentials yet. Update the manager control plane first."),
        ), patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=False)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[info] Role: joined node", printed_text)
        self.assertIn("[skip] Luma node agent skipped", printed_text)
        self.assertIn("[ok] Joined node update complete", printed_text)

    def test_update_joined_node_skips_agent_refresh_when_node_is_unregistered(self):
        with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch("luma.cli._local_agent_config", return_value=None), patch("luma.cli._safe_local_nomad_node_id", return_value="stale-node-id"), patch(
            "luma.cli._control_context",
            return_value=("https://luma.example.com", "management-token", False, None),
        ), patch(
            "luma.cli._refresh_local_node_agent",
            side_effect=LumaError("control API error 400: {\"error\": \"nodeName or nodeId must match a registered node\"}"),
        ), patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=False)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[info] Role: joined node", printed_text)
        self.assertIn("[skip] Luma node agent skipped", printed_text)
        self.assertIn("nodeName or nodeId must match a registered node", printed_text)
        self.assertIn("[ok] Joined node update complete", printed_text)

    def test_update_joined_node_prefers_explicit_control_token_over_local_agent_config(self):
        with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch(
            "luma.cli._local_agent_config",
            return_value={
                "endpoint": "https://luma.example.com",
                "token": "stale-agent-token",
                "nodeName": "home-mac-mini",
                "nodeId": "old-node-id",
            },
        ), patch("luma.cli._safe_local_nomad_node_id", return_value="new-node-id"), patch(
            "luma.cli._control_context",
            return_value=("https://luma.example.com", "join-token", False, None),
        ) as context, patch(
            "luma.cli._refresh_local_node_agent"
        ) as refresh:
            code = main(["update", "--control-url", "https://luma.example.com", "--token", "join-token"])

        self.assertEqual(code, 0)
        context.assert_called_once()
        refresh.assert_called_once_with(
            endpoint="https://luma.example.com",
            token="join-token",
            insecure=False,
            resolve_ip=None,
            allow_skip=False,
        )

    def test_update_after_reexec_skips_installer_and_refreshes_manager(self):
        old_reexec = _set_env("LUMA_UPDATE_REEXECED", "1")
        try:
            with patch("luma.cli._run_luma_installer") as installer, patch(
                "luma.cli._manager_refresh_decision", return_value=(True, "local manager control state found")
            ), patch("luma.cli._refresh_manager_control") as refresh, patch("luma.cli._try_refresh_manager_agent"), patch("builtins.print") as printed:
                code = main(["update"])
        finally:
            _restore_env("LUMA_UPDATE_REEXECED", old_reexec)

        self.assertEqual(code, 0)
        installer.assert_not_called()
        refresh.assert_called_once()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("[skip] Luma CLI already updated in this run", printed_text)
        self.assertIn("[ok] Manager update complete", printed_text)

    def test_update_without_manager_state_updates_cli_only(self):
        with patch("luma.cli._existing_control_state", return_value=None), patch(
            "luma.cli._run_luma_installer"
        ) as installer, patch("luma.cli._reexec_after_luma_update"), patch("luma.cli.refresh_manager_control_local") as refresh, patch("builtins.print") as printed:
            code = main(["update"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref=None, skip_node_agent_refresh=False)
        refresh.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("CLI updated", printed_text)
        self.assertIn("Manager control-plane refresh skipped", printed_text)

    def test_update_fleet_updates_local_cli_and_remote_nodes_with_install_ref(self):
        client = Mock()
        client.update_fleet.return_value = {
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "results": [{"nodeName": "home-mac-mini", "region": "home", "os": "darwin", "status": "succeeded", "message": "Luma installer finished"}],
        }
        with patch("luma.cli._run_luma_installer") as installer, patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch(
            "luma.cli._control_context", return_value=("https://luma.example.com", "management-token", False, None)
        ), patch(
            "luma.cli.ControlClient", return_value=client
        ), patch("builtins.print"):
            code = main(["update", "fleet", "--install-ref", "main", "--timeout", "120"])

        self.assertEqual(code, 0)
        installer.assert_called_once_with(install_ref="main", skip_node_agent_refresh=False)
        client.update_fleet.assert_called_once_with(install_ref="main", include_all=False, include_manager=False, timeout=120)

    def test_update_fleet_include_manager_flag_is_explicit(self):
        client = Mock()
        client.update_fleet.return_value = {"succeeded": 0, "failed": 0, "skipped": 0, "results": []}
        with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._manager_refresh_decision", return_value=(False, "no local manager control state found")
        ), patch(
            "luma.cli._control_context", return_value=("https://luma.example.com", "management-token", False, None)
        ), patch(
            "luma.cli.ControlClient", return_value=client
        ), patch("builtins.print"):
            code = main(["update", "fleet", "--include-manager"])

        self.assertEqual(code, 0)
        client.update_fleet.assert_called_once_with(install_ref="", include_all=False, include_manager=True, timeout=900)

    def test_update_manager_rejects_bootstrap_only_options(self):
        with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch(
            "luma.cli._existing_control_state", return_value={"domain": "luma.example.com"}
        ), patch("luma.cli.refresh_manager_control_local") as refresh:
            code = main(["update", "manager", "--domain", "luma.example.com", "--http-port", "8080"])

        self.assertEqual(code, 1)
        refresh.assert_not_called()

    def test_update_manager_does_not_call_full_bootstrap_paths(self):
        state = {"clusterId": "luma-test", "domain": "luma.example.com", "deployToken": "deploy", "joinToken": "join"}
        with patch("luma.cli._run_luma_installer"), patch("luma.cli._reexec_after_luma_update"), patch("luma.cli._existing_control_state", return_value=state), patch(
            "luma.cli.refresh_manager_control_local", return_value=["Control refreshed"]
        ), patch("luma.cli.bootstrap_manager_local") as bootstrap_manager, patch("luma.cli.bootstrap_node") as bootstrap_node_call, patch(
            "luma.cli.install_docker"
        ) as docker, patch("luma.cli.setup_egress") as egress:
            code = main(["update", "manager", "--domain", "luma.example.com"])

        self.assertEqual(code, 0)
        bootstrap_manager.assert_not_called()
        bootstrap_node_call.assert_not_called()
        docker.assert_not_called()
        egress.assert_not_called()

    def test_service_restart_exposes_recreate_and_task_modes(self):
        client = Mock()
        client.restart_application.return_value = {
            "stack": "granary",
            "service": "mysql",
            "mode": "task",
            "restarted": [{"allocId": "alloc-1", "task": "mysql", "mode": "task"}],
        }
        with patch(
            "luma.cli._control_context",
            return_value=("https://luma.example.com", "deploy-token", False, None),
        ), patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
            code = main(["service", "restart", "granary", "--service", "mysql", "--mode", "task", "--timeout", "45"])

        self.assertEqual(code, 0)
        client.restart_application.assert_called_once_with(stack="granary", service="mysql", mode="task", timeout=45)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Restart finished: granary/mysql (task)", printed_text)

    def test_version_local_skips_control_check(self):
        with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print") as printed:
            code = main(["version", "--local"])

        self.assertEqual(code, 0)
        client_cls.assert_not_called()
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn(f"Luma CLI: {__version__}", printed_text)
        self.assertNotIn("Luma Control", printed_text)

    def test_version_prints_cli_without_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                with patch("builtins.print") as printed:
                    code = main(["version"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn(f"Luma CLI: {__version__}", printed_text)
        self.assertIn("Luma Control: not checked", printed_text)

    def test_version_prints_control_health_from_explicit_url(self):
        client = Mock()
        client.health.return_value = {
            "version": "0.1.0",
            "nodeJoinModel": "region-first",
            "capabilities": ["node-region", "service-proxy"],
        }
        with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
            code = main(["version", "--control-url", "https://luma.example.com"])

        self.assertEqual(code, 0)
        client_cls.assert_called_once_with("https://luma.example.com", "health", insecure=False, resolve_ip=None)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Luma Control: 0.1.0", printed_text)
        self.assertIn("Node join model: region-first", printed_text)
        self.assertIn("Capabilities: node-region, service-proxy", printed_text)

    def test_status_prints_control_plane_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.status.return_value = {
                    "clusterId": "luma-test",
                    "version": "0.1.2",
                    "configPath": "/opt/luma/luma.yaml",
                    "dns": {
                        "provider": "cloudflare",
                        "zone": "example.com",
                        "zoneIdConfigured": True,
                        "tokenEnv": "CLOUDFLARE_API_TOKEN",
                        "tokenConfigured": True,
                        "target": "203.0.113.10",
                        "ready": True,
                    },
                    "storage": {
                        "storageClasses": [
                            {
                                "name": "cn-nfs",
                                "provider": "nfs",
                                "mode": "managed",
                                "node": "manager",
                                "path": "/srv/luma",
                                "regions": ["cn"],
                            }
                        ]
                    },
                    "nodes": {
                        "registered": 2,
                        "items": [
                            {"name": "manager", "region": "cn", "status": "labeled", "displayName": "manager"},
                            {"name": "docker-home", "region": "home", "status": "labeled", "displayName": "mini-gaojiu"},
                        ],
                    },
                    "nomad": {
                        "available": True,
                        "leader": "100.64.0.1:4647",
                        "nodes": [
                            {
                                "hostname": "manager",
                                "lumaNode": "manager",
                                "role": "client",
                                "state": "ready",
                                "availability": "eligible",
                                "region": "cn",
                                "leader": True,
                            },
                            {
                                "hostname": "docker-home",
                                "lumaNode": "docker-home",
                                "role": "client",
                                "state": "ready",
                                "availability": "eligible",
                                "region": "home",
                                "leader": False,
                            },
                        ],
                    },
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["status"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Luma status", printed_text)
        self.assertIn("Control", printed_text)
        self.assertIn("Cluster  luma-test", printed_text)
        self.assertIn("Version  0.1.2", printed_text)
        self.assertIn("DNS", printed_text)
        self.assertIn("Ready     yes", printed_text)
        self.assertIn("Provider  cloudflare", printed_text)
        self.assertIn("Orchestrator (Nomad)", printed_text)
        self.assertIn("Storage", printed_text)
        self.assertIn("Summary: storageClasses=1", printed_text)
        self.assertIn("cn-nfs", printed_text)
        self.assertIn("/srv/luma", printed_text)
        self.assertIn("Nodes", printed_text)
        self.assertIn("Summary: registered=2, nomad=2", printed_text)
        self.assertIn("NAME         REGION", printed_text)
        self.assertIn("docker-home  home    labeled     ready  client", printed_text)
        self.assertIn("manager      cn      labeled     ready  client", printed_text)

    def test_status_prints_dns_missing_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.status.return_value = {
                    "clusterId": "luma-test",
                    "version": "0.1.2",
                    "configPath": "/opt/luma/luma.yaml",
                    "dns": {
                        "provider": "not configured",
                        "zone": "",
                        "zoneIdConfigured": False,
                        "tokenEnv": "CLOUDFLARE_API_TOKEN",
                        "tokenConfigured": True,
                        "target": "",
                        "ready": False,
                        "missing": ["provider", "zoneId", "target"],
                    },
                    "portainer": {
                        "apiUrl": "https://100.64.0.1:9443/api",
                        "endpointIdConfigured": True,
                        "swarmIdConfigured": True,
                        "ready": True,
                    },
                    "storage": {"storageClasses": []},
                    "nodes": {"registered": 0, "items": []},
                    "swarm": {"available": True, "nodes": []},
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["status"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("Ready     no", printed_text)
        self.assertIn("Provider  not configured", printed_text)
        self.assertIn("Missing   provider, zoneId, target", printed_text)

    def test_status_uses_env_context_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            old_insecure = _set_env("LUMA_INSECURE", "false")
            old_resolve = _set_env("LUMA_RESOLVE_IP", "")
            try:
                client = Mock()
                client.status.return_value = {"clusterId": "luma-test", "version": "0.1.2"}
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print") as printed:
                    code = main(["--no-env", "status", "--format", "json"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)
                _restore_env("LUMA_INSECURE", old_insecure)
                _restore_env("LUMA_RESOLVE_IP", old_resolve)

        self.assertEqual(code, 0)
        client_cls.assert_called_once_with("https://luma.example.com", "deploy-token", insecure=False, resolve_ip=None)
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "status")
        self.assertEqual(payload["result"]["clusterId"], "luma-test")

    def test_status_cli_context_overrides_env_and_login_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://env.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "env-token")
            try:
                save_context(endpoint="https://context.example.com", cluster_id="luma-test", token="context-token")
                client = Mock()
                client.status.return_value = {"clusterId": "luma-test"}
                with patch("luma.cli.ControlClient", return_value=client) as client_cls, patch("builtins.print"):
                    code = main(
                        [
                            "--no-env",
                            "status",
                            "--control-url",
                            "https://cli.example.com",
                            "--token",
                            "cli-token",
                            "--format",
                            "json",
                        ]
                    )
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)

        self.assertEqual(code, 0)
        client_cls.assert_called_once_with("https://cli.example.com", "cli-token", insecure=False, resolve_ip=None)

    def test_status_json_reports_invalid_env_bool(self):
        old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
        old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
        old_insecure = _set_env("LUMA_INSECURE", "sometimes")
        try:
            with patch("luma.cli.ControlClient") as client_cls, patch("builtins.print") as printed:
                code = main(["--no-env", "status", "--format", "json"])
        finally:
            _restore_env("LUMA_CONTROL_URL", old_url)
            _restore_env("LUMA_DEPLOY_TOKEN", old_token)
            _restore_env("LUMA_INSECURE", old_insecure)

        self.assertEqual(code, 1)
        client_cls.assert_not_called()
        payload = json.loads(printed.call_args_list[-1].args[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "luma_error")
        self.assertIn("LUMA_INSECURE", payload["error"]["message"])

    def test_secret_and_registry_list_use_env_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            try:
                secret_client = Mock()
                secret_client.list_secrets.return_value = {"secrets": ["DATABASE_URL"]}
                registry_client = Mock()
                registry_client.list_registries.return_value = {"registries": [{"host": "ghcr.io", "username": "bot"}]}
                with patch("luma.cli.ControlClient", side_effect=[secret_client, registry_client]) as client_cls, patch(
                    "builtins.print"
                ) as printed:
                    secret_code = main(["--no-env", "secret", "list", "--format", "json"])
                    registry_code = main(["--no-env", "registry", "list", "--format", "json"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)

        self.assertEqual(secret_code, 0)
        self.assertEqual(registry_code, 0)
        self.assertEqual(client_cls.call_count, 2)
        for call in client_cls.call_args_list:
            self.assertEqual(call.args[:2], ("https://luma.example.com", "deploy-token"))
        payloads = [json.loads(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(payloads[0]["result"]["secrets"], ["DATABASE_URL"])
        self.assertEqual(payloads[1]["result"]["registries"][0]["host"], "ghcr.io")

    def test_secret_and_registry_writes_use_env_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            old_url = _set_env("LUMA_CONTROL_URL", "https://luma.example.com")
            old_token = _set_env("LUMA_DEPLOY_TOKEN", "deploy-token")
            try:
                secret_client = Mock()
                secret_client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                registry_client = Mock()
                registry_client.set_registry.return_value = {"host": "ghcr.io"}
                with patch("luma.cli.ControlClient", side_effect=[secret_client, registry_client]) as client_cls, patch(
                    "sys.stdin", io.StringIO("registry-token\n")
                ), patch("builtins.print"):
                    secret_code = main(["--no-env", "secret", "set", "DATABASE_URL", "--value", "postgres://secret"])
                    registry_code = main(["--no-env", "registry", "login", "ghcr.io", "--username", "bot", "--password-stdin"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)
                _restore_env("LUMA_CONTROL_URL", old_url)
                _restore_env("LUMA_DEPLOY_TOKEN", old_token)

        self.assertEqual(secret_code, 0)
        self.assertEqual(registry_code, 0)
        self.assertEqual(client_cls.call_count, 2)
        for call in client_cls.call_args_list:
            self.assertEqual(call.args[:2], ("https://luma.example.com", "deploy-token"))
        secret_client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
        registry_client.set_registry.assert_called_once_with(host="ghcr.io", username="bot", password="registry-token")

    def test_node_status_accepts_node_alias_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.status.return_value = {
                    "clusterId": "luma-test",
                    "nodes": {
                        "registered": 2,
                        "items": [
                            {
                                "name": "gaojiu",
                                "displayName": "gaojiu",
                                "aliases": ["home-mac-mini"],
                                "region": "home",
                                "status": "labeled",
                                "agentStatus": "ready",
                                "agentOs": "darwin",
                                "agentLastSeen": 0,
                            },
                            {
                                "name": "lab",
                                "displayName": "lab",
                                "region": "home",
                                "status": "labeled",
                                "agentStatus": "ready",
                                "agentOs": "linux",
                                "agentLastSeen": 0,
                            },
                        ],
                    },
                    "nomad": {
                        "available": True,
                        "nodes": [
                            {"name": "gaojiu", "lumaNode": "gaojiu", "hostname": "Mac.lan"},
                            {"name": "lab", "lumaNode": "lab", "hostname": "ubuntu"},
                        ],
                    },
                }
                with patch("luma.cli.ControlClient", return_value=client), patch("builtins.print") as printed:
                    code = main(["node", "status", "home-mac-mini"])
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

        self.assertEqual(code, 0)
        printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        self.assertIn("gaojiu", printed_text)
        self.assertNotIn("\n  lab", printed_text)

    def test_node_exit_cleans_local_nomad_and_runtime_state(self):
        remote = Mock()
        remote.sudo_result.return_value = Mock(code=0, output="stopped\n")
        remote.sudo.return_value = ""

        with patch("luma.cli.LocalExecutor", return_value=remote):
            results = exit_local_node()

        self.assertEqual(results, ["Nomad agent stopped", "Removed /opt/luma"])
        self.assertIn("nomad", remote.sudo_result.call_args.args[0])
        remote.sudo.assert_called_once_with("rm -rf /opt/luma")

    def test_node_exit_optional_deep_cleanup(self):
        remote = Mock()
        remote.sudo_result.side_effect = [
            Mock(code=0, output="skipped\n"),
            Mock(code=0, output="done\n"),
            Mock(code=0, output="done\n"),
        ]
        remote.sudo.return_value = ""

        with patch("luma.cli.LocalExecutor", return_value=remote):
            results = exit_local_node(tailscale=True, prune_docker=True)

        self.assertEqual(
            results,
            ["Nomad agent stop skipped", "Removed /opt/luma", "Tailscale logged out", "Docker pruned"],
        )
        commands = [call.args[0] for call in remote.sudo_result.call_args_list]
        self.assertTrue(any("tailscale logout" in command for command in commands))
        prune_commands = [command for command in commands if "docker system prune -af --volumes" in command]
        self.assertEqual(len(prune_commands), 1)
        self.assertIn("/opt/luma/events/node-exit.jsonl", prune_commands[0])
        self.assertIn("logger -t luma", prune_commands[0])

    def test_node_join_prompts_for_worker_config_during_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                secret_values = iter(["ts-key", "sudo-pass"])
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "worker-1",
                    "region": "global",
                    "nomadRpcAddr": "100.64.0.1:4647",
                }
                client.label_node.return_value = {"message": "labels applied"}
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", return_value="Docker available"
                ), patch(
                    "luma.cli.ControlClient", return_value=client
                ), patch("luma.cli.install_nomad_node", return_value=[]), patch(
                    "luma.cli.local_nomad_node_info", return_value=("worker-1", "node-id-1")
                ), patch(
                    "luma.cli._local_tailscale_ip", return_value="100.64.0.10"
                ):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "global-sg-1",
                        ]
                    )
                self.assertEqual(code, 0)
                client.register_node.assert_called_once_with(node_name="global-sg-1", region="global")
                client.label_node.assert_called_once_with(
                    node_name="worker-1",
                    region="global",
                    registered_name="global-sg-1",
                    node_id="node-id-1",
                    tailscale_ip="100.64.0.10",
                )
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
                self.assertIn("LUMA_SUDO_PASSWORD", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_home_node_join_requires_tailscale_key_when_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value=""
                ), patch("luma.cli._local_tailscale_connected", return_value=False), patch(
                    "luma.cli.ControlClient"
                ) as client_cls, patch("builtins.print"):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "home",
                            "--name",
                            "home-mac-mini",
                        ]
                    )
                self.assertEqual(code, 1)
                client_cls.assert_not_called()
                self.assertNotIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_home_node_join_prompts_for_required_tailscale_key_before_registering(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                secret_values = iter(["ts-key", "sudo-pass"])
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "home-mac-mini",
                    "region": "home",
                    "nomadRpcAddr": "100.64.0.1:4647",
                }
                client.label_node.return_value = {"message": "labels applied"}
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", side_effect=lambda _prompt: next(secret_values)
                ), patch("luma.cli._local_tailscale_connected", side_effect=[False, False]), patch(
                    "luma.cli.configure_dns", return_value="DNS ok"
                ), patch("luma.cli.install_docker", return_value="Docker available"
                ), patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.install_nomad_node", return_value=[]
                ), patch("luma.cli.local_nomad_node_info", return_value=("docker-home", "home-id-1")
                ), patch(
                    "luma.cli._local_tailscale_ip", return_value="100.64.0.20"
                ):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "home",
                            "--name",
                            "home-mac-mini",
                        ]
                    )
                self.assertEqual(code, 0)
                client.register_node.assert_called_once_with(node_name="home-mac-mini", region="home")
                client.label_node.assert_called_once_with(
                    node_name="docker-home",
                    region="home",
                    registered_name="home-mac-mini",
                    node_id="home-id-1",
                    tailscale_ip="100.64.0.20",
                )
                self.assertIn("TAILSCALE_AUTHKEY", configured_keys(config_path))
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_nomad_node_join_labels_and_installs_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "bot",
                    "region": "global",
                    "nomadRpcAddr": "100.113.204.125:4647",
                }
                client.label_node.return_value = {
                    "message": "labels applied",
                    "agentToken": "agent-token",
                    "nodeName": "bot",
                }
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", return_value="Docker available"
                ), patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.install_nomad_node", return_value=[]
                ) as install_nomad, patch(
                    "luma.cli.local_nomad_node_info", return_value=("bot-host", "nomad-node-id")
                ), patch(
                    "luma.cli._local_tailscale_ip", return_value="100.80.0.20"
                ), patch(
                    "luma.cli._install_node_agent_from_token"
                ) as install_agent:
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "bot",
                            "--engine",
                            "nomad",
                        ]
                    )

                self.assertEqual(code, 0)
                client.register_node.assert_called_once_with(node_name="bot", region="global")
                install_nomad.assert_called_once()
                install_kwargs = install_nomad.call_args.kwargs
                self.assertEqual(install_kwargs["server_addrs"], ["100.113.204.125:4647"])
                self.assertIsNone(install_kwargs["egress_proxy"])
                client.label_node.assert_called_once_with(
                    node_name="bot-host",
                    region="global",
                    registered_name="bot",
                    node_id="nomad-node-id",
                    tailscale_ip="100.80.0.20",
                )
                install_agent.assert_called_once()
                self.assertEqual(install_agent.call_args.kwargs["agent_token"], "agent-token")
                self.assertEqual(install_agent.call_args.kwargs["node_id"], "nomad-node-id")
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_checks_docker_before_registering(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                client = Mock()
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", side_effect=LumaError("Docker is not ready")
                ), patch("luma.cli.ControlClient", return_value=client), patch("builtins.print"):
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "global-sg-1",
                        ]
                    )

                self.assertEqual(code, 1)
                client.register_node.assert_not_called()
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_unregisters_when_local_nomad_join_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_config = _set_env("LUMA_USER_CONFIG", str(config_path))
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                client = Mock()
                client.register_node.return_value = {
                    "nodeName": "global-sg-1",
                    "region": "global",
                    "nomadRpcAddr": "100.64.0.1:4647",
                }
                with patch("sys.stdin.isatty", return_value=True), patch(
                    "luma.userconfig.getpass.getpass", return_value="sudo-pass"
                ), patch("luma.cli.configure_dns", return_value="DNS ok"), patch(
                    "luma.cli.install_docker", return_value="Docker available"
                ), patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.install_nomad_node", side_effect=LumaError("nomad join failed")
                ), patch("builtins.print") as printed:
                    code = main(
                        [
                            "node",
                            "join",
                            "https://luma.example.com",
                            "--token",
                            "join-token",
                            "--region",
                            "global",
                            "--name",
                            "global-sg-1",
                        ]
                    )

                self.assertEqual(code, 1)
                client.unregister_node.assert_called_once_with(node_name="global-sg-1")
                output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertIn("[ok] Rolled back node registration: global-sg-1", output)
            finally:
                _restore_env("LUMA_USER_CONFIG", old_config)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_node_join_rejects_profile_argument(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.print"), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "node",
                    "join",
                    "https://luma.example.com",
                    "--token",
                    "join-token",
                    "--profile",
                    "home-node",
                    "--region",
                    "home",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_macos_tailscale_uses_authkey_when_available(self):
        node = LumaConfig({"nodes": {"mini": {"host": "localhost", "region": "home"}}}, None).get_node("mini")
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=0, output="/usr/local/bin/tailscale\n"),
            Mock(code=1, output="not logged in\n"),
            Mock(code=0, output=""),
        ]

        results = setup_tailscale(node, authkey="ts-key", executor=remote)

        self.assertEqual(results, ["Tailscale connected: luma-mini"])
        commands = [call.args[0] for call in remote.run_result.call_args_list]
        self.assertTrue(any("tailscale up" in command and "--authkey ts-key" in command for command in commands))
        self.assertTrue(any("tailscale up" in command and "--accept-routes" in command for command in commands))

    def test_tailscale_up_retries_with_reset_for_existing_nondefault_flags(self):
        node = LumaConfig({"nodes": {"mini": {"host": "localhost", "region": "home"}}}, None).get_node("mini")
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = ""
        remote.sudo_result.side_effect = [
            Mock(code=1, output="not logged in\n"),
            Mock(
                code=1,
                output=(
                    "Error: changing settings via 'tailscale up' requires mentioning all "
                    "non-default flags. tailscale up --auth-key=ts-key --accept-routes\n"
                ),
            ),
            Mock(code=0, output=""),
        ]

        results = setup_tailscale(node, authkey="ts-key", executor=remote)

        self.assertEqual(results, ["Tailscale installed", "Tailscale connected: luma-mini"])
        commands = [call.args[0] for call in remote.sudo_result.call_args_list]
        self.assertIn("tailscale status", commands[0])
        self.assertIn("tailscale up", commands[1])
        self.assertNotIn("--reset", commands[1])
        self.assertIn("--accept-routes", commands[1])
        self.assertIn("tailscale up", commands[2])
        self.assertIn("--reset", commands[2])
        self.assertIn("--accept-routes", commands[2])

    def test_linux_tailscale_reports_already_installed_when_binary_exists(self):
        node = LumaConfig({"nodes": {"mini": {"host": "localhost", "region": "home"}}}, None).get_node("mini")
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo.return_value = "luma_tailscale_present\n"
        remote.sudo_result.return_value = Mock(code=0, output="")

        results = setup_tailscale(node, executor=remote)

        self.assertEqual(results, ["Tailscale already installed", "Tailscale already logged in"])
        command = remote.sudo.call_args.args[0]
        self.assertIn("command -v tailscale", command)
        self.assertIn("curl -fsSL https://tailscale.com/install.sh | sh", command)

    def test_install_nomad_node_skips_binary_download_when_pinned_version_exists(self):
        node = LumaConfig({"nodes": {"worker": {"host": "localhost", "region": "cn"}}}, None).get_node("worker")
        remote = Mock()
        remote.sudo.side_effect = [
            "luma_nomad_binary_present\n",
            "",
            "",
            "",
        ]
        uname = Mock(machine="x86_64")
        with patch("luma.bootstrap.LocalExecutor", return_value=remote), patch(
            "luma.bootstrap.setup_tailscale", return_value=["Tailscale already logged in"]
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.80.0.20"), patch(
            "luma.nomad_node.detect_os", return_value="linux"
        ), patch("luma.nomad_node.detect_cpu_total_compute", return_value=None), patch(
            "luma.bootstrap.os.uname", return_value=uname
        ), patch(
            "luma.bootstrap.verify_local_nomad_node", return_value="Nomad agent ready"
        ):
            results = install_nomad_node(
                node,
                role="client",
                region="cn",
                node_name="worker",
                server_addrs=["100.113.204.125:4647"],
                install_docker_first=False,
            )

        self.assertIn("Nomad binary already installed", results)
        self.assertIn("Nomad config written", results)
        self.assertIn("Nomad agent started", results)
        binary_command = remote.sudo.call_args_list[0].args[0]
        self.assertIn("nomad version", binary_command)
        self.assertIn("grep -Eq", binary_command)
        self.assertLess(binary_command.index("nomad version"), binary_command.index("curl -fsSL"))

    def test_noninteractive_config_skips_missing_optional_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            old_sudo = _set_env("LUMA_SUDO_PASSWORD", "")
            try:
                with patch("sys.stdin.isatty", return_value=False):
                    result = ensure_interactive_config("worker", path=config_path)
                self.assertIsNone(result)
                self.assertFalse(config_path.exists())
            finally:
                _restore_env("TAILSCALE_AUTHKEY", old_ts)
                _restore_env("LUMA_SUDO_PASSWORD", old_sudo)

    def test_noninteractive_config_still_requires_required_values(self):
        old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
        try:
            with patch("sys.stdin.isatty", return_value=False):
                with self.assertRaises(LumaError) as ctx:
                    ensure_interactive_config("manager", keys=["CLOUDFLARE_API_TOKEN"])
            self.assertIn("CLOUDFLARE_API_TOKEN", str(ctx.exception))
        finally:
            _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_user_config_loads_missing_or_empty_env_without_overriding_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".luma.config.json"
            config_path.write_text(
                '{"version":1,"env":{"CLOUDFLARE_API_TOKEN":"from-config","TAILSCALE_AUTHKEY":"ts-key"}}\n',
                encoding="utf-8",
            )
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "from-env")
            old_ts = _set_env("TAILSCALE_AUTHKEY", "")
            try:
                load_user_config(config_path)
                import os

                self.assertEqual(os.environ["CLOUDFLARE_API_TOKEN"], "from-env")
                self.assertEqual(os.environ["TAILSCALE_AUTHKEY"], "ts-key")
            finally:
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)
                _restore_env("TAILSCALE_AUTHKEY", old_ts)

    def test_deploy_without_context_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            try:
                code = main(["deploy", str(service_path), "--skip-dns", "--skip-orchestrator"])
                self.assertEqual(code, 1)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_deploy_env_file_must_exist_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_home = _set_env("LUMA_CONFIG_HOME", str(root / "home"))
            service_path = root / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                ),
                encoding="utf-8",
            )
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                with patch("luma.cli.ControlClient", return_value=client):
                    code = main(["deploy", str(service_path), "--env", str(root / "missing.env")])
                self.assertEqual(code, 1)
                client.deploy_events.assert_not_called()
                client.deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_control_client_requires_https(self):
        with self.assertRaises(Exception):
            ControlClient("http://luma.example.com", "secret")

    def test_control_client_resolve_ip_keeps_https_endpoint_host(self):
        client = ControlClient("https://luma.example.com:8443", "secret", insecure=True, resolve_ip="203.0.113.10")
        self.assertEqual(client._request_url("/v1/health"), "https://203.0.113.10:8443/v1/health")
        self.assertEqual(client._host_header, "luma.example.com:8443")

    def test_control_client_resolve_ip_requires_insecure(self):
        with self.assertRaises(LumaError):
            ControlClient("https://luma.example.com", "secret", resolve_ip="203.0.113.10")

    def test_control_client_reports_node_api_error_directly(self):
        client = ControlClient("https://luma.example.com", "secret")
        error = urllib.error.HTTPError(
            "https://luma.example.com/v1/nodes/register",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error": "nodeName, profile, and region are required"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(LumaError) as raised:
            client.register_node(node_name="m3max", region="home")

        self.assertIn("control API error 400", str(raised.exception))
        self.assertIn("nodeName, profile, and region are required", str(raised.exception))

    def test_control_client_reports_storage_api_error_directly(self):
        client = ControlClient("https://luma.example.com", "secret")
        error = urllib.error.HTTPError(
            "https://luma.example.com/v1/storage",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error": "storage class cn-nfs endpoint is required for nfs"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(LumaError) as raised:
            client.set_storage(name="cn-nfs", provider="nfs", node="cn-node", path="/srv/luma")

        self.assertIn("control API error 400", str(raised.exception))
        self.assertIn("endpoint is required for nfs", str(raised.exception))

    def test_control_client_storage_mutations_use_operational_timeout(self):
        client = ControlClient("https://luma.example.com", "secret")
        with patch.object(client, "request", return_value={"saved": True}) as request:
            client.set_storage(
                name="cn-nfs",
                provider="nfs",
                node="cn-node",
                path="/srv/luma",
            )
        self.assertEqual(request.call_args.kwargs["timeout"], 360)

        with patch.object(client, "request", return_value={"removed": True}) as request:
            client.remove_storage(name="cn-nfs")
        self.assertEqual(request.call_args.kwargs["timeout"], 360)

    def test_control_client_reports_missing_agent_token_endpoint_as_old_control(self):
        client = ControlClient("https://luma.example.com", "secret")
        error = urllib.error.HTTPError(
            "https://luma.example.com/v1/nodes/agent-token",
            404,
            "Not Found",
            {},
            io.BytesIO(b'{"error": "not found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(LumaError) as raised:
            client.issue_agent_token(node_name="home-mac-mini", node_id="node-1")

        self.assertIn("does not support node-agent credentials", str(raised.exception))
        self.assertIn("luma update manager", str(raised.exception))

    def test_control_client_reports_timeout_without_traceback(self):
        client = ControlClient("https://luma.example.com", "secret")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")), self.assertRaises(LumaError) as raised:
            client.deploy(manifest="name: api\nimage: nginx\nregion: cn\nexposure: none\n", source_name="service.yaml", timeout=42)

        self.assertIn("control API timed out after 42s", str(raised.exception))
        self.assertIn("/v1/deployments", str(raised.exception))
        self.assertIn("manager may still be applying", str(raised.exception))

    def test_control_client_build_deploy_sends_provider_manifest_and_env(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.build_deploy(
                provider_id="gitea:lin",
                repository="acme/app",
                build_node="builder",
                manifest="name: api\nregion: cn\nexposure: none\n",
                env_secrets={"DATABASE_URL": "postgres://secret"},
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://luma.example.com/v1/builds")
        self.assertEqual(body["providerId"], "gitea:lin")
        self.assertEqual(body["repository"], "acme/app")
        self.assertEqual(body["manifest"], "name: api\nregion: cn\nexposure: none\n")
        self.assertEqual(body["envSecrets"], {"DATABASE_URL": "postgres://secret"})

    def test_control_client_cancels_repository_import_build(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"run":{"id":"build-1","status":"canceling"}}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = client.cancel_build("build-1")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://luma.example.com/v1/builds/build-1/cancel")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {})
        self.assertEqual(result["run"]["status"], "canceling")

    def test_control_client_direct_build_proxy_mode_is_capability_gated_and_explicit(self):
        client = ControlClient("https://luma.example.com", "secret")
        health_response = MagicMock()
        health_response.read.return_value = b'{"capabilities":["build-proxy-mode-v1"]}'
        health_response.__enter__.return_value = health_response
        build_response = MagicMock()
        build_response.read.return_value = b'{"ok":true}'
        build_response.__enter__.return_value = build_response

        with patch("urllib.request.urlopen", side_effect=[health_response, build_response]) as urlopen:
            client.build_deploy(repo_url="https://github.com/acme/app", proxy_mode="direct")

        request = urlopen.call_args_list[1].args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["proxyMode"], "direct")
        self.assertIn("proxy", body)
        self.assertEqual(body["proxy"], "")

    def test_control_client_direct_build_proxy_mode_rejects_older_control(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"capabilities":[]}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response), self.assertRaisesRegex(
            LumaError, "update the manager"
        ):
            client.build_deploy(repo_url="https://github.com/acme/app", proxy_mode="direct")

    def test_node_label_waits_longer_than_manager_node_discovery(self):
        client = ControlClient("https://luma.example.com", "secret")
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            client.label_node(node_name="orbstack", region="home", registered_name="mac-mini-gaojiu", node_id="node-id")

        timeout = urlopen.call_args.kwargs["timeout"]
        self.assertGreaterEqual(timeout, 120)

    def test_secret_set_sends_value_to_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                with patch("luma.cli.ControlClient", return_value=client), patch(
                    "luma.cli.getpass.getpass", return_value="postgres://secret"
                ), patch("builtins.print") as printed:
                    code = main(["secret", "set", "DATABASE_URL"])
                self.assertEqual(code, 0)
                client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("postgres://secret", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_secret_set_can_read_value_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                client = Mock()
                client.set_secret.return_value = {"name": "DATABASE_URL", "saved": True}
                with patch("luma.cli.ControlClient", return_value=client), patch(
                    "sys.stdin", io.StringIO("postgres://secret\n")
                ), patch("builtins.print") as printed:
                    code = main(["secret", "set", "DATABASE_URL", "--value-stdin"])
                self.assertEqual(code, 0)
                client.set_secret.assert_called_once_with(name="DATABASE_URL", value="postgres://secret")
                printed_text = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
                self.assertNotIn("postgres://secret", printed_text)
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)

    def test_secret_set_rejects_value_and_stdin_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = _set_env("LUMA_CONFIG_HOME", str(Path(tmp) / "home"))
            try:
                save_context(endpoint="https://luma.example.com", cluster_id="luma-test", token="deploy-token")
                with patch("luma.cli.ControlClient") as client_cls:
                    code = main(["secret", "set", "DATABASE_URL", "--value", "a", "--value-stdin"])
                self.assertEqual(code, 1)
                client_cls.return_value.set_secret.assert_not_called()
            finally:
                _restore_env("LUMA_CONFIG_HOME", old_home)


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "LUMA_TEST_VALUE=from-file",
                        "export LUMA_QUOTED='hello world'",
                        "LUMA_EMPTY=",
                    ]
                ),
                encoding="utf-8",
            )
            import os

            old_value = os.environ.get("LUMA_TEST_VALUE")
            old_quoted = os.environ.get("LUMA_QUOTED")
            old_empty = os.environ.get("LUMA_EMPTY")
            try:
                os.environ["LUMA_TEST_VALUE"] = "from-env"
                loaded = load_env_file(path)
                self.assertEqual(os.environ["LUMA_TEST_VALUE"], "from-env")
                self.assertEqual(os.environ["LUMA_QUOTED"], "hello world")
                self.assertEqual(os.environ["LUMA_EMPTY"], "")
                self.assertNotIn("LUMA_TEST_VALUE", loaded)
            finally:
                _restore_env("LUMA_TEST_VALUE", old_value)
                _restore_env("LUMA_QUOTED", old_quoted)
                _restore_env("LUMA_EMPTY", old_empty)


class NomadBootstrapTests(unittest.TestCase):
    def test_manager_config_merge_preserves_operator_providers_and_nested_defaults(self):
        existing = {
            "providers": {
                "dns": {
                    "type": "cloudflare",
                    "zone": "itool.tech",
                    "zoneId": "zone-id",
                    "apiTokenEnv": "CLOUDFLARE_API_TOKEN",
                }
            },
            "defaults": {"engine": "nomad", "routesRoot": "/opt/luma/routes"},
            "nodes": {"manager": {"host": "localhost", "roles": ["edge"]}},
        }
        incoming = {
            "defaults": {"engine": "nomad", "images": {"lumaControl": "example/control:new"}},
            "nodes": {"manager": {"host": "localhost"}},
        }

        merged = _merge_control_config(existing, incoming)

        self.assertEqual(merged["providers"], existing["providers"])
        self.assertEqual(merged["defaults"]["routesRoot"], "/opt/luma/routes")
        self.assertEqual(merged["defaults"]["images"]["lumaControl"], "example/control:new")
        self.assertEqual(merged["nodes"]["manager"]["roles"], ["edge"])

    def test_install_control_config_generates_config_without_local_path(self):
        config = LumaConfig({}, None)
        node = config.default_manager()
        if node is None:
            from luma.config import NodeConfig

            node = NodeConfig(
                name="manager",
                host="localhost",
                public_ip="127.0.0.1",
                region="cn",
                roles=["nomad-manager", "edge"],
            )
        remote = Mock()
        remote.write_secret.return_value = "Secret written: /opt/luma/luma.yaml"

        result = install_control_config(remote, config, node)

        self.assertEqual(result, "Secret written: /opt/luma/luma.yaml")
        content = remote.write_secret.call_args.args[0]
        data = yaml.safe_load(content)
        self.assertEqual(data["project"], "luma")
        self.assertEqual(data["nodes"]["manager"]["host"], "localhost")
        self.assertEqual(data["defaults"]["publicNetwork"], "public")

    def test_control_image_pull_failure_is_fatal(self):
        remote = Mock()
        remote.sudo.side_effect = Exception("pull failed")

        with self.assertRaisesRegex(LumaError, "failed to pull Luma Control image"):
            _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        remote.upload.assert_not_called()
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker image inspect" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_image_pull_retries_transient_registry_ingress_failure(self):
        remote = Mock()
        remote.sudo.side_effect = [
            Exception('Head "https://registry.example/v2/control/manifests/v1": EOF'),
            Exception("unexpected status code 502 Bad Gateway"),
            "",
        ]

        with patch("luma.bootstrap.time.sleep") as sleep:
            result = _ensure_control_image(remote, "registry.example/control:v1")

        self.assertEqual(result, "Control image pulled: registry.example/control:v1")
        self.assertEqual(remote.sudo.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2, 4])

    def test_control_image_prefetch_routes_internal_registry_direct_in_egress(self):
        remote = Mock()
        installed = yaml.safe_dump({"mixed-port": 7890, "rules": ["MATCH,EGRESS"]})
        remote.run_result.return_value = Mock(code=0, output="401")
        remote.sudo.return_value = base64.b64encode(installed.encode()).decode()
        remote.run.return_value = ""

        with patch("luma.bootstrap._docker_daemon_uses_egress_proxy", return_value=True), patch(
            "luma.bootstrap._wait_nomad_job", return_value="egress ready"
        ):
            result = _ensure_control_registry_direct_route(
                remote,
                "registry.itool.tech/luma-control:v1",
            )

        self.assertEqual(result, "Internal registry now bypasses external egress: registry.itool.tech")
        written = yaml.safe_load(remote.write_secret.call_args.args[0])
        self.assertEqual(
            written["rules"],
            ["DOMAIN,registry.itool.tech,DIRECT", "MATCH,EGRESS"],
        )
        remote.run.assert_called_once_with(
            "nomad job restart -yes -on-error=fail egress",
            timeout=180,
        )

    def test_control_image_pulls_published_image_during_bootstrap(self):
        remote = Mock()
        remote.sudo.return_value = ""

        result = _ensure_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(remote.upload.call_count, 0)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("docker pull ghcr.io/liutianjie/luma-control:latest" in cmd for cmd in docker_commands))
        self.assertFalse(any("docker build" in cmd for cmd in docker_commands))

    def test_control_image_pull_uses_ephemeral_registry_auth_and_deletes_it(self):
        remote = Mock()
        remote.sudo.return_value = ""

        result = _ensure_control_image(
            remote,
            "registry.itool.tech/luma-control:v1",
            registry_auth={
                "serverAddress": "registry.itool.tech",
                "username": "luma-pull",
                "password": "secret-value",
            },
        )

        self.assertEqual(result, "Control image pulled: registry.itool.tech/luma-control:v1")
        config = json.loads(remote.write_secret.call_args.args[0])
        encoded = config["auths"]["registry.itool.tech"]["auth"]
        self.assertEqual(base64.b64decode(encoded).decode(), "luma-pull:secret-value")
        self.assertNotIn("secret-value", " ".join(str(call.args) for call in remote.sudo.call_args_list))
        pull_command = remote.sudo.call_args_list[0].args[0]
        self.assertIn("DOCKER_CONFIG=/run/luma/control-image-auth-", pull_command)
        self.assertIn("docker pull registry.itool.tech/luma-control:v1", pull_command)
        self.assertIn("rm -rf /run/luma/control-image-auth-", remote.sudo.call_args_list[-1].args[0])
        self.assertFalse(remote.sudo.call_args_list[-1].kwargs["check"])

    def test_control_latest_image_resolves_to_pulled_repo_digest(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")

        def sudo(command):
            if "docker info --format" in command:
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            if "docker pull" in command:
                return ""
            if "docker image inspect --format" in command:
                return '["ghcr.io/liutianjie/luma-control@sha256:abc123","mirror.local/luma-control@sha256:def456"]\n'
            return ""

        remote.sudo.side_effect = sudo

        image, result = _resolve_control_image(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(image, "ghcr.io/liutianjie/luma-control@sha256:abc123")
        self.assertIn("Control image pulled: ghcr.io/liutianjie/luma-control:latest", result)
        self.assertIn("resolved digest: ghcr.io/liutianjie/luma-control@sha256:abc123", result)

    def test_control_pinned_image_does_not_require_repo_digest_lookup(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")

        def sudo(command):
            if "docker info --format" in command:
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            return ""

        remote.sudo.side_effect = sudo

        image, result = _resolve_control_image(remote, "ghcr.io/liutianjie/luma-control@sha256:abc123")

        self.assertEqual(image, "ghcr.io/liutianjie/luma-control@sha256:abc123")
        self.assertIn("Control image pull egress ready for ghcr.io", result)
        self.assertIn("Control image pulled: ghcr.io/liutianjie/luma-control@sha256:abc123", result)
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertFalse(any("docker image inspect --format" in cmd for cmd in docker_commands))

    def test_control_image_pull_configures_docker_daemon_egress_proxy(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")
        info_calls = 0

        def sudo(command):
            nonlocal info_calls
            if "docker info --format" in command:
                info_calls += 1
                if info_calls == 1:
                    return "HTTPProxy= HTTPSProxy=\n"
                return "HTTPProxy=http://127.0.0.1:7890 HTTPSProxy=http://127.0.0.1:7890\n"
            if "systemctl restart docker" in command:
                return ""
            return ""

        remote.sudo.side_effect = sudo

        result = _ensure_control_image_pull_egress(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertEqual(result, "Control image pull egress configured for ghcr.io: Docker daemon proxy http://127.0.0.1:7890")
        self.assertTrue(any("nomad job status -short egress" in call.args[0] for call in remote.run_result.call_args_list))
        docker_commands = [call.args[0] for call in remote.sudo.call_args_list]
        self.assertTrue(any("HTTP_PROXY=http://127.0.0.1:7890" in cmd for cmd in docker_commands))
        self.assertTrue(any("NO_PROXY=localhost,127.0.0.1" in cmd for cmd in docker_commands))

    def test_control_image_pull_requires_running_egress_gateway(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=1, output="")

        with self.assertRaisesRegex(LumaError, "control image pull egress requires a running Nomad egress job"):
            _ensure_control_image_pull_egress(remote, "ghcr.io/liutianjie/luma-control:latest")

        self.assertIn("nomad job status -short egress", remote.run_result.call_args.args[0])
        remote.sudo.assert_not_called()

    def test_control_stack_deploy_uses_resolved_digest_image(self):
        digest_image = "ghcr.io/liutianjie/luma-control@sha256:abc123"
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")
        remote.sudo.return_value = ""
        submitted = {}
        progress = []

        def capture_job(_remote, job_json, job_id):
            submitted["job"] = job_json
            submitted["jobId"] = job_id
            return f"Nomad job deployed: {job_id}"

        config = LumaConfig({}, None)

        with patch(
            "luma.bootstrap._ensure_control_image_pull_egress",
            return_value="Control image pull egress ready for ghcr.io: Docker daemon proxy http://127.0.0.1:7890",
        ), patch("luma.bootstrap._ensure_control_image", return_value="Control image pulled: ghcr.io/liutianjie/luma-control:latest"), patch(
            "luma.bootstrap._control_image_repo_digest", return_value=digest_image
        ), patch(
            "luma.bootstrap._nomad_tmpfs_compat_status", return_value="Nomad tmpfs compatibility ok"
        ), patch(
            "luma.bootstrap._deploy_nomad_job", side_effect=capture_job
        ), patch(
            "luma.bootstrap._wait_nomad_job", return_value="Nomad job running: luma-control"
        ):
            result = deploy_control_stack(remote, config, "luma.example.com", emit=progress.append)

        self.assertIn("Control image pull egress ready for ghcr.io", result[0])
        self.assertEqual(result[1], "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(result[2], f"Control image digest resolved: {digest_image}")
        self.assertEqual(result[3], "Nomad tmpfs compatibility ok")
        self.assertEqual(result[4], "Nomad job deployed: luma-control")
        self.assertEqual(result[5], "Nomad job running: luma-control")
        progress_text = "\n".join(progress)
        self.assertIn("[start] Ensure control image pull egress", progress_text)
        self.assertIn("[start] Pull Luma control image", progress_text)
        self.assertIn("[start] Resolve Luma control image digest", progress_text)
        self.assertIn("[start] Check Nomad tmpfs compatibility", progress_text)
        self.assertEqual(submitted["jobId"], "luma-control")
        self.assertIn(f'"image": "{digest_image}"', submitted["job"])

    def test_control_stack_deploy_can_skip_pull_egress_precheck(self):
        digest_image = "ghcr.io/liutianjie/luma-control@sha256:abc123"
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Status = running\n")
        remote.sudo.return_value = ""
        submitted = {}
        progress = []

        def capture_job(_remote, job_json, job_id):
            submitted["job"] = job_json
            return f"Nomad job deployed: {job_id}"

        config = LumaConfig({}, None)

        with patch("luma.bootstrap._ensure_control_image_pull_egress") as ensure_egress, patch(
            "luma.bootstrap._ensure_control_image",
            return_value="Control image pulled: ghcr.io/liutianjie/luma-control:latest",
        ), patch("luma.bootstrap._control_image_repo_digest", return_value=digest_image), patch(
            "luma.bootstrap._nomad_tmpfs_compat_status", return_value="Nomad tmpfs compatibility ok"
        ), patch(
            "luma.bootstrap._deploy_nomad_job", side_effect=capture_job
        ), patch(
            "luma.bootstrap._wait_nomad_job", return_value="Nomad job running: luma-control"
        ):
            result = deploy_control_stack(
                remote,
                config,
                "luma.example.com",
                emit=progress.append,
                require_pull_egress=False,
            )

        ensure_egress.assert_not_called()
        self.assertEqual(result[0], "Control image pulled: ghcr.io/liutianjie/luma-control:latest")
        self.assertEqual(result[2], "Nomad tmpfs compatibility ok")
        self.assertNotIn("[start] Ensure control image pull egress", "\n".join(progress))
        self.assertIn(f'"image": "{digest_image}"', submitted["job"])

    def test_control_stack_deploy_forwards_only_lae_control_allowlist(self):
        image = "ghcr.io/liutianjie/luma-control@sha256:" + "a" * 64
        config = LumaConfig(
            {
                "defaults": {
                    "engine": "nomad",
                    "images": {"lumaControl": image},
                }
            },
            None,
        )
        remote = Mock()
        submitted = {}
        canary = "inline-manager-secret-must-not-enter-nomad-job"

        def capture_job(_remote, job_json, job_id):
            submitted["job"] = json.loads(job_json)
            submitted["jobId"] = job_id
            return f"Nomad job deployed: {job_id}"

        environment = {
            "LUMA_LAE_SERVICE_PRINCIPALS_FILE": "/opt/luma/control/lae-builder-principals.json",
            "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE": "/opt/luma/control/lae-runtime-principals.json",
            "LUMA_CREDENTIAL_BROKER_URL": "https://broker.internal/v1/redeem",
            "LUMA_CREDENTIAL_BROKER_TOKEN_FILE": "/opt/luma/control/broker.token",
            "LUMA_OBJECT_SOURCE_BROKER_URL": "https://broker.internal/v1/objects",
            "LUMA_LAE_ADMIN_API_URL": "https://lae-api.internal",
            "LUMA_LAE_ADMIN_TOKEN_FILE": "/opt/luma/control/lae-admin.token",
            "LUMA_LAE_SERVICE_TOKEN": canary,
            "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_JSON": json.dumps(
                {"runtime": {"token": canary}}
            ),
            "LUMA_CREDENTIAL_BROKER_TOKEN": canary,
            "UNRELATED_MANAGER_SECRET": canary,
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "luma.bootstrap._ensure_control_image",
            return_value=f"Control image pulled: {image}",
        ), patch(
            "luma.bootstrap._nomad_tmpfs_compat_status",
            return_value="Nomad tmpfs compatibility ok",
        ), patch(
            "luma.bootstrap._deploy_nomad_job",
            side_effect=capture_job,
        ), patch(
            "luma.bootstrap._wait_nomad_job",
            return_value="Nomad job running: luma-control",
        ):
            deploy_control_stack(
                remote,
                config,
                "luma.example.com",
                require_pull_egress=False,
                node_name="manager-1",
            )

        self.assertEqual(submitted["jobId"], "luma-control")
        task_environment = submitted["job"]["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        self.assertEqual(
            task_environment["LUMA_LAE_SERVICE_PRINCIPALS_FILE"],
            "/opt/luma/control/lae-builder-principals.json",
        )
        self.assertEqual(
            task_environment["LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_FILE"],
            "/opt/luma/control/lae-runtime-principals.json",
        )
        self.assertEqual(
            task_environment["LUMA_CREDENTIAL_BROKER_TOKEN_FILE"],
            "/opt/luma/control/broker.token",
        )
        for forbidden in (
            "LUMA_LAE_SERVICE_TOKEN",
            "LUMA_LAE_RUNTIME_SERVICE_PRINCIPALS_JSON",
            "LUMA_CREDENTIAL_BROKER_TOKEN",
            "UNRELATED_MANAGER_SECRET",
        ):
            self.assertNotIn(forbidden, task_environment)
        self.assertNotIn(canary, json.dumps(submitted["job"], sort_keys=True))

    def test_manager_control_refresh_updates_ingress_without_recreating_other_core_stacks(self):
        config = LumaConfig(
            {
                "defaults": {"engine": "nomad"},
                "nodes": {
                    "manager": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "roles": ["nomad-manager", "edge"],
                    }
                }
            },
            None,
        )
        node = config.get_node("manager")
        state = {
            "clusterId": "luma-test",
            "deployToken": "deploy",
            "joinToken": "join",
            "deployments": {
                "services": {},
                "compose": {
                    "granary": {
                        "status": "active",
                        "tcpRelayPorts": [3306],
                    }
                },
            },
        }
        with patch("luma.bootstrap.local_host_name", return_value="manager-host"), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("manager-host", "nomad-node-id")
        ), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ) as install_config, patch("luma.bootstrap.install_control_state", return_value="state") as install_state, patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch(
            "luma.bootstrap._prefetch_control_image_for_manager_refresh", return_value="control image prefetched"
        ) as prefetch, patch(
            "luma.bootstrap.configure_firewall", return_value="firewall"
        ) as configure_fw, patch(
            "luma.bootstrap._deploy_nomad_job", return_value="traefik deployed"
        ) as deploy_nomad, patch(
            "luma.bootstrap._wait_nomad_job", return_value="traefik ready"
        ), patch(
            "luma.bootstrap.install_docker"
        ) as docker, patch("luma.bootstrap.setup_egress") as egress, patch(
            "luma.bootstrap._refresh_core_services"
        ) as refresh_core, patch(
            "luma.bootstrap.sync_nomad_tailscale_service_metadata", return_value="metadata"
        ) as sync_metadata, patch("luma.bootstrap.configure_tailscale_watchdog", return_value="watchdog") as watchdog:
            result = refresh_manager_control_local(config, node, "luma.example.com", state)

        self.assertIn("firewall", result)
        self.assertIn("traefik ready", result)
        self.assertIn("metadata", result)
        self.assertIn("watchdog", result)
        self.assertIn("control image prefetched", result)
        self.assertIn("config", result)
        self.assertIn("state", result)
        self.assertIn("control", result)
        self.assertEqual(state["domain"], "luma.example.com")
        self.assertEqual(state["nodes"]["manager"]["nomadNodeId"], "nomad-node-id")
        self.assertNotIn("managerAddr", state)
        install_config.assert_called_once()
        install_state.assert_called_once()
        deploy_control.assert_called_once()
        self.assertTrue(deploy_control.call_args.kwargs["control_image_prepared"])
        prefetch.assert_called_once()
        configure_fw.assert_called_once()
        self.assertEqual(configure_fw.call_args.kwargs["tcp_ports"], [3306])
        deploy_nomad.assert_called_once()
        self.assertIn("--entrypoints.tcp-3306.address=:3306", deploy_nomad.call_args.args[1])
        watchdog.assert_called_once()
        sync_metadata.assert_called_once()
        docker.assert_not_called()
        egress.assert_not_called()
        refresh_core.assert_not_called()

    def test_nomad_manager_control_refresh_uses_luma_node_name(self):
        config = LumaConfig(
            {
                "defaults": {"engine": "nomad"},
                "nodes": {
                    "aly": {
                        "host": "iZ0jl8auywzycory05d9cuZ",
                        "publicIp": "127.0.0.1",
                        "region": "cn",
                        "roles": ["nomad-manager", "edge"],
                    }
                },
            },
            None,
        )
        node = config.get_node("aly")
        state = {
            "clusterId": "luma-test",
            "deployToken": "deploy",
            "joinToken": "join",
            "nodes": {
                "iZ0jl8auywzycory05d9cuZ": {
                    "region": "cn",
                    "aliases": ["old-manager"],
                }
            },
        }
        with patch("luma.bootstrap.local_host_name", return_value="iZ0jl8auywzycory05d9cuZ"), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("iZ0jl8auywzycory05d9cuZ", "node-id")
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.113.204.125"), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ), patch("luma.bootstrap.install_control_state", return_value="state"), patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch(
            "luma.bootstrap._prefetch_control_image_for_manager_refresh", return_value="control image prefetched"
        ), patch("luma.bootstrap.configure_firewall", return_value="firewall"), patch(
            "luma.bootstrap._deploy_nomad_job", return_value="traefik deployed"
        ), patch("luma.bootstrap._wait_nomad_job", return_value="traefik ready"), patch(
            "luma.bootstrap.sync_nomad_tailscale_service_metadata", return_value="metadata"
        ), patch(
            "luma.bootstrap.configure_tailscale_watchdog", return_value="watchdog"
        ):
            refresh_manager_control_local(config, node, "luma.example.com", state)

        deploy_control.assert_called_once()
        self.assertEqual(deploy_control.call_args.kwargs["node_name"], "aly")
        self.assertTrue(deploy_control.call_args.kwargs["control_image_prepared"])
        self.assertIn("aly", state["nodes"])
        self.assertNotIn("iZ0jl8auywzycory05d9cuZ", state["nodes"])
        self.assertEqual(state["nodes"]["aly"]["displayName"], "aly")
        self.assertIn("iZ0jl8auywzycory05d9cuZ", state["nodes"]["aly"]["aliases"])
        self.assertIn("old-manager", state["nodes"]["aly"]["aliases"])
        self.assertNotIn("managerAddr", state)

    def test_nomad_manager_control_refresh_prefers_existing_nomad_meta_over_hostname_config(self):
        config = LumaConfig(
            {
                "defaults": {"engine": "nomad"},
                "nodes": {
                    "iZ0jl8auywzycory05d9cuZ": {
                        "host": "localhost",
                        "publicIp": "127.0.0.1",
                        "region": "cn",
                        "roles": ["nomad-manager", "edge"],
                    }
                },
            },
            None,
        )
        node = config.get_node("iZ0jl8auywzycory05d9cuZ")
        state = {
            "clusterId": "luma-test",
            "deployToken": "deploy",
            "joinToken": "join",
            "nodes": {
                "aly": {
                    "region": "cn",
                    "nodeId": "node-id",
                    "labels": {"luma.node.name": "aly", "luma.node.id": "node-id"},
                }
            },
        }
        with patch("luma.bootstrap.local_host_name", return_value="iZ0jl8auywzycory05d9cuZ"), patch(
            "luma.bootstrap.local_nomad_node_info", return_value=("aly", "node-id")
        ), patch("luma.bootstrap._tailscale_ip", return_value="100.113.204.125"), patch(
            "luma.bootstrap.install_control_config", return_value="config"
        ), patch("luma.bootstrap.install_control_state", return_value="state"), patch(
            "luma.bootstrap.deploy_control_stack", return_value=["control"]
        ) as deploy_control, patch(
            "luma.bootstrap._prefetch_control_image_for_manager_refresh", return_value="control image prefetched"
        ), patch("luma.bootstrap.configure_firewall", return_value="firewall"), patch(
            "luma.bootstrap._deploy_nomad_job", return_value="traefik deployed"
        ), patch("luma.bootstrap._wait_nomad_job", return_value="traefik ready"), patch(
            "luma.bootstrap.sync_nomad_tailscale_service_metadata", return_value="metadata"
        ), patch(
            "luma.bootstrap.configure_tailscale_watchdog", return_value="watchdog"
        ):
            refresh_manager_control_local(config, node, "luma.example.com", state)

        deploy_control.assert_called_once()
        self.assertEqual(deploy_control.call_args.kwargs["node_name"], "aly")
        self.assertTrue(deploy_control.call_args.kwargs["control_image_prepared"])
        self.assertIn("aly", state["nodes"])
        self.assertNotIn("iZ0jl8auywzycory05d9cuZ", state["nodes"])
        self.assertEqual(state["nodes"]["aly"]["displayName"], "aly")
        self.assertIn("iZ0jl8auywzycory05d9cuZ", state["nodes"]["aly"]["aliases"])
        self.assertNotIn("managerAddr", state)

    def test_manager_syncs_ready_nomad_nodes_with_tailscale_service_metadata(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(
                code=0,
                output=json.dumps(
                    [
                        {
                            "ID": "node-manager",
                            "Status": "ready",
                            "Address": "100.106.154.3",
                        },
                        {
                            "ID": "node-tecent",
                            "Status": "ready",
                            "Address": "100.64.29.91",
                        },
                        {
                            "ID": "node-down",
                            "Status": "down",
                            "Address": "100.64.0.9",
                        },
                    ]
                ),
            ),
            Mock(code=0, output="Metadata updated"),
            Mock(code=0, output="Metadata updated"),
        ]

        result = sync_nomad_tailscale_service_metadata(remote)

        self.assertEqual(result, "Nomad Tailscale service metadata applied to 2 ready node(s)")
        commands = [call.args[0] for call in remote.run_result.call_args_list[1:]]
        self.assertIn(
            "nomad node meta apply -node-id node-manager luma_tailscale_ip=100.106.154.3",
            commands,
        )
        self.assertIn(
            "nomad node meta apply -node-id node-tecent luma_tailscale_ip=100.64.29.91",
            commands,
        )

    def test_manager_metadata_sync_defers_unreachable_nodes_during_update(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(
                code=0,
                output=json.dumps(
                    [
                        {
                            "ID": "node-lab",
                            "Status": "ready",
                            "Address": "100.69.154.50",
                        }
                    ]
                ),
            ),
            Mock(code=1, output="Unexpected response code: 404 (No path to node)"),
        ]

        result = sync_nomad_tailscale_service_metadata(remote, strict=False)

        self.assertIn("1 unreachable node(s) deferred", result)

    def test_verify_nomad_node_accepts_tailscale_http_address(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="ready\n")

        result = verify_local_nomad_node(remote, http_addrs=["100.69.154.50"])

        self.assertEqual(result, "Nomad agent ready")
        command = remote.run_result.call_args.args[0]
        self.assertIn("http://127.0.0.1:4646/v1/agent/self", command)
        self.assertIn("http://100.69.154.50:4646/v1/agent/self", command)

    def test_install_docker_repairs_known_bad_apt_mirror(self):
        remote = Mock()
        remote.run_result.return_value = Mock(code=0, output="Linux\n")
        remote.sudo_result.return_value = Mock(code=1, output="")
        remote.sudo.return_value = ""

        result = install_docker(remote)

        self.assertEqual(result, "Docker installed")
        command = remote.sudo.call_args.args[0]
        self.assertIn("command -v apt-get", command)
        self.assertIn("mirrors.ivolces.com/ubuntu", command)
        self.assertIn("mirrors.aliyun.com/ubuntu", command)
        self.assertIn("apt-get install -y docker.io docker-compose-v2", command)
        self.assertNotIn("apt-get install -y docker.io docker-compose-v2 curl ca-certificates ufw python3-yaml || true", command)

    def test_install_docker_on_macos_requires_docker_cli(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=1, output=""),
        ]

        with self.assertRaisesRegex(LumaError, "Install Docker Desktop"):
            install_docker(remote)

    def test_install_docker_on_macos_requires_running_daemon(self):
        remote = Mock()
        remote.run_result.side_effect = [
            Mock(code=0, output="Darwin\n"),
            Mock(code=0, output=""),
            Mock(code=1, output=""),
        ]

        with self.assertRaisesRegex(LumaError, "Start Docker Desktop"):
            install_docker(remote)

class ControlApiTests(unittest.TestCase):
    def test_agent_manager_update_runs_in_independent_systemd_unit_and_reports_status(self):
        from luma.agent import manager_control_update_status, start_manager_control_update

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "manager-updates"
            executable = Path(tmp) / "home" / "tao" / ".local" / "bin" / "luma"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            old_root = _set_env("LUMA_MANAGER_UPDATE_ROOT", str(root))
            completed = Mock(returncode=0, stdout="")
            try:
                with patch("luma.agent.node_agent_os", return_value="linux"), patch(
                    "luma.agent.shutil.which", return_value="/usr/bin/systemd-run"
                ), patch("luma.agent._installed_luma_executable", return_value=str(executable)), patch(
                    "luma.agent._current_install_layout",
                    return_value=(Path(tmp) / "home" / "tao", Path(tmp) / "home" / "tao" / ".local" / "share" / "luma", executable.parent),
                ), patch("luma.agent.subprocess.run", return_value=completed) as run:
                    result = start_manager_control_update(
                        install_ref="v0.1.173",
                        control_image="ghcr.io/liutianjie/luma-control:v0.1.173",
                        domain="luma.example.com",
                        watchdog_peers=["100.69.154.50"],
                    )
                invocation = run.call_args.args[0]
                self.assertEqual(invocation[0], "systemd-run")
                self.assertIn("--property=Type=exec", invocation)
                self.assertIn(str(executable), invocation)
                self.assertIn("--setenv=LUMA_USER_HOME=" + str(Path(tmp) / "home" / "tao"), invocation)
                self.assertIn(
                    "--setenv=LUMA_TAILSCALE_WATCHDOG_PEERS=100.69.154.50",
                    invocation,
                )
                self.assertNotIn("management-token", " ".join(invocation))

                update_id = str(result["updateId"])
                (root / f"{update_id}.status").write_text("0\n", encoding="utf-8")
                (root / f"{update_id}.log").write_text("control healthy\n", encoding="utf-8")
                status = manager_control_update_status(update_id=update_id)
                self.assertEqual(status["status"], "succeeded")
                self.assertEqual(status["log"], ["control healthy"])
            finally:
                _restore_env("LUMA_MANAGER_UPDATE_ROOT", old_root)

    def test_manager_update_handler_uses_manager_agent_transient_update_capability(self):
        from luma.control.server import handle_manager_update_start

        analyzer = "100.66.177.70:5000/lae/agent-runner@sha256:" + "a" * 64

        state = {
            "clusterId": "luma-test",
            "domain": "luma.example.com",
            "deployToken": "management-token",
            "nodes": {
                "manager": {
                    "labels": {"role.nomad-manager": "true"},
                    "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": ["manager-update-v1"]},
                },
                "lab": {
                    "tailscaleIP": "100.69.154.50",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": [],
                    },
                },
            },
        }
        result = {
            "taskId": "task-1",
            "updateId": "manager-1783835000000-aabbccdd",
            "status": "running",
            "installRef": "v0.1.173",
            "controlImage": "ghcr.io/liutianjie/luma-control:v0.1.173",
        }
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server._run_node_agent_task", return_value=result
        ) as run:
            response = handle_manager_update_start(
                "management-token",
                {
                    "installRef": "v0.1.173",
                    "controlEnvironment": {
                        "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": analyzer
                    },
                },
            )

        self.assertEqual(response["updateId"], "manager-1783835000000-aabbccdd")
        self.assertEqual(response["managerNode"], "manager")
        self.assertEqual(run.call_args.args[2], "start-manager-update")
        self.assertEqual(run.call_args.kwargs["required_capability"], "manager-update-v1")
        self.assertEqual(run.call_args.args[3]["domain"], "luma.example.com")
        self.assertEqual(
            run.call_args.args[3]["controlEnvironment"],
            {"LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": analyzer},
        )
        self.assertEqual(
            run.call_args.args[3]["tailscaleWatchdogPeers"],
            ["100.69.154.50"],
        )

    def test_manager_control_environment_is_validated_merged_and_persisted(self):
        from luma.bootstrap import install_control_environment

        first_digest = "100.66.177.70:5000/lae/agent-runner@sha256:" + "a" * 64
        second_digest = "100.66.177.70:5000/lae/agent-runner@sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control" / "control.env"
            installed = install_control_environment(
                {
                    "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": first_digest,
                    "LUMA_LAE_RUNTIME_STORAGE_CLASS": "runtime-nfs",
                },
                path=path,
            )
            self.assertEqual(
                installed["LUMA_BUILDER_ANALYZE_IMAGE_DIGEST"], first_digest
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            merged = install_control_environment(
                {"LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": second_digest}, path=path
            )
            self.assertEqual(
                merged,
                {
                    "LUMA_BUILDER_ANALYZE_IMAGE_DIGEST": second_digest,
                    "LUMA_LAE_RUNTIME_STORAGE_CLASS": "runtime-nfs",
                },
            )
            self.assertNotIn("export ", path.read_text(encoding="utf-8"))

    def test_agent_mirrors_control_image_with_proxy_and_verifies_digest(self):
        from luma.agent import mirror_control_image
        from luma.local import LocalResult

        digest = "sha256:" + "a" * 64
        events = []
        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming",
            side_effect=[LocalResult(0, "copy complete"), LocalResult(0, digest + "\n")],
        ) as run:
            result = mirror_control_image(
                source_image="ghcr.io/liutianjie/luma-control:v0.1.175",
                push_image="localhost:5000/luma-control:v0.1.175",
                destination_image="100.66.177.70:5000/luma-control:v0.1.175",
                proxy="http://100.106.154.3:7890",
                insecure=True,
                progress=events.append,
            )

        copy_command = run.call_args_list[0].args[0]
        self.assertEqual(copy_command[:2], ["/usr/local/bin/crane", "copy"])
        self.assertIn("--insecure", copy_command)
        self.assertEqual(run.call_args_list[0].kwargs["env"]["HTTPS_PROXY"], "http://100.106.154.3:7890")
        self.assertIn("localhost:5000", run.call_args_list[0].kwargs["env"]["NO_PROXY"])
        self.assertEqual(result["destinationImage"], "100.66.177.70:5000/luma-control:v0.1.175")
        self.assertEqual(result["digest"], digest)
        self.assertTrue(any("verified" in str(event.get("line") or "").lower() for event in events))

    def test_agent_mirror_uses_ephemeral_registry_auth_config(self):
        from luma.agent import mirror_control_image
        from luma.local import LocalResult

        digest = "sha256:" + "c" * 64
        observed: list[dict[str, Any]] = []

        def run(_command, **kwargs):
            docker_config = Path(kwargs["env"]["DOCKER_CONFIG"])
            observed.append(json.loads((docker_config / "config.json").read_text(encoding="utf-8")))
            return LocalResult(0, "copy complete" if len(observed) == 1 else digest + "\n")

        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming", side_effect=run
        ) as process:
            mirror_control_image(
                source_image="ghcr.io/liutianjie/luma-control:v0.1.212",
                push_image="registry.example.com/luma-control:v0.1.212",
                destination_image="registry.example.com/luma-control:v0.1.212",
                registry_auth={"username": "lae", "password": "registry-secret", "serveraddress": "registry.example.com"},
            )

        self.assertEqual(len(observed), 2)
        self.assertIn("registry.example.com", observed[0]["auths"])
        docker_config_path = Path(process.call_args_list[0].kwargs["env"]["DOCKER_CONFIG"])
        self.assertFalse(docker_config_path.exists())
        self.assertNotIn("registry-secret", repr(process.call_args_list))

    def test_agent_mirrors_system_image_for_one_runtime_platform(self):
        from luma.agent import mirror_system_image
        from luma.local import LocalResult

        digest = "sha256:" + "b" * 64
        with patch("luma.agent._crane_binary", return_value="/usr/local/bin/crane"), patch(
            "luma.agent._run_process_streaming",
            side_effect=[LocalResult(0, "copy complete"), LocalResult(0, digest + "\n")],
        ) as run:
            result = mirror_system_image(
                source_image="registry:2",
                push_image="localhost:5000/luma-system/registry-runtime:test",
                destination_image="100.66.177.70:5000/luma-system/registry-runtime:test",
                platform="linux/amd64",
                insecure=True,
            )

        copy_command = run.call_args_list[0].args[0]
        self.assertIn("--platform", copy_command)
        self.assertEqual(copy_command[copy_command.index("--platform") + 1], "linux/amd64")
        self.assertEqual(result["digest"], digest)
        self.assertIn("System image", result["message"])

    def test_async_control_image_preparation_persists_progress_and_internal_ref(self):
        from luma.control.server import handle_control_image_prepare_get, handle_control_image_prepare_start
        from luma.control.state import save_state

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_insecure = _set_env("LUMA_LAE_BUILDER_REGISTRY_INSECURE", "1")
            try:
                save_state(
                    {
                        "clusterId": "luma-test",
                        "deployToken": "management-token",
                        "build": {
                            "defaultNode": "builder",
                            "nodes": ["builder"],
                            "registryHost": "100.66.177.70:5000",
                            "pushHost": "localhost:5000",
                        },
                        "nodes": {
                            "manager": {
                                "labels": {"role.nomad-manager": "true"},
                                "agent": {
                                    "status": "online",
                                    "lastSeen": int(time.time()),
                                    "os": "linux",
                                    "arch": "amd64",
                                    "capabilities": ["manager-update-v1"],
                                },
                            },
                            "builder": {
                                "roles": ["builder"],
                                "agent": {
                                    "status": "online",
                                    "lastSeen": int(time.time()),
                                    "capabilities": ["docker-build", "control-image-mirror-v1"],
                                },
                            }
                        },
                    }
                )

                def mirror(_state, node, action, payload, **kwargs):
                    self.assertEqual(node, "builder")
                    self.assertEqual(action, "mirror-control-image")
                    self.assertEqual(payload["pushImage"], "localhost:5000/luma-control:v0.1.175")
                    self.assertEqual(payload["platform"], "linux/amd64")
                    self.assertEqual(kwargs["required_capability"], "control-image-mirror-v1")
                    kwargs["progress"]({"line": "copying layers"})
                    return {
                        "taskId": "task-1",
                        "destinationImage": payload["destinationImage"],
                        "digest": "sha256:" + "b" * 64,
                        "message": "cached",
                    }

                with patch("luma.control.server.load_config", return_value=Mock()), patch(
                    "luma.control.server._egress_proxy_for_node", return_value="http://proxy:7890"
                ), patch("luma.control.server._run_node_agent_task", side_effect=mirror):
                    started = handle_control_image_prepare_start(
                        "management-token",
                        {
                            "installRef": "v0.1.175",
                            "controlImage": "ghcr.io/liutianjie/luma-control:v0.1.175",
                        },
                    )
                    deadline = time.time() + 3
                    current = started
                    while current.get("status") in {"queued", "running"} and time.time() < deadline:
                        time.sleep(0.02)
                        current = handle_control_image_prepare_get("management-token", str(started["id"]))

                self.assertEqual(current["status"], "succeeded")
                self.assertEqual(current["result"]["destinationImage"], "100.66.177.70:5000/luma-control:v0.1.175")
                self.assertIn("copying layers", current["log"])
                self.assertNotIn("proxy", current["plan"])
            finally:
                _restore_env("LUMA_LAE_BUILDER_REGISTRY_INSECURE", old_insecure)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_async_fleet_update_persists_node_progress_and_recovers_by_id(self):
        from luma.control.server import handle_fleet_update_operation_get, handle_fleet_update_operation_start
        from luma.control.state import save_state

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                save_state({"clusterId": "luma-test", "deployToken": "management-token"})

                def run_fleet(_token, request, *, progress=None):
                    self.assertEqual(request["nodeNames"], ["lab"])
                    if progress:
                        progress({"nodeName": "lab", "status": "pending", "agentVersionBefore": "0.1.172"})
                        progress({"nodeName": "lab", "status": "succeeded", "message": "updated"})
                    return {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0, "results": []}

                with patch("luma.control.server.handle_fleet_update", side_effect=run_fleet):
                    started = handle_fleet_update_operation_start(
                        "management-token",
                        {"installRef": "v0.1.173", "nodeNames": ["lab"]},
                    )
                    deadline = time.time() + 3
                    current = started
                    while current.get("status") in {"queued", "running"} and time.time() < deadline:
                        time.sleep(0.02)
                        current = handle_fleet_update_operation_get("management-token", str(started["id"]))

                self.assertEqual(current["status"], "succeeded")
                self.assertEqual(current["nodes"][0]["nodeName"], "lab")
                self.assertEqual(current["nodes"][0]["status"], "succeeded")
                self.assertNotIn("output", current["nodes"][0])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_dashboard_route_sentinel_reports_structured_route_health(self):
        from luma.control.server import handle_route_sentinel

        state = {"clusterId": "luma-test", "deployToken": "management-token"}
        routes = {
            "app": {"kind": "http", "domain": "app.example.com"},
            "tcp": {"kind": "tcp", "domain": "tcp.example.com"},
        }
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server.load_config", return_value=Mock()
        ), patch("luma.control.server._dashboard_route_files", return_value=routes), patch(
            "luma.control.server._sentinel_active_http_domains",
            return_value={"app.example.com"},
        ), patch(
            "luma.control.server._sentinel_probe_public_route",
            return_value={"domain": "app.example.com", "status": 200, "ok": True, "latencyMs": 12, "error": ""},
        ):
            result = handle_route_sentinel("management-token", {})

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)

    def test_route_sentinel_domain_probe_is_independent_from_service_probe(self):
        from luma.control.server import _sentinel_probe_public_route

        unauthorized = urllib.error.HTTPError(
            "https://app.example.com/", 401, "unauthorized", {}, None
        )
        missing = urllib.error.HTTPError(
            "https://missing.example.com/", 404, "missing", {}, None
        )
        with patch(
            "luma.control.server.urllib.request.urlopen",
            side_effect=[unauthorized, missing],
        ):
            published = _sentinel_probe_public_route("app.example.com")
            unpublished = _sentinel_probe_public_route("missing.example.com")

        self.assertTrue(published["ok"])
        self.assertEqual(published["status"], 401)
        self.assertFalse(unpublished["ok"])
        self.assertEqual(unpublished["status"], 404)

    def test_route_sentinel_accepts_application_owned_json_404(self):
        import io
        from luma.control.server import _sentinel_probe_public_route

        application_404 = urllib.error.HTTPError(
            "https://api.example.com/",
            404,
            "missing",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"detail":"Not Found"}'),
        )
        with patch(
            "luma.control.server.urllib.request.urlopen",
            side_effect=application_404,
        ):
            result = _sentinel_probe_public_route("api.example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 404)

    def test_route_sentinel_excludes_stale_route_files_from_default_inventory(self):
        from luma.control.server import handle_route_sentinel

        state = {"clusterId": "luma-test", "deployToken": "management-token"}
        routes = {
            "active": {"kind": "http", "domain": "active.example.com"},
            "stale": {"kind": "http", "domain": "stale.example.com"},
        }
        with patch("luma.control.server.load_state", return_value=state), patch(
            "luma.control.server.load_config", return_value=Mock()
        ), patch("luma.control.server._dashboard_route_files", return_value=routes), patch(
            "luma.control.server._sentinel_active_http_domains",
            return_value={"active.example.com"},
        ), patch(
            "luma.control.server._sentinel_probe_public_route",
            return_value={"domain": "active.example.com", "status": 200, "ok": True, "latencyMs": 4, "error": ""},
        ) as probe:
            result = handle_route_sentinel("management-token", {})

        probe.assert_called_once_with("active.example.com")
        self.assertEqual(result["total"], 1)

    def test_route_sentinel_inventory_joins_active_deployments_not_route_files(self):
        from luma.control.server import _sentinel_active_http_domains

        state = {
            "deployments": {
                "services": {
                    "app": {
                        "status": "active",
                        "manifest": "name: app\nexposure: tailscale-relay\ndomain: active.example.com\n",
                    }
                }
            }
        }
        routes = {
            "app": {"kind": "http", "domain": "active.example.com"},
            "old-app": {"kind": "http", "domain": "stale.example.com"},
        }
        errors = []
        with patch("luma.control.server.nomad_services_summary", return_value=[]):
            domains = _sentinel_active_http_domains(Mock(), state, routes, errors)

        self.assertEqual(domains, {"active.example.com"})
        self.assertEqual(errors, [])

    def test_prune_agent_tasks_drops_old_terminal_keeps_active_and_recent(self):
        from luma.control.server import (
            AGENT_TASK_PROGRESS_LIMIT,
            AGENT_TASK_RETENTION_SECONDS,
            _prune_agent_tasks,
        )

        now = 1_000_000
        old = now - AGENT_TASK_RETENTION_SECONDS - 10
        recent = now - 5
        state = {
            "agentTasks": {
                "old-done": {"status": "succeeded", "completedAt": old},
                "old-failed": {"status": "failed", "completedAt": old},
                "old-timeout": {"status": "timeout", "updatedAt": old},
                "recent-done": {
                    "status": "succeeded",
                    "completedAt": recent,
                    "message": "x" * 10_000,
                    "progress": [{"line": str(index)} for index in range(500)],
                },
                "old-queued": {"status": "queued", "createdAt": old},      # active: keep
                "old-running": {"status": "running", "updatedAt": old},    # active: keep
            }
        }
        _prune_agent_tasks(state, now=now)
        survivors = set(state["agentTasks"])
        # old terminal tasks gone; active tasks and recent terminal survive
        self.assertEqual(survivors, {"recent-done", "old-queued", "old-running"})
        recent_task = state["agentTasks"]["recent-done"]
        self.assertEqual(len(recent_task["progress"]), AGENT_TASK_PROGRESS_LIMIT)
        self.assertEqual(recent_task["progress"][0]["line"], "200")
        self.assertEqual(len(recent_task["message"]), 4000)

    def test_agent_idle_long_poll_persists_only_initial_heartbeat(self):
        from luma.control import state as control_state

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker": {
                        "nodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker", "luma.node.id": "worker-node-id"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(
                    state["deployToken"],
                    {"nodeName": "worker", "nodeId": "worker-node-id"},
                )
                original_save = control_state.save_state
                with patch("luma.control.state.save_state", wraps=original_save) as save:
                    lease = handle_node_agent_lease(
                        issued["agentToken"],
                        {
                            "nodeName": "worker",
                            "nodeId": "worker-node-id",
                            "os": "linux",
                            "capabilities": [],
                            "waitSeconds": 2,
                        },
                    )
                self.assertIsNone(lease["task"])
                self.assertEqual(save.call_count, 1)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)


        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                management_result = handle_node_register(state["deployToken"], {"nodeName": "b", "region": "global"})
                join_result = handle_node_register(state["joinToken"], {"nodeName": "c", "region": "home"})
                self.assertEqual(management_result["nodeName"], "b")
                self.assertEqual(management_result["region"], "global")
                self.assertEqual(join_result["nodeName"], "c")
                self.assertEqual(join_result["region"], "home")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_nomad_node_register_returns_rpc_addr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "aly": {
                        "status": "manager",
                        "tailscaleIP": "100.113.204.125",
                        "labels": {"role.nomad-manager": "true", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")

                result = handle_node_register(state["joinToken"], {"nodeName": "bot", "region": "global"})

                self.assertEqual(result["nomadRpcAddr"], "100.113.204.125:4647")
                self.assertEqual(result["nomadServerAddr"], "100.113.204.125:4647")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_management_and_join_tokens_can_label_node_after_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                result = handle_node_label(state["joinToken"], {"nodeName": "b", "nodeId": "node-id-b", "region": "global"})
                labels = result["labels"]
                self.assertEqual(labels["region"], "global")
                self.assertEqual(labels["luma.node.name"], "b")
                self.assertNotIn("egress", labels)
                self.assertNotIn("role.global-worker", labels)
                self.assertEqual(result["nodeName"], "b")
                self.assertEqual(result["nomadNodeId"], "node-id-b")
                management_result = handle_node_label(state["deployToken"], {"nodeName": "b", "nodeId": "node-id-b", "region": "global"})
                self.assertEqual(management_result["nodeName"], "b")
                self.assertEqual(management_result["nomadNodeId"], "node-id-b")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_nomad_join_token_labels_node_without_swarm_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"m4": {"region": "home", "status": "registered"}}
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")

                result = handle_node_label(
                    state["joinToken"],
                    {
                        "nodeName": "m4-host",
                        "registeredName": "m4",
                        "nodeId": "nomad-node-id",
                        "region": "home",
                        "tailscaleIP": "100.121.94.123",
                    },
                )

                self.assertEqual(result["nodeName"], "m4")
                self.assertEqual(result["nomadNodeId"], "nomad-node-id")
                saved = load_state()
                self.assertEqual(saved["nodes"]["m4"]["nodeId"], "nomad-node-id")
                self.assertEqual(saved["nodes"]["m4"]["nomadNodeId"], "nomad-node-id")
                self.assertEqual(saved["nodes"]["m4"]["tailscaleIP"], "100.121.94.123")
                self.assertTrue(saved["nodes"]["m4"]["agent"]["tokenHash"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_label_node_keeps_requested_name_as_luma_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                handle_node_register(state["joinToken"], {"nodeName": "global-sg-1", "region": "global"})
                result = handle_node_label(
                    state["joinToken"],
                    {
                        "nodeName": "docker-hostname",
                        "nodeId": "node-id-1",
                        "registeredName": "global-sg-1",
                        "region": "global",
                        "tailscaleIP": "100.64.0.30",
                        "tailscaleName": "global-sg-1.ts.net",
                    },
                )
                saved = load_state()
                self.assertEqual(result["nodeName"], "global-sg-1")
                self.assertEqual(result["displayName"], "global-sg-1")
                self.assertEqual(result["tailscaleIP"], "100.64.0.30")
                self.assertEqual(result["tailscaleName"], "global-sg-1.ts.net")
                self.assertIn("global-sg-1", saved["nodes"])
                self.assertEqual(saved["nodes"]["global-sg-1"]["nomadHostname"], "docker-hostname")
                self.assertEqual(saved["nodes"]["global-sg-1"]["nomadNodeId"], "node-id-1")
                self.assertEqual(saved["nodes"]["global-sg-1"]["tailscaleIP"], "100.64.0.30")
                self.assertEqual(saved["nodes"]["global-sg-1"]["tailscaleName"], "global-sg-1.ts.net")
                self.assertEqual(saved["nodes"]["global-sg-1"]["labels"]["luma.node.id"], "node-id-1")
                self.assertEqual(saved["nodes"]["global-sg-1"]["status"], "labeled")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_rejoin_updates_nomad_identity_without_swarm_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_node_label(
                    state["joinToken"],
                    {
                        "nodeName": "m4-host",
                        "nodeId": "old-node-id",
                        "registeredName": "m4",
                        "region": "home",
                    },
                )
                with patch("luma.control.server.docker_request") as docker:
                    result = handle_node_label(
                        state["joinToken"],
                        {
                            "nodeName": "m4-host",
                            "nodeId": "new-node-id",
                            "registeredName": "m4",
                            "region": "home",
                        },
                    )
                saved = load_state()
                docker.assert_not_called()
                self.assertEqual(saved["nodes"]["m4"]["nomadNodeId"], "new-node-id")
                self.assertEqual(saved["nodes"]["m4"]["nodeId"], "new-node-id")
                self.assertEqual(result["previousNodeId"], "old-node-id")
                self.assertNotIn("pinnedServicesUpdated", result)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_nomad_agent_join_updates_node_identity_and_keeps_agent_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.113.204.125:4647"
                state["nodes"] = {
                    "bot": {
                        "region": "global",
                        "status": "labeled",
                        "nodeId": "agent-node-id",
                        "labels": {"region": "global", "luma.node.name": "bot", "luma.node.id": "agent-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "bot", "nodeId": "agent-node-id"})
                agent_token = issued["agentToken"]
                handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "bot",
                        "nodeId": "agent-node-id",
                        "os": "linux",
                        "capabilities": ["nomad-join"],
                        "waitSeconds": 0,
                    },
                )

                def run_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(node_name, "bot")
                    self.assertEqual(action, "join-nomad")
                    self.assertEqual(payload["serverAddr"], "100.113.204.125:4647")
                    self.assertNotIn("egressProxy", payload)
                    self.assertEqual(kwargs["required_capability"], "nomad-join")
                    return {
                        "taskId": "task-join",
                        "nodeName": "bot-host",
                        "nodeId": "nomad-node-id",
                        "tailscaleIP": "100.80.0.20",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task):
                    result = handle_node_nomad_join(state["deployToken"], {"nodeName": "bot"})

                saved = load_state()["nodes"]["bot"]
                self.assertEqual(result["nomadNodeId"], "nomad-node-id")
                self.assertEqual(saved["nodeId"], "nomad-node-id")
                self.assertEqual(saved["nomadNodeId"], "nomad-node-id")
                self.assertEqual(saved["nomadHostname"], "bot-host")
                self.assertEqual(saved["tailscaleIP"], "100.80.0.20")
                self.assertIn("agent-node-id", saved["agent"]["knownNodeIds"])
                self.assertEqual(saved["agent"]["nodeId"], "agent-node-id")
                lease = handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "bot",
                        "nodeId": "agent-node-id",
                        "os": "linux",
                        "capabilities": ["nomad-join"],
                        "waitSeconds": 0,
                    },
                )
                self.assertIsNone(lease["task"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_nomad_join_task_lease_injects_tailscale_key_without_persisting_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["secrets"] = {"TAILSCALE_AUTHKEY": "ts-secret"}
                state["nodes"] = {
                    "bot": {
                        "region": "global",
                        "nodeId": "agent-node-id",
                        "labels": {"region": "global", "luma.node.name": "bot", "luma.node.id": "agent-node-id"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "bot", "nodeId": "agent-node-id"})
                current = load_state()
                current.setdefault("agentTasks", {})["task-join"] = {
                    "id": "task-join",
                    "nodeName": "bot",
                    "action": "join-nomad",
                    "payload": {"nodeName": "bot", "region": "global", "serverAddr": "100.113.204.125:4647"},
                    "status": "queued",
                }
                save_state(current)

                leased = handle_node_agent_lease(
                    issued["agentToken"],
                    {
                        "nodeName": "bot",
                        "nodeId": "agent-node-id",
                        "os": "linux",
                        "capabilities": ["nomad-join"],
                        "waitSeconds": 0,
                    },
                )["task"]

                self.assertEqual(leased["payload"]["tailscaleAuthKey"], "ts-secret")
                saved_payload = load_state()["agentTasks"]["task-join"]["payload"]
                self.assertNotIn("tailscaleAuthKey", saved_payload)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_runs_ready_node_agents_and_reports_skipped_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": now, "os": "darwin", "capabilities": ["docker-volume", "luma-update"]},
                    },
                    "lab": {
                        "region": "home",
                        "agent": {"status": "offline", "lastSeen": now - 1000, "os": "linux", "capabilities": ["docker-volume"]},
                    },
                }
                save_state(state)

                def run_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(action, "update-luma")
                    self.assertEqual(payload["installRef"], "main")
                    self.assertEqual(kwargs["required_capability"], "luma-update")
                    return {"taskId": "task-1", "message": "Luma installer finished", "installRef": payload["installRef"]}

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task) as run:
                    result = handle_fleet_update(state["deployToken"], {"installRef": "main", "includeAll": True, "timeout": 120})

                run.assert_called_once()
                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["skipped"], 1)
                by_name = {item["nodeName"]: item for item in result["results"]}
                self.assertEqual(by_name["home-mac-mini"]["status"], "succeeded")
                self.assertEqual(by_name["lab"]["status"], "skipped")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_injects_node_scoped_egress_proxy_for_cn_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.106.154.3:4647"
                state["nodes"] = {
                    "tecent": {
                        "region": "cn",
                        "labels": {"region": "cn", "luma.node.name": "tecent"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "os": "linux",
                            "capabilities": ["luma-update", "luma-update-proxy-v1"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )

                def run_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(node_name, "tecent")
                    self.assertEqual(action, "update-luma")
                    self.assertEqual(payload["installRef"], "v0.1.235")
                    self.assertEqual(payload["proxy"], "http://100.106.154.3:7890")
                    self.assertEqual(kwargs["required_capability"], "luma-update")
                    return {
                        "taskId": "task-update",
                        "message": "Luma installer finished",
                        "installRef": payload["installRef"],
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task):
                    result = handle_fleet_update(
                        state["deployToken"],
                        {"installRef": "v0.1.235", "nodeNames": ["tecent"]},
                    )

                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["failed"], 0)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_fleet_update_fails_fast_when_legacy_cn_agent_cannot_receive_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.106.154.3:4647"
                state["nodes"] = {
                    "tecent": {
                        "region": "cn",
                        "labels": {"region": "cn", "luma.node.name": "tecent"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )

                with patch("luma.control.server._run_node_agent_task") as run:
                    result = handle_fleet_update(
                        state["deployToken"],
                        {"installRef": "v0.1.235", "nodeNames": ["tecent"]},
                    )

                run.assert_not_called()
                self.assertEqual(result["failed"], 1)
                self.assertIn("one-time exact-ref bootstrap", result["results"][0]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_fleet_update_explicit_empty_target_list_updates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    },
                }
                save_state(state)

                with patch("luma.control.server._run_node_agent_task") as run:
                    result = handle_fleet_update(
                        state["deployToken"],
                        {"installRef": "v0.1.173", "includeAll": True, "nodeNames": []},
                    )

                run.assert_not_called()
                self.assertEqual(result["total"], 0)
                self.assertEqual(result["results"], [])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_verifies_new_agent_heartbeat_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "version": "0.1.172",
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    },
                }
                save_state(state)

                def run_task(*_args, **_kwargs):
                    current = load_state()
                    current["nodes"]["lab"]["agent"]["lastSeen"] = int(time.time()) + 2
                    current["nodes"]["lab"]["agent"]["version"] = "0.1.173"
                    save_state(current)
                    return {"taskId": "task-1", "message": "installer finished", "installRef": "v0.1.173"}

                events = []
                with patch("luma.control.server._run_node_agent_task", side_effect=run_task), patch(
                    "luma.control.server.time.sleep", return_value=None
                ):
                    result = handle_fleet_update(
                        state["deployToken"],
                        {
                            "installRef": "v0.1.173",
                            "includeAll": True,
                            "nodeNames": ["lab"],
                            "waitReadySeconds": 5,
                        },
                        progress=events.append,
                    )

                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["results"][0]["agentVersionBefore"], "0.1.172")
                self.assertEqual(result["results"][0]["agentVersionAfter"], "0.1.173")
                self.assertIn("installing", [event["status"] for event in events])
                self.assertIn("verifying", [event["status"] for event in events])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_commit_update_verifies_reported_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "version": "0.1.221",
                            "os": "linux",
                            "capabilities": ["luma-update"],
                        },
                    },
                }
                save_state(state)

                def run_task(*_args, **_kwargs):
                    current = load_state()
                    current["nodes"]["lab"]["agent"]["lastSeen"] = int(time.time()) + 2
                    current["nodes"]["lab"]["agent"]["version"] = "0.1.222"
                    save_state(current)
                    return {
                        "taskId": "task-1",
                        "message": "installer finished",
                        "installRef": "a" * 40,
                        "installedVersion": "0.1.222",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task), patch(
                    "luma.control.server.time.sleep", return_value=None
                ):
                    result = handle_fleet_update(
                        state["deployToken"],
                        {
                            "installRef": "a" * 40,
                            "nodeNames": ["lab"],
                            "waitReadySeconds": 5,
                        },
                    )

                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["results"][0]["installedVersion"], "0.1.222")
                self.assertEqual(result["results"][0]["agentVersionAfter"], "0.1.222")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_skips_manager_nodes_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "manager": {
                        "region": "cn",
                        "status": "manager",
                        "swarmRole": "manager",
                        "swarmManager": True,
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["luma-update"]},
                    },
                    "home-mac-mini": {
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": now, "os": "darwin", "capabilities": ["luma-update"]},
                    },
                }
                save_state(state)

                def run_task(_state, node_name, action, payload, **_kwargs):
                    self.assertEqual(node_name, "home-mac-mini")
                    self.assertEqual(action, "update-luma")
                    return {"message": f"updated {node_name}", "installRef": payload.get("installRef") or ""}

                with patch("luma.control.server._run_node_agent_task", side_effect=run_task) as run:
                    result = handle_fleet_update(state["deployToken"], {"includeAll": True})

                run.assert_called_once()
                by_name = {item["nodeName"]: item for item in result["results"]}
                self.assertEqual(by_name["manager"]["status"], "skipped")
                self.assertIn("manager node is skipped", by_name["manager"]["message"])
                self.assertEqual(by_name["home-mac-mini"]["status"], "succeeded")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_can_include_manager_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager": {
                        "region": "cn",
                        "status": "manager",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "os": "linux", "capabilities": ["luma-update"]},
                    },
                }
                save_state(state)

                with patch("luma.control.server._run_node_agent_task", return_value={"message": "updated manager"}) as run:
                    result = handle_fleet_update(state["deployToken"], {"includeManager": True})

                run.assert_called_once()
                self.assertEqual(result["succeeded"], 1)
                self.assertEqual(result["results"][0]["nodeName"], "manager")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_fleet_update_skips_ready_agents_without_update_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "old-agent": {
                        "region": "home",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "os": "linux", "capabilities": ["docker-volume"]},
                    }
                }
                save_state(state)

                with patch("luma.control.server._run_node_agent_task") as run:
                    result = handle_fleet_update(state["deployToken"], {"includeAll": True})
                run.assert_not_called()
                self.assertEqual(result["succeeded"], 0)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["results"][0]["status"], "skipped")
                self.assertIn("does not support fleet update", result["results"][0]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_register_rejects_unknown_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaises(Exception):
                    handle_node_register(state["joinToken"], {"nodeName": "b", "region": "mars"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_registered_only_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nomadRpcAddr"] = "100.64.0.1:4647"
                save_state(state)
                handle_node_register(state["joinToken"], {"nodeName": "m3max", "region": "home"})
                result = handle_node_unregister(state["deployToken"], {"nodeName": "m3max"})
                saved = load_state()
                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertNotIn("m3max", saved.get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_removes_registered_nomad_node_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "m4mini": {
                        "region": "home",
                        "displayName": "m4mini",
                        "hostname": "orbstack",
                        "nodeId": "node-id-1",
                        "nomadNodeId": "node-id-1",
                        "labels": {"luma.node.name": "m4mini", "luma.node.id": "node-id-1", "region": "home"},
                    }
                }
                save_state(state)
                nomad = Mock()
                with patch("luma.control.server.docker_request") as docker, patch(
                    "luma.control.server.NomadApi", return_value=nomad
                ):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                docker.assert_not_called()
                self.assertGreaterEqual(nomad.request.call_count, 2)
                self.assertEqual(
                    nomad.request.call_args_list[0].args,
                    (
                        "POST",
                        "/v1/node/node-id-1/drain",
                        {
                            "DrainSpec": {"Deadline": 0, "IgnoreSystemJobs": True},
                            "MarkEligible": False,
                            "Meta": {"message": "removed by Luma node remove: m4mini"},
                        },
                    ),
                )
                self.assertEqual(
                    nomad.request.call_args_list[1].args,
                    ("POST", "/v1/node/node-id-1/eligibility", {"Eligibility": "ineligible"}),
                )
                self.assertNotIn("m4mini", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_prefers_saved_luma_record_over_duplicate_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "m4mini": {
                        "region": "home",
                        "status": "labeled",
                        "displayName": "m4mini",
                        "hostname": "orbstack",
                        "nodeId": "stale-node-id",
                        "nomadNodeId": "stale-node-id",
                        "labels": {"luma.node.name": "m4mini", "luma.node.id": "stale-node-id", "region": "home"},
                    },
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "hostname": "orbstack",
                        "nodeId": "active-node-id",
                        "nomadNodeId": "active-node-id",
                        "labels": {"luma.node.name": "home-mac-mini", "luma.node.id": "active-node-id", "region": "home"},
                    },
                }
                save_state(state)
                nomad = Mock()
                with patch("luma.control.server.docker_request") as docker, patch(
                    "luma.control.server.NomadApi", return_value=nomad
                ):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                docker.assert_not_called()
                self.assertEqual(nomad.request.call_args_list[0].args[1], "/v1/node/stale-node-id/drain")
                saved_nodes = load_state().get("nodes", {})
                self.assertNotIn("m4mini", saved_nodes)
                self.assertIn("home-mac-mini", saved_nodes)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_stale_alias_never_drains_shared_manager_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                shared_id = "manager-node-id"
                state["nodes"] = {
                    "aly": {
                        "region": "cn",
                        "status": "labeled",
                        # Live Nomad reconciliation can copy server identity
                        # fields onto the stale alias because both records share
                        # one node ID. The authoritative sibling still makes
                        # alias-only removal safe.
                        "nomadRole": "server",
                        "nomadServer": True,
                        "nodeId": shared_id,
                        "labels": {
                            "luma.node.name": "aly",
                            "luma.node.id": shared_id,
                            "region": "cn",
                        },
                    },
                    "manager-hostname": {
                        "region": "cn",
                        "status": "manager",
                        "nomadRole": "server",
                        "nomadServer": True,
                        "nodeId": shared_id,
                        "nomadNodeId": shared_id,
                        "labels": {
                            "luma.node.name": "manager-hostname",
                            "luma.node.id": shared_id,
                            "role.nomad-manager": "true",
                            "region": "cn",
                        },
                    },
                }
                save_state(state)
                with patch("luma.control.server.NomadApi") as nomad:
                    result = handle_node_unregister(
                        state["deployToken"], {"nodeName": "aly"}
                    )

                self.assertTrue(result["removed"])
                self.assertTrue(result["registeredRemoved"])
                self.assertFalse(result["nomadDrained"])
                self.assertEqual(
                    result["nomadDrainSkipped"], "shared_manager_identity"
                )
                nomad.assert_not_called()
                saved_nodes = load_state().get("nodes", {})
                self.assertNotIn("aly", saved_nodes)
                self.assertIn("manager-hostname", saved_nodes)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_reports_missing_node_without_docker_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                with patch("luma.control.server.docker_request") as docker:
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertFalse(result["removed"])
                self.assertFalse(result["registeredRemoved"])
                docker.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_drains_nomad_node_left_after_prior_state_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "luma.yaml"
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(config_path))
            try:
                config_path.write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                nomad = Mock()
                nomad.request.side_effect = [
                    [{"ID": "node-id-tecent", "Name": "VM-0-10-ubuntu"}],
                    {"Meta": {"luma_node_name": "tecent"}},
                    {},
                    {},
                ]

                with patch("luma.control.server.NomadApi", return_value=nomad):
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "tecent"})

                self.assertTrue(result["removed"])
                self.assertFalse(result["registeredRemoved"])
                self.assertTrue(result["nomadDrained"])
                self.assertEqual(result["nomadNodeId"], "node-id-tecent")
                self.assertEqual(nomad.request.call_args_list[2].args[1], "/v1/node/node-id-tecent/drain")
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_refuses_to_remove_manager_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"manager": {"region": "cn", "status": "manager", "nomadRole": "server"}}
                save_state(state)
                with self.assertRaisesRegex(LumaError, "refusing to unregister Nomad manager"):
                    handle_node_unregister(state["deployToken"], {"nodeName": "manager"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_unregister_does_not_call_docker_when_removing_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"m4mini": {"region": "home", "displayName": "m4mini"}}
                save_state(state)
                with patch("luma.control.server.docker_request", side_effect=LumaError("Docker unavailable")) as docker:
                    result = handle_node_unregister(state["deployToken"], {"nodeName": "m4mini"})

                self.assertTrue(result["removed"])
                docker.assert_not_called()
                self.assertNotIn("m4mini", load_state().get("nodes", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_resolves_luma_node_name_to_nomad_meta_constraint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "mac-mini-gaojiu": {
                        "region": "home",
                        "status": "labeled",
                        "hostname": "orbstack",
                        "nodeId": "node-id-gaojiu",
                        "nomadNodeId": "node-id-gaojiu",
                        "labels": {
                            "region": "home",
                            "luma.node.name": "mac-mini-gaojiu",
                            "luma.node.id": "node-id-gaojiu",
                        },
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "ghcr.io/me/home-panel:1",
                        "region": "home",
                        "node": "mac-mini-gaojiu",
                        "exposure": "none",
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "home-panel.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertEqual(result["service"], "home-panel")
                stack = (root / "stacks" / "home" / "home-panel" / "home-panel.nomad.json").read_text(encoding="utf-8")
                self.assertIn('"LTarget": "${meta.luma_node_name}"', stack)
                self.assertIn('"RTarget": "mac-mini-gaojiu"', stack)
                self.assertNotIn("node.hostname", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_refreshes_nomad_cni_hostports_after_nomad_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["nomad-cni-repair"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "blg",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [{"node": "blg", "deleted": 1}], "skipped": []},
                    create=True,
                ) as refresh, patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(result["cniHostports"]["nodes"], ["blg"])
                refresh.assert_called_once()
                self.assertEqual(refresh.call_args.args[2], "api")
                self.assertEqual(refresh.call_args.kwargs["fallback_nodes"], ["blg"])
                self.assertEqual(refresh.call_args.kwargs["ports"], [18080])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_does_not_truncate_live_route_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "blg",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                route_target = root / "routes" / "api.yml"
                original_write_text = Path.write_text

                def guarded_write_text(path, *args, **kwargs):
                    if Path(path) == route_target:
                        raise AssertionError("route file was overwritten directly")
                    return original_write_text(path, *args, **kwargs)

                with patch.object(Path, "write_text", autospec=True, side_effect=guarded_write_text), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ), patch("luma.control.server.sync_dns", return_value="DNS skipped"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [], "skipped": []},
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(result["service"], "api")
                self.assertIn("http://100.64.0.3:18080", route_target.read_text(encoding="utf-8"))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_stages_route_write_outside_watched_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                    }
                }
                save_state(state)
                routes_root = root / "routes"
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(routes_root)}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "blg",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                route_target = routes_root / "api.yml"
                replaced_sources: list[Path] = []
                real_replace = os.replace

                def record_replace(src, dst):
                    if Path(dst) == route_target:
                        replaced_sources.append(Path(src))
                    return real_replace(src, dst)

                with patch("luma.control.server.os.replace", side_effect=record_replace), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ), patch("luma.control.server.sync_dns", return_value="DNS skipped"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [], "skipped": []},
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertTrue(replaced_sources)
                self.assertNotEqual(replaced_sources[0].parent, routes_root)
                self.assertNotIn(routes_root, replaced_sources[0].parents)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_route_write_falls_back_when_staging_crosses_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                    }
                }
                save_state(state)
                routes_root = root / "routes"
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(routes_root)}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "blg",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                route_target = routes_root / "api.yml"
                real_replace = os.replace
                exdev_raised = False
                fallback_sources: list[Path] = []

                def replace_with_cross_device_once(src, dst):
                    nonlocal exdev_raised
                    if Path(dst) == route_target and not exdev_raised:
                        exdev_raised = True
                        raise OSError(errno.EXDEV, "Invalid cross-device link")
                    if Path(dst) == route_target:
                        fallback_sources.append(Path(src))
                    return real_replace(src, dst)

                with patch("luma.control.server.os.replace", side_effect=replace_with_cross_device_once), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})
                ), patch("luma.control.server.sync_dns", return_value="DNS skipped"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [], "skipped": []},
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertTrue(exdev_raised)
                self.assertTrue(fallback_sources)
                self.assertEqual(fallback_sources[-1].parent, routes_root / ".luma-route-staging")
                self.assertEqual(fallback_sources[-1].suffix, ".tmp")
                self.assertFalse(list((routes_root / ".luma-route-staging").glob("*.tmp")))
                self.assertIn("http://100.64.0.3:18080", route_target.read_text(encoding="utf-8"))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_validates_route_before_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "tailscaleIP": "100.64.0.3",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                    }
                }
                save_state(state)
                routes_root = root / "routes"
                routes_root.mkdir()
                route_target = routes_root / "api.yml"
                previous_route = "http:\n  routers:\n    api:\n      rule: Host(`api.example.com`)\n"
                route_target.write_text(previous_route, encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(routes_root)}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "blg",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [], "skipped": []},
                ), patch("luma.control.server.render_tailscale_route", return_value="not: a-traefik-route\n"):
                    with self.assertRaisesRegex(LumaError, "invalid route file"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(route_target.read_text(encoding="utf-8"), previous_route)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_waits_for_rollout_before_recreating_gateway_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "blg",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                        "publishPort": 18080,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [], "skipped": []},
                ), patch("luma.control.server.resolve_nomad_static_route_target", side_effect=lambda service, _state, **_kwargs: service), patch(
                    "luma.control.server._probe_public_route",
                    side_effect=[
                        LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 504"),
                        "Public route reachable: https://api.example.com/ -> HTTP 200",
                    ],
                ) as probe, patch(
                    "luma.control.server.handle_application_restart",
                    return_value={"mode": "recreate", "restarted": [{"allocId": "alloc-api", "task": "*", "mode": "recreate"}]},
                ) as restart:
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(probe.call_count, 2)
                restart.assert_not_called()
                self.assertIn("Public route settled after rollout", result["probe"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertNotIn("Recover application upstream", steps)
                self.assertIn("Probe public route=ok:Public route settled after rollout", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_reconciles_traefik_404_without_restarting_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "defaults": {
                                "stackRoot": str(root / "stacks"),
                                "routesRoot": str(root / "routes"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                stale_route = routes / "api.yml"
                stale_route.write_text(
                    "http:\n  routers:\n    api: {}\n  services:\n    api: {}\n",
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {})), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": [], "results": [], "skipped": []},
                ), patch(
                    "luma.control.server._probe_public_route",
                    side_effect=[
                        LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 404 (Traefik router not found)"),
                        "Public route reachable: https://api.example.com/ -> HTTP 200",
                    ],
                ) as probe, patch(
                    "luma.control.server.handle_application_restart",
                    return_value={"mode": "recreate", "restarted": [{"allocId": "alloc-api", "task": "*", "mode": "recreate"}]},
                ) as restart:
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(probe.call_count, 2)
                restart.assert_not_called()
                self.assertFalse(stale_route.exists())
                self.assertIn("Recovered public route after provider reconciliation", result["probe"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Remove stale file-provider route=ok:Removed stale file-provider route", steps)
                self.assertIn("Reconcile Traefik provider=ok:waiting for Nomad provider convergence", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_persistent_traefik_404_recreates_ingress_not_application(self):
        from luma.control.server import _probe_public_route_with_recovery

        service = ServiceSpec(
            source=Path("api.yaml"),
            name="api",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="api.example.com",
            port=80,
        )
        miss = LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 404 (Traefik router not found)")
        steps = []
        with patch("luma.control.server._probe_public_route", side_effect=miss), patch(
            "luma.control.server._wait_for_public_route",
            side_effect=[miss, "Public route reachable: https://api.example.com/ -> HTTP 200"],
        ) as wait, patch(
            "luma.control.server._recover_traefik_ingress",
            return_value="Traefik allocation recreated (new-traefik)",
        ) as recover_ingress, patch(
            "luma.control.server._recover_public_route_allocation"
        ) as recover_app:
            result = _probe_public_route_with_recovery(
                "deploy-token",
                service,
                stack="api",
                skip_orchestrator=False,
                steps=steps,
            )

        self.assertEqual(wait.call_count, 2)
        recover_ingress.assert_called_once_with()
        recover_app.assert_not_called()
        self.assertIn("Recovered public route after Traefik recreate", result)

    def test_persistent_gateway_failure_waits_then_recreates_application(self):
        from luma.control.server import _probe_public_route_with_recovery

        service = ServiceSpec(
            source=Path("api.yaml"),
            name="api",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="api.example.com",
            port=80,
        )
        gateway = LumaError("Public route unhealthy: https://api.example.com/ -> HTTP 502")
        steps = []
        with patch("luma.control.server._probe_public_route", side_effect=gateway), patch(
            "luma.control.server._wait_for_public_route",
            side_effect=[gateway, "Public route reachable: https://api.example.com/ -> HTTP 200"],
        ) as wait, patch(
            "luma.control.server._recover_public_route_allocation",
            return_value="allocation recreate completed (1 replaced by 1 running allocation(s))",
        ) as recover_app, patch(
            "luma.control.server._recover_traefik_ingress"
        ) as recover_ingress:
            result = _probe_public_route_with_recovery(
                "deploy-token",
                service,
                stack="api",
                skip_orchestrator=False,
                steps=steps,
            )

        self.assertEqual(wait.call_count, 2)
        recover_app.assert_called_once_with("deploy-token", "api")
        recover_ingress.assert_not_called()
        self.assertIn("Recovered public route after allocation recreate", result)

    def test_nomad_node_pin_does_not_require_swarm_node_id(self):
        state = {"nodes": {"lab": {"name": "lab", "region": "home", "status": "ready"}}}
        service = ServiceSpec(
            source=Path("kato.yaml"),
            name="kato",
            image="ghcr.io/liutianjie/kato:latest",
            region="home",
            node="lab",
            exposure="none",
        )
        resolved = resolve_service_node_pin(service, state, engine="nomad")
        self.assertEqual(resolved.node, "lab")
        self.assertIsNone(resolved.node_id)

    def test_node_record_lookup_accepts_aliases(self):
        nodes = {
            "gaojiu": {
                "displayName": "gaojiu",
                "aliases": ["home-mac-mini", "Mac.lan"],
                "region": "home",
            }
        }

        self.assertIs(_node_record_for_name(nodes, "gaojiu"), nodes["gaojiu"])
        self.assertIs(_node_record_for_name(nodes, "home-mac-mini"), nodes["gaojiu"])
        self.assertIs(_node_record_for_name(nodes, "Mac.lan"), nodes["gaojiu"])

    def test_dashboard_nodes_accept_terminal_alias_connections(self):
        from luma.control.server import _dashboard_nodes, _registered_nodes_summary, _update_agent_heartbeat

        record = {}
        _update_agent_heartbeat(record, {"version": "0.1.173", "capabilities": ["terminal"], "os": "linux"})
        registered = _registered_nodes_summary({"bot": record})
        self.assertEqual(registered[0]["agentVersion"], "0.1.173")

        rows = _dashboard_nodes(
            [
                {
                    "name": "bot",
                    "displayName": "bot",
                    "hostname": "global-sg-1",
                    "aliases": ["global-sg-1"],
                    "agentStatus": "ready",
                    "agentVersion": "0.1.173",
                    "storageCapabilities": ["terminal"],
                }
            ],
            [],
            terminal_nodes={"global-sg-1"},
        )

        self.assertEqual(rows[0]["name"], "bot")
        self.assertTrue(rows[0]["terminalConnected"])
        self.assertEqual(rows[0]["terminalStatus"], "connected")
        self.assertEqual(rows[0]["agentVersion"], "0.1.173")

    def test_state_nodes_expands_aliases_for_internal_resolution(self):
        state = {
            "nodes": {
                "aly": {
                    "displayName": "aly",
                    "aliases": ["iZ0jl8auywzycory05d9cuZ"],
                    "region": "cn",
                }
            }
        }

        nodes = _state_nodes(state)
        self.assertEqual(nodes["aly"]["region"], "cn")
        self.assertEqual(nodes["iZ0jl8auywzycory05d9cuZ"]["region"], "cn")

    def test_pinned_deployment_validates_image_for_target_node_platform_before_stack_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "nodeId": "node-id-home",
                        "nomadNodeId": "node-id-home",
                        "platform": {"os": "linux", "arch": "aarch64"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image"],
                        },
                        "labels": {"region": "home", "luma.node.name": "home-mac-mini", "luma.node.id": "node-id-home"},
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "pura",
                        "image": "ghcr.io/acme/pura:main",
                        "region": "home",
                        "node": "home-mac-mini",
                        "exposure": "none",
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=LumaError("target node Docker pull failed; image does not provide a manifest for target platform linux/arm64"),
                ) as agent:
                    with self.assertRaisesRegex(LumaError, "target platform linux/arm64"):
                        handle_deployment(
                            state["deployToken"],
                            {"manifest": manifest, "sourceName": "pura.yaml", "skipDns": True, "skipOrchestrator": True},
                        )
                agent.assert_called_once()
                self.assertEqual(agent.call_args.args[1], "home-mac-mini")
                self.assertEqual(agent.call_args.args[2], "resolve-docker-image")
                self.assertEqual(agent.call_args.args[3]["platform"], "linux/arm64")
                self.assertFalse((root / "stacks" / "home" / "pura" / "pura.nomad.json").exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_deployment_renders_target_platform_manifest_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "nodeId": "node-id-home",
                        "nomadNodeId": "node-id-home",
                        "nomadAttributes": {"os.name": "linux", "cpu.arch": "aarch64"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image"],
                        },
                        "labels": {"region": "home", "luma.node.name": "home-mac-mini", "luma.node.id": "node-id-home"},
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "pura",
                        "image": "ghcr.io/acme/pura:main",
                        "region": "home",
                        "node": "home-mac-mini",
                        "exposure": "none",
                    }
                )
                digest = "ghcr.io/acme/pura@sha256:58307df1e4f8efcfec29a8f7a6653c65446d6afded7d03caa3329f2c0ac92719"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_registry_image_digest", return_value=digest
                ), patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"deployed": digest, "digest": digest, "message": "Target node image pull ready"},
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "pura.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                agent.assert_called_once()
                self.assertEqual(agent.call_args.args[1], "home-mac-mini")
                self.assertEqual(agent.call_args.args[2], "resolve-docker-image")
                self.assertEqual(agent.call_args.args[3]["platform"], "linux/arm64")
                stack = (root / "stacks" / "home" / "pura" / "pura.nomad.json").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertEqual(result["image"]["platform"], "linux/arm64")
                self.assertEqual(result["image"]["resolvedBy"], "target-node")
                self.assertIn(f'"image": "{digest}"', stack)
                self.assertNotIn("ghcr.io/acme/pura:main", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_private_deployment_sends_registry_auth_to_target_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "region": "home",
                        "status": "labeled",
                        "nodeId": "node-id-lab",
                        "nomadNodeId": "node-id-lab",
                        "nomadAttributes": {"os.name": "linux", "cpu.arch": "amd64"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image"],
                        },
                        "labels": {"region": "home", "luma.node.name": "lab", "luma.node.id": "node-id-lab"},
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "gcode.gaojiua.com:3000", "username": "nick", "password": "secret"},
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "docs",
                        "image": "gcode.gaojiua.com:3000/acme/docs:latest",
                        "region": "home",
                        "node": "lab",
                        "exposure": "none",
                    }
                )
                digest = "gcode.gaojiua.com:3000/acme/docs@sha256:58307df1e4f8efcfec29a8f7a6653c65446d6afded7d03caa3329f2c0ac92719"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_registry_image_digest", return_value=digest
                ), patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"deployed": digest, "digest": digest, "message": "Target node image pull ready"},
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "docs.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                payload = agent.call_args.args[3]
                self.assertEqual(payload["image"], digest)
                self.assertEqual(payload["platform"], "linux/amd64")
                self.assertEqual(payload["registryAuth"]["password"], "secret")
                self.assertTrue(result["image"]["registryAuth"])
                stack = (root / "stacks" / "home" / "docs" / "docs.nomad.json").read_text(encoding="utf-8")
                self.assertIn(f'"image": "{digest}"', stack)
                self.assertIn('"server_address": "gcode.gaojiua.com:3000"', stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_refuses_pinned_down_nomad_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "region": "home",
                        "status": "labeled",
                        "hostname": "orbstack",
                        "nodeId": "node-id-home",
                        "nomadNodeId": "node-id-home",
                        "nomadStatus": "down",
                        "labels": {"region": "home", "luma.node.name": "home-mac-mini", "luma.node.id": "node-id-home"},
                    }
                }
                service = ServiceSpec(
                    source=Path("home-panel.yaml"),
                    name="home-panel",
                    image="ghcr.io/me/home-panel:1",
                    region="home",
                    node="home-mac-mini",
                    exposure="none",
                )
                with self.assertRaisesRegex(LumaError, "Nomad node is down"):
                    resolve_service_node_pin(service, state)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_refuses_pinned_nomad_node_when_scheduling_ineligible(self):
        state = {
            "nodes": {
                "home-mac-mini": {
                    "region": "home",
                    "nodeId": "home-node-id",
                    "nomadNodeId": "home-node-id",
                    "schedulingEligibility": "ineligible",
                    "labels": {"luma.node.name": "home-mac-mini", "luma.node.id": "home-node-id"},
                }
            }
        }
        service = ServiceSpec(
            source=Path("home-panel.yaml"),
            name="home-panel",
            image="ghcr.io/me/home-panel:1",
            region="home",
            node="home-mac-mini",
            exposure="none",
        )
        with self.assertRaisesRegex(LumaError, "scheduling eligibility is ineligible"):
            resolve_service_node_pin(service, state)

    def test_deployment_uses_control_state_and_portainer_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {"type": "cloudflare", "zone": "example.com"},
                                "portainer": {},
                            },
                            "defaults": {"stackRoot": str(root / "stacks")},
                            "nodes": {
                                "edge": {
                                    "host": "edge",
                                    "publicIp": "203.0.113.10",
                                    "region": "cn",
                                    "roles": ["edge"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                probe_error = urllib.error.HTTPError("https://api.example.com/", 404, "not found", {}, None)
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns", return_value="DNS updated"
                ), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertIn(str(root / "stacks" / "cn" / "api" / "api.nomad.json"), result["written"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Parse manifest=ok:api -> cn/cn-edge", steps)
                self.assertIn("Sync DNS=ok:DNS updated", steps)
                self.assertIn("Deploy Nomad job=ok:Nomad job deployed", steps)
                self.assertIn("Probe public route=ok:Public route reachable: https://api.example.com/ -> HTTP 404", steps)
                deployment_state = load_state()
                self.assertEqual(deployment_state["deployments"]["services"]["api"]["name"], "api")
                self.assertEqual(deployment_state["deployments"]["services"]["api"]["manifest"], manifest)
                with self.assertRaises(Exception):
                    handle_deployment(state["joinToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_tailscale_relay_public_probe_checks_https_domain(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("home-panel.yaml"),
            name="home-panel",
            image="nginx:alpine",
            region="home",
            exposure="tailscale-relay",
            domain="panel.example.com",
            port=8080,
        )
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch("luma.control.server.urllib.request.urlopen", return_value=response) as urlopen:
            result = _probe_public_route(service)

        self.assertEqual(result, "Public route reachable: https://panel.example.com/ -> HTTP 200")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://panel.example.com/")
        self.assertEqual(urlopen.call_args.args[0].get_method(), "HEAD")

    def test_public_probe_rejects_gateway_statuses(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("home-panel.yaml"),
            name="home-panel",
            image="nginx:alpine",
            region="home",
            exposure="tailscale-relay",
            domain="panel.example.com",
            port=8080,
        )
        probe_error = urllib.error.HTTPError("https://panel.example.com/", 504, "gateway timeout", {}, None)
        with patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
            with self.assertRaisesRegex(LumaError, "Public route unhealthy: https://panel.example.com/ -> HTTP 504"):
                _probe_public_route(service)

    def test_public_probe_rejects_traefik_route_miss_404(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("panel.yaml"),
            name="panel",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="panel.example.com",
            port=80,
        )
        probe_error = urllib.error.HTTPError(
            "https://panel.example.com/",
            404,
            "not found",
            {"Server": "Traefik", "Content-Type": "text/plain; charset=utf-8"},
            io.BytesIO(b"404 page not found\n"),
        )
        with patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
            with self.assertRaisesRegex(LumaError, "Traefik router not found"):
                _probe_public_route(service)

    def test_public_probe_allows_app_default_404_body_without_traefik_header(self):
        from luma.control.server import _probe_public_route

        service = ServiceSpec(
            source=Path("panel.yaml"),
            name="panel",
            image="nginx:alpine",
            region="cn",
            exposure="cn-edge",
            domain="panel.example.com",
            port=80,
        )
        probe_error = urllib.error.HTTPError(
            "https://panel.example.com/",
            404,
            "not found",
            {"Content-Type": "text/plain; charset=utf-8"},
            io.BytesIO(b"404 page not found\n"),
        )
        with patch("luma.control.server.urllib.request.urlopen", side_effect=probe_error):
            result = _probe_public_route(service)

        self.assertEqual(result, "Public route reachable: https://panel.example.com/ -> HTTP 404 (the app may not serve /)")

    def test_deployment_failure_after_manager_work_leaves_failed_partial_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch("luma.control.server.sync_dns", return_value="DNS updated"), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ), patch("luma.control.server._probe_public_route", side_effect=LumaError("route refused")):
                    with self.assertRaisesRegex(LumaError, "Probe public route failed"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                deployment = load_state()["deployments"]["services"]["api"]
                self.assertEqual(deployment["status"], "failed_partial")
                self.assertIn("Probe public route failed", deployment["lastError"])
                self.assertEqual(deployment["manifest"], manifest)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in deployment["steps"])
                self.assertIn("Sync DNS=ok:DNS updated", steps)
                self.assertIn("Deploy Nomad job=ok:Nomad job deployed", steps)
                self.assertIn("Probe public route=fail:route refused", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_deployment_preview_renders_without_writing_or_deploying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "edge": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "edge-node-id",
                        "labels": {"luma.node.id": "edge-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "node": "edge",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.sync_dns") as sync, patch("luma.control.server.deploy_to_nomad") as deploy:
                    result = handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["summary"]["exposure"], "cn-edge")
                self.assertEqual(result["artifacts"][0]["kind"], "job")
                self.assertIn('"LTarget": "${meta.luma_node_name}"', result["artifacts"][0]["content"])
                self.assertIn('"RTarget": "edge"', result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
                sync.assert_not_called()
                deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_preview_keeps_secret_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"API_PASSWORD": "${API_PASSWORD}"},
                    }
                )
                result = handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})

                self.assertEqual(result["service"], "api")
                self.assertIn('"API_PASSWORD": "${API_PASSWORD}"', result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_deployment_rejects_existing_compose_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "api": {
                            "kind": "compose",
                            "name": "api",
                            "slug": "api",
                            "manifest": "name: api\nregion: cn\ncompose: docker-compose.yml\n",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                with patch("luma.control.server.deploy_to_nomad") as deploy:
                    with self.assertRaisesRegex(LumaError, "deployment name already exists"):
                        handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                deploy.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_config_returns_saved_service_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api.yaml",
                            "updatedAt": 123,
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                result = handle_deployment_config(state["deployToken"], "api")
                self.assertEqual(result["kind"], "service")
                self.assertEqual(result["name"], "api")
                self.assertEqual(result["sourceName"], "console:api.yaml")
                self.assertEqual(result["updatedAt"], 123)
                self.assertEqual(result["manifest"], manifest)
                self.assertEqual(result["composeContent"], "")
                with self.assertRaisesRegex(LumaError, "unauthorized"):
                    handle_deployment_config(state["joinToken"], "api")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_config_returns_saved_compose_manifest_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "app-stack": {
                            "kind": "compose",
                            "name": "app-stack",
                            "slug": "app-stack",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:luma.compose.yml",
                            "updatedAt": 456,
                        }
                    },
                }
                save_state(state)
                result = handle_deployment_config(state["deployToken"], "app-stack")
                self.assertEqual(result["kind"], "compose")
                self.assertEqual(result["name"], "app-stack")
                self.assertEqual(result["manifest"], sidecar)
                self.assertEqual(result["composeContent"], compose)
                self.assertEqual(result["updatedAt"], 456)
                with self.assertRaisesRegex(LumaError, "deployment not found"):
                    handle_deployment_config(state["deployToken"], "missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_preview_rejects_invalid_region_exposure_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "global",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with self.assertRaisesRegex(LumaError, "exposure=cn-edge requires region=cn"):
                    handle_deployment_preview(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_recreates_business_stack_allocations_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {"ID": "alloc-api", "ClientStatus": "running", "TaskStates": {"api": {}}},
                        {"ID": "alloc-worker", "ClientStatus": "running", "TaskStates": {"worker": {}}},
                        {"ID": "alloc-old", "ClientStatus": "complete", "TaskStates": {"old": {}}},
                    ],
                    {},
                    {},
                    [
                        {"ID": "alloc-api-new", "ClientStatus": "running", "TaskStates": {"api": {}}},
                        {"ID": "alloc-worker-new", "ClientStatus": "running", "TaskStates": {"worker": {}}},
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})
                self.assertEqual(result["stack"], "myapp")
                self.assertEqual(result["mode"], "recreate")
                self.assertEqual(len(result["restarted"]), 2)
                self.assertEqual(result["replacementAllocations"], ["alloc-api-new", "alloc-worker-new"])
                self.assertEqual(result["delivery"]["status"], "skipped")
                api.request.assert_any_call("GET", "/v1/job/myapp/allocations")
                api.request.assert_any_call("POST", "/v1/allocation/alloc-api/stop", None)
                api.request.assert_any_call("POST", "/v1/allocation/alloc-worker/stop", None)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_recreates_pending_allocation_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-pending",
                            "ClientStatus": "pending",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        },
                        {
                            "ID": "alloc-history",
                            "ClientStatus": "failed",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        },
                    ],
                    {},
                    {
                        "ID": "myapp",
                        "TaskGroups": [{"Name": "api", "Count": 1, "Tasks": [{"Name": "api"}]}],
                    },
                    {"EvalID": "eval-recovery"},
                    [
                        {
                            "ID": "alloc-new",
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        }
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["mode"], "recreate")
                self.assertEqual(result["replacementAllocations"], ["alloc-new"])
                self.assertEqual(result["recovery"]["strategy"], "force-evaluate")
                api.request.assert_any_call("POST", "/v1/allocation/alloc-pending/stop", None)
                api.request.assert_any_call(
                    "POST",
                    "/v1/job/myapp/evaluate",
                    {"JobID": "myapp", "EvalOptions": {"ForceReschedule": True}},
                )
                self.assertNotIn(
                    call("POST", "/v1/allocation/alloc-history/stop", None),
                    api.request.call_args_list,
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_recovers_unknown_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-unknown",
                            "ClientStatus": "unknown",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        }
                    ],
                    {},
                    [
                        {
                            "ID": "alloc-new",
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                            "TaskStates": {"api": {}},
                        }
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["replacementAllocations"], ["alloc-new"])
                api.request.assert_any_call("POST", "/v1/allocation/alloc-unknown/stop", None)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_forces_evaluation_when_allocations_are_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [],
                    {"ID": "myapp", "TaskGroups": [{"Name": "api", "Count": 1}]},
                    {"EvalID": "eval-recovery"},
                    [{"ID": "alloc-new", "ClientStatus": "running", "DesiredStatus": "run"}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["replacementAllocations"], ["alloc-new"])
                self.assertEqual(result["recovery"]["evaluationId"], "eval-recovery")
                api.request.assert_any_call(
                    "POST",
                    "/v1/job/myapp/evaluate",
                    {"JobID": "myapp", "EvalOptions": {"ForceReschedule": True}},
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_reports_blocked_pinned_node_instead_of_missing_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [],
                    {"ID": "myapp", "TaskGroups": [{"Name": "api", "Count": 1}]},
                    {"EvalID": "eval-blocked"},
                    [],
                    {
                        "ID": "eval-blocked",
                        "Status": "blocked",
                        "FailedTGAllocs": {
                            "api": {
                                "ConstraintFiltered": {
                                    "${meta.region} = home": 3,
                                    "${meta.luma_node_name} = blg": 4,
                                }
                            }
                        },
                    },
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    with self.assertRaisesRegex(
                        LumaError,
                        "requested node blg is unavailable, down, or scheduling-ineligible",
                    ):
                        handle_application_restart(state["deployToken"], {"stack": "myapp"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_reports_placement_failure_from_completed_parent_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [],
                    {"ID": "myapp", "TaskGroups": [{"Name": "api", "Count": 1}]},
                    {"EvalID": "eval-parent"},
                    [],
                    {
                        "ID": "eval-parent",
                        "Status": "complete",
                        "BlockedEval": "eval-child",
                        "FailedTGAllocs": {
                            "api": {
                                "ConstraintFiltered": {
                                    "${meta.region} = home": 3,
                                    "${meta.luma_node_name} = blg": 3,
                                }
                            }
                        },
                    },
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    with self.assertRaisesRegex(
                        LumaError,
                        "requested node blg is unavailable, down, or scheduling-ineligible",
                    ):
                        handle_application_restart(state["deployToken"], {"stack": "myapp"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_restores_gc_job_from_saved_deployment_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump(
                    {
                        "name": "myapp",
                        "image": "example/myapp:1",
                        "region": "home",
                        "exposure": "none",
                    }
                )
                state["deployments"] = {
                    "services": {
                        "myapp": {
                            "kind": "service",
                            "name": "myapp",
                            "slug": "myapp",
                            "manifest": manifest,
                            "sourceName": "deploy/myapp.luma.yml",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                api = Mock()
                api.request.side_effect = [
                    LumaError("Nomad API error 404: job not found"),
                    [{"ID": "alloc-restored", "ClientStatus": "running", "DesiredStatus": "run"}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server.handle_deployment",
                    return_value={"service": "myapp", "probe": "ready"},
                ) as deploy:
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["replacementAllocations"], ["alloc-restored"])
                self.assertEqual(result["recovery"], {"strategy": "stored-deployment", "kind": "service"})
                deploy.assert_called_once()
                self.assertEqual(deploy.call_args.args[0], state["deployToken"])
                self.assertEqual(deploy.call_args.args[1]["manifest"], manifest)
                self.assertEqual(deploy.call_args.args[1]["origin"], "application-restart-recovery")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_refreshes_nomad_cni_hostports_after_allocation_recreate(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "nodeId": "node-blg",
                        "region": "home",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["nomad-cni-repair"],
                        },
                    }
                }
                save_state(state)
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-api",
                            "ClientStatus": "running",
                            "TaskStates": {"api": {}},
                            "NodeID": "node-blg",
                            "NodeName": "ubuntu-general-1",
                            "AllocatedResources": {"Shared": {"Ports": [{"Label": "http", "Value": 14173, "To": 4173}]}},
                        },
                    ],
                    {},
                    [
                        {
                            "ID": "alloc-api-new",
                            "ClientStatus": "running",
                            "TaskStates": {"api": {}},
                            "NodeID": "node-blg",
                        }
                    ],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server._queue_node_agent_task",
                    return_value="task-cni",
                ) as queue, patch("luma.control.server._wait_node_agent_task", return_value={"deleted": 1, "staleAllocIds": ["old"]}):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp"})

                self.assertEqual(result["cniHostports"]["nodes"], ["blg"])
                queue.assert_called_once()
                self.assertEqual(queue.call_args.args[2], "repair-nomad-cni-hostports")
                self.assertEqual(queue.call_args.args[3], {"ports": [14173]})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_can_restart_single_task_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                api = Mock()
                api.request.side_effect = [
                    [
                        {"ID": "alloc-app", "ClientStatus": "running", "TaskStates": {"api": {}, "worker": {}}},
                    ],
                    {},
                ]
                with patch("luma.control.server.NomadApi", return_value=api):
                    result = handle_application_restart(state["deployToken"], {"stack": "myapp", "service": "api"})
                self.assertEqual(result["mode"], "task")
                self.assertEqual(result["restarted"], [{"allocId": "alloc-app", "task": "api", "mode": "task"}])
                api.request.assert_any_call("POST", "/v1/client/allocation/alloc-app/restart", {"TaskName": "api"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_application_restart_removes_stale_http_file_route_for_nomad_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "tecent": {
                        "name": "tecent",
                        "nodeId": "node-tecent",
                        "tailscaleIP": "100.64.29.91",
                    }
                }
                manifest = yaml.safe_dump(
                    {
                        "name": "linkshell-gateway",
                        "image": "example/gateway:1",
                        "region": "cn",
                        "public": True,
                        "exposure": "cn-edge",
                        "domain": "gateway.example.com",
                        "port": 8787,
                        "publishPort": 8787,
                    }
                )
                state["deployments"] = {
                    "services": {
                        "linkshell-gateway": {
                            "kind": "service",
                            "name": "linkshell-gateway",
                            "slug": "linkshell-gateway",
                            "manifest": manifest,
                            "sourceName": "gateway.yaml",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "defaults": {
                                "engine": "nomad",
                                "routesRoot": str(root / "routes"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                stale_route = routes / "linkshell-gateway.yml"
                stale_route.write_text(
                    "http:\n  routers:\n    linkshell-gateway: {}\n  services:\n    linkshell-gateway: {}\n",
                    encoding="utf-8",
                )
                api = Mock()
                api.request.side_effect = [
                    [
                        {
                            "ID": "alloc-old",
                            "ClientStatus": "running",
                            "TaskStates": {"gateway": {}},
                            "NodeID": "node-tecent",
                        }
                    ],
                    {},
                    [{"ID": "alloc-new", "ClientStatus": "running", "TaskStates": {"gateway": {}}}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server.sync_dns", return_value="DNS unchanged"
                ), patch(
                    "luma.control.server._wait_for_public_route", return_value="Public route reachable"
                ):
                    result = handle_application_restart(state["deployToken"], {"stack": "linkshell-gateway"})

                self.assertFalse(stale_route.exists())
                self.assertEqual(result["delivery"]["status"], "ready")
                self.assertEqual(
                    result["delivery"]["routes"],
                    [f"Removed stale file-provider route: {stale_route}"],
                )
                self.assertEqual(result["delivery"]["probes"], ["Public route reachable"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_removes_all_stale_compose_edge_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "tecent": {"name": "tecent", "nodeId": "node-tecent", "tailscaleIP": "100.64.29.91"}
                }
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "web": {"image": "example/web:1"},
                            "admin": {"image": "example/admin:1"},
                        }
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "multi-http",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "services": {
                            "web": {
                                "node": "tecent",
                                "exposure": "cn-edge",
                                "domain": "web.example.com",
                                "port": 3000,
                                "publishPort": 13000,
                            },
                            "admin": {
                                "node": "tecent",
                                "exposure": "cn-edge",
                                "domain": "admin.example.com",
                                "port": 3001,
                                "publishPort": 13001,
                            },
                        },
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "multi-http": {
                            "kind": "compose",
                            "name": "multi-http",
                            "slug": "multi-http",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "luma.compose.yml",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                web_route = routes / "multi-http-web.yml"
                admin_route = routes / "multi-http-admin.yml"
                for route in (web_route, admin_route):
                    route.write_text(
                        "http:\n  routers:\n    stale: {}\n  services:\n    stale: {}\n",
                        encoding="utf-8",
                    )
                api = Mock()
                api.request.side_effect = [
                    [{"ID": "alloc-old", "ClientStatus": "running", "TaskStates": {"web": {}, "admin": {}}}],
                    {},
                    [{"ID": "alloc-new", "ClientStatus": "running", "TaskStates": {"web": {}, "admin": {}}}],
                ]
                with patch("luma.control.server.NomadApi", return_value=api), patch(
                    "luma.control.server.sync_dns", return_value="DNS unchanged"
                ), patch(
                    "luma.control.server._wait_for_public_route", return_value="Public route reachable"
                ):
                    result = handle_application_restart(state["deployToken"], {"stack": "multi-http"})

                self.assertFalse(web_route.exists())
                self.assertFalse(admin_route.exists())
                self.assertEqual(len(result["delivery"]["routes"]), 2)
                self.assertEqual(len(result["delivery"]["probes"]), 2)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_application_restart_rejects_system_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with self.assertRaisesRegex(LumaError, "system stack"):
                    handle_application_restart(state["deployToken"], {"stack": "traefik"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_certificate_retry_reloads_matching_http_route_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                routes = root / "routes"
                routes.mkdir()
                route_file = routes / "tikhub.yml"
                route_text = yaml.safe_dump(
                    {
                        "http": {
                            "routers": {
                                "tikhub": {
                                    "rule": "Host(`tikhub.itool.tech`)",
                                    "entryPoints": ["websecure"],
                                    "tls": {"certResolver": "letsencrypt"},
                                    "service": "tikhub",
                                }
                            },
                            "services": {
                                "tikhub": {"loadBalancer": {"servers": [{"url": "http://100.64.0.10:8082"}]}}
                            },
                        }
                    }
                )
                route_file.write_text(route_text, encoding="utf-8")
                old_mtime = route_file.stat().st_mtime_ns
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                with patch("luma.control.server.NomadApi") as nomad_api:
                    result = handle_certificate_retry(
                        state["deployToken"],
                        {"domain": "tikhub.itool.tech", "routeId": "tikhub"},
                    )
                self.assertEqual(result["mode"], "route-file-reload")
                self.assertEqual(result["routeId"], "tikhub")
                self.assertEqual(result["certResolver"], "letsencrypt")
                self.assertEqual(route_file.read_text(encoding="utf-8"), route_text)
                self.assertGreaterEqual(route_file.stat().st_mtime_ns, old_mtime)
                nomad_api.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_certificate_retry_does_not_revalidate_atypical_route_file(self):
        # Cert retry rewrites a file's own bytes to trigger a reload; it must NOT
        # re-validate the (unchanged, already-live) file, whose on-disk shape may
        # predate the current renderer (e.g. an inline provider backend with no
        # sibling `services` map). Re-validating would newly reject a working file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                routes = root / "routes"
                routes.mkdir()
                route_file = routes / "legacy.yml"
                # HTTP router with a certResolver but no `services` map — would fail
                # _validate_route_file_text, which requires non-empty routers AND services.
                route_text = yaml.safe_dump(
                    {
                        "http": {
                            "routers": {
                                "legacy": {
                                    "rule": "Host(`legacy.itool.tech`)",
                                    "entryPoints": ["websecure"],
                                    "tls": {"certResolver": "letsencrypt"},
                                    "service": "legacy@file",
                                }
                            }
                        }
                    }
                )
                route_file.write_text(route_text, encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                with patch("luma.control.server.NomadApi") as nomad_api:
                    result = handle_certificate_retry(
                        state["deployToken"],
                        {"domain": "legacy.itool.tech", "routeId": "legacy"},
                    )
                self.assertEqual(result["mode"], "route-file-reload")
                self.assertEqual(route_file.read_text(encoding="utf-8"), route_text)
                nomad_api.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_certificate_retry_rejects_tcp_route_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                routes = root / "routes"
                routes.mkdir()
                (routes / "mysql.yml").write_text(
                    yaml.safe_dump(
                        {
                            "tcp": {
                                "routers": {"mysql": {"rule": "HostSNI(`*`)", "service": "mysql"}},
                                "services": {"mysql": {"loadBalancer": {"servers": [{"address": "100.64.0.10:3306"}]}}},
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(LumaError, "HTTP route file not found"):
                    handle_certificate_retry(state["deployToken"], {"domain": "mysql.itool.tech", "routeId": "mysql"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_cleans_dns_portainer_and_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "cf-token")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                stack_dir = root / "stacks" / "home" / "home-panel"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                route_file = root / "routes" / "home-panel.yml"
                route_file.parent.mkdir(parents=True)
                route_file.write_text("http:\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zoneId": "zone-id"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "ghcr.io/me/home-panel:1",
                        "region": "home",
                        "exposure": "tailscale-relay",
                        "domain": "panel.example.com",
                        "port": 8080,
                    }
                )
                state["deployments"] = {
                    "services": {
                        "home-panel": {
                            "kind": "service",
                            "name": "home-panel",
                            "slug": "home-panel",
                            "manifest": manifest,
                            "sourceName": "console:home-panel",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                with patch("luma.control.server.delete_dns", return_value="DNS deleted: panel.example.com") as dns, patch(
                    "luma.control.server.remove_from_nomad", return_value="Nomad job removed: home-panel"
                ) as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "home-panel"})
                dns.assert_called_once()
                stack.assert_called_once()
                self.assertFalse(stack_dir.exists())
                self.assertFalse(route_file.exists())
                self.assertEqual(result["service"], "home-panel")
                self.assertIn(str(stack_dir), result["files"])
                self.assertIn(str(route_file), result["files"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Delete DNS=ok:DNS deleted: panel.example.com", steps)
                self.assertIn("Remove Nomad job=ok:Nomad job removed: home-panel", steps)
                self.assertIn("Delete generated files=ok:Generated files removed", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_service_history_handler_returns_slugged_nomad_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")
                with patch(
                    "luma.control.server.job_versions",
                    return_value=[{"version": 3, "stable": True, "image": "app:v3"}],
                ) as versions:
                    result = handle_service_history(state["deployToken"], {"name": "My App"})
                versions.assert_called_once()
                self.assertEqual(versions.call_args.kwargs["slug"], "my-app")
                self.assertEqual(result["service"], "My App")
                self.assertEqual(result["slug"], "my-app")
                self.assertEqual(result["versions"][0]["image"], "app:v3")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_rollback_handler_reverts_requested_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad"}}), encoding="utf-8")
                with patch("luma.control.server.revert_job", return_value="Nomad job api reverted to v2") as revert:
                    result = handle_service_rollback(state["deployToken"], {"name": "api", "version": "2"})
                revert.assert_called_once()
                self.assertEqual(revert.call_args.kwargs["slug"], "api")
                self.assertEqual(revert.call_args.kwargs["version"], 2)
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["message"], "Nomad job api reverted to v2")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_dry_run_does_not_delete_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                with patch("luma.control.server.delete_dns") as dns, patch("luma.control.server.remove_from_nomad") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "api", "dryRun": True})
                dns.assert_not_called()
                stack.assert_not_called()
                self.assertTrue(stack_dir.exists())
                self.assertTrue(result["dryRun"])
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Nomad job would be removed: api", steps)
                self.assertIn("Generated files would be removed", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_by_name_uses_registered_manifest_and_forgets_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump({"name": "api", "image": "nginx:alpine", "region": "cn", "exposure": "none"})
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: api") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "api"})
                stack.assert_called_once()
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["sourceName"], "console:api")
                self.assertFalse(stack_dir.exists())
                self.assertNotIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_requires_registered_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {"services": {}, "compose": {}}
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.docker_request") as docker_request, patch("luma.control.server.remove_from_nomad") as remove:
                    with self.assertRaisesRegex(LumaError, "deployment not found: gitea"):
                        handle_service_remove(state["deployToken"], {"name": "gitea"})
                docker_request.assert_not_called()
                remove.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_by_name_removes_registered_compose_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "app-stack": {
                            "kind": "compose",
                            "name": "app-stack",
                            "slug": "app-stack",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:app-stack",
                        }
                    },
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: app-stack") as stack:
                    result = handle_service_remove(state["deployToken"], {"name": "app-stack"})
                stack.assert_called_once()
                self.assertEqual(result["deployment"], "app-stack")
                self.assertEqual(result["sourceName"], "console:app-stack")
                self.assertFalse(stack_dir.exists())
                self.assertNotIn("app-stack", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_single_service_named_volumes_from_recorded_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "volumes": ["api-data:/data", "/host/data:/host-data", "./local-data:/local-data"],
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "cn" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: api"), patch(
                    "luma.control.server._remove_docker_volume_across_nodes",
                    return_value={"name": "api-data", "status": "removed local Docker volume"},
                ) as remove_volume:
                    result = handle_service_remove(
                        state["deployToken"],
                        {"name": "api", "skipDns": True, "deleteStorage": True},
                    )
                remove_volume.assert_called_once()
                # native render creates the volume under the RAW source name, so
                # cleanup must target "api-data" (not the slug-prefixed
                # "api_api-data", which was never created — that orphaned data).
                self.assertEqual(remove_volume.call_args.args[0], "api-data")
                self.assertEqual(remove_volume.call_args.args[1], [])
                self.assertIn("removed=1", result["storageCleanup"])
                self.assertNotIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_single_service_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                state["nodes"] = {"home-node": {"region": "home", "hostname": "home-node"}}
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/me/api:1",
                        "region": "home",
                        "volumes": ["api-data:/data"],
                        "storage": {"api-data": {"storageClass": "home-nfs", "path": "api/api-data"}},
                    }
                )
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "sourceName": "console:api",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                stack_dir = root / "stacks" / "home" / "api"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: api"), patch(
                    "luma.control.server._storage_node_is_local", return_value=True
                ), patch("luma.control.server._run_host_prep_command", return_value="removed") as host_prep, patch(
                    "luma.control.server._remove_docker_volume_across_nodes",
                    return_value={"name": "api_api-data", "status": "removed local Docker volume"},
                ):
                    result = handle_service_remove(state["deployToken"], {"name": "api", "deleteStorage": True, "skipDns": True})
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/api/api-data", commands)
                self.assertIn("Managed storage cleanup finished: removed=1", result["storageCleanup"])
                self.assertIn("Docker volume cleanup finished: removed=1", result["storageCleanup"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_single_service_deploy_prepares_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                state["nodes"] = {"home-node": {"region": "home", "swarmHostname": "home-node"}}
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "registry.local/api:1",
                        "region": "home",
                        "volumes": ["api-data:/data"],
                        "storage": {"api-data": {"storageClass": "home-nfs", "path": "api/api-data"}},
                    }
                )
                with patch("luma.control.server.image_pull_requires_egress", return_value=False), patch(
                    "luma.control.server.resolve_service_image", side_effect=lambda _config, service, **_kwargs: (service, {"selected": service.image})
                ), patch("luma.control.server._storage_node_is_local", return_value=True), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep, patch("luma.control.server.deploy_to_nomad", return_value="Orchestrator deploy skipped"):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "console:api", "skipDns": True, "skipOrchestrator": True})
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/api/api-data", commands)
                self.assertIn("storagePreparation", result)
                self.assertIn("api", load_state()["deployments"]["services"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_can_delete_compose_managed_storage_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-node",
                        "path": "/srv/luma",
                    }
                }
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {
                            "nextcloud-data": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-data"},
                            "nextcloud-db": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-db"},
                        },
                    }
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "nextcloud": {"image": "nextcloud:apache", "volumes": ["nextcloud-data:/var/www/html"]},
                            "postgres": {"image": "postgres:16", "volumes": ["nextcloud-db:/var/lib/postgresql/data"]},
                        },
                        "volumes": {"nextcloud-data": {}, "nextcloud-db": {}},
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "nextcloud": {
                            "kind": "compose",
                            "name": "nextcloud",
                            "slug": "nextcloud",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:nextcloud",
                        }
                    },
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "nextcloud"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text("services: {}\n", encoding="utf-8")
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: nextcloud"), patch(
                    "luma.control.server._storage_node_is_local", return_value=True
                ), patch("luma.control.server._run_host_prep_command", return_value="removed") as host_prep:
                    result = handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True})
                self.assertEqual(host_prep.call_count, 2)
                commands = "\n".join(call.args[0] for call in host_prep.call_args_list)
                self.assertIn("/srv/luma/nextcloud/nextcloud-data", commands)
                self.assertIn("/srv/luma/nextcloud/nextcloud-db", commands)
                self.assertIn("removed=2", result["storageCleanup"])
                self.assertNotIn("nextcloud", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_storage_cleanup_dry_run_does_not_delete_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {"provider": "nfs", "mode": "managed", "node": "home-node", "path": "/srv/luma"}
                }
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {"nextcloud-db": {"storageClass": "home-nfs", "path": "nextcloud/nextcloud-db"}},
                    }
                )
                compose = yaml.safe_dump(
                    {
                        "services": {"postgres": {"image": "postgres:16", "volumes": ["nextcloud-db:/var/lib/postgresql/data"]}},
                        "volumes": {"nextcloud-db": {}},
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "nextcloud": {
                            "kind": "compose",
                            "name": "nextcloud",
                            "slug": "nextcloud",
                            "manifest": sidecar,
                            "composeContent": compose,
                            "sourceName": "console:nextcloud",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad") as remove, patch("luma.control.server._run_host_prep_command") as host_prep:
                    result = handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True, "dryRun": True})
                remove.assert_not_called()
                host_prep.assert_not_called()
                self.assertIn("/srv/luma/nextcloud/nextcloud-db", result["storageCleanup"])
                self.assertIn("nextcloud", load_state()["deployments"]["compose"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_remove_rejects_delete_storage_with_skip_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                with self.assertRaisesRegex(LumaError, "delete-storage"):
                    handle_service_remove(state["deployToken"], {"name": "nextcloud", "deleteStorage": True, "skipOrchestrator": True})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_control_status_reports_dns_and_nomad_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_token = _set_env("CLOUDFLARE_API_TOKEN", "")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "CLOUDFLARE_API_TOKEN", "value": "cf-token"})
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=False)
                state["nodes"] = {
                    "manager": {"region": "cn", "status": "manager", "labels": {"region": "cn", "role.nomad-manager": "true"}},
                    "home": {"displayName": "mini-gaojiu", "region": "home", "status": "labeled"},
                }
                state["storageClasses"] = {
                    "cn-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "manager",
                        "path": "/srv/luma",
                        "regions": ["cn"],
                    }
                }
                from luma.control.state import save_state

                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {
                                "dns": {"type": "cloudflare", "zone": "example.com", "zoneId": "zone-id"},
                            },
                            "nodes": {
                                "edge": {
                                    "host": "edge",
                                    "publicIp": "203.0.113.10",
                                    "region": "cn",
                                    "roles": ["edge"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                nomad_nodes = [
                    {"name": "home", "hostname": "home-host", "region": "home", "status": "ready"},
                    {"name": "manager", "hostname": "manager", "region": "cn", "status": "ready", "leader": True},
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "100.64.0.1:4647", "nodes": nomad_nodes},
                ), patch("luma.control.server.nomad_services_summary", return_value=[]):
                    result = handle_control_status(state["deployToken"])
                self.assertEqual(result["dns"]["provider"], "cloudflare")
                self.assertTrue(result["dns"]["tokenConfigured"])
                self.assertTrue(result["dns"]["zoneIdConfigured"])
                self.assertEqual(result["dns"]["target"], "203.0.113.10")
                self.assertEqual(result["dns"]["missing"], [])
                self.assertTrue(result["nomad"]["available"])
                self.assertEqual(result["nomad"]["nodes"][1]["leader"], True)
                self.assertEqual(result["nodes"]["registered"], 2)
                self.assertEqual(result["nodes"]["items"][0]["name"], "home")
                self.assertEqual(result["nodes"]["items"][0]["displayName"], "mini-gaojiu")
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "cn-nfs")
                self.assertEqual(result["storage"]["storageClasses"][0]["path"], "/srv/luma")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("CLOUDFLARE_API_TOKEN", old_token)

    def test_dashboard_nomad_readiness_uses_nomad_summary_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"bot": {"region": "global", "status": "registered"}}
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}, "providers": {"dns": {"type": "cloudflare"}}}),
                    encoding="utf-8",
                )
                nomad_summary = {
                    "available": False,
                    "error": "Nomad API unavailable",
                    "leader": "",
                    "nodes": [],
                }
                with patch("luma.control.server.nomad_status_summary", return_value=nomad_summary), patch(
                    "luma.control.server.nomad_services_summary", return_value=[]
                ):
                    result = handle_dashboard(state["deployToken"])

                self.assertFalse(result["readiness"]["nomad"]["available"])
                self.assertEqual(result["readiness"]["nomad"]["error"], "Nomad API unavailable")
                self.assertEqual(result["nodes"][0]["name"], "bot")
                self.assertEqual(result["nodes"][0]["state"], "missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_single_service_uses_task_desired_when_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "codex-gitea",
                        "jobId": "codex-gitea",
                        "status": "running",
                        "running": 0,
                        "region": "home",
                        "compose": False,
                        "tasks": [
                            {
                                "name": "codex-gitea",
                                "stack": "codex-gitea",
                                "fullName": "codex-gitea",
                                "status": "pending",
                                "region": "home",
                                "running": 0,
                                "desired": 1,
                                "pending": 1,
                                "nodes": ["lab"],
                            },
                            {
                                "name": "codex-gitea-sidecar",
                                "stack": "codex-gitea",
                                "fullName": "codex-gitea-sidecar",
                                "status": "running",
                                "region": "home",
                                "running": 1,
                                "desired": 1,
                                "pending": 0,
                                "nodes": ["lab"],
                            }
                        ],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                service = result["services"][0]
                self.assertEqual(service["fullName"], "codex-gitea")
                self.assertEqual(service["running"], 0)
                self.assertEqual(service["desired"], 1)
                self.assertEqual(service["pending"], 1)
                self.assertEqual(service["nodes"], ["lab"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_keeps_configured_node_visible_without_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": yaml.safe_dump(
                                {
                                    "name": "api",
                                    "image": "example/api:1",
                                    "region": "home",
                                    "node": "blg",
                                    "exposure": "tailscale-relay",
                                    "domain": "api.example.com",
                                    "port": 8080,
                                }
                            ),
                            "status": "failed_partial",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "api",
                        "jobId": "api",
                        "status": "pending",
                        "running": 0,
                        "desired": 1,
                        "pending": 1,
                        "region": "home",
                        "compose": False,
                        "tasks": [],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                service = result["services"][0]
                self.assertEqual(service["node"], "blg")
                self.assertEqual(service["nodes"], ["blg"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_flags_failed_route_probe_even_when_nomad_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "home",
                        "exposure": "tailscale-relay",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {
                        "api": {
                            "kind": "service",
                            "name": "api",
                            "slug": "api",
                            "manifest": manifest,
                            "status": "failed_partial",
                            "lastError": "Probe public route failed: Public route unhealthy: https://api.example.com/ -> HTTP 504",
                        }
                    },
                    "compose": {},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad"}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "api",
                        "jobId": "api",
                        "status": "running",
                        "running": 1,
                        "region": "home",
                        "compose": False,
                        "tasks": [
                            {
                                "name": "api",
                                "stack": "api",
                                "fullName": "api",
                                "status": "running",
                                "region": "home",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["blg"],
                            }
                        ],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                service = result["services"][0]
                self.assertEqual(service["deploymentStatus"], "failed_partial")
                self.assertTrue(any("Public route unhealthy" in item for item in service["diagnostics"]))
                issue_messages = [issue["message"] for issue in result["issues"]]
                self.assertTrue(any("Public route unhealthy" in message for message in issue_messages))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_expands_compose_job_services_with_manifest_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                sidecar = yaml.safe_dump(
                    {
                        "name": "granary",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "mysql": {
                                "node": "lab",
                                "exposure": "tcp-relay",
                                "domain": "granary-db.itool.tech",
                                "port": 3306,
                                "publishPort": 3306,
                            },
                            "granary": {
                                "node": "lab",
                                "exposure": "tailscale-relay",
                                "domain": "api-granary.itool.tech",
                                "port": 8888,
                                "publishPort": 8888,
                            },
                            "granary-frontend": {
                                "node": "lab",
                                "exposure": "tailscale-relay",
                                "domain": "granary.itool.tech",
                                "port": 80,
                                "publishPort": 8081,
                            },
                        },
                    }
                )
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "granary": {
                            "kind": "compose",
                            "name": "granary",
                            "slug": "granary",
                            "manifest": sidecar,
                            "composeContent": "services: {}\n",
                            "sourceName": "luma.compose.yml",
                        }
                    },
                }
                save_state(state)
                routes = root / "routes"
                routes.mkdir()
                (routes / "granary-mysql.yml").write_text(
                    yaml.safe_dump(
                        {
                            "tcp": {
                                "routers": {"granary-mysql": {"rule": "HostSNI(`*`)", "service": "granary-mysql"}},
                                "services": {
                                    "granary-mysql": {"loadBalancer": {"servers": [{"address": "100.64.0.10:3306"}]}}
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (routes / "granary-granary-frontend.yml").write_text(
                    yaml.safe_dump(
                        {
                            "http": {
                                "routers": {"granary-frontend": {"rule": "Host(`granary.itool.tech`)", "service": "granary-frontend"}},
                                "services": {
                                    "granary-frontend": {"loadBalancer": {"servers": [{"url": "http://100.64.0.10:8081"}]}}
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "routesRoot": str(routes)}}),
                    encoding="utf-8",
                )
                nomad_services = [
                    {
                        "name": "granary",
                        "jobId": "granary",
                        "status": "running",
                        "running": 1,
                        "region": "home",
                        "compose": True,
                        "tasks": [
                            {
                                "name": "mysql",
                                "stack": "granary",
                                "fullName": "granary_mysql",
                                "status": "running",
                                "region": "home",
                                "targetPort": "3306",
                                "publishPort": "3306",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["lab"],
                                "tasks": [{"id": "alloc-1", "node": "lab", "state": "running"}],
                            },
                            {
                                "name": "granary",
                                "stack": "granary",
                                "fullName": "granary_granary",
                                "status": "running",
                                "region": "home",
                                "targetPort": "8888",
                                "publishPort": "8888",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["lab"],
                            },
                            {
                                "name": "granary-frontend",
                                "stack": "granary",
                                "fullName": "granary_granary-frontend",
                                "status": "running",
                                "region": "home",
                                "targetPort": "80",
                                "publishPort": "8081",
                                "running": 1,
                                "desired": 1,
                                "nodes": ["lab"],
                            },
                        ],
                    }
                ]
                with patch(
                    "luma.control.server.nomad_status_summary",
                    return_value={"available": True, "leader": "127.0.0.1:4647", "nodes": []},
                ), patch("luma.control.server.nomad_services_summary", return_value=nomad_services), patch(
                    "luma.control.server._service_stats_by_name", return_value={}
                ):
                    result = handle_dashboard(state["deployToken"])

                services = {item["fullName"]: item for item in result["services"]}
                self.assertEqual(services["granary_mysql"]["exposure"], "tcp-relay")
                self.assertEqual(services["granary_mysql"]["domain"], "granary-db.itool.tech")
                self.assertEqual(services["granary_mysql"]["targetPort"], "3306")
                self.assertEqual(services["granary_mysql"]["routeId"], "granary-mysql")
                self.assertEqual(services["granary_granary"]["domain"], "api-granary.itool.tech")
                self.assertEqual(services["granary_granary-frontend"]["domain"], "granary.itool.tech")
                self.assertEqual(services["granary_granary-frontend"]["exposure"], "tailscale-relay")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_logs_resolve_compose_task_full_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["deployments"] = {
                    "services": {},
                    "compose": {
                        "granary": {
                            "kind": "compose",
                            "name": "granary",
                            "slug": "granary",
                            "manifest": yaml.safe_dump(
                                {
                                    "name": "granary",
                                    "compose": "docker-compose.yml",
                                    "services": {"mysql": {"exposure": "tcp-relay", "domain": "granary-db.itool.tech", "port": 3306}},
                                }
                            ),
                            "composeContent": "services: {}\n",
                            "sourceName": "luma.compose.yml",
                        }
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")
                log_text = "mysql ready\n"
                calls: list[str] = []

                def request(_self, method, path, body=None):
                    calls.append(path)
                    if path == "/v1/job/granary/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "running",
                                "CreateTime": 10,
                                "TaskGroup": "granary",
                                "TaskStates": {"mysql": {"State": "running"}, "granary": {"State": "running"}},
                            }
                        ]
                    raise AssertionError(path)

                def request_text(_self, method, path, body=None):
                    calls.append(path)
                    self.assertIn("task=mysql", path)
                    if "type=stdout" in path:
                        return json.dumps({"Data": base64.b64encode(log_text.encode()).decode(), "Offset": 10})
                    if "type=stderr" in path:
                        return json.dumps({"Data": "", "Offset": 0})
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request), patch("luma.control.server.NomadApi.request_text", request_text):
                    result = handle_dashboard_logs(state["deployToken"], "granary_mysql", tail=20)

                self.assertIn("/v1/job/granary/allocations", calls)
                self.assertEqual(result["service"], "granary_mysql")
                self.assertEqual(result["logs"], ["mysql ready"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_logs_tails_nomad_alloc_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")
                log_text = "2026-06-05T10:00:00Z api booted\n2026-06-05T10:00:01Z api ready\n"
                calls: list[str] = []

                def request(_self, method, path, body=None):
                    calls.append(path)
                    if path == "/v1/job/codex-gitea/allocations":
                        return [{
                            "ID": "alloc-1",
                            "DesiredStatus": "run",
                            "ClientStatus": "running",
                            "CreateTime": 10,
                            "TaskGroup": "codex-gitea",
                            "TaskStates": {"codex-gitea": {"State": "running"}},
                        }]
                    raise AssertionError(path)

                def request_text(_self, method, path, body=None):
                    calls.append(path)
                    if path.startswith("/v1/client/fs/logs/alloc-1?") and "type=stdout" in path:
                        payload = {"Data": base64.b64encode(log_text.encode()).decode(), "Offset": 10}
                        return json.dumps(payload) + json.dumps({"Data": "ignored"})
                    if path.startswith("/v1/client/fs/logs/alloc-1?") and "type=stderr" in path:
                        return json.dumps({"Data": "", "Offset": 0})
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request), patch("luma.control.server.NomadApi.request_text", request_text):
                    result = handle_dashboard_logs(state["deployToken"], "codex-gitea", tail=20)

                self.assertIn("/v1/job/codex-gitea/allocations", calls)
                self.assertTrue(any("/v1/client/fs/logs/alloc-1?" in call for call in calls))
                self.assertEqual(result["service"], "codex-gitea")
                self.assertEqual(result["logs"], [line for line in log_text.splitlines()])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_runtime_events_include_pending_allocation_pull_progress(self):
        from luma.control.server import handle_dashboard_runtime_events

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "tecent": {
                        "nodeId": "nomad-node-1",
                        "hostname": "VM-0-10-ubuntu",
                        "agent": {
                            "status": "ready",
                            "diagnostics": {
                                "recentImagePullErrors": [
                                    'Task event: alloc_id=alloc-1 task=app type=Driver msg="Docker image pull progress: Pulled 9/14 layers" failed=false'
                                ]
                            },
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/luxe-monitor":
                        return {
                            "ID": "luxe-monitor",
                            "Meta": {"luma.compose": "true"},
                            "TaskGroups": [
                                {
                                    "Name": "luxe-monitor",
                                    "Tasks": [
                                        {"Name": "app", "Config": {"image": "100.66.177.70:5000/liutianjie/luxe-monitor:a494e7f"}},
                                    ],
                                }
                            ],
                        }
                    if path == "/v1/job/luxe-monitor/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "pending",
                                "TaskGroup": "luxe-monitor",
                                "CreateTime": 10,
                                "NodeID": "nomad-node-1",
                                "NodeName": "VM-0-10-ubuntu",
                                "TaskStates": {
                                    "app": {
                                        "State": "pending",
                                        "Events": [
                                            {"Type": "Driver", "DisplayMessage": "Downloading image", "Message": "Docker image pull progress: Pulled 9/14 layers", "Time": 123}
                                        ],
                                    }
                                },
                            }
                        ]
                    if path == "/v1/allocation/alloc-1":
                        return request(_self, method, "/v1/job/luxe-monitor/allocations", body)[0]
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request):
                    result = handle_dashboard_runtime_events(state["deployToken"], "luxe-monitor_app")

                self.assertEqual(result["service"], "luxe-monitor_app")
                self.assertEqual(result["job"], "luxe-monitor")
                self.assertEqual(result["task"], "app")
                self.assertEqual(result["allocId"], "alloc-1")
                self.assertEqual(result["node"], "tecent")
                self.assertEqual(result["status"], "pending")
                self.assertEqual(result["image"], "100.66.177.70:5000/liutianjie/luxe-monitor:a494e7f")
                self.assertTrue(any("Downloading image" in event["message"] for event in result["events"]))
                self.assertTrue(any("Pulled 9/14" in event["message"] for event in result["events"]))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_pull_diagnostics_runs_on_latest_allocation_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "nodeId": "nomad-node-1",
                        "agent": {"status": "ready", "capabilities": ["docker-image"]},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/api":
                        return {
                            "ID": "api",
                            "TaskGroups": [
                                {
                                    "Name": "api",
                                    "Tasks": [
                                        {"Name": "api", "Config": {"image": "ghcr.io/acme/api@sha256:abc123"}},
                                    ],
                                }
                            ],
                        }
                    if path == "/v1/job/api/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "pending",
                                "CreateTime": 10,
                                "NodeID": "nomad-node-1",
                                "TaskStates": {"api": {"State": "pending"}},
                            }
                        ]
                    raise AssertionError(path)

                def queue_task(_state, node_name, action, payload, **kwargs):
                    self.assertEqual(node_name, "blg")
                    self.assertEqual(action, "diagnose-docker-pull")
                    self.assertEqual(payload["image"], "ghcr.io/acme/api@sha256:abc123")
                    self.assertEqual(kwargs["required_capability"], "docker-image")
                    return "task-1"

                with patch("luma.control.server.NomadApi.request", request), patch(
                    "luma.control.server._queue_node_agent_task",
                    side_effect=queue_task,
                ), patch(
                    "luma.control.server._wait_node_agent_task",
                    return_value={"taskId": "task-1", "ok": True, "lines": ["Downloading"], "output": "Downloading\n"},
                ):
                    result = handle_service_pull_diagnostics(state["deployToken"], "api", timeout=30)

                self.assertEqual(result["service"], "api")
                self.assertEqual(result["node"], "blg")
                self.assertEqual(result["image"], "ghcr.io/acme/api@sha256:abc123")
                self.assertEqual(result["lines"], ["Downloading"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_service_pull_diagnostics_falls_back_to_first_task_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "nodeId": "nomad-node-1",
                        "agent": {"status": "ready", "capabilities": ["docker-image"]},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}), encoding="utf-8")

                def request(_self, method, path, body=None):
                    if path == "/v1/job/api":
                        return {
                            "ID": "api",
                            "TaskGroups": [
                                {
                                    "Name": "api",
                                    "Tasks": [
                                        {"Name": "web", "Config": {"image": "ghcr.io/acme/web@sha256:abc123"}},
                                    ],
                                }
                            ],
                        }
                    if path == "/v1/job/api/allocations":
                        return [
                            {
                                "ID": "alloc-1",
                                "DesiredStatus": "run",
                                "ClientStatus": "pending",
                                "CreateTime": 10,
                                "NodeID": "nomad-node-1",
                                "TaskStates": {"web": {"State": "pending"}},
                            }
                        ]
                    raise AssertionError(path)

                with patch("luma.control.server.NomadApi.request", request), patch(
                    "luma.control.server._queue_node_agent_task",
                    return_value="task-1",
                ) as queue_task, patch(
                    "luma.control.server._wait_node_agent_task",
                    return_value={"taskId": "task-1", "ok": True, "lines": [], "output": ""},
                ):
                    result = handle_service_pull_diagnostics(state["deployToken"], "api", timeout=30)

                self.assertEqual(result["task"], "web")
                self.assertEqual(result["image"], "ghcr.io/acme/web@sha256:abc123")
                self.assertEqual(queue_task.call_args.args[3]["image"], "ghcr.io/acme/web@sha256:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_node_agent_progress_appends_lines_to_running_task(self):
        from luma.control.server import handle_node_agent_progress

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"blg": {"nodeId": "node-1", "agent": {"status": "ready"}}}
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "blg", "nodeId": "node-1"})
                current = load_state()
                current.setdefault("agentTasks", {})["task-pull"] = {
                    "nodeName": "blg",
                    "action": "diagnose-docker-pull",
                    "status": "running",
                    "payload": {"image": "ghcr.io/acme/api:latest"},
                }
                save_state(current)

                result = handle_node_agent_progress(
                    issued["agentToken"],
                    {"nodeName": "blg", "nodeId": "node-1", "taskId": "task-pull", "events": [{"type": "output", "line": "Downloading"}]},
                )

                self.assertEqual(result["taskId"], "task-pull")
                task = load_state()["agentTasks"]["task-pull"]
                self.assertEqual(task["progress"][0]["line"], "Downloading")
                self.assertNotIn("registryAuth", json.dumps(task))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_dashboard_handler_serves_static_assets_and_rejects_missing_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/dashboard/", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("Luma · 控制台".encode("utf-8"), response.read())
            with urllib.request.urlopen(base + "/dashboard/app.js", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"/v1/dashboard", response.read())
            with urllib.request.urlopen(base + "/dashboard/asset-luma-logo-mark.png", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertGreater(len(response.read()), 0)
            # A deep client-side route falls back to index.html (SPA routing) with 200.
            with urllib.request.urlopen(base + "/dashboard/apps/granary", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "text/html")
                self.assertIn("Luma · 控制台".encode("utf-8"), response.read())
            # An unknown asset path still 404s as JSON, not HTML.
            with self.assertRaises(urllib.error.HTTPError) as missing_asset:
                urllib.request.urlopen(base + "/dashboard/does-not-exist.js", timeout=5)
            self.assertEqual(missing_asset.exception.code, 404)
            missing_asset_body = missing_asset.exception.read()
            self.assertNotIn("Luma · 控制台".encode("utf-8"), missing_asset_body)
            self.assertEqual(json.loads(missing_asset_body.decode("utf-8"))["error"], "not found")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base + "/v1/dashboard", timeout=5)
            self.assertEqual(raised.exception.code, 401)
            error_payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(error_payload["error"], "missing bearer token")
            self.assertEqual(error_payload["errorInfo"]["code"], "luma_error")
            self.assertEqual(error_payload["errorInfo"]["message"], "missing bearer token")
            self.assertTrue(error_payload["requestId"].startswith("req-"))
            missing_request = urllib.request.Request(
                base + "/v1/missing",
                data=b"{}",
                headers={"Content-Type": "application/json", "Authorization": "Bearer token"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(missing_request, timeout=5)
            self.assertEqual(missing.exception.code, 404)
            missing_payload = json.loads(missing.exception.read().decode("utf-8"))
            self.assertEqual(missing_payload["errorInfo"]["code"], "not_found")
            self.assertEqual(missing_payload["error"], "not found")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_asgi_app_serves_dashboard_health_and_rejects_missing_token(self):
        from starlette.testclient import TestClient
        from luma.control.server import create_app

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with TestClient(create_app()) as client:
                    response = client.get("/dashboard/")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("Luma · 控制台", response.text)
                    # A deep client-side route falls back to index.html (SPA routing).
                    deep = client.get("/dashboard/apps/granary/logs")
                    self.assertEqual(deep.status_code, 200)
                    self.assertIn("Luma · 控制台", deep.text)
                    self.assertEqual(deep.headers["content-type"], "text/html; charset=utf-8")
                    # An unknown asset path still 404s as JSON, not HTML.
                    missing_asset = client.get("/dashboard/does-not-exist.js")
                    self.assertEqual(missing_asset.status_code, 404)
                    self.assertNotIn("Luma · 控制台", missing_asset.text)
                    self.assertEqual(missing_asset.json()["error"], "not found")
                    health = client.get("/v1/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertIn("terminal", health.json()["capabilities"])
                    login = client.post("/v1/auth/login/verify", json={}, headers={"Authorization": f"Bearer {state['deployToken']}"})
                    self.assertEqual(login.status_code, 200)
                    self.assertEqual(login.json()["clusterId"], "luma-test")
                    rejected = client.get("/v1/dashboard")
                    self.assertEqual(rejected.status_code, 401)
                    self.assertEqual(rejected.json()["error"], "missing bearer token")
                    # The dashboard fallback never intercepts unknown /v1 GETs.
                    rejected_v1 = client.get("/v1/nope")
                    self.assertEqual(rejected_v1.status_code, 401)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_terminal_websocket_relays_between_browser_and_agent(self):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            original_broker = control_server.TERMINAL_BROKER
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                control_server.TERMINAL_BROKER = control_server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
                with TestClient(control_server.create_app()) as client:
                    with client.websocket_connect(
                        "/v1/terminal/agent?node=worker-storage&nodeId=worker-node-id"
                    ) as agent:
                        agent.send_json({"type": "auth", "token": agent_token})
                        self.assertEqual(agent.receive_json()["type"], "ready")
                        with client.websocket_connect(
                            "/v1/terminal/browser?node=worker-storage"
                        ) as browser:
                            browser.send_json({"type": "auth", "token": state["deployToken"]})
                            opened = browser.receive_json()
                            self.assertEqual(opened["type"], "open")
                            session_id = opened["sessionId"]
                            agent_open = agent.receive_json()
                            self.assertEqual(agent_open["type"], "open")
                            self.assertEqual(agent_open["sessionId"], session_id)
                            browser.send_json({"type": "input", "data": "pwd\n"})
                            agent_input = agent.receive_json()
                            self.assertEqual(agent_input["type"], "input")
                            self.assertEqual(agent_input["sessionId"], session_id)
                            self.assertEqual(agent_input["data"], "pwd\n")
                            agent.send_json({"type": "output", "sessionId": session_id, "data": "/root\r\n"})
                            self.assertEqual(browser.receive_json()["data"], "/root\r\n")
                            agent.send_json({"type": "exit", "sessionId": session_id, "exitCode": 0})
                            exit_event = browser.receive_json()
                            self.assertEqual(exit_event["type"], "exit")
                            self.assertEqual(exit_event["exitCode"], 0)
                            with self.assertRaises(WebSocketDisconnect):
                                browser.receive_json()
            finally:
                control_server.TERMINAL_BROKER = original_broker
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_terminal_websocket_canonicalizes_agent_alias(self):
        from starlette.testclient import TestClient
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            original_broker = control_server.TERMINAL_BROKER
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "gaojiu": {
                        "displayName": "gaojiu",
                        "hostname": "home-mac-mini",
                        "aliases": ["home-mac-mini"],
                        "region": "home",
                        "nodeId": "gaojiu-node-id",
                        "labels": {"luma.node.name": "gaojiu", "luma.node.id": "gaojiu-node-id", "region": "home"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "gaojiu", "nodeId": "gaojiu-node-id"})
                agent_token = issued["agentToken"]
                control_server.TERMINAL_BROKER = control_server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
                with TestClient(control_server.create_app()) as client:
                    with client.websocket_connect(
                        "/v1/terminal/agent?node=home-mac-mini&nodeId=gaojiu-node-id"
                    ) as agent:
                        agent.send_json({"type": "auth", "token": agent_token})
                        ready = agent.receive_json()
                        self.assertEqual(ready["type"], "ready")
                        self.assertEqual(ready["node"], "gaojiu")
                        self.assertEqual(control_server.TERMINAL_BROKER.connected_nodes(), {"gaojiu"})
                        with client.websocket_connect("/v1/terminal/browser?node=gaojiu") as browser:
                            browser.send_json({"type": "auth", "token": state["deployToken"]})
                            opened = browser.receive_json()
                            self.assertEqual(opened["type"], "open")
                            self.assertEqual(opened["node"], "gaojiu")
                            agent_open = agent.receive_json()
                            self.assertEqual(agent_open["type"], "open")
                            self.assertEqual(agent_open["node"], "gaojiu")
            finally:
                control_server.TERMINAL_BROKER = original_broker
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_terminal_broker_closes_browser_and_only_cleans_old_agent_sessions(self):
        import asyncio
        from luma.control.server import TerminalBroker, _TerminalAgentConnection, _TerminalSession

        class FakeBrowser:
            def __init__(self):
                self.sent = []
                self.closed = False

            async def send_json(self, payload):
                self.sent.append(payload)

            async def close(self, code=1000):
                self.closed = code

        class FakeAgentSocket:
            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        async def scenario():
            broker = TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
            old_agent = _TerminalAgentConnection("worker-storage", FakeAgentSocket())
            new_agent = _TerminalAgentConnection("worker-storage", FakeAgentSocket())
            old_browser = FakeBrowser()
            new_browser = FakeBrowser()
            broker._sessions["old"] = _TerminalSession("old", "worker-storage", old_browser, old_agent)
            broker._sessions["new"] = _TerminalSession("new", "worker-storage", new_browser, new_agent)

            self.assertEqual(await broker._session_ids_for_agent("worker-storage", old_agent), ["old"])
            await broker.close_session("old", notify_agent=True, browser_message="terminal agent disconnected")

            self.assertTrue(old_browser.closed)
            self.assertEqual(old_browser.sent[0]["message"], "terminal agent disconnected")
            self.assertEqual(old_agent.websocket.sent[0], {"type": "close", "sessionId": "old"})
            self.assertIn("new", broker._sessions)
            self.assertFalse(new_browser.closed)

        asyncio.run(scenario())

    def test_terminal_broker_rejects_pending_auth_overflow(self):
        import asyncio
        from luma.control.server import TerminalBroker

        class FakeWebSocket:
            def __init__(self):
                self.closed = None

            async def close(self, code=1000):
                self.closed = code

        async def scenario():
            broker = TerminalBroker(per_node_limit=1, idle_timeout_seconds=60)
            broker._pending_auth = asyncio.Semaphore(0)
            websocket = FakeWebSocket()

            self.assertFalse(await broker._acquire_pending_auth(websocket))
            self.assertEqual(websocket.closed, 1013)

        asyncio.run(scenario())

    def test_terminal_websocket_rejects_wrong_agent_token(self):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        from luma.control.server import create_app

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                    }
                }
                save_state(state)
                with TestClient(create_app()) as client, self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/v1/terminal/agent?node=worker-storage&nodeId=worker-node-id") as agent:
                        agent.send_json({"type": "auth", "token": "wrong"})
                        agent.receive_json()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_asgi_log_stream_defers_docker_socket_to_background_reader(self):
        import asyncio
        from luma.control.server import _asgi_stream_service_logs

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                with patch("luma.control.server.DockerSocketConnection") as docker_socket:
                    response = asyncio.run(_asgi_stream_service_logs(state["deployToken"], "api_api", "", 20))
                self.assertEqual(response.media_type, "application/x-ndjson")
                docker_socket.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_docker_socket_connect_closes_socket_when_connect_fails(self):
        from luma.control.server import DockerSocketConnection

        class FailingSocket:
            def __init__(self):
                self.closed = False

            def connect(self, _path):
                raise OSError("socket unavailable")

            def close(self):
                self.closed = True

        failing = FailingSocket()
        with patch("luma.control.server.socket.socket", return_value=failing):
            with self.assertRaises(OSError):
                DockerSocketConnection("/missing/docker.sock").connect()
        self.assertTrue(failing.closed)

    def test_deployment_renders_referenced_secrets_into_nomad_job_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://secret"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                response = MagicMock()
                response.__enter__.return_value.status = 200
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns", return_value="DNS updated"
                ), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ) as deploy, patch("luma.control.server.urllib.request.urlopen", return_value=response):
                    result = handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
                self.assertEqual(result["service"], "api")
                self.assertEqual(result["probe"], "Public route reachable: https://api.example.com/ -> HTTP 200")
                deploy.assert_called_once()
                job_text = deploy.call_args.args[1]
                self.assertIn('"DATABASE_URL": "postgres://secret"', job_text)
                self.assertIn('"DATABASE_URL": "postgres://secret"', (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_renders_storage_without_writing_or_deploying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "home-nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["home"]}
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "uptime-kuma": {
                                "image": "louislam/uptime-kuma:1",
                                "volumes": ["kuma-data:/app/data"],
                            }
                        },
                        "volumes": {"kuma-data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "uptime-kuma",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "volumes": {"kuma-data": {"storageClass": "home-nfs", "path": "uptime-kuma/kuma-data"}},
                        "services": {
                            "uptime-kuma": {
                                "exposure": "tailscale-relay",
                                "domain": "kuma.example.com",
                                "port": 3001,
                                "relay": {"host": "100.64.0.2"},
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad") as upsert:
                    result = handle_compose_deployment_preview(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(result["deployment"], "uptime-kuma")
                self.assertEqual(result["artifacts"][0]["kind"], "job")
                self.assertIn('"source": "kuma-data"', result["artifacts"][0]["content"])
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                self.assertFalse((root / "stacks").exists())
                upsert.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_rejects_missing_storage_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["data:/data"]}}, "volumes": {"data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"data": {"storageClass": "missing-nfs", "path": "app/data"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "unknown storage class"):
                    handle_compose_deployment_preview(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_preview_keeps_secret_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "environment": {"APP_PASSWORD": "${APP_PASSWORD}"}}}})
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "docker-compose.yml", "region": "cn"})

                result = handle_compose_deployment_preview(
                    state["deployToken"],
                    {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                )

                self.assertEqual(result["deployment"], "app-stack")
                self.assertIn('"APP_PASSWORD": "${APP_PASSWORD}"', result["artifacts"][0]["content"])
                self.assertFalse((root / "stacks").exists())
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_blocks_storage_backend_switch_without_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "nfs", "path": "pg-data"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_allows_storage_backend_switch_after_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                stack_dir = root / "stacks" / "compose" / "app-stack"
                stack_dir.mkdir(parents=True)
                (stack_dir / f"{stack_dir.name}.nomad.json").write_text(
                    yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}, "volumes": {"pg-data": {}}}),
                    encoding="utf-8",
                )
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "nfs", "path": "pg-data", "adopted": True}},
                    }
                )
                result = handle_compose_deployment(
                    state["deployToken"],
                    {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                )
                self.assertEqual(result["deployment"], "app-stack")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_rejected_storage_switch_does_not_poison_baseline_on_retry(self):
        # Regression: a rejected storage switch must NOT overwrite the stored
        # backend baseline. Otherwise retrying the same (rejected) switch sees
        # new-vs-new, slips past the guard, and orphans the old volume's data.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma", "regions": ["cn"]}
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})

                def deploy(path: str):
                    sidecar = yaml.safe_dump(
                        {
                            "name": "app-stack",
                            "compose": "docker-compose.yml",
                            "region": "cn",
                            "volumes": {"pg-data": {"storageClass": "nfs", "path": path}},
                        }
                    )
                    return handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )

                # 1) first deploy establishes backend baseline at path=pg-data
                result = deploy("pg-data")
                self.assertEqual(result["deployment"], "app-stack")
                record = load_state()["deployments"]["compose"]["app-stack"]
                self.assertEqual(record["storageBackends"]["pg-data"]["path"], "pg-data")

                # 2) switch to a different path is rejected
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    deploy("pg-data-v2")
                # the rejected switch must NOT have poisoned the baseline
                record = load_state()["deployments"]["compose"]["app-stack"]
                self.assertEqual(record["status"], "failed_partial")
                self.assertEqual(record["storageBackends"]["pg-data"]["path"], "pg-data")

                # 3) retrying the same switch is STILL rejected (the bug: it passed)
                with self.assertRaisesRegex(LumaError, "storage backend changed"):
                    deploy("pg-data-v2")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_rejects_sidecar_storage_classes_in_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "storageClasses": {"nfs": {"provider": "nfs", "mode": "external", "endpoint": "nas:/srv/luma"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "managed by Luma Control"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_rejects_unsafe_compose_upload_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump({"name": "app-stack", "compose": "../docker-compose.yml", "region": "cn"})
                with self.assertRaisesRegex(LumaError, "relative path without"):
                    handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_class_is_managed_in_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {
                        "name": "home-nas",
                        "region": "home",
                        "status": "labeled",
                        "swarmNodeId": "home-node-id",
                        "labels": {"luma.node.name": "home-nas", "luma.node.id": "home-node-id", "region": "home"},
                    },
                }
                save_state(state)
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {
                            "name": "home-nfs",
                            "provider": "nfs",
                            "node": "home-nas",
                            "path": "/srv/luma",
                            "regions": ["home", "cn"],
                        },
                    )
                self.assertTrue(result["saved"])
                listed = handle_storage_list(state["deployToken"])
                self.assertEqual(listed["storageClasses"][0]["name"], "home-nfs")
                self.assertEqual(listed["storageClasses"][0]["path"], "/srv/luma")
                self.assertNotIn("workloads", listed["storageClasses"][0])
                self.assertNotIn("exportRoot", listed["storageClasses"][0])
                persisted = load_state()
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["provider"], "nfs")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["mode"], "managed")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["path"], "/srv/luma")
                self.assertEqual(persisted["storageClasses"]["home-nfs"]["mountOptions"], DEFAULT_NFS_MOUNT_OPTIONS)
                self.assertNotIn("workloads", persisted["storageClasses"]["home-nfs"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_validates_new_managed_and_external_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {"name": "home-nas", "region": "home", "status": "labeled"},
                }
                save_state(state)
                with patch("luma.control.server.docker_request", return_value=[]):
                    with self.assertRaisesRegex(LumaError, "unknown Luma node"):
                        handle_storage_set(state["deployToken"], {"name": "bad-nfs", "provider": "nfs", "node": "missing", "path": "/srv/luma"})
                with self.assertRaisesRegex(LumaError, "cannot set endpoint"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "bad-nfs", "provider": "nfs", "node": "home-nas", "path": "/srv/luma", "endpoint": "home-nas:/srv/luma"},
                    )
                with self.assertRaisesRegex(LumaError, "requires at least one region"):
                    handle_storage_set(
                        state["deployToken"],
                        {"name": "company-nfs", "provider": "nfs", "external": True, "endpoint": "nfs.example.com:/srv/luma"},
                    )
                result = handle_storage_set(
                    state["deployToken"],
                    {"name": "company-nfs", "external": True, "endpoint": "nfs.example.com:/srv/luma", "regions": ["cn"], "workloads": ["database"]},
                )
                self.assertTrue(result["saved"])
                saved = load_state()["storageClasses"]["company-nfs"]
                self.assertEqual(saved["provider"], "nfs")
                self.assertEqual(saved["mode"], "external")
                self.assertEqual(saved["regions"], ["cn"])
                self.assertNotIn("workloads", saved)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_rejects_unregistered_manager_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {"name": "home-mac-mini", "region": "home", "status": "labeled"},
                }
                save_state(state)
                with patch("luma.control.server.docker_request", side_effect=AssertionError("Docker discovery should not run")):
                    with self.assertRaisesRegex(LumaError, "unknown Luma node"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "cn-nfs", "provider": "nfs", "node": "iZ0jl8auywzycory05d9cuZ", "path": "/srv/luma"},
                        )
                self.assertNotIn("cn-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_uses_registered_manager_without_swarm_labeling(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "status": "manager",
                        "hostname": "manager-host",
                        "nodeId": "manager-node-id",
                        "labels": {"region": "cn", "ingress": "true"},
                    }
                }
                save_state(state)
                docker_calls = []

                def fake_docker_request(method, path, body=None):
                    docker_calls.append((method, path, body))
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "ID": "manager-node-id"}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "provider": "nfs", "node": "manager-host", "path": "/srv/luma"},
                    )
                self.assertTrue(result["saved"])
                persisted = load_state()
                self.assertEqual(persisted["storageClasses"]["cn-nfs"]["path"], "/srv/luma")
                manager = persisted["nodes"]["manager-host"]
                self.assertEqual(manager["region"], "cn")
                self.assertEqual(manager["hostname"], "manager-host")
                self.assertEqual(manager["nodeId"], "manager-node-id")
                self.assertEqual(manager["status"], "manager")
                self.assertEqual(manager["labels"], {"region": "cn", "ingress": "true"})
                self.assertFalse(any(method == "POST" for method, _path, _body in docker_calls))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_keeps_registered_manager_labels_for_storage_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "status": "manager",
                        "hostname": "manager-host",
                        "nodeId": "manager-node-id",
                        "labels": {"region": "cn", "ingress": "true"},
                    }
                }
                save_state(state)
                docker_calls = []

                def fake_docker_request(method, path, body=None):
                    docker_calls.append((method, path, body))
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "ID": "manager-node-id"}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "node": "manager-host", "path": "/srv/luma"},
                    )
                self.assertTrue(result["saved"])
                manager = load_state()["nodes"]["manager-host"]
                self.assertEqual(manager["labels"], {"region": "cn", "ingress": "true"})
                self.assertFalse(any(method == "POST" for method, _path, _body in docker_calls))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_creates_managed_path_for_local_storage_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"luma.node.name": "manager-host", "luma.node.id": "manager-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                storage_path = root / "srv" / "luma"

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "node": "manager-host", "path": str(storage_path)},
                    )
                self.assertTrue(result["saved"])
                command = host_prep.call_args.args[0]
                self.assertIn("nfs-kernel-server", command)
                self.assertIn(str(storage_path), command)
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_does_not_save_managed_class_when_host_prep_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-host": {
                        "region": "cn",
                        "swarmHostname": "manager-host",
                        "swarmNodeId": "manager-node-id",
                        "labels": {"luma.node.name": "manager-host", "luma.node.id": "manager-node-id", "region": "cn"},
                    }
                }
                save_state(state)

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", side_effect=LumaError("prep failed")
                ), patch("luma.control.server.remove_from_nomad") as remove:
                    with self.assertRaisesRegex(LumaError, "failed to prepare managed NFS storage"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "bad-nfs", "node": "manager-host", "path": "/srv/luma"},
                        )
                self.assertNotIn("bad-nfs", load_state().get("storageClasses", {}))
                remove.assert_not_called()
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_reuses_existing_managed_export_for_same_node_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "nodeId": "builder-node-id",
                        "labels": {
                            "luma.node.name": "builder",
                            "luma.node.id": "builder-node-id",
                            "region": "home",
                        },
                    }
                }
                state["storageClasses"] = {
                    "builder-registry-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "builder",
                        "path": "/srv/luma",
                        "regions": ["home"],
                    }
                }
                save_state(state)

                with patch("luma.control.server._prepare_managed_nfs_host") as prepare:
                    result = handle_storage_set(
                        state["deployToken"],
                        {
                            "name": "lae-staging-runtime-nfs",
                            "node": "builder",
                            "path": "/srv/luma",
                            "regions": ["cn"],
                            "nodes": ["manager", "tecent"],
                        },
                    )

                prepare.assert_not_called()
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export reused")
                self.assertEqual(result["storageHost"]["reusedFrom"], "builder-registry-nfs")
                saved = load_state()["storageClasses"]["lae-staging-runtime-nfs"]
                self.assertEqual(saved["exportName"], "builder-registry-nfs")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_remove_retains_shared_export_then_removes_owner_file_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["storageClasses"] = {
                    "builder-registry-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "builder",
                        "path": "/srv/luma",
                    },
                    "lae-staging-runtime-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "builder",
                        "path": "/srv/luma",
                        "exportName": "builder-registry-nfs",
                    },
                }
                save_state(state)

                with patch("luma.control.server._remove_local_nfs_export") as remove_export, patch(
                    "luma.control.server.remove_from_nomad", return_value="removed"
                ):
                    first = handle_storage_remove(
                        state["deployToken"], {"name": "builder-registry-nfs"}
                    )
                    remove_export.assert_not_called()
                    self.assertEqual(
                        first["storageHost"]["export"],
                        "retained: shared by lae-staging-runtime-nfs",
                    )

                    second = handle_storage_remove(
                        state["deployToken"], {"name": "lae-staging-runtime-nfs"}
                    )
                    self.assertEqual(remove_export.call_count, 1)
                    removed_spec = remove_export.call_args.args[0]
                    self.assertEqual(removed_spec.name, "builder-registry-nfs")
                    self.assertEqual(second["storageHost"]["export"], remove_export.return_value)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_rejects_remote_managed_storage_when_agent_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command"
                ) as host_prep:
                    with self.assertRaisesRegex(LumaError, "node agent is not ready"):
                        handle_storage_set(
                            state["deployToken"],
                            {"name": "worker-nfs", "node": "worker-storage", "path": "/srv/luma"},
                        )
                host_prep.assert_not_called()
                self.assertNotIn("worker-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_storage_set_uses_remote_node_agent_before_saving_storage_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "worker-storage",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["nfs-host", "managed-volume-path"],
                        "waitSeconds": 0,
                    },
                )

                errors = []

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "manager-host", "Swarm": {"NodeID": "manager-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                def agent_worker():
                    try:
                        for _ in range(10):
                            leased = handle_node_agent_lease(
                                agent_token,
                                {
                                    "nodeName": "worker-storage",
                                    "nodeId": "worker-node-id",
                                    "os": "linux",
                                    "capabilities": ["nfs-host", "managed-volume-path"],
                                    "waitSeconds": 1,
                                },
                            ).get("task")
                            if not leased:
                                continue
                            self.assertEqual(leased["action"], "prepare-managed-nfs-host")
                            self.assertEqual(leased["payload"]["path"], "/srv/luma")
                            handle_node_agent_complete(
                                agent_token,
                                {
                                    "nodeName": "worker-storage",
                                    "nodeId": "worker-node-id",
                                    "taskId": leased["id"],
                                    "status": "succeeded",
                                    "message": "prepared",
                                    "result": {"message": "host NFS export ready"},
                                },
                            )
                            return
                        errors.append("agent did not receive task")
                    except Exception as exc:
                        errors.append(str(exc))

                thread = threading.Thread(target=agent_worker)
                thread.start()
                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command"
                ) as host_prep:
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "worker-nfs", "node": "worker-storage", "path": "/srv/luma"},
                    )
                thread.join(timeout=5)
                self.assertFalse(errors)
                host_prep.assert_not_called()
                self.assertTrue(result["saved"])
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
                self.assertIn("worker-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_join_token_cannot_issue_agent_token_by_node_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                with self.assertRaisesRegex(LumaError, "nodeId is required"):
                    handle_node_agent_token(state["joinToken"], {"nodeName": "worker-storage"})
                issued = handle_node_agent_token(state["joinToken"], {"nodeName": "anything", "nodeId": "worker-node-id"})
                self.assertEqual(issued["nodeName"], "worker-storage")
                self.assertTrue(issued["agentToken"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_token_issuance_reports_provisioned_until_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                    }
                }
                save_state(state)
                handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage"})
                status = handle_control_status(state["deployToken"])
                item = status["nodes"]["items"][0]
                self.assertEqual(item["agentStatus"], "provisioned")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_long_poll_does_not_drop_task_queued_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                leased: list[dict[str, object] | None] = []
                errors: list[str] = []

                def lease_worker():
                    try:
                        result = handle_node_agent_lease(
                            agent_token,
                            {
                                "nodeName": "worker-storage",
                                "nodeId": "worker-node-id",
                                "os": "linux",
                                "capabilities": ["nfs-host"],
                                "waitSeconds": 2,
                            },
                        )
                        leased.append(result.get("task"))
                    except Exception as exc:
                        errors.append(str(exc))

                thread = threading.Thread(target=lease_worker)
                thread.start()
                time.sleep(0.2)
                current = load_state()
                current.setdefault("agentTasks", {})["task-race"] = {
                    "id": "task-race",
                    "nodeName": "worker-storage",
                    "action": "prepare-managed-nfs-host",
                    "payload": {"name": "race", "path": "/srv/luma"},
                    "status": "queued",
                }
                save_state(current)
                thread.join(timeout=5)
                self.assertFalse(errors)
                self.assertTrue(leased)
                self.assertIsNotNone(leased[0])
                self.assertEqual(leased[0]["id"], "task-race")
                self.assertEqual(load_state()["agentTasks"]["task-race"]["status"], "running")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_agent_heartbeat_does_not_lease_queued_task(self):
        from luma.control import server as control_server

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-storage": {
                        "region": "cn",
                        "swarmHostname": "worker-storage",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-storage", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-storage", "nodeId": "worker-node-id"})
                agent_token = issued["agentToken"]
                current = load_state()
                current.setdefault("agentTasks", {})["task-waiting"] = {
                    "id": "task-waiting",
                    "nodeName": "worker-storage",
                    "action": "prepare-managed-nfs-host",
                    "payload": {"name": "waiting", "path": "/srv/luma"},
                    "status": "queued",
                }
                save_state(current)

                result = control_server.handle_node_agent_heartbeat(
                    agent_token,
                    {
                        "nodeName": "worker-storage",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["nfs-host"],
                    },
                )

                self.assertEqual(result["status"], "ready")
                saved = load_state()
                self.assertEqual(saved["agentTasks"]["task-waiting"]["status"], "queued")
                self.assertEqual(saved["nodes"]["worker-storage"]["agent"]["status"], "online")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_node_agent_alias_can_lease_canonical_node_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "aly": {
                        "region": "cn",
                        "aliases": ["iZ0jl8auywzycory05d9cuZ"],
                        "swarmNodeId": "node-id-aly",
                    }
                }
                save_state(state)
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "iZ0jl8auywzycory05d9cuZ", "nodeId": "node-id-aly"})
                agent_token = issued["agentToken"]
                current = load_state()
                current.setdefault("agentTasks", {})["task-image"] = {
                    "id": "task-image",
                    "nodeName": "aly",
                    "action": "resolve-docker-image",
                    "payload": {"image": "ghcr.io/acme/api:latest"},
                    "status": "queued",
                }
                save_state(current)

                leased = handle_node_agent_lease(
                    agent_token,
                    {
                        "nodeName": "iZ0jl8auywzycory05d9cuZ",
                        "nodeId": "node-id-aly",
                        "os": "linux",
                        "capabilities": ["docker-image"],
                        "waitSeconds": 0,
                    },
                ).get("task")

                self.assertIsNotNone(leased)
                self.assertEqual(leased["id"], "task-image")
                handle_node_agent_complete(
                    agent_token,
                    {
                        "nodeName": "iZ0jl8auywzycory05d9cuZ",
                        "nodeId": "node-id-aly",
                        "taskId": "task-image",
                        "status": "succeeded",
                        "message": "resolved",
                        "result": {"deployed": "ghcr.io/acme/api@sha256:abc"},
                    },
                )
                self.assertEqual(load_state()["agentTasks"]["task-image"]["status"], "succeeded")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_host_prep_runs_privileged_chroot_container(self):
        docker_calls = []
        raw_calls = []

        def fake_docker_request(method, path, body=None):
            docker_calls.append((method, path, body))
            if method == "GET" and path == "/images/ubuntu%3A22.04/json":
                return {}
            if method == "POST" and path.startswith("/containers/create"):
                return {"Id": "container-id"}
            if method == "POST" and path == "/containers/container-id/start":
                return None
            if method == "POST" and path == "/containers/container-id/wait":
                return {"StatusCode": 0}
            if method == "DELETE" and path.startswith("/containers/container-id"):
                return None
            raise AssertionError(f"unexpected Docker request: {method} {path}")

        def fake_docker_request_raw(method, path, headers=None):
            raw_calls.append((method, path, headers))
            if method == "GET" and path.startswith("/containers/container-id/logs"):
                return 200, "prepared"
            raise AssertionError(f"unexpected raw Docker request: {method} {path}")

        with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_docker_request_raw
        ):
            result = _run_host_prep_container("echo ready")

        self.assertEqual(result, "prepared")
        create_body = next(body for method, path, body in docker_calls if method == "POST" and path.startswith("/containers/create"))
        self.assertEqual(create_body["Cmd"], ["chroot", "/host", "bash", "-lc", "echo ready"])
        self.assertTrue(create_body["HostConfig"]["Privileged"])
        self.assertEqual(create_body["HostConfig"]["PidMode"], "host")
        self.assertEqual(create_body["HostConfig"]["NetworkMode"], "host")
        self.assertIn("/:/host", create_body["HostConfig"]["Binds"])
        self.assertTrue(any(method == "DELETE" and path.startswith("/containers/container-id") for method, path, _ in docker_calls))

    def test_storage_remove_removes_managed_storage_nomad_job_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "cn-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "cn-node",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                with patch("luma.control.server.remove_from_nomad", return_value="Nomad job removed: luma-storage-cn-nfs") as remove:
                    result = handle_storage_remove(state["deployToken"], {"name": "cn-nfs"})
                remove.assert_called_once()
                self.assertEqual(remove.call_args.kwargs["slug"], "luma-storage-cn-nfs")
                self.assertEqual(result["storageHost"]["removed"], "Nomad job removed: luma-storage-cn-nfs")
                self.assertNotIn("cn-nfs", load_state().get("storageClasses", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_resolves_storage_class_from_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "external",
                        "endpoint": "home-nas:/srv/luma",
                        "regions": ["cn"],
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "pg-data", "initialize": "empty"}},
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed") as upsert:
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": False},
                    )
                stack_text = upsert.call_args.args[1]
                self.assertIn('"source": "pg-data"', stack_text)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                self.assertEqual(result["storage"]["mounts"][0]["endpoint"], "home-nas:/srv/luma")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_nomad_compose_deployment_registers_job_and_resolves_node_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "name": "lab",
                        "region": "home",
                        "status": "ready",
                        "tailscaleIP": "100.69.154.50",
                    }
                }
                state["registries"] = {
                    "gcode.gaojiua.com:3000": {
                        "username": "deploy",
                        "password": "registry-token",
                        "serverAddress": "gcode.gaojiua.com:3000",
                    }
                }
                save_state(state)
                handle_secret_set(state["deployToken"], {"name": "GRANARY_MYSQL_ROOT_PASSWORD", "value": "mysql-secret"})
                handle_secret_set(state["deployToken"], {"name": "GRANARY_ADMIN_PASSWORD", "value": "admin-secret"})
                handle_secret_set(state["deployToken"], {"name": "GRANARY_JWT_SECRET", "value": "jwt-secret"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump(
                    {
                        "services": {
                            "mysql": {
                                "image": "mysql:8.4.9",
                                "environment": {
                                    "MYSQL_DATABASE": "granary",
                                    "MYSQL_ROOT_PASSWORD": "${GRANARY_MYSQL_ROOT_PASSWORD}",
                                },
                                "volumes": ["granary_mysql_data:/var/lib/mysql"],
                            },
                            "granary": {
                                "image": "gcode.gaojiua.com:3000/gaojiuatech/granary:latest",
                                "environment": {
                                    "GRANARY_ADMIN_PASSWORD": "${GRANARY_ADMIN_PASSWORD}",
                                    "GRANARY_JWT_SECRET": "${GRANARY_JWT_SECRET}",
                                    "GRANARY_MYSQL_DSN": "root:${GRANARY_MYSQL_ROOT_PASSWORD}@tcp(mysql:3306)/granary",
                                },
                            },
                            "granary-frontend": {
                                "image": "gcode.gaojiua.com:3000/gaojiuatech/granary-frontend:latest",
                            },
                        },
                        "volumes": {"granary_mysql_data": {}},
                    }
                )
                sidecar = yaml.safe_dump(
                    {
                        "name": "granary",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "mysql": {"node": "lab", "exposure": "tcp-relay", "domain": "granary-db.itool.tech", "port": 3306},
                            "granary": {"node": "lab", "exposure": "tailscale-relay", "domain": "api-granary.itool.tech", "port": 8888},
                            "granary-frontend": {"node": "lab", "exposure": "tailscale-relay", "domain": "granary.itool.tech", "port": 80, "publishPort": 8081},
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job registered for granary") as deploy, patch(
                    "luma.control.server.docker_request"
                ) as docker_request, patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server._probe_public_route", return_value="Public route probe skipped"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True},
                    )

                deploy.assert_called_once()
                docker_request.assert_not_called()
                job_text = deploy.call_args.args[1]
                self.assertIn('"ID": "granary"', job_text)
                self.assertIn('"source": "granary_mysql_data"', job_text)
                self.assertIn('"server_address": "gcode.gaojiua.com:3000"', job_text)
                self.assertIn("mysql-secret", job_text)
                self.assertEqual(result["orchestrator"], "Nomad job registered for granary")
                mysql_route = (root / "routes" / "granary-mysql.yml").read_text(encoding="utf-8")
                api_route = (root / "routes" / "granary-granary.yml").read_text(encoding="utf-8")
                frontend_route = (root / "routes" / "granary-granary-frontend.yml").read_text(encoding="utf-8")
                self.assertIn("100.69.154.50:3306", mysql_route)
                self.assertIn("http://100.69.154.50:8888", api_route)
                self.assertIn("http://100.69.154.50:8081", frontend_route)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_refreshes_nomad_cni_hostports_after_nomad_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "blg": {
                        "name": "blg",
                        "region": "home",
                        "nodeId": "node-blg",
                        "tailscaleIP": "100.84.163.118",
                        "labels": {"luma.node.id": "node-blg", "luma.node.name": "blg", "region": "home"},
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["nomad-cni-repair"],
                        },
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"web": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "web-stack",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "web": {
                                "node": "blg",
                                "exposure": "tailscale-relay",
                                "domain": "web.example.com",
                                "port": 80,
                                "publishPort": 18081,
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch(
                    "luma.control.server._refresh_nomad_cni_hostports_for_job",
                    return_value={"nodes": ["blg"], "results": [{"node": "blg", "deleted": 1}], "skipped": []},
                    create=True,
                ) as refresh, patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True},
                    )

                self.assertEqual(result["cniHostports"]["nodes"], ["blg"])
                refresh.assert_called_once()
                self.assertEqual(refresh.call_args.args[2], "web-stack")
                self.assertEqual(refresh.call_args.kwargs["fallback_nodes"], ["blg"])
                self.assertEqual(refresh.call_args.kwargs["ports"], [18081])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_deployment_prepares_managed_storage_paths_before_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-nas": {
                        "name": "home-nas",
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "home-node-id",
                    }
                }
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "app-stack/pg-data", "initialize": "empty"}},
                    }
                )

                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True, "skipOrchestrator": True},
                    )
                commands = [call.args[0] for call in host_prep.call_args_list]
                self.assertTrue(any("nfs-kernel-server" in command for command in commands))
                self.assertTrue(any("/srv/luma/app-stack/pg-data" in command for command in commands))
                self.assertEqual(result["storagePreparation"][0]["prepared"], "host NFS export ready")
                self.assertEqual(result["storagePreparation"][1]["prepared"], "volume path ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_set_prepares_managed_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "cn-node": {
                        "name": "cn-node",
                        "region": "cn",
                        "status": "labeled",
                        "swarmHostname": "cn-node",
                        "swarmNodeId": "cn-node-id",
                        "labels": {"luma.node.name": "cn-node", "luma.node.id": "cn-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "cn-node", "Swarm": {"NodeID": "cn-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ):
                    result = handle_storage_set(
                        state["deployToken"],
                        {"name": "cn-nfs", "provider": "nfs", "node": "cn-node", "path": "/srv/luma"},
                    )
                self.assertEqual(result["storageHost"]["prepared"], "host NFS export ready")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_only_targets_storage_classes_referenced_by_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["portainerApiUrl"] = "https://127.0.0.1:9443/api"
                state["portainerAdminPassword"] = "secret"
                state["portainerEndpointId"] = 1
                state["swarmId"] = "swarm"
                state["nodes"] = {
                    "home-nas": {"name": "home-nas", "region": "cn", "status": "labeled", "swarmNodeId": "home-node-id", "labels": {"luma.node.name": "home-nas", "luma.node.id": "home-node-id"}},
                    "archive-nas": {"name": "archive-nas", "region": "cn", "status": "labeled"},
                }
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    },
                    "archive-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "archive-nas",
                        "path": "/srv/archive",
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "pg-data"}},
                    }
                )
                def fake_docker_request(method, path, body=None):
                    if method == "GET" and path == "/info":
                        return {"Name": "home-nas", "Swarm": {"NodeID": "home-node-id"}}
                    raise AssertionError(f"unexpected Docker request: {method} {path}")

                with patch("luma.control.server.docker_request", side_effect=fake_docker_request), patch(
                    "luma.control.server._run_host_prep_command", return_value="ok"
                ) as host_prep:
                    result = handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                self.assertEqual(len(result["storage"]["storageClasses"]), 1)
                self.assertEqual(result["storage"]["storageClasses"][0]["name"], "home-nfs")
                commands = [call.args[0] for call in host_prep.call_args_list]
                self.assertTrue(any("nfs-kernel-server" in command for command in commands))
                self.assertTrue(any("/srv/luma/pg-data" in command for command in commands))
                self.assertFalse(any("/srv/archive" in command for command in commands))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_storage_apply_rejects_unsafe_managed_volume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {"home-nas": {"name": "home-nas", "region": "cn", "status": "labeled"}}
                state["storageClasses"] = {
                    "home-nfs": {
                        "provider": "nfs",
                        "mode": "managed",
                        "node": "home-nas",
                        "path": "/srv/luma",
                    }
                }
                save_state(state)
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine", "volumes": ["pg-data:/data"]}}, "volumes": {"pg-data": {}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "app-stack",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "volumes": {"pg-data": {"storageClass": "home-nfs", "path": "../escape"}},
                    }
                )
                with self.assertRaisesRegex(LumaError, "relative and cannot contain"):
                    handle_storage_apply(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_registry_credentials_are_saved_without_returning_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                saved = handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                self.assertEqual(saved, {"host": "ghcr.io", "username": "octo", "saved": True})
                listed = handle_registry_list(state["deployToken"])
                serialized = json.dumps(listed)
                self.assertIn("ghcr.io", serialized)
                self.assertIn("octo", serialized)
                self.assertNotIn("ghp_secret", serialized)
                self.assertEqual(load_state()["registries"]["ghcr.io"]["password"], "ghp_secret")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_registry_remove_cleans_luma_registry_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                result = handle_registry_remove(state["deployToken"], {"host": "ghcr.io"})
                self.assertTrue(result["removed"])
                self.assertNotIn("portainerRegistryRemoved", result)
                self.assertNotIn("ghcr.io", load_state().get("registries", {}))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_uses_registry_auth_for_pull_and_nomad_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                )
                captured = {}

                def fake_resolve(_config, service, **kwargs):
                    captured["image_auth"] = kwargs.get("registry_auth")
                    return service, {"requested": service.image, "selected": service.image, "registryAuth": bool(kwargs.get("registry_auth"))}

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image", side_effect=fake_resolve
                ), patch(
                    "luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"
                ) as deploy:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": False},
                    )
                self.assertTrue(result["image"]["registryAuth"])
                self.assertEqual(captured["image_auth"]["username"], "octo")
                deploy.assert_called_once()
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn('"auth": {', stack)
                self.assertIn('"username": "octo"', stack)
                self.assertIn('"password": "ghp_secret"', stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_unpinned_fixed_tag_deployment_defers_image_pull_to_scheduled_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:1.2.3",
                        "region": "cn",
                        "exposure": "none",
                    }
                )

                def fail_manager_pull(*_args, **_kwargs):
                    raise AssertionError("manager Docker image pull should not be used for unpinned deployments")

                with patch("luma.control.server.docker_request_raw", side_effect=fail_manager_pull):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertTrue(result["image"]["deferred"])
                self.assertEqual(result["image"]["resolvedBy"], "scheduled-node")
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn("\"image\": \"ghcr.io/acme/api:1.2.3\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_node_agent_image_resolve_lease_injects_registry_auth_without_persisting_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "worker-1": {
                        "region": "cn",
                        "swarmHostname": "worker-1",
                        "swarmNodeId": "worker-node-id",
                        "labels": {"luma.node.name": "worker-1", "luma.node.id": "worker-node-id", "region": "cn"},
                    }
                }
                save_state(state)
                handle_registry_set(
                    state["deployToken"],
                    {"host": "ghcr.io", "username": "octo", "password": "ghp_secret"},
                )
                issued = handle_node_agent_token(state["deployToken"], {"nodeName": "worker-1", "nodeId": "worker-node-id"})
                current = load_state()
                current.setdefault("agentTasks", {})["task-image"] = {
                    "id": "task-image",
                    "nodeName": "worker-1",
                    "action": "resolve-docker-image",
                    "payload": {"image": "ghcr.io/acme/private-api:1", "forcePull": False, "platform": ""},
                    "status": "queued",
                }
                save_state(current)

                leased = handle_node_agent_lease(
                    issued["agentToken"],
                    {
                        "nodeName": "worker-1",
                        "nodeId": "worker-node-id",
                        "os": "linux",
                        "capabilities": ["docker-image"],
                        "waitSeconds": 0,
                    },
                )["task"]
                self.assertEqual(leased["payload"]["registryAuth"]["password"], "ghp_secret")
                persisted_payload = load_state()["agentTasks"]["task-image"]["payload"]
                self.assertNotIn("registryAuth", persisted_payload)
                self.assertNotIn("ghp_secret", json.dumps(load_state().get("agentTasks", {})))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_unpinned_docker_hub_deployment_keeps_original_image_without_manager_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "imageMirrors": ["mirror.local"]}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                    }
                )

                def fail_manager_pull(*_args, **_kwargs):
                    raise AssertionError("manager Docker image pull should not be used for unpinned deployments")

                with patch("luma.control.server.docker_request_raw", side_effect=fail_manager_pull):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertTrue(result["image"]["deferred"])
                self.assertFalse(result["image"]["fallback"])
                self.assertEqual(result["image"]["deployed"], "nginx:alpine")
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn("\"image\": \"nginx:alpine\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_target_pull_network_failure_configures_target_proxy_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-1": {
                        "region": "cn",
                        "status": "manager",
                        "tailscaleIP": "100.64.0.1",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "manager-1", "role.egress": "true"},
                    },
                    "worker-1": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "worker-node-id",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:latest",
                        "region": "cn",
                        "node": "worker-1",
                        "exposure": "none",
                    }
                )
                docker_nodes = [
                    {
                        "ID": "worker-node-id",
                        "Description": {"Hostname": "worker-1", "Platform": {"OS": "linux", "Architecture": "x86_64"}},
                        "Spec": {"Role": "worker", "Availability": "active", "Labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.10"},
                    }
                ]
                digest = "ghcr.io/acme/api@sha256:abc123"
                with patch("luma.control.server.docker_request", return_value=docker_nodes), patch(
                    "luma.control.server._running_egress_gateway_node_name", return_value="manager-1"
                ), patch(
                    "luma.control.server.resolve_registry_image_digest", return_value=digest
                ), patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=[
                        LumaError("target node Docker pull failed for ghcr.io/acme/api@sha256:abc123: failed to do request: EOF"),
                        {"message": "Docker daemon egress proxy configured"},
                        {"deployed": digest, "digest": digest},
                    ],
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertEqual([call.args[2] for call in agent.call_args_list], ["resolve-docker-image", "configure-docker-egress-proxy", "resolve-docker-image"])
                self.assertEqual(agent.call_args_list[1].args[3]["proxy"], "http://100.64.0.1:7890")
                self.assertEqual(agent.call_args_list[0].args[3]["image"], digest)
                self.assertEqual(result["image"]["deployed"], digest)
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn(f"\"image\": \"{digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_docker_hub_image_falls_back_to_mirror_after_proxy_retry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "manager-1": {
                        "region": "cn",
                        "status": "manager",
                        "tailscaleIP": "100.64.0.1",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "manager-1", "role.egress": "true"},
                    },
                    "worker-1": {
                        "region": "cn",
                        "status": "labeled",
                        "swarmNodeId": "worker-node-id",
                        "agent": {
                            "status": "online",
                            "lastSeen": int(time.time()),
                            "capabilities": ["docker-image", "docker-egress-proxy"],
                        },
                        "labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks"), "imageMirrors": ["mirror.local"]}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "node": "worker-1",
                        "exposure": "none",
                    }
                )
                docker_nodes = [
                    {
                        "ID": "worker-node-id",
                        "Description": {"Hostname": "worker-1", "Platform": {"OS": "linux", "Architecture": "x86_64"}},
                        "Spec": {"Role": "worker", "Availability": "active", "Labels": {"region": "cn", "luma.node.name": "worker-1", "luma.node.id": "worker-node-id"}},
                        "Status": {"State": "ready", "Addr": "100.64.0.10"},
                    }
                ]
                mirror_digest = "mirror.local/nginx@sha256:def456"
                with patch("luma.control.server.docker_request", return_value=docker_nodes), patch(
                    "luma.control.server._running_egress_gateway_node_name", return_value="manager-1"
                ) as running_egress, patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=[
                        LumaError("target node Docker pull failed for nginx:alpine: failed to do request: EOF"),
                        {"message": "Docker daemon egress proxy configured"},
                        LumaError("target node Docker pull failed for nginx:alpine: failed to do request: EOF"),
                        {"deployed": mirror_digest, "digest": mirror_digest},
                    ],
                ) as agent:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                self.assertEqual(
                    [call.args[2] for call in agent.call_args_list],
                    ["resolve-docker-image", "configure-docker-egress-proxy", "resolve-docker-image", "resolve-docker-image"],
                )
                self.assertEqual(agent.call_args_list[1].args[3]["proxy"], "http://100.64.0.1:7890")
                self.assertEqual(
                    [call.args[3]["image"] for call in agent.call_args_list if call.args[2] == "resolve-docker-image"],
                    ["nginx:alpine", "nginx:alpine", "mirror.local/nginx:alpine"],
                )
                self.assertGreaterEqual(running_egress.call_count, 1)
                self.assertTrue(result["image"]["fallback"])
                self.assertEqual(result["image"]["selected"], "mirror.local/nginx:alpine")
                self.assertEqual(result["image"]["deployed"], mirror_digest)
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertIn(f"\"image\": \"{mirror_digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_tailscale_relay_uses_pinned_nomad_node_tailscale_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "m3max": {"name": "m3max", "region": "home", "status": "ready", "tailscaleIP": "100.64.0.3"},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "home-panel",
                        "image": "nginx:alpine",
                        "region": "home",
                        "node": "m3max",
                        "exposure": "tailscale-relay",
                        "domain": "panel.example.com",
                        "port": 8080,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch(
                    "luma.control.server.deploy_to_nomad",
                    return_value="Nomad job deployed",
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "home-panel.yaml", "skipDns": True},
                    )
                route = (root / "routes" / "home-panel.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.3:8080", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_tailscale_relay_uses_publish_port_for_pinned_nomad_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {
                        "name": "home-mac-mini",
                        "region": "home",
                        "status": "ready",
                        "tailscaleIP": "100.64.0.2",
                    },
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "code-server",
                        "image": "lscr.io/linuxserver/code-server:latest",
                        "region": "home",
                        "node": "home-mac-mini",
                        "exposure": "tailscale-relay",
                        "domain": "code.example.com",
                        "port": 8443,
                        "publishPort": 1997,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.resolve_service_image",
                    side_effect=lambda _config, service, **_kwargs: (service, {"requested": service.image, "selected": service.image}),
                ), patch(
                    "luma.control.server.deploy_to_nomad",
                    return_value="Nomad job deployed",
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "code-server.yaml", "skipDns": True},
                )
                route = (root / "routes" / "code-server.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.2:1997", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_cn_edge_uses_registered_node_address_for_nomad_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "tecent": {
                        "name": "tecent",
                        "region": "cn",
                        "status": "ready",
                        "nodeId": "node-tecent",
                        "tailscaleIP": "100.64.29.91",
                        "labels": {"luma.node.name": "tecent", "region": "cn"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "defaults": {
                                "stackRoot": str(root / "stacks"),
                                "routesRoot": str(root / "routes"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                routes = root / "routes"
                routes.mkdir()
                stale_route = routes / "price-app.yml"
                stale_route.write_text(
                    "http:\n  routers:\n    price-app: {}\n  services:\n    price-app: {}\n",
                    encoding="utf-8",
                )
                compose = yaml.safe_dump({"services": {"app": {"image": "nginx:alpine"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "price",
                        "compose": "docker-compose.yml",
                        "region": "cn",
                        "services": {
                            "app": {
                                "node": "tecent",
                                "exposure": "cn-edge",
                                "domain": "price.example.com",
                                "port": 8000,
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed") as deploy, patch(
                    "luma.control.server.sync_dns", return_value="DNS skipped"
                ), patch("luma.control.server._probe_public_route", return_value="Public route reachable"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml", "skipDns": True},
                    )

                stack_text = deploy.call_args.args[1]
                self.assertIn('"Address": "100.64.29.91"', stack_text)
                self.assertNotIn('"AddressMode": "host"', stack_text)
                self.assertFalse(stale_route.exists())
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Remove stale file-provider route app=ok:Removed stale file-provider route", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_compose_tailscale_relay_uses_pinned_nomad_node_tailscale_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "home-mac-mini": {"name": "home-mac-mini", "region": "home", "status": "ready", "tailscaleIP": "100.64.0.2"},
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks"), "routesRoot": str(root / "routes")},
                        }
                    ),
                    encoding="utf-8",
                )
                compose = yaml.safe_dump({"services": {"nextcloud": {"image": "nextcloud:apache"}}})
                sidecar = yaml.safe_dump(
                    {
                        "name": "nextcloud",
                        "compose": "docker-compose.yml",
                        "region": "home",
                        "services": {
                            "nextcloud": {
                                "region": "home",
                                "node": "home-mac-mini",
                                "exposure": "tailscale-relay",
                                "domain": "next.example.com",
                                "port": 80,
                            }
                        },
                    }
                )
                with patch("luma.control.server.deploy_to_nomad", return_value="Nomad job deployed"), patch(
                    "luma.control.server.sync_dns", return_value="DNS synced"
                ), patch("luma.control.server._probe_public_route", return_value="Public route probe skipped"):
                    result = handle_compose_deployment(
                        state["deployToken"],
                        {"manifest": sidecar, "composeContent": compose, "sourceName": "luma.compose.yml"},
                    )
                route = (root / "routes" / "nextcloud-nextcloud.yml").read_text(encoding="utf-8")
                self.assertIn("http://100.64.0.2:80", route)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Resolve relay nextcloud=ok", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_deployment_fails_when_referenced_secret_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = os.environ.get("DATABASE_URL")
            try:
                os.environ.pop("DATABASE_URL", None)
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"providers": {"dns": {"type": "cloudflare", "zone": "example.com"}}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), self.assertRaises(LumaError):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_secret_list_hides_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", tmp)
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://secret"})
                result = handle_secret_list(state["deployToken"])
                self.assertEqual(result, {"secrets": ["DATABASE_URL"]})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_scoped_env_secrets_are_imported_from_deploy_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = _set_env("DATABASE_URL", "postgres://stale-global")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://legacy-global"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"):
                    result = handle_deployment(
                        state["deployToken"],
                        {
                            "manifest": manifest,
                            "sourceName": "api.yaml",
                            "skipDns": True,
                            "skipOrchestrator": True,
                            "envSecrets": {"DATABASE_URL": "postgres://scoped-api", "UNUSED_TOKEN": "do-not-store"},
                        },
                    )
                persisted = load_state()
                self.assertEqual(persisted["scopedSecrets"]["api"]["DATABASE_URL"], "postgres://scoped-api")
                self.assertNotIn("UNUSED_TOKEN", persisted["scopedSecrets"]["api"])
                self.assertIn("api/DATABASE_URL", handle_secret_list(state["deployToken"])["secrets"])
                stack_text = Path(result["written"][0]).read_text(encoding="utf-8")
                self.assertIn("postgres://scoped-api", stack_text)
                self.assertNotIn("postgres://legacy-global", stack_text)
                self.assertNotIn("postgres://stale-global", stack_text)
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Load scoped env=ok:api: imported 1 of 1 referenced secret(s)", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_existing_scoped_secret_blocks_global_fallback_for_missing_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_openai = _set_env("OPENAI_API_KEY", "global-openai")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_secret_set(state["deployToken"], {"name": "DATABASE_URL", "value": "postgres://scoped-api", "scope": "api"})
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                    }
                )
                with self.assertRaisesRegex(LumaError, "missing scoped deployment secrets for api: OPENAI_API_KEY"):
                    handle_deployment(state["deployToken"], {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("OPENAI_API_KEY", old_openai)

    def test_scoped_secret_env_does_not_leak_to_unscoped_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            old_db = _set_env("DATABASE_URL", "")
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                api_manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                worker_manifest = yaml.safe_dump(
                    {
                        "name": "worker",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "none",
                        "env": {"DATABASE_URL": "${DATABASE_URL}"},
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"):
                    handle_deployment(
                        state["deployToken"],
                        {
                            "manifest": api_manifest,
                            "sourceName": "api.yaml",
                            "skipDns": True,
                            "skipOrchestrator": True,
                            "envSecrets": {"DATABASE_URL": "postgres://api-only"},
                        },
                    )
                    # The scoped secret is persisted under the api scope only.
                    self.assertEqual(load_state()["scopedSecrets"]["api"]["DATABASE_URL"], "postgres://api-only")
                    # worker has no scope and no global secret, so its ${DATABASE_URL}
                    # cannot resolve — the api-scoped value must NOT bleed into it.
                    with self.assertRaisesRegex(LumaError, "missing deployment secret: DATABASE_URL"):
                        handle_deployment(
                            state["deployToken"],
                            {
                                "manifest": worker_manifest,
                                "sourceName": "worker.yaml",
                                "skipDns": True,
                                "skipOrchestrator": True,
                            },
                        )
                # Render is pure: no deploy ever mutates process-global env.
                self.assertEqual(os.environ.get("DATABASE_URL"), "")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)
                _restore_env("DATABASE_URL", old_db)

    def test_deployment_skip_flags_are_honored_by_control_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "nginx:alpine",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                )
                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.sync_dns"
                ) as sync, patch("luma.control.server.deploy_to_nomad") as deploy:
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                sync.assert_not_called()
                deploy.assert_not_called()
                self.assertEqual(result["dns"], "DNS skipped: --skip-dns")
                self.assertEqual(result["orchestrator"], "Orchestrator deploy skipped")
                self.assertEqual(result["probe"], "Public route probe skipped: orchestrator deploy skipped")
                steps = "\n".join(f"{step['name']}={step['status']}:{step['message']}" for step in result["steps"])
                self.assertIn("Sync DNS=ok:DNS skipped: --skip-dns", steps)
                self.assertIn("Deploy Nomad job=ok:Orchestrator deploy skipped", steps)
                self.assertIn("Probe public route=ok:Public route probe skipped: orchestrator deploy skipped", steps)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_unpinned_deployment_resolves_latest_to_digest_for_scheduled_node_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "providers": {"dns": {"type": "cloudflare", "zone": "example.com"}},
                            "defaults": {"stackRoot": str(root / "stacks")},
                        }
                    ),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:latest",
                        "region": "cn",
                        "exposure": "none",
                    }
                )

                digest = "ghcr.io/acme/api@sha256:abc123"

                with patch("luma.control.server.ensure_image_pull_egress_proxy", return_value="Image pull egress ready"), patch(
                    "luma.control.server.docker_request_raw", side_effect=AssertionError("manager Docker pull should not resolve latest tags")
                ), patch(
                    "luma.control.server.resolve_registry_image_digest",
                    return_value=digest,
                    create=True,
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                stack = (root / "stacks" / "cn" / "api" / "api.nomad.json").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["requested"], "ghcr.io/acme/api:latest")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertFalse(result["image"].get("deferred", False))
                self.assertEqual(result["image"]["resolvedBy"], "registry")
                self.assertIn(f"\"image\": \"{digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_pinned_latest_deployment_resolves_digest_when_target_agent_lacks_docker_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "lab": {
                        "region": "home",
                        "status": "labeled",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": ["terminal"]},
                        "labels": {"region": "home", "luma.node.name": "lab", "luma.node.id": "node-lab"},
                    }
                }
                save_state(state)
                (root / "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}),
                    encoding="utf-8",
                )
                manifest = yaml.safe_dump(
                    {
                        "name": "codex-gitea",
                        "image": "ghcr.io/liutianjie/gitea-review-agent:latest",
                        "region": "home",
                        "node": "lab",
                        "exposure": "none",
                    }
                )
                digest = "ghcr.io/liutianjie/gitea-review-agent@sha256:def456"

                with patch("luma.control.server.resolve_registry_image_digest", return_value=digest, create=True), patch(
                    "luma.control.server._run_node_agent_task",
                    side_effect=AssertionError("node agent image pull should not be required"),
                ):
                    result = handle_deployment(
                        state["deployToken"],
                        {"manifest": manifest, "sourceName": "codex-gitea.yaml", "skipDns": True, "skipOrchestrator": True},
                    )
                stack = (root / "stacks" / "home" / "codex-gitea" / "codex-gitea.nomad.json").read_text(encoding="utf-8")
                self.assertEqual(result["image"]["requested"], "ghcr.io/liutianjie/gitea-review-agent:latest")
                self.assertEqual(result["image"]["deployed"], digest)
                self.assertEqual(result["image"]["resolvedBy"], "registry")
                self.assertIn(f"\"image\": \"{digest}\"", stack)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_registry_digest_resolver_handles_bearer_auth_challenge(self):
        digest = "sha256:" + "a" * 64
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:acme/api:pull"'
        unauthorized = urllib.error.HTTPError(
            "https://ghcr.io/v2/acme/api/manifests/latest",
            401,
            "Unauthorized",
            {"WWW-Authenticate": challenge},
            io.BytesIO(b""),
        )
        token_response = MagicMock()
        token_response.__enter__.return_value.read.return_value = b'{"token":"registry-token"}'
        manifest_response = MagicMock()
        manifest_response.__enter__.return_value.headers = {"Docker-Content-Digest": digest}

        with patch("luma.control.server.urllib.request.urlopen", side_effect=[unauthorized, token_response, manifest_response]) as urlopen:
            resolved = resolve_registry_image_digest("ghcr.io/acme/api:latest")

        self.assertEqual(resolved, f"ghcr.io/acme/api@{digest}")
        manifest_retry = urlopen.call_args_list[2].args[0]
        self.assertEqual(manifest_retry.headers["Authorization"], "Bearer registry-token")
        self.assertEqual(manifest_retry.headers["Accept"].split(",", 1)[0], "application/vnd.oci.image.index.v1+json")
        token_request = urlopen.call_args_list[1].args[0]
        self.assertIn("scope=repository%3Aacme%2Fapi%3Apull", token_request.full_url)

    def test_service_image_falls_back_to_domestic_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "traefik/whoami:latest",
                        "region": "cn",
                        "exposure": "cn-edge",
                        "domain": "api.example.com",
                        "port": 80,
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"defaults": {"imageMirrors": ["mirror.local"]}}, None)
            with patch("luma.control.server.ensure_image_present") as ensure:
                ensure.side_effect = [LumaError("upstream failed"), "mirror.local/traefik/whoami@sha256:abc123"]
                selected, result = resolve_service_image(config, service)
            self.assertEqual(selected.image, "mirror.local/traefik/whoami@sha256:abc123")
            self.assertEqual(result["selected"], "mirror.local/traefik/whoami:latest")
            self.assertEqual(result["deployed"], "mirror.local/traefik/whoami@sha256:abc123")
            self.assertTrue(result["fallback"])

    def test_empty_image_mirrors_disables_default_mirror_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "traefik/whoami:latest",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({"defaults": {"imageMirrors": []}}, None)
            with patch("luma.control.server.ensure_image_present", side_effect=LumaError("upstream failed")) as ensure:
                with self.assertRaisesRegex(
                    LumaError,
                    "unable to pull service image; tried traefik/whoami:latest",
                ):
                    resolve_service_image(config, service)
            self.assertEqual(ensure.call_count, 1)

    def test_service_image_pull_sends_registry_auth_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_path = Path(tmp) / "service.yaml"
            service_path.write_text(
                yaml.safe_dump(
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/private-api:1",
                        "region": "cn",
                        "exposure": "none",
                    }
                ),
                encoding="utf-8",
            )
            service = load_service(service_path)
            config = LumaConfig({}, None)
            calls = []

            def fake_raw(method, path, *, headers=None):
                calls.append((method, path, headers or {}))
                if method == "GET":
                    return 404, ""
                return 200, "{}"

            with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
                selected, result = resolve_service_image(
                    config,
                    service,
                    registry_auth={"username": "octo", "password": "ghp_secret1", "serveraddress": "ghcr.io"},
                )
            self.assertEqual(selected.image, "ghcr.io/acme/private-api:1")
            self.assertTrue(result["registryAuth"])
            pull_headers = calls[-1][2]
            self.assertIn("X-Registry-Auth", pull_headers)
            self.assertTrue(pull_headers["X-Registry-Auth"].endswith("="))
            decoded = json.loads(base64.b64decode(pull_headers["X-Registry-Auth"], validate=True).decode("utf-8"))
            self.assertEqual(decoded["username"], "octo")
            self.assertEqual(decoded["password"], "ghp_secret1")
            self.assertEqual(decoded["serveraddress"], "ghcr.io")

    def test_image_pull_egress_registry_whitelist(self):
        self.assertTrue(image_pull_requires_egress("ghcr.io/acme/api:latest"))
        self.assertTrue(image_pull_requires_egress("nginx:alpine"))
        self.assertFalse(image_pull_requires_egress("docker.1panel.live/library/nginx:alpine"))

    def test_target_image_pull_proxy_uses_running_egress_allocation_node(self):
        from luma.control.server import _target_image_pull_proxy_url

        with tempfile.TemporaryDirectory() as tmp:
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(Path(tmp) / "luma.yaml"))
            try:
                Path(tmp, "luma.yaml").write_text(
                    yaml.safe_dump({"defaults": {"engine": "nomad", "nomadAddr": "http://nomad.example"}}),
                    encoding="utf-8",
                )
                state = {
                    "nodes": {
                        "aaa-stale-egress": {
                            "tailscaleIP": "100.64.0.99",
                            "labels": {"role.egress": "true"},
                        },
                        "manager-1": {
                            "nodeId": "egress-node-id",
                            "tailscaleIP": "100.64.0.1",
                        },
                        "worker-1": {
                            "nodeId": "worker-node-id",
                            "tailscaleIP": "100.64.0.10",
                        },
                    }
                }

                def request(_client, method, path, body=None):
                    self.assertEqual((method, path), ("GET", "/v1/job/egress/allocations"))
                    return [
                        {
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                            "NodeID": "egress-node-id",
                            "NodeName": "nomad-egress-host",
                        }
                    ]

                with patch("luma.control.server.NomadApi.request", request):
                    self.assertEqual(_target_image_pull_proxy_url(state, "worker-1"), "http://100.64.0.1:7890")
                    self.assertEqual(_target_image_pull_proxy_url(state, "manager-1"), "http://127.0.0.1:7890")
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_image_pull_egress_configures_daemon_proxy_through_node_agent(self):
        state = {
            "nodes": {
                "manager-1": {
                    "nodeId": "node-1",
                    "hostname": "manager-host",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": ["docker-egress-proxy"],
                    },
                }
            }
        }
        docker_calls = []

        def fake_docker(method, path, body=None):
            docker_calls.append((method, path))
            if path == "/info":
                if len([call for call in docker_calls if call[1] == "/info"]) == 1:
                    return {"Name": "manager-host", "ID": "node-1"}
                return {"Name": "manager-host", "ID": "node-1", "HTTPProxy": "http://127.0.0.1:7890"}
            raise AssertionError(path)

        with patch("luma.control.server.docker_request", side_effect=fake_docker), patch(
            "luma.control.server._require_egress_gateway_running"
        ) as require_egress, patch(
            "luma.control.server._run_node_agent_task",
            return_value={"message": "Docker daemon egress proxy configured"},
        ) as agent:
            result = ensure_image_pull_egress_proxy(state, "ghcr.io/acme/api:latest")
        require_egress.assert_called_once()
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[2], "configure-docker-egress-proxy")
        self.assertEqual(agent.call_args.kwargs["required_capability"], "docker-egress-proxy")
        self.assertEqual(result, "Docker daemon egress proxy configured")

    def test_private_direct_registry_bypasses_existing_daemon_egress_proxy(self):
        state = {
            "registries": {
                "gcode.gaojiua.com:3000": {
                    "serverAddress": "gcode.gaojiua.com:3000",
                    "username": "Nickname4th",
                    "password": "secret",
                }
            },
            "nodes": {
                "manager-1": {
                    "nodeId": "node-1",
                    "hostname": "manager-host",
                    "agent": {
                        "status": "online",
                        "lastSeen": int(time.time()),
                        "capabilities": ["docker-egress-proxy"],
                    },
                }
            },
        }
        docker_calls = []

        def fake_docker(method, path, body=None):
            docker_calls.append((method, path))
            if path == "/info":
                if len([call for call in docker_calls if call[1] == "/info"]) == 1:
                    return {
                        "Name": "manager-host",
                        "ID": "node-1",
                        "HTTPProxy": "http://127.0.0.1:7890",
                        "NoProxy": "localhost,127.0.0.1",
                    }
                return {
                    "Name": "manager-host",
                    "ID": "node-1",
                    "HTTPProxy": "http://127.0.0.1:7890",
                    "NoProxy": "localhost,127.0.0.1,gcode.gaojiua.com:3000,gcode.gaojiua.com",
                }
            raise AssertionError(path)

        with patch("luma.control.server.docker_request", side_effect=fake_docker), patch(
            "luma.control.server._run_node_agent_task",
            return_value={"message": "Docker daemon proxy bypass configured"},
        ) as agent:
            result = ensure_image_pull_network(
                state,
                "gcode.gaojiua.com:3000/gaojiuatech/tifenxia-journey-preview:latest",
            )
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[2], "configure-docker-egress-proxy")
        payload = agent.call_args.args[3]
        self.assertIn("gcode.gaojiua.com:3000", payload["noProxy"])
        self.assertIn("gcode.gaojiua.com", payload["noProxy"])
        self.assertEqual(result, "Docker daemon proxy bypass configured")

    def test_latest_service_image_uses_local_digest_cache_when_registry_matches(self):
        calls = []
        digest = "ghcr.io/acme/api@sha256:abc123"

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            if method == "GET":
                return 200, json.dumps({"RepoDigests": [digest]})
            return 200, "{}"

        with patch("luma.control.server.resolve_registry_image_digest", return_value=digest), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ):
            resolved = ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)
        self.assertEqual(resolved, digest)
        self.assertEqual([method for method, _path, _headers in calls], ["GET"])
        self.assertEqual(calls[0][1], "/images/ghcr.io%2Facme%2Fapi%40sha256%3Aabc123/json")

    def test_latest_service_image_pulls_when_registry_digest_drifted(self):
        calls = []
        remote_digest = "ghcr.io/acme/api@sha256:def456"
        local_digest = "ghcr.io/acme/api@sha256:abc123"

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            if method == "GET" and "%40sha256%3Adef456" in path:
                return 404, ""
            if method == "GET":
                return 200, json.dumps({"RepoDigests": [local_digest]})
            return 200, "Digest: sha256:def456\n"

        with patch("luma.control.server.resolve_registry_image_digest", return_value=remote_digest), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ):
            resolved = ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)

        self.assertEqual(resolved, remote_digest)
        self.assertEqual([method for method, _path, _headers in calls], ["GET", "GET", "POST"])

    def test_pinned_service_image_uses_local_cache_when_present(self):
        calls = []

        def fake_raw(method, path, *, headers=None):
            calls.append((method, path, headers or {}))
            return 200, "{}"

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw):
            ensure_image_present("ghcr.io/acme/api:1.0.0")
        self.assertEqual([method for method, _path, _headers in calls], ["GET"])

    def test_service_image_pull_error_points_to_daemon_proxy_for_registry_network_failures(self):
        def fake_raw(method, path, *, headers=None):
            if method == "GET":
                return 404, ""
            return 500, '{"message":"failed to do request: Head \\"https://ghcr.io/v2/acme/api/manifests/latest\\": EOF"}'

        with patch("luma.control.server.resolve_registry_image_digest", return_value="ghcr.io/acme/api@sha256:def456"), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ), self.assertRaisesRegex(
            LumaError,
            "Docker daemon could not reach the registry",
        ):
            ensure_image_present("ghcr.io/acme/api:latest", force_pull=True)

    def test_private_service_image_pull_error_points_to_proxy_bypass_not_egress(self):
        def fake_raw(method, path, *, headers=None):
            if method == "GET":
                return 404, ""
            return 500, '{"message":"failed to do request: Head \\"https://registry.example.com/v2/acme/api/manifests/latest\\": EOF"}'

        registry_auth = {"username": "octo", "password": "secret", "serveraddress": "registry.example.com"}
        with patch("luma.control.server.resolve_registry_image_digest", return_value="registry.example.com/acme/api@sha256:def456"), patch(
            "luma.control.server.docker_request_raw", side_effect=fake_raw
        ), self.assertRaisesRegex(
            LumaError,
            "private registry.*proxy bypass",
        ):
            ensure_image_present("registry.example.com/acme/api:latest", registry_auth=registry_auth, force_pull=True)

    def test_service_image_stream_error_points_to_target_platform_manifest(self):
        def fake_raw(method, path, *, headers=None):
            self.assertEqual(method, "POST")
            self.assertIn("platform=linux/arm64", path)
            return 200, '{"error":"no matching manifest for linux/arm64/v8 in the manifest list entries"}'

        with patch("luma.control.server.docker_request_raw", side_effect=fake_raw), self.assertRaisesRegex(
            LumaError,
            "target platform linux/arm64",
        ):
            ensure_image_present("ghcr.io/acme/api:1.0.0", platform="linux/arm64")

    def test_digest_image_without_registry_uses_docker_hub_registry(self):
        image = "mysql:8.4.9@sha256:c36050afdca850f23cef85703f84c7531a5ae155a11b5ee1c60acb09937c4084"
        self.assertEqual(registry_host_from_image(image), DEFAULT_DOCKER_REGISTRY)


def _docker_service(service_id, name, image, replicas, constraints, labels):
    return {
        "ID": service_id,
        "Spec": {
            "Name": name,
            "Labels": labels,
            "Mode": {"Replicated": {"Replicas": replicas}},
            "TaskTemplate": {
                "ContainerSpec": {"Image": image},
                "Placement": {"Constraints": constraints},
            },
        },
    }


def _docker_task(service_id, node_id, state, container_id=""):
    status = {"State": state}
    if container_id:
        status["ContainerStatus"] = {"ContainerID": container_id}
    return {
        "ID": f"{service_id}-{node_id}-{state}",
        "ServiceID": service_id,
        "NodeID": node_id,
        "DesiredState": "running",
        "Status": status,
    }


def _restore_env(key, value):
    import os

    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


class _JsonResponse:
    def __init__(self, payload: Any, headers: Dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {}

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _set_env(key, value):
    import os

    old = os.environ.get(key)
    os.environ[key] = value
    return old


class GithubImportTests(unittest.TestCase):
    def test_git_provider_credentials_support_multiple_accounts_without_returning_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                saved_personal = handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"},
                )
                saved_work = handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "work", "username": "octo-work", "token": "ghp_work"},
                )
                saved_gitea = handle_git_provider_set(
                    state["deployToken"],
                    {
                        "type": "gitea",
                        "account": "lin",
                        "baseUrl": "https://gcode.gaojiua.com:3000",
                        "username": "lin",
                        "token": "gitea_secret",
                    },
                )

                self.assertEqual(saved_personal["id"], "github:personal")
                self.assertEqual(saved_work["id"], "github:work")
                self.assertEqual(saved_gitea["id"], "gitea:lin")
                listed = handle_git_provider_list(state["deployToken"])
                serialized = json.dumps(listed)
                self.assertIn("github:personal", serialized)
                self.assertIn("github:work", serialized)
                self.assertIn("gitea:lin", serialized)
                self.assertIn("octo-work", serialized)
                self.assertNotIn("ghp_personal", serialized)
                self.assertNotIn("ghp_work", serialized)
                self.assertNotIn("gitea_secret", serialized)
                self.assertEqual(load_state()["gitProviders"]["github:work"]["token"], "ghp_work")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_git_provider_remove_deletes_only_selected_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(state["deployToken"], {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"})
                handle_git_provider_set(state["deployToken"], {"type": "github", "account": "work", "username": "octo-work", "token": "ghp_work"})

                removed = handle_git_provider_remove(state["deployToken"], {"id": "github:personal"})

                self.assertTrue(removed["removed"])
                providers = load_state().get("gitProviders", {})
                self.assertNotIn("github:personal", providers)
                self.assertIn("github:work", providers)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_gitea_provider_repository_list_uses_selected_account_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {
                        "type": "gitea",
                        "account": "lin",
                        "baseUrl": "https://gcode.gaojiua.com:3000",
                        "username": "lin",
                        "token": "gitea_secret",
                    },
                )

                def fake_urlopen(request, **_kwargs):
                    self.assertEqual(request.full_url, "https://gcode.gaojiua.com:3000/api/v1/user/repos?limit=100&page=1")
                    self.assertEqual(request.headers.get("Authorization"), "token gitea_secret")
                    return _JsonResponse(
                        [
                            {
                                "full_name": "gaojiuatech/price",
                                "clone_url": "https://gcode.gaojiua.com:3000/gaojiuatech/price.git",
                                "default_branch": "main",
                                "private": True,
                            }
                        ]
                    )

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_repositories(state["deployToken"], "gitea:lin")

                self.assertEqual(
                    result["repositories"],
                    [
                        {
                            "fullName": "gaojiuatech/price",
                            "cloneUrl": "https://gcode.gaojiua.com:3000/gaojiuatech/price.git",
                            "defaultBranch": "main",
                            "private": True,
                        }
                    ],
                )
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_github_provider_refs_include_branches_and_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"},
                )

                def fake_urlopen(request, **_kwargs):
                    self.assertEqual(request.headers.get("Authorization"), "Bearer ghp_personal")
                    if request.full_url == "https://api.github.com/repos/acme/app/branches?per_page=100&page=1":
                        return _JsonResponse([{"name": "main"}])
                    if request.full_url == "https://api.github.com/repos/acme/app/tags?per_page=100&page=1":
                        return _JsonResponse([{"name": "v1.2.3"}])
                    raise AssertionError(f"unexpected URL: {request.full_url}")

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_refs(state["deployToken"], "github:personal", "acme/app")

                self.assertEqual(result["refs"], [{"name": "main", "type": "branch"}, {"name": "v1.2.3", "type": "tag"}])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_clone_builds_shallow_command_with_ref(self):
        from luma import gitops

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), ref="main")
        self.assertEqual(captured["cmd"][:4], ["git", "clone", "--depth", "1"])
        self.assertIn("--branch", captured["cmd"])
        self.assertIn("main", captured["cmd"])

    def test_clone_fetches_full_commit_without_treating_it_as_a_branch(self):
        from luma import gitops

        captured = []
        commit = "45b76e581e894f5d66ef142944c781073c35f5ef"

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), ref=commit)

        self.assertEqual([command[1] for command in captured], ["init", "-C", "-C", "-C"])
        self.assertEqual(captured[2][-2:], ["origin", commit])
        self.assertEqual(captured[3][-2:], ["--detach", "FETCH_HEAD"])
        self.assertNotIn("--branch", [item for command in captured for item in command])

    def test_clone_accepts_uppercase_full_commit(self):
        from luma import gitops

        captured = []
        commit = "45B76E581E894F5D66EF142944C781073C35F5EF"

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), ref=commit)

        self.assertEqual(captured[2][-2:], ["origin", commit])
        self.assertEqual(captured[3][-2:], ["--detach", "FETCH_HEAD"])

    def test_clone_injects_token_and_proxy(self):
        from luma import gitops

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["username"] = Path(captured["env"]["LUMA_GIT_USERNAME_FILE"]).read_text(encoding="utf-8")
            captured["password"] = Path(captured["env"]["LUMA_GIT_PASSWORD_FILE"]).read_text(encoding="utf-8")
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), proxy="http://127.0.0.1:7890", token="ghp_secret")
        clone_url = captured["cmd"][-2]
        self.assertEqual(clone_url, "https://github.com/acme/app")
        self.assertNotIn("ghp_secret", " ".join(captured["cmd"]))
        self.assertNotIn("ghp_secret", json.dumps(captured["env"]))
        self.assertEqual(captured["username"], "x-access-token")
        self.assertEqual(captured["password"], "ghp_secret")
        self.assertEqual(captured["env"]["GIT_ASKPASS_REQUIRE"], "force")
        self.assertEqual(captured["env"]["HTTPS_PROXY"], "http://127.0.0.1:7890")

    def test_clone_redacts_token_on_failure(self):
        from luma import gitops
        from luma.errors import LumaError

        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=1, stdout="fatal: https://x-access-token:ghp_secret@github.com/acme/app not found")):
            with self.assertRaises(LumaError) as ctx:
                gitops.clone("https://github.com/acme/app", Path("/tmp/x"), token="ghp_secret")
        self.assertNotIn("ghp_secret", str(ctx.exception))
        self.assertIn("***@", str(ctx.exception))

    def test_clone_rejects_repository_urls_that_can_leak_credentials(self):
        from luma import gitops

        rejected = (
            "https://user:gitea-super-secret@gcode.example.com/acme/app.git",
            "https://gcode.example.com/acme/app.git?access_token=gitea-super-secret",
            "https://gcode.example.com/acme/app.git#token=gitea-super-secret",
            "ssh://user:password@gcode.example.com/acme/app.git",
        )
        with patch("luma.gitops.subprocess.run") as run:
            for url in rejected:
                with self.subTest(url=url), self.assertRaisesRegex(LumaError, "credentials|query parameters"):
                    gitops.clone(url, Path("/tmp/x"))
            run.assert_not_called()

    def test_clone_removes_inherited_git_trace_environment(self):
        from luma import gitops

        captured = {}

        def fake_run(_cmd, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return Mock(returncode=0, stdout="")

        trace_env = {
            "GIT_TRACE": "1",
            "GIT_TRACE2": "/tmp/git-trace",
            "GIT_TRACE_PACKET": "1",
            "GIT_TRACE_CURL": "1",
            "GIT_CURL_VERBOSE": "1",
        }
        with patch.dict(os.environ, trace_env, clear=False), patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://github.com/acme/app", Path("/tmp/x"), token="gitea-super-secret")

        for name in trace_env:
            self.assertNotIn(name, captured["env"])

    def test_image_repo_from_repo_url(self):
        from luma.control.server import _image_repo_from_repo_url, normalize_import_repo_url

        self.assertEqual(_image_repo_from_repo_url("https://github.com/Acme/App"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app.git"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("git@github.com:acme/app.git"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("github.com/acme/My_Repo"), "acme/my_repo")
        # query string / fragment must not leak into the image name
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app?token=x"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app#readme"), "acme/app")
        self.assertEqual(_image_repo_from_repo_url("https://github.com/acme/app/"), "acme/app")
        self.assertEqual(normalize_import_repo_url("LiuTianjie/luxe-monitor"), "https://github.com/LiuTianjie/luxe-monitor.git")
        self.assertEqual(normalize_import_repo_url("LiuTianjie/luxe-monitor.git"), "https://github.com/LiuTianjie/luxe-monitor.git")
        self.assertEqual(normalize_import_repo_url("https://gcode.example.com/acme/app.git"), "https://gcode.example.com/acme/app.git")
        self.assertEqual(normalize_import_repo_url("github.com/acme"), "github.com/acme")

    def test_build_image_credentials_injected_at_lease_not_stored(self):
        # Security: gitToken / registryAuth must never be persisted in the build
        # payload; they are added only when the task is leased to the agent.
        from luma.control.server import _agent_task_lease_payload

        state = {
            "gitProviders": {
                "github:personal": {"type": "github", "account": "personal", "username": "octo", "token": "ghp_secret"}
            },
            "registries": {"build-1:5000": {"username": "u", "password": "p", "serverAddress": "build-1:5000"}},
        }
        stored_payload = {"repoUrl": "https://github.com/acme/app", "gitProviderId": "github:personal", "pushHost": "build-1:5000", "repo": "acme/app"}
        # the stored payload itself carries no credentials
        self.assertNotIn("gitToken", stored_payload)
        self.assertNotIn("registryAuth", stored_payload)
        leased = _agent_task_lease_payload(state, {"action": "build-image", "payload": stored_payload})
        self.assertEqual(leased.get("gitToken"), "ghp_secret")
        self.assertEqual((leased.get("registryAuth") or {}).get("username"), "u")
        # original stored payload is untouched (no mutation back into state)
        self.assertNotIn("gitToken", stored_payload)
        self.assertNotIn("registryAuth", stored_payload)

    def test_mirror_registry_credentials_injected_at_lease_not_stored(self):
        from luma.control.server import _agent_task_lease_payload

        state = {
            "registries": {
                "registry.example.com": {
                    "username": "lae",
                    "password": "registry-secret",
                    "serverAddress": "registry.example.com",
                }
            }
        }
        stored_payload = {
            "sourceImage": "ghcr.io/liutianjie/luma-control:v1",
            "pushImage": "registry.example.com/luma-control:v1",
        }

        leased = _agent_task_lease_payload(
            state,
            {"action": "mirror-control-image", "payload": stored_payload},
        )

        self.assertEqual(leased["registryAuth"]["password"], "registry-secret")
        self.assertNotIn("registryAuth", stored_payload)

    def test_build_image_credentials_fall_back_to_legacy_github_token_secret(self):
        from luma.control.server import _agent_task_lease_payload

        state = {"secrets": {"GITHUB_TOKEN": "ghp_legacy"}}
        leased = _agent_task_lease_payload(
            state,
            {"action": "build-image", "payload": {"repoUrl": "https://github.com/acme/app", "pushHost": "build-1:5000", "repo": "acme/app"}},
        )
        self.assertEqual(leased.get("gitToken"), "ghp_legacy")

    def test_build_image_credentials_do_not_fallback_when_provider_selected_but_missing(self):
        from luma.control.server import _agent_task_lease_payload

        state = {"secrets": {"GITHUB_TOKEN": "ghp_legacy"}}
        leased = _agent_task_lease_payload(
            state,
            {
                "action": "build-image",
                "payload": {"repoUrl": "https://github.com/acme/app", "gitProviderId": "github:missing", "pushHost": "build-1:5000", "repo": "acme/app"},
            },
        )
        self.assertNotIn("gitToken", leased)

    def test_build_image_credentials_inject_provider_username(self):
        from luma.control.server import _agent_task_lease_payload

        state = {"gitProviders": {"gitea:lin": {"type": "gitea", "account": "lin", "username": "lin", "token": "gitea_secret"}}}
        leased = _agent_task_lease_payload(
            state,
            {
                "action": "build-image",
                "payload": {"repoUrl": "https://gcode.gaojiua.com:3000/acme/app", "gitProviderId": "gitea:lin", "pushHost": "build-1:5000", "repo": "acme/app"},
            },
        )
        self.assertEqual(leased.get("gitToken"), "gitea_secret")
        self.assertEqual(leased.get("gitUsername"), "lin")

    def test_clone_uses_configured_git_username_for_https_token(self):
        from luma import gitops

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env") or {}
            captured["username"] = Path(captured["env"]["LUMA_GIT_USERNAME_FILE"]).read_text(encoding="utf-8")
            captured["password"] = Path(captured["env"]["LUMA_GIT_PASSWORD_FILE"]).read_text(encoding="utf-8")
            return Mock(returncode=0, stdout="")

        with patch("luma.gitops.subprocess.run", side_effect=fake_run):
            gitops.clone("https://gcode.gaojiua.com:3000/acme/app", Path("/tmp/x"), token="gitea_secret", username="lin")

        clone_url = captured["cmd"][-2]
        self.assertEqual(clone_url, "https://gcode.gaojiua.com:3000/acme/app")
        self.assertEqual(captured["username"], "lin")
        self.assertEqual(captured["password"], "gitea_secret")
        self.assertNotIn("gitea_secret", " ".join(captured["cmd"]))
        self.assertNotIn("gitea_secret", json.dumps(captured["env"]))

    def test_head_commit_full_requires_complete_object_id(self):
        from luma import gitops

        full = "0123456789abcdef0123456789abcdef01234567"
        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=0, stdout=full + "\n")) as run:
            self.assertEqual(gitops.head_commit_full(Path("/tmp/repo")), full)
        self.assertEqual(run.call_args.args[0][-1], "HEAD")

        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=0, stdout="abc123\n")):
            with self.assertRaisesRegex(LumaError, "invalid full object id"):
                gitops.head_commit_full(Path("/tmp/repo"))
        with patch("luma.gitops.subprocess.run", return_value=Mock(returncode=0, stdout=("a" * 41) + "\n")):
            with self.assertRaisesRegex(LumaError, "invalid full object id"):
                gitops.head_commit_full(Path("/tmp/repo"))

    def test_git_provider_repositories_fetch_all_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {"type": "gitea", "account": "lin", "baseUrl": "https://gcode.gaojiua.com:3000", "username": "lin", "token": "gitea_secret"},
                )
                calls = []

                def fake_urlopen(request, **_kwargs):
                    calls.append(request.full_url)
                    if request.full_url.endswith("page=1"):
                        return _JsonResponse(
                            [
                                {
                                    "full_name": f"acme/repo-{index}",
                                    "clone_url": f"https://gcode.gaojiua.com:3000/acme/repo-{index}.git",
                                    "default_branch": "main",
                                    "private": True,
                                }
                                for index in range(100)
                            ]
                        )
                    if request.full_url.endswith("page=2"):
                        return _JsonResponse(
                            [
                                {
                                    "full_name": "acme/repo-100",
                                    "clone_url": "https://gcode.gaojiua.com:3000/acme/repo-100.git",
                                    "default_branch": "main",
                                    "private": True,
                                }
                            ]
                        )
                    raise AssertionError(f"unexpected URL: {request.full_url}")

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_repositories(state["deployToken"], "gitea:lin")

                self.assertEqual(len(result["repositories"]), 101)
                self.assertTrue(calls[0].endswith("page=1"))
                self.assertTrue(calls[1].endswith("page=2"))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_git_provider_refs_fetch_all_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                handle_git_provider_set(
                    state["deployToken"],
                    {"type": "github", "account": "personal", "username": "octo", "token": "ghp_personal"},
                )
                calls = []

                def fake_urlopen(request, **_kwargs):
                    calls.append(request.full_url)
                    if request.full_url == "https://api.github.com/repos/acme/app/branches?per_page=100&page=1":
                        return _JsonResponse([{"name": f"branch-{index}"} for index in range(100)])
                    if request.full_url == "https://api.github.com/repos/acme/app/branches?per_page=100&page=2":
                        return _JsonResponse([{"name": "branch-100"}])
                    if request.full_url == "https://api.github.com/repos/acme/app/tags?per_page=100&page=1":
                        return _JsonResponse([{"name": "v1"}])
                    raise AssertionError(f"unexpected URL: {request.full_url}")

                with patch("luma.control.server.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = handle_git_provider_refs(state["deployToken"], "github:personal", "acme/app")

                self.assertIn("https://api.github.com/repos/acme/app/branches?per_page=100&page=2", calls)
                self.assertEqual(len([item for item in result["refs"] if item["type"] == "branch"]), 101)
                self.assertIn({"name": "v1", "type": "tag"}, result["refs"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_deploy_accepts_provider_repository_mode(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["gitProviders"] = {
                    "gitea:lin": {
                        "type": "gitea",
                        "account": "lin",
                        "baseUrl": "https://gcode.gaojiua.com:3000",
                        "cloneBaseUrl": "https://gcode.gaojiua.com:3000",
                        "username": "lin",
                        "token": "gitea_secret",
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                captured = {}

                def fake_run_task(_state, node_name, action, payload, **_kwargs):
                    captured.update({"node": node_name, "action": action, "payload": payload})
                    return {
                        "image": "100.66.177.70:5000/gaojiuatech/price:abc123",
                        "manifest": "name: price\nimage: placeholder\nregion: cn\nexposure: none\n",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task), patch(
                    "luma.control.server.handle_deployment", return_value={"service": "price", "steps": []}
                ) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {"providerId": "gitea:lin", "repository": "gaojiuatech/price", "ref": "main"},
                    )

                self.assertEqual(captured["node"], "builder")
                self.assertEqual(captured["action"], "build-image")
                self.assertEqual(captured["payload"]["repoUrl"], "https://gcode.gaojiua.com:3000/gaojiuatech/price.git")
                self.assertEqual(captured["payload"]["gitProviderId"], "gitea:lin")
                self.assertEqual(captured["payload"]["registryHost"], "100.66.177.70:5000")
                self.assertEqual(captured["payload"]["pushHost"], "localhost:5000")
                self.assertEqual(captured["payload"]["repo"], "gaojiuatech/price")
                self.assertEqual(captured["payload"]["ref"], "main")
                deployed_manifest = deploy.call_args.args[1]["manifest"]
                self.assertIn("image: 100.66.177.70:5000/gaojiuatech/price:abc123", deployed_manifest)
                self.assertEqual(result["image"], "100.66.177.70:5000/gaojiuatech/price:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_requires_declared_builder_node(self):
        from luma.control.server import handle_build_deploy
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build", "docker-image"]},
                    },
                    "blg": {
                        "name": "blg",
                        "region": "cn",
                        "tailscaleIP": "100.84.163.118",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build", "docker-image"]},
                    },
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                with self.assertRaisesRegex(LumaError, "declared, ready builder node"):
                    handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app", "buildNode": "blg"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_config_declares_builder_nodes(self):
        from luma.control.server import handle_build_config_set, handle_control_status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                save_state(state)

                result = handle_build_config_set(
                    state["deployToken"],
                    {"nodes": ["builder"], "defaultNode": "builder", "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000", "directEgressNodes": ["builder"]},
                )

                self.assertEqual(result["build"]["defaultNode"], "builder")
                self.assertEqual(result["build"]["nodes"][0]["name"], "builder")
                self.assertEqual(result["build"]["directEgressNodes"], ["builder"])
                status = handle_control_status(state["deployToken"])
                self.assertEqual(status["build"]["registryHost"], "100.66.177.70:5000")
                self.assertEqual(status["build"]["directEgressNodes"], ["builder"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_direct_egress_builder_bypasses_manager_proxy(self):
        from luma.control.server import _egress_proxy_for_node

        state = {
            "managerAddr": "100.106.154.3",
            "build": {"directEgressNodes": ["builder"]},
            "nodes": {
                "builder": {"name": "builder", "region": "home", "aliases": ["build-1"]},
                "home-worker": {"name": "home-worker", "region": "home"},
            },
        }
        config = LumaConfig({"defaults": {"nomadServer": "100.106.154.3:4647"}}, None)

        self.assertEqual(_egress_proxy_for_node(config, state, "build-1"), "")
        self.assertEqual(_egress_proxy_for_node(config, state, "home-worker"), "http://100.106.154.3:7890")

    def test_runtime_config_marks_manager_for_local_ingress_addressing(self):
        from luma.control.server import _config_with_state_nodes

        config = LumaConfig({"nodes": {}}, None)
        state = {
            "nodes": {
                "manager": {
                    "name": "manager",
                    "status": "manager",
                    "region": "cn",
                    "tailscaleIP": "100.106.154.3",
                }
            }
        }

        runtime = _config_with_state_nodes(config, state)

        self.assertTrue(runtime.nodes["manager"].raw["lumaLocalIngress"])

    def test_build_run_records_failed_import_events(self):
        from luma.control.server import handle_build_deploy, handle_build_run_list, handle_build_run_get
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                def fake_run_task(*_args, **_kwargs):
                    raise LumaError("git clone failed: 403")

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task):
                    with self.assertRaisesRegex(LumaError, "git clone failed"):
                        handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app"})

                listed = handle_build_run_list(state["deployToken"])["runs"]
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["status"], "failed")
                detail = handle_build_run_get(state["deployToken"], listed[0]["id"])["run"]
                self.assertEqual(detail["request"]["buildNode"], "builder")
                self.assertNotIn("envSecrets", detail["request"])
                self.assertIn("git clone failed: 403", detail["events"][-1]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_run_event_history_is_bounded(self):
        from luma.control.server import (
            _append_build_run_event,
            _create_build_run,
            handle_build_run_get,
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                run_id = _create_build_run(
                    {"repoUrl": "https://github.com/acme/app"},
                    source="https://github.com/acme/app",
                    build_node="builder",
                )
                with patch("luma.control.server.BUILD_RUN_EVENT_LIMIT", 100):
                    for index in range(125):
                        _append_build_run_event(
                            run_id,
                            {
                                "name": "Build image",
                                "status": "progress",
                                "message": f"line-{index}",
                            },
                        )

                events = handle_build_run_get(state["deployToken"], run_id)["run"][
                    "events"
                ]
                self.assertEqual(len(events), 100)
                self.assertEqual(events[0]["message"], "line-25")
                self.assertEqual(events[-1]["message"], "line-124")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_run_history_compacts_large_legacy_output(self):
        from luma.control.server import (
            _prune_build_runs,
            handle_build_run_get,
            handle_build_run_list,
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                state["buildRuns"] = {
                    "build-legacy": {
                        "id": "build-legacy",
                        "status": "failed",
                        "message": "x" * 10_000,
                        "events": [
                            {"name": "Build image", "status": "progress", "message": f"line-{index}"}
                            for index in range(500)
                        ],
                        "createdAt": 1,
                        "updatedAt": 2,
                    }
                }
                with patch("luma.control.server.BUILD_RUN_EVENT_LIMIT", 100):
                    _prune_build_runs(state)
                save_state(state)

                detail = handle_build_run_get(state["deployToken"], "build-legacy")["run"]
                summary = handle_build_run_list(state["deployToken"])["runs"][0]
                self.assertEqual(len(detail["events"]), 100)
                self.assertEqual(detail["events"][0]["message"], "line-400")
                self.assertEqual(len(detail["message"]), 4000)
                self.assertEqual(len(summary["message"]), 500)
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_deployment_history_records_events_with_origin(self):
        from luma.control.server import _record_deployment_event, _deployment_origin, handle_deployment_history, handle_deployment_history_get

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                token = state["deployToken"]

                # origin defaults to cli when unspecified, dashboard when tagged.
                self.assertEqual(_deployment_origin(None), "cli")
                self.assertEqual(_deployment_origin({}), "cli")
                self.assertEqual(_deployment_origin({"origin": "dashboard"}), "dashboard")
                self.assertEqual(_deployment_origin({"origin": "anything-else"}), "cli")

                _record_deployment_event(kind="service", name="web", slug="web", source_name="service.yaml", origin="cli", status="active", steps=[{"name": "x", "status": "ok"}])
                _record_deployment_event(kind="compose", name="stack", slug="stack", source_name="luma.compose.yml", origin="dashboard", status="failed_partial", error="boom")

                events = handle_deployment_history(token)["events"]
                self.assertEqual(len(events), 2)
                # newest first
                self.assertEqual(events[0]["name"], "stack")
                self.assertEqual(events[0]["origin"], "dashboard")
                self.assertEqual(events[0]["status"], "failed_partial")
                self.assertEqual(events[0]["error"], "boom")
                self.assertEqual(events[1]["name"], "web")
                self.assertEqual(events[1]["origin"], "cli")
                self.assertEqual(events[1]["stepCount"], 1)
                # list omits the heavy steps array; detail endpoint returns it
                self.assertNotIn("steps", events[1])
                detail = handle_deployment_history_get(token, events[1]["id"])["event"]
                self.assertEqual(detail["steps"], [{"name": "x", "status": "ok"}])
                from luma.errors import LumaError
                with self.assertRaises(LumaError):
                    handle_deployment_history_get(token, "deploy-missing")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_run_retry_reuses_existing_record(self):
        from luma.control.server import handle_build_deploy, handle_build_run_get, handle_build_run_list, handle_build_run_retry
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                def fail_task(*_args, **_kwargs):
                    raise LumaError("docker buildx build failed")

                with patch("luma.control.server._run_node_agent_task", side_effect=fail_task):
                    with self.assertRaisesRegex(LumaError, "docker buildx build failed"):
                        handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app"})

                failed = handle_build_run_list(state["deployToken"])["runs"][0]

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "image": "100.66.177.70:5000/acme/app:abc123",
                        "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\n",
                    },
                ), patch("luma.control.server.handle_deployment", return_value={"service": "app", "steps": []}):
                    result = handle_build_run_retry(state["deployToken"], failed["id"])

                listed = handle_build_run_list(state["deployToken"])["runs"]
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["id"], failed["id"])
                self.assertEqual(listed[0]["status"], "succeeded")
                self.assertEqual(result["buildRunId"], failed["id"])
                detail = handle_build_run_get(state["deployToken"], failed["id"])["run"]
                self.assertEqual(detail["events"][0]["name"], "Build image")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_run_cancel_signals_legacy_agent_task_and_fences_success(self):
        from luma.control.server import (
            _complete_build_run,
            _create_build_run,
            handle_build_run_cancel,
            handle_build_run_get,
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                run_id = _create_build_run(
                    {"repoUrl": "https://github.com/acme/app"},
                    source="https://github.com/acme/app",
                    build_node="builder",
                )
                current = load_state()
                created_at = current["buildRuns"][run_id]["createdAt"]
                current["agentTasks"] = {
                    "task-legacy": {
                        "id": "task-legacy",
                        "nodeName": "builder",
                        "action": "build-image",
                        "payload": {"repoUrl": "https://github.com/acme/app"},
                        "status": "running",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                    }
                }
                save_state(current)

                canceled = handle_build_run_cancel(state["deployToken"], run_id, {})

                self.assertFalse(canceled["replayed"])
                self.assertEqual(canceled["run"]["status"], "canceling")
                persisted = load_state()
                self.assertEqual(persisted["buildRuns"][run_id]["agentTaskId"], "task-legacy")
                self.assertEqual(persisted["agentTasks"]["task-legacy"]["buildRunId"], run_id)
                self.assertTrue(persisted["agentTasks"]["task-legacy"]["cancelRequestedAt"])

                # A late success response cannot revive a run after the user
                # requested cancellation.
                _complete_build_run(run_id, "succeeded", result={"service": "app"})
                detail = handle_build_run_get(state["deployToken"], run_id)["run"]
                self.assertEqual(detail["status"], "canceled")
                self.assertEqual(detail["message"], "build canceled")
                self.assertEqual(detail["result"], {})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_run_cancel_stops_queued_task_immediately(self):
        from luma.control.server import _create_build_run, handle_build_run_cancel

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                run_id = _create_build_run(
                    {"repoUrl": "https://github.com/acme/app"},
                    source="https://github.com/acme/app",
                    build_node="builder",
                )
                current = load_state()
                created_at = current["buildRuns"][run_id]["createdAt"]
                current["buildRuns"][run_id]["agentTaskId"] = "task-queued"
                current["agentTasks"] = {
                    "task-queued": {
                        "id": "task-queued",
                        "nodeName": "builder",
                        "action": "build-image",
                        "payload": {"repoUrl": "https://github.com/acme/app"},
                        "status": "queued",
                        "createdAt": created_at,
                        "updatedAt": created_at,
                        "buildRunId": run_id,
                    }
                }
                save_state(current)

                result = handle_build_run_cancel(state["deployToken"], run_id, {})

                self.assertEqual(result["run"]["status"], "canceled")
                self.assertEqual(load_state()["agentTasks"]["task-queued"]["status"], "canceled")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_git_import_records_source_and_application_update_rebuilds_it(self):
        from luma.control.server import handle_application_update, handle_build_deploy, handle_deployment_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": int(time.time()), "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                build_images = iter([
                    "100.66.177.70:5000/acme/app:first",
                    "100.66.177.70:5000/acme/app:second",
                ])

                def fake_build(*_args, **_kwargs):
                    return {
                        "image": next(build_images),
                        "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\n",
                    }

                def fake_deploy(token, body, **_kwargs):
                    from luma.control.server import _load_service_manifest, _mark_service_deployment

                    service = _load_service_manifest(body["manifest"])
                    _mark_service_deployment(
                        service,
                        body["manifest"],
                        str(body.get("sourceName") or ""),
                        status="active",
                        steps=[],
                        git_source=body.get("gitSource"),
                    )
                    return {"service": service.name, "steps": []}

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_build), patch("luma.control.server.handle_deployment", side_effect=fake_deploy):
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app",
                            "ref": "main",
                            "buildNode": "builder",
                            "proxyMode": "direct",
                        },
                    )

                config = handle_deployment_config(state["deployToken"], "app")
                self.assertEqual(config["gitSource"]["repoUrl"], "https://github.com/acme/app")
                self.assertEqual(config["gitSource"]["ref"], "main")
                self.assertEqual(config["gitSource"]["buildNode"], "builder")
                self.assertEqual(config["gitSource"]["proxyMode"], "direct")
                self.assertEqual(config["gitSource"]["buildRunId"], result["buildRunId"])

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_build), patch("luma.control.server.handle_deployment", side_effect=fake_deploy) as deploy:
                    update = handle_application_update(state["deployToken"], {"name": "app"})

                self.assertEqual(update["service"], "app")
                self.assertNotEqual(update["buildRunId"], result["buildRunId"])
                update_body = deploy.call_args.args[1]
                self.assertEqual(update_body["gitSource"]["repoUrl"], "https://github.com/acme/app")
                self.assertEqual(update_body["gitSource"]["ref"], "main")
                self.assertEqual(update_body["gitSource"]["proxyMode"], "direct")
                self.assertIn("image: 100.66.177.70:5000/acme/app:second", update_body["manifest"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_run_retry_accepts_env_secret_overrides_without_storing_values(self):
        from luma.control.server import handle_build_deploy, handle_build_run_get, handle_build_run_list, handle_build_run_retry
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"], "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)

                with patch("luma.control.server._run_node_agent_task", side_effect=LumaError("missing deployment secret")):
                    with self.assertRaisesRegex(LumaError, "missing deployment secret"):
                        handle_build_deploy(state["deployToken"], {"repoUrl": "https://github.com/acme/app"})

                failed = handle_build_run_list(state["deployToken"])["runs"][0]

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "image": "100.66.177.70:5000/acme/app:abc123",
                        "manifest": "name: app\nimage: placeholder\nregion: cn\nexposure: none\nenv:\n  DATABASE_URL: ${DATABASE_URL}\n",
                    },
                ), patch("luma.control.server.handle_deployment", return_value={"service": "app", "steps": []}) as deploy:
                    result = handle_build_run_retry(
                        state["deployToken"],
                        failed["id"],
                        {"envSecrets": {"DATABASE_URL": "postgres://secret"}},
                    )

                deploy_body = deploy.call_args.args[1]
                self.assertEqual(deploy_body["envSecrets"], {"DATABASE_URL": "postgres://secret"})
                self.assertEqual(result["buildRunId"], failed["id"])
                detail = handle_build_run_get(state["deployToken"], failed["id"])["run"]
                self.assertEqual(detail["request"]["envSecretNames"], ["DATABASE_URL"])
                self.assertNotIn("postgres://secret", json.dumps(detail))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_node_agent_build_progress_is_forwarded(self):
        from luma.control.server import _wait_node_agent_task

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["agentTasks"] = {
                    "task-1": {
                        "id": "task-1",
                        "nodeName": "builder",
                        "action": "build-image",
                        "status": "succeeded",
                        "progress": [{"type": "output", "line": "Buildx builder is missing; recreating it"}],
                        "result": {"image": "100.66.177.70:5000/acme/app:abc123"},
                    }
                }
                save_state(state)
                events: list[dict[str, str]] = []

                result = _wait_node_agent_task("task-1", "builder", "build-image", timeout=1, progress=lambda event: events.append(event))

                self.assertEqual(result["image"], "100.66.177.70:5000/acme/app:abc123")
                self.assertEqual(events[0]["name"], "Build image")
                self.assertEqual(events[0]["status"], "progress")
                self.assertIn("recreating", events[0]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)

    def test_build_deploy_expands_owner_repo_shortcut_to_github_url(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                captured = {}

                def fake_run_task(_state, node_name, action, payload, **_kwargs):
                    captured.update({"node": node_name, "action": action, "payload": payload})
                    return {
                        "image": "100.66.177.70:5000/liutianjie/luxe-monitor:abc123",
                        "manifest": "name: luxe-monitor\nimage: placeholder\nregion: cn\nexposure: none\n",
                    }

                with patch("luma.control.server._run_node_agent_task", side_effect=fake_run_task), patch(
                    "luma.control.server.handle_deployment", return_value={"service": "luxe-monitor", "steps": []}
                ) as deploy:
                    result = handle_build_deploy(state["deployToken"], {"repoUrl": "LiuTianjie/luxe-monitor"})

                self.assertEqual(captured["payload"]["repoUrl"], "https://github.com/LiuTianjie/luxe-monitor.git")
                self.assertEqual(captured["payload"]["repo"], "liutianjie/luxe-monitor")
                self.assertEqual(deploy.call_args.args[1]["sourceName"], "https://github.com/LiuTianjie/luxe-monitor.git")
                self.assertEqual(result["image"], "100.66.177.70:5000/liutianjie/luxe-monitor:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_accepts_manual_manifest_when_repo_has_none(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                manual_manifest = "name: price\nimage: placeholder\nregion: cn\nexposure: none\n"

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={"image": "100.66.177.70:5000/gaojiuatech/price:abc123", "manifest": ""},
                ), patch("luma.control.server.handle_deployment", return_value={"service": "price", "steps": []}) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {"repoUrl": "https://github.com/gaojiuatech/price", "manifest": manual_manifest},
                    )

                deployed_manifest = deploy.call_args.args[1]["manifest"]
                self.assertIn("name: price", deployed_manifest)
                self.assertIn("image: 100.66.177.70:5000/gaojiuatech/price:abc123", deployed_manifest)
                self.assertEqual(result["image"], "100.66.177.70:5000/gaojiuatech/price:abc123")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_routes_compose_import_to_compose_handler(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                sidecar = "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n"
                compose = (
                    "services:\n"
                    "  web:\n"
                    "    image: 100.66.177.70:5000/acme/app/web:abc123\n"
                    "  worker:\n"
                    "    image: acme/app:local\n"
                )
                source_compose = (
                    "services:\n"
                    "  web:\n"
                    "    image: acme/app:local\n"
                    "    build: .\n"
                    "  worker:\n"
                    "    image: acme/app:local\n"
                )

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "kind": "compose",
                        "manifest": sidecar,
                        "composeContent": compose,
                        "images": {"web": "100.66.177.70:5000/acme/app/web:abc123"},
                        "imageAliases": {
                            "acme/app:local": "100.66.177.70:5000/acme/app/web:abc123"
                        },
                        "image": "100.66.177.70:5000/acme/app/web:abc123",
                    },
                ), patch("luma.control.server.handle_compose_deployment", return_value={"deployment": "app-stack", "steps": []}) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app",
                            "ref": "main",
                            "domain": "ignored.example.com",
                            "exposure": "cn-edge",
                            "port": 3000,
                            # Repository-import callers may submit the source
                            # Compose for preview, but the Builder copy has the
                            # immutable image rewrites and must win.
                            "composeContent": source_compose,
                        },
                    )

                deploy_body = deploy.call_args.args[1]
                self.assertEqual(deploy_body["manifest"], sidecar)
                deployed_compose = yaml.safe_load(deploy_body["composeContent"])
                self.assertEqual(
                    deployed_compose["services"]["web"]["image"],
                    "100.66.177.70:5000/acme/app/web:abc123",
                )
                self.assertEqual(
                    deployed_compose["services"]["worker"]["image"],
                    "100.66.177.70:5000/acme/app/web:abc123",
                )
                self.assertEqual(deploy_body["sourceName"], "https://github.com/acme/app")
                self.assertNotIn("envSecrets", deploy_body)
                self.assertEqual(result["deployment"], "app-stack")
                self.assertEqual(result["images"]["web"], "100.66.177.70:5000/acme/app/web:abc123")
                self.assertIn("Compose import ignores service-level override(s): exposure, domain, port", result["steps"][1]["message"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_deploy_passes_env_secrets_to_final_service_deploy(self):
        from luma.control.server import handle_build_deploy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "home",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "ready", "os": "linux", "capabilities": ["docker-build"]},
                    }
                }
                state["build"] = {"defaultNode": "builder", "registryHost": "100.66.177.70:5000", "pushHost": "localhost:5000"}
                save_state(state)
                repo_manifest = "name: api\nregion: cn\nexposure: none\nenv:\n  DATABASE_URL: ${DATABASE_URL}\n"

                with patch(
                    "luma.control.server._run_node_agent_task",
                    return_value={
                        "kind": "service",
                        "manifest": repo_manifest,
                        "image": "100.66.177.70:5000/acme/app:abc123",
                    },
                ), patch("luma.control.server.handle_deployment", return_value={"service": "api", "steps": []}) as deploy:
                    result = handle_build_deploy(
                        state["deployToken"],
                        {
                            "repoUrl": "https://github.com/acme/app",
                            "envSecrets": {"DATABASE_URL": "postgres://secret"},
                        },
                    )

                deploy_body = deploy.call_args.args[1]
                self.assertEqual(deploy_body["envSecrets"], {"DATABASE_URL": "postgres://secret"})
                self.assertIn("image: 100.66.177.70:5000/acme/app:abc123", deploy_body["manifest"])
                self.assertEqual(result["service"], "api")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_build_image_lease_without_secrets(self):
        from luma.control.server import _agent_task_lease_payload

        leased = _agent_task_lease_payload({}, {"action": "build-image", "payload": {"repoUrl": "https://github.com/acme/app", "pushHost": "build-1:5000", "repo": "acme/app"}})
        self.assertNotIn("gitToken", leased)
        self.assertNotIn("registryAuth", leased)

    def test_safe_image_repo_rejects_bad_input(self):
        from luma.agent import _safe_image_repo
        from luma.errors import LumaError

        self.assertEqual(_safe_image_repo("acme/app"), "acme/app")
        for bad in ("../etc", "acme/app;rm", "UP PER"):
            with self.assertRaises(LumaError):
                _safe_image_repo(bad)

    def test_safe_registry_host(self):
        from luma.agent import _safe_registry_host
        from luma.errors import LumaError

        self.assertEqual(_safe_registry_host("node-1:5000"), "node-1:5000")
        self.assertEqual(_safe_registry_host("localhost:5000"), "localhost:5000")
        with self.assertRaises(LumaError):
            _safe_registry_host("bad host:5000")

    def test_safe_repo_subpath_blocks_escape(self):
        from luma.agent import _safe_repo_subpath
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            self.assertEqual(_safe_repo_subpath(root, "sub"), (root / "sub").resolve())
            with self.assertRaises(LumaError):
                _safe_repo_subpath(root, "../../etc/passwd")

    def test_build_image_discovers_luma_manifest_outside_repo_root(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "deploy").mkdir(parents=True)
            (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
            (dest / "deploy" / "app.luma.yml").write_text(
                "name: nested-app\nimage: placeholder\nregion: cn\nexposure: none\n",
                encoding="utf-8",
            )

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.66.177.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        self.assertIn("name: nested-app", result["manifest"])
        self.assertEqual(result["image"], "100.66.177.70:5000/acme/app:abc123")

    def test_build_image_prefers_root_manifest_over_nested_compose_manifest(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "examples").mkdir(parents=True)
            (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
            (dest / ".luma.yml").write_text(
                "name: root-app\nimage: placeholder\nregion: cn\nexposure: none\n",
                encoding="utf-8",
            )
            (dest / "examples" / "luma.compose.yml").write_text(
                "name: example-stack\ncompose: docker-compose.yml\nregion: cn\n",
                encoding="utf-8",
            )
            (dest / "examples" / "docker-compose.yml").write_text(
                "services:\n  web:\n    image: nginx:alpine\n",
                encoding="utf-8",
            )

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.66.177.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        self.assertEqual(result["kind"], "service")
        self.assertIn("name: root-app", result["manifest"])

    def test_build_image_discovers_nested_compose_manifest_and_rewrites_build_services(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            deploy = dest / "deploy"
            (deploy / "web").mkdir(parents=True)
            (deploy / "prod.luma.compose.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (deploy / "docker-compose.yml").write_text(
                "services:\n  web:\n    build:\n      context: ./web\n      dockerfile: Dockerfile\n      platform: linux/amd64\n  redis:\n    image: redis:7-alpine\n",
                encoding="utf-8",
            )
            (deploy / "web" / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.66.177.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        compose = yaml.safe_load(result["composeContent"])
        self.assertEqual(result["kind"], "compose")
        self.assertIn("name: app-stack", result["manifest"])
        self.assertEqual(compose["services"]["web"]["image"], "100.66.177.70:5000/acme/app:abc123")
        self.assertNotIn("build", compose["services"]["web"])
        self.assertEqual(compose["services"]["redis"]["image"], "redis:7-alpine")

    def test_build_image_rewrites_services_reusing_a_built_compose_image(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "web").mkdir(parents=True)
            (dest / "luma.compose.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: home\n",
                encoding="utf-8",
            )
            (dest / "docker-compose.yml").write_text(
                "x-app-image: &app-image registry.example/acme/app:latest\n"
                "services:\n"
                "  web:\n"
                "    image: *app-image\n"
                "    build:\n"
                "      context: ./web\n"
                "      dockerfile: Dockerfile\n"
                "      x-luma-repo: acme/app\n"
                "  worker:\n"
                "    image: *app-image\n",
                encoding="utf-8",
            )
            (dest / "web" / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch(
            "luma.gitops.head_commit", return_value="abc123"
        ), patch("luma.agent._docker_binary", return_value="docker"), patch(
            "luma.agent._docker_buildx_available", return_value=True
        ), patch("luma.agent._ensure_buildx_builder", return_value="luma-builder"), patch(
            "luma.agent._run_process_streaming", return_value=completed
        ):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.66.177.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        compose = yaml.safe_load(result["composeContent"])
        expected = "100.66.177.70:5000/acme/app:abc123"
        self.assertEqual(compose["services"]["web"]["image"], expected)
        self.assertEqual(compose["services"]["worker"]["image"], expected)
        self.assertEqual(
            result["imageAliases"],
            {"registry.example/acme/app:latest": expected},
        )

    def test_build_image_treats_docker_compose_luma_yml_as_compose_manifest(self):
        from luma.agent import build_image

        def fake_clone(_url, dest, **_kwargs):
            (dest / "web").mkdir(parents=True)
            (dest / "docker-compose.luma.yml").write_text(
                "name: app-stack\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n",
                encoding="utf-8",
            )
            (dest / "docker-compose.yml").write_text(
                "services:\n  web:\n    build:\n      context: ./web\n      dockerfile: Dockerfile\n",
                encoding="utf-8",
            )
            (dest / "web" / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        completed = Mock(code=0, output="built\n")
        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True), patch(
            "luma.agent._ensure_buildx_builder", return_value="luma-builder"
        ), patch("luma.agent._run_process_streaming", return_value=completed):
            result = build_image(
                {
                    "repoUrl": "https://github.com/acme/app",
                    "registryHost": "100.66.177.70:5000",
                    "pushHost": "localhost:5000",
                    "repo": "acme/app",
                }
            )

        compose = yaml.safe_load(result["composeContent"])
        self.assertEqual(result["kind"], "compose")
        self.assertIn("name: app-stack", result["manifest"])
        self.assertEqual(compose["services"]["web"]["image"], "100.66.177.70:5000/acme/app:abc123")
        self.assertNotIn("build", compose["services"]["web"])

    def test_build_image_rejects_ambiguous_same_priority_luma_manifests(self):
        from luma.agent import build_image
        from luma.errors import LumaError

        def fake_clone(_url, dest, **_kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".luma.yml").write_text("name: app\nregion: cn\nbuild: {}\n", encoding="utf-8")
            (dest / "luma.compose.yml").write_text("name: app-stack\ncompose: docker-compose.yml\nregion: cn\n", encoding="utf-8")
            (dest / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx:alpine\n", encoding="utf-8")
            (dest / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")

        with patch("luma.gitops.clone", side_effect=fake_clone), patch("luma.gitops.head_commit", return_value="abc123"), patch(
            "luma.agent._docker_binary", return_value="docker"
        ), patch("luma.agent._docker_buildx_available", return_value=True):
            with self.assertRaisesRegex(LumaError, "multiple Luma deployment manifests"):
                build_image(
                    {
                        "repoUrl": "https://github.com/acme/app",
                        "registryHost": "100.66.177.70:5000",
                        "pushHost": "localhost:5000",
                        "repo": "acme/app",
                    }
                )

    def test_buildx_capability_advertised_when_present(self):
        import luma.agent as agent

        agent._BUILDX_AVAILABLE = None
        with patch("luma.agent.shutil.which", return_value="/usr/bin/docker-buildx"):
            caps = agent.node_agent_capabilities("linux")
        agent._BUILDX_AVAILABLE = None
        self.assertIn("docker-build", caps)

    def test_buildx_capability_absent_when_missing(self):
        import luma.agent as agent

        agent._BUILDX_AVAILABLE = None
        with patch("luma.agent.shutil.which", return_value=None), patch("luma.agent.os.path.exists", return_value=False):
            caps = agent.node_agent_capabilities("linux")
        agent._BUILDX_AVAILABLE = None
        self.assertNotIn("docker-build", caps)

    def test_buildx_builder_quotes_no_proxy_commas_for_driver_opt(self):
        from luma.agent import _ensure_buildx_builder, _buildx_driver_opt

        self.assertEqual(_buildx_driver_opt("env.NO_PROXY=localhost,127.0.0.1"), '"env.NO_PROXY=localhost,127.0.0.1"')

        calls = []
        inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                inspect_count += 1
                return Mock(returncode=1 if inspect_count == 1 else 0, stdout="")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(
                    returncode=0,
                    stdout=json.dumps(
                        [
                            "HTTP_PROXY=http://127.0.0.1:7890",
                            "HTTPS_PROXY=http://127.0.0.1:7890",
                            "NO_PROXY=localhost,127.0.0.1,100.66.177.70:5000",
                        ]
                    ),
                )
            return Mock(returncode=0, stdout="created\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder(
                "docker",
                proxy="http://127.0.0.1:7890",
                no_proxy="localhost,127.0.0.1,100.66.177.70:5000",
            )

        self.assertEqual(builder, "luma-builder-egress")
        create_cmd = calls[1]
        self.assertIn('"env.NO_PROXY=localhost,127.0.0.1,100.66.177.70:5000"', create_cmd)
        self.assertNotIn("env.NO_PROXY=localhost\\,127.0.0.1\\,100.66.177.70:5000", create_cmd)
        self.assertNotIn("env.NO_PROXY=localhost,127.0.0.1,100.66.177.70:5000", create_cmd)

    def test_buildx_builder_uses_docker_config_env_for_every_step(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        inspect_count = 0
        buildx_env = {"DOCKER_CONFIG": "/tmp/luma-docker-config"}

        def fake_run(cmd, **kwargs):
            nonlocal inspect_count
            calls.append((cmd, kwargs))
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                inspect_count += 1
                return Mock(returncode=1 if inspect_count == 1 else 0, stdout="")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(returncode=0, stdout="[]")
            return Mock(returncode=0, stdout="created\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder("docker", proxy="", no_proxy="localhost", env=buildx_env)

        self.assertEqual(builder, "luma-builder")
        self.assertEqual([kwargs.get("env") for _, kwargs in calls], [buildx_env, buildx_env, buildx_env, buildx_env])

    def test_buildx_builder_reuses_matching_proxy_driver_options(self):
        from luma.agent import _ensure_buildx_builder

        def fake_run(cmd, **_kwargs):
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder-egress\n")
            if cmd[:2] == ["docker", "inspect"]:
                return Mock(
                    returncode=0,
                    stdout=json.dumps(
                        [
                            "HTTP_PROXY=http://manager:7890",
                            "HTTPS_PROXY=http://manager:7890",
                            "NO_PROXY=localhost,127.0.0.1,100.64.0.0/10",
                        ]
                    ),
                )
            raise AssertionError(cmd)

        with patch("luma.agent.subprocess.run", side_effect=fake_run) as run:
            builder = _ensure_buildx_builder(
                "docker",
                proxy="http://manager:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        self.assertEqual(builder, "luma-builder-egress")
        self.assertEqual(run.call_count, 2)

    def test_buildx_builder_proxy_match_rejects_changed_no_proxy(self):
        from luma.agent import _buildx_builder_proxy_matches

        container_env = json.dumps(
            [
                "HTTP_PROXY=http://manager:7890",
                "HTTPS_PROXY=http://manager:7890",
                "NO_PROXY=localhost,127.0.0.1",
            ]
        )
        with patch(
            "luma.agent.subprocess.run",
            return_value=Mock(returncode=0, stdout=container_env),
        ):
            matches = _buildx_builder_proxy_matches(
                "docker",
                "luma-builder-egress",
                proxy="http://manager:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        self.assertFalse(matches)

    def test_buildx_builder_recreates_when_proxy_url_changes(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        docker_inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal docker_inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder-egress\n")
            if cmd[:2] == ["docker", "inspect"]:
                docker_inspect_count += 1
                proxy = "http://aly:7890" if docker_inspect_count == 1 else "http://manager:7890"
                return Mock(
                    returncode=0,
                    stdout=json.dumps(
                        [f"HTTP_PROXY={proxy}", f"HTTPS_PROXY={proxy}", "NO_PROXY=localhost,127.0.0.1"]
                    ),
                )
            return Mock(returncode=0, stdout="ok\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder(
                "docker",
                proxy="http://manager:7890",
                no_proxy="localhost,127.0.0.1",
            )

        self.assertEqual(builder, "luma-builder-egress")
        self.assertEqual(calls[2], ["docker", "buildx", "rm", "-f", "luma-builder-egress"])
        self.assertEqual(calls[3][:5], ["docker", "buildx", "create", "--name", "luma-builder-egress"])

    def test_buildx_builder_recreates_no_proxy_builder_with_proxy_options(self):
        from luma.agent import _ensure_buildx_builder

        calls = []
        docker_inspect_count = 0

        def fake_run(cmd, **_kwargs):
            nonlocal docker_inspect_count
            calls.append(cmd)
            if cmd[:3] == ["docker", "buildx", "inspect"]:
                return Mock(returncode=0, stdout="Name: luma-builder\n")
            if cmd[:2] == ["docker", "inspect"]:
                docker_inspect_count += 1
                container_env = [
                    "HTTP_PROXY=http://aly:7890",
                    "HTTPS_PROXY=http://aly:7890",
                    "NO_PROXY=localhost",
                ] if docker_inspect_count == 1 else []
                return Mock(returncode=0, stdout=json.dumps(container_env))
            return Mock(returncode=0, stdout="ok\n")

        with patch("luma.agent.subprocess.run", side_effect=fake_run):
            builder = _ensure_buildx_builder("docker", proxy="", no_proxy="localhost")

        self.assertEqual(builder, "luma-builder")
        self.assertEqual(calls[2], ["docker", "buildx", "rm", "-f", "luma-builder"])
        create_cmd = calls[3]
        self.assertNotIn("env.HTTP_PROXY=", " ".join(create_cmd))

    def test_buildx_build_recreates_missing_builder_and_retries(self):
        from luma.agent import _docker_buildx_build
        from luma.local import LocalResult

        progress: list[dict[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docker_config = root / "docker-config"
            docker_config.mkdir()
            context_dir = root / "src"
            context_dir.mkdir()
            dockerfile = context_dir / "Dockerfile"
            dockerfile.write_text("FROM busybox\n", encoding="utf-8")

            with patch(
                "luma.agent._run_process_streaming",
                side_effect=[
                    LocalResult(code=1, output='ERROR: no builder "luma-builder-egress" found\n'),
                    LocalResult(code=0, output="pushed\n"),
                ],
            ) as run, patch("luma.agent._ensure_buildx_builder", return_value="luma-builder-egress") as ensure:
                image = _docker_buildx_build(
                    docker="docker",
                    builder="luma-builder-egress",
                    docker_config=docker_config,
                    push_host="localhost:5000",
                    registry_host="100.66.177.70:5000",
                    repo="acme/app",
                    sha="abc123",
                    context_dir=context_dir,
                    dockerfile_path=dockerfile,
                    platform="linux/amd64",
                    proxy="http://127.0.0.1:7890",
                    build_timeout=1800,
                    progress=lambda event: progress.append(event),
                )

        self.assertEqual(image, "100.66.177.70:5000/acme/app:abc123")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(ensure.call_args.args, ("docker",))
        self.assertEqual(ensure.call_args.kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(ensure.call_args.kwargs["no_proxy"], "localhost,127.0.0.1,::1,localhost:5000,100.66.177.70:5000")
        self.assertTrue(ensure.call_args.kwargs["recreate"])
        self.assertEqual(ensure.call_args.kwargs["env"]["DOCKER_CONFIG"], str(docker_config))
        self.assertIn("recreating it and retrying once", progress[0]["line"])

    def test_buildx_build_streams_output_to_progress(self):
        from luma.agent import _docker_buildx_build
        from luma.local import LocalResult

        progress: list[dict[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docker_config = root / "docker-config"
            docker_config.mkdir()
            context_dir = root / "src"
            context_dir.mkdir()
            dockerfile = context_dir / "Dockerfile"
            dockerfile.write_text("FROM busybox\n", encoding="utf-8")

            captured_command = []

            def fake_stream(command, **kwargs):
                captured_command.extend(command)
                kwargs["on_line"]("#1 [internal] load build definition")
                kwargs["on_line"]("#2 pushing layers")
                return LocalResult(code=0, output="#1 [internal] load build definition\n#2 pushing layers\n")

            with patch("luma.agent._run_process_streaming", side_effect=fake_stream):
                image = _docker_buildx_build(
                    docker="docker",
                    builder="luma-builder",
                    docker_config=docker_config,
                    push_host="localhost:5000",
                    registry_host="100.66.177.70:5000",
                    repo="acme/app",
                    sha="abc123",
                    context_dir=context_dir,
                    dockerfile_path=dockerfile,
                    platform="linux/amd64",
                    proxy="",
                    build_timeout=1800,
                    progress=lambda event: progress.append(event),
                )

        self.assertEqual(image, "100.66.177.70:5000/acme/app:abc123")
        self.assertNotIn("--push", captured_command)
        output_index = captured_command.index("--output")
        self.assertEqual(
            captured_command[output_index + 1],
            "type=image,push=true,registry.insecure=true",
        )
        self.assertEqual([event["line"] for event in progress], ["#1 [internal] load build definition", "#2 pushing layers"])

    def test_registry_serve_configures_insecure_registry_and_docker_no_proxy(self):
        from luma.control.server import handle_registry_serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "cn",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build", "docker-image"]},
                    },
                    "tecent": {
                        "name": "tecent",
                        "region": "cn",
                        "tailscaleIP": "100.84.163.118",
                        "agent": {
                            "status": "online",
                            "lastSeen": now,
                            "os": "linux",
                            "capabilities": [],
                            "diagnostics": {"docker": {"proxy": {"http": "http://10.0.0.2:7890", "noProxy": "localhost,127.0.0.1"}}},
                        },
                    },
                }
                save_state(state)
                calls = []
                events = []

                def fake_run_task(_state, node_name, action, payload, **_kwargs):
                    calls.append((node_name, action, payload))
                    events.append(("agent", node_name, action))
                    return {"message": f"{action} ok"}

                def fake_deployment(_token, _body, **_kwargs):
                    events.append(("deploy", "luma-registry"))
                    return {"service": "luma-registry", "steps": []}

                with patch("luma.control.server.handle_deployment", side_effect=fake_deployment), patch(
                    "luma.control.server._run_node_agent_task", side_effect=fake_run_task
                ):
                    result = handle_registry_serve(state["deployToken"], {"node": "builder"})

                self.assertEqual(result["registryHost"], "100.66.177.70:5000")
                tecent_no_proxy = [
                    payload["noProxy"]
                    for node_name, action, payload in calls
                    if node_name == "tecent" and action == "configure-docker-egress-proxy"
                ][0]
                self.assertIn("100.66.177.70:5000", tecent_no_proxy)
                self.assertIn("100.66.177.70", tecent_no_proxy)
                self.assertIn("100.64.0.0/10", tecent_no_proxy)
                tecent_proxy = [
                    payload["proxy"]
                    for node_name, action, payload in calls
                    if node_name == "tecent" and action == "configure-docker-egress-proxy"
                ][0]
                self.assertEqual(tecent_proxy, "http://10.0.0.2:7890")
                self.assertIn(("tecent", "configure-insecure-registry", {"registry": "100.66.177.70:5000"}), calls)
                deploy_index = events.index(("deploy", "luma-registry"))
                self.assertTrue(events[:deploy_index])
                self.assertTrue(all(event[0] == "agent" for event in events[:deploy_index]))
                self.assertFalse(any(event[0] == "agent" for event in events[deploy_index + 1 :]))
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_docker_restart_recreates_and_waits_for_affected_nomad_allocations(self):
        from luma.control.server import _reconcile_allocations_after_docker_restart

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("defaults:\n  nomadAddr: http://127.0.0.1:4646\n", encoding="utf-8")
                state = {
                    "clusterId": "luma-test",
                    "nomadToken": "nomad-token",
                    "nodes": {
                        "tecent": {
                            "name": "tecent",
                            "nomadNodeId": "node-tecent",
                            "agent": {"status": "online", "lastSeen": int(time.time()), "capabilities": []},
                        }
                    },
                }
                job_reads = 0
                calls = []

                def request(_client, method, path, body=None):
                    nonlocal job_reads
                    calls.append((method, path, body))
                    if (method, path) == ("GET", "/v1/allocation/alloc-old"):
                        return {
                            "ID": "alloc-old",
                            "JobID": "api",
                            "TaskGroup": "api",
                            "NodeID": "node-tecent",
                            "ClientStatus": "running",
                            "DesiredStatus": "run",
                        }
                    if (method, path) == ("GET", "/v1/job/api/allocations"):
                        job_reads += 1
                        if job_reads == 1:
                            return [{"ID": "alloc-old", "TaskGroup": "api", "ClientStatus": "running", "DesiredStatus": "run"}]
                        return [{"ID": "alloc-new", "TaskGroup": "api", "ClientStatus": "running", "DesiredStatus": "run"}]
                    if (method, path) == ("POST", "/v1/allocation/alloc-old/stop"):
                        return {}
                    raise AssertionError((method, path, body))

                with patch("luma.control.server.NomadApi.request", request):
                    result = _reconcile_allocations_after_docker_restart(state, "tecent", {"alloc-old"}, timeout=2)

                self.assertEqual(result["recreatedAllocationIds"], ["alloc-old"])
                self.assertEqual(result["jobs"], ["api"])
                self.assertIn(("POST", "/v1/allocation/alloc-old/stop", None), calls)
                self.assertGreaterEqual(job_reads, 2)
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_docker_restart_reconcile_rejects_allocation_from_another_node(self):
        from luma.control.server import _reconcile_allocations_after_docker_restart

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("defaults:\n  nomadAddr: http://127.0.0.1:4646\n", encoding="utf-8")
                state = {
                    "nomadToken": "nomad-token",
                    "nodes": {
                        "tecent": {"name": "tecent", "nomadNodeId": "node-tecent"},
                        "blg": {"name": "blg", "nomadNodeId": "node-blg"},
                    },
                }
                allocation = {
                    "ID": "alloc-other",
                    "JobID": "api",
                    "TaskGroup": "api",
                    "NodeID": "node-blg",
                    "ClientStatus": "running",
                    "DesiredStatus": "run",
                }
                with patch("luma.control.server.NomadApi.request", return_value=allocation), self.assertRaisesRegex(
                    LumaError, "not tecent"
                ):
                    _reconcile_allocations_after_docker_restart(state, "tecent", {"alloc-other"}, timeout=1)
            finally:
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_configure_insecure_registry_only_restarts_docker_when_config_changes(self):
        from luma.agent import configure_insecure_registry

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=1\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-b", "alloc-a"}) as active_allocs:
            result = configure_insecure_registry(registry="100.66.177.70:5000")

        script = executor.sudo.call_args.args[0]
        self.assertIn("changed=$(python3", script)
        self.assertIn("changed = host not in regs", script)
        self.assertIn('if [ "$changed" = "1" ]; then systemctl restart docker; fi', script)
        self.assertTrue(result["changed"])
        self.assertTrue(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], ["alloc-a", "alloc-b"])
        active_allocs.assert_called_once_with(executor)

    def test_configure_insecure_registry_reports_no_restart_when_unchanged(self):
        from luma.agent import configure_insecure_registry

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=0\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-a"}):
            result = configure_insecure_registry(registry="100.66.177.70:5000")

        self.assertFalse(result["changed"])
        self.assertFalse(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], [])
        self.assertIn("already configured", result["message"])

    def test_configure_docker_egress_proxy_only_restarts_when_content_changes(self):
        from luma.agent import configure_docker_egress_proxy

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=1\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-b", "alloc-a"}) as active_allocs:
            result = configure_docker_egress_proxy(
                proxy="http://10.0.0.2:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        script = executor.sudo.call_args.args[0]
        self.assertIn('cmp -s "$tmp" "$f"', script)
        self.assertIn('if [ -f "$f" ] && cmp -s "$tmp" "$f"', script)
        self.assertEqual(script.count("systemctl restart docker"), 1)
        self.assertTrue(result["changed"])
        self.assertTrue(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], ["alloc-a", "alloc-b"])
        active_allocs.assert_called_once_with(executor)

    def test_configure_docker_egress_proxy_reports_no_restart_when_content_matches(self):
        from luma.agent import configure_docker_egress_proxy

        executor = MagicMock()
        executor.sudo.return_value = "LUMA_DOCKER_CONFIG_CHANGED=0\n"
        with patch("luma.agent.node_agent_os", return_value="linux"), patch(
            "luma.agent.LocalExecutor", return_value=executor
        ), patch("luma.agent._active_nomad_docker_alloc_ids", return_value={"alloc-a"}):
            result = configure_docker_egress_proxy(
                proxy="http://10.0.0.2:7890",
                no_proxy="localhost,127.0.0.1,100.64.0.0/10",
            )

        self.assertFalse(result["changed"])
        self.assertFalse(result["dockerRestarted"])
        self.assertEqual(result["affectedAllocationIds"], [])
        self.assertIn("already configured", result["message"])

    def test_registry_serve_requires_ready_linux_docker_node(self):
        from luma.control.server import handle_registry_serve
        from luma.errors import LumaError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                now = int(time.time())
                state["nodes"] = {
                    "builder": {
                        "name": "builder",
                        "region": "cn",
                        "tailscaleIP": "100.66.177.70",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    },
                    "blg": {
                        "name": "blg",
                        "region": "cn",
                        "tailscaleIP": "100.84.163.118",
                        "agent": {"status": "online", "lastSeen": now, "os": "linux", "capabilities": ["docker-build"]},
                    },
                }
                state["build"] = {"defaultNode": "builder", "nodes": ["builder"]}
                save_state(state)

                with self.assertRaisesRegex(LumaError, "docker-image capability"):
                    handle_registry_serve(state["deployToken"], {"node": "blg"})
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_registry_serve_secure_domain_uses_tls_auth_without_docker_restarts(self):
        from luma.control.server import handle_registry_serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                (root / "luma.yaml").write_text("providers: {}\n", encoding="utf-8")
                state = init_state(
                    domain="luma.example.com",
                    cluster_id="luma-test",
                    overwrite=True,
                )
                now = int(time.time())
                state["nodes"] = {
                    "manager": {
                        "name": "manager",
                        "region": "cn",
                        "tailscaleIP": "100.106.154.3",
                        "labels": {"role.nomad-manager": "true"},
                        "agent": {
                            "status": "online",
                            "lastSeen": now,
                            "os": "linux",
                            "capabilities": ["docker-image"],
                        },
                    }
                }
                save_state(state)
                password = "p" * 64
                deployed = {}

                def fake_deployment(_token, body, **_kwargs):
                    deployed.update(yaml.safe_load(body["manifest"]))
                    return {"service": "registry", "steps": []}

                with patch(
                    "luma.control.server._mirror_registry_runtime_image",
                    return_value="100.66.177.70:5000/luma-system/registry-runtime:test@sha256:"
                    + "a" * 64,
                ), patch(
                    "luma.control.server.handle_deployment",
                    side_effect=fake_deployment,
                ), patch(
                    "luma.control.server._verify_authenticated_registry",
                    return_value={"status": 200, "authenticated": True},
                ), patch(
                    "luma.control.server.handle_registry_set"
                ) as registry_set, patch(
                    "luma.control.server.handle_build_config_set"
                ) as build_config, patch(
                    "luma.control.server._run_node_agent_task"
                ) as agent_task:
                    result = handle_registry_serve(
                        state["deployToken"],
                        {
                            "node": "manager",
                            "domain": "registry.example.com",
                            "username": "lae",
                            "password": password,
                        },
                    )

                self.assertEqual(deployed["exposure"], "cn-edge")
                self.assertEqual(deployed["domain"], "registry.example.com")
                self.assertNotIn("publishPort", deployed)
                serialized = yaml.safe_dump(deployed)
                self.assertIn("basicauth.users=lae:{SHA}", serialized)
                self.assertNotIn(password, serialized)
                agent_task.assert_not_called()
                registry_set.assert_called_once_with(
                    state["deployToken"],
                    {
                        "host": "registry.example.com",
                        "username": "lae",
                        "password": password,
                    },
                )
                build_config.assert_called_once_with(
                    state["deployToken"],
                    {
                        "registryHost": "registry.example.com",
                        "pushHost": "registry.example.com",
                    },
                )
                self.assertTrue(result["secure"])
                self.assertTrue(result["authenticated"])
                self.assertTrue(result["activated"])
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_egress_proxy_for_cn_build_node_uses_manager_gateway(self):
        from luma.config import LumaConfig
        from luma.control.server import _egress_proxy_for_node

        config = LumaConfig({"defaults": {"nomadServer": "100.64.0.1:4647"}}, None)
        state = {"nodes": {"build-1": {"region": "cn", "agent": {"capabilities": ["docker-build"]}}}}
        self.assertEqual(_egress_proxy_for_node(config, state, "build-1"), "http://100.64.0.1:7890")

    def test_egress_proxy_for_global_build_node_is_empty(self):
        from luma.config import LumaConfig
        from luma.control.server import _egress_proxy_for_node

        config = LumaConfig({"defaults": {"nomadServer": "100.64.0.1:4647"}}, None)
        state = {"nodes": {"build-2": {"region": "global", "agent": {"capabilities": ["docker-build"]}}}}
        self.assertEqual(_egress_proxy_for_node(config, state, "build-2"), "")

    def test_egress_proxy_for_unknown_node_is_empty(self):
        from luma.config import LumaConfig
        from luma.control.server import _egress_proxy_for_node

        config = LumaConfig({"defaults": {"nomadServer": "100.64.0.1:4647"}}, None)
        self.assertEqual(_egress_proxy_for_node(config, {"nodes": {}}, "missing"), "")

    def test_build_proxy_request_missing_uses_auto_policy(self):
        from luma.control.server import _build_proxy_for_request

        with patch("luma.control.server._egress_proxy_for_node", return_value="http://manager:7890") as auto:
            proxy = _build_proxy_for_request(Mock(), {}, "builder", {})

        self.assertEqual(proxy, "http://manager:7890")
        auto.assert_called_once()

    def test_build_proxy_request_explicit_empty_is_direct(self):
        from luma.control.server import _build_proxy_for_request

        with patch("luma.control.server._egress_proxy_for_node") as auto:
            proxy = _build_proxy_for_request(Mock(), {}, "builder", {"proxy": ""})

        self.assertEqual(proxy, "")
        auto.assert_not_called()

    def test_build_proxy_request_direct_mode_is_direct_and_validated(self):
        from luma.control.server import _build_proxy_for_request

        with patch("luma.control.server._egress_proxy_for_node") as auto:
            proxy = _build_proxy_for_request(Mock(), {}, "builder", {"proxyMode": "direct"})

        self.assertEqual(proxy, "")
        auto.assert_not_called()
        with self.assertRaisesRegex(LumaError, "proxyMode must be auto or direct"):
            _build_proxy_for_request(Mock(), {}, "builder", {"proxyMode": "invalid"})


class RenderSecretsIsolationTests(unittest.TestCase):
    """Two concurrent-style deploys referencing the same secret name with
    different scoped values must each render their OWN value. Guards the
    regression where secrets flowed through process-global os.environ, letting
    one deploy clobber another's value mid-render."""

    def _config(self):
        return LumaConfig(
            {"defaults": {"engine": "nomad", "entrypoint": "websecure", "certResolver": "letsencrypt"}},
            None,
        )

    def _service(self, name: str):
        manifest = f"""
name: {name}
image: ghcr.io/acme/{name}:latest
region: cn
exposure: none
env:
  DB_PW: ${{DB_PW}}
"""
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(manifest)
            tmp.close()
            return load_service(Path(tmp.name)), manifest
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_same_secret_name_resolves_per_scope(self):
        from luma.control.server import _render_secrets
        from luma.nomad_render import render_nomad_job

        # body is empty -> no incoming envSecrets, so _render_secrets does NOT
        # touch the filesystem; it just reads the stored scoped values.
        state = {
            "secrets": {},
            "scopedSecrets": {
                "svc-a": {"DB_PW": "secret-A"},
                "svc-b": {"DB_PW": "secret-B"},
            },
        }
        config = self._config()

        service_a, manifest_a = self._service("svc-a")
        service_b, manifest_b = self._service("svc-b")

        secrets_a, _ = _render_secrets(state, scope="svc-a", body={}, texts=[manifest_a])
        secrets_b, _ = _render_secrets(state, scope="svc-b", body={}, texts=[manifest_b])

        self.assertEqual(secrets_a["DB_PW"], "secret-A")
        self.assertEqual(secrets_b["DB_PW"], "secret-B")

        job_a = render_nomad_job(config, service_a, as_json=False, secrets=secrets_a)["Job"]
        job_b = render_nomad_job(config, service_b, as_json=False, secrets=secrets_b)["Job"]

        self.assertEqual(job_a["TaskGroups"][0]["Tasks"][0]["Env"]["DB_PW"], "secret-A")
        self.assertEqual(job_b["TaskGroups"][0]["Tasks"][0]["Env"]["DB_PW"], "secret-B")

    def test_global_secret_used_when_no_scope_override(self):
        from luma.control.server import _render_secrets

        state = {"secrets": {"DB_PW": "global-pw"}, "scopedSecrets": {}}
        _, manifest = self._service("svc-a")
        secrets, result = _render_secrets(state, scope="svc-a", body={}, texts=[manifest])
        self.assertEqual(secrets["DB_PW"], "global-pw")
        self.assertFalse(result["scoped"])

    def test_extra_referenced_token_resolves_from_scope(self):
        # cloudflared tunnel.tokenEnv is a plain field, not a ${...} reference,
        # so it must be passed as extra_referenced or the scoped/--env paths
        # silently drop it. Scoped-only token must resolve.
        from luma.control.server import _render_secrets

        state = {
            "secrets": {},
            "scopedSecrets": {"home-tool": {"CLOUDFLARE_TUNNEL_TOKEN": "scoped-tok"}},
        }
        secrets, result = _render_secrets(
            state,
            scope="home-tool",
            body={},
            texts=["name: tool\n"],  # no ${...} in the manifest text
            extra_referenced={"CLOUDFLARE_TUNNEL_TOKEN"},
        )
        self.assertEqual(secrets["CLOUDFLARE_TUNNEL_TOKEN"], "scoped-tok")
        self.assertTrue(result["scoped"])

    def test_extra_referenced_token_imported_from_env(self):
        # --env path: the token arrives via envSecrets and must NOT be filtered
        # out as "unreferenced" just because it isn't a ${...} placeholder.
        from luma.control.server import _render_secrets

        with tempfile.TemporaryDirectory() as tmp:
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(Path(tmp) / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(Path(tmp) / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                state["secrets"] = {}
                state["scopedSecrets"] = {}
                save_state(state)
                secrets, result = _render_secrets(
                    state,
                    scope="home-tool",
                    body={"envSecrets": {"CLOUDFLARE_TUNNEL_TOKEN": "env-tok"}},
                    texts=["name: tool\n"],
                    extra_referenced={"CLOUDFLARE_TUNNEL_TOKEN"},
                )
                self.assertEqual(secrets["CLOUDFLARE_TUNNEL_TOKEN"], "env-tok")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)

    def test_extra_referenced_token_missing_raises(self):
        # No global, no scope, no --env -> must raise a clear scoped-secret error
        # rather than render later failing with an opaque missing-secret.
        from luma.control.server import _render_secrets

        state = {"secrets": {}, "scopedSecrets": {"home-tool": {"OTHER": "x"}}}
        with self.assertRaises(LumaError):
            _render_secrets(
                state,
                scope="home-tool",
                body={},
                texts=["name: tool\n"],
                extra_referenced={"CLOUDFLARE_TUNNEL_TOKEN"},
            )


class DeployConcurrencyTests(unittest.TestCase):
    """The state-touching deploy handlers must be serialized by _DEPLOY_LOCK so
    two concurrent deploys cannot interleave their read-modify-write of
    control.json (the slug-availability / scopedSecrets TOCTOU)."""

    def _assert_serialized(self, handler_name: str):
        from luma.control import server as srv

        active = 0
        max_active = 0
        track_lock = threading.Lock()

        def fake_load_state(*a, **k):
            nonlocal active, max_active
            with track_lock:
                active += 1
                max_active = max(max_active, active)
            # Widen the critical-section window so an unlocked handler would
            # show overlapping entries (max_active > 1).
            time.sleep(0.05)
            with track_lock:
                active -= 1
            # load_state runs as the first line inside the lock; abort the rest
            # of the handler — we only care that entry is serialized.
            raise LumaError("stop after entry")

        handler = getattr(srv, handler_name)
        errors: list[Exception] = []

        def run():
            try:
                handler("tok", {"manifest": "x", "composeContent": "x"})
            except LumaError:
                pass
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        with patch.object(srv, "load_state", side_effect=fake_load_state):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1)

    def test_handle_deployment_is_serialized(self):
        self._assert_serialized("handle_deployment")

    def test_handle_compose_deployment_is_serialized(self):
        self._assert_serialized("handle_compose_deployment")


class MacPublishPortGuardTests(unittest.TestCase):
    """Mac/OrbStack nodes can't use Nomad bridge port mapping (publishPort binds
    a Mac host NIC IP absent inside the OrbStack VM -> silent 502). Deploy must
    fail fast when a service pins to a darwin node with publishPort set, but only
    then — Linux pins and unpinned (Nomad-scheduled) services stay unaffected."""

    def _spec(self, yaml: str) -> ServiceSpec:
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            f.write(yaml)
            f.close()
            return load_service(Path(f.name))
        finally:
            Path(f.name).unlink(missing_ok=True)

    def _mac_state(self):
        return {"nodes": {"macmini": {"name": "macmini", "region": "home", "platform": "darwin/arm64"}}}

    def _linux_state(self):
        return {"nodes": {"lab": {"name": "lab", "region": "home", "platform": "linux/amd64"}}}

    def test_mac_node_with_publish_port_raises(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: macmini\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\npublishPort: 18080\n"
        )
        with self.assertRaises(LumaError) as ctx:
            resolve_service_node_pin(spec, self._mac_state())
        self.assertIn("publishPort", str(ctx.exception))

    def test_mac_node_without_publish_port_passes(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: macmini\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\n"
        )
        resolved = resolve_service_node_pin(spec, self._mac_state())
        self.assertEqual(resolved.node_platform, "darwin/arm64")

    def test_linux_node_with_publish_port_passes(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: lab\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\npublishPort: 18080\n"
        )
        resolved = resolve_service_node_pin(spec, self._linux_state())
        self.assertEqual(resolved.node_platform, "linux/amd64")

    def _mac_state_real_shape(self):
        # The record shape node join + agent heartbeat actually produces: NO
        # top-level platform/os/arch, os+arch nested under "agent". Before the
        # fix the guard read only top-level keys and so never fired in
        # production even though the unit fixtures (with top-level platform)
        # passed. This locks the guard to the real data shape.
        return {"nodes": {"macmini": {"name": "macmini", "region": "home", "agent": {"os": "darwin", "arch": "arm64"}}}}

    def test_mac_node_real_record_shape_with_publish_port_raises(self):
        spec = self._spec(
            "name: app\nimage: nginx:latest\nregion: home\nnode: macmini\n"
            "exposure: tailscale-relay\ndomain: a.example.com\nport: 8080\npublishPort: 18080\n"
        )
        with self.assertRaises(LumaError) as ctx:
            resolve_service_node_pin(spec, self._mac_state_real_shape())
        self.assertIn("publishPort", str(ctx.exception))

    def test_compose_mac_node_real_record_shape_bridge_exposure_raises(self):
        with self.assertRaises(LumaError) as ctx:
            _ensure_compose_exposure_supported_on_nodes(
                self._mac_state_real_shape(), self._compose_dep("macmini")
            )
        self.assertIn("macOS", str(ctx.exception))

    def _compose_dep(self, node: str, exposure: str = "tailscale-relay"):
        d = tempfile.mkdtemp()
        (Path(d) / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx:latest\n")
        sidecar = (
            "name: tool\ncompose: docker-compose.yml\nregion: home\nservices:\n"
            f"  app:\n    node: {node}\n    exposure: {exposure}\n"
        )
        if exposure != "none":
            sidecar += "    domain: a.example.com\n    port: 8080\n"
        sc = Path(d) / "luma.compose.yml"
        sc.write_text(sidecar)
        return load_compose_deployment(sc)

    def test_compose_mac_node_bridge_exposure_raises(self):
        # compose render has no host-mode path; a bridge exposure on a Mac node
        # would silently 502. Must fail fast like the native path does.
        with self.assertRaises(LumaError) as ctx:
            _ensure_compose_exposure_supported_on_nodes(self._mac_state(), self._compose_dep("macmini"))
        self.assertIn("macOS", str(ctx.exception))

    def test_compose_linux_node_bridge_exposure_passes(self):
        _ensure_compose_exposure_supported_on_nodes(self._linux_state(), self._compose_dep("lab"))

    def test_compose_mac_node_none_exposure_passes(self):
        _ensure_compose_exposure_supported_on_nodes(self._mac_state(), self._compose_dep("macmini", exposure="none"))

    def _compose_dep_sibling_pin(self, pin_node: str):
        # Exposed service carries NO node of its own; a sibling (db) pins the
        # whole group to pin_node. Since a compose group runs on one node (the
        # union of all pins), the exposed service still lands on pin_node and
        # renders a bridge port there. The guard must catch this, not skip it
        # because the exposed service's own .node is None.
        d = tempfile.mkdtemp()
        (Path(d) / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:latest\n  db:\n    image: postgres:16\n"
        )
        sidecar = (
            "name: tool\ncompose: docker-compose.yml\nregion: home\nservices:\n"
            "  app:\n    exposure: tailscale-relay\n    domain: a.example.com\n    port: 8080\n"
            f"  db:\n    node: {pin_node}\n    exposure: none\n"
        )
        sc = Path(d) / "luma.compose.yml"
        sc.write_text(sidecar)
        return load_compose_deployment(sc)

    def test_compose_mac_group_pin_via_sibling_raises(self):
        # Regression: the exposed service has no node pin, but its db sibling
        # pins the group to a Mac node -> the exposed bridge port lands on Mac
        # and silently 502s. The guard must fire on the group's resolved node.
        with self.assertRaises(LumaError) as ctx:
            _ensure_compose_exposure_supported_on_nodes(
                self._mac_state(), self._compose_dep_sibling_pin("macmini")
            )
        self.assertIn("macOS", str(ctx.exception))

    def test_compose_linux_group_pin_via_sibling_passes(self):
        _ensure_compose_exposure_supported_on_nodes(
            self._linux_state(), self._compose_dep_sibling_pin("lab")
        )


class DeployTerminalStateTests(unittest.TestCase):
    """A deploy that fails partway must drive the record to a terminal state even
    when the failure is NOT a LumaError (e.g. OSError from a full/read-only disk,
    raw socket errors from the Nomad/DNS calls). Leaving it at "pending" strands a
    ghost deploy that also blocks later deploys (pending counts as occupying
    tcp-relay ports)."""

    def test_non_luma_error_marks_failed_partial_not_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_state = _set_env("LUMA_CONTROL_STATE_DIR", str(root / "state"))
            old_config = _set_env("LUMA_CONTROL_CONFIG", str(root / "luma.yaml"))
            try:
                state = init_state(domain="luma.example.com", cluster_id="luma-test", overwrite=True)
                save_state(state)
                (root / "luma.yaml").write_text(yaml.safe_dump({"defaults": {"stackRoot": str(root / "stacks")}}), encoding="utf-8")
                manifest = yaml.safe_dump({"name": "api", "image": "registry.local/api:1", "region": "cn", "exposure": "none"})
                with patch(
                    "luma.control.server.resolve_service_node_pin",
                    side_effect=OSError("disk full"),
                ):
                    with self.assertRaises(OSError):
                        handle_deployment(
                            state["deployToken"],
                            {"manifest": manifest, "sourceName": "api.yaml", "skipDns": True, "skipOrchestrator": True},
                        )
                record = load_state()["deployments"]["services"]["api"]
                self.assertEqual(record["status"], "failed_partial")
                self.assertNotEqual(record["status"], "pending")
            finally:
                _restore_env("LUMA_CONTROL_STATE_DIR", old_state)
                _restore_env("LUMA_CONTROL_CONFIG", old_config)


class TcpIngressRefreshAdvisoryTests(unittest.TestCase):
    """A deploy that introduces a tcp-relay port Traefik has no static entrypoint
    for must surface an advisory (the job runs but the port is unreachable until
    `luma update manager` rebuilds entrypoints). Ports already served by an active
    deployment — including this slug's own active record on redeploy — must NOT
    warn, or the advisory becomes noise that erodes trust."""

    def _state(self, deployments):
        return {"deployments": {"services": deployments.get("services", {}), "compose": deployments.get("compose", {})}}

    def test_brand_new_port_warns(self):
        state = self._state({})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [3306])

    def test_port_served_by_active_deployment_does_not_warn(self):
        state = self._state({"services": {"other": {"status": "active", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [])

    def test_port_only_in_failed_deployment_still_warns(self):
        # A failed deploy never made it into Traefik's entrypoint set.
        state = self._state({"services": {"other": {"status": "failed_partial", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [3306])

    def test_redeploy_same_port_does_not_warn(self):
        state = self._state({"services": {"me": {"status": "active", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306]), [])

    def test_mixed_new_and_existing_warns_only_for_new(self):
        state = self._state({"services": {"other": {"status": "active", "tcpRelayPorts": [3306]}}})
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(state, [3306, 5432]), [5432])

    def test_no_tcp_ports_no_warning(self):
        self.assertEqual(_tcp_relay_ports_needing_ingress_refresh(self._state({}), []), [])


class AgentCallbackNoDeployLockTests(unittest.TestCase):
    """A deploy holds _DEPLOY_LOCK while it dispatches image-pull tasks to a node
    agent and waits (up to AGENT_TASK_TIMEOUT_SECONDS) for the agent to call back
    via handle_node_agent_lease / handle_node_agent_complete. If those callback
    handlers ever grabbed _DEPLOY_LOCK too (e.g. someone adds @_serialize_deploy),
    every agent-dispatched deploy would self-deadlock until the 300s timeout. Lock
    this invariant: the callbacks must NOT block on the deploy lock."""

    def _assert_not_blocked_by_deploy_lock(self, handler):
        result: dict = {}

        def run():
            try:
                handler("tok", {})  # empty body -> handler validates and raises fast
            except LumaError as exc:
                result["raised"] = str(exc)
            except Exception as exc:  # pragma: no cover - unexpected
                result["error"] = repr(exc)

        with _DEPLOY_LOCK:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(timeout=5)
            blocked = thread.is_alive()
        # The handler must have run to completion (raised a validation error)
        # while the deploy lock was held by this thread.
        self.assertFalse(blocked, "agent callback blocked on _DEPLOY_LOCK — would deadlock agent-dispatched deploys")
        self.assertIn("raised", result)

    def test_agent_lease_does_not_block_on_deploy_lock(self):
        from luma.control.server import handle_node_agent_lease

        self._assert_not_blocked_by_deploy_lock(handle_node_agent_lease)

    def test_agent_complete_does_not_block_on_deploy_lock(self):
        from luma.control.server import handle_node_agent_complete

        self._assert_not_blocked_by_deploy_lock(handle_node_agent_complete)


if __name__ == "__main__":
    unittest.main()
