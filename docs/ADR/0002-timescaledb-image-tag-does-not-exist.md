# ADR 0002 — The pinned TimescaleDB image tag does not exist

- **Status:** Accepted
- **Date:** 6 September 2026
- **Week:** 2
- **Supersedes:** the container pin in Rev 2 §C.3 and Detailed Implementation Plan §3.2

## Context

Every source pins the database image as:

```
timescale/timescaledb-ha:pg18.6-ts2.29.2
```

It does not exist, and never did:

```
$ docker run timescale/timescaledb-ha:pg18.6-ts2.29.2
docker: Error response from daemon: failed to resolve reference
"docker.io/timescale/timescaledb-ha:pg18.6-ts2.29.2": not found
```

Querying the registry, `timescale/timescaledb-ha` publishes 166 `pg18*` tags and
the newest PostgreSQL patch among them is **18.4**:

```
pg18.4-ts2.29.2        <- the real one
pg18.4-ts2.29.2-oss
pg18-ts2.29
pg18
```

Both halves of the plan's pin were verified independently and correctly:
PostgreSQL 18.6 *is* the current release (13 Aug 2026), and TimescaleDB 2.29.2
*is* current (18 Aug 2026). The pin was then formed by concatenating them. But
**the TimescaleDB HA image lags the PostgreSQL patch stream** — Timescale
rebuilds on their own cadence, so a PG patch released three weeks ago has no
corresponding image yet.

This is the same failure mode as ADR 0001, one layer down: each component
verified in isolation, the *combination* never checked against the thing that
actually has to resolve it.

## Why it matters more than a typo

The repository's single most valuable property is that a stranger can clone it
and run the full demo with no credentials (conflict C-02, and the reason
`IP_CHAT_PROVIDER` defaults to `fake`). A non-existent image tag means
`docker compose up` fails on the first pull with `not found`.

Nothing in the test suite would have caught it. The unit tests use an in-memory
fake; the integration tests skip when no database is reachable — and "not
reachable" is exactly what a failed pull looks like. It surfaces the first time
someone runs the demo, which in a portfolio project means it surfaces in front of
an interviewer.

## Decision

Pin **`timescale/timescaledb-ha:pg18.4-ts2.29.2`**.

TimescaleDB 2.29.2 is unchanged and is the part that matters: 2.29.0 dropped
PG15, and 2.29.1 fixed three security advisories, so the floor stays at 2.29.1.
What moves is the PostgreSQL patch level, 18.6 → 18.4, which is what the image
actually ships.

Updated in `docker-compose.yml`, `.github/workflows/ci.yml`, the integration test
fixtures, and §3.2 of the Detailed Implementation Plan.

## Consequences

**Say the right thing in an interview.** "PostgreSQL 18.6" is now wrong for the
deployed artifact. The accurate sentence is: *"PostgreSQL 18.4 with TimescaleDB
2.29.2 — the extension version is what I care about, and the HA image lags the
PostgreSQL patch stream by a few weeks, so 18.4 is the newest base Timescale has
built against 2.29.2."* That is a better answer than the original, because it
shows you know why the two version numbers are not independent.

**Do not chase the PG patch.** Moving to 18.6 means either waiting for Timescale
to publish it, or building a custom image, or dropping the HA image for
`postgres:18.6` plus a hand-installed extension. None of those is worth it: the
delta from 18.4 to 18.6 is two patch releases of a database holding thirty rows a
day.

## The general rule this produces

A version matrix verifies each row and cannot see a conflict *between* rows or a
combination that no artifact exists for. Two mechanisms now cover that gap:

| Layer | Mechanism | Catches |
|---|---|---|
| Python dependencies | `uv.lock`, committed | ADR 0001's arq/redis conflict |
| Container images | `docker compose pull` in CI | this |

CI gains a `docker compose pull` step so a nonexistent tag fails a build rather
than a demo.

## Related

- ADR 0001 — dependency pin conflicts (same failure mode, Python layer)
- Rev 2 §C.3 (container pins), §B B-14 (version drift and `latest` tags)
