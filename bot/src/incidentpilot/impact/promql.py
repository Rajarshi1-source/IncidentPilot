"""Impact is COMPUTED, never generated (W6-02, W6-05 — B-10, INV-06).

This module runs **before** any model call, and the ordering is the whole point.
A model asked to estimate impact will produce a plausible number; that number
will appear in a document titled "Post-Incident Review"; and someone will put it
in a board deck. So the numbers are computed here from PromQL over the incident
window, injected into the prompt as verified facts, and `impact` is deliberately
**absent from `PIRDraft`** — if it were a schema field, the model would fill it.

The validator closes the loop from the other side: any number appearing in the
draft that is not in ``computed_impact_numbers`` is an ungrounded numeric claim
and rejects the draft.

**When Prometheus is unreachable, the answer is "unavailable" (W6-05, FMEA #15).**
Not zero, not "approximately", not last week's figure. A gap that says it is a
gap is recoverable; a confident fabrication in a postmortem is not, and the
distinction is worth more in an interview than the query set is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from incidentpilot.adapters.metrics.base import MetricsError
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

STATUS_OK = "computed"
STATUS_UNAVAILABLE = "unavailable"

# The four queries, verbatim from the plan. Doubled braces are PromQL label
# selectors surviving ``str.format``; the single-braced names are the
# substitutions. Written this way rather than with an f-string so the template
# and the rendered query are both inspectable -- the template is what a reviewer
# checks, the rendered query is what gets cited.
IMPACT_QUERIES: dict[str, str] = {
    "failed_requests": ('sum(increase(http_requests_total{{service="{svc}",status=~"5.."}}[{w}]))'),
    "total_requests": 'sum(increase(http_requests_total{{service="{svc}"}}[{w}]))',
    "p99_latency_ms": (
        "histogram_quantile(0.99, sum by (le) "
        '(rate(http_request_duration_seconds_bucket{{service="{svc}"}}[{w}]))) * 1000'
    ),
    "pods_unready": 'count(kube_pod_status_ready{{condition="false",namespace="{ns}"}})',
}

# Derived, not queried: a ratio the reader actually wants, computed from two
# numbers that are each independently citable.
DERIVED = ("error_rate_pct",)


@dataclass(frozen=True, slots=True)
class MetricWindow:
    """One computed number and the citation that resolves to it.

    ``ref`` is the ``metric:{query}@{t0}-{t1}`` form from the evidence grammar.
    It is built here rather than by the context builder because this is the only
    place that knows the exact query text and the exact window -- and a citation
    whose window was reconstructed later is a citation that can drift.
    """

    key: str
    query: str
    value: float | None
    start: datetime
    end: datetime

    @property
    def ref(self) -> str:
        from incidentpilot.domain.evidence import metric_ref

        return metric_ref(self.query, self.start, self.end)


@dataclass(frozen=True, slots=True)
class ImpactReport:
    status: str
    window_start: datetime
    window_end: datetime
    service: str | None
    values: dict[str, float | None] = field(default_factory=dict)
    windows: tuple[MetricWindow, ...] = ()
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == STATUS_OK

    def as_json(self) -> dict[str, Any]:
        """What lands in ``incidents.impact``.

        ``status`` is always present, so a reader (or the renderer) can tell
        "we looked and there was nothing" from "we could not look" without
        inspecting whether the numbers happen to be null.
        """
        payload: dict[str, Any] = {
            "status": self.status,
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "service": self.service,
        }
        if self.available:
            payload["values"] = dict(self.values)
        if self.reason:
            payload["reason"] = self.reason
        return payload

    def numbers(self) -> set[str]:
        """Every computed figure, as the strings a draft might contain.

        The validator compares against this set, so the formatting has to match
        what a model would plausibly write: `1234`, `1234.5` and `12.3` all
        appear, because rejecting a correct number over a trailing zero would
        push honest drafts to the skeleton.
        """
        out: set[str] = set()
        for value in self.values.values():
            if value is None:
                continue
            out.update(_number_forms(value))
        return out


def _number_forms(value: float) -> set[str]:
    """Every reasonable rendering of one computed number."""
    forms = {f"{value:.1f}", f"{value:.2f}", f"{value:g}"}
    if value == int(value):
        forms.add(str(int(value)))
    forms.add(str(round(value)))
    return forms


def window_for(detected_at: datetime, resolved_at: datetime | None, *, now: datetime) -> str:
    """The PromQL range literal for this incident's duration.

    Rounded up to whole minutes and floored at one: `increase(...[0s])` is an
    error, and an incident resolved in forty seconds is still an incident whose
    impact someone will ask about.
    """
    end = resolved_at or now
    minutes = max(1, int((end - detected_at).total_seconds() // 60) + 1)
    return f"{minutes}m"


async def compute_impact(
    metrics: Any,
    *,
    service: str | None,
    namespace: str | None,
    detected_at: datetime,
    resolved_at: datetime | None,
    now: datetime,
) -> ImpactReport:
    """Run the four queries over the incident window. Deterministic, no model.

    A single failure fails the whole report rather than producing a partial one.
    Half an impact block is worse than none: a reader sees three numbers and a
    blank, assumes the blank is a zero, and the document is now wrong in a way
    nobody flagged.
    """
    end = resolved_at or now
    window = window_for(detected_at, resolved_at, now=now)
    svc = service or "unknown"
    ns = namespace or "default"

    values: dict[str, float | None] = {}
    windows: list[MetricWindow] = []

    for key, template in IMPACT_QUERIES.items():
        expr = template.format(svc=svc, w=window, ns=ns)
        try:
            instant = await metrics.query(expr, at=end)
        except MetricsError as exc:
            log.warning("impact.unavailable", query=key, error=str(exc))
            return ImpactReport(
                status=STATUS_UNAVAILABLE,
                window_start=detected_at,
                window_end=end,
                service=service,
                reason=f"{key}: {exc}",
            )
        values[key] = instant.value
        windows.append(
            MetricWindow(key=key, query=expr, value=instant.value, start=detected_at, end=end)
        )

    failed = values.get("failed_requests")
    total = values.get("total_requests")
    if failed is not None and total:
        values["error_rate_pct"] = round(failed / total * 100, 2)

    log.info("impact.computed", service=service, window=window, keys=sorted(values))
    return ImpactReport(
        status=STATUS_OK,
        window_start=detected_at,
        window_end=end,
        service=service,
        values=values,
        windows=tuple(windows),
    )


def unavailable(
    detected_at: datetime, end: datetime, *, service: str | None, reason: str
) -> ImpactReport:
    """The honest empty report, for callers with no metrics adapter at all."""
    return ImpactReport(
        status=STATUS_UNAVAILABLE,
        window_start=detected_at,
        window_end=end,
        service=service,
        reason=reason,
    )


def verified_facts_block(report: ImpactReport) -> str:
    """The fenced block the prompt calls "verified facts".

    Labelled unmistakably, because the model's instruction is *reuse these
    exactly, do not compute new ones* -- and a block that reads like ordinary
    context is a block the model will paraphrase into a new number.
    """
    if not report.available:
        return (
            "VERIFIED FACTS (computed, not generated)\n"
            "  metrics unavailable for this window — do NOT estimate impact.\n"
            f"  reason: {report.reason or 'unknown'}"
        )

    lines = ["VERIFIED FACTS (computed, not generated — reuse these exactly)"]
    duration = report.window_end - report.window_start
    lines.append(f"  incident_duration_minutes: {int(duration / timedelta(minutes=1))}")
    for key, value in sorted(report.values.items()):
        lines.append(f"  {key}: {'unknown' if value is None else f'{value:g}'}")
    return "\n".join(lines)
