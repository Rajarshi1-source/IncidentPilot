"""Data access. The database is the arbiter, never the cache.

The two things worth reading closely here are ``next_dedup_epoch`` (X-03: no
source contained the code that writes it) and ``record_alert`` (X-02: the
re-fire that the unique constraint correctly rejects but that we still want to
count).
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.db.models import (
    Alert,
    CorrelationFeedback,
    Incident,
    OutboxEvent,
    PageEvent,
    Responder,
    Runbook,
    Service,
    SlackMessage,
    TimelineEvent,
)
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


# --- the transcript (W4) -----------------------------------------------------

# Six hours: longer than nearly every incident, so the mapping is a cache hit
# for the whole life of a war room.
CHANNEL_CACHE_TTL_S = 6 * 3600

# Negative answers expire in a minute, not six hours. A channel becomes an
# incident channel a fraction of a second after conversations.create returns,
# and a six-hour negative cache would blackhole the opening messages of an
# incident -- the ones that say what someone saw first, which are the ones a PIR
# most wants to cite.
CHANNEL_NEGATIVE_TTL_S = 60

# Distinguishes "cached: not ours" from "not cached". Storing an empty string
# would be indistinguishable from a miss.
NOT_AN_INCIDENT = "-"


class ChannelCache:
    """``channel_id -> incident_id``, cached in Valkey, arbitrated by Postgres.

    INV-04: the cache is an accelerator, never the source of truth. Every miss
    -- and every Valkey failure -- falls through to the database, so
    ``FLUSHALL`` costs latency and nothing else. A design that stored the
    mapping only in the cache would lose the transcript of every live incident
    on an eviction, and the loss would be silent (B-07).
    """

    def __init__(
        self,
        valkey: Any,
        sessions: Any,
        *,
        ttl_s: int = CHANNEL_CACHE_TTL_S,
        negative_ttl_s: int = CHANNEL_NEGATIVE_TTL_S,
        prefix: str = "ip:chan",
    ) -> None:
        self._valkey = valkey
        self._sessions = sessions
        self._ttl_s = ttl_s
        self._negative_ttl_s = negative_ttl_s
        self._prefix = prefix

    def _key(self, channel_id: str) -> str:
        return f"{self._prefix}:{channel_id}"

    async def incident_for_channel(self, channel_id: str) -> int | None:
        cached = await self._get(channel_id)
        if cached == NOT_AN_INCIDENT:
            return None
        if cached is not None:
            return int(cached)

        async with self._sessions() as session:
            incident_id = await incident_id_for_channel(session, channel_id)

        await self._put(channel_id, incident_id)
        return incident_id

    async def invalidate(self, channel_id: str) -> None:
        """Drop one mapping. Called when a channel is recorded onto an incident.

        Suppressed rather than raised: a cache that cannot be cleared costs at
        most six hours of a stale mapping, and the database is the arbiter
        anyway (INV-04).
        """
        with contextlib.suppress(Exception):
            await self._valkey.delete(self._key(channel_id))

    async def _get(self, channel_id: str) -> str | None:
        # Valkey unreachable? Ingest must keep working: a dropped message is
        # unrecoverable, an uncached lookup costs a millisecond.
        value = None
        with contextlib.suppress(Exception):
            value = await self._valkey.get(self._key(channel_id))
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def _put(self, channel_id: str, incident_id: int | None) -> None:
        value = NOT_AN_INCIDENT if incident_id is None else str(incident_id)
        ttl = self._negative_ttl_s if incident_id is None else self._ttl_s
        with contextlib.suppress(Exception):
            await self._valkey.set(self._key(channel_id), value, ex=ttl)


async def incident_id_for_channel(session: AsyncSession, channel_id: str) -> int | None:
    """The database's answer, which is the only authoritative one (INV-04)."""
    stmt = select(Incident.id).where(Incident.chat_channel_id == channel_id)
    return (await session.execute(stmt)).scalars().first()


async def store_message(
    session: AsyncSession,
    *,
    incident_id: int,
    channel_id: str,
    ts: str,
    text_body: str,
    thread_ts: str | None = None,
    user_id: str | None = None,
    raw: dict[str, Any] | None = None,
) -> int | None:
    """Persist one message. Returns its id, or None if it was a redelivery.

    ``ON CONFLICT DO NOTHING`` on ``(channel_id, ts)`` is the whole conversion
    from at-least-once delivery to exactly-once storage. Returning None rather
    than raising is deliberate: a redelivery is the *normal* shape of the Events
    API under retry, not an error condition, and treating it as one would fill
    the logs during exactly the minute nobody can afford noise.
    """
    stmt = (
        pg_insert(SlackMessage)
        .values(
            incident_id=incident_id,
            channel_id=channel_id,
            ts=ts,
            thread_ts=thread_ts,
            user_id=user_id,
            text=text_body,
            raw=raw or {},
        )
        .on_conflict_do_nothing(constraint="uq_message_channel_ts")
        .returning(SlackMessage.id)
    )
    return (await session.execute(stmt)).scalar()


# Append-only by construction, and idempotent under redelivery.
#
# Two mechanisms, because they cover different failures. The revision number is
# computed *inside* the INSERT, so two concurrent edits cannot both read "1" and
# both write it -- the loser hits uq_message_revision and DO NOTHING makes it a
# no-op. And the NOT EXISTS guard compares against the *latest* revision, so the
# Events API redelivering the same message_changed event does not append a
# second identical row.
#
# Comparing against the latest rather than against any prior revision is
# deliberate: an edit sequence A -> B -> A is three real revisions and must stay
# three, while a redelivery is always identical to the row immediately before it.
APPEND_REVISION = text(
    """
    INSERT INTO slack_message_revisions (message_id, revision, kind, text)
    SELECT m.id, COALESCE(latest.revision, 0) + 1, :kind, :text
      FROM slack_messages m
      LEFT JOIN LATERAL (
           SELECT r.revision, r.kind, r.text
             FROM slack_message_revisions r
            WHERE r.message_id = m.id
            ORDER BY r.revision DESC
            LIMIT 1
      ) latest ON TRUE
     WHERE m.channel_id = :channel_id
       AND m.ts = :ts
       AND (latest.revision IS NULL
            OR latest.kind IS DISTINCT FROM :kind
            OR latest.text IS DISTINCT FROM :text)
    ON CONFLICT ON CONSTRAINT uq_message_revision DO NOTHING
    RETURNING id, revision
    """
)


async def append_revision(
    session: AsyncSession,
    *,
    channel_id: str,
    ts: str,
    kind: str,
    text_body: str | None,
) -> int | None:
    """Record an edit or a delete as a new revision. Returns the revision number.

    The original row is never touched. A PIR cites ``msg:{ts}``; a message that
    could be rewritten in place would make that citation a claim about the
    present rather than evidence about the past, and the difference is the whole
    point of the document.

    Returns None when the message is unknown -- an edit to something that
    arrived before the bot joined -- or when this exact revision already exists.
    """
    row = (
        await session.execute(
            APPEND_REVISION,
            {"channel_id": channel_id, "ts": ts, "kind": kind, "text": text_body},
        )
    ).first()
    return int(row.revision) if row else None


async def stored_message_count(session: AsyncSession, channel_id: str) -> int:
    """Rows we hold for a channel -- the numerator of transcript completeness."""
    result = await session.execute(
        text("SELECT count(*) FROM slack_messages WHERE channel_id = :c"),
        {"c": channel_id},
    )
    return int(result.scalar_one())


async def active_incident_channels(session: AsyncSession) -> dict[str, int]:
    """``chat_channel_id -> incident_id`` for every non-terminal incident.

    The reconciler's work list: the channels whose completeness ratio is still
    changing, and therefore the only ones worth spending the history budget on.
    """
    stmt = (
        select(Incident.chat_channel_id, Incident.id)
        .where(Incident.chat_channel_id.isnot(None))
        .where(Incident.state.notin_([s.value for s in TERMINAL]))
    )
    rows = (await session.execute(stmt)).all()
    return {str(channel_id): int(incident_id) for channel_id, incident_id in rows}


async def known_channel_ids(session: AsyncSession) -> set[str]:
    """Every channel any incident has ever owned, terminal or not.

    Orphan detection asks "does *any* incident row know about this channel",
    not "is one open" -- archiving the channel of a closed incident because it
    is no longer active would delete the record people go back to read (B-08).
    """
    stmt = select(Incident.chat_channel_id).where(Incident.chat_channel_id.isnot(None))
    return {str(cid) for cid in (await session.execute(stmt)).scalars().all()}


async def held_message_ts(
    session: AsyncSession, channel_id: str, candidates: list[str]
) -> set[str]:
    """Which of these Slack timestamps we already store.

    A set intersection rather than a count comparison: equal counts over
    different timestamps is a real failure mode -- one message lost and one
    stored twice -- and a count-only check would report it as healthy.
    """
    if not candidates:
        return set()
    rows = await session.execute(
        text("SELECT ts FROM slack_messages WHERE channel_id = :c AND ts = ANY(:ts)"),
        {"c": channel_id, "ts": list(candidates)},
    )
    return {str(ts) for ts in rows.scalars().all()}


# --- responders and paging (W5) ----------------------------------------------


class OnCallCache:
    """``schedule -> OnCall``, cached in Valkey for a minute (W5-05).

    Sixty seconds, not six hours. A rotation handover mid-incident must not keep
    paging the person who just went to bed, and the provider call is cheap
    enough that a short TTL costs nothing. The channel cache can afford six
    hours because a channel's incident never changes; an on-call rota changes on
    a schedule.

    Like every cache here, it is an accelerator and never an arbiter (INV-04):
    a miss, an eviction or an unreachable Valkey all fall through to the ladder.
    """

    def __init__(self, valkey: Any, *, ttl_s: int = 60, prefix: str = "ip:oncall") -> None:
        self._valkey = valkey
        self._ttl_s = ttl_s
        self._prefix = prefix

    def _key(self, schedule: str) -> str:
        return f"{self._prefix}:{schedule}"

    async def get(self, schedule: str) -> tuple[str | None, str | None] | None:
        """``(primary, secondary)``, or None on a miss."""
        raw = None
        with contextlib.suppress(Exception):
            raw = await self._valkey.get(self._key(schedule))
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        primary, _, secondary = value.partition("|")
        return (primary or None, secondary or None)

    async def put(self, schedule: str, primary: str | None, secondary: str | None) -> None:
        """Only a *confident* answer is cached.

        A degraded answer from the static rota must not be written back: caching
        it would keep the incident on the fallback rung for the whole TTL even
        after the provider recovered, and -- worse -- the next lookup would be a
        cache hit and would stop announcing itself as degraded.
        """
        if primary is None:
            return
        with contextlib.suppress(Exception):
            await self._valkey.set(
                self._key(schedule), f"{primary}|{secondary or ''}", ex=self._ttl_s
            )


async def responder_timezone(session: AsyncSession, slack_user_id: str) -> str:
    """The responder's local timezone, defaulting to UTC.

    Denormalized onto every ``page_events`` row at write time so the fatigue
    window can decide "was this a night page" without joining across chunks.
    """
    stmt = select(Responder.timezone).where(Responder.slack_user_id == slack_user_id)
    return str((await session.execute(stmt)).scalars().first() or "UTC")


async def record_page(
    session: AsyncSession,
    *,
    at: datetime,
    responder: str,
    incident_id: int | None,
    severity: str | None,
    tz: str = "UTC",
    accepted: bool | None = None,
) -> None:
    """One row per page. The raw material the fatigue score is computed from.

    Written by the relay in the same transaction that records the dispatch, so a
    page that happened is always a page that was counted -- a fatigue score
    built on a partial record would under-count exactly the responders who were
    paged during the outage that dropped the writes.
    """
    await session.execute(
        pg_insert(PageEvent).values(
            time=at,
            responder=responder,
            incident_id=incident_id,
            severity=severity,
            tz=tz,
            accepted=accepted,
        )
    )


async def incident_for_routing(session: AsyncSession, incident_id: int) -> Incident | None:
    stmt = select(Incident).where(Incident.id == incident_id)
    return (await session.execute(stmt)).scalars().first()


async def team_for_service(session: AsyncSession, service_id: int | None) -> str | None:
    """The paging schedule a service belongs to.

    ``services.team`` doubles as the schedule name rather than adding a
    ``paging_schedule`` column: one string, one place to keep in step with the
    rota file, and the mapping is already what an on-call rotation is named after.
    """
    if service_id is None:
        return None
    stmt = select(Service.team).where(Service.id == service_id)
    return (await session.execute(stmt)).scalars().first()


# --- runbooks (W5) -----------------------------------------------------------


async def sync_runbooks(session: AsyncSession, parsed: list[Any]) -> dict[str, int]:
    """Upsert the parsed runbooks and return ``name -> id``.

    The file in git is the source of truth; this table exists so that
    ``incidents.runbook_id`` and ``runbook_step_signals.runbook_id`` have
    something to reference. So the upsert overwrites everything except the
    primary key -- an edit to the markdown is meant to win, and a row that had
    drifted from the file would make D4's efficacy numbers describe a runbook
    nobody is actually reading.

    ``step_ids`` is written from the parsed markers rather than maintained by
    hand, which is what keeps ``Q5``'s ``unnest(r.step_ids)`` and the detector's
    signals talking about the same set of steps.
    """
    ids: dict[str, int] = {}
    for runbook in parsed:
        stmt = (
            pg_insert(Runbook)
            .values(
                name=runbook.name,
                alert_pattern=runbook.alert_pattern,
                severity_filter=runbook.severity_filter,
                service_filter=runbook.service_filter,
                body=runbook.body,
                step_ids=list(runbook.step_ids),
                version=runbook.version,
                git_sha=runbook.git_sha,
                is_active=True,
            )
            .on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "alert_pattern": runbook.alert_pattern,
                    "severity_filter": runbook.severity_filter,
                    "service_filter": runbook.service_filter,
                    "body": runbook.body,
                    "step_ids": list(runbook.step_ids),
                    "version": runbook.version,
                    "git_sha": runbook.git_sha,
                    "is_active": True,
                    "updated_at": text("now()"),
                },
            )
            .returning(Runbook.id)
        )
        ids[runbook.name] = int((await session.execute(stmt)).scalar_one())
    return ids


async def set_incident_runbook(session: AsyncSession, incident_id: int, runbook_id: int) -> None:
    """Record which runbook this incident was given.

    ``Q4`` and ``Q5`` both join through this column, so an incident that was
    pinned a runbook but never recorded it is an incident that contributes
    nothing to efficacy -- invisible in exactly the same way a runbook nobody
    follows is.
    """
    await session.execute(
        update(Incident).where(Incident.id == incident_id).values(runbook_id=runbook_id)
    )


async def record_correlation_feedback(
    session: AsyncSession,
    *,
    incident_id: int,
    alert_id: int | None,
    action: str,
    actor: str,
    original_score: float | None,
    original_reasons: list[Any] | None,
) -> None:
    """The labelled data that tunes ``merge_threshold`` (D3, W5-16).

    The original score and reasons are stored, not just the correction. "A human
    disagreed" is a fact; "a human disagreed with 0.71 on shared-service plus
    time-proximity" is a data point you can tune against, and the difference is
    whether the correction path is a feedback loop or a complaint box.
    """
    session.add(
        CorrelationFeedback(
            incident_id=incident_id,
            alert_id=alert_id,
            action=action,
            actor=actor,
            original_score=original_score,
            original_reasons=original_reasons or [],
        )
    )


async def alert_by_fingerprint(
    session: AsyncSession, incident_id: int, fingerprint: str
) -> Alert | None:
    stmt = (
        select(Alert)
        .where(Alert.incident_id == incident_id)
        .where(Alert.fingerprint == fingerprint)
        .order_by(Alert.starts_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def record_timeline_event(
    session: AsyncSession,
    *,
    incident_id: int,
    intent: str,
    description: str,
    author_user_id: str | None = None,
    confidence: float | None = None,
    source_message_ts: str | None = None,
    at: datetime | None = None,
) -> None:
    """An asserted timeline event, from a slash command rather than inference.

    ``confidence`` is 1.0 for these and below 1.0 for everything the intent
    ladder guessed. A PIR reading the timeline can therefore tell the difference
    between "a human typed this" and "a regex thought this" -- which is exactly
    the distinction a reader needs when a claim is contested.
    """
    values: dict[str, Any] = {
        "incident_id": incident_id,
        "intent": intent,
        "description": description[:2000],
        "author_user_id": author_user_id,
        "confidence": confidence,
        "source_message_ts": source_message_ts,
    }
    if at is not None:
        values["time"] = at
        await session.execute(pg_insert(TimelineEvent).values(**values))
        return
    # The hypertable partitions on `time`, so it cannot be left to a default
    # that does not exist. now() is read in SQL rather than in Python for the
    # same reason every other timestamp here is: one clock, and it is Postgres'.
    await session.execute(
        text(
            "INSERT INTO timeline_events"
            " (time, incident_id, intent, confidence, description, author_user_id,"
            "  source_message_ts)"
            " VALUES (now(), :incident_id, :intent, :confidence, :description,"
            "         :author_user_id, :source_message_ts)"
        ),
        values,
    )
