"""Pick the runbook for an incident — from the ROOT SIGNAL (W5-11, D3/D4).

The one decision in this file that is worth an interview answer: the match is
made against the **root signal**, not the loudest alert.

During a storm forty alerts arrive and the noisiest is almost never the cause.
`postgres-primary-down` fires once; the thirty-nine `HighErrorRate` alerts it
causes fire on every service downstream of it. Matching on frequency, on
severity, or on "the first one we saw" pins a *High Error Rate* runbook to an
incident whose actual fix is in the database runbook — and the responder then
spends ten minutes working the wrong list while the real cause sits one hop away
in the dependency graph.

Week 2 already computes the root signal as the first alert on the deepest
dependency. This module's whole job is to not throw that away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from incidentpilot.runbooks.parser import ParsedRunbook
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RunbookMatch:
    runbook: ParsedRunbook
    matched_on: str
    score: int
    reason: str


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def match(
    runbooks: list[ParsedRunbook],
    *,
    root_signal: str | None,
    severity: str | None = None,
    service: str | None = None,
    fallback_alertname: str | None = None,
) -> RunbookMatch | None:
    """The best runbook for this incident, or None.

    ``root_signal`` is tried first and ``fallback_alertname`` only if the root
    signal matches nothing -- the fallback exists for an incident that never got
    a root signal (a single alert, no correlation), not as a second chance for
    the loudest alert to win.

    Returning None is a legitimate answer. A war room with no runbook is worse
    than one with the right runbook and far better than one with the wrong
    runbook, because a wrong runbook is followed.
    """
    for candidate_name, source in ((root_signal, "root_signal"), (fallback_alertname, "alertname")):
        if not candidate_name:
            continue
        best = _best_for(runbooks, candidate_name, severity=severity, service=service)
        if best is not None:
            runbook, score, reason = best
            log.info(
                "runbook.matched",
                runbook=runbook.name,
                matched_on=candidate_name,
                source=source,
                score=score,
            )
            return RunbookMatch(
                runbook=runbook, matched_on=candidate_name, score=score, reason=reason
            )

    log.info("runbook.no_match", root_signal=root_signal, alertname=fallback_alertname)
    return None


def _best_for(
    runbooks: list[ParsedRunbook],
    alertname: str,
    *,
    severity: str | None,
    service: str | None,
) -> tuple[ParsedRunbook, int, str] | None:
    """Highest specificity wins; filename order breaks ties.

    Specificity is counted rather than ranked by rule order, so adding a
    narrower runbook does not require reordering the ones already there. A
    runbook that names both a severity and a service is a deliberate, specific
    choice and should beat a generic pattern that happens to match too.
    """
    scored: list[tuple[ParsedRunbook, int, str]] = []
    for runbook in runbooks:
        if not _compiled(runbook.alert_pattern).search(alertname):
            continue

        reasons = [f"alert_pattern `{runbook.alert_pattern}`"]
        score = 1

        if runbook.severity_filter is not None:
            if severity is None or runbook.severity_filter != severity:
                continue
            score += 1
            reasons.append(f"severity {severity}")

        if runbook.service_filter is not None:
            if service is None or not _compiled(runbook.service_filter).search(service):
                continue
            score += 2  # a service filter is the narrowest statement available
            reasons.append(f"service {service}")

        scored.append((runbook, score, ", ".join(reasons)))

    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[0]
