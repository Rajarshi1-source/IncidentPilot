"""The Slack Events receiver (W4-01, W4-02).

Two decisions in this file are worth more than the code around them.

**The signature is computed over the raw bytes.** FastAPI will happily hand a
handler a parsed body, and re-serializing that dict produces different bytes --
different key order, different whitespace, no trailing newline -- and therefore
a different digest, so *every* request fails verification with a message that
says nothing useful. ``await request.body()`` first, parse second. The habit was
established in week 1 on the Alertmanager endpoint, which does not need it, so
that this endpoint, which does, inherits it (B-02 note, §8).

**The message is stored before the 200, not after.** The obvious shape for a
handler with a 3-second budget is to ack immediately and do the work in a
background task -- and it would be wrong here. Slack's retry *is* our durability
guarantee: a non-2xx gets the event redelivered, a 2xx does not. Acking first
and crashing a millisecond later loses that message permanently, silently, and
the loss shows up weeks later as a PIR citing a transcript with a hole in it.
Storing first means the worst case is a duplicate delivery, which
``UNIQUE (channel_id, ts)`` already turns into a no-op.

The write is a single indexed INSERT, so the 3-second budget is not close to at
risk. If it ever were, the fix is a durable queue in front of the write -- not a
200 issued before the data is safe.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, Request, Response, status

from incidentpilot.api.deps import IngestorDep, SettingsDep, elapsed_s
from incidentpilot.api.security import Unauthorized, verify_slack
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import WEBHOOK_LATENCY, WEBHOOK_REJECTED

router = APIRouter(tags=["webhooks"])
log = get_logger(__name__)

SOURCE = "slack"

URL_VERIFICATION = "url_verification"
EVENT_CALLBACK = "event_callback"


@router.post("/webhooks/slack/events", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    response: Response,
    settings: SettingsDep,
    ingestor: IngestorDep,
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_retry_num: str | None = Header(default=None),
    x_slack_retry_reason: str | None = Header(default=None),
) -> dict[str, Any]:
    """Accept one Slack Events API delivery.

    Returns 200 on anything we have durably handled, 401 on a failed signature,
    and 5xx on a storage failure -- the last one deliberately, so Slack
    redelivers rather than considering the message accepted.
    """
    raw = await request.body()
    secret = settings.slack_signing_secret

    try:
        verify_slack(
            raw,
            x_slack_request_timestamp,
            x_slack_signature,
            secret.get_secret_value() if secret else None,
            max_age_s=settings.signature_max_age_s,
        )
    except Unauthorized as exc:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason=exc.reason).inc()
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="unauthorized").observe(elapsed_s(request))
        log.warning("webhook.rejected", source=SOURCE, reason=exc.reason)
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"detail": "unauthorized"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="malformed_json").inc()
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "malformed json"}

    if not isinstance(payload, dict):
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="not_an_object").inc()
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "payload must be an object"}

    kind = str(payload.get("type") or "")

    # The one-time handshake when the Request URL is configured. Verified like
    # everything else first -- an unsigned challenge is an unauthenticated
    # stranger asking us to echo a string back.
    if kind == URL_VERIFICATION:
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
        return {"challenge": str(payload.get("challenge") or "")}

    if kind != EVENT_CALLBACK:
        # Rate-limit notices, app_uninstalled, tokens_revoked. Acknowledged so
        # Slack stops retrying; not processed, because nothing here handles them.
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
        log.info("slack.event_ignored", type=kind)
        return {"ok": True, "ignored": kind}

    event = payload.get("event")
    if not isinstance(event, dict):
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="no_event").inc()
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "event_callback has no event"}

    if x_slack_retry_num:
        # Not used for deduplication -- the (channel_id, ts) constraint does
        # that, and it keeps working across a restart, which a header cannot.
        # Logged because a rising retry count is the earliest visible sign that
        # this endpoint has started answering too slowly.
        log.info(
            "slack.retry_delivery",
            attempt=x_slack_retry_num,
            reason=x_slack_retry_reason,
            event_id=payload.get("event_id"),
        )

    try:
        result = await ingestor.on_event(event)
    except Exception:
        # Fail loudly with a 5xx so Slack redelivers. Swallowing this and
        # returning 200 would trade a retry for permanent data loss.
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="error").observe(elapsed_s(request))
        log.exception("slack.ingest_failed", event_id=payload.get("event_id"))
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"detail": "ingest failed"}

    WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
    return {"ok": True, "stored": result.stored, "reason": result.reason}
