"""In-memory model provider (W6-12).

Three jobs, and the third is the one that earns its place:

1. A clean clone runs the whole PIR demo with no API key.
2. The week 7 replay performs zero network calls (INV-10).
3. **It inspects what was sent.** Every payload is recorded verbatim, which is
   what makes the D6 CI test possible: `test_no_raw_pii_reaches_provider` walks
   these payloads and fails the build if a raw email or card number ever crossed
   the boundary. A fake that only returned canned answers could not prove that.

Responses are a queue, so a test can script "fabricated citation, then valid" and
watch the generator retry and then succeed -- the D1 acceptance criterion.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from incidentpilot.adapters.llm.base import (
    Completion,
    PermanentProviderError,
    RetryableProviderError,
)


@dataclass(frozen=True, slots=True)
class SentPayload:
    """Exactly what left the boundary. The D6 test reads these."""

    role: str
    model: str
    system: str
    user: str

    @property
    def text(self) -> str:
        return f"{self.system}\n{self.user}"


@dataclass
class FakeLLM:
    name: str = "fake"
    responses: deque[Any] = field(default_factory=deque)
    payloads: list[SentPayload] = field(default_factory=list)
    fail_times: int = 0
    fail_with: type[Exception] = RetryableProviderError
    calls: int = 0

    def next_response(self, response: Any) -> None:
        """Queue one reply: a model instance, a dict, or a JSON string."""
        self.responses.append(response)

    def fail_next(self, times: int = 1, *, error: type[Exception] = RetryableProviderError) -> None:
        self.fail_times = times
        self.fail_with = error

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
        self.payloads.append(SentPayload(role=role, model=model, system=system, user=user))
        self.calls += 1

        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with(f"injected {self.fail_with.__name__} from the fake provider")

        if not self.responses:
            raise PermanentProviderError(
                "the fake provider has no queued response -- "
                "call next_response() with the reply this test expects"
            )

        response = self.responses.popleft()
        raw = _to_text(response)
        try:
            parsed = schema.model_validate_json(raw)
        except ValidationError as exc:
            # Deliberately a *permanent* error. A response that does not match
            # the schema will not match it on retry either, and the generator's
            # job is to move down the chain rather than spend the budget again.
            raise PermanentProviderError(f"fake response does not match schema: {exc}") from exc

        return Completion(
            parsed=parsed,
            raw_text=raw,
            provider=self.name,
            model=model,
            input_tokens=len(user) // 4,
            output_tokens=len(raw) // 4,
            cost_usd=0.0,
            latency_ms=1,
        )


def _to_text(response: Any) -> str:
    if isinstance(response, BaseModel):
        return response.model_dump_json()
    if isinstance(response, str):
        return response
    return json.dumps(response, default=str)
