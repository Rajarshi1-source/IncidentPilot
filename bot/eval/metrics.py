"""The scorers (W7-08).

Six metrics, three of them deterministic and therefore gateable hard. Every one
is computed by code against human labels; none of them asks a model what it
thinks of another model's work. That split is the whole licence for the gate:
**a gate that can itself hallucinate is not a gate.**

Two implementation choices are load-bearing rather than incidental:

* **The timeline is matched in buckets, not on exact timestamps.** A three-second
  offset is noise and punishing it would make the metric measure clock jitter;
  a minute-wide bucket still catches an event placed in the wrong phase, which
  is the error that matters.
* **Action items are matched fuzzily.** They are paraphrases by nature -- "add a
  replica lag alert" and "alert on replica lag" are the same item -- and exact
  match under-reports so badly that the number stops tracking quality at all.
  ``difflib`` rather than an embedding: deterministic, free, offline, and
  explainable to the person whose item was scored as a miss.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from incidentpilot.pir.context import GroundedContext
from incidentpilot.pir.schema import PIRDraft, walk_claims
from incidentpilot.pir.validator import CitationValidator

# Wide enough to absorb the difference between "when the message was sent" and
# "when the classifier logged it", narrow enough that a remediation placed
# before detection is still a miss.
DEFAULT_BUCKET_S = 60

# Tuned on the corpus. Below this two genuinely different items start matching;
# above it, honest paraphrases stop.
DEFAULT_ACTION_THRESHOLD = 0.6


def similarity(a: str, b: str) -> float:
    """Normalized similarity of two short strings, order-insensitive at the edges."""
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def timeline_f1(
    pred: list[Any], truth: list[dict[str, Any]], bucket_s: int = DEFAULT_BUCKET_S
) -> float:
    """Match on (time bucket, intent).

    Both empty scores 1.0: an incident with nothing to put on a timeline is
    correctly represented by an empty timeline, and scoring it 0.0 would
    penalise the flapping fixtures for behaving exactly as intended.
    """
    predicted = {(int(_offset(e) // bucket_s), _intent(e)) for e in pred}
    labelled = {(int(float(g.get("t", 0)) // bucket_s), str(g.get("intent", ""))) for g in truth}
    if not predicted and not labelled:
        return 1.0
    hits = len(predicted & labelled)
    precision = hits / len(predicted) if predicted else 0.0
    recall = hits / len(labelled) if labelled else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def citation_precision(draft: PIRDraft | None, ctx: GroundedContext) -> float:
    """Cited IDs that exist **and** support the claim, over total citations.

    Both conditions, not just existence. A real ID pasted onto an unrelated
    sentence is the more insidious failure: it survives a set-membership check,
    it renders as a working chip in the UI, and it is wrong.

    No draft scores 1.0 -- a skeleton makes no citations and there is nothing to
    be imprecise about. The *coverage* SLI treats the skeleton as 0.0 for the
    opposite and equally deliberate reason: it has made no cited claims, and
    reporting perfect grounding for it would make the one SLI with a zero error
    budget look healthy on exactly the days the feature was unavailable.
    """
    if draft is None:
        return 1.0
    validator = CitationValidator()
    valid = ctx.valid_reference_set()
    total = supported = 0
    for _, item in walk_claims(draft):
        text = getattr(item, "description", None) or getattr(item, "text", "")
        for citation in item.citations:
            total += 1
            ref = citation.ref.strip()
            if ref in valid and validator.supports(ref, str(text), ctx):
                supported += 1
    return 1.0 if total == 0 else supported / total


def uncited_claims(draft: PIRDraft | None) -> int:
    """Claims carrying no citation at all.

    Should be structurally impossible -- ``Claim.citations`` has ``min_length=1``
    -- and is counted anyway. An invariant enforced in one place is one refactor
    from being enforced nowhere, and this metric is what would notice.
    """
    if draft is None:
        return 0
    return sum(1 for _, item in walk_claims(draft) if not item.citations)


def fabricated_citations(draft: PIRDraft | None, ctx: GroundedContext) -> int:
    """Cited IDs absent from ``valid_reference_set()``.

    The adversarial bucket's target. A transcript containing something that
    *looks* like a reference must not become a citation, and this counts every
    time it did.
    """
    if draft is None:
        return 0
    valid = ctx.valid_reference_set()
    return sum(
        1
        for _, item in walk_claims(draft)
        for citation in item.citations
        if citation.ref.strip() not in valid
    )


def action_item_recall(
    pred: list[Any], truth: list[str], thresh: float = DEFAULT_ACTION_THRESHOLD
) -> float:
    """Labelled action items recovered, fuzzily matched.

    Recall rather than F1 on purpose. An extra action item in a postmortem costs
    someone five minutes; a missing one costs the next outage, so the asymmetry
    belongs in the metric rather than in a comment about the metric.
    """
    if not truth:
        return 1.0
    descriptions = [str(getattr(p, "description", p)) for p in pred]
    hits = sum(
        1
        for label in truth
        if max((similarity(label, d) for d in descriptions), default=0.0) >= thresh
    )
    return hits / len(truth)


def storm_compression(created: int, expected: int) -> float:
    """1.0 when the alert storm produced exactly the incidents it should have.

    Symmetric around the target, because both errors are real: forty channels is
    the D3 failure, and one channel for two genuinely separate outages hides one
    of them -- which is worse.
    """
    return max(0.0, 1 - abs(created - expected) / max(expected, 1))


# --- aggregation --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncidentScore:
    """One replayed incident, scored."""

    incident_id: int
    bucket: str
    timeline_f1: float
    citation_precision: float
    uncited_claims: int
    fabricated_citations: int
    action_item_recall: float
    storm_compression: float
    cost_usd: float
    generation_ms: int
    network_calls: int
    prompt_drift: int
    layer: str
    # A fixture that degraded to the skeleton when the recording says it should
    # not have. Its own gate row because it is the harness's own listed failure
    # mode -- "everything degrades to skeleton in replay", usually a validator
    # threshold that got stricter -- and because every other metric goes
    # *quiet* when it happens: a skeleton has no citations to be imprecise
    # about, so citation_precision reads 1.000 on a corpus that produced no
    # cited claims at all. The first version of this file shipped without it
    # and reported a green gate over eleven skeletons.
    unexpected_skeleton: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Aggregate:
    """Corpus-level numbers, plus the per-bucket view that stops them lying."""

    scores: list[IncidentScore] = field(default_factory=list)

    def add(self, score: IncidentScore) -> None:
        self.scores.append(score)

    def __len__(self) -> int:
        return len(self.scores)

    def mean(self, name: str) -> float:
        values = [float(getattr(s, name)) for s in self.scores]
        return statistics.fmean(values) if values else 0.0

    def total(self, name: str) -> int:
        return sum(int(getattr(s, name)) for s in self.scores)

    def p95_generation_ms(self) -> float:
        """p95 by nearest-rank, not by interpolation.

        Interpolating invents a latency no request experienced. On a 40-point
        corpus that is the difference between a number you can point at in the
        recording and one you cannot.
        """
        values = sorted(s.generation_ms for s in self.scores)
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, round(0.95 * len(values)) - 1))
        return float(values[index])

    def cost_per_pir_usd(self) -> float:
        drafted = [s for s in self.scores if s.layer != "skeleton"]
        if not drafted:
            return 0.0
        return sum(s.cost_usd for s in drafted) / len(drafted)

    def as_dict(self) -> dict[str, float]:
        """The metric map the gate compares against the baseline."""
        return {
            "timeline_f1": round(self.mean("timeline_f1"), 4),
            "citation_precision": round(self.mean("citation_precision"), 4),
            "uncited_claims": float(self.total("uncited_claims")),
            "fabricated_citations": float(self.total("fabricated_citations")),
            "action_item_recall": round(self.mean("action_item_recall"), 4),
            "storm_compression": round(self.mean("storm_compression"), 4),
            "cost_per_pir_usd": round(self.cost_per_pir_usd(), 4),
            "p95_generation_ms": self.p95_generation_ms(),
            "prompt_drift": float(self.total("prompt_drift")),
            "unexpected_skeleton": float(self.total("unexpected_skeleton")),
            "network_calls": float(self.total("network_calls")),
            "replay_errors": float(sum(1 for s in self.scores if not s.ok)),
        }

    def by_bucket(self) -> dict[str, dict[str, float]]:
        """Per-bucket breakdown.

        An aggregate F1 of 0.91 hiding 0.44 on the code-switched bucket is a
        metric that lies to you, and the per-bucket view is what tells you where
        the next week goes.
        """
        buckets: dict[str, Aggregate] = {}
        for score in self.scores:
            buckets.setdefault(score.bucket, Aggregate()).add(score)
        return {
            name: agg.as_dict() | {"n": float(len(agg))} for name, agg in sorted(buckets.items())
        }


def _offset(entry: Any) -> float:
    return float(getattr(entry, "t", 0.0))


def _intent(entry: Any) -> str:
    return str(getattr(entry, "intent", ""))
