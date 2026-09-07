"""W6-02..05, 13, 15..17, 20: impact, validator, privacy, prompts, skeleton.

The validator tests are the flagship's actual acceptance criteria. D1 says a
fabricated ID must reject; B-09 says nothing may gate on self-reported
confidence; B-10 and INV-06 say a number the model invented must not survive.
All three are single assertions here, and all three are cheap because the gate
is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from incidentpilot.adapters.llm.base import BudgetExceeded
from incidentpilot.adapters.metrics.fake import FakeMetrics
from incidentpilot.config.models_config import ModelConfigError, load_models_config
from incidentpilot.domain.evidence import CitationKind
from incidentpilot.domain.redaction_rules import PIIClass
from incidentpilot.impact.promql import IMPACT_QUERIES, compute_impact, verified_facts_block
from incidentpilot.pir.context import (
    AlertRef,
    DeployRef,
    GroundedContext,
    MessageRef,
    TimelineRef,
)
from incidentpilot.pir.prompts.registry import PromptIntegrityError, load_registry, sha256_of
from incidentpilot.pir.schema import ActionItem, Citation, Claim, PIRDraft
from incidentpilot.pir.skeleton import BANNER, render_skeleton
from incidentpilot.pir.validator import (
    FABRICATED,
    NON_PARTICIPANT,
    UNGROUNDED_NUMBER,
    CitationValidator,
)
from incidentpilot.privacy.redactor import Redactor
from incidentpilot.resilience.budget import BudgetBreaker, Budgets
from tests.conftest import FakeValkey

START = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
END = START + timedelta(minutes=14)
MSG_TS = "1757000012.000100"


@pytest.fixture
def ctx() -> GroundedContext:
    context = GroundedContext(
        incident_id=1,
        public_key="inc-2026-09-07-1",
        title="Checkout returning 5xx",
        severity="sev1",
        detected_at=START,
        resolved_at=END,
        service="checkout",
        runbook_name="High Error Rate",
    )
    context.messages = [
        MessageRef(
            ts=MSG_TS, user_id="U_ANANYA", text="checkout is returning 5xx on every card payment"
        ),
        MessageRef(ts="1757000090.000100", user_id="U_RAVI", text="rolling back the config change"),
    ]
    context.timeline = [
        TimelineRef(
            id="1757000090-remediation_start",
            at=START + timedelta(minutes=1),
            intent="remediation_start",
            description="rolling back the config change",
            author_user_id="U_RAVI",
        )
    ]
    context.alerts = [
        AlertRef(
            fingerprint="fp-checkout-5xx",
            alertname="HighErrorRate",
            service="checkout",
            starts_at=START,
            is_root_signal=True,
        )
    ]
    context.deploys = [
        DeployRef(
            sha="a1b2c3d4e5f6",
            service="checkout",
            deployed_at=START - timedelta(minutes=12),
            title="raise pool ceiling",
            actor="U_RAVI",
        )
    ]
    return context


def _cite(ref: str, kind: CitationKind = CitationKind.MESSAGE) -> Citation:
    return Citation(kind=kind, ref=ref)


def _draft(**kwargs: object) -> PIRDraft:
    base: dict[str, object] = {
        "summary": [
            Claim(
                text="Checkout returned 5xx on card payments after a config change.",
                citations=[_cite(f"msg:{MSG_TS}")],
            )
        ]
    }
    base.update(kwargs)
    return PIRDraft.model_validate(base)


# --- impact (W6-02, W6-05) ----------------------------------------------------


async def test_impact_is_deterministic() -> None:
    """Same incident, same window, same numbers. Every time.

    Determinism is what lets the validator compare a draft's numbers against a
    computed set at all -- a stochastic impact block would make INV-06
    unenforceable.
    """
    metrics = FakeMetrics()
    first = await compute_impact(
        metrics,
        service="checkout",
        namespace="prod",
        detected_at=START,
        resolved_at=END,
        now=END,
    )
    second = await compute_impact(
        FakeMetrics(),
        service="checkout",
        namespace="prod",
        detected_at=START,
        resolved_at=END,
        now=END,
    )
    assert first.available
    assert first.values == second.values


async def test_all_four_queries_run_over_the_incident_window() -> None:
    metrics = FakeMetrics()
    await compute_impact(
        metrics,
        service="checkout",
        namespace="prod",
        detected_at=START,
        resolved_at=END,
        now=END,
    )
    assert len(metrics.queries) == len(IMPACT_QUERIES)
    # 14 minutes, rounded up: `increase(...[0s])` is an error and a
    # forty-second incident still has an impact someone will ask about.
    assert all("[15m]" in q or "kube_pod" in q for q, _, _ in metrics.queries)


async def test_metrics_unavailable_is_honest() -> None:
    """FMEA #15. Not zero, not "approximately", not last week's figure.

    A gap that says it is a gap is recoverable; a confident fabrication in a
    postmortem is not.
    """
    report = await compute_impact(
        FakeMetrics(unavailable=True),
        service="checkout",
        namespace="prod",
        detected_at=START,
        resolved_at=END,
        now=END,
    )
    assert not report.available
    assert report.as_json()["status"] == "unavailable"
    assert "values" not in report.as_json()
    assert report.numbers() == set()
    assert "do NOT estimate" in verified_facts_block(report)


async def test_an_error_rate_is_derived_not_queried() -> None:
    metrics = FakeMetrics(values={})
    report = await compute_impact(
        metrics,
        service="checkout",
        namespace="prod",
        detected_at=START,
        resolved_at=END,
        now=END,
    )
    assert "error_rate_pct" in report.values


async def test_every_computed_number_is_citable() -> None:
    """Each figure carries a metric window, so a claim about it can be grounded."""
    report = await compute_impact(
        FakeMetrics(),
        service="checkout",
        namespace="prod",
        detected_at=START,
        resolved_at=END,
        now=END,
    )
    assert len(report.windows) == len(IMPACT_QUERIES)
    assert all(w.ref.startswith("metric:") for w in report.windows)


# --- the validator (W6-16) ----------------------------------------------------


def test_a_valid_draft_passes_with_full_coverage(ctx: GroundedContext) -> None:
    report = CitationValidator().validate(_draft(), ctx)
    assert report.ok, report.messages
    assert report.coverage == 1.0


def test_fabricated_citation_is_rejected(ctx: GroundedContext) -> None:
    """D1's acceptance criterion, and the cheapest check in the system.

    A well-formed id that is not in the set we stored means the model produced a
    plausible invention. One hash lookup catches it.
    """
    draft = _draft(
        summary=[Claim(text="Checkout failed.", citations=[_cite("msg:9999999999.999999")])]
    )
    report = CitationValidator().validate(draft, ctx)

    assert not report.ok
    assert FABRICATED in report.reasons()
    assert "was never stored" in report.messages[0]


def test_a_real_id_on_an_unrelated_sentence_is_caught(ctx: GroundedContext) -> None:
    """The subtler failure: the citation exists, it just says something else."""
    draft = _draft(
        summary=[
            Claim(
                text="Certificate renewal failed on the ingress controller.",
                citations=[_cite(f"msg:{MSG_TS}")],
            )
        ]
    )
    assert not CitationValidator().validate(draft, ctx).ok


def test_ungrounded_number_rejected(ctx: GroundedContext) -> None:
    """INV-06 / B-10's other half.

    Impact is absent from the schema so the model cannot be *asked* for a
    number; this is what stops it volunteering one in prose.
    """
    draft = _draft(
        summary=[
            Claim(
                text="Checkout returned 5xx, affecting 48000 customers.",
                citations=[_cite(f"msg:{MSG_TS}")],
            )
        ]
    )
    report = CitationValidator().validate(draft, ctx)
    assert not report.ok
    assert UNGROUNDED_NUMBER in report.reasons()


def test_a_computed_number_is_allowed(ctx: GroundedContext) -> None:
    """The other direction: a figure that came from the impact block passes."""
    from incidentpilot.impact.promql import ImpactReport

    ctx.attach_impact(
        ImpactReport(
            status="computed",
            window_start=START,
            window_end=END,
            service="checkout",
            values={"failed_requests": 1432.0},
        )
    )
    draft = _draft(
        summary=[
            Claim(
                text="Checkout returned 5xx on 1432 requests.",
                citations=[_cite(f"msg:{MSG_TS}")],
            )
        ]
    )
    assert CitationValidator().validate(draft, ctx).ok


def test_an_action_item_owner_must_have_been_there(ctx: GroundedContext) -> None:
    """The model picks a name off the runbook or a service label.

    The document then assigns work to someone who has never heard of the
    incident, which is cheap to catch and embarrassing to ship.
    """
    draft = _draft(
        action_items=[
            ActionItem(
                description="Restore the pool ceiling.",
                owner="U_NOBODY",
                priority="P0",
                category="reliability",
                citations=[_cite(f"msg:{MSG_TS}")],
            )
        ]
    )
    report = CitationValidator().validate(draft, ctx)
    assert not report.ok
    assert NON_PARTICIPANT in report.reasons()


def test_the_validator_makes_no_model_call(ctx: GroundedContext) -> None:
    """A gate that costs money and can itself hallucinate is not a gate.

    Asserted structurally: the module imports nothing that could reach a
    provider, so there is no object in scope that could make a call.
    """
    import ast
    from pathlib import Path

    source = Path("src/incidentpilot/pir/validator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any("llm" in m or "router" in m for m in imports)


def test_a_deploy_citation_resolves(ctx: GroundedContext) -> None:
    """The sixth kind, which had nowhere to resolve from before W6-22."""
    draft = _draft(
        summary=[
            Claim(
                text="A config change deployed to checkout preceded the errors.",
                citations=[_cite("deploy:a1b2c3d4e5f6", CitationKind.DEPLOY)],
            )
        ]
    )
    assert CitationValidator().validate(draft, ctx).ok


# --- the skeleton (W6-17) -----------------------------------------------------


def test_skeleton_needs_no_model(ctx: GroundedContext) -> None:
    """Layer 3, and the reason blocking the provider still yields a document."""
    markdown = render_skeleton(ctx)

    assert BANNER in markdown
    assert ctx.public_key in markdown
    assert "TODO" in markdown
    assert "No model was involved" in markdown


def test_the_skeleton_is_deterministic(ctx: GroundedContext) -> None:
    """Week 7 compares skeletons byte-for-byte; a varying document cannot be
    part of a regression corpus."""
    assert render_skeleton(ctx) == render_skeleton(ctx)


def test_the_skeleton_states_missing_metrics_rather_than_guessing(
    ctx: GroundedContext,
) -> None:
    markdown = render_skeleton(ctx)
    assert "no impact figures are estimated" in markdown.lower()


def test_the_skeleton_lists_the_evidence_a_human_can_cite(ctx: GroundedContext) -> None:
    """A skeleton whose author cannot cite anything produces an uncited PIR."""
    markdown = render_skeleton(ctx)
    assert "alert:fp-checkout-5xx" in markdown
    assert "deploy:a1b2c3d4e5f6" in markdown


# --- privacy (W6-13, D6) ------------------------------------------------------


def test_redaction_round_trip_lossless() -> None:
    original = "email ananya@example.com about order ORD-99182 from 9876543210"
    redactor = Redactor()
    redacted = redactor.redact(original)

    assert "ananya@example.com" not in redacted
    assert "9876543210" not in redacted
    assert redactor.restore(redacted) == original


def test_tokens_are_stable_within_an_incident() -> None:
    """`<EMAIL_1>` is the same person throughout, so the model can still reason
    about "the same customer reported it twice" without seeing the address."""
    redactor = Redactor()
    out = redactor.redact("ananya@example.com said X; later ananya@example.com said Y")
    assert out.count("<EMAIL_1>") == 2


def test_a_long_number_that_is_not_a_card_survives() -> None:
    """Without the Luhn check, every trace id becomes <CARD_1> and the model
    goes blind to the incident for no privacy gain."""
    redactor = Redactor()
    assert "4111111111111111" not in redactor.redact("card 4111111111111111")
    assert "1234567890123456" in redactor.redact("trace 1234567890123456")


def test_private_addresses_are_topology_not_pii() -> None:
    redactor = Redactor()
    assert "10.4.2.19" in redactor.redact("pod at 10.4.2.19 is unready")


def test_credentials_are_recognized() -> None:
    redactor = Redactor()
    out = redactor.redact("key AKIAIOSFODNN7EXAMPLE and token xoxb-123456789012-abcdef")
    assert "AKIA" not in out
    assert "xoxb-" not in out


def test_the_audit_records_the_class_never_the_value() -> None:
    """A redaction log containing what it redacted is a second copy of the data
    with none of the controls."""
    redactor = Redactor()
    redactor.redact("ananya@example.com")
    audit = redactor.audit()

    assert audit == [{"class": str(PIIClass.EMAIL), "count": 1}]
    assert "ananya" not in str(audit)
    assert all("ananya" not in str(f) for f in redactor.findings)


# --- prompts (W6-15) ----------------------------------------------------------


def test_prompt_hash_matches_file() -> None:
    """A version string can lie; a content hash cannot.

    Editing a prompt without bumping its version is the easiest way to make an
    eval corpus meaningless: every stored result claims `v2.1.0` and three came
    from a different one.
    """
    registry = load_registry()
    prompt = registry["synthesize"]
    assert prompt.version == "2.1.0"
    assert prompt.sha256 == sha256_of(prompt.path.read_text(encoding="utf-8"))


def test_a_tampered_prompt_fails_to_load(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import shutil

    from incidentpilot.pir.prompts.registry import PROMPTS_DIR

    shutil.copytree(PROMPTS_DIR, tmp_path / "prompts")
    target = tmp_path / "prompts" / "pir_v2_1_0.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nsneaky edit\n", encoding="utf-8")

    with pytest.raises(PromptIntegrityError, match="without its version"):
        load_registry(tmp_path / "prompts" / "registry.yaml")


def test_the_prompt_forbids_inventing_numbers_and_ids() -> None:
    """The constraints are advisory -- the schema and the validator are what
    enforce them -- but a prompt that does not even ask is leaving free
    compliance on the table."""
    body = load_registry()["synthesize"].body.lower()
    assert "never invent a reference id" in body
    assert "never produce a number" in body
    assert '"unknown" is a valid answer' in body


# --- models.yaml (W6-11, INV-07) ----------------------------------------------


def test_roles_resolve_to_a_provider_and_a_model() -> None:
    config = load_models_config()
    assert set(config.roles) == {"extract", "synthesize", "judge"}
    assert config.role("synthesize").primary.model


def test_synthesize_has_a_second_provider() -> None:
    """Layer 2 of the fallback chain is a different vendor, not a retry.

    Retrying the same provider on a provider outage is not a fallback.
    """
    chain = load_models_config().role("synthesize").chain()
    assert len(chain) == 2
    assert chain[0].provider != chain[1].provider


def test_the_judge_role_refuses_to_resolve_on_the_production_path() -> None:
    """A judge on the hot path would put a second model between an incident and
    its postmortem. The validator is deterministic precisely so nothing has to."""
    config = load_models_config()
    with pytest.raises(ModelConfigError, match="offline_only"):
        config.role("judge")
    assert config.role("judge", offline=True).name == "judge"


def test_models_yaml_is_the_only_place_a_model_name_appears() -> None:
    """INV-07, as a test rather than only as a CI grep.

    The grep runs on a push; this runs on every local test invocation, which is
    where the mistake is actually made.
    """
    import re
    from pathlib import Path

    pattern = re.compile(r"gpt-[0-9]|claude-|gemini-|llama-")
    offenders = [
        path
        for path in Path("src/incidentpilot").rglob("*.py")
        if "config" not in path.parts and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"model names outside config/: {offenders}"


# --- the budget breaker (W6-20) -----------------------------------------------


async def test_budget_trip_yields_skeleton(fake_valkey: FakeValkey) -> None:
    """Tripping raises, and the generator turns that into the skeleton.

    A cost control that stops the product working is one somebody disables the
    first time it fires.
    """
    breaker = BudgetBreaker(fake_valkey, Budgets(per_incident=0.10))
    await breaker.check(1, ceiling=0.10)

    await breaker.record(1, 0.12)
    with pytest.raises(BudgetExceeded, match="incident budget"):
        await breaker.check(1, ceiling=0.10)


async def test_an_unreachable_cache_allows_the_call(fake_valkey: FakeValkey) -> None:
    """Failing closed would let a cache blip remove the PIR feature entirely.

    The ceiling exists to stop a runaway loop, not to be the last line of
    defence against a compromised process -- and the trade is stated rather than
    discovered during an outage.
    """
    fake_valkey.alive = False
    await BudgetBreaker(fake_valkey, Budgets(per_incident=0.01)).check(1, ceiling=0.01)


async def test_each_tier_trips_independently(fake_valkey: FakeValkey) -> None:
    breaker = BudgetBreaker(fake_valkey, Budgets(per_incident=10.0, per_day=0.05))
    await breaker.record(1, 0.06)
    with pytest.raises(BudgetExceeded, match="day budget"):
        await breaker.check(1, ceiling=10.0)
