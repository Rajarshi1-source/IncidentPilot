"""FastAPI application factory.

An explicit factory rather than a module-level ``app``: the tests build an app
with different settings and a fake Valkey without monkeypatching anything, which
is the same property that makes the replay harness possible.
"""

from __future__ import annotations

import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request, Response
from redis.asyncio import Redis

from incidentpilot import __version__
from incidentpilot.api import health
from incidentpilot.api.slash import commands as slash_commands
from incidentpilot.api.webhooks import alertmanager, paging, slack
from incidentpilot.config.assert_invariants import assert_invariants
from incidentpilot.config.graph_loader import load_service_graph
from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.db import repositories as repo
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.orchestration.orchestrator import Orchestrator
from incidentpilot.orchestration.streams import StreamClient, StreamProducer
from incidentpilot.runbooks.library import build_library
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger, reset_context
from incidentpilot.telemetry.metrics import (
    mark_transcript_ratio_unmeasured,
    set_degradation_level,
)
from incidentpilot.telemetry.tracing import configure_tracing, current_trace_id
from incidentpilot.transcript.classifier import IntentClassifier
from incidentpilot.transcript.ingestor import TranscriptIngestor

# Must run before anything opens an async connection (Windows/psycopg).
configure_event_loop()

log = get_logger(__name__)


def _install_shutdown_handler(app: FastAPI) -> None:
    """Flip a flag on SIGTERM so in-flight work can drain.

    Kubernetes sends SIGTERM and then waits ``terminationGracePeriodSeconds``.
    The worker uses this flag to stop claiming new stream entries while
    finishing what it holds; without it, an eviction mid-orchestration leaves
    entries claimed and stranded until XAUTOCLAIM sweeps them 60 s later.
    """

    def _handle(signum: int, _frame: Any) -> None:
        app.state.shutting_down = True
        log.info("shutdown.signal", signal=signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Signal handlers are process-global and unavailable off the main
        # thread; under pytest and on Windows this is a normal no-op.
        with suppress(ValueError, AttributeError, OSError, NotImplementedError):
            signal.signal(sig, _handle)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Settings = app.state.settings

    configure_logging(level=cfg.log_level, json_output=cfg.log_json)
    configure_tracing(
        enabled=cfg.otel_enabled,
        service_name=cfg.service_name,
        endpoint=cfg.otel_endpoint,
    )

    # Fail closed, and fail loudly, before serving a single request. A
    # production deployment with an unset webhook secret must not start.
    assert_invariants(cfg)

    if not hasattr(app.state, "valkey"):
        app.state.valkey = Redis.from_url(
            cfg.valkey_url.get_secret_value(),
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=30,
        )

    if not hasattr(app.state, "streams"):
        app.state.streams = StreamProducer(
            app.state.valkey,
            maxlen=cfg.stream_maxlen,
            raw_stream=cfg.stream_alerts_raw,
            resolved_stream=cfg.stream_alerts_resolved,
            wal_stream=cfg.stream_alerts_wal,
        )

    # The transcript write path. Slack now pushes messages at this process,
    # and every one of them is a database write -- so the API owns an engine
    # from week 4 onward, where before it only produced onto a stream.
    #
    # ``build_engine`` opens no connection, so a clean clone with no database
    # still starts; the first message event is what discovers a broken DSN, and
    # it discovers it as a 5xx that makes Slack redeliver rather than as a
    # startup crash loop.
    if not hasattr(app.state, "sessions"):
        app.state.sessions = build_session_factory(build_engine(cfg))

    # One channel->incident cache for the whole process. The ingestor and the
    # slash commands must resolve a war room the same way; two caches would
    # eventually disagree in a way nobody could reproduce.
    if not hasattr(app.state, "channels"):
        app.state.channels = repo.ChannelCache(
            app.state.valkey, app.state.sessions, ttl_s=cfg.channel_cache_ttl_s
        )

    if not hasattr(app.state, "ingestor"):
        app.state.ingestor = TranscriptIngestor(
            app.state.sessions,
            app.state.channels,
            classifier=IntentClassifier() if cfg.intent_layer2_enabled else None,
        )

    # The slash commands drive the same state machine the worker does, so the
    # API needs an orchestrator. One instance: the service graph and correlation
    # config are read-only and building them per request would re-read YAML on
    # the hot path. `Settings` satisfies the CorrelationConfig Protocol, so
    # there is no second copy of merge_threshold to keep in step.
    if not hasattr(app.state, "orchestrator"):
        app.state.orchestrator = Orchestrator(load_service_graph(), cfg)

    # Runbooks are files in git, loaded once and synced to the table so that
    # `incidents.runbook_id` has something to reference. A failure to load is
    # logged and survived here rather than fatal: the API's job is to accept
    # webhooks, and refusing to start over a missing markdown file would take
    # ingest down for a documentation problem.
    if not hasattr(app.state, "runbooks"):
        try:
            app.state.runbooks = await build_library(app.state.sessions, cfg.runbooks_dir)
        except Exception as exc:
            log.error("runbooks.load_failed", error=str(exc), directory=cfg.runbooks_dir)
            app.state.runbooks = None

    app.state.shutting_down = False
    _install_shutdown_handler(app)
    set_degradation_level(0)
    mark_transcript_ratio_unmeasured()

    log.info(
        "startup.complete",
        version=__version__,
        environment=cfg.environment,
        chat_provider=cfg.chat_provider,
    )

    try:
        yield
    finally:
        app.state.shutting_down = True
        if app.state.owns_valkey:
            with suppress(Exception):
                await app.state.valkey.aclose()
        log.info("shutdown.complete")


def create_app(
    cfg: Settings | None = None,
    *,
    valkey: StreamClient | None = None,
    streams: StreamProducer | None = None,
    sessions: Any = None,
    ingestor: Any = None,
    channels: Any = None,
    runbooks: Any = None,
    orchestrator: Any = None,
) -> FastAPI:
    """Build the application.

    ``valkey`` and ``streams`` are injectable so tests can supply a fake without
    reaching into module globals.
    """
    cfg = cfg or default_settings

    app = FastAPI(
        title="IncidentPilot",
        version=__version__,
        description="Self-hosted incident response with citation-anchored PIRs.",
        lifespan=lifespan,
    )

    app.state.settings = cfg
    app.state.shutting_down = False
    app.state.owns_valkey = valkey is None
    if valkey is not None:
        app.state.valkey = valkey
    if streams is not None:
        app.state.streams = streams
    if sessions is not None:
        app.state.sessions = sessions
    if ingestor is not None:
        app.state.ingestor = ingestor
    if channels is not None:
        app.state.channels = channels
    if runbooks is not None:
        app.state.runbooks = runbooks
    if orchestrator is not None:
        app.state.orchestrator = orchestrator

    @app.middleware("http")
    async def timing_and_context(request: Request, call_next: Any) -> Response:
        """Own the clock so handlers do not have to.

        The middleware stamps a monotonic start on ``request.state`` and the
        handlers read their latency through ``deps.elapsed_s(request)``. The
        plan's code contract writes ``request.state.elapsed`` directly, but a
        value assigned after ``call_next`` returns is always 0.0 by the time a
        handler observes its histogram -- the handler runs *inside* that call.
        One helper, one clock read, and the handlers still contain none.
        """
        reset_context()
        request.state.started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            request.state.elapsed = time.perf_counter() - request.state.started
        response.headers["X-Response-Time-Ms"] = f"{request.state.elapsed * 1000:.1f}"
        if trace_id := current_trace_id():
            response.headers["X-Trace-Id"] = trace_id
        return response

    app.include_router(health.router)
    app.include_router(alertmanager.router)
    app.include_router(paging.router)
    app.include_router(slack.router)
    app.include_router(slash_commands.router)

    return app


app = create_app()
