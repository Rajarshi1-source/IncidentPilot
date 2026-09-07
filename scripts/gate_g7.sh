#!/usr/bin/env bash
# G7 acceptance gate.
#
#   1. `make eval` prints every metric with a per-bucket breakdown,
#      network calls 0, wall < 30 s.
#   2. Change one word in pir_v2_1_0.md without updating the baseline
#      -> the gate goes RED, and the report names what moved.
#
# The second half is the one that matters, and it is the one Rev 1 got wrong.
# Its eval job was gated on `github.event.head_commit.modified` -- only
# populated on push events, absent on pull requests, and a list, so `contains()`
# tested exact element equality and was false essentially always. The job never
# ran once. **A quality gate that never runs is worse than no gate, because you
# believe you have one** (B-11), so this script does not check that a gate
# exists; it makes the gate fail and checks that it noticed.
#
# Same anti-vacuity discipline as G2-G6: a check that can be satisfied by doing
# nothing is not a check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="$ROOT/bot"
PROMPT="$BOT_DIR/src/incidentpilot/pir/prompts/pir_v2_1_0.md"
CORPUS="eval/corpus"

WALL_BUDGET_S=30
CORPUS_SIZE=40

fail() { printf '\n\033[31mGATE G7: FAIL\033[0m - %s\n\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

WORK="$(mktemp -d)"
# The Python that runs the harness is the platform interpreter, not the shell's.
# On a Git-Bash checkout those disagree about what `/tmp/...` means, and the
# failure is a FileNotFoundError for a file the shell can plainly see. `cygpath`
# is absent on Linux, where the two already agree, so the fallback is identity.
native() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
WORKP="$(native "$WORK")"
RESTORE=0
cleanup() {
  # The prompt is restored whatever happens, including on Ctrl-C. A gate that
  # can leave the working tree modified is a gate people stop running.
  if [[ "$RESTORE" == "1" && -f "$WORK/prompt.bak" ]]; then
    cp "$WORK/prompt.bak" "$PROMPT"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

printf '\nG7 - the replay harness and the CI eval gate\n'
printf 'corpus %s\n\n' "$BOT_DIR/$CORPUS"

cd "$BOT_DIR"

# --- the corpus is the size it claims to be -----------------------------------
found=$(find "$CORPUS" -name 'incident_*.jsonl' | wc -l | tr -d '[:space:]')
if [[ "$found" -ne "$CORPUS_SIZE" ]]; then
  fail "expected $CORPUS_SIZE recordings, found $found"
fi
ok "corpus holds $CORPUS_SIZE recordings across 11 buckets"

# --- 1. the green run ---------------------------------------------------------
start=$(python -c 'import time; print(time.time())')
if ! uv run python -m eval.run --corpus "$CORPUS" --gate --report "$WORKP/green.md" \
     --json "$WORKP/green.json" >"$WORK/green.log" 2>&1; then
  tail -40 "$WORK/green.log" >&2
  fail "the committed corpus does not pass its own committed baseline"
fi
end=$(python -c 'import time; print(time.time())')
wall=$(python -c "print(f'{$end - $start:.1f}')")

if ! awk -v w="$wall" -v b="$WALL_BUDGET_S" 'BEGIN{exit !(w < b)}'; then
  fail "replay took ${wall}s, budget is ${WALL_BUDGET_S}s"
fi
ok "40 incidents replayed in ${wall}s (budget ${WALL_BUDGET_S}s)"

# --- INV-10: measured, not asserted -------------------------------------------
#
# Read out of the run's own JSON rather than grepped from a log line, because a
# log line can be printed by code that never checked anything. The number comes
# from a socket guard that was installed for the duration of every replay.
network=$(python -c "import json;print(int(json.load(open(r'$WORKP/green.json'))['metrics']['network_calls']))")
if [[ "$network" -ne 0 ]]; then
  fail "$network network call(s) during replay - INV-10 requires zero"
fi
ok "zero network calls (socket guard active for every fixture)"

# --- the report is readable, not just present ---------------------------------
for needle in "Per bucket" "code_switched" "GATE: PASS" "small corpus"; do
  grep -q "$needle" "$WORK/green.md" || fail "the report is missing '$needle'"
done
ok "report carries per-bucket numbers and states the corpus limitation"

if ! grep -q "judge_agreement" "$WORK/green.md"; then
  fail "judge-vs-human agreement is not reported"
fi
ok "judge agreement measured on the labelled subset and reported, never gated"

# --- determinism, three runs --------------------------------------------------
for run in 1 2 3; do
  uv run python -m eval.run --corpus "$CORPUS" --json "$WORKP/det$run.json" >/dev/null 2>&1
done
if ! python - "$WORKP" <<'PY'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
scored = {
    json.dumps({k: v for k, v in json.loads((work / f"det{i}.json").read_text()).items()
                if k != "wall_s"}, sort_keys=True)
    for i in (1, 2, 3)
}
sys.exit(0 if len(scored) == 1 else 1)
PY
then
  fail "three runs produced three different results - replay is not deterministic"
fi
ok "three runs, identical scores (wall-clock excluded: it measures the harness)"

# --- 2. the red run -----------------------------------------------------------
#
# The headline. One word, no baseline update, and CI must go red with a readable
# reason.
cp "$PROMPT" "$WORK/prompt.bak"
RESTORE=1
python - "$(native "$PROMPT")" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
body = path.read_text(encoding="utf-8")
edited = body.replace("You are not a", "You are never a", 1)
if edited == body:
    print("the prompt no longer contains the phrase this gate edits", file=sys.stderr)
    raise SystemExit(1)
path.write_text(edited, encoding="utf-8")
PY

set +e
uv run python -m eval.run --corpus "$CORPUS" --gate --report "$WORKP/red.md" >"$WORK/red.log" 2>&1
red_status=$?
set -e

cp "$WORK/prompt.bak" "$PROMPT"
RESTORE=0

if [[ "$red_status" -eq 0 ]]; then
  fail "a one-word prompt change did not fail the gate - B-11 has regressed"
fi
ok "one word changed in pir_v2_1_0.md -> gate exits $red_status"

grep -q "prompt_drift" "$WORK/red.md" || fail "the report does not name prompt_drift"
grep -q "GATE: FAIL" "$WORK/red.md" || fail "the report does not read FAIL"
grep -q "no longer matches its registry entry" "$WORK/red.md" \
  || fail "the report does not explain WHY the corpus can no longer score this prompt"
ok "the sticky comment names the metric that moved and why"

grep -q "::error::" "$WORK/red.log" || fail "no GitHub annotation was emitted"
ok "::error:: annotation emitted, so the red check is readable without the log"

# --- the working tree is exactly as we found it -------------------------------
if ! git -C "$ROOT" diff --quiet -- "$PROMPT"; then
  fail "the gate left the prompt modified"
fi
ok "prompt restored; the gate leaves no diff behind"

printf '\n\033[32mGATE G7: PASS\033[0m\n'
printf '  green: %s\n' "$(tail -1 "$WORK/green.log" | tr -d '\r')"
printf '  red:   %s\n\n' "$(grep -m1 'GATE: FAIL' "$WORK/red.log" | tr -d '\r')"
