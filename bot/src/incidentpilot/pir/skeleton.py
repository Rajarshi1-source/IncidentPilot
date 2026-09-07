"""Layer 3: the honest floor of the product (W6-17).

**The skeleton is not a consolation prize.** It is generated entirely from data
already in hand -- the timeline, the computed impact, the participants, the
runbook that was pinned -- and engineers finish a skeleton far more often than
they start from a blank page. That is a real feature, and describing it as a
fallback undersells it.

It needs no model, no key, no network. Which is why blocking the provider at the
network level still produces a posted document within 90 seconds (G6): there is
nothing left to block.

It posts with a plain banner. Not a euphemism, not a smaller font: *"AI drafting
unavailable; this is the structured skeleton, please complete the narrative."*
A degraded document that does not say it is degraded is worse than no document,
because someone will read it as the finished thing.
"""

from __future__ import annotations

from datetime import timedelta

from incidentpilot.impact.promql import ImpactReport
from incidentpilot.pir.context import GroundedContext

BANNER = (
    "> :construction: **AI drafting unavailable** — this is the structured "
    "skeleton, generated from the recorded timeline and computed metrics. "
    "Please complete the narrative sections marked _TODO_."
)

# The prompts a human fills in. Phrased as questions rather than headings,
# because a heading over an empty section invites deletion and a question
# invites an answer.
TODO_PROMPTS: tuple[tuple[str, str], ...] = (
    ("Summary", "What happened, in two sentences someone outside the team would understand?"),
    ("Root cause", 'What actually caused it? _"We do not know yet" is a valid answer._'),
    ("Contributing factors", "What made it worse, or made it take longer to find?"),
    ("What went well", "What worked? Detection, tooling, a runbook step, a person."),
    ("What went wrong", "What slowed you down that could be fixed?"),
    ("Action items", "One line each, with an owner who was actually in this incident."),
)


def render_skeleton(ctx: GroundedContext, impact: ImpactReport | None = None) -> str:
    """Deterministic markdown. Same incident in, same document out.

    Determinism matters beyond tidiness: the week 7 replay compares skeletons
    byte-for-byte, and a document that varies run to run cannot be part of a
    regression corpus.
    """
    report = impact if impact is not None else ctx.impact
    lines: list[str] = [
        BANNER,
        "",
        f"# {ctx.public_key} — {ctx.title}",
        "",
        f"**Severity** {ctx.severity.upper()}  ·  "
        f"**Detected** {ctx.detected_at.isoformat()}  ·  "
        f"**Duration** {_duration(ctx)}",
        "",
    ]

    lines += _impact_section(report)
    lines += _timeline_section(ctx)
    lines += _evidence_section(ctx)

    for heading, prompt in TODO_PROMPTS:
        lines += [f"## {heading}", "", f"_TODO — {prompt}_", ""]

    lines += [
        "---",
        "",
        f"_Generated from {len(ctx.messages)} stored messages, "
        f"{len(ctx.timeline)} timeline events and {len(ctx.alerts)} alerts. "
        "No model was involved in this document._",
    ]
    return "\n".join(lines)


def _duration(ctx: GroundedContext) -> str:
    if ctx.resolved_at is None:
        return "ongoing"
    total = int((ctx.resolved_at - ctx.detected_at) / timedelta(minutes=1))
    hours, minutes = divmod(max(total, 0), 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _impact_section(report: ImpactReport | None) -> list[str]:
    """Computed numbers, or an honest gap. Never an estimate (B-10, FMEA #15)."""
    lines = ["## Impact", ""]
    if report is None or not report.available:
        reason = report.reason if report is not None else "no metrics adapter configured"
        lines += [
            "_Metrics unavailable for this window — **no impact figures are estimated.**_",
            "",
            f"_Reason: {reason}_",
            "",
        ]
        return lines

    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for key, value in sorted(report.values.items()):
        lines.append(f"| `{key}` | {'unknown' if value is None else f'{value:g}'} |")
    lines += ["", "_Computed from PromQL over the incident window, not generated._", ""]
    return lines


def _timeline_section(ctx: GroundedContext) -> list[str]:
    lines = ["## Timeline", ""]
    if not ctx.timeline:
        lines += ["_No classified events were recorded for this incident._", ""]
        return lines

    for entry in ctx.timeline:
        who = f" <@{entry.author_user_id}>" if entry.author_user_id else ""
        lines.append(
            f"- `{entry.at.strftime('%H:%M:%S')}` **{entry.intent}**{who} — "
            f"{entry.description}  `tl:{entry.id}`"
        )
    lines.append("")
    return lines


def _evidence_section(ctx: GroundedContext) -> list[str]:
    """What is available to cite.

    Listed even though nothing here cites it yet, because the human completing
    the narrative needs the same reference ids the model would have used -- a
    skeleton whose author cannot cite anything produces an uncited PIR, which is
    the thing this whole feature exists to prevent.
    """
    lines = ["## Evidence available", ""]
    root = ctx.root_signal_alert
    if root is not None:
        lines.append(
            f"- **Root signal** `{root.alertname}` on `{root.service or 'unknown'}` "
            f"`alert:{root.fingerprint}`"
        )
    if ctx.runbook_name:
        lines.append(f"- **Runbook** {ctx.runbook_name} ({len(ctx.runbook_steps)} steps recorded)")
    for deploy in ctx.deploys[:5]:
        lines.append(
            f"- **Deploy** `{deploy.sha}` to `{deploy.service}` at "
            f"{deploy.deployed_at.isoformat()}  `deploy:{deploy.sha}`"
        )
    lines.append(
        f"- **Transcript** {len(ctx.messages)} messages from {len(ctx.participants)} participants"
    )
    lines.append("")
    return lines
