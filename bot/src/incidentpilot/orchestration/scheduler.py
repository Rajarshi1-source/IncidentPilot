"""Periodic jobs (W5-17).

**Not arq, and the reason is recorded rather than quietly worked around.**
ADR 0001 deferred ``arq==0.28.0`` because it caps ``redis[hiredis]<6`` and this
project pins ``redis==8.1.0``; taking arq would mean downgrading the client the
entire ingest path runs on to get a job runner for three timers. So the timers
are a plain asyncio loop.

That is a smaller thing than it sounds. What arq would add is a durable job
queue, and none of these jobs need one: each is a **sweep** that recomputes its
own work list from the database every time it runs. A missed tick costs a delay,
never a lost job — restart the process and the next pass finds exactly the same
incidents. A job runner earns its keep when work is enqueued and must not be
lost; here the database *is* the queue.

Three jobs:

* **SLA nudge** (5 min) — an engaged incident nobody has acknowledged gets a
  visible reminder. Not a page: the responder was already paged, and a second
  page for the same incident is how a pager stops being read.
* **Abandonment sweep** (hourly) — an incident with no activity for 24 hours is
  moved to ``abandoned``. B-13: a state machine with no escape hatch accumulates
  incidents that are neither open nor closed, and after a month the dashboard is
  a museum.
* **Reconciler** (15 min) — transcript completeness and orphan channels (W4).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import unit_of_work
from incidentpilot.domain.states import S
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger

log = get_logger(__name__)

# Engaged but unacknowledged for this long: nudge. Deliberately longer than the
# 30-second time-to-acknowledge SLO, because a nudge at the SLO boundary would
# fire on every incident and mean nothing.
NUDGE_AFTER_S = 600

# Engaged incidents nobody has acknowledged, with no nudge already queued. The
# idempotency key carries the nudge number, so a five-minute loop produces one
# nudge per interval rather than one per pass.
# Incidents that have resolved and have no PIR yet. `resolved` only: an incident
# already in `pir_drafting` was claimed by a previous pass, and the LEFT JOIN is
# belt-and-braces against a document written by a path other than this sweep.
RESOLVED_AWAITING_PIR = text(
    """
    SELECT i.id
      FROM incidents i
      LEFT JOIN pir_documents p ON p.incident_id = i.id
     WHERE i.state = 'resolved'
       AND p.id IS NULL
     ORDER BY i.resolved_at
     LIMIT :limit
    """
)

STALE_UNACKNOWLEDGED = text(
    """
    SELECT id, public_key, detected_at,
           EXTRACT(EPOCH FROM (now() - detected_at))::int AS age_s
      FROM incidents
     WHERE state IN ('engaged', 'triaging')
       AND acknowledged_at IS NULL
       AND detected_at < now() - make_interval(secs => :after_s)
     ORDER BY detected_at
     LIMIT :limit
    """
)

# No transition, no message, no alert for the window. All three matter: an
# incident with a live conversation in it is not abandoned no matter how old the
# state change is, and one still receiving alerts is very much not abandoned.
ABANDONED_CANDIDATES = text(
    """
    SELECT i.id, i.public_key
      FROM incidents i
     WHERE i.state NOT IN ('closed', 'merged', 'false_positive', 'abandoned')
       AND COALESCE(i.last_alert_at, i.detected_at) < now() - make_interval(hours => :hours)
       AND NOT EXISTS (
             SELECT 1 FROM slack_messages m
              WHERE m.incident_id = i.id
                AND m.received_at > now() - make_interval(hours => :hours))
       AND NOT EXISTS (
             SELECT 1 FROM incident_transitions t
              WHERE t.incident_id = i.id
                AND t.occurred_at > now() - make_interval(hours => :hours))
     ORDER BY i.detected_at
     LIMIT :limit
    """
)


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    interval_s: int
    run: Callable[[], Awaitable[int]]


def utcnow() -> datetime:
    """The scheduler's single clock read."""
    return datetime.now(UTC)


class Scheduler:
    """Runs a handful of sweeps on their own intervals.

    Each job returns how many things it acted on, which is what the loop logs.
    A sweep that silently does nothing forever looks identical to one that is
    not running at all, and this is the cheapest way to tell them apart.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        cfg: Settings,
        reconciler: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._cfg = cfg
        self._reconciler = reconciler

    def jobs(self) -> list[Job]:
        jobs = [
            Job("sla_nudge", self._cfg.sla_nudge_interval_s, self.nudge_unacknowledged),
            Job("abandonment_sweep", 3600, self.sweep_abandoned),
            # 20s, not 300s: the SLO is "resolve to draft posted, p95 under 90
            # seconds", and a sweep interval is part of that budget. Anything
            # slower spends most of the allowance waiting for the poll.
            Job("pir_sweep", 20, self.sweep_pir),
        ]
        if self._reconciler is not None:
            jobs.append(Job("reconciler", self._cfg.reconcile_interval_s, self._reconcile))
        return jobs

    # -- jobs ------------------------------------------------------------

    async def sweep_pir(self, *, limit: int = 5) -> int:
        """Generate the PIR for every incident that has resolved (W8-14, D1).

        **This is the job that makes the flagship actually run.** Until week 8
        `PIRGenerator` was never instantiated anywhere under ``src/``: the
        generator, the validator, the citation grammar and the fallback ladder
        were all built and all gate-tested, and nothing in the running system
        ever called them. G6 passes because ``gate_g6.sh`` constructs the
        generator itself in an inline script -- it proved the component works,
        not that the product uses it.

        Third occurrence of that shape in this project, after `ensure_engaged`
        and the schema that existed as a side effect of another test module:
        every half correct, the composition missing.

        The state machine is what makes it safe to run on an interval.
        ``resolved -> pir_drafting`` is a real transition guarded by
        ``UNIQUE (incident_id, seq)``, so two schedulers racing the same
        incident cannot both claim it -- the loser's transition raises and its
        pass simply finds nothing to do. That is why this sweeps for `resolved`
        rather than keeping a queue of its own.

        A small limit per pass on purpose: PIR generation is the one job here
        that can take sixty seconds and spend money, and a scheduler that tried
        to drain a backlog in one pass would hold its other sweeps off for
        minutes.
        """
        drafted = 0
        async with unit_of_work(self._sessions) as session:
            rows = (await session.execute(RESOLVED_AWAITING_PIR, {"limit": limit})).mappings().all()

        for row in rows:
            incident_id = int(row["id"])
            try:
                # Claim it first, in its own transaction. If generation then
                # crashes the incident sits in `pir_drafting` and the
                # PIR_FAILED branch is what recovers it -- which is a state a
                # human can see, unlike an incident that silently never got one.
                async with unit_of_work(self._sessions) as session:
                    await self._orchestrator().transition(
                        session, incident_id, S.PIR_DRAFTING, actor="scheduler"
                    )
            except Exception as exc:
                log.info("scheduler.pir_claim_skipped", incident_id=incident_id, error=str(exc))
                continue

            try:
                result = await self._generate_pir(incident_id)
            except Exception as exc:
                log.error("scheduler.pir_failed", incident_id=incident_id, error=str(exc))
                async with unit_of_work(self._sessions) as session:
                    await self._orchestrator().transition(
                        session, incident_id, S.PIR_FAILED, actor="scheduler", reason=str(exc)[:200]
                    )
                continue

            async with unit_of_work(self._sessions) as session:
                await self._orchestrator().transition(
                    session,
                    incident_id,
                    S.PIR_DRAFTED,
                    actor="scheduler",
                    reason=f"{result.layer} coverage={result.coverage}",
                )
            drafted += 1
            log.info(
                "scheduler.pir_drafted",
                incident_id=incident_id,
                layer=result.layer,
                coverage=result.coverage,
            )
        return drafted

    async def _generate_pir(self, incident_id: int) -> Any:
        """Build the generator and persist exactly one document.

        Constructed per incident rather than held on the scheduler: the router
        reads `models.yaml` and the budget breaker holds a Valkey handle, and a
        long-lived generator would pin a configuration that ops expects to be
        able to change without a restart.
        """
        from incidentpilot.adapters.llm.router import LLMRouter, build_providers
        from incidentpilot.adapters.metrics.factory import build_metrics
        from incidentpilot.config.models_config import load_models_config
        from incidentpilot.pir.generator import PIRGenerator, persist
        from incidentpilot.privacy.redactor import Redactor

        config = load_models_config()
        # A fresh redactor per incident: sharing one would leak the *fact*
        # that the same address appears in two incidents (D6).
        redactor = Redactor(enabled=self._cfg.redact_pii)
        router = LLMRouter(config, build_providers(config), redactor=redactor)
        generator = PIRGenerator(router, metrics=build_metrics(self._cfg))

        async with unit_of_work(self._sessions) as session:
            result = await generator.generate(session, incident_id)
            await persist(session, result)
        return result

    async def nudge_unacknowledged(self, *, limit: int = 50) -> int:
        """Remind the channel, do not page again.

        The responder has already been woken up; a second page for the same
        incident trains people to ignore the pager, which is the one outcome
        this whole product is trying to avoid.
        """
        nudged = 0
        async with unit_of_work(self._sessions) as session:
            rows = (
                await session.execute(
                    STALE_UNACKNOWLEDGED, {"after_s": NUDGE_AFTER_S, "limit": limit}
                )
            ).mappings()
            for row in rows:
                age_s = int(row["age_s"])
                # One nudge per interval, keyed on the bucket the age falls in.
                bucket = age_s // max(self._cfg.sla_nudge_interval_s, 60)
                if await repo.enqueue_outbox(
                    session,
                    int(row["id"]),
                    "post_sla_nudge",
                    payload={"age_s": age_s, "public_key": str(row["public_key"])},
                    discriminator=f"nudge:{bucket}",
                ):
                    nudged += 1
        if nudged:
            log.info("scheduler.nudged", count=nudged)
        return nudged

    async def sweep_abandoned(self, *, limit: int = 50) -> int:
        """The escape hatch B-13 asks for.

        Without it, an incident that nobody ever resolved sits in ``engaged``
        forever: it is not open in any useful sense and it is not closed, so it
        distorts every MTTR number and every open-incident count. Moving it to a
        terminal state is not a claim that it was handled -- ``abandoned`` says
        exactly what happened.
        """
        swept = 0
        hours = self._cfg.abandonment_after_h
        async with unit_of_work(self._sessions) as session:
            rows = (
                (await session.execute(ABANDONED_CANDIDATES, {"hours": hours, "limit": limit}))
                .mappings()
                .all()
            )

        for row in rows:
            try:
                async with unit_of_work(self._sessions) as session:
                    incident = await repo.get_for_update(session, int(row["id"]))
                    if incident is None:
                        continue
                    await self._transition_to_abandoned(session, incident, hours)
                swept += 1
            except Exception as exc:  # one stuck incident must not stop the sweep
                log.warning("scheduler.abandon_failed", incident_id=row["id"], error=str(exc))
        if swept:
            log.warning("scheduler.abandoned", count=swept, after_h=hours)
        return swept

    async def _transition_to_abandoned(
        self, session: AsyncSession, incident: Any, hours: int
    ) -> None:
        """Only from the states that permit it.

        ``TRANSITIONS`` allows ``abandoned`` from ``engaged`` and
        ``acknowledged`` only. An incident stuck somewhere else -- mid-PIR, say --
        is a different problem and forcing it here would paper over it.
        """
        from incidentpilot.orchestration.orchestrator import Orchestrator

        state = S(incident.state)
        if state not in {S.ENGAGED, S.ACKNOWLEDGED}:
            log.info("scheduler.abandon_skipped", incident_id=incident.id, state=str(state))
            return

        orchestrator: Orchestrator = self._orchestrator()
        await orchestrator.transition(
            session,
            int(incident.id),
            S.ABANDONED,
            actor="scheduler",
            reason=f"no activity for {hours}h",
        )

    def _orchestrator(self) -> Any:
        from incidentpilot.config.graph_loader import load_service_graph
        from incidentpilot.orchestration.orchestrator import Orchestrator

        if not hasattr(self, "_orch"):
            self._orch = Orchestrator(load_service_graph(), self._cfg)
        return self._orch

    async def _reconcile(self) -> int:
        if self._reconciler is None:  # pragma: no cover - the job is not registered
            return 0
        await self._reconciler.run_once()
        return 1

    # -- the loop --------------------------------------------------------

    async def run(self, *, stop: asyncio.Event, max_passes: int | None = None) -> None:
        """One task per job, each on its own interval.

        Separate tasks rather than one loop with modulo arithmetic: a slow
        reconcile pass must not delay the abandonment sweep, and a job that
        raises must not take the others down with it.
        """
        async with asyncio.TaskGroup() as group:
            for job in self.jobs():
                group.create_task(self._run_job(job, stop, max_passes))

    async def _run_job(self, job: Job, stop: asyncio.Event, max_passes: int | None) -> None:
        passes = 0
        while not stop.is_set():
            try:
                acted = await job.run()
                log.debug("scheduler.pass", job=job.name, acted=acted)
            except Exception as exc:
                # A job that dies takes its sweep with it forever. Log and keep
                # the interval: the next pass recomputes the work list anyway.
                log.error("scheduler.job_failed", job=job.name, error=str(exc))

            passes += 1
            if max_passes is not None and passes >= max_passes:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=job.interval_s)


async def run(cfg: Settings | None = None, *, max_passes: int | None = None) -> None:
    """``python -m incidentpilot.orchestration.scheduler``."""
    cfg = cfg or default_settings
    configure_event_loop()
    configure_logging(level=cfg.log_level, json_output=cfg.log_json)

    from redis.asyncio import Redis

    from incidentpilot.adapters.chat.factory import build_chat, build_history
    from incidentpilot.db.engine import build_engine, build_session_factory
    from incidentpilot.orchestration.handlers import ChatHandlers
    from incidentpilot.orchestration.reconciler import HistoryBudget, Reconciler

    engine = build_engine(cfg)
    sessions = build_session_factory(engine)
    valkey = Redis.from_url(cfg.valkey_url.get_secret_value(), decode_responses=True)

    reconciler = Reconciler(
        sessions,
        build_history(cfg),
        HistoryBudget(valkey, period_s=cfg.history_budget_period_s),
        valkey=valkey,
        handlers=ChatHandlers(build_chat(cfg, valkey=valkey), sessions),
    )
    scheduler = Scheduler(sessions, cfg=cfg, reconciler=reconciler)

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, AttributeError, OSError, NotImplementedError):
            signal.signal(sig, lambda *_: stop.set())

    log.info("scheduler.started", jobs=[j.name for j in scheduler.jobs()])
    try:
        await scheduler.run(stop=stop, max_passes=max_passes)
    finally:
        await engine.dispose()
        with contextlib.suppress(Exception):
            await valkey.aclose()
        log.info("scheduler.stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
