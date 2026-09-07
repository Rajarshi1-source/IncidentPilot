---
name: Replica Lag / Promotion
alert_pattern: "ReplicaLag|ReplicationDelay|PostgresReplicationLag"
severity_filter: sev2
service_filter: null
version: 1.2.0
---

Replication has fallen behind far enough that read traffic is serving stale data,
or the primary is at risk. Promotion is a one-way door — work the first three
steps before you take it, because a promotion done on a lag spike that was about
to clear costs a resync and a second incident.

<!-- step:verify-replica-lag -->
### Verify the lag is real and current

```bash
psql -h {{primary_host}} -c "SELECT now() - pg_last_xact_replay_timestamp() AS lag;"
```

Expect under 5s. Between 5s and 60s, continue to the next step — this is usually
a load spike that clears. Over 60s and still climbing, treat promotion as likely
and keep going quickly.

A lag figure from a dashboard is not enough here: the panel may be five minutes
stale, which is the same order as the number you are reading off it.

<!-- step:check-wal-backlog -->
### Check the WAL backlog on the primary

```bash
psql -h {{primary_host}} -c "SELECT slot_name, active, \
  pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained \
  FROM pg_replication_slots;"
```

An inactive slot retaining tens of gigabytes is a different incident: the replica
is gone and the primary is filling its disk. That is a **DiskFull** problem
wearing a replication costume, and dropping the slot is the fix, not promotion.

<!-- step:check-primary-load -->
### Check whether the primary is simply overloaded

```bash
psql -h {{primary_host}} -c "SELECT count(*), state FROM pg_stat_activity \
  GROUP BY state ORDER BY 1 DESC;"
```

Look at `{{dashboard_url}}` for write throughput over the last hour. Lag caused by
a write burst clears on its own; lag caused by a stuck replica does not. If a
long-running transaction is holding things up, killing that query is a far
cheaper mitigation than a failover.

<!-- step:promote-replica -->
### Promote the replica

The one-way door. Announce it in-channel before you run it, because a second
person promoting a second replica during the same incident is a split brain that
takes a day to unpick.

```bash
kubectl -n {{namespace}} exec sts/{{service}}-replica-0 -- pg_ctl promote
kubectl -n {{namespace}} annotate svc {{service}}-primary \
  incidentpilot.io/promoted-at="$(date -Is)" --overwrite
```

Then repoint the writer service and confirm exactly one primary is accepting
writes.

<!-- step:verify-recovery -->
### Verify recovery

```bash
psql -h {{primary_host}} -c "SELECT pg_is_in_recovery();"
```

`f` on the promoted node, and error rate back at baseline on `{{dashboard_url}}`.
Say so in the channel: "replica promoted, lag recovered" is the line the PIR will
cite, and a recovery nobody wrote down is a recovery the timeline cannot show.
