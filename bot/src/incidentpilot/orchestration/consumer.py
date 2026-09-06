"""Stream consumer: XREADGROUP, advisory-lock partitioning, XAUTOCLAIM reclaim.

Consumer groups give at-least-once delivery *with visibility*: XREADGROUP
claims, XACK releases, and un-ACKed entries stay in the Pending Entries List, so
a pod eviction mid-orchestration loses nothing.

The reclaim loop is the piece most implementations omit. Without XAUTOCLAIM, a
crashed worker's claimed entries are stranded in the PEL forever -- the work is
neither done nor redelivered, and nothing surfaces it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Protocol

from incidentpilot.domain.normalize import (
    AlertSource,
    AlertStatus,
    NormalizedAlert,
)
from incidentpilot.orchestration.orchestrator import IngestOutcome
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import STREAM_PENDING

log = get_logger(__name__)

DEFAULT_GROUP = "cg-orchestrate"
RECLAIM_IDLE_MS = 60_000


class SessionFactory(Protocol):
    """Callable that yields a transactional session context.

    A Protocol rather than ``async_sessionmaker[AsyncSession]`` for the same
    reason ``StreamClient`` exists: the consumer only calls the factory and uses
    the session as a context manager, so depending on the concrete type would
    make the in-memory fake untypeable and push the seam into a cast.
    """

    def __call__(self) -> AbstractAsyncContextManager[Any]: ...


class IngestOrchestrator(Protocol):
    """What the consumer needs from the orchestrator."""

    async def ingest(self, session: Any, alert: NormalizedAlert) -> IngestOutcome: ...

    async def refresh_root_signal(self, session: Any, incident_id: int) -> str | None: ...


def alert_from_stream(fields: dict[str, str]) -> NormalizedAlert:
    """Rebuild a NormalizedAlert from stream fields.

    The inverse of ``to_stream_fields``. Kept adjacent to nothing else on
    purpose: there is exactly one encoding of an alert on the wire and exactly
    one decoder, so a field added on one side and forgotten on the other is a
    type error rather than a silent None.
    """
    return NormalizedAlert(
        source=AlertSource(fields["source"]),
        status=AlertStatus(fields["status"]),
        fingerprint=fields["fingerprint"],
        dedup_key=fields["dedup_key"],
        alertname=fields["alertname"],
        service=fields.get("service") or None,
        severity=fields["severity"],
        severity_rank=int(fields["severity_rank"]),
        starts_at=datetime.fromisoformat(fields["starts_at"]),
        ends_at=datetime.fromisoformat(fields["ends_at"]) if fields.get("ends_at") else None,
        labels=json.loads(fields.get("labels") or "{}"),
        annotations=json.loads(fields.get("annotations") or "{}"),
        generator_url=fields.get("generator_url") or None,
        raw=json.loads(fields.get("raw") or "{}"),
    )


async def ensure_group(client: Any, stream: str, group: str) -> None:
    """Create the consumer group, tolerating the case where it exists.

    ``mkstream=True`` so a worker can start before the first alert ever arrives
    -- otherwise the first deploy of a quiet environment crashes on a missing
    key, which looks like a bug and is not.
    """
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
        log.info("consumer.group_created", stream=stream, group=group)
    except Exception as exc:  # BUSYGROUP: already exists
        if "BUSYGROUP" not in str(exc):
            raise


class AlertConsumer:
    def __init__(
        self,
        client: Any,
        session_factory: SessionFactory,
        orchestrator: IngestOrchestrator,
        *,
        stream: str = "alerts.raw",
        group: str = DEFAULT_GROUP,
        consumer_name: str = "worker-1",
        batch: int = 10,
        block_ms: int = 2000,
    ) -> None:
        self._client = client
        self._sessions = session_factory
        self._orchestrator = orchestrator
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name
        self._batch = batch
        self._block_ms = block_ms
        self._stopping = False

    def stop(self) -> None:
        """SIGTERM path: stop claiming new entries, finish what we hold."""
        self._stopping = True

    async def run(self) -> None:
        await ensure_group(self._client, self.stream, self.group)
        while not self._stopping:
            processed = await self.poll_once()
            if processed == 0:
                await self.reclaim_stale()

    async def poll_once(self) -> int:
        """Claim a batch and handle it. Returns how many entries were handled."""
        entries: Any = await self._client.xreadgroup(
            self.group,
            self.consumer_name,
            {self.stream: ">"},
            count=self._batch,
            block=self._block_ms,
        )
        if not entries:
            return 0

        handled = 0
        for _stream, messages in entries:
            for message_id, fields in messages:
                if await self._handle(message_id, _decode(fields)):
                    await self._client.xack(self.stream, self.group, message_id)
                    handled += 1
        await self._publish_lag()
        return handled

    async def _handle(self, message_id: str, fields: dict[str, str]) -> bool:
        """Process one entry. Returns True if it should be ACKed.

        Returning False leaves the entry in the Pending Entries List, where
        XAUTOCLAIM will pick it up after the idle timeout. That is the recovery
        path for a transient failure, and it is why nothing here swallows an
        exception into a success.
        """
        try:
            alert = alert_from_stream(fields)
        except (KeyError, ValueError) as exc:
            # A malformed entry will never succeed on retry. ACK it so it does
            # not occupy the PEL forever, and log loudly -- silently dropping is
            # how you lose an alert without noticing.
            log.error("consumer.undecodable_entry", message_id=message_id, error=str(exc))
            return True

        try:
            async with self._sessions() as session, session.begin():
                outcome = await self._orchestrator.ingest(session, alert)
                if outcome.created or outcome.was_new_alert:
                    await self._orchestrator.refresh_root_signal(session, outcome.incident_id)
            self._log_outcome(alert, outcome)
            return True
        except Exception as exc:
            log.warning(
                "consumer.handle_failed",
                message_id=message_id,
                alertname=alert.alertname,
                error=str(exc),
            )
            return False  # un-ACKed; XAUTOCLAIM redelivers

    def _log_outcome(self, alert: NormalizedAlert, outcome: IngestOutcome) -> None:
        log.info(
            "consumer.handled",
            alertname=alert.alertname,
            incident_id=outcome.incident_id,
            created=outcome.created,
            duplicate=not outcome.was_new_alert,
            explanation=outcome.decision.explanation,
        )

    async def reclaim_stale(self) -> int:
        """Reclaim entries a dead consumer left claimed.

        Without this, a crashed worker's entries sit in the PEL indefinitely:
        not done, not redelivered, not visible. This is the loop that makes
        at-least-once delivery actually deliver.
        """
        cursor = "0-0"
        reclaimed = 0
        while True:
            result: Any = await self._client.xautoclaim(
                self.stream,
                self.group,
                self.consumer_name,
                min_idle_time=RECLAIM_IDLE_MS,
                start_id=cursor,
                count=self._batch,
            )
            cursor, claimed = result[0], result[1]
            for message_id, fields in claimed:
                if await self._handle(message_id, _decode(fields)):
                    await self._client.xack(self.stream, self.group, message_id)
                    reclaimed += 1
            if cursor in ("0-0", b"0-0") or not claimed:
                break

        if reclaimed:
            log.warning("consumer.reclaimed", stream=self.stream, count=reclaimed)
        return reclaimed

    async def _publish_lag(self) -> None:
        """Emit PEL depth -- the metric the HPA scales on.

        Queue depth rather than CPU, because a worker blocked on a slow external
        call uses no CPU at all while the backlog grows.
        """
        try:
            pending: Any = await self._client.xpending(self.stream, self.group)
            depth = pending.get("pending", 0) if isinstance(pending, dict) else 0
            STREAM_PENDING.labels(stream=self.stream, group=self.group).set(depth)
        except Exception:  # never let telemetry break the consumer
            pass


def _decode(fields: Any) -> dict[str, str]:
    """Normalize bytes-or-str field maps into str/str."""
    out: dict[str, str] = {}
    for key, value in dict(fields).items():
        k = key.decode() if isinstance(key, bytes) else str(key)
        v = value.decode() if isinstance(value, bytes) else str(value)
        out[k] = v
    return out


async def run_worker(
    client: Any,
    session_factory: SessionFactory,
    orchestrator: IngestOrchestrator,
    *,
    stream: str = "alerts.raw",
    consumer_name: str = "worker-1",
    stop_check: Callable[[], bool] | None = None,
) -> None:
    """Entrypoint used by the worker process and the compose service."""
    consumer = AlertConsumer(
        client, session_factory, orchestrator, stream=stream, consumer_name=consumer_name
    )
    if stop_check is None:
        await consumer.run()
        return

    await ensure_group(client, stream, consumer.group)
    while not stop_check():
        if await consumer.poll_once() == 0:
            await consumer.reclaim_stale()
            await asyncio.sleep(0)


__all__ = [
    "AlertConsumer",
    "alert_from_stream",
    "ensure_group",
    "run_worker",
    "utcnow_iso",
]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()
