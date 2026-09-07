"""Choosing the human (W5-05, W5-06, W5-08 — D5, FMEA #14).

This module **decides**; ``orchestration/handlers.py`` **acts**. The split is
INV-03: resolving who is on call is a read and may happen anywhere, while paging
someone and posting in a channel are external writes and may only happen in the
relay. Keeping the decision here also means the whole routing policy is testable
with nothing but a database — no Slack, no pager, no network.

## The ladder, and why every rung announces itself

FMEA #14 is "the paging provider is unreachable". The failure that matters is
not that we cannot page someone; it is that we might *quietly* not page someone,
and the first anyone learns of it is that nobody showed up.

    1. cache            60s TTL, a confident answer from a moment ago
    2. provider         the hosted rota -- the authoritative answer
    3. static rota      config/oncall.yaml: stale, but a name
    4. team broadcast   nobody resolved: shout in the team channel

Rungs 3 and 4 are **degraded** and carry a reason string that the handler posts
in the channel. A silent fallback fails G5, and it should: an incident where the
bot believes it paged someone and did not is strictly worse than one where it
says "I could not reach PagerDuty, here is who the rota says it is."

## The fatigue decision, and the one line worth memorizing

The score is arithmetic; anyone can write a weighted sum. What makes this worth
describing is that **the bot never silently reroutes**. Above 0.75 it pages the
secondary *and* tells the primary, with an opt-in button. Automation that
quietly decides a human is too tired to hear about their own incident would be
resented, and rightly -- they are the one person who knows whether they are fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.adapters.paging.base import OnCall, OnCallSource, PagingError
from incidentpilot.db import queries
from incidentpilot.db import repositories as repo
from incidentpilot.domain.fatigue import (
    Routing,
    fatigue_score,
    routing_decision,
    top_reasons,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """Everything the handler needs, and nothing it has to decide for itself."""

    schedule: str
    oncall: OnCall
    routing: Routing
    score: float
    reasons: tuple[str, ...] = ()
    # Who gets woken up, and who merely gets told. The distinction is the whole
    # differentiator: `notify` is never empty when the primary was displaced.
    page: tuple[str, ...] = ()
    notify: tuple[str, ...] = ()
    invite: tuple[str, ...] = ()
    # Set only when nobody could be resolved at all (rung 4).
    broadcast_channel: str | None = None
    degraded_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        return self.oncall.degraded or self.broadcast_channel is not None

    @property
    def rerouted(self) -> bool:
        return self.routing is Routing.PAGE_SECONDARY_NOTIFY_PRIMARY


class ResponderRouter:
    """Resolves on-call, scores fatigue, and produces a plan. No side effects."""

    def __init__(
        self,
        paging: Any,
        cache: repo.OnCallCache,
        *,
        static_schedule: Any | None = None,
        max_pages: int = 2,
        default_schedule: str = "default",
    ) -> None:
        self._paging = paging
        self._cache = cache
        self._max_pages = max_pages
        self._default_schedule = default_schedule
        # Built lazily and only if needed: a deployment on the static rota
        # already *is* the provider, and building a second copy of it to serve
        # as its own fallback would be a comedy.
        self._static = static_schedule

    # -- the ladder ------------------------------------------------------

    def _static_adapter(self) -> Any:
        if self._static is None:
            from incidentpilot.adapters.paging.static_schedule import StaticSchedule

            self._static = StaticSchedule()
        return self._static

    async def resolve_oncall(self, schedule: str) -> OnCall:
        """Walk the ladder. Never raises; the worst answer is an announced one."""
        cached = await self._cache.get(schedule)
        if cached is not None and cached[0]:
            return OnCall(
                schedule=schedule,
                primary=cached[0],
                secondary=cached[1],
                source=OnCallSource.CACHE,
            )

        try:
            oncall: OnCall = await self._paging.oncall_for(schedule)
            if oncall.known:
                # Only a confident answer is written back -- see OnCallCache.put.
                await self._cache.put(schedule, oncall.primary, oncall.secondary)
                return oncall
            log.warning("oncall.provider_empty", schedule=schedule)
        except (PagingError, TimeoutError, ConnectionError) as exc:
            log.warning("oncall.provider_failed", schedule=schedule, error=str(exc))
            return await self._from_static(schedule, f"paging provider unavailable: {exc}")

        return await self._from_static(schedule, "paging provider returned no on-call")

    async def _from_static(self, schedule: str, why: str) -> OnCall:
        """Rung 3. A stale name beats no name, and it says which it is."""
        try:
            fallback: OnCall = await self._static_adapter().oncall_for(schedule)
        # Broad by intent: the ladder's contract is that it never raises.
        # An exception escaping here would abort routing entirely, which is
        # the silent failure G5 exists to forbid.
        except Exception as exc:
            log.error("oncall.static_failed", schedule=schedule, error=str(exc))
            return OnCall(
                schedule=schedule,
                primary=None,
                source=OnCallSource.TEAM_BROADCAST,
                reason=why,
            )

        if not fallback.known:
            return OnCall(
                schedule=schedule,
                primary=None,
                source=OnCallSource.TEAM_BROADCAST,
                reason=why,
            )
        return OnCall(
            schedule=fallback.schedule,
            primary=fallback.primary,
            secondary=fallback.secondary,
            source=OnCallSource.STATIC_SCHEDULE,
            reason=why,
        )

    def team_channel_for(self, schedule: str) -> str | None:
        channel: str | None = self._static_adapter().team_channel_for(schedule)
        return channel

    # -- the plan --------------------------------------------------------

    async def plan(
        self,
        session: AsyncSession,
        *,
        schedule: str | None,
        at: datetime,
    ) -> RoutingPlan:
        """Resolve, score, decide.

        ``at`` is the incident's detection time, not the wall clock. The score
        is then a property of the incident rather than of when the relay
        happened to get to the row -- which is also what makes the week 7 replay
        route the same incident to the same person every time (INV-10).
        """
        schedule = schedule or self._default_schedule
        oncall = await self.resolve_oncall(schedule)

        if not oncall.known:
            # Rung 4. Nobody resolved: shout, rather than fail quietly.
            return RoutingPlan(
                schedule=schedule,
                oncall=oncall,
                routing=Routing.PAGE_PRIMARY,
                score=0.0,
                broadcast_channel=self.team_channel_for(schedule),
                degraded_reason=oncall.reason or "no on-call could be resolved",
            )

        primary = str(oncall.primary)
        window = await queries.window_for(session, primary, at=at)
        score = fatigue_score(window, self._max_pages)
        decision = routing_decision(score)
        reasons = tuple(top_reasons(window, max_pages=self._max_pages))

        page, notify, invite = self._targets(decision, primary, oncall.secondary)

        log.info(
            "routing.decided",
            schedule=schedule,
            primary=primary,
            secondary=oncall.secondary,
            source=str(oncall.source),
            score=round(score, 3),
            decision=str(decision),
        )
        return RoutingPlan(
            schedule=schedule,
            oncall=oncall,
            routing=decision,
            score=score,
            reasons=reasons,
            page=page,
            notify=notify,
            invite=invite,
            degraded_reason=oncall.reason if oncall.degraded else None,
            meta={"window": window.__dict__ if hasattr(window, "__dict__") else {}},
        )

    def _targets(
        self, decision: Routing, primary: str, secondary: str | None
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Turn a band into concrete people.

        The degenerate case is the interesting one: at the top band with **no
        secondary configured**, there is nobody to reroute to. The primary is
        paged anyway and the notice says why -- refusing to page because the
        rota is one person deep would be an automated decision to leave an
        incident unattended.
        """
        if decision is Routing.PAGE_PRIMARY:
            return (primary,), (), (primary,)

        if decision is Routing.PAGE_PRIMARY_AND_INVITE_SECONDARY:
            if secondary is None:
                return (primary,), (), (primary,)
            return (primary,), (secondary,), (primary, secondary)

        if secondary is None:
            return (primary,), (), (primary,)
        # The primary is notified, never silently dropped.
        return (secondary,), (primary,), (primary, secondary)
