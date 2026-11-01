"""Inactivity transitions, durable clocks and actual workload references."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luma.control import server
from luma.errors import LumaError
from luma.registry_management import apply_protection

DIGEST = 'sha256:' + 'a' * 64
DAY = 86400
START = 1800000000


def inventory():
    return {'entries': [{'repository': 'luma/control', 'digest': DIGEST,
                         'tags': ['latest'], 'createdAt': 1}], 'summary': {}}


def history(result):
    return {e['repository'] + '@' + e['digest']: e for e in result['entries']}


class InactivityTests(unittest.TestCase):
    def test_unused_window_restarts_after_use_and_incomplete_scans(self):
        first = apply_protection(inventory(), [], complete=True, now=START)
        self.assertEqual(first['entries'][0]['unusedSince'], START)
        self.assertEqual(first['summary']['candidateCount'], 0)
        before = apply_protection(inventory(), [], complete=True, now=START + 7 * DAY - 1, previous_usage=history(first))
        due = apply_protection(inventory(), [], complete=True, now=START + 7 * DAY, previous_usage=history(before))
        self.assertEqual(before['summary']['candidateCount'], 0)
        self.assertEqual(due['summary']['candidateCount'], 1)
        ref = {'repository': 'luma/control', 'digest': DIGEST, 'kind': 'nomad-current'}
        used = apply_protection(inventory(), [ref], complete=True, now=START + 8 * DAY, previous_usage=history(due))
        self.assertEqual(used['entries'][0]['unusedSince'], 0)
        self.assertEqual(used['entries'][0]['protectionStatus'], 'protected')
        idle = apply_protection(inventory(), [], complete=True, now=START + 9 * DAY, previous_usage=history(used))
        self.assertEqual(idle['entries'][0]['unusedSince'], START + 9 * DAY)
        for failed in (False, True):
            data = inventory()
            if failed:
                data['errors'] = [{'message': 'missing manifest'}]
            unknown = apply_protection(data, [], complete=failed, now=START + 20 * DAY, previous_usage=history(idle))
            self.assertEqual(unknown['entries'][0]['protectionStatus'], 'unknown')
            resumed = apply_protection(inventory(), [], complete=True, now=START + 21 * DAY, previous_usage=history(unknown))
            self.assertEqual(resumed['entries'][0]['unusedSince'], START + 21 * DAY)
            self.assertFalse(resumed['entries'][0]['deletable'])

    def test_legacy_policy_migrates_to_seven_days_recommend_once(self):
        state = {'registryManagement': {'policy': {'mode': 'enforce', 'maxAgeDays': 30, 'keepLast': 20, 'systemKeepLast': 3}}}
        policy = server._registry_management_state(state)['policy']
        self.assertEqual((policy['mode'], policy['maxAgeDays'], policy['keepLast'], policy['systemKeepLast']), ('recommend', 7, 0, 0))
        policy.update(mode='off', maxAgeDays=10)
        self.assertEqual(server._registry_management_state(state)['policy']['maxAgeDays'], 10)
        self.assertEqual(server._registry_management_state(state)['policy']['mode'], 'off')

    def test_usage_survives_cache_invalidation_and_control_restart(self):
        state = {'registryManagement': {}}
        spec = {'host': 'registry.test', 'node': 'builder'}
        with tempfile.TemporaryDirectory() as root, patch.object(server, 'state_dir', return_value=Path(root)), \
                patch.object(server, '_REGISTRY_SCAN_CACHE', {}), patch.object(server, '_REGISTRY_SCAN_EPOCHS', {}), \
                patch.object(server, '_managed_registry_spec', return_value=spec), \
                patch.object(server, '_apply_state_secrets'), \
                patch.object(server, '_refresh_registry_inventory', side_effect=lambda *a: inventory()), \
                patch.object(server, '_registry_nomad_references', return_value=([], True, '')), \
                patch.object(server, '_mutate_control_state', side_effect=lambda f: f(state)), \
                patch('luma.registry_management.time.time', return_value=START):
            server.save_state({'host': spec['host'], 'result': inventory()}, Path(root) / 'registry-inventory.json')
            self.assertIsNone(server._load_registry_scan_snapshot(spec))
            first = server._registry_inventory_for_state(state, refresh=True)
            server._invalidate_registry_scan(spec['host'])
            self.assertEqual(server._registry_usage_history(spec['host'])['luma/control@' + DIGEST]['unusedSince'], START)
            with patch('luma.registry_management.time.time', return_value=START + 7 * DAY):
                due = server._registry_inventory_for_state(state, refresh=True)
            self.assertEqual(first['summary']['candidateCount'], 0)
            self.assertEqual(due['summary']['candidateCount'], 1)
            with patch.object(server, '_refresh_registry_inventory', side_effect=LumaError('offline')):
                with self.assertRaises(LumaError):
                    server._registry_inventory_for_state(state, refresh=True)
            self.assertEqual(server._registry_usage_history(spec['host']), {})

    def test_nomad_uses_current_job_and_running_allocations_not_history(self):
        def job(tag, stopped=False):
            return {'Stop': stopped, 'TaskGroups': [{'Tasks': [{'Config': {'image': 'registry.test/acme/api:' + tag}}]}]}
        responses = {
            '/v1/jobs': [{'ID': 'api'}],
            '/v1/job/api': job('new'),
            '/v1/job/api/allocations': [{'ID': 'live', 'ClientStatus': 'running'}, {'ID': 'old', 'ClientStatus': 'complete'}],
            '/v1/allocation/live': {'Job': job('still-running-old')},
        }
        with patch.object(server, 'load_config'), patch.object(server, 'nomad_addr'), patch.object(server, 'NomadApi') as api:
            api.return_value.request.side_effect = lambda method, path: copy.deepcopy(responses[path])
            refs, complete, error = server._registry_nomad_references({}, {'host': 'registry.test'})
            self.assertTrue(complete, error)
            self.assertEqual({r['tag'] for r in refs}, {'new', 'still-running-old'})
            responses['/v1/job/api']['Stop'] = True
            refs, complete, _ = server._registry_nomad_references({}, {'host': 'registry.test'})
            self.assertTrue(complete)
            self.assertEqual({r['tag'] for r in refs}, {'still-running-old'})
            responses['/v1/allocation/live'] = {}
            self.assertFalse(server._registry_nomad_references({}, {'host': 'registry.test'})[1])

    def test_queued_automatic_deletion_cannot_bypass_new_unused_window(self):
        row = inventory()['entries'][0]
        row.update(protectionStatus='retained', protectionReasons=[])
        request = [{'repository': row['repository'], 'digest': row['digest']}]
        result = server._registry_deletion_preview_from_inventory({'entries': [row], 'protectionComplete': True}, request)
        self.assertFalse(result['allowed'])
        self.assertTrue(server._registry_deletion_preview_from_inventory({'entries': [row], 'protectionComplete': True}, request, manual_override=True)['allowed'])
