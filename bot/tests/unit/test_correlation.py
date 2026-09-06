"""Storm compression, the dependency graph, and severity (D3, W2-08..W2-10)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from incidentpilot.config.graph_loader import load_service_graph
from incidentpilot.domain.correlation import (
    Decision,
    OpenIncident,
    correlate,
    is_storm,
    jaccard,
    pick_root_signal,
)
from incidentpilot.domain.graph import ServiceGraph
from incidentpilot.domain.normalize import NormalizedAlert, normalize_alertmanager
from incidentpilot.domain.severity import assess, escalate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T0 = datetime(2026, 9, 6, 9, 14, 2, tzinfo=UTC)


@dataclass(frozen=True)
class Cfg:
    """The correlation knobs, as a plain object. Correlation takes cfg as a
    parameter precisely so this is possible without application settings."""

    correlation_window_s: int = 300
    merge_threshold: float = 0.62
    storm_threshold: int = 5


GRAPH = load_service_graph()


def _storm(n: int) -> list[NormalizedAlert]:
    payload = json.loads((FIXTURES / f"storm_{n}.json").read_text(encoding="utf-8"))
    return [normalize_alertmanager(a, payload) for a in payload["alerts"]]


# --- the graph ----------------------------------------------------------------


def test_graph_loads_from_the_checked_in_yaml() -> None:
    assert "postgres-primary" in GRAPH.services()
    assert "payments-api" in GRAPH.services()


def test_graph_depth_stable() -> None:
    """Depth grows *downward through the stack*: a datastore everything rests
    on is deep, an edge API that nothing depends on is depth 0.

    This orientation is what ``pick_root_signal`` needs, since it maximizes
    depth to find the deepest failing dependency. Measuring it the other way --
    longest chain below a service -- inverts the answer and makes root-signal
    selection pick ``checkout-web``, the loudest symptom, which is the precise
    failure D3 exists to prevent.
    """
    assert GRAPH.depth("checkout-web") == 0, "nothing depends on the edge service"
    assert GRAPH.depth("payments-api") > GRAPH.depth("checkout-web")
    assert GRAPH.depth("postgres-primary") > GRAPH.depth("payments-api")
    assert GRAPH.depth("postgres-primary") == max(GRAPH.depth(s) for s in GRAPH.services()), (
        "the primary datastore should be the deepest node in this topology"
    )


def test_graph_depth_of_an_unknown_service_is_zero() -> None:
    assert GRAPH.depth("service-that-does-not-exist") == 0
    assert GRAPH.depth(None) == 0


def test_graph_distance_is_undirected() -> None:
    """Correlation cares about relatedness, not causality direction."""
    down = GRAPH.distance("payments-api", "postgres-primary")
    up = GRAPH.distance("postgres-primary", "payments-api")
    assert down == up == 1


def test_graph_distance_to_self_is_zero() -> None:
    assert GRAPH.distance("payments-api", "payments-api") == 0


def test_graph_distance_is_none_for_unknown_services() -> None:
    assert GRAPH.distance("payments-api", "nope") is None
    assert GRAPH.distance(None, "payments-api") is None


def test_graph_tolerates_a_cycle_without_hanging() -> None:
    """A graph derived from live traces will contain a cycle eventually, and a
    correlator that crashes on one is worse than one that scores it imperfectly."""
    cyclic = ServiceGraph.from_mapping({"a": ["b"], "b": ["c"], "c": ["a"]})
    assert cyclic.depth("a") >= 0  # terminates
    assert cyclic.distance("a", "c") == 1


def test_empty_graph_is_a_real_operating_mode() -> None:
    empty = ServiceGraph.empty()
    assert empty.depth("anything") == 0
    assert empty.distance("a", "b") is None


# --- jaccard ------------------------------------------------------------------


def test_jaccard_compares_pairs_not_keys() -> None:
    """Two alerts both carrying `cluster` is not evidence; both carrying
    `cluster=prod` is."""
    same = jaccard({"cluster": "prod"}, {"cluster": "prod"})
    different = jaccard({"cluster": "prod"}, {"cluster": "staging"})
    assert same == 1.0
    assert different == 0.0


def test_jaccard_of_empty_sets_is_zero() -> None:
    assert jaccard({}, {}) == 0.0


# --- correlation --------------------------------------------------------------


def test_first_alert_opens_a_new_incident() -> None:
    alerts = _storm(5)
    decision = correlate(alerts[0], [], GRAPH, Cfg())
    assert not decision.is_merge
    assert decision.explanation == "new incident"


def test_related_alert_merges_and_explains_itself() -> None:
    alerts = _storm(12)
    root = alerts[0]
    incident = OpenIncident(
        id=1,
        severity_rank=root.severity_rank,
        detected_at=root.starts_at,
        primary_service=root.service,
        stable_labels=root.stable_labels,
    )
    decision = correlate(alerts[2], [incident], GRAPH, Cfg())

    assert decision.is_merge
    assert decision.merge_into == 1
    assert decision.reasons, "a merge with no reasons is opaque grouping"
    assert "merged" in decision.explanation


def test_best_match_wins_not_first_match() -> None:
    """Conflict C-03, asserted directly.

    Two open incidents both clear the threshold. First-match would return
    whichever the caller happened to list first -- a database sort order -- so
    the same fixture could produce different results between runs. That is
    exactly the non-determinism the replay harness exists to exclude.
    """
    alert = _storm(12)[4]  # HighErrorRate on payments-api

    close = OpenIncident(
        id=99,
        severity_rank=alert.severity_rank,
        detected_at=alert.starts_at - timedelta(seconds=5),
        primary_service="payments-api",
        stable_labels=alert.stable_labels,
    )
    far = OpenIncident(
        id=11,
        severity_rank=alert.severity_rank,
        detected_at=alert.starts_at - timedelta(seconds=280),
        primary_service="postgres-primary",
        stable_labels={"cluster": alert.stable_labels.get("cluster", "")},
    )

    forward = correlate(alert, [far, close], GRAPH, Cfg())
    reversed_order = correlate(alert, [close, far], GRAPH, Cfg())

    assert forward.merge_into == reversed_order.merge_into == 99
    assert forward.score == reversed_order.score


def test_correlation_never_absorbs_a_more_severe_incident() -> None:
    """Merging a Sev1 into a Sev3 hides the Sev1. Over-correlation is worse than
    under-correlation: two channels is a nuisance, a hidden outage is not."""
    sev1 = _storm(5)[0]
    assert sev1.severity == "sev1"

    sev3_incident = OpenIncident(
        id=7,
        severity_rank=2,  # sev3
        detected_at=sev1.starts_at - timedelta(seconds=5),
        primary_service=sev1.service,
        stable_labels=sev1.stable_labels,
    )
    assert not correlate(sev1, [sev3_incident], GRAPH, Cfg()).is_merge


def test_an_alert_outside_the_window_does_not_merge_on_time() -> None:
    alert = _storm(5)[0]
    stale = OpenIncident(
        id=3,
        severity_rank=alert.severity_rank,
        detected_at=alert.starts_at - timedelta(hours=6),
        primary_service="kafka-events",
        stable_labels={},
    )
    assert not correlate(alert, [stale], GRAPH, Cfg()).is_merge


def test_a_wider_window_merges_more() -> None:
    """The counterfactual the eval harness runs: --set correlation.window_s=600.

    It works only because ``cfg`` is a parameter rather than a module import --
    the same reason the correlator is testable without application settings.

    The incident here already implicates the alert's own service, so topology
    contributes fully and the window is the only thing that decides the merge.
    """
    alert = _storm(12)[4]  # HighErrorRate on payments-api
    assert alert.service == "payments-api"

    quiet_for_400s = OpenIncident(
        id=5,
        severity_rank=alert.severity_rank,
        detected_at=alert.starts_at - timedelta(seconds=600),
        primary_service="postgres-primary",
        stable_labels={"cluster": "prod-ap-south-1", "severity": "critical"},
        affected_services=frozenset({"postgres-primary", "payments-api"}),
        last_alert_at=alert.starts_at - timedelta(seconds=400),
    )

    narrow = correlate(alert, [quiet_for_400s], GRAPH, Cfg(correlation_window_s=300))
    wide = correlate(alert, [quiet_for_400s], GRAPH, Cfg(correlation_window_s=900))

    assert not narrow.is_merge, "400s of silence is outside a 300s window"
    assert wide.is_merge, "the same gap is inside a 900s window"
    assert wide.score > narrow.score


# --- root signal --------------------------------------------------------------


def test_root_signal_is_the_deepest_dependency_not_the_loudest() -> None:
    """The whole point of D3. A database fails before the six services that
    depend on it, and the runbook that helps is the database one."""
    alerts = _storm(40)
    root = pick_root_signal(alerts, GRAPH)
    assert root is not None
    assert root.service == "postgres-primary"


def test_root_signal_is_the_first_alert_on_that_dependency() -> None:
    alerts = _storm(40)
    root = pick_root_signal(alerts, GRAPH)
    assert root is not None
    same_service = [a for a in alerts if a.service == root.service]
    assert root.starts_at == min(a.starts_at for a in same_service)


def test_root_signal_of_an_empty_list_is_none() -> None:
    assert pick_root_signal([], GRAPH) is None


def test_root_signal_without_a_graph_falls_back_to_earliest() -> None:
    """No topology is a real operating mode; it degrades, it does not crash."""
    alerts = _storm(12)
    root = pick_root_signal(alerts, ServiceGraph.empty())
    assert root is not None
    assert root.starts_at == min(a.starts_at for a in alerts)


# --- storm threshold (C-11) ---------------------------------------------------


def test_is_storm_uses_the_configured_threshold() -> None:
    """Conflict C-11: storm_threshold is configured everywhere and read nowhere.
    It gates the banner and the metric, not the merge decision."""
    cfg = Cfg(storm_threshold=5)
    assert not is_storm(5, cfg)
    assert is_storm(6, cfg)


# --- severity -----------------------------------------------------------------


def test_severity_reason_is_auditable() -> None:
    """Severity drives paging, so it must be explainable from the row alone."""
    decision = assess(
        alert_severity="sev2",
        service_tier=1,
        affected_service_count=4,
        error_budget_burn=21.7,
    )
    assert decision.level == "sev1"
    assert "error budget burn 21.7x" in decision.reason
    assert decision.reason.startswith("sev1:")
    assert decision.is_paging


def test_severity_never_drops_below_the_alert_label() -> None:
    decision = assess(alert_severity="sev1", service_tier=3)
    assert decision.level == "sev1"


def test_tier_one_service_raises_the_floor() -> None:
    decision = assess(alert_severity="sev4", service_tier=1)
    assert decision.level == "sev2"
    assert "tier-1 service" in decision.reason


def test_two_tier_one_services_is_a_sev1() -> None:
    decision = assess(alert_severity="sev3", tier_one_services=2)
    assert decision.level == "sev1"


def test_escalation_is_attributed() -> None:
    decision = escalate("sev3", "sev1", actor="U123", note="customer escalation")
    assert decision.level == "sev1"
    assert "U123" in decision.reason
    assert "customer escalation" in decision.reason


def test_de_escalation_is_refused_not_applied_silently() -> None:
    """An incident that quietly drops from sev1 to sev3 while nobody is looking
    is how a page stops arriving."""
    decision = escalate("sev1", "sev3", actor="U123")
    assert decision.level == "sev1"
    assert "refused" in decision.reason


# --- the whole storm ----------------------------------------------------------


@pytest.mark.parametrize("n", [5, 12, 40])
def test_storm_fixtures_compress_to_one_incident(n: int) -> None:
    """G2's correlation half, in pure code -- no database needed.

    Replays the fixture through the same decision function the worker uses and
    asserts the cascade collapses to a single incident.
    """
    alerts = _storm(n)
    cfg = Cfg()
    incidents: list[OpenIncident] = []
    merges = 0

    for alert in alerts:
        decision = correlate(alert, incidents, GRAPH, cfg)
        if decision.is_merge:
            merges += 1
            # The incident accumulates: services and last-activity grow, and the
            # next alert is scored against the new front. The worker persists
            # the same growth to the row.
            idx = next(i for i, inc in enumerate(incidents) if inc.id == decision.merge_into)
            incidents[idx] = incidents[idx].absorb(alert)
        else:
            incidents.append(
                OpenIncident(
                    id=len(incidents) + 1,
                    severity_rank=alert.severity_rank,
                    detected_at=alert.starts_at,
                    primary_service=alert.service,
                    stable_labels=alert.stable_labels,
                    affected_services=frozenset({alert.service} if alert.service else set()),
                    last_alert_at=alert.starts_at,
                )
            )

    assert len(incidents) == 1, (
        f"storm_{n} produced {len(incidents)} incidents; "
        f"reasons: {[i.primary_service for i in incidents]}"
    )
    assert merges == n - 1

    root = pick_root_signal(alerts, GRAPH)
    assert root is not None and root.service == "postgres-primary"


def test_decision_new_incident_has_zero_score() -> None:
    assert Decision.new_incident().score == 0.0
    assert not Decision.new_incident().is_merge


# --- defensive branches in the pure layer -------------------------------------


def test_dependencies_of_an_unknown_service_is_empty() -> None:
    assert GRAPH.dependencies("service-that-does-not-exist") == ()
    assert GRAPH.dependencies("postgres-primary") == ()


def test_distance_between_two_disconnected_components_is_none() -> None:
    """BFS exhausts the frontier without reaching the target.

    A real graph has islands -- a service nobody has wired up yet, or a
    third-party dependency reached only through a component we do not model.
    Correlation must return "unrelated", not raise.
    """
    split = ServiceGraph.from_mapping({"a": ["b"], "island": ["outpost"]})
    assert split.distance("a", "island") is None
    assert split.distance("b", "outpost") is None


def test_correlation_ignores_an_incident_with_no_services() -> None:
    """A freshly created incident whose service is unknown offers no topology
    signal; the scorer must skip it rather than treat None as a match."""
    alert = _storm(5)[0]
    faceless = OpenIncident(
        id=42,
        severity_rank=alert.severity_rank,
        detected_at=alert.starts_at,
        primary_service=None,
        stable_labels={},
        affected_services=frozenset(),
        last_alert_at=alert.starts_at,
    )
    decision = correlate(alert, [faceless], GRAPH, Cfg())
    assert not decision.is_merge


def test_correlation_handles_an_alert_with_no_service() -> None:
    """Cluster-level alerts carry no service label at all."""
    alerts = _storm(5)
    root = alerts[0]
    incident = OpenIncident(
        id=1,
        severity_rank=root.severity_rank,
        detected_at=root.starts_at,
        primary_service=root.service,
        stable_labels=root.stable_labels,
        affected_services=frozenset({root.service or ""}),
        last_alert_at=root.starts_at,
    )
    serviceless = normalize_alertmanager(
        {
            "labels": {"alertname": "ClusterWideOutage", "severity": "critical"},
            "startsAt": root.starts_at.isoformat().replace("+00:00", "Z"),
        },
        {},
    )
    decision = correlate(serviceless, [incident], GRAPH, Cfg())
    assert isinstance(decision, Decision)  # scored on time + labels only, no crash


def test_absorb_grows_services_and_last_activity() -> None:
    """The persistent side mirrors this; both must move together during a storm."""
    alerts = _storm(12)
    incident = OpenIncident(
        id=1,
        severity_rank=alerts[0].severity_rank,
        detected_at=alerts[0].starts_at,
        primary_service=alerts[0].service,
        stable_labels=alerts[0].stable_labels,
        affected_services=frozenset({alerts[0].service or ""}),
        last_alert_at=alerts[0].starts_at,
    )
    grown = incident.absorb(alerts[4])

    assert alerts[4].service in grown.services
    assert grown.last_alert_at == alerts[4].starts_at
    assert grown.detected_at == incident.detected_at, "the start must not move forward"
    assert incident.services != grown.services, "absorb must not mutate in place"


def test_nearest_hop_skips_unreachable_services() -> None:
    """An incident can implicate a service the graph does not connect to the
    alert's -- a third-party dependency, or one nobody has wired up yet. Those
    candidates are skipped, not treated as distance zero.
    """
    alerts = _storm(5)
    root = alerts[0]
    mixed = OpenIncident(
        id=1,
        severity_rank=root.severity_rank,
        detected_at=root.starts_at,
        primary_service=root.service,
        stable_labels=root.stable_labels,
        # "orphan-service" is absent from the graph entirely, so distance() to
        # it is None and it must be skipped in favour of postgres-primary.
        affected_services=frozenset({"orphan-service", "postgres-primary"}),
        last_alert_at=root.starts_at,
    )
    decision = correlate(alerts[4], [mixed], GRAPH, Cfg())

    assert decision.is_merge
    assert any("postgres-primary" in reason for reason in decision.reasons), (
        "the reachable candidate must be the one named in the explanation"
    )
