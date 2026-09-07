"""The metrics the platform exposes about itself.

These names are a stable API. Grafana dashboards and the multi-window burn-rate
alert rules match on them, so a rename is a breaking change and is treated as
one -- ``test_metric_names_stable`` snapshots the full set.

Defined here in week 1, before most of the code that emits them, because the
SLOs were written first (docs/SLO.md) and a metric that arrives after the
dashboard is a metric nobody wired up.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# An explicit registry rather than the process-global default. Two reasons: the
# tests can build a fresh one per case instead of fighting duplicate-timeseries
# errors, and the replay harness can assert on emitted metrics without the rest
# of the process leaking into the assertion.
REGISTRY = CollectorRegistry()

# --- ingest ------------------------------------------------------------------
# Buckets stop at 2.5s deliberately: the SLO is p99 < 250ms, so resolution
# matters below half a second and anything above 2.5s is equally bad news.
WEBHOOK_LATENCY = Histogram(
    "ip_webhook_seconds",
    "Webhook handling latency, from request receipt to response.",
    ["source", "outcome"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)

# --- incident lifecycle ------------------------------------------------------
TIME_TO_WAR_ROOM = Histogram(
    "ip_time_to_war_room_seconds",
    "Alert accepted to channel created, responder invited and runbook pinned.",
    buckets=(1, 2, 5, 10, 20, 30, 60, 120),
    registry=REGISTRY,
)

TIME_TO_ACK = Histogram(
    "ip_time_to_acknowledge_seconds",
    "Alert accepted to first human action.",
    ["severity"],
    buckets=(10, 30, 60, 120, 300, 600, 1800),
    registry=REGISTRY,
)

INCIDENT_TRANSITIONS = Counter(
    "ip_transitions_total",
    "State machine transitions.",
    ["from_state", "to_state"],
    registry=REGISTRY,
)

# The proof of D3. Alerts absorbed per incident; a healthy storm shows a long
# right tail here and a flat incident count.
STORM_COMPRESSION = Histogram(
    "ip_alerts_per_incident",
    "Correlated alerts absorbed into a single incident.",
    buckets=(1, 2, 5, 10, 20, 50, 100),
    registry=REGISTRY,
)

# --- queue -------------------------------------------------------------------
# The HPA scales on this rather than CPU: queue depth is what actually reflects
# backlog, and a worker blocked on a slow external call uses no CPU at all.
STREAM_PENDING = Gauge(
    "ip_stream_pending",
    "Entries in the Pending Entries List, per stream and consumer group.",
    ["stream", "group"],
    registry=REGISTRY,
)

# --- PIR (D1) ----------------------------------------------------------------
PIR_GENERATION = Histogram(
    "ip_pir_seconds",
    "PIR generation latency by fallback layer and outcome.",
    ["layer", "outcome"],
    buckets=(1, 5, 10, 20, 30, 60, 90, 120),
    registry=REGISTRY,
)

# Must read 1.0. Any dip is an incident, and it pages -- this is the one SLI
# with a zero error budget.
PIR_CITATION_COV = Gauge(
    "ip_pir_citation_coverage",
    "Fraction of published PIR claims carrying a valid, supporting citation.",
    registry=REGISTRY,
)

# The honest quality signal for the whole AI feature: how much a human had to
# rewrite. Almost nobody measures this, which is exactly why it is here.
PIR_EDIT_RATIO = Histogram(
    "ip_pir_human_edit_ratio",
    "Normalized edit distance between the posted draft and the approved final.",
    ["prompt_version"],
    buckets=(0.0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0),
    registry=REGISTRY,
)

# --- cost governance ---------------------------------------------------------
LLM_COST = Counter(
    "ip_llm_cost_usd_total",
    "Cumulative model spend, by role and resolved provider/model.",
    ["role", "provider", "model"],
    registry=REGISTRY,
)

BUDGET_TRIPS = Counter(
    "ip_budget_trips_total",
    "Budget breaker trips, by scope (incident, day, month).",
    ["scope"],
    registry=REGISTRY,
)

# --- the SLIs nobody else has ------------------------------------------------
# The bot can be 100% available and still have silently dropped three messages,
# after which the PIR is wrong and nobody notices. Availability of a
# data-collection system is measured in data, not in HTTP 200s.
TRANSCRIPT_RATIO = Gauge(
    "ip_transcript_completeness",
    "Messages stored divided by messages in the channel (nightly reconcile).",
    registry=REGISTRY,
)

DEGRADATION_LEVEL = Gauge(
    "ip_degradation_level",
    "Current degradation level: 0 normal, 1 degraded, 2 brownout, 3 lifeboat.",
    registry=REGISTRY,
)

SLACK_429 = Counter(
    "ip_slack_rate_limited_total",
    "Slack 429 responses, by method and priority lane.",
    ["method", "priority"],
    registry=REGISTRY,
)

# --- supporting counters -----------------------------------------------------
DUPLICATE_MESSAGES = Counter(
    "ip_duplicate_messages_total",
    "Slack events redelivered and rejected by the (channel_id, ts) constraint.",
    registry=REGISTRY,
)

WEBHOOK_REJECTED = Counter(
    "ip_webhook_rejected_total",
    "Webhook requests rejected at the trust boundary, by source and reason.",
    ["source", "reason"],
    registry=REGISTRY,
)

ALERTS_ACCEPTED = Counter(
    "ip_alerts_accepted_total",
    "Alerts durably accepted onto a stream, by source and status.",
    ["source", "status"],
    registry=REGISTRY,
)

BROWNOUT_BUFFERED = Counter(
    "ip_brownout_buffered_total",
    "Alerts written to the write-ahead stream while the database was unreachable.",
    registry=REGISTRY,
)


OUTBOX_DEAD = Counter(
    "ip_outbox_dead_total",
    "Outbox rows that exhausted their retries, by action.",
    ["action"],
    registry=REGISTRY,
)

OUTBOX_DISPATCHED = Counter(
    "ip_outbox_dispatched_total",
    "External side effects performed by the relay, by action.",
    ["action"],
    registry=REGISTRY,
)


# --- transcript (W4, B-01) ---------------------------------------------------
# The evidence that G4 actually held in production rather than only in CI. Rows
# stored is the numerator of ip_transcript_completeness; without it a dip in the
# ratio cannot be attributed to lost messages versus a miscounted denominator.
MESSAGES_STORED = Counter(
    "ip_messages_stored_total",
    "Transcript rows written as events arrived, by kind (message, revision).",
    ["kind"],
    registry=REGISTRY,
)

# Must stay at zero for every label except reconciler. This is INV-02 expressed
# as a time series: the unit test proves it for the code that exists today, this
# proves it for the code that is running right now.
HISTORY_CALLS = Counter(
    "ip_slack_history_calls_total",
    "Calls to conversations.history / conversations.replies, by caller.",
    ["caller"],
    registry=REGISTRY,
)


# --- the PIR path (W6) -------------------------------------------------------
# Attempts by outcome, so "we fell back to the secondary provider" is visible in
# a graph rather than inferred from a latency bump. Deliberately NOT labelled by
# model: the model is config, and a metric labelled by it would break every
# dashboard the day E-1 is settled.
LLM_CALLS = Counter(
    "ip_llm_calls_total",
    "Model calls, by role, provider and outcome.",
    ["role", "provider", "outcome"],
    registry=REGISTRY,
)

# The number that says whether the grounding gate is doing anything. Zero means
# either perfect drafts or a validator that never runs, and those look identical
# from the outside -- which is why the layer label is here.
VALIDATION_FAILURES = Counter(
    "ip_pir_validation_failures_total",
    "Draft rejections by the deterministic citation validator, by layer and reason.",
    ["layer", "reason"],
    registry=REGISTRY,
)

# D6. Counted by class, never by value: a metric carrying the redacted values
# would be a second copy of the data with none of the controls.
PII_REDACTED = Counter(
    "ip_pii_redacted_total",
    "Values replaced with stable tokens before egress, by class.",
    ["pii_class"],
    registry=REGISTRY,
)


def set_degradation_level(level: int) -> None:
    """Single writer for the degradation gauge, so the value cannot drift."""
    DEGRADATION_LEVEL.set(level)


def mark_transcript_ratio_unmeasured() -> None:
    """Publish NaN until the reconciler has actually measured something.

    A prometheus_client Gauge reads 0 from the moment it is defined, so a fresh
    pod would expose ``ip_transcript_completeness 0`` -- indistinguishable, to a
    dashboard or an alert rule, from having lost the entire transcript. Since
    this SLI has the tightest target in docs/SLO.md, that would page someone
    within a minute of every deploy, and people who get paged by deploys stop
    reading the alert.

    NaN is the Prometheus idiom for "no data": comparisons against it are false,
    so no rule fires, and a graph shows a gap rather than a cliff. The first
    reconcile pass replaces it with a real number.
    """
    TRANSCRIPT_RATIO.set(float("nan"))
