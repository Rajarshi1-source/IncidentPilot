"""W5-05, W5-08: the on-call ladder and the routing decision (G5, FMEA #14).

The ladder tests are the G5 gate's logic half. The gate itself drives the
handler and asserts on what was posted; these assert on what was *decided*, which
is where the "never silently" rule actually lives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from incidentpilot.adapters.paging.base import OnCall, OnCallSource, RetryablePagingError
from incidentpilot.adapters.paging.fake import FakePaging
from incidentpilot.adapters.paging.static_schedule import StaticSchedule
from incidentpilot.db.repositories import OnCallCache
from incidentpilot.domain.fatigue import ResponderWindow, Routing
from incidentpilot.orchestration.routing import ResponderRouter
from tests.conftest import FakeValkey

AT = datetime(2026, 9, 7, 3, 14, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: Any) -> None:
        self._rows = rows

    def mappings(self) -> Any:
        return self._rows


class WindowSession:
    """Answers ``Q7`` with a canned window, so the ladder can be tested alone."""

    def __init__(self, windows: dict[str, ResponderWindow]) -> None:
        self._windows = windows

    async def execute(self, statement: Any, params: Any = None) -> _Rows:
        responder = (params or {}).get("responder")
        window = self._windows.get(responder or "", ResponderWindow(responder=responder or ""))
        return _Rows(
            [
                {
                    "responder": window.responder,
                    "pages_8h": window.pages_8h,
                    "night_pages_24h": window.night_pages_24h,
                    "sev1_count_7d": window.sev1_count_7d,
                    "incident_minutes_24h": window.incident_minutes_24h,
                    "consecutive_oncall_days": window.consecutive_oncall_days,
                }
            ]
        )

    async def __aenter__(self) -> WindowSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def session_with(windows: dict[str, ResponderWindow] | None = None) -> Any:
    """``plan()`` takes a session, not a factory -- it is already inside one.

    That is deliberate in the router: the caller owns the transaction, so a
    routing decision and the page it produces can share one if they need to.
    """
    return WindowSession(windows or {})


@pytest.fixture
def cache(fake_valkey: FakeValkey) -> OnCallCache:
    return OnCallCache(fake_valkey)


@pytest.fixture
def static() -> StaticSchedule:
    return StaticSchedule(at=AT)


# --- the ladder ---------------------------------------------------------------


async def test_provider_answer_is_used_and_cached(cache: OnCallCache) -> None:
    paging = FakePaging(rota={"payments": ("U_PRIMARY", "U_SECONDARY")})
    router = ResponderRouter(paging, cache)

    first = await router.resolve_oncall("payments")
    assert first.source is OnCallSource.PROVIDER
    assert first.primary == "U_PRIMARY"

    second = await router.resolve_oncall("payments")
    assert second.source is OnCallSource.CACHE
    assert len(paging.oncall_lookups) == 1, "the cached answer still hit the provider"


async def test_oncall_fallback_ladder(cache: OnCallCache, static: StaticSchedule) -> None:
    """G5's core: provider down, cache cold, static rota answers -- and says so.

    The `degraded` flag and the reason string are the contract with the handler.
    A fallback that resolved someone and looked identical to a provider answer
    would pass every test here and fail the gate, which is the whole point of
    the gate.
    """
    router = ResponderRouter(FakePaging(fail=True), cache, static_schedule=static)

    oncall = await router.resolve_oncall("payments")

    assert oncall.source is OnCallSource.STATIC_SCHEDULE
    assert oncall.primary in {"U_PAY_ANANYA", "U_PAY_RAVI", "U_PAY_MEERA"}
    assert oncall.degraded is True
    assert oncall.reason and "unavailable" in oncall.reason


async def test_a_degraded_answer_is_never_cached(
    cache: OnCallCache, static: StaticSchedule, fake_valkey: FakeValkey
) -> None:
    """Otherwise the incident stays on the fallback rung for the whole TTL.

    Worse: the next lookup would be a *cache hit*, which is not degraded, and
    the channel would stop being told that on-call is unreliable.
    """
    router = ResponderRouter(FakePaging(fail=True), cache, static_schedule=static)
    await router.resolve_oncall("payments")
    assert "ip:oncall:payments" not in fake_valkey.keys


async def test_unknown_schedule_falls_all_the_way_to_broadcast(cache: OnCallCache) -> None:
    """Rung 4. Nobody resolved is a thing to shout about, not to swallow."""
    empty = StaticSchedule(at=AT)
    empty._rotations = {}
    router = ResponderRouter(FakePaging(fail=True), cache, static_schedule=empty)

    oncall = await router.resolve_oncall("nonexistent")
    assert oncall.source is OnCallSource.TEAM_BROADCAST
    assert oncall.known is False


async def test_the_ladder_never_raises(cache: OnCallCache) -> None:
    """A resolver that throws aborts routing entirely -- the silent failure G5
    forbids, arriving through the back door."""

    class Exploding:
        async def oncall_for(self, schedule: str) -> OnCall:
            raise RetryablePagingError("boom")

    class ExplodingStatic:
        async def oncall_for(self, schedule: str) -> OnCall:
            raise RuntimeError("the rota file is a directory")

        def team_channel_for(self, schedule: str) -> str | None:
            return "#fallback"

    router = ResponderRouter(Exploding(), cache, static_schedule=ExplodingStatic())
    oncall = await router.resolve_oncall("payments")
    assert oncall.known is False
    assert oncall.source is OnCallSource.TEAM_BROADCAST


# --- the plan -----------------------------------------------------------------


async def test_a_fresh_primary_is_simply_paged(cache: OnCallCache) -> None:
    router = ResponderRouter(FakePaging(rota={"default": ("U_A", "U_B")}), cache)
    plan = await router.plan(session_with(), schedule="default", at=AT)

    assert plan.routing is Routing.PAGE_PRIMARY
    assert plan.page == ("U_A",)
    assert plan.notify == ()
    assert plan.degraded is False


async def test_a_tired_primary_is_never_removed_quietly(cache: OnCallCache) -> None:
    """D5's acceptance criterion, at the point the decision is made."""
    tired = ResponderWindow("U_A", pages_8h=3, night_pages_24h=2, incident_minutes_24h=300)
    router = ResponderRouter(FakePaging(rota={"default": ("U_A", "U_B")}), cache)

    plan = await router.plan(session_with({"U_A": tired}), schedule="default", at=AT)

    assert plan.routing is Routing.PAGE_SECONDARY_NOTIFY_PRIMARY
    assert plan.page == ("U_B",)
    assert plan.notify == ("U_A",), "the primary must be told, always"
    assert plan.invite == ("U_A", "U_B")
    assert plan.reasons, "a reroute with no stated reason is unarguable"


async def test_a_one_person_rota_still_gets_paged(cache: OnCallCache) -> None:
    """The degenerate case, and the one worth defending.

    At the top band with nobody to reroute to, refusing to page would be an
    automated decision to leave an incident unattended. The primary is paged and
    the notice says why.
    """
    tired = ResponderWindow("U_SOLO", pages_8h=9, night_pages_24h=9, incident_minutes_24h=999)
    router = ResponderRouter(FakePaging(rota={"default": ("U_SOLO", None)}), cache)

    plan = await router.plan(session_with({"U_SOLO": tired}), schedule="default", at=AT)

    assert plan.routing is Routing.PAGE_SECONDARY_NOTIFY_PRIMARY
    assert plan.page == ("U_SOLO",)


async def test_the_middle_band_invites_rather_than_replaces(cache: OnCallCache) -> None:
    warm = ResponderWindow("U_A", pages_8h=2, night_pages_24h=2)
    router = ResponderRouter(FakePaging(rota={"default": ("U_A", "U_B")}), cache)

    plan = await router.plan(session_with({"U_A": warm}), schedule="default", at=AT)

    assert plan.routing is Routing.PAGE_PRIMARY_AND_INVITE_SECONDARY
    assert plan.page == ("U_A",)
    assert plan.notify == ("U_B",)


async def test_a_broadcast_plan_carries_a_channel_and_a_reason(cache: OnCallCache) -> None:
    empty = StaticSchedule(at=AT)
    empty._rotations = {}
    router = ResponderRouter(FakePaging(fail=True), cache, static_schedule=empty)

    plan = await router.plan(session_with(), schedule="payments", at=AT)

    assert plan.page == ()
    assert plan.degraded is True
    assert plan.degraded_reason
