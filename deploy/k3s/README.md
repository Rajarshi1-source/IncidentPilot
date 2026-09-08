# Deploying to k3s (W8-18)

The chart in `charts/incidentpilot` is the deployment. This directory holds the
two things it deliberately does **not** contain: the datastores, and the Secrets.

## Why the secrets are here as a command, not a file

There is no `secrets.yaml` in the chart and there never will be. A chart that
templates Secret objects invites a `values-prod.yaml` with real credentials in
it, and that file reaches git within about two weeks. The chart *names* the
Secrets it reads; something out of band creates them.

For a real cluster that is External Secrets or sealed-secrets. For a single-node
k3s demo box it is six `kubectl create secret` commands, run once, by a human,
and never written down:

```bash
kubectl create namespace incidentpilot

kubectl -n incidentpilot create secret generic incidentpilot-database \
  --from-literal=url='postgresql+psycopg://ip:REDACTED@postgres:5432/incidentpilot'
kubectl -n incidentpilot create secret generic incidentpilot-valkey \
  --from-literal=url='redis://valkey:6379/0'
kubectl -n incidentpilot create secret generic incidentpilot-slack \
  --from-literal=signingSecret='REDACTED' --from-literal=botToken='xoxb-REDACTED'
kubectl -n incidentpilot create secret generic incidentpilot-alertmanager \
  --from-literal=bearer="$(openssl rand -hex 32)"
kubectl -n incidentpilot create secret generic incidentpilot-paging \
  --from-literal=webhookSecret="$(openssl rand -hex 32)"

# The lifeboat's webhook must NOT be a Slack app this deployment owns. If the
# only path to learning IncidentPilot is down runs through IncidentPilot, the
# lifeboat is decorative.
kubectl -n incidentpilot create secret generic incidentpilot-lifeboat \
  --from-literal=url='https://hooks.slack.com/services/REDACTED'
```

## The datastores

`postgres.yaml` and `valkey.yaml` here are a **single-node demo** deployment:
one replica each, a PVC, no replication, no backup. That is stated plainly
rather than implied, because a StatefulSet in a repository looks like a
production database until somebody loses one.

For anything real, use a managed PostgreSQL with TimescaleDB (or a
Timescale-managed instance) and a managed Valkey/Redis. The application does not
care — `IP_DATABASE_URL` and `IP_VALKEY_URL` are the entire coupling.

## Install

```bash
# 1. Datastores first: the migration Job in the chart needs them.
kubectl -n incidentpilot apply -f deploy/k3s/postgres.yaml
kubectl -n incidentpilot apply -f deploy/k3s/valkey.yaml
kubectl -n incidentpilot rollout status statefulset/postgres --timeout=180s

# 2. Schema and reference data. Run BEFORE the app: correlation reads
#    services.graph_depth, and an empty services table turns a 40-alert cascade
#    into 36 incidents.
kubectl -n incidentpilot apply -f deploy/k3s/migrate-job.yaml
kubectl -n incidentpilot wait --for=condition=complete job/incidentpilot-migrate --timeout=300s

# 3. The application.
helm upgrade --install incidentpilot charts/incidentpilot \
  --namespace incidentpilot \
  --set image.tag=0.8.0 --set dashboard.tag=0.8.0 \
  --atomic --timeout 5m --wait

# 4. Prove it, from outside.
kubectl -n incidentpilot port-forward svc/incidentpilot-incidentpilot-api 18000:8000 &
IP_API_URL=http://localhost:18000 bash scripts/smoke.sh --expect-channel --timeout 120
```

`--atomic` reverts a failed *release*, which is a statement about Helm. Step 4 is
what makes it a statement about the product — the release workflow runs the same
smoke test and rolls back on its failure, not only on Helm's.

## Ingress

Left to the cluster. k3s ships Traefik; a two-line IngressRoute pointing at
`incidentpilot-incidentpilot-dashboard:3000` is all the public URL needs. The
chart does not template one, because an Ingress is where TLS, hostnames and
auth policy live, and those are properties of a deployment rather than of an
application.

**Set `dashboard.demoMode=true` on anything publicly reachable.** It forces the
chat and paging adapters onto the in-memory fakes, so the deployment cannot
create a channel in a real workspace or page a real human — enforced by the
factories rather than by a flag somebody remembers to check.
