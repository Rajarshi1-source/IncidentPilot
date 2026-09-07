"""Replay paging adapter (W7-04).

Returns the on-call the recording captured, so a fixture that exercised the
fallback ladder replays the *rung it actually reached*. Inventing a healthy
provider response here would make every degraded fixture score as a healthy one,
which is the specific way a harness ends up unable to see its own blind spot.
"""

from __future__ import annotations

from dataclasses import dataclass

from eval.recorder import KIND_ONCALL, Recording
from incidentpilot.adapters.paging.base import (
    OnCall,
    OnCallSource,
    PageResult,
    RetryablePagingError,
)


@dataclass(frozen=True, slots=True)
class PageCall:
    user: str
    incident_key: str
    severity: str


class ReplayPaging:
    """Recorded rota, recorded source, recorded failures."""

    def __init__(self, recording: Recording) -> None:
        self._entries = recording.of_kind(KIND_ONCALL)
        self.pages: list[PageCall] = []
        self.lookups: list[str] = []

    async def oncall_for(self, schedule: str) -> OnCall:
        self.lookups.append(schedule)
        entry = next(
            (e for e in self._entries if str(e.request.get("schedule") or "") == schedule),
            self._entries[0] if self._entries else None,
        )
        if entry is None:
            # No recorded lookup at all. The static-rota rung of the ladder is
            # what production would reach here, so say so rather than
            # fabricating a provider answer.
            return OnCall(
                schedule=schedule,
                primary=None,
                source=OnCallSource.TEAM_BROADCAST,
                reason="no recorded on-call lookup for this incident",
            )

        response = entry.response
        if response.get("error"):
            raise RetryablePagingError(str(response["error"]))

        return OnCall(
            schedule=schedule,
            primary=response.get("user") or response.get("primary"),
            secondary=response.get("secondary"),
            source=OnCallSource(str(response.get("source") or OnCallSource.PROVIDER)),
            reason=response.get("reason"),
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
        existing = next(
            (p for p in self.pages if p.user == user and p.incident_key == incident_key), None
        )
        if existing is not None:
            return PageResult(responder=user, delivered=True, ref=f"dedup:{incident_key}")
        self.pages.append(PageCall(user=user, incident_key=incident_key, severity=severity))
        return PageResult(responder=user, delivered=True, ref=f"replay-{len(self.pages):04d}")
