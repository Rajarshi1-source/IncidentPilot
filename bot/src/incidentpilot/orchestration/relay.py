"""Relay entrypoint: ``python -m incidentpilot.orchestration.relay``.

Its own process, not a thread in the API. Three reasons, all of them the plan's:

* The API must never be blocked by slow work -- its SLO is a p99 of 250 ms and
  a Slack call is two orders of magnitude slower than that.
* Exactly one component performs external writes (INV-03). A relay embedded in
  three API replicas is three components.
* It scales on a different signal: pending outbox depth, not request rate.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from incidentpilot.adapters.chat.factory import build_chat
from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.orchestration.handlers import build_handlers
from incidentpilot.orchestration.outbox import OutboxRelay
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger

log = get_logger(__name__)

POLL_IDLE_S = 1.0
STUCK_SWEEP_EVERY = 30  # idle passes between reclaim sweeps


async def run(cfg: Settings | None = None, *, max_passes: int | None = None) -> None:
    cfg = cfg or default_settings
    configure_event_loop()
    configure_logging(level=cfg.log_level, json_output=cfg.log_json)

    engine = build_engine(cfg)
    sessions = build_session_factory(engine)
    relay = OutboxRelay(build_handlers(build_chat(cfg), sessions))

    stopping = asyncio.Event()

    def _stop(*_: object) -> None:
        log.info("relay.shutdown_requested")
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, AttributeError, OSError, NotImplementedError):
            signal.signal(sig, _stop)

    log.info("relay.started", chat_provider=cfg.chat_provider)
    idle_passes = 0
    passes = 0

    try:
        while not stopping.is_set():
            async with sessions() as session, session.begin():
                dispatched = await relay.relay_once(session)

            passes += 1
            if dispatched == 0:
                idle_passes += 1
                # Sweep for rows a dead relay left 'claimed' (crash point 7).
                # Only when idle: a busy relay has better things to do, and a
                # stuck row is by definition not urgent.
                if idle_passes % STUCK_SWEEP_EVERY == 0:
                    async with sessions() as session, session.begin():
                        await relay.reclaim_stuck(session)
                await asyncio.sleep(POLL_IDLE_S)
            else:
                idle_passes = 0

            if max_passes is not None and passes >= max_passes:
                break
    finally:
        await engine.dispose()
        log.info("relay.stopped", passes=passes)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
