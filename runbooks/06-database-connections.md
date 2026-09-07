---
name: Database Connection Issues
alert_pattern: "Postgres.*|DatabaseConn.*|ConnectionPoolExhausted"
severity_filter: sev1
service_filter: null
version: 1.0.0
---

Connections to the database are failing or exhausted. Almost every incident of
this shape is one of three things: too many clients, one query holding locks, or
a failover that half-happened.

<!-- step:check-pool-stats -->
### Check the pool

```bash
psql -h {{primary_host}} -c "SELECT count(*), state, application_name \
  FROM pg_stat_activity GROUP BY state, application_name ORDER BY 1 DESC;"
psql -h {{primary_host}} -c "SHOW max_connections;"
```

A wall of `idle in transaction` is an application bug leaking transactions, not a
database that needs more connections. Raising `max_connections` there makes the
next occurrence worse.

<!-- step:check-active-queries -->
### Find the query holding everything up

```bash
psql -h {{primary_host}} -c "SELECT pid, now()-query_start AS runtime, wait_event_type, \
  left(query, 120) FROM pg_stat_activity WHERE state <> 'idle' \
  ORDER BY runtime DESC LIMIT 10;"
```

One query at the top of that list, ten minutes old, holding a lock, is the whole
incident more often than not. `pg_cancel_backend(pid)` before
`pg_terminate_backend(pid)` — cancel is polite and usually enough.

<!-- step:check-replication -->
### Check replication state

```bash
psql -h {{primary_host}} -c "SELECT client_addr, state, sync_state, \
  pg_wal_lsn_diff(sent_lsn, replay_lsn) AS bytes_behind FROM pg_stat_replication;"
```

If this points at lag rather than connections, switch to the **Replica Lag /
Promotion** runbook and say so in the channel so the timeline records the pivot.

<!-- step:failover-decision -->
### Decide about failover, out loud

State the decision and the reason in-channel before acting. Failover is a
one-way door and the PIR will be asked who decided and on what basis; a decision
made in someone's head is a decision nobody can review.

<!-- step:escalate-dba -->
### Escalate to the database on-call

Use `/escalate` rather than a DM. The database rota is two people deep, which
means the fatigue score is usually already high there — routing will say who it
picked and why, and that record is the point.
