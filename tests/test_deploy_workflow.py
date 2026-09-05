from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from starlette.testclient import TestClient

from luma.cli import build_parser, main
from luma.control.client import ControlClient
from luma.control.server import ControlHandler, create_app
from luma.control.state import init_state, load_state, mutate_state
from luma.control.workflows import handle_workflow_check, handle_workflow_get, handle_workflow_record
from luma.deploy_workflow import make_recipe, validate_recipe
from luma.errors import LumaError


class DeployWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.cwd = Path.cwd()
        os.chdir(self.root)
        (self.root / '.git').mkdir()
        (self.root / '.luma.yml').write_text('name: app\nimage: nginx:alpine\nregion: cn\nexposure: none\n')
        self.environment = patch.dict(os.environ, {
            'LUMA_CONTROL_STATE_DIR': str(self.root / 'state'),
            'LUMA_CONTROL_URL': 'https://control.example.com',
            'LUMA_DEPLOY_TOKEN': 'test-token',
        })
        self.environment.start()
        state = init_state(domain='control.example.com', overwrite=True)
        self.token = state['deployToken']
        self.client = Mock()
        self.client.check_workflow.side_effect = lambda body: handle_workflow_check(self.token, body)
        self.client.record_workflow.side_effect = lambda body: handle_workflow_record(self.token, body)
        self.client.get_workflow.side_effect = lambda name: handle_workflow_get(self.token, name)
        self.client.deploy_events.side_effect = lambda **kwargs: iter([{'status': 'done', 'result': {'service': 'app'}}])
        self.client.build_deploy_events.side_effect = lambda **kwargs: iter([{'status': 'done', 'result': {'service': 'app', 'buildRunId': 'build-new'}}])
        self.client.deploy_compose_events.side_effect = lambda **kwargs: iter([{'status': 'done', 'result': {'deployment': 'app'}}])
        self.client_patch = patch('luma.cli.ControlClient', return_value=self.client)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.environment.stop()
        os.chdir(self.cwd)
        self.temp.cleanup()

    def recipe(self, *argv):
        return make_recipe(build_parser().parse_args(['--no-env', *argv]))

    def record(self, *argv, name='app', source='manual', **extra):
        return handle_workflow_record(self.token, {'name': name, 'recipe': self.recipe(*argv), 'source': source, **extra})['workflow']

    def invoke(self, *argv, interactive=False, answer='n'):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), patch('sys.stdin.isatty', return_value=interactive), patch('builtins.input', return_value=answer) as prompt:
            code = main(['--no-env', *argv])
        return code, output.getvalue(), error.getvalue(), prompt

    def test_first_deploy_is_allowed_and_automatically_recorded(self):
        code, output, error, _ = self.invoke('deploy', '.luma.yml', '--format', 'json')
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)['result']['workflow']['saved'])
        saved = handle_workflow_get(self.token, 'app')['workflow']
        self.assertEqual(saved['source'], 'cli-success')
        self.assertEqual(saved['recipe']['method'], 'image-deploy')
        self.assertEqual(saved['lastSuccess']['recipe'], saved['recipe'])

    def test_same_workflow_repeats_without_confirmation(self):
        self.record('import', 'acme/app', '--ref', 'main')
        code, _, error, prompt = self.invoke('import', 'acme/app', '--ref', 'main', '--timeout', '40')
        self.assertEqual(code, 0, error)
        prompt.assert_not_called()
        self.client.build_deploy_events.assert_called_once()

    def test_changed_ref_stops_before_build_and_keeps_record(self):
        old = self.record('import', 'acme/app', '--ref', 'main', source='cli-success')
        code, _, error, _ = self.invoke('import', 'acme/app', '--ref', 'dev', '--format', 'json')
        self.assertEqual(code, 1)
        self.assertIn('ref', error)
        self.assertIn('--accept-workflow-change', error)
        self.client.build_deploy_events.assert_not_called()
        self.client.record_workflow.assert_not_called()
        self.assertEqual(handle_workflow_get(self.token, 'app')['workflow'], old)

    def test_local_build_is_stopped_before_reservation_or_docker(self):
        self.record('import', 'acme/app', '--ref', 'main')
        with patch('luma.local_build.local_source_metadata', return_value={'path': str(self.root), 'repoUrl': 'https://github.com/acme/app.git', 'revision': 'abc'}), patch('luma.local_build.build_and_push_local_source') as build:
            code, _, error, _ = self.invoke('build', 'local', '.', '--format', 'json')
        self.assertEqual(code, 1)
        self.assertIn('remote-build', error)
        self.assertIn('local-build', error)
        self.client.prepare_local_build.assert_not_called()
        build.assert_not_called()

    def test_first_local_build_records_after_completion(self):
        self.client.prepare_local_build.return_value = {'run': {'id': 'local-id'}, 'upload': {'registryHost': 'builder:5000', 'repository': 'acme/app', 'tag': 'abc', 'platform': 'linux/amd64'}}
        self.client.complete_local_build.return_value = {'service': 'app', 'image': 'builder:5000/acme/app:abc'}
        with patch('luma.local_build.local_source_metadata', return_value={'path': str(self.root), 'repoUrl': 'https://github.com/acme/app.git', 'revision': 'abc'}), patch('luma.local_build.build_and_push_local_source', return_value={'image': 'builder:5000/acme/app:abc'}):
            code, _, error, _ = self.invoke('build', 'local', '.')
        self.assertEqual(code, 0, error)
        saved = handle_workflow_get(self.token, 'app')['workflow']
        self.assertEqual(saved['recipe']['method'], 'local-build')
        self.assertEqual(saved['repoKey'], 'github.com/acme/app')

    def test_compose_deploy_checks_and_records_the_stack(self):
        Path('docker-compose.yml').write_text('services:\n  web:\n    image: nginx:alpine\n')
        Path('luma.compose.yml').write_text('name: app\ncompose: docker-compose.yml\nregion: cn\nservices:\n  web:\n    exposure: none\n')
        with patch('luma.cli._control_storage_classes_for_local', return_value={}), patch('luma.cli._control_node_records_for_local', return_value={}):
            code, _, error, _ = self.invoke('compose', 'deploy', 'luma.compose.yml')
        self.assertEqual(code, 0, error)
        self.assertEqual(handle_workflow_get(self.token, 'app')['workflow']['recipe']['method'], 'compose-deploy')

    def test_retry_does_not_bypass_prior_local_build_workflow(self):
        self.record('build', 'local', '.', selector={'repoUrl': 'https://github.com/acme/app.git'})
        self.client.get_build.return_value = {'run': {'mode': 'local', 'request': {'repoUrl': 'https://github.com/acme/app.git'}, 'result': {'service': 'app'}}}
        code, _, error, _ = self.invoke('build', 'retry', 'local-id')
        self.assertEqual(code, 1)
        self.assertIn('local-build', error)
        self.client.retry_build.assert_not_called()

    def test_first_retry_is_allowed_and_records_its_parameters(self):
        self.client.get_build.return_value = {'run': {'request': {'repoUrl': 'https://github.com/acme/app.git', 'ref': 'main', 'gitToken': 'never-save-this'}}}
        self.client.retry_build.return_value = {'service': 'app', 'buildRunId': 'build-id'}
        code, _, error, _ = self.invoke('build', 'retry', 'build-id')
        self.assertEqual(code, 0, error)
        record = handle_workflow_get(self.token, 'app')['workflow']
        self.assertEqual(record['recipe']['retryParameters']['ref'], 'main')
        self.assertNotIn('never-save-this', json.dumps(record))
        result = handle_workflow_check(self.token, {'recipe': self.recipe('import', 'acme/app', '--ref', 'main')})
        self.assertEqual(result['status'], 'match')

    def test_remote_import_discovers_prior_local_recipe_by_repo(self):
        self.record('build', 'local', '.', selector={'repoUrl': 'git@github.com:acme/app.git'})
        code, _, error, _ = self.invoke('import', 'acme/app')
        self.assertEqual(code, 1)
        self.assertIn('local-build', error)
        self.client.build_deploy_events.assert_not_called()

    def test_accept_flag_allows_change_and_success_updates_record(self):
        self.record('import', 'acme/app', '--ref', 'main', note='Use the remote Builder')
        code, _, error, _ = self.invoke('import', 'acme/app', '--ref', 'dev', '--accept-workflow-change')
        self.assertEqual(code, 0, error)
        saved = handle_workflow_get(self.token, 'app')['workflow']
        self.assertIn('dev', saved['recipe']['argv'])
        self.assertEqual(saved['note'], 'Use the remote Builder')
        self.assertEqual(saved['lastSuccess']['buildId'], 'build-new')

    def test_interactive_rejection_does_not_deploy(self):
        self.record('import', 'acme/app', '--ref', 'main')
        code, _, _, prompt = self.invoke('import', 'acme/app', '--ref', 'dev', interactive=True, answer='')
        self.assertEqual(code, 1)
        prompt.assert_called_once()
        self.client.build_deploy_events.assert_not_called()

    def test_interactive_confirmation_deploys(self):
        self.record('import', 'acme/app', '--ref', 'main')
        code, _, error, prompt = self.invoke('import', 'acme/app', '--ref', 'dev', interactive=True, answer='yes')
        self.assertEqual(code, 0, error)
        prompt.assert_called_once()
        self.client.build_deploy_events.assert_called_once()

    def test_json_never_prompts_even_with_a_tty(self):
        self.record('import', 'acme/app', '--ref', 'main')
        code, _, _, prompt = self.invoke('import', 'acme/app', '--ref', 'dev', '--format', 'json', interactive=True)
        self.assertEqual(code, 1)
        prompt.assert_not_called()

    def test_confirmed_but_failed_deploy_preserves_last_success(self):
        old = self.record('import', 'acme/app', '--ref', 'main', source='cli-success')
        self.client.build_deploy_events.side_effect = LumaError('build failed')
        code, _, _, _ = self.invoke('import', 'acme/app', '--ref', 'dev', '--accept-workflow-change')
        self.assertEqual(code, 1)
        self.assertEqual(handle_workflow_get(self.token, 'app')['workflow'], old)
        self.client.record_workflow.assert_not_called()

    def test_dry_run_does_not_check_or_record(self):
        code, _, error, _ = self.invoke('deploy', '.luma.yml', '--dry-run', '--format', 'json')
        self.assertEqual(code, 0, error)
        self.client.check_workflow.assert_not_called()
        self.client.record_workflow.assert_not_called()

    def test_skipped_orchestrator_does_not_record_success(self):
        code, _, error, _ = self.invoke('deploy', '.luma.yml', '--skip-orchestrator')
        self.assertEqual(code, 0, error)
        self.client.record_workflow.assert_not_called()

    def test_missing_or_unreachable_check_fails_before_deploy(self):
        self.client.check_workflow.side_effect = LumaError('connection refused')
        code, _, _, _ = self.invoke('deploy', '.luma.yml')
        self.assertEqual(code, 1)
        self.client.deploy_events.assert_not_called()

    def test_record_failure_does_not_report_successful_deployment_as_failed(self):
        self.client.record_workflow.side_effect = LumaError('disk full')
        code, output, error, _ = self.invoke('deploy', '.luma.yml', '--format', 'json')
        self.assertEqual(code, 0, error)
        payload = json.loads(output)
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['result']['workflow']['saved'])

    def test_record_command_does_not_deploy_and_show_returns_recipe(self):
        code, _, error, _ = self.invoke('workflow', 'record', 'app', '--note', 'Remote build only', '--', 'import', 'acme/app', '--ref', 'main')
        self.assertEqual(code, 0, error)
        self.client.build_deploy_events.assert_not_called()
        code, output, error, _ = self.invoke('workflow', 'show', 'app', '--format', 'json')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['result']['workflow']['note'], 'Remote build only')

    def test_run_reuses_recipe_without_shell_and_restores_cwd(self):
        self.record('import', 'acme/app', '--ref', 'main')
        code, _, error, _ = self.invoke('workflow', 'run', 'app', '--path', str(self.root))
        self.assertEqual(code, 0, error)
        self.assertEqual(self.client.build_deploy_events.call_args.kwargs['ref'], 'main')
        self.assertEqual(Path.cwd(), self.root)

    def test_credentials_and_env_contents_are_not_recorded(self):
        Path('.env').write_text('SECRET=hidden-env-value\n')
        args = build_parser().parse_args(['import', 'https://user:secret@example.com/acme/app.git?token=hidden#secret', '--token', 'control-secret'])
        recipe = make_recipe(args)
        text = json.dumps(recipe)
        self.assertNotIn('control-secret', text)
        self.assertNotIn('user:secret', text)
        self.assertNotIn('hidden', text)
        self.assertNotIn('SECRET', text)
        self.assertIn('https://example.com/acme/app.git', text)

    def test_recipe_cannot_embed_confirmation_or_redirect_control(self):
        for extra in [['--accept-workflow-change'], ['--control-url', 'https://other.example'], ['--token', 'secret'], ['--workflow-app', 'different']]:
            with self.subTest(extra=extra), self.assertRaises(LumaError):
                validate_recipe({'schemaVersion': 1, 'method': 'remote-build', 'argv': ['import', 'acme/app', *extra]})

    def test_application_record_survives_history_pruning_and_reload(self):
        saved = self.record('import', 'acme/app')
        mutate_state(lambda state: state.update({'buildRuns': {}, 'deploymentEvents': []}))
        self.assertEqual(load_state()['deploymentWorkflows']['app'], saved)

    def test_manual_edit_keeps_prior_success_separate(self):
        old = self.record('import', 'acme/app', '--ref', 'main', source='cli-success')
        new = self.record('import', 'acme/app', '--ref', 'dev')
        self.assertEqual(new['lastSuccess'], old['lastSuccess'])
        self.assertNotEqual(new['recipe'], new['lastSuccess']['recipe'])

    def test_concurrent_record_change_is_not_overwritten(self):
        first = self.record('import', 'acme/app')
        second = self.record('import', 'acme/app', '--ref', 'main')
        with self.assertRaisesRegex(LumaError, 'changed while'):
            self.record('import', 'acme/app', source='cli-success', expectedRevision=first['revision'])
        self.assertEqual(handle_workflow_get(self.token, 'app')['workflow'], second)

    def test_ambiguous_monorepo_requires_application_selection(self):
        self.record('import', 'acme/repo', '--compose-sidecar', 'a.yml', name='a')
        self.record('import', 'acme/repo', '--compose-sidecar', 'b.yml', name='b')
        with self.assertRaisesRegex(LumaError, 'multiple application'):
            handle_workflow_check(self.token, {'recipe': self.recipe('import', 'acme/repo')})
        result = handle_workflow_check(self.token, {'recipe': self.recipe('import', 'acme/repo', '--compose-sidecar', 'b.yml')})
        self.assertEqual(result['status'], 'match')
        self.assertEqual(result['workflow']['name'], 'b')

    def test_new_application_in_known_repository_is_not_blocked(self):
        self.record('import', 'acme/repo', name='a')
        result = handle_workflow_check(self.token, {'recipe': self.recipe('deploy', '.luma.yml'), 'selector': {'name': 'b', 'repoUrl': 'https://github.com/acme/repo.git'}})
        self.assertEqual(result['status'], 'unrecorded')

    def test_provider_and_url_identify_same_repository(self):
        self.record('import', '--provider-id', 'github:me', '--repository', 'acme/app', '--ref', 'main')
        result = handle_workflow_check(self.token, {'recipe': self.recipe('import', 'https://github.com/acme/app.git', '--ref', 'main')})
        self.assertEqual(result['status'], 'match')

    def test_asgi_routes_authenticate_and_persist(self):
        with TestClient(create_app()) as client:
            headers = {'Authorization': f'Bearer {self.token}'}
            self.assertEqual(client.get('/v1/workflows').status_code, 401)
            self.assertEqual(client.post('/v1/workflows', json={}).status_code, 401)
            response = client.post('/v1/workflows', headers=headers, json={'name': 'app', 'recipe': self.recipe('import', 'acme/app')})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get('/v1/workflows/app', headers=headers).json()['workflow']['name'], 'app')
            self.assertEqual(len(client.get('/v1/workflows', headers=headers).json()['workflows']), 1)
            response = client.post('/v1/workflows/check', headers=headers, json={'recipe': self.recipe('import', 'acme/app')})
            self.assertEqual(response.json()['status'], 'match')
            self.assertIn('deployment-workflow-v1', client.get('/v1/health').json()['capabilities'])

    def test_legacy_http_routes_check_and_save_workflows(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), ControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path, body=None):
                req = urllib.request.Request(
                    f'http://127.0.0.1:{server.server_port}' + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'},
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            recipe = self.recipe('import', 'acme/app')
            self.assertEqual(request('/v1/workflows/check', {'recipe': recipe})['status'], 'unrecorded')
            request('/v1/workflows', {'name': 'app', 'recipe': recipe})
            self.assertEqual(request('/v1/workflows/app')['workflow']['name'], 'app')
            self.assertEqual(len(request('/v1/workflows')['workflows']), 1)
            self.assertEqual(request('/v1/workflows/check', {'recipe': recipe})['status'], 'match')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_old_control_fails_with_upgrade_instruction(self):
        client = ControlClient('https://control.example', 'token')
        with patch.object(client, 'request', side_effect=LumaError('control API error 404: not found')):
            with self.assertRaisesRegex(LumaError, 'update the manager'):
                client.check_workflow({})


if __name__ == '__main__':
    unittest.main()
