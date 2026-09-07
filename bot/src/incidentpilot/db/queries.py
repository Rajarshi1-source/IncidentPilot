"""The analytics queries, as functions (W5-07, W5-14).

Week 5 commits three of the twelve: ``Q7`` (the fatigue window, D5's input),
``Q4`` (runbook efficacy) and ``Q5`` (dead steps, D4's input). The rest arrive
in week 8 with the dashboard.

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
