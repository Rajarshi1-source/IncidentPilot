"""Pure domain logic: fingerprinting, dedup identity, normalization."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from incidentpilot.domain.fingerprint import STABLE_LABELS, dedup_key, fingerprint
from incidentpilot.domain.normalize import (
    AlertSource,
    AlertStatus,
    MalformedPayload,
    normalize_alertmanager,
    normalize_severity,
    parse_timestamp,
    to_stream_fields,
)


def _alert(**labels: str) -> dict[str, object]:
    base = {
        "alertname": "PostgresPrimaryDown",
        "service": "postgres-primary",
        "namespace": "data",
        "cluster": "prod-ap-south-1",
        "severity": "critical",
    }
    base.update(labels)
    return {"labels": base}


# --- fingerprint --------------------------------------------------------------


def test_fingerprint_is_stable_across_calls() -> None:
    assert fingerprint(_alert()) == fingerprint(_alert())


def test_fingerprint_ignores_volatile_labels() -> None:
    """The B-03 bug in one assertion.

    A restarted pod changes `pod` and `instance`. If those fed the fingerprint,
    the same underlying problem would look like a brand new incident every time
    Kubernetes rescheduled -- which is how one outage becomes forty channels.
    """
    stable = fingerprint(_alert())
    restarted = fingerprint(
        _alert(pod="postgres-primary-1", instance="10.42.9.4:9187", container_id="abc123")
    )
    assert stable == restarted


def test_fingerprint_changes_when_a_stable_label_changes() -> None:
    assert fingerprint(_alert()) != fingerprint(_alert(service="payments-api"))
    assert fingerprint(_alert()) != fingerprint(_alert(cluster="prod-eu-west-1"))


def test_fingerprint_is_32_hex_chars() -> None:
    value = fingerprint(_alert())
    assert len(value) == 32
    assert all(c in "0123456789abcdef" for c in value)


def test_fingerprint_tolerates_missing_labels() -> None:
    assert len(fingerprint({})) == 32
    assert len(fingerprint({"labels": {}})) == 32


def test_stable_labels_do_not_include_volatile_ones() -> None:
    assert "pod" not in STABLE_LABELS
    assert "instance" not in STABLE_LABELS


# --- dedup key ----------------------------------------------------------------


def test_dedup_key_prefers_alertmanager_group_key() -> None:
    """groupKey encodes the operator's own grouping decision.

    Re-deriving it silently overrides what someone deliberately configured in
    alertmanager.yml.
    """
    alert = dict(_alert())
    alert["groupKey"] = '{}/{severity="sev1"}:{alertname="PostgresPrimaryDown"}'
    assert dedup_key(alert) == alert["groupKey"]


def test_dedup_key_falls_back_to_fingerprint() -> None:
    assert dedup_key(_alert()) == fingerprint(_alert())
    empty_group = dict(_alert())
    empty_group["groupKey"] = ""
    assert dedup_key(empty_group) == fingerprint(_alert())


# --- timestamps ---------------------------------------------------------------


def test_parse_timestamp_returns_aware_utc() -> None:
    parsed = parse_timestamp("2026-09-06T09:14:02.117Z")
    assert parsed == datetime(2026, 9, 6, 9, 14, 2, 117000, tzinfo=UTC)
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_timestamp_treats_alertmanager_zero_as_absent() -> None:
    """Alertmanager sends year-1 for an absent endsAt rather than omitting it.

    Missing this makes every firing alert look like it ended in year 1, which
    then makes every duration negative.
    """
    assert parse_timestamp("0001-01-01T00:00:00Z") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


def test_parse_timestamp_assumes_utc_for_naive_input() -> None:
    parsed = parse_timestamp("2026-09-06T09:14:02")
    assert parsed is not None and parsed.tzinfo is UTC


def test_parse_timestamp_rejects_garbage() -> None:
    assert parse_timestamp("not-a-timestamp") is None


# --- severity -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("critical", "sev1"),
        ("CRITICAL", "sev1"),
        ("page", "sev1"),
        ("error", "sev2"),
        ("warning", "sev3"),
        ("info", "sev4"),
        ("sev2", "sev2"),
    ],
)
def test_normalize_severity_maps_common_spellings(raw: str, expected: str) -> None:
    level, _ = normalize_severity(raw)
    assert level == expected


def test_unknown_severity_defaults_to_sev3_not_sev1() -> None:
    """Defaulting unknown input to the most severe level would make every
    mislabelled alert page a human at 3 a.m., which is how people learn to
    ignore the pager."""
    assert normalize_severity("banana")[0] == "sev3"
    assert normalize_severity(None)[0] == "sev3"


def test_severity_rank_orders_correctly() -> None:
    ranks = [normalize_severity(s)[1] for s in ("sev1", "sev2", "sev3", "sev4")]
    assert ranks == sorted(ranks, reverse=True)


# --- normalization ------------------------------------------------------------


def test_normalize_alertmanager_extracts_the_expected_shape() -> None:
    envelope = {"groupKey": "gk-1", "status": "firing"}
    entry = {
        "status": "firing",
        "labels": _alert()["labels"],
        "annotations": {"summary": "primary down"},
        "startsAt": "2026-09-06T09:14:02.117Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph",
    }
    alert = normalize_alertmanager(entry, envelope)

    assert alert.source is AlertSource.ALERTMANAGER
    assert alert.status is AlertStatus.FIRING
    assert alert.alertname == "PostgresPrimaryDown"
    assert alert.service == "postgres-primary"
    assert alert.severity == "sev1"
    assert alert.dedup_key == "gk-1"
    assert alert.ends_at is None
    assert alert.starts_at.tzinfo is UTC


def test_normalize_alertmanager_detects_resolved() -> None:
    entry = {
        "status": "resolved",
        "labels": _alert()["labels"],
        "startsAt": "2026-09-06T09:14:02Z",
        "endsAt": "2026-09-06T09:41:11Z",
    }
    alert = normalize_alertmanager(entry, {})
    assert alert.status is AlertStatus.RESOLVED
    assert alert.ends_at is not None


def test_normalize_rejects_an_alert_with_no_alertname() -> None:
    with pytest.raises(MalformedPayload, match="alertname"):
        normalize_alertmanager({"labels": {"service": "x"}, "startsAt": "2026-09-06T09:00:00Z"}, {})


def test_normalize_rejects_an_alert_with_no_start_time() -> None:
    """Never invent a start time. The whole product is a timeline."""
    with pytest.raises(MalformedPayload, match="startsAt"):
        normalize_alertmanager({"labels": _alert()["labels"]}, {})


def test_stable_labels_property_filters_to_the_stable_set() -> None:
    entry = {
        "labels": {**_alert()["labels"], "pod": "p-0", "instance": "1.2.3.4"},  # type: ignore[dict-item]
        "startsAt": "2026-09-06T09:14:02Z",
    }
    alert = normalize_alertmanager(entry, {})
    assert set(alert.stable_labels) <= set(STABLE_LABELS)
    assert "pod" not in alert.stable_labels


def test_to_stream_fields_is_all_strings() -> None:
    """Redis stream values must be strings/bytes; a nested dict raises at XADD."""
    entry = {
        "labels": _alert()["labels"],
        "annotations": {"summary": "x"},
        "startsAt": "2026-09-06T09:14:02Z",
    }
    fields = to_stream_fields(normalize_alertmanager(entry, {}))
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items())
    assert fields["severity"] == "sev1"
