"""Did anyone actually follow the runbook? (W5-13, D4)

Three independent signals, deduplicated by the database into one row per step:

    command_match   a transcript message contains the step's command
    reaction        someone reacted on the pinned runbook message
    slash_command   someone typed `/step done verify-replica-lag`

**Why three.** Any single source is wrong in a predictable direction. Slash
commands only capture people who remember to type them, which is nobody during a
sev1. Reactions capture agreement rather than execution. Command matching
captures the work but misses steps whose "command" is a decision -- "decide about
failover, out loud" has nothing to grep for. Together they cover most of the
ground, and the ones they still miss are steps that genuinely leave no trace,
which is itself worth knowing.

**Why the dedup is a constraint and not a check.** ``UNIQUE (incident_id,
step_id)`` means a responder who runs the command, reacts to the pinned message
*and* types `/step done` has followed that step once. Three rows would inflate
adherence, and an efficacy metric that flatters itself is worse than no metric --
it produces confident, wrong answers to "which runbooks work".

**The loop is not closed, and says so.** These signals feed ``Q5``, the corrected
dead-step query. The auto-PR job that would act on ``Q5`` is deferred (E-3): its
filter needs five incidents per runbook and eight weeks will not produce five.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.db.models import RunbookStepSignal
from incidentpilot.runbooks.parser import ParsedRunbook
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

COMMAND_MATCH = "command_match"
REACTION = "reaction"
SLASH_COMMAND = "slash_command"

# Tokens that appear in nearly every step and identify nothing. Matching on
# `kubectl` alone would mark every step of every kubernetes runbook as followed
# the moment anyone typed anything.
_GENERIC = frozenset(
    {
        "kubectl",
        "psql",
        "curl",
        "echo",
        "grep",
        "sudo",
        "bash",
        "sh",
        "get",
        "-n",
        "|",
        "&&",
    }
)

_FENCE = re.compile(r"```(?:[a-z]*\n)?(.*?)```", re.DOTALL)
_WORD = re.compile(r"[A-Za-z0-9_./:-]{3,}")


@dataclass(frozen=True, slots=True)
class StepSignal:
    step_id: str
    detected_by: str
    source_message_ts: str | None = None


@lru_cache(maxsize=512)
def _fingerprint(step_body: str) -> frozenset[str]:
    """The distinctive tokens inside a step's fenced code blocks.

    Only fenced blocks, because prose contains the service name and the alert
    name and would match any message that mentioned either. Generic tokens are
    dropped, and a step whose fingerprint ends up empty simply never matches by
    command -- which is the correct outcome for "decide about failover, out
    loud", not a bug to work around.
    """
    tokens: set[str] = set()
    for block in _FENCE.findall(step_body):
        for word in _WORD.findall(block):
            lowered = word.lower()
            if lowered not in _GENERIC:
                tokens.add(lowered)
    return frozenset(tokens)


def detect_in_message(runbook: ParsedRunbook, text: str, *, min_tokens: int = 2) -> str | None:
    """Which step, if any, this message looks like an execution of.

    Requires ``min_tokens`` distinctive tokens rather than one. A single shared
    token -- a table name, a flag -- appears in several steps of the same
    runbook, and a one-token threshold would attribute a message to whichever
    step happened to be checked first.
    """
    lowered = text.lower()
    best: tuple[str, int] | None = None
    for step in runbook.steps:
        fingerprint = _fingerprint(step.body)
        if not fingerprint:
            continue
        hits = sum(1 for token in fingerprint if token in lowered)
        if hits >= min_tokens and (best is None or hits > best[1]):
            best = (step.step_id, hits)
    return best[0] if best else None


async def record_signal(
    session: AsyncSession,
    *,
    incident_id: int,
    runbook_id: int,
    signal: StepSignal,
) -> bool:
    """Store one step signal. Returns False when the step was already recorded.

    ``ON CONFLICT DO NOTHING`` on ``(incident_id, step_id)`` is where the three
    sources collapse into one. Returning False rather than raising keeps the
    caller simple: the second and third signals for a step are the *expected*
    case, not an error to handle.
    """
    stmt = (
        pg_insert(RunbookStepSignal)
        .values(
            incident_id=incident_id,
            runbook_id=runbook_id,
            step_id=signal.step_id,
            detected_by=signal.detected_by,
            source_message_ts=signal.source_message_ts,
        )
        .on_conflict_do_nothing(constraint="uq_step_signal")
        .returning(RunbookStepSignal.id)
    )
    inserted = (await session.execute(stmt)).scalar() is not None
    if inserted:
        log.info(
            "runbook.step_signal",
            incident_id=incident_id,
            step_id=signal.step_id,
            detected_by=signal.detected_by,
        )
    return inserted


def valid_step(runbook: ParsedRunbook, step_id: str) -> bool:
    """Whether a `/step done <id>` argument names a real step of this runbook.

    Checked before writing, so a typo produces "no such step, here are the
    five" rather than a signal row for a step that does not exist -- which would
    make adherence exceed 1.0 and quietly corrupt D4.
    """
    return step_id in set(runbook.step_ids)
