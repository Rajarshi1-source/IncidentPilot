"""Runbook markdown → substituted text → Block Kit (W5-12).

Substitution is `{{placeholder}}`, filled from the incident's own context, and
the reason it exists is that a runbook full of `<your-service>` is a runbook
people re-type by hand at 3 a.m. and get wrong. A command that can be copied and
run is worth several paragraphs of prose.

**An unknown placeholder is left visible, never blanked.** `{{primary_host}}`
with no value renders as `{{primary_host}}`, not as an empty string -- because
`psql -h  -c "..."` is a command that looks runnable, fails confusingly, and
wastes a minute of an incident. Leaving the marker in place makes the gap
obvious to the human reading it.
"""

from __future__ import annotations

import re
from typing import Any

from incidentpilot.adapters.chat import block_kit
from incidentpilot.runbooks.parser import ParsedRunbook, Step

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


def substitute(text: str, context: dict[str, Any]) -> str:
    """Fill `{{name}}` from ``context``; leave unknown markers untouched."""

    def _replace(match: re.Match[str]) -> str:
        value = context.get(match.group(1))
        return match.group(0) if value in (None, "") else str(value)

    return PLACEHOLDER.sub(_replace, text)


def unresolved(text: str) -> list[str]:
    """Which placeholders had no value. Surfaced, not swallowed.

    The renderer reports these so the pinned message can say "3 values could not
    be filled" rather than leaving the responder to notice on their own.
    """
    return sorted({m.group(1) for m in PLACEHOLDER.finditer(text)})


def render_context(
    *,
    service: str | None,
    alertname: str | None,
    severity: str | None,
    public_key: str | None = None,
    namespace: str | None = None,
    primary_host: str | None = None,
    dashboard_url: str | None = None,
) -> dict[str, Any]:
    """The values a runbook may reference.

    A closed set on purpose. Handing the whole incident row to the substituter
    would let a runbook interpolate anything, including fields that will change
    shape later, and the coupling would only show up when one of them did.
    """
    return {
        "service": service,
        "alertname": alertname,
        "severity": severity,
        "public_key": public_key,
        # A default rather than nothing: almost every deployment uses one
        # namespace, and `kubectl -n default` at least runs.
        "namespace": namespace or "default",
        "primary_host": primary_host,
        "dashboard_url": dashboard_url,
    }


def render_steps(runbook: ParsedRunbook, context: dict[str, Any]) -> list[tuple[str, str]]:
    return [(step.step_id, substitute(step.body, context)) for step in runbook.steps]


def render_markdown(runbook: ParsedRunbook, context: dict[str, Any]) -> str:
    """The whole runbook as one markdown string, for the dashboard and the PIR."""
    return substitute(runbook.body, context)


def render_blocks(
    runbook: ParsedRunbook,
    context: dict[str, Any],
    *,
    matched_on: str,
    url: str | None = None,
) -> list[dict[str, Any]]:
    """Block Kit for the pinned message.

    Each step keeps its id in a context line under it. That is not decoration:
    `/step done verify-replica-lag` requires the responder to be able to read the
    id off the message, and it is the same id D4's efficacy query groups by.
    """
    steps = render_steps(runbook, context)
    blocks = block_kit.runbook_message(
        name=runbook.name,
        version=runbook.version,
        steps=steps,
        matched_on=matched_on,
        url=url,
    )

    missing = unresolved("\n".join(body for _, body in steps))
    if missing:
        blocks.append(
            block_kit.context(
                [f":grey_question: unresolved: {', '.join('`{{' + m + '}}`' for m in missing)}"]
            )
        )
    return block_kit.clamp(blocks)


def step_titles(runbook: ParsedRunbook) -> list[tuple[str, str]]:
    """``(step_id, title)`` pairs, for the `/step` command's help text."""
    return [(step.step_id, _title(step)) for step in runbook.steps]


def _title(step: Step) -> str:
    return step.title
