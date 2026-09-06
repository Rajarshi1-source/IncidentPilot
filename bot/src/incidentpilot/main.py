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
from incidentpilot.api.webhooks import alertmanager, paging
from incidentpilot.config.assert_invariants import assert_invariants
from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.orchestration.streams import StreamClient, StreamProducer
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger, reset_context
from incidentpilot.telemetry.metrics import set_degradation_level
from incidentpilot.telemetry.tracing import configure_tracing, current_trace_id

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

    app.state.shutting_down = False
    _install_shutdown_handler(app)
    set_degradation_level(0)

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

    return app


app = create_app()
