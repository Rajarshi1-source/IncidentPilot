"""The 13-state machine (W2-07, B-13, C-05)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from incidentpilot.domain.states import (
    EFFECTS,
    TERMINAL,
    TIMING_FIELD,
    TRANSITIONS,
    InvalidTransition,
    S,
    assert_transition,
    effect_for,
    is_terminal,
    reachable_from,
)


def test_there_are_thirteen_reachable_states_plus_two_terminal_branches() -> None:
    """Fifteen enum members: the 13-state lifecycle plus merged and abandoned,
    which Rev 2 counts inside the thirteen. The count that matters is that every
    state below is justified, not the label on the number."""
    assert len(list(S)) == 15
    assert len(TERMINAL) == 4


@given(st.lists(st.sampled_from(list(S)), min_size=1, max_size=8))
def test_no_path_reaches_invalid_state(path: list[S]) -> None:
    """G2, second criterion. No sequence of attempted transitions -- legal or
    not -- can leave the machine in something that is not a state."""
    cur = S.DETECTED
    for nxt in path:
        try:
            assert_transition(cur, nxt)
            cur = nxt
        except InvalidTransition:
            pass
    assert cur in set(S)


@pytest.mark.parametrize("state", sorted(TERMINAL))
def test_terminal_states_accept_nothing(state: S) -> None:
    for target in S:
        with pytest.raises(InvalidTransition, match="terminal"):
            assert_transition(state, target)


def test_every_transition_in_the_table_is_permitted() -> None:
    for cur, allowed in TRANSITIONS.items():
        for nxt in allowed:
            assert_transition(cur, nxt)  # must not raise


def test_transitions_not_in_the_table_are_refused() -> None:
    with pytest.raises(InvalidTransition, match="not permitted"):
        assert_transition(S.DETECTED, S.RESOLVED)  # cannot skip the whole lifecycle
    with pytest.raises(InvalidTransition, match="not permitted"):
        assert_transition(S.ENGAGED, S.CLOSED)  # closing needs a PIR first
    with pytest.raises(InvalidTransition, match="not permitted"):
        assert_transition(S.RESOLVED, S.ENGAGED)  # reopen goes via REOPENED


def test_acknowledged_is_backed_by_a_transition() -> None:
    """B-13: Rev 1 stored acknowledged_at with no state to produce it, so the
    headline time-to-acknowledge metric had nothing behind it."""
    assert S.ACKNOWLEDGED in reachable_from(S.ENGAGED)
    assert TIMING_FIELD[S.ACKNOWLEDGED] == "acknowledged_at"


def test_mitigated_is_not_resolved() -> None:
    """Impact stopped versus work finished. Separating them is what lets
    time-to-mitigate be reported on its own."""
    assert S.RESOLVED in reachable_from(S.MITIGATED)
    assert TIMING_FIELD[S.MITIGATED] == "mitigated_at"
    assert TIMING_FIELD[S.RESOLVED] == "resolved_at"


def test_remediation_can_fail_and_go_back() -> None:
    assert S.ACKNOWLEDGED in reachable_from(S.MITIGATING)
    assert S.MITIGATING in reachable_from(S.MITIGATED)


def test_reopen_does_not_create_a_second_incident() -> None:
    assert S.REOPENED in reachable_from(S.RESOLVED)
    assert reachable_from(S.REOPENED) == frozenset({S.ACKNOWLEDGED})


def test_pir_failure_still_reaches_drafted() -> None:
    """The product never blocks on a model: a failed generation accepts the
    deterministic skeleton and the incident still completes."""
    assert S.PIR_DRAFTED in reachable_from(S.PIR_FAILED)


def test_every_state_except_detected_is_reachable() -> None:
    """A state nobody can reach is dead code that looks like a feature."""
    reached = {S.DETECTED}
    frontier = [S.DETECTED]
    while frontier:
        cur = frontier.pop()
        for nxt in reachable_from(cur):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    assert reached == set(S), f"unreachable states: {sorted(set(S) - reached)}"


def test_every_transition_target_is_a_real_state() -> None:
    for allowed in TRANSITIONS.values():
        for nxt in allowed:
            assert nxt in set(S)


def test_terminal_states_have_no_outgoing_edges_in_the_table() -> None:
    for state in TERMINAL:
        assert state not in TRANSITIONS


# --- effects (C-05) -----------------------------------------------------------


def test_engagement_declares_its_compensation() -> None:
    """A failed engagement must archive the channel it created, or the
    workspace fills with debris."""
    effect = effect_for(S.TRIAGING, S.ENGAGED)
    assert "create_channel" in effect.outbox
    assert effect.compensate == ("archive_channel",)


def test_skeleton_pir_is_actually_posted() -> None:
    """Conflict C-05, asserted directly.

    Rev 2's EFFECTS table omits this edge. Without it the skeleton is generated
    and persisted but never posted -- the incident reaches pir_drafted with
    nothing visible in the channel, silently failing the week 6 gate while every
    unit test still passes.
    """
    effect = effect_for(S.PIR_FAILED, S.PIR_DRAFTED)
    assert effect.outbox == ("post_pir_skeleton",), (
        "the skeleton PIR would be persisted but never posted"
    )


def test_merge_announces_itself() -> None:
    assert effect_for(S.TRIAGING, S.MERGED).outbox == ("post_merge_notice",)


def test_every_effect_key_is_a_legal_transition() -> None:
    """An effect on an impossible edge is a side effect that can never fire."""
    for (cur, nxt), _ in EFFECTS.items():
        assert nxt in TRANSITIONS.get(cur, frozenset()), f"{cur} -> {nxt} is not a legal edge"


def test_transitions_without_effects_return_an_empty_effect() -> None:
    effect = effect_for(S.ENGAGED, S.ACKNOWLEDGED)
    assert effect.outbox == ()
    assert effect.compensate == ()


def test_is_terminal_matches_the_set() -> None:
    for state in S:
        assert is_terminal(state) == (state in TERMINAL)
