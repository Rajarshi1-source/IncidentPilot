#!/usr/bin/env bash
# Drive the demo (W8-14).
#
#   ./scripts/demo.sh storm   --alerts 40 --service payments
#   ./scripts/demo.sh chatter --incident 1
#   ./scripts/demo.sh resolve --incident 1
#
# Everything here goes through the **public HTTP surface** -- the same webhooks
# Alertmanager and Slack call. Nothing reaches into the database, and that is
# the point rather than a limitation: a demo that seeds rows directly proves the
# schema renders, not that the system works. If ingest, correlation, the outbox
# or the relay is broken, this script fails, which is exactly what G8 needs it
# to do.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${IP_API_URL:-http://localhost:${IP_PORT_API:-18000}}"
BEARER="${ALERTMANAGER_BEARER:-local-dev-token}"
SIGNING_SECRET="${SLACK_SIGNING_SECRET:-dev-not-a-real-secret}"

CMD="${1:-}"; shift || true

ALERTS=40
SERVICE="payments"
INCIDENT=1
MESSAGES=12

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alerts)   ALERTS="$2"; shift 2 ;;
    --service)  SERVICE="$2"; shift 2 ;;
    --incident) INCIDENT="$2"; shift 2 ;;
    --messages) MESSAGES="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

die() { printf '\033[31mdemo: %s\033[0m\n' "$1" >&2; exit 1; }
say() { printf '  %s\n' "$1"; }

wait_for_api() {
  for _ in $(seq 1 60); do
    if curl -fsS "$API/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  die "the API at $API never became healthy"
}

# --- storm --------------------------------------------------------------------
#
# One Alertmanager POST carrying N alerts from a real cascade, NOT N copies of
# one alert. Forty identical alerts would correlate on label overlap alone and
# the dependency-graph signal -- the actual differentiator -- would never be
# exercised. The fixture is the same table the G2 gate and the eval corpus use,
# so all three measure the same cascade.
demo_storm() {
  wait_for_api
  say "posting $ALERTS correlated alerts to $API/webhooks/alertmanager"

  local payload
  payload="$(python - "$ROOT" "$ALERTS" "$SERVICE" <<'PY'
import json, sys, pathlib
root, count, service = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root / "bot"))
from tests.fixtures.make_storm import build
body = build(count)
# Re-label onto the requested service family so `--service` means something.
# The cascade's shape (which service depends on which) is what correlation
# reasons about, so only the namespace is rewritten, never the service graph.
for alert in body["alerts"]:
    alert["labels"]["namespace"] = service
print(json.dumps(body))
PY
)" || die "could not build the storm fixture"

  local code
  code="$(printf '%s' "$payload" | curl -sS -o /tmp/ip-demo-storm.json -w '%{http_code}' \
    -X POST "$API/webhooks/alertmanager" \
    -H "Authorization: Bearer $BEARER" \
    -H 'Content-Type: application/json' \
    --data-binary @-)" || die "the webhook call failed"

  [[ "$code" == "202" ]] || die "expected 202 from the webhook, got $code ($(cat /tmp/ip-demo-storm.json))"
  say "accepted: $(cat /tmp/ip-demo-storm.json)"
  say "correlation and channel creation happen in the worker; smoke.sh waits for them"
}

# --- chatter ------------------------------------------------------------------
#
# Slack events, signed the way Slack signs them. The signature is computed here
# rather than skipped because the trust boundary is a real one: an unsigned
# event is rejected, and a demo that bypassed it would be demonstrating a
# different system.
demo_chatter() {
  wait_for_api
  local channel
  channel="$(python - "$API" "$INCIDENT" <<'PY'
import json, sys, urllib.request
api, incident = sys.argv[1], int(sys.argv[2])
with urllib.request.urlopen(f"{api}/api/incidents/active", timeout=10) as r:
    body = json.load(r)
rows = body.get("incidents") or []
if not rows:
    raise SystemExit("no active incident to talk in -- run `demo.sh storm` first")
row = rows[min(incident - 1, len(rows) - 1)]
print(row.get("chat_channel_id") or "")
PY
)" || die "could not resolve the incident channel"

  [[ -n "$channel" ]] || die "the incident has no channel yet -- the relay may not have run"
  say "posting $MESSAGES messages into $channel"

  python - "$API" "$SIGNING_SECRET" "$channel" "$MESSAGES" <<'PY'
import hashlib, hmac, json, sys, time, urllib.request

api, secret, channel, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

# A real incident's shape: someone looks, someone acts, someone confirms. The
# intent classifier turns these into timeline rows, and the PIR cites them --
# so the demo transcript has to contain the words a real one would.
SCRIPT = [
    ("U_PRIMARY", "looking at the postgres-primary dashboard now"),
    ("U_PRIMARY", "connection pool is saturated, checking the replica"),
    ("U_SECOND",  "can someone check whether the 03:10 deploy is related"),
    ("U_PRIMARY", "promoting the replica now"),
    ("U_SECOND",  "watching the error rate"),
    ("U_PRIMARY", "promotion complete"),
    ("U_SECOND",  "error rate is dropping"),
    ("U_PRIMARY", "payments-api looks healthy again"),
    ("U_SECOND",  "checkout is back to normal"),
    ("U_PRIMARY", "traffic is normal"),
    ("U_SECOND",  "we should add an alert for replica lag"),
    ("U_SECOND",  "we should document the promotion runbook properly"),
]

for index in range(count):
    user, text_body = SCRIPT[index % len(SCRIPT)]
    ts = f"{int(time.time())}.{index:06d}"
    event = {
        "type": "event_callback",
        "event_id": f"Ev{index:08d}",
        "event": {
            "type": "message", "channel": channel, "user": user,
            "text": text_body, "ts": ts,
        },
    }
    raw = json.dumps(event).encode()
    stamp = str(int(time.time()))
    base = b"v0:" + stamp.encode() + b":" + raw
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{api}/webhooks/slack/events", data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": sig,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 300:
            raise SystemExit(f"slack event rejected with {r.status}")
    time.sleep(0.05)
print(f"  posted {count} signed events")
PY
}

# --- resolve ------------------------------------------------------------------
demo_resolve() {
  wait_for_api
  say "resolving incident $INCIDENT via /slash/resolve"

  python - "$API" "$SIGNING_SECRET" "$INCIDENT" <<'PY'
import hashlib, hmac, json, sys, time, urllib.parse, urllib.request

api, secret, incident = sys.argv[1], sys.argv[2], int(sys.argv[3])

with urllib.request.urlopen(f"{api}/api/incidents/active", timeout=10) as r:
    rows = json.load(r).get("incidents") or []
if not rows:
    raise SystemExit("nothing active to resolve")
row = rows[min(incident - 1, len(rows) - 1)]

form = urllib.parse.urlencode({
    "command": "/resolve",
    "text": "replica promoted, error rate normal",
    "channel_id": row.get("chat_channel_id") or "",
    "user_id": "U_PRIMARY",
    "user_name": "primary",
    "response_url": "https://hooks.invalid/none",
    "trigger_id": "T1",
}).encode()

stamp = str(int(time.time()))
base = b"v0:" + stamp.encode() + b":" + form
sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
req = urllib.request.Request(
    f"{api}/slash/resolve", data=form,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": sig,
    },
)
with urllib.request.urlopen(req, timeout=15) as r:
    print(f"  /resolve -> {r.status}")
PY
  say "PIR generation runs in the worker; smoke.sh waits for the document"
}

case "$CMD" in
  storm)   demo_storm ;;
  chatter) demo_chatter ;;
  resolve) demo_resolve ;;
  *)
    cat >&2 <<'USAGE'
usage: demo.sh <storm|chatter|resolve> [flags]

  storm   --alerts N --service NAME    post a correlated cascade
  chatter --incident N --messages M    post signed Slack messages
  resolve --incident N                 run /resolve

Everything goes through the public HTTP surface, so a broken pipeline fails the
script rather than being papered over by a direct database write.
USAGE
    exit 2 ;;
esac
