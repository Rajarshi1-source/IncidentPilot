"""W5-06: the fatigue score and its three bands (D5, C-04).

The scorer is pure, so these are ordinary function tests. What they pin is not
the arithmetic -- any weighted sum would produce a number -- but the two
properties that make the feature defensible: the top band never removes the
primary silently, and the score can always explain itself.
"""

from __future__ import annotations

import pytest

from incidentpilot.domain.fatigue import (
    INVITE_SECONDARY_AT,
    REROUTE_AT,
    ResponderWindow,
    Routing,
    fatigue_components,
    fatigue_score,
    routing_decision,
    top_reasons,
)

FRESH = ResponderWindow(responder="U1")


def test_a_fresh_responder_scores_zero() -> None:
    assert fatigue_score(FRESH) == 0.0
    assert routing_decision(0.0) is Routing.PAGE_PRIMARY


def test_weights_sum_to_one() -> None:
    """So a fully saturated responder scores exactly 1.0 and not 0.93.

    A score whose ceiling is not 1.0 makes every threshold in the band table a
    number nobody can reason about without re-deriving the weights.
    """
    assert sum(c.weight for c in fatigue_components(FRESH)) == pytest.approx(1.0)


def test_every_term_saturates() -> None:
    """Fourteen pages and four pages both mean "this person has had enough".

    Without saturation one extreme input would dominate the other four and the
    five-signal score would quietly become a one-signal one.
    """
    extreme = ResponderWindow(
        responder="U1",
        pages_8h=99,
        night_pages_24h=99,
        incident_minutes_24h=9999,
        consecutive_oncall_days=99,
        sev1_count_7d=99,
    )
    assert fatigue_score(extreme) == 1.0
    assert all(c.ratio <= 1.0 for c in fatigue_components(extreme))


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (FRESH, Routing.PAGE_PRIMARY),
        # 2 pages in 8h = 0.30 exactly: still the primary's incident.
        (ResponderWindow("U1", pages_8h=2), Routing.PAGE_PRIMARY),
        # 0.30 + 0.25 = 0.55: over the invite line, under the reroute line.
        (
            ResponderWindow("U1", pages_8h=2, night_pages_24h=2),
            Routing.PAGE_PRIMARY_AND_INVITE_SECONDARY,
        ),
        # 0.30 + 0.25 + 0.20 = 0.75: the boundary is inclusive of the top band.
        (
            ResponderWindow("U1", pages_8h=2, night_pages_24h=2, incident_minutes_24h=240),
            Routing.PAGE_SECONDARY_NOTIFY_PRIMARY,
        ),
    ],
)
def test_fatigue_bands(window: ResponderWindow, expected: Routing) -> None:
    assert routing_decision(fatigue_score(window)) is expected


def test_band_boundaries_are_named_constants() -> None:
    """0.75 appears in the copy, in the tests and in the interview answer.

    Three literals would drift; one constant cannot.
    """
    assert routing_decision(INVITE_SECONDARY_AT - 0.001) is Routing.PAGE_PRIMARY
    assert routing_decision(INVITE_SECONDARY_AT) is Routing.PAGE_PRIMARY_AND_INVITE_SECONDARY
    assert routing_decision(REROUTE_AT - 0.001) is Routing.PAGE_PRIMARY_AND_INVITE_SECONDARY
    assert routing_decision(REROUTE_AT) is Routing.PAGE_SECONDARY_NOTIFY_PRIMARY


def test_the_top_band_still_involves_the_primary() -> None:
    """The differentiator, asserted at the level it is decided.

    ``page_secondary_notify_primary`` is deliberately not
    ``page_secondary_instead``. The name is the promise.
    """
    assert Routing.PAGE_SECONDARY_NOTIFY_PRIMARY.value.endswith("notify_primary")


def test_night_pages_matter_more_per_page_than_daytime_ones() -> None:
    """0.25 over a ceiling of 2 versus 0.30 over a ceiling of max_pages.

    Being woken at 03:00 twice should not score the same as two pages during a
    working afternoon, and this is the assertion that says so.
    """
    day = fatigue_score(ResponderWindow("U1", pages_8h=1))
    night = fatigue_score(ResponderWindow("U1", pages_8h=1, night_pages_24h=1))
    assert night > day


def test_incident_time_counts_even_with_one_page() -> None:
    """One page and four hours in a war room is the exhausting shape.

    A page-count-only score reads that as a quiet day, which is exactly the
    failure mode this term exists to cover.
    """
    assert fatigue_score(ResponderWindow("U1", pages_8h=1, incident_minutes_24h=240)) > 0.3


def test_reasons_explain_the_score_and_omit_the_zeroes() -> None:
    """ "0 sev1 in 7 days" as a reason someone is tired would be absurd.

    The announcement has to survive being read by the person it is about.
    """
    window = ResponderWindow("U1", pages_8h=3, night_pages_24h=2)
    reasons = top_reasons(window)
    assert any("page" in r for r in reasons)
    assert not any("sev1" in r for r in reasons)
    assert len(reasons) <= 3


def test_max_pages_is_configurable_without_touching_the_bands() -> None:
    """A team that expects four pages a shift is not a team in crisis.

    The ceiling moves; the band boundaries do not, because what "0.75 means"
    should be the same sentence everywhere.
    """
    window = ResponderWindow("U1", pages_8h=2)
    assert fatigue_score(window, max_pages=2) > fatigue_score(window, max_pages=4)


def test_the_scorer_is_pure() -> None:
    """INV-01/C-04 behaviourally: same window, same score, no clock anywhere.

    Rev 2's signature takes a ``now`` it never uses. Taking it would put a clock
    in ``domain/`` and make the week 7 replay non-deterministic.
    """
    window = ResponderWindow("U1", pages_8h=2, night_pages_24h=1)
    assert fatigue_score(window) == fatigue_score(window)
    import inspect

    assert "now" not in inspect.signature(fatigue_score).parameters
