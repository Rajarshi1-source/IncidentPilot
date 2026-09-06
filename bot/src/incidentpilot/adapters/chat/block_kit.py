"""Block Kit builders.

A builder rather than string concatenation, because Block Kit JSON is verbose
and fails in subtle, hard-to-debug ways -- a malformed block returns a generic
``invalid_blocks`` with no indication of which one.

Two hard limits apply and both are enforced here rather than discovered in
production: **50 blocks per message** and **3000 characters per section text**.
"""

from __future__ import annotations

from typing import Any

MAX_BLOCKS = 50
MAX_SECTION_CHARS = 3000

SEVERITY_EMOJI = {
    "sev1": ":rotating_light:",
    "sev2": ":warning:",
    "sev3": ":large_yellow_circle:",
    "sev4": ":information_source:",
}


def _truncate(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    """Truncate visibly. A silently cut section reads as complete and is not."""
    if len(text) <= limit:
        return text
    marker = "\n\n_… truncated_"
    return text[: limit - len(marker)] + marker


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text)}}


def context(elements: list[str]) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": _truncate(e, 150)} for e in elements[:10]],
    }


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def header(text: str) -> dict[str, Any]:
    # Header blocks are plain_text only and cap at 150 characters.
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def actions(buttons: list[tuple[str, str, str]]) -> dict[str, Any]:
    """``[(text, action_id, value), ...]``. Max five per block."""
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label[:75], "emoji": True},
                "action_id": action_id,
                "value": value,
            }
            for label, action_id, value in buttons[:5]
        ],
    }


def clamp(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce the 50-block ceiling, saying so rather than truncating silently.

    A long PIR paginates or links out to the dashboard; it never quietly loses
    its last sections, because the reader has no way to tell that happened.
    """
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    kept = blocks[: MAX_BLOCKS - 1]
    kept.append(context([f":scissors: {len(blocks) - MAX_BLOCKS + 1} more blocks — see dashboard"]))
    return kept


def incident_header(
    *,
    public_key: str,
    title: str,
    severity: str,
    root_signal: str | None,
    correlated_alert_count: int,
    affected_services: int,
    elapsed_label: str,
    responders: list[str],
    runbook_url: str | None = None,
    merge_reasons: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The pinned message, updated in place for the life of the incident.

    One message rather than a stream of them: a war room where the bot posts a
    new status line every thirty seconds is a war room nobody can read.
    """
    emoji = SEVERITY_EMOJI.get(severity, ":warning:")
    # "_No responder engaged yet_" rather than an empty line: the absence of a
    # responder is information during an incident, and a blank space reads as a
    # rendering bug rather than a fact.
    who = (
        "*Responders:* " + ", ".join(f"<@{r}>" for r in responders)
        if responders
        else "_No responder engaged yet_"
    )
    blocks: list[dict[str, Any]] = [
        header(f"{emoji} {title}"),
        section(f"*{public_key}*  ·  *{severity.upper()}*  ·  elapsed *{elapsed_label}*\n{who}"),
    ]

    if correlated_alert_count > 1:
        # The D3 proof, in the place people actually look. "40 correlated
        # alerts, root signal postgres-primary" is the demo's opening moment.
        detail = (
            f"*{correlated_alert_count} correlated alerts* across "
            f"*{affected_services} service{'s' if affected_services != 1 else ''}*"
        )
        if root_signal:
            detail += f"\n:dart: Root signal: `{root_signal}`"
        blocks.append(section(detail))

    if merge_reasons:
        # Explainability is the differentiator over opaque commercial grouping.
        # A responder who cannot see why two alerts were merged cannot trust it.
        blocks.append(context([f":link: {reason}" for reason in merge_reasons[:4]]))

    if runbook_url:
        blocks.append(context([f":books: <{runbook_url}|Runbook>"]))

    return clamp(blocks)


def merge_notice(
    *,
    alertname: str,
    service: str | None,
    score: float,
    reasons: list[str],
    fingerprint: str,
) -> list[dict[str, Any]]:
    """Posted when an alert is absorbed. Always shows its reasoning.

    ``/split`` is offered on every merge because over-correlation is worse than
    under-correlation -- merging two genuinely independent incidents hides one
    of them, and the correction has to be one click away.
    """
    where = f" on `{service}`" if service else ""
    return clamp(
        [
            section(f":heavy_plus_sign: Correlated *{alertname}*{where}"),
            context([f"score *{score:.2f}* — " + ", ".join(reasons[:3])]),
            actions([("Split into its own incident", "split_alert", fingerprint)]),
        ]
    )


def storm_notice(
    *,
    correlated_alert_count: int,
    affected_services: int,
    root_signal: str | None,
) -> list[dict[str, Any]]:
    root = f"\n:dart: Root signal: `{root_signal}`" if root_signal else ""
    return clamp(
        [
            section(
                f":cyclone: *Alert storm* — *{correlated_alert_count}* alerts absorbed into "
                f"this incident across *{affected_services}* services.{root}"
            ),
            context(["One war room, not forty. `/split` if any of these is unrelated."]),
        ]
    )


def timer(*, elapsed_label: str, state: str) -> list[dict[str, Any]]:
    """Priority 2. Dropped under pressure -- a stale timer is invisible."""
    return [context([f":stopwatch: *{elapsed_label}* elapsed  ·  state `{state}`"])]


def degradation_banner(level: int, reason: str) -> list[dict[str, Any]]:
    """Degradation is announced, never silent (INV-12).

    Silent degradation is worse than failure, because people keep trusting
    output that is no longer trustworthy.
    """
    labels = {1: "Degraded", 2: "Brownout", 3: "Lifeboat"}
    return [
        section(
            f":warning: *IncidentPilot is running in "
            f"{labels.get(level, 'reduced')} mode* — {reason}"
        )
    ]
