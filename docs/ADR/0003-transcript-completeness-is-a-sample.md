# ADR 0003 — Transcript completeness is a sample, and the reconciler archives orphans directly

- **Status:** Accepted
- **Date:** 7 September 2026
- **Week:** 4
- **Refines:** Detailed Implementation Plan §8 (W4-10, W4-11, W4-12), INV-02, INV-03

Two decisions the week-4 task list implies but does not settle, recorded here
because both look like shortcuts until the constraint behind them is stated.

---

## Decision 1 — `ip_transcript_completeness` is a sampled estimator

### Context

W4-11 asks the reconciler to emit "messages stored divided by messages in the
channel". The denominator is not obtainable.

Since **3 March 2026**, a non-Marketplace Slack app gets `conversations.history`
at **1 request per minute, 15 messages per request**. Counting a 200-message
channel is therefore fourteen paged requests — **fourteen minutes** — during
which the channel has moved on and the count is already wrong. Doing that for
every active channel, every fifteen minutes, needs more budget than exists.

The plan's own framing makes the problem sharper rather than softer: this is
the SLI with the tightest target in `docs/SLO.md`, precisely because its failure
mode is silent. A completeness metric that was itself quietly measuring a
fifteenth of the channel would be the same species of wrongness the entire week
exists to eliminate.

### Decision

One channel per pass, one page, round-robin:

> Of the **newest 15 messages Slack reports** for this channel, how many do we
> already hold?

Round-robin so one busy incident cannot starve the rest, with the cursor in
Valkey so a deploy does not reset it to the first channel forever. The newest
page is deliberately the sample frame: a live ingestion failure loses the most
recent messages first, so this is where the signal appears earliest.

The comparison is a **set intersection**, not a count comparison. Equal counts
over different timestamps — one message lost, one stored twice — is a real
failure and a count-only check would report it as healthy.

### Consequences

- The metric is an estimator with a sample size of 15 per channel per pass. It
  detects a *live* ingestion failure quickly and would **not** detect a single
  message lost three hours ago in a channel that has since moved on.
- The interface enforces the honesty: `HistoryReader` offers
  `recent_message_ts`, and deliberately has no `message_count`, so no caller can
  ask for a number the platform will not give.
- The budget is a Valkey key (`SET … NX EX 60`), not a local variable, because
  the limit counts the *app*: three replicas politely calling once a minute each
  is three calls a minute. It **fails closed** — an unreachable Valkey means the
  minute is treated as spent, since a skipped pass costs nothing and a 429 costs
  the next minute too.
- Marketplace approval, or a Slack policy change, would make a true count
  affordable. `history_budget_period_s` is configuration for that day, not a dial
  for making a demo look faster.

### Rejected alternatives

| Alternative | Why not |
|---|---|
| Page the full channel | Fourteen minutes per channel; the budget does not exist |
| Compare against our own `seen_count` | Circular — it measures our records against our records and cannot see a message we never received |
| Drop the metric | This is the SLI that catches the failure with no other symptom; dropping it is choosing not to know |

---

## Decision 2 — the reconciler archives orphan channels through `handlers.py`

### Context

W4-12 archives any `#inc-*` channel no incident row claims (B-08). Archiving is
an external write, and **INV-03** says external writes go through the outbox
relay.

They cannot here. `outbox_events.incident_id` is `NOT NULL` with a foreign key,
and an orphan channel is by definition one that **no incident row claims** —
there is nothing to hang the row off. The options were: relax the column to
nullable (a migration, in a week the plan gives no migration, to support one
cleanup path), invent a placeholder incident (a fake row in the table the whole
product is about), or move the call.

### Decision

The write stays inside `orchestration/handlers.py` — the one module permitted to
touch a chat adapter — as `ChatHandlers.archive_orphan_channel(channel_id)`, and
`orchestration/reconciler.py` calls that method directly rather than enqueuing an
outbox row.

### Consequences

- INV-03's actual guarantee is intact and still structurally enforced:
  `test_no_external_writes_outside_relay` scans the import graph and the call
  sites, and the reconciler holds no chat adapter of its own.
- What is given up is the outbox's raced-writer protection. That property does
  not apply here: `conversations.archive` is idempotent, and the reconciler is a
  single scheduled job rather than N workers competing over a queue.
- The orphan definition is "no incident row has ever recorded this channel", not
  "no incident is currently open". Archiving a closed incident's channel would
  delete the record people go back and read, which is the opposite of the point.
- `conversations.list` is Tier 2 and independent of the history limit, so the
  sweep does not spend the 1/min budget. Conflating the two would make the sweep
  as rare as the sampler for no reason.

If a second non-incident-scoped external write ever appears, the right move is a
nullable `incident_id` with a partial index and a migration that says why — not
a second exemption. One documented deviation is a decision; two is a pattern
nobody wrote down.
