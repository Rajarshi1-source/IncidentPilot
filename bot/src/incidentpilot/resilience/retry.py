"""Retry policy.

The rule that matters: **retry only what is idempotent.** Retrying
``conversations.create`` without an idempotency key creates a second channel,
which is the exact bug the outbox exists to prevent -- so the retry helper
refuses to run an unkeyed call rather than trusting the caller to remember.

Full jitter, not equal jitter, because the failure mode is synchronized retry:
a storm makes N relay replicas fail at the same instant, and without
randomization they all come back at the same instant too.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable

from incidentpilot.adapters.chat.base import PermanentChatError, RetryableChatError
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 4
BASE_DELAY_S = 0.5
MAX_DELAY_S = 300.0

# Methods that mutate external state and therefore may only be retried when the
# caller supplies an idempotency key. Everything else is a read or is naturally
# set-valued (invite, pin, archive) and safe to repeat.
REQUIRES_IDEMPOTENCY_KEY: frozenset[str] = frozenset({"conversations.create"})


class NonIdempotentRetry(Exception):
    """A create-shaped call was offered for retry with no idempotency key."""


def full_jitter_delay(
    attempt: int, *, base: float = BASE_DELAY_S, cap: float = MAX_DELAY_S
) -> float:
    """``random.uniform(0, min(cap, base * 2**attempt))``.

    The randomization is the point. Exponential backoff alone re-synchronizes
    every failed caller onto the same retry instant, which is how a recovering
    dependency gets knocked over a second time.
    """
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)


def next_attempt_delay(attempts: int) -> float:
    """Delay before the relay re-claims a deferred outbox row."""
    return full_jitter_delay(attempts)


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    method: str,
    idempotency_key: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run ``operation``, retrying transient failures only.

    Refuses outright to retry a mutating call with no idempotency key. That is
    deliberate: making it a loud error rather than a silent single-attempt keeps
    the guarantee visible, because a caller who forgot the key has a bug either
    way and should be told.
    """
    if method in REQUIRES_IDEMPOTENCY_KEY and not idempotency_key:
        raise NonIdempotentRetry(
            f"{method} may not be retried without an idempotency key -- "
            "a retry would create a second channel (B-08)"
        )

    import asyncio

    do_sleep = sleep or asyncio.sleep
    last: Exception | None = None

    for attempt in range(max_attempts):
        try:
            return await operation()
        except PermanentChatError:
            # A 4xx fails identically forever. Retrying spends the rate-limit
            # budget on a call that cannot succeed.
            raise
        except RetryableChatError as exc:
            last = exc
            if attempt == max_attempts - 1:
                break
            delay = full_jitter_delay(attempt)
            log.warning(
                "retry.deferred",
                method=method,
                attempt=attempt + 1,
                delay_s=round(delay, 2),
                error=str(exc),
            )
            await do_sleep(delay)

    assert last is not None
    raise last
