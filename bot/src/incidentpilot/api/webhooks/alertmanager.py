"""Alertmanager webhook receiver.

Nothing slow happens before the 202. The contract is "I have durably accepted
this", and that is all -- correlation, channel creation and paging all happen in
the worker, off the request path. Alertmanager's HTTP timeout is not our
orchestration budget.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, Request, Response, status

from incidentpilot.api.deps import SettingsDep, StreamsDep, elapsed_s
from incidentpilot.api.security import Unauthorized, verify_bearer
from incidentpilot.domain.normalize import MalformedPayload, normalize_alertmanager
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import (
    ALERTS_ACCEPTED,
    WEBHOOK_LATENCY,
    WEBHOOK_REJECTED,
)

router = APIRouter(tags=["webhooks"])
log = get_logger(__name__)

SOURCE = "alertmanager"


@router.post("/webhooks/alertmanager", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    response: Response,
    settings: SettingsDep,
    streams: StreamsDep,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Accept an Alertmanager v4 payload.

    B-02: Alertmanager does NOT sign its payloads. There is no HMAC header to
    validate -- ``http_config`` offers basic auth, bearer tokens, OAuth2 and TLS
    and nothing else. The trust model is bearer + mTLS + NetworkPolicy
    allowlist, and it is documented here rather than left implicit because
    claiming signature validation on this endpoint is an instant credibility
    loss with anyone who has configured Alertmanager.
    """
    bearer = settings.alertmanager_bearer
    try:
        verify_bearer(authorization, bearer.get_secret_value() if bearer else None)
    except Unauthorized as exc:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason=exc.reason).inc()
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="unauthorized").observe(elapsed_s(request))
        log.warning("webhook.rejected", source=SOURCE, reason=exc.reason)
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"detail": "unauthorized"}

    # Raw bytes, parsed here rather than by FastAPI. This endpoint does not need
    # the raw body for an HMAC, but the Slack receiver in week 4 does -- and
    # establishing the habit in week 1 means week 4 inherits it instead of
    # discovering that a re-serialized body fails every signature check.
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="malformed_json").inc()
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="bad_request").observe(elapsed_s(request))
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "malformed json"}

    if not isinstance(payload, dict):
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="not_an_object").inc()
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "payload must be an object"}

    entries = payload.get("alerts")
    if not isinstance(entries, list):
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="no_alerts").inc()
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "payload has no alerts array"}

    normalized = []
    rejected = 0
    for entry in entries:
        try:
            normalized.append(normalize_alertmanager(entry, payload))
        except MalformedPayload as exc:
            # One bad alert in a batch of forty must not reject the other
            # thirty-nine. Count it and carry on; a storm is exactly when
            # partial acceptance matters most.
            rejected += 1
            WEBHOOK_REJECTED.labels(source=SOURCE, reason="malformed_alert").inc()
            log.warning("webhook.alert_rejected", source=SOURCE, error=str(exc))

    if normalized:
        await streams.publish_many(normalized)
        for alert in normalized:
            ALERTS_ACCEPTED.labels(source=SOURCE, status=str(alert.status)).inc()

    WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
    log.info(
        "webhook.accepted",
        source=SOURCE,
        accepted=len(normalized),
        rejected=rejected,
        group_key=payload.get("groupKey"),
    )
    return {"accepted": len(normalized), "rejected": rejected}
