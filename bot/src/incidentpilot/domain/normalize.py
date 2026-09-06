"""Raw webhook payloads -> one normalized shape.

PURE MODULE (INV-01): no I/O, no clock, no randomness.

Timestamps are parsed from the payload, never taken from the local clock. An
alert that arrives late is still an alert that *started* when it says it did,
and the whole product is a timeline -- so inventing a receive-time here would
corrupt the one thing we are selling. ``received_at`` is stamped at the edge by
the caller, which owns the clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from incidentpilot.domain.fingerprint import dedup_key, fingerprint

# Alertmanager severity label -> our rank. Higher rank is more severe, so a
# plain integer comparison answers "may this alert be absorbed into that
# incident?" without a lookup table at the call site (see W2 correlation).
SEVERITY_RANK: dict[str, int] = {"sev1": 4, "sev2": 3, "sev3": 2, "sev4": 1}

# Common spellings seen in the wild, mapped onto our four levels.
_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "sev1",
    "page": "sev1",
    "sev1": "sev1",
    "p1": "sev1",
    "error": "sev2",
    "high": "sev2",
    "sev2": "sev2",
    "p2": "sev2",
    "warning": "sev3",
    "warn": "sev3",
    "sev3": "sev3",
    "p3": "sev3",
    "info": "sev4",
    "informational": "sev4",
    "none": "sev4",
    "sev4": "sev4",
    "p4": "sev4",
}


class AlertSource(StrEnum):
    ALERTMANAGER = "alertmanager"
    PAGING = "paging"
    DEPLOY = "deploy"
    MANUAL = "manual"


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class MalformedPayload(ValueError):
    """The payload is not something we can normalize. Callers return 400."""


@dataclass(frozen=True, slots=True)
class NormalizedAlert:
    """The one shape every downstream stage consumes.

    Frozen because it crosses a queue boundary: anything that mutates an alert
    after it has been published has introduced a race that will only show up
    under a storm.
    """

    source: AlertSource
    status: AlertStatus
    fingerprint: str
    dedup_key: str
    alertname: str
    service: str | None
    severity: str
    severity_rank: int
    starts_at: datetime
    ends_at: datetime | None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    generator_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def stable_labels(self) -> dict[str, str]:
        """Only the labels correlation is allowed to reason about."""
        from incidentpilot.domain.fingerprint import STABLE_LABELS

        return {k: v for k, v in self.labels.items() if k in STABLE_LABELS}


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an RFC 3339 timestamp into an aware UTC datetime.

    Alertmanager sends ``0001-01-01T00:00:00Z`` for an absent ``endsAt`` rather
    than omitting the field, so that sentinel is treated as None. Missing it
    means every firing alert looks like it ended in year 1.
    """
    if value in (None, "", "0001-01-01T00:00:00Z"):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year <= 1:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_severity(raw_severity: str | None) -> tuple[str, int]:
    """Map a free-text severity label onto (level, rank).

    Unknown values become sev3 rather than sev1. Defaulting unknown input to the
    most severe level would make every mislabelled alert page a human at 3 a.m.,
    which is how people learn to ignore the pager.
    """
    key = (raw_severity or "").strip().lower()
    level = _SEVERITY_ALIASES.get(key, "sev3")
    return level, SEVERITY_RANK[level]


def _as_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """Narrow an untrusted payload branch to a mapping, or None."""
    return value if isinstance(value, dict) else None


def normalize_alertmanager(
    alert: dict[str, Any],
    envelope: dict[str, Any] | None = None,
) -> NormalizedAlert:
    """Normalize one entry from an Alertmanager v4 webhook payload.

    ``envelope`` is the surrounding request body, which carries ``groupKey`` --
    the operator's own grouping decision, and the field we prefer over a
    re-derived fingerprint.
    """
    if not isinstance(alert, dict):
        raise MalformedPayload("alert entry is not an object")

    envelope = envelope or {}
    labels = _as_str_map(alert.get("labels"))
    annotations = _as_str_map(alert.get("annotations"))

    alertname = labels.get("alertname")
    if not alertname:
        raise MalformedPayload("alert has no alertname label")

    starts_at = parse_timestamp(alert.get("startsAt"))
    if starts_at is None:
        raise MalformedPayload("alert has no parseable startsAt")

    status_raw = str(alert.get("status") or envelope.get("status") or "firing").lower()
    status = AlertStatus.RESOLVED if status_raw == "resolved" else AlertStatus.FIRING

    severity, rank = normalize_severity(labels.get("severity"))

    # groupKey lives on the envelope; hand the merged view to dedup_key so it
    # can prefer the operator's grouping over our fingerprint.
    for_dedup: dict[str, Any] = {"labels": labels}
    if envelope.get("groupKey"):
        for_dedup["groupKey"] = envelope["groupKey"]

    return NormalizedAlert(
        source=AlertSource.ALERTMANAGER,
        status=status,
        fingerprint=fingerprint({"labels": labels}),
        dedup_key=dedup_key(for_dedup),
        alertname=alertname,
        service=labels.get("service"),
        severity=severity,
        severity_rank=rank,
        starts_at=starts_at,
        ends_at=parse_timestamp(alert.get("endsAt")),
        labels=labels,
        annotations=annotations,
        generator_url=alert.get("generatorURL") or None,
        raw=alert,
    )


def normalize_paging(event: dict[str, Any]) -> NormalizedAlert:
    """Normalize a paging-provider incident webhook.

    Deliberately tolerant about shape: providers differ, and this is an adapter
    boundary. What it will not do is invent a start time -- a paging event with
    no timestamp is malformed, and guessing would put a wrong row on a timeline
    someone may be held accountable to.
    """
    if not isinstance(event, dict):
        raise MalformedPayload("paging event is not an object")

    # Providers nest the payload differently: some send {data: {incident: {...}}},
    # some send the incident at the top level. Unwrap defensively and keep the
    # result narrowed to a real mapping so the accessors below are total.
    data = _as_mapping(event.get("data")) or event
    incident = _as_mapping(data.get("incident")) or data

    title = incident.get("title") or incident.get("summary") or event.get("event")
    if not title:
        raise MalformedPayload("paging event has no title")

    service_block = incident.get("service")
    service = (
        service_block.get("summary")
        if isinstance(service_block, dict)
        else (service_block if isinstance(service_block, str) else None)
    )

    starts_at = parse_timestamp(
        incident.get("created_at") or incident.get("createdAt") or event.get("occurred_at")
    )
    if starts_at is None:
        raise MalformedPayload("paging event has no parseable timestamp")

    status_raw = str(incident.get("status") or "").lower()
    status = AlertStatus.RESOLVED if status_raw == "resolved" else AlertStatus.FIRING

    severity, rank = normalize_severity(incident.get("urgency") or incident.get("priority"))

    labels = {"alertname": str(title), "severity": severity}
    if service:
        labels["service"] = str(service)

    return NormalizedAlert(
        source=AlertSource.PAGING,
        status=status,
        fingerprint=fingerprint({"labels": labels}),
        dedup_key=str(incident.get("id") or fingerprint({"labels": labels})),
        alertname=str(title),
        service=str(service) if service else None,
        severity=severity,
        severity_rank=rank,
        starts_at=starts_at,
        ends_at=parse_timestamp(incident.get("resolved_at")),
        labels=labels,
        annotations={"html_url": str(incident.get("html_url"))} if incident.get("html_url") else {},
        raw=event,
    )


def to_stream_fields(alert: NormalizedAlert) -> dict[str, str]:
    """Flatten to the string-only map a Redis stream entry accepts.

    Streams store field/value pairs as bytes. Nested structures are JSON-encoded
    here rather than at the call site so there is exactly one encoding of an
    alert on the wire, and the consumer has exactly one thing to decode.
    """
    import json

    return {
        "source": str(alert.source),
        "status": str(alert.status),
        "fingerprint": alert.fingerprint,
        "dedup_key": alert.dedup_key,
        "alertname": alert.alertname,
        "service": alert.service or "",
        "severity": alert.severity,
        "severity_rank": str(alert.severity_rank),
        "starts_at": alert.starts_at.isoformat(),
        "ends_at": alert.ends_at.isoformat() if alert.ends_at else "",
        "labels": json.dumps(alert.labels, sort_keys=True),
        "annotations": json.dumps(alert.annotations, sort_keys=True),
        "generator_url": alert.generator_url or "",
        "raw": json.dumps(alert.raw, sort_keys=True, default=str),
    }
