"""Webhook endpoint behaviour, including the G1 acceptance criteria."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from incidentpilot.api.security import sign_paging
from tests.conftest import ALERTMANAGER_BEARER, PAGING_SECRET, FakeValkey


def test_alertmanager_bad_bearer_401(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """G1, criterion 1."""
    res = client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": "Bearer wrong"},
        json=alertmanager_payload,
    )
    assert res.status_code == 401
    assert fake_valkey.streams == {}, "a rejected request must publish nothing"


def test_alertmanager_missing_bearer_401(
    client: TestClient, alertmanager_payload: dict[str, Any]
) -> None:
    res = client.post("/webhooks/alertmanager", json=alertmanager_payload)
    assert res.status_code == 401


def test_alertmanager_good_bearer_202_and_entry_on_stream(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """G1, criteria 2 and 3 (correctness half; latency measured separately)."""
    res = client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json=alertmanager_payload,
    )
    assert res.status_code == 202
    # `buffered` reports the brownout write-ahead path (W7-19): zero on the
    # healthy path, and the count when the primary stream refused.
    assert res.json() == {"accepted": 2, "rejected": 0, "buffered": 0}

    entries = fake_valkey.entries("alerts.raw")
    assert len(entries) == 2
    assert {e["alertname"] for e in entries} == {"PostgresPrimaryDown", "HighErrorRate"}
    assert all(e["severity"] == "sev1" for e in entries)


def test_resolved_alerts_go_to_a_separate_stream(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """Mixing resolves into alerts.raw means a *resolve* can create an incident."""
    payload = json.loads(json.dumps(alertmanager_payload))
    payload["alerts"][0]["status"] = "resolved"
    payload["alerts"][0]["endsAt"] = "2026-09-06T09:41:11Z"

    res = client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json=payload,
    )
    assert res.status_code == 202
    assert len(fake_valkey.entries("alerts.raw")) == 1
    assert len(fake_valkey.entries("alerts.resolved")) == 1


def test_one_malformed_alert_does_not_reject_the_batch(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """A storm is exactly when partial acceptance matters most."""
    payload = json.loads(json.dumps(alertmanager_payload))
    payload["alerts"].append(
        {"labels": {"service": "no-alertname"}, "startsAt": "2026-09-06T09:00:00Z"}
    )

    res = client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json=payload,
    )
    assert res.status_code == 202
    assert res.json() == {"accepted": 2, "rejected": 1, "buffered": 0}
    assert len(fake_valkey.entries("alerts.raw")) == 2


def test_alertmanager_rejects_malformed_json(client: TestClient) -> None:
    res = client.post(
        "/webhooks/alertmanager",
        headers={
            "Authorization": f"Bearer {ALERTMANAGER_BEARER}",
            "Content-Type": "application/json",
        },
        content=b"{not json",
    )
    assert res.status_code == 400


def test_alertmanager_rejects_payload_without_alerts_array(client: TestClient) -> None:
    res = client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json={"version": "4"},
    )
    assert res.status_code == 400


def test_response_carries_a_timing_header(
    client: TestClient, alertmanager_payload: dict[str, Any]
) -> None:
    """Guards the middleware bug where elapsed was always 0.0."""
    res = client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json=alertmanager_payload,
    )
    assert "X-Response-Time-Ms" in res.headers
    assert float(res.headers["X-Response-Time-Ms"]) > 0.0


# --- paging -------------------------------------------------------------------


def _paging_event() -> dict[str, Any]:
    return {
        "event": "incident.triggered",
        "data": {
            "incident": {
                "id": "PD-INC-9021",
                "title": "Payments API error rate critical",
                "status": "triggered",
                "urgency": "high",
                "created_at": "2026-09-06T09:15:00Z",
                "service": {"summary": "payments-api"},
                "html_url": "https://example.pagerduty.com/incidents/PD-INC-9021",
            }
        },
    }


def test_paging_accepts_a_signed_event(client: TestClient, fake_valkey: FakeValkey) -> None:
    body = json.dumps(_paging_event()).encode()
    res = client.post(
        "/webhooks/paging",
        headers={"X-Paging-Signature": sign_paging(body, PAGING_SECRET)},
        content=body,
    )
    assert res.status_code == 202
    entries = fake_valkey.entries("alerts.raw")
    assert len(entries) == 1
    assert entries[0]["dedup_key"] == "PD-INC-9021"
    assert entries[0]["source"] == "paging"


def test_paging_accepts_rotated_signature_end_to_end(
    client: TestClient, fake_valkey: FakeValkey
) -> None:
    body = json.dumps(_paging_event()).encode()
    header = f"{sign_paging(body, 'previous-secret')},{sign_paging(body, PAGING_SECRET)}"
    res = client.post("/webhooks/paging", headers={"X-Paging-Signature": header}, content=body)
    assert res.status_code == 202


def test_paging_rejects_an_unsigned_event(client: TestClient, fake_valkey: FakeValkey) -> None:
    body = json.dumps(_paging_event()).encode()
    res = client.post("/webhooks/paging", content=body)
    assert res.status_code == 401
    assert fake_valkey.streams == {}


def test_paging_rejects_a_tampered_body(client: TestClient) -> None:
    body = json.dumps(_paging_event()).encode()
    signature = sign_paging(body, PAGING_SECRET)
    res = client.post(
        "/webhooks/paging",
        headers={"X-Paging-Signature": signature},
        content=body + b" ",
    )
    assert res.status_code == 401


# --- health -------------------------------------------------------------------


def test_healthz_is_liveness_only(client: TestClient, fake_valkey: FakeValkey) -> None:
    """Liveness must not depend on Valkey.

    A liveness probe that fails on a dependency blip tells Kubernetes to kill a
    healthy pod, turning a blip into a restart storm.
    """
    fake_valkey.alive = False
    assert client.get("/healthz").status_code == 200


def test_readyz_fails_when_valkey_is_down(client: TestClient, fake_valkey: FakeValkey) -> None:
    assert client.get("/readyz").status_code == 200
    fake_valkey.alive = False
    res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["checks"]["valkey"] is False


def test_metrics_endpoint_exposes_our_registry(
    client: TestClient, alertmanager_payload: dict[str, Any]
) -> None:
    client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json=alertmanager_payload,
    )
    body = client.get("/metrics").text
    assert "ip_webhook_seconds" in body
    assert "ip_alerts_accepted_total" in body
