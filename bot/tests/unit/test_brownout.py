"""Brownout write-ahead and replay (W7-19, D7 L2).

One asymmetry drives every assertion here: **a dropped alert is unrecoverable,
a delayed alert is not.** That is why ingest is the one AP component in an
otherwise CP system, and why the webhook falls *forward* into a durable buffer
rather than back into a 500.

The one case where it must not fall forward is when the buffer is gone too. A
202 means "durably accepted", and Alertmanager will not resend an alert it
believes we have -- so a 202 we cannot honour converts a recoverable delay into
an unrecoverable loss, which is the exact trade this module exists to avoid.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi.testclient import TestClient

from incidentpilot.orchestration.streams import StreamProducer
from incidentpilot.resilience.degradation import Level
from tests.conftest import ALERTMANAGER_BEARER, FakeValkey


def _post(client: TestClient, payload: dict[str, Any]) -> Any:
    return client.post(
        "/webhooks/alertmanager",
        headers={"Authorization": f"Bearer {ALERTMANAGER_BEARER}"},
        json=payload,
    )


def test_brownout_buffers_and_replays(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """Primary stream down: still 202, buffered to the WAL, replayed on recovery."""
    fake_valkey.fail_streams.add("alerts.raw")

    res = _post(client, alertmanager_payload)

    assert res.status_code == 202, "ingest must keep accepting during a brownout"
    assert res.json()["buffered"] == 2
    assert fake_valkey.entries("alerts.raw") == []
    assert len(fake_valkey.entries("alerts.wal")) == 2

    degradation = _degradation(client)
    assert degradation.level is Level.BROWNOUT
    assert degradation.said("brownout"), "a silent brownout fails INV-12"
    assert degradation.said("replayed on recovery")

    # Recovery: the buffer drains, in order, onto the primary stream.
    fake_valkey.fail_streams.clear()
    producer = StreamProducer(fake_valkey)
    replayed = _run(producer.replay_wal())

    assert replayed == 2
    assert len(fake_valkey.entries("alerts.raw")) == 2
    assert fake_valkey.entries("alerts.wal") == []
    assert {e["alertname"] for e in fake_valkey.entries("alerts.raw")} == {
        "PostgresPrimaryDown",
        "HighErrorRate",
    }


def test_replay_preserves_order(fake_valkey: FakeValkey) -> None:
    """A timeline product that replays its backlog out of order is worse than one
    that lost it: the incident that arrives second becomes the incident that
    started first, and correlation reasons from the wrong root."""
    producer = StreamProducer(fake_valkey)
    for index in range(5):
        _run(fake_valkey.xadd("alerts.wal", {"alertname": f"A{index}", "fingerprint": str(index)}))

    assert _run(producer.replay_wal()) == 5
    assert [e["alertname"] for e in fake_valkey.entries("alerts.raw")] == [
        "A0",
        "A1",
        "A2",
        "A3",
        "A4",
    ]


def test_replay_deletes_only_after_a_successful_republish(fake_valkey: FakeValkey) -> None:
    """Delete-then-publish would lose whatever was in flight when a drain crashed.

    Publish-then-delete replays a handful twice instead, and the incident dedup
    key makes a duplicate harmless while the loss would not be.
    """
    producer = StreamProducer(fake_valkey)
    _run(fake_valkey.xadd("alerts.wal", {"alertname": "A0", "fingerprint": "0"}))
    fake_valkey.fail_streams.add("alerts.raw")

    with contextlib.suppress(ConnectionError):
        _run(producer.replay_wal())

    assert len(fake_valkey.entries("alerts.wal")) == 1, "a failed drain must not consume the buffer"


def test_a_total_stream_failure_is_a_503_not_a_lying_202(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """When the buffer is gone too, say so.

    202 means "durably accepted". Alertmanager will not resend an alert it
    believes we have, so a 202 we cannot honour turns a recoverable delay into
    an unrecoverable loss -- the one outcome the whole brownout path exists to
    prevent.
    """
    fake_valkey.fail_streams.update({"alerts.raw", "alerts.wal"})

    res = _post(client, alertmanager_payload)

    assert res.status_code == 503
    assert fake_valkey.entries("alerts.raw") == []
    assert fake_valkey.entries("alerts.wal") == []


def test_the_healthy_path_buffers_nothing(
    client: TestClient, alertmanager_payload: dict[str, Any], fake_valkey: FakeValkey
) -> None:
    """The WAL is a fallback, not a second copy of every alert."""
    assert _post(client, alertmanager_payload).json()["buffered"] == 0
    assert fake_valkey.entries("alerts.wal") == []
    assert _degradation(client).level is Level.NORMAL


def test_wal_depth_is_observable(fake_valkey: FakeValkey) -> None:
    """A brownout must be visible on a dashboard, not inferred from a latency bump."""
    producer = StreamProducer(fake_valkey)
    assert _run(producer.wal_depth()) == 0
    _run(fake_valkey.xadd("alerts.wal", {"alertname": "A0", "fingerprint": "0"}))
    assert _run(producer.wal_depth()) == 1


def _degradation(client: TestClient) -> Any:
    """The manager the app built in its lifespan.

    Reached through the app rather than constructed here on purpose: the
    assertion is that the *webhook* moved the level, and a second manager built
    in the test would report NORMAL forever while the real one was in brownout.
    """
    return client.app.state.degradation  # type: ignore[attr-defined]


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
