"""The PII pattern set (W6-13, D6).

PURE MODULE (INV-01). Patterns and classification only -- no I/O, no clock, no
model, no Presidio import. The impure half (the recognizer that walks text and
the token store that keeps a per-incident mapping) lives in ``privacy/``.

**Why hand-written patterns rather than Presidio here.** Presidio pulls spaCy
and a model download, which would break the "clone it and run the demo with no
credentials" property the whole repository is built around. The patterns below
are the deterministic floor: they run everywhere, they run in the replay harness
with no network (INV-10), and they are what the CI test actually asserts on. A
Presidio recognizer can be layered on top through the same interface for
deployments that want NER-grade recall on names.

**The custom recognizers are the interesting half.** Generic PII detectors find
emails and card numbers; they do not find a UPI VPA, an Indian phone format, an
order id, a JWT or an AWS key -- and those are what an incident channel is
actually full of. §16 names them specifically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PIIClass(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    CARD = "card"
    UPI = "upi"
    JWT = "jwt"
    AWS_KEY = "aws_key"
    IP = "ip"
    ORDER_ID = "order_id"
    SLACK_TOKEN = "slack_token"


@dataclass(frozen=True, slots=True)
class Rule:
    """One recognizer. ``priority`` breaks ties on overlapping matches.

    Overlaps are real and not hypothetical: a JWT contains base64 that looks
    like nothing else, but an AWS secret key inside a URL also matches the
    generic token shapes. Higher priority wins, so the specific classification
    survives and the audit log says `aws_key` rather than `token`.
    """

    pii_class: PIIClass
    pattern: re.Pattern[str]
    priority: int = 0


# Ordered by specificity, not by likelihood. The first four are the ones §16
# names as domain recognizers and the reason a generic PII library is not enough.
RULES: tuple[Rule, ...] = (
    # AWS access key ids have a fixed prefix and length -- unambiguous, so it
    # outranks everything and is checked first.
    Rule(PIIClass.AWS_KEY, re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b"), priority=100),
    # xoxb-, xoxp-, xapp-: a Slack token pasted into a channel is both PII-ish
    # and an active credential, and it must never leave the boundary.
    Rule(
        PIIClass.SLACK_TOKEN,
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
        priority=95,
    ),
    # Three dot-separated base64url segments. Anchored on the `eyJ` header that
    # every JSON JWT starts with, so ordinary dotted identifiers do not match.
    Rule(
        PIIClass.JWT,
        re.compile(r"\beyJ[0-9A-Za-z_-]{6,}\.[0-9A-Za-z_-]{6,}\.[0-9A-Za-z_-]{6,}\b"),
        priority=90,
    ),
    Rule(
        PIIClass.EMAIL,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        priority=80,
    ),
    # A UPI VPA looks like an email with no dot in the handle: `ananya@okhdfc`.
    # Checked after EMAIL so a real address is classified as an address.
    Rule(PIIClass.UPI, re.compile(r"\b[A-Za-z0-9._-]{2,}@[A-Za-z]{3,}\b"), priority=70),
    # 13-19 digits with optional separators. Luhn is checked by the recognizer,
    # not here: a checksum is logic, and this module is patterns.
    Rule(
        PIIClass.CARD,
        re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
        priority=60,
    ),
    # Indian formats: +91 with or without a space, or a bare ten digits starting
    # 6-9. The leading-digit constraint is what stops it eating order numbers.
    Rule(
        PIIClass.PHONE,
        re.compile(r"(?:\+91[\s-]?|\b0)?\b[6-9]\d{9}\b"),
        priority=50,
    ),
    Rule(
        PIIClass.ORDER_ID,
        re.compile(r"\b(?:ORD|ORDER|INV)[-_]?[0-9A-Z]{6,}\b", re.IGNORECASE),
        priority=40,
    ),
    # Private ranges are deliberately excluded by the recognizer, not here: a
    # 10.x address in an incident channel is topology, not PII, and redacting it
    # would make half the transcript unreadable to the model.
    Rule(
        PIIClass.IP,
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        priority=30,
    ),
)

# Redacting these would make the transcript useless to the model without
# protecting anyone. Kept as a frozenset rather than a regex branch so the
# exclusion is inspectable and testable on its own.
PRIVATE_IP_PREFIXES: frozenset[str] = frozenset({"10.", "192.168.", "127.", "0."})


def is_private_ip(value: str) -> bool:
    """172.16.0.0/12 needs arithmetic; the rest is a prefix check."""
    if any(value.startswith(prefix) for prefix in PRIVATE_IP_PREFIXES):
        return True
    parts = value.split(".")
    if len(parts) == 4 and parts[0] == "172":
        try:
            return 16 <= int(parts[1]) <= 31
        except ValueError:
            return False
    return False


def luhn_ok(digits: str) -> bool:
    """The card checksum.

    Without it, any long number -- an order id, a trace id, a byte count --
    becomes ``<CARD_1>`` and the model loses the ability to reason about the
    incident. A checksum is the cheapest way to tell a card from a big number.
    """
    stripped = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(stripped) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(stripped)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def token_for(pii_class: PIIClass, ordinal: int) -> str:
    """``<EMAIL_1>``.

    Stable **per incident**: the same address is the same token throughout, so
    the model can still reason about "the same customer reported it twice"
    without ever seeing the address. A random token per occurrence would protect
    the same data and destroy that.
    """
    return f"<{pii_class.name}_{ordinal}>"


# `<EMAIL_1>`, for the restore pass on render.
TOKEN = re.compile(r"<([A-Z_]+)_(\d+)>")
