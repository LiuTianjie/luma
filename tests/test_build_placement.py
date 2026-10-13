from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luma.build_platform.nomad import Nomad
from luma.build_platform.placement import resolve_platform, selected_nodes, verify_native
from luma.control.build_pool import declared_names, inventory, target_inventory, validate_nodes
from luma.control import server
from luma.errors import LumaError


class NativeBuildPlacement(unittest.TestCase):
    def setUp(self):
        self.state = {'build': {'nodes': ['x86'], 'defaultNode': 'legacy'}, 'nodes': {
            'x86': {'name': 'x86', 'nodePlatform': 'linux/amd64', 'nomadNodeId': 'id-x86', 'region': 'cn'},
            'arm': {'name': 'arm', 'nodePlatform': 'linux/arm64', 'nomadNodeId': 'id-arm', 'region': 'cn'},
            'other': {'name': 'other', 'nodePlatform': 'linux/amd64', 'nomadNodeId': 'id-other', 'roles': ['builder']},
        }}

    def test_only_explicit_enrollment_not_default_or_role(self):
        self.assertEqual(declared_names(self.state), ['x86'])
        self.assertEqual([node['name'] for node in inventory(self.state)], ['x86'])
        self.state['build']['nodes'] = []
        self.assertEqual(inventory(self.state), [])

    def test_offline_target_keeps_architecture_identity(self):
        data = target_inventory(self.state)
        self.assertEqual(resolve_platform({'targetPlatformsByNode': data['byNode']}, node='arm'), 'linux/arm64')

    def test_empty_inventory_does_not_fall_back_to_x86(self):
        with self.assertRaisesRegex(LumaError, 'no architecture fallback'):
            resolve_platform({}, node='arm')
        with self.assertRaises(LumaError):
            resolve_platform({}, region='cn', requested='linux/amd64')

    def test_unknown_member_does_not_silently_shrink_region_platforms(self):
        self.state['nodes']['unknown'] = {'name': 'unknown', 'region': 'cn'}
        data = target_inventory(self.state)
        with self.assertRaises(LumaError):
            resolve_platform({'targetPlatformsByRegion': data['byRegion']}, region='cn')

    def test_manual_node_must_be_enrolled_and_native(self):
        nodes = inventory(self.state)
        self.assertEqual(selected_nodes(nodes, 'linux/amd64', 'x86')[0]['nodeId'], 'id-x86')
        for platform, name in [('linux/arm64', 'x86'), ('linux/amd64', 'other')]:
            with self.assertRaises(LumaError):
                selected_nodes(nodes, platform, name)

    def test_enrollment_rejects_unknown_architecture_and_non_linux(self):
        validate_nodes(self.state, ['arm', 'x86'])
        self.state['nodes']['mac'] = {'name': 'mac', 'nodePlatform': 'darwin/arm64', 'nomadNodeId': 'id-mac'}
        with self.assertRaises(LumaError):
            validate_nodes(self.state, ['mac'])

    def test_executor_rejects_emulation_even_if_container_reports_target_arch(self):
        verify_native('linux/amd64', 'amd64', machine='x86_64', system='Linux')
        verify_native('linux/arm64', 'arm64', machine='aarch64', system='Linux')
        with self.assertRaises(LumaError):
            verify_native('linux/amd64', 'arm64', machine='x86_64', system='Linux')
        with self.assertRaises(LumaError):
            verify_native('linux/arm64', 'arm64', machine='x86_64', system='Linux')

    def test_live_candidates_reject_wrong_architecture_drain_and_small_nodes(self):
        nomad = Nomad(SimpleNamespace(cpu=2000, memory_mb=4096, build_pool='default'))
        node = {'Status': 'ready', 'SchedulingEligibility': 'eligible', 'NodePool': 'default',
                'Attributes': {'kernel.name': 'linux', 'cpu.arch': 'amd64', 'cpu.totalcompute': '8000', 'memory.totalbytes': str(8 * 1024**3)},
                'Drivers': {'docker': {'Detected': True, 'Healthy': True}}}
        for altered in [{}, {'Drain': True}, {'Attributes': {**node['Attributes'], 'cpu.arch': 'arm64'}},
                        {'Attributes': {**node['Attributes'], 'memory.totalbytes': str(2 * 1024**3)}}]:
            nomad._node_cache.clear()
            nomad.request = Mock(return_value={**node, **altered})
            self.assertEqual(nomad.eligible('linux/amd64', inventory(self.state)), [] if altered else ['id-x86'])
            nomad.request.assert_called_once_with('GET', '/v1/node/id-x86', missing_ok=True)

    def test_jobspec_uses_approved_nomad_ids_and_native_architecture(self):
        settings = SimpleNamespace(public_url='https://build', build_pool='default', executor_image='image', cpu=2000, memory_mb=4096, disk_mb=8192, nomad_namespace='builds')
        nomad = Nomad(settings)
        bootstrap = {'endpoint': 'https://build', 'attemptId': 'bld2-aa-a1', 'token': 'test'}
        nomad.request = Mock(side_effect=[None, {'Items': {'bootstrap': json.dumps(bootstrap)}}, None, {}])
        store = Mock()
        store.attempt.return_value = {'id': 'bld2-aa-a1'}
        run = {'id': 'bld2-aa', 'request': {'buildNode': 'x86'}, 'prepared': {'platforms': ['linux/amd64']}}
        nomad.submit(store, run, 1, allowed_nodes=['id-x86', 'id-x86.2'])
        job = nomad.request.call_args.args[2]['Job']
        constraints = {item['LTarget']: item for item in job['Constraints']}
        self.assertEqual(constraints['${attr.cpu.arch}']['RTarget'], 'amd64')
        regex = constraints['${node.unique.id}']['RTarget']
        self.assertIsNotNone(re.fullmatch(regex, 'id-x86.2'))
        self.assertIsNone(re.fullmatch(regex, 'id-x86X2'))
        self.assertIsNone(re.fullmatch(regex, 'id-other'))
        self.assertTrue(job['TaskGroups'][0]['Tasks'][0]['Config']['cpu_hard_limit'])

    @patch.dict('os.environ', {'LUMA_BUILD_PLATFORM_URL': 'http://build', 'LUMA_BUILD_PLATFORM_ENABLED': '1'})
    def test_config_replace_and_clear_preserve_legacy_cache_default(self):
        with patch.object(server, '_mutate_control_state', side_effect=lambda fn: fn(self.state)), patch.object(server, 'require_token'):
            result = server.handle_build_config_set('token', {'nodes': ['x86', 'arm'], 'defaultNode': 'x86'})
            self.assertEqual([node['name'] for node in result['build']['nodes']], ['x86', 'arm'])
            self.assertEqual(self.state['build']['defaultNode'], 'legacy')
            result = server.handle_build_config_set('token', {'nodes': []})
            self.assertEqual(result['build']['nodes'], [])
            self.assertEqual(self.state['build']['defaultNode'], 'legacy')
