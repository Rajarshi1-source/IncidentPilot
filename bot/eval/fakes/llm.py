"""Replay model provider (W7-04, INV-10).

Returns the response the model actually gave, for the role that asked. It is the
component that makes a 40-incident corpus score in seconds instead of dollars,
and it is the one that has to be honest about what replay can and cannot
measure.

**What it measures.** Everything downstream of the model: the validator, the
citation grammar, the fallback ladder, the impact injection, the timeline
assembly, the storm compression. Those are the parts that fail silently and the
parts a prompt change can break structurally.

**What it cannot measure.** Whether a *different prompt* would have produced a
better answer. Replay returns recorded output; the prompt is an input to a
process that already ran. So the harness does not pretend otherwise: it detects
that the prompt no longer matches the one behind the recording and fails the
gate on **drift**, with both hashes named. Claiming a replayed F1 responds to a
prompt edit would be the same class of lie as a gate that never runs.

Cost is recomputed from the recorded token counts against the *current* rates in
``models.yaml``, which is what makes ``--set`` a real counterfactual for spend.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from eval.recorder import KIND_LLM, Entry, Recording
from incidentpilot.adapters.llm.base import (
    Completion,
    PermanentProviderError,
    RetryableProviderError,
    estimate_cost,
)


class ReplayLLM:
    """One queue of recorded responses per role, consumed in recorded order."""

    name = "replay"

    def __init__(self, recording: Recording) -> None:
        self._by_role: dict[str, list[Entry]] = {}
        for entry in recording.of_kind(KIND_LLM):
            self._by_role.setdefault(str(entry.get("role") or "synthesize"), []).append(entry)
        self._cursor: dict[str, int] = {}
        self.calls = 0
        self.total_cost_usd = 0.0
        # The latency production actually paid, summed across every attempt --
        # successes and failures alike, because a fixture that burned a 60s
        # timeout before falling through spent that time. Replay finishes in
        # microseconds, so measuring the harness's own wall clock and calling it
        # `p95_generation_ms` would report a 3 ms p95 against a 30 s budget: a
        # number that is both perfectly green and completely uninformative.
        self.total_latency_ms = 0
        self._in_rate = 0.0
        self._out_rate = 0.0
        self.payloads: list[tuple[str, str]] = []
        self.recorded_prompt_hashes: list[str] = []

    def responses_for(self, role: str) -> int:
        return len(self._by_role.get(role, ()))

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
    ) -> Completion:
        # Recorded before any failure injection: a provider that raises still
        # received the payload, and that is exactly the case where "did PII
        # leave?" matters most.
        self.payloads.append((system, user))
        self.calls += 1

        queue = self._by_role.get(role, [])
        index = self._cursor.get(role, 0)
        if index >= len(queue):
            raise PermanentProviderError(
                f"the recording has no further {role!r} response. Either the generator "
                "made more attempts than it did in production, or the recording is truncated."
            )
        self._cursor[role] = index + 1
        entry = queue[index]

        recorded_hash = str(entry.get("prompt_sha256") or "")
        if recorded_hash:
            self.recorded_prompt_hashes.append(recorded_hash)

        response = entry.response
        self.total_latency_ms += int(response.get("latency_ms") or 0)
        error = response.get("error")
        if error:
            # The fallback-ladder bucket. `retryable` decides whether the chain
            # moves on or gives up, and the recording says which happened.
            kind = (
                RetryableProviderError
                if bool(response.get("retryable", True))
                else PermanentProviderError
            )
            raise kind(str(error))

        raw = _content(response)
        try:
            parsed = schema.model_validate_json(raw)
        except ValidationError as exc:
            # Permanent, deliberately. Output that does not match the schema
            # will not match it on retry, and the generator's job is to move
            # down the chain rather than spend the budget again. The
            # invalid-JSON fixtures ride this path.
            raise PermanentProviderError(f"recorded response does not match schema: {exc}") from exc

        input_tokens = int(response.get("input_tokens") or len(user) // 4)
        output_tokens = int(response.get("output_tokens") or len(raw) // 4)
        cost = estimate_cost(
            input_tokens,
            output_tokens,
            input_usd_per_mtok=self._in_rate,
            output_usd_per_mtok=self._out_rate,
        )
        self.total_cost_usd += cost

        return Completion(
            parsed=parsed,
            raw_text=raw,
            provider=self.name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=int(response.get("latency_ms") or 0),
            reported_confidence=response.get("confidence"),
        )

    def set_rates(self, *, input_usd_per_mtok: float, output_usd_per_mtok: float) -> None:
        """Price the recorded tokens at the configured rate.

        Set by the replayer from the resolved role spec, so ``--set`` changing a
        model (and with it the rates in ``models.yaml``) moves ``cost_per_pir_usd``
        for real. Quality does not move -- see the module docstring -- and the
        report says so rather than letting a reader assume otherwise.
        """
        self._in_rate = input_usd_per_mtok
        self._out_rate = output_usd_per_mtok


def _content(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        return content
    import json

    return json.dumps(content, default=str)
