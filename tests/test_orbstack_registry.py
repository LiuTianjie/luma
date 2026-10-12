from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luma import orbstack_registry as orb
from luma.agent import execute_agent_task, node_agent_capabilities
from luma.errors import LumaError
from luma.registry_access import ensure_managed_registry_access, normalize_managed_registry_hosts
from luma.registry_proxy import proxy_bypasses_registry_hosts


HOST = "100.66.177.70:5000"


class ManagedRegistryContractTests(unittest.TestCase):
    def test_existing_proxy_bypass_semantics_are_shared_by_both_runtimes(self):
        cases = [
            (HOST, "100.64.0.0/10", True), (HOST, "100.66.177.70", True),
            (HOST, HOST, True), (HOST, "100.66.177.70:5001", False),
            (HOST, "*", True), (HOST, "10.0.0.0/8", False),
            ("registry.example.com:5000", "example.com", True),
            ("registry.example.com:5000", "example.com:5000", True),
            ("registry.example.com:5000", "example.com:5001", False),
            ("registry.example.com:5000", ".example.com", True),
            ("example.com:5000", ".example.com", False),
            ("badexample.com:5000", "example.com", False),
            ("registry.example.com:5000", "*.example.com", True),
            ("[2001:db8::1]:5000", "2001:db8::/32", True),
            ("[2001:db8::1]:5000", "2001:db8::1", True),
            ("[2001:db8::1]:5000", "[2001:db8::1]:5000", True),
            ("[2001:db8::1]:5000", "[2001:db8::1]:5001", False),
            ("[2001:db8::1]:5000", "[2001:db8::1]garbage", False),
        ]
        for host, no_proxy, expected in cases:
            with self.subTest(host=host, no_proxy=no_proxy):
                self.assertEqual(proxy_bypasses_registry_hosts({"HTTPSProxy": "http://proxy:7890", "NoProxy": no_proxy}, [host]), expected)
        self.assertFalse(proxy_bypasses_registry_hosts({"HTTPSProxy": "http://proxy:7890", "NoProxy": HOST}, [HOST, "other.internal:5000"]))

    def test_only_complete_literal_host_authorities_are_accepted(self):
        self.assertEqual(normalize_managed_registry_hosts([HOST, "Registry.Internal:5000", HOST, "[::1]:5000"]),
                         [HOST, "[::1]:5000", "registry.internal:5000"])
        for value in ("", " ", ":", "host:", "host:zero", "host:0", "host:65536", "host/", "http://host", "host?", "host#", "user@host", "*.internal", "10.0.0.0/8", "999.999.999.999", "[::1]garbage", "[::1]foo:80", "host\n"):
            with self.subTest(value=value), self.assertRaises(LumaError):
                normalize_managed_registry_hosts([value])

    def test_agent_dispatch_and_capability_share_the_runtime_owner(self):
        for platform in ("linux", "darwin"):
            self.assertIn("managed-registry-access-v1", node_agent_capabilities(platform))
        expected = {"verified": True, "registries": [HOST]}
        with patch("luma.agent.ensure_managed_registry_access", return_value=expected) as ensure:
            result = execute_agent_task({"action": "managed-registry-access", "payload": {"registries": [HOST], "checkOnly": True}})
        self.assertEqual(result, expected)
        ensure.assert_called_once_with(registries=[HOST], check_only=True)

    def test_unsupported_platform_and_invalid_check_only_do_not_execute(self):
        executor = Mock()
        for args in ({"os_name": "windows"}, {"os_name": "linux", "check_only": "false"}):
            with self.subTest(args=args), self.assertRaises(LumaError):
                ensure_managed_registry_access([HOST], executor=executor, **args)
        executor.sudo.assert_not_called()

    def test_linux_owner_requires_verified_readback(self):
        executor = Mock()
        executor.sudo.return_value = 'LUMA_MANAGED_REGISTRY_RESULT=' + json.dumps({"verified": True, "registries": [HOST], "changed": False, "dockerRestarted": False})
        result = ensure_managed_registry_access([HOST], executor=executor, os_name="linux", check_only=True)
        self.assertTrue(result["verified"])
        self.assertIn("true <<'LUMA_REGISTRY_PY'", executor.sudo.call_args.args[0])
        executor.sudo.return_value = 'LUMA_MANAGED_REGISTRY_RESULT=' + json.dumps({"verified": True, "registries": ["other:5000"]})
        with self.assertRaisesRegex(LumaError, "invalid verification"):
            ensure_managed_registry_access([HOST], executor=executor, os_name="linux")


class OrbStackRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luma-orb-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / ".orbstack/config"
        self.directory.mkdir(parents=True)
        self.config = self.directory / "docker.json"
        self.config.write_text("{}\n")
        self.socket = self.root / ".orbstack/run/docker.sock"
        self.socket.parent.mkdir()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.bind(str(self.socket))
        self.addCleanup(connection.close)
        self.owner = SimpleNamespace(pw_dir=str(self.root), pw_uid=os.geteuid(), pw_name="orb-owner")
        self.orbctl = self.root / "orbctl"
        self.orbctl.write_text("fixture")
        self.info = {"OperatingSystem": "OrbStack", "RegistryConfig": {"IndexConfigs": {}}, "HTTPProxy": "", "HTTPSProxy": "", "NoProxy": ""}
        self.commands = []
        self.containers = ""
        self.ps_outputs = []
        self.activate = True
        self.restart_error = False
        self.ps_hook = None
        self.info_failures = 0
        for target, value in (("DOCKER_SOCKET", self.socket), ("ORBCTL", self.orbctl)):
            patcher = patch.object(orb, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(orb.pwd, "getpwuid", return_value=self.owner)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(orb, "_run", side_effect=self.run_command)
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(orb, "_probe")
        self.probe = patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "docker":
            self.assertEqual(command[1:3], ["--host", f"unix://{self.socket}"])
            if command[3] == "info":
                if self.info_failures:
                    self.info_failures -= 1
                    raise RuntimeError("engine reconnecting")
                return json.dumps(self.info)
            if command[3] == "ps":
                if self.ps_hook:
                    self.ps_hook()
                return self.ps_outputs.pop(0) if self.ps_outputs else self.containers
        elif command == [str(self.orbctl), "restart", "docker"]:
            if self.restart_error:
                raise RuntimeError("OrbStack Docker restart failed")
            if self.activate:
                self.info["RegistryConfig"]["IndexConfigs"][HOST] = {"Secure": False}
            return "restarted"
        raise AssertionError(command)

    def apply(self, **kwargs):
        return orb._apply([HOST], docker="docker", expected_socket=self.socket, **kwargs)

    def set_ready(self):
        self.config.write_text(json.dumps({"insecure-registries": [HOST]}))
        self.info["RegistryConfig"]["IndexConfigs"][HOST] = {"Secure": False}

    def test_owner_socket_is_the_source_of_configuration_identity(self):
        owner, selected = orb._socket_owner()
        self.assertEqual(owner.pw_name, "orb-owner")
        self.assertEqual(selected, self.socket)
        with patch.object(orb, "DOCKER_SOCKET", self.config), self.assertRaisesRegex(RuntimeError, "socket"):
            orb._socket_owner()
        with patch.object(orb.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.root), pw_uid=0)), self.assertRaisesRegex(RuntimeError, "user-owned"):
            orb._socket_owner()

    def test_preserves_user_settings_and_applies_to_owner_runtime(self):
        original = {"log-driver": "local", "insecure-registries": ["old.internal:5000"], "features": {"containerd-snapshotter": True}}
        self.config.write_text(json.dumps(original))
        self.config.chmod(0o600)
        result = self.apply()
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved, {**original, "insecure-registries": ["old.internal:5000", HOST]})
        self.assertEqual(json.loads(self.config.with_name("docker.json.luma-registry.bak").read_text()), original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue(result["verified"])
        self.assertTrue(result["dockerRestarted"])
        self.assertEqual(result["registries"], [HOST])
        self.assertEqual(self.commands.count([str(self.orbctl), "restart", "docker"]), 1)
        self.probe.assert_called_once_with([HOST])

    def test_live_and_persisted_ready_is_noop_even_with_running_containers(self):
        self.set_ready()
        self.containers = "production-container"
        original = self.config.read_bytes()
        result = self.apply()
        self.assertFalse(result["changed"])
        self.assertFalse(result["dockerRestarted"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(any("ps" in command or "restart" in command for command in self.commands))

    def test_busy_missing_configuration_is_not_written_or_restarted(self):
        self.containers = "production-container"
        original = self.config.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "maintenance window"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.config.with_name("docker.json.luma-registry.bak").exists())
        self.probe.assert_not_called()

    def test_busy_saved_but_not_activated_configuration_is_not_ready(self):
        self.config.write_text(json.dumps({"insecure-registries": [HOST]}))
        self.containers = "production-container"
        with self.assertRaisesRegex(RuntimeError, "maintenance window"):
            self.apply()
        self.assertFalse(any("restart" in command for command in self.commands))

    def test_read_only_mode_checks_live_runtime_without_opening_settings(self):
        self.set_ready()
        self.config.unlink()
        self.directory.rmdir()
        self.containers = "production-container"
        result = self.apply(check_only=True)
        self.assertFalse(result["changed"])
        self.assertFalse(self.directory.exists())
        self.info["RegistryConfig"]["IndexConfigs"] = {}
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            self.apply(check_only=True)
        self.assertFalse(self.directory.exists())

    def test_unknown_runtime_or_changed_socket_is_rejected(self):
        self.info["OperatingSystem"] = "Docker Desktop"
        with self.assertRaisesRegex(RuntimeError, "not OrbStack"):
            self.apply()
        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            orb._apply([HOST], docker="docker", expected_socket=self.root / "wrong.sock")
        self.assertEqual(self.config.read_text(), "{}\n")

    def test_bad_json_and_ambiguous_objects_are_preserved(self):
        for content in ("not json", "[]", '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1,"x":2}', '{"insecure-registries":"host"}'):
            with self.subTest(content=content):
                self.config.write_text(content)
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.config.read_text(), content)
        self.assertFalse(any("restart" in command for command in self.commands))

    def test_symlink_and_special_file_settings_are_rejected_before_reading(self):
        self.config.unlink()
        other = self.root / "other.json"
        other.write_text("{}")
        self.config.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, "symbolic"):
            self.apply()
        self.config.unlink()
        os.mkfifo(self.config)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.apply()

    def test_restart_failure_and_unactivated_readback_never_return_ready(self):
        self.restart_error = True
        with self.assertRaisesRegex(RuntimeError, "restart failed"):
            self.apply()
        self.probe.assert_not_called()
        self.restart_error = False
        self.activate = False
        with patch.object(orb.time, "monotonic", side_effect=[0, 46]), self.assertRaisesRegex(RuntimeError, "readiness is unverified"):
            self.apply()
        self.probe.assert_not_called()

    def test_reconnect_waits_for_the_engine_after_restart(self):
        original_run = self.run_command
        def run(command, **kwargs):
            output = original_run(command, **kwargs)
            if command == [str(self.orbctl), "restart", "docker"]:
                self.info_failures = 1
            return output
        self.run.side_effect = run
        with patch.object(orb.time, "sleep") as sleep:
            result = self.apply()
        self.assertTrue(result["verified"])
        sleep.assert_called_once_with(0.5)

    def test_container_starting_during_apply_aborts_restart_and_restores_own_candidate(self):
        original = self.config.read_bytes()
        self.ps_outputs = ["", "new-container"]
        with self.assertRaisesRegex(RuntimeError, "maintenance window"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(any("restart" in command for command in self.commands))

    def test_concurrent_operator_edit_is_preserved_when_second_idle_check_fails(self):
        self.ps_outputs = ["", "new-container"]
        def edit():
            if len(self.ps_outputs) == 1:
                self.config.write_text('{"operator":"new value"}')
        self.ps_hook = edit
        with self.assertRaisesRegex(RuntimeError, "maintenance window"):
            self.apply()
        self.assertEqual(json.loads(self.config.read_text()), {"operator": "new value"})

    def test_parallel_luma_setup_is_rejected_by_owner_lock(self):
        with orb._settings_lock(self.directory, self.owner.pw_uid), self.assertRaisesRegex(RuntimeError, "in progress"):
            self.apply()
        self.assertEqual(self.config.read_text(), "{}\n")

    def test_proxy_without_runtime_bypass_fails_without_changing_preferences(self):
        self.set_ready()
        self.info["HTTPSProxy"] = "http://user:secret@proxy.example.com:7890"
        before = self.config.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "proxy exclusions") as error:
            self.apply()
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.config.read_bytes(), before)
        self.info["NoProxy"] = "localhost,100.64.0.0/10"
        self.assertTrue(self.apply()["verified"])

    def test_root_launcher_drops_to_socket_owner_and_uses_isolated_system_python(self):
        result = {"verified": True, "registries": [HOST], "changed": False, "dockerRestarted": False}
        process = Mock(returncode=0)
        process.communicate.return_value = (orb.RESULT_PREFIX + json.dumps(result), "")
        with patch.object(orb.os, "geteuid", return_value=0), patch.object(orb.subprocess, "Popen", return_value=process) as launch:
            self.assertEqual(orb.ensure_orbstack_registries([HOST], docker="docker", check_only=True), result)
        command = launch.call_args.args[0]
        self.assertEqual(command[:8], ["/usr/bin/sudo", "-n", "-H", "-u", "orb-owner", "--", "/usr/bin/python3", "-I"])
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual(launch.call_args.kwargs["env"]["HOME"], str(self.root))
        self.assertNotIn("/var/root", command[:9])
        request = json.loads(process.communicate.call_args.args[0])
        self.assertEqual(request["socket"], str(self.socket))
        self.assertIs(request["checkOnly"], True)
        # The actual isolated child source compiles without loading packages
        # from the privileged installation or requiring another process.
        compile(command[-1], '<orb-owner-child>', 'exec')

    def test_owner_subprocess_timeout_kills_the_entire_process_group(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired("setup", 300), ("", "")]
        with patch.object(orb.subprocess, "Popen", return_value=process), patch.object(orb.os, "killpg") as kill, self.assertRaisesRegex(RuntimeError, "verify the Docker engine"):
            orb.ensure_orbstack_registries([HOST], docker="docker")
        kill.assert_called_once_with(12345, orb.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)


class OrbStackProbeTests(unittest.TestCase):
    def test_probe_uses_exact_http_without_environment_proxy_or_redirects(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(orb.urllib.request, "build_opener", return_value=opener) as build:
            orb._probe([HOST])
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], orb._NoRedirect)
        opener.open.assert_called_once_with("http://" + HOST + "/v2/", timeout=15)
        for status in (401, 302, 503):
            opener.open.side_effect = urllib.error.HTTPError("http://" + HOST, status, "failure", {}, None)
            with patch.object(orb.urllib.request, "build_opener", return_value=opener):
                if status == 401:
                    orb._probe([HOST])
                else:
                    with self.assertRaisesRegex(RuntimeError, "probe failed"):
                        orb._probe([HOST])


if __name__ == "__main__":
    unittest.main()
