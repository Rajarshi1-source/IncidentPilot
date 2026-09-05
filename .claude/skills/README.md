# IncidentPilot — Claude Skill Set

Eight skills aligned to **IncidentPilot Implementation Plan Rev 2** (5 September 2026), following
the same conventions as the KubeThrifty, HealOps, ChaosProof, and TraceMap sets: kebab-case names,
pushy `description` blocks under 1024 characters, no angle brackets in frontmatter, a `.skill`
archive with the `skillname/SKILL.md` internal path, plus a readable `.SKILL.md` copy.

## The set

| Skill | Lines | Reference file | Covers |
|---|---|---|---|
| `incidentpilot-ingest-correlation` | 191 | — | Webhook trust boundary, fingerprinting, dedup, storm compression, root-signal selection |
| `incidentpilot-state-orchestration` | 231 | — | 13-state machine, transactional outbox, consumer groups, advisory-lock partitioning |
| `incidentpilot-slack-platform` | 205 | — | Event-sourced transcript, Block Kit, slash commands, priority rate limiter |
| `incidentpilot-pir-grounding` | 231 | — | Evidence graph, citation invariant, LLM router, validator, skeleton fallback |
| `incidentpilot-mlops-eval` | 190 | `eval-harness.md` | Recorder, replayer, golden corpus, five metrics, CI merge gate |
| `incidentpilot-timescale-data` | 188 | `schema-and-queries.md` | PG 18.6 + TimescaleDB 2.29.2 schema, hypertables, aggregates, 12 analytics queries |
| `incidentpilot-resilience-brownout` | 204 | — | Four degradation levels, lifeboat, self-SLOs, FMEA, chaos validation |
| `incidentpilot-nextjs-dashboard` | 162 | — | Next.js 16.3 dashboard, PIR viewer, SLO and MLOps pages, demo mode |

## Companion documents

- **`IncidentPilot_starter_code.md`** — runnable reference implementation in 8 parts. Every skill
  points here rather than duplicating code, which is what keeps each `SKILL.md` under 250 lines.
- **`evals/trigger-eval.json`** — 44 realistic routing queries (32 should-trigger, 12 near-miss
  negatives including cross-project ones for KubeThrifty, ChaosProof, TraceMap, HealOps, PRGraph).
- **`evals/results.json`** + **`evals/score_routing.py`** — the routing pass and its scorer.
- **`evals/test-run-iteration-1.md`** — three functional test prompts run against the skills.

## How the skills relate

```
ingest-correlation ──► state-orchestration ──► slack-platform
        │                     │                     │
        │                     ▼                     ▼
        └──────────────► timescale-data ◄──── pir-grounding ──► mlops-eval
                              ▲                     │
                              │                     ▼
                       nextjs-dashboard      resilience-brownout
```

Each skill names its neighbours in the description ("Pair with ...") so multi-skill tasks pull the
right combination rather than one skill trying to cover everything.

## Eval status — read this before trusting the numbers

The routing pass scored **44/44**, which by itself means very little: claude.ai has no subagents, so
the same model that wrote the descriptions also did the routing. That is a self-consistency check on
description separation, not an independent measurement.

The useful output is the **six low-confidence calls** the pass flagged, listed in `results.json`.
Two of them drove real edits in iteration 2 — project-scoping clauses were added to the trigger
lists of `resilience-brownout` and `state-orchestration`, because "multi window burn rate" and
"transactional outbox" are generic phrases that would otherwise pull those skills into unrelated
questions.

Four remain worth watching:

| # | Risk |
|---|---|
| 10 | "citation chips" legitimately spans `slack-platform` (rendering) and `pir-grounding` (semantics) |
| 23 | "query for the dashboard" spans `timescale-data` and `nextjs-dashboard`; either is useful |
| 33 | A query about debugging real CrashLoopBackOff pods says "alertmanager" and may pull `ingest-correlation` |
| 39 | A HealOps approval-step question says "model that step" and may pull `state-orchestration` |

**To measure this properly**, run the skill-creator description optimizer in Claude Code, which
splits the eval set 60/40 and runs each query three times against a real model:

```bash
python -m scripts.run_loop \
  --eval-set evals/trigger-eval.json \
  --skill-path incidentpilot-<name> \
  --model <model-id> --max-iterations 5 --verbose
```

That loop reports a genuine trigger rate on held-out queries and proposes description improvements
from what actually failed. Everything here is the draft it should start from.

## Installing

Open any `.skill` file card and press **Save skill**, or upload it in Settings. The `.SKILL.md` and
`.<name>.md` reference copies are for reading and diffing, not for installation.

## Version pins these skills assume

Python 3.14.7 · FastAPI 0.141.1 · Pydantic 2.13.5 · SQLAlchemy 2.0.52 · psycopg 3.3.5 ·
redis-py 8.1.0 · slack-bolt 1.30.0 · PostgreSQL 18.6 · TimescaleDB 2.29.2 · pgvector 0.8+ ·
Valkey 9.1.2 · Prometheus 3.14.0 · Alertmanager 0.34.0 · Grafana 13.2.1 · Kubernetes 1.36.4
(floor 1.35) · Helm 4.2.4 · Node 24 LTS · Next.js 16.3.

Verified against upstream release feeds on 5 September 2026. Re-check before a build — three of
these moved during the two weeks before this set was written.
