# ADR 0005 — Grounding the PIR: what the model is and is not allowed to do

- **Status:** Accepted
- **Date:** 7 September 2026
- **Week:** 6
- **Refines:** Detailed Implementation Plan §10, INV-05/06/07, B-09, B-10, D1, D6, E-1

Week 6 is the flagship, and most of its decisions are about **removing**
capability from the model rather than adding it. This records the ones the plan
implies but does not settle, and two bugs the gate found in the code that was
supposed to enforce it.

---

## Decision 1 — E-1 stays unsettled, deliberately

The committed default for `synthesize` remains **GPT-5.4-mini**, as Rev 2 §D.2
specifies, for consistency with the other capstones. Rev 2 also flags that a
current-generation small model now sits roughly 4× below it on price.

**That argument is not settled here, and settling it here would be the mistake.**
The whole of INV-07 exists so that the answer can be produced by evidence rather
than by argument: week 7 replays this project's own golden corpus with
`--set roles.synthesize.model=<candidate>` and compares quality and cost on
fixtures from this system. Promoting a model on a price list is how you end up
defending a number you did not measure — which is exactly the follow-up question
an interviewer asks.

What week 6 owes that experiment is the *mechanism*, and it delivers it:
`config/models.yaml` is the only place a model name appears, every role resolves
through the router, and a CI grep plus a unit test both fail the build on a
model string anywhere under `src/` outside `config/`.

---

## Decision 2 — the validator's support check needed a third signal

`_supports()` was specified as "normalized token overlap plus shared entities".
Built exactly that way, it rejected a correct claim in the very first
end-to-end run: *"A config change deployed to checkout preceded the errors."*
citing `deploy:a1b2c3d4e5f6`.

The overlap was 0.17 against a threshold of 0.35, and the entity regex — which
looks for hyphens, version numbers and SHAs — never matched, because in this
domain the identifying words are **single tokens**: `checkout`, `payments`,
`HighErrorRate`.

So `GroundedContext.strong_terms` was added: the service names, alert names and
runbook step ids belonging to *this* incident. A claim and its citation sharing
one of those counts as support.

The direction of the fix is the point. A validator that is too strict pushes
honest drafts to the skeleton and **quietly kills the feature**, and it does so
in a way that looks like the model being bad rather than the gate being wrong.
That failure mode is worse than the one it guards against, and it is why the
threshold is tuned on the corpus in week 7 with its false-rejection rate
reported rather than assumed.

---

## Decision 3 — the skeleton reports coverage 0.0, not 1.0

`ip_pir_citation_coverage` has a zero error budget. Tempting, then, to record
1.000 for a skeleton — it contains no uncited claims, after all.

It records **0.0**. The skeleton makes no cited claims at all, and a document
with no claims is not a document with perfect grounding. Reporting 1.000 would
make the one SLI with a zero error budget look healthy on exactly the days the
feature was unavailable, which is the specific kind of dishonest instrumentation
this project keeps writing tests against.

---

## Decision 4 — deploy events needed a table (migration 0007)

The evidence grammar names six citable kinds. Five had a home; `deploy:{sha}`
had only "deploy webhook events" in §10.2, which is not a place. A citation kind
with nowhere to resolve from is a kind the validator must reject on every draft
— and "deployed twelve minutes before detection" is the single most useful
sentence a postmortem can contain.

Two details on that table:

- **`UNIQUE (sha, service, environment)`.** CI retries a failed notification and
  the same commit ships to staging and production. Two rows would make one
  deploy citable under two ids, half of which would then look fabricated.
- **The repository allowlist is doing more work than the bearer.** A forged
  deploy row makes a citation **valid**, and nothing downstream can catch it —
  the validator's whole job is to check that cited evidence exists, and forged
  evidence exists. `assert_invariants` refuses a production config that sets a
  deploy bearer without an allowlist.

---

## Two bugs the gate found, both invisible to the tests that existed

### The budget breaker was never awaited

`LLMRouter.complete` called `self._budget.check(...)` without `await`. That
builds a coroutine, discards it, and proceeds. **The cost control was a no-op**,
and every direct unit test of `BudgetBreaker` passed because they call it
themselves.

What caught it was an integration assertion phrased as a *consequence* rather
than a behaviour — `assert provider.calls == 0, "the budget was checked before
anything was sent"` — which is only false if the check actually fired. A test
that asserted `BudgetExceeded` was raised somewhere would have passed too.

### The tokenizer glued trailing punctuation onto words

`[a-z0-9][a-z0-9._/-]*` is right for `checkout-api` and `v2.1.0` and wrong at
the end of a sentence: *"...on checkout."* tokenized as `checkout.`, matching
neither the cited message nor the service name. Every claim that happened to end
on the word that would have supported it was rejected as unsupported.

Both bugs share a shape worth naming: the component was correct in isolation and
wrong in composition, and only a test that drove the real path found it. That is
the third time this project has hit that shape — after `app.state.channels` in
week 5 and the integration suite's schema dependency — and it is the argument
for the gates being end-to-end rather than a summary of unit results.

---

## What the model is not allowed to do, restated

| Removed capability | How | Guarded by |
|---|---|---|
| Produce an uncited claim | `Claim.citations` has `min_length=1` | parse failure, `test_uncited_claim_fails_parse` |
| Produce an impact number | `impact` absent from `PIRDraft`; computed and injected | `test_impact_is_absent_from_the_schema` |
| State a number in prose | validator checks every literal against the computed set | `test_ungrounded_number_rejected` |
| Cite something that does not exist | `valid_reference_set()` membership | `test_fabricated_citation_is_rejected` |
| Assign work to a stranger | owner ∈ participants | `test_an_action_item_owner_must_have_been_there` |
| Gate on its own confidence | `reported_confidence` recorded, never branched on | B-09, by construction |
| See a customer's email | redaction at the router, before egress | `test_no_raw_pii_reaches_provider` |
| Block the product | every path terminates in the skeleton | G6, first run |
