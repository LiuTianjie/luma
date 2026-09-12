import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from starlette.websockets import WebSocketDisconnect

from luma.control import server
from luma.errors import LumaError


class FakeWebSocket:
    def __init__(self, query, token):
        self.query_params = query
        self.token = token
        self.sent = []
        self.closed = None

    async def accept(self):
        pass

    async def receive_json(self):
        if self.token is not None:
            token, self.token = self.token, None
            return {"type": "auth", "token": token}
        raise WebSocketDisconnect(1000)

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        self.closed = code


class TerminalResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {
            "deployToken": "management-token",
            "nodes": {"worker": {
                "nodeId": "node-id",
                "aliases": ["worker-alias"],
                "agent": {"tokenHash": server._hash_agent_token("agent-token")},
            }},
        }

    async def exercise_blocked_connection(self, *, agent=False, application=False):
        loop_thread = threading.get_ident()
        started, release = threading.Event(), threading.Event()

        def pause():
            self.assertNotEqual(threading.get_ident(), loop_thread)
            started.set()
            self.assertTrue(release.wait(3), "terminal lookup did not get released")

        def load():
            self.assertNotEqual(threading.get_ident(), loop_thread)
            if not application:
                pause()
            return self.state

        def resolve(state, service):
            self.assertIs(state, self.state)
            self.assertEqual(service, "app_api")
            pause()
            return {"node": "worker", "service": service, "job": "app", "task": "api", "allocId": "alloc"}

        broker = server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
        query = {"service": "app_api"} if application else {"node": "worker-alias", "nodeId": "node-id"}
        socket = FakeWebSocket(query, "agent-token" if agent else "management-token")
        with patch.object(server, "load_state", side_effect=AssertionError("terminal must not load history")) as full, \
             patch.object(server, "load_dashboard_state", side_effect=load), \
             patch.object(server, "_resolve_application_terminal_target", side_effect=resolve):
            task = asyncio.create_task(broker.connect_agent(socket) if agent else broker.connect_browser(socket))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3))
                self.assertFalse(task.done())
                # Exercise the real health handler on the same event loop while
                # terminal DB/upstream work is deliberately still blocked.
                response = await asyncio.wait_for(server._asgi_health(None), 1)
                self.assertTrue(json.loads(response.body)["ok"])
                self.assertFalse(release.is_set())
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
            full.assert_not_called()
        if agent:
            self.assertIn({"type": "ready", "node": "worker"}, socket.sent)
        else:
            self.assertEqual(socket.closed, 1013)  # authenticated; no agent connected

    async def test_agent_reconnect_does_not_block_health_or_load_history(self):
        await self.exercise_blocked_connection(agent=True)

    async def test_browser_auth_does_not_block_health_or_load_history(self):
        await self.exercise_blocked_connection()

    async def test_application_terminal_nomad_lookup_does_not_block_health(self):
        await self.exercise_blocked_connection(application=True)

    async def test_light_snapshot_preserves_agent_identity_and_credentials(self):
        with patch.object(server, "load_dashboard_state", return_value=self.state):
            self.assertEqual(server._terminal_agent_identity("agent-token", "worker-alias", "node-id"), "worker")
            for token, node_id in [("wrong", "node-id"), ("agent-token", "other-node")]:
                with self.subTest(token=token, node_id=node_id), self.assertRaises(LumaError):
                    server._terminal_agent_identity(token, "worker", node_id)

    async def test_browser_requires_management_auth_before_nomad_lookup(self):
        with patch.object(server, "load_dashboard_state", return_value=self.state), \
             patch.object(server, "_resolve_application_terminal_target") as resolve:
            with self.assertRaises(LumaError):
                server._terminal_browser_target("wrong", "", "app_api")
            resolve.assert_not_called()
            self.assertEqual(server._terminal_browser_target("management-token", "worker-alias", ""), ("worker", None))
