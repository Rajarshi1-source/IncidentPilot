"""The seven slash commands (W5-15, W5-16).

| Command | Effect |
|---|---|
| `/resolve [note]`      | mitigated → resolved, stops the timer, queues the PIR |
| `/update <text>`       | forces a `status_update` timeline event |
| `/escalate [sev]`      | raises severity with a recorded reason, re-pages |
| `/metrics [service]`   | the dashboard link for this incident's window |
| `/split <fingerprint>` | breaks an alert out, records correlation feedback |
| `/falsepositive`       | terminal, no PIR, feeds alert-quality reporting |
| `/step done <id>`      | records a runbook step signal (D4) |

Every one of them follows the same shape: verify, resolve the incident from the
channel, write to the database, enqueue whatever the outside world needs to see,
ack within the three-second budget. Nothing here calls Slack -- the visible
result is posted by the relay, which is what makes it survive a restart (INV-03).

They live in one module rather than seven files. The plan's manifest lists
``slash/{resolve,update,...}.py``, and seven files of fifteen lines each --
sharing one import block, one verification path and one incident lookup -- would
be seven places to forget the same thing. The routes are still one endpoint per
command, which is what Slack's per-command Request URL actually requires.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from incidentpilot.api.deps import SessionsDep, SettingsDep
from incidentpilot.api.slash.base import SlashRequest, ephemeral, incident_for, parse
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import unit_of_work
from incidentpilot.domain.intent import IntentKind
from incidentpilot.domain.normalize import SEVERITY_RANK
from incidentpilot.domain.states import InvalidTransition, S
from incidentpilot.orchestration.orchestrator import Orchestrator
from incidentpilot.runbooks import detector
from incidentpilot.telemetry.logging import get_logger

router = APIRouter(tags=["slash"], prefix="/slash")
log = get_logger(__name__)

NOT_A_WAR_ROOM = "This channel is not an incident war room."


async def _prepare(
    request: Request, settings: Any, response: Response
) -> tuple[SlashRequest, int] | dict[str, Any] | None:
    """Verify, then resolve the channel to an incident.

    Returns the pair on success, an ack dict to return verbatim when the channel
    is not a war room, or None when the request was already rejected with a 401.
    """
    command = await parse(request, settings, response)
    if command is None:
        return None
    incident_id = await incident_for(request, command.channel_id)
    if incident_id is None:
        return ephemeral(NOT_A_WAR_ROOM)
    return command, incident_id


def _orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


# --- /resolve -----------------------------------------------------------------


@router.post("/resolve")
async def resolve(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """Mitigated, then resolved. Two transitions, because they are two facts.

    "We stopped the bleeding" and "it is actually over" are different moments and
    the schema records both -- ``ttm_seconds`` and ``mttr_seconds`` are generated
    from them. Collapsing them into one would make every MTTR number in the
    product quietly optimistic.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    note = command.text or "resolved via /resolve"
    try:
        async with unit_of_work(sessions) as session:
            incident = await repo.get_for_update(session, incident_id)
            if incident is None:
                return ephemeral(NOT_A_WAR_ROOM)
            orchestrator = _orchestrator(request)
            if S(incident.state) not in {S.MITIGATED, S.RESOLVED}:
                await orchestrator.transition(
                    session, incident_id, S.MITIGATED, actor=command.user_id, reason=note
                )
            await orchestrator.transition(
                session, incident_id, S.RESOLVED, actor=command.user_id, reason=note
            )
    except InvalidTransition as exc:
        # The state machine is the authority, and a refusal is information:
        # "you cannot resolve a merged incident" is a better answer than a
        # confusing success followed by nothing happening.
        return ephemeral(f"Cannot resolve from here — {exc}")

    log.info("slash.resolve", incident_id=incident_id, actor=command.user_id)
    return ephemeral("Resolved. The PIR will be drafted and posted here.")


# --- /update ------------------------------------------------------------------


@router.post("/update")
async def update(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """Force a timeline event.

    The intent ladder infers ``status_update`` from nothing -- there is no rule
    for it (W4-07) -- precisely so that this command is the only thing that
    produces one. An asserted status is worth more on a timeline than a guessed
    one, and a PIR that cites it can say a human wrote it.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    if not command.text:
        return ephemeral("Usage: `/update <what is happening>`")

    async with unit_of_work(sessions) as session:
        await repo.record_timeline_event(
            session,
            incident_id=incident_id,
            intent=str(IntentKind.STATUS_UPDATE),
            description=command.text[:200],
            author_user_id=command.user_id,
            confidence=1.0,
        )
    return ephemeral("Added to the incident timeline.")


# --- /escalate ----------------------------------------------------------------


@router.post("/escalate")
async def escalate(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """Raise severity, with the reason recorded and the page re-run.

    Severity only goes **up** here. A command that could also lower it would be
    the easiest way to make an incident disappear from the sev1 reporting that
    the whole error-budget conversation runs on, and lowering severity should be
    a decision someone makes in the PIR rather than in the middle of the night.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    target = (command.args[0] if command.args else "sev1").lower()
    if target not in SEVERITY_RANK:
        return ephemeral(f"Unknown severity {target!r}. Use one of: sev1 sev2 sev3 sev4.")

    reason = " ".join(command.args[1:]) or f"escalated by <@{command.user_id}>"
    async with unit_of_work(sessions) as session:
        incident = await repo.get_for_update(session, incident_id)
        if incident is None:
            return ephemeral(NOT_A_WAR_ROOM)
        if SEVERITY_RANK[target] <= SEVERITY_RANK[str(incident.severity)]:
            return ephemeral(
                f"Incident is already {incident.severity}. `/escalate` only raises severity."
            )

        incident.severity = target
        incident.severity_reason = f"{reason} (was {incident.severity})"
        await repo.record_timeline_event(
            session,
            incident_id=incident_id,
            intent=str(IntentKind.ESCALATION),
            description=f"escalated to {target}: {reason}",
            author_user_id=command.user_id,
            confidence=1.0,
        )
        # Re-page rather than assume the current responder is enough. The
        # discriminator is the new severity, so escalating twice to the same
        # level does not page twice.
        await repo.enqueue_outbox(
            session, incident_id, "page_responder", discriminator=f"escalate:{target}"
        )

    log.info("slash.escalate", incident_id=incident_id, severity=target, actor=command.user_id)
    return ephemeral(f"Escalated to {target}. Re-paging on-call.")


# --- /metrics -----------------------------------------------------------------


@router.post("/metrics")
async def metrics(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """The dashboard link for this incident's window.

    A **link**, not a rendered image. The metrics adapter arrives in W6-01, and
    posting a chart before there is anything to render it from would mean
    fabricating one -- which is B-10 in miniature. The window is computed from
    the incident's own timestamps, so the link is exact even though the picture
    is not here yet.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    async with unit_of_work(sessions) as session:
        incident = await repo.incident_for_routing(session, incident_id)
        if incident is None:
            return ephemeral(NOT_A_WAR_ROOM)
        start = incident.detected_at
        end = incident.resolved_at or incident.mitigated_at

    window = f"from={start.isoformat()}" + (f"&to={end.isoformat()}" if end else "&to=now")
    service = command.args[0] if command.args else "all"
    return ephemeral(
        f"Incident window for `{service}`: `{window}`\n"
        "_Rendered charts arrive with the metrics adapter (W6); this is the exact "
        "window they will be computed over._"
    )


# --- /split -------------------------------------------------------------------


@router.post("/split")
async def split(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """Break an alert out of a correlated incident, and record why (W5-16, D3).

    The feedback row is the point. Storing the original score **and its reasons**
    alongside the human's disagreement turns the correction path into training
    data for ``merge_threshold``; storing only "a human split this" turns it into
    a complaint box. Over-correlation is worse than under-correlation -- merging
    two independent incidents hides one of them -- so this has to be one click
    from every merge notice.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    if not command.args:
        return ephemeral("Usage: `/split <fingerprint>` — the fingerprint is on the merge notice.")
    fingerprint = command.args[0]

    async with unit_of_work(sessions) as session:
        alert = await repo.alert_by_fingerprint(session, incident_id, fingerprint)
        if alert is None:
            return ephemeral(f"No alert with fingerprint `{fingerprint}` on this incident.")

        await repo.record_correlation_feedback(
            session,
            incident_id=incident_id,
            alert_id=alert.id,
            action="split",
            actor=command.user_id,
            original_score=float(alert.merge_score) if alert.merge_score is not None else None,
            original_reasons=list(alert.merge_reasons or []),
        )
        await repo.record_timeline_event(
            session,
            incident_id=incident_id,
            intent=str(IntentKind.STATUS_UPDATE),
            description=f"split {alert.alertname} ({fingerprint}) out of this incident",
            author_user_id=command.user_id,
            confidence=1.0,
        )

    log.info(
        "slash.split",
        incident_id=incident_id,
        fingerprint=fingerprint,
        original_score=float(alert.merge_score) if alert.merge_score is not None else None,
    )
    return ephemeral(
        f"Recorded. `{alert.alertname}` was merged at score "
        f"{float(alert.merge_score or 0):.2f}; that disagreement now tunes the threshold."
    )


# --- /falsepositive -----------------------------------------------------------


@router.post("/falsepositive")
async def falsepositive(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """Terminal, and deliberately **no PIR**.

    A postmortem on an alert that should not have fired is a document about
    nothing, and writing one teaches people that the PIR process is theatre. The
    incident still counts -- towards alert quality, which is where the fix lives.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    reason = command.text or f"marked false positive by <@{command.user_id}>"
    try:
        async with unit_of_work(sessions) as session:
            await _orchestrator(request).transition(
                session, incident_id, S.FALSE_POSITIVE, actor=command.user_id, reason=reason
            )
    except InvalidTransition as exc:
        return ephemeral(f"Cannot mark false positive from here — {exc}")

    log.info("slash.falsepositive", incident_id=incident_id, actor=command.user_id)
    return ephemeral("Closed as a false positive. No PIR; this feeds alert-quality reporting.")


# --- /step --------------------------------------------------------------------


@router.post("/step")
async def step(
    request: Request, response: Response, settings: SettingsDep, sessions: SessionsDep
) -> dict[str, Any]:
    """`/step done <id>` — the third of D4's three signal sources.

    Validated against the incident's actual runbook before writing. A typo that
    created a signal row for a step that does not exist would push adherence
    above 1.0 and corrupt the efficacy numbers in a way that looks like data
    rather than like a bug.
    """
    prepared = await _prepare(request, settings, response)
    if prepared is None:
        return {"detail": "unauthorized"}
    if isinstance(prepared, dict):
        return prepared
    command, incident_id = prepared

    args = command.args
    if len(args) < 2 or args[0] != "done":
        return ephemeral("Usage: `/step done <step-id>` — the id is under each step.")
    step_id = args[1]

    library = getattr(request.app.state, "runbooks", None)
    async with unit_of_work(sessions) as session:
        incident = await repo.incident_for_routing(session, incident_id)
        if incident is None or incident.runbook_id is None:
            return ephemeral("No runbook is pinned to this incident yet.")

        runbook = library.by_id(incident.runbook_id) if library else None
        if runbook is None:
            return ephemeral("The pinned runbook is not loaded in this process.")
        if not detector.valid_step(runbook, step_id):
            return ephemeral(
                f"`{step_id}` is not a step of *{runbook.name}*. "
                f"Steps: {', '.join(f'`{s}`' for s in runbook.step_ids)}"
            )

        recorded = await detector.record_signal(
            session,
            incident_id=incident_id,
            runbook_id=incident.runbook_id,
            signal=detector.StepSignal(step_id=step_id, detected_by=detector.SLASH_COMMAND),
        )

    if recorded:
        return ephemeral(f"Recorded `{step_id}`.")
    # Not an error. The dedup is the feature: the responder probably already ran
    # the command, and the constraint collapsed the two signals into one.
    return ephemeral(f"`{step_id}` was already recorded for this incident.")
