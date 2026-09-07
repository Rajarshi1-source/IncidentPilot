"""W4-03, W4-06, W4-08: the parts of the transcript path that need no database.

The channel cache tests are the INV-04 ones: the cache is an accelerator and the
database is the arbiter, so every failure mode of the cache -- a miss, a flush,
Valkey being unreachable entirely -- must cost latency and nothing else.

The ingestor tests here cover the guards that run *before* the transaction
opens. The storage behaviour itself is in ``tests/integration`` against a real
Postgres, because ``ON CONFLICT DO NOTHING`` is the thing being tested and a
fake would only be testing the fake.
"""

from __future__ import annotations

from typing import Any

import pytest

from incidentpilot.db.repositories import (
    CHANNEL_NEGATIVE_TTL_S,
    NOT_AN_INCIDENT,
    ChannelCache,
)
from incidentpilot.domain.intent import IntentKind
from incidentpilot.transcript.ingestor import TranscriptIngestor
from incidentpilot.transcript.mutations import RevisionResult
from tests.conftest import FakeValkey

CHANNEL = "C0INCIDENT"


class _Result:
    def __init__(self, value: int | None) -> None:
        self._value = value

    def scalars(self) -> _Result:
        return self

    def first(self) -> int | None:
        return self._value


class FakeSession:
    """Answers the one query ``incident_id_for_channel`` issues, and counts it."""

    def __init__(self, answers: dict[str, int], counter: list[str]) -> None:
        self._answers = answers
        self._counter = counter

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self._counter.append("select")
        # The statement is a parameterized select on chat_channel_id; the value
        # is in its bind parameters. Reading it back keeps the fake honest about
        # *which* channel was asked for rather than answering everything.
        binds = statement.compile().params
        channel = next(iter(binds.values())) if binds else None
        return _Result(self._answers.get(str(channel)))

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def session_factory(answers: dict[str, int], counter: list[str]) -> Any:
    def factory() -> FakeSession:
        return FakeSession(answers, counter)

    return factory


@pytest.fixture
def db_calls() -> list[str]:
    return []


@pytest.fixture
def cache(fake_valkey: FakeValkey, db_calls: list[str]) -> ChannelCache:
    return ChannelCache(fake_valkey, session_factory({CHANNEL: 42}, db_calls))


# --- the channel cache (W4-08, INV-04) ----------------------------------------


async def test_miss_falls_through_to_the_database_and_caches(
    cache: ChannelCache, fake_valkey: FakeValkey, db_calls: list[str]
) -> None:
    assert await cache.incident_for_channel(CHANNEL) == 42
    assert db_calls == ["select"]
    assert fake_valkey.keys[f"ip:chan:{CHANNEL}"] == "42"

    assert await cache.incident_for_channel(CHANNEL) == 42
    assert db_calls == ["select"], "second lookup should have been served from cache"


async def test_channel_lookup_survives_flush(
    cache: ChannelCache, fake_valkey: FakeValkey, db_calls: list[str]
) -> None:
    """B-07/INV-04: an eviction must cost a query, never a transcript.

    If the mapping lived only in the cache, ``FLUSHALL`` would silently orphan
    every live incident's messages -- they would arrive, find no incident, and
    be dropped with no error anywhere.
    """
    assert await cache.incident_for_channel(CHANNEL) == 42
    await fake_valkey.flushall()

    assert await cache.incident_for_channel(CHANNEL) == 42
    assert db_calls == ["select", "select"]


async def test_valkey_being_down_does_not_stop_ingest(
    cache: ChannelCache, fake_valkey: FakeValkey
) -> None:
    """A dropped message is unrecoverable; an uncached lookup is a millisecond."""
    fake_valkey.alive = False
    assert await cache.incident_for_channel(CHANNEL) == 42


async def test_unknown_channel_is_negatively_cached_but_briefly(
    fake_valkey: FakeValkey, db_calls: list[str]
) -> None:
    """A six-hour negative cache would blackhole an incident's opening messages.

    ``conversations.create`` returns and the first "what is everyone seeing"
    lands seconds later. Those are the messages a PIR most wants to cite, so the
    negative TTL is a minute, not the positive TTL.
    """
    cache = ChannelCache(fake_valkey, session_factory({}, db_calls))
    assert await cache.incident_for_channel("C0RANDOM") is None
    assert fake_valkey.keys["ip:chan:C0RANDOM"] == NOT_AN_INCIDENT
    assert CHANNEL_NEGATIVE_TTL_S <= 60

    assert await cache.incident_for_channel("C0RANDOM") is None
    assert db_calls == ["select"]


async def test_invalidate_forces_a_re_read(
    cache: ChannelCache, fake_valkey: FakeValkey, db_calls: list[str]
) -> None:
    await cache.incident_for_channel(CHANNEL)
    await cache.invalidate(CHANNEL)
    await cache.incident_for_channel(CHANNEL)
    assert db_calls == ["select", "select"]


# --- ingestor guards (they run before any transaction opens) ------------------


class ExplodingSessions:
    """Any database access is a failure in these cases, so make it loud."""

    def __call__(self) -> Any:
        raise AssertionError("the ingestor opened a transaction it should have skipped")


class RecordingMutations:
    def __init__(self) -> None:
        self.changed: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    async def on_message_changed(self, event: dict[str, Any]) -> RevisionResult:
        self.changed.append(event)
        return RevisionResult("edited", str(event.get("channel")), "1.0", 1)

    async def on_message_deleted(self, event: dict[str, Any]) -> RevisionResult:
        self.deleted.append(event)
        return RevisionResult("deleted", str(event.get("channel")), "1.0", 2)


@pytest.fixture
def guard_ingestor(fake_valkey: FakeValkey, db_calls: list[str]) -> TranscriptIngestor:
    return TranscriptIngestor(
        ExplodingSessions(),  # type: ignore[arg-type]
        ChannelCache(fake_valkey, session_factory({CHANNEL: 42}, db_calls)),
    )


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ({"channel": CHANNEL, "ts": "1.0", "bot_id": "B1", "text": "hi"}, "bot_message"),
        ({"channel": CHANNEL, "ts": "1.0", "subtype": "channel_join"}, "ignored_subtype"),
        ({"ts": "1.0", "text": "hi"}, "malformed_event"),
        ({"channel": CHANNEL, "text": "hi"}, "malformed_event"),
        ({"channel": "C0RANDOM", "ts": "1.0", "text": "hi"}, "not_an_incident_channel"),
        ({"channel": CHANNEL, "ts": "not-a-ts", "text": "hi"}, "malformed_ts"),
    ],
)
async def test_events_that_never_reach_the_database(
    guard_ingestor: TranscriptIngestor, event: dict[str, Any], reason: str
) -> None:
    result = await guard_ingestor.on_message(event)
    assert result.stored is False
    assert result.reason == reason


async def test_bot_messages_are_dropped_so_the_bot_cannot_cite_itself(
    guard_ingestor: TranscriptIngestor,
) -> None:
    """A PIR quoting the bot's own banner as evidence is a closed loop."""
    event = {"channel": CHANNEL, "ts": "1757000000.000100", "bot_id": "B1", "text": "rolling back"}
    assert (await guard_ingestor.on_message(event)).reason == "bot_message"


# --- routing (W4-06) ----------------------------------------------------------


@pytest.fixture
def routing(fake_valkey: FakeValkey, db_calls: list[str]) -> tuple[Any, RecordingMutations]:
    mutations = RecordingMutations()
    ingestor = TranscriptIngestor(
        ExplodingSessions(),  # type: ignore[arg-type]
        ChannelCache(fake_valkey, session_factory({CHANNEL: 42}, db_calls)),
        mutations=mutations,  # type: ignore[arg-type]
    )
    return ingestor, mutations


async def test_edits_route_to_mutations_not_to_storage(
    routing: tuple[Any, RecordingMutations],
) -> None:
    ingestor, mutations = routing
    result = await ingestor.on_event(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": CHANNEL,
            "ts": "1757000009.000000",
            "message": {"ts": "1757000000.000100", "text": "corrected"},
        }
    )
    assert result.reason == "edit"
    assert result.revision is not None and result.revision.appended
    assert len(mutations.changed) == 1


async def test_deletes_route_to_mutations(routing: tuple[Any, RecordingMutations]) -> None:
    ingestor, mutations = routing
    result = await ingestor.on_event(
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": CHANNEL,
            "deleted_ts": "1757000000.000100",
        }
    )
    assert result.reason == "delete"
    assert len(mutations.deleted) == 1


async def test_classification_happens_before_the_transaction_opens() -> None:
    """So a network-backed classifier in week 6 cannot hold a row lock open.

    The ingestor calls ``classify_with`` before ``unit_of_work``; this asserts
    the helper stands alone, which is what makes that ordering possible.
    """
    from incidentpilot.transcript.classifier import classify_with

    intent = await classify_with(None, "rolling back the deploy")
    assert intent.kind is IntentKind.REMEDIATION_START
