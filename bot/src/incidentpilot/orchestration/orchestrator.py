"""State transitions and incident creation.

Two properties this module exists to guarantee:

1. **A state change and its side-effect intents commit together.** Writing to
   Slack and then to Postgres leaves orphaned channels when the process dies
   between them -- at 30 incidents a day that is weekly, not hypothetical.
2. **Nothing here performs an external write.** The outbox holds the intent; the
   relay (week 3) is the only component that talks to Slack. If two processes
   can create a channel, eventually two will.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.db import repositories as repo
from incidentpilot.db.models import Incident, IncidentTransition
from incidentpilot.domain.correlation import (
    CorrelationConfig,
    Decision,
    correlate,
    is_storm,
    pick_root_signal,
)
from incidentpilot.domain.graph import ServiceGraph
from incidentpilot.domain.normalize import NormalizedAlert
from incidentpilot.domain.severity import assess
from incidentpilot.domain.states import (
    TIMING_FIELD,
    InvalidTransition,
    S,
    assert_transition,
    effect_for,
)
from incidentpilot.telemetry.logging import get_logger, incident_context
from incidentpilot.telemetry.metrics import INCIDENT_TRANSITIONS, STORM_COMPRESSION

log = get_logger(__name__)


def utcnow() -> datetime:
    """The single clock read in orchestration.

    Lives here rather than in ``domain/`` (INV-01) so the pure layer stays
    replayable, and in one function rather than scattered so the replayer has
    one thing to substitute.
    """
    return datetime.now(UTC)


def public_key_for(alert: NormalizedAlert, at: datetime) -> str:
    """Human-readable incident id: inc-2026-09-05-payments-degraded."""
    date = at.date().isoformat()
    service = (alert.service or "unknown").lower().replace("_", "-")
    slug = alert.alertname.lower()
    slug = "".join(c if c.isalnum() else "-" for c in slug).strip("-")
    return f"inc-{date}-{service}-{slug}"[:120]


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What happened to one alert. Returned so the consumer can log and count
    without re-querying."""

    incident_id: int
    created: bool
    decision: Decision
    was_new_alert: bool
    storm: bool = False


class Orchestrator:
    def __init__(self, graph: ServiceGraph, cfg: CorrelationConfig) -> None:
        self._graph = graph
        self._cfg = cfg

    # -- transitions -----------------------------------------------------

    async def transition(
        self,
        session: AsyncSession,
        incident_id: int,
        nxt: S,
        *,
        actor: str,
        reason: str = "",
    ) -> Incident:
        """Advance one incident. Raises ``InvalidTransition`` on an illegal edge.

        The transition table exists twice on purpose: ``assert_transition``
        catches programmer error at development time, and
        ``UNIQUE (incident_id, seq)`` catches concurrency error at 3 a.m. If two
        workers both try to advance the same incident, one commits and the other
        gets a constraint violation and retries against fresh state.
        """
        incident = await repo.get_for_update(session, incident_id)
        if incident is None:
            raise InvalidTransition(f"incident {incident_id} does not exist")

        cur = S(incident.state)
        assert_transition(cur, nxt)

        await session.execute(
            insert(IncidentTransition).values(
                incident_id=incident_id,
                seq=incident.state_seq + 1,
                from_state=cur.value,
                to_state=nxt.value,
                actor=actor,
                reason=reason,
            )
        )

        incident.state = nxt.value
        incident.state_seq += 1

        # Timestamps only. tta/ttm/mttr are GENERATED columns -- one definition,
        # in the schema, incapable of drifting.
        if field := TIMING_FIELD.get(nxt):
            setattr(incident, field, utcnow())

        # Same transaction as the state change. That atomicity is the whole
        # point of the outbox.
        for action in effect_for(cur, nxt).outbox:
            await repo.enqueue_outbox(session, incident_id, action)

        INCIDENT_TRANSITIONS.labels(from_state=cur.value, to_state=nxt.value).inc()
        with incident_context(incident_id=incident_id, state=nxt.value):
            log.info("transition.applied", from_state=cur.value, to_state=nxt.value, actor=actor)

        return incident

    async def compensate(
        self,
        session: AsyncSession,
        incident_id: int,
        *,
        failed_from: S,
        failed_to: S,
        reason: str,
    ) -> list[str]:
        """Undo a transition that could not be completed (Saga compensation).

        ``TransitionEffect.compensate`` sits next to the ``outbox`` tuple in
        ``states.py`` precisely so the undo is impossible to forget -- but a
        declaration nothing reads is documentation, not behaviour. This is the
        code that reads it.

        The concrete case: engagement creates a channel, invites responders and
        pins a runbook. If it fails partway, the channel exists and no incident
        will ever own it. Compensation archives it rather than leaving debris in
        the workspace forever.

        Enqueued through the outbox like any other side effect, so the undo is
        itself retryable and idempotent -- a compensation path that can fail
        without recovery is just a second way to leak.
        """
        actions = effect_for(failed_from, failed_to).compensate
        enqueued: list[str] = []
        for action in actions:
            if await repo.enqueue_outbox(
                session,
                incident_id,
                action,
                payload={"reason": reason, "failed_transition": f"{failed_from}->{failed_to}"},
            ):
                enqueued.append(action)

        if enqueued:
            with incident_context(incident_id=incident_id):
                log.warning(
                    "transition.compensating",
                    failed_from=failed_from.value,
                    failed_to=failed_to.value,
                    actions=enqueued,
                    reason=reason,
                )
        return enqueued

    async def ensure_engaged(
        self,
        session: AsyncSession,
        incident_id: int,
        *,
        actor: str = "system",
    ) -> Incident | None:
        """Drive a freshly detected incident to ``engaged``, idempotently.

        At-least-once delivery means the worker will see the same alert twice --
        after a crash between COMMIT and XACK, XAUTOCLAIM redelivers it. A
        replayed entry must not attempt a transition that already happened, so
        this advances only from the states where advancing is still meaningful
        and returns None when there is nothing to do.

        Blindly calling ``transition()`` on redelivery raises
        ``InvalidTransition`` and sends a perfectly healthy entry to the DLQ.
        """
        incident = await repo.get_for_update(session, incident_id)
        if incident is None:
            return None

        state = S(incident.state)
        if state in {S.DETECTED, S.TRIAGING}:
            if state is S.DETECTED:
                await self.transition(session, incident_id, S.TRIAGING, actor=actor)
            return await self.transition(session, incident_id, S.ENGAGED, actor=actor)

        # Already engaged or beyond: the previous attempt got there. Not an
        # error -- this is what a successful retry looks like.
        return incident

    # -- ingest ----------------------------------------------------------

    async def ingest(self, session: AsyncSession, alert: NormalizedAlert) -> IngestOutcome:
        """Correlate one alert into a new or existing incident.

        Holds the correlation advisory lock for the transaction: two workers
        deciding "new or merge" concurrently for the same key is precisely the
        race the dedup constraint exists to catch, and serializing for a few
        milliseconds beats catching forty IntegrityErrors during a storm.
        """
        await repo.acquire_correlation_lock(session, alert.dedup_key)

        # Exact-identity match first. The same alert re-firing belongs to the
        # incident it already opened, regardless of what the scorer thinks.
        existing = await repo.find_open_by_dedup_key(session, alert.dedup_key)
        if existing is not None:
            return await self._attach(session, existing, alert, Decision.new_incident())

        open_incidents = await repo.load_open_incidents(session)
        decision = correlate(alert, open_incidents, self._graph, self._cfg)

        if decision.is_merge and decision.merge_into is not None:
            target = await repo.get_for_update(session, decision.merge_into)
            if target is not None:
                return await self._attach(session, target, alert, decision)

        return await self._create(session, alert)

    async def _create(self, session: AsyncSession, alert: NormalizedAlert) -> IngestOutcome:
        service_id = await repo.service_id_for(session, alert.service)
        epoch = await repo.next_dedup_epoch(session, alert.dedup_key)
        now = utcnow()

        severity = assess(
            alert_severity=alert.severity,
            service_tier=None,
            affected_service_count=1,
        )

        incident = Incident(
            public_key=f"{public_key_for(alert, alert.starts_at)}-{epoch}"
            if epoch
            else public_key_for(alert, alert.starts_at),
            dedup_key=alert.dedup_key,
            dedup_epoch=epoch,
            title=f"{alert.alertname} on {alert.service or 'unknown'}",
            severity=severity.level,
            severity_reason=severity.reason,
            state=S.DETECTED.value,
            state_seq=0,
            primary_service_id=service_id,
            affected_services=[service_id] if service_id else [],
            root_signal=alert.alertname,
            correlated_alert_count=1,
            detected_at=alert.starts_at,
            last_alert_at=alert.starts_at,
            impact={"_stable_labels": dict(alert.stable_labels)},
            created_at=now,
        )
        session.add(incident)
        await session.flush()  # assign the id without committing

        await repo.record_alert(session, incident.id, alert, is_root_signal=True)

        with incident_context(incident_id=incident.id):
            log.info(
                "incident.created",
                public_key=incident.public_key,
                dedup_key=alert.dedup_key,
                dedup_epoch=epoch,
                severity=severity.level,
                severity_reason=severity.reason,
            )

        return IngestOutcome(
            incident_id=incident.id,
            created=True,
            decision=Decision.new_incident(),
            was_new_alert=True,
        )

    async def _attach(
        self,
        session: AsyncSession,
        incident: Incident,
        alert: NormalizedAlert,
        decision: Decision,
    ) -> IngestOutcome:
        service_id = await repo.service_id_for(session, alert.service)

        was_new = await repo.record_alert(
            session,
            incident.id,
            alert,
            merge_score=decision.score or None,
            merge_reasons=list(decision.reasons),
        )

        if was_new:
            await repo.absorb_alert_into_incident(session, incident, alert, service_id=service_id)

        storm = is_storm(incident.correlated_alert_count, self._cfg)
        if storm:
            # C-11: storm_threshold is configured in every source and read by
            # none of them. It gates the banner and the metric, not the merge --
            # without it the demo's defining moment has no visible output.
            STORM_COMPRESSION.observe(incident.correlated_alert_count)
            await repo.enqueue_outbox(
                session,
                incident.id,
                "post_storm_update",
                payload={
                    "correlated_alert_count": incident.correlated_alert_count,
                    "root_signal": incident.root_signal,
                    "services": len(incident.affected_services),
                },
                # One update per multiple-of-five, so a 40-alert cascade posts
                # eight banners rather than thirty-five. Throttling by content
                # rather than by a timer keeps it deterministic for replay.
                discriminator=f"storm-{incident.correlated_alert_count // 5}",
            )

        with incident_context(incident_id=incident.id):
            log.info(
                "alert.attached",
                alertname=alert.alertname,
                new_alert=was_new,
                count=incident.correlated_alert_count,
                explanation=decision.explanation,
            )

        return IngestOutcome(
            incident_id=incident.id,
            created=False,
            decision=decision,
            was_new_alert=was_new,
            storm=storm,
        )

    async def refresh_root_signal(
        self,
        session: AsyncSession,
        incident_id: int,
    ) -> str | None:
        """Recompute which alert is the root signal as the cascade develops.

        The root can change: a payments alert looks causal until the database
        alert arrives thirty seconds later. The pinned runbook follows the root,
        so it has to be recomputed rather than fixed at creation.
        """
        from sqlalchemy import select

        from incidentpilot.db.models import Alert

        rows = (
            (await session.execute(select(Alert).where(Alert.incident_id == incident_id)))
            .scalars()
            .all()
        )
        if not rows:
            return None

        root = pick_root_signal(list(rows), self._graph)
        if root is None:
            return None

        await repo.mark_root_signal(session, incident_id, root.id)

        incident = await repo.get_for_update(session, incident_id)
        if incident is not None:
            incident.root_signal = root.alertname
        return root.alertname
