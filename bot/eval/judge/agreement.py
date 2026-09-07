"""Judge-versus-human agreement on the labelled subset (W7-13).

The step most people skip. Using a model to score another model's output is
ordinary; **measuring whether that model agrees with a human, and publishing the
number, is not** -- and it is the only thing that licenses gating softly on the
judge while gating hard on the deterministic metrics.

Both numbers are reported for a reason. Raw agreement flatters a skewed label
distribution: if nine of ten incidents are labelled 2, a judge that answers 2
every time agrees 90% of the time and has learned nothing. Cohen's kappa
subtracts the agreement chance alone would produce, and the difference between
the two numbers is the honest measure of whether the judge is doing work.

The labels live in ``corpus/judge_labels.json`` -- next to the corpus, not in a
spreadsheet, for the same reason ground truth lives inside the recordings.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DIMENSIONS = ("coherence", "root_cause", "action_items")
LABELS_FILE = "judge_labels.json"


@dataclass(frozen=True, slots=True)
class Agreement:
    n: int
    rate: float
    kappa: float
    dimension: str = "all"

    def __str__(self) -> str:
        return f"judge_agreement: {self.rate:.2f} (n={self.n}, Cohen's kappa {self.kappa:.2f})"


def cohens_kappa(human: list[int], judge: list[int]) -> float:
    """Chance-corrected agreement.

    Returns 1.0 when both raters are perfectly constant and identical -- the
    formula divides by zero there, and "two raters who always agree" is
    agreement rather than an undefined quantity. It is also, notably, the case
    where the raw rate is most misleading, which is why the caller prints both.
    """
    if not human or len(human) != len(judge):
        return 0.0
    n = len(human)
    observed = sum(1 for a, b in zip(human, judge, strict=True) if a == b) / n

    human_counts = Counter(human)
    judge_counts = Counter(judge)
    expected = sum(
        (human_counts[label] / n) * (judge_counts[label] / n)
        for label in set(human_counts) | set(judge_counts)
    )
    if abs(1 - expected) < 1e-12:
        return 1.0 if abs(observed - 1.0) < 1e-12 else 0.0
    return (observed - expected) / (1 - expected)


def measure(pairs: list[tuple[int, int]]) -> Agreement:
    """Agreement over ``(human, judge)`` score pairs."""
    if not pairs:
        return Agreement(n=0, rate=0.0, kappa=0.0)
    human = [h for h, _ in pairs]
    judge = [j for _, j in pairs]
    rate = sum(1 for h, j in pairs if h == j) / len(pairs)
    return Agreement(n=len(pairs), rate=rate, kappa=cohens_kappa(human, judge))


def measure_corpus(labels: dict[str, dict[str, dict[str, int]]]) -> dict[str, Agreement]:
    """Per-dimension and overall agreement from the labels file.

    Shape: ``{incident_id: {"human": {dim: score}, "judge": {dim: score}}}``.
    """
    out: dict[str, Agreement] = {}
    everything: list[tuple[int, int]] = []
    for dimension in DIMENSIONS:
        pairs = [
            (int(entry["human"][dimension]), int(entry["judge"][dimension]))
            for entry in labels.values()
            if dimension in entry.get("human", {}) and dimension in entry.get("judge", {})
        ]
        if pairs:
            out[dimension] = measure(pairs)
            everything.extend(pairs)
    # n is incidents, not incident-dimensions: "n=10" in a report means ten
    # incidents were labelled by a person, and inflating it to 30 by counting
    # each dimension separately would overstate how much human effort backs the
    # number.
    overall = measure(everything)
    out["all"] = Agreement(n=len(labels), rate=overall.rate, kappa=overall.kappa, dimension="all")
    return out


def load_agreement(corpus: Path | str) -> str | None:
    """The one-line summary for the PR comment, or None when unlabelled."""
    path = Path(corpus) / LABELS_FILE
    if not path.exists():
        return None
    body = json.loads(path.read_text(encoding="utf-8"))
    labels = body.get("labels") or {}
    if not labels:
        return None
    return str(measure_corpus(labels)["all"])
