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
    idle_in_transaction_ms: int | None = None,
) -> AsyncEngine:
    """Create an engine with UTC pinned on every connection.

    ``SET timezone = 'UTC'`` is not decoration: the whole product is a timeline,
    and a session that reports local time makes every rendered timestamp subtly
    wrong in a way nobody notices until a postmortem crosses midnight.
    """
    options = ["-c timezone=UTC"]
    if statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={statement_timeout_ms}")

    # A session that opens a transaction and then stops is the worst thing that
    # can happen to this schema.
    #
    # The relay claims outbox rows with `UPDATE ... SET status = 'claimed'`
    # inside a transaction. Kill that process hard -- SIGKILL, an OOM, a node
    # eviction -- and PostgreSQL keeps the transaction open until the TCP
    # connection is reaped, which can be many minutes. For that whole window the
    # claimed rows are locked, `reclaim_stuck` cannot reclaim them (its own
    # UPDATE queues behind the dead one), and anything that needs a table lock
    # on `incidents` blocks too. The system does not error; it simply stops.
    #
    # Observed while running the gates: a killed test run left one `idle in
    # transaction` session and every later run hung until the connection was
    # terminated by hand.
    #
    # Sixty seconds is comfortably longer than any legitimate transaction here
    # -- the relay's longest is one external call -- and far shorter than the
    # default TCP keepalive, which is what would otherwise decide.
    #
    # Not applied in dev or test. The crash matrix *deliberately* holds
    # transactions open across a simulated crash and then resumes them, so a
    # timeout there kills the thing under test -- nine of fifteen crash points
    # failed when this was set to five seconds. Production has no such case:
    # the longest legitimate transaction is one external call.
    idle_ms = idle_in_transaction_ms
    if idle_ms is None and not settings.is_dev:
        idle_ms = 60_000
    if idle_ms is not None:
        options.append(f"-c idle_in_transaction_session_timeout={idle_ms}")

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
