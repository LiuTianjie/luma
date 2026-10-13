from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from luma.build_platform.blobs import Blobs
from luma.build_platform.model import Conflict, decode_cursor, encode_cursor, public_request
from luma.build_platform.nomad import Nomad
from luma.build_platform.registry import Harbor
from luma.build_platform.source import deployment_result, plan_source
from luma.control import build_platform as gateway
from luma.errors import LumaError
from luma.nomad_api import _register_build_release


class BuildPlatformContracts(unittest.TestCase):
    def test_public_request_and_cursor(self):
        body = public_request({'repoUrl': 'https://git/app', 'envSecrets': {'KEY': 'secret'}, 'gitToken': 'token'})
        self.assertEqual(body, {'repoUrl': 'https://git/app', 'envSecretNames': ['KEY']})
        value = {'run': 'bld2-test', 'after': 8, 'through': 9}
        self.assertEqual(decode_cursor(encode_cursor(value)), value)
        with self.assertRaises(LumaError):
            decode_cursor('!bad')

    def test_compose_plan_preserves_consumers_and_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'Dockerfile').write_text('FROM scratch\n')
            (root / 'luma.compose.yml').write_text('name: app\ncompose: docker-compose.yml\nregion: cn\n')
            (root / 'docker-compose.yml').write_text('services:\n  api:\n    image: app:local\n    build:\n      context: .\n      args:\n        MODE: production\n  worker:\n    image: app:local\n')
            plan = plan_source(root, {'composeSidecar': 'luma.compose.yml', 'platform': 'linux/amd64'}, {'projectKey': 'git/project', 'targetPlatformsByRegion': {'cn': ['linux/amd64']}}, 'luma')
            self.assertEqual(plan['targets'][0]['args'], {'MODE': 'production'})
            self.assertEqual(plan['platforms'], ['linux/amd64'])
            result = deployment_result({**plan, 'sha': 'a' * 40}, {'api': 'registry/app@sha256:' + 'b' * 64})
            import yaml
            compose = yaml.safe_load(result['composeContent'])
            self.assertEqual(compose['services']['api']['image'], compose['services']['worker']['image'])
            self.assertNotIn('build', compose['services']['api'])

    def test_log_milestones_have_stable_replay_bytes(self):
        blobs = object.__new__(Blobs)
        blobs.append = Mock()
        store = Mock()
        store.get.return_value = {'created_at': 100}
        blobs.milestone(store, 'run', 'source', 'Source', 'sha')
        first = blobs.append.call_args
        blobs.milestone(store, 'run', 'source', 'Source', 'sha')
        self.assertEqual(first, blobs.append.call_args)

    def test_registration_lost_reply_reconciles_without_resubmit(self):
        client = Mock()
        client.request.return_value = {'Meta': {'luma.release.id': 'rel-new', 'luma.release.generation': '3'}, 'JobModifyIndex': 50}
        result = _register_build_release(client, {'ID': 'app'}, {'id': 'rel-new', 'generation': 3})
        self.assertEqual(result['JobModifyIndex'], 50)
        self.assertEqual(client.request.call_count, 1)

    def test_older_release_cannot_overwrite_new_generation(self):
        client = Mock()
        client.request.return_value = {'Meta': {'luma.release.generation': '4'}, 'JobModifyIndex': 50}
        with self.assertRaisesRegex(LumaError, 'superseded'):
            _register_build_release(client, {'ID': 'app'}, {'id': 'rel-old', 'generation': 3})
        self.assertEqual(client.request.call_count, 1)

    @patch('luma.control.build_platform.request')
    def test_intervening_manual_deployment_fails_closed(self, gateway_request):
        gateway_request.return_value = {'expectedIndex': 20}
        client = Mock()
        client.request.return_value = {'Meta': {}, 'JobModifyIndex': 21}
        with self.assertRaisesRegex(LumaError, 'changed after'):
            _register_build_release(client, {'ID': 'app'}, {'id': 'rel-old', 'generation': 3})
        self.assertEqual(client.request.call_count, 1)

    @patch('luma.control.build_platform.request')
    def test_new_job_has_cas_baseline_zero(self, gateway_request):
        gateway_request.return_value = {'expectedIndex': 0}
        missing = LumaError('missing')
        missing.__cause__ = urllib.error.HTTPError('http://nomad', 404, '', {}, None)
        client = Mock()
        client.request.side_effect = [missing, {'JobModifyIndex': 51}]
        _register_build_release(client, {'ID': 'app'}, {'id': 'rel-new', 'generation': 3})
        payload = client.request.call_args.args[2]
        self.assertEqual((payload['EnforceIndex'], payload['JobModifyIndex']), (True, 0))
        self.assertEqual(payload['Job']['Meta']['luma.release.id'], 'rel-new')

    def test_missing_or_lost_allocation_does_not_claim_stopped(self):
        nomad = Nomad(SimpleNamespace())
        nomad.request = Mock(side_effect=[{'Stop': True}, [{'ClientStatus': 'lost'}]])
        self.assertFalse(nomad.observe('job')['terminal'])
        nomad.request = Mock(return_value=None)
        self.assertFalse(nomad.observe('job')['terminal'])

    def test_existing_job_does_not_reissue_bootstrap_token(self):
        nomad = Nomad(SimpleNamespace())
        nomad.request = Mock(return_value={'Meta': {'luma.attempt': 'bld2-test-a1'}})
        store = Mock()
        store.attempt.return_value = {'id': 'bld2-test-a1'}
        nomad.submit(store, {'id': 'bld2-test'}, 1)
        self.assertEqual(nomad.request.call_count, 1)
        store.attempt.assert_called_once_with('bld2-test', 1)

    def test_registry_verifies_config_platform_and_digest(self):
        registry = Harbor(SimpleNamespace(registry_url='https://registry', registry_host='registry', registry_username='robot', registry_password='secret'))
        raw = json.dumps({'os': 'linux', 'architecture': 'arm64'}).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        registry.manifest = Mock(return_value={'manifest': {'config': {'digest': digest}}, 'image': 'registry/luma/app@sha256:' + 'a' * 64})
        registry.request = Mock(return_value=(raw, {}))
        with self.assertRaisesRegex(Conflict, 'native platform'):
            registry.platform_manifest('registry/luma/app@sha256:' + 'a' * 64, 'linux/amd64')
        registry.platform_manifest('registry/luma/app@sha256:' + 'a' * 64, 'linux/arm64')
        registry.request.return_value = (b'tampered', {})
        with self.assertRaisesRegex(Conflict, 'digest verification'):
            registry.platform_manifest('registry/luma/app@sha256:' + 'a' * 64, 'linux/arm64')

    @patch.dict('os.environ', {'LUMA_BUILD_PLATFORM_URL': 'http://unavailable', 'LUMA_BUILD_API_TOKEN': 'test'})
    @patch('urllib.request.urlopen', side_effect=OSError('offline'))
    def test_gateway_outage_fails_closed(self, _):
        with self.assertRaisesRegex(LumaError, 'not sent to the legacy builder'):
            gateway.request('POST', '/v1/builds', {})

class BuildPlatformApiContracts(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient
        from luma.build_platform.api import create_app
        self.store, self.blobs, self.harbor = Mock(), Mock(), Mock()
        self.run = {'id': 'bld2-aabb', 'request': {'ref': 'main'}, 'source': 'https://git/app', 'project_key': 'git/app',
            'status': 'running', 'phase': 'building', 'build_status': 'building', 'mode': 'builder',
            'created_at': 1, 'updated_at': 2, 'next_event': 4, 'result': {}}
        self.store.get.return_value = self.run
        self.blobs.events.side_effect = lambda store, run, after, through, limit: [{'message': str(n)} for n in range(after + 1, min(through + 1, after + limit + 1))]
        settings = SimpleNamespace(api_token='internal', registry_host='registry')
        self.client = TestClient(create_app(settings, store=self.store, blobs=self.blobs, harbor=self.harbor))
        self.headers = {'Authorization': 'Bearer internal'}

    def tearDown(self):
        self.client.close()

    def test_authenticated_detail_obeys_legacy_live_cursor_contract(self):
        first = self.client.get('/v1/builds/bld2-aabb?limit=2', headers=self.headers)
        self.assertEqual(first.status_code, 200)
        page = first.json()['eventsPage']
        self.assertEqual(page['resumeAfter'], 1)
        self.assertTrue(page['hasMore'])
        self.run['next_event'] = 5
        second = self.client.get('/v1/builds/bld2-aabb', params={'limit': 2, 'cursor': page['nextCursor']}, headers=self.headers).json()
        self.assertEqual([event['message'] for event in second['run']['events']], ['2', '3'])
        self.assertFalse(second['eventsPage']['hasMore'])
        live = self.client.get('/v1/builds/bld2-aabb?after=3', headers=self.headers).json()
        self.assertEqual(live['run']['events'], [{'message': '4'}])

    def test_missing_service_token_cannot_read_build(self):
        response = self.client.get('/v1/builds/bld2-aabb')
        self.assertEqual(response.status_code, 401)
        self.store.get.assert_not_called()

    def test_different_registry_override_rejected_before_admission(self):
        response = self.client.post('/v1/builds', headers=self.headers, json={
            'source': 'https://git/app', 'projectKey': 'git/app', 'request': {'registryHost': 'other'}})
        self.assertEqual(response.status_code, 409)
        self.store.create.assert_not_called()

class PublicationTransportContract(unittest.TestCase):
    def test_unknown_nomad_commit_is_retryable_across_control_http(self):
        from luma.control.build_platform import retryable_publication_error
        error = LumaError('Nomad registration failed')
        error.__cause__ = urllib.error.URLError('connection reset')
        self.assertTrue(retryable_publication_error(error))
        error.__cause__ = urllib.error.HTTPError('http://nomad', 409, 'CAS', {}, None)
        self.assertTrue(retryable_publication_error(error))
        self.assertFalse(retryable_publication_error(LumaError('invalid manifest')))
