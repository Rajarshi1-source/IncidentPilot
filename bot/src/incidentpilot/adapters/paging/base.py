"""The paging boundary (W5-01).

Two implementations ship — a hosted provider and a YAML rota — and that is the
point rather than a hedge. An interface with one implementation is a guess about
what varies; the static schedule is what proves the seam is real, and it is also
what lets a clean clone page someone (into a fake channel) with no vendor
account at all.

The interface is deliberately two methods wide. ``oncall_for`` is a **read** and
may be called from anywhere; ``page`` is an **external write** and, like every
other external write in this system, may only be called from
``orchestration/handlers.py`` (INV-03). That asymmetry is enforced by
``test_no_external_writes_outside_relay``, which lists ``page`` among the write
methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class OnCallSource(StrEnum):
    """Which rung of the fallback ladder answered.

    Carried on the result rather than logged, because W5-08's rule is that a
    degraded answer must be *announced*: the handler reads this to decide
    whether the channel gets a notice, and the G5 gate asserts on it.
    """

    PROVIDER = "provider"
    CACHE = "cache"
    STATIC_SCHEDULE = "static_schedule"
    TEAM_BROADCAST = "team_broadcast"


# Everything below PROVIDER is a degraded answer and says so in the channel.
DEGRADED_SOURCES: frozenset[OnCallSource] = frozenset(
    {OnCallSource.STATIC_SCHEDULE, OnCallSource.TEAM_BROADCAST}
)


@dataclass(frozen=True, slots=True)
class OnCall:
    """Who is on call, and how confident we are that this is current."""

    schedule: str
    primary: str | None
    secondary: str | None = None
    source: OnCallSource = OnCallSource.PROVIDER
    # Set when the ladder fell through, e.g. "PagerDuty returned 503".
    reason: str | None = None

    @property
    def degraded(self) -> bool:
        return self.source in DEGRADED_SOURCES

    @property
    def known(self) -> bool:
        return self.primary is not None


@dataclass(frozen=True, slots=True)
class PageResult:
    """What the provider did with the page. ``ref`` is the vendor's own id."""

    responder: str
    delivered: bool
    ref: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class PagingError(Exception):
    """Any failure from the paging provider."""


class RetryablePagingError(PagingError):
    """A 429, a 5xx, a timeout. Safe to retry -- the dedup key makes it so."""


class PermanentPagingError(PagingError):
    """A 4xx that will fail identically forever."""


@runtime_checkable
class PagingAdapter(Protocol):
    """Everything the system is allowed to ask a paging provider for."""

    async def oncall_for(self, schedule: str) -> OnCall:
        """Who is on call. A read -- callable from anywhere."""
        ...

    async def page(
        self,
        user: str,
        *,
        incident_key: str,
        title: str,
        severity: str,
        url: str | None = ...,
    ) -> PageResult:
        """Wake someone up. An external write -- relay only (INV-03).

        ``incident_key`` is the deduplication key the provider keys on, and it
        is the incident's ``public_key`` rather than a generated id for exactly
        the reason the outbox has idempotency keys: a retried page must reach
        the same alert, not create a second one. Waking someone twice for one
        incident is how a pager stops being trusted.
        """
        ...
