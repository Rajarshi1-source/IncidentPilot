"""Outbox mechanics, the fake adapter's guarantees, rate limiting and Block Kit.

The database-backed outbox behaviour lives in the integration suite; this covers
the decisions that are pure -- idempotency-key derivation, priority lanes,
channel naming, block limits -- and the properties of the fake that every later
week depends on.
"""

from __future__ import annotations

import pytest

from incidentpilot.adapters.chat import block_kit
from incidentpilot.adapters.chat.base import (
    MAX_CHANNEL_NAME,
    PermanentChatError,
    RetryableChatError,
    channel_name,
    slugify,
)
from incidentpilot.adapters.chat.fake import FakeChat
from incidentpilot.adapters.chat.ratelimit import (
    SHED_AT_PRIORITY,
    bucket_for,
    priority_for,
)
from incidentpilot.adapters.chat.slack import NAME_TAKEN, PERMANENT_ERRORS, classify
from incidentpilot.orchestration.outbox import MAX_ATTEMPTS, idem_key
from incidentpilot.resilience.breaker import BreakerRegistry
from incidentpilot.resilience.retry import (
    NonIdempotentRetry,
    full_jitter_delay,
    with_retry,
)

# --- idempotency keys ---------------------------------------------------------


def test_enqueue_is_idempotent_by_key() -> None:
    """The same intent produces the same key, which is what stops a retry
    creating a second channel."""
    assert idem_key(1, "create_channel") == idem_key(1, "create_channel")


def test_key_separates_incidents_and_actions() -> None:
    assert idem_key(1, "create_channel") != idem_key(2, "create_channel")
    assert idem_key(1, "create_channel") != idem_key(1, "invite_responders")


def test_discriminator_distinguishes_repeats_of_one_action() -> None:
    """A storm banner at 5 alerts and again at 10 are different intents."""
    assert idem_key(1, "post_storm_update", "storm-1") != idem_key(
        1, "post_storm_update", "storm-2"
    )


def test_key_is_a_full_sha256() -> None:
    key = idem_key(1, "create_channel")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


# --- the fake's guarantees ----------------------------------------------------


async def test_fake_create_is_idempotent_by_key() -> None:
    """Crash point 8 in miniature: the same key returns the same channel."""
    chat = FakeChat()
    first = await chat.create_channel("inc-a", idempotency_key="k1")
    second = await chat.create_channel("inc-a", idempotency_key="k1")

    assert first.id == second.id
    assert second.already_existed
    assert len(chat.channels) == 1
    assert chat.call_count("conversations.create") == 2, "both attempts were recorded"


async def test_fake_different_keys_make_different_channels() -> None:
    chat = FakeChat()
    a = await chat.create_channel("inc-a", idempotency_key="k1")
    b = await chat.create_channel("inc-b", idempotency_key="k2")
    assert a.id != b.id


async def test_fetch_history_raises_rather_than_returning_empty() -> None:
    """INV-02 with teeth.

    Returning an empty list would hide exactly the regression this guards:
    since 3 March 2026 a history fetch is 1 request/minute and 15 messages, and
    the real-world failure is silent -- the app simply knows less than it did.
    """
    chat = FakeChat()
    with pytest.raises(AssertionError, match="event-sourced"):
        await chat.fetch_history("C1")
    with pytest.raises(AssertionError, match="hot path"):
        await chat.fetch_replies("C1", "123.456")


async def test_invite_is_set_valued() -> None:
    """Crash point 10: dying two responders into four is safe to replay whole."""
    chat = FakeChat()
    channel = await chat.create_channel("inc-a", idempotency_key="k1")
    await chat.invite(channel.id, ["U1", "U2"])
    await chat.invite(channel.id, ["U1", "U2", "U3", "U4"])
    assert chat.members[channel.id] == {"U1", "U2", "U3", "U4"}


async def test_archive_is_idempotent() -> None:
    """Crash point 12: compensation must be safe to run twice."""
    chat = FakeChat()
    channel = await chat.create_channel("inc-a", idempotency_key="k1")
    await chat.archive_channel(channel.id)
    await chat.archive_channel(channel.id)
    assert channel.id in chat.archived


async def test_reset_calls_keeps_workspace_state() -> None:
    """What a pod restart actually looks like: Slack still holds the channel,
    our record of having called it is gone."""
    chat = FakeChat()
    await chat.create_channel("inc-a", idempotency_key="k1")
    chat.reset_calls()

    assert chat.call_count("conversations.create") == 0
    assert len(chat.channels) == 1, "Slack does not forget across our restart"


# --- priority lanes (B-12) ----------------------------------------------------


def test_channel_creation_is_the_highest_priority() -> None:
    assert priority_for("conversations.create") == 0
    assert priority_for("conversations.invite") == 0


def test_the_runbook_post_outranks_a_generic_post() -> None:
    """The runbook is the war room's reason to exist; a status update is not."""
    assert priority_for("chat.postMessage", purpose="runbook") == 0
    assert priority_for("chat.postMessage") == 1


def test_timer_and_reactions_are_shed_first() -> None:
    """A stale timer is invisible; a delayed war room is an outage."""
    assert priority_for("chat.update", purpose="timer") >= SHED_AT_PRIORITY
    assert priority_for("reactions.add") >= SHED_AT_PRIORITY


def test_priority_2_dropped_under_pressure_but_0_is_not() -> None:
    assert priority_for("chat.update:timer") >= SHED_AT_PRIORITY
    assert priority_for("conversations.create") < SHED_AT_PRIORITY


def test_post_message_is_bucketed_at_one_per_second() -> None:
    """Slack allows roughly 1 message/sec PER CHANNEL, which is why buckets are
    keyed on (method, channel) and not on method alone."""
    assert bucket_for("chat.postMessage").rate_per_s == 1.0


def test_channel_creation_is_tier_2_not_tier_1() -> None:
    """Rev 1 had the tiers backwards: Tier 1 is the MOST restrictive."""
    assert bucket_for("conversations.create").rate_per_s < bucket_for("chat.postMessage").rate_per_s


# --- error classification -----------------------------------------------------


def test_permanent_errors_are_not_retried() -> None:
    """A 4xx fails identically forever; retrying spends rate-limit budget."""
    for code in ("invalid_auth", "missing_scope", "channel_not_found"):
        assert isinstance(classify(code), PermanentChatError)


def test_unknown_errors_are_treated_as_transient() -> None:
    """Defaulting to retryable is the safe direction: a retried permanent error
    wastes a little budget, a dropped transient error loses a war room."""
    assert isinstance(classify("some_new_slack_error"), RetryableChatError)


def test_name_taken_is_not_a_permanent_error() -> None:
    """It is a signal to try the next suffix, not a failure."""
    assert not NAME_TAKEN & PERMANENT_ERRORS


# --- retry --------------------------------------------------------------------


async def test_create_channel_not_retried_without_key() -> None:
    """Retrying an unkeyed create makes a second channel -- the exact bug the
    outbox exists to prevent. Loud refusal beats a silent single attempt."""

    async def op() -> str:
        return "never reached"

    with pytest.raises(NonIdempotentRetry, match="idempotency key"):
        await with_retry(op, method="conversations.create")


async def test_retry_gives_up_after_max_attempts() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        raise RetryableChatError("429")

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(RetryableChatError):
        await with_retry(flaky, method="chat.postMessage", max_attempts=3, sleep=no_sleep)
    assert calls == 3


async def test_retry_stops_immediately_on_a_permanent_error() -> None:
    calls = 0

    async def permanent() -> str:
        nonlocal calls
        calls += 1
        raise PermanentChatError("invalid_auth")

    with pytest.raises(PermanentChatError):
        await with_retry(permanent, method="chat.postMessage")
    assert calls == 1, "a permanent error must not be retried at all"


def test_backoff_is_jittered_not_deterministic() -> None:
    """Exponential backoff alone re-synchronizes every failed caller onto the
    same retry instant, which is how a recovering dependency is knocked over
    twice."""
    delays = {full_jitter_delay(3) for _ in range(20)}
    assert len(delays) > 1, "delays must vary"
    assert all(0 <= d <= 4.0 for d in delays)


def test_outbox_gives_up_at_eight_attempts() -> None:
    assert MAX_ATTEMPTS == 8


# --- breakers -----------------------------------------------------------------


def test_breaker_is_per_dependency() -> None:
    """A global breaker means one flaky dependency opens the circuit for all of
    them -- the model provider times out and Slack calls start being refused."""
    registry = BreakerRegistry()
    slack = registry.for_dependency("slack")
    llm = registry.for_dependency("llm")

    assert slack is not llm
    assert registry.for_dependency("slack") is slack, "same name, same breaker"


def test_breaker_opens_after_five_failures() -> None:
    registry = BreakerRegistry()
    breaker = registry.for_dependency("slack")
    assert breaker.fail_max == 5
    assert breaker.reset_timeout == 30


# --- channel naming (W3-13) ---------------------------------------------------


def test_channel_name_is_slack_legal() -> None:
    name = channel_name(date="2026-09-06", service="payments-api", summary="Postgres Primary Down")
    assert name == "inc-2026-09-06-payments-api-postgres-primary-down"
    assert len(name) <= MAX_CHANNEL_NAME
    assert name == name.lower()
    assert " " not in name


def test_channel_name_truncates_the_summary_not_the_date() -> None:
    """Responders scan a channel list for which incident, which service, when.
    Losing the date to fit a long alert name is the wrong trade."""
    name = channel_name(
        date="2026-09-06",
        service="payments-api",
        summary="A" * 200,
    )
    assert len(name) <= MAX_CHANNEL_NAME
    assert name.startswith("inc-2026-09-06-payments-api-")


def test_channel_name_collision_gets_a_suffix() -> None:
    """Slack rejects a duplicate name outright, and an incident that cannot get
    a channel is an incident with no war room."""
    base = channel_name(date="2026-09-06", service="payments", summary="down")
    second = channel_name(date="2026-09-06", service="payments", summary="down", suffix=1)
    assert base != second
    assert second.endswith("-1")
    assert len(second) <= MAX_CHANNEL_NAME


def test_channel_name_survives_unicode_and_punctuation() -> None:
    name = channel_name(date="2026-09-06", service="paiements-api", summary="Café — 50% erreurs!")
    assert all(c.isalnum() or c == "-" for c in name)


def test_slugify_handles_empty_input() -> None:
    assert slugify("") == ""
    assert slugify("!!!") == ""


# --- Block Kit ----------------------------------------------------------------


def test_blocks_under_50() -> None:
    """Slack rejects a message above 50 blocks outright."""
    too_many = [block_kit.section(f"line {i}") for i in range(80)]
    clamped = block_kit.clamp(too_many)
    assert len(clamped) <= block_kit.MAX_BLOCKS


def test_clamping_says_it_clamped() -> None:
    """A message that silently loses its last sections reads as complete."""
    clamped = block_kit.clamp([block_kit.section(f"line {i}") for i in range(80)])
    assert "more blocks" in str(clamped[-1])


def test_section_text_is_truncated_visibly() -> None:
    block = block_kit.section("x" * 5000)
    text = block["text"]["text"]
    assert len(text) <= block_kit.MAX_SECTION_CHARS
    assert "truncated" in text


def test_incident_header_shows_the_storm_count() -> None:
    """The D3 proof, in the place people actually look."""
    blocks = block_kit.incident_header(
        public_key="inc-2026-09-06-payments-degraded",
        title="PostgresPrimaryDown",
        severity="sev1",
        root_signal="PostgresPrimaryDown",
        correlated_alert_count=40,
        affected_services=12,
        elapsed_label="4m",
        responders=["U1"],
    )
    rendered = str(blocks)
    assert "40 correlated alerts" in rendered
    assert "12 service" in rendered
    assert "postgres" in rendered.lower()


def test_incident_header_states_when_nobody_is_engaged() -> None:
    """The absence of a responder is information during an incident."""
    blocks = block_kit.incident_header(
        public_key="inc-1",
        title="X",
        severity="sev2",
        root_signal=None,
        correlated_alert_count=1,
        affected_services=1,
        elapsed_label="0m",
        responders=[],
    )
    assert "No responder engaged yet" in str(blocks)


def test_merge_notice_always_shows_its_reasoning_and_offers_split() -> None:
    """Opaque grouping is what people distrust about commercial tools, and
    over-correlation needs its correction one click away."""
    blocks = block_kit.merge_notice(
        alertname="HighErrorRate",
        service="payments-api",
        score=0.71,
        reasons=["45s after last activity", "1 hop from postgres-primary"],
        fingerprint="abc123",
    )
    rendered = str(blocks)
    assert "0.71" in rendered
    assert "1 hop from postgres-primary" in rendered
    assert "split_alert" in rendered
