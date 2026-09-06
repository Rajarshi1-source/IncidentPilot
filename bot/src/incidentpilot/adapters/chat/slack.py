"""Slack implementation of ``ChatAdapter``.

Wraps every call in the rate limiter (per method *and* channel), a per-
dependency circuit breaker, and retry-only-if-idempotent. Slack errors are
classified into retryable and permanent, because retrying a 4xx spends the
rate-limit budget on a call that cannot succeed.

The name-collision path deserves the attention it gets: Slack rejects a
duplicate channel name outright, and an incident that cannot get a channel is an
incident with no war room.
"""

from __future__ import annotations

import time
from typing import Any

from incidentpilot.adapters.chat.base import (
    Channel,
    ChatError,
    PermanentChatError,
    PostedMessage,
    RetryableChatError,
    channel_name,
)
from incidentpilot.adapters.chat.ratelimit import SlackRateLimiter, priority_for
from incidentpilot.resilience.breaker import BreakerRegistry
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

DEPENDENCY = "slack"

# Slack error codes that will fail identically on every retry.
PERMANENT_ERRORS = frozenset(
    {
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "missing_scope",
        "not_allowed_token_type",
        "channel_not_found",
        "invalid_name",
        "invalid_name_specials",
        "invalid_name_maxlength",
        "restricted_action",
    }
)

# Names already taken. Not an error -- a signal to try the next suffix.
NAME_TAKEN = frozenset({"name_taken", "name_taken_i18n"})


def classify(error_code: str) -> ChatError:
    if error_code in PERMANENT_ERRORS:
        return PermanentChatError(error_code)
    return RetryableChatError(error_code)


class SlackChat:
    """Thin, deliberately: complexity here is complexity on the critical path."""

    def __init__(
        self,
        client: Any,
        limiter: SlackRateLimiter,
        breakers: BreakerRegistry,
        *,
        max_name_attempts: int = 5,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._breakers = breakers
        self._max_name_attempts = max_name_attempts

    async def _guarded(
        self,
        method: str,
        channel: str | None,
        call: Any,
        *,
        purpose: str | None = None,
    ) -> Any:
        """Rate limit, then breaker, then the call.

        Order matters: checking the limiter first means a shed call never counts
        toward opening the breaker. Slack being busy is not Slack being broken,
        and conflating them would trip the circuit during exactly the storm the
        limiter exists to survive.
        """
        allowed = await self._limiter.acquire(
            method, channel, purpose=purpose, now=time.monotonic()
        )
        if not allowed:
            if priority_for(method, purpose=purpose) >= 2:
                return None  # shed: decorative work, dropped and counted
            raise RetryableChatError(f"{method} rate limited")

        breaker = self._breakers.for_dependency(DEPENDENCY)

        async def _invoke() -> Any:
            response = await call()
            if not response.get("ok", False):
                raise classify(str(response.get("error", "unknown_error")))
            return response

        # pybreaker ships no type information for call_async; the wrapper it
        # calls is ours and fully typed, so the ignore is scoped to the seam.
        return await breaker.call_async(_invoke)  # type: ignore[no-untyped-call]

    async def create_channel(self, name: str, *, idempotency_key: str) -> Channel:
        """Create, walking a numeric suffix past a taken name.

        Slack has no idempotency-key concept of its own, so ours is enforced by
        the outbox: the same key produces the same row, and the row's dispatch
        record is what stops a second create. On retry the handler finds
        ``chat_channel_id`` already set and never reaches this method.
        """
        base = name
        for suffix in range(self._max_name_attempts):
            candidate = base if suffix == 0 else f"{base[:76]}-{suffix}"
            try:
                response = await self._guarded(
                    "conversations.create",
                    None,
                    lambda c=candidate: self._client.conversations_create(name=c),
                )
            except ChatError as exc:
                if str(exc) in NAME_TAKEN:
                    log.info("channel.name_taken", name=candidate)
                    continue
                raise
            channel = response["channel"]
            return Channel(id=str(channel["id"]), name=str(channel["name"]))

        raise PermanentChatError(f"could not find a free channel name near {base!r}")

    async def invite(self, channel_id: str, user_ids: list[str]) -> None:
        if not user_ids:
            return
        try:
            await self._guarded(
                "conversations.invite",
                channel_id,
                lambda: self._client.conversations_invite(
                    channel=channel_id, users=",".join(user_ids)
                ),
            )
        except ChatError as exc:
            # Everyone is already in the channel: the desired state, reached by
            # a previous attempt. That is a success, not a failure.
            if str(exc) in {"already_in_channel", "cant_invite_self"}:
                return
            raise

    async def post_message(
        self,
        channel_id: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        priority: int = 1,
    ) -> PostedMessage | None:
        purpose = {0: "runbook", 1: "pir"}.get(priority)
        response = await self._guarded(
            "chat.postMessage",
            channel_id,
            lambda: self._client.chat_postMessage(channel=channel_id, text=text, blocks=blocks),
            purpose=purpose,
        )
        if response is None:
            return None  # shed
        return PostedMessage(channel_id=channel_id, ts=str(response["ts"]))

    async def update_message(
        self,
        channel_id: str,
        ts: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        priority: int = 2,
    ) -> None:
        await self._guarded(
            "chat.update",
            channel_id,
            lambda: self._client.chat_update(channel=channel_id, ts=ts, text=text, blocks=blocks),
            purpose="timer",
        )

    async def pin(self, channel_id: str, ts: str) -> None:
        try:
            await self._guarded(
                "pins.add",
                channel_id,
                lambda: self._client.pins_add(channel=channel_id, timestamp=ts),
            )
        except ChatError as exc:
            if str(exc) == "already_pinned":
                return
            raise

    async def archive_channel(self, channel_id: str) -> None:
        try:
            await self._guarded(
                "conversations.archive",
                channel_id,
                lambda: self._client.conversations_archive(channel=channel_id),
            )
        except ChatError as exc:
            if str(exc) in {"already_archived", "channel_not_found"}:
                return
            raise

    async def permalink(self, channel_id: str, ts: str) -> str | None:
        response = await self._guarded(
            "chat.getPermalink",
            channel_id,
            lambda: self._client.chat_getPermalink(channel=channel_id, message_ts=ts),
        )
        return str(response["permalink"]) if response else None


__all__ = ["DEPENDENCY", "NAME_TAKEN", "PERMANENT_ERRORS", "SlackChat", "channel_name", "classify"]
