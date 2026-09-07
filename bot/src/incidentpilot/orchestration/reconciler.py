"""The only component permitted to read Slack history (W4-10..12, INV-02).

Two jobs, both of them the kind nothing else notices when they are missing:

**Transcript completeness.** The bot can return HTTP 200 to every request, keep
every pod ready, and still have silently lost three messages -- after which the
PIR is wrong, its citations point into a transcript with holes, and there is no
error anywhere to see. Availability of a data-collection system is measured in
data, not in 200s, which is why ``ip_transcript_completeness`` has the tightest
target in ``docs/SLO.md``.

**Orphan channels.** A ``#inc-…`` channel that no incident row knows about is
invisible to the dashboard, un-resolvable by ``/resolve``, and sits in the
workspace forever (B-08). The outbox makes this rare; the sweep makes it
recoverable when it happens anyway.

## The budget, and why the estimator is a sample

Since 3 March 2026 a non-Marketplace app gets ``conversations.history`` at
**1 request per minute, 15 messages per request**. So "count the messages in the
channel and compare" is not an available operation -- a 200-message channel is
fourteen pages and therefore fourteen minutes, during which the count is already
stale.

What this does instead: take the newest page for **one** channel per pass and
ask how many of those timestamps we already hold. It is a sampled estimator, and
naming it one matters -- a "completeness ratio" that quietly inspected a
fifteenth of the channel would be the same species of silent wrongness the whole
week exists to eliminate. The sample is drawn from the newest messages, which is
where a live ingestion failure shows up first.

The budget is enforced in Valkey, not in a local variable, because it is a
*global* limit: three replicas each politely calling once a minute is three
calls a minute, and Slack counts the app, not the pod.

## One deliberate deviation from INV-03, recorded rather than hidden

Archiving an orphan channel is an external write, and INV-03 says those happen
in the outbox relay. It cannot go through the outbox here: ``outbox_events``
requires an ``incident_id``, and an orphan is by definition a channel no
incident row claims -- there is nothing to hang the row off.

So the write still happens *inside* ``orchestration/handlers.py`` (the one
module allowed to touch a chat adapter) and the reconciler calls that method.
The invariant's actual guarantee -- exactly one module performs external chat
writes -- is intact, and the property the outbox buys, that two workers cannot
both create a channel, does not apply to an operation that is idempotent and run
by a single scheduled job.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.chat.history import (
    HISTORY_PAGE_LIMIT,
    INCIDENT_CHANNEL_PREFIX,
    HistoryReader,
)
from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.db import repositories as repo
from incidentpilot.orchestration.handlers import ChatHandlers
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger
from incidentpilot.telemetry.metrics import HISTORY_CALLS, TRANSCRIPT_RATIO

log = get_logger(__name__)

CALLER = "reconciler"
BUDGET_KEY = "ip:history:budget"
CURSOR_KEY = "ip:recon:cursor"


class HistoryBudget:
    """A global one-call-per-minute gate, held in Valkey.

    ``SET key 1 NX EX 60`` is the whole mechanism: whoever wins the SET owns the
    minute. No lease renewal, no lock release -- the key expiring *is* the
    budget refilling, so a replica that dies holding it costs at most one unused
    minute rather than wedging the reconciler forever.

    **Fails closed.** If Valkey cannot be reached the budget is treated as
    spent, because the alternative is guessing, and guessing wrong on a 1/min
    limit means a 429 that takes the next minute with it. A skipped reconcile
    pass costs nothing; the transcript is already complete or already isn't.
    """

    def __init__(self, valkey: Any, *, key: str = BUDGET_KEY, period_s: int = 60) -> None:
        self._valkey = valkey
        self._key = key
        self._period_s = period_s

    async def acquire(self) -> bool:
        try:
            return bool(await self._valkey.set(self._key, "1", nx=True, ex=self._period_s))
        except Exception as exc:
            log.warning("reconciler.budget_unavailable", error=str(exc))
            return False


@dataclass(frozen=True, slots=True)
class TranscriptReport:
    """One sampled comparison. ``ratio is None`` means the pass did no work."""

    channel_id: str | None
    sampled: int
    held: int
    ratio: float | None
    skipped: str | None = None


class Reconciler:
    """Transcript completeness and orphan-channel cleanup.

    ``handlers`` is optional so the completeness half can run in a process with
    no chat credentials at all -- which is what the unit tests do, and what a
    read-only deployment would want.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        history: HistoryReader,
        budget: HistoryBudget,
        *,
        valkey: Any = None,
        handlers: ChatHandlers | None = None,
        sample_limit: int = HISTORY_PAGE_LIMIT,
    ) -> None:
        self._sessions = sessions
        self._history = history
        self._budget = budget
        self._valkey = valkey
        self._handlers = handlers
        self._sample_limit = sample_limit

    # -- transcript completeness (W4-10, W4-11) --------------------------

    async def reconcile_transcripts(self) -> TranscriptReport:
        """Sample one active channel, if the minute's budget is available."""
        async with self._sessions() as session:
            channels = await repo.active_incident_channels(session)

        if not channels:
            return TranscriptReport(None, 0, 0, None, skipped="no_active_channels")

        if not await self._budget.acquire():
            # Not an error. Another replica -- or this one, 40 seconds ago --
            # already spent the minute.
            return TranscriptReport(None, 0, 0, None, skipped="budget_exhausted")

        channel_id = await self._next_channel(sorted(channels))
        HISTORY_CALLS.labels(caller=CALLER).inc()
        sampled = await self._history.recent_message_ts(channel_id, limit=self._sample_limit)

        if not sampled:
            # An empty channel is complete by definition. Reporting 0.0 here
            # would page someone about a war room nobody has spoken in yet.
            TRANSCRIPT_RATIO.set(1.0)
            return TranscriptReport(channel_id, 0, 0, 1.0)

        async with self._sessions() as session:
            held = await repo.held_message_ts(session, channel_id, sampled)

        missing = [ts for ts in sampled if ts not in held]
        ratio = (len(sampled) - len(missing)) / len(sampled)
        TRANSCRIPT_RATIO.set(ratio)
        if missing:
            # Loud on purpose. This is the one SLI whose failure has no other
            # symptom, so the log line has to carry the evidence with it.
            log.error(
                "transcript.incomplete",
                channel_id=channel_id,
                sampled=len(sampled),
                missing=missing,
            )
        return TranscriptReport(channel_id, len(sampled), len(sampled) - len(missing), ratio)

    async def _next_channel(self, channels: list[str]) -> str:
        """Round-robin, so one busy incident cannot starve the rest.

        The cursor lives in Valkey rather than in the process: the reconciler is
        restarted by every deploy, and a per-process cursor would re-sample the
        same first channel forever on a busy day.
        """
        previous: str | None = None
        if self._valkey is not None:
            with contextlib.suppress(Exception):
                raw = await self._valkey.get(CURSOR_KEY)
                previous = raw.decode() if isinstance(raw, bytes) else (raw and str(raw))

        chosen = channels[0]
        if previous in channels:
            chosen = channels[(channels.index(previous) + 1) % len(channels)]

        if self._valkey is not None:
            with contextlib.suppress(Exception):
                await self._valkey.set(CURSOR_KEY, chosen, ex=86400)
        return chosen

    # -- orphan channels (W4-12, B-08) -----------------------------------

    async def sweep_orphan_channels(self, *, prefix: str = INCIDENT_CHANNEL_PREFIX) -> list[str]:
        """Archive every ``#inc-*`` channel no incident row claims.

        Uses ``conversations.list`` (Tier 2), not history, so it does not touch
        the 1/min budget -- the two limits are independent and conflating them
        would make the sweep as rare as the sampler for no reason.
        """
        if self._handlers is None:
            return []

        candidates = await self._history.list_incident_channels(prefix=prefix)
        if not candidates:
            return []

        async with self._sessions() as session:
            known = await repo.known_channel_ids(session)

        archived: list[str] = []
        for channel in candidates:
            if channel.id in known:
                continue
            # Archiving is idempotent, so a sweep that runs twice on the same
            # orphan is a no-op rather than a second failure to reason about.
            await self._handlers.archive_orphan_channel(channel.id)
            archived.append(channel.id)
            log.warning("channel.orphan_archived", channel_id=channel.id, name=channel.name)
        return archived

    async def run_once(self) -> TranscriptReport:
        report = await self.reconcile_transcripts()
        await self.sweep_orphan_channels()
        return report


async def run(cfg: Settings | None = None, *, max_passes: int | None = None) -> None:
    """``python -m incidentpilot.orchestration.reconciler``.

    Its own loop for now. W5-17 moves the schedule into ``scheduler.py`` once
    the job runner lands -- the interval is deliberately the same 15 minutes the
    scheduler will use, so the move is a wiring change and not a behaviour one.
    """
    cfg = cfg or default_settings
    configure_event_loop()
    configure_logging(level=cfg.log_level, json_output=cfg.log_json)

    from redis.asyncio import Redis

    from incidentpilot.adapters.chat.factory import build_chat, build_history
    from incidentpilot.db.engine import build_engine, build_session_factory
    from incidentpilot.orchestration.handlers import ChatHandlers

    engine = build_engine(cfg)
    sessions = build_session_factory(engine)
    valkey = Redis.from_url(cfg.valkey_url.get_secret_value(), decode_responses=True)
    reconciler = Reconciler(
        sessions,
        build_history(cfg),
        HistoryBudget(valkey, period_s=cfg.history_budget_period_s),
        valkey=valkey,
        handlers=ChatHandlers(build_chat(cfg, valkey=valkey), sessions),
    )

    stopping = asyncio.Event()

    def _stop(*_: object) -> None:
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, AttributeError, OSError, NotImplementedError):
            signal.signal(sig, _stop)

    log.info("reconciler.started", interval_s=cfg.reconcile_interval_s)
    passes = 0
    try:
        while not stopping.is_set():
            await reconciler.run_once()
            passes += 1
            if max_passes is not None and passes >= max_passes:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=cfg.reconcile_interval_s)
    finally:
        await engine.dispose()
        with contextlib.suppress(Exception):
            await valkey.aclose()
        log.info("reconciler.stopped", passes=passes)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
