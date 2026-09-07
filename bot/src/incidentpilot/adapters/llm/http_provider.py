"""The two hosted providers plus the local one, over plain httpx (W6-12).

Three implementations, one file, and the file has **no model name in it**
(INV-07) -- the model arrives as an argument from ``config/models.yaml``.

They share a base class because they differ in exactly three ways: the URL path,
the auth header, and where the JSON lives in the response envelope. Writing them
as three separate 150-line modules would triple the surface on which the *shared*
behaviour -- timeout translation, retryable-vs-permanent classification, fenced
JSON recovery -- could drift apart.

**Why not the vendor SDKs.** Same reason as the paging adapter: `respx` records
and replays plain httpx, and INV-10 requires the week 7 corpus to run with zero
network calls. An SDK's internal client is far harder to intercept, and its own
retry and error types would leak through this interface and become something the
next implementation has to imitate.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from incidentpilot.adapters.llm.base import (
    Completion,
    PermanentProviderError,
    RetryableProviderError,
    estimate_cost,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

# Models wrap JSON in a fence more often than any prompt instruction prevents.
FENCED = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


class HTTPProvider:
    """Shared transport. Subclasses supply the three things that differ."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        input_usd_per_mtok: float = 0.0,
        output_usd_per_mtok: float = 0.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient()
        self._in_rate = input_usd_per_mtok
        self._out_rate = output_usd_per_mtok

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- the three differences -------------------------------------------

    def _path(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _body(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _extract(self, body: dict[str, Any]) -> tuple[str, int, int]:  # pragma: no cover
        raise NotImplementedError

    # -- the shared path -------------------------------------------------

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
        if not self._api_key and self.name != "local":
            raise PermanentProviderError(f"no API key configured for provider {self.name!r}")

        started = time.perf_counter()
        payload = self._body(
            model=model,
            system=system,
            user=user,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

        try:
            response = await self._client.post(
                f"{self._base}{self._path()}",
                json=payload,
                headers=self._headers(),
                timeout=timeout_s,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableProviderError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableProviderError(f"{self.name} {response.status_code}")
        if response.status_code >= 400:
            raise PermanentProviderError(
                f"{self.name} {response.status_code}: {response.text[:200]}"
            )

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RetryableProviderError(f"{self.name} returned a non-JSON envelope") from exc

        text, input_tokens, output_tokens = self._extract(body)
        parsed = _parse(text, schema, provider=self.name)

        return Completion(
            parsed=parsed,
            raw_text=text,
            provider=self.name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(
                input_tokens,
                output_tokens,
                input_usd_per_mtok=self._in_rate,
                output_usd_per_mtok=self._out_rate,
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _parse(text: str, schema: type[BaseModel], *, provider: str) -> BaseModel:
    """Parse the model's output, recovering a fenced block if there is one.

    A schema mismatch is **permanent**, not retryable: the same prompt will
    produce the same shape, and the generator's job is to move down the chain
    rather than spend the budget again on an identical failure.
    """
    candidate = text.strip()
    fenced = FENCED.search(candidate)
    if fenced is not None:
        candidate = fenced.group(1).strip()

    try:
        return schema.model_validate_json(candidate)
    except ValidationError as exc:
        log.warning("llm.schema_mismatch", provider=provider, error=str(exc)[:400])
        raise PermanentProviderError(f"{provider} output does not match the schema") from exc


class OpenAICompatibleProvider(HTTPProvider):
    """Chat Completions shape. Also what the local runtime speaks."""

    name = "openai"

    def _path(self) -> str:
        return "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "model": kwargs["model"],
            "messages": [
                {"role": "system", "content": kwargs["system"]},
                {"role": "user", "content": kwargs["user"]},
            ],
            "max_completion_tokens": kwargs["max_output_tokens"],
            "temperature": kwargs["temperature"],
            # Asked for at the API level, not only in the prompt. A schema
            # instruction the model may ignore is not a constraint.
            "response_format": {"type": "json_object"},
        }

    def _extract(self, body: dict[str, Any]) -> tuple[str, int, int]:
        choices = body.get("choices") or []
        if not choices:
            raise RetryableProviderError("openai-compatible response has no choices")
        text = str((choices[0].get("message") or {}).get("content") or "")
        usage = body.get("usage") or {}
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


class LocalProvider(OpenAICompatibleProvider):
    """A self-hosted runtime behind an OpenAI-compatible endpoint.

    The same wire format, and worth naming separately because it is the answer
    to "we cannot send incident data outside our network at all" (§16) -- not a
    development convenience. Pointing ``synthesize`` here is a config change,
    which is the whole reason roles exist.
    """

    name = "local"


class AnthropicProvider(HTTPProvider):
    """The Messages API: a top-level system prompt and a content-block reply."""

    name = "anthropic"

    def _path(self) -> str:
        return "/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            # A dated header, not a version number in the client library: the
            # API's compatibility contract is pinned by this string.
            "anthropic-version": "2023-06-01",
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _body(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "model": kwargs["model"],
            # System is a top-level parameter here, not a message with
            # role="system" -- the single most common porting mistake between
            # the two APIs, and it fails as a 400 rather than as bad output.
            "system": kwargs["system"],
            "messages": [{"role": "user", "content": kwargs["user"]}],
            "max_tokens": kwargs["max_output_tokens"],
            "temperature": kwargs["temperature"],
        }

    def _extract(self, body: dict[str, Any]) -> tuple[str, int, int]:
        blocks = body.get("content") or []
        text = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
        if not text:
            raise RetryableProviderError("anthropic response has no text content")
        usage = body.get("usage") or {}
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def _dumps(value: Any) -> str:  # pragma: no cover - convenience for debugging
    return json.dumps(value, indent=2, default=str)
