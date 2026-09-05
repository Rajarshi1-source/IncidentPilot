# IncidentPilot — Complete Schema and Analytics Queries

Companion reference for `incidentpilot-timescale-data`. Target: PostgreSQL 18.6, TimescaleDB 2.29.2,
pgvector 0.8+.

## Contents

1. Extensions, enums, and conventions
2. Reference tables — `services`, `runbooks`, `responders`
3. Incident core — `incidents`, `alerts`, `incident_transitions`
4. Transcript — `slack_messages`, `slack_message_revisions`
5. Outbox and coordination — `outbox_events`, `correlation_feedback`
6. PIR and actions — `pir_documents`, `action_items`, `runbook_step_signals`
7. Hypertables — `timeline_events`, `signal_samples`, `llm_calls`, `page_events`
8. Continuous aggregates
9. Policies (compression, retention, refresh)
10. The 12 analytics queries
11. Index inventory and rationale
12. Migration order and CI checks

---

## 1. Extensions, enums, conventions

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE incident_state AS ENUM (
  'detected','triaging','engaged','acknowledged','mitigating','mitigated',
  'resolved','reopened','pir_drafting','pir_drafted','pir_failed',
  'closed','merged','false_positive','abandoned');

CREATE TYPE severity_level AS ENUM ('sev1','sev2','sev3','sev4');
CREATE TYPE outbox_status  AS ENUM ('pending','claimed','dispatched','failed','dead');
```

Conventions applied everywhere:

- **Every timestamp is `TIMESTAMPTZ`**, stored UTC. No exceptions, including in hypertables.
- **`BIGSERIAL` surrogate keys**, plus a human-readable `public_key` on `incidents`.
- **Unique constraints carry correctness**, not just hygiene — they are the dedup and idempotency
  mechanism.
- **JSONB for raw payloads only.** Anything queried gets a column.

---

## 2. Reference tables

```sql
CREATE TABLE services (
  id                 SERIAL PRIMARY KEY,
  name               TEXT UNIQUE NOT NULL,
  team               TEXT NOT NULL,
  tier               SMALLINT NOT NULL DEFAULT 2,        -- 1 = user-facing critical
  slo_target         NUMERIC(6,4),                       -- 0.9990
  slo_window_days    SMALLINT NOT NULL DEFAULT 30,
  paging_service_ref TEXT,                               -- opaque; adapter resolves
  depends_on         INT[] NOT NULL DEFAULT '{}',        -- correlation graph edges
  graph_depth        SMALLINT,                           -- cached topological depth
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE runbooks (
  id            SERIAL PRIMARY KEY,
  name          TEXT UNIQUE NOT NULL,
  alert_pattern TEXT NOT NULL,                -- regex over alertname
  severity_filter severity_level,
  service_filter TEXT,
  body          TEXT NOT NULL,                -- markdown with step: comments
  step_ids      TEXT[] NOT NULL DEFAULT '{}', -- parsed from body at load
  version       TEXT NOT NULL DEFAULT '1.0.0',
  git_sha       TEXT,                         -- provenance for the auto-PR loop
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_runbook_active ON runbooks (is_active) WHERE is_active;

CREATE TABLE responders (
  id             SERIAL PRIMARY KEY,
  slack_user_id  TEXT UNIQUE NOT NULL,
  display_name   TEXT NOT NULL,
  paging_user_ref TEXT,
  team           TEXT,
  timezone       TEXT NOT NULL DEFAULT 'UTC',   -- fatigue scoring needs local hours
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`graph_depth` is cached rather than computed per alert: correlation runs on the hot path and a
recursive CTE per alert during a storm is exactly the wrong place to spend milliseconds. Recompute
it whenever `depends_on` changes.

---

## 3. Incident core

```sql
CREATE TABLE incidents (
  id                 BIGSERIAL PRIMARY KEY,
  public_key         TEXT UNIQUE NOT NULL,
  dedup_key          TEXT NOT NULL,
  dedup_epoch        INT  NOT NULL DEFAULT 0,
  parent_incident_id BIGINT REFERENCES incidents(id),

  title              TEXT NOT NULL,
  severity           severity_level NOT NULL,
  severity_reason    TEXT,
  state              incident_state NOT NULL DEFAULT 'detected',
  state_seq          INT NOT NULL DEFAULT 0,

  primary_service_id INT REFERENCES services(id),
  affected_services  INT[] NOT NULL DEFAULT '{}',
  root_signal        TEXT,
  correlated_alert_count INT NOT NULL DEFAULT 1,

  detected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  engaged_at         TIMESTAMPTZ,
  acknowledged_at    TIMESTAMPTZ,
  mitigated_at       TIMESTAMPTZ,
  resolved_at        TIMESTAMPTZ,
  closed_at          TIMESTAMPTZ,

  chat_channel_id    TEXT,
  chat_channel_name  TEXT,
  runbook_id         INT REFERENCES runbooks(id),

  impact             JSONB NOT NULL DEFAULT '{}',   -- computed, never model-generated
  error_budget_burn  NUMERIC(8,5),
  embedding          vector(1536),

  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_incident_dedup UNIQUE (dedup_key, dedup_epoch),
  CONSTRAINT ck_ack_after_detect     CHECK (acknowledged_at IS NULL OR acknowledged_at >= detected_at),
  CONSTRAINT ck_mitigate_after_ack   CHECK (mitigated_at    IS NULL OR mitigated_at    >= detected_at),
  CONSTRAINT ck_resolve_after_detect CHECK (resolved_at     IS NULL OR resolved_at     >= detected_at),
  CONSTRAINT ck_merged_has_parent    CHECK (state <> 'merged' OR parent_incident_id IS NOT NULL)
);

ALTER TABLE incidents
  ADD COLUMN tta_seconds  INT GENERATED ALWAYS AS
    (EXTRACT(EPOCH FROM (acknowledged_at - detected_at))::INT) STORED,
  ADD COLUMN ttm_seconds  INT GENERATED ALWAYS AS
    (EXTRACT(EPOCH FROM (mitigated_at   - detected_at))::INT) STORED,
  ADD COLUMN mttr_seconds INT GENERATED ALWAYS AS
    (EXTRACT(EPOCH FROM (resolved_at    - detected_at))::INT) STORED;

CREATE TABLE alerts (
  id             BIGSERIAL PRIMARY KEY,
  incident_id    BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  source         TEXT NOT NULL,
  fingerprint    TEXT NOT NULL,
  alertname      TEXT NOT NULL,
  service        TEXT,
  labels         JSONB NOT NULL,
  annotations    JSONB NOT NULL DEFAULT '{}',
  starts_at      TIMESTAMPTZ NOT NULL,
  ends_at        TIMESTAMPTZ,
  is_root_signal BOOLEAN NOT NULL DEFAULT FALSE,
  merge_score    NUMERIC(4,3),                 -- NULL for the incident-creating alert
  merge_reasons  JSONB NOT NULL DEFAULT '[]',  -- explainability, surfaced in Slack
  received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw            JSONB NOT NULL,
  UNIQUE (fingerprint, starts_at)
);
CREATE INDEX idx_alerts_incident ON alerts (incident_id, starts_at);

CREATE TABLE incident_transitions (
  id          BIGSERIAL PRIMARY KEY,
  incident_id BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  seq         INT NOT NULL,
  from_state  incident_state NOT NULL,
  to_state    incident_state NOT NULL,
  actor       TEXT NOT NULL,              -- 'system' or a slack_user_id
  reason      TEXT,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (incident_id, seq)               -- two workers cannot both write seq = N
);
```

---

## 4. Transcript

```sql
CREATE TABLE slack_messages (
  id            BIGSERIAL PRIMARY KEY,
  incident_id   BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  channel_id    TEXT NOT NULL,
  ts            TEXT NOT NULL,             -- Slack's id: the citation anchor
  thread_ts     TEXT,
  user_id       TEXT,
  text          TEXT NOT NULL,
  redacted_text TEXT,                      -- what actually goes to a model
  raw           JSONB NOT NULL,
  received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel_id, ts)                  -- at-least-once delivery, exactly-once storage
);
CREATE INDEX idx_msg_incident ON slack_messages (incident_id, ts);
CREATE INDEX idx_msg_fts ON slack_messages USING gin (to_tsvector('english', text));

CREATE TABLE slack_message_revisions (
  id          BIGSERIAL PRIMARY KEY,
  message_id  BIGINT NOT NULL REFERENCES slack_messages(id) ON DELETE CASCADE,
  revision    INT NOT NULL,
  kind        TEXT NOT NULL,               -- 'edited' | 'deleted'
  text        TEXT,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (message_id, revision)
);
```

Revisions are appended, never applied in place. A PIR cites `msg:{ts}`; if that message could be
silently rewritten, the citation would stop being evidence.

---

## 5. Outbox and coordination

```sql
CREATE TABLE outbox_events (
  id              BIGSERIAL PRIMARY KEY,
  incident_id     BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  action          TEXT NOT NULL,
  payload         JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,
  status          outbox_status NOT NULL DEFAULT 'pending',
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  dispatched_at   TIMESTAMPTZ,
  result          JSONB
);
CREATE INDEX idx_outbox_claim ON outbox_events (next_attempt_at) WHERE status = 'pending';
CREATE INDEX idx_outbox_dead  ON outbox_events (created_at DESC)  WHERE status = 'dead';

CREATE TABLE correlation_feedback (
  id            BIGSERIAL PRIMARY KEY,
  incident_id   BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  alert_id      BIGINT REFERENCES alerts(id) ON DELETE SET NULL,
  action        TEXT NOT NULL,             -- 'split' | 'merge'
  original_score NUMERIC(4,3),
  original_reasons JSONB NOT NULL DEFAULT '[]',
  actor         TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`correlation_feedback` is the labelled data that tunes `merge_threshold` and feeds the eval corpus.
A correlation engine without a correction path drifts silently.

---

## 6. PIR and actions

```sql
CREATE TABLE pir_documents (
  id                BIGSERIAL PRIMARY KEY,
  incident_id       BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  revision          INT NOT NULL DEFAULT 1,
  generation_layer  TEXT NOT NULL,          -- llm_primary | llm_secondary | skeleton
  body              JSONB NOT NULL,         -- PIRDraft with claims + citations
  rendered_markdown TEXT,

  provider          TEXT,
  model             TEXT,
  prompt_version    TEXT,
  prompt_sha256     TEXT,
  input_tokens      INT,
  output_tokens     INT,
  cost_usd          NUMERIC(10,6),
  generation_ms     INT,

  citation_coverage NUMERIC(4,3),           -- must be 1.000 to publish
  validation_passed BOOLEAN NOT NULL,
  validation_errors JSONB NOT NULL DEFAULT '[]',
  human_edit_ratio  NUMERIC(4,3),
  reviewed_by       TEXT,
  reviewed_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (incident_id, revision)
);

CREATE TABLE action_items (
  id             BIGSERIAL PRIMARY KEY,
  incident_id    BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  pir_id         BIGINT REFERENCES pir_documents(id) ON DELETE SET NULL,
  description    TEXT NOT NULL,
  citations      JSONB NOT NULL DEFAULT '[]',   -- which message proposed it
  owner          TEXT,
  priority       TEXT NOT NULL,                 -- P0 | P1 | P2
  category       TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'open',
  external_ref   TEXT,
  due_at         TIMESTAMPTZ,
  completed_at   TIMESTAMPTZ,
  reopened_count INT NOT NULL DEFAULT 0,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ai_open ON action_items (priority, created_at) WHERE status = 'open';

CREATE TABLE runbook_step_signals (
  id          BIGSERIAL PRIMARY KEY,
  incident_id BIGINT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  runbook_id  INT NOT NULL REFERENCES runbooks(id),
  step_id     TEXT NOT NULL,
  detected_by TEXT NOT NULL,        -- 'command_match' | 'reaction' | 'slash_command'
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_message_ts TEXT,
  UNIQUE (incident_id, step_id)
);
```

---

## 7. Hypertables

```sql
CREATE TABLE timeline_events (
  time              TIMESTAMPTZ NOT NULL,
  incident_id       BIGINT NOT NULL,
  intent            TEXT NOT NULL,
  confidence        NUMERIC(3,2),
  description       TEXT NOT NULL,
  author_user_id    TEXT,
  source_message_ts TEXT,
  metadata          JSONB NOT NULL DEFAULT '{}'
);
SELECT create_hypertable('timeline_events','time', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON timeline_events (incident_id, time DESC);
CREATE INDEX ON timeline_events (intent, time DESC);

CREATE TABLE signal_samples (
  time        TIMESTAMPTZ NOT NULL,
  incident_id BIGINT NOT NULL,
  series      TEXT NOT NULL,
  service     TEXT NOT NULL,
  value       DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('signal_samples','time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ON signal_samples (incident_id, series, time DESC);

CREATE TABLE llm_calls (
  time           TIMESTAMPTZ NOT NULL,
  incident_id    BIGINT,
  role           TEXT NOT NULL,
  provider       TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_version TEXT,
  input_tokens   INT,
  output_tokens  INT,
  cost_usd       NUMERIC(10,6),
  latency_ms     INT,
  outcome        TEXT NOT NULL
);
SELECT create_hypertable('llm_calls','time', chunk_time_interval => INTERVAL '7 days');

CREATE TABLE page_events (
  time        TIMESTAMPTZ NOT NULL,
  responder   TEXT NOT NULL,
  incident_id BIGINT,
  severity    severity_level,
  tz          TEXT NOT NULL DEFAULT 'UTC',
  accepted    BOOLEAN
);
SELECT create_hypertable('page_events','time', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON page_events (responder, time DESC);
```

---

## 8. Continuous aggregates

```sql
CREATE MATERIALIZED VIEW incident_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', time) AS day,
       intent,
       count(*)                    AS events,
       count(DISTINCT incident_id) AS incidents
FROM timeline_events
GROUP BY day, intent;

CREATE MATERIALIZED VIEW llm_cost_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', time) AS day, role, model, prompt_version,
       sum(cost_usd)  AS cost,
       count(*)       AS calls,
       avg(latency_ms) AS avg_ms
FROM llm_calls
GROUP BY day, role, model, prompt_version;

CREATE MATERIALIZED VIEW page_load_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', time) AS day, responder,
       count(*) AS pages,
       count(*) FILTER (WHERE severity = 'sev1') AS sev1_pages
FROM page_events
GROUP BY day, responder;
```

Continuous aggregates union materialized buckets with fresh raw data by default (real-time
aggregation). That is what you want on a dashboard, but it explains why a cagg number can differ
slightly from a plain aggregate run a second earlier.

---

## 9. Policies

```sql
-- Compression: segment by the LOW-cardinality column, order by time
ALTER TABLE timeline_events SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'intent',
  timescaledb.compress_orderby   = 'incident_id, time DESC');
SELECT add_compression_policy('timeline_events', INTERVAL '14 days');

ALTER TABLE signal_samples SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'series, service',
  timescaledb.compress_orderby   = 'time DESC');
SELECT add_compression_policy('signal_samples', INTERVAL '3 days');

ALTER TABLE llm_calls SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'role, model',
  timescaledb.compress_orderby   = 'time DESC');
SELECT add_compression_policy('llm_calls', INTERVAL '30 days');

-- Retention
SELECT add_retention_policy('signal_samples',  INTERVAL '90 days');
SELECT add_retention_policy('timeline_events', INTERVAL '400 days');
SELECT add_retention_policy('page_events',     INTERVAL '400 days');
-- incidents, pir_documents, action_items: NO retention. They are the organizational memory.

-- Refresh
SELECT add_continuous_aggregate_policy('incident_daily',
  start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour');
SELECT add_continuous_aggregate_policy('llm_cost_daily',
  start_offset => INTERVAL '90 days', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour');
```

**Common mistake:** setting `compress_segmentby = 'incident_id'` on `timeline_events`. It is the
highest-cardinality column, so every compressed batch holds a handful of rows and the result can be
larger than uncompressed after per-batch overhead. Segment on `intent`, order on `incident_id`.

---

## 10. The 12 analytics queries

```sql
-- Q1. Active incidents (dashboard banner)
SELECT i.public_key, i.title, i.severity, i.state, s.name AS service,
       EXTRACT(EPOCH FROM (now() - i.detected_at))/60 AS elapsed_min,
       i.correlated_alert_count
FROM incidents i LEFT JOIN services s ON s.id = i.primary_service_id
WHERE i.state NOT IN ('closed','merged','false_positive','abandoned')
ORDER BY i.severity, i.detected_at;

-- Q2. MTTA / TTM / MTTR per service, 30 days
SELECT s.name,
       count(*) AS incidents,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY i.tta_seconds)  AS p50_tta_s,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY i.ttm_seconds)/60.0  AS p50_ttm_min,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY i.mttr_seconds)/60.0 AS p50_mttr_min,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY i.mttr_seconds)/60.0 AS p95_mttr_min
FROM incidents i JOIN services s ON s.id = i.primary_service_id
WHERE i.resolved_at > now() - INTERVAL '30 days'
GROUP BY s.name ORDER BY p50_mttr_min DESC;

-- Q3. Storm compression ratio (the D3 proof)
SELECT date_trunc('week', detected_at) AS wk,
       count(*) FILTER (WHERE parent_incident_id IS NULL) AS incidents,
       sum(correlated_alert_count)                        AS alerts,
       round(sum(correlated_alert_count)::numeric
             / nullif(count(*) FILTER (WHERE parent_incident_id IS NULL),0), 1) AS ratio
FROM incidents WHERE detected_at > now() - INTERVAL '90 days'
GROUP BY wk ORDER BY wk;

-- Q4. Runbook efficacy (the D4 input)
SELECT r.name, count(*) AS uses,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY i.ttm_seconds)/60.0 AS median_ttm_min,
       avg(sig.followed::float / nullif(array_length(r.step_ids,1),0))  AS adherence
FROM incidents i
JOIN runbooks r ON r.id = i.runbook_id
LEFT JOIN LATERAL (
  SELECT count(*) AS followed FROM runbook_step_signals rs
  WHERE rs.incident_id = i.id AND rs.runbook_id = r.id
) sig ON TRUE
WHERE i.mitigated_at IS NOT NULL
GROUP BY r.name HAVING count(*) >= 3
ORDER BY median_ttm_min DESC;

-- Q5. Dead steps: skipped in more than 80% of uses
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

-- Q6. Repeat incidents via pgvector
SELECT i2.public_key, i2.title, i2.detected_at,
       1 - (i1.embedding <=> i2.embedding) AS similarity
FROM incidents i1, incidents i2
WHERE i1.id = $1 AND i2.id <> i1.id AND i2.embedding IS NOT NULL
  AND 1 - (i1.embedding <=> i2.embedding) > 0.85
ORDER BY i1.embedding <=> i2.embedding LIMIT 5;

-- Q7. Responder fatigue window (the D5 input)
SELECT p.responder, count(*) AS pages_8h,
       count(*) FILTER (
         WHERE extract(hour FROM p.time AT TIME ZONE p.tz) NOT BETWEEN 8 AND 22) AS night_pages
FROM page_events p WHERE p.time > now() - INTERVAL '8 hours'
GROUP BY p.responder ORDER BY pages_8h DESC;

-- Q8. Human incident-time per service (toil budget)
SELECT s.name, round(sum(i.mttr_seconds)/3600.0, 1) AS human_hours_7d
FROM incidents i JOIN services s ON s.id = i.primary_service_id
WHERE i.resolved_at > now() - INTERVAL '7 days'
GROUP BY s.name ORDER BY human_hours_7d DESC;

-- Q9. PIR grounding compliance (zero-error-budget SLI)
SELECT date_trunc('day', created_at) AS d,
       count(*) AS pirs,
       count(*) FILTER (WHERE citation_coverage = 1.000) AS fully_grounded,
       count(*) FILTER (WHERE generation_layer = 'skeleton') AS skeletons,
       round(avg(human_edit_ratio), 3) AS avg_edit_ratio
FROM pir_documents GROUP BY d ORDER BY d DESC LIMIT 30;

-- Q10. Action-item half-life by priority
SELECT priority,
       count(*) FILTER (WHERE status = 'open') AS open_now,
       percentile_cont(0.5) WITHIN GROUP (
         ORDER BY EXTRACT(EPOCH FROM (completed_at - created_at))/86400
       ) FILTER (WHERE completed_at IS NOT NULL) AS median_close_days,
       max(EXTRACT(EPOCH FROM (now() - created_at))/86400)
         FILTER (WHERE status = 'open') AS oldest_open_days
FROM action_items GROUP BY priority ORDER BY priority;

-- Q11. Cost per PIR by prompt version (MLOps panel)
SELECT prompt_version, count(*) AS pirs,
       round(avg(cost_usd), 4) AS avg_cost,
       round(avg(generation_ms)) AS avg_ms,
       round(avg(human_edit_ratio), 3) AS avg_edit_ratio
FROM pir_documents WHERE created_at > now() - INTERVAL '60 days'
GROUP BY prompt_version ORDER BY prompt_version DESC;

-- Q12. Transcript completeness by active incident (the key SLI)
SELECT i.public_key, count(m.id) AS stored, i.chat_channel_id
FROM incidents i LEFT JOIN slack_messages m ON m.incident_id = i.id
WHERE i.state NOT IN ('closed','merged','false_positive','abandoned')
GROUP BY i.id, i.public_key, i.chat_channel_id;
```

---

## 11. Index inventory

| Index | Table | Why |
|---|---|---|
| `uq_incident_dedup` | incidents | Correctness: prevents duplicate incidents |
| `idx_inc_open` (partial) | incidents | The hot dashboard query, "what is open" |
| `idx_inc_embed` (HNSW) | incidents | Repeat detection |
| `idx_alerts_incident` | alerts | Alert list on the incident page |
| `UNIQUE (channel_id, ts)` | slack_messages | Correctness: exactly-once storage |
| `idx_msg_fts` (GIN) | slack_messages | Search across transcripts |
| `UNIQUE (incident_id, seq)` | incident_transitions | Correctness: concurrency guard |
| `idx_outbox_claim` (partial) | outbox_events | Relay claim query, only pending rows |
| `idx_ai_open` (partial) | action_items | Aging dashboard |
| `(incident_id, time DESC)` | timeline_events | Per-incident timeline read |
| `(incident_id, series, time DESC)` | signal_samples | Impact window read |

Partial indexes matter here because the working set is tiny relative to history: a handful of open
incidents against months of closed ones.

---

## 12. Migration order and CI checks

Order within the initial migration:

1. Extensions
2. Enums
3. Reference tables
4. `incidents` (self-referencing FK added after creation)
5. Dependent relational tables
6. Plain hypertable tables, then `create_hypertable`
7. Compression settings, then policies
8. Continuous aggregates, then refresh policies
9. Indexes

Two CI checks worth automating:

```sql
-- No naked TIMESTAMP anywhere
SELECT table_name, column_name FROM information_schema.columns
WHERE table_schema='public' AND data_type='timestamp without time zone';
-- must return zero rows

-- Every hypertable has both a compression and a retention policy
SELECT h.hypertable_name FROM timescaledb_information.hypertables h
WHERE NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs j
                  WHERE j.hypertable_name = h.hypertable_name
                    AND j.proc_name = 'policy_compression');
```

The first one has caught real bugs; the second stops a hypertable quietly growing forever because
someone forgot a policy in a later migration.
