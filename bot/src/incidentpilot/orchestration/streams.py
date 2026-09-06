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
from incidentpilot.telemetry.tracing import carrier_for_stream


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

    async def ping(self) -> bool:
        """Readiness probe support."""
        try:
            return bool(await self._client.ping())
        except Exception:
            return False
