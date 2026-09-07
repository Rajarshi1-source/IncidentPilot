"""Resolves the paging provider from configuration.

Mirrors ``adapters/chat/factory.py``, and for the same reason: construction
lives in exactly one module so the INV-03 import-graph check stays meaningful.
Widening the exemption list every time an entrypoint needs to wire an adapter
would hollow the invariant out until it caught nothing.
"""

from __future__ import annotations

from typing import Any

from incidentpilot.config.settings import Settings
from incidentpilot.telemetry.logging import get_logger

log = get_logger(__name__)


def build_paging(cfg: Settings) -> Any:
    """Return the configured paging adapter.

    ``static`` is the default, which is what lets a clean clone run the full
    routing path -- ladder, fatigue score, announcement -- with no vendor
    account. Imports are deferred per branch so a deployment on the static rota
    never needs httpx configured for a provider it does not use.
    """
    if cfg.paging_provider == "pagerduty":
        from incidentpilot.adapters.paging.pagerduty import PagerDutyPaging

        log.info("paging.provider_resolved", provider="pagerduty")
        return PagerDutyPaging(
            routing_key=(
                cfg.paging_routing_key.get_secret_value() if cfg.paging_routing_key else ""
            ),
            api_token=cfg.paging_api_token.get_secret_value() if cfg.paging_api_token else None,
        )

    if cfg.paging_provider == "fake":
        from incidentpilot.adapters.paging.fake import FakePaging

        log.info("paging.provider_resolved", provider="fake")
        return FakePaging()

    from incidentpilot.adapters.paging.static_schedule import StaticSchedule

    log.info("paging.provider_resolved", provider="static")
    return StaticSchedule()
