#!/usr/bin/env bash
# G5 acceptance gate.
#
#   Pager unreachable -> cache -> static rota -> team channel, each announced.
#   Silent failure fails the gate.
#
# The last line is the gate. Resolving someone from a stale rota and posting a
# notice indistinguishable from a healthy page would satisfy every assertion
# about *who* was paged and still be the failure this week exists to prevent, so
# this script checks the ladder AND greps what the channel was told.
#
# Same anti-vacuity discipline as G2/G3/G4: pytest exits 0 when every test
# SKIPS, so both the skip count and the pass count are asserted.
set -euo pipefail

DB_URL="${IP_TEST_DATABASE_URL:-postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot}"
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../bot" && pwd)"

EXPECTED_RUNBOOKS=8

fail() { printf '\n\033[31mGATE G5: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

printf '\nG5 - responders, fatigue routing, runbooks\n'
printf 'database %s\n\n' "${DB_URL%%\?*}"

cd "$BOT_DIR"
export IP_DATABASE_URL="$DB_URL"
export IP_TEST_DATABASE_URL="$DB_URL"

uv run alembic upgrade head >/dev/null 2>&1 || fail "could not apply migrations"

# --- the pure half: the score and the bands, no database needed ---------------
if ! uv run pytest tests/unit/test_fatigue.py tests/unit/test_routing.py -q -rs \
     >/tmp/g5-decide.log 2>&1; then
  tail -40 /tmp/g5-decide.log >&2
  fail "the routing decision is wrong before it ever reaches a channel"
fi
ok "fatigue bands hold; the top band pages the secondary AND notifies the primary"

# --- the runbooks -------------------------------------------------------------
if ! uv run pytest tests/unit/test_runbooks.py -q -rs >/tmp/g5-runbooks.log 2>&1; then
  tail -40 /tmp/g5-runbooks.log >&2
  fail "runbooks do not parse, or the matcher picked the loudest alert"
fi

runbook_count=$(ls -1 ../runbooks/*.md 2>/dev/null | wc -l | tr -d '[:space:]')
[[ "$runbook_count" == "$EXPECTED_RUNBOOKS" ]] \
  || fail "expected $EXPECTED_RUNBOOKS runbooks, found ${runbook_count:-0}"
ok "$runbook_count runbooks parse; every step id is unique and kebab-case"
ok "the runbook is matched on the ROOT SIGNAL, not on the loudest alert"

# --- the slash commands -------------------------------------------------------
if ! uv run pytest tests/unit/test_slash.py -q -rs >/tmp/g5-slash.log 2>&1; then
  tail -40 /tmp/g5-slash.log >&2
  fail "a slash command missed its 3-second ack or skipped signature verification"
fi
ok "seven slash commands, each verified and acked inside Slack's 3s budget"

# --- the ladder, against a real database --------------------------------------
if ! uv run pytest tests/integration/test_routing_integration.py -q -rs \
     >/tmp/g5-ladder.log 2>&1; then
  tail -40 /tmp/g5-ladder.log >&2
  fail "the fallback ladder did not behave, or did not announce itself"
fi

if grep -q "no PostgreSQL reachable" /tmp/g5-ladder.log; then
  fail "database not reachable - the gate cannot be satisfied by skipping"
fi

skipped=$(grep -cE "^SKIPPED" /tmp/g5-ladder.log || true)
if [[ "${skipped:-0}" -gt 0 ]]; then
  grep -E "^SKIPPED" /tmp/g5-ladder.log | head -3 >&2
  fail "$skipped ladder test(s) skipped - a skipped gate is not a passed gate"
fi
ok "cache -> provider -> static rota -> team broadcast, each rung announced"

# --- the corrected queries ----------------------------------------------------
if ! uv run pytest tests/integration/test_queries_integration.py -q -rs \
     >/tmp/g5-queries.log 2>&1; then
  tail -40 /tmp/g5-queries.log >&2
  fail "Q5 (X-01) or Q4 (C-07) does not run against a real database"
fi
ok "Q5 (X-01 corrected) and Q4 (C-07 corrected) execute and return the right rows"

# --- the assertion the gate is actually named after ---------------------------
#
# Driven end to end here rather than trusted from the suite: the provider is
# unreachable, the cache is empty, and the question is whether a human reading
# the channel would KNOW the answer came from a stale file.
notice=$(uv run python - <<'PY'
import asyncio

import httpx
import respx

from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.adapters.paging.pagerduty import REST_BASE, PagerDutyPaging
from incidentpilot.adapters.paging.static_schedule import StaticSchedule
from incidentpilot.db.repositories import OnCallCache
from incidentpilot.orchestration.routing import ResponderRouter
from incidentpilot.runtime import configure_event_loop
from incidentpilot.adapters.chat import block_kit

configure_event_loop()


class NoCache:
    """An empty cache is rung 1 missing, which is the case under test."""

    async def get(self, key): return None
    async def set(self, key, value, **kw): return True


async def main() -> None:
    with respx.mock:
        respx.get(f"{REST_BASE}/oncalls").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            paging = PagerDutyPaging(routing_key="rk", api_token="tok", client=client)
            router = ResponderRouter(paging, OnCallCache(NoCache()))
            oncall = await router.resolve_oncall("payments")

    assert oncall.known, "the static rota should still name someone"
    assert oncall.source.value == "static_schedule", oncall.source

    blocks = block_kit.routing_notice(
        paged=[oncall.primary],
        notified=[],
        score=0.0,
        reasons=[],
        rerouted=False,
        oncall_source=oncall.source.value,
        degraded_reason=oncall.reason,
    )
    print(str(blocks))


asyncio.run(main())
PY
) || fail "the live ladder check did not complete"

echo "$notice" | grep -qi "degraded" \
  || fail "the fallback did not announce itself - silent failure fails G5"
echo "$notice" | grep -qi "static_schedule" \
  || fail "the notice does not say where the answer came from"
ok "a degraded lookup says so in the channel, naming the rung it fell to"

printf '\n\033[32mGATE G5: PASS\033[0m\n\n'
