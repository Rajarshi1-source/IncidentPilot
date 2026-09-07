---
name: Disk Full
alert_pattern: "DiskFull|NodeDiskPressure|FilesystemAlmostOutOfSpace"
severity_filter: sev2
service_filter: null
version: 1.0.0
---

A filesystem is filling. Buy time first, then find the cause — a full disk on a
database node escalates from "alert" to "outage" in minutes, and the ordering
here reflects that.

<!-- step:check-usage -->
### Check where the pressure is

```bash
kubectl -n {{namespace}} exec deploy/{{service}} -- df -h
kubectl get nodes -o custom-columns=NAME:.metadata.name,PRESSURE:.status.conditions[?\(@.type==\"DiskPressure\"\)].status
```

Distinguish node disk from a PVC. They have different fixes and the alert usually
does not say which.

<!-- step:find-largest -->
### Find what is eating it

```bash
kubectl -n {{namespace}} exec deploy/{{service}} -- du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -20
```

The usual suspects, in order of frequency: unrotated logs, a core dump, a
retained WAL segment from a dead replication slot, and a temp directory nobody
cleans up.

<!-- step:rotate-logs -->
### Rotate logs

```bash
kubectl -n {{namespace}} exec deploy/{{service}} -- logrotate -f /etc/logrotate.conf
```

Buys time. It is not the fix, and saying so in the channel stops the incident
being closed on a mitigation.

<!-- step:clear-tmp -->
### Clear temporary files

Only files you can name. "Deleting things until the graph goes down" during an
incident is how a disk-full alert becomes a data-loss postmortem.

<!-- step:expand-or-evict -->
### Expand the volume, or evict

```bash
kubectl -n {{namespace}} patch pvc {{service}}-data \
  -p '{"spec":{"resources":{"requests":{"storage":"100Gi"}}}}'
```

Expansion needs a storage class that allows it; check before you promise it in
the channel. If it does not, evicting the workload to a node with room is the
faster path.
