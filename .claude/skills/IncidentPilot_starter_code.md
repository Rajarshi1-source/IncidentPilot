# IncidentPilot — Starter Code

Runnable reference implementation for the Rev 2 plan. Every skill points here instead of duplicating
code. Pins: Python 3.14.7, FastAPI 0.141.1, Pydantic 2.13.5, SQLAlchemy 2.0.52, psycopg 3.3.5,
redis-py 8.1.0, slack-bolt 1.30.0, PostgreSQL 18.6 + TimescaleDB 2.29.2, Valkey 9.1.2.

## Parts

1. Configuration and the domain core (states, fingerprint, intent, fatigue)
2. Ingest — webhook trust boundary, normalization, correlation
3. Orchestration — transitions, outbox, relay, consumer
4. Slack — event-sourced transcript, Block Kit, fake adapter
5. PIR — grounded context, LLM router, generator, validator, skeleton
6. Eval — recorder and replayer
7. Resilience — breaker, degradation manager, lifeboat
8. Tests worth writing first

---

## Part 1 — Configuration and the domain core

```python
# src/incidentpilot/config/settings.py
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IP_", env_nested_delimiter="__")

    environment: str = "dev"
    database_url: SecretStr
    valkey_url: SecretStr

    chat_provider: str = "slack"          # or "fake"
    paging_provider: str = "pagerduty"    # or "static"
    metrics_provider: str = "prometheus"  # or "fake"
    llm_config_path: str = "config/models.yaml"

    slack_signing_secret: SecretStr | None = None
    alertmanager_bearer: SecretStr | None = None
    paging_webhook_secret: SecretStr | None = None
    signature_max_age_s: int = 300

    llm_budget_usd_per_incident: float = 0.50
    llm_budget_usd_per_day: float = 5.0
    require_citations: bool = True         # CI asserts this is True in prod configs
    redact_pii: bool = True

    correlation_window_s: int = 300
    merge_threshold: float = 0.62
    storm_threshold: int = 5

    fatigue_lookback_h: int = 8
    fatigue_max_pages: int = 2

settings = Settings()  # import this, never os.environ
```

```python
# src/incidentpilot/domain/states.py
from enum import StrEnum
from dataclasses import dataclass

class S(StrEnum):
    DETECTED="detected"; TRIAGING="triaging"; ENGAGED="engaged"
    ACKNOWLEDGED="acknowledged"; MITIGATING="mitigating"; MITIGATED="mitigated"
    RESOLVED="resolved"; REOPENED="reopened"
    PIR_DRAFTING="pir_drafting"; PIR_DRAFTED="pir_drafted"; PIR_FAILED="pir_failed"
    CLOSED="closed"; MERGED="merged"; FALSE_POSITIVE="false_positive"; ABANDONED="abandoned"

TERMINAL = {S.CLOSED, S.MERGED, S.FALSE_POSITIVE, S.ABANDONED}

TRANSITIONS: dict[S, set[S]] = {
    S.DETECTED:     {S.TRIAGING, S.FALSE_POSITIVE},
    S.TRIAGING:     {S.ENGAGED, S.MERGED, S.FALSE_POSITIVE},
    S.ENGAGED:      {S.ACKNOWLEDGED, S.MITIGATED, S.ABANDONED, S.FALSE_POSITIVE},
    S.ACKNOWLEDGED: {S.MITIGATING, S.MITIGATED, S.ABANDONED},
    S.MITIGATING:   {S.MITIGATED, S.ACKNOWLEDGED},
    S.MITIGATED:    {S.RESOLVED, S.MITIGATING},
    S.RESOLVED:     {S.PIR_DRAFTING, S.REOPENED},
    S.REOPENED:     {S.ACKNOWLEDGED},
    S.PIR_DRAFTING: {S.PIR_DRAFTED, S.PIR_FAILED},
    S.PIR_FAILED:   {S.PIR_DRAFTING, S.PIR_DRAFTED},
    S.PIR_DRAFTED:  {S.CLOSED},
}

TIMING_FIELD = {S.ENGAGED: "engaged_at", S.ACKNOWLEDGED: "acknowledged_at",
                S.MITIGATED: "mitigated_at", S.RESOLVED: "resolved_at", S.CLOSED: "closed_at"}

class InvalidTransition(Exception): ...

def assert_transition(cur: S, nxt: S) -> None:
    if cur in TERMINAL:
        raise InvalidTransition(f"{cur} is terminal")
    if nxt not in TRANSITIONS.get(cur, set()):
        raise InvalidTransition(f"{cur} -> {nxt} not permitted")

@dataclass(frozen=True)
class TransitionEffect:
    outbox: tuple[str, ...] = ()
    compensate: tuple[str, ...] = ()

EFFECTS: dict[tuple[S, S], TransitionEffect] = {
    (S.TRIAGING, S.ENGAGED): TransitionEffect(
        outbox=("create_channel", "invite_responders", "pin_runbook", "start_timer"),
        compensate=("archive_channel",)),
    (S.TRIAGING, S.MERGED):      TransitionEffect(outbox=("post_merge_notice",)),
    (S.RESOLVED, S.PIR_DRAFTING):TransitionEffect(outbox=("post_generating_notice",)),
    (S.PIR_DRAFTING, S.PIR_DRAFTED): TransitionEffect(outbox=("post_pir","notify_reviewers")),
    (S.PIR_FAILED, S.PIR_DRAFTED):   TransitionEffect(outbox=("post_pir_skeleton",)),
}
```

```python
# src/incidentpilot/domain/fingerprint.py
import hashlib
STABLE_LABELS = ("alertname", "service", "namespace", "cluster", "severity")

def fingerprint(alert: dict) -> str:
    lbl = alert.get("labels", {})
    raw = "|".join(f"{k}={lbl.get(k, '')}" for k in STABLE_LABELS)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

def dedup_key(alert: dict) -> str:
    return alert.get("groupKey") or fingerprint(alert)
```

```python
# src/incidentpilot/domain/intent.py
import re
from enum import StrEnum
from dataclasses import dataclass

class IntentKind(StrEnum):
    NOISE="noise"; INVESTIGATION="investigation"; REMEDIATION_START="remediation_start"
    REMEDIATION_END="remediation_end"; RECOVERY_SIGNAL="recovery_signal"
    ESCALATION="escalation"; STATUS_UPDATE="status_update"; RUNBOOK_STEP="runbook_step"

SNAPSHOT_TRIGGERS = {IntentKind.REMEDIATION_START, IntentKind.RECOVERY_SIGNAL}

RULES: list[tuple[IntentKind, re.Pattern[str], float]] = [
    (IntentKind.REMEDIATION_START, re.compile(
        r"\b(rolling ?back|roll ?back|restart(ing)?|failing ?over|promot(e|ing)|"
        r"scal(e|ing) (up|down)|deploy(ing)? (fix|hotfix)|kubectl (rollout|delete|scale)|"
        r"helm (rollback|upgrade))\b", re.I), 0.95),
    (IntentKind.RECOVERY_SIGNAL, re.compile(
        r"\b(recovered|back to normal|error rate (is )?down|lag (is )?gone|"
        r"looks (healthy|green|clean)|traffic normal)\b", re.I), 0.9),
    (IntentKind.INVESTIGATION, re.compile(
        r"\b(check(ing)?|look(ing)? at|seeing|grep|logs?|dashboard|query|"
        r"why|what changed|suspect)\b", re.I), 0.7),
    (IntentKind.ESCALATION, re.compile(
        r"\b(escalat(e|ing)|paging|need help|calling in|waking)\b", re.I), 0.85),
    (IntentKind.REMEDIATION_END, re.compile(
        r"\b(rollback (done|complete)|deployed|applied|promotion (done|complete))\b", re.I), 0.9),
]

@dataclass(frozen=True)
class Intent:
    kind: IntentKind
    confidence: float
    summary: str

def detect_intent(text: str) -> Intent:
    """Layer 1 of the ladder: deterministic, free, ~80% of real chatter.
    Escalate to embeddings, then to the `extract` LLM role, only on NOISE."""
    t = text.strip()
    if len(t) < 3 or t.startswith(":"):
        return Intent(IntentKind.NOISE, 1.0, "")
    for kind, pat, conf in RULES:
        if pat.search(t):
            return Intent(kind, conf, t[:200])
    return Intent(IntentKind.NOISE, 0.5, t[:200])
```

```python
# src/incidentpilot/domain/fatigue.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ResponderWindow:
    responder: str
    pages_8h: int
    night_pages_24h: int
    incident_minutes_24h: int
    consecutive_oncall_days: int
    sev1_count_7d: int

def fatigue_score(r: ResponderWindow, max_pages: int = 2) -> float:
    s  = 0.30 * min(r.pages_8h / max_pages, 1.0)
    s += 0.25 * min(r.night_pages_24h / 2, 1.0)
    s += 0.20 * min(r.incident_minutes_24h / 240, 1.0)
    s += 0.15 * (1.0 if r.consecutive_oncall_days >= 5 else 0.0)
    s += 0.10 * min(r.sev1_count_7d / 3, 1.0)
    return min(s, 1.0)

def routing_decision(score: float) -> str:
    if score < 0.5:  return "page_primary"
    if score < 0.75: return "page_primary_and_invite_secondary"
    return "page_secondary_notify_primary"   # never silently remove the primary
```

---

## Part 2 — Ingest

```python
# src/incidentpilot/api/security.py
import hmac, hashlib, time

class Unauthorized(Exception): ...

def verify_bearer(header: str | None, expected: str) -> None:
    if not header or not header.startswith("Bearer "):
        raise Unauthorized("missing bearer")
    if not hmac.compare_digest(header[7:], expected):
        raise Unauthorized("bad bearer")

def verify_slack(raw_body: bytes, ts: str | None, sig: str | None, secret: str,
                 max_age_s: int = 300) -> None:
    if not ts or not sig:
        raise Unauthorized("missing slack headers")
    if abs(time.time() - int(ts)) > max_age_s:
        raise Unauthorized("stale timestamp")
    base = b"v0:" + ts.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise Unauthorized("bad signature")

def verify_paging(raw_body: bytes, sig_header: str | None, secret: str) -> None:
    """Providers may send several signatures during key rotation: accept if ANY matches."""
    if not sig_header:
        raise Unauthorized("missing signature")
    mine = "v1=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(mine, s.strip()) for s in sig_header.split(",")):
        raise Unauthorized("bad signature")
```

```python
# src/incidentpilot/api/webhooks/alertmanager.py
from fastapi import APIRouter, Request, Response, Header

router = APIRouter()

@router.post("/webhooks/alertmanager", status_code=202)
async def receive(request: Request, authorization: str | None = Header(default=None)):
    """Alertmanager does NOT sign payloads — no HMAC exists. Bearer + mTLS + NetworkPolicy
    is the trust model. Return 202 fast; orchestration happens in the worker."""
    verify_bearer(authorization, settings.alertmanager_bearer.get_secret_value())
    raw = await request.body()                       # raw bytes; parse ourselves
    payload = json.loads(raw)
    accepted = 0
    for alert in payload.get("alerts", []):
        if alert.get("status") == "resolved":
            await streams.xadd("alerts.resolved", normalize(alert, payload))
        else:
            await streams.xadd("alerts.raw", normalize(alert, payload))
        accepted += 1
    WEBHOOK_LATENCY.labels(source="alertmanager", outcome="ok").observe(request.state.elapsed)
    return {"accepted": accepted}
```

```python
# src/incidentpilot/domain/correlation.py
from dataclasses import dataclass

@dataclass(frozen=True)
class Decision:
    merge_into: int | None
    score: float = 0.0
    reasons: tuple[str, ...] = ()

    @classmethod
    def new_incident(cls) -> "Decision": return cls(None)
    @classmethod
    def merge(cls, incident_id: int, score: float, reasons: list[str]) -> "Decision":
        return cls(incident_id, score, tuple(reasons))

def jaccard(a: dict, b: dict) -> float:
    sa, sb = {f"{k}={v}" for k, v in a.items()}, {f"{k}={v}" for k, v in b.items()}
    return 0.0 if not (sa | sb) else len(sa & sb) / len(sa | sb)

def correlate(alert, open_incidents, graph, cfg) -> Decision:
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

def pick_root_signal(alerts, graph):
    """First alert on the deepest failing dependency — not the loudest, not the newest."""
    return min(alerts, key=lambda a: (-graph.depth(a.service), a.starts_at))
```

---

## Part 3 — Orchestration

```python
# src/incidentpilot/orchestration/orchestrator.py
async def transition(self, session, incident_id: int, nxt: S, actor: str, reason: str = ""):
    inc = await self.repo.get_for_update(session, incident_id)
    cur = S(inc.state)
    assert_transition(cur, nxt)

    await session.execute(insert(IncidentTransition).values(
        incident_id=incident_id, seq=inc.state_seq + 1,
        from_state=cur, to_state=nxt, actor=actor, reason=reason))

    inc.state, inc.state_seq = nxt, inc.state_seq + 1
    if field := TIMING_FIELD.get(nxt):
        setattr(inc, field, utcnow())

    for action in EFFECTS.get((cur, nxt), TransitionEffect()).outbox:
        await self.outbox.enqueue(session, incident_id, action)

    INCIDENT_TRANSITIONS.labels(from_state=cur, to_state=nxt).inc()
    return inc
```

```python
# src/incidentpilot/orchestration/outbox.py
import hashlib, random
from sqlalchemy import text

def idem_key(incident_id: int, action: str, disc: str = "") -> str:
    return hashlib.sha256(f"{incident_id}|{action}|{disc}".encode()).hexdigest()

class Outbox:
    async def enqueue(self, session, incident_id: int, action: str,
                      payload: dict | None = None, disc: str = "") -> None:
        await session.execute(insert(OutboxEvent).values(
            incident_id=incident_id, action=action, payload=payload or {},
            idempotency_key=idem_key(incident_id, action, disc),
        ).on_conflict_do_nothing(index_elements=["idempotency_key"]))

CLAIM = text("""
    UPDATE outbox_events SET status='claimed', attempts = attempts + 1
    WHERE id IN (SELECT id FROM outbox_events
                 WHERE status='pending' AND next_attempt_at <= now()
                 ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :batch)
    RETURNING *;
""")

async def relay_once(session, handlers, batch: int = 20) -> int:
    rows = (await session.execute(CLAIM, {"batch": batch})).mappings().all()
    for row in rows:
        try:
            result = await handlers[row["action"]](row)     # the ONLY external write
            await mark_dispatched(session, row["id"], result)
        except RetryableError as e:
            delay = min(2 ** row["attempts"], 300) + random.uniform(0, 5)
            await defer(session, row["id"], delay, str(e))
            if row["attempts"] >= 8:
                await mark_dead(session, row["id"])
                OUTBOX_DEAD.labels(action=row["action"]).inc()
    return len(rows)
```

```python
# src/incidentpilot/orchestration/consumer.py
LOCK = text("SELECT pg_try_advisory_xact_lock(hashtext('inc:' || :iid))")

async def consume(r, session_factory, stream="alerts.raw", group="cg-orchestrate",
                  consumer="worker-1"):
    await ensure_group(r, stream, group)
    while not shutting_down():
        entries = await r.xreadgroup(group, consumer, {stream: ">"}, count=10, block=2000)
        for _, msgs in entries or []:
            for msg_id, fields in msgs:
                async with session_factory() as session, session.begin():
                    iid = int(fields.get("incident_id", 0) or 0)
                    if iid and not (await session.execute(LOCK, {"iid": iid})).scalar():
                        continue                      # leave un-ACKed; another worker later
                    await handle(session, fields)
                await r.xack(stream, group, msg_id)

async def reclaim_stale(r, stream, group, consumer):
    cursor = "0-0"
    while True:
        cursor, claimed, _ = await r.xautoclaim(
            stream, group, consumer, min_idle_time=60_000, start_id=cursor, count=10)
        for msg_id, fields in claimed:
            RECLAIMED.labels(stream=stream).inc()
            await handle_reclaimed(msg_id, fields)
        if cursor == "0-0":
            return
```

---

## Part 4 — Slack

```python
# src/incidentpilot/transcript/ingestor.py
@app.event("message")
async def on_message(event, logger):
    if event.get("bot_id") or event.get("subtype") in {"channel_join", "channel_leave"}:
        return
    incident_id = await cache.incident_for_channel(event["channel"])
    if incident_id is None:
        return

    async with uow() as session:
        stmt = insert(SlackMessage).values(
            incident_id=incident_id, channel_id=event["channel"], ts=event["ts"],
            thread_ts=event.get("thread_ts"), user_id=event.get("user"),
            text=event["text"], raw=event,
        ).on_conflict_do_nothing(index_elements=["channel_id", "ts"])
        if (await session.execute(stmt)).rowcount == 0:
            DUPLICATE_MESSAGES.inc(); return

        intent = detect_intent(event["text"])
        if intent.kind is not IntentKind.NOISE:
            await session.execute(insert(TimelineEvent).values(
                time=slack_ts_to_dt(event["ts"]), incident_id=incident_id,
                intent=intent.kind, confidence=intent.confidence,
                source_message_ts=event["ts"], description=intent.summary,
                author_user_id=event.get("user")))
            if intent.kind in SNAPSHOT_TRIGGERS:
                await session.execute(insert(OutboxEvent).values(
                    incident_id=incident_id, action="capture_metric_snapshot",
                    payload={"reason": str(intent.kind), "at": event["ts"]},
                    idempotency_key=idem_key(incident_id, "snapshot", event["ts"])))
```

```python
# src/incidentpilot/adapters/chat/fake.py
class FakeChat(ChatAdapter):
    """Makes the whole demo runnable with no Slack workspace, and makes the
    crash tests and eval harness possible."""
    def __init__(self):
        self.calls: list[tuple] = []
        self.channels: dict[str, dict] = {}

    async def create_channel(self, name: str, idempotency_key: str) -> dict:
        self.calls.append(("conversations.create", name, idempotency_key))
        if idempotency_key in self.channels:
            return self.channels[idempotency_key]          # idempotent, like the real relay
        ch = {"id": f"C{len(self.channels):06d}", "name": name}
        self.channels[idempotency_key] = ch
        return ch

    async def fetch_history(self, *a, **kw):
        raise AssertionError("history must never be called on the hot path")

    def call_count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)
```

```python
# src/incidentpilot/adapters/chat/ratelimit.py
PRIORITY = {"conversations.create": 0, "conversations.invite": 0,
            "chat.postMessage:runbook": 0, "chat.postMessage:pir": 1,
            "chat.update:timer": 2, "reactions.add": 3}

async def acquire(r, method: str, channel: str | None, priority: int) -> bool:
    """chat.postMessage is ~1/sec per channel, so bucket per (method, channel).
    Priority 2-3 work is DROPPED under pressure, not queued: a stale timer is
    invisible, a delayed war room is an outage."""
    key = f"ip:rl:{method}:{channel or 'global'}"
    allowed = await leaky_bucket(r, key, rate=RATES[method], burst=BURSTS[method])
    if not allowed and priority >= 2:
        SLACK_SHED.labels(method=method).inc()
        return False
    if not allowed:
        await backoff_with_jitter()
        return await acquire(r, method, channel, priority)
    return True
```

---

## Part 5 — PIR

```python
# src/incidentpilot/pir/schema.py
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field

class CitationKind(StrEnum):
    MESSAGE="message"; TIMELINE="timeline"; METRIC="metric"
    DEPLOY="deploy"; ALERT="alert"; RUNBOOK="runbook"

class Citation(BaseModel):
    kind: CitationKind
    ref: str
    model_config = ConfigDict(extra="forbid")

class Claim(BaseModel):
    text: str = Field(min_length=3, max_length=600)
    citations: list[Citation] = Field(min_length=1)     # the invariant, in the type system
    model_config = ConfigDict(extra="forbid")

class ActionItem(BaseModel):
    description: str
    owner: str | None = None
    priority: str = Field(pattern=r"^P[0-2]$")
    category: str
    citations: list[Citation] = Field(min_length=1)

class PIRDraft(BaseModel):
    summary: list[Claim] = Field(min_length=1, max_length=5)
    timeline: list["TimelineEntry"]
    contributing_factors: list[Claim] = Field(default_factory=list)
    what_went_well: list[Claim] = Field(default_factory=list)
    what_went_wrong: list[Claim] = Field(default_factory=list)
    root_cause_hypothesis: Claim | None = None          # "unknown" is a valid answer
    action_items: list[ActionItem] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")
    # impact is deliberately absent: computed and injected, never generated
```

```python
# src/incidentpilot/adapters/llm/router.py
class LLMRouter:
    def __init__(self, cfg, providers, budget, redactor):
        self.cfg, self.providers, self.budget, self.redactor = cfg, providers, budget, redactor

    async def complete(self, *, role: str, system: str, user: str, schema, incident_id: int):
        self.budget.check(incident_id, role)          # raises BudgetExceeded -> skeleton
        user = self.redactor.redact(user)            # PII boundary, before egress
        last: Exception | None = None
        for spec in self.cfg.chain_for(role):        # [primary, secondary]
            try:
                res = await self.providers[spec.provider].complete_structured(
                    role=role, system=system, user=user, schema=schema,
                    max_output_tokens=spec.max_output_tokens,
                    temperature=spec.temperature, timeout_s=spec.timeout_s)
                self.budget.record(incident_id, res.cost_usd)
                LLM_CALLS.labels(role=role, provider=spec.provider,
                                 model=res.model, outcome="ok").inc()
                return res
            except (ProviderError, TimeoutError, ValidationError) as e:
                last = e
                LLM_CALLS.labels(role=role, provider=spec.provider,
                                 model=spec.model, outcome="error").inc()
        raise AllProvidersFailed(role) from last
```

```python
# src/incidentpilot/pir/validator.py
class CitationValidator:
    def __init__(self, support_threshold: float = 0.35):
        self.threshold = support_threshold

    def validate(self, draft: PIRDraft, ctx: GroundedContext) -> ValidationReport:
        errors: list[str] = []
        valid = ctx.valid_reference_set()
        for path, claim in walk_claims(draft):
            if not claim.citations:
                errors.append(f"{path}: uncited claim")
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

    def _supports(self, c: Citation, text: str, ctx) -> bool:
        """Cheap and deterministic: normalized token overlap plus shared entities.
        Catches the common failure of a real id attached to an unrelated sentence."""
        src = ctx.text_for(c.ref)
        if not src:
            return False
        a, b = normalize_tokens(text), normalize_tokens(src)
        overlap = len(a & b) / max(len(a), 1)
        return overlap >= self.threshold or bool(shared_entities(text, src))
```

```python
# src/incidentpilot/pir/generator.py
async def generate(incident_id: int) -> PIRDocument:
    ctx = await build_grounded_context(incident_id)     # from OUR store, never Slack
    impact = await compute_impact(incident_id)          # PromQL, deterministic
    ctx.attach_impact(impact)

    for attempt, layer in enumerate(("llm_primary", "llm_secondary"), start=1):
        try:
            res = await router.complete(role="synthesize", system=SYSTEM,
                                        user=render_prompt(ctx), schema=PIRDraft,
                                        incident_id=incident_id)
            report = validator.validate(res.parsed, ctx)
            if report.ok:
                return persist(incident_id, res, report, layer)
            VALIDATION_FAILURES.labels(layer=layer).inc()
            log.warning("pir.validation_failed", errors=report.errors[:5])
        except (AllProvidersFailed, BudgetExceeded) as e:
            log.warning("pir.layer_failed", layer=layer, error=str(e))
            break

    return persist_skeleton(incident_id, ctx, impact)   # always terminates in something local
```

---

## Part 6 — Eval

```python
# bot/eval/replayer.py
class Replayer:
    def __init__(self, recording: Recording):
        self.rec = recording
        self.clock = VirtualClock(start=recording.start)

    async def run(self) -> ReplayResult:
        fakes = Fakes(chat=FakeChat(), paging=FakePaging(self.rec),
                      metrics=FakeMetrics(self.rec), llm=FakeLLM(self.rec))
        app = build_app(settings_override(fakes), clock=self.clock)
        with no_network_guard():                 # any socket call fails the run
            for entry in self.rec.entries:
                self.clock.advance_to(entry.t)
                await app.dispatch(entry)
        return ReplayResult(
            incidents=fakes.chat.channels, draft=app.last_pir_draft,
            calls=fakes.chat.calls, cost=fakes.llm.total_cost,
            ground_truth=self.rec.ground_truth)
```

```python
# bot/eval/run.py  (entrypoint)
def main() -> int:
    results = [Replayer(load(p)).run_sync() for p in corpus_paths(args.corpus)]
    metrics = aggregate([score(r) for r in results])
    if args.report:
        Path(args.report).write_text(render_markdown(metrics, baseline))
    print(render_table(metrics, baseline))
    if args.gate:
        gr = evaluate(metrics, baseline)
        for f in gr.failures:
            print(f"::error::{f}")
        return 0 if gr.passed else 1
    return 0
```

---

## Part 7 — Resilience

```python
# src/incidentpilot/resilience/degradation.py
from enum import IntEnum

class Level(IntEnum):
    NORMAL=0; DEGRADED=1; BROWNOUT=2; LIFEBOAT=3

class DegradationManager:
    """Automatic and announced. Silent degradation is worse than failure, because
    people keep trusting output that is no longer trustworthy."""
    def __init__(self, chat, gauge): self.chat, self.gauge, self._level = chat, gauge, Level.NORMAL

    async def set_level(self, level: Level, reason: str) -> None:
        if level == self._level:
            return
        prev, self._level = self._level, level
        self.gauge.set(int(level))
        log.warning("degradation.changed", from_level=int(prev), to_level=int(level), reason=reason)
        if level >= Level.DEGRADED:
            await self.chat.post_ops_banner(BANNERS[level].format(reason=reason))

    @property
    def llm_available(self) -> bool: return self._level < Level.DEGRADED
    @property
    def db_available(self) -> bool:  return self._level < Level.BROWNOUT
```

```python
# lifeboat/main.py  — SEPARATE image, no application imports, ~40 lines
import json, os, pathlib, urllib.request

PROBE = os.environ["PROBE_URL"]; HOOK = os.environ["FALLBACK_WEBHOOK"]
STATE = pathlib.Path(os.environ.get("LAST_ONCALL_PATH", "/state/oncall.json"))
FAILS = pathlib.Path("/tmp/lifeboat.fails")

def healthy() -> bool:
    try:
        with urllib.request.urlopen(PROBE, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False

def main() -> None:
    n = int(FAILS.read_text()) if FAILS.exists() else 0
    if healthy():
        FAILS.write_text("0"); return
    n += 1; FAILS.write_text(str(n))
    if n != 2:                                   # fire once, on the second failure
        return
    oncall = json.loads(STATE.read_text()).get("responder", "unknown") if STATE.exists() else "unknown"
    body = json.dumps({"text":
        f":rotating_light: IncidentPilot is unreachable. Fall back to the manual process.\n"
        f"Last known on-call: {oncall}\nManual runbook: {os.environ.get('MANUAL_RUNBOOK_URL','')}"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        HOOK, data=body, headers={"Content-Type": "application/json"}), timeout=5)

if __name__ == "__main__":
    main()
```

Every feature added to a lifeboat is another way for it to fail. Keep it this small.

---

## Part 8 — Tests worth writing first

```python
def test_history_never_called_on_hot_path(fake_chat, incident_fixture):
    generate_pir_sync(incident_fixture.id)
    assert fake_chat.call_count("conversations.history") == 0
    assert fake_chat.call_count("conversations.replies") == 0

@pytest.mark.parametrize("crash_at", CRASH_POINTS)          # 12 injection points
def test_no_orphan_channel_on_crash(crash_at, fake_chat, db):
    with crash_after(crash_at):
        run_orchestration(alert_fixture())
    run_orchestration(alert_fixture())                       # retry after "restart"
    assert fake_chat.call_count("conversations.create") == 1
    assert db.count("incidents") == 1

def test_storm_compresses_to_one_incident(db):
    for a in load_fixture("storm_40.json"):
        ingest_sync(a)
    assert db.count("incidents", "parent_incident_id IS NULL") == 1
    assert db.scalar("SELECT correlated_alert_count FROM incidents LIMIT 1") == 40
    assert db.count("alerts", "is_root_signal") == 1

def test_fabricated_citation_is_rejected(fake_llm, ctx):
    fake_llm.next_response(pir_with_citation("msg:9999999999.999999"))
    report = CitationValidator().validate(fake_llm.parse(), ctx)
    assert not report.ok and "fabricated" in report.errors[0]

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

These five cover the architectural invariants that the rest of the system depends on. Write them
before the features they guard — they are cheap now and very expensive to retrofit.
