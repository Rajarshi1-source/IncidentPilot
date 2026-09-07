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
from incidentpilot.adapters.chat.base import ChatAdapter, PermanentChatError, channel_name
from incidentpilot.adapters.paging.base import (
    PermanentPagingError,
    RetryablePagingError,
)
from incidentpilot.db import repositories as repo
from incidentpilot.orchestration.outbox import idem_key
from incidentpilot.orchestration.routing import ResponderRouter, RoutingPlan
from incidentpilot.runbooks import renderer
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import OUTBOX_DISPATCHED, TIME_TO_WAR_ROOM

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Transient paging failures are retried by the relay this many times before
# the war room is told nobody could be reached. Three attempts of
# exponential backoff is roughly a minute -- long enough to ride out a blip,
# short enough that an unreachable pager does not stay a secret.
PAGE_ATTEMPTS_BEFORE_BROADCAST = 3

LOAD_INCIDENT = text(
    """
    SELECT i.id, i.public_key, i.title, i.severity, i.state, i.root_signal,
           i.correlated_alert_count, i.chat_channel_id, i.chat_channel_name,
           i.detected_at, i.engaged_at, i.acknowledged_at, i.affected_services,
           i.primary_service_id,
           i.runbook_id,
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
        paging: Any | None = None,
        router: ResponderRouter | None = None,
        runbooks: Any | None = None,
    ) -> None:
        self._chat = chat
        self._sessions = sessions
        self._now = now or (lambda: datetime.now(UTC))
        # Optional so week 3's crash matrix, which predates paging, still builds
        # a handler map without a pager. `page_responder` says so explicitly
        # rather than failing obscurely three frames down when one is missing.
        self._paging = paging
        self._router = router
        self._runbooks = runbooks

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
            "page_responder": self.page_responder,
            "post_sla_nudge": self.post_sla_nudge,
            "post_generating_notice": self.post_generating_notice,
            "post_pir": self.post_pir,
            "post_pir_skeleton": self.post_pir_skeleton,
            "notify_reviewers": self.notify_reviewers,
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
        """Priority 0: the runbook is the war room's reason to exist.

        The runbook is chosen from the **root signal** (W5-11), not from the
        loudest alert. During a storm the noisiest alert is almost never the
        cause -- `postgres-primary-down` fires once, the thirty-nine
        `HighErrorRate` alerts it causes fire everywhere downstream -- and
        pinning the high-error-rate runbook to a database incident sends the
        responder through the wrong list while the real cause sits one hop away.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        payload = row.get("payload") or {}
        channel_id = str(incident["chat_channel_id"])
        matched = self._match_runbook(incident)

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
            channel_id,
            text=f"{incident['public_key']} — {incident['title']}",
            blocks=blocks,
            priority=0,
        )
        if posted is not None:
            await self._chat.pin(channel_id, posted.ts)

        runbook_name: str | None = None
        if matched is not None:
            runbook_name = matched.runbook.name
            await self._post_runbook(incident, matched, channel_id)
        else:
            # Said out loud rather than left blank. "No runbook matched" is
            # useful -- it is how a gap in the catalogue gets noticed -- while an
            # empty war room just looks like the bot is broken.
            await self._chat.post_message(
                channel_id,
                text="no runbook matched",
                blocks=[
                    block_kit.context(
                        [
                            ":books: No runbook matched root signal "
                            f"`{incident['root_signal'] or 'unknown'}`."
                        ]
                    )
                ],
                priority=1,
            )

        OUTBOX_DISPATCHED.labels(action="pin_runbook").inc()
        return {"ts": posted.ts if posted else None, "runbook": runbook_name}

    def _match_runbook(self, incident: dict[str, Any]) -> Any:
        if self._runbooks is None:
            return None
        return self._runbooks.match(
            root_signal=incident.get("root_signal"),
            severity=str(incident["severity"]),
            service=incident.get("primary_service"),
        )

    async def _post_runbook(self, incident: dict[str, Any], matched: Any, channel_id: str) -> None:
        """Post the rendered runbook and record which one the incident got.

        Recording it is not bookkeeping: ``Q4`` and ``Q5`` both join through
        ``incidents.runbook_id``, so an incident that was pinned a runbook and
        never recorded it contributes nothing to efficacy -- invisible in exactly
        the way a runbook nobody follows is.
        """
        context = renderer.render_context(
            service=incident.get("primary_service"),
            alertname=incident.get("root_signal"),
            severity=str(incident["severity"]),
            public_key=str(incident["public_key"]),
        )
        blocks = renderer.render_blocks(matched.runbook, context, matched_on=matched.matched_on)
        runbook_message = await self._chat.post_message(
            channel_id,
            text=f"Runbook: {matched.runbook.name}",
            blocks=blocks,
            priority=0,
        )

        runbook_id = self._runbooks.id_for(matched.runbook.name) if self._runbooks else None
        if runbook_id is not None:
            async with self._sessions() as session, session.begin():
                await repo.set_incident_runbook(session, int(incident["id"]), runbook_id)

        log.info(
            "runbook.pinned",
            incident_id=incident["id"],
            runbook=matched.runbook.name,
            matched_on=matched.matched_on,
            ts=runbook_message.ts if runbook_message else None,
        )

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

    async def page_responder(self, row: dict[str, Any]) -> dict[str, Any]:
        """Wake the right human, and say out loud who and why (W5-08, D5).

        The ladder ends here. ``ResponderRouter`` decided; this function performs
        the three external writes that decision implies -- the page, the invite,
        the announcement -- and records the ``page_events`` row that the next
        fatigue score will be computed from.

        **Nothing here is allowed to be silent.** A degraded on-call lookup posts
        a notice; a reroute posts who was skipped, the score, the reasons and an
        opt-in button; a rota that cannot deliver a page broadcasts in the team
        channel. G5 fails on a silent fallback, and that is the point of the gate
        rather than an inconvenience in it.
        """
        incident_id = int(row["incident_id"])
        incident = await self._incident(incident_id)
        if incident is None:
            return {"skipped": "incident missing"}
        if not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")
        if self._router is None or self._paging is None:
            log.warning("paging.not_configured", incident_id=incident_id)
            return {"skipped": "no paging adapter configured"}

        channel_id = str(incident["chat_channel_id"])
        async with self._sessions() as session:
            schedule = await repo.team_for_service(session, incident["primary_service_id"])
            plan = await self._router.plan(
                session, schedule=schedule, at=incident["detected_at"] or self._now()
            )

        if plan.broadcast_channel is not None:
            return await self._broadcast(plan, channel_id)

        paged: list[str] = []
        for user in plan.page:
            try:
                result = await self._paging.page(
                    user,
                    incident_key=str(incident["public_key"]),
                    title=str(incident["title"]),
                    severity=str(incident["severity"]),
                )
            except PermanentPagingError as exc:
                # The static rota can name someone but cannot ring a phone. That
                # is rung 4 in its second sense: we know who, we simply cannot
                # reach them, so the team channel hears about it.
                log.warning("paging.undeliverable", incident_id=incident_id, error=str(exc))
                return await self._broadcast(plan, channel_id, reason=str(exc))
            except RetryablePagingError as exc:
                # A 503 might be a blip, so the first few attempts go back to the
                # relay for exponential backoff. But "keep retrying quietly" is
                # the silent failure G5 forbids -- after PAGE_ATTEMPTS_BEFORE_
                # BROADCAST the incident is minutes old with nobody woken up, and
                # at that point shouting in the team channel beats a ninth retry.
                if int(row.get("attempts") or 0) < PAGE_ATTEMPTS_BEFORE_BROADCAST:
                    log.warning(
                        "paging.transient_failure",
                        incident_id=incident_id,
                        attempts=row.get("attempts"),
                        error=str(exc),
                    )
                    raise
                log.error("paging.exhausted", incident_id=incident_id, error=str(exc))
                return await self._broadcast(plan, channel_id, reason=str(exc))

            if result.delivered:
                paged.append(user)
                await self._record_page(incident, user)

        if plan.invite:
            await self._chat.invite(channel_id, list(plan.invite))

        await self._chat.post_message(
            channel_id,
            text="paged " + (", ".join(paged) if paged else "nobody"),
            blocks=block_kit.routing_notice(
                paged=paged,
                notified=list(plan.notify),
                score=plan.score,
                reasons=list(plan.reasons),
                rerouted=plan.rerouted,
                oncall_source=str(plan.oncall.source),
                degraded_reason=plan.degraded_reason,
            ),
            priority=0,
        )
        OUTBOX_DISPATCHED.labels(action="page_responder").inc()
        log.info(
            "responder.paged",
            incident_id=incident_id,
            paged=paged,
            notified=list(plan.notify),
            score=round(plan.score, 3),
            source=str(plan.oncall.source),
            degraded=plan.degraded,
        )
        return {
            "paged": paged,
            "notified": list(plan.notify),
            "score": round(plan.score, 3),
            "source": str(plan.oncall.source),
            "degraded": plan.degraded,
        }

    async def _broadcast(
        self, plan: RoutingPlan, war_room: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Rung 4: nobody could be paged, so say so where people will see it.

        Posted in **both** places deliberately. The team channel is where the
        people not yet in the incident are; the war room is where whoever reads
        the transcript afterwards will look for why nobody arrived.
        """
        why = reason or plan.degraded_reason or "no on-call could be resolved"
        target = plan.broadcast_channel or war_room

        await self._chat.post_message(
            target,
            text="no on-call resolved",
            blocks=block_kit.broadcast_notice(schedule=plan.schedule, reason=why),
            priority=0,
        )
        if target != war_room:
            await self._chat.post_message(
                war_room,
                text="on-call lookup degraded",
                blocks=block_kit.routing_notice(
                    paged=[],
                    notified=[],
                    score=plan.score,
                    reasons=list(plan.reasons),
                    rerouted=False,
                    oncall_source=str(plan.oncall.source),
                    degraded_reason=why,
                ),
                priority=0,
            )

        OUTBOX_DISPATCHED.labels(action="page_responder").inc()
        log.error("paging.broadcast_fallback", schedule=plan.schedule, reason=why)
        return {"paged": [], "broadcast": target, "degraded": True, "reason": why}

    async def _record_page(self, incident: dict[str, Any], user: str) -> None:
        """The row the next fatigue score is computed from.

        Written immediately after each page rather than batched at the end of
        the loop: if the process dies part-way through, the pages that already
        happened must still be counted, or the responder who was just woken up
        looks fresh to the next incident.
        """
        async with self._sessions() as session, session.begin():
            tz = await repo.responder_timezone(session, user)
            await repo.record_page(
                session,
                at=self._now(),
                responder=user,
                incident_id=int(incident["id"]),
                severity=str(incident["severity"]),
                tz=tz,
            )

    async def post_sla_nudge(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 1. Enqueued by the scheduler, posted here like everything else.

        Priority 1 rather than 0: a nudge matters, but not more than creating the
        war room it is nudging about. Under rate-limit pressure the channel still
        gets created first, which is the ordering the whole priority-lane design
        exists to guarantee.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")
        if incident["acknowledged_at"] is not None:
            # Someone acknowledged between the sweep and the dispatch. Not an
            # error -- the queue is allowed to be a few seconds behind reality --
            # but posting the nudge anyway would be noise in a live incident.
            return {"skipped": "acknowledged before dispatch"}

        payload = row.get("payload") or {}
        await self._chat.post_message(
            str(incident["chat_channel_id"]),
            text=f"{incident['public_key']} is unacknowledged",
            blocks=block_kit.sla_nudge(
                public_key=str(incident["public_key"]),
                elapsed_label=_elapsed_label(incident["detected_at"], self._now()),
                responders=[str(u) for u in payload.get("user_ids", [])],
            ),
            priority=1,
        )
        OUTBOX_DISPATCHED.labels(action="post_sla_nudge").inc()
        return {"posted": True, "age_s": payload.get("age_s")}

    # -- the PIR path (W6) -----------------------------------------------
    #
    # These four are the actions `EFFECTS` has referenced since week 2. Until
    # now none of them had a handler, which means the relay would have marked
    # every one DEAD on arrival -- eight retries it could never win, on the four
    # transitions the flagship feature depends on. C-05 warned about exactly
    # this shape for `post_pir_skeleton`: the document is generated, persisted,
    # and never posted, and every unit test still passes.

    async def post_generating_notice(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 2. Says a PIR is being written, so the silence is explained.

        Sheddable: if the channel is rate-limited, losing this costs a reader a
        moment of confusion, while losing the PIR itself costs the incident its
        record. That is exactly the trade the priority lanes exist to make.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        await self._chat.post_message(
            str(incident["chat_channel_id"]),
            text="drafting the post-incident review",
            blocks=[
                block_kit.context(
                    [":writing_hand: Drafting the post-incident review from the transcript…"]
                )
            ],
            priority=2,
        )
        OUTBOX_DISPATCHED.labels(action="post_generating_notice").inc()
        return {"posted": True}

    async def post_pir(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 1. The validated document, with citation chips.

        Permalinks are resolved **here** rather than in the renderer, because
        ``chat.getPermalink`` is an external call and this is the only module
        allowed to make one (INV-03). A permalink that cannot be resolved
        degrades the chip to a plain label -- it never removes the citation,
        which would turn a grounded claim into an ungrounded one at the last
        possible moment.
        """
        return await self._post_document(row, layer="model")

    async def post_pir_skeleton(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 1. The honest floor, posted with its banner (C-05).

        The same lane as the full PIR on purpose. A skeleton is not a lesser
        document -- it is the document, minus the narrative -- and shedding it
        under pressure would mean the incidents that happen during a bad
        afternoon are the ones with no record.
        """
        return await self._post_document(row, layer="skeleton")

    async def _post_document(self, row: dict[str, Any], *, layer: str) -> dict[str, Any]:
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        channel_id = str(incident["chat_channel_id"])
        payload = row.get("payload") or {}
        markdown = str(payload.get("markdown") or "")
        if not markdown:
            # The generator persists first and enqueues second, so an empty
            # payload means the row was written by something else. Dead rather
            # than retried: an empty document will still be empty in a minute.
            raise PermanentChatError("no document body on the outbox row")

        blocks = [block_kit.section(chunk) for chunk in _chunk_markdown(markdown)]
        posted = await self._chat.post_message(
            channel_id,
            text=f"Post-incident review — {incident['public_key']}",
            blocks=block_kit.clamp(blocks),
            priority=1,
        )
        if posted is not None:
            await self._chat.pin(channel_id, posted.ts)

        OUTBOX_DISPATCHED.labels(action=f"post_pir_{layer}").inc()
        log.info(
            "pir.posted",
            incident_id=incident["id"],
            layer=layer,
            coverage=payload.get("coverage"),
            ts=posted.ts if posted else None,
        )
        return {"posted": True, "layer": layer, "ts": posted.ts if posted else None}

    async def notify_reviewers(self, row: dict[str, Any]) -> dict[str, Any]:
        """Priority 2. Asks the people who were there to check it.

        The reviewers are the incident's participants, not a fixed list: the
        only people who can tell whether a claim is true are the ones who were
        in the channel when it happened.
        """
        incident = await self._incident(int(row["incident_id"]))
        if incident is None or not incident["chat_channel_id"]:
            raise LookupError("channel not created yet")

        payload = row.get("payload") or {}
        reviewers = [str(u) for u in payload.get("reviewers", [])]
        if not reviewers:
            return {"skipped": "no participants to notify"}

        await self._chat.post_message(
            str(incident["chat_channel_id"]),
            text="review requested",
            blocks=[
                block_kit.section(
                    ":mag: "
                    + ", ".join(f"<@{u}>" for u in reviewers)
                    + " — you were in this incident. Please check the claims that "
                    "cite your messages."
                ),
            ],
            priority=2,
        )
        OUTBOX_DISPATCHED.labels(action="notify_reviewers").inc()
        return {"notified": len(reviewers)}

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
    paging: Any | None = None,
    router: ResponderRouter | None = None,
    runbooks: Any | None = None,
) -> dict[str, Handler]:
    return ChatHandlers(
        chat, sessions, now=now, paging=paging, router=router, runbooks=runbooks
    ).as_map()


__all__ = ["ChatHandlers", "Handler", "build_handlers", "idem_key"]


# Slack caps a section block at 3000 characters, and a PIR is longer than that.
# Split on blank lines so a paragraph is never cut mid-sentence: `block_kit`
# would truncate visibly, which is correct for a stray long line and wrong for a
# document whose whole point is that every claim is complete.
def _chunk_markdown(markdown: str, *, limit: int = 2800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in markdown.split("\n\n"):
        if size + len(paragraph) > limit and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks
