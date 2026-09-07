"""W5-14: the analytics queries, executed rather than reviewed (X-01, C-07).

``Q5`` is the reason this file exists. As published in
``schema-and-queries.md`` it does not run at all -- three independent errors --
and the failure mode is the cruel one: the runbook-efficacy job would execute,
return nothing, and look exactly like a system with no dead steps. A query that
is only ever read is a query nobody has checked.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.config.settings import Settings
from incidentpilot.db import queries
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.runbooks import detector
from incidentpilot.runbooks.library import RunbookLibrary

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)

AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

# Six incidents on one runbook: above Q5's production threshold of five, which
# is the filter E-3 says the auto-PR job will not clear inside eight weeks.
USES = 6


@pytest_asyncio.fixture(scope="module")
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(Settings(environment="test", database_url=DB_URL))
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no PostgreSQL reachable at {DB_URL}")
    yield build_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[int]:
    """Six mitigated incidents on one runbook.

    Every incident follows the first two steps and nobody ever runs
    ``never-run-step`` -- the shape a dead step actually has in the wild, which
    is what ``Q5`` is supposed to surface.
    """
    async with sessions() as session:
        await session.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        await session.execute(text("DELETE FROM runbook_step_signals"))
        await session.execute(text("DELETE FROM runbooks WHERE name = 'Efficacy Fixture'"))
        runbook_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO runbooks (name, alert_pattern, body, step_ids)"
                        " VALUES ('Efficacy Fixture', 'Fixture', 'body',"
                        " ARRAY['first-step','second-step','never-run-step'])"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
        )

        for index in range(USES):
            incident_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO incidents (public_key, dedup_key, title, severity,"
                            " state, detected_at, mitigated_at, runbook_id)"
                            " VALUES (:key, :key, 'Fixture', 'sev2', 'mitigated', :start,"
                            " :end, :rb) RETURNING id"
                        ),
                        {
                            "key": f"inc-fixture-{index}",
                            "start": AT - timedelta(hours=index + 1),
                            "end": AT - timedelta(hours=index, minutes=30),
                            "rb": runbook_id,
                        },
                    )
                ).scalar_one()
            )
            for step_id in ("first-step", "second-step"):
                await session.execute(
                    text(
                        "INSERT INTO runbook_step_signals"
                        " (incident_id, runbook_id, step_id, detected_by)"
                        " VALUES (:i, :rb, :s, 'command_match')"
                    ),
                    {"i": incident_id, "rb": runbook_id, "s": step_id},
                )
        await session.commit()
        yield runbook_id


# --- Q5 (X-01) ----------------------------------------------------------------


async def test_dead_step_query_runs(
    sessions: async_sessionmaker[AsyncSession], seeded: int
) -> None:
    """The corrected query executes and finds the step nobody runs.

    The published version raises before it can find anything: it expands
    ``step_ids`` twice, references an alias from a different expansion inside a
    FILTER, and leaves ``r.name`` ambiguous in the outer query.
    """
    async with sessions() as session:
        rows = await queries.dead_steps(session)

    dead = {row["step"] for row in rows}
    assert "never-run-step" in dead
    assert "first-step" not in dead
    assert next(r for r in rows if r["step"] == "never-run-step")["skip_rate"] == 1


async def test_a_step_that_is_usually_followed_is_not_dead(
    sessions: async_sessionmaker[AsyncSession], seeded: int
) -> None:
    """The 80% threshold is what stops "someone skipped it once" being a finding."""
    async with sessions() as session:
        rows = await queries.dead_steps(session, skip_threshold=0.8)
    assert all(row["step"] != "second-step" for row in rows)


async def test_the_min_uses_filter_is_why_the_auto_pr_job_is_deferred(
    sessions: async_sessionmaker[AsyncSession], seeded: int
) -> None:
    """E-3, made concrete rather than asserted in prose.

    Raise the threshold above the seeded uses and the query returns nothing --
    which is exactly what the auto-PR job would do for eight weeks, because
    eight weeks will not produce five incidents per runbook. The signal capture
    and the query ship now; the loop has not closed, and that is the honest
    thing to say.
    """
    async with sessions() as session:
        assert await queries.dead_steps(session, min_uses=USES + 1) == []


# --- Q4 (C-07) ----------------------------------------------------------------


async def test_runbook_efficacy_counts_per_step_rows(
    sessions: async_sessionmaker[AsyncSession], seeded: int
) -> None:
    """C-07: adherence is per-step rows over declared steps.

    Rev 2's query reads ``rs.steps_followed`` and ``rs.steps_total``, columns
    that do not exist on a table defined as one row per executed step.
    """
    async with sessions() as session:
        rows = await queries.runbook_efficacy(session, min_uses=3)

    fixture = next(r for r in rows if r["name"] == "Efficacy Fixture")
    assert fixture["uses"] == USES
    # Two of three steps followed on every incident.
    assert fixture["adherence"] == pytest.approx(2 / 3)
    assert fixture["median_ttm_min"] == pytest.approx(30.0)


# --- the detector's dedup (W5-13) ---------------------------------------------


async def test_step_signal_deduped(sessions: async_sessionmaker[AsyncSession], seeded: int) -> None:
    """Three sources, one row.

    A responder who runs the command, reacts to the pinned runbook and types
    ``/step done`` has followed that step once. Three rows would inflate
    adherence, and a metric that flatters itself is worse than no metric.
    """
    async with sessions() as session, session.begin():
        incident_id = int(
            (
                await session.execute(text("SELECT id FROM incidents ORDER BY id LIMIT 1"))
            ).scalar_one()
        )
        first = await detector.record_signal(
            session,
            incident_id=incident_id,
            runbook_id=seeded,
            signal=detector.StepSignal("never-run-step", detector.COMMAND_MATCH),
        )
        second = await detector.record_signal(
            session,
            incident_id=incident_id,
            runbook_id=seeded,
            signal=detector.StepSignal("never-run-step", detector.REACTION),
        )
        third = await detector.record_signal(
            session,
            incident_id=incident_id,
            runbook_id=seeded,
            signal=detector.StepSignal("never-run-step", detector.SLASH_COMMAND),
        )

    assert first is True
    assert second is False
    assert third is False

    async with sessions() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM runbook_step_signals"
                    " WHERE incident_id = :i AND step_id = 'never-run-step'"
                ),
                {"i": incident_id},
            )
        ).scalar_one()
    assert count == 1


# --- the library sync (W5-10) -------------------------------------------------


async def test_runbooks_sync_from_disk_and_are_idempotent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The file in git is the source of truth; the table exists for the FKs.

    Running it twice must not create eight more rows, because it runs at every
    process start and there are three processes.
    """
    library = RunbookLibrary.from_directory("runbooks")
    async with sessions() as session, session.begin():
        first = await library.sync(session)
    async with sessions() as session, session.begin():
        second = await library.sync(session)

    assert len(first) == 8
    assert first == second

    async with sessions() as session:
        stored = (
            await session.execute(
                text("SELECT step_ids FROM runbooks WHERE name = 'Replica Lag / Promotion'")
            )
        ).scalar_one()
    # Written from the parsed markers, which is what keeps Q5's
    # `unnest(r.step_ids)` and the detector's signals talking about one set.
    assert stored[0] == "verify-replica-lag"
