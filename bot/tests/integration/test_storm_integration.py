"""G2 against a real PostgreSQL 18.6 + TimescaleDB 2.29.2.

The pure-domain tests prove the correlation *decision*. These prove the whole
path: migrations apply, the dedup constraint holds, forty alerts become one
incident row with thirty-nine attached, and exactly one alert carries the root
flag.

Skipped automatically when no database is reachable, so `make test` stays green
on a laptop with nothing running.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.config.graph_loader import load_service_graph
from incidentpilot.config.settings import Settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.db.models import Service
from incidentpilot.domain.normalize import NormalizedAlert, normalize_alertmanager
from incidentpilot.domain.states import InvalidTransition, S
from incidentpilot.orchestration.orchestrator import Orchestrator

pytestmark = pytest.mark.integration

BOT_DIR = Path(__file__).resolve().parents[2]
FIXTURES = BOT_DIR / "tests" / "fixtures"
# Default matches the port docker-compose publishes, not the container-internal
# 5432 -- a developer who ran `docker compose up` should be able to run the
# integration suite with no environment variables at all. CI overrides this
# because Actions service containers publish on the standard port.
DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)
GRAPH = load_service_graph()


@dataclass(frozen=True)
class Cfg:
    correlation_window_s: int = 300
    merge_threshold: float = 0.62
    storm_threshold: int = 5


def _storm(n: int) -> list[NormalizedAlert]:
    payload = json.loads((FIXTURES / f"storm_{n}.json").read_text(encoding="utf-8"))
    return [normalize_alertmanager(a, payload) for a in payload["alerts"]]


@pytest_asyncio.fixture(scope="module")
async def migrated_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Apply migrations from empty, then hand back a session factory.

    Runs the real `alembic upgrade head` rather than `metadata.create_all`:
    hypertables, compression policies and generated columns only exist in the
    migrations, so create_all would produce a schema that passes tests and does
    not match production.
    """
    settings = Settings(environment="test", database_url=DB_URL)
    engine = build_engine(settings)

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no PostgreSQL reachable at {DB_URL}")

    env = {**os.environ, "IP_DATABASE_URL": DB_URL}
    subprocess.run(
        ["uv", "run", "alembic", "downgrade", "base"],
        cwd=BOT_DIR,
        env=env,
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        await engine.dispose()
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")

    yield build_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    migrated_db: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A clean slate per test, with the service catalogue seeded."""
    async with migrated_db() as s:
        await s.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        await s.execute(text("TRUNCATE services RESTART IDENTITY CASCADE"))
        for name in sorted(GRAPH.services()):
            s.add(Service(name=name, team="platform", tier=1 if "postgres" in name else 2))
        await s.commit()
        yield s


@pytest.fixture
def orchestrator() -> Orchestrator:
    return Orchestrator(GRAPH, Cfg())


# --- schema -------------------------------------------------------------------


async def test_schema_has_no_naked_timestamp(session: AsyncSession) -> None:
    """INV-08. This product's entire output is a timeline and responders sit in
    IST, PT and CET; a naive column is a data-corruption bug waiting to happen."""
    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND data_type='timestamp without time zone'"
            )
        )
    ).all()
    assert rows == [], f"naked TIMESTAMP columns: {rows}"


async def test_generated_columns_compute(session: AsyncSession, orchestrator: Orchestrator) -> None:
    """MTTR is the schema's job. There is exactly one definition and nothing can
    drift from it."""
    alert = _storm(5)[0]
    outcome = await orchestrator.ingest(session, alert)
    await session.flush()

    await session.execute(
        text(
            "UPDATE incidents SET acknowledged_at = detected_at + interval '90 seconds', "
            "mitigated_at = detected_at + interval '11 minutes', "
            "resolved_at = detected_at + interval '20 minutes' WHERE id = :i"
        ),
        {"i": outcome.incident_id},
    )
    row = (
        await session.execute(
            text("SELECT tta_seconds, ttm_seconds, mttr_seconds FROM incidents WHERE id = :i"),
            {"i": outcome.incident_id},
        )
    ).one()
    assert row.tta_seconds == 90
    assert row.ttm_seconds == 660
    assert row.mttr_seconds == 1200


async def test_every_hypertable_has_a_compression_policy(session: AsyncSession) -> None:
    """Stops a hypertable quietly growing forever because someone forgot a
    policy in a later migration."""
    rows = (
        await session.execute(
            text(
                "SELECT h.hypertable_name FROM timescaledb_information.hypertables h "
                "WHERE NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs j "
                "WHERE j.hypertable_name = h.hypertable_name "
                "AND j.proc_name = 'policy_compression')"
            )
        )
    ).all()
    assert rows == [], f"hypertables without a compression policy: {rows}"


# --- dedup and epochs ---------------------------------------------------------


async def test_dedup_epoch_advances(session: AsyncSession, orchestrator: Orchestrator) -> None:
    """X-03: every source explains that the epoch bumps when an incident closes;
    none contained the code that writes it."""
    alert = _storm(5)[0]

    first = await orchestrator.ingest(session, alert)
    await session.flush()
    assert await repo.next_dedup_epoch(session, alert.dedup_key) == 1

    await session.execute(
        text("UPDATE incidents SET state = 'closed' WHERE id = :i"), {"i": first.incident_id}
    )
    await session.flush()

    second = await orchestrator.ingest(session, alert)
    await session.flush()

    assert second.created, "a closed incident must not absorb a fresh firing"
    assert second.incident_id != first.incident_id

    epochs = (
        (
            await session.execute(
                text("SELECT dedup_epoch FROM incidents WHERE dedup_key = :k ORDER BY dedup_epoch"),
                {"k": alert.dedup_key},
            )
        )
        .scalars()
        .all()
    )
    assert epochs == [0, 1]


async def test_dedup_survives_cache_flush(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """INV-04: the arbiter is a database constraint, not a cache key. There is
    no cache in this path at all, which is the point."""
    alert = _storm(5)[0]
    first = await orchestrator.ingest(session, alert)
    await session.flush()
    again = await orchestrator.ingest(session, alert)
    await session.flush()

    assert again.incident_id == first.incident_id
    assert not again.created


async def test_alert_refire_is_counted_not_lost(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """X-02: Alertmanager re-sends with the SAME startsAt, so the unique
    constraint correctly rejects it -- but a plain DO NOTHING makes the retry
    invisible. 'Re-sent four times before we mitigated' is a real PIR sentence."""
    alert = _storm(5)[0]
    outcome = await orchestrator.ingest(session, alert)
    await session.flush()

    for _ in range(3):
        await orchestrator.ingest(session, alert)
    await session.flush()

    row = (
        await session.execute(
            text("SELECT seen_count FROM alerts WHERE incident_id = :i"),
            {"i": outcome.incident_id},
        )
    ).one()
    assert row.seen_count == 4

    count = (
        await session.execute(
            text("SELECT count(*) FROM alerts WHERE incident_id = :i"),
            {"i": outcome.incident_id},
        )
    ).scalar_one()
    assert count == 1, "the re-fire must not create a second alert row"


# --- G2: the storm ------------------------------------------------------------


@pytest.mark.parametrize("n", [5, 12, 40])
async def test_storm_compresses_to_one_incident(
    session: AsyncSession, orchestrator: Orchestrator, n: int
) -> None:
    """G2, first criterion, against a real database."""
    for alert in _storm(n):
        outcome = await orchestrator.ingest(session, alert)
        await orchestrator.refresh_root_signal(session, outcome.incident_id)
    await session.flush()

    incidents = (
        await session.execute(
            text("SELECT count(*) FROM incidents WHERE parent_incident_id IS NULL")
        )
    ).scalar_one()
    assert incidents == 1, f"storm_{n} produced {incidents} incidents"

    correlated = (
        await session.execute(text("SELECT correlated_alert_count FROM incidents LIMIT 1"))
    ).scalar_one()
    assert correlated == n

    roots = (
        await session.execute(text("SELECT count(*) FROM alerts WHERE is_root_signal"))
    ).scalar_one()
    assert roots == 1, "exactly one alert carries the root flag"

    root_service = (
        await session.execute(text("SELECT service FROM alerts WHERE is_root_signal"))
    ).scalar_one()
    assert root_service == "postgres-primary", (
        "the root signal must be the deepest failing dependency, not the loudest symptom"
    )


async def test_storm_records_merge_reasons(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """Explainability is the differentiator over opaque commercial grouping."""
    for alert in _storm(12):
        await orchestrator.ingest(session, alert)
    await session.flush()

    rows = (
        await session.execute(
            text("SELECT merge_score, merge_reasons FROM alerts WHERE merge_score IS NOT NULL")
        )
    ).all()
    assert rows, "merged alerts must record why"
    for score, reasons in rows:
        assert float(score) >= 0.62
        assert reasons, "a merge with no reasons is opaque grouping"


async def test_storm_emits_a_throttled_banner(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """C-11: storm_threshold gates the banner and the metric.

    Throttled by content (one per multiple of five) rather than by a timer, so
    a 40-alert cascade posts eight updates rather than thirty-five -- and the
    throttling stays deterministic for replay.
    """
    for alert in _storm(40):
        await orchestrator.ingest(session, alert)
    await session.flush()

    banners = (
        await session.execute(
            text("SELECT count(*) FROM outbox_events WHERE action = 'post_storm_update'")
        )
    ).scalar_one()
    assert 1 <= banners <= 8, f"expected a throttled banner count, got {banners}"


# --- transitions --------------------------------------------------------------


async def test_transition_appends_an_audit_row(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()

    await orchestrator.transition(session, outcome.incident_id, S.TRIAGING, actor="system")
    await session.flush()

    row = (
        await session.execute(
            text(
                "SELECT from_state, to_state, seq, actor FROM incident_transitions "
                "WHERE incident_id = :i"
            ),
            {"i": outcome.incident_id},
        )
    ).one()
    assert (row.from_state, row.to_state, row.seq, row.actor) == (
        "detected",
        "triaging",
        1,
        "system",
    )


async def test_engagement_enqueues_its_outbox_actions(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """The state change and the side-effect intents commit together. That
    atomicity is the whole point of the outbox (B-08)."""
    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()
    await orchestrator.transition(session, outcome.incident_id, S.TRIAGING, actor="system")
    await orchestrator.transition(session, outcome.incident_id, S.ENGAGED, actor="system")
    await session.flush()

    actions = (
        (
            await session.execute(
                text("SELECT action FROM outbox_events WHERE incident_id = :i ORDER BY action"),
                {"i": outcome.incident_id},
            )
        )
        .scalars()
        .all()
    )
    for expected in ("create_channel", "invite_responders", "pin_runbook", "start_timer"):
        assert expected in actions


async def test_invalid_transition_raises(session: AsyncSession, orchestrator: Orchestrator) -> None:
    """G2, second criterion, at the database boundary rather than in pure code."""
    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()

    with pytest.raises(InvalidTransition):
        await orchestrator.transition(session, outcome.incident_id, S.RESOLVED, actor="system")


async def test_timing_stamp_is_written_on_acknowledge(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()
    for nxt in (S.TRIAGING, S.ENGAGED, S.ACKNOWLEDGED):
        await orchestrator.transition(session, outcome.incident_id, nxt, actor="U123")
    await session.flush()

    row = (
        await session.execute(
            text("SELECT acknowledged_at, tta_seconds FROM incidents WHERE id = :i"),
            {"i": outcome.incident_id},
        )
    ).one()
    assert row.acknowledged_at is not None
    assert row.tta_seconds is not None


async def test_seq_constraint_prevents_a_double_advance(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """UNIQUE (incident_id, seq) is what catches concurrency error at 3 a.m.

    Simulated by writing the row a second worker would have written, then
    letting the orchestrator try to write the same sequence number.
    """
    from sqlalchemy.exc import IntegrityError

    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()

    await session.execute(
        text(
            "INSERT INTO incident_transitions (incident_id, seq, from_state, to_state, actor) "
            "VALUES (:i, 1, 'detected', 'triaging', 'other-worker')"
        ),
        {"i": outcome.incident_id},
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await orchestrator.transition(session, outcome.incident_id, S.TRIAGING, actor="system")
        await session.flush()


async def test_load_open_incidents_excludes_terminal(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()
    assert len(await repo.load_open_incidents(session)) == 1

    await session.execute(
        text("UPDATE incidents SET state = 'closed' WHERE id = :i"), {"i": outcome.incident_id}
    )
    await session.flush()
    assert await repo.load_open_incidents(session) == []


async def test_outbox_enqueue_is_idempotent(
    session: AsyncSession, orchestrator: Orchestrator
) -> None:
    """The idempotency key is what licenses the retry: without it, a replayed
    transition creates a second Slack channel."""
    outcome = await orchestrator.ingest(session, _storm(5)[0])
    await session.flush()

    assert await repo.enqueue_outbox(session, outcome.incident_id, "create_channel") is True
    assert await repo.enqueue_outbox(session, outcome.incident_id, "create_channel") is False
    await session.flush()

    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE incident_id = :i AND action = 'create_channel'"
            ),
            {"i": outcome.incident_id},
        )
    ).scalar_one()
    assert count == 1


async def test_compression_ratio_is_sane(session: AsyncSession) -> None:
    """INV-09 / B-05, measured rather than asserted in a comment.

    Segmenting by the highest-cardinality column produces one tiny batch per
    incident and can end up LARGER than uncompressed after per-batch overhead.
    If this ratio is poor, `segmentby` is wrong.
    """
    await session.execute(
        text(
            """
            -- Seeded 4-5 days back on purpose. chunk_time_interval is 1 day, so
            -- data written around now() lands in the CURRENT chunk, which
            -- `show_chunks(older_than => 0)` will not return -- the test then
            -- skips, and a gate that fails on skips becomes intermittently red
            -- for reasons that have nothing to do with the code.
            INSERT INTO signal_samples (time, incident_id, series, service, value)
            SELECT now() - interval '4 days' - (s || ' seconds')::interval,
                   (s % 5) + 1,
                   (ARRAY['http_5xx_rate','p99_latency_ms','pods_unready'])[(s % 3) + 1],
                   (ARRAY['payments-api','postgres-primary'])[(s % 2) + 1],
                   random() * 100
            FROM generate_series(1, 60000) s
            """
        )
    )
    await session.commit()

    chunks: Any = (
        (
            await session.execute(
                text("SELECT show_chunks('signal_samples', older_than => INTERVAL '0 seconds')")
            )
        )
        .scalars()
        .all()
    )
    if not chunks:
        pytest.skip("no chunk old enough to compress")

    await session.execute(text(f"SELECT compress_chunk('{chunks[0]}', if_not_compressed => true)"))
    await session.commit()

    ratio = (
        await session.execute(
            text(
                "SELECT before_compression_total_bytes::float "
                "/ NULLIF(after_compression_total_bytes, 0) "
                "FROM chunk_compression_stats('signal_samples') "
                "WHERE compression_status = 'Compressed' LIMIT 1"
            )
        )
    ).scalar()

    assert ratio is not None, "no compressed chunk found"
    assert ratio >= 4.0, f"compression ratio {ratio:.1f}x is poor -- check segmentby"
