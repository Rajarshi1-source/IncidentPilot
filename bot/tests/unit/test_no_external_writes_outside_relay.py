"""INV-03: exactly one component performs external writes.

If two processes can create a Slack channel, eventually two will. The outbox
makes idempotency tractable precisely by concentrating side effects in one
module -- and that concentration is a property of the import graph, not of
anyone's discipline.

This is the test that stops a "temporary" direct Slack call being added in
week 6 to see something appear in a channel. It is cheap now and would be
expensive to retrofit after three more weeks of code.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "incidentpilot"

# The single module allowed to import a chat adapter's write surface.
RELAY = "orchestration/handlers.py"

# The one documented exception, and the reason it has to be one.
#
# The degradation banner (W7-17, INV-12) is an external write, and every other
# external write in this system goes through the transactional outbox. This one
# cannot. The outbox is a *database table*, and degradation level 2 is "the
# database is unreachable" -- so routing the announcement through it would mean
# the notice that the database is down can only be delivered when the database
# is up. That is the same circular dependency that makes `IncidentPilotDown`
# bypass IncidentPilot, and it fails in the same direction: silently, on the one
# occasion it matters.
#
# The trade it accepts is duplication rather than loss. Two processes changing
# level could post two banners; the alternative is a system that degrades
# without saying so, which INV-12 exists to forbid. A duplicate banner is
# noise, an unannounced degradation is a lie -- so the write stays direct,
# best-effort (it never raises, see `_post`), and listed here instead of
# quietly excluded.
DEGRADATION = "resilience/degradation.py"

# Wiring is allowed to name an adapter (main.py constructs one); calling its
# write methods is not. These are the methods that mutate the outside world.
WRITE_METHODS: frozenset[str] = frozenset(
    {
        "create_channel",
        "invite",
        "post_message",
        "update_message",
        "pin",
        "archive_channel",
        # W5: waking a human is an external write like any other, and the
        # paging adapters are subject to the same rule as the chat ones.
        "page",
    }
)

# Modules that may legitimately reference the adapter *type* -- for construction,
# dependency injection or type annotations -- without calling through it.
ALLOWED_TYPE_REFERENCES: frozenset[str] = frozenset(
    {
        "main.py",
        "api/deps.py",
        "orchestration/handlers.py",
        "adapters/chat/base.py",
        "adapters/chat/slack.py",
        "adapters/chat/fake.py",
        "adapters/chat/ratelimit.py",
        "adapters/chat/block_kit.py",
        "adapters/chat/factory.py",
        # Transport and read-only surfaces. Both live under adapters/chat/
        # for exactly this reason: the exemption stays a property of one
        # directory rather than growing a list of scattered files.
        "adapters/chat/socket_mode.py",
        "adapters/chat/history.py",
        # W5. The paging adapters define the write surface and the factory
        # constructs one; neither calls through it.
        "adapters/paging/base.py",
        "adapters/paging/fake.py",
        "adapters/paging/pagerduty.py",
        "adapters/paging/static_schedule.py",
        "adapters/paging/factory.py",
        # Decides who to page and asks the provider who is on call -- a read.
        # The write itself happens in handlers.py.
        "orchestration/routing.py",
    }
)


def _modules() -> list[tuple[str, Path]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.endswith("__init__.py"):
            continue
        out.append((rel, path))
    return out


def test_only_handlers_import_the_chat_adapter() -> None:
    """A module that imports a concrete chat adapter can call it.

    Restricting the import is stronger and simpler than trying to detect calls,
    because a module that never imports the adapter cannot reach it at all.
    """
    violations: list[str] = []
    for rel, path in _modules():
        if rel in ALLOWED_TYPE_REFERENCES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module: str | None = None
            lineno = getattr(node, "lineno", 0)
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module and (
                module.startswith("incidentpilot.adapters.chat.slack")
                or module.startswith("slack_sdk")
                or module.startswith("slack_bolt")
            ):
                violations.append(f"{rel}:{lineno} imports {module}")

    assert not violations, (
        "external chat writes must go through the outbox relay (INV-03):\n  "
        + "\n  ".join(violations)
    )


def test_no_module_outside_the_relay_calls_a_write_method() -> None:
    """Belt and braces: catch a write call reached through an injected adapter.

    The import check above cannot see ``self._chat.create_channel(...)`` when
    ``_chat`` arrived as a constructor argument, which is exactly how a shortcut
    would most plausibly be written.
    """
    violations: list[str] = []
    for rel, path in _modules():
        if rel in (RELAY, DEGRADATION) or rel.startswith("adapters/chat/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in WRITE_METHODS:
                continue
            owner = func.value
            owner_name = (
                owner.attr
                if isinstance(owner, ast.Attribute)
                else (owner.id if isinstance(owner, ast.Name) else "")
            )
            # Heuristic, and deliberately narrow: only flag calls on something
            # that looks like a chat handle. A broader match would fire on
            # unrelated `.pin(...)` methods and train people to ignore it.
            haystack = owner_name.lower()
            if any(token in haystack for token in ("chat", "slack", "paging", "pager")):
                violations.append(f"{rel}:{node.lineno} {owner_name}.{func.attr}()")

    assert not violations, (
        "only orchestration/handlers.py may perform external chat writes (INV-03):\n  "
        + "\n  ".join(violations)
    )


def test_the_degradation_exception_stays_one_call() -> None:
    """The exception above is bounded to a single, best-effort post.

    An allowlist entry with no test is an allowlist entry that grows. This one
    permits exactly one write method, called exactly once, inside a `try` --
    adding `create_channel` to the degradation manager, or letting the post
    raise, would fail here rather than passing on the strength of a comment.
    """
    path = SRC / DEGRADATION
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WRITE_METHODS
    ]
    assert len(calls) == 1, f"the degradation exception permits one write call, found {len(calls)}"
    only = calls[0].func
    assert isinstance(only, ast.Attribute)
    assert only.attr == "post_message", (
        "the degradation manager may announce, not create channels or page"
    )

    guarded = any(
        isinstance(handler, ast.Try)
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in WRITE_METHODS
            for inner in ast.walk(handler)
        )
        for handler in ast.walk(tree)
    )
    assert guarded, (
        "the announcement must not raise -- an exception here would abort the "
        "degradation it was announcing and leave the system at the old level"
    )


def test_the_relay_actually_exists() -> None:
    """A passing check over a missing file would be a false negative."""
    assert (SRC / RELAY).is_file(), f"the relay module is missing at {RELAY}"
