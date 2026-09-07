"""The markdown table for the sticky PR comment (W7-10, W7-16).

The artifact this whole week exists to produce. A red check caused by one word
in a prompt, with a table naming the metric that moved, is the single most
senior-looking thing in the repository -- and it only works if the comment is
readable by someone who has never seen the harness.

Three rules the format follows:

* **The failing row is named, not summarised.** "GATE: FAIL (5/9)" tells a
  reviewer to go and read the logs. "HARD prompt_drift: 1 (required == 0)" tells
  them what to do.
* **Per-bucket, not just aggregate.** An aggregate F1 of 0.91 hiding 0.44 on the
  code-switched bucket is a metric that lies, and the bucket table is where the
  next week's work comes from.
* **The corpus size is stated with its limitation.** Forty incidents is a small
  corpus. Saying so is a feature in a review, not a weakness -- the alternative
  is implying statistical power the number does not have.
"""

from __future__ import annotations

from eval.gate import GateResult
from eval.metrics import Aggregate

# The rows that go in the headline table, in the order a reader meets them.
HEADLINE = (
    "timeline_f1",
    "citation_precision",
    "uncited_claims",
    "fabricated_citations",
    "action_item_recall",
    "storm_compression",
    "prompt_drift",
    "unexpected_skeleton",
    "cost_per_pir_usd",
    "p95_generation_ms",
)

BUCKET_COLUMNS = ("timeline_f1", "citation_precision", "action_item_recall", "storm_compression")


def render_markdown(
    aggregate: Aggregate,
    results: dict[str, float],
    baseline: dict[str, float],
    gate: GateResult,
    *,
    wall_s: float,
    network_calls: int,
    judge_line: str | None = None,
    counterfactual: str | None = None,
    notes: list[str] | None = None,
) -> str:
    lines: list[str] = []
    verdict = "PASS" if gate.passed else "FAIL"
    lines.append(f"## Eval gate — {verdict}")
    lines.append("")
    if counterfactual:
        lines.append(f"> Counterfactual run: `{counterfactual}`")
        lines.append("")

    lines.append("| metric | value | baseline | delta | gate |")
    lines.append("|---|---|---|---|---|")
    by_metric = {c.metric: c for c in gate.checks}
    for metric in HEADLINE:
        if metric not in results:
            continue
        value = results[metric]
        check = by_metric.get(metric)
        reference = baseline.get(metric)
        if check is not None and check.tier == "BUDGET":
            ref_text = f"budget {_num(check.reference)}"
            delta = ""
        elif reference is None:
            ref_text = "—"
            delta = ""
        else:
            ref_text = _num(reference)
            delta = _delta(value - reference)
        status = "—" if check is None else ("PASS" if check.passed else "**FAIL**")
        lines.append(f"| {metric} | {_num(value)} | {ref_text} | {delta} | {status} |")

    lines.append("")
    lines.append(
        f"**GATE: {gate.summary}** · corpus {len(aggregate)} · wall {wall_s:.1f}s · "
        f"network calls {network_calls}"
    )

    if gate.failures:
        lines.append("")
        lines.append("### Why it failed")
        lines.extend(f"- {failure}" for failure in gate.failures)
        # The count tells a reviewer a gate tripped; the detail tells them what
        # to do about it. A sticky comment that says "prompt_drift: 40" and
        # nothing else sends them to the logs, which is the failure mode this
        # whole artifact exists to avoid.
        for note in notes or []:
            lines.append(f"  - {note}")

    lines.append("")
    lines.append("### Per bucket")
    lines.append("| bucket | n | " + " | ".join(BUCKET_COLUMNS) + " |")
    lines.append("|---|---|" + "---|" * len(BUCKET_COLUMNS))
    for bucket, values in aggregate.by_bucket().items():
        cells = " | ".join(_num(values.get(column, 0.0)) for column in BUCKET_COLUMNS)
        lines.append(f"| {bucket} | {int(values.get('n', 0))} | {cells} |")

    if judge_line:
        lines.append("")
        lines.append(f"### Judge agreement\n\n`{judge_line}`")
        lines.append("")
        lines.append(
            "Reported, never gated. The hard gates are deterministic; the judge is "
            "measured against human labels so that gating *softly* on it is a stated "
            "trade rather than an unexamined one."
        )

    lines.append("")
    lines.append(
        f"<sub>{len(aggregate)} incidents is a small corpus and the numbers should be read "
        "that way: it is wide enough to catch a structural regression and far too narrow "
        "for a confidence interval. Replay scores recorded model responses, so a prompt "
        "edit shows up as `prompt_drift`, not as a quality delta — see "
        "docs/ADR/0006.</sub>"
    )
    return "\n".join(lines) + "\n"


def render_table(
    aggregate: Aggregate,
    results: dict[str, float],
    baseline: dict[str, float],
    gate: GateResult,
    *,
    wall_s: float,
    network_calls: int,
) -> str:
    """The terminal form. Same numbers, aligned for a shell rather than a browser."""
    width = max(len(m) for m in HEADLINE)
    lines = [f"{'metric'.ljust(width)}   value      baseline   gate"]
    lines.append("-" * (width + 30))
    by_metric = {c.metric: c for c in gate.checks}
    for metric in HEADLINE:
        if metric not in results:
            continue
        check = by_metric.get(metric)
        reference = (
            f"budget {_num(check.reference)}"
            if check is not None and check.tier == "BUDGET"
            else _num(baseline.get(metric, 0.0))
        )
        status = "—" if check is None else ("PASS" if check.passed else "FAIL")
        lines.append(
            f"{metric.ljust(width)}   {_num(results[metric]).ljust(10)} "
            f"{reference.ljust(10)} {status}"
        )

    lines.append("")
    lines.append("per bucket:")
    for bucket, values in aggregate.by_bucket().items():
        cells = "  ".join(f"{c}={_num(values.get(c, 0.0))}" for c in BUCKET_COLUMNS)
        lines.append(f"  {bucket.ljust(24)} n={int(values.get('n', 0)):<3} {cells}")

    lines.append("")
    lines.append(
        f"GATE: {gate.summary} · corpus {len(aggregate)} · wall {wall_s:.1f}s · "
        f"network calls {network_calls}"
    )
    for failure in gate.failures:
        lines.append(f"  FAIL  {failure}")
    return "\n".join(lines)


def _num(value: float | None) -> str:
    if value is None:
        return "—"
    if float(value).is_integer() and abs(value) < 1e6 and abs(value) >= 100:
        return str(int(value))
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}"


def _delta(value: float) -> str:
    if abs(value) < 5e-4:
        return "0.000"
    return f"{value:+.3f}"
