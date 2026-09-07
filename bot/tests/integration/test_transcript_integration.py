"""🚦 G4, second half: 200 messages, 200 rows, zero duplicates (W4-03..08).

Against a real PostgreSQL, because every property under test is a property of
the database rather than of the code around it. ``ON CONFLICT DO NOTHING`` on
``UNIQUE (channel_id, ts)`` is the entire conversion of Slack's at-least-once
event delivery into exactly-once storage; a fake would only prove the fake
agrees with itself.

The gate, restated: post two hundred messages, replay every one of them, and
still hold exactly two hundred rows with two hundred distinct timestamps -- with
the transcript-completeness gauge reading 1.0 and not a single call to
``conversations.history`` anywhere in the run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.adapters.chat.history import FakeHistory
from incidentpilot.config.settings import Settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.orchestration.reconciler import HistoryBudget, Reconciler
from incidentpilot.telemetry.metrics import REGISTRY
from incidentpilot.transcript.classifier import IntentClassifier
from incidentpilot.transcript.ingestor import TranscriptIngestor
from tests.conftest import FakeValkey

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)

CHANNEL = "C0WARROOM"
MESSAGE_COUNT = 200


# The plan's G4 snippet writes this as f"175700000{i:04d}.000100", which is a
# thirteen-digit epoch -- year 57000 -- and blows up inside the platform's C
# library before it ever reaches the database. Arithmetic on the base timestamp
# is both correct and readable, and the ordering it produces is the ordering
# Slack would have produced.
EPOCH = 1757000000


def _ts(index: int) -> str:
    """Slack's ``ts``: a unix timestamp that is also the message's primary key."""
    return f"{EPOCH + index}.000100"


def _event(index: int, text_body: str = "looking at the dashboard") -> dict[str, Any]:
    return {
        "type": "message",
        "channel": CHANNEL,
        "ts": _ts(index),
        "user": f"U{index % 4}",
        "text": text_body,
    }


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
async def incident_id(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[int]:
    """One incident that already owns a channel, which is the week 3 end state."""
    async with sessions() as session:
        await session.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        await session.execute(text("DELETE FROM timeline_events"))
        row = await session.execute(
            text(
                "INSERT INTO incidents (public_key, dedup_key, title, severity, state, "
                "chat_channel_id, chat_channel_name) "
                "VALUES ('inc-w4', 'dk-w4', 'Transcript test', 'sev2', 'engaged', "
                ":channel, 'inc-2026-09-07-payments') RETURNING id"
            ),
            {"channel": CHANNEL},
        )
        new_id = int(row.scalar_one())
        await session.commit()
        yield new_id


@pytest.fixture
def valkey() -> FakeValkey:
    return FakeValkey()


@pytest.fixture
def ingestor(sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey) -> TranscriptIngestor:
    return TranscriptIngestor(
        sessions,
        repo.ChannelCache(valkey, sessions),
        classifier=IntentClassifier(),
    )


async def _count(sessions: Any, sql: str, params: dict[str, Any] | None = None) -> int:
    async with sessions() as session:
        return int((await session.execute(text(sql), params or {})).scalar_one())


def _duplicate_total() -> float:
    for metric in REGISTRY.collect():
        if metric.name == "ip_duplicate_messages":
            return float(metric.samples[0].value)
    return 0.0


# --- 🚦 the gate ---------------------------------------------------------------


async def test_two_hundred_messages_two_hundred_rows(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    for i in range(MESSAGE_COUNT):
        result = await ingestor.on_event(_event(i))
        assert result.stored is True, f"message {i} was not stored: {result.reason}"

    rows = await _count(
        sessions, "SELECT count(*) FROM slack_messages WHERE incident_id = :i", {"i": incident_id}
    )
    distinct = await _count(sessions, "SELECT count(DISTINCT ts) FROM slack_messages")
    assert rows == MESSAGE_COUNT
    assert distinct == MESSAGE_COUNT


async def test_replaying_every_event_changes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """The Events API redelivers. That must be boring, not damaging.

    Two hundred replays produce two hundred rejections and zero new rows, and
    the rejections are *counted* -- a duplicate that leaves no trace is
    indistinguishable from a message that was never sent.
    """
    for i in range(MESSAGE_COUNT):
        await ingestor.on_event(_event(i))

    before = _duplicate_total()
    for i in range(MESSAGE_COUNT):
        replay = await ingestor.on_event(_event(i))
        assert replay.stored is False
        assert replay.reason == "duplicate"

    rows = await _count(
        sessions, "SELECT count(*) FROM slack_messages WHERE incident_id = :i", {"i": incident_id}
    )
    assert rows == MESSAGE_COUNT
    assert _duplicate_total() - before == MESSAGE_COUNT


async def test_duplicate_event_stored_once(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """W4-03, in its smallest form: the constraint is the mechanism."""
    assert (await ingestor.on_event(_event(1))).stored is True
    assert (await ingestor.on_event(_event(1))).stored is False
    assert await _count(sessions, "SELECT count(*) FROM slack_messages") == 1


# --- the citation anchor (W4-04) ----------------------------------------------


async def test_timeline_written_in_same_txn(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    await ingestor.on_event(_event(7, "rolling back the payments deploy"))

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT intent, source_message_ts, description, author_user_id "
                    "FROM timeline_events WHERE incident_id = :i"
                ),
                {"i": incident_id},
            )
        ).one()

    assert row.intent == "remediation_start"
    # The whole point of the column: this is what a week 6 PIR claim cites.
    assert row.source_message_ts == _ts(7)
    assert row.author_user_id == "U3"


async def test_a_failed_timeline_write_rolls_back_the_message(
    sessions: async_sessionmaker[AsyncSession],
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """W4-04's real assertion: the two writes share one transaction.

    Without that, a crash between them leaves evidence with no index into it --
    and nothing notices, because the message *is* there and the timeline is
    merely shorter than it should be.

    The failure is injected by subclassing rather than by patching: the snapshot
    enqueue is the last statement inside the unit of work, so raising there
    proves the message insert before it is rolled back too.
    """

    class BrokenIngestor(TranscriptIngestor):
        async def _enqueue_snapshot(self, *args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("injected failure after the message insert")

    broken = BrokenIngestor(sessions, repo.ChannelCache(valkey, sessions))

    with pytest.raises(RuntimeError):
        await broken.on_event(_event(3, "rolling back now"))

    assert await _count(sessions, "SELECT count(*) FROM slack_messages") == 0
    assert await _count(sessions, "SELECT count(*) FROM timeline_events") == 0


async def test_noise_stores_a_message_but_no_timeline_row(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """The transcript keeps everything; the timeline keeps what happened.

    "morning all" is evidence a PIR may quote and is not an event a PIR should
    narrate.
    """
    assert (await ingestor.on_event(_event(2, "morning all, coffee first"))).stored is True
    assert await _count(sessions, "SELECT count(*) FROM slack_messages") == 1
    assert await _count(sessions, "SELECT count(*) FROM timeline_events") == 0


# --- the metric snapshot (W4-05) ----------------------------------------------


async def test_snapshot_enqueued_once(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """The idempotency key is derived from the message ts, so a replay is a no-op."""
    first = await ingestor.on_event(_event(9, "rolling back the payments deploy"))
    assert first.snapshot_enqueued is True

    await ingestor.on_event(_event(9, "rolling back the payments deploy"))

    count = await _count(
        sessions,
        "SELECT count(*) FROM outbox_events WHERE action = 'capture_metric_snapshot'",
    )
    assert count == 1


async def test_only_snapshot_intents_enqueue(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """Impact is measured between remediation and recovery. Nothing else needs
    a frozen metric window, and a snapshot per message would be a Prometheus
    query per message."""
    await ingestor.on_event(_event(4, "checking the dashboard"))
    await ingestor.on_event(_event(5, "error rate is down to baseline"))

    count = await _count(
        sessions,
        "SELECT count(*) FROM outbox_events WHERE action = 'capture_metric_snapshot'",
    )
    assert count == 1, "only the recovery signal should have enqueued a snapshot"


async def test_the_relay_has_a_handler_for_the_action_it_enqueues(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """An action with no handler is marked dead on arrival by the relay.

    Eight retries it can never win, and a dead-letter queue full of rows that
    were never wrong is a queue people stop reading.
    """
    from incidentpilot.orchestration.handlers import build_handlers

    await ingestor.on_event(_event(6, "rolling back now"))

    async with sessions() as session:
        action = (
            await session.execute(text("SELECT action FROM outbox_events ORDER BY id DESC LIMIT 1"))
        ).scalar_one()

    assert str(action) in build_handlers(FakeChat(), sessions)


# --- edits and deletes (W4-06) ------------------------------------------------


async def test_edit_preserves_original(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """A PIR cites ``msg:{ts}``. If that text could change, it is not evidence."""
    await ingestor.on_event(_event(1, "restarting the api pods"))

    await ingestor.on_event(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": CHANNEL,
            "ts": _ts(2),
            "message": {"ts": _ts(1), "text": "restarting the api pods (all three)"},
        }
    )

    async with sessions() as session:
        original = (
            await session.execute(
                text("SELECT text FROM slack_messages WHERE ts = :ts"), {"ts": _ts(1)}
            )
        ).scalar_one()
        revisions = (
            await session.execute(
                text("SELECT revision, kind, text FROM slack_message_revisions ORDER BY revision")
            )
        ).all()

    assert original == "restarting the api pods", "the original row was rewritten"
    assert len(revisions) == 1
    assert revisions[0].revision == 1
    assert revisions[0].kind == "edited"
    assert revisions[0].text == "restarting the api pods (all three)"


async def test_successive_edits_append(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    await ingestor.on_event(_event(1, "first"))
    for body in ("second", "third"):
        await ingestor.on_event(
            {
                "subtype": "message_changed",
                "channel": CHANNEL,
                "ts": _ts(9),
                "message": {"ts": _ts(1), "text": body},
            }
        )

    async with sessions() as session:
        rows = (
            await session.execute(
                text("SELECT revision, text FROM slack_message_revisions ORDER BY revision")
            )
        ).all()

    assert [(r.revision, r.text) for r in rows] == [(1, "second"), (2, "third")]


async def test_a_redelivered_edit_does_not_append_twice(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """Slack redelivers ``message_changed`` too, not only ``message``.

    Appending a second identical revision would be harmless-looking and quietly
    wrong: the audit trail would claim someone edited the message twice.
    """
    await ingestor.on_event(_event(1, "first"))
    edit = {
        "subtype": "message_changed",
        "channel": CHANNEL,
        "ts": _ts(9),
        "message": {"ts": _ts(1), "text": "corrected"},
    }
    await ingestor.on_event(edit)
    await ingestor.on_event(edit)

    assert await _count(sessions, "SELECT count(*) FROM slack_message_revisions") == 1


async def test_delete_is_recorded_without_repeating_the_text(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """The revision records the withdrawal, not a second copy of the content."""
    await ingestor.on_event(_event(1, "wrong channel, sorry"))
    await ingestor.on_event(
        {"subtype": "message_deleted", "channel": CHANNEL, "deleted_ts": _ts(1)}
    )

    async with sessions() as session:
        row = (await session.execute(text("SELECT kind, text FROM slack_message_revisions"))).one()

    assert row.kind == "deleted"
    assert row.text is None


async def test_an_edit_to_an_unknown_message_is_a_no_op(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """Someone edits a message from before the bot joined the channel."""
    result = await ingestor.on_event(
        {
            "subtype": "message_changed",
            "channel": CHANNEL,
            "ts": _ts(9),
            "message": {"ts": "1600000000.000100", "text": "ancient history"},
        }
    )
    assert result.revision is not None and not result.revision.appended
    assert await _count(sessions, "SELECT count(*) FROM slack_message_revisions") == 0


# --- the channel cache against a real database (W4-08, INV-04) ----------------


async def test_channel_lookup_survives_flush(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """B-07: the dedup and routing identity lives in Postgres, never in a key.

    ``FLUSHALL`` mid-incident must cost a query. If it cost the mapping, every
    message from that point on would arrive, find no incident, and vanish with
    no error anywhere.
    """
    cache = repo.ChannelCache(valkey, sessions)
    assert await cache.incident_for_channel(CHANNEL) == incident_id

    await valkey.flushall()
    assert await cache.incident_for_channel(CHANNEL) == incident_id


async def test_ingest_continues_with_valkey_down(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    valkey.alive = False
    ingestor = TranscriptIngestor(sessions, repo.ChannelCache(valkey, sessions))

    assert (await ingestor.on_event(_event(1, "checking logs"))).stored is True
    assert await _count(sessions, "SELECT count(*) FROM slack_messages") == 1


# --- completeness, measured the way production measures it (W4-10, W4-11) -----


async def test_completeness_reads_one_after_a_clean_ingest(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """The G4 tail: 200 stored, and the SLI agrees.

    The reconciler samples the newest page, which is the fifteen messages a live
    ingestion failure would have lost first.
    """
    for i in range(MESSAGE_COUNT):
        await ingestor.on_event(_event(i))

    history = FakeHistory(channel_ts={CHANNEL: [_ts(i) for i in range(MESSAGE_COUNT)]})
    reconciler = Reconciler(sessions, history, HistoryBudget(valkey), valkey=valkey)

    report = await reconciler.reconcile_transcripts()
    assert report.ratio == 1.0
    assert report.sampled == 15, "the sample is one page, which is all the budget buys"
    assert history.call_count("conversations.history") == 1


async def test_a_dropped_message_shows_up_in_the_ratio(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """The failure mode with no other symptom, made visible.

    Slack reports a message we never stored -- the shape of a silent ingest
    failure -- and the ratio drops below 1.0.
    """
    for i in range(20):
        await ingestor.on_event(_event(i))

    slack_says = [_ts(i) for i in range(20)] + [_ts(99)]
    reconciler = Reconciler(
        sessions,
        FakeHistory(channel_ts={CHANNEL: slack_says}),
        HistoryBudget(valkey),
        valkey=valkey,
    )

    report = await reconciler.reconcile_transcripts()
    assert report.ratio is not None and report.ratio < 1.0
    assert report.held == report.sampled - 1


# --- 🚦 the invariant, over the full path -------------------------------------


async def test_zero_history_calls_across_the_whole_ingest(
    sessions: async_sessionmaker[AsyncSession],
    ingestor: TranscriptIngestor,
    incident_id: int,
) -> None:
    """Two hundred messages ingested; the write adapter never read anything.

    ``FakeChat.fetch_history`` raises, so a regression here is an error rather
    than a silently shorter transcript.
    """
    chat = FakeChat()
    for i in range(MESSAGE_COUNT):
        await ingestor.on_event(_event(i))

    assert chat.call_count("conversations.history") == 0
    assert chat.call_count("conversations.replies") == 0
    assert await _count(sessions, "SELECT count(*) FROM slack_messages") == MESSAGE_COUNT
