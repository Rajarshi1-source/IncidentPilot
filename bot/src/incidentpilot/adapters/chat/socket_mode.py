"""Socket Mode transport for development (W4-01).

Socket Mode and the HTTP receiver are two transports for one behaviour, and the
only thing this module is allowed to contain is the transport half. Every event
either receiver accepts is handed to the same ``TranscriptIngestor.on_event``,
because the alternative -- a dev path and a prod path that drift -- is how a bug
ships that nobody can reproduce locally.

Why bother with Socket Mode at all: it opens an outbound WebSocket, so a laptop
needs no public URL, no tunnel, and no re-registration of a Request URL every
time the tunnel restarts. That is genuinely hours saved in week 1 of Slack work.
Why not use it in production: there is no TLS termination you control, no
NetworkPolicy in front of it, and no signature to verify -- the trust boundary
becomes "we hold an app token", which is a strictly weaker statement than the
HTTP receiver's signed-request check (§16).

Consequently: Socket Mode is gated on ``IP_SLACK_SOCKET_MODE`` and refuses to
start outside a dev environment, rather than being merely discouraged in a
comment.
"""

from __future__ import annotations

from typing import Any

from incidentpilot.config.settings import Settings
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.transcript.ingestor import TranscriptIngestor

log = get_logger(__name__)


class SocketModeRunner:
    """Wires Slack's Socket Mode client to the ingestor.

    The Bolt import is deferred into ``start`` so that importing this module --
    which the invariant tests do, along with everything else under ``src`` --
    never requires ``slack_bolt`` to be installed. A deployment running the fake
    adapter has no reason to carry it.
    """

    def __init__(self, cfg: Settings, ingestor: TranscriptIngestor) -> None:
        if not cfg.is_dev:
            raise RuntimeError(
                "Socket Mode is a development transport: it has no signature to "
                "verify and no network boundary in front of it. Production uses "
                "the HTTP receiver at /webhooks/slack/events."
            )
        if not cfg.slack_app_token or not cfg.slack_bot_token:
            raise RuntimeError("Socket Mode needs both IP_SLACK_APP_TOKEN and IP_SLACK_BOT_TOKEN")
        self._cfg = cfg
        self._ingestor = ingestor

    async def start(self) -> None:  # pragma: no cover - needs a live workspace
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.app.async_app import AsyncApp

        app = AsyncApp(token=self._cfg.slack_bot_token.get_secret_value())  # type: ignore[union-attr]

        @app.event("message")
        async def _on_message(event: dict[str, Any]) -> None:
            await self._ingestor.on_event(event)

        log.info("socket_mode.starting")
        handler = AsyncSocketModeHandler(app, self._cfg.slack_app_token.get_secret_value())  # type: ignore[union-attr]
        await handler.start_async()  # type: ignore[no-untyped-call]
