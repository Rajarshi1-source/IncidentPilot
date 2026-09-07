# ADR 0006 — What replay can and cannot measure

**Status:** accepted · **Date:** 2026-09-07 · **Week:** 7 · **Supersedes:** nothing

## Context

Week 7 builds the replay harness and the CI eval gate. The acceptance criterion
is stated as: *change one word in `pir_v2_1_0.md` without updating the baseline,
open a PR, and CI goes red on `eval-gate` with the sticky comment showing which
metric moved.*

Building it surfaced a problem with the second half of that sentence.

## The problem

Replay returns **recorded** model responses. That is what makes forty incidents
score in under two seconds with zero network calls and zero cost, and it is what
makes the whole thing usable as a merge gate.

But it means a prompt edit **cannot** change the model output the harness scores.
The prompt is an input to a process that already ran. If the harness scored the
recorded responses and reported `timeline_f1: 0.967` after the edit, that number
would be true and completely uninformative — it would be the F1 of the *old*
prompt, presented as though it were the new one's.

That is the same category of defect as B-11 itself. Rev 1's eval job never
executed and everyone believed there was a gate; a harness that scores a stale
corpus after a prompt change reports a number about something it did not
measure. The first is a gate that never runs. The second is worse: a gate that
runs and lies.

## Decision

**Replay does not pretend to evaluate a prompt change. It detects that it can no
longer evaluate, and fails the gate on that.**

`prompt_drift` is a HARD gate row alongside `citation_precision`,
`uncited_claims` and `fabricated_citations`. It counts fixtures whose recorded
`prompt_sha256` no longer matches the working tree's prompt. Two paths reach it:

- The prompt registry raises `PromptIntegrityError` when a prompt file no longer
  matches its `registry.yaml` entry — the common case, and the one G7
  demonstrates. Caught in `Replayer.current_prompt()` and reported as drift
  rather than as forty crashes.
- The hash comparison catches an edit that *did* update the registry, where file
  and entry agree with each other and neither agrees with the corpus. This is
  the more dangerous of the two, precisely because everything looks consistent.

The report names the prompt and both hashes, and says what to do: re-record with
`--live`, or revert.

### What this buys

The gate goes red on a one-word prompt change, deterministically, with a
readable reason, in under two seconds, offline. G7's screenshot is real.

More importantly the mechanism is *honest about its own limits*, which is a
better thing to defend in a review than a fabricated quality delta. "My harness
tells me when it has stopped being able to answer the question" is a stronger
claim than "my harness scores prompt changes", because the second one is not
true of any replay harness and an interviewer who has built one will know it.

### What this costs

Answering "is the new prompt *better*?" requires calling real providers. That is
`--live`, it is a manual procedure with a credential, and it is deliberately not
something CI can do — a merge gate that spends money and varies run to run is a
merge gate people route around. The workflow is:

1. Edit the prompt, bump the version and hash in `registry.yaml`.
2. `python -m eval.run --corpus eval/corpus --live` to re-record.
3. Read the numbers. If they hold, `eval.gate.promote()` moves the baseline.
4. Commit the refreshed corpus *and* the new baseline in the same PR.

Step 4 is what makes the diff reviewable: a prompt change arrives with the
evidence for it attached.

## Two gate rows the reference did not list

Both were added after the harness caught something the reference's list would
not have.

### `unexpected_skeleton`

The first working version of the corpus reported `GATE: PASS (11/11)` while
eleven fixtures had silently degraded to the skeleton. Every quality metric had
stopped measuring rather than started failing: a skeleton has no citations to be
imprecise about and no claims to leave uncited, so `citation_precision` read
1.000 on a corpus that produced almost no cited claims.

The recording says which fixtures are *supposed* to reach layer 3 — three do,
because the provider-failure bucket exists to prove the skeleton ships when
every vendor is gone. Everything else reaching it is now a hard failure.

This is the harness's own listed failure mode ("everything degrades to skeleton
in replay", usually a validator threshold that got stricter), and it is the row
that makes every other row mean something.

### `network_calls` and `replay_errors`

INV-10 as a gate row rather than a comment, and "a fixture that raised is not a
fixture that passed". Both are counted by the run itself, and `gate_g7.sh` reads
the socket count out of the run's JSON rather than grepping a log line — a log
line can be printed by code that never checked anything.

## The other thing replay must not measure: itself

`p95_generation_ms` is computed from the **recorded** latencies, not from how
fast the harness replays them. The first version timed itself and reported a
3 ms p95 against a 30 s budget: perfectly green, and silent on whether the
product meets its SLO. `test_replay_is_deterministic` compares `generation_ms`
across three runs for exactly this reason — a self-timed metric is the one field
that cannot be deterministic, so including it in the comparison is what forced
the fix.

The same reasoning excludes wall-clock time from the determinism assertion: it
measures the harness, not the system.

## Why the replay runs entirely in memory

No Postgres, no Valkey, no session. A database connection **is** a socket, and
the socket guard would trip on it — so honouring INV-10 and querying a database
are mutually exclusive.

This is a genuine limitation and worth stating: the harness measures correlation,
intent classification, context assembly, impact injection, the validator, the
fallback ladder and the citation grammar. It does **not** measure the persistence
layer, the outbox, or the state machine's interaction with real constraints.
Those are covered by the integration suite against a real Postgres (G2–G6), and
the split is deliberate — different failures, different tools.

The seam that makes it possible is one constructor argument:
`PIRGenerator(context_loader=...)`. Production loads the context from six
queries; the harness assembles the identical object from a recording. A
constructor argument rather than a subclass or a patched module, for the same
reason `now` is one: the injected shape is the shape the tests already exercise,
so the seam is not a special path that only the harness walks.

## On the corpus being authored rather than captured

Forty fixtures, written by hand from the shapes real incidents take. There is no
year of production traffic behind this project and a corpus claiming otherwise
would be the same category of lie as a gate that never runs. The corpus README
says so, the report footer says the corpus is small and why, and four buckets
exist specifically to be failed:

- `code_switched` scores 0.667 on timeline F1, because the intent rules were
  written in English and a half-Hindi channel is normal on an Indian team. It is
  measured **while it fails**, which is what will prove the fix when layer 2 of
  the classifier ladder is tuned.
- `flapping` and `abandoned` must produce **no PIR at all**.
- `unknown_root_cause` requires the model to abstain; `root_cause_hypothesis` is
  nullable in the schema precisely so that abstention is representable.

An aggregate that hid the code-switched bucket would be a metric that lies, which
is why the report leads with per-bucket numbers.

## On E-1, still unsettled

`models.yaml` still ships GPT-5.4-mini as the committed `synthesize` default.
Week 7 owed the decision a *mechanism*, not a verdict, and the mechanism is here:

```
python -m eval.run --corpus eval/corpus \
  --set roles.synthesize.model=<candidate> \
  --set roles.synthesize.input_usd_per_mtok=<rate> \
  --set roles.synthesize.output_usd_per_mtok=<rate>
```

Rates travel with the model deliberately. Overriding the name alone changes what
the report *says* was used without changing what it cost, and a cost comparison
between two models priced identically is not a comparison.

What the counterfactual can settle today is **cost**, exactly, on this project's
own fixtures. What it cannot settle without `--live` is **quality**, for the
reason this whole ADR is about. So the honest statement of where E-1 stands is:
the machinery to answer it exists, the cost half is answerable offline, and the
quality half requires one deliberate `--live` run against the candidate. That is
a smaller and more truthful claim than "the harness settles E-1", and it is the
one the code supports.

`model_deployments` (migration 0008) is where the answer lands when it is made:
the promotion ladder, the traffic share, who promoted it, and the eval run that
justified it. The `ck_model_deployments_default_needs_evidence` constraint means
a configuration cannot become the default without an eval run attached —
enforced by the schema rather than by a runbook, because the schema is the one
that is awake at 2 a.m.

## The counterfactual that sells D3

Worth recording because it is the number, not the claim:

```
$ python -m eval.run --corpus eval/corpus --incident 215 --verbose
    alerts 40 -> incidents 1   root=f7140e30...

$ python -m eval.run --corpus eval/corpus --incident 215 \
    --set correlation.merge_threshold=0.99 --verbose
    alerts 40 -> incidents 38  root=f7140e30...
```

Disabling correlation turns the 40-alert cascade into 38 war rooms. That is the
sentence that sells the feature, and it took nine seconds and no argument.

## Consequences

- A prompt change cannot merge without either a corpus refresh or a revert. That
  is friction, and it is the intended friction: a prompt is an artifact and
  artifacts have tests.
- The corpus must be regenerated when the impact queries or the evidence grammar
  change. `eval/corpus/_generate.py` is committed alongside its output so that
  is a command (`make eval-record`) rather than an archaeology project.
- `--live` is the only path to a genuine quality comparison, and it is a manual
  procedure. Anyone reading the gate should know that; the report footer says it.
