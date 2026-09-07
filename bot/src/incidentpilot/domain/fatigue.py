"""Fatigue-aware routing (W5-06, D5).

PURE MODULE (INV-01). No clock, no database, no config import.

**C-04, resolved in favour of the starter code.** Rev 2 declares
``fatigue_score(r: ResponderWindow, now: datetime)`` — and never uses ``now`` in
the body. A clock in ``domain/`` is the first cause of flaky replay, and this
scorer runs inside the week 7 corpus pass where the same input must produce the
same output every time. So all time-window arithmetic happens in SQL (``Q7``,
``db/queries.py``) and arrives here as five plain numbers.

**The differentiator is the restraint, not the arithmetic.** Any weighted sum
would produce a number. What makes D5 worth describing is that the bot never
silently reroutes: above 0.75 it pages the secondary *and* tells the primary,
with an opt-in button. Automation that quietly decides a human is too tired to
be told about their own incident would be resented, and rightly — the on-call
engineer is the one person who knows whether they are actually fine.

The weights are a starting point, not a finding. They are stated here rather
than tuned in secret, and `docs/SLO.md` does not claim they are validated: with
30 incidents a day it takes months to gather enough paging history to fit them
against anything real.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Band boundaries. Named because they appear in the announcement copy, in the
# tests, and in the interview answer, and three copies of 0.75 would drift.
INVITE_SECONDARY_AT = 0.50
REROUTE_AT = 0.75


class Routing(StrEnum):
    PAGE_PRIMARY = "page_primary"
    PAGE_PRIMARY_AND_INVITE_SECONDARY = "page_primary_and_invite_secondary"
    PAGE_SECONDARY_NOTIFY_PRIMARY = "page_secondary_notify_primary"


@dataclass(frozen=True, slots=True)
class ResponderWindow:
    """One responder's recent load, as computed by ``Q7``.

    Every field is a count or a duration over a window the query already closed.
    Nothing here is derived from the current time, which is what keeps the
    scorer pure and the replay deterministic.
    """

    responder: str
    pages_8h: int = 0
    night_pages_24h: int = 0
    incident_minutes_24h: int = 0
    consecutive_oncall_days: int = 0
    sev1_count_7d: int = 0


@dataclass(frozen=True, slots=True)
class FatigueComponent:
    """One term of the score, kept so the announcement can explain itself.

    A number with no reasons attached is exactly the opaque automation this
    feature exists to avoid being. "0.81" convinces nobody; "3 pages in 8h, 2 of
    them at night, 4h in incidents" is a claim someone can disagree with.
    """

    name: str
    weight: float
    ratio: float
    detail: str

    @property
    def contribution(self) -> float:
        return self.weight * self.ratio


def _capped(value: float, ceiling: float) -> float:
    """``min(value / ceiling, 1.0)``, guarding a zero ceiling.

    Each term saturates on purpose: the difference between four pages and
    fourteen does not matter, because both mean "this person has had enough".
    A term that kept growing would let one extreme input dominate the other
    four and turn a five-signal score into a one-signal one.
    """
    if ceiling <= 0:
        return 0.0
    return min(value / ceiling, 1.0)


def fatigue_components(
    window: ResponderWindow,
    *,
    max_pages: int = 2,
) -> tuple[FatigueComponent, ...]:
    """The five terms, with their reasons. ``fatigue_score`` sums these."""
    return (
        FatigueComponent(
            "recent_pages",
            0.30,
            _capped(window.pages_8h, max_pages),
            f"{window.pages_8h} page{'s' if window.pages_8h != 1 else ''} in 8h",
        ),
        FatigueComponent(
            "night_pages",
            0.25,
            _capped(window.night_pages_24h, 2),
            f"{window.night_pages_24h} outside 08:00-22:00 local",
        ),
        # Time *in* incidents, not just paged. Being paged once and then living
        # in a war room for four hours is the more exhausting shape, and a
        # page-count-only score would rate it as a quiet day.
        FatigueComponent(
            "incident_time",
            0.20,
            _capped(window.incident_minutes_24h, 240),
            f"{window.incident_minutes_24h} min in incidents in 24h",
        ),
        FatigueComponent(
            "long_rotation",
            0.15,
            1.0 if window.consecutive_oncall_days >= 5 else 0.0,
            f"{window.consecutive_oncall_days} consecutive on-call days",
        ),
        FatigueComponent(
            "sev1_load",
            0.10,
            _capped(window.sev1_count_7d, 3),
            f"{window.sev1_count_7d} sev1 in 7 days",
        ),
    )


def fatigue_score(window: ResponderWindow, max_pages: int = 2) -> float:
    """0.0 (fresh) to 1.0 (saturated). Pure, deterministic, clock-free."""
    return min(sum(c.contribution for c in fatigue_components(window, max_pages=max_pages)), 1.0)


def routing_decision(score: float) -> Routing:
    """Three bands, and the top one still tells the primary.

    ``page_secondary_notify_primary`` is deliberately not
    ``page_secondary_instead``. The primary is notified with an opt-in ("I'm
    good, add me") because the alternative is a system that removes someone
    from their own incident without telling them.
    """
    if score < INVITE_SECONDARY_AT:
        return Routing.PAGE_PRIMARY
    if score < REROUTE_AT:
        return Routing.PAGE_PRIMARY_AND_INVITE_SECONDARY
    return Routing.PAGE_SECONDARY_NOTIFY_PRIMARY


def top_reasons(window: ResponderWindow, *, limit: int = 3, max_pages: int = 2) -> list[str]:
    """The largest contributing terms, for the in-channel announcement.

    Only terms that actually contributed: listing "0 sev1 in 7 days" as a reason
    someone is tired would make the whole explanation look automated in the
    pejorative sense.
    """
    contributing = [c for c in fatigue_components(window, max_pages=max_pages) if c.ratio > 0]
    contributing.sort(key=lambda c: c.contribution, reverse=True)
    return [c.detail for c in contributing[:limit]]
