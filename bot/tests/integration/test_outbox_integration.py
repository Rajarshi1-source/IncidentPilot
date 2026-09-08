"""The outbox against a real database.

These are the properties that only exist in Postgres: the unique constraint on
``idempotency_key``, ``FOR UPDATE SKIP LOCKED`` letting two relays work the same
table without coordination, and the deferral / dead-letter state machine.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.chat.base import PermanentChatError, RetryableChatError
from incidentpilot.config.settings import Settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.orchestration.outbox import MAX_ATTEMPTS, OutboxRelay

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)


@pytest_asyncio.fixture(scope="module")
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(environment="test", database_url=DB_URL)
    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no PostgreSQL reachable at {DB_URL}")
    yield build_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def incident_id(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[int]:
    """One minimal incident for outbox rows to hang off."""
    async with sessions() as session:
        await session.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        row = await session.execute(
            text(
                "INSERT INTO incidents (public_key, dedup_key, title, severity, state) "
                "VALUES ('inc-test', 'dk-test', 'Test', 'sev1', 'detected') RETURNING id"
            )
        )
        new_id = int(row.scalar_one())
        await session.commit()
        yield new_id


async def _status_of(sessions: Any, row_id: int) -> tuple[str, int, str | None]:
    async with sessions() as session:
        row = (
            await session.execute(
                text("SELECT status, attempts, last_error FROM outbox_events WHERE id = :i"),
                {"i": row_id},
            )
        ).one()
    return str(row.status), int(row.attempts), row.last_error


async def _enqueue(sessions: Any, incident_id: int, action: str, **kw: Any) -> int:
    async with sessions() as session, session.begin():
        await repo.enqueue_outbox(session, incident_id, action, **kw)
    async with sessions() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT id FROM outbox_events WHERE incident_id = :i AND action = :a "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"i": incident_id, "a": action},
                )
            ).scalar_one()
        )


# --- the constraint -----------------------------------------------------------


async def test_outbox_unique_key(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """The idempotency key is a UNIQUE constraint, not a convention.

    A retried transition must not enqueue the same intent twice, and the
    database is what guarantees it rather than the caller remembering.
    """
    async with sessions() as session, session.begin():
        first = await repo.enqueue_outbox(session, incident_id, "create_channel")
        second = await repo.enqueue_outbox(session, incident_id, "create_channel")

    assert first is True
    assert second is False, "the second enqueue must be a no-op, not an error"

    async with sessions() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE incident_id = :i"),
                {"i": incident_id},
            )
        ).scalar_one()
    assert count == 1


async def test_discriminator_allows_a_deliberate_repeat(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """A storm banner at 5 alerts and again at 10 are different intents."""
    async with sessions() as session, session.begin():
        await repo.enqueue_outbox(session, incident_id, "post_storm_update", discriminator="1")
        await repo.enqueue_outbox(session, incident_id, "post_storm_update", discriminator="2")

    async with sessions() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE action = 'post_storm_update'")
            )
        ).scalar_one()
    assert count == 2


# --- the relay ----------------------------------------------------------------


async def test_relay_dispatches_and_marks_the_row(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    row_id = await _enqueue(sessions, incident_id, "create_channel")

    async def handler(row: dict[str, Any]) -> dict[str, Any]:
        return {"channel_id": "C1"}

    relay = OutboxRelay({"create_channel": handler})
    async with sessions() as session, session.begin():
        assert await relay.relay_once(session) == 1

    status, attempts, error = await _status_of(sessions, row_id)
    assert status == "dispatched"
    assert attempts == 1
    assert error is None


async def test_a_transient_failure_is_deferred_not_lost(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    row_id = await _enqueue(sessions, incident_id, "create_channel")

    async def flaky(row: dict[str, Any]) -> dict[str, Any]:
        raise RetryableChatError("429 slow down")

    relay = OutboxRelay({"create_channel": flaky})
    async with sessions() as session, session.begin():
        await relay.relay_once(session)

    status, attempts, error = await _status_of(sessions, row_id)
    assert status == "pending", "a transient failure returns the row to the pool"
    assert attempts == 1
    assert error is not None and "429" in error


async def test_a_permanent_failure_dies_immediately(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """Retrying a 4xx spends rate-limit budget on a call that cannot succeed."""
    row_id = await _enqueue(sessions, incident_id, "create_channel")

    async def broken(row: dict[str, Any]) -> dict[str, Any]:
        raise PermanentChatError("invalid_auth")

    relay = OutboxRelay({"create_channel": broken})
    async with sessions() as session, session.begin():
        await relay.relay_once(session)

    status, attempts, _ = await _status_of(sessions, row_id)
    assert status == "dead"
    assert attempts == 1, "no retries at all"


async def test_poison_row_reaches_dead(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """Eight attempts, then dead -- surfaced on the dashboard, never silently
    dropped. A DLQ nobody looks at is the same as dropping."""
    row_id = await _enqueue(sessions, incident_id, "create_channel")

    # Isolate this row from anything else pending.
    #
    # `relay_once` claims the OLDEST pending row, so on a database that already
    # holds work -- after the G8 demo has run, say -- every pass here would
    # dispatch somebody else's row and this one would never accumulate an
    # attempt. The test passed for two weeks purely because it ran against a
    # quiet database, which is the same defect this project keeps writing tests
    # about: a green signal that is not measuring what it claims.
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                UPDATE outbox_events SET status = 'dispatched'
                 WHERE status = 'pending' AND id <> :keep
                """
            ),
            {"keep": row_id},
        )

    async def always_fails(row: dict[str, Any]) -> dict[str, Any]:
        raise RetryableChatError("still broken")

    relay = OutboxRelay({"create_channel": always_fails})

    for _ in range(MAX_ATTEMPTS + 2):
        # Clear the backoff so the test does not wait out real jitter.
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE outbox_events SET next_attempt_at = now() WHERE id = :i"),
                {"i": row_id},
            )
        async with sessions() as session, session.begin():
            await relay.relay_once(session)

        status, _, _ = await _status_of(sessions, row_id)
        if status == "dead":
            break

    status, attempts, _ = await _status_of(sessions, row_id)
    assert status == "dead"
    assert attempts >= MAX_ATTEMPTS


async def test_an_unknown_action_dies_rather_than_retrying(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    row_id = await _enqueue(sessions, incident_id, "action_nobody_implemented")

    relay = OutboxRelay({})
    async with sessions() as session, session.begin():
        await relay.relay_once(session)

    status, _, error = await _status_of(sessions, row_id)
    assert status == "dead"
    assert error is not None and "no handler" in error


async def test_one_bad_row_does_not_block_the_batch(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """During a storm the batch is forty rows. Losing thirty-nine because of one
    bad payload would be an expensive way to be tidy."""
    await _enqueue(sessions, incident_id, "create_channel")
    await _enqueue(sessions, incident_id, "pin_runbook")

    async def ok(row: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def bad(row: dict[str, Any]) -> dict[str, Any]:
        raise RetryableChatError("nope")

    relay = OutboxRelay({"create_channel": bad, "pin_runbook": ok})
    async with sessions() as session, session.begin():
        dispatched = await relay.relay_once(session)

    assert dispatched == 1, "the healthy row still went out"


# --- concurrency --------------------------------------------------------------


async def test_skip_locked_lets_two_relays_share_the_table(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """Postgres row locks ARE the coordination primitive.

    This is the direct answer to "why not ZooKeeper": N relay replicas need no
    coordination service, because SKIP LOCKED lets each step past rows another
    holds instead of blocking behind them.
    """
    for i in range(10):
        await _enqueue(sessions, incident_id, "post_storm_update", discriminator=str(i))

    handled: list[str] = []

    def make_handler(worker: str) -> Any:
        async def handler(row: dict[str, Any]) -> dict[str, Any]:
            handled.append(worker)
            await asyncio.sleep(0)
            return {}

        return handler

    async def run(worker: str) -> int:
        relay = OutboxRelay({"post_storm_update": make_handler(worker)}, batch=5)
        async with sessions() as session, session.begin():
            return await relay.relay_once(session)

    a, b = await asyncio.gather(run("a"), run("b"))

    assert a + b == 10, "every row was dispatched exactly once"
    assert len(handled) == 10, "no row was handled twice"

    async with sessions() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE status <> 'dispatched'")
            )
        ).scalar_one()
    assert remaining == 0
