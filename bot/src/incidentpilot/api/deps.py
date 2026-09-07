"""Dependency wiring.

Resources live on ``app.state``, created once in the lifespan and handed to
handlers here. Nothing is a module-level singleton: the tests build an app with
different settings without monkeypatching, and that is the whole reason the
adapters are fakeable and the replay harness works.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import Depends, Request

from incidentpilot.config.settings import Settings
from incidentpilot.db.repositories import ChannelCache
from incidentpilot.orchestration.streams import StreamClient, StreamProducer
from incidentpilot.resilience.degradation import DegradationManager
from incidentpilot.transcript.ingestor import TranscriptIngestor


def elapsed_s(request: Request) -> float:
    """Seconds since the middleware stamped the request.

    The single clock read on the request path. Handlers call this instead of
    reaching for ``time`` themselves, so latency measurement stays in one place
    and the handlers stay free of clock access.
    """
    started: float | None = getattr(request.state, "started", None)
    if started is None:
        return 0.0
    return time.perf_counter() - started


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_valkey(request: Request) -> StreamClient:
    client: StreamClient = request.app.state.valkey
    return client


def get_streams(request: Request) -> StreamProducer:
    producer: StreamProducer = request.app.state.streams
    return producer


def get_ingestor(request: Request) -> TranscriptIngestor:
    """The transcript ingestor built once in the lifespan.

    Injected rather than constructed per request so the session factory, the
    channel cache and the classifier's embedded exemplar index are all built
    once -- and so a test can substitute a recording double without patching a
    module global.
    """
    ingestor: TranscriptIngestor = request.app.state.ingestor
    return ingestor


def get_sessions(request: Request) -> Any:
    """The API's session factory, built once in the lifespan.

    Week 4 gave the API an engine (the transcript is a write path); week 5's
    slash commands need it too, and reaching for a module-level factory here
    would undo the property that makes the whole suite fixture-driven.
    """
    return request.app.state.sessions


def get_degradation(request: Request) -> DegradationManager:
    """The one degradation manager, built in the lifespan.

    One instance, because the level is a property of the process rather than of
    a request, and two managers with two opinions about the level would fight
    over a single Prometheus gauge.
    """
    manager: DegradationManager = request.app.state.degradation
    return manager


def get_channels(request: Request) -> ChannelCache:
    """The one channel->incident cache, shared with the ingestor."""
    cache: ChannelCache = request.app.state.channels
    return cache


SettingsDep = Annotated[Settings, Depends(get_settings)]
ValkeyDep = Annotated[StreamClient, Depends(get_valkey)]
StreamsDep = Annotated[StreamProducer, Depends(get_streams)]
IngestorDep = Annotated[TranscriptIngestor, Depends(get_ingestor)]
SessionsDep = Annotated[Any, Depends(get_sessions)]
ChannelsDep = Annotated[ChannelCache, Depends(get_channels)]
DegradationDep = Annotated[DegradationManager, Depends(get_degradation)]
