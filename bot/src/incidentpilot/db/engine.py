"""Async engine and session factory.

Separate pools per concern (API reads, worker writes, relay) are the bulkhead
pattern applied in the data layer: a slow analytics query cannot starve incident
creation. Week 2 only needs the worker pool; the split is established now so
week 8's analytics endpoints inherit it rather than sharing one pool by default.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from incidentpilot.config.settings import Settings


def build_engine(
    settings: Settings,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    statement_timeout_ms: int | None = None,
) -> AsyncEngine:
    """Create an engine with UTC pinned on every connection.

    ``SET timezone = 'UTC'`` is not decoration: the whole product is a timeline,
    and a session that reports local time makes every rendered timestamp subtly
    wrong in a way nobody notices until a postmortem crosses midnight.
    """
    options = ["-c timezone=UTC"]
    if statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={statement_timeout_ms}")

    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,  # a recycled connection after a failover is a 5s stall otherwise
        pool_recycle=1800,
        connect_args={"options": " ".join(options)},
        echo=False,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,  # a committed incident stays readable for logging
        autoflush=False,
    )


@asynccontextmanager
async def unit_of_work(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction spanning the state change and its outbox rows.

    That span is the entire point of the outbox: writing to Slack and then to
    Postgres leaves orphaned channels when the process dies between them, and at
    30 incidents a day "when" is weekly, not hypothetical.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
