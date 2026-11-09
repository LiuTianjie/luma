"""Real TLS/PTY transport checks against an isolated, local Control broker."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import pty
import signal
import subprocess
import tempfile
from pathlib import Path
import sys
import termios
import time
import unittest
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch, AsyncMock

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from luma.agent import _PtySession, _terminal_ws_connect, _run_terminal_supervisor
from luma.command_session import CommandSession, CHUNK_SIZE, command_request
from luma.cli import build_parser
from luma.cli import terminal
from luma.control import server
from luma.control.client import ControlClient
from luma.errors import LumaError
from luma.terminal_direct import DirectTerminals


class BrokerSocket:
    """Adapt the local websocket fixture to Starlette's broker interface."""
    def __init__(self, websocket):
        self.websocket = websocket
        self.query_params = dict(parse_qsl(urlsplit(websocket.request.path).query))
        self.cookies = {}
        self.headers = {}

    async def accept(self):
        pass

    async def receive_json(self):
        return json.loads(await self.websocket.recv())

    async def send_json(self, message):
        await self.websocket.send(json.dumps(message))

    async def close(self, code=1000):
        await self.websocket.close(code=code)


class TerminalTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.output = asyncio.Queue()
        self.processes = []
        self.agent_messages = []
        self.relay_sessions = {}
        self.broker = server.TerminalBroker(per_node_limit=2, idle_timeout_seconds=60)
        self.direct = DirectTerminals({"terminalDirect": {"listenHost": "127.0.0.1", "port": 0}},
                                      outbound=self.output, open_terminal=self.open_terminal)
        await self.direct.start()
        self.addAsyncCleanup(self.direct.stop)
        state = {"deployToken": "management-token", "nodes": {"worker": {
            "nodeId": "node-id", "aliases": ["worker-alias"],
            "agent": {"tokenHash": server._hash_agent_token("agent-token")},
        }}}
        self.state_patch = patch.object(server, "load_dashboard_state", return_value=state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

        async def route(websocket):
            adapter = BrokerSocket(websocket)
            if websocket.request.path.startswith("/agent"):
                await self.broker.connect_agent(adapter)
            else:
                await self.broker.connect_browser(adapter)

        self.control_server = await serve(route, "127.0.0.1", 0, close_timeout=1)
        self.port = self.control_server.sockets[0].getsockname()[1]
        self.agent = await connect(f"ws://127.0.0.1:{self.port}/agent?node=worker&nodeId=node-id", proxy=None)
        await terminal._send(self.agent, {"type": "auth", "token": "agent-token", "directTerminal": True, "execV1": True})
        self.assertEqual((await terminal._receive(self.agent))["type"], "ready")
        self.pumps = [asyncio.create_task(self.agent_receive()), asyncio.create_task(self.agent_send())]
        self.client = ControlClient("https://control.example.com", "management-token")

    def open_terminal(self, message, output):
        if message.get("mode") == "exec":
            process = CommandSession(message["sessionId"], message, outbound=output)
            self.processes.append(process)
            if output is self.output:
                self.relay_sessions[message["sessionId"]] = process
            return process
        # A real PTY child, with deterministic output and no machine mutations.
        script = ("import os,sys; assert os.tcgetpgrp(0) == os.getpgrp(); "
                  "line=sys.stdin.readline(); print('echo:' + line.strip()); "
                  "print('size:%sx%s' % (os.get_terminal_size(0).columns, os.get_terminal_size(0).lines)); "
                  "sys.exit(7)")
        process = _PtySession(message["sessionId"], outbound=output, loop=self.loop, rows=32, cols=120,
                              argv=[sys.executable, "-c", script])
        self.processes.append(process)
        if output is self.output:
            self.relay_sessions[message["sessionId"]] = process
        return process

    async def agent_receive(self):
        try:
            async for raw in self.agent:
                message = json.loads(raw)
                if self.direct.handle(message):
                    continue
                sid = message.get("sessionId")
                if message["type"] == "open":
                    self.open_terminal(message, self.output)
                elif sid in self.relay_sessions:
                    process = self.relay_sessions[sid]
                    if isinstance(process, CommandSession) and message["type"] in {"stdin", "stdin-eof", "exec-ack"}:
                        process.receive(message)
                    elif message["type"] == "input":
                        process.write(message["data"])
                    elif message["type"] == "resize":
                        process.resize(rows=message["rows"], cols=message["cols"])
                    elif message["type"] == "close":
                        process.close()
        finally:
            await self.direct.reset()

    async def agent_send(self):
        while True:
            message = await self.output.get()
            self.agent_messages.append(message)
            await terminal._send(self.agent, message)

    async def asyncTearDown(self):
        await self.agent.close()
        for task in self.pumps:
            task.cancel()
        await asyncio.gather(*self.pumps, return_exceptions=True)
        await self.direct.stop()
        self.control_server.close()
        await self.control_server.wait_closed()
        for process in self.processes:
            process.close()
            if isinstance(process, CommandSession):
                await asyncio.gather(process.task, return_exceptions=True)
            else:
                await asyncio.to_thread(process.reader.join, 2)

    def control_connection(self, client, *, name, target):
        return connect(f"ws://127.0.0.1:{self.port}/browser?{target}={name}", proxy=None, close_timeout=1)

    async def execute(self, argv, *, transport="relay", data=None, timeout=5, chunk_size=CHUNK_SIZE):
        stdout, stderr = io.BytesIO(), io.BytesIO()
        out, err = io.TextIOWrapper(stdout), io.TextIOWrapper(stderr)
        remaining = data or b""
        async def read():
            nonlocal remaining
            chunk, remaining = remaining[:chunk_size], remaining[chunk_size:]
            return chunk
        with patch.object(terminal, "control_connection", side_effect=self.control_connection), \
             patch.object(terminal, "_read_stdin", side_effect=read), \
             patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            result = await asyncio.wait_for(terminal.run_shell(
                self.client, name="worker", target="node", transport=transport,
                execution={"mode": "exec", "argv": argv, "timeoutSeconds": timeout}, forward_stdin=data is not None,
            ), timeout + 8)
        return result, stdout.getvalue(), stderr.getvalue()

    async def test_exec_separates_binary_streams_and_exit_status_without_tty(self):
        script = "import os; assert not os.isatty(0); os.write(1,b'out\\x00\\xff'); os.write(2,b'err\\x00\\xfe'); raise SystemExit(23)"
        for route in ("relay", "direct", "auto"):
            with self.subTest(route=route):
                self.assertEqual(await self.execute([sys.executable, "-c", script], transport=route),
                                 (23, b"out\0\xff", b"err\0\xfe"))

    async def test_exec_relay_works_without_a_node_listener(self):
        await self.direct.stop()
        result = await self.execute([sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"])
        self.assertEqual(result, (0, b"0\n", b""))

    async def test_exec_streams_more_than_flow_control_window(self):
        data = bytes(range(256)) * 8192
        script = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(data[::-1])"
        self.assertEqual(await self.execute([sys.executable, "-c", script], data=data), (0, data, data[::-1]))

    async def test_oversized_stdin_frame_ends_session_and_reaps_command(self):
        with self.assertRaisesRegex(LumaError, "invalid exec stdin frame"):
            await self.execute([sys.executable, "-c", "import time; time.sleep(60)"], transport="direct",
                               data=b"x" * (CHUNK_SIZE + 1), chunk_size=CHUNK_SIZE + 1)
        process = self.processes[-1]
        await asyncio.wait_for(process.wait_closed(), 5)
        if process.process:
            self.assertIsNotNone(process.process.returncode)

    async def test_exec_timeout_terminates_process(self):
        code, _, err = await self.execute([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.15)
        self.assertEqual(code, 124)
        self.assertIn(b"timed out", err)
        process = self.processes[-1]
        await asyncio.wait_for(asyncio.shield(process.task), 4)
        self.assertIsNotNone(process.process.returncode)

    async def test_exec_disconnect_cancels_remote_process(self):
        for route in ("relay", "direct"):
            count = len(self.processes)
            task = asyncio.create_task(self.execute([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60, transport=route))
            async def started():
                while len(self.processes) == count or self.processes[-1].process is None:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(started(), 3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            process = self.processes[-1]
            await asyncio.wait_for(asyncio.gather(process.task, return_exceptions=True), 4)
            self.assertIsNotNone(process.process.returncode)

    async def test_silent_exec_uses_deadline_instead_of_shell_idle_timeout(self):
        browser = AsyncMock()
        session = server._TerminalSession("silent-exec", "worker", browser, self.broker._agents["worker"])
        session.last_activity = time.time() - 3600
        session.execution_deadline = time.monotonic() + 3600
        with patch("luma.control.server.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            await self.broker._watch_idle(session)
        browser.close.assert_not_awaited()
        browser.send_json.assert_not_awaited()

    async def test_activity_cannot_extend_exec_deadline_and_shells_still_expire(self):
        for execution in (True, False):
            browser = AsyncMock()
            session = server._TerminalSession("expired", "worker", browser, self.broker._agents["worker"])
            self.broker._sessions[session.id] = session
            if execution:
                session.execution_deadline = time.monotonic() - 1
                session.touch()
            else:
                session.last_activity = time.time() - 3600
            with patch("luma.control.server.asyncio.sleep", AsyncMock()):
                await self.broker._watch_idle(session)
            self.assertTrue(session.closed)
            expected = "remote command deadline exceeded" if execution else "terminal session idle timeout"
            self.assertEqual(browser.send_json.call_args.args[0]["message"], expected)

    async def test_direct_control_lease_close_is_bounded(self):
        control = AsyncMock()
        control.recv.side_effect = ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
        with self.assertRaisesRegex(LumaError, "Control session closed"):
            await asyncio.wait_for(terminal._watch_control_lease(control), 2)

    async def test_exec_rejects_invalid_authorization_and_non_node_targets(self):
        for token, query in (("wrong-token", "exec-node=worker"), ("agent-token", "exec-node=worker"),
                             ("management-token", "exec-node=worker&service=app_api")):
            async with connect(f"ws://127.0.0.1:{self.port}/browser?{query}", proxy=None) as ws:
                await terminal._send(ws, {"type": "auth", "token": token, "transport": "relay", "mode": "exec",
                                          "argv": [sys.executable, "-c", "raise SystemExit(0)"]})
                with self.assertRaises((LumaError, ConnectionClosed)):
                    await terminal._receive(ws, timeout=2)
        self.assertEqual(self.processes, [])

    def test_exec_rejects_unbounded_or_malformed_requests(self):
        for value in ([], [""], ["echo", "a\0b"], ["echo", "x" * 131073], ["echo", 42]):
            with self.assertRaises(ValueError):
                command_request({"argv": value})
        for timeout in (0, -1, float("inf"), float("nan"), 86401, True):
            with self.assertRaises(ValueError):
                command_request({"argv": ["true"], "timeoutSeconds": timeout})

    async def test_exec_rejects_legacy_agent_before_starting_a_process(self):
        self.broker._agents["worker"].exec_v1 = False
        with self.assertRaisesRegex(LumaError, "exec-v1"):
            await self.execute([sys.executable, "-c", "raise SystemExit(0)"])
        self.assertEqual(self.processes, [])

    async def test_exec_target_cannot_be_downgraded_to_a_shell(self):
        async with connect(f"ws://127.0.0.1:{self.port}/browser?exec-node=worker", proxy=None) as ws:
            await terminal._send(ws, {"type": "auth", "token": "management-token", "transport": "relay"})
            with self.assertRaises(ConnectionClosed):
                await terminal._receive(ws, timeout=2)
        self.assertEqual(self.processes, [])

    async def test_exec_argv_is_literal_and_missing_command_returns_127(self):
        arg = "$HOME; echo should-not-run"
        self.assertEqual(await self.execute([sys.executable, "-c", "import sys; print(sys.argv[1])", arg]),
                         (0, (arg + "\n").encode(), b""))
        result = await self.execute(["/nonexistent/luma-executable"])
        self.assertEqual(result[0], 127)

    def test_exec_parser_preserves_remote_flags_and_uses_relay(self):
        args = build_parser().parse_args(["node", "exec", "--stdin", "--timeout", "12", "worker", "--", "sh", "-lc", "echo x"])
        self.assertEqual((args.argv, args.timeout, args.stdin, args.transport), (["sh", "-lc", "echo x"], 12, True, "relay"))

    def test_exec_cli_returns_distinct_interrupt_and_termination_status(self):
        script = """
import asyncio
from luma.cli.main import main
from luma.cli import terminal
async def waiting(*args, **kwargs):
    print('ready', flush=True)
    await asyncio.Future()
terminal.run_shell = waiting
terminal._terminal_client = lambda args: None
raise SystemExit(main(['node', 'exec', 'worker', '--', 'true']))
"""
        for sig, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                try:
                    self.assertEqual(proc.stdout.readline(), b"ready\n")
                    proc.send_signal(sig)
                    self.assertEqual(proc.wait(timeout=5), expected)
                finally:
                    if proc.poll() is None:
                        proc.kill()

    async def test_terminal_origin_override_preserves_tls_hostname_on_both_sides(self):
        args = build_parser().parse_args(["node", "shell", "worker", "--resolve-ip", "203.0.113.8"])
        with patch.object(terminal, "_control_context", return_value=("https://control.example.com", "secret", False, "203.0.113.8")):
            client = terminal._terminal_client(args)
        self.assertFalse(client.insecure)
        with patch("websockets.asyncio.client.connect") as connect_mock:
            terminal.control_connection(client, name="worker", target="node")
            self.assertTrue(connect_mock.call_args.kwargs["ssl"].check_hostname)
            self.assertEqual(connect_mock.call_args.kwargs["server_hostname"], "control.example.com")
            self.assertEqual(connect_mock.call_args.kwargs["host"], "203.0.113.8")
            self.assertTrue(connect_mock.call_args.args[0].startswith("wss://control.example.com/"))
        with patch("websockets.connect") as connect_mock:
            await _terminal_ws_connect("https://control.example.com/prefix", token="secret", node_name="worker", node_id="id", insecure=False, resolve_ip="203.0.113.8")
            self.assertTrue(connect_mock.call_args.kwargs["ssl"].check_hostname)
            self.assertEqual(connect_mock.call_args.kwargs["server_hostname"], "control.example.com")
            self.assertTrue(connect_mock.call_args.args[0].startswith("wss://control.example.com/prefix/v1/terminal/agent?"))

    async def test_preferred_public_relay_failure_falls_back_without_disabling_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agent.json"
            config.write_text(json.dumps({"endpoint": "https://control.example.com", "token": "agent-token",
                                          "nodeName": "worker", "nodeId": "id", "insecure": True,
                                          "terminalRelay": {"resolveIp": "203.0.113.8"},
                                          "terminalDirect": {"enabled": False}}))
            connect_mock = AsyncMock(side_effect=[OSError("origin unavailable"), asyncio.CancelledError()])
            with patch("luma.agent._terminal_ws_connect", connect_mock), patch("luma.control.client.ControlClient.health", return_value={}), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(asyncio.CancelledError):
                    await _run_terminal_supervisor(config)
            self.assertEqual([call.kwargs["resolve_ip"] for call in connect_mock.call_args_list], ["203.0.113.8", None])
            self.assertFalse(connect_mock.call_args_list[0].kwargs["insecure"])
            self.assertFalse(connect_mock.call_args_list[1].kwargs["insecure"])

    async def exercise_cli(self, transport="auto"):
        routes = []

        async def interact(websocket, *, control=None):
            routes.append("direct" if control else "relay")
            await terminal._send(websocket, {"type": "resize", "rows": 18, "cols": 77})
            await terminal._send(websocket, {"type": "input", "data": "你好 Luma\n"})
            output = ""
            while True:
                message = await terminal._receive(websocket, timeout=5)
                if message["type"] == "output":
                    output += message["data"]
                elif message["type"] == "exit":
                    self.assertIn("echo:你好 Luma", output)
                    self.assertIn("size:77x18", output)
                    return message["exitCode"]

        with patch.object(terminal, "control_connection", side_effect=self.control_connection), \
             patch.object(terminal, "interact", side_effect=interact), contextlib.redirect_stderr(io.StringIO()):
            result = await asyncio.wait_for(terminal.run_shell(self.client, name="worker-alias", target="node",
                                                              transport=transport), 10)
        self.assertEqual(result, 7)
        return routes

    async def test_auto_direct_uses_real_tls_and_pty_without_relaying_output(self):
        self.assertEqual(await self.exercise_cli(), ["direct"])
        self.assertFalse(any(message["type"] == "output" for message in self.agent_messages))
        self.assertEqual(len(self.processes), 1)

    async def test_auto_falls_back_when_direct_address_is_unreachable(self):
        self.direct.urls = ["wss://127.0.0.1:1/v1/terminal/direct"]
        self.assertEqual(await self.exercise_cli(), ["relay"])
        self.assertTrue(any(message["type"] == "output" for message in self.agent_messages))
        self.assertEqual(len(self.processes), 1)

    async def test_forced_relay_and_old_agent_use_existing_route(self):
        self.assertEqual(await self.exercise_cli("relay"), ["relay"])
        self.broker._agents["worker"].direct_terminal = False
        self.assertEqual(await self.exercise_cli(), ["relay"])

    async def test_required_direct_never_opens_relay_shell(self):
        self.direct.urls = ["wss://127.0.0.1:1/v1/terminal/direct"]
        with self.assertRaisesRegex(LumaError, "unreachable"):
            await self.exercise_cli("direct")
        self.assertEqual(self.processes, [])

    async def offer(self, browser):
        await terminal._send(browser, {"type": "auth", "token": "management-token", "transport": "auto"})
        self.assertEqual((await terminal._receive(browser))["type"], "open")
        return await terminal._receive(browser)

    async def test_pin_mismatch_and_expired_ticket_cannot_open_a_shell(self):
        async with self.control_connection(self.client, name="worker", target="node") as browser:
            offer = await self.offer(browser)
            with self.assertRaises(LumaError):
                await terminal.connect_direct(dict(offer, fingerprint="0" * 64))
            self.assertEqual(self.processes, [])
            self.assertIn(offer["sessionId"], self.direct.pending)
            self.direct.pending[offer["sessionId"]]["expires"] = time.monotonic() - 1
            direct = await terminal.connect_direct(offer)
            async with direct:
                await terminal._send(direct, {"type": "auth", "sessionId": offer["sessionId"], "ticket": offer["ticket"]})
                with self.assertRaises(ConnectionClosed):
                    await direct.recv()
            self.assertEqual(self.processes, [])

    async def test_ticket_is_single_use_and_control_disconnect_closes_direct_process(self):
        async with self.control_connection(self.client, name="worker", target="node") as browser:
            offer = await self.offer(browser)
            direct = await terminal.connect_direct(offer)
            async with direct:
                auth = {"type": "auth", "sessionId": offer["sessionId"], "ticket": offer["ticket"]}
                await terminal._send(direct, auth)
                self.assertEqual((await terminal._receive(direct))["type"], "open")
                second = await terminal.connect_direct(offer)
                async with second:
                    await terminal._send(second, auth)
                    with self.assertRaises(ConnectionClosed):
                        await second.recv()
                self.assertEqual(len(self.processes), 1)
                await browser.close()
                with self.assertRaises(ConnectionClosed):
                    await asyncio.wait_for(direct.recv(), 3)
        await self.direct.reset()

    async def test_another_agent_cannot_inject_ticket_or_output_into_session(self):
        async with self.control_connection(self.client, name="worker", target="node") as browser:
            offer = await self.offer(browser)
            sid = offer["sessionId"]
            other = server._TerminalAgentConnection("other-node", object())
            await self.broker.forward_to_browser(sid, {"type": "error", "message": "injected"}, agent=other)
            self.assertIn(sid, self.broker._sessions)
            direct = await terminal.connect_direct(offer)
            await direct.close()


class LocalTerminalTests(unittest.IsolatedAsyncioTestCase):
    def assert_tty_restored(self, fd, original):
        actual = termios.tcgetattr(fd)
        # macOS sets PENDIN when returning to canonical mode; it is kernel
        # input-queue state rather than a raw-mode/echo setting to restore.
        actual[3] &= ~getattr(termios, "PENDIN", 0)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(actual, original)

    async def test_remote_exit_restores_local_tty_and_does_not_swallow_ctrl_c(self):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        stdin = os.fdopen(os.dup(slave), "r")
        stdout = os.fdopen(os.dup(slave), "w")
        messages = []
        responses = asyncio.Queue()

        class Socket:
            async def send(self, raw):
                message = json.loads(raw)
                messages.append(message)
                if message["type"] == "resize":
                    asyncio.get_running_loop().call_later(0.02, os.write, master, b"\x03")
                elif message["type"] == "input":
                    await responses.put(json.dumps({"type": "exit", "exitCode": 9}))

            async def recv(self):
                return await responses.get()

        try:
            with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), \
                 contextlib.redirect_stderr(io.StringIO()):
                code = await asyncio.wait_for(terminal.interact(Socket()), 3)
            self.assertEqual(code, 9)
            self.assertIn({"type": "input", "data": "\x03"}, messages)
            self.assert_tty_restored(slave, original)
        finally:
            stdin.close()
            stdout.close()
            os.close(master)
            os.close(slave)

    async def test_local_tty_restored_on_connection_error(self):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)

        class Input:
            def fileno(self):
                return slave

        class Socket:
            async def send(self, raw):
                pass

            async def recv(self):
                raise OSError("lost connection")

        try:
            with patch.object(sys, "stdin", Input()), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(OSError):
                await terminal.interact(Socket())
            self.assert_tty_restored(slave, original)
        finally:
            os.close(master)
            os.close(slave)

    def test_shell_rejects_noninteractive_input_before_authentication(self):
        args = build_parser().parse_args(["node", "shell", "worker"])
        with patch.object(sys.stdin, "isatty", return_value=False), patch.object(terminal, "_control_context") as context:
            with self.assertRaisesRegex(LumaError, "interactive terminal"):
                terminal.cmd_shell(args)
            context.assert_not_called()

    async def test_pty_preserves_unicode_boundaries_and_drains_exit_output(self):
        output = asyncio.Queue()
        script = "import os; data=('中文'*5000).encode(); os.write(1,data[:1]); os.write(1,data[1:]); raise SystemExit(6)"
        process = _PtySession("drain", outbound=output, loop=asyncio.get_running_loop(), rows=32, cols=120,
                              argv=[sys.executable, "-c", script])
        try:
            text = ""
            while True:
                message = await asyncio.wait_for(output.get(), 5)
                if message["type"] == "output":
                    text += message["data"]
                else:
                    self.assertEqual(message["type"], "exit")
                    self.assertEqual(message["exitCode"], 6)
                    break
            self.assertEqual(text, "中文" * 5000)
        finally:
            process.close()
            await asyncio.to_thread(process.reader.join, 2)
