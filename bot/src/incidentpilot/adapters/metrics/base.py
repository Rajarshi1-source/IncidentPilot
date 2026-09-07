"""The metrics boundary (W6-01).

Two methods, because two is what deterministic impact needs: an instant value
over a window (`query`) and a series over one (`query_range`). Anything larger
would be an invitation to compute impact ad hoc at the call site, which is the
B-10 failure in a different costume.

Note what is **absent**: there is no `estimate`, no `approximate`, no default
value. When Prometheus is unreachable the adapter raises and
``impact/promql.py`` records `{"status": "unavailable"}` — never a number. An
honest gap in a document titled "Post-Incident Review" is recoverable; a
confident fabrication in one is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InstantValue:
    """One number, and the query and window that produced it.

    The query text travels with the value on purpose: it is what makes the
    number citable as ``metric:{query}@{t0}-{t1}``, and a number whose
    provenance is not recoverable is not evidence.
    """

    query: str
    value: float | None
    start: datetime
    end: datetime
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RangeSeries:
    query: str
    series: str
    points: list[tuple[datetime, float]]
    labels: dict[str, str] = field(default_factory=dict)


class MetricsError(Exception):
    """The metrics backend could not answer. Never swallowed into a default."""


class MetricsUnavailable(MetricsError):
    """Unreachable, timed out, or returned an error status."""


@runtime_checkable
class MetricsAdapter(Protocol):
    async def query(self, expr: str, *, at: datetime) -> InstantValue: ...

    async def query_range(
        self, expr: str, *, start: datetime, end: datetime, step_s: int
    ) -> list[RangeSeries]: ...


def series_key(labels: dict[str, Any]) -> str:
    """A stable name for one returned series.

    Sorted, so the same series produces the same key on every run -- which is
    what lets ``signal_samples`` have a primary key that survives a replay.
    """
    if not labels:
        return "{}"
    inner = ",".join(f"{k}={labels[k]}" for k in sorted(labels) if k != "__name__")
    name = str(labels.get("__name__", ""))
    return f"{name}{{{inner}}}" if inner else name or "{}"
