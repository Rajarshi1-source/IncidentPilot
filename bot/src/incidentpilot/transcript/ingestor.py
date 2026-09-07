"""Every message is persisted AS IT ARRIVES. We never read history back (B-01).

This module is the architectural claim the project is built on, so the reasoning
is worth stating in full rather than pointing at a ticket number.

Since **3 March 2026** Slack limits ``conversations.history`` and
``conversations.replies`` to **1 request per minute, 15 messages per request**
for any app not approved for the Marketplace. Reading a 150-message incident at
resolve time therefore takes **ten minutes**, and the failure mode is *silent*:
no error, no 429 anyone notices -- the app simply knows less than it used to and
the PIR is built on a transcript with holes in it. ``conversations.replies`` on
a public channel additionally needs a **user** token; bot tokens only work in
DMs, which is a second, independent reason not to build on it.

So the read path is inverted. Slack pushes each message to us as it happens, we
store it, and the PIR pipeline reads our database. Slack is a write path, not a
read path -- one sentence that turns a rate-limit problem into an event-sourcing
decision.

Three properties make that safe:

* ``UNIQUE (channel_id, ts)`` converts Slack's at-least-once event delivery into
  exactly-once storage. The Events API redelivers on any non-2xx and on its own
  schedule; ``ON CONFLICT DO NOTHING`` plus a counter is the entire fix (W4-03).
* The derived ``timeline_events`` row is written in the **same transaction** as
  the message (W4-04). A crash between them would leave evidence with no index
  into it, and nothing would ever notice -- the message is there, the timeline
  is merely shorter than it should be.
* ``source_message_ts`` is carried onto every timeline row. It is the citation
  anchor: the reason a week 6 PIR claim can say ``msg:1757000000.000100`` and
  have that resolve to something a human can click. Treat it as required, not
  as a nicety -- losing it would not become visible until week 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import unit_of_work
from incidentpilot.db.models import TimelineEvent
from incidentpilot.domain.intent import SNAPSHOT_TRIGGERS, Intent, IntentKind
from incidentpilot.domain.normalize import MalformedPayload, slack_ts_to_dt
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import DUPLICATE_MESSAGES, MESSAGES_STORED
from incidentpilot.transcript.classifier import IntentClassifier, classify_with
from incidentpilot.transcript.mutations import MessageMutations, RevisionResult

log = get_logger(__name__)

SNAPSHOT_ACTION = "capture_metric_snapshot"

# Membership churn, topic edits, pins. Real events, zero evidentiary value, and
# storing them would dilute the transcript a PIR is grounded in.
IGNORED_SUBTYPES: frozenset[str] = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "bot_message",
        "message_replied",
        "pinned_item",
        "unpinned_item",
    }
)

CHANGED = "message_changed"
DELETED = "message_deleted"


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one event produced. Every field is something a test asserts on."""

    stored: bool
    reason: str
    incident_id: int | None = None
    message_id: int | None = None
    intent: Intent | None = None
    snapshot_enqueued: bool = False
    revision: RevisionResult | None = None


class TranscriptIngestor:
    """Turns Slack ``message`` events into the incident's system of record.

    The ingestor owns no clock and no HTTP client. It is handed a session
    factory, a channel cache and (optionally) the intent classifier, which is
    what lets the G4 gate drive 200 events through it with nothing running but
    Postgres.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        channels: repo.ChannelCache,
        *,
        classifier: IntentClassifier | None = None,
        mutations: MessageMutations | None = None,
    ) -> None:
        self._sessions = sessions
        self._channels = channels
        self._classifier = classifier
        self._mutations = mutations or MessageMutations(sessions)

    # -- entry point -----------------------------------------------------

    async def on_event(self, event: dict[str, Any]) -> IngestResult:
        """Route one Slack event. The single door both receivers come through.

        Socket Mode (dev) and the HTTP receiver (prod) differ only in transport;
        routing here rather than in either receiver is what stops the two drifting
        into different behaviour, which is the classic way a dev-only bug ships.
        """
        subtype = str(event.get("subtype") or "")

        if subtype == CHANGED:
            revision = await self._mutations.on_message_changed(event)
            return IngestResult(stored=False, reason="edit", revision=revision)
        if subtype == DELETED:
            revision = await self._mutations.on_message_deleted(event)
            return IngestResult(stored=False, reason="delete", revision=revision)

        return await self.on_message(event)

    # -- the hot path ----------------------------------------------------

    async def on_message(self, event: dict[str, Any]) -> IngestResult:
        if event.get("bot_id"):
            # Our own war-room posts. Storing them would make the bot cite
            # itself, and a PIR that quotes its own banner as evidence is a
            # closed loop with no human in it.
            return IngestResult(stored=False, reason="bot_message")
        if str(event.get("subtype") or "") in IGNORED_SUBTYPES:
            return IngestResult(stored=False, reason="ignored_subtype")

        channel_id = str(event.get("channel") or "")
        ts = str(event.get("ts") or "")
        if not channel_id or not ts:
            return IngestResult(stored=False, reason="malformed_event")

        incident_id = await self._channels.incident_for_channel(channel_id)
        if incident_id is None:
            return IngestResult(stored=False, reason="not_an_incident_channel")

        try:
            occurred_at = slack_ts_to_dt(ts)
        except MalformedPayload:
            log.warning("transcript.bad_ts", channel_id=channel_id, ts=ts)
            return IngestResult(stored=False, reason="malformed_ts")

        text_body = str(event.get("text") or "")

        # Layer 1 is pure and free; layer 2 is local and deterministic in week 4.
        # Both run before the transaction opens so the write is as short as
        # possible -- and so that when week 6 swaps in a network-backed embedder
        # (W6-14, batched over NOISE), no rewrite is needed to keep an HTTP call
        # out of an open transaction.
        intent = await classify_with(self._classifier, text_body)

        async with unit_of_work(self._sessions) as session:
            message_id = await repo.store_message(
                session,
                incident_id=incident_id,
                channel_id=channel_id,
                ts=ts,
                text_body=text_body,
                thread_ts=event.get("thread_ts"),
                user_id=event.get("user"),
                raw=event,
            )
            if message_id is None:
                # Redelivery. The constraint rejected it, which is the design
                # working -- counted rather than logged as an error, because
                # under retry pressure this is the common case, not the odd one.
                DUPLICATE_MESSAGES.inc()
                return IngestResult(
                    stored=False, reason="duplicate", incident_id=incident_id, intent=intent
                )

            snapshot = False
            if intent.kind is not IntentKind.NOISE:
                await session.execute(
                    pg_insert(TimelineEvent).values(
                        time=occurred_at,
                        incident_id=incident_id,
                        intent=str(intent.kind),
                        confidence=intent.confidence,
                        description=intent.summary,
                        author_user_id=event.get("user"),
                        source_message_ts=ts,  # the citation anchor (D1)
                    )
                )
                if intent.kind in SNAPSHOT_TRIGGERS:
                    snapshot = await self._enqueue_snapshot(session, incident_id, intent, ts)

        MESSAGES_STORED.labels(kind="message").inc()
        log.info(
            "transcript.stored",
            incident_id=incident_id,
            channel_id=channel_id,
            ts=ts,
            intent=str(intent.kind),
            snapshot=snapshot,
        )
        return IngestResult(
            stored=True,
            reason="stored",
            incident_id=incident_id,
            message_id=message_id,
            intent=intent,
            snapshot_enqueued=snapshot,
        )

    async def _enqueue_snapshot(
        self, session: AsyncSession, incident_id: int, intent: Intent, ts: str
    ) -> bool:
        """Ask the relay for a metric snapshot at this moment (W4-05).

        The idempotency key is derived from the message ts, so a redelivered
        event that somehow got past the message constraint still cannot produce
        a second snapshot. It is enqueued in the same transaction as the message
        that triggered it: the alternative -- calling Prometheus from here --
        would put a network round trip inside the ingest path and make a slow
        metrics backend into dropped transcript.
        """
        return await repo.enqueue_outbox(
            session,
            incident_id,
            SNAPSHOT_ACTION,
            payload={"reason": str(intent.kind), "at": ts},
            discriminator=ts,
        )
