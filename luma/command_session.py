"""Non-PTY command processes carried by the authenticated terminal transport."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import math
import os
from pathlib import Path
import signal
from typing import Any


CHUNK_SIZE = 32768
WINDOW = 16


def command_request(value: dict) -> dict:
    argv = value.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > 4096
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0] or sum(len(arg.encode()) for arg in argv) > 131072):
        raise ValueError("exec requires a nonempty argv without NUL bytes (maximum 128 KiB)")
    timeout = value.get("timeoutSeconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError("exec timeout must be greater than zero and at most 86400 seconds")
    return {"mode": "exec", "argv": argv, "timeoutSeconds": timeout}


class CommandSession:
    def __init__(self, session_id: str, request: dict, *, outbound: asyncio.Queue):
        self.request = command_request(request)
        self.session_id = session_id
        self.outbound = outbound
        self.loop = asyncio.get_running_loop()
        self.process: Any = None
        self.stdin: asyncio.Queue = asyncio.Queue(maxsize=WINDOW + 1)
        self.output_credit = asyncio.Semaphore(WINDOW)
        self.outstanding = 0
        self.eof = False
        self.closing = False
        self.task = self.loop.create_task(self._run())

    def receive(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "exec-ack":
            # Only replenish an outstanding output frame.
            if self.outstanding:
                self.outstanding -= 1
                self.output_credit.release()
        elif kind == "stdin":
            if self.eof:
                raise ValueError("stdin is already closed")
            encoded = message.get("data", "")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK_SIZE + 2) // 3):
                raise ValueError("invalid exec stdin frame")
            data = base64.b64decode(encoded, validate=True)
            if not data or len(data) > CHUNK_SIZE:
                raise ValueError("invalid exec stdin frame")
            self.stdin.put_nowait(data)
        elif kind == "stdin-eof":
            if not self.eof:
                self.eof = True
                self.stdin.put_nowait(None)

    def close(self) -> None:
        def cancel():
            if not self.closing and not self.task.done():
                self.closing = True
                self.task.cancel()
        self.loop.call_soon_threadsafe(cancel)

    async def wait_closed(self) -> None:
        await asyncio.gather(self.task, return_exceptions=True)

    async def emit(self, **message) -> None:
        await self.outbound.put({"sessionId": self.session_id, **message})

    async def _input(self) -> None:
        while True:
            data = await self.stdin.get()
            try:
                if data is None:
                    self.process.stdin.close()
                    return
                self.process.stdin.write(data)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Commands may legitimately ignore stdin.
                pass
            await self.emit(type="stdin-ack")

    async def _output(self, reader, stream: str) -> None:
        while True:
            data = await reader.read(CHUNK_SIZE)
            if not data:
                return
            await self.output_credit.acquire()
            self.outstanding += 1
            await self.emit(type="exec-output", stream=stream, data=base64.b64encode(data).decode("ascii"))

    async def _execute(self) -> int:
        self.process = await asyncio.create_subprocess_exec(
            *self.request["argv"], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.home()) if Path.home().exists() else "/", start_new_session=True,
        )
        tasks = [asyncio.create_task(self._input()),
                 asyncio.create_task(self._output(self.process.stdout, "stdout")),
                 asyncio.create_task(self._output(self.process.stderr, "stderr"))]
        try:
            code = await self.process.wait()
            await asyncio.gather(*tasks[1:])
            return code if code >= 0 else 128 - code
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop(self) -> None:
        if self.process is None:
            return
        # Descendants can outlive their parent and keep stdout/stderr open.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                pass
        finally:
            # A shutdown cancellation must not skip escalation for a child
            # ignoring SIGTERM, even if the event loop is already stopping.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(), 2)

    async def _run(self) -> None:
        finished = False
        try:
            try:
                code = await asyncio.wait_for(self._execute(), self.request["timeoutSeconds"])
            except asyncio.TimeoutError:
                await self._stop()
                finished = True
                await self.emit(type="exit", exitCode=124, timedOut=True)
                return
            except FileNotFoundError:
                await self.emit(type="exec-output", stream="stderr", data=base64.b64encode(b"luma: executable not found\n").decode())
                code = 127
            except PermissionError:
                await self.emit(type="exec-output", stream="stderr", data=base64.b64encode(b"luma: executable is not permitted\n").decode())
                code = 126
            finished = True
            await self.emit(type="exit", exitCode=code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.emit(type="error", message=f"exec failed ({type(exc).__name__})")
        finally:
            if not finished:
                # Supervisor shutdown can cancel us again while we're reaping
                # the process. Keep that cleanup alive through the second cancel.
                cleanup = asyncio.create_task(self._stop())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
