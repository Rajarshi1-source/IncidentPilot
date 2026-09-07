"""🚦 G4, first half: INV-02 — history is never called on the hot path (W4-13).

Wired into CI as its own step rather than folded into the unit run, because this
is the invariant the whole week exists to protect and a failure here should be
legible at a glance rather than buried in a summary line. It needs no database
and no network, so it runs on every pull request in seconds.

The bug being guarded (B-01) is a Rev 1 design that fetched the Slack thread at
resolve time. Since 3 March 2026 that costs one request per minute and returns
fifteen messages at a time, so a 150-message incident takes ten minutes to
read -- and, worse, the failure is silent. No 429 anyone notices, no exception:
the app simply knows less than it used to, and the first symptom is a PIR whose
citations point into a transcript with holes in it.

Three independent guards, because one is not enough:

1. **Structural.** The write adapter is not a history reader and cannot be used
   as one -- a hot-path caller holding a ``ChatAdapter`` has nothing to call.
2. **Behavioural.** ``FakeChat.fetch_history`` raises rather than returning an
   empty list, so a regression fails loudly in CI instead of quietly reporting
   an empty transcript in production.
3. **Lexical.** An AST scan over ``src/`` catches a call to any history method
   from any module outside the two that are allowed to have one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from incidentpilot.adapters.chat.base import ChatAdapter
from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.adapters.chat.history import FakeHistory, HistoryReader

SRC = Path(__file__).resolve().parents[2] / "src" / "incidentpilot"

# Every way of naming a history read, at the call site.
HISTORY_CALLS: frozenset[str] = frozenset(
    {
        "fetch_history",
        "fetch_replies",
        "conversations_history",
        "conversations_replies",
        "recent_message_ts",
    }
)

# The two modules that are allowed to contain one, and why:
#   history.py    -- defines the reader; the calls are its implementation
#   reconciler.py -- the single consumer, gated on a global 1/min budget
#   fake.py       -- defines the raising stubs that make guard 2 work
ALLOWED: frozenset[str] = frozenset(
    {
        "adapters/chat/history.py",
        "adapters/chat/fake.py",
        "orchestration/reconciler.py",
    }
)


def _modules() -> list[tuple[str, Path]]:
    return [
        (path.relative_to(SRC).as_posix(), path)
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "__init__.py"
    ]


# --- guard 1: structural ------------------------------------------------------


def test_the_write_adapter_is_not_a_history_reader() -> None:
    """A hot-path caller holds a ``ChatAdapter`` and therefore holds nothing
    that can read history. That is INV-02 enforced by the type, not by care."""
    chat = FakeChat()
    assert isinstance(chat, ChatAdapter)
    assert not isinstance(chat, HistoryReader)
    assert isinstance(FakeHistory(), HistoryReader)


def test_the_chat_protocol_declares_no_read_methods() -> None:
    """``fetch_history`` is absent from the Protocol on purpose.

    Adding it "just for the reconciler" would put a history call one attribute
    access away from every relay handler.
    """
    surface = set(ChatAdapter.__protocol_attrs__)  # type: ignore[attr-defined]
    assert not (surface & HISTORY_CALLS)


# --- guard 2: behavioural -----------------------------------------------------


async def test_fake_chat_raises_rather_than_returning_empty() -> None:
    """Returning ``[]`` would hide exactly the bug this guards.

    A silent empty transcript is indistinguishable, downstream, from an incident
    where nobody said anything -- which is precisely how B-01 would come back.
    """
    chat = FakeChat()
    with pytest.raises(AssertionError, match="event-sourced"):
        await chat.fetch_history("C0AAA")
    with pytest.raises(AssertionError):
        await chat.fetch_replies("C0AAA", "1757000000.000100")


async def test_history_never_called_on_the_relay_path() -> None:
    """Everything the relay can do to Slack, with the call log inspected after.

    The week 6 PIR generator will be added to this list; the assertion it has to
    satisfy is already here and already passing, so the generator inherits the
    constraint rather than being audited for it later.
    """
    chat = FakeChat()
    channel = await chat.create_channel("inc-2026-09-07-payments", idempotency_key="k1")
    await chat.invite(channel.id, ["U1", "U2"])
    posted = await chat.post_message(channel.id, text="runbook", priority=0)
    assert posted is not None
    await chat.pin(channel.id, posted.ts)
    await chat.update_message(channel.id, posted.ts, text="timer")
    await chat.permalink(channel.id, posted.ts)
    await chat.archive_channel(channel.id)

    assert chat.call_count("conversations.history") == 0
    assert chat.call_count("conversations.replies") == 0


# --- guard 3: lexical ---------------------------------------------------------


def test_history_is_not_called_outside_the_reconciler() -> None:
    violations: list[str] = []
    for rel, path in _modules():
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else "")
            )
            if name in HISTORY_CALLS:
                violations.append(f"{rel}:{node.lineno} {name}()")

    assert not violations, (
        "conversations.history has exactly one caller, the reconciler (INV-02, B-01):\n  "
        + "\n  ".join(violations)
    )


def test_the_transcript_package_never_reads_slack() -> None:
    """The read path is Slack pushing to us. Nothing in ``transcript/`` pulls.

    Stronger than the call scan for this package: it may not even *import* the
    history module, so there is no object there to call in the first place.
    """
    violations: list[str] = []
    for rel, path in _modules():
        if not rel.startswith("transcript/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            else:
                continue
            if "chat.history" in module or module.startswith("slack_"):
                violations.append(f"{rel}:{node.lineno} imports {module}")

    assert not violations, "transcript/ must be a write path only:\n  " + "\n  ".join(violations)


def test_the_guard_is_not_vacuous() -> None:
    """A passing scan over zero files, or an allowlist that grew to cover
    everything, would be a false negative."""
    modules = _modules()
    assert len(modules) > 30
    assert len(ALLOWED) == 3
