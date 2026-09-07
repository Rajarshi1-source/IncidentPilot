"""Redaction at the egress boundary (W6-13, D6).

Round-trip lossless: ``restore(redact(x)) == x``. That property is what makes
the whole thing usable -- the model sees `<EMAIL_1>`, the rendered PIR shows the
real address to the humans who are entitled to see it, and nothing in between
had to choose between privacy and readability.

**Stable tokens per incident.** The same address is `<EMAIL_1>` everywhere in
one incident's context, so the model can still reason about "the same customer
reported it twice" while never seeing the address. A fresh token per occurrence
would protect exactly the same data and destroy that inference.

**The audit records the class and the location, never the value.** A redaction
log containing the values it redacted is a second copy of the data with none of
the controls -- which is a genuinely common way this feature is built wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from incidentpilot.domain.redaction_rules import (
    RULES,
    TOKEN,
    PIIClass,
    is_private_ip,
    luhn_ok,
    token_for,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Finding:
    """One redaction, for the audit trail. Deliberately without the value."""

    pii_class: PIIClass
    token: str
    start: int
    end: int


@dataclass
class Redactor:
    """Per-incident token store. One instance per incident, by construction.

    Sharing an instance across incidents would leak the *fact* that the same
    address appears in two of them, which is a smaller leak than the address
    itself and still not one to make by accident.
    """

    mapping: dict[str, str] = field(default_factory=dict)
    reverse: dict[str, str] = field(default_factory=dict)
    counters: dict[PIIClass, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    enabled: bool = True

    # -- the boundary ----------------------------------------------------

    def redact(self, text: str) -> str:
        """Replace every recognized value with its stable token."""
        if not self.enabled or not text:
            return text

        spans = self._spans(text)
        if not spans:
            return text

        out: list[str] = []
        cursor = 0
        for start, end, pii_class in spans:
            value = text[start:end]
            token = self._token(pii_class, value)
            out.append(text[cursor:start])
            out.append(token)
            self.findings.append(Finding(pii_class, token, start, end))
            cursor = end
        out.append(text[cursor:])
        return "".join(out)

    def restore(self, text: str) -> str:
        """Put the real values back, for humans who are entitled to see them.

        Unknown tokens are left as they are rather than blanked: a `<EMAIL_9>`
        this redactor never issued means the model invented a token, and showing
        it is how anyone finds that out.
        """
        if not self.reverse:
            return text

        def _swap(match: re.Match[str]) -> str:
            return self.reverse.get(match.group(0), match.group(0))

        return TOKEN.sub(_swap, text)

    # -- internals -------------------------------------------------------

    def _token(self, pii_class: PIIClass, value: str) -> str:
        existing = self.mapping.get(value)
        if existing is not None:
            return existing
        ordinal = self.counters.get(pii_class, 0) + 1
        self.counters[pii_class] = ordinal
        token = token_for(pii_class, ordinal)
        self.mapping[value] = token
        self.reverse[token] = value
        # Class and position only. Never the value.
        log.debug("privacy.redacted", pii_class=str(pii_class), token=token)
        return token

    def _spans(self, text: str) -> list[tuple[int, int, PIIClass]]:
        """Non-overlapping matches, highest-priority rule winning.

        Overlap resolution matters: a UPI VPA and an email have nearly the same
        shape, and a card pattern will happily eat the digits of a phone number.
        Collecting every candidate and then resolving by priority is what keeps
        the *classification* right, which is what the audit log reports.
        """
        candidates: list[tuple[int, int, PIIClass, int]] = []
        for rule in RULES:
            for match in rule.pattern.finditer(text):
                value = match.group(0)
                if rule.pii_class is PIIClass.CARD and not luhn_ok(value):
                    # A long number that is not a card is an order id, a trace
                    # id, or a byte count -- redacting it would blind the model
                    # to the incident for no privacy gain.
                    continue
                if rule.pii_class is PIIClass.IP and is_private_ip(value):
                    # Topology, not PII.
                    continue
                candidates.append((match.start(), match.end(), rule.pii_class, rule.priority))

        candidates.sort(key=lambda c: (-c[3], c[0], -(c[1] - c[0])))
        chosen: list[tuple[int, int, PIIClass]] = []
        taken: list[tuple[int, int]] = []
        for start, end, pii_class, _ in candidates:
            if any(start < t_end and end > t_start for t_start, t_end in taken):
                continue
            taken.append((start, end))
            chosen.append((start, end, pii_class))

        chosen.sort(key=lambda c: c[0])
        return chosen

    # -- reporting -------------------------------------------------------

    def audit(self) -> list[dict[str, object]]:
        """What was redacted, by class. Values never appear."""
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[str(finding.pii_class)] = counts.get(str(finding.pii_class), 0) + 1
        return [{"class": name, "count": count} for name, count in sorted(counts.items())]

    @property
    def redacted_count(self) -> int:
        return len(self.findings)
