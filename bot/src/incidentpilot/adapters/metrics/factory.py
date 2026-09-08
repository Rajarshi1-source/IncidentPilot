"""Resolve the metrics adapter from configuration (W8-14).

The third factory, and it arrives late for a telling reason: until week 8
nothing in ``src/`` ever constructed a metrics adapter, because nothing in
``src/`` ever constructed a ``PIRGenerator``. The impact computation was built,
gate-tested and never run by the product.

Same contract as the chat and paging factories, including demo mode: a demo must
not reach a real Prometheus any more than it may reach a real Slack.
"""

from __future__ import annotations

from typing import Any

from incidentpilot.config.settings import Settings
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)


def build_metrics(cfg: Settings) -> Any:
    """Prometheus when configured and not in demo mode; the deterministic fake otherwise.

    The fake is not a stub that returns zeros -- it derives a stable value from
    a hash of the query, so the same window returns the same number on every
    run. That is what lets a clean clone show a populated impact block without
    a metrics backend, and it is why the demo's numbers do not move between
    rehearsals.

    Note what neither adapter does: invent a value when the backend is
    unreachable. `PrometheusMetrics` raises, and ``impact/promql.py`` records
    "unavailable" -- an honest gap in a postmortem is recoverable, a confident
    fabrication is not.
    """
    provider = "fake" if cfg.demo_mode else cfg.metrics_provider

    if provider == "prometheus":
        from incidentpilot.adapters.metrics.prometheus import PrometheusMetrics

        log.info("metrics.provider_resolved", provider="prometheus")
        return PrometheusMetrics(base_url=cfg.prometheus_url)

    from incidentpilot.adapters.metrics.fake import FakeMetrics

    log.info("metrics.provider_resolved", provider="fake")
    return FakeMetrics()
