"""The four levels, and the rule that they are announced (W7-17, INV-12, D7).

```
L0 NORMAL      everything green            full pipeline
L1 DEGRADED    provider open / over budget skeleton PIRs, deterministic timeline, banner
L2 BROWNOUT    Postgres unreachable        webhooks still 202, alerts buffered to the WAL,
                                           channel creation from cached state, replay later
L3 LIFEBOAT    our own readiness failing   a separate CronJob, a separate image, posts a manual
                                           runbook and the last known on-call to a fallback channel
```

**Silent degradation is worse than failure.** A product that fails visibly gets
worked around; a product that quietly starts producing less trustworthy output
keeps being trusted. So every level change posts in-channel and moves
``ip_degradation_level`` -- and the announcement is a *precondition* of the level
change, not a side effect of it: ``set_level`` records the new level only after
the banner has been attempted, so a level that could not be announced is
reported as an announcement failure rather than becoming an unannounced level.

**Levels are ratcheted down deliberately, never automatically.** Recovering
from L2 the moment one query succeeds produces a system that flaps between
levels during a partial outage, announcing each flap, which is worse than
staying degraded. ``recover`` exists and the caller decides when to use it.

**The gauge is the single writer.** ``telemetry.metrics.set_degradation_level``
is the only thing that touches ``DEGRADATION_LEVEL``, so the value cannot drift
between two components with different opinions about how bad things are.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from incidentpilot.telemetry.logging import get_logger
from incidentpilot.telemetry.metrics import set_degradation_level

log = get_logger(__name__)


class Level(IntEnum):
    NORMAL = 0
    DEGRADED = 1
    BROWNOUT = 2
    LIFEBOAT = 3


# What the channel is told. Written for a responder mid-incident, not for an
# operator reading a dashboard: it says what still works, because "degraded" on
# its own makes people stop trusting the parts that are fine.
BANNERS: dict[Level, str] = {
    Level.DEGRADED: (
        ":warning: *IncidentPilot is degraded.* AI drafting is unavailable ({reason}). "
        "Timeline, correlation, paging and the transcript are unaffected — the PIR will "
        "be posted as a deterministic skeleton you can edit."
    ),
    Level.BROWNOUT: (
        ":warning: *IncidentPilot is in brownout.* The database is unreachable ({reason}). "
        "Alerts are still being accepted and buffered durably and will be replayed on "
        "recovery. New war rooms and state changes are paused."
    ),
    Level.LIFEBOAT: (
        ":rotating_light: *IncidentPilot is unavailable* ({reason}). Fall back to the manual "
        "process. The lifeboat will post the last known on-call to the fallback channel."
    ),
}

RECOVERED = (
    ":white_check_mark: *IncidentPilot is back to normal.* Recovered from {level} ({reason})."
)


class DegradationManager:
    """Holds the current level, announces every change, moves the gauge."""

    def __init__(self, chat: Any = None, *, ops_channel: str | None = None) -> None:
        self._chat = chat
        self._ops_channel = ops_channel
        self._level = Level.NORMAL
        self.announcements: list[str] = []
        set_degradation_level(int(Level.NORMAL))

    @property
    def level(self) -> Level:
        return self._level

    async def set_level(self, level: Level, reason: str) -> bool:
        """Move to ``level``, announcing it. Returns whether it was announced.

        A repeated set to the same level is a no-op and is *not* re-announced:
        a brownout that re-asserts itself every five seconds would post twelve
        banners a minute into a channel people are trying to work in, and a
        channel full of banners is a channel where the banner stops being read.
        """
        if level == self._level:
            return True

        previous = self._level
        announced = await self._announce(level, reason)

        self._level = level
        set_degradation_level(int(level))
        log.warning(
            "degradation.changed",
            from_level=int(previous),
            to_level=int(level),
            reason=reason,
            announced=announced,
        )
        if not announced and level >= Level.DEGRADED:
            # The one case worth its own log line at error level. The system is
            # degraded and nobody was told, which is precisely the state
            # INV-12 exists to make impossible -- so it is recorded as its own
            # failure rather than folded into the level change.
            log.error("degradation.unannounced", to_level=int(level), reason=reason)
        return announced

    async def recover(self, reason: str = "dependencies healthy") -> bool:
        """Return to NORMAL, announcing that too.

        Recovery is announced for the same reason degradation is: people who
        were told to use the manual process need to be told to stop.
        """
        if self._level == Level.NORMAL:
            return True
        previous = self._level
        self._level = Level.NORMAL
        set_degradation_level(int(Level.NORMAL))
        message = RECOVERED.format(level=previous.name.lower(), reason=reason)
        self.announcements.append(message)
        log.info("degradation.recovered", from_level=int(previous), reason=reason)
        return await self._post(message)

    # -- properties the rest of the system reads --------------------------

    @property
    def llm_available(self) -> bool:
        return self._level < Level.DEGRADED

    @property
    def db_available(self) -> bool:
        return self._level < Level.BROWNOUT

    @property
    def accepting_writes(self) -> bool:
        """Ingest keeps accepting at every level below the lifeboat.

        A dropped alert is unrecoverable; a delayed one is not. That asymmetry
        is why ingest is the one AP component in an otherwise CP system, and
        why this property is not simply ``db_available``.
        """
        return self._level < Level.LIFEBOAT

    # -- announcement ------------------------------------------------------

    async def _announce(self, level: Level, reason: str) -> bool:
        if level < Level.DEGRADED:
            return True
        message = BANNERS[level].format(reason=reason)
        self.announcements.append(message)
        return await self._post(message)

    async def _post(self, message: str) -> bool:
        if self._chat is None or self._ops_channel is None:
            return False
        try:
            await self._chat.post_message(
                self._ops_channel,
                text=message,
                # Priority 0: this is the one message that must survive load
                # shedding. Shedding the notice that the system is degraded, in
                # order to protect the system, is a genuinely circular failure.
                priority=0,
            )
        except Exception as exc:
            # An announcement that raises would abort the degradation it was
            # announcing, leaving the system at the *old* level while the
            # dependency behind the new one is still broken.
            log.warning("degradation.announce_failed", error=str(exc)[:200])
            return False
        return True

    def said(self, needle: str) -> bool:
        """Whether anything announced contains ``needle``. For the gate's assertions."""
        return any(needle.lower() in message.lower() for message in self.announcements)
