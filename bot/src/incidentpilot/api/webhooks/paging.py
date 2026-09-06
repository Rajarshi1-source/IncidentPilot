"""Paging-provider webhook receiver.

Unlike Alertmanager, the paging provider *can* prove who it is -- it signs with
HMAC-SHA256. The subtlety is that it may send several signatures at once during
key rotation, and accepting only the first turns a routine secret rotation into
a hard outage of on-call callbacks.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, Request, Response, status

from incidentpilot.api.deps import SettingsDep, StreamsDep, elapsed_s
from incidentpilot.api.security import Unauthorized, verify_paging
from incidentpilot.domain.normalize import MalformedPayload, normalize_paging
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import (
    ALERTS_ACCEPTED,
    WEBHOOK_LATENCY,
    WEBHOOK_REJECTED,
)

router = APIRouter(tags=["webhooks"])
log = get_logger(__name__)

SOURCE = "paging"


@router.post("/webhooks/paging", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    response: Response,
    settings: SettingsDep,
    streams: StreamsDep,
    x_signature: str | None = Header(default=None, alias="X-Paging-Signature"),
) -> dict[str, object]:
    """Accept a paging-provider incident webhook.

    The signature is computed over the raw request body, so the body is read as
    bytes and parsed here. Verification happens before parsing: an unverified
    payload should never reach a parser.
    """
    raw = await request.body()

    secret = settings.paging_webhook_secret
    try:
        verify_paging(raw, x_signature, secret.get_secret_value() if secret else None)
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

    try:
        alert = normalize_paging(payload)
    except MalformedPayload as exc:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="malformed_event").inc()
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="bad_request").observe(elapsed_s(request))
        log.warning("webhook.event_rejected", source=SOURCE, error=str(exc))
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": str(exc)}

    await streams.publish(alert)
    ALERTS_ACCEPTED.labels(source=SOURCE, status=str(alert.status)).inc()
    WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
    log.info("webhook.accepted", source=SOURCE, dedup_key=alert.dedup_key)
    return {"accepted": 1}
