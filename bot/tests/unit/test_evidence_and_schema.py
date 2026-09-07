"""W6-07, W6-09: the ID grammar and the schema that carries it (INV-05, D1).

The schema tests are the load-bearing ones. `Claim.citations` having
``min_length=1`` is not a validation nicety -- it is the grounding invariant
expressed in the type system, and these assert that an uncited claim genuinely
cannot be constructed rather than merely being discouraged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from incidentpilot.domain.evidence import (
    CitationKind,
    MalformedReference,
    alert_ref,
    deploy_ref,
    extract_numbers,
    is_reference,
    kind_of,
    message_ref,
    metric_ref,
    parse_reference,
    runbook_ref,
    timeline_ref,
)
from incidentpilot.pir.schema import (
    CLAIM_FIELDS,
    ActionItem,
    Citation,
    Claim,
    PIRDraft,
    TimelineEntry,
    walk_claims,
)

AT = datetime(2026, 9, 7, 3, 14, tzinfo=UTC)


def _citation(ref: str = "msg:1757000000.000100") -> Citation:
    return Citation(kind=CitationKind.MESSAGE, ref=ref)


def _claim(text: str = "Checkout returned errors for eleven minutes.") -> Claim:
    return Claim(text=text, citations=[_citation()])


# --- the grammar (W6-07) ------------------------------------------------------


def test_ref_grammar() -> None:
    """Six kinds, six prefixes, and the prefix is what routes the lookup."""
    assert message_ref("1757000000.000100") == "msg:1757000000.000100"
    assert timeline_ref(42) == "tl:42"
    assert alert_ref("abc123") == "alert:abc123"
    assert runbook_ref(7, "verify-replica-lag") == "rb:7#verify-replica-lag"
    assert kind_of("deploy:a1b2c3d4") is CitationKind.DEPLOY


def test_every_kind_round_trips() -> None:
    for ref in (
        "msg:1757000000.000100",
        "tl:99",
        "alert:fp",
        "deploy:abc",
        "rb:1#step",
        metric_ref("up", AT, AT),
    ):
        assert is_reference(ref)
        assert str(parse_reference(ref)) == ref


def test_a_deploy_sha_has_exactly_one_spelling() -> None:
    """A webhook sends forty characters and a human writes seven.

    Two ids for one deploy would make half the citations to it look fabricated
    to a validator that is doing its job correctly.
    """
    full = "A1B2C3D4E5F60718293A4B5C6D7E8F90ABCDEF12"
    assert deploy_ref(full) == deploy_ref(full.lower())
    assert deploy_ref(full) == deploy_ref(full[:12])


def test_a_metric_ref_carries_its_window() -> None:
    """The same query over a different window is a different fact."""
    later = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    assert metric_ref("up", AT, AT) != metric_ref("up", AT, later)
    assert metric_ref("up", AT, later) == metric_ref("up", AT, later)


def test_a_metric_ref_survives_json() -> None:
    """A PromQL expression is full of braces, quotes and commas.

    Embedding it whole would produce an id nobody can put in a JSON string
    without escaping, and an id that gets mangled is an id that looks
    fabricated.
    """
    expr = 'sum(increase(http_requests_total{service="x",status=~"5.."}[5m]))'
    ref = metric_ref(expr, AT, AT)
    assert "{" not in ref and '"' not in ref and " " not in ref


@pytest.mark.parametrize(
    "bad", ["", "msg", "1757000000.000100", "message:123", "msg:", "see msg:123 for details"]
)
def test_a_malformed_reference_is_a_different_failure(bad: str) -> None:
    """Syntactically invalid means "did not understand the grammar".

    A well-formed id that is not in the valid set means "invented a plausible
    one", and the second is the interesting failure. Collapsing them would lose
    the distinction in every log line.
    """
    assert not is_reference(bad)
    with pytest.raises(MalformedReference):
        parse_reference(bad)


# --- number extraction (INV-06) -----------------------------------------------


def test_extract_numbers_finds_magnitudes() -> None:
    found = extract_numbers("Errors peaked at 1432 requests, or 12.7% of traffic.")
    assert "1432" in found
    assert "12.7" in found


def test_small_integers_are_not_claims_about_magnitude() -> None:
    """ "three of the replicas", "step 4", "P0" -- rejecting these would push
    honest drafts to the skeleton and catch nothing that matters."""
    assert extract_numbers("Two of the 3 replicas were unready at step 4.") == set()


def test_identifiers_are_not_numbers() -> None:
    """sev1, p99, http2, 5xx are names, not measurements."""
    assert extract_numbers("sev1 p99 http2 5xx") == set()


def test_thousands_separators_normalize() -> None:
    """The model writes 1,432 and the impact block computed 1432."""
    assert "1432" in extract_numbers("about 1,432 failed requests")


# --- the schema (W6-09, INV-05) -----------------------------------------------


def test_uncited_claim_fails_parse() -> None:
    """THE invariant. An uncited claim is unrepresentable, not merely invalid.

    It fails at parse time -- before the validator, before any business logic,
    before anything can decide to be lenient about it.
    """
    with pytest.raises(ValidationError):
        Claim(text="Something happened.", citations=[])


def test_a_draft_of_uncited_claims_cannot_be_built() -> None:
    with pytest.raises(ValidationError):
        PIRDraft.model_validate({"summary": [{"text": "It broke.", "citations": []}]})


def test_impact_is_absent_from_the_schema() -> None:
    """B-10 in the type system.

    If `impact` were a field, the model would fill it -- and a plausible
    invented number in a document titled "Post-Incident Review" is the failure
    this whole project is built around. It is computed and injected instead.
    """
    assert "impact" not in PIRDraft.model_fields
    for forbidden in ("users_affected", "revenue_impact", "confidence"):
        assert forbidden not in PIRDraft.model_fields


def test_extra_fields_are_forbidden() -> None:
    """A model that invents a field is improvising the rest of the object too."""
    with pytest.raises(ValidationError):
        PIRDraft.model_validate({"summary": [_claim().model_dump()], "estimated_cost_usd": 40000})


def test_root_cause_is_nullable_and_defaults_to_unknown() -> None:
    """A schema that REQUIRES a root cause guarantees the model invents one for
    the incidents where nobody knows -- exactly where a confident wrong answer
    does the most damage."""
    draft = PIRDraft(summary=[_claim()])
    assert draft.root_cause_hypothesis is None


def test_action_item_priority_is_constrained() -> None:
    with pytest.raises(ValidationError):
        ActionItem(
            description="Fix it", priority="urgent", category="reliability", citations=[_citation()]
        )


def test_every_claim_field_is_walked() -> None:
    """The guard against the exact bug this module exists to prevent.

    ``walk_claims`` is driven by ``CLAIM_FIELDS`` rather than by
    ``model_fields``, so adding a section to the schema without adding it here
    would create a section the validator never checks. This test is what turns
    that into a failure rather than a silent hole.
    """
    schema_fields = set(PIRDraft.model_fields)
    assert set(CLAIM_FIELDS) == schema_fields, (
        "PIRDraft and CLAIM_FIELDS have diverged — the validator would skip "
        f"{schema_fields ^ set(CLAIM_FIELDS)}"
    )


def test_walk_claims_reaches_every_section() -> None:
    draft = PIRDraft(
        summary=[_claim("Summary claim about checkout errors.")],
        timeline=[TimelineEntry(at=AT, description="Rollback started.", citations=[_citation()])],
        contributing_factors=[_claim("A contributing factor.")],
        what_went_well=[_claim("Detection was fast.")],
        what_went_wrong=[_claim("The runbook was stale.")],
        root_cause_hypothesis=_claim("A config change removed the pool ceiling."),
        action_items=[
            ActionItem(
                description="Restore the ceiling.",
                priority="P0",
                category="reliability",
                citations=[_citation()],
            )
        ],
    )
    paths = {path.split("[")[0] for path, _ in walk_claims(draft)}
    assert paths == set(CLAIM_FIELDS)
