"""All twelve analytics queries execute against a real schema (W8-01).

The point of this file is narrow and worth stating: **a query that is only ever
read is a query nobody has checked.** Three of the reference's twelve did not
run at all when they were first executed -- Q5 had three independent errors, Q7
named a column that does not exist, and both would have failed silently in a
job nobody watched. This runs every one of them.

It asserts *execution and shape*, not row counts. The database here has whatever
the rest of the suite left in it, so asserting "three services appear" would
make this test a hostage to another module's fixtures. What it proves is that
every query parses, binds its parameters, references only columns that exist,
and returns the keys the dashboard reads -- which is exactly the class of bug
that reaches production otherwise.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.config.settings import Settings
from incidentpilot.db import queries
from incidentpilot.db.engine import build_engine, build_session_factory

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)


@pytest_asyncio.fixture(scope="module")
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(Settings(environment="test", database_url=DB_URL))
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def test_all_12_queries_run(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Every query executes. Twelve of them, counted.

    The count is asserted against ``ALL_QUERIES`` so that deleting a query
    fails here rather than quietly shrinking the coverage of a test whose name
    still says twelve.
    """
    assert len(queries.ALL_QUERIES) == 12

    async with sessions() as session:
        # Q1 — the banner.
        active = await queries.active_incidents(session)
        for incident in active:
            assert incident.public_key
            assert incident.elapsed_min >= 0
            # Both the elapsed figure and the start instant: the UI needs the
            # second to tick a timer without re-fetching.
            assert incident.detected_at is not None

        # Q2 — MTTA / TTM / MTTR. TTM stays its own column.
        for mttr in await queries.mttr_by_service(session, days=30):
            assert "service" in mttr
            assert {"p50_tta_s", "p50_ttm_min", "p50_mttr_min", "p95_mttr_min"} <= set(mttr)

        # Q3 — storm compression (D3).
        for week in await queries.storm_compression(session, days=90):
            assert {"week", "incidents", "alerts", "ratio"} <= set(week)

        # Q4 / Q5 — runbook efficacy and dead steps.
        for eff in await queries.runbook_efficacy(session, min_uses=1):
            assert {"name", "uses"} <= set(eff)
        for dead in await queries.dead_steps(session, min_uses=1, skip_threshold=0.0):
            assert {"name", "step", "uses", "skipped", "skip_rate"} <= set(dead)

        # Q6 — repeat incidents. An id that does not exist returns nothing
        # rather than raising: the page must render for an incident with no
        # embedding, which is every incident until it resolves.
        assert await queries.repeat_incidents(session, -1) == []

        # Q7 — the fatigue window.
        from datetime import UTC, datetime

        windows = await queries.fatigue_windows(session, at=datetime(2026, 9, 7, tzinfo=UTC))
        assert isinstance(windows, dict)

        # Q8 — toil.
        for toil in await queries.toil_by_service(session, days=7):
            assert {"service", "incidents", "incident_hours"} <= set(toil)

        # Q9 — grounding compliance, the zero-error-budget SLI.
        for day in await queries.grounding_compliance(session, limit=30):
            assert {"day", "pirs", "fully_grounded", "skeletons"} <= set(day)
            assert day["fully_grounded"] <= day["pirs"]

        # Q10 — action-item half-life.
        for action in await queries.action_half_life(session):
            assert {"priority", "open_now", "median_close_days", "oldest_open_days"} <= set(action)

        # Q11 — cost per PIR, grouped by prompt version AND model.
        for cost in await queries.cost_per_pir(session, days=60):
            assert {"prompt_version", "model", "pirs", "avg_cost_usd"} <= set(cost)

        # Q12 — transcript completeness. A count, never a ratio (ADR 0003).
        for tr in await queries.transcript_completeness(session):
            assert {"public_key", "stored"} <= set(tr)
            assert "ratio" not in tr, (
                "Q12 must not present a fraction -- the denominator needs a "
                "history call we are rate-limited out of (ADR 0003)"
            )


async def test_q9_counts_only_the_published_revision(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A superseded draft must not drag down the SLI that has to read 1.000.

    A rejected attempt and the document that shipped are different things. Q9
    takes the highest revision per incident, so a fabricated-citation rejection
    followed by a clean retry counts once -- as the clean one.
    """
    from sqlalchemy import text

    async with sessions() as session:
        distinct_incidents = (
            await session.execute(text("SELECT count(DISTINCT incident_id) FROM pir_documents"))
        ).scalar_one()
        rows = await queries.grounding_compliance(session, limit=365)
        assert sum(int(r["pirs"]) for r in rows) <= int(distinct_incidents), (
            "Q9 counted more PIRs than there are incidents with one -- "
            "the DISTINCT ON is not doing its job"
        )


async def test_analytics_queries_survive_an_empty_window(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A one-day window on a mostly-empty database returns nothing, not an error.

    The dashboard's empty state is a real state -- a fresh clone has no
    incidents -- and a query that divides by a zero count or chokes on a NULL
    percentile would make the first page a new user sees a 500.
    """
    async with sessions() as session:
        assert await queries.mttr_by_service(session, days=1) is not None
        assert await queries.storm_compression(session, days=1) is not None
        assert await queries.toil_by_service(session, days=1) is not None
        assert await queries.cost_per_pir(session, days=1) is not None
