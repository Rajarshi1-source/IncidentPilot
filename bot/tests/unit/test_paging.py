"""W5-01..W5-04: the paging adapters.

Two implementations behind one Protocol, and the static rota is the one that
proves the seam is real -- an interface with a single implementation is a guess
about what varies.

The PagerDuty tests use ``respx`` rather than the vendor SDK because the adapter
speaks plain httpx, which is exactly why it was written that way (W5-02): the
week 7 replay must perform zero network calls, and an SDK's internal client is
far harder to intercept.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from incidentpilot.adapters.paging.base import (
    OnCallSource,
    PagingAdapter,
    PermanentPagingError,
    RetryablePagingError,
)
from incidentpilot.adapters.paging.fake import FakePaging
from incidentpilot.adapters.paging.pagerduty import EVENTS_URL, REST_BASE, PagerDutyPaging
from incidentpilot.adapters.paging.static_schedule import StaticSchedule

MONDAY = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)  # the rotation epoch


# --- the Protocol -------------------------------------------------------------


def test_both_implementations_satisfy_the_protocol() -> None:
    """Structural, not inherited. A third implementation needs no registration."""
    assert isinstance(FakePaging(), PagingAdapter)
    assert isinstance(StaticSchedule(), PagingAdapter)


# --- the static rota (W5-03) --------------------------------------------------


async def test_static_schedule() -> None:
    schedule = StaticSchedule(at=MONDAY)
    oncall = await schedule.oncall_for("payments")

    assert oncall.source is OnCallSource.STATIC_SCHEDULE
    assert oncall.primary == "U_PAY_ANANYA"
    assert oncall.secondary == "U_PAY_RAVI"


async def test_rotation_advances_with_the_period() -> None:
    first = await StaticSchedule(at=MONDAY).oncall_for("payments")
    later = await StaticSchedule(at=MONDAY.replace(day=12)).oncall_for("payments")
    assert first.primary != later.primary


async def test_rotation_is_computed_from_the_incident_not_the_wall_clock() -> None:
    """The replay property (INV-10).

    A schedule that read ``datetime.now()`` would route the same recorded
    incident to a different person on a different day, and every routing
    assertion in the corpus would be a coin flip.
    """
    a = await StaticSchedule(at=MONDAY).oncall_for("payments")
    b = await StaticSchedule(at=MONDAY).oncall_for("payments")
    assert a.primary == b.primary


async def test_an_unknown_schedule_falls_back_to_the_default_rota() -> None:
    oncall = await StaticSchedule(at=MONDAY).oncall_for("a-team-that-does-not-exist")
    assert oncall.primary == "U_ONCALL_A"


async def test_a_one_person_rota_has_no_secondary() -> None:
    schedule = StaticSchedule(at=MONDAY)
    schedule._rotations = {
        "solo": type(
            "R",
            (),
            {"schedule": "solo", "members": ["U_ONLY"], "period_days": 7, "team_channel": "#solo"},
        )()
    }
    oncall = await schedule.oncall_for("solo")
    assert oncall.primary == "U_ONLY"
    assert oncall.secondary is None


async def test_the_static_schedule_refuses_to_pretend_it_paged_someone() -> None:
    """A YAML file cannot ring a phone.

    Returning ``delivered=False`` would let the incident proceed believing a
    human had been woken up; raising forces the caller onto the broadcast rung,
    which is the honest outcome.
    """
    with pytest.raises(PermanentPagingError, match="cannot deliver"):
        await StaticSchedule(at=MONDAY).page(
            "U_PAY_ANANYA", incident_key="inc-1", title="t", severity="sev1"
        )


def test_every_rotation_names_a_team_channel() -> None:
    """Rung 4 of the ladder posts there. A rota with no channel has no rung 4."""
    schedule = StaticSchedule(at=MONDAY)
    for name, rotation in schedule.rotations().items():
        assert rotation.team_channel, f"{name} has no team_channel"
        assert rotation.members, f"{name} has no members"


# --- the fake (W5-04) ---------------------------------------------------------


async def test_a_retried_page_does_not_wake_someone_twice() -> None:
    """The property the outbox's at-least-once retry depends on.

    Without provider-side dedup on ``incident_key``, a crash between the page
    and its record would page again on retry -- and a pager that fires twice per
    incident is a pager people stop reading.
    """
    paging = FakePaging()
    for _ in range(3):
        await paging.page("U1", incident_key="inc-2026-09-07-1", title="t", severity="sev1")
    assert paging.call_count("U1") == 1


async def test_the_fake_can_be_made_to_fail() -> None:
    with pytest.raises(RetryablePagingError):
        await FakePaging(fail=True).oncall_for("payments")


# --- PagerDuty (W5-02) --------------------------------------------------------


@respx.mock
async def test_oncall_reads_the_escalation_level_not_the_first_row() -> None:
    """The API does not promise ordering.

    Taking ``entries[0]`` works until someone adds a second escalation level,
    and then the bot pages the backup instead of the person on call.
    """
    respx.get(f"{REST_BASE}/oncalls").mock(
        return_value=httpx.Response(
            200,
            json={
                "oncalls": [
                    {"escalation_level": 2, "user": {"id": "U_SECOND"}},
                    {"escalation_level": 1, "user": {"id": "U_FIRST"}},
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = PagerDutyPaging(routing_key="rk", api_token="tok", client=client)
        oncall = await adapter.oncall_for("PSCHED")

    assert oncall.primary == "U_FIRST"
    assert oncall.secondary == "U_SECOND"


@respx.mock
async def test_page_sends_the_incident_key_as_the_dedup_key() -> None:
    """Derived, never generated -- the same reason the outbox keys are."""
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(202, json={"status": "success", "dedup_key": "inc-1"})
    )
    async with httpx.AsyncClient() as client:
        adapter = PagerDutyPaging(routing_key="rk", client=client)
        result = await adapter.page(
            "U1", incident_key="inc-1", title="Postgres primary down", severity="sev1"
        )

    assert result.delivered is True
    sent: dict[str, Any] = json.loads(route.calls[0].request.content)
    assert sent["dedup_key"] == "inc-1"
    assert sent["payload"]["severity"] == "critical"


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_statuses_are_retryable(status: int) -> None:
    """The relay defers a retryable error and marks a permanent one dead.

    Getting this split wrong means either eight pointless retries on a 400, or a
    503 that kills the row and silently drops the page.
    """
    respx.post(EVENTS_URL).mock(return_value=httpx.Response(status))
    async with httpx.AsyncClient() as client:
        adapter = PagerDutyPaging(routing_key="rk", client=client)
        with pytest.raises(RetryablePagingError):
            await adapter.page("U1", incident_key="inc-1", title="t", severity="sev1")


@respx.mock
async def test_a_4xx_is_permanent() -> None:
    respx.post(EVENTS_URL).mock(return_value=httpx.Response(400, text="bad routing key"))
    async with httpx.AsyncClient() as client:
        adapter = PagerDutyPaging(routing_key="rk", client=client)
        with pytest.raises(PermanentPagingError):
            await adapter.page("U1", incident_key="inc-1", title="t", severity="sev1")


@respx.mock
async def test_a_network_failure_is_retryable() -> None:
    respx.post(EVENTS_URL).mock(side_effect=httpx.ConnectError("no route to host"))
    async with httpx.AsyncClient() as client:
        adapter = PagerDutyPaging(routing_key="rk", client=client)
        with pytest.raises(RetryablePagingError):
            await adapter.page("U1", incident_key="inc-1", title="t", severity="sev1")


async def test_oncall_without_a_rest_token_fails_loudly() -> None:
    """Two surfaces, two tokens. Half-configuring the provider must not be
    something you discover at 3 a.m."""
    async with httpx.AsyncClient() as client:
        adapter = PagerDutyPaging(routing_key="rk", api_token=None, client=client)
        with pytest.raises(PermanentPagingError, match="REST token"):
            await adapter.oncall_for("PSCHED")
