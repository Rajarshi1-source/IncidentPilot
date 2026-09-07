"""The 13-state incident lifecycle.

PURE MODULE (INV-01).

The transition table exists twice: as the ``TRANSITIONS`` dict here, which
catches programmer error at development time, and as ``UNIQUE (incident_id,
seq)`` in the schema, which catches concurrency error at 3 a.m. Never rely on
"there's only one worker", because there is never only one worker.

Each state beyond a naive linear machine exists for a concrete reason, and the
reason is written next to it -- a state you cannot justify is a state that will
be misused.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class S(StrEnum):
    DETECTED = "detected"
    TRIAGING = "triaging"
    ENGAGED = "engaged"
    ACKNOWLEDGED = "acknowledged"
    MITIGATING = "mitigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    PIR_DRAFTING = "pir_drafting"
    PIR_DRAFTED = "pir_drafted"
    PIR_FAILED = "pir_failed"
    CLOSED = "closed"
    MERGED = "merged"
    FALSE_POSITIVE = "false_positive"
    ABANDONED = "abandoned"


# Terminal states own no channel and accept no further transitions.
TERMINAL: frozenset[S] = frozenset({S.CLOSED, S.MERGED, S.FALSE_POSITIVE, S.ABANDONED})

# Why each non-obvious state exists:
#
#   acknowledged   -- without it, `acknowledged_at` has no transition behind it
#                     and the headline time-to-acknowledge metric is unbacked.
#   merged         -- required by storm compression; a merged incident is
#                     terminal and owns no channel.
#   false_positive -- flapping alerts must not generate PIRs, or the
#                     PIR-completion metric is noise.
#   mitigated      -- NOT the same as resolved: impact stopped versus work
#                     finished. Separating them is what lets time-to-mitigate be
#                     reported on its own, which is the number users actually feel.
#   reopened       -- premature resolution is common and must not create a
#                     second incident.
#   abandoned      -- otherwise the active-incidents panel fills with zombies.
#   pir_failed     -- a branch that still reaches pir_drafted via the skeleton,
#                     so the product never blocks on a model.
TRANSITIONS: dict[S, frozenset[S]] = {
    S.DETECTED: frozenset({S.TRIAGING, S.FALSE_POSITIVE}),
    S.TRIAGING: frozenset({S.ENGAGED, S.MERGED, S.FALSE_POSITIVE}),
    S.ENGAGED: frozenset({S.ACKNOWLEDGED, S.MITIGATED, S.ABANDONED, S.FALSE_POSITIVE}),
    S.ACKNOWLEDGED: frozenset({S.MITIGATING, S.MITIGATED, S.ABANDONED}),
    S.MITIGATING: frozenset({S.MITIGATED, S.ACKNOWLEDGED}),  # remediation failed, go back
    S.MITIGATED: frozenset({S.RESOLVED, S.MITIGATING}),  # regression, go back
    S.RESOLVED: frozenset({S.PIR_DRAFTING, S.REOPENED}),
    S.REOPENED: frozenset({S.ACKNOWLEDGED}),
    S.PIR_DRAFTING: frozenset({S.PIR_DRAFTED, S.PIR_FAILED}),
    S.PIR_FAILED: frozenset({S.PIR_DRAFTING, S.PIR_DRAFTED}),  # retry, or accept skeleton
    S.PIR_DRAFTED: frozenset({S.CLOSED}),
}

# Which timestamp column a transition stamps. Timestamps ONLY -- tta_seconds,
# ttm_seconds and mttr_seconds are GENERATED ALWAYS AS ... STORED columns, so
# there is exactly one definition of MTTR in the system and it lives in the
# schema. Every "our MTTR numbers disagree" argument comes from two services
# computing it differently.
TIMING_FIELD: dict[S, str] = {
    S.ENGAGED: "engaged_at",
    S.ACKNOWLEDGED: "acknowledged_at",
    S.MITIGATED: "mitigated_at",
    S.RESOLVED: "resolved_at",
    S.CLOSED: "closed_at",
}


class InvalidTransition(Exception):
    """The requested transition is not permitted from the current state."""


def assert_transition(cur: S, nxt: S) -> None:
    """Raise unless ``cur -> nxt`` is a legal edge."""
    if cur in TERMINAL:
        raise InvalidTransition(f"{cur} is terminal")
    if nxt not in TRANSITIONS.get(cur, frozenset()):
        raise InvalidTransition(f"{cur} -> {nxt} not permitted")


def is_terminal(state: S) -> bool:
    return state in TERMINAL


def reachable_from(cur: S) -> frozenset[S]:
    return TRANSITIONS.get(cur, frozenset())


@dataclass(frozen=True, slots=True)
class TransitionEffect:
    """Every transition declares its side effects AND its compensation.

    Keeping effects in a lookup table rather than scattered ``if`` branches
    means the machine stays readable and the compensation is impossible to
    forget, because it sits next to the thing it undoes.
    """

    outbox: tuple[str, ...] = ()
    compensate: tuple[str, ...] = ()


# Conflict C-05: this is the starter code's table, not Rev 2's. Rev 2 omits
# (PIR_FAILED -> PIR_DRAFTED), which means that transition fires no outbox event
# -- so the skeleton PIR is generated and persisted but never posted. The
# incident reaches pir_drafted with nothing visible in the channel, silently
# failing the week 6 gate while every unit test still passes.
EFFECTS: dict[tuple[S, S], TransitionEffect] = {
    # Order is dispatch order -- the relay claims by id ascending -- and
    # `page_responder` sits second on purpose. Waking the right human is the
    # SLO (time-to-acknowledge); the pinned runbook and the live timer are
    # decorations by comparison, and a war room nobody has been told about is
    # not a war room. W5 added it here rather than to a new transition because
    # "someone should be paged" is a property of becoming engaged, not a
    # separate event that could be missed.
    (S.TRIAGING, S.ENGAGED): TransitionEffect(
        outbox=(
            "create_channel",
            "page_responder",
            "invite_responders",
            "pin_runbook",
            "start_timer",
        ),
        compensate=("archive_channel",),
    ),
    (S.TRIAGING, S.MERGED): TransitionEffect(outbox=("post_merge_notice",)),
    (S.RESOLVED, S.PIR_DRAFTING): TransitionEffect(outbox=("post_generating_notice",)),
    (S.PIR_DRAFTING, S.PIR_DRAFTED): TransitionEffect(outbox=("post_pir", "notify_reviewers")),
    (S.PIR_FAILED, S.PIR_DRAFTED): TransitionEffect(outbox=("post_pir_skeleton",)),
}


def effect_for(cur: S, nxt: S) -> TransitionEffect:
    return EFFECTS.get((cur, nxt), TransitionEffect())
