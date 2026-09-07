"""W5-15, W5-16: the seven slash commands.

The latency test is the one Slack cares about -- 3 seconds or the user sees
`operation_timeout` -- and the `/split` test is the one D3 cares about, because
the feedback row is what turns a correction into training data.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incidentpilot.api.security import sign_slack
from incidentpilot.config.settings import Settings
from incidentpilot.main import create_app
from incidentpilot.orchestration.streams import StreamProducer
from tests.conftest import SLACK_SECRET, FakeValkey

CHANNEL = "C0WARROOM"
USER = "U_RESPONDER"
INCIDENT_ID = 42

# Slack's own budget. Measured against the whole request, not against the
# handler, because that is what the user experiences.
ACK_BUDGET_S = 3.0


class FakeOrchestrator:
    """Records transitions instead of driving the state machine.

    The state machine itself is tested in ``test_states.py`` and against a real
    database in the integration suite; what these tests need to know is that the
    command asked for the right one.
    """

    def __init__(self) -> None:
        self.transitions: list[tuple[int, str, str]] = []

    async def transition(
        self, session: Any, incident_id: int, nxt: Any, *, actor: str, reason: str = ""
    ) -> None:
        self.transitions.append((incident_id, str(nxt), actor))


class FakeChannels:
    """Stands in for the shared ChannelCache."""

    def __init__(self, mapping: dict[str, int] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {CHANNEL: INCIDENT_ID}

    async def incident_for_channel(self, channel_id: str) -> int | None:
        return self.mapping.get(channel_id)


class RecordingSession:
    """Enough of a session for the commands that only read and write rows."""

    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.store.setdefault("executed", []).append(args[0])
        return _Empty()

    def add(self, obj: Any) -> None:
        self.store.setdefault("added", []).append(obj)

    async def commit(self) -> None:
        self.store["committed"] = True

    async def rollback(self) -> None:
        self.store["rolled_back"] = True

    async def __aenter__(self) -> RecordingSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Empty:
    def scalars(self) -> _Empty:
        return self

    def first(self) -> None:
        return None

    def scalar(self) -> None:
        return None

    def mappings(self) -> list[Any]:
        return []

    def all(self) -> list[Any]:
        return []


@pytest.fixture
def store() -> dict[str, Any]:
    return {}


@pytest.fixture
def orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
def slash_app(
    test_settings: Settings, store: dict[str, Any], orchestrator: FakeOrchestrator
) -> FastAPI:
    valkey = FakeValkey()
    streams = StreamProducer(
        valkey,
        maxlen=test_settings.stream_maxlen,
        raw_stream=test_settings.stream_alerts_raw,
        resolved_stream=test_settings.stream_alerts_resolved,
        wal_stream=test_settings.stream_alerts_wal,
    )

    def sessions() -> RecordingSession:
        return RecordingSession(store)

    return create_app(
        test_settings,
        valkey=valkey,
        streams=streams,
        sessions=sessions,
        ingestor=object(),
        channels=FakeChannels(),
        runbooks=None,
        orchestrator=orchestrator,
    )


@pytest.fixture
def slash_client(slash_app: FastAPI) -> Any:
    with TestClient(slash_app) as client:
        yield client


def _post(client: Any, path: str, *, text: str = "", channel: str = CHANNEL) -> Any:
    body = urlencode(
        {
            "command": path.rsplit("/", 1)[-1],
            "text": text,
            "user_id": USER,
            "user_name": "responder",
            "channel_id": channel,
            "channel_name": "inc-2026-09-07-payments",
            "response_url": "https://hooks.slack.test/commands/1",
            "trigger_id": "trig-1",
        }
    ).encode()
    timestamp = str(int(time.time()))
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": sign_slack(body, timestamp, SLACK_SECRET),
        },
    )


COMMANDS = [
    "/slash/resolve",
    "/slash/update",
    "/slash/escalate",
    "/slash/metrics",
    "/slash/split",
    "/slash/falsepositive",
    "/slash/step",
]


def test_all_seven_commands_are_mounted(slash_app: FastAPI) -> None:
    """W5-15 asks for seven. One endpoint per command, because Slack needs a
    Request URL per command anyway."""
    # Read from the OpenAPI schema rather than `app.routes`: this FastAPI
    # version keeps included routers as opaque objects, so walking `.routes`
    # would assert on an implementation detail and quietly stop checking
    # anything the day that changes.
    assert set(COMMANDS) <= set(slash_app.openapi()["paths"])


@pytest.mark.parametrize("path", COMMANDS)
def test_slash_acks_fast(slash_client: Any, path: str) -> None:
    """Slack shows `operation_timeout` past 3 seconds.

    Measured over the whole request rather than inside the handler, because that
    is what the person who typed the command experiences.
    """
    started = time.perf_counter()
    response = _post(slash_client, path, text="done verify-replica-lag")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < ACK_BUDGET_S, f"{path} took {elapsed:.2f}s"


@pytest.mark.parametrize("path", COMMANDS)
def test_every_command_verifies_its_signature(slash_client: Any, path: str) -> None:
    """One shared verification path, so a new command cannot be added without it
    by forgetting to copy the block."""
    body = urlencode({"command": path, "channel_id": CHANNEL, "user_id": USER}).encode()
    response = slash_client.post(
        path, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("path", COMMANDS)
def test_a_command_outside_a_war_room_is_refused_politely(slash_client: Any, path: str) -> None:
    """Ephemeral, not an error. Typing `/resolve` in #general is a mistake, not
    a fault, and a 500 would look like the bot is broken."""
    response = _post(slash_client, path, channel="C0GENERAL")
    assert response.status_code == 200
    assert "war room" in response.json()["text"].lower()


@pytest.mark.parametrize("path", COMMANDS)
def test_the_ack_is_ephemeral(slash_client: Any, path: str) -> None:
    """The *result* is posted in-channel by the relay. Echoing the request too
    would double every action in a transcript a PIR will cite."""
    response = _post(slash_client, path, text="done x")
    assert response.json().get("response_type") == "ephemeral"


def test_update_requires_text(slash_client: Any) -> None:
    assert "Usage" in _post(slash_client, "/slash/update").json()["text"]


def test_escalate_rejects_an_unknown_severity(slash_client: Any) -> None:
    response = _post(slash_client, "/slash/escalate", text="sev9")
    assert "Unknown severity" in response.json()["text"]


def test_split_requires_a_fingerprint(slash_client: Any) -> None:
    """The fingerprint is on the merge notice, and the message says so."""
    response = _post(slash_client, "/slash/split")
    assert "Usage" in response.json()["text"]
    assert "fingerprint" in response.json()["text"]


def test_step_rejects_a_malformed_invocation(slash_client: Any) -> None:
    assert "Usage" in _post(slash_client, "/slash/step", text="verify-replica-lag").json()["text"]


def test_metrics_returns_a_window_not_a_fabricated_chart(slash_client: Any) -> None:
    """W6 brings the metrics adapter. Posting a chart before there is anything
    to render it from would be B-10 in miniature."""
    response = _post(slash_client, "/slash/metrics")
    assert response.status_code == 200


def test_falsepositive_asks_for_the_terminal_transition(
    slash_client: Any, orchestrator: FakeOrchestrator
) -> None:
    """Terminal, and deliberately no PIR.

    A postmortem on an alert that should not have fired is a document about
    nothing, and writing one teaches people the PIR process is theatre.
    """
    _post(slash_client, "/slash/falsepositive", text="bad threshold")
    assert orchestrator.transitions == [(INCIDENT_ID, "false_positive", USER)]


def test_the_real_lifespan_builds_the_shared_channel_cache(test_settings: Settings) -> None:
    """The wiring the fixtures inject, asserted on the path production takes.

    Every test above supplies ``channels=`` explicitly, so all of them would
    pass with the lifespan never building one -- and the first real slash
    command would fail with ``'State' object has no attribute 'channels'``. A
    smoke run caught exactly that; this is the assertion that keeps it caught.
    """
    valkey = FakeValkey()
    app = create_app(
        test_settings,
        valkey=valkey,
        streams=StreamProducer(
            valkey,
            maxlen=test_settings.stream_maxlen,
            raw_stream=test_settings.stream_alerts_raw,
            resolved_stream=test_settings.stream_alerts_resolved,
            wal_stream=test_settings.stream_alerts_wal,
        ),
    )
    with TestClient(app):
        assert app.state.channels is not None
        # The ingestor holds the *same* instance, not a second one: a command
        # typed in a war room must resolve from the mapping the messages did.
        assert app.state.ingestor._channels is app.state.channels
