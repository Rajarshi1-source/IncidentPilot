"""Severity assignment, with an auditable reason.

PURE MODULE (INV-01).

Severity drives paging, so it must be auditable. Every decision returns the
level *and* the sentence that justifies it -- "sev1: tier-1 service + error
budget burn 14.4x + 3 services affected". When someone asks in the review why
this was a Sev1, the answer is in the row, not in someone's memory.
"""

from __future__ import annotations

from dataclasses import dataclass

from incidentpilot.domain.normalize import SEVERITY_RANK

# A tier-1 service is user-facing and critical; tier 3 is internal tooling.
# Tier is data about the service, not about the alert, so it is looked up rather
# than inferred from labels.
TIER_FLOOR: dict[int, str] = {
    1: "sev2",  # a tier-1 service never opens below sev2
    2: "sev3",
    3: "sev4",
}

# Burn rate at which a slow-burn alert is treated as a page. 14.4x consumes 2%
# of a 30-day error budget in one hour, which is the Google SRE workbook's
# fast-burn threshold.
FAST_BURN = 14.4


@dataclass(frozen=True, slots=True)
class SeverityDecision:
    level: str
    rank: int
    reason: str

    @property
    def is_paging(self) -> bool:
        return self.level in {"sev1", "sev2"}


def _max_level(a: str, b: str) -> str:
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b


def assess(
    *,
    alert_severity: str,
    service_tier: int | None = None,
    affected_service_count: int = 1,
    error_budget_burn: float | None = None,
    tier_one_services: int = 0,
) -> SeverityDecision:
    """Combine the alert's own label with what we know about blast radius.

    The alert label is the floor, never the ceiling: Alertmanager rules are
    written per-alert and cannot know that three other services are already
    down. Escalation is always *upward* and always explained.
    """
    level = alert_severity if alert_severity in SEVERITY_RANK else "sev3"
    reasons: list[str] = [f"alert label {alert_severity}"]

    if service_tier is not None:
        floor = TIER_FLOOR.get(service_tier)
        if floor and SEVERITY_RANK[floor] > SEVERITY_RANK[level]:
            level = floor
            reasons.append(f"tier-{service_tier} service")
        elif service_tier == 1:
            reasons.append("tier-1 service")

    if error_budget_burn is not None and error_budget_burn >= FAST_BURN:
        level = _max_level(level, "sev1")
        reasons.append(f"error budget burn {error_budget_burn:.1f}x")

    if tier_one_services >= 2:
        level = _max_level(level, "sev1")
        reasons.append(f"{tier_one_services} tier-1 services affected")
    elif affected_service_count >= 3:
        level = _max_level(level, "sev2")
        reasons.append(f"{affected_service_count} services affected")

    return SeverityDecision(
        level=level,
        rank=SEVERITY_RANK[level],
        reason=f"{level}: " + " + ".join(reasons),
    )


def escalate(
    current: str,
    target: str,
    *,
    actor: str,
    note: str = "",
) -> SeverityDecision:
    """Raise severity mid-incident. Never lowers it silently.

    A de-escalation is a real thing people want, but it must be explicit and
    attributed -- an incident that quietly drops from sev1 to sev3 while nobody
    is looking is how a page stops arriving.
    """
    target_level = target if target in SEVERITY_RANK else current
    if SEVERITY_RANK[target_level] < SEVERITY_RANK[current]:
        reason = f"{current}: de-escalation to {target_level} requested by {actor} — refused"
        return SeverityDecision(level=current, rank=SEVERITY_RANK[current], reason=reason)

    detail = f" ({note})" if note else ""
    return SeverityDecision(
        level=target_level,
        rank=SEVERITY_RANK[target_level],
        reason=f"{target_level}: escalated from {current} by {actor}{detail}",
    )
