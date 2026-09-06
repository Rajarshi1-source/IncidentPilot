"""Resolves the chat provider from configuration.

Construction lives here rather than in the relay entrypoint so INV-03's
import-graph check stays meaningful: exactly one module builds a concrete chat
adapter, and exactly one module calls its write methods. Widening the exemption
list every time a new entrypoint needs to wire one up would hollow out the
invariant until it caught nothing.

The provider is named in config, never imported by name at the call site --
which is also the reason `fake` can be the default and a clean clone runs the
whole demo with no Slack workspace and no credentials.
"""

from __future__ import annotations

from typing import Any

from incidentpilot.config.settings import Settings
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)


def build_chat(cfg: Settings, *, valkey: Any = None) -> Any:
    """Return the configured chat adapter.

    Imports are deferred inside each branch on purpose: a deployment running the
    fake adapter should not need `slack_sdk` installed, and the fake path is the
    one an interviewer exercises on a clean clone.
    """
    if cfg.chat_provider == "slack":
        from slack_sdk.web.async_client import AsyncWebClient

        from incidentpilot.adapters.chat.ratelimit import SlackRateLimiter
        from incidentpilot.adapters.chat.slack import SlackChat
        from incidentpilot.resilience.breaker import BreakerRegistry

        if valkey is None:
            from redis.asyncio import Redis

            valkey = Redis.from_url(cfg.valkey_url.get_secret_value(), decode_responses=True)

        token = cfg.slack_bot_token.get_secret_value() if cfg.slack_bot_token else ""
        log.info("chat.provider_resolved", provider="slack")
        return SlackChat(AsyncWebClient(token=token), SlackRateLimiter(valkey), BreakerRegistry())

    from incidentpilot.adapters.chat.fake import FakeChat

    log.info("chat.provider_resolved", provider="fake")
    return FakeChat()
