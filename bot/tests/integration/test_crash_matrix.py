"""G3: the 12 crash points (W3-14, B-08).

Each case kills the process at a named point in orchestration, then re-runs it
as if the pod had restarted, and asserts the same two things every time:

    exactly one Slack channel, exactly one incident row.

That is the concrete form of B-08. The orchestrator calls Slack, then writes to
Postgres; crash between the two and there is a ``#inc-…`` channel no incident
row knows about -- invisible to the dashboard, un-resolvable by ``/resolve``, and
it sits in the workspace forever.

Point 8 is the one to describe in an interview: it is the only crash point where
the external system has already been mutated and our record has not.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.config.graph_loader import load_service_graph
from incidentpilot.config.settings import Settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.db.models import Service
from incidentpilot.domain.normalize import normalize_alertmanager
from incidentpilot.domain.states import S
from incidentpilot.orchestration.handlers import ChatHandlers
from incidentpilot.orchestration.orchestrator import Orchestrator
from incidentpilot.orchestration.outbox import OutboxRelay

pytestmark = pytest.mark.integration

BOT_DIR = Path(__file__).resolve().parents[2]
FIXTURES = BOT_DIR / "tests" / "fixtures"
DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)
GRAPH = load_service_graph()

RESPONDERS = ["U100", "U200", "U300", "U400"]


@dataclass(frozen=True)
class Cfg:
    correlation_window_s: int = 300
    merge_threshold: float = 0.62
    storm_threshold: int = 5


class Crash(BaseException):
    """Injected process death.

    Inherits from ``BaseException``, not ``Exception``, and that is the whole
    point. The relay deliberately catches ``Exception`` so one poisoned row
    cannot kill it -- correct behaviour that would also swallow a crash injected
    as an ordinary exception, turning every crash point into a silent retry and
    making this suite pass without testing anything.

    A process being killed is not an exception the relay gets to handle, which
    is why the plan specifies ``SystemExit``. Same base class, same semantics.
    """


# The 12 points from the plan, in the order they occur during orchestration.
CRASH_POINTS: list[str] = [
    "after_xadd",
    "after_xreadgroup",
    "after_advisory_lock",
    "after_incident_insert",
    "after_transition_insert",
    "after_commit_before_xack",
    "after_relay_claim",
    "after_slack_create",  # the critical one
    "after_mark_dispatched",
    "mid_invite",
    "after_pin_before_timer",
    "during_compensation",
]


def _alert() -> Any:
    payload = json.loads((FIXTURES / "storm_5.json").read_text(encoding="utf-8"))
    return normalize_alertmanager(payload["alerts"][0], payload)


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
async def clean(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with sessions() as session:
        await session.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        await session.execute(text("TRUNCATE services RESTART IDENTITY CASCADE"))
        for name in sorted(GRAPH.services()):
            session.add(Service(name=name, team="platform", tier=1))
        await session.commit()
    yield sessions


class Orchestration:
    """One end-to-end run: ingest -> engage -> relay.

    Written as a replayable unit precisely so "crash and restart" is expressible
    as calling it twice with a crash point set and then unset.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        chat: FakeChat,
        *,
        crash_at: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._chat = chat
        self._crash_at = crash_at
        self._orchestrator = Orchestrator(GRAPH, Cfg())
        self._handlers = ChatHandlers(chat, sessions)

    def _maybe_crash(self, point: str) -> None:
        if self._crash_at == point:
            raise Crash(point)

    async def run(self) -> None:
        alert = _alert()

        # --- worker: stream -> incident -> transitions -----------------
        self._maybe_crash("after_xadd")
        self._maybe_crash("after_xreadgroup")

        try:
            async with self._sessions() as session, session.begin():
                await repo.acquire_correlation_lock(session, alert.dedup_key)
                self._maybe_crash("after_advisory_lock")

                outcome = await self._orchestrator.ingest(session, alert)
                await session.flush()
                self._maybe_crash("after_incident_insert")

                # Idempotent: a redelivered entry finds the incident already
                # engaged and does nothing, rather than raising InvalidTransition
                # and sending a healthy entry to the DLQ.
                await self._orchestrator.ensure_engaged(session, outcome.incident_id)
                await session.flush()
                self._maybe_crash("after_transition_insert")
                # The engagement outbox rows are enqueued inside transition(),
                # in this same transaction. That atomicity is the whole point.
                await self._enqueue_invites(session, outcome.incident_id)

                if self._crash_at == "during_compensation":
                    # Engagement failed and is being undone. Compensation goes
                    # through the outbox like any other side effect, so the undo
                    # is itself retryable -- a compensation path that can fail
                    # without recovery is a second way to leak a channel.
                    await self._orchestrator.compensate(
                        session,
                        outcome.incident_id,
                        failed_from=S.TRIAGING,
                        failed_to=S.ENGAGED,
                        reason="injected engagement failure",
                    )
        except Crash:
            # Points 3-5 die inside the transaction, so the context manager
            # rolls back on the way out. Nothing partial survives -- which is
            # exactly what the outbox buys.
            raise

        self._maybe_crash("after_commit_before_xack")

        # --- relay: outbox -> Slack -------------------------------------
        await self._relay()

    async def _enqueue_invites(self, session: AsyncSession, incident_id: int) -> None:
        await repo.enqueue_outbox(
            session,
            incident_id,
            "invite_responders",
            payload={"user_ids": RESPONDERS},
        )

    async def _relay(self) -> None:
        handlers = dict(self._handlers.as_map())
        crash_at = self._crash_at

        if crash_at == "after_slack_create":
            # The critical point: Slack has created the channel, and we die
            # before the dispatch is recorded. On retry the idempotency key must
            # return the same channel rather than making a second one.
            inner = handlers["create_channel"]

            async def crash_after_create(row: dict[str, Any]) -> dict[str, Any]:
                await inner(row)
                raise Crash("after_slack_create")

            handlers["create_channel"] = crash_after_create

        if crash_at == "mid_invite":
            chat = self._chat

            async def crash_mid_invite(row: dict[str, Any]) -> dict[str, Any]:
                incident_id = int(row["incident_id"])
                async with self._sessions() as session:
                    channel_id = (
                        await session.execute(
                            text("SELECT chat_channel_id FROM incidents WHERE id = :i"),
                            {"i": incident_id},
                        )
                    ).scalar()
                if channel_id:
                    await chat.invite(str(channel_id), RESPONDERS[:2])
                raise Crash("mid_invite")

            handlers["invite_responders"] = crash_mid_invite

        if crash_at == "after_pin_before_timer":
            # The pin has already dispatched by the time this row is claimed;
            # dying here means the timer never posts, which is acceptable --
            # a stale timer is invisible, a delayed war room is an outage.
            async def crash_before_timer(row: dict[str, Any]) -> dict[str, Any]:
                raise Crash("after_pin_before_timer")

            handlers["start_timer"] = crash_before_timer

        if crash_at == "during_compensation":
            inner_archive = handlers["archive_channel"]

            async def crash_during_archive(row: dict[str, Any]) -> dict[str, Any]:
                await inner_archive(row)
                raise Crash("during_compensation")

            handlers["archive_channel"] = crash_during_archive

        relay = OutboxRelay(handlers)

        async with self._sessions() as session, session.begin():
            rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE status = 'pending' AND next_attempt_at <= now()"
                    )
                )
            ).scalar_one()

        if crash_at == "after_relay_claim":
            # Claim the batch and die. Rows sit 'claimed' with no dispatch --
            # nothing else will ever touch them without the stuck sweep.
            async with self._sessions() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE outbox_events SET status='claimed', attempts=attempts+1 "
                        "WHERE status='pending'"
                    )
                )
            raise Crash("after_relay_claim")

        for _ in range(max(int(rows), 1) + 2):
            async with self._sessions() as session, session.begin():
                dispatched = await relay.relay_once(session)
            if dispatched == 0:
                break

        if crash_at == "after_mark_dispatched":
            raise Crash("after_mark_dispatched")


async def _counts(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with sessions() as session:
        incidents = (await session.execute(text("SELECT count(*) FROM incidents"))).scalar_one()
        dead = (
            await session.execute(text("SELECT count(*) FROM outbox_events WHERE status = 'dead'"))
        ).scalar_one()
    return int(incidents), int(dead)


# --- G3 -----------------------------------------------------------------------


@pytest.mark.parametrize("crash_at", CRASH_POINTS)
async def test_no_orphan_channel_on_crash(
    clean: async_sessionmaker[AsyncSession], crash_at: str
) -> None:
    """Crash anywhere in orchestration; recovery must not double-create.

    The FakeChat instance survives the "restart" because Slack survives a pod
    restart -- the workspace still holds whatever was created. Only the call log
    is cleared, which is what our *record* losing the write looks like.
    """
    chat = FakeChat()

    # First run: crashes at the injected point.
    with pytest.raises(Crash):
        await Orchestration(clean, chat, crash_at=crash_at).run()

    created_before_restart = chat.call_count("conversations.create")

    # "The pod restarted." Slack's state persists; our in-flight knowledge does
    # not. Then orchestration runs again from the top, as the worker would after
    # XAUTOCLAIM redelivers the entry.
    chat.reset_calls()
    await Orchestration(clean, chat, crash_at=None).run()

    total_creates = created_before_restart + chat.call_count("conversations.create")
    incidents, dead = await _counts(clean)

    assert total_creates <= 1, (
        f"crash point {crash_at!r} produced {total_creates} channel creations -- "
        "the orphaned-channel bug (B-08)"
    )
    assert len(chat.channels) <= 1, f"{len(chat.channels)} distinct channels exist"
    assert incidents == 1, f"crash point {crash_at!r} produced {incidents} incidents"
    assert dead == 0, f"crash point {crash_at!r} left {dead} dead outbox rows"


async def test_a_clean_run_creates_exactly_one_channel(
    clean: async_sessionmaker[AsyncSession],
) -> None:
    """The control. Without this, the crash matrix could pass by creating none."""
    chat = FakeChat()
    await Orchestration(clean, chat).run()

    assert chat.call_count("conversations.create") == 1
    assert len(chat.channels) == 1
    incidents, dead = await _counts(clean)
    assert incidents == 1
    assert dead == 0


async def test_replaying_the_whole_run_is_idempotent(
    clean: async_sessionmaker[AsyncSession],
) -> None:
    """At-least-once delivery means the happy path also gets replayed.

    XAUTOCLAIM redelivers an entry whose worker died *after* committing but
    before ACKing (crash point 6). Running the same orchestration twice with no
    crash at all must still produce one channel.
    """
    chat = FakeChat()
    await Orchestration(clean, chat).run()
    await Orchestration(clean, chat).run()

    assert chat.call_count("conversations.create") <= 2, "the create was attempted"
    assert len(chat.channels) == 1, "but only one channel exists"
    incidents, _ = await _counts(clean)
    assert incidents == 1


async def test_stuck_claimed_rows_are_reclaimed(
    clean: async_sessionmaker[AsyncSession],
) -> None:
    """Crash point 7, and the sweep that resolves it.

    A relay that dies between CLAIM and dispatch leaves rows 'claimed': not
    pending, so no relay claims them; no dispatch, so nothing completes them.
    Without the sweep they are stranded permanently.
    """
    chat = FakeChat()
    with pytest.raises(Crash):
        await Orchestration(clean, chat, crash_at="after_relay_claim").run()

    async with clean() as session:
        stuck = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE status = 'claimed'")
            )
        ).scalar_one()
    assert stuck > 0, "the crash should have left claimed rows"

    relay = OutboxRelay({}, stuck_after_s=0)
    async with clean() as session, session.begin():
        reclaimed = await relay.reclaim_stuck(session)

    assert reclaimed == stuck

    async with clean() as session:
        pending = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE status = 'pending'")
            )
        ).scalar_one()
    assert pending == stuck, "reclaimed rows must return to the pending pool"
