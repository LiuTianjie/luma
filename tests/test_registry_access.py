from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from luma.errors import LumaError
from luma.registry_access import REGISTRY_SETUP_SCRIPT, configure_join_registries, join_insecure_registries

HOST = "100.66.177.70:5000"


class RegistryPolicyTests(unittest.TestCase):
    def test_legacy_builder_is_discovered_but_not_local_push_host(self):
        state = {"build": {"registryHost": HOST, "pushHost": "localhost:5000"},
                 "nodes": {"builder": {"tailscaleIP": "100.66.177.70"}}}
        self.assertEqual(join_insecure_registries(state), [HOST])

    def test_no_downgrade_for_unknown_or_tls_registry(self):
        for host in ["registry.example.com", "10.0.0.1:5000", "100.66.177.70:5443", HOST]:
            self.assertEqual(join_insecure_registries({"build": {"registryHost": host}}), [])
        state = {"build": {"registryHost": HOST}, "nodes": {"builder": {"tailscaleIP": "100.66.177.70"}},
                 "managedRegistryTransports": {HOST: "https"}}
        self.assertEqual(join_insecure_registries(state), [])
        state.pop("managedRegistryTransports")
        state["registries"] = {HOST: {"username": "u"}}
        self.assertEqual(join_insecure_registries(state), [])

    def test_explicit_managed_http_ports(self):
        self.assertEqual(join_insecure_registries({"managedRegistryTransports": {
            "registry.internal:5050": "http", "secure.example.com": "https"}}), ["registry.internal:5050"])

    def test_configuration_failure_is_not_swallowed(self):
        executor = Mock()
        executor.sudo.side_effect = LumaError("restart failed")
        with self.assertRaisesRegex(LumaError, "node must not join"):
            configure_join_registries([HOST], executor=executor, os_name="linux")

    def test_mac_exec_nodes_do_not_rewrite_linux_docker_config(self):
        executor = Mock()
        configure_join_registries([HOST], executor=executor, os_name="darwin")
        executor.sudo.assert_not_called()


class RegistrySetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "daemon.json"
        self.dropin = self.root / "no-proxy.conf"
        self.calls = []
        self.containers = ""
        self.probe_status = 200
        self.initial = {"RegistryConfig": {"IndexConfigs": {}}, "NoProxy": "localhost,custom.internal"}
        self.current = self.initial
        self.script = REGISTRY_SETUP_SCRIPT.replace("/etc/docker/daemon.json", str(self.config)).replace(
            "/etc/systemd/system/docker.service.d/zz-luma-registry-no-proxy.conf", str(self.dropin))

    def run_script(self):
        def check_output(args, **kwargs):
            self.calls.append(args)
            if args[:2] == ('docker', 'info'):
                return json.dumps(self.current)
            if args[:2] == ('docker', 'ps'):
                return self.containers
            if args == ('systemctl', 'restart', 'docker'):
                config = json.loads(self.config.read_text())
                proxy = self.dropin.read_text().split('NO_PROXY=')[1].split('"')[0]
                self.current = {"RegistryConfig": {"IndexConfigs": {h: {"Secure": False} for h in config['insecure-registries']}}, "NoProxy": proxy}
            return ''
        response = Mock(status=self.probe_status)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        with patch('sys.argv', ['setup', json.dumps([HOST])]), patch('subprocess.check_output', side_effect=check_output), patch(
            'urllib.request.build_opener', return_value=opener):
            exec(compile(self.script, '<registry-setup>', 'exec'), {})
        return opener

    def test_fresh_setup_preserves_settings_and_is_idempotent(self):
        original = {"log-driver": "local", "insecure-registries": ["old.internal:5000"]}
        self.config.write_text(json.dumps(original))
        opener = self.run_script()
        self.assertEqual(json.loads(self.config.read_text())['log-driver'], 'local')
        self.assertEqual(json.loads(self.config.read_text())['insecure-registries'], ['old.internal:5000', HOST])
        self.assertEqual(json.loads(self.config.with_name('daemon.json.luma-registry.bak').read_text()), original)
        self.assertTrue(self.dropin.read_text().startswith('[Service]\n'))
        self.assertIn('custom.internal', self.dropin.read_text())
        opener.open.assert_called_once_with('http://' + HOST + '/v2/', timeout=15)
        self.calls.clear()
        self.containers = 'existing-running-container'
        self.run_script()
        self.assertNotIn(('systemctl', 'restart', 'docker'), self.calls)

    def test_malformed_config_is_not_overwritten(self):
        self.config.write_text('not json')
        with self.assertRaises(json.JSONDecodeError):
            self.run_script()
        self.assertEqual(self.config.read_text(), 'not json')
        self.assertEqual(self.calls, [])

    def test_busy_node_is_not_restarted_or_changed(self):
        self.containers = 'container-1'
        with self.assertRaisesRegex(RuntimeError, 'drain this node'):
            self.run_script()
        self.assertFalse(self.config.exists())
        self.assertFalse(self.dropin.exists())
        self.assertNotIn(('systemctl', 'restart', 'docker'), self.calls)

    def test_daemon_proxy_settings_are_preserved(self):
        self.config.write_text(json.dumps({'proxies': {'http-proxy': 'http://proxy:7890'}}))
        self.run_script()
        config = json.loads(self.config.read_text())
        self.assertEqual(config['proxies']['http-proxy'], 'http://proxy:7890')
        self.assertIn(HOST, config['proxies']['no-proxy'])

    def test_unreachable_registry_fails_setup(self):
        self.probe_status = 503
        with self.assertRaisesRegex(RuntimeError, 'probe failed'):
            self.run_script()


class RegistryJoinIntegrationTests(unittest.TestCase):
    def test_register_returns_policy_for_local_join(self):
        from luma.control.state import init_state, save_state
        from luma.control.server import handle_node_register
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'LUMA_CONTROL_STATE_DIR': tmp + '/state', 'LUMA_CONTROL_CONFIG': tmp + '/luma.yaml'}):
            Path(tmp, 'luma.yaml').write_text('defaults:\n  engine: nomad\n')
            state = init_state(domain='luma.example.com', cluster_id='test', overwrite=True)
            state['nomadRpcAddr'] = '100.64.0.1:4647'
            state['managedRegistryTransports'] = {HOST: 'http'}
            save_state(state)
            result = handle_node_register(state['joinToken'], {'nodeName': 'ppt', 'region': 'cn'})
            self.assertEqual(result['insecureRegistries'], [HOST])

    def test_control_requires_registry_capable_agent(self):
        from luma.control.state import init_state, save_state
        from luma.control.server import handle_node_nomad_join
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'LUMA_CONTROL_STATE_DIR': tmp + '/state', 'LUMA_CONTROL_CONFIG': tmp + '/luma.yaml'}):
            Path(tmp, 'luma.yaml').write_text('defaults:\n  engine: nomad\n')
            state = init_state(domain='luma.example.com', cluster_id='test', overwrite=True)
            state['nomadRpcAddr'] = '100.64.0.1:4647'
            state['managedRegistryTransports'] = {HOST: 'http'}
            state['nodes'] = {'ppt': {'region': 'cn', 'nodeId': 'agent-id'}}
            save_state(state)
            with patch('luma.control.server._run_node_agent_task', return_value={
                'nodeName': 'ppt', 'nodeId': 'nomad-id', 'tailscaleIP': '100.64.0.2'}) as task:
                handle_node_nomad_join(state['deployToken'], {'nodeName': 'ppt'})
            self.assertEqual(task.call_args.args[3]['insecureRegistries'], [HOST])
            self.assertEqual(task.call_args.kwargs['required_capability'], 'nomad-join-registry-v1')

    def test_agent_join_forwards_registry_policy(self):
        from luma.agent import execute_agent_task, node_agent_capabilities
        with patch('luma.bootstrap.install_nomad_node', return_value=[] ) as install, patch(
            'luma.bootstrap.local_nomad_node_info', return_value=('ppt', 'node-id')), patch(
            'luma.bootstrap._tailscale_ip', return_value='100.64.0.2'):
            execute_agent_task({'action': 'join-nomad', 'payload': {
                'nodeName': 'ppt', 'region': 'cn', 'serverAddr': '100.64.0.1:4647', 'insecureRegistries': [HOST]}})
        self.assertEqual(install.call_args.kwargs['insecure_registries'], [HOST])
        self.assertIn('nomad-join-registry-v1', node_agent_capabilities('linux'))

    def test_setup_failure_prevents_nomad_install(self):
        from luma.bootstrap import install_nomad_node
        from luma.config import NodeConfig
        node = NodeConfig(name='ppt', host='ppt', region='cn', roles=['cn'], raw={})
        with patch('luma.bootstrap.LocalExecutor') as executor, patch('luma.bootstrap.install_docker'), patch(
            'luma.bootstrap.setup_tailscale'), patch('luma.nomad_node.detect_os', return_value='linux'), patch(
            'luma.registry_access.configure_join_registries', side_effect=LumaError('registry unreachable')):
            with self.assertRaisesRegex(LumaError, 'registry unreachable'):
                install_nomad_node(node, role='client', region='cn', node_name='ppt', insecure_registries=[HOST])
        executor.return_value.sudo.assert_not_called()

    def test_json_progress_is_flushed(self):
        from luma.cli import _print_json
        stream = Mock()
        _print_json({'type': 'event'}, file=stream)
        stream.flush.assert_called_once()
