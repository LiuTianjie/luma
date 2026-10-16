from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from luma.config import load_config
from luma.control import server
from luma.control.state import init_state, load_state, save_state
from luma.errors import LumaError

HOST = '100.64.0.70:5000'
CAPABILITY = 'managed-registry-access-v2'
LEGACY_CAPABILITY = 'managed-registry-access-v1'


def registered(name, region='home', *, manager=False, capabilities=None):
    return {'name': name, 'region': region, 'nodeId': 'id-' + name, 'nomadStatus': 'ready',
            'roles': ['nomad-manager'] if manager else [], 'tailscaleIP': '100.64.0.9',
            'agent': {'status': 'ready', 'os': 'darwin', 'arch': 'arm64',
                      'capabilities': capabilities if capabilities is not None else [CAPABILITY, 'docker-image', 'nomad-join-registry-v1']}}


def nomad_node(name, region='home'):
    return {'ID': 'id-' + name, 'Name': name, 'Datacenter': 'dc1', 'NodePool': 'default',
            'Status': 'ready', 'SchedulingEligibility': 'eligible', 'Drain': False,
            'Meta': {'luma_node_name': name, 'region': region, 'role': 'worker'},
            'Attributes': {'kernel.name': 'darwin'},
            'Drivers': {'docker': {'Detected': True, 'Healthy': True}}}


def docker_job(*, name='worker', image=None, region='home', pin=''):
    constraints = [{'LTarget': '${meta.region}', 'RTarget': region, 'Operand': '='}]
    if pin:
        constraints.append({'LTarget': '${meta.luma_node_name}', 'RTarget': pin, 'Operand': '='})
    return json.dumps({'Job': {'ID': name, 'Datacenters': ['dc1'], 'Constraints': constraints,
                              'TaskGroups': [{'Name': name, 'Count': 1, 'Tasks': [
                                  {'Name': name, 'Driver': 'docker', 'Config': {'image': image or HOST + '/app:1'}}]}]}})


def receipt(registries=None, **extra):
    return {'registries': registries or [HOST], 'verified': True, 'runtime': 'orbstack',
            'changed': False, 'dockerRestarted': False, 'affectedAllocationIds': [],
            'message': 'verified without changes', **extra}


class ManagedRegistryControlTests(unittest.TestCase):
    def setUp(self):
        self.state = {'managedRegistryTransports': {HOST: 'http'},
                      'nodes': {name: registered(name) for name in ('air1', 'air2')}}
        self.live = [nomad_node('air1'), nomad_node('air2'), nomad_node('elsewhere', 'cn')]
        self.config = type('Config', (), {'defaults': {}})()
        runtime = patch.object(server, 'load_runtime_state', side_effect=lambda: self.state)
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_registry_preflight_uses_fresh_heartbeat_after_slow_image_cache(self):
        captured = copy.deepcopy(self.state)
        captured['nodes']['air1']['agent'].update(status='online', lastSeen=1000)
        now = 1000 + server.AGENT_STALE_SECONDS + 1
        self.state['nodes']['air1']['agent'].update(status='online', lastSeen=now)
        with patch.object(server.time, 'time', return_value=now), patch.object(
            server, '_run_node_agent_task', return_value=receipt()
        ) as task:
            result = server._ensure_nodes_managed_registry_access(captured, {'air1': {HOST}})
        self.assertEqual(result['nodes'][0]['node'], 'air1')
        self.assertEqual(task.call_args.args[0]['nodes']['air1']['agent']['lastSeen'], now)
        self.assertEqual(captured['nodes']['air1']['agent']['lastSeen'], 1000)

    def test_registry_preflight_rejects_targets_that_lost_readiness_or_capability(self):
        captured = copy.deepcopy(self.state)
        for nodes in (
            {},
            {'air1': registered('air1', capabilities=[LEGACY_CAPABILITY])},
            {'air1': {**registered('air1'), 'agent': {'status': 'offline', 'capabilities': [CAPABILITY]}}},
        ):
            with self.subTest(nodes=nodes), patch.object(server, 'load_runtime_state', return_value={'nodes': nodes}), patch.object(server, '_run_node_agent_task') as task:
                with self.assertRaises(LumaError):
                    server._ensure_nodes_managed_registry_access(captured, {'air1': {HOST}})
                task.assert_not_called()

    def api(self, method, path, *args, **kwargs):
        self.assertEqual(method, 'GET')
        if path == '/v1/nodes':
            return self.live
        return next(node for node in self.live if path == '/v1/node/' + node['ID'])

    def test_darwin_docker_target_uses_only_final_job_pin(self):
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task', return_value=receipt()) as task:
            result = server._ensure_job_managed_registry_access(self.config, self.state, docker_job(pin='air2'))
        self.assertEqual(task.call_count, 1)
        self.assertEqual(task.call_args.args[1:4], ('air2', 'managed-registry-access', {'registries': [HOST], 'checkOnly': False}))
        self.assertEqual(task.call_args.kwargs['required_capability'], CAPABILITY)
        self.assertEqual(result['nodes'][0]['node'], 'air2')

    def test_every_candidate_capability_is_checked_before_first_mutation(self):
        self.state['nodes']['air2']['agent']['capabilities'] = ['docker-image']
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task') as task:
            with self.assertRaisesRegex(LumaError, 'air2.*upgrade'):
                server._ensure_job_managed_registry_access(self.config, self.state, docker_job())
            task.assert_not_called()

    def test_v1_only_receipts_are_rejected_for_workers_and_managers(self):
        for manager in (False, True):
            self.state['nodes']['air1'] = registered('air1', manager=manager, capabilities=[LEGACY_CAPABILITY, 'docker-image'])
            # A version string is not the capability contract.
            self.state['nodes']['air1']['agent']['version'] = '999.0.0'
            with self.subTest(manager=manager), patch.object(server, '_run_node_agent_task', return_value=receipt()) as task:
                with self.assertRaisesRegex(LumaError, CAPABILITY + '.*upgrade'):
                    server._ensure_nodes_managed_registry_access(self.state, {'air1': {HOST}})
                task.assert_not_called()

    def test_region_only_deploy_discovers_air3_and_checks_the_whole_pool_first(self):
        for name in ('air1', 'air2'):
            self.state['nodes'][name]['region'] = 'render'
        self.live = [nomad_node('air1', 'render'), nomad_node('air2', 'render'), nomad_node('air3', 'render'), nomad_node('unrelated-old', 'cn')]
        self.state['nodes']['air3'] = registered('air3', 'render', capabilities=[LEGACY_CAPABILITY, 'docker-image'])
        self.state['nodes']['unrelated-old'] = registered('unrelated-old', 'cn', capabilities=[LEGACY_CAPABILITY])
        job = docker_job(region='render')
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task', return_value=receipt()) as task:
            with self.assertRaisesRegex(LumaError, 'air3.*' + CAPABILITY):
                server._ensure_job_managed_registry_access(self.config, self.state, job)
            task.assert_not_called()
            self.state['nodes']['air3']['agent']['capabilities'].append(CAPABILITY)
            self.state['nodes']['air3']['agent']['version'] = '0.0.0'
            result = server._ensure_job_managed_registry_access(self.config, self.state, job)
            self.assertEqual([call.args[1] for call in task.call_args_list], ['air1', 'air2', 'air3'])
            self.assertEqual([row['node'] for row in result['nodes']], ['air1', 'air2', 'air3'])
            # The next deployment discovers another ready regional node rather
            # than retaining a fixed inventory of the original Air machines.
            task.reset_mock()
            self.live.append(nomad_node('future-render-worker', 'render'))
            self.state['nodes']['future-render-worker'] = registered('future-render-worker', 'render')
            server._ensure_job_managed_registry_access(self.config, self.state, job)
            self.assertEqual([call.args[1] for call in task.call_args_list], ['air1', 'air2', 'air3', 'future-render-worker'])
            self.assertNotIn('unrelated-old', [call.args[1] for call in task.call_args_list])

    def test_new_agent_keeps_published_v1_action_compatibility_with_one_owner(self):
        from luma.agent import execute_agent_task, node_agent_capabilities

        for platform in ('linux', 'darwin'):
            capabilities = node_agent_capabilities(platform)
            self.assertIn(LEGACY_CAPABILITY, capabilities)
            self.assertIn(CAPABILITY, capabilities)
        expected = receipt()
        with patch('luma.agent.ensure_managed_registry_access', return_value=expected) as owner:
            result = execute_agent_task({'action': 'managed-registry-access', 'payload': {'registries': [HOST], 'checkOnly': True}})
            self.assertEqual(result, expected)
            owner.assert_called_once_with(registries=[HOST], check_only=True)
        with patch('luma.agent.ensure_managed_registry_access', return_value=expected) as owner:
            result = execute_agent_task({'action': 'configure-insecure-registry', 'payload': {'registry': HOST}})
            self.assertEqual(result, {**expected, 'registry': HOST})
            owner.assert_called_once_with(registries=[HOST])

    def test_manager_is_verified_read_only_when_it_is_a_real_target(self):
        self.state['nodes']['air1']['roles'] = ['nomad-manager']
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task', return_value=receipt()) as task:
            server._ensure_job_managed_registry_access(self.config, self.state, docker_job(pin='air1'))
        self.assertEqual(task.call_args.args[3], {'registries': [HOST], 'checkOnly': True})

    def test_manager_cannot_report_configuration_change_from_read_only_check(self):
        self.state['nodes']['air1']['roles'] = ['nomad-manager']
        with patch.object(server, '_run_node_agent_task', return_value=receipt(changed=True)):
            with self.assertRaisesRegex(LumaError, 'manager.*must not change'):
                server._ensure_nodes_managed_registry_access(self.state, {'air1': {HOST}})

    def test_busy_or_unknown_runtime_stops_without_fallback_and_preserves_progress(self):
        for failure in ('Docker is busy; maintenance required', 'Docker runtime is unknown'):
            events = []
            with self.subTest(failure=failure), patch.object(server, '_run_node_agent_task', side_effect=[receipt(), LumaError(failure)]) as task:
                with self.assertRaisesRegex(LumaError, 'air2; already verified: air1'):
                    server._ensure_nodes_managed_registry_access(self.state, {'air1': {HOST}, 'air2': {HOST}}, progress=events.append)
            self.assertEqual(task.call_count, 2)
            self.assertEqual([event['status'] for event in events], ['start', 'ok', 'start', 'fail'])
            self.assertIn(failure, events[-1]['message'])

    def test_missing_readback_or_wrong_host_never_counts_as_success(self):
        for response in ({'registries': [HOST]}, {'registries': [HOST], 'verified': True}, receipt(['other.internal:5000']), receipt(verified='true'), receipt(changed='false')):
            with self.subTest(response=response), patch.object(server, '_run_node_agent_task', return_value=response):
                with self.assertRaisesRegex(LumaError, 'did not confirm'):
                    server._ensure_nodes_managed_registry_access(self.state, {'air1': {HOST}})

    def test_public_or_unknown_private_images_do_not_trigger_downgrade(self):
        for image in ('ghcr.io/acme/app:1', 'registry.example.com/app:1', '10.1.2.3:5000/app:1'):
            with self.subTest(image=image), patch.object(server.NomadApi, 'request') as api, patch.object(server, '_run_node_agent_task') as task:
                result = server._ensure_job_managed_registry_access(self.config, self.state, docker_job(image=image))
                self.assertEqual(result['nodes'], [])
                api.assert_not_called()
                task.assert_not_called()
        with patch.object(server, '_run_node_agent_task') as task:
            with self.assertRaisesRegex(LumaError, 'only Luma-managed'):
                server._ensure_nodes_managed_registry_access(self.state, {'air1': {'ghcr.io'}})
            task.assert_not_called()

    def test_https_managed_transport_is_never_downgraded(self):
        self.state['managedRegistryTransports'][HOST] = 'https'
        with patch.object(server.NomadApi, 'request') as api, patch.object(server, '_run_node_agent_task') as task:
            server._ensure_job_managed_registry_access(self.config, self.state, docker_job())
            api.assert_not_called()
            task.assert_not_called()

    def test_unregistered_scheduler_candidate_is_not_silently_skipped(self):
        self.live.append(nomad_node('unregistered'))
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task') as task:
            with self.assertRaisesRegex(LumaError, 'unregistered Nomad node'):
                server._ensure_job_managed_registry_access(self.config, self.state, docker_job())
            task.assert_not_called()

    def test_stale_nomad_identity_cannot_configure_a_new_same_named_node(self):
        self.live[0]['ID'] = 'stale-id-air1'
        self.state['nodes']['air1']['agent']['knownNodeIds'] = ['stale-id-air1']
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task') as task:
            with self.assertRaisesRegex(LumaError, 'identity mismatch'):
                server._ensure_job_managed_registry_access(self.config, self.state, docker_job(pin='air1'))
            task.assert_not_called()

    def test_unknown_docker_driver_fails_before_any_node_mutation(self):
        self.live[0]['Drivers'] = {}
        with patch.object(server.NomadApi, 'request', side_effect=self.api), patch.object(server, '_run_node_agent_task') as task:
            with self.assertRaisesRegex(LumaError, 'no ready Docker target'):
                server._ensure_job_managed_registry_access(self.config, self.state, docker_job(pin='air1'))
            task.assert_not_called()


class ManagedRegistryDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config_path = self.root / 'luma.yaml'
        config_path.write_text(yaml.safe_dump({'defaults': {'stackRoot': str(self.root / 'stacks'), 'routesRoot': str(self.root / 'routes')}}))
        env = patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': str(self.root / 'state'), 'LUMA_CONTROL_CONFIG': str(config_path)})
        env.start()
        self.addCleanup(env.stop)
        self.state = init_state(domain='luma.example.com', cluster_id='registry-tests', overwrite=True)
        self.state.update(managedRegistryTransports={HOST: 'http'}, nodes={'air1': registered('air1'), 'air2': registered('air2')})
        self.state['nomadRpcAddr'] = '100.64.0.1:4647'
        save_state(self.state)
        self.token = self.state['deployToken']
        self.config = load_config(config_path)

    def test_service_and_compose_check_resolved_image_before_nomad_submission(self):
        for kind in ('service', 'compose'):
            with self.subTest(kind=kind):
                calls = []
                def cached_service(config, service, **kwargs):
                    calls.append('cache')
                    return replace(service, image=HOST + '/app:immutable'), {}
                def cached_compose(config, state, deployment, **kwargs):
                    calls.append('cache')
                    compose = copy.deepcopy(deployment.compose)
                    compose['services']['worker']['image'] = HOST + '/app:immutable'
                    return replace(deployment, compose=compose), {}
                def task(_state, node, action, payload, **kwargs):
                    calls.append('registry')
                    self.assertEqual(node, 'air1')
                    self.assertEqual(payload['registries'], [HOST])
                    return receipt()
                def api(method, path, *args, **kwargs):
                    node = nomad_node('air1')
                    return [node] if path == '/v1/nodes' else node
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(server, 'resolve_service_image', side_effect=cached_service))
                    stack.enter_context(patch.object(server, '_cache_compose_images_on_builder', side_effect=cached_compose))
                    stack.enter_context(patch.object(server.NomadApi, 'request', side_effect=api))
                    stack.enter_context(patch.object(server, '_run_node_agent_task', side_effect=task))
                    deploy = stack.enter_context(patch.object(server, 'deploy_to_nomad', side_effect=lambda *args, **kwargs: calls.append('deploy') or 'Nomad ready'))
                    stack.enter_context(patch.object(server, '_refresh_nomad_cni_hostports_for_job', return_value={}))
                    if kind == 'service':
                        body = {'manifest': 'name: registry-test-service\nimage: ghcr.io/acme/app:1\nregion: home\nnode: air1\nexposure: none\n', 'skipDns': True}
                        result = server.handle_deployment(self.token, body)
                    else:
                        body = {'manifest': 'name: registry-test-compose\ncompose: docker-compose.yml\nregion: home\nservices:\n  worker:\n    node: air1\n    exposure: none\n',
                                'composeContent': 'services:\n  worker:\n    image: ghcr.io/acme/app:1\n', 'skipDns': True}
                        result = server.handle_compose_deployment(self.token, body)
                    self.assertEqual(calls, ['cache', 'registry', 'deploy'])
                    self.assertIn(HOST + '/app:immutable', deploy.call_args.args[1])
                    self.assertTrue(any(step['name'] == 'Verify managed registry access' and step['status'] == 'ok' for step in result['steps']))

    def test_registry_failure_prevents_nomad_submission(self):
        body = {'manifest': 'name: blocked-app\nimage: ghcr.io/acme/app:1\nregion: home\nnode: air1\nexposure: none\n', 'skipDns': True}
        with patch.object(server, 'resolve_service_image', side_effect=lambda _config, service, **kwargs: (replace(service, image=HOST + '/app:1'), {})), patch.object(
            server, '_ensure_job_managed_registry_access', side_effect=LumaError('Docker busy; maintenance required')
        ), patch.object(server, 'deploy_to_nomad') as deploy:
            with self.assertRaisesRegex(LumaError, 'maintenance required'):
                server.handle_deployment(self.token, body)
            deploy.assert_not_called()
        self.assertEqual(server._service_deployment_record(load_state(), 'blocked-app')['status'], 'failed_partial')

    def test_remote_join_checks_both_capabilities_before_configuring(self):
        for capabilities in ([CAPABILITY], ['nomad-join-registry-v1'], ['nomad-join-registry-v1', LEGACY_CAPABILITY]):
            state = load_state()
            state['nodes']['air1']['agent']['capabilities'] = capabilities
            save_state(state)
            with self.subTest(capabilities=capabilities), patch.object(server, '_run_node_agent_task') as task:
                with self.assertRaisesRegex(LumaError, 'upgrad'):
                    server.handle_node_nomad_join(self.token, {'nodeName': 'air1'})
                task.assert_not_called()

    def test_v2_task_cannot_be_leased_after_agent_falls_back_to_v1(self):
        for action in ('managed-registry-access', 'join-nomad'):
            with self.subTest(action=action):
                state = load_state()
                state['nodes']['air1']['agent']['capabilities'] = [CAPABILITY, LEGACY_CAPABILITY, 'nomad-join-registry-v1']
                token = server._issue_node_agent_token(state, 'air1', node_id='id-air1')
                save_state(state)
                task_id = server._queue_node_agent_task(state, 'air1', action, {'registries': [HOST]}, required_capability=CAPABILITY)
                stored = load_state()['agentTasks'][task_id]
                self.assertEqual(stored['requiredCapability'], CAPABILITY)
                with patch.object(server, '_record_metrics_history'):
                    leased = server.handle_node_agent_lease(token, {'nodeName': 'air1', 'nodeId': 'id-air1',
                        'capabilities': [LEGACY_CAPABILITY, 'nomad-join-registry-v1'], 'os': 'linux', 'waitSeconds': 0})
                self.assertIsNone(leased['task'])
                self.assertEqual(load_state()['agentTasks'][task_id]['status'], 'queued')

    def test_fresh_remote_join_installs_runtime_before_registry_probe_and_nomad_start(self):
        from luma.agent import execute_agent_task

        state = load_state()
        state['nodes']['air1']['agent']['os'] = 'linux'
        save_state(state)
        installed = False
        connected = False
        phases = []
        actions = []
        executor = Mock()

        def install_docker(_executor):
            nonlocal installed
            self.assertFalse(installed)
            installed = True
            phases.append('install-docker')
            return 'Docker installed'

        def setup_tailscale(*args, **kwargs):
            nonlocal connected
            self.assertTrue(installed)
            connected = True
            phases.append('connect-tailscale')
            return 'Tailscale ready'

        def runtime_owner(registries, **kwargs):
            if not installed:
                raise LumaError('Docker is absent on this fresh node')
            self.assertTrue(connected)
            self.assertEqual(registries, [HOST])
            phases.append('verify-registry')
            return receipt(runtime='linux', changed=True, dockerRestarted=True)

        def sudo(command):
            if 'systemctl restart nomad' in command:
                self.assertIn('verify-registry', phases)
                phases.append('start-nomad')
            return ''

        def run_agent(_state, _node, action, payload, **kwargs):
            actions.append(action)
            return execute_agent_task({'action': action, 'payload': payload})

        executor.sudo.side_effect = sudo
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(server, '_run_node_agent_task', side_effect=run_agent))
            stack.enter_context(patch('luma.agent.LocalExecutor', return_value=executor))
            stack.enter_context(patch('luma.agent._docker_binary', side_effect=lambda: '/usr/bin/docker' if installed else ''))
            stack.enter_context(patch('luma.agent.node_agent_os', return_value='linux'))
            stack.enter_context(patch('luma.bootstrap.LocalExecutor', return_value=executor))
            stack.enter_context(patch('luma.bootstrap.install_docker', side_effect=install_docker))
            stack.enter_context(patch('luma.bootstrap.setup_tailscale', side_effect=setup_tailscale))
            stack.enter_context(patch('luma.bootstrap._tailscale_ip', side_effect=lambda _executor: '100.64.0.2' if connected else ''))
            stack.enter_context(patch('luma.nomad_node.detect_os', return_value='linux'))
            stack.enter_context(patch('luma.nomad_node.detect_cpu_total_compute', return_value=None))
            stack.enter_context(patch('luma.registry_access.ensure_managed_registry_access', side_effect=runtime_owner))
            stack.enter_context(patch('luma.bootstrap.verify_local_nomad_node', return_value='Nomad ready'))
            stack.enter_context(patch('luma.bootstrap.local_nomad_node_info', return_value=('air1', 'new-nomad-air1')))
            result = server.handle_node_nomad_join(self.token, {'nodeName': 'air1'})
        self.assertEqual(actions, ['join-nomad'])
        self.assertEqual(phases, ['install-docker', 'connect-tailscale', 'verify-registry', 'start-nomad'])
        self.assertEqual(result['nodeId'], 'new-nomad-air1')

    def test_skipped_orchestrator_does_not_mutate_registry_configuration(self):
        body = {'manifest': 'name: no-submit-app\nimage: ' + HOST + '/app:1\nregion: home\nnode: air1\nexposure: none\n', 'skipDns': True, 'skipOrchestrator': True}
        with patch.object(server, 'resolve_service_image', side_effect=lambda _config, service, **kwargs: (service, {})), patch.object(server, '_ensure_job_managed_registry_access') as preflight:
            server.handle_deployment(self.token, body)
            preflight.assert_not_called()


if __name__ == '__main__':
    unittest.main()
