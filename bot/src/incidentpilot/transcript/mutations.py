"""Edits and deletes, appended -- never applied in place (W4-06, B-01).

The rule this module exists to enforce is one sentence long: **a PIR cites
``msg:{ts}``, so that message must never change underneath the citation.**

Slack delivers an edit as a ``message_changed`` event carrying both the new
``message`` and the ``previous_message``, and a delete as ``message_deleted``
carrying only the ``deleted_ts``. The obvious implementation -- ``UPDATE
slack_messages SET text = ...`` -- would quietly rewrite evidence, and the
rewrite would be undetectable afterwards: the document would say one thing, the
linked message another, and nothing would record that they had ever agreed.

So the original row is immutable and each mutation becomes a
``slack_message_revisions`` row. Rendering a transcript means reading the
original plus its revisions; auditing one means reading the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import unit_of_work
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import MESSAGES_STORED

log = get_logger(__name__)

EDITED = "edited"
DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class RevisionResult:
    """What a mutation event produced. ``revision is None`` means nothing changed."""

    kind: str
    channel_id: str
    ts: str
    revision: int | None

    @property
    def appended(self) -> bool:
        return self.revision is not None


class MessageMutations:
    """Turns ``message_changed`` / ``message_deleted`` into appended revisions."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def on_message_changed(self, event: dict[str, Any]) -> RevisionResult | None:
        """Record an edit.

        The edited body lives in ``event["message"]``, not at the top level, and
        the *edited message's* ts lives there too -- the top-level ``ts`` on a
        ``message_changed`` event is the timestamp of the change itself, not of
        the message being changed. Reading the wrong one files every edit
        against a message that does not exist, and ``append_revision`` would
        return None forever without complaining.
        """
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        channel_id = str(event.get("channel") or "")
        ts = str(message.get("ts") or "")
        if not channel_id or not ts:
            return None

        return await self._append(channel_id, ts, EDITED, str(message.get("text") or ""))

    async def on_message_deleted(self, event: dict[str, Any]) -> RevisionResult | None:
        """Record a delete.

        The text is stored as None rather than as the previous body: the point
        of the revision is that the message was withdrawn, and duplicating the
        text into a "deleted" row would recreate exactly the content someone
        just asked to remove. The original row still holds it, which is a
        deliberate, documented retention decision (§16) rather than an accident.
        """
        channel_id = str(event.get("channel") or "")
        ts = str(event.get("deleted_ts") or event.get("previous_message", {}).get("ts") or "")
        if not channel_id or not ts:
            return None

        return await self._append(channel_id, ts, DELETED, None)

    async def _append(
        self, channel_id: str, ts: str, kind: str, text_body: str | None
    ) -> RevisionResult:
        async with unit_of_work(self._sessions) as session:
            revision = await repo.append_revision(
                session, channel_id=channel_id, ts=ts, kind=kind, text_body=text_body
            )

        if revision is not None:
            MESSAGES_STORED.labels(kind="revision").inc()
            log.info("transcript.revision", channel_id=channel_id, ts=ts, kind=kind, rev=revision)
        else:
            # Either a redelivery of this exact revision, or an edit to a
            # message that predates the bot joining the channel. Both are
            # expected; neither is an error worth a warning during an incident.
            log.debug("transcript.revision_skipped", channel_id=channel_id, ts=ts, kind=kind)

        return RevisionResult(kind=kind, channel_id=channel_id, ts=ts, revision=revision)
