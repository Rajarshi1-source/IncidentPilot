"""The gate. Deterministic, free, and containing no model (W6-16, INV-05/06).

**A gate that costs money and can itself hallucinate is not a gate.** Everything
here is set membership and token overlap: it runs in microseconds, it produces
the same verdict every time, and its failures are explainable to the person
whose PIR was rejected.

Four checks, in increasing order of cost and decreasing order of certainty:

1. **Uncited claim.** Should be impossible -- ``Claim.citations`` has
   ``min_length=1`` -- and is checked anyway, because an invariant enforced in
   one place is an invariant one refactor away from being enforced nowhere.
2. **Fabricated citation.** The cheapest and most valuable: is this ID in the
   set we actually stored? This is the failure mode that matters (B-09), and it
   is caught by a hash lookup.
3. **Unsupported citation.** A *real* ID attached to an unrelated sentence.
   Normalized token overlap plus shared entities, above a tuned threshold.
4. **Ungrounded number** (INV-06) and **non-participant owner**. Both are cheap
   set membership against things we computed or observed.

``support_threshold`` is the one tunable, and it is a genuine trade-off in both
directions. Too loose and a real ID on an unrelated sentence passes. Too strict
and honest drafts get pushed to the skeleton, quietly killing the feature -- the
worse failure, because it looks like the model being bad rather than the gate
being wrong. Week 7 tunes it on the corpus and reports its false-rejection rate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from incidentpilot.domain.evidence import extract_numbers, is_reference
from incidentpilot.pir.context import GroundedContext
from incidentpilot.pir.schema import ActionItem, PIRDraft, TimelineEntry, walk_claims
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

DEFAULT_SUPPORT_THRESHOLD = 0.35

# Interior dots, slashes and hyphens are kept -- `checkout-api`, `v2.1.0` and
# `kube_pod_status_ready` are single tokens and splitting them would lose the
# strongest signals there are. Trailing punctuation is NOT part of the word: a
# claim ending "...on checkout." must match the service named `checkout`, and
# for a while it did not.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._/-]*[a-z0-9]|[a-z0-9]")

# Words that appear in every incident sentence and carry no evidential weight.
# Kept tiny: a large stop list starts deleting the words incidents are about.
_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "this",
        "that",
        "it",
        "we",
        "with",
        "as",
        "by",
        "from",
        "after",
        "before",
        "during",
        "which",
        "had",
        "has",
    }
)

# Things that identify a specific system rather than describe one: service
# names, versions, shas, ids. A claim and its citation sharing one of these is
# strong evidence they are about the same thing even when the prose differs.
_ENTITY = re.compile(r"\b(?:[a-z0-9]+-[a-z0-9-]+|v?\d+\.\d+[\w.]*|[0-9a-f]{7,40}|U[A-Z0-9]{6,})\b")

FABRICATED = "fabricated"
UNCITED = "uncited"
UNSUPPORTED = "unsupported"
UNGROUNDED_NUMBER = "ungrounded_number"
NON_PARTICIPANT = "non_participant"
MALFORMED = "malformed_reference"


@dataclass(frozen=True, slots=True)
class Finding:
    """One rejection. Named ``Finding`` rather than ``ValidationError`` because
    pydantic already owns that name in this codebase and two of them in one
    traceback is a genuinely confusing minute."""

    path: str
    reason: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason} — {self.detail}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    ok: bool
    errors: tuple[Finding, ...] = ()
    coverage: float = 0.0
    checked_claims: int = 0
    checked_citations: int = 0

    @property
    def messages(self) -> list[str]:
        return [str(e) for e in self.errors]

    def reasons(self) -> set[str]:
        return {e.reason for e in self.errors}

    def as_json(self) -> list[dict[str, str]]:
        return [{"path": e.path, "reason": e.reason, "detail": e.detail} for e in self.errors]


@dataclass
class CitationValidator:
    support_threshold: float = DEFAULT_SUPPORT_THRESHOLD

    def validate(self, draft: PIRDraft, ctx: GroundedContext) -> ValidationReport:
        errors: list[Finding] = []
        valid = ctx.valid_reference_set()
        items = walk_claims(draft)

        supported_claims = 0
        citations_seen = 0

        for path, item in items:
            text = _text_of(item)
            citations = list(item.citations)

            if not citations:
                # The schema should have made this unreachable. Kept because an
                # invariant enforced in one place is one refactor from being
                # enforced nowhere.
                errors.append(Finding(path, UNCITED, "claim has no citations"))
                continue

            claim_ok = True
            for citation in citations:
                citations_seen += 1
                ref = citation.ref.strip()

                if not is_reference(ref):
                    errors.append(Finding(path, MALFORMED, f"not a reference id: {ref!r}"))
                    claim_ok = False
                    continue
                if ref not in valid:
                    # THE check. A well-formed ID that is not in the set we
                    # stored means the model produced a plausible invention.
                    errors.append(
                        Finding(path, FABRICATED, f"{citation.kind}:{ref} was never stored")
                    )
                    claim_ok = False
                    continue
                if not self._supports(ref, text, ctx):
                    errors.append(
                        Finding(path, UNSUPPORTED, f"{ref} does not support the claim text")
                    )
                    claim_ok = False

            if claim_ok:
                supported_claims += 1

        errors.extend(self._check_owners(draft, ctx))
        errors.extend(self._check_numbers(draft, ctx))

        coverage = supported_claims / len(items) if items else 0.0
        report = ValidationReport(
            ok=not errors,
            errors=tuple(errors),
            coverage=round(coverage, 3),
            checked_claims=len(items),
            checked_citations=citations_seen,
        )
        if not report.ok:
            log.warning(
                "pir.validation_failed",
                errors=report.messages[:5],
                coverage=report.coverage,
                reasons=sorted(report.reasons()),
            )
        return report

    # -- the checks ------------------------------------------------------

    def _check_owners(self, draft: PIRDraft, ctx: GroundedContext) -> list[Finding]:
        """An action item owned by someone who was not in the incident.

        The model picks a name off the runbook or a service label. Cheap to
        catch, embarrassing to ship: the document assigns work to a person who
        has never heard of the incident.
        """
        participants = ctx.participants
        out: list[Finding] = []
        for index, item in enumerate(draft.action_items):
            if item.owner and item.owner not in participants:
                out.append(
                    Finding(
                        f"action_items[{index}]",
                        NON_PARTICIPANT,
                        f"{item.owner} did not take part in this incident",
                    )
                )
        return out

    def _check_numbers(self, draft: PIRDraft, ctx: GroundedContext) -> list[Finding]:
        """INV-06: every number must be one we computed.

        This is the other half of B-10. Impact is absent from the schema so the
        model cannot be *asked* for a number; this stops it volunteering one in
        prose. Small integers are excluded by ``extract_numbers`` -- "three
        replicas" is not a claim about magnitude, and rejecting it would push
        honest drafts to the skeleton for nothing.
        """
        allowed = ctx.computed_impact_numbers
        out: list[Finding] = []
        for path, item in walk_claims(draft):
            for number in extract_numbers(_text_of(item)):
                if number not in allowed:
                    out.append(
                        Finding(
                            path,
                            UNGROUNDED_NUMBER,
                            f"{number} was not computed from metrics",
                        )
                    )
        return out

    def _supports(self, ref: str, claim_text: str, ctx: GroundedContext) -> bool:
        """Does the cited artifact plausibly say what the claim says?

        Cheap on purpose: normalized token overlap, or a shared entity. It
        catches the common failure -- a real ID attached to an unrelated
        sentence -- at zero cost and with no model in the loop.
        """
        source = ctx.text_for(ref)
        if source is None:
            return False

        claim_tokens = _tokens(claim_text)
        source_tokens = _tokens(source)
        if not claim_tokens or not source_tokens:
            # An empty message is real evidence of nothing being said, and a
            # claim citing it cannot be checked either way. Accept rather than
            # reject: a false rejection here pushes an honest draft to the
            # skeleton, which is the worse of the two failures.
            return True

        overlap = len(claim_tokens & source_tokens) / len(claim_tokens)
        if overlap >= self.support_threshold:
            return True
        if _entities(claim_text) & _entities(source):
            return True
        # A shared *strong term* -- a service, an alert name, a runbook step --
        # is as good as an entity match and far more common in this domain,
        # where those names are single words. Without this, a claim that cites a
        # deploy by naming the service it went to is rejected as unsupported.
        strong = ctx.strong_terms
        return bool(
            (claim_tokens & strong)
            and (source_tokens & strong)
            and (claim_tokens & source_tokens & strong)
        )


def _text_of(item: object) -> str:
    if isinstance(item, TimelineEntry | ActionItem):
        return item.description
    return getattr(item, "text", "")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 2}


def _entities(text: str) -> set[str]:
    return {m.group(0).lower() for m in _ENTITY.finditer(text)}
