"""Alert-storm compression (D3).

PURE MODULE (INV-01). ``cfg`` is a parameter, never an import: reading
``settings`` here would make the module depend on process-global state, which
breaks INV-01 *and* the eval harness's counterfactual mode
(``--set correlation.window_s=600`` works by passing a different cfg).

A cascading failure fires forty alerts. A naive bot creates forty channels,
splits the responders across twelve war rooms, and hits the
``conversations.create`` rate limit at exactly the wrong moment. This module is
what turns that into one incident.

Three independent signals, weighted, with a tuned threshold. Deliberately a
hand-tuned linear score rather than a classifier: you have to explain the merge
in a Slack message, and "0.71: 45s apart, 1 hop from postgres-primary, shared
cluster label" is explainable in a way a model's logit is not. Explainability is
the differentiator over commercial AI grouping, which is typically opaque.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from incidentpilot.domain.graph import ServiceGraph


class CorrelationConfig(Protocol):
    """The knobs correlation reads. A Protocol so tests and the eval harness can
    pass a plain object without constructing application settings.

    Declared as read-only properties, not bare attributes. A Protocol with
    mutable attributes demands a *settable* member, which means a frozen
    dataclass does not satisfy it -- and an immutable config is exactly what you
    want for something the correlator must never mutate mid-run. Correlation
    only ever reads these, so the Protocol should only ever require reading.
    """

    @property
    def correlation_window_s(self) -> int: ...
    @property
    def merge_threshold(self) -> float: ...
    @property
    def storm_threshold(self) -> int: ...


@dataclass(frozen=True, slots=True)
class OpenIncident:
    """The projection of an incident that correlation is allowed to see.

    Deliberately not the ORM row. Correlation must not be able to reach a
    relationship and issue a query, and a narrow frozen view makes that
    structurally impossible rather than merely discouraged.

    **An incident accumulates.** ``affected_services`` and ``last_alert_at``
    grow as alerts merge in, and both are what the next alert is scored against.
    Rev 2's formula compares every alert to the incident's *origin* -- its
    ``primary_service`` and ``detected_at`` -- which measured on the 40-alert
    cascade produces twelve incidents, not one: everything more than two hops
    from the root scores about 0.50 against a 0.62 threshold, and temporal
    decay from t0 has nearly expired by the time the outer ring fires.

    A cascade chains outward. ``checkout-web`` is two hops from
    ``postgres-primary`` but one hop from ``payments-api``, which joined the
    incident thirty seconds ago; and a cascade still firing at t+240s is
    self-evidently still the same incident. Scoring against the accumulated
    front rather than the origin is what makes the topology signal do real work
    instead of only catching the root's immediate neighbours.

    Note what did *not* change: the weights are still 0.4 / 0.4 / 0.2 and the
    threshold is still 0.62. This is not the correlator being loosened until the
    gate passes -- it is the comparison being made against the right thing.
    """

    id: int
    severity_rank: int
    detected_at: datetime
    primary_service: str | None
    stable_labels: dict[str, str] = field(default_factory=dict)
    affected_services: frozenset[str] = frozenset()
    last_alert_at: datetime | None = None

    @property
    def services(self) -> frozenset[str]:
        """Every service implicated so far, including the primary."""
        if self.affected_services:
            return self.affected_services
        return frozenset({self.primary_service}) if self.primary_service else frozenset()

    @property
    def active_at(self) -> datetime:
        """When the incident last showed signs of life."""
        return self.last_alert_at or self.detected_at

    def absorb(self, alert: CorrelatableAlert) -> OpenIncident:
        """Return the incident with this alert folded in. Pure -- no mutation.

        The worker persists the same growth to ``incidents.affected_services``;
        this keeps the in-memory view and the row in step during a storm, when
        forty alerts arrive faster than a round trip per alert.
        """
        merged_labels = {**self.stable_labels}
        for key, value in alert.stable_labels.items():
            merged_labels.setdefault(key, value)

        services = set(self.services)
        if alert.service:
            services.add(alert.service)

        return OpenIncident(
            id=self.id,
            severity_rank=max(self.severity_rank, alert.severity_rank),
            detected_at=min(self.detected_at, alert.starts_at),
            primary_service=self.primary_service,
            stable_labels=merged_labels,
            affected_services=frozenset(services),
            last_alert_at=max(self.active_at, alert.starts_at),
        )


class CorrelatableAlert(Protocol):
    """What correlation needs from an alert. ``NormalizedAlert`` satisfies it."""

    @property
    def severity_rank(self) -> int: ...
    @property
    def starts_at(self) -> datetime: ...
    @property
    def service(self) -> str | None: ...
    @property
    def stable_labels(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class Decision:
    """Merge into an existing incident, or open a new one -- with the why."""

    merge_into: int | None
    score: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def is_merge(self) -> bool:
        return self.merge_into is not None

    @property
    def explanation(self) -> str:
        """The sentence posted in-channel. Opaque grouping is what people
        distrust about commercial tools."""
        if not self.is_merge:
            return "new incident"
        return f"merged — {self.score:.2f} score: " + ", ".join(self.reasons)

    @classmethod
    def new_incident(cls) -> Decision:
        return cls(None)

    @classmethod
    def merge(cls, incident_id: int, score: float, reasons: list[str]) -> Decision:
        return cls(incident_id, score, tuple(reasons))


def jaccard(a: dict[str, str], b: dict[str, str]) -> float:
    """Overlap of two label sets, compared as key=value pairs.

    Pairs rather than keys: two alerts both carrying ``cluster`` is not
    evidence, two alerts both carrying ``cluster=prod-ap-south-1`` is.
    """
    sa = {f"{k}={v}" for k, v in a.items()}
    sb = {f"{k}={v}" for k, v in b.items()}
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _nearest_hop(
    service: str | None,
    candidates: frozenset[str],
    graph: ServiceGraph,
) -> tuple[int, str] | None:
    """Fewest hops from ``service`` to any already-implicated service.

    Returns the distance and which service it was measured to, so the merge
    explanation can name it -- "1 hop from payments-api" is a sentence a
    responder can check; "1 hop" is not.
    """
    if not service or not candidates:
        return None
    best: tuple[int, str] | None = None
    for candidate in candidates:
        hops = graph.distance(service, candidate)
        if hops is None:
            continue
        if best is None or hops < best[0]:
            best = (hops, candidate)
    return best


def correlate(
    alert: CorrelatableAlert,
    open_incidents: list[OpenIncident],
    graph: ServiceGraph,
    cfg: CorrelationConfig,
) -> Decision:
    """Three signals, weighted, **best match wins**.

    Best-match rather than first-match (conflict C-03). During a storm several
    open incidents can clear the threshold simultaneously, and first-match would
    tie the merge target to the sort order of ``open_incidents`` -- which is a
    database ordering. The same 40-alert fixture could then produce different
    results between runs, which is exactly the non-determinism the replay
    harness exists to exclude.
    """
    best = Decision.new_incident()

    for inc in open_incidents:
        # Never absorb a more severe incident into a less severe one. Merging a
        # Sev1 into a Sev3 hides the Sev1, and over-correlation is worse than
        # under-correlation: two channels is a nuisance, a hidden outage is not.
        if inc.severity_rank < alert.severity_rank:
            continue

        score = 0.0
        why: list[str] = []

        # 1. Temporal proximity to the incident's LAST activity, not its start.
        #    A cascade still firing four minutes in is still the same incident;
        #    decaying from t0 would expire the signal exactly when the outer
        #    ring of a large storm arrives.
        dt = (alert.starts_at - inc.active_at).total_seconds()
        if 0 <= dt <= cfg.correlation_window_s:
            score += 0.4 * (1 - dt / cfg.correlation_window_s)
            why.append(f"{int(dt)}s after last activity")

        # 2. Topological closeness to the NEAREST already-implicated service.
        #    Cascades chain outward: checkout-web is two hops from the failing
        #    database but one hop from payments-api, which joined thirty seconds
        #    ago. Measuring only to the origin catches the root's immediate
        #    neighbours and nothing else.
        nearest = _nearest_hop(alert.service, inc.services, graph)
        if nearest is not None:
            hops, via = nearest
            if hops <= 2:
                score += 0.4 * (1 - hops / 3)
                why.append(f"{hops} hop(s) from {via}")

        # 3. Label overlap -- shared cluster, namespace, deploy.
        overlap = jaccard(dict(alert.stable_labels), dict(inc.stable_labels))
        if overlap > 0:
            score += 0.2 * overlap
            why.append(f"label overlap {overlap:.2f}")

        if score >= cfg.merge_threshold and score > best.score:
            best = Decision.merge(inc.id, score, why)

    return best


class RootSignalCandidate(Protocol):
    @property
    def service(self) -> str | None: ...
    @property
    def starts_at(self) -> datetime: ...


def pick_root_signal[T: RootSignalCandidate](alerts: list[T], graph: ServiceGraph) -> T | None:
    """The FIRST alert on the DEEPEST failing dependency.

    Not the loudest, not the newest, not the first to arrive. A database fails
    before the six services that depend on it, and the runbook that helps is the
    database one. Getting this right is the difference between a war room that
    opens with "restart the payments pods" and one that opens with "promote the
    replica".
    """
    if not alerts:
        return None
    return min(alerts, key=lambda a: (-graph.depth(a.service), a.starts_at))


def is_storm(correlated_alert_count: int, cfg: CorrelationConfig) -> bool:
    """Whether an incident has absorbed enough alerts to be worth announcing.

    Conflict C-11: ``storm_threshold`` is configured in every source and read by
    none of them. It gates the banner and the metric, not the merge decision --
    without it the demo's defining moment ("40 correlated alerts, root signal:
    postgres-primary") has no visible output.
    """
    return correlated_alert_count > cfg.storm_threshold
