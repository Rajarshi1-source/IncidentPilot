# IncidentPilot — Service Level Objectives

**Written in week 1, before the code that measures them.** That ordering is
deliberate and it is the point: most candidates build an SRE tool and never
define its SLIs. This is production infrastructure, so it has SLOs.

Status column reflects what is actually instrumented today, not what is planned.

---

## The eight SLIs

| SLI | Definition | SLO | Error budget (30 d) | Status |
|---|---|---|---|---|
| **Ingest availability** | `1 - (5xx on /webhooks/* ÷ total)` | **99.9 %** | 43 min | ✅ W1 |
| **Ingest latency** | p99 webhook ack | **< 250 ms** | — | ✅ W1 |
| **Time-to-war-room** | alert accepted → channel created + responder invited + runbook pinned | **p95 < 10 s** | — | ⏳ W3 |
| **Responder notified** | alert accepted → responder DM delivered | **p95 < 30 s** | — | ⏳ W5 |
| **Transcript completeness** | messages in store ÷ messages in channel (nightly reconcile) | **≥ 99.99 %** | see below | ⏳ W4 |
| **PIR delivery** | resolve → draft posted, any layer including skeleton | **99.5 %, p95 < 90 s** | 3.6 h | ⏳ W6 |
| **PIR grounding** | PIRs with zero uncited claims | **100 % — hard invariant** | **zero** | ⏳ W6 |
| **Cost per incident** | LLM spend ÷ incidents | **< $0.50** | breaker at 2× | ⏳ W6 |

## Two of these are unusual, and both are deliberate

### Transcript completeness is the real availability metric

The bot can be 100 % up and still have silently dropped three messages. After
that the PIR is wrong, the citations point at a transcript with holes in it, and
**nobody notices** — there is no error, no 5xx, no alert. Availability of a
data-collection system is measured in data, not in HTTP 200s.

This is why it has the tightest target in the table. Four nines on a ~5 000
message/day transcript still permits half a message a day; anything looser and
the grounding guarantee stops meaning anything.

### PIR grounding has a zero error budget

Every other SLO here is probabilistic. This one is an **invariant**, enforced by
a deterministic validator rather than a model, and the enforcement is a type
constraint: `Claim.citations` has `min_length=1`, so an uncited claim cannot be
represented, let alone published.

If it cannot cite, it does not ship — it degrades to a skeleton. A budget would
imply some rate of ungrounded claims is acceptable, and none is.

---

## Alerting: burn rate, not thresholds

Multi-window multi-burn-rate, per the Google SRE workbook:

| Window pair | Burn rate | Consumes | Action |
|---|---|---|---|
| 5 m and 1 h | 14.4× | 2 % of the 30-day budget in 1 h | **page** |
| 30 m and 6 h | 6× | 5 % in 6 h | **ticket** |

A single-window threshold alert on a 99.9 % target either pages constantly or
never fires. Two windows is what makes it neither: the short window catches the
fast burn, and requiring both to be lit suppresses the single-scrape spike.

Rules live in `monitoring/prometheus/rules/incidentpilot-slo.yml` (W7).

## The one alert that must not route through us

`IncidentPilotDown` carries `route: fallback` and is delivered by a receiver
that does not traverse IncidentPilot (INV-11, `monitoring/alertmanager/`). If
the only path to learning your incident tool is down runs through your incident
tool, you have built a circular dependency.

`test_fallback_route_present` asserts this against the shipped config, because a
comment in a YAML file survives exactly until someone reorganizes the routing
tree.

---

## Metric → SLI map

| SLI | Metric | Emitted by |
|---|---|---|
| Ingest availability | `ip_webhook_seconds{outcome}` | `api/webhooks/*` |
| Ingest latency | `ip_webhook_seconds` | middleware + `deps.elapsed_s` |
| Time-to-war-room | `ip_time_to_war_room_seconds` | orchestrator (W3) |
| Responder notified | `ip_time_to_acknowledge_seconds` | paging adapter (W5) |
| Transcript completeness | `ip_transcript_completeness` | reconciler (W4) |
| PIR delivery | `ip_pir_seconds{layer,outcome}` | generator (W6) |
| PIR grounding | `ip_pir_citation_coverage` | validator (W6) |
| Cost per incident | `ip_llm_cost_usd_total` | LLM router (W6) |

Metric names are a stable API — dashboards and alert rules match on them, so a
rename is a breaking change. `test_metric_names_stable` freezes the set.

---

## What is deliberately *not* an SLO

- **Dashboard availability.** It is a read surface over data that is already
  durable. If it is down during an incident, responders use Slack, which is
  where the incident actually happens.
- **PIR quality.** Not an SLO because it is not a rate — it is gated in CI by
  the eval harness (W7), where a regression blocks a merge rather than burning a
  budget. Quality belongs at the gate, not on the pager.
