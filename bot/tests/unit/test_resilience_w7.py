"""The degradation ladder, the lifeboat, and the brownout buffer (W7-17/18/19).

The property under test throughout is not "it stayed up" -- it is **which
invariant survived**. A resilience test that asserts a process is alive tells
you nothing; every one of these asserts what the system did while it was broken.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from incidentpilot.resilience.degradation import BANNERS, DegradationManager, Level
from incidentpilot.telemetry.metrics import DEGRADATION_LEVEL

LIFEBOAT = Path(__file__).resolve().parents[3] / "lifeboat" / "main.py"


class RecordingChat:
    """Just enough chat surface to see what the channel was told."""

    def __init__(self, *, fail: bool = False) -> None:
        self.posts: list[tuple[str, str, int]] = []
        self.fail = fail

    async def post_message(
        self, channel_id: str, *, text: str, blocks: Any = None, priority: int = 0
    ) -> None:
        if self.fail:
            raise ConnectionError("slack is unreachable")
        self.posts.append((channel_id, text, priority))


# --- W7-17: the four levels, announced ----------------------------------------


async def test_degradation_announces() -> None:
    """INV-12. Every level change posts, and the gauge moves with it.

    Silent degradation is worse than failure: a product that fails visibly gets
    worked around, and one that quietly starts producing less trustworthy output
    keeps being trusted.
    """
    chat = RecordingChat()
    manager = DegradationManager(chat, ops_channel="C_OPS")

    assert await manager.set_level(Level.DEGRADED, "provider circuit open")
    assert manager.level is Level.DEGRADED
    assert DEGRADATION_LEVEL._value.get() == 1
    assert manager.said("degraded")
    assert manager.said("provider circuit open")
    # The banner says what still works. "Degraded" alone makes people stop
    # trusting the parts that are fine.
    assert "Timeline, correlation, paging" in chat.posts[0][1]

    assert await manager.set_level(Level.BROWNOUT, "database unreachable")
    assert DEGRADATION_LEVEL._value.get() == 2
    assert manager.said("buffered durably")

    assert await manager.set_level(Level.LIFEBOAT, "readiness failing")
    assert DEGRADATION_LEVEL._value.get() == 3
    assert len(chat.posts) == 3


async def test_all_four_levels_have_an_inducible_announcement() -> None:
    """D7's definition of done: every level can be induced deliberately."""
    for level in (Level.DEGRADED, Level.BROWNOUT, Level.LIFEBOAT):
        chat = RecordingChat()
        manager = DegradationManager(chat, ops_channel="C_OPS")
        await manager.set_level(level, "induced by a chaos experiment")
        assert chat.posts, f"{level.name} did not announce itself"
        assert BANNERS[level].split("{")[0].strip() in chat.posts[0][1]


async def test_repeating_a_level_does_not_repeat_the_banner() -> None:
    """A brownout re-asserting every five seconds must not post twelve banners a minute.

    A channel full of banners is a channel where the banner stops being read,
    which is the same outcome as never posting one.
    """
    chat = RecordingChat()
    manager = DegradationManager(chat, ops_channel="C_OPS")
    await manager.set_level(Level.BROWNOUT, "database unreachable")
    await manager.set_level(Level.BROWNOUT, "database unreachable")
    await manager.set_level(Level.BROWNOUT, "still unreachable")
    assert len(chat.posts) == 1


async def test_an_unannounceable_degradation_still_degrades_and_says_so() -> None:
    """The announcement must not be able to abort the degradation.

    If posting raised, the system would stay at the *old* level while the
    dependency behind the new one was still broken -- the failure would be
    hidden by the failure of the thing that reports failures.
    """
    chat = RecordingChat(fail=True)
    manager = DegradationManager(chat, ops_channel="C_OPS")

    announced = await manager.set_level(Level.BROWNOUT, "database unreachable")

    assert announced is False
    assert manager.level is Level.BROWNOUT
    assert DEGRADATION_LEVEL._value.get() == 2


async def test_recovery_is_announced_too() -> None:
    """People told to use the manual process need to be told to stop."""
    chat = RecordingChat()
    manager = DegradationManager(chat, ops_channel="C_OPS")
    await manager.set_level(Level.BROWNOUT, "database unreachable")
    assert await manager.recover("database back")
    assert manager.level is Level.NORMAL
    assert manager.said("back to normal")


async def test_the_capability_flags_track_the_level() -> None:
    manager = DegradationManager()
    assert manager.llm_available and manager.db_available and manager.accepting_writes

    await manager.set_level(Level.DEGRADED, "budget exhausted")
    assert not manager.llm_available
    assert manager.db_available

    await manager.set_level(Level.BROWNOUT, "database unreachable")
    assert not manager.db_available
    # Ingest keeps accepting: a dropped alert is unrecoverable, a delayed one
    # is not. That asymmetry is why ingest is the one AP component here.
    assert manager.accepting_writes

    await manager.set_level(Level.LIFEBOAT, "we are gone")
    assert not manager.accepting_writes


# --- W7-18: the lifeboat ------------------------------------------------------


def test_lifeboat_imports_nothing() -> None:
    """Standard library only. D7's whole premise.

    If the lifeboat imported application code it would fail for the same reason
    the application failed -- it would be a second copy of the bug rather than
    an insurance policy against it.
    """
    tree = ast.parse(LIFEBOAT.read_text(encoding="utf-8"), filename=str(LIFEBOAT))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported, "the lifeboat should import something, if only urllib"
    assert "incidentpilot" not in imported
    non_stdlib = imported - set(sys.stdlib_module_names)
    assert not non_stdlib, f"the lifeboat must import stdlib only, found {sorted(non_stdlib)}"


def test_lifeboat_shares_no_image_with_the_bot() -> None:
    """A `FROM incidentpilot-bot` lifeboat is not a separate failure domain."""
    raw = (LIFEBOAT.parent / "Dockerfile").read_text(encoding="utf-8")
    # Instructions only. Matching the whole file makes the check fire on the
    # comment that explains the check -- the same self-inflicted false positive
    # as the floating-tag grep in ci.yml, and a guard that flags itself is a
    # guard people learn to ignore.
    instructions = "\n".join(
        line for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    froms = [line for line in instructions.splitlines() if line.upper().startswith("FROM")]
    assert len(froms) == 1, "one stage: a build step is another thing that can fail"
    assert "incidentpilot" not in froms[0].lower()
    assert ":latest" not in instructions
    assert "pip install" not in instructions
    assert "COPY --from" not in instructions, "nothing may be copied out of the bot image"


def test_lifeboat_fires_once_on_the_second_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the first -- a single failed probe during a rolling restart is normal,
    and paging on it teaches people to ignore the one alarm that still works.

    Not the third either, and not every minute after: an hour of downtime is one
    piece of news, and sixty copies of it is a muted channel.
    """
    monkeypatch.syspath_prepend(str(LIFEBOAT.parent))
    import importlib

    lifeboat = importlib.import_module("main")
    importlib.reload(lifeboat)

    posted: list[str] = []
    monkeypatch.setattr(lifeboat, "FAILS", tmp_path / "fails")
    monkeypatch.setattr(lifeboat, "STATE", tmp_path / "oncall.json")
    monkeypatch.setattr(lifeboat, "MANUAL_RUNBOOK", "https://runbook.invalid/manual")
    monkeypatch.setattr(lifeboat, "healthy", lambda: False)

    def _capture(text: str) -> bool:
        posted.append(text)
        return True

    monkeypatch.setattr(lifeboat, "post", _capture)

    (tmp_path / "oncall.json").write_text('{"responder": "U_PRIMARY"}', encoding="utf-8")

    assert lifeboat.main() == 0
    assert posted == [], "the first failed probe must not fire"

    assert lifeboat.main() == 0
    assert len(posted) == 1
    assert "U_PRIMARY" in posted[0]
    assert "manual" in posted[0].lower()

    for _ in range(5):
        lifeboat.main()
    assert len(posted) == 1, "a long outage is one message, not one per minute"

    # Recovery resets the counter, so the next outage fires again.
    monkeypatch.setattr(lifeboat, "healthy", lambda: True)
    lifeboat.main()
    monkeypatch.setattr(lifeboat, "healthy", lambda: False)
    lifeboat.main()
    lifeboat.main()
    assert len(posted) == 2


def test_lifeboat_survives_missing_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown on-call is still worth posting.

    The alternative -- refusing to alert because the state file is gone -- makes
    the lifeboat fail exactly when the bot that writes that file has been down
    long enough to matter.
    """
    monkeypatch.syspath_prepend(str(LIFEBOAT.parent))
    import importlib

    lifeboat = importlib.reload(importlib.import_module("main"))
    monkeypatch.setattr(lifeboat, "STATE", tmp_path / "nothing-here.json")
    assert lifeboat.last_oncall() == "unknown"
    assert "unknown" in lifeboat.message()
