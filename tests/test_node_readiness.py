import copy
import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from starlette.testclient import TestClient

from luma.control import server
from luma.errors import LumaError
from luma.node_readiness import wait_for_node_readiness


class JoinReadinessTests(unittest.TestCase):
    def setUp(self):
        self.state = {'nodes': {'worker': {'nodeId': 'node-1'}}}
        self.token = server._issue_node_agent_token(self.state, 'worker', node_id='node-1')
        self.body = {'nodeName': 'worker', 'nodeId': 'node-1'}
        self.patch = patch.object(server, 'load_runtime_state', return_value=self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def heartbeat(self):
        record = server._require_node_agent_token(self.state, self.token, 'worker', node_id='node-1')
        server._update_agent_heartbeat(record, {'version': '1.2.3', 'capabilities': ['exec']})

    def test_readiness_does_not_create_its_own_heartbeat(self):
        before = copy.deepcopy(self.state)
        self.assertFalse(server.handle_node_agent_readiness(self.token, self.body)['ready'])
        self.assertEqual(self.state, before)
        self.heartbeat()
        self.assertTrue(server.handle_node_agent_readiness(self.token, self.body)['ready'])

    def test_rotated_credential_rejects_old_heartbeat_and_old_token(self):
        self.heartbeat()
        old = self.token
        self.token = server._issue_node_agent_token(self.state, 'worker', node_id='node-1')
        self.assertFalse(server.handle_node_agent_readiness(self.token, self.body)['ready'])
        with self.assertRaisesRegex(LumaError, 'unauthorized'):
            server.handle_node_agent_readiness(old, self.body)
        self.heartbeat()
        self.assertTrue(server.handle_node_agent_readiness(self.token, self.body)['ready'])

    def test_wrong_node_and_stale_heartbeat_fail(self):
        for body in ({'nodeName': 'worker', 'nodeId': 'wrong'}, {'nodeName': 'other', 'nodeId': 'wrong'}):
            with self.assertRaisesRegex(LumaError, 'unauthorized'):
                server.handle_node_agent_readiness(self.token, body)
        self.heartbeat()
        self.state['nodes']['worker']['agent']['lastSeen'] = 1
        self.assertFalse(server.handle_node_agent_readiness(self.token, self.body)['ready'])
        with self.assertRaisesRegex(LumaError, 'required'):
            server.handle_node_agent_readiness(self.token, {})

    def test_waits_for_literal_ready_and_matching_identity(self):
        client = Mock()
        client.request.side_effect = [LumaError('temporary connection error'), {'ready': 'yes', 'nodeId': 'node-1'},
                                     {'ready': True, 'nodeId': 'other'}, {'ready': True, 'nodeId': 'node-1'}]
        with patch('luma.node_readiness.time.sleep'):
            result = wait_for_node_readiness(client, node_name='worker', node_id='node-1')
        self.assertTrue(result['ready'])
        self.assertEqual(client.request.call_count, 4)
        self.assertEqual(client.request.call_args.args[1], '/v1/node-agent/readiness')

    def test_old_manager_is_actionable_and_timeout_is_bounded(self):
        client = Mock()
        client.request.side_effect = LumaError('HTTP 404')
        with self.assertRaisesRegex(LumaError, 'update the manager'):
            wait_for_node_readiness(client, node_name='worker', node_id='node-1')
        with self.assertRaisesRegex(LumaError, 'not removed'):
            wait_for_node_readiness(client, node_name='worker', node_id='node-1', timeout=0)
        client.request.assert_called_once()

    def test_asgi_route_uses_node_scoped_auth(self):
        # No app lifespan/worker startup or real state directories for route tests.
        client = TestClient(server.create_app())
        try:
            response = client.post('/v1/node-agent/readiness', json=self.body,
                                   headers={'Authorization': 'Bearer ' + self.token})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['ready'])
            response = client.post('/v1/node-agent/readiness', json=self.body,
                                   headers={'Authorization': 'Bearer incorrect'})
            self.assertEqual(response.status_code, 401, response.text)
        finally:
            client.close()

    def test_legacy_route_has_same_read_only_result(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.ControlHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        before = copy.deepcopy(self.state)
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/v1/node-agent/readiness',
                data=json.dumps(self.body).encode(), headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertFalse(json.load(response)['ready'])
            self.assertEqual(self.state, before)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)
