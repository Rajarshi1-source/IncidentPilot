"""Hypertables, compression and retention.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06

Conversion order matters: create the plain table, convert it, set compression,
then add policies. Converting a table that already has data needs
``migrate_data => true`` and takes a lock, so do it while the table is empty.

The B-05 fix is the ``compress_segmentby`` choice. Segmenting ``timeline_events``
by ``incident_id`` -- the highest-cardinality column in the table -- produces one
tiny compressed batch per incident and defeats columnar compression entirely;
with per-batch overhead you can end up LARGER than uncompressed. Segment on the
low-cardinality dimension, order by time.

Hypertables reference ``incident_id`` as a plain column with no foreign key.
FK enforcement across many chunks is a chunk-management cost with little payoff
here; integrity is the application's job plus a periodic orphan check.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- timeline_events ------------------------------------------------
    op.create_table(
        "timeline_events",
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("incident_id", sa.BigInteger, nullable=False),
        sa.Column("intent", sa.Text, nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2)),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("author_user_id", sa.Text),
        sa.Column("source_message_ts", sa.Text),  # the citation anchor
        sa.Column(
            "metadata",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.execute(
        "SELECT create_hypertable('timeline_events','time', "
        "chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("CREATE INDEX ON timeline_events (incident_id, time DESC)")
    op.execute("CREATE INDEX ON timeline_events (intent, time DESC)")
    op.execute(
        """
        ALTER TABLE timeline_events SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'intent',
          timescaledb.compress_orderby   = 'incident_id, time DESC')
        """
    )
    op.execute("SELECT add_compression_policy('timeline_events', INTERVAL '14 days')")
    op.execute("SELECT add_retention_policy('timeline_events', INTERVAL '400 days')")

    # --- signal_samples: the table that actually justifies TimescaleDB ---
    op.create_table(
        "signal_samples",
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("incident_id", sa.BigInteger, nullable=False),
        sa.Column("series", sa.Text, nullable=False),
        sa.Column("service", sa.Text, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
    )
    # 1-day chunks because this is the high-rate table; the rule of thumb is
    # chunks that fit comfortably in memory, and too many tiny chunks is its own
    # planning cost.
    op.execute(
        "SELECT create_hypertable('signal_samples','time', chunk_time_interval => INTERVAL '1 day')"
    )
    op.execute("CREATE INDEX ON signal_samples (incident_id, series, time DESC)")
    op.execute(
        """
        ALTER TABLE signal_samples SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'series, service',
          timescaledb.compress_orderby   = 'time DESC')
        """
    )
    op.execute("SELECT add_compression_policy('signal_samples', INTERVAL '3 days')")
    op.execute("SELECT add_retention_policy('signal_samples', INTERVAL '90 days')")

    # --- llm_calls -------------------------------------------------------
    op.create_table(
        "llm_calls",
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("incident_id", sa.BigInteger),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("outcome", sa.Text, nullable=False),
    )
    op.execute(
        "SELECT create_hypertable('llm_calls','time', chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute(
        """
        ALTER TABLE llm_calls SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'role, model',
          timescaledb.compress_orderby   = 'time DESC')
        """
    )
    op.execute("SELECT add_compression_policy('llm_calls', INTERVAL '30 days')")

    # --- page_events -----------------------------------------------------
    # Conflict C-06: the time column is `time`, not `paged_at`. Rev 2's fatigue
    # query uses paged_at, which does not exist and would fail outright.
    op.create_table(
        "page_events",
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("responder", sa.Text, nullable=False),
        sa.Column("incident_id", sa.BigInteger),
        sa.Column("severity", postgresql.ENUM(name="severity_level", create_type=False)),
        sa.Column("tz", sa.Text, nullable=False, server_default="UTC"),
        sa.Column("accepted", sa.Boolean),
    )
    op.execute(
        "SELECT create_hypertable('page_events','time', chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("CREATE INDEX ON page_events (responder, time DESC)")
    op.execute(
        """
        ALTER TABLE page_events SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'responder',
          timescaledb.compress_orderby   = 'time DESC')
        """
    )
    op.execute("SELECT add_compression_policy('page_events', INTERVAL '30 days')")
    op.execute("SELECT add_retention_policy('page_events', INTERVAL '400 days')")

    # Deliberately NO retention on incidents, pir_documents or action_items.
    # They are small and they are the organizational memory -- deleting them
    # destroys the repeat-incident detection that makes the product worth having.


def downgrade() -> None:
    for table in ("page_events", "llm_calls", "signal_samples", "timeline_events"):
        op.execute(
            f"""
            DO $$
            DECLARE j RECORD;
            BEGIN
              FOR j IN SELECT job_id FROM timescaledb_information.jobs
                       WHERE hypertable_name = '{table}'
              LOOP
                PERFORM delete_job(j.job_id);
              END LOOP;
            EXCEPTION WHEN OTHERS THEN NULL;
            END $$;
            """
        )
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
