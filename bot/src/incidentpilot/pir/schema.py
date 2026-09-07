"""The PIR schema — where the grounding invariant lives (W6-09, INV-05).

**The whole idea in one line:** `Claim.citations` has `min_length=1`, so an
uncited claim is *unrepresentable*. Malformed output fails at parse time, before
any business logic, before the validator, before anything can decide to be
lenient about it. Prompt instructions are advisory; a schema is not.

Two absences carry as much weight as the fields:

* **`impact` is not here.** It is computed by ``impact/promql.py`` and injected
  into the prompt as verified facts (B-10, INV-06). If it were a schema field
  the model would fill it, and a plausible invented number in a document titled
  "Post-Incident Review" is the failure this project is built around.
* **`confidence` is not here either.** Self-reported confidence correlates with
  fluency, not correctness (B-09). It is worth recording as telemetry and it is
  never worth branching on, so it is not part of the contract.

And one deliberate nullable: `root_cause_hypothesis`. A schema that *requires* a
root cause guarantees the model invents one for the incidents where nobody
actually knows -- which are precisely the incidents where a confident wrong
answer does the most damage.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from incidentpilot.domain.evidence import CitationKind

# Long enough for a real sentence, short enough that a paragraph cannot hide
# three uncited assertions behind one citation.
CLAIM_MAX_CHARS = 600


class Citation(BaseModel):
    """A pointer into the evidence graph. ``extra="forbid"`` on purpose.

    A model that adds ``{"kind": ..., "ref": ..., "quote": "..."}`` is inventing
    a field, and an invented field is the first sign that the rest of the object
    is being improvised too. Failing the parse is cheaper than discovering it in
    the validator.
    """

    kind: CitationKind
    ref: str = Field(min_length=3, max_length=200)
    model_config = ConfigDict(extra="forbid")


class Claim(BaseModel):
    """The atomic unit of a PIR. Prose without a citation cannot exist here."""

    text: str = Field(min_length=3, max_length=CLAIM_MAX_CHARS)
    citations: list[Citation] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class TimelineEntry(BaseModel):
    """A moment, cited.

    The timestamp is required and typed: a timeline whose entries carry
    free-text times ("shortly after the deploy") cannot be ordered, and an
    unorderable timeline is a narrative, not evidence.
    """

    at: datetime
    description: str = Field(min_length=3, max_length=CLAIM_MAX_CHARS)
    citations: list[Citation] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class ActionItem(BaseModel):
    """What to do next, and who said it was needed.

    ``owner`` is nullable and the validator checks it against the incident's
    participants when set. Assigning work to somebody who was not in the
    incident is a specific, common and embarrassing failure -- the model picks a
    name from the runbook or from a service label.
    """

    description: str = Field(min_length=3, max_length=CLAIM_MAX_CHARS)
    owner: str | None = None
    priority: str = Field(pattern=r"^P[0-2]$")
    category: str = Field(min_length=2, max_length=64)
    citations: list[Citation] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class PIRDraft(BaseModel):
    """What the model is allowed to produce. Nothing else parses.

    ``summary`` is capped at five claims because a summary that runs to a page
    is not a summary, and an unbounded list is how a model fills space when it
    has little to say.
    """

    summary: list[Claim] = Field(min_length=1, max_length=5)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    contributing_factors: list[Claim] = Field(default_factory=list)
    what_went_well: list[Claim] = Field(default_factory=list)
    what_went_wrong: list[Claim] = Field(default_factory=list)
    root_cause_hypothesis: Claim | None = None
    action_items: list[ActionItem] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


# Every field that holds citable prose, in the order a reader meets them. Used
# by the validator to walk the draft and by the renderer to lay it out -- one
# list, so a new section cannot be added to the schema and silently skipped by
# the thing that checks it.
CLAIM_FIELDS: tuple[str, ...] = (
    "summary",
    "timeline",
    "contributing_factors",
    "what_went_well",
    "what_went_wrong",
    "root_cause_hypothesis",
    "action_items",
)


def walk_claims(draft: PIRDraft) -> list[tuple[str, Claim | TimelineEntry | ActionItem]]:
    """``(path, item)`` for everything that carries citations.

    Driven by ``CLAIM_FIELDS`` rather than by ``model_fields`` so that adding a
    field to the schema without adding it here is a *test* failure
    (``test_every_claim_field_is_walked``) rather than a silent hole in the
    validator -- which is the exact shape of bug this whole module exists to
    make impossible.
    """
    out: list[tuple[str, Claim | TimelineEntry | ActionItem]] = []
    for field_name in CLAIM_FIELDS:
        value = getattr(draft, field_name)
        if value is None:
            continue
        if isinstance(value, list):
            out.extend((f"{field_name}[{i}]", item) for i, item in enumerate(value))
        else:
            out.append((field_name, value))
    return out


def all_text(draft: PIRDraft) -> str:
    """Every word the draft asserts, for the ungrounded-number check."""
    parts = [
        item.description if hasattr(item, "description") else item.text
        for _, item in walk_claims(draft)
    ]
    return "\n".join(str(p) for p in parts)
