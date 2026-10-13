"""Database failure classification and idempotent receipt persistence."""
from __future__ import annotations

import logging
import sqlite3
import time

from ..errors import DatabaseBusyError, LumaError

LOG = logging.getLogger(__name__)


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


def persist_receipt(write, *, owner: str):
    """Retry only a captured, idempotent outcome; never replay external work.

    Keep the executor/fence until persistence succeeds. A process shutdown may
    interrupt this wait; normal restart reconciliation then reports an
    interrupted operation requiring inspection, not a new execution attempt.
    """
    attempt = 0
    while True:
        try:
            return write()
        except Exception as exc:
            if not is_transient_database_error(exc):
                raise
            delay = min(5, 2 ** min(attempt, 3))
            attempt += 1
            LOG.warning("Control receipt pending: owner=%s attempt=%s retry_in=%ss", owner, attempt, delay)
            time.sleep(delay)
