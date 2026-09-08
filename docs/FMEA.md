# Failure modes, effects and analysis

**The residual-risk column is the point of this document.** Detection and
mitigation are the easy columns — every failure mode has something written in
them, and a table with only those two reads as though everything is covered.
Residual risk forces the admission of what is *still* broken after the
mitigation, which is both better engineering and a far better answer to "what
would take this down?" than a claim that nothing would.

Twenty rows (eighteen from week 7, two added in week 8). Where a row's mitigation is a specific test, gate or file, it is
named — a mitigation with no artifact behind it is an intention.

Scored on the usual triple: **S**everity, **L**ikelihood and **D**etectability,
each 1–5, with detectability inverted (5 = we would not notice). RPN is their
product. The ordering below is by RPN, and the top four are the ones worth
arguing about.

---

## The table

| # | Failure | Effect | Detection | Mitigation | Residual risk | S | L | D | RPN |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **Slack events silently dropped** (delivery failure, our 5xx, a missed retry) | The transcript has holes. Every PIR grounded in it is wrong, and no error is raised anywhere | `ip_transcript_completeness`, `TranscriptCompletenessBelowObjective` | Event-sourced writes with `UNIQUE (channel_id, ts)`; the reconciler compares against Slack's newest page once a minute | **The SLI is a sample, not a census.** Slack allows one `conversations.history` call per minute and fifteen messages per call, so counting a channel is not an available operation. A message lost three hours ago in a channel the round-robin has already visited will not be found (ADR 0003) | 5 | 3 | 5 | **75** |
| 2 | **Fabricated citation reaches a published PIR** | A postmortem asserts something with a reference that resolves to nothing. The grounding guarantee is the product | `CitationCoverageBelowOne`; `fabricated_citations` is a hard gate on every eval run | `Claim.citations` has `min_length=1` so an uncited claim is unrepresentable; the deterministic validator checks every ref against `valid_reference_set()`; two rejections degrade to the skeleton | **A real ID attached to a wrong-but-plausible sentence.** `_supports()` is token overlap plus shared entities, not comprehension. A claim that reuses the vocabulary of a message it misrepresents passes, and only a human reading the PIR would catch it | 5 | 2 | 4 | **40** |
| 3 | **Impact numbers invented by the model** | A fabricated availability figure in a document titled "Post-Incident Review" ends up in a board deck | `ungrounded_number` on `ip_pir_validation_failures_total`; B-10 fixture in the corpus | Impact is computed from PromQL *before* any model call; `impact` is absent from `PIRDraft` so the model cannot be asked for one; the validator rejects any number not in the computed set | **Numbers below the trivial threshold pass.** `extract_numbers` ignores values ≤ 10 without a decimal, because rejecting "three replicas" would push honest drafts to the skeleton. "We lost 8 minutes" is therefore unchecked | 5 | 2 | 4 | **40** |
| 4 | **Alert storm creates one channel per alert** | Forty war rooms, responders split across twelve of them, and the `conversations.create` rate limit hit at the worst moment | `ip_alerts_per_incident`; `storm_compression` floor of 0.95 in the eval gate | Best-match correlation over three weighted signals with a severity guard; four storm fixtures in the corpus (5/12/40/40 alerts) | **Over-correlation is the opposite and worse failure.** Two genuinely separate outages merged into one incident hides one of them. `storm_compression` is symmetric so the metric catches it, but only against a *labelled* expectation — an unlabelled real-world over-merge looks like excellent compression | 4 | 3 | 3 | **36** |
| 5 | **Postgres unreachable during ingest** | Nothing can be orchestrated. If alerts are dropped, they are dropped permanently — Alertmanager will not resend | `DegradationLevelElevated`, `ip_brownout_buffered_total` | Brownout L2: webhooks still return 202, alerts buffer to `alerts.wal`, `replay_wal()` drains in order on recovery (`test_brownout_buffers_and_replays`) | **If Valkey is down too, the 503 is honest but the alert is still lost** — Alertmanager's own retry is the only remaining recovery, and it is time-bounded. There is deliberately no local disk spool: the pod is the thing most likely to be replaced during the outage that filled it | 5 | 2 | 3 | **30** |
| 6 | **Relay crashes after the external call, before recording it** | An orphaned Slack channel with no incident behind it, or a duplicate war room on retry | Twelve injected crash points in `test_crash_matrix.py` (G3) | Transactional outbox with `idempotency_key UNIQUE`; every external write is idempotent by key; compensation is safe to run twice | **Slack's own idempotency is what the key rests on.** If a provider stopped honouring a repeated `conversations.create` for the same name, the guarantee weakens to "usually one channel" — and the failure would appear as a duplicate, not an error | 3 | 3 | 3 | **27** |
| 7 | **The eval corpus rots** | The gate keeps passing while measuring something other than the current system. Everyone believes there is a gate | `prompt_drift` is a hard gate row; `format: 1` header refused if it does not match; the nightly job reports corpus age | Recordings carry the prompt hash they were produced under; a mismatch fails the gate by name rather than scoring stale responses | **Only the prompt is version-checked.** A change to the *evidence assembly* — a new field in the context, a different truncation rule — changes what the model would have seen without changing any hash, and the corpus would score it as if nothing had happened | 4 | 3 | 2 | **24** |
| 8 | **Model provider down or rate-limiting** | No AI-drafted PIR on exactly the days incidents cluster | `ip_llm_calls_total{outcome="error"}`, `EverythingIsASkeleton` | Two providers then the deterministic skeleton, which needs no key and no network; G6 measures the skeleton at 0.02 s against a 90 s budget | **The skeleton is a real degradation, not an equivalent.** It carries the timeline, the impact block and the evidence index, and no narrative or action items. Delivery meets its SLO while the feature is effectively absent, which is why `EverythingIsASkeleton` is a separate alert | 3 | 4 | 2 | **24** |
| 9 | **Paging provider unreachable** | The right human is never woken, and nobody knows it | `OnCall.source` is on the result and the handler announces every degraded rung (G5) | Ladder: provider → cache → static YAML rota → team-channel broadcast, each announced in-channel | **The static rota is a file in git and goes stale silently.** If the roster changed last month and nobody edited the YAML, the fallback confidently pages the wrong person — which is worse than paging nobody, because it looks like it worked | 4 | 3 | 2 | **24** |
| 10 | **Valkey unavailable** | Rate limiting, dedup claims, the channel cache and the budget counters all lose their store | `ip_stream_pending` stops reporting; connection errors in logs | Nothing is cache-only (INV-04): every read has a DB fallback, and the real dedup guard is `UNIQUE (dedup_key, dedup_epoch)` in Postgres | **The budget breaker fails *open*.** An unreadable counter reads as zero spend, deliberately — failing closed would let a cache blip remove the PIR feature. So a Valkey outage during a runaway retry loop removes the cost ceiling at the moment it is most needed | 3 | 3 | 2 | **18** |
| 11 | **Slack 429 storm** | Channel creation and paging queue behind decorative work; time-to-war-room blows its SLO | `ip_slack_rate_limited_total{method,priority}` | Per-method *and* per-channel token buckets, priority lanes, and shedding that drops priority 2–3 first (B-12) | **Shedding is counted but the shed work is gone.** A dropped timer refresh is invisible to the responder, who sees a stale duration and may conclude the incident is older than it is | 3 | 3 | 2 | **18** |
| 12 | **Worker OOM or eviction mid-orchestration** | An incident half-created: a channel with no responder, or a state advance never recorded | Pending Entries List depth; `XAUTOCLAIM` reclaim counters | Consumer groups with explicit ACK; `UNIQUE (incident_id, seq)` on transitions makes a double advance impossible | **Reclaim latency is a real gap.** An entry sits in the PEL for the idle timeout before another worker takes it, so a pod killed at the wrong moment costs that delay on an incident whose whole objective is ten seconds | 3 | 3 | 2 | **18** |
| 13 | **Prometheus unreachable at PIR time** | Impact cannot be computed for the incident window | `impact.unavailable` log; three `metrics_unavailable` fixtures in the corpus | The report records `{"status": "unavailable"}` with a reason and the prompt says *do not estimate*; the renderer prints "unavailable", never a zero | **A gap that says it is a gap is still a gap.** The PIR ships without the number a reader most wants, and there is no backfill: the window has passed and the samples for it may have been retained away by the time anyone notices | 3 | 3 | 2 | **18** |
| 14 | **Duplicate Slack event delivery** | The same message stored twice; a doubled transcript and doubled citations | `ip_duplicate_messages_total` | `UNIQUE (channel_id, ts)` rejects the second write; the counter makes the rejection visible rather than silent | **Only exact redelivery is caught.** A message *edited* to identical text arrives with a new revision and is appended, which is correct behaviour for an append-only transcript but means the same words can legitimately appear twice | 2 | 4 | 2 | **16** |
| 15 | **Model output does not match the schema** | No PIR from that attempt | Parse failure recorded as a permanent provider error; two `invalid_json` fixtures in the corpus | Structured output validated at the boundary; a schema mismatch is treated as *permanent* (it will not parse on retry) so the chain moves down rather than spending the budget again | **A response that parses but is subtly wrong is not caught here.** The schema constrains shape, not truth; that is the validator's job, and the validator is token overlap (see row 2) | 2 | 3 | 2 | **12** |
| 16 | **Cost runaway** | A retry loop burns the month's model budget in an afternoon | `ip_budget_trips_total`, `BudgetBreakerTripping`, `CostPerIncidentAboveObjective` | Three tiers checked *before* the call — per incident, per day, per month — so a tripped budget costs nothing rather than one wasted call | **The breaker degrades rather than blocks, by design.** A cost control that can make an outage worse is a bug, so a compromised or looping process still gets the skeleton path and still consumes non-model resources | 3 | 2 | 2 | **12** |
| 17 | **Clock skew between the API and the workers** | Correlation windows and SLA nudges measured against the wrong now; a cascade at the window boundary splits into two incidents | Correlated-alert counts that disagree with the alert timestamps | All timestamps are `TIMESTAMPTZ`, connections `SET timezone = 'UTC'`, and time is injected rather than read ambiently — which is also what makes replay deterministic | **Nothing detects skew directly.** There is no clock-drift alert, so a slow NTP failure on one node would show up as inexplicable correlation behaviour rather than as a clock problem | 3 | 2 | 3 | **18** |
| 18 | **IncidentPilot itself is down** | The tool needed most when things are broken is the thing that is broken | `IncidentPilotDown` with `route: fallback`; the lifeboat CronJob probes `/readyz` every minute | The alert bypasses IncidentPilot entirely (INV-11, `test_alertmanager_config.py`); the lifeboat posts the last known on-call from a separate image sharing no code (`test_lifeboat_imports_nothing`) | **The lifeboat depends on three things it cannot verify:** the fallback webhook still being valid, `/state/oncall.json` having been written while the bot was healthy, and the cluster still scheduling CronJobs. A control-plane failure takes the lifeboat with it, and nothing below it catches that | 5 | 1 | 2 | **10** |

---

## Two rows week 8 added, and why they were not there before

Both were found by the first gate that starts from **empty volumes**. Every
earlier gate ran against a database some previous test had populated, which is
why neither was visible for seven weeks.

| # | Failure | Effect | Detection | Mitigation | Residual risk | S | L | D | RPN |
|---|---|---|---|---|---|---|---|---|---|
| 19 | **Reference data never seeded on a fresh deployment** | `services.graph_depth` is empty, correlation's topology term scores zero for every alert, and a 40-alert cascade becomes 36 incidents. D3 absent, every pod healthy | G8 asserts ≥10 services before running the demo; the smoke test asserts *exactly one* incident rather than "at least one" | `config/sync_reference.py` runs after `alembic upgrade head`, in the same one-shot service and the same Kubernetes Job | **Nothing detects a graph that is present but wrong.** An edge deleted from `service-graph.yaml` by mistake re-syncs cleanly and quietly weakens correlation; only the eval corpus's storm fixtures would notice, and only if someone runs them | 4 | 2 | 5 | **40** |
| 20 | **A component is built, tested and never called** | The flagship PIR pipeline existed for two weeks without the product ever invoking it. `ensure_engaged` likewise: incidents sat in `detected` and no war room ever opened | Only an end-to-end run through the public HTTP surface. Unit and gate tests both construct the component themselves | `scripts/demo.sh` drives everything through the real webhooks, and `smoke.sh` asserts the *outcome* (a channel exists, a PIR row was written) rather than that a function returned | **This is a class, not an instance.** Six occurrences so far, and the only detector is an end-to-end path that exercises the wiring. Any component added without a demo step that reaches it is invisible to this mitigation in exactly the same way | 5 | 3 | 5 | **75** |

Row 20 ties with row 1 at the top, and it deserves to. Both are failures with no
error: the system reports success and simply does less than it claims. The
mitigation for both is the same in shape — assert on a *consequence* observed
from outside, never on a call having been made.

## What the ordering says

Row 1 sits at the top not because it is likely but because **it is the one we
would not notice**. Detectability 5 is the honest score for a failure with no
error, no 5xx and no exception — a transcript that is quietly shorter than it
should be — and it is the reason transcript completeness is the SLI with the
tightest target in `docs/SLO.md` rather than an afterthought.

Rows 2 and 3 are the product's central promise and its central risk. Both are
mitigated by mechanisms that are deterministic and cheap, and both have a
residual risk of the same shape: **the validator checks provenance, not
meaning.** A citation that exists and shares vocabulary with its claim passes,
whether or not it supports it. Saying that plainly is more useful than claiming
the grounding problem is solved, and it is where the next real work is.

## What is deliberately absent

There is no row for "the model produces a low-quality narrative". It is not a
failure mode with a detection strategy — it is a quality gradient, it is what
the eval corpus and the judge measure, and putting it in an FMEA would dilute a
table whose value is that every row names something that either happens or does
not.

There is also no row for "the dashboard is down". It is a read-only view over
data that is already durable, and its absence costs nobody an incident.

## How this table is maintained

Every production surprise becomes a corpus fixture (§11 of the eval reference)
*and* a row here, or the row's residual risk is updated to admit that the
surprise was in it all along. A row that has never changed since it was written
is a row nobody has tested against reality.
