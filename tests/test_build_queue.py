import argparse
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from luma.control import build_queue as queue
from luma.control import server as srv
from luma.control.client import ControlClient
from luma.control.state import init_state, load_state, save_state
from luma.cli import _wait_for_queued_build
from luma.errors import LumaError


class BuildQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, LUMA_CONTROL_STATE_DIR=str(root / 'state'), LUMA_CONTROL_CONFIG=str(root / 'luma.yaml'))
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / 'luma.yaml').write_text('providers: {}\n')
        state = init_state(domain='luma.example.com', cluster_id='test', overwrite=True)
        state['build'] = {'defaultNode': 'builder', 'registryHost': 'builder:5000'}
        state['nodes'] = {'cn': {'name': 'cn', 'region': 'cn', 'nomadStatus': 'ready',
                                  'agent': {'status': 'ready', 'os': 'linux', 'arch': 'amd64'}}}
        save_state(state)
        self.token = state['deployToken']

    def remote(self, project='acme/app', **body):
        return srv._create_build_run(body, source=f'https://github.com/{project}.git', build_node='builder',
                                     project_key=project, queued_work=body)

    def prepare(self):
        return srv.handle_local_build_prepare(self.token, {'repoUrl': 'https://github.com/acme/app.git', 'region': 'cn', 'queue': True})

    def submit(self, prepared, **extra):
        return srv.handle_local_build_complete(self.token, prepared['run']['id'], {'queue': True,
            'buildResult': {'kind': 'service', 'image': prepared['upload']['image'],
                            'manifest': 'name: app\nregion: cn\nexposure: none\n'}, **extra})

    def finish(self, item, status='succeeded'):
        srv._complete_build_run(item[0], status)
        srv._mutate_control_state(lambda state: queue.discard(state, state['buildRuns'][item[0]]))

    def test_fifo_atomic_claim_and_other_project_independence(self):
        first, second, other = self.remote(), self.remote(), self.remote('acme/other')
        self.assertEqual(queue.position(second), {'queuePosition': 2, 'waitingFor': first})
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: queue.claim(), range(6)))
        self.assertEqual({item[0] for item in claims if item}, {first, other})
        self.assertIsNone(queue.claim())
        self.finish(next(item for item in claims if item and item[0] == first), 'failed')
        self.assertEqual(queue.claim()[0], second)

    def test_local_uploads_can_build_together_and_join_fifo_on_submission(self):
        a, b = self.prepare(), self.prepare()
        remote = self.remote()
        self.submit(b)
        self.submit(a)
        self.assertEqual(queue.claim()[0], remote)
        self.finish((remote, {}))
        self.assertEqual(queue.claim()[0], b['run']['id'])
        self.finish((b['run']['id'], {}))
        self.assertEqual(queue.claim()[0], a['run']['id'])

    def test_cancel_queued_does_not_cancel_active_or_leak_payload(self):
        first = self.remote(envSecrets={'PASSWORD': 'first-value'})
        second = self.remote(envSecrets={'PASSWORD': 'second-value'})
        item = queue.claim()
        srv.handle_build_run_cancel(self.token, second)
        state = load_state()
        self.assertEqual(state['buildRuns'][first]['status'], 'running')
        self.assertEqual(state['buildRuns'][second]['status'], 'canceled')
        self.assertNotIn(second, state['buildQueue'])
        public = json.dumps(srv.handle_build_run_get(self.token, first))
        self.assertNotIn('first-value', public)
        self.assertNotIn('envSecrets', public)
        self.assertEqual(item[1]['body']['envSecrets']['PASSWORD'], 'first-value')

    def test_accepted_upload_replay_is_idempotent_and_validated(self):
        prepared = self.prepare()
        first = self.submit(prepared)
        second = self.submit(prepared)
        self.assertEqual(first['buildRunId'], second['buildRunId'])
        self.assertEqual(len(load_state()['buildQueue']), 1)
        with self.assertRaisesRegex(LumaError, 'different result'):
            self.submit(prepared, envSecrets={'PASSWORD': 'different'})
        other = self.prepare()
        with self.assertRaisesRegex(LumaError, 'reserved project repository'):
            srv.handle_local_build_complete(self.token, other['run']['id'], {'queue': True, 'buildResult': {
                'image': 'evil:5000/other:latest', 'kind': 'service', 'manifest': 'name: app'}})
        self.assertEqual(load_state()['buildRuns'][other['run']['id']]['status'], 'running')

    def test_worker_runs_local_deployment_with_attempt_secrets_and_cleans_up(self):
        prepared = self.prepare()
        self.submit(prepared, envSecrets={'PASSWORD': 'private-value'})
        item = queue.claim()
        with patch.object(srv, 'handle_deployment', return_value={'service': 'app'}) as deploy:
            queue.execute(item)
        self.assertEqual(deploy.call_args.args[1]['envSecrets'], {'PASSWORD': 'private-value'})
        state = load_state()
        self.assertEqual(state['buildRuns'][item[0]]['status'], 'succeeded')
        self.assertNotIn(item[0], state['buildQueue'])
        self.assertNotIn('private-value', json.dumps(srv.handle_build_run_get(self.token, item[0])))

    def test_failed_progress_does_not_release_project_before_unwind(self):
        first, second = self.remote(), self.remote()
        queue.claim()
        srv._append_build_run_event(first, {'status': 'fail', 'message': 'step failed'})
        self.assertIsNone(queue.claim())
        self.finish((first, {}), 'failed')
        self.assertEqual(queue.claim()[0], second)

    def test_restart_keeps_queue_but_does_not_replay_active_deploy(self):
        first = self.prepare()
        self.submit(first)
        queued = self.remote()
        item = queue.claim()
        unuploaded = self.prepare()
        with patch.object(srv, '_CONTROL_PROCESS_INSTANCE_ID', 'new-process'):
            srv._reconcile_orphaned_build_runs_after_control_restart()
            queue.recover()
        state = load_state()
        self.assertEqual(state['buildRuns'][item[0]]['status'], 'failed')
        self.assertNotIn(item[0], state['buildQueue'])
        self.assertEqual(state['buildRuns'][queued]['status'], 'queued')
        self.assertEqual(state['buildRuns'][unuploaded['run']['id']]['status'], 'running')
        self.assertEqual(queue.claim()[0], queued)

    def test_upload_lease_no_longer_expires_after_acceptance(self):
        prepared = self.prepare()
        self.submit(prepared)
        item = queue.claim()
        srv._mutate_control_state(lambda state: srv._expire_stale_local_build_runs(state['buildRuns'], 9999999999))
        self.assertEqual(load_state()['buildRuns'][item[0]]['status'], 'finalizing')

    def test_retry_cannot_duplicate_a_queued_attempt(self):
        original = self.remote()
        with self.assertRaisesRegex(LumaError, 'active build cannot be retried'):
            srv._create_build_run({}, source='test', build_node='builder', project_key='acme/app', retry_of=original, queued_work={})

    def test_remote_submission_and_execution_keep_one_build_identity(self):
        with patch.object(srv, '_require_build_node', return_value='builder'), patch.object(srv, '_build_proxy_for_request', return_value=''):
            response = srv.handle_build_deploy(self.token, {'queue': True, 'repoUrl': 'https://github.com/acme/app.git', 'envSecrets': {'PASSWORD': 'one'}})
            build_id = response['buildRunId']
            self.assertEqual(response['run']['status'], 'queued')
            with patch.object(srv, '_run_node_agent_task', return_value={
                'kind': 'service', 'image': 'builder:5000/acme/app:immutable',
                'manifest': 'name: app\nimage: placeholder\nregion: cn\nexposure: none\n',
            }), patch.object(srv, 'handle_deployment', return_value={'service': 'app'}) as deploy:
                queue.execute(queue.claim())
        state = load_state()
        self.assertEqual(len(state['buildRuns']), 1)
        self.assertEqual(state['buildRuns'][build_id]['status'], 'succeeded')
        self.assertEqual(deploy.call_args.args[1]['gitSource']['buildRunId'], build_id)
        self.assertEqual(deploy.call_args.args[1]['envSecrets'], {'PASSWORD': 'one'})

    def test_workflow_is_recorded_without_a_waiting_client(self):
        from luma.cli import build_parser
        from luma.deploy_workflow import make_recipe
        prepared = self.prepare()
        recipe = make_recipe(build_parser().parse_args(['build', 'local', '.']))
        self.submit(prepared, workflow={'recipe': recipe, 'expectedRevision': 0,
                                       'selector': {'name': 'app', 'repoUrl': 'https://github.com/acme/app.git'}})
        with patch.object(srv, 'handle_deployment', return_value={'service': 'app'}):
            queue.execute(queue.claim())
        state = load_state()
        self.assertEqual(state['deploymentWorkflows']['app']['lastSuccess']['buildId'], prepared['run']['id'])
        self.assertTrue(state['buildRuns'][prepared['run']['id']]['result']['workflow']['saved'])
        self.assertNotIn('workflow', state['buildRuns'][prepared['run']['id']]['request'])

    def test_workflow_conflict_does_not_fail_deployment(self):
        prepared = self.prepare()
        self.submit(prepared, workflow={'expectedRevision': 10})
        with patch.object(srv, 'handle_deployment', return_value={'service': 'app'}), patch.object(srv, 'handle_workflow_record', side_effect=LumaError('newer recipe preserved')):
            queue.execute(queue.claim())
        run = srv.handle_build_run_get(self.token, prepared['run']['id'])['run']
        self.assertEqual(run['status'], 'succeeded')
        self.assertFalse(run['result']['workflow']['saved'])
        self.assertEqual(run['result']['workflow']['warning'], 'newer recipe preserved')

    def test_queue_result_preserves_compose_sidecar_confirmation(self):
        self.assertEqual(srv._build_run_result_summary({'deployment': 'app', 'composeSidecar': 'luma.compose.yml'})['composeSidecar'], 'luma.compose.yml')

    def test_uploaded_images_are_protected_while_queued(self):
        from luma.registry_management import collect_state_image_references
        prepared = self.prepare()
        self.submit(prepared)
        refs = collect_state_image_references(load_state(), 'builder:5000')
        self.assertTrue(any(ref['repository'] == 'acme/app' and ref['tag'] == prepared['upload']['tag'] for ref in refs))

    def test_http_acceptance_is_independent_of_deployment_completion(self):
        from starlette.testclient import TestClient
        prepared = self.prepare()
        entered, release = threading.Event(), threading.Event()
        def deploy(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test deployment was not released')
            return {'service': 'app'}
        with patch.object(srv, 'handle_deployment', side_effect=deploy), patch.object(srv.operations_api.OperationsWorker, 'start'), patch.object(srv.operations_api.OperationsWorker, 'close'):
            with TestClient(srv.create_app()) as client:
                try:
                    response = client.post(f"/v1/builds/local/{prepared['run']['id']}/complete", headers={'Authorization': f'Bearer {self.token}'}, json={
                        'queue': True, 'buildResult': {'kind': 'service', 'image': prepared['upload']['image'], 'manifest': 'name: app\nregion: cn\nexposure: none\n'}})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()['queued'])
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(load_state()['buildRuns'][prepared['run']['id']]['status'], 'finalizing')
                finally:
                    release.set()
        self.assertEqual(load_state()['buildRuns'][prepared['run']['id']]['status'], 'succeeded')

    def test_worker_calls_existing_remote_executor_without_creating_another_run(self):
        run = self.remote(repoUrl='https://github.com/acme/app.git')
        with patch.object(srv, 'handle_build_deploy') as execute:
            queue.execute(queue.claim())
        self.assertEqual(execute.call_args.kwargs['_queued_run_id'], run)
        self.assertNotIn('queue', execute.call_args.args[1])


class QueueClientTests(unittest.TestCase):
    def test_capability_negotiation_and_legacy_fallback(self):
        for supported in (True, False):
            client = ControlClient('https://control.example.com', 'token')
            with patch.object(client, 'health', return_value={'capabilities': ['build-queue-v1'] if supported else []}), patch.object(client, 'request', return_value={}) as request:
                client.prepare_local_build({'repoUrl': 'https://github.com/acme/app.git'})
                self.assertEqual(request.call_args.args[2].get('queue', False), supported)
                client.complete_local_build('b1', build_result={})
                self.assertEqual(request.call_args.args[2].get('queue', False), supported)
                client.build_deploy(repo_url='https://github.com/acme/app.git')
                self.assertEqual(request.call_args.args[2].get('queue', False), supported)

    def test_poll_timeout_never_cancels_or_fails_server_job(self):
        args = argparse.Namespace(timeout=0, format='json', quiet=True)
        client = Mock()
        client.get_build.return_value = {'run': {'status': 'queued', 'queuePosition': 2, 'waitingFor': 'b0'}}
        with self.assertRaisesRegex(LumaError, 'server task continues'):
            _wait_for_queued_build(args, client, {'queued': True, 'buildRunId': 'b1'})
        client.cancel_build.assert_not_called()
        client.fail_local_build.assert_not_called()

    def test_poll_does_not_treat_acceptance_as_deployment_success(self):
        args = argparse.Namespace(timeout=10, format='json', quiet=True)
        client = Mock()
        client.get_build.side_effect = [
            {'run': {'status': 'queued', 'queuePosition': 1}},
            {'run': {'status': 'succeeded', 'result': {'service': 'app', 'image': 'image:immutable'}}},
        ]
        with patch('luma.cli.time.sleep'):
            result = _wait_for_queued_build(args, client, {'queued': True, 'buildRunId': 'b1'})
        self.assertEqual(result, {'service': 'app', 'image': 'image:immutable', 'buildRunId': 'b1'})


if __name__ == '__main__':
    unittest.main()
