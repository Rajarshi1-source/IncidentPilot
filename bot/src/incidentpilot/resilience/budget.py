"""The three-tier spend breaker: degrades, never blocks (W6-20).

Per incident, per day, per month. Three tiers because they fail differently: one
pathological incident, one bad afternoon, and one bad month need different
responses, and a single ceiling would either be too tight for the first or too
loose for the third.

**Tripping is not an error.** ``BudgetExceeded`` is caught by the generator and
turned into the skeleton, so the incident still reaches ``pir_drafted`` and a
document is still posted. A cost control that stops the product working is a
cost control someone disables the first time it fires.

State lives in Valkey with TTLs that match the windows, and every counter is
best-effort: an unreachable Valkey means the breaker **allows** the call. That
is the deliberate direction. Failing closed here would let a cache blip take
away the PIR feature, and the ceiling exists to stop a runaway loop, not to be
the last line of defence against a compromised process.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from incidentpilot.adapters.llm.base import BudgetExceeded
from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import BUDGET_TRIPS

log = get_logger(__name__)

INCIDENT = "incident"
DAY = "day"
MONTH = "month"

# Long enough to outlive any incident, short enough that the key set does not
# grow without bound.
INCIDENT_TTL_S = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Budgets:
    per_incident: float = 0.50
    per_day: float = 5.00
    per_month: float = 50.00


class BudgetBreaker:
    """Reads three counters, raises before the call rather than after it."""

    def __init__(
        self,
        valkey: Any,
        budgets: Budgets | None = None,
        *,
        prefix: str = "ip:budget",
        now: Any = None,
    ) -> None:
        self._valkey = valkey
        self._budgets = budgets or Budgets()
        self._prefix = prefix
        self._now = now or (lambda: datetime.now(UTC))

    # -- keys ------------------------------------------------------------

    def _incident_key(self, incident_id: int) -> str:
        return f"{self._prefix}:inc:{incident_id}"

    def _day_key(self) -> str:
        return f"{self._prefix}:day:{self._now().strftime('%Y-%m-%d')}"

    def _month_key(self) -> str:
        return f"{self._prefix}:month:{self._now().strftime('%Y-%m')}"

    # -- the gate --------------------------------------------------------

    async def check(self, incident_id: int, *, role: str = "", ceiling: float = 0.0) -> None:
        """Raise ``BudgetExceeded`` if any tier is spent.

        Checked **before** the prompt is rendered or anything is sent, so a
        tripped budget costs nothing at all rather than one wasted call.

        ``ceiling`` is the role's own per-incident allowance from
        ``models.yaml`` -- a `synthesize` call may be allowed where an `extract`
        call is not, because they cost two orders of magnitude apart.
        """
        limits = (
            (INCIDENT, self._incident_key(incident_id), ceiling or self._budgets.per_incident),
            (DAY, self._day_key(), self._budgets.per_day),
            (MONTH, self._month_key(), self._budgets.per_month),
        )
        for scope, key, limit in limits:
            if limit <= 0:
                continue
            spent = await self._read(key)
            if spent >= limit:
                BUDGET_TRIPS.labels(scope=scope).inc()
                log.warning(
                    "budget.tripped",
                    scope=scope,
                    role=role,
                    incident_id=incident_id,
                    spent_usd=round(spent, 4),
                    limit_usd=limit,
                )
                raise BudgetExceeded(f"{scope} budget exhausted: ${spent:.4f} of ${limit:.2f}")

    async def record(self, incident_id: int, cost_usd: float) -> None:
        """Add spend to all three tiers. Best-effort by design."""
        if cost_usd <= 0:
            return
        for key, ttl in (
            (self._incident_key(incident_id), INCIDENT_TTL_S),
            (self._day_key(), 2 * 24 * 3600),
            (self._month_key(), 40 * 24 * 3600),
        ):
            with contextlib.suppress(Exception):
                await self._valkey.incrbyfloat(key, cost_usd)
                await self._valkey.expire(key, ttl)

    async def spent(self, incident_id: int) -> float:
        return await self._read(self._incident_key(incident_id))

    async def _read(self, key: str) -> float:
        """An unreachable Valkey reads as zero, and that is deliberate.

        Failing closed would let a cache blip remove the PIR feature entirely.
        The ceiling exists to stop a runaway loop, not to be the last line of
        defence against a compromised process -- and the honest trade is stated
        here rather than discovered during an outage.
        """
        try:
            raw = await self._valkey.get(key)
        except Exception as exc:
            log.warning("budget.unreadable", key=key, error=str(exc))
            return 0.0
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except TypeError, ValueError:
            return 0.0
