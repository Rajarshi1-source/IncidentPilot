---
name: High Latency
alert_pattern: "HighLatency|LatencyP99High|SlowResponse"
severity_filter: sev2
service_filter: null
version: 1.0.0
---

`{{service}}` is slow but not erroring. Latency incidents are almost always
queueing somewhere — the work is to find which queue, not to restart things and
hope.

<!-- step:check-downstream -->
### Check downstream first

```bash
curl -sS http://{{service}}.{{namespace}}:8080/metrics \
  | grep -E 'client_request_duration|upstream_latency'
```

If a dependency's p99 moved before ours did, the incident is there. Compare the
two on `{{dashboard_url}}` before touching anything here.

<!-- step:check-conn-pool -->
### Check the connection pool

```bash
psql -h {{primary_host}} -c "SELECT count(*), state FROM pg_stat_activity \
  WHERE application_name LIKE '{{service}}%' GROUP BY state;"
```

A pool at its ceiling turns every request into a wait for a connection. That is
latency with no CPU and no errors, which is exactly the shape that sends people
looking in the wrong place.

<!-- step:check-cache-hit-rate -->
### Check the cache hit rate

A hit rate that dropped is a cause, not a symptom: an eviction storm or a cold
cache after a restart multiplies backend load without changing request volume.

<!-- step:identify-bottleneck -->
### Name the bottleneck before mitigating

Write it in the channel in one sentence — "connection pool saturated at 40, p99
tracks pool wait". Mitigating a bottleneck you have not named is how an incident
gets three mitigations and no explanation, and the PIR then has nothing to cite.

<!-- step:mitigate -->
### Mitigate

Scale the pool, scale the replicas, or shed load — in that order of preference,
because the first two are reversible and the third is visible to customers.

```bash
kubectl -n {{namespace}} scale deploy/{{service}} --replicas=6
```
