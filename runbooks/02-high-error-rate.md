---
name: High Error Rate
alert_pattern: "HighErrorRate|ErrorBudgetBurn|Error5xxRate"
severity_filter: sev2
service_filter: null
version: 1.0.0
---

`{{service}}` is returning errors above its SLO. The error budget is burning; the
question is whether it is burning from a deploy, a dependency, or a traffic
shape you have not seen before.

<!-- step:open-dashboard -->
### Open the error panel and read the shape

`{{dashboard_url}}`

A step change points at a deploy or a config flip. A ramp points at saturation —
a pool filling, a cache warming down, a queue backing up. The shape narrows the
search more than any log line will.

<!-- step:check-recent-deploys -->
### Check what shipped

```bash
kubectl -n {{namespace}} rollout history deploy/{{service}} | tail -5
```

Line the deploy time up against the moment the graph moved. If they match within
a few minutes, stop investigating and roll back; you can read the diff after the
error rate is down.

<!-- step:check-breaker-state -->
### Check the circuit breakers

```bash
curl -sS http://{{service}}.{{namespace}}:8080/metrics | grep -E 'circuit_breaker|_open'
```

An open breaker means the errors are downstream and `{{service}}` is the
messenger. Chasing it here wastes the first ten minutes of the incident.

<!-- step:rollback -->
### Roll back

```bash
kubectl -n {{namespace}} rollout undo deploy/{{service}}
kubectl -n {{namespace}} rollout status deploy/{{service}} --timeout=120s
```

<!-- step:verify-recovery -->
### Verify recovery, and say so

Error rate back under the SLO threshold on `{{dashboard_url}}`, sustained for
five minutes rather than for one sample. Post the recovery in the channel — the
timeline needs the moment it stopped as much as the moment it started.
