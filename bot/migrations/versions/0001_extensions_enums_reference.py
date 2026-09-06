"""Extensions, enums, and the reference tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-06

Order matters and is the C-09 resolution. Rev 2 §6.2 presents ``incidents``
before ``runbooks``, but ``incidents.runbook_id`` has ``REFERENCES runbooks(id)``
and ``runbooks`` is never defined in that section at all. Reference tables come
first: extensions -> enums -> services/runbooks/responders -> incidents.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- extensions ----------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Toolkit is a SEPARATE extension from timescaledb (conflict C-08). It ships
    # in the timescaledb-ha image but not in a plain timescaledb install, so it
    # is created best-effort and every dependent aggregate degrades without it
    # rather than failing `alembic upgrade head`.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'timescaledb_toolkit unavailable; percentile aggregates will be skipped';
        END
        $$;
        """
    )

    # --- enums ---------------------------------------------------------
    op.execute(
        """
        CREATE TYPE incident_state AS ENUM (
          'detected','triaging','engaged','acknowledged','mitigating','mitigated',
          'resolved','reopened','pir_drafting','pir_drafted','pir_failed',
          'closed','merged','false_positive','abandoned')
        """
    )
    op.execute("CREATE TYPE severity_level AS ENUM ('sev1','sev2','sev3','sev4')")
    op.execute(
        "CREATE TYPE outbox_status AS ENUM ('pending','claimed','dispatched','failed','dead')"
    )

    # --- reference tables ----------------------------------------------
    op.create_table(
        "services",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("team", sa.Text, nullable=False),
        # tier 1 = user-facing critical. Tier is data about the service, not
        # about the alert, which is why severity looks it up rather than
        # inferring it from labels.
        sa.Column("tier", sa.SmallInteger, nullable=False, server_default="2"),
        sa.Column("slo_target", sa.Numeric(6, 4)),
        sa.Column("slo_window_days", sa.SmallInteger, nullable=False, server_default="30"),
        # Opaque to us; the paging adapter resolves it. Storing a vendor's id
        # shape here would leak the vendor into the schema.
        sa.Column("paging_service_ref", sa.Text),
        sa.Column(
            "depends_on",
            sa.ARRAY(sa.Integer),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        # Cached rather than computed per alert: correlation runs on the hot
        # path, and a recursive CTE per alert during a storm is exactly the
        # wrong place to spend milliseconds. Recompute when depends_on changes.
        sa.Column("graph_depth", sa.SmallInteger),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "runbooks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("alert_pattern", sa.Text, nullable=False),
        sa.Column("severity_filter", postgresql.ENUM(name="severity_level", create_type=False)),
        sa.Column("service_filter", sa.Text),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("step_ids", sa.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version", sa.Text, nullable=False, server_default="1.0.0"),
        sa.Column("git_sha", sa.Text),  # provenance for the D4 auto-PR loop
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("CREATE INDEX idx_runbook_active ON runbooks (is_active) WHERE is_active")

    op.create_table(
        "responders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slack_user_id", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("paging_user_ref", sa.Text),
        sa.Column("team", sa.Text),
        # Fatigue scoring needs local hours: a page at 03:00 IST is a night page
        # regardless of what UTC says.
        sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("responders")
    op.execute("DROP INDEX IF EXISTS idx_runbook_active")
    op.drop_table("runbooks")
    op.drop_table("services")
    op.execute("DROP TYPE IF EXISTS outbox_status")
    op.execute("DROP TYPE IF EXISTS severity_level")
    op.execute("DROP TYPE IF EXISTS incident_state")
    # Extensions are deliberately left in place: dropping timescaledb would take
    # every hypertable in the database with it, including ones this migration
    # never created.
