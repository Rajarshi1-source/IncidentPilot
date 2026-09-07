"""CI deploy events become citable evidence (W6-22, D1).

The sixth citation kind. "Deployed twelve minutes before detection" is the
single most useful sentence a postmortem can contain, and it is the claim a
model is most likely to make and least able to support -- so the deploys have to
be *stored*, not inferred from a commit message someone pasted in the channel.

**Trust model: bearer plus a repository allowlist.** CI systems do not sign
their notifications any more than Alertmanager does (B-02), so this endpoint
gets the same treatment as the Alertmanager one: a constant-time bearer check,
and then a check that the repository is one we expect. The allowlist is the part
that matters -- a leaked bearer with no allowlist lets anyone write deploy rows,
and a fabricated deploy row is worse than a fabricated citation because it makes
the citation *valid*.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy import text as sql

from incidentpilot.api.deps import SessionsDep, SettingsDep, elapsed_s
from incidentpilot.api.security import Unauthorized, verify_bearer
from incidentpilot.db.engine import unit_of_work
from incidentpilot.domain.evidence import deploy_ref
from incidentpilot.domain.normalize import parse_timestamp
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import WEBHOOK_LATENCY, WEBHOOK_REJECTED

router = APIRouter(tags=["webhooks"])
log = get_logger(__name__)

SOURCE = "deploy"

# ON CONFLICT DO NOTHING because CI retries a failed notification step, and a
# second row for one deploy would make the same event citable under two ids.
_INSERT = sql(
    """
    INSERT INTO deploy_events
        (sha, service, environment, repository, actor, title, url, deployed_at, raw)
    VALUES
        (:sha, :service, :environment, :repository, :actor, :title, :url,
         :deployed_at, CAST(:raw AS jsonb))
    ON CONFLICT ON CONSTRAINT uq_deploy_identity DO NOTHING
    RETURNING id
    """
)


@router.post("/webhooks/deploy", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    response: Response,
    settings: SettingsDep,
    sessions: SessionsDep,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Accept one deploy notification.

    202 rather than 201 even when a row is written: the contract is "durably
    accepted", and whether it was new or a retry is our business, not CI's. A
    404-shaped answer to a duplicate would make every CI retry look like a
    failure in someone's pipeline.
    """
    bearer = settings.deploy_bearer
    try:
        verify_bearer(authorization, bearer.get_secret_value() if bearer else None)
    except Unauthorized as exc:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason=exc.reason).inc()
        WEBHOOK_LATENCY.labels(source=SOURCE, outcome="unauthorized").observe(elapsed_s(request))
        log.warning("webhook.rejected", source=SOURCE, reason=exc.reason)
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"detail": "unauthorized"}

    raw = await request.body()
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

    sha = str(payload.get("sha") or payload.get("commit") or "").strip()
    service = str(payload.get("service") or "").strip()
    if not sha or not service:
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="missing_identity").inc()
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"detail": "sha and service are required"}

    repository = str(payload.get("repository") or "").strip() or None
    allowlist = settings.deploy_repo_allowlist
    if allowlist and (repository or "") not in allowlist:
        # The part that matters. A leaked bearer with no allowlist lets anyone
        # write deploy rows, and a fabricated deploy makes a *valid* citation --
        # which is worse than a fabricated one, because nothing catches it.
        WEBHOOK_REJECTED.labels(source=SOURCE, reason="repo_not_allowed").inc()
        log.warning("webhook.repo_rejected", source=SOURCE, repository=repository)
        response.status_code = status.HTTP_403_FORBIDDEN
        return {"detail": "repository not in the allowlist"}

    # Normalized here, once, through the evidence grammar's own rule -- a webhook
    # sends forty characters and a human writes seven, and two spellings would
    # make half the citations to one deploy look fabricated.
    normalized = deploy_ref(sha).split(":", 1)[1]
    deployed_at = parse_timestamp(payload.get("deployed_at")) or datetime.now(UTC)

    async with unit_of_work(sessions) as session:
        inserted = (
            await session.execute(
                _INSERT,
                {
                    "sha": normalized,
                    "service": service,
                    "environment": str(payload.get("environment") or "production"),
                    "repository": repository,
                    "actor": payload.get("actor"),
                    "title": payload.get("title"),
                    "url": payload.get("url"),
                    "deployed_at": deployed_at,
                    "raw": json.dumps(payload, default=str),
                },
            )
        ).scalar()

    WEBHOOK_LATENCY.labels(source=SOURCE, outcome="ok").observe(elapsed_s(request))
    log.info(
        "deploy.recorded",
        sha=normalized,
        service=service,
        repository=repository,
        stored=inserted is not None,
    )
    return {"accepted": True, "sha": normalized, "stored": inserted is not None}
