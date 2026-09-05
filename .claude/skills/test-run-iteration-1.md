# Functional test run — iteration 1

Method note, stated plainly: claude.ai has no subagents, so these were run by the same model that
wrote the skills, following the skill-creator's claude.ai guidance. That makes this a sanity check
on whether the skill body contains the answer, **not** an independent evaluation. Baseline
(no-skill) comparisons were skipped for the same reason. Re-run in Claude Code with subagents for a
rigorous version.

---

## T1 — "compression on timeline_events is barely doing anything"

**Prompt (eval #22):** *"compression on timeline_events is barely doing anything, the compressed
chunks are almost the same size. im segmenting by incident_id and intent"*

**Skill consulted:** `incidentpilot-timescale-data`

**Did the skill contain the answer?** Yes, directly. The "Critical rules" section names this exact
mistake ("Compression segments by the LOW-cardinality column") and the reference file repeats it
under "Common mistake" with the corrected DDL.

**Output produced:**

> `incident_id` is the highest-cardinality column in the table, so segmenting on it gives you one
> tiny compressed batch per incident. With per-batch overhead you can finish larger than you started.
> Move it to `orderby`:
>
> ```sql
> ALTER TABLE timeline_events SET (
>   timescaledb.compress,
>   timescaledb.compress_segmentby = 'intent',
>   timescaledb.compress_orderby   = 'incident_id, time DESC');
> ```
>
> You keep fast per-incident reads because the sparse min/max index prunes batches on
> `incident_id`, and you get long runs of identical `intent` values for the compressor. Decompress
> and recompress the affected chunks after the change, then check
> `chunk_compression_stats('timeline_events')` — you should see roughly 8-12x on realistic data.

**Verdict:** pass. Nothing was invented; both the diagnosis and the fix came from the skill.

---

## T2 — "pir generation is timing out on conversations.history"

**Prompt (eval #9):** *"my pir generation is timing out. it calls conversations.history at /resolve
and only gets 15 messages back per call even though i set limit=200. the app isnt in the
marketplace"*

**Skill consulted:** `incidentpilot-slack-platform`

**Did the skill contain the answer?** Yes — the constraint, the date, the reason, and the
architectural fix are all in the skill body, which is the point of putting them there.

**Output produced:**

> That is the March 2026 rate-limit change, not a bug in your code. Non-Marketplace apps get 1
> request per minute and a hard cap of 15 objects on `conversations.history` and
> `conversations.replies`, and the `limit` parameter is silently clamped. A 150-message thread would
> take ten minutes to read.
>
> Do not paginate around it — remove the read entirely. Subscribe to `message.channels` and persist
> every message to `slack_messages` as it arrives, with `UNIQUE (channel_id, ts)` and
> `ON CONFLICT DO NOTHING` so redelivery is harmless. At `/resolve`, the PIR pipeline reads your own
> database and never touches Slack. Keep one caller of `conversations.history` — a 15-minute
> reconciler, globally throttled to one call per minute — purely to emit a
> `transcript_completeness` metric.
>
> Add this test so the regression cannot come back:
> ```python
> def test_history_never_called_on_hot_path(fake_chat, incident_fixture):
>     generate_pir_sync(incident_fixture.id)
>     assert fake_chat.call_count("conversations.history") == 0
> ```

**Verdict:** pass. The skill also supplied the framing sentence ("Slack is a write path, not a read
path") which is what makes this useful in an interview rather than just in code.

---

## T3 — "the postmortem said 12000 users, nobody knows where that came from"

**Prompt (eval #13):** *"the generated postmortem said the outage affected roughly 12000 users.
nobody knows where that number came from, its definitely not from prometheus. how do i stop the
model inventing numbers"*

**Skill consulted:** `incidentpilot-pir-grounding`

**Did the skill contain the answer?** Yes. The "Critical rules" section leads with it and the
"Deterministic impact" section has the mechanism.

**Output produced:**

> Two changes, and the first is the one that actually fixes it.
>
> **1. Remove `impact` from the output schema.** If the model can emit a number, it eventually will.
> `PIRDraft` has no impact field at all in this design — impact is computed from PromQL over the
> incident window before the call, written to `incidents.impact`, and injected into the prompt as a
> fenced "verified facts" block with an instruction to reuse those numbers exactly.
>
> **2. Add a numeric guard to the validator.** Extract every number from the draft and assert
> membership in `ctx.computed_impact_numbers`; anything else is an ungrounded numeric claim and
> fails validation, which sends the PIR to the retry path and then to the skeleton.
>
> If Prometheus is unreachable, the section reads "metrics unavailable for this window." Never an
> estimate. An honest gap beats a confident fabrication, and it is a better answer in a review.

**Verdict:** pass, with one observation: the skill's phrasing ("numbers are computed, never
generated") carried straight into the answer, which is the intended behaviour for a skill whose job
is to make a specific architectural stance stick.

---

## What the run changed

Nothing in the skill bodies. The routing eval (see `results.json`) drove the only edits in iteration
2: project-scoping clauses were added to the trigger lists of `incidentpilot-resilience-brownout`
and `incidentpilot-state-orchestration`, because "multi window burn rate" and "transactional outbox"
are generic phrases that appear verbatim in those descriptions and would otherwise pull the skill
into unrelated questions.
