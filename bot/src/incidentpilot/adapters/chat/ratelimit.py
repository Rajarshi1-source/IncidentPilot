"""Per-method, per-channel rate limiting with priority lanes (B-12).

Two corrections to Rev 1 are baked in here:

**Slack's tiers run the other way from intuition.** Tier 1 is the *most*
restrictive (~1+/min); Tier 4 is the most permissive (~100+/min). Rev 1 stated
it backwards. ``conversations.create`` is Tier 2 (~20/min), and
``chat.postMessage`` has its own allowance of roughly **one message per second
per channel** -- which is why buckets are keyed on ``(method, channel)`` and not
on method alone. A global per-method bucket would let one busy incident starve
every other channel.

**Priority ≥ 2 is dropped under pressure, not queued.** Queueing decorative
updates behind a storm only delays the critical ones. A stale timer is
invisible; a delayed war room is an outage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import SLACK_429

log = get_logger(__name__)

# Lower number preempts. The split is by consequence, not by convenience: 0 is
# "the incident does not exist without this", 3 is decoration.
PRIORITY: dict[str, int] = {
    "conversations.create": 0,
    "conversations.invite": 0,
    "chat.postMessage:runbook": 0,
    "chat.postMessage:pir": 1,
    "chat.postMessage": 1,
    "chat.update:timer": 2,
    "chat.update": 2,
    "reactions.add": 3,
}

# Anything at or above this is shed rather than delayed.
SHED_AT_PRIORITY = 2


@dataclass(frozen=True, slots=True)
class Bucket:
    """Leaky bucket parameters for one Slack method."""

    rate_per_s: float
    burst: int


# Conservative relative to the documented tiers: being rate-limited *by Slack*
# costs a round trip and a Retry-After wait, so it is cheaper to self-throttle
# slightly early than to discover the limit at 429.
RATES: dict[str, Bucket] = {
    "conversations.create": Bucket(rate_per_s=0.25, burst=5),  # Tier 2, ~20/min
    "conversations.invite": Bucket(rate_per_s=0.5, burst=10),
    "conversations.archive": Bucket(rate_per_s=0.25, burst=5),
    "chat.postMessage": Bucket(rate_per_s=1.0, burst=3),  # ~1/sec PER CHANNEL
    "chat.update": Bucket(rate_per_s=1.0, burst=3),
    "pins.add": Bucket(rate_per_s=0.5, burst=5),
    "reactions.add": Bucket(rate_per_s=1.0, burst=5),
    "chat.getPermalink": Bucket(rate_per_s=5.0, burst=20),
}

DEFAULT_BUCKET = Bucket(rate_per_s=1.0, burst=5)

# Atomic leaky bucket. Read-modify-write from Python would let two relay
# replicas both see room and both spend it, which is precisely the race that
# produces a 429 during a storm.
_LEAKY_BUCKET_LUA = """
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local burst    = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])

local state    = redis.call('HMGET', key, 'tokens', 'ts')
local tokens   = tonumber(state[1])
local last     = tonumber(state[2])

if tokens == nil then
  tokens = burst
  last = now
end

local elapsed = math.max(0, now - last)
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return allowed
"""


class BucketStore(Protocol):
    """The slice of Valkey the limiter needs."""

    def register_script(self, script: str) -> Any: ...


def bucket_for(method: str) -> Bucket:
    return RATES.get(method, DEFAULT_BUCKET)


def priority_for(method: str, *, purpose: str | None = None) -> int:
    """Resolve a method (optionally qualified by purpose) to a lane.

    ``chat.postMessage`` is priority 1 in general but priority 0 when it is the
    runbook: the runbook *is* the war room's reason to exist, while a status
    update is not.
    """
    if purpose:
        qualified = f"{method}:{purpose}"
        if qualified in PRIORITY:
            return PRIORITY[qualified]
    return PRIORITY.get(method, 1)


class SlackRateLimiter:
    """Decides whether a call proceeds, waits, or is dropped."""

    def __init__(self, client: Any, *, key_prefix: str = "ip:rl") -> None:
        self._client = client
        self._prefix = key_prefix
        self._script: Any | None = None

    def _key(self, method: str, channel: str | None) -> str:
        return f"{self._prefix}:{method}:{channel or 'global'}"

    async def _take_token(self, method: str, channel: str | None, now: float) -> bool:
        bucket = bucket_for(method)
        if self._script is None:
            self._script = self._client.register_script(_LEAKY_BUCKET_LUA)
        result = await self._script(
            keys=[self._key(method, channel)],
            args=[bucket.rate_per_s, bucket.burst, now, 3600],
        )
        return bool(int(result))

    async def acquire(
        self,
        method: str,
        channel: str | None,
        *,
        purpose: str | None = None,
        now: float,
    ) -> bool:
        """True to proceed, False to drop this call entirely.

        ``now`` is injected rather than read here so the limiter is
        deterministic under replay -- the same reason ``domain/`` has no clock.
        """
        priority = priority_for(method, purpose=purpose)

        if await self._take_token(method, channel, now):
            return True

        if priority >= SHED_AT_PRIORITY:
            # Shed the decorative path. Counted, never silent: a drop nobody can
            # see is indistinguishable from a bug.
            SLACK_429.labels(method=method, priority=str(priority)).inc()
            log.info("slack.shed", method=method, channel=channel, priority=priority)
            return False

        # Priority 0-1 is never dropped. The caller waits and retries; the relay
        # will re-claim the row on its next pass rather than blocking here.
        SLACK_429.labels(method=method, priority=str(priority)).inc()
        log.warning("slack.throttled", method=method, channel=channel, priority=priority)
        return False

    @staticmethod
    def backoff_seconds(attempt: int, *, cap: float = 300.0) -> float:
        """Exponential with full jitter, capped.

        Full jitter rather than equal jitter because the failure mode here is
        synchronized retry: a storm makes N relay replicas fail at the same
        instant, and without randomization they all come back at the same
        instant too.
        """
        return min(cap, math.pow(2, attempt))
