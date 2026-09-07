"""A rota from a YAML file (W5-03).

The second implementation, and the reason the ``PagingAdapter`` Protocol is
worth having at all. An interface with one implementation is a guess about what
varies; this one is exercised on every clean clone, so the seam is real.

It is also the middle rung of the fallback ladder (W5-05, FMEA #14). When the
hosted provider is unreachable, "we do not know who is on call" is the worst
possible answer during an incident — a checked-in rota is a stale answer, and a
stale answer that says it is stale beats no answer.

Rotation is computed from the incident's timestamp, not from ``datetime.now()``:
this module is imported by the router, which the week 7 replay drives, and a
schedule that reads the wall clock would make the same recorded incident route
to a different person on Tuesday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from incidentpilot.adapters.paging.base import (
    OnCall,
    OnCallSource,
    PageResult,
    PermanentPagingError,
)
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parents[1].parent / "config" / "oncall.yaml"

# The rotation epoch. Fixed rather than "the first Monday of the file" so the
# same YAML produces the same rota on every machine and in every replay.
ROTATION_EPOCH = date(2026, 1, 5)  # a Monday


@dataclass(frozen=True, slots=True)
class Rotation:
    schedule: str
    members: list[str]
    period_days: int
    team_channel: str | None


class StaticSchedule:
    """Round-robin rotation over a checked-in list of responders."""

    def __init__(self, path: Path | None = None, *, at: datetime | None = None) -> None:
        self._path = path or DEFAULT_PATH
        self._at = at
        self._rotations: dict[str, Rotation] | None = None

    # -- loading ---------------------------------------------------------

    def rotations(self) -> dict[str, Rotation]:
        if self._rotations is None:
            self._rotations = _load(self._path)
        return self._rotations

    def team_channel_for(self, schedule: str) -> str | None:
        """The channel the last rung of the ladder broadcasts into."""
        rotation = self.rotations().get(schedule) or self.rotations().get("default")
        return rotation.team_channel if rotation else None

    # -- PagingAdapter ---------------------------------------------------

    async def oncall_for(self, schedule: str) -> OnCall:
        rotations = self.rotations()
        rotation = rotations.get(schedule) or rotations.get("default")
        if rotation is None or not rotation.members:
            return OnCall(
                schedule=schedule,
                primary=None,
                source=OnCallSource.STATIC_SCHEDULE,
                reason=f"no rotation defined for {schedule!r}",
            )

        index = self._slot(rotation)
        primary = rotation.members[index % len(rotation.members)]
        secondary = (
            rotation.members[(index + 1) % len(rotation.members)]
            if len(rotation.members) > 1
            else None
        )
        return OnCall(
            schedule=schedule,
            primary=primary,
            secondary=secondary,
            source=OnCallSource.STATIC_SCHEDULE,
            reason="resolved from the checked-in rota",
        )

    async def page(
        self,
        user: str,
        *,
        incident_key: str,
        title: str,
        severity: str,
        url: str | None = None,
    ) -> PageResult:
        """A YAML file cannot ring a phone, and pretending otherwise is worse
        than failing.

        Raising ``PermanentPagingError`` rather than returning
        ``delivered=False`` is deliberate: the caller's fallback for "cannot
        page" is a team-channel broadcast, and a soft failure would let the
        incident proceed believing someone had been woken up.
        """
        raise PermanentPagingError(
            "the static schedule can say who is on call but cannot deliver a page; "
            "the caller must fall back to a team-channel broadcast"
        )

    # -- rotation --------------------------------------------------------

    def _slot(self, rotation: Rotation) -> int:
        """Which rotation slot the reference instant falls in.

        ``at`` is injected by the caller (the incident's ``detected_at`` in
        production, a fixed instant under replay). It defaults to now only for
        an operator running this interactively, which is the one context where
        the wall clock is the right answer.
        """
        moment = self._at or datetime.now(UTC)
        days = (moment.date() - ROTATION_EPOCH).days
        period = max(rotation.period_days, 1)
        return days // period


def _load(path: Path) -> dict[str, Rotation]:
    if not path.is_file():
        log.warning("oncall.rota_missing", path=str(path))
        return {}

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    schedules = raw.get("schedules") or {}
    out: dict[str, Rotation] = {}
    for name, body in schedules.items():
        members = [str(m) for m in (body or {}).get("members") or []]
        out[str(name)] = Rotation(
            schedule=str(name),
            members=members,
            period_days=int((body or {}).get("period_days", 7)),
            team_channel=(body or {}).get("team_channel"),
        )
    return out
