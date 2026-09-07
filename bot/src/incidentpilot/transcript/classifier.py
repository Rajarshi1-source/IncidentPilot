"""Intent ladder layers 2 and 3 (W4-09, W6-14, C-10).

C-10: both the Slack skill and Rev 2 describe a three-layer ladder -- regex,
then embedding similarity against labelled exemplars, then the ``extract`` model
role -- but the repo tree only ever had ``domain/intent.py``, which is layer 1
and must stay pure (INV-01). Layers 2 and 3 reach an embedding provider and the
model router, so they live here, outside ``domain/``, and compose the pure layer
rather than replacing it.

**The escalation policy is the whole design.** Layer 1 answers ~80% of real
incident chatter at zero cost. Layer 2 runs only on what layer 1 returns as
*low-confidence* NOISE, so an emoji or a one-word ack never costs anything. Layer
3 (the model) runs only on what layer 2 also declines, batched, and arrives in
week 6 through the seam left at the bottom of this file.

**The embedder shipped here is deterministic and local, by design.** A clean
clone runs the whole demo with no credentials and no model download, and the
week 7 replay harness performs zero network calls (INV-10) -- both would be
false if layer 2 required a hosted embedding endpoint. It is a lexical
approximation, not a semantic one: it catches re-orderings and morphological
variants of phrasings in ``exemplars.yaml`` and misses genuine synonymy. That
ceiling is why ``EmbeddingProvider`` is a Protocol, and swapping in a real
provider in week 6 is a constructor argument rather than a rewrite.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

import yaml

from incidentpilot.domain.intent import (
    SUMMARY_CHARS,
    Intent,
    IntentKind,
    detect_intent,
    is_escalatable,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

EXEMPLARS_PATH = Path(__file__).with_name("exemplars.yaml")

# Tuned on the exemplar set: high enough that ordinary chatter does not collide
# with a remediation phrase, low enough that a re-ordering of one is caught.
# Layer 2 is *advisory* -- it produces timeline rows, not state transitions -- so
# the cost of a false positive is a slightly noisy timeline, while the cost of a
# false negative is a gap in the evidence a PIR is built from. The threshold
# leans accordingly, and the number belongs in a test rather than in prose:
# ``test_threshold_rejects_ordinary_chatter`` is what actually pins it.
DEFAULT_THRESHOLD = 0.42

# Layer 2 can never be as sure as a literal rule match. Capping its confidence
# below layer 1's weakest rule keeps the two distinguishable on a timeline row,
# which matters when someone asks why a PIR believes remediation started at
# 03:14.
MAX_LAYER2_CONFIDENCE = 0.65

_WORD = re.compile(r"[a-z0-9]+")

# Deliberately tiny. A large stop list starts deleting the words incidents are
# about ("down", "up", "out", "back") and those carry most of the signal here.
_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "at", "for", "in", "is", "it", "of", "on", "the", "to", "we"}
)

_CHAR_NGRAM = 3


class EmbeddingProvider(Protocol):
    """The slice of an embedding backend layer 2 needs.

    Async and batched because the real implementations are both: one HTTP round
    trip for a list of strings, not one per string.
    """

    async def embed(self, texts: Sequence[str]) -> list[dict[str, float]]: ...


class LexicalEmbedder:
    """A deterministic sparse embedder: word tokens plus character trigrams.

    Sparse dict-of-float vectors rather than dense arrays, so there is no numpy
    dependency and no fixed dimensionality to collide in -- feature hashing
    would trade a real dependency for a synthetic source of false positives,
    which is a bad trade for something whose job is to be boring.
    """

    async def embed(self, texts: Sequence[str]) -> list[dict[str, float]]:
        return [self.vector(t) for t in texts]

    @staticmethod
    def vector(text: str) -> dict[str, float]:
        lowered = text.lower()
        words = [w for w in _WORD.findall(lowered) if w not in _STOPWORDS]
        features: Counter[str] = Counter(f"w:{w}" for w in words)

        # Character trigrams over the word stream give morphological tolerance
        # for free: "revert" and "reverting" share four of them, and "error" and
        # "errors" all but one. That is most of what a small embedding model
        # buys at this scale, at none of the cost.
        joined = " ".join(words)
        for i in range(len(joined) - _CHAR_NGRAM + 1):
            features[f"c:{joined[i : i + _CHAR_NGRAM]}"] += 1

        norm = math.sqrt(sum(v * v for v in features.values()))
        if norm == 0:
            return {}
        return {k: v / norm for k, v in features.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Both vectors arrive L2-normalized, so this is a plain dot product."""
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(feature, 0.0) for feature, weight in a.items())


def load_exemplars(path: Path | None = None) -> dict[IntentKind, list[str]]:
    """Read ``exemplars.yaml``. Unknown intent labels fail loudly.

    A typo'd key would otherwise silently drop a whole intent's exemplars, and
    the only symptom would be classification quietly getting worse.
    """
    raw = yaml.safe_load((path or EXEMPLARS_PATH).read_text(encoding="utf-8")) or {}
    out: dict[IntentKind, list[str]] = {}
    for label, phrases in raw.items():
        kind = IntentKind(str(label))
        if kind is IntentKind.NOISE:
            raise ValueError("exemplars for NOISE would make layer 2 unable to abstain")
        out[kind] = [str(p) for p in phrases or []]
    return out


class IntentClassifier:
    """The full ladder: pure rules, then similarity, then (week 6) the model.

    ``escalate`` is the layer 3 seam. It stays None in week 4 -- there is no
    model adapter yet -- and W6-14 passes the ``extract`` role's batched
    callable. Leaving the parameter in place now means week 6 wires a function
    in rather than re-cutting the composition.
    """

    def __init__(
        self,
        *,
        embedder: EmbeddingProvider | None = None,
        exemplars: dict[IntentKind, list[str]] | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        escalate: Callable[[str], Awaitable[Intent | None]] | None = None,
    ) -> None:
        self._embedder = embedder or LexicalEmbedder()
        self._exemplars = exemplars if exemplars is not None else load_exemplars()
        self._threshold = threshold
        self._escalate = escalate
        self._index: list[tuple[IntentKind, dict[str, float]]] | None = None

    async def _ensure_index(self) -> list[tuple[IntentKind, dict[str, float]]]:
        """Embed the exemplar set once, lazily.

        Lazily rather than in ``__init__`` because embedding is async and a
        constructor that cannot await would force every caller into a two-step
        build for a cache that most processes touch a handful of times.
        """
        if self._index is None:
            index: list[tuple[IntentKind, dict[str, float]]] = []
            for kind, phrases in self._exemplars.items():
                for vector in await self._embedder.embed(phrases):
                    index.append((kind, vector))
            self._index = index
        return self._index

    async def classify(self, text: str) -> Intent:
        """Layer 1, then layer 2 only on low-confidence NOISE, then layer 3."""
        intent = detect_intent(text)
        if not is_escalatable(intent):
            return intent

        kind, score = await self.best_match(text)
        if kind is not None and score >= self._threshold:
            log.debug("intent.layer2", kind=str(kind), score=round(score, 3))
            return Intent(
                kind=kind,
                confidence=min(round(score, 2), MAX_LAYER2_CONFIDENCE),
                summary=text.strip()[:SUMMARY_CHARS],
            )

        if self._escalate is not None:
            escalated = await self._escalate(text)
            if escalated is not None:
                return escalated

        return intent

    async def best_match(self, text: str) -> tuple[IntentKind | None, float]:
        """Nearest exemplar and its similarity. Exposed so tests can pin the score."""
        index = await self._ensure_index()
        (probe,) = await self._embedder.embed([text])
        if not probe or not index:
            return None, 0.0

        best_kind: IntentKind | None = None
        best_score = 0.0
        for kind, vector in index:
            score = cosine(probe, vector)
            if score > best_score:
                best_kind, best_score = kind, score
        return best_kind, best_score


async def classify_with(classifier: IntentClassifier | None, text: str) -> Intent:
    """Run the ladder, or just layer 1 when no classifier is configured.

    The ingestor calls this rather than branching itself: layer 2 is on the
    §0.2 cut list, and a system that has cut it must still ingest transcripts
    with the pure layer intact.
    """
    if classifier is None:
        return detect_intent(text)
    return await classifier.classify(text)


__all__: list[str] = [
    "DEFAULT_THRESHOLD",
    "EmbeddingProvider",
    "IntentClassifier",
    "LexicalEmbedder",
    "classify_with",
    "cosine",
    "load_exemplars",
]
