"""In-memory metrics adapter (W6-01).

Deterministic by construction: the same query over the same window returns the
same number every time. That is what lets the week 7 replay assert on a PIR's
impact block, and it is why the values are generated from a hash of the query
rather than from a random number generator with a seed -- a seeded RNG is
deterministic only if nobody changes the order of the calls.

``unavailable=True`` makes it raise, which is how ``test_metrics_unavailable_is_honest``
proves that impact degrades to "unavailable" rather than to a plausible number.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from incidentpilot.adapters.metrics.base import (
    InstantValue,
    MetricsUnavailable,
    RangeSeries,
)


def _deterministic(expr: str, salt: str = "") -> float:
    """A stable pseudo-value in [0, 1000) derived from the query text."""
    digest = hashlib.sha256(f"{expr}|{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100_000 / 100.0


class FakeMetrics:
    """Records every query so tests can assert on the window, not just the value."""

    def __init__(
        self,
        *,
        values: dict[str, float] | None = None,
        unavailable: bool = False,
    ) -> None:
        self.values = values or {}
        self.unavailable = unavailable
        self.queries: list[tuple[str, datetime, datetime]] = []

    async def query(self, expr: str, *, at: datetime) -> InstantValue:
        if self.unavailable:
            raise MetricsUnavailable("fake metrics backend is unavailable")
        self.queries.append((expr, at, at))
        value = self.values.get(expr, _deterministic(expr))
        return InstantValue(query=expr, value=value, start=at, end=at)

    async def query_range(
        self, expr: str, *, start: datetime, end: datetime, step_s: int
    ) -> list[RangeSeries]:
        if self.unavailable:
            raise MetricsUnavailable("fake metrics backend is unavailable")
        self.queries.append((expr, start, end))

        points: list[tuple[datetime, float]] = []
        moment = start
        index = 0
        while moment <= end:
            points.append((moment, _deterministic(expr, str(index))))
            moment += timedelta(seconds=step_s)
            index += 1
        return [RangeSeries(query=expr, series=f"fake{{q={expr[:24]}}}", points=points)]
