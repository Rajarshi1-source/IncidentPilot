"""W4-10..12: the only component allowed to read Slack history (INV-02).

The budget test is the load-bearing one. Slack's 1-request-per-minute limit is
counted against the *app*, so three replicas each politely calling once a minute
is three calls a minute -- which is why the gate lives in Valkey and why this
test drives two reconcilers through the same one.
"""

from __future__ import annotations

from typing import Any

import pytest

from incidentpilot.adapters.chat.history import ChannelSummary, FakeHistory
from incidentpilot.orchestration.reconciler import (
    BUDGET_KEY,
    CURSOR_KEY,
    HistoryBudget,
    Reconciler,
)
from incidentpilot.telemetry.metrics import REGISTRY
from tests.conftest import FakeValkey

CHANNEL = "C0AAA"
OTHER = "C0BBB"
TS = [f"1757000000.00010{i}" for i in range(4)]


class _Rows:
    """Serves both shapes the repository layer reads.

    ``.all()`` yields whole rows -- ``active_incident_channels`` wants tuples --
    while ``.scalars().all()`` yields single columns, which is what
    ``held_message_ts`` and ``known_channel_ids`` read. The fake returns the
    same list for both because each query in the sequence uses exactly one.
    """

    def __init__(self, rows: Any) -> None:
        self._rows = rows

    def all(self) -> Any:
        return self._rows

    def scalars(self) -> _Rows:
        return self


class QueuedSession:
    """Returns canned results in call order.

    The reconciler's query sequence is fixed and short -- active channels, then
    held timestamps, then known channels for the sweep -- so asserting on the
    order is a real check rather than a fake being agreeable.
    """

    def __init__(self, queue: list[Any]) -> None:
        self._queue = queue

    async def execute(self, statement: Any, params: Any = None) -> _Rows:
        assert self._queue, "the reconciler issued more queries than the test expected"
        return _Rows(self._queue.pop(0))

    async def __aenter__(self) -> QueuedSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def sessions_returning(*results: Any) -> Any:
    queue = list(results)

    def factory() -> QueuedSession:
        return QueuedSession(queue)

    return factory


def _gauge() -> float | None:
    for metric in REGISTRY.collect():
        if metric.name == "ip_transcript_completeness":
            return metric.samples[0].value
    return None


# --- the budget (W4-10) -------------------------------------------------------


async def test_budget_allows_one_call_per_period(fake_valkey: FakeValkey) -> None:
    budget = HistoryBudget(fake_valkey)
    assert await budget.acquire() is True
    assert await budget.acquire() is False
    assert BUDGET_KEY in fake_valkey.keys


async def test_budget_is_global_across_replicas(fake_valkey: FakeValkey) -> None:
    """Slack counts the app, not the pod."""
    first = HistoryBudget(fake_valkey)
    second = HistoryBudget(fake_valkey)
    assert await first.acquire() is True
    assert await second.acquire() is False


async def test_budget_fails_closed_when_valkey_is_unreachable(
    fake_valkey: FakeValkey,
) -> None:
    """Guessing wrong on a 1/min limit costs the next minute too.

    A skipped reconcile pass costs nothing -- the transcript is already complete
    or already is not, and the next tick will find out.
    """
    fake_valkey.alive = False
    assert await HistoryBudget(fake_valkey).acquire() is False


async def test_reconciler_respects_global_budget(fake_valkey: FakeValkey) -> None:
    history = FakeHistory(channel_ts={CHANNEL: TS})
    budget = HistoryBudget(fake_valkey)

    def build() -> Reconciler:
        return Reconciler(
            sessions_returning([(CHANNEL, 1)], TS),
            history,
            budget,
            valkey=fake_valkey,
        )

    first = await build().reconcile_transcripts()
    second = await build().reconcile_transcripts()

    assert first.ratio == 1.0
    assert second.skipped == "budget_exhausted"
    assert history.call_count("conversations.history") == 1


async def test_no_active_channels_spends_no_budget(fake_valkey: FakeValkey) -> None:
    """The budget is checked after the work list, not before.

    A quiet workspace must not burn the minute another replica could use on a
    channel that actually has messages in it.
    """
    history = FakeHistory()
    reconciler = Reconciler(sessions_returning([]), history, HistoryBudget(fake_valkey))

    assert (await reconciler.reconcile_transcripts()).skipped == "no_active_channels"
    assert history.calls == []
    assert BUDGET_KEY not in fake_valkey.keys


# --- the completeness gauge (W4-11) -------------------------------------------


async def test_completeness_gauge_emitted(fake_valkey: FakeValkey) -> None:
    reconciler = Reconciler(
        sessions_returning([(CHANNEL, 1)], TS),
        FakeHistory(channel_ts={CHANNEL: TS}),
        HistoryBudget(fake_valkey),
        valkey=fake_valkey,
    )
    report = await reconciler.reconcile_transcripts()

    assert report.sampled == 4
    assert report.held == 4
    assert _gauge() == 1.0


async def test_a_missing_message_moves_the_gauge(fake_valkey: FakeValkey) -> None:
    """The one SLI whose failure has no other symptom.

    The bot can be 100% available and still have silently dropped a message; if
    this number does not move, nothing ever notices.
    """
    reconciler = Reconciler(
        sessions_returning([(CHANNEL, 1)], TS[:3]),
        FakeHistory(channel_ts={CHANNEL: TS}),
        HistoryBudget(fake_valkey),
        valkey=fake_valkey,
    )
    report = await reconciler.reconcile_transcripts()

    assert report.held == 3
    assert report.ratio == pytest.approx(0.75)
    assert _gauge() == pytest.approx(0.75)


async def test_an_empty_channel_is_complete_not_broken(fake_valkey: FakeValkey) -> None:
    """Reporting 0.0 would page someone about a war room nobody has spoken in."""
    reconciler = Reconciler(
        sessions_returning([(CHANNEL, 1)]),
        FakeHistory(channel_ts={CHANNEL: []}),
        HistoryBudget(fake_valkey),
        valkey=fake_valkey,
    )
    assert (await reconciler.reconcile_transcripts()).ratio == 1.0
    assert _gauge() == 1.0


async def test_channels_are_sampled_round_robin(fake_valkey: FakeValkey) -> None:
    """One busy incident must not starve every other channel of its sample."""
    history = FakeHistory(channel_ts={CHANNEL: TS, OTHER: TS})

    def run() -> Reconciler:
        return Reconciler(
            sessions_returning([(CHANNEL, 1), (OTHER, 2)], TS),
            history,
            HistoryBudget(fake_valkey),
            valkey=fake_valkey,
        )

    first = await run().reconcile_transcripts()
    fake_valkey.keys.pop(BUDGET_KEY)  # the minute rolls over
    second = await run().reconcile_transcripts()

    assert {first.channel_id, second.channel_id} == {CHANNEL, OTHER}
    assert fake_valkey.keys[CURSOR_KEY] == second.channel_id


# --- orphan channels (W4-12, B-08) --------------------------------------------


class RecordingHandlers:
    def __init__(self) -> None:
        self.archived: list[str] = []

    async def archive_orphan_channel(self, channel_id: str) -> dict[str, Any]:
        self.archived.append(channel_id)
        return {"archived": channel_id}


async def test_orphan_channel_archived(fake_valkey: FakeValkey) -> None:
    """A ``#inc-*`` channel no incident row claims is invisible and permanent.

    Not on the dashboard, not resolvable by ``/resolve``, and it sits in the
    workspace forever. The outbox makes this rare (B-08); the sweep makes it
    recoverable when it happens anyway.
    """
    handlers = RecordingHandlers()
    reconciler = Reconciler(
        sessions_returning([CHANNEL]),
        FakeHistory(
            channels=[
                ChannelSummary(CHANNEL, "inc-2026-09-07-payments-down"),
                ChannelSummary("C0ORPHAN", "inc-2026-09-06-checkout-latency"),
            ]
        ),
        HistoryBudget(fake_valkey),
        handlers=handlers,  # type: ignore[arg-type]
    )

    assert await reconciler.sweep_orphan_channels() == ["C0ORPHAN"]
    assert handlers.archived == ["C0ORPHAN"]


async def test_a_closed_incidents_channel_is_not_an_orphan(
    fake_valkey: FakeValkey,
) -> None:
    """Orphan means "no incident row knows about it", not "no incident is open".

    Archiving the channel of a closed incident would delete the record people go
    back to read -- which is the opposite of what this feature is for.
    """
    handlers = RecordingHandlers()
    reconciler = Reconciler(
        sessions_returning(["C0CLOSED"]),
        FakeHistory(channels=[ChannelSummary("C0CLOSED", "inc-2026-08-01-old")]),
        HistoryBudget(fake_valkey),
        handlers=handlers,  # type: ignore[arg-type]
    )

    assert await reconciler.sweep_orphan_channels() == []
    assert handlers.archived == []


async def test_the_sweep_does_not_touch_the_history_budget(
    fake_valkey: FakeValkey,
) -> None:
    """``conversations.list`` is Tier 2 and independent of the history limit.

    Conflating the two would make the sweep as rare as the sampler for no
    reason at all.
    """
    history = FakeHistory(channels=[])
    reconciler = Reconciler(
        sessions_returning([]),
        history,
        HistoryBudget(fake_valkey),
        handlers=RecordingHandlers(),  # type: ignore[arg-type]
    )

    await reconciler.sweep_orphan_channels()
    assert history.call_count("conversations.history") == 0
    assert BUDGET_KEY not in fake_valkey.keys


async def test_no_handlers_means_no_archiving(fake_valkey: FakeValkey) -> None:
    """The completeness half must run in a process with no chat credentials."""
    reconciler = Reconciler(
        sessions_returning([]),
        FakeHistory(channels=[ChannelSummary("C0ORPHAN", "inc-x")]),
        HistoryBudget(fake_valkey),
    )
    assert await reconciler.sweep_orphan_channels() == []
