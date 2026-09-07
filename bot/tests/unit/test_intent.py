"""W4-07: the intent ladder's pure layer.

Twenty-five cases, chosen the way a real corpus would be rather than to make the
regexes look good: every rule gets a hit and a near-miss, the ordering hazards
are pinned, and the known code-switching gap is measured instead of omitted.
"""

from __future__ import annotations

import pytest

from incidentpilot.domain.intent import (
    RULES,
    SNAPSHOT_TRIGGERS,
    SUMMARY_CHARS,
    Intent,
    IntentKind,
    detect_intent,
    is_escalatable,
)

K = IntentKind

# (message, expected kind). Twenty-five, and the count is asserted below so a
# future edit cannot quietly shrink the set while the file still claims 25.
CASES: list[tuple[str, IntentKind]] = [
    # -- remediation start -------------------------------------------------
    ("rolling back the payments deploy now", K.REMEDIATION_START),
    ("restarting the api pods", K.REMEDIATION_START),
    ("kubectl rollout undo deployment/checkout", K.REMEDIATION_START),
    ("helm rollback payments 14", K.REMEDIATION_START),
    ("failing over to the replica in ap-south-1", K.REMEDIATION_START),
    ("scaling up the consumer group", K.REMEDIATION_START),
    ("deploying a hotfix for the null pointer", K.REMEDIATION_START),
    # -- remediation end ---------------------------------------------------
    ("rollback is complete", K.REMEDIATION_END),
    ("deployed, watching the graphs", K.REMEDIATION_END),
    ("promotion is done, replica is primary now", K.REMEDIATION_END),
    # -- recovery ----------------------------------------------------------
    ("error rate is down to baseline", K.RECOVERY_SIGNAL),
    ("everything looks green again", K.RECOVERY_SIGNAL),
    ("replication lag is gone", K.RECOVERY_SIGNAL),
    ("traffic is normal", K.RECOVERY_SIGNAL),
    # -- escalation --------------------------------------------------------
    ("escalating to the database team", K.ESCALATION),
    ("paging the secondary", K.ESCALATION),
    ("need help here, this is beyond me", K.ESCALATION),
    ("who owns the notifications service", K.ESCALATION),
    # -- investigation -----------------------------------------------------
    ("checking the dashboard for the last hour", K.INVESTIGATION),
    ("looking at logs for checkout-api", K.INVESTIGATION),
    ("why did the p99 jump at 14:02", K.INVESTIGATION),
    ("what changed in the last deploy", K.INVESTIGATION),
    # -- noise -------------------------------------------------------------
    (":eyes:", K.NOISE),
    ("ok", K.NOISE),
    ("morning all, coffee first", K.NOISE),
]


def test_case_set_is_the_size_it_claims() -> None:
    """A shrinking corpus is the quietest way for coverage to rot."""
    assert len(CASES) == 25


@pytest.mark.parametrize(("message", "expected"), CASES)
def test_intent_rules(message: str, expected: IntentKind) -> None:
    assert detect_intent(message).kind is expected


def test_completion_is_not_read_as_a_fresh_remediation() -> None:
    """The ordering hazard, pinned.

    ``roll ?back`` matches "rollback complete" exactly as happily as "rolling
    back". If REMEDIATION_START were tried first, every completion would be
    filed as a new remediation -- and W4-05 would then take its metric snapshot
    at the moment the work *finished*, which is the one moment the snapshot is
    worthless.
    """
    assert detect_intent("rollback complete").kind is K.REMEDIATION_END
    assert detect_intent("rolling back now").kind is K.REMEDIATION_START


def test_escalation_beats_investigation() -> None:
    """ "need help checking the logs" is a request for people, not for logs."""
    assert detect_intent("need help checking the logs").kind is K.ESCALATION


def test_asserted_intents_are_never_guessed() -> None:
    """STATUS_UPDATE and RUNBOOK_STEP have no rules, on purpose.

    Both are asserted by a slash command in week 5. Inferring them from free
    text would put timeline rows nobody typed into a document people are held
    accountable to.
    """
    rule_kinds = {kind for kind, _, _ in RULES}
    assert K.STATUS_UPDATE not in rule_kinds
    assert K.RUNBOOK_STEP not in rule_kinds


def test_snapshot_triggers_are_the_two_moments_impact_is_measured_between() -> None:
    assert set(SNAPSHOT_TRIGGERS) == {K.REMEDIATION_START, K.RECOVERY_SIGNAL}


def test_summary_is_bounded() -> None:
    """An unbounded description turns a timeline into someone's pasted trace."""
    intent = detect_intent("rolling back " + "x" * 5000)
    assert len(intent.summary) == SUMMARY_CHARS


def test_empty_and_emoji_are_confidently_noise() -> None:
    """Confidence 1.0 is what stops layer 2 spending a call on an emoji."""
    for message in ("", "  ", ":fire:"):
        intent = detect_intent(message)
        assert intent.kind is K.NOISE
        assert intent.confidence == 1.0
        assert not is_escalatable(intent)


def test_unmatched_text_is_escalatable_noise() -> None:
    """The 0.5 is load-bearing: it is what layer 2 keys off (C-10)."""
    intent = detect_intent("morning all, coffee first")
    assert intent.kind is K.NOISE
    assert intent.confidence == 0.5
    assert is_escalatable(intent)


def test_detection_is_deterministic() -> None:
    """INV-01 in behavioural form: same input, same output, no clock, no state."""
    message = "failing over to the replica"
    first = detect_intent(message)
    assert first == detect_intent(message)
    assert isinstance(first, Intent)


# --- the gap we measure rather than hide (§8.3) -------------------------------
#
# Running these split the case in two, which is a more useful answer than the
# blanket "regex is bad at Hinglish" the plan anticipated:
#
#   * when the *technical* token stays English -- "rollback", "logs", "deploy" --
#     layer 1 still fires, because that token is what the rule matches on. Most
#     real code-switching in an engineering channel is this shape.
#   * when the whole clause is Hindi and the verb carries the meaning ("kam ho
#     gaya", "bulao"), there is nothing for a rule to match and it is NOISE.
#
# So the gap is narrower than feared and sharper than described. Both halves go
# into the week 7 corpus and are scored; the split is the honest answer to
# "what is weakest right now?".

CODE_SWITCHED_CAUGHT: list[tuple[str, IntentKind]] = [
    ("bhai rollback kar do abhi", K.REMEDIATION_START),
    ("logs dekh raha hoon", K.INVESTIGATION),
]

CODE_SWITCHED_MISSED: list[tuple[str, IntentKind]] = [
    ("error rate kam ho gaya hai", K.RECOVERY_SIGNAL),
    ("DB team ko bulao yaar", K.ESCALATION),
]


@pytest.mark.parametrize(("message", "expected"), CODE_SWITCHED_CAUGHT)
def test_code_switching_survives_when_the_technical_token_is_english(
    message: str, expected: IntentKind
) -> None:
    assert detect_intent(message).kind is expected


@pytest.mark.xfail(
    strict=False,
    reason=(
        "§8.3: an all-Hindi clause carries its meaning in the verb, and layer 1 "
        "has no rule that can see it. Measured here and scored in the week 7 "
        "corpus rather than omitted; layer 2 does not rescue it either, because "
        "the exemplars are English. A known gap beats an unknown one -- and an "
        "XPASS here would mean someone fixed it."
    ),
)
@pytest.mark.parametrize(("message", "expected"), CODE_SWITCHED_MISSED)
def test_all_hindi_clauses_are_a_known_gap(message: str, expected: IntentKind) -> None:
    assert detect_intent(message).kind is expected
