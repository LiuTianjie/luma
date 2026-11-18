"""Bound macOS runner stdout/stderr while retaining ordinary print output."""
from __future__ import annotations

import contextlib
import io
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5


class RunnerLogHandler(RotatingFileHandler):
    def handleError(self, record) -> None:
        # Logging failures must not recursively write through our stderr adapter.
        with contextlib.suppress(OSError):
            sys.__stderr__.write("luma runner: could not write service log\n")


class LogStream(io.TextIOBase):
    def __init__(self, handler: RotatingFileHandler) -> None:
        self.handler = handler

    def write(self, text: str) -> int:
        if text:
            self.handler.handle(logging.LogRecord("luma.runner", logging.INFO, "", 0, text, (), None))
        return len(text)

    def flush(self) -> None:
        self.handler.flush()


@contextlib.contextmanager
def rotating_output(path: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUPS):
    handler = RunnerLogHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.terminator = ""
    # Startup errors from the previous process also count toward this limit.
    if path.stat().st_size >= max_bytes:
        handler.doRollover()
    stream = LogStream(handler)
    try:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            yield
    finally:
        handler.close()
