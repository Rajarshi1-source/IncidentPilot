# Architecture

Diagrams are Mermaid, rendered by GitHub. No exported PNGs — a diagram that
lives as an image is a diagram that goes stale the first time somebody changes
the code and cannot regenerate it.

---

## The shape of the system

```mermaid
flowchart LR
  subgraph edge["Trust boundary"]
    AM[Alertmanager<br/>bearer + mTLS + NetworkPolicy]
    SL[Slack events<br/>HMAC signed]
    PG[Pager webhook<br/>HMAC signed]
  end

  AM --> API
  SL --> API
  PG --> API

  API[["api<br/>verify → normalize → XADD → 202"]]
  API -->|alerts.raw| VK[(Valkey Streams)]
  API -->|messages| DB[(PostgreSQL<br/>+ TimescaleDB)]

  VK -->|XREADGROUP| W[["worker<br/>correlate → state machine"]]
  W --> DB
  W -.->|outbox rows,<br/>same transaction| DB

  DB -->|FOR UPDATE SKIP LOCKED| R[["relay<br/>THE ONLY external writer"]]
  R -->|create channel, page,<br/>pin runbook| OUT[Slack / PagerDuty]

  SCH[["scheduler<br/>SLA nudge · abandonment<br/>reconcile · PIR sweep"]] --> DB
  SCH -->|resolve → PIR| LLM[LLM provider]

  DB --> QAPI[["/api read layer<br/>5s reads · 30s analytics"]]
  QAPI --> DASH[["dashboard<br/>Next.js, Server Components"]]

  LB[["lifeboat<br/>separate image, stdlib only"]] -.->|probes /readyz| API
  LB -.->|posts when we are gone| FALLBACK[Fallback channel]
```

**Five processes, and the split is the bulkhead.** Ingest is latency-bound with
a 250 ms p99 to hold. Orchestration is CPU-bound on correlation and intent
classification. The relay is the only process permitted to perform an external
write. The scheduler owns the one job that can take sixty seconds and spend
money. Sharing any two of them means a forty-alert storm competes with the
webhook still trying to return 202.

The lifeboat is dotted because it shares nothing with the rest — different
image, standard library only, its own failure domain. If it imported the
application it would fail for the same reason the application failed.

---

## The write path, and why the outbox exists

```mermaid
sequenceDiagram
  autonumber
  participant AM as Alertmanager
  participant API as api
  participant VK as Valkey
  participant W as worker
  participant DB as PostgreSQL
  participant R as relay
  participant SL as Slack

  AM->>API: POST /webhooks/alertmanager (40 alerts)
  API->>API: verify bearer, normalize
  API->>VK: XADD alerts.raw (pipelined)
  API-->>AM: 202 Accepted

  loop per alert
    W->>VK: XREADGROUP
    W->>DB: BEGIN
    W->>DB: correlate → merge or create
    W->>DB: INSERT outbox_events (SAME transaction)
    W->>DB: COMMIT
    W->>VK: XACK
  end

  R->>DB: SELECT ... FOR UPDATE SKIP LOCKED
  R->>SL: conversations.create (idempotency key)
  SL-->>R: channel
  R->>DB: mark dispatched
```

The transaction boundary at steps 8–11 is the whole design. The state change and
the intent to perform a side effect commit **together**, so there is no window
in which the database believes a channel exists and Slack disagrees. The relay
then retries at least once, and the idempotency key is what makes retrying an
external write safe.

Crash between steps 14 and 16 — after Slack created the channel, before we
recorded it — is crash point 8 of twelve in `test_crash_matrix.py`. On retry the
same key returns the same channel.

---

## The PIR pipeline

```mermaid
flowchart TD
  RES[incident resolved] --> SWEEP[scheduler sweep, 20s]
  SWEEP -->|"transition: resolved → pir_drafting<br/>UNIQUE (incident_id, seq) stops a race"| CTX

  CTX[build grounded context<br/>6 queries, no Slack] --> IMP
  IMP["compute impact from PromQL<br/><b>before any model call</b>"] --> PROMPT

  PROMPT[render prompt<br/>evidence + verified facts] --> L1
  L1{{"layer 1: primary model"}} --> VAL
  VAL{deterministic validator}

  VAL -->|"ok"| PUB[persist + post<br/>coverage 1.000]
  VAL -->|"fabricated ID /<br/>ungrounded number"| L2{{"layer 2: second provider"}}
  L2 --> VAL2{validator}
  VAL2 -->|ok| PUB
  VAL2 -->|"fails again"| SKEL

  L1 -.->|"budget exceeded /<br/>all providers down"| SKEL
  SKEL["layer 3: deterministic skeleton<br/>no key, no network, coverage 0.0"] --> PUB
```

**Impact is computed before the model runs**, and `impact` is absent from the
draft schema, so the model cannot be asked for a number. The validator rejects
any number in the draft that is not in the computed set.

The skeleton reports coverage **0.0, not 1.0**. It makes no cited claims, and
recording perfect grounding for it would make the one SLI with a zero error
budget look healthy on exactly the days the feature was unavailable.

---

## Degradation

```mermaid
stateDiagram-v2
  [*] --> L0
  L0: L0 NORMAL — full pipeline
  L1: L1 DEGRADED — skeleton PIRs, banner
  L2: L2 BROWNOUT — 202 still, alerts buffered to alerts.wal
  L3: L3 LIFEBOAT — separate image posts to a fallback channel

  L0 --> L1: provider open / over budget
  L1 --> L2: database unreachable
  L2 --> L3: our own readiness failing > 60s
  L1 --> L0: recovered
  L2 --> L0: recovered
  L3 --> L0: recovered
```

Every transition is **announced**. Silent degradation is worse than failure: a
product that fails visibly gets worked around, and one that quietly starts
producing less trustworthy output keeps being trusted.

L2 keeps accepting alerts because **a dropped alert is unrecoverable and a
delayed one is not** — that asymmetry is why ingest is the one AP component in
an otherwise CP system.

---

## What is deliberately not here

- **No websockets.** A 15-second revalidation is enough for a handful of
  concurrent incidents. Websockets would be complexity with no payoff, and
  saying so is a better answer than adding them for show.
- **No FK from the hypertables to `incidents`.** FK enforcement across many
  chunks is a chunk-management cost with little payoff; integrity is enforced in
  the application plus a periodic orphan check. A considered trade, not an
  oversight.
- **No `secrets.yaml` in the Helm chart, ever.** A chart that templates Secret
  objects invites a `values-prod.yaml` with real credentials, and that file
  reaches git within about two weeks. Secrets are references to objects created
  out of band; `helm template` with default values emits zero of them, and a CI
  step asserts it.
- **No auto-promotion of the eval baseline.** A gate that promotes its own
  baseline on green ratchets to whatever it last measured and can never detect a
  slow regression.

---

## The invariants, and where each is enforced

| # | Invariant | Enforced by |
|---|---|---|
| INV-01 | `domain/` is pure — no I/O, clock, randomness | `test_domain_purity.py` walks the AST |
| INV-02 | `conversations.history` is never called on the hot path | the fake adapter **raises**; a Prometheus rule watches production |
| INV-03 | Only the relay performs external writes | AST test over every module, one documented exception |
| INV-04 | Nothing correctness-critical lives only in cache | every read has a DB fallback |
| INV-05 | An uncited claim is unrepresentable | `Claim.citations` has `min_length=1` |
| INV-06 | Every number in a PIR was computed, not generated | validator checks against the computed set |
| INV-07 | No model name in code outside `config/` | CI grep + unit test |
| INV-08 | No naked `TIMESTAMP` | schema query returning zero rows |
| INV-09 | `timeline_events` segments by `intent` | compression-ratio test ≥ 8× |
| INV-10 | Replay makes zero network calls | socket guard, asserted per fixture |
| INV-11 | `IncidentPilotDown` does not route through us | `test_alertmanager_config.py` |
| INV-12 | Degradation is announced | `test_degradation_announces` |

Every one of these is a test rather than a paragraph. A paragraph describes
what somebody intended; a test describes what is true this morning.
