"""In-memory paging adapter (W5-04).

Records every page so the crash matrix, the G5 gate and the week 7 replay can
assert on counts rather than on log lines. Deduplicates by ``incident_key`` the
way a real provider does, which is what makes "a retried page does not wake
someone twice" a testable claim rather than a hopeful comment.
"""

from __future__ import annotations

from dataclasses import dataclass

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


class FakePaging:
    """Configurable rota, recorded pages, injectable failure."""

    def __init__(
        self,
        *,
        rota: dict[str, tuple[str, str | None]] | None = None,
        fail: bool = False,
    ) -> None:
        self.rota = rota or {"default": ("U_PRIMARY", "U_SECONDARY")}
        self.pages: list[PageCall] = []
        self.oncall_lookups: list[str] = []
        self.fail = fail

    def call_count(self, user: str | None = None) -> int:
        return sum(1 for p in self.pages if user is None or p.user == user)

    async def oncall_for(self, schedule: str) -> OnCall:
        self.oncall_lookups.append(schedule)
        if self.fail:
            raise RetryablePagingError("injected paging provider failure")
        primary, secondary = self.rota.get(schedule, self.rota.get("default", (None, None)))
        return OnCall(
            schedule=schedule,
            primary=primary,
            secondary=secondary,
            source=OnCallSource.PROVIDER,
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
        if self.fail:
            raise RetryablePagingError("injected paging provider failure")

        # Deduplicated the way a real provider is: a retry after a crash between
        # the page and the record must reach the same alert, not open a second
        # one. Without this the outbox's at-least-once retry would wake someone
        # twice per incident and the pager would stop being believed.
        existing = next(
            (p for p in self.pages if p.user == user and p.incident_key == incident_key), None
        )
        if existing is not None:
            return PageResult(responder=user, delivered=True, ref=f"dedup:{incident_key}")

        self.pages.append(PageCall(user=user, incident_key=incident_key, severity=severity))
        return PageResult(
            responder=user,
            delivered=True,
            ref=f"fake-{len(self.pages):04d}",
            detail={"title": title, "url": url},
        )
