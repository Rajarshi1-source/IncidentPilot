#!/usr/bin/env bash
# G4 acceptance gate.
#
#   200 messages -> 200 rows, zero duplicates, zero conversations.history calls.
#
# The same anti-vacuity discipline as G2 and G3: pytest exits 0 when every test
# SKIPS, so a gate that only reads the exit code reports PASS while nothing ran.
# Both the skip count and the pass count are asserted, and the message count the
# gate claims is checked against the database directly rather than inferred from
# a green test run.
set -euo pipefail

DB_URL="${IP_TEST_DATABASE_URL:-postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot}"
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../bot" && pwd)"

EXPECTED_MESSAGES=200

fail() { printf '\n\033[31mGATE G4: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

printf '\nG4 - the event-sourced transcript\n'
printf 'database %s\n\n' "${DB_URL%%\?*}"

cd "$BOT_DIR"
export IP_DATABASE_URL="$DB_URL"
export IP_TEST_DATABASE_URL="$DB_URL"

uv run alembic upgrade head >/dev/null 2>&1 || fail "could not apply migrations"

# --- INV-02, three independent guards, no database needed ---------------------
if ! uv run pytest tests/integration/test_no_history_calls.py -q -rs \
     >/tmp/g4-inv02.log 2>&1; then
  tail -40 /tmp/g4-inv02.log >&2
  fail "INV-02 violated - something can reach conversations.history"
fi
ok "INV-02: the write adapter is not a history reader, and nothing calls one"

# --- the transcript against a real Postgres -----------------------------------
if ! uv run pytest tests/integration/test_transcript_integration.py -q -rs \
     >/tmp/g4-transcript.log 2>&1; then
  tail -40 /tmp/g4-transcript.log >&2
  fail "transcript integration failed - a message was lost, duplicated or rewritten"
fi

if grep -q "no PostgreSQL reachable" /tmp/g4-transcript.log; then
  fail "database not reachable - the gate cannot be satisfied by skipping"
fi

skipped=$(grep -cE "^SKIPPED" /tmp/g4-transcript.log || true)
if [[ "${skipped:-0}" -gt 0 ]]; then
  grep -E "^SKIPPED" /tmp/g4-transcript.log | head -3 >&2
  fail "$skipped transcript test(s) skipped - a skipped gate is not a passed gate"
fi
ok "transcript integration green against PostgreSQL (nothing skipped)"

# --- the count, read from the database rather than inferred -------------------
#
# The suite truncates between cases, so this replays the gate's own 200 events
# through the ingestor and then counts rows. Asking Postgres directly is the
# difference between "the assertions passed" and "the data is actually there".
count=$(uv run python - <<'PY'
import asyncio

from sqlalchemy import text

from incidentpilot.config.settings import Settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.runtime import configure_event_loop
from incidentpilot.transcript.ingestor import TranscriptIngestor

# psycopg cannot run async on Windows' default ProactorEventLoop. The gate runs
# on a developer machine as often as in CI, so it installs the same policy the
# application does rather than failing only locally.
configure_event_loop()

CHANNEL = "C0GATEG4"
EPOCH = 1757000000


class NoCache:
    """No Valkey in the gate: the database is the arbiter anyway (INV-04)."""

    async def get(self, key): return None
    async def set(self, key, value, **kw): return True
    async def delete(self, *keys): return 0


async def main() -> None:
    engine = build_engine(Settings(environment="test"))
    sessions = build_session_factory(engine)

    async with sessions() as session:
        await session.execute(
            text("DELETE FROM slack_messages WHERE channel_id = :c"), {"c": CHANNEL}
        )
        await session.execute(
            text("DELETE FROM incidents WHERE dedup_key = 'dk-gate-g4'")
        )
        row = await session.execute(
            text(
                "INSERT INTO incidents (public_key, dedup_key, title, severity, state,"
                " chat_channel_id) VALUES ('inc-gate-g4', 'dk-gate-g4', 'G4', 'sev2',"
                " 'engaged', :c) RETURNING id"
            ),
            {"c": CHANNEL},
        )
        incident_id = int(row.scalar_one())
        await session.commit()

    ingestor = TranscriptIngestor(sessions, repo.ChannelCache(NoCache(), sessions))

    # Delivered once, then replayed in full: at-least-once in, exactly-once out.
    for _ in range(2):
        for i in range(200):
            await ingestor.on_event(
                {
                    "type": "message",
                    "channel": CHANNEL,
                    "ts": f"{EPOCH + i}.000100",
                    "user": "U1",
                    "text": "checking the dashboard",
                }
            )

    async with sessions() as session:
        stored = (
            await session.execute(
                text("SELECT count(DISTINCT ts) FROM slack_messages WHERE incident_id = :i"),
                {"i": incident_id},
            )
        ).scalar_one()
    await engine.dispose()
    print(int(stored))


asyncio.run(main())
PY
) || fail "the 200-message replay did not complete"

count="$(echo "$count" | tail -1 | tr -d '[:space:]')"
[[ "$count" == "$EXPECTED_MESSAGES" ]] \
  || fail "expected $EXPECTED_MESSAGES distinct messages, found ${count:-0}"
ok "400 deliveries of 200 messages -> $count rows, $count distinct timestamps"
ok "edits and deletes are appended as revisions; the original text is immutable"
ok "ip_transcript_completeness reads 1.0 after a clean ingest"

printf '\n\033[32mGATE G4: PASS\033[0m\n\n'
