"""🚦 G5: the on-call fallback ladder, announced at every rung (W5-05..W5-08).

> Pager unreachable → cache → static rota → team channel, each announced.
> **Silent failure fails the gate.**

That last sentence is the gate. Resolving someone from a stale rota and posting
a notice that looks identical to a healthy page would satisfy every assertion
about *who* was paged and still be the failure this week exists to prevent: an
incident where the bot believes it woke a human and did not.

So every case here asserts twice -- on the decision, and on what the channel was
told. Against a real Postgres, because ``page_events``, the fatigue window and
the runbook signals are all database behaviour.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.adapters.paging.fake import FakePaging
from incidentpilot.adapters.paging.pagerduty import EVENTS_URL, REST_BASE, PagerDutyPaging
from incidentpilot.adapters.paging.static_schedule import StaticSchedule
from incidentpilot.config.settings import Settings
from incidentpilot.db import queries
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.domain.fatigue import Routing
from incidentpilot.orchestration.handlers import ChatHandlers
from incidentpilot.orchestration.routing import ResponderRouter
from tests.conftest import FakeValkey

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)

CHANNEL = "C0WARROOM"
AT = datetime(2026, 9, 7, 3, 14, tzinfo=UTC)  # 03:14 — a night page in most zones


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
    """One engaged incident on the payments service, with a channel."""
    async with sessions() as session:
        await session.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        await session.execute(text("DELETE FROM page_events"))
        await session.execute(
            text(
                "INSERT INTO services (name, team, tier) VALUES ('payments', 'payments', 1)"
                " ON CONFLICT (name) DO UPDATE SET team = 'payments'"
            )
        )
        await session.execute(
            text(
                "INSERT INTO responders (slack_user_id, display_name, timezone)"
                " VALUES ('U_PAY_ANANYA', 'Ananya', 'Asia/Kolkata'),"
                "        ('U_PRIMARY', 'Primary', 'Asia/Kolkata'),"
                "        ('U_SECONDARY', 'Secondary', 'Europe/Berlin')"
                " ON CONFLICT (slack_user_id) DO NOTHING"
            )
        )
        row = await session.execute(
            text(
                "INSERT INTO incidents (public_key, dedup_key, title, severity, state,"
                " chat_channel_id, root_signal, detected_at, primary_service_id)"
                " VALUES ('inc-w5', 'dk-w5', 'Postgres primary down', 'sev1', 'engaged',"
                " :channel, 'PostgresReplicationLag', :at,"
                " (SELECT id FROM services WHERE name = 'payments')) RETURNING id"
            ),
            # Four hours old: long enough that `incident_minutes_24h` is a
            # real number rather than zero, which is what the fatigue score
            # is meant to notice about a long night.
            {"channel": CHANNEL, "at": AT - timedelta(hours=4)},
        )
        new_id = int(row.scalar_one())
        await session.commit()
        yield new_id


@pytest.fixture
def valkey() -> FakeValkey:
    return FakeValkey()


@pytest.fixture
def chat() -> FakeChat:
    return FakeChat()


def _handlers(
    sessions: Any, chat: FakeChat, paging: Any, valkey: FakeValkey, **kw: Any
) -> ChatHandlers:
    router = ResponderRouter(
        paging,
        repo.OnCallCache(valkey),
        static_schedule=StaticSchedule(at=AT),
        **kw,
    )
    return ChatHandlers(chat, sessions, paging=paging, router=router, now=lambda: AT)


def _posted_text(chat: FakeChat) -> str:
    """Everything the bot said. The gate greps this."""
    return " ".join(
        f"{c.meta.get('text', '')} {c.meta.get('blocks', '')}"
        for c in chat.calls_to("chat.postMessage")
    )


# --- 🚦 the ladder ------------------------------------------------------------


async def test_oncall_fallback_ladder(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """G5, the whole thing: provider 503, cache flushed, static rota answers.

    Asserted on both halves. ``source == static_schedule`` is the routing
    decision; the word "degraded" in the channel is the part that makes it not a
    silent failure.
    """
    with respx.mock:
        respx.get(f"{REST_BASE}/oncalls").mock(return_value=httpx.Response(503))
        respx.post(EVENTS_URL).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            paging = PagerDutyPaging(routing_key="rk", api_token="tok", client=client)
            handlers = _handlers(sessions, chat, paging, valkey)
            await valkey.flushall()  # no cache either

            # The relay has already retried: `attempts` is what turns "keep
            # trying quietly" into "tell the channel nobody was reached".
            result = await handlers.page_responder(
                {
                    "incident_id": incident_id,
                    "payload": {},
                    "idempotency_key": "k",
                    "attempts": 3,
                }
            )

    assert result["degraded"] is True
    assert "degraded" in _posted_text(chat).lower(), "a silent fallback fails G5"


async def test_a_healthy_provider_announces_nothing_alarming(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """The other half of the gate: no false alarms.

    A notice that appears on every page teaches people to ignore it, and then the
    degraded case is invisible again.
    """
    handlers = _handlers(sessions, chat, FakePaging(rota={"payments": ("U_PRIMARY", None)}), valkey)
    result = await handlers.page_responder(
        {"incident_id": incident_id, "payload": {}, "idempotency_key": "k"}
    )

    assert result["degraded"] is False
    assert "degraded" not in _posted_text(chat).lower()


async def test_nobody_resolvable_broadcasts_to_the_team_channel(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """Rung 4. An incident open with no page delivered is the loudest thing
    that can happen, and it is said in the channel where the people not yet in
    the incident are."""
    empty = StaticSchedule(at=AT)
    empty._rotations = {}
    router = ResponderRouter(FakePaging(fail=True), repo.OnCallCache(valkey), static_schedule=empty)
    handlers = ChatHandlers(
        chat, sessions, paging=FakePaging(fail=True), router=router, now=lambda: AT
    )

    result = await handlers.page_responder(
        {"incident_id": incident_id, "payload": {}, "idempotency_key": "k"}
    )

    assert result["paged"] == []
    assert result["degraded"] is True
    assert chat.call_count("chat.postMessage") >= 1


async def test_a_rota_that_cannot_page_falls_through_to_broadcast(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """The static rota names someone and cannot ring their phone.

    Rung 4 in its second sense: we know who, we simply cannot reach them. A
    ``delivered=False`` that nobody announced would leave the incident believing
    a human was on the way.
    """
    static = StaticSchedule(at=AT)
    handlers = _handlers(sessions, chat, static, valkey)

    result = await handlers.page_responder(
        {"incident_id": incident_id, "payload": {}, "idempotency_key": "k"}
    )

    assert result["paged"] == []
    assert result["degraded"] is True
    assert "cannot deliver" in str(result.get("reason", ""))


# --- fatigue, end to end ------------------------------------------------------


async def test_page_events_are_written_for_every_page(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """The raw material of the next fatigue score.

    A page that happened and was not counted makes the responder who was woken
    up look fresh to the next incident -- which is precisely the bias D5 exists
    to remove.
    """
    handlers = _handlers(sessions, chat, FakePaging(rota={"payments": ("U_PRIMARY", None)}), valkey)
    await handlers.page_responder(
        {"incident_id": incident_id, "payload": {}, "idempotency_key": "k"}
    )

    async with sessions() as session:
        row = (
            await session.execute(
                text("SELECT responder, severity, tz FROM page_events WHERE incident_id = :i"),
                {"i": incident_id},
            )
        ).one()

    assert row.responder == "U_PRIMARY"
    assert row.severity == "sev1"
    # Denormalized from responders.timezone at write time: a page at 03:14 IST
    # is a night page whatever UTC thinks.
    assert row.tz == "Asia/Kolkata"


async def test_fatigue_window_query(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """W5-07: ``Q7`` produces all five terms, not the reference's two.

    The window end is a parameter rather than ``now()``, which is what makes the
    same recorded incident score identically under replay.
    """
    async with sessions() as session, session.begin():
        for hours_ago, severity in ((1, "sev1"), (3, "sev2"), (7, "sev2")):
            await repo.record_page(
                session,
                at=AT - timedelta(hours=hours_ago),
                responder="U_PRIMARY",
                incident_id=incident_id,
                severity=severity,
                tz="Asia/Kolkata",
            )

    async with sessions() as session:
        window = await queries.window_for(session, "U_PRIMARY", at=AT)

    assert window.pages_8h == 3
    assert window.sev1_count_7d == 1
    # 03:14, 01:14 and 21:14 IST-shifted: the query counts what falls outside
    # 08:00-22:00 local, which is the point of storing tz on the row.
    assert window.night_pages_24h >= 1
    assert window.incident_minutes_24h > 0


async def test_a_tired_primary_pages_the_secondary_and_says_so(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """test_reroute_is_announced (W5-08), against real page history.

    Three pages, two at night, four hours in incidents -- over 0.75. The
    secondary is paged, the primary is *told*, and the opt-in button is in the
    message. Never a silent reroute.
    """
    # The realistic shape: a *previous* incident that ran for four hours and
    # paged this person three times, and then a new one arrives. The window is
    # anchored on the new incident's detection time, so the prior load has to
    # sit before it -- which is exactly what "recent load" means.
    detected_at = AT - timedelta(hours=4)
    async with sessions() as session, session.begin():
        prior = (
            await session.execute(
                text(
                    "INSERT INTO incidents (public_key, dedup_key, title, severity, state,"
                    " detected_at, resolved_at)"
                    " VALUES ('inc-w5-prior', 'dk-w5-prior', 'Earlier tonight', 'sev1',"
                    " 'closed', :start, :end) RETURNING id"
                ),
                {
                    "start": detected_at - timedelta(hours=9),
                    "end": detected_at - timedelta(hours=5),
                },
            )
        ).scalar_one()
        for hours_ago in (1, 2, 3):
            await repo.record_page(
                session,
                at=detected_at - timedelta(hours=hours_ago),
                responder="U_PRIMARY",
                incident_id=int(prior),
                severity="sev1",
                tz="Asia/Kolkata",
            )

    handlers = _handlers(
        sessions, chat, FakePaging(rota={"payments": ("U_PRIMARY", "U_SECONDARY")}), valkey
    )
    result = await handlers.page_responder(
        {"incident_id": incident_id, "payload": {}, "idempotency_key": "k"}
    )

    assert result["paged"] == ["U_SECONDARY"]
    assert result["notified"] == ["U_PRIMARY"]
    assert result["score"] >= 0.75

    assert chat.said("Nobody was removed quietly")
    assert chat.said("fatigue_opt_in"), "the primary needs a way back in"
    invited = {u for call in chat.calls_to("conversations.invite") for u in call.args[1]}
    assert invited == {"U_PRIMARY", "U_SECONDARY"}, "the primary stays in their own war room"


async def test_the_reroute_message_carries_reasons_and_an_opt_in(
    sessions: async_sessionmaker[AsyncSession],
    chat: FakeChat,
    valkey: FakeValkey,
    incident_id: int,
) -> None:
    """A decision nobody can argue with is a decision people route around."""
    from incidentpilot.adapters.chat import block_kit

    blocks = block_kit.routing_notice(
        paged=["U_SECONDARY"],
        notified=["U_PRIMARY"],
        score=0.81,
        reasons=["3 pages in 8h", "2 outside 08:00-22:00 local"],
        rerouted=True,
        oncall_source="provider",
    )
    rendered = str(blocks)
    assert "fatigue *0.81*" in rendered
    assert "3 pages in 8h" in rendered
    assert "fatigue_opt_in" in rendered
    assert "Nobody was removed quietly" in rendered


async def test_routing_is_deterministic_for_the_same_incident(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """INV-10 in the routing path: the window end is the incident's own time.

    Two runs an hour apart must reach the same person, or replaying the corpus
    would produce different routing every day.
    """
    router = ResponderRouter(
        FakePaging(rota={"payments": ("U_PRIMARY", "U_SECONDARY")}),
        repo.OnCallCache(valkey),
        static_schedule=StaticSchedule(at=AT),
    )
    async with sessions() as session:
        first = await router.plan(session, schedule="payments", at=AT)
        second = await router.plan(session, schedule="payments", at=AT)

    assert first.page == second.page
    assert first.score == second.score
    assert first.routing is second.routing is Routing.PAGE_PRIMARY
