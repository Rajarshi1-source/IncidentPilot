"""The chat boundary.

A Protocol rather than a base class, so a fake is a structural substitute with
no inheritance and no registration. That seam is what lets the whole demo run in
``docker compose up`` with no Slack workspace, and it is what makes the week 7
replay harness possible at all.

Slack changed its API terms in 2025 and again in 2026. This interface is the
insurance: a Mattermost or Discord implementation is roughly 200 lines behind
it, and nothing above this line knows which one is installed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Slack: lowercase, no spaces, max 80 characters, unique per workspace.
MAX_CHANNEL_NAME = 80


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    name: str
    already_existed: bool = False


@dataclass(frozen=True, slots=True)
class PostedMessage:
    channel_id: str
    ts: str
    permalink: str | None = None


@dataclass(frozen=True, slots=True)
class ChatCall:
    """One recorded interaction. The fake keeps these; the crash matrix counts them."""

    method: str
    args: tuple[Any, ...] = ()
    idempotency_key: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ChatError(Exception):
    """Any failure from the chat provider."""


class RetryableChatError(ChatError):
    """Transient: a 429, a 5xx, a timeout. Safe to retry *if* idempotent."""


class PermanentChatError(ChatError):
    """A 4xx that will fail identically forever. Retrying wastes the budget."""


@runtime_checkable
class ChatAdapter(Protocol):
    """Everything the relay is allowed to do to the outside world.

    Deliberately small. Every method here is an external side effect, and the
    relay is the only component permitted to call any of them (INV-03) -- if two
    processes can create a channel, eventually two will.

    Note what is *absent*: there is no ``fetch_history``. Since 3 March 2026
    Slack limits ``conversations.history`` to 1 request/minute and 15 messages
    for non-Marketplace apps, so the transcript is event-sourced on arrival
    instead (B-01, week 4). The reconciler owns the one exception and reaches
    for it through a separate, rate-limited interface -- keeping it out of this
    Protocol means a hot-path caller cannot reach it by accident.
    """

    async def create_channel(self, name: str, *, idempotency_key: str) -> Channel: ...

    async def invite(self, channel_id: str, user_ids: list[str]) -> None: ...

    async def post_message(
        self,
        channel_id: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = ...,
        priority: int = ...,
    ) -> PostedMessage | None: ...

    async def update_message(
        self,
        channel_id: str,
        ts: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = ...,
        priority: int = ...,
    ) -> None: ...

    async def pin(self, channel_id: str, ts: str) -> None: ...

    async def archive_channel(self, channel_id: str) -> None: ...

    async def permalink(self, channel_id: str, ts: str) -> str | None: ...


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, ASCII, hyphen-separated. Empty in, empty out."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return _SLUG_STRIP.sub("-", normalized.lower()).strip("-")


def channel_name(
    *,
    date: str,
    service: str | None,
    summary: str,
    suffix: int = 0,
) -> str:
    """Build ``inc-2026-09-06-payments-postgres-primary-down``.

    Truncation takes it out of the **summary**, never the date or the service.
    Responders scan a channel list for "which incident, which service, when" --
    losing the date to fit a long alert name is the wrong trade, and a name
    truncated at the front is unrecognizable.

    ``suffix`` disambiguates a collision. Slack rejects a duplicate name outright,
    and an incident that cannot get a channel is an incident with no war room.
    """
    prefix = f"inc-{date}"
    if service:
        prefix = f"{prefix}-{slugify(service)}"
    if suffix:
        tail = f"-{suffix}"
        budget = MAX_CHANNEL_NAME - len(prefix) - len(tail) - 1
        body = slugify(summary)[:budget] if budget > 0 else ""
        return f"{prefix}-{body}{tail}".replace("--", "-").strip("-")[:MAX_CHANNEL_NAME]

    budget = MAX_CHANNEL_NAME - len(prefix) - 1
    body = slugify(summary)[:budget] if budget > 0 else ""
    name = f"{prefix}-{body}" if body else prefix
    return name.strip("-")[:MAX_CHANNEL_NAME]
