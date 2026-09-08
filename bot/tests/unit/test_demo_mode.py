"""DEMO_MODE cannot write anywhere (W8-09, §16).

The public URL on a résumé is a URL strangers open. The guarantee this file
tests is not "the demo declines to write" but "**the demo has nothing to write
with**" -- the adapter factories resolve to the in-memory fakes, so a mistaken
call reaches a dictionary rather than a workspace.

That distinction is the whole design. A flag checked at ten call sites is a flag
somebody forgets at the eleventh, and the eleventh is the one that creates a
channel in a stranger's Slack.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from incidentpilot.adapters.chat.factory import build_chat
from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.adapters.paging.fake import FakePaging
from incidentpilot.config.settings import Settings

SRC = Path(__file__).resolve().parents[2] / "src" / "incidentpilot"


def _demo(**overrides: object) -> Settings:
    return Settings(environment="prod", demo_mode=True, **overrides)  # type: ignore[arg-type]


def test_demo_mode_blocks_writes() -> None:
    """Configured for Slack, running in demo: the adapter is still a fake.

    Demo mode wins over configuration deliberately. Whoever deploys the demo
    will copy a production env file, and the one setting they must not be able
    to get wrong is the one that reaches a real workspace.
    """
    cfg = _demo(chat_provider="slack", slack_bot_token="xoxb-not-a-real-token")
    adapter = build_chat(cfg)
    assert isinstance(adapter, FakeChat), "demo mode must not resolve a real chat adapter"


def test_demo_mode_blocks_paging() -> None:
    from incidentpilot.adapters.paging.factory import build_paging

    cfg = _demo(paging_provider="pagerduty")
    assert isinstance(build_paging(cfg), FakePaging)


def test_demo_mode_is_off_by_default() -> None:
    """A safety switch that defaults to on is a switch nobody tests the other way.

    More to the point: a production deployment that silently ran in demo mode
    would page nobody during a real incident, which is a far worse failure than
    the one demo mode prevents.
    """
    assert Settings(environment="test").demo_mode is False
    assert Settings(environment="test").effective_chat_provider == "fake"  # the C-02 default


def test_a_real_deployment_still_reaches_slack() -> None:
    """The other direction. A guard that blocks everything is not a guard.

    The assertion is "it did not silently fall back to the fake", not "it
    returned a working Slack client". Building the real one needs the optional
    `llm`/slack async extras, which the unit environment deliberately does not
    install -- so taking the Slack branch and failing to import is *evidence of
    the right branch*, while a FakeChat would be the bug. Asserting the
    successful construction instead would make this test a check on which
    extras happen to be installed.
    """
    cfg = Settings(
        environment="prod",
        demo_mode=False,
        chat_provider="slack",
        slack_bot_token="xoxb-not-a-real-token",
    )
    assert cfg.effective_chat_provider == "slack"
    try:
        adapter = build_chat(cfg)
    except ModuleNotFoundError:
        return  # took the Slack branch; the optional dependency is simply absent
    assert not isinstance(adapter, FakeChat)


def test_every_provider_decision_goes_through_the_effective_property() -> None:
    """The structural half, enforced on the AST rather than by review.

    A factory that reads ``cfg.chat_provider`` directly bypasses demo mode
    entirely and would keep passing every behavioural test above -- because
    those construct the factory that was fixed, not the one somebody adds next
    month. This checks the rule at the only place it can be checked: every
    factory in the tree.
    """
    violations: list[str] = []
    for path in sorted(SRC.rglob("factory.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"chat_provider", "paging_provider"}
                and isinstance(node.value, ast.Name)
            ):
                rel = path.relative_to(SRC).as_posix()
                violations.append(f"{rel}:{node.lineno} reads {node.value.id}.{node.attr}")

    assert not violations, (
        "provider selection must go through effective_chat_provider / "
        "effective_paging_provider so DEMO_MODE cannot be bypassed (W8-09):\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.parametrize("provider_attr", ["effective_chat_provider", "effective_paging_provider"])
def test_demo_mode_forces_fakes_whatever_is_configured(provider_attr: str) -> None:
    cfg = _demo(chat_provider="slack", paging_provider="pagerduty")
    assert getattr(cfg, provider_attr) == "fake"
