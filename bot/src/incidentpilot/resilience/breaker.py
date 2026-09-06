"""Circuit breakers, one per dependency.

**Per dependency, never global.** A global breaker means one flaky dependency
opens the circuit for all of them: the model provider times out and suddenly
Slack calls are being refused too. That is a common and damaging mistake, and
``test_breaker_is_per_dependency`` exists to stop it being reintroduced.

Configuration, from the plan: 5 failures in 60 s opens; 30 s open; a single
probe in half-open.
"""

from __future__ import annotations

from typing import Any

import pybreaker

from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

FAIL_MAX = 5
RESET_TIMEOUT_S = 30


class _BreakerLog(pybreaker.CircuitBreakerListener):
    """A breaker that opens without saying so is an outage nobody can explain."""

    def __init__(self, name: str) -> None:
        self.name = name

    def state_change(self, cb: pybreaker.CircuitBreaker, old: Any, new: Any) -> None:
        old_name = getattr(old, "name", str(old))
        new_name = getattr(new, "name", str(new))
        level = log.warning if new_name != "closed" else log.info
        level("breaker.state_change", dependency=self.name, from_state=old_name, to_state=new_name)

    def failure(self, cb: pybreaker.CircuitBreaker, exc: BaseException) -> None:
        log.warning("breaker.failure", dependency=self.name, error=str(exc))


class BreakerRegistry:
    """One breaker per named dependency, created on first use.

    A registry rather than module-level singletons so tests get a clean set per
    case -- a breaker left open by one test is a confusing failure in the next.
    """

    def __init__(
        self,
        *,
        fail_max: int = FAIL_MAX,
        reset_timeout_s: int = RESET_TIMEOUT_S,
    ) -> None:
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout_s
        self._breakers: dict[str, pybreaker.CircuitBreaker] = {}

    def for_dependency(self, name: str) -> pybreaker.CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = pybreaker.CircuitBreaker(
                fail_max=self._fail_max,
                reset_timeout=self._reset_timeout,
                name=name,
                listeners=[_BreakerLog(name)],
                # A permanent 4xx must not count toward opening the circuit: the
                # dependency is healthy and we are asking it something invalid.
                # Counting those would open the breaker on our own bug and hide
                # it behind an apparent outage.
                exclude=[_is_permanent],
            )
        return self._breakers[name]

    def names(self) -> list[str]:
        return sorted(self._breakers)

    def reset_all(self) -> None:
        for breaker in self._breakers.values():
            breaker.close()


def _is_permanent(exc: BaseException) -> bool:
    from incidentpilot.adapters.chat.base import PermanentChatError

    return isinstance(exc, PermanentChatError)


class BreakerOpen(Exception):
    """The circuit for this dependency is open; the call was not attempted."""


def is_open_error(exc: BaseException) -> bool:
    return isinstance(exc, pybreaker.CircuitBreakerError)
