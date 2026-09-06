# ADR 0001 — Two pins from the plan's dependency set cannot be installed together

- **Status:** Accepted
- **Date:** 6 September 2026
- **Week:** 1
- **Supersedes:** the dependency list in the Detailed Implementation Plan §3.1

## Context

The plan's authoritative dependency set (§3.1, inherited from Rev 2 §C.1) pins
both:

```
redis==8.1.0        # Rev 2 §C.1: "Streams, consumer groups, XAUTOCLAIM"
arq==0.28.0         # Rev 2 §C.1: "Scheduled jobs (nudges, SLA timers, reconciliation)"
```

`uv lock` refuses to resolve:

```
Because arq==0.28.0 depends on redis[hiredis]>=4.2.0,<6 and your project
depends on arq==0.28.0, we can conclude that your project depends on
redis>=4.2.0,<6. And because your project depends on redis==8.1.0 [...]
your project's requirements are unsatisfiable.
```

Checked against PyPI: **arq 0.28.0 and redis 8.1.0 are both the current latest
releases.** This is not a stale or mistyped pin on either side — arq simply has
not caught up with redis 8, and its `<6` ceiling is three majors behind.

Both pins were individually verified against upstream when Rev 2 was written.
Neither was checked *against the other*, which is the failure mode a lockfile
exists to catch and why `uv.lock` is committed from week 1.

The same set also pins `presidio-analyzer==2.2.364`, which is not needed until
week 6 and pulls spacy plus a model download into every week-1 CI run.

## Decision

**Remove `arq` and `presidio-analyzer` from the dependency set for now.** Both
are recorded as deferred in `bot/pyproject.toml` with a pointer to this ADR.

- `arq` is used only by `orchestration/scheduler.py`, which is a **week 5**
  deliverable. Nothing in weeks 1–4 needs it.
- `presidio-analyzer` is used only by `privacy/redactor.py`, a **week 6**
  deliverable.

`redis==8.1.0` stays. Downgrading three majors to accommodate a scheduler
library would be the tail wagging the dog: Streams, consumer groups and
`XAUTOCLAIM` are the ingest and orchestration substrate, and redis-py 8.x is
current.

## Consequences

The week 5 scheduler needs a transport decision that this ADR deliberately does
**not** make now, because making it in week 1 would be deciding week 5's
architecture on a dependency-resolution error rather than on its merits. The
options, for the record:

1. **Kubernetes CronJobs.** The Helm chart already has a `cronjobs.yaml` slot in
   the plan's file manifest, and the scheduled work is cron-shaped: reconciler
   every 15 min, SLA nudges every 5 min, abandonment sweep hourly. Removes the
   dependency entirely. Costs a compose-side equivalent for the local demo.
2. **A plain asyncio loop in the worker**, driven by the existing stream
   consumer's tick. ~60 lines, no new dependency, no new failure mode.
3. **Keep arq, downgrade redis to 5.x.** Rejected above.
4. **Wait for arq to support redis 8.** Not a plan.

Options 1 and 2 are complementary — CronJobs in Kubernetes, the asyncio loop for
`docker compose up` — and that pairing is the likely answer. Decided in week 5.

## What this changes about how the pins are treated

Rev 2 §C states every pin was "checked against the upstream release feed or
package index today." That was true and it was still insufficient: a version
matrix verifies each row and cannot see a conflict between two rows. The
lockfile is the only thing that checks the set as a set.

Practical consequence for the remaining weeks: **add a dependency by editing
`pyproject.toml` and running `uv lock`, never by trusting the plan's table.**
The table says what to aim for; the resolver says what is possible.

## Related

- Detailed Implementation Plan §3.1 (the dependency set), §3.3 (reproducibility)
- Rev 2 §C.1 (version matrix), §B B-14 (version drift and lockfiles)
