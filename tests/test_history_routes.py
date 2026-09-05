from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from luma.control import server
from luma.errors import LumaError


class HistoryRouteTests(unittest.TestCase):
    routes = (
        ('/v1/history', 'list_history', ()),
        ('/v1/history/build/build-1', 'get_history', ('build', 'build-1')),
        ('/v1/builds', 'list_builds', ()),
        ('/v1/builds/build-1', 'get_build', ('build-1',)),
        ('/v1/deployments/history', 'list_deployments', ()),
        ('/v1/deployments/history/deploy-1', 'get_deployment', ('deploy-1',)),
    )

    def call_legacy(self, path, query, token='management'):
        handler = server.ControlHandler.__new__(server.ControlHandler)
        handler.path = path + ('?' + query if query else '')
        handler.headers = {'Authorization': 'Bearer ' + token}
        handler._json = Mock()
        handler._error = Mock()
        handler.do_GET()
        return handler

    def call_asgi(self, path, query, token='management'):
        request = server.Request({'type': 'http', 'method': 'GET', 'path': path,
                                  'query_string': query.encode(),
                                  'headers': [(b'authorization', ('Bearer ' + token).encode())]})
        return asyncio.run(server._asgi_authenticated_get(request))

    def test_all_history_routes_forward_identical_flat_queries_in_both_stacks(self):
        query = 'limit=10&limit=25&cursor=page%2Btwo&app=api&status=failed&source=build&since=10&until=20'
        expected = {'limit': '25', 'cursor': 'page+two', 'app': 'api', 'status': 'failed', 'source': 'build', 'since': '10', 'until': '20'}
        for path, function, args in self.routes:
            with self.subTest(path=path), patch.object(server, 'load_auth_state', return_value={'deployToken': 'management'}), patch.object(server, 'load_state', side_effect=AssertionError('history auth must not hydrate entities')), patch.object(server.control_history, function, return_value={'page': {'hasMore': False}}) as adapter:
                handler = self.call_legacy(path, query)
                handler._json.assert_called_once_with(200, {'page': {'hasMore': False}})
                self.assertEqual(adapter.call_args.args, (*args, expected))
                response = self.call_asgi(path, query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(adapter.call_args.args, (*args, expected))
                self.assertEqual(adapter.call_count, 2)

    def test_authentication_rejects_join_and_wrong_tokens_before_any_history_query(self):
        for path, function, _args in self.routes:
            for token in ('join-token', 'wrong'):
                with self.subTest(path=path, token=token), patch.object(server, 'load_auth_state', return_value={'deployToken': 'management', 'joinToken': 'join-token'}), patch.object(server.control_history, function) as adapter:
                    handler = self.call_legacy(path, 'limit=invalid&cursor=invalid', token)
                    self.assertEqual(handler._error.call_args.args[0], 401)
                    response = self.call_asgi(path, 'limit=invalid&cursor=invalid', token)
                    self.assertEqual(response.status_code, 401)
                    adapter.assert_not_called()

    def test_invalid_pagination_is_400_in_both_stacks_after_auth(self):
        with patch.object(server, 'load_auth_state', return_value={'deployToken': 'management'}):
            handler = self.call_legacy('/v1/history', 'limit=101')
            self.assertEqual(handler._error.call_args.args[0], 400)
            self.assertEqual(self.call_asgi('/v1/history', 'limit=101').status_code, 400)

    def test_direct_legacy_handler_calls_keep_optional_query_arguments(self):
        handlers = (
            (server.handle_build_run_list, (), 'list_builds'),
            (server.handle_build_run_get, ('b1',), 'get_build'),
            (server.handle_deployment_history, (), 'list_deployments'),
            (server.handle_deployment_history_get, ('d1',), 'get_deployment'),
        )
        for handler, args, function in handlers:
            with self.subTest(function=function), patch.object(server, 'load_auth_state', return_value={'deployToken': 'management'}), patch.object(server.control_history, function, return_value={}) as adapter:
                handler('management', *args)
                self.assertEqual(adapter.call_args.args, (*args, None))


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'LUMA_CONTROL_STATE_DIR': self.directory.name})
        self.env.start()
        self.state = server.init_state(domain='history.example.com', cluster_id='history-test', overwrite=True)
        self.token = self.state['deployToken']

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def pages(self, handler, key, *args):
        items = []
        query = {'limit': 100}
        while True:
            response = handler(self.token, *args, query)
            items.extend(response[key])
            if not response['page']['hasMore']:
                return items
            query = {'limit': 100, 'cursor': response['page']['nextCursor']}

    def test_build_history_over_100_preserves_older_running_records(self):
        self.state['buildRuns'] = {
            f'b-{index}': {'id': f'b-{index}', 'status': 'succeeded', 'createdAt': index + 1, 'updatedAt': index + 1, 'events': []}
            for index in range(150)
        }
        self.state['buildRuns']['old-running'] = {'id': 'old-running', 'status': 'running', 'projectKey': 'old-project', 'createdAt': 1, 'updatedAt': 1, 'events': []}
        server.save_state(self.state)
        new_id = server._create_build_run({}, source='test', build_node='builder', project_key='new-project')
        rows = self.pages(server.handle_build_run_list, 'runs')
        self.assertEqual(len(rows), 152)
        self.assertIn(new_id, {row['id'] for row in rows})
        self.assertEqual(next(row for row in rows if row['id'] == 'old-running')['status'], 'running')

    def test_deployment_history_over_200_remains_readable(self):
        self.state['deploymentEvents'] = [
            {'id': f'd-{index}', 'kind': 'service', 'name': 'api', 'slug': 'api', 'origin': 'cli', 'status': 'succeeded', 'createdAt': index + 1, 'steps': [{'message': str(index)}]}
            for index in range(205)
        ]
        server.save_state(self.state)
        server._record_deployment_event(kind='service', name='api', slug='api', source_name='api.yaml', origin='dashboard', status='failed', steps=[{'message': 'failure'}])
        rows = self.pages(server.handle_deployment_history, 'events')
        self.assertEqual(len(rows), 206)
        self.assertIn('d-0', {row['id'] for row in rows})
        self.assertEqual(server.handle_deployment_history_get(self.token, 'd-0')['event']['steps'], [{'message': '0'}])

    def test_cancellation_keeps_more_than_300_build_events(self):
        self.state['buildRuns'] = {'b1': {'id': 'b1', 'status': 'running', 'createdAt': 1, 'updatedAt': 1,
                                        'events': [{'message': f'line-{index}'} for index in range(350)]}}
        server.save_state(self.state)
        server.handle_build_run_cancel(self.token, 'b1')
        page = server.handle_build_run_get(self.token, 'b1', {'limit': 100})
        events = list(page['run']['events'])
        while page['eventsPage']['hasMore']:
            page = server.handle_build_run_get(self.token, 'b1', {'limit': 100, 'cursor': page['eventsPage']['nextCursor']})
            events.extend(page['run']['events'])
        self.assertEqual(len(events), 351)
        self.assertEqual(events[0]['message'], 'line-0')
        self.assertEqual(events[-1]['status'], 'canceled')

    def test_legacy_prune_hook_never_iterates_or_deletes_historical_runs(self):
        class NoScan(dict):
            def values(self):
                raise AssertionError('a heartbeat must not scan historical builds')
            def items(self):
                raise AssertionError('a heartbeat must not scan historical builds')
        runs = NoScan({'old-running': {'status': 'running'}})
        server._prune_build_runs({'buildRuns': runs}, limit=0)
        self.assertIn('old-running', runs)

    def test_retry_attempt_links_preserve_root_and_project_busy_guard(self):
        original = server._create_build_run({'repoUrl': 'https://example.com/app'}, source='test', build_node='builder', project_key='app')
        server._append_build_run_event(original, {'message': 'first failure evidence'})
        server._complete_build_run(original, 'failed', message='first failed')
        first_detail = server.handle_build_run_get(self.token, original)['run']
        retry = server._create_build_run({'repoUrl': 'https://example.com/app'}, source='test', build_node='builder', project_key='app', retry_of=original)
        self.assertNotEqual(retry, original)
        with self.assertRaisesRegex(LumaError, 'active build'):
            server._create_build_run({}, source='test', build_node='builder', project_key='app', retry_of=original)
        with self.assertRaisesRegex(LumaError, 'active build cannot be retried'):
            server._create_build_run({}, source='test', build_node='builder', project_key='app', retry_of=retry)
        self.assertEqual(len(server.handle_build_run_list(self.token)['runs']), 2)
        server._complete_build_run(retry, 'failed', message='second failed')
        third = server._create_build_run({}, source='test', build_node='builder', project_key='app', retry_of=retry)
        third_detail = server.handle_build_run_get(self.token, third)['run']
        self.assertEqual(third_detail['retryOf'], retry)
        self.assertEqual(third_detail['retryRootId'], original)
        self.assertEqual(server.handle_build_run_get(self.token, original)['run'], first_detail)

    def test_active_build_guards_use_filtered_entity_iterator(self):
        class ActiveOnly(dict):
            def values(self):
                raise AssertionError('must use active_values')
            def items(self):
                raise AssertionError('must use active_values')
            def active_values(self, statuses):
                return (value for value in super().values() if value['status'] in statuses)
        runs = ActiveOnly({'b1': {'id': 'b1', 'projectKey': 'app', 'status': 'running', 'mode': 'local', 'expiresAt': 20}})
        with self.assertRaisesRegex(LumaError, 'already has an active build'):
            server._require_build_project_available(runs, 'app')
        server._expire_stale_local_build_runs(runs, now=30)
        self.assertEqual(runs['b1']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
