# IncidentPilot — Detailed Implementation Plan

### The executable build plan derived from **Implementation Plan Rev 2** · Junior SRE Capstone
### Generated 6 September 2026 · Target: 8 weeks, greenfield

---

> **What this document is.** Rev 2 is a *design and audit* document — 2,863 lines that decide
> **what** to build and **why**, and correct 14 defects in Rev 1. It is not a build order.
>
> This document is the **execution layer**: every file to create, in what order, with what
> contract, guarded by which test, gated by which acceptance criterion. It resolves the eleven
> places where Rev 2, the starter code, and the skill reference files disagree, and it records
> which side won and why (§0.4).
>
> **Source-of-truth hierarchy, applied throughout:**
> 1. `IncidentPilot_Implementation_Plan_REV2.md` — **primary**. Architecture, defect fixes,
>    version pins, differentiators.
> 2. `.claude/skills/*.skill` (8 skills) — **binding as project rules**. Their "Critical rules"
>    sections are non-negotiable invariants (§1); their "Definition of done" sections are the
>    acceptance criteria (Appendix D).
> 3. `.claude/skills/IncidentPilot_starter_code.md` — **the reference implementation**. Where it
>    and Rev 2 show the same function differently, the starter code wins (it is later and more
>    complete); every such case is logged in §0.4.
> 4. `references/schema-and-queries.md` (621 lines) and `references/eval-harness.md` (337 lines) —
>    **authoritative for the data layer and the eval harness respectively**, superseding the
>    abbreviated versions in Rev 2 §6 and §10.
> 5. `IncidentPilot_Implementation_Plan.md` (Rev 1) — **last resort only**, and only for content
>    Rev 2 references but never enumerates. Exactly one such gap-fill is used in this plan (the
>    runbook catalogue, Appendix C) and it is labelled as such.

---

## Table of Contents

**Part 0 — Ground truth**
- §0.1 Codebase audit: what exists today
- §0.2 What "done" means for this project
- §0.3 Document conventions and traceability IDs
- §0.4 **Conflicts between sources, and how this plan resolves them** (11 findings)
- §0.5 Defects in the source material that this plan fixes (4 findings)

**Part 1 — Foundations**
- §1 The twelve invariants (distilled from the skills' Critical Rules)
- §2 Repository scaffold — complete file manifest, week-tagged
- §3 Toolchain, pins, and the reproducible-build contract
- §4 The dependency order: what must exist before what

**Part 2 — Week-by-week execution (the build order)**
- §5 Week 1 — Ingest and the trust boundary
- §6 Week 2 — Correlation and the state machine
- §7 Week 3 — Slack orchestration and the outbox relay
- §8 Week 4 — Event-sourced transcript
- §9 Week 5 — Responders, fatigue routing, runbooks
- §10 Week 6 — Deterministic impact and the grounded PIR
- §11 Week 7 — Replay harness and the CI eval gate
- §12 Week 8 — Dashboard, deploy, polish

**Part 3 — Cross-cutting specifications**
- §13 Data layer — migration order, DDL assembly, CI schema checks
- §14 Configuration and the vendor-agnosticism contract
- §15 Observability, SLIs, and alert rules
- §16 Security, privacy, and the data boundary
- §17 Resilience — the four levels, wired concretely
- §18 CI/CD — three workflows, job by job
- §19 Testing strategy — the 10-layer matrix and the 12 crash points

**Part 4 — Verification and delivery**
- §20 Acceptance gates — the binary test per week
- §21 Demo script and the 10 screenshots
- §22 README assembly order

**Appendices**
- A. Defect traceability: B-01 … B-14 → file → test
- B. Differentiator traceability: D1 … D9 → file → acceptance criterion
- C. Runbook catalogue (8 runbooks, step IDs) — *Rev 1 gap-fill*
- D. Per-skill Definition of Done — the consolidated checklist
- E. Open decisions requiring your input
- F. Risk register with build-order mitigations

---

# Part 0 — Ground truth

## §0.1 Codebase audit: what exists today

Audited `D:\New Projects\IncidentPilot` on 6 September 2026, git branch `main`, working tree clean,
two commits (`140226f` Initial commit, `7e0f8a1` Skill files updated).

```
IncidentPilot/
├── .claude/
│   ├── settings.local.json
│   └── skills/
│       ├── incidentpilot-ingest-correlation.skill      (zip, 191-line SKILL.md)
│       ├── incidentpilot-state-orchestration.skill     (zip, 231-line SKILL.md)
│       ├── incidentpilot-slack-platform.skill          (zip, 205-line SKILL.md)
│       ├── incidentpilot-pir-grounding.skill           (zip, 231-line SKILL.md)
│       ├── incidentpilot-mlops-eval.skill              (zip, 190 + references/eval-harness.md 337)
│       ├── incidentpilot-timescale-data.skill          (zip, 188 + references/schema-and-queries.md 621)
│       ├── incidentpilot-resilience-brownout.skill     (zip, 204-line SKILL.md)
│       ├── incidentpilot-nextjs-dashboard.skill        (zip, 162-line SKILL.md)
│       ├── incidentpilot-mlops-eval.eval-harness.md    (unpacked copy, 337 lines)
│       ├── incidentpilot-timescale-data.schema-and-queries.md (unpacked copy, 621 lines)
│       ├── IncidentPilot_starter_code.md               (766 lines, 8 parts)
│       ├── README.md                                   (92 lines)
│       └── test-run-iteration-1.md                     (120 lines)
├── IncidentPilot_Implementation_Plan.md                (Rev 1, 2,161 lines)
└── IncidentPilot_Implementation_Plan_REV2.md           (Rev 2, 2,863 lines)
```

**Verdict: zero implementation exists.** No `bot/`, no `dashboard/`, no `charts/`, no
`docker-compose.yml`, no `pyproject.toml`, no migrations, no tests, no CI workflows. Every artifact
in this plan is a net-new file.

Two consequences that shape the plan:

1. **There is nothing to refactor around and nothing to preserve.** Week 1 can establish the
   `domain/`-purity boundary correctly from the first commit, rather than retrofitting it — and that
   boundary is what makes the D2 replay harness possible at all. Retrofitting it in week 7 would
   cost more than the harness is worth.
2. **The skills are already installed as project rules** but describe a codebase that does not
   exist. Their "Definition of done" sections are therefore forward-looking acceptance criteria, and
   this plan schedules each one against the week that earns it (Appendix D).

## §0.2 What "done" means for this project

This is a **portfolio capstone for Junior SRE interviews in India**, not a product. That reframes
several trade-offs, and the plan applies the reframing consistently:

| Dimension | The bar | Why not higher |
|---|---|---|
| Correctness of the invariants | **Absolute.** Citation coverage 1.000, no orphaned channels, no lost messages | These are the interview claims. A single counter-example destroys the pitch |
| Test coverage | 85% overall, **100% on `domain/`** | `domain/` is pure and free to test; adapters need containers |
| Scale | ~30 incidents/day, 500-alert burst | Above this, §13.5's advisory-lock ceiling is the documented next step, not a build task |
| Multi-tenancy | **Out of scope** | Rev 2 §1.3 — three months of work, zero interview payoff |
| Paging transport | **Out of scope** (read schedules only) | Rev 2 §1.3 — carrier redundancy is a different product |
| Automated remediation | **Out of scope** | That is HealOps; IncidentPilot *observes* remediation |
| Uptime of the public demo | Best-effort on a €4 VPS | It must survive a six-month job search, not an SLA |

**The cut order if week 8 arrives with work outstanding** (Rev 2 §2.1, binding):
dashboard analytics pages → runbooks 7–8 → fatigue routing → *never* the eval harness or the
citation validator.

## §0.3 Document conventions and traceability IDs

| Prefix | Meaning | Example |
|---|---|---|
| `INV-n` | Invariant. Violating one is a build failure, not a bug | `INV-03` |
| `W<w>-<nn>` | Task, week `w`, sequence `nn` | `W3-07` |
| `F:<path>` | File to create | `F:bot/src/incidentpilot/domain/states.py` |
| `T:<name>` | Test that guards a task | `T:test_no_orphan_channel_on_crash` |
| `B-nn` | Rev 2 defect being fixed | `B-01` |
| `Dn` | Rev 2 differentiator being implemented | `D3` |
| `G<w>` | Week `w` acceptance gate (binary) | `G4` |
| `C-nn` | Source conflict resolved in §0.4 | `C-03` |
| `X-nn` | Defect found in the source material, §0.5 | `X-02` |

Every task row carries the invariants it protects and the defect or differentiator it serves, so a
reviewer can walk backwards from any line of code to the decision that justified it.

**Section references.** A bare `§n.n` in a task table's *Guards* column points at the section of
**Rev 2** that specifies the behaviour. References to *this* document are written with a section
name attached ("§13.5's advisory-lock ceiling", "the corpus buckets in §11.2"). Part 3 of this plan
deliberately mirrors Rev 2's Part IV numbering — §13 data, §15 observability, §16 security, §18
CI/CD — so the two documents can be read side by side; where the numbers coincide, so does the
subject.

## §0.4 Conflicts between sources, and how this plan resolves them

Reading Rev 2, the starter code, and the two reference files against each other surfaced **eleven**
places where they disagree. None is fatal; all would cost an hour each to discover mid-build. Each
resolution below is applied throughout Part 2.

---

**C-01 — `Settings.slack_signing_secret` is required in Rev 2, optional in the starter code**

- Rev 2 §5.2: `slack_signing_secret: SecretStr` (no default → required at import).
- Starter code Part 1: `slack_signing_secret: SecretStr | None = None`.

**Resolution: starter code wins.** Rev 2 §13.2 promises the demo runs with `IP_LLM_PROVIDER_SYNTH:
fake` and no API key, and Rev 2 §17.1 lists a `local` environment whose whole point is "anyone can
run the full demo with no API key." A required Slack secret makes `docker compose up` fail on a
clean clone, which breaks the single most valuable property of the repository. All three webhook
secrets are `SecretStr | None`, and `config/assert_invariants.py` (W1-09) enforces that they are
**present when `environment != "dev"`** — the check moves from import time to startup time, where it
can distinguish the two cases.

---

**C-02 — `docker-compose.yml` makes the Slack secret mandatory, contradicting the same section**

Rev 2 §13.2 sets `IP_SLACK_SIGNING_SECRET: ${SLACK_SIGNING_SECRET:?set in .env}`. The `:?` operator
makes Compose **abort** if the variable is unset — while the paragraph immediately below the YAML
says an interviewer can clone and run the demo without credentials.

**Resolution:** use `:-` with an obviously-fake default for the two Slack variables:

```yaml
IP_SLACK_SIGNING_SECRET: ${SLACK_SIGNING_SECRET:-dev-not-a-real-secret}
IP_SLACK_BOT_TOKEN:      ${SLACK_BOT_TOKEN:-xoxb-fake-for-demo}
IP_CHAT_PROVIDER:        ${CHAT_PROVIDER:-fake}
```

`IP_CHAT_PROVIDER: fake` is added because a fake *LLM* provider alone is not enough — the demo also
needs a fake *chat* provider or channel creation calls a Slack API that will reject the token. Rev 2
§13.2 omits this; it is required for the promise in its own prose to hold. See W8-11.

---

**C-03 — `correlate()` returns the first match above threshold in Rev 2, the best match in the starter code**

- Rev 2 §9.3: `if s >= CFG.merge_threshold: return Decision.merge(inc, score=s)` — returns on the
  **first** incident that clears the bar, in list order.
- Starter code Part 2: tracks `best` and returns the **highest-scoring** match.

**Resolution: starter code wins**, and this matters more than it looks. During a storm, several open
incidents can clear 0.62 simultaneously; first-match makes the merge target depend on the ordering of
`open_incidents`, which is a database sort order — so the same 40-alert fixture could produce
different results between runs. That is exactly the non-determinism the D2 replay harness is built to
exclude. Best-match is deterministic given a stable input set.

The starter code's severity guard (`if inc.severity_rank < alert.severity_rank: continue`) is also
absent from Rev 2 §9.3 despite Rev 2 §9.3 prose promising "correlation never merges across severity
boundaries." Keep the guard.

---

**C-04 — `fatigue_score()` signature differs**

- Rev 2 §9.5: `fatigue_score(r: ResponderWindow, now: datetime) -> float` — takes a clock.
- Starter code Part 1: `fatigue_score(r: ResponderWindow, max_pages: int = 2) -> float` — pure.

**Resolution: starter code wins.** `now` is never used in Rev 2's body, and `domain/` is required by
INV-01 to be free of clock access — a `datetime.now()` inside `domain/` is precisely the bug that
makes replay non-deterministic (`references/eval-harness.md` §11 lists "an unadaptered clock" as the
first cause of flaky replay). All time-window arithmetic happens in the repository query that builds
`ResponderWindow` (`Q7` in `schema-and-queries.md`), not in the scorer.

---

**C-05 — `EFFECTS` table: the starter code has two entries Rev 2 omits**

Starter code Part 1 adds `(S.PIR_FAILED, S.PIR_DRAFTED) → post_pir_skeleton` and gives
`(S.TRIAGING, S.MERGED)` its `post_merge_notice`.

**Resolution: starter code wins** — use its `EFFECTS` verbatim. Without `post_pir_skeleton`, the
`pir_failed → pir_drafted` transition fires no outbox event, so the skeleton is generated and
persisted but **never posted to Slack**. The incident would reach `pir_drafted` with nothing visible
in the channel, which silently breaks the G6 acceptance gate ("skeleton posted within 90 s") while
every unit test still passes. This is the highest-consequence conflict in the list.

---

**C-06 — `page_events` timestamp column is `paged_at` in Rev 2, `time` in the schema reference**

- Rev 2 §6.5 query 4: `WHERE p.paged_at > now() - INTERVAL '8 hours'`.
- `schema-and-queries.md` §7: `CREATE TABLE page_events (time TIMESTAMPTZ NOT NULL, ...)`.

**Resolution: schema reference wins.** `page_events` is a hypertable and TimescaleDB's time column
is `time` throughout the reference (consistent with `timeline_events`, `signal_samples`,
`llm_calls`). Rev 2's query would fail with `column p.paged_at does not exist`. Use `Q7` from the
reference verbatim.

---

**C-07 — `runbook_step_signals` is one-row-per-step, but Rev 2's efficacy query treats it as one-row-per-incident**

Rev 2 §6.5 query 2 selects `rs.steps_followed` and `rs.steps_total` from `runbook_step_signals` —
columns that do not exist. The reference schema defines the table as one row per *executed step*
with `UNIQUE (incident_id, step_id)`.

**Resolution: schema reference wins.** Use `Q4` from `schema-and-queries.md`, which computes
adherence with a `LEFT JOIN LATERAL (SELECT count(*) ...)` against the per-step rows and divides by
`array_length(r.step_ids, 1)`. Rev 2's version is a leftover from an earlier table shape.

---

**C-08 — `llm_cost_daily` uses a TimescaleDB Toolkit function in Rev 2, plain `avg()` in the reference**

- Rev 2 §6.3: `approx_percentile(0.95, percentile_agg(latency_ms)) AS p95_ms`.
- `schema-and-queries.md` §8: `avg(latency_ms) AS avg_ms`.

**Resolution: use the reference (`avg`) for the committed migration, and add the Toolkit p95 as a
second, optional aggregate.** `approx_percentile`/`percentile_agg` ship in **TimescaleDB Toolkit**,
which is a separate extension from `timescaledb`. It *is* present in the
`timescale/timescaledb-ha:pg18.4-ts2.29.2` image, so Rev 2's version works in Docker — but it will
fail on a plain `timescaledb` install, and the migration must therefore
`CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit` before using it. W2-04 adds that extension
guarded, and puts p95 in a separate cagg so a Toolkit-less environment degrades to the average
rather than failing `alembic upgrade head`.

---

**C-09 — `incidents` references `runbooks(id)` before `runbooks` exists**

Rev 2 §6.2 presents `services` → `incidents` → `alerts` → … but `incidents.runbook_id` has
`REFERENCES runbooks(id)` and `runbooks` is never defined in §6.2 at all (it appears only in
`schema-and-queries.md` §2).

**Resolution:** follow `schema-and-queries.md` §12's migration order, which is correct:
extensions → enums → **reference tables (`services`, `runbooks`, `responders`)** → `incidents` →
dependent tables → hypertables → compression → caggs → indexes. §13.2 of this plan writes it out as
an ordered checklist.

---

**C-10 — The intent ladder's layer 2 is specified but has no home in the file tree**

Both the Slack skill and Rev 2 §8 (pattern 4) describe a three-layer intent ladder:
regex → **small-embedding similarity against labelled exemplars** → the `extract` LLM role. Rev 2's
repo tree (§5.1) has `domain/intent.py` and the starter code implements only layer 1; there is no
module for layers 2 and 3, and no exemplar store.

**Resolution:** layer 1 ships in week 4 inside `domain/intent.py` (pure, no I/O — it must stay in
`domain/`). Layers 2 and 3 are **impure** (they call an embedding provider and the LLM router) and
therefore cannot live in `domain/` without breaking INV-01. They go in a new
`F:bot/src/incidentpilot/transcript/classifier.py` that composes `detect_intent()` with the two
escalation layers, plus `F:bot/src/incidentpilot/transcript/exemplars.yaml`. Layer 2 is scheduled
W4-09 and layer 3 W6-14; both are on the §0.2 cut list ahead of the eval harness.

---

**C-11 — `storm_threshold` and `merge_threshold` settings are defined but never consumed**

`merge_threshold: 0.62` and `storm_threshold: 5` appear in the starter code's `Settings`;
`merge_threshold` is used by `correlate()`, but `storm_threshold` is used nowhere in any source.

**Resolution:** `storm_threshold` gates the **storm banner and the metric**, not the merge decision.
When an incident's `correlated_alert_count` crosses it, W2-11 emits a single throttled channel
update and increments `STORM_COMPRESSION`. Without this the D3 demo has no visible "40 correlated
alerts" line, which is the moment the demo is built around (Rev 2 §2.2, 0:12).

## §0.5 Defects in the source material that this plan fixes

Distinct from §0.4 (disagreements between sources), these are places where a source is **wrong on
its own terms** — the code or SQL will not run.

---

**X-01 — 🔴 `schema-and-queries.md` Q5 ("dead steps") is not valid SQL**

```sql
-- As written in references/schema-and-queries.md §10, Q5 — DOES NOT RUN
SELECT r.name, step, uses, skipped, round(skipped::numeric/uses, 2) AS skip_rate
FROM (
  SELECT r.id, r.name, unnest(r.step_ids) AS step,
         count(DISTINCT i.id) AS uses,
         count(DISTINCT i.id) FILTER (
           WHERE NOT EXISTS (SELECT 1 FROM runbook_step_signals rs
                             WHERE rs.incident_id = i.id AND rs.step_id = unnest_step)) AS skipped
  FROM runbooks r JOIN incidents i ON i.runbook_id = r.id
  CROSS JOIN LATERAL unnest(r.step_ids) AS unnest_step
  GROUP BY r.id, r.name, step
) t JOIN runbooks r ON r.id = t.id
WHERE uses >= 5 AND skipped::numeric/uses > 0.8;
```

Three independent errors: `unnest()` in the select list *and* a `CROSS JOIN LATERAL unnest()` both
expand the array, producing a cross product; `unnest_step` is referenced in the `FILTER` but is a
different expansion than `step`; and the outer query aliases `runbooks` as `r` while `t` already
exposes `r.name`, so `r.name` is ambiguous.

**Corrected version, committed as `Q5` in W5-14** (expand once, in the lateral, and reference that
one alias throughout):

```sql
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
WHERE s.uses >= 5 AND s.skipped::numeric / s.uses > 0.8
ORDER BY skip_rate DESC;
```

This query is the **input to differentiator D4's auto-PR**, so shipping the broken version would
mean the runbook-efficacy job silently produces nothing. `T:test_dead_step_query_runs` (W5-14)
executes it against a seeded Testcontainer.

---

**X-02 — 🟠 The `alerts` unique constraint cannot express a re-fire**

`schema-and-queries.md` §3 and Rev 2 §6.2 both declare `UNIQUE (fingerprint, starts_at)` on
`alerts`. Alertmanager re-sends a firing alert on every `repeat_interval` (default 4 h) **with the
same `startsAt`** — `startsAt` is the time the alert *began*, not the time it was sent. So the
natural dedup path (insert, catch `IntegrityError`) is correct, but the table can never record that
the alert re-fired, and `received_at` on the surviving row is the *first* receipt, not the latest.

**Resolution:** keep the constraint (it is the correct idempotency guard) and add
`last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()` plus `seen_count INT NOT NULL DEFAULT 1`, updated
with `ON CONFLICT (fingerprint, starts_at) DO UPDATE SET last_seen_at = now(), seen_count =
alerts.seen_count + 1`. This is a two-column addition that turns a silent no-op into evidence, and
"Alertmanager re-sent this 4 times before we mitigated" is a real PIR sentence. W2-03.

---

**X-03 — 🟠 `dedup_epoch` has no writer**

Rev 2 §5.5 and the ingest skill both explain that `dedup_epoch` "bumps when an incident closes, so
the same alert can recur next week," and `UNIQUE (dedup_key, dedup_epoch)` depends on it. **No source
contains the code that increments it**, and no source says where the *next* epoch for a given
`dedup_key` is read from.

**Resolution (W2-06):** epoch is derived, never stored globally. The open-incident lookup
(`WHERE dedup_key = :key AND state NOT IN (terminal)`) is attempted first; on miss, insert with:

```sql
INSERT INTO incidents (dedup_key, dedup_epoch, ...)
VALUES (:key,
        COALESCE((SELECT max(dedup_epoch) + 1 FROM incidents WHERE dedup_key = :key), 0),
        ...)
```

The subquery is evaluated inside the same transaction that holds the advisory lock on the
correlation key, so two workers cannot both compute the same epoch — and if they somehow do, the
unique constraint is still the arbiter (INV-04). `T:test_dedup_epoch_advances` covers
close-then-refire.

---

**X-04 — 🟡 `signal_samples` volume math needs a sampling rate that no source states**

Rev 2 §6.4 justifies TimescaleDB on "20 series × 1-second resolution × 60 minutes = 72,000 rows per
incident." But `impact/sampler.py` is never specified, and 1-second resolution over an hour requires
either a 3,600-point PromQL range query per series (`step=1s`) or a polling loop. Prometheus'
default scrape interval is 15 s, so **1-second resolution does not exist in the source data** — a
`step=1s` query returns an interpolated staircase, not 72,000 distinct observations.

**Resolution:** be honest and keep the conclusion. The sampler issues one `query_range` per series
with `step` equal to the scrape interval (default 15 s), giving 240 points/series/hour × 20 series =
4,800 rows/incident, ~144 K rows/month. **That is not a hypertable justification on its own.** What
survives is: (a) `signal_samples` is still the largest table by an order of magnitude, (b)
declarative retention and compression are the real wins, (c) `time_bucket_gapfill()` is genuinely
needed for the incident chart. §13.5 rewrites the `SCALING_DECISION.md` paragraph accordingly and
records this as a *second* correction to the same number — entirely consistent with Rev 2 §6.4's own
principle that "a decision document that hides its own revision is worthless."

Flagged in Appendix E as a decision you may want to take differently: setting the demo cluster's
scrape interval to 5 s and the sampler to `step=5s` restores the original order of magnitude
honestly, at the cost of a non-default Prometheus config.

---

# Part 1 — Foundations

## §1 The twelve invariants

Distilled from the eight skills' "Critical rules (never violate)" sections. Every one has a test
that fails the build. Listed in the order an interviewer would discover a violation.

| ID | Invariant | Enforced by | Source |
|---|---|---|---|
| **INV-01** | `domain/` is pure: no I/O, no clock, no randomness, no imports outside `domain/` and stdlib | `T:test_domain_purity` (AST scan) | all skills; replay determinism |
| **INV-02** | `conversations.history` / `conversations.replies` are never called outside `reconciler.py` | `T:test_history_never_called_on_hot_path`; `FakeChat.fetch_history` raises | slack-platform; B-01 |
| **INV-03** | Every external write goes through the outbox relay; no adapter is called from `api/` or `domain/` | `T:test_no_external_writes_outside_relay` (import graph) | state-orchestration; B-08 |
| **INV-04** | Dedup and idempotency identity live in Postgres unique constraints, never in a cache key | `T:test_dedup_survives_cache_flush` | ingest-correlation; B-07 |
| **INV-05** | `Claim.citations` has `min_length=1`; an uncited claim is unrepresentable | Pydantic schema + `T:test_uncited_claim_fails_parse` | pir-grounding; B-09 |
| **INV-06** | Numbers in a PIR are computed from PromQL and injected; never generated | `_extract_numbers` + `T:test_ungrounded_number_rejected` | pir-grounding; B-10 |
| **INV-07** | No vendor or model string in `bot/src/` outside `config/` | CI grep job (§18.1) | pir-grounding; §D |
| **INV-08** | Every timestamp is `TIMESTAMPTZ`, stored UTC | CI query on `information_schema.columns` | timescale-data; B-06 |
| **INV-09** | Compression `segmentby` is the low-cardinality column | `T:test_compression_ratio_is_sane` (≥ 8×) | timescale-data; B-05 |
| **INV-10** | Replay performs zero network calls | socket guard around `Replayer.run()` | mlops-eval; D2 |
| **INV-11** | `IncidentPilotDown` routes to a receiver that does not traverse IncidentPilot | Alertmanager route + `T:test_fallback_route_present` | resilience-brownout; D7 |
| **INV-12** | Degradation is announced: every level change posts a banner and moves the gauge | `T:test_degradation_announces` | resilience-brownout; D7 |

**INV-01 deserves its enforcement spelled out**, because everything else depends on it and it is
trivially violated by an innocent `datetime.now()`:

```python
# bot/tests/unit/test_domain_purity.py
FORBIDDEN_MODULES = {
    "asyncio", "httpx", "requests", "sqlalchemy", "redis", "psycopg",
    "slack_sdk", "slack_bolt", "openai", "anthropic", "random", "uuid",
    "incidentpilot.db", "incidentpilot.adapters", "incidentpilot.api",
    "incidentpilot.orchestration", "incidentpilot.telemetry",
}
FORBIDDEN_CALLS = {("datetime", "now"), ("datetime", "utcnow"), ("time", "time")}

def test_domain_purity() -> None:
    """domain/ must be a pure function library. This is what makes 40 incidents
    replayable in nine seconds, and it is the first thing to rot if unguarded."""
    for path in (SRC / "domain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert _root_module(node) not in FORBIDDEN_MODULES, f"{path}: impure import"
            if isinstance(node, ast.Call) and _is_forbidden_call(node, FORBIDDEN_CALLS):
                raise AssertionError(f"{path}: clock access in domain/")
```

`random` and `uuid` are forbidden alongside the I/O modules — `references/eval-harness.md` §11 names
both as causes of run-to-run replay drift, and they are far easier to add accidentally than an HTTP
client.

## §2 Repository scaffold — complete file manifest

Week tags say when a file is created. This is the full tree; nothing else gets created.

### 2.1 `bot/` — the application

```
bot/
├── pyproject.toml                                    W1  deps, ruff, mypy strict, pytest config
├── uv.lock                                           W1  committed; CI uses --frozen
├── Dockerfile                                        W1  multi-stage, non-root 10001, HEALTHCHECK
├── .dockerignore                                     W1
├── alembic.ini                                       W2
├── src/incidentpilot/
│   ├── __init__.py                                   W1  __version__
│   ├── main.py                                       W1  FastAPI factory + lifespan
│   ├── config/
│   │   ├── settings.py                               W1  pydantic-settings, env_prefix=IP_
│   │   ├── models.yaml                               W6  LLM ROLES — only place a model name exists
│   │   ├── models_config.py                          W6  loader + ModelSpec + chain_for(role)
│   │   ├── correlation.yaml                          W2  window, merge threshold, storm threshold
│   │   ├── service-graph.yaml                        W2  D3 fallback when TraceMap is absent
│   │   └── assert_invariants.py                      W1  startup + CI guard (C-01)
│   ├── domain/                    # PURE — INV-01. No I/O, no clock, no randomness.
│   │   ├── states.py                                 W2  13 states, TRANSITIONS, EFFECTS, TIMING_FIELD
│   │   ├── fingerprint.py                            W1  STABLE_LABELS, fingerprint, dedup_key
│   │   ├── normalize.py                              W1  raw webhook payload -> NormalizedAlert
│   │   ├── correlation.py                            W2  Decision, jaccard, correlate, pick_root_signal
│   │   ├── graph.py                                  W2  ServiceGraph: distance(), depth()
│   │   ├── severity.py                               W2  severity + severity_reason derivation
│   │   ├── intent.py                                 W4  ladder layer 1 (regex), SNAPSHOT_TRIGGERS
│   │   ├── fatigue.py                                W5  fatigue_score, routing_decision
│   │   ├── evidence.py                               W6  Citation kinds, reference-ID grammar
│   │   └── redaction_rules.py                        W6  pure pattern set (presidio call lives outside)
│   ├── api/
│   │   ├── security.py                               W1  verify_bearer / verify_slack / verify_paging
│   │   ├── deps.py                                   W1  DI: session, valkey, settings, clock
│   │   ├── health.py                                 W1  /healthz /readyz /metrics
│   │   ├── webhooks/
│   │   │   ├── alertmanager.py                       W1  bearer only — Alertmanager cannot sign (B-02)
│   │   │   ├── paging.py                             W1  multi-signature HMAC (rotation)
│   │   │   ├── slack.py                              W4  v0 HMAC over RAW body
│   │   │   └── deploy.py                             W6  CI deploy events -> PIR evidence
│   │   ├── slash/{resolve,update,escalate,metrics,falsepositive,split,step}.py   W5
│   │   └── query/{incidents,pirs,analytics,runbooks}.py                          W8
│   ├── orchestration/
│   │   ├── consumer.py                               W2  XREADGROUP + advisory lock + XAUTOCLAIM
│   │   ├── orchestrator.py                           W2  transition() — the 30 important lines
│   │   ├── outbox.py                                 W3  enqueue, CLAIM, relay_once, backoff, DLQ
│   │   ├── handlers.py                               W3  action name -> adapter call (relay only)
│   │   ├── reconciler.py                             W4  transcript + orphan-channel reconciliation
│   │   └── scheduler.py                              W5  arq: nudges, SLA timers, abandonment
│   ├── transcript/
│   │   ├── ingestor.py                               W4  message event -> slack_messages + timeline
│   │   ├── mutations.py                              W4  message_changed / message_deleted
│   │   ├── classifier.py                             W4  intent ladder L2/L3 composition (C-10)
│   │   └── exemplars.yaml                            W4  labelled exemplars for L2
│   ├── impact/
│   │   ├── promql.py                                 W6  IMPACT_QUERIES, deterministic compute
│   │   ├── sampler.py                                W6  signal_samples capture (X-04)
│   │   └── error_budget.py                           W6  burn from services.slo_target
│   ├── pir/
│   │   ├── context.py                                W6  GroundedContext, valid_reference_set()
│   │   ├── schema.py                                 W6  Citation, Claim, ActionItem, PIRDraft
│   │   ├── generator.py                              W6  3-layer fallback chain
│   │   ├── validator.py                              W6  CitationValidator — deterministic gate
│   │   ├── skeleton.py                               W6  deterministic markdown floor
│   │   ├── renderer.py                               W6  JSON -> Block Kit + Markdown
│   │   └── prompts/{registry.py,registry.yaml,pir_v2_1_0.md,extract_v1_4_0.md}   W6
│   ├── adapters/
│   │   ├── chat/{base,slack,fake,ratelimit,block_kit}.py                    W3/W4
│   │   ├── paging/{base,pagerduty,static_schedule,fake}.py                  W5
│   │   ├── metrics/{base,prometheus,fake}.py                                W6
│   │   └── llm/{base,router,openai_provider,anthropic_provider,local_provider,fake}.py  W6
│   ├── runbooks/{matcher,renderer,parser,detector}.py                       W5
│   ├── runbooks/efficacy.py                                                 post-MVP (see note)
│   ├── privacy/{redactor,recognizers}.py                                    W6
│   ├── resilience/{breaker,retry,ratelimit}.py       W3
│   ├── resilience/budget.py                          W6  three-tier BudgetBreaker
│   ├── resilience/degradation.py                     W7  DegradationManager, 4 levels
│   ├── db/{engine,models,repositories}.py            W2
│   ├── db/queries.py                                 W8  the 12 analytics queries as functions
│   └── telemetry/{metrics,tracing,logging}.py        W1
├── migrations/
│   ├── env.py                                        W2
│   └── versions/
│       ├── 0001_extensions_enums_reference.py        W2
│       ├── 0002_incident_core.py                     W2
│       ├── 0003_transcript_outbox.py                 W3
│       ├── 0004_hypertables_policies.py              W2
│       ├── 0005_caggs.py                             W2
│       ├── 0006_pir_actions_runbook_signals.py       W6
│       └── 0007_model_deployments.py                 W7
├── eval/
│   ├── run.py  recorder.py  replayer.py  metrics.py  gate.py  report.py     W6/W7
│   ├── fakes/{chat,paging,metrics,llm}.py                                   W7
│   ├── judge/{rubric.md,agreement.py}                                       W7
│   └── corpus/{manifest.json,incident_*.jsonl}                              W7
└── tests/{conftest.py,unit/,integration/,replay/,load/}                     W1-W8
```

> **`runbooks/efficacy.py` is post-MVP, deliberately.** D4's weekly auto-PR job cannot produce
> anything until ≥ 5 incidents per runbook exist (`Q5`'s `uses >= 5` filter). Build the **signal
> capture and the query** in week 5 — those are what the interview answer actually describes — and
> ship the PR-opening job afterwards. Claiming a closed loop that has never closed is exactly what a
> follow-up question exposes.

### 2.2 The rest of the repository

```
dashboard/                          W8   Next.js 16.3, Node 24 LTS, App Router, TS strict
├── app/                                 routes per the nextjs-dashboard skill
├── components/{ui,pir,incidents,charts}/
├── lib/{api.ts,zod-schemas.ts,tokens.ts}
├── Dockerfile  package.json  next.config.ts  tailwind.config.ts

lifeboat/                           W7   SEPARATE image — no application imports
├── main.py  (~40 lines)  Dockerfile  pyproject.toml

charts/incidentpilot/               W8   Helm 4.2.4, apiVersion v2
├── Chart.yaml  values.yaml  values-demo.yaml
└── templates/{api,worker,relay}-deployment.yaml, service.yaml, hpa.yaml,
    pdb.yaml, networkpolicy.yaml, servicemonitor.yaml, lifeboat-cronjob.yaml,
    cronjobs.yaml, externalsecret.yaml, NOTES.txt
    # NOTE: no secrets.yaml — ever (Rev 2 §13.3)

deploy/compose/init/01-extensions.sql          W2
deploy/k3s/{README.md,bootstrap.sh}            W8

monitoring/
├── prometheus/{prometheus.yml,rules/incidentpilot-slo.yml}   W1/W7
├── alertmanager/alertmanager.yml                             W1  incl. fallback route (INV-11)
└── grafana/provisioning/dashboards/{incident-ops.json,incidentpilot-slo.json}  W8

runbooks/                           W5   8 markdown runbooks with step: ids (Appendix C)
scripts/{demo.sh,smoke.sh,seed.py,record.sh}                  W7/W8
docs/
├── ARCHITECTURE.md                 W8
├── SCALING_DECISION.md             W2 (database) + W3 (partitioning) — two decisions, one file
├── SLO.md                          W1
├── FMEA.md                         W7  18 rows with residual risk
└── ADR/0001..0009-*.md             ongoing
.github/workflows/{ci.yml,release.yml,eval-nightly.yml}       W1/W8/W7
docker-compose.yml  Makefile  README.md  .env.example         W1/W8
```

The three files that carry the most interview weight are `docs/SCALING_DECISION.md`,
`bot/eval/gate.py`, and `bot/src/incidentpilot/pir/validator.py`.

## §3 Toolchain, pins, and the reproducible-build contract

All pins are Rev 2 §C, verified 5 September 2026. **Re-verify before starting** — Rev 2 notes three
of them moved in the two weeks before it was written.

### 3.1 `bot/pyproject.toml` — the authoritative dependency set

```toml
[project]
name = "incidentpilot"
version = "0.1.0"
requires-python = "==3.14.*"          # pin the minor: litellm and several SDKs cap at <3.15
dependencies = [
  "fastapi==0.141.1",
  "uvicorn[standard]==0.52.4",
  "pydantic==2.13.5",
  "pydantic-settings==2.15.0",
  "sqlalchemy==2.0.52",
  "alembic==1.19.2",
  "psycopg[binary,pool]==3.3.5",      # NOT asyncpg — Rev 2 §C.1
  "redis==8.1.0",
  "slack-bolt==1.30.0",               # bumped from the portfolio's 1.28.x
  "slack-sdk==3.44.1",
  "httpx==0.28.1",
  "tenacity==9.1.4",
  "pybreaker==1.4.1",
  "structlog==26.1.0",
  "prometheus-client==0.26.0",
  "opentelemetry-sdk==1.44.0",
  "arq==0.28.0",
  "presidio-analyzer==2.2.364",
  "pyyaml==6.0.2",
]

[project.optional-dependencies]
llm = ["openai==3.8.0", "anthropic==1.4.0"]
dev = ["ruff==0.16.6", "mypy==2.3.1", "pytest==9.1.1", "pytest-asyncio==1.4.0",
       "pytest-cov", "hypothesis", "testcontainers==4.15.0", "respx", "freezegun"]

[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["integration: needs Testcontainers", "replay: eval corpus"]
```

Two notes worth a sentence in an interview:

- **`openai` and `anthropic` are optional extras, not core dependencies.** The adapter *interface*
  is core; the implementations are not. That is what provider-agnosticism means structurally rather
  than aspirationally — `uv sync` without either extra still starts the app, on the fake or local
  provider.
- **pytest 9.1.1 removed several deprecated hooks** (Rev 2 §C.1). Do not copy `conftest.py` patterns
  from pre-9 projects.

### 3.2 Image pins (no `latest`, ever)

```
timescale/timescaledb-ha:pg18.4-ts2.29.2    # includes Toolkit (see C-08)
valkey/valkey:9.1.2-alpine
prom/prometheus:v3.14.0
prom/alertmanager:v0.34.0                    # reason-label change — see §15.4
grafana/grafana:13.2.1
python:3.14.7-slim-trixie
node:24-alpine
ghcr.io/astral-sh/uv:0.12.10
```

**PostgreSQL 18.5 was never shipped** (regression) — do not reference it anywhere, including in
comments. TimescaleDB must be **≥ 2.29.1** (three security advisories fixed there).

### 3.3 The reproducibility contract

| Artifact | Mechanism | Verified by |
|---|---|---|
| Python deps | `uv.lock` committed, CI runs `uv sync --frozen` | CI fails on drift |
| Container base | Digest-pinned in the chart, tag-pinned in compose | `helm template` diff |
| Prompts | `sha256` in `registry.yaml`, stored per PIR row | `T:test_prompt_hash_matches_file` |
| Eval baseline | `corpus/manifest.json`, moved only by a human | Gate reads it, never a workflow literal |
| Schema | Alembic forward-only, `upgrade head` from empty | CI job on a Testcontainer |

## §4 The dependency order

```
     settings.py ──┬──► telemetry ──► main.py ──► health.py
                   │
   domain/ (pure) ─┴──► api/security.py ──► webhooks ──► Valkey Streams
        │                                                     │
        │                                                     ▼
        └──────────────────► db/models + migrations ──► consumer ──► orchestrator
                                      │                                   │
                                      ▼                                   ▼
                                 outbox table ◄──────────────────── transition()
                                      │
                                      ▼
                              relay + handlers ──► chat adapter ──► Slack
                                                        │
                                                        ▼
                                             transcript/ingestor ──► timeline_events
                                                        │
                                       impact/promql ───┴──► pir/context ──► pir/generator
                                                                                  │
                                                                    eval/recorder ┴► replayer ──► gate
```

**The two ordering constraints that actually bite:**

1. **`domain/` before everything.** Weeks 1–2 write pure logic first and wire it second. Written the
   other way round, `datetime.now()` and a database session leak into `correlation.py` within a day,
   and INV-01 is unrecoverable without a rewrite.
2. **The outbox before the first Slack call.** Week 3 must not call `conversations.create` from
   anywhere except `handlers.py`. If a "temporary" direct call appears to see a channel show up,
   INV-03 is dead and the G3 crash tests cannot pass.

---

# Part 2 — Week-by-week execution

Each week has: **objectives**, a **task table** (ID · task · files · invariants/defects · test),
**code contracts** for the non-obvious pieces, and a **binary acceptance gate**. A week is not done
until its gate passes; per Rev 2 §2.1, you do not proceed on a red gate.

---

## §5 Week 1 — Ingest and the trust boundary

**Objective.** A webhook that a stranger cannot forge, that returns 202 in under 250 ms, and that
puts a normalized alert on a durable stream. Plus the `domain/` purity boundary and the telemetry
that every later week depends on.

**Why this order.** Ingest is the only subsystem whose SLO (99.9% availability, p99 < 250 ms) is
externally visible from day one, and `api/security.py` is the file most likely to be probed in an
interview. Building it first also forces the `domain/` boundary before there is anything to be lazy
about.

### 5.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W1-01 | Repo skeleton, `uv init`, `pyproject.toml`, `uv.lock`, ruff + mypy strict clean on an empty tree | `F:bot/pyproject.toml`, `F:bot/uv.lock` | §3.3 | `make lint` |
| W1-02 | `Settings` with `env_prefix="IP_"`, all secrets `SecretStr \| None` (C-01) | `F:.../config/settings.py` | C-01 | `T:test_settings_load_without_secrets` |
| W1-03 | `assert_invariants.py`: in non-dev, all three webhook secrets present and `require_citations is True` | `F:.../config/assert_invariants.py` | INV-05, C-01 | `T:test_prod_config_rejects_missing_secret` |
| W1-04 | structlog JSON logging; bind `incident_id`, `trace_id`, `state` into context | `F:.../telemetry/logging.py` | §15 | `T:test_log_line_is_json_with_incident_id` |
| W1-05 | The 15 Prometheus metrics from Rev 2 §15.1, verbatim names | `F:.../telemetry/metrics.py` | §15.1 | `T:test_metric_names_stable` |
| W1-06 | OTel tracing setup; span per webhook, propagated into stream fields | `F:.../telemetry/tracing.py` | §15 | manual |
| W1-07 | `domain/fingerprint.py` — `STABLE_LABELS`, `fingerprint()`, `dedup_key()` | `F:.../domain/fingerprint.py` | INV-01 | `T:test_fingerprint_ignores_volatile_labels` |
| W1-08 | `domain/normalize.py` — Alertmanager v4 / paging / deploy payload → `NormalizedAlert` | `F:.../domain/normalize.py` | INV-01 | contract fixtures |
| W1-09 | **`test_domain_purity`** — the AST scan from §1 | `F:bot/tests/unit/test_domain_purity.py` | **INV-01** | itself |
| W1-10 | `api/security.py` — `verify_bearer`, `verify_slack`, `verify_paging` (multi-sig) | `F:.../api/security.py` | B-02 | `T:test_security_*` (6 cases) |
| W1-11 | `/webhooks/alertmanager` — bearer only, raw-body parse, `XADD`, 202 | `F:.../api/webhooks/alertmanager.py` | B-02 | `T:test_alertmanager_bad_bearer_401` |
| W1-12 | `/webhooks/paging` — multi-signature HMAC accept-if-any | `F:.../api/webhooks/paging.py` | B-02 | `T:test_paging_accepts_rotated_signature` |
| W1-13 | Valkey Streams producer; `MAXLEN ~ 100000`; `alerts.raw` / `alerts.resolved` | `F:.../orchestration/streams.py` | §7.2 | `T:test_xadd_visible_via_xrange` |
| W1-14 | `/healthz` (liveness: process up), `/readyz` (DB + Valkey reachable), `/metrics` | `F:.../api/health.py` | §13.3 | `T:test_readyz_fails_when_db_down` |
| W1-15 | `main.py` app factory + lifespan (pools open/close, graceful SIGTERM hook) | `F:.../main.py` | §12.3 | `T:test_sigterm_drains` |
| W1-16 | `Dockerfile` multi-stage, UID 10001, read-only-rootfs-compatible, HEALTHCHECK | `F:bot/Dockerfile` | §16 | `docker build` in CI |
| W1-17 | `docker-compose.yml` with the C-02 fixes; `.env.example` | `F:docker-compose.yml`, `F:.env.example` | C-02 | `T:test_compose_up_without_env` |
| W1-18 | `ci.yml` — `changes`, `lint-and-type`, `test`, `security` jobs (eval-gate added W7) | `F:.github/workflows/ci.yml` | B-11 | green on PR |
| W1-19 | `docs/SLO.md` — the eight SLIs from Rev 2 §1.4, **written before the code they measure** | `F:docs/SLO.md` | §1.4 | review |
| W1-20 | `monitoring/alertmanager/alertmanager.yml` with the `route: fallback` receiver | `F:monitoring/alertmanager/alertmanager.yml` | **INV-11** | `T:test_fallback_route_present` |

### 5.2 Code contract — the webhook handler

The single most important property is that **nothing slow happens before the 202**. Rev 2 §4.3: the
webhook contract is "I have durably accepted this," and Alertmanager's HTTP timeout is not your
orchestration budget.

```python
# api/webhooks/alertmanager.py
@router.post("/webhooks/alertmanager", status_code=202)
async def receive(request: Request, authorization: str | None = Header(default=None)):
    """Alertmanager does NOT sign payloads — no HMAC header exists. `http_config` offers
    basic auth, bearer tokens, OAuth2 and TLS, nothing more. The trust model here is
    bearer + mTLS + NetworkPolicy allowlist: the source cannot prove identity, so the
    network does it for them. (B-02)"""
    verify_bearer(authorization, settings.alertmanager_bearer.get_secret_value())
    raw = await request.body()                  # raw bytes; we parse, not FastAPI
    payload = json.loads(raw)
    accepted = 0
    for alert in payload.get("alerts", []):
        stream = "alerts.resolved" if alert.get("status") == "resolved" else "alerts.raw"
        await streams.xadd(stream, normalize(alert, payload))
        accepted += 1
    WEBHOOK_LATENCY.labels(source="alertmanager", outcome="ok").observe(request.state.elapsed)
    return {"accepted": accepted}
```

Three details that are easy to get wrong and expensive to debug:

- **Read the raw body, parse it yourself.** For the Slack endpoint (W4) the HMAC is over the raw
  bytes; a re-serialized body produces a different hash and *every* request fails signature
  verification. Establishing the `await request.body()` habit in week 1 means week 4 inherits it.
- **`resolved` alerts go to a different stream.** They drive `mitigated`/auto-resolve logic, not
  incident creation. Mixing them into `alerts.raw` means a resolve can create an incident.
- **The `elapsed` value comes from middleware**, not from a `time.time()` in the handler — the
  handler must stay free of clock reads so the same code path is replayable.

### 5.3 The three trust models, as shipped

| Source | Mechanism | Code path | Failure mode if wrong |
|---|---|---|---|
| Alertmanager | Bearer + mTLS + NetworkPolicy | `verify_bearer`, constant-time | Anyone who can reach the pod can create incidents |
| Paging provider | HMAC-SHA256, `v1=` prefix, **accept if any of several match** | `verify_paging` | Key rotation causes a hard outage of on-call callbacks |
| Slack | v0 HMAC over `v0:{ts}:{raw}`, ≤ 300 s replay window | `verify_slack` (W4) | Replay attacks; forged `/resolve` |
| Deploy (CI) | Bearer + repo allowlist | `verify_bearer` (W6) | Fabricated deploy evidence in a PIR |

The interview sentence, memorized: *"Three sources, three trust models. Slack signs, PagerDuty signs
with rotation support, Alertmanager doesn't sign at all — so that endpoint gets mTLS plus a bearer
plus a NetworkPolicy, because it's the one that can't prove who it is."*

### 5.4 🚦 G1 — acceptance gate

```bash
# 1. Forged request is rejected
curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8000/webhooks/alertmanager \
     -H 'Authorization: Bearer wrong' -d @fixtures/alertmanager_firing.json      # → 401

# 2. Good request accepted fast, 100 iterations, p99 measured
./scripts/bench_ingest.sh --n 100 --p99-max-ms 250                               # → PASS

# 3. The event is durably on the stream
valkey-cli XRANGE alerts.raw - + COUNT 1                                         # → 1 entry
```

Plus `T:test_domain_purity` green, `mypy --strict` clean, and `docker compose up` succeeding on a
clone with **no `.env` file present** (C-02).

---

## §6 Week 2 — Correlation and the state machine

**Objective.** Forty alerts become one incident. Thirteen states, enforced in Python and in the
database. The full schema exists and `alembic upgrade head` runs from empty.

**Why this order.** Correlation must exist before channel creation (week 3), or week 3 builds the
B-03 bug (one channel per alert) and then has to unbuild it. This is the sequencing decision that
Rev 1 got wrong.

### 6.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W2-01 | Alembic init; `env.py` wired to `settings.database_url` | `F:bot/alembic.ini`, `F:bot/migrations/env.py` | §13.2 | `alembic upgrade head` |
| W2-02 | Migration 0001 — extensions, enums, `services`/`runbooks`/`responders` (C-09 order) | `F:.../versions/0001_*.py` | C-09, INV-08 | `T:test_schema_has_no_naked_timestamp` |
| W2-03 | Migration 0002 — `incidents`, `alerts` (+`last_seen_at`,`seen_count` per **X-02**), `incident_transitions`, generated columns, partial + HNSW indexes | `F:.../versions/0002_*.py` | X-02, INV-08 | `T:test_generated_columns_compute` |
| W2-04 | Migration 0004 — hypertables + compression + retention; Toolkit guarded (C-08) | `F:.../versions/0004_*.py` | INV-09, C-08 | `T:test_compression_ratio_is_sane` |
| W2-05 | Migration 0005 — the three continuous aggregates + refresh policies | `F:.../versions/0005_*.py` | §6.3 | `T:test_cagg_refreshes` |
| W2-06 | `db/models.py`, `repositories.py`, UoW; **`dedup_epoch` derivation (X-03)** | `F:.../db/models.py`, `F:.../db/repositories.py` | X-03, INV-04 | `T:test_dedup_epoch_advances` |
| W2-07 | `domain/states.py` — 13 states, `TRANSITIONS`, `TERMINAL`, `EFFECTS` (**starter-code version, C-05**), `TIMING_FIELD`, `assert_transition` | `F:.../domain/states.py` | B-13, C-05, INV-01 | `T:test_no_path_reaches_invalid_state` (hypothesis) |
| W2-08 | `domain/graph.py` — `ServiceGraph.distance()`, `.depth()`, loaded from `service-graph.yaml` or TraceMap | `F:.../domain/graph.py`, `F:.../config/service-graph.yaml` | D3, INV-01 | `T:test_graph_depth_stable` |
| W2-09 | `domain/correlation.py` — **best-match** scorer with severity guard (**C-03**), `pick_root_signal` | `F:.../domain/correlation.py` | D3, C-03, INV-01 | `T:test_storm_compresses_to_one_incident` |
| W2-10 | `domain/severity.py` — severity + `severity_reason` string | `F:.../domain/severity.py` | INV-01 | `T:test_severity_reason_is_auditable` |
| W2-11 | Storm banner + `STORM_COMPRESSION` metric on `correlated_alert_count > storm_threshold` (**C-11**) | `F:.../orchestration/consumer.py` | C-11, D3 | `T:test_storm_banner_throttled` |
| W2-12 | `orchestration/orchestrator.py` — `transition()` with `FOR UPDATE` + seq guard + outbox enqueue | `F:.../orchestration/orchestrator.py` | B-08, INV-03 | `T:test_concurrent_transition_one_wins` |
| W2-13 | `orchestration/consumer.py` — `XREADGROUP`, advisory lock, `XACK`, `XAUTOCLAIM` reclaim loop | `F:.../orchestration/consumer.py` | Rev 2 §7.2, §13.5 | `T:test_stale_entry_reclaimed` |
| W2-14 | 40-alert storm fixture + 5-alert and 12-alert fixtures | `F:bot/tests/fixtures/storm_{5,12,40}.json` | D3 | used by G2 |
| W2-15 | `docs/SCALING_DECISION.md` — the **database** decision, with the corrected arithmetic *and* the X-04 second correction | `F:docs/SCALING_DECISION.md` | B-04, X-04 | review |

### 6.2 Code contract — correlation (the C-03 resolution, shipped)

```python
# domain/correlation.py — pure. No clock, no DB, no config import: cfg is passed in.
def correlate(alert, open_incidents, graph, cfg) -> Decision:
    """Three independent signals, weighted, best match wins.

    Best-match rather than first-match (C-03): during a storm several open incidents
    can clear the threshold at once, and first-match would make the merge target depend
    on the database sort order of `open_incidents` — non-determinism that would defeat
    the replay harness."""
    best = Decision.new_incident()
    for inc in open_incidents:
        if inc.severity_rank < alert.severity_rank:
            continue                       # never absorb a more severe incident
        score, why = 0.0, []
        dt = (alert.starts_at - inc.detected_at).total_seconds()
        if 0 <= dt <= cfg.correlation_window_s:
            score += 0.4 * (1 - dt / cfg.correlation_window_s)
            why.append(f"{int(dt)}s apart")
        d = graph.distance(alert.service, inc.primary_service)
        if d is not None and d <= 2:
            score += 0.4 * (1 - d / 3)
            why.append(f"{d} hop(s) from {inc.primary_service}")
        j = jaccard(alert.stable_labels, inc.stable_labels)
        if j > 0:
            score += 0.2 * j
            why.append(f"label overlap {j:.2f}")
        if score >= cfg.merge_threshold and score > best.score:
            best = Decision.merge(inc.id, score, why)
    return best
```

`cfg` is a parameter, not an import. That is not style — importing `settings` into `domain/` makes
the module read process-global state, which breaks both INV-01 and the counterfactual mode of the
eval harness (`--set correlation.window_s=600` works by passing a different `cfg`, and cannot work
if the value is read from a module global).

### 6.3 Code contract — the transition, and why the constraint matters more than the dict

```python
async def transition(self, session, incident_id: int, nxt: S, actor: str, reason: str = ""):
    inc = await self.repo.get_for_update(session, incident_id)
    cur = S(inc.state)
    assert_transition(cur, nxt)                       # the dict: catches programmer error

    await session.execute(insert(IncidentTransition).values(
        incident_id=incident_id, seq=inc.state_seq + 1,
        from_state=cur, to_state=nxt, actor=actor, reason=reason))
    #  ^ UNIQUE (incident_id, seq): catches concurrency error at 3 a.m.

    inc.state, inc.state_seq = nxt, inc.state_seq + 1
    if field := TIMING_FIELD.get(nxt):
        setattr(inc, field, utcnow())                 # timestamps only — MTTR is generated

    for action in EFFECTS.get((cur, nxt), TransitionEffect()).outbox:
        await self.outbox.enqueue(session, incident_id, action)   # SAME transaction

    INCIDENT_TRANSITIONS.labels(from_state=cur, to_state=nxt).inc()
    return inc
```

`_stamp_timing` writes **timestamps only**. `tta_seconds`, `ttm_seconds`, and `mttr_seconds` are
`GENERATED ALWAYS AS ... STORED` columns, so there is exactly one definition of MTTR in the system
and it lives in the schema. Every "our MTTR numbers disagree" argument comes from two services
computing it differently.

### 6.4 🚦 G2 — acceptance gate

```python
def test_storm_compresses_to_one_incident(db):
    for a in load_fixture("storm_40.json"):
        ingest_sync(a)
    assert db.count("incidents", "parent_incident_id IS NULL") == 1
    assert db.scalar("SELECT correlated_alert_count FROM incidents LIMIT 1") == 40
    assert db.count("alerts", "is_root_signal") == 1

@given(st.lists(st.sampled_from(list(S)), min_size=1, max_size=8))
def test_no_path_reaches_invalid_state(path):
    cur = S.DETECTED
    for nxt in path:
        try:
            assert_transition(cur, nxt); cur = nxt
        except InvalidTransition:
            pass
    assert cur in set(S)
```

Plus: `alembic upgrade head` then `downgrade base` on an empty PG 18.6 container, and the
naked-`TIMESTAMP` query returning zero rows.

---

## §7 Week 3 — Slack orchestration and the outbox relay

**Objective.** Kill the worker mid-orchestration and get **no duplicate channel**. This is the week
that produces the best single demo of idempotency in the project.

### 7.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W3-01 | Migration 0003 — `outbox_events`, `slack_messages`, `slack_message_revisions`, `correlation_feedback` | `F:.../versions/0003_*.py` | B-08 | `T:test_outbox_unique_key` |
| W3-02 | `outbox.py` — `idem_key()`, `enqueue()` with `ON CONFLICT DO NOTHING`, `CLAIM` SQL | `F:.../orchestration/outbox.py` | B-08, INV-03 | `T:test_enqueue_is_idempotent` |
| W3-03 | `relay_once()` — claim batch, dispatch, backoff+jitter, `dead` at 8 attempts | `F:.../orchestration/outbox.py` | B-08 | `T:test_poison_row_reaches_dead` |
| W3-04 | Relay entrypoint (`python -m incidentpilot.orchestration.outbox --relay`) | same | §4.2 | compose service starts |
| W3-05 | `adapters/chat/base.py` — the `ChatAdapter` Protocol | `F:.../adapters/chat/base.py` | INV-03 | mypy |
| W3-06 | `adapters/chat/slack.py` — create, invite, pin, postMessage, update, archive, getPermalink | `F:.../adapters/chat/slack.py` | — | integration (recorded) |
| W3-07 | **`adapters/chat/fake.py`** — records calls, idempotent by key, `fetch_history` **raises** | `F:.../adapters/chat/fake.py` | **INV-02** | used by every later test |
| W3-08 | `adapters/chat/ratelimit.py` — leaky bucket per `(method, channel)`, priority lanes 0–3 | `F:.../adapters/chat/ratelimit.py` | B-12 | `T:test_priority_2_dropped_under_pressure` |
| W3-09 | `adapters/chat/block_kit.py` — builder for pinned header, merge notice, PIR, review actions | `F:.../adapters/chat/block_kit.py` | §9 pattern | `T:test_blocks_under_50` |
| W3-10 | `handlers.py` — `create_channel`, `invite_responders`, `pin_runbook`, `start_timer`, `post_merge_notice`, `archive_channel` | `F:.../orchestration/handlers.py` | INV-03 | crash matrix |
| W3-11 | `resilience/breaker.py` — pybreaker **per dependency**, 5 fails/60 s → open 30 s | `F:.../resilience/breaker.py` | §11.1 | `T:test_breaker_is_per_dependency` |
| W3-12 | `resilience/retry.py` — tenacity, full jitter, **only on idempotent calls** | `F:.../resilience/retry.py` | §11.1 | `T:test_create_channel_not_retried_without_key` |
| W3-13 | Channel naming: slugify, 80-char cap, numeric suffix on collision | `F:.../adapters/chat/slack.py` | — | `T:test_channel_name_collision` |
| W3-14 | **The 12 crash points** + `crash_after()` fixture | `F:bot/tests/integration/test_crash_matrix.py` | **B-08** | G3 |
| W3-15 | `docs/SCALING_DECISION.md` — append the **partitioning** decision (serialize per incident) | `F:docs/SCALING_DECISION.md` | §13.5 | review |
| W3-16 | `import-graph` test: nothing outside `orchestration/handlers.py` imports an adapter's write methods | `F:bot/tests/unit/test_no_external_writes_outside_relay.py` | **INV-03** | itself |

### 7.2 The 12 crash points (W3-14)

The crash matrix is the concrete form of B-08. Each point injects a `SystemExit` after the named
operation; the test then re-runs orchestration (simulating restart) and asserts exactly one channel
and one incident.

| # | Crash after… | What must survive |
|---|---|---|
| 1 | `XADD`, before `XREADGROUP` | Entry un-ACKed; reclaimed |
| 2 | `XREADGROUP`, before advisory lock | Un-ACKed; another worker takes it |
| 3 | Advisory lock acquired, before incident INSERT | Lock released with txn; retry clean |
| 4 | Incident INSERT, before transition INSERT | Rollback — same transaction |
| 5 | Transition INSERT, before outbox enqueue | Rollback — same transaction |
| 6 | COMMIT, before `XACK` | Redelivery hits `UNIQUE(dedup_key, epoch)` → attach path |
| 7 | Relay CLAIM, before Slack call | Row `claimed`; `next_attempt_at` reclaim after timeout |
| 8 | **Slack `conversations.create` returned, before `mark_dispatched`** | Idempotency key → retry re-uses channel. *The critical one.* |
| 9 | `mark_dispatched`, before `invite_responders` claim | Invite is a separate row; proceeds |
| 10 | Mid-invite (2 of 4 responders) | Invite is idempotent in Slack; re-invite is a no-op |
| 11 | After `pin_runbook`, before `start_timer` | Timer is priority 2 — may be dropped entirely |
| 12 | During compensation (`archive_channel`) | Compensation is itself idempotent |

Point 8 is the one to describe in an interview, because it is the only one where the external system
has already been mutated and the local record has not.

### 7.3 Rate limiting — the priority lanes, concretely

```python
PRIORITY = {"conversations.create": 0, "conversations.invite": 0,
            "chat.postMessage:runbook": 0, "chat.postMessage:pir": 1,
            "chat.update:timer": 2, "reactions.add": 3}
```

Priority ≥ 2 is **dropped under pressure, not queued** — queueing decorative updates behind a storm
only delays the critical ones. Every drop increments `SLACK_SHED` so the behaviour is visible rather
than mysterious. The line worth memorizing: *a stale timer is invisible, a delayed war room is an
outage.*

Note the tier direction: **Slack Tier 1 is the most restrictive**, Tier 4 the most permissive —
Rev 1 had this backwards (B-12). `conversations.create` is Tier 2 (~20/min);
`chat.postMessage` is roughly 1/sec **per channel**, which is why buckets are keyed on
`(method, channel)` and not on method alone.

### 7.4 🚦 G3 — acceptance gate

```python
@pytest.mark.parametrize("crash_at", CRASH_POINTS)   # all 12
def test_no_orphan_channel_on_crash(crash_at, fake_chat, db):
    with crash_after(crash_at):
        run_orchestration(alert_fixture())
    run_orchestration(alert_fixture())               # retry after "restart"
    assert fake_chat.call_count("conversations.create") == 1
    assert db.count("incidents") == 1
```

Plus the live version: `docker kill incidentpilot-worker-1` during orchestration, restart, and
observe one channel. Record this as a GIF — it is screenshot #2 in §21.

---

## §8 Week 4 — Event-sourced transcript

**Objective.** Two hundred messages posted, two hundred rows stored, **zero calls to
`conversations.history`**. This is the B-01 fix and the architectural claim the project is built on.

**The constraint, restated so it is not forgotten mid-build:** since **3 March 2026**,
`conversations.history` and `conversations.replies` are limited to **1 request/minute, 15 messages
per request** for any app not approved for the Slack Marketplace. A 150-message thread would take
ten minutes to read, and the failure is **silent** — no error, the app simply knows less than it used
to. Note also that `conversations.replies` on public channels requires a **user** token; bot tokens
only work in DMs. That is a second, independent reason not to depend on it.

### 8.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W4-01 | Slack Bolt app; Socket Mode in dev, HTTP receiver in prod | `F:.../api/webhooks/slack.py` | §16 | manual |
| W4-02 | `verify_slack` wired to the HTTP receiver over the **raw** body | `F:.../api/webhooks/slack.py` | B-02 | `T:test_slack_sig_over_raw_body` |
| W4-03 | `transcript/ingestor.py` — `message` event → `slack_messages` `ON CONFLICT DO NOTHING` | `F:.../transcript/ingestor.py` | B-01, INV-02 | `T:test_duplicate_event_stored_once` |
| W4-04 | Same transaction: derived `timeline_events` row with `source_message_ts` | `F:.../transcript/ingestor.py` | D1 anchor | `T:test_timeline_written_in_same_txn` |
| W4-05 | `SNAPSHOT_TRIGGERS` → outbox `capture_metric_snapshot` with a ts-derived idem key | `F:.../transcript/ingestor.py` | D1 | `T:test_snapshot_enqueued_once` |
| W4-06 | `transcript/mutations.py` — `message_changed` / `message_deleted` → `slack_message_revisions`, **append-only** | `F:.../transcript/mutations.py` | B-01 | `T:test_edit_preserves_original` |
| W4-07 | `domain/intent.py` — ladder layer 1: the 5 regex rules + `NOISE` default | `F:.../domain/intent.py` | INV-01 | `T:test_intent_rules` (25 cases) |
| W4-08 | `cache.incident_for_channel()` — Valkey `ip:chan:{id}`, 6 h TTL, **DB fallback on miss** | `F:.../db/repositories.py` | INV-04 | `T:test_channel_lookup_survives_flush` |
| W4-09 | `transcript/classifier.py` layer 2 — embedding similarity vs `exemplars.yaml` (C-10) | `F:.../transcript/classifier.py` | C-10 | `T:test_paraphrase_classified` |
| W4-10 | `orchestration/reconciler.py` — **the only** `conversations.history` caller, globally 1/min | `F:.../orchestration/reconciler.py` | **INV-02** | `T:test_reconciler_respects_global_budget` |
| W4-11 | `TRANSCRIPT_RATIO` gauge emitted by the reconciler | `F:.../telemetry/metrics.py` | §1.4 SLI | `T:test_completeness_gauge_emitted` |
| W4-12 | Orphan-channel reconciliation: list `#inc-*`, archive any with no incident row | `F:.../orchestration/reconciler.py` | B-08 | `T:test_orphan_channel_archived` |
| W4-13 | **`test_history_never_called_on_hot_path`** wired into CI as its own step | `F:bot/tests/integration/test_no_history_calls.py` | **INV-02** | G4 |

### 8.2 Code contract — the ingestor

```python
@app.event("message")
async def on_message(event, logger):
    """Every message is persisted AS IT ARRIVES. We never read history back. (B-01)"""
    if event.get("bot_id") or event.get("subtype") in {"channel_join", "channel_leave"}:
        return
    incident_id = await cache.incident_for_channel(event["channel"])
    if incident_id is None:
        return                                          # not an incident channel

    async with uow() as session:
        stmt = insert(SlackMessage).values(
            incident_id=incident_id, channel_id=event["channel"], ts=event["ts"],
            thread_ts=event.get("thread_ts"), user_id=event.get("user"),
            text=event["text"], raw=event,
        ).on_conflict_do_nothing(index_elements=["channel_id", "ts"])
        if (await session.execute(stmt)).rowcount == 0:
            DUPLICATE_MESSAGES.inc(); return            # redelivery, already stored

        intent = detect_intent(event["text"])           # pure, deterministic, free
        if intent.kind is not IntentKind.NOISE:
            await session.execute(insert(TimelineEvent).values(
                time=slack_ts_to_dt(event["ts"]), incident_id=incident_id,
                intent=intent.kind, confidence=intent.confidence,
                source_message_ts=event["ts"],          # ← the citation anchor
                description=intent.summary, author_user_id=event.get("user")))
            if intent.kind in SNAPSHOT_TRIGGERS:
                await session.execute(insert(OutboxEvent).values(
                    incident_id=incident_id, action="capture_metric_snapshot",
                    payload={"reason": str(intent.kind), "at": event["ts"]},
                    idempotency_key=idem_key(incident_id, "snapshot", event["ts"])))
```

`source_message_ts` is what makes every downstream PIR claim citable. Treat it as a required column,
not a nicety — losing it loses the grounding invariant, and the loss would not be visible until week
6.

**Edits are appended, never applied.** A PIR cites `msg:{ts}`; if that message could be silently
rewritten, the citation would stop being evidence. `slack_message_revisions` holds
`(message_id, revision, kind, text, observed_at)` with `UNIQUE (message_id, revision)`.

### 8.3 The known weakness, measured rather than hidden

Layer 1 regex handles code-switched English/Hindi poorly, which matters on Indian engineering teams
where a channel is routinely half and half. **Do not hide this.** Four code-switched fixtures go in
the eval corpus (week 7) and are scored even while they fail. Rev 2 §20.2 Q27 makes this the answer
to "what's the weakest part right now?" — a known, measured gap is a much better answer than an
unknown one.

### 8.4 🚦 G4 — acceptance gate

```python
def test_history_never_called_on_hot_path(fake_chat, incident_fixture):
    generate_pir_sync(incident_fixture.id)
    assert fake_chat.call_count("conversations.history") == 0
    assert fake_chat.call_count("conversations.replies") == 0

def test_two_hundred_messages_two_hundred_rows(fake_chat, db, incident_fixture):
    for i in range(200):
        deliver_message_event(incident_fixture.channel_id, ts=f"175700000{i:04d}.000100")
    assert db.count("slack_messages", f"incident_id = {incident_fixture.id}") == 200
    assert db.scalar("SELECT count(DISTINCT ts) FROM slack_messages") == 200
```

Plus: replay each event twice → still 200 rows, `DUPLICATE_MESSAGES` at 200.
`ip_transcript_completeness` reads 1.0.

---

## §9 Week 5 — Responders, fatigue routing, runbooks

**Objective.** The right human, chosen with awareness of how much they have already been paged, and
a runbook matched to the **root signal** rather than the loudest alert. Plus the seven slash commands.

### 9.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W5-01 | `adapters/paging/base.py` — Protocol: `oncall_for(schedule)`, `page(user, incident)` | `F:.../adapters/paging/base.py` | §3.1 | mypy |
| W5-02 | `adapters/paging/pagerduty.py` via httpx (not the vendor SDK — keeps the adapter honest) | `F:.../adapters/paging/pagerduty.py` | — | recorded fixtures |
| W5-03 | `adapters/paging/static_schedule.py` — YAML rota; the second implementation that proves the adapter | `F:.../adapters/paging/static_schedule.py` | §3.1 | `T:test_static_schedule` |
| W5-04 | `adapters/paging/fake.py` for replay | `F:.../adapters/paging/fake.py` | INV-10 | used by W7 |
| W5-05 | On-call cache `ip:oncall:{schedule}` 60 s TTL; **fallback ladder** cache → static YAML → team channel broadcast | `F:.../db/repositories.py` | FMEA #14 | `T:test_oncall_fallback_ladder` |
| W5-06 | `domain/fatigue.py` — `fatigue_score` (**pure, C-04**) + `routing_decision` | `F:.../domain/fatigue.py` | D5, C-04, INV-01 | `T:test_fatigue_bands` |
| W5-07 | `page_events` writes on every page; `Q7` window query builds `ResponderWindow` | `F:.../db/queries.py` | D5, C-06 | `T:test_fatigue_window_query` |
| W5-08 | Routing announcement in-channel — **never silently reroute** | `F:.../orchestration/handlers.py` | D5 | `T:test_reroute_is_announced` |
| W5-09 | Eight runbooks with `<!-- step:id -->` markers (Appendix C) | `F:runbooks/*.md` | D4 | `T:test_all_runbooks_parse` |
| W5-10 | `runbooks/parser.py` — extract `step_ids[]` from body at load | `F:.../runbooks/parser.py` | D4 | `T:test_step_ids_extracted` |
| W5-11 | `runbooks/matcher.py` — `alert_pattern` regex + severity/service filters, matched on the **root signal** | `F:.../runbooks/matcher.py` | D3/D4 | `T:test_runbook_matches_root_not_loudest` |
| W5-12 | `runbooks/renderer.py` — markdown + alert-context substitution → Block Kit | `F:.../runbooks/renderer.py` | — | `T:test_render_substitutes` |
| W5-13 | `runbooks/detector.py` — command match / reaction / `/step done` → `runbook_step_signals` | `F:.../runbooks/detector.py` | D4 | `T:test_step_signal_deduped` |
| W5-14 | **`Q5` corrected dead-step query (X-01)** committed with a test | `F:.../db/queries.py` | **X-01** | `T:test_dead_step_query_runs` |
| W5-15 | Seven slash commands, each `ack()` **within 3 s** then work async via `response_url` | `F:.../api/slash/*.py` | §7.3 | `T:test_slash_acks_fast` |
| W5-16 | `/split` writes `correlation_feedback` with original score + reasons | `F:.../api/slash/split.py` | D3 | `T:test_split_records_feedback` |
| W5-17 | `orchestration/scheduler.py` (arq) — SLA nudges 5 m, abandonment sweep, reconciler 15 m | `F:.../orchestration/scheduler.py` | B-13 | `T:test_abandonment_after_24h` |

### 9.2 Code contract — fatigue routing

```python
# domain/fatigue.py — pure (C-04): no clock. The window comes from Q7.
def fatigue_score(r: ResponderWindow, max_pages: int = 2) -> float:
    s  = 0.30 * min(r.pages_8h / max_pages, 1.0)
    s += 0.25 * min(r.night_pages_24h / 2, 1.0)        # night = local time, computed in Q7
    s += 0.20 * min(r.incident_minutes_24h / 240, 1.0) # time IN incidents, not just paged
    s += 0.15 * (1.0 if r.consecutive_oncall_days >= 5 else 0.0)
    s += 0.10 * min(r.sev1_count_7d / 3, 1.0)
    return min(s, 1.0)

def routing_decision(score: float) -> str:
    if score < 0.5:  return "page_primary"
    if score < 0.75: return "page_primary_and_invite_secondary"
    return "page_secondary_notify_primary"   # never silently remove the primary
```

**The design constraint that is the actual differentiator:** the bot never silently reroutes. At
score > 0.75 it pages the secondary *and* notifies the primary with an opt-in button ("I'm good, add
me"). Automation that quietly decides a human is too tired would be resented — and saying that you
thought about it is worth more in an interview than the scoring function itself.

`night_pages_24h` uses `responders.timezone` (the column exists for exactly this), evaluated in SQL:
`extract(hour FROM p.time AT TIME ZONE p.tz) NOT BETWEEN 8 AND 22`. Keeping the timezone arithmetic
in SQL is what lets `fatigue_score` stay pure.

### 9.3 Runbook step markers (the D4 substrate)

```markdown
<!-- step:verify-replica-lag -->
### 1. Verify replica lag
```bash
psql -h {{primary_host}} -c "SELECT now() - pg_last_xact_replay_timestamp();"
```
Expect < 5s. If greater, continue to step 2.
```

`parser.py` extracts `step_ids` at load into `runbooks.step_ids[]`. `detector.py` writes a
`runbook_step_signals` row on any of three signals — a message matching the step's command pattern,
a reaction on the pinned runbook, or `/step done verify-replica-lag` — with
`UNIQUE (incident_id, step_id)` making the three signals collapse to one.

### 9.4 🚦 G5 — acceptance gate

Make the paging provider unreachable (NetworkPolicy deny or a `respx` 503) and confirm the ladder:
cache hit → static YAML → team-channel broadcast, **with a visible degradation notice at each step**.
It must never silently fail.

```python
def test_oncall_fallback_ladder(respx_mock, fake_chat, valkey):
    respx_mock.get(url__regex=r".*/oncalls").respond(503)
    valkey.flushall()                                  # no cache either
    responder = resolve_oncall_sync("payments")
    assert responder.source == "static_schedule"
    assert "degraded" in fake_chat.last_message_text.lower()
```

---

## §10 Week 6 — Deterministic impact and the grounded PIR

**Objective.** The flagship. Every claim carries a citation; the schema cannot represent one that
does not; a deterministic validator rejects fabricated IDs; and blocking the provider at the network
level still produces a posted document within 90 seconds.

**The order within the week matters and is not negotiable:** impact computation (W6-01…04) comes
*before* any model call, because the computed numbers are inputs to the prompt. Building the
generator first invites the B-10 bug — a model asked to estimate impact will produce a plausible
number that ends up in a document titled "Post-Incident Review."

### 10.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W6-01 | `adapters/metrics/{base,prometheus,fake}.py` — `query_range`, `query` | `F:.../adapters/metrics/*.py` | §3.1 | recorded fixtures |
| W6-02 | `impact/promql.py` — the four `IMPACT_QUERIES`, computed over the incident window | `F:.../impact/promql.py` | **B-10, INV-06** | `T:test_impact_is_deterministic` |
| W6-03 | `impact/sampler.py` — `query_range` at scrape-interval `step` (**X-04**), writes `signal_samples` | `F:.../impact/sampler.py` | X-04 | `T:test_sampler_row_count` |
| W6-04 | `impact/error_budget.py` — burn from `services.slo_target` and `slo_window_days` | `F:.../impact/error_budget.py` | — | `T:test_burn_math` |
| W6-05 | Prometheus unreachable → `impact = {"status": "unavailable"}`, **never an estimate** | `F:.../impact/promql.py` | FMEA #15 | `T:test_metrics_unavailable_is_honest` |
| W6-06 | Migration 0006 — `pir_documents`, `action_items`, `runbook_step_signals` | `F:.../versions/0006_*.py` | — | `T:test_pir_unique_revision` |
| W6-07 | `domain/evidence.py` — the six citation kinds and the reference-ID grammar | `F:.../domain/evidence.py` | D1, INV-01 | `T:test_ref_grammar` |
| W6-08 | `pir/context.py` — `GroundedContext`, `valid_reference_set()`, `text_for()`, `participants` | `F:.../pir/context.py` | D1 | `T:test_valid_ref_set_complete` |
| W6-09 | `pir/schema.py` — `Citation`, `Claim(min_length=1)`, `ActionItem`, `PIRDraft(extra="forbid")` | `F:.../pir/schema.py` | **INV-05** | `T:test_uncited_claim_fails_parse` |
| W6-10 | `adapters/llm/base.py` + `router.py` — roles, chain, budget, redaction, telemetry | `F:.../adapters/llm/*.py` | **INV-07** | `T:test_router_falls_through_chain` |
| W6-11 | `config/models.yaml` + `models_config.py` — the **only** place a model name exists | `F:.../config/models.yaml` | **INV-07** | CI grep |
| W6-12 | Two provider implementations + `local_provider` + `fake` | `F:.../adapters/llm/*.py` | §3.2 | `T:test_two_providers_same_interface` |
| W6-13 | `privacy/redactor.py` + `recognizers.py` — stable per-incident tokens, restore on render | `F:.../privacy/*.py` | D6 | `T:test_redaction_round_trip_lossless` |
| W6-14 | Intent ladder layer 3 — the `extract` role, batched, only on `NOISE` (C-10) | `F:.../transcript/classifier.py` | C-10 | `T:test_l3_only_on_noise` |
| W6-15 | `pir/prompts/` — registry, `registry.yaml` with sha256, `pir_v2_1_0.md` | `F:.../pir/prompts/*` | §10.1 | `T:test_prompt_hash_matches_file` |
| W6-16 | `pir/validator.py` — `CitationValidator`, `_supports()`, number check, owner check | `F:.../pir/validator.py` | **INV-05, INV-06** | `T:test_fabricated_citation_is_rejected` |
| W6-17 | `pir/skeleton.py` — deterministic markdown from timeline + impact + participants + runbook | `F:.../pir/skeleton.py` | D1 layer 3 | `T:test_skeleton_needs_no_model` |
| W6-18 | `pir/generator.py` — the three-layer chain, always terminating locally | `F:.../pir/generator.py` | D1 | G6 |
| W6-19 | `pir/renderer.py` — Block Kit citation chips via `chat.getPermalink` + markdown | `F:.../pir/renderer.py` | D1 | `T:test_chip_resolves` |
| W6-20 | `resilience/budget.py` — three-tier breaker; **degrades, never blocks** | `F:.../resilience/budget.py` | §10.4 | `T:test_budget_trip_yields_skeleton` |
| W6-21 | `eval/recorder.py` — capture every external interaction, redact at capture | `F:bot/eval/recorder.py` | D2 | `T:test_recording_has_no_secrets` |
| W6-22 | `/webhooks/deploy` — CI deploy events become `deploy:{sha}` evidence | `F:.../api/webhooks/deploy.py` | D1 | `T:test_deploy_is_citable` |
| W6-23 | CI grep job: no vendor/model string outside `config/` | `F:.github/workflows/ci.yml` | **INV-07** | itself |

### 10.2 The evidence graph — the ID grammar

Every citable artifact gets a stable, prefixed ID. The prefix tells the validator which store to
check and the renderer which chip to draw.

| Kind | ID form | Source table/column |
|---|---|---|
| `message` | `msg:{slack_ts}` | `slack_messages.ts` |
| `timeline` | `tl:{id}` | `timeline_events` |
| `alert` | `alert:{fingerprint}` | `alerts.fingerprint` |
| `deploy` | `deploy:{sha}` | deploy webhook events |
| `metric` | `metric:{query}@{t0}-{t1}` | computed impact windows |
| `runbook` | `rb:{runbook_id}#{step_id}` | `runbook_step_signals` |

```python
def valid_reference_set(self) -> set[str]:
    """The single source of truth for what may be cited. If an ID is not in here,
    the model invented it."""
    return {f"msg:{m.ts}" for m in self.messages} \
         | {f"tl:{t.id}" for t in self.timeline} \
         | {f"alert:{a.fingerprint}" for a in self.alerts} \
         | {f"deploy:{d.sha}" for d in self.deploys} \
         | {w.ref for w in self.metric_windows} \
         | {f"rb:{s.runbook_id}#{s.step_id}" for s in self.runbook_steps}
```

### 10.3 Why `root_cause_hypothesis` is nullable

```python
root_cause_hypothesis: Claim | None = None      # "unknown" is a valid, honest answer
```

This matters more than it looks. A schema that *requires* a root cause guarantees the model invents
one for the incidents where nobody actually knows — which are precisely the incidents where a
confident wrong answer does the most damage. Three corpus fixtures (week 7) have a genuinely unknown
root cause, and the model is scored on whether it **abstains**.

Likewise, `impact` is deliberately **absent** from `PIRDraft`. It is computed and injected, never
generated (B-10). If it were a schema field, the model would fill it.

### 10.4 The validator — deterministic, free, and fast

```python
class CitationValidator:
    def __init__(self, support_threshold: float = 0.35):
        self.threshold = support_threshold

    def validate(self, draft: PIRDraft, ctx: GroundedContext) -> ValidationReport:
        errors: list[str] = []
        valid = ctx.valid_reference_set()
        for path, claim in walk_claims(draft):
            if not claim.citations:
                errors.append(f"{path}: uncited claim")           # schema should prevent
            for c in claim.citations:
                if c.ref not in valid:
                    errors.append(f"{path}: fabricated citation {c.kind}:{c.ref}")
                elif not self._supports(c, claim.text, ctx):
                    errors.append(f"{path}: citation does not support claim")
        for ai in draft.action_items:
            if ai.owner and ai.owner not in ctx.participants:
                errors.append(f"action item owner not a participant: {ai.owner}")
        for num in extract_numbers(draft):
            if num not in ctx.computed_impact_numbers:
                errors.append(f"ungrounded numeric claim: {num}")
        return ValidationReport(ok=not errors, errors=errors,
                                coverage=self._coverage(draft, valid, ctx))
```

`_supports()` is **cheap on purpose**: normalized token overlap plus shared entities (service names,
versions, user IDs) above a tuned threshold. No model call. It catches the common failure — a real
ID attached to an unrelated sentence — at zero cost.

**Tune `support_threshold` on the corpus and report its false-rejection rate.** A validator that is
too strict pushes everything to the skeleton and quietly kills the feature; that failure mode is
listed in `references/eval-harness.md` §11 and is worth a counterfactual run
(`--set validator.support_threshold=0.8`).

### 10.5 The three-layer fallback

| Layer | Trigger | Output |
|---|---|---|
| 1 — primary | normal | Full narrative, every section cited |
| 2 — secondary | primary provider error/timeout, or 2 validation failures | Same prompt, second provider, reduced context if oversized |
| 3 — skeleton | budget exceeded, all providers failed, or 3 validation failures | Deterministic markdown: timeline, computed impact, participants, runbook used, TODO prompts |

The skeleton is **not a consolation prize** — it is the honest floor of the product, generated from
data already in hand, and engineers finish a skeleton far more often than they start from a blank
page. It posts with a plain banner: *"AI drafting unavailable; this is the structured skeleton,
please complete the narrative."*

Recall **C-05**: `(S.PIR_FAILED, S.PIR_DRAFTED)` must carry `post_pir_skeleton` in `EFFECTS`, or the
skeleton is persisted and never posted.

### 10.6 The model roles (INV-07 in practice)

```yaml
# config/models.yaml — the ONLY place a model name appears
roles:
  extract:
    provider: ${LLM_PROVIDER_EXTRACT:-openai}
    model:    ${LLM_MODEL_EXTRACT}
    max_output_tokens: 512
    temperature: 0.0
    timeout_s: 8
    budget_usd_per_incident: 0.05
  synthesize:
    provider: ${LLM_PROVIDER_SYNTH:-openai}
    model:    ${LLM_MODEL_SYNTH}
    max_output_tokens: 4096
    temperature: 0.2
    timeout_s: 60
    budget_usd_per_incident: 0.40
  judge:
    provider: ${LLM_PROVIDER_JUDGE:-anthropic}
    model:    ${LLM_MODEL_JUDGE}
    offline_only: true                 # never on the production path
fallback_chain: [primary, secondary, deterministic_skeleton]
```

Per Rev 2 §D.2, the committed default for `synthesize` stays **GPT-5.4-mini** for portfolio
consistency, with the open question recorded in Appendix E. The eval harness (week 7) is the
mechanism that settles it with evidence rather than opinion — which is the whole point of building
it. `judge` carries `offline_only: true` and the router refuses to resolve it outside `eval/`.

### 10.7 🚦 G6 — acceptance gate

Two runs, in this order:

```bash
# 1. Provider blocked at the network level
docker network disconnect incidentpilot_default llm-egress-proxy
./scripts/demo.sh resolve --incident $ID
# ASSERT: skeleton posted within 90 s; incident state == pir_drafted; banner visible

# 2. Unblocked
docker network connect incidentpilot_default llm-egress-proxy
./scripts/demo.sh resolve --incident $ID2
# ASSERT: full PIR; citation_coverage == 1.000; zero uncited claims
```

```python
def test_fabricated_citation_is_rejected(fake_llm, ctx):
    fake_llm.next_response(pir_with_citation("msg:9999999999.999999"))
    report = CitationValidator().validate(fake_llm.parse(), ctx)
    assert not report.ok and "fabricated" in report.errors[0]
```

---

## §11 Week 7 — Replay harness and the CI eval gate

**Objective.** `make eval` replays 40 incidents against fake adapters in under 30 seconds with
**zero network calls**, and a one-word prompt change turns CI red. This is the crown jewel and the
one thing nothing else in the OSS incident field has.

**Read `references/eval-harness.md` before writing any code in this week.** It is 337 lines and it
is authoritative for the recording format, the corpus composition, the metric implementations, the
gate, and the CI workflow. This section schedules it; it does not replace it.

### 11.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W7-01 | `eval/` layout per `eval-harness.md` §1 | `F:bot/eval/**` | D2 | — |
| W7-02 | Recording format v1: `{"format": 1}` header, then `{"t","kind",...}` lines | `F:bot/eval/recorder.py` | §11 (corpus rot) | `T:test_recording_roundtrip` |
| W7-03 | `fakes/chat.py` — replays fixtures; **`fetch_history` raises `AssertionError`** | `F:bot/eval/fakes/chat.py` | **INV-02** | itself |
| W7-04 | `fakes/{paging,metrics,llm}.py` | `F:bot/eval/fakes/*.py` | INV-10 | — |
| W7-05 | `VirtualClock` + `Replayer.run()` advancing on recorded `t` | `F:bot/eval/replayer.py` | INV-10 | `T:test_replay_is_deterministic` (×3 runs, byte-identical) |
| W7-06 | **Socket guard** — any socket call during replay fails the run | `F:bot/eval/replayer.py` | **INV-10** | `T:test_socket_guard_trips` |
| W7-07 | The 40-incident corpus, 11 buckets (§11.2) | `F:bot/eval/corpus/*.jsonl` | D2 | manifest check |
| W7-08 | `metrics.py` — `timeline_f1`, `citation_precision`, `action_item_recall`, `storm_compression`, `uncited_claims`, `fabricated_citations` | `F:bot/eval/metrics.py` | §10.2 | `T:test_metric_edge_cases` |
| W7-09 | `gate.py` — HARD / SOFT / BUDGET, baseline read from `manifest.json` | `F:bot/eval/gate.py` | §10.3 | `T:test_gate_blocks_regression` |
| W7-10 | `report.py` — the markdown table for the sticky PR comment | `F:bot/eval/report.py` | §14.1 | snapshot test |
| W7-11 | `run.py` CLI: `--corpus --gate --report --incident --verbose --live --set` | `F:bot/eval/run.py` | §7 of harness ref | `T:test_cli_flags` |
| W7-12 | Counterfactual `--set` overriding config for the replay only | `F:bot/eval/run.py` | D2 | `T:test_counterfactual_changes_outcome` |
| W7-13 | `judge/rubric.md` + `agreement.py` — judge-vs-human on a 10-incident subset | `F:bot/eval/judge/*` | §10.2 | reported, not gated |
| W7-14 | **`eval-gate` CI job with `dorny/paths-filter@v3`** (the B-11 fix) | `F:.github/workflows/ci.yml` | **B-11** | G7 |
| W7-15 | `eval-nightly.yml` — full corpus + `--live` refresh check, non-blocking | `F:.github/workflows/eval-nightly.yml` | — | scheduled |
| W7-16 | Per-bucket breakdown in the report, not just aggregates | `F:bot/eval/report.py` | §5 of harness ref | review |
| W7-17 | `resilience/degradation.py` — the four levels, announced (INV-12) | `F:.../resilience/degradation.py` | **INV-12** | `T:test_degradation_announces` |
| W7-18 | `lifeboat/` — separate image, ~40 lines, no application imports | `F:lifeboat/main.py`, `F:lifeboat/Dockerfile` | **D7** | `T:test_lifeboat_imports_nothing` |
| W7-19 | Brownout write-ahead: `alerts.wal` on `DatabaseUnavailable`, replay on recovery | `F:.../api/webhooks/*.py` | §12.4 | `T:test_brownout_buffers_and_replays` |
| W7-20 | `docs/FMEA.md` — 18 rows, each with a **residual risk** column | `F:docs/FMEA.md` | §11.2 | review |
| W7-21 | `monitoring/prometheus/rules/incidentpilot-slo.yml` — multi-window burn rate | `F:monitoring/.../*.yml` | §15.3 | `promtool check rules` |
| W7-22 | Migration 0007 — `model_deployments` (shadow/canary provenance) | `F:.../versions/0007_*.py` | §10.5 | — |

### 11.2 The corpus — 40 incidents, 11 buckets

| Bucket | n | What it protects |
|---|---|---|
| Real incidents (demo cluster) | 12 | Realistic language and mess |
| Storms (5 / 12 / 40 alerts) | 4 | Storm compression (D3) |
| False positive / flapping | 3 | **No PIR must be produced** |
| Reopened | 2 | State branch |
| Abandoned | 2 | State branch |
| Metrics unavailable | 3 | Impact says unavailable, never estimates |
| Provider timeout / invalid JSON | 3 | Fallback ladder |
| Unknown root cause | 3 | The model must **abstain** |
| Code-switched English/Hindi | 4 | Known weakness, measured |
| Very long (400+ messages) | 2 | Context assembly, truncation |
| Adversarial (fake IDs in messages) | 2 | Validator resistance |
| **Total** | **40** | |

The **adversarial bucket** deserves the attention. Put a message in the transcript containing
something that *looks* like a reference — `msg:1757000000.000100` — that does not exist in the store.
A model that copies it into a citation must be rejected by the validator. This is a cheap, concrete
test of prompt-injection resistance and it is the bucket interviewers find most interesting.

**Ground truth is human-labelled once, carefully**, and lives *inside* the recording file, not in a
separate spreadsheet — they drift apart otherwise. Label the timeline at intent granularity (not
word granularity), and label action items as the *set* a competent reviewer would extract.

### 11.3 The gate

```
HARD (must equal): citation_precision   == 1.0
                   uncited_claims       == 0
                   fabricated_citations == 0
SOFT (tolerance):  timeline_f1          >= baseline - 0.03
                   action_item_recall   >= baseline - 0.03
                   storm_compression    >= 0.95
BUDGET:            cost_per_pir_usd     <= 0.50
                   p95_generation_ms    <= 30000
```

**Why the soft tolerance exists:** model outputs vary slightly run to run even at temperature 0, and
a zero-tolerance soft gate produces flaky CI that people learn to re-run until green — which is
worse than no gate. Three points is wide enough to absorb noise and narrow enough to catch real
regression.

**Two of the gates are deterministic and those are the hard ones.** The soft gates may use the
`judge` role, and the harness reports judge-vs-human agreement on a 10-incident subset:

```
judge_agreement: 0.82 (n=10, Cohen's kappa 0.61)
```

Knowing your judge agrees with humans 82% of the time is what licenses gating **softly** on it while
gating **hard** on the deterministic metrics. Stating that split out loud is a genuinely senior thing
to say in a review.

### 11.4 The CI wiring (the B-11 fix, in full)

```yaml
  changes:
    runs-on: ubuntu-latest
    outputs: {prompts: ${{ steps.f.outputs.prompts }}}
    steps:
      - uses: actions/checkout@v5
      - uses: dorny/paths-filter@v3
        id: f
        with:
          filters: |
            prompts:
              - 'bot/src/incidentpilot/pir/prompts/**'
              - 'bot/src/incidentpilot/config/models.yaml'
              - 'bot/eval/**'

  eval-gate:
    needs: [changes, test]
    if: needs.changes.outputs.prompts == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v5
        with: {version: "0.12.10", enable-cache: true}
      - run: uv sync --frozen --all-extras
      - run: uv run python -m eval.run --corpus bot/eval/corpus --gate --report eval-report.md
      - uses: actions/upload-artifact@v4
        with: {name: eval-report, path: eval-report.md}
      - uses: marocchino/sticky-pull-request-comment@v2
        with: {path: eval-report.md}
```

**Do not** gate on `github.event.head_commit.modified`. It is only populated on push events, is
absent on pull requests, and is a list — so `contains()` tests exact element equality and is false
essentially always. Rev 1 shipped that condition, which meant its eval job never once executed.

### 11.5 The lifeboat (D7's L3)

```python
# lifeboat/main.py — SEPARATE image, no application imports, ~40 lines
PROBE = os.environ["PROBE_URL"]; HOOK = os.environ["FALLBACK_WEBHOOK"]
STATE = pathlib.Path(os.environ.get("LAST_ONCALL_PATH", "/state/oncall.json"))
FAILS = pathlib.Path("/tmp/lifeboat.fails")

def main() -> None:
    n = int(FAILS.read_text()) if FAILS.exists() else 0
    if healthy():
        FAILS.write_text("0"); return
    n += 1; FAILS.write_text(str(n))
    if n != 2:                                   # fire once, on the second failure
        return
    oncall = json.loads(STATE.read_text()).get("responder", "unknown") if STATE.exists() else "unknown"
    ...
```

`T:test_lifeboat_imports_nothing` walks `lifeboat/main.py`'s AST and asserts every import is from
the standard library. **Every feature added to a lifeboat is another way for it to fail** — if it
imports application code, it fails for the same reason the application failed, and it is not a
lifeboat.

### 11.6 🚦 G7 — acceptance gate

1. `make eval` → all metrics printed with per-bucket breakdown, `network calls 0`, wall < 30 s.
2. Change one word in `pir_v2_1_0.md` without updating the baseline, open a PR → **CI red on
   `eval-gate`**, sticky comment shows which metric moved.
3. Screenshot both. They are README items 7 and 8, and Rev 2 §20.2 Q28 makes the red build the
   answer to "show me something you're proud of."

---

## §12 Week 8 — Dashboard, deploy, polish

**Objective.** The end-to-end demo runs three times consecutively without manual intervention, from
a public URL, from a clean clone, with no API key.

### 12.1 Tasks

| ID | Task | Files | Guards | Test |
|---|---|---|---|---|
| W8-01 | `db/queries.py` — the 12 analytics queries as typed functions | `F:.../db/queries.py` | ref §10 | `T:test_all_12_queries_run` |
| W8-02 | `api/query/*` — read endpoints; statement timeout 5 s reads / 30 s analytics | `F:.../api/query/*.py` | §13.6 | contract tests |
| W8-03 | Next.js 16.3 scaffold, Node 24 LTS, TS strict, Tailwind, shadcn **copied into the repo** | `F:dashboard/**` | skill | `next build` |
| W8-04 | Routes per the dashboard skill; Server Components by default | `F:dashboard/app/**` | skill | — |
| W8-05 | PIR viewer with **citation chips**; `citation_coverage` + `human_edit_ratio` in the header | `F:dashboard/components/pir/*` | D1 | `T:chips_resolve` |
| W8-06 | `ops/slo` and `ops/models` pages — the two that make it an operator's tool | `F:dashboard/app/ops/**` | §15.2 | screenshots 9 |
| W8-07 | Zod validation at the API boundary; fail loudly on schema drift | `F:dashboard/lib/zod-schemas.ts` | skill | typecheck |
| W8-08 | Severity/state token map used by badges, timeline rows **and** charts | `F:dashboard/lib/tokens.ts` | skill | visual |
| W8-09 | `DEMO_MODE=true` — fixtures, **no outbound writes possible** | `F:dashboard/**`, `F:.../config/settings.py` | §16 | `T:test_demo_mode_blocks_writes` |
| W8-10 | Two Grafana dashboards provisioned as JSON | `F:monitoring/grafana/**` | §15.2 | screenshots 9 |
| W8-11 | Compose: `IP_CHAT_PROVIDER=fake` default (**C-02**) so a clean clone runs | `F:docker-compose.yml` | **C-02** | G8 |
| W8-12 | Helm chart: 3 deployments, HPA on stream lag, PDB, NetworkPolicy, ServiceMonitor, lifeboat CronJob, **no `secrets.yaml`** | `F:charts/incidentpilot/**` | §13.3 | `helm install` in kind |
| W8-13 | `release.yml` — buildx multi-arch, SBOM + provenance, cosign keyless, `--atomic` deploy, smoke, rollback | `F:.github/workflows/release.yml` | §14.2 | tag a release |
| W8-14 | `scripts/{demo.sh,smoke.sh,seed.py}` | `F:scripts/*` | §21 | G8 |
| W8-15 | k6 load test: 500 alerts in 60 s, p99 < 250 ms, zero drops | `F:bot/tests/load/*` | §1.4 | `T:load_gate` |
| W8-16 | `docs/ARCHITECTURE.md` with the Mermaid diagrams (GitHub-native, no stale PNG) | `F:docs/ARCHITECTURE.md` | §19.1 | render check |
| W8-17 | README in the §22 order; 10 screenshots; storm GIF at the top | `F:README.md` | §19 | review |
| W8-18 | Deploy to k3s on the demo VM; public URL live | `F:deploy/k3s/*` | §17.1 | G8 |

### 12.2 The dashboard rules that are easy to violate

- **Server Components by default.** `'use client'` only at the leaves that need it: charts, filters,
  the live timer, citation popovers. A dashboard that ships the query layer to the browser is slower
  and leaks more than it needs to.
- **Never render a claim without its citation affordance.** The grounding invariant *is* the product;
  if the UI hides provenance the guarantee is invisible and might as well not exist.
- **Numbers come from continuous aggregates, not raw scans.** A page that re-scans `signal_samples`
  on every load is how you discover the difference between a query that works and one that scales.
- **The live timer ticks client-side from a server-supplied start timestamp** — never from client
  clock arithmetic against a rendered string.
- **Poll, do not stream.** A 15-second revalidation is enough for a handful of concurrent incidents.
  Websockets here is complexity without payoff; say so if asked rather than adding it for show.
- **Explicit degraded states.** "Metrics unavailable for this window" is a real backend state and the
  UI must render it rather than showing a zero. When `ip_degradation_level > 0`, show a banner.

### 12.3 🚦 G8 — acceptance gate

```bash
git clone <repo> /tmp/fresh && cd /tmp/fresh
docker compose up -d              # NO .env file, NO API key
./scripts/demo.sh storm --alerts 40 --service payments
./scripts/demo.sh chatter --incident 1
./scripts/demo.sh resolve --incident 1
./scripts/smoke.sh --expect-channel --expect-pir --timeout 120
```

Run three times consecutively, no manual intervention, all green. Then confirm the public URL serves
the dashboard in `DEMO_MODE`.

---

# Part 3 — Cross-cutting specifications

## §13 Data layer

`references/schema-and-queries.md` is authoritative for all DDL. This section covers what that file
does not: the **order**, the **Alembic mechanics**, and the **CI checks**.

### 13.1 The twelve tables plus four hypertables

| # | Table | Kind | Primary correctness mechanism |
|---|---|---|---|
| 1 | `services` | reference | `name UNIQUE`; `graph_depth` cached, not computed per alert |
| 2 | `runbooks` | reference | `step_ids[]` parsed at load; `git_sha` for D4 provenance |
| 3 | `responders` | reference | `timezone` — fatigue scoring needs local hours |
| 4 | `incidents` | core | **`UNIQUE (dedup_key, dedup_epoch)`** + generated timing columns |
| 5 | `alerts` | core | `UNIQUE (fingerprint, starts_at)` + `last_seen_at`/`seen_count` (**X-02**) |
| 6 | `incident_transitions` | core | **`UNIQUE (incident_id, seq)`** — the concurrency guard |
| 7 | `slack_messages` | transcript | **`UNIQUE (channel_id, ts)`** — at-least-once → exactly-once |
| 8 | `slack_message_revisions` | transcript | `UNIQUE (message_id, revision)` — append-only |
| 9 | `outbox_events` | coordination | **`idempotency_key UNIQUE`** + partial claim index |
| 10 | `correlation_feedback` | coordination | the `/split` training signal |
| 11 | `pir_documents` | PIR | `UNIQUE (incident_id, revision)`; `prompt_sha256` provenance |
| 12 | `action_items` | PIR | partial index on `status = 'open'` |
| 13 | `runbook_step_signals` | PIR | `UNIQUE (incident_id, step_id)` — three signals collapse to one |
| H1 | `timeline_events` | hypertable | 7 d chunks; `segmentby = 'intent'` (**INV-09**) |
| H2 | `signal_samples` | hypertable | 1 d chunks; `segmentby = 'series, service'` |
| H3 | `llm_calls` | hypertable | 7 d chunks; `segmentby = 'role, model'` |
| H4 | `page_events` | hypertable | 7 d chunks; time column is **`time`** (**C-06**) |

Five of those unique constraints *are* the correctness mechanism of a whole subsystem, not hygiene:
`(dedup_key, dedup_epoch)` prevents duplicate war rooms, `(channel_id, ts)` makes message storage
exactly-once, `(incident_id, seq)` prevents double state advances, `idempotency_key` prevents
duplicate Slack calls, and `(incident_id, step_id)` collapses the three runbook signals. **None of
these belongs in a cache** (INV-04).

### 13.2 Migration order (C-09 resolution, as a checklist)

```
0001  1. CREATE EXTENSION timescaledb, vector, pg_trgm
      2. (optionally) CREATE EXTENSION timescaledb_toolkit   -- guarded, C-08
      3. CREATE TYPE incident_state, severity_level, outbox_status
      4. services, runbooks, responders            ← reference tables FIRST (C-09)
0002  5. incidents  (self-referencing FK added after creation)
      6. ALTER TABLE incidents ADD generated columns
      7. alerts, incident_transitions
0003  8. slack_messages, slack_message_revisions, outbox_events, correlation_feedback
0004  9. plain hypertable tables, then create_hypertable()
     10. compression settings, then add_compression_policy()
     11. add_retention_policy()
0005 12. continuous aggregates, then add_continuous_aggregate_policy()
0006 13. pir_documents, action_items, runbook_step_signals
0007 14. model_deployments
 all 15. indexes last
```

**Conversion order inside step 9–11 matters:** create the plain table → `create_hypertable` →
`SET (timescaledb.compress, ...)` → `add_compression_policy`. Converting a table that already has
data requires `migrate_data => true` and takes a lock, so do it while the table is empty.

Hypertable creation and policies belong **in migrations**, not a hand-run script. A schema that only
exists because someone ran `psql` once is not reproducible.

### 13.3 Foreign keys and hypertables

`timeline_events`, `signal_samples`, `llm_calls`, and `page_events` reference `incident_id` as a
**plain BIGINT column with no FK**. FK enforcement across many chunks is a chunk-management cost with
little payoff here; integrity is enforced in the application plus a periodic orphan check in the
reconciler. Say this deliberately if asked — it is a considered trade, not an oversight.

### 13.4 CI schema checks

```sql
-- INV-08: no naked TIMESTAMP anywhere
SELECT table_name, column_name FROM information_schema.columns
WHERE table_schema = 'public' AND data_type = 'timestamp without time zone';
-- must return zero rows

-- Every hypertable has a compression policy
SELECT h.hypertable_name FROM timescaledb_information.hypertables h
WHERE NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs j
                  WHERE j.hypertable_name = h.hypertable_name
                    AND j.proc_name = 'policy_compression');
-- must return zero rows
```

The first has caught real bugs. The second stops a hypertable quietly growing forever because
someone forgot a policy in a later migration.

Plus `T:test_compression_ratio_is_sane`: seed a `signal_samples` chunk with realistic data, compress
it, and assert ≥ 8×. If it fails, `segmentby` is wrong — which is the exact B-05 defect, caught
mechanically rather than by review.

### 13.5 `docs/SCALING_DECISION.md` — both decisions, and both corrections

The file carries two decisions. The first is the database choice, and it must record **two**
corrections to the same number, not one:

> **What I got wrong, twice.** My first justification claimed timeline events would reach millions
> of rows a month. Doing the arithmetic properly — 30 incidents × 45 min × ~20 events/min — gives
> ~800 K rows/month, which plain PostgreSQL handles comfortably with a BRIN index. I then justified
> TimescaleDB on the metric-sample table at "72,000 rows per incident," which assumed 1-second
> resolution. Prometheus scrapes at 15 s by default, so that resolution does not exist in the source
> data; the real figure at a 15 s step is ~4,800 rows per incident, ~144 K rows/month.
>
> **What survives.** `signal_samples` is still the largest table by an order of magnitude;
> declarative retention and compression policies replace ~150 lines of cron and partition
> maintenance I would otherwise have to write, test, and monitor; and `time_bucket_gapfill()`
> renders the incident chart correctly across gaps where plain SQL needs a `generate_series` LEFT
> JOIN. Those three are the justification. The row count is not.
>
> **When I'd reverse it.** If metric snapshots left scope, TimescaleDB would go with them and I'd use
> a BRIN index on a plain table. That is the trigger, and it is written down so a future maintainer
> can act on it.

This is the most quotable file in the repository, and volunteering a *second* correction is stronger
than volunteering the first — it demonstrates the audit habit rather than a single anecdote.

The second decision is partitioning (§13.4 of Rev 2): serialize per incident via
`pg_try_advisory_xact_lock(hashtext('inc:' || incident_id))`, parallelize across incidents. The
insight to state plainly: *the natural parallelism boundary is the incident, not the message, because
the ordering constraint is imposed by Slack per channel and there is exactly one channel per
incident. Choosing the partition key correctly is the whole scaling decision; everything else is
configuration.*

### 13.6 Connection handling

- psycopg 3.3.5 async + SQLAlchemy 2.0.52. Chosen over asyncpg for more predictable `TIMESTAMPTZ`
  handling, pipeline support, and behaviour under pgbouncer transaction pooling.
- **Separate pools per concern** (API reads, worker writes, relay) — the bulkhead pattern in the data
  layer, and why a slow analytics query cannot starve incident creation.
- `SET timezone = 'UTC'` on connect.
- Statement timeouts: 5 s API reads, 30 s analytics, none for migrations.
- Least-privilege app role; **separate migration role**; no `SUPERUSER` anywhere.

### 13.7 Cache responsibilities (7, each with an invalidation rule)

| # | Key | Purpose | TTL | Invalidation |
|---|---|---|---|---|
| 1 | `ip:chan:{channel_id}` | channel → incident_id | 6 h | Written on create; **DB fallback on miss** |
| 2 | `ip:oncall:{schedule}` | on-call roster | 60 s | Short TTL; provider is rate-limited |
| 3 | `ip:rl:{method}:{channel}` | token bucket per Slack method **and channel** | sliding | Leaky-bucket refill |
| 4 | `ip:claim:{dedup_key}` | short in-flight claim, **renewed** (B-07) | 60 s + renewal | Released on success/failure; the DB unique key is the real guard |
| 5 | `ip:budget:{day}` / `ip:budget:inc:{id}` | LLM spend counters | 25 h / 24 h | `INCRBYFLOAT`; breaker reads |
| 6 | `ip:fatigue:{user}` | rolling page count | 8 h | Sorted set, `ZREMRANGEBYSCORE` |
| 7 | `ip:ctx:{incident_id}` | assembled PIR context | 2 h | Lets a retry skip re-assembly |

**Nothing is cache-only.** Every read has a DB fallback — that is what makes the cache AP while the
incident state stays CP (§12.1 of Rev 2).

## §14 Configuration and the vendor-agnosticism contract

Three files, three jobs:

| File | Contains | Changed by |
|---|---|---|
| `config/settings.py` | Environment-driven runtime config, `IP_` prefix, secrets as `SecretStr` | Ops |
| `config/models.yaml` | **The only place a model name exists** | An eval-gated PR |
| `config/correlation.yaml` | window, merge threshold, storm threshold | An eval-gated PR |

**`settings` is imported, never `os.environ`.** Settings are injected into constructors, which is
why the tests need no monkeypatching — and no-monkeypatching is itself a signal about the design.

The CI grep that enforces INV-07:

```bash
if grep -rEn "gpt-[0-9]|claude-|gemini-|llama-" bot/src --include="*.py" \
   | grep -v "src/incidentpilot/config/"; then
  echo "::error::hardcoded model name — resolve through LLMRouter roles"; exit 1
fi
```

The interview answer this buys, verbatim from Rev 2 §D.3: *"I never pin a model in code. The system
defines three roles — extract, synthesize, judge — and each resolves through an adapter from config.
Between starting the project and finishing it, the vendor shipped a new family and cut prices 80% on
the small tier. My cost per PIR dropped by changing one YAML value, and the eval harness proved
quality didn't regress before I merged it."*

## §15 Observability

### 15.1 The metrics (Rev 2 §15.1, names are stable API)

```python
WEBHOOK_LATENCY      = Histogram("ip_webhook_seconds", "", ["source","outcome"],
                                 buckets=[.01,.025,.05,.1,.25,.5,1,2.5])
TIME_TO_WAR_ROOM     = Histogram("ip_time_to_war_room_seconds", "", buckets=[1,2,5,10,20,30,60,120])
TIME_TO_ACK          = Histogram("ip_time_to_acknowledge_seconds", "", ["severity"])
STREAM_PENDING       = Gauge("ip_stream_pending", "", ["stream","group"])       # HPA source
INCIDENT_TRANSITIONS = Counter("ip_transitions_total", "", ["from_state","to_state"])
STORM_COMPRESSION    = Histogram("ip_alerts_per_incident", "", buckets=[1,2,5,10,20,50,100])
PIR_GENERATION       = Histogram("ip_pir_seconds", "", ["layer","outcome"])
PIR_CITATION_COV     = Gauge("ip_pir_citation_coverage", "")                    # must be 1.0
PIR_EDIT_RATIO       = Histogram("ip_pir_human_edit_ratio", "", ["prompt_version"])
LLM_COST             = Counter("ip_llm_cost_usd_total", "", ["role","provider","model"])
BUDGET_TRIPS         = Counter("ip_budget_trips_total", "", ["scope"])
TRANSCRIPT_RATIO     = Gauge("ip_transcript_completeness", "")                  # the real SLI
DEGRADATION_LEVEL    = Gauge("ip_degradation_level", "")                        # 0-3
SLACK_429            = Counter("ip_slack_rate_limited_total", "", ["method","priority"])
```

`T:test_metric_names_stable` snapshots these names. Grafana dashboards and alert rules match on
them; a rename is a breaking change and should be treated as one.

### 15.2 The two dashboards

**A — Incident Operations** (for the team): active incidents with elapsed time · MTTA/TTM/MTTR trend
from continuous aggregates · **alert compression ratio** (D3's proof) · on-call load + fatigue
heatmap by hour · action-item aging with the P0 SLO line · repeat-incident rate.

**B — IncidentPilot SLOs** (the bot monitoring itself): ingest availability + error-budget burn-down
· time-to-war-room p50/p95/p99 against 10 s · **transcript completeness** · PIR delivery by layer,
stacked · **citation coverage, a flat line at 1.0** · cost per PIR with breaker trips annotated ·
degradation level over time · stream pending.

Dashboard B goes at the top of the README. *"The incident tool has its own SLO dashboard"* lands with
SRE interviewers immediately, and almost nobody does it.

### 15.3 Alert rules

Multi-window multi-burn-rate, per the Google SRE workbook — 2% of budget in 1 h pages, 5% in 6 h
tickets. **Single-window threshold alerts on a 99.9% target either page constantly or never fire**,
which is the whole reason for the two-window form.

```yaml
- alert: IngestErrorBudgetFastBurn
  expr: ip:ingest_error_ratio:rate5m > (14.4 * 0.001)
    and ip:ingest_error_ratio:rate1h > (14.4 * 0.001)
  for: 2m
  labels: {severity: page}

- alert: CitationCoverageBelowOne
  expr: ip_pir_citation_coverage < 1
  for: 1m
  labels: {severity: page}
  annotations: {summary: "PIR published with an uncited claim — grounding invariant violated"}

- alert: TranscriptIncomplete
  expr: ip_transcript_completeness < 0.9999
  for: 15m
  labels: {severity: ticket}

- alert: StreamBacklogGrowing
  expr: ip_stream_pending > 100
  for: 2m
  labels: {severity: page}

- alert: DegradedMode
  expr: ip_degradation_level >= 2
  for: 1m
  labels: {severity: page}

- alert: IncidentPilotDown              # the meta-alert
  expr: up{job="incidentpilot-api"} == 0
  for: 1m
  labels: {severity: page, route: fallback}     # ← MUST bypass IncidentPilot (INV-11)
```

`IncidentPilotDown` must route straight to the paging provider and `#sre-fallback`. **If the only
path to hearing that your incident tool is down runs through your incident tool, you have built a
circular dependency** — and an interviewer will check.

### 15.4 The Alertmanager 0.34.0 label change

0.34.0 (16 Aug 2026) splits `authError` and `rateLimited` out of `clientError` in the `reason` label
on `alertmanager_notifications_failed_total`. **Any dashboard panel or alert matching
`reason="clientError"` will silently under-count after the upgrade.** Grep the provisioned Grafana
JSON for that string before shipping, and — per Rev 2 §22.1 — if you bump Alertmanager here, bump it
in HealOps and ChaosProof too so the three portfolio projects' dashboards stay consistent.

### 15.5 Logs and traces

Every log line is structlog JSON with `incident_id`, `trace_id`, and `state` bound in context, so a
single `grep` (or Loki query) reconstructs one incident end to end. OTel spans wrap
webhook → stream → worker → relay → external call, with the LLM call as its own span carrying
`prompt_version` and `cost_usd` as attributes.

## §16 Security, privacy, and the data boundary

| Area | Control |
|---|---|
| Webhook trust | Per-source verification (§5.3); constant-time compare; 300 s replay window; **raw-body HMAC before parsing** |
| Slack scopes | Least privilege: `channels:manage`, `chat:write`, `pins:write`, `commands`, `users:read`, `channels:history` (events only). **No** `admin.*`, `files:read`, `search:read` |
| Secrets | External Secrets / sealed-secrets; never in the chart, never in a committed env file; `gitleaks` in CI |
| Container | Non-root UID 10001, read-only rootfs, all caps dropped, seccomp RuntimeDefault, slim base, Trivy gate on HIGH/CRITICAL |
| Supply chain | `uv.lock` committed, SBOM + provenance on build, cosign keyless signing, digest-pinned deploys |
| Network | NetworkPolicy: egress only to Slack/LLM/paging/DNS; ingress only from Alertmanager and the ingress controller |
| Database | Least-privilege role, separate migration role, TLS, no `SUPERUSER` |
| PII | D6 redaction before egress; `redaction_audit` records the **class**, never the value; CI test asserting no raw PII reaches a provider |
| Audit | `incident_transitions` and `outbox_events` are append-only |
| Dashboard authz | Read-only public demo with synthetic data; **never expose a real workspace's incident data publicly** |

**On the public demo specifically:** `DEMO_MODE=true` seeds synthetic incidents and disables all
outbound writes. Your live resume link must not be a way for strangers to create channels in a real
Slack workspace. Say this in interviews — demonstrating that you thought about the security of your
own portfolio is unusual and memorable.

**The PII CI test is the one to name:**

```python
def test_no_raw_pii_reaches_provider(inspecting_fake_provider, incident_with_pii):
    generate_pir_sync(incident_with_pii.id)
    for payload in inspecting_fake_provider.payloads:
        assert not EMAIL_RE.search(payload), "raw email reached the provider"
        assert not CARD_RE.search(payload), "raw card number reached the provider"
```

Custom recognizers for the domain: order IDs, UPI VPAs, Indian phone formats, JWTs, AWS keys.
**Stable tokens per incident** — `<EMAIL_1>` is the same person throughout — so the model can still
reason about "the same customer reported it twice" while never seeing the address.

## §17 Resilience — the four levels, wired concretely

| Level | Trigger (concrete) | Behaviour | Announced by |
|---|---|---|---|
| **L0 Normal** | all breakers closed | Full pipeline | — |
| **L1 Degraded** | LLM breaker open **or** `BudgetExceeded` | Skeleton PIRs; deterministic timeline only | Channel banner + gauge = 1 |
| **L2 Brownout** | `DatabaseUnavailable` on the ingest path | Webhooks still 202; alerts to `alerts.wal`; channel creation from cached state; **replay on recovery** | Ops banner + gauge = 2 + page |
| **L3 Lifeboat** | `/readyz` failing > 60 s, observed **externally** | Separate CronJob posts manual runbook + last-known on-call to `#sre-fallback`, pages directly | The lifeboat itself |

The asymmetry that justifies L2 being the one AP component in an otherwise CP system: **a dropped
alert is unrecoverable; a delayed alert is not.**

```python
async def ingest(alert: NormalizedAlert):
    try:
        await store_alert(alert)                              # normal path
    except DatabaseUnavailable:
        await valkey.xadd("alerts.wal", alert.to_fields())    # brownout buffer
        BROWNOUT_BUFFERED.inc()
        await degradation.set_level(Level.BROWNOUT, "postgres unreachable")
    # 202 either way: the caller's contract is "durably accepted", and the stream
    # IS durable (AOF everysec). On recovery, replay_wal() drains in order.
```

**Breakers are per dependency, never global.** A global breaker means one flaky dependency opens the
circuit for all of them — a common and damaging mistake, and `T:test_breaker_is_per_dependency`
guards against reintroducing it.

**Chaos validation** (post-MVP if week 8 is tight, but the invariants are written now):

| Experiment | Invariant asserted |
|---|---|
| Kill the API pod mid-orchestration | No orphaned channel; incident completes on retry |
| Kill the worker after `XADD`, before insert | Entry reclaimed by `XAUTOCLAIM`; nothing lost |
| Add 2 s latency to Slack | Time-to-war-room SLO degrades; **no duplicate calls** |
| Blackhole the model provider | PIR delivered as skeleton within 90 s |
| Kill Postgres during PIR generation | L2 brownout; alerts buffered; replay on recovery |
| Kill everything | Lifeboat posts to the fallback channel within 120 s |

Two capstones validating each other — ChaosProof proving IncidentPilot's degradation modes — is a
portfolio-level story very few candidates have. Wire it up and screenshot it.

## §18 CI/CD

### 18.1 `ci.yml` — jobs, in dependency order

| Job | Runs when | Does |
|---|---|---|
| `changes` | always | `dorny/paths-filter@v3` → `prompts`, `bot`, `dashboard` outputs |
| `lint-and-type` | `bot == true` | ruff check, ruff format --check, **mypy --strict**, the INV-07 grep, `assert_invariants` |
| `test` | `bot == true` | unit (`--cov-fail-under=85`), integration (Testcontainers), **the B-01 regression test as its own step** |
| `schema` | `bot == true` | `alembic upgrade head` + `downgrade base` on a PG 18.6 container; the two INV-08/policy queries |
| `eval-gate` | `prompts == true` | replay 40, gate, upload report, sticky PR comment |
| `dashboard` | `dashboard == true` | `next build`, `tsc --noEmit`, Lighthouse a11y ≥ 95 |
| `security` | always | `pip-audit`, Trivy fs (fail on HIGH/CRITICAL), `gitleaks` |

`concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}`.

### 18.2 `release.yml`

Tag-triggered. buildx multi-arch (`linux/amd64,linux/arm64`), `provenance: true`, `sbom: true`,
cosign keyless signing of the digest, then a `production` environment gate (manual approval,
deliberate), then:

```bash
helm upgrade --install incidentpilot ./charts/incidentpilot \
  --namespace incidentpilot --create-namespace \
  --set image.digest=${{ needs.build.outputs.digest }} \
  --atomic --timeout 5m --wait
./scripts/smoke.sh --expect-channel --expect-pir --timeout 120
# on failure: helm rollback incidentpilot --wait
```

`--atomic` plus an explicit smoke test plus `helm rollback` on failure is a complete deployment
safety story in three lines, and it is exactly what an SRE interviewer wants to hear about CD.

**Why rolling and not blue/green:** blue/green means two full stacks including shared state, and the
shared Slack workspace makes "two live versions" *actively harmful* — both would try to create the
same channel. The sentence to use: *"I chose rolling because blue/green's isolation assumption breaks
when the side effects are in a shared external system."*

### 18.3 Rollout strategy per component

| Component | Strategy | Reasoning |
|---|---|---|
| api | Rolling, `maxUnavailable: 0` | Stateless; ingest must never lose capacity mid-deploy — alerts don't pause for deploys |
| worker / relay | Rolling, 60 s grace | In-flight work drains; un-ACKed entries are safe regardless |
| DB migrations | **Expand → migrate → contract**, forward-only | Never a destructive migration in the same release as the code that stops using the column |
| Prompts / models | **Shadow → canary → default** | Quality changes need traffic-based validation, not a boolean deploy |
| Dashboard | Rolling | Purely presentational |

**No CPU limit on the api container, only requests.** CFS throttling on a latency-sensitive webhook
path causes exactly the p99 spikes the SLO forbids. Memory *is* limited, because memory is
incompressible. Knowing which side of that well-known argument you are on is a signal.

## §19 Testing strategy

| Layer | Scope | Tooling | Gate |
|---|---|---|---|
| Unit | All of `domain/` | pytest 9.1.1 | ≥ 85% overall, **100% `domain/`** |
| Property | Transitions never reach an invalid state; dedup idempotent under permutation; redaction round-trip lossless | hypothesis | must pass |
| Contract | Recorded Alertmanager 0.34 / PagerDuty v3 / Slack Events payloads | JSON fixtures | must pass |
| Integration | Real PG 18.6 + Valkey 9.1 | Testcontainers 4.15 | must pass |
| **Idempotency** | **12 crash points** (§7.2) | fault injection | must pass |
| Rate limit | Simulated 429s; priority lane preserved | fake chat adapter | must pass |
| **Replay/eval** | 40 golden incidents | `eval/` harness | **the CI gate** |
| Chaos | ChaosProof experiments | LitmusChaos | invariants hold |
| Load | 500 alerts in 60 s, p99 < 250 ms, zero drops | k6 | must pass |
| E2E | Full demo script, headless | pytest + fake chat | before every release |

**The five tests to write before the features they guard**, from the starter code Part 8 — they are
cheap now and very expensive to retrofit:

1. `test_history_never_called_on_hot_path` — INV-02
2. `test_no_orphan_channel_on_crash` (×12) — INV-03
3. `test_storm_compresses_to_one_incident` — D3
4. `test_fabricated_citation_is_rejected` — INV-05
5. `test_no_path_reaches_invalid_state` (hypothesis) — B-13

Add two more from this plan's invariants:

6. `test_domain_purity` — INV-01, and the one that protects the other six
7. `test_no_external_writes_outside_relay` — INV-03's structural half

---

# Part 4 — Verification and delivery

## §20 Acceptance gates — the binary test per week

| Gate | Week | The single command or assertion | Fails if |
|---|---|---|---|
| **G1** | 1 | Bad bearer → 401; good → 202 at p99 < 250 ms over 100 reqs; entry visible via `XRANGE` | Any of the three |
| **G2** | 2 | 40-alert storm → **exactly 1** incident, 39 attached, 1 root signal; every invalid transition raises | Any |
| **G3** | 3 | `docker kill` the worker mid-orchestration → restart → **no duplicate channel**; 12/12 crash points | Any |
| **G4** | 4 | 200 messages → **200 rows**, 0 duplicates, **0** `conversations.history` calls | Any |
| **G5** | 5 | Paging provider unreachable → cache → static YAML → team channel, **each announced** | Silent failure at any step |
| **G6** | 6 | Provider blocked → skeleton within 90 s, state `pir_drafted`; unblocked → full PIR, **0 uncited claims** | Either run |
| **G7** | 7 | One-word prompt change → **CI red on `eval-gate`**, PR blocked, sticky comment shows the moved metric | Gate green, or gate never ran |
| **G8** | 8 | Clean clone, no `.env`, `docker compose up`, full demo **three times consecutively** | Any manual intervention |

A red gate stops the week. That is the point of having them, and "I cut scope when week 6's gate
went red" is a better interview answer than a plan that never slipped.

## §21 Demo script and screenshots

The demo is 4 minutes. **The storm at 0:05 and the red CI at 2:50 are the two moments people
remember.** Lead with the storm; close with the gate.

```
0:00  "Every incident tool creates a channel. Watch what happens on an alert storm."
0:05  ./scripts/demo.sh storm --alerts 40 --service payments
0:12  → ONE channel: #inc-2026-09-05-payments-degraded
      → Pinned: "40 correlated alerts · root signal: postgres-primary · 6 services affected"
      → On-call invited; a second responder auto-added (paged twice in the last 8 hours)
      → Runbook pinned, matched to the ROOT signal, not the loudest alert
0:45  Type: "checking the replica lag dashboard"     → [investigation] tagged silently
1:00  Type: "rolling back payments to v4.2.1"        → [remediation_start] + metric snapshot
1:30  /resolve "replica promoted, lag recovered"
1:35  → Deterministic impact FIRST: 4,182 failed requests, 11.4 min, 38% of the
        monthly error budget for payments
1:55  → PIR posted. Every sentence has a citation chip: [msg] [deploy] [metric]
2:10  Click a citation → jumps to the exact Slack message
2:20  "Now the part no other incident bot has."
      make eval  → 40 golden incidents replayed, F1 0.91, citations 1.00, $0.31/PIR
2:50  Edit the prompt, push → CI goes RED on the eval gate. Merge blocked.
3:20  Dashboard: MTTA/MTTR trend, on-call load, action-item aging, cost per PIR
3:50  "The bot's own SLO dashboard. It's production infrastructure, so it has SLOs."
```

**The 10 screenshots** (take them once, use everywhere — README, resume, LinkedIn):

1. Storm compression: 40 alerts → 1 channel, correlation reasons visible
2. War room at t+10 s — responder invited, runbook pinned, timer running
3. Timeline auto-tagging in the channel
4. **PIR with visible citation chips** ← the money shot
5. Citation click-through to the source Slack message
6. Fatigue routing notice ("paging secondary; primary paged twice since 01:00")
7. **`make eval` terminal output** with the gate table
8. **Red CI check** blocking a prompt PR + the sticky comment
9. SLO dashboard (Dashboard B)
10. Runbook-efficacy report (D4's query output — see the honesty note in §2.1)

## §22 README assembly order

Recruiters read the first screen only. In this order:

1. One-line hook + **the storm GIF** — 15 seconds: 40 alerts → 1 war room → PIR with citations
2. **Live demo link** + `Demo credentials: read-only, synthetic data`
3. **The gap**: "Netflix Dispatch archived Sept 2025. Grafana OnCall OSS archived March 2026. This is
   the self-hosted alternative, plus the governance neither had." *(Re-verify the week of your
   interview and say "as of my last check in September 2026" — panels reward dated claims.)*
4. Architecture diagram — **Mermaid**, renders natively on GitHub, stays in sync, no stale PNG
5. What's different — the D1–D9 table
6. Quickstart: `git clone && docker compose up` → working demo **with no API key**
7. The eval gate — screenshot of the red CI check and the PR comment table
8. SLO dashboard screenshot
9. Design decisions → links to `docs/ADR/` and `docs/SCALING_DECISION.md`
10. Roadmap · Licence · *"Not affiliated with Slack or PagerDuty."*

---

# Appendices

## Appendix A — Defect traceability (B-01 … B-14)

Every Rev 2 defect, the file that fixes it, and the test that proves it stays fixed. This table is
the answer to "you said Rev 1 was broken — show me."

| ID | Defect | Fixed in | Guarded by | Week |
|---|---|---|---|---|
| **B-01** | PIR pipeline fetches Slack thread at resolve time (1 req/min, 15 msgs since 3 Mar 2026) | `transcript/ingestor.py`, `orchestration/reconciler.py` | `T:test_history_never_called_on_hot_path`; `FakeChat.fetch_history` raises | W4 |
| **B-02** | Claims signature validation on Alertmanager, which does not sign | `api/security.py`, `api/webhooks/alertmanager.py` | `T:test_alertmanager_bad_bearer_401`, `T:test_paging_accepts_rotated_signature` | W1 |
| **B-03** | One alert storm → 40 Slack channels | `domain/correlation.py` | `T:test_storm_compresses_to_one_incident` | W2 |
| **B-04** | TimescaleDB volume arithmetic wrong | `docs/SCALING_DECISION.md` (+ **X-04**, a second correction) | Review | W2 |
| **B-05** | `compress_segmentby = 'incident_id'` — highest cardinality | migration 0004 | `T:test_compression_ratio_is_sane` (≥ 8×) | W2 |
| **B-06** | `TIMESTAMP` instead of `TIMESTAMPTZ` | all migrations | CI `information_schema` query (INV-08) | W2 |
| **B-07** | Redis `SETNX` dedup loses work on worker crash | `db/repositories.py` + `UNIQUE (dedup_key, dedup_epoch)` | `T:test_dedup_survives_cache_flush` | W2 |
| **B-08** | No outbox → orphaned Slack channels | `orchestration/outbox.py`, `handlers.py` | `T:test_no_orphan_channel_on_crash` × 12 | W3 |
| **B-09** | Gating on self-reported LLM confidence | `pir/validator.py` (citation invariant) | `T:test_fabricated_citation_is_rejected` | W6 |
| **B-10** | `users_affected_estimate` generated by the model | `impact/promql.py`; `impact` absent from `PIRDraft` | `T:test_ungrounded_number_rejected` | W6 |
| **B-11** | CI prompt job gated on `head_commit.modified` — never runs | `.github/workflows/ci.yml` with `dorny/paths-filter@v3` | G7 (must go red on a prompt edit) | W7 |
| **B-12** | Slack rate-limit tiers described backwards; no priority lane | `adapters/chat/ratelimit.py` | `T:test_priority_2_dropped_under_pressure` | W3 |
| **B-13** | State machine has no escape hatches | `domain/states.py` (13 states) | `T:test_no_path_reaches_invalid_state` | W2 |
| **B-14** | Version drift, `latest` tags, no lockfile | `pyproject.toml`, `uv.lock`, §3.2 pins | CI `uv sync --frozen`; no `latest` grep | W1 |

## Appendix B — Differentiator traceability (D1 … D9)

| ID | Differentiator | Core files | Acceptance criterion | Week |
|---|---|---|---|---|
| **D1** | Evidence-anchored PIR | `pir/{schema,context,validator,generator,skeleton,renderer}.py` | Citation coverage 1.000 on every published PIR; fabricated ID → reject → retry → skeleton, no partial publication | W6 |
| **D2** | Incident replay + eval gate | `eval/**` | `make eval` < 30 s, **0 network calls**; a one-word prompt change turns CI red | W7 |
| **D3** | Alert-storm compression | `domain/{correlation,graph,severity}.py`, `/split` | 40 alerts → 1 incident, 1 root signal; every merge posts its score and reasons | W2 |
| **D4** | Closed-loop runbook efficacy | `runbooks/{parser,detector}.py`, `Q4`/`Q5` (**X-01 corrected**) | Step signals captured from three sources, deduped; dead-step query runs on seeded data. *Auto-PR job is post-MVP — say so* | W5 |
| **D5** | Fatigue-aware routing | `domain/fatigue.py`, `Q7` | Score > 0.75 pages the secondary **and** notifies the primary with an opt-in button; never silent | W5 |
| **D6** | PII redaction / data boundary | `privacy/{redactor,recognizers}.py` | Round-trip lossless; `T:test_no_raw_pii_reaches_provider`; local-model mode documented | W6 |
| **D7** | Brownout + lifeboat | `resilience/degradation.py`, `lifeboat/` | All four levels inducible and announced; lifeboat posts within 120 s from an image sharing no code | W7 |
| **D8** | Organizational learning | `Q6` (pgvector), `Q10`, staleness escalation | Repeat detection surfaces "similar to #INC-204, action item still open" in-channel | W8 |
| **D9** | Portfolio integration | `config/service-graph.yaml` (TraceMap fallback), deploy webhook (HealOps), chaos (ChaosProof) | Graph loads from TraceMap **or** the checked-in YAML — the project stands alone | W2/W6 |

**D9's rule, stated once so it is not violated:** always ship the YAML fallback. *A skill that only
works when another project is deployed is not a product.*

## Appendix C — Runbook catalogue (8 runbooks)

> **Provenance note.** Rev 2 §2.1 (week 5) requires "8 runbooks" but never enumerates them. Rev 1
> §3 lists six types. Per the source hierarchy in the header, this is the **one** place Rev 1 is used
> as a gap-fill. The six are carried forward; #7 and #8 are added because the Rev 2 demo storyline
> (replica lag → promotion) and the B-03 storm example (`CertExpiry`) both need runbooks that Rev 1's
> six do not cover.

| # | Alert pattern | Runbook | Step IDs |
|---|---|---|---|
| 1 | `ServiceDown\|InstanceDown` | **Service Down** | `verify-health`, `check-recent-deploys`, `check-dependencies`, `restart-or-rollback`, `escalate` |
| 2 | `HighErrorRate\|ErrorBudgetBurn` | **High Error Rate** | `open-dashboard`, `check-recent-deploys`, `check-breaker-state`, `rollback`, `verify-recovery` |
| 3 | `HighLatency\|LatencyP99High` | **High Latency** | `check-downstream`, `check-conn-pool`, `check-cache-hit-rate`, `identify-bottleneck`, `mitigate` |
| 4 | `PodCrashLoop\|CrashLoopBackOff` | **Pod CrashLoopBackOff** | `get-logs`, `describe-pod`, `check-oom`, `check-config`, `rollback-deployment` |
| 5 | `DiskFull\|NodeDiskPressure` | **Disk Full** | `check-usage`, `find-largest`, `rotate-logs`, `clear-tmp`, `expand-or-evict` |
| 6 | `Postgres.*\|DatabaseConn.*` | **Database Connection Issues** | `check-pool-stats`, `check-active-queries`, `check-replication`, `failover-decision`, `escalate-dba` |
| 7 | `ReplicaLag\|ReplicationDelay` | **Replica Lag / Promotion** | `verify-replica-lag`, `check-wal-backlog`, `check-primary-load`, `promote-replica`, `verify-recovery` |
| 8 | `CertExpiry\|TLSCertExpiring` | **Certificate Expiry** | `identify-cert`, `check-issuer`, `renew-or-reissue`, `roll-pods`, `verify-chain` |

Runbook 7 is the one the demo uses (Rev 2 §2.2: *"rolling back payments"* → *"replica promoted, lag
recovered"*), so build it first and make it the best of the eight — it is the one that appears in
screenshots 2 and 4.

Each runbook is markdown with `<!-- step:id -->` markers, `{{placeholder}}` substitution, a
`git_sha` recorded on load for D4 provenance, and a `severity_filter` / `service_filter` where the
match should be narrower than the alert pattern alone.

## Appendix D — Per-skill Definition of Done, consolidated

The eight skills' own acceptance criteria, mapped to the week that earns each one. Every box must be
ticked before the project is presentable.

**`incidentpilot-ingest-correlation`** (W1–W2)
- [ ] Bad bearer → 401; good → 202 at p99 < 250 ms over 100 requests
- [ ] The 40-alert storm fixture → **exactly one** incident, 39 alerts attached, one `is_root_signal`
- [ ] Killing the worker between `XADD` and incident creation loses nothing (un-ACKed, reclaimed)
- [ ] Every merge decision in the log carries its score and ≥ 1 human-readable reason

**`incidentpilot-state-orchestration`** (W2–W3)
- [ ] Every invalid transition raises `InvalidTransition`, covered by a unit test
- [ ] All 12 crash points → exactly one Slack channel and one incident row
- [ ] Two workers racing the same incident → one advance, one retry, never two
- [ ] A poisoned outbox row reaches `dead` after 8 attempts and appears on the dashboard

**`incidentpilot-slack-platform`** (W3–W4)
- [ ] `test_history_never_called_on_hot_path` passes: zero history/replies calls during PIR generation
- [ ] 200 messages → exactly 200 rows, zero duplicates, completeness ratio 1.0
- [ ] Simulated 429 storms leave channel creation and invites successful; timer updates dropped **and counted**
- [ ] The full demo runs against the fake adapter with **no Slack credentials present**

**`incidentpilot-pir-grounding`** (W6)
- [ ] Citation coverage 1.000 on every published PIR; anything less is an alerting condition
- [ ] A fabricated reference ID → rejection → retry → skeleton, with **no partial publication**
- [ ] Blocking the provider at the network level still yields a posted PIR within 90 s
- [ ] No vendor or model string anywhere in `src/` outside `config/`

**`incidentpilot-mlops-eval`** (W7)
- [ ] `make eval` completes in < 30 s with **zero network calls**, printing per-bucket breakdowns
- [ ] Editing one word of a prompt without updating the baseline fails CI with a readable reason
- [ ] The PR comment table renders and is committed as a README screenshot
- [ ] Judge-vs-human agreement measured and reported on a labelled subset

**`incidentpilot-timescale-data`** (W2, W6)
- [ ] `alembic upgrade head` on an empty PG 18.6 produces every table, hypertable, policy and index; `downgrade base` works
- [ ] No `TIMESTAMP` without TZ anywhere (CI query)
- [ ] `EXPLAIN` on the open-incidents query uses the partial index; the 30-day MTTR trend hits a cagg
- [ ] Compression on a populated `signal_samples` chunk achieves ~8–12×

**`incidentpilot-resilience-brownout`** (W7)
- [ ] All four degradation levels can be induced deliberately and each announces itself
- [ ] `ip_degradation_level` is a gauge, alerted at ≥ 2
- [ ] The lifeboat posts within 120 s of total unavailability, from an image sharing no code
- [ ] `docs/FMEA.md` has 18 rows, each with a **residual risk**
- [ ] Every chaos invariant passes in CI or in a recorded run

**`incidentpilot-nextjs-dashboard`** (W8)
- [ ] `next build` passes with TS strict and no `any` in `app/` or `components/`
- [ ] Every page renders a sensible empty state and a loading skeleton
- [ ] `DEMO_MODE=true` serves the full dashboard from fixtures with **no backend writes possible**
- [ ] Lighthouse accessibility ≥ 95 on the incident list and PIR viewer
- [ ] Citation chips resolve to real sources in the demo dataset

## Appendix E — Open decisions requiring your input

Four decisions this plan could not make on your behalf. Three have a recommended default already
applied, so the build is not blocked; the fourth needs an answer before week 6.

---

**E-1 — The `synthesize` model default (needs an answer before W6-11)**

Rev 2 §D.2 flags that GPT-5.6 Luna's 30 July price cut put a current-generation model roughly **4×
below** GPT-5.4-mini, and deliberately **did not** change your portfolio-wide baseline. This plan
follows that: `config/models.yaml` ships **GPT-5.4-mini** for `synthesize`, for consistency with your
other capstones.

**The mechanism to settle it is built in week 7**, and this is the best possible use of it:

```bash
python -m eval.run --corpus bot/eval/corpus --set roles.synthesize.model=<candidate> --report cf.md
```

Compare `timeline_f1`, `action_item_recall`, and `cost_per_pir_usd` against the committed baseline
and promote whichever wins on your own golden set. That turns a price-list argument into an
evidence-backed decision, which is exactly the interview story.

**Also outstanding from Rev 2 §22.1:** whether to sweep `slack-bolt` (HealOps pins 1.28.x, 1.30.0
has shipped) and Alertmanager (HealOps/ChaosProof pin 0.33.1, 0.34.0 shipped 16 Aug) across the
portfolio. This plan pins the newer versions here. If you bump Alertmanager in one project, bump it
in all three — otherwise the `reason`-label change (§15.4) leaves three dashboards inconsistent.

---

**E-2 — Prometheus scrape interval for the demo cluster (X-04)**

Default applied: 15 s scrape, sampler at `step=15s`, ~4,800 rows/incident, and
`SCALING_DECISION.md` records the corrected arithmetic honestly.

The alternative is a 5 s scrape interval on the demo cluster with the sampler at `step=5s`, which
restores ~14,400 rows/incident and a stronger volume story — at the cost of a non-default Prometheus
config you would need to explain. **My recommendation is to keep 15 s and keep the correction**: Rev
2 §20.2 Q25 makes "what did you get wrong" a headline answer, and a second self-caught arithmetic
error is a *better* answer than a bigger number.

---

**E-3 — D4's auto-PR job: ship or defer?**

Default applied: **defer to post-MVP**, build the signal capture and the corrected query in week 5.
The reason is in §2.1 — the query filters on `uses >= 5` and you will not have five incidents per
runbook inside eight weeks, so the job would run and produce nothing.

If you would rather demo it, the honest version is to seed the corpus with synthetic runbook usage
and label the screenshot "on seeded data." Say which it is; do not let a viewer assume production.

---

**E-4 — Where this plan lives**

Default applied: `IncidentPilot_Detailed_Implementation_Plan.md` in the repo root, alongside Rev 1
and Rev 2. If you would rather it lived in `docs/`, move it before the first commit of week 1 so no
links break.

## Appendix F — Risk register with build-order mitigations

Rev 2 §21.2's register, extended with the **week** each mitigation lands, so a risk is never
theoretical for longer than it has to be.

| Risk | Likelihood | Impact | Mitigation | Lands |
|---|---|---|---|---|
| Slack changes API terms again | **High** (they did in 2025 *and* 2026) | High | The chat adapter isolates it; Mattermost/Discord implementations are ~200 lines each. **Say this as a design justification, not a hope** | W3 |
| Model provider deprecates a model mid-project | High | Low | Roles + config + eval gate — that is the entire point of §14 | W6/W7 |
| Eval corpus too small to be meaningful | Medium | Medium | 40 incidents is honest for a solo project — **state the number and its limitation** rather than implying statistical power you do not have | W7 |
| Over-correlation merges unrelated incidents | Medium | High | Severity boundary guard (C-03), explainable scores, `/split`, feedback into the corpus | W2/W5 |
| Scope creep past 8 weeks | **High** | High | The §0.2 cut order; the eval harness and validator are never cut | ongoing |
| Demo depends on a live Slack workspace | Medium | High | `DEMO_MODE` + fake chat adapter (C-02) + recorded GIFs. **Never let a demo depend on someone else's uptime** | W3/W8 |
| `domain/` purity rots under time pressure | **High** | **Critical** | `T:test_domain_purity` in CI from **week 1**, before there is anything to be lazy about | W1 |
| A "temporary" direct Slack call bypasses the outbox | Medium | High | `T:test_no_external_writes_outside_relay` import-graph assertion, added the same week as the relay | W3 |
| Validator too strict → everything degrades to skeleton | Medium | High | Tune `support_threshold` on the corpus; report the false-rejection rate; counterfactual `--set validator.support_threshold=0.8` | W7 |
| Alertmanager 0.34 label change silently breaks a panel | Low | Medium | Grep provisioned Grafana JSON for `reason="clientError"` before shipping (§15.4) | W8 |

---

## Closing note — the three things this plan protects hardest

1. **`domain/` purity (INV-01), from week 1.** Everything downstream — replay, counterfactuals, the
   eval gate, the nine-second corpus run — is a consequence of it. It is also the only invariant that
   cannot be added later without a rewrite, which is why its test ships before the code it guards.

2. **The outbox as the sole external writer (INV-03).** The orphaned-channel class of bug is
   guaranteed at 30 incidents/day without it, and it is the failure an SRE interviewer will probe
   first because it is the one they have personally cleaned up.

3. **The citation invariant (INV-05/06).** It is the difference between "AI-assisted" and
   "AI-governed," and it is the reason this project belongs in a 2026 interview rather than a 2023
   one. Everything else on the list — storm compression, fatigue routing, brownout — is good
   engineering that someone else could also have done. Making ungrounded output *structurally
   impossible*, and then proving it with a gate that goes red, is the part nobody else has built.

*Derived from IncidentPilot Implementation Plan Rev 2 (5 September 2026), eight project skills, the
starter-code reference, and the schema and eval-harness reference files. Re-verify version pins and
the competitive landscape before interviews — both move.*
