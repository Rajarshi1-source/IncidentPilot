# Scaling decisions

Two decisions live here. The first is the database; the second, added in week 3,
is the partitioning model. Interviewers read `docs/` before they read `src/`, and
this is the file to point them at.

---

## Decision 1 — PostgreSQL + TimescaleDB, on two tables only

**Status:** Accepted (week 2) · **Trigger to revisit:** metric snapshots leaving scope

### Context

Three workloads share one product:

| Workload | Volume | Shape |
|---|---|---|
| Transactional incident records | ~30 rows/day | Relational, FK-heavy, needs multi-row ACID |
| Append-only transcript | ~5 K rows/day | One table, one unique constraint, full-text search |
| Metric snapshots | see below | Time-series, write-once, read in windows |

### Options

**A — Plain PostgreSQL + native partitioning.** Works. Costs hand-written
partition maintenance, a cron job for retention, and `generate_series` LEFT JOINs
for gap-filled charts: roughly 150 lines of infrastructure code to write, test
and monitor.

**B — PostgreSQL + TimescaleDB on the two time-series tables.** Declarative
retention and compression policies, continuous aggregates,
`time_bucket_gapfill()`. One extension, no second database, same connection pool,
same ORM.

**C — PostgreSQL + ClickHouse.** Best raw analytics performance. Costs a second
datastore, a sync path, and a consistency problem between an incident record and
its samples.

### Decision: B

Rejected C on operational cost for a single-operator system. *(Worth saying out
loud in an interview: I did choose ClickHouse — for TraceMap, which is 100%
analytical. Here the workload is 90% transactional. Same engineer, different
answer, because the workloads are different.)*

### What I got wrong — twice

**First correction.** The original justification claimed timeline events would
reach millions of rows a month. Doing the arithmetic properly — 30 incidents ×
45 min × ~20 events/min — gives **~800 K rows/month**, which plain PostgreSQL
handles comfortably with a BRIN index. That number does not justify anything.

**Second correction.** The replacement justification was the metric-sample table
at "72,000 rows per incident", from 20 series × **1-second resolution** × 60
minutes. Building the sampler exposed the problem: Prometheus scrapes at 15 s by
default, so **one-second resolution does not exist in the source data**. A
`step=1s` range query returns an interpolated staircase, not 72,000 distinct
observations. At a 15 s step the real figure is:

```
20 series × 240 points/hour = 4,800 rows per incident
4,800 × 30 incidents/day × 30 days ≈ 144 K rows/month
```

Two orders of magnitude below the claim.

### What survives

The row count was never the real argument. What holds up:

1. **`signal_samples` is still the largest table by an order of magnitude**, and
   it is the only one with a genuine time-series access pattern — write once,
   read in windows, never update.
2. **Declarative retention and compression replace ~150 lines** of cron and
   partition maintenance that would otherwise need writing, testing and
   monitoring. `add_retention_policy('signal_samples', INTERVAL '90 days')` is
   one line and it is *operationally* different from a cron job: it cannot
   silently stop running without showing up in `timescaledb_information.jobs`.
3. **`time_bucket_gapfill()`** renders the incident chart correctly across gaps.
   Plain SQL needs a `generate_series` LEFT JOIN per chart.
4. **Continuous aggregates** mean the dashboard never scans raw data.

### When I would reverse it

If metric snapshots left scope, TimescaleDB would go with them and `timeline_events`
would become a plain table with a BRIN index. That is the trigger, and it is
written down so a future maintainer can act on it rather than inheriting a
dependency nobody can justify.

### Why both corrections are recorded

A decision document that hides its own revisions is worthless. The first
correction was in the plan; the second was found by building the thing. Keeping
both is the more useful artifact — and volunteering a *second* self-caught
arithmetic error demonstrates the audit habit rather than a single anecdote.

### Related implementation notes

- `compress_segmentby` is the **low-cardinality** column. Segmenting
  `timeline_events` by `incident_id` — the highest-cardinality column in the
  table — produces one tiny compressed batch per incident and defeats columnar
  compression entirely; with per-batch overhead you can end up larger than
  uncompressed. Segment on `intent`, order by `incident_id, time DESC`.
  `test_compression_ratio_is_sane` measures this rather than trusting it.
- Hypertables carry **no foreign key** to `incidents`. FK enforcement across many
  chunks is a chunk-management cost with little payoff; integrity is the
  application's job plus a periodic orphan check.
- **TimescaleDB Toolkit is a separate extension** from `timescaledb`. It ships in
  the `timescaledb-ha` image but not in a plain install, so the percentile
  aggregate is created best-effort and the committed aggregate uses plain
  `avg()`.

---

## Decision 2 — Serialize per incident, parallelize across incidents

**Status:** Accepted (week 2, exercised in week 3) · **Trigger to revisit:** ~500 concurrent incidents

### Problem

Workers scale horizontally, but Slack imposes per-channel ordering and roughly
one message per second per channel. Two workers processing events for the same
incident produce interleaved channel updates and duplicated side effects.

### Options

**A — A single worker.** Correct, trivially ordered, and a single point of
throughput failure. A 40-alert storm serializes behind one process.

**B — N workers, no partitioning.** Maximum throughput, wrong output. Timer
updates race the runbook post; two workers both invite the responder.

**C — N workers, partitioned by incident.** Each incident is handled by exactly
one worker at a time via a Postgres advisory lock; different incidents run fully
in parallel.

### Decision: C

`pg_advisory_xact_lock(hashtext('corr:' || dedup_key))` while deciding new-or-merge,
and `pg_try_advisory_xact_lock(hashtext('inc:' || incident_id))` in the stream
consumer. If the consumer lock is not acquired, the entry is left un-ACKed and
another worker picks it up later — no busy-waiting, no coordination service.

The correlation lock **blocks** rather than trying-and-skipping, deliberately:
the caller is about to decide whether to create an incident, and two workers
making that decision concurrently is exactly the race the dedup constraint exists
to catch. Serializing for a few milliseconds beats catching forty
`IntegrityError`s during a storm.

### The insight worth stating

The natural parallelism boundary is the **incident**, not the message, because
the ordering constraint is imposed by Slack per channel and there is exactly one
channel per incident. **Choosing the partition key correctly is the whole scaling
decision; everything else is configuration.**

`SELECT ... FOR UPDATE SKIP LOCKED` on the outbox is the same idea applied to
dispatch: N relay replicas need no coordination service, because Postgres row
locks *are* the coordination primitive. That is the direct answer to "why not
ZooKeeper" — ZooKeeper is a consensus service, not a queue, and adding a 3-node
quorum to coordinate 20 rows a minute would be the most over-engineered decision
in the project.

### When I would revisit

Above roughly 500 concurrent incidents, advisory-lock contention on a single
primary becomes the ceiling. The next step is stream partitioning by
`hash(incident_id) % N` with sticky consumers, which removes the lock entirely.

---

## Decision 3 — Correlation scores against the accumulated incident, not its origin

**Status:** Accepted (week 2) · **Supersedes:** the formula in Rev 2 §9.3

### Problem

The plan's correlation formula scores each alert against the incident's
**origin** — its `primary_service` and `detected_at`. Measured on the 40-alert
cascade fixture, that produces **twelve incidents, not one**:

```
PostgresPrimaryDown       postgres-primary   dt=  0s  hops=0  => 1.000  MERGE
ReplicaLagHigh            postgres-replica   dt= 14s  hops=1  => 0.734  MERGE
HighErrorRate             payments-api       dt= 23s  hops=1  => 0.686  MERGE
HighErrorRate             checkout-web       dt= 38s  hops=2  => 0.533
KafkaConsumerLag          kafka-events       dt= 53s  hops=2  => 0.485
                                                   ... threshold is 0.62
```

Everything more than two hops from the root scores around 0.50, and temporal
decay from t0 has nearly expired by the time the outer ring of a large storm
fires. The gate the formula is supposed to pass is "40 alerts → 1 incident".

### Decision

An incident **accumulates**. `affected_services` and `last_alert_at` grow as
alerts merge in, and both are what the next alert is scored against:

- **Temporal proximity** is measured to the incident's last activity, not its
  start. A cascade still firing four minutes in is self-evidently still the same
  incident.
- **Topological distance** is the minimum hop count to *any* already-implicated
  service. Cascades chain outward: `checkout-web` is two hops from the failing
  database but one hop from `payments-api`, which joined thirty seconds ago.

### What did not change

The weights are still 0.4 / 0.4 / 0.2 and the threshold is still 0.62. This is
not the correlator being loosened until the gate passes — it is the comparison
being made against the right thing. Measuring only to the origin catches the
root's immediate neighbours and nothing else, which makes the dependency-graph
signal decorative.

### Related correction

`ServiceGraph.depth()` had the same class of error in the opposite direction.
`pick_root_signal` maximizes depth, so depth must grow **downward through the
stack** — a datastore everything rests on is deep, an edge API is depth 0.
Implemented as "longest dependency chain below a service", it returns the
inverse and root-signal selection picks `checkout-web`: the loudest symptom,
which is the precise failure D3 exists to prevent.
