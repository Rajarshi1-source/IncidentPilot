"""Read endpoints for the dashboard (W8-02, §13.6).

Two statement-timeout classes, and the split is the point:

* **5 s for reads** -- the incident list, an incident, a PIR. These sit in front
  of a responder during an incident. A read that takes longer than five seconds
  has already failed at its job; returning 503 lets the UI say so instead of
  holding a spinner over a war room.
* **30 s for analytics** -- the trend and rollup pages. Nobody is blocked on
  them, and a ninety-day percentile over a real dataset legitimately takes
  seconds.

**Separate pools per class, not just separate timeouts** (the bulkhead in the
data layer). A slow analytics scan cannot exhaust the pool the incident list
needs, which is the specific way a dashboard takes down the thing it is
monitoring.

Cache headers are set here rather than left to the client. The dashboard's
Server Components honour them, and putting the policy next to the query means
the answer to "how stale can this be?" lives with the thing that knows.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import DBAPIError

from incidentpilot.db import queries
from incidentpilot.telemetry.logging import get_logger

router = APIRouter(prefix="/api", tags=["query"])
log = get_logger(__name__)

# Seconds a browser may reuse a response. Active incidents are never cached:
# a stale banner during an incident is worse than a slow one.
CACHE_NONE = "no-store"
CACHE_SHORT = "public, max-age=15, stale-while-revalidate=30"
CACHE_ANALYTICS = "public, max-age=300, stale-while-revalidate=600"


def get_read_sessions(request: Request) -> Any:
    """The 5-second pool."""
    return request.app.state.read_sessions


def get_analytics_sessions(request: Request) -> Any:
    """The 30-second pool."""
    return request.app.state.analytics_sessions


ReadDep = Annotated[Any, Depends(get_read_sessions)]
AnalyticsDep = Annotated[Any, Depends(get_analytics_sessions)]


async def _run(factory: Any, call: Any, response: Response, cache: str) -> Any:
    """Run one query, turning a timeout into a 503 rather than a 500.

    The distinction is not pedantry. A 500 says "this endpoint is broken and
    will stay broken"; a 503 says "try again", which is what a statement timeout
    actually means and what lets the dashboard show *metrics unavailable for
    this window* -- a real backend state the UI is required to render rather
    than papering over with a zero.
    """
    try:
        async with factory() as session:
            result = await call(session)
    except DBAPIError as exc:
        if _is_timeout(exc):
            log.warning("query.timeout", error=str(exc)[:200])
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="query exceeded its statement timeout",
            ) from exc
        raise
    response.headers["Cache-Control"] = cache
    return result


def _is_timeout(exc: DBAPIError) -> bool:
    # Postgres 57014, query_canceled. Matched on the SQLSTATE rather than on the
    # message text, which is localised and changes between versions.
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return str(sqlstate) == "57014"


# --- incident reads (5 s pool) ------------------------------------------------


@router.get("/incidents/active")
async def active_incidents(sessions: ReadDep, response: Response) -> dict[str, Any]:
    """Q1. The banner. Never cached."""
    rows = await _run(sessions, queries.active_incidents, response, CACHE_NONE)
    return {
        "incidents": [
            {
                "public_key": r.public_key,
                "title": r.title,
                "severity": r.severity,
                "state": r.state,
                "service": r.service,
                # Both: the elapsed figure for a server-rendered first paint,
                # and the start instant so the client timer ticks from a
                # server-supplied timestamp rather than from clock arithmetic
                # against a rendered string.
                "detected_at": r.detected_at.isoformat(),
                "elapsed_min": round(r.elapsed_min, 1),
                "correlated_alert_count": r.correlated_alert_count,
                "chat_channel_id": r.chat_channel_id,
            }
            for r in rows
        ]
    }


@router.get("/incidents/{incident_id}/similar")
async def similar_incidents(
    incident_id: int,
    sessions: ReadDep,
    response: Response,
    threshold: Annotated[float, Query(ge=0.0, le=1.0)] = 0.85,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> dict[str, Any]:
    """Q6, and D8's in-channel sentence rendered as a panel.

    An incident with no embedding returns an empty list, not a 404. Embeddings
    are computed after resolution, so "not yet similar to anything" is the
    normal state for the incident someone is looking at right now.
    """
    rows = await _run(
        sessions,
        lambda s: queries.repeat_incidents(s, incident_id, threshold=threshold, limit=limit),
        response,
        CACHE_SHORT,
    )
    return {"similar": [_jsonable(r) for r in rows]}


@router.get("/transcript/completeness")
async def transcript_completeness(sessions: ReadDep, response: Response) -> dict[str, Any]:
    """Q12. Stored counts per active incident.

    Returns a count, never a ratio. The denominator -- what Slack holds -- needs
    a history call we are rate-limited out of (ADR 0003), and printing a
    fraction here would imply a denominator we do not have.
    """
    rows = await _run(sessions, queries.transcript_completeness, response, CACHE_SHORT)
    return {"incidents": [_jsonable(r) for r in rows]}


# --- analytics (30 s pool) ----------------------------------------------------


@router.get("/analytics/mttr")
async def mttr(
    sessions: AnalyticsDep,
    response: Response,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """Q2. TTM is a separate column from MTTR and stays that way in the payload."""
    rows = await _run(
        sessions, lambda s: queries.mttr_by_service(s, days=days), response, CACHE_ANALYTICS
    )
    return {"days": days, "services": [_jsonable(r) for r in rows]}


@router.get("/analytics/compression")
async def compression(
    sessions: AnalyticsDep,
    response: Response,
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> dict[str, Any]:
    """Q3. The D3 proof, weekly."""
    rows = await _run(
        sessions, lambda s: queries.storm_compression(s, days=days), response, CACHE_ANALYTICS
    )
    return {"days": days, "weeks": [_jsonable(r) for r in rows]}


@router.get("/analytics/toil")
async def toil(
    sessions: AnalyticsDep,
    response: Response,
    days: Annotated[int, Query(ge=1, le=365)] = 7,
) -> dict[str, Any]:
    """Q8. Elapsed incident hours, not engineer-hours -- the field name says so."""
    rows = await _run(
        sessions, lambda s: queries.toil_by_service(s, days=days), response, CACHE_ANALYTICS
    )
    return {"days": days, "services": [_jsonable(r) for r in rows]}


@router.get("/analytics/actions")
async def actions(sessions: AnalyticsDep, response: Response) -> dict[str, Any]:
    """Q10. Half-life by priority, and the oldest thing still open."""
    rows = await _run(sessions, queries.action_half_life, response, CACHE_ANALYTICS)
    return {"priorities": [_jsonable(r) for r in rows]}


@router.get("/analytics/runbooks")
async def runbooks(
    sessions: AnalyticsDep,
    response: Response,
    min_uses: Annotated[int, Query(ge=1, le=100)] = 3,
) -> dict[str, Any]:
    """Q4 and Q5 together: efficacy, and the steps nobody follows.

    Both in one response because they are read together -- a runbook's adherence
    number is not actionable until you can see which step is dragging it down.
    ``dead_steps`` will be empty until a runbook has five uses (E-3), and the
    payload says so rather than letting an empty list read as "no dead steps".
    """
    efficacy = await _run(
        sessions,
        lambda s: queries.runbook_efficacy(s, min_uses=min_uses),
        response,
        CACHE_ANALYTICS,
    )
    dead = await _run(sessions, queries.dead_steps, response, CACHE_ANALYTICS)
    return {
        "efficacy": [_jsonable(r) for r in efficacy],
        "dead_steps": [_jsonable(r) for r in dead],
        "dead_step_min_uses": queries.DEAD_STEP_MIN_USES,
        "note": (
            "dead_steps needs "
            f"{queries.DEAD_STEP_MIN_USES} uses per runbook before it reports anything; "
            "an empty list means not enough data, not a clean bill of health (E-3)"
        ),
    }


# --- the operator's two pages -------------------------------------------------


@router.get("/ops/grounding")
async def grounding(
    sessions: AnalyticsDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """Q9. The SLI with a zero error budget, per day.

    Fully-grounded over published is the number that must read 1.000. Skeletons
    are reported alongside rather than folded in: a day of provider outages is a
    day the feature degraded, not a day grounding failed, and collapsing the two
    would make the flagship metric unreadable on exactly the days it matters.
    """
    rows = await _run(
        sessions, lambda s: queries.grounding_compliance(s, limit=limit), response, CACHE_ANALYTICS
    )
    return {"days": [_jsonable(r) for r in rows]}


@router.get("/ops/models")
async def models(
    sessions: AnalyticsDep,
    response: Response,
    days: Annotated[int, Query(ge=1, le=365)] = 60,
) -> dict[str, Any]:
    """Q11 plus the deployment ledger. The MLOps governance story in one payload.

    Cost is grouped by prompt version *and* model, because the E-1 question is
    "did the same prompt get cheaper on a different model" and a rollup that
    collapses the model cannot answer it.
    """
    cost = await _run(
        sessions, lambda s: queries.cost_per_pir(s, days=days), response, CACHE_ANALYTICS
    )
    deployments = await _run(sessions, _deployments, response, CACHE_ANALYTICS)
    return {"days": days, "cost": [_jsonable(r) for r in cost], "deployments": deployments}


async def _deployments(session: Any) -> list[dict[str, Any]]:
    """The model_deployments ledger (migration 0008).

    Answers "which configuration wrote this PIR" for the whole history rather
    than one row at a time, which is what makes a promotion auditable.
    """
    from sqlalchemy import text as sql

    rows = (
        await session.execute(
            sql(
                """
                SELECT role, provider, model, state, traffic_pct, prompt_version,
                       eval_run_id, promoted_by, started_at, ended_at
                  FROM model_deployments
                 ORDER BY started_at DESC
                 LIMIT 50
                """
            )
        )
    ).mappings()
    return [_jsonable(r) for r in rows]


def _jsonable(row: Any) -> dict[str, Any]:
    """Rows out of the database contain Decimal and datetime; JSON does not.

    Decimals become floats deliberately rather than strings. These are all
    aggregate statistics being fed to a chart, where a string is useless; the
    places where exactness matters -- money in ``llm_calls``, timings in
    ``incidents`` -- are stored as their proper types and are not routed
    through here.
    """
    from datetime import date, datetime
    from decimal import Decimal

    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, datetime | date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out
