"""The merge gate (W7-09).

Three tiers, and the tiering is the design:

**HARD — must equal.** Computed by code, no model in the loop, no tolerance.
Citation precision, uncited claims, fabricated citations. These are invariants
rather than targets: the grounding SLI has a zero error budget because it is
enforced by a deterministic validator, so a "mostly grounded" PIR is not a
slightly worse PIR, it is a broken one.

**SOFT — a three-point tolerance against a committed baseline.** Model outputs
vary slightly run to run even at temperature 0, and a zero-tolerance soft gate
produces flaky CI that people learn to re-run until green -- which is worse than
no gate, because the re-run habit generalises to the hard checks too. Three
points is wide enough to absorb noise and narrow enough to catch real regression.

**BUDGET — ceilings, not comparisons.** Cost and latency are constraints the
product has to live inside, so they are absolute rather than relative to
whatever last week happened to cost.

The baseline lives in ``corpus/manifest.json`` next to the corpus it was measured
on, and it moves only when a human promotes a version. A baseline typed into a
workflow file drifts from the corpus that produced it within two PRs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# HARD: (comparison, required value). Deterministic metrics only -- deliberately.
HARD: dict[str, tuple[str, float]] = {
    "citation_precision": ("==", 1.0),
    "uncited_claims": ("==", 0.0),
    "fabricated_citations": ("==", 0.0),
    # Not in the reference's original list, and it belongs there. A recording
    # was produced by a specific prompt; if the prompt has changed, the corpus
    # no longer measures the system under test. Replay cannot tell you whether
    # a new prompt is better -- it can only tell you, truthfully, that it can no
    # longer tell you. This is the check that makes a one-word edit turn CI red
    # (B-11, G7), and the alternative -- scoring stale responses as if the edit
    # never happened -- is a gate that reports green on a change it did not see.
    "prompt_drift": ("==", 0.0),
    # INV-10 as a gate row rather than a comment. A replay that opened a socket
    # measured something other than the corpus.
    "network_calls": ("==", 0.0),
    # The quiet one. When a draft degrades to the skeleton every quality metric
    # stops measuring rather than starting to fail -- there are no citations to
    # be imprecise about and no claims to leave uncited -- so a corpus that
    # collapsed entirely reports citation_precision 1.000 and passes. Counting
    # the collapse is what makes the other rows mean anything.
    "unexpected_skeleton": ("==", 0.0),
    # A fixture that raised is not a fixture that passed.
    "replay_errors": ("==", 0.0),
}

# SOFT: metric -> tolerance below the committed baseline.
SOFT: dict[str, float] = {
    "timeline_f1": 0.03,
    "action_item_recall": 0.03,
}

# SOFT floors that are absolute rather than baseline-relative.
FLOORS: dict[str, float] = {
    "storm_compression": 0.95,
}

BUDGET: dict[str, float] = {
    "cost_per_pir_usd": 0.50,
    "p95_generation_ms": 30_000.0,
}

# The metrics a promotion records. Deliberately not every metric: baselining
# `network_calls` or `prompt_drift` would turn a hard invariant into a moving
# target, which is the one thing a baseline must never do.
BASELINED: tuple[str, ...] = (
    "timeline_f1",
    "citation_precision",
    "action_item_recall",
    "storm_compression",
    "cost_per_pir_usd",
)

DEFAULT_BASELINE: dict[str, float] = {
    "timeline_f1": 0.0,
    "citation_precision": 1.0,
    "action_item_recall": 0.0,
    "storm_compression": 0.95,
    "cost_per_pir_usd": 0.0,
}


@dataclass(frozen=True, slots=True)
class Check:
    """One gate row, with everything the report needs to render it."""

    metric: str
    tier: str
    value: float
    reference: float | None
    passed: bool
    detail: str = ""


@dataclass
class GateResult:
    checks: list[Check] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        good = sum(1 for c in self.checks if c.passed)
        return f"{'PASS' if self.passed else 'FAIL'} ({good}/{len(self.checks)})"


def load_baseline(manifest_path: Path | str) -> dict[str, float]:
    """Read the committed baseline from ``manifest.json``.

    A missing manifest is not an error -- the first run on a new corpus has no
    baseline yet -- but a missing *baseline key* inside a manifest that exists
    is, because that is how a promotion half-lands.
    """
    path = Path(manifest_path)
    if not path.exists():
        return dict(DEFAULT_BASELINE)
    body = json.loads(path.read_text(encoding="utf-8"))
    baseline = body.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError(f"{path} has no 'baseline' object -- a promotion did not finish")
    return {str(k): float(v) for k, v in baseline.items() if isinstance(v, int | float)}


def _compare(value: float, op: str, want: float) -> bool:
    if op == "==":
        # Exact for the integer counters, epsilon for the ratio. A float
        # comparison written as `== 1.0` on a computed mean is how a gate that
        # is doing its job reports a failure nobody can reproduce.
        return abs(value - want) < 1e-9
    if op == "<=":
        return value <= want
    if op == ">=":
        return value >= want
    raise ValueError(f"unknown comparison {op!r}")


def evaluate(results: dict[str, float], baseline: dict[str, float]) -> GateResult:
    """Score the run against the committed baseline."""
    out = GateResult()

    for metric, (op, want) in HARD.items():
        value = float(results.get(metric, 0.0))
        passed = _compare(value, op, want)
        out.checks.append(
            Check(metric=metric, tier="HARD", value=value, reference=want, passed=passed)
        )
        if not passed:
            out.failures.append(f"HARD {metric}: {_fmt(value)} (required {op} {_fmt(want)})")

    for metric, tolerance in SOFT.items():
        value = float(results.get(metric, 0.0))
        reference = float(baseline.get(metric, 0.0))
        passed = value >= reference - tolerance
        out.checks.append(
            Check(
                metric=metric,
                tier="SOFT",
                value=value,
                reference=reference,
                passed=passed,
                detail=f"tol {tolerance:.2f}",
            )
        )
        if not passed:
            out.failures.append(
                f"REGRESSION {metric}: {value:.3f} vs baseline {reference:.3f} "
                f"(tol {tolerance:.2f})"
            )

    for metric, floor in FLOORS.items():
        value = float(results.get(metric, 0.0))
        passed = value >= floor
        out.checks.append(
            Check(metric=metric, tier="SOFT", value=value, reference=floor, passed=passed)
        )
        if not passed:
            out.failures.append(f"FLOOR {metric}: {value:.3f} < {floor:.3f}")

    for metric, ceiling in BUDGET.items():
        value = float(results.get(metric, 0.0))
        passed = value <= ceiling
        out.checks.append(
            Check(
                metric=metric,
                tier="BUDGET",
                value=value,
                reference=ceiling,
                passed=passed,
                detail="ceiling",
            )
        )
        if not passed:
            out.failures.append(f"BUDGET {metric}: {_fmt(value)} > {_fmt(ceiling)}")

    return out


def promote(manifest_path: Path | str, results: dict[str, float], **extra: Any) -> None:
    """Move the baseline. Only ever called by a human, never by CI.

    A gate that promotes its own baseline on green ratchets to whatever it last
    measured and can never detect a slow regression -- each run is 0.001 worse
    than the last and each run passes.
    """
    path = Path(manifest_path)
    body = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    body["baseline"] = {key: results[key] for key in BASELINED if key in results} | {
        str(k): v for k, v in extra.items()
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"
