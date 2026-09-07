"""W4-09: intent ladder layer 2 (C-10).

What these pin is the *escalation policy* rather than the embedder. The embedder
shipped in week 4 is a deterministic lexical stand-in and is expected to be
replaced in week 6; the policy -- layer 1 wins, layer 2 only sees low-confidence
NOISE, layer 3 only sees what layer 2 declines -- is the part that must survive
that swap.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from incidentpilot.domain.intent import Intent, IntentKind, detect_intent
from incidentpilot.transcript.classifier import (
    DEFAULT_THRESHOLD,
    IntentClassifier,
    LexicalEmbedder,
    classify_with,
    cosine,
    load_exemplars,
)

K = IntentKind


@pytest.fixture
def classifier() -> IntentClassifier:
    return IntentClassifier()


# --- the exemplar store -------------------------------------------------------


def test_exemplars_load_and_cover_the_intents_layer_2_can_assign() -> None:
    exemplars = load_exemplars()
    assert K.NOISE not in exemplars, "layer 2 must be able to abstain"
    assert {K.REMEDIATION_START, K.RECOVERY_SIGNAL, K.ESCALATION} <= set(exemplars)
    assert all(phrases for phrases in exemplars.values()), "an empty intent is dead weight"


async def test_no_exemplar_duplicates_a_layer_1_rule() -> None:
    """A phrase layer 1 already matches is a rule change, not an exemplar.

    Left in, it would be unreachable -- layer 2 never sees a message layer 1
    classified -- and the file would slowly become a list of things that do
    nothing, which is how a curated set stops being curated.
    """
    leaked = [
        phrase
        for phrases in load_exemplars().values()
        for phrase in phrases
        if detect_intent(phrase).kind is not K.NOISE
    ]
    assert not leaked, f"these exemplars are already matched by layer 1: {leaked}"


# --- the escalation policy ----------------------------------------------------


async def test_layer_1_is_never_overridden(classifier: IntentClassifier) -> None:
    """A rule match is more trustworthy than a similarity score, always."""
    result = await classifier.classify("rolling back the payments deploy")
    assert result.kind is K.REMEDIATION_START
    assert result.confidence == 0.95


async def test_confident_noise_never_reaches_layer_2() -> None:
    """An emoji must not cost an embedding call -- that is the whole ladder."""
    calls: list[str] = []

    class CountingEmbedder(LexicalEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[dict[str, float]]:
            calls.extend(texts)
            return await super().embed(texts)

    classifier = IntentClassifier(embedder=CountingEmbedder())
    assert (await classifier.classify(":fire:")).kind is K.NOISE
    assert calls == [], "layer 2 ran on a message layer 1 was certain about"


async def test_paraphrase_classified(classifier: IntentClassifier) -> None:
    """W4-09's acceptance case.

    None of these match a layer 1 rule, and all of them are things a responder
    actually types. Catching them is the entire reason layer 2 exists.
    """
    paraphrases = {
        "putting the old version back out now": K.REMEDIATION_START,
        "bouncing the pods again to clear it": K.REMEDIATION_START,
        "the queue has drained": K.RECOVERY_SIGNAL,
        "someone from the database team needs to be in here": K.ESCALATION,
    }
    for message, expected in paraphrases.items():
        assert detect_intent(message).kind is K.NOISE, f"layer 1 already handles {message!r}"
        assert (await classifier.classify(message)).kind is expected, message


async def test_threshold_rejects_ordinary_chatter(classifier: IntentClassifier) -> None:
    """The number in ``DEFAULT_THRESHOLD``, pinned by behaviour rather than prose.

    A layer 2 that fires on "anyone want coffee" would fill a PIR's timeline
    with rows that are not evidence of anything.
    """
    for message in ("anyone want coffee", "standup in five", "happy friday everyone"):
        assert (await classifier.classify(message)).kind is K.NOISE, message


async def test_layer_2_confidence_stays_below_a_rule_match(
    classifier: IntentClassifier,
) -> None:
    """So a timeline row says which layer believed it.

    That distinction is what someone needs when they ask why the PIR thinks
    remediation started at 03:14.
    """
    result = await classifier.classify("putting the old version back out now")
    assert result.kind is K.REMEDIATION_START
    assert result.confidence < 0.70


async def test_layer_3_seam_runs_only_after_layer_2_declines() -> None:
    """W6-14 plugs the ``extract`` role in here; the ordering is fixed now."""
    seen: list[str] = []

    async def fake_extract(text: str) -> Intent | None:
        seen.append(text)
        return Intent(K.STATUS_UPDATE, 0.5, text)

    classifier = IntentClassifier(escalate=fake_extract)

    assert (await classifier.classify("rolling back now")).kind is K.REMEDIATION_START
    assert seen == [], "layer 3 ran on a message layer 1 classified"

    assert (await classifier.classify("the queue has drained")).kind is K.RECOVERY_SIGNAL
    assert seen == [], "layer 3 ran on a message layer 2 classified"

    assert (await classifier.classify("anyone want coffee")).kind is K.STATUS_UPDATE
    assert seen == ["anyone want coffee"]


async def test_absent_classifier_degrades_to_layer_1() -> None:
    """Layer 2 is on the §0.2 cut list; cutting it must not stop ingestion."""
    assert (await classify_with(None, "rolling back now")).kind is K.REMEDIATION_START
    assert (await classify_with(None, "putting the old version back out")).kind is K.NOISE


# --- the embedder itself ------------------------------------------------------


def test_vectors_are_normalized_and_empty_input_is_empty() -> None:
    vector = LexicalEmbedder.vector("rolling back the deploy")
    assert abs(sum(v * v for v in vector.values()) - 1.0) < 1e-9
    assert LexicalEmbedder.vector("") == {}
    assert cosine({}, vector) == 0.0


def test_similarity_is_symmetric_and_self_similarity_is_one() -> None:
    a = LexicalEmbedder.vector("the queue has drained")
    b = LexicalEmbedder.vector("queue drained completely")
    assert abs(cosine(a, a) - 1.0) < 1e-9
    assert cosine(a, b) == pytest.approx(cosine(b, a))


def test_threshold_is_a_deliberate_value() -> None:
    """Guards against someone 'fixing' recall by setting it to zero."""
    assert 0.2 < DEFAULT_THRESHOLD < 0.8
