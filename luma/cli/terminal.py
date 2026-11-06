"""Interactive terminals over Luma's authenticated WebSocket transports."""
from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import ssl
import sys
import termios
import tty
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..control.client import ControlClient
from ..errors import LumaError
from ..terminal_direct import DirectTerminals
from ..command_session import CHUNK_SIZE, WINDOW, command_request
from .common import _control_context


@dataclass
class TerminalClient:
    endpoint: str
    token: str
    insecure: bool = False
    resolve_ip: str | None = None


def _terminal_client(args) -> TerminalClient:
    endpoint, token, insecure, resolve_ip = _control_context(args, require_token=True)
    # Validate the HTTPS endpoint without adopting HTTP's URL-rewrite resolver.
    validated = ControlClient(endpoint, token, insecure=insecure)
    return TerminalClient(validated.endpoint, token, insecure, resolve_ip)


def cmd_shell(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise LumaError("shell requires an interactive terminal (stdin and stdout must be TTYs)")
    client = _terminal_client(args)
    return asyncio.run(run_shell(client, name=args.name, target="service" if args.command == "app" else "node",
                                 transport=args.transport, diagnose=args.diagnose))


def cmd_exec(args: argparse.Namespace) -> int:
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    try:
        request = command_request({"argv": argv, "timeoutSeconds": args.timeout})
    except ValueError as exc:
        raise LumaError(str(exc)) from exc
    client = _terminal_client(args)
    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        terminated = False
        def terminate():
            nonlocal terminated
            terminated = True
            task.cancel()
        loop.add_signal_handler(signal.SIGTERM, terminate)
        try:
            return await run_shell(client, name=args.name, target="node", transport=args.transport,
                                   execution=request, forward_stdin=args.stdin, diagnose=args.diagnose)
        except asyncio.CancelledError:
            if terminated:
                return 143
            raise
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    try:
        return asyncio.run(run())
    except LumaError as exc:
        print(f"luma: {exc}; remote completion may be unknown; command was not retried", file=sys.stderr)
        return 255


def control_connection(client: ControlClient, *, name: str, target: str):
    from websockets.asyncio.client import connect

    parsed = urlsplit(client.endpoint)
    uri = urlunsplit(("wss", parsed.netloc, parsed.path.rstrip("/") + "/v1/terminal/browser",
                      urlencode({target: name}), ""))
    tls = ssl._create_unverified_context() if client.insecure else ssl.create_default_context()
    options: dict[str, Any] = {}
    if client.resolve_ip:
        # Override only the TCP destination; preserve Host and TLS server name.
        options.update(host=client.resolve_ip, port=parsed.port or 443, proxy=None, server_hostname=parsed.hostname)
    return connect(uri, ssl=tls, open_timeout=10, close_timeout=2, ping_interval=20,
                   ping_timeout=20, max_size=1024 * 1024, **options)


async def _send(websocket, message):
    await websocket.send(json.dumps(message, separators=(",", ":")))


async def _receive(websocket, *, timeout=None):
    raw = await asyncio.wait_for(websocket.recv(), timeout) if timeout else await websocket.recv()
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise LumaError("invalid terminal response")
    if message.get("type") == "error":
        raise LumaError(str(message.get("message") or "terminal failed"))
    return message


async def connect_direct(offer: dict[str, Any]):
    """Race reachable addresses; never send a ticket before checking the TLS pin."""
    from websockets.asyncio.client import connect

    fingerprint = str(offer.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not offer.get("ticket") or not offer.get("sessionId"):
        raise LumaError("invalid direct terminal offer")
    urls = offer.get("urls")
    if not isinstance(urls, list) or not urls:
        raise LumaError("node has no direct terminal address")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE  # Certificate is pinned through authenticated Control below.
    tls.minimum_version = ssl.TLSVersion.TLSv1_2

    async def probe(url):
        websocket = None
        try:
            uri = DirectTerminals._checked_url(url)
            websocket = await connect(uri, ssl=tls, proxy=None, open_timeout=3, close_timeout=1,
                                      ping_interval=20, ping_timeout=20, max_size=1024 * 1024)
            certificate = websocket.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if not secrets.compare_digest(hashlib.sha256(certificate).hexdigest(), fingerprint):
                raise LumaError("direct terminal certificate does not match Control's fingerprint")
            return websocket
        except BaseException:
            if websocket:
                await websocket.close()
            raise

    tasks = [asyncio.create_task(probe(url)) for url in urls[:16]]
    selected = None
    try:
        for task in asyncio.as_completed(tasks):
            try:
                selected = await task
                break
            except (OSError, ValueError, LumaError, TimeoutError):
                continue
            except Exception:
                continue
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in results if not isinstance(ws, BaseException) and ws is not selected),
                             return_exceptions=True)
    if selected is None:
        raise LumaError("direct terminal unreachable (check the node address and TCP port 17443)")
    return selected


async def run_shell(client: ControlClient, *, name: str, target: str, transport: str,
                    execution: dict | None = None, forward_stdin: bool = False, diagnose: bool = False) -> int:
    direct = None
    try:
        # Old Controls reject this unknown target before opening a PTY. They must
        # not silently interpret an exec request as an interactive shell.
        async with control_connection(client, name=name, target="exec-node" if execution else target) as control:
            try:
                await _send(control, {"type": "auth", "token": client.token, "transport": transport, **(execution or {})})
                opened = await _receive(control, timeout=15)
                if opened.get("type") != "open":
                    raise LumaError("Control did not open a terminal session")
                if execution and opened.get("mode") != "exec":
                    raise LumaError("Control does not support exec-v1; update Control before running commands")
                route = opened.get("transport", "relay")
                if route == "relay":
                    if transport == "direct":
                        raise LumaError("Control or node agent needs an update for direct terminals")
                else:
                    offer = await _receive(control, timeout=10)
                    if offer.get("type") not in {"direct-offer", "relay-ready"}:
                        raise LumaError("node did not offer a direct terminal")
                    try:
                        if offer.get("type") == "relay-ready":
                            raise LumaError("direct listener is unavailable")
                        direct = await connect_direct(offer)
                    except LumaError:
                        if transport == "direct":
                            raise
                        print("luma: direct connection unavailable; using Control relay", file=sys.stderr)
                        if offer.get("type") != "relay-ready":
                            await _send(control, {"type": "relay"})
                            ready = await _receive(control, timeout=5)
                            if ready.get("type") != "relay-ready":
                                raise LumaError("node did not accept relay fallback")
                    else:
                        # Once authentication is sent, a shell may have started. Never
                        # silently open a second shell if its acknowledgement is lost.
                        await _send(direct, {"type": "auth", "sessionId": offer["sessionId"], "ticket": offer["ticket"]})
                        ready = await _receive(direct, timeout=5)
                        if ready.get("type") != "open":
                            raise LumaError("node did not accept the direct terminal ticket")
                actual = "direct" if direct else "relay"
                peer = direct.transport.get_extra_info("peername") if direct else None
                address = f" ({peer[0]}:{peer[1]})" if peer else ""
                if diagnose:
                    control_peer = control.transport.get_extra_info("peername")
                    samples = []
                    try:
                        for _ in range(3):
                            pong = await control.ping()
                            samples.append(round(1000 * await asyncio.wait_for(pong, 2), 1))
                        print(f"luma: route={actual}{address}; Control peer={control_peer}; Control RTT ms={samples}", file=sys.stderr)
                    except asyncio.TimeoutError:
                        print(f"luma: route={actual}{address}; Control peer={control_peer}; Control ping timed out", file=sys.stderr)
                if execution:
                    return await interact_command(direct or control, control=control if direct else None,
                                                  forward_stdin=forward_stdin)
                print(f"luma: connected to {name} via {actual}{address}; Ctrl-] disconnects", file=sys.stderr, flush=True)
                return await interact(direct or control, control=control if direct else None)
            finally:
                if direct:
                    await direct.close()
                with contextlib.suppress(Exception):
                    await _send(control, {"type": "close"})
    except LumaError:
        raise
    except Exception as exc:
        # Don't print frames, handshake headers or credentials from transport errors.
        raise LumaError(f"terminal connection ended ({type(exc).__name__}); check Control and node connectivity") from exc


async def _read_stdin() -> bytes:
    fd = sys.stdin.fileno()
    if stat.S_ISREG(os.fstat(fd).st_mode):
        return os.read(fd, CHUNK_SIZE)
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    def readable():
        if not ready.done():
            try:
                ready.set_result(os.read(fd, CHUNK_SIZE))
            except Exception as exc:
                ready.set_exception(exc)
    loop.add_reader(fd, readable)
    try:
        return await ready
    finally:
        loop.remove_reader(fd)


async def _watch_control_lease(control) -> None:
    from websockets.exceptions import ConnectionClosedOK
    try:
        while True:
            await _receive(control)
    except ConnectionClosedOK:
        # The direct exit frame can race Control's normal close. Drain briefly,
        # but never keep an unleased direct command alive indefinitely.
        await asyncio.sleep(1)
        raise LumaError("Control session closed before the terminal exit was received")


async def interact_command(websocket, *, control=None, forward_stdin=False) -> int:
    from websockets.exceptions import ConnectionClosed
    credits = asyncio.Semaphore(WINDOW)
    async def send_input():
        try:
            if forward_stdin:
                while True:
                    await credits.acquire()
                    data = await _read_stdin()
                    if not data:
                        break
                    await _send(websocket, {"type": "stdin", "data": base64.b64encode(data).decode("ascii")})
            await _send(websocket, {"type": "stdin-eof"})
        except ConnectionClosed:
            # A fast command may finish without reading any stdin.
            pass

    async def receive_output():
        while True:
            message = await _receive(websocket)
            if message.get("type") == "stdin-ack":
                credits.release()
            elif message.get("type") == "exec-output":
                data = base64.b64decode(message.get("data", ""), validate=True)
                stream = sys.stdout if message.get("stream") == "stdout" else sys.stderr
                stream.buffer.write(data)
                stream.buffer.flush()
                with contextlib.suppress(ConnectionClosed):
                    await _send(websocket, {"type": "exec-ack"})
            elif message.get("type") == "exit":
                if message.get("timedOut"):
                    print("luma: remote command timed out", file=sys.stderr, flush=True)
                code = int(message.get("exitCode", 255))
                return code if 0 <= code <= 255 else 255

    sender = asyncio.create_task(send_input())
    receiver = asyncio.create_task(receive_output())
    tasks = [sender, receiver]
    if control:
        tasks.append(asyncio.create_task(_watch_control_lease(control)))
    pending = set(tasks)
    try:
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                return receiver.result()
            for task in done:
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _size() -> dict[str, Any]:
    try:
        columns, rows = os.get_terminal_size(sys.stdout.fileno())
    except OSError:
        columns, rows = 120, 32
    return {"type": "resize", "cols": columns, "rows": rows}


async def interact(websocket: Any, *, control: Any = None) -> int:
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    queue: asyncio.Queue = asyncio.Queue()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    installed_signals = {}
    tasks = []

    def read_input():
        data = os.read(fd, 4096)
        if not data:
            queue.put_nowait(None)
            loop.remove_reader(fd)
            return
        before, separator, _ = data.partition(b"\x1d")
        text = decoder.decode(before)
        if text:
            queue.put_nowait({"type": "input", "data": text})
        if separator:
            queue.put_nowait(None)
            loop.remove_reader(fd)

    async def send_input():
        while True:
            message = await queue.get()
            if message is None:
                return 0
            await _send(websocket, message)

    async def receive_output():
        while True:
            message = await _receive(websocket)
            if message.get("type") == "output":
                sys.stdout.write(str(message.get("data") or ""))
                sys.stdout.flush()
            elif message.get("type") == "exit":
                code = int(message.get("exitCode", -1))
                return code if 0 <= code <= 255 else 1

    try:
        await _send(websocket, _size())
        tty.setraw(fd)
        loop.add_reader(fd, read_input)
        for signum in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP):
            installed_signals[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, lambda sig=signum: queue.put_nowait(_size() if sig == signal.SIGWINCH else None))
        tasks = [asyncio.create_task(receive_output()), asyncio.create_task(send_input())]
        if control:
            tasks.append(asyncio.create_task(_watch_control_lease(control)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Prefer the actual remote exit status if input/Control closes concurrently.
        for task in tasks:
            if task in done:
                return task.result()
        return 1
    finally:
        loop.remove_reader(fd)
        for signum, handler in installed_signals.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, handler)
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print(file=sys.stderr, flush=True)
