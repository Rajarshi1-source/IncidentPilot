"""Citations become chips (W6-19, D1).

Two renderings from one draft: markdown for ``pir_documents`` and the dashboard,
Block Kit for the channel. The Block Kit form is the demo's money shot, so the
citation chips get real design attention rather than being a list of ids in
brackets.

**A chip is a context block with a permalink**, resolved through
``chat.getPermalink`` -- which is a *write-path* API call and therefore made by
the relay handler, not here. This module produces the blocks with the ids in
them and a permalink map; the handler fills it. Keeping the resolution out of
here is what stops the renderer needing a Slack client (INV-03).

Chips are labelled by kind, not numbered. `msg 14:02` and `deploy a1b2c3d` say
what the evidence *is*; `[3]` makes the reader hunt for a footnote nobody wrote.
"""

from __future__ import annotations

from typing import Any

from incidentpilot.adapters.chat import block_kit
from incidentpilot.domain.evidence import CitationKind, parse_reference
from incidentpilot.impact.promql import ImpactReport
from incidentpilot.pir.context import GroundedContext
from incidentpilot.pir.schema import Citation, PIRDraft

CHIP_EMOJI: dict[CitationKind, str] = {
    CitationKind.MESSAGE: ":speech_balloon:",
    CitationKind.TIMELINE: ":clock3:",
    CitationKind.ALERT: ":rotating_light:",
    CitationKind.DEPLOY: ":rocket:",
    CitationKind.METRIC: ":bar_chart:",
    CitationKind.RUNBOOK: ":books:",
}

SECTIONS: tuple[tuple[str, str], ...] = (
    ("summary", "Summary"),
    ("root_cause_hypothesis", "Root cause"),
    ("contributing_factors", "Contributing factors"),
    ("what_went_well", "What went well"),
    ("what_went_wrong", "What went wrong"),
)


def chip_label(citation: Citation, ctx: GroundedContext) -> str:
    """A short, human label for one citation.

    The reference id is exact and unreadable; the label is readable and
    ambiguous. Both appear -- the label in the chip, the id in the markdown --
    because a reader scanning the channel wants "14:02" and a reviewer checking
    the document wants `msg:1757000012.000100`.
    """
    try:
        reference = parse_reference(citation.ref)
    except ValueError:
        return citation.ref[:40]

    emoji = CHIP_EMOJI.get(reference.kind, ":link:")
    if reference.kind is CitationKind.MESSAGE:
        for message in ctx.messages:
            if message.ts == reference.body:
                return f"{emoji} msg {_hhmm(message.ts)}"
        return f"{emoji} msg"
    if reference.kind is CitationKind.DEPLOY:
        return f"{emoji} deploy {reference.body[:8]}"
    if reference.kind is CitationKind.ALERT:
        for alert in ctx.alerts:
            if alert.fingerprint == reference.body:
                return f"{emoji} {alert.alertname}"
        return f"{emoji} alert"
    if reference.kind is CitationKind.RUNBOOK:
        return f"{emoji} {reference.body.split('#')[-1]}"
    if reference.kind is CitationKind.METRIC:
        return f"{emoji} metric"
    return f"{emoji} {reference.kind}"


def _hhmm(slack_ts: str) -> str:
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(float(slack_ts), UTC).strftime("%H:%M")
    except TypeError, ValueError, OSError, OverflowError:
        return slack_ts[:10]


# --- markdown -----------------------------------------------------------------


def render_pir_markdown(ctx: GroundedContext, draft: PIRDraft, impact: ImpactReport) -> str:
    """The stored form. Every claim followed by its reference ids.

    Ids rather than labels here, because this is the auditable copy: someone
    checking a contested claim needs the exact id to look the evidence up, and
    "msg 14:02" is not a lookup key.
    """
    lines: list[str] = [
        f"# {ctx.public_key} — {ctx.title}",
        "",
        f"**Severity** {ctx.severity.upper()}  ·  **Service** {ctx.service or 'unknown'}",
        "",
    ]

    lines += _impact_markdown(impact)

    for field_name, heading in SECTIONS:
        value = getattr(draft, field_name)
        if not value:
            if field_name == "root_cause_hypothesis":
                # The honest null, rendered rather than omitted. A missing
                # section reads as an oversight; this reads as a finding.
                lines += [
                    f"## {heading}",
                    "",
                    "_Not established during this incident._",
                    "",
                ]
            continue
        lines += [f"## {heading}", ""]
        claims = value if isinstance(value, list) else [value]
        for claim in claims:
            refs = " ".join(f"`{c.ref}`" for c in claim.citations)
            lines.append(f"- {claim.text}  {refs}")
        lines.append("")

    if draft.timeline:
        lines += ["## Timeline", ""]
        for entry in draft.timeline:
            refs = " ".join(f"`{c.ref}`" for c in entry.citations)
            lines.append(f"- `{entry.at.strftime('%H:%M:%S')}` {entry.description}  {refs}")
        lines.append("")

    if draft.action_items:
        lines += [
            "## Action items",
            "",
            "| Priority | Owner | Action | Evidence |",
            "|---|---|---|---|",
        ]
        for item in draft.action_items:
            owner = f"<@{item.owner}>" if item.owner else "_unassigned_"
            refs = " ".join(f"`{c.ref}`" for c in item.citations)
            lines.append(f"| {item.priority} | {owner} | {item.description} | {refs} |")
        lines.append("")

    lines += [
        "---",
        "",
        "_Every claim above carries a citation into the stored incident record. "
        "Impact figures are computed from metrics, never generated._",
    ]
    return "\n".join(lines)


def _impact_markdown(impact: ImpactReport) -> list[str]:
    lines = ["## Impact", ""]
    if not impact.available:
        lines += [
            "_Metrics unavailable for this window — **no figures are estimated.**_",
            "",
        ]
        return lines
    lines += ["| Metric | Value |", "|---|---|"]
    for key, value in sorted(impact.values.items()):
        lines.append(f"| `{key}` | {'unknown' if value is None else f'{value:g}'} |")
    lines.append("")
    return lines


# --- Block Kit ----------------------------------------------------------------


def render_pir_blocks(
    ctx: GroundedContext,
    draft: PIRDraft,
    impact: ImpactReport,
    *,
    permalinks: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The channel form, with citation chips.

    ``permalinks`` maps a reference id to a resolved URL. Supplied by the relay
    handler, which is the only component allowed to call ``chat.getPermalink``
    (INV-03); absent, the chips still render with their labels and simply do not
    link. A missing permalink must never remove the citation.
    """
    links = permalinks or {}
    blocks: list[dict[str, Any]] = [
        block_kit.header(f"Post-incident review — {ctx.public_key}"),
        block_kit.section(f"*{ctx.title}*\n{ctx.severity.upper()}  ·  {ctx.service or 'unknown'}"),
    ]

    if impact.available:
        figures = "  ·  ".join(
            f"*{k}* {'—' if v is None else f'{v:g}'}" for k, v in sorted(impact.values.items())
        )
        blocks.append(block_kit.section(f":bar_chart: {figures}"))
    else:
        blocks.append(
            block_kit.context([":grey_question: metrics unavailable — no figures estimated"])
        )

    for field_name, heading in SECTIONS:
        value = getattr(draft, field_name)
        if not value:
            if field_name == "root_cause_hypothesis":
                blocks.append(block_kit.section(f"*{heading}*\n_Not established._"))
            continue
        claims = value if isinstance(value, list) else [value]
        blocks.append(block_kit.section(f"*{heading}*"))
        for claim in claims:
            blocks.append(block_kit.section(f"• {claim.text}"))
            blocks.append(block_kit.context(_chips(claim.citations, ctx, links)))

    if draft.action_items:
        blocks.append(block_kit.divider())
        blocks.append(block_kit.section("*Action items*"))
        for item in draft.action_items:
            owner = f"<@{item.owner}>" if item.owner else "_unassigned_"
            blocks.append(block_kit.section(f"`{item.priority}` {owner} — {item.description}"))
            blocks.append(block_kit.context(_chips(item.citations, ctx, links)))

    blocks.append(block_kit.divider())
    blocks.append(
        block_kit.actions(
            [
                ("Approve", "pir_approve", ctx.public_key),
                ("Request changes", "pir_changes", ctx.public_key),
            ]
        )
    )
    return block_kit.clamp(blocks)


def _chips(citations: list[Citation], ctx: GroundedContext, links: dict[str, str]) -> list[str]:
    out: list[str] = []
    for citation in citations:
        label = chip_label(citation, ctx)
        url = links.get(citation.ref)
        out.append(f"<{url}|{label}>" if url else label)
    return out


def citation_refs(draft: PIRDraft) -> list[str]:
    """Every reference id in the draft, for the handler to resolve permalinks."""
    from incidentpilot.pir.schema import walk_claims

    refs: list[str] = []
    for _, item in walk_claims(draft):
        refs.extend(c.ref for c in item.citations)
    return sorted(set(refs))
