#!/usr/bin/env bash
# G8 acceptance gate.
#
#   Clean clone, no .env, no API key -> the full demo, three times consecutively,
#   no manual intervention.
#
# The gate spec runs the demo three times. This runs the whole CYCLE three
# times -- `down -v`, `up`, storm, chatter, resolve, smoke -- because repeating
# only the demo against a warm database proves the second run finds the first
# run's data, not that the system starts from nothing. "Works on a clean clone"
# is the claim, so a clean clone is what each pass gets.
#
# What this gate is really for: every prior gate ran against a database some
# earlier test had populated, and week 8 found three defects that only appear on
# an empty one -- an image that could not migrate itself, a `services` table
# nobody seeded (D3 silently absent), and a PIR generator the product never
# called. A gate that starts from nothing is the only one that could have caught
# any of them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CYCLES="${CYCLES:-3}"
TIMEOUT="${TIMEOUT:-120}"

fail() { printf '\n\033[31mGATE G8: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
head() { printf '\n\033[1m--- cycle %s of %s ---\033[0m\n' "$1" "$2"; }

cd "$ROOT"

# --- the clean-clone contract -------------------------------------------------
#
# Asserted rather than assumed. If any of these regress, `docker compose up` on
# a stranger's laptop stops working and nobody notices until they try it.
if [[ -f .env ]]; then
  fail "a .env file exists -- G8 is the no-credentials gate and this run would not prove it"
fi
ok "no .env file present"

if grep -nE '\$\{[A-Z_]+\}|\$\{[A-Z_]+\:\?' docker-compose.yml | grep -v ':-' ; then
  fail "docker-compose.yml has a variable with no default -- a clean clone cannot start"
fi
ok "every compose variable has a default (C-02)"

for var in CHAT_PROVIDER METRICS_PROVIDER; do
  grep -q "${var}:-fake" docker-compose.yml \
    || fail "$var does not default to 'fake' -- a clean clone would call a real API"
done
ok "chat and metrics default to the fakes; no API key is required"

# --- the cycles ---------------------------------------------------------------
for cycle in $(seq 1 "$CYCLES"); do
  head "$cycle" "$CYCLES"

  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d >/dev/null 2>&1 || fail "cycle $cycle: docker compose up failed"

  # Wait for the API rather than sleeping a fixed amount: a fixed sleep is a
  # race that passes on this machine and fails on a slower one.
  deadline=$(( $(date +%s) + TIMEOUT ))
  until curl -fsS http://localhost:"${IP_PORT_API:-18000}"/readyz >/dev/null 2>&1; do
    [[ $(date +%s) -lt $deadline ]] || fail "cycle $cycle: the stack never became ready"
    sleep 2
  done
  ok "cycle $cycle: stack up and ready from empty volumes"

  # The migrate service must have completed, and it must have seeded the
  # reference tables. An empty `services` table is what turned a 40-alert
  # cascade into 36 incidents the first time this ran.
  services=$(docker compose exec -T postgres psql -U ip -d incidentpilot -t \
    -c "SELECT count(*) FROM services;" 2>/dev/null | tr -d ' \r\n')
  [[ "${services:-0}" -ge 10 ]] \
    || fail "cycle $cycle: only ${services:-0} services seeded -- correlation's topology signal is blind"
  ok "cycle $cycle: schema migrated and $services services seeded"

  bash scripts/demo.sh storm --alerts 40 --service payments >/dev/null \
    || fail "cycle $cycle: the storm demo failed"
  sleep 12
  bash scripts/demo.sh chatter --incident 1 --messages 12 >/dev/null \
    || fail "cycle $cycle: the chatter demo failed"
  sleep 4
  bash scripts/demo.sh resolve --incident 1 >/dev/null \
    || fail "cycle $cycle: the resolve demo failed"
  ok "cycle $cycle: storm, chatter and resolve all accepted"

  sleep 25
  bash scripts/smoke.sh --expect-channel --expect-pir --timeout "$TIMEOUT" \
    || fail "cycle $cycle: the smoke test failed"
  ok "cycle $cycle: smoke green"
done

# --- the dashboard's read surface --------------------------------------------
#
# G8 also claims the public URL serves the dashboard in DEMO_MODE. The read API
# it renders from is checked here; the UI itself is checked by `next build` and
# its own tests, because a curl against a server-rendered page proves only that
# a page exists.
for path in /api/incidents/active /api/analytics/mttr /api/analytics/compression \
            /api/analytics/actions /api/analytics/runbooks /api/ops/grounding /api/ops/models \
            /api/transcript/completeness; do
  curl -fsS "http://localhost:${IP_PORT_API:-18000}$path" >/dev/null \
    || fail "read endpoint $path does not answer"
done
ok "all eight dashboard read endpoints answer"

printf '\n\033[32mGATE G8: PASS\033[0m\n'
printf '  %s consecutive clean-clone cycles, no .env, no API key, no manual intervention\n\n' "$CYCLES"
