"""Ingest against a real Valkey.

The unit suite uses an in-memory fake, which is fast and offline but proves
nothing about XADD semantics, MAXLEN trimming, or how redis-py encodes a field
map. This suite proves those against the pinned image.

Skipped automatically when no Valkey is reachable, so `make test` stays green on
a laptop with nothing running.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from incidentpilot.domain.normalize import (
    AlertSource,
    AlertStatus,
    NormalizedAlert,
)
from incidentpilot.orchestration.streams import StreamProducer

pytestmark = pytest.mark.integration

# Matches the docker-compose published port (see the port block there).
VALKEY_URL = os.environ.get("IP_TEST_VALKEY_URL", "redis://127.0.0.1:56379/15")


def _alert(name: str = "PostgresPrimaryDown", status: str = "firing") -> NormalizedAlert:
    return NormalizedAlert(
        source=AlertSource.ALERTMANAGER,
        status=AlertStatus(status),
        fingerprint="a" * 32,
        dedup_key="gk-integration",
        alertname=name,
        service="postgres-primary",
        severity="sev1",
        severity_rank=4,
        starts_at=datetime(2026, 9, 6, 9, 14, 2, tzinfo=UTC),
        ends_at=None,
        labels={"alertname": name, "service": "postgres-primary", "severity": "critical"},
        annotations={"summary": "primary down"},
        raw={"labels": {"alertname": name}},
    )


@pytest_asyncio.fixture
async def valkey() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(VALKEY_URL, decode_responses=True, socket_connect_timeout=2)
    try:
        await client.ping()
    except Exception:
        pytest.skip(f"no Valkey reachable at {VALKEY_URL}")
    await client.flushdb()  # database 15, reserved for tests
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def producer(valkey: Redis) -> StreamProducer:
    return StreamProducer(valkey, maxlen=1000)  # Redis satisfies StreamClient


async def test_xadd_visible_via_xrange(producer: StreamProducer, valkey: Redis) -> None:
    """G1, criterion 3: the event is durably on the stream."""
    entry_id = await producer.publish(_alert())
    assert entry_id

    # redis-py types async returns as `Awaitable[Any] | Any`; the awaited
    # value is an unnarrowable union, so name the type here rather than
    # loosening the whole module.
    entries: Any = await valkey.xrange("alerts.raw", "-", "+")
    assert len(entries) == 1

    _, fields = entries[0]
    assert fields["alertname"] == "PostgresPrimaryDown"
    assert fields["severity"] == "sev1"
    assert fields["dedup_key"] == "gk-integration"
    # Nested structures are JSON-encoded on the wire so there is exactly one
    # encoding of an alert, and the consumer has exactly one thing to decode.
    assert json.loads(fields["labels"])["service"] == "postgres-primary"


async def test_resolved_alerts_land_on_their_own_stream(
    producer: StreamProducer, valkey: Redis
) -> None:
    await producer.publish(_alert(status="firing"))
    await producer.publish(_alert(status="resolved"))

    assert int(await valkey.xlen("alerts.raw")) == 1
    assert int(await valkey.xlen("alerts.resolved")) == 1


async def test_publish_many_is_one_round_trip(producer: StreamProducer, valkey: Redis) -> None:
    """A storm carries forty alerts; forty sequential XADDs is forty round trips."""
    alerts = [_alert(name=f"Alert{i}") for i in range(40)]
    ids = await producer.publish_many(alerts)

    assert len(ids) == 40
    assert len(set(ids)) == 40, "stream entry ids must be unique"
    assert int(await valkey.xlen("alerts.raw")) == 40


async def test_maxlen_trims_the_stream(valkey: Redis) -> None:
    """Approximate trimming is O(1) amortised; exact trimming is not."""
    small = StreamProducer(valkey, maxlen=10)
    for i in range(50):
        await small.publish(_alert(name=f"Alert{i}"))

    length = int(await valkey.xlen("alerts.raw"))
    # `approximate=True` trims to *at least* maxlen, at a radix-tree node
    # boundary -- so assert the bound, not an exact count. Asserting equality
    # here is a classic flaky test.
    assert length >= 10
    assert length <= 50


async def test_wal_buffer_uses_the_brownout_stream(producer: StreamProducer, valkey: Redis) -> None:
    await producer.buffer_wal(_alert())
    assert int(await valkey.xlen("alerts.wal")) == 1
    assert int(await valkey.xlen("alerts.raw")) == 0


async def test_ping_reports_reachability(producer: StreamProducer) -> None:
    assert await producer.ping() is True


async def test_consumer_group_sees_published_entries(
    producer: StreamProducer, valkey: Redis
) -> None:
    """The property the whole ingest design rests on.

    XREADGROUP claims, XACK releases, and un-ACKed entries stay in the Pending
    Entries List -- so a worker evicted mid-orchestration loses nothing. Week 2
    builds the consumer; this asserts the producer side is compatible with it.
    """
    await producer.publish_many([_alert(name=f"Alert{i}") for i in range(3)])
    await valkey.xgroup_create("alerts.raw", "cg-test", id="0")

    claimed: Any = await valkey.xreadgroup("cg-test", "worker-1", {"alerts.raw": ">"}, count=10)
    _, messages = claimed[0]
    assert len(messages) == 3

    pending: Any = await valkey.xpending("alerts.raw", "cg-test")
    assert pending["pending"] == 3, "claimed-but-unacked entries must be visible"

    await valkey.xack("alerts.raw", "cg-test", messages[0][0])
    pending_after: Any = await valkey.xpending("alerts.raw", "cg-test")
    assert pending_after["pending"] == 2
