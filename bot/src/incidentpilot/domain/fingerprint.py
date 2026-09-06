"""Deterministic alert identity.

PURE MODULE (INV-01): no I/O, no clock, no randomness. Everything here is a
function of its arguments, which is what makes the replay harness possible.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Labels that are stable across firings of the same underlying problem.
#
# Volatile labels -- pod name, instance IP, container id -- are excluded on
# purpose: including them means the same alert on a restarted pod looks like a
# brand new incident, which is precisely the storm-generating bug (B-03).
STABLE_LABELS: tuple[str, ...] = (
    "alertname",
    "service",
    "namespace",
    "cluster",
    "severity",
)


def fingerprint(alert: dict[str, Any]) -> str:
    """Stable identity for an alert, across firings.

    The hash is truncated to 32 hex characters. That is 128 bits of the SHA-256
    digest -- far beyond collision risk at any volume this system will see, and
    short enough to read in a Slack message and a log line.
    """
    labels: dict[str, Any] = alert.get("labels") or {}
    raw = "|".join(f"{key}={labels.get(key, '')}" for key in STABLE_LABELS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def dedup_key(alert: dict[str, Any]) -> str:
    """Identity of a *firing*, used for the incident dedup constraint.

    Prefer Alertmanager's own ``groupKey``: it already encodes the grouping
    decision the operator configured in ``alertmanager.yml``. Re-deriving it
    means silently overriding an operator's intent, which is both rude and
    surprising when someone has deliberately grouped by ``cluster``.
    """
    group_key = alert.get("groupKey")
    if isinstance(group_key, str) and group_key:
        return group_key
    return fingerprint(alert)
