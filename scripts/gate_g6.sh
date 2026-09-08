#!/usr/bin/env bash
# G6 acceptance gate.
#
#   Provider blocked -> skeleton in 90 s.
#   Unblocked        -> full PIR, zero uncited claims.
#
# The first half matters more than the second. A feature that produces an
# excellent document when the vendor is up and nothing when it is down is a
# feature that is absent on exactly the days incidents cluster.
#
# D1 is stricter than the gate line: "a fabricated ID rejects, retries, then
# degrades -- no partial publication". So this checks the sequence, not just the
# endpoints, and counts the rows in pir_documents at the end.
#
# Same anti-vacuity discipline as G2-G5: pytest exits 0 when every test SKIPS.
set -euo pipefail

DB_URL="${IP_TEST_DATABASE_URL:-postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot}"
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../bot" && pwd)"

SKELETON_BUDGET_S=90

fail() { printf '\n\033[31mGATE G6: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
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

printf '\nG6 - deterministic impact and the grounded PIR\n'
printf 'database %s\n\n' "${DB_URL%%\?*}"

cd "$BOT_DIR"
export IP_DATABASE_URL="$DB_URL"
export IP_TEST_DATABASE_URL="$DB_URL"

uv run alembic upgrade head >/dev/null 2>&1 || fail "could not apply migrations"

# --- INV-07: no model name in code --------------------------------------------
#
# Run here as well as in lint-and-type. This is the invariant that makes the
# week 7 experiment a flag instead of a diff, and a gate that depends on another
# job having run is not a gate.
if grep -rEn "gpt-[0-9]|claude-|gemini-|llama-" src --include="*.py" \
   | grep -v "src/incidentpilot/config/"; then
  fail "a model name appears in code outside config/ (INV-07)"
fi
ok "INV-07: roles resolve through config/models.yaml; no model name in code"

# --- INV-05 / B-10: the schema is the contract --------------------------------
if ! uv run pytest tests/unit/test_evidence_and_schema.py -q -rs >/tmp/g6-schema.log 2>&1; then
  tail -40 /tmp/g6-schema.log >&2
  fail "an uncited claim is representable, or impact leaked into the schema"
fi
ok "INV-05: an uncited claim cannot be constructed; impact is absent from PIRDraft"

# --- the deterministic gate ---------------------------------------------------
if ! uv run pytest tests/unit/test_pir_core.py -q -rs >/tmp/g6-core.log 2>&1; then
  tail -40 /tmp/g6-core.log >&2
  fail "the validator, the impact computation or the skeleton is wrong"
fi
ok "the validator rejects fabricated IDs, ungrounded numbers and absent owners"
ok "metrics unavailable renders as 'unavailable', never as an estimate (FMEA #15)"

# --- D6: the PII boundary ------------------------------------------------------
if ! uv run pytest tests/unit/test_pii_boundary.py -q -rs >/tmp/g6-pii.log 2>&1; then
  tail -40 /tmp/g6-pii.log >&2
  fail "raw PII reached a provider adapter (D6)"
fi
ok "D6: no raw email, card, AWS key or Slack token reaches a provider payload"

# --- the two runs, against a real database ------------------------------------
if ! uv run pytest tests/integration/test_pir_integration.py -q -rs >/tmp/g6-pir.log 2>&1; then
  tail -60 /tmp/g6-pir.log >&2
  fail "the PIR pipeline failed against PostgreSQL"
fi

if grep -q "no PostgreSQL reachable" /tmp/g6-pir.log; then
  fail "database not reachable - the gate cannot be satisfied by skipping"
fi

skipped=$(grep -cE "^SKIPPED" /tmp/g6-pir.log || true)
if [[ "${skipped:-0}" -gt 0 ]]; then
  grep -E "^SKIPPED" /tmp/g6-pir.log | head -3 >&2
  fail "$skipped PIR test(s) skipped - a skipped gate is not a passed gate"
fi
ok "blocked provider -> skeleton; working provider -> coverage 1.000"
ok "D1: a fabricated ID rejects, retries, then degrades - one row, never a partial"

# --- the timing claim, measured rather than asserted --------------------------
#
# The gate says "skeleton in 90 s". The suite asserts it too, but the number in
# the gate should be a measurement, not a repetition of a test's own constant.
elapsed=$(uv run python - <<'PY'
import asyncio
import time

from sqlalchemy import text

from incidentpilot.adapters.llm.base import RetryableProviderError
from incidentpilot.adapters.llm.fake import FakeLLM
from incidentpilot.adapters.llm.router import LLMRouter
from incidentpilot.adapters.metrics.fake import FakeMetrics
from incidentpilot.config.models_config import load_models_config
from incidentpilot.config.settings import Settings
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.pir.generator import PIRGenerator
from incidentpilot.runtime import configure_event_loop

configure_event_loop()


async def main() -> None:
    engine = build_engine(Settings(environment="test"))
    sessions = build_session_factory(engine)

    async with sessions() as session:
        row = await session.execute(
            text("SELECT id FROM incidents ORDER BY id DESC LIMIT 1")
        )
        incident_id = row.scalar()

    if incident_id is None:
        print("-1")
        await engine.dispose()
        return

    # Every provider in the chain unreachable. Nothing left to block.
    provider = FakeLLM()
    provider.fail_next(times=50, error=RetryableProviderError)
    router = LLMRouter(
        load_models_config(),
        {"fake": provider, "anthropic": provider, "openai": provider, "local": provider},
    )
    generator = PIRGenerator(router, metrics=FakeMetrics())

    started = time.perf_counter()
    async with sessions() as session:
        result = await generator.generate(session, int(incident_id))
    duration = time.perf_counter() - started

    assert result.layer == "skeleton", result.layer
    assert "AI drafting unavailable" in result.markdown
    await engine.dispose()
    print(f"{duration:.2f}")


asyncio.run(main())
PY
) || fail "the timed skeleton run did not complete"

elapsed="$(echo "$elapsed" | tail -1 | tr -d '[:space:]')"
if [[ "$elapsed" == "-1" ]]; then
  fail "no incident in the database to generate against"
fi
if ! awk -v e="$elapsed" -v b="$SKELETON_BUDGET_S" 'BEGIN{exit !(e < b)}'; then
  fail "skeleton took ${elapsed}s, budget is ${SKELETON_BUDGET_S}s"
fi
ok "skeleton produced in ${elapsed}s with every provider unreachable (budget ${SKELETON_BUDGET_S}s)"

printf '\n\033[32mGATE G6: PASS\033[0m\n\n'
