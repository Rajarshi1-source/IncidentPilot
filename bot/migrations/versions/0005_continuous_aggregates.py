"""Continuous aggregates and their refresh policies.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06

The dashboard must never scan raw data. A page that re-scans ``signal_samples``
on every load is how you discover the difference between a query that works and
one that scales.

Conflict C-08: Rev 2's ``llm_cost_daily`` uses ``approx_percentile`` /
``percentile_agg``, which ship in **TimescaleDB Toolkit** -- a separate extension
from ``timescaledb``. It is present in the timescaledb-ha image but not in a
plain install. The committed aggregate therefore uses plain ``avg()``, and the
Toolkit percentile goes in a second, guarded aggregate so a Toolkit-less
environment degrades to the average rather than failing ``upgrade head``.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE MATERIALIZED VIEW incident_daily
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 day', time) AS day,
               intent,
               count(*)                    AS events,
               count(DISTINCT incident_id) AS incidents
        FROM timeline_events
        GROUP BY day, intent
        WITH NO DATA
        """
    )
    op.execute(
        """
        SELECT add_continuous_aggregate_policy('incident_daily',
          start_offset => INTERVAL '30 days',
          end_offset   => INTERVAL '1 hour',
          schedule_interval => INTERVAL '1 hour')
        """
    )

    op.execute(
        """
        CREATE MATERIALIZED VIEW llm_cost_daily
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 day', time) AS day, role, model, prompt_version,
               sum(cost_usd)   AS cost,
               count(*)        AS calls,
               avg(latency_ms) AS avg_ms
        FROM llm_calls
        GROUP BY day, role, model, prompt_version
        WITH NO DATA
        """
    )
    op.execute(
        """
        SELECT add_continuous_aggregate_policy('llm_cost_daily',
          start_offset => INTERVAL '90 days',
          end_offset   => INTERVAL '1 hour',
          schedule_interval => INTERVAL '1 hour')
        """
    )

    op.execute(
        """
        CREATE MATERIALIZED VIEW page_load_daily
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 day', time) AS day, responder,
               count(*) AS pages,
               count(*) FILTER (WHERE severity = 'sev1') AS sev1_pages
        FROM page_events
        GROUP BY day, responder
        WITH NO DATA
        """
    )
    op.execute(
        """
        SELECT add_continuous_aggregate_policy('page_load_daily',
          start_offset => INTERVAL '90 days',
          end_offset   => INTERVAL '1 hour',
          schedule_interval => INTERVAL '1 hour')
        """
    )

    # Toolkit-only, and optional (C-08). Wrapped so a plain timescaledb install
    # skips it with a notice instead of failing the migration.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE MATERIALIZED VIEW llm_latency_daily
            WITH (timescaledb.continuous) AS
            SELECT time_bucket('1 day', time) AS day, role, model,
                   percentile_agg(latency_ms) AS latency_agg
            FROM llm_calls
            GROUP BY day, role, model
            WITH NO DATA;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'timescaledb_toolkit unavailable; llm_latency_daily skipped';
        END
        $$;
        """
    )


def downgrade() -> None:
    for view in (
        "llm_latency_daily",
        "page_load_daily",
        "llm_cost_daily",
        "incident_daily",
    ):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view} CASCADE")
