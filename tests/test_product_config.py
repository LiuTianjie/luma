"""Repository assets, bootstrap profiles, user config and env files."""
import base64
import errno
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

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
from luma.assets import asset_path
from luma.config import LumaConfig
from luma.cloudflare import CloudflareClient, delete_dns, sync_control_dns
from luma.bootstrap import (
    _acme_email,
    _deploy_nomad_job,
    _is_tailscale_manager_addr,
    _last_command_value,
    _nomad_tmpfs_compat_status,
    _parse_kernel_version,
    _parse_nomad_version,
    _traefik_ports,
    configure_firewall,
    configure_public_port_guards,
    configure_tailscale_watchdog,
)
from luma.control.context import save_context
from luma.control.server import _normalize_container_stats_for_engine, _service_stats_by_name
from luma.envfile import load_env_file
from luma.egress import ensure_mihomo_direct_domains, minimal_mihomo_config_from_bytes
from luma.errors import LumaError
from luma.local import LocalExecutor
from luma.profiles import PROFILES
from luma.service import load_service
from luma.cli import main
from tests.support import _restore_env, _set_env


class ProductConfigTests(unittest.TestCase):
    def test_version_files_stay_in_sync(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        init_file = (root / "luma" / "__init__.py").read_text(encoding="utf-8")

        pyproject_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
        init_version = re.search(r'^__version__ = "([^"]+)"$', init_file, re.MULTILINE)

        self.assertIsNotNone(pyproject_version)
        self.assertIsNotNone(init_version)
        self.assertEqual(pyproject_version.group(1), init_version.group(1))

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
            lease_calls = 0
            def lease_agent_task(**_kwargs):
                nonlocal lease_calls
                lease_calls += 1
                if lease_calls == 1:
                    raise LumaError("temporary control failure")
                return {"task": {}}
            client.lease_agent_task.side_effect = lease_agent_task
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

    def test_successful_task_report_retries_without_inverting_to_failed(self):
        # A task that executes successfully but whose SUCCESS report to Control
        # fails (network drop) must retain its busy heartbeat and retry the same
        # terminal result. The host mutation already happened, so it must never
        # be inverted into a synthetic failed result.
        client = Mock()
        client.complete_agent_task.side_effect = [
            LumaError("control unreachable"),
            {"taskId": "task-x", "status": "succeeded"},
        ]
        task = {"id": "task-x", "action": "noop", "payload": {}}
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"busyHeartbeatIntervalSeconds": 0.01}), encoding="utf-8")
            with patch("luma.agent.execute_agent_task", return_value={"message": "done"}), patch(
                "luma.agent.node_agent_os", return_value="linux"
            ), patch("luma.agent.node_agent_arch", return_value="x86_64"), patch(
                "luma.agent.node_agent_capabilities", return_value=["docker-image"]
            ), patch("luma.agent.node_agent_metrics", return_value={}), patch(
                "luma.agent.time.sleep"
            ) as sleep, patch("sys.stderr"):
                restart = _complete_agent_task(client, node_name="lab", node_id="node-1", task=task, config_path=config)

        self.assertFalse(restart)
        self.assertEqual(client.complete_agent_task.call_count, 2)
        self.assertEqual(
            [call.kwargs["status"] for call in client.complete_agent_task.call_args_list],
            ["succeeded", "succeeded"],
        )
        sleep.assert_called_once_with(0.5)

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
                with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
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
        ), patch("sys.argv", ["/Users/mini/.local/share/luma/venv/bin/luma"]), patch.dict(
            os.environ, {"HOME": "/var/root", "LUMA_AGENT_EXECUTABLE": ""}, clear=False
        ):
            result = update_luma_install(install_ref="main", config_path=Path("/opt/luma/node-agent/agent.json"))

        self.assertFalse(result["restartAgent"])
        install_env = run.call_args.kwargs["env"]
        self.assertEqual(install_env["LUMA_USER_HOME"], "/Users/mini")
        self.assertEqual(install_env["LUMA_INSTALL_HOME"], "/Users/mini/.local/share/luma")
        self.assertEqual(install_env["LUMA_BIN_DIR"], "/Users/mini/.local/bin")
        service_command = executor.sudo.call_args_list[0].args[0]
        self.assertIn("/Users/mini/.local/bin/luma", service_command)
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
                        "serverAddr": "100.64.0.125:4647",
                        "tailscaleAuthKey": "ts-key",
                    },
                }
            )

        install_nomad.assert_called_once()
        install_kwargs = install_nomad.call_args.kwargs
        self.assertEqual(install_kwargs["role"], "client")
        self.assertEqual(install_kwargs["region"], "global")
        self.assertEqual(install_kwargs["node_name"], "bot")
        self.assertEqual(install_kwargs["server_addrs"], ["100.64.0.125:4647"])
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
        self.assertIn("container-terminal", node_agent_capabilities("linux"))
        self.assertIn("container-terminal", node_agent_capabilities("darwin"))
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
                            "name": "home-2",
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
                                                "allocIds": ["old-ledger", "current-ledger"],
                                                "shadowedAllocIds": ["current-ledger"],
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
            with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                code = main(["--no-env", "doctor", "--deep"])

            self.assertEqual(code, 1)
            output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
            self.assertIn("Node home-2 Nomad CNI hostports: fail", output)
            self.assertIn("tcp/8081 old-ledger -> current-ledger", output)
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
                            "name": "home-2",
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
            with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                code = main(["--no-env", "doctor", "--deep"])

            self.assertEqual(code, 1)
            output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
            self.assertIn("Node home-2 Nomad CNI hostports: fail", output)
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
                            "name": "home-2",
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
            with patch("luma.cli.common.ControlClient", return_value=client), patch("builtins.print") as printed:
                code = main(["--no-env", "doctor", "--deep"])

            self.assertEqual(code, 1)
            output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
            self.assertIn("Node home-2 Nomad Docker pull timeout: fail", output)
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
        self.assertIn('"$SOURCE_DIR/luma/installation.py" shim > "$shim_tmp"', installer)
        self.assertIn('mv -f "$shim_tmp" "$BIN_DIR/luma"', installer)

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
            installer.index('CANDIDATE_DIR="$(mktemp -d'),
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
            self.assertIn("LUMA_DNS_EDGE_TARGET", result)
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
        from luma.cli.manager import _find_nomad_cli

        with patch("luma.cli.manager.shutil.which", return_value=None), patch("luma.cli.manager.Path.exists", return_value=True), patch(
            "luma.cli.manager.os.access", return_value=True
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
        index_html = asset_path("dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("Luma · 控制台", index_html)
        scripts = re.findall(r'<script[^>]+src="/dashboard/([^\"]+\.js)"', index_html)
        self.assertTrue(scripts)
        self.assertIn("/v1/dashboard", asset_path(f"dashboard/{scripts[0]}").read_text(encoding="utf-8"))
        images = list(asset_path("dashboard").glob("*.png"))
        self.assertTrue(images)
        self.assertGreater(images[0].stat().st_size, 0)
        root = Path(__file__).resolve().parents[1]
        self.assertIn('"assets/dashboard/*"', (root / "pyproject.toml").read_text(encoding="utf-8"))

    def test_luma_control_nomad_job_uses_autorevert_and_node_pin(self):
        from luma.nomad_render import render_control_job

        job = render_control_job(image="ghcr.io/mini/luma-control:0.1.0", node_name="aly", as_json=False)["Job"]
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
            ["registry.example.net", "registry.example.net"],
        )

        self.assertTrue(changed)
        self.assertEqual(
            yaml.safe_load(updated)["rules"],
            ["DOMAIN,registry.example.net,DIRECT", "MATCH,EGRESS"],
        )
        unchanged, changed_again = ensure_mihomo_direct_domains(updated, ["registry.example.net"])
        self.assertFalse(changed_again)
        self.assertEqual(unchanged, updated)


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



if __name__ == "__main__":
    unittest.main()
