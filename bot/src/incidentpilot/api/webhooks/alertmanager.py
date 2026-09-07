"""Alertmanager webhook receiver.

Nothing slow happens before the 202. The contract is "I have durably accepted
this", and that is all -- correlation, channel creation and paging all happen in
the worker, off the request path. Alertmanager's HTTP timeout is not our
orchestration budget.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, Request, Response, status

from incidentpilot.api.deps import DegradationDep, SettingsDep, StreamsDep, elapsed_s
from incidentpilot.api.security import Unauthorized, verify_bearer
from incidentpilot.domain.normalize import (
    MalformedPayload,
    NormalizedAlert,
    normalize_alertmanager,
)
from incidentpilot.resilience.degradation import Level
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import (
    ALERTS_ACCEPTED,
    BROWNOUT_BUFFERED,
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
    degradation: DegradationDep,
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

    buffered = 0
    if normalized:
        buffered = await _accept(normalized, streams, degradation)
        if buffered < 0:
            # Neither the primary stream nor the WAL took it. A 202 here would
            # be a lie -- the contract is "durably accepted" -- and a lie at
            # this boundary is unrecoverable, because Alertmanager will not
            # resend an alert it believes we have.
            WEBHOOK_REJECTED.labels(source=SOURCE, reason="stream_unavailable").inc()
            WEBHOOK_LATENCY.labels(source=SOURCE, outcome="unavailable").observe(elapsed_s(request))
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"detail": "alert stream unavailable"}
        for alert in normalized:
            ALERTS_ACCEPTED.labels(source=SOURCE, status=str(alert.status)).inc()

    WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
    log.info(
        "webhook.accepted",
        source=SOURCE,
        accepted=len(normalized),
        rejected=rejected,
        group_key=payload.get("groupKey"),
        buffered=buffered,
    )
    return {"accepted": len(normalized), "rejected": rejected, "buffered": buffered}


async def _accept(
    alerts: list[NormalizedAlert],
    streams: StreamsDep,
    degradation: DegradationDep,
) -> int:
    """Publish, or buffer to the write-ahead stream. Returns rows buffered, -1 if lost.

    The brownout path (W7-19, D7 L2). The reasoning is one asymmetry: **a
    dropped alert is unrecoverable, a delayed alert is not.** That is why ingest
    is the one AP component in an otherwise CP system, and why this falls
    forward into a buffer rather than back into a 500.

    The WAL is a different stream, not a retry of the same one. Retrying
    ``alerts.raw`` when ``alerts.raw`` is what failed is a loop; ``alerts.wal``
    is drained by ``replay_wal`` on recovery, in order, which is what makes the
    delay recoverable rather than merely tolerated.

    Note what does *not* happen here: no local disk spool. A file on the API
    pod's filesystem is not durable -- the pod is the thing most likely to be
    replaced during the outage that filled it -- and a buffer that loses data on
    reschedule is a buffer that lies about the 202 it licensed.
    """
    try:
        await streams.publish_many(alerts)
    except Exception as exc:
        log.warning("webhook.primary_stream_failed", source=SOURCE, error=str(exc)[:200])
        try:
            for alert in alerts:
                await streams.buffer_wal(alert)
        except Exception as wal_exc:
            log.error("webhook.wal_failed", source=SOURCE, error=str(wal_exc)[:200])
            return -1
        BROWNOUT_BUFFERED.inc(len(alerts))
        await degradation.set_level(Level.BROWNOUT, f"alert stream unavailable: {exc}")
        return len(alerts)
    return 0
