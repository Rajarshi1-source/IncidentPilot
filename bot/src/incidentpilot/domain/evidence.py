"""The evidence grammar: six citation kinds, one ID form each (W6-07, D1).

PURE MODULE (INV-01). This is the vocabulary the whole grounding guarantee is
written in, and it has to be identical in three places that never talk to each
other: the context builder that produces valid IDs, the validator that checks
them, and the renderer that turns them into clickable chips. One module, three
consumers, no second opinion about what ``msg:`` means.

| Kind       | ID form                        | Source                     |
|------------|--------------------------------|----------------------------|
| `message`  | `msg:{slack_ts}`               | `slack_messages.ts`        |
| `timeline` | `tl:{id}`                      | `timeline_events`          |
| `alert`    | `alert:{fingerprint}`          | `alerts.fingerprint`       |
| `deploy`   | `deploy:{sha}`                 | deploy webhook events      |
| `metric`   | `metric:{query}@{t0}-{t1}`     | computed impact windows    |
| `runbook`  | `rb:{runbook_id}#{step_id}`    | `runbook_step_signals`     |

**Why the prefix carries meaning.** The validator uses it to know which store to
check and the renderer uses it to know which chip to draw, so a bare id would
force both to guess. It also makes a fabricated citation *look* fabricated in a
log line, which matters at 3 a.m. more than it looks on paper.

The metric form is the awkward one and deliberately so: a metric citation is
only meaningful together with the exact query and the exact window, so both are
in the id. The query is hashed rather than embedded whole -- a PromQL expression
contains braces, quotes and commas, and an id nobody can put in a JSON string
without escaping is an id that will eventually be mangled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CitationKind(StrEnum):
    MESSAGE = "message"
    TIMELINE = "timeline"
    METRIC = "metric"
    DEPLOY = "deploy"
    ALERT = "alert"
    RUNBOOK = "runbook"


PREFIX: dict[CitationKind, str] = {
    CitationKind.MESSAGE: "msg",
    CitationKind.TIMELINE: "tl",
    CitationKind.METRIC: "metric",
    CitationKind.DEPLOY: "deploy",
    CitationKind.ALERT: "alert",
    CitationKind.RUNBOOK: "rb",
}

KIND_OF: dict[str, CitationKind] = {prefix: kind for kind, prefix in PREFIX.items()}

# `prefix:body`, where the body is non-empty and has no whitespace. Anchored, so
# "see msg:123 for details" is not a reference -- a citation is the whole field
# or it is not a citation.
REFERENCE = re.compile(r"\A(msg|tl|metric|deploy|alert|rb):(\S+)\Z")

# Eight hex characters of the query digest. Long enough that two different
# PromQL expressions in one incident will not collide, short enough to read.
QUERY_DIGEST_CHARS = 8


class MalformedReference(ValueError):
    """The string is not a reference at all -- distinct from "not in the set".

    Worth its own error: a syntactically invalid id means the model did not
    understand the grammar, while a well-formed id that is not in the valid set
    means it invented a plausible one. The second is the interesting failure.
    """


@dataclass(frozen=True, slots=True)
class Reference:
    kind: CitationKind
    body: str

    def __str__(self) -> str:
        return f"{PREFIX[self.kind]}:{self.body}"


def parse_reference(ref: str) -> Reference:
    match = REFERENCE.match(ref.strip())
    if match is None:
        raise MalformedReference(f"not a reference id: {ref!r}")
    return Reference(kind=KIND_OF[match.group(1)], body=match.group(2))


def is_reference(ref: str) -> bool:
    return REFERENCE.match(ref.strip()) is not None


def kind_of(ref: str) -> CitationKind:
    return parse_reference(ref).kind


# --- constructors: the only places these strings are built --------------------


def message_ref(slack_ts: str) -> str:
    return f"msg:{slack_ts}"


def timeline_ref(timeline_id: int | str) -> str:
    return f"tl:{timeline_id}"


def alert_ref(fingerprint: str) -> str:
    return f"alert:{fingerprint}"


def deploy_ref(sha: str) -> str:
    """Short SHA, lowercased.

    Normalized because a deploy webhook sends the full forty characters and a
    human writes seven. Two ids for one deploy would make half the citations
    fabricated-looking for no reason.
    """
    return f"deploy:{sha.strip().lower()[:12]}"


def runbook_ref(runbook_id: int | str, step_id: str) -> str:
    return f"rb:{runbook_id}#{step_id}"


def metric_ref(query: str, start: datetime, end: datetime) -> str:
    """``metric:{query-digest}@{t0}-{t1}``.

    The window is in the id because the same query over a different window is a
    different fact. Both timestamps are whole seconds: a microsecond difference
    between the id the context built and the id the renderer rebuilt would make
    every metric citation look fabricated.
    """
    digest = hashlib.sha256(query.encode()).hexdigest()[:QUERY_DIGEST_CHARS]
    return f"metric:{digest}@{int(start.timestamp())}-{int(end.timestamp())}"


# --- number extraction, for INV-06 -------------------------------------------

# Digits with optional separators and decimals. Deliberately does not match a
# number glued to a letter -- `sev1`, `p99`, `http2`, `5xx` are identifiers, not
# claims about magnitude, and flagging them would make every honest draft fail.
NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w])")

# Numbers small enough to be ordinary prose rather than a measurement: "two of
# the three replicas", "step 4". The threshold is deliberate -- INV-06 is about
# fabricated *impact*, and rejecting "3 replicas" would push honest drafts to
# the skeleton while catching nothing that matters.
TRIVIAL_MAX = 10


def extract_numbers(text: str) -> set[str]:
    """Every numeric literal a claim asserts, normalized for comparison."""
    out: set[str] = set()
    for match in NUMBER.finditer(text):
        whole = match.group(1).replace(",", "")
        fraction = match.group(2)
        try:
            value = float(f"{whole}.{fraction}") if fraction else float(whole)
        except ValueError:  # pragma: no cover - the regex cannot produce this
            continue
        if abs(value) <= TRIVIAL_MAX and fraction is None:
            continue
        out.add(match.group(0).replace(",", ""))
    return out
