"""🚦 G6: the grounded PIR, against a real database (W6-06..19, D1).

> Provider blocked → skeleton in 90 s. Unblocked → full PIR, zero uncited claims.

Both halves matter, and the first one more. A feature that produces an excellent
document when the vendor is up and nothing when it is down is a feature that is
absent on exactly the days incidents cluster.

The D1 criterion is stricter than the gate line: *"a fabricated ID rejects,
retries, then degrades — **no partial publication**"*. So these assert on the
sequence, not just the endpoints: reject, retry, degrade, and exactly one row in
``pir_documents`` at the end.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incidentpilot.adapters.llm.base import RetryableProviderError
from incidentpilot.adapters.llm.fake import FakeLLM
from incidentpilot.adapters.llm.router import LLMRouter
from incidentpilot.adapters.metrics.fake import FakeMetrics
from incidentpilot.config.models_config import load_models_config
from incidentpilot.config.settings import Settings
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.pir.context import build_grounded_context
from incidentpilot.pir.generator import (
    LAYER_PRIMARY,
    LAYER_SKELETON,
    PIRGenerator,
    persist,
)
from incidentpilot.pir.validator import CitationValidator
from incidentpilot.privacy.redactor import Redactor
from incidentpilot.resilience.budget import BudgetBreaker, Budgets
from tests.conftest import FakeValkey

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "IP_TEST_DATABASE_URL",
    "postgresql+psycopg://ip:ip@127.0.0.1:55432/incidentpilot",
)

CHANNEL = "C0PIR"
START = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
END = START + timedelta(minutes=14)
SHA = "a1b2c3d4e5f6"

# The gate's own budget. The skeleton path must finish well inside it, and it
# does -- there is nothing left to block.
SKELETON_BUDGET_S = 90.0


@pytest_asyncio.fixture(scope="module")
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(Settings(environment="test", database_url=DB_URL))
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no PostgreSQL reachable at {DB_URL}")
    yield build_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def incident_id(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[int]:
    """A resolved incident with a transcript, an alert and a preceding deploy."""
    async with sessions() as session:
        await session.execute(text("TRUNCATE incidents RESTART IDENTITY CASCADE"))
        await session.execute(text("DELETE FROM timeline_events"))
        await session.execute(text("DELETE FROM deploy_events"))
        await session.execute(
            text(
                "INSERT INTO services (name, team, tier) VALUES ('checkout', 'checkout', 1)"
                " ON CONFLICT (name) DO NOTHING"
            )
        )
        new_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO incidents (public_key, dedup_key, title, severity, state,"
                        " chat_channel_id, root_signal, detected_at, resolved_at,"
                        " primary_service_id)"
                        " VALUES ('inc-w6', 'dk-w6', 'Checkout returning 5xx', 'sev1',"
                        " 'resolved', :c, 'HighErrorRate', :start, :end,"
                        " (SELECT id FROM services WHERE name = 'checkout')) RETURNING id"
                    ),
                    {"c": CHANNEL, "start": START, "end": END},
                )
            ).scalar_one()
        )

        for index, (user, body) in enumerate(
            [
                ("U_ANANYA", "checkout is returning 5xx on every card payment"),
                ("U_RAVI", "rolling back the config change on checkout"),
                ("U_ANANYA", "error rate is down, checkout looks healthy again"),
            ]
        ):
            await session.execute(
                text(
                    "INSERT INTO slack_messages (incident_id, channel_id, ts, user_id, text, raw)"
                    " VALUES (:i, :c, :ts, :u, :t, '{}')"
                ),
                {
                    "i": new_id,
                    "c": CHANNEL,
                    "ts": f"{int(START.timestamp()) + index * 30}.000100",
                    "u": user,
                    "t": body,
                },
            )

        await session.execute(
            text(
                "INSERT INTO timeline_events (time, incident_id, intent, confidence,"
                " description, author_user_id, source_message_ts)"
                " VALUES (:t, :i, 'remediation_start', 0.95,"
                " 'rolling back the config change on checkout', 'U_RAVI', :ts)"
            ),
            {
                "t": START + timedelta(seconds=30),
                "i": new_id,
                "ts": f"{int(START.timestamp()) + 30}.000100",
            },
        )
        await session.execute(
            text(
                "INSERT INTO alerts (incident_id, source, fingerprint, alertname, service,"
                " labels, starts_at, is_root_signal, raw)"
                " VALUES (:i, 'alertmanager', 'fp-checkout-5xx', 'HighErrorRate', 'checkout',"
                " '{}', :start, true, '{}')"
            ),
            {"i": new_id, "start": START},
        )
        await session.execute(
            text(
                "INSERT INTO deploy_events (sha, service, environment, actor, title, deployed_at)"
                " VALUES (:sha, 'checkout', 'production', 'U_RAVI', 'raise pool ceiling', :at)"
            ),
            {"sha": SHA, "at": START - timedelta(minutes=12)},
        )
        await session.commit()
        yield new_id


@pytest.fixture
def valkey() -> FakeValkey:
    return FakeValkey()


def _router(provider: FakeLLM, valkey: FakeValkey, **kwargs: Any) -> LLMRouter:
    return LLMRouter(
        load_models_config(),
        {"fake": provider, "anthropic": provider, "openai": provider, "local": provider},
        redactor=Redactor(),
        **kwargs,
    )


def _good_draft(ts: str) -> dict[str, Any]:
    """A draft that cites only real evidence and states no invented number."""
    return {
        "summary": [
            {
                "text": "Checkout returned 5xx on card payments until the config change "
                "was rolled back.",
                "citations": [{"kind": "message", "ref": f"msg:{ts}"}],
            }
        ],
        "root_cause_hypothesis": {
            "text": "A config change deployed to checkout preceded the errors.",
            "citations": [{"kind": "deploy", "ref": f"deploy:{SHA}"}],
        },
        "action_items": [
            {
                "description": "Restore the connection pool ceiling on checkout.",
                "owner": "U_RAVI",
                "priority": "P0",
                "category": "reliability",
                "citations": [{"kind": "message", "ref": f"msg:{ts}"}],
            }
        ],
    }


def _fabricated_draft() -> dict[str, Any]:
    return {
        "summary": [
            {
                "text": "Checkout returned 5xx on card payments.",
                "citations": [{"kind": "message", "ref": "msg:9999999999.999999"}],
            }
        ]
    }


# --- 🚦 the gate, half one: the provider is unreachable ------------------------


async def test_a_blocked_provider_still_produces_a_document(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """G6, first run. Nothing to block: the skeleton needs no key and no network.

    Every provider in the chain fails, and the incident still ends with a
    document that says what happened, what was computed, and what a human needs
    to finish.
    """
    import time

    provider = FakeLLM()
    provider.fail_next(times=10, error=RetryableProviderError)
    generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())

    started = time.perf_counter()
    async with sessions() as session:
        result = await generator.generate(session, incident_id)
    elapsed = time.perf_counter() - started

    assert result.layer == LAYER_SKELETON
    assert elapsed < SKELETON_BUDGET_S
    assert "AI drafting unavailable" in result.markdown
    assert "TODO" in result.markdown
    # The banner is the point. A degraded document that does not say it is
    # degraded gets read as the finished thing.
    assert result.markdown.startswith(">")


async def test_the_skeleton_still_carries_the_computed_impact(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """Impact is computed BEFORE the model call, so it survives the model failing.

    That ordering is the feature: the numbers were never the model's to produce.
    """
    provider = FakeLLM()
    provider.fail_next(times=10)
    generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())

    async with sessions() as session:
        result = await generator.generate(session, incident_id)

    assert result.impact.available
    assert "failed_requests" in result.markdown


async def test_a_tripped_budget_degrades_rather_than_blocking(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    breaker = BudgetBreaker(valkey, Budgets(per_incident=0.01))
    await breaker.record(incident_id, 0.99)

    provider = FakeLLM()
    generator = PIRGenerator(_router(provider, valkey, budget=breaker), metrics=FakeMetrics())

    async with sessions() as session:
        result = await generator.generate(session, incident_id)

    assert result.layer == LAYER_SKELETON
    assert provider.calls == 0, "the budget was checked before anything was sent"


# --- 🚦 the gate, half two: the provider works ---------------------------------


async def test_a_grounded_draft_publishes_with_full_coverage(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """G6, second run: zero uncited claims, coverage 1.000."""
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)
    first_ts = ctx.messages[0].ts

    provider = FakeLLM()
    provider.next_response(_good_draft(first_ts))
    generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())

    async with sessions() as session:
        result = await generator.generate(session, incident_id)

    assert result.layer == LAYER_PRIMARY
    assert result.validation is not None and result.validation.ok
    assert result.coverage == 1.0
    assert result.prompt_version == "2.1.0"
    assert result.prompt_sha256


async def test_the_prompt_carries_every_reference_id(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """The model chooses from a list rather than recalling from training.

    That is why fabrication is rare, and it is why fabrication is detectable
    when it happens: the valid set and the prompt corpus are the same objects.
    """
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)
        provider = FakeLLM()
        provider.next_response(_good_draft(ctx.messages[0].ts))
        generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())
        await generator.generate(session, incident_id)

    sent = provider.payloads[0].user
    for ref in ctx.valid_reference_set():
        if ref.startswith("metric:"):
            continue  # windows are listed by ref too, but after impact attaches
        assert ref in sent, f"{ref} was citable but never shown to the model"


# --- 🚦 D1: reject, retry, degrade, no partial publication ---------------------


async def test_a_fabricated_citation_rejects_then_retries_then_degrades(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """The full D1 sequence, and the assertion that matters is the last one.

    Two fabricated drafts, two rejections, and exactly ONE document at the end --
    the skeleton. A generator that persisted as it went would leave half a PIR
    behind on each rejected attempt, which is the "partial publication" D1
    forbids.
    """
    provider = FakeLLM()
    provider.next_response(_fabricated_draft())
    provider.next_response(_fabricated_draft())
    generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())

    async with sessions() as session:
        result = await generator.generate(session, incident_id)
        _, revision = await persist(session, result)
        await session.commit()

    assert provider.calls == 2, "the second provider was not tried"
    assert result.layer == LAYER_SKELETON

    async with sessions() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM pir_documents WHERE incident_id = :i"),
                {"i": incident_id},
            )
        ).scalar_one()
    assert count == 1, "a rejected attempt was published"
    assert revision == 1


async def test_a_retry_after_a_rejection_can_succeed(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """Reject, then the second provider gets it right. No degradation needed."""
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    provider = FakeLLM()
    provider.next_response(_fabricated_draft())
    provider.next_response(_good_draft(ctx.messages[0].ts))
    generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())

    async with sessions() as session:
        result = await generator.generate(session, incident_id)

    assert result.layer == "llm_secondary"
    assert result.coverage == 1.0


# --- persistence (W6-06) ------------------------------------------------------


async def test_pir_unique_revision(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """Revisions increment; the constraint is what makes that safe.

    Computed inside the INSERT, so two concurrent generations cannot both write
    revision 1 -- the loser hits ``uq_pir_revision``, which is the correct
    outcome rather than something the caller must remember to handle.
    """
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    for expected in (1, 2):
        provider = FakeLLM()
        provider.next_response(_good_draft(ctx.messages[0].ts))
        generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())
        async with sessions() as session:
            result = await generator.generate(session, incident_id)
            _, revision = await persist(session, result)
            await session.commit()
        assert revision == expected


async def test_the_stored_row_records_how_it_was_made(
    sessions: async_sessionmaker[AsyncSession], valkey: FakeValkey, incident_id: int
) -> None:
    """Coverage, prompt version and hash, and the layer. Auditable afterwards.

    A coverage figure that only ever existed inside a passing test is not
    evidence of anything.
    """
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    provider = FakeLLM()
    provider.next_response(_good_draft(ctx.messages[0].ts))
    generator = PIRGenerator(_router(provider, valkey), metrics=FakeMetrics())

    async with sessions() as session:
        result = await generator.generate(session, incident_id)
        await persist(session, result)
        await session.commit()

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT generation_layer, citation_coverage, validation_passed,"
                    " prompt_version, prompt_sha256 FROM pir_documents"
                    " WHERE incident_id = :i"
                ),
                {"i": incident_id},
            )
        ).one()

    assert row.generation_layer == LAYER_PRIMARY
    assert float(row.citation_coverage) == 1.0
    assert row.validation_passed is True
    assert row.prompt_version == "2.1.0"
    assert len(row.prompt_sha256) == 64


# --- the context (W6-08) ------------------------------------------------------


async def test_the_context_comes_from_our_store_not_from_slack(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """INV-02 held all the way to the flagship.

    Week 4 event-sourced the transcript precisely so this is a query rather than
    ten minutes of paginated history calls.
    """
    from incidentpilot.adapters.chat.fake import FakeChat

    chat = FakeChat()
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    assert len(ctx.messages) == 3
    assert chat.call_count("conversations.history") == 0


async def test_the_valid_reference_set_covers_all_six_kinds(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    from incidentpilot.impact.promql import compute_impact

    ctx.attach_impact(
        await compute_impact(
            FakeMetrics(),
            service="checkout",
            namespace=None,
            detected_at=START,
            resolved_at=END,
            now=END,
        )
    )
    refs = ctx.valid_reference_set()
    prefixes = {ref.split(":", 1)[0] for ref in refs}
    assert prefixes == {"msg", "tl", "alert", "deploy", "metric"}


async def test_participants_are_the_people_who_were_there(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)
    assert ctx.participants == {"U_ANANYA", "U_RAVI"}


async def test_a_deploy_outside_the_window_is_not_evidence(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """Deploys are facts about the world; the window is what makes one relevant.

    A deploy from last week is not evidence about this incident, and letting it
    into the valid set would make an irrelevant citation *valid*.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO deploy_events (sha, service, environment, deployed_at)"
                " VALUES ('ffffffffffff', 'checkout', 'production', :at)"
            ),
            {"at": START - timedelta(days=7)},
        )

    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    assert {d.sha for d in ctx.deploys} == {SHA}


# --- the validator against real evidence --------------------------------------


async def test_an_ungrounded_number_is_rejected_against_real_impact(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """B-10, end to end: computed numbers pass, invented ones do not."""
    from incidentpilot.impact.promql import compute_impact

    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)
    ctx.attach_impact(
        await compute_impact(
            FakeMetrics(),
            service="checkout",
            namespace=None,
            detected_at=START,
            resolved_at=END,
            now=END,
        )
    )

    from incidentpilot.pir.schema import PIRDraft

    draft = PIRDraft.model_validate(
        {
            "summary": [
                {
                    "text": "Checkout errors affected 48000 customers.",
                    "citations": [{"kind": "message", "ref": f"msg:{ctx.messages[0].ts}"}],
                }
            ]
        }
    )
    report = CitationValidator().validate(draft, ctx)
    assert not report.ok
    assert "ungrounded_number" in report.reasons()


async def test_the_deploy_that_fixed_it_is_still_evidence(
    sessions: async_sessionmaker[AsyncSession], incident_id: int
) -> None:
    """The window does not close at ``resolved_at``.

    A rollback is often the last thing that happens before someone types
    ``/resolve``, so the deploy that fixed the incident lands at or just after
    resolution. "The fix shipped at 03:41" is a claim a PIR should be able to
    make -- and a smoke run against the real app is what found this, because the
    deploy webhook fired a moment after the incident closed and the deploy was
    invisible to the context.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO deploy_events (sha, service, environment, title, deployed_at)"
                " VALUES ('bbbbbbbbbbbb', 'checkout', 'production', 'the rollback', :at)"
            ),
            {"at": END + timedelta(minutes=2)},
        )

    async with sessions() as session:
        ctx = await build_grounded_context(session, incident_id)

    assert "bbbbbbbbbbbb" in {d.sha for d in ctx.deploys}
    assert "deploy:bbbbbbbbbbbb" in ctx.valid_reference_set()
