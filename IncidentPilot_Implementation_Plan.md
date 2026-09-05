# IncidentPilot — Incident Management Automation Bot

## Complete Implementation Plan for Junior SRE Engineer Interview (2026)

---

## Table of Contents

1. Project Overview & Interview Hook
2. Tech Stack (with justification for every choice)
3. MVP Blueprint (7-week plan)
4. Detailed System Architecture (HLD)
5. Detailed System Design (LLD)
6. Database Design & Choice (with comparison matrix)
7. Caching & Messaging — Redis vs Kafka vs RabbitMQ (verdict)
8. Design Patterns Used
9. Resilience Patterns & Mitigation Strategies
10. DevOps / MLOps Wrapper (GPT-4 governance)
11. Docker & Kubernetes Deployment Strategy
12. CI/CD Pipeline
13. Monitoring & Observability
14. README Blueprint with Architecture Diagram
15. Interview Prep — Questions & Answers (SRE-focused)
16. Week-by-Week Build Schedule

---

## 1. Project Overview & Interview Hook

**Project Name:** IncidentPilot

**Tagline:** "From alert to action in 45 seconds. From incident to PIR in 15 minutes."

**One-liner:** A production-grade incident management automation platform where Prometheus and PagerDuty alerts trigger a Slack bot that instantly creates a dedicated incident war-room channel, auto-assigns the on-call engineer, posts a pre-filled runbook tailored to the specific alert type, starts an incident timer, captures the entire conversation timeline from the Slack thread, integrates Grafana metrics screenshots into the channel automatically, and after resolution uses GPT-4 to generate a complete Post-Incident Review document with structured timeline, impact analysis, contributing factors, and action items extracted from the thread — reducing time-to-acknowledge from 8 minutes to 45 seconds and PIR writing time from 2 hours to 15 minutes of review.

**Interview Hook (memorize this):**

> "I built an incident management bot that solves the universal SRE pain: incident response is chaotic and PIR writing is procrastinated. When Prometheus or PagerDuty fires an alert, my Python bot instantly creates a dedicated Slack channel like '#inc-2026-04-30-auth-svc-down', assigns the on-call engineer pulled from PagerDuty rotation, posts a runbook specific to that alert, and starts a timer. During the incident, the bot watches the Slack thread for keywords like 'rolling back' or 'deploying fix' and auto-tags timeline events. When someone marks the incident resolved, GPT-4 reads the entire thread plus the Grafana metrics during the incident window, and drafts a complete PIR with timeline, impact, root cause hypothesis, and action items — engineers review in 15 minutes instead of writing for 2 hours. I used PostgreSQL for incident records because they're deeply relational, TimescaleDB only for the timeline events because they're high-frequency, and Redis Streams over Kafka for the alert ingestion queue because event volume is under 200 per day. The MLOps wrapper around GPT-4 includes prompt versioning, output validation, and human-in-the-loop fallback when GPT-4 returns low-confidence drafts."

**Why SRE interviewers love this:**
- **Universal SRE pain** — every ops team at every company has this exact problem
- **Quantified impact** — "8 min → 45s" and "2 hours → 15 min" are concrete and memorable
- **Incident lifecycle ownership** — detection → response → resolution → PIR is the full SRE workflow
- **PagerDuty/Slack mastery** — every SRE role uses these tools
- **GPT-4 with governance** — not just plugging in OpenAI; prompt versioning, validation, MLOps thinking
- **Toil reduction** — automating the most-hated SRE task (writing PIRs at midnight) is gold
- **Demo-able** — the war-room channel + auto-PIR is a wow moment

**Target Companies:**
- **Zepto, Blinkit, Dunzo** — quick commerce companies with frequent incidents
- **Razorpay, PhonePe, CRED** — FinTech needs tight incident response (compliance)
- **Swiggy, Flipkart, Meesho** — SRE-heavy cultures
- **Atlassian India** — Opsgenie + Statuspage are their products, this is adjacent
- **PagerDuty India** — literally their domain
- **Hasura, GitLab, Datadog India** — DevTools companies with mature SRE

---

## 2. Tech Stack — Every Choice Justified

### Core Stack

| Layer | Technology | Why This (Interview Answer) |
|---|---|---|
| **Bot Backend** | Python 3.12 + FastAPI | Python is the SRE/DevOps lingua franca. FastAPI provides async webhook handling. `slack-bolt` SDK for Slack integration. `pdpyras` for PagerDuty. `openai` for GPT-4. All major integrations have first-class Python support. |
| **Slack Integration** | Slack Bolt SDK (Python) | Official Slack SDK. Handles OAuth, event subscriptions, interactive components, Block Kit messages. Industry standard. |
| **PagerDuty Integration** | pdpyras (Python REST/Events API client) | Official PagerDuty SDK. Webhook receiver + outbound API for on-call lookup, incident annotation. |
| **AI / LLM** | OpenAI GPT-4 (with structured output / JSON mode) | Best-in-class for natural language understanding (parsing chat threads), reasoning (root cause hypothesis), and summarization. JSON mode ensures machine-readable PIR drafts. |
| **Alert Source** | Prometheus + Alertmanager | The SRE standard. Webhook receiver pattern. Multi-window multi-burn-rate alerts (from BudgetGuard knowledge). |
| **Dashboard** | Next.js 14 + TypeScript | Incident analytics dashboard: MTTR trends, top-firing alerts, on-call load distribution, PIR completion rate. |
| **Charts** | Recharts + Tremor | Time-series MTTR charts, incident frequency heatmaps, severity distribution. |
| **UI** | Tailwind CSS + shadcn/ui | Dark-mode SRE dashboard. shadcn for polished components. |
| **Database** | PostgreSQL 16 (primary) + TimescaleDB extension (for timeline) | See Section 6. Hybrid approach: relational data in PostgreSQL tables, high-frequency timeline events in a TimescaleDB hypertable. Best of both worlds in one database. |
| **Cache & Queue** | Redis 7 | See Section 7. Channel state cache, Slack rate limit tracking, alert deduplication, alert ingestion queue (Streams). |
| **Grafana Integration** | Grafana HTTP API + render API | Auto-fetch dashboard screenshots during incidents. Embed in Slack channel for context. |
| **Container Orchestration** | Kubernetes + Helm | Bot runs on K8s. Helm chart for full platform deployment. |
| **CI/CD** | GitHub Actions | Lint → test → build → deploy. PR review for prompt template changes. |
| **Monitoring** | Prometheus + Grafana (self-monitoring) | The bot itself is critical infrastructure — must be highly available. Track: bot response time, GPT-4 latency, PIR generation success rate. |

### Why NOT These Alternatives

| Rejected Option | Why |
|---|---|
| Slack Workflow Builder | Slack's built-in workflow tool is too limited. Can't integrate with PagerDuty, can't call GPT-4, can't do complex routing logic. We need a real backend. |
| Rootly / Incident.io / FireHydrant | Commercial incident management SaaS. Using them means configuring, not building. The whole point is to demonstrate I can build SRE tooling. |
| Hubot / Errbot | Older chatbot frameworks. Less Slack-native than Slack Bolt SDK. Slack Bolt is the modern standard. |
| Microsoft Teams Bot Framework | Slack dominates SRE/engineering teams in Bangalore. Teams is Microsoft-shop oriented. |
| Local LLMs (Llama, Mistral) | Open-source LLMs are improving but GPT-4 still leads at incident-context summarization which requires nuanced reasoning. Mention in interviews: "I'd add an open-source LLM option for cost/privacy concerns post-MVP." |
| MongoDB | Incident data is deeply relational: incident → channel → on_call_assignment → timeline_events → action_items → pir_document → reviews. JOINs are essential. MongoDB would force denormalization. |
| Neo4j (graph DB) | Tempting because incident relationships look graph-like (incident → caused by deploy → owned by team). But the graph traversal depth is shallow (2-3 hops). PostgreSQL with junction tables handles this with simpler operations. |
| Pure TimescaleDB | Most data is relational and low-frequency. Only timeline events are high-frequency. Using TimescaleDB for everything would force time-series semantics on data that doesn't need them. PostgreSQL with TimescaleDB extension for ONE table is the right balance. |
| Kafka | Under 200 alert events per day. Kafka's 3-broker minimum is absurd overengineering. Redis Streams handles this trivially. |

---

## 3. MVP Blueprint — 7-Week Build Plan

### MVP Scope — The Core Workflow

```
1. ALERT FIRES (Prometheus or PagerDuty)
        ↓
2. BOT CREATES INCIDENT CHANNEL
   - Naming: #inc-YYYY-MM-DD-service-summary
   - Topic: alert details + severity
   - Pin: incident timer + key links
        ↓
3. BOT ASSIGNS ON-CALL
   - Query PagerDuty for current on-call
   - @mention them in channel
   - Send DM with incident link
        ↓
4. BOT POSTS RUNBOOK
   - Look up runbook for alert type
   - Render runbook with alert context
   - Post as pinned message
        ↓
5. BOT STARTS TIMER
   - Pin "Started: HH:MM, Elapsed: X minutes"
   - Updates every 5 minutes
        ↓
6. INCIDENT IN PROGRESS (humans + bot)
   - Bot watches thread for command words
   - Timeline events auto-tagged: "rolling back", "deploying fix", "checking dashboard"
   - Bot fetches Grafana screenshots on /metrics command
        ↓
7. RESOLUTION
   - Engineer types /resolve in channel
   - Bot stops timer, captures total duration
        ↓
8. AUTO-PIR GENERATION (GPT-4)
   - Bot pulls full thread transcript
   - Bot pulls Grafana metrics for incident window
   - Bot calls GPT-4 with structured prompt
   - Bot generates PIR document with sections:
     • Executive summary
     • Timeline
     • Impact (users affected, duration, severity)
     • Root cause hypothesis
     • Contributing factors
     • Action items (extracted from thread)
        ↓
9. PIR REVIEW
   - PIR posted as channel message
   - Engineers review/edit in 15 min
   - Save to PostgreSQL
   - Export to Confluence/Notion (optional)
        ↓
10. POST-INCIDENT
    - Channel archived (with TTL)
    - Action items tracked in dashboard
    - Incident contributes to MTTR metrics
```

### What to Build (MVP)

**Must Have:**
- Webhook receivers: Prometheus Alertmanager + PagerDuty
- Slack channel auto-creation with proper naming + topic
- On-call lookup from PagerDuty schedule
- Runbook library: at least 6 runbooks for common alert types
- Incident timer (live-updating message)
- Slack thread monitoring (event subscriptions)
- Timeline event auto-detection (keyword/intent extraction)
- /resolve, /update, /metrics, /escalate Slack slash commands
- GPT-4 PIR generation with prompt versioning
- PIR review workflow (post draft, await edits, save)
- Dashboard: incident list, MTTR trend, on-call load
- Helm chart, docker-compose.yml
- GitHub Actions CI/CD

**Nice to Have (Post-MVP):**
- Confluence/Notion export of PIRs
- Auto-rollback execution (integrate with deployment system)
- War room video call link auto-generation (Zoom/Meet)
- Status page integration (Statuspage.io API)
- Customer impact estimation from request volume during incident
- Action item tracking with Jira integration
- Incident severity auto-classification (using GPT-4)
- Multi-org support (multi-tenant SaaS mode)

### The 6 Initial Runbook Types

Each runbook is a Markdown template with `{placeholders}` filled by the bot:

| Alert Type | Runbook Sections |
|---|---|
| **Service Down** | Symptoms, Verify (curl health), Likely causes, Quick checks (recent deploys), Escalation |
| **High Error Rate** | Symptoms, Check dashboard, Recent deploys, Resilience4j circuit breaker state, Rollback procedure |
| **High Latency** | Symptoms, Check downstream dependencies, Database connection pool, Cache hit rate, Bottleneck identification |
| **Pod CrashLoopBackOff** | Symptoms, kubectl logs commands, Common causes (OOM, config error, missing dependency), Rollback procedure |
| **Disk Full** | Symptoms, Check disk usage commands, Common culprits (logs, tmp files), Cleanup procedure |
| **Database Connection Issues** | Symptoms, Connection pool stats, Failover procedure, Read replica promotion, Escalation to DBA |

### Week-by-Week Schedule

**Week 1 — Slack Bot Foundation**
- Day 1–2: Slack App registration, OAuth flow, install in test workspace
- Day 3–4: Slack Bolt SDK setup, basic event subscription (channel created, message posted)
- Day 5: Webhook receiver: POST /api/webhooks/alertmanager
- Day 6: Channel auto-creation logic (naming convention, topic, members)
- Day 7: Test: fire mock alert → see channel created in Slack
- Deliverable: Bot creates incident channels from Alertmanager webhooks

**Week 2 — On-Call Lookup + Runbook Posting**
- Day 1–2: PagerDuty API integration (pdpyras): list on-call schedules, get current on-call
- Day 3: Auto-assign on-call: invite to channel, @mention, send DM
- Day 4: Runbook library structure (Markdown templates with placeholders)
- Day 5: Runbook lookup by alert name/labels
- Day 6: Render and post runbook as pinned message
- Day 7: Multi-runbook support (alert can match multiple runbooks)
- Deliverable: Channel auto-populated with on-call and runbook

**Week 3 — Timer + Timeline + Slash Commands**
- Day 1–2: Live-updating incident timer (Slack message edit every 5 min)
- Day 3: Slash commands: /resolve, /update, /metrics, /escalate
- Day 4: Timeline event detection: parse messages for keywords ("rolling back", "deploy", "checking")
- Day 5: Auto-tag timeline events with timestamps + author + intent
- Day 6: /metrics command: fetch Grafana screenshot via render API, post to channel
- Day 7: Integration test: end-to-end incident lifecycle
- Deliverable: Full in-channel incident management

**Week 4 — Database + Persistence**
- Day 1–2: PostgreSQL schema: incidents, channels, timeline_events, on_call_assignments
- Day 3: TimescaleDB extension for timeline_events hypertable (high-frequency)
- Day 4: SQLAlchemy models, Alembic migrations
- Day 5: Persist every event: channel created, runbook posted, slash commands, resolution
- Day 6: Redis caching: channel state, on-call lookup, dedup
- Day 7: Audit trail: every bot action logged with context
- Deliverable: Complete persistent record of every incident

**Week 5 — GPT-4 PIR Generation (the killer feature)**
- Day 1–2: OpenAI integration with JSON mode for structured output
- Day 3: PIR prompt template engineering (versioned in code)
- Day 4: Thread transcript extraction (collect all messages, sort by timestamp)
- Day 5: Grafana metrics fetch for incident window (PromQL queries via API)
- Day 6: PIR generation pipeline: thread + metrics → GPT-4 → structured PIR JSON
- Day 7: PIR review flow: post draft to channel, allow edits, save final
- Deliverable: Auto-generated PIRs with thread context

**Week 6 — Dashboard + DORA Metrics**
- Day 1–2: Next.js dashboard scaffold
- Day 3: Incident list view with filters (active, resolved, by service)
- Day 4: Per-incident detail page: timeline, runbook used, PIR document
- Day 5: MTTR trend chart, incident frequency heatmap, top-firing alerts
- Day 6: On-call load distribution chart (incidents per engineer)
- Day 7: PIR completion rate, action item tracking
- Deliverable: Comprehensive incident analytics dashboard

**Week 7 — Polish, MLOps, Deploy**
- Day 1: GPT-4 prompt versioning + A/B testing infrastructure
- Day 2: PIR validation: verify GPT-4 output schema, fall back gracefully
- Day 3: Helm chart for full platform
- Day 4: Docker Compose for local development
- Day 5: GitHub Actions CI/CD
- Day 6: Documentation (README, architecture diagram, demo video)
- Day 7: Deploy publicly, polish demo
- Deliverable: Production-ready platform with full demo

---

## 4. Detailed System Architecture (HLD)

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                  EXTERNAL ALERT SOURCES                                  │
│                                                                         │
│  ┌────────────────┐      ┌─────────────────┐                            │
│  │  Prometheus    │      │  PagerDuty      │                            │
│  │  Alertmanager  │      │                 │                            │
│  │                │      │  • Schedules    │                            │
│  │  • Recording   │      │  • Escalations  │                            │
│  │    rules       │      │  • Incident API │                            │
│  │  • Alert rules │      └────────┬────────┘                            │
│  │  • Webhook     │               │                                     │
│  └────────┬───────┘               │                                     │
│           │ webhook                │ webhook + REST                     │
│           ▼                        ▼                                     │
└───────────┼────────────────────────┼─────────────────────────────────────┘
            │                        │
            ▼                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                  KUBERNETES CLUSTER                                      │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              INCIDENTPILOT BOT BACKEND (FastAPI)                 │   │
│  │                                                                 │   │
│  │  ┌──────────────────────────────────────────────────────────┐    │   │
│  │  │ WEBHOOK LAYER                                            │    │   │
│  │  │                                                          │    │   │
│  │  │  /api/webhooks/alertmanager  → Alert ingestion           │    │   │
│  │  │  /api/webhooks/pagerduty     → PD incident events        │    │   │
│  │  │  /api/webhooks/slack         → Slack events + commands   │    │   │
│  │  │                                                          │    │   │
│  │  │  Signature validation, rate limiting, dedup              │    │   │
│  │  └────────────────────────┬─────────────────────────────────┘    │   │
│  │                           │                                      │   │
│  │                           ▼                                      │   │
│  │  ┌──────────────────────────────────────────────────────────┐    │   │
│  │  │ INCIDENT ORCHESTRATOR (state machine)                    │    │   │
│  │  │                                                          │    │   │
│  │  │  States: detected → channel_created → on_call_assigned   │    │   │
│  │  │           → runbook_posted → in_progress → resolved      │    │   │
│  │  │           → pir_generated → reviewed → closed            │    │   │
│  │  │                                                          │    │   │
│  │  │  Transitions trigger workflow steps                      │    │   │
│  │  └────┬─────────────────┬──────────────────┬─────────────────┘    │   │
│  │       │                 │                  │                     │   │
│  │       ▼                 ▼                  ▼                     │   │
│  │  ┌─────────┐      ┌──────────────┐   ┌──────────────────┐        │   │
│  │  │ Slack   │      │ PagerDuty    │   │ Runbook          │        │   │
│  │  │ Bot     │      │ Client       │   │ Engine           │        │   │
│  │  │         │      │              │   │                  │        │   │
│  │  │ Channel │      │ Get on-call  │   │ Match alert →    │        │   │
│  │  │ create  │      │ Acknowledge  │   │ template lookup  │        │   │
│  │  │ Pin msg │      │ Annotate     │   │ Render with vars │        │   │
│  │  │ Timer   │      │              │   │                  │        │   │
│  │  └─────────┘      └──────────────┘   └──────────────────┘        │   │
│  │                                                                 │   │
│  │  ┌──────────────────────────────────────────────────────────┐    │   │
│  │  │ TIMELINE COLLECTOR                                       │    │   │
│  │  │                                                          │    │   │
│  │  │  Listens to channel messages (Slack events API)          │    │   │
│  │  │  Detects intent: "rolling back", "deploying", "fixed"    │    │   │
│  │  │  Auto-tags timeline events                               │    │   │
│  │  │  Stores in TimescaleDB hypertable                        │    │   │
│  │  └──────────────────────────────────────────────────────────┘    │   │
│  │                                                                 │   │
│  │  ┌──────────────────────────────────────────────────────────┐    │   │
│  │  │ PIR GENERATOR (GPT-4 with MLOps wrapper)                 │    │   │
│  │  │                                                          │    │   │
│  │  │  ┌────────────────┐  ┌──────────────┐  ┌─────────────┐   │    │   │
│  │  │  │ Context        │  │ Prompt       │  │ Output      │   │    │   │
│  │  │  │ Collector      │→ │ Builder      │→ │ Validator   │   │    │   │
│  │  │  │                │  │ (versioned)  │  │             │   │    │   │
│  │  │  │ • Thread msgs  │  │              │  │ • Schema    │   │    │   │
│  │  │  │ • Grafana imgs │  │ Loads        │  │ • Required  │   │    │   │
│  │  │  │ • Timeline     │  │ template     │  │   sections  │   │    │   │
│  │  │  │ • Runbook used │  │ from         │  │ • Length    │   │    │   │
│  │  │  │ • Resolution   │  │ versioned    │  │   limits    │   │    │   │
│  │  │  │   action       │  │ store        │  │             │   │    │   │
│  │  │  └────────────────┘  └──────┬───────┘  └─────────────┘   │    │   │
│  │  │                             │                            │    │   │
│  │  │                             ▼                            │    │   │
│  │  │                    ┌────────────────┐                    │    │   │
│  │  │                    │  GPT-4 API      │                    │    │   │
│  │  │                    │  (JSON mode)    │                    │    │   │
│  │  │                    └────────────────┘                    │    │   │
│  │  └──────────────────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌────────────────┐  ┌──────────────┐  ┌────────────────────────────┐  │
│  │ PostgreSQL 16  │  │  Redis 7     │  │  Next.js Dashboard         │  │
│  │ + TimescaleDB  │  │              │  │                            │  │
│  │                │  │  • Channel   │  │  • Active incidents         │  │
│  │  • incidents   │  │    state     │  │  • Timeline view           │  │
│  │  • channels    │  │  • On-call   │  │  • MTTR trends             │  │
│  │  • runbooks    │  │    cache     │  │  • PIR documents           │  │
│  │  • on_call     │  │  • Alert     │  │  • Action items            │  │
│  │  • PIRs        │  │    dedup     │  │  • DORA metrics            │  │
│  │  • action_items│  │  • Stream    │  │                            │  │
│  │                │  │    queue     │  │                            │  │
│  │  TimescaleDB   │  │  • Slack     │  │                            │  │
│  │  hypertable:   │  │    rate lim  │  │                            │  │
│  │  • timeline_   │  │              │  │                            │  │
│  │    events      │  │              │  │                            │  │
│  └────────────────┘  └──────────────┘  └────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                          │                              │
                          ▼                              ▼
                  ┌──────────────────┐          ┌─────────────────┐
                  │   Slack          │          │   Grafana        │
                  │                  │          │                  │
                  │   Workspace      │          │   • Render API   │
                  │   • Channels     │          │   • PromQL       │
                  │   • Threads      │          │     queries      │
                  │   • Bot          │          │   • Dashboard    │
                  │     interaction  │          │     screenshots  │
                  └──────────────────┘          └─────────────────┘
```

### Core Request Flows

**Flow 1: Alert → Channel → On-Call → Runbook**
```
Step 1:  Prometheus alert KubePodCrashLooping fires for auth-service
    ↓
Step 2:  Alertmanager webhook POST → /api/webhooks/alertmanager
         Payload: { alerts: [{ labels: {alertname, service, severity}, ... }] }
    ↓
Step 3:  WEBHOOK LAYER:
         a) Validate webhook signature (HMAC)
         b) Check Redis dedup: "alert:dedup:{fingerprint}" — skip if duplicate
         c) Parse alert payload
         d) Enqueue to Redis Stream: "incident-events"
    ↓
Step 4:  ORCHESTRATOR consumes from stream:
         a) Determine severity: page (critical) / ticket (warning)
         b) Compute incident name: "inc-2026-04-30-auth-svc-crashloop"
         c) Create incident record in PostgreSQL: status=detected
    ↓
Step 5:  SLACK BOT:
         a) Create channel: #inc-2026-04-30-auth-svc-crashloop
         b) Set topic: "🔴 KubePodCrashLooping on auth-service | Started 14:32"
         c) Pin alert details message
         d) Update incident: status=channel_created
    ↓
Step 6:  PAGERDUTY CLIENT:
         a) Get current on-call from schedule
         b) Acknowledge PagerDuty incident (if PD-triggered)
         c) Get on-call email → Slack user lookup
         d) Update incident: status=on_call_assigned
    ↓
Step 7:  SLACK BOT:
         a) Invite on-call to channel
         b) @mention them: "👋 @alice you're on-call. War room ready."
         c) Send DM with channel link
    ↓
Step 8:  RUNBOOK ENGINE:
         a) Lookup runbook by alertname: "KubePodCrashLooping"
         b) Render template with alert vars (service, namespace, pod)
         c) Format as Slack Block Kit message
    ↓
Step 9:  SLACK BOT:
         a) Post runbook as pinned message
         b) Start incident timer: pinned message updating every 5 min
         c) Update incident: status=runbook_posted
    ↓
Step 10: TIMELINE COLLECTOR:
         a) Subscribe to channel messages
         b) Listen for timeline keywords
         c) Begin recording timeline events to TimescaleDB
```

**Total time from alert fire to runbook posted: < 45 seconds**

**Flow 2: In-Progress Incident — Timeline Collection**
```
While incident is active, the bot listens to the channel:

Engineer: "Checking Grafana dashboard..."
    ↓
TIMELINE COLLECTOR:
  - Detect intent: "investigation" (regex: "checking|looking at|investigating")
  - Store event: { incident_id, ts, author, intent: "investigation", text: "..." }

Engineer: "/metrics auth-service"
    ↓
SLASH COMMAND HANDLER:
  - Fetch Grafana screenshot via render API
  - Post screenshot to channel
  - Store event: { intent: "metrics_fetch", grafana_url: "..." }

Engineer: "Rolling back to v1.2.3"
    ↓
TIMELINE COLLECTOR:
  - Detect intent: "remediation_start" (regex: "rolling back|deploying|reverting")
  - Store event: { intent: "remediation_start", action: "rollback to v1.2.3" }

Engineer: "Pods are healthy now"
    ↓
TIMELINE COLLECTOR:
  - Detect intent: "recovery_signal" (regex: "healthy|recovered|fixed|working")
  - Store event: { intent: "recovery_signal" }

Engineer: "/resolve"
    ↓
SLASH COMMAND HANDLER:
  - Stop incident timer
  - Update incident: status=resolved, resolved_at=now
  - Trigger PIR generation (Flow 3)
```

**Flow 3: PIR Generation (the killer feature)**
```
Step 1:  Engineer types /resolve in incident channel
    ↓
Step 2:  ORCHESTRATOR:
         a) Mark incident resolved
         b) Stop incident timer
         c) Compute total duration
         d) Trigger PIR generation
    ↓
Step 3:  CONTEXT COLLECTOR:
         a) Fetch all messages from channel via Slack API
         b) Fetch all timeline events from PostgreSQL
         c) Fetch runbook used
         d) Fetch Grafana metrics for incident window:
            - HTTP error rate during incident
            - Latency P99 during incident
            - Active alert during incident
         e) Bundle everything into structured context
    ↓
Step 4:  PROMPT BUILDER (versioned):
         a) Load current PIR prompt template (e.g., v1.4.0)
         b) Inject context: thread, timeline, metrics, runbook
         c) Add structured output schema (JSON mode)
    ↓
Step 5:  GPT-4 CALL:
         a) Send to OpenAI with model="gpt-4-turbo", response_format=json
         b) Receive structured PIR JSON:
            {
              "summary": "...",
              "timeline": [{ time, event }, ...],
              "impact": { duration_minutes, users_affected_estimate, severity },
              "root_cause_hypothesis": "...",
              "contributing_factors": ["..."],
              "what_went_well": ["..."],
              "what_went_wrong": ["..."],
              "action_items": [
                { description, owner, priority, jira_ticket: null }
              ]
            }
    ↓
Step 6:  OUTPUT VALIDATOR:
         a) Validate JSON schema (all required fields present)
         b) Check required sections present
         c) Verify action items have owners
         d) Confidence check: if GPT-4 returns "I don't have enough context" → fallback
    ↓
Step 7:  IF VALIDATION PASSES:
         a) Render PIR as Markdown
         b) Post draft to incident channel as threaded message
         c) Update incident: status=pir_generated
         d) Notify on-call: "📝 PIR drafted. Review and edit, then click Approve."
    ↓
Step 8:  IF VALIDATION FAILS:
         a) Log failure with reason
         b) Fall back to template-based PIR (skeleton only)
         c) Notify: "⚠️ Auto-PIR generation failed. Using template. Engineer fills in."
    ↓
Step 9:  PIR REVIEW:
         a) Engineers review draft in Slack
         b) Edit via interactive Block Kit
         c) Click "Approve" button
         d) PIR saved to PostgreSQL with version history
    ↓
Step 10: POST-PIR:
         a) Action items extracted to action_items table
         b) Dashboard updated with new incident metrics
         c) Channel archived after 30 days (configurable)
```

**Total PIR generation time: ~30 seconds. Engineer review time: ~15 minutes.**

### State Machine Diagram

```
    ┌─────────┐
    │detected │ (alert received)
    └────┬────┘
         │
         ▼
    ┌─────────────────┐
    │channel_created  │ (Slack channel ready)
    └────────┬────────┘
             │
             ▼
    ┌─────────────────────┐
    │on_call_assigned     │ (PagerDuty looked up, engineer added)
    └──────────┬──────────┘
               │
               ▼
    ┌──────────────────┐
    │runbook_posted    │ (runbook rendered + pinned)
    └────────┬─────────┘
             │
             ▼
    ┌──────────────┐
    │ in_progress  │ ───────────────────┐
    └──────┬───────┘                    │
           │                             │ (events collected)
           │                             │
           │ /resolve                    │
           ▼                             │
    ┌──────────────┐                     │
    │  resolved    │ ◄───────────────────┘
    └──────┬───────┘
           │
           ▼
    ┌──────────────────┐
    │ pir_generating   │ (GPT-4 working)
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │ pir_generated    │ (draft posted)
    └────────┬─────────┘
             │ (engineer approves)
             ▼
    ┌─────────────┐
    │  reviewed   │
    └──────┬──────┘
           │
           ▼
    ┌──────────┐
    │  closed  │ (30 days later, archived)
    └──────────┘
```

---

## 5. Detailed System Design (LLD)

### 5.1 Project Structure

```
incidentpilot/
├── bot/                                       # Python FastAPI backend
│   ├── src/
│   │   ├── __init__.py
│   │   ├── main.py                            # FastAPI entrypoint
│   │   ├── config.py
│   │   │
│   │   ├── api/
│   │   │   ├── webhooks/
│   │   │   │   ├── alertmanager.py            # POST /api/webhooks/alertmanager
│   │   │   │   ├── pagerduty.py               # POST /api/webhooks/pagerduty
│   │   │   │   └── slack.py                   # POST /api/webhooks/slack
│   │   │   ├── slash_commands/
│   │   │   │   ├── resolve.py                 # /resolve
│   │   │   │   ├── update.py                  # /update
│   │   │   │   ├── metrics.py                 # /metrics
│   │   │   │   └── escalate.py                # /escalate
│   │   │   ├── incidents.py                   # GET /api/incidents/*
│   │   │   ├── pirs.py                        # GET /api/pirs/*
│   │   │   ├── runbooks.py                    # GET /api/runbooks/*
│   │   │   └── health.py
│   │   │
│   │   ├── core/
│   │   │   ├── orchestrator.py                # State machine
│   │   │   ├── deduplicator.py                # Alert dedup via Redis
│   │   │   ├── incident_namer.py              # Channel name generator
│   │   │   ├── timeline_collector.py          # Watch Slack messages
│   │   │   ├── intent_detector.py             # Regex/keyword intent extraction
│   │   │   └── severity_classifier.py
│   │   │
│   │   ├── workflows/
│   │   │   ├── create_incident.py             # End-to-end: alert → channel
│   │   │   ├── assign_on_call.py
│   │   │   ├── post_runbook.py
│   │   │   ├── resolve_incident.py
│   │   │   └── generate_pir.py                # Triggered on resolve
│   │   │
│   │   ├── pir/                                # PIR generation (MLOps-aware)
│   │   │   ├── context_collector.py            # Gather thread + metrics + timeline
│   │   │   ├── prompt_builder.py               # Versioned prompt assembly
│   │   │   ├── prompt_templates/
│   │   │   │   ├── pir_v1_0_0.txt
│   │   │   │   ├── pir_v1_3_0.txt
│   │   │   │   └── pir_v1_4_0.txt              # Current
│   │   │   ├── prompt_versions.yaml            # Version registry
│   │   │   ├── gpt4_client.py                  # OpenAI API wrapper
│   │   │   ├── output_validator.py             # JSON schema validation
│   │   │   ├── output_schema.py                # Pydantic models
│   │   │   ├── fallback_generator.py           # Template-based fallback
│   │   │   └── pir_renderer.py                 # JSON → Markdown
│   │   │
│   │   ├── runbooks/
│   │   │   ├── library/
│   │   │   │   ├── service_down.md
│   │   │   │   ├── high_error_rate.md
│   │   │   │   ├── high_latency.md
│   │   │   │   ├── pod_crashloop.md
│   │   │   │   ├── disk_full.md
│   │   │   │   └── db_connection_issues.md
│   │   │   ├── matcher.py                      # Alert → runbook
│   │   │   ├── renderer.py                     # Template rendering
│   │   │   └── registry.py
│   │   │
│   │   ├── integrations/
│   │   │   ├── slack/
│   │   │   │   ├── client.py                   # Slack Bolt wrapper
│   │   │   │   ├── channel_manager.py          # Create, archive, invite
│   │   │   │   ├── message_builder.py          # Block Kit messages
│   │   │   │   ├── timer_manager.py            # Live-updating timer
│   │   │   │   └── thread_collector.py         # Fetch full thread
│   │   │   ├── pagerduty/
│   │   │   │   ├── client.py                   # pdpyras wrapper
│   │   │   │   ├── on_call_lookup.py
│   │   │   │   └── incident_annotator.py
│   │   │   ├── grafana/
│   │   │   │   ├── client.py                   # Grafana HTTP API
│   │   │   │   ├── render_api.py               # Screenshot generation
│   │   │   │   └── promql_query.py
│   │   │   └── openai/
│   │   │       ├── client.py
│   │   │       └── rate_limiter.py
│   │   │
│   │   ├── db/
│   │   │   ├── connection.py
│   │   │   ├── models.py                       # SQLAlchemy
│   │   │   └── queries.py
│   │   │
│   │   └── cache/
│   │       ├── redis_client.py
│   │       ├── alert_dedup.py
│   │       ├── on_call_cache.py
│   │       └── channel_state.py
│   │
│   ├── tests/
│   │   ├── test_intent_detector.py
│   │   ├── test_pir_validator.py
│   │   ├── test_orchestrator.py
│   │   ├── test_runbook_matcher.py
│   │   └── fixtures/
│   │       ├── alert_payloads/
│   │       ├── slack_threads/
│   │       └── gpt4_responses/
│   ├── Dockerfile
│   └── requirements.txt
│
├── dashboard/                                  # Next.js dashboard
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx                        # Active + recent incidents
│   │   │   ├── incidents/
│   │   │   │   ├── page.tsx                    # Full list
│   │   │   │   └── [id]/page.tsx               # Detail view
│   │   │   ├── pirs/
│   │   │   │   ├── page.tsx                    # PIR list
│   │   │   │   └── [id]/page.tsx               # Single PIR (markdown)
│   │   │   ├── analytics/
│   │   │   │   ├── mttr/page.tsx
│   │   │   │   ├── on-call-load/page.tsx
│   │   │   │   └── action-items/page.tsx
│   │   │   ├── runbooks/page.tsx
│   │   │   └── api/
│   │   │       ├── incidents/route.ts
│   │   │       ├── pirs/route.ts
│   │   │       └── stats/route.ts
│   │   ├── components/
│   │   │   ├── incidents/
│   │   │   │   ├── IncidentCard.tsx
│   │   │   │   ├── ActiveIncidentBanner.tsx     # Pulsing red bar
│   │   │   │   ├── TimelineView.tsx             # Vertical timeline
│   │   │   │   ├── SeverityBadge.tsx
│   │   │   │   └── DurationDisplay.tsx          # Live timer
│   │   │   ├── pir/
│   │   │   │   ├── PIRDocument.tsx              # Markdown render
│   │   │   │   ├── ActionItemList.tsx
│   │   │   │   └── PIRReviewBanner.tsx
│   │   │   ├── analytics/
│   │   │   │   ├── MTTRChart.tsx
│   │   │   │   ├── IncidentHeatmap.tsx
│   │   │   │   ├── OnCallLoadChart.tsx
│   │   │   │   └── DORAMetrics.tsx
│   │   │   └── common/
│   │   │       ├── StatusBadge.tsx
│   │   │       └── LoadingSkeleton.tsx
│   │   └── types/index.ts
│   ├── Dockerfile
│   └── package.json
│
├── runbook-library/                           # Markdown templates (also in bot/)
│   ├── service_down.md
│   ├── high_error_rate.md
│   └── ...
│
├── prompt-templates/                          # Versioned PIR prompts
│   ├── pir_v1_0_0.txt
│   ├── pir_v1_3_0.txt
│   └── pir_v1_4_0.txt
│
├── charts/
│   └── incidentpilot/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── bot-deployment.yaml
│           ├── dashboard-deployment.yaml
│           ├── postgres-statefulset.yaml
│           ├── redis-deployment.yaml
│           ├── secrets.yaml
│           ├── ingress.yaml
│           └── service.yaml
│
├── monitoring/
│   ├── grafana/
│   │   └── incidentpilot-self-monitoring.json
│   └── alerting-rules.yml                     # Self-monitoring alerts
│
├── .github/workflows/
│   ├── ci.yml
│   ├── deploy.yml
│   └── pir-prompt-validation.yml              # Validate prompt template changes
│
├── docker-compose.yml
├── Makefile
└── README.md
```

### 5.2 The PIR Generation Pipeline (the key feature)

```python
# bot/src/pir/context_collector.py

@dataclass
class PIRContext:
    incident_id: int
    incident_title: str
    severity: str
    started_at: datetime
    resolved_at: datetime
    duration_minutes: int
    
    # Full Slack thread
    slack_messages: List[dict]              # All messages, sorted by ts
    
    # Auto-detected timeline events
    timeline_events: List[dict]              # From TimescaleDB
    
    # Runbook used
    runbook_name: str
    runbook_content: str
    
    # Metrics during incident
    grafana_screenshots: List[str]           # URLs to PNG renders
    error_rate_during: float                 # Avg error rate
    latency_p99_during: float
    
    # On-call info
    on_call_engineer: str
    response_time_seconds: int               # Time from alert to first message


class ContextCollector:
    async def collect(self, incident_id: int) -> PIRContext:
        incident = await db.get_incident(incident_id)
        
        # 1. Fetch entire Slack thread
        messages = await slack.fetch_channel_history(incident.channel_id)
        
        # 2. Fetch timeline events from TimescaleDB
        events = await db.get_timeline_events(incident_id)
        
        # 3. Fetch Grafana screenshots for incident window
        screenshots = await self._fetch_grafana_screenshots(
            service=incident.affected_service,
            start=incident.started_at,
            end=incident.resolved_at
        )
        
        # 4. Compute metric averages during incident
        metrics = await self._compute_incident_metrics(incident)
        
        # 5. Get runbook used
        runbook = await runbook_registry.get(incident.runbook_used)
        
        return PIRContext(
            incident_id=incident.id,
            incident_title=incident.title,
            severity=incident.severity,
            started_at=incident.started_at,
            resolved_at=incident.resolved_at,
            duration_minutes=int((incident.resolved_at - incident.started_at).total_seconds() / 60),
            slack_messages=messages,
            timeline_events=events,
            runbook_name=runbook.name,
            runbook_content=runbook.content,
            grafana_screenshots=screenshots,
            error_rate_during=metrics['error_rate'],
            latency_p99_during=metrics['latency_p99'],
            on_call_engineer=incident.on_call_engineer,
            response_time_seconds=incident.response_time_seconds,
        )
```

```python
# bot/src/pir/prompt_builder.py

class PromptBuilder:
    """Builds versioned PIR prompts with full context."""
    
    def __init__(self, version: str = "v1.4.0"):
        self.version = version
        self.template = self._load_template(version)
    
    def build(self, context: PIRContext) -> tuple[str, str]:
        """Returns (system_prompt, user_prompt)."""
        
        # Format Slack messages chronologically
        thread = "\n".join([
            f"[{msg['ts']}] {msg['user']}: {msg['text']}"
            for msg in context.slack_messages
        ])
        
        # Format timeline events
        timeline = "\n".join([
            f"- {e['timestamp']}: [{e['intent']}] {e['description']}"
            for e in context.timeline_events
        ])
        
        # Build user prompt with all context
        user_prompt = self.template.format(
            incident_title=context.incident_title,
            severity=context.severity,
            duration_minutes=context.duration_minutes,
            slack_thread=thread,
            timeline_events=timeline,
            error_rate=context.error_rate_during,
            latency_p99=context.latency_p99_during,
            on_call=context.on_call_engineer,
            response_time=context.response_time_seconds,
            runbook_name=context.runbook_name,
        )
        
        system_prompt = """You are an expert SRE writing a Post-Incident Review (PIR).
        
You will be given:
1. The Slack thread from the incident war room
2. Auto-detected timeline events
3. Metrics during the incident
4. The runbook that was used

Your output MUST be valid JSON matching this schema:
{
  "executive_summary": "1-2 sentence summary for leadership",
  "timeline": [{"time": "HH:MM", "event": "what happened"}],
  "impact": {
    "duration_minutes": int,
    "severity": "critical|major|minor",
    "users_affected_estimate": "best estimate from context",
    "services_affected": ["..."]
  },
  "root_cause_hypothesis": "your best hypothesis based on the thread",
  "contributing_factors": ["..."],
  "what_went_well": ["..."],
  "what_went_wrong": ["..."],
  "action_items": [
    {
      "description": "specific actionable item",
      "owner": "person mentioned in thread or 'TBD'",
      "priority": "P0|P1|P2",
      "category": "monitoring|runbook|code|process|capacity"
    }
  ],
  "confidence": 0.0-1.0
}

If you don't have enough context to determine root cause, say so explicitly
in the hypothesis and set confidence below 0.5. Do NOT speculate beyond
what the thread supports."""
        
        return system_prompt, user_prompt
```

```python
# bot/src/pir/output_validator.py

from pydantic import BaseModel, Field, validator
from typing import List, Literal


class TimelineEntry(BaseModel):
    time: str = Field(..., pattern=r'^\d{2}:\d{2}$')
    event: str = Field(..., min_length=10, max_length=500)


class Impact(BaseModel):
    duration_minutes: int = Field(..., ge=0)
    severity: Literal["critical", "major", "minor"]
    users_affected_estimate: str
    services_affected: List[str]


class ActionItem(BaseModel):
    description: str = Field(..., min_length=10, max_length=500)
    owner: str
    priority: Literal["P0", "P1", "P2"]
    category: Literal["monitoring", "runbook", "code", "process", "capacity"]


class PIRDocument(BaseModel):
    executive_summary: str = Field(..., min_length=50, max_length=500)
    timeline: List[TimelineEntry] = Field(..., min_items=2)
    impact: Impact
    root_cause_hypothesis: str = Field(..., min_length=20)
    contributing_factors: List[str]
    what_went_well: List[str] = Field(..., min_items=1)
    what_went_wrong: List[str] = Field(..., min_items=1)
    action_items: List[ActionItem] = Field(..., min_items=1, max_items=10)
    confidence: float = Field(..., ge=0.0, le=1.0)


class PIRValidator:
    def validate(self, gpt4_output: dict) -> tuple[bool, PIRDocument | None, str | None]:
        try:
            pir = PIRDocument(**gpt4_output)
            
            # Additional checks beyond schema
            if pir.confidence < 0.5:
                return False, None, f"Low confidence ({pir.confidence}). Need human review."
            
            if len(pir.timeline) < 3:
                return False, None, "Timeline too sparse. Need more events."
            
            return True, pir, None
        except Exception as e:
            return False, None, f"Schema validation failed: {e}"
```

### 5.3 Versioned Prompt Template

```
# prompt-templates/pir_v1_4_0.txt

You are writing a Post-Incident Review for the following incident:

**Incident Title:** {incident_title}
**Severity:** {severity}
**Duration:** {duration_minutes} minutes
**On-call:** {on_call}
**Response time (alert → first action):** {response_time} seconds
**Runbook used:** {runbook_name}

## Metrics During Incident
- Error rate: {error_rate}%
- P99 latency: {latency_p99}ms

## Auto-detected Timeline Events
{timeline_events}

## Full Slack Thread
{slack_thread}

---

Generate a complete Post-Incident Review based ONLY on what's in the thread above.

Important rules:
1. Use ONLY information from the Slack thread, timeline, and metrics. Do not invent details.
2. If root cause isn't clear from the thread, say so and set confidence below 0.5.
3. Action items must be SPECIFIC and ACTIONABLE — not vague aspirations.
4. Quote exact timestamps from messages when constructing the timeline.
5. Identify the on-call engineer's response time and praise quick actions.
6. Categorize each action item appropriately.

Return only valid JSON matching the specified schema.
```

### 5.4 Slack Block Kit Message Builder

```python
# bot/src/integrations/slack/message_builder.py

class IncidentChannelMessageBuilder:
    @staticmethod
    def build_initial_message(alert: dict) -> dict:
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🚨 INCIDENT: {alert['summary']}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Severity:*\n{alert['severity']}"},
                        {"type": "mrkdwn", "text": f"*Service:*\n{alert['service']}"},
                        {"type": "mrkdwn", "text": f"*Started:*\n{alert['started_at']}"},
                        {"type": "mrkdwn", "text": f"*Alert:*\n{alert['alertname']}"},
                    ]
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📊 Grafana"},
                            "url": alert['grafana_url']
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📖 Runbook"},
                            "action_id": "view_runbook"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "🛑 Resolve"},
                            "action_id": "resolve_incident",
                            "style": "danger"
                        }
                    ]
                }
            ]
        }
    
    @staticmethod
    def build_pir_draft(pir: PIRDocument) -> dict:
        return {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "📝 Post-Incident Review (Draft)"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Confidence:* {pir.confidence:.0%}"}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Executive Summary*\n{pir.executive_summary}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root Cause Hypothesis*\n{pir.root_cause_hypothesis}"}},
                # ... more sections
                {
                    "type": "actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "action_id": "approve_pir", "style": "primary"},
                        {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"}, "action_id": "edit_pir"},
                        {"type": "button", "text": {"type": "plain_text", "text": "🔄 Regenerate"}, "action_id": "regenerate_pir"},
                    ]
                }
            ]
        }
```

---

## 6. Database Design & Choice

### 6.1 Comparison Matrix

| Criteria | PostgreSQL 16 + TimescaleDB | Plain PostgreSQL | MongoDB | Cassandra |
|---|---|---|---|---|
| **Most data: relational** | Native — incidents, channels, runbooks, PIRs, action_items | Same | Manual $lookup | Limited |
| **Timeline events: time-series** | TimescaleDB hypertable for the ONE table that needs it | Slow for high-frequency events | Document model OK | Write-optimized but bad for ad-hoc queries |
| **JSONB** | Slack message blobs, GPT-4 responses, runbook variables | Same | Native | No |
| **Full-text search** | tsvector for searching across PIRs | Same | $text | No |
| **ACID** | Full | Full | Single-doc only | Eventual |
| **Operational complexity** | One database (extension) | Same | Medium | High (3+ nodes) |

### VERDICT: PostgreSQL 16 with TimescaleDB Extension (Hybrid Approach)

**Why this hybrid is uniquely correct for IncidentPilot:**

This is the most sophisticated database decision among all my projects. Most of my data is relational and low-frequency:
- Incidents: ~10-50 per day
- PIRs: 1 per incident
- Action items: ~3-5 per incident
- Channels: 1 per incident
- On-call assignments: 1 per incident
- Runbooks: ~20 in library

But ONE table is high-frequency: **timeline_events**. During a busy incident, the bot collects ~50-200 events per minute (every Slack message gets analyzed, intent extracted, stored). Over 30 days, this is potentially 1-5 million rows.

**The right choice: PostgreSQL with TimescaleDB extension applied ONLY to timeline_events.**

```sql
-- Most tables: regular PostgreSQL
CREATE TABLE incidents (...);          -- Relational, indexed normally
CREATE TABLE channels (...);
CREATE TABLE pirs (...);
CREATE TABLE action_items (...);

-- ONE table: TimescaleDB hypertable
CREATE TABLE timeline_events (...);
SELECT create_hypertable('timeline_events', 'time', chunk_time_interval => INTERVAL '1 day');
```

This gives me:
- Relational power for incident-PIR-action-item chains (FKs, JOINs, ACID)
- Time-series performance for timeline event queries (P99 latency over 30 days)
- One database to manage, one connection pool, one ORM
- Selective complexity — TimescaleDB pays its weight only where needed

**Interview answer:**
> "Most of IncidentPilot's data is relational and low-frequency — incidents, channels, PIRs, action items. PostgreSQL handles these natively with foreign keys and JOINs for queries like 'show all action items from incidents involving auth-service.' But timeline events are different — during a busy incident, the bot generates dozens of events per minute. Over 30 days, that's millions of rows. Querying 'all events for incident #47' is fast in either database, but querying 'event frequency over time grouped by intent' over 30 days is slow in plain PostgreSQL and fast in TimescaleDB. So I apply TimescaleDB hypertable partitioning ONLY to that one table. Best of both worlds — relational simplicity for most data, time-series performance where it matters."

### 6.2 Schema Design

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- =============================================
-- RELATIONAL TABLES
-- =============================================

CREATE TABLE services (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) UNIQUE NOT NULL,
    team            VARCHAR(100),
    pagerduty_service_id VARCHAR(50),
    slack_channel_default VARCHAR(50),  -- For non-incident notifications
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE runbooks (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) UNIQUE NOT NULL,
    alert_pattern   VARCHAR(500) NOT NULL,         -- Regex matching alertname
    severity_filter VARCHAR(20),                    -- Optional: only for this severity
    template        TEXT NOT NULL,                  -- Markdown with {placeholders}
    version         VARCHAR(20) NOT NULL DEFAULT '1.0',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_runbook_pattern ON runbooks(alert_pattern);

CREATE TABLE incidents (
    id              BIGSERIAL PRIMARY KEY,
    incident_key    VARCHAR(100) UNIQUE NOT NULL,    -- "inc-2026-04-30-auth-svc-down"
    
    -- Source
    source          VARCHAR(20) NOT NULL,             -- alertmanager, pagerduty, manual
    source_id       VARCHAR(200),                     -- Original alert fingerprint or PD incident ID
    
    -- Classification
    title           VARCHAR(500) NOT NULL,
    description     TEXT,
    severity        VARCHAR(20) NOT NULL,             -- critical, major, minor
    affected_service_id INT REFERENCES services(id),
    affected_services JSONB DEFAULT '[]',             -- Array of service names
    
    -- Lifecycle state
    status          VARCHAR(30) NOT NULL DEFAULT 'detected',
                    -- detected, channel_created, on_call_assigned, runbook_posted,
                    -- in_progress, resolved, pir_generating, pir_generated, reviewed, closed
    
    -- Timing
    started_at      TIMESTAMP NOT NULL,
    detected_at     TIMESTAMP DEFAULT NOW(),
    acknowledged_at TIMESTAMP,                        -- First human response
    resolved_at     TIMESTAMP,
    closed_at       TIMESTAMP,
    
    -- Metrics
    response_time_seconds INT,                        -- detected → acknowledged
    duration_minutes INT,                             -- started → resolved
    
    -- Slack channel
    slack_channel_id VARCHAR(50),
    slack_channel_name VARCHAR(100),
    
    -- On-call
    on_call_user    VARCHAR(100),
    on_call_pagerduty_id VARCHAR(50),
    
    -- Runbook
    runbook_id      INT REFERENCES runbooks(id),
    
    -- Raw data
    raw_alert       JSONB,                            -- Full Alertmanager payload
    
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_incident_status ON incidents(status);
CREATE INDEX idx_incident_started ON incidents(started_at DESC);
CREATE INDEX idx_incident_severity ON incidents(severity);
CREATE INDEX idx_incident_channel ON incidents(slack_channel_id);

CREATE TABLE pir_documents (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     BIGINT UNIQUE REFERENCES incidents(id) ON DELETE CASCADE,
    
    -- Generated content
    executive_summary TEXT,
    timeline        JSONB DEFAULT '[]',                -- Array of {time, event}
    impact          JSONB,                              -- duration, severity, users
    root_cause_hypothesis TEXT,
    contributing_factors JSONB DEFAULT '[]',
    what_went_well  JSONB DEFAULT '[]',
    what_went_wrong JSONB DEFAULT '[]',
    
    -- GPT-4 metadata
    gpt_model       VARCHAR(50),                       -- e.g., gpt-4-turbo
    prompt_version  VARCHAR(20),                       -- e.g., v1.4.0
    confidence_score DECIMAL(3,2),
    generation_time_seconds INT,
    tokens_used     INT,
    
    -- Review state
    status          VARCHAR(20) DEFAULT 'draft',       -- draft, reviewed, approved
    reviewed_by     VARCHAR(100),
    reviewed_at     TIMESTAMP,
    edited          BOOLEAN DEFAULT FALSE,
    final_markdown  TEXT,                              -- The approved version
    
    -- Validation
    validation_passed BOOLEAN,
    validation_errors JSONB DEFAULT '[]',
    
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE action_items (
    id              BIGSERIAL PRIMARY KEY,
    pir_id          BIGINT REFERENCES pir_documents(id) ON DELETE CASCADE,
    incident_id     BIGINT REFERENCES incidents(id) ON DELETE CASCADE,
    
    description     TEXT NOT NULL,
    owner           VARCHAR(100),
    priority        VARCHAR(5) NOT NULL,                -- P0, P1, P2
    category        VARCHAR(30) NOT NULL,               -- monitoring, runbook, code, process, capacity
    
    status          VARCHAR(20) DEFAULT 'open',         -- open, in_progress, completed, cancelled
    jira_ticket     VARCHAR(50),
    completed_at    TIMESTAMP,
    
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_action_status ON action_items(status);
CREATE INDEX idx_action_owner ON action_items(owner);

CREATE TABLE on_call_assignments (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     BIGINT REFERENCES incidents(id),
    user_email      VARCHAR(200),
    slack_user_id   VARCHAR(50),
    pagerduty_user_id VARCHAR(50),
    schedule_name   VARCHAR(200),
    assigned_at     TIMESTAMP DEFAULT NOW()
);

-- =============================================
-- TIMESCALEDB HYPERTABLE
-- =============================================

CREATE TABLE timeline_events (
    time            TIMESTAMPTZ NOT NULL,
    incident_id     BIGINT NOT NULL,
    
    -- Event source
    source          VARCHAR(20) NOT NULL,                -- slack_message, slash_command, system
    
    -- Event content
    intent          VARCHAR(50) NOT NULL,                -- detection, investigation, remediation_start,
                                                          -- remediation_end, recovery_signal, escalation,
                                                          -- metrics_fetch, runbook_step, comment
    description     TEXT NOT NULL,
    
    -- Author
    author_user_id  VARCHAR(50),
    author_name     VARCHAR(100),
    
    -- Slack reference
    slack_message_ts VARCHAR(50),
    slack_thread_ts VARCHAR(50),
    
    -- Metadata
    metadata        JSONB DEFAULT '{}'                    -- Action-specific data
);

SELECT create_hypertable('timeline_events', 'time',
    chunk_time_interval => INTERVAL '1 day');

CREATE INDEX idx_timeline_incident ON timeline_events (incident_id, time DESC);
CREATE INDEX idx_timeline_intent ON timeline_events (intent, time DESC);

-- Compression on chunks older than 7 days
ALTER TABLE timeline_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'incident_id, intent',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('timeline_events', INTERVAL '7 days');
```

### 6.3 Key Queries

```sql
-- 1. Active incidents (dashboard top banner)
SELECT i.*, s.name AS service_name,
       EXTRACT(EPOCH FROM (NOW() - i.started_at)) / 60 AS elapsed_minutes
FROM incidents i
LEFT JOIN services s ON i.affected_service_id = s.id
WHERE i.status NOT IN ('closed', 'reviewed')
ORDER BY i.severity, i.started_at;

-- 2. MTTR per service (DORA metric)
SELECT s.name,
       COUNT(*) AS incidents,
       AVG(i.duration_minutes) AS mttr_minutes,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY i.duration_minutes) AS median_mttr,
       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY i.duration_minutes) AS p95_mttr
FROM incidents i
JOIN services s ON i.affected_service_id = s.id
WHERE i.status = 'closed'
  AND i.resolved_at > NOW() - INTERVAL '30 days'
GROUP BY s.name
ORDER BY mttr_minutes DESC;

-- 3. PIR completion rate
SELECT 
    COUNT(*) FILTER (WHERE i.status IN ('reviewed', 'closed')) AS pirs_completed,
    COUNT(*) FILTER (WHERE i.status = 'pir_generated') AS pirs_pending_review,
    COUNT(*) FILTER (WHERE i.resolved_at IS NOT NULL) AS total_resolved
FROM incidents i
WHERE i.resolved_at > NOW() - INTERVAL '30 days';

-- 4. Open action items by priority
SELECT priority, COUNT(*), 
       AVG(EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400) AS avg_age_days
FROM action_items
WHERE status = 'open'
GROUP BY priority
ORDER BY priority;

-- 5. Timeline events for an incident (uses hypertable)
SELECT time, intent, description, author_name
FROM timeline_events
WHERE incident_id = 47
ORDER BY time;

-- 6. Time-series query: intent distribution over 30 days
SELECT time_bucket('1 day', time) AS day,
       intent,
       COUNT(*) AS event_count
FROM timeline_events
WHERE time > NOW() - INTERVAL '30 days'
GROUP BY day, intent
ORDER BY day, intent;

-- 7. On-call load distribution
SELECT on_call_user, 
       COUNT(*) AS incidents_handled,
       SUM(duration_minutes) AS total_incident_minutes,
       AVG(response_time_seconds) AS avg_response_seconds
FROM incidents
WHERE started_at > NOW() - INTERVAL '30 days'
GROUP BY on_call_user
ORDER BY incidents_handled DESC;
```

---

## 7. Caching & Messaging — Redis 7

### Purposes

**Purpose 1 — Alert Deduplication**
```
Alertmanager and PagerDuty may send duplicates (retries, grouping).
Key: "alert:dedup:{fingerprint}"     → 1 (TTL: 600s = 10 min)
On webhook: SET NX → if fails, skip processing
```

**Purpose 2 — Channel State Cache**
```
Key: "channel:{channel_id}:state"    → { incident_id, status, timer_msg_ts } (TTL: 1h)
Avoids DB roundtrip when bot processes Slack events
```

**Purpose 3 — On-Call Cache**
```
Key: "oncall:{schedule_id}"          → { user_email, user_id } (TTL: 5 min)
PagerDuty rate limits — cache the on-call lookup. Refresh every 5 minutes.
```

**Purpose 4 — Slack Rate Limit Tracking**
```
Slack has tier-based rate limits (~100 calls/min for Tier 1).
Key: "slack:ratelimit:{method}"      → call count (sliding window)
Avoids 429 errors that would cause incident response delays.
```

**Purpose 5 — Alert Ingestion Queue (Redis Streams)**
```
Stream: "alert-events"
Producer: Webhook handlers (Alertmanager, PagerDuty)
Consumer: Orchestrator workers

Why queue?
- Webhook must respond <10s (HTTP timeout). Orchestration takes 30+ seconds
  (PD lookup + Slack channel creation + runbook posting).
- Decouples webhook receipt from orchestration.
- Retry on failure: messages stay pending until ACKed.
- Multiple consumer workers can process in parallel.
```

**Purpose 6 — PIR Generation Cache**
```
Key: "pir:context:{incident_id}"     → Pre-built context (TTL: 1h)
If GPT-4 fails, retry without recollecting context.
```

**Why NOT Kafka?**
> "Under 200 alerts per day across all sources. Kafka's 3-broker overhead for 200 msgs/day is unjustifiable. Redis Streams handles consumer groups, acknowledgment, and pending message visibility natively."

**Why NOT RabbitMQ?**
> "Redis already serves 5 other purposes. Adding RabbitMQ for one queue means a separate Erlang service, separate management UI, separate monitoring. Redis Streams gives me everything I need with zero additional infrastructure."

---

## 8. Design Patterns Used

### 8.1 State Machine Pattern (Incident Lifecycle)

```python
class IncidentState(Enum):
    DETECTED = "detected"
    CHANNEL_CREATED = "channel_created"
    ON_CALL_ASSIGNED = "on_call_assigned"
    RUNBOOK_POSTED = "runbook_posted"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    PIR_GENERATING = "pir_generating"
    PIR_GENERATED = "pir_generated"
    REVIEWED = "reviewed"
    CLOSED = "closed"


VALID_TRANSITIONS = {
    IncidentState.DETECTED: [IncidentState.CHANNEL_CREATED],
    IncidentState.CHANNEL_CREATED: [IncidentState.ON_CALL_ASSIGNED],
    IncidentState.ON_CALL_ASSIGNED: [IncidentState.RUNBOOK_POSTED],
    IncidentState.RUNBOOK_POSTED: [IncidentState.IN_PROGRESS],
    IncidentState.IN_PROGRESS: [IncidentState.RESOLVED],
    IncidentState.RESOLVED: [IncidentState.PIR_GENERATING],
    IncidentState.PIR_GENERATING: [IncidentState.PIR_GENERATED, IncidentState.RESOLVED],  # Retry
    IncidentState.PIR_GENERATED: [IncidentState.REVIEWED],
    IncidentState.REVIEWED: [IncidentState.CLOSED],
}


class IncidentOrchestrator:
    async def transition(self, incident_id: int, new_state: IncidentState):
        incident = await db.get_incident(incident_id)
        current = IncidentState(incident.status)
        
        if new_state not in VALID_TRANSITIONS[current]:
            raise InvalidTransitionError(f"{current} → {new_state} not allowed")
        
        # Run state-specific workflow
        await self._run_workflow(incident, current, new_state)
        
        # Persist new state
        await db.update_incident_status(incident_id, new_state.value)
```

### 8.2 Strategy Pattern (Intent Detection)

```python
class IntentDetectionStrategy(ABC):
    @abstractmethod
    def detect(self, message: str) -> Optional[Intent]: ...

class KeywordIntentDetector(IntentDetectionStrategy):
    """Regex-based detection (MVP)."""
    PATTERNS = {
        "remediation_start": [r"rolling back", r"deploying.*fix", r"reverting"],
        "recovery_signal":   [r"healthy", r"working again", r"recovered"],
        # ...
    }

class GPT4IntentDetector(IntentDetectionStrategy):
    """LLM-based detection (post-MVP, more nuanced)."""
    async def detect(self, message: str) -> Optional[Intent]:
        # Send to GPT-4 with classification prompt
        ...
```

### 8.3 Builder Pattern (Block Kit Messages)

```python
# Already shown in 5.4 — clean Block Kit construction
```

### 8.4 Chain of Responsibility (Alert Processing)

```python
chain = SignatureValidationHandler()
chain.set_next(DeduplicationHandler()) \
     .set_next(SeverityClassifierHandler()) \
     .set_next(RunbookMatcherHandler()) \
     .set_next(EnqueueHandler())

await chain.handle(alert_payload)
```

### 8.5 Observer Pattern (Event Propagation)

```python
event_bus.on('incident:created', create_slack_channel)
event_bus.on('incident:created', notify_pagerduty_acknowledge)
event_bus.on('incident:created', start_metrics_recording)
event_bus.on('incident:resolved', trigger_pir_generation)
event_bus.on('incident:resolved', stop_metrics_recording)
event_bus.on('incident:resolved', update_dashboard)
event_bus.on('pir:generated', notify_review_needed)
```

### 8.6 Template Method (PIR Pipeline)

The PIR generation has a fixed pipeline: collect context → build prompt → call GPT-4 → validate → render → notify. Different incident types may need different context but the pipeline is invariant.

### 8.7 Adapter Pattern (LLM Abstraction)

```python
class LLMAdapter(ABC):
    @abstractmethod
    async def complete(self, prompt: str, schema: dict) -> dict: ...

class GPT4Adapter(LLMAdapter):
    async def complete(self, prompt, schema):
        return await openai_client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[...],
            response_format={"type": "json_object"}
        )

class LocalLLMAdapter(LLMAdapter):
    """Future: support Llama/Mistral for cost/privacy."""
    ...
```

---

## 9. Resilience Patterns & Mitigation Strategies

### 9.1 Resilience Patterns

| Pattern | Where Used | Why |
|---|---|---|
| **Circuit Breaker** | Slack API, PagerDuty API, OpenAI API calls | If external service is down, fail fast instead of timing out |
| **Retry with Backoff** | All external API calls | Handle transient failures (network blips, brief outages) |
| **Timeout** | Every external call (configurable per service) | Prevent stuck workers |
| **Bulkhead** | Separate worker pools per webhook source | Slow PagerDuty API doesn't starve Slack webhook processing |
| **Dead Letter Queue** | Redis Stream for failed events | Manual review of unprocessable events |
| **Graceful Degradation** | If GPT-4 fails, fall back to template PIR | Don't block resolution because PIR generation failed |
| **Idempotency** | All webhook processing keyed by alert fingerprint | Duplicate webhooks safe |

### 9.2 Critical Mitigation Strategies

**Scenario 1: GPT-4 API is down**
- **Mitigation:** Fall back to a skeleton PIR template that an engineer fills in
- **Notification:** Channel message: "⚠️ AI PIR generation unavailable. Skeleton template provided. Engineer please complete."
- **Audit:** Log failure with timestamp, error code, retry count
- **Recovery:** Cache the incident context in Redis. When GPT-4 recovers, regenerate.

**Scenario 2: Slack API rate limits hit**
- **Mitigation:** Redis-based sliding window rate limiter prevents exceeding Slack tier limits
- **Backoff:** If 429 received, exponential backoff with jitter
- **Critical path protection:** Channel creation always succeeds (high priority); decorative updates (timer refresh) can be delayed

**Scenario 3: PagerDuty schedule lookup fails**
- **Mitigation:** Use cached on-call from previous lookup (5-min TTL)
- **Fallback:** If no cache, use service team's primary Slack channel for routing
- **Alert:** Notify SRE team that PagerDuty integration is degraded

**Scenario 4: Database is unreachable**
- **Mitigation:** Buffer writes to Redis Stream, replay when DB recovers
- **Read fallback:** Serve dashboard from Redis cache where possible
- **Critical reads:** Active incident list MUST work — kept in Redis at all times

**Scenario 5: Bot is itself the source of an outage**
- The IRONY: incident management system causing incidents
- **Mitigation:** Deploy bot with at least 2 replicas in different availability zones
- **Self-monitoring:** Prometheus alert if bot's `/health` is failing
- **Escape hatch:** Operations team can fall back to manual channel creation if bot is down

### 9.3 The "Failure of the Failure System" Principle

> "If IncidentPilot itself goes down during an incident, we have a meta-incident. The mitigation hierarchy: First, multi-replica deployment with K8s redeployment. Second, monitoring of the monitor — Prometheus alerts on the bot's own SLI degradation. Third, a documented manual fallback runbook for operators to handle incidents without the bot. The metric to watch is the bot's own availability SLO, which I track on a separate dashboard. If the bot's availability drops below 99.9%, that becomes the highest-priority engineering task."

---

## 10. DevOps / MLOps Wrapper (GPT-4 Governance)

This is the MLOps layer that wraps GPT-4 — what separates production AI from prototype AI.

### 10.1 Prompt Versioning

```yaml
# prompt-templates/prompt_versions.yaml

current: v1.4.0

versions:
  v1.0.0:
    file: pir_v1_0_0.txt
    deployed: 2026-04-01
    deprecated: 2026-04-15
    notes: "Initial version"
    
  v1.3.0:
    file: pir_v1_3_0.txt
    deployed: 2026-04-15
    deprecated: 2026-04-29
    notes: "Added structured action item categories"
    
  v1.4.0:
    file: pir_v1_4_0.txt
    deployed: 2026-04-29
    notes: "Added confidence score, refined timeline format"
    metrics:
      avg_confidence_score: 0.78
      validation_pass_rate: 0.92
      avg_tokens: 3200
```

Every PIR generation records which prompt version was used. This enables:
- A/B testing new prompts on a percentage of incidents
- Rollback to a previous version if quality degrades
- Quality metrics over time per prompt version

### 10.2 Output Validation

Already shown in 5.2 — Pydantic schema validation ensures GPT-4 returns well-formed PIRs.

### 10.3 Quality Metrics Tracking

```python
# bot/src/pir/quality_tracker.py

class PIRQualityTracker:
    async def record_generation(
        self,
        incident_id: int,
        prompt_version: str,
        gpt_model: str,
        confidence_score: float,
        validation_passed: bool,
        tokens_used: int,
        generation_time_seconds: int,
        engineer_edits_count: Optional[int] = None,  # Recorded after review
    ):
        await db.insert_pir_quality_record(...)
    
    async def get_metrics(self, prompt_version: str, time_window: timedelta):
        return {
            "total_generations": ...,
            "avg_confidence": ...,
            "validation_pass_rate": ...,
            "avg_tokens_per_pir": ...,
            "avg_engineer_edits": ...,  # Lower = better PIR drafts
            "regeneration_rate": ...,   # How often engineers click "regenerate"
        }
```

### 10.4 Human-in-the-Loop Fallback

```python
def should_fall_back(pir_result: PIRGenerationResult) -> bool:
    if not pir_result.validation_passed:
        return True
    if pir_result.confidence < 0.5:
        return True
    if pir_result.tokens_used > MAX_TOKENS_THRESHOLD:
        return True  # Suspicious — possibly hallucinating
    return False


def fallback_to_template(incident: Incident) -> str:
    """When GPT-4 fails, provide a structured template for the engineer."""
    return f"""
# Post-Incident Review: {incident.title}

## Executive Summary
[TODO: Engineer to fill in 1-2 sentences for leadership]

## Timeline
- {incident.started_at.strftime('%H:%M')}: Incident detected
- [TODO: Add key events]
- {incident.resolved_at.strftime('%H:%M')}: Incident resolved

## Impact
- Duration: {incident.duration_minutes} minutes
- Severity: {incident.severity}
- Users affected: [TODO: estimate]

## Root Cause
[TODO: Engineer to identify root cause]

## Action Items
[TODO: List action items with owners]

---
ℹ️ This template was generated because automatic PIR generation was unavailable.
"""
```

### 10.5 Cost Tracking

```python
# bot/src/integrations/openai/cost_tracker.py

PRICING = {
    "gpt-4-turbo": {"input": 0.01 / 1000, "output": 0.03 / 1000},  # per token
}

class OpenAICostTracker:
    async def record(self, model: str, input_tokens: int, output_tokens: int):
        cost = (input_tokens * PRICING[model]["input"] +
                output_tokens * PRICING[model]["output"])
        
        await redis.hincrby("openai:daily_cost", date.today().isoformat(), cost * 100)  # cents
        await db.insert_cost_record(model, input_tokens, output_tokens, cost)
    
    async def get_daily_cost(self) -> float:
        return await redis.hget("openai:daily_cost", date.today().isoformat()) / 100
```

### 10.6 Prompt Change Review Process

```yaml
# .github/workflows/pir-prompt-validation.yml

name: PIR Prompt Validation

on:
  pull_request:
    paths: ['bot/src/pir/prompt_templates/**']

jobs:
  validate-prompt-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Run prompt smoke tests
        run: |
          # Use 10 historical incidents as test cases
          # Generate PIRs with new prompt
          # Compare output quality metrics with previous version
          python -m bot.src.pir.tests.regression_test
      
      - name: Cost analysis
        run: |
          # Estimate cost difference vs current prompt
          python -m bot.src.pir.tests.cost_analysis
      
      - name: Require manual approval
        # Require security or SRE team review for prompt changes
        # Prevents accidental prompt drift
```

---

## 11. Docker & Kubernetes Deployment

### 11.1 Docker Compose

```yaml
version: '3.8'

services:
  bot:
    build: ./bot
    ports: ["8000:8000"]
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/incidentpilot
      - REDIS_URL=redis://redis:6379
      - SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}
      - SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET}
      - PAGERDUTY_API_KEY=${PAGERDUTY_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - GRAFANA_URL=${GRAFANA_URL}
      - GRAFANA_API_KEY=${GRAFANA_API_KEY}

  dashboard:
    build: ./dashboard
    ports: ["3000:3000"]

  postgres:
    image: timescale/timescaledb:latest-pg16
    ports: ["5432:5432"]
    environment: { POSTGRES_DB: incidentpilot, POSTGRES_USER: postgres, POSTGRES_PASSWORD: postgres }
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./bot/sql/init.sql:/docker-entrypoint-initdb.d/01-init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]

  grafana:
    image: grafana/grafana:latest
    ports: ["3001:3000"]
    volumes:
      - ./monitoring/grafana:/etc/grafana/provisioning

volumes:
  postgres_data:
```

### 11.2 Helm Chart

```yaml
# charts/incidentpilot/values.yaml

bot:
  replicaCount: 2  # HA — bot must always be available
  image:
    repository: yourdockerhub/incidentpilot-bot
    tag: latest
  resources:
    requests: { cpu: 200m, memory: 512Mi }
    limits: { cpu: 1000m, memory: 1Gi }
  
  # Pod Disruption Budget — at least 1 pod always available
  pdb:
    enabled: true
    minAvailable: 1

dashboard:
  replicaCount: 1
  image:
    repository: yourdockerhub/incidentpilot-dashboard
    tag: latest

postgresql:
  enabled: true
  image: timescale/timescaledb:latest-pg16
  persistence: { size: 20Gi }

redis:
  enabled: true

slack:
  botToken:
    secretName: incidentpilot-slack
  signingSecret:
    secretName: incidentpilot-slack

pagerduty:
  apiKey:
    secretName: incidentpilot-pagerduty

openai:
  apiKey:
    secretName: incidentpilot-openai
  model: gpt-4-turbo
  maxTokens: 4000
```

---

## 12. CI/CD Pipeline

```yaml
# .github/workflows/deploy.yml

name: Deploy IncidentPilot

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: timescale/timescaledb:latest-pg16
        env: { POSTGRES_DB: incidentpilot_test, POSTGRES_USER: postgres, POSTGRES_PASSWORD: postgres }
        ports: [5432:5432]
      redis:
        image: redis:7-alpine
        ports: [6379:6379]
    
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: cd bot && pip install -r requirements.txt -r requirements-dev.txt
      - run: cd bot && pytest tests/ -v --cov=src
      
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd dashboard && npm ci && npm run lint && npm run test

  pir-prompt-tests:
    runs-on: ubuntu-latest
    if: contains(github.event.head_commit.modified, 'prompt_templates/')
    steps:
      - uses: actions/checkout@v4
      - name: Test PIR prompts against historical incidents
        run: |
          python -m bot.src.pir.tests.regression_test
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY_TEST }}

  build-and-push:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKER_USERNAME }}
          password: ${{ secrets.DOCKER_PASSWORD }}
      - run: |
          docker build -t ${{ secrets.DOCKER_USERNAME }}/incidentpilot-bot:${{ github.sha }} ./bot
          docker push ${{ secrets.DOCKER_USERNAME }}/incidentpilot-bot:${{ github.sha }}

  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          helm upgrade --install incidentpilot ./charts/incidentpilot \
            --set bot.image.tag=${{ github.sha }} \
            --namespace incidentpilot --create-namespace
```

---

## 13. Monitoring — The "Scaling Decision" to Document

### Dashboard Panels (4 Core)

**Panel 1: Active Incidents Banner**
- Pulsing red bar showing active incidents with elapsed time
- Auto-refresh every 30 seconds

**Panel 2: MTTR Trend**
- 30-day MTTR line chart per service
- Annotations for major changes (new on-call rotation, infrastructure changes)

**Panel 3: Bot Performance (self-monitoring)**
- Time-to-acknowledge (alert → channel created)
- PIR generation success rate (validation pass %)
- GPT-4 cost per day

**Panel 4: Action Items Aging**
- Open action items by priority and age
- Escalation triggers when P0 items age >7 days

### The "Scaling Decision" to Document

> **Scaling Decision: Hybrid PostgreSQL + TimescaleDB Extension Over Pure TimescaleDB**
>
> **Problem:** IncidentPilot has mixed data patterns. Most data (incidents, PIRs, action items) is low-frequency relational data with FK relationships. But timeline_events is high-frequency time-series data — during a busy incident, the bot generates 50-200 events per minute.
>
> **Option A — Pure PostgreSQL:** Use plain PostgreSQL for everything. Pros: simplest. Cons: timeline_events queries over 30 days scan millions of rows. "Show event distribution by intent over 30 days" takes 5-10 seconds.
>
> **Option B — Pure TimescaleDB:** Apply TimescaleDB hypertables to all tables. Pros: consistent. Cons: hypertables add operational overhead (chunk management, compression policies) for tables that don't benefit. Incidents table at 50/day doesn't need partitioning.
>
> **Option C — Hybrid:** Use plain PostgreSQL for relational tables, apply TimescaleDB hypertable ONLY to timeline_events. Pros: each table uses the optimal storage. Cons: developers must remember which tables are hypertables.
>
> **Decision:** Option C. TimescaleDB is a PostgreSQL extension — I get one database, one connection pool, one ORM. The cost of "remembering" which table is a hypertable is trivial (it's documented in the migration). The benefit is dramatic: timeline_events queries over 30 days drop from 5-10s to under 100ms via continuous aggregates and chunk pruning, while incidents and PIRs queries remain in plain PostgreSQL with no overhead.
>
> **The principle:** Don't apply specialized storage to data that doesn't need it. TimescaleDB earns its complexity only on time-series workloads. Most SRE data isn't time-series — it's relational. Use the right tool for each table.
>
> **When I'd reconsider:** If we scaled to thousands of incidents per day with millions of action items, I might partition the action_items table by created_at. But at current scale, no partitioning needed.

---

## 14. Interview Prep — Top Questions & Answers

### SRE / Incident Management

**Q1: "Walk me through the architecture."**

> "When an alert fires from Prometheus or PagerDuty, the webhook hits my FastAPI bot which validates signatures and dedups via Redis. The orchestrator runs a state machine: detected → channel_created → on_call_assigned → runbook_posted → in_progress → resolved → pir_generated → reviewed → closed. For each transition, the bot calls Slack to create the channel, PagerDuty for on-call lookup, and a runbook engine to render the alert-specific runbook. During the incident, a timeline collector watches Slack messages and auto-tags events using regex/keyword detection. When an engineer types /resolve, the bot triggers GPT-4 PIR generation with full context — Slack thread, timeline events, Grafana metrics — and posts a structured draft for review. PostgreSQL stores everything relational; one TimescaleDB hypertable handles high-frequency timeline events."

**Q2: "Why did you build this from scratch instead of using Rootly or Incident.io?"**

> "Two reasons. First, the project demonstrates deep understanding of incident management primitives — channel orchestration, PagerDuty integration, GPT-4 prompt engineering, MLOps governance. Plugging in Rootly proves I can configure a tool, not that I understand the system. Second, building it surfaces design decisions interviewers want to discuss: state machine design, multi-source webhook handling, AI fallback strategies, prompt versioning. These are the senior-level conversations. Mentioning Rootly in interviews shows awareness; building IncidentPilot shows depth."

**Q3: "What's the key SRE metric this improves?"**

> "Three metrics. First, Time-to-Acknowledge: from alert fired to first human action. Without the bot, this is 5-15 minutes (humans see the page, navigate to PagerDuty, find runbook, create war room). With the bot, it's 30-60 seconds — the channel exists with the runbook posted before the human has even checked their phone. Second, MTTR: faster TTA + structured runbook = faster resolution. Third, PIR completion rate: typically only 30-50% of incidents get PIRs because writing them is painful. Auto-generation pushes this to 90%+ because the draft is done — engineers just review."

### Database

**Q4: "Why hybrid PostgreSQL + TimescaleDB instead of one or the other?"**

> "Most data — incidents, PIRs, action items — is relational and low-frequency. Tens of incidents per day. PostgreSQL handles this perfectly with FKs and JOINs. But timeline_events are different. During a busy incident, the bot generates 50-200 events per minute — every Slack message gets analyzed, intent extracted, stored. Over 30 days, millions of rows. Queries like 'event frequency by intent over 30 days' are slow in plain PostgreSQL but fast in TimescaleDB via continuous aggregates and chunk pruning. So I apply TimescaleDB hypertable to ONLY that one table. TimescaleDB IS a PostgreSQL extension, so it's one database, one connection, one ORM. The cost is zero; the benefit is significant."

**Q5: "Why not MongoDB for the flexible PIR document structure?"**

> "PIR documents have a flexible internal structure (JSONB columns handle this in PostgreSQL), but their relationships are deeply relational: PIR → incident → service → team, and PIR → action items → owner. MongoDB would force $lookup chains for queries like 'all open P0 action items from incidents in the last 30 days for the platform team.' PostgreSQL handles this with a 3-table JOIN in milliseconds. JSONB gives me MongoDB's schema flexibility for the document content while keeping relational power for everything that connects to it."

### MLOps / GPT-4 Governance

**Q6: "How do you ensure GPT-4 doesn't hallucinate in PIRs?"**

> "Five layers. First, prompt engineering — the system prompt explicitly says 'Use ONLY information from the Slack thread. Do not invent details. If you don't have enough context, say so and set confidence below 0.5.' Second, JSON mode forces structured output that I validate against a Pydantic schema — required sections, length limits, valid enums. Third, confidence scoring — if GPT-4 reports confidence below 0.5, I fall back to a template instead of trusting the output. Fourth, human review is mandatory before PIR finalization — no PIR is auto-published. Fifth, prompt versioning — I track which prompt version generated each PIR, so I can A/B test improvements and roll back if quality degrades."

**Q7: "What's prompt versioning and why does it matter?"**

> "Prompt templates are like code. They drift, they have bugs, they need refactoring. If I update a prompt and PIR quality degrades, I need to know which prompt was used to generate which PIR. Without versioning, debugging is impossible. So I store every prompt template as a versioned file (pir_v1_4_0.txt), record the version with each PIR generation, and track quality metrics per version: validation pass rate, average confidence, average engineer edits. This gives me a feedback loop to improve prompts. Critical for production AI."

**Q8: "What if GPT-4's API is down during an incident?"**

> "Graceful degradation. The PIR generator has three layers. Layer 1: GPT-4 with full context. Layer 2: GPT-4 with reduced context (if context is too large for retry). Layer 3: skeleton template — structured headers with TODOs that an engineer fills in. The bot posts whichever layer succeeded with a notification: 'AI generation unavailable, please complete this template.' The principle: a degraded PIR is better than no PIR. Engineers are 80% more likely to write a PIR if they have a starting structure than from a blank page."

### Architecture

**Q9: "How do you handle concurrent incidents?"**

> "Each incident is independently processed. The orchestrator state machine runs per-incident. Slack channels are unique per incident, so there's no contention there. The Redis Stream consumer can have multiple workers — incidents process in parallel. The shared resources are PagerDuty API rate limits and Slack API rate limits — both have Redis-based sliding window rate limiters. If we have 10 simultaneous incidents, the bot creates 10 channels in parallel, but PagerDuty lookups and Slack message sends are throttled to stay within rate limits. In practice, even at 100 incidents/day, simultaneity is rare — typical max is 3-5 active at once."

**Q10: "How do you keep the bot itself reliable?"**

> "Three pillars. First, deployment: 2+ replicas with Pod Disruption Budget ensuring at least 1 always available, anti-affinity to spread across zones. Second, self-monitoring: Prometheus alerts on the bot's own SLIs — webhook response time, PIR generation success rate, GPT-4 latency. If the bot's availability drops below 99.9%, that's a critical alert. Third, manual fallback runbook: documented procedures for operators to handle incidents without the bot if it goes down. The principle: the incident response system is critical infrastructure, but it's not the only path. There's always a manual escape hatch."

**Q11: "Describe the demo."**

> "I trigger a synthetic alert via the Alertmanager webhook. Within 5 seconds, a new Slack channel appears: '#inc-2026-04-30-auth-svc-down'. The on-call engineer is auto-invited and pinged. A runbook is pinned. A live timer message appears. I type messages in the channel: 'Checking dashboard', 'Found the issue, rolling back v2.3.1'. The bot auto-tags these as timeline events. I post '/metrics auth-service' and the bot fetches a Grafana screenshot in real-time. I type '/resolve'. Within 30 seconds, the bot posts a complete PIR draft with timeline, impact analysis, root cause hypothesis, and 4 action items extracted from my messages. The dashboard shows the incident with full timeline. That's the wow moment — engineers walking into the channel after the fact see a complete, structured incident record."

---

## 15. Deployment Checklist

- [ ] Slack App registered, OAuth working
- [ ] Webhook receivers handling Alertmanager + PagerDuty
- [ ] Channel auto-creation with proper naming
- [ ] On-call lookup from PagerDuty
- [ ] All 6 runbook templates working
- [ ] Live-updating incident timer
- [ ] Timeline event auto-detection
- [ ] /resolve, /metrics, /escalate slash commands
- [ ] GPT-4 PIR generation with structured output
- [ ] PIR validation + fallback to template
- [ ] Dashboard: incidents, PIRs, MTTR, action items
- [ ] Helm chart deploying full platform
- [ ] CI/CD pipeline green
- [ ] Self-monitoring: Prometheus alerts on bot health
- [ ] Demo video recording: alert → channel → resolution → PIR
- [ ] README with architecture diagram, screenshots
- [ ] Live deployment with public link

### Cost Estimate

| Service | Provider | Cost |
|---|---|---|
| GKE Autopilot (small cluster) | Google Cloud | ~$70/mo ($300 free credit) |
| OR EC2 t3.medium | AWS | ~$30/mo |
| OpenAI GPT-4 API | OpenAI | ~$15-30/mo (demo usage) |
| Slack | Slack | Free |
| PagerDuty | PagerDuty | Free dev tier |
| **Total** | | **~$45-100/month** |

---

## Final Word — Why This Project Wins SRE Interviews

IncidentPilot is the most interview-relevant SRE project of all my proposals. Here's why:

**Universal pain → universal credibility:** Every SRE on every team has manually written PIRs at 2 AM. Every team has fumbled the first 5 minutes of an incident. Every interviewer has felt this pain. When you say "I built a bot that creates the war room in 30 seconds and drafts the PIR in 30 seconds," you're solving a problem they personally hate.

**Quantified impact:** "8 minutes to 45 seconds. 2 hours to 15 minutes." These numbers are concrete, memorable, and immediately translate to business value (engineering hours saved, faster incident resolution).

**Full SRE workflow ownership:** This project covers detection (alerts), response (channels, runbooks), resolution (slash commands, metrics fetching), post-mortem (PIR generation), and analytics (dashboard, DORA metrics). It's the complete SRE incident lifecycle.

**Production AI / MLOps:** This is the only project among my SRE proposals that demonstrates governing GPT-4 in production — prompt versioning, output validation, confidence scoring, human-in-the-loop fallback. Companies are figuring out how to use LLMs reliably; you've already built it.

**The maturity hierarchy:**
1. **Level 1 (most freshers):** "I configured Slack notifications for Prometheus alerts" → basic
2. **Level 2 (good freshers):** "I built a Slack bot that creates incident channels" → intermediate
3. **Level 3 (advanced):** "I built a complete incident lifecycle automation with GPT-4 PIR generation, prompt versioning, and human-in-the-loop fallback" → senior SRE thinking

**The sentence that wins:**
> "Every incident automatically gets a war room channel in 30 seconds, a runbook tailored to the alert, and a GPT-4-drafted PIR after resolution. We went from writing PIRs at midnight on Sundays to reviewing PIRs Monday morning. PIR completion rate went from 40% to 95%."

This is the project that gets hired at Zepto SRE, Blinkit SRE, Razorpay SRE, PhonePe SRE, and Atlassian India. Every quick-commerce company, every fintech, every SaaS — they all have this exact pain. You solved it as a fresher.

---

*Document prepared as a complete implementation guide for IncidentPilot — Incident Management Automation Bot (2026)*
