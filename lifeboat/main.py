"""The dead-man's switch (W7-18, D7 L3).

A SEPARATE image. **No application imports, standard library only.** If the
lifeboat imported the bot it would fail for the same reason the bot failed, and
a lifeboat that sinks with the ship is not a lifeboat -- it is a second copy of
the bug. ``test_lifeboat_imports_nothing`` walks this file's AST and asserts
every import is stdlib, so the property is enforced rather than intended.

Every feature added here is another way for it to fail. It probes readiness, and
on the *second* consecutive failure it posts one message naming the last known
on-call and a manual runbook link. Nothing else. It does not query the database,
it does not read the incident, it does not try to be clever about which channel
-- all of those are dependencies, and dependencies are what it is insuring
against.

Two consecutive failures rather than one, deliberately. A single failed probe
during a rolling restart is normal and paging on it would train people to ignore
the lifeboat, which is the only alarm that still works when everything else is
down.

The counter lives on disk under ``/tmp`` because the CronJob runs a fresh
container every minute and process memory does not survive that. A wiped
counter fails safe in the direction that matters: it delays the alert by one
minute rather than suppressing it.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

PROBE = os.environ.get("PROBE_URL", "http://incidentpilot-api:8000/readyz")
HOOK = os.environ.get("FALLBACK_WEBHOOK", "")
STATE = pathlib.Path(os.environ.get("LAST_ONCALL_PATH", "/state/oncall.json"))
FAILS = pathlib.Path(os.environ.get("FAIL_COUNT_PATH", "/tmp/lifeboat.fails"))
MANUAL_RUNBOOK = os.environ.get("MANUAL_RUNBOOK_URL", "")

# Fire on the second consecutive failure, and only on the second: a longer
# outage must not repost every minute. The bot being down for an hour is one
# piece of news, and sixty copies of it is a muted channel.
FIRE_AT = 2

PROBE_TIMEOUT_S = 3
POST_TIMEOUT_S = 5


def healthy() -> bool:
    """One HTTP GET. Any exception is a failure -- there is no partial credit."""
    try:
        with urllib.request.urlopen(PROBE, timeout=PROBE_TIMEOUT_S) as response:
            return int(response.status) == 200
    except Exception:
        return False


def read_fails() -> int:
    try:
        return int(FAILS.read_text().strip())
    except Exception:
        return 0


def write_fails(count: int) -> None:
    try:
        FAILS.parent.mkdir(parents=True, exist_ok=True)
        FAILS.write_text(str(count))
    except Exception:
        # An unwritable counter must not stop the alert. Losing the count means
        # the next run starts from zero, which delays by a minute; raising here
        # would mean no alert at all.
        pass


def last_oncall() -> str:
    """Written by the bot while it was healthy. The only state the lifeboat reads."""
    try:
        return str(json.loads(STATE.read_text()).get("responder") or "unknown")
    except Exception:
        return "unknown"


def message() -> str:
    lines = [
        ":rotating_light: IncidentPilot is unreachable. Fall back to the manual process.",
        f"Last known on-call: {last_oncall()}",
    ]
    if MANUAL_RUNBOOK:
        lines.append(f"Manual runbook: {MANUAL_RUNBOOK}")
    lines.append("This message came from the lifeboat, which shares no code with the bot.")
    return "\n".join(lines)


def post(text: str) -> bool:
    if not HOOK:
        print("no FALLBACK_WEBHOOK configured; not posting", file=sys.stderr)
        return False
    request = urllib.request.Request(
        HOOK,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
            return int(response.status) < 300
    except Exception as exc:
        print(f"lifeboat post failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    if healthy():
        write_fails(0)
        return 0

    count = read_fails() + 1
    write_fails(count)
    if count != FIRE_AT:
        return 0

    posted = post(message())
    print(f"lifeboat fired after {count} failed probes; posted={posted}")
    # Exit 0 either way. A non-zero exit makes the CronJob retry, and a retrying
    # lifeboat during a total outage is a retry storm aimed at the one webhook
    # still answering.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
