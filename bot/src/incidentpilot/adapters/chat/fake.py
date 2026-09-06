"""In-memory chat adapter.

This is not a test double bolted on afterwards -- it is what makes the whole
demo runnable with no Slack workspace, and what makes the crash matrix and the
week 7 replay harness possible at all. `docker compose up` on a clean clone runs
against this.

It also *enforces* an architectural rule. ``fetch_history`` raises rather than
returning empty, so a regression that reintroduces a history fetch fails loudly
in CI instead of silently in production.
"""

from __future__ import annotations

from typing import Any

from incidentpilot.adapters.chat.base import (
    Channel,
    ChatCall,
    PostedMessage,
    RetryableChatError,
)


class FakeChat:
    """Records every call; idempotent by key, exactly like the real relay path."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls: list[ChatCall] = []
        self.channels: dict[str, Channel] = {}  # idempotency_key -> Channel
        self.by_id: dict[str, Channel] = {}
        self.members: dict[str, set[str]] = {}
        self.messages: dict[str, list[PostedMessage]] = {}
        self.pinned: dict[str, set[str]] = {}
        self.archived: set[str] = set()
        self.dropped: list[ChatCall] = []
        # Injects a transient failure after N calls, so retry and the crash
        # matrix can be exercised without patching anything.
        self._fail_after = fail_after
        self._seq = 0

    # -- bookkeeping -----------------------------------------------------

    def _record(self, method: str, *args: Any, key: str | None = None, **meta: Any) -> None:
        self.calls.append(ChatCall(method=method, args=args, idempotency_key=key, meta=meta))
        if self._fail_after is not None and len(self.calls) > self._fail_after:
            raise RetryableChatError(f"injected failure after {self._fail_after} calls")

    def call_count(self, method: str) -> int:
        return sum(1 for c in self.calls if c.method == method)

    def calls_to(self, method: str) -> list[ChatCall]:
        return [c for c in self.calls if c.method == method]

    def reset_calls(self) -> None:
        """Clear the call log but keep the workspace state.

        This is what "the process restarted" looks like: Slack still holds the
        channel it created, and our record of having called it is gone. The
        crash matrix depends on that distinction.
        """
        self.calls.clear()
        self.dropped.clear()

    # -- ChatAdapter -----------------------------------------------------

    async def create_channel(self, name: str, *, idempotency_key: str) -> Channel:
        """Idempotent by key, which is the property the whole outbox rests on.

        The relay may crash after Slack has created the channel but before the
        dispatch is recorded (crash point 8). On retry the same key must return
        the same channel rather than making a second one -- that is what
        licenses retrying an external write at all.
        """
        self._record("conversations.create", name, key=idempotency_key)
        if idempotency_key in self.channels:
            existing = self.channels[idempotency_key]
            return Channel(id=existing.id, name=existing.name, already_existed=True)

        self._seq += 1
        channel = Channel(id=f"C{self._seq:06d}", name=name)
        self.channels[idempotency_key] = channel
        self.by_id[channel.id] = channel
        self.members[channel.id] = set()
        self.messages[channel.id] = []
        self.pinned[channel.id] = set()
        return channel

    async def invite(self, channel_id: str, user_ids: list[str]) -> None:
        """Set semantics: re-inviting someone already present is a no-op.

        Slack behaves this way too (``already_in_channel``), which is why crash
        point 10 -- dying halfway through a four-person invite -- is safe to
        replay wholesale.
        """
        self._record("conversations.invite", channel_id, tuple(user_ids))
        self.members.setdefault(channel_id, set()).update(user_ids)

    async def post_message(
        self,
        channel_id: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        priority: int = 0,
    ) -> PostedMessage | None:
        self._record("chat.postMessage", channel_id, key=None, priority=priority)
        self._seq += 1
        posted = PostedMessage(channel_id=channel_id, ts=f"{1757000000 + self._seq}.000100")
        self.messages.setdefault(channel_id, []).append(posted)
        return posted

    async def update_message(
        self,
        channel_id: str,
        ts: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        priority: int = 2,
    ) -> None:
        self._record("chat.update", channel_id, ts, priority=priority)

    async def pin(self, channel_id: str, ts: str) -> None:
        self._record("pins.add", channel_id, ts)
        self.pinned.setdefault(channel_id, set()).add(ts)

    async def archive_channel(self, channel_id: str) -> None:
        """Idempotent: archiving an archived channel is not an error.

        Compensation must be safe to run twice (crash point 12), or the cleanup
        path becomes its own source of failures.
        """
        self._record("conversations.archive", channel_id)
        self.archived.add(channel_id)

    async def permalink(self, channel_id: str, ts: str) -> str | None:
        self._record("chat.getPermalink", channel_id, ts)
        return f"https://fake.slack/archives/{channel_id}/p{ts.replace('.', '')}"

    # -- the rule this class enforces ------------------------------------

    async def fetch_history(self, *args: Any, **kwargs: Any) -> Any:
        """Never callable. This is INV-02 with teeth.

        Since 3 March 2026 Slack limits ``conversations.history`` and
        ``conversations.replies`` to 1 request/minute and 15 messages per
        request for non-Marketplace apps. A 150-message incident would take ten
        minutes to read, and the failure is silent -- no error, the app simply
        knows less than it used to.

        Raising here means a regression that reintroduces a history fetch fails
        loudly in CI rather than quietly in production. Returning an empty list
        would hide exactly the bug this guards.
        """
        raise AssertionError(
            "conversations.history must never be called on the hot path -- "
            "the transcript is event-sourced (B-01, INV-02)"
        )

    async def fetch_replies(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "conversations.replies must never be called on the hot path (B-01, INV-02)"
        )
