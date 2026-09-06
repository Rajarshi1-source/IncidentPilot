"""Data access. The database is the arbiter, never the cache.

The two things worth reading closely here are ``next_dedup_epoch`` (X-03: no
source contained the code that writes it) and ``record_alert`` (X-02: the
re-fire that the unique constraint correctly rejects but that we still want to
count).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.db.models import Alert, Incident, OutboxEvent, Service
from incidentpilot.domain.correlation import OpenIncident
from incidentpilot.domain.normalize import NormalizedAlert
from incidentpilot.domain.states import TERMINAL

TERMINAL_LITERAL = ", ".join(f"'{s.value}'" for s in sorted(TERMINAL))

# Advisory lock keyed on the correlation identity, held for the transaction.
# Serialize per incident, parallelize across incidents: the ordering constraint
# comes from Slack being per-channel and there is one channel per incident, so
# the incident is the natural partition key.
ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:key))")
TRY_ADVISORY_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtext(:key))")


async def acquire_correlation_lock(session: AsyncSession, dedup_key: str) -> None:
    """Block until this correlation key is ours for the rest of the transaction.

    Blocking rather than try-and-skip because the caller is about to decide
    whether to create an incident, and two workers making that decision
    concurrently is exactly the race the dedup constraint exists to catch --
    better to serialize for a few milliseconds than to catch an IntegrityError
    forty times during a storm.
    """
    await session.execute(ADVISORY_LOCK, {"key": f"corr:{dedup_key}"})


async def try_incident_lock(session: AsyncSession, incident_id: int) -> bool:
    """Non-blocking per-incident lock for the stream consumer.

    If it is not acquired the caller leaves the stream entry un-ACKed and
    returns; another worker picks it up later. No busy-waiting, no coordination
    service.
    """
    result = await session.execute(TRY_ADVISORY_LOCK, {"key": f"inc:{incident_id}"})
    return bool(result.scalar())


async def next_dedup_epoch(session: AsyncSession, dedup_key: str) -> int:
    """The epoch a new incident for this key should take (X-03).

    Rev 2 and the ingest skill both explain that ``dedup_epoch`` bumps when an
    incident closes, and ``UNIQUE (dedup_key, dedup_epoch)`` depends on it --
    but no source contains the code that increments it, or says where the next
    value is read from.

    Derived, never stored globally. Correctness rests on the caller holding the
    correlation advisory lock, so two workers cannot compute the same value; and
    if they somehow do, the unique constraint is still the arbiter.
    """
    stmt = select(Incident.dedup_epoch).where(Incident.dedup_key == dedup_key)
    epochs = (await session.execute(stmt)).scalars().all()
    return max(epochs) + 1 if epochs else 0


async def find_open_by_dedup_key(session: AsyncSession, dedup_key: str) -> Incident | None:
    """The non-terminal incident for this key, if one is live."""
    stmt = (
        select(Incident)
        .where(Incident.dedup_key == dedup_key)
        .where(Incident.state.notin_([s.value for s in TERMINAL]))
        .order_by(Incident.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def load_open_incidents(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    limit: int = 200,
) -> list[OpenIncident]:
    """Open incidents, projected into the pure view correlation consumes.

    Returns ``OpenIncident``, not ORM rows, so correlation cannot reach a
    relationship and issue a query. The narrow frozen view makes that
    structurally impossible rather than merely discouraged.
    """
    stmt = (
        select(Incident, Service.name)
        .join(Service, Service.id == Incident.primary_service_id, isouter=True)
        .where(Incident.state.notin_([s.value for s in TERMINAL]))
        .order_by(Incident.detected_at.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(Incident.detected_at >= since)

    rows = (await session.execute(stmt)).all()

    service_names = await _service_name_map(session)
    out: list[OpenIncident] = []
    for incident, primary_name in rows:
        affected = frozenset(
            service_names[sid] for sid in incident.affected_services if sid in service_names
        )
        if primary_name:
            affected = affected | {primary_name}
        out.append(
            OpenIncident(
                id=incident.id,
                severity_rank=_rank(incident.severity),
                detected_at=incident.detected_at,
                primary_service=primary_name,
                stable_labels=_stable_labels_of(incident),
                affected_services=affected,
                last_alert_at=incident.last_alert_at or incident.detected_at,
            )
        )
    return out


async def _service_name_map(session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(select(Service.id, Service.name))).all()
    return dict(rows)  # type: ignore[arg-type]


async def service_id_for(session: AsyncSession, name: str | None) -> int | None:
    if not name:
        return None
    stmt = select(Service.id).where(Service.name == name)
    return (await session.execute(stmt)).scalars().first()


def _rank(severity: str) -> int:
    from incidentpilot.domain.normalize import SEVERITY_RANK

    return SEVERITY_RANK.get(severity, 2)


def _stable_labels_of(incident: Incident) -> dict[str, str]:
    """Labels the incident has accumulated, kept on the impact blob.

    Stored under a reserved key rather than in a column because they are
    correlation state, not something anyone queries -- and JSONB for anything
    queried would be the wrong call.
    """
    raw = incident.impact.get("_stable_labels") if incident.impact else None
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


async def record_alert(
    session: AsyncSession,
    incident_id: int,
    alert: NormalizedAlert,
    *,
    merge_score: float | None = None,
    merge_reasons: list[str] | None = None,
    is_root_signal: bool = False,
) -> bool:
    """Store an alert. Returns True if it was new, False if it was a re-fire.

    X-02: Alertmanager re-sends a firing alert on every ``repeat_interval`` with
    the SAME ``startsAt`` -- that field is when the alert *began*, not when it
    was sent. ``UNIQUE (fingerprint, starts_at)`` correctly rejects the
    duplicate, but a plain DO NOTHING makes the retry invisible. Counting it
    turns a silent no-op into evidence.
    """
    stmt = (
        pg_insert(Alert)
        .values(
            incident_id=incident_id,
            source=str(alert.source),
            fingerprint=alert.fingerprint,
            alertname=alert.alertname,
            service=alert.service,
            labels=alert.labels,
            annotations=alert.annotations,
            starts_at=alert.starts_at,
            ends_at=alert.ends_at,
            is_root_signal=is_root_signal,
            merge_score=merge_score,
            merge_reasons=merge_reasons or [],
            raw=alert.raw,
        )
        .on_conflict_do_update(
            constraint="uq_alert_firing",
            set_={
                "last_seen_at": text("now()"),
                "seen_count": Alert.__table__.c.seen_count + 1,
            },
        )
        .returning(Alert.seen_count)
    )
    seen_count = (await session.execute(stmt)).scalar_one()
    return bool(seen_count == 1)


async def mark_root_signal(session: AsyncSession, incident_id: int, alert_id: int) -> None:
    """Exactly one alert per incident carries the root flag.

    Clearing first rather than assuming: the root signal can change as a cascade
    reveals a deeper cause, and two flagged alerts would make the pinned runbook
    ambiguous.
    """
    await session.execute(
        update(Alert).where(Alert.incident_id == incident_id).values(is_root_signal=False)
    )
    await session.execute(update(Alert).where(Alert.id == alert_id).values(is_root_signal=True))


async def absorb_alert_into_incident(
    session: AsyncSession,
    incident: Incident,
    alert: NormalizedAlert,
    *,
    service_id: int | None,
) -> None:
    """Grow the incident to include this alert.

    Mirrors ``OpenIncident.absorb`` on the persistent side, so the in-memory
    view the correlator scores against and the row on disk stay in step during a
    storm, when forty alerts arrive faster than a round trip each.
    """
    affected = list(incident.affected_services)
    if service_id is not None and service_id not in affected:
        affected.append(service_id)

    labels = dict(incident.impact or {})
    stable = dict(labels.get("_stable_labels") or {})
    for key, value in alert.stable_labels.items():
        stable.setdefault(key, value)
    labels["_stable_labels"] = stable

    incident.correlated_alert_count += 1
    incident.affected_services = affected
    incident.impact = labels
    incident.last_alert_at = max(incident.last_alert_at or incident.detected_at, alert.starts_at)


async def enqueue_outbox(
    session: AsyncSession,
    incident_id: int,
    action: str,
    *,
    payload: dict[str, Any] | None = None,
    discriminator: str = "",
) -> bool:
    """Write the *intent* to perform an external action, in this transaction.

    Returns False when the row already existed, which is the normal path on a
    retry rather than an error. The idempotency key is what licenses the retry:
    without it, a replayed transition creates a second Slack channel.
    """
    import hashlib

    key = hashlib.sha256(f"{incident_id}|{action}|{discriminator}".encode()).hexdigest()

    stmt = (
        pg_insert(OutboxEvent)
        .values(
            incident_id=incident_id,
            action=action,
            payload=payload or {},
            idempotency_key=key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(OutboxEvent.id)
    )
    return (await session.execute(stmt)).scalar() is not None


async def get_for_update(session: AsyncSession, incident_id: int) -> Incident | None:
    """SELECT ... FOR UPDATE. The row lock the transition depends on."""
    stmt = select(Incident).where(Incident.id == incident_id).with_for_update()
    return (await session.execute(stmt)).scalars().first()
