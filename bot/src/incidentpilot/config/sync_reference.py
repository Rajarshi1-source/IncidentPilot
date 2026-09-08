"""Sync the checked-in reference data into the database (W8-11).

    python -m incidentpilot.config.sync_reference

**Why this exists.** `service-graph.yaml` ships in the repository and
`ServiceGraph` reads it in-process, so correlation's *topology* signal works
from the file. But `incidents.primary_service_id` is a foreign key into
`services`, and `services.graph_depth` is cached in the table because
correlation runs on the hot path and a recursive CTE per alert during a storm is
the wrong place to spend milliseconds.

On a clean clone that table is **empty**. Every alert then resolves to a NULL
service, the accumulated-services set stays empty, `_nearest_hop` finds nothing,
and the topology term contributes zero to every score. Forty correlated alerts
became **36 incidents** the first time week 8's demo ran against fresh volumes:
D3 -- the differentiator the whole of week 2 exists for -- silently absent on
precisely the scenario G8 tests.

It never showed before because every earlier gate ran against a database some
other test had already populated. That is the same defect as week 7's schema
that existed as a side effect of one module running first, and the same shape as
the un-awaited budget breaker: each half correct, the composition wrong.

Run after `alembic upgrade head` and before anything ingests. Idempotent, so
running it on every deploy is the intended usage rather than a risk.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.config.graph_loader import load_service_graph
from incidentpilot.config.settings import Settings
from incidentpilot.config.settings import settings as default_settings
from incidentpilot.db.engine import build_engine, build_session_factory
from incidentpilot.domain.graph import ServiceGraph
from incidentpilot.runtime import configure_event_loop
from incidentpilot.telemetry.logging import configure_logging, get_logger

log = get_logger(__name__)

# Which team owns what. Not in the graph file because the graph is about
# *dependencies* and this is about *people*; conflating them would mean an
# ownership change looked like a topology change to correlation.
TEAMS: dict[str, str] = {
    "postgres-primary": "data",
    "postgres-replica": "data",
    "payments-api": "payments",
    "payments-worker": "payments",
    "checkout-web": "storefront",
    "catalog-api": "catalog",
    "valkey-cache": "platform",
    "kafka-events": "platform",
    "notifications": "platform",
    "search-index": "catalog",
    "session-store": "storefront",
    "upi-gateway": "payments",
}

_UPSERT = text(
    """
    INSERT INTO services (name, team, tier, graph_depth)
    VALUES (:name, :team, :tier, :depth)
    ON CONFLICT (name) DO UPDATE
       SET graph_depth = EXCLUDED.graph_depth,
           team        = EXCLUDED.team
    """
)


async def sync_services(session: AsyncSession, graph: ServiceGraph | None = None) -> int:
    """Upsert every service in the graph, with its depth cached.

    ``graph_depth`` is recomputed on every run rather than only on insert. It is
    a *derived* value -- the plan says to recompute it whenever the graph
    changes -- and an ON CONFLICT that left it alone would mean editing
    `service-graph.yaml` silently had no effect on root-signal selection, which
    is the one thing depth decides.
    """
    resolved = graph or load_service_graph()
    names = sorted(str(n) for n in resolved.services())
    if not names:
        log.warning("reference.no_services_in_graph")
        return 0

    for name in names:
        depth = resolved.depth(name)
        await session.execute(
            _UPSERT,
            {
                "name": name,
                "team": TEAMS.get(name, "unassigned"),
                # Depth 0 is a datastore everything sits on: tier 1. Anything
                # above it is tier 2. Crude, and honest about being crude --
                # tier drives nothing today beyond display.
                "tier": 1 if depth == 0 else 2,
                "depth": depth,
            },
        )

    log.info("reference.services_synced", count=len(names))
    return len(names)


async def run(cfg: Settings | None = None) -> int:
    cfg = cfg or default_settings
    engine = build_engine(cfg)
    sessions = build_session_factory(engine)
    try:
        async with sessions() as session, session.begin():
            return await sync_services(session)
    finally:
        await engine.dispose()


def main() -> int:
    configure_event_loop()
    configure_logging(level="INFO", json_output=False)
    count = asyncio.run(run())
    print(f"synced {count} services from the checked-in dependency graph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
