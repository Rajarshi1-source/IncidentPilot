# IncidentPilot

Self-hosted incident response with **citation-anchored post-incident reviews**.
Forty alerts become one war room; the postmortem cannot make a claim it cannot
cite; and a one-word change to a prompt turns CI red.

```bash
git clone https://github.com/Rajarshi1-source/IncidentPilot && cd IncidentPilot
docker compose up -d          # no .env, no API key, nothing to configure
./scripts/demo.sh storm --alerts 40 --service payments
./scripts/demo.sh chatter --incident 1
./scripts/demo.sh resolve  --incident 1
./scripts/smoke.sh --expect-channel --expect-pir
```

Dashboard on <http://localhost:13000>, API on <http://localhost:18000>.
That sequence is the acceptance gate for the final week, and it runs three times
consecutively from empty volumes with no manual intervention.

---

## What it does

An alert storm arrives. Instead of forty Slack channels, correlation produces
**one incident** and says why it merged each alert — *"0.87 score: 9s after last
activity, 1 hop from payments-api, label overlap 0.43"*. The right human is
paged, weighted by how much they have already been paged today. The channel
becomes a **write path**: every message is stored as it arrives, so the
postmortem is a database query rather than ten minutes of paginated history
calls.

When the incident resolves, impact is computed from PromQL **before any model
runs**, and the generated review is checked by a deterministic validator that
rejects any claim whose citation does not resolve to something actually stored.
If the model fabricates, it retries; if it fabricates again, it degrades to a
skeleton that needs no vendor and no network. Nothing is ever published with a
claim that cannot be traced.

---

## The five things that make it different

**1 · Evidence-anchored PIRs.** `Claim.citations` has `min_length=1`, so an
uncited claim is *unrepresentable* — a schema constraint, not a prompt
instruction. Six citation kinds with a stable ID grammar, validated by set
membership and token overlap. No model sits in the gate, because a gate that can
hallucinate is not a gate.

**2 · A replay harness and a real merge gate.** 40 recorded incidents across 11
buckets replay against fake adapters in **1.8 seconds with zero network calls**,
enforced by a socket guard rather than by intention. Hard gates are
deterministic; the LLM judge is measured against human labels (`judge_agreement:
0.87, kappa 0.68`) and reported, never gated on. Editing one word of a prompt
turns the build red with a sticky comment naming what moved.

**3 · Alert-storm compression.** Three weighted signals — temporal proximity to
the incident's active span, topological distance through the service graph,
stable-label overlap — with a severity guard that refuses to hide a Sev1 inside
a Sev3. Explainable by construction: you have to justify a merge in a Slack
message, and *"0.71: 45s apart, 1 hop from postgres-primary"* is checkable in a
way a classifier's logit is not.

**4 · Brownout and a lifeboat.** Four degradation levels, each inducible and each
**announced** — silent degradation is worse than failure, because people keep
trusting output that is no longer trustworthy. When everything is down, a
separate CronJob from an image that shares no code with the bot posts the last
known on-call to a fallback channel. Its AST is walked in CI to prove it imports
nothing but the standard library.

**5 · It knows its own SLOs.** Eight SLIs written in week one, before the code
that measures them, with multi-window burn-rate alerting. Two are unusual on
purpose: **transcript completeness** is the real availability metric (the bot can
be 100% up and still have silently dropped three messages), and **PIR grounding
has a zero error budget**, because it is an invariant rather than a target.

---

## Architecture

Five processes, split along what actually bounds them: ingest is latency-bound,
orchestration is CPU-bound, the relay is the only external writer, the scheduler
owns the one job that can take a minute. Full diagrams in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
Alertmanager ─┐
Slack events ─┼─► api ─► Valkey Streams ─► worker ─► PostgreSQL ─► relay ─► Slack
Pager webhook ┘   202     (durable)      (correlate)   (+outbox,     (the only
                  fast                                  one txn)     external writer)
                                                            │
                          scheduler ──► PIR pipeline ◄──────┘
                                             │
                                        dashboard
```

The transactional outbox is the centre of it: the state change and the intent to
perform a side effect commit in **one transaction**, so there is never a moment
where the database believes a channel exists and Slack disagrees. Twelve crash
points are injected and tested.

---

## The eight gates

Each week ends in a binary gate. A red gate stops the week — that is the point of
having them.

| | Gate | What it proves |
|---|---|---|
| G1 | Bad bearer → 401; good → 202 at p99 < 250 ms | the trust boundary, and that ingest is fast |
| G2 | 40 alerts → **1 incident**, 39 attached, 1 root signal | storm compression, and a state machine with no illegal path |
| G3 | Kill the worker mid-orchestration → no duplicate channel, 12/12 crash points | idempotency that survives a crash |
| G4 | 200 messages → 200 rows, **zero history calls** | the transcript is a write path |
| G5 | Pager unreachable → cache → static rota → team channel, **each announced** | a silent fallback fails the gate |
| G6 | Provider blocked → skeleton in 90 s; unblocked → zero uncited claims | the product works without a vendor |
| G7 | A one-word prompt change turns CI **red** | the quality gate actually runs |
| G8 | Clean clone, no `.env`, full demo ×3, no intervention | it runs for a stranger |

```bash
make gate-g2   # …through gate-g8
make eval      # replay 40 incidents, print every metric
```

---

## What I got wrong

The most useful section in this repository. Every item was found by a gate, not
by review — and most share one shape: **each half correct, the composition
wrong.**

- **CI was red for three weeks and reported green locally.** The integration
  suite never applied migrations; the schema existed as a side effect of one test
  module happening to run fourth alphabetically. Fixed with a session-scoped
  fixture *and* an explicit CI step, so the dependency is visible rather than
  implied by collection order.
- **The budget breaker was never awaited.** `LLMRouter` called
  `self._budget.check(...)` without `await` — built a coroutine, discarded it,
  proceeded. Every direct unit test of `BudgetBreaker` passed, because they
  called it themselves. Caught by an assertion phrased as a consequence:
  `assert provider.calls == 0`.
- **`PIRGenerator` was never instantiated anywhere in `src/`.** The flagship —
  generator, validator, citation grammar, fallback ladder — was fully built and
  gate-tested, and the running product never called it. G6 passed because the
  gate script constructs the generator itself: it proved the component worked,
  not that anything used it.
- **A 40-alert cascade became 36 incidents on a clean clone.** The dependency
  graph ships as YAML, but `services.graph_depth` is cached in a table nothing
  seeded. Every earlier gate ran against a database some previous test had
  populated. D3 — the differentiator the whole of week 2 exists for — was
  silently absent on exactly the scenario the final gate tests.
- **Out-of-order alerts could not correlate.** The temporal term guarded on
  `0 <= dt`, and at-least-once delivery *guarantees* out-of-order arrival. One
  reclaimed alert opened a second war room for the same cascade. Direction was
  never the signal; proximity is.
- **`ip_pir_citation_coverage` read 0 before anything was measured**, so a fresh
  pod tripped `CitationCoverageBelowOne` and IncidentPilot opened an incident
  *about itself* through its own webhook. Self-monitoring working correctly on a
  metric that was lying.
- **TimescaleDB was justified with arithmetic that was wrong twice.**
  [docs/SCALING_DECISION.md](docs/SCALING_DECISION.md) records both corrections
  and states the trigger for reversing the decision. Volunteering the *second*
  correction is stronger than volunteering the first.

The full failure-mode table, each row with a **residual risk** column — what is
still broken after the mitigation — is in [docs/FMEA.md](docs/FMEA.md).

---

## Running it

```bash
make up            # the whole stack: api, worker, relay, scheduler, dashboard
make test          # 536 unit tests
make test-integration   # 102 against a real PostgreSQL + Valkey
make eval          # the 40-incident replay corpus
make lint          # ruff, mypy --strict, and the no-hardcoded-model grep
```

Nothing above needs a credential. `IP_CHAT_PROVIDER` defaults to `fake`, the
metrics adapter is deterministic, and the PIR degrades to its skeleton without a
model — so a clean clone produces a real war room and a real (if unlovely)
postmortem.

To point it at a real Slack workspace, copy `.env.example` and fill in the four
values it asks for.

---

## Layout

```
bot/            the Python service — api, worker, relay, scheduler
bot/eval/       the replay harness and the 40-incident corpus
dashboard/      Next.js 16 on Node 24, Server Components by default
lifeboat/       ~130 lines, standard library only, its own image
charts/         Helm — three deployments, HPA on stream lag, no secrets.yaml
runbooks/       eight runbooks with parseable step IDs
monitoring/     Prometheus rules, Alertmanager routing, two Grafana dashboards
docs/           ADRs, the FMEA, the SLOs, the scaling decision
```

---

## Decisions worth reading

- [ADR 0003](docs/ADR/0003-transcript-completeness-is-a-sample.md) — why the
  headline SLI is a sample, and what would change if the app were Marketplace-approved
- [ADR 0005](docs/ADR/0005-grounding-the-pir.md) — why the skeleton reports
  coverage 0.0 rather than 1.0
- [ADR 0006](docs/ADR/0006-replay-and-the-eval-gate.md) — what replay **cannot**
  measure, and why the gate says so instead of pretending
- [docs/SLO.md](docs/SLO.md) — eight SLIs, written before the code that measures them
- [docs/FMEA.md](docs/FMEA.md) — 18 failure modes, each with its residual risk

---

## Status

Eight weeks, eight gates, all green. Built as a portfolio capstone; the
engineering is real, the demo data is synthetic and the dashboard says so on
every page.
