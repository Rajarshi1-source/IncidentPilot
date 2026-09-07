"""Shared plumbing for the seven slash commands (W5-15).

**The three-second rule, and why it is answered differently here than in week 4.**
Slack retries a slash command that does not respond within 3 seconds and shows
the user `operation_timeout`. So every handler acks immediately and lets the work
land afterwards.

That is the *opposite* of the decision in ``api/webhooks/slack.py``, where the
message is stored **before** the 200. The difference is who owns durability. For
an event, Slack's retry is our durability guarantee, so acking first would lose
data. For a slash command, the durable record is the row we write -- a state
transition, a feedback row, an outbox entry -- and the user's confirmation is
just a confirmation. Acking first costs nothing because nothing is lost if the
human never sees the ephemeral reply.

**Nothing here posts to `response_url`.** The plan's phrasing is "work async via
``response_url``", and the outbox is strictly better: a `response_url` POST is an
external write, so routing it through the relay (INV-03) means the visible result
survives a worker restart instead of vanishing with the request that triggered
it. The ephemeral ack is the HTTP response body -- no adapter, no API call, no
invariant to bend.

The body is `application/x-www-form-urlencoded`, and the signature is over those
**raw bytes**. Re-encoding the parsed form produces different bytes and fails
every check, exactly as it would for a JSON event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request, Response, status

from incidentpilot.api.security import Unauthorized, verify_slack
from incidentpilot.config.settings import Settings
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import WEBHOOK_REJECTED

log = get_logger(__name__)

SOURCE = "slack_slash"


@dataclass(frozen=True, slots=True)
class SlashRequest:
    command: str
    text: str
    user_id: str
    user_name: str
    channel_id: str
    channel_name: str
    response_url: str
    trigger_id: str

    @property
    def args(self) -> list[str]:
        return self.text.split()


def ephemeral(text: str) -> dict[str, Any]:
    """The ack. Only the invoking user sees it.

    Ephemeral rather than in-channel on purpose: the *result* of a command is
    posted in-channel by the relay, and echoing the request as well would double
    every action in a transcript that a PIR will later cite.
    """
    return {"response_type": "ephemeral", "text": text}


def in_channel(text: str) -> dict[str, Any]:
    return {"response_type": "in_channel", "text": text}


async def parse(request: Request, settings: Settings, response: Response) -> SlashRequest | None:
    """Verify the signature and parse the form. None means already rejected.

    Returning None with the status already set keeps each command handler to its
    own logic instead of repeating seven identical verification blocks -- and,
    more importantly, means a new command cannot be added *without* verification
    by forgetting to copy it.
    """
    raw = await request.body()
    secret = settings.slack_signing_secret

    try:
        verify_slack(
            raw,
            request.headers.get("x-slack-request-timestamp"),
            request.headers.get("x-slack-signature"),
            secret.get_secret_value() if secret else None,
            max_age_s=settings.signature_max_age_s,
        )
    except Unauthorized as exc:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason=exc.reason).inc()
        log.warning("slash.rejected", reason=exc.reason)
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return None

    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
    return SlashRequest(
        command=form.get("command", ""),
        text=form.get("text", "").strip(),
        user_id=form.get("user_id", ""),
        user_name=form.get("user_name", ""),
        channel_id=form.get("channel_id", ""),
        channel_name=form.get("channel_name", ""),
        response_url=form.get("response_url", ""),
        trigger_id=form.get("trigger_id", ""),
    )


async def incident_for(request: Request, channel_id: str) -> int | None:
    """Which incident this channel belongs to.

    The *same* ``ChannelCache`` instance the transcript ingestor uses, held on
    ``app.state``. One cache rather than two: a command typed in a war room must
    resolve from the identical mapping the messages did, and two caches would
    eventually disagree in a way nobody could reproduce. Like everything keyed
    on it, a miss falls through to Postgres (INV-04).
    """
    incident_id: int | None = await request.app.state.channels.incident_for_channel(channel_id)
    return incident_id
