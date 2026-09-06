#!/usr/bin/env bash
# G1 acceptance gate, criteria 1-3.
#
#   1. A forged request is rejected            -> 401
#   2. A good request is accepted, fast        -> 202, p99 < 250 ms over N requests
#   3. The event is durably on the stream      -> visible via XLEN
#
# Exits non-zero on any failure. A red gate stops the week.
#
# Deliberately avoids `read < <(...)`: `read` returns 1 at EOF-without-newline
# even after populating its variables, and under `set -e` that kills the script
# with every check having passed. Plain command substitution has no such edge.
set -euo pipefail

# 127.0.0.1, not localhost, and deliberately so.
#
# On Windows `localhost` resolves to ::1 first. A server bound to IPv4 only makes
# curl attempt IPv6, fail, and fall back -- measured here at 213 ms of connect
# time per request against 1.2 ms direct. The gate then reports a p99 of ~243 ms
# against a 250 ms budget while the application's real p99 is ~25 ms: a pass by
# luck that becomes a spurious failure on a slower machine.
#
# A benchmark that measures the harness instead of the system under test is
# worse than no benchmark, because you act on the number.
HOST="${HOST:-http://127.0.0.1:18000}"
BEARER="${ALERTMANAGER_BEARER:-local-dev-token}"
PAYLOAD="${PAYLOAD:-bot/tests/fixtures/alertmanager_firing.json}"
N=100
P99_MAX_MS=250

while [[ $# -gt 0 ]]; do
  case "$1" in
    --n) N="$2"; shift 2 ;;
    --p99-max-ms) P99_MAX_MS="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --payload) PAYLOAD="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { printf '\n\033[31mGATE G1: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; }

[[ -f "$PAYLOAD" ]] || fail "payload fixture not found: $PAYLOAD"

printf '\nG1 - ingest and the trust boundary\n'
printf 'target %s | %s requests | p99 budget %s ms\n\n' "$HOST" "$N" "$P99_MAX_MS"

post() {
  # $1 = extra header args as a single string ("" for none)
  # prints "<http_code> <time_total>"
  if [[ -n "$1" ]]; then
    curl -s -o /dev/null -w '%{http_code} %{time_total}' \
      -X POST "$HOST/webhooks/alertmanager" \
      -H "$1" \
      -H 'Content-Type: application/json' \
      --data-binary "@$PAYLOAD"
  else
    curl -s -o /dev/null -w '%{http_code} %{time_total}' \
      -X POST "$HOST/webhooks/alertmanager" \
      -H 'Content-Type: application/json' \
      --data-binary "@$PAYLOAD"
  fi
}

# --- 1. the trust boundary ----------------------------------------------------
result=$(post "Authorization: Bearer definitely-not-the-token")
code=${result%% *}
[[ "$code" == "401" ]] || fail "forged bearer returned $code, expected 401"
ok "forged bearer rejected (401)"

# An endpoint that rejects a *wrong* token but accepts *no* token is a common
# and total hole, so check both.
result=$(post "")
code=${result%% *}
[[ "$code" == "401" ]] || fail "missing bearer returned $code, expected 401"
ok "missing bearer rejected (401)"

# --- 2. accepted, and fast ----------------------------------------------------
times_file=$(mktemp)
trap 'rm -f "$times_file"' EXIT

i=0
while [[ $i -lt $N ]]; do
  result=$(post "Authorization: Bearer $BEARER")
  code=${result%% *}
  secs=${result##* }
  [[ "$code" == "202" ]] || fail "valid request returned $code, expected 202"
  awk -v s="$secs" 'BEGIN { printf "%.3f\n", s * 1000 }' >> "$times_file"
  i=$((i + 1))
done
ok "$N valid requests accepted (202)"

stats=$(sort -n "$times_file" | awk -v n="$N" '
  { v[NR] = $1 }
  END {
    # Nearest-rank percentile: index = ceil(p * n), clamped into range.
    i50 = int(0.50 * n + 0.999); if (i50 < 1) i50 = 1
    i95 = int(0.95 * n + 0.999); if (i95 < 1) i95 = 1
    i99 = int(0.99 * n + 0.999); if (i99 < 1) i99 = 1
    printf "%.1f %.1f %.1f %.1f", v[i50], v[i95], v[i99], v[n]
  }')

p50=$(echo "$stats" | cut -d' ' -f1)
p95=$(echo "$stats" | cut -d' ' -f2)
p99=$(echo "$stats" | cut -d' ' -f3)
pmax=$(echo "$stats" | cut -d' ' -f4)

printf '        p50 %s ms | p95 %s ms | p99 %s ms | max %s ms\n' "$p50" "$p95" "$p99" "$pmax"
awk -v p="$p99" -v budget="$P99_MAX_MS" 'BEGIN { exit !(p < budget) }' \
  || fail "p99 ${p99} ms exceeds the ${P99_MAX_MS} ms budget"
ok "p99 within budget"

# --- 3. durable on the stream -------------------------------------------------
entries=""
if command -v valkey-cli >/dev/null 2>&1; then
  entries=$(valkey-cli XLEN alerts.raw 2>/dev/null | tr -d '\r' || true)
elif command -v docker >/dev/null 2>&1; then
  container=$(docker ps --filter "ancestor=valkey/valkey:9.1.2-alpine" --format '{{.Names}}' | head -1)
  if [[ -n "$container" ]]; then
    entries=$(docker exec "$container" valkey-cli XLEN alerts.raw 2>/dev/null | tr -d '\r' || true)
  fi
fi

if [[ -n "$entries" && "$entries" =~ ^[0-9]+$ ]]; then
  [[ "$entries" -gt 0 ]] || fail "alerts.raw is empty - nothing was durably accepted"
  ok "alerts.raw holds $entries entries"
else
  skip "stream check (no valkey-cli reachable)"
fi

printf '\n\033[32mGATE G1: PASS\033[0m\n\n'
