"""Replay metrics adapter (W7-04).

Returns the recorded PromQL response, matched on the query text. Two behaviours
are deliberate and neither is convenience:

**A query with no recorded response raises ``MetricsUnavailable``.** Not zero,
not an invented number. If the impact code starts asking a question the
recording never asked, that is a change in what impact means, and the corpus
must be re-recorded rather than quietly answered. Fabricating here would let a
new query ship with fake numbers behind it and a green gate.

**A recording can mark metrics unavailable outright.** Three corpus fixtures do,
because "impact says unavailable, never estimates" is a behaviour worth a bucket
of its own -- and the only way to test it is to be unable to answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from eval.recorder import KIND_PROMQL, Recording
from incidentpilot.adapters.metrics.base import (
    InstantValue,
    MetricsUnavailable,
    RangeSeries,
)


class ReplayMetrics:
    """Recorded PromQL, matched on the exact expression."""

    def __init__(self, recording: Recording) -> None:
        self.queries: list[str] = []
        self.unavailable = bool(recording.ground_truth.get("metrics_unavailable", False))
        self._by_query: dict[str, float | None] = {}
        self._reason = str(recording.ground_truth.get("metrics_reason") or "not recorded")
        for entry in recording.of_kind(KIND_PROMQL):
            query = str(entry.get("query") or "")
            response = entry.response
            if response.get("error"):
                self.unavailable = True
                self._reason = str(response["error"])
                continue
            value = response.get("value")
            self._by_query[query] = None if value is None else float(value)

    async def query(self, expr: str, *, at: datetime) -> InstantValue:
        self.queries.append(expr)
        if self.unavailable:
            raise MetricsUnavailable(self._reason)
        if expr not in self._by_query:
            raise MetricsUnavailable(
                f"no recorded response for this query -- the impact queries have changed "
                f"since the corpus was recorded, so the corpus is stale: {expr}"
            )
        return InstantValue(query=expr, value=self._by_query[expr], start=at, end=at)

    async def query_range(
        self, expr: str, *, start: datetime, end: datetime, step_s: int
    ) -> list[RangeSeries]:
        self.queries.append(expr)
        if self.unavailable:
            raise MetricsUnavailable(self._reason)
        value = self._by_query.get(expr)
        if value is None:
            raise MetricsUnavailable(f"no recorded range response: {expr}")
        points: list[tuple[datetime, float]] = []
        moment = start
        while moment <= end:
            points.append((moment, value))
            moment += timedelta(seconds=step_s)
        return [RangeSeries(query=expr, series=f"replay{{q={expr[:24]}}}", points=points)]
