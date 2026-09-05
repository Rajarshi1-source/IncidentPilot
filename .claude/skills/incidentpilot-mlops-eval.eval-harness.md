# IncidentPilot — Eval Harness Reference

Companion reference for `incidentpilot-mlops-eval`. Read this before writing eval code.

## Contents

1. Layout
2. The recording format
3. Fake adapters and determinism
4. Corpus composition and labelling
5. Metric implementations
6. The gate and the baseline
7. CLI and Makefile
8. CI workflow
9. Counterfactual mode
10. Judge usage and agreement measurement
11. Common failure modes of the harness itself

---

## 1. Layout

```
bot/eval/
├── __init__.py
├── run.py                # entrypoint: replay corpus, compute metrics, gate
├── recorder.py           # production-side capture
├── replayer.py           # deterministic replay against fakes
├── fakes/
│   ├── chat.py           # fake Slack: records calls, replays events
│   ├── paging.py
│   ├── metrics.py        # replays recorded PromQL responses
│   └── llm.py            # replays recorded model responses OR calls live in --live mode
├── metrics.py            # the five scorers
├── gate.py               # hard/soft comparison against baseline
├── report.py             # markdown table for the PR comment
├── judge/
│   ├── rubric.md
│   └── agreement.py      # judge vs human on the labelled subset
└── corpus/
    ├── manifest.json     # bucket -> incident ids, and the committed baseline
    └── incident_<id>.jsonl
```

---

## 2. The recording format

One JSONL file per incident. Every line is `{"t": <seconds from incident start>, "kind": ..., ...}`.

```jsonl
{"t":0.000,"kind":"alert","payload":{"labels":{...},"annotations":{...},"startsAt":"..."}}
{"t":0.180,"kind":"paging.oncall","request":{"schedule":"payments"},"response":{"user":"U123"}}
{"t":1.021,"kind":"chat.conversations.create","request":{"name":"inc-..."},"response":{"channel":{"id":"C123"}}}
{"t":1.400,"kind":"chat.event.message","payload":{"ts":"1757....","user":"U123","text":"looking"}}
{"t":612.0,"kind":"promql","query":"sum(increase(http_requests_total{...}[11m]))","response":{...}}
{"t":640.1,"kind":"llm","role":"synthesize","prompt_sha256":"9f2c...","request":{"system":"...","user":"..."},"response":{"content":"{...}"}}
{"t":999.9,"kind":"ground_truth","labels":{
    "timeline":[{"t":94,"intent":"remediation_start"},{"t":300,"intent":"recovery_signal"}],
    "action_items":["add replica lag alert","document promotion runbook"],
    "root_cause":"replica promotion delayed by stale connection pool",
    "expected_incidents":1,
    "expected_alerts":40}}
```

Rules that keep the corpus usable:

- **Redact at capture, not later.** A corpus with real customer data committed to git is a liability
  you cannot un-commit.
- **Strip secrets** — tokens, signing secrets, auth headers — with an allowlist of retained fields,
  not a denylist of removed ones.
- **Record responses, not just requests.** Replay needs both sides.
- **`ground_truth` is human-labelled, once, carefully.** It is the thing all metrics score against,
  so a sloppy label permanently distorts every future comparison.

---

## 3. Fake adapters and determinism

Replay determinism comes from the architecture, not from a random seed:

- Pure `domain/` logic — no clock, no randomness, no I/O.
- Every boundary is an adapter, so every boundary is fakeable.
- Time is injected: the replayer advances a virtual clock using recorded `t` values, so timeouts and
  windows behave identically on every run.

```python
class FakeChat(ChatAdapter):
    def __init__(self, recording: Recording):
        self.recording, self.calls = recording, []

    async def create_channel(self, name: str, idempotency_key: str) -> Channel:
        self.calls.append(("conversations.create", name, idempotency_key))
        return self.recording.response_for("chat.conversations.create")

    async def fetch_history(self, *a, **kw):
        raise AssertionError(
            "history must never be called on the hot path — the transcript is event-sourced")
```

That `AssertionError` is deliberate: the harness enforces the architectural rule, so a regression
that reintroduces a history fetch fails loudly in CI rather than silently in production.

If a run performs a network call, the harness fails. Assert it explicitly by installing a socket
guard for the duration of the replay — "no network" is a property worth testing, not just intending.

---

## 4. Corpus composition and labelling

| Bucket | n | What it protects |
|---|---|---|
| Real incidents (demo cluster) | 12 | Realistic language and mess |
| Storms (5 / 12 / 40 alerts) | 4 | Storm compression |
| False positive / flapping | 3 | No PIR must be produced |
| Reopened | 2 | State branch |
| Abandoned | 2 | State branch |
| Metrics unavailable | 3 | Impact says unavailable, never estimates |
| Provider timeout / invalid JSON | 3 | Fallback ladder |
| Unknown root cause | 3 | The model must abstain |
| Code-switched English/Hindi | 4 | Known weakness, measured |
| Very long (400+ messages) | 2 | Context assembly, truncation |
| Adversarial (fake IDs in messages) | 2 | Validator resistance |

The adversarial bucket deserves explanation. Put a message in the transcript containing something
that looks like a reference — `msg:1757000000.000100` — that does not exist in the store. A model
that copies it into a citation must be rejected by the validator. This is a cheap, concrete test of
prompt-injection resistance and it is the kind of test that impresses in a review.

Labelling guidance: label the timeline at intent granularity, not word granularity; label action
items as the *set* a competent reviewer would extract; record `expected_incidents` for storm
fixtures. Keep labels in the recording file, not a separate spreadsheet — they drift apart otherwise.

---

## 5. Metric implementations

```python
def timeline_f1(pred: list[TimelineEntry], truth: list[dict], bucket_s: int = 60) -> float:
    """Match on (time bucket, intent). Bucketing avoids punishing a 3-second offset,
    which is noise, while still catching an event placed in the wrong phase."""
    P = {(int(e.t // bucket_s), e.intent) for e in pred}
    T = {(int(g["t"] // bucket_s), g["intent"]) for g in truth}
    if not P and not T: return 1.0
    tp = len(P & T)
    prec = tp / len(P) if P else 0.0
    rec  = tp / len(T) if T else 0.0
    return 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)


def citation_precision(draft: PIRDraft, ctx: GroundedContext) -> float:
    """Cited IDs that exist AND support the claim, over total citations."""
    total = supported = 0
    for _, claim in walk_claims(draft):
        for c in claim.citations:
            total += 1
            if c.ref in ctx.valid_reference_set() and supports(c, claim.text, ctx):
                supported += 1
    return 1.0 if total == 0 else supported / total


def action_item_recall(pred: list[ActionItem], truth: list[str], thresh: float = 0.6) -> float:
    """Fuzzy match: action items are paraphrases, so exact match under-reports badly."""
    if not truth: return 1.0
    hit = sum(1 for t in truth if max((similarity(t, p.description) for p in pred), default=0) >= thresh)
    return hit / len(truth)


def storm_compression(created: int, expected: int) -> float:
    return max(0.0, 1 - abs(created - expected) / max(expected, 1))
```

Report **per-bucket** breakdowns alongside aggregates. An aggregate F1 of 0.91 hiding 0.44 on the
code-switched bucket is a metric that lies to you, and the per-bucket view is what tells you where to
spend the next week.

---

## 6. The gate and the baseline

The baseline lives in `corpus/manifest.json` next to the prompt registry entry, and moves only when a
human promotes a version:

```json
{
  "baseline": {
    "prompt_version": "v2.1.0",
    "timeline_f1": 0.908,
    "citation_precision": 1.000,
    "action_item_recall": 0.856,
    "storm_compression": 0.975,
    "cost_per_pir_usd": 0.318
  }
}
```

```
HARD (must equal): citation_precision == 1.0
                   uncited_claims == 0
                   fabricated_citations == 0
SOFT (tolerance):  timeline_f1        >= baseline - 0.03
                   action_item_recall >= baseline - 0.03
                   storm_compression  >= 0.95
BUDGET:            cost_per_pir_usd   <= 0.50
                   p95_generation_ms  <= 30000
```

Why the soft tolerance exists: model outputs vary slightly run to run even at temperature 0, and a
zero-tolerance soft gate produces flaky CI that people learn to re-run until green — which is worse
than no gate. Three points is wide enough to absorb noise and narrow enough to catch real regression.

---

## 7. CLI and Makefile

```bash
python -m eval.run --corpus eval/corpus                     # score, print, no gate
python -m eval.run --corpus eval/corpus --gate              # exit 1 on failure
python -m eval.run --corpus eval/corpus --gate --report eval-report.md
python -m eval.run --incident 204 --verbose                 # one incident, full trace
python -m eval.run --corpus eval/corpus --live              # call real providers (rare, manual)
python -m eval.run --corpus eval/corpus --set roles.synthesize.model=MODEL_ID   # counterfactual
```

```makefile
eval:        ; uv run python -m eval.run --corpus bot/eval/corpus
eval-gate:   ; uv run python -m eval.run --corpus bot/eval/corpus --gate --report eval-report.md
eval-record: ; uv run python -m eval.recorder --attach   # capture the next incident
```

`--live` exists so you can refresh recordings when a provider changes behaviour, but it is never
what CI runs.

---

## 8. CI workflow

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
essentially always. A gate that never runs is worse than no gate, because you believe you have one.

Report format for the PR comment:

```
| metric               | value   | baseline | delta   | gate |
|----------------------|---------|----------|---------|------|
| timeline_f1          | 0.913   | 0.908    | +0.005  | PASS |
| citation_precision   | 1.000   | 1.000    |  0.000  | PASS |
| uncited_claims       | 0       | 0        |  0      | PASS |
| action_item_recall   | 0.842   | 0.856    | -0.014  | PASS |
| storm_compression    | 0.975   | 0.975    |  0.000  | PASS |
| cost_per_pir_usd     | 0.312   | budget 0.500 |     | PASS |
GATE: PASS (6/6) · corpus 40 · wall 9.2s · network calls 0
```

---

## 9. Counterfactual mode

`--set key=value` overrides configuration for the replay only, so you can answer "what would have
happened if" with evidence:

| Question | Command |
|---|---|
| Would a wider window have over-merged? | `--set correlation.window_s=600` |
| Does the cheaper model hold quality? | `--set roles.synthesize.model=MODEL_ID` |
| Does a stricter validator over-reject? | `--set validator.support_threshold=0.8` |
| How many channels without correlation? | `--set correlation.enabled=false` |

Report the delta against the recorded actual, not just the absolute number. "Disabling correlation
turns the 40-alert fixture into 34 channels" is the sentence that sells the feature.

---

## 10. Judge usage and agreement

Some qualities — is the narrative coherent, is the root-cause hypothesis reasonable — cannot be
scored deterministically. Use the `judge` role with a fixed rubric in `judge/rubric.md`, and then do
the step most people skip: measure the judge against human labels on a 10-incident subset and report
the agreement rate.

```
judge_agreement: 0.82 (n=10, Cohen's kappa 0.61)
```

Knowing your judge agrees with humans 82% of the time is what licenses gating **softly** on it while
gating **hard** on the deterministic metrics. Stating that split out loud is a genuinely senior thing
to say in a review.

The judge never runs in the production path. It is offline-only, and the config marks it so.

---

## 11. Failure modes of the harness itself

| Symptom | Likely cause | Fix |
|---|---|---|
| Replay results differ run to run | An unadaptered clock, `random`, or `uuid4` in domain code | Inject it; the harness should surface which module |
| Gate passes but production quality drops | Corpus does not cover the failing shape | Add a bucket; every production surprise becomes a fixture |
| Gate flaky at the tolerance edge | Soft tolerance too tight, or corpus too small | Widen tolerance or grow the corpus — never re-run until green |
| Cost per PIR in eval far below production | Recorded responses are shorter than real ones | Refresh recordings with `--live` periodically |
| Everything degrades to skeleton in replay | Validator threshold too strict after a change | Check `_supports()` false-rejection rate before blaming the model |
| Corpus rots as the schema changes | Recordings encode old field names | Version the recording format and write a migration for the corpus |

The last one is worth planning for from the start: put `"format": 1` at the top of every recording
file, and treat corpus migration as a normal task rather than a crisis.
