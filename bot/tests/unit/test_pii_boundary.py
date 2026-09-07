"""D6: no raw PII reaches a provider adapter (§16, W6-13).

§16 names this as "the PII CI test to name in an interview", and the reason is
that it asserts on the **egress payload** rather than on the redactor. A test of
the redactor proves the redactor works; this proves nothing bypassed it, which
is the failure that actually happens -- a second code path, a new provider, a
prompt assembled after the redaction step.

That is only possible because ``FakeLLM`` records exactly what it was sent. A
fake that only returned canned answers could not support this test at all.
"""

from __future__ import annotations

import re

import pytest

from incidentpilot.adapters.llm.fake import FakeLLM
from incidentpilot.adapters.llm.router import LLMRouter
from incidentpilot.config.models_config import load_models_config
from incidentpilot.pir.schema import PIRDraft
from incidentpilot.privacy.redactor import Redactor

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
AWS_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")

DIRTY = (
    "Customer ananya.sharma@example.com reported it twice. "
    "Card 4111111111111111 was declined. "
    "Someone pasted AKIAIOSFODNN7EXAMPLE and xoxb-123456789012-abcdefghijkl. "
    "Callback to 9876543210 about ORD-99182."
)


def _valid_draft_json() -> str:
    return (
        '{"summary": [{"text": "Checkout failed for card payments.",'
        ' "citations": [{"kind": "message", "ref": "msg:1757000000.000100"}]}]}'
    )


@pytest.fixture
def provider() -> FakeLLM:
    fake = FakeLLM()
    fake.next_response(_valid_draft_json())
    return fake


@pytest.fixture
def router(provider: FakeLLM) -> LLMRouter:
    config = load_models_config()
    return LLMRouter(config, {"fake": provider}, redactor=Redactor())


async def test_no_raw_pii_reaches_provider(router: LLMRouter, provider: FakeLLM) -> None:
    """The one to name. Asserted on what LEFT, not on what the redactor did."""
    await router.complete(
        role="synthesize",
        system="You write postmortems.",
        user=DIRTY,
        schema=PIRDraft,
        incident_id=1,
    )

    assert provider.payloads, "nothing was sent — the test would pass vacuously"
    for payload in provider.payloads:
        assert not EMAIL_RE.search(payload.text), "raw email reached the provider"
        assert not CARD_RE.search(payload.text), "raw card number reached the provider"
        assert not AWS_RE.search(payload.text), "an AWS key reached the provider"
        assert not SLACK_TOKEN_RE.search(payload.text), "a Slack token reached the provider"


async def test_the_system_prompt_is_redacted_too(provider: FakeLLM) -> None:
    """Not only the user turn.

    An easy way to reintroduce this bug is to assemble context into the system
    prompt -- which is exactly where a "here are the incident's participants"
    block would naturally go.
    """
    router = LLMRouter(load_models_config(), {"fake": provider}, redactor=Redactor())
    await router.complete(
        role="synthesize",
        system=f"Context: {DIRTY}",
        user="Write it.",
        schema=PIRDraft,
        incident_id=1,
    )
    assert not EMAIL_RE.search(provider.payloads[0].system)


async def test_redaction_survives_a_provider_failure() -> None:
    """A failing provider still received the payload.

    That is the case where "did PII leave?" matters most, and a fake that
    recorded only successful calls would answer it wrongly.
    """
    provider = FakeLLM()
    provider.fail_next(times=1)
    router = LLMRouter(load_models_config(), {"fake": provider}, redactor=Redactor())

    with pytest.raises(Exception):  # noqa: B017 - any provider failure will do
        await router.complete(
            role="extract",
            system="s",
            user=DIRTY,
            schema=PIRDraft,
            incident_id=1,
        )

    assert provider.payloads
    assert not EMAIL_RE.search(provider.payloads[0].text)


async def test_stable_tokens_preserve_the_inference_that_matters(
    router: LLMRouter, provider: FakeLLM
) -> None:
    """`<EMAIL_1>` is the same person throughout.

    The model can still reason about "the same customer reported it twice" while
    never seeing the address -- which is the whole reason the tokens are stable
    rather than random per occurrence.
    """
    await router.complete(
        role="synthesize",
        system="s",
        user="a@b.com first, then a@b.com again, and c@d.com once",
        schema=PIRDraft,
        incident_id=1,
    )
    sent = provider.payloads[0].user
    assert sent.count("<EMAIL_1>") == 2
    assert "<EMAIL_2>" in sent


async def test_a_router_without_a_redactor_is_a_deliberate_choice() -> None:
    """Documented rather than silently permitted.

    ``redactor=None`` is what the local-model path uses: a self-hosted runtime
    inside the network is not an egress boundary, and tokenizing there would
    make the transcript harder to reason about for no privacy gain. The test
    exists so that removing the redactor from the *hosted* path is a visible
    change rather than an omission.
    """
    provider = FakeLLM()
    provider.next_response(_valid_draft_json())
    router = LLMRouter(load_models_config(), {"fake": provider}, redactor=None)

    await router.complete(
        role="synthesize", system="s", user="a@b.com", schema=PIRDraft, incident_id=1
    )
    assert "a@b.com" in provider.payloads[0].user
