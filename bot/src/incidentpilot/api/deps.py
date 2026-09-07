"""Dependency wiring.

Resources live on ``app.state``, created once in the lifespan and handed to
handlers here. Nothing is a module-level singleton: the tests build an app with
different settings without monkeypatching, and that is the whole reason the
adapters are fakeable and the replay harness works.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, Request

from incidentpilot.config.settings import Settings
from incidentpilot.orchestration.streams import StreamClient, StreamProducer
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


SettingsDep = Annotated[Settings, Depends(get_settings)]
ValkeyDep = Annotated[StreamClient, Depends(get_valkey)]
StreamsDep = Annotated[StreamProducer, Depends(get_streams)]
IngestorDep = Annotated[TranscriptIngestor, Depends(get_ingestor)]
