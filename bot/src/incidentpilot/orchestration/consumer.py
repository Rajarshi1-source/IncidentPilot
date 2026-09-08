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
import contextlib
import json
import os
import signal
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.exc import DBAPIError

from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.domain.normalize import (
    AlertSource,
    AlertStatus,
    NormalizedAlert,
)
from incidentpilot.orchestration.orchestrator import IngestOutcome
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger
from incidentpilot.telemetry.metrics import STREAM_PENDING

log = get_logger(__name__)

# Three attempts, then fall through to the Pending Entries List. A deadlock that
# survives three immediate retries is not the transient kind, and spinning on it
# would hold the consumer off the rest of the storm.
DEADLOCK_RETRIES = 3
DEADLOCK_BACKOFF_S = 0.05

# 40P01 deadlock_detected, 40001 serialization_failure. Matched on SQLSTATE
# rather than message text, which is localised and changes between versions.
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})


def _is_retryable_conflict(exc: BaseException) -> bool:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return str(sqlstate) in RETRYABLE_SQLSTATES


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

    async def ensure_engaged(self, session: Any, incident_id: int) -> Any:
        """Drive a freshly detected incident to `engaged`, idempotently.

        On the Protocol rather than only on `Orchestrator` because the consumer
        calls it on every newly created incident: a fake orchestrator in a test
        that omitted it would type-check while the real war room never opened.
        """
        ...


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

        # A deadlock is transient by definition, and Postgres documents retry as
        # the correct response. The worker inserts outbox rows while the relay
        # claims them with FOR UPDATE SKIP LOCKED, and under a storm those two
        # touch `outbox_events.idempotency_key` in opposite orders often enough
        # to deadlock roughly once per forty-alert cascade.
        #
        # Without this the entry is left un-ACKed and XAUTOCLAIM recovers it --
        # which works, and was observed working, but costs the reclaim idle
        # timeout on every storm. Retrying in-process turns a minutes-long
        # recovery into a millisecond one, and the PEL path stays underneath as
        # the backstop for everything a retry cannot fix.
        for attempt in range(1, DEADLOCK_RETRIES + 1):
            try:
                return await self._ingest_once(alert)
            except DBAPIError as exc:
                if not _is_retryable_conflict(exc) or attempt == DEADLOCK_RETRIES:
                    log.warning(
                        "consumer.handle_failed",
                        message_id=message_id,
                        alertname=alert.alertname,
                        error=str(exc),
                    )
                    return False
                # Backoff scaled by attempt rather than randomised: the replay
                # harness runs this code, and a jittered sleep would make a
                # recorded incident score differently between runs (INV-10).
                log.info(
                    "consumer.deadlock_retry",
                    message_id=message_id,
                    alertname=alert.alertname,
                    attempt=attempt,
                )
                await asyncio.sleep(DEADLOCK_BACKOFF_S * attempt)
            except Exception as exc:
                log.warning(
                    "consumer.handle_failed",
                    message_id=message_id,
                    alertname=alert.alertname,
                    error=str(exc),
                )
                return False
        return False

    async def _ingest_once(self, alert: NormalizedAlert) -> bool:
        """One ingest transaction. Raises; the caller decides about retrying."""
        async with self._sessions() as session, session.begin():
            outcome = await self._orchestrator.ingest(session, alert)
            if outcome.created or outcome.was_new_alert:
                await self._orchestrator.refresh_root_signal(session, outcome.incident_id)
            if outcome.created:
                # Open the war room. `create_channel` hangs off the
                # TRIAGING -> ENGAGED transition, so without this an incident
                # sits in `detected` forever and nobody is ever told -- which is
                # what the running stack actually did until week 8's end-to-end
                # demo drove it through the real webhook.
                #
                # It went unnoticed because the crash matrix (G3) calls the
                # handlers directly and the state tests (G2) call `transition`
                # directly: both halves correct, nothing joining them. Same
                # shape as the un-awaited budget breaker and the schema that
                # existed as a side effect of another module running first.
                #
                # Idempotent by construction: `ensure_engaged` advances only
                # from DETECTED/TRIAGING and returns without transitioning
                # otherwise, so an XAUTOCLAIM redelivery cannot raise
                # InvalidTransition and strand a healthy entry in the DLQ.
                #
                # Only on `created`: an alert merging into an existing incident
                # must not re-drive a state machine that has moved on.
                await self._orchestrator.ensure_engaged(session, outcome.incident_id)
        self._log_outcome(alert, outcome)
        return True

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


# --- process entrypoint (W8-11) ----------------------------------------------
#
# `python -m incidentpilot.orchestration.consumer`. The compose stack and the
# Helm chart both run the worker as its own process, and until week 8 this
# module had no `__main__` -- `run_worker` was reachable only from a test.
#
# A separate deployment rather than a thread inside the API, and that is the
# bulkhead argument again: orchestration is the CPU-bound half (correlation over
# the service graph, intent classification), ingest is the latency-bound half
# with a 250 ms p99 to hold. Sharing a process means a storm's correlation work
# competes with the webhook that is still trying to return 202.


async def _run(cfg: Settings | None = None) -> None:
    cfg = cfg or default_settings
    configure_event_loop()
    configure_logging(level=cfg.log_level, json_output=cfg.log_json)

    from redis.asyncio import Redis

    from incidentpilot.config.graph_loader import load_service_graph
    from incidentpilot.db.engine import build_engine, build_session_factory
    from incidentpilot.orchestration.orchestrator import Orchestrator

    engine = build_engine(cfg)
    sessions = build_session_factory(engine)
    valkey = Redis.from_url(cfg.valkey_url.get_secret_value(), decode_responses=True)

    # `Settings` satisfies the CorrelationConfig Protocol, so the worker and the
    # API read one definition of merge_threshold rather than two copies that
    # drift.
    orchestrator = Orchestrator(load_service_graph(), cfg)

    stopping = asyncio.Event()

    def _stop(*_: object) -> None:
        log.info("worker.shutdown_requested")
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, AttributeError, OSError, NotImplementedError):
            signal.signal(sig, _stop)

    log.info("worker.started", stream=cfg.stream_alerts_raw)
    try:
        await run_worker(
            valkey,
            sessions,
            orchestrator,
            stream=cfg.stream_alerts_raw,
            consumer_name=os.environ.get("IP_CONSUMER_NAME", "worker-1"),
            stop_check=stopping.is_set,
        )
    finally:
        await engine.dispose()
        with contextlib.suppress(Exception):
            await valkey.aclose()
        log.info("worker.stopped")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
