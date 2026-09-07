---
name: Pod CrashLoopBackOff
alert_pattern: "PodCrashLoop|CrashLoopBackOff|ContainerRestarting"
severity_filter: sev2
service_filter: null
version: 1.0.0
---

A pod in `{{namespace}}` is restarting in a loop. The logs of the *previous*
container are where the answer is — the current one has usually not got far
enough to say anything.

<!-- step:get-logs -->
### Read the previous container's logs

```bash
kubectl -n {{namespace}} logs deploy/{{service}} --previous --tail=200
```

`--previous` is the whole trick. Without it you read the logs of a container that
crashed before it wrote anything useful, conclude there is nothing there, and go
looking somewhere else.

<!-- step:describe-pod -->
### Describe the pod

```bash
kubectl -n {{namespace}} describe pod -l app={{service}} | tail -40
```

Read `Last State`, `Reason` and `Exit Code`. Exit 137 is OOM, exit 1 with no logs
is usually a config parse failure at startup, and `CreateContainerConfigError` is
a missing secret or ConfigMap.

<!-- step:check-oom -->
### Check for OOM

```bash
kubectl -n {{namespace}} get pod -l app={{service}} \
  -o jsonpath='{.items[*].status.containerStatuses[*].lastState.terminated.reason}'
```

`OOMKilled` means raise the limit or fix the leak. Raising the limit is the
mitigation; deciding which of the two it was belongs in the PIR, not in the first
ten minutes.

<!-- step:check-config -->
### Check config and secrets

```bash
kubectl -n {{namespace}} get deploy/{{service}} -o yaml | grep -A5 -E 'envFrom|secretKeyRef'
```

A renamed key in a ConfigMap crashes every replica at once and looks exactly like
a bad image.

<!-- step:rollback-deployment -->
### Roll back

```bash
kubectl -n {{namespace}} rollout undo deploy/{{service}}
```

If the previous revision crashes too, the cause is outside the deployment — a
dependency, a secret rotation, a node problem — and the search moves with it.
