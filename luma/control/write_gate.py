"""FIFO admission to this process's single Control database writer.

SQLite still arbitrates with other processes. This gate prevents local polling
threads from repeatedly overtaking a waiting deployment in SQLite's busy loop.
The caller's deadline includes both local admission and SQLite lock acquisition.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager

from ..errors import LumaError


class WriterGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.pending: deque[object] = deque()
        self.owner_thread: int | None = None
        self.owner = ""

    @contextmanager
    def acquire(self, owner: str, *, timeout: float = 30):
        deadline = time.monotonic() + timeout
        ticket = object()
        thread = threading.get_ident()
        with self.condition:
            if self.owner_thread == thread:
                raise LumaError(f"nested Control write transaction: {owner}; already writing in {self.owner}")
            self.pending.append(ticket)
            self.condition.notify_all()
            try:
                while self.owner_thread is not None or self.pending[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # Receipt cleanup recognizes locked/busy failures and
                        # retries only persistence, never the deployment.
                        raise LumaError(f"Control database is locked: writer wait timed out: {owner}; current writer: {self.owner}")
                    self.condition.wait(remaining)
                self.pending.popleft()
                self.owner_thread, self.owner = thread, owner
            except BaseException:
                if ticket in self.pending:
                    self.pending.remove(ticket)
                elif self.owner_thread == thread:
                    self.owner_thread, self.owner = None, ""
                self.condition.notify_all()
                raise
        try:
            yield deadline
        finally:
            with self.condition:
                self.owner_thread, self.owner = None, ""
                self.condition.notify_all()
