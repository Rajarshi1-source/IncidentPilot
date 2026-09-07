"""Outbox action handlers -- the ONLY external writes in the system (INV-03).

Everything above this file writes *intent* to ``outbox_events``; this file is
where intent becomes an API call. Concentrating side effects in one module is
what makes idempotency tractable: there is exactly one place that can create a
Slack channel, so there is exactly one place that has to get the idempotency key
right.

``test_no_external_writes_outside_relay`` enforces the boundary structurally, so
a "temporary" direct call added elsewhere to see a channel appear fails the
build rather than quietly dissolving the guarantee.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.chat import block_kit
from incidentpilot.adapters.chat.base import ChatAdapter, channel_name
from incidentpilot.orchestration.outbox import idem_key
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import OUTBOX_DISPATCHED, TIME_TO_WAR_ROOM

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

LOAD_INCIDENT = text(
    """
    SELECT i.id, i.public_key, i.title, i.severity, i.state, i.root_signal,
           i.correlated_alert_count, i.chat_channel_id, i.chat_channel_name,
           i.detected_at, i.engaged_at, i.affected_services,
           s.name AS primary_service
      FROM incidents i
      LEFT JOIN services s ON s.id = i.primary_service_id
     WHERE i.id = :id
    """
)

RECORD_CHANNEL = text(
    """
    UPDATE incidents
       SET chat_channel_id = :channel_id, chat_channel_name = :channel_name
     WHERE id = :id AND chat_channel_id IS NULL
    """
)


def _elapsed_label(since: datetime | None, now: datetime) -> str:
    if since is None:
        return "0m"
    total = int((now - since).total_seconds())
    hours, remainder = divmod(max(total, 0), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


class ChatHandlers:
    """Builds the action -> handler map the relay dispatches through."""

    def __init__(
        self,
        chat: ChatAdapter,
        sessions: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat = chat
        self._sessions = sessions
        self._now = now or (lambda: datetime.now(UTC))

    def as_map(self) -> dict[str, Handler]:
        return {
            "create_channel": self.create_channel,
            "invite_responders": self.invite_responders,
            "pin_runbook": self.pin_runbook,
            "start_timer": self.start_timer,
            "post_merge_notice": self.post_merge_notice,
            "post_storm_update": self.post_storm_update,
            "archive_channel": self.archive_channel,
            "capture_metric_snapshot": self.capture_metric_snapshot,
        }

    # -- helpers ---------------------------------------------------------

    async def _incident(self, incident_id: int) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (await session.execute(LOAD_INCIDENT, {"id": incident_id})).mappings().first()
            return dict(row) if row else None

    # -- handlers --------------------------------------------------------

    async def create_channel(self, row: dict[str, Any]) -> dict[str, Any]:
        """The critical one, and the reason the idempotency key exists.

        The relay can die between Slack creating the channel and this function
        recording it (crash point 8) -- the only crash point where the external
        system has already been mutated and our record has not. On retry the
        same key returns the same channel, so the second attempt records what
        the first one created instead of making a duplicate.
        """
        incident_id = int(row["incident_id"])
        incident = await self._incident(incident_id)
        if incident is None:
            return {"skipped": "incident missing"}

        # Already recorded: a retry after the write succeeded. Not an error --
        # this is the normal shape of at-least-once delivery.
        if incident["chat_channel_id"]:
            return {"channel_id": incident["chat_channel_id"], "reused": True}

        name = channel_name(
            date=incident["detected_at"].date().isoformat(),
            service=incident["primary_service"],
            summary=incident["title"],
        )

        # The key is derived from the row, not generated: a generated key would
        # differ on every retry and defeat the whole mechanism.
        channel = await self._chat.create_channel(name, idempotency_key=row["idempotency_key"])

        async with self._sessions() as session, session.begin():
            await session.execute(
                RECORD_CHANNEL,
                {"id": incident_id, "channel_id": channel.id, "channel_name": channel.name},
            )

        if incident["detected_at"]:
            TIME_TO_WAR_ROOM.observe((self._now() - incident["detected_at"]).total_seconds())
        OUTBOX_DISPATCHED.labels(action="create_channel").inc()

        log.info(
            "channel.created",
            incident_id=incident_id,
            channel_id=channel.id,
            name=channel.name,
            reused=channel.already_existed,
        )
        return {"channel_id": channel.id, "name": channel.name, "reused": channel.already_existed}

    async def invite_responders(self, row: dict[str, Any]) -> dict[str, Any]:
        """Set semantics, so a half-finished invite is safe to replay whole.

        Crash point 10 is dying two responders into four. Slack treats
        re-inviting an existing member as ``already_in_channel`` rather than an
        error, so the retry simply completes the set.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            # No channel yet: the create row has not dispatched. Raising sends
            # this back to pending, and ordering resolves itself on retry.
            raise LookupError("channel not created yet")

        payload = row.get("payload") or {}
        user_ids = [str(u) for u in payload.get("user_ids", [])]
        if not user_ids:
            return {"invited": 0}

        await self._chat.invite(incident["chat_channel_id"], user_ids)
        OUTBOX_DISPATCHED.labels(action="invite_responders").inc()
        return {"invited": len(user_ids)}

    async def pin_runbook(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 0: the runbook is the war room's reason to exist."""
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        payload = row.get("payload") or {}
        blocks = block_kit.incident_header(
            public_key=incident["public_key"],
            title=incident["title"],
            severity=incident["severity"],
            root_signal=incident["root_signal"],
            correlated_alert_count=int(incident["correlated_alert_count"]),
            affected_services=len(incident["affected_services"] or []),
            elapsed_label=_elapsed_label(incident["detected_at"], self._now()),
            responders=[str(u) for u in payload.get("user_ids", [])],
            runbook_url=payload.get("runbook_url"),
        )

        posted = await self._chat.post_message(
            incident["chat_channel_id"],
            text=f"{incident['public_key']} — {incident['title']}",
            blocks=blocks,
            priority=0,
        )
        if posted is not None:
            await self._chat.pin(incident["chat_channel_id"], posted.ts)
        OUTBOX_DISPATCHED.labels(action="pin_runbook").inc()
        return {"ts": posted.ts if posted else None}

    async def start_timer(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 2. May be dropped entirely under pressure.

        Crash point 11 dies before this runs, and that is *acceptable* -- a
        stale timer is invisible, a delayed war room is an outage. Shedding here
        is a feature, not a gap.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        blocks = block_kit.timer(
            elapsed_label=_elapsed_label(incident["detected_at"], self._now()),
            state=str(incident["state"]),
        )
        await self._chat.post_message(
            incident["chat_channel_id"], text="timer", blocks=blocks, priority=2
        )
        OUTBOX_DISPATCHED.labels(action="start_timer").inc()
        return {"posted": True}

    async def post_merge_notice(self, row: dict[str, Any]) -> dict[str, Any]:
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        payload = row.get("payload") or {}
        blocks = block_kit.merge_notice(
            alertname=str(payload.get("alertname", "alert")),
            service=payload.get("service"),
            score=float(payload.get("score", 0.0)),
            reasons=[str(r) for r in payload.get("reasons", [])],
            fingerprint=str(payload.get("fingerprint", "")),
        )
        await self._chat.post_message(
            incident["chat_channel_id"], text="correlated alert", blocks=blocks, priority=1
        )
        OUTBOX_DISPATCHED.labels(action="post_merge_notice").inc()
        return {"posted": True}

    async def post_storm_update(self, row: dict[str, Any]) -> dict[str, Any]:
        """The D3 banner. Throttled by content upstream, one per five alerts."""
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        payload = row.get("payload") or {}
        blocks = block_kit.storm_notice(
            correlated_alert_count=int(payload.get("correlated_alert_count", 0)),
            affected_services=int(payload.get("services", 0)),
            root_signal=payload.get("root_signal"),
        )
        await self._chat.post_message(
            incident["chat_channel_id"], text="alert storm", blocks=blocks, priority=1
        )
        OUTBOX_DISPATCHED.labels(action="post_storm_update").inc()
        return {"posted": True}

    async def archive_channel(self, row: dict[str, Any]) -> dict[str, Any]:
        """Compensation for a failed engagement. Idempotent by construction.

        Crash point 12 dies mid-compensation. Archiving an archived channel is
        not an error, so the cleanup path cannot become its own source of
        failures -- which would be a genuinely absurd way to lose an incident.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            return {"skipped": "no channel to archive"}

        await self._chat.archive_channel(incident["chat_channel_id"])
        OUTBOX_DISPATCHED.labels(action="archive_channel").inc()
        log.info("channel.archived", incident_id=incident["id"])
        return {"archived": True}

    async def capture_metric_snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        """Freeze the metric window around a remediation or recovery moment.

        Enqueued by the transcript ingestor when a message crosses a
        ``SNAPSHOT_TRIGGERS`` intent (W4-05). The window it names is what week 6
        computes impact over, and it exists as an outbox row rather than as an
        inline Prometheus query for one reason: a slow metrics backend must
        never be able to turn into dropped transcript.

        The sampler itself is W6-03. Until then this records the request and
        succeeds, because the alternative -- no handler -- makes the relay mark
        every snapshot row dead on arrival (eight retries it can never win), and
        a dead-letter queue full of rows that were never wrong teaches people to
        stop reading it.
        """
        payload = row.get("payload") or {}
        OUTBOX_DISPATCHED.labels(action="capture_metric_snapshot").inc()
        log.info(
            "snapshot.deferred",
            incident_id=row["incident_id"],
            reason=payload.get("reason"),
            at=payload.get("at"),
        )
        return {"recorded": True, "sampler": "W6-03", "at": payload.get("at")}

    # -- called by the reconciler, not by the relay ----------------------

    async def archive_orphan_channel(self, channel_id: str) -> dict[str, Any]:
        """Archive a ``#inc-*`` channel that no incident row claims (W4-12).

        Not an outbox action, and the reason is structural rather than a
        shortcut: ``outbox_events.incident_id`` is NOT NULL, and an orphan is by
        definition a channel with no incident to hang the row off. So the write
        stays here -- inside the one module permitted to touch a chat adapter --
        and ``orchestration/reconciler.py`` calls it directly. INV-03's actual
        guarantee holds; what does not apply is the outbox's raced-writer
        protection, which an idempotent archive run by a single scheduled job
        does not need.
        """
        await self._chat.archive_channel(channel_id)
        OUTBOX_DISPATCHED.labels(action="archive_orphan_channel").inc()
        log.warning("channel.orphan_archived", channel_id=channel_id)
        return {"archived": channel_id}


def build_handlers(
    chat: ChatAdapter,
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Handler]:
    return ChatHandlers(chat, sessions, now=now).as_map()


__all__ = ["ChatHandlers", "Handler", "build_handlers", "idem_key"]
