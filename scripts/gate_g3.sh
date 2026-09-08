#!/usr/bin/env bash
# G3 acceptance gate.
#
#   Kill the worker mid-orchestration, restart -> no duplicate channel.
#   12/12 crash points.
#
# Guards against the failure G2 hit first time round: pytest exits 0 when every
# test SKIPS, so a gate that only checks the exit code reports PASS while
# nothing ran. Both the skip count and the pass count are asserted.
set -euo pipefail

DB_URL="${IP_TEST_DATABASE_URL:-postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot}"
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../bot" && pwd)"

# The 12 crash points from the plan. Named here as well as in the test so the
# gate fails loudly if one is quietly deleted -- a crash matrix that shrinks
# without anyone noticing is the same class of problem as a gate that skips.
EXPECTED_CRASH_POINTS=12

fail() { printf '\n\033[31mGATE G3: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

# --- the integration suite needs the outbox to itself ------------------------
#
# The compose stack runs a live relay that polls `outbox_events` every second.
# These tests enqueue rows and assert on what a relay they construct does with
# them -- so a second relay running against the same database dispatches rows
# out from under the assertions, and the gate fails about one run in three for a
# reason that has nothing to do with the code under test.
#
# Found by running this gate five times in a row. A gate that is right two
# thirds of the time is worse than no gate: it teaches people to re-run.
#
# CI never hits this (the workflow starts only postgres and valkey), which is
# exactly why it survived seven weeks of green builds.
if docker compose ps --services --filter status=running 2>/dev/null | grep -qx relay; then
  echo "  pausing the compose relay for the duration of this gate"
  docker compose stop relay worker scheduler >/dev/null 2>&1 || true
  STOPPED_BACKGROUND_WRITERS=1
  trap 'if [[ "${STOPPED_BACKGROUND_WRITERS:-0}" == "1" ]]; then           docker compose start relay worker scheduler >/dev/null 2>&1 || true; fi' EXIT
fi

printf '\nG3 - Slack orchestration and the transactional outbox\n'
printf 'database %s\n\n' "${DB_URL%%\?*}"

cd "$BOT_DIR"
export IP_DATABASE_URL="$DB_URL"
export IP_TEST_DATABASE_URL="$DB_URL"

uv run alembic upgrade head >/dev/null 2>&1 || fail "could not apply migrations"

# --- the crash matrix ---------------------------------------------------------
if ! uv run pytest tests/integration/test_crash_matrix.py -q -rs \
     >/tmp/g3-crash.log 2>&1; then
  tail -40 /tmp/g3-crash.log >&2
  fail "crash matrix failed - a crash produced a duplicate channel or a lost incident"
fi

if grep -q "no PostgreSQL reachable" /tmp/g3-crash.log; then
  fail "database not reachable - the gate cannot be satisfied by skipping"
fi

skipped=$(grep -cE "^SKIPPED" /tmp/g3-crash.log || true)
if [[ "${skipped:-0}" -gt 0 ]]; then
  grep -E "^SKIPPED" /tmp/g3-crash.log | head -3 >&2
  fail "$skipped crash-matrix test(s) skipped - a skipped gate is not a passed gate"
fi

crash_passed=$(grep -oE "[0-9]+ passed" /tmp/g3-crash.log | grep -oE "[0-9]+" | head -1)
# 12 crash points plus the control cases (clean run, replay, stuck reclaim).
if [[ "${crash_passed:-0}" -lt $((EXPECTED_CRASH_POINTS + 3)) ]]; then
  fail "only ${crash_passed:-0} crash-matrix assertions ran; expected at least $((EXPECTED_CRASH_POINTS + 3))"
fi
ok "${crash_passed} crash-matrix assertions ran (none skipped)"
ok "12/12 crash points: no duplicate channel, no lost incident, no dead rows"

# --- the outbox itself --------------------------------------------------------
if ! uv run pytest tests/integration/test_outbox_integration.py -q -rs \
     >/tmp/g3-outbox.log 2>&1; then
  tail -30 /tmp/g3-outbox.log >&2
  fail "outbox integration tests failed"
fi

outbox_skipped=$(grep -cE "^SKIPPED" /tmp/g3-outbox.log || true)
[[ "${outbox_skipped:-0}" -eq 0 ]] || fail "$outbox_skipped outbox test(s) skipped"
ok "idempotency key is a UNIQUE constraint; poison rows reach 'dead' at 8 attempts"
ok "two relays share the table via FOR UPDATE SKIP LOCKED, no row handled twice"

# --- the architectural boundary -----------------------------------------------
if ! uv run pytest tests/unit/test_no_external_writes_outside_relay.py -q \
     >/tmp/g3-inv03.log 2>&1; then
  tail -20 /tmp/g3-inv03.log >&2
  fail "INV-03 violated - something outside the relay performs external writes"
fi
ok "INV-03: only the relay performs external chat writes"

# --- INV-02, still holding ----------------------------------------------------
if ! uv run pytest tests/unit/test_outbox_and_chat.py -q -k "history" \
     >/tmp/g3-inv02.log 2>&1; then
  tail -20 /tmp/g3-inv02.log >&2
  fail "INV-02 violated - conversations.history is reachable"
fi
ok "INV-02: the fake adapter still refuses conversations.history"

printf '\n\033[32mGATE G3: PASS\033[0m\n\n'
