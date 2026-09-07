"""Role in, completion out (W6-10, INV-07).

The router is where four separate guarantees meet, and the **order** is the
design:

1. **Budget first.** Checked before anything is rendered or sent, so a tripped
   budget costs nothing at all (W6-20).
2. **Redaction second.** The last thing that happens to the text before it can
   leave the process (D6). Putting it here rather than in each provider means a
   new provider cannot forget it -- there is one egress, and it is redacted.
3. **The chain third.** Primary, then secondary, and the loop is what turns "one
   vendor is having an afternoon" into a slower PIR rather than no PIR.
4. **Telemetry always.** Every attempt increments a counter with its outcome, so
   "we fell back" is visible rather than inferred from latency.

Layer 3 -- the skeleton -- is deliberately **not** in this loop. It is not a
provider, it needs no key and no network, and modelling it as one would make the
honest floor of the product look like a vendor that always succeeds.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from incidentpilot.adapters.llm.base import (
    AllProvidersFailed,
    Completion,
    ProviderError,
)
from incidentpilot.config.models_config import ModelsConfig, RoleSpec
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import LLM_CALLS, LLM_COST

log = get_logger(__name__)


class LLMRouter:
    """Resolves a role to a chain and runs it."""

    def __init__(
        self,
        config: ModelsConfig,
        providers: dict[str, Any],
        *,
        budget: Any | None = None,
        redactor: Any | None = None,
        offline: bool = False,
    ) -> None:
        self._config = config
        self._providers = providers
        self._budget = budget
        self._redactor = redactor
        # Set only by the eval harness. It is what lets `judge` resolve at all,
        # and it is a constructor argument rather than an environment variable
        # so that a production process cannot acquire it by accident.
        self._offline = offline

    def role(self, name: str) -> RoleSpec:
        return self._config.role(name, offline=self._offline)

    async def complete(
        self,
        *,
        role: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        incident_id: int,
    ) -> Completion:
        spec = self.role(role)

        if self._budget is not None:
            # Raises BudgetExceeded, which the generator turns into a skeleton.
            # Checked before rendering or sending anything: a tripped budget
            # should cost nothing, not one wasted call.
            #
            # `await` is load-bearing. Without it this builds a coroutine,
            # discards it, and proceeds -- the breaker never fires and every
            # direct unit test of it still passes. An integration test that
            # asserted "zero calls were made" is what caught it.
            await self._budget.check(incident_id, role=role, ceiling=spec.budget_usd_per_incident)

        if self._redactor is not None:
            # The egress boundary (D6). One place, so a new provider cannot
            # forget it.
            system = self._redactor.redact(system)
            user = self._redactor.redact(user)

        last: Exception | None = None
        for rung in spec.chain():
            provider = self._providers.get(rung.provider)
            if provider is None:
                log.warning("llm.provider_missing", role=role, provider=rung.provider)
                last = ProviderError(f"provider {rung.provider!r} is not configured")
                continue

            try:
                completion: Completion = await provider.complete_structured(
                    role=role,
                    model=rung.model,
                    system=system,
                    user=user,
                    schema=schema,
                    max_output_tokens=rung.max_output_tokens,
                    temperature=rung.temperature,
                    timeout_s=rung.timeout_s,
                )
            except ProviderError as exc:
                last = exc
                LLM_CALLS.labels(role=role, provider=rung.provider, outcome="error").inc()
                log.warning(
                    "llm.attempt_failed",
                    role=role,
                    provider=rung.provider,
                    error=str(exc)[:200],
                )
                continue

            if self._budget is not None:
                await self._budget.record(incident_id, completion.cost_usd)
            LLM_CALLS.labels(role=role, provider=rung.provider, outcome="ok").inc()
            LLM_COST.labels(role=role, provider=completion.provider, model=completion.model).inc(
                completion.cost_usd
            )
            log.info(
                "llm.completed",
                role=role,
                provider=completion.provider,
                latency_ms=completion.latency_ms,
                cost_usd=round(completion.cost_usd, 6),
                # Recorded, never branched on (B-09).
                reported_confidence=completion.reported_confidence,
            )
            return completion

        raise AllProvidersFailed(f"every provider failed for role {role!r}") from last


def build_providers(config: ModelsConfig, *, offline: bool = False) -> dict[str, Any]:
    """Construct the providers named in ``models.yaml``.

    ``offline`` forces every role onto the fake, which is what the replay
    harness passes: INV-10 requires the corpus to run with zero network calls,
    and the surest way to guarantee that is for no HTTP client to exist.
    """
    from incidentpilot.adapters.llm.fake import FakeLLM

    providers: dict[str, Any] = {"fake": FakeLLM()}
    if offline:
        return providers

    import os

    from incidentpilot.adapters.llm.http_provider import (
        AnthropicProvider,
        LocalProvider,
        OpenAICompatibleProvider,
    )

    kinds = {
        "openai": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
        "local": LocalProvider,
    }
    for name, spec in config.providers.items():
        kind = str(spec.get("kind") or name)
        cls = kinds.get(kind)
        if cls is None:
            continue
        key_env = str(spec.get("api_key_env") or "")
        providers[name] = cls(
            base_url=str(spec.get("base_url") or ""),
            api_key=os.environ.get(key_env) if key_env else None,
        )
    return providers
