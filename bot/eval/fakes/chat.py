"""Replay chat adapter -- and the enforcement of INV-02 (W7-03, B-01).

``fetch_history`` raises ``AssertionError``. That is the whole reason this class
exists separately from ``adapters/chat/fake.py``: the harness enforces an
architectural rule, so a regression that reintroduces a history fetch fails
loudly in CI rather than silently in production.

The failure it guards is invisible by nature. Since 3 March 2026 Slack limits
``conversations.history`` to 1 request/minute and 15 messages for non-Marketplace
apps -- there is no exception, no 429 to catch, just a transcript that is quietly
shorter than it should be and a PIR that is quietly wrong. An assertion is the
only detector available for a failure with no error.
"""

from __future__ import annotations

from typing import Any

from eval.recorder import KIND_CHANNEL_CREATE, Recording
from incidentpilot.adapters.chat.base import Channel, ChatCall, PostedMessage


class ReplayChat:
    """Returns the recorded channel; records every call for assertion."""

    def __init__(self, recording: Recording) -> None:
        self._recording = recording
        self.calls: list[ChatCall] = []
        self.channels: dict[str, Channel] = {}
        self.posts: list[tuple[str, str]] = []
        self._seq = 0

    # -- bookkeeping -----------------------------------------------------

    def _record(self, method: str, *args: Any, key: str | None = None, **meta: Any) -> None:
        self.calls.append(ChatCall(method=method, args=args, idempotency_key=key, meta=meta))

    def call_count(self, method: str) -> int:
        return sum(1 for call in self.calls if call.method == method)

    @property
    def channel_count(self) -> int:
        """Distinct channels created. The storm-compression denominator."""
        return len(self.channels)

    # -- ChatAdapter -----------------------------------------------------

    async def create_channel(self, name: str, *, idempotency_key: str) -> Channel:
        self._record("conversations.create", name, key=idempotency_key)
        if idempotency_key in self.channels:
            existing = self.channels[idempotency_key]
            return Channel(id=existing.id, name=existing.name, already_existed=True)

        recorded = next(
            (e for e in self._recording.of_kind(KIND_CHANNEL_CREATE)),
            None,
        )
        # The recorded id when there is one, a synthetic one otherwise. A
        # storm fixture creates a channel the recording never saw precisely
        # when correlation has regressed, and the harness must be able to
        # count that rather than crash on it.
        self._seq += 1
        recorded_id = (recorded.response.get("channel") or {}).get("id") if recorded else None
        channel_id = str(recorded_id or "")
        if not channel_id or self._seq > 1:
            channel_id = f"C_REPLAY_{self._seq:04d}"
        channel = Channel(id=channel_id, name=name)
        self.channels[idempotency_key] = channel
        return channel

    async def invite(self, channel_id: str, user_ids: list[str]) -> None:
        self._record("conversations.invite", channel_id, tuple(user_ids))

    async def post_message(
        self,
        channel_id: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        priority: int = 0,
    ) -> PostedMessage | None:
        self._record("chat.postMessage", channel_id, priority=priority, text=text)
        self._seq += 1
        self.posts.append((channel_id, text))
        return PostedMessage(channel_id=channel_id, ts=f"{1757000000 + self._seq}.000100")

    async def update_message(
        self,
        channel_id: str,
        ts: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        priority: int = 2,
    ) -> None:
        self._record("chat.update", channel_id, ts, priority=priority, text=text)

    async def pin(self, channel_id: str, ts: str) -> None:
        self._record("pins.add", channel_id, ts)

    async def archive_channel(self, channel_id: str) -> None:
        self._record("conversations.archive", channel_id)

    async def permalink(self, channel_id: str, ts: str) -> str | None:
        self._record("chat.getPermalink", channel_id, ts)
        return f"https://replay.invalid/{channel_id}/p{ts.replace('.', '')}"

    # -- the rule this class enforces ------------------------------------

    async def fetch_history(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "history must never be called on the hot path -- the transcript is "
            "event-sourced (B-01, INV-02)"
        )

    async def fetch_replies(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "replies must never be called on the hot path -- the transcript is "
            "event-sourced (B-01, INV-02)"
        )
