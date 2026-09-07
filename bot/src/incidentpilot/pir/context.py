"""Everything the PIR is allowed to know (W6-08, D1).

Built **from our database**, never from Slack (INV-02, B-01). The transcript was
event-sourced in week 4 precisely so this function is a query rather than ten
minutes of paginated history calls.

``valid_reference_set()`` is the single source of truth for what may be cited.
If an ID is not in there, the model invented it -- and that one sentence is the
whole of the grounding guarantee. Everything else in this file exists to build
that set correctly and to let ``_supports()`` look up the text behind an ID.

**The context is also the prompt's corpus.** The same objects that define what is
citable are the ones rendered into the prompt with their IDs attached, so the
model is choosing from a list rather than recalling from training. That is why
fabrication is rare *and* why it is detectable when it happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.domain.evidence import (
    alert_ref,
    deploy_ref,
    message_ref,
    runbook_ref,
    timeline_ref,
)
from incidentpilot.impact.promql import ImpactReport
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

# How much transcript to carry. A long incident's full transcript will not fit,
# and truncating the *newest* messages would drop the resolution -- so this
# takes the whole thing when it fits and the two ends when it does not, which is
# where the narrative actually lives.
MAX_MESSAGES = 400
DEPLOY_LOOKBACK_HOURS = 6

# The window does not close at `resolved_at`. The deploy that *fixed* the
# incident frequently lands at or just after resolution -- a rollback is
# often the last thing that happens before someone types `/resolve` -- and
# "the fix shipped at 03:41" is a claim a PIR should be able to make. A smoke
# run against the real app found this: the deploy webhook fired a moment
# after the incident closed and the deploy was invisible to the context.
DEPLOY_TRAILING_MINUTES = 30


@dataclass(frozen=True, slots=True)
class MessageRef:
    ts: str
    user_id: str | None
    text: str


@dataclass(frozen=True, slots=True)
class TimelineRef:
    id: str
    at: datetime
    intent: str
    description: str
    author_user_id: str | None


@dataclass(frozen=True, slots=True)
class AlertRef:
    fingerprint: str
    alertname: str
    service: str | None
    starts_at: datetime
    is_root_signal: bool


@dataclass(frozen=True, slots=True)
class DeployRef:
    sha: str
    service: str
    deployed_at: datetime
    title: str | None
    actor: str | None


@dataclass(frozen=True, slots=True)
class RunbookStepRef:
    runbook_id: int
    step_id: str
    detected_by: str


@dataclass
class GroundedContext:
    """The incident, as evidence."""

    incident_id: int
    public_key: str
    title: str
    severity: str
    detected_at: datetime
    resolved_at: datetime | None
    service: str | None
    runbook_name: str | None = None

    messages: list[MessageRef] = field(default_factory=list)
    timeline: list[TimelineRef] = field(default_factory=list)
    alerts: list[AlertRef] = field(default_factory=list)
    deploys: list[DeployRef] = field(default_factory=list)
    runbook_steps: list[RunbookStepRef] = field(default_factory=list)
    impact: ImpactReport | None = None

    # -- the guarantee ---------------------------------------------------

    def valid_reference_set(self) -> set[str]:
        """The single source of truth for what may be cited.

        If an ID is not in here, the model invented it. Six kinds, six
        constructors, all of them from ``domain/evidence.py`` -- building any of
        these strings by hand here would be the first step towards two
        incompatible spellings of the same citation.
        """
        refs = {message_ref(m.ts) for m in self.messages}
        refs |= {timeline_ref(t.id) for t in self.timeline}
        refs |= {alert_ref(a.fingerprint) for a in self.alerts}
        refs |= {deploy_ref(d.sha) for d in self.deploys}
        refs |= {runbook_ref(s.runbook_id, s.step_id) for s in self.runbook_steps}
        if self.impact is not None and self.impact.available:
            refs |= {w.ref for w in self.impact.windows}
        return refs

    def text_for(self, ref: str) -> str | None:
        """The prose behind an ID, for the support check.

        Returns None for an unknown ID rather than an empty string: "no such
        evidence" and "evidence with no words in it" are different, and
        ``_supports`` must not treat a fabricated ID as merely unsupportive.
        """
        for message in self.messages:
            if message_ref(message.ts) == ref:
                return message.text
        for entry in self.timeline:
            if timeline_ref(entry.id) == ref:
                return f"{entry.intent} {entry.description}"
        for alert in self.alerts:
            if alert_ref(alert.fingerprint) == ref:
                return f"{alert.alertname} {alert.service or ''}"
        for deploy in self.deploys:
            if deploy_ref(deploy.sha) == ref:
                return f"deploy {deploy.sha} {deploy.service} {deploy.title or ''}"
        for step in self.runbook_steps:
            if runbook_ref(step.runbook_id, step.step_id) == ref:
                return f"{self.runbook_name or 'runbook'} {step.step_id}"
        if self.impact is not None and self.impact.available:
            for window in self.impact.windows:
                if window.ref == ref:
                    return f"{window.key} {window.query} {window.value}"
        return None

    @property
    def participants(self) -> set[str]:
        """Everyone who spoke or acted. Action-item owners are checked against it.

        Assigning work to somebody who was not in the incident is a specific and
        embarrassing failure -- the model picks a name off the runbook or a
        service label -- and it is trivially detectable.
        """
        who = {m.user_id for m in self.messages if m.user_id}
        who |= {t.author_user_id for t in self.timeline if t.author_user_id}
        who |= {d.actor for d in self.deploys if d.actor}
        return {w for w in who if w}

    @property
    def strong_terms(self) -> set[str]:
        """Words that identify *this* incident rather than describe one.

        Service names, alert names, the runbook -- in this domain they are
        usually single words ("checkout", "payments", "HighErrorRate"), which a
        generic entity regex looking for hyphens and version numbers will never
        match. Without them the support check rejects a perfectly good claim
        that cites a deploy by naming the service it went to, and a
        false rejection pushes an honest draft to the skeleton: the worse of
        the two failures, because it looks like the model being bad rather than
        the gate being wrong.
        """
        terms = {a.alertname.lower() for a in self.alerts}
        terms |= {a.service.lower() for a in self.alerts if a.service}
        terms |= {d.service.lower() for d in self.deploys}
        terms |= {s.step_id.lower() for s in self.runbook_steps}
        if self.service:
            terms.add(self.service.lower())
        if self.runbook_name:
            terms.add(self.runbook_name.lower())
        return {t for t in terms if len(t) > 2}

    @property
    def computed_impact_numbers(self) -> set[str]:
        """Every number the draft is permitted to state (INV-06)."""
        return self.impact.numbers() if self.impact is not None else set()

    def attach_impact(self, impact: ImpactReport) -> None:
        self.impact = impact

    @property
    def root_signal_alert(self) -> AlertRef | None:
        return next((a for a in self.alerts if a.is_root_signal), None)


# --- loading ------------------------------------------------------------------

_MESSAGES = sql(
    """
    SELECT ts, user_id, text
      FROM slack_messages
     WHERE incident_id = :id
     ORDER BY ts
    """
)

_TIMELINE = sql(
    """
    SELECT time, incident_id, intent, description, author_user_id, source_message_ts
      FROM timeline_events
     WHERE incident_id = :id
     ORDER BY time
    """
)

_ALERTS = sql(
    """
    SELECT fingerprint, alertname, service, starts_at, is_root_signal
      FROM alerts
     WHERE incident_id = :id
     ORDER BY starts_at
    """
)

# Deploys are not owned by the incident -- they are facts about the world that
# happened near it. The window is what makes them relevant, and the lookback
# reaches back before detection because the interesting deploy is always the one
# that preceded the alert.
_DEPLOYS = sql(
    """
    WITH bounds AS (
        -- CAST explicitly. Postgres cannot infer the type of a parameter that
        -- only ever appears as `$n IS NULL`, and the failure is an
        -- AmbiguousParameter error at execution time rather than anything
        -- visible while writing the query. Second occurrence in this codebase;
        -- the first was Q7's responder filter.
        SELECT CAST(:service AS text) AS service
    )
    SELECT d.sha, d.service, d.deployed_at, d.title, d.actor
      FROM deploy_events d CROSS JOIN bounds b
     WHERE d.deployed_at BETWEEN :start AND :end
       AND (b.service IS NULL OR d.service = b.service)
     ORDER BY d.deployed_at DESC
     LIMIT 20
    """
)

_STEPS = sql(
    """
    SELECT runbook_id, step_id, detected_by
      FROM runbook_step_signals
     WHERE incident_id = :id
     ORDER BY observed_at
    """
)

_INCIDENT = sql(
    """
    SELECT i.id, i.public_key, i.title, i.severity, i.detected_at, i.resolved_at,
           i.mitigated_at, s.name AS service, r.name AS runbook_name
      FROM incidents i
      LEFT JOIN services s ON s.id = i.primary_service_id
      LEFT JOIN runbooks r ON r.id = i.runbook_id
     WHERE i.id = :id
    """
)


async def build_grounded_context(session: AsyncSession, incident_id: int) -> GroundedContext:
    """Assemble the evidence. One incident, six queries, no network."""
    row = (await session.execute(_INCIDENT, {"id": incident_id})).mappings().first()
    if row is None:
        raise LookupError(f"no such incident: {incident_id}")

    ctx = GroundedContext(
        incident_id=int(row["id"]),
        public_key=str(row["public_key"]),
        title=str(row["title"]),
        severity=str(row["severity"]),
        detected_at=row["detected_at"],
        resolved_at=row["resolved_at"] or row["mitigated_at"],
        service=row["service"],
        runbook_name=row["runbook_name"],
    )

    messages = (await session.execute(_MESSAGES, {"id": incident_id})).mappings().all()
    ctx.messages = [
        MessageRef(ts=str(m["ts"]), user_id=m["user_id"], text=str(m["text"]))
        for m in _trim(list(messages))
    ]

    for entry in (await session.execute(_TIMELINE, {"id": incident_id})).mappings():
        # timeline_events is a hypertable with no surrogate key, so the citable
        # id is the composite that identifies a row: time and intent. Built here
        # rather than invented later, so the renderer can find the row again.
        ctx.timeline.append(
            TimelineRef(
                id=f"{int(entry['time'].timestamp())}-{entry['intent']}",
                at=entry["time"],
                intent=str(entry["intent"]),
                description=str(entry["description"]),
                author_user_id=entry["author_user_id"],
            )
        )

    for alert in (await session.execute(_ALERTS, {"id": incident_id})).mappings():
        ctx.alerts.append(
            AlertRef(
                fingerprint=str(alert["fingerprint"]),
                alertname=str(alert["alertname"]),
                service=alert["service"],
                starts_at=alert["starts_at"],
                is_root_signal=bool(alert["is_root_signal"]),
            )
        )

    from datetime import timedelta

    deploys = await session.execute(
        _DEPLOYS,
        {
            "start": ctx.detected_at - timedelta(hours=DEPLOY_LOOKBACK_HOURS),
            "end": (ctx.resolved_at + timedelta(minutes=DEPLOY_TRAILING_MINUTES))
            if ctx.resolved_at
            else ctx.detected_at + timedelta(hours=DEPLOY_LOOKBACK_HOURS),
            "service": ctx.service,
        },
    )
    for deploy in deploys.mappings():
        ctx.deploys.append(
            DeployRef(
                sha=str(deploy["sha"]),
                service=str(deploy["service"]),
                deployed_at=deploy["deployed_at"],
                title=deploy["title"],
                actor=deploy["actor"],
            )
        )

    for step in (await session.execute(_STEPS, {"id": incident_id})).mappings():
        ctx.runbook_steps.append(
            RunbookStepRef(
                runbook_id=int(step["runbook_id"]),
                step_id=str(step["step_id"]),
                detected_by=str(step["detected_by"]),
            )
        )

    log.info(
        "pir.context_built",
        incident_id=incident_id,
        messages=len(ctx.messages),
        timeline=len(ctx.timeline),
        alerts=len(ctx.alerts),
        deploys=len(ctx.deploys),
        refs=len(ctx.valid_reference_set()),
    )
    return ctx


def _trim(rows: list[Any]) -> list[Any]:
    """Keep both ends when the transcript is too long for one prompt.

    The opening messages say what someone saw first and the closing ones say how
    it ended; the middle is usually a long tail of "still looking". Dropping the
    tail would silently remove the resolution from the evidence.
    """
    if len(rows) <= MAX_MESSAGES:
        return rows
    half = MAX_MESSAGES // 2
    return rows[:half] + rows[-half:]
