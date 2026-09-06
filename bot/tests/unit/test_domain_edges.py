"""The defensive branches in ``domain/``.

The plan holds ``domain/`` to 100% coverage, and these are the paths that get
there: malformed input from an untrusted webhook, and the timestamp shapes real
providers actually send. They are cheap to test because the module is pure --
which is the whole argument for INV-01.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from incidentpilot.domain.normalize import (
    AlertStatus,
    MalformedPayload,
    normalize_alertmanager,
    normalize_paging,
    parse_timestamp,
)

# --- parse_timestamp: the shapes providers really send ------------------------


def test_parse_timestamp_accepts_a_datetime_object() -> None:
    """Some provider SDKs hand back a parsed datetime, not a string."""
    aware = datetime(2026, 9, 6, 9, 14, 2, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    parsed = parse_timestamp(aware)
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed.hour == 3 and parsed.minute == 44  # IST -> UTC


def test_parse_timestamp_makes_a_naive_datetime_utc() -> None:
    parsed = parse_timestamp(datetime(2026, 9, 6, 9, 14, 2))
    assert parsed is not None and parsed.tzinfo is UTC


def test_parse_timestamp_rejects_non_string_non_datetime() -> None:
    """An integer epoch is a plausible thing to receive and is not supported.

    Returning None rather than guessing is deliberate: a wrongly-parsed epoch
    would put a row on the timeline at the wrong moment, and the timeline is the
    product.
    """
    assert parse_timestamp(1757148842) is None
    assert parse_timestamp(["2026-09-06T09:14:02Z"]) is None
    assert parse_timestamp({"at": "2026-09-06"}) is None


def test_parse_timestamp_rejects_a_year_one_sentinel_in_any_form() -> None:
    assert parse_timestamp("0001-01-01T00:00:00+00:00") is None


# --- normalize_alertmanager: malformed input ----------------------------------


def test_normalize_alertmanager_rejects_a_non_object_entry() -> None:
    """The `alerts` array is attacker-influenced; entries may be anything."""
    with pytest.raises(MalformedPayload, match="not an object"):
        normalize_alertmanager("not-an-object", {})  # type: ignore[arg-type]
    with pytest.raises(MalformedPayload, match="not an object"):
        normalize_alertmanager(["nested"], {})  # type: ignore[arg-type]


def test_normalize_alertmanager_tolerates_a_missing_envelope() -> None:
    """Called without the surrounding payload, dedup falls back to fingerprint."""
    entry = {
        "labels": {"alertname": "DiskFull", "service": "node-1", "severity": "warning"},
        "startsAt": "2026-09-06T09:14:02Z",
    }
    alert = normalize_alertmanager(entry)
    assert alert.severity == "sev3"
    assert alert.dedup_key == alert.fingerprint


def test_normalize_alertmanager_takes_status_from_the_envelope() -> None:
    """Alertmanager sets status on the envelope; entries may omit it."""
    entry = {
        "labels": {"alertname": "DiskFull", "severity": "warning"},
        "startsAt": "2026-09-06T09:14:02Z",
    }
    alert = normalize_alertmanager(entry, {"status": "resolved"})
    assert alert.status is AlertStatus.RESOLVED


# --- normalize_paging: shape tolerance and hard failures ----------------------


def test_normalize_paging_rejects_a_non_object() -> None:
    with pytest.raises(MalformedPayload, match="not an object"):
        normalize_paging("triggered")  # type: ignore[arg-type]


def test_normalize_paging_rejects_an_event_with_no_title() -> None:
    with pytest.raises(MalformedPayload, match="no title"):
        normalize_paging({"data": {"incident": {"id": "PD-1"}}})


def test_normalize_paging_rejects_an_event_with_no_timestamp() -> None:
    """Never invent a start time, even when everything else is present."""
    with pytest.raises(MalformedPayload, match="no parseable timestamp"):
        normalize_paging({"data": {"incident": {"id": "PD-1", "title": "API down"}}})


def test_normalize_paging_accepts_a_flat_incident() -> None:
    """Providers nest differently; a top-level incident must still normalize."""
    alert = normalize_paging(
        {
            "id": "PD-2",
            "title": "Checkout latency",
            "created_at": "2026-09-06T09:15:00Z",
            "service": "checkout",
            "urgency": "high",
        }
    )
    assert alert.dedup_key == "PD-2"
    assert alert.service == "checkout"
    assert alert.severity == "sev2"


def test_normalize_paging_falls_back_to_summary_then_event_for_the_title() -> None:
    alert = normalize_paging(
        {
            "event": "incident.triggered",
            "data": {"incident": {"summary": "Replica lag", "created_at": "2026-09-06T09:15:00Z"}},
        }
    )
    assert alert.alertname == "Replica lag"


def test_normalize_paging_handles_a_string_service_block() -> None:
    alert = normalize_paging(
        {"title": "X", "created_at": "2026-09-06T09:15:00Z", "service": "payments-api"}
    )
    assert alert.service == "payments-api"


def test_normalize_paging_handles_a_missing_service() -> None:
    alert = normalize_paging({"title": "X", "created_at": "2026-09-06T09:15:00Z"})
    assert alert.service is None
    assert "service" not in alert.labels


def test_normalize_paging_marks_resolved_and_keeps_the_end_time() -> None:
    alert = normalize_paging(
        {
            "title": "X",
            "status": "resolved",
            "created_at": "2026-09-06T09:15:00Z",
            "resolved_at": "2026-09-06T09:41:00Z",
        }
    )
    assert alert.status is AlertStatus.RESOLVED
    assert alert.ends_at is not None


def test_normalize_paging_uses_occurred_at_when_the_incident_has_no_timestamp() -> None:
    alert = normalize_paging(
        {"occurred_at": "2026-09-06T09:15:00Z", "data": {"incident": {"title": "X"}}}
    )
    assert alert.starts_at.tzinfo is UTC


def test_normalize_paging_generates_a_dedup_key_when_there_is_no_id() -> None:
    alert = normalize_paging({"title": "X", "created_at": "2026-09-06T09:15:00Z"})
    assert alert.dedup_key == alert.fingerprint
