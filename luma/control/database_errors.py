"""Database failure classification and idempotent receipt persistence."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from ..errors import DatabaseBusyError, LumaError

LOG = logging.getLogger(__name__)
DEFAULT_RECEIPT_MAX_ATTEMPTS = 60


def is_database_busy(exc: BaseException) -> bool:
    if isinstance(exc, DatabaseBusyError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        return code in (5, 6) or any(  # SQLITE_BUSY / SQLITE_LOCKED, also on Python 3.9.
            word in str(exc).lower() for word in ("locked", "busy")
        )
    return isinstance(exc, LumaError) and exc.__cause__ is not None and is_database_busy(exc.__cause__)


def is_transient_database_error(exc: BaseException) -> bool:
    if is_database_busy(exc):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        return "disk i/o error" in str(exc).lower()
    return isinstance(exc, LumaError) and exc.__cause__ is not None and is_transient_database_error(exc.__cause__)


def persist_receipt(
    write,
    *,
    owner: str,
    max_attempts: int = DEFAULT_RECEIPT_MAX_ATTEMPTS,
    stop_event: threading.Event | None = None,
):
    """Retry only a captured, idempotent outcome; never replay external work.

    Keep the executor/fence until persistence succeeds, bounded by
    ``max_attempts`` and an optional ``stop_event``. A process shutdown or
    permanent disk failure stops retrying so workers do not spin forever;
    normal restart reconciliation then reports an interrupted operation
    requiring inspection, not a new execution attempt.
    """
    attempt = 0
    while True:
        try:
            return write()
        except Exception as exc:
            if not is_transient_database_error(exc):
                raise
            attempt += 1
            if max_attempts > 0 and attempt >= max_attempts:
                LOG.error("Control receipt exhausted retries: owner=%s attempts=%s error=%s", owner, attempt, exc)
                raise
            if stop_event is not None and stop_event.is_set():
                LOG.warning("Control receipt interrupted by stop signal: owner=%s attempt=%s", owner, attempt)
                raise
            delay = min(5, 2 ** min(attempt - 1, 3))
            LOG.warning("Control receipt pending: owner=%s attempt=%s retry_in=%ss", owner, attempt, delay)
            if stop_event is not None:
                if stop_event.wait(delay):
                    LOG.warning("Control receipt interrupted during backoff: owner=%s attempt=%s", owner, attempt)
                    raise
            else:
                time.sleep(delay)
