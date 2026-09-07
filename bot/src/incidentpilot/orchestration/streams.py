"""Valkey Streams producer.

The webhook's whole job is: verify, normalize, XADD, return 202. The 202 means
"I have durably accepted this", nothing more -- Alertmanager's HTTP timeout is
not our orchestration budget. Everything slow happens in the worker.

Streams rather than a plain list because consumer groups give at-least-once
delivery *with visibility*: XREADGROUP claims, XACK releases, and un-ACKed
entries stay in the Pending Entries List, so a pod eviction mid-orchestration
loses nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable

from incidentpilot.domain.normalize import NormalizedAlert, to_stream_fields
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.tracing import carrier_for_stream

log = get_logger(__name__)


class StreamPipeline(Protocol):
    """The two pipeline operations the ingest path uses."""

    def xadd(
        self,
        name: str,
        fields: Any,
        *,
        maxlen: int | None = ...,
        approximate: bool = ...,
    ) -> Any: ...

    def execute(self) -> Awaitable[list[Any]]: ...


@runtime_checkable
class StreamClient(Protocol):
    """The narrow slice of Valkey/Redis that publishing actually needs.

    Typing against this rather than the concrete ``Redis`` class is what makes
    the in-memory fake a *typed* substitute rather than a cast. Every boundary
    is an adapter, so every boundary is fakeable -- that property is what the
    week 7 replay harness is built on, and it is cheaper to establish now than
    to retrofit.

    Note the return types: ``Awaitable[Any]``, not ``async def``. redis-py's
    methods are declared to return ``Awaitable``, not a coroutine, so a Protocol
    written with ``async def`` accepts the in-memory fake and *rejects the real
    client* -- exactly backwards. ``Awaitable`` admits both, which is the point
    of having the seam at all.
    """

    def xadd(
        self,
        name: str,
        fields: Any,
        *,
        maxlen: int | None = ...,
        approximate: bool = ...,
    ) -> Awaitable[Any]: ...

    def ping(self) -> Awaitable[Any]: ...

    def pipeline(self, transaction: bool = ...) -> StreamPipeline: ...

    # The three the brownout drain needs (W7-19). Added to the Protocol rather
    # than reached for with getattr: the WAL replay is a correctness path, and a
    # fake that does not implement it should fail to type-check rather than
    # fail at 3 a.m.
    def xrange(self, name: str, *, count: int | None = ...) -> Awaitable[Any]: ...

    def xdel(self, name: str, *ids: Any) -> Awaitable[Any]: ...

    def xlen(self, name: str) -> Awaitable[Any]: ...


class StreamProducer:
    """Publishes normalized alerts onto durable streams.

    Holds a client rather than creating one per call: connection setup on the
    ingest path would blow the 250 ms p99 budget on the first request after any
    idle period.
    """

    def __init__(
        self,
        client: StreamClient,
        *,
        maxlen: int = 100_000,
        raw_stream: str = "alerts.raw",
        resolved_stream: str = "alerts.resolved",
        wal_stream: str = "alerts.wal",
    ) -> None:
        self._client = client
        self._maxlen = maxlen
        self.raw_stream = raw_stream
        self.resolved_stream = resolved_stream
        self.wal_stream = wal_stream

    def stream_for(self, alert: NormalizedAlert) -> str:
        """Resolved alerts go to their own stream.

        They drive mitigation and auto-resolve logic, not incident creation.
        Mixing them into ``alerts.raw`` means a *resolve* can create an
        incident -- a genuinely confusing bug to chase at 3 a.m.
        """
        return self.resolved_stream if alert.status == "resolved" else self.raw_stream

    async def publish(self, alert: NormalizedAlert, *, stream: str | None = None) -> str:
        """XADD one alert. Returns the stream entry id."""
        fields: dict[str, str] = to_stream_fields(alert)
        # Carry W3C trace context with the entry: the worker consumes it in a
        # different process, and without this it starts an unparented trace,
        # losing the causal link the trace exists to show.
        fields.update(carrier_for_stream())

        target = stream or self.stream_for(alert)
        entry_id: bytes | str = await self._client.xadd(
            target,
            fields,
            maxlen=self._maxlen,
            approximate=True,  # O(1) amortised; exact trimming is not
        )
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    async def publish_many(self, alerts: list[NormalizedAlert]) -> list[str]:
        """Publish a batch in one round trip.

        An Alertmanager payload routinely carries dozens of alerts, and a storm
        carries forty. Issuing forty sequential XADDs turns one request into
        forty network round trips inside the 250 ms budget.
        """
        if not alerts:
            return []

        pipe = self._client.pipeline(transaction=False)
        for alert in alerts:
            fields = to_stream_fields(alert)
            fields.update(carrier_for_stream())
            pipe.xadd(
                self.stream_for(alert),
                fields,
                maxlen=self._maxlen,
                approximate=True,
            )
        results: list[Any] = await pipe.execute()
        return [r.decode() if isinstance(r, bytes) else str(r) for r in results]

    async def buffer_wal(self, alert: NormalizedAlert) -> str:
        """Brownout (L2) write-ahead path.

        A dropped alert is unrecoverable; a delayed one is not. That asymmetry
        is why ingest is the one AP component in an otherwise CP system.
        """
        return await self.publish(alert, stream=self.wal_stream)

    async def replay_wal(self, *, batch: int = 500) -> int:
        """Drain the brownout buffer back onto the primary stream (W7-19).

        **In order, and only on success.** Each entry is republished and then
        deleted, so a crash mid-drain replays a handful of alerts twice rather
        than losing them -- and the incident dedup key makes the duplicate
        harmless while the loss would not be. Deleting first would be faster and
        would silently drop whatever was in flight when the process died, which
        is the failure the buffer existed to prevent.

        Batched because a long brownout can leave thousands of entries, and
        draining them in one read would hold the whole backlog in memory on a
        process that has just recovered from being unhealthy.
        """
        entries: list[Any] = await self._client.xrange(self.wal_stream, count=batch)
        if not entries:
            return 0

        replayed = 0
        for entry_id, fields in entries:
            decoded = {
                (k.decode() if isinstance(k, bytes) else str(k)): (
                    v.decode() if isinstance(v, bytes) else str(v)
                )
                for k, v in dict(fields).items()
            }
            await self._client.xadd(self.raw_stream, decoded, maxlen=self._maxlen, approximate=True)
            await self._client.xdel(self.wal_stream, entry_id)
            replayed += 1

        log.info("wal.replayed", count=replayed, stream=self.wal_stream)
        return replayed

    async def wal_depth(self) -> int:
        """How much is still buffered. Surfaced so a brownout is visible, not inferred."""
        try:
            return int(await self._client.xlen(self.wal_stream))
        except Exception:
            return 0

    async def ping(self) -> bool:
        """Readiness probe support."""
        try:
            return bool(await self._client.ping())
        except Exception:
            return False
