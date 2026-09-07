"""W4-01, W4-02: the Slack Events receiver and its trust boundary.

The signature test is the one that matters. Slack signs the **raw bytes** of the
request, and the single most common way to lose a day on this integration is to
let FastAPI parse the body, re-serialize it, and hash that instead -- which
produces a valid-looking HMAC over bytes Slack never sent, and fails 100% of
requests with an error message that says nothing.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incidentpilot.api.security import sign_slack
from incidentpilot.config.settings import Settings
from incidentpilot.main import create_app
from incidentpilot.orchestration.streams import StreamProducer
from incidentpilot.transcript.ingestor import IngestResult
from tests.conftest import SLACK_SECRET, FakeValkey

CHANNEL = "C0INCIDENT"


class RecordingIngestor:
    """Stands in for the real ingestor so the receiver can be tested alone."""

    def __init__(self, *, explode: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self._explode = explode

    async def on_event(self, event: dict[str, Any]) -> IngestResult:
        if self._explode:
            raise RuntimeError("database unreachable")
        self.events.append(event)
        return IngestResult(
            stored=True, reason="stored", incident_id=1, message_id=len(self.events)
        )


@pytest.fixture
def ingestor() -> RecordingIngestor:
    return RecordingIngestor()


def _build(cfg: Settings, ingestor: Any) -> FastAPI:
    valkey = FakeValkey()
    streams = StreamProducer(
        valkey,
        maxlen=cfg.stream_maxlen,
        raw_stream=cfg.stream_alerts_raw,
        resolved_stream=cfg.stream_alerts_resolved,
        wal_stream=cfg.stream_alerts_wal,
    )
    return create_app(cfg, valkey=valkey, streams=streams, ingestor=ingestor)


@pytest.fixture
def slack_client(test_settings: Settings, ingestor: RecordingIngestor) -> Any:
    with TestClient(_build(test_settings, ingestor)) as client:
        yield client


def _signed(body: dict[str, Any], *, secret: str = SLACK_SECRET, age_s: int = 0) -> Any:
    """Serialize once and sign *those* bytes -- the whole point of the test."""
    raw = json.dumps(body).encode()
    timestamp = str(int(time.time()) - age_s)
    return raw, {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": sign_slack(raw, timestamp, secret),
    }


def _message_event(ts: str = "1757000000.000100", text: str = "rolling back") -> dict[str, Any]:
    return {
        "type": "event_callback",
        "event_id": "Ev0001",
        "event": {"type": "message", "channel": CHANNEL, "ts": ts, "user": "U1", "text": text},
    }


# --- the trust boundary -------------------------------------------------------


def test_slack_sig_over_raw_body(slack_client: Any, ingestor: RecordingIngestor) -> None:
    raw, headers = _signed(_message_event())
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)

    assert response.status_code == 200
    assert response.json()["stored"] is True
    assert ingestor.events[0]["ts"] == "1757000000.000100"


def test_reserialized_body_fails_verification(slack_client: Any) -> None:
    """The bug this endpoint is built to avoid, asserted directly.

    Same JSON, different bytes: a space after each separator. The signature is
    over bytes, so it must not verify -- if it did, the handler would be hashing
    something other than what Slack sent.
    """
    payload = _message_event()
    raw, headers = _signed(payload)
    reserialized = json.dumps(payload, indent=2).encode()
    assert reserialized != raw

    response = slack_client.post("/webhooks/slack/events", content=reserialized, headers=headers)
    assert response.status_code == 401


def test_unsigned_request_is_rejected(slack_client: Any) -> None:
    raw = json.dumps(_message_event()).encode()
    assert slack_client.post("/webhooks/slack/events", content=raw).status_code == 401


def test_wrong_secret_is_rejected(slack_client: Any) -> None:
    raw, headers = _signed(_message_event(), secret="not-the-signing-secret")
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 401


def test_stale_timestamp_is_rejected(slack_client: Any) -> None:
    """A correctly signed replay from an hour ago is still an attack."""
    raw, headers = _signed(_message_event(), age_s=3600)
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 401


def test_ingestor_never_runs_on_a_rejected_request(
    slack_client: Any, ingestor: RecordingIngestor
) -> None:
    raw = json.dumps(_message_event()).encode()
    slack_client.post("/webhooks/slack/events", content=raw)
    assert ingestor.events == []


# --- the protocol -------------------------------------------------------------


def test_url_verification_echoes_the_challenge(slack_client: Any) -> None:
    raw, headers = _signed({"type": "url_verification", "challenge": "abc123"})
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}


def test_url_verification_is_still_signature_checked(slack_client: Any) -> None:
    """An unsigned challenge is a stranger asking us to echo a string."""
    raw = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    assert slack_client.post("/webhooks/slack/events", content=raw).status_code == 401


def test_unknown_envelope_is_acknowledged_not_processed(
    slack_client: Any, ingestor: RecordingIngestor
) -> None:
    """Acked so Slack stops retrying something nothing here handles."""
    raw, headers = _signed({"type": "app_rate_limited", "minute_rate_limited": 1})
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 200
    assert ingestor.events == []


def test_retry_delivery_is_still_processed(slack_client: Any, ingestor: RecordingIngestor) -> None:
    """The retry header is logged, never used for dedup.

    Deduplication is ``UNIQUE (channel_id, ts)``, which keeps working across a
    restart -- a header cannot.
    """
    raw, headers = _signed(_message_event())
    headers |= {"X-Slack-Retry-Num": "2", "X-Slack-Retry-Reason": "http_timeout"}
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 200
    assert len(ingestor.events) == 1


def test_malformed_json_is_a_400_not_a_500(slack_client: Any) -> None:
    raw = b"{not json"
    timestamp = str(int(time.time()))
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": sign_slack(raw, timestamp, SLACK_SECRET),
    }
    response = slack_client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 400


# --- durability ---------------------------------------------------------------


def test_storage_failure_returns_5xx_so_slack_redelivers(test_settings: Settings) -> None:
    """The most important behaviour in this file.

    Slack's retry *is* our durability guarantee: a non-2xx gets the event
    redelivered, a 2xx does not. Swallowing a database failure and answering 200
    would trade one retry for permanent, silent data loss -- and a PIR built on
    a transcript with a hole in it is exactly the B-01 failure in a new costume.
    """
    with TestClient(_build(test_settings, RecordingIngestor(explode=True))) as client:
        raw, headers = _signed(_message_event())
        response = client.post("/webhooks/slack/events", content=raw, headers=headers)
    assert response.status_code == 500
