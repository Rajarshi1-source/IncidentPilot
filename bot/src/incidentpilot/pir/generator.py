"""The three-layer chain, always terminating locally (W6-18, D1).

| Layer | Trigger | Output |
|---|---|---|
| 1 — primary | normal | full narrative, every section cited |
| 2 — secondary | provider failure, or 2 validation failures | same prompt, second provider |
| 3 — skeleton | budget exceeded, all providers failed, or 3 failures | deterministic markdown |

**Impact is computed first, before any model call.** That ordering is the
feature. The numbers go into the prompt as verified facts, `impact` is absent
from ``PIRDraft`` so the model cannot be asked for one, and the validator
rejects any number in the draft that is not in the computed set (B-10, INV-06).

**Nothing is published until it validates.** The D1 criterion is "a fabricated
ID rejects, retries, then degrades — *no partial publication*", so the document
is persisted exactly once, at the end, when there is something to persist. A
generator that wrote as it went would leave half a PIR on a rejected attempt.

**It always terminates in something local.** Every escape from the loop lands on
the skeleton, which needs no key and no network -- which is why blocking the
provider at the network level still yields a posted document (G6).
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.adapters.llm.base import (
    AllProvidersFailed,
    BudgetExceeded,
    Completion,
    ProviderError,
)
from incidentpilot.impact.promql import ImpactReport, compute_impact, unavailable
from incidentpilot.pir.context import GroundedContext, build_grounded_context
from incidentpilot.pir.prompts.registry import Prompt, get_prompt
from incidentpilot.pir.schema import PIRDraft
from incidentpilot.pir.skeleton import render_skeleton
from incidentpilot.pir.validator import CitationValidator, ValidationReport
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import (
    PIR_CITATION_COV,
    PIR_GENERATION,
    VALIDATION_FAILURES,
)

log = get_logger(__name__)

LAYER_PRIMARY = "llm_primary"
LAYER_SECONDARY = "llm_secondary"
LAYER_SKELETON = "skeleton"

# Two model attempts, then the skeleton. Three is not better: the third attempt
# costs another sixty seconds against a 90-second gate, and a model that has
# fabricated twice on the same evidence will fabricate again.
MAX_MODEL_ATTEMPTS = 2

SYSTEM = (
    "You produce grounded post-incident reviews. Every claim carries a citation "
    "from the supplied evidence. You never invent reference IDs and you never "
    "produce a number that is not in the verified facts block."
)


@dataclass(frozen=True, slots=True)
class PIRResult:
    incident_id: int
    layer: str
    markdown: str
    draft: PIRDraft | None
    validation: ValidationReport | None
    impact: ImpactReport
    completion: Completion | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    generation_ms: int = 0

    @property
    def coverage(self) -> float:
        """1.000 on a published model draft; 0.0 on the skeleton.

        The skeleton reports 0.0 rather than 1.0 on purpose: it makes no cited
        claims, and recording perfect coverage for a document with no claims
        would make the SLI look healthy on exactly the days it is not.
        """
        return self.validation.coverage if self.validation is not None else 0.0

    @property
    def is_skeleton(self) -> bool:
        return self.layer == LAYER_SKELETON


class PIRGenerator:
    """Context, impact, then at most two model attempts, then the skeleton."""

    def __init__(
        self,
        router: Any,
        *,
        metrics: Any = None,
        validator: CitationValidator | None = None,
        prompt: Prompt | None = None,
        now: Any = None,
        context_loader: Any = None,
    ) -> None:
        self._router = router
        self._metrics = metrics
        self._validator = validator or CitationValidator()
        self._prompt = prompt
        self._now = now or (lambda: datetime.now(UTC))
        # The one seam the replay harness needs (W7-05). Production loads the
        # context from six queries; the harness assembles the identical object
        # from a recording, because a Postgres connection is a socket and INV-10
        # forbids one. A constructor argument rather than a subclass or a
        # patched module, for the same reason ``now`` is: the injected shape is
        # the shape the tests already exercise, so the seam is not a special
        # path that only the harness walks.
        self._context_loader = context_loader

    def prompt(self) -> Prompt:
        if self._prompt is None:
            self._prompt = get_prompt("synthesize")
        return self._prompt

    async def generate(self, session: AsyncSession | None, incident_id: int) -> PIRResult:
        started = time.perf_counter()

        # `session` is None only when a context_loader was injected -- the
        # harness assembles the context from a recording and never touches a
        # database. The default loader needs a real session, so the two are
        # checked together rather than trusting the caller to pair them.
        if self._context_loader is None:
            if session is None:
                raise ValueError(
                    "generate() needs a session, or a context_loader that does not use one"
                )
            ctx = await build_grounded_context(session, incident_id)
        else:
            loaded = self._context_loader(session, incident_id)
            ctx = await loaded if inspect.isawaitable(loaded) else loaded
        impact = await self._impact(ctx)
        ctx.attach_impact(impact)

        prompt = self.prompt()
        rendered = prompt.render(
            verified_facts=_facts(impact),
            incident_header=_header(ctx),
            evidence=_evidence(ctx),
        )

        failures = 0
        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            layer = LAYER_PRIMARY if attempt == 1 else LAYER_SECONDARY
            try:
                completion = await self._router.complete(
                    role="synthesize",
                    system=SYSTEM,
                    user=rendered,
                    schema=PIRDraft,
                    incident_id=incident_id,
                )
            except BudgetExceeded as exc:
                # Not an error path worth retrying: the ceiling will still be
                # reached on the next attempt, and every attempt costs the gate
                # another sixty seconds.
                log.warning("pir.budget_exceeded", incident_id=incident_id, error=str(exc))
                break
            except (AllProvidersFailed, ProviderError) as exc:
                log.warning("pir.layer_failed", layer=layer, error=str(exc)[:200])
                failures += 1
                continue

            report = self._validator.validate(completion.parsed, ctx)
            if report.ok:
                elapsed = int((time.perf_counter() - started) * 1000)
                PIR_GENERATION.labels(layer=layer, outcome="ok").observe(elapsed / 1000)
                PIR_CITATION_COV.set(report.coverage)
                log.info(
                    "pir.generated",
                    incident_id=incident_id,
                    layer=layer,
                    coverage=report.coverage,
                    claims=report.checked_claims,
                    generation_ms=elapsed,
                )
                return PIRResult(
                    incident_id=incident_id,
                    layer=layer,
                    markdown=render_markdown(ctx, completion.parsed, impact),
                    draft=completion.parsed,
                    validation=report,
                    impact=impact,
                    completion=completion,
                    prompt_version=prompt.version,
                    prompt_sha256=prompt.sha256,
                    generation_ms=elapsed,
                )

            failures += 1
            for reason in sorted(report.reasons()):
                VALIDATION_FAILURES.labels(layer=layer, reason=reason).inc()
            log.warning(
                "pir.validation_rejected",
                incident_id=incident_id,
                layer=layer,
                attempt=attempt,
                errors=report.messages[:5],
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        PIR_GENERATION.labels(layer=LAYER_SKELETON, outcome="degraded").observe(elapsed / 1000)
        # Deliberately NOT 1.0. The skeleton makes no cited claims, and
        # reporting perfect coverage for it would make the zero-error-budget SLI
        # look healthy on exactly the days it is not.
        PIR_CITATION_COV.set(0.0)
        log.warning(
            "pir.degraded_to_skeleton",
            incident_id=incident_id,
            failures=failures,
            generation_ms=elapsed,
        )
        return PIRResult(
            incident_id=incident_id,
            layer=LAYER_SKELETON,
            markdown=render_skeleton(ctx, impact),
            draft=None,
            validation=None,
            impact=impact,
            generation_ms=elapsed,
        )

    async def _impact(self, ctx: GroundedContext) -> ImpactReport:
        """Computed before the model call, or honestly absent (B-10, FMEA #15)."""
        now = self._now()
        if self._metrics is None:
            return unavailable(
                ctx.detected_at,
                ctx.resolved_at or now,
                service=ctx.service,
                reason="no metrics adapter configured",
            )
        return await compute_impact(
            self._metrics,
            service=ctx.service,
            namespace=None,
            detected_at=ctx.detected_at,
            resolved_at=ctx.resolved_at,
            now=now,
        )


# --- prompt assembly ----------------------------------------------------------


def _facts(impact: ImpactReport) -> str:
    from incidentpilot.impact.promql import verified_facts_block

    return verified_facts_block(impact)


def _header(ctx: GroundedContext) -> str:
    root = ctx.root_signal_alert
    lines = [
        f"{ctx.public_key} — {ctx.title}",
        f"severity: {ctx.severity}",
        f"service: {ctx.service or 'unknown'}",
        f"detected: {ctx.detected_at.isoformat()}",
        f"resolved: {ctx.resolved_at.isoformat() if ctx.resolved_at else 'not yet'}",
    ]
    if root is not None:
        lines.append(f"root signal: {root.alertname} on {root.service or 'unknown'}")
    if ctx.runbook_name:
        lines.append(f"runbook pinned: {ctx.runbook_name}")
    return "\n".join(lines)


def _evidence(ctx: GroundedContext) -> str:
    """The corpus, every line prefixed with the ID that cites it.

    Format matters more than it looks: the model copies what it sees, so an ID
    that sits at the start of the line and is visually separated is an ID that
    gets reproduced correctly. IDs buried mid-sentence get paraphrased.
    """
    from incidentpilot.domain.evidence import (
        alert_ref,
        deploy_ref,
        message_ref,
        runbook_ref,
        timeline_ref,
    )

    lines: list[str] = []

    if ctx.alerts:
        lines.append("## Alerts")
        for alert in ctx.alerts:
            flag = " [ROOT SIGNAL]" if alert.is_root_signal else ""
            lines.append(
                f"{alert_ref(alert.fingerprint)} | {alert.starts_at.isoformat()} | "
                f"{alert.alertname} on {alert.service or 'unknown'}{flag}"
            )

    if ctx.deploys:
        lines.append("\n## Deploys near this incident")
        for deploy in ctx.deploys:
            lines.append(
                f"{deploy_ref(deploy.sha)} | {deploy.deployed_at.isoformat()} | "
                f"{deploy.service} | {deploy.title or ''}"
            )

    if ctx.timeline:
        lines.append("\n## Timeline (classified events)")
        for entry in ctx.timeline:
            lines.append(
                f"{timeline_ref(entry.id)} | {entry.at.isoformat()} | {entry.intent} | "
                f"{entry.description}"
            )

    if ctx.runbook_steps:
        lines.append("\n## Runbook steps recorded")
        for step in ctx.runbook_steps:
            lines.append(
                f"{runbook_ref(step.runbook_id, step.step_id)} | {step.step_id} "
                f"(detected by {step.detected_by})"
            )

    if ctx.impact is not None and ctx.impact.available:
        lines.append("\n## Metric windows (cite these for impact statements)")
        for window in ctx.impact.windows:
            lines.append(f"{window.ref} | {window.key} = {window.value}")

    lines.append("\n## Transcript")
    for message in ctx.messages:
        who = message.user_id or "unknown"
        lines.append(f"{message_ref(message.ts)} | {who} | {message.text}")

    return "\n".join(lines)


# --- rendering ----------------------------------------------------------------


def render_markdown(ctx: GroundedContext, draft: PIRDraft, impact: ImpactReport) -> str:
    """The validated draft as markdown, citations intact.

    Block Kit rendering with clickable chips lives in ``pir/renderer.py``; this
    is the plain form that goes into ``pir_documents.rendered_markdown`` and
    into the dashboard.
    """
    from incidentpilot.pir.renderer import render_pir_markdown

    return render_pir_markdown(ctx, draft, impact)


# --- persistence --------------------------------------------------------------

_INSERT_PIR = sql(
    """
    INSERT INTO pir_documents
        (incident_id, revision, generation_layer, body, rendered_markdown,
         provider, model, prompt_version, prompt_sha256,
         input_tokens, output_tokens, cost_usd, generation_ms,
         citation_coverage, validation_passed, validation_errors)
    VALUES
        (:incident_id,
         (SELECT coalesce(max(revision), 0) + 1 FROM pir_documents
           WHERE incident_id = :incident_id),
         :layer, CAST(:body AS jsonb), :markdown,
         :provider, :model, :prompt_version, :prompt_sha256,
         :input_tokens, :output_tokens, :cost_usd, :generation_ms,
         :coverage, :passed, CAST(:errors AS jsonb))
    RETURNING id, revision
    """
)


async def persist(session: AsyncSession, result: PIRResult) -> tuple[int, int]:
    """Store the document. Called once, after the outcome is decided.

    The revision number is computed inside the INSERT, so two concurrent
    generations cannot both write revision 1 -- ``uq_pir_revision`` would reject
    the loser, which is the correct outcome and not one the caller has to
    remember to handle.
    """
    import json

    completion = result.completion
    row = (
        await session.execute(
            _INSERT_PIR,
            {
                "incident_id": result.incident_id,
                "layer": result.layer,
                "body": json.dumps(
                    result.draft.model_dump(mode="json") if result.draft else {}, default=str
                ),
                "markdown": result.markdown,
                "provider": completion.provider if completion else None,
                "model": completion.model if completion else None,
                "prompt_version": result.prompt_version,
                "prompt_sha256": result.prompt_sha256,
                "input_tokens": completion.input_tokens if completion else None,
                "output_tokens": completion.output_tokens if completion else None,
                "cost_usd": completion.cost_usd if completion else None,
                "generation_ms": result.generation_ms,
                "coverage": result.coverage,
                "passed": result.validation.ok if result.validation else False,
                "errors": json.dumps(result.validation.as_json() if result.validation else []),
            },
        )
    ).one()
    return int(row.id), int(row.revision)
