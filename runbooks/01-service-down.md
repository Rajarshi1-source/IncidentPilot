---
name: Service Down
alert_pattern: "ServiceDown|InstanceDown|UpProbeFailed"
severity_filter: sev1
service_filter: null
version: 1.0.0
---

`{{service}}` is not answering its health probe. Work outward from the service
itself: most "service down" alerts are a bad deploy, and the second most common
cause is a dependency that is actually the incident.

<!-- step:verify-health -->
### Verify it is really down

```bash
kubectl -n {{namespace}} get pods -l app={{service}}
curl -sS -o /dev/null -w '%{http_code}\n' http://{{service}}.{{namespace}}:8080/healthz
```

A probe failing from Prometheus but succeeding from inside the cluster is a
network-policy or DNS incident, not a service incident — and the runbook you want
is a different one.

<!-- step:check-recent-deploys -->
### Check what shipped

```bash
kubectl -n {{namespace}} rollout history deploy/{{service}} | tail -5
```

Anything in the last hour is the prime suspect. Say the SHA in the channel: the
deploy webhook makes it citable evidence in the PIR, and "deployed 12 minutes
before detection" is the single most useful sentence a postmortem can contain.

<!-- step:check-dependencies -->
### Check the dependencies before blaming the service

```bash
kubectl -n {{namespace}} logs deploy/{{service}} --tail=100 | grep -iE 'refused|timeout|dns'
```

If IncidentPilot already correlated other alerts into this incident, read the
root signal on the pinned message first — it is chosen as the first alert on the
deepest dependency, which is usually the thing to fix.

<!-- step:restart-or-rollback -->
### Restart, or roll back

Restart only if you have a reason to believe the process is wedged; otherwise
roll back, because a restart of bad code produces bad code again three minutes
later and costs you the evidence.

```bash
kubectl -n {{namespace}} rollout undo deploy/{{service}}
```

<!-- step:escalate -->
### Escalate if it is still down

Ten minutes without a working hypothesis is the escalation threshold. Use
`/escalate` so the severity change and its reason are recorded rather than
happening in a DM.
