from __future__ import annotations

import asyncio
import unittest

from lae_api.app import _database_ready


class _Result:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _Connection:
    def __init__(
        self,
        *,
        value: int = 1,
        error: Exception | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.value = value
        self.error = error
        self.delay_seconds = delay_seconds
        self.statement: str | None = None

    async def exec_driver_sql(self, statement: str) -> _Result:
        self.statement = statement
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return _Result(self.value)


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)


class DatabaseReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_adapters_without_engine_remain_ready(self) -> None:
        self.assertTrue(await _database_ready(None))

    async def test_postgres_readiness_executes_bounded_query(self) -> None:
        connection = _Connection()

        self.assertTrue(await _database_ready(_Engine(connection)))
        self.assertEqual(connection.statement, "SELECT 1")

    async def test_postgres_error_fails_readiness_closed(self) -> None:
        engine = _Engine(_Connection(error=RuntimeError("database unavailable")))

        self.assertFalse(await _database_ready(engine))

    async def test_postgres_timeout_fails_readiness_closed(self) -> None:
        engine = _Engine(_Connection(delay_seconds=0.05))

        self.assertFalse(await _database_ready(engine, timeout_seconds=0.001))
