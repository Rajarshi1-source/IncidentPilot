# ADR 0004 — Routing decisions, the fatigue window, and no job runner

- **Status:** Accepted
- **Date:** 7 September 2026
- **Week:** 5
- **Refines:** Detailed Implementation Plan §9 (W5-05, W5-07, W5-17), ADR 0001, C-04, C-06, X-01

Four calls week 5 had to make that the plan implies without settling. Two are
corrections to the source material, two are consequences of an earlier decision.

---

## Decision 1 — `Q7` computes five terms, not two

### Context

`fatigue_score` reads five numbers: `pages_8h`, `night_pages_24h`,
`incident_minutes_24h`, `consecutive_oncall_days`, `sev1_count_7d`. `Q7` in
`schema-and-queries.md` returns **two** of them — `pages_8h` and `night_pages`.
C-04 settles that the scorer stays pure and all time arithmetic happens in SQL,
which makes the missing three the query's problem, not the scorer's.

### Decision

`Q7` is rewritten to produce all five, with three details worth stating:

- **The window end is a parameter, not `now()`.** The router passes the
  incident's `detected_at`, so the score is a property of the incident rather
  than of when the relay happened to reach the row — and the week 7 replay
  routes the same recorded incident to the same person every time (INV-10).
- **Both ends of the window are closed.** Without `p.time <= :at`, a page that
  happened *after* the reference instant still counts. Invisible in production,
  where `at` is always recent; quietly wrong under replay, where the entire
  point is to score the incident as it was.
- **`incident_minutes_24h` de-duplicates by incident before joining.** An
  incident that paged someone three times contributes its duration once.
  Summing per page would treat one noisy incident as three, and inflate the term
  that is specifically meant to measure *time* rather than volume.

### Consequence worth naming: `consecutive_oncall_days` is a proxy

There is no shift table. The hosted provider owns rotations and the static rota
is a YAML file with a period, so "how many days has this person been on call" is
not answerable from our data. The query counts consecutive calendar days on
which the responder was **paged at all**.

That **under-counts a quiet rotation** — five uneventful days on call score
zero. The bias is downward, so it errs towards paging the primary, and that is
the direction to err in: over-counting would reroute away from someone who was
fine. The term is 0.15 of the score, the proxy is labelled in the SQL, and it is
a better answer than dropping the term and pretending the score has four parts.

---

## Decision 2 — `Q5` and `Q4` are committed corrected (X-01, C-07)

`Q5` as published does not run: it expands `step_ids` twice (select list *and*
`CROSS JOIN LATERAL`), references `unnest_step` from a different expansion inside
a `FILTER`, and leaves `r.name` ambiguous. `Q4` selects `rs.steps_followed` and
`rs.steps_total`, columns that do not exist on a table defined as one row per
executed step.

Both are corrected and both are **executed** by
`tests/integration/test_queries_integration.py` rather than reviewed. The failure
mode of shipping the broken `Q5` is the cruel one: the efficacy job would run,
return nothing, and look exactly like a system with no dead steps — which is
precisely the silence D4 exists to break in other people's runbooks.

`min_uses` is a parameter so the test can seed six incidents. The production
default stays 5, and E-3's deferral of the auto-PR job is asserted rather than
claimed: `test_the_min_uses_filter_is_why_the_auto_pr_job_is_deferred` raises the
threshold above the seeded data and shows the query returning nothing.

---

## Decision 3 — no job runner; the database is the queue (W5-17)

### Context

W5-17 specifies arq. ADR 0001 already deferred `arq==0.28.0` because it caps
`redis[hiredis]<6` against this project's `redis==8.1.0`. Taking arq means
downgrading the client the entire ingest path runs on, to get a job runner for
three timers.

### Decision

`orchestration/scheduler.py` is a plain asyncio loop, one task per job.

What arq would add is a **durable job queue**, and none of these jobs need one.
Each is a *sweep* that recomputes its work list from the database on every pass:
a missed tick costs a delay, never a lost job, and restarting the process finds
exactly the same incidents. A job runner earns its keep when work is *enqueued*
and must not be lost — here the database is the queue, and the outbox already
covers the one place where losing an enqueued intent would matter.

### Consequences

- No dashboard of queued jobs, no retry history per job. Acceptable: each job
  logs how many things it acted on, which is what distinguishes "ran and found
  nothing" from "did not run" — the failure that actually happens.
- One task per job rather than one loop with modulo arithmetic, so a slow
  reconcile pass cannot delay the abandonment sweep and a job that raises cannot
  take the others down.
- If a genuinely enqueued, must-not-be-lost job appears later, the answer is the
  outbox table that already exists, not a second queue technology.

---

## Decision 4 — a transient paging failure retries, then broadcasts

### Context

The ladder covers "we cannot resolve who is on call". It did not cover "we know
who, the provider is returning 503, and we cannot ring their phone." Retrying
quietly is the obvious behaviour and is exactly the silent failure G5 forbids: the
incident is minutes old with nobody woken up and nothing said.

### Decision

`page_responder` reads the outbox row's `attempts`. Below
`PAGE_ATTEMPTS_BEFORE_BROADCAST` (3) it re-raises, so the relay defers with
exponential backoff — roughly a minute, enough to ride out a blip. At or above
it, the war room and the team channel are told nobody was reached.

A `PermanentPagingError` skips the retries entirely, which is how the static rota
behaves: it can name someone and cannot page them, and it says so by raising
rather than returning `delivered=False`. A soft failure there would let the
incident proceed believing a human was on the way.

---

## One deviation from the file manifest, recorded

The manifest lists `api/slash/{resolve,update,escalate,metrics,falsepositive,split,step}.py`
and `runbooks/{matcher,renderer,parser,detector}.py`.

- The seven slash commands live in one `api/slash/commands.py`. Seven files of
  fifteen lines each, sharing one import block, one verification path and one
  incident lookup, would be seven places to forget the same thing. The **routes**
  are still one endpoint per command, which is what Slack's per-command Request
  URL requires.
- `runbooks/library.py` is a fifth module. Parsing, matching and rendering are
  pure; "the eight files on disk, synced to the table, addressable by the id
  `runbook_step_signals` references" is stateful, and hiding it inside one of the
  pure three is what would make them impure.
