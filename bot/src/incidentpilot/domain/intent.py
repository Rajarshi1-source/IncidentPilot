"""Intent detection, layer 1 of the ladder (W4-07).

PURE MODULE (INV-01). No I/O, no clock, no randomness -- which is what lets the
same function run in the hot path, in the week 7 replay harness, and in a unit
test with no fixtures.

The ladder is a cost argument, not an accuracy one:

    1. regex rules          free, instant, ~80% of real incident chatter   W4
    2. embedding similarity cheap, catches paraphrase                      W4
    3. the `extract` role   expensive, only on what layers 1-2 call NOISE  W6

Layers 2 and 3 are impure -- they call an embedding provider and the model
router -- so they live in ``transcript/classifier.py`` rather than here (C-10).
This module is the floor the other two escalate from, and it is the only layer
the ingestor is allowed to depend on.

**The known weakness, stated rather than hidden (§8.3).** These rules handle
code-switched English/Hindi badly, and a channel that is half and half is normal
on an Indian engineering team. Four code-switched fixtures are scored in the
week 7 corpus *while they fail*; ``test_code_switched_is_a_known_gap`` records
the gap here. A measured gap is a much better answer to "what is weakest right
now?" than an unmeasured one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Longest summary carried onto a timeline row. Slack allows 40k characters per
# message; a timeline is scanned, not read, and an unbounded description turns
# the PIR context window into someone's pasted stack trace.
SUMMARY_CHARS = 200


class IntentKind(StrEnum):
    NOISE = "noise"
    INVESTIGATION = "investigation"
    REMEDIATION_START = "remediation_start"
    REMEDIATION_END = "remediation_end"
    RECOVERY_SIGNAL = "recovery_signal"
    ESCALATION = "escalation"
    STATUS_UPDATE = "status_update"
    RUNBOOK_STEP = "runbook_step"


# The two moments worth a metric snapshot: someone started changing the system,
# and the system started looking better. Everything a PIR says about "did the
# fix work" is the comparison between those two windows, and neither can be
# reconstructed after the fact -- Prometheus retention outlives nobody's memory
# of exactly when the rollback began.
SNAPSHOT_TRIGGERS: frozenset[IntentKind] = frozenset(
    {IntentKind.REMEDIATION_START, IntentKind.RECOVERY_SIGNAL}
)

# STATUS_UPDATE and RUNBOOK_STEP are deliberately absent from the rule list.
# Both are *asserted* rather than inferred -- `/update` writes the first (W5-15)
# and `/step done` the second (W5-13). Guessing at them from free text would
# produce timeline rows nobody typed, and a timeline is evidence.
RULES: tuple[tuple[IntentKind, re.Pattern[str], float], ...] = (
    # END before START on purpose. `roll ?back` matches "rollback complete" just
    # as happily as "rolling back", so ordering START first would file every
    # completion as a fresh remediation -- and W4-05 would then take a metric
    # snapshot at the moment the work *finished*, which is the one moment it is
    # worthless.
    (
        IntentKind.REMEDIATION_END,
        re.compile(
            r"\b(rollback (is )?(done|complete|finished)|rolled ?back|deployed|applied|"
            r"promotion (is )?(done|complete)|restarted|failed ?over|"
            r"scale[ds]? (up|down)|fix (is )?(in|out|live|shipped))\b",
            re.I,
        ),
        0.90,
    ),
    (
        IntentKind.RECOVERY_SIGNAL,
        re.compile(
            r"\b(recovered|recovering|back to normal|error rate (is )?(down|dropping|normal)|"
            r"lag (is )?(gone|zero|clearing)|looks (healthy|green|clean|good)|"
            r"traffic (is )?normal|p99 (is )?(back|normal)|all green)\b",
            re.I,
        ),
        0.90,
    ),
    (
        IntentKind.REMEDIATION_START,
        re.compile(
            r"\b(rolling ?back|roll ?back|restart(ing)?|failing ?over|promot(e|ing)|"
            r"scal(e|ing) (up|down)|deploy(ing)? (a )?(fix|hotfix|patch)|"
            r"kubectl (rollout|delete|scale|drain|cordon)|helm (rollback|upgrade)|"
            r"flush(ing)? (the )?cache|draining)\b",
            re.I,
        ),
        0.95,
    ),
    # Before INVESTIGATION: "need help checking the logs" is a call for people,
    # and reading it as someone quietly grepping loses the only signal in it.
    (
        IntentKind.ESCALATION,
        re.compile(
            r"\b(escalat(e|ing|ion)|paging|need (help|more hands|another pair)|"
            r"calling in|waking|pulling in|can someone|who owns)\b",
            re.I,
        ),
        0.85,
    ),
    (
        IntentKind.INVESTIGATION,
        re.compile(
            r"\b(check(ing)?|look(ing)? at|seeing|grep|logs?|dashboard|query|trace|"
            r"why|what changed|suspect|digging|investigat(e|ing))\b",
            re.I,
        ),
        0.70,
    ),
)


@dataclass(frozen=True, slots=True)
class Intent:
    kind: IntentKind
    confidence: float
    summary: str


# A bare emoji reaction, a "+1", a "ack" -- real messages, zero timeline value.
_MIN_MEANINGFUL_CHARS = 3


def detect_intent(text: str) -> Intent:
    """Classify one message. Deterministic, allocation-light, never raises.

    Returns ``NOISE`` with confidence 0.5 when nothing matched -- and the 0.5 is
    load-bearing rather than decorative. Layer 2 escalates on *low-confidence
    NOISE only*, so a message that is confidently noise (an emoji, a one-word
    ack) never costs an embedding call, while a paraphrase nobody wrote a rule
    for does.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_MEANINGFUL_CHARS or stripped.startswith(":"):
        return Intent(IntentKind.NOISE, 1.0, "")

    summary = stripped[:SUMMARY_CHARS]
    for kind, pattern, confidence in RULES:
        if pattern.search(stripped):
            return Intent(kind, confidence, summary)
    return Intent(IntentKind.NOISE, 0.5, summary)


def is_escalatable(intent: Intent) -> bool:
    """Whether layer 2 should spend an embedding call on this message.

    Kept here, next to the confidence values it reads, so the escalation policy
    cannot drift from the rules that produce the confidences.
    """
    return intent.kind is IntentKind.NOISE and intent.confidence < 1.0
