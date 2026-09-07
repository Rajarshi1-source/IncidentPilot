"""The scorers and the gate (W7-08, W7-09, W7-10, W7-13).

The metrics are what everything else in this week is in service of, so their
edge cases get tested directly rather than only through the corpus. A scorer
that returns 1.0 for "nothing to score" and a scorer that returns 1.0 for
"scored perfectly" look identical in an aggregate, and telling them apart is the
difference between a gate and a green light.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eval.gate import BUDGET, HARD, SOFT, evaluate, load_baseline, promote
from eval.judge.agreement import cohens_kappa, measure, measure_corpus
from eval.metrics import (
    Aggregate,
    IncidentScore,
    action_item_recall,
    citation_precision,
    fabricated_citations,
    similarity,
    storm_compression,
    timeline_f1,
    uncited_claims,
)
from eval.recorder import corpus_paths
from eval.replayer import PredictedTimelineEntry
from eval.report import render_markdown, render_table
from incidentpilot.pir.context import GroundedContext, MessageRef
from incidentpilot.pir.schema import ActionItem, Citation, Claim, PIRDraft

CORPUS = Path(__file__).resolve().parents[2] / "eval" / "corpus"


def _ctx() -> GroundedContext:
    ctx = GroundedContext(
        incident_id=1,
        public_key="INC-0001",
        title="payments degraded",
        severity="sev1",
        detected_at=datetime(2026, 9, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 9, 1, 0, 30, tzinfo=UTC),
        service="payments-api",
    )
    ctx.messages = [
        MessageRef(ts="1.000100", user_id="U1", text="restarting payments-api to clear the pool"),
        MessageRef(ts="2.000100", user_id="U2", text="checkout is completely unrelated to sharks"),
    ]
    return ctx


def _draft(ref: str, text: str) -> PIRDraft:
    return PIRDraft(
        summary=[Claim(text=text, citations=[Citation(kind="message", ref=ref)])],
        action_items=[
            ActionItem(
                # Reuses the words of the message it cites, which is what a
                # grounded item looks like -- and what `_supports` checks.
                description="alert when the payments-api pool is saturated instead of restarting",
                priority="P1",
                category="monitoring",
                citations=[Citation(kind="message", ref="msg:1.000100")],
            )
        ],
    )


# --- W7-08: metric edge cases -------------------------------------------------


def test_metric_edge_cases_timeline() -> None:
    truth = [{"t": 94, "intent": "remediation_start"}, {"t": 300, "intent": "recovery_signal"}]

    exact = [
        PredictedTimelineEntry(t=94, intent="remediation_start", description=""),
        PredictedTimelineEntry(t=300, intent="recovery_signal", description=""),
    ]
    assert timeline_f1(exact, truth) == pytest.approx(1.0)

    # A three-second offset is noise and must not be punished; bucketing is
    # what stops the metric measuring clock jitter instead of correctness.
    jittered = [
        PredictedTimelineEntry(t=97, intent="remediation_start", description=""),
        PredictedTimelineEntry(t=303, intent="recovery_signal", description=""),
    ]
    assert timeline_f1(jittered, truth) == pytest.approx(1.0)

    # An event placed in the wrong phase is a real error and must be caught.
    misplaced = [
        PredictedTimelineEntry(t=400, intent="remediation_start", description=""),
        PredictedTimelineEntry(t=300, intent="recovery_signal", description=""),
    ]
    assert timeline_f1(misplaced, truth) == pytest.approx(0.5)

    # Both empty is a correct answer, not a zero. The flapping fixtures depend
    # on this: an incident with nothing to put on a timeline is represented
    # correctly by an empty timeline.
    assert timeline_f1([], []) == pytest.approx(1.0)
    assert timeline_f1([], truth) == pytest.approx(0.0)
    assert timeline_f1(exact, []) == pytest.approx(0.0)


def test_citation_precision_requires_support_not_just_existence() -> None:
    """A real ID on an unrelated sentence is the insidious failure.

    It survives a set-membership check, it renders as a working chip in the UI,
    and it is wrong. Precision that only checked existence would score it 1.000.
    """
    ctx = _ctx()
    supported = _draft("msg:1.000100", "restarting payments-api to clear the pool")
    assert citation_precision(supported, ctx) == pytest.approx(1.0)

    unrelated = _draft("msg:2.000100", "the database primary lost its lease during failover")
    assert citation_precision(unrelated, ctx) < 1.0


def test_fabricated_and_uncited_are_counted_separately() -> None:
    ctx = _ctx()
    fabricated = _draft("msg:9999999999.999999", "restarting payments-api to clear the pool")
    assert fabricated_citations(fabricated, ctx) == 1
    assert uncited_claims(fabricated) == 0

    # A skeleton has no draft. Nothing to fabricate, nothing left uncited --
    # which is why `unexpected_skeleton` exists as its own gate row: without it
    # a total collapse reads as a perfect score.
    assert fabricated_citations(None, ctx) == 0
    assert uncited_claims(None) == 0
    assert citation_precision(None, ctx) == pytest.approx(1.0)


def test_action_item_recall_is_fuzzy_because_items_are_paraphrases() -> None:
    items = [
        ActionItem(
            description="add an alert on replica lag",
            priority="P1",
            category="monitoring",
            citations=[Citation(kind="message", ref="msg:1.000100")],
        )
    ]
    assert action_item_recall(items, ["alert on replica lag"]) == pytest.approx(1.0)
    assert action_item_recall(items, ["rewrite the billing service in rust"]) == pytest.approx(0.0)
    # No labelled items is a vacuous 1.0, not a 0.0: there was nothing to recall.
    assert action_item_recall([], []) == pytest.approx(1.0)
    assert action_item_recall([], ["something"]) == pytest.approx(0.0)


def test_storm_compression_is_symmetric() -> None:
    assert storm_compression(1, 1) == pytest.approx(1.0)
    # Forty channels for one outage: the D3 failure.
    assert storm_compression(40, 1) == pytest.approx(0.0)
    # One channel for two separate outages hides one of them, which is worse --
    # so it is penalised too rather than rewarded as "extra compression".
    assert storm_compression(1, 2) == pytest.approx(0.5)


def test_similarity_is_symmetric_and_bounded() -> None:
    assert similarity("a", "a") == pytest.approx(1.0)
    assert similarity("promote the replica", "PROMOTE THE REPLICA") == pytest.approx(1.0)
    assert 0.0 <= similarity("apples", "orangutans") < 0.5


def test_p95_uses_nearest_rank_not_interpolation() -> None:
    """Interpolating invents a latency no request experienced."""
    aggregate = Aggregate()
    for index, ms in enumerate([100, 200, 300, 400, 50_000]):
        aggregate.add(
            IncidentScore(
                incident_id=index,
                bucket="real",
                timeline_f1=1.0,
                citation_precision=1.0,
                uncited_claims=0,
                fabricated_citations=0,
                action_item_recall=1.0,
                storm_compression=1.0,
                cost_usd=0.01,
                generation_ms=ms,
                network_calls=0,
                prompt_drift=0,
                layer="llm_primary",
            )
        )
    assert aggregate.p95_generation_ms() in {400.0, 50_000.0}
    assert aggregate.p95_generation_ms() == 50_000.0


def test_cost_per_pir_excludes_the_skeleton() -> None:
    """A skeleton costs nothing and would drag the average towards zero.

    Averaging it in would let a corpus that collapsed entirely report an
    excellent cost per PIR -- the cheapest possible product is one that does
    not work.
    """
    aggregate = Aggregate()
    common: dict[str, Any] = {
        "bucket": "real",
        "timeline_f1": 1.0,
        "citation_precision": 1.0,
        "uncited_claims": 0,
        "fabricated_citations": 0,
        "action_item_recall": 1.0,
        "storm_compression": 1.0,
        "generation_ms": 100,
        "network_calls": 0,
        "prompt_drift": 0,
    }
    aggregate.add(IncidentScore(incident_id=1, cost_usd=0.30, layer="llm_primary", **common))
    aggregate.add(IncidentScore(incident_id=2, cost_usd=0.0, layer="skeleton", **common))
    assert aggregate.cost_per_pir_usd() == pytest.approx(0.30)


# --- W7-09: the gate ----------------------------------------------------------


def _metrics(**overrides: float) -> dict[str, float]:
    base = {
        "timeline_f1": 0.90,
        "citation_precision": 1.0,
        "uncited_claims": 0.0,
        "fabricated_citations": 0.0,
        "action_item_recall": 0.85,
        "storm_compression": 1.0,
        "cost_per_pir_usd": 0.30,
        "p95_generation_ms": 12_000.0,
        "prompt_drift": 0.0,
        "network_calls": 0.0,
        "replay_errors": 0.0,
        "unexpected_skeleton": 0.0,
    }
    base.update(overrides)
    return base


BASELINE = {"timeline_f1": 0.90, "action_item_recall": 0.85, "citation_precision": 1.0}


def test_gate_passes_a_clean_run() -> None:
    assert evaluate(_metrics(), BASELINE).passed


def test_gate_blocks_regression() -> None:
    """A soft metric four points below baseline fails; two points does not."""
    tolerable = evaluate(_metrics(timeline_f1=0.88), BASELINE)
    assert tolerable.passed, "a two-point move is noise and must not produce flaky CI"

    regressed = evaluate(_metrics(timeline_f1=0.86), BASELINE)
    assert not regressed.passed
    assert any("timeline_f1" in f for f in regressed.failures)


def test_the_hard_gates_have_no_tolerance() -> None:
    """One fabricated citation in forty incidents fails the build.

    The grounding SLI has a zero error budget because it is enforced by a
    deterministic validator, not because 100% is an aspiration -- so 0.999 is
    not "nearly right", it is a broken invariant.
    """
    assert not evaluate(_metrics(citation_precision=0.999), BASELINE).passed
    assert not evaluate(_metrics(fabricated_citations=1.0), BASELINE).passed
    assert not evaluate(_metrics(uncited_claims=1.0), BASELINE).passed


def test_prompt_drift_and_silent_skeletons_are_hard_failures() -> None:
    """The two rows this project added to the reference's list.

    Both catch a corpus that has stopped measuring rather than a system that
    has started failing, which is the harder failure to see and the one that
    makes every other number meaningless.
    """
    assert "prompt_drift" in HARD
    assert "unexpected_skeleton" in HARD
    assert not evaluate(_metrics(prompt_drift=1.0), BASELINE).passed
    assert not evaluate(_metrics(unexpected_skeleton=1.0), BASELINE).passed
    assert not evaluate(_metrics(network_calls=1.0), BASELINE).passed


def test_budget_ceilings_are_absolute() -> None:
    assert not evaluate(_metrics(cost_per_pir_usd=0.51), BASELINE).passed
    assert not evaluate(_metrics(p95_generation_ms=30_001.0), BASELINE).passed
    assert BUDGET["cost_per_pir_usd"] == 0.50
    assert SOFT["timeline_f1"] == 0.03


def test_the_committed_baseline_is_the_one_the_corpus_measured() -> None:
    """A baseline typed by hand drifts from the corpus within two PRs."""
    baseline = load_baseline(CORPUS / "manifest.json")
    assert baseline["citation_precision"] == 1.0
    assert 0.0 < baseline["timeline_f1"] <= 1.0
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["corpus"]["n"] == len(corpus_paths(CORPUS))


def test_promote_is_explicit_and_writes_only_the_baseline(tmp_path: Path) -> None:
    """CI must never promote. A self-promoting gate can never fail.

    Each run would be 0.001 worse than the last, each run would pass, and the
    baseline would ratchet down to whatever the corpus happened to score.
    """
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"corpus": {"n": 40}}), encoding="utf-8")
    promote(manifest, _metrics())
    body = json.loads(manifest.read_text(encoding="utf-8"))
    assert body["corpus"]["n"] == 40, "promotion must not clobber the corpus map"
    assert body["baseline"]["timeline_f1"] == 0.90
    assert "network_calls" not in body["baseline"], "only quality metrics are baselined"


# --- W7-10 / W7-16: the report ------------------------------------------------


def test_report_names_the_failing_metric_and_breaks_down_by_bucket() -> None:
    aggregate = Aggregate()
    for index, bucket in enumerate(("real", "code_switched")):
        aggregate.add(
            IncidentScore(
                incident_id=index,
                bucket=bucket,
                timeline_f1=1.0 if bucket == "real" else 0.44,
                citation_precision=1.0,
                uncited_claims=0,
                fabricated_citations=0,
                action_item_recall=0.8,
                storm_compression=1.0,
                cost_usd=0.02,
                generation_ms=1000,
                network_calls=0,
                prompt_drift=0,
                layer="llm_primary",
            )
        )
    metrics = _metrics(prompt_drift=1.0)
    gate = evaluate(metrics, BASELINE)
    markdown = render_markdown(
        aggregate,
        metrics,
        BASELINE,
        gate,
        wall_s=9.2,
        network_calls=0,
        judge_line="judge_agreement: 0.87 (n=10, Cohen's kappa 0.68)",
        notes=["synthesize prompt no longer matches its registry entry"],
    )

    assert "Eval gate — FAIL" in markdown
    assert "**FAIL**" in markdown
    assert "HARD prompt_drift" in markdown
    assert "no longer matches its registry entry" in markdown
    # The per-bucket view is what stops an aggregate hiding a weak bucket.
    assert "code_switched" in markdown
    assert "0.440" in markdown
    assert "judge_agreement" in markdown
    # The honest caveat travels with the numbers, not in a README nobody opens.
    assert "small corpus" in markdown

    text = render_table(aggregate, metrics, BASELINE, gate, wall_s=9.2, network_calls=0)
    assert "GATE: FAIL" in text
    assert "network calls 0" in text


# --- W7-13: judge agreement ---------------------------------------------------


def test_cohens_kappa_corrects_for_chance() -> None:
    """The number that stops raw agreement flattering a skewed distribution.

    A judge answering "2" to everything on a corpus that is 90% "2" agrees 90%
    of the time and has learned nothing. Kappa says so.
    """
    human = [2, 2, 2, 2, 2, 2, 2, 2, 2, 1]
    lazy = [2] * 10
    assert measure(list(zip(human, lazy, strict=True))).rate == pytest.approx(0.9)
    assert cohens_kappa(human, lazy) == pytest.approx(0.0)

    perfect = measure([(2, 2), (1, 1), (0, 0)])
    assert perfect.rate == pytest.approx(1.0)
    assert perfect.kappa == pytest.approx(1.0)


def test_judge_agreement_is_measured_on_the_labelled_subset() -> None:
    body = json.loads((CORPUS / "judge_labels.json").read_text(encoding="utf-8"))
    agreement = measure_corpus(body["labels"])

    # n is incidents, not incident-dimensions. Counting each of three
    # dimensions separately would report n=30 and overstate how much human
    # effort backs the number.
    assert agreement["all"].n == 10
    assert 0.0 < agreement["all"].rate <= 1.0
    assert "root_cause" in agreement
    assert "judge_agreement" in str(agreement["all"])


def test_the_judge_is_offline_only_and_ungated() -> None:
    """A judge on the production path would put a second model in the loop.

    The validator is deterministic precisely so nothing has to. And nothing the
    judge produces appears in HARD, SOFT or BUDGET -- it is reported, and the
    report says it is reported.
    """
    from incidentpilot.config.models_config import ModelConfigError, load_models_config

    config = load_models_config()
    with pytest.raises(ModelConfigError, match="offline_only"):
        config.role("judge")
    assert config.role("judge", offline=True).offline_only

    gated = set(HARD) | set(SOFT) | set(BUDGET)
    assert not any("judge" in metric for metric in gated)
