from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_postgres_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    if not dsn.startswith("postgresql+asyncpg://"):
        raise ValueError("LAE store requires a PostgreSQL postgresql+asyncpg URL")
    return create_async_engine(
        dsn,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=300,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
