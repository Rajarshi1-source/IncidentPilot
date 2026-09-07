"""The one read path into Slack, kept deliberately awkward to reach (INV-02).

``ChatAdapter`` has no ``fetch_history`` and never will. This is a *separate*
Protocol, in a separate module, with exactly one consumer --
``orchestration/reconciler.py`` -- because the cheapest way to guarantee nobody
calls history on the hot path is to make sure the hot path never holds an object
that can.

The budget this interface is shaped around, restated because every method here
is a consequence of it: since **3 March 2026**, a non-Marketplace Slack app gets
``conversations.history`` and ``conversations.replies`` at **1 request per
minute, 15 messages per request**, and ``conversations.replies`` on a public
channel needs a *user* token rather than a bot one.

Note what that forbids. "Count the messages in this channel" is not an operation
this interface offers, because it cannot be performed: a 200-message channel is
fourteen pages, which is fourteen minutes. What it offers instead is
``recent_message_ts`` -- the newest page, once -- and transcript completeness is
computed as a **sample**: of the last N messages Slack reports, how many do we
hold? That is an estimator, and calling it one is more honest than a "count"
that silently reads a fifteenth of the channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Slack's per-request ceiling for a non-Marketplace app. Asking for more is not
# an error -- it just returns 15, which is the kind of silent truncation this
# whole design exists to avoid depending on.
HISTORY_PAGE_LIMIT = 15

# Channels we own are named `inc-YYYY-MM-DD-...` (see adapters/chat/base.py).
INCIDENT_CHANNEL_PREFIX = "inc-"


@dataclass(frozen=True, slots=True)
class ChannelSummary:
    id: str
    name: str
    is_archived: bool = False


@runtime_checkable
class HistoryReader(Protocol):
    """Reads. Never writes. The reconciler's whole view of Slack."""

    async def recent_message_ts(self, channel_id: str, *, limit: int = ...) -> list[str]:
        """The newest page of message timestamps. Costs one unit of the budget."""
        ...

    async def list_incident_channels(self, *, prefix: str = ...) -> list[ChannelSummary]:
        """Channels whose name looks like ours. ``conversations.list``, not history."""
        ...


class FakeHistory:
    """In-memory history reader for tests and for ``docker compose up``.

    Records every call so ``test_reconciler_respects_global_budget`` can assert
    on the count rather than on a log line.
    """

    def __init__(
        self,
        *,
        channel_ts: dict[str, list[str]] | None = None,
        channels: list[ChannelSummary] | None = None,
    ) -> None:
        self.channel_ts: dict[str, list[str]] = channel_ts or {}
        self.channels: list[ChannelSummary] = channels or []
        self.calls: list[tuple[str, str]] = []

    def call_count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    async def recent_message_ts(
        self, channel_id: str, *, limit: int = HISTORY_PAGE_LIMIT
    ) -> list[str]:
        self.calls.append(("conversations.history", channel_id))
        return list(self.channel_ts.get(channel_id, []))[-limit:]

    async def list_incident_channels(
        self, *, prefix: str = INCIDENT_CHANNEL_PREFIX
    ) -> list[ChannelSummary]:
        self.calls.append(("conversations.list", prefix))
        return [c for c in self.channels if c.name.startswith(prefix) and not c.is_archived]


class SlackHistory:
    """The real reader. Thin on purpose -- the budget lives in the reconciler.

    Deliberately *not* given a retry decorator. A 429 here means the one call
    per minute has already been spent, and retrying inside the same pass would
    spend the next minute's budget too; the reconciler simply comes back on its
    next tick, which is exactly what a 1/min budget wants.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def recent_message_ts(
        self, channel_id: str, *, limit: int = HISTORY_PAGE_LIMIT
    ) -> list[str]:
        response = await self._client.conversations_history(
            channel=channel_id, limit=min(limit, HISTORY_PAGE_LIMIT)
        )
        messages = response.get("messages") or []
        return [str(m["ts"]) for m in messages if m.get("ts")]

    async def list_incident_channels(
        self, *, prefix: str = INCIDENT_CHANNEL_PREFIX
    ) -> list[ChannelSummary]:
        """One page of public channels.

        ``exclude_archived`` is set at the API rather than filtered here: an
        archived channel is already in the state the orphan sweep would put it
        in, and paging through thousands of them to discard each one would burn
        a Tier 2 budget to learn nothing.
        """
        out: list[ChannelSummary] = []
        cursor: str | None = None
        while True:
            response = await self._client.conversations_list(
                exclude_archived=True, types="public_channel", limit=200, cursor=cursor
            )
            for channel in response.get("channels") or []:
                name = str(channel.get("name") or "")
                if name.startswith(prefix):
                    out.append(ChannelSummary(id=str(channel["id"]), name=name))
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return out
