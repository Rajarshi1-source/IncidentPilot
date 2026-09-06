"""Stream consumer mechanics (W2-13).

Exercised against an in-memory fake rather than a live Valkey, because the
properties that matter here are *decisions*, not Redis semantics: what gets
ACKed, what is deliberately left in the Pending Entries List, and what the
reclaim loop picks up. Redis semantics are covered by the integration suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from incidentpilot.domain.correlation import Decision
from incidentpilot.domain.normalize import (
    AlertSource,
    AlertStatus,
    NormalizedAlert,
    to_stream_fields,
)
from incidentpilot.orchestration.consumer import (
    AlertConsumer,
    _decode,
    alert_from_stream,
    ensure_group,
)
from incidentpilot.orchestration.orchestrator import IngestOutcome


def _alert(name: str = "PostgresPrimaryDown") -> NormalizedAlert:
    return NormalizedAlert(
        source=AlertSource.ALERTMANAGER,
        status=AlertStatus.FIRING,
        fingerprint="f" * 32,
        dedup_key="dk-1",
        alertname=name,
        service="postgres-primary",
        severity="sev1",
        severity_rank=4,
        starts_at=datetime(2026, 9, 6, 9, 14, 2, tzinfo=UTC),
        ends_at=None,
        labels={"alertname": name, "service": "postgres-primary", "severity": "critical"},
        annotations={"summary": "down"},
        raw={"labels": {"alertname": name}},
    )


class FakeStreamClient:
    """Enough Redis for the consumer's decisions to be observable."""

    def __init__(self, entries: list[tuple[str, dict[str, str]]] | None = None) -> None:
        self.pending = list(entries or [])
        self.acked: list[str] = []
        self.groups: list[tuple[str, str]] = []
        self.reclaimable: list[tuple[str, dict[str, str]]] = []
        self._served = False

    async def xgroup_create(
        self,
        stream: str,
        group: str,
        id: str = "0",  # noqa: A002 - redis-py names it `id`
        mkstream: bool = False,
    ) -> None:
        self.groups.append((stream, group))

    async def xreadgroup(
        self, group: str, consumer: str, streams: dict[str, str], count: int = 10, block: int = 0
    ) -> Any:
        if self._served or not self.pending:
            return []
        self._served = True
        stream = next(iter(streams))
        return [(stream, list(self.pending))]

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append(message_id)
        return 1

    async def xautoclaim(
        self, stream: str, group: str, consumer: str, min_idle_time: int, start_id: str, count: int
    ) -> Any:
        claimed, self.reclaimable = self.reclaimable, []
        return ["0-0", claimed, []]

    async def xpending(self, stream: str, group: str) -> Any:
        return {"pending": len(self.pending) - len(self.acked)}


class RecordingOrchestrator:
    """Stands in for the real orchestrator; records what it was asked to do."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ingested: list[NormalizedAlert] = []
        self.fail = fail

    async def ingest(self, session: Any, alert: NormalizedAlert) -> IngestOutcome:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.ingested.append(alert)
        return IngestOutcome(
            incident_id=1, created=True, decision=Decision.new_incident(), was_new_alert=True
        )

    async def refresh_root_signal(self, session: Any, incident_id: int) -> str | None:
        return "PostgresPrimaryDown"


class FakeSessionFactory:
    def __call__(self) -> Any:
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> Any:
        return self


def _entry(alert: NormalizedAlert, entry_id: str = "1-0") -> tuple[str, dict[str, str]]:
    return (entry_id, to_stream_fields(alert))


# --- round trip ---------------------------------------------------------------


def test_alert_survives_a_round_trip_through_the_stream() -> None:
    """One encoding, one decoder. A field added on one side and forgotten on
    the other should be a decode failure, not a silent None."""
    original = _alert()
    restored = alert_from_stream(to_stream_fields(original))

    assert restored.alertname == original.alertname
    assert restored.dedup_key == original.dedup_key
    assert restored.severity_rank == original.severity_rank
    assert restored.starts_at == original.starts_at
    assert restored.labels == original.labels
    assert restored.service == original.service


def test_round_trip_preserves_a_null_end_time() -> None:
    restored = alert_from_stream(to_stream_fields(_alert()))
    assert restored.ends_at is None


def test_decode_normalizes_bytes_fields() -> None:
    """redis-py returns bytes unless decode_responses is set; the consumer must
    not care which client it was handed."""
    decoded = _decode({b"alertname": b"X", "service": "y"})
    assert decoded == {"alertname": "X", "service": "y"}


# --- consumption --------------------------------------------------------------


async def test_valid_entry_is_handled_and_acked() -> None:
    client = FakeStreamClient([_entry(_alert())])
    orch = RecordingOrchestrator()
    consumer = AlertConsumer(client, FakeSessionFactory(), orch)

    handled = await consumer.poll_once()

    assert handled == 1
    assert client.acked == ["1-0"]
    assert len(orch.ingested) == 1


async def test_a_failed_handler_leaves_the_entry_unacked() -> None:
    """The recovery path. Leaving it in the Pending Entries List is what lets
    XAUTOCLAIM redeliver it -- swallowing the exception into a success would
    lose the alert silently, which is the worst outcome in this system.
    """
    client = FakeStreamClient([_entry(_alert())])
    consumer = AlertConsumer(
        client,
        FakeSessionFactory(),
        RecordingOrchestrator(fail=True),
    )

    handled = await consumer.poll_once()

    assert handled == 0
    assert client.acked == [], "a failed entry must stay claimed for redelivery"


async def test_an_undecodable_entry_is_acked_not_retried_forever() -> None:
    """A malformed entry will never succeed on retry, so it must not occupy the
    PEL indefinitely. It is ACKed and logged loudly -- dropping it silently is
    how you lose an alert without noticing."""
    client = FakeStreamClient([("9-0", {"source": "alertmanager"})])  # missing everything else
    orch = RecordingOrchestrator()
    consumer = AlertConsumer(client, FakeSessionFactory(), orch)

    handled = await consumer.poll_once()

    assert handled == 1
    assert client.acked == ["9-0"]
    assert orch.ingested == [], "a malformed entry must not reach the orchestrator"


async def test_empty_stream_returns_zero() -> None:
    consumer = AlertConsumer(
        FakeStreamClient([]),
        FakeSessionFactory(),
        RecordingOrchestrator(),
    )
    assert await consumer.poll_once() == 0


async def test_a_batch_is_handled_in_order() -> None:
    alerts = [_alert(f"Alert{i}") for i in range(5)]
    client = FakeStreamClient([_entry(a, f"{i + 1}-0") for i, a in enumerate(alerts)])
    orch = RecordingOrchestrator()
    consumer = AlertConsumer(client, FakeSessionFactory(), orch)

    assert await consumer.poll_once() == 5
    assert [a.alertname for a in orch.ingested] == [f"Alert{i}" for i in range(5)]


# --- the reclaim loop ---------------------------------------------------------


async def test_stale_entry_reclaimed() -> None:
    """The piece most implementations omit.

    Without XAUTOCLAIM, a crashed worker's claimed entries sit in the Pending
    Entries List forever: not done, not redelivered, not visible. This loop is
    what makes at-least-once delivery actually deliver.
    """
    client = FakeStreamClient([])
    client.reclaimable = [_entry(_alert("StaleAlert"), "7-0")]
    orch = RecordingOrchestrator()
    consumer = AlertConsumer(client, FakeSessionFactory(), orch)

    reclaimed = await consumer.reclaim_stale()

    assert reclaimed == 1
    assert client.acked == ["7-0"]
    assert orch.ingested[0].alertname == "StaleAlert"


async def test_reclaim_with_nothing_stale_is_a_no_op() -> None:
    consumer = AlertConsumer(
        FakeStreamClient([]),
        FakeSessionFactory(),
        RecordingOrchestrator(),
    )
    assert await consumer.reclaim_stale() == 0


async def test_a_reclaimed_entry_that_fails_again_stays_unacked() -> None:
    client = FakeStreamClient([])
    client.reclaimable = [_entry(_alert(), "7-0")]
    consumer = AlertConsumer(
        client,
        FakeSessionFactory(),
        RecordingOrchestrator(fail=True),
    )

    assert await consumer.reclaim_stale() == 0
    assert client.acked == []


# --- group setup and shutdown -------------------------------------------------


async def test_ensure_group_tolerates_an_existing_group() -> None:
    """A worker restart must not crash because the group is already there."""

    class Existing(FakeStreamClient):
        async def xgroup_create(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")

    await ensure_group(Existing(), "alerts.raw", "cg-orchestrate")  # must not raise


async def test_ensure_group_reraises_a_real_error() -> None:
    class Broken(FakeStreamClient):
        async def xgroup_create(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("NOAUTH authentication required")

    with pytest.raises(RuntimeError, match="NOAUTH"):
        await ensure_group(Broken(), "alerts.raw", "cg-orchestrate")


async def test_stop_halts_the_run_loop() -> None:
    """SIGTERM path: stop claiming new entries, finish what we hold."""
    consumer = AlertConsumer(
        FakeStreamClient([]),
        FakeSessionFactory(),
        RecordingOrchestrator(),
    )
    consumer.stop()
    await consumer.run()  # returns immediately rather than looping forever


async def test_lag_gauge_is_published_after_a_poll() -> None:
    """Queue depth, not CPU, is what the HPA scales on: a worker blocked on a
    slow external call uses no CPU at all while the backlog grows."""
    from incidentpilot.telemetry.metrics import REGISTRY

    client = FakeStreamClient([_entry(_alert())])
    consumer = AlertConsumer(client, FakeSessionFactory(), RecordingOrchestrator())
    await consumer.poll_once()

    value = REGISTRY.get_sample_value(
        "ip_stream_pending", {"stream": "alerts.raw", "group": "cg-orchestrate"}
    )
    assert value is not None


async def test_telemetry_failure_never_breaks_the_consumer() -> None:
    """Metrics are the least important thing in the process."""

    class NoPending(FakeStreamClient):
        async def xpending(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("xpending unavailable")

    client = NoPending([_entry(_alert())])
    consumer = AlertConsumer(client, FakeSessionFactory(), RecordingOrchestrator())
    assert await consumer.poll_once() == 1
