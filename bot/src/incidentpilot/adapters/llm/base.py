"""The model boundary (W6-10, INV-07).

**No model name appears in this file, or in any file under ``src/`` outside
``config/``.** The system speaks in *roles* -- `extract`, `synthesize`, `judge` --
and the router resolves a role to a provider and a model from
``config/models.yaml``. A CI grep fails the build on any vendor or model string
elsewhere.

That is not vendor-neutrality theatre. A model name in code means a price change
is a code change, a deprecation is a code change, and the week 7 experiment that
settles E-1 -- replay the corpus with `--set roles.synthesize.model=<candidate>`
-- would be a diff instead of a flag. The roles are the interface; the names are
data.

``complete_structured`` returns *parsed* output, not text. Every provider is
responsible for getting JSON that matches the schema out of its own API, because
each does it differently and pushing that upward would put three vendors' quirks
into the generator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Completion:
    """One model response, with everything an audit needs.

    ``raw_text`` is kept alongside the parsed object because a validation
    failure is about what the model *said*, and a report that can only show the
    parsed form cannot explain a parse failure at all.
    """

    parsed: Any
    raw_text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    # Recorded, never branched on (B-09). Self-reported confidence correlates
    # with fluency rather than correctness; gating on it is how a fluent
    # fabrication passes and a hedged truth fails.
    reported_confidence: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """Any failure from a model provider."""


class RetryableProviderError(ProviderError):
    """A 429, a 5xx, a timeout. The chain moves to the next provider."""


class PermanentProviderError(ProviderError):
    """A 4xx, a refused request, an unparseable body after retries."""


class BudgetExceeded(Exception):
    """Spend ceiling reached. Degrades to the skeleton; never blocks."""


class AllProvidersFailed(ProviderError):
    """Every provider in the chain for this role failed."""


@runtime_checkable
class LLMProvider(Protocol):
    """What a provider must do. Deliberately one method wide."""

    name: str

    async def complete_structured(
        self,
        *,
        role: str,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_output_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> Completion: ...


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> float:
    """Cost in USD from token counts and the rates in ``models.yaml``.

    The rates are configuration rather than constants for the same reason the
    model names are: prices change more often than code should.
    """
    return (input_tokens * input_usd_per_mtok + output_tokens * output_usd_per_mtok) / 1_000_000
