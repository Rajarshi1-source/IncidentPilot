"""Shared fixtures.

The app is built with an injected fake Valkey rather than a patched module
global. That is the same seam the replay harness uses in week 7, so exercising
it from week 1 keeps it honest.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incidentpilot.config.settings import Settings
from incidentpilot.main import create_app
from incidentpilot.orchestration.streams import StreamProducer

FIXTURES = Path(__file__).parent / "fixtures"

ALERTMANAGER_BEARER = "test-alertmanager-bearer"
PAGING_SECRET = "test-paging-secret"
SLACK_SECRET = "test-slack-signing-secret"


class FakeValkey:
    """In-memory stand-in for the bits of Valkey the ingest path uses.

    Records entries per stream so tests can assert what was published without a
    container. The integration tests run the same assertions against a real
    Valkey; this one keeps the unit suite fast and offline.
    """

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._seq = 0
        self.alive = True

    async def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        if maxlen is not None and len(self.streams[name]) > maxlen:
            self.streams[name] = self.streams[name][-maxlen:]
        return entry_id

    async def ping(self) -> bool:
        if not self.alive:
            raise ConnectionError("fake valkey is down")
        return True

    async def aclose(self) -> None:
        self.alive = False

    def pipeline(self, transaction: bool = False) -> FakePipeline:
        return FakePipeline(self)

    def entries(self, stream: str) -> list[dict[str, str]]:
        return [fields for _, fields in self.streams.get(stream, [])]


class FakePipeline:
    def __init__(self, client: FakeValkey) -> None:
        self._client = client
        self._queued: list[tuple[str, dict[str, str], int | None]] = []

    def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> FakePipeline:
        self._queued.append((name, dict(fields), maxlen))
        return self

    async def execute(self) -> list[str]:
        results: list[str] = []
        for name, fields, maxlen in self._queued:
            results.append(await self._client.xadd(name, fields, maxlen=maxlen))
        self._queued.clear()
        return results


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """psycopg async cannot run on Windows' default ProactorEventLoop.

    It raises `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
    in async mode`, so every async database call fails on a Windows dev machine
    while passing in Linux CI -- the worst shape of bug to debug, because the
    suite is green everywhere it runs automatically.

    Production is Linux containers where the default is already a selector loop;
    this only affects local development. See `incidentpilot.runtime`.
    """
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        environment="test",
        alertmanager_bearer=ALERTMANAGER_BEARER,
        paging_webhook_secret=PAGING_SECRET,
        slack_signing_secret=SLACK_SECRET,
        log_json=True,
        otel_enabled=False,
    )


@pytest.fixture
def fake_valkey() -> FakeValkey:
    return FakeValkey()


@pytest.fixture
def app(test_settings: Settings, fake_valkey: FakeValkey) -> FastAPI:
    producer = StreamProducer(
        fake_valkey,
        maxlen=test_settings.stream_maxlen,
        raw_stream=test_settings.stream_alerts_raw,
        resolved_stream=test_settings.stream_alerts_resolved,
        wal_stream=test_settings.stream_alerts_wal,
    )
    return create_app(test_settings, valkey=fake_valkey, streams=producer)


@pytest.fixture
def client(app: FastAPI) -> Any:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def alertmanager_payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (FIXTURES / "alertmanager_firing.json").read_text(encoding="utf-8")
    )
    return payload
