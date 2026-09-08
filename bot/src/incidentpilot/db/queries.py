"""The analytics queries, as functions (W5-07, W5-14).

Week 5 committed three of the twelve: ``Q7`` (the fatigue window, D5's input),
``Q4`` (runbook efficacy) and ``Q5`` (dead steps, D4's input). Week 8 adds the
remaining nine, which are the dashboard's entire data layer.

**The SQL lives here, never at the call site.** The API passes bound parameters
and gets rows back. A query assembled from request fragments is one nobody can
test in isolation, and it is an injection surface wearing a filter's clothes --
``test_all_12_queries_run`` can be written at all only because every query is a
named object with a signature.

Two of them exist here because the reference version does not work.

**X-01 — ``Q5`` as published does not run.** Three independent errors: it
expands ``r.step_ids`` twice (once in the select list, once in a
``CROSS JOIN LATERAL``) producing a cross product; the ``FILTER`` clause
references ``unnest_step``, a *different* expansion than the ``step`` it is
grouped by; and the outer query aliases ``runbooks`` as ``r`` while the subquery
already exposes ``r.name``, so ``r.name`` is ambiguous. Shipping it would mean
the runbook-efficacy job silently produces nothing -- and "silently produces
nothing" is precisely the failure D4 is supposed to detect in other people's
runbooks. ``test_dead_step_query_runs`` executes the corrected version against a
real database.

**Q7 as published computes two of the five terms the scorer needs.** The
reference returns ``pages_8h`` and ``night_pages``; ``fatigue_score`` also wants
``incident_minutes_24h``, ``consecutive_oncall_days`` and ``sev1_count_7d``.
The version here computes all five, and the window end is a **parameter** rather
than ``now()`` so the same recorded incident scores identically under replay
(INV-10). That parameter is also what keeps ``domain/fatigue.py`` pure (C-04):
every piece of time arithmetic happens in SQL, and the scorer receives numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.domain.fatigue import ResponderWindow

# Q7 — the responder fatigue window (D5).
#
# C-06: the time column on ``page_events`` is ``time``, not ``paged_at``. Rev 2's
# version of this query names ``p.paged_at`` and fails outright with "column
# does not exist"; the schema reference is the one that is right.
#
# Both ends of the window are closed. Without the upper bound, a page that
# happened *after* the reference instant would still be counted -- which is
# invisible in production, where `at` is always recent, and quietly wrong
# under replay, where the whole point is to score an incident as it was.
#
# `night` is evaluated per responder timezone: a page at 03:00 IST is a night
# page whatever UTC says, and `page_events.tz` exists for exactly this. Keeping
# the timezone arithmetic in SQL is what lets the scorer stay pure.
Q7_FATIGUE_WINDOW = text(
    """
    WITH bounds AS (
        -- Both parameters are CAST explicitly. Postgres cannot infer the
        -- type of a parameter that only ever appears as `$n IS NULL`, and
        -- the failure is an AmbiguousParameter error at execution time
        -- rather than anything visible while writing the query.
        SELECT CAST(:at AS timestamptz) AS at,
               CAST(:responder AS text) AS responder
    ),
    pages AS (
        SELECT p.responder,
               count(*) FILTER (WHERE p.time > b.at - INTERVAL '8 hours')  AS pages_8h,
               count(*) FILTER (
                   WHERE p.time > b.at - INTERVAL '24 hours'
                     AND extract(hour FROM p.time AT TIME ZONE p.tz) NOT BETWEEN 8 AND 22
               ) AS night_pages_24h,
               count(*) FILTER (
                   WHERE p.time > b.at - INTERVAL '7 days' AND p.severity = 'sev1'
               ) AS sev1_count_7d
          FROM page_events p CROSS JOIN bounds b
         WHERE p.time > b.at - INTERVAL '7 days' AND p.time <= b.at
           AND (b.responder IS NULL OR p.responder = b.responder)
         GROUP BY p.responder
    ),
    -- Time IN incidents, not merely paged for them. One page followed by four
    -- hours in a war room is the more exhausting shape, and a page-count-only
    -- score would read it as a quiet day.
    engaged AS (
        -- DISTINCT (responder, incident) before the join, so an incident that
        -- paged someone three times contributes its duration once. Summing per
        -- page would treat a noisy incident as three separate ones and inflate
        -- the term that is supposed to measure time, not volume.
        SELECT d.responder,
               COALESCE(SUM(EXTRACT(EPOCH FROM (
                   LEAST(COALESCE(i.resolved_at, b.at), b.at) - i.detected_at
               )) / 60.0), 0)::int AS incident_minutes_24h
          FROM (
              SELECT DISTINCT p.responder, p.incident_id
                FROM page_events p CROSS JOIN bounds b
               WHERE p.time > b.at - INTERVAL '24 hours' AND p.time <= b.at
                 AND p.incident_id IS NOT NULL
                 AND (b.responder IS NULL OR p.responder = b.responder)
          ) d
          CROSS JOIN bounds b
          JOIN incidents i ON i.id = d.incident_id
         GROUP BY d.responder
    ),
    -- A proxy, and labelled as one. There is no shift table -- the hosted
    -- provider owns rotations and the static rota is a YAML file -- so this
    -- counts consecutive calendar days on which the responder was paged at all.
    -- It UNDER-counts a quiet rotation, which biases the score downward and so
    -- errs towards paging the primary. That is the conservative direction: the
    -- failure mode of over-counting is rerouting away from someone who was fine.
    paged_days AS (
        SELECT DISTINCT p.responder,
               (p.time AT TIME ZONE p.tz)::date AS day
          FROM page_events p CROSS JOIN bounds b
         WHERE p.time > b.at - INTERVAL '30 days' AND p.time <= b.at
           AND (b.responder IS NULL OR p.responder = b.responder)
    ),
    streaks AS (
        SELECT responder, day,
               day - (row_number() OVER (PARTITION BY responder ORDER BY day))::int AS grp
          FROM paged_days
    ),
    runs AS (
        SELECT responder, grp, count(*)::int AS len, max(day) AS last_day
          FROM streaks
         GROUP BY responder, grp
    ),
    consecutive AS (
        -- Only the run that is still current counts. A five-day streak that
        -- ended a fortnight ago says nothing about how tired someone is today.
        SELECT r.responder, max(r.len) AS consecutive_oncall_days
          FROM runs r CROSS JOIN bounds b
         WHERE r.last_day >= (b.at AT TIME ZONE 'UTC')::date - 1
         GROUP BY r.responder
    )
    SELECT pg.responder,
           pg.pages_8h,
           pg.night_pages_24h,
           pg.sev1_count_7d,
           COALESCE(e.incident_minutes_24h, 0) AS incident_minutes_24h,
           COALESCE(c.consecutive_oncall_days, 0) AS consecutive_oncall_days
      FROM pages pg
      LEFT JOIN engaged e ON e.responder = pg.responder
      LEFT JOIN consecutive c ON c.responder = pg.responder
     ORDER BY pg.pages_8h DESC
    """
)


# Q4 — runbook efficacy (D4).
#
# C-07: ``runbook_step_signals`` is one row per *executed step*, not one row per
# incident. Rev 2's version selects ``rs.steps_followed`` and ``rs.steps_total``,
# columns that do not exist; adherence is computed by counting the per-step rows
# and dividing by the runbook's declared step count.
Q4_RUNBOOK_EFFICACY = text(
    """
    SELECT r.name,
           count(*) AS uses,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY i.ttm_seconds) / 60.0 AS median_ttm_min,
           avg(sig.followed::float / nullif(array_length(r.step_ids, 1), 0)) AS adherence
      FROM incidents i
      JOIN runbooks r ON r.id = i.runbook_id
      LEFT JOIN LATERAL (
           SELECT count(*) AS followed
             FROM runbook_step_signals rs
            WHERE rs.incident_id = i.id AND rs.runbook_id = r.id
      ) sig ON TRUE
     WHERE i.mitigated_at IS NOT NULL
     GROUP BY r.name
    HAVING count(*) >= :min_uses
     ORDER BY median_ttm_min DESC
    """
)


# Q5 — dead steps (X-01, corrected).
#
# Expand the array exactly once, in the lateral, and reference that one alias
# everywhere. ``min_uses`` is a parameter rather than a literal 5 so the test can
# execute it against a small seeded fixture -- the production threshold stays 5,
# and E-3 already records that the auto-PR job on top of this is deferred
# post-MVP because eight weeks will not produce five incidents per runbook.
Q5_DEAD_STEPS = text(
    """
    SELECT rb.name, s.step_id AS step, s.uses, s.skipped,
           round(s.skipped::numeric / s.uses, 2) AS skip_rate
      FROM (
        SELECT r.id AS runbook_id,
               st.step_id,
               count(DISTINCT i.id) AS uses,
               count(DISTINCT i.id) FILTER (
                 WHERE NOT EXISTS (
                   SELECT 1 FROM runbook_step_signals rs
                    WHERE rs.incident_id = i.id AND rs.step_id = st.step_id)) AS skipped
          FROM runbooks r
          JOIN incidents i ON i.runbook_id = r.id
          CROSS JOIN LATERAL unnest(r.step_ids) AS st(step_id)
         WHERE i.mitigated_at IS NOT NULL
         GROUP BY r.id, st.step_id
      ) s
      JOIN runbooks rb ON rb.id = s.runbook_id
     WHERE s.uses >= :min_uses AND s.skipped::numeric / s.uses > :skip_threshold
     ORDER BY skip_rate DESC
    """
)

# The production threshold. Named so the deferral in E-3 points at something.
DEAD_STEP_MIN_USES = 5
DEAD_STEP_SKIP_THRESHOLD = 0.8


async def fatigue_windows(
    session: AsyncSession,
    *,
    at: datetime,
    responder: str | None = None,
) -> dict[str, ResponderWindow]:
    """Build ``ResponderWindow`` per responder (``Q7``).

    ``at`` is required, not defaulted to now: the router passes the incident's
    detection time so the score is a property of the incident rather than of
    when the query happened to run, and the replay harness passes a recorded
    instant so the same incident routes to the same person every time.
    """
    rows = (await session.execute(Q7_FATIGUE_WINDOW, {"at": at, "responder": responder})).mappings()
    return {
        str(row["responder"]): ResponderWindow(
            responder=str(row["responder"]),
            pages_8h=int(row["pages_8h"] or 0),
            night_pages_24h=int(row["night_pages_24h"] or 0),
            incident_minutes_24h=int(row["incident_minutes_24h"] or 0),
            consecutive_oncall_days=int(row["consecutive_oncall_days"] or 0),
            sev1_count_7d=int(row["sev1_count_7d"] or 0),
        )
        for row in rows
    }


async def window_for(session: AsyncSession, responder: str, *, at: datetime) -> ResponderWindow:
    """One responder's window. An unknown responder is a fresh one, not an error.

    Someone's first ever page must not fail routing, and a missing row means
    exactly "no pages in the window" -- which is a zero score, not a gap.
    """
    windows = await fatigue_windows(session, at=at, responder=responder)
    return windows.get(responder, ResponderWindow(responder=responder))


async def runbook_efficacy(session: AsyncSession, *, min_uses: int = 3) -> list[dict[str, Any]]:
    rows = (await session.execute(Q4_RUNBOOK_EFFICACY, {"min_uses": min_uses})).mappings().all()
    return [dict(row) for row in rows]


async def dead_steps(
    session: AsyncSession,
    *,
    min_uses: int = DEAD_STEP_MIN_USES,
    skip_threshold: float = DEAD_STEP_SKIP_THRESHOLD,
) -> list[dict[str, Any]]:
    """Steps skipped in more than ``skip_threshold`` of the runbook's uses.

    The input to D4's auto-PR job, which is **deferred post-MVP** (E-3) and said
    so out loud: the filter needs five incidents per runbook and eight weeks will
    not produce them. The query and the signal capture ship now, because that is
    what the interview answer actually describes; claiming a closed loop that has
    never closed is what a follow-up question exposes.
    """
    rows = (
        await session.execute(
            Q5_DEAD_STEPS, {"min_uses": min_uses, "skip_threshold": skip_threshold}
        )
    ).mappings()
    return [dict(row) for row in rows]


# =============================================================================
# Week 8 — the remaining nine (W8-01)
#
# Every one is a typed function rather than a string the API layer interpolates
# into. Two reasons, and the second is the one that matters: SQL assembled at a
# call site is SQL nobody can test in isolation, and an analytics endpoint that
# accepts a fragment of a query is an injection surface wearing a filter's
# clothes. The API passes bound parameters; the SQL lives here.
#
# Three of the reference's twelve needed correcting before they would run at
# all. Q4, Q5 and Q7 were corrected in week 5 (above); the notes below record
# what changed in the rest and why, so a reviewer can walk backwards from the
# code to the decision.
# =============================================================================


@dataclass(frozen=True, slots=True)
class ActiveIncident:
    """One row of the dashboard banner (Q1).

    ``elapsed_min`` is computed in SQL and sent as a number, and ``detected_at``
    is sent alongside it. The reference returns only the elapsed figure; sending
    the start instant as well is what lets the banner tick without re-fetching,
    and without the UI doing clock arithmetic against a rendered string.
    """

    public_key: str
    title: str
    severity: str
    state: str
    service: str | None
    detected_at: datetime
    elapsed_min: float
    correlated_alert_count: int
    chat_channel_id: str | None


# Q1 — active incidents.
#
# The state list is negative on purpose: NOT IN (the terminal states). A positive
# list of "open" states would need editing every time the state machine gains
# one, and the failure mode is an incident that silently stops appearing on the
# banner -- during the incident.
Q1_ACTIVE_INCIDENTS = text(
    """
    SELECT i.public_key, i.title, i.severity::text AS severity, i.state::text AS state,
           s.name AS service, i.detected_at,
           EXTRACT(EPOCH FROM (now() - i.detected_at)) / 60.0 AS elapsed_min,
           i.correlated_alert_count, i.chat_channel_id
      FROM incidents i
      LEFT JOIN services s ON s.id = i.primary_service_id
     WHERE i.state NOT IN ('closed', 'merged', 'false_positive', 'abandoned')
     ORDER BY i.severity, i.detected_at
    """
)

# Q2 — MTTA / TTM / MTTR per service.
#
# The reference JOINs services, which silently drops every incident with no
# primary service -- exactly the ones where triage was hardest and the MTTR
# worst. LEFT JOIN with a coalesce keeps them under an explicit 'unassigned'
# label, because a metric that improves by discarding its worst cases is a
# metric that lies.
#
# TTM is reported separately from MTTR deliberately. Time-to-mitigate is what
# the responder controls; the tail between mitigation and resolution is
# paperwork, and averaging the two together hides which half is slow.
Q2_MTTR_BY_SERVICE = text(
    """
    SELECT coalesce(s.name, 'unassigned') AS service,
           count(*) AS incidents,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY i.tta_seconds)          AS p50_tta_s,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY i.ttm_seconds) / 60.0   AS p50_ttm_min,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY i.mttr_seconds) / 60.0  AS p50_mttr_min,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY i.mttr_seconds) / 60.0 AS p95_mttr_min
      FROM incidents i
      LEFT JOIN services s ON s.id = i.primary_service_id
     WHERE i.resolved_at > now() - make_interval(days => :days)
     GROUP BY coalesce(s.name, 'unassigned')
     ORDER BY p50_mttr_min DESC NULLS LAST
    """
)

# Q3 — storm compression (the D3 proof).
#
# parent_incident_id IS NULL counts *root* incidents only, so a merged child does
# not inflate the denominator and quietly halve the ratio the whole
# differentiator rests on.
Q3_STORM_COMPRESSION = text(
    """
    SELECT date_trunc('week', detected_at) AS week,
           count(*) FILTER (WHERE parent_incident_id IS NULL) AS incidents,
           sum(correlated_alert_count)                        AS alerts,
           round(sum(correlated_alert_count)::numeric
                 / nullif(count(*) FILTER (WHERE parent_incident_id IS NULL), 0), 1) AS ratio
      FROM incidents
     WHERE detected_at > now() - make_interval(days => :days)
     GROUP BY week
     ORDER BY week
    """
)

# Q6 — repeat incidents (D8).
#
# The reference cross-joins `incidents i1, incidents i2` and then filters
# `i1.id = $1`, which asks the planner to consider every pair before discarding
# all but one row's worth. Fetching the one embedding in a CTE first turns that
# into a single scan against the HNSW index -- same answer, and it uses the index
# migration 0002 created rather than ignoring it.
#
# open_action_items is the half that makes D8 a differentiator rather than a
# similarity toy: "similar to #204, action item still open" is a sentence that
# changes what somebody does next.
Q6_REPEAT_INCIDENTS = text(
    """
    WITH target AS (
        SELECT embedding FROM incidents WHERE id = :incident_id
    )
    SELECT i.public_key, i.title, i.detected_at,
           1 - (i.embedding <=> t.embedding) AS similarity,
           (SELECT count(*) FROM action_items a
             WHERE a.incident_id = i.id AND a.status = 'open') AS open_action_items
      FROM incidents i CROSS JOIN target t
     WHERE i.id <> :incident_id
       AND i.embedding IS NOT NULL
       AND t.embedding IS NOT NULL
       AND 1 - (i.embedding <=> t.embedding) > :threshold
     ORDER BY i.embedding <=> t.embedding
     LIMIT :limit
    """
)

# Q8 — human incident-time per service (the toil budget).
#
# Counts *elapsed* incident time, not engineer-hours, and the column name says
# so. Three people in a war room for an hour is three hours of human attention
# and this reports one; the honest name is the fix, because a "toil" figure that
# silently under-reports by a factor of the team size is worse than no figure.
Q8_TOIL_BY_SERVICE = text(
    """
    SELECT coalesce(s.name, 'unassigned') AS service,
           count(*) AS incidents,
           round(sum(i.mttr_seconds) / 3600.0, 1) AS incident_hours
      FROM incidents i
      LEFT JOIN services s ON s.id = i.primary_service_id
     WHERE i.resolved_at > now() - make_interval(days => :days)
     GROUP BY coalesce(s.name, 'unassigned')
     ORDER BY incident_hours DESC NULLS LAST
    """
)

# Q9 — PIR grounding compliance. The SLI with a zero error budget.
#
# Only the *published* revision of each incident counts. Without the DISTINCT ON,
# a rejected draft that was later superseded would be counted alongside the
# document that actually shipped, and the one metric that must read 1.000 would
# be dragged down by attempts nobody ever saw.
Q9_GROUNDING_COMPLIANCE = text(
    """
    WITH published AS (
        SELECT DISTINCT ON (incident_id) *
          FROM pir_documents
         ORDER BY incident_id, revision DESC
    )
    SELECT date_trunc('day', created_at) AS day,
           count(*) AS pirs,
           count(*) FILTER (WHERE citation_coverage = 1.000) AS fully_grounded,
           count(*) FILTER (WHERE generation_layer = 'skeleton') AS skeletons,
           round(avg(human_edit_ratio)::numeric, 3) AS avg_edit_ratio
      FROM published
     GROUP BY day
     ORDER BY day DESC
     LIMIT :limit
    """
)

# Q10 — action-item half-life by priority.
#
# oldest_open_days is the number that makes this a control loop rather than a
# chart: a median close time looks healthy while one P0 has been open for eight
# months, and the maximum is what a reviewer actually asks about.
Q10_ACTION_HALF_LIFE = text(
    """
    SELECT priority,
           count(*) FILTER (WHERE status = 'open') AS open_now,
           count(*) FILTER (WHERE status = 'done') AS closed,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (completed_at - created_at)) / 86400.0
           ) FILTER (WHERE completed_at IS NOT NULL) AS median_close_days,
           max(EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0)
               FILTER (WHERE status = 'open') AS oldest_open_days
      FROM action_items
     GROUP BY priority
     ORDER BY priority
    """
)

# Q11 — cost per PIR by prompt version (the MLOps panel).
#
# Grouped by prompt version *and* model, which the reference does not do. The
# whole E-1 argument is "the same prompt got cheaper on a different model", and a
# rollup that collapses the model makes exactly that comparison invisible.
#
# Skeletons are excluded: they cost nothing, so averaging them in means a run of
# provider outages reads as a cost improvement.
Q11_COST_PER_PIR = text(
    """
    SELECT prompt_version, model,
           count(*) AS pirs,
           round(avg(cost_usd)::numeric, 4) AS avg_cost_usd,
           round(sum(cost_usd)::numeric, 4) AS total_cost_usd,
           round(avg(generation_ms)) AS avg_ms,
           round(avg(human_edit_ratio)::numeric, 3) AS avg_edit_ratio
      FROM pir_documents
     WHERE created_at > now() - make_interval(days => :days)
       AND generation_layer <> 'skeleton'
     GROUP BY prompt_version, model
     ORDER BY prompt_version DESC, model
    """
)

# Q12 — transcript completeness by active incident (the key SLI).
#
# Reports what we hold. The denominator -- what Slack holds -- is not available
# without a history call we are rate-limited out of (ADR 0003), so this returns
# the stored count and the channel, and the reconciler samples the ratio
# separately. Presenting a fraction here would imply a denominator we do not have.
Q12_TRANSCRIPT_COMPLETENESS = text(
    """
    SELECT i.public_key, i.chat_channel_id,
           count(m.id) AS stored,
           max(m.received_at) AS newest_stored_at
      FROM incidents i
      LEFT JOIN slack_messages m ON m.incident_id = i.id
     WHERE i.state NOT IN ('closed', 'merged', 'false_positive', 'abandoned')
     GROUP BY i.id, i.public_key, i.chat_channel_id
     ORDER BY i.public_key
    """
)


async def active_incidents(session: AsyncSession) -> list[ActiveIncident]:
    rows = (await session.execute(Q1_ACTIVE_INCIDENTS)).mappings().all()
    return [
        ActiveIncident(
            public_key=str(r["public_key"]),
            title=str(r["title"]),
            severity=str(r["severity"]),
            state=str(r["state"]),
            service=r["service"],
            detected_at=r["detected_at"],
            elapsed_min=float(r["elapsed_min"] or 0.0),
            correlated_alert_count=int(r["correlated_alert_count"] or 0),
            chat_channel_id=r["chat_channel_id"],
        )
        for r in rows
    ]


async def mttr_by_service(session: AsyncSession, *, days: int = 30) -> list[dict[str, Any]]:
    rows = (await session.execute(Q2_MTTR_BY_SERVICE, {"days": days})).mappings().all()
    return [dict(r) for r in rows]


async def storm_compression(session: AsyncSession, *, days: int = 90) -> list[dict[str, Any]]:
    rows = (await session.execute(Q3_STORM_COMPRESSION, {"days": days})).mappings().all()
    return [dict(r) for r in rows]


async def repeat_incidents(
    session: AsyncSession,
    incident_id: int,
    *,
    threshold: float = 0.85,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Similar past incidents, and whether their action items are still open (D8).

    Returns nothing rather than raising when the incident has no embedding.
    Similarity is an enrichment; an incident without an embedding is a normal
    state -- they are computed after resolution -- and must not break the page.
    """
    rows = (
        await session.execute(
            Q6_REPEAT_INCIDENTS,
            {"incident_id": incident_id, "threshold": threshold, "limit": limit},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def toil_by_service(session: AsyncSession, *, days: int = 7) -> list[dict[str, Any]]:
    rows = (await session.execute(Q8_TOIL_BY_SERVICE, {"days": days})).mappings().all()
    return [dict(r) for r in rows]


async def grounding_compliance(session: AsyncSession, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = (await session.execute(Q9_GROUNDING_COMPLIANCE, {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def action_half_life(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(Q10_ACTION_HALF_LIFE)).mappings().all()
    return [dict(r) for r in rows]


async def cost_per_pir(session: AsyncSession, *, days: int = 60) -> list[dict[str, Any]]:
    rows = (await session.execute(Q11_COST_PER_PIR, {"days": days})).mappings().all()
    return [dict(r) for r in rows]


async def transcript_completeness(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(Q12_TRANSCRIPT_COMPLETENESS)).mappings().all()
    return [dict(r) for r in rows]


# Every query, for the test that runs all twelve. Listed explicitly rather than
# discovered by reflection: a query that stops being exported should fail the
# count assertion, not vanish quietly from the suite meant to cover it.
ALL_QUERIES: tuple[str, ...] = (
    "Q1_ACTIVE_INCIDENTS",
    "Q2_MTTR_BY_SERVICE",
    "Q3_STORM_COMPRESSION",
    "Q4_RUNBOOK_EFFICACY",
    "Q5_DEAD_STEPS",
    "Q6_REPEAT_INCIDENTS",
    "Q7_FATIGUE_WINDOW",
    "Q8_TOIL_BY_SERVICE",
    "Q9_GROUNDING_COMPLIANCE",
    "Q10_ACTION_HALF_LIFE",
    "Q11_COST_PER_PIR",
    "Q12_TRANSCRIPT_COMPLETENESS",
)
